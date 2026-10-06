"""Business regressions for reporting, batch creation and the shared identity gate."""
import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_workspace import AccountWorkspace
from app.database import Database
from app.errors import ConflictError, ValidationError
from app.native_browser import NativeBrowser, BrowserHub
from app.service import CoreService
from app.work_reports import work_report, history_totals
from app.follow_monitor import FollowMonitorManager


class NoLegacy:
    def list_all_windows(self): return {'windows': [], 'stale': False}


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'test.db'); self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('report-owner', 'correct horse battery staple')['id']
        self.other = self.service.register_user('report-other', 'correct horse battery staple')['id']
        self.native = NativeBrowser(self.db, self.root)
        self.hub = BrowserHub(self.native, NoLegacy())
        self.accounts = AccountWorkspace(self.service, self.hub)

    def tearDown(self):
        self.native.shutdown(); self.tmp.cleanup()

    def batch(self, **kwargs):
        return {'action': 'batch_create', 'request_key': str(uuid.uuid4()), 'prefix': '窗口', 'count': 4, 'start': 10, **kwargs}

    def test_batch_is_atomic_retry_idempotent_and_does_not_open_or_reassign(self):
        original = self.accounts.save(self.owner, {'name': '正在采集', 'native': True})['id']
        profile = self.accounts.get(self.owner, original)['profile_id']
        token = self.service.acquire_browser_lease(self.owner, profile, operation_type='collection', entity_id='running')
        body = self.batch()
        a = self.accounts.command(self.owner, body); b = self.accounts.command(self.owner, body)
        self.assertEqual(a['ids'], b['ids']); self.assertTrue(b['reused'])
        rows = self.accounts.snapshot(self.owner)['plans']
        self.assertEqual(5, len(rows)); self.assertEqual(5, len({r['profile_id'] for r in rows}))
        self.assertEqual(['窗口 10','窗口 11','窗口 12','窗口 13'], [r['name'] for r in rows[1:]])
        self.assertEqual([], self.accounts.snapshot(self.other)['plans'])
        self.assertEqual({}, self.native.processes)
        with self.db.read() as c:
            self.assertEqual(token, c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone()[0])
        with self.assertRaises(ConflictError): self.accounts.command(self.owner, {**body, 'count': 5})

    def test_batch_failure_rolls_back_every_new_window_and_can_retry(self):
        create = self.native.create
        calls = 0
        def fail(c, *args):
            nonlocal calls
            calls += 1
            if calls == 3: raise RuntimeError('fixture failure')
            return create(c, *args)
        body = self.batch()
        with patch.object(self.native, 'create', side_effect=fail), self.assertRaises(RuntimeError):
            self.accounts.command(self.owner, body)
        with self.db.read() as c:
            for table in ('native_browser_profiles','account_window_plans','account_creation_batches'):
                self.assertEqual(0,c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        self.assertEqual(4,self.accounts.command(self.owner,body)['created'])

    def test_split_sources_block_cross_owner_collection_and_survive_source_removal(self):
        self.service.upsert_manual_split_candidates(self.owner, [{'username':'@Split.Source','queued':True}])
        claimed = self.service.claim_workbench_identity(self.other,username='https://www.instagram.com/SPLIT.SOURCE/',source='following')
        self.assertTrue(claimed['duplicate']); self.assertFalse(claimed['should_collect_profile'])
        with self.db.write() as c:
            c.execute('DELETE FROM split_candidates WHERE owner_user_id=?',(self.owner,))
        self.assertTrue(self.service.check_global_dedupe('split.source')['seen'])

    def test_upgrade_restores_missing_registry_without_dropping_review_history(self):
        claim = self.service.claim_workbench_identity(self.owner,username='old.private',source='followers')
        cand = self.service.create_workbench_candidate(self.owner,claim_id=claim['claim_id'],username='old.private',visibility='private',profile={},screening={},review_cache={})
        with self.db.write() as c:
            c.execute('DELETE FROM global_seen WHERE account_id=?',(claim['claim_id'],))
            c.execute('DELETE FROM schema_migrations WHERE version=29')
        self.db.initialize(); self.db.initialize()
        self.assertTrue(self.service.check_global_dedupe('OLD.PRIVATE')['seen'])
        with self.db.read() as c:
            self.assertEqual(cand['id'],c.execute('SELECT id FROM workbench_candidates').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])

    def add_post(self, name, when, *, owner=None, published=1, status='completed', ident=None):
        ident = ident or str(uuid.uuid4())
        with self.db.write() as c:
            c.execute('''INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,result_json,due_at,created_at,updated_at)
              VALUES(?,?,?,'posting','same-window',?,'{}',?,?,?,?)''', (ident,owner or self.owner,ident,status,json.dumps({'published':published,'confirmed_at':when,'executor':{'username':name,'window_name':'执行窗口','instagram_user_id':name+'-id'}}),when,when,when))

    def test_report_counts_full_history_local_midnight_and_changed_accounts(self):
        # UTC+7 local September 13: start inclusive, following midnight exclusive.
        self.add_post('old.actor','2026-09-12T16:59:59+00:00')
        self.add_post('new.actor','2026-09-12T17:00:00+00:00')
        self.add_post('third.actor','2026-09-13T16:59:59+00:00')
        self.add_post('next.day','2026-09-13T17:00:00+00:00')
        self.add_post('failed.actor','2026-09-12T18:00:00+00:00',published=0,status='failed')
        self.add_post('foreign','2026-09-12T18:00:00+00:00',owner=self.other)
        with self.db.write() as c:
            for i in range(2001):
                ident=f'history-{i}'
                c.execute('''INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,result_json,due_at,created_at,updated_at)
                  VALUES(?,?,?,'posting','legacy-window','completed','{}','{"published":1}','2026-09-12T18:00:00+00:00','2026-09-12T18:00:00+00:00','2026-09-12T18:00:00+00:00')''',(ident,self.owner,ident))
        r=work_report(self.db,self.owner,'2026-09-13T00:00:00+07:00','2026-09-14T00:00:00+07:00')
        self.assertEqual(2003,r['totals']['posting'])
        self.assertEqual({'new.actor','third.actor',''}, {row['username'] for row in r['rows']})
        self.assertEqual(2001,r['unattributed'])

    def test_report_added_counts_follow_checks_period_owner_and_window_not_posts(self):
        entries = [
            (self.owner,'baseline','w1','actor.one',0,0,'2026-09-12T17:00:00+00:00'),
            (self.owner,'before','w1','actor.one',1,0,'2026-09-12T16:59:59+00:00'),
            (self.owner,'first','w1','actor.one',2,1,'2026-09-12T17:00:00+00:00'),
            (self.owner,'second','w1','actor.one',3,0,'2026-09-13T16:59:59+00:00'),
            (self.owner,'other-window','w2','actor.two',4,2,'2026-09-13T08:00:00+00:00'),
            (self.owner,'next-day','w1','actor.one',6,0,'2026-09-13T17:00:00+00:00'),
            (self.other,'foreign','w1','foreign.actor',99,0,'2026-09-13T08:00:00+00:00'),
        ]
        with self.db.write() as c:
            c.executemany("""INSERT INTO follow_monitor_rounds(owner_user_id,batch_id,profile_id,owner_username,
              actual_count,first_read_count,added_count,repeat_count,unfollow_count,checked_at)
              VALUES(?,?,?,?,100,100,?,?,0,?)""",entries)
        self.add_post('posting.actor','2026-09-13T08:00:00+00:00')
        start,end='2026-09-13T00:00:00+07:00','2026-09-14T00:00:00+07:00'
        report=work_report(self.db,self.owner,start,end)
        self.assertEqual(9,report['totals']['added'])  # repeats are already inside added_count
        self.assertEqual(1,report['totals']['posting'])
        self.assertEqual({'actor.one':5,'actor.two':4},{r['username']:r['added'] for r in report['rows'] if r['added']})
        self.assertEqual(9,sum(r['added'] for r in report['rows']))
        self.assertEqual(report,work_report(self.db,self.owner,start,end),'refresh is read-only')
        month=work_report(self.db,self.owner,'2026-09-01T00:00:00+07:00','2026-10-01T00:00:00+07:00')
        self.assertEqual(16,month['totals']['added'])

    def test_history_deduplicates_task_and_review_account_and_rejects_bad_dates(self):
        self.service.upsert_manual_split_candidates(self.owner,[{'username':'only.source'}])
        claim=self.service.claim_workbench_identity(self.owner,username='collected.private',source='followers')
        self.service.create_workbench_candidate(self.owner,claim_id=claim['claim_id'],username='collected.private',visibility='private',profile={},screening={},review_cache={})
        task=self.service.create_task(self.owner,name='same collected account',modes=['followers'],targets=['source.account'],settings={})
        self.service.record_result(self.owner,task['id'],task['targets'][0]['id'],username='collected.private',instagram_user_id=None,source_mode='followers',visibility='private',profile={},screening={},qualified=None,dedupe_claim_id=claim['claim_id'])
        r=history_totals(self.db,self.owner,'2000-01-01T00:00:00Z','2100-01-01T00:00:00Z')
        self.assertEqual(1,r['total_collected'])
        expected_seen={'only.source','source.account','collected.private'}
        self.assertEqual(len(expected_seen),r['global_dedupe'])
        with self.db.read() as c:
            self.assertEqual(expected_seen,{row[0] for row in c.execute(
                'SELECT a.current_username_norm FROM instagram_accounts a JOIN global_seen g ON g.account_id=a.id'
            )})
        for start,end in [('bad','2026-01-01T00:00:00Z'),('2026-01-01','2026-02-01'),('2026-02-01T00:00:00Z','2026-01-01T00:00:00Z')]:
            with self.assertRaises(ValidationError):work_report(self.db,self.owner,start,end)

    def test_action_reports_keep_execution_actor_and_exclude_preexisting_and_failed_actions(self):
        for operation in ('follow','greet'):
            campaign=self.service.create_action_campaign(self.owner,operation=operation,execution_type='campaign',profile_id='window-at-execution',targets=[operation+'.'+s for s in ('confirmed','already_done','failed','unknown')],message='hello',interval_min_seconds=0,interval_max_seconds=0,limit_count=4)
            for target,status in zip(campaign['targets'],('confirmed','already_done','failed','unknown')):
                attempt=self.service.start_action_attempt(self.owner,campaign['id'],target['id'],details={'executor':{'username':'actual.actor','instagram_user_id':'123','window_name':'执行时窗口'}})
                self.service.finish_action_attempt(self.owner,campaign['id'],attempt,status=status,details={'message':'fixture result'})
        report=work_report(self.db,self.owner,'2000-01-01T00:00:00Z','2100-01-01T00:00:00Z')
        self.assertEqual(1,report['totals']['follow']);self.assertEqual(1,report['totals']['greet'])
        self.assertEqual(1,len(report['rows']));self.assertEqual('actual.actor',report['rows'][0]['username'])
        self.assertEqual('执行时窗口',report['rows'][0]['window_name'])

    def test_executor_capture_uses_session_identity_and_never_saves_login_cookies(self):
        from app.work_reports import capture_executor
        context=SimpleNamespace(cookies=AsyncMock(return_value=[{'name':'sessionid','value':'do-not-save'},{'name':'ds_user_id','value':'12345'}]))
        worker=SimpleNamespace(_context=context,page=SimpleNamespace(evaluate=AsyncMock(return_value='Actual.Actor')))
        actor=asyncio.run(capture_executor(worker,self.db,self.owner,'window'))
        self.assertEqual('actual.actor',actor['username']);self.assertEqual('12345',actor['instagram_user_id'])
        self.assertNotIn('do-not-save',json.dumps(actor))
        context.cookies.return_value=[]
        self.assertEqual('',asyncio.run(capture_executor(worker,self.db,self.owner,'window'))['username'])

    def test_report_http_endpoint_authenticates_and_ignores_injected_owner(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        from app.config import Settings
        app=create_app(Settings('fixture-startup-token-for-tests-only',self.root/'test.db',self.root),database=self.db,bitbrowser=self.hub)
        client=TestClient(app)
        self.add_post('own.actor','2026-09-12T18:00:00+00:00')
        self.add_post('foreign.actor','2026-09-12T18:00:00+00:00',owner=self.other)
        body={'kind':'activity','owner_user_id':self.other,'start':'2026-09-12T00:00:00Z','end':'2026-09-13T00:00:00Z'}
        headers={'X-Startup-Token':'fixture-startup-token-for-tests-only'}
        self.assertEqual(401,client.post('/api/reports/query',json=body,headers=headers).status_code)
        headers['Authorization']='Bearer '+self.service.login('report-owner','correct horse battery staple')['token']
        response=client.post('/api/reports/query',json=body,headers=headers)
        self.assertEqual(200,response.status_code);self.assertEqual(1,response.json()['totals']['posting'])
        self.assertEqual('own.actor',response.json()['rows'][0]['username'])
        self.assertGreaterEqual(client.post('/api/reports/query',json={**body,'kind':'invalid'},headers=headers).status_code,400)
        client.close()

    def test_incomplete_second_read_reaches_positive_observation_commit(self):
        manager=FollowMonitorManager(self.service,self.hub)
        diagnostic=SimpleNamespace(fingerprint=lambda s:s,start=lambda _:None,stop=AsyncMock(),report=lambda:{})
        manager._read_following_round=AsyncMock(return_value=({'only.one'},{'source_total':100,'first_read_count':1,'second_read_count':1,'second_homepage_count':100}))
        with patch('app.follow_monitor.RelationshipDiagnostics',return_value=diagnostic),patch.object(manager.diagnostics,'save'),patch.object(manager,'_commit_following_scan') as commit:
            asyncio.run(manager._scan_following_stage(SimpleNamespace(page=None),'owner','run','window','123','account'))
            commit.assert_called_once()
            self.assertEqual({'only.one'}, commit.call_args.args[-1])
            self.assertEqual(100, commit.call_args.kwargs['source_total'])


if __name__=='__main__':unittest.main()
