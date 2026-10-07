"""Fresh SQLite checkpoint fences without repeated connection/schema setup."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from app.errors import ConflictError, NotFoundError
from app.standalone_nurture_selftest import Fixture
from app.studio_worker import StudioBrowser


class StandaloneNurtureCheckpointTests(unittest.TestCase):
    def exercise_checkpoint(self, operation):
        """Expose the real manager callback through its normal browser boundary."""
        async def run(directory):
            fixture = Fixture(directory, 'checkpoint')
            ident = (await fixture.start(['checkpoint-window'], config={'minutes': 1}))[0]
            exercised, errors = [], []

            async def step(browser, _step, counts, _config):
                if not exercised:
                    exercised.append(True)
                    try:
                        await operation(fixture, ident, browser.checkpoint)
                    except BaseException as error:
                        # The manager normally converts failures to job state;
                        # retain assertion failures so the test cannot swallow one.
                        errors.append(error)
                return counts

            with fixture.patches(), patch.object(StudioBrowser, 'nurture_step', new=step):
                await fixture.execute(ident)
            self.assertEqual([True], exercised)
            if errors:
                raise errors[0]
            self.assertEqual('completed', fixture.manager.get(fixture.owner, ident)['status'])
            self.assertEqual(['checkpoint-window'], fixture.closed)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            asyncio.run(run(directory))

    @staticmethod
    def change(fixture, statement, parameters=()):
        with fixture.database.write() as connection:
            connection.execute(statement, parameters)

    def test_each_checkpoint_opens_one_fresh_full_durability_autocommit_connection(self):
        async def check(fixture, ident, checkpoint):
            database = fixture.database
            original = database._connect
            opened = []

            def connect():
                connection = original()
                self.assertIsNone(connection.isolation_level)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(2, connection.execute('PRAGMA synchronous').fetchone()[0])
                self.assertEqual(1, connection.execute('PRAGMA foreign_keys').fetchone()[0])
                opened.append(connection)
                return connection

            with patch.object(database, '_connect', side_effect=connect):
                await checkpoint()
                self.assertEqual(1, len(opened))
                await checkpoint()
                self.assertEqual(2, len(opened))
            self.assertIsNot(opened[0], opened[1], 'job or lease reads must not reuse cached state')
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
        self.exercise_checkpoint(check)

    def test_owner_missing_and_deleted_job_fences_remain_fail_closed(self):
        async def check(fixture, ident, checkpoint):
            with fixture.database.read() as connection:
                row = dict(connection.execute('SELECT * FROM studio_jobs WHERE id=?', (ident,)).fetchone())
            mutations = (
                ('wrong_owner', 'UPDATE studio_jobs SET owner_user_id=? WHERE id=?', (fixture.other, ident)),
                ('deleted_job', "UPDATE studio_jobs SET deleted_at='synthetic-deleted' WHERE id=?", (ident,)),
                ('missing_job', 'DELETE FROM studio_jobs WHERE id=?', (ident,)),
            )
            for name, statement, parameters in mutations:
                with self.subTest(fence=name):
                    self.change(fixture, statement, parameters)
                    try:
                        with self.assertRaises(NotFoundError):
                            await checkpoint()
                    finally:
                        self.change(fixture, 'DELETE FROM studio_jobs WHERE id=?', (ident,))
                        self.change(fixture, 'INSERT INTO studio_jobs (' + ','.join(row) + ') VALUES ('
                                    + ','.join('?' for _ in row) + ')', tuple(row.values()))
                    await checkpoint()
        self.exercise_checkpoint(check)

    def test_new_cancelled_and_needs_review_commits_are_observed(self):
        async def check(fixture, ident, checkpoint):
            for status in ('cancelled', 'needs_review'):
                with self.subTest(status=status):
                    self.change(fixture, 'UPDATE studio_jobs SET status=? WHERE id=?', (status, ident))
                    try:
                        with self.assertRaises(asyncio.CancelledError):
                            await checkpoint()
                    finally:
                        self.change(fixture, "UPDATE studio_jobs SET status='running' WHERE id=?", (ident,))
                    await checkpoint()
        self.exercise_checkpoint(check)

    def test_missing_and_replaced_lease_generations_are_observed(self):
        async def check(fixture, ident, checkpoint):
            with fixture.database.read() as connection:
                row = dict(connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',
                                              ('checkpoint-window',)).fetchone())
            mutations = (
                ('missing', 'DELETE FROM browser_operation_leases WHERE profile_id=?'),
                ('replacement', "UPDATE browser_operation_leases SET lease_token='replacement' WHERE profile_id=?"),
            )
            for name, statement in mutations:
                with self.subTest(lease=name):
                    self.change(fixture, statement, ('checkpoint-window',))
                    try:
                        with self.assertRaises(ConflictError):
                            await checkpoint()
                    finally:
                        self.change(fixture, 'DELETE FROM browser_operation_leases WHERE profile_id=?', ('checkpoint-window',))
                        self.change(fixture, 'INSERT INTO browser_operation_leases (' + ','.join(row) + ') VALUES ('
                                    + ','.join('?' for _ in row) + ')', tuple(row.values()))
                    await checkpoint()
        self.exercise_checkpoint(check)

    def test_separate_connection_lease_commit_between_reads_is_visible(self):
        async def check(fixture, ident, checkpoint):
            database = fixture.database
            original_read = database.read
            token = fixture.tokens['checkpoint-window']
            interleaved, retained_cursors = [], []
            test = self

            class Cursor:
                def __init__(self, cursor, connection):
                    self.cursor, self.connection = cursor, connection
                    retained_cursors.append(cursor)

                def fetchone(self):
                    row = self.cursor.fetchone()
                    # Keep the first cursor alive through the second SELECT.
                    # A fresh lease commit must still be visible in autocommit.
                    test.assertIsNone(self.connection.isolation_level)
                    test.assertFalse(self.connection.in_transaction)
                    test.change(fixture, "UPDATE browser_operation_leases SET lease_token='between-reads' WHERE profile_id=?",
                                ('checkpoint-window',))
                    interleaved.append(True)
                    return row

            class Connection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, statement, *parameters):
                    cursor = self.connection.execute(statement, *parameters)
                    if statement.lstrip().upper().startswith('SELECT') and 'studio_jobs' in statement and not interleaved:
                        return Cursor(cursor, self.connection)
                    return cursor

            @contextmanager
            def read():
                with original_read() as connection:
                    yield Connection(connection)

            try:
                with patch.object(database, 'read', read):
                    with self.assertRaises(ConflictError):
                        await checkpoint()
            finally:
                self.change(fixture, 'UPDATE browser_operation_leases SET lease_token=? WHERE profile_id=?',
                            (token, 'checkpoint-window'))
            self.assertEqual([True], interleaved)
            await checkpoint()
        self.exercise_checkpoint(check)

    def test_pause_waits_then_resume_reads_new_status(self):
        async def check(fixture, ident, checkpoint):
            gate = fixture.manager.gates[ident]
            gate.clear()
            waiting = asyncio.create_task(checkpoint())
            try:
                await asyncio.sleep(0)
                self.assertFalse(waiting.done())
                self.change(fixture, "UPDATE studio_jobs SET status='needs_review' WHERE id=?", (ident,))
                gate.set()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
            finally:
                gate.set()
                if not waiting.done():
                    waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                self.change(fixture, "UPDATE studio_jobs SET status='running' WHERE id=?", (ident,))
            await checkpoint()
        self.exercise_checkpoint(check)

    def test_pause_resume_rechecks_lease_generation(self):
        async def check(fixture, ident, checkpoint):
            gate = fixture.manager.gates[ident]
            token = fixture.tokens['checkpoint-window']
            gate.clear()
            waiting = asyncio.create_task(checkpoint())
            try:
                await asyncio.sleep(0)
                self.assertFalse(waiting.done())
                self.change(fixture, "UPDATE browser_operation_leases SET lease_token='while-paused' WHERE profile_id=?",
                            ('checkpoint-window',))
                gate.set()
                with self.assertRaises(ConflictError):
                    await waiting
            finally:
                gate.set()
                if not waiting.done():
                    waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                self.change(fixture, 'UPDATE browser_operation_leases SET lease_token=? WHERE profile_id=?',
                            (token, 'checkpoint-window'))
            await checkpoint()
        self.exercise_checkpoint(check)

    def test_cancellation_while_paused_and_manager_shutdown_propagate(self):
        async def check(fixture, ident, checkpoint):
            gate = fixture.manager.gates[ident]
            gate.clear()
            waiting = asyncio.create_task(checkpoint())
            try:
                await asyncio.sleep(0)
                self.assertFalse(waiting.done())
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
            finally:
                gate.set()
                await asyncio.gather(waiting, return_exceptions=True)
            fixture.manager.stopping = True
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await checkpoint()
            finally:
                fixture.manager.stopping = False
            await checkpoint()
        self.exercise_checkpoint(check)


if __name__ == '__main__':
    unittest.main()
