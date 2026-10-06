"""A task cannot release a window while native cleanup or close still owns it."""
import asyncio
from contextlib import contextmanager
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.playwright_worker import PlaywrightWorker
from app.errors import ConflictError
from test_core import FakeCollectionWorker
import test_collection_drain_r56 as drain_cases
from test_runtime_r24 import WaitingManager


class TaskRetirementR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain_cases.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain_cases.CollectionDrainR56Tests.asyncTearDown
    task = drain_cases.CollectionDrainR56Tests.task
    manager = drain_cases.CollectionDrainR56Tests.manager
    gate = drain_cases.CollectionDrainR56Tests.gate
    until = drain_cases.CollectionDrainR56Tests.until
    leases = drain_cases.CollectionDrainR56Tests.leases
    async def test_native_cleanup_keeps_window_lease_through_completion_and_stop(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                release = self.gate()
                entered, collecting = asyncio.Event(), asyncio.Event()
                pages = []
                class Worker(FakeCollectionWorker):
                    # Only native cleanup is delegated. This fixture's legacy
                    # collection method does not implement the child/sink protocol.
                    supports_single_candidate_handoff = False
                    supports_candidate_batch_sink = False
                    supports_parallel_screening_tab = False
                    supports_collection_progress_sink = False
                    def __init__(self, provider):
                        super().__init__(provider)
                        self.cleanup = PlaywrightWorker(provider)
                        self.cleanup.disconnect_timeout_seconds = .01
                        async def close():
                            entered.set()
                            while not release.is_set():
                                try:
                                    await release.wait()
                                except asyncio.CancelledError:
                                    pass
                        self.cleanup.page = self.cleanup._worker_owned_page = SimpleNamespace(close=close)
                        self.cleanup._playwright = SimpleNamespace(stop=AsyncMock())
                        pages.append(self.cleanup)
                    def __getattr__(self, name):
                        return getattr(self.cleanup, name)
                    async def collect_followers(self, target, **kwargs):
                        collecting.set()
                        if stop:
                            await asyncio.Event().wait()
                        return await super().collect_followers(target, **kwargs)
                    async def disconnect(self):
                        await self.cleanup.disconnect()
                profile = f'owned-{stop}'
                task = self.task((f'source_{stop}',), (profile,), live_queue_enabled=False)
                manager, provider = self.manager(Worker)
                stopping = None
                try:
                    await manager.start(self.owner, task['id'])
                    await asyncio.wait_for(collecting.wait(), 5)
                    if stop:
                        stopping = asyncio.create_task(manager.stop(self.owner, task['id'], close_windows=False))
                    await asyncio.wait_for(entered.wait(), 5)
                    await asyncio.sleep(.07)
                    self.assertIn(profile, self.leases(), 'native cleanup still owns this window')
                    self.assertIn(task['id'], manager.active_task_ids())
                    self.assertEqual([], provider.closed)
                    with self.assertRaises(ConflictError):
                        self.service.acquire_browser_lease(self.owner, profile, operation_type='account', entity_id='competitor')
                    if stopping:
                        stopping.cancel()
                        await asyncio.sleep(0)
                        stopping.cancel()
                        self.assertFalse(stopping.done())
                finally:
                    release.set()
                    if stopping:
                        await asyncio.gather(stopping, return_exceptions=True)
                    await asyncio.wait_for(manager.wait(task['id']), 5)
                    for page in pages:
                        for _ in range(100):
                            if not page._late_lifecycle_tasks:
                                break
                            await asyncio.sleep(.01)
                self.assertNotIn(profile, self.leases())

    async def test_stop_retries_negative_close_ack_without_releasing_active_or_inactive_window(self):
        for active in (True, False):
            with self.subTest(active=active):
                profile = f'close-{active}'
                task = self.task((f'close_source_{active}',), (profile,))
                manager, provider = self.manager(manager_type=WaitingManager)
                provider.result = {'closed': False, 'window_state': 'closing'}
                if active:
                    await manager.start(self.owner, task['id'])
                stopping = asyncio.create_task(manager.stop(self.owner, task['id']))
                try:
                    await self.until(lambda: bool(provider.closed))
                    await asyncio.sleep(.04)
                    self.assertIn(profile, self.leases(), 'close request is not a close acknowledgement')
                    self.assertFalse(stopping.done())
                    with self.assertRaises(ConflictError):
                        self.service.acquire_browser_lease(self.owner, profile, operation_type='account', entity_id='competitor')
                finally:
                    provider.result = {'closed': True}
                    await asyncio.wait_for(stopping, 5)
                self.assertNotIn(profile, self.leases())
                self.assertGreaterEqual(len(provider.closed), 2)

    async def test_delete_and_concurrent_stop_keep_failed_close_owned_until_retry_succeeds(self):
        task = self.task()
        manager, provider = self.manager(manager_type=WaitingManager)
        provider.result = {'provider_success': False}
        await manager.start(self.owner, task['id'])
        deleting = asyncio.create_task(manager.delete_window(self.owner, task['id'], 'window-a'))
        stopping = None
        try:
            await self.until(lambda: bool(provider.closed))
            await asyncio.sleep(.04)
            self.assertIn('window-a', self.leases())
            self.assertFalse(deleting.done())
            stopping = asyncio.create_task(manager.stop(self.owner, task['id']))
            await asyncio.sleep(.04)
            self.assertIn('window-a', self.leases())
            self.assertFalse(stopping.done())
        finally:
            provider.result = {'closed': True}
            await asyncio.wait_for(deleting, 5)
            if stopping:
                await asyncio.wait_for(stopping, 5)
        self.assertNotIn('window-a', self.leases())

    async def test_continue_cannot_rearm_target_during_task_wide_close_retry(self):
        task = self.task()
        manager, provider = self.manager(manager_type=WaitingManager)
        provider.result = {'closed': False}
        await manager.start(self.owner, task['id'])
        target = task['targets'][0]['id']
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'running', window_id='window-a')
        stopping = asyncio.create_task(manager.stop(self.owner, task['id']))
        try:
            await self.until(lambda: bool(provider.closed))
            await asyncio.sleep(.04)
            before = self.service.get_task(self.owner, task['id'])['targets'][0]
            with self.assertRaises(ConflictError):
                await manager.resume_window(self.owner, task['id'], 'window-a')
            self.assertEqual(before, self.service.get_task(self.owner, task['id'])['targets'][0])
        finally:
            provider.result = {'closed': True}
            await asyncio.wait_for(stopping, 5)

    async def test_slow_close_ownership_probe_does_not_block_event_loop(self):
        manager, provider = self.manager()
        token = self.service.acquire_browser_lease(self.owner, 'slow-close', operation_type='account', entity_id='closing')
        loop = asyncio.get_running_loop()
        armed, entered, release = threading.Event(), threading.Event(), threading.Event()
        blocked = []
        read = self.database.read
        def close(_profile):
            if not armed.is_set():
                armed.set()
                return {'closed': False}
            return {'closed': True}
        provider.close_profile = close
        @contextmanager
        def slow_read():
            with read() as connection:
                if armed.is_set() and not entered.is_set():
                    entered.set()
                    # Only an unblocked event loop can acknowledge this probe.
                    loop.call_soon_threadsafe(release.set)
                    blocked.append(not release.wait(1))
                yield connection
        try:
            with patch.object(self.database, 'read', slow_read):
                await asyncio.wait_for(manager._close_profiles_until_confirmed({'slow-close': token}), 5)
            self.assertTrue(entered.is_set())
            self.assertEqual([False], blocked, 'SQLite close retry blocked all task scheduling')
        finally:
            release.set()
            self.service.release_browser_lease('slow-close', token)

    async def test_inactive_close_keeps_expired_lease_protected_during_native_io(self):
        task = self.task()
        manager, provider = self.manager()
        entered, release = threading.Event(), self.gate(threaded=True)
        def close(_profile):
            entered.set()
            release.wait(5)
            return {'closed': True}
        provider.close_profile = close
        stopping = asyncio.create_task(manager.stop(self.owner, task['id']))
        try:
            await self.until(entered.is_set)
            with self.database.write() as connection:
                connection.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01T00:00:00+00:00'")
            self.assertIn('window-a', self.leases())
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='account', entity_id='competitor')
            self.assertFalse(stopping.done())
        finally:
            release.set()
            await asyncio.wait_for(stopping, 5)
        self.assertNotIn('window-a', self.leases())
