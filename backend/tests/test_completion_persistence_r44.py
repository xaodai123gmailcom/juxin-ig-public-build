"""Durable pending work always wins over stale completion/progress markers."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database, _prune_terminal_candidate_spool
from app.cloud_workspace import export_workspace, import_workspace
from app.errors import ConflictError
from app.service import CoreService


class CompletionPersistenceR44Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'pending.sqlite3')
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user('completion-r44', 'completion test password')['id']
        self.task = self.service.create_task(self.owner, name='Durable completion',
            modes=['followers', 'following'], targets=['source'], settings={})
        self.target = self.task['targets'][0]['id']
        self.args = (self.owner, self.task['id'], self.target, 'followers')

    def tearDown(self):
        self.temp.cleanup()

    def append(self, names=('screen_later',)):
        return self.service.append_task_mode_candidates(*self.args, names)

    def complete(self):
        return self.service.set_target_runtime_status(*self.args[:3], 'completed')

    def stats(self):
        return self.service.task_mode_candidate_stats(*self.args)

    def checkpoint(self, mode='followers'):
        self.service.upsert_checkpoint(*self.args[:3], mode=mode, stage='mode_completed',
            cursor={'candidate_spool_complete': True, 'candidate_spool_natural_end': True},
            counters={'source_total': 167, 'discovered': 100, 'processed': 100, 'saved': 100,
                      'previous_counters': {'discovered': 200, 'processed': 200, 'saved': 200}})

    def test_target_completion_rejects_pending_despite_stale_completed_checkpoint(self):
        self.append()
        self.checkpoint()
        with self.assertRaises(ConflictError):
            self.complete()
        self.assertEqual(1, self.stats()['pending'])
        with self.database.read() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM split_candidate_history').fetchone()[0])
        self.assertNotEqual('completed', self.service.get_task(self.owner, self.task['id'])['targets'][0]['status'])

    def test_target_completion_checks_all_modes(self):
        self.service.append_task_mode_candidates(*self.args[:3], 'following', ['another_pending'])
        with self.assertRaises(ConflictError):
            self.complete()

    def test_task_completion_rejects_pending_before_publishing(self):
        self.append()
        with self.assertRaisesRegex(ConflictError, 'candidates are pending'):
            self.service.finalize_task_runtime_status(self.owner, self.task['id'], 'completed')
        self.assertNotEqual('completed', self.service.get_task(self.owner, self.task['id'])['status'])

    def test_database_rejects_direct_target_and_task_completion(self):
        self.append()
        for table, key in [('task_targets', self.target), ('tasks', self.task['id'])]:
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                with self.database.write() as c:
                    c.execute(f"UPDATE {table} SET status='completed' WHERE id=?", (key,))
        self.assertEqual(1, self.stats()['pending'])

    def test_empty_source_can_complete_without_percentage_quota(self):
        self.checkpoint()
        self.complete()
        done = self.service.finalize_task_runtime_status(self.owner, self.task['id'], 'completed')
        self.assertEqual('completed', done['status'])

    def test_late_new_candidate_rejected_but_duplicate_append_is_idempotent(self):
        self.append()
        self.service.finish_task_mode_candidate(*self.args, 'screen_later', state='deduped')
        self.complete()
        self.service.finalize_task_runtime_status(self.owner, self.task['id'], 'completed')
        self.assertEqual(0, self.append()['added'])
        with self.assertRaises(ConflictError):
            self.append(['too_late'])
        self.assertEqual({'total': 1, 'pending': 0, 'recorded': 0, 'deduped': 1}, self.stats())

    def test_database_rejects_late_insert_and_reopen(self):
        self.append()
        self.service.finish_task_mode_candidate(*self.args, 'screen_later', state='deduped')
        self.complete()
        statements = [
            ("INSERT INTO task_mode_candidates(target_id,mode,username_norm,username_display,discovery_order,discovered_at,updated_at) VALUES(?,'followers','late','late',2,'now','now')", (self.target,)),
            ("INSERT OR REPLACE INTO task_mode_candidates(target_id,mode,username_norm,username_display,discovery_order,discovered_at,updated_at) VALUES(?,'followers','screen_later','screen_later',1,'now','now')", (self.target,)),
            ("UPDATE task_mode_candidates SET state='pending' WHERE target_id=?", (self.target,)),
        ]
        for sql, params in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.database.write() as c:
                    c.execute(sql, params)
        self.assertEqual({'total': 1, 'pending': 0, 'recorded': 0, 'deduped': 1}, self.stats())

    def test_deleted_target_rejects_late_producer_batch(self):
        with self.database.write() as c:
            c.execute("UPDATE task_targets SET current_stage='deleted_archived' WHERE id=?", (self.target,))
        with self.assertRaises(ConflictError):
            self.append()
        self.assertEqual(0, self.stats()['total'])

    def test_stopped_target_can_keep_durable_work_for_resume(self):
        self.service.set_target_runtime_status(*self.args[:3], 'stopped')
        self.service.finalize_task_runtime_status(self.owner, self.task['id'], 'stopped')
        self.assertEqual(1, self.append()['pending'])

    def test_deleted_history_backup_restores_pending_without_requeue(self):
        self.append()
        self.service.delete_task_target(*self.args[:3], dismiss=True)
        payload, _ = export_workspace(self.database, self.owner, self.temp.name)
        restored = Database(Path(self.temp.name) / 'restored.sqlite3')
        restored.initialize()
        service = CoreService(restored)
        owner = service.register_user('restored-user', 'restored test password')['id']
        import_workspace(restored, owner, self.temp.name, payload)
        task = service.get_task(owner, self.task['id'])
        self.assertEqual('deleted_archived', task['targets'][0]['current_stage'])
        self.assertEqual(1, service.task_mode_candidate_stats(
            owner, self.task['id'], self.target, 'followers')['pending'])
        self.assertEqual([], service.list_split_candidates(owner))
        with self.assertRaises(ConflictError):
            service.append_task_mode_candidates(
                owner, self.task['id'], self.target, 'followers', ['late_after_restore'])

    def test_stale_numeric_checkpoint_cannot_inflate_live_progress(self):
        self.append(['finished', 'unfinished'])
        self.service.finish_task_mode_candidate(*self.args, 'finished', state='recorded')
        self.checkpoint()
        task = self.service.get_task(self.owner, self.task['id'])
        self.assertEqual({'source_total': 167, 'discovered': 2, 'processed': 1, 'saved': 1,
                          'skipped_global_duplicates': 0, 'qualified_for_review': 0, 'discarded': 0, 'hover_discarded': 0},
                         task['targets'][0]['mode_progress']['followers'])

    def test_retention_preserves_entire_mode_if_any_candidate_is_pending(self):
        self.append(['finished', 'unfinished'])
        self.service.finish_task_mode_candidate(*self.args, 'finished', state='recorded')
        self.checkpoint()
        self.service.finalize_task_runtime_status(self.owner, self.task['id'], 'stopped')
        with self.database.write() as c:
            c.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00Z' WHERE id=?", (self.task['id'],))
            _prune_terminal_candidate_spool(c)
        self.database.initialize()
        self.assertEqual({'total': 2, 'pending': 1, 'recorded': 1, 'deduped': 0}, self.stats())
        self.service.finish_task_mode_candidate(*self.args, 'unfinished', state='deduped')
        self.database.initialize()
        self.assertEqual(0, self.stats()['total'])
        # Once the technical spool is pruned, historical checkpoint display still works.
        self.assertEqual(167, self.service.get_task(self.owner, self.task['id'])['targets'][0]['mode_progress']['followers']['source_total'])

    def test_append_racing_completion_never_produces_completed_with_pending(self):
        gate = threading.Barrier(2)
        def run(call):
            gate.wait(timeout=5)
            try:
                call()
                return True
            except ConflictError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, self.append)
            b = pool.submit(run, self.complete)
            self.assertEqual(1, int(a.result(timeout=10)) + int(b.result(timeout=10)))
        status = self.service.get_task(self.owner, self.task['id'])['targets'][0]['status']
        self.assertFalse(status == 'completed' and self.stats()['pending'] > 0)

if __name__ == '__main__':
    unittest.main()
