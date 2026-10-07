"""r18 regressions: SQLite and controlled interface returns, no browser/DOM runtime."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.async_cleanup import finish_owned
from app.database import Database
from app.errors import ConflictError
from app.execution_manager import _CombinedStopEvent
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.service import CoreService


class StabilityAsyncTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, batches):
        worker = PlaywrightWorker(SimpleNamespace())
        worker._read_visible_account_hrefs = AsyncMock(side_effect=batches)
        worker._guard = AsyncMock()
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        return worker

    async def test_r29_overlapping_windows_read_all_seventeen_accounts(self):
        names = [f'/account{i}/' for i in range(17)]
        worker = self.worker([names[:6], names[4:10], names[8:14], names[12:]])
        surface = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            {'valid':True,'moved':True,'top':100*(i+1)} for i in range(3)]))
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            result = await worker._read_visible_account_dialog(surface,17,monitor_observation=True,surface_kind='following')
        self.assertEqual([f'account{i}' for i in range(17)],result)
        self.assertEqual(3, surface.evaluate.await_count)
        for call in surface.evaluate.await_args_list:
            self.assertIn('relation-action: advance',call.args[0])
        self.assertEqual(3,worker.last_relation_scroll['movements'])

    async def test_r29_repaint_wait_reads_before_scrolling_again(self):
        worker = self.worker([['/first/'],['/first/'],['/first/','/second/']])
        surface = SimpleNamespace(evaluate=AsyncMock(return_value={'valid':True,'moved':True,'top':100}))
        with patch('app.playwright_worker.asyncio.sleep',new=AsyncMock()):
            result=await worker._read_visible_account_dialog(surface,2,monitor_observation=True,surface_kind='following')
        self.assertEqual(['first','second'],result)
        self.assertEqual(1,surface.evaluate.await_count)

    async def test_r29_invalid_scroller_is_not_success(self):
        worker=self.worker([['/first/']])
        surface=SimpleNamespace(evaluate=AsyncMock(return_value={'valid':False}))
        with self.assertRaises(WorkerExecutionError) as error:
            await worker._read_visible_account_dialog(surface,2,monitor_observation=True,surface_kind='following')
        self.assertEqual('instagram_following_list_not_rendered',error.exception.code)
        self.assertEqual(1,worker.last_relation_scroll['invalid'])

    async def test_r02_bio_is_not_network_failure_but_system_notice_is(self):
        worker=self.worker([])
        page=SimpleNamespace(locator=lambda _:SimpleNamespace(evaluate=AsyncMock(return_value=[])))
        for marker in ('something went wrong','no internet','reload page'):
            self.assertIsNone(await worker._visible_transport_failure(page,'Biography: '+marker+' is my new song'))
        self.assertEqual('instagram_network_unavailable',await worker._visible_transport_failure(page,'No internet'))
        system=SimpleNamespace(locator=lambda _:SimpleNamespace(evaluate=AsyncMock(return_value=['Something went wrong'])))
        self.assertEqual('instagram_profile_not_ready',await worker._visible_transport_failure(system,'A loaded profile and a system alert'))

    async def test_r04_repeated_cancel_waits_for_owned_cleanup(self):
        entered=asyncio.Event();release=asyncio.Event();cleaned=[]
        async def cleanup():
            entered.set();await release.wait();cleaned.append(True)
        task=asyncio.create_task(finish_owned(cleanup()))
        await entered.wait();task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(0)
        self.assertFalse(task.done());self.assertFalse(cleaned)
        release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual([True],cleaned)

    async def test_r23_cancelled_wait_leaves_no_stop_watchers(self):
        first=asyncio.Event();second=asyncio.Event();combined=_CombinedStopEvent(first,second)
        for _ in range(20):
            with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(combined.wait(),.001)
        self.assertFalse(first._waiters);self.assertFalse(second._waiters)

    async def test_r01_abandoned_worker_waits_for_old_operations(self):
        worker=self.worker([]);worker._page_stage_abandoned=True
        release=asyncio.Event();old=asyncio.create_task(release.wait());worker._track_late_lifecycle_task(old)
        worker._connect_impl=AsyncMock()
        with self.assertRaises(WorkerExecutionError):await worker.connect('window-one')
        worker._connect_impl.assert_not_awaited()
        release.set();await old
        await worker.connect('window-one')
        self.assertFalse(worker._page_stage_abandoned)
        worker._connect_impl.assert_awaited_once()

    async def test_r07_privacy_cache_is_bounded_without_successful_profile_reads(self):
        worker=self.worker([])
        for i in range(1000):worker._remember_privacy(f'account{i}',True)
        self.assertEqual(512,len(worker._profile_privacy_cache))
        self.assertNotIn('account0',worker._profile_privacy_cache)

    async def test_r08_child_cleanup_keeps_parent_notification_session(self):
        worker=self.worker([]);notification=SimpleNamespace(detach=AsyncMock())
        context=SimpleNamespace(_juxin_notifications_session=notification)
        worker._context=context;worker._browser=None
        await worker.disconnect()
        notification.detach.assert_not_awaited()
        self.assertIs(notification,context._juxin_notifications_session)

    async def test_r29_failed_scroll_retries_once_in_new_page_and_confirms_progress(self):
        from app.follow_monitor import FollowMonitorManager
        worker=SimpleNamespace(
            collect_following=AsyncMock(side_effect=[WorkerExecutionError('missing scroller',reason='instagram_following_list_not_rendered'),SimpleNamespace(usernames=['one','two'],source_total=2)]),
            _replace_stuck_page_once=AsyncMock(),_finish_page_recovery=AsyncMock())
        members,metrics=await FollowMonitorManager._read_following_round(None,worker,'owner')
        self.assertEqual({'one','two'},members);self.assertEqual(2,metrics['source_total'])
        worker._replace_stuck_page_once.assert_awaited_once_with('owner')
        worker._finish_page_recovery.assert_awaited_once_with(progressed=True)


class StabilityDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'r18.sqlite');self.db.initialize()
        self.service=CoreService(self.db);self.owner=self.service.register_user('r18-owner','test password sufficiently long')['id']
    def tearDown(self):self.tmp.cleanup()

    def test_r28_clock_jump_does_not_transfer_live_lease(self):
        token=self.service.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='monitor-one')
        with self.db.write() as c:c.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(ConflictError):self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='another')
        self.service.release_browser_lease('w1',token)
        other=self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='another')
        self.assertNotEqual(token,other)

    def test_r12_old_delete_keeps_recoverable_target(self):
        task=self.service.create_task(self.owner,name='history',modes=['followers'],targets=['source_one'],window_ids=['w1'],settings={})
        target=task['targets'][0]['id']
        self.service.delete_history_target(self.owner,target)
        with self.db.read() as c:
            self.assertEqual('stopped',c.execute('SELECT status FROM task_targets WHERE id=?',(target,)).fetchone()[0])
        self.service.delete_task(self.owner,task['id'])
        with self.db.read() as c:
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM task_targets WHERE id=?',(target,)).fetchone()[0])
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM task_list_dismissals WHERE task_id=?',(task['id'],)).fetchone()[0])

    def test_r19_reordered_new_rows_are_replayed_to_durable_dedupe(self):
        self.assertEqual(['new_entry','old_a','old_b'],PlaywrightWorker._names_after_resume_tail(['new_entry','old_a','old_b'],('old_a','old_b')))

import hashlib
import json
import threading
from app.cloud_workspace import CloudWorkspace, export_workspace, import_workspace, decode_workspace
from app.instagram_nurture import NurtureInteractions
from app.studio_worker import ResultUncertain
import test_studio as studio_fixtures
import test_native_cloud as cloud_fixtures


class StabilityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    setUp=studio_fixtures.StudioTests.setUp
    tearDown=studio_fixtures.StudioTests.tearDown
    start=studio_fixtures.StudioTests.start
    execute=studio_fixtures.StudioTests.execute

    async def asyncTearDown(self):await self.m.shutdown()

    async def test_r16_threaded_close_retains_lock_despite_repeated_cancel(self):
        ident=(await self.start(kind='nurture',config={'minutes':1,'dwell_min':60,'dwell_max':60}))['job_ids'][0]
        entered=threading.Event();release=threading.Event()
        def close(profile):entered.set();release.wait(4)
        self.browser.close_profile=close
        with patch('app.studio.PlaywrightWorker',studio_fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',new=AsyncMock(return_value={'browse':1})):
            task=asyncio.create_task(self.execute(ident))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():await asyncio.sleep(.002)
                task.cancel();await asyncio.sleep(0);task.cancel();await asyncio.sleep(0)
                self.assertFalse(task.done())
                with self.assertRaises(ConflictError):self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='other')
            finally:
                release.set();await asyncio.gather(task,return_exceptions=True)
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])

    async def test_r18_unknown_action_holds_window_blocks_next_round_and_can_be_resolved(self):
        # New starts deliberately create one fixed-policy job per window. Seed
        # two persisted jobs to retain the old uncertain-action queue fence test.
        from test_nurture_scheduler import NurtureSchedulerTests
        ids=await NurtureSchedulerTests.create_persisted_queue(self,['w1'],rounds=2)
        async def step(browser,*args):
            await browser.nurture_action_begin('comment:/p/known',{'action':'comment','target':'/p/known','state':'pending'})
            raise ResultUncertain('confirmation unavailable')
        with patch('app.studio.PlaywrightWorker',studio_fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ids[0])
        self.assertEqual('needs_review',self.m.get(self.owner,ids[0])['status'])
        self.assertEqual(1,self.m.daily_action_counts(self.owner,'w1')['comment'])
        self.m._schedule_ready();self.assertNotIn(ids[1],self.m.active_ids())
        with self.assertRaises(ConflictError):self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='other')
        await self.m.control(self.owner,ids[0],'confirm_actions')
        self.assertEqual(1,self.m.daily_action_counts(self.owner,'w1')['comment'])
        self.assertEqual('paused',self.m.get(self.owner,ids[0])['status'])
        await self.m.control(self.owner,ids[0],'cancel')
        token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='other')
        self.s.release_browser_lease('w1',token)

    async def test_r20_confirmed_action_persists_before_later_failure_and_skips_retry(self):
        ident=(await self.start(kind='nurture',config={'minutes':1,'dwell_min':60,'dwell_max':60}))['job_ids'][0]
        async def step(browser,*args):
            await browser.nurture_action_begin('comment:/p/known',{'action':'comment','target':'/p/known','state':'pending'})
            await browser.nurture_action_confirmed('comment:/p/known',{'comment':1})
            raise ValueError('later scroll failed')
        with patch('app.studio.PlaywrightWorker',studio_fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ident)
        row=self.m.get(self.owner,ident);self.assertEqual('failed',row['status'])
        journal=json.loads(row['result_json'])['nurture_actions']
        browser=SimpleNamespace(nurture_actions=journal,nurture_observation=AsyncMock(),nurture_action_begin=AsyncMock())
        self.assertFalse(await NurtureInteractions(browser).begin('comment','/p/known/'))
        browser.nurture_action_begin.assert_not_awaited()
        self.assertEqual(1,self.m.daily_action_counts(self.owner,'w1')['comment'])

    async def test_r27_old_paused_round_remains_visible_after_501_completed_jobs(self):
        ident=(await self.start(kind='nurture',config={'minutes':1}))['job_ids'][0]
        self.m.update(ident,status='paused')
        with self.db.write() as c:
            original=dict(c.execute('SELECT * FROM studio_jobs WHERE id=?',(ident,)).fetchone());keys=list(original)
            for n in range(501):
                row={**original,'id':f'history-{n}','request_key':f'history-request-{n}','status':'completed'}
                c.execute(f'INSERT INTO studio_jobs ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',[row[k] for k in keys])
        snapshot=self.m.snapshot(self.owner)
        self.assertIn(ident,[j['id'] for j in snapshot['jobs']]);self.assertEqual(501,len(snapshot['jobs']))

    async def test_r13_r15_r17_restore_spool_and_old_nurture_job(self):
        ident=(await self.start(kind='nurture',config={'minutes':1}))['job_ids'][0]
        task=self.s.create_task(self.owner,name='spool',targets=['source'],modes=['followers'],settings={})
        self.s.append_task_mode_candidates(self.owner,task['id'],task['targets'][0]['id'],'followers',['alpha','beta'])
        payload,_=export_workspace(self.db,self.owner,self.tmp.name)
        payload=cloud_fixtures.NativeCloudTests.rewrite(self,payload,lambda data:[row.pop(key,None) for row in data['tables']['studio_jobs'] for key in ('deleted_at','source_draft_id','draft_target_profile_id')])
        dest=Database(Path(self.tmp.name)/'restore.sqlite');dest.initialize();s=CoreService(dest);owner=s.register_user('restore-owner','valid password for restore')['id']
        root=Path(self.tmp.name)/'restore'
        import_workspace(dest,owner,root,payload)
        with dest.read() as c:
            self.assertEqual(2,c.execute('SELECT COUNT(*) FROM task_mode_candidates').fetchone()[0])
            self.assertEqual(2,c.execute('SELECT pending FROM task_mode_candidate_counters').fetchone()[0])
            self.assertEqual('',c.execute('SELECT source_draft_id FROM studio_jobs WHERE id=?',(ident,)).fetchone()[0])
            self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())

    async def test_r14_shutdown_does_not_wait_on_cloud_network_lock(self):
        api=cloud_fixtures.MemoryCloud();cloud=CloudWorkspace(self.s,self.tmp.name,api)
        cloud.command(self.owner,{'action':'login','email':'owner@example.test','password':'test-password'})
        entered=threading.Event();release=threading.Event();errors=[];original=api.request
        def request(path,**kwargs):
            if path.startswith('/rest/'):
                entered.set();release.wait(3)
            return original(path,**kwargs)
        api.request=request
        def sync():
            try:cloud.sync(self.owner)
            except InterruptedError:errors.append('stopped')
        thread=threading.Thread(target=sync);thread.start()
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():await asyncio.sleep(.002)
            await asyncio.wait_for(asyncio.to_thread(cloud.shutdown),.3)
        finally:release.set();await asyncio.to_thread(thread.join,2)
        self.assertEqual(['stopped'],errors);self.assertIsNone(api.remote)
