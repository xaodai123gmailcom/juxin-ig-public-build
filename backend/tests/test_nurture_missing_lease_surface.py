"""Missing transient rows never turn durable nurture holds into manual access."""
import json
import unittest
from app.errors import ConflictError
import test_account_surface as fixtures

class NurtureMissingLeaseSurfaceTests(unittest.TestCase):
    tearDown=fixtures.AccountSurfaceTests.tearDown
    def setUp(self):
        fixtures.AccountSurfaceTests.setUp(self)
        with self.db.write() as c:
            c.execute('''INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,
                config_json,result_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                ('held',self.owner,'held','nurture',self.profile,'completed','{}',
                 json.dumps({'window_hold':True,'window_cleanup':{'state':'lease_lost'}}),
                 '2026-10-03','2026-10-03','2026-10-03'))

    def test_missing_row_denies_both_interactive_and_readonly_surfaces(self):
        for body in (self.body,{**self.body,'read_only':True,'view_target':'old-target'}):
            with self.assertRaisesRegex(ConflictError,'养号窗口清理仍待核验'):
                self.surface.update(self.owner,self.row,body)
        self.assertEqual([],self.adapter.shows)

    def test_reconciled_hold_and_failed_legacy_flag_do_not_block_normal_surface(self):
        with self.db.write() as c:c.execute("UPDATE studio_jobs SET result_json=json_set(result_json,'$.window_hold',json('false'))")
        self.assertTrue(self.surface.update(self.owner,self.row,self.body)['attached'])
        with self.db.write() as c:c.execute("UPDATE studio_jobs SET status='failed',result_json=json_set(result_json,'$.window_hold',json('true'))")
        self.assertTrue(self.surface.update(self.owner,self.row,self.body)['attached'])

    def test_unrelated_profile_is_not_blocked_or_closed(self):
        with self.db.write() as c:c.execute("UPDATE studio_jobs SET profile_id='other-profile'")
        self.assertTrue(self.surface.update(self.owner,self.row,self.body)['attached'])
        self.assertFalse(self.adapter.closed)
