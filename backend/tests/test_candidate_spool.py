from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.errors import ValidationError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.service import CoreService


PASSWORD = "candidate spool test password"


class CandidateSpoolServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "spool.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.user = self.service.register_user("candidate-spool", PASSWORD)
        self.task = self.service.create_task(
            self.user["id"],
            name="candidate spool",
            modes=["followers"],
            targets=["source_account"],
            settings={"location_enabled": False},
        )
        self.target_id = self.task["targets"][0]["id"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_batch_is_bounded_idempotent_ordered_and_cascades(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.append_task_mode_candidates(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
                [f"candidate_{index:03d}" for index in range(101)],
            )

        first = self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["First.User", "second_user", "FIRST.USER"],
        )
        self.assertEqual(2, first["added"])
        self.assertEqual(2, first["total"])
        repeated = self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["second_user", "third_user"],
        )
        self.assertEqual(1, repeated["added"])
        pending = self.service.list_pending_task_mode_candidates(
            self.user["id"], self.task["id"], self.target_id, "followers"
        )
        self.assertEqual(
            ["First.User", "second_user", "third_user"],
            [item["username"] for item in pending],
        )
        self.assertEqual([1, 2, 3], [item["discovery_order"] for item in pending])

        capped = self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["fourth_user", "fifth_user"],
            max_total=4,
        )
        self.assertEqual(1, capped["added"])
        self.assertEqual(4, capped["total"])

        self.service.finish_task_mode_candidate(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            "first.user",
            state="recorded",
        )
        stats = self.service.task_mode_candidate_stats(
            self.user["id"], self.task["id"], self.target_id, "followers"
        )
        self.assertEqual(
            {"total": 4, "pending": 3, "recorded": 1, "deduped": 0},
            stats,
        )

        with self.database.write() as connection:
            connection.execute("DELETE FROM task_targets WHERE id=?", (self.target_id,))
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_mode_candidates"
                ).fetchone()[0],
            )

    def test_startup_checkpoint_wrapper_keeps_completed_discovery(self) -> None:
        checkpoint = {
            "stage": "interrupted_recoverable",
            "cursor": {
                "resume_stage": "screening_accounts",
                "resume_cursor": {
                    "candidate_spool_version": 1,
                    "candidate_spool_complete": True,
                },
            },
        }
        self.assertTrue(ExecutionManager._candidate_spool_complete(checkpoint))
        # Old finite-limit releases could mark a relationship mode complete merely
        # because their numeric ceiling was reached. Recovery must reopen it.
        self.assertFalse(
            ExecutionManager._candidate_spool_complete(
                checkpoint, require_natural_end=True
            )
        )
        checkpoint["cursor"]["resume_cursor"][
            "candidate_spool_natural_end"
        ] = True
        self.assertTrue(
            ExecutionManager._candidate_spool_complete(
                checkpoint, require_natural_end=True
            )
        )

    def test_result_commit_gap_is_reconciled_without_profile_reopen(self) -> None:
        self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["already_saved"],
        )
        self.service.record_result(
            self.user["id"],
            self.task["id"],
            self.target_id,
            username="already_saved",
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile={"username": "already_saved"},
            screening={},
            qualified=True,
        )
        # Simulate a process exit before finish_task_mode_candidate().
        stats = self.service.reconcile_task_mode_candidates(
            self.user["id"], self.task["id"], self.target_id, "followers"
        )
        self.assertEqual(0, stats["pending"])
        self.assertEqual(1, stats["recorded"])

    def test_existing_database_adds_candidate_spool_non_destructively(self) -> None:
        with self.database.write() as connection:
            connection.execute("DROP TABLE task_mode_candidates")
            connection.execute("DELETE FROM schema_migrations WHERE version=8")
        self.database.initialize()
        self.assertEqual(
            self.task["id"], self.service.get_task(self.user["id"], self.task["id"])["id"]
        )
        added = self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["after_upgrade"],
        )
        self.assertEqual(1, added["total"])
        with self.database.read() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=8"
                ).fetchone()
            )


class _NoopBitBrowser:
    def close_profile(self, _profile_id: str) -> dict[str, Any]:
        return {"closed": True}


class _ResumeWithoutRediscoveryWorker:
    collect_calls = 0
    profile_reads: Counter[str] = Counter()
    failures_remaining = 1

    @classmethod
    def reset(cls) -> None:
        cls.collect_calls = 0
        cls.profile_reads = Counter()
        cls.failures_remaining = 1

    def __init__(self, _bitbrowser: Any) -> None:
        pass

    async def connect(self, _profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed

    async def disconnect(self) -> None:
        pass

    async def collect_followers(self, _target: str, *, limit: int) -> CollectionOutcome:
        type(self).collect_calls += 1
        return CollectionOutcome(
            "followers", ["candidate_a", "candidate_b", "candidate_c"][:limit]
        )

    async def collect_following(self, target: str, *, limit: int) -> CollectionOutcome:
        raise AssertionError((target, limit))

    async def collect_post_likers(
        self, target: str, *, max_posts: int, per_post_limit: int
    ) -> CollectionOutcome:
        raise AssertionError((target, max_posts, per_post_limit))

    async def read_visible_profile(
        self, username: str, *, include_activity: bool = False
    ) -> dict[str, Any]:
        del include_activity
        type(self).profile_reads[username] += 1
        if username == "candidate_b" and type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise RuntimeError("simulated process failure after one durable result")
        return {
            "username": username,
            "visibility": "public",
            "followers": 10,
            "following": 10,
            "posts": 1,
            "activity_days": None,
        }

    async def read_visible_account_location(self, _target: str) -> str | None:
        return "美国"


class _BatchNetworkWorker(_ResumeWithoutRediscoveryWorker):
    supports_candidate_batch_sink = True
    collect_calls = 0
    initial_counts: list[int] = []
    profile_reads: Counter[str] = Counter()

    @classmethod
    def reset(cls) -> None:
        cls.collect_calls = 0
        cls.initial_counts = []
        cls.profile_reads = Counter()

    async def collect_followers(
        self,
        _target: str,
        *,
        limit: int,
        candidate_sink: Any,
        initial_candidate_count: int,
    ) -> CollectionOutcome:
        cls = type(self)
        cls.collect_calls += 1
        cls.initial_counts.append(initial_candidate_count)
        usernames = [
            f"durable_{index:03d}"
            for index in range(min(limit if limit is not None else 237, 237))
        ]
        if cls.collect_calls == 1:
            await candidate_sink(usernames[:100])
            await candidate_sink(usernames[100:137])
            raise WorkerExecutionError(
                "simulated network loss during visible-list scroll",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        await candidate_sink(usernames[initial_candidate_count:])
        return CollectionOutcome(
            "followers", [], candidate_count=len(usernames)
        )


class _ScreenNetworkCheckpointWorker(_ResumeWithoutRediscoveryWorker):
    collect_calls = 0
    network_available: asyncio.Event | None = None

    @classmethod
    def reset(cls) -> None:
        cls.collect_calls = 0
        cls.profile_reads = Counter()
        cls.network_available = asyncio.Event()

    async def collect_followers(self, _target: str, *, limit: int) -> CollectionOutcome:
        type(self).collect_calls += 1
        return CollectionOutcome("followers", ["screen_wait"][:limit])

    async def read_visible_profile(
        self, username: str, *, include_activity: bool = False
    ) -> dict[str, Any]:
        del include_activity
        type(self).profile_reads[username] += 1
        assert type(self).network_available is not None
        if not type(self).network_available.is_set():
            raise WorkerExecutionError(
                "network lost after source discovery",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        return {
            "username": username,
            "visibility": "public",
            "followers": 10,
            "following": 10,
            "posts": 1,
            "activity_days": None,
        }


class CandidateSpoolExecutionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "execution.sqlite3")
        database.initialize()
        self.service = CoreService(database, session_hours=1)
        self.user = self.service.register_user("candidate-execution", PASSWORD)
        self.managers: list[ExecutionManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.shutdown()
        self.temp_dir.cleanup()

    def _task(self, *, limit: int = 3) -> dict[str, Any]:
        return self.service.create_task(
            self.user["id"],
            name="candidate execution",
            modes=["followers"],
            targets=["source_account"],
            window_ids=["window-1"],
            settings={
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": limit}},
            },
        )

    async def _wait_for_progressing_candidate_task(
        self,
        manager: ExecutionManager,
        task: dict[str, Any],
        *,
        inactivity_timeout: float = 45.0,
        hard_timeout: float = 180.0,
    ) -> None:
        """Bound deadlocks without treating steady SQLite progress as a timeout."""

        wait_task = asyncio.create_task(manager.wait(task["id"]))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_progress_at = started_at
        last_marker: tuple[int, int] | None = None
        target_id = task["targets"][0]["id"]
        try:
            while not wait_task.done():
                stats = await asyncio.to_thread(
                    self.service.task_mode_candidate_stats,
                    self.user["id"],
                    task["id"],
                    target_id,
                    "followers",
                )
                marker = (
                    int(stats["total"]),
                    int(stats["recorded"]) + int(stats["deduped"]),
                )
                now = loop.time()
                if marker != last_marker:
                    last_marker = marker
                    last_progress_at = now
                if now - last_progress_at > inactivity_timeout:
                    self.fail(
                        "candidate spool stopped making durable progress: "
                        f"marker={marker}, stats={stats}"
                    )
                if now - started_at > hard_timeout:
                    self.fail(
                        "candidate spool exceeded the hard completion guard while "
                        f"still progressing: marker={marker}, stats={stats}"
                    )
                await asyncio.wait({wait_task}, timeout=0.25)
            await wait_task
        finally:
            if not wait_task.done():
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)

    async def test_resume_does_not_reopen_processed_profiles_or_source_list(self) -> None:
        _ResumeWithoutRediscoveryWorker.reset()
        task = self._task()
        manager = ExecutionManager(
            self.service,
            _NoopBitBrowser(),
            worker_factory=_ResumeWithoutRediscoveryWorker,
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])
        # A full debug-mode suite can briefly stall the shared executor on a
        # busy CI host. Keep this bounded without making scheduler latency part
        # of the durable-resume contract under test.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        interrupted = self.service.get_task(self.user["id"], task["id"])
        # An unclassified failure in one BitBrowser window is recoverable and must
        # not escalate into a task-wide terminal failure. The durable spool remains
        # the source of truth for the explicit resume below.
        self.assertEqual("recoverable", interrupted["status"])
        stats = self.service.task_mode_candidate_stats(
            self.user["id"], task["id"], task["targets"][0]["id"], "followers"
        )
        self.assertEqual(
            {"total": 3, "pending": 2, "recorded": 1, "deduped": 0}, stats
        )

        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])
        await manager.resume(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        self.assertEqual(
            "completed", self.service.get_task(self.user["id"], task["id"])["status"]
        )
        self.assertEqual(1, _ResumeWithoutRediscoveryWorker.collect_calls)
        # Each retained public account has one base-profile read plus one required
        # latest-post activity read. Candidate A is still not reopened on resume;
        # candidate B adds its one pre-failure attempt before the successful pair.
        self.assertEqual(2, _ResumeWithoutRediscoveryWorker.profile_reads["candidate_a"])
        self.assertEqual(3, _ResumeWithoutRediscoveryWorker.profile_reads["candidate_b"])
        self.assertEqual(2, _ResumeWithoutRediscoveryWorker.profile_reads["candidate_c"])

    async def test_network_loss_keeps_each_flushed_batch_and_resumes_from_count(self) -> None:
        _BatchNetworkWorker.reset()
        task = self._task(limit=237)
        manager = ExecutionManager(
            self.service,
            _NoopBitBrowser(),
            worker_factory=_BatchNetworkWorker,
            network_retry_delays=(0,),
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])
        # This intentionally persists 237 profiles across two durable batches.
        # Python 3.14 debug asyncio plus Windows antivirus can make each SQLite
        # handoff much slower than the aggregate wall-clock budget. Guard actual
        # lack of durable progress while allowing a steadily advancing run to finish.
        await self._wait_for_progressing_candidate_task(manager, task)
        self.assertEqual(
            "completed", self.service.get_task(self.user["id"], task["id"])["status"]
        )
        self.assertEqual([0, 137], _BatchNetworkWorker.initial_counts)
        self.assertEqual(2, _BatchNetworkWorker.collect_calls)
        self.assertEqual(237, len(_BatchNetworkWorker.profile_reads))
        self.assertTrue(all(value == 2 for value in _BatchNetworkWorker.profile_reads.values()))

    async def test_network_checkpoint_preserves_completed_source_spool(self) -> None:
        _ScreenNetworkCheckpointWorker.reset()
        task = self._task(limit=1)
        manager = ExecutionManager(
            self.service,
            _NoopBitBrowser(),
            worker_factory=_ScreenNetworkCheckpointWorker,
            network_retry_delays=(60,),
        )
        self.managers.append(manager)
        await manager.start(self.user["id"], task["id"])
        for _ in range(200):
            if self.service.get_task(self.user["id"], task["id"])["status"] == "waiting_network":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            "waiting_network", self.service.get_task(self.user["id"], task["id"])["status"]
        )
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], task["targets"][0]["id"], "followers"
        )
        self.assertEqual("waiting_network", checkpoint["stage"])
        self.assertTrue(
            checkpoint["cursor"]["resume_cursor"]["candidate_spool_complete"]
        )

        assert _ScreenNetworkCheckpointWorker.network_available is not None
        _ScreenNetworkCheckpointWorker.network_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=3)
        self.assertEqual(1, _ScreenNetworkCheckpointWorker.collect_calls)
        self.assertEqual(
            "completed", self.service.get_task(self.user["id"], task["id"])["status"]
        )


class _FakeLink:
    def __init__(self, username: str) -> None:
        self.username = username

    async def get_attribute(self, name: str) -> str | None:
        return f"/{self.username}/" if name == "href" else None


class _FakeLinks:
    def __init__(self, dialog: "_FakeDialog") -> None:
        self.dialog = dialog

    async def count(self) -> int:
        return len(self.dialog.current)

    def nth(self, index: int) -> _FakeLink:
        return _FakeLink(self.dialog.current[index])


class _FakeDialog:
    def __init__(self, snapshots: list[list[str]]) -> None:
        self.snapshots = snapshots
        self.index = 0

    @property
    def current(self) -> list[str]:
        return self.snapshots[min(self.index, len(self.snapshots) - 1)]

    def locator(self, _selector: str) -> _FakeLinks:
        return _FakeLinks(self)

    async def evaluate(self, expression: str) -> None:
        # DOM projection/measurement is read-only. Advance snapshots only when
        # production actually scrolls, including its fallback href reader.
        if "relation-action: advance" in expression:
            self.index = min(self.index + 1, len(self.snapshots) - 1)
        elif "relation-action: reset" in expression:
            self.index = 0

    async def inner_text(self, timeout: int) -> str:
        del timeout
        return "Followers"


class _DialogWorker(PlaywrightWorker):
    async def _guard(self) -> None:
        return None

    async def _has_visible_loading_indicator(self) -> bool:
        return False

    async def _page_surface_failure(self, **_kwargs: Any) -> str | None:
        return None


class _RelationDialogWorker(_DialogWorker):
    def __init__(self, visible_count: int, dialog: _FakeDialog) -> None:
        super().__init__(_NoopBitBrowser())
        self.visible_count = visible_count
        self.dialog = dialog
        # These snapshots are already settled. Delayed batches use a logical clock
        # in test_collection_stability; do not spend twenty wall seconds per fixture.
        self.collection_loading_grace_seconds = .01

    async def _navigate_profile(self, username: str) -> str:
        return username

    async def _visible_relation_count(
        self, username_norm: str, relation: str
    ) -> int:
        del username_norm, relation
        return self.visible_count

    async def _open_relation_surface(
        self, username_norm: str, relation: str
    ) -> _FakeDialog:
        del username_norm, relation
        return self.dialog


class CandidateSpoolDialogTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_relation_progress_reports_visible_source_total_and_batches(
        self,
    ) -> None:
        worker = _RelationDialogWorker(
            10, _FakeDialog([[f"account_{index:03d}" for index in range(10)]])
        )
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 2
        stored: set[str] = set()
        progress: list[dict[str, Any]] = []

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        async def progress_sink(update: dict[str, Any]) -> None:
            progress.append(dict(update))

        outcome = await worker.collect_followers(
            "counted_source",
            limit=10,
            candidate_sink=sink,
            progress_sink=progress_sink,
        )

        self.assertEqual(10, outcome.source_total)
        self.assertEqual(10, outcome.candidate_count)
        self.assertEqual(
            {
                "mode": "followers",
                "source_total": 10,
                "discovered_count": 0,
            },
            progress[0],
        )
        self.assertEqual(10, progress[-1]["discovered_count"])
        self.assertTrue(
            all(update["source_total"] == 10 for update in progress)
        )

    async def test_rendered_zero_relation_total_is_reported_not_unknown(self) -> None:
        worker = _RelationDialogWorker(0, _FakeDialog([[]]))
        progress: list[dict[str, Any]] = []

        async def progress_sink(update: dict[str, Any]) -> None:
            progress.append(dict(update))

        outcome = await worker.collect_following(
            "empty_source", limit=10, progress_sink=progress_sink
        )

        self.assertEqual(0, outcome.source_total)
        self.assertEqual(
            [
                {
                    "mode": "following",
                    "source_total": 0,
                    "discovered_count": 0,
                }
            ],
            progress,
        )

    def test_post_liker_outcome_keeps_unknown_source_total(self) -> None:
        outcome = CollectionOutcome(mode="post_likers", usernames=[])
        self.assertIsNone(outcome.source_total)

    async def test_stalled_relation_below_visible_count_is_recoverable_not_complete(
        self,
    ) -> None:
        worker = _RelationDialogWorker(
            100, _FakeDialog([[f"account_{index:03d}" for index in range(10)]])
        )
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 2
        stored: set[str] = set()

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        with self.assertRaises(WorkerExecutionError) as context:
            await worker._collect_relation_once(
                "stalled_source", relation="followers", limit=100, candidate_sink=sink
            )
        # The scan emits an incomplete-list reason. The public API's one fresh-page
        # attempt/manual gate is covered separately by test_profile_stall_recovery.
        self.assertEqual("instagram_followers_list_incomplete", context.exception.code)
        self.assertTrue(context.exception.details["pause_required"])
        self.assertEqual(10, len(stored))

    async def test_small_live_count_drift_can_finish_after_settled_confirmation(
        self,
    ) -> None:
        # A changed header total does not override a genuinely settled physical
        # tail. Supply explicit bottom/no-loader evidence instead of treating a
        # percentage of the displayed total as proof of completion.
        class NoLoaders:
            async def count(self) -> int:
                return 0

        class SettledDialog(_FakeDialog):
            end_confirmations = 0

            def locator(self, selector: str) -> Any:
                return _FakeLinks(self) if "a[href" in selector else NoLoaders()

            async def evaluate(self, expression: str) -> Any:
                if "relation-action: measure" in expression:
                    self.end_confirmations += 1
                    return {"bottom": True, "height": len(self.current)}
                return None

        dialog = SettledDialog([[f"account_{index:03d}" for index in range(99)]])
        worker = _RelationDialogWorker(
            100, dialog
        )
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 2
        stored: set[str] = set()

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        outcome = await worker.collect_followers(
            "drifting_source", limit=100, candidate_sink=sink
        )
        self.assertEqual(99, outcome.candidate_count)
        self.assertEqual(99, len(stored))
        self.assertGreaterEqual(dialog.end_confirmations, 2)

    async def test_dialog_flushes_no_more_than_one_hundred_names(self) -> None:
        worker = _DialogWorker(_NoopBitBrowser())
        worker.collection_poll_interval_seconds = 0
        dialog = _FakeDialog([[f"account_{index:03d}" for index in range(250)]])
        stored: set[str] = set()
        batch_sizes: list[int] = []

        async def sink(batch: list[str]) -> int:
            batch_sizes.append(len(batch))
            stored.update(batch)
            return len(stored)

        usernames = await worker._read_visible_account_dialog(
            dialog,
            250,
            candidate_sink=sink,
            candidate_total_limit=250,
        )
        self.assertEqual([], usernames)
        self.assertEqual([100, 100, 50], batch_sizes)
        self.assertEqual(250, len(stored))

    async def test_resume_uses_moving_tail_not_new_insert_count(self) -> None:
        worker = _DialogWorker(_NoopBitBrowser())
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 2
        existing = {f"account_{index:03d}" for index in range(30)}
        snapshots = [
            [f"account_{index:03d}" for index in range(start, start + 5)]
            for start in range(0, 30, 5)
        ]
        snapshots.append([f"account_{index:03d}" for index in range(30, 40)])
        dialog = _FakeDialog(snapshots)

        async def sink(batch: list[str]) -> int:
            existing.update(batch)
            return len(existing)

        await worker._read_visible_account_dialog(
            dialog,
            40,
            candidate_sink=sink,
            initial_candidate_count=30,
            candidate_total_limit=40,
        )
        self.assertEqual(40, len(existing))
        self.assertEqual(len(snapshots) - 1, dialog.index)

    async def test_two_post_liker_surfaces_keep_the_per_post_limit(self) -> None:
        worker = _DialogWorker(_NoopBitBrowser())
        worker.collection_poll_interval_seconds = 0
        stored: set[str] = set()
        batches: list[list[str]] = []

        async def sink(batch: list[str]) -> int:
            batches.append(list(batch))
            stored.update(batch)
            return len(stored)

        await worker._read_visible_account_dialog(
            _FakeDialog([["liker_a", "liker_b", "liker_c", "liker_d"]]),
            3,
            surface_kind="post_likers",
            candidate_sink=sink,
            initial_candidate_count=0,
            candidate_total_limit=6,
            surface_unique_limit=3,
        )
        self.assertEqual({"liker_a", "liker_b", "liker_c"}, stored)
        await worker._read_visible_account_dialog(
            _FakeDialog([["liker_a", "liker_e", "liker_f", "liker_g"]]),
            3,
            surface_kind="post_likers",
            candidate_sink=sink,
            initial_candidate_count=3,
            candidate_total_limit=6,
            surface_unique_limit=3,
        )
        # The second surface consumes its own three visible rows (one duplicate and
        # two new), instead of the first post consuming the six-row global allowance.
        self.assertEqual(
            {"liker_a", "liker_b", "liker_c", "liker_e", "liker_f"}, stored
        )
        self.assertEqual([3, 3], [len(batch) for batch in batches])

    async def test_near_limit_resume_keeps_hundred_row_batches(self) -> None:
        worker = _DialogWorker(_NoopBitBrowser())
        worker.collection_poll_interval_seconds = 0
        stored = {f"account_{index:04d}" for index in range(2999)}
        dialog = _FakeDialog([[f"account_{index:04d}" for index in range(3000)]])
        batch_sizes: list[int] = []

        async def sink(batch: list[str]) -> int:
            batch_sizes.append(len(batch))
            for username in batch:
                if len(stored) >= 3000:
                    break
                stored.add(username)
            return len(stored)

        await worker._read_visible_account_dialog(
            dialog,
            3000,
            candidate_sink=sink,
            initial_candidate_count=2999,
            candidate_total_limit=3000,
        )
        self.assertEqual(3000, len(stored))
        self.assertEqual(30, len(batch_sizes))
        self.assertTrue(all(size == 100 for size in batch_sizes))


if __name__ == "__main__":
    unittest.main()
