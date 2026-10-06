"""Offline cloud configuration regressions using mocked network adapters."""
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.cloud_workspace import CloudWorkspace, SupabaseAPI
from app.config import Settings, validate_cloud_configuration
from app.errors import ConflictError, ValidationError

PROJECT = 'https://example.supabase.co'
KEY = 'sb_publishable_offline_fixture'


class CloudConfigurationTests(unittest.TestCase):
    def test_default_backend_configuration_is_disabled_and_has_no_endpoint_or_key(self):
        # An empty Windows environment cannot resolve Path.home(). Keep cloud
        # variables isolated and make the unrelated data-directory fixture explicit.
        with patch.dict(os.environ, {'IGAC_STARTUP_TOKEN': 'x' * 32}, clear=True), \
                patch('app.config._default_data_dir', return_value=Path('/offline-fixture')), \
                patch('app.config.Path.home', side_effect=RuntimeError('Home directory unavailable')) as home:
            settings = Settings.from_env()
        home.assert_not_called()
        self.assertEqual(Path('/offline-fixture'), settings.data_dir)
        self.assertFalse(settings.cloud_enabled)
        self.assertEqual('', settings.supabase_url)
        self.assertEqual('', settings.supabase_publishable_key)

    def test_cloud_environment_needs_explicit_enable_flag(self):
        with patch.dict(os.environ, {'IGAC_STARTUP_TOKEN': 'x' * 32,
                'IGAC_SUPABASE_URL': PROJECT, 'IGAC_SUPABASE_PUBLISHABLE_KEY': KEY}, clear=True), \
                patch('app.config._default_data_dir', return_value=Path('/offline-fixture')), \
                patch('app.config.Path.home', side_effect=RuntimeError('Home directory unavailable')) as home:
            settings = Settings.from_env()
        home.assert_not_called()
        self.assertEqual(Path('/offline-fixture'), settings.data_dir)
        self.assertFalse(settings.cloud_enabled)
        self.assertNotIn(KEY, repr(settings))
        self.assertEqual(PROJECT, settings.supabase_url)

    def test_cloud_configuration_requires_valid_origin_and_nonprivileged_key(self):
        self.assertEqual((True, PROJECT, KEY), validate_cloud_configuration(True, PROJECT + '/', KEY))
        for url in ('http://example.supabase.co', PROJECT + '/path', PROJECT + '?token=secret',
                    'https://user:password@example.supabase.co', 'https://localhost',
                    'https://127.0.0.1', 'https://host.internal'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_cloud_configuration(True, url, KEY)
        for key in ('', 'sb_secret_fixture', 'credential with spaces'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_cloud_configuration(True, PROJECT, key)
        for role, allowed in [('anon', True), ('service_role', False)]:
            payload = base64.urlsafe_b64encode(json.dumps({'role': role}).encode()).decode().rstrip('=')
            key = 'header.' + payload + '.signature'
            if allowed:
                self.assertEqual(key, validate_cloud_configuration(True, PROJECT, key)[2])
            else:
                with self.assertRaises(ValueError):
                    validate_cloud_configuration(True, PROJECT, key)

    def test_unconfigured_api_never_constructs_a_network_opener(self):
        with patch('app.cloud_workspace.urllib.request.build_opener') as opener:
            for api in (SupabaseAPI(), SupabaseAPI(project_url=PROJECT, publishable_key=KEY)):
                with self.assertRaises(ValidationError):
                    api.request('/auth/v1/settings')
            opener.assert_not_called()

    def test_no_background_worker_or_network_commands_before_opt_in(self):
        cloud = CloudWorkspace(SimpleNamespace(database=None), Path('/offline-fixture'))
        with patch('app.cloud_workspace.threading.Thread') as thread, patch.object(cloud.api, 'request') as request:
            cloud.start()
            cloud.sync('owner')
            for action in ('probe', 'signup', 'login', 'sync'):
                with self.assertRaises(ValidationError):
                    cloud.command('owner', {'action': action})
            thread.assert_not_called()
            request.assert_not_called()

    def test_disable_clears_sessions_and_never_acknowledges_an_inflight_request(self):
        cloud = CloudWorkspace(SimpleNamespace(database=None), Path('/offline-fixture'),
                               enabled=True, project_url=PROJECT, publishable_key=KEY)
        cloud.sessions['owner'] = {'access_token': 'fixture-session'}
        with cloud.sync_gate:
            with self.assertRaises(ConflictError):
                cloud.configure(enabled=False, project_url='', publishable_key='')
            self.assertTrue(cloud.configured)
        result = cloud.configure(enabled=False, project_url='', publishable_key='')
        self.assertEqual({'configured': False, 'enabled': False, 'project_url': '', 'activated': True}, result)
        self.assertEqual({}, cloud.sessions)
        self.assertFalse(cloud.configured)

    def test_existing_positional_settings_contract_is_unchanged(self):
        settings = Settings('x' * 32, Path('/db'), Path('/data'), 'http://127.0.0.1:54345', None, 24, '127.0.0.1', 8765)
        self.assertEqual(24, settings.session_hours)
        self.assertFalse(settings.cloud_enabled)
