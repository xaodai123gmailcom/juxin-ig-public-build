from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Callable


# Test discovery is normally launched from the repository root.  Keep this file
# independently executable without requiring an editable backend installation.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from app.service import CoreService


PASSWORD = "network endurance test password"


class NoopBitBrowserClient:
    """The execution tests exercise orchestration, not the localhost adapter."""

    def __init__(self) -> None:
        self.closed_profiles: list[str] = []

    def close_profile(self, profile_id: str) -> dict[str, Any]:
        self.closed_profiles.append(profile_id)
        return {"closed": True}


class FiftyWindowPartialOutageWorker:
    """Save one result, then repeatedly lose transport on selected profiles.

    Every initial connection waits at a barrier.  That makes all fifty workers claim
    one target apiece before fast healthy workers can drain the shared queue, keeping
    the isolation assertion deterministic rather than scheduler-dependent.
    """

    expected_windows = 0
    offline_profiles: set[str] = set()
    initial_connections: set[str] = set()
    all_initially_connected: asyncio.Event | None = None
    reconnects: Counter[str] = Counter()
    assigned_targets: dict[str, str] = {}
    profile_reads: Counter[str] = Counter()
    failures_remaining: dict[str, int] = {}

    @classmethod
    def reset(
        cls,
        *,
        expected_windows: int,
        offline_profiles: set[str],
        failures_per_offline_profile: int,
    ) -> None:
        cls.expected_windows = expected_windows
        cls.offline_profiles = set(offline_profiles)
        cls.initial_connections = set()
        cls.all_initially_connected = asyncio.Event()
        cls.reconnects = Counter()
        cls.assigned_targets = {}
        cls.profile_reads = Counter()
        cls.failures_remaining = {
            profile_id: failures_per_offline_profile
            for profile_id in offline_profiles
        }

    def __init__(self, _bitbrowser: Any) -> None:
        self.profile_id = ""

    async def connect(
        self, profile_id: str, *, open_if_needed: bool = True
    ) -> None:
        del open_if_needed
        self.profile_id = profile_id
        cls = type(self)
        if profile_id in cls.initial_connections:
            cls.reconnects[profile_id] += 1
            await asyncio.sleep(0)
            return

        cls.initial_connections.add(profile_id)
        assert cls.all_initially_connected is not None
        if len(cls.initial_connections) == cls.expected_windows:
            cls.all_initially_connected.set()
        await cls.all_initially_connected.wait()

    async def disconnect(self) -> None:
        await asyncio.sleep(0)

    async def collect_followers(
        self, target: str, *, limit: int
    ) -> CollectionOutcome:
        type(self).assigned_targets[self.profile_id] = target
        await asyncio.sleep(0)
        usernames = [f"{target}.candidate{index}" for index in range(3)]
        return CollectionOutcome("followers", usernames[:limit])

    async def collect_following(
        self, target: str, *, limit: int
    ) -> CollectionOutcome:
        raise AssertionError(f"Unexpected following collection for {target}/{limit}")

    async def collect_post_likers(
        self, target: str, *, max_posts: int, per_post_limit: int
    ) -> CollectionOutcome:
        raise AssertionError(
            f"Unexpected post-liker collection for {target}/{max_posts}/{per_post_limit}"
        )

    async def read_visible_profile(
        self, username: str, *, include_activity: bool = False
    ) -> dict[str, Any]:
        del include_activity
        cls = type(self)
        cls.profile_reads[username] += 1
        if (
            self.profile_id in cls.offline_profiles
            and username.endswith(".candidate1")
            and cls.failures_remaining[self.profile_id] > 0
        ):
            cls.failures_remaining[self.profile_id] -= 1
            raise WorkerExecutionError(
                "Simulated Instagram transport outage",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        await asyncio.sleep(0)
        return {
            "username": username,
            "visibility": "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": None,
        }

    async def read_visible_account_location(self, target: str) -> str | None:
        del target
        return "美国"


class FiftyWindowAllWaitingWorker:
    expected_windows = 0
    initial_connections: set[str] = set()
    all_initially_connected: asyncio.Event | None = None

    @classmethod
    def reset(cls, *, expected_windows: int) -> None:
        cls.expected_windows = expected_windows
        cls.initial_connections = set()
        cls.all_initially_connected = asyncio.Event()

    def __init__(self, _bitbrowser: Any) -> None:
        self.profile_id = ""

    async def connect(
        self, profile_id: str, *, open_if_needed: bool = True
    ) -> None:
        del open_if_needed
        self.profile_id = profile_id
        cls = type(self)
        if profile_id not in cls.initial_connections:
            cls.initial_connections.add(profile_id)
            assert cls.all_initially_connected is not None
            if len(cls.initial_connections) == cls.expected_windows:
                cls.all_initially_connected.set()
            await cls.all_initially_connected.wait()

    async def disconnect(self) -> None:
        await asyncio.sleep(0)

    async def collect_followers(
        self, target: str, *, limit: int
    ) -> CollectionOutcome:
        del target, limit
        raise WorkerExecutionError(
            "Simulated long-duration outage",
            reason="instagram_network_unavailable",
            pause_required=True,
            status_code=503,
        )

    async def collect_following(
        self, target: str, *, limit: int
    ) -> CollectionOutcome:
        raise AssertionError(f"Unexpected following collection for {target}/{limit}")

    async def collect_post_likers(
        self, target: str, *, max_posts: int, per_post_limit: int
    ) -> CollectionOutcome:
        raise AssertionError(
            f"Unexpected post-liker collection for {target}/{max_posts}/{per_post_limit}"
        )


class NetworkEnduranceTestCase(unittest.IsolatedAsyncioTestCase):
    """Deterministic multi-window recovery regressions.

    Fifty is a stress sample, not a product window limit.  The production scheduler
    must continue to derive its worker count from the selected profiles.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "network-endurance.sqlite3")
        database.initialize()
        self.service = CoreService(database, session_hours=1)
        self.user = self.service.register_user("network-endurance", PASSWORD)
        self.managers: list[ExecutionManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            try:
                await asyncio.wait_for(manager.shutdown(), timeout=5)
            except Exception:
                # Preserve the original assertion while still allowing TemporaryDirectory
                # cleanup.  Successful tests leave no active coordinator here.
                pass
        self.temp_dir.cleanup()

    async def _eventually(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = 5,
        message: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail(message)

    async def _eventually_with_progress(
        self,
        probe: Callable[[], Any],
        *,
        inactivity_timeout: float = 45,
        hard_timeout: float = 180,
        message: str,
    ) -> None:
        """Wait for a state while failing fast only when durable work stalls."""

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_progress_at = started_at
        sentinel = object()
        last_marker: object = sentinel
        while True:
            ready, marker = await probe()
            now = loop.time()
            if ready:
                return
            if last_marker is sentinel or marker != last_marker:
                last_marker = marker
                last_progress_at = now
            if now - last_progress_at > inactivity_timeout:
                self.fail(
                    f"{message}; no durable progress for {inactivity_timeout:g}s; "
                    f"marker={marker}"
                )
            if now - started_at > hard_timeout:
                self.fail(
                    f"{message}; exceeded {hard_timeout:g}s hard guard while "
                    f"progressing; marker={marker}"
                )
            await asyncio.sleep(0.05)

    def _create_task(
        self, *, name: str, window_count: int
    ) -> dict[str, Any]:
        return self.service.create_task(
            self.user["id"],
            name=name,
            modes=["followers"],
            targets=[f"source_{index:03d}" for index in range(window_count)],
            window_ids=[f"window_{index:03d}" for index in range(window_count)],
            settings={
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 3}},
            },
        )

    async def test_fifty_windows_isolate_repeated_partial_outages_and_resume_now(
        self,
    ) -> None:
        window_count = 50
        offline_count = 10
        offline_profiles = {
            f"window_{index:03d}" for index in range(offline_count)
        }
        FiftyWindowPartialOutageWorker.reset(
            expected_windows=window_count,
            offline_profiles=offline_profiles,
            failures_per_offline_profile=2,
        )
        task = self._create_task(
            name="50-window partial outage endurance",
            window_count=window_count,
        )
        manager = ExecutionManager(
            self.service,
            NoopBitBrowserClient(),
            worker_factory=FiftyWindowPartialOutageWorker,
            # An hour-long retry proves Continue is a broadcast wake-up, not a short
            # automatic timer that happens to make the test pass.
            network_retry_delays=(3600,),
            network_retry_concurrency=4,
            network_retry_stagger_seconds=0,
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])

        async def initial_partial_outage_state() -> tuple[bool, tuple[int, int, int]]:
            diagnostics = await manager.runtime_diagnostics(
                self.user["id"], task["id"]
            )
            stored = self.service.get_task(self.user["id"], task["id"])
            completed = sum(
                target["status"] == "completed" for target in stored["targets"]
            )
            result_count = len(
                self.service.list_results(self.user["id"], task["id"])
            )
            waiting_count = diagnostics["network_waiting_window_count"]
            return (
                diagnostics["network_waiting_window_count"] == offline_count
                and completed == window_count - offline_count,
                (completed, waiting_count, result_count),
            )

        # Fifty workers persist real SQLite checkpoints here. Observe the durable
        # counters so a slow virtualized disk can finish while a true stall remains
        # bounded independently of total wall-clock throughput.
        await self._eventually_with_progress(
            initial_partial_outage_state,
            message="Healthy windows did not finish independently of offline windows",
        )

        worker = FiftyWindowPartialOutageWorker
        self.assertEqual(window_count, len(worker.initial_connections))
        self.assertEqual(window_count, len(worker.assigned_targets))

        # Forty healthy targets save three results each; ten offline targets have
        # already durably saved candidate0 before candidate1 loses transport.
        expected_durable_during_wait = (
            (window_count - offline_count) * 3 + offline_count
        )
        waiting_results = self.service.list_results(self.user["id"], task["id"])
        self.assertEqual(expected_durable_during_wait, len(waiting_results))
        for profile_id in offline_profiles:
            target = worker.assigned_targets[profile_id]
            self.assertEqual(2, worker.profile_reads[f"{target}.candidate0"])

        # First explicit Continue wakes all ten one-hour sleepers immediately.  Their
        # second deterministic transport failure must create a fresh wait, while the
        # already committed result count stays unchanged.
        await manager.resume(self.user["id"], task["id"])
        await self._eventually(
            lambda: all(
                worker.reconnects[profile_id] >= 1
                and worker.failures_remaining[profile_id] == 0
                for profile_id in offline_profiles
            ),
            timeout=30,
            message="Continue did not wake every waiting profile for the first retry",
        )
        await self._eventually(
            lambda: len(
                manager._runs[task["id"]].network_waiters
            )
            == offline_count,
            timeout=30,
            message="Repeated outages did not return every affected profile to wait",
        )
        self.assertEqual(
            expected_durable_during_wait,
            len(self.service.list_results(self.user["id"], task["id"])),
        )

        # Second Continue wakes the next one-hour wait.  All fifty targets finish from
        # their durable checkpoints; candidate0 is never reopened after recovery.
        await manager.resume(self.user["id"], task["id"])
        completion_wait = asyncio.create_task(manager.wait(task["id"]))

        async def final_completion_state() -> tuple[bool, tuple[int, int, int]]:
            stored = self.service.get_task(self.user["id"], task["id"])
            completed_count = sum(
                target["status"] == "completed" for target in stored["targets"]
            )
            result_count = len(
                self.service.list_results(self.user["id"], task["id"])
            )
            reconnect_count = sum(worker.reconnects.values())
            return (
                completion_wait.done(),
                (completed_count, result_count, reconnect_count),
            )

        try:
            await self._eventually_with_progress(
                final_completion_state,
                message="Recovered windows stopped before durable completion",
            )
            await completion_wait
        finally:
            if not completion_wait.done():
                completion_wait.cancel()
                await asyncio.gather(completion_wait, return_exceptions=True)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertTrue(
            all(target["status"] == "completed" for target in completed["targets"])
        )
        self.assertEqual(
            window_count * 3,
            len(self.service.list_results(self.user["id"], task["id"])),
        )
        for profile_id in offline_profiles:
            target = worker.assigned_targets[profile_id]
            self.assertEqual(2, worker.reconnects[profile_id])
            self.assertEqual(2, worker.profile_reads[f"{target}.candidate0"])
            self.assertEqual(4, worker.profile_reads[f"{target}.candidate1"])
            self.assertEqual(2, worker.profile_reads[f"{target}.candidate2"])

    async def test_stop_wins_over_fifty_waiting_worker_teardown(self) -> None:
        window_count = 50
        FiftyWindowAllWaitingWorker.reset(expected_windows=window_count)
        task = self._create_task(
            name="50-window stop versus recovery teardown",
            window_count=window_count,
        )
        manager = ExecutionManager(
            self.service,
            NoopBitBrowserClient(),
            worker_factory=FiftyWindowAllWaitingWorker,
            network_retry_delays=(3600,),
            network_retry_concurrency=4,
            network_retry_stagger_seconds=0,
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])

        # Every worker writes a real SQLite checkpoint before it enters the
        # durable network wait.  Fifty serialized writes can exceed eight
        # seconds on shared Windows runners; this is a deadlock sentinel, not
        # a throughput SLA, so match the stress-test headroom used above.
        await self._eventually(
            lambda: (
                task["id"] in manager._runs
                and len(manager._runs[task["id"]].network_waiters) == window_count
            ),
            timeout=60,
            message="Not every selected window reached the durable network wait",
        )

        stopped = await asyncio.wait_for(
            manager.stop(self.user["id"], task["id"], close_windows=False),
            timeout=8,
        )
        self.assertEqual("stopped", stopped["status"])

        # Give any coordinator-finally writes a chance to run.  STOPPED is the user's
        # terminal override and may never fall back to RECOVERABLE/WAITING_NETWORK.
        await asyncio.sleep(0.05)
        durable = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", durable["status"])
        self.assertNotIn(task["id"], manager.active_task_ids())
        self.assertTrue(
            all(
                target["status"] not in {"running", "waiting_network"}
                for target in durable["targets"]
            )
        )
        diagnostics = await manager.runtime_diagnostics(
            self.user["id"], task["id"]
        )
        self.assertFalse(diagnostics["active"])
        self.assertEqual("stopped", diagnostics["status"])
        self.assertEqual(0, diagnostics["network_waiting_window_count"])


if __name__ == "__main__":
    unittest.main()
