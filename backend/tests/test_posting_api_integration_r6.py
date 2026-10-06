"""Real application routes enforce owner isolation without external actions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import isoformat
from test_core import FakeBitBrowserClient

class PostingApiIntegrationR6Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        db=Database(root/'db.sqlite3');db.initialize()
        settings=Settings(startup_token='offline-posting-api-startup-token-12345',database_path=db.path,data_dir=root)
        self.app=create_app(settings,database=db,bitbrowser=FakeBitBrowserClient())
        self.app.state.posting.recover()
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.db=db;self.users={};self.headers={}
        for name in ('api-owner','api-other'):
            user=self.app.state.service.register_user(name,'offline fixture password')
            self.users[name]=user['id'];token=self.app.state.service.login(name,'offline fixture password')['token']
            self.headers[name]={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}
        with db.write() as c:
            for name in self.users:
                c.execute('INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(name,self.users[name],name,'mountain',name+' caption',isoformat(),isoformat()))

    def command(self,body,owner='api-owner'):
        return self.client.post('/api/posting/command',headers=self.headers[owner],json=body)

    def test_both_routes_require_startup_and_session_auth(self):
        for headers in ({},{'X-Startup-Token':self.headers['api-owner']['X-Startup-Token']},{'Authorization':self.headers['api-owner']['Authorization']}):
            self.assertEqual(401,self.client.get('/api/posting/snapshot',headers=headers).status_code)
            self.assertEqual(401,self.client.post('/api/posting/command',headers=headers,json={'action':'cancel','job_id':'api-owner'}).status_code)

    def test_snapshot_and_mutations_are_owner_scoped(self):
        for owner in self.users:
            r=self.client.get('/api/posting/snapshot?timezone=Asia%2FShanghai',headers=self.headers[owner]);self.assertEqual(200,r.status_code,r.text)
            self.assertEqual([owner],[j['id'] for j in r.json()['jobs']]);self.assertFalse(r.json()['credentials']['pexels_configured'])
        self.assertEqual(404,self.command({'action':'edit','job_id':'api-other','caption':'foreign change'}).status_code)
        self.assertEqual(200,self.command({'action':'edit','job_id':'api-owner','caption':'owned change'}).status_code)
        with self.db.read() as c:
            self.assertEqual('api-other caption',c.execute("SELECT caption FROM posting_jobs WHERE id='api-other'").fetchone()[0])
            self.assertEqual('owned change',c.execute("SELECT caption FROM posting_jobs WHERE id='api-owner'").fetchone()[0])

    def test_unconfigured_generate_does_not_create_jobs_or_touch_network(self):
        provider=self.app.state.posting.provider
        provider.search=Mock(side_effect=AssertionError('network forbidden'))
        before=self.client.get('/api/posting/snapshot',headers=self.headers['api-owner']).json()['totals']
        r=self.command({'action':'generate','request_id':'offline-request-001','theme':'mountain','caption':'fixture','count':1})
        self.assertGreaterEqual(r.status_code,400,r.text);provider.search.assert_not_called()
        after=self.client.get('/api/posting/snapshot',headers=self.headers['api-owner']).json()['totals'];self.assertEqual(before,after)

    def test_retry_endpoint_requires_owned_failure_and_does_not_bypass_preparation(self):
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed',failure_stage='preparation'")
        self.assertEqual(404,self.command({'action':'retry','job_id':'api-other'}).status_code)
        response=self.command({'action':'retry','job_id':'api-owner'})
        self.assertEqual(422,response.status_code);self.assertIn('安全配置',response.text)
        with self.db.read() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM posting_retry_history').fetchone()[0])
            self.assertEqual('failed',c.execute("SELECT status FROM posting_jobs WHERE id='api-owner'").fetchone()[0])
        row=self.client.get('/api/posting/snapshot',headers=self.headers['api-owner']).json()['jobs'][0]
        self.assertEqual('prepare',row['retry_action']);self.assertFalse(row['can_retry_cleanup'])
        self.assertNotIn('attempt_id',row);self.assertNotIn('lease_token',row)

    def test_legacy_posting_route_and_wrong_platform_cannot_bypass_new_queue(self):
        r=self.command({'action':'edit','job_id':'api-owner','caption':'changed','platform':'facebook'})
        self.assertGreaterEqual(r.status_code,400)
        r=self.client.post('/api/studio/command',headers=self.headers['api-owner'],json={'action':'start','kind':'posting','window_ids':['fixture']})
        self.assertGreaterEqual(r.status_code,400,r.text)
        self.assertFalse(self.app.state.studio.posting_enabled)

    def test_cursor_pagination_exposes_all_owned_rows_and_rejects_bad_bounds(self):
        with self.db.write() as c:
            for i in range(61):
                c.execute('INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(f'page-{i:03}',self.users['api-owner'],f'page-{i}','theme','caption',isoformat(),isoformat()))
        seen=[];cursor=None
        for _ in range(10):
            params={'timezone':'UTC','limit':20}
            if cursor:params['cursor']=cursor
            r=self.client.get('/api/posting/snapshot',headers=self.headers['api-owner'],params=params)
            self.assertEqual(200,r.status_code,r.text);body=r.json();seen.extend(j['id'] for j in body['jobs'])
            if not body['pagination']['has_more']:break
            cursor=body['pagination']['next_cursor'];self.assertTrue(cursor)
        self.assertEqual(62,len(seen));self.assertEqual(62,len(set(seen)));self.assertNotIn('api-other',seen)
        for params in ({'limit':0},{'limit':201},{'limit':'oops'},{'cursor':'not-json'},{'cursor':'x'*257}):
            self.assertGreaterEqual(self.client.get('/api/posting/snapshot',headers=self.headers['api-owner'],params=params).status_code,400)
