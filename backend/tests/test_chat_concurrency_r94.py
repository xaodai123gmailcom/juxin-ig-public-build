"""Fault-inject the build gates that previously confused slow disks with locks."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import test_chat_concurrency_r34 as chat
import test_window_performance_r33 as windows


class BuildConcurrencyR94Tests(unittest.TestCase):
    def _run_gate(self, case_type, method, configure):
        case = case_type(method)
        original_setup = case.setUp

        def setup():
            original_setup()
            configure(case)

        case.setUp = setup
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.errors, result.errors)
        self.assertEqual([], result.skipped)
        return result

    def _slow_commit_gate(self, case_type, method, writer_name, expected_commits):
        connect = sqlite3.connect
        commits = []

        class SlowCommit(sqlite3.Connection):
            def commit(connection):
                if threading.current_thread().name == writer_name:
                    # Longer than BOTH historical 0.75s and 1s build limits.
                    # Keep the real transaction and FULL durability enabled.
                    time.sleep(1.1)
                    commits.append(writer_name)
                return super().commit()

        def delayed_connect(*args, **kwargs):
            kwargs['factory'] = SlowCommit
            return connect(*args, **kwargs)

        with patch('app.database.sqlite3.connect', side_effect=delayed_connect):
            result = self._run_gate(case_type, method, lambda case: None)
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(expected_commits, len(commits))

    def test_chat_gate_accepts_slow_full_durability_result_and_heartbeat(self):
        self._slow_commit_gate(chat.ChatConcurrencyR34Tests,
            'test_slow_chat_does_not_block_another_task_result_and_heartbeat',
            'write_result', 2)

    def test_inventory_gate_accepts_slow_full_durability_writer(self):
        self._slow_commit_gate(windows.WindowPerformanceR33Tests,
            'test_slow_inventory_does_not_hold_database_write_lock', 'write', 1)

    def _blocked_gate(self, case_type, method, rpc, *, sqlite_only=False):
        cleanup = []

        def configure(case):
            original_call = case.bridge.call

            @contextmanager
            def held_lock():
                if sqlite_only:
                    connection = sqlite3.connect(case.db.path, isolation_level=None)
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        yield
                    finally:
                        connection.rollback()
                        connection.close()
                else:
                    with case.db.write():
                        yield

            def call(method, **body):
                if method == rpc:
                    with held_lock():
                        try:
                            return original_call(method, **body)
                        finally:
                            cleanup.append('RPC released')
                return original_call(method, **body)

            case.bridge.call = call

        result = self._run_gate(case_type, method, configure)
        self.assertEqual(1, len(result.failures), result.failures)
        message = 'SQLite writer transaction' if sqlite_only else 'process SQLite write fence'
        self.assertIn(message, result.failures[0][1])
        self.assertEqual(['RPC released'], cleanup, 'failure must release the RPC before deleting its database')

    def test_chat_gate_rejects_real_process_writer_lock_and_cleans_up(self):
        self._blocked_gate(chat.ChatConcurrencyR34Tests,
            'test_slow_chat_does_not_block_another_task_result_and_heartbeat', 'chat-translation')

    def test_chat_gate_rejects_independent_sqlite_transaction_and_cleans_up(self):
        self._blocked_gate(chat.ChatConcurrencyR34Tests,
            'test_slow_chat_does_not_block_another_task_result_and_heartbeat',
            'chat-translation', sqlite_only=True)

    def test_inventory_gate_rejects_real_writer_lock_and_cleans_up(self):
        self._blocked_gate(windows.WindowPerformanceR33Tests,
            'test_slow_inventory_does_not_hold_database_write_lock', 'inventory')


if __name__ == '__main__':
    unittest.main()
