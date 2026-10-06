from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError, PlaywrightWorker
from app.service import CoreService


PASSWORD = "long-run-test-password"


class _NoopBitBrowser:
    def __init__(self):
        self.closed = []

    def close_profile(self, profile_id):
        self.closed.append(profile_id)
        return {"closed": True}


class _BaseWorker:
    def __init__(self, _bitbrowser: object) -> None:
        self.profile_id = ""

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id

    async def disconnect(self) -> None:
        return None

    async def collect_following(
        self, target: str, *, limit: int | None
    ) -> CollectionOutcome:
        del target, limit
        return CollectionOutcome("following", [])

    async def collect_post_likers(
        self, target: str, *, max_posts: int, per_post_limit: int
    ) -> CollectionOutcome:
        del target, max_posts, per_post_limit
        return CollectionOutcome("post_likers", [])

    async def read_visible_profile(
        self, target: str, *, include_activity: bool = False
    ) -> dict[str, Any]:
        return {
            "username": target,
            "visibility": "public",
            "followers": 10,
            "following": 10,
            "posts": 1,
            "activity_days": 0 if include_activity else None,
        }

    async def read_visible_account_location(self, target: str) -> str:
        del target
        return "美国"


class _PermanentIncompleteWorker(_BaseWorker):
    attempts: dict[str, int] = {}

    async def collect_followers(
        self, target: str, *, limit: int | None
    ) -> CollectionOutcome:
        del limit
        type(self).attempts[target] = type(self).attempts.get(target, 0) + 1
        if target == "permanent_stall":
            raise WorkerExecutionError(
                "relationship list made no progress",
                reason="instagram_followers_list_incomplete",
                pause_required=True,
                status_code=503,
            )
        return CollectionOutcome("followers", [f"{target}_fan"], source_total=1)


class _TransientNetworkWorker(_BaseWorker):
    connects = 0
    attempts = 0
    healthy = True

    async def connect(self, profile_id, *, open_if_needed=True):
        type(self).connects += 1
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def connection_healthy(self):
        return type(self).healthy

    async def collect_followers(self, target, *, limit):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise WorkerExecutionError("WAN temporarily unavailable", reason="instagram_network_unavailable", status_code=503)
        return CollectionOutcome("followers", [target + "_fan"], source_total=1)


class _PermanentSurfaceWorker(_BaseWorker):
    attempts: dict[str, int] = {}

    async def collect_followers(
        self, target: str, *, limit: int | None
    ) -> CollectionOutcome:
        del limit
        type(self).attempts[target] = type(self).attempts.get(target, 0) + 1
        if target == "permanent_surface":
            raise WorkerExecutionError(
                "Instagram surface never became ready",
                reason="instagram_profile_temporarily_unavailable",
                pause_required=True,
                status_code=503,
            )
        return CollectionOutcome("followers", [f"{target}_fan"], source_total=1)


class _TemporaryRecoveryWorker(_TransientNetworkWorker):
    replacements = []

    def request_page_replacement(self, target, reason):
        type(self).replacements.append((target, reason))

    async def collect_followers(self, target, *, limit):
        type(self).attempts += 1
        if type(self).attempts == 1:
            error=PlaywrightWorker._page_recovery_exhausted(
                WorkerExecutionError("temporary blank", reason="browser_window_surface_unstable"), target)
            error.details["retry_after_seconds"]=0
            raise error
        return CollectionOutcome("followers", [target+"_fan"], source_total=1)


class _ResumeTailWorker(_BaseWorker):
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    attempts = 0
    received_resume_tails: list[list[str]] = []
    replacements: list[tuple[str, str]] = []

    def request_page_replacement(self, target, reason):
        type(self).replacements.append((target, reason))

    async def collect_followers(
        self,
        target: str,
        *,
        limit: int | None,
        candidate_sink: Any,
        initial_candidate_count: int,
        progress_sink: Any,
        initial_resume_tail: list[str] | None = None,
    ) -> CollectionOutcome:
        del target, limit, initial_candidate_count
        type(self).attempts += 1
        type(self).received_resume_tails.append(list(initial_resume_tail or []))
        if type(self).attempts == 1:
            await candidate_sink(["tail_one", "tail_two"])
            await progress_sink(
                {
                    "source_total": 5000,
                    "resume_tail": ["Tail_One", "@Tail_Two"],
                    "rendered_count": 212,
                    "progress_epoch": 7,
                }
            )
            raise WorkerExecutionError(
                "list paused after durable progress",
                reason="instagram_followers_list_incomplete",
                pause_required=True,
                status_code=503,
            )
        await candidate_sink(["tail_three"])
        await progress_sink(
            {
                "source_total": 5000,
                "resume_tail": ["tail_two", "tail_three"],
                "rendered_count": 300,
                "progress_epoch": 8,
            }
        )
        return CollectionOutcome("followers", [], source_total=5000)


class _GenerationService:
    def __init__(self) -> None:
        self.renewals: list[tuple[str, str]] = []
        self.first_renewal = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def renew_browser_lease(self, profile_id: str, token: str) -> None:
        self.renewals.append((profile_id, token))
        self.loop.call_soon_threadsafe(self.first_renewal.set)

    def get_task(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        del owner_user_id, task_id
        return {"status": "running", "last_error": None}

    def set_task_runtime_status(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _ImmediateGenerationWorker(_BaseWorker):
    async def collect_followers(
        self, target: str, *, limit: int | None
    ) -> CollectionOutcome:
        del target, limit
        return CollectionOutcome("followers", [])


class CollectionLongRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "long-run.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user("long-run-user", PASSWORD)
        self.managers: list[ExecutionManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.shutdown()
        self.temp_dir.cleanup()

    def _task(self, targets: list[str], window: str = "long-run-window") -> dict[str, Any]:
        return self.service.create_task(
            self.user["id"],
            name="24小时等效耐久任务",
            modes=["followers"],
            targets=targets,
            window_ids=[window],
            settings={
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )

    async def test_page_network_failure_reuses_live_cdp_but_reconnects_dead_cdp(self):
        for healthy in (True, False):
            with self.subTest(healthy=healthy):
                _TransientNetworkWorker.healthy=healthy
                _TransientNetworkWorker.connects=0
                _TransientNetworkWorker.attempts=0
                task=self._task(["wan_alive" if healthy else "wan_dead"])
                manager=ExecutionManager(self.service,_NoopBitBrowser(),worker_factory=_TransientNetworkWorker,network_retry_delays=(0,))
                self.managers.append(manager)
                await manager.start(self.user["id"],task["id"])
                await asyncio.wait_for(manager.wait(task["id"]),timeout=8)
                stored=self.service.get_task(self.user["id"],task["id"])
                self.assertEqual("completed",stored["targets"][0]["status"])
                self.assertEqual(2,_TransientNetworkWorker.attempts)
                self.assertEqual(1 if healthy else 2,_TransientNetworkWorker.connects)
                self.assertEqual(["long-run-window"], manager.bitbrowser.closed)

    async def test_failed_new_page_automatically_retries_without_disconnecting_old_page(self):
        _TemporaryRecoveryWorker.attempts=0
        _TemporaryRecoveryWorker.connects=0
        _TemporaryRecoveryWorker.healthy=True
        _TemporaryRecoveryWorker.replacements=[]
        task=self._task(["temporary_recovery"])
        manager=ExecutionManager(self.service,_NoopBitBrowser(),worker_factory=_TemporaryRecoveryWorker,network_retry_delays=(0,), recovery_cooldown_seconds=.2)
        self.managers.append(manager)
        retry_entered = asyncio.Event()
        observation_done = asyncio.Event()
        wait_retry_delay = manager._wait_retry_delay

        async def observed_retry(*args):
            # Observe the durable waiting state before the zero-delay automatic
            # retry can consume it. Fixed sleep counts race worker startup and
            # may also miss the entire transient state on a busy build machine.
            retry_entered.set()
            await observation_done.wait()
            return await wait_retry_delay(*args)

        manager._wait_retry_delay = observed_retry
        try:
            await manager.start(self.user["id"],task["id"])
            control=manager._runs[task["id"]]
            await asyncio.wait_for(retry_entered.wait(), timeout=8)
            self.assertTrue(control.network_waiters)
            self.assertEqual(1,_TemporaryRecoveryWorker.attempts)
            self.assertEqual(1,_TemporaryRecoveryWorker.connects)
            waiter=next(iter(control.network_waiters.values()))
            self.assertEqual("waiting_network",waiter["state"])
            self.assertIsNotNone(waiter.get("next_retry_at"))
        finally:
            observation_done.set()
        # No Continue/Retry call: the timer must resume the original read itself.
        await asyncio.wait_for(manager.wait(task["id"]),timeout=8)
        self.assertEqual("completed",self.service.get_task(self.user["id"],task["id"])["targets"][0]["status"])
        self.assertEqual(2,_TemporaryRecoveryWorker.attempts)
        self.assertEqual(1,_TemporaryRecoveryWorker.connects)
        self.assertEqual([("temporary_recovery", "browser_window_surface_unstable")], _TemporaryRecoveryWorker.replacements)
        self.assertEqual(["long-run-window"], manager.bitbrowser.closed)

    def test_login_and_unknown_recovery_failures_remain_manual(self):
        for reason in ("instagram_login_required","instagram_challenge","instagram_rate_limited","unknown"):
            error=PlaywrightWorker._page_recovery_exhausted(WorkerExecutionError("check required",reason=reason))
            self.assertTrue(ExecutionManager._is_profile_intervention_error(error))
            self.assertFalse(ExecutionManager._is_instagram_surface_retry_error(error))

    async def _assert_permanent_failure_retains_target_during_cooldown(
        self, worker_type, source, reason, **limits
    ) -> None:
        worker_type.attempts = {}
        task = self._task([source, "healthy_after_stall"])
        manager = ExecutionManager(
            self.service, _NoopBitBrowser(), worker_factory=worker_type,
            network_retry_delays=(0,), network_retry_stagger_seconds=0,
            recovery_cooldown_seconds=30, **limits,
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])
        control = manager._runs[task["id"]]
        async def wait_for_cooldown():
            while True:
                waiter = control.network_waiters.get("long-run-window", {})
                if waiter.get("reason") == reason:
                    return waiter
                await asyncio.sleep(.01)
        waiter = await asyncio.wait_for(wait_for_cooldown(), timeout=5)
        stored = self.service.get_task(self.user["id"], task["id"])
        by_username = {target["username"]: target for target in stored["targets"]}
        original = by_username[source]
        self.assertEqual("waiting_network", original["status"])
        self.assertEqual("long-run-window", original["current_window_id"])
        self.assertEqual(3, worker_type.attempts[source])
        self.assertNotIn("healthy_after_stall", worker_type.attempts)
        self.assertEqual("waiting_network", waiter["state"])
        self.assertGreaterEqual(waiter["retry_delay_seconds"], 30)
        self.assertIsNotNone(waiter["next_retry_at"])
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], original["id"], "followers"
        )
        self.assertEqual("waiting_network", checkpoint["stage"])
        self.assertEqual(reason, checkpoint["counters"]["reason"])
        # Stop cancels the delayed retry. The next target must not inherit an
        # occupied window before its current owner has actually finished cleanup.
        await manager.stop(self.user["id"], task["id"], close_windows=False)
        self.assertEqual(3, worker_type.attempts[source])
        self.assertNotIn("healthy_after_stall", worker_type.attempts)
        self.assertEqual("stopped", self.service.get_task(self.user["id"], task["id"])["status"])

    async def test_permanent_incomplete_cools_down_without_releasing_current_target(self) -> None:
        await self._assert_permanent_failure_retains_target_during_cooldown(
            _PermanentIncompleteWorker, "permanent_stall",
            "instagram_relationship_list_no_progress",
            relationship_no_progress_retry_limit=2,
        )

    async def test_permanent_surface_cools_down_without_releasing_current_target(self) -> None:
        await self._assert_permanent_failure_retains_target_during_cooldown(
            _PermanentSurfaceWorker, "permanent_surface",
            "instagram_surface_no_progress", surface_no_progress_retry_limit=2,
        )

    async def test_resume_tail_survives_retry_and_final_checkpoint(self) -> None:
        _ResumeTailWorker.attempts = 0
        _ResumeTailWorker.received_resume_tails = []
        _ResumeTailWorker.replacements = []
        task = self._task(["large_relation"])
        manager = ExecutionManager(
            self.service,
            _NoopBitBrowser(),
            worker_factory=_ResumeTailWorker,
            network_retry_delays=(0,),
            relationship_no_progress_retry_limit=2,
        )
        self.managers.append(manager)

        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)

        # A failed initial pass resumes its tail. Its first natural end still
        # leaves a header gap, so the one supplemental pass starts at the top.
        self.assertEqual([[], ["tail_one", "tail_two"], []], _ResumeTailWorker.received_resume_tails)
        self.assertEqual(3, _ResumeTailWorker.attempts)
        self.assertEqual([("large_relation", "instagram_followers_list_incomplete")], _ResumeTailWorker.replacements)
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], task["targets"][0]["id"], "followers"
        )
        self.assertEqual("mode_completed", checkpoint["stage"])
        self.assertEqual(["tail_two", "tail_three"], checkpoint["cursor"]["resume_tail"])
        self.assertEqual(300, checkpoint["cursor"]["rendered_count"])
        self.assertEqual(8, checkpoint["cursor"]["progress_epoch"])
        self.assertTrue(checkpoint["cursor"]["automatic_gap_recheck_started"])
        self.assertEqual(3, checkpoint["counters"]["discovered"])
        self.assertEqual(5000, checkpoint["counters"]["source_total"])
        self.assertEqual(["long-run-window"], manager.bitbrowser.closed)

    async def test_24_hour_equivalent_heartbeats_and_worker_generations_stay_bounded(self) -> None:
        service = _GenerationService()
        manager = ExecutionManager(
            service,  # type: ignore[arg-type]
            _NoopBitBrowser(),
            worker_factory=_ImmediateGenerationWorker,
            lease_heartbeat_interval_seconds=3600,
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id="owner",
            task_id="24-hour-equivalent",
            pause_event=pause,
            stop_event=asyncio.Event(),
            leases={"window-0": "lease-0"},
            target_queue=asyncio.Queue(),
        )

        # A scheduled heartbeat renews immediately rather than sleeping through the
        # startup/model-preparation interval.
        heartbeat = asyncio.create_task(manager._heartbeat(control))
        await asyncio.wait_for(service.first_renewal.wait(), timeout=0.2)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)

        # At the production 20-second cadence, 4,320 renewal passes equal 24 hours.
        for generation in range(4320):
            if generation and generation % 240 == 0:
                control.leases = {
                    f"window-{generation // 240}": f"lease-{generation // 240}"
                }
            await manager._renew_all_leases(control)
        self.assertEqual(4321, len(service.renewals))

        # Repeated window restarts retain only live generations, never a day-long
        # list of completed Task objects and tracebacks.
        for generation in range(500):
            profile_id = f"dynamic-{generation}"
            task = manager._spawn_window_loop(
                control, profile_id, [], [], {"live_queue_enabled": False}
            )
            await task
            await asyncio.sleep(0)
            self.assertEqual([], control.worker_tasks)
            self.assertNotIn(profile_id, control.profile_worker_tasks)
            self.assertNotIn(profile_id, control.started_profile_ids)


if __name__ == "__main__":
    unittest.main()
