"""Auto-close guards use durable task ownership without claiming new sources."""
import json
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.service import CoreService


class UnfinishedWindowTargetsR56Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'guard.db')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('guard-owner', 'long enough password')['id']
        self.task = self.service.create_task(self.owner, name='guard', modes=['followers'],
            targets=['source.one'], settings={}, window_ids=['w1', 'w2'])
        self.target = self.task['targets'][0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def protected(self, window='w1', owner=None):
        return self.service.has_unfinished_window_targets(owner or self.owner, self.task['id'], window)

    def test_unassigned_pending_protects_eligible_windows_and_uses_hard_affinity(self):
        self.assertTrue(self.protected('w1'))
        self.assertTrue(self.protected('w2'))
        with self.db.write() as c:
            c.execute('UPDATE task_targets SET allowed_window_ids_json=? WHERE id=?',
                      (json.dumps(['w2']), self.target))
        self.assertFalse(self.protected('w1'))
        self.assertTrue(self.protected('w2'))

    def test_current_owner_and_failure_keep_actual_window_not_stale_preference(self):
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET preferred_window_id='w2' WHERE id=?", (self.target,))
        self.service.set_target_runtime_status(self.owner, self.task['id'], self.target, 'running', window_id='w1')
        self.assertTrue(self.protected('w1'))
        self.assertFalse(self.protected('w2'))
        self.service.set_target_runtime_status(self.owner, self.task['id'], self.target, 'failed', window_id='w1')
        # A failed lease remains on its real execution window. A stale preferred
        # window cannot claim it or prevent the real window from being repaired.
        self.assertTrue(self.protected('w1'))
        self.assertFalse(self.protected('w2'))
        with self.db.write() as c:
            c.execute('UPDATE task_targets SET preferred_window_id=NULL WHERE id=?', (self.target,))
        self.assertTrue(self.protected('w1'))
        self.assertFalse(self.protected('w2'))

    def test_completed_archived_and_waiting_pool_sources_do_not_hold_window(self):
        self.service.set_target_runtime_status(self.owner, self.task['id'], self.target, 'completed', window_id='w1')
        self.service.upsert_manual_split_candidates(self.owner, [{'username': 'waiting.source', 'queued': True}])
        self.assertFalse(self.protected())
        with self.db.write() as c:
            c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                "preferred_window_id,current_stage,created_at,updated_at) VALUES('removed',?,'removed.source',"
                "'removed.source',2,'pending','w1','deleted_archived','2026-09-24','2026-09-24')", (self.task['id'],))
        self.assertFalse(self.protected())

    def test_guard_is_owner_scoped_and_does_not_consume_pending_candidates_or_assignments(self):
        other = self.service.register_user('guard-other', 'long enough password')['id']
        self.service.append_task_mode_candidates(self.owner, self.task['id'], self.target, 'followers', ['pending.person'])
        with self.db.read() as c:
            before = tuple(c.iterdump())
        self.assertTrue(self.protected())
        self.assertFalse(self.protected(owner=other))
        with self.db.read() as c:
            self.assertEqual(before, tuple(c.iterdump()))
            self.assertEqual('pending', c.execute('SELECT state FROM task_mode_candidates').fetchone()[0])
            self.assertEqual(2, c.execute('SELECT COUNT(*) FROM task_windows').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
