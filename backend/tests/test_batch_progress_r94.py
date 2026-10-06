"""Adversarial scheduler boundaries in the production batch collector."""
import asyncio
import inspect
import unittest
from unittest.mock import patch

import test_single_handoff_r94 as batch


class BatchProgressR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = batch.BatchCollectionR59Tests.asyncSetUp
    asyncTearDown = batch.BatchCollectionR59Tests.asyncTearDown
    _task_and_control = batch.BatchCollectionR59Tests._task_and_control
    pipeline = batch.BatchCollectionR59Tests.pipeline
    until = batch.BatchCollectionR59Tests.until
    stats = batch.BatchCollectionR59Tests.stats
    cleanup = batch.BatchCollectionR59Tests.cleanup

    async def test_two_children_finish_when_sibling_changes_queue_before_waiter_runs(self):
        await self._delayed_waiter_case(2)

    async def test_three_children_finish_when_sibling_changes_queue_before_waiter_runs(self):
        await self._delayed_waiter_case(3)

    async def _delayed_waiter_case(self, children):
        # Delay scheduling an idle consumer's notification waiter. This is a
        # legal event-loop order: the last busy child can commit and finish before
        # a just-created Event.wait coroutine receives its first time slice.
        # All database claims, writes and pipeline decisions remain real.
        waiter_created, allow_waiter = asyncio.Event(), asyncio.Event()
        original_create = asyncio.create_task
        delayed = False

        async def delayed_wait(coro):
            entered = False
            try:
                await allow_waiter.wait()
                entered = True
                return await coro
            finally:
                if not entered:
                    coro.close()

        def schedule(coro, *args, **kwargs):
            nonlocal delayed
            caller = inspect.currentframe().f_back
            if (not delayed and caller.f_code.co_name == 'consume'
                    and getattr(coro, '__qualname__', '') == 'Event.wait'):
                delayed = True
                waiter_created.set()
                return original_create(delayed_wait(coro), *args, **kwargs)
            return original_create(coro, *args, **kwargs)

        with patch('asyncio.create_task', schedule):
            task, target, control, state, source, job = self.pipeline(
                children, names=[f'last.account.{children}'])
            try:
                await asyncio.wait_for(waiter_created.wait(), 5)
                await self.until(lambda: not state.source_running and state.source_calls == 1, job)
                state.gates[state.candidates[0]].set()
                await self.until(lambda: any(c.disconnect_calls for c in state.children), job)
                self.assertEqual(0, self.stats(task, target)['pending'])
                allow_waiter.set()
                done, _ = await asyncio.wait({job}, timeout=2)
                self.assertIn(job, done, 'all rows committed but pipeline stranded an idle child')
                self.assertEqual(0, job.result()['pending'])
                self.assertEqual([('child', state.candidates[0])], state.screened)
                self.assertTrue(all(c.disconnect_calls == 1 for c in state.children))
            finally:
                allow_waiter.set()
                await self.cleanup(state, job)
