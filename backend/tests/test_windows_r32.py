"""Window RPC concurrency, cancellation and persisted manual intent regressions."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_workspace import AccountWorkspace
from app.database import Database
from app.embedded_browser import EmbeddedBrowser
from app.errors import ConflictError
from app.service import CoreService


class WindowBridge:
    def __init__(self):
        self.live = {}
        self.calls = []
        self.generation = 0
        self.hook = None

    def call(self, method, **body):
        self.calls.append((method, body))
        if self.hook:
            self.hook(method, body)
        profile = body.get('profile')
        if method == 'hide':
            return {'hidden': True}
        if method == 'inventory':
            return {'profiles': [{'id': key, **value} for key, value in self.live.items()]}
        if method in {'ensure', 'open-whatsapp'}:
            if profile not in self.live:
                if method == 'ensure' and not body['open']:
                    raise ConflictError('closed')
                self.generation += 1
                self.live[profile] = {'generation': self.generation, 'ws': f'ws://127.0.0.1:1234/{self.generation}'}
            return {**self.live[profile], **({'page_loaded': True} if method == 'open-whatsapp' else {})}
        if method == 'verify':
            if self.live.get(profile) != {'generation': body['generation'], 'ws': body['ws']}:
                raise ConflictError('stale')
            return {'verified': True}
        if method == 'close':
            if self.live[profile]['generation'] != body['generation']:
                raise ConflictError('stale')
            self.live.pop(profile)
            return {'closed': True}
        raise AssertionError(method)


class WindowsR32Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'windows.sqlite')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('windows-r32', 'correct horse battery staple')['id']
        self.bridge = WindowBridge()
        self.native = EmbeddedBrowser(self.db, self.tmp.name, bridge=self.bridge)
        self.accounts = AccountWorkspace(self.service, SimpleNamespace(native=self.native, close_profile=self.native.close_profile))
        self.plan = self.accounts.save(self.owner, {'name': 'one', 'native': True})['id']
        self.profile = self.accounts.get(self.owner, self.plan)['profile_id']
        other = self.accounts.save(self.owner, {'name': 'two', 'native': True})['id']
        self.other_profile = self.accounts.get(self.owner, other)['profile_id']

    def tearDown(self):
        self.tmp.cleanup()

    def _thread(self, operation, results, errors):
        def run():
            try:
                results.append(operation())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def test_slow_verify_does_not_block_other_window_or_ticket_cancellation(self):
        for operation in ('verify', 'commit', 'action'):
            with self.subTest(operation=operation):
                token = self.native.begin_connection_attempt(self.profile)
                state = self.native.connection_endpoint(self.profile, open_if_needed=True, attempt_id=token)
                self.native.verify_connection_endpoint(self.profile, state['ws'], state['generation'], token)
                action = self.native.begin_action_attempt(self.profile, state['ws'], state['generation'])
                entered, release, unrelated = threading.Event(), threading.Event(), threading.Event()
                results, errors = [], []
                def hook(method, body):
                    if method == 'verify':
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('fixture verification blocked')
                self.bridge.hook = hook
                invoke = {
                    'verify': lambda: self.native.verify_connection_endpoint(self.profile, state['ws'], state['generation'], token),
                    'commit': lambda: self.native.commit_connection_attempt(token),
                    'action': lambda: self.native.resolve_action_attempt(action),
                }[operation]
                verifier = self._thread(invoke, results, errors)
                self.assertTrue(entered.wait(2))
                def other_window():
                    ticket = self.native.begin_connection_attempt(self.other_profile)
                    self.native.cancel_connection_attempt(ticket)
                    (self.native.cancel_action_attempt(action) if operation == 'action' else self.native.cancel_connection_attempt(token))
                    unrelated.set()
                other_errors = []
                other = self._thread(other_window, [], other_errors)
                try:
                    self.assertTrue(unrelated.wait(.5), 'one slow bridge RPC blocked all windows and cancellation')
                finally:
                    release.set()
                    verifier.join(3)
                    other.join(3)
                    self.bridge.hook = None
                self.assertFalse(verifier.is_alive())
                self.assertFalse(other.is_alive())
                self.assertEqual([], other_errors)
                self.assertEqual([], results, 'cancelled RPC must not commit a connection or action')
                self.assertEqual(1, len(errors))
                self.assertIsInstance(errors[0], ConflictError)
                self.assertEqual({}, self.native.action_leases)
                self.native.cancel_connection_attempt(token)
                self.native.cancel_action_attempt(action)

    def test_whatsapp_open_rejects_shutdown_before_rpc(self):
        self.native.shutdown()
        with self.assertRaises(ConflictError):
            self.native.open_whatsapp(self.profile, 'https://web.whatsapp.com/')
        self.assertFalse(any(method == 'open-whatsapp' for method, _ in self.bridge.calls))
        self.assertEqual({}, self.native.processes)

    def test_slow_verification_rejects_close_published_during_rpc(self):
        token = self.native.begin_connection_attempt(self.profile)
        state = self.native.connection_endpoint(self.profile, open_if_needed=True, attempt_id=token)
        entered, release = threading.Event(), threading.Event()
        results, errors, closed, close_errors = [], [], [], []
        def hook(method, body):
            if method == 'verify':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture verification blocked')
        self.bridge.hook = hook
        verifier = self._thread(lambda: self.native.verify_connection_endpoint(self.profile, state['ws'], state['generation'], token), results, errors)
        self.assertTrue(entered.wait(2))
        closer = self._thread(lambda: self.native.close_profile(self.profile), closed, close_errors)
        try:
            deadline = time.monotonic() + 2
            while self.profile not in self.native.closing and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertIn(self.profile, self.native.closing)
            self.assertNotIn(token, self.native.connections)
        finally:
            release.set()
            verifier.join(3)
            closer.join(3)
            self.bridge.hook = None
        self.assertEqual([], results)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual([], close_errors)
        self.assertTrue(closed[0]['closed'])
        self.assertNotIn(self.profile, self.bridge.live)

    def test_delayed_whatsapp_open_cannot_republish_state_after_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def hook(method, body):
            if method == 'open-whatsapp':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture opening blocked')
        self.bridge.hook = hook
        opener = self._thread(lambda: self.native.open_whatsapp(self.profile, 'https://web.whatsapp.com/'), results, errors)
        self.assertTrue(entered.wait(2))
        try:
            self.native.shutdown()
        finally:
            release.set()
            opener.join(3)
            self.bridge.hook = None
        self.assertFalse(opener.is_alive())
        self.assertEqual([], results)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual({}, self.native.processes)
        self.assertNotIn('close', [method for method, _ in self.bridge.calls], 'Core restart must retain desktop login sessions')

    def test_pending_whatsapp_open_observes_close_fence_and_other_windows_continue(self):
        entered, release = threading.Event(), threading.Event()
        results, errors, closed, close_errors = [], [], [], []
        def hook(method, body):
            if method == 'open-whatsapp':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture opening blocked')
        self.bridge.hook = hook
        opener = self._thread(lambda: self.native.open_whatsapp(self.profile, 'https://web.whatsapp.com/'), results, errors)
        self.assertTrue(entered.wait(2))
        closer = self._thread(lambda: self.native.close_profile(self.profile), closed, close_errors)
        try:
            deadline = time.monotonic() + 2
            while self.profile not in self.native.closing and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertIn(self.profile, self.native.closing)
            with self.assertRaises(ConflictError):
                self.native.begin_connection_attempt(self.profile)
            self.native.begin_connection_attempt(self.other_profile)
        finally:
            release.set()
            opener.join(3)
            closer.join(3)
            self.bridge.hook = None
        self.assertEqual([], results)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual([], close_errors)
        self.assertTrue(closed[0]['closed'])
        self.assertNotIn(self.profile, self.native.processes)
        self.assertNotIn(self.profile, self.bridge.live)

    def _remembered(self, plan=None):
        with self.db.read() as connection:
            row = connection.execute('SELECT opened FROM account_window_open_state WHERE plan_id=?', (plan or self.plan,)).fetchone()
            return None if row is None else row[0]

    def _remember(self, opened):
        with self.db.write() as connection:
            self.accounts.restore.remember(connection, self.owner, self.plan, opened)
        self.accounts.restore.started.add(self.owner)

    def test_shutdown_capture_never_promotes_task_pages_to_manual_open_intent(self):
        for prior in (None, False, True):
            with self.subTest(prior=prior):
                with self.db.write() as connection:
                    connection.execute('DELETE FROM account_window_open_state WHERE plan_id=?', (self.plan,))
                self.accounts.restore.started.add(self.owner)
                if prior is not None:
                    self._remember(prior)
                token = self.service.acquire_browser_lease(self.owner, self.profile, operation_type='studio', entity_id='task')
                try:
                    if prior is True:
                        # Task cleanup can close a manually opened page before
                        # its lease is released. Manual intent must survive.
                        self.bridge.live.pop(self.profile, None)
                    else:
                        self.native.connection_endpoint(self.profile, open_if_needed=True)
                    self.accounts.restore.capture_before_shutdown()
                    self.assertEqual(None if prior is None else int(prior), self._remembered())
                finally:
                    self.service.release_browser_lease(self.profile, token)

    def test_capture_protects_lease_released_while_inventory_is_pending(self):
        self._remember(True)
        token = self.service.acquire_browser_lease(self.owner, self.profile, operation_type='studio', entity_id='cleanup')
        def hook(method, body):
            if method == 'inventory':
                self.service.release_browser_lease(self.profile, token)
        self.bridge.hook = hook
        try:
            self.accounts.restore.capture_before_shutdown()
        finally:
            self.bridge.hook = None
            self.service.release_browser_lease(self.profile, token)
        self.assertEqual(1, self._remembered())

    def test_capture_cannot_overwrite_newer_manual_open_when_inventory_is_stale(self):
        # Freeze timestamp to the same millisecond: ordering relies on the
        # acquisition fence, not on two writes getting distinct timestamps.
        with patch('app.account_restore.isoformat', return_value='2026-09-16T12:00:00.000Z'):
            self._remember(True)
            self._assert_capture_serializes_new_operation(manual=True)
        self.assertEqual(1, self._remembered())

    def test_capture_serializes_new_task_until_observation_commits(self):
        self._remember(True)
        self._assert_capture_serializes_new_operation(manual=False)

    def _assert_capture_serializes_new_operation(self, *, manual):
        entered, release, operation_entered, requested = (threading.Event() for _ in range(4))
        capture_errors, operation_errors, results = [], [], []
        def hook(method, body):
            if method == 'inventory':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture inventory blocked')
        def operation():
            requested.set()
            if manual:
                def opener(provider, profile, platform, cookies):
                    operation_entered.set()
                    return self.native.open_whatsapp(profile, 'https://web.whatsapp.com/')
                self.accounts.opener = opener
                return self.accounts.control_profile(self.owner, self.profile, 'open')
            token = self.service.acquire_browser_lease(self.owner, self.profile, operation_type='studio', entity_id='new-task')
            try:
                operation_entered.set()
                # Even a short task must not slip entirely through inventory.
                self.bridge.live.pop(self.profile, None)
            finally:
                self.service.release_browser_lease(self.profile, token)
        self.bridge.hook = hook
        capture = self._thread(self.accounts.restore.capture_before_shutdown, [], capture_errors)
        self.assertTrue(entered.wait(2))
        actor = self._thread(operation, results, operation_errors)
        try:
            self.assertTrue(requested.wait(2))
            self.assertFalse(operation_entered.wait(.15), 'new operation acquired inside the shutdown observation')
        finally:
            release.set()
            capture.join(3)
            actor.join(3)
            self.bridge.hook = None
        self.assertFalse(capture.is_alive())
        self.assertFalse(actor.is_alive())
        self.assertEqual([], capture_errors)
        self.assertEqual([], operation_errors)
        self.assertTrue(operation_entered.is_set())

    def test_legacy_manual_controls_persist_intent_and_failed_close_keeps_it(self):
        self.accounts.opener = lambda provider, profile, platform, cookies: self.native.open_whatsapp(profile, 'https://web.whatsapp.com/')
        self.accounts.control_profile(self.owner, self.profile, 'open')
        self.assertEqual(1, self._remembered())
        token = self.service.acquire_browser_lease(self.owner, self.profile, operation_type='studio', entity_id='active')
        try:
            with self.assertRaises(ConflictError):
                self.accounts.control_profile(self.owner, self.profile, 'close')
            self.assertEqual(1, self._remembered())
        finally:
            self.service.release_browser_lease(self.profile, token)
        self.accounts.control_profile(self.owner, self.profile, 'close')
        self.assertEqual(0, self._remembered())
        self.db.initialize()
        self.accounts.restore.run(self.owner)
        self.assertNotIn(self.profile, self.bridge.live)


if __name__ == '__main__':
    unittest.main()
