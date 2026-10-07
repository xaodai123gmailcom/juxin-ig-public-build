from __future__ import annotations
import asyncio,base64,copy,hashlib,io,json,sys,tempfile,time,unittest,uuid,zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService,isoformat
from app.account_workspace import AccountWorkspace
from app.native_browser import NativeBrowser,BrowserHub,validate_proxy
from app.cloud_workspace import CloudWorkspace,export_workspace,import_workspace,decode_workspace,SupabaseAPI
from app.errors import ConflictError,NotFoundError,ValidationError

class Legacy:
    def status(self):return {'connected':False}
    def list_all_windows(self):raise RuntimeError('BitBrowser is not installed')

class MemoryCloud:
    def __init__(self):self.remote=None;self.calls=[];self.user=str(uuid.uuid4())
    def request(self,path,*,method='GET',token=None,body=None):
        self.calls.append((path,method,token))
        if path.startswith('/auth/v1/token'):return {'access_token':'user-jwt','refresh_token':'private-refresh','expires_in':3600,'user':{'id':self.user,'email':'owner@example.test'}}
        if path.startswith('/rest/v1/juxin_workspaces'):
            assert token=='user-jwt';return [copy.deepcopy(self.remote)] if self.remote else []
        if path.endswith('/juxin_save_workspace'):
            assert token=='user-jwt'
            if body['expected_revision']!=(self.remote['revision'] if self.remote else 0):raise ConflictError('revision conflict')
            self.remote={'revision':body['expected_revision']+1,'payload':copy.deepcopy(body['new_payload'])};return self.remote['revision']
        return {}

class NativeCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=Database(self.root/'a.sqlite');self.db.initialize();self.s=CoreService(self.db)
        self.owner=self.s.register_user('native-owner','correct horse battery staple')['id'];self.other=self.s.register_user('native-other','correct horse battery staple')['id']
        self.native=NativeBrowser(self.db,self.root);self.hub=BrowserHub(self.native,Legacy());self.accounts=AccountWorkspace(self.s,self.hub)
    def tearDown(self):self.native.shutdown();self.tmp.cleanup()
    def plan(self,name='独立一',owner=None):return self.accounts.save(owner or self.owner,{'name':name,'native':True})['id']
    def new_destination(self):
        db=Database(self.root/'destination.sqlite');db.initialize();s=CoreService(db);owner=s.register_user('destination-user','correct horse battery staple')['id'];return db,s,owner
    def payload(self):return export_workspace(self.db,self.owner,self.root)[0]
    def rewrite(self,payload,edit):
        data=decode_workspace(payload);edit(data);raw=json.dumps(data).encode();return {'format':1,'encoding':'zlib+base64','sha256':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(zlib.compress(raw)).decode()}
    def test_native_creation_works_without_legacy_and_every_window_has_own_directory(self):
        a,b=self.plan(),self.plan('独立二');rows=self.accounts.snapshot(self.owner)['plans']
        self.assertEqual(2,len(self.hub.list_all_windows()['windows']));self.assertTrue(all(r['native'] for r in rows))
        self.assertNotEqual(self.native.directory(rows[0]['profile_id']),self.native.directory(rows[1]['profile_id']))
        self.assertNotIn(self.other,json.dumps(rows));self.assertFalse(any(self.root.glob('browser-profiles/**/*')))
    def test_foreign_user_cannot_operate_native_profile_through_any_task_lease(self):
        ident=self.accounts.get(self.owner,self.plan())['profile_id']
        for operation in ('collection','action','monitor','studio','account'):
            with self.subTest(operation=operation),self.assertRaises(NotFoundError):self.s.acquire_browser_lease(self.other,ident,operation_type=operation,entity_id='foreign')
        with self.assertRaises(NotFoundError):self.accounts.control_profile(self.other,ident,'open')
    def test_locked_native_plan_cannot_be_reconfigured(self):
        ident=self.plan();row=self.accounts.get(self.owner,ident);token=self.s.acquire_browser_lease(self.owner,row['profile_id'],operation_type='studio',entity_id='job')
        with self.assertRaises(ConflictError):self.accounts.save(self.owner,{'id':ident,'revision':1,'name':'new','native':True})
        self.s.release_browser_lease(row['profile_id'],token)
        self.assertEqual('独立一',self.native.get(row['profile_id'])['name'])
    def test_invalid_proxy_never_reaches_browser_command_line(self):
        for value in ['file:///tmp','--remote-debugging-port=111','http://user:password@host:80','http://host:bad','http://host:80/path']:
            with self.subTest(value=value),self.assertRaises(ValidationError):validate_proxy(value)
        self.assertEqual('socks5://127.0.0.1:9000',validate_proxy('socks5://127.0.0.1:9000'))
    def test_stale_plan_save_rolls_back_native_configuration(self):
        ident=self.plan();self.accounts.save(self.owner,{'id':ident,'revision':1,'name':'updated','native':True})
        with self.assertRaises(ConflictError):self.accounts.save(self.owner,{'id':ident,'revision':1,'name':'stale','native':True})
        row=self.accounts.get(self.owner,ident);self.assertEqual('updated',self.native.get(row['profile_id'])['name'])
    def test_generation_and_action_tickets_prevent_cross_window_or_late_actions(self):
        ident=self.accounts.get(self.owner,self.plan())['profile_id'];p=SimpleNamespace(poll=lambda:None)
        self.native.processes[ident]={'process':p,'generation':1,'ws':'ws://127.0.0.1:1234/devtools/browser/one'};self.native.generations[ident]=1
        try:
            with self.assertRaises(ConflictError):self.native.verify_connection_endpoint(ident,'ws://127.0.0.1:1234/devtools/browser/two',1)
            token=self.native.begin_action_attempt(ident,self.native.processes[ident]['ws'],1);self.native.cancel_action_attempt(token)
            with self.assertRaises(ConflictError):self.native.resolve_action_attempt(token)
            token=self.native.begin_action_attempt(ident,self.native.processes[ident]['ws'],1);self.native.resolve_action_attempt(token);lease=self.native.adopt_action_attempt(token)
            with self.assertRaises(ConflictError):self.native.close_profile(ident)
            self.native.end_action(lease);self.native.generations[ident]=2
            with self.assertRaises(ConflictError):self.native.verify_connection_endpoint(ident,self.native.processes[ident]['ws'],1)
        finally:self.native.processes.clear()
    def test_cancelled_connection_cannot_reopen_a_profile(self):
        ident=self.accounts.get(self.owner,self.plan())['profile_id'];token=self.native.begin_connection_attempt(ident);self.native.cancel_connection_attempt(token)
        with patch('app.native_browser._spawn_browser_process') as launch:
            with self.assertRaises(ConflictError):self.native.connection_endpoint(ident,open_if_needed=True,attempt_id=token)
            launch.assert_not_called()
    def test_close_fence_blocks_new_connection_before_slow_shutdown_finishes(self):
        import threading
        ident=self.accounts.get(self.owner,self.plan())['profile_id'];entered=threading.Event();finish=threading.Event()
        self.native.processes[ident]={'process':SimpleNamespace(poll=lambda:None),'generation':1,'ws':'ws://127.0.0.1:1234/devtools/browser/one'};self.native.generations[ident]=1
        def stop(_state):entered.set();finish.wait(3)
        with patch.object(self.native,'_stop_process',side_effect=stop):
            thread=threading.Thread(target=lambda:self.native.close_profile(ident));thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ConflictError):self.native.begin_connection_attempt(ident)
                with self.assertRaises(ConflictError):self.native.close_profile(ident)
            finally:finish.set();thread.join(3)
        self.assertNotIn(ident,self.native.closing);self.assertNotIn(ident,self.native.processes)
    def test_cloud_export_excludes_other_users_passwords_sessions_and_leases(self):
        self.plan('own');self.plan('secret-other',self.other);self.s.login('native-owner','correct horse battery staple')
        data=decode_workspace(self.payload());text=json.dumps(data)
        self.assertNotIn('secret-other',text);self.assertNotIn(self.other,text)
        for key in ('auth_sessions','app_users','password_hash','browser_operation_leases','cloud_workspace_links'):self.assertNotIn(key,data['tables'])
    def test_cloud_restore_remaps_owner_preserves_history_and_stays_idle(self):
        ident=self.plan();profile=self.accounts.get(self.owner,ident)['profile_id'];now=isoformat()
        with self.db.write() as c:
            c.execute('INSERT INTO follow_monitor_runs(id,owner_user_id,profile_ids_json,status,started_at) VALUES(?,?,?,?,?)',('round',self.owner,json.dumps([profile]),'completed',now))
            c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('job',self.owner,'request','nurture',profile,'queued','{}',now,now,now))
        dest,_,owner=self.new_destination();import_workspace(dest,owner,self.root/'destination',self.payload())
        with dest.read() as c:
            self.assertEqual(owner,c.execute('SELECT owner_user_id FROM native_browser_profiles').fetchone()[0]);self.assertEqual('paused',c.execute('SELECT status FROM studio_jobs').fetchone()[0])
            self.assertEqual('round',c.execute('SELECT id FROM follow_monitor_runs').fetchone()[0]);self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
    def test_old_backup_restores_split_deduplication_after_destination_migration(self):
        self.s.upsert_manual_split_candidates(self.owner,[{'username':'split.backup','queued':True}])
        payload=self.rewrite(self.payload(),lambda d:d['tables'].pop('account_creation_batches'))
        dest,s,owner=self.new_destination()
        import_workspace(dest,owner,self.root/'destination',payload)
        self.assertTrue(s.check_global_dedupe('SPLIT.BACKUP')['seen'])
        claim=s.claim_workbench_identity(owner,username='split.backup',source='followers')
        self.assertTrue(claim['duplicate']);self.assertFalse(claim['should_collect_profile'])

    def test_existing_local_data_is_not_overwritten(self):
        self.plan();before=self.payload()
        with self.assertRaises(ConflictError):import_workspace(self.db,self.owner,self.root,before)
        self.assertEqual(before,self.payload())
    def test_corrupt_or_cross_owner_backup_rolls_back(self):
        self.plan();dest,_,owner=self.new_destination();bad=self.rewrite(self.payload(),lambda d:d['tables']['account_window_plans'][0].update(owner_user_id=self.other))
        with self.assertRaises(ValidationError):import_workspace(dest,owner,self.root/'destination',bad)
        with dest.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM account_window_plans').fetchone()[0])
        bad=dict(self.payload(),sha256='wrong')
        with self.assertRaises(ValidationError):decode_workspace(bad)
    def test_media_backup_cannot_read_files_outside_app_directory(self):
        self.plan();outside=Path(self.tmp.name).parent/('not-managed-'+uuid.uuid4().hex)
        try:
            outside.write_text('external secret')
            with self.db.write() as c:c.execute('INSERT INTO studio_assets VALUES(?,?,?,?,?,?,?,?,?,?)',('asset',self.owner,'manual','x',str(outside),'photo','','','',isoformat()))
            self.assertEqual({},decode_workspace(self.payload())['assets'])
        finally:outside.unlink()
    def test_collection_graph_restores_with_valid_relationships(self):
        task=self.s.create_task(self.owner,name='收集',modes=['following'],targets=['source_account'],settings={})
        self.s.record_result(self.owner,task['id'],task['targets'][0]['id'],username='person',instagram_user_id='123456',source_mode='following',visibility='public',profile={'followers_count':25},screening={},qualified=True)
        dest,service,owner=self.new_destination();import_workspace(dest,owner,self.root/'destination',self.payload())
        with dest.read() as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM task_results').fetchone()[0])
            self.assertEqual({'source_account','person'},{r[0] for r in c.execute('SELECT current_username_norm FROM instagram_accounts JOIN global_seen ON global_seen.account_id=instagram_accounts.id')})
            self.assertEqual(2,c.execute('SELECT total_count FROM global_seen_stats').fetchone()[0]);self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(0,c.execute('SELECT count(*) FROM studio_assets').fetchone()[0])
    def test_lost_upload_ack_is_reconciled_without_new_upload(self):
        self.plan();api=MemoryCloud();cloud=CloudWorkspace(self.s,self.root,api);cloud.command(self.owner,{'action':'login','email':'owner@example.test','password':'local-test-password'});cloud.sync(self.owner)
        with self.db.write() as c:c.execute("UPDATE cloud_workspace_links SET revision=0,digest='' WHERE owner_user_id=?",(self.owner,))
        cloud.sync(self.owner);self.assertEqual(1,api.remote['revision']);self.assertEqual(1,cloud.status(self.owner)['revision'])
    def test_cloud_cas_and_owner_session_are_used_and_noop_sync_is_not_reuploaded(self):
        self.plan();api=MemoryCloud();cloud=CloudWorkspace(self.s,self.root,api)
        cloud.command(self.owner,{'action':'login','email':'owner@example.test','password':'local-test-password'});cloud.sync(self.owner)
        self.assertEqual(1,api.remote['revision']);cloud.sync(self.owner);self.assertEqual(1,api.remote['revision'])
        api.remote['revision']=2;api.remote['payload']['sha256']='remote-change'
        with self.assertRaises(ConflictError):cloud.sync(self.owner)
        self.assertEqual(2,api.remote['revision']);self.assertNotIn('user-jwt',json.dumps(cloud.status(self.owner)))
    def test_active_task_allows_consistent_cloud_snapshot(self):
        ident=self.accounts.get(self.owner,self.plan())['profile_id'];api=MemoryCloud();cloud=CloudWorkspace(self.s,self.root,api)
        cloud.command(self.owner,{'action':'login','email':'owner@example.test','password':'local-test-password'})
        token=self.s.acquire_browser_lease(self.owner,ident,operation_type='monitor',entity_id='round');cloud.sync(self.owner)
        self.assertEqual(1,api.remote['revision']);self.s.release_browser_lease(ident,token);cloud.sync(self.owner);self.assertEqual(1,api.remote['revision'])
    def test_new_computer_downloads_and_does_not_overwrite_remote_on_first_login(self):
        self.plan();api=MemoryCloud();first=CloudWorkspace(self.s,self.root,api);first.command(self.owner,{'action':'login','email':'owner@example.test','password':'local-test-password'});first.sync(self.owner)
        dest,s,owner=self.new_destination();second=CloudWorkspace(s,self.root/'destination',api);second.command(owner,{'action':'login','email':'owner@example.test','password':'local-test-password'});second.sync(owner)
        self.assertEqual(1,api.remote['revision']);self.assertEqual(1,second.status(owner)['revision'])
        with dest.read() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM native_browser_profiles').fetchone()[0])

if __name__=='__main__':unittest.main()
