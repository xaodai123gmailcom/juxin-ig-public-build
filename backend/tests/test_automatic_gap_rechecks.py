"""Natural-source completion permits exactly one durable gap recheck."""
import asyncio
import unittest

from app.errors import ConflictError
from app.playwright_worker import CollectionOutcome
from app.service import CoreService
from test_core import FakeCollectionWorker
import test_collection_drain_r56 as drain


class NaturalGapCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain.CollectionDrainR56Tests.asyncTearDown
    task = drain.CollectionDrainR56Tests.task
    manager = drain.CollectionDrainR56Tests.manager
    gate = drain.CollectionDrainR56Tests.gate
    until = drain.CollectionDrainR56Tests.until
    leases = drain.CollectionDrainR56Tests.leases

    async def test_natural_gap_finishes_after_one_extra_with_truthful_counts_and_saved_dedup(self):
        task = self.task()
        calls, reads = [], []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                calls.append(username)
                return CollectionOutcome('followers', ['seen_account'], source_total=3)
            async def read_visible_profile(self, username, **kwargs):

                if not kwargs.get('include_activity'):
                    reads.append(username)
                return await super().read_visible_profile(username, **kwargs)
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 15)
        self.assertEqual(2, len(calls))
        self.assertEqual(['seen_account'], reads)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', current['status'])
        self.assertTrue(current['collection_list_dismissed'])
        self.assertEqual('automatic', current['completion_policy'])
        coverage = current['mode_coverage']['followers']
        self.assertEqual(2, coverage['unobserved_count'])
        self.assertEqual(1, coverage['discovered'])
        self.assertEqual(1, coverage['processed'])
        self.assertNotIn('automatic_recheck', coverage)
        self.assertEqual({}, self.leases())
        self.database.initialize()
        restarted = CoreService(self.database)
        restored = restarted.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', restored['status'])
        self.assertEqual(2, restored['mode_coverage']['followers']['unobserved_count'])
        self.assertNotIn('automatic_recheck', restored['mode_coverage']['followers'])
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM task_results WHERE target_id=?', (current['id'],)).fetchone()[0])
            self.assertGreaterEqual(connection.execute('SELECT COUNT(*) FROM global_seen').fetchone()[0], 1)
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM split_completed_targets WHERE target_id=?', (current['id'],)).fetchone()[0])

    async def test_repeated_recognized_identity_is_not_screened_twice(self):
        task = self.task()
        scans, reads = [], []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                scans.append(username)
                return CollectionOutcome('followers', ['first', 'first', 'second'], source_total=2)
            async def read_visible_profile(self, username, **kwargs):

                if not kwargs.get('include_activity'):
                    reads.append(username)
                return await super().read_visible_profile(username, **kwargs)
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertEqual(1, len(scans))
        self.assertEqual(['first', 'second'], reads)
        self.assertEqual(2, self.service.task_mode_candidate_stats(self.owner, task['id'], task['targets'][0]['id'], 'followers')['recorded'])

    async def test_no_gap_completion_waits_for_disconnect_and_provider_close(self):
        task = self.task()
        entered, release = self.gate(), self.gate()
        class Worker(FakeCollectionWorker):
            async def disconnect(self):
                entered.set()
                await release.wait()
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(entered.wait(), 5)
        target = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', target['status'])
        self.assertFalse(target['collection_list_dismissed'])
        self.assertEqual('automatic', target['completion_policy'])
        def before_close(profile):
            self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])
            self.assertIn(profile, self.leases())
        provider.on_close = before_close
        release.set()
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertTrue(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])

    async def test_disconnect_failure_never_dismisses_completed_card(self):
        task = self.task()
        failed = self.gate()
        attempts = []
        class Worker(FakeCollectionWorker):
            async def disconnect(self):
                attempts.append(1)
                if len(attempts) == 1:
                    failed.set()
                    raise RuntimeError('cleanup failed')
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(failed.wait(), 5)
        await self.until(lambda: not manager._runs[task['id']].worker_tasks)
        self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])
        self.assertIn('window-a', self.leases())
        result = await manager.stop_window(self.owner, task['id'], 'window-a')
        self.assertEqual('closed', result['status'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertTrue(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])

    async def test_stop_after_natural_end_resumes_details_without_whole_list_replay(self):
        task = self.task()
        entered = self.gate()
        scans = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                scans.append(username)
                return CollectionOutcome('followers', ['seen', 'pending'], source_total=3)
            async def read_visible_profile(self, username, **kwargs):
                if username == 'pending':
                    entered.set()
                    await asyncio.Event().wait()
                return await super().read_visible_profile(username, **kwargs)
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(entered.wait(), 5)
        await manager.stop(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        target = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertNotEqual('completed', target['status'])
        self.assertFalse(target['collection_list_dismissed'])
        self.database.initialize()
        self.service.recover_interrupted_operations()
        resumed_reads = []
        class Resumed(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                raise AssertionError('A naturally ended source must not be replayed for a gap')
            async def read_visible_profile(self, username, **kwargs):
                if username == 'seen':
                    raise AssertionError('Previously recognized identity must not enter details again')
                if not kwargs.get('include_activity'):
                    resumed_reads.append(username)
                return await super().read_visible_profile(username, **kwargs)
        resumed, _ = self.manager(Resumed)
        await resumed.retry_target(self.owner, task['id'], target['id'])
        await asyncio.wait_for(resumed.wait(task['id']), 10)
        self.assertEqual(['source', 'source'], scans)
        self.assertEqual(['pending'], resumed_reads)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual(1, current['mode_coverage']['followers']['unobserved_count'])
        self.assertTrue(current['collection_list_dismissed'])

    async def test_only_gapped_relation_mode_gets_one_extra_pass(self):
        task = self.service.create_task(self.owner, name='modes', modes=['followers', 'following'],
            targets=['source'], window_ids=['window-a'], settings={'live_queue_enabled': True, 'location_enabled': False})
        calls = {'followers': 0, 'following': 0}
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                calls['followers'] += 1
                return CollectionOutcome('followers', ['one'], source_total=2)
            async def collect_following(self, username, **kwargs):
                calls['following'] += 1
                return CollectionOutcome('following', ['two'], source_total=1)
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 15)
        self.assertEqual({'followers': 2, 'following': 1}, calls)

    async def test_completed_legacy_gap_unchanged_by_startup(self):
        task = self.task()
        target = task['targets'][0]['id']
        self.service.upsert_checkpoint(self.owner, task['id'], target, mode='followers', stage='mode_completed',
            cursor={'candidate_spool_complete': True, 'candidate_spool_natural_end': True},
            counters={'source_total': 3, 'discovered': 1, 'processed': 1})
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed', window_id='window-a')
        self.service.finalize_task_runtime_status(self.owner, task['id'], 'completed')
        before = self.service.get_checkpoint(self.owner, task['id'], target, 'followers')
        self.service.recover_interrupted_operations()
        self.assertEqual(before, self.service.get_checkpoint(self.owner, task['id'], target, 'followers'))
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', current['status'])
        self.assertIsNone(current['completion_policy'])


    async def test_restart_natural_end_checkpoint_does_not_schedule_gap_replay(self):
        task = self.task()
        target = task['targets'][0]['id']
        self.service.upsert_checkpoint(self.owner, task['id'], target, mode='followers', stage='mode_completed',
            cursor={'candidate_spool_complete': True, 'candidate_spool_natural_end': True},
            counters={'source_total': 3, 'discovered': 0, 'processed': 0})
        self.database.initialize()
        self.service.recover_interrupted_operations()
        class Resumed(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                raise AssertionError('Confirmed natural source end must not trigger automatic gap rechecks')
        manager, _ = self.manager(Resumed)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', current['status'])
        self.assertEqual(3, current['mode_coverage']['followers']['unobserved_count'])
        self.assertTrue(current['collection_list_dismissed'])

    async def test_missing_or_replaced_lease_never_authorizes_automatic_dismissal(self):
        task = self.task()
        target = task['targets'][0]['id']
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed',
            window_id='window-a', automatic_completion_token=token)
        self.service.release_browser_lease('window-a', token)  # not proof of cleanup
        replacement = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        self.service.release_browser_lease('window-a', token, completed_cleanup=True)
        self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])
        with self.database.read() as connection:
            self.assertEqual(replacement, connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?', ('window-a',)).fetchone()[0])
        self.service.release_browser_lease('window-a', replacement)
        self.service.recover_interrupted_operations()
        self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])

    async def test_release_and_dismissal_rollback_together(self):
        import sqlite3
        task = self.task()
        target = task['targets'][0]['id']
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed',
            window_id='window-a', automatic_completion_token=token)
        with self.database.write() as connection:
            connection.execute("CREATE TRIGGER fail_dismiss BEFORE INSERT ON task_target_list_dismissals BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.release_browser_lease('window-a', token, completed_cleanup=True)
        self.assertIn(token, self.database.live_browser_lease_tokens)
        self.assertIn('window-a', self.leases())
        self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])
        with self.database.write() as connection:
            connection.execute('DROP TRIGGER fail_dismiss')
        self.service.release_browser_lease('window-a', token, completed_cleanup=True)
        self.assertNotIn(token, self.database.live_browser_lease_tokens)
        self.assertTrue(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])

    async def test_sequential_sources_wait_for_same_owned_window_release(self):
        task = self.task(sources=('first_source', 'second_source'))
        entered, release = self.gate(), self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                if username == 'second_source':
                    entered.set()
                    await release.wait()
                return CollectionOutcome('followers', [username + '_fan'], source_total=1)
        manager, _ = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(entered.wait(), 5)
        targets = self.service.get_task(self.owner, task['id'])['targets']
        self.assertEqual('completed', targets[0]['status'])
        self.assertEqual('running', targets[1]['status'])
        self.assertFalse(targets[0]['collection_list_dismissed'])
        self.assertEqual(targets[0]['current_window_id'], targets[1]['current_window_id'])
        self.assertIn('window-a', self.leases())
        release.set()
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertTrue(all(target['collection_list_dismissed'] for target in self.service.get_task(self.owner, task['id'])['targets']))

    async def test_automatic_completion_intent_cannot_bypass_other_mode_pending_fence(self):
        task = self.service.create_task(self.owner, name='pending other mode', modes=['followers', 'following'],
            targets=['source'], window_ids=['window-a'], settings={'live_queue_enabled': True})
        target = task['targets'][0]['id']
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'running', window_id='window-a')
        self.service.append_task_mode_candidates(self.owner, task['id'], target, 'following', ['not_processed'])
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed',
                window_id='window-a', automatic_completion_token=token)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('running', current['status'])
        self.assertIsNone(current['completion_policy'])
        self.assertFalse(current['collection_list_dismissed'])
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM task_automatic_completions WHERE target_id=?', (target,)).fetchone()[0])
        self.service.release_browser_lease('window-a', token)
