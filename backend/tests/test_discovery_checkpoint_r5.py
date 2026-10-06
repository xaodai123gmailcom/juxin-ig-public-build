"""Checkpoint coalescing never delays recognition-time identity reservations."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from app.playwright_worker import CollectionOutcome
import test_parallel_relation_pipeline as fixture
from test_parallel_relation_pipeline import (
    _NoopBitBrowser, _PipelineManager,
    _PipelineState, _RelationParent, _ScreeningChild,
)


class DiscoveryCheckpointR5Tests(unittest.IsolatedAsyncioTestCase):
    _task_and_control = fixture.ParallelRelationPipelineTests._task_and_control

    async def asyncSetUp(self):
        await fixture.ParallelRelationPipelineTests.asyncSetUp(self)
        self.pipelines = []

    async def asyncTearDown(self):
        for job, control, state in self.pipelines:
            control.pause_event.set()
            job.cancel()
        await asyncio.gather(*(job for job, _, _ in self.pipelines), return_exceptions=True)
        await fixture.ParallelRelationPipelineTests.asyncTearDown(self)

    async def launch(self):
        task, target, control = self._task_and_control(1)
        state = _PipelineState('checkpoint')
        ready = asyncio.Event()

        class Source(_RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = 'r59-batch'

            async def create_parallel_screening_worker(self):
                return _ScreeningChild(state)

            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                # Hold the source open while tests invoke its real manager callback.
                # Children remain idle or blocked on profile completion, keeping
                # source checkpoints separate from terminal screening checkpoints.
                self.sink = candidate_sink
                ready.set()
                await asyncio.Event().wait()
                return CollectionOutcome('followers', [])

        source = Source(state)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        job = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, source, target, 'followers', task['settings'], None))
        self.pipelines.append((job, control, state))
        await asyncio.wait_for(ready.wait(), 10)
        return task, target, control, source

    def checkpoint(self, task, target):
        return self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')

    def stats(self, task, target):
        return self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')

    async def test_100_names_commit_individually_but_checkpoint_once_and_repeats_add_none(self):
        task, target, control, source = await self.launch()
        names = [f'checkpoint.person{i:03d}' for i in range(100)]
        original = self.service.discover_task_mode_candidate
        committed = []

        def discover(*args, **kwargs):
            result = original(*args, **kwargs)
            # A separate connection sees each reservation before the callback is
            # allowed to proceed to the next recognized identity.
            self.assertTrue(self.service.check_global_dedupe(args[-1])['seen'])
            committed.append(args[-1])
            return result

        with patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover), \
             patch.object(self.service, 'upsert_checkpoint', wraps=self.service.upsert_checkpoint) as save:
            first = await asyncio.wait_for(source.sink(names), 10)
            self.assertEqual(names, committed)
            self.assertEqual(100, first['total'])
            self.assertEqual(1, save.call_count)
            self.assertEqual(100, self.checkpoint(task, target)['counters']['discovered'])
            repeated = await asyncio.wait_for(source.sink(names), 10)
            self.assertEqual(100, repeated['total'])
            self.assertEqual(200, len(committed))
            self.assertEqual(1, save.call_count)
            self.assertEqual(100, self.stats(task, target)['pending'])

    async def test_pause_between_identities_flushes_committed_prefix_before_waiting(self):
        task, target, control, source = await self.launch()
        loop = asyncio.get_running_loop()
        prefix_saved = asyncio.Event()
        original_discover = self.service.discover_task_mode_candidate
        original_save = self.service.upsert_checkpoint
        names = [f'pause.person{i}' for i in range(5)]

        def discover(*args, **kwargs):
            result = original_discover(*args, **kwargs)
            if result['total'] == 1:
                loop.call_soon_threadsafe(control.pause_event.clear)
            return result

        def save(*args, **kwargs):
            result = original_save(*args, **kwargs)
            if kwargs['counters'].get('discovered') == 1:
                loop.call_soon_threadsafe(prefix_saved.set)
            return result

        with patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover), \
             patch.object(self.service, 'upsert_checkpoint', side_effect=save) as saves:
            call = asyncio.create_task(source.sink(names))
            try:
                await asyncio.wait_for(prefix_saved.wait(), 10)
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertFalse(call.done())
                self.assertFalse(control.pause_event.is_set())
                self.assertEqual(1, self.stats(task, target)['total'])
                self.assertEqual(1, self.checkpoint(task, target)['counters']['discovered'])
                control.pause_event.set()
                await asyncio.wait_for(call, 10)
                self.assertEqual(2, saves.call_count)
                self.assertEqual(5, self.checkpoint(task, target)['counters']['discovered'])
            finally:
                control.pause_event.set()
                if not call.done():
                    call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    async def test_exception_after_commit_flushes_real_prefix_without_claiming_the_suffix(self):
        task, target, _, source = await self.launch()
        original = self.service.discover_task_mode_candidate
        names = [f'error.person{i}' for i in range(5)]

        def discover(*args, **kwargs):
            result = original(*args, **kwargs)
            if result['total'] == 3:
                raise RuntimeError('injected failure after third durable commit')
            return result

        with patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover):
            with self.assertRaisesRegex(RuntimeError, 'third durable commit'):
                await source.sink(names)
        self.assertEqual(3, self.stats(task, target)['total'])
        self.assertEqual(3, self.checkpoint(task, target)['counters']['discovered'])
        self.assertFalse(self.service.check_global_dedupe(names[3])['seen'])
        self.assertFalse(self.service.check_global_dedupe(names[4])['seen'])

    async def test_repeated_cancellation_after_commit_still_flushes_before_returning(self):
        task, target, _, source = await self.launch()
        loop = asyncio.get_running_loop()
        committed = asyncio.Event()
        release = threading.Event()
        original = self.service.discover_task_mode_candidate

        def discover(*args, **kwargs):
            result = original(*args, **kwargs)
            loop.call_soon_threadsafe(committed.set)
            if not release.wait(10):
                raise RuntimeError('test did not release durable write')
            return result

        with patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover):
            call = asyncio.create_task(source.sink(['cancel.first', 'cancel.never']))
            try:
                await asyncio.wait_for(committed.wait(), 10)
                call.cancel()
                await asyncio.sleep(0)
                call.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(call, 10)
                self.assertEqual(1, self.stats(task, target)['total'])
                self.assertEqual(1, self.checkpoint(task, target)['counters']['discovered'])
                self.assertFalse(self.service.check_global_dedupe('cancel.never')['seen'])
            finally:
                release.set()
                await asyncio.gather(call, return_exceptions=True)

    async def test_concurrent_windows_keep_one_claim_and_checkpoint_each_callback(self):
        first = await self.launch()
        second = await self.launch()
        names = [f'race.person{i:03d}' for i in range(100)]
        with patch.object(self.service, 'upsert_checkpoint', wraps=self.service.upsert_checkpoint) as save:
            results = await asyncio.wait_for(asyncio.gather(
                first[3].sink(names), second[3].sink(names)), 20)
            self.assertEqual(2, save.call_count)
        self.assertEqual(200, sum(result['total'] for result in results))
        self.assertEqual(100, sum(self.stats(task, target)['pending'] for task, target, _, _ in (first, second)))
        self.assertEqual(100, sum(self.stats(task, target)['deduped'] for task, target, _, _ in (first, second)))
        for task, target, _, _ in (first, second):
            self.assertEqual(100, self.checkpoint(task, target)['counters']['discovered'])
