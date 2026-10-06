"""Source-query equivalence, targeted mutation, upgrade and restore guarantees."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.workbench_aggregates import (initialize_workbench_aggregates, rebuild_workbench_aggregates,
                                     snapshot_totals, _trigger_definitions)
from test_pure_ig_data_migration_r5 import insert, NOW


class WorkbenchAggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'counts.sqlite')
        self.db.initialize()
        with self.db.write() as c:
            for owner in ('one','two'):
                insert(c,'app_users',id=owner,username_norm=owner,username_display=owner,password_hash='synthetic',created_at=NOW)
            for index in range(4):
                owner='one' if index<3 else 'two';platform='facebook' if index==2 else 'instagram'
                name=('fb:' if index==2 else '')+'person'+str(index)
                insert(c,'tasks',id='task'+str(index),owner_user_id=owner,name='task',status='running',modes_json='[]',settings_json=json.dumps({'platform':platform}),created_at=NOW,updated_at=NOW)
                insert(c,'task_targets',id='target'+str(index),task_id='task'+str(index),username_norm=name,username_display=name,queue_order=1,created_at=NOW,updated_at=NOW)
                insert(c,'instagram_accounts',id='account'+str(index),current_username_norm=name,current_username_display=name,first_seen_at=NOW,last_seen_at=NOW)
                insert(c,'instagram_username_aliases',account_id='account'+str(index),username_norm=name,first_seen_at=NOW,last_seen_at=NOW)
                insert(c,'task_results',id='result'+str(index),task_id='task'+str(index),target_id='target'+str(index),account_id='account'+str(index),sources_json='["followers"]',visibility='public' if index%2==0 else 'private',profile_json='{}',screening_json='{}',created_at=NOW,updated_at=NOW)
                insert(c,'workbench_candidates',id='candidate'+str(index),owner_user_id=owner,account_id='account'+str(index),visibility='public' if index%2==0 else 'private',status='approved',reviewed_at=NOW,created_at=NOW,updated_at=NOW)
                insert(c,'workbench_identity_claims',account_id='account'+str(index),claimed_by_user_id=owner,source='followers',claimed_at=NOW)
                insert(c,'split_candidate_history',id='history'+str(index),owner_user_id=owner,username_norm=name,username_display=name,completed_at=NOW,created_at=NOW,updated_at=NOW)
                insert(c,'workbench_collection_exclusions',id='excluded'+str(index),account_id='account'+str(index),owner_user_id=owner,username_display=name,reason_code='fixture',reason='fixture',excluded_at=NOW)
            for name in ('trg_action_success_no_update','trg_action_success_no_delete','trg_workbench_dismissal_no_update','trg_workbench_dismissal_no_delete',
                         'trg_workbench_exclusion_no_update','trg_workbench_exclusion_no_delete'):
                c.execute('DROP TRIGGER '+name)
        self.assert_exact()

    def tearDown(self):self.tmp.cleanup()

    @staticmethod
    def oracle(c,owner):
        result={}
        def scalar(metric,sql,params=()):result[metric]=int(c.execute(sql,params).fetchone()[0] or 0)
        scalar('claimed',"SELECT COUNT(*) FROM workbench_identity_claims r JOIN instagram_accounts a ON a.id=r.account_id WHERE a.current_username_norm NOT GLOB 'fb:*'")
        for metric,where in (('total_collected','1'),('total_public',"r.visibility='public'"),('total_private',"r.visibility='private'")):
            scalar(metric,"SELECT COUNT(*) FROM task_results r JOIN tasks t ON t.id=r.task_id JOIN instagram_accounts a ON a.id=r.account_id WHERE t.owner_user_id=? AND CASE WHEN json_valid(t.settings_json) THEN json_type(t.settings_json)='object' AND (json_type(t.settings_json,'$.platform') IS NULL OR (json_type(t.settings_json,'$.platform')='text' AND json_extract(t.settings_json,'$.platform')='instagram')) ELSE 0 END AND a.current_username_norm NOT GLOB 'fb:*' AND "+where,(owner,))
        scalar('total_split',"SELECT COUNT(*) FROM split_candidate_history WHERE owner_user_id=? AND username_norm NOT GLOB 'fb:*'",(owner,))
        scalar('collection_excluded',"SELECT COUNT(*) FROM workbench_collection_exclusions WHERE owner_user_id=? AND username_display NOT GLOB 'fb:*'",(owner,))
        scalar('approved_dismissed',"SELECT COUNT(*) FROM workbench_candidate_dismissals d JOIN workbench_candidates c ON c.id=d.candidate_id JOIN instagram_accounts a ON a.id=c.account_id WHERE d.owner_user_id=? AND a.current_username_norm NOT GLOB 'fb:*'",(owner,))
        for status,visibility,count in c.execute("SELECT c.status,c.visibility,COUNT(*) FROM workbench_candidates c JOIN instagram_accounts a ON a.id=c.account_id WHERE c.owner_user_id=? AND a.current_username_norm NOT GLOB 'fb:*' GROUP BY c.status,c.visibility",(owner,)):
            result['candidate:'+status+':'+visibility]=count
        for operation,count in c.execute("SELECT operation,COUNT(*) FROM action_success_ledger WHERE owner_user_id=? AND username_norm NOT GLOB 'fb:*' GROUP BY operation",(owner,)):
            result['success:'+operation]=count
        for visibility in ('public','private'):
            operation='greet' if visibility=='public' else 'follow'
            scalar('approved:'+visibility,"""SELECT COUNT(*) FROM workbench_candidates c JOIN instagram_accounts a ON a.id=c.account_id
                WHERE c.owner_user_id=? AND c.status='approved' AND c.visibility=? AND a.current_username_norm NOT GLOB 'fb:*'
                  AND NOT EXISTS(SELECT 1 FROM workbench_candidate_dismissals d WHERE d.candidate_id=c.id)
                  AND NOT EXISTS(SELECT 1 FROM instagram_username_aliases alias WHERE alias.account_id=c.account_id
                    AND EXISTS(SELECT 1 FROM action_success_ledger s WHERE s.owner_user_id=? AND s.operation=?
                      AND s.username_norm=alias.username_norm AND s.username_norm NOT GLOB 'fb:*'))""",(owner,visibility,owner,operation))
        return {k:v for k,v in result.items() if v}

    def assert_exact(self,c=None):
        if c is None:
            with self.db.read() as c:self.assert_exact(c)
            return
        for owner in ('one','two'):
            expected=self.oracle(c,owner);actual={k:v for k,v in snapshot_totals(c,owner).items() if v}
            self.assertEqual(expected,actual,owner)
            for visibility in ('public','private'):
                rows=list(c.execute('SELECT candidate_id FROM workbench_actionable_candidates WHERE owner_user_id=? AND visibility=? ORDER BY reviewed_at DESC,candidate_id DESC',(owner,visibility)))
                self.assertEqual(expected.get('approved:'+visibility,0),len(rows))
        self.assertFalse(list(c.execute('PRAGMA foreign_key_check')))

    def test_status_owner_platform_and_source_mutation_matrix(self):
        cases=[
            "UPDATE workbench_candidates SET status='pending' WHERE id='candidate0'",
            "UPDATE workbench_candidates SET status='rejected',visibility='private' WHERE id='candidate0'",
            "UPDATE workbench_candidates SET owner_user_id='two' WHERE id='candidate0'",
            "UPDATE workbench_candidates SET reviewed_at='2030-01-01' WHERE id='candidate0'",
            "UPDATE instagram_accounts SET current_username_norm='fb:changed' WHERE id='account0'",
            "UPDATE instagram_accounts SET current_username_norm='legacy.ig' WHERE id='account2'",
            "UPDATE tasks SET owner_user_id='two' WHERE id='task0'",
            "UPDATE tasks SET settings_json='{\"platform\":\"facebook\"}' WHERE id='task0'",
            "UPDATE tasks SET settings_json='{}' WHERE id='task2'",
            "UPDATE task_results SET visibility='excluded' WHERE id='result0'",
            "UPDATE split_candidate_history SET owner_user_id='two',username_norm='fb:changed' WHERE id='history0'",
            "UPDATE workbench_collection_exclusions SET owner_user_id='two',username_display='fb:changed' WHERE id='excluded0'",
            "UPDATE workbench_identity_claims SET claimed_by_user_id='two' WHERE account_id='account0'",
            "DELETE FROM tasks WHERE id='task0'",
            "DELETE FROM workbench_candidates WHERE id='candidate0'",
            "DELETE FROM workbench_identity_claims WHERE account_id='account0'",
            "DELETE FROM split_candidate_history WHERE id='history0'",
            "DELETE FROM workbench_collection_exclusions WHERE id='excluded0'",
        ]
        for sql in cases:
            with self.subTest(sql=sql),self.db.read() as c:
                c.execute('BEGIN IMMEDIATE');c.execute(sql);self.assert_exact(c);c.rollback();self.assert_exact(c)

    def success(self,c,username='person0',owner='one',operation='greet'):
        insert(c,'action_success_ledger',owner_user_id=owner,operation=operation,username_norm=username,username_display=username,
               campaign_id='history-only',target_id='history-only',attempt_id=username,completed_at=NOW)

    def test_alias_success_dismissal_immediate_dependency_matrix(self):
        with self.db.write() as c:
            self.success(c);self.assert_exact(c)
            c.execute("UPDATE action_success_ledger SET owner_user_id='two'");self.assert_exact(c)
            c.execute("UPDATE action_success_ledger SET owner_user_id='one',operation='follow'");self.assert_exact(c)
            c.execute("UPDATE action_success_ledger SET operation='greet',username_norm='renamed'");self.assert_exact(c)
            insert(c,'instagram_username_aliases',account_id='account0',username_norm='renamed',first_seen_at=NOW,last_seen_at=NOW);self.assert_exact(c)
            c.execute("UPDATE instagram_username_aliases SET account_id='account1' WHERE username_norm='renamed'");self.assert_exact(c)
            c.execute("UPDATE action_success_ledger SET operation='follow'");self.assert_exact(c)
            c.execute("DELETE FROM instagram_username_aliases WHERE username_norm='renamed'");self.assert_exact(c)
            insert(c,'workbench_candidate_dismissals',id='dismiss',candidate_id='candidate1',owner_user_id='two',dismissed_at=NOW);self.assert_exact(c)
            c.execute("UPDATE workbench_candidate_dismissals SET owner_user_id='one',candidate_id='candidate0'");self.assert_exact(c)
            c.execute("DELETE FROM workbench_candidate_dismissals");self.assert_exact(c)
            c.execute("DELETE FROM action_success_ledger");self.assert_exact(c)

    def test_canonical_account_change_reconciles_dismissal_owner_and_availability(self):
        with self.db.write() as c:
            insert(c,'instagram_accounts',id='spare',current_username_norm='fb:spare',current_username_display='spare',first_seen_at=NOW,last_seen_at=NOW)
            insert(c,'workbench_candidate_dismissals',id='dismiss',candidate_id='candidate0',owner_user_id='two',dismissed_at=NOW)
            c.execute("UPDATE workbench_candidates SET account_id='spare' WHERE id='candidate0'");self.assert_exact(c)
            c.execute("UPDATE instagram_accounts SET current_username_norm='ig.spare' WHERE id='spare'");self.assert_exact(c)

    def test_restart_missing_trigger_repair_and_restore_rebuild_rollback(self):
        with self.db.write() as c:
            c.execute('DROP TRIGGER trg_workbench_aggregate_candidate_update')
            c.execute("UPDATE workbench_candidates SET status='pending' WHERE id='candidate0'")
        self.db.initialize();self.assert_exact()
        with self.db.read() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM workbench_snapshot_counts ORDER BY 1,2')]
            c.execute('BEGIN IMMEDIATE');c.execute("UPDATE workbench_candidates SET owner_user_id='two' WHERE id='candidate0'")
            rebuild_workbench_aggregates(c);self.assertTrue(c.in_transaction);self.assert_exact(c);c.rollback()
            self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM workbench_snapshot_counts ORDER BY 1,2')])

    def test_projection_corruption_can_rebuild_without_negative_counter_failure(self):
        with self.db.write() as c:
            c.execute('UPDATE workbench_snapshot_counts SET total=0')
            rebuild_workbench_aggregates(c);self.assert_exact(c)

    def test_task_settings_scope_preserves_missing_but_excludes_invalid_or_explicit_null(self):
        for settings in ('{}','{"platform":"instagram"}','{"platform":null}','[]','null','{broken','{"platform":42}'):
            with self.subTest(settings=settings),self.db.read() as c:
                c.execute('BEGIN IMMEDIATE');c.execute("UPDATE tasks SET settings_json=? WHERE id='task0'",(settings,))
                self.assert_exact(c);c.rollback()

    def test_success_insert_vm_does_not_grow_with_permanent_history(self):
        steps=[];size=0
        with self.db.write() as c:
            for population in (1000,20000):
                rows=[('one','greet','history'+str(i),'History','old','old','old'+str(i),NOW) for i in range(size,population)]
                c.executemany('INSERT INTO action_success_ledger VALUES(?,?,?,?,?,?,?,?)',rows);size=population
                counter=[0]
                def progress():counter[0]+=1;return 0
                c.set_progress_handler(progress,1)
                self.success(c)
                c.set_progress_handler(None,0);steps.append(counter[0])
                c.execute("DELETE FROM action_success_ledger WHERE username_norm='person0'")
                self.assert_exact(c)
        self.assertLessEqual(steps[1],steps[0]+100,steps)

    def test_ordinary_restart_does_not_rebuild_memberships(self):
        statements=[]
        with self.db.read() as c:
            c.set_trace_callback(statements.append);initialize_workbench_aggregates(c)
        self.assertFalse(any(s.startswith('DELETE FROM workbench_aggregate_members') for s in statements))

    def test_metric_and_page_reads_are_indexed_and_success_refresh_seeks_expression_key(self):
        with self.db.read() as c:
            for sql in (
                "SELECT metric,total FROM workbench_snapshot_counts WHERE owner_user_id='one'",
                "SELECT candidate_id FROM workbench_actionable_candidates WHERE owner_user_id='one' AND visibility='public' ORDER BY reviewed_at DESC,candidate_id DESC LIMIT 20",
                "SELECT r.owner_user_id FROM action_success_ledger r WHERE json_array(r.owner_user_id,r.operation,r.username_norm) IN (json_array('one','greet','person0'))",
            ):
                plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN '+sql))
                self.assertIn('SEARCH',plan);self.assertNotIn('TEMP B-TREE',plan)


if __name__=='__main__':unittest.main()
