"""Posting preflight uses the same durable fence as actual Core admission."""
import json
import unittest

from app.errors import ConflictError
from app.posting_workflow import PostingManager
import test_studio as studio_fixtures
from test_posting_workflow_v2 import Provider


class PostingDurablePreflightTests(unittest.IsolatedAsyncioTestCase):
    setUp=studio_fixtures.StudioTests.setUp
    tearDown=studio_fixtures.StudioTests.tearDown

    def prepare_fixture(self,kind,owner=None):
        owner=owner or self.owner
        manager=PostingManager(self.s,self.browser,provider=Provider(self.db));manager.recover()
        with self.db.write() as c:
            c.execute("INSERT INTO posting_account_snapshots(owner_user_id,profile_id,username,status,checked_at) VALUES(?,'w1','verified.fixture','ok','now')",(self.owner,))
            if kind=='nurture':
                c.execute("""INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,result_json,due_at,created_at,updated_at)
                    VALUES('durable-holder',?,'durable-holder','nurture','w1','completed','{}',?,'now','now','now')""",
                    (owner,json.dumps({'window_hold':True,'window_cleanup':{'state':'lease_lost'}})))
            else:
                c.execute("""INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,profile_id,lease_token,status,created_at,updated_at)
                    VALUES('durable-holder',?,'durable-holder','theme','caption','w1','missing-row-token','needs_review','now','now')""",(owner,))
        return manager

    async def assert_preflight(self,kind,owner=None):
        manager=self.prepare_fixture(kind,owner)
        ident=manager.generate(self.owner,{'request_id':'preflight-test-001','theme':'fixture','caption':'exact','count':1})['job_ids'][0]
        await manager._prepare(manager.get(self.owner,ident))
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET profile_id='w1',expected_username='verified.fixture' WHERE id=?",(ident,))
        row=manager.get(self.owner,ident)
        with self.assertRaises(ConflictError) as admission:
            self.s.acquire_browser_lease(self.owner,'w1',operation_type='posting',entity_id=ident)
        with self.assertRaises(ConflictError) as start:
            await manager.command(self.owner,{'action':'start','job_ids':[ident],'reviewed':[{key:row[key] for key in ('id','caption','asset_id','profile_id','expected_username','queue_revision')}]})
        self.assertEqual(admission.exception.details,start.exception.details)
        self.assertEqual(row,manager.get(self.owner,ident))
        with self.assertRaises(ConflictError) as assign:
            await manager.command(self.owner,{'action':'assign','job_id':ident,'profile_id':'w1','expected_username':'verified.fixture'})
        self.assertEqual(admission.exception.details,assign.exception.details)
        self.assertEqual(row,manager.get(self.owner,ident))
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])
        self.assertEqual([],self.browser.closed)
        return manager,ident,admission.exception.details

    async def test_missing_nurture_lease_blocks_start_and_assign_identically_to_real_admission(self):
        manager,ident,_=await self.assert_preflight('nurture')
        # Editing and cancelling a genuinely ready draft remain local and safe.
        self.assertTrue(next(row for row in manager.snapshot(self.owner)['jobs'] if row['id']==ident)['can_modify'])
        await manager.command(self.owner,{'action':'edit','job_id':ident,'caption':'updated draft'})
        await manager.command(self.owner,{'action':'cancel','job_id':ident})
        with self.db.read() as c:
            self.assertTrue(json.loads(c.execute("SELECT result_json FROM studio_jobs WHERE id='durable-holder'").fetchone()[0])['window_hold'])

    async def test_missing_posting_lease_blocks_start_and_assign_identically_to_real_admission(self):
        await self.assert_preflight('posting')

    async def test_foreign_durable_hold_identity_is_not_disclosed(self):
        _,_,details=await self.assert_preflight('nurture',self.other)
        self.assertIsNone(details['entity_id'])


if __name__=='__main__':unittest.main()
