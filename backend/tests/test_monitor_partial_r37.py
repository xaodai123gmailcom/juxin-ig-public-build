"""Persist partial positive observations without guessing missing unfollows."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.follow_monitor import FollowMonitorManager
from app.playwright_worker import WorkerExecutionError
from app.service import CoreService


class MonitorPartialR37Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'monitor.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('r37-monitor', 'correct horse battery staple')['id']
        self.manager = FollowMonitorManager(self.service, SimpleNamespace())

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, run, names, total=None):
        return self.manager._commit_following_scan(self.owner, run, 'window', 'identity', 'self', set(names), source_total=len(names) if total is None else total)

    def worker(self, names, total):
        return SimpleNamespace(page=SimpleNamespace(), collect_following=AsyncMock(return_value=SimpleNamespace(usernames=list(names), source_total=total)))

    def scan(self, worker, run='r2', identity='identity'):
        return asyncio.run(self.manager._scan_following_stage(worker, self.owner, run, 'window', identity, 'self'))

    def members(self):
        with self.db.read() as connection:
            return {row[0] for row in connection.execute('SELECT m.username FROM follow_monitor_members m JOIN follow_monitor_accounts a ON a.owner_user_id=m.owner_user_id AND a.profile_id=m.profile_id AND a.generation=m.generation')}

    def test_first_32_of_33_read_is_saved_as_baseline(self):
        names = {f'account{i}' for i in range(32)}
        worker = self.worker(names, 33)
        self.assertEqual((0, 0, 0), self.scan(worker, 'first'))
        self.assertEqual(names, self.members())
        account = self.manager.snapshot(self.owner)['accounts'][0]
        self.assertEqual((32, 33, 'completed'), (account['following_count'], account['homepage_count'], account['last_status']))
        self.assertEqual(0, account['baseline_verified'])
        self.assertEqual(2, worker.collect_following.await_count)

    def test_partial_saves_new_names_and_keeps_absent_baseline_until_complete(self):
        self.commit('seed', {'kept', 'missing'})
        self.assertEqual((1, 0, 0), self.scan(self.worker({'kept', 'new'}, 3)))
        self.assertEqual({'kept', 'missing', 'new'}, self.members())
        self.assertEqual((0, 0, 0), self.scan(self.worker({'kept', 'new'}, 3), 'r3'))
        account = self.manager.snapshot(self.owner)['accounts'][0]
        self.assertEqual(2, account['previous_following_count'], 'previous actual count is observed count, not union size')
        self.assertEqual((0, 1, 0), self.scan(self.worker({'kept', 'new'}, 2), 'r4'))
        snapshot = self.manager.snapshot(self.owner)
        self.assertEqual({'kept', 'new'}, self.members())
        self.assertEqual(['missing'], [row['username'] for row in snapshot['latest_unfollow']])
        self.assertEqual(1, snapshot['accounts'][0]['baseline_verified'])
        self.assertEqual((1, 0, 1), self.scan(self.worker({'kept', 'new', 'missing'}, 3), 'r5'))

    def test_even_99_of_100_never_guesses_missing_account_unfollowed(self):
        names = {f'account{i}' for i in range(100)}
        self.commit('seed', names)
        self.assertEqual((0, 0, 0), self.scan(self.worker(names - {'account0'}, 100)))
        self.assertEqual(names, self.members())

    def test_unknown_header_still_saves_new_names_and_keeps_missing(self):
        self.commit('seed', {'old'})
        self.assertEqual((1, 0, 0), self.scan(self.worker({'new'}, None)))
        self.assertEqual({'old', 'new'}, self.members())

    def test_both_scroll_attempts_stall_but_confirmed_rows_are_saved(self):
        self.commit('seed', {'old'})
        worker = self.worker(set(), 33)
        worker.last_relation_partial_usernames = ['new']
        worker.last_relation_source_total = 33
        worker._replace_stuck_page_once = AsyncMock()
        worker._finish_page_recovery = AsyncMock()
        worker.collect_following.side_effect = WorkerExecutionError('scroll stalled', reason='instagram_following_list_incomplete')
        self.assertEqual((1, 0, 0), self.scan(worker))
        self.assertEqual({'old', 'new'}, self.members())
        self.assertEqual(2, worker.collect_following.await_count)
        worker._finish_page_recovery.assert_awaited_once_with(progressed=False)

    def test_zero_rows_with_positive_header_and_scroll_error_remains_failure(self):
        self.commit('seed', {'old'})
        before = self.manager.snapshot(self.owner)['accounts']
        worker = self.worker(set(), 33)
        worker.last_relation_partial_usernames = []
        worker.last_relation_source_total = 33
        worker._replace_stuck_page_once = AsyncMock()
        worker._finish_page_recovery = AsyncMock()
        worker.collect_following.side_effect = WorkerExecutionError('nothing rendered', reason='instagram_following_list_not_rendered')
        with self.assertRaises(WorkerExecutionError):
            self.scan(worker)
        self.assertEqual(before, self.manager.snapshot(self.owner)['accounts'])
        self.assertEqual({'old'}, self.members())

    def test_explicit_successful_zero_can_confirm_unfollow(self):
        self.commit('seed', {'old'})
        worker = self.worker(set(), 0)
        self.assertEqual((0, 1, 0), self.scan(worker))
        self.assertEqual(set(), self.members())
        self.assertEqual(1, worker.collect_following.await_count)

    def test_union_reaching_total_is_not_a_complete_final_observation(self):
        self.commit('seed', {'kept', 'old'})
        worker = self.worker(set(), 3)
        worker.collect_following.side_effect = [
            SimpleNamespace(usernames=['kept', 'first'], source_total=3),
            SimpleNamespace(usernames=['kept', 'second'], source_total=3),
        ]
        self.assertEqual((2, 0, 0), self.scan(worker))
        self.assertEqual({'kept', 'old', 'first', 'second'}, self.members())
        self.assertEqual(0, self.manager.snapshot(self.owner)['accounts'][0]['baseline_verified'])

    def test_cancellation_at_commit_boundary_preserves_saved_baseline(self):
        self.commit('seed', {'old'})
        before = self.manager.snapshot(self.owner)['accounts']
        worker = self.worker({'new'}, 1)
        worker.monitor_checkpoint = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            self.scan(worker)
        self.assertEqual(before, self.manager.snapshot(self.owner)['accounts'])
        self.assertEqual({'old'}, self.members())

    def test_network_failure_is_not_downgraded_to_success(self):
        self.commit('seed', {'old'})
        before = self.manager.snapshot(self.owner)['accounts']
        worker = self.worker(set(), 33)
        worker.last_relation_partial_usernames = ['new']
        worker.last_relation_source_total = 33
        worker.collect_following.side_effect = WorkerExecutionError('network offline', reason='instagram_network_error')
        with self.assertRaises(WorkerExecutionError):
            self.scan(worker)
        self.assertEqual(before, self.manager.snapshot(self.owner)['accounts'])
        self.assertEqual(1, worker.collect_following.await_count)

    def test_changed_identity_starts_its_own_partial_baseline(self):
        self.commit('seed', {'old'})
        self.assertEqual((0, 0, 0), self.scan(self.worker({'new'}, 33), identity='different'))
        self.assertEqual({'new'}, self.members())
        with self.db.read() as connection:
            self.assertEqual({'old'}, {row[0] for row in connection.execute("SELECT username FROM follow_monitor_seen WHERE instagram_user_id='identity'")})


if __name__ == '__main__':
    unittest.main()
