"""Offline failure injection for same-version browser repair and rollback."""
from pathlib import Path
import io
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import repair_native_browser as repair
from test_build_recovery_r94 import load_browser_fixture


def corrupt_file_error(code=1392):
    error = OSError('injected unreadable generated browser file')
    error.winerror = code
    return error


class RepairPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='Juxin 修复 ! (35) ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.browsers = self.root / 'build/browsers'
        self.destination = self.browsers / 'chromium-123'
        self.destination.mkdir(parents=True)
        (self.destination / 'old-runtime').write_bytes(b'original')
        registry = self.root / 'playwright/driver/package'
        registry.mkdir(parents=True)
        (registry / 'browsers.json').write_text(json.dumps({'browsers': [
            {'name': 'chromium', 'revision': '123', 'browserVersion': '1.2.3.4'}]}))
        self.install = load_browser_fixture(self.root, self.browsers)

    def download(self, *, verify=None, missing=(), empty=(), browser_root=None):
        content = io.BytesIO()
        with ZipFile(content, 'w') as archive:
            for name in ('chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
                if name not in missing:
                    archive.writestr('chrome-win64/' + name, b'' if name in empty else b'new-runtime')
        with patch.object(self.install.__globals__['urllib'].request, 'urlopen', return_value=io.BytesIO(content.getvalue())):
            self.install(verify=verify, browser_root=browser_root)

    def assert_original(self):
        self.assertEqual(b'original', (self.destination / 'old-runtime').read_bytes())
        self.assertFalse((self.destination / 'chrome-win64').exists())

    def test_missing_dll_cannot_replace_original(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete.*chrome.dll'):
            self.download(missing={'chrome.dll'})
        self.assert_original()

    def test_empty_resource_cannot_replace_original(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete.*resources.pak'):
            self.download(empty={'resources.pak'})
        self.assert_original()

    def test_disk_readback_rejects_corrupted_extracted_files_before_publication(self):
        original = ZipFile.extractall
        for name in ('chrome.exe', 'chrome.dll', 'resources.pak', 'locales/en-US.pak'):
            with self.subTest(resource=name):
                def extract(archive, path, *args, **kwargs):
                    original(archive, path, *args, **kwargs)
                    (Path(path) / 'chrome-win64' / name).write_bytes(b'bad-runtime')
                verify = Mock()
                with patch.object(ZipFile, 'extractall', extract), self.assertRaisesRegex(RuntimeError, 'disk readback integrity'):
                    self.download(verify=verify)
                verify.assert_not_called()
                self.assert_original()

    def test_unreadable_extracted_file_keeps_previous_runtime(self):
        original = Path.open
        def open_file(path, *args, **kwargs):
            if 'extracted' in path.parts and path.name == 'chrome.exe' and args == ('rb',):
                raise corrupt_file_error()
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file), self.assertRaises(OSError) as caught:
            self.download()
        self.assertEqual(1392, caught.exception.winerror)
        self.assert_original()

    def test_failed_actual_path_verification_restores_original(self):
        def verify(destination):
            self.assertEqual(self.destination, destination)
            self.assertEqual(b'new-runtime', (destination / 'chrome-win64/chrome.exe').read_bytes())
            self.assertEqual(1, len(list(self.browsers.parent.glob('juxin-browser-previous-*'))))
            raise RuntimeError('injected native exit 3')
        with self.assertRaisesRegex(RuntimeError, 'native exit 3'):
            self.download(verify=verify)
        self.assert_original()
        rejected = list(self.browsers.parent.glob('juxin-browser-rejected-*/chrome-win64/chrome.exe'))
        self.assertEqual(1, len(rejected))
        self.assertFalse(rejected[0].is_relative_to(self.browsers))

    def test_failure_without_previous_runtime_does_not_leave_bad_packaging_input(self):
        shutil.rmtree(self.destination)
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            self.download(verify=Mock(side_effect=RuntimeError('failed')))
        self.assertFalse(self.destination.exists())
        self.assertEqual([], list(self.browsers.iterdir()))

    def test_success_deletes_backup_only_after_verification(self):
        observed = []
        def verify(destination):
            observed.extend(self.browsers.parent.glob('juxin-browser-previous-*/old-runtime'))
            self.assertEqual(b'original', observed[0].read_bytes())
            self.assertTrue((destination / 'INSTALLATION_COMPLETE').is_file())
        self.download(verify=verify)
        self.assertEqual(1, len(observed))
        self.assertFalse(observed[0].exists())
        self.assertEqual(b'new-runtime', (self.destination / 'chrome-win64/chrome.exe').read_bytes())

    def test_cancel_during_verification_restores_original(self):
        with self.assertRaises(KeyboardInterrupt):
            self.download(verify=Mock(side_effect=KeyboardInterrupt()))
        self.assert_original()

    def test_failed_rollback_preserves_original_outside_packaging_input(self):
        original = Path.rename
        def rename(path, target):
            if path.name.startswith('juxin-browser-previous-'):
                raise PermissionError('rollback locked')
            return original(path, target)
        with patch.object(Path, 'rename', rename), self.assertRaisesRegex(RuntimeError, 'previous runtime preserved'):
            self.download(verify=Mock(side_effect=RuntimeError('failed')))
        self.assertFalse(self.destination.exists())
        backups = list(self.browsers.parent.glob('juxin-browser-previous-*/old-runtime'))
        self.assertEqual(1, len(backups))
        self.assertEqual(b'original', backups[0].read_bytes())

    def test_locked_rejected_runtime_cannot_remove_previous_backup(self):
        original = Path.rename
        def rename(path, target):
            if Path(target).name.startswith('juxin-browser-rejected-'):
                raise PermissionError('candidate is locked')
            return original(path, target)
        with patch.object(Path, 'rename', rename), self.assertRaisesRegex(RuntimeError, 'cannot remove rejected runtime'):
            self.download(verify=Mock(side_effect=RuntimeError('failed')))
        backups = list(self.browsers.parent.glob('juxin-browser-previous-*/old-runtime'))
        self.assertEqual(1, len(backups))
        self.assertEqual(b'original', backups[0].read_bytes())
        self.assertTrue((self.destination / 'chrome-win64/chrome.exe').exists())

    def test_download_failure_cannot_touch_original(self):
        with patch.object(self.install.__globals__['urllib'].request, 'urlopen', side_effect=OSError('network down')):
            with self.assertRaisesRegex(OSError, 'network down'):
                self.install()
        self.assert_original()

    def test_active_download_past_ten_minutes_is_not_cancelled(self):
        with patch.object(self.install.__globals__['time'], 'monotonic', side_effect=[0, 601]):
            self.download()
        self.assertEqual(b'new-runtime', (self.destination / 'chrome-win64/chrome.exe').read_bytes())

    def test_explicit_root_is_not_overridden_by_inherited_browser_directory(self):
        wrong = self.root / 'unrelated-cache'
        self.install.__globals__['os'].environ['PLAYWRIGHT_BROWSERS_PATH'] = str(wrong)
        self.download(browser_root=self.browsers)
        self.assertFalse(wrong.exists())
        self.assertTrue((self.destination / 'chrome-win64/chrome.exe').exists())


class RecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / '原项目'
        self.run = Path(self.tmp.name) / '报告'
        self.enterContext(patch.object(repair, 'prepare_runtime_permissions', return_value={'status': 'fixture'}))

    def test_healthy_browser_never_downloads(self):
        install = Mock()
        verify = Mock(return_value={'status': 'passed'})
        result = repair.recover(self.project, self.run, verifier=verify, installer=install)
        self.assertEqual('already_verified', result['status'])
        self.assertEqual(0, result['repair_attempts'])
        install.assert_not_called()
        verify.assert_called_once()

    def test_failed_browser_downloads_once_and_requires_same_path_recheck(self):
        verify = Mock(side_effect=[RuntimeError('exit 3'), {'status': 'passed'}])
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install)
        self.assertEqual('repaired', result['status'])
        self.assertEqual(1, result['repair_attempts'])
        install.assert_called_once()
        self.assertEqual(2, verify.call_count)
        for call in verify.call_args_list:
            self.assertEqual((self.project, self.project / 'build/browsers'), call.args[:2])

    def test_failed_replacement_does_not_retry_forever_or_report_success(self):
        verify = Mock(side_effect=RuntimeError('still exit 3'))
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install)
        self.assertEqual('failed', result['status'])
        self.assertEqual('still exit 3', result['error'])
        install.assert_called_once()
        self.assertEqual(2, verify.call_count)

    def test_download_failure_stops_after_one_attempt(self):
        install = Mock(side_effect=OSError('network down'))
        verify = Mock(side_effect=RuntimeError('exit 3'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install)
        self.assertEqual('failed', result['status'])
        self.assertEqual(1, result['repair_attempts'])
        self.assertEqual('network down', result['error'])
        install.assert_called_once()
        verify.assert_called_once()

    def test_installer_success_without_verification_is_rejected(self):
        result = repair.recover(self.project, self.run,
            verifier=Mock(side_effect=RuntimeError('exit 3')), installer=Mock())
        self.assertEqual('failed', result['status'])
        self.assertIn('was not verified', result['error'])

    def test_permissions_are_prepared_before_first_check_and_after_replacement(self):
        events = []
        def prepare(root):
            events.append('permissions')
            return {'status': 'prepared'}
        def verify(*args):
            events.append('verify')
            if len(events) == 2:raise RuntimeError('failed')
            return {'status': 'passed'}
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install, preparer=prepare)
        self.assertEqual(['permissions', 'verify', 'permissions', 'verify'], events)
        self.assertEqual('repaired', result['status'])

    def test_permission_failure_does_not_trigger_a_pointless_download(self):
        install = Mock();verify = Mock()
        result = repair.recover(self.project, self.run, verifier=verify, installer=install,
                                preparer=Mock(side_effect=PermissionError('access denied')))
        self.assertEqual('failed', result['status'])
        self.assertEqual(0, result['repair_attempts'])
        install.assert_not_called();verify.assert_not_called()

    def test_corrupt_file_during_permission_inspection_is_rebuilt_once_and_verified(self):
        cause = corrupt_file_error()
        wrapped = RuntimeError('permission inspection failed');wrapped.__cause__ = cause
        prepare = Mock(side_effect=[wrapped, {'status': 'prepared'}])
        verify = Mock(return_value={'status': 'passed'})
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install, preparer=prepare)
        self.assertEqual('repaired', result['status'])
        self.assertEqual((1, 0), (result['repair_attempts'], result['compatibility_attempts']))
        self.assertEqual(1392, result['permissions'][0]['winerror'])
        self.assertEqual(2, prepare.call_count);verify.assert_called_once();install.assert_called_once()

    def test_structured_launch_corruption_is_rebuilt_with_full_verification(self):
        error = startup_error('native_browser_launch_failed');error.details['winerror'] = 1392
        verify = Mock(side_effect=[error, {'status': 'passed'}])
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install)
        self.assertEqual('repaired', result['status']);install.assert_called_once()
        self.assertEqual(2, verify.call_count)

    def test_repeated_corruption_stops_with_storage_diagnosis_not_compatibility_retry(self):
        for code in (1392, 1393):
            with self.subTest(code=code):
                error = startup_error('native_browser_launch_failed');error.details['winerror'] = code
                verify = Mock(side_effect=error)
                install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
                result = repair.recover(self.project, self.run, verifier=verify, installer=install)
                self.assertEqual(('failed', 1, 0, code),
                    (result['status'], result['repair_attempts'], result['compatibility_attempts'], result['winerror']))
                self.assertEqual('filesystem_corruption', result['failure_kind'])
                self.assertIn('another healthy local drive', result['next_action'])
                install.assert_called_once()

    def test_corrupt_initial_runtime_then_fresh_startup_failure_can_recover_compatibility(self):
        for during_prepare in (False, True):
            with self.subTest(during_prepare=during_prepare):
                initial = corrupt_file_error()
                prepare = Mock(side_effect=[initial, {'status': 'prepared'}, {'status': 'prepared'}]) if during_prepare else Mock(return_value={'status': 'prepared'})
                verify = Mock(side_effect=([initial] if not during_prepare else []) + [startup_error(), {'status': 'passed'}])
                install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
                result = repair.recover(self.project, self.run, verifier=verify, installer=install, preparer=prepare)
                self.assertEqual(('compatibility_repaired', 1, 1),
                    (result['status'], result['repair_attempts'], result['compatibility_attempts']))
                self.assertEqual(2, install.call_count)
                self.assertTrue(install.call_args.kwargs['compatibility'])

    def test_new_runtime_permission_denial_still_fails_after_corruption_rebuild(self):
        prepare = Mock(side_effect=[corrupt_file_error(), PermissionError('replacement access denied')])
        verify = Mock()
        install = Mock(side_effect=lambda **kw: kw['verify'](kw['browser_root'] / 'chromium-123'))
        result = repair.recover(self.project, self.run, verifier=verify, installer=install, preparer=prepare)
        self.assertEqual(('failed', 1, 0), (result['status'], result['repair_attempts'], result['compatibility_attempts']))
        self.assertNotIn('failure_kind', result)
        verify.assert_not_called();install.assert_called_once()

    def test_download_failure_during_corruption_repair_keeps_its_actual_cause(self):
        install = Mock(side_effect=OSError('new download connection failure'))
        result = repair.recover(self.project, self.run, verifier=Mock(), installer=install,
                               preparer=Mock(side_effect=corrupt_file_error()))
        self.assertEqual('failed', result['status'])
        self.assertIn('new download connection failure', result['error'])
        self.assertNotIn('failure_kind', result)
        self.assertEqual(0, result['compatibility_attempts'])
        install.assert_called_once()

    def test_wrapped_access_denial_is_not_mistaken_for_file_corruption(self):
        cause = PermissionError('denied');cause.winerror = 5
        wrapped = RuntimeError('permission inspection failed');wrapped.__cause__ = cause
        install = Mock();verify = Mock()
        result = repair.recover(self.project, self.run, verifier=verify, installer=install,
                               preparer=Mock(side_effect=wrapped))
        self.assertEqual(('failed', 0), (result['status'], result['repair_attempts']))
        install.assert_not_called();verify.assert_not_called()

    def test_environment_forces_bundle_and_restores_overrides_on_failure(self):
        keys = {'IGAC_BROWSER_DIR': 'old-root', 'PLAYWRIGHT_BROWSERS_PATH': 'old-cache',
                'IGAC_NATIVE_BROWSER_EXECUTABLE': 'unrelated-chrome'}
        with patch.dict(os.environ, keys):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                with repair.runtime_environment(self.project):
                    self.assertEqual(str(self.project), os.environ['IGAC_BROWSER_DIR'])
                    self.assertEqual(str(self.project), os.environ['PLAYWRIGHT_BROWSERS_PATH'])
                    self.assertNotIn('IGAC_NATIVE_BROWSER_EXECUTABLE', os.environ)
                    raise RuntimeError('failed')
            for key, value in keys.items():
                self.assertEqual(value, os.environ[key])

    def test_empty_environment_is_restored_after_check(self):
        with patch.dict(os.environ, {}, clear=True):
            with repair.runtime_environment(self.project):
                self.assertIn('IGAC_BROWSER_DIR', os.environ)
            self.assertEqual({}, dict(os.environ))


class VerifierContractTests(unittest.TestCase):
    def test_failure_comparison_uses_fresh_profiles_and_does_not_promote_cdp_to_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);run=root/'reports';run.mkdir()
            cases=[]
            def probe(executable,profile,output,**options):
                self.assertFalse(profile.exists())
                profile.mkdir(parents=True)
                cases.append((profile,options))
                return {'status':'cdp_responded','args':[str(executable)],'pid':123}
            with patch('app.browser_runtime.bundled_executable',return_value=Path(sys.executable)), \
                 patch.object(repair,'runtime_fingerprint',return_value={'sha256':'fixture'}), \
                 patch.object(repair,'probe',side_effect=probe), \
                 patch.object(repair,'installed_comparison_browser',return_value=Path(sys.executable)), \
                 patch.object(repair,'windows_events',return_value={'status':'no_matching_events'}):
                result=repair.collect_failure_comparison(root,run)
            self.assertEqual('collected',result['status'])
            self.assertIn('does not satisfy',result['purpose'])
            self.assertEqual(['short-data-path','minimal-settings','installed-browser-comparison'],[row['name'] for row in result['cases']])
            self.assertEqual([False,True,False],[options['minimal'] for _,options in cases])
            self.assertTrue(all(options['timeout']==20 for _,options in cases))
            self.assertTrue(all(not path.exists() for path,_ in cases))

    def test_failure_comparison_keeps_original_failure_if_collecting_it_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder)
            failed={'status':'failed','error':'original exit 3',
                    'checks':[{'details':{'reason':'native_browser_exited','exit_code':3}}]}
            with patch.object(repair,'resolve_project',return_value=project), \
                 patch.object(repair,'runtime_fingerprint',return_value={}), \
                 patch.object(repair,'recover',return_value=failed), \
                 patch.object(repair,'collect_failure_comparison',side_effect=PermissionError('report locked')):
                self.assertEqual(1,repair.main(project))
            with ZipFile(next((project/'installer-output').glob('native-browser-repair-*.zip'))) as archive:
                result=json.loads(archive.read('summary.json'))
            self.assertEqual('failed',result['status'])
            self.assertEqual('original exit 3',result['error'])
            self.assertEqual('report locked',result['comparison_error'])

    def test_healthy_recovery_does_not_launch_diagnostic_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder)
            with patch.object(repair,'resolve_project',return_value=project), \
                 patch.object(repair,'runtime_fingerprint',return_value={}), \
                 patch.object(repair,'recover',return_value={'status':'already_verified'}), \
                 patch.object(repair,'collect_failure_comparison') as compare:
                self.assertEqual(0,repair.main(project))
            compare.assert_not_called()

    def test_comparison_cleanup_failure_stops_new_windows_and_preserves_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);run=root/'reports';run.mkdir()
            def probe(executable,profile,output,**options):
                profile.mkdir(parents=True)
                return {'status':'startup_timeout','cleanup_error':'owned child still running'}
            with patch('app.browser_runtime.bundled_executable',return_value=Path(sys.executable)), \
                 patch.object(repair,'runtime_fingerprint',return_value={}), \
                 patch.object(repair,'probe',side_effect=probe) as run_probe, \
                 patch.object(repair,'installed_comparison_browser',return_value=None):
                result=repair.collect_failure_comparison(root,run)
            run_probe.assert_called_once()
            retained=Path(result['retained_fixture']);self.addCleanup(shutil.rmtree,retained,ignore_errors=True)
            self.assertTrue((retained/'p').is_dir())
            self.assertIn('skipped',result['remaining_cases'])

    def test_repair_archive_contains_launch_evidence_but_not_profile_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);run=root/'reports';stage=run/'before-repair'/'run-fixture'/'launches'/'owner'/'profile'
            stage.mkdir(parents=True)
            expected={'juxin-startup.json','juxin-startup.stderr.log','juxin-startup.chrome.log'}
            for name in expected|{'Cookies','Login Data','Preferences','test.sqlite3'}:
                (stage/name).write_text('fixture')
            destination=root/'report.zip';repair.make_repair_bundle(run,destination)
            with ZipFile(destination) as archive:
                self.assertEqual(expected,{Path(name).name for name in archive.namelist()})
                self.assertIsNone(archive.testzip())

    def test_permission_adapter_cannot_preload_the_wrong_project_app(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);import browser_sandbox_permissions;assert 'app' not in sys.modules"
        result = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT / 'scripts')], capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))

    def verify_fixture(self, report):
        with tempfile.TemporaryDirectory(prefix='Juxin 验证 (35) ') as folder:
            root = Path(folder)
            output = root / 'reports'
            def main(args):
                self.assertIn('--no-headless', args)
                self.assertIn('--require-bundled', args)
                self.assertNotIn('IGAC_NATIVE_BROWSER_EXECUTABLE', os.environ)
                run = output / 'run-fixture'
                run.mkdir(parents=True)
                (run / 'result.json').write_text(json.dumps(report))
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
            with patch.object(repair.importlib.util, 'spec_from_file_location', return_value=spec), \
                    patch.object(repair.importlib.util, 'module_from_spec', return_value=SimpleNamespace(main=main)):
                return repair.verify_runtime(root, root / 'build/browsers', output)

    def test_two_actual_pinned_versions_are_required(self):
        report = {'status': 'passed', 'require_bundled': True, 'bundled_version': '1.2.3.4',
                  'actual_browser_versions': ['1.2.3.4', '1.2.3.4'], 'network_verified': True}
        self.assertEqual(report, self.verify_fixture(report))
        for versions in (None, [], ['1.2.3.4'], ['1.2.3.4', '9.9.9.9']):
            with self.subTest(versions=versions), self.assertRaisesRegex(RuntimeError, 'pinned version'):
                self.verify_fixture({**report, 'actual_browser_versions': versions})

    def test_cdp_success_without_network_check_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'network requests were not verified'):
            self.verify_fixture({'status': 'passed', 'require_bundled': True, 'bundled_version': '1.2.3.4',
                                 'actual_browser_versions': ['1.2.3.4', '1.2.3.4']})

    def test_system_chrome_success_does_not_replace_bundled_check(self):
        with self.assertRaisesRegex(RuntimeError, 'exact packaged browser'):
            self.verify_fixture({'status': 'passed', 'require_bundled': False})

    def test_failed_report_cannot_be_accepted_even_if_verifier_returns(self):
        with self.assertRaisesRegex(RuntimeError, 'did not pass'):
            self.verify_fixture({'status': 'failed', 'require_bundled': True})

    def test_release_repairs_bundle_before_automatic_selection(self):
        source = (ROOT / 'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(source.index('scripts\\repair_native_browser.py'), source.index('$NativeBrowserExitCode ='))
        self.assertLess(source.index('$BundledBrowserExitCode -ne 0'), source.index('$PyInstallerExitCode ='))
        self.assertIn('test_native_repair_r94.py', source)
        launcher = (ROOT / 'REPAIR_NATIVE_BROWSER.bat').read_text(encoding='utf-8')
        self.assertIn('Enter-IgacBuildMutex', launcher)
        self.assertIn('finally { Exit-IgacBuildMutex $mutex }', launcher)
        self.assertIn('--project-dir-env', launcher)



# F8 recovery scenarios. The campaign calls these with distinct versions,
# failure types and Unicode paths; they exercise production functions.
sys.path.insert(0, str(ROOT / 'backend'))
from app.browser_bundle import (BUNDLE_DESCRIPTOR, STABLE_METADATA_URL, bundle_descriptor,
                                official_archive_url, stable_candidate)
from app.errors import UpstreamUnavailableError


def stable_payload(version):
    return {'channels': {'Stable': {'channel': 'Stable', 'version': version,
            'downloads': {'chrome': [{'platform': 'win64', 'url': official_archive_url(version)}]}}}}


def compatibility_record(version='2.0.0.0'):
    return {'format': 1, 'source': 'chrome-for-testing-stable', 'driver_revision': '123',
            'driver_version': '1.2.3.4', 'version': version,
            'download_url': official_archive_url(version), 'archive_sha256': 'a' * 64}


def startup_error(reason='native_browser_exited', code=3):
    return UpstreamUnavailableError('controlled startup failure', details={'reason': reason, 'exit_code': code})


class CompatibilityRecoveryTests(unittest.TestCase):
    families = ('stable_version', 'stable_assets', 'bundle_valid', 'bundle_invalid',
                'publish_success', 'publish_failure', 'same_version_repair',
                'recovery_transitions', 'profile_version_guard', 'owned_process_failure')

    def scenario(self, family, index):
        with tempfile.TemporaryDirectory(prefix=f'聚鑫 F8 [{index}] ') as folder:
            root = Path(folder)
            version = f'{2 + index}.0.123.{index}'
            if family == 'stable_version':
                self.assertEqual((version, official_archive_url(version)), stable_candidate(stable_payload(version), '1.2.3.4'))
                for minimum in (version, f'{3 + index}.0.0.0'):
                    with self.assertRaises(ValueError):stable_candidate(stable_payload(version), minimum)
            elif family == 'stable_assets':
                payload = stable_payload(version)
                stable = payload['channels']['Stable'];assets = stable['downloads']['chrome']
                bad = index % 8
                if bad == 0:assets[0]['url'] = f'https://example.invalid/{index}/chrome.zip'
                elif bad == 1:assets.append(dict(assets[0]))
                elif bad == 2:assets[0]['platform'] = 'win32'
                elif bad == 3:stable['channel'] = 'Beta'
                elif bad == 4:stable['version'] = f'../../{index}'
                elif bad == 5:assets[0]['url'] += '?redirect=elsewhere'
                elif bad == 6:stable['downloads']['chrome'] = [None]
                else:del payload['channels']['Stable']
                with self.assertRaises(ValueError):stable_candidate(payload, '1.2.3.4')
            elif family in ('bundle_valid', 'bundle_invalid'):
                record = compatibility_record(version);descriptor = root / BUNDLE_DESCRIPTOR
                descriptor.write_text(json.dumps(record))
                if family == 'bundle_valid':
                    self.assertEqual(record, bundle_descriptor(root, '123', '1.2.3.4'))
                    descriptor.unlink()
                    self.assertEqual('1.2.3.4', bundle_descriptor(root, '123', '1.2.3.4')['version'])
                else:
                    changes = [('format', True), ('driver_revision', '999'), ('driver_version', '9.9.9.9'),
                               ('version', '0.0.0.1'), ('archive_sha256', 'z' * 64),
                               ('download_url', 'https://example.invalid/chrome.zip'), ('source', 'arbitrary')]
                    kind = index % 10
                    if kind < 7:
                        record[changes[kind][0]] = changes[kind][1];descriptor.write_text(json.dumps(record))
                    elif kind == 7:descriptor.write_text('{')
                    elif kind == 8:descriptor.write_text(' ' * 8193)
                    else:
                        # Ordinary Windows accounts cannot create symlinks.
                        # Exercise rejection without requiring developer mode.
                        with patch.object(Path, 'is_symlink', return_value=True):
                            with self.assertRaises(ValueError):bundle_descriptor(root, '123', '1.2.3.4')
                        return
                    with self.assertRaises((ValueError, OSError)):bundle_descriptor(root, '123', '1.2.3.4')
            elif family in ('publish_success', 'publish_failure', 'same_version_repair'):
                self.publication(root, version, family, index)
            elif family == 'recovery_transitions':
                kind = index % 8
                initial = startup_error(code=index)
                def installer(**kwargs):kwargs['verify'](kwargs['browser_root'] / 'chromium-123')
                install = Mock(side_effect=installer)
                expected, attempts, compat = 'compatibility_repaired', 2, 1
                if kind == 0:outcomes = [initial, startup_error('native_browser_timeout'), {'status': 'passed'}]
                elif kind == 1:outcomes = [initial, startup_error(), startup_error()];expected = 'failed'
                elif kind == 2:outcomes = [initial, {'status': 'passed'}];expected, attempts, compat = 'repaired', 1, 0
                elif kind == 3:outcomes = [{'status': 'passed'}];expected, attempts, compat = 'already_verified', 0, 0
                elif kind == 4:outcomes = [initial, RuntimeError('network verification failed')];expected, attempts, compat = 'failed', 1, 0
                elif kind == 5:outcomes = [RuntimeError('untyped initial failure'), startup_error()];expected, attempts, compat = 'failed', 1, 0
                elif kind == 6:
                    outcomes = [initial];install = Mock(side_effect=OSError('download failed'));expected, attempts, compat = 'failed', 1, 0
                else:
                    outcomes = [initial];install = Mock();expected, attempts, compat = 'failed', 1, 0
                verify = Mock(side_effect=outcomes)
                result = repair.recover(root, root / 'reports', verifier=verify, installer=install, preparer=Mock(return_value={}))
                self.assertEqual(expected, result['status']);self.assertEqual(attempts, install.call_count)
                self.assertEqual(compat, result['compatibility_attempts'])
                if compat:self.assertTrue(install.call_args.kwargs['compatibility'])
            elif family == 'profile_version_guard':
                from app.browser_runtime import select_browser_runtime
                base = root / 'chromium-123';base.mkdir()
                (base / BUNDLE_DESCRIPTOR).write_text(json.dumps(compatibility_record(version)))
                executable = base / 'chrome.exe';executable.write_bytes(b'fixture')
                profile = root / 'profile';profile.mkdir();(profile / 'Cookies.fixture').write_bytes(b'preserve')
                kind = index % 3
                actual = version if kind != 1 else f'{9 + index}.0.0.0'
                if kind == 2:(profile / 'Last Version').write_text(f'{9 + index}.0.0.0')
                with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(root), 'IGAC_NATIVE_BROWSER_EXECUTABLE': ''}), \
                     patch('app.browser_runtime.pinned_chromium', return_value=('123', '1.2.3.4')), \
                     patch('app.browser_runtime.installed_chrome', return_value=None), \
                     patch('app.browser_runtime._bundled_or_development_executable', return_value=executable), \
                     patch('app.browser_runtime.windows_file_version', return_value=actual):
                    if kind == 0:self.assertEqual(version, select_browser_runtime(profile, windows=True)['version'])
                    else:
                        with self.assertRaises(UpstreamUnavailableError) as caught:select_browser_runtime(profile, windows=True)
                        self.assertEqual('native_runtime_bundle_version_mismatch' if kind == 1 else 'native_runtime_downgrade', caught.exception.details['reason'])
                        self.assertFalse((profile / 'juxin-runtime.json').exists())
                self.assertEqual(b'preserve', (profile / 'Cookies.fixture').read_bytes())
            elif family == 'owned_process_failure':
                import asyncio
                import verify_native_browser as verifier
                original = subprocess.Popen;children = []
                script = f"import os,sys;os.write(2,('fixture {index} '+chr(27979)).encode());sys.exit(3)"
                def spawn(args, **kwargs):
                    child = original([sys.executable, '-c', script, *args[1:]], **kwargs);children.append(child);return child
                with patch('app.native_browser.browser_executable', return_value=Path(sys.executable)), \
                     patch('app.native_browser._spawn_browser_process', side_effect=spawn):
                    with self.assertRaises(UpstreamUnavailableError) as caught:
                        asyncio.run(verifier.verify_with_timeout(root, 'http://127.0.0.1:1/', headless=False))
                self.assertEqual('native_browser_exited', caught.exception.details['reason'])
                self.assertEqual(3, caught.exception.details['exit_code'])
                self.assertEqual(1, len(children));self.assertEqual(3, children[0].poll())
            else:raise AssertionError(family)

    def publication(self, root, version, family, index):
        browsers = root / 'build/browsers';destination = browsers / 'chromium-123';destination.mkdir(parents=True)
        original = f'original-{index}'.encode();(destination / 'old-runtime').write_bytes(original)
        registry = root / 'playwright/driver/package';registry.mkdir(parents=True)
        (registry / 'browsers.json').write_text(json.dumps({'browsers': [
            {'name': 'chromium', 'revision': '123', 'browserVersion': '1.2.3.4'}]}))
        install = load_browser_fixture(root, browsers)
        if family == 'same_version_repair':(destination / BUNDLE_DESCRIPTOR).write_text(json.dumps(compatibility_record(version)))
        data = io.BytesIO()
        with ZipFile(data, 'w') as z:
            for name in ('chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
                z.writestr('chrome-win64/' + name, f'candidate-{index}'.encode())
        archive = data.getvalue();calls = []
        def download(url, **kwargs):
            calls.append(url)
            if url == STABLE_METADATA_URL:return io.BytesIO(json.dumps(stable_payload(version)).encode())
            self.assertEqual(official_archive_url(version), url)
            return io.BytesIO(archive)
        def verify(path):
            import hashlib
            self.assertEqual(destination, path)
            record = bundle_descriptor(path, '123', '1.2.3.4')
            self.assertEqual(version, record['version'])
            self.assertEqual(hashlib.sha256(archive).hexdigest(), record['archive_sha256'])
            self.assertTrue((path / 'INSTALLATION_COMPLETE').is_file())
            self.assertTrue(list(browsers.parent.glob('juxin-browser-previous-*/old-runtime')))
            if family == 'publish_failure':
                if index % 2:raise KeyboardInterrupt()
                raise startup_error()
        with patch.object(install.__globals__['urllib'].request, 'urlopen', side_effect=download):
            if family == 'publish_failure':
                with self.assertRaises(KeyboardInterrupt if index % 2 else UpstreamUnavailableError):
                    install(browser_root=browsers, verify=verify, compatibility=True)
                self.assertEqual(original, (destination / 'old-runtime').read_bytes())
                self.assertFalse((destination / 'chrome-win64').exists())
            else:
                install(browser_root=browsers, verify=verify, compatibility=family != 'same_version_repair')
                self.assertFalse((destination / 'old-runtime').exists())
                self.assertEqual(version, bundle_descriptor(destination, '123', '1.2.3.4')['version'])
                self.assertFalse(list(browsers.parent.glob('juxin-browser-previous-*')))
        self.assertEqual(1 if family == 'same_version_repair' else 2, len(calls))

    def test_all_compatibility_branches(self):
        for family in self.families:
            for i in range(10):
                with self.subTest(family=family, index=i):self.scenario(family, i)

    def test_compatibility_without_verifier_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);registry = root / 'playwright/driver/package';registry.mkdir(parents=True)
            (registry / 'browsers.json').write_text(json.dumps({'browsers': [
                {'name': 'chromium', 'revision': '123', 'browserVersion': '1.2.3.4'}]}))
            install = load_browser_fixture(root, root / 'build/browsers')
            with patch.object(install.__globals__['urllib'].request, 'urlopen') as download:
                with self.assertRaisesRegex(RuntimeError, 'requires full final-path verification'):install(compatibility=True)
                download.assert_not_called()

    def test_compatibility_installer_cannot_skip_verifier(self):
        def install(**kwargs):
            if not kwargs.get('compatibility'):kwargs['verify'](kwargs['browser_root'] / 'chromium-123')
        result = repair.recover(Path('unused'), Path('unused'), verifier=Mock(side_effect=startup_error()),
                                installer=install, preparer=Mock(return_value={}))
        self.assertEqual('failed', result['status']);self.assertEqual(1, result['compatibility_attempts'])
        self.assertIn('was not verified', result['error'])


class BuildResilienceTests(unittest.TestCase):
    families = ('metadata_retry', 'metadata_fail_closed', 'extraction_cleanup', 'verification_cleanup')

    def scenario(self, family, index):
        fixture = RepairPublicationTests()
        fixture.setUp()
        try:
            fetch = fixture.install.__globals__['fetch_stable_candidate']
            version = f'{index + 2}.0.123.{index}'
            payload = json.dumps(stable_payload(version)).encode()
            if family == 'metadata_retry':
                failures = 1 + index % 2
                codes = (408, 429, 500, 502, 503, 504)
                kind = index % 4
                if kind == 0:
                    errors = [urllib.error.HTTPError(STABLE_METADATA_URL, codes[(index + n) % 6], 'temporary', {}, None) for n in range(failures)]
                elif kind == 1:
                    errors = [urllib.error.URLError(f'connection reset {index}-{n}') for n in range(failures)]
                elif kind == 2:
                    errors = [http.client.IncompleteRead(payload[:1 + index % (len(payload) - 1)], 17) for n in range(failures)]
                else:
                    cut = 1 + index % (len(payload) - 1)
                    errors = [io.BytesIO(payload[:cut]) for n in range(failures)]
                    for response in errors:response.headers = {'Content-Length': str(len(payload))}
                opener = Mock(side_effect=[*errors, io.BytesIO(payload)]);sleep = Mock()
                self.assertEqual((version, official_archive_url(version)), fetch('1.2.3.4', opener=opener, sleep=sleep))
                self.assertEqual(failures + 1, opener.call_count)
                self.assertEqual(list(range(1, failures + 1)), [c.args[0] for c in sleep.call_args_list])
                for call in opener.call_args_list:self.assertEqual(((STABLE_METADATA_URL,), {'timeout': 30}), call)
            elif family == 'metadata_fail_closed':
                kind = index % 8
                attempts = 1
                if kind == 0:
                    responses = [urllib.error.HTTPError(STABLE_METADATA_URL, (400, 401, 403, 404)[index // 8 % 4], 'permanent', {}, None)]
                elif kind == 1:
                    responses = [urllib.error.URLError('offline')] * 3;attempts = 3
                elif kind == 2:
                    responses = [io.BytesIO(b'{"invalid":' + str(index).encode())]
                elif kind == 3:
                    invalid = stable_payload(version)
                    invalid['channels']['Stable']['downloads']['chrome'][0]['url'] = f'https://example.invalid/{index}/chrome.zip'
                    responses = [io.BytesIO(json.dumps(invalid).encode())]
                elif kind == 4:
                    responses = [io.BytesIO(json.dumps(stable_payload('1.2.3.4')).encode())]
                elif kind == 5:
                    responses = [io.BytesIO(b'x' * (1024 * 1024 + 1))]
                elif kind == 6:
                    responses = [PermissionError(f'local permission failure {index}')]
                else:
                    response = io.BytesIO(payload);response.headers = {'Content-Length': str(1024 * 1024 + index + 1)}
                    responses = [response]
                opener = Mock(side_effect=responses);sleep = Mock()
                with self.assertRaises((OSError, ValueError, RuntimeError)):
                    fetch('1.2.3.4', opener=opener, sleep=sleep)
                self.assertEqual(attempts, opener.call_count)
                self.assertEqual(attempts - 1, sleep.call_count)
                fixture.assert_original()
            elif family == 'extraction_cleanup':
                original = shutil.rmtree
                locked_paths = []
                def cleanup(path, *args, **kwargs):
                    if Path(path).name.startswith('juxin-browser-extract-'):
                        locked_paths.append(Path(path))
                        raise (PermissionError if index % 2 else OSError)(f'cleanup locked {index}')
                    return original(path, *args, **kwargs)
                error = None if index % 3 == 0 else (RuntimeError(f'original verification {index}') if index % 3 == 1 else KeyboardInterrupt(f'cancel {index}'))
                verify = Mock(side_effect=error)
                with patch.object(shutil, 'rmtree', cleanup):
                    if error is None:
                        fixture.download(verify=verify)
                        self.assertTrue((fixture.destination / 'chrome-win64/chrome.exe').is_file())
                        self.assertFalse((fixture.destination / 'old-runtime').exists())
                    else:
                        with self.assertRaises(type(error)) as caught:fixture.download(verify=verify)
                        self.assertIs(error, caught.exception)
                        fixture.assert_original()
                verify.assert_called_once()
                self.assertEqual(1, len(locked_paths))
                self.assertFalse(locked_paths[0].is_relative_to(fixture.browsers))
                self.assertTrue(locked_paths[0].exists())
            elif family == 'verification_cleanup':
                import verify_native_browser as verifier
                from contextlib import ExitStack
                original_temp = tempfile.mkdtemp;original_cleanup = shutil.rmtree
                owned = [];cleanup_calls = []
                def temporary(*args, **kwargs):
                    if kwargs.get('prefix') == 'juxin-native-':kwargs['dir'] = fixture.root
                    result = original_temp(*args, **kwargs)
                    # Windows tempfile paths may use RUNNER~1 while the
                    # verifier resolves the same directory to its long name.
                    if kwargs.get('prefix') == 'juxin-native-':owned.append(Path(result).resolve())
                    return result
                def cleanup(path, *args, **kwargs):
                    if Path(path).resolve() in owned:
                        cleanup_calls.append(Path(path).resolve());raise PermissionError(f'fixture cleanup locked {index}')
                    return original_cleanup(path, *args, **kwargs)
                failed = index % 2
                failure = RuntimeError(f'original native verification {index}')
                check = AsyncMock(side_effect=failure if failed else None,
                                  return_value={'actual_browser_versions': [version, version]})
                server = Mock(server_port=12345)
                diagnostics = fixture.root / f'diagnostics-{index}'
                with ExitStack() as stack:
                    stack.enter_context(patch.object(verifier, 'pinned_chromium', return_value=('123', version)))
                    stack.enter_context(patch.object(verifier, 'select_browser_runtime', return_value={'version': version}))
                    stack.enter_context(patch.object(verifier, 'verify_with_timeout', check))
                    stack.enter_context(patch.object(verifier, 'ThreadingHTTPServer', return_value=server))
                    stack.enter_context(patch.object(verifier.threading, 'Thread'))
                    stack.enter_context(patch.dict(os.environ, {}, clear=True))
                    stack.enter_context(patch.object(tempfile, 'mkdtemp', temporary))
                    stack.enter_context(patch.object(shutil, 'rmtree', cleanup))
                    if failed:
                        with self.assertRaises(RuntimeError) as caught:verifier.main(['--diagnostics-dir', str(diagnostics)])
                        self.assertIs(failure, caught.exception)
                    else:
                        verifier.main(['--diagnostics-dir', str(diagnostics)])
                report = json.loads(next(diagnostics.glob('run-*/result.json')).read_text())
                self.assertEqual('failed' if failed else 'passed', report['status'])
                self.assertEqual(0 if failed else 1, len(cleanup_calls))
                if failed:self.assertEqual(str(failure), report['error'])
                else:self.assertIn(str(index), report['cleanup_warning'])
                self.assertTrue(owned[0].is_dir())
                server.shutdown.assert_called_once();server.server_close.assert_called_once()
            else:
                raise AssertionError(family)
        finally:
            fixture.doCleanups()

    def test_transient_metadata_errors_retry_with_bounds(self):
        for index in range(24):
            with self.subTest(index=index):self.scenario('metadata_retry', index)

    def test_invalid_metadata_and_permanent_errors_fail_closed(self):
        for index in range(32):
            with self.subTest(index=index):self.scenario('metadata_fail_closed', index)

    def test_extraction_cleanup_preserves_success_failure_and_cancel(self):
        for index in range(6):
            with self.subTest(index=index):self.scenario('extraction_cleanup', index)

    def test_native_fixture_cleanup_preserves_verification_results(self):
        for index in range(4):
            with self.subTest(index=index):self.scenario('verification_cleanup', index)


if __name__ == '__main__':
    unittest.main()
