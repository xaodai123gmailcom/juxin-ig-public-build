"""One durable gap pass, without changing relation-end or lease ownership rules."""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
import test_parallel_relation_pipeline as pipeline
import test_collection_drain_r56 as drain
from test_core import FakeCollectionWorker
from test_parallel_relation_pipeline import _PipelineManager, _NoopBitBrowser


class SingleGapRecheckR6Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    def worker(self, target, control, passes):
        case = self

        class Worker:
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True

            def __init__(self):
                self.calls = []
                self.reads = []

            async def collect_followers(self, username, *, candidate_sink, progress_sink,
                                        initial_candidate_count, initial_resume_tail=None, **kwargs):
                case.assertEqual(target['username'], username)
                self.calls.append((initial_candidate_count, initial_resume_tail))
                return await passes[len(self.calls) - 1](self, candidate_sink, progress_sink)

            async def screen(self, username):
                self.reads.append(username)

        return Worker()

    def pass_with(self, names, total, *, progress=None):
        async def collect(worker, sink, progress_sink):
            if progress is not None:
                await progress_sink(progress)
            await sink(names)
            return CollectionOutcome('followers', [], source_total=total)
        return collect

    async def execute(self, task, target, control, worker, checkpoint=None):
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        return await manager._execute_candidate_spooled_mode(
            control, worker, target, 'followers', task['settings'], checkpoint,
        )

    def checkpoint(self, task, target):
        return self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')

    async def test_no_gap_unknown_and_invalid_totals_do_not_schedule_extra(self):
        for total in (None, 0, 1, True, '5', -1):
            with self.subTest(total=total):
                task, target, control = self._task_and_control(1)
                worker = self.worker(target, control, [self.pass_with(['one'], total)])
                result = await self.execute(task, target, control, worker)
                self.assertEqual(1, len(worker.calls))
                self.assertTrue(result['discovery_complete'])
                self.assertFalse(result['automatic_gap_recheck_started'])

    async def test_gap_runs_once_from_start_and_counts_durable_unique_rows(self):
        task, target, control = self._task_and_control(1)

        async def extra(worker, sink, progress):
            checkpoint = self.checkpoint(task, target)
            cursor = ExecutionManager._checkpoint_resume_cursor(checkpoint)
            self.assertTrue(cursor['automatic_gap_recheck_started'])
            self.assertFalse(cursor['candidate_spool_complete'])
            self.assertFalse(cursor['candidate_spool_natural_end'])
            self.assertNotIn('resume_tail', cursor)
            self.assertNotIn('rendered_count', cursor)
            self.assertNotIn('progress_epoch', cursor)
            self.assertEqual((1, None), worker.calls[-1])
            await progress({'source_total': 9, 'resume_tail': ['new'], 'progress_epoch': 2})
            await sink(['one', 'one', 'new'])
            return CollectionOutcome('followers', [], source_total=9)

        worker = self.worker(target, control, [
            self.pass_with(['one', 'one'], 4, progress={
                'resume_tail': ['one'], 'rendered_count': 5, 'progress_epoch': 11,
            }), extra,
        ])
        result = await self.execute(task, target, control, worker)
        self.assertEqual(2, len(worker.calls))
        self.assertEqual(['one', 'new'], worker.reads)
        self.assertEqual(2, result['total'])
        self.assertEqual(9, result['source_total'])
        self.assertTrue(result['discovery_complete'])
        self.assertTrue(self.checkpoint(task, target)['cursor']['automatic_gap_recheck_started'])

    async def test_extra_pass_uses_newest_lower_or_zero_source_total(self):
        for latest in (0, 1):
            with self.subTest(latest=latest):
                task, target, control = self._task_and_control(1)
                worker = self.worker(target, control, [
                    self.pass_with(['one'], 5), self.pass_with([], latest),
                ])
                result = await self.execute(task, target, control, worker)
                self.assertEqual(2, len(worker.calls))
                self.assertEqual(1, result['total'])
                self.assertEqual(latest, result['source_total'])
                self.assertEqual(latest, self.checkpoint(task, target)['counters']['source_total'])
                self.assertTrue(result['discovery_complete'])

    async def test_progress_gap_without_successful_source_return_never_qualifies(self):
        for reason in ('instagram_login_required', 'instagram_network_unavailable',
                       'instagram_followers_list_incomplete'):
            with self.subTest(reason=reason):
                task, target, control = self._task_and_control(1)
                async def failed(worker, sink, progress):
                    await progress({'source_total': 10})
                    raise WorkerExecutionError('not a verified end', reason=reason)
                worker = self.worker(target, control, [failed])
                with self.assertRaises(WorkerExecutionError):
                    await self.execute(task, target, control, worker)
                cursor = self.checkpoint(task, target)['cursor']
                self.assertFalse(cursor['candidate_spool_complete'])
                self.assertNotIn('automatic_gap_recheck_started', cursor)
                self.assertEqual(1, len(worker.calls))

    async def test_extra_failure_stays_incomplete_and_wrapped_restart_gets_no_third_pass(self):
        task, target, control = self._task_and_control(1)
        async def failed(worker, sink, progress):
            await progress({'resume_tail': ['second_tail'], 'source_total': 7})
            raise WorkerExecutionError('login interrupted extra pass', reason='instagram_login_required')
        worker = self.worker(target, control, [self.pass_with(['one'], 7), failed])
        with self.assertRaises(WorkerExecutionError):
            await self.execute(task, target, control, worker)
        previous = self.checkpoint(task, target)
        self.assertFalse(previous['cursor']['candidate_spool_complete'])
        self.assertTrue(previous['cursor']['automatic_gap_recheck_started'])
        wrapped = {'stage': 'waiting_network', 'cursor': {'resume_cursor': previous['cursor']},
                   'counters': {'previous_counters': previous['counters']}}
        self.service.database.initialize()
        resumed = self.worker(target, control, [self.pass_with(['one', 'two'], 7)])
        result = await self.execute(task, target, control, resumed, wrapped)
        self.assertEqual([(1, ['second_tail'])], resumed.calls)
        self.assertTrue(result['discovery_complete'])
        self.assertTrue(result['automatic_gap_recheck_started'])
        self.assertEqual(2, result['total'])

    async def test_pause_at_gap_boundary_persists_budget_before_extra_starts(self):
        task, target, control = self._task_and_control(1)
        written = asyncio.Event()
        async def first(worker, sink, progress):
            await sink(['one'])
            control.pause_event.clear()
            return CollectionOutcome('followers', [], source_total=3)
        original = self.service.upsert_checkpoint
        loop = asyncio.get_running_loop()
        def upsert(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs['cursor'].get('automatic_gap_recheck_started'):
                loop.call_soon_threadsafe(written.set)
            return value
        worker = self.worker(target, control, [first, self.pass_with(['two'], 3)])
        with patch.object(self.service, 'upsert_checkpoint', upsert):
            running = asyncio.create_task(self.execute(task, target, control, worker))
            try:
                await asyncio.wait_for(written.wait(), 3)
                self.assertEqual(1, len(worker.calls))
                self.assertFalse(running.done())
                self.assertFalse(self.checkpoint(task, target)['cursor']['candidate_spool_complete'])
            finally:
                control.pause_event.set()
            result = await asyncio.wait_for(running, 3)
        self.assertEqual(2, len(worker.calls))
        self.assertTrue(result['discovery_complete'])

    async def test_repeated_cancellation_during_budget_commit_resumes_only_extra(self):
        task, target, control = self._task_and_control(1)
        entered = asyncio.Event()
        release = threading.Event()
        original = self.service.upsert_checkpoint
        loop = asyncio.get_running_loop()
        def upsert(*args, **kwargs):
            if kwargs['cursor'].get('automatic_gap_recheck_started'):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError('test did not release checkpoint write')
            return original(*args, **kwargs)
        worker = self.worker(target, control, [self.pass_with(['one'], 5)])
        with patch.object(self.service, 'upsert_checkpoint', upsert):
            running = asyncio.create_task(self.execute(task, target, control, worker))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                running.cancel()
                await asyncio.sleep(0)
                running.cancel()
                self.assertEqual(1, len(worker.calls))
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        checkpoint = self.checkpoint(task, target)
        self.assertTrue(checkpoint['cursor']['automatic_gap_recheck_started'])
        self.assertFalse(checkpoint['cursor']['candidate_spool_complete'])
        resumed = self.worker(target, control, [self.pass_with(['one', 'two'], 5)])
        result = await self.execute(task, target, control, resumed, checkpoint)
        self.assertEqual([(1, None)], resumed.calls)
        self.assertTrue(result['discovery_complete'])

    async def test_failed_budget_write_cannot_start_extra(self):
        task, target, control = self._task_and_control(1)
        original = self.service.upsert_checkpoint
        def upsert(*args, **kwargs):
            if kwargs['cursor'].get('automatic_gap_recheck_started'):
                raise RuntimeError('disk is unavailable')
            return original(*args, **kwargs)
        worker = self.worker(target, control, [self.pass_with(['one'], 5)])
        with patch.object(self.service, 'upsert_checkpoint', upsert):
            with self.assertRaisesRegex(RuntimeError, 'disk is unavailable'):
                await self.execute(task, target, control, worker)
        self.assertEqual(1, len(worker.calls))
        self.assertFalse(self.checkpoint(task, target)['cursor']['candidate_spool_complete'])

    async def test_unconfirmed_rows_do_not_allow_recheck_or_completion(self):
        task, target, control = self._task_and_control(1)
        worker = self.worker(target, control, [self.pass_with(['one'], 5, progress={
            'pending_relation_usernames': ['unconfirmed'],
        })])
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.execute(task, target, control, worker)
        self.assertEqual('instagram_relation_unconfirmed_rows', caught.exception.code)
        self.assertEqual(1, len(worker.calls))
        cursor = self.checkpoint(task, target)['cursor']
        self.assertFalse(cursor['candidate_spool_complete'])
        self.assertNotIn('automatic_gap_recheck_started', cursor)

    async def test_explicit_manual_recheck_is_already_the_supplemental_pass(self):
        task, target, control = self._task_and_control(1)
        checkpoint = {'cursor': {'source_recheck_requested': True}, 'counters': {'source_total': 5}}
        worker = self.worker(target, control, [self.pass_with(['one'], 5)])
        result = await self.execute(task, target, control, worker, checkpoint)
        self.assertEqual(1, len(worker.calls))
        self.assertTrue(result['automatic_gap_recheck_started'])
        self.assertTrue(result['discovery_complete'])

    async def test_retired_campaign_fields_do_not_restore_three_to_five_rounds(self):
        task, target, control = self._task_and_control(1)
        checkpoint = {'cursor': {'automatic_recheck': {'round': 1, 'max_rounds': 5}},
                      'counters': {'source_total': 9, 'automatic_recheck_round': 1}}
        worker = self.worker(target, control, [self.pass_with(['one'], 9), self.pass_with(['one'], 9)])
        result = await self.execute(task, target, control, worker, checkpoint)
        self.assertEqual(2, len(worker.calls))
        self.assertTrue(result['discovery_complete'])
        self.assertNotIn('automatic_recheck', self.checkpoint(task, target)['cursor'])

    async def test_historical_complete_checkpoint_is_not_backfilled(self):
        task, target, control = self._task_and_control(1)
        checkpoint = {'cursor': {'candidate_spool_complete': True, 'candidate_spool_natural_end': True},
                      'counters': {'source_total': 9}}
        worker = self.worker(target, control, [])
        result = await self.execute(task, target, control, worker, checkpoint)
        self.assertEqual([], worker.calls)
        self.assertTrue(result['discovery_complete'])
        self.assertFalse(result['automatic_gap_recheck_started'])

    async def test_real_worker_normal_passes_reopen_and_reset_top_even_without_new_rows(self):
        from types import SimpleNamespace
        from test_collection_tail_r94 import TailWorker, TailSurface
        for mode in ('followers', 'following'):
            with self.subTest(mode=mode):
                worker = TailWorker(header=5)
                worker.page = SimpleNamespace(url='https://www.instagram.com/source/')
                worker._navigate_profile = AsyncMock()
                # A surface retained for a different old page is never reused.
                worker._paused_relation_surface = (object(), 'source', mode, object(), 50)
                surface = TailSurface(worker, count=2, late_at=None)
                worker._open_relation_surface = AsyncMock(return_value=surface)
                saved = set()
                async def sink(batch):
                    saved.update(batch)
                    return len(saved)
                collect = worker.collect_followers if mode == 'followers' else worker.collect_following
                with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
                    first = await collect('source', limit=None, candidate_sink=sink)
                    self.assertIsNone(worker._paused_relation_surface)
                    second = await collect('source', limit=None, candidate_sink=sink,
                                           initial_candidate_count=len(saved))
                self.assertEqual(2, first.candidate_count)
                self.assertEqual(2, second.candidate_count)
                self.assertEqual(5, second.source_total)
                self.assertEqual(2, worker._navigate_profile.await_count)
                self.assertEqual(2, worker._open_relation_surface.await_count)
                self.assertEqual(2, surface.resets)
                self.assertGreaterEqual(len(surface.measure_times), 4)
                self.assertIsNone(worker._paused_relation_surface)


class SingleGapManagerRecoveryR6Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain.CollectionDrainR56Tests.asyncTearDown
    task = drain.CollectionDrainR56Tests.task
    manager = drain.CollectionDrainR56Tests.manager
    gate = drain.CollectionDrainR56Tests.gate
    until = drain.CollectionDrainR56Tests.until
    leases = drain.CollectionDrainR56Tests.leases

    async def test_second_pass_private_error_preserves_budget_through_mode_unavailable(self):
        task = self.task()
        target = task['targets'][0]
        calls = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                calls.append(username)
                if len(calls) == 2:
                    raise WorkerExecutionError('source became private',
                        reason='instagram_private_profile', pause_required=False)
                return CollectionOutcome('followers', ['one'], source_total=5)
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await self.until(lambda: (self.service.get_checkpoint(
            self.owner, task['id'], target['id'], 'followers') or {}).get('stage') == 'mode_unavailable')
        # Live queues remain open for new sources when one is unavailable.
        # End that idle owner before simulating a process restart.
        await manager.stop(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        checkpoint = self.service.get_checkpoint(self.owner, task['id'], target['id'], 'followers')
        self.assertEqual('mode_unavailable', checkpoint['stage'])
        cursor = manager._checkpoint_resume_cursor(checkpoint)
        self.assertTrue(cursor['automatic_gap_recheck_started'])
        self.assertFalse(cursor['candidate_spool_complete'])
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertNotEqual('completed', current['status'])
        self.assertFalse(current['collection_list_dismissed'])
        self.database.initialize()
        self.service.recover_interrupted_operations()
        resumed_calls = []
        class Resumed(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                resumed_calls.append(username)
                return CollectionOutcome('followers', ['one', 'two'], source_total=5)
        resumed, _ = self.manager(Resumed)
        await resumed.retry_target(self.owner, task['id'], target['id'])
        await asyncio.wait_for(resumed.wait(task['id']), 10)
        self.assertEqual(['source', 'source'], calls)
        self.assertEqual(['source'], resumed_calls)
        checkpoint = self.service.get_checkpoint(self.owner, task['id'], target['id'], 'followers')
        self.assertEqual('mode_completed', checkpoint['stage'])
        self.assertTrue(checkpoint['cursor']['automatic_gap_recheck_started'])
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertTrue(current['collection_list_dismissed'])
        self.assertEqual(3, current['mode_coverage']['followers']['unobserved_count'])
