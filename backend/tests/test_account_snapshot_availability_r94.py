"""The account view remains readable while the optional desktop inventory stalls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import Settings
from app.database import Database
from app.errors import UpstreamUnavailableError
from app.main import create_app
from app.service import CoreService
from app.snapshot_runtime import SnapshotInventoryReader, SnapshotJSONResponse


class Request:
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


class Inventory:
    def __init__(self):
        self.native = self
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.rows = []

    def inventory(self):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError('fixture was not released')
        return self.rows

    def list_all_windows(self):
        return {'windows': self.inventory(), 'connection': {'connected': True, 'provider': 'native'}}


class AccountSnapshotAvailabilityR94Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'core.sqlite3')
        self.db.initialize()
        service = CoreService(self.db)
        self.owner = service.register_user('independent-account', 'snapshot-test-password')
        self.inventory = Inventory()
        self.addCleanup(self.inventory.release.set)
        self.inventory.rows = [dict(id='w1', name='Owned window', is_open=True,
                                   owner_user_id=self.owner['id'], ready=True),
                               dict(id='foreign', name='Other owner', is_open=True,
                                   owner_user_id='another-owner')]
        self.app = create_app(Settings(startup_token='account-snapshot-startup-token',
            database_path=self.db.path, data_dir=Path(self.temp.name)),
            database=self.db, bitbrowser=self.inventory)
        self.app.state.snapshot_inventory.timeout_seconds = .05
        self.plan = self.app.state.accounts.save(self.owner['id'], {'name': 'Saved account'})
        self.route = next(r.endpoint for r in self.app.routes
                          if getattr(r, 'path', '') == '/api/accounts/snapshot')
        self.addAsyncCleanup(self.app.state.snapshot_inventory.close)
        self.addAsyncCleanup(self.release_inventory)

    async def release_inventory(self):
        self.inventory.release.set()

    async def account(self, request=None, owner=None):
        response = await self.route((owner or self.owner, 'session'), request or Request())
        return json.loads(response.body) if hasattr(response, 'body') else response

    async def test_repeated_account_reads_cannot_starve_live_status_workers(self):
        # Emulate rapid page switches/retries while an account database read is
        # stalled. A small real executor makes saturation deterministic.
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
        entered, release = asyncio.Event(), threading.Event()
        original = self.app.state.accounts.snapshot
        calls = []

        def held(*args, **kwargs):
            calls.append(1)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise TimeoutError('account read fixture was not released')
            return original(*args, **kwargs)

        live_route = next(r.endpoint for r in self.app.routes
                          if getattr(r, 'path', '') == '/api/workbench/live-status')
        jobs = []
        with patch.object(self.app.state.accounts, 'snapshot', side_effect=held):
            try:
                jobs.append(asyncio.create_task(self.account()))
                await asyncio.wait_for(entered.wait(), 2)
                jobs.extend(asyncio.create_task(self.account()) for _ in range(11))
                # Let the retries reach their worker submission points.
                await asyncio.sleep(.1)
                live = asyncio.create_task(live_route((self.owner, 'session')))
                jobs.append(live)
                done, _ = await asyncio.wait({live}, timeout=1)
                self.assertIn(live, done, 'account retries exhausted workers needed by task live status')
                self.assertEqual(1, len(calls), 'same-owner account database reads overlapped')
                self.assertFalse(jobs[0].done(), 'slow read must remain held for the responsiveness check')
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_cancelled_request_keeps_fence_until_real_read_settles(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.accounts.snapshot
        calls = []

        def held(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('account read was not released')
            return original(*args, **kwargs)

        jobs = []
        with patch.object(self.app.state.accounts, 'snapshot', side_effect=held):
            try:
                first = asyncio.create_task(self.account())
                jobs.append(first)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                first.cancel()
                second = asyncio.create_task(self.account())
                jobs.append(second)
                await asyncio.sleep(.1)
                self.assertFalse(first.done(), 'cancel released a still-running SQLite worker')
                self.assertFalse(second.done())
                self.assertEqual(1, len(calls))
                release.set()
                results = await asyncio.gather(*jobs, return_exceptions=True)
                self.assertIsInstance(results[0], asyncio.CancelledError)
                self.assertEqual(['Saved account'], [p['name'] for p in results[1]['plans']])
                self.assertEqual(2, len(calls))
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_disconnected_queued_read_does_not_start_database_work(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.accounts.snapshot
        calls = []

        def held(*args, **kwargs):
            calls.append(1)
            entered.set()
            if not release.wait(10):
                raise TimeoutError('account read was not released')
            return original(*args, **kwargs)

        request = Request()
        jobs = []
        with patch.object(self.app.state.accounts, 'snapshot', side_effect=held):
            try:
                jobs.append(asyncio.create_task(self.account()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                jobs.append(asyncio.create_task(self.account(request)))
                await asyncio.sleep(0)
                request.disconnected = True
                release.set()
                results = await asyncio.gather(*jobs, return_exceptions=True)
                self.assertIsInstance(results[1], UpstreamUnavailableError)
                self.assertEqual(1, len(calls))
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_other_owner_read_is_independent_and_failure_allows_retry(self):
        other = self.app.state.service.register_user('other-account', 'snapshot-test-password')
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.accounts.snapshot

        def held(owner, **kwargs):
            if owner == self.owner['id']:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('account read was not released')
                raise RuntimeError('controlled account read failure')
            return original(owner, **kwargs)

        with patch.object(self.app.state.accounts, 'snapshot', side_effect=held):
            job = asyncio.create_task(self.account())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                independent = await asyncio.wait_for(self.account(owner=other), 1)
                self.assertEqual([], independent['plans'])
                self.assertFalse(job.done())
                release.set()
                with self.assertRaisesRegex(RuntimeError, 'controlled account read failure'):
                    await job
            finally:
                release.set()
                await asyncio.gather(job, return_exceptions=True)
        recovered = await asyncio.wait_for(self.account(), 1)
        self.assertEqual(['Saved account'], [p['name'] for p in recovered['plans']])

    async def test_cancelled_encoder_keeps_fence_and_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        calls = []

        def held_encoder(*args, **kwargs):
            self.assertNotEqual(loop_thread, threading.get_ident())
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('encoder was not released')
            return SnapshotJSONResponse(*args, **kwargs)

        jobs = []
        with patch('app.main.SnapshotJSONResponse', side_effect=held_encoder), \
                patch.object(self.app.state.accounts, 'snapshot', wraps=self.app.state.accounts.snapshot) as read:
            try:
                first = asyncio.create_task(self.account())
                jobs.append(first)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                first.cancel()
                jobs.append(asyncio.create_task(self.account()))
                await asyncio.sleep(.1)
                self.assertFalse(first.done())
                self.assertEqual(1, read.call_count, 'retry entered while prior encoding was still owned')
                release.set()
                results = await asyncio.gather(*jobs, return_exceptions=True)
                self.assertIsInstance(results[0], asyncio.CancelledError)
                self.assertEqual('Saved account', results[1]['plans'][0]['name'])
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_queued_read_gets_new_durable_lease_instead_of_cached_response(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.accounts.snapshot
        calls = []

        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('old account result was not released')
            return result

        jobs = []
        with patch.object(self.app.state.accounts, 'snapshot', side_effect=held):
            try:
                jobs.append(asyncio.create_task(self.account()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                jobs.append(asyncio.create_task(self.account()))
                service = self.app.state.service
                task = service.create_task(self.owner['id'], name='New task', modes=['followers'],
                    targets=['source'], window_ids=['w1'], settings={})
                token = service.acquire_browser_lease(self.owner['id'], 'w1',
                    operation_type='collection', entity_id=task['id'])
                release.set()
                old, fresh = await asyncio.gather(*jobs)
                self.assertNotIn('w1', old['locks'])
                self.assertEqual('collection', fresh['locks']['w1']['operation_type'])
                self.assertIn(token, self.db.live_browser_lease_tokens)
                self.assertEqual(2, len(calls))
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_cold_account_read_returns_saved_plans_while_desktop_is_held(self):
        self.inventory.release.clear()
        job = asyncio.create_task(self.account())
        try:
            done, _ = await asyncio.wait({job}, timeout=1)
            self.assertIn(job, done, 'account page still waits for optional desktop inventory')
            payload = job.result()
            self.assertEqual(['Saved account'], [p['name'] for p in payload['plans']])
            self.assertTrue(payload['inventory_stale'])
            self.assertFalse(payload['connection']['connected'])
            again = await asyncio.wait_for(asyncio.gather(*(self.account() for _ in range(8))), 1)
            self.assertTrue(all(item['inventory_stale'] for item in again))
            self.assertEqual(1, self.inventory.calls, 'retry launched a second desktop worker')
        finally:
            self.inventory.release.set()
            await asyncio.gather(job, return_exceptions=True)

    async def test_account_inventory_is_complete_owner_filtered_and_retains_current_task_lock(self):
        first = await self.account()
        self.assertEqual(['w1'], [r['id'] for r in first['windows']])
        self.assertEqual('Owned window', first['windows'][0]['name'])
        task = self.app.state.service.create_task(self.owner['id'], name='Active',
            modes=['followers'], targets=['source'], window_ids=['w1'], settings={})
        token = self.app.state.service.acquire_browser_lease(self.owner['id'], 'w1',
            operation_type='collection', entity_id=task['id'])
        self.inventory.release.clear()
        job = asyncio.create_task(self.account())
        try:
            done, _ = await asyncio.wait({job}, timeout=1)
            self.assertIn(job, done)
            payload = job.result()
            self.assertTrue(payload['inventory_stale'])
            self.assertEqual('collection', payload['locks']['w1']['operation_type'])
            self.assertEqual('unknown', payload['windows'][0]['window_state'])
            self.assertFalse(payload['windows'][0]['ready'])
            self.assertIn(token, self.db.live_browser_lease_tokens)
        finally:
            self.inventory.release.set()
            await asyncio.gather(job, return_exceptions=True)

    async def test_read_delivers_late_completed_inventory_instead_of_discarding_it(self):
        self.inventory.release.clear()
        reader = SnapshotInventoryReader(self.inventory.list_all_windows, timeout_seconds=.01)
        try:
            self.assertTrue((await reader.read())['stale'])
            self.inventory.release.set()
            await asyncio.wait_for(asyncio.shield(reader._pending), 2)
            # Every request would exceed the deadline: the next poll must consume
            # the completed read, not start another slow probe and return stale forever.
            self.inventory.release.clear()
            result = await reader.read()
            self.assertFalse(result.get('stale', False))
            self.assertTrue(result['connection']['connected'])
            self.assertEqual(1, self.inventory.calls)
        finally:
            self.inventory.release.set()
            await reader.close()

    async def test_authenticated_account_route_works_during_held_full_snapshot(self):
        service = self.app.state.service
        login = service.login('independent-account', 'snapshot-test-password')
        headers = {'X-Startup-Token': self.app.state.settings.startup_token,
                   'Authorization': 'Bearer ' + login['token']}
        entered, release = threading.Event(), threading.Event()
        original = service.get_workbench_snapshot
        def held(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('business fixture not released')
            return original(*args, **kwargs)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            with patch.object(service, 'get_workbench_snapshot', side_effect=held):
                job = asyncio.create_task(client.get('/api/workbench/snapshot', headers=headers))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    response = await asyncio.wait_for(client.get('/api/accounts/snapshot', headers=headers), 1)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual('Owned window', response.json()['windows'][0]['name'])
                    self.assertFalse(job.done(), 'fixture must still hold the global snapshot')
                    command = await asyncio.wait_for(client.post('/api/accounts/command', headers=headers,
                        json={'action': 'notes', 'id': self.plan['id'], 'revision': 1, 'notes': 'Saved while history waits'}), 1)
                    self.assertEqual(200, command.status_code)
                    self.assertEqual('Saved while history waits', self.app.state.accounts.get(self.owner['id'], self.plan['id'])['notes'])
                    denied = await client.get('/api/accounts/snapshot', headers={'X-Startup-Token': headers['X-Startup-Token']})
                    self.assertEqual(401, denied.status_code)
                finally:
                    release.set()
                    await asyncio.gather(job, return_exceptions=True)

    async def test_cancelled_inventory_waiter_does_not_cancel_shared_worker(self):
        self.inventory.release.clear()
        reader = SnapshotInventoryReader(self.inventory.list_all_windows, timeout_seconds=.05)
        first = asyncio.create_task(reader.read())
        try:
            self.assertTrue(await asyncio.to_thread(self.inventory.entered.wait, 2))
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            self.assertTrue((await reader.read())['stale'])
            self.assertEqual(1, self.inventory.calls)
            self.inventory.release.set()
            await asyncio.wait_for(asyncio.shield(reader._pending), 2)
            result = await reader.read()
            self.assertTrue(result['connection']['connected'])
            self.assertEqual(1, self.inventory.calls)
        finally:
            self.inventory.release.set()
            await reader.close()
