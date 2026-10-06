"""Late observation delivery cannot reopen or mutate closed source history."""
import unittest
from app.errors import ConflictError
import test_completion_persistence_r44 as persistence


class ResultFenceR94Tests(unittest.TestCase):
    setUp = persistence.CompletionPersistenceR44Tests.setUp
    tearDown = persistence.CompletionPersistenceR44Tests.tearDown

    def record(self, username='saved.person', *, mode='followers', marker='original', instagram_id=None):
        return self.service.record_result(self.owner, self.task['id'], self.target,
            username=username, instagram_user_id=instagram_id, source_mode=mode,
            visibility='private', profile={'username':username,'marker':marker},
            screening={}, qualified=None)

    def rows(self, table):
        with self.database.read() as c:
            return [dict(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]

    def close(self, deleted):
        if deleted:
            self.service.delete_task_target(self.owner, self.task['id'], self.target, dismiss=True)
        else:
            self.service.set_target_runtime_status(self.owner, self.task['id'], self.target, 'completed', window_id='finished-window')

    def test_new_late_result_cannot_add_identity_or_undo_closed_source(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                # Each case gets its own SQLite state; no earlier failed assertion
                # can make this second scenario pass by changing the first source.
                if deleted:
                    self.tearDown(); self.setUp()
                self.record()
                self.close(deleted)
                tables=('task_targets','task_results','instagram_accounts','instagram_username_aliases','global_seen','event_log')
                before={table:self.rows(table) for table in tables}
                with self.assertRaises(ConflictError):
                    self.record('too.late')
                self.assertEqual(before,{table:self.rows(table) for table in tables})
                with self.assertRaises(ConflictError):
                    self.service.append_task_mode_candidates(*self.args, ['late.candidate'])

    def test_duplicate_late_result_is_idempotent_and_keeps_saved_evidence(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                if deleted:
                    self.tearDown(); self.setUp()
                first=self.record()
                self.close(deleted)
                tables=('task_targets','task_results','instagram_accounts','instagram_username_aliases','global_seen','event_log')
                before={table:self.rows(table) for table in tables}
                response=self.record(marker='stale overwrites evidence')
                self.assertEqual(first['id'],response['id'])
                self.assertTrue(response['deduped'])
                self.assertEqual(before,{table:self.rows(table) for table in tables})
                with self.assertRaises(ConflictError):
                    self.record(mode='following')

    def test_unfinished_stopped_source_still_accepts_its_last_durable_result(self):
        self.service.set_target_runtime_status(self.owner,self.task['id'],self.target,'stopped')
        result=self.record()
        self.assertFalse(result['deduped'])
        self.assertEqual(1,len(self.rows('task_results')))

    def test_conflicting_stable_id_cannot_overwrite_another_accounts_evidence(self):
        self.record('first.person', instagram_id='111')
        self.record('second.person', instagram_id='222')
        tables=('task_results','instagram_accounts','instagram_username_aliases','global_seen','task_targets','event_log')
        before={table:self.rows(table) for table in tables}
        for conflicting_id in ('222','333'):
            with self.subTest(conflicting_id=conflicting_id), self.assertRaises(ConflictError):
                self.record('first.person', instagram_id=conflicting_id, marker='wrong account evidence')
            self.assertEqual(before,{table:self.rows(table) for table in tables})

    def test_known_identity_rename_still_deduplicates_without_new_result(self):
        first=self.record('first.person',instagram_id='111')
        second=self.record('renamed.person',instagram_id='111')
        self.assertEqual(first['account_id'],second['account_id'])
        self.assertEqual(1,len(self.rows('task_results')))
        self.assertEqual(2,len([row for row in self.rows('instagram_username_aliases') if row['account_id']==first['account_id']]))

    def test_deleted_source_ignores_late_header_snapshot(self):
        self.service.capture_target_source_profile(self.owner,self.task['id'],self.target,
            {'username':'source','followers':150,'following':40,'posts':3})
        self.close(True)
        before=self.rows('task_targets')
        self.assertFalse(self.service.capture_target_source_profile(self.owner,self.task['id'],self.target,
            {'username':'source','followers':999,'following':888,'posts':777}))
        self.assertEqual(before,self.rows('task_targets'))
