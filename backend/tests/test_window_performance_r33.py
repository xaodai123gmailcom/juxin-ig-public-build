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

    def _snapshot_work(self):
        original = self.db._connect
        counts = {'connections': 0, 'statements': 0, 'instructions': 0}
        calls_before = len(self.bridge.calls)
        def trace(sql):
            counts['statements'] += 1
        def progress():
            counts['instructions'] += 100
            return 0
        def connect():
            counts['connections'] += 1
            connection = original()
            connection.set_trace_callback(trace)
            connection.set_progress_handler(progress, 100)
            return connection
        self.db._connect = connect
        try:
            snapshot = self.accounts.snapshot(self.owner)
        finally:
            self.db._connect = original
        self.assertEqual(1, sum(method == 'inventory' for method, _ in self.bridge.calls[calls_before:]))
        return snapshot, counts

    def _legacy_studio(self, connection, ident, profile, *, owner=None, **fields):
        values = dict(id=ident, owner_user_id=owner or self.owner, request_key=ident,
                      kind='posting', profile_id=profile, status='queued', config_json='{}',
                      result_json='{}', due_at='2026-01-01', created_at='2026-01-01',
                      updated_at='2026-01-01')
        values.update(fields)
        connection.execute('INSERT INTO studio_jobs(' + ','.join(values) + ') VALUES(' +
                           ','.join('?' for _ in values) + ')', tuple(values.values()))

    def _legacy_posting(self, connection, ident, profile, *, owner=None, **fields):
        values = dict(id=ident, owner_user_id=owner or self.owner, request_key=ident,
                      theme='fixture', caption='retained', profile_id=profile,
                      status='queued', created_at='2026-01-01', updated_at='2026-01-01')
        values.update(fields)
        connection.execute('INSERT INTO posting_jobs(' + ','.join(values) + ') VALUES(' +
                           ','.join('?' for _ in values) + ')', tuple(values.values()))

    def _legacy_lease(self, connection, ident, profile, *, owner=None, operation='studio'):
        connection.execute('''INSERT INTO browser_operation_leases(profile_id,owner_user_id,
            operation_type,entity_id,lease_token,acquired_at,heartbeat_at,expires_at)
            VALUES(?,?,?,?,?,'2000-01-01','2000-01-01','2000-01-01')''',
            (profile, owner or self.owner, operation, ident, 'token-' + ident))

    def test_fresh_snapshot_statement_budget_is_constant_at_zero_one_and_250_windows(self):
        measurements = []
        for count, added in ((0, 0), (1, 1), (250, 249)):
            with self.subTest(windows=count):
                self._seed(added)
                snapshot, work = self._snapshot_work()
                self.assertEqual(count, len(snapshot['plans']))
                self.assertLessEqual(work['connections'], 3, work)
                self.assertLessEqual(work['statements'], 12, work)
                measurements.append((work['connections'], work['statements']))
        self.assertEqual(1, len(set(measurements)), measurements)

    def test_retained_legacy_jobs_add_no_per_profile_or_per_job_sql(self):
        from app.posting_schema import initialize_posting_schema
        with self.db.write() as connection:
            initialize_posting_schema(connection)
        measurements = []
        for count, added in ((0, 0), (1, 1), (250, 249)):
            with self.subTest(windows=count):
                rows = self._seed(added)
                with self.db.write() as connection:
                    for _, profile in rows:
                        self._legacy_studio(connection, 's-' + profile, profile,
                                            kind='material', result_json='{"window_hold":true}')
                        self._legacy_posting(connection, 'p-' + profile, profile, status='needs_review')
                snapshot, work = self._snapshot_work()
                self.assertEqual(count, len(snapshot['plans']))
                self.assertEqual(count, len(snapshot['locks']))
                self.assertTrue(all(row['operation_type'] == 'account' and row['entity_id'] is None
                                    for row in snapshot['locks'].values()))
                self.assertLessEqual(work['connections'], 3, work)
                # The optional retained ledger adds one batch read. It must not
                # change the original fresh-database 12-statement contract.
                self.assertLessEqual(work['statements'], 13, work)
                self.assertLess(work['instructions'], 100_000, work)
                measurements.append((work['connections'], work['statements']))
        self.assertEqual(1, len(set(measurements)), measurements)
        # Many history rows on one profile must not create a per-job SQL loop.
        profile = self._seed(1)[0][1]
        with self.db.write() as connection:
            for number in range(250):
                self._legacy_studio(connection, 'idle-' + str(number), profile, status='completed')
                self._legacy_posting(connection, 'idle-p-' + str(number), profile, status='completed')
        snapshot, work = self._snapshot_work()
        self.assertNotIn(profile, snapshot['locks'])
        self.assertEqual(measurements[-1], (work['connections'], work['statements']))

    def test_batch_holds_match_profile_fences_without_changing_ownership(self):
        from app.posting_retirement import legacy_profile_hold, legacy_window_state
        from app.posting_schema import initialize_posting_schema
        profiles = []
        with self.db.write() as connection:
            initialize_posting_schema(connection)
            for number, fields in enumerate((
                    {}, {'status': 'running'}, {'status': 'needs_review'}, {'inflight': 1},
                    {'cursor': 1}, {'result_json': '{broken'}, {'result_json': '[]'},
                    {'result_json': 'null'}, {'result_json': '{"failure":null}'},
                    {'result_json': '{"failure":{"uncertain":true}}'},
                    {'result_json': '{"window_cleanup":{"state":"unknown"}}'},
                    {'result_json': '{"published":true}'},
                    {'status': 'completed', 'result_json': '{"window_hold":true}'},
                    {'status': 'completed', 'result_json': '{"published":true}'})):
                profile = 'studio-' + str(number)
                profiles.append(profile)
                self._legacy_studio(connection, profile, profile, **fields)
            for number, fields in enumerate((
                    {}, {'status': 'running'}, {'status': 'needs_review'},
                    {'lease_token': 'missing-live-row'}, {'attempt_id': 'uncertain'},
                    {'submitted_at': '2026-01-01'}, {'failure_stage': 'unknown'},
                    {'status': 'completed', 'attempt_id': 'known-success'})):
                profile = 'posting-' + str(number)
                profiles.append(profile)
                self._legacy_posting(connection, profile, profile, **fields)
            for ident, profile, owner in (('leased-studio', 'original-studio', self.other),
                                           ('leased-posting', 'original-posting', self.other)):
                profiles.append(profile)
                if ident == 'leased-studio':
                    self._legacy_studio(connection, ident, profile, owner=owner)
                else:
                    self._legacy_posting(connection, ident, profile, owner=owner)
                self._legacy_lease(connection, ident, 'mismatched-' + profile,
                                   operation='studio' if ident == 'leased-studio' else 'posting')
            self._legacy_studio(connection, 'precedence-studio', 'shared', owner=self.other, status='running')
            self._legacy_posting(connection, 'precedence-posting', 'shared', status='running')
            self._legacy_posting(connection, 'first-posting-running', 'post-shared', status='running')
            self._legacy_posting(connection, 'second-posting-review', 'post-shared', owner=self.other,
                                 status='needs_review')
            profiles.extend(('shared', 'post-shared'))
            connection.execute('''INSERT INTO posting_receipts(job_id,owner_user_id,profile_id,
                username,confirmed_at,day_utc,evidence_json) VALUES(?,?,?,'fixture',
                '2026-01-01','2026-01-01','{}')''', ('posting-0', self.owner, 'posting-0'))
            self._legacy_studio(connection, 'nurture-cleanup', 'nurture-cleanup', kind='nurture',
                                status='completed', result_json='{"window_hold":true}')
            self._legacy_lease(connection, 'successor', 'shared', operation='account')
        with self.db.read() as connection:
            connection.execute('BEGIN')
            leases = list(connection.execute('SELECT * FROM browser_operation_leases'))
            expected = [row for profile in sorted(profiles)
                        if (row := legacy_profile_hold(connection, profile)) is not None]
            before = connection.total_changes
            studio_ids, holds, cleanup = legacy_window_state(connection, leases=leases, include_nurture=True)
            self.assertEqual({row['profile_id'] for row in expected}, {row['profile_id'] for row in holds})
            expected_by_profile = {row['profile_id']: row for row in expected}
            for hold in holds:
                durable = {key: value for key, value in hold.items() if key != '_legacy_owner_ambiguous'}
                if hold['profile_id'] in {'shared', 'post-shared'}:
                    self.assertTrue(hold['_legacy_owner_ambiguous'])
                else:
                    self.assertFalse(hold['_legacy_owner_ambiguous'])
                    self.assertEqual(expected_by_profile[hold['profile_id']], durable)
            self.assertEqual(before, connection.total_changes, 'a snapshot must not mutate ownership')
            self.assertEqual(['nurture-cleanup'], [row['id'] for row in cleanup])
            self.assertIn('leased-studio', studio_ids)
            self.assertNotIn('nurture-cleanup', studio_ids)
            self.assertEqual(leases, list(connection.execute('SELECT * FROM browser_operation_leases')))
        locks = self.accounts.snapshot(self.owner)['locks']
        self.assertEqual('account', locks['shared']['operation_type'])
        self.assertNotIn('can_reconcile_window_state', locks['shared'], 'actual successor stays authoritative')
        self.assertNotIn('can_reconcile_window_state', locks['original-studio'])
        self.assertNotIn('can_reconcile_window_state', locks['original-posting'])
        self.assertTrue(locks['posting-0']['can_reconcile_window_state'])
        self.assertTrue(locks['nurture-cleanup']['cleanup_required'])
        self.assertEqual('cleanup_pending', locks['nurture-cleanup']['state'])
        # Fresh reads see resolved durable evidence without a cache or deletion.
        with self.db.write() as connection:
            connection.execute("UPDATE studio_jobs SET result_json='{}' WHERE id='nurture-cleanup'")
        self.assertNotIn('nurture-cleanup', self.accounts.snapshot(self.owner)['locks'])

    def test_batch_legacy_reader_tolerates_missing_optional_tables_and_columns(self):
        import sqlite3
        from app.posting_retirement import legacy_profile_hold, legacy_window_state
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        def durable_holds():
            return [{key: value for key, value in row.items() if key != '_legacy_owner_ambiguous'}
                    for row in legacy_window_state(connection)[1]]
        self.assertEqual((set(), [], []), legacy_window_state(connection, include_nurture=True))
        # A pre-migration posting ledger may lack failure_stage and later fields.
        connection.execute('CREATE TABLE posting_jobs(id TEXT, owner_user_id TEXT, profile_id TEXT, status TEXT)')
        connection.execute("INSERT INTO posting_jobs VALUES('old','owner','profile','running')")
        self.assertEqual('old', legacy_window_state(connection)[1][0]['id'])
        connection.execute("INSERT INTO posting_jobs VALUES('second','other-owner','profile','needs_review')")
        self.assertEqual([legacy_profile_hold(connection, 'profile')], durable_holds())
        connection.execute("DELETE FROM posting_jobs WHERE id='second'")
        connection.execute("UPDATE posting_jobs SET status='completed'")
        self.assertEqual([], legacy_window_state(connection)[1])
        connection.execute('CREATE TABLE browser_operation_leases(operation_type TEXT, entity_id TEXT)')
        connection.execute("INSERT INTO browser_operation_leases VALUES('posting','old')")
        self.assertEqual('old', legacy_window_state(connection)[1][0]['id'])
        connection.execute('DELETE FROM browser_operation_leases')
        connection.execute("UPDATE posting_jobs SET status='queued'")
        connection.execute('ALTER TABLE posting_jobs ADD COLUMN _legacy_has_receipt INTEGER DEFAULT 0')
        connection.execute('CREATE TABLE posting_receipts(job_id TEXT PRIMARY KEY)')
        connection.execute("INSERT INTO posting_receipts VALUES('old')")
        expected = legacy_profile_hold(connection, 'profile')
        self.assertIsNotNone(expected)
        self.assertEqual([expected], durable_holds())
        self.assertEqual(0, legacy_window_state(connection)[1][0]['_legacy_has_receipt'])
        connection.execute('ALTER TABLE posting_jobs ADD COLUMN _legacy_owner_ambiguous INTEGER DEFAULT 0')
        connection.execute("INSERT INTO posting_jobs(id,owner_user_id,profile_id,status) VALUES('other','other-owner','profile','running')")
        self.assertTrue(legacy_window_state(connection)[1][0]['_legacy_owner_ambiguous'])
        self.assertTrue(all(row[0] == 0 for row in connection.execute('SELECT _legacy_owner_ambiguous FROM posting_jobs')))

    def test_ambiguous_retained_owners_never_gain_reconciliation_from_query_order(self):
        from app.posting_schema import initialize_posting_schema
        with self.db.write() as connection:
            initialize_posting_schema(connection)
            self._legacy_posting(connection, 'first-running', 'posting-shared', status='running')
            self._legacy_posting(connection, 'second-review', 'posting-shared', owner=self.other,
                                 status='needs_review')
            self._legacy_studio(connection, 'studio-first', 'mixed-shared', status='running')
            self._legacy_posting(connection, 'posting-second', 'mixed-shared', owner=self.other,
                                 status='needs_review')
            self._legacy_studio(connection, 'studio-own', 'studio-shared', status='running')
            self._legacy_studio(connection, 'studio-other', 'studio-shared', owner=self.other,
                                status='needs_review')
            self._legacy_posting(connection, 'same-owner-first', 'unambiguous', status='running')
            self._legacy_posting(connection, 'same-owner-second', 'unambiguous', status='needs_review')
            before = {table: [dict(row) for row in connection.execute('SELECT * FROM ' + table)]
                      for table in ('studio_jobs', 'posting_jobs', 'browser_operation_leases')}
        for indexed in (True, False):
            with self.db.write() as connection:
                if not indexed:
                    connection.execute('DROP INDEX posting_jobs_window')
                else:
                    connection.execute('CREATE INDEX IF NOT EXISTS posting_jobs_window ON posting_jobs(profile_id,status)')
            for analyzed in (False, True):
                with self.subTest(indexed=indexed, analyzed=analyzed):
                    if analyzed:
                        with self.db.write() as connection:
                            connection.execute('ANALYZE')
                    for owner in (self.owner, self.other):
                        snapshot = self.accounts.snapshot(owner)
                        states = {row['profile_id']: row for row in self.service.list_browser_lease_states(owner)}
                        for profile in ('posting-shared', 'mixed-shared', 'studio-shared'):
                            for row in (snapshot['locks'][profile], states[profile]):
                                self.assertEqual('occupied', row['state'])
                                self.assertEqual('account', row['operation_type'])
                                self.assertIsNone(row['entity_id'])
                                self.assertNotIn('can_reconcile_window_state', row)
                                self.assertNotIn('_legacy_owner_ambiguous', row)
                            self.assertFalse(states[profile]['owned_by_current_login'])
                        for row in (snapshot['locks']['unambiguous'], states['unambiguous']):
                            self.assertEqual(owner == self.owner, row.get('can_reconcile_window_state', False))
        with self.db.read() as connection:
            for table, rows in before.items():
                self.assertEqual(rows, [dict(row) for row in connection.execute('SELECT * FROM ' + table)])

    def test_malformed_completed_nurture_result_still_fails_closed(self):
        import sqlite3
        with self.db.write() as connection:
            self._legacy_studio(connection, 'malformed-nurture', 'profile', kind='nurture',
                                status='completed', result_json='{broken')
        with self.assertRaises(sqlite3.OperationalError):
            self.accounts.snapshot(self.owner)

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
