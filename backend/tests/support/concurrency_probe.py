"""Check concurrency by ownership and ordering, not disk throughput.

The generous deadline is a deadlock guard, never a product latency promise.
Controlled RPCs stay blocked until explicitly released, including on failure.
"""
from __future__ import annotations

import sqlite3
import threading


WAIT_SECONDS = 15


class ThreadGroup:
    def __init__(self, case):
        self.case = case
        self.threads = []
        self.releases = []
        case.addCleanup(self.close)

    def release_event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def start(self, operation):
        values, errors = [], []

        def run():
            try:
                values.append(operation())
            except BaseException as error:
                errors.append(error)

        # Cleanup still requires a successful join. A broken implementation
        # must fail the test without leaving the build process alive forever.
        thread = threading.Thread(target=run, name=operation.__name__, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, values, errors

    def close(self):
        for release in self.releases:
            release.set()
        for thread in self.threads:
            thread.join(WAIT_SECONDS)
        self.case.assertFalse(
            [thread.name for thread in self.threads if thread.is_alive()],
            'concurrency fixture did not stop after releasing its controlled RPCs',
        )


def assert_database_writer_available(case, database):
    """Probe both process ownership and SQLite's real RESERVED writer lock.

    Call only while the controlled desktop RPC is entered and before starting
    any other writer. A real lock regression fails immediately, independently
    of the time needed to flush a subsequent durable result to disk.
    """
    acquired = database._write_lock.acquire(blocking=False)
    case.assertTrue(acquired, 'desktop RPC still owns the process SQLite write fence')
    try:
        connection = sqlite3.connect(database.path, timeout=0, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
        except sqlite3.OperationalError as error:
            raise AssertionError(
                'desktop RPC still owns a SQLite writer transaction'
            ) from error
        finally:
            connection.close()
    finally:
        database._write_lock.release()
