"""Initialization owns its SQLite connection, even when PRAGMA or close fails."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database


class ConnectionInitializationTests(unittest.TestCase):
    def test_each_initialization_failure_closes_once_and_preserves_primary(self):
        for failure_at in range(5):
            for error_type in (sqlite3.OperationalError, KeyboardInterrupt):
                with self.subTest(failure_at=failure_at, error_type=error_type):
                    primary = error_type('injected initialization failure')
                    class Connection:
                        closed = 0
                        step = 0
                        def __setattr__(self, name, value):
                            if name == 'row_factory':
                                self.operation()
                            object.__setattr__(self, name, value)
                        def operation(self):
                            current = self.step
                            self.step += 1
                            if current == failure_at:
                                raise primary
                        def execute(self, sql):
                            self.operation()
                        def close(self):
                            self.closed += 1
                    connection = Connection()
                    with patch('app.database.sqlite3.connect', return_value=connection):
                        with self.assertRaises(error_type) as caught:
                            Database(':memory:')._connect()
                    self.assertIs(caught.exception, primary)
                    self.assertEqual(connection.closed, 1)

    def test_close_failure_keeps_initialization_error_and_reports_cleanup(self):
        primary = sqlite3.OperationalError('primary pragma failure')
        class Connection:
            def execute(self, sql):
                raise primary
            def close(self):
                raise OSError('injected close failure')
        with patch('app.database.sqlite3.connect', return_value=Connection()):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                Database(':memory:')._connect()
        self.assertIs(caught.exception, primary)
        self.assertIn('injected close failure', '\n'.join(primary.__notes__))

    def test_normal_settings_commit_rollback_and_close_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / 'database.sqlite3')
            connection = database._connect()
            try:
                self.assertIs(connection.row_factory, sqlite3.Row)
                self.assertIsNone(connection.isolation_level)
                for pragma, expected in (('foreign_keys', 1), ('busy_timeout', 30000),
                                         ('synchronous', 2), ('wal_autocheckpoint', 1000)):
                    self.assertEqual(connection.execute('PRAGMA ' + pragma).fetchone()[0], expected)
                connection.execute('CREATE TABLE sample(value INTEGER)')
            finally:
                connection.close()
            with database.write() as writer:
                writer.execute('INSERT INTO sample VALUES (1)')
            with self.assertRaises(sqlite3.ProgrammingError):
                writer.execute('SELECT 1')
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with database.write() as failed:
                    failed.execute('INSERT INTO sample VALUES (2)')
                    raise RuntimeError('rollback')
            with self.assertRaises(sqlite3.ProgrammingError):
                failed.execute('SELECT 1')
            with database.read() as reader:
                self.assertEqual([row[0] for row in reader.execute('SELECT value FROM sample')], [1])
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
