"""Fault-inject the window retirement build gate, including real lock failures."""
from contextlib import contextmanager
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import test_collection_drain_r56 as drain
import test_shared_retirement_r94 as retirement


class RetirementConcurrencyR94Tests(unittest.TestCase):
    def run_gate(self, configure):
        case = retirement.SharedRetirementR94Tests(
            'test_provider_close_does_not_block_unrelated_window_admission')
        setup, teardown = case.asyncSetUp, case.asyncTearDown
        remaining = []

        async def configured_setup():
            await setup()
            configure(case)

        async def checked_teardown():
            remaining.extend(case.leases())
            await teardown()

        case.asyncSetUp, case.asyncTearDown = configured_setup, checked_teardown
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.errors, result.errors)
        self.assertEqual([], result.skipped)
        self.assertEqual([], remaining, 'the gate must join close/admission before deleting its database')
        return result

    def test_gate_accepts_slow_durable_admission_while_close_stays_pending(self):
        connect = sqlite3.connect
        context = threading.local()
        commits = []

        class SlowCommit(sqlite3.Connection):
            def commit(connection):
                if getattr(context, 'admitting', False):
                    self.assertEqual(2, connection.execute('PRAGMA synchronous').fetchone()[0])
                    # Exceeds both the former .5s admission cutoff and the 3s
                    # automatic provider response. FULL durability stays enabled.
                    time.sleep(3.2)
                    commits.append('unrelated')
                return super().commit()

        def delayed_connect(*args, **kwargs):
            kwargs['factory'] = SlowCommit
            return connect(*args, **kwargs)

        def configure(case):
            acquire = case.service.acquire_browser_lease

            def delayed_acquire(owner, profile, **kwargs):
                context.admitting = profile == 'unrelated'
                try:
                    return acquire(owner, profile, **kwargs)
                finally:
                    context.admitting = False

            case.service.acquire_browser_lease = delayed_acquire

        with patch('app.database.sqlite3.connect', side_effect=delayed_connect):
            result = self.run_gate(configure)
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(['unrelated'], commits)

    def test_gate_accepts_delayed_admission_thread(self):
        admitted = []

        def configure(case):
            acquire = case.service.acquire_browser_lease

            def delayed_acquire(owner, profile, **kwargs):
                if profile == 'unrelated':
                    time.sleep(.8)
                    admitted.append(profile)
                return acquire(owner, profile, **kwargs)

            case.service.acquire_browser_lease = delayed_acquire

        result = self.run_gate(configure)
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(['unrelated'], admitted)

    def blocked_gate(self, kind, expected_message):
        closed = []

        def configure(case):
            close = drain.CloseProvider.close_profile

            @contextmanager
            def hold():
                if kind == 'surface':
                    with case.database.browser_surface_lock:
                        yield
                elif kind == 'process_writer':
                    with case.database.write():
                        yield
                else:
                    connection = sqlite3.connect(case.database.path, isolation_level=None)
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        yield
                    finally:
                        connection.rollback()
                        connection.close()

            def held_close(provider, profile):
                with hold():
                    try:
                        return close(provider, profile)
                    finally:
                        closed.append(profile)

            replacement = patch.object(drain.CloseProvider, 'close_profile', held_close)
            replacement.start()
            case.addCleanup(replacement.stop)

        result = self.run_gate(configure)
        self.assertEqual(1, len(result.failures), result.failures)
        self.assertIn(expected_message, result.failures[0][1])
        self.assertEqual(['monitor'], closed)

    def test_gate_rejects_provider_holding_global_surface_lock(self):
        self.blocked_gate('surface', 'global window admission lock')

    def test_gate_rejects_provider_holding_process_writer(self):
        self.blocked_gate('process_writer', 'process SQLite write fence')

    def test_gate_rejects_provider_holding_sqlite_transaction(self):
        self.blocked_gate('sqlite', 'SQLite writer transaction')

    def test_gate_rejects_early_release_of_closing_window(self):
        closed = []

        def configure(case):
            close = drain.CloseProvider.close_profile

            def released_close(provider, profile):
                with case.database.read() as connection:
                    token = connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone()[0]
                case.service.release_browser_lease(profile, token)
                try:
                    return close(provider, profile)
                finally:
                    closed.append(profile)

            replacement = patch.object(drain.CloseProvider, 'close_profile', released_close)
            replacement.start()
            case.addCleanup(replacement.stop)

        result = self.run_gate(configure)
        self.assertEqual(1, len(result.failures), result.failures)
        self.assertIn('closing window must retain its lease', result.failures[0][1])
        self.assertEqual(['monitor'], closed)


if __name__ == '__main__':
    unittest.main()
