"""A slow recovery commit must not freeze snapshots or abandon its checkpoint."""
import asyncio
import json
from contextlib import contextmanager
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock

from app.config import Settings
from app.database import Database
from app.execution_manager import ExecutionControl
from app.main import create_app
from app.service import CoreService
from app.playwright_worker import WorkerExecutionError


class Request:
    async def is_disconnected(self):
        return False


class Browser:
    def list_all_windows(self):
        return {'windows': [{'id': 'w1', 'name': 'Window', 'is_open': True}],
                'connection': {'connected': True}}


class RecoveryResponsivenessR94Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'recovery.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('responsive-recovery', 'long-test-password')
        self.task = self.service.create_task(self.owner['id'], name='Recovery',
            modes=['followers'], targets=['source'], window_ids=['w1'], settings={})
        self.target = self.task['targets'][0]
        self.service.set_task_runtime_status(self.owner['id'], self.task['id'], 'running')
        self.service.set_target_runtime_status(self.owner['id'], self.task['id'],
            self.target['id'], 'running', window_id='w1')
        token = self.service.acquire_browser_lease(self.owner['id'], 'w1',
            operation_type='collection', entity_id=self.task['id'])
        self.app = create_app(Settings(startup_token='recovery-startup-token',
            database_path=self.db.path, data_dir=Path(self.temp.name)), database=self.db,
            bitbrowser=Browser())
        self.manager = self.app.state.execution_manager
        pause = asyncio.Event(); pause.set()
        self.control = ExecutionControl(owner_user_id=self.owner['id'], task_id=self.task['id'],
            pause_event=pause, stop_event=asyncio.Event(), leases={'w1': token},
            target_queue=asyncio.Queue(), started_profile_ids={'w1'})
        self.manager._runs[self.task['id']] = self.control
        self.loop_thread = threading.get_ident()

    async def waiting(self):
        error = WorkerExecutionError('hover not ready', reason='instagram_page_recovery_exhausted')
        error.details.update(original_reason='instagram_hover_card_unavailable',
            hover_diagnostics={'stage': 'unavailable', 'row_recycled': 2, 'unsafe': 'ignore'})
        return await self.manager._set_network_waiting(self.control, 'w1', error,
            retry_count=0, retry_delay=5, target=self.target, mode='followers')

    async def test_recovery_database_writes_leave_event_loop_available(self):
        original = self.db.write
        calls = []
        @contextmanager
        def checked():
            self.assertNotEqual(self.loop_thread, threading.get_ident(),
                'recovery SQLite writes block the browser/snapshot event loop')
            calls.append(1)
            with original() as connection:
                yield connection
        with patch.object(self.db, 'write', checked):
            await self.waiting()
            await self.manager._clear_network_waiting(self.control, 'w1', self.target)
        self.assertTrue(calls)
        checkpoint = self.service.get_checkpoint(self.owner['id'], self.task['id'], self.target['id'], 'followers')
        self.assertEqual('waiting_network', checkpoint['stage'])

    async def test_child_retry_does_not_replace_healthy_parent_and_persists_failure_scope(self):
        worker = Mock(connection_healthy=AsyncMock(return_value=True), _deferred_screening_workers={})
        error = WorkerExecutionError('child temporarily unavailable', reason='instagram_page_recovery_exhausted')
        error.details.update(original_reason='instagram_profile_not_ready', recovery_scope='screening_child',
            candidate_username='pending.account', recovery_target='pending.account')
        with patch.object(self.manager, '_wait_retry_delay', AsyncMock(return_value=True)):
            generation = await self.manager._recover_network_connection(self.control, worker, 'w1', error,
                target=self.target, mode='followers')
        self.assertIsNotNone(generation)
        worker.request_page_replacement.assert_not_called()
        worker.disconnect.assert_not_called()
        checkpoint = self.service.get_checkpoint(self.owner['id'], self.task['id'], self.target['id'], 'followers')
        self.assertEqual('screening_child', checkpoint['counters']['recovery_scope'])

    async def test_snapshot_can_read_live_state_while_recovery_transition_lock_is_held(self):
        self.control.network_waiters['w1'] = dict(profile_id='w1', state='waiting_network',
            target_id=self.target['id'], reason='instagram_hover_card_unavailable')
        route = next(r.endpoint for r in self.app.routes if getattr(r, 'path', '') == '/api/workbench/snapshot')
        job = None
        try:
            async with self.control.network_state_lock:
                job = asyncio.create_task(route(Request(), (self.owner, 'session'), limit=10, history_limit=10))
                done, _ = await asyncio.wait({job}, timeout=3)
                self.assertIn(job, done, 'snapshot queued behind recovery persistence')
                result = json.loads(job.result().body)
        finally:
            if job:
                await asyncio.gather(job, return_exceptions=True)
        self.assertTrue(result['windows'][0]['locked'])
        profile = result['tasks'][0]['runtime']['profile_states'][0]
        self.assertEqual('waiting_network', profile['state'])
        self.assertEqual(self.target['id'], profile['current_target_id'])
        await self.app.state.snapshot_inventory.close()

    async def test_cancel_waits_for_real_recovery_commit_and_keeps_original_diagnostics(self):
        original = self.db.write
        entered, release = threading.Event(), threading.Event()
        @contextmanager
        def held():
            self.assertNotEqual(self.loop_thread, threading.get_ident(), 'synchronous recovery write')
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release write')
            with original() as connection:
                yield connection
        job = None
        with patch.object(self.db, 'write', held):
            try:
                job = asyncio.create_task(self.waiting())
                for _ in range(200):
                    if entered.is_set() or job.done(): break
                    await asyncio.sleep(.005)
                if job.done(): job.result()
                self.assertTrue(entered.is_set())
                job.cancel()
                diagnostic = await asyncio.wait_for(self.manager.runtime_diagnostics(
                    self.owner['id'], self.task['id'], known_status='running'), 3)
                self.assertEqual([], diagnostic['network_waiters'], 'uncommitted recovery became retryable')
                self.assertEqual({}, self.control.network_waiters)
                self.assertFalse(job.done(), 'cancel abandoned an active database commit')
            finally:
                release.set()
                if job:
                    result = await asyncio.gather(job, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        checkpoint = self.service.get_checkpoint(self.owner['id'], self.task['id'], self.target['id'], 'followers')
        self.assertEqual('waiting_network', checkpoint['stage'])
        self.assertEqual({'stage': 'unavailable', 'row_recycled': 2}, checkpoint['counters']['hover_diagnostics'])

    async def test_network_reconciliation_cannot_resume_paused_or_finalized_task(self):
        await self.waiting()
        for status in ('paused', 'stopped'):
            self.service.set_task_runtime_status(self.owner['id'], self.task['id'], status)
            async with self.control.network_state_lock:
                await self.manager._reconcile_network_task_status_locked(self.control)
            self.assertEqual(status, self.service.get_task_status(self.owner['id'], self.task['id']))

    async def test_pause_committing_while_recovery_queues_cannot_be_overwritten(self):
        original = self.db.write
        entered, release = threading.Event(), threading.Event()
        first = True
        @contextmanager
        def gate_first_background_write():
            nonlocal first
            if first and threading.get_ident() != self.loop_thread:
                first = False
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('test did not release recovery')
            with original() as connection:
                yield connection
        job = None
        with patch.object(self.db, 'write', gate_first_background_write):
            try:
                job = asyncio.create_task(self.waiting())
                for _ in range(200):
                    if entered.is_set() or job.done(): break
                    await asyncio.sleep(.005)
                if job.done(): job.result()
                self.assertTrue(entered.is_set())
                self.service.set_task_runtime_status(self.owner['id'], self.task['id'], 'paused')
            finally:
                release.set()
                if job: await job
        self.assertEqual('paused', self.service.get_task_status(self.owner['id'], self.task['id']))

    async def test_duplicate_reconcile_does_not_request_another_write_transaction(self):
        await self.waiting()
        with patch.object(self.db, 'write', side_effect=AssertionError('unchanged network status rewrote SQLite')):
            async with self.control.network_state_lock:
                await self.manager._reconcile_network_task_status_locked(self.control)


if __name__ == '__main__':
    unittest.main()
