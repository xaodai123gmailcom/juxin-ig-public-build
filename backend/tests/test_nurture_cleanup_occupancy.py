"""Account/workbench inventory must show the exact admission fence, without writes."""
import asyncio
import json
import unittest
from app.account_workspace import AccountWorkspace
from app.errors import ConflictError
import test_studio as fixtures

class CleanupOccupancyTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown

    def seed(self,status='completed'):
        job=asyncio.run(self.m.command(self.owner,{'action':'start','kind':'nurture','profile_ids':['w1'],'request_id':'occupancy-fixture','config':{'minutes':1}}))['job_ids'][0]
        self.m.update(job,status=status,result_json=json.dumps({'window_hold':True,'window_cleanup':{'state':'lease_lost'}}))
        with self.db.write() as c:c.execute("UPDATE studio_jobs SET deleted_at='2026-10-03' WHERE id=?",(job,))
        return job

    def test_lease_less_archived_hold_is_visible_in_both_inventories_without_creating_lease(self):
        job=self.seed()
        states=self.s.list_browser_lease_states(self.owner)
        self.assertEqual(1,len(states));self.assertEqual('cleanup_pending',states[0]['state'])
        self.assertEqual(job,states[0]['entity_id']);self.assertTrue(states[0]['owned_by_current_login'])
        self.assertIsNone(self.s.list_browser_lease_states(self.other)[0]['entity_id'])
        accounts=AccountWorkspace(self.s,self.browser)
        row=accounts.snapshot(self.owner)['locks']['w1']
        self.assertTrue(row['cleanup_required']);self.assertEqual('studio',row['operation_type'])
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        with self.assertRaises(ConflictError):self.s.acquire_browser_lease(self.owner,'w1',operation_type='account',entity_id='open')

    def test_actual_successor_lease_is_never_overwritten_or_deleted_by_projection(self):
        self.seed()
        with self.db.write() as c:c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?,?,?,?)',
            ('w1',self.other,'account','successor','live-token','2026-10-04','2026-10-04','2099-01-01'))
        states=self.s.list_browser_lease_states(self.owner)
        self.assertEqual(1,len(states));self.assertEqual('account',states[0]['operation_type'])
        self.assertIsNone(states[0]['entity_id']);self.assertFalse(states[0]['owned_by_current_login'])
        row=AccountWorkspace(self.s,self.browser).snapshot(self.owner)['locks']['w1']
        self.assertTrue(row['cleanup_required']);self.assertEqual('account',row['operation_type'])
        with self.db.read() as c:self.assertEqual('live-token',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])

    def test_failed_historical_flag_does_not_manufacture_occupancy(self):
        self.seed(status='failed')
        self.assertEqual([],self.s.list_browser_lease_states(self.owner))
        self.assertEqual({},AccountWorkspace(self.s,self.browser).snapshot(self.owner)['locks'])
