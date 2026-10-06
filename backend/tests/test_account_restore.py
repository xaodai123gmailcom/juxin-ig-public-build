import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.account_workspace import AccountWorkspace
from app.native_browser import NativeBrowser
from app.errors import ConflictError

class RestoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'restore.sqlite');self.db.initialize();self.service=CoreService(self.db)
  self.owner=self.service.register_user('restore-owner','correct horse battery staple')['id'];self.other=self.service.register_user('restore-other','correct horse battery staple')['id']
  self.native=NativeBrowser(self.db,self.tmp.name);self.opened=set();self.calls=[]
  self.native.inventory=lambda:[{'id':p,'is_open':True,'owner_user_id':self.native.get(p)['owner_user_id']} for p in self.opened]
  def close(p):self.opened.discard(p);return {'closed':True}
  def opener(provider,p,platform,cookies):self.calls.append(p);self.opened.add(p);return {'opened':True,'page_loaded':True}
  self.m=AccountWorkspace(self.service,SimpleNamespace(native=self.native,close_profile=close),opener=opener)
 def tearDown(self):self.tmp.cleanup()
 def plan(self,owner=None):return self.m.save(owner or self.owner,{'name':'restore fixture','native':True})['id']
 def state(self,ident):
  with self.db.read() as c:
   row=c.execute('SELECT opened FROM account_window_open_state WHERE plan_id=?',(ident,)).fetchone();return None if row is None else row[0]
 def test_restart_restores_only_opened_windows_for_current_owner(self):
  a,b,c=self.plan(),self.plan(),self.plan(self.other)
  for ident,owner in ((a,self.owner),(b,self.owner),(c,self.other)):self.m.command(owner,{'id':ident,'action':'open'})
  self.m.command(self.owner,{'id':b,'action':'close'});self.opened.clear();self.calls.clear();self.db.initialize();self.m.restore.run(self.owner)
  self.assertEqual([self.m.get(self.owner,a)['profile_id']],self.calls);self.assertEqual(0,self.state(b));self.assertEqual(1,self.state(c))
 def test_restore_rechecks_closed_intent_and_preserves_task_lock(self):
  a=self.plan();self.m.command(self.owner,{'id':a,'action':'open'});self.m.command(self.owner,{'id':a,'action':'close'});self.calls.clear()
  self.assertTrue(self.m.command(self.owner,{'id':a,'action':'restore'})['skipped']);self.assertFalse(self.calls)
  self.m.command(self.owner,{'id':a,'action':'open'});self.opened.clear();self.calls.clear();profile=self.m.get(self.owner,a)['profile_id']
  token=self.service.acquire_browser_lease(self.owner,profile,operation_type='studio',entity_id='fixture')
  self.m.restore.run(self.owner);self.assertFalse(self.calls);self.assertEqual(1,self.state(a));self.assertEqual([a],self.m.restore.status(self.owner)['failed'])
  self.m.restore.started.add(self.owner);self.m.restore.capture_before_shutdown();self.assertEqual(1,self.state(a),'failed restore must stay open for next startup')
  self.service.release_browser_lease(profile,token)
 def test_shutdown_records_actual_state_before_cleanup_and_keeps_config(self):
  a,b=self.plan(),self.plan();self.m.command(self.owner,{'id':a,'action':'open'});self.m.command(self.owner,{'id':b,'action':'open'})
  self.opened.remove(self.m.get(self.owner,b)['profile_id']);self.m.restore.started.add(self.owner);self.m.restore.capture_before_shutdown();self.opened.clear()
  self.assertEqual(1,self.state(a));self.assertEqual(0,self.state(b));self.assertEqual(2,len(self.m.snapshot(self.owner)['plans']))
if __name__=='__main__':unittest.main()
