"""Completed rechecks dismiss only their card, after exact-window cleanup."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.database import Database
from app.errors import ConflictError, NotFoundError
from app.main import create_app
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from app.schemas import WorkbenchTaskTargetControlPayload
from app.service import CoreService, LIVE_TASK_RECENT_TARGET_LIMIT
from test_core import FakeCollectionWorker
import test_explicit_source_recheck as recheck_cases
from test_runtime_r24 import WaitingManager
from test_spool_performance_r30 import CountingDatabase, seed_task, STAMP


class CompletedCardDismissalR96Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recheck_cases.ExplicitSourceRecheckTests.asyncSetUp
    asyncTearDown = recheck_cases.ExplicitSourceRecheckTests.asyncTearDown
    task = recheck_cases.ExplicitSourceRecheckTests.task
    manager = recheck_cases.ExplicitSourceRecheckTests.manager
    gate = recheck_cases.ExplicitSourceRecheckTests.gate
    until = recheck_cases.ExplicitSourceRecheckTests.until
    leases = recheck_cases.ExplicitSourceRecheckTests.leases
    completed_gap = recheck_cases.ExplicitSourceRecheckTests.completed_gap
    checkpoint_bytes = recheck_cases.ExplicitSourceRecheckTests.checkpoint_bytes
    lease_token = recheck_cases.ExplicitSourceRecheckTests.lease_token

    def row(self, task, target):
        return next(row for row in self.service.get_task(self.owner, task['id'])['targets']
                    if row['id'] == target)

    def rows(self, *tables):
        with self.database.read() as connection:
            return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in tables}

    async def real_gap(self):
        task = self.task()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers', ['already_read'], source_total=3)
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 5)
        return task, task['targets'][0]['id'], manager, provider

    async def check_finished_gap(self, usernames, expected_count, expected_gap):
        # These two explicit-command regressions require a legacy visible gap.
        # Keep real_gap() on the actual automatic path for lifecycle tests.
        original_release = self.service.release_browser_lease
        with patch.object(self.service, 'release_browser_lease',
                          side_effect=lambda profile, token, **kwargs: original_release(profile, token)):
            task, target, manager, _ = await self.real_gap()
        initial = self.row(task, target)
        self.assertIsNone(initial['source_recheck'])
        self.assertFalse(initial['collection_list_dismissed'])
        self.assertEqual(2, initial['mode_coverage']['followers']['unobserved_count'])
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers', usernames, source_total=3)
        manager.worker_factory = Worker
        await manager.recheck_source(self.owner, task['id'], target, 'followers')
        await asyncio.wait_for(manager.wait(task['id']), 5)
        current = self.row(task, target)
        self.assertEqual('completed', current['status'])
        self.assertEqual('completed', current['source_recheck']['state'])
        self.assertEqual('followers', current['source_recheck']['mode'])
        self.assertTrue(current['source_recheck']['completed_at'])
        self.assertEqual(expected_gap, current['mode_coverage']['followers']['unobserved_count'])
        self.assertEqual(expected_count, self.service.task_mode_candidate_stats(
            self.owner, task['id'], target, 'followers')['recorded'])
        for view in (self.service.list_tasks_with_details(self.owner)[0],
                     self.service.get_task_live_status(self.owner, task['id'], current_target_ids=[])):
            projected = next(row for row in view['targets'] if row['id'] == target)
            self.assertEqual(current['source_recheck'], projected['source_recheck'])
        await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertTrue(self.row(task, target)['collection_list_dismissed'])

    async def test_recheck_no_new_accounts_completes_with_actual_gap(self):
        await self.check_finished_gap([], 1, 2)

    async def test_recheck_more_accounts_completes_with_remaining_gap(self):
        await self.check_finished_gap(['already_read', 'new_account'], 2, 1)

    async def test_pending_spool_must_drain_before_recheck_completion_and_dismissal(self):
        task, target = self.completed_gap()
        reading = self.gate(); release = self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers', ['pending_account'], source_total=3)
            async def read_visible_profile(self, username, **kwargs):
                reading.set(); await release.wait()
                return await super().read_visible_profile(username, **kwargs)
        manager, _ = self.manager(Worker)
        await manager.recheck_source(self.owner, task['id'], target, 'followers')
        await asyncio.wait_for(reading.wait(), 3)
        self.assertEqual(1, self.service.task_mode_candidate_stats(self.owner, task['id'], target, 'followers')['pending'])
        self.assertEqual('active', self.row(task, target)['source_recheck']['state'])
        self.assertEqual('recoverable', self.service.source_recheck_completion_status(self.owner, task['id'], target, 'followers'))
        with self.assertRaises(ConflictError):
            await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertEqual([], self.rows('task_target_list_dismissals')['task_target_list_dismissals'])
        release.set(); await asyncio.wait_for(manager.wait(task['id']), 5)
        self.assertEqual('completed', self.row(task, target)['status'])

    async def assert_source_error_not_completed(self, reason):
        task, target = self.completed_gap()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                raise WorkerExecutionError('source failed', reason=reason, pause_required=True)
        manager, _ = self.manager(Worker)
        with patch.object(manager, '_recover_network_connection', AsyncMock(return_value=None)):
            await manager.recheck_source(self.owner, task['id'], target, 'followers')
            await asyncio.wait_for(manager.wait(task['id']), 5)
        self.assertNotEqual('completed', self.row(task, target)['status'])
        self.assertEqual('active', self.row(task, target)['source_recheck']['state'])
        self.assertIsNone(self.row(task, target)['source_recheck']['completed_at'])
        with self.assertRaises(ConflictError):
            self.service.dismiss_completed_task_target(self.owner, task['id'], target)

    async def test_source_error_never_completes_recheck(self):
        await self.assert_source_error_not_completed('source_unavailable')

    async def test_login_error_never_completes_recheck(self):
        await self.assert_source_error_not_completed('instagram_login_required')

    async def test_rate_limit_never_completes_recheck(self):
        await self.assert_source_error_not_completed('instagram_rate_limited')

    async def test_interrupted_recheck_never_completes(self):
        task, target = self.completed_gap(); entered = self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                entered.set(); await asyncio.Event().wait()
        manager, _ = self.manager(Worker)
        await manager.recheck_source(self.owner, task['id'], target, 'followers')
        await asyncio.wait_for(entered.wait(), 3)
        await manager.stop(self.owner, task['id'])
        self.assertNotEqual('completed', self.row(task, target)['status'])
        self.assertEqual('active', self.row(task, target)['source_recheck']['state'])
        with self.assertRaises(ConflictError):
            await manager.dismiss_completed_target(self.owner, task['id'], target)

    async def test_dismissal_preserves_data_history_siblings_and_foreign_window_lease(self):
        task, target, manager, provider = await self.real_gap()
        # Seed actual completion history too; the recheck fixture separately verifies its trigger.
        with self.database.write() as connection:
            connection.execute("INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_task_id,source_target_id,source_status,completed_at,created_at,updated_at) VALUES('saved-history',?,'source','source',?,?,'completed',?,?,?)",
                               (self.owner, task['id'], target, task['created_at'], task['created_at'], task['created_at']))
        sibling = self.task(sources=('sibling',), windows=('window-b',))
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='action', entity_id='unrelated-action')
        before = self.rows('tasks', 'task_targets', 'task_windows', 'task_checkpoints', 'task_results',
                           'task_mode_candidates', 'global_seen', 'split_completed_targets', 'split_candidate_history',
                           'task_target_recovery_controls', 'browser_operation_leases')
        closed = list(provider.closed)
        result = await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertTrue(result['collection_list_dismissed'])
        self.assertEqual(before, self.rows(*before))
        self.assertFalse(self.row(sibling, sibling['targets'][0]['id'])['collection_list_dismissed'])
        self.assertEqual(closed, provider.closed)
        self.assertEqual(token, self.lease_token())
        self.service.release_browser_lease('window-a', token)

    async def test_repeated_dismissal_restart_and_every_projection_are_stable(self):
        task, target = self.completed_gap(); manager, _ = self.manager()
        await manager.dismiss_completed_target(self.owner, task['id'], target)
        before = self.rows('task_target_list_dismissals', 'event_log')
        await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertEqual(before, self.rows(*before))
        database = Database(self.database.path); database.initialize(); service = CoreService(database)
        for view in (service.get_task(self.owner, task['id']),
                     service.list_tasks_with_details(self.owner, limit=1, detail_limit=5)[0],
                     service.get_task_live_status(self.owner, task['id'], current_target_ids=[])):
            self.assertTrue(view['targets'][0]['collection_list_dismissed'])
        self.assertEqual(1, len(service.list_tasks_with_details(self.owner)))

    async def test_live_overlay_includes_old_dismissal_without_mutating_target_timestamp(self):
        task = self.task(sources=tuple('source_' + str(i) for i in range(LIVE_TASK_RECENT_TARGET_LIMIT + 5)))
        target = task['targets'][0]['id']
        self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed', window_id='window-a')
        with self.database.write() as connection:
            connection.execute("UPDATE task_targets SET updated_at='2000-01-01' WHERE id=?", (target,))
        self.assertNotIn(target, {row['id'] for row in self.service.get_task_live_status(self.owner, task['id'], current_target_ids=[])['targets']})
        before = self.rows('task_targets')
        self.service.dismiss_completed_task_target(self.owner, task['id'], target)
        self.assertEqual(before, self.rows('task_targets'))
        overlay = self.service.get_task_live_status(self.owner, task['id'], current_target_ids=[])
        self.assertTrue(next(row for row in overlay['targets'] if row['id'] == target)['collection_list_dismissed'])

    async def test_owner_wrong_target_and_unfinished_status_are_rejected(self):
        task, target = self.completed_gap()
        other_owner = self.service.register_user('another-owner', 'test password long enough')['id']
        other_task = self.task(sources=('other_source',))
        for owner, task_id, target_id, error in (
            (other_owner, task['id'], target, NotFoundError),
            (self.owner, other_task['id'], target, NotFoundError),
            (self.owner, task['id'], 'missing', NotFoundError),
            (self.owner, other_task['id'], other_task['targets'][0]['id'], ConflictError),
        ):
            with self.assertRaises(error):
                self.service.dismiss_completed_task_target(owner, task_id, target_id)
        self.assertEqual([], self.rows('task_target_list_dismissals')['task_target_list_dismissals'])

    async def test_dismiss_wins_race_recheck_never_acquires_or_cleans(self):
        task, target = self.completed_gap(); manager, provider = self.manager()
        entered = self.gate(threaded=True); release = self.gate(threaded=True)
        original = self.service.dismiss_completed_task_target
        def blocked(*args):
            entered.set(); self.assertTrue(release.wait(3)); return original(*args)
        with patch.object(self.service, 'dismiss_completed_task_target', blocked), \
             patch.object(self.service, 'acquire_browser_lease_async', wraps=self.service.acquire_browser_lease_async) as acquire:
            dismiss = asyncio.create_task(manager.dismiss_completed_target(self.owner, task['id'], target))
            await self.until(entered.is_set)
            recheck = asyncio.create_task(manager.recheck_source(self.owner, task['id'], target, 'followers'))
            await asyncio.sleep(.02); self.assertFalse(recheck.done()); self.assertEqual(0, acquire.call_count)
            release.set(); await dismiss
            with self.assertRaises(ConflictError) as caught: await recheck
            self.assertEqual('collection_card_dismissed', caught.exception.details['reason'])
            self.assertEqual(0, acquire.call_count)
        self.assertEqual([], provider.closed); self.assertEqual({}, self.leases())

    async def test_recheck_wins_race_dismiss_cannot_hide_requeued_source(self):
        task, target = self.completed_gap(task_status='paused'); manager, _ = self.manager(manager_type=WaitingManager)
        entered = self.gate(); release = self.gate(); original = manager._recheck_source_owned
        async def blocked(*args):
            entered.set(); await release.wait(); return await original(*args)
        with patch.object(manager, '_recheck_source_owned', blocked):
            recheck = asyncio.create_task(manager.recheck_source(self.owner, task['id'], target, 'followers'))
            await entered.wait()
            dismiss = asyncio.create_task(manager.dismiss_completed_target(self.owner, task['id'], target))
            await asyncio.sleep(.02); self.assertFalse(dismiss.done())
            release.set(); await recheck
            with self.assertRaises(ConflictError): await dismiss
        self.assertEqual('pending', self.row(task, target)['status'])
        self.assertFalse(self.row(task, target)['collection_list_dismissed'])

    async def test_service_transaction_guard_blocks_direct_recheck_after_dismissal(self):
        task, target = self.completed_gap()
        self.service.dismiss_completed_task_target(self.owner, task['id'], target)
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        before = self.checkpoint_bytes(target)
        with self.assertRaises(ConflictError) as caught:
            self.service.recheck_task_source(self.owner, task['id'], target, 'followers', profile_id='window-a', lease_token=token)
        self.assertEqual('collection_card_dismissed', caught.exception.details['reason'])
        self.assertEqual(before, self.checkpoint_bytes(target)); self.assertEqual(token, self.lease_token())
        self.service.release_browser_lease('window-a', token)

    async def test_completed_card_stays_during_disconnect_then_dismisses_after_release(self):
        task, target = self.completed_gap(); entered = self.gate(); release = self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                return CollectionOutcome('followers', [], source_total=3)
            async def disconnect(self):
                entered.set(); await release.wait()
        manager, _ = self.manager(Worker)
        await manager.recheck_source(self.owner, task['id'], target, 'followers')
        await asyncio.wait_for(entered.wait(), 3)
        self.assertEqual('completed', self.row(task, target)['status'])
        with self.assertRaises(ConflictError) as caught:
            await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertEqual('collection_card_cleanup_pending', caught.exception.details['reason'])
        self.assertFalse(self.row(task, target)['collection_list_dismissed'])
        release.set(); await asyncio.wait_for(manager.wait(task['id']), 5)
        await manager.dismiss_completed_target(self.owner, task['id'], target)

    async def test_close_failed_card_stays_until_exact_lease_close_succeeds(self):
        task, target = self.completed_gap(); manager, provider = self.manager()
        provider.result = {'closed': False}
        await manager.recheck_source(self.owner, task['id'], target, 'followers')
        control = manager._runs[task['id']]
        await self.until(lambda: control.profile_states.get('window-a', {}).get('reason') == 'browser_close_failed')
        self.assertEqual('completed', self.row(task, target)['status'])
        self.assertIsNone(control.profile_states['window-a']['current_target_id'])
        with self.assertRaises(ConflictError) as caught:
            await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertEqual('collection_card_cleanup_pending', caught.exception.details['reason'])
        self.assertFalse(self.row(task, target)['collection_list_dismissed'])
        provider.result = {'closed': True}
        self.assertTrue(await manager._close_drained_window(control, 'window-a'))
        await asyncio.wait_for(manager.wait(task['id']), 5)
        await manager.dismiss_completed_target(self.owner, task['id'], target)

    async def test_unrelated_live_sibling_does_not_block_or_get_stopped(self):
        task = self.task(sources=('complete_source', 'sibling'), windows=('window-a',))
        target, sibling = [row['id'] for row in task['targets']]
        entered = self.gate(); release = self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, username, **kwargs):
                if username == 'sibling':
                    entered.set(); await release.wait()
                return CollectionOutcome('followers', [], source_total=0)
        manager, provider = self.manager(Worker)
        # Hold the first heartbeat after its initial renewal, so the exact-row
        # assertion measures dismissal rather than a concurrent periodic renew.
        manager.lease_heartbeat_interval_seconds = 60
        await manager.start(self.owner, task['id']); await asyncio.wait_for(entered.wait(), 3)
        control = manager._runs[task['id']]; token = self.lease_token()
        before = self.rows('task_targets', 'task_windows', 'task_checkpoints', 'browser_operation_leases')
        await manager.dismiss_completed_target(self.owner, task['id'], target)
        self.assertEqual(before, self.rows(*before)); self.assertEqual(token, self.lease_token())
        self.assertFalse(control.stop_event.is_set()); self.assertFalse(control.profile_stop_events['window-a'].is_set())
        self.assertEqual(sibling, control.profile_states['window-a']['current_target_id'])
        self.assertEqual([], provider.closed)
        release.set(); await asyncio.wait_for(manager.wait(task['id']), 5)

    async def test_http_schema_dispatch_platform_owner_and_wrong_target_guards(self):
        task, target = self.completed_gap()
        other_task = self.task(sources=('other',))
        manager, provider = self.manager()
        settings = Settings(startup_token='dismiss-card-test-startup-token', database_path=self.database.path, data_dir=self.database.path.parent)
        app = create_app(settings, database=self.database, bitbrowser=provider)
        login = self.service.login('drain-r56', 'test password long enough')
        headers = {'X-Startup-Token': settings.startup_token, 'Authorization': 'Bearer ' + login['token']}
        self.assertEqual('dismiss_completed', WorkbenchTaskTargetControlPayload(task_id=task['id'], target_id=target, action='dismiss_completed').action)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
            payload = {'task_id': task['id'], 'target_id': target, 'action': 'dismiss_completed', 'platform': 'instagram'}
            for changed, expected in (({'platform': 'facebook'}, 422), ({'task_id': other_task['id']}, 404)):
                response = await client.post('/api/workbench/commands', json={'command': 'task_target_control', 'payload': {**payload, **changed}})
                self.assertEqual(expected, response.status_code, response.text)
            response = await client.post('/api/workbench/commands', json={'command': 'task_target_control', 'payload': payload})
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()['result']['collection_list_dismissed'])
        self.assertTrue(self.row(task, target)['collection_list_dismissed'])


class CompletedCardDismissalPerformanceR96Tests(unittest.TestCase):
    """Measure real SQLite work, including tasks unrelated to the heartbeat."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CountingDatabase(Path(self.temp.name) / 'dismissal-performance.sqlite3')
        self.database.initialize()
        seed_task(self.database)
        self.service = CoreService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def seed_markers(self, start, count, *, task='task', owner='owner', prefix='archive'):
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                "current_window_id,current_stage,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'completed','historical-window','completed_archived',?,?)",
                ((f'{prefix}{index:06d}', task, f'{prefix}{index:06d}', f'{prefix}{index:06d}',
                  index + 2, STAMP, STAMP) for index in range(start, start + count)),
            )
            connection.executemany(
                "INSERT INTO task_target_list_dismissals(target_id,task_id,owner_user_id,dismissed_at) "
                "VALUES(?,?,?,?)",
                ((f'{prefix}{index:06d}', task, owner, STAMP) for index in range(start, start + count)),
            )

    def live(self):
        return self.service.get_task_live_status('owner', 'task', current_target_ids=('target',))

    def test_twenty_thousand_own_markers_keep_selection_bounded(self):
        self.seed_markers(0, 1000)
        small, small_steps = self.database.measure(self.live)
        self.seed_markers(1000, 19_000)
        large, large_steps = self.database.measure(self.live)
        self.assertEqual(len(small['targets']), len(large['targets']))
        self.assertLessEqual(len(large['targets']), 2 * LIVE_TASK_RECENT_TARGET_LIMIT + 1)
        self.assertEqual(LIVE_TASK_RECENT_TARGET_LIMIT,
                         sum(row['collection_list_dismissed'] for row in large['targets']))
        self.assertLessEqual(large_steps, small_steps + 2000, (small_steps, large_steps))

    def test_unrelated_task_and_owner_markers_do_not_enter_live_scan(self):
        self.seed_markers(0, LIVE_TASK_RECENT_TARGET_LIMIT)
        seed_task(self.database, task='other-task', target='other-target')
        seed_task(self.database, task='foreign-task', target='foreign-target', owner='foreign-owner')
        self.seed_markers(0, 1000, task='other-task', prefix='other')
        self.seed_markers(0, 1000, task='foreign-task', owner='foreign-owner', prefix='foreign')
        small, small_steps = self.database.measure(self.live)
        self.seed_markers(1000, 19_000, task='other-task', prefix='other')
        self.seed_markers(1000, 19_000, task='foreign-task', owner='foreign-owner', prefix='foreign')
        large, large_steps = self.database.measure(self.live)
        self.assertEqual(small, large)
        self.assertEqual(LIVE_TASK_RECENT_TARGET_LIMIT,
                         sum(row['collection_list_dismissed'] for row in large['targets']))
        self.assertLessEqual(large_steps, small_steps + 2000, (small_steps, large_steps))

    def test_old_marker_schema_migrates_without_changing_dismissal_or_history(self):
        self.seed_markers(0, 1)
        with self.database.write() as connection:
            before_target = tuple(connection.execute("SELECT * FROM task_targets WHERE id='archive000000'").fetchone())
            before_marker = tuple(connection.execute(
                "SELECT target_id,owner_user_id,dismissed_at FROM task_target_list_dismissals"
            ).fetchone())
            connection.execute('DROP TABLE task_target_list_dismissals')
            connection.execute("CREATE TABLE task_target_list_dismissals ("
                               "target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,"
                               "owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,"
                               "dismissed_at TEXT NOT NULL)")
            connection.execute('INSERT INTO task_target_list_dismissals VALUES(?,?,?)', before_marker)
        self.database.initialize()
        self.database.initialize()  # Idempotent after restart, including the backfill.
        with self.database.read() as connection:
            after_marker = connection.execute('SELECT * FROM task_target_list_dismissals').fetchone()
            self.assertEqual('task', after_marker['task_id'])
            self.assertEqual(before_marker, tuple(after_marker[key] for key in ('target_id', 'owner_user_id', 'dismissed_at')))
            self.assertEqual(before_target, tuple(connection.execute("SELECT * FROM task_targets WHERE id='archive000000'").fetchone()))
            plan = ' '.join(str(tuple(row)) for row in connection.execute(
                'EXPLAIN QUERY PLAN SELECT target_id FROM task_target_list_dismissals '
                'WHERE owner_user_id=? AND task_id=? ORDER BY dismissed_at DESC,target_id DESC LIMIT ?',
                ('owner', 'task', LIVE_TASK_RECENT_TARGET_LIMIT),
            ))
            self.assertIn('COVERING INDEX idx_task_target_list_dismissals_recent', plan)
            self.assertNotIn('TEMP B-TREE', plan)
        self.assertTrue(next(row for row in self.live()['targets'] if row['id'] == before_marker[0])['collection_list_dismissed'])


if __name__ == '__main__':
    unittest.main()
