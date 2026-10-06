"""Target previews use owned native accounts under the existing operation fence."""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService,isoformat
from app.account_workspace import AccountWorkspace
from app.native_browser import NativeBrowser
from app.errors import ConflictError,NotFoundError,ValidationError

class ProfilePreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'test.sqlite');self.db.initialize()
        self.service=CoreService(self.db);self.owner=self.service.register_user('preview-owner','correct horse battery staple')['id']
        self.other=self.service.register_user('preview-other','correct horse battery staple')['id']
        self.native=NativeBrowser(self.db,self.tmp.name);self.calls=[]
        def preview(profile,username,action):
            with self.db.read() as c:
                row=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone()
                self.assertEqual('account',row['operation_type']);self.assertEqual(self.owner,row['owner_user_id'])
            self.calls.append((profile,username,action));return {'opened':True,'page_loaded':False,'http_status':403,'message':'HTTP 403'}
        self.native.preview_profile=preview
        self.accounts=AccountWorkspace(self.service,SimpleNamespace(native=self.native))
        self.ident=self.accounts.save(self.owner,{'native':True,'name':'审核窗口'})['id']
        self.profile=self.accounts.get(self.owner,self.ident)['profile_id']
        self.body={'action':'profile_preview','id':self.ident,'username':'target.user','preview_action':'target'}
    def tearDown(self):self.tmp.cleanup()
    def test_preview_uses_owned_profile_under_lease_and_preserves_http_failure(self):
        result=self.accounts.command(self.owner,self.body)
        self.assertEqual(403,result['http_status']);self.assertFalse(result['page_loaded'])
        self.assertEqual([(self.profile,'target.user','target')],self.calls)
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
    def test_every_running_task_blocks_preview_and_keeps_original_lease(self):
        # Real task/campaign rows are required: orphan leases are intentionally
        # reconciled by the scheduler and cannot stand in for running work.
        now=isoformat()
        with self.db.write() as c:
            c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES('original-task',?,'collection','running','[]','{}',?,?)",(self.owner,now,now))
            c.execute("INSERT INTO action_campaigns(id,owner_user_id,operation,execution_type,profile_id,interval_min_seconds,interval_max_seconds,limit_count,status,created_at,updated_at) VALUES('original-task',?,'greet','campaign',?,8,15,10,'running',?,?)",(self.owner,self.profile,now,now))
        for operation in ('collection','action','monitor','studio','account'):
            token=self.service.acquire_browser_lease(self.owner,self.profile,operation_type=operation,entity_id='original-task')
            with self.assertRaises(ConflictError):self.accounts.command(self.owner,self.body)
            with self.db.read() as c:
                row=c.execute('SELECT lease_token,operation_type,entity_id FROM browser_operation_leases WHERE profile_id=?',(self.profile,)).fetchone()
                self.assertEqual((token,operation,'original-task'),tuple(row))
            self.service.release_browser_lease(self.profile,token)
        self.assertEqual([],self.calls)
    def test_wrong_owner_cannot_borrow_account_session(self):
        with self.assertRaises(NotFoundError):self.accounts.command(self.other,self.body)
        self.assertEqual([],self.calls)
    def test_invalid_targets_and_other_platforms_never_navigate(self):
        for value in ('../direct','https://evil.test','user?query=1','',None):
            with self.assertRaises(ValidationError):self.accounts.command(self.owner,{**self.body,'username':value})
        ident=self.accounts.save(self.owner,{'native':True,'name':'其他平台','platform':'whatsapp'})['id']
        with self.assertRaises(ValidationError):self.accounts.command(self.owner,{**self.body,'id':ident})
        self.assertEqual([],self.calls)
    def test_navigation_error_releases_only_its_own_temporary_operation(self):
        other_id=self.accounts.save(self.owner,{'native':True,'name':'任务窗口'})['id']
        other_profile=self.accounts.get(self.owner,other_id)['profile_id']
        token=self.service.acquire_browser_lease(self.owner,other_profile,operation_type='studio',entity_id='ongoing')
        def fail(*args):raise RuntimeError('network error')
        self.native.preview_profile=fail
        with self.assertRaises(RuntimeError):self.accounts.command(self.owner,self.body)
        with self.db.read() as c:
            rows=c.execute('SELECT profile_id,lease_token FROM browser_operation_leases').fetchall()
            self.assertEqual([(other_profile,token)],[tuple(row) for row in rows])

if __name__=='__main__':unittest.main()
