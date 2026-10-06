from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.errors import ConflictError, NotFoundError, ValidationError
from app.follow_monitor import FollowMonitorManager
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError, following_row_button_state, _MONITOR_FOLLOWING_ROWS_SCRIPT
from app.schemas import FollowMonitorStartRequest
from app.service import CoreService


class Browser:
    def __init__(self, raises=False):
        self.closed = []
        self.raises = raises
    def close_profile(self, profile_id):
        self.closed.append(profile_id)
        if self.raises:
            self.raises = False
            raise RuntimeError('temporary close failure')


class Worker:
    def __init__(self, browser):
        self.page = SimpleNamespace()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.collect_following = AsyncMock(return_value=SimpleNamespace(usernames=['alpha'],source_total=1))


class FollowMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'monitor.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('monitor-owner','correct horse battery staple')['id']
        self.browser = Browser()
        self.manager = FollowMonitorManager(self.service,self.browser)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, run_id, names, *, total=500, profile='w1', identity='ig1'):
        # These persistence tests explicitly supply a verified complete fixture;
        # live completeness is exercised through _scan_following_stage below.
        return self.manager._commit_following_scan(self.owner,run_id,profile,identity,'owner_one',set(names),source_total=total,snapshot_complete=True)

    def test_four_rounds_keep_490_and_identify_first_repeat_and_removed(self):
        fixed = {f'fixed_{i}' for i in range(488)}
        self.assertEqual((0,0,0), self.commit('r1',fixed|{'a','b'}))
        self.assertEqual((1,1,0), self.commit('r2',fixed|{'b','c'}))
        self.assertEqual((1,1,0), self.commit('r3',fixed|{'b','d'}))
        self.assertEqual((2,2,1), self.commit('r4',fixed|{'c','e'}))
        snapshot = self.manager.snapshot(self.owner)
        fourth = {r['username']:r['result_kind'] for r in snapshot['latest_follow'] if r['batch_id']=='r4'}
        self.assertEqual({'c':'repeat_following','e':'new_following'},fourth)
        removed = {r['username'] for r in snapshot['latest_unfollow'] if r['batch_id']=='r4'}
        self.assertEqual({'b','d'},removed)
        account = snapshot['accounts'][0]
        self.assertEqual((500,490,490,4,1,4),(account['homepage_count'],account['following_count'],account['previous_following_count'],account['total_added_count'],account['total_repeat_count'],account['total_unfollow_count']))
        self.assertEqual({'total':4,'month':4,'week':4,'today':4,'repeated':1},snapshot['counts'])
        with self.db.read() as c:
            rows = c.execute('SELECT * FROM follow_monitor_rounds ORDER BY batch_id').fetchall()
            self.assertEqual([10]*4,[row['homepage_count']-row['actual_count'] for row in rows])
            self.assertIsNone(rows[0]['previous_actual_count'])
            self.assertEqual(493,c.execute('SELECT count(*) FROM follow_monitor_seen').fetchone()[0])
            self.assertEqual([3,4],[row[0] for row in c.execute('SELECT DISTINCT generation FROM follow_monitor_members ORDER BY generation')])

    def test_baseline_member_reappearing_is_repeat_and_missing_first_scan_is_new(self):
        self.commit('r1',{'a'})
        self.commit('r2',{'b'})
        self.assertEqual((2,1,1),self.commit('r3',{'a','previously_unread'}))
        events = {r['username']:r['result_kind'] for r in self.manager.snapshot(self.owner)['latest_follow'] if r['batch_id']=='r3'}
        self.assertEqual('repeat_following',events['a'])
        self.assertEqual('new_following',events['previously_unread'])

    def test_commit_is_idempotent(self):
        self.commit('r1',{'a'})
        self.assertEqual((1,0,0),self.commit('r2',{'a','b'}))
        self.assertEqual((1,0,0),self.commit('r2',{'a','b'}))
        self.assertEqual(1,self.manager.snapshot(self.owner)['counts']['total'])
        self.assertEqual(2,self.manager.snapshot(self.owner)['accounts'][0]['generation'])

    def test_identity_profile_and_owner_are_isolated(self):
        self.commit('r1',{'a'})
        self.commit('r2',{'b'})
        self.assertEqual((0,0,0),self.commit('r3',{'a'},identity='ig2'))
        self.assertEqual((1,1,0),self.commit('r4',{'b'},identity='ig2'))
        self.assertEqual((0,0,0),self.commit('r5',{'a'},profile='w2'))
        self.assertEqual((1,1,0),self.commit('r6',{'b'},profile='w2'))
        other = self.service.register_user('another','correct horse battery staple')['id']
        self.assertEqual([],self.manager.snapshot(other)['accounts'])
        self.manager._commit_following_scan(other,'r7','w1','ig1','owner_one',{'a'},source_total=1)
        self.assertEqual((1,1,0),self.manager._commit_following_scan(other,'r8','w1','ig1','owner_one',{'b'},source_total=1))

    def test_empty_baseline_and_case_normalization(self):
        self.commit('r1',set(),total=0)
        self.assertEqual((1,0,0),self.commit('r2',{' Alpha ','alpha','owner_one'},total=1))
        self.assertEqual(1,self.manager.snapshot(self.owner)['accounts'][0]['following_count'])

    def test_upgrade_preserves_counts_and_seeds_existing_baseline_once(self):
        self.commit('r1',{'a'})
        self.commit('r2',{'a','b'})
        with self.db.write() as c:
            c.execute('DELETE FROM schema_migrations WHERE version >= 17')
            c.execute('DELETE FROM follow_monitor_seen')
            c.execute('UPDATE follow_monitor_accounts SET total_added_count=75, total_dm_count=6, baseline_verified=0')
        self.db.initialize()
        self.db.initialize()
        account = self.manager.snapshot(self.owner)['accounts'][0]
        self.assertEqual(75,account['total_added_count'])
        self.assertEqual(2,account['generation'])
        self.assertEqual(6,self.manager.snapshot(self.owner)['dm_accounts'][0]['total_dm_count'])
        self.commit('r3',{'b'})
        self.assertEqual((1,0,1),self.commit('r4',{'a','b'}))
        self.assertEqual(76,self.manager.snapshot(self.owner)['accounts'][0]['total_added_count'])
        with self.db.read() as c:
            self.assertEqual(set(range(1,42)),{row[0] for row in c.execute('SELECT version FROM schema_migrations')})

    def test_follow_and_dm_commits_do_not_mutate_each_other(self):
        self.commit('f1',{'a'})
        before = self.manager.snapshot(self.owner)['accounts']
        dms = [{'thread_id':'t1','sender':'A','preview':'hello'},{'thread_id':'t1','sender':'A','preview':'hello'}]
        self.assertEqual(1,self.manager._commit_dm_scan(self.owner,'dm1','w1','ig1','owner_one',dms))
        self.assertEqual(1,self.manager._commit_dm_scan(self.owner,'dm1','w1','ig1','owner_one',dms))
        self.assertEqual(before,self.manager.snapshot(self.owner)['accounts'])
        dm_before = self.manager.snapshot(self.owner)['latest_dm']
        self.commit('f2',{'b'})
        self.assertEqual(dm_before,self.manager.snapshot(self.owner)['latest_dm'])
        self.assertEqual(1,self.manager.snapshot(self.owner)['dm_accounts'][0]['total_dm_count'])

    def test_every_short_read_gets_at_most_one_supplemental_attempt(self):
        async def exercise():
            for total,count,expected in [(150,149,2),(150,148,2),(100,99,2),(100,98,2),(500,495,2),(500,490,2),(101,99,2),(0,0,1),(None,3,1)]:
                with self.subTest(total=total,count=count):
                    w=Worker(None)
                    w.collect_following.return_value=SimpleNamespace(usernames=[f'a{i}' for i in range(count)],source_total=total)
                    members,metrics=await self.manager._read_following_round(w,'owner_one')
                    self.assertEqual(expected,w.collect_following.await_count)
                    self.assertEqual(count,len(members))
                    self.assertEqual(count,metrics['first_read_count'])
                    for call in w.collect_following.await_args_list:
                        self.assertTrue(call.kwargs['monitor_observation'])
                        self.assertIsNone(call.kwargs['limit'])
        asyncio.run(exercise())

    def test_second_scan_unions_only_this_round_and_never_reads_third_time(self):
        async def exercise():
            self.commit('old',{'old_account'})
            first={f'a{i}' for i in range(148)}
            second=first-{'a0'}|{'a148'}
            w=Worker(None)
            w.collect_following.side_effect=[SimpleNamespace(usernames=list(first),source_total=150),SimpleNamespace(usernames=list(second),source_total=150)]
            members,metrics=await self.manager._read_following_round(w,'owner_one')
            self.assertEqual(first|second,members)
            self.assertNotIn('old_account',members)
            self.assertEqual(149,len(members))
            self.assertEqual(148,metrics['second_read_count'])
            self.assertEqual(2,w.collect_following.await_count)
            w.collect_following=AsyncMock(return_value=SimpleNamespace(usernames=['only_one'],source_total=150))
            members,metrics=await self.manager._read_following_round(w,'owner_one')
            self.assertEqual({'only_one'},members)
            self.assertEqual(2,w.collect_following.await_count)
        asyncio.run(exercise())

    def test_second_scan_can_complete_list_and_records_changing_header(self):
        async def exercise():
            first={f'a{i}' for i in range(148)}
            second=first-{'a0','a1'}|{'a148','a149'}
            w=Worker(None)
            w.collect_following.side_effect=[SimpleNamespace(usernames=list(first),source_total=150),SimpleNamespace(usernames=list(second),source_total=151)]
            members,metrics=await self.manager._read_following_round(w,'owner_one')
            self.assertEqual(150,len(members))
            self.assertEqual(150,metrics['source_total'])
            self.assertEqual(151,metrics['second_homepage_count'])
        asyncio.run(exercise())

    def test_growing_header_saves_observation_without_guessing_unfollows(self):
        async def exercise():
            original = {f'a{i}' for i in range(200)}
            self.commit('seed', original, total=200)
            before = self.manager.snapshot(self.owner)['accounts']
            worker = Worker(None)
            worker.collect_following.side_effect = [
                SimpleNamespace(usernames=[f'a{i}' for i in range(148)], source_total=150),
                SimpleNamespace(usernames=[f'a{i}' for i in range(150)], source_total=200),
            ]
            self.assertEqual((0, 0, 0), await self.manager._scan_following_stage(worker, self.owner, 'r2', 'w1', 'ig1', 'owner_one'))
            account = self.manager.snapshot(self.owner)['accounts'][0]
            self.assertEqual(150, account['following_count'])
            self.assertEqual(0, account['baseline_verified'])
            self.assertEqual(2, worker.collect_following.await_count)
            with self.db.read() as connection:
                self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM follow_monitor_rounds WHERE batch_id=?', ('r2',)).fetchone()[0])
                generation = connection.execute('SELECT generation FROM follow_monitor_accounts').fetchone()[0]
                self.assertEqual(200, connection.execute('SELECT COUNT(*) FROM follow_monitor_members WHERE generation=?', (generation,)).fetchone()[0])
                self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM follow_monitor_latest_unfollow WHERE batch_id=?', ('r2',)).fetchone()[0])
        asyncio.run(exercise())

    def test_complete_merged_read_with_growing_header_can_still_commit(self):
        async def exercise():
            original = {f'a{i}' for i in range(151)}
            self.commit('seed', original, total=151)
            worker = Worker(None)
            worker.collect_following.side_effect = [
                SimpleNamespace(usernames=[f'a{i}' for i in range(148)], source_total=150),
                SimpleNamespace(usernames=sorted(original), source_total=151),
            ]
            self.assertEqual((0, 0, 0), await self.manager._scan_following_stage(worker, self.owner, 'r2', 'w1', 'ig1', 'owner_one'))
            self.assertEqual(2, self.manager.snapshot(self.owner)['accounts'][0]['generation'])
            with self.db.read() as connection:
                row = connection.execute('SELECT homepage_count, second_homepage_count, actual_count FROM follow_monitor_rounds WHERE batch_id=?', ('r2',)).fetchone()
                self.assertEqual((150, 151, 151), tuple(row))
        asyncio.run(exercise())

    def test_failed_first_scroll_preserves_valid_rows_for_one_supplemental_read(self):
        async def exercise():
            names = [f'a{i}' for i in range(17)]
            self.commit('seed', set(names), total=17)
            worker = Worker(None)
            worker.last_relation_partial_usernames = names[:10]
            worker.last_relation_source_total = 17
            worker._replace_stuck_page_once = AsyncMock()
            worker._finish_page_recovery = AsyncMock()
            worker.collect_following.side_effect = [
                WorkerExecutionError('scroller stopped after valid rows', reason='instagram_following_list_not_rendered'),
                SimpleNamespace(usernames=names[5:], source_total=17),
            ]
            self.assertEqual((0, 0, 0), await self.manager._scan_following_stage(worker, self.owner, 'r2', 'w1', 'ig1', 'owner_one'))
            self.assertEqual(17, self.manager.snapshot(self.owner)['accounts'][0]['following_count'])
            worker._replace_stuck_page_once.assert_awaited_once_with('owner_one')
            worker._finish_page_recovery.assert_awaited_once_with(progressed=True)
            with self.db.read() as connection:
                row = connection.execute('SELECT first_read_count, second_read_count, actual_count FROM follow_monitor_rounds WHERE batch_id=?', ('r2',)).fetchone()
                self.assertEqual((10, 12, 17), tuple(row))
        asyncio.run(exercise())

    def test_failed_supplemental_scroll_keeps_observed_rows_and_old_members(self):
        async def exercise():
            self.commit('seed', {f'a{i}' for i in range(17)}, total=17)
            before = self.manager.snapshot(self.owner)['accounts']
            worker = Worker(None)
            worker.last_relation_partial_usernames = [f'a{i}' for i in range(10)]
            worker.last_relation_source_total = 17
            worker._replace_stuck_page_once = AsyncMock()
            worker._finish_page_recovery = AsyncMock()
            worker.collect_following.side_effect = WorkerExecutionError('scroller stopped', reason='instagram_following_list_incomplete')
            self.assertEqual((0, 0, 0), await self.manager._scan_following_stage(worker, self.owner, 'r2', 'w1', 'ig1', 'owner_one'))
            account = self.manager.snapshot(self.owner)['accounts'][0]
            self.assertEqual((10, 0, 2), (account['following_count'], account['baseline_verified'], account['generation']))
            with self.db.read() as connection:
                self.assertEqual(17, connection.execute('SELECT COUNT(*) FROM follow_monitor_members WHERE generation=2').fetchone()[0])
            self.assertEqual(2, worker.collect_following.await_count)
            worker._finish_page_recovery.assert_awaited_once_with(progressed=False)
        asyncio.run(exercise())

    def test_pause_at_first_read_boundary_holds_before_opening_supplemental_page(self):
        async def exercise():
            entered = asyncio.Event()
            resume = asyncio.Event()
            async def checkpoint():
                entered.set()
                await resume.wait()
            worker = Worker(None)
            worker.monitor_checkpoint = checkpoint
            worker._replace_stuck_page_once = AsyncMock()
            worker._finish_page_recovery = AsyncMock()
            worker.collect_following.side_effect = [
                SimpleNamespace(usernames=['a'], source_total=2),
                SimpleNamespace(usernames=['a', 'b'], source_total=2),
            ]
            task = asyncio.create_task(self.manager._read_following_round(worker, 'owner_one'))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                worker._replace_stuck_page_once.assert_not_awaited()
                self.assertEqual(1, worker.collect_following.await_count)
                resume.set()
                members, _ = await task
                self.assertEqual({'a', 'b'}, members)
                worker._replace_stuck_page_once.assert_awaited_once_with('owner_one')
            finally:
                resume.set()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(exercise())

    def test_follow_scan_skips_dm_and_retries_transient_close_failure(self):
        async def exercise():
            self.browser.raises=True
            w=Worker(None)
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock(side_effect=AssertionError('must not open DMs'))
            with patch('app.follow_monitor.PlaywrightWorker',return_value=w):
                self.assertEqual((0,0,0,0),await self.manager._scan_profile(self.owner,'r1','w1'))
            self.manager._read_pending_dms.assert_not_awaited()
            w.disconnect.assert_awaited_once()
            self.assertEqual(['w1','w1'],self.browser.closed)
            self.assertEqual([],self.service.list_browser_lease_states(self.owner,active_collection_entity_ids=set(),active_action_entity_ids=set(),active_monitor_entity_ids=set(),inactive_grace_seconds=0))
        asyncio.run(exercise())

    def test_dm_scan_skips_following_and_closes_window(self):
        async def exercise():
            w=Worker(None)
            w.collect_following.side_effect=AssertionError('must not read following')
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock(return_value=[{'thread_id':'t1','sender':'a','preview':'hello'}])
            with patch('app.follow_monitor.PlaywrightWorker',return_value=w):
                self.assertEqual((0,0,1,0),await self.manager._scan_profile(self.owner,'d1','w1','dm'))
            w.collect_following.assert_not_awaited()
            self.assertEqual([],self.manager.snapshot(self.owner)['accounts'])
            self.assertEqual(['w1'],self.browser.closed)
        asyncio.run(exercise())

    def test_failure_preserves_saved_observation_and_window(self):
        async def exercise():
            self.commit('r1',{'a','b'})
            before=self.manager.snapshot(self.owner)['accounts']
            w=Worker(None);w.collect_following.side_effect=RuntimeError('page closed')
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            with patch('app.follow_monitor.PlaywrightWorker',return_value=w):
                with self.assertRaisesRegex(RuntimeError,'page closed'):
                    await self.manager._scan_profile(self.owner,'r2','w1')
            self.assertEqual(before,self.manager.snapshot(self.owner)['accounts'])
            self.assertEqual([],self.browser.closed)
        asyncio.run(exercise())

    def test_separate_runs_can_overlap_but_same_kind_conflicts(self):
        async def exercise():
            block=asyncio.Event()
            async def scan(*args):
                await block.wait()
                return (0,0,0,0)
            self.manager._scan_profile=scan
            f=await self.manager.start(self.owner,['w1'],check_kind='following')
            d=await self.manager.start(self.owner,['w2'],check_kind='dm')
            with self.assertRaises(ConflictError):
                await self.manager.start(self.owner,['w3'],check_kind='following')
            snapshot=self.manager.snapshot(self.owner)
            self.assertEqual('running',snapshot['runs']['following']['status'])
            self.assertEqual('running',snapshot['runs']['dm']['status'])
            tasks=list(self.manager._tasks.values());block.set();await asyncio.gather(*tasks)
            self.assertEqual('completed',self.manager.snapshot(self.owner)['runs']['following']['status'])
            self.assertEqual('completed',self.manager.snapshot(self.owner)['runs']['dm']['status'])
            self.assertEqual(set(),self.manager.active_run_ids())
        asyncio.run(exercise())

    def test_each_kind_clears_only_its_own_results_and_failed_run_status(self):
        async def exercise():
            self.commit('f1',{'a'});self.commit('f2',{'b'})
            self.manager._commit_dm_scan(self.owner,'d1','w1','ig1','owner_one',[{'thread_id':'t1'}])
            follow=self.manager.snapshot(self.owner)['latest_follow']
            self.manager._scan_profile=AsyncMock(side_effect=RuntimeError('not logged in'))
            await self.manager.start(self.owner,['w1'],check_kind='dm')
            await asyncio.gather(*list(self.manager._tasks.values()))
            state=self.manager.snapshot(self.owner)
            self.assertEqual(follow,state['latest_follow'])
            self.assertEqual([],state['latest_dm'])
            self.assertEqual('failed',state['runs']['dm']['status'])
            self.assertEqual(1,state['runs']['dm']['failed'])
            self.assertEqual(2,state['accounts'][0]['generation'])
            self.assertEqual('completed',state['accounts'][0]['last_status'])
            self.manager._commit_dm_scan(self.owner,'d2','w1','ig1','owner_one',[{'thread_id':'t2'}])
            dm=self.manager.snapshot(self.owner)['latest_dm']
            await self.manager.start(self.owner,['w1'],check_kind='following')
            await asyncio.gather(*list(self.manager._tasks.values()))
            state=self.manager.snapshot(self.owner)
            self.assertEqual(dm,state['latest_dm'])
            self.assertEqual([],state['latest_follow'])
            self.assertEqual([],state['rounds'])
        asyncio.run(exercise())

    def test_same_window_lease_prevents_second_check_without_closing_first(self):
        async def exercise():
            lease=self.service.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='first')
            try:
                with self.assertRaises(ConflictError):
                    await self.manager._scan_profile(self.owner,'second','w1','dm')
                self.assertEqual([],self.browser.closed)
            finally:
                self.service.release_browser_lease('w1',lease)
        asyncio.run(exercise())

    def test_monitor_worker_does_not_hide_extra_retries_in_recovery(self):
        async def exercise():
            w=PlaywrightWorker(object())
            w._collect_relation_once=AsyncMock(side_effect=WorkerExecutionError('not rendered',reason='instagram_following_list_not_rendered'))
            w._replace_stuck_page_once=AsyncMock()
            with self.assertRaises(WorkerExecutionError):
                await w.collect_following('owner_one',limit=None,monitor_observation=True)
            w._collect_relation_once.assert_awaited_once()
            w._replace_stuck_page_once.assert_not_awaited()
        asyncio.run(exercise())

    def test_each_open_waits_then_resets_top_before_first_read(self):
        async def exercise():
            events=[]
            worker=PlaywrightWorker(object())
            worker._navigate_profile=AsyncMock()
            worker._visible_relation_count=AsyncMock(return_value=1)
            worker._guard=AsyncMock()
            async def reset(script):
                self.assertIn('scrollTop = 0',script)
                events.append('top')
            surface=SimpleNamespace(evaluate=AsyncMock(side_effect=reset))
            async def opened(*args):
                events.append('open')
                return surface
            async def read(*args,**kwargs):
                events.append('read')
                return ['alpha']
            async def delay(seconds):
                events.append(('wait',seconds))
            worker._open_relation_surface=opened
            worker._read_visible_account_dialog=AsyncMock(side_effect=read)
            with patch('app.playwright_worker.asyncio.sleep',side_effect=delay):
                for _ in range(2):
                    outcome=await worker.collect_following('owner_one',limit=None,monitor_observation=True)
                    self.assertEqual(['alpha'],outcome.usernames)
            expected=['open',('wait',3.0),'top',('wait',worker.collection_poll_interval_seconds),'read']
            self.assertEqual(expected*2,events)
        asyncio.run(exercise())

    def test_loading_first_batch_never_scrolls_past_initial_accounts(self):
        async def exercise():
            worker=PlaywrightWorker(object())
            worker._guard=AsyncMock()
            worker._read_visible_account_hrefs=AsyncMock(side_effect=[[],['/owner_one/','/accounts/'],['/alpha/','/bravo/']])
            surface=SimpleNamespace(evaluate=AsyncMock())
            with patch('app.playwright_worker.asyncio.sleep',new_callable=AsyncMock) as delay:
                names=await worker._read_visible_account_dialog(surface,2,exclude={'owner_one'},surface_kind='following',monitor_observation=True)
            self.assertEqual(['alpha','bravo'],names)
            self.assertEqual(2,delay.await_count)
            surface.evaluate.assert_not_awaited()
        asyncio.run(exercise())

    def test_loaded_first_batch_keeps_original_scroll_speed(self):
        async def exercise():
            worker=PlaywrightWorker(object())
            worker._guard=AsyncMock()
            worker._read_visible_account_hrefs=AsyncMock(side_effect=[['/alpha/'],['/alpha/','/bravo/']])
            surface=SimpleNamespace(evaluate=AsyncMock())
            with patch('app.playwright_worker.asyncio.sleep',new_callable=AsyncMock) as delay:
                names=await worker._read_visible_account_dialog(surface,2,surface_kind='following',monitor_observation=True)
            self.assertEqual(['alpha','bravo'],names)
            surface.evaluate.assert_awaited_once()
            self.assertIn('relation-action: advance',surface.evaluate.await_args.args[0])
            delay.assert_awaited_once_with(worker.collection_poll_interval_seconds)
        asyncio.run(exercise())

    def test_first_batch_timeout_fails_without_scrolling_or_empty_result(self):
        async def exercise():
            worker=PlaywrightWorker(object())
            worker._guard=AsyncMock()
            worker._read_visible_account_hrefs=AsyncMock(return_value=[])
            worker._relation_surface_failure=AsyncMock(return_value=None)
            worker.collection_loading_grace_seconds=1.0
            elapsed=0.0
            worker._collection_monotonic=lambda: elapsed
            async def delay(seconds):
                nonlocal elapsed
                elapsed+=seconds
            surface=SimpleNamespace(evaluate=AsyncMock(),inner_text=AsyncMock(return_value='Loading...'))
            with patch('app.playwright_worker.asyncio.sleep',side_effect=delay):
                with self.assertRaises(WorkerExecutionError) as failure:
                    await worker._read_visible_account_dialog(surface,None,surface_kind='following',monitor_observation=True)
            self.assertEqual('instagram_following_list_not_rendered',failure.exception.code)
            self.assertGreaterEqual(elapsed,1.0)
            self.assertLess(elapsed,1.0+worker.collection_poll_interval_seconds)
            surface.evaluate.assert_not_awaited()
        asyncio.run(exercise())

    def test_api_schema_defaults_to_combined_and_rejects_unrelated_modes(self):
        self.assertEqual('combined',FollowMonitorStartRequest(profile_ids=['w1']).check_kind)
        self.assertEqual('dm',FollowMonitorStartRequest(profile_ids=['w1'],check_kind='dm').check_kind)
        with self.assertRaises(ValueError):
            FollowMonitorStartRequest(profile_ids=['w1'],check_kind='followers')

    def test_combined_run_commits_successful_stages_and_reports_partial_failures(self):
        async def exercise(follow_fails, dm_fails):
            profile=f'w-{int(follow_fails)}-{int(dm_fails)}'
            self.commit(f'seed-{profile}',{'alpha'},total=1,profile=profile)
            self.manager._commit_dm_scan(self.owner,f'dm-seed-{profile}',profile,'ig1','owner_one',[{'thread_id':'old'}])
            worker=Worker(None)
            events=[]
            async def following(*args,**kwargs):
                events.append('following')
                if follow_fails:
                    raise RuntimeError('following did not load')
                return SimpleNamespace(usernames=['alpha','bravo'],source_total=2)
            async def dms(actual_worker):
                self.assertIs(worker,actual_worker)
                # No close/reconnect between the stages; following is already committed.
                worker.disconnect.assert_not_awaited()
                self.assertNotIn(profile,self.browser.closed)
                account=next(a for a in self.manager.snapshot(self.owner)['accounts'] if a['profile_id']==profile)
                self.assertEqual(1 if follow_fails else 2,account['generation'])
                events.append('dm')
                if dm_fails:
                    raise RuntimeError('inbox did not load')
                return [{'thread_id':'new','sender':'bravo','preview':'hello'}]
            worker.collect_following.side_effect=following
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock(side_effect=dms)
            with patch('app.follow_monitor.PlaywrightWorker',return_value=worker):
                started=await self.manager.start(self.owner,[profile])
                await asyncio.gather(*list(self.manager._tasks.values()))
            self.assertEqual(['following','dm'],events)
            worker.connect.assert_awaited_once_with(profile,open_if_needed=True)
            worker.disconnect.assert_awaited_once()
            self.assertEqual(int(not follow_fails and not dm_fails),self.browser.closed.count(profile))
            state=self.manager.snapshot(self.owner)
            run=state['run']
            self.assertEqual('combined',run['check_kind'])
            self.assertEqual('failed' if follow_fails and dm_fails else 'partial' if follow_fails or dm_fails else 'completed',run['status'])
            self.assertEqual((int(not follow_fails),int(not dm_fails)),(run['added_count'],run['dm_count']))
            self.assertEqual(1,run['processed'])
            self.assertEqual(int(follow_fails or dm_fails),run['failed'])
            for kind in ('combined','following','dm'):
                self.assertEqual(started['run_id'],state['runs'][kind]['id'])
            self.assertEqual([] if follow_fails else ['bravo'],[r['username'] for r in state['latest_follow']])
            self.assertEqual([] if dm_fails else ['new'],[r['thread_id'] for r in state['latest_dm']])
            self.assertEqual(0 if follow_fails else 1,len(state['rounds']))
            account=next(a for a in state['accounts'] if a['profile_id']==profile)
            dm_account=next(a for a in state['dm_accounts'] if a['profile_id']==profile)
            self.assertEqual('failed' if follow_fails else 'completed',account['last_status'])
            self.assertEqual('failed' if dm_fails else 'completed',dm_account['last_status'])
            self.assertEqual(1 if dm_fails else 2,dm_account['total_dm_count'])
            self.assertEqual(set(),self.manager.active_run_ids())
            self.assertEqual([],self.service.list_browser_lease_states(self.owner,active_collection_entity_ids=set(),active_action_entity_ids=set(),active_monitor_entity_ids=set(),inactive_grace_seconds=0))
        for follow_fails,dm_fails in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(follow_fails=follow_fails,dm_fails=dm_fails):
                asyncio.run(exercise(follow_fails,dm_fails))

    def test_successful_empty_dm_stage_is_partial_when_following_fails(self):
        async def exercise():
            worker=Worker(None)
            worker.collect_following.side_effect=RuntimeError('list did not load')
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock(return_value=[])
            with patch('app.follow_monitor.PlaywrightWorker',return_value=worker):
                await self.manager.start(self.owner,['w1'])
                await asyncio.gather(*list(self.manager._tasks.values()))
            state=self.manager.snapshot(self.owner)
            self.assertEqual('partial',state['run']['status'])
            self.assertEqual(0,state['run']['dm_count'])
            self.assertEqual('completed',state['dm_accounts'][0]['last_status'])
            self.assertEqual({'following':'list did not load'},state['run']['errors'][0]['stages'])
        asyncio.run(exercise())

    def test_combined_conflicts_with_active_split_checks_in_both_directions(self):
        async def exercise():
            block=asyncio.Event()
            async def scan(*args):
                await block.wait()
                return (0,0,0,0)
            self.manager._scan_profile=scan
            for kind in ('following','dm'):
                block.clear()
                await self.manager.start(self.owner,['w1'],check_kind=kind)
                with self.assertRaises(ConflictError):
                    await self.manager.start(self.owner,['w2'])
                tasks=list(self.manager._tasks.values());block.set();await asyncio.gather(*tasks)
            block.clear()
            await self.manager.start(self.owner,['w1'])
            for kind in ('combined','following','dm'):
                with self.assertRaises(ConflictError):
                    await self.manager.start(self.owner,['w2'],check_kind=kind)
            tasks=list(self.manager._tasks.values());block.set();await asyncio.gather(*tasks)
        asyncio.run(exercise())

    def test_cancelled_combined_check_does_not_start_dm_and_releases_window(self):
        async def exercise():
            entered=asyncio.Event()
            async def following(*args,**kwargs):
                entered.set()
                await asyncio.Event().wait()
            worker=Worker(None)
            worker.collect_following.side_effect=following
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock()
            with patch('app.follow_monitor.PlaywrightWorker',return_value=worker):
                await self.manager.start(self.owner,['w1'])
                await entered.wait()
                await self.manager.shutdown()
            self.manager._read_pending_dms.assert_not_awaited()
            worker.disconnect.assert_awaited_once()
            self.assertEqual([],self.browser.closed)
            self.assertEqual('stopped',self.manager.snapshot(self.owner)['run']['status'])
        asyncio.run(exercise())


    def test_pause_before_start_resume_and_cancel_before_start(self):
        async def exercise():
            self.manager._scan_profile=AsyncMock(return_value=(0,0,0,0))
            run=await self.manager.start(self.owner,['w1'])
            await self.manager.control(self.owner,run['run_id'],'pause')
            await asyncio.sleep(0)
            self.manager._scan_profile.assert_not_awaited()
            self.assertEqual('paused',self.manager.snapshot(self.owner)['run']['status'])
            await self.manager.control(self.owner,run['run_id'],'resume')
            await asyncio.gather(*list(self.manager._tasks.values()))
            self.manager._scan_profile.assert_awaited_once()
            self.assertEqual('completed',self.manager.snapshot(self.owner)['run']['status'])
            run=await self.manager.start(self.owner,['w2'])
            await self.manager.control(self.owner,run['run_id'],'cancel')
            await asyncio.gather(*list(self.manager._tasks.values()),return_exceptions=True)
            self.manager._scan_profile.assert_awaited_once()
            self.assertEqual('cancelled',self.manager.snapshot(self.owner)['run']['status'])
            self.assertFalse(self.manager._controls)
            self.assertFalse(self.manager._owner_runs)
        asyncio.run(exercise())

    def test_shutdown_before_first_task_step_records_stopped_and_clears_ownership(self):
        async def exercise():
            self.manager._scan_profile = AsyncMock(return_value=(0, 0, 0, 0))
            run = await self.manager.start(self.owner, ['w1'])
            await self.manager.shutdown()
            self.manager._scan_profile.assert_not_awaited()
            self.assertEqual('stopped', self.manager.snapshot(self.owner)['run']['status'])
            self.assertFalse(self.manager.active_run_ids())
            self.assertNotIn(run['run_id'], self.manager._controls)
            self.assertFalse(self.manager._owner_runs)
        asyncio.run(exercise())

    def test_completed_window_releases_while_next_window_is_paused(self):
        async def exercise():
            entered=asyncio.Event();release=asyncio.Event()
            workers=[]
            def factory(_browser):
                worker=Worker(None)
                workers.append(worker)
                return worker
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            async def dm(worker):
                if len(workers)==1:
                    await self.manager.control(self.owner,run['run_id'],'pause')
                    entered.set()
                    await release.wait()
                return []
            self.manager._read_pending_dms=AsyncMock(side_effect=dm)
            with patch('app.follow_monitor.PlaywrightWorker',side_effect=factory):
                run=await self.manager.start(self.owner,['w1','w2'],concurrency=1)
                await entered.wait()
                self.assertEqual(1,len(workers))
                self.assertEqual(['w1'],self.manager.snapshot(self.owner)['run']['active_profile_ids'])
                # Paused windows retain their lease; resume allows their result commit.
                self.assertNotIn('w1',self.browser.closed)
                await self.manager.control(self.owner,run['run_id'],'resume')
                release.set()
                second_entered=asyncio.Event();second_release=asyncio.Event()
                async def second_dm(worker):
                    second_entered.set()
                    await second_release.wait()
                    return []
                self.manager._read_pending_dms=AsyncMock(side_effect=second_dm)
                await second_entered.wait()
                snapshot=self.manager.snapshot(self.owner)
                self.assertEqual('running',snapshot['run']['status'])
                self.assertEqual(['w1'],snapshot['run']['finished_profile_ids'])
                self.assertEqual(['w2'],snapshot['run']['active_profile_ids'])
                self.assertEqual(['w1'],self.browser.closed)
                leases=self.service.list_browser_lease_states(self.owner,active_collection_entity_ids=set(),active_action_entity_ids=set(),active_monitor_entity_ids=self.manager.active_run_ids(),inactive_grace_seconds=0)
                self.assertNotIn('w1',[item['profile_id'] for item in leases])
                await self.manager.control(self.owner,run['run_id'],'pause')
                await self.manager.control(self.owner,run['run_id'],'cancel')
                await asyncio.gather(*list(self.manager._tasks.values()),return_exceptions=True)
            self.assertEqual(['w1'],self.browser.closed)
            self.assertEqual([],self.manager.snapshot(self.owner)['run']['active_profile_ids'])
            self.assertEqual('cancelled',self.manager.snapshot(self.owner)['run']['status'])
        asyncio.run(asyncio.wait_for(exercise(),timeout=5))

    def test_cancel_during_dm_keeps_following_result_and_aggregate_counts(self):
        async def exercise():
            self.commit('seed',{'old'},total=1)
            worker=Worker(None)
            worker.collect_following.return_value=SimpleNamespace(usernames=['old','new'],source_total=2)
            entered=asyncio.Event()
            async def dm(_worker):
                entered.set()
                await asyncio.Event().wait()
            self.manager._read_identity=AsyncMock(return_value=('ig1','owner_one'))
            self.manager._read_pending_dms=AsyncMock(side_effect=dm)
            with patch('app.follow_monitor.PlaywrightWorker',return_value=worker):
                run=await self.manager.start(self.owner,['w1','w2'],concurrency=1)
                await entered.wait()
                await self.manager.control(self.owner,run['run_id'],'cancel')
                await self.manager.control(self.owner,run['run_id'],'cancel')
                with self.assertRaises(ConflictError):
                    await self.manager.control(self.owner,run['run_id'],'resume')
                await asyncio.gather(*list(self.manager._tasks.values()),return_exceptions=True)
            state=self.manager.snapshot(self.owner)
            self.assertEqual('cancelled',state['run']['status'])
            self.assertEqual(1,state['run']['added_count'])
            self.assertEqual(['new'],[row['username'] for row in state['latest_follow']])
            self.assertEqual('completed',state['accounts'][0]['last_status'])
            self.assertEqual([],self.browser.closed)
            self.assertEqual(1,worker.connect.await_count)
        asyncio.run(exercise())

    def test_control_is_owner_scoped_and_cannot_resume_terminal_run(self):
        async def exercise():
            other=self.service.register_user('another-owner','correct horse battery staple')['id']
            self.manager._scan_profile=AsyncMock(return_value=(0,0,0,0))
            run=await self.manager.start(self.owner,['w1'])
            with self.assertRaises(NotFoundError):
                await self.manager.control(other,run['run_id'],'cancel')
            self.assertEqual('running',self.manager.snapshot(self.owner)['run']['status'])
            await asyncio.gather(*list(self.manager._tasks.values()))
            with self.assertRaises(ConflictError):
                await self.manager.control(self.owner,run['run_id'],'resume')
        asyncio.run(exercise())

    def test_restart_recovers_paused_and_cancelling_runs_without_resetting_history(self):
        self.commit('seed',{'a'},total=1)
        with self.db.write() as c:
            for state in ('paused','cancelling'):
                c.execute("INSERT INTO follow_monitor_runs(id,owner_user_id,profile_ids_json,check_kind,status,started_at) VALUES(?,?,'[]','combined',?,'2026-01-01')",(state,self.owner,state))
        self.manager.recover_interrupted()
        snapshot=self.manager.snapshot(self.owner)
        self.assertEqual({'interrupted'},{run['status'] for run in snapshot['logs']})
        self.assertEqual(1,snapshot['accounts'][0]['following_count'])

    def test_authenticated_api_routes_forward_check_kind(self):
        import json
        from app.config import Settings
        from app.main import create_app
        token=self.service.login('monitor-owner','correct horse battery staple')['token']
        settings=Settings(startup_token='monitor-startup-token-long-enough',database_path=self.db.path,data_dir=Path(self.tmp.name))
        app=create_app(settings,database=self.db,bitbrowser=SimpleNamespace())
        async def request(method,path,headers,payload=None):
            # Exercise the real ASGI routing, authentication and request model
            # without a network server or an optional HTTP test-client package.
            body=json.dumps(payload).encode() if payload is not None else b''
            sent=[]
            async def receive():
                return {'type':'http.request','body':body,'more_body':False}
            async def send(message):
                sent.append(message)
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
                   'method':method,'scheme':'http','path':path,'raw_path':path.encode(),
                   'query_string':b'','root_path':'','server':('localhost',80),'client':('127.0.0.1',12345),
                   'headers':[(k.lower().encode(),v.encode()) for k,v in {**headers,'Content-Type':'application/json'}.items()]}
            await app(scope,receive,send)
            status=next(m['status'] for m in sent if m['type']=='http.response.start')
            data=json.loads(b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body'))
            return status,data
        async def exercise():
            with patch.object(FollowMonitorManager,'start',new_callable=AsyncMock) as start, patch.object(FollowMonitorManager,'control',new_callable=AsyncMock) as control:
                start.return_value={'run_id':'run','status':'running','total':1}
                control.return_value={'run_id':'run','status':'paused'}
                async with app.router.lifespan_context(app):
                    headers={'X-Startup-Token':settings.startup_token}
                    status,_=await request('POST','/api/follow-monitor/runs',headers,{'profile_ids':['w1']})
                    self.assertEqual(401,status)
                    status,_=await request('POST','/api/follow-monitor/control',headers,{'run_id':'run','action':'pause'})
                    self.assertEqual(401,status)
                    headers['Authorization']=f'Bearer {token}'
                    for kind in ('combined','following','dm'):
                        status,data=await request('POST','/api/follow-monitor/runs',headers,{'profile_ids':['w1'],'concurrency':1,'check_kind':kind})
                        self.assertEqual(200,status,data)
                        self.assertEqual(kind,start.await_args.kwargs['check_kind'])
                    for action in ('pause','resume','cancel'):
                        status,data=await request('POST','/api/follow-monitor/control',headers,{'run_id':'run','action':action})
                        self.assertEqual(200,status,data)
                        control.assert_awaited_with(self.owner,'run',action)
                    status,_=await request('POST','/api/follow-monitor/control',headers,{'run_id':'run','action':'delete'})
                    self.assertEqual(422,status)
                    status,data=await request('GET','/api/follow-monitor/snapshot',headers)
                    self.assertEqual(200,status)
                    self.assertEqual({'combined':None,'following':None,'dm':None},data['runs'])
                    status,_=await request('POST','/api/follow-monitor/runs',headers,{'profile_ids':['w1'],'check_kind':'followers'})
                    self.assertEqual(422,status)
                    stopped=[];app.state.request_shutdown=lambda:stopped.append(True)
                    status,_=await request('POST','/api/internal/shutdown',{})
                    self.assertEqual(401,status);self.assertFalse(stopped)
                    status,data=await request('POST','/api/internal/shutdown',headers)
                    self.assertEqual(200,status);self.assertTrue(data['accepted'])
                    await asyncio.sleep(0);self.assertTrue(stopped)
                    status,_=await request('POST','/api/follow-monitor/runs',headers,{'profile_ids':['w1']})
                    self.assertEqual(503,status)
        asyncio.run(exercise())


def run_following_dom_fixtures(fixtures):
    import json
    import subprocess
    process = subprocess.run(
        ['node', str(Path(__file__).parent / 'fixtures/following_rows_dom.mjs')],
        input=json.dumps({'script': _MONITOR_FOLLOWING_ROWS_SCRIPT, 'fixtures': fixtures}, ensure_ascii=False),
        # Node reads and writes UTF-8 JSON. Never use the Windows ANSI code page:
        # a decoder error in subprocess's Windows reader thread loses stdout.
        text=True, encoding='utf-8', errors='strict', capture_output=True, timeout=15, check=True,
    )
    return json.loads(process.stdout)


class FollowingButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def row(name, label=None, *, display_name=None, extra=None):
            children=[{'tag':'a','attrs':{'href':f'/{name}/'},'children':[{'tag':'img'}]},
                      {'tag':'div','children':[{'tag':'a','attrs':{'href':f'/{name}/'},'text':name},
                                               {'tag':'span','text':display_name or name}]}]
            if label is not None:
                children.append({'tag':'button','text':label})
            children.extend(extra or [])
            return {'tag':'div','children':children}
        def dialog(children):
            return {'tag':'div','attrs':{'role':'dialog'},'children':children}
        cls.real={f'actual_{i}' for i in range(8)}
        fixtures=[
            dialog([row(name,'已关注') for name in sorted(cls.real)]
                +[{'tag':'div','text':'为你推荐'}]+[row(f'suggested_{i}','关注') for i in range(30)]),
            dialog([row('real','Following',display_name='Follow'),row('suggested','Follow',display_name='Following'),
                    row('back','Follow back'),row('pending','Requested'),row('no_button')]),
            dialog([row('no_button'),row('real','已关注')]),
            dialog([row('real','Following'),{'tag':'div','text':'Suggested for you'},row('recommended_followed','Following')]),
            dialog([row('real','Following',extra=[{'tag':'button','text':'Follow','attrs':{'hidden':True}}])]),
            dialog([row('real','已关注',display_name='为你推荐'),row('other','已关注')]),
        ]
        cls.fixtures=run_following_dom_fixtures(fixtures)

    def test_node_fixture_roundtrip_is_independent_of_system_codepage(self):
        import subprocess
        labels = ['已关注', 'กำลังติดตาม', 'フォロー中', '팔로잉']
        fixture = {'tag': 'div', 'attrs': {'role': 'dialog'}, 'children': [
            {'tag': 'div', 'children': [
                {'tag': 'a', 'attrs': {'href': f'/account_{i}/'}, 'text': f'account_{i}'},
                {'tag': 'button', 'text': label},
            ]}
            for i, label in enumerate(labels)
        ]}
        # Exercise actual Node stdin/stdout while emulating non-UTF-8 defaults.
        # Without an explicit encoding, Chinese/Thai Windows either loses the
        # reader-thread result or silently corrupts the button text.
        for codepage in ('gbk', 'cp950', 'cp874', 'cp1252'):
            with self.subTest(codepage=codepage):
                with patch.object(subprocess, '_text_encoding', return_value=codepage):
                    rows = run_following_dom_fixtures([fixture])[0]
                self.assertEqual(labels, [row['actions'][0] for row in rows])
                self.assertEqual([f'/account_{i}/' for i in range(4)], self.read(rows))

    def read(self, rows):
        async def exercise():
            worker=PlaywrightWorker(Browser())
            # Known fixtures with missing buttons get one bounded retry interval.
            now=iter([10.0,14.0,14.0,14.0])
            worker._collection_monotonic=lambda: next(now,14.0)
            return await worker._read_visible_account_hrefs(SimpleNamespace(evaluate=AsyncMock(return_value=rows)),
                surface_kind='following',monitor_observation=True)
        return asyncio.run(exercise())

    def test_eight_followed_plus_thirty_recommendations_return_eight(self):
        names={href.strip('/') for href in self.read(self.fixtures[0])}
        self.assertEqual(self.real,names)
        self.assertEqual(8,len(names))

    def test_complete_monitor_read_stops_at_recommendations_without_count_cap(self):
        async def exercise():
            worker=PlaywrightWorker(Browser())
            worker._guard=AsyncMock()
            surface=SimpleNamespace(evaluate=AsyncMock(return_value=self.fixtures[0]))
            names=await worker._read_visible_account_dialog(surface,None,surface_kind='following',monitor_observation=True)
            self.assertEqual(self.real,set(names))
            # One atomic row read; no bottom scroll into the recommendation list.
            surface.evaluate.assert_awaited_once_with(_MONITOR_FOLLOWING_ROWS_SCRIPT)
        asyncio.run(exercise())

    def test_only_same_row_button_qualifies_not_names_or_pending_requests(self):
        self.assertEqual(['/real/'],self.read(self.fixtures[1]))
        self.assertEqual(['/real/'],self.read(self.fixtures[2]))

    def test_recommendation_heading_excludes_even_a_following_button_below_it(self):
        self.assertEqual(['/real/'],self.read(self.fixtures[3]))

    def test_hidden_cta_and_display_name_are_not_button_or_section_evidence(self):
        self.assertEqual(['/real/'],self.read(self.fixtures[4]))
        self.assertEqual(['/real/','/other/'],self.read(self.fixtures[5]))

    def test_button_labels_are_exact_and_multilingual(self):
        for label in ['已关注','关注中','Following',' FOLLOWING ','追蹤中','กำลังติดตาม','フォロー中','팔로잉']:
            self.assertEqual('following',following_row_button_state([label]),label)
        for label in ['关注','Follow','Follow back','Requested','回关','已请求']:
            self.assertEqual('not_following',following_row_button_state([label]),label)
        for label in ['', 'Following suggestions', 'not following', 'Somebody Following', '已关注推荐']:
            self.assertEqual('unknown',following_row_button_state([label]),label)
        self.assertEqual('not_following',following_row_button_state(['Following','Follow']))

    def test_late_button_is_reread_in_place_before_returning_accounts(self):
        async def exercise():
            worker=PlaywrightWorker(Browser())
            partial=[{'href':'/a/','actions':['Following'],'has_row':True}, {'href':'/b/','actions':[],'has_row':True}]
            complete=[partial[0],{'href':'/b/','actions':['已关注'],'has_row':True}]
            surface=SimpleNamespace(evaluate=AsyncMock(side_effect=[partial,complete]))
            with patch('app.playwright_worker.asyncio.sleep',new_callable=AsyncMock) as sleep:
                names=await worker._read_visible_account_hrefs(surface,surface_kind='following',monitor_observation=True)
            self.assertEqual(['/a/','/b/'],names)
            self.assertEqual(2,surface.evaluate.await_count)
            sleep.assert_awaited_once_with(worker.collection_poll_interval_seconds)
        asyncio.run(exercise())

    def test_pause_preserves_current_batch_and_resumes_without_reopening(self):
        async def exercise():
            worker=PlaywrightWorker(Browser())
            worker.collection_poll_interval_seconds=0
            worker._guard=AsyncMock()
            # Keep the production checkpoint and pause-subtracting clock, but
            # control only the worker's time. A real 20 ms sleep can measure as
            # 16 ms on Windows; asyncio's scheduler/timeout clock stays real.
            elapsed=10.0
            worker_asyncio=SimpleNamespace(**vars(asyncio))
            worker_asyncio.get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed)
            gate=asyncio.Event();gate.set()
            paused=asyncio.Event()
            async def checkpoint():
                if not gate.is_set(): paused.set()
                await gate.wait()
            worker.monitor_checkpoint=checkpoint
            worker._read_visible_account_hrefs=AsyncMock(side_effect=[['/a/'],['/b/']])
            async def scroll(_script):
                gate.clear()
            surface=SimpleNamespace(evaluate=AsyncMock(side_effect=scroll))
            with patch('app.playwright_worker.asyncio',worker_asyncio):
                active_time=worker._collection_monotonic()
                task=asyncio.create_task(worker._read_visible_account_dialog(surface,2,surface_kind='following',monitor_observation=True))
                try:
                    await paused.wait()
                    pause_seconds=60.0
                    self.assertGreater(pause_seconds,worker.collection_loading_grace_seconds)
                    elapsed+=pause_seconds
                    await asyncio.sleep(0)  # Let runnable work proceed with the gate shut.
                    self.assertFalse(task.done())
                    self.assertEqual(1,worker._read_visible_account_hrefs.await_count)
                    gate.set()
                    self.assertEqual(['a','b'],await task)
                    self.assertEqual(2,worker._read_visible_account_hrefs.await_count)
                    self.assertEqual(pause_seconds,worker._monitor_paused_seconds)
                    self.assertEqual(active_time,worker._collection_monotonic())
                    surface.evaluate.assert_awaited_once()
                finally:
                    gate.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
        asyncio.run(asyncio.wait_for(exercise(),timeout=3))

    def test_invalid_row_data_cannot_fall_back_to_unfiltered_profile_links(self):
        async def exercise():
            worker=PlaywrightWorker(Browser())
            with self.assertRaises(WorkerExecutionError):
                await worker._read_visible_account_hrefs(SimpleNamespace(evaluate=AsyncMock(return_value=['/suggested/'])),surface_kind='following',monitor_observation=True)
        asyncio.run(exercise())


if __name__=='__main__':
    unittest.main()
