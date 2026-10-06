"""Offline API regression for a queued post blocking historical nurture cleanup.

Only setup seeds SQLite. From that broken state onward every transition goes
through the authenticated application routes; SQL is used only for assertions.
The application lifespan is deliberately not entered, so a reviewed start can
be checked at the durable queue boundary without starting a publishing worker.
"""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.database import Database
from app.errors import ConflictError, UpstreamUnavailableError
from app.main import create_app
from test_core import FakeBitBrowserClient


class OfflineClosedProvider(FakeBitBrowserClient):
    """Provide only explicit closure evidence; all browser operations fail."""

    def __init__(self):
        super().__init__()
        self.proof_checks = []
        self.failure = None
        self.proofs_completed = 0

    def _post(self, path, payload=None):
        self.calls.append((path, payload or {}))
        raise AssertionError('Browser/network operations are forbidden in this API test')

    @contextmanager
    def closed_profile_guard(self, profile, owner):
        self.proof_checks.append((profile, owner))
        if self.failure is not None:
            raise self.failure
        yield {
            'closed': True,
            'profile_id': profile,
            'owner_user_id': owner,
            'verification': 'desktop-absence-v1',
        }
        self.proofs_completed += 1


class PostingWithdrawApiR63Tests(unittest.TestCase):
    PROFILE = 'offline-window'
    USERNAME = 'verified.fixture'
    POST = 'queued-post'
    NURTURE = 'old-completed-nurture'
    ASSET = 'preserved-material'
    CAPTION = 'Original reviewed caption\n保留原文案与素材 🌲'
    OLD_TIME = '2026-10-02T12:00:00+00:00'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db = Database(root / 'api.sqlite3')
        self.db.initialize()
        self.provider = OfflineClosedProvider()
        settings = Settings(
            startup_token='offline-withdraw-api-token-123456789',
            database_path=self.db.path,
            data_dir=root,
        )
        self.app = create_app(settings, database=self.db, bitbrowser=self.provider)
        self.posting = self.app.state.posting
        self.posting.recover()
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.users = {}
        self.headers = {}
        for name in ('owner', 'other'):
            username = 'withdraw-api-' + name
            user = self.app.state.service.register_user(username, 'offline fixture password')
            self.users[name] = user['id']
            token = self.app.state.service.login(username, 'offline fixture password')['token']
            self.headers[name] = {
                'X-Startup-Token': settings.startup_token,
                'Authorization': 'Bearer ' + token,
            }
        self.executor = Mock(side_effect=AssertionError('Publishing is forbidden'))
        self.posting.executor_factory = self.executor
        worker_patch = patch(
            'app.studio.PlaywrightWorker',
            side_effect=AssertionError('Real browser workers are forbidden'),
        )
        self.browser_worker = worker_patch.start()
        self.addCleanup(worker_patch.stop)
        self.posting.provider.prepare = Mock(side_effect=AssertionError('Material downloads are forbidden'))
        self.posting.provider.cleanup = Mock(side_effect=AssertionError('Material cleanup is forbidden'))

        image = io.BytesIO()
        Image.new('RGB', (24, 24), '#345678').save(image, format='JPEG')
        self.material_bytes = image.getvalue()
        self.material_path = root / 'preserved.jpg'
        self.material_path.write_bytes(self.material_bytes)
        self.historical_result = {
            'window_hold': True,
            'counts': {'browse': 4, 'like': 2},
            'confirmed_at': self.OLD_TIME,
            'nurture_outcome': 'completed',
            'nurture_finished_at': self.OLD_TIME,
            'window_cleanup': {'state': 'lease_lost', 'lease_token': 'old-retired-token'},
        }
        # This represents the already-broken user state, including a previously
        # verified account. There is intentionally no browser-operation lease.
        with self.db.write() as connection:
            connection.execute(
                '''INSERT INTO studio_jobs
                   (id,owner_user_id,request_key,kind,profile_id,status,config_json,
                    result_json,cursor,total_steps,due_at,created_at,updated_at)
                   VALUES(?,?,?,'nurture',?,'completed','{}',?,4,4,?,?,?)''',
                (self.NURTURE, self.users['owner'], self.NURTURE, self.PROFILE,
                 json.dumps(self.historical_result), self.OLD_TIME, self.OLD_TIME, self.OLD_TIME),
            )
            connection.execute(
                '''INSERT INTO posting_jobs
                   (id,owner_user_id,request_key,theme,caption,profile_id,
                    expected_username,expected_actor_id,asset_id,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)''',
                (self.POST, self.users['owner'], self.POST, 'forest', self.CAPTION,
                 self.PROFILE, self.USERNAME, 'previous-actor', self.ASSET,
                 self.OLD_TIME, self.OLD_TIME),
            )
            connection.execute(
                '''INSERT INTO posting_assets
                   (id,provider_id,job_id,sha256,path,state,source_url,
                    photographer,download_url,created_at)
                   VALUES(?,?,?,?,?,'ready',?,'Offline fixture',?,?)''',
                (self.ASSET, 'offline-provider-id', self.POST,
                 hashlib.sha256(self.material_bytes).hexdigest(), str(self.material_path),
                 'https://www.pexels.com/photo/offline-fixture',
                 'https://images.pexels.com/offline-fixture.jpg', self.OLD_TIME),
            )
            connection.execute(
                '''INSERT INTO posting_account_snapshots
                   (owner_user_id,profile_id,username,posts_count,status,checked_at)
                   VALUES(?,?,?,7,'ok',?)''',
                (self.users['owner'], self.PROFILE, self.USERNAME, self.OLD_TIME),
            )
        self.original_post = self.row('posting_jobs', self.POST)
        self.original_nurture = self.row('studio_jobs', self.NURTURE)
        self.original_asset = self.row('posting_assets', self.ASSET)

    def tearDown(self):
        self.executor.assert_not_called()
        self.browser_worker.assert_not_called()
        self.posting.provider.prepare.assert_not_called()
        self.posting.provider.cleanup.assert_not_called()
        self.assertEqual([], self.provider.calls)
        with self.db.read() as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM posting_receipts').fetchone()[0])
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])

    def row(self, table, ident):
        self.assertIn(table, {'posting_jobs', 'studio_jobs', 'posting_assets'})
        with self.db.read() as connection:
            return dict(connection.execute('SELECT * FROM ' + table + ' WHERE id=?', (ident,)).fetchone())

    def command(self, body, owner='owner'):
        return self.client.post('/api/posting/command', headers=self.headers[owner], json=body)

    def cleanup(self):
        return self.client.post('/api/studio/command', headers=self.headers['owner'], json={
            'action': 'control', 'job_id': self.NURTURE, 'operation': 'retry_cleanup',
        })

    def posting_snapshot(self):
        response = self.client.get('/api/posting/snapshot', headers=self.headers['owner'])
        self.assertEqual(200, response.status_code, response.text)
        return next(job for job in response.json()['jobs'] if job['id'] == self.POST)

    def reviewed(self, job):
        return {key: job[key] for key in ('id', 'caption', 'asset_id', 'profile_id', 'expected_username', 'queue_revision')}

    def assert_preserved(self):
        post = self.row('posting_jobs', self.POST)
        for key in ('caption', 'asset_id', 'theme', 'created_at', 'request_key'):
            self.assertEqual(self.original_post[key], post[key], key)
        self.assertEqual(self.original_asset, self.row('posting_assets', self.ASSET))
        self.assertEqual(self.material_bytes, self.material_path.read_bytes())

    def withdraw(self):
        response = self.command({'action': 'withdraw', 'job_id': self.POST})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(self.POST, response.json()['withdrawn'])
        self.assertTrue(response.json()['requires_assignment'])
        self.assertTrue(response.json()['requires_review'])
        self.assertFalse(response.json()['will_repost'])
        return response

    def test_api_withdraw_cleanup_reassign_and_fresh_reviewed_start(self):
        queued = self.posting_snapshot()
        self.assertTrue(queued['can_withdraw'])
        self.assertFalse(queued['can_modify'])
        stale_review = self.reviewed(queued)
        blocked = self.cleanup()
        self.assertEqual(409, blocked.status_code, blocked.text)
        details = blocked.json()['error']['details']
        self.assertEqual('posting', details['module'])
        self.assertEqual(self.POST, details['job_id'])
        self.assertEqual('queued', details['status'])
        self.assertEqual([], self.provider.proof_checks)
        self.assertEqual(self.original_nurture, self.row('studio_jobs', self.NURTURE))

        self.withdraw()
        draft = self.posting_snapshot()
        self.assertEqual('ready', draft['status'])
        self.assertFalse(draft['can_withdraw'])
        self.assertTrue(draft['can_modify'])
        for key in ('profile_id', 'expected_username', 'expected_actor_id'):
            self.assertEqual('', draft[key], key)
        self.assert_preserved()
        self.assertEqual(self.original_nurture, self.row('studio_jobs', self.NURTURE))
        self.assertEqual([], self.provider.proof_checks)
        with self.db.read() as connection:
            audit = [dict(row) for row in connection.execute('SELECT * FROM posting_withdraw_history')]
        self.assertEqual(1, len(audit))
        for key in ('profile_id', 'expected_username', 'expected_actor_id'):
            self.assertEqual(self.original_post[key], audit[0][key], key)
        self.assertEqual(self.users['owner'], audit[0]['owner_user_id'])
        self.assertEqual(self.POST, audit[0]['job_id'])
        self.assertTrue(audit[0]['withdrawn_at'])
        self.assertEqual(409, self.command({'action': 'withdraw', 'job_id': self.POST}).status_code)
        self.assertEqual(409, self.command({
            'action': 'start', 'job_ids': [self.POST], 'reviewed': [stale_review],
        }).status_code)

        assignment = {'action': 'assign', 'job_id': self.POST,
                      'profile_id': self.PROFILE, 'expected_username': self.USERNAME}
        self.assertEqual(409, self.command(assignment).status_code, 'Historical hold must still block assignment')
        recovered = self.cleanup()
        self.assertEqual(200, recovered.status_code, recovered.text)
        self.assertTrue(recovered.json()['cleanup_reconciled'])
        self.assertFalse(recovered.json()['cleanup_pending'])
        self.assertEqual([(self.PROFILE, self.users['owner'])], self.provider.proof_checks)
        self.assertEqual(1, self.provider.proofs_completed)
        history = self.row('studio_jobs', self.NURTURE)
        result = json.loads(history['result_json'])
        self.assertFalse(result['window_hold'])
        receipt = result['window_cleanup']
        self.assertEqual('reconciled_closed', receipt['state'])
        self.assertEqual('desktop-absence-v1', receipt['reconciliation_evidence']['verification'])
        self.assertEqual('old-retired-token', receipt['lease_token'])
        for key in ('counts', 'confirmed_at', 'nurture_outcome', 'nurture_finished_at'):
            self.assertEqual(self.historical_result[key], result[key], key)
        for key in ('status', 'cursor', 'total_steps', 'created_at', 'updated_at', 'deleted_at'):
            self.assertEqual(self.original_nurture[key], history[key], key)
        response = self.client.get('/api/studio/snapshot', headers=self.headers['owner'])
        self.assertEqual(200, response.status_code, response.text)
        card = next(job for job in response.json()['jobs'] if job['id'] == self.NURTURE)
        self.assertFalse(card['result']['window_hold'])
        self.assertEqual('reconciled_closed', card['result']['window_cleanup']['state'])

        wrong_account = dict(assignment, expected_username='unverified.fixture')
        self.assertEqual(422, self.command(wrong_account).status_code)
        assigned = self.command(assignment)
        self.assertEqual(200, assigned.status_code, assigned.text)
        # Reassigning exactly the same account/content cannot revive the old
        # pre-withdrawal modal or a delayed/replayed start request.
        self.assertEqual(409, self.command({
            'action': 'start', 'job_ids': [self.POST], 'reviewed': [stale_review],
        }).status_code)
        no_revision = dict(stale_review)
        no_revision.pop('queue_revision')
        self.assertEqual(409, self.command({
            'action': 'start', 'job_ids': [self.POST], 'reviewed': [no_revision],
        }).status_code)
        self.assertEqual(422, self.command({'action': 'start', 'job_ids': [self.POST]}).status_code)
        fresh_review = self.reviewed(self.posting_snapshot())
        wrong_review = dict(fresh_review, caption='This was not the reviewed caption')
        self.assertEqual(409, self.command({
            'action': 'start', 'job_ids': [self.POST], 'reviewed': [wrong_review],
        }).status_code)
        self.assertEqual('ready', self.posting_snapshot()['status'])
        started = self.command({'action': 'start', 'job_ids': [self.POST], 'reviewed': [fresh_review]})
        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual([self.POST], started.json()['queued'])
        restarted = self.posting_snapshot()
        self.assertEqual('queued', restarted['status'])
        self.assertEqual(dict(fresh_review,queue_revision=fresh_review['queue_revision']+1), self.reviewed(restarted))
        self.assertTrue(restarted['can_withdraw'])
        self.assertEqual('', restarted['expected_actor_id'])
        self.assertFalse(restarted['window_held'])
        self.assertIsNone(restarted['submitted_at'])
        self.assertEqual('', self.row('posting_jobs', self.POST)['attempt_id'])
        self.assertEqual(1, self.provider.proofs_completed)
        with self.db.read() as connection:
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM posting_withdraw_history').fetchone()[0])
        self.assert_preserved()

    def assert_unverified_cleanup_retains_hold(self, failure, expected_status):
        self.withdraw()
        self.provider.failure = failure
        response = self.cleanup()
        self.assertEqual(expected_status, response.status_code, response.text)
        self.assertEqual([(self.PROFILE, self.users['owner'])], self.provider.proof_checks)
        self.assertEqual(0, self.provider.proofs_completed)
        self.assertEqual(self.original_nurture, self.row('studio_jobs', self.NURTURE))
        self.assertEqual('ready', self.posting_snapshot()['status'])
        self.assertEqual(409, self.command({
            'action': 'assign', 'job_id': self.POST,
            'profile_id': self.PROFILE, 'expected_username': self.USERNAME,
        }).status_code)
        self.assert_preserved()

    def test_open_provider_refuses_cleanup_and_keeps_historical_hold(self):
        self.assert_unverified_cleanup_retains_hold(ConflictError('Fixture profile remains open'), 409)

    def test_unknown_provider_refuses_cleanup_and_keeps_historical_hold(self):
        self.assert_unverified_cleanup_retains_hold(UpstreamUnavailableError('Fixture inventory unavailable'), 503)

    def test_foreign_user_cannot_withdraw_or_change_owner_job(self):
        response = self.command({'action': 'withdraw', 'job_id': self.POST}, owner='other')
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual(self.original_post, self.row('posting_jobs', self.POST))
        self.assertEqual(self.original_nurture, self.row('studio_jobs', self.NURTURE))
        with self.db.read() as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM posting_withdraw_history').fetchone()[0])
        self.assertEqual([], self.provider.proof_checks)

    def test_snapshot_and_command_reject_an_already_claimed_queue_entry(self):
        self.posting.tasks[self.POST] = Mock(done=Mock(return_value=False))
        self.assertEqual('queued', self.posting_snapshot()['status'])
        self.assertFalse(self.posting_snapshot()['can_withdraw'])
        response = self.command({'action': 'withdraw', 'job_id': self.POST})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(self.original_post, self.row('posting_jobs', self.POST))
        self.assertEqual(self.original_nurture, self.row('studio_jobs', self.NURTURE))
        with self.db.read() as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM posting_withdraw_history').fetchone()[0])
        self.assertEqual([], self.provider.proof_checks)


if __name__ == '__main__':
    unittest.main()
