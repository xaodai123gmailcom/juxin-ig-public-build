"""Real SQLite completion and ownership boundaries, without Instagram access."""
from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import test_parallel_relation_pipeline as pipeline
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome


TIMEOUT = 30


class PipelineCompletionR44Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    async def test_source_finished_with_all_profiles_pending_keeps_task_and_lease_active(self):
        for child_count in (1, 2):
            with self.subTest(children=child_count):
                source_saved, allow_profiles = asyncio.Event(), asyncio.Event()
                screened = []
                children = []
                closed_profiles = []
                names = [f"actual_{child_count}_{index}" for index in range(8)]

                class Browser:
                    def close_profile(self, profile_id):
                        closed_profiles.append(profile_id)
                        return {"closed": True}

                class Child:
                    closed = False

                    async def read_visible_profile(self, username, **_kwargs):
                        await allow_profiles.wait()
                        screened.append(username)
                        return {"username": username, "visibility": "private",
                                "followers": 12, "following": 9, "posts": 2}

                    async def disconnect(self):
                        self.closed = True

                class Parent(Child):
                    supports_candidate_batch_sink = True
                    supports_collection_progress_sink = True
                    supports_parallel_screening_tab = True

                    async def connect(self, *_args, **_kwargs):
                        pass

                    async def create_parallel_screening_worker(self):
                        child = Child()
                        children.append(child)
                        return child

                    async def collect_followers(self, _target, *, candidate_sink, **_kwargs):
                        await candidate_sink(names)
                        return CollectionOutcome("followers", [], source_total=999)

                class Manager(ExecutionManager):
                    async def _save_candidate_progress_checkpoint(self, *args, **kwargs):
                        await super()._save_candidate_progress_checkpoint(*args, **kwargs)
                        if kwargs.get("discovery_complete"):
                            source_saved.set()

                task = self.service.create_task(self.user["id"], name="source precedes screening",
                    modes=["followers"], targets=[f"source_{child_count}"],
                    window_ids=[f"window_{child_count}"], settings={
                        "parallel_screening_workers": child_count,
                        "local_person_recognition": False, "location_enabled": False,
                    })
                manager = Manager(self.service, Browser(),
                                  worker_factory=lambda _: Parent())
                try:
                    await manager.start(self.user["id"], task["id"])
                    control = manager._runs[task["id"]]
                    await asyncio.wait_for(source_saved.wait(), TIMEOUT)
                    stored = self.service.get_task(self.user["id"], task["id"])
                    stats = self.service.task_mode_candidate_stats(self.user["id"], task["id"],
                        task["targets"][0]["id"], "followers")
                    self.assertEqual((8, 0), (stats["pending"], stats["recorded"]))
                    self.assertEqual("running", stored["targets"][0]["status"])
                    self.assertNotEqual("completed", stored["status"])
                    self.assertFalse(control.coordinator.done())
                    self.assertEqual([], closed_profiles)
                    self.assertEqual(1, len(self.service.list_browser_lease_states(self.user["id"])))
                    allow_profiles.set()
                    await asyncio.wait_for(asyncio.shield(control.coordinator), TIMEOUT)
                    stored = self.service.get_task(self.user["id"], task["id"])
                    self.assertEqual("completed", stored["status"])
                    self.assertEqual(sorted(names), sorted(screened))
                    self.assertTrue(all(child.closed for child in children))
                    self.assertEqual([f"window_{child_count}"], closed_profiles)
                    with self.service.database.read() as connection:
                        rows = connection.execute("SELECT a.current_username_norm AS username_norm FROM task_results r JOIN instagram_accounts a ON a.id=r.account_id WHERE r.task_id=?",
                                                  (task["id"],)).fetchall()
                    self.assertEqual(sorted(names), sorted(row["username_norm"] for row in rows))
                    self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))
                finally:
                    allow_profiles.set()
                    await manager.shutdown()

    async def test_repeated_cancel_joins_reconciliation_write(self):
        task, target, control = self._task_and_control(1)
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        original = self.service.reconcile_task_mode_candidates

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(TIMEOUT):
                raise TimeoutError("test did not release reconciliation")
            result = original(*args, **kwargs)
            committed.set()
            return result

        self.service.reconcile_task_mode_candidates = blocked
        manager = ExecutionManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, object(), target, "followers", task["settings"], None))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, TIMEOUT))
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done(), "an owned SQLite write must settle before window teardown")
            self.assertFalse(committed.is_set())
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await asyncio.to_thread(committed.wait, TIMEOUT)
            self.service.reconcile_task_mode_candidates = original
        self.assertTrue(execution.cancelled())
        self.assertTrue(committed.is_set())

    async def test_successful_retained_retry_keeps_page_owned_until_cancelled_close_finishes(self):
        task, target, control = self._task_and_control(1)
        self.service.append_task_mode_candidates(self.user["id"], task["id"], target["id"],
                                                "followers", ["retained_retry"])
        close_started, allow_close = asyncio.Event(), asyncio.Event()
        state = pipeline._PipelineState()
        state.allow_screen.set()

        class Child(pipeline._ScreeningChild):
            async def disconnect(self):
                close_started.set()
                await allow_close.wait()
                await super().disconnect()

        child = Child(state)
        parent = pipeline._RelationParent(state)
        parent._deferred_screening_workers = {"retained_retry": child}
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}))
        try:
            await asyncio.wait_for(close_started.wait(), TIMEOUT)
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
            self.assertIs(child, parent._deferred_screening_workers.get("retained_retry"))
        finally:
            allow_close.set()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(execution.cancelled())
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)

    async def test_cancel_during_child_natural_close_joins_it_before_pipeline_exits(self):
        task, target, control = self._task_and_control(1)
        close_started, allow_close = asyncio.Event(), asyncio.Event()
        state = pipeline._PipelineState()

        class Child(pipeline._ScreeningChild):
            async def disconnect(self):
                close_started.set()
                await allow_close.wait()
                await super().disconnect()

        child = Child(state)

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, *_args, **_kwargs):
                return CollectionOutcome("followers", [], source_total=0)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=child), target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(close_started.wait(), TIMEOUT)
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done(), "source completion cannot release a closing child")
            self.assertEqual(0, child.disconnect_calls)
        finally:
            allow_close.set()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(execution.cancelled())
        self.assertEqual(1, child.disconnect_calls)
        checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
        self.assertTrue(manager._candidate_spool_complete(checkpoint, require_natural_end=True))

    async def test_repeated_cancel_during_second_child_creation_joins_first_child_close(self):
        task, target, control = self._task_and_control(2)
        second_create, close_started, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
        state = pipeline._PipelineState()

        class Child(pipeline._ScreeningChild):
            async def disconnect(self):
                close_started.set()
                await allow_close.wait()
                await super().disconnect()

        child = Child(state)

        class Parent(pipeline._RelationParent):
            async def create_parallel_screening_worker(self):
                self.create_calls += 1
                if self.create_calls == 1:
                    return child
                second_create.set()
                await asyncio.Event().wait()

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=child), target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(second_create.wait(), TIMEOUT)
            execution.cancel()
            await asyncio.wait_for(close_started.wait(), TIMEOUT)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
        finally:
            allow_close.set()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(execution.cancelled())
        self.assertEqual(1, child.disconnect_calls)
        self.assertFalse(state.source_started.is_set())

    async def test_cancelled_failure_checkpoint_joins_writer_and_preserves_natural_end(self):
        task, target, control = self._task_and_control(1)
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        self.service.upsert_checkpoint(self.user["id"], task["id"], target["id"],
            mode="followers", stage="screening_accounts", cursor={
                "candidate_spool_complete": True, "candidate_spool_natural_end": True},
            counters={"pending_candidates": 1}, recoverable=True)
        original = self.service.upsert_checkpoint

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(TIMEOUT):
                raise TimeoutError("test did not release failure checkpoint")
            result = original(*args, **kwargs)
            committed.set()
            return result

        self.service.upsert_checkpoint = blocked
        manager = ExecutionManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._save_profile_local_failure_checkpoint(
            control, target, "followers", None, reason="injected_failure", message="retry pending"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, TIMEOUT))
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await asyncio.to_thread(committed.wait, TIMEOUT)
            self.service.upsert_checkpoint = original
        self.assertTrue(execution.cancelled())
        checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
        self.assertTrue(manager._candidate_spool_complete(checkpoint, require_natural_end=True))

    async def test_repeated_cancel_before_window_disconnect_keeps_generation_and_lease(self):
        connect_started, close_started, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Worker:
            async def connect(self, *_args, **_kwargs):
                connect_started.set()
                await asyncio.Event().wait()

            async def disconnect(self):
                close_started.set()
                await allow_close.wait()

        task = self.service.create_task(self.user["id"], name="cancel cleanup lock",
            modes=["followers"], targets=["cleanup_source"], window_ids=["cleanup_window"],
            settings={"local_person_recognition": False})
        manager = ExecutionManager(self.service, pipeline._NoopBitBrowser(), worker_factory=lambda _: Worker())
        lock_acquired = False
        try:
            await manager.start(self.user["id"], task["id"])
            control = manager._runs[task["id"]]
            await asyncio.wait_for(connect_started.wait(), TIMEOUT)
            await control.network_state_lock.acquire()
            lock_acquired = True
            window = control.profile_worker_tasks["cleanup_window"]
            window.cancel()
            await asyncio.sleep(0)
            window.cancel()
            await asyncio.sleep(0)
            self.assertFalse(window.done(), "cleanup must survive cancellation before its disconnect await")
            self.assertIs(window, control.profile_worker_tasks["cleanup_window"])
            control.network_state_lock.release()
            lock_acquired = False
            await asyncio.wait_for(close_started.wait(), TIMEOUT)
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.user["id"], "cleanup_window",
                    operation_type="monitor", entity_id="must_wait")
            self.assertFalse(control.coordinator.done())
        finally:
            if lock_acquired:
                control.network_state_lock.release()
            allow_close.set()
            await manager.shutdown()
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))


if __name__ == "__main__":
    unittest.main()
