"""Every window user joins native cleanup and confirmed provider close."""
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.action_manager import ActionCampaignManager
from app.follow_monitor import FollowMonitorManager
from app.studio import StudioManager
from app.errors import ConflictError
from test_core import FakeActionWorker
import test_collection_drain_r56 as drain_cases
from support.concurrency_probe import WAIT_SECONDS, assert_database_writer_available


class SharedRetirementR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain_cases.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain_cases.CollectionDrainR56Tests.asyncTearDown
    gate = drain_cases.CollectionDrainR56Tests.gate
    until = drain_cases.CollectionDrainR56Tests.until
    leases = drain_cases.CollectionDrainR56Tests.leases

    async def launch(self, role, provider, disconnected, release, stack, *, native_cancel=False):
        class Worker(FakeActionWorker):
            page = SimpleNamespace()
            async def disconnect(self):
                disconnected.set()
                if native_cancel:
                    raise asyncio.CancelledError()
            async def wait_for_cleanup(self):
                await release.wait()
        if role in {'manual', 'campaign'}:
            manager = ActionCampaignManager(self.service, provider, worker_factory=Worker)
            self.managers.append(manager)
            if role == 'manual':
                return asyncio.create_task(manager.manual_action(self.owner, operation='follow', profile_id=role, target=f'person_{role}', message=None))
            campaign = await manager.start_campaign(self.owner, operation='follow', profile_id=role, targets=[f'person_{role}'], message=None, interval='1 秒', limit=1)
            return manager._campaigns[campaign['id']].task
        if role == 'monitor':
            manager = FollowMonitorManager(self.service, provider)
            self.managers.append(manager)
            manager._read_identity = AsyncMock(return_value=('ig1', 'owner_one'))
            manager._read_pending_dms = AsyncMock(return_value=[])
            stack.enter_context(patch('app.follow_monitor.PlaywrightWorker', Worker))
            return asyncio.create_task(manager._scan_profile(self.owner, 'scan', role, 'dm'))
        manager = StudioManager(self.service, provider)
        self.managers.append(manager)
        stack.enter_context(patch('app.studio.PlaywrightWorker', Worker))
        stack.enter_context(patch('app.studio.StudioBrowser.nurture_step', AsyncMock(return_value={})))
        result = await manager.command(self.owner, {'action':'start', 'kind':'nurture', 'request_id':'retirement-nurture', 'profile_ids':[role], 'config':{'minutes':1, 'surfaces':['profile'], 'targets':['example']}})
        ident = result['job_ids'][0]
        manager.gates[ident] = asyncio.Event()
        manager.gates[ident].set()
        return asyncio.create_task(manager._execute(manager.get(self.owner, ident)))

    async def test_all_roles_join_native_cleanup_before_provider_close_and_lease_release(self):
        for role in ('monitor', 'manual', 'campaign', 'studio'):
            with self.subTest(role=role), ExitStack() as stack:
                provider = drain_cases.CloseProvider()
                disconnected, release = asyncio.Event(), self.gate()
                task = await self.launch(role, provider, disconnected, release, stack)
                try:
                    await asyncio.wait_for(disconnected.wait(), 3)
                    await asyncio.sleep(.04)
                    self.assertIn(role, self.leases())
                    self.assertEqual([], provider.closed)
                    self.assertFalse(task.done())
                    with self.assertRaises(ConflictError):
                        self.service.acquire_browser_lease(self.owner, role, operation_type='account', entity_id='competitor')
                finally:
                    release.set()
                    await asyncio.wait_for(task, 5)
                self.assertNotIn(role, self.leases())
                self.assertEqual([role], provider.closed)

    async def test_all_roles_retry_negative_ack_without_repeating_completed_work(self):
        for role in ('monitor', 'manual', 'campaign', 'studio'):
            with self.subTest(role=role), ExitStack() as stack:
                provider = drain_cases.CloseProvider()
                provider.result = {'closed':False, 'window_state':'closing'}
                disconnected, release = asyncio.Event(), self.gate()
                release.set()
                before_actions = len(FakeActionWorker.calls)
                task = await self.launch(role, provider, disconnected, release, stack)
                try:
                    await self.until(lambda: bool(provider.closed))
                    await asyncio.sleep(.04)
                    self.assertIn(role, self.leases())
                    self.assertFalse(task.done())
                    provider.result = False
                    await self.until(lambda: len(provider.closed) >= 2)
                    await asyncio.sleep(.04)
                    self.assertIn(role, self.leases(), 'literal False is not a close acknowledgement')
                finally:
                    provider.result = {'closed':True}
                    await asyncio.wait_for(task, 5)
                self.assertNotIn(role, self.leases())
                self.assertGreaterEqual(len(provider.closed), 2)
                if role in {'manual', 'campaign'}:
                    self.assertEqual(1, len(FakeActionWorker.calls) - before_actions)

    async def test_provider_close_does_not_block_unrelated_window_admission(self):
        with ExitStack() as stack:
            provider = drain_cases.CloseProvider()
            entered, release_close = self.gate(threaded=True), self.gate(threaded=True)
            close_returned = self.gate(threaded=True)
            close_timed_out = self.gate(threaded=True)
            disconnected, release = asyncio.Event(), self.gate()
            release.set()

            def hold_close(_profile):
                entered.set()
                # Success requires explicit release by the test. The watchdog
                # only prevents a broken synchronous close from hanging the
                # event loop; its expiry must never count as a close response.
                if not release_close.wait(WAIT_SECONDS * 2):
                    close_timed_out.set()
                close_returned.set()

            provider.on_close = hold_close
            task = await self.launch('monitor', provider, disconnected, release, stack)
            competing = None
            try:
                await self.until(entered.is_set)
                # Probe before starting any unrelated writer. A slow commit or
                # executor scheduling delay is not evidence of a lock held by
                # provider I/O. Real ownership regressions fail immediately.
                acquired = self.database.browser_surface_lock.acquire(blocking=False)
                self.assertTrue(acquired, 'provider close held the global window admission lock')
                self.database.browser_surface_lock.release()
                assert_database_writer_available(self, self.database)
                self.assertIn('monitor', self.leases(), 'closing window must retain its lease')
                competing = asyncio.create_task(self.service.acquire_browser_lease_async(self.owner, 'unrelated', operation_type='account', entity_id='other'))
                done, _ = await asyncio.wait({competing}, timeout=WAIT_SECONDS)
                self.assertIn(competing, done, 'unrelated window admission did not finish while provider close stayed pending')
                token = competing.result()  # Exceptions must not count as admission.
                self.assertTrue(token)
                self.assertFalse(close_returned.is_set(), 'provider close must stay pending through admission')
                self.assertFalse(task.done())
                self.assertEqual({'monitor', 'unrelated'}, set(self.leases()))
                with self.assertRaises(ConflictError):
                    await self.service.acquire_browser_lease_async(self.owner, 'monitor', operation_type='account', entity_id='must-not-steal')
            finally:
                release_close.set()
                try:
                    await asyncio.wait_for(task, WAIT_SECONDS)
                finally:
                    if competing:
                        token = await asyncio.wait_for(competing, WAIT_SECONDS)
                        self.service.release_browser_lease('unrelated', token)
            self.assertTrue(close_returned.is_set())
            self.assertFalse(close_timed_out.is_set(), 'controlled provider close exceeded its deadlock watchdog')
            self.assertEqual({}, self.leases())

    async def test_native_cancel_signal_still_joins_cleanup_for_every_role(self):
        for role in ('monitor', 'manual', 'campaign', 'studio'):
            with self.subTest(role=role), ExitStack() as stack:
                provider = drain_cases.CloseProvider()
                disconnected, release = asyncio.Event(), self.gate()
                task = await self.launch(role, provider, disconnected, release, stack, native_cancel=True)
                try:
                    await asyncio.wait_for(disconnected.wait(), 3)
                    await asyncio.sleep(.04)
                    self.assertIn(role, self.leases())
                    self.assertEqual([], provider.closed)
                finally:
                    release.set()
                    await asyncio.wait_for(task, 5)
                self.assertNotIn(role, self.leases())

    async def test_action_reconnect_waits_for_previous_native_cleanup(self):
        manager = ActionCampaignManager(self.service, drain_cases.CloseProvider())
        release = self.gate()
        worker = SimpleNamespace(disconnect=AsyncMock(), wait_for_cleanup=AsyncMock(side_effect=release.wait), connect=AsyncMock())
        reconnect = asyncio.create_task(manager._reconnect_follow_worker(worker, 'reconnect'))
        try:
            await self.until(lambda: worker.disconnect.await_count == 1)
            await asyncio.sleep(.04)
            worker.connect.assert_not_awaited()
        finally:
            release.set()
            self.assertIsNone(await asyncio.wait_for(reconnect, 5))
        worker.connect.assert_awaited_once_with('reconnect', open_if_needed=True)
