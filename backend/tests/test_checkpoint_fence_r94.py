"""Late checkpoint writers cannot revive archived work or erase durable progress."""
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from app.database import Database
from app.errors import ConflictError
from app.service import CoreService
import test_completion_persistence_r44 as completion_cases


class CheckpointFenceR94Tests(unittest.TestCase):
    setUp = completion_cases.CompletionPersistenceR44Tests.setUp
    tearDown = completion_cases.CompletionPersistenceR44Tests.tearDown

    def save(self, marker):
        return self.service.upsert_checkpoint(*self.args[:3], mode='followers',
            stage='collecting_list', cursor={'resume_tail': [marker], 'progress_epoch': 4},
            counters={'discovered': 1, 'processed': 0})

    def test_late_checkpoint_cannot_restore_deleted_target_or_admit_new_candidates(self):
        self.service.append_task_mode_candidates(*self.args, ['saved_before_delete'])
        before = self.save('saved_before_delete')
        self.service.delete_task_target(*self.args[:3], dismiss=True)
        with self.assertRaises(ConflictError):
            self.save('late_after_delete')
        current = self.service.get_task(self.owner, self.task['id'])['targets'][0]
        self.assertEqual('deleted_archived', current['current_stage'])
        self.assertEqual(before, self.service.get_checkpoint(*self.args))
        with self.assertRaises(ConflictError):
            self.service.append_task_mode_candidates(*self.args, ['unwanted_new_candidate'])

    def test_late_checkpoint_cannot_rewrite_completed_target_history(self):
        before = self.save('completed_source')
        self.service.set_target_runtime_status(*self.args[:3], 'completed')
        self.service.archive_completed_task_target_from_list(*self.args[:3])
        current = self.service.get_task(self.owner, self.task['id'])['targets'][0]
        with self.assertRaises(ConflictError):
            self.save('stale_source')
        self.assertEqual(current, self.service.get_task(self.owner, self.task['id'])['targets'][0])
        self.assertEqual(before, self.service.get_checkpoint(*self.args))

    def test_failed_checkpoint_transaction_preserves_previous_cursor_and_pending_spool_on_reopen(self):
        self.service.append_task_mode_candidates(*self.args, ['saved_candidate'])
        before = self.save('saved_candidate')
        write = self.database.write
        @contextmanager
        def failed_commit():
            with write() as connection:
                yield connection
                raise sqlite3.OperationalError('injected commit failure')
        with patch.object(self.database, 'write', failed_commit), self.assertRaises(sqlite3.OperationalError):
            self.save('uncommitted_cursor')
        reopened = Database(self.database.path)
        reopened.initialize()
        recovered = CoreService(reopened)
        recovered.recover_interrupted_operations()
        self.assertEqual(before, recovered.get_checkpoint(*self.args))
        self.assertEqual(1, recovered.task_mode_candidate_stats(*self.args)['pending'])

    def test_stopped_unfinished_target_keeps_last_checkpoint_for_explicit_resume(self):
        self.service.append_task_mode_candidates(*self.args, ['unfinished'])
        self.service.set_target_runtime_status(*self.args[:3], 'stopped')
        expected = self.save('unfinished')
        self.assertEqual(expected, self.service.get_checkpoint(*self.args))
        self.assertEqual(1, self.service.task_mode_candidate_stats(*self.args)['pending'])

    def test_terminal_backfill_is_idempotent_and_preserves_archived_target(self):
        self.service.set_target_runtime_status(*self.args[:3], 'completed', window_id='finished-window')
        self.service.archive_completed_task_target_from_list(*self.args[:3])
        with self.database.read() as connection:
            before = dict(connection.execute('SELECT * FROM task_targets WHERE id=?', (self.target,)).fetchone())
        first = self.service.upsert_checkpoint(*self.args[:3], mode='followers', stage='mode_completed',
            cursor={'source_total': 1}, counters={'saved': 1}, recoverable=False)
        again = self.service.upsert_checkpoint(*self.args[:3], mode='followers', stage='mode_completed',
            cursor={}, counters={'saved': 0}, recoverable=False)
        self.assertEqual(first, again)
        with self.database.read() as connection:
            after = dict(connection.execute('SELECT * FROM task_targets WHERE id=?', (self.target,)).fetchone())
        self.assertEqual(before, after)
