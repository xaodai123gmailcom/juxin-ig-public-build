"""Regressions for large histories, sparse legacy evidence and additive upgrades."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.database import Database
from app.service import CoreService

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from snapshot_scale_fixture import seed_snapshot_scale

INDICES = ('idx_results_progress_provenance', 'idx_workbench_claims_progress',
           'idx_workbench_candidates_progress', 'idx_workbench_candidates_legacy_progress',
           'idx_workbench_exclusions_progress', 'idx_workbench_exclusions_owner_page')


class SnapshotScaleR95Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'scale.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('scale-fixture', 'scale-fixture-password')['id']
        self.fixture = seed_snapshot_scale(self.db.path, self.owner, result_count=1000,
            identity_count=1400, split_count=12, profile_bytes=8192)

    def progress(self):
        with self.db.read() as c:
            return self.service._mode_progress_for_targets(c, [self.fixture['target_id']])

    def snapshot(self, limit=10):
        return self.service.get_workbench_snapshot(self.owner, limit=limit,
                                                   history_limit=limit, maintain=False)

    def test_equal_timestamp_history_materializes_only_the_requested_page(self):
        expected = self.snapshot()
        with self.db.read() as c:
            columns = [row[1] for row in c.execute('PRAGMA table_info(workbench_collection_exclusions)')]
            plans = [row[3] for row in c.execute('''EXPLAIN QUERY PLAN SELECT *
                FROM workbench_collection_exclusions WHERE owner_user_id=?
                ORDER BY excluded_at DESC,id DESC LIMIT 10''', (self.owner,))]
        self.assertFalse(any('TEMP B-TREE' in row for row in plans), plans)
        original = self.db._connect
        evaluated = []
        def connect():
            c = original()
            def probe(value):
                evaluated.append(len(value)); return value
            c.create_function('snapshot_history_payload', 1, probe)
            projection = ','.join('snapshot_history_payload(profile_snapshot_json) AS profile_snapshot_json'
                if name == 'profile_snapshot_json' else name for name in columns)
            c.execute('CREATE TEMP VIEW workbench_collection_exclusions AS SELECT ' +
                      projection + ' FROM main.workbench_collection_exclusions')
            return c
        with patch.object(self.db, '_connect', side_effect=connect):
            actual = self.snapshot()
        self.assertEqual(expected['collection_exclusion_history'], actual['collection_exclusion_history'])
        self.assertEqual(10, len(evaluated), 'history limit still sorts/evaluates every same-time profile')
        self.assertTrue(actual['has_more']['collection_exclusion_history'])
        self.assertEqual(self.fixture['total_collected'], actual['counts']['total_collected'])

    def test_progress_lookup_seeks_exact_target_totals(self):
        statements = []
        with self.db.read() as c:
            c.set_trace_callback(statements.append)
            self.service._mode_progress_for_targets(c, [self.fixture['target_id']])
            c.set_trace_callback(None)
            lookup = next(sql for sql in statements if 'FROM workbench_progress_totals' in sql)
            plans = [row[3] for row in c.execute('EXPLAIN QUERY PLAN ' + lookup)]
            self.assertTrue(any('SEARCH workbench_progress_totals USING PRIMARY KEY (target_id=?)' in p for p in plans), plans)
            self.assertFalse(any('SCAN' in p for p in plans), plans)
            # Plan wording alone is insufficient: reading exact totals must stay
            # small even when the target's retained review/result history grows.
            steps = []
            c.set_progress_handler(lambda: steps.append(1) or 0, 1)
            try:
                rows = c.execute(lookup).fetchall()
                self.assertEqual(self.fixture['qualified_for_review'], rows[0]['qualified'])
            finally:
                c.set_progress_handler(None, 0)
            self.assertLess(len(steps), 100, 'target projection lookup scanned retained history')
        # A real legacy row must retain the same result when source fields vanish.
        before = self.progress()
        with self.db.write() as c:
            c.execute("UPDATE workbench_candidates SET source_mode=NULL,source_target=NULL WHERE id='scale-c0000000'")
        self.assertEqual(before, self.progress())

    def test_legacy_join_chain_keeps_owner_target_and_mode_provenance(self):
        other_task = self.service.create_task(self.owner, name='Other source',
            modes=['followers', 'following'], targets=['other.source'], settings={})
        other_target = other_task['targets'][0]['id']
        other_owner = self.service.register_user('other-scale-owner', 'long-password')['id']
        foreign_task = self.service.create_task(other_owner, name='Other owner',
            modes=['followers', 'following'], targets=['foreign.source'], settings={})
        foreign_target = foreign_task['targets'][0]['id']
        target = self.fixture['target_id']
        # Four valid legacy admissions span owners, targets and source modes.
        # The remaining rows each lack one piece of matching provenance.
        with self.db.write() as c:
            for index in range(0, 26, 2):
                c.execute('''UPDATE workbench_candidates
                    SET source_target=NULL,source_mode=NULL WHERE id=?''',
                    (f'scale-c{index:07}',))
            c.execute("UPDATE workbench_identity_claims SET source='following' WHERE account_id='scale-a0000002'")
            c.execute("UPDATE task_results SET sources_json='[\"followers\",\"following\"]' WHERE account_id='scale-a0000002'")
            c.execute("UPDATE workbench_candidates SET source_mode='following' WHERE id='scale-c0000002'")
            c.execute('UPDATE workbench_identity_claims SET source_target=? WHERE account_id=?',
                      (other_target, 'scale-a0000004'))
            c.execute('UPDATE task_results SET task_id=?,target_id=? WHERE account_id=?',
                      (other_task['id'], other_target, 'scale-a0000004'))
            c.execute('UPDATE workbench_candidates SET owner_user_id=? WHERE id=?',
                      (other_owner, 'scale-c0000006'))
            c.execute('''UPDATE workbench_identity_claims SET claimed_by_user_id=?,
                source_target=?,source='following' WHERE account_id=?''',
                (other_owner, foreign_target, 'scale-a0000006'))
            c.execute('''UPDATE task_results SET task_id=?,target_id=?,sources_json='["following"]'
                WHERE account_id=?''', (foreign_task['id'], foreign_target, 'scale-a0000006'))
            c.execute('UPDATE workbench_candidates SET owner_user_id=? WHERE id=?',
                      (other_owner, 'scale-c0000008'))
            c.execute('UPDATE task_results SET task_id=? WHERE account_id=?',
                      (foreign_task['id'], 'scale-a0000010'))
            c.execute('UPDATE workbench_identity_claims SET source_target=? WHERE account_id=?',
                      (other_target, 'scale-a0000012'))
            c.execute("UPDATE workbench_identity_claims SET source='following' WHERE account_id IN ('scale-a0000014','scale-a0000016')")
            c.execute("UPDATE task_results SET sources_json='[\"following\"]' WHERE account_id='scale-a0000016'")
            c.execute("UPDATE workbench_candidates SET source_mode='followers' WHERE id='scale-c0000016'")
            c.execute('UPDATE workbench_candidates SET source_target=? WHERE id=?',
                      (other_target, 'scale-c0000018'))
            c.execute("UPDATE workbench_identity_claims SET source='collection' WHERE account_id='scale-a0000020'")
            c.execute("UPDATE task_results SET sources_json='{broken' WHERE account_id='scale-a0000022'")
            c.execute("UPDATE task_results SET sources_json='{\"mode\":\"followers\"}' WHERE account_id='scale-a0000024'")
        with self.db.read() as c:
            trace = []
            c.set_trace_callback(trace.append)
            self.service._mode_progress_for_targets(c, [target, other_target, foreign_target])
            c.set_trace_callback(None)
            # Isolate these thirteen legacy rows from the other unchanged
            # modern admissions. Their compact contributions must retain the
            # exact same four owner/target/mode matches as the original joins.
            legacy_ids = [f'scale-a{index:07}' for index in range(0, 26, 2)]
            marks = ','.join('?' for _ in legacy_ids)
            actual = {(row['target_id'], row['mode']): row['qualified']
                      for row in c.execute(f"""SELECT target_id,mode,SUM(qualified) AS qualified
                          FROM workbench_progress_contributions
                          WHERE account_id IN ({marks}) AND kind='qualified'
                          GROUP BY target_id,mode""", legacy_ids)}
            self.assertEqual({(target, 'followers'): 1, (target, 'following'): 1,
                              (other_target, 'followers'): 1,
                              (foreign_target, 'following'): 1}, actual)
            lookup = next(sql for sql in trace if 'FROM workbench_progress_totals' in sql)
            plans = [row[3] for row in c.execute('EXPLAIN QUERY PLAN ' + lookup)]
            self.assertTrue(any('SEARCH workbench_progress_totals USING PRIMARY KEY' in p for p in plans), plans)

    def test_additive_index_upgrade_preserves_every_retained_row_and_exact_counter(self):
        before = self.snapshot()
        before_progress = self.progress()
        tables = ('task_results', 'workbench_candidates', 'workbench_collection_exclusions',
                  'workbench_identity_claims', 'global_seen', 'split_candidate_history')
        with self.db.write() as c:
            retained = {table: [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                        for table in tables}
            for name in INDICES:
                c.execute(f'DROP INDEX {name}')
        self.db.initialize()
        self.db.initialize()  # No repeated rebuild, projection drift or data rewriting.
        with self.db.read() as c:
            for table, rows in retained.items():
                self.assertEqual(rows, [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')])
            found = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue(set(INDICES) <= found)
            self.assertEqual([], c.execute('PRAGMA foreign_key_check').fetchall())
        after = self.snapshot()
        self.assertEqual(before['counts'], after['counts'])
        self.assertEqual(before['dedupe'], after['dedupe'])
        self.assertEqual(before_progress, self.progress())
        mode = before_progress[self.fixture['target_id']]['followers']
        self.assertEqual(self.fixture['qualified_for_review'], mode['qualified_for_review'])
        self.assertEqual(self.fixture['discarded'], mode['discarded'])
        self.assertEqual(self.fixture['discarded'], mode['hover_discarded'])
        for key in ('total_collected', 'total_public', 'total_private', 'total_split'):
            self.assertEqual(self.fixture[key], after['counts'][key])
        self.assertEqual(self.fixture['global_dedupe_count'], after['global_dedupe_count'])

    def test_fixture_refuses_reusing_a_nonempty_ledger(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Refusing nonempty'):
            seed_snapshot_scale(self.db.path, self.owner, result_count=1, identity_count=1)
        self.assertEqual(before['counts'], self.snapshot()['counts'])


if __name__ == '__main__':
    unittest.main()
