"""Count real SQLite work and verify fresh, isolated window observations."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_workspace import AccountWorkspace
from app.database import Database
from app.embedded_browser import EmbeddedBrowser
from app.service import CoreService, isoformat
from support.concurrency_probe import WAIT_SECONDS, ThreadGroup, assert_database_writer_available


class InventoryBridge:
    def __init__(self):
        self.live = {}
        self.calls = []
        self.hook = None

    def call(self, method, **body):
        self.calls.append((method, body))
        if self.hook:
            self.hook(method, body)
        if method == 'inventory':
            return {'profiles': [{'id': key, **value} for key, value in self.live.items()]}
        if method == 'hide':
            return {'hidden': True}
        raise AssertionError(method)


class WindowPerformanceR33Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.threads = ThreadGroup(self)
        self.db = Database(Path(self.tmp.name) / 'window-read.sqlite')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('performance-owner', 'correct horse battery staple')['id']
        self.other = self.service.register_user('performance-other', 'correct horse battery staple')['id']
        self.bridge = InventoryBridge()
        self.native = EmbeddedBrowser(self.db, self.tmp.name, bridge=self.bridge)
        self.accounts = AccountWorkspace(self.service, SimpleNamespace(native=self.native))

    def _seed(self, count, *, owner=None):
        owner = owner or self.owner
        now = isoformat()
        rows = []
        with self.db.write() as connection:
            serial = connection.execute('SELECT coalesce(max(serial),0) FROM native_browser_profiles').fetchone()[0]
            plan_serial = connection.execute('SELECT coalesce(max(serial),0) FROM account_window_plans WHERE owner_user_id=?', (owner,)).fetchone()[0]
            for offset in range(count):
                profile, plan = 'native:' + str(uuid.uuid4()), str(uuid.uuid4())
                connection.execute('INSERT INTO native_browser_profiles VALUES(?,?,?,?,?,?,?,?)', (profile, owner, serial + offset + 1, f'window {offset}', '', 'http://old.proxy.test:8080', now, now))
                connection.execute('INSERT INTO account_window_plans(id,owner_user_id,serial,name,profile_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (plan, owner, plan_serial + offset + 1, f'window {offset}', profile, now, now))
                rows.append((plan, profile))
        return rows

    def test_snapshot_database_work_does_not_add_connection_per_window(self):
        self._seed(250)
        original = self.db._connect
        counts = {'connections': 0, 'statements': 0}
        def connect():
            counts['connections'] += 1
            connection = original()
            connection.set_trace_callback(lambda sql: counts.__setitem__('statements', counts['statements'] + 1))
            return connection
        self.db._connect = connect
        try:
            snapshot = self.accounts.snapshot(self.owner)
        finally:
            self.db._connect = original
        self.assertEqual(250, len(snapshot['plans']))
        self.assertLessEqual(counts['connections'], 3, counts)
        self.assertLessEqual(counts['statements'], 12, counts)
        self.assertEqual(1, sum(method == 'inventory' for method, _ in self.bridge.calls))
        self.assertTrue(all(plan['proxy_server'] == 'http://old.proxy.test:8080' for plan in snapshot['plans']))

    def test_plan_and_proxy_come_from_same_database_snapshot(self):
        plan, profile = self._seed(1)[0]
        original = self.db._connect
        calls = 0
        def connect():
            nonlocal calls
            calls += 1
            if calls == 2:
                # Change both fields atomically after the first read finishes.
                # N+1 per-plan fetches used to combine old name with new proxy.
                writer = original()
                try:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('UPDATE account_window_plans SET name=?,revision=revision+1 WHERE id=?', ('new name', plan))
                    writer.execute('UPDATE native_browser_profiles SET proxy_server=? WHERE id=?', ('http://new.proxy.test:8080', profile))
                    writer.commit()
                finally:
                    writer.close()
            return original()
        self.db._connect = connect
        try:
            observed = self.accounts.snapshot(self.owner)['plans'][0]
        finally:
            self.db._connect = original
        self.assertEqual(('window 0', 'http://old.proxy.test:8080'), (observed['name'], observed['proxy_server']))
        fresh = self.accounts.snapshot(self.owner)['plans'][0]
        self.assertEqual(('new name', 'http://new.proxy.test:8080'), (fresh['name'], fresh['proxy_server']))

    def test_slow_inventory_does_not_hold_database_write_lock(self):
        plan, profile = self._seed(1)[0]
        entered, release, written = threading.Event(), self.threads.release_event(), threading.Event()
        results, errors = [], []
        def hook(method, body):
            if method == 'inventory':
                entered.set()
                release.wait()
        def snapshot():
            try:
                results.append(self.accounts.snapshot(self.owner))
            except BaseException as error:
                errors.append(error)
        def write():
            try:
                with self.db.write() as connection:
                    connection.execute('UPDATE native_browser_profiles SET proxy_server=? WHERE id=?', ('http://new.proxy.test:8080', profile))
                written.set()
            except BaseException as error:
                errors.append(error)
        self.bridge.hook = hook
        reader, _, _ = self.threads.start(snapshot)
        self.assertTrue(entered.wait(WAIT_SECONDS), f'inventory RPC did not start: {errors!r}')
        assert_database_writer_available(self, self.db)
        writer, _, _ = self.threads.start(write)
        try:
            self.assertTrue(written.wait(WAIT_SECONDS), f'unrelated durable write did not finish: {errors!r}')
            self.assertTrue(reader.is_alive(), 'inventory response must still be blocked')
        finally:
            release.set()
            reader.join(WAIT_SECONDS)
            writer.join(WAIT_SECONDS)
            self.bridge.hook = None
        self.assertEqual([], errors)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual('http://old.proxy.test:8080', results[0]['plans'][0]['proxy_server'])

    def test_next_snapshot_has_fresh_windows_and_leases_and_owner_scoped_plans(self):
        plan, profile = self._seed(1)[0]
        other_plan, other_profile = self._seed(1, owner=self.other)[0]
        self.bridge.live = {profile: {'generation': 1}, other_profile: {'generation': 2}}
        token = self.service.acquire_browser_lease(self.owner, profile, operation_type='studio', entity_id='active')
        first = self.accounts.snapshot(self.owner)
        self.assertEqual([plan], [row['id'] for row in first['plans']])
        self.assertEqual([{'id': profile, 'opened': True}], first['windows'])
        self.assertIn(profile, first['locks'])
        self.service.release_browser_lease(profile, token)
        self.bridge.live.pop(profile)
        second = self.accounts.snapshot(self.owner)
        self.assertEqual([{'id': profile, 'opened': False}], second['windows'])
        self.assertNotIn(profile, second['locks'])
        self.assertEqual([other_plan], [row['id'] for row in self.accounts.snapshot(self.other)['plans']])

    def test_inventory_plan_binding_scan_grows_with_rows_not_rows_squared(self):
        self._seed(1000)
        original = self.db._connect
        instructions = 0
        def progress():
            nonlocal instructions
            instructions += 100
            return 0
        def connect():
            connection = original()
            connection.set_progress_handler(progress, 100)
            return connection
        self.db._connect = connect
        try:
            rows = self.native.inventory()
        finally:
            self.db._connect = original
        self.assertEqual(1000, len(rows))
        self.assertLess(instructions, 90_000, f'inventory scanned task/account history per profile: {instructions} VM instructions')

    def test_inventory_index_preserves_archived_bindings_and_unbound_profiles(self):
        active_plan, active = self._seed(1)[0]
        archived_plan, archived = self._seed(1)[0]
        foreign_plan, foreign = self._seed(1, owner=self.other)[0]
        legacy = 'native:' + str(uuid.uuid4())
        now = isoformat()
        with self.db.write() as connection:
            connection.execute('UPDATE account_window_plans SET archived=1 WHERE id=?', (archived_plan,))
            for offset in range(3):
                connection.execute('INSERT INTO account_window_plans(id,owner_user_id,serial,name,profile_id,archived,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)', (str(uuid.uuid4()), self.owner, 3 + offset, 'old binding', active, now, now))
            connection.execute('INSERT INTO native_browser_profiles VALUES(?,?,?,?,?,?,?,?)', (legacy, self.owner, 4, 'legacy unbound', '', '', now, now))
        self.bridge.live = {profile: {'generation': number + 1} for number, profile in enumerate((active, archived, foreign, legacy))}
        before = self.native.inventory()
        self.db.initialize()
        after = self.native.inventory()
        self.assertEqual(before, after, 'ordinary index initialization changed window visibility')
        self.assertEqual({active, foreign, legacy}, {row['id'] for row in after})
        self.assertEqual(3, len(after), 'archived duplicate bindings must not multiply a window')
        snapshot = self.accounts.snapshot(self.owner)
        self.assertEqual([active_plan], [row['id'] for row in snapshot['plans']])
        self.assertEqual({active, legacy}, {row['id'] for row in snapshot['windows']})
        self.assertTrue(all(row['opened'] for row in snapshot['windows']))


if __name__ == '__main__':
    unittest.main()
