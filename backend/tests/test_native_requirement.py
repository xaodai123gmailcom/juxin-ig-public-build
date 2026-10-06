"""Explicit Chrome dependency: fail closed without touching account data."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
from app.browser_runtime import select_browser_runtime
from app.browser_requirement import read_requirement
from app.errors import UpstreamUnavailableError
from browser_build_policy import requirement_from_report, save_json, installed_candidate


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='Chrome 兼容 (6) ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.profile = self.root / 'account'; self.profile.mkdir()
        self.cookie = self.profile / 'Cookies.fixture'; self.cookie.write_bytes(b'keep')
        self.chrome = self.root / 'Program Files/Google/Chrome/Application/chrome.exe'
        self.chrome.parent.mkdir(parents=True); self.chrome.write_bytes(b'fixture')
        self.policy = self.root / 'juxin-runtime-requirement.json'
        self.record = {'format': 1, 'mode': 'installed-chrome-required', 'minimum_version': '154.0.8037.58'}
        save_json(self.policy, self.record)
        self.enterContext(patch.dict(os.environ, {'IGAC_RUNTIME_REQUIREMENT': str(self.policy),
            'IGAC_NATIVE_BROWSER_EXECUTABLE': ''}))
        self.enterContext(patch('app.browser_runtime.installed_chrome', return_value=self.chrome))
        self.enterContext(patch('app.browser_runtime.windows_file_version', return_value='154.0.8037.58'))
        self.bundle = self.enterContext(patch('app.browser_runtime._bundled_or_development_executable',
                                               side_effect=AssertionError('must not launch bundled runtime')))

    def select(self):
        return select_browser_runtime(self.profile, windows=True)

    def reject(self, reason):
        before = {p.name: p.read_bytes() for p in self.profile.iterdir()}
        with self.assertRaises(UpstreamUnavailableError) as caught: self.select()
        self.assertEqual(reason, caught.exception.details['reason'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.profile.iterdir()})
        self.bundle.assert_not_called()

    def test_correct_chrome_records_engine_only_in_owned_profile(self):
        self.assertEqual('installed-chrome', self.select()['source'])
        self.assertEqual(b'keep', self.cookie.read_bytes())
        self.assertEqual(str(self.chrome), json.loads((self.profile / 'juxin-runtime.json').read_text())['executable'])

    def test_missing_chrome_never_falls_back_to_unverified_bundle(self):
        with patch('app.browser_runtime.installed_chrome', return_value=None):
            self.reject('native_runtime_chrome_required')

    def test_old_chrome_is_rejected_without_profile_changes(self):
        with patch('app.browser_runtime.windows_file_version', return_value='153.0.8000.1'):
            self.reject('native_runtime_chrome_too_old')

    def test_updated_chrome_is_allowed(self):
        with patch('app.browser_runtime.windows_file_version', return_value='155.0.9000.1'):
            self.assertEqual('155.0.9000.1', self.select()['version'])

    def test_missing_policy_is_not_an_implicit_standard_edition(self):
        self.policy.unlink(); self.reject('native_runtime_requirement_invalid')

    def test_bad_policy_is_rejected(self):
        for data in (b'broken', b'{}', b'[]', b'x' * 8193,
                     json.dumps({**self.record, 'format': True}).encode(),
                     json.dumps({**self.record, 'minimum_version': '../chrome'}).encode()):
            with self.subTest(data=data[:70]):
                self.policy.write_bytes(data); self.reject('native_runtime_requirement_invalid')

    def test_override_cannot_substitute_broken_bundle(self):
        with patch.dict(os.environ, {'IGAC_NATIVE_BROWSER_EXECUTABLE': str(self.root / 'bad/chrome.exe')}):
            self.reject('native_runtime_chrome_override_rejected')

    def test_same_installed_override_is_allowed(self):
        with patch.dict(os.environ, {'IGAC_NATIVE_BROWSER_EXECUTABLE': str(self.chrome)}):
            self.assertEqual('installed-chrome', self.select()['source'])

    def test_existing_newer_profile_is_never_downgraded(self):
        save_json(self.profile / 'juxin-runtime.json', {'source': 'bundled',
            'version': '155.0.9000.1', 'executable': str(self.root / 'old/chrome.exe')})
        self.reject('native_runtime_downgrade')

    def test_explicit_compatible_migration_preserves_owned_profile(self):
        save_json(self.profile / 'juxin-runtime.json', {'source': 'bundled',
            'version': '151.0.7922.34', 'executable': str(self.root / 'old/chrome.exe')})
        self.assertEqual('installed-chrome', self.select()['source'])
        self.assertEqual(b'keep', self.cookie.read_bytes())

    def test_missing_dependency_on_non_windows_is_clear(self):
        with self.assertRaises(UpstreamUnavailableError) as caught:
            select_browser_runtime(self.profile, windows=False)
        self.assertEqual('native_runtime_chrome_required', caught.exception.details['reason'])

    def test_development_without_policy_is_unchanged(self):
        with patch.dict(os.environ, {'IGAC_RUNTIME_REQUIREMENT': ''}):
            self.assertIsNone(read_requirement())
            self.assertEqual('installed-chrome', self.select()['source'])


class BuildSealTests(unittest.TestCase):
    def report(self):
        return {'status': 'passed', 'headless': False, 'require_installed_chrome': True,
                'selected_runtime': {'source': 'installed-chrome', 'version': '154.0.8037.58'},
                'actual_browser_versions': ['154.0.8037.58'] * 2,
                'network_verified': True, 'isolation_verified': True,
                'persistence_verified': True, 'ownership_verified': True}

    def test_complete_report_seals_minimum_version_without_builder_path(self):
        record = requirement_from_report(self.report(), installed=True)
        self.assertEqual({'format': 1, 'mode': 'installed-chrome-required',
                          'minimum_version': '154.0.8037.58'}, record)

    def test_cdp_response_alone_cannot_seal_a_build(self):
        with self.assertRaises(RuntimeError):
            requirement_from_report({'status': 'cdp_responded'}, installed=True)

    def test_every_native_check_is_mandatory(self):
        for key in ('network_verified', 'isolation_verified', 'persistence_verified', 'ownership_verified'):
            for invalid in (None, False, 1, 'true'):
                with self.subTest(key=key, invalid=invalid):
                    report = self.report(); report[key] = invalid
                    with self.assertRaises(RuntimeError): requirement_from_report(report, installed=True)

    def test_failed_headless_wrong_engine_changed_version_are_rejected(self):
        changes = [{'status': 'failed'}, {'headless': True}, {'require_installed_chrome': False},
                   {'selected_runtime': {'source': 'override', 'version': '154.0.8037.58'}},
                   {'actual_browser_versions': ['154.0.8037.58']},
                   {'actual_browser_versions': ['154.0.8037.58', '155.0.0.1']}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                requirement_from_report({**self.report(), **change}, installed=True)

    def test_missing_chrome_cannot_create_candidate(self):
        with patch('browser_build_policy.os', SimpleNamespace(name='nt')), \
                patch('app.browser_runtime.installed_chrome', return_value=None), self.assertRaises(RuntimeError):
            installed_candidate()

    def test_atomic_policy_write_failure_keeps_previous_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'; path.write_bytes(b'previous')
            with patch.object(Path, 'replace', side_effect=PermissionError('locked')), self.assertRaises(PermissionError):
                save_json(path, self.report())
            self.assertEqual(b'previous', path.read_bytes())
            self.assertFalse(path.with_suffix('.json.tmp').exists())


class InstalledVerifierTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('chrome_requirement_verifier', ROOT / 'scripts/verify_native_browser.py')
        self.verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.verify)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = self.root / 'policy.json'
        self.policy.write_text('previous success must be removed')
        self.args = ['--no-headless', '--require-installed-chrome', '--policy-output', str(self.policy),
                     '--diagnostics-dir', str(self.root / 'diagnostics')]
        self.enterContext(patch.dict(os.environ, {'IGAC_NATIVE_BROWSER_EXECUTABLE': 'old-override',
                                                'IGAC_BROWSER_DIR': str(self.root / 'broken-bundle')}))
        self.enterContext(patch.object(self.verify, 'os', SimpleNamespace(name='nt', environ=os.environ)))
        self.enterContext(patch.object(self.verify, 'installed_chrome', return_value=self.root / 'chrome.exe'))
        self.enterContext(patch.object(self.verify, 'pinned_chromium', return_value=('1234', '151.0.7922.34')))
        self.bundle = self.enterContext(patch.object(self.verify, 'bundled_chromium',
            side_effect=AssertionError('explicit installed mode must not inspect failed bundle')))
        self.choice = {'source': 'installed-chrome', 'executable': str(self.root / 'chrome.exe'),
                       'version': '154.0.8037.58'}
        self.enterContext(patch.object(self.verify, 'select_browser_runtime', return_value=self.choice))
        self.server = Mock(server_port=12345)
        self.enterContext(patch.object(self.verify, 'ThreadingHTTPServer', return_value=self.server))
        self.enterContext(patch.object(self.verify, 'collect_fixture_diagnostics', return_value={}))
        self.enterContext(patch.object(self.verify.tempfile, 'mkdtemp', side_effect=self.tempdir))

    def tempdir(self, *, prefix, dir=None):
        folder = self.root / ('run-test' if dir else 'fixture'); folder.mkdir(exist_ok=True)
        return str(folder)

    def test_full_gate_seals_dependency_and_restores_override(self):
        report = BuildSealTests().report()
        async def full_gate(*args, **kwargs):
            self.assertNotIn('IGAC_NATIVE_BROWSER_EXECUTABLE', os.environ)
            self.assertIs(False, kwargs['headless'])
            return {k: report[k] for k in ('actual_browser_versions', 'network_verified',
                'isolation_verified', 'persistence_verified', 'ownership_verified')}
        with patch.object(self.verify, 'verify_with_timeout', side_effect=full_gate) as gate:
            self.verify.main(self.args)
        gate.assert_called_once(); self.bundle.assert_not_called()
        self.assertEqual('installed-chrome-required', json.loads(self.policy.read_text())['mode'])
        self.assertEqual('old-override', os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE'])
        self.server.server_close.assert_called_once()

    def test_network_failure_removes_previous_seal_and_keeps_original_error(self):
        with patch.object(self.verify, 'verify_with_timeout', new=AsyncMock(side_effect=RuntimeError('network failed'))), \
                self.assertRaisesRegex(RuntimeError, 'network failed'):
            self.verify.main(self.args)
        self.assertFalse(self.policy.exists())
        self.assertEqual('old-override', os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE'])

    def test_absent_chrome_does_not_fall_back_or_start_fixture(self):
        with patch.object(self.verify, 'installed_chrome', return_value=None), \
                patch.object(self.verify, 'verify_with_timeout') as gate, self.assertRaisesRegex(RuntimeError, 'requires Google Chrome'):
            self.verify.main(self.args)
        gate.assert_not_called(); self.bundle.assert_not_called(); self.assertFalse(self.policy.exists())

    def test_wrong_selected_engine_is_rejected(self):
        self.choice['source'] = 'bundled'
        with patch.object(self.verify, 'verify_with_timeout') as gate, self.assertRaisesRegex(RuntimeError, 'wrong browser'):
            self.verify.main(self.args)
        gate.assert_not_called(); self.assertFalse(self.policy.exists())


if __name__ == '__main__': unittest.main()
