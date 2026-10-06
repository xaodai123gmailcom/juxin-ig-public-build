"""Bounded streaming work and cancellation cleanup for the relation pipeline."""
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
from completion_wait import wait_for_collection_operation
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError


class PipelineR30Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    async def test_streaming_batches_do_not_repeat_full_spool_reconciliation(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        state.allow_screen.set()
        finished = {f"stream_{index}": asyncio.Event() for index in range(12)}
        original_finish = self.service.finish_task_mode_candidate
        original_reconcile = self.service.reconcile_task_mode_candidates
        reconciliations = []
        loop = asyncio.get_running_loop()

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            loop.call_soon_threadsafe(finished[args[4]].set)
            return result

        def reconcile(*args, **kwargs):
            reconciliations.append((args[2], args[3]))
            return original_reconcile(*args, **kwargs)

        self.service.finish_task_mode_candidate = finish
        self.service.reconcile_task_mode_candidates = reconcile

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, _target, *, candidate_sink, progress_sink, **_kwargs):
                self.state.source_running = True
                try:
                    for username, committed in finished.items():
                        await candidate_sink([username])
                        await asyncio.wait_for(committed.wait(), 3)
                        # Let the now-empty consumer reach its wait before the next
                        # durable batch, reproducing normal slow visible scrolling.
                        await asyncio.sleep(0.003)
                    return CollectionOutcome("followers", [], source_total=len(finished))
                finally:
                    self.state.source_running = False

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        parent = Parent(state, child=pipeline._ScreeningChild(state))
        result = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], None,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )
        self.assertEqual(0, result["pending"])
        self.assertEqual(12, result["recorded"])
        self.assertEqual(12, len(state.screened))
        self.assertEqual(2, len(reconciliations),
                         "full reconciliation belongs at source entry and natural completion, not each consumer wake")

    async def test_failed_source_joins_idle_consumers_event_waiters_across_retries(self):
        task, target, control = self._task_and_control(1)

        class CountedEvent(asyncio.Event):
            waiters = 0
            ready = None

            async def wait(self):
                self.waiters += 1
                if self.waiters >= 2:
                    self.ready.set()
                try:
                    return await super().wait()
                finally:
                    self.waiters -= 1

        stop = CountedEvent()
        stop.ready = asyncio.Event()
        control.stop_event = stop
        state = pipeline._PipelineState()

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, *_args, **_kwargs):
                await asyncio.wait_for(stop.ready.wait(), 3)
                raise WorkerExecutionError("source list interrupted", reason="instagram_followers_list_incomplete")

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        for _ in range(3):
            stop.ready.clear()
            parent = Parent(state, child=pipeline._ScreeningChild(state))
            with self.assertRaises(WorkerExecutionError):
                await wait_for_collection_operation(
                    lambda: manager._execute_candidate_spooled_mode(
                        control, parent, target,
                        "followers", task["settings"], None,
                    ),
                    manager, self.service, control.owner_user_id, control.task_id,
                )
            self.assertEqual(0, stop.waiters, "retry must not leave old stop waits behind")
            self.assertEqual(1, parent.child.disconnect_calls)

    async def test_bad_candidates_reconcile_their_alias_groups_and_keep_one_final_full_pass(self):
        task, target, control = self._task_and_control(1)
        names = ["bad_first", "healthy_middle", "bad_last"]
        self.service.append_task_mode_candidates(
            self.user["id"], task["id"], target["id"], "followers", names)
        state = pipeline._PipelineState()
        original = self.service.reconcile_task_mode_candidates
        reconciliations = []

        def reconcile(*args, **kwargs):
            reconciliations.append(kwargs.get("usernames"))
            return original(*args, **kwargs)

        self.service.reconcile_task_mode_candidates = reconcile

        class Parent(pipeline._RelationParent):
            async def screen(self, username):
                if username.startswith("bad_"):
                    raise PlaywrightWorker._page_recovery_exhausted(
                        WorkerExecutionError("still loading", reason="instagram_profile_not_ready"), username)
                await super().screen(username)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError):
            await manager._execute_candidate_spooled_mode(
                control, Parent(state), target, "followers", task["settings"],
                {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}})
        self.assertEqual([None, ["bad_first"], ["bad_last"], None], reconciliations)
        stats = self.service.task_mode_candidate_stats(self.user["id"], task["id"], target["id"], "followers")
        self.assertEqual(2, stats["pending"])
        self.assertEqual(1, stats["recorded"])
        self.assertEqual([("parent", "healthy_middle")], state.screened)

    async def _assert_source_return_survives_cancel(self, source_total, cancellations):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        state.allow_screen.set()
        consumer_write_started = asyncio.Event()
        source_returned = asyncio.Event()
        release_write = threading.Event()
        original = self.service.upsert_checkpoint
        loop = asyncio.get_running_loop()

        def delayed(*args, **kwargs):
            if kwargs.get("stage") == "screening_accounts" and not kwargs.get("cursor", {}).get("candidate_spool_complete"):
                loop.call_soon_threadsafe(consumer_write_started.set)
                if not release_write.wait(5):
                    raise RuntimeError("test did not release consumer checkpoint")
            return original(*args, **kwargs)

        self.service.upsert_checkpoint = delayed

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, *_args, **kwargs):
                await kwargs["candidate_sink"]([f"committed_{index}" for index in range(12)])
                await consumer_write_started.wait()
                source_returned.set()
                return CollectionOutcome("followers", [], source_total=source_total)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=pipeline._ScreeningChild(state)), target,
            "followers", task["settings"], None))
        try:
            await asyncio.wait_for(source_returned.wait(), 3)
            control.stop_event.set()
            for _ in range(cancellations):
                execution.cancel()
                await asyncio.sleep(0)
            self.assertFalse(execution.done(), "the older writer must settle before the owner exits")
        finally:
            release_write.set()
            await asyncio.gather(execution, return_exceptions=True)
            self.service.upsert_checkpoint = original
        checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
        self.assertTrue(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
        if source_total is not None:
            self.assertEqual(source_total, checkpoint["counters"]["source_total"])

    async def test_one_cancel_while_returned_source_total_waits_for_consumer_lock_preserves_end(self):
        await self._assert_source_return_survives_cancel(source_total=12, cancellations=1)

    async def test_repeated_cancel_while_completed_source_waits_for_consumer_lock_preserves_end(self):
        await self._assert_source_return_survives_cancel(source_total=None, cancellations=2)

    async def test_cancel_during_compatibility_batch_flush_does_not_mark_partial_source_complete(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        first_batch = asyncio.Event()
        original = self.service.append_task_mode_candidates
        loop = asyncio.get_running_loop()

        def appended(*args, **kwargs):
            result = original(*args, **kwargs)
            loop.call_soon_threadsafe(first_batch.set)
            return result

        self.service.append_task_mode_candidates = appended

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, *_args, **_kwargs):
                return CollectionOutcome("followers", [f"compat_{index}" for index in range(150)], source_total=150)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=pipeline._ScreeningChild(state, block=True)),
            target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(first_batch.wait(), 3)
            control.stop_event.set()
            execution.cancel()
        finally:
            await asyncio.gather(execution, return_exceptions=True)
            self.service.append_task_mode_candidates = original
        checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
        stats = self.service.task_mode_candidate_stats(self.user["id"], task["id"], target["id"], "followers")
        self.assertEqual(100, stats["total"])
        self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))


if __name__ == "__main__":
    unittest.main()
