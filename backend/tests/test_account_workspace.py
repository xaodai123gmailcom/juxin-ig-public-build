from __future__ import annotations
import asyncio,json,sys,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService,isoformat
from app.account_workspace import AccountWorkspace
from app.errors import ConflictError,NotFoundError,ValidationError

class Browser:
    def __init__(self):self.calls=[];self.stale=False;self.hook=None
    def list_all_windows(self):return {'windows':[{'id':'w1','name':'窗口一'},{'id':'w2','name':'窗口二'}],'stale':self.stale}
    def open_profile(self,p):
        if self.hook:self.hook()
        self.calls.append(('open',p));return {'opened':True}
    def close_profile(self,p):
        if self.hook:self.hook()
        self.calls.append(('close',p));return {'closed':True}

class AccountWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'test.sqlite');self.db.initialize()
        self.service=CoreService(self.db);self.owner=self.service.register_user('account-owner','correct horse battery staple')['id'];self.other=self.service.register_user('account-other','correct horse battery staple')['id']
        self.browser=Browser();self.m=AccountWorkspace(self.service,self.browser,opener=lambda provider,profile,platform,cookies:provider.open_profile(profile))
    def tearDown(self):self.tmp.cleanup()
    def create(self,profile='',owner=None,name='方案一'):
        return self.m.save(owner or self.owner,{'name':name,'username':'example','profile_id':profile})['id']
    def payload(self,ident):
        r=self.m.get(self.owner,ident)
        return {'id':ident,'revision':r['revision'],'name':r['name'],'profile_id':r['profile_id']}
    def test_schema_and_preview_are_honest_and_idempotent(self):
        self.db.initialize();ident=self.create()
        with self.db.read() as c:self.assertEqual(list(range(1,42)),[r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')])
        snapshot=self.m.snapshot(self.owner);self.assertEqual('not_connected',snapshot['cloud_sync']);self.assertEqual('native_browser',snapshot['mode']);self.assertEqual([],self.browser.calls)
        with self.assertRaises(ValidationError):self.m.command(self.owner,{'action':'open','id':ident})
    def test_edit_bind_and_open_use_existing_window(self):
        ident=self.create();payload=self.payload(ident);payload.update(name='账号 A',profile_id='w1');self.m.save(self.owner,payload)
        self.m.command(self.owner,{'action':'open','id':ident});self.m.command(self.owner,{'action':'close','id':ident})
        self.assertEqual([('open','w1'),('close','w1')],self.browser.calls);self.assertEqual(4,len(self.m.snapshot(self.owner)['events']))
    def test_owner_isolation_and_one_window_one_account(self):
        ident=self.create('w1')
        self.assertEqual([],self.m.snapshot(self.other)['plans']);self.assertEqual([],self.m.snapshot(self.other)['events'])
        with self.assertRaises(NotFoundError):self.m.command(self.other,{'action':'open','id':ident})
        with self.assertRaises(ConflictError):self.create('w1',owner=self.other)
        self.assertEqual([],self.browser.calls)
    def test_stale_edit_and_archive_cannot_overwrite_newer_binding(self):
        ident=self.create('w1');stale=self.payload(ident);changed=dict(stale);changed['profile_id']='w2';self.m.save(self.owner,changed)
        with self.assertRaises(ConflictError):self.m.save(self.owner,stale)
        with self.assertRaises(ConflictError):self.m.command(self.owner,{'action':'archive','id':ident,'revision':1})
        self.assertEqual('w2',self.m.get(self.owner,ident)['profile_id'])
    def test_archive_preserves_history_and_does_not_delete_browser(self):
        ident=self.create('w1');self.m.command(self.owner,{'action':'archive','id':ident,'revision':1});self.assertEqual([],self.m.snapshot(self.owner)['plans']);self.assertEqual(2,len(self.m.snapshot(self.owner)['events']));self.assertEqual([],self.browser.calls)
        self.create('w1');self.assertEqual(2,self.m.snapshot(self.owner)['plans'][0]['serial'])
    def test_missing_or_stale_inventory_cannot_be_bound(self):
        with self.assertRaises(ValidationError):self.create('unknown')
        self.browser.stale=True
        with self.assertRaises(ConflictError):self.create('w1')
    def test_locked_window_blocks_every_management_operation_even_after_five_seconds(self):
        ident=self.create('w1');lease=self.service.acquire_browser_lease(self.other,'w1',operation_type='monitor',entity_id='live-run',ttl_seconds=600)
        with self.db.write() as c:c.execute('UPDATE browser_operation_leases SET heartbeat_at=? WHERE profile_id=?',(isoformat(datetime.now(timezone.utc)-timedelta(seconds=30)),'w1'))
        for action in ('open','close','archive','delete','refresh','back','forward'):
            with self.subTest(action=action),self.assertRaises(ConflictError):self.m.command(self.owner,{'action':action,'id':ident,'revision':1})
        with self.assertRaises(ConflictError):self.m.save(self.owner,self.payload(ident))
        changed=self.payload(ident);changed['profile_id']='w2'
        with self.assertRaises(ConflictError):self.m.save(self.owner,changed)
        self.assertEqual([],self.browser.calls);self.assertEqual('w1',self.m.get(self.owner,ident)['profile_id'])
        self.service.release_browser_lease('w1',lease)
    def test_delete_closes_window_removes_plan_and_preserves_history(self):
        ident=self.create('w1')
        with self.assertRaises(NotFoundError):self.m.command(self.other,{'action':'delete','id':ident,'revision':1})
        with self.assertRaises(ConflictError):self.m.command(self.owner,{'action':'delete','id':ident,'revision':2})
        self.assertEqual([],self.browser.calls)
        self.m.command(self.owner,{'action':'delete','id':ident,'revision':1})
        self.assertEqual([('close','w1')],self.browser.calls)
        self.assertEqual([],self.m.snapshot(self.owner)['plans'])
        self.assertEqual(2,len(self.m.snapshot(self.owner)['events']))
        self.assertEqual({},self.m.snapshot(self.owner)['locks'])
    def test_failed_delete_retains_plan_and_releases_only_its_lease(self):
        ident=self.create('w1')
        def fail():raise RuntimeError('close failed')
        self.browser.hook=fail
        with self.assertRaises(RuntimeError):self.m.command(self.owner,{'action':'delete','id':ident,'revision':1})
        self.assertEqual(ident,self.m.get(self.owner,ident)['id'])
        self.assertEqual({},self.m.snapshot(self.owner)['locks'])
    def test_atomic_control_holds_lock_against_scheduler_and_refresh(self):
        def hook():
            self.service.list_browser_lease_states(self.owner,active_collection_entity_ids=set(),active_action_entity_ids=set(),active_monitor_entity_ids=set(),active_studio_entity_ids=set(),inactive_grace_seconds=0)
            with self.assertRaises(ConflictError):self.service.acquire_browser_lease(self.other,'w1',operation_type='studio',entity_id='new-task')
        self.browser.hook=hook;self.m.control_profile(self.owner,'w1','open')
        token=self.service.acquire_browser_lease(self.other,'w1',operation_type='studio',entity_id='new-task');self.service.release_browser_lease('w1',token)
    def test_provider_failure_releases_only_management_lock(self):
        def fail():raise RuntimeError('offline')
        self.browser.hook=fail
        with self.assertRaises(RuntimeError):self.m.control_profile(self.owner,'w1','close')
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
    def test_recover_only_clears_its_own_management_locks(self):
        self.service.acquire_browser_lease(self.owner,'w1',operation_type='account',entity_id='abandoned')
        self.service.acquire_browser_lease(self.owner,'w2',operation_type='studio',entity_id='other-module')
        self.m.recover()
        with self.db.read() as c:self.assertEqual(['studio'],[r[0] for r in c.execute('SELECT operation_type FROM browser_operation_leases')])
    def test_authenticated_routes_and_old_manual_command_cannot_bypass_lock(self):
        from app.main import create_app
        from app.config import Settings
        settings=Settings(startup_token='account-test-startup-token-123456',database_path=self.db.path,data_dir=Path(self.tmp.name));app=create_app(settings,database=self.db,bitbrowser=self.browser)
        async def request(path,body=None,token=None):
            raw=json.dumps(body).encode() if body is not None else b''
            headers=[(b'x-startup-token',settings.startup_token.encode()),(b'content-type',b'application/json')]
            if token:headers.append((b'authorization',('Bearer '+token).encode()))
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','scheme':'http','method':'POST' if body is not None else 'GET','path':path,'raw_path':path.encode(),'query_string':b'','headers':headers,'client':('127.0.0.1',1),'server':('127.0.0.1',17831)}
            sent=[];delivered=False
            async def receive():
                nonlocal delivered
                if not delivered:delivered=True;return {'type':'http.request','body':raw,'more_body':False}
                await asyncio.Event().wait()
            async def send(message):sent.append(message)
            await app(scope,receive,send)
            return next(m['status'] for m in sent if m['type']=='http.response.start'),json.loads(b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body'))
        async def run():
            self.assertEqual(401,(await request('/api/accounts/snapshot'))[0]);self.create('w1')
            owner=self.service.login('account-owner','correct horse battery staple')['token'];other=self.service.login('account-other','correct horse battery staple')['token']
            self.assertEqual(1,len((await request('/api/accounts/snapshot',token=owner))[1]['plans']));self.assertEqual(0,len((await request('/api/accounts/snapshot',token=other))[1]['plans']))
            self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='posting-live')
            for path,body in [('/api/workbench/commands',{'command':'bitbrowser_close','payload':{'profile_id':'w1'}}),('/api/bitbrowser/w1/open',{}),('/v1/bitbrowser/windows/w1/close',{})]:
                self.assertEqual(409,(await request(path,body,owner))[0],path)
            self.assertEqual([],self.browser.calls)
        asyncio.run(run())

if __name__=='__main__':unittest.main()
