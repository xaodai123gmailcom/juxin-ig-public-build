from __future__ import annotations

import asyncio
import contextvars
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from completion_wait import wait_for_collection_operation
from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.playwright_worker import WorkerExecutionError
from app.service import CoreService


PASSWORD = "parallel-relation-pipeline-password"


class _HeldDatabaseRead:
    """Hold one real SQLite connection until the test releases its read."""

    def __init__(self, service, original, *, should_hold=lambda: True, error=None):
        self.service = service
        self.original = original
        self.should_hold = should_hold
        self.error = error
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = threading.Event()
        self.connection_closed = threading.Event()
        self._lock = threading.Lock()
        self._held = False

    def __call__(self, *args, **kwargs):
        with self._lock:
            hold = not self._held and self.should_hold()
            self._held = self._held or hold
        if not hold:
            return self.original(*args, **kwargs)
        try:
            with self.service.database.read() as connection:
                connection.execute("SELECT COUNT(*) FROM task_mode_candidates").fetchone()
                self.loop.call_soon_threadsafe(self.started.set)
                # The owning test always releases/joins this injected stall in
                # finally. The normal per-case watchdog still fails closed.
                self.release.wait()
                result = self.original(*args, **kwargs)
                if self.error is not None:
                    raise self.error
                return result
        finally:
            self.connection_closed.set()
            self.loop.call_soon_threadsafe(self.closed.set)


async def _event_loop_turn() -> None:
    """Let already-queued cancellation callbacks run without a timing sleep."""
    loop = asyncio.get_running_loop()
    advanced = loop.create_future()
    loop.call_soon(advanced.set_result, None)
    await advanced


class _NoopBitBrowser:
    """Local provider used by pipeline tests, including full manager runs."""

    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_profile(self, profile_id: str) -> dict[str, bool]:
        self.closed.append(profile_id)
        return {"closed": True}


class _PipelineState:
    def __init__(self, prefix: str = "pipeline") -> None:
        self.candidates = [f"{prefix}_a", f"{prefix}_b", f"{prefix}_c"]
        self.source_running = False
        self.source_started = asyncio.Event()
        self.source_cancelled = asyncio.Event()
        self.screen_started = asyncio.Event()
        self.allow_screen = asyncio.Event()
        self.child_cancelled = asyncio.Event()
        self.overlap_observed = False
        self.screened: list[tuple[str, str]] = []


class _ScreeningChild:
    def __init__(
        self,
        state: _PipelineState,
        *,
        fail: bool = False,
        block: bool = False,
    ) -> None:
        self.state = state
        self.fail = fail
        self.block = block
        self.disconnect_calls = 0

    async def screen(self, username: str) -> None:
        self.state.screen_started.set()
        if self.state.source_running:
            self.state.overlap_observed = True
        if self.fail:
            raise RuntimeError("screening child crashed")
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.state.child_cancelled.set()
                raise
        await self.state.allow_screen.wait()
        self.state.screened.append(("child", username))

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _RelationParent:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    supports_parallel_screening_tab = True

    def __init__(
        self,
        state: _PipelineState,
        *,
        child: _ScreeningChild | None = None,
        create_error: Exception | None = None,
        block_source: bool = False,
    ) -> None:
        self.state = state
        self.child = child
        self.create_error = create_error
        self.block_source = block_source
        self.create_calls = 0
        self.parent_screened_while_source_running = False

    async def create_parallel_screening_worker(self) -> _ScreeningChild:
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        assert self.child is not None
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
        self.state.source_running = True
        self.state.source_started.set()
        try:
            await progress_sink(
                {
                    "source_total": 3,
                    "resume_tail": self.state.candidates[:2],
                    "rendered_count": 37,
                    "progress_epoch": 11,
                }
            )
            await candidate_sink(self.state.candidates)
            if self.child is not None and self.create_error is None:
                # The owning test supplies the deadline (including slow SQLite
                # commits). This event is an ordering barrier, not a speed check.
                await self.state.screen_started.wait()
                self.state.overlap_observed = self.state.overlap_observed or bool(
                    self.state.source_running
                )
            if self.block_source:
                await asyncio.Event().wait()
            self.state.allow_screen.set()
            await asyncio.sleep(0)
            return CollectionOutcome("followers", [], source_total=3)
        except asyncio.CancelledError:
            self.state.source_cancelled.set()
            raise
        finally:
            self.state.source_running = False

    async def collect_following(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("following was not requested")

    async def collect_post_likers(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("post likers were not requested")

    async def screen(self, username: str) -> None:
        if self.state.source_running:
            self.parent_screened_while_source_running = True
        self.state.screened.append(("parent", username))


class _PipelineManager(ExecutionManager):
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
        del control, target_id, mode, settings, claim_id
        await worker.screen(username)
        return True


class ParallelRelationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp_dir.name) / "parallel.sqlite3")
        database.initialize()
        self.service = CoreService(database)
        self.user = self.service.register_user("parallel-pipeline", PASSWORD)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _task_and_control(
        self,
        parallel_screening_workers: int = 2,
    ) -> tuple[dict[str, Any], dict[str, Any], ExecutionControl]:
        # Each topology / independent owner pipeline needs its own source. These
        # cases test child ownership, not duplicate admission or legacy reuse.
        self._fixture_source_serial = getattr(self, "_fixture_source_serial", 0) + 1
        task = self.service.create_task(
            self.user["id"],
            name=f"1-{parallel_screening_workers} relation pipeline",
            modes=["followers"],
            targets=[f"source_account_{self._fixture_source_serial}"],
            settings={
                "location_enabled": False,
                "parallel_screening_workers": parallel_screening_workers,
            },
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

    async def test_paused_page_failure_waits_without_tearing_down_or_reopening(self):
        task,target,control=self._task_and_control(1)
        state=_PipelineState(prefix='closed_page')
        class ClosedChild(_ScreeningChild):
            async def screen(self,username):
                state.screen_started.set()
                await state.allow_screen.wait()
                raise RuntimeError('Target page, context or browser has been closed')
        child=ClosedChild(state);parent=_RelationParent(state,child=child)
        manager=_PipelineManager(self.service,_NoopBitBrowser())
        running=asyncio.create_task(manager._execute_candidate_spooled_mode(control,parent,target,'followers',task['settings'],None))
        try:
            await asyncio.wait_for(state.screen_started.wait(),3)
            control.pause_event.clear();state.allow_screen.set()
            await asyncio.sleep(.15)
            self.assertFalse(running.done())
            self.assertEqual(child.disconnect_calls,0)
            self.assertEqual(parent.create_calls,1)
            control.pause_event.set()
            result=await asyncio.wait_for(running,5)
            self.assertEqual(result['pending'],0)
            self.assertEqual(result['recorded'],3)
        finally:
            control.pause_event.set()
            if not running.done():running.cancel()
            await asyncio.gather(running,return_exceptions=True)

    async def test_selectable_topologies_obey_three_child_cap(self) -> None:
        for configured_count in (1, 2, 3):
            child_count=configured_count
            with self.subTest(topology=f"1-{configured_count}"):
                task, target, control = self._task_and_control(child_count)
                task["settings"]["parallel_screening_workers"]=configured_count
                state = _PipelineState(prefix=f"select_{configured_count}")
                child = _ScreeningChild(state)
                parent = _RelationParent(state, child=child)
                manager = _PipelineManager(self.service, _NoopBitBrowser())

                progress = await wait_for_collection_operation(
                    lambda: manager._execute_candidate_spooled_mode(
                        control, parent, target,
                        "followers", task["settings"], None,
                    ),
                    manager, self.service, control.owner_user_id, control.task_id,
                )

                self.assertEqual(child_count, parent.create_calls)
                self.assertEqual(3, progress["recorded"])
                self.assertEqual(0, progress["pending"])

    async def test_two_child_slots_overlap_source_and_screen_each_row_once(self) -> None:
        task, target, control = self._task_and_control()
        state = _PipelineState()
        child = _ScreeningChild(state)
        parent = _RelationParent(state, child=child)
        manager = _PipelineManager(self.service, _NoopBitBrowser())

        progress = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], None,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )

        self.assertTrue(state.overlap_observed)
        self.assertEqual(2, parent.create_calls)
        self.assertEqual(1, child.disconnect_calls)
        self.assertFalse(parent.parent_screened_while_source_running)
        self.assertEqual(
            ["pipeline_a", "pipeline_b", "pipeline_c"],
            sorted(username for _role, username in state.screened),
        )
        self.assertEqual(3, len({username for _role, username in state.screened}))
        self.assertTrue(all(role == "child" for role, _username in state.screened))
        self.assertEqual(0, progress["pending"])

    async def test_hover_failure_drains_confirmed_child_rows_before_pausing(self) -> None:
        task, target, control = self._task_and_control(1)
        state = _PipelineState(prefix="hover_drain")
        source_failed = asyncio.Event()

        class Parent(_RelationParent):
            async def collect_followers(self, target, *, limit, candidate_sink,
                                        initial_candidate_count, progress_sink,
                                        initial_resume_tail=None):
                await progress_sink({"source_total": 23, "resume_tail": ["hover_drain_b"],
                                     "rendered_count": 6, "progress_epoch": 2})
                await candidate_sink(state.candidates)
                await state.screen_started.wait()
                source_failed.set()
                raise WorkerExecutionError("hover card missing",
                                           reason="instagram_hover_card_unavailable",
                                           pause_required=True)

        child = _ScreeningChild(state)
        parent = Parent(state, child=child)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        running = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(source_failed.wait(), 3)
            await asyncio.sleep(.05)
            self.assertFalse(running.done())
            self.assertEqual(0, child.disconnect_calls)
            self.assertFalse(state.child_cancelled.is_set())
            state.allow_screen.set()
            with self.assertRaises(WorkerExecutionError) as error:
                await asyncio.wait_for(running, 5)
            self.assertEqual("instagram_hover_card_unavailable", error.exception.code)
            self.assertEqual(sorted(state.candidates), sorted(name for _, name in state.screened))
            stats = self.service.task_mode_candidate_stats(
                self.user["id"], task["id"], target["id"], "followers")
            self.assertEqual(0, stats["pending"])
            checkpoint = self.service.get_checkpoint(
                self.user["id"], task["id"], target["id"], "followers")
            self.assertFalse(checkpoint["cursor"].get("discovery_complete", False))
        finally:
            state.allow_screen.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_hover_failure_parent_drains_when_children_crash_or_cannot_connect(self) -> None:
        for creation_failed in (False, True):
            with self.subTest(creation_failed=creation_failed):
                task, target, control = self._task_and_control(1)
                state = _PipelineState(prefix=f"fallback_{creation_failed}")

                class Parent(_RelationParent):
                    async def collect_followers(self, target, *, limit, candidate_sink,
                                                initial_candidate_count, progress_sink,
                                                initial_resume_tail=None):
                        await candidate_sink(state.candidates)
                        if not creation_failed:
                            await state.screen_started.wait()
                        raise WorkerExecutionError("hover card missing",
                                                   reason="instagram_hover_card_unavailable",
                                                   pause_required=True)

                parent = Parent(state, child=_ScreeningChild(state, fail=True)
                                if not creation_failed else None,
                                create_error=RuntimeError('no child target')
                                if creation_failed else None)
                manager = _PipelineManager(self.service, _NoopBitBrowser())
                with self.assertRaises(WorkerExecutionError) as error:
                    await wait_for_collection_operation(
                        lambda: manager._execute_candidate_spooled_mode(
                            control, parent, target,
                            "followers", task["settings"], None,
                        ),
                        manager, self.service, control.owner_user_id, control.task_id,
                    )
                self.assertEqual('instagram_hover_card_unavailable', error.exception.code)
                self.assertEqual(sorted(state.candidates), sorted(
                    username for role, username in state.screened if role == 'parent'))
                stats = self.service.task_mode_candidate_stats(
                    self.user['id'], task['id'], target['id'], 'followers')
                self.assertEqual(0, stats['pending'])

    async def test_child_creation_failure_uses_parent_before_source_moves(self) -> None:
        task, target, control = self._task_and_control()
        state = _PipelineState()
        parent = _RelationParent(
            state,
            create_error=RuntimeError("new child page unavailable"),
        )
        manager = _PipelineManager(self.service, _NoopBitBrowser())

        progress = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], None,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )

        self.assertEqual(2, parent.create_calls)
        self.assertFalse(parent.parent_screened_while_source_running)
        self.assertTrue(all(role == "parent" for role, _ in state.screened))
        self.assertEqual(3, progress["recorded"])

    async def test_child_failure_falls_back_after_source_and_keeps_latest_tail(self) -> None:
        task, target, control = self._task_and_control()
        state = _PipelineState()
        child = _ScreeningChild(state, fail=True)
        parent = _RelationParent(state, child=child)
        manager = _PipelineManager(self.service, _NoopBitBrowser())

        progress = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], None,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )

        self.assertTrue(state.overlap_observed)
        self.assertEqual(1, child.disconnect_calls)
        self.assertFalse(parent.parent_screened_while_source_running)
        self.assertEqual(3, progress["recorded"])
        self.assertEqual(3, len({username for _role, username in state.screened}))
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual(["pipeline_a", "pipeline_b"], checkpoint["cursor"]["resume_tail"])
        self.assertEqual(37, checkpoint["cursor"]["rendered_count"])
        self.assertEqual(11, checkpoint["cursor"]["progress_epoch"])

    async def test_exhausted_replacement_pauses_before_parent_fallback(self) -> None:
        from app.playwright_worker import WorkerExecutionError

        task, target, control = self._task_and_control()
        state = _PipelineState()

        class Child(_ScreeningChild):
            async def screen(self, username: str) -> None:
                self.state.screen_started.set()
                raise WorkerExecutionError("new page still stalled", reason="instagram_page_recovery_exhausted", pause_required=True)

        child = Child(state)
        parent = _RelationParent(state, child=child, block_source=True)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError) as error:
            await wait_for_collection_operation(
                lambda: manager._execute_candidate_spooled_mode(
                    control, parent, target,
                    "followers", task["settings"], None,
                ),
                manager, self.service, control.owner_user_id, control.task_id,
            )
        self.assertEqual("instagram_page_recovery_exhausted", error.exception.code)
        self.assertTrue(state.source_cancelled.is_set())
        self.assertEqual([], state.screened)
        pending = self.service.task_mode_candidate_stats(self.user["id"], task["id"], target["id"], "followers")
        self.assertEqual(3, pending["pending"])

    async def test_exhausted_replacement_joins_sibling_read_before_cleanup(self) -> None:
        task, target, control = self._task_and_control()
        state = _PipelineState(prefix="held_read")
        read_owner = contextvars.ContextVar("pipeline_read_owner", default=None)
        reader_drain_finished = asyncio.Event()
        reader_close_states: list[bool] = []
        failure = WorkerExecutionError(
            "new page still stalled", reason="instagram_page_recovery_exhausted",
            pause_required=True,
        )

        class FailingChild(_ScreeningChild):
            async def screen(self, username: str) -> None:
                self.state.screen_started.set()
                await held_read.started.wait()
                raise failure

        class ReadingChild(_ScreeningChild):
            async def disconnect(self) -> None:
                reader_close_states.append(held_read.connection_closed.is_set())
                await super().disconnect()

        first_child = FailingChild(state)
        reading_child = ReadingChild(state)

        class Parent(_RelationParent):
            async def create_parallel_screening_worker(self):
                self.create_calls += 1
                return first_child if self.create_calls == 1 else reading_child

        class Manager(_PipelineManager):
            async def _drain_candidate_spool(self, control, worker, *args, **kwargs):
                if worker is reading_child:
                    await state.screen_started.wait()
                token = read_owner.set(worker)
                try:
                    return await super()._drain_candidate_spool(control, worker, *args, **kwargs)
                finally:
                    read_owner.reset(token)
                    if worker is reading_child and held_read.started.is_set():
                        reader_drain_finished.set()

        original_read = self.service.list_pending_task_mode_candidates
        held_read = _HeldDatabaseRead(
            self.service, original_read,
            should_hold=lambda: read_owner.get() is reading_child,
        )
        self.service.list_pending_task_mode_candidates = held_read
        parent = Parent(state, child=first_child, block_source=True)
        manager = Manager(self.service, _NoopBitBrowser())
        running = asyncio.create_task(wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target, "followers", task["settings"], None,
            ), manager, self.service, control.owner_user_id, control.task_id,
        ))
        try:
            await asyncio.wait_for(state.source_cancelled.wait(), 10)
            # settle_pipeline cancels source and siblings in one synchronous
            # step. Observe those queued cancellations before releasing SQLite.
            await _event_loop_turn()
            self.assertFalse(reader_drain_finished.is_set(), "cancelled reader lost ownership")
            self.assertFalse(held_read.connection_closed.is_set())
            self.assertFalse(running.done())
            self.assertEqual([], reader_close_states)
            held_read.release.set()
            with self.assertRaises(WorkerExecutionError) as error:
                await asyncio.wait_for(running, 10)
            self.assertIs(failure, error.exception)
            self.assertTrue(held_read.connection_closed.is_set())
            self.assertEqual([True], reader_close_states)
            self.assertTrue(state.source_cancelled.is_set())
            self.assertEqual([], state.screened)
            self.assertFalse(parent.parent_screened_while_source_running)
            self.assertEqual(1, first_child.disconnect_calls)
            self.assertEqual(1, reading_child.disconnect_calls)
            pending = self.service.task_mode_candidate_stats(
                self.user["id"], task["id"], target["id"], "followers",
            )
            self.assertEqual(3, pending["pending"])
        finally:
            held_read.release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if held_read.started.is_set():
                await asyncio.wait_for(held_read.closed.wait(), 10)
            self.service.list_pending_task_mode_candidates = original_read

    async def test_cancelled_read_keeps_reservation_through_repeated_cancel_and_late_error(self):
        for late_error in (None, OSError("late SQLite read failure")):
            with self.subTest(late_error=late_error is not None):
                task, target, control = self._task_and_control(1)
                self.service.append_task_mode_candidates(
                    self.user["id"], task["id"], target["id"], "followers", ["held_candidate"],
                )
                original_read = self.service.list_pending_task_mode_candidates
                held_read = _HeldDatabaseRead(self.service, original_read, error=late_error)
                self.service.list_pending_task_mode_candidates = held_read
                manager = _PipelineManager(self.service, _NoopBitBrowser())
                state = _PipelineState()
                child = _ScreeningChild(state)
                reservation_lock = asyncio.Lock()
                reservations: set[str] = set()
                running = asyncio.create_task(manager._drain_candidate_spool(
                    control, child, target["id"], "followers", task["settings"],
                    discovery_complete=False, candidate_reservation_lock=reservation_lock,
                    candidate_reservations=reservations,
                ))
                try:
                    await asyncio.wait_for(held_read.started.wait(), 10)
                    for _ in range(3):
                        running.cancel()
                        await _event_loop_turn()
                        self.assertFalse(running.done(), "read owner returned before connection close")
                        self.assertTrue(reservation_lock.locked())
                        self.assertFalse(held_read.connection_closed.is_set())
                    held_read.release.set()
                    with self.assertRaises(asyncio.CancelledError) as cancelled:
                        await asyncio.wait_for(running, 10)
                    self.assertIs(late_error, cancelled.exception.__cause__)
                    self.assertTrue(held_read.connection_closed.is_set())
                    self.assertFalse(reservation_lock.locked())
                    self.assertEqual(set(), reservations)
                    self.assertEqual([], state.screened)
                    self.assertEqual(1, self.service.task_mode_candidate_stats(
                        self.user["id"], task["id"], target["id"], "followers",
                    )["pending"])
                finally:
                    held_read.release.set()
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    if held_read.started.is_set():
                        await asyncio.wait_for(held_read.closed.wait(), 10)
                    self.service.list_pending_task_mode_candidates = original_read

    async def test_owned_read_preserves_success_and_uncancelled_error(self):
        result = object()
        failure = OSError("uncancelled SQLite read failure")
        for expected_error in (None, failure):
            with self.subTest(error=expected_error is not None):
                held_read = _HeldDatabaseRead(
                    self.service, lambda: result, error=expected_error,
                )
                running = asyncio.create_task(ExecutionManager._await_durable_thread_call(held_read))
                try:
                    await asyncio.wait_for(held_read.started.wait(), 10)
                    held_read.release.set()
                    if expected_error is None:
                        self.assertIs(result, await asyncio.wait_for(running, 10))
                    else:
                        with self.assertRaises(OSError) as error:
                            await asyncio.wait_for(running, 10)
                        self.assertIs(failure, error.exception)
                    self.assertTrue(held_read.connection_closed.is_set())
                finally:
                    held_read.release.set()
                    await asyncio.gather(running, return_exceptions=True)
                    if held_read.started.is_set():
                        await asyncio.wait_for(held_read.closed.wait(), 10)

    async def test_candidate_page_failure_keeps_source_and_checkpoint_then_retries_retained_page(self):
        from app.playwright_worker import WorkerExecutionError
        task, target, control = self._task_and_control(1)
        state = _PipelineState()
        class Child(_ScreeningChild):
            failed = False
            async def screen(self, username):
                self.state.screen_started.set()
                if not self.failed:
                    self.failed = True
                    error = WorkerExecutionError('candidate page stalled', reason='instagram_page_recovery_exhausted')
                    error.details.update(original_reason='instagram_profile_not_ready', auto_retry=False)
                    raise error
                await super().screen(username)
        class Parent(_RelationParent):
            collections = 0
            async def collect_followers(self, *args, **kwargs):
                self.collections += 1
                return await super().collect_followers(*args, **kwargs)
        child = Child(state); parent = Parent(state, child=child)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError) as error:
            await wait_for_collection_operation(
                lambda: manager._execute_candidate_spooled_mode(
                    control, parent, target,
                    'followers', task['settings'], None,
                ),
                manager, self.service, control.owner_user_id, control.task_id,
            )
        self.assertFalse(state.source_cancelled.is_set())
        self.assertEqual(0, child.disconnect_calls, 'failed old page stays owned for handling')
        self.assertTrue(error.exception.details['source_discovery_complete'])
        self.assertEqual('pipeline_a', error.exception.details['candidate_username'])
        checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
        self.assertTrue(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
        self.assertEqual(1, self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')['pending'])
        self.assertEqual(['pipeline_b', 'pipeline_c'], sorted(username for _, username in state.screened),
                         'a failed child must not block healthy pending candidates')
        result = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                'followers', task['settings'], checkpoint,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )
        self.assertEqual(0, result['pending'])
        self.assertEqual(1, parent.collections, 'completed source is never rescanned')
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)

    async def test_failed_candidate_is_not_claimed_by_healthy_sibling(self):
        from app.playwright_worker import WorkerExecutionError
        task, target, control = self._task_and_control(2)
        state = _PipelineState()
        class Child(_ScreeningChild):
            async def screen(self, username):
                self.state.screen_started.set()
                if username == 'pipeline_a':
                    error = WorkerExecutionError('candidate page stalled', reason='instagram_page_recovery_exhausted')
                    error.details.update(original_reason='instagram_profile_not_ready', auto_retry=False)
                    raise error
                await super().screen(username)
        children = [Child(state), Child(state)]
        class Parent(_RelationParent):
            async def create_parallel_screening_worker(self):
                child = children[self.create_calls]
                self.create_calls += 1
                return child
        parent = Parent(state, child=children[0])
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError):
            await wait_for_collection_operation(
                lambda: manager._execute_candidate_spooled_mode(
                    control, parent, target,
                    'followers', task['settings'], None,
                ),
                manager, self.service, control.owner_user_id, control.task_id,
            )
        self.assertFalse(state.source_cancelled.is_set())
        self.assertEqual(['pipeline_b', 'pipeline_c'], sorted(name for _, name in state.screened))
        self.assertEqual(1, self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')['pending'])
        self.assertEqual(1, len(parent._deferred_screening_workers))

    async def test_stop_cancels_and_closes_both_pages_with_resumable_checkpoint(self) -> None:
        task, target, control = self._task_and_control()
        state = _PipelineState()
        child = _ScreeningChild(state, block=True)
        parent = _RelationParent(state, child=child, block_source=True)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
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

        await asyncio.wait_for(state.source_started.wait(), timeout=2)
        await asyncio.wait_for(state.screen_started.wait(), timeout=2)
        control.stop_event.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)

        self.assertTrue(state.source_cancelled.is_set())
        self.assertTrue(state.child_cancelled.is_set())
        self.assertEqual(1, child.disconnect_calls)
        stats = self.service.task_mode_candidate_stats(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual(3, stats["pending"])
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual("discovering_accounts", checkpoint["stage"])
        self.assertFalse(checkpoint["cursor"]["candidate_spool_complete"])
        self.assertEqual(["pipeline_a", "pipeline_b"], checkpoint["cursor"]["resume_tail"])
        self.assertEqual(11, checkpoint["cursor"]["progress_epoch"])


if __name__ == "__main__":
    unittest.main()
