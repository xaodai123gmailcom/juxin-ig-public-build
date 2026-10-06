"""Read-only, affirmative closure evidence for historical nurture recovery."""
import threading
import unittest
from io import BytesIO
from unittest.mock import patch

from app.errors import ConflictError, NotFoundError, UpstreamUnavailableError
import test_embedded_browser as fixtures


class NurtureClosedProfileGuardTests(unittest.TestCase):
    setUp = fixtures.EmbeddedBrowserTests.setUp
    tearDown = fixtures.EmbeddedBrowserTests.tearDown

    def proof(self):
        return {'closed': True, 'profile_id': self.profile, 'owner_user_id': self.owner,
                'verification': 'desktop-absence-v1'}

    def test_closed_confirmation_has_short_bounded_rpc_and_no_external_fallback(self):
        from app.embedded_browser import DesktopBridge
        bridge = DesktopBridge('http://127.0.0.1:12345', 'offline-fixture-token')
        with patch.object(bridge.opener, 'open', return_value=BytesIO(b'{}')) as call:
            bridge.call('confirm-closed', profile=self.profile, owner=self.owner)
            self.assertEqual(2, call.call_args.kwargs['timeout'])
        with self.assertRaisesRegex(UpstreamUnavailableError, '不能安全核验'):
            with self.hub.closed_profile_guard('legacy-profile', self.owner):
                self.fail('legacy provider must not manufacture closed proof')

    def test_exact_positive_desktop_ack_is_read_only_and_routed_by_hub(self):
        with patch.object(self.bridge, 'call', return_value=self.proof()) as call:
            with self.hub.closed_profile_guard(self.profile, self.owner) as proof:
                self.assertEqual(self.proof(), proof)
            call.assert_called_once_with('confirm-closed', profile=self.profile, owner=self.owner)
        self.assertEqual({}, self.native.connections)
        self.assertEqual({}, self.bridge.live)
        with self.db.read() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])

    def test_wrong_owner_is_rejected_before_desktop_access(self):
        with patch.object(self.bridge, 'call') as call:
            with self.assertRaises(NotFoundError):
                with self.native.closed_profile_guard(self.profile, 'another-owner'):
                    self.fail('must not enter')
            call.assert_not_called()

    def test_unknown_open_malformed_or_unavailable_provider_never_means_closed(self):
        cases = [None, False, {}, {'closed': True},
                 {**self.proof(), 'closed': False},
                 {**self.proof(), 'profile_id': 'other'},
                 {**self.proof(), 'owner_user_id': 'other'},
                 {**self.proof(), 'verification': 'cached-inventory'}]
        for proof in cases:
            with self.subTest(proof=proof), patch.object(self.bridge, 'call', return_value=proof):
                with self.assertRaises(UpstreamUnavailableError):
                    with self.native.closed_profile_guard(self.profile, self.owner):
                        self.fail('must not enter')
        for error in [ConflictError('still open'), UpstreamUnavailableError('offline')]:
            with patch.object(self.bridge, 'call', side_effect=error):
                with self.assertRaises(type(error)):
                    with self.native.closed_profile_guard(self.profile, self.owner):
                        self.fail('must not enter')

    def test_local_opening_action_closing_and_shutdown_are_protected(self):
        fixtures = [
            (self.native.connections, 'ticket', {'profile': self.profile}),
            (self.native.actions, 'action', {'profile': self.profile}),
            (self.native.action_leases, 'lease', self.profile),
        ]
        for collection, key, value in fixtures:
            collection[key] = value
            try:
                with patch.object(self.bridge, 'call') as call, self.assertRaises(ConflictError):
                    with self.native.closed_profile_guard(self.profile, self.owner):
                        self.fail('must not enter')
                call.assert_not_called()
            finally:
                collection.pop(key)
        self.native.closing.add(self.profile)
        with self.assertRaises(ConflictError):
            with self.native.closed_profile_guard(self.profile, self.owner):
                self.fail('must not enter')
        self.native.closing.clear()
        self.native.stopping = True
        with self.assertRaises(ConflictError):
            with self.native.closed_profile_guard(self.profile, self.owner):
                self.fail('must not enter')

    def test_ticket_created_during_verification_is_rechecked_and_preserved(self):
        ticket = []
        def verify(*args, **kwargs):
            ticket.append(self.native.begin_connection_attempt(self.profile))
            return self.proof()
        with patch.object(self.bridge, 'call', side_effect=verify), self.assertRaises(ConflictError):
            with self.native.closed_profile_guard(self.profile, self.owner):
                self.fail('must not enter')
        self.assertIn(ticket[0], self.native.connections)
        self.native.cancel_connection_attempt(ticket[0])

    def test_guard_fences_new_connection_ticket_until_database_commit_finishes(self):
        attempted, finished = threading.Event(), threading.Event()
        result = []
        def connect():
            attempted.set()
            result.append(self.native.begin_connection_attempt(self.profile))
            finished.set()
        with patch.object(self.bridge, 'call', return_value=self.proof()):
            with self.native.closed_profile_guard(self.profile, self.owner):
                thread = threading.Thread(target=connect)
                thread.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(finished.wait(.03))
            self.assertTrue(finished.wait(1))
            thread.join(1)
        self.native.cancel_connection_attempt(result[0])
