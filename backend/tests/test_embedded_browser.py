import sys,tempfile,unittest,threading,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.account_workspace import AccountWorkspace
from app.embedded_browser import EmbeddedBrowser
from app.native_browser import BrowserHub
from app.errors import ConflictError,UpstreamUnavailableError

class Bridge:
    def __init__(self):self.live={};self.calls=[];self.generation=0;self.hold=None;self.entered=None
    def call(self,method,**b):
        self.calls.append((method,b))
        if method=='hide':return {'hidden':True}
        if method=='inventory':return {'profiles':[{'id':k,**v} for k,v in self.live.items()]}
        key=b.get('profile')
        if method=='open-whatsapp':
            if key not in self.live:
                self.generation+=1;self.live[key]={'ws':'ws://127.0.0.1:1234/devtools/browser/'+str(self.generation),'generation':self.generation}
            return {**self.live[key],'page_loaded':True,'message':''}
        if method=='ensure':
            if key not in self.live:
                if not b['open']:raise ConflictError('closed')
                self.generation+=1;self.live[key]={'ws':'ws://127.0.0.1:1234/devtools/browser/'+str(self.generation),'generation':self.generation}
            if self.hold:self.entered.set();self.hold.wait(3)
            return dict(self.live[key])
        if method=='close':
            if self.live[key]['generation']!=b['generation']:raise ConflictError('stale')
            self.live.pop(key);return {'closed':True}
        if method=='verify':
            if self.live.get(key)!={'ws':b['ws'],'generation':b['generation']}:raise ConflictError('stale')
            return {'verified':True}
        if method=='manual-navigation':return {'page_loaded':True}
        if method=='reset-whatsapp-storage':return {'reset':True}
        if method=='chat-translation':return {'messages':[]}
        if method=='whatsapp-diagnostics':return {'checks':{'fixture':True}}
        raise AssertionError(method)

class EmbeddedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'a.sqlite');self.db.initialize();self.service=CoreService(self.db)
        self.owner=self.service.register_user('embedded-owner','correct horse battery staple')['id'];self.bridge=Bridge();self.native=EmbeddedBrowser(self.db,self.tmp.name,bridge=self.bridge)
        class Legacy:
            def status(self):return {'connected':False}
            def list_all_windows(self):return {'windows':[]}
            def resolve_cdp_endpoint(self,endpoint):raise AssertionError('embedded endpoint must never resolve through external browser')
        self.hub=BrowserHub(self.native,Legacy());self.accounts=AccountWorkspace(self.service,self.hub)
        plan=self.accounts.save(self.owner,{'name':'embedded','native':True});self.profile=self.accounts.get(self.owner,plan['id'])['profile_id']
    def tearDown(self):self.tmp.cleanup()
    def test_all_whatsapp_navigation_and_custom_checks_avoid_automation(self):
        for config in ({'platform':'whatsapp'},{'platform':'custom','custom_url':'https://web.whatsapp.com/'}):
            plan=self.accounts.save(self.owner,{'name':'WA rebuilt','native':True,**config})
            with patch('app.playwright_worker.PlaywrightWorker',side_effect=AssertionError('WhatsApp must not construct Playwright')):
                for action in ('home','inbox','refresh','back','forward'):
                    self.assertTrue(self.accounts.command(self.owner,{'id':plan['id'],'action':action})['page_loaded'])
                self.assertEqual({'checks':{'fixture':True}},self.accounts.command(self.owner,{'id':plan['id'],'action':'whatsapp_diagnostics'}))
                self.assertEqual({'messages':[]},self.accounts.command(self.owner,{'id':plan['id'],'action':'chat_translation','step':{'kind':'poll'}}))
        calls=[method for method,_ in self.bridge.calls]
        self.assertEqual(4,calls.count('open-whatsapp'));self.assertEqual(6,calls.count('manual-navigation'))
    def test_bridge_failure_never_launches_external_chrome(self):
        with patch.object(self.bridge,'call',side_effect=UpstreamUnavailableError('offline')),patch('app.native_browser._spawn_browser_process') as spawn:
            with self.assertRaises(UpstreamUnavailableError):self.native.open_profile(self.profile)
            spawn.assert_not_called()
    def test_actual_endpoint_routed_to_owned_session_and_generation_fenced(self):
        token=self.native.begin_connection_attempt(self.profile);p=self.native.connection_endpoint(self.profile,open_if_needed=True,attempt_id=token)
        self.assertEqual(p['ws'],self.hub.resolve_cdp_endpoint(p['ws']));self.native.verify_connection_endpoint(self.profile,p['ws'],p['generation'],token);self.native.commit_connection_attempt(token)
        action=self.native.begin_action(self.profile,p['ws'],p['generation'])
        with self.assertRaises(ConflictError):self.native.close_profile(self.profile)
        self.native.end_action(action);self.native.close_profile(self.profile);new=self.native.connection_endpoint(self.profile,open_if_needed=True)
        self.assertNotEqual(p,new)
        with self.assertRaises(ConflictError):self.native.verify_connection_endpoint(self.profile,p['ws'],p['generation'])
    def test_core_restart_recovers_existing_view_and_shutdown_keeps_login(self):
        p=self.native.connection_endpoint(self.profile,open_if_needed=True);self.native.shutdown()
        fresh=EmbeddedBrowser(self.db,self.tmp.name,bridge=self.bridge)
        self.assertTrue(fresh.inventory()[0]['is_open']);self.assertEqual(p,fresh.connection_endpoint(self.profile));fresh.close_profile(self.profile);self.assertFalse(self.bridge.live)
    def test_context_navigation_stays_under_task_lock_and_delete_hides_inventory(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='busy')
        for action in ('back','forward','refresh','delete'):
            with self.assertRaises(ConflictError):self.accounts.command(self.owner,{'id':plan['id'],'revision':1,'action':action})
        self.assertFalse(any(method=='manual-navigation' for method,_ in self.bridge.calls))
        self.assertEqual('studio',self.accounts.snapshot(self.owner)['locks'][self.profile]['operation_type'])
        self.service.release_browser_lease(self.profile,token)
        for action in ('back','forward','refresh'):
            self.assertTrue(self.accounts.command(self.owner,{'id':plan['id'],'action':action})['page_loaded'])
            self.assertEqual({},self.accounts.snapshot(self.owner)['locks'])
        calls=[b for method,b in self.bridge.calls if method=='manual-navigation']
        self.assertEqual(['back','forward','refresh'],[b['action'] for b in calls])
        self.assertTrue(all(b['profile']==self.profile and b['owner']==self.owner for b in calls))
        self.accounts.command(self.owner,{'id':plan['id'],'revision':1,'action':'delete'})
        self.assertEqual([],self.native.inventory());self.assertEqual([],self.accounts.snapshot(self.owner)['plans'])
        self.assertEqual(self.profile,self.native.get(self.profile)['id']);self.assertFalse(self.bridge.live)
    def test_edit_live_metadata_does_not_reconfigure_or_steal_task_window(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        result=self.accounts.save(self.owner,{'id':plan['id'],'revision':1,'native':True,'name':'renamed','group':'team','notes':'note'})
        self.assertEqual(2,result['revision']);self.assertEqual('renamed',self.native.get(self.profile)['name']);self.assertIn(self.profile,self.bridge.live)
        with self.assertRaises(ConflictError):self.accounts.save(self.owner,{'id':plan['id'],'revision':2,'native':True,'name':'renamed','proxy_server':'http://proxy.test:8080'})
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='job')
        try:
            with self.assertRaises(ConflictError):self.accounts.save(self.owner,{'id':plan['id'],'revision':2,'native':True,'name':'blocked'})
        finally:self.service.release_browser_lease(self.profile,token)
    def test_whatsapp_reset_is_confirmed_owned_and_blocked_during_tasks(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.accounts.save(self.owner,{'id':plan['id'],'revision':1,'native':True,'name':'WA','platform':'whatsapp'})
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        body={'id':plan['id'],'action':'reset_whatsapp_storage','revision':2,'confirm':True}
        from app.errors import ValidationError
        with self.assertRaises(ValidationError):self.accounts.command(self.owner,{**body,'confirm':False})
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='account',entity_id='fixture')
        try:
            with self.assertRaises(ConflictError):self.accounts.command(self.owner,body)
        finally:self.service.release_browser_lease(self.profile,token)
        self.assertTrue(self.accounts.command(self.owner,body)['reset']);self.assertNotIn(self.profile,self.bridge.live)
        reset=[b for m,b in self.bridge.calls if m=='reset-whatsapp-storage'];self.assertEqual([{'owner':self.owner,'profile':self.profile}],reset)
    def test_chat_translation_obeys_ownership_and_task_exclusion_without_hiding(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        body={'id':plan['id'],'action':'chat_translation','step':{'kind':'poll'}}
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='translation-guard')
        try:
            with self.assertRaises(ConflictError):self.accounts.command(self.owner,body)
        finally:self.service.release_browser_lease(self.profile,token)
        self.assertFalse(any(m=='chat-translation' for m,_ in self.bridge.calls))
        before_hides=sum(m=='hide' for m,_ in self.bridge.calls)
        self.assertEqual({'messages':[]},self.accounts.command(self.owner,body))
        self.assertEqual(before_hides,sum(m=='hide' for m,_ in self.bridge.calls),'poll must not hide the chat it is reading')
        self.assertEqual({},self.accounts.snapshot(self.owner)['locks'])
        call=[b for m,b in self.bridge.calls if m=='chat-translation'][-1]
        self.assertEqual(self.owner,call['owner']);self.assertEqual(self.profile,call['profile'])
        stranger=self.service.register_user('translation-stranger','correct horse battery staple')['id']
        from app.errors import NotFoundError
        with self.assertRaises(NotFoundError):self.accounts.command(stranger,body)

    def test_whatsapp_diagnostics_cannot_touch_a_busy_window(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.accounts.save(self.owner,{'id':plan['id'],'revision':1,'native':True,'name':'WA','platform':'whatsapp'})
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        body={'id':plan['id'],'action':'whatsapp_diagnostics'}
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='account',entity_id='busy')
        try:
            with self.assertRaises(ConflictError):self.accounts.command(self.owner,body)
        finally:self.service.release_browser_lease(self.profile,token)
        self.assertFalse(any(m=='whatsapp-diagnostics' for m,_ in self.bridge.calls))
        self.assertEqual({'checks':{'fixture':True}},self.accounts.command(self.owner,body))
        self.assertEqual({},self.accounts.snapshot(self.owner)['locks'])

    def test_task_waits_for_translation_dom_step_then_hides_before_acquisition(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        entered=threading.Event();release=threading.Event();task_started=threading.Event();acquired=threading.Event()
        failures=[];tokens=[];call=self.bridge.call
        def bridge(method,**body):
            if method=='chat-translation':
                entered.set()
                if not release.wait(3):raise TimeoutError('test DOM step stalled')
            return call(method,**body)
        def translate():
            try:self.accounts.command(self.owner,{'id':plan['id'],'action':'chat_translation','step':{'kind':'poll'}})
            except Exception as error:failures.append(error)
        def task():
            task_started.set()
            try:tokens.append(self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='after-translation'));acquired.set()
            except Exception as error:failures.append(error)
        with patch.object(self.bridge,'call',side_effect=bridge):
            reader=threading.Thread(target=translate);reader.start()
            self.assertTrue(entered.wait(2));worker=threading.Thread(target=task);worker.start()
            try:
                self.assertTrue(task_started.wait(1));self.assertFalse(acquired.wait(.1))
                self.assertFalse(any(m=='hide' for m,_ in self.bridge.calls))
            finally:release.set();reader.join(3);worker.join(3)
        self.assertEqual([],failures);self.assertTrue(acquired.is_set())
        self.assertEqual('hide',self.bridge.calls[-1][0])
        self.service.release_browser_lease(self.profile,tokens[0])

    def test_failed_translation_releases_fence_without_hiding_page(self):
        plan=self.accounts.snapshot(self.owner)['plans'][0]
        self.native.connection_endpoint(self.profile,open_if_needed=True)
        with patch.object(self.native,'chat_translation',side_effect=RuntimeError('DOM unavailable')):
            with self.assertRaisesRegex(RuntimeError,'DOM unavailable'):
                self.accounts.command(self.owner,{'id':plan['id'],'action':'chat_translation','step':{'kind':'apply'}})
        self.assertFalse(any(m=='hide' for m,_ in self.bridge.calls))
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='after-error')
        self.service.release_browser_lease(self.profile,token)

    def test_cancelled_connection_cannot_create_new_view(self):
        token=self.native.begin_connection_attempt(self.profile);self.native.cancel_connection_attempt(token)
        with self.assertRaises(ConflictError):self.native.connection_endpoint(self.profile,open_if_needed=True,attempt_id=token)
        self.assertFalse(self.bridge.live)
    def test_close_publishes_fence_before_waiting_for_pending_creation(self):
        self.bridge.hold=threading.Event();self.bridge.entered=threading.Event();token=self.native.begin_connection_attempt(self.profile);errors=[]
        def opening():
            try:self.native.connection_endpoint(self.profile,open_if_needed=True,attempt_id=token)
            except ConflictError:errors.append('cancelled')
        thread=threading.Thread(target=opening);thread.start();self.assertTrue(self.bridge.entered.wait(2))
        closing=threading.Thread(target=lambda:self.native.close_profile(self.profile));closing.start()
        deadline=time.monotonic()+2
        while self.profile not in self.native.closing and time.monotonic()<deadline:time.sleep(.01)
        try:
            self.assertIn(self.profile,self.native.closing)
            with self.assertRaises(ConflictError):self.native.begin_connection_attempt(self.profile)
        finally:self.bridge.hold.set();thread.join(3);closing.join(3)
        self.assertEqual(['cancelled'],errors);self.assertFalse(self.bridge.live)

if __name__=='__main__':unittest.main()
