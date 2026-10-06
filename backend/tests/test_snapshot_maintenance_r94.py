"""A WAL snapshot must not queue behind optional maintenance's writer lock."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.service import CoreService
from support.concurrency_probe import ThreadGroup


class SnapshotMaintenanceR94Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'snapshot.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('snapshot_writer', 'snapshot-writer-password')['id']
        with self.db.write() as connection:
            connection.execute("UPDATE workbench_state_revision SET last_cleanup_at='2000-01-01 00:00:00'")
        self.threads = ThreadGroup(self)

    def snapshot_while_writer_is_held(self):
        done = threading.Event()
        def snapshot():
            try:
                return self.service.get_workbench_snapshot(self.owner, limit=10, history_limit=10)
            finally:
                done.set()
        thread, values, errors = self.threads.start(snapshot)
        self.assertTrue(done.wait(3), 'optional cleanup blocked a read-only snapshot behind an active writer')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(0, values[0]['counts']['total_collected'])
        with self.db.read() as connection:
            self.assertEqual('2000-01-01 00:00:00', connection.execute(
                'SELECT last_cleanup_at FROM workbench_state_revision').fetchone()[0])

    def test_active_collection_writer_does_not_block_snapshot_for_cleanup(self):
        with self.db.write():
            self.snapshot_while_writer_is_held()
        self.assertTrue(self.db.maintain_transient_data())
        self.assertFalse(self.db.maintain_transient_data())

    def test_external_sqlite_writer_does_not_consume_snapshot_timeout(self):
        connection = sqlite3.connect(self.db.path, timeout=0, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            self.snapshot_while_writer_is_held()
        finally:
            connection.rollback(); connection.close()
        self.assertTrue(self.db.maintain_transient_data())

    def test_actual_cleanup_error_is_not_silenced_or_marked_complete(self):
        with patch('app.database._prune_diagnostic_event_log', side_effect=sqlite3.OperationalError('invalid table')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'invalid table'):
                self.db.maintain_transient_data()
        self.assertTrue(self.db.maintain_transient_data())


if __name__ == '__main__':
    unittest.main()
