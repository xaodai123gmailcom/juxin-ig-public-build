"""A confirmed manual grant permits one stopped task page, never another lease."""
import unittest

import test_account_surface as fixtures
from app.errors import ConflictError


class ManualSurfaceR51Tests(unittest.TestCase):
    setUp = fixtures.AccountSurfaceTests.setUp
    tearDown = fixtures.AccountSurfaceTests.tearDown

    def prepare(self):
        token = self.service.acquire_browser_lease(
            self.owner, self.profile, operation_type='collection', entity_id='manual-task')
        key = self.surface.task_key({'lease_token': token})
        self.stopped = False
        self.surface.manual_control_active = lambda owner, profile, lease: (
            self.stopped and (owner, profile, lease) == (self.owner, self.profile, token))
        return token, key

    def permit(self, key):
        return self.surface.permit_interference(self.owner, self.row, key, 'source-page')

    def interactive(self, permit, **overrides):
        body = {**self.body, 'view_target': 'source-page', 'interference_grant': permit['grant']}
        return self.surface.update(self.owner, self.row, {**body, **overrides})

    def test_confirmation_without_actual_quiescence_cannot_grant_input(self):
        _, key = self.prepare()
        with self.assertRaises(ConflictError):
            self.permit(key)
        self.assertEqual({}, self.surface.interference)
        self.assertFalse(self.adapter.shows)

    def test_stopped_page_accepts_input_and_keeps_original_task_lease(self):
        token, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        self.assertTrue(self.interactive(permit)['attached'])
        self.assertEqual('source-page', permit['target'])
        self.assertEqual(key, permit['task_key'])
        self.assertEqual(permit['grant'], self.surface.manual_info(self.owner, self.row)['grant'])
        with self.db.read() as c:
            lease = c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?', (self.profile,)).fetchone()
        self.assertEqual((token, 'manual-task'), (lease['lease_token'], lease['entity_id']))
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(self.owner, self.profile, operation_type='action', entity_id='steal')
        with self.assertRaises(ConflictError):
            self.manager.command(self.owner, {'id': self.ident, 'action': 'close'})

    def test_grant_cannot_target_another_tab_or_use_invalid_token(self):
        _, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        for body in ({'view_target': 'screen-2'}, {'interference_grant': 'made-up'},
                     {'interference_grant': None}, {'view_target': None}):
            with self.subTest(body=body), self.assertRaises(ConflictError):
                self.interactive(permit, **body)
        self.assertFalse(self.adapter.shows)

    def test_replaced_lease_and_stale_plan_cannot_reuse_grant(self):
        token, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        self.service.release_browser_lease(self.profile, token)
        replacement = self.service.acquire_browser_lease(
            self.owner, self.profile, operation_type='collection', entity_id='new-task')
        with self.assertRaises(ConflictError):
            self.interactive(permit)
        self.assertEqual({'active': False}, self.surface.manual_info(self.owner, self.row))
        self.assertNotEqual(token, replacement)

    def test_wrong_owner_cannot_use_or_revoke_grant(self):
        _, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        with self.assertRaises(ConflictError):
            self.surface.permit_interference(self.other, self.row, key, 'source-page')
        with self.assertRaises(ConflictError):
            self.surface.revoke_manual(self.other, self.profile)
        self.assertEqual({'active': False}, self.surface.manual_info(self.other, self.row))
        self.assertTrue(self.interactive(permit)['attached'])

    def test_manager_resume_invalidates_input_even_before_old_renderer_refresh(self):
        _, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        self.stopped = False
        with self.assertRaises(ConflictError):
            self.interactive(permit)
        self.assertEqual({'active': False}, self.surface.manual_info(self.owner, self.row))

    def test_revocation_hides_native_surface_and_blocks_late_heartbeat(self):
        _, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        self.interactive(permit)
        self.surface.revoke_manual(self.owner, self.profile)
        self.assertFalse(self.adapter.visible)
        self.assertEqual({}, self.surface.interference)
        with self.assertRaises(ConflictError):
            self.interactive(permit)

    def test_failed_native_hide_does_not_report_revocation_success(self):
        _, key = self.prepare()
        self.stopped = True
        permit = self.permit(key)
        def fail(_profile):
            raise RuntimeError('native hide failed')
        self.adapter.surface_hide = fail
        with self.assertRaisesRegex(RuntimeError, 'native hide failed'):
            self.surface.revoke_manual(self.owner, self.profile)
        self.assertEqual(permit['grant'], self.surface.manual_info(self.owner, self.row)['grant'])

    def test_old_confirmation_token_cannot_end_new_manual_session(self):
        _, key = self.prepare()
        self.stopped = True
        old = self.permit(key)
        current = self.permit(key)
        self.assertNotEqual(old['grant'], current['grant'])
        with self.assertRaises(ConflictError):
            self.surface.validate_manual_grant(self.owner, self.row, key, old['grant'])
        self.assertTrue(self.surface.validate_manual_grant(self.owner, self.row, key, current['grant']))

