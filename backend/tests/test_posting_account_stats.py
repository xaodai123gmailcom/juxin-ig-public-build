"""Stored window counts use confirmed posting records, independent of recent-job limits."""
import asyncio,json,unittest
from unittest.mock import patch
import test_studio as fixtures
from app.posting_account_stats import save_account_snapshot
from app.account_workspace import AccountWorkspace
from app.errors import ConflictError

class AccountStatsTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start

    def test_posts_zero_missing_and_account_change_are_distinct(self):
        save_account_snapshot(self.db,self.owner,'w1',{'username':'old','posts_count':0})
        row=self.m.snapshot(self.owner)['window_stats'][0]
        self.assertEqual(0,row['posts_count']);self.assertEqual('ok',row['status'])
        save_account_snapshot(self.db,self.owner,'w1',{'username':'new','posts_count':None,'message':'请登录'})
        row=self.m.snapshot(self.owner)['window_stats'][0]
        self.assertIsNone(row['posts_count']);self.assertEqual('new',row['username']);self.assertEqual('请登录',row['message'])
        self.assertEqual([],self.m.snapshot(self.other)['window_stats'])

    def test_success_counts_only_confirmed_posts_once_per_job(self):
        async def run():
            for i,(kind,status,published) in enumerate([('posting','completed',1),('posting','needs_review',1),('posting','failed',0),('material','completed',1),('posting','completed',0)]):
                ident=(await self.start(kind=kind,key='stats-request-'+str(i)))['job_ids'][0]
                self.m.update(ident,status=status,result_json=json.dumps({'published':published}))
                if i==0:self.m.update(ident,status=status,result_json=json.dumps({'published':published,'refreshed':True}))
        asyncio.run(run())
        rows=self.m.snapshot(self.owner)['window_stats']
        self.assertEqual(1,rows[0]['published_count']);self.assertEqual('w1',rows[0]['profile_id'])
        self.assertIsNone(rows[0]['posts_count']);self.assertEqual([],self.m.snapshot(self.other)['window_stats'])

    def test_open_records_fresh_snapshot_while_window_is_locked(self):
        counts=iter([9,10])
        def opener(*args):
            with self.assertRaises(ConflictError):self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='other')
            return {'opened':True,'instagram_stats':{'username':'me','posts_count':next(counts),'status':'ok'}}
        manager=AccountWorkspace(self.s,self.browser,opener=opener)
        for expected in (9,10):
            manager.control_profile(self.owner,'w1','open')
            self.assertEqual(expected,self.m.snapshot(self.owner)['window_stats'][0]['posts_count'])
        token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='active')
        with self.assertRaises(ConflictError):manager.control_profile(self.owner,'w1','open')
        self.s.release_browser_lease('w1',token)
