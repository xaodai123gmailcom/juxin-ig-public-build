"""The build watchdog tolerates useful work, but cannot hide a stalled collector."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.async_cleanup import finish_owned
from completion_wait import (
    CollectionCompletionTimeout, _ProgressBudget,
    wait_for_collection_completion, wait_for_collection_operation,
)


def snapshot(*, total=164, recorded=0, deduped=0, status="running", **extra):
    return {"targets": [{
        "id": "source", "status": status,
        "candidates": {"total": total, "recorded": recorded, "deduped": deduped,
                       "pending": total - recorded - deduped},
        **extra,
    }]}


class CompletionBudgetR53Tests(unittest.TestCase):
    def test_healthy_slow_drain_can_exceed_old_thirty_second_wall_budget(self):
        budget = _ProgressBudget(0, idle_timeout=30, overall_timeout=180)
        budget.observe(snapshot(), 0)
        for now, recorded in ((20, 40), (40, 80), (60, 120), (80, 164)):
            self.assertIsNone(budget.expired(now))
            budget.observe(snapshot(recorded=recorded), now)
        self.assertIsNone(budget.expired(100))
        self.assertEqual(10, budget.remaining(100))

    def test_deadlock_expires_without_any_durable_advancement(self):
        budget = _ProgressBudget(0, 30, 180)
        budget.observe(snapshot(), 0)
        self.assertIsNone(budget.expired(29))
        self.assertEqual("no durable progress", budget.expired(30))

    def test_retry_heartbeat_stage_and_timestamp_churn_do_not_extend_deadline(self):
        budget = _ProgressBudget(0, 30, 180)
        budget.observe(snapshot(recorded=5), 0)
        for now in (10, 20, 29):
            budget.observe(snapshot(recorded=5, status="waiting_network",
                                    stage=f"retry-{now}", last_success_at=now,
                                    updated_at=now, retry_count=now), now)
        self.assertEqual("no durable progress", budget.expired(30))

    def test_counter_rollback_and_terminal_reclassification_are_not_new_progress(self):
        budget = _ProgressBudget(0, 30, 180)
        budget.observe(snapshot(recorded=20), 0)
        budget.observe(snapshot(total=100, recorded=1), 10)
        budget.observe(snapshot(recorded=10, deduped=10), 29)
        self.assertEqual("no durable progress", budget.expired(30))

    def test_perpetual_durable_progress_still_hits_absolute_cap(self):
        budget = _ProgressBudget(0, 30, 100)
        for now in range(0, 100, 20):
            budget.observe(snapshot(total=now + 1), now)
        self.assertEqual("overall deadline", budget.expired(100))
        self.assertEqual(0, budget.remaining(100))

    def test_each_completed_target_renews_budget_only_once(self):
        budget = _ProgressBudget(0, 30, 180)
        budget.observe(snapshot(total=0, status="completed"), 20)
        self.assertIsNone(budget.expired(40))
        budget.observe(snapshot(total=0, status="running"), 40)
        budget.observe(snapshot(total=0, status="completed"), 49)
        self.assertEqual("no durable progress", budget.expired(50))


class _Service:
    def __init__(self):
        self.stats = {"total": 164, "pending": 22, "recorded": 142, "deduped": 0}
        self.status = "running"
        self.on_read = None

    def get_task(self, owner, task_id):
        if self.on_read:
            self.on_read()
        return {"status": self.status, "targets": [{
            "id": "source", "username": "short_source", "status": self.status,
            "current_stage": "screening_accounts", "last_error": "retry fixture",
        }]}

    def task_mode_candidate_stats(self, *_args):
        return copy.deepcopy(self.stats)


class _Manager:
    def __init__(self):
        self.release = asyncio.Event()
        self.wait_entered = asyncio.Event()
        self.wait_finished = asyncio.Event()
        self.coordinator = asyncio.create_task(self.run())

    async def run(self):
        await self.release.wait()
        return "manager completed"

    async def wait(self, _task_id):
        self.wait_entered.set()
        try:
            return await asyncio.shield(self.coordinator)
        finally:
            self.wait_finished.set()

    async def runtime_diagnostics(self, *_args, **kwargs):
        assert "known_status" in kwargs
        return {"network_waiters": [{"reason": "controlled_no_progress"}],
                "profile_states": [{"current_target": "short_source"}]}


class CompletionWaitLifecycleR53Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = _Manager()
        self.service = _Service()

    async def asyncTearDown(self):
        self.manager.release.set()
        await asyncio.gather(self.manager.coordinator, return_exceptions=True)

    def assert_observers_finished(self):
        self.assertFalse([
            task for task in asyncio.all_tasks()
            if task.get_name() in {"collection-test-completion", "collection-test-durable-observer"}
        ])

    async def test_returns_real_manager_result_and_joins_its_observer(self):
        self.manager.release.set()
        result = await wait_for_collection_completion(
            self.manager, self.service, "owner", "task",
        )
        self.assertEqual("manager completed", result)
        self.assert_observers_finished()

    async def test_pending_rows_do_not_become_completion_and_timeout_reports_counts(self):
        # Even a stale completed status cannot stand in for the coordinator.
        self.service.status = "completed"

        class AfterTwoSnapshots(_ProgressBudget):
            samples = 0

            def observe(self, observed, now):
                super().observe(observed, now)
                self.samples += 1

            def expired(self, now):
                # The policy itself is exercised above with explicit timestamps.
                # This lifecycle case must not depend on Windows thread startup
                # finishing within a tiny wall-clock timeout.
                return "no durable progress" if self.samples >= 2 else None

        for pending in (22, 0):
            with self.subTest(pending=pending):
                self.service.stats.update(pending=pending, recorded=164 - pending)
                with patch("completion_wait._ProgressBudget", AfterTwoSnapshots):
                    with self.assertRaises(CollectionCompletionTimeout) as caught:
                        await asyncio.wait_for(wait_for_collection_completion(
                            self.manager, self.service, "owner", "task", poll_interval=.001,
                        ), 10)
                message = str(caught.exception)
                self.assertIn("no durable progress", message)
                self.assertIn(f'"pending": {pending}', message)
                self.assertIn(f'"recorded": {164 - pending}', message)
                self.assertIn("controlled_no_progress", message)
                self.assertFalse(self.manager.coordinator.done())
                self.assert_observers_finished()

    async def test_early_manager_failure_is_propagated_without_becoming_timeout(self):
        self.manager.coordinator.cancel()
        await asyncio.gather(self.manager.coordinator, return_exceptions=True)

        async def broken():
            raise RuntimeError("actual coordinator failure")

        self.manager.coordinator = asyncio.create_task(broken())
        with self.assertRaisesRegex(RuntimeError, "actual coordinator failure"):
            await wait_for_collection_completion(self.manager, self.service, "owner", "task")
        self.assert_observers_finished()

    async def test_caller_cancellation_leaves_coordinator_owned_by_fixture(self):
        observer = asyncio.create_task(wait_for_collection_completion(
            self.manager, self.service, "owner", "task",
        ))
        await self.manager.wait_entered.wait()
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertFalse(self.manager.coordinator.done())
        self.assert_observers_finished()

    async def test_bad_observation_propagates_and_does_not_cancel_manager(self):
        def broken():
            raise RuntimeError("database read failed")

        self.service.on_read = broken
        with self.assertRaisesRegex(RuntimeError, "database read failed"):
            await wait_for_collection_completion(self.manager, self.service, "owner", "task")
        self.assertFalse(self.manager.coordinator.done())
        self.assert_observers_finished()

    async def test_completion_and_repeated_cancellation_join_the_inflight_database_reader(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                manager = _Manager()
                service = _Service()
                entered = threading.Event()
                release = threading.Event()
                closed = threading.Event()

                def blocked_read():
                    entered.set()
                    try:
                        if not release.wait(30):
                            raise RuntimeError("test failed to release its database reader")
                    finally:
                        closed.set()

                service.on_read = blocked_read
                observer = asyncio.create_task(wait_for_collection_completion(
                    manager, service, "owner", "task",
                ))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                    if cancel:
                        observer.cancel()
                    else:
                        manager.release.set()
                    # This event proves completion/cancellation reached the owned
                    # observer; no timing guess is needed to inspect the barrier.
                    await asyncio.wait_for(manager.wait_finished.wait(), 10)
                    self.assertFalse(observer.done())
                    if cancel:
                        observer.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(observer.done())
                    self.assertFalse(closed.is_set())
                    release.set()
                    if cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await observer
                        self.assertFalse(manager.coordinator.done())
                    else:
                        self.assertEqual("manager completed", await observer)
                    self.assertTrue(closed.is_set())
                    self.assert_observers_finished()
                finally:
                    release.set()
                    manager.release.set()
                    await asyncio.gather(observer, manager.coordinator, return_exceptions=True)

    async def test_direct_slow_drain_renews_only_on_progress_and_waits_for_cleanup(self):
        # Advance the watchdog's clock, not the event loop clock: this tests a
        # 40-second drain without making Windows scheduling speed an assertion.
        clock = [0.0]
        budgets = []
        observed = asyncio.Queue()
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        calls = []
        expected = {"recorded": 164, "pending": 0}

        class ControlledClockBudget(_ProgressBudget):
            def __init__(self, now, idle_timeout, overall_timeout):
                super().__init__(clock[0], idle_timeout, overall_timeout)
                budgets.append(self)

            def observe(self, value, now):
                super().observe(value, clock[0])
                observed.put_nowait(value)

            def expired(self, now):
                return super().expired(clock[0])

            def remaining(self, now):
                return super().remaining(clock[0])

        async def await_terminal_count(count):
            while True:
                value = await observed.get()
                if value["targets"][0]["candidates"]["recorded"] == count:
                    return

        async def operation():
            calls.append("started")
            await await_terminal_count(142)
            for count in (148, 154, 160, 164):
                clock[0] += 10
                self.service.stats.update(recorded=count, pending=164 - count)
                await await_terminal_count(count)
            # Even a completed status and an empty durable queue cannot stand
            # in for the operation's still-owned child cleanup.
            self.service.status = "completed"
            cleanup_started.set()
            await release_cleanup.wait()
            calls.append("cleaned")
            return expected

        with patch("completion_wait._ProgressBudget", ControlledClockBudget):
            waiter = asyncio.create_task(wait_for_collection_operation(
                operation, self.manager, self.service, "owner", "task",
                idle_timeout=15, overall_timeout=120, poll_interval=.001,
            ))
            try:
                await asyncio.wait_for(cleanup_started.wait(), 10)
                self.assertEqual(40, clock[0])
                self.assertGreater(clock[0], budgets[0].idle_timeout)
                self.assertEqual(164, budgets[0].high_water[("source", "terminal")])
                self.assertFalse(waiter.done())
                self.assertEqual(["started"], calls)
                release_cleanup.set()
                self.assertIs(expected, await waiter)
                self.assertEqual(["started", "cleaned"], calls)
                self.assert_observers_finished()
            finally:
                release_cleanup.set()
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def test_direct_stalled_queue_times_out_and_joins_child_cleanup(self):
        # Drive expiration from observations rather than requiring a thread to
        # start within a few milliseconds. Policy timing is tested above.
        class AfterTwoSnapshots(_ProgressBudget):
            samples = 0

            def observe(self, observed, now):
                super().observe(observed, now)
                self.samples += 1

            def expired(self, now):
                return "no durable progress" if self.samples >= 2 else None

        for pending in (22, 0):
            with self.subTest(pending=pending):
                self.service.status = "completed"
                self.service.stats.update(pending=pending, recorded=164 - pending)
                cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
                calls = []

                async def close_child():
                    cleanup_started.set()
                    await release_cleanup.wait()
                    calls.append("cleaned")

                async def operation():
                    calls.append("started")
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await finish_owned(close_child())

                with patch("completion_wait._ProgressBudget", AfterTwoSnapshots):
                    waiter = asyncio.create_task(wait_for_collection_operation(
                        operation, self.manager, self.service, "owner", "task",
                        idle_timeout=15, overall_timeout=120, poll_interval=.001,
                    ))
                    try:
                        await asyncio.wait_for(cleanup_started.wait(), 10)
                        self.assertFalse(waiter.done(), "timeout must join owned cleanup")
                        self.assertEqual(["started"], calls)
                        release_cleanup.set()
                        with self.assertRaises(CollectionCompletionTimeout) as caught:
                            await waiter
                        self.assertIn("no durable progress", str(caught.exception))
                        self.assertIn(f'"pending": {pending}', str(caught.exception))
                        self.assertIn("controlled_no_progress", str(caught.exception))
                        self.assertEqual(["started", "cleaned"], calls)
                        self.assert_observers_finished()
                    finally:
                        release_cleanup.set()
                        if not waiter.done():
                            waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)

    async def test_direct_repeated_cancellation_keeps_child_owned_until_cleanup(self):
        entered, cleanup_started, release_cleanup = [asyncio.Event() for _ in range(3)]
        calls = []

        async def close_child():
            cleanup_started.set()
            await release_cleanup.wait()
            calls.append("cleaned")

        async def operation():
            calls.append("started")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await finish_owned(close_child())

        waiter = asyncio.create_task(wait_for_collection_operation(
            operation, self.manager, self.service, "owner", "task",
        ))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            waiter.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 10)
            self.assertFalse(waiter.done())
            waiter.cancel()
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.assertEqual(["started"], calls)
            release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(["started", "cleaned"], calls)
            self.assert_observers_finished()
        finally:
            release_cleanup.set()
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_direct_operation_failure_is_not_replaced_with_timeout(self):
        calls = []

        async def operation():
            calls.append("started")
            raise RuntimeError("direct pipeline failure")

        with self.assertRaisesRegex(RuntimeError, "direct pipeline failure"):
            await wait_for_collection_operation(
                operation, self.manager, self.service, "owner", "task",
            )
        self.assertEqual(["started"], calls)
        self.assert_observers_finished()

    async def test_invalid_budgets_are_rejected_before_observers_start(self):
        for value in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await wait_for_collection_completion(
                    self.manager, self.service, "owner", "task", idle_timeout=value,
                )
        self.assert_observers_finished()


if __name__ == "__main__":
    unittest.main()
