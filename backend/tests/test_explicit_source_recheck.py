"""Explicit gap rechecks settle old workers and retain every saved identity."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch

from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome
import test_collection_drain_r56 as drain_cases
from test_core import FakeCollectionWorker
from test_runtime_r24 import WaitingManager


class ExplicitSourceRecheckTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain_cases.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain_cases.CollectionDrainR56Tests.asyncTearDown
    task = drain_cases.CollectionDrainR56Tests.task
    manager = drain_cases.CollectionDrainR56Tests.manager
    gate = drain_cases.CollectionDrainR56Tests.gate
    until = drain_cases.CollectionDrainR56Tests.until
    leases = drain_cases.CollectionDrainR56Tests.leases

    def completed_gap(self, *, windows=('window-a',), modes=('followers',), task_status='completed'):
        task = self.service.create_task(self.owner, name='gap', modes=list(modes), targets=['source'],
            window_ids=list(windows), settings={'live_queue_enabled': True, 'location_enabled': False})
        target = task['targets'][0]['id']
        for mode in modes:
            self.service.upsert_checkpoint(self.owner, task['id'], target, mode=mode,
                stage='mode_completed', cursor={'candidate_spool_version': 1,
                    'candidate_spool_complete': True, 'candidate_spool_natural_end': True,
                    'resume_tail': ['already_read'], 'rendered_count': 1},
                counters={'source_total': 3, 'discovered': 1, 'processed': 1, 'saved': 1}, recoverable=False)
        # Use the production completion trigger to create immutable split
        # history, rather than testing an empty history table.
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO split_candidates(id,owner_user_id,username_norm,username_display,candidate_kind,"
                "source_task_id,source_target_id,queue_state,queued_task_id,queued_target_id,created_at,updated_at) "
                "VALUES(?,?,? ,?,'manual',?,?,'claimed',?,?,?,?)",
                ('candidate-'+target,self.owner,'source','source',task['id'],target,task['id'],target,
                 task['created_at'],task['created_at']))
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed', window_id=windows[0])
        if task_status == 'paused':
            self.service.set_task_runtime_status(self.owner, task['id'], task_status)
        else:
            self.service.finalize_task_runtime_status(self.owner, task['id'], task_status)
        return task, target

    def lease_token(self, profile='window-a'):
        with self.database.read() as connection:
            return connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone()[0]

    def checkpoint_bytes(self, target, mode='followers'):
        with self.database.read() as connection:
            return tuple(connection.execute('SELECT stage,cursor_json,counters_json,recoverable,updated_at FROM task_checkpoints WHERE target_id=? AND mode=?', (target,mode)).fetchone())

    async def test_completed_gap_really_scans_and_keeps_other_mode_exact(self):
        task, target = self.completed_gap(modes=('followers','following'))
        before = self.checkpoint_bytes(target, 'following')
        scanned = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                scanned.append(username)
                return CollectionOutcome('followers', ['found_one', 'found_two'], source_total=3)
        manager, provider = self.manager(Worker)
        result = await manager.recheck_source(self.owner, task['id'], target, 'followers')
        self.assertFalse(result['waiting_for_task_resume'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertEqual(['source'], scanned)
        self.assertEqual(before, self.checkpoint_bytes(target, 'following'))
        current = self.service.get_task(self.owner, task['id'])
        self.assertEqual('completed', current['status'])
        self.assertEqual('completed', current['targets'][0]['status'])
        self.assertEqual(2, self.service.task_mode_candidate_stats(self.owner, task['id'], target,'followers')['total'])
        self.assertEqual({}, self.leases())

    async def test_busy_other_task_never_released_or_rewound(self):
        task, target = self.completed_gap()
        token = self.service.acquire_browser_lease(self.owner,'window-a',operation_type='collection',entity_id='other-collection-task')
        before = self.checkpoint_bytes(target)
        manager, provider = self.manager()
        with self.assertRaises(ConflictError) as caught:
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual('source_recheck_no_idle_window', caught.exception.details['reason'])
        self.assertEqual(before, self.checkpoint_bytes(target))
        self.assertEqual(token, self.lease_token())
        self.assertEqual([], provider.closed)
        self.service.release_browser_lease('window-a',token)

    async def test_inactive_paused_task_retains_pause_and_repeat_does_not_reset(self):
        task,target=self.completed_gap(task_status='paused')
        manager,_=self.manager(manager_type=WaitingManager)
        result=await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertTrue(result['waiting_for_task_resume'])
        self.assertEqual('paused',self.service.get_task(self.owner,task['id'])['status'])
        before=self.checkpoint_bytes(target)
        token=self.lease_token()
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual(token,self.lease_token())

    async def test_transaction_failure_preserves_checkpoint_and_releases_new_lease(self):
        task,target=self.completed_gap()
        before=self.checkpoint_bytes(target)
        with self.database.write() as connection:
            connection.execute("CREATE TRIGGER fail_recheck BEFORE UPDATE OF current_stage ON task_targets WHEN NEW.current_stage='source_recheck_requested' BEGIN SELECT RAISE(ABORT,'test fault'); END")
        manager,_=self.manager()
        with self.assertRaises(sqlite3.IntegrityError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual({},self.leases())
        with self.database.read() as connection:
            self.assertEqual(0,connection.execute('SELECT count(*) FROM task_source_rechecks').fetchone()[0])

    async def test_running_refused_then_paused_cleanup_keeps_lease_before_rewind(self):
        task=self.task()
        target=task['targets'][0]['id']
        entered=self.gate(); cleanup=self.gate(); release=self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                cleanup.set()
                await release.wait()
        manager,_=self.manager(Worker)
        await manager.start(self.owner,task['id'])
        await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        before=self.checkpoint_bytes(target)
        token=self.lease_token()
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,self.checkpoint_bytes(target))
        await manager.pause(self.owner,task['id'])
        rechecking=asyncio.create_task(manager.recheck_source(self.owner,task['id'],target,'followers'))
        await asyncio.wait_for(cleanup.wait(),3)
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual(token,self.lease_token())
        self.assertFalse(rechecking.done())
        release.set()
        result=await asyncio.wait_for(rechecking,5)
        self.assertTrue(result['waiting_for_task_resume'])
        self.assertEqual(token,self.lease_token())
        self.assertFalse(manager._runs[task['id']].pause_event.is_set())

    async def test_real_saved_spool_results_and_global_dedupe_survive_recheck(self):
        task=self.service.create_task(self.owner,name='real gap',modes=['followers','following'],
            targets=['real_source'],window_ids=['window-a'],settings={'live_queue_enabled':True})
        target=task['targets'][0]['id']
        reads=[]
        class Initial(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers',['already_read'],source_total=3)
            async def collect_following(self, username, **kwargs):
                return CollectionOutcome('following',['old_following'],source_total=1)
            async def read_visible_profile(self, username, **kwargs):
                reads.append(username)
                return await super().read_visible_profile(username,**kwargs)
        manager,_=self.manager(Initial)
        # Seed the saved data of a legacy completed gap. New normal runtime
        # completion now automatically dismisses its card.
        original_release = self.service.release_browser_lease
        with patch.object(self.service, 'release_browser_lease',
                          side_effect=lambda profile, token, **kwargs: original_release(profile, token)):
            await manager.start(self.owner,task['id'])
            await asyncio.wait_for(manager.wait(task['id']),10)
        with self.database.read() as connection:
            old_spool=[tuple(row) for row in connection.execute('SELECT * FROM task_mode_candidates ORDER BY discovery_order')]
            old_results=[tuple(row) for row in connection.execute('SELECT * FROM task_results ORDER BY id')]
            old_dedupe=[tuple(row) for row in connection.execute('SELECT * FROM global_seen ORDER BY account_id')]
        self.assertEqual(2,len(old_spool))
        self.assertEqual(2,len(old_results))
        self.assertGreaterEqual(len(old_dedupe),2)
        other=self.checkpoint_bytes(target,'following')
        reads.clear()
        class Recheck(Initial):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers',['already_read','new_account'],source_total=3)
            async def collect_following(self, username, **kwargs):
                raise AssertionError('unchanged finished mode must not rescan')
        manager.worker_factory=Recheck
        await manager.recheck_source(self.owner,task['id'],target,'followers')
        await asyncio.wait_for(manager.wait(task['id']),10)
        self.assertNotIn('already_read',reads)
        self.assertIn('new_account',reads)
        self.assertEqual(other,self.checkpoint_bytes(target,'following'))
        with self.database.read() as connection:
            new_spool=[tuple(row) for row in connection.execute('SELECT * FROM task_mode_candidates ORDER BY discovery_order')]
            new_results=[tuple(row) for row in connection.execute('SELECT * FROM task_results ORDER BY id')]
            new_dedupe=[tuple(row) for row in connection.execute('SELECT * FROM global_seen ORDER BY account_id')]
            recheck=connection.execute('SELECT state FROM task_source_rechecks WHERE target_id=?',(target,)).fetchone()
        for row in old_spool: self.assertIn(row,new_spool)
        for row in old_results: self.assertIn(row,new_results)
        for row in old_dedupe: self.assertIn(row[0],{saved[0] for saved in new_dedupe})
        self.assertEqual(3,len(new_spool))
        self.assertEqual('completed',recheck['state'])
        with self.database.write() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE task_targets SET status='pending' WHERE id=?",(target,))

    async def test_live_transaction_failure_removes_only_settled_recheck_fence(self):
        task=self.task()
        target=task['targets'][0]['id']; entered=self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        manager,_=self.manager(Worker)
        await manager.start(self.owner,task['id'])
        await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        before=self.checkpoint_bytes(target); token=self.lease_token()
        await manager.pause(self.owner,task['id'])
        with self.database.write() as connection:
            connection.execute("CREATE TRIGGER fail_live_recheck BEFORE UPDATE OF current_stage ON task_targets WHEN NEW.current_stage='source_recheck_requested' BEGIN SELECT RAISE(ABORT,'test fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual(token,self.lease_token())
        self.assertEqual(set(),manager._runs[task['id']].source_recheck_profiles)
        with self.database.write() as connection:
            connection.execute('DROP TRIGGER fail_live_recheck')
        result=await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertTrue(result['waiting_for_task_resume'])

    async def test_cleanup_failure_never_rewinds_or_releases_lease(self):
        task=self.task(); target=task['targets'][0]['id']; entered=self.gate(); failing=[True]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                if failing[0]: raise RuntimeError('cleanup still owns a page')
        manager,_=self.manager(Worker)
        await manager.start(self.owner,task['id'])
        await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        before=self.checkpoint_bytes(target); token=self.lease_token()
        await manager.pause(self.owner,task['id'])
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual(token,self.lease_token())
        self.assertIn('window-a',manager._runs[task['id']].source_recheck_profiles)
        failing[0]=False

    async def test_recheck_does_not_resume_other_targets_or_other_incomplete_mode(self):
        task=self.service.create_task(self.owner,name='bounded',modes=['followers','following'],
            targets=['chosen','untouched'],window_ids=['window-a'],settings={'live_queue_enabled':True})
        target=task['targets'][0]['id']; untouched=task['targets'][1]['id']
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='mode_completed',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='following',stage='discovering_accounts',
            cursor={'candidate_spool_complete':False,'resume_tail':['keep_tail']},
            counters={'source_total':9,'discovered':2,'processed':1})
        self.service.set_target_runtime_status(self.owner,task['id'],target,'stopped',window_id='window-a')
        self.service.finalize_task_runtime_status(self.owner,task['id'],'stopped')
        other=self.checkpoint_bytes(target,'following'); scanned=[]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                scanned.append(('followers',username))
                return CollectionOutcome('followers',['new_account'],source_total=3)
            async def collect_following(self, username, **kwargs):
                scanned.append(('following',username))
                raise AssertionError('other mode must remain unchanged')
        manager,_=self.manager(Worker)
        await manager.recheck_source(self.owner,task['id'],target,'followers')
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual([('followers','chosen')],scanned)
        self.assertEqual(other,self.checkpoint_bytes(target,'following'))
        current=self.service.get_task(self.owner,task['id'])
        rows={row['id']:row for row in current['targets']}
        self.assertEqual('pending',rows[untouched]['status'])
        self.assertEqual('recoverable',rows[target]['status'])

    async def test_failed_cleanup_can_be_retried_by_stop_without_losing_lease(self):
        task=self.task(); target=task['targets'][0]['id']; entered=self.gate(); failures=[True]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                if failures[0]:
                    raise RuntimeError('cleanup still owns a page')
        manager,_=self.manager(Worker)
        await manager.start(self.owner,task['id']); await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        before=self.checkpoint_bytes(target); token=self.lease_token()
        await manager.pause(self.owner,task['id'])
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        with self.assertRaises(ConflictError):
            await manager.stop_window(self.owner,task['id'],'window-a')
        self.assertEqual(token,self.lease_token())
        self.assertEqual(before,self.checkpoint_bytes(target))
        self.assertEqual('source_recheck_cleanup_failed',manager._runs[task['id']].profile_states['window-a']['reason'])
        failures[0]=False
        await manager.stop_window(self.owner,task['id'],'window-a')
        self.assertEqual(token,self.lease_token())
        self.assertNotIn('window-a',manager._runs[task['id']].source_recheck_profiles)
        await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual('source_recheck_requested',self.checkpoint_bytes(target)[0])

    async def test_task_stop_joins_inflight_cleanup_retry_before_release(self):
        task=self.task(); target=task['targets'][0]['id']; entered=self.gate(); retry_entered=self.gate(); release=self.gate()
        calls=[0]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set(); await asyncio.Event().wait()
            async def disconnect(self):
                calls[0]+=1
                if calls[0]==1: raise RuntimeError('first cleanup failed')
                retry_entered.set(); await release.wait()
        manager,provider=self.manager(Worker)
        await manager.start(self.owner,task['id']); await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        await manager.pause(self.owner,task['id'])
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        retry=asyncio.create_task(manager.stop_window(self.owner,task['id'],'window-a'))
        await asyncio.wait_for(retry_entered.wait(),3)
        token=self.lease_token()
        stopping=asyncio.create_task(manager.stop(self.owner,task['id']))
        await asyncio.sleep(.04)
        self.assertFalse(stopping.done()); self.assertEqual(token,self.lease_token())
        self.assertEqual([],provider.closed)
        release.set()
        await asyncio.wait_for(asyncio.gather(retry,stopping),5)
        self.assertEqual({},self.leases())

    async def test_live_idle_recheck_preserves_unrelated_shared_queue(self):
        task,target=self.completed_gap(task_status='paused')
        self.service.add_targets(self.owner,task['id'],['untouched'])
        idle=self.gate(); scanned=[]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                scanned.append(username)
                return CollectionOutcome('followers',['fresh'],source_total=3)
        class Manager(ExecutionManager):
            calls=0
            async def _window_loop(self, control, profile_id, *args):
                self.calls+=1
                if self.calls==1:
                    self._profile_state_locked(control,profile_id,state='idle')
                    idle.set(); await control.stop_event.wait(); return
                return await super()._window_loop(control,profile_id,*args)
        manager,_=self.manager(Worker,manager_type=Manager)
        await manager.start(self.owner,task['id']); await asyncio.wait_for(idle.wait(),3)
        control=manager._runs[task['id']]
        with patch.object(self.service,'claim_next_split_candidate',wraps=self.service.claim_next_split_candidate) as claim:
            await manager.recheck_source(self.owner,task['id'],target,'followers')
            worker=control.profile_worker_tasks['window-a']
            await asyncio.wait_for(worker,5)
            self.assertEqual(0,claim.call_count)
        self.assertEqual(['source'],scanned)
        self.assertEqual(1,control.target_queue.qsize())
        queued=control.target_queue.get_nowait(); control.target_queue.task_done()
        self.assertEqual('untouched',queued['username'])

    async def test_shutdown_retries_retained_failed_driver_before_release(self):
        task=self.task(); target=task['targets'][0]['id']; entered=self.gate(); cleanup=self.gate(); release=self.gate(); calls=[0]
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set(); await asyncio.Event().wait()
            async def disconnect(self):
                calls[0]+=1
                if calls[0]==1: raise RuntimeError('first cleanup failed')
                cleanup.set(); await release.wait()
        manager,provider=self.manager(Worker)
        await manager.start(self.owner,task['id']); await asyncio.wait_for(entered.wait(),3)
        self.service.upsert_checkpoint(self.owner,task['id'],target,mode='followers',stage='screening_accounts',
            cursor={'candidate_spool_complete':True,'candidate_spool_natural_end':True},
            counters={'source_total':3,'discovered':1,'processed':1})
        await manager.pause(self.owner,task['id'])
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        token=self.lease_token(); stopping=asyncio.create_task(manager.shutdown())
        await asyncio.wait_for(cleanup.wait(),3)
        self.assertFalse(stopping.done()); self.assertEqual(token,self.lease_token()); self.assertEqual([],provider.closed)
        release.set(); await asyncio.wait_for(stopping,5)
        self.assertEqual({},self.leases())

    async def test_successful_recheck_preserves_history_then_automatic_dismiss_blocks_repeat(self):
        task,target=self.completed_gap(modes=('followers','following'))
        other=self.checkpoint_bytes(target,'following')
        def history():
            with self.database.read() as connection:
                return ([tuple(row) for row in connection.execute('SELECT * FROM split_completed_targets ORDER BY target_id')],
                        [tuple(row) for row in connection.execute('SELECT * FROM split_candidate_history ORDER BY id')])
        before=history(); requested=[]
        self.assertEqual(1,len(before[0])); self.assertEqual(1,len(before[1]))
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers',['found_one'],source_total=3)
        manager,_=self.manager(Worker)
        for attempt in range(1):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
            await asyncio.wait_for(manager.wait(task['id']),5)
            self.assertEqual(before,history())
            self.assertEqual(other,self.checkpoint_bytes(target,'following'))
            with self.database.read() as connection:
                row=connection.execute('SELECT state,requested_at FROM task_source_rechecks WHERE target_id=?',(target,)).fetchone()
                self.assertEqual('completed',row['state'])
                requested.append(row['requested_at'])
            for status in ('pending','failed','recoverable'):
                with self.database.write() as connection:
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute('UPDATE task_targets SET status=? WHERE id=?',(status,target))
            await asyncio.sleep(.002)
        self.assertTrue(self.service.get_task(self.owner,task['id'])['targets'][0]['collection_list_dismissed'])
        with self.assertRaises(ConflictError):
            await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual(before,history())

    async def test_old_database_reinstalls_narrow_lifecycle_guard(self):
        task,target=self.completed_gap()
        with self.database.write() as connection:
            connection.execute('DROP TRIGGER trg_split_target_generation_fence')
            connection.execute("CREATE TRIGGER trg_split_target_generation_fence BEFORE UPDATE OF status ON task_targets WHEN OLD.status='completed' AND NEW.status!='completed' BEGIN SELECT RAISE(ABORT,'old fence'); END")
        self.database.initialize()
        with self.database.write() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE task_targets SET status='pending' WHERE id=?",(target,))
        manager,_=self.manager(manager_type=WaitingManager)
        await manager.recheck_source(self.owner,task['id'],target,'followers')
        self.assertEqual('pending',self.service.get_task(self.owner,task['id'])['targets'][0]['status'])

if __name__=='__main__': unittest.main()
