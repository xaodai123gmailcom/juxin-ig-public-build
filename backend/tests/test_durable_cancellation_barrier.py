from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.service import CoreService


PASSWORD = "durable-cancellation-barrier-password"


class _NoopBitBrowser:
    pass


class _IdleScreeningChild:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _BarrierRelationParent:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    supports_parallel_screening_tab = True

    def __init__(
        self,
        *,
        emit_progress: bool = False,
        emit_candidates: bool = False,
    ) -> None:
        self.emit_progress = emit_progress
        self.emit_candidates = emit_candidates
        self.child = _IdleScreeningChild()

    async def create_parallel_screening_worker(self) -> _IdleScreeningChild:
        return self.child

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
        del target, limit, initial_candidate_count, initial_resume_tail
        if self.emit_progress:
            await progress_sink(
                {
                    "source_total": 1,
                    "resume_tail": ["old_tail"],
                    "rendered_count": 1,
                    "progress_epoch": 1,
                }
            )
        if self.emit_candidates:
            await candidate_sink(["durable_candidate"])
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _NoScreenManager(ExecutionManager):
    async def _screen_and_record(
        self,
        control: ExecutionControl,
        worker: Any,
        target_id: str,
        username: str,
        mode: str,
        settings: dict[str, Any],
        *,
        claim_id: str | None = None,
    ) -> bool:
        del control, worker, target_id, username, mode, settings, claim_id
        raise AssertionError("cancellation must happen before browser screening")


class DurableCancellationBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "durable-barrier.sqlite3")
        database.initialize()
        self.service = CoreService(database)
        self.user = self.service.register_user("durable-barrier", PASSWORD)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _task_and_control(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], ExecutionControl]:
        task = self.service.create_task(
            self.user["id"],
            name="durable cancellation barrier",
            modes=["followers"],
            targets=["source_account"],
            settings={"location_enabled": False},
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.user["id"],
            task_id=task["id"],
            pause_event=pause,
            stop_event=asyncio.Event(),
            leases={},
            target_queue=asyncio.Queue(),
        )
        return task, task["targets"][0], control

    async def _wait_for_thread(self, event: threading.Event) -> None:
        self.assertTrue(await asyncio.to_thread(event.wait, 2))

    async def test_durable_thread_call_drains_commit_before_cancelled_error(self) -> None:
        started = threading.Event()
        release = threading.Event()
        committed = threading.Event()

        def blocked_write() -> str:
            started.set()
            if not release.wait(2):
                raise TimeoutError("test did not release durable write")
            committed.set()
            return "committed"

        operation = asyncio.create_task(
            ExecutionManager._await_durable_thread_call(blocked_write)
        )
        await self._wait_for_thread(started)
        operation.cancel()
        await asyncio.sleep(0.05)

        self.assertFalse(operation.done())
        self.assertFalse(committed.is_set())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertTrue(committed.is_set())

    async def test_pipeline_joins_old_upsert_before_final_stop_checkpoint(self) -> None:
        task, target, control = self._task_and_control()
        manager = _NoScreenManager(self.service, _NoopBitBrowser())
        parent = _BarrierRelationParent(emit_progress=True)
        original_upsert = self.service.upsert_checkpoint
        first_started = threading.Event()
        release_first = threading.Event()
        sequence: list[str] = []
        sequence_lock = threading.Lock()
        call_count = 0

        def blocked_first_upsert(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            with sequence_lock:
                call_count += 1
                number = call_count
                sequence.append(f"start-{number}")
            if number == 1:
                first_started.set()
                if not release_first.wait(2):
                    raise TimeoutError("test did not release old checkpoint")
            result = original_upsert(*args, **kwargs)
            with sequence_lock:
                sequence.append(f"commit-{number}")
            return result

        self.service.upsert_checkpoint = blocked_first_upsert  # type: ignore[method-assign]
        execution = asyncio.create_task(
            manager._execute_candidate_spooled_mode(
                control,
                parent,
                target,
                "followers",
                task["settings"],
                None,
            )
        )
        try:
            await self._wait_for_thread(first_started)
            control.stop_event.set()
            await asyncio.sleep(0.05)
            self.assertFalse(execution.done())

            release_first.set()
            with self.assertRaises(asyncio.CancelledError):
                await execution
        finally:
            release_first.set()
            self.service.upsert_checkpoint = original_upsert  # type: ignore[method-assign]
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

        # The pipeline's cancellation cleanup owns call 2.  It cannot start (and
        # therefore cannot be overwritten) until the cancelled call 1 has committed.
        self.assertGreaterEqual(call_count, 2)
        self.assertLess(sequence.index("commit-1"), sequence.index("start-2"))
        self.assertEqual(1, parent.child.disconnect_calls)
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual("discovering_accounts", checkpoint["stage"])
        self.assertFalse(checkpoint["cursor"]["candidate_spool_complete"])

    async def test_pipeline_stop_waits_for_candidate_append_commit(self) -> None:
        task, target, control = self._task_and_control()
        manager = _NoScreenManager(self.service, _NoopBitBrowser())
        parent = _BarrierRelationParent(emit_candidates=True)
        original_append = self.service.append_task_mode_candidates
        append_started = threading.Event()
        release_append = threading.Event()
        append_committed = threading.Event()
        stop_returned = threading.Event()
        late_commit = threading.Event()

        def blocked_append(*args: Any, **kwargs: Any) -> Any:
            append_started.set()
            if not release_append.wait(2):
                raise TimeoutError("test did not release candidate append")
            result = original_append(*args, **kwargs)
            if stop_returned.is_set():
                late_commit.set()
            append_committed.set()
            return result

        self.service.append_task_mode_candidates = blocked_append  # type: ignore[method-assign]
        execution = asyncio.create_task(
            manager._execute_candidate_spooled_mode(
                control,
                parent,
                target,
                "followers",
                task["settings"],
                None,
            )
        )
        try:
            await self._wait_for_thread(append_started)
            control.stop_event.set()
            await asyncio.sleep(0.05)
            self.assertFalse(execution.done())
            self.assertFalse(append_committed.is_set())

            release_append.set()
            with self.assertRaises(asyncio.CancelledError):
                await execution
            stop_returned.set()
        finally:
            release_append.set()
            self.service.append_task_mode_candidates = original_append  # type: ignore[method-assign]
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

        self.assertTrue(append_committed.is_set())
        self.assertFalse(late_commit.is_set())
        stats = self.service.task_mode_candidate_stats(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual(1, stats["pending"])

    async def test_cancelled_drain_waits_for_identity_claim_commit(self) -> None:
        task, target, control = self._task_and_control()
        self.service.append_task_mode_candidates(
            self.user["id"],
            task["id"],
            target["id"],
            "followers",
            ["claim_barrier_candidate"],
        )
        manager = _NoScreenManager(self.service, _NoopBitBrowser())
        original_claim = self.service.claim_workbench_identity
        claim_started = threading.Event()
        release_claim = threading.Event()
        claim_committed = threading.Event()
        stop_returned = threading.Event()
        late_commit = threading.Event()

        def blocked_claim(*args: Any, **kwargs: Any) -> Any:
            claim_started.set()
            if not release_claim.wait(2):
                raise TimeoutError("test did not release identity claim")
            result = original_claim(*args, **kwargs)
            if stop_returned.is_set():
                late_commit.set()
            claim_committed.set()
            return result

        self.service.claim_workbench_identity = blocked_claim  # type: ignore[method-assign]
        drain = asyncio.create_task(
            manager._drain_candidate_spool(
                control,
                object(),
                target["id"],
                "followers",
                task["settings"],
                discovery_complete=False,
            )
        )
        try:
            await self._wait_for_thread(claim_started)
            drain.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(drain.done())
            self.assertFalse(claim_committed.is_set())

            release_claim.set()
            with self.assertRaises(asyncio.CancelledError):
                await drain
            stop_returned.set()
        finally:
            release_claim.set()
            self.service.claim_workbench_identity = original_claim  # type: ignore[method-assign]
            if not drain.done():
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)

        self.assertTrue(claim_committed.is_set())
        self.assertFalse(late_commit.is_set())
        duplicate = self.service.claim_workbench_identity(
            self.user["id"],
            username="claim_barrier_candidate",
            source="followers",
            source_target=target["id"],
        )
        self.assertTrue(duplicate["duplicate"])


if __name__ == "__main__":
    unittest.main()
