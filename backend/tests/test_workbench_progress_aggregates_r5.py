"""Incremental progress evidence agrees with the original queries after mutations."""
from __future__ import annotations

import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from app.database import Database
from app.service import CoreService
from app.workbench_progress_aggregates import (
    initialize_workbench_progress_aggregates, rebuild_workbench_progress_aggregates,
)
from support.progress_aggregate_reference import legacy_mode_progress_for_targets

NOW = '2026-10-01T00:00:00Z'


class ProgressAggregateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'progress.db')
        self.db.initialize()
        with self.db.write() as c:
            initialize_workbench_progress_aggregates(c)
            for owner in ('owner1', 'owner2'):
                c.execute('INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) VALUES(?,?,?,?,?)',
                          (owner, owner, owner, 'synthetic', NOW))
            for i in range(2):
                c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES(?,?,'test','running','[\"followers\"]','{}',?,?)",
                          (f'task{i}', 'owner1', NOW, NOW))
                c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES(?,?,?,?,0,'running',?,?)",
                          (f'target{i}', f'task{i}', f'source{i}', f'source{i}', NOW, NOW))
                for mode in ('followers', 'following', 'post_likers'):
                    c.execute("INSERT INTO task_checkpoints(id,task_id,target_id,mode,stage,cursor_json,counters_json,updated_at) VALUES(?,?,?,?,'saved','{}',?,?)",
                              (f'cp{i}{mode}', f'task{i}', f'target{i}', mode,
                               json.dumps({'saved': 2, 'processed': 2, 'skipped_global_duplicates': 0}), NOW))
            # Exclusion history remains immutable in normal use. These deliberate
            # repair operations exercise trigger correctness after old migrations.
            c.execute('DROP TRIGGER trg_workbench_exclusion_no_update')
            c.execute('DROP TRIGGER trg_workbench_exclusion_no_delete')

    def tearDown(self):
        self.temp.cleanup()

    def account(self, c, i):
        a = f'account{i}'
        c.execute('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)', (a, a, a, NOW, NOW))
        return a

    def pieces(self, c, i, *, order=('result', 'claim', 'review', 'exclusion')):
        a = self.account(c, i)
        for piece in order:
            if piece == 'result':
                c.execute("INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) VALUES(?,'task0','target0',?,'[\"followers\",\"following\",\"following\"]','public','{}','{}',?,?)",
                          (f'result{i}', a, NOW, NOW))
            elif piece == 'claim':
                c.execute("INSERT INTO workbench_identity_claims VALUES(?,'owner1','followers','target0',?)", (a, NOW))
            elif piece == 'review':
                c.execute("INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,status,source_mode,source_target,created_at,updated_at) VALUES(?,'owner1',?,'public','pending','followers','target0',?,?)",
                          (f'review{i}', a, NOW, NOW))
            elif piece == 'exclusion':
                c.execute("INSERT INTO workbench_collection_exclusions(id,account_id,owner_user_id,username_display,reason_code,reason,profile_snapshot_json,excluded_at) VALUES(?,?,'owner1',?,'synthetic','test','{\"page_read_status\":\"hover_preview\"}',?)",
                          (f'exclusion{i}', a, a, NOW))
            self.equivalent(c)
        return a

    def equivalent(self, c):
        targets = ['target0', 'target1', 'missing']
        self.assertEqual(legacy_mode_progress_for_targets(c, targets),
                         CoreService._mode_progress_for_targets(c, targets))
        rows = c.execute('SELECT * FROM workbench_progress_totals').fetchall()
        for row in rows:
            self.assertTrue(all(row[k] >= 0 for k in
                ('qualified', 'recorded_total', 'excluded_total', 'discarded', 'hover_discarded')), dict(row))

    def test_all_insert_orders_and_target_local_read_work(self):
        import itertools
        with self.db.write() as c:
            for i, order in enumerate(itertools.permutations(('result', 'claim', 'review', 'exclusion'))):
                self.pieces(c, i, order=order)
            steps = [0]
            c.set_progress_handler(lambda: steps.__setitem__(0, steps[0]+1) or 0, 1)
            CoreService._mode_progress_for_targets(c, ['target0'])
            c.set_progress_handler(None, 0)
            self.assertLess(steps[0], 700)

    def test_mutations_legacy_provenance_owners_and_invalid_json(self):
        with self.db.write() as c:
            for i in range(8):
                self.pieces(c, i)
            rng = random.Random(79)
            for n in range(160):
                a = f'account{rng.randrange(8)}'
                kind = n % 6
                if kind == 0:
                    c.execute('UPDATE workbench_candidates SET source_target=?,source_mode=?,owner_user_id=?,status=? WHERE account_id=?',
                              (rng.choice([None, 'target0', 'target1']),
                               rng.choice([None, 'followers', 'following', 'post_likers']),
                               rng.choice(['owner1', 'owner2']), rng.choice(['approved','pending','rejected']), a))
                elif kind == 1:
                    c.execute('UPDATE workbench_identity_claims SET source_target=?,source=?,claimed_by_user_id=? WHERE account_id=?',
                              (rng.choice([None, 'target0', 'target1']), rng.choice(['collection','followers','following']), rng.choice(['owner1','owner2']), a))
                elif kind == 2:
                    c.execute('UPDATE task_results SET sources_json=? WHERE account_id=?',
                              (rng.choice(['broken', 'null', '{}', '"followers"', '["followers"]', '["following","following"]', '[null,5,"followers"]']), a))
                elif kind == 3:
                    c.execute('UPDATE workbench_collection_exclusions SET owner_user_id=?,profile_snapshot_json=? WHERE account_id=?',
                              (rng.choice(['owner1','owner2']), rng.choice(['{}','broken','{"page_read_status":"hover_preview"}']), a))
                elif kind == 4:
                    c.execute('UPDATE tasks SET owner_user_id=? WHERE id=?',
                              (rng.choice(['owner1','owner2']), rng.choice(['task0','task1'])))
                else:
                    task = rng.randrange(2)
                    c.execute('UPDATE task_results SET task_id=?,target_id=? WHERE account_id=?',
                              (f'task{task}', f'target{task}', a))
                self.equivalent(c)

    def test_delete_cascades_rollback_rebuild_and_missing_trigger_repair(self):
        with self.db.write() as c:
            for i in range(4): self.pieces(c, i)
            before = [tuple(r) for r in c.execute('SELECT * FROM workbench_progress_totals ORDER BY 1,2')]
            c.execute('SAVEPOINT rollback_test')
            c.execute('DELETE FROM tasks WHERE id=\'task0\'')
            self.equivalent(c)
            c.execute('ROLLBACK TO rollback_test'); c.execute('RELEASE rollback_test')
            self.assertEqual(before, [tuple(r) for r in c.execute('SELECT * FROM workbench_progress_totals ORDER BY 1,2')])
            for table in ('workbench_candidates','workbench_identity_claims','task_results','workbench_collection_exclusions'):
                c.execute(f'DELETE FROM {table} WHERE account_id=?', ('account0',))
                self.equivalent(c)
            c.execute('DROP TRIGGER trg_progress_workbench_candidates_update')
            c.execute("UPDATE workbench_candidates SET source_mode=NULL")
            initialize_workbench_progress_aggregates(c)
            self.equivalent(c)
            c.execute('UPDATE workbench_progress_totals SET qualified=9999')
            rebuild_workbench_progress_aggregates(c)
            self.equivalent(c)
            self.assertTrue(c.in_transaction, 'rebuild must not commit restore transaction')
            c.execute('DROP TABLE workbench_progress_totals')
            initialize_workbench_progress_aggregates(c)
            self.equivalent(c)

    def test_opaque_legacy_checkpoint_mode_keeps_sqlite_source_value_semantics(self):
        with self.db.write() as c:
            self.pieces(c, 0, order=('result', 'claim', 'exclusion'))
            # SQLite exposes nested array/object values as text, while numeric
            # values are numbers. Preserve the old Python evidence-map behavior
            # even for an opaque legacy mode outside today's supported modes.
            for mode, sources in [('[' + '"followers"' + ']', '[[' + '"followers"' + ']]'),
                                  ('1', '[1]')]:
                c.execute("UPDATE task_results SET sources_json=? WHERE account_id='account0'", (sources,))
                c.execute("INSERT INTO task_checkpoints(id,task_id,target_id,mode,stage,cursor_json,counters_json,updated_at) VALUES(?,'task0','target0',?,'saved','{}',?,?)",
                          ('opaque'+mode, mode, json.dumps({'saved':1,'processed':1}), NOW))
                self.equivalent(c)

    def test_noop_and_payload_updates_do_not_rewrite_progress(self):
        with self.db.write() as c:
            self.pieces(c, 0)
            before = c.total_changes
            c.execute("UPDATE workbench_candidates SET source_mode=source_mode,source_target=source_target WHERE account_id='account0'")
            c.execute("UPDATE workbench_identity_claims SET source=source,source_target=source_target WHERE account_id='account0'")
            c.execute("UPDATE task_results SET sources_json=sources_json,profile_json='{}' WHERE account_id='account0'")
            self.assertEqual(3, c.total_changes-before,
                             'unchanged proof must not rewrite projection rows')
            self.equivalent(c)

    def test_changed_trigger_definition_rebuilds_exact_evidence(self):
        with self.db.write() as c:
            self.pieces(c, 0)
            c.execute('DROP TRIGGER trg_progress_workbench_candidates_update')
            c.execute("""CREATE TRIGGER trg_progress_workbench_candidates_update
                AFTER UPDATE ON workbench_candidates BEGIN SELECT 1; END""")
            c.execute("UPDATE workbench_candidates SET source_mode='following' WHERE account_id='account0'")
            initialize_workbench_progress_aggregates(c)
            self.equivalent(c)

    def test_restart_does_not_reaggregate_history(self):
        with self.db.write() as c:
            for i in range(5): self.pieces(c, i)
            statements = []
            c.set_trace_callback(statements.append)
            initialize_workbench_progress_aggregates(c)
            c.set_trace_callback(None)
            self.assertFalse(any(s.lstrip().startswith('INSERT INTO workbench_progress_contributions') for s in statements))
            self.equivalent(c)


if __name__ == '__main__': unittest.main()
