"""Permission scope and startup classification; no real Windows ACL mutation."""
from pathlib import Path
import asyncio
import io
import importlib.util
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import browser_permissions as permissions
from app import browser_runtime as runtime
from app.errors import UpstreamUnavailableError
from test_native_diagnostic import diag


class BrowserPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='浏览器 ! & (36) ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'build/browsers/chromium-1234/chrome-win64'
        self.folder.mkdir(parents=True)
        for name in ('chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
            path = self.folder / name;path.parent.mkdir(exist_ok=True);path.write_bytes(b'fixture')
        self.exe = self.folder / 'chrome.exe'
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, b'ok', b''))
        self.enterContext(patch.dict(permissions._prepared, {}, clear=True))

    def prepare(self, **kwargs):
        return permissions.ensure_browser_sandbox_access(self.exe, windows=True, runner=self.runner, **kwargs)

    def test_official_setup_receives_only_selected_runtime_without_shell(self):
        (self.folder / 'setup.exe').write_bytes(b'fixture')
        result = self.prepare()
        self.assertEqual('chrome-setup', result['method'])
        args = self.runner.call_args.args[0]
        # Windows may expose TEMP through an 8.3 alias; production resolves it
        # before constructing both arguments. Check the same canonical files.
        self.assertEqual([str((self.folder / 'setup.exe').resolve()), '--configure-browser-in-directory=' + str(self.folder.resolve())], args)
        self.assertFalse(self.runner.call_args.kwargs['shell'])
        self.assertEqual(45, self.runner.call_args.kwargs['timeout'])

    def test_fallback_only_adds_read_execute_to_runtime_not_parent_or_data(self):
        self.prepare()
        args = self.runner.call_args.args[0]
        self.assertEqual(str(self.folder.resolve()), args[1])
        self.assertEqual(['/grant', '*S-1-15-2-1:(OI)(CI)(RX)', '/T', '/C', '/L'], args[2:])
        self.assertFalse(self.runner.call_args.kwargs['shell'])
        self.assertNotIn('/reset', args)

    def test_failed_setup_uses_documented_fallback(self):
        (self.folder / 'setup.exe').write_bytes(b'fixture')
        self.runner.side_effect = [subprocess.CompletedProcess([], 2, b'', b'setup error'),
                                   subprocess.CompletedProcess([], 0, b'ok', b'')]
        result = self.prepare()
        self.assertEqual('icacls-read-execute', result['method'])
        self.assertEqual(2, len(result['attempts']))

    def test_timeout_is_bounded_and_falls_back_once(self):
        (self.folder / 'setup.exe').write_bytes(b'fixture')
        self.runner.side_effect = [subprocess.TimeoutExpired('setup', 45), subprocess.CompletedProcess([], 0, b'ok', b'')]
        self.assertEqual('prepared', self.prepare()['status'])
        self.assertEqual(2, self.runner.call_count)

    def test_failure_is_not_cached_or_reported_as_prepared(self):
        self.runner.return_value = subprocess.CompletedProcess([], 5, b'', b'access denied')
        for _ in range(2):
            with self.assertRaisesRegex(permissions.BrowserPermissionError, 'exit 5'):
                self.prepare()
        self.assertEqual(2, self.runner.call_count)
        self.assertEqual({}, permissions._prepared)

    def test_success_is_reused_and_replaced_files_are_prepared_again(self):
        self.prepare()
        with patch.object(permissions.os, 'walk', side_effect=AssertionError('cached launches must not scan every file')):
            self.assertEqual('prepared_in_this_process', self.prepare()['status'])
        self.runner.assert_called_once()
        (self.folder / 'chrome.dll').write_bytes(b'a different browser DLL')
        self.prepare()
        self.assertEqual(2, self.runner.call_count)

    def test_unrelated_executable_is_rejected_without_mutation(self):
        with self.assertRaisesRegex(permissions.BrowserPermissionError, 'only accepts'):
            permissions.ensure_browser_sandbox_access(self.root / 'Google/Chrome/Application/chrome.exe',
                                                     windows=True, runner=self.runner)
        self.runner.assert_not_called()

    def test_nested_link_cannot_expand_permission_scope(self):
        external = self.root / 'business-data';external.mkdir()
        try:(self.folder / 'external').symlink_to(external, target_is_directory=True)
        except OSError:self.skipTest('OS does not permit symlinks')
        with self.assertRaisesRegex(permissions.BrowserPermissionError, 'links or reparse'):
            self.prepare()
        self.runner.assert_not_called()

    def test_reparse_point_in_runtime_is_rejected(self):
        original = Path.lstat
        def info(path):
            result = original(path)
            if path.name == 'resources.pak':
                return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
            return result
        with patch.object(Path, 'lstat', info), self.assertRaisesRegex(permissions.BrowserPermissionError, 'reparse'):
            self.prepare()
        self.runner.assert_not_called()

    def test_hardlink_cannot_grant_permissions_to_external_data(self):
        original = self.root / 'private-file';original.write_bytes(b'private')
        os.link(original, self.folder / 'linked-file')
        with self.assertRaisesRegex(permissions.BrowserPermissionError, 'hard-linked'):
            self.prepare()
        self.runner.assert_not_called()

    def test_nonwindows_never_runs_windows_commands(self):
        self.assertEqual({'status': 'not_windows'}, permissions.ensure_browser_sandbox_access(
            self.exe, windows=False, runner=self.runner))
        self.runner.assert_not_called()

    def test_unreadable_tree_stops_before_permission_mutation(self):
        with patch.object(permissions.os, 'walk', side_effect=PermissionError('directory unreadable')):
            with self.assertRaisesRegex(permissions.BrowserPermissionError, 'directory unreadable'):
                self.prepare()
        self.runner.assert_not_called()

    def test_production_bundled_selection_prepares_access(self):
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.folder.parent.parent)}), \
                patch.object(runtime, 'bundled_executable', return_value=self.exe), \
                patch.object(runtime, 'ensure_browser_sandbox_access') as prepare:
            self.assertEqual(self.exe, runtime._bundled_or_development_executable())
        prepare.assert_called_once_with(self.exe)

    def test_production_permission_failure_is_reported_before_launch(self):
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.folder.parent.parent)}), \
                patch.object(runtime, 'bundled_executable', return_value=self.exe), \
                patch.object(runtime, 'ensure_browser_sandbox_access', side_effect=permissions.BrowserPermissionError('denied')):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                runtime._bundled_or_development_executable()
        self.assertEqual('native_runtime_permissions', caught.exception.details['reason'])


class SandboxDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_responding_cdp_port_does_not_hide_sandbox_denial(self):
        (self.root / 'chrome.log').write_text('ERROR: Sandbox cannot access executable D:\\browser\\chrome.exe. Access is denied. (0x5)')
        result = {'status': 'cdp_responded'}
        diag.classify_startup_logs(self.root, result)
        self.assertEqual('sandbox_access_denied', result['status'])
        self.assertEqual('cdp_responded', result['startup_status'])

    def test_cache_warning_or_crash_histogram_is_not_classified_as_access_denial(self):
        (self.root / 'chrome.log').write_text('Failed to open persistent cache files (0x3)\nHistogram: Startup.CrashBubbleShown')
        result = {'status': 'cdp_responded'}
        diag.classify_startup_logs(self.root, result)
        self.assertEqual({'status': 'cdp_responded'}, result)

    def test_unreadable_chrome_log_does_not_hide_console_denial(self):
        (self.root / 'chrome.log').touch()
        (self.root / 'console.log').write_text('Sandbox cannot access executable')
        original = Path.read_text
        def read(path, **kwargs):
            if path.name == 'chrome.log':raise PermissionError('locked')
            return original(path, **kwargs)
        result = {'status': 'cdp_responded'}
        with patch.object(Path, 'read_text', read):diag.classify_startup_logs(self.root, result)
        self.assertEqual('sandbox_access_denied', result['status'])


class BrowserNetworkVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/verify_native_browser.py'
        spec = importlib.util.spec_from_file_location('network_fixture_gate', path)
        self.gate = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.gate)
        self.page = Mock()
        self.page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.page.locator.return_value.inner_text = AsyncMock(return_value='Local browser isolation fixture')
        self.page.evaluate = AsyncMock(return_value={'status': 200, 'text': '<body>Local browser isolation fixture</body>'})

    async def test_real_page_and_network_response_are_required(self):
        await self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/')
        self.page.goto.assert_awaited_once()
        self.page.evaluate.assert_awaited_once()

    async def test_http_error_cannot_pass_as_a_browser_window(self):
        self.page.goto.return_value = SimpleNamespace(status=500)
        with self.assertRaisesRegex(AssertionError, 'did not load'):
            await self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/')
        self.page.evaluate.assert_not_awaited()

    async def test_browser_error_document_cannot_pass(self):
        self.page.locator.return_value.inner_text.return_value = 'This site cannot be reached'
        with self.assertRaisesRegex(AssertionError, 'error page'):
            await self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/')

    async def test_cdp_works_but_failed_network_service_is_rejected(self):
        self.page.evaluate.side_effect = RuntimeError('net::ERR_FAILED')
        with self.assertRaisesRegex(RuntimeError, 'ERR_FAILED'):
            await self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/')

    async def test_unanswered_network_request_stops_without_external_cancellation(self):
        async def never_returns(*args):
            await asyncio.Event().wait()
        self.page.evaluate.side_effect = never_returns
        with patch.object(self.gate, 'NETWORK_CHECK_TIMEOUT_SECONDS', 0.02, create=True):
            task = asyncio.create_task(self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/'))
            try:
                done, _ = await asyncio.wait([task], timeout=0.3)
                self.assertIn(task, done, 'Unanswered fetch leaves the repair/build verifier waiting forever')
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_navigation_and_dom_reads_share_the_network_deadline(self):
        async def never_returns(*args):
            await asyncio.Event().wait()
        for operation in (self.page.goto, self.page.locator.return_value.inner_text):
            with self.subTest(operation=operation), patch.object(self.gate, 'NETWORK_CHECK_TIMEOUT_SECONDS', 0.02):
                operation.side_effect = never_returns
                try:
                    with self.assertRaisesRegex(RuntimeError, 'page/network check timed out'):
                        await asyncio.wait_for(self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/'), 0.5)
                finally:
                    operation.side_effect = None

    async def test_operator_cancellation_is_preserved(self):
        self.page.evaluate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.gate.verify_network_page(self.page, 'http://127.0.0.1:12345/')

    async def test_overall_deadline_finishes_cleanup_before_retry(self):
        events = []
        async def stalled_verification(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                events.append('cleanup finished')
        with patch.object(self.gate, 'VERIFICATION_TIMEOUT_SECONDS', 0.02), \
                patch.object(self.gate, 'verify', side_effect=stalled_verification):
            with self.assertRaisesRegex(RuntimeError, 'verification timed out'):
                await self.gate.verify_with_timeout(Path('.'), 'http://127.0.0.1:12345/')
        self.assertEqual(['cleanup finished'], events)
        with patch.object(self.gate, 'verify', new=AsyncMock(return_value={'network_verified': True})):
            self.assertEqual({'network_verified': True}, await self.gate.verify_with_timeout(Path('.'), 'http://127.0.0.1:12345/'))


if __name__ == '__main__':unittest.main()
