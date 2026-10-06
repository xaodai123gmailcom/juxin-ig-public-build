import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService,isoformat
from app.account_workspace import AccountWorkspace
from app.account_platforms import PLATFORMS,parse_cookies,custom_url
from app.account_unread import account_unread_snapshot
from app.errors import ConflictError,ValidationError

class Provider:
    def list_all_windows(self):return {'windows':[{'id':'w1'},{'id':'w2'}],'stale':False}
    def close_profile(self,profile):return {'closed':True}

class AccountUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'db');self.db.initialize();self.service=CoreService(self.db)
        self.owner=self.service.register_user('updates-owner','correct horse battery staple')['id']
        self.other=self.service.register_user('updates-other','correct horse battery staple')['id']
        self.calls=[];self.fail_open=False
        def opener(provider,profile,platform,cookies):
            self.calls.append((profile,platform,cookies))
            if self.fail_open:raise RuntimeError('private fixture secret')
            return {'opened':True,'login_verified':False,'cookies_imported':len(cookies or [])}
        self.m=AccountWorkspace(self.service,Provider(),opener=opener)
    def tearDown(self):self.tmp.cleanup()
    def plan(self,profile='',owner=None,platform='instagram'):
        return self.m.save(owner or self.owner,{'name':'test','profile_id':profile,'platform':platform})['id']
    def test_cookie_create_validation_and_partial_failure_do_not_duplicate(self):
        payload={'name':'Cookie fixture','profile_id':'w1','platform':'instagram'}
        with self.assertRaises(ValidationError):self.m.command(self.owner,{'action':'save_with_cookies','plan':payload,'cookie_text':'invalid'})
        self.assertEqual([],self.m.snapshot(self.owner)['plans'])
        self.fail_open=True
        result=self.m.command(self.owner,{'action':'save_with_cookies','plan':payload,'cookie_text':'sessionid=private-fixture'})
        self.assertTrue(result['saved']);self.assertTrue(result['cookie_failed']);self.assertNotIn('private',json.dumps(result))
        self.fail_open=False
        result=self.m.command(self.owner,{'action':'import_cookies','id':result['id'],'revision':result['revision'],'cookie_text':'sessionid=private-fixture'})
        self.assertFalse(result['login_verified']);self.assertEqual(1,len(self.m.snapshot(self.owner)['plans']))
        self.assertNotIn('private-fixture',json.dumps(self.m.snapshot(self.owner)))
    def test_cookie_save_and_task_lock(self):
        ident=self.plan('w1');token=self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='fixture')
        try:
            with self.assertRaises(ConflictError):self.m.command(self.owner,{'action':'save_with_cookies','plan':{'id':ident,'revision':1,'name':'rename','profile_id':'w1'},'cookie_text':'sessionid=fixture'})
            self.assertEqual([],self.calls)
        finally:self.service.release_browser_lease('w1',token)
    def test_order_persists_and_does_not_change_task_binding_or_revisions(self):
        a=self.plan('w1');b=self.plan('w2');other=self.plan(owner=self.other)
        token=self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='fixture')
        self.m.command(self.owner,{'action':'reorder','ids':[b,a]})
        with self.assertRaisesRegex(RuntimeError, 'browser work is active'):
            self.db.initialize()
        rows=self.m.snapshot(self.owner)['plans'];self.assertEqual([b,a],[r['id'] for r in rows]);self.assertEqual([1,1],[r['revision'] for r in rows])
        with self.db.read() as c:self.assertEqual(1,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        for ids in ([a],[a,a],[a,other]):
            with self.assertRaises((ConflictError,ValidationError)):self.m.command(self.owner,{'action':'reorder','ids':ids})
        self.service.release_browser_lease('w1',token)
        self.db.initialize()
        self.assertEqual([b,a],[row['id'] for row in self.m.snapshot(self.owner)['plans']])
        self.m.command(self.owner,{'action':'delete','id':b,'revision':1})
        self.assertEqual([a],[r['id'] for r in self.m.snapshot(self.owner)['plans']])
    def test_unread_owner_active_plan_filter_and_no_lease_or_navigation(self):
        a=self.plan('w1');self.plan('w2',owner=self.other)
        self.m.db.write
        class Reader:
            def unread_snapshot(inner,owner):
                self.assertEqual(owner,self.owner)
                return {'windows':{'w1':{'count':12,'capped':False,'status':'live','observed_at':isoformat()},'w2':{'count':99,'status':'live','observed_at':isoformat()}}}
        first=account_unread_snapshot(self.db,Reader(),self.owner);self.assertEqual(12,first['total']);self.assertEqual({'w1'},set(first['windows']))
        self.m.command(self.owner,{'action':'delete','id':a,'revision':1})
        self.assertEqual(0,account_unread_snapshot(self.db,Reader(),self.owner)['total']);self.assertEqual([],self.calls)
    def test_message_total_sums_owned_windows_once_and_decreases_when_read(self):
        self.plan('w1',platform='whatsapp')
        with self.assertRaises(ConflictError):self.plan('w1',platform='whatsapp')
        self.plan('w2',platform='whatsapp')
        counts={'w1':3,'w2':123,'foreign-window':999}
        class Reader:
            def unread_snapshot(inner,owner):
                return {'windows':{ident:{'count':count,'capped':False,'status':'live','observed_at':isoformat()} for ident,count in counts.items()}}
        result=account_unread_snapshot(self.db,Reader(),self.owner)
        self.assertEqual(126,result['total']);self.assertEqual(3,result['windows']['w1']['count'])
        counts['w1']=2;self.assertEqual(125,account_unread_snapshot(self.db,Reader(),self.owner)['total'])
        counts['w1']=0;counts['w2']=0;self.assertEqual(0,account_unread_snapshot(self.db,Reader(),self.owner)['total'])
        self.assertEqual([],self.calls)

    def test_custom_domain_cookie_and_non_instagram_task_rejection(self):
        ident=self.m.save(self.owner,{'name':'custom','platform':'custom','custom_url':'https://example.com/app','profile_id':'w1'})['id']
        self.m.command(self.owner,{'action':'open','id':ident})
        self.assertEqual({'id':'custom','url':'https://example.com/app'},self.calls[0][1])
        with self.assertRaises(ValidationError):self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='job')
        dest=self.calls[0][1]
        self.assertEqual('example.com',parse_cookies('s=fixture',dest)[0]['domain'].lstrip('.'))
        for host in ('evil.example.com','other.com'):
            with self.assertRaises(ValidationError):parse_cookies(json.dumps([{'name':'s','value':'fixture','domain':host}]),dest)
        for url in ('http://example.com','https://127.0.0.1','https://user:pass@example.com','https://example.com:9999','https://host.local'):
            with self.assertRaises(ValidationError):custom_url(url)
    def test_catalog_not_placeholder_or_unavailable_native_app(self):
        catalog=self.m.snapshot(self.owner)['platforms']
        self.assertGreaterEqual(len(catalog),15)
        self.assertIn('telegram_k',PLATFORMS);self.assertNotIn('messenger',PLATFORMS);self.assertNotIn('facebook',PLATFORMS)
        for key in ('signal','line'):
            with self.assertRaises(ValidationError):self.plan(platform=key)
    def test_creation_and_last_open_time_are_persistent_and_not_rewritten_by_notes(self):
        ident=self.plan('w1');before=self.m.snapshot(self.owner)['plans'][0]
        self.assertTrue(before['created_at']);self.assertIsNone(before['last_opened_at'])
        self.m.command(self.owner,{'id':ident,'action':'open'})
        opened=self.m.snapshot(self.owner)['plans'][0]['last_opened_at'];self.assertTrue(opened)
        self.m.command(self.owner,{'id':ident,'action':'notes','revision':1,'notes':'new note'})
        self.db.initialize();after=self.m.snapshot(self.owner)['plans'][0]
        self.assertEqual(before['created_at'],after['created_at']);self.assertEqual(opened,after['last_opened_at'])
    def test_unread_bad_or_failed_source_stays_unknown(self):
        self.plan('w1')
        class Reader:
            def unread_snapshot(self,owner):return {'windows':{'w1':{'count':True,'status':'live','observed_at':'bad'}}}
        result=account_unread_snapshot(self.db,Reader(),self.owner);self.assertEqual(0,result['total']);self.assertIsNone(result['windows']['w1']['count'])
        result=account_unread_snapshot(self.db,None,self.owner);self.assertEqual(1,result['unknown'])

if __name__=='__main__':unittest.main()
