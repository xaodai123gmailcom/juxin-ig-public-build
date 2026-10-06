"""Repeat-startup repair stays exact without rereading retained JSON payloads."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database, SCHEMA


class TracingDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.statements = []

    def _connect(self):
        connection = super()._connect()
        connection.set_trace_callback(self.statements.append)
        return connection


class StartupScalingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = TracingDatabase(Path(self.temporary.name) / 'startup.sqlite3')
        self.database.initialize()
        with self.database.write() as connection:
            for owner in ('owner-a', 'owner-b'):
                connection.execute(
                    'INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) '
                    'VALUES(?,?,?,?,?)', (owner, owner, owner, 'synthetic-unused', '2026-01-01'))

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, count=3, payload_bytes=0):
        payload = json.dumps({'retained_profile': 'x' * payload_bytes})
        with self.database.write() as connection:
            for number in range(count):
                owner = 'owner-a' if number % 2 == 0 else 'owner-b'
                for kind in ('candidate', 'exclusion'):
                    key = f'{kind}-{number}'
                    connection.execute(
                        'INSERT INTO instagram_accounts(id,current_username_norm,current_username_display,first_seen_at,last_seen_at) '
                        'VALUES(?,?,?,?,?)', (key, key, key, '2026-01-01', '2026-01-01'))
                    connection.execute('INSERT INTO global_seen VALUES(?,?,?,?)',
                                       (key, '["followers"]', '2026-01-01', '2026-01-01'))
                    if kind == 'candidate':
                        cache = json.dumps({'unicode': '北京', 'preview': 'c' * (128 + number)}, ensure_ascii=False)
                        connection.execute(
                            'INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,status,profile_json,review_cache_json,created_at,updated_at) '
                            'VALUES(?,?,?,?,?,?,?,?,?)',
                            (key, owner, key, 'private', 'pending', payload, cache, '2026-01-01', '2026-01-01'))
                    else:
                        connection.execute(
                            'INSERT INTO workbench_collection_exclusions(id,account_id,owner_user_id,username_display,reason_code,reason,profile_snapshot_json,excluded_at) '
                            'VALUES(?,?,?,?,?,?,?,?)',
                            (key, key, owner, key, 'exact_verified_exclusion', 'retained reason', payload, '2026-01-01'))

    def expected_usage(self):
        with self.database.read() as connection:
            return [tuple(row) for row in connection.execute('''
                SELECT owner_user_id,
                  SUM(CASE WHEN status='pending' AND review_cache_json<>'{}' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status='pending' THEN MAX(length(CAST(review_cache_json AS BLOB))-2,0) ELSE 0 END),
                  SUM(CASE WHEN status<>'pending' AND review_cache_json<>'{}' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status<>'pending' THEN MAX(length(CAST(review_cache_json AS BLOB))-2,0) ELSE 0 END)
                FROM workbench_candidates NOT INDEXED GROUP BY owner_user_id ORDER BY owner_user_id
            ''')]

    def assert_usage(self):
        expected = self.expected_usage()
        with self.database.read() as connection:
            actual = [tuple(row) for row in connection.execute('SELECT * FROM workbench_cache_usage ORDER BY owner_user_id')]
        self.assertEqual(expected, actual)

    def test_upgrade_creates_narrow_indexes_preserves_ledgers_and_repairs_usage(self):
        self.seed()
        with self.database.write() as connection:
            preserved = {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                         for name in ('instagram_accounts', 'global_seen', 'workbench_candidates', 'workbench_collection_exclusions')}
            for name in ('idx_workbench_cache_rebuild', 'idx_exclusions_legacy_hover'):
                connection.execute('DROP INDEX ' + name)
            connection.execute('DELETE FROM schema_migrations WHERE version=16')
            connection.execute('DROP TABLE workbench_cache_usage')
        self.database.initialize()
        self.assert_usage()
        with self.database.read() as connection:
            self.assertEqual('ok', connection.execute('PRAGMA quick_check').fetchone()[0])
            for name, rows in preserved.items():
                self.assertEqual(rows, [tuple(row) for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY 1')])
            self.assertIsNotNone(connection.execute('SELECT 1 FROM schema_migrations WHERE version=16').fetchone())
            columns = [row[2] for row in connection.execute('PRAGMA index_info(idx_workbench_cache_rebuild)')]
            self.assertEqual(['owner_user_id', 'status', None, None], columns)
            self.assertIn("WHERE reason_code='hover_preview_unavailable'", connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_exclusions_legacy_hover'").fetchone()[0])

    def test_each_restart_repairs_wrong_and_missing_projection_and_terminal_cache(self):
        self.seed()
        for fault in ('wrong', 'missing'):
            with self.subTest(fault=fault):
                with self.database.write() as connection:
                    # Model a legacy decision that committed before cache cleanup.
                    connection.execute("UPDATE workbench_candidates SET status='approved',review_cache_json=? WHERE id='candidate-0'",
                                       (json.dumps({'preview': 'terminal'}),))
                    if fault == 'wrong':
                        connection.execute('UPDATE workbench_cache_usage SET pending_entries=999,pending_bytes=-1,terminal_entries=0,terminal_bytes=0')
                    else:
                        connection.execute('DELETE FROM workbench_cache_usage')
                self.database.initialize()
                self.assert_usage()
                with self.database.read() as connection:
                    self.assertEqual('{}', connection.execute("SELECT review_cache_json FROM workbench_candidates WHERE id='candidate-0'").fetchone()[0])
                    self.assertEqual(6, connection.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])

    def test_rebuild_bytecode_reads_index_not_profile_table(self):
        self.seed()
        self.database.statements.clear()
        self.database.initialize()
        statement = next(sql for sql in self.database.statements
                         if sql.lstrip().startswith('INSERT INTO workbench_cache_usage('))
        select = statement[statement.index('SELECT owner_user_id'):]
        with self.database.read() as connection:
            plan = ' '.join(str(row[3]) for row in connection.execute('EXPLAIN QUERY PLAN ' + select))
            self.assertIn('idx_workbench_cache_rebuild', plan)
            table_root = connection.execute("SELECT rootpage FROM sqlite_master WHERE name='workbench_candidates'").fetchone()[0]
            bytecode = [tuple(row) for row in connection.execute('EXPLAIN ' + select)]
            table_cursors = {row[2] for row in bytecode if row[1] == 'OpenRead' and row[3] == table_root}
            self.assertFalse([row for row in bytecode if row[1] == 'Column' and row[2] in table_cursors], bytecode)
            hover_plan = ' '.join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM workbench_collection_exclusions WHERE reason_code='hover_preview_unavailable' LIMIT 1"))
            self.assertIn('idx_exclusions_legacy_hover', hover_plan)
        self.assertFalse(any('SELECT COUNT(*)' in sql and "status!='pending'" in sql for sql in self.database.statements))

    def test_global_count_seed_is_not_evaluated_for_existing_singleton(self):
        self.seed()
        seed = SCHEMA[SCHEMA.index('INSERT OR IGNORE INTO global_seen_stats('):].split(';', 1)[0]
        # A non-deterministic probe marks actual execution, including SQLite's
        # aggregate COUNT fast path which a SQL VM step budget cannot detect.
        calls = []
        with self.database.write() as connection:
            connection.create_function('probe_count', 0, lambda: calls.append(True) or 6)
            probe = seed.replace('COUNT(*)', 'probe_count()')
            connection.execute(probe)
            self.assertEqual([], calls)
            connection.execute('DELETE FROM global_seen_stats')
            connection.execute(probe)
            self.assertEqual([True], calls)
            self.assertEqual(6, connection.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])
            connection.execute('DELETE FROM global_seen_stats')
        self.database.initialize()
        with self.database.read() as connection:
            self.assertEqual(6, connection.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])

    def test_pre_v13_migration_still_reconciles_wrong_global_counter(self):
        self.seed()
        with self.database.write() as connection:
            connection.execute('UPDATE global_seen_stats SET total_count=999')
            connection.execute('DELETE FROM schema_migrations WHERE version=13')
        self.database.initialize()
        with self.database.read() as connection:
            self.assertEqual(6, connection.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])
            self.assertIsNotNone(connection.execute('SELECT 1 FROM schema_migrations WHERE version=13').fetchone())

    def test_old_core_writes_without_projection_triggers_are_repaired(self):
        self.seed()
        with self.database.write() as connection:
            for suffix in ('insert', 'update', 'delete'):
                connection.execute('DROP TRIGGER trg_workbench_cache_usage_' + suffix)
            connection.execute(
                "UPDATE workbench_candidates SET owner_user_id='owner-b',review_cache_json=? WHERE id='candidate-0'",
                (json.dumps({'preview': 'old core 北京'}, ensure_ascii=False),))
            connection.execute("UPDATE workbench_candidates SET status='rejected' WHERE id='candidate-1'")
            connection.execute('UPDATE workbench_cache_usage SET pending_entries=999,pending_bytes=999')
        self.database.initialize()
        self.assert_usage()
        with self.database.read() as connection:
            self.assertEqual('{}', connection.execute("SELECT review_cache_json FROM workbench_candidates WHERE id='candidate-1'").fetchone()[0])
            self.assertIn('北京', connection.execute("SELECT review_cache_json FROM workbench_candidates WHERE id='candidate-0'").fetchone()[0])
            self.assertEqual(3, connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_workbench_cache_usage_%'").fetchone()[0])

    @unittest.skipUnless(Path('/proc/self/io').is_file(), 'Linux logical read accounting')
    def test_restart_read_volume_does_not_scale_with_retained_profile_payloads(self):
        self.seed(count=384, payload_bytes=32768)
        self.database.initialize()
        self.database.statements.clear()
        def read_chars():
            return int(next(line.split(':', 1)[1] for line in Path('/proc/self/io').read_text().splitlines() if line.startswith('rchar:')))
        before = read_chars()
        self.database.initialize()
        consumed = read_chars() - before
        # More than 24 MiB of permanent payloads plus cached previews are
        # retained. A restart should read metadata and narrow index pages only.
        self.assertLess(consumed, 2 * 1024 * 1024, f'Restart read {consumed} logical bytes')
        self.assert_usage()


if __name__ == '__main__':
    unittest.main()
