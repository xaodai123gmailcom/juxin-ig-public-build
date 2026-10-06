"""Database dispatch must not stop the browser loop or lose a committed claim."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.service import CoreService
from test_core import FakeCollectionWorker


class EmptyWorker(FakeCollectionWorker):
    def __init__(self, provider):
        super().__init__(provider)
        self.sources = provider.sources

    async def collect_followers(self, target, **kwargs):
        self.sources.append(target)
        return CollectionOutcome('followers', [], source_total=0)


class Provider:
    def __init__(self):
        self.sources = []
        self.closed = []

    def close_profile(self, profile):
        self.closed.append(profile)
        return {'closed': True}


class DispatchResponsivenessR94Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'dispatch.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('dispatch-owner', 'long-test-password')['id']
        self.provider = Provider()
        self.manager = ExecutionManager(self.service, self.provider, worker_factory=EmptyWorker)

    def prepare(self, live=False, source='source'):
        if live:
            self.service.upsert_manual_split_candidates(self.owner,
                [{'username': source, 'queued': True, 'allowed_window_ids': ['w1']}])
        task = self.service.create_task(self.owner, name='dispatch', modes=['followers'],
            targets=[] if live else [source], window_ids=['w1'],
            settings={'live_queue_enabled': live, 'location_enabled': False, 'local_person_recognition': False})
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        token = self.service.acquire_browser_lease(self.owner, 'w1', operation_type='collection', entity_id=task['id'])
        pause = asyncio.Event(); pause.set()
        control = ExecutionControl(owner_user_id=self.owner, task_id=task['id'], pause_event=pause,
            stop_event=asyncio.Event(), leases={'w1': token}, target_queue=asyncio.Queue(), started_profile_ids={'w1'})
        control.work_available.set()
        return task, control

    def run_window(self, task, control):
        return asyncio.create_task(self.manager._window_loop(control, 'w1', list(task['targets']),
            control.target_queue, ['followers'], task['settings']))

    async def test_dispatch_state_reads_leave_browser_event_loop(self):
        await self.assert_off_loop('collection_dispatch_state')

    async def test_target_transitions_leave_browser_event_loop(self):
        await self.assert_off_loop('set_target_runtime_status')

    async def test_checkpoint_reads_leave_browser_event_loop(self):
        await self.assert_off_loop('get_checkpoint')

    async def test_checkpoint_writes_leave_browser_event_loop(self):
        await self.assert_off_loop('upsert_checkpoint')

    async def test_live_target_claims_leave_browser_event_loop(self):
        await self.assert_off_loop('claim_next_split_candidate', live=True)

    async def test_drained_window_release_leaves_browser_event_loop(self):
        await self.assert_off_loop('release_browser_lease')

    async def assert_off_loop(self, method, live=False):
        task, control = self.prepare(live)
        original = getattr(self.service, method)
        loop_thread = threading.get_ident()
        threads = []
        def observed(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args)
        with patch.object(self.service, method, observed):
            await asyncio.wait_for(self.run_window(task, control), 5)
        self.assertEqual(['source'], self.provider.sources)
        self.assertTrue(threads, method)
        self.assertNotIn(loop_thread, threads, method + ' blocks all browser coroutines on slow SQLite')

    async def test_repeated_cancel_preserves_target_claim_until_commit_and_reconciliation(self):
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                task, control = self.prepare(live=True, source=f'source_{after_commit}')
                entered, release = threading.Event(), threading.Event()
                claimed_ids = []
                original = self.service.claim_next_split_candidate
                def held(*args):
                    result = original(*args) if after_commit else None
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('fixture claim not released')
                    result = result if after_commit else original(*args)
                    if result is not None:
                        claimed_ids.append(result['id'])
                    return result
                with patch.object(self.service, 'claim_next_split_candidate', held):
                    job = self.run_window(task, control)
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        job.cancel()
                        await asyncio.sleep(0)
                        job.cancel()
                        await asyncio.sleep(.02)
                        self.assertFalse(job.done(), 'cancel abandoned an in-flight durable claim')
                        self.assertIn(control.leases['w1'], self.db.live_browser_lease_tokens)
                        control.stop_event.set()
                    finally:
                        release.set()
                        result = await asyncio.gather(job, return_exceptions=True)
                self.assertIsInstance(result[0], asyncio.CancelledError)
                target = self.service.get_task(self.owner, task['id'])['targets'][0]
                self.assertEqual('recoverable', target['status'], 'committed claim was lost before cleanup')
                self.assertEqual([target['id']], claimed_ids)
                self.assertIn(control.leases['w1'], self.db.live_browser_lease_tokens)
                self.assertEqual([], self.provider.sources)
                self.service.release_browser_lease('w1', control.leases['w1'])

    async def test_pause_during_claim_wait_prevents_browser_work_until_resume(self):
        task, control = self.prepare(live=True)
        entered, release = threading.Event(), threading.Event()
        original = self.service.claim_next_split_candidate
        def held(*args):
            result = original(*args)
            if result is not None:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('fixture claim not released')
            return result
        with patch.object(self.service, 'claim_next_split_candidate', held):
            job = self.run_window(task, control)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                control.pause_event.clear()
                release.set()
                await asyncio.sleep(.03)
                self.assertEqual([], self.provider.sources)
                self.assertFalse(job.done())
                self.assertIn(control.leases['w1'], self.db.live_browser_lease_tokens)
            finally:
                control.pause_event.set(); release.set()
                await asyncio.wait_for(job, 5)
        self.assertEqual(['source'], self.provider.sources)

    async def test_new_work_during_empty_read_is_processed_before_window_retires(self):
        for live in (False, True):
            with self.subTest(live=live):
                source, late = f'source_{live}', f'late_{live}'
                task, control = self.prepare(live=live, source=source)
                entered, release = threading.Event(), threading.Event()
                original = self.service.has_unfinished_window_targets
                armed = True
                def stale_empty(*args):
                    nonlocal armed
                    result = original(*args)
                    if armed and not result:
                        armed = False
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('fixture empty read not released')
                    return result
                start = len(self.provider.sources)
                with patch.object(self.service, 'has_unfinished_window_targets', stale_empty):
                    job = self.run_window(task, control)
                    control.coordinator = job
                    self.manager._runs[task['id']] = control
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        added = await self.manager.add_targets(self.owner, task['id'], [late])
                        self.assertEqual(1, added['added'])
                    finally:
                        release.set()
                        await asyncio.wait_for(job, 5)
                        self.manager._runs.pop(task['id'], None)
                self.assertEqual([source, late], self.provider.sources[start:])
                current = self.service.get_task(self.owner, task['id'])
                self.assertTrue(all(row['status'] == 'completed' for row in current['targets']))

    async def test_real_writer_contention_does_not_block_loop_or_read_only_status(self):
        task, control = self.prepare(live=True)
        entered, writer_entered, release, writer_finished = (threading.Event() for _ in range(4))
        def writer():
            try:
                with self.db.write():
                    writer_entered.set(); release.wait(5)
            finally:
                writer_finished.set()
        thread = threading.Thread(target=writer)
        original = self.service.claim_next_split_candidate
        armed = False
        def blocked(*args):
            nonlocal armed
            if not armed:
                armed = True; thread.start()
                if not writer_entered.wait(2):
                    raise TimeoutError('writer did not start')
                entered.set()
            return original(*args)
        with patch.object(self.service, 'claim_next_split_candidate', blocked):
            job = self.run_window(task, control)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                pulse = asyncio.Event()
                asyncio.get_running_loop().call_soon(pulse.set)
                await asyncio.wait_for(pulse.wait(), .5)
                self.assertEqual('ok', await asyncio.wait_for(asyncio.to_thread(self.db.liveness_check), .5))
                self.assertFalse(writer_finished.is_set(), 'loop waited until the writer watchdog released')
                self.assertFalse(job.done())
            finally:
                release.set()
                await asyncio.wait_for(job, 5)
                if thread.ident is not None:
                    await asyncio.to_thread(thread.join, 2)
        self.assertEqual(['source'], self.provider.sources)

    async def test_cancelled_durable_call_preserves_cancellation_after_late_storage_error(self):
        entered, release = threading.Event(), threading.Event()
        def failed():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture write not released')
            raise OSError('late storage failure')
        job = asyncio.create_task(self.manager._await_durable_thread_call(failed))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            job.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(job.done())
        finally:
            release.set()
            outcome = await asyncio.gather(job, return_exceptions=True)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)

    async def test_uncancelled_durable_failure_still_reports_real_storage_error(self):
        def failed():
            raise OSError('real storage failure')
        with self.assertRaisesRegex(OSError, 'real storage failure'):
            await self.manager._await_durable_thread_call(failed)

    async def test_repeated_cancel_during_disconnect_removes_finished_worker_reference(self):
        task, control = self.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        class HeldClose(EmptyWorker):
            async def disconnect(self):
                entered.set()
                await release.wait()
        self.manager.worker_factory = HeldClose
        job = self.run_window(task, control)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            job.cancel(); await asyncio.sleep(0)
            job.cancel(); await asyncio.sleep(.02)
            self.assertFalse(job.done())
            self.assertIn('w1', control.profile_workers)
            self.assertIn(control.leases['w1'], self.db.live_browser_lease_tokens)
        finally:
            release.set()
            result = await asyncio.gather(job, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertNotIn('w1', control.profile_workers, 'finished worker remains in live window registry')
        self.assertEqual(['w1'], self.provider.closed)

    async def test_old_disconnect_does_not_remove_replacement_worker_reference(self):
        task, control = self.prepare()
        replacement = object()
        class ReplacedClose(EmptyWorker):
            async def disconnect(self):
                control.profile_workers['w1'] = replacement
        self.manager.worker_factory = ReplacedClose
        await asyncio.wait_for(self.run_window(task, control), 5)
        self.assertIs(replacement, control.profile_workers['w1'])

    async def test_cancel_during_durable_release_publishes_cleanup_after_commit(self):
        task, control = self.prepare()
        token = control.leases['w1']
        entered, release = threading.Event(), threading.Event()
        original = self.service.release_browser_lease
        def held(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture release not unblocked')
            original(*args, **kwargs)
        with patch.object(self.service, 'release_browser_lease', held):
            job = asyncio.create_task(self.manager._close_drained_window(control, 'w1'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                job.cancel()
                await asyncio.sleep(0)
                job.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(job.done())
                self.assertEqual(token, control.leases['w1'])
                self.assertIn(token, self.db.live_browser_lease_tokens)
            finally:
                release.set()
                outcome = await asyncio.gather(job, return_exceptions=True)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)
        self.assertNotIn('w1', control.leases)
        self.assertNotIn(token, self.db.live_browser_lease_tokens)
        self.assertEqual('closed', control.profile_states['w1']['state'])
        self.assertNotIn('w1', control.closing_profile_ids)
        self.assertTrue(control.workers_changed.is_set())

    async def test_late_release_does_not_remove_replacement_lease_or_window_state(self):
        task, control = self.prepare()
        original = self.service.release_browser_lease
        entered, release = threading.Event(), threading.Event()
        replacement = {}
        def held(*args, **kwargs):
            original(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture release not unblocked')
        with patch.object(self.service, 'release_browser_lease', held):
            job = asyncio.create_task(self.manager._close_drained_window(control, 'w1'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                replacement['token'] = self.service.acquire_browser_lease(
                    self.owner, 'w1', operation_type='collection', entity_id=task['id'])
                control.leases['w1'] = replacement['token']
                control.profile_states['w1'] = {'state': 'working', 'generation': 'new'}
            finally:
                release.set()
                result = await asyncio.wait_for(job, 5)
        self.assertFalse(result)
        self.assertEqual(replacement['token'], control.leases['w1'])
        self.assertIn(replacement['token'], self.db.live_browser_lease_tokens)
        self.assertEqual({'state': 'working', 'generation': 'new'}, control.profile_states['w1'])
