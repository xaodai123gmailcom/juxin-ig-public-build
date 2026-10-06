"""Specified-window edits cannot cross a delayed queue/recovery generation."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.errors import ConflictError, NotFoundError
from app.service import CoreService


class SpecifiedWindowsR34Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'specified.sqlite')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('specified-r34', 'correct horse battery staple')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def recoverable(self):
        task = self.service.create_task(self.owner, name='source', modes=['followers'],
            targets=['same_target'], window_ids=['window-a', 'window-b'],
            settings={'live_queue_enabled': True, 'location_enabled': False})
        target = task['targets'][0]
        self.service.upsert_checkpoint(self.owner, task['id'], target['id'],
            mode='followers', stage='screening_accounts', cursor={'offset': 5},
            counters={'source_total': 50, 'saved': 3}, recoverable=True)
        self.service.set_target_runtime_status(self.owner, task['id'], target['id'], 'recoverable')
        failure = next(row for row in self.service.list_split_candidates(self.owner) if row['kind'] == 'failure')
        waiting = self.service.requeue_split_candidate(self.owner, failure['id'], allowed_window_ids=['window-a'])
        return task, target, failure, waiting

    def test_old_requeue_cannot_change_a_new_same_name_generation(self):
        task, target, failure, waiting = self.recoverable()
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        self.service.claim_next_split_candidate(self.owner, task['id'], 'window-a')
        self.service.set_target_runtime_status(self.owner, task['id'], target['id'], 'completed', window_id='window-a')
        blocked = self.service.upsert_manual_split_candidates(self.owner,
            [{'username': 'same_target', 'queued': True, 'allowed_window_ids': ['window-b']}],
            include_outcome=True)
        self.assertEqual(0, blocked['accepted_count'])
        self.assertEqual('split_already_executed', blocked['duplicates'][0]['reason'])
        # R93 permits an explicitly confirmed new generation. The old recovery
        # request must still be unable to edit that new generation or its window.
        confirmed = self.service.upsert_manual_split_candidates(self.owner,
            [{'username': 'same_target', 'queued': True, 'allowed_window_ids': ['window-b']}],
            include_outcome=True, allow_completed=True)
        self.assertEqual(1, confirmed['accepted_count'])
        fresh = confirmed['candidates'][0]
        self.assertNotEqual(waiting['id'], fresh['id'])
        for windows in (None, ['window-a']):
            with self.subTest(windows=windows), self.assertRaises(ConflictError):
                self.service.requeue_split_candidate(self.owner, failure['id'], allowed_window_ids=windows)
        current = next(row for row in self.service.list_split_candidates(self.owner) if row['id'] == fresh['id'])
        self.assertEqual(['window-b'], current['allowed_window_ids'])
        self.assertEqual('queued', current['queue_state'])

    def test_delayed_requeue_cannot_undo_a_later_explicit_window_edit(self):
        task, target, failure, waiting = self.recoverable()
        edited = self.service.set_split_candidate_allowed_windows(self.owner, waiting['id'], ['window-b'])
        with self.assertRaises(ConflictError):
            self.service.requeue_split_candidate(self.owner, failure['id'], allowed_window_ids=['window-a'])
        replay = self.service.requeue_split_candidate(self.owner, failure['id'])
        self.assertEqual(edited['id'], replay['id'])
        self.assertEqual(['window-b'], replay['allowed_window_ids'])
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        self.assertIsNone(self.service.claim_next_split_candidate(self.owner, task['id'], 'window-a'))
        claimed = self.service.claim_next_split_candidate(self.owner, task['id'], 'window-b')
        self.assertEqual(target['id'], claimed['id'])
        self.assertEqual(3, self.service.get_checkpoint(self.owner, task['id'], target['id'], 'followers')['counters']['saved'])

    def test_same_requeue_request_is_harmless_after_its_window_claims(self):
        task, target, failure, waiting = self.recoverable()
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        token = self.service.acquire_browser_lease(self.owner, 'window-a', operation_type='collection', entity_id=task['id'])
        try:
            claimed = self.service.claim_next_split_candidate(self.owner, task['id'], 'window-a')
            replay = self.service.requeue_split_candidate(self.owner, failure['id'], allowed_window_ids=['window-a'])
            self.assertEqual(waiting['id'], replay['id'])
            self.assertEqual(claimed['id'], replay['queued_target_id'])
            self.assertEqual('claimed', replay['queue_state'])
            with self.db.read() as connection:
                self.assertEqual(token, connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?', ('window-a',)).fetchone()[0])
            with self.assertRaises(ConflictError):
                self.service.set_split_candidate_allowed_windows(self.owner, waiting['id'], ['window-b'])
        finally:
            self.service.release_browser_lease('window-a', token)

    def test_deleted_waiting_id_cannot_reassign_or_remove_its_replacement(self):
        old = self.service.upsert_manual_split_candidates(self.owner,
            [{'username': 'replace_waiting', 'queued': True, 'allowed_window_ids': ['window-a']}], include_outcome=True)['candidates'][0]
        self.service.delete_waiting_split_candidate(self.owner, old['id'])
        fresh = self.service.upsert_manual_split_candidates(self.owner,
            [{'username': 'replace_waiting', 'queued': True, 'allowed_window_ids': ['window-b']}], include_outcome=True)['candidates'][0]
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_allowed_windows(self.owner, old['id'], ['window-a'])
        with self.assertRaises(NotFoundError):
            self.service.delete_waiting_split_candidate(self.owner, old['id'])
        self.assertEqual(['window-b'], next(row for row in self.service.list_split_candidates(self.owner) if row['id'] == fresh['id'])['allowed_window_ids'])

    def test_paused_source_keeps_recovery_waiting_and_other_task_cannot_claim(self):
        task, target, failure, waiting = self.recoverable()
        other = self.service.create_task(self.owner, name='other', modes=['followers'], targets=['other_seed'],
            window_ids=['window-a'], settings={'live_queue_enabled': True, 'location_enabled': False})
        self.service.set_task_runtime_status(self.owner, other['id'], 'running')
        self.service.set_task_runtime_status(self.owner, task['id'], 'paused')
        self.assertIsNone(self.service.claim_next_split_candidate(self.owner, task['id'], 'window-a'))
        self.assertIsNone(self.service.claim_next_split_candidate(self.owner, other['id'], 'window-a'))
        self.service.set_split_candidate_allowed_windows(self.owner, waiting['id'], ['window-b'])
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        self.assertIsNone(self.service.claim_next_split_candidate(self.owner, task['id'], 'window-a'))
        self.assertEqual(target['id'], self.service.claim_next_split_candidate(self.owner, task['id'], 'window-b')['id'])


if __name__ == '__main__':
    unittest.main()
