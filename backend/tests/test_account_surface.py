"""Window presentation cannot acquire, release, or bypass a running task lease."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.account_workspace import AccountWorkspace
from app.account_surface import AccountSurface
from app.native_browser import NativeBrowser
from app.errors import ConflictError, ValidationError

class Adapter:
    def __init__(self):self.visible=set();self.shows=[];self.closed=False
    def surface_hide(self,profile=None):
        if profile:self.visible.discard(profile)
        else:self.visible.clear()
    def surface_show(self,profile,bounds,grant,target=None,read_only=False):
        if self.closed:return {'attached':False}
        self.visible={profile};self.shows.append((profile,bounds,grant));return {'attached':True}

class AccountSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'a.sqlite');self.db.initialize();self.service=CoreService(self.db)
        self.owner=self.service.register_user('surface-owner','correct horse battery staple')['id']
        self.other=self.service.register_user('surface-other','correct horse battery staple')['id']
        self.native=NativeBrowser(self.db,self.tmp.name)
        self.manager=AccountWorkspace(self.service,SimpleNamespace(native=self.native))
        self.ident=self.manager.save(self.owner,{'name':'窗口一','native':True})['id']
        self.row=self.manager.get(self.owner,self.ident);self.profile=self.row['profile_id']
        self.native.processes[self.profile]={'process':SimpleNamespace(pid=3100,poll=lambda:None)}
        self.adapter=Adapter();self.surface=AccountSurface(self.service,self.native,adapter=self.adapter)
        self.service.before_browser_operation=self.surface.suspend
        self.body={'visible':True,'grant':'test-grant','bounds':{'x':100,'y':120,'width':900,'height':650}}
    def tearDown(self):self.tmp.cleanup()
    def show(self):return self.surface.update(self.owner,self.row,self.body)
    def test_same_account_page_used_without_creating_another_session_or_lease(self):
        self.assertTrue(self.show()['attached']);self.assertEqual(self.profile,self.adapter.shows[-1][0])
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
    def test_task_disables_manual_surface_before_lease_is_returned(self):
        self.show();token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='post-one')
        self.assertFalse(self.adapter.visible)
        with self.assertRaises(ConflictError):self.show()
        with self.db.read() as c:self.assertEqual(token,c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',(self.profile,)).fetchone()[0])
        self.surface.update(self.owner,None,{'visible':False})
        with self.db.read() as c:self.assertEqual(token,c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',(self.profile,)).fetchone()[0])
        self.service.release_browser_lease(self.profile,token);self.assertTrue(self.show()['attached'])
    def test_readonly_task_surface_preserves_lease_and_other_controls_remain_locked(self):
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='watch')
        self.assertTrue(self.surface.update(self.owner,self.row,{**self.body,'read_only':True,'view_target':'task-page'})['attached'])
        with self.assertRaises(ConflictError):self.show()
        with self.assertRaises(ConflictError):self.manager.command(self.owner,{'id':self.ident,'action':'close'})
        self.service.release_browser_lease(self.profile,token)
        with self.assertRaises(ConflictError):self.surface.update(self.owner,self.row,{**self.body,'read_only':True,'view_target':'task-page'})

    def test_superseded_interactive_denial_cannot_hide_newer_readonly_task_surface(self):
        # Aborting fetch does not stop a request already accepted by Core.
        arrived=threading.Event();release=threading.Event();errors=[]
        def old_request():
            arrived.set()
            if not release.wait(5):return
            try:self.surface.update(self.owner,self.row,{**self.body,'grant':'old-grant'})
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=old_request)
        thread.start()
        try:
            self.assertTrue(arrived.wait(2))
            token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='collection',entity_id='new-task')
            self.assertFalse(self.adapter.visible)
            current={**self.body,'grant':'new-grant','read_only':True,'view_target':'task-page'}
            self.assertTrue(self.surface.update(self.owner,self.row,current)['attached'])
            release.set();thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],ConflictError)
            self.assertEqual(self.adapter.visible,{self.profile})
            self.assertEqual(self.adapter.shows[-1][2],'new-grant')
            with self.db.read() as c:
                self.assertEqual(c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',(self.profile,)).fetchone()[0],token)
        finally:
            release.set();thread.join(5)

    def test_interference_requires_current_lease_and_selected_page(self):
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='job')
        key=self.surface.task_key({'lease_token':token})
        with self.assertRaises(ConflictError):self.surface.permit_interference(self.owner,self.row,key,'page-one')
        body={**self.body,'interference_grant':'obsolete-permit','view_target':'page-one'}
        with self.assertRaises(ConflictError):self.surface.update(self.owner,self.row,body)
        self.assertTrue(self.surface.update(self.owner,self.row,{**body,'read_only':True})['attached'])
        self.service.release_browser_lease(self.profile,token)
        self.assertTrue(self.surface.update(self.owner,self.row,body)['attached'])

    def test_all_other_task_types_also_disable_surface(self):
        for operation in ('collection','action','monitor','account'):
            self.show();token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type=operation,entity_id='run')
            self.assertFalse(self.adapter.visible);self.service.release_browser_lease(self.profile,token)
    def test_failed_native_input_suspension_rolls_back_task_acquisition(self):
        def fail(profile):raise RuntimeError('cannot disable input')
        self.service.before_browser_operation=fail
        with self.assertRaises(RuntimeError):self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='run')
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
    def test_wrong_owner_and_stale_plan_never_show(self):
        with self.assertRaises(ConflictError):self.surface.update(self.other,self.row,self.body)
        with self.db.write() as c:c.execute('UPDATE account_window_plans SET revision=revision+1 WHERE id=?',(self.ident,))
        with self.assertRaises(ConflictError):self.show()
        self.assertFalse(self.adapter.shows)
    def test_closed_browser_and_invalid_geometry_are_not_reported_as_embedded(self):
        for bounds in ({},{'x':0,'y':0,'width':-1,'height':100},{'x':0,'y':0,'width':True,'height':100}):
            with self.assertRaises(ValidationError):self.surface.update(self.owner,self.row,{**self.body,'bounds':bounds})
        self.adapter.closed=True;self.assertFalse(self.show()['attached'])
    def test_hide_and_switch_only_change_presentation(self):
        self.show();self.surface.update(self.owner,None,{'visible':False});self.assertFalse(self.adapter.visible)
        self.assertEqual(self.profile,self.manager.get(self.owner,self.ident)['profile_id'])
        self.assertEqual(1,len(self.native.processes))
    def test_unsupported_environment_has_explicit_status(self):
        surface=AccountSurface(self.service,None)
        self.assertFalse(surface.update(self.owner,self.row,self.body)['attached'])
    def test_toolbar_actions_are_rejected_while_task_owns_window(self):
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='studio',entity_id='run')
        for action in ('home','refresh','inbox','close','open','profile_preview'):
            with self.subTest(action=action),self.assertRaises(ConflictError):self.manager.command(self.owner,{'action':action,'id':self.ident})
        self.service.release_browser_lease(self.profile,token)


    def test_close_one_task_page_checks_current_owner_binding_and_lease(self):
        calls=[]
        def call(method,**body):
            calls.append((method,body))
            return {'generation':3,'pages':[{'id':'screen-2'}]} if method=='watch-profile' else {'closed':True,'target':body['target']}
        self.native.bridge=SimpleNamespace(call=call)
        token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='collection',entity_id='close-test')
        key=self.surface.task_key({'lease_token':token})
        info=self.manager.task_page_close_info(self.owner,self.ident,key,'screen-2')
        self.assertTrue(self.manager.close_task_page(self.owner,info)['closed'])
        self.assertEqual(calls[-1][0],'close-task-page')
        self.assertEqual(calls[-1][1]['target'],'screen-2')
        with self.db.read() as c:self.assertEqual(token,c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',(self.profile,)).fetchone()[0])
        with self.assertRaises(ConflictError):self.manager.task_page_close_info(self.owner,self.ident,'stale','screen-2')
        with self.assertRaises(Exception):self.manager.task_page_close_info(self.other,self.ident,key,'screen-2')
        self.service.release_browser_lease(self.profile,token)
        replacement=self.service.acquire_browser_lease(self.owner,self.profile,operation_type='collection',entity_id='replacement')
        with self.assertRaises(ConflictError):self.manager.close_task_page(self.owner,info)
        self.assertEqual(len([x for x in calls if x[0]=='close-task-page']),1)
        self.service.release_browser_lease(self.profile,replacement)

if __name__=='__main__':unittest.main()
