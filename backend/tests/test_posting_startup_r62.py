"""Offline posting startup and exact-window ownership regressions; no live IG."""
import asyncio
import hashlib
import tempfile
import unittest
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.service import CoreService
from app.account_workspace import AccountWorkspace
from app.embedded_browser import EmbeddedBrowser
from app.native_browser import BrowserHub
from app.posting_executor import PostingExecutor
from app.posting_schema import initialize_posting_schema
from app.errors import ConflictError, ValidationError
from app.standalone_nurture import OWN_LINK, OWN_METRICS
from test_embedded_browser import Bridge


class PostingIdentityReadinessR62Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'test.sqlite3')
        self.db.initialize()
        with self.db.write() as connection:
            initialize_posting_schema(connection)
            connection.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,status,created_at,updated_at) VALUES('job','owner','key','theme','caption','running','now','now')")
        self.job = {'id': 'job', 'profile_id': 'window', 'expected_username': 'expected',
                    'expected_actor_id': '', 'caption': 'caption'}
        path = Path(self.temp.name) / 'asset.jpg'
        path.write_bytes(b'offline fixture only')
        self.asset = {'path': str(path), 'render_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.calls = []
        self.link_reads = ['', '', 'expected']
        self.metric_reads = [None, {}, {}]
        self.uid = '123456789'
        self.connect_error = None
        self.fail_checkpoint = False
        self.stall_link_read = False
        self.swap_cookie_on_metrics = False
        fixture = self

        class Page:
            def __init__(self, context): self.context = context
            async def goto(self, url, **kwargs): fixture.calls.append('proof_navigation')
            async def close(self): fixture.calls.append('proof_closed')
            async def evaluate(self, script, *args):
                if script == OWN_LINK:
                    fixture.calls.append('link_read')
                    if fixture.stall_link_read: await asyncio.Event().wait()
                    if len(fixture.link_reads) > 1: return fixture.link_reads.pop(0)
                    return fixture.link_reads[0]
                if script == OWN_METRICS:
                    fixture.calls.append('metrics_read')
                    if fixture.swap_cookie_on_metrics: fixture.uid = '987654321'
                    if len(fixture.metric_reads) > 1: return fixture.metric_reads.pop(0)
                    return fixture.metric_reads[0]
                raise AssertionError('unexpected script')

        class Context:
            async def cookies(self, url): return [{'name': 'ds_user_id', 'value': fixture.uid}]
            async def new_page(self): return Page(self)

        class Worker:
            def __init__(self, *args): self.page = Page(Context())
            async def connect(self, *args, **kwargs):
                fixture.calls.append('connect')
                if fixture.connect_error: raise fixture.connect_error
            @asynccontextmanager
            async def _destructive_action_lease(self): yield

        class Browser:
            def __init__(self, worker, checkpoint, before): self.before = before
            async def publish(self, *args):
                await self.account_snapshot({'username': 'expected'})
                fixture.calls.append('upload')
                await self.before('share')
                fixture.calls.append('submit')
                return {}

        self.worker_type = Worker
        self.browser_type = Browser
        self.executor = PostingExecutor(SimpleNamespace(db=self.db, bitbrowser=None))

    async def publish(self, *, probe_sleep=None):
        async def checkpoint():
            self.calls.append('checkpoint')
            if self.fail_checkpoint and 'proof_navigation' in self.calls: raise ConflictError('lease changed')
        async def before_submit(): self.calls.append('before_submit')
        async def confirmed(*args): raise AssertionError('fixture must not claim a live publication')
        polling = patch('app.instagram_identity.asyncio.sleep', probe_sleep) if probe_sleep else nullcontext()
        with patch('app.posting_executor.PlaywrightWorker', self.worker_type), patch('app.posting_executor.StudioBrowser', self.browser_type), polling:
            return await self.executor.publish(self.job, self.asset, checkpoint, before_submit, confirmed)

    async def test_delayed_profile_render_is_awaited_before_upload(self):
        # Hydration in this fixture advances per probe, not per wall-clock tick.
        # Preserve the production deadline and make each simulated wait yield
        # cooperatively. A 40 ms budget with three 1 ms sleeps is not portable:
        # Windows can round each timer wakeup to a much coarser clock quantum.
        real_sleep = asyncio.sleep
        waits = []
        async def next_render(delay):
            waits.append(delay)
            await real_sleep(0)
        await self.publish(probe_sleep=next_render)
        self.assertEqual([PostingExecutor.identity_poll_seconds] * 3, waits)
        self.assertGreaterEqual(self.calls.count('link_read'), 3)
        self.assertGreaterEqual(self.calls.count('metrics_read'), 2)
        self.assertLess(self.calls.index('proof_closed'), self.calls.index('upload'))
        self.assertEqual(1, self.calls.count('before_submit'))
        self.assertEqual('submission', self.job['_posting_phase'])
        self.assertEqual([], self.executor.owned_tabs)

    async def test_success_proof_survives_coarse_timer_wakeups(self):
        # Emulate coarse/loaded Windows wakeups without changing the production
        # deadline. Keep all identity, close-before-upload and single-submit
        # assertions; this is not permission to retry a mismatched account.
        real_sleep = asyncio.sleep
        self.executor.identity_poll_seconds = .001
        for quantum in (.016, .032, .064):
            for repeat in range(4):
                with self.subTest(quantum=quantum, repeat=repeat):
                    self.calls.clear()
                    self.link_reads = ['', '', 'expected']
                    self.metric_reads = [None, {}, {}]
                    waits = []
                    async def coarse_sleep(delay):
                        waits.append(delay)
                        await real_sleep(max(quantum, delay))
                    await self.publish(probe_sleep=coarse_sleep)
                    self.assertEqual([.001] * 3, waits)
                    self.assertGreaterEqual(self.calls.count('link_read'), 3)
                    self.assertGreaterEqual(self.calls.count('metrics_read'), 2)
                    self.assertLess(self.calls.index('proof_closed'), self.calls.index('upload'))
                    self.assertEqual(1, self.calls.count('before_submit'))
                    self.assertEqual([], self.executor.owned_tabs)
        self.assertEqual(PostingExecutor.identity_ready_timeout_seconds, self.executor.identity_ready_timeout_seconds)

    async def test_visible_wrong_sidebar_identity_never_retries_or_uploads(self):
        self.link_reads = ['different_user', 'expected']
        with self.assertRaises(ValidationError): await self.publish()
        self.assertEqual(1, self.calls.count('link_read'))
        self.assertNotIn('upload', self.calls)
        self.assertIn('proof_closed', self.calls)

    async def test_expected_actor_mismatch_never_opens_proof_tab(self):
        self.job['expected_actor_id'] = '987654321'
        with self.assertRaises(ValidationError): await self.publish()
        self.assertNotIn('proof_navigation', self.calls)
        self.assertNotIn('upload', self.calls)

    async def test_absent_own_profile_proof_times_out_without_upload(self):
        # Only timeout-negative tests shorten the production deadline.
        self.executor.identity_ready_timeout_seconds = .04
        self.executor.identity_poll_seconds = .001
        self.link_reads = ['expected']
        self.metric_reads = [None]
        with self.assertRaises(ValidationError) as caught: await self.publish()
        self.assertEqual('posting_identity_unverified', caught.exception.details['reason'])
        self.assertNotIn('upload', self.calls)
        self.assertIn('proof_closed', self.calls)

    async def test_checkpoint_loss_during_readiness_reclaims_proof_tab(self):
        self.fail_checkpoint = True
        with self.assertRaises(ConflictError): await self.publish()
        self.assertNotIn('upload', self.calls)
        self.assertIn('proof_closed', self.calls)
        self.assertEqual([], self.executor.owned_tabs)

    async def test_stalled_readiness_probe_is_bounded_and_reclaimed(self):
        self.executor.identity_ready_timeout_seconds = .04
        self.stall_link_read = True
        with self.assertRaises(ValidationError) as caught:
            await asyncio.wait_for(self.publish(), timeout=1)
        self.assertEqual('posting_identity_unverified', caught.exception.details['reason'])
        self.assertNotIn('upload', self.calls)
        self.assertIn('proof_closed', self.calls)

    async def test_cookie_change_during_ready_probe_blocks_upload(self):
        self.link_reads = ['expected']
        self.metric_reads = [{}]
        self.swap_cookie_on_metrics = True
        with self.assertRaises(ValidationError) as caught: await self.publish()
        self.assertEqual('posting_account_mismatch', caught.exception.details['reason'])
        self.assertNotIn('upload', self.calls)
        self.assertIn('proof_closed', self.calls)

    async def test_connection_failure_retains_browser_open_phase(self):
        self.connect_error = ConflictError('synthetic startup failure')
        with self.assertRaises(ConflictError): await self.publish()
        self.assertEqual('browser_open', self.job['_posting_phase'])
        self.assertNotIn('upload', self.calls)


class PostingEmbeddedLeaseStartupR62Tests(unittest.TestCase):
    def test_posting_lease_allows_own_ensure_and_still_blocks_management(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'test.sqlite3'); db.initialize()
            service = CoreService(db)
            owner = service.register_user('posting-startup', 'offline fixture password')['id']
            bridge = Bridge()
            native = EmbeddedBrowser(db, folder, bridge=bridge)
            hub = BrowserHub(native, SimpleNamespace(status=lambda: {'connected': False}))
            accounts = AccountWorkspace(service, hub)
            plan = accounts.save(owner, {'name': 'offline posting', 'native': True})
            profile = accounts.get(owner, plan['id'])['profile_id']
            with db.write() as connection:
                initialize_posting_schema(connection)
                connection.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,status,profile_id,created_at,updated_at) VALUES('job',?,'key','theme','caption','queued',?,'now','now')", (owner, profile))
            token = service.acquire_browser_lease(owner, profile, operation_type='posting', entity_id='job')
            with db.write() as connection:
                connection.execute("UPDATE posting_jobs SET lease_token=?,status='running' WHERE id='job'", (token,))
            attempt = hub.begin_connection_attempt(profile)
            endpoint = hub.connection_endpoint(profile, open_if_needed=True, attempt_id=attempt)
            hub.verify_connection_endpoint(profile, endpoint['ws'], endpoint['generation'], attempt)
            hub.commit_connection_attempt(attempt)
            self.assertIn(profile, bridge.live)
            self.assertEqual('hide', bridge.calls[0][0])
            for action in ('open', 'close'):
                with self.subTest(action=action), self.assertRaises(ConflictError):
                    accounts.control_profile(owner, profile, action)
            with db.read() as connection:
                lease = connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone()
            self.assertEqual((owner, 'posting', 'job', token), tuple(lease[k] for k in ('owner_user_id','operation_type','entity_id','lease_token')))
            self.assertTrue(hub.close_profile(profile)['closed'])
            with db.read() as connection:
                self.assertEqual(token, connection.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
