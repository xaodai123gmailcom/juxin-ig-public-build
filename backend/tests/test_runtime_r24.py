"""Collection ownership regressions; SQLite and controlled cleanup, no browser."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionManager


class CloseClient:
    def __init__(self):
        self.closed = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def close_profile(self, profile_id):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError('Test did not release browser close')
        self.closed.append(profile_id)


class WaitingManager(ExecutionManager):
    async def _window_loop(self, control, profile_id, *args):
        await control.stop_event.wait()


class RuntimeR24Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.service import CoreService
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'runtime.sqlite3')
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user('runtime-r24', 'test password sufficiently long')['id']
        self.browser = CloseClient()
        self.manager = WaitingManager(self.service, self.browser, lease_heartbeat_interval_seconds=.02)
        self.task = self.service.create_task(self.owner, name='r24', modes=['followers'],
            targets=['source_r24'], window_ids=['window-one'],
            settings={'local_person_recognition': False, 'live_queue_enabled': True})

    async def asyncTearDown(self):
        self.browser.release.set()
        await self.manager.shutdown()
        # Clean failed-baseline fixtures too: a broken coordinator can leave its
        # independent heartbeat alive, which is precisely what these tests expose.
        remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def wait_for_close(self):
        async with asyncio.timeout(2):
            while not self.browser.entered.is_set():
                await asyncio.sleep(.002)

    async def test_stale_stop_never_closes_window_now_owned_by_another_task(self):
        token = self.service.acquire_browser_lease(self.owner, 'window-one',
            operation_type='action', entity_id='running-greeting')
        try:
            result = await self.manager.stop(self.owner, self.task['id'])
            self.assertEqual('stopped', result['status'])
            self.assertEqual([], self.browser.closed)
            with self.database.read() as connection:
                row = connection.execute('SELECT entity_id,lease_token FROM browser_operation_leases').fetchone()
            self.assertEqual(('running-greeting', token), tuple(row))
        finally:
            self.service.release_browser_lease('window-one', token)

    async def test_stale_close_fences_window_until_thread_finishes_despite_cancellation(self):
        self.browser.release.clear()
        stopping = asyncio.create_task(self.manager.stop(self.owner, self.task['id']))
        try:
            await self.wait_for_close()
            stopping.cancel()
            await asyncio.sleep(.01)
            stopping.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(stopping.done())
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'window-one',
                    operation_type='monitor', entity_id='competing-monitor')
        finally:
            self.browser.release.set()
            await asyncio.gather(stopping, return_exceptions=True)
        self.assertEqual(['window-one'], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_active_stop_repeated_cancel_completes_cleanup_and_releases_ownership(self):
        await self.manager.start(self.owner, self.task['id'])
        await asyncio.sleep(.01)
        self.browser.release.clear()
        stopping = asyncio.create_task(self.manager.stop(self.owner, self.task['id']))
        try:
            await self.wait_for_close()
            stopping.cancel()
            await asyncio.sleep(.01)
            stopping.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(stopping.done())
            self.assertIn(self.task['id'], self.manager.active_task_ids())
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'window-one',
                    operation_type='monitor', entity_id='competing-monitor')
        finally:
            self.browser.release.set()
            await asyncio.gather(stopping, return_exceptions=True)
        self.assertEqual(['window-one'], self.browser.closed)
        self.assertEqual(set(), self.manager.active_task_ids())
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        self.assertNotIn(self.task['id'], self.manager._runs)

    async def test_window_progress_is_not_refreshed_by_siblings_or_network_probe(self):
        from unittest.mock import patch
        await self.manager.start(self.owner, self.task['id'])
        await self.manager.add_windows(self.owner, self.task['id'], ['window-two'])
        control = self.manager._runs[self.task['id']]
        with patch.object(self.manager, '_utc_iso', return_value='2026-09-15T00:00:01+00:00'):
            self.manager._profile_state_locked(control, 'window-one', state='working',
                target={'id': 'first', 'username': 'first'})
            self.manager._record_profile_progress(control, 'window-one')
        with patch.object(self.manager, '_utc_iso', return_value='2026-09-15T00:04:01+00:00'):
            self.manager._profile_state_locked(control, 'window-two', state='working',
                target={'id': 'second', 'username': 'second'})
            self.manager._record_profile_progress(control, 'window-two')
            self.manager._profile_state_locked(control, 'window-one', state='working',
                target={'id': 'first', 'username': 'first'},
                last_success_at='2026-09-15T00:04:01+00:00')
        diagnostic = await self.manager.runtime_diagnostics(self.owner, self.task['id'])
        states = {state['profile_id']: state for state in diagnostic['profile_states']}
        self.assertEqual('2026-09-15T00:00:01+00:00', states['window-one']['last_progress_at'])
        self.assertEqual('2026-09-15T00:04:01+00:00', states['window-two']['last_progress_at'])
        self.assertEqual('2026-09-15T00:04:01+00:00', diagnostic['last_progress_at'])

    async def test_new_target_resets_progress_clock_and_idle_clears_it(self):
        from unittest.mock import patch
        await self.manager.start(self.owner, self.task['id'])
        control = self.manager._runs[self.task['id']]
        with patch('app.execution_manager.ExecutionManager._utc_iso', return_value='2026-09-15T00:00:01+00:00'):
            self.manager._profile_state_locked(control, 'window-one', state='working',
                target={'id': 'first', 'username': 'first'})
        with patch('app.execution_manager.ExecutionManager._utc_iso', return_value='2026-09-15T00:04:01+00:00'):
            self.manager._profile_state_locked(control, 'window-one', state='working',
                target={'id': 'second', 'username': 'second'})
        self.assertEqual('2026-09-15T00:04:01+00:00', control.profile_states['window-one']['last_progress_at'])
        self.manager._profile_state_locked(control, 'window-one', state='idle')
        self.assertIsNone(control.profile_states['window-one']['last_progress_at'])

    async def test_old_stop_completion_does_not_unregister_restarted_generation(self):
        cleaned = asyncio.Event()
        return_from_cleanup = asyncio.Event()
        original_finalize = self.manager._finalize_run
        first = True

        async def delayed_return(control, *args):
            nonlocal first
            await original_finalize(control, *args)
            if first:
                first = False
                cleaned.set()
                await return_from_cleanup.wait()

        self.manager._finalize_run = delayed_return
        await self.manager.start(self.owner, self.task['id'])
        old = self.manager._runs[self.task['id']]
        stopping = asyncio.create_task(self.manager.stop(self.owner, self.task['id']))
        try:
            await asyncio.wait_for(cleaned.wait(), 2)
            self.service.retry_task_target(self.owner, self.task["id"], self.task["targets"][0]["id"])
            await self.manager.start(self.owner, self.task['id'])
            restarted = self.manager._runs[self.task['id']]
            self.assertIsNot(old, restarted)
            return_from_cleanup.set()
            await asyncio.wait_for(stopping, 2)
            self.assertIs(restarted, self.manager._runs.get(self.task['id']))
            self.assertIn(self.task['id'], self.manager.active_task_ids())
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'window-one',
                    operation_type='monitor', entity_id='competing-monitor')
        finally:
            return_from_cleanup.set()
            await asyncio.gather(stopping, return_exceptions=True)

    async def test_delete_keeps_lease_through_close_and_joins_concurrent_stop(self):
        await self.manager.start(self.owner, self.task['id'])
        await asyncio.sleep(.01)
        self.browser.release.clear()
        deleting = asyncio.create_task(self.manager.delete_window(self.owner, self.task['id'], 'window-one'))
        stopping = None
        try:
            await self.wait_for_close()
            deleting.cancel()
            await asyncio.sleep(.01)
            deleting.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(deleting.done())
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'window-one',
                    operation_type='monitor', entity_id='competing-monitor')
            with self.assertRaises(ConflictError):
                await self.manager.resume_window(self.owner, self.task['id'], 'window-one')
            stopping = asyncio.create_task(self.manager.stop(self.owner, self.task['id']))
            await asyncio.sleep(.01)
            self.assertFalse(stopping.done())
        finally:
            self.browser.release.set()
            await asyncio.gather(deleting, *([stopping] if stopping else []), return_exceptions=True)
        self.assertEqual(['window-one'], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        self.assertEqual(set(), self.manager.active_task_ids())

    async def test_control_with_replaced_token_never_closes_new_owner_window(self):
        await self.manager.start(self.owner, self.task['id'])
        await asyncio.sleep(.01)
        control = self.manager._runs[self.task['id']]
        original_token = control.leases['window-one']
        self.service.release_browser_lease('window-one', original_token)
        replacement_token = self.service.acquire_browser_lease(self.owner, 'window-one',
            operation_type='action', entity_id='new-greeting-owner')
        try:
            await self.manager.stop(self.owner, self.task['id'])
            self.assertEqual([], self.browser.closed)
            with self.database.read() as connection:
                row = connection.execute('SELECT entity_id,lease_token FROM browser_operation_leases').fetchone()
            self.assertEqual(('new-greeting-owner', replacement_token), tuple(row))
        finally:
            self.service.release_browser_lease('window-one', replacement_token)

    async def test_deleted_lease_in_heartbeat_snapshot_does_not_fail_siblings(self):
        from app.execution_manager import ExecutionControl
        pause = asyncio.Event()
        pause.set()
        tokens = {profile: self.service.acquire_browser_lease(self.owner, profile,
                  operation_type='collection', entity_id=self.task['id'])
                  for profile in ['window-one', 'window-two']}
        control = ExecutionControl(self.owner, self.task['id'], pause, asyncio.Event(), tokens, asyncio.Queue())
        entered = threading.Event()
        release = threading.Event()
        renewed = []
        original = self.service.renew_browser_lease

        def delayed_renew(profile, token):
            if profile == 'window-one':
                entered.set()
                release.wait(2)
            original(profile, token)
            renewed.append(profile)

        self.service.renew_browser_lease = delayed_renew
        renewing = asyncio.create_task(self.manager._renew_all_leases(control))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.002)
            removed = tokens.pop('window-one')
            self.service.release_browser_lease('window-one', removed)
            release.set()
            await asyncio.wait_for(renewing, 2)
            self.assertEqual(['window-two'], renewed)
        finally:
            release.set()
            await asyncio.gather(renewing, return_exceptions=True)
            for profile, token in tokens.items():
                self.service.release_browser_lease(profile, token)
