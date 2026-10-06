"""R24 monitor lifecycle regressions using real lease rows and blocked I/O."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.action_manager import ActionCampaignManager
from app.errors import ConflictError
from app.follow_monitor import FollowMonitorManager
from app.service import CoreService


class MonitorLifecycleR24Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'monitor.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('monitor-r24', 'correct horse battery staple')['id']
        self.closed = []
        self.browser = SimpleNamespace(close_profile=self.closed.append)
        self.manager = FollowMonitorManager(self.service, self.browser)

    def tearDown(self):
        self.tmp.cleanup()

    def leases(self):
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM browser_operation_leases')]

    def test_shutdown_rejects_late_start_without_new_run_or_clearing_results(self):
        async def exercise():
            self.manager._scan_profile = AsyncMock(return_value=(0, 0, 0, 0))
            self.manager._commit_dm_scan(self.owner, 'saved', 'w1', 'ig1', 'owner_one', [
                {'thread_id': 't1', 'sender': 'A', 'preview': 'hello'},
            ])
            await self.manager.shutdown()
            try:
                with self.assertRaises(ConflictError):
                    await self.manager.start(self.owner, ['w1'], check_kind='dm')
            finally:
                await self.manager.shutdown()
            with self.db.read() as connection:
                self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM follow_monitor_runs').fetchone()[0])
                self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM follow_monitor_latest_dm').fetchone()[0])
            self.manager._scan_profile.assert_not_awaited()
        asyncio.run(exercise())

    def test_worker_constructor_failure_does_not_leave_window_lease(self):
        async def exercise():
            with patch('app.follow_monitor.PlaywrightWorker', side_effect=RuntimeError('worker initialization failed')):
                with self.assertRaisesRegex(RuntimeError, 'worker initialization failed'):
                    await self.manager._scan_profile(self.owner, 'broken', 'w1', 'dm')
            self.assertEqual([], self.leases())
            self.assertEqual([], self.closed)
        asyncio.run(exercise())

    def test_action_worker_constructor_failure_cannot_leave_campaign_running(self):
        async def exercise():
            def broken_factory(_browser):
                raise RuntimeError('action worker initialization failed')
            manager = ActionCampaignManager(self.service, self.browser, worker_factory=broken_factory)
            try:
                campaign = await manager.start_campaign(self.owner, operation='follow', profile_id='w1',
                    targets=['alpha'], message=None, interval='8-15', limit=1)
                task = manager._campaigns[campaign['id']].task
                await asyncio.gather(task, return_exceptions=True)
                current = self.service.get_action_campaign(self.owner, campaign['id'])
                self.assertEqual('failed', current['status'])
                self.assertEqual(['failed'], [target['status'] for target in current['targets']])
                self.assertEqual([], current['attempts'])
                self.assertEqual([], self.leases())
                self.assertFalse(manager.active_campaign_ids())
            finally:
                await manager.shutdown()
        asyncio.run(exercise())

    def test_lease_loss_interrupts_blocked_scan_and_never_commits_its_result(self):
        async def exercise():
            entered = asyncio.Event()
            failed = asyncio.Event()
            worker = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
            async def read_dm(_worker):
                entered.set()
                await asyncio.Event().wait()
            self.manager._read_identity = AsyncMock(return_value=('ig1', 'owner_one'))
            self.manager._read_pending_dms = AsyncMock(side_effect=read_dm)
            original_wait = asyncio.wait_for
            async def fast_heartbeat_wait(awaitable, *, timeout):
                if timeout == 60:
                    awaitable.close()
                    await entered.wait()
                    raise asyncio.TimeoutError()
                return await original_wait(awaitable, timeout=timeout)
            def lose_lease(*args, **kwargs):
                failed_loop.call_soon_threadsafe(failed.set)
                raise ConflictError('Browser operation lease was lost')
            failed_loop = asyncio.get_running_loop()
            with patch('app.follow_monitor.PlaywrightWorker', return_value=worker), patch(
                'app.follow_monitor.asyncio.wait_for', side_effect=fast_heartbeat_wait
            ), patch.object(self.service, 'renew_browser_lease', side_effect=lose_lease):
                await self.manager.start(self.owner, ['w1'], check_kind='dm')
                task = next(iter(self.manager._tasks.values()))
                await original_wait(failed.wait(), timeout=2)
                completed = False
                try:
                    await original_wait(asyncio.shield(task), timeout=0.5)
                    completed = True
                except asyncio.TimeoutError:
                    pass
                finally:
                    await self.manager.shutdown()
            self.assertTrue(completed, 'lost lease left a scan running without its heartbeat')
            run = self.manager.snapshot(self.owner)['run']
            self.assertEqual('failed', run['status'])
            self.assertEqual(1, run['failed'])
            self.assertEqual(0, run['dm_count'])
            self.assertEqual([], self.leases())
            worker.disconnect.assert_awaited_once()
        asyncio.run(exercise())

    def test_old_run_controls_cannot_pause_a_new_run(self):
        async def exercise():
            self.manager._scan_profile = AsyncMock(return_value=(0, 0, 0, 0))
            old = await self.manager.start(self.owner, ['w1'], check_kind='dm')
            await asyncio.gather(*list(self.manager._tasks.values()))
            new = await self.manager.start(self.owner, ['w1'], check_kind='dm')
            with self.assertRaises(ConflictError):
                await self.manager.control(self.owner, old['run_id'], 'pause')
            self.assertTrue(self.manager._controls[new['run_id']].gate.is_set())
            await self.manager.shutdown()
        asyncio.run(exercise())

    def test_cancelled_shutdown_waits_for_disconnect_and_persists_stopped(self):
        async def exercise():
            entered = asyncio.Event()
            disconnecting = asyncio.Event()
            permit_disconnect = asyncio.Event()
            async def read_dm(_worker):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect():
                disconnecting.set()
                await permit_disconnect.wait()
            worker = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(side_effect=disconnect))
            self.manager._read_identity = AsyncMock(return_value=('ig1', 'owner_one'))
            self.manager._read_pending_dms = AsyncMock(side_effect=read_dm)
            with patch('app.follow_monitor.PlaywrightWorker', return_value=worker):
                await self.manager.start(self.owner, ['w1'], check_kind='dm')
                await entered.wait()
                shutdown = asyncio.create_task(self.manager.shutdown())
                await disconnecting.wait()
                shutdown.cancel()
                await asyncio.sleep(0)
                shutdown.cancel()
                await asyncio.sleep(0)
                self.assertFalse(shutdown.done())
                self.assertEqual(1, len(self.leases()))
                permit_disconnect.set()
                with self.assertRaises(asyncio.CancelledError):
                    await shutdown
            self.assertEqual('stopped', self.manager.snapshot(self.owner)['run']['status'])
            self.assertEqual([], self.leases())
            self.assertFalse(self.manager._controls)
            self.assertFalse(self.manager._owner_runs)
            self.assertEqual([], self.closed)
        asyncio.run(asyncio.wait_for(exercise(), timeout=5))

    def test_old_scan_cleanup_does_not_close_or_release_a_replacement_window(self):
        async def exercise():
            worker = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
            self.manager._read_identity = AsyncMock(return_value=('ig1', 'owner_one'))
            replacement_token = None
            async def replace_owner(_worker):
                nonlocal replacement_token
                old = self.leases()[0]
                self.service.release_browser_lease('w1', old['lease_token'])
                replacement_token = self.service.acquire_browser_lease(self.owner, 'w1', operation_type='monitor', entity_id='new-run')
                raise ConflictError('old lease lost')
            self.manager._read_pending_dms = AsyncMock(side_effect=replace_owner)
            try:
                with patch('app.follow_monitor.PlaywrightWorker', return_value=worker):
                    with self.assertRaisesRegex(ConflictError, 'old lease lost'):
                        await self.manager._scan_profile(self.owner, 'old-run', 'w1', 'dm')
                self.assertEqual([], self.closed)
                self.assertEqual(replacement_token, self.leases()[0]['lease_token'])
                worker.disconnect.assert_awaited_once()
            finally:
                if replacement_token:
                    self.service.release_browser_lease('w1', replacement_token)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
