"""Slow real SQLite recovery and bounded direct-operation observation."""
import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_cross_recovery_r25 as cross
import test_parallel_relation_pipeline as pipeline
import test_pipeline_r29 as isolation
from test_completion_wait_r53 import _Service
from completion_wait import (
    CollectionCompletionTimeout, _ProgressBudget, wait_for_collection_operation,
)
from app.playwright_worker import WorkerExecutionError


class SlowRecoveryR91Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control
    _checkpoint = isolation.PipelineR29Tests._checkpoint
    _stats = isolation.PipelineR29Tests._stats

    async def _slow_commits(self, operation, *, delay, count):
        original = self.service.upsert_checkpoint
        lock = threading.Lock()
        delayed = 0

        def slow_commit(*args, **kwargs):
            nonlocal delayed
            with lock:
                should_delay = threading.current_thread() is not threading.main_thread() and delayed < count
                if should_delay:
                    delayed += 1
            if should_delay:
                time.sleep(delay)
            return original(*args, **kwargs)

        with patch.object(self.service, "upsert_checkpoint", slow_commit):
            await operation(self)
        self.assertEqual(count, delayed)

    async def test_slow_commits_preserve_candidates_and_completed_source(self):
        await self._slow_commits(
            cross.CrossRecoveryR25Tests.test_repeated_retained_candidate_failures_never_reopen_completed_source,
            delay=.4, count=6)

    async def test_slow_isolated_candidate_retries_preserve_healthy_tail(self):
        await self._slow_commits(
            isolation.PipelineR29Tests.test_one_bad_child_does_not_block_healthy_candidates_or_lose_claim,
            delay=.5, count=7)

    async def test_slow_child_claim_does_not_trip_nested_source_fixture_deadline(self):
        original = self.service.claim_workbench_identity
        delayed = threading.Event()

        def slow_claim(*args, **kwargs):
            if not delayed.is_set():
                delayed.set()
                time.sleep(2.2)
            return original(*args, **kwargs)

        with patch.object(self.service, "claim_workbench_identity", slow_claim):
            await cross.CrossRecoveryR25Tests.test_repeated_retained_candidate_failures_never_reopen_completed_source(self)
        self.assertTrue(delayed.is_set())


class _AfterTwoSnapshots(_ProgressBudget):
    samples = 0

    def observe(self, snapshot, now):
        super().observe(snapshot, now)
        self.samples += 1

    def expired(self, now):
        # Exercise cancellation deterministically; the actual clock policy is
        # covered independently by CompletionBudgetR53Tests.
        return "no durable progress" if self.samples >= 2 else None


class OperationWaitR91Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = _Service()
        self.manager = type("Diagnostics", (), {
            "runtime_diagnostics": AsyncMock(return_value={"network_waiters": [], "profile_states": []}),
        })()

    async def observe(self, factory, **kwargs):
        return await wait_for_collection_operation(
            factory, self.manager, self.service, "owner", "task", **kwargs)

    def assert_no_observers(self):
        self.assertFalse([task for task in asyncio.all_tasks() if task.get_name() in {
            "collection-test-completion", "collection-test-durable-observer",
        }])

    async def test_returns_exact_result_or_original_failure(self):
        result = {"pending": 0, "discovery_complete": True}

        async def success():
            return result

        self.assertIs(result, await self.observe(success))
        error = WorkerExecutionError("unread", reason="instagram_profile_not_ready")
        error.details["candidate_username"] = "still.pending"

        async def failure():
            raise error

        with self.assertRaises(WorkerExecutionError) as caught:
            await self.observe(failure)
        self.assertIs(error, caught.exception)
        self.assertEqual("still.pending", caught.exception.details["candidate_username"])
        self.assert_no_observers()

    async def test_zero_pending_and_completed_status_cannot_pass_a_stuck_operation(self):
        self.service.status = "completed"
        self.service.stats.update(pending=0, recorded=164)
        cleaned = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("completion_wait._ProgressBudget", _AfterTwoSnapshots):
            with self.assertRaisesRegex(CollectionCompletionTimeout, "no durable progress") as caught:
                await asyncio.wait_for(self.observe(blocked, poll_interval=.001), 10)
        self.assertIn('"pending": 0', str(caught.exception))
        self.assertTrue(cleaned.is_set())
        self.assert_no_observers()

    async def test_timeout_joins_owned_cleanup_even_when_observer_is_cancelled_again(self):
        cleanup_entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release.wait()
                cleaned.set()

        with patch("completion_wait._ProgressBudget", _AfterTwoSnapshots):
            observer = asyncio.create_task(self.observe(blocked, poll_interval=.001))
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 10)
                self.assertFalse(observer.done())
                observer.cancel()
                await asyncio.sleep(0)
                self.assertFalse(observer.done())
                self.assertFalse(cleaned.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await observer
                self.assertTrue(cleaned.is_set())
                self.assert_no_observers()
            finally:
                release.set()
                await asyncio.gather(observer, return_exceptions=True)

    async def test_invalid_budget_does_not_create_an_operation(self):
        operation = AsyncMock()
        with self.assertRaises(ValueError):
            await self.observe(operation, idle_timeout=0)
        operation.assert_not_called()
        self.assert_no_observers()


if __name__ == "__main__":
    unittest.main()
