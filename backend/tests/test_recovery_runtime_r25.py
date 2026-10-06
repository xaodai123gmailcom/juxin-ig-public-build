"""Automatic collection recovery with real SQLite and controlled browser failures."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from completion_wait import wait_for_collection_completion
from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from app.service import CoreService


class Browser:
    def close_profile(self, profile_id):
        pass


class Worker:
    def __init__(self, _browser):
        self.profile_id = None
        self.attempts = 0
        self.attempt_times = []
        self.reason = "instagram_followers_list_incomplete"
        self.failures = 3
        self.replacements = []
        self.connects = 0
        self.disconnect_started = asyncio.Event()
        self.allow_disconnect = asyncio.Event()
        self.allow_disconnect.set()

    async def connect(self, profile_id, **_kwargs):
        self.profile_id = profile_id
        self.connects += 1

    async def disconnect(self):
        self.disconnect_started.set()
        await self.allow_disconnect.wait()

    async def connection_healthy(self):
        return True

    def request_page_replacement(self, target, reason):
        self.replacements.append((target, reason))

    def prepare_page_retry(self, target):
        pass

    async def collect_followers(self, target, *, limit):
        self.attempts += 1
        self.attempt_times.append(asyncio.get_running_loop().time())
        if target != "healthy" and self.attempts <= self.failures:
            error = WorkerExecutionError("temporarily stalled", reason=self.reason)
            if self.reason == "instagram_page_recovery_exhausted":
                error.details.update(original_reason="browser_window_surface_unstable", auto_retry=False)
            raise error
        return CollectionOutcome("followers", [target + "_fan"], source_total=1)

    async def read_visible_profile(self, target, **_kwargs):
        return dict(username=target, visibility="public", followers=10, following=10, posts=2)

    async def read_visible_account_location(self, target):
        return "美国"


class StreamingRecoveryWorker(Worker):
    supports_candidate_batch_sink = True

    def __init__(self, browser):
        super().__init__(browser)
        self.attempt_started = asyncio.Event()
        self.allow_progress = asyncio.Event()
        self.progress_written = asyncio.Event()
        self.allow_completion = asyncio.Event()

    async def collect_followers(self, target, *, limit, candidate_sink, initial_candidate_count):
        self.attempts += 1
        if self.attempts == 1:
            raise WorkerExecutionError("loading shell", reason="instagram_profile_not_ready")
        self.attempt_started.set()
        await self.allow_progress.wait()
        await candidate_sink(["stream_fan"])
        self.progress_written.set()
        await self.allow_completion.wait()
        return CollectionOutcome("followers", [], source_total=1)


class RecoveryRuntimeR25Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "recovery.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("recovery-r25", "long enough test password")["id"]
        self.workers = []
        self.managers = []

    async def asyncTearDown(self):
        for worker in self.workers:
            worker.allow_disconnect.set()
        for manager in self.managers:
            await manager.shutdown()
        self.temp.cleanup()

    def fixture(self, reason="instagram_followers_list_incomplete", failures=3, cooldown=.04, windows=None, targets=None,
                worker_type=Worker):
        def factory(browser):
            worker = worker_type(browser)
            worker.reason, worker.failures = reason, failures
            self.workers.append(worker)
            return worker
        manager = ExecutionManager(self.service, Browser(), worker_factory=factory,
            network_retry_delays=(.001,), network_retry_stagger_seconds=0,
            relationship_no_progress_retry_limit=1, surface_no_progress_retry_limit=1)
        # Assignment also lets the regression run against the old constructor.
        manager.recovery_cooldown_seconds = cooldown
        self.managers.append(manager)
        task = self.service.create_task(self.owner, name="automatic recovery", modes=["followers"],
            targets=targets or ["stalled"], window_ids=windows or ["window"],
            settings={"local_person_recognition": False, "location_enabled": False})
        return manager, task

    async def until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.002)

    async def waiting(self, manager, task, reason=None):
        control = manager._runs[task["id"]]
        await self.until(lambda: any(reason is None or w["reason"] == reason for w in control.network_waiters.values()))
        return control

    def hold_retry(self, manager):
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def held(control, profile_id, generation, delay):
            observed.append(delay)
            entered.set()
            await release.wait()
            return not control.stop_event.is_set()

        manager._wait_retry_delay = held
        return entered, release, observed

    def test_exhausted_transient_recovery_is_automatic_but_unknown_and_account_guards_are_manual(self):
        for reason in ("browser_window_surface_unstable", "instagram_profile_dom_unrecognized",
                       "instagram_profile_not_ready", "instagram_location_load_failed", "instagram_network_unavailable"):
            with self.subTest(reason=reason):
                error = WorkerExecutionError("new page exhausted", reason="instagram_page_recovery_exhausted")
                error.details.update(original_reason=reason, auto_retry=False)
                self.assertFalse(ExecutionManager._is_profile_intervention_error(error))
                self.assertTrue(ExecutionManager._is_instagram_surface_retry_error(error))
        for reason in ("instagram_login_required", "instagram_challenge", "instagram_rate_limited",
                       "instagram_action_blocked", "unknown"):
            with self.subTest(reason=reason):
                error = WorkerExecutionError("manual guard", reason="instagram_page_recovery_exhausted")
                error.details.update(original_reason=reason, auto_retry=True)
                self.assertTrue(ExecutionManager._is_profile_intervention_error(error))
                self.assertFalse(ExecutionManager._is_instagram_surface_retry_error(error))

    async def test_list_retry_exhaustion_recovers_same_target_without_new_owner_or_duplicates(self):
        manager, task = self.fixture()
        await manager.start(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        stored = self.service.get_task(self.owner, task["id"])
        self.assertEqual("completed", stored["targets"][0]["status"])
        self.assertEqual(task["targets"][0]["id"], stored["targets"][0]["id"])
        self.assertEqual(4, self.workers[0].attempts)
        self.assertEqual(1, len(self.workers))
        self.assertEqual(1, self.workers[0].connects)
        self.assertEqual(1, len(self.service.list_results(self.owner, task["id"])))

    async def test_transient_hover_failure_reopens_same_list_without_manual_retry(self):
        manager, task = self.fixture("instagram_hover_card_unavailable", failures=2)
        await manager.start(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["targets"][0]["status"])
        self.assertEqual(3, self.workers[0].attempts)
        self.assertEqual(1, self.workers[0].connects)
        self.assertEqual(2, len(self.workers[0].replacements))
        self.assertEqual(1, len(self.service.list_results(self.owner, task["id"])))

    async def test_surface_retry_exhaustion_recovers_automatically(self):
        manager, task = self.fixture("instagram_profile_temporarily_unavailable")
        await manager.start(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["targets"][0]["status"])
        self.assertEqual(4, self.workers[0].attempts)

    async def test_exhaustion_keeps_each_later_attempt_spaced_until_real_progress(self):
        manager, task = self.fixture(failures=4, cooldown=.05)
        await manager.start(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        times = self.workers[0].attempt_times
        self.assertEqual(5, len(times))
        for before, after in zip(times[1:], times[2:]):
            self.assertGreaterEqual(after - before, .045)

    async def test_new_page_failure_has_automatic_deadline_and_persists_original_cause(self):
        manager, task = self.fixture("instagram_page_recovery_exhausted", failures=1, cooldown=.15)
        entered, release, observed = self.hold_retry(manager)
        await manager.start(self.owner, task["id"])
        try:
            await asyncio.wait_for(entered.wait(), 5)
            waiter = manager._runs[task["id"]].network_waiters["window"]
            self.assertEqual("waiting_network", waiter["state"])
            self.assertIsNotNone(waiter["next_retry_at"])
            # R93 uses the short transport backoff for replacement exhaustion;
            # the separate list-stall cooldown must not force a longer wait.
            self.assertEqual(observed[0], .001)
            checkpoint = self.service.get_checkpoint(self.owner, task["id"], task["targets"][0]["id"], "followers")
            self.assertEqual("browser_window_surface_unstable", checkpoint["counters"].get("original_reason"))
        finally:
            release.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual(2, self.workers[0].attempts)

    async def test_cooldown_pause_blocks_timer_and_retry_now_until_resume(self):
        manager, task = self.fixture(failures=2, cooldown=.08)
        await manager.start(self.owner, task["id"])
        await self.waiting(manager, task, "instagram_relationship_list_no_progress")
        await manager.pause(self.owner, task["id"])
        await manager.retry_network_now(self.owner, task["id"])
        await asyncio.sleep(.12)
        self.assertEqual(2, self.workers[0].attempts)
        await manager.resume(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual(3, self.workers[0].attempts)

    async def test_stop_during_cooldown_never_restarts_and_keeps_lock_through_cleanup(self):
        manager, task = self.fixture(failures=99, cooldown=.08)
        await manager.start(self.owner, task["id"])
        await self.waiting(manager, task, "instagram_relationship_list_no_progress")
        worker = self.workers[0]
        worker.allow_disconnect.clear()
        stopping = asyncio.create_task(manager.stop(self.owner, task["id"], close_windows=False))
        try:
            await asyncio.wait_for(worker.disconnect_started.wait(), 5)
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, "window", operation_type="action", entity_id="other")
            await asyncio.sleep(.12)
            self.assertEqual(2, worker.attempts)
            self.assertFalse(stopping.done())
        finally:
            worker.allow_disconnect.set()
            await asyncio.wait_for(stopping, 5)
        self.assertEqual("stopped", self.service.get_task(self.owner, task["id"])["status"])
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_one_cooling_window_does_not_stop_healthy_sibling(self):
        class IndependentWorker(Worker):
            async def collect_followers(self, target, *, limit):
                if target == "healthy":
                    # Require this window to finish while its sibling is cooling,
                    # regardless of how long SQLite commits take on this machine.
                    await entered.wait()
                return await super().collect_followers(target, limit=limit)

        manager, task = self.fixture(failures=2, cooldown=.1, windows=["one", "two"],
            targets=["stalled", "healthy"], worker_type=IndependentWorker)
        entered, release, observed = self.hold_retry(manager)
        await manager.start(self.owner, task["id"])
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await self.until(lambda: any(t["username"] == "healthy" and t["status"] == "completed"
                for t in self.service.get_task(self.owner, task["id"])["targets"]))
            stored = self.service.get_task(self.owner, task["id"])
            stalled = next(t for t in stored["targets"] if t["username"] == "stalled")
            self.assertEqual("waiting_network", stalled["status"])
            self.assertGreaterEqual(observed[0], .1)
            self.assertEqual(2, len(self.workers))
            self.assertEqual(1, len(self.service.list_results(self.owner, task["id"])))
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, stalled["current_window_id"],
                    operation_type="action", entity_id="must-not-steal-cooling-window")
        finally:
            release.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        stored = self.service.get_task(self.owner, task["id"])
        self.assertTrue(all(t["status"] == "completed" for t in stored["targets"]))
        self.assertEqual(2, len(self.service.list_results(self.owner, task["id"])))
        self.assertEqual(2, len(self.workers))
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_expired_cooldown_reports_running_probe_while_healthy_sibling_completes(self):
        probe_entered, finish_probe = asyncio.Event(), asyncio.Event()

        class SlowSiblingWorker(Worker):
            async def collect_followers(self, target, *, limit):
                if target == "healthy":
                    # Simulate slower healthy work: it completes after the real
                    # cooldown has expired, not before a 100 ms wall-clock race.
                    await probe_entered.wait()
                elif self.attempts >= 2:
                    probe_entered.set()
                    await finish_probe.wait()
                return await super().collect_followers(target, limit=limit)

        manager, task = self.fixture(failures=2, cooldown=.1, windows=["one", "two"],
            targets=["stalled", "healthy"], worker_type=SlowSiblingWorker)
        await manager.start(self.owner, task["id"])
        try:
            await asyncio.wait_for(probe_entered.wait(), 5)
            await self.until(lambda: any(t["username"] == "healthy" and t["status"] == "completed"
                for t in self.service.get_task(self.owner, task["id"])["targets"]))
            stored = self.service.get_task(self.owner, task["id"])
            stalled = next(t for t in stored["targets"] if t["username"] == "stalled")
            self.assertEqual("running", stalled["status"])
            diagnostic = await manager.runtime_diagnostics(self.owner, task["id"])
            profile = next(p for p in diagnostic["profile_states"] if p["profile_id"] == stalled["current_window_id"])
            self.assertTrue(profile["recovery_in_progress"])
            self.assertFalse(profile["progress_confirmed"])
            self.assertEqual("recovering_page", profile["current_stage"])
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, stalled["current_window_id"],
                    operation_type="action", entity_id="must-not-steal-recovering-window")
        finally:
            finish_probe.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        stored = self.service.get_task(self.owner, task["id"])
        self.assertTrue(all(t["status"] == "completed" for t in stored["targets"]))
        self.assertEqual(2, len(self.service.list_results(self.owner, task["id"])))
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_resumed_stream_reports_actual_work_before_whole_mode_finishes(self):
        manager, task = self.fixture(worker_type=StreamingRecoveryWorker)
        await manager.start(self.owner, task["id"])
        await self.until(lambda: bool(self.workers) and self.workers[0].attempt_started.is_set())
        worker = self.workers[0]
        diagnostic = await manager.runtime_diagnostics(self.owner, task["id"])
        profile = diagnostic["profile_states"][0]
        self.assertEqual("running", diagnostic["status"])
        self.assertFalse(diagnostic["waiting_for_network"])
        self.assertEqual([], diagnostic["network_waiters"])
        self.assertEqual(0, diagnostic["network_waiting_window_count"])
        self.assertEqual("working", profile["state"])
        self.assertEqual("recovering_page", profile["current_stage"])
        self.assertTrue(profile["recovery_in_progress"])
        self.assertFalse(profile["progress_confirmed"])
        self.assertIsNotNone(profile["reason"])
        self.assertIsNone(profile["next_retry_at"])
        worker.allow_progress.set()
        await asyncio.wait_for(worker.progress_written.wait(), 5)
        diagnostic = await manager.runtime_diagnostics(self.owner, task["id"])
        profile = diagnostic["profile_states"][0]
        self.assertEqual("running", diagnostic["status"])
        self.assertTrue(profile["progress_confirmed"])
        self.assertIsNone(profile["reason"])
        self.assertIsNone(profile["message"])
        self.assertEqual("collecting_list", profile["current_stage"])
        checkpoint = self.service.get_checkpoint(self.owner, task["id"], task["targets"][0]["id"], "followers")
        self.assertNotEqual("mode_completed", checkpoint["stage"])
        self.assertEqual(1, checkpoint["counters"]["discovered"])
        worker.allow_completion.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])

    async def test_resume_preserves_remaining_transport_cooldown_before_reading_page(self):
        manager, task = self.fixture(failures=0)
        entered, release, observed = self.hold_retry(manager)
        target_id = task["targets"][0]["id"]
        self.service.upsert_checkpoint(self.owner, task["id"], target_id, mode="followers", stage="waiting_network",
            cursor={"resume_stage": "discovering_accounts", "resume_cursor": {"resume_tail": ["kept_tail"]}},
            counters={"reason": "TimeoutError",
                "wait_kind": "network", "retry_delay_seconds": 30,
                "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                "surface_retry_count": 2, "previous_counters": {"discovered": 0, "processed": 0}}, recoverable=True)
        await manager.start(self.owner, task["id"])
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(0, self.workers[0].attempts)
            self.assertGreater(observed[0], 0)
            self.assertLessEqual(observed[0], 30)
        finally:
            release.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual(1, self.workers[0].attempts)

    async def test_core_recovery_then_explicit_resume_honors_saved_cooldown(self):
        manager, task = self.fixture(failures=0)
        entered, release, observed = self.hold_retry(manager)
        target_id = task["targets"][0]["id"]
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.service.set_target_runtime_status(self.owner, task["id"], target_id, "waiting_network", window_id="window")
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        self.service.upsert_checkpoint(self.owner, task["id"], target_id, mode="followers", stage="waiting_network",
            cursor={"resume_stage": "discovering_accounts", "resume_cursor": {"resume_tail": ["kept_tail"]}},
            counters={"reason": "instagram_surface_no_progress", "original_reason": "instagram_profile_not_ready",
                "wait_kind": "instagram_page", "retry_delay_seconds": 30, "next_retry_at": deadline,
                "surface_retry_count": 2, "retry_count": 7,
                "previous_counters": {"discovered": 0, "processed": 0}}, recoverable=True)
        self.service.recover_interrupted_operations()
        recovered = self.service.get_checkpoint(self.owner, task["id"], target_id, "followers")
        self.assertEqual("interrupted_recoverable", recovered["stage"])
        self.assertEqual(deadline, recovered["counters"]["next_retry_at"])
        await manager.retry_target(self.owner, task["id"], target_id)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(0, self.workers[0].attempts)
            self.assertGreater(observed[0], 0)
            self.assertLessEqual(observed[0], 30)
            diagnostic = await manager.runtime_diagnostics(self.owner, task["id"])
            self.assertEqual(7, diagnostic["profile_states"][0]["retry_count"])
        finally:
            release.set()
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["targets"][0]["status"])

    def test_cooldown_configuration_rejects_zero_negative_and_nonfinite_delays(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ExecutionManager(self.service, Browser(), recovery_cooldown_seconds=value)


if __name__ == "__main__":
    unittest.main()
