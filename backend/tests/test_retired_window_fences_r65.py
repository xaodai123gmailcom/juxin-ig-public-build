"""A missing transient lease must not expose a durably held posting window."""
import unittest
from unittest.mock import Mock
from app.errors import ConflictError
from app.posting_schema import initialize_posting_schema
import test_account_surface as fixtures

class PostingMissingLeaseSurfaceR6Tests(unittest.TestCase):
    tearDown=fixtures.AccountSurfaceTests.tearDown
    def setUp(self):
        fixtures.AccountSurfaceTests.setUp(self)
        with self.db.write() as connection:
            initialize_posting_schema(connection)
            connection.execute('''INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,
                profile_id,lease_token,status,created_at,updated_at)
                VALUES('durable-posting',?,'durable-posting-request','fixture','exact',?,
                       'durable-posting-token','needs_review','2026-10-02','2026-10-02')''',
                (self.owner,self.profile))
            self.assertEqual(0,connection.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])

    def test_missing_browser_row_blocks_interactive_surface(self):
        with self.assertRaises(ConflictError):
            self.surface.update(self.owner,self.row,self.body)
        self.assertEqual([],self.adapter.shows)

    def test_missing_browser_row_blocks_translation_apply_before_native_ipc(self):
        native=Mock(return_value={'applied':True});self.native.chat_translation=native
        with self.assertRaises(ConflictError):
            self.manager.command(self.owner,{'action':'chat_translation','id':self.ident,'step':{'kind':'apply'}})
        native.assert_not_called()

    def test_missing_browser_row_also_rejects_readonly_task_view(self):
        with self.assertRaises(ConflictError):
            self.surface.update(self.owner,self.row,{**self.body,'read_only':True,'view_target':'task-page'})
        self.assertEqual([],self.adapter.shows)

    def test_normal_posting_row_keeps_existing_readonly_policy(self):
        from app.service import isoformat
        with self.db.write() as c:
            c.execute('''INSERT INTO browser_operation_leases(profile_id,owner_user_id,operation_type,
                entity_id,lease_token,acquired_at,heartbeat_at,expires_at) VALUES(?,?,'posting',
                'durable-posting','durable-posting-token',?,?,?)''',(self.profile,self.owner,isoformat(),isoformat(),'2100-01-01'))
        readonly=self.surface.update(self.owner,self.row,{**self.body,'read_only':True,'view_target':'task-page'})
        self.assertTrue(readonly['attached'])
        with self.assertRaises(ConflictError):self.surface.update(self.owner,self.row,self.body)
        with self.db.read() as c:
            self.assertEqual('durable-posting-token',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])

    def test_readonly_watch_uses_generic_operation_without_legacy_job_details(self):
        from types import SimpleNamespace
        from app.service import isoformat
        with self.db.write() as c:
            c.execute("""INSERT INTO browser_operation_leases(profile_id,owner_user_id,operation_type,
                entity_id,lease_token,acquired_at,heartbeat_at,expires_at) VALUES(?,?,'posting',
                'durable-posting','durable-posting-token',?,?,?)""",(self.profile,self.owner,isoformat(),isoformat(),'2100-01-01'))
        self.native.bridge=SimpleNamespace(call=Mock(return_value={'pages':[]}))
        watched=self.manager.watch_task(self.owner,self.ident)
        self.assertEqual('account',watched['operation'])
        self.assertNotIn('durable-posting',str(watched))
        with self.db.read() as c:
            self.assertEqual('durable-posting-token',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])

    def test_missing_hold_does_not_block_unrelated_profile_or_retired_hold(self):
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET profile_id='different-window'")
        self.assertTrue(self.surface.update(self.owner,self.row,self.body)['attached'])
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET profile_id=?,lease_token='',status='cancelled'",(self.profile,))
        self.assertTrue(self.surface.update(self.owner,self.row,self.body)['attached'])

    def test_generic_toolbar_cannot_bypass_missing_row_hold(self):
        for action in ('home','refresh','inbox','close','open','profile_preview'):
            with self.subTest(action=action),self.assertRaises(ConflictError):
                self.manager.command(self.owner,{'action':action,'id':self.ident})

if __name__=='__main__':unittest.main()
