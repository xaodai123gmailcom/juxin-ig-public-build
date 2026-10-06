"""A slower build machine must not hide deadlocks or release windows early."""
import asyncio
import copy
import threading
import time
import unittest
from unittest.mock import patch

import test_nurture_scheduler as scheduler
from app.errors import ConflictError
from studio_wait import _StudioProgressBudget, StudioCompletionTimeout, wait_for_studio_completion


def sample(cursor=0, status='running', *, active=True, leased=True, **extra):
    return {'jobs': [{'id': 'job', 'profile_id': 'w1', 'cursor': cursor,
                      'total_steps': 3, 'inflight': 0, 'status': status, **extra}],
            'active_ids': ['job'] if active else [],
            'leases': [{'profile_id': 'w1', 'entity_id': 'job'}] if leased else []}


class StudioWaitBudgetR82Tests(unittest.TestCase):
    def test_real_progress_can_exceed_old_four_second_total_limit(self):
        budget = _StudioProgressBudget(0, 30, 180)
        for now, cursor in ((0, 0), (20, 1), (40, 2), (60, 3)):
            self.assertIsNone(budget.expired(now))
            budget.observe(sample(cursor), now)
        self.assertIsNone(budget.expired(80))

    def test_stuck_job_still_fails_without_progress(self):
        budget = _StudioProgressBudget(0, 30, 180)
        budget.observe(sample(), 0)
        self.assertEqual('no durable progress', budget.expired(30))

    def test_retries_timestamps_and_status_churn_do_not_renew_budget(self):
        budget = _StudioProgressBudget(0, 30, 180)
        budget.observe(sample(2), 0)
        for now in (10, 20, 29):
            budget.observe(sample(2, 'waiting_window', message=f'retry {now}', updated_at=now), now)
            budget.observe(sample(2), now)
        self.assertEqual('no durable progress', budget.expired(30))

    def test_cursor_rollback_and_repeated_completion_do_not_renew_budget(self):
        budget = _StudioProgressBudget(0, 30, 180)
        budget.observe(sample(3, 'completed', active=False, leased=False), 0)
        budget.observe(sample(1), 10)
        budget.observe(sample(3, 'completed', active=False, leased=False), 29)
        self.assertEqual('no durable progress', budget.expired(30))

    def test_cleanup_release_is_a_single_progress_milestone(self):
        budget = _StudioProgressBudget(0, 30, 180)
        budget.observe(sample(3, 'completed'), 0)
        budget.observe(sample(3, 'completed', active=False, leased=False), 20)
        self.assertIsNone(budget.expired(40))
        budget.observe(sample(3, 'completed', active=False, leased=False), 49)
        self.assertEqual('no durable progress', budget.expired(50))

    def test_continuous_progress_cannot_extend_absolute_deadline(self):
        budget = _StudioProgressBudget(0, 30, 100)
        for now in range(0, 100, 20):
            budget.observe(sample(now+1), now)
        self.assertEqual('overall deadline', budget.expired(100))
        self.assertEqual(0, budget.remaining(100))


class StudioWaitRuntimeR82Tests(unittest.IsolatedAsyncioTestCase):
    setUp = scheduler.NurtureSchedulerTests.setUp
    tearDown = scheduler.NurtureSchedulerTests.tearDown
    asyncSetUp = scheduler.NurtureSchedulerTests.asyncSetUp
    asyncTearDown = scheduler.NurtureSchedulerTests.asyncTearDown
    create = scheduler.NurtureSchedulerTests.create
    create_persisted_queue = scheduler.NurtureSchedulerTests.create_persisted_queue
    wait_for = scheduler.NurtureSchedulerTests.wait_for
    finish = scheduler.NurtureSchedulerTests.finish

    async def test_two_rounds_complete_with_close_slower_than_old_test_budget(self):
        close = self.browser.close_profile

        def slow_close(profile):
            # Two healthy rounds need >4s; all four windows still overlap.
            time.sleep(2.2)
            return close(profile)

        with patch.object(self.browser, 'close_profile', slow_close):
            await scheduler.NurtureSchedulerTests.test_four_default_windows_overlap_and_next_round_waits_for_its_own_window(self)

    async def test_completed_status_cannot_pass_while_close_and_lease_are_owned(self):
        ids = await self.create_persisted_queue(['w1'], rounds=2)
        step_counts = [self.m.get(self.owner,ident)['total_steps'] for ident in ids]
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        close = self.browser.close_profile
        calls = []

        def blocked_close(profile):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(30):
                raise RuntimeError('test did not release close')
            return close(profile)

        async def step(*_args):
            calls.append('w1')
            return {'browse': 1}

        with patch.object(self.browser, 'close_profile', blocked_close), patch('app.studio.StudioBrowser.nurture_step', step):
            waiter = asyncio.create_task(wait_for_studio_completion(self.m, self.owner, ids))
            try:
                await asyncio.wait_for(entered.wait(), 30)
                self.assertEqual('completed', self.m.get(self.owner, ids[0])['status'])
                self.assertFalse(waiter.done())
                self.assertEqual(['w1']*step_counts[0], calls)
                self.assertEqual(0, self.m.get(self.owner, ids[1])['cursor'])
                self.m._schedule_ready()
                self.assertEqual({ids[0]}, self.m.active_ids())
                with self.assertRaises(ConflictError):
                    self.s.acquire_browser_lease(self.owner, 'w1', operation_type='monitor', entity_id='other')
                release.set()
                result = await waiter
                self.assertEqual([], result['leases'])
                self.assertEqual(['w1']*sum(step_counts), calls)
                self.assertEqual(['w1', 'w1'], self.browser.closed)
            finally:
                release.set()
                await asyncio.gather(waiter, return_exceptions=True)

    async def test_failed_job_reports_the_actual_reason_without_waiting_for_timeout(self):
        ids = await self.create(['w1'])

        async def step(*_args):
            raise RuntimeError('injected nurture failure')

        with patch('app.studio.StudioBrowser.nurture_step', step):
            with self.assertRaisesRegex(AssertionError, 'injected nurture failure'):
                await wait_for_studio_completion(self.m, self.owner, ids)

    async def test_stalled_observation_reports_completed_but_unreleased_details(self):
        class AfterTwoSamples(_StudioProgressBudget):
            samples = 0

            def observe(self, snapshot, now):
                super().observe(snapshot, now)
                self.samples += 1

            def expired(self, now):
                return 'no durable progress' if self.samples >= 2 else None

        state = sample(3, 'completed', active=False)
        with patch('studio_wait._read_snapshot', return_value=copy.deepcopy(state)), patch('studio_wait._StudioProgressBudget', AfterTwoSamples):
            with self.assertRaises(StudioCompletionTimeout) as caught:
                await wait_for_studio_completion(self.m, self.owner, ['job'])
        message = str(caught.exception)
        for detail in ('no durable progress', '"status": "completed"', '"profile_id": "w1"', '"leases": ['):
            self.assertIn(detail, message)

    async def test_missing_job_cannot_be_treated_as_completed(self):
        with self.assertRaisesRegex(AssertionError, 'job missing'):
            await wait_for_studio_completion(self.m, self.owner, ['missing'])

    async def test_repeated_cancellation_joins_inflight_read_without_touching_jobs(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked_read(*_args):
            entered.set()
            try:
                if not release.wait(30):
                    raise RuntimeError('test did not release read')
                return sample(3, 'completed', active=False, leased=False)
            finally:
                finished.set()

        with patch('studio_wait._read_snapshot', blocked_read):
            waiter = asyncio.create_task(wait_for_studio_completion(self.m, self.owner, ['job']))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 30))
                waiter.cancel()
                await asyncio.sleep(0)
                waiter.cancel()
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertTrue(finished.is_set())
                self.assertFalse(self.m.active_ids())
            finally:
                release.set()
                await asyncio.gather(waiter, return_exceptions=True)

    async def test_invalid_deadlines_fail_before_starting_jobs(self):
        for value in (0, -1, float('inf'), float('nan'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await wait_for_studio_completion(self.m, self.owner, ['job'], idle_timeout=value)
        self.assertFalse(self.m.active_ids())


if __name__ == '__main__':
    unittest.main()
