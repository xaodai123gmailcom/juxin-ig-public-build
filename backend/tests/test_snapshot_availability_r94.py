"""Real WAL reads must remain usable while optional work is blocked."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import fastapi.routing
from starlette.responses import JSONResponse

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import CoreService
from app.snapshot_runtime import SnapshotInventoryReader, SnapshotJSONResponse, run_transient_maintenance
from support.concurrency_probe import ThreadGroup


class Request:
    async def is_disconnected(self):
        return False


class Inventory:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def list_all_windows(self):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError('fixture inventory not released')
        return {'windows': [{'id': 'window-1', 'name': 'Window', 'is_open': True}],
                'connection': {'connected': True, 'state': 'ready'}}


class SnapshotAvailabilityR94Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'core.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('snapshot-availability', 'snapshot-test-password')
        self.inventory = Inventory()
        self.addCleanup(self.inventory.release.set)
        settings = Settings(startup_token='snapshot-test-startup-token',
                            database_path=self.db.path, data_dir=Path(temporary.name))
        self.app = create_app(settings, database=self.db, bitbrowser=self.inventory)
        self.endpoint = next(r.endpoint for r in self.app.routes if getattr(r, 'path', '') == '/api/workbench/snapshot')
        self.threads = ThreadGroup(self)

    async def snapshot(self):
        response = await self.endpoint(Request(), (self.owner, 'test-session'), limit=10, history_limit=10)
        return json.loads(response.body)

    def claim_window(self):
        task = self.service.create_task(self.owner['id'], name='Active', modes=['followers'],
            targets=['source_account'], window_ids=['window-1'], settings={})
        self.service.set_task_runtime_status(self.owner['id'], task['id'], 'running')
        token = self.service.acquire_browser_lease(self.owner['id'], 'window-1',
            operation_type='collection', entity_id=task['id'])
        return task, token

    async def while_writer_held(self, *, external):
        task, token = self.claim_window()
        entered, release = threading.Event(), self.threads.release_event()

        def writer():
            if external:
                c = sqlite3.connect(self.db.path, isolation_level=None)
                try:
                    c.execute('BEGIN IMMEDIATE'); entered.set(); release.wait(10)
                finally:
                    c.rollback(); c.close()
            else:
                with self.db.write():
                    entered.set(); release.wait(10)

        thread, _, errors = self.threads.start(writer)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        read = asyncio.create_task(self.snapshot())
        try:
            done, _ = await asyncio.wait({read}, timeout=3)
            self.assertIn(read, done, 'snapshot still waits for a writer through window-lease reconciliation')
            snapshot = read.result()
            self.assertEqual(task['id'], snapshot['windows'][0]['lock_entity_id'])
            self.assertTrue(snapshot['windows'][0]['locked'])
            self.assertIn(token, self.db.live_browser_lease_tokens)
        finally:
            release.set()
            await asyncio.gather(read, return_exceptions=True)
            await asyncio.to_thread(thread.join, 3)
        self.assertEqual([], errors)

    async def test_full_snapshot_returns_with_live_lease_while_collection_writer_is_held(self):
        await self.while_writer_held(external=False)

    async def test_full_snapshot_returns_with_live_lease_while_external_sqlite_writer_is_held(self):
        await self.while_writer_held(external=True)

    async def test_http_snapshot_does_not_execute_optional_cleanup(self):
        with patch.object(self.db, 'maintain_transient_data', side_effect=AssertionError('cleanup ran inside snapshot')):
            self.assertEqual(0, (await self.snapshot())['counts']['total_collected'])

    async def test_http_snapshot_encoding_does_not_run_on_collection_event_loop(self):
        login = self.service.login('snapshot-availability', 'snapshot-test-password')
        headers = {'X-Startup-Token': self.app.state.settings.startup_token,
                   'Authorization': 'Bearer ' + login['token']}
        loop_thread = threading.get_ident()
        rendering_threads = []
        original = SnapshotJSONResponse.render
        original_serialize = fastapi.routing.serialize_response
        async def serialize(**kwargs):
            content = kwargs.get('response_content')
            if isinstance(content, dict) and 'snapshot_seq' in content and 'pending' in content:
                rendering_threads.append(threading.get_ident())
            return await original_serialize(**kwargs)
        def render(response, content):
            if isinstance(content, dict) and 'snapshot_seq' in content and 'pending' in content:
                rendering_threads.append(threading.get_ident())
            return original(response, content)
        with patch.object(SnapshotJSONResponse, 'render', render), \
             patch.object(fastapi.routing, 'serialize_response', side_effect=serialize):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
                response = await client.get('/api/workbench/snapshot', headers=headers)
        self.assertEqual(200, response.status_code)
        self.assertTrue(rendering_threads, 'the actual HTTP snapshot response must be encoded')
        self.assertNotIn(loop_thread, rendering_threads, 'large snapshot encoding still blocks collection and controls')
        self.assertEqual(0, response.json()['counts']['total_collected'])

    async def test_slow_encoding_keeps_controls_responsive_and_cancel_retains_snapshot_fence(self):
        entered, release = threading.Event(), self.threads.release_event()
        loop_thread = threading.get_ident()
        original = SnapshotJSONResponse.render
        encoded = []
        def render(response, content):
            if isinstance(content, dict) and 'snapshot_seq' in content and 'pending' in content:
                self.assertNotEqual(loop_thread, threading.get_ident())
                encoded.append(1)
                if len(encoded) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError('encoding fixture was not released')
            return original(response, content)
        with patch.object(SnapshotJSONResponse, 'render', render):
            first = asyncio.create_task(self.snapshot())
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                # A bound runtime control must execute on the event loop even
                # while the snapshot serializer is event-held in another thread.
                acted = asyncio.Event()
                asyncio.get_running_loop().call_soon(acted.set)
                await asyncio.wait_for(acted.wait(), 3)
                first.cancel()
                second = asyncio.create_task(self.snapshot())
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertFalse(first.done(), 'cancel abandoned the response encoder')
                self.assertFalse(second.done(), 'retry passed the owned serialization fence')
                self.assertEqual(1, len(encoded))
            finally:
                release.set()
                result = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertIsInstance(result[1], dict)

    async def test_background_encoder_preserves_typed_json_semantics(self):
        original = self.app.state.service.get_workbench_snapshot
        def data(*args, **kwargs):
            result = original(*args, **kwargs)
            # Historical/adaptor evidence can contain non-finite unknown values.
            # The old typed FastAPI route encoded these as null, never NaN tokens.
            result['storage']['fixture_unknown'] = float('nan')
            result['storage']['fixture_nonfinite'] = [float('inf'), -float('inf')]
            return result
        with patch.object(self.app.state.service, 'get_workbench_snapshot', side_effect=data):
            response = await self.endpoint(Request(), (self.owner, 'test-session'), limit=10, history_limit=10)
        payload = json.loads(response.body)
        self.assertIsNone(payload['storage']['fixture_unknown'])
        self.assertEqual([None, None], payload['storage']['fixture_nonfinite'])
        self.assertEqual('application/json', response.headers['content-type'])

    async def test_slow_inventory_does_not_block_business_snapshot_and_retries_do_not_overlap(self):
        self.inventory.release.clear()
        first = asyncio.create_task(self.snapshot())
        second = None
        try:
            done, _ = await asyncio.wait({first}, timeout=3.5)
            self.assertIn(first, done, 'optional inventory still blocks the entire workbench')
            snapshot = first.result()
            self.assertEqual(0, snapshot['counts']['total_collected'])
            self.assertTrue(snapshot['connection']['inventory_stale'])
            second = asyncio.create_task(self.snapshot())
            done, _ = await asyncio.wait({second}, timeout=3.5)
            self.assertIn(second, done)
            self.assertEqual(1, self.inventory.calls, 'timed-out inventory spawned another provider thread')
        finally:
            self.inventory.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            reader = getattr(self.app.state, 'snapshot_inventory', None)
            if reader:
                await reader.close()

    async def test_provider_outage_preserves_known_windows_and_current_lease_fences(self):
        await self.snapshot()
        task, _ = self.claim_window()
        from app.errors import UpstreamUnavailableError
        with patch.object(self.inventory, 'list_all_windows', side_effect=UpstreamUnavailableError('desktop unavailable')):
            snapshot = await self.snapshot()
        self.assertEqual(1, len(snapshot['windows']), 'temporary inventory failure erased the selector')
        window = snapshot['windows'][0]
        self.assertTrue(window['locked'])
        self.assertEqual(task['id'], window['lock_entity_id'])
        self.assertFalse(window['ready'])
        self.assertTrue(snapshot['connection']['inventory_stale'])
        recovered = await self.snapshot()
        self.assertFalse(recovered['connection'].get('inventory_stale', False))
        self.assertTrue(recovered['windows'][0]['opened'])

    async def test_cancelling_request_does_not_release_owner_fence_while_sqlite_thread_runs(self):
        original = self.app.state.service.get_workbench_snapshot
        entered, release = threading.Event(), self.threads.release_event()
        calls = []

        def gated(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set(); release.wait(10)
            return original(*args, **kwargs)

        with patch.object(self.app.state.service, 'get_workbench_snapshot', side_effect=gated):
            first = asyncio.create_task(self.snapshot())
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                first.cancel()
                await asyncio.sleep(0)
                second = asyncio.create_task(self.snapshot())
                # Allow the competing route to reach its owner fence. The first
                # thread is event-held; this is not a disk speed measurement.
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertEqual(1, len(calls), 'cancelled route released ownership before the SQLite worker ended')
                self.assertFalse(first.done())
            finally:
                release.set()
                results = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIsInstance(results[1], dict)

    async def test_deferred_stale_lease_cleanup_keeps_lock_until_idle_then_releases(self):
        task, token = self.claim_window()
        self.db.live_browser_lease_tokens.discard(token)
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01'")
        blocker = sqlite3.connect(self.db.path, isolation_level=None, timeout=0)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            snapshot = await asyncio.wait_for(self.snapshot(), 3)
            self.assertTrue(snapshot['windows'][0]['locked'])
            self.assertEqual(task['id'], snapshot['windows'][0]['lock_entity_id'])
        finally:
            blocker.rollback(); blocker.close()
        snapshot = await self.snapshot()
        self.assertFalse(snapshot['windows'][0]['locked'])

    async def test_cached_inventory_is_filtered_for_each_login_and_cannot_leak_task_ids(self):
        task, _ = self.claim_window()
        original = self.inventory.list_all_windows
        def owned():
            payload = original()
            payload['windows'][0]['owner_user_id'] = self.owner['id']
            return payload
        with patch.object(self.inventory, 'list_all_windows', side_effect=owned):
            await self.snapshot()
        other = self.service.register_user('snapshot-other-owner', 'snapshot-test-password')
        from app.errors import UpstreamUnavailableError
        with patch.object(self.inventory, 'list_all_windows', side_effect=UpstreamUnavailableError('offline')):
            response = await self.endpoint(Request(), (other, 'other-token'), limit=10, history_limit=10)
            result = json.loads(response.body)
        self.assertEqual([], result['windows'])
        self.assertNotIn(task['id'], str(result))

    async def test_background_cleanup_does_not_hold_snapshot_and_stop_joins_real_pass(self):
        entered, release = threading.Event(), self.threads.release_event()
        stop = asyncio.Event()
        def held_cleanup():
            with self.db.write():
                entered.set(); release.wait(10)
        with patch.object(self.db, 'maintain_transient_data', side_effect=held_cleanup):
            job = asyncio.create_task(run_transient_maintenance(self.db, stop, interval_seconds=.01))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                snapshot = await asyncio.wait_for(self.snapshot(), 3)
                self.assertEqual(0, snapshot['counts']['total_collected'])
                stop.set()
                await asyncio.sleep(0)
                self.assertFalse(job.done(), 'shutdown abandoned a cleanup thread holding a write transaction')
            finally:
                release.set(); stop.set()
                await asyncio.wait_for(job, 3)

    async def test_background_cleanup_failure_is_logged_and_next_pass_retries(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = []
        def cleanup():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError('fault injection')
            loop.call_soon_threadsafe(stop.set)
        with patch.object(self.db, 'maintain_transient_data', side_effect=cleanup), \
                self.assertLogs('app.snapshot_runtime', level='ERROR') as log:
            await asyncio.wait_for(run_transient_maintenance(self.db, stop, interval_seconds=.01), 3)
        self.assertEqual(2, len(calls))
        self.assertIn('fault injection', '\n'.join(log.output))


class SnapshotInventoryR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_late_provider_failure_is_observed_and_next_call_recovers(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 1:
                entered.set(); release.wait(10)
                raise RuntimeError('late inventory fault')
            return {'windows': [], 'connection': {'connected': True}}
        reader = SnapshotInventoryReader(load, timeout_seconds=.02)
        try:
            first = await reader.read()
            self.assertTrue(first['connection']['inventory_stale'])
            release.set()
            await asyncio.gather(reader._pending, return_exceptions=True)
            recovered = await reader.read()
            self.assertTrue(recovered['connection']['connected'])
            self.assertEqual(2, len(calls))
        finally:
            release.set(); await reader.close()

    async def test_invalid_inventory_preserves_cache_without_mutating_it(self):
        payload = {'windows': [{'id': 'w1', 'name': 'Window', 'is_open': True, 'ready': True}],
                   'connection': {'connected': True}}
        reader = SnapshotInventoryReader(lambda: payload)
        try:
            first = await reader.read()
            first['windows'][0]['name'] = 'caller mutated'
            for invalid in ({'windows': None}, {'windows': [None]}, {'windows': [], 'connection': None}):
                reader.load = lambda: invalid
                fallback = await reader.read()
                self.assertEqual('Window', fallback['windows'][0]['name'])
                self.assertFalse(fallback['windows'][0]['ready'])
                self.assertTrue(fallback['connection']['inventory_stale'])
                fallback['windows'].clear()
            reader.load = lambda: payload
            recovered = await reader.read()
            self.assertTrue(recovered['windows'][0]['ready'])
        finally:
            await reader.close()

    async def test_close_waits_for_rpc_and_cannot_launch_another_inventory(self):
        release = threading.Event()
        calls = []
        def load():
            calls.append(1); release.wait(10)
            return {'windows': []}
        reader = SnapshotInventoryReader(load, timeout_seconds=.02)
        close = None
        try:
            await reader.read()
            close = asyncio.create_task(reader.close())
            await asyncio.sleep(0)
            self.assertFalse(close.done())
            self.assertTrue((await reader.read())['connection']['inventory_stale'])
            self.assertEqual(1, len(calls))
        finally:
            release.set()
            await asyncio.wait_for(close or reader.close(), 3)


if __name__ == '__main__':
    unittest.main()
