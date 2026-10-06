"""Slow desktop chat RPCs cannot own SQLite's write transaction.

Real SQLite and Core lease methods; only the desktop transport is controlled.
No claim of Windows, Electron, or live-message latency measurement.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_workspace import AccountWorkspace
from app.database import Database
from app.embedded_browser import EmbeddedBrowser
from app.errors import ConflictError, NotFoundError
from app.service import CoreService
from support.concurrency_probe import WAIT_SECONDS, ThreadGroup, assert_database_writer_available


class ChatBridge:
    def __init__(self):
        self.calls = []
        self.live = {}
        self.chat_hook = None

    def call(self, method, **body):
        self.calls.append((method, body))
        profile = body.get('profile')
        if method == 'hide':
            return {'hidden': True}
        if method == 'inventory':
            return {'profiles': [{'id': key, **value} for key, value in self.live.items()]}
        if method == 'ensure':
            self.live.setdefault(profile, {'ws': 'ws://127.0.0.1:1234/fixture', 'generation': 1})
            return self.live[profile]
        if method == 'verify':
            return {'verified': True}
        if method == 'chat-translation':
            if self.chat_hook:
                self.chat_hook()
            return {'messages': []}
        raise AssertionError(method)


class ChatConcurrencyR34Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.threads = ThreadGroup(self)
        self.db = Database(Path(self.tmp.name) / 'chat.sqlite')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('chat-r34', 'correct horse battery staple')['id']
        self.bridge = ChatBridge()
        self.native = EmbeddedBrowser(self.db, self.tmp.name, bridge=self.bridge)
        self.accounts = AccountWorkspace(self.service, SimpleNamespace(native=self.native))
        self.plan = self.accounts.save(self.owner, {'name': 'chat', 'native': True})['id']
        self.profile = self.accounts.get(self.owner, self.plan)['profile_id']
        self.other_plan = self.accounts.save(self.owner, {'name': 'collecting', 'native': True})['id']
        self.other_profile = self.accounts.get(self.owner, self.other_plan)['profile_id']
        self.native.connection_endpoint(self.profile, open_if_needed=True)
        self.body = {'action': 'chat_translation', 'id': self.plan, 'step': {'kind': 'poll'}}

    def _thread(self, operation):
        return self.threads.start(operation)

    def _slow_chat(self):
        entered, release = threading.Event(), self.threads.release_event()
        def hold():
            entered.set()
            release.wait()
        self.bridge.chat_hook = hold
        thread, values, errors = self._thread(lambda: self.accounts.command(self.owner, self.body))
        self.assertTrue(entered.wait(WAIT_SECONDS), f'chat RPC did not start: {errors!r}')
        return thread, values, errors, release

    def _event_count(self):
        with self.db.read() as connection:
            return connection.execute("SELECT count(*) FROM account_window_events WHERE action='r34-result' ").fetchone()[0]

    def test_slow_chat_does_not_block_another_task_result_and_heartbeat(self):
        token = self.service.acquire_browser_lease(self.owner, self.other_profile, operation_type='collection', entity_id='other-task')
        reader, values, errors, release = self._slow_chat()
        assert_database_writer_available(self, self.db)
        persisted = threading.Event()
        def write_result():
            with self.db.write() as connection:
                self.assertEqual(2, connection.execute('PRAGMA synchronous').fetchone()[0])
                self.accounts._event(connection, self.owner, self.other_plan, 'collecting', 'r34-result')
            self.service.renew_browser_lease(self.other_profile, token)
            persisted.set()
        writer, _, write_errors = self._thread(write_result)
        try:
            self.assertTrue(persisted.wait(WAIT_SECONDS),
                            f'writer or lease heartbeat did not finish while chat stayed blocked: {write_errors!r}')
            self.assertEqual(1, self._event_count())
            self.assertTrue(reader.is_alive(), 'the desktop response must still be blocked')
        finally:
            release.set();reader.join(WAIT_SECONDS);writer.join(WAIT_SECONDS)
            self.service.release_browser_lease(self.other_profile, token)
        self.assertFalse(reader.is_alive());self.assertFalse(writer.is_alive())
        self.assertEqual([], errors);self.assertEqual([], write_errors)
        self.assertEqual([{'messages': []}], values)

    def test_desktop_rpc_has_no_open_sqlite_write_transaction(self):
        def probe():
            # Independent connection bypasses the process RLock: this detects
            # the actual SQLite RESERVED writer lock, not only Python locking.
            connection = sqlite3.connect(self.db.path, timeout=.1, isolation_level=None)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute("UPDATE account_window_events SET name=name WHERE action='r34-result'")
                connection.rollback()
            finally:
                connection.close()
        self.bridge.chat_hook = probe
        self.assertEqual({'messages': []}, self.accounts.command(self.owner, self.body))

    def test_task_acquisition_waits_then_excludes_later_translation(self):
        reader, values, errors, release = self._slow_chat()
        started, acquired = threading.Event(), threading.Event()
        def acquire():
            started.set()
            token = self.service.acquire_browser_lease(self.owner, self.profile, operation_type='collection', entity_id='after-chat')
            acquired.set()
            return token
        worker, tokens, task_errors = self._thread(acquire)
        try:
            self.assertTrue(started.wait(WAIT_SECONDS));self.assertFalse(acquired.wait(.1))
            with self.db.read() as connection:
                self.assertIsNone(connection.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?', (self.profile,)).fetchone())
        finally:
            release.set();reader.join(WAIT_SECONDS);worker.join(WAIT_SECONDS)
        self.assertEqual([], errors);self.assertEqual([], task_errors)
        self.assertEqual([{'messages': []}], values);self.assertEqual(1, len(tokens))
        try:
            self.assertEqual('hide', self.bridge.calls[-1][0])
            with self.assertRaises(ConflictError):
                self.accounts.command(self.owner, self.body)
            self.assertEqual(1, sum(method == 'chat-translation' for method, _ in self.bridge.calls))
        finally:
            if tokens:self.service.release_browser_lease(self.profile, tokens[0])

    def test_rpc_failure_releases_surface_fence_for_other_thread(self):
        def fail():
            raise RuntimeError('controlled DOM failure')
        self.bridge.chat_hook = fail
        before = sum(method == 'hide' for method, _ in self.bridge.calls)
        with self.assertRaisesRegex(RuntimeError, 'controlled DOM failure'):
            self.accounts.command(self.owner, self.body)
        self.assertEqual(before, sum(method == 'hide' for method, _ in self.bridge.calls))
        worker, tokens, errors = self._thread(lambda: self.service.acquire_browser_lease(self.owner, self.profile, operation_type='collection', entity_id='after-failure'))
        worker.join(WAIT_SECONDS)
        self.assertFalse(worker.is_alive());self.assertEqual([], errors)
        self.assertEqual(1, len(tokens));self.service.release_browser_lease(self.profile, tokens[0])
        with self.db.write() as connection:
            self.assertEqual(2, connection.execute('PRAGMA synchronous').fetchone()[0])

    def test_plan_edits_keep_the_same_surface_fence_during_chat(self):
        # save, notes and archive are the production paths that change the
        # binding/revision. All must still acquire an ordinary account lease.
        for action in ('save', 'notes', 'archive'):
            with self.subTest(action=action):
                row = self.accounts.get(self.owner, self.plan)
                reader, _, errors, release = self._slow_chat()
                started, edited = threading.Event(), threading.Event()
                def edit():
                    started.set()
                    if action == 'save':
                        result = self.accounts.save(self.owner, {'id': self.plan, 'revision': row['revision'], 'name': 'renamed', 'native': True})
                    else:
                        result = self.accounts.command(self.owner, {'action': action, 'id': self.plan, 'revision': row['revision'], 'notes': 'preserved'})
                    edited.set()
                    return result
                writer, _, edit_errors = self._thread(edit)
                try:
                    self.assertTrue(started.wait(WAIT_SECONDS));self.assertFalse(edited.wait(.1))
                    self.assertEqual(row['revision'], self.accounts.get(self.owner, self.plan)['revision'])
                finally:
                    release.set();reader.join(WAIT_SECONDS);writer.join(WAIT_SECONDS)
                self.assertEqual([], errors);self.assertEqual([], edit_errors)
                self.assertTrue(edited.is_set())

    def test_stale_revision_or_wrong_owner_never_reaches_desktop(self):
        original = self.accounts.get
        read, release = threading.Event(), self.threads.release_event()
        def get(owner, ident):
            row = original(owner, ident)
            if threading.current_thread().name == 'stale-chat':
                read.set()
                release.wait()
            return row
        failures = []
        def translate():
            try:self.accounts.command(self.owner, self.body)
            except BaseException as error:failures.append(error)
        with patch.object(self.accounts, 'get', side_effect=get):
            def stale_chat():
                threading.current_thread().name = 'stale-chat'
                translate()
            worker, _, worker_errors = self._thread(stale_chat)
            try:
                self.assertTrue(read.wait(WAIT_SECONDS))
                self.accounts.command(self.owner, {'action': 'notes', 'id': self.plan, 'revision': 1, 'notes': 'updated'})
            finally:
                release.set();worker.join(WAIT_SECONDS)
        self.assertFalse(worker.is_alive());self.assertEqual([], worker_errors)
        self.assertEqual(1, len(failures));self.assertIsInstance(failures[0], ConflictError)
        with self.assertRaises(NotFoundError):
            self.accounts.command('other-owner', self.body)
        self.assertFalse(any(method == 'chat-translation' for method, _ in self.bridge.calls))


if __name__ == '__main__':
    unittest.main()
