"""Durable per-target Reel decisions are fenced by the existing collection lease."""
import tempfile
import unittest
from pathlib import Path
from app.database import Database
from app.service import CoreService
from app.errors import ValidationError

class ParentReelDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.database=Database(Path(self.temp.name)/'test.sqlite3');self.database.initialize()
        self.service=CoreService(self.database);self.owner=self.service.register_user('reel-owner','fixture-password')['id']
        self.task=self.service.create_task(self.owner,name='collector',modes=['followers'],targets=['source'],window_ids=['window'],settings={'live_queue_enabled':False})
        self.tid=self.task['id'];self.target=self.task['targets'][0]['id']
        self.token=self.service.acquire_browser_lease(self.owner,'window',operation_type='collection',entity_id=self.tid)
        self.service.set_task_runtime_status(self.owner,self.tid,'running')
        self.service.set_target_runtime_status(self.owner,self.tid,self.target,'running',window_id='window')
        self.service.discover_task_mode_candidate(self.owner,self.tid,self.target,'followers','pending_person')
        self.cursor={'candidate_spool_version':1,'candidate_spool_complete':True,'candidate_spool_natural_end':True,'pending_relation_usernames':[]}
        self.checkpoint(self.cursor)
    def checkpoint(self,cursor):
        self.service.upsert_checkpoint(self.owner,self.tid,self.target,mode='followers',stage='screening_accounts',cursor=cursor,counters={'source_total':2,'discovered':1,'pending_candidates':1})
    def claim(self,key='/reel/Stable1',selected=True,token=None):
        return self.service.claim_parent_reel_decision(self.owner,self.tid,self.target,'window',key,selected,lease_token=self.token if token is None else token)
    def rows(self):
        with self.database.read() as c:return [tuple(row) for row in c.execute('SELECT * FROM task_parent_reel_decisions')]
    def test_claim_precedes_click_and_ambiguous_or_skip_decisions_never_repeat(self):
        self.assertTrue(self.claim());before=self.rows();self.assertFalse(self.claim());self.assertEqual(before,self.rows())
        self.assertTrue(self.claim('/reel/Skipped',False));self.assertFalse(self.claim('/reel/Skipped',True))
        self.assertEqual(0,self.rows()[1][4])
    def test_restart_and_reacquired_window_do_not_reset_existing_decision(self):
        self.assertTrue(self.claim())
        self.service.release_browser_lease('window',self.token)
        self.database.initialize()
        self.service=CoreService(self.database)
        self.token=self.service.acquire_browser_lease(self.owner,'window',operation_type='collection',entity_id=self.tid)
        self.assertFalse(self.claim())
        self.assertTrue(self.claim('/reel/NewAfterRestart'))
    def test_expired_replaced_owner_and_paused_state_cannot_claim(self):
        self.assertFalse(self.claim(token='wrong-generation'))
        self.service.set_task_runtime_status(self.owner,self.tid,'paused')
        self.assertFalse(self.claim())
        self.assertEqual([],self.rows())
    def test_unfinished_or_unconfirmed_source_cannot_claim(self):
        for cursor in ({**self.cursor,'candidate_spool_natural_end':False},
                       {**self.cursor,'pending_relation_usernames':['not_confirmed']},
                       {'resume_cursor':{**self.cursor,'pending_relation_usernames':['not_confirmed']}}):
            self.checkpoint(cursor);self.assertFalse(self.claim())
        self.assertEqual([],self.rows())
    def test_completion_race_without_pending_details_cannot_claim(self):
        with self.database.write() as c:
            c.execute("UPDATE task_mode_candidates SET state='deduped' WHERE target_id=?",(self.target,))
        self.assertFalse(self.claim());self.assertEqual([],self.rows())
    def test_only_final_followers_mode_and_all_source_checkpoints_qualify(self):
        with self.database.write() as c:c.execute("UPDATE tasks SET modes_json='[\"followers\",\"following\"]' WHERE id=?",(self.tid,))
        self.assertFalse(self.claim())
        with self.database.write() as c:c.execute("UPDATE tasks SET modes_json='[\"following\",\"followers\"]' WHERE id=?",(self.tid,))
        self.assertFalse(self.claim())
        self.service.upsert_checkpoint(self.owner,self.tid,self.target,mode='following',stage='mode_completed',cursor=self.cursor,counters={})
        self.assertTrue(self.claim())
    def test_platform_and_key_scope_are_fail_closed(self):
        with self.database.write() as c:c.execute("UPDATE tasks SET settings_json=json_set(settings_json,'$.platform','facebook') WHERE id=?",(self.tid,))
        with self.assertRaises(ValidationError):self.claim()
        self.assertEqual([],self.rows())
        with self.database.write() as c:c.execute("UPDATE tasks SET settings_json=json_set(settings_json,'$.platform','instagram') WHERE id=?",(self.tid,))
        for key in ('https://evil.invalid/reel/a','/reel/../../bad','/reel/a?x=1',''):
            with self.subTest(key=key),self.assertRaises(ValidationError):self.claim(key)
        with self.assertRaises(ValidationError):self.claim(selected='yes')
