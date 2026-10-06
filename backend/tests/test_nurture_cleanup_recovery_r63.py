"""Offline historical completion recovery; no browser or real user data."""
import asyncio
import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_studio as fixtures
from app.database import Database
from app.errors import ConflictError, UpstreamUnavailableError
from app.nurture_cleanup_recovery import reconcile_closed_nurture
from app.service import CoreService
from app.studio import StudioManager


class ClosedProvider(fixtures.Browser):
    def __init__(self):
        super().__init__()
        self.checks=[];self.failure=None;self.before_proof=None;self.inside=False
        self.proof_override=None

    @contextmanager
    def closed_profile_guard(self, profile, owner):
        self.checks.append((profile,owner))
        if self.failure:raise self.failure
        if self.before_proof:self.before_proof()
        self.inside=True
        try:
            yield self.proof_override if self.proof_override is not None else {
                'closed':True,'profile_id':profile,'owner_user_id':owner,'verification':'desktop-absence-v1'}
        finally:self.inside=False


class NurtureCleanupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown

    async def asyncSetUp(self):
        self.provider=ClosedProvider();self.m.bitbrowser=self.provider

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def until(self,predicate):
        async with asyncio.timeout(3):
            while not predicate():await asyncio.sleep(.001)

    async def seed(self, *, state=None, status='completed', deleted=False):
        ident=(await self.m.command(self.owner,{'action':'start','kind':'nurture',
            'profile_ids':['w1'],'request_id':'recovery-fixture-request','config':{'minutes':1}}))['job_ids'][0]
        result={'window_hold':True,'counts':{'browse':4,'like':2},'confirmed_at':'2026-10-02T12:00:00Z',
                'nurture_outcome':status,'nurture_finished_at':'2026-10-02T12:00:00Z'}
        if state:result['window_cleanup']={'state':state,'lease_token':'historical-token'}
        self.m.update(ident,status=status,cursor=4,result_json=json.dumps(result),message='historical message')
        if deleted:
            with self.db.write() as c:c.execute("UPDATE studio_jobs SET deleted_at='2026-10-03' WHERE id=?",(ident,))
        return ident

    def row(self, ident):
        with self.db.read() as c:return dict(c.execute('SELECT * FROM studio_jobs WHERE id=?',(ident,)).fetchone())

    def clone(self, ident, new_id, profile='w1'):
        source=self.row(ident)
        with self.db.write() as c:
            c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,result_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (new_id,self.owner,new_id,'nurture',profile,source['status'],source['config_json'],source['result_json'],source['due_at'],source['created_at'],source['updated_at']))
        return new_id

    def raw_lease(self, ident, *, owner=None, token='replacement', operation='account'):
        with self.db.write() as c:
            c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?,?,?,?)',
                ('w1',owner or self.owner,operation,ident,token,'2000-01-01','2000-01-01','2000-01-01'))

    async def test_legacy_closed_reconciles_without_changing_history_or_browser(self):
        ident=await self.seed();before=self.row(ident)
        self.m.recover()
        result=await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertTrue(result['cleanup_reconciled']);self.assertFalse(result['cleanup_pending'])
        after=self.row(ident);saved=json.loads(after['result_json'])
        self.assertFalse(saved['window_hold'])
        self.assertEqual('reconciled_closed',saved['window_cleanup']['state'])
        self.assertEqual('desktop-absence-v1',saved['window_cleanup']['reconciliation_evidence']['verification'])
        self.assertIn('reconciled_at',saved['window_cleanup'])
        for key in ('counts','confirmed_at','nurture_finished_at','nurture_outcome'):
            self.assertEqual(json.loads(before['result_json'])[key],saved[key])
        for key in ('updated_at','created_at','cursor','status','deleted_at'):
            self.assertEqual(before[key],after[key])
        self.assertEqual([],self.provider.closed)
        token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='account',entity_id='new-user-request')
        self.s.release_browser_lease('w1',token)

    async def test_pending_missing_lease_can_be_verified_explicitly(self):
        ident=await self.seed(state='pending')
        self.assertTrue((await self.m.control(self.owner,ident,'retry_cleanup'))['cleanup_reconciled'])
        self.assertEqual('historical-token',json.loads(self.row(ident)['result_json'])['window_cleanup']['lease_token'])

    async def test_owned_pending_cleanup_keeps_existing_path(self):
        ident=await self.seed(state='pending')
        self.raw_lease(ident,token='historical-token',operation='studio')
        with patch.object(self.m,'_schedule_nurture_cleanup') as schedule:
            response=await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertTrue(response['cleanup_pending']);schedule.assert_called_once()
        self.assertEqual([],self.provider.checks)

    async def test_any_replacement_lease_is_preserved_even_expired_foreign(self):
        ident=await self.seed(state='lease_lost');self.raw_lease('another',owner=self.other)
        before=self.row(ident)
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertEqual(before,self.row(ident));self.assertEqual([],self.provider.checks)
        with self.db.read() as c:self.assertEqual('replacement',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])

    async def test_open_or_unknown_provider_keeps_hold(self):
        ident=await self.seed(state='lease_lost')
        for error in (ConflictError('open'),UpstreamUnavailableError('unknown')):
            self.provider.failure=error;before=self.row(ident)
            with self.assertRaises(type(error)):await self.m.control(self.owner,ident,'retry_cleanup')
            self.assertEqual(before,self.row(ident))
        self.assertEqual([],self.provider.closed)

    async def test_malformed_or_wrong_profile_proof_is_not_closure(self):
        ident=await self.seed(state='lease_lost')
        for proof in ({},{'closed':True}, {'closed':True,'profile_id':'other','owner_user_id':self.owner,'verification':'desktop-absence-v1'}):
            self.provider.proof_override=proof;before=self.row(ident)
            with self.assertRaises(UpstreamUnavailableError):await self.m.control(self.owner,ident,'retry_cleanup')
            self.assertEqual(before,self.row(ident))

    async def test_inflight_and_pending_or_unresolved_actions_are_preserved(self):
        ident=await self.seed(state='lease_lost');original=self.row(ident)
        for state in ('pending','unresolved_stopped','unknown',None):
            result=json.loads(original['result_json']);result['nurture_actions']={'like:x':{'state':state}}
            self.m.update(ident,result_json=json.dumps(result))
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.m.update(ident,result_json=original['result_json'],inflight=1)
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertEqual([],self.provider.checks)

    async def test_active_same_profile_task_prevents_recovery(self):
        ident=await self.seed(state='lease_lost');task=asyncio.create_task(asyncio.sleep(60))
        self.m.tasks['other']=task;self.m.task_profiles['other']='w1'
        try:
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
            self.assertEqual([],self.provider.checks)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_unfinished_studio_job_blocks_even_without_a_lease(self):
        ident=await self.seed(state='lease_lost');source=self.row(ident)
        with self.db.write() as c:
            c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('other',self.other,'other','nurture','w1','paused',source['config_json'],source['due_at'],source['created_at'],source['updated_at']))
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertEqual([],self.provider.checks)

    async def test_other_manager_activity_rechecked_after_provider_proof(self):
        ident=await self.seed(state='lease_lost');active=[True]
        self.m.cleanup_profile_active=lambda profile:profile=='w1' and active[0]
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertEqual([],self.provider.checks)
        active[0]=False
        self.provider.before_proof=lambda:active.__setitem__(0,True)
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])

    async def test_result_change_during_provider_verification_prevents_commit(self):
        ident=await self.seed(state='lease_lost')
        self.provider.before_proof=lambda:self.m.update(ident,message='concurrent edit')
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertEqual('concurrent edit',self.row(ident)['message'])

    async def test_replacement_arriving_during_provider_verification_is_not_touched(self):
        ident=await self.seed(state='lease_lost')
        self.provider.before_proof=lambda:self.raw_lease('concurrent',owner=self.other)
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])

    async def test_failed_legacy_row_does_not_global_block_or_get_rewritten(self):
        ident=await self.seed(state='lease_lost',status='failed');before=self.row(ident)
        with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry_cleanup')
        self.m.recover();self.assertEqual(before,self.row(ident))
        token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='account',entity_id='normal-open')
        self.s.release_browser_lease('w1',token)
        self.assertEqual([],self.provider.checks)

    async def test_archived_and_old_holds_are_visible_once_and_recoverable(self):
        ident=await self.seed(state='lease_lost',deleted=True);source=self.row(ident)
        with self.db.write() as c:
            for i in range(501):
                c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (f'new-{i}',self.owner,f'new-{i}','nurture','other','completed',source['config_json'],source['due_at'],'2099-01-01','2099-01-01'))
        jobs=self.m.snapshot(self.owner)['jobs']
        self.assertEqual(1,sum(job['id']==ident for job in jobs))
        self.assertTrue((await self.m.control(self.owner,ident,'retry_cleanup'))['cleanup_reconciled'])
        self.assertEqual(source['deleted_at'],self.row(ident)['deleted_at'])

    async def test_restart_attempt_recovers_closed_hold_once(self):
        ident=await self.seed();fresh_db=Database(self.db.path);fresh_db.initialize()
        service=CoreService(fresh_db,session_hours=1);service.recover_interrupted_operations()
        newer=StudioManager(service,self.provider);newer.recover();newer.start_scheduler()
        try:await asyncio.wait_for(newer.cleanup_recovery_task,3)
        finally:await newer.shutdown()
        self.assertFalse(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertEqual(1,len(self.provider.checks))
        next_manager=StudioManager(service,self.provider);next_manager.recover();next_manager.start_scheduler()
        try:
            self.assertIsNone(next_manager.cleanup_recovery_task)
            self.assertEqual(1,len(self.provider.checks))
        finally:await next_manager.shutdown()

    async def test_open_startup_attempt_does_not_repeat_each_scheduler_tick(self):
        ident=await self.seed();self.provider.failure=ConflictError('still open')
        self.m.recover();self.m.start_scheduler();await asyncio.wait_for(self.m.cleanup_recovery_task,3)
        for _ in range(4):self.m._schedule_ready()
        self.m.start_scheduler()
        self.assertEqual(1,len(self.provider.checks))
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])

    async def test_startup_skips_other_rows_of_failed_profile_but_checks_next_profile(self):
        first=await self.seed();second=self.clone(first,'second');third=self.clone(first,'third','w2')
        def reject_first_profile():
            if self.provider.checks[-1][0]=='w1':raise ConflictError('still open')
        self.provider.before_proof=reject_first_profile
        self.m.recover();self.m.start_scheduler();await asyncio.wait_for(self.m.cleanup_recovery_task,3)
        self.assertEqual(['w1','w2'],[p for p,_ in self.provider.checks])
        for ident in (first,second):self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertFalse(json.loads(self.row(third)['result_json'])['window_hold'])

    async def test_unavailable_bridge_defers_all_remaining_startup_checks(self):
        first=await self.seed();second=self.clone(first,'second');third=self.clone(first,'third','w2')
        self.provider.failure=UpstreamUnavailableError('bridge unavailable')
        self.m.recover();self.m.start_scheduler();await self.until(lambda:len(self.provider.checks)==1)
        await asyncio.sleep(.02)
        self.assertFalse(self.m.cleanup_recovery_task.done())
        self.assertEqual(1,len(self.provider.checks))
        for ident in (first,second,third):self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        for _ in range(3):self.m._schedule_ready()
        self.assertEqual(1,len(self.provider.checks))

    async def test_deferred_bridge_recovery_eventually_clears_all_closed_holds(self):
        first=await self.seed();second=self.clone(first,'second');third=self.clone(first,'third','w2')
        self.provider.failure=UpstreamUnavailableError('not ready')
        self.m.cleanup_recovery_retry_delays=(.01,.02,.03)
        self.m.cleanup_recovery_batch_size=2
        self.m.recover();self.m.start_scheduler();await self.until(lambda:len(self.provider.checks)>=2)
        self.assertTrue(json.loads(self.row(first)['result_json'])['window_hold'])
        self.assertEqual({'w1'},set(p for p,_ in self.provider.checks))
        self.provider.failure=None
        await asyncio.wait_for(self.m.cleanup_recovery_task,3)
        for ident in (first,second,third):self.assertFalse(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertEqual([],self.provider.closed)

    async def test_unknown_proof_retries_at_backoff_without_releasing_or_overlapping(self):
        ident=await self.seed();self.provider.proof_override={}
        self.m.cleanup_recovery_retry_delays=(.02,.04,.06)
        self.m.recover();self.m.start_scheduler();await self.until(lambda:len(self.provider.checks)>=3)
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertFalse(self.provider.inside)
        count=len(self.provider.checks)
        await asyncio.sleep(.02);self.assertEqual(count,len(self.provider.checks))
        await self.m.shutdown();count=len(self.provider.checks)
        await asyncio.sleep(.07);self.assertEqual(count,len(self.provider.checks))

    async def test_unsupported_provider_stays_manual_without_background_retries(self):
        ident=await self.seed();self.m.bitbrowser=fixtures.Browser()
        self.m.cleanup_recovery_retry_delays=(.001,)
        self.m.recover();self.m.start_scheduler();await asyncio.wait_for(self.m.cleanup_recovery_task,3)
        self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        with self.assertRaises(UpstreamUnavailableError) as error:
            await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertEqual('closed_profile_guard_unsupported',error.exception.details['reason'])

    async def test_shutdown_drains_active_background_proof_thread(self):
        ident=await self.seed();entered=threading.Event();release=threading.Event()
        def block():
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture watchdog')
        self.provider.before_proof=block
        self.m.recover();self.m.start_scheduler();await self.until(entered.is_set)
        stopping=asyncio.create_task(self.m.shutdown())
        try:
            await asyncio.sleep(.02);self.assertFalse(stopping.done())
            self.assertEqual(1,len(self.provider.checks))
            self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        finally:release.set();await asyncio.wait_for(stopping,3)
        self.assertFalse(self.provider.inside)
        self.assertFalse(json.loads(self.row(ident)['result_json'])['window_hold'])

    async def test_successful_profile_can_reconcile_all_its_historical_rows(self):
        first=await self.seed();second=self.clone(first,'second')
        self.m.recover();self.m.start_scheduler();await asyncio.wait_for(self.m.cleanup_recovery_task,3)
        self.assertEqual(2,len(self.provider.checks))
        for ident in (first,second):self.assertFalse(json.loads(self.row(ident)['result_json'])['window_hold'])

    async def test_just_reconciled_old_archived_row_remains_in_bounded_history(self):
        ident=await self.seed(state='lease_lost',deleted=True)
        with self.db.write() as c:
            c.execute("UPDATE studio_jobs SET created_at='2000-01-01',updated_at='2000-01-02',deleted_at='2001-01-01' WHERE id=?",(ident,))
        before=self.row(ident)
        for i in range(501):
            clone=self.clone(ident,f'history-{i}','other')
            self.m.update(clone,result_json='{}')
            with self.db.write() as c:c.execute("UPDATE studio_jobs SET created_at='2020-01-01' WHERE id=?",(clone,))
        await self.m.control(self.owner,ident,'retry_cleanup')
        jobs=self.m.snapshot(self.owner)['jobs']
        self.assertEqual(500,len(jobs));self.assertEqual(ident,jobs[0]['id'])
        self.assertEqual(1,sum(job['id']==ident for job in jobs))
        after=self.row(ident)
        for key in ('created_at','updated_at','deleted_at'):self.assertEqual(before[key],after[key])

    async def test_provider_io_is_off_loop_and_cancellation_drains_ownership(self):
        ident=await self.seed(state='lease_lost');entered=threading.Event();release=threading.Event()
        def block():
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture watchdog')
        self.provider.before_proof=block
        task=asyncio.create_task(self.m.control(self.owner,ident,'retry_cleanup'))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            task.cancel();await asyncio.sleep(.02);self.assertFalse(task.done())
            self.assertTrue(json.loads(self.row(ident)['result_json'])['window_hold'])
        finally:
            release.set();await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(json.loads(self.row(ident)['result_json'])['window_hold'])
        self.assertFalse(self.provider.inside)

    async def test_ready_posting_blocker_identifies_owned_task_and_actionable_module(self):
        ident=await self.seed(state='lease_lost')
        from app.posting_schema import initialize_posting_schema
        with self.db.write() as c:
            initialize_posting_schema(c)
            c.execute("""INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,profile_id,status,created_at,updated_at)
                VALUES('ready-draft',?,'ready-draft','fixture','fixture','w1','ready','2026-10-04','2026-10-04')""",(self.owner,))
        with self.assertRaises(ConflictError) as error:
            await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertIn('发布任务（待发布，任务编号 ready-draft）',str(error.exception))
        self.assertIn('请到“发布”处理或停止该任务',str(error.exception))
        self.assertEqual('posting',error.exception.details['module'])
        self.assertEqual('ready-draft',error.exception.details['job_id'])
        self.assertEqual([],self.provider.checks)
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='cancelled' WHERE id='ready-draft'")
        self.assertTrue((await self.m.control(self.owner,ident,'retry_cleanup'))['cleanup_reconciled'])

    async def test_foreign_workflow_blocker_does_not_disclose_module_status_or_id(self):
        ident=await self.seed(state='lease_lost');source=self.row(ident)
        with self.db.write() as c:
            c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,config_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('private-other-job',self.other,'foreign-request','nurture','w1','paused',source['config_json'],source['due_at'],source['created_at'],source['updated_at']))
        with self.assertRaises(ConflictError) as error:
            await self.m.control(self.owner,ident,'retry_cleanup')
        self.assertIn('任务所属用户处理后',str(error.exception))
        self.assertNotIn('private-other-job',str(error.exception))
        self.assertEqual({'reason':'unfinished_workflow'},error.exception.details)
        self.assertEqual([],self.provider.checks)


if __name__=='__main__':unittest.main()
