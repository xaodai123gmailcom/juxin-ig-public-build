"""Authenticated manual-operation routes with a controlled stopping barrier.

Only the asynchronous pause acknowledgement and native transport are controlled.
The routes, owner checks, grants, surface fences and ordinary resume remain real.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database
from app.embedded_browser import EmbeddedBrowser
from app.errors import ConflictError
from app.execution_manager import ExecutionControl
from app.main import create_app


class Bridge:
    def __init__(self):
        self.generation = 7
        self.pages = ['source-page', 'screen-page']
        self.visible = False
        self.calls = []
        self.before_hide = lambda: None
        self.hide_error = False

    def call(self, method, **body):
        self.calls.append((method, body))
        if method == 'inventory':
            return {'profiles': []}
        if method == 'watch-profile':
            target = body.get('target') or self.pages[0]
            if target not in self.pages:
                raise ConflictError('任务页面不可用')
            return {'generation': self.generation, 'target': target,
                    'pages': [{'id': page, 'label': page, 'title': page} for page in self.pages],
                    'image': '', 'captured_at': '2026-09-18T10:00:00Z', 'title': target}
        if method == 'show':
            if body.get('target') not in self.pages:
                raise ConflictError('任务页面已关闭')
            self.visible = True
            return {'attached': True}
        if method == 'hide':
            self.before_hide()
            if self.hide_error:
                raise ConflictError('native hide failed')
            self.visible = False
            return {'attached': False}
        raise AssertionError(method)


class Browser:
    def __init__(self, native):
        self.native = native

    def list_all_windows(self, **_kwargs):
        return {'windows': [], 'total': 0, 'provider_success': True, 'stale': False}


class ManualApiR51Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name)
        self.database = Database(data / 'manual.sqlite')
        self.database.initialize()
        self.bridge = Bridge()
        native = EmbeddedBrowser(self.database, data, bridge=self.bridge)
        settings = Settings(startup_token='r51-manual-api-startup-token-long-enough',
                            database_path=data / 'manual.sqlite', data_dir=data)
        self.app = create_app(settings, database=self.database, bitbrowser=Browser(native))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.service = self.app.state.service
        self.accounts = self.app.state.accounts
        self.manager = self.app.state.execution_manager
        self.surface = self.accounts.surface
        password = 'manual API regression password'
        self.headers = {}
        self.owners = {}
        for name in ('manual-owner', 'manual-other'):
            self.owners[name] = self.service.register_user(name, password)['id']
            token = self.service.login(name, password)['token']
            self.headers[name] = {'X-Startup-Token': settings.startup_token,
                                  'Authorization': 'Bearer ' + token}
        self.owner = self.owners['manual-owner']
        self.ident = self.accounts.save(self.owner, {'name': 'Manual window', 'native': True})['id']
        row = self.accounts.get(self.owner, self.ident)
        self.profile = row['profile_id']
        # Continue now rechecks the durable task before waking its window.
        # Use a real owned task rather than a lease pointing to a missing row.
        self.task_id = self.service.create_task(self.owner, name='manual API task',
            modes=['followers'], targets=['manual_source'], window_ids=[self.profile], settings={})['id']
        self.token = self.service.acquire_browser_lease(self.owner, self.profile,
            operation_type='collection', entity_id=self.task_id)
        self.key = self.surface.task_key({'lease_token': self.token})
        self.control = ExecutionControl(owner_user_id=self.owner, task_id=self.task_id,
            pause_event=asyncio.Event(), stop_event=asyncio.Event(),
            leases={self.profile: self.token}, target_queue=asyncio.Queue())
        self.control.pause_event.set()
        self.control.coordinator = SimpleNamespace(done=lambda: False)
        self.control.profile_worker_tasks[self.profile] = SimpleNamespace(done=lambda: False)
        self.control.profile_pause_events[self.profile] = asyncio.Event()
        self.control.profile_pause_events[self.profile].set()
        self.control.profile_stop_events[self.profile] = asyncio.Event()
        self.control.manual_events[self.profile] = asyncio.Event()
        self.manager._runs[self.task_id] = self.control
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error = None

        async def begin(owner, profile, lease):
            self.assertEqual((self.owner, self.profile, self.token), (owner, profile, lease))
            self.entered.set()
            if not await asyncio.to_thread(self.release.wait, 5):
                raise ConflictError('pause test deadline')
            if self.error:
                raise self.error
            self.control.profile_pause_events[profile].clear()
            self.control.manual_requests[profile] = {'state': 'active', 'lease_token': lease,
                'settled': asyncio.Event(), 'resume': asyncio.Event(), 'children': {}}
            self.control.manual_requests[profile]['settled'].set()
            return {'status': 'manual_control'}
        self.manager.begin_manual_control = begin

    def post(self, path, body, owner='manual-owner'):
        return self.client.post(path, headers=self.headers[owner], json=body)

    def begin_body(self):
        return {'id': self.ident, 'task_key': self.key, 'target': 'source-page'}

    def begin(self):
        self.release.set()
        response = self.post('/api/accounts/interference', self.begin_body())
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def show(self, permit=None, read_only=False):
        return self.post('/api/accounts/surface', {
            'id': self.ident, 'visible': True, 'view_target': 'source-page',
            'read_only': read_only, 'interference_grant': permit and permit['grant'],
            'grant': 'desktop-grant', 'bounds': {'x': 100, 'y': 100, 'width': 900, 'height': 650}})

    def assert_same_lease(self):
        with self.database.read() as connection:
            lease = connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',
                                       (self.profile,)).fetchone()
        self.assertEqual((self.token, self.task_id), (lease['lease_token'], lease['entity_id']))

    def test_no_input_grant_is_published_before_the_pause_barrier_acknowledges(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, '/api/accounts/interference', self.begin_body())
            try:
                self.assertTrue(self.entered.wait(3))
                self.assertFalse(pending.done())
                self.assertEqual({}, self.surface.interference)
                self.assertEqual(409, self.show().status_code)
                self.assertEqual(200, self.show(read_only=True).status_code)
                self.assert_same_lease()
            finally:
                self.release.set()
            response = pending.result(5)
        self.assertEqual(200, response.status_code, response.text)
        permit = response.json()
        self.assertEqual((self.key, 'source-page'), (permit['task_key'], permit['target']))
        self.assertEqual(200, self.show(permit).status_code)
        watch = self.post('/api/accounts/watch', {'id': self.ident, 'target': 'source-page'})
        self.assertEqual(permit['grant'], watch.json()['manual_control']['grant'])
        self.assert_same_lease()

    def test_target_or_generation_changed_during_stop_never_receives_authorization(self):
        for changed in ('target', 'generation'):
            with self.subTest(changed=changed):
                self.entered.clear(); self.release.clear()
                self.bridge.pages = ['source-page', 'screen-page']
                self.control.manual_requests.clear()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(self.post, '/api/accounts/interference', self.begin_body())
                    try:
                        self.assertTrue(self.entered.wait(3))
                        if changed == 'target':
                            self.bridge.pages = ['screen-page']
                        else:
                            self.bridge.generation += 1
                    finally:
                        self.release.set()
                    response = pending.result(5)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual({}, self.surface.interference)
                self.assert_same_lease()

    def test_changed_lease_during_pause_rejects_the_old_confirmation(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, '/api/accounts/interference', self.begin_body())
            try:
                self.assertTrue(self.entered.wait(3))
                self.service.release_browser_lease(self.profile, self.token)
                replacement = self.service.acquire_browser_lease(self.owner, self.profile,
                    operation_type='collection', entity_id='replacement')
            finally:
                self.release.set()
            response = pending.result(5)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual({}, self.surface.interference)
        self.assertNotEqual(self.token, replacement)

    def test_pause_failure_stays_read_only_without_releasing_the_window(self):
        self.error = ConflictError('pause could not settle')
        self.release.set()
        response = self.post('/api/accounts/interference', self.begin_body())
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual({}, self.surface.interference)
        self.assertEqual(409, self.show().status_code)
        self.assertEqual(200, self.show(read_only=True).status_code)
        self.assert_same_lease()

    def test_wrong_owner_or_missing_login_cannot_reach_the_pause_barrier(self):
        response = self.post('/api/accounts/interference', self.begin_body(), 'manual-other')
        self.assertEqual(404, response.status_code, response.text)
        response = self.client.post('/api/accounts/interference', json=self.begin_body())
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(self.entered.is_set())
        self.assertEqual({}, self.surface.interference)

    def test_existing_window_continue_revokes_native_input_before_resuming_worker(self):
        permit = self.begin()
        self.assertEqual(200, self.show(permit).status_code)
        observations = []
        self.bridge.before_hide = lambda: observations.append(self.control.profile_pause_events[self.profile].is_set())
        response = self.post(f'/api/tasks/{self.task_id}/windows/{self.profile}/resume', {})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([False], observations)
        self.assertFalse(self.bridge.visible)
        self.assertTrue(self.control.profile_pause_events[self.profile].is_set())
        self.assertEqual({}, self.surface.interference)
        self.assertEqual(409, self.show(permit).status_code)
        self.assert_same_lease()

    def test_resume_route_checks_the_grant_then_hides_input_before_resuming(self):
        permit = self.begin()
        self.assertEqual(200, self.show(permit).status_code)
        body = {'id': self.ident, 'task_key': self.key, 'grant': permit['grant']}
        denied = self.post('/api/accounts/interference/resume', {**body, 'grant': 'stale-grant'})
        self.assertEqual(409, denied.status_code, denied.text)
        self.assertTrue(self.bridge.visible)
        observations = []
        self.bridge.before_hide = lambda: observations.append(self.control.profile_pause_events[self.profile].is_set())
        response = self.post('/api/accounts/interference/resume', body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['resumed'])
        self.assertTrue(observations)
        self.assertFalse(any(observations))
        self.assertFalse(self.bridge.visible)
        self.assertTrue(self.control.profile_pause_events[self.profile].is_set())
        self.assert_same_lease()

    def test_failed_native_hide_prevents_existing_continue_from_running_automation(self):
        permit = self.begin()
        self.assertEqual(200, self.show(permit).status_code)
        self.bridge.hide_error = True
        response = self.post(f'/api/tasks/{self.task_id}/windows/{self.profile}/resume', {})
        self.assertEqual(409, response.status_code, response.text)
        self.assertFalse(self.control.profile_pause_events[self.profile].is_set())
        self.assertFalse(self.control.manual_requests[self.profile].get('resume_requested', False))
        self.assertEqual(permit['grant'], self.surface.manual_info(self.owner, self.accounts.get(self.owner, self.ident))['grant'])
        self.assert_same_lease()

    def test_new_confirmation_waits_for_old_resume_and_old_grant_cannot_revoke_new_grant(self):
        import httpx
        old = self.begin()
        original_end = self.manager.end_manual_control
        original_begin = self.manager.begin_manual_control
        original_get = self.accounts.get

        async def scenario():
            resume_entered, finish_resume = asyncio.Event(), asyncio.Event()
            queued_row_read = threading.Event()
            order = []

            async def held_end(*args):
                resume_entered.set()
                await finish_resume.wait()
                order.append('resume')
                return await original_end(*args)

            async def tracked_begin(*args):
                order.append('begin')
                return await original_begin(*args)

            def tracked_get(*args):
                row = original_get(*args)
                queued_row_read.set()
                return row

            self.manager.end_manual_control = held_end
            self.manager.begin_manual_control = tracked_begin
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url='http://testserver', headers=self.headers['manual-owner']) as client:
                body = {'id': self.ident, 'task_key': self.key, 'grant': old['grant']}
                resuming = asyncio.create_task(client.post('/api/accounts/interference/resume', json=body))
                confirming = None
                try:
                    await asyncio.wait_for(resume_entered.wait(), 3)
                    self.assertFalse(self.bridge.visible)
                    self.accounts.get = tracked_get
                    confirming = asyncio.create_task(client.post('/api/accounts/interference', json=self.begin_body()))
                    self.assertTrue(await asyncio.to_thread(queued_row_read.wait, 3))
                    for _ in range(5):
                        await asyncio.sleep(0)
                    self.assertEqual([], order, 'new confirmation cannot interleave between revoke and resume')
                    self.assertFalse(confirming.done())
                    finish_resume.set()
                    response = await asyncio.wait_for(resuming, 3)
                    self.assertEqual(200, response.status_code, response.text)
                    response = await asyncio.wait_for(confirming, 3)
                    self.assertEqual(200, response.status_code, response.text)
                    current = response.json()
                    self.assertEqual(['resume', 'begin'], order)
                    self.assertNotEqual(old['grant'], current['grant'])
                    stale = await client.post('/api/accounts/interference/resume', json=body)
                    self.assertEqual(409, stale.status_code, stale.text)
                    state = self.surface.manual_info(self.owner, original_get(self.owner, self.ident))
                    self.assertEqual(current['grant'], state['grant'])
                    self.assertFalse(self.control.profile_pause_events[self.profile].is_set())
                finally:
                    finish_resume.set()
                    await asyncio.gather(resuming, *([confirming] if confirming else []), return_exceptions=True)
        try:
            asyncio.run(scenario())
        finally:
            self.accounts.get = original_get
            self.manager.end_manual_control = original_end
            self.manager.begin_manual_control = original_begin
