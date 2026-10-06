"""Real authenticated HTTP + SQLite repro; browser transport is closed-proof only."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import isoformat
from test_core import FakeBitBrowserClient

class Browser(FakeBitBrowserClient):
    @contextmanager
    def closed_profile_guard(self, profile, owner):
        yield {'closed':True,'profile_id':profile,'owner_user_id':owner,'verification':'desktop-absence-v1'}
    def close_profile(self,*args,**kwargs):raise AssertionError('must not close a browser')
    def open_profile(self,*args,**kwargs):raise AssertionError('must not open a browser')

class HiddenCollectionBlockerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        self.db=Database(root/'db.sqlite');self.db.initialize()
        settings=Settings(startup_token='hidden-blocker-api-startup-token-12345',database_path=self.db.path,data_dir=root)
        self.app=create_app(settings,database=self.db,bitbrowser=Browser())
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.s=self.app.state.service;self.manager=self.app.state.execution_manager
        self.owners={};self.headers={}
        for name in ('owner','other'):
            self.owners[name]=self.s.register_user(name,'offline fixture password')['id']
            token=self.s.login(name,'offline fixture password')['token']
            self.headers[name]={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}
        self.owner=self.owners['owner'];self.ident=self.task(self.owner,'w1','hidden')
        with self.db.write() as c:
            c.execute("INSERT INTO task_list_dismissals VALUES(?,?,?)",(self.ident,self.owner,isoformat()))
            c.execute("INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,result_json,due_at,created_at,updated_at) VALUES('held',?,'held','nurture','w1','completed','{}',?,?,?,?)",
                (self.owner,json.dumps({'window_hold':True,'counts':{'browse':7},'window_cleanup':{'state':'lease_lost','lease_token':'old-token'}}),isoformat(),isoformat(),isoformat()))
    def task(self,owner,profile,name,completed=True):
        row=self.s.create_task(owner,name=name,modes=['followers'],targets=[name+'_source'],window_ids=[profile],settings={})
        target=row['targets'][0]['id']
        self.s.upsert_checkpoint(owner,row['id'],target,mode='followers',stage='completed',cursor={'position':17},counters={'saved':1})
        self.s.record_result(owner,row['id'],target,username=name+'_result',instagram_user_id='fixture-'+name,
            source_mode='followers',visibility='public',profile={'followers_count':4},screening={},qualified=False)
        with self.db.write() as c:
            c.execute("UPDATE tasks SET status='paused' WHERE id=?",(row['id'],))
            if completed:c.execute("UPDATE task_targets SET status='completed',current_window_id=?,current_stage='completed_archived' WHERE task_id=?",(profile,row['id']))
        return row['id']
    def command(self,action,owner='owner',**kwargs):
        return self.client.post('/api/studio/command',headers=self.headers[owner],json={'action':action,'job_id':'held',**kwargs})
    def locate(self):
        response=self.command('locate_cleanup_collection');self.assertEqual(200,response.status_code,response.text);return response.json()['blocker']
    def stop(self,blocker=None):
        blocker=blocker or self.locate()
        return self.command('stop_cleanup_collection',task_id=blocker['task_id'],version=blocker['version'])
    def retained(self):
        tables=['task_targets','task_checkpoints','task_results','task_list_dismissals','task_target_list_dismissals','studio_jobs','task_mode_candidates','instagram_accounts','instagram_username_aliases','global_seen','global_identity_owners','global_seen_stats','global_seen_platform_stats','review_records','task_mode_progress']
        with self.db.read() as c:
            known={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table:[tuple(row) for row in c.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in tables if table in known}
    def test_hidden_paused_task_is_found_and_stopped_without_unarchiving_or_unlock(self):
        response=self.client.get('/api/tasks',headers=self.headers['owner']);self.assertEqual(200,response.status_code);self.assertEqual([],response.json()['tasks'])
        refusal=self.command('control',operation='retry_cleanup');self.assertEqual(409,refusal.status_code);self.assertIn(self.ident,refusal.text)
        before=self.retained()
        for table in ('task_checkpoints','task_results','instagram_accounts','global_seen','global_identity_owners'):
            self.assertTrue(before[table], table+' must contain real retained evidence')
        blocker=self.locate();self.assertTrue(blocker['dismissed']);self.assertTrue(blocker['can_stop']);self.assertEqual(['w1'],blocker['window_ids'])
        response=self.stop(blocker);self.assertEqual(200,response.status_code,response.text);self.assertEqual('stopped',response.json()['status'])
        self.assertEqual(before,self.retained());self.assertEqual([],self.client.get('/api/tasks',headers=self.headers['owner']).json()['tasks'])
        with self.db.read() as c:self.assertEqual('stopped',c.execute('SELECT status FROM tasks WHERE id=?',(self.ident,)).fetchone()[0])
        clean=self.command('control',operation='retry_cleanup');self.assertEqual(200,clean.status_code,clean.text);self.assertTrue(clean.json()['cleanup_reconciled'])
        with self.db.read() as c:
            result=json.loads(c.execute("SELECT result_json FROM studio_jobs WHERE id='held'").fetchone()[0]);self.assertFalse(result['window_hold']);self.assertEqual({'browse':7},result['counts'])
    def test_unarchived_completed_targets_can_be_located_without_backfill(self):
        with self.db.write() as c:c.execute('DELETE FROM task_list_dismissals')
        blocker=self.locate();self.assertFalse(blocker['dismissed']);self.assertTrue(blocker['can_stop']);self.assertEqual(200,self.stop(blocker).status_code)
    def test_foreign_owner_cannot_inspect_or_stop(self):
        for action in ('locate_cleanup_collection','stop_cleanup_collection'):
            response=self.command(action,owner='other',task_id=self.ident,version=1);self.assertEqual(404,response.status_code);self.assertNotIn(self.ident,response.text)
    def test_foreign_blocker_does_not_disclose_identity(self):
        with self.db.write() as c:c.execute('UPDATE tasks SET owner_user_id=? WHERE id=?',(self.owners['other'],self.ident))
        response=self.command('locate_cleanup_collection');self.assertEqual(409,response.status_code);self.assertNotIn(self.ident,response.text);self.assertNotIn('hidden',response.text)
    def test_stale_version_and_changed_task_are_rejected(self):
        blocker=self.locate()
        with self.db.write() as c:c.execute('UPDATE tasks SET version=version+1 WHERE id=?',(self.ident,))
        self.assertEqual(409,self.stop(blocker).status_code)
        self.assertEqual(409,self.command('stop_cleanup_collection',task_id='another',version=1).status_code)
        for value in (None,True,'1'):
            self.assertEqual(422,self.command('stop_cleanup_collection',task_id=self.ident,version=value).status_code)
    def test_new_active_status_prevents_stale_stop(self):
        blocker=self.locate()
        with self.db.write() as c:c.execute("UPDATE tasks SET status='running' WHERE id=?",(self.ident,))
        self.assertEqual(409,self.stop(blocker).status_code)
        self.assertFalse(self.locate()['can_stop'])
    def test_any_lease_even_expired_successor_is_preserved(self):
        blocker=self.locate()
        for profile,owner,operation,entity in [('w1',self.owners['other'],'account','successor'),('w1',self.owner,'collection',self.ident),('w2',self.owner,'collection',self.ident)]:
            with self.db.write() as c:c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?,?,?,?)',(profile,owner,operation,entity,'token','2000','2000','2000'))
            with self.db.read() as c:before=[tuple(r) for r in c.execute('SELECT * FROM browser_operation_leases')]
            self.assertFalse(self.locate()['can_stop']);self.assertEqual(409,self.stop(blocker).status_code)
            with self.db.read() as c:self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM browser_operation_leases')])
            with self.db.write() as c:c.execute('DELETE FROM browser_operation_leases')
    def test_live_paused_worker_requires_normal_drain(self):
        self.manager._runs[self.ident]=SimpleNamespace(coordinator=SimpleNamespace(done=lambda:False),worker_tasks=[])
        self.addCleanup(self.manager._runs.clear)
        self.assertFalse(self.locate()['can_stop']);self.assertEqual(409,self.stop().status_code)
    def test_active_studio_jobs_block_even_without_a_lease(self):
        studio=self.app.state.studio
        try:
            for ident in ('held','other-cleanup'):
                studio.tasks[ident]=SimpleNamespace(done=lambda:False)
                studio.task_profiles[ident]='w1'
                self.assertFalse(self.locate()['can_stop']);self.assertEqual(409,self.stop().status_code)
                studio.tasks.clear();studio.task_profiles.clear()
        finally:
            studio.tasks.clear();studio.task_profiles.clear()

    def test_unfinished_bound_targets_are_not_blindly_stopped(self):
        with self.db.write() as c:c.execute("UPDATE tasks SET status='stopped' WHERE id=?",(self.ident,))
        self.ident=self.task(self.owner,'w1','unfinished',completed=False)
        with self.db.write() as c:c.execute("UPDATE task_targets SET current_window_id='w1' WHERE task_id=?",(self.ident,))
        before=self.retained();self.assertFalse(self.locate()['can_stop']);self.assertEqual(409,self.stop().status_code);self.assertEqual(before,self.retained())
    def test_terminal_parent_with_unfinished_bound_target_matches_cleanup_gate(self):
        with self.db.write() as c:c.execute("UPDATE tasks SET status='stopped' WHERE id=?",(self.ident,))
        self.ident=self.task(self.owner,'w1','terminal_parent',completed=False)
        with self.db.write() as c:
            c.execute("UPDATE tasks SET status='stopped' WHERE id=?",(self.ident,))
            c.execute("UPDATE task_targets SET current_window_id='w1' WHERE task_id=?",(self.ident,))
        refused=self.command('control',operation='retry_cleanup')
        self.assertEqual(409,refused.status_code);self.assertIn(self.ident,refused.text)
        blocker=self.locate();self.assertEqual(self.ident,blocker['task_id']);self.assertEqual('stopped',blocker['status'])
        self.assertFalse(blocker['can_stop']);self.assertEqual(409,self.stop(blocker).status_code)

    def test_other_profile_task_and_lease_are_untouched(self):
        other=self.task(self.owner,'w5','unrelated')
        with self.db.write() as c:
            c.execute("UPDATE tasks SET status='running' WHERE id=?",(other,))
            c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?,?,?,?)',('w5',self.owner,'collection',other,'other-token','2099','2099','2099'))
        with self.db.read() as c:before=tuple(c.execute('SELECT * FROM tasks WHERE id=?',(other,)).fetchone());lease=tuple(c.execute('SELECT * FROM browser_operation_leases').fetchone())
        self.assertEqual(200,self.stop().status_code)
        with self.db.read() as c:
            self.assertEqual(before,tuple(c.execute('SELECT * FROM tasks WHERE id=?',(other,)).fetchone()));self.assertEqual(lease,tuple(c.execute('SELECT * FROM browser_operation_leases').fetchone()))
    def test_normal_restart_keeps_locator_and_stop_does_not_restore_hidden_history(self):
        self.db.initialize();self.s.recover_interrupted_operations()
        blocker=self.locate();self.assertEqual(self.ident,blocker['task_id']);self.assertTrue(blocker['can_stop'])
        self.assertEqual(200,self.stop(blocker).status_code)
        self.db.initialize();self.s.recover_interrupted_operations()
        with self.db.read() as c:
            self.assertEqual('stopped',c.execute('SELECT status FROM tasks WHERE id=?',(self.ident,)).fetchone()[0])
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM task_list_dismissals WHERE task_id=?',(self.ident,)).fetchone()[0])
        self.assertIsNone(self.locate())

    def test_lookup_ignores_page_and_target_budget(self):
        for i in range(12):self.task(self.owner,'other',f'new{i}')
        response=self.client.get('/api/tasks?limit=1&detail_limit=1',headers=self.headers['owner']);self.assertTrue(response.json()['has_more'])
        self.assertEqual(self.ident,self.locate()['task_id'])
    def test_missing_credentials_cannot_locate_or_stop(self):
        for action in ('locate_cleanup_collection','stop_cleanup_collection'):
            response=self.client.post('/api/studio/command',json={'action':action,'job_id':'held','task_id':self.ident,'version':1});self.assertEqual(401,response.status_code)


class HiddenCollectionBlockerAsyncTests(unittest.IsolatedAsyncioTestCase):
    setUp=HiddenCollectionBlockerTests.setUp
    task=HiddenCollectionBlockerTests.task

    async def test_surface_contention_keeps_loop_responsive_and_cancel_drains_stop(self):
        import threading
        from app.nurture_collection_blocker import locate_collection_blocker
        studio=self.app.state.studio
        blocker=locate_collection_blocker(studio,self.manager,self.owner,'held')['blocker']
        entered=threading.Event();release=threading.Event()
        def hold_surface():
            with self.db.browser_surface_lock:
                entered.set()
                if not release.wait(3):raise AssertionError('surface fixture timeout')
        holder=threading.Thread(target=hold_surface);holder.start()
        await asyncio.to_thread(entered.wait,1)
        stopping=asyncio.create_task(self.manager.stop_nurture_collection_blocker(
            studio,self.owner,'held',self.ident,blocker['version']))
        try:
            await asyncio.sleep(.02)
            self.assertFalse(stopping.done());self.assertTrue(self.manager._lock.locked())
            stopping.cancel();await asyncio.sleep(.02)
            self.assertFalse(stopping.done(),'cancellation must not release manager ownership early')
            self.assertTrue(self.manager._lock.locked())
        finally:
            release.set();await asyncio.to_thread(holder.join,1)
            await asyncio.gather(stopping,return_exceptions=True)
        self.assertFalse(self.manager._lock.locked())
        with self.db.read() as c:
            self.assertEqual('stopped',c.execute('SELECT status FROM tasks WHERE id=?',(self.ident,)).fetchone()[0])
            self.assertTrue(json.loads(c.execute("SELECT result_json FROM studio_jobs WHERE id='held'").fetchone()[0])['window_hold'])
