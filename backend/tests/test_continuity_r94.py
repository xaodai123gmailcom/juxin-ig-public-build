"""Independent page cancellation must not turn into whole-task cancellation."""
import asyncio
import unittest

import test_single_handoff_r94 as fixture
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.service import CoreService
from app.database import Database


class ContinuityR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.BatchCollectionR59Tests.asyncSetUp
    asyncTearDown = fixture.BatchCollectionR59Tests.asyncTearDown
    _task_and_control = fixture.BatchCollectionR59Tests._task_and_control
    until = fixture.BatchCollectionR59Tests.until
    stats = fixture.BatchCollectionR59Tests.stats

    async def test_cancelled_profile_future_keeps_username_owned_until_cleanup(self):
        await self._cancelled_read(3)

    async def test_cancelled_profile_future_with_two_children_resumes_only_unfinished(self):
        await self._cancelled_read(2)

    async def _cancelled_read(self, count):
        task, target, control = self._task_and_control(count)
        source_finished, fail_read, closing, release_close = [asyncio.Event() for _ in range(4)]
        names = ['cancelled.person'] + [f'healthy.{n}' for n in range(6)]
        attempts, closed = [], []
        class Child:
            failed = False
            async def read_visible_profile(self, username, **kwargs):
                attempts.append(username)
                if username == names[0]:
                    await fail_read.wait()
                    self.failed = True
                    # A browser operation's Future can be cancelled although the
                    # owning collection Task was never cancelled by the user.
                    reply = asyncio.get_running_loop().create_future()
                    reply.cancel()
                    return await reply
                return dict(username=username, visibility='private', posts=4, followers=10, following=10)
            async def disconnect(self):
                if self.failed:
                    closing.set()
                    await release_close.wait()
                closed.append(self)
        class Parent:
            supports_candidate_batch_sink = True
            supports_parallel_screening_tab = True
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def create_parallel_screening_worker(self): return Child()
            async def collect_followers(self, target, *, candidate_sink, **kwargs):
                await candidate_sink(names)
                source_finished.set()
                return CollectionOutcome('followers', [], source_total=len(names))
        settings = {**task['settings'], 'location_enabled': False, 'gpt_enabled': False}
        manager = ExecutionManager(self.service, fixture._NoopBitBrowser())
        job = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(), target, 'followers', settings, None))
        try:
            await asyncio.wait_for(source_finished.wait(), 5)
            fail_read.set()
            await asyncio.wait_for(closing.wait(), 5)
            await self.until(lambda: attempts.count(names[0]) > 1 or len(closed) >= count - 1, job)
            self.assertEqual(1, attempts.count(names[0]), 'another child reclaimed a still-closing profile')
            self.assertEqual(6, self.stats(task, target)['recorded'])
            self.assertFalse(job.done())
            release_close.set()
            with self.assertRaises(WorkerExecutionError) as raised:
                await asyncio.wait_for(job, 5)
            self.assertEqual('browser_window_surface_unstable', raised.exception.details['original_reason'])
            self.assertTrue(raised.exception.details['source_discovery_complete'])
            self.assertEqual(1, self.stats(task, target)['pending'])
            self.assertEqual(count, len(closed))
            self.service = CoreService(Database(self.service.database.path))
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            resumed = []
            class Recovered(Child):
                async def read_visible_profile(self, username, **kwargs):
                    resumed.append(username)
                    return dict(username=username, visibility='private', posts=4, followers=10, following=10)
            class RecoveredSource(Parent):
                async def create_parallel_screening_worker(self): return Recovered()
                async def collect_followers(self, *args, **kwargs):
                    raise AssertionError('completed source must not be read again')
            manager = ExecutionManager(self.service, fixture._NoopBitBrowser())
            result = await asyncio.wait_for(manager._execute_candidate_spooled_mode(
                control, RecoveredSource(), target, 'followers', settings, checkpoint), 5)
            self.assertEqual([names[0]], resumed)
            self.assertEqual((7, 0), (result['recorded'], result['pending']))
        finally:
            fail_read.set(); release_close.set(); control.pause_event.set()
            if not job.done(): job.cancel()
            await asyncio.gather(job, return_exceptions=True)
