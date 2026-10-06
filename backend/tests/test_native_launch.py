"""Exercise native launch failures, Unicode argv and exact runtime selection.

The child fixture is Python, not Chromium. Real browser isolation and cookie
persistence are separately required by scripts/verify_native_browser.py.
"""
from __future__ import annotations

import importlib.util
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.browser_runtime import (BrowserLaunchLog, browser_executable, bundled_executable,
                                select_browser_runtime, windows_file_version)
from app.errors import UpstreamUnavailableError
from app.native_browser import NativeBrowser


class NativeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        os.environ.pop('IGAC_BROWSER_DIR', None)
        os.environ.pop('IGAC_NATIVE_BROWSER_EXECUTABLE', None)
        self.tmp = tempfile.TemporaryDirectory()
        # Windows TEMP may use an 8.3 alias (RUNNER~1); production resolves
        # executables to long paths. Use the same canonical fixture root.
        self.root = Path(self.tmp.name).resolve() / '新建文件夹 (2)' / '戴'
        self.root.mkdir(parents=True)
        self.native = NativeBrowser(None, self.root, executable=sys.executable)
        self.ident = 'native:' + str(uuid.uuid4())
        row = {'owner_user_id': str(uuid.uuid4()), 'proxy_server': ''}
        self.native.get = lambda ident: row

    def tearDown(self):
        self.native.shutdown()
        self.tmp.cleanup()

    def runtime(self, revision, *, complete=True):
        folder = self.root / ('chromium-' + revision) / 'chrome-win64'
        folder.mkdir(parents=True)
        (folder / 'chrome.exe').write_bytes(b'fixture')
        if complete:
            for name in ('chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
                path = folder / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b'fixture')
        return folder / 'chrome.exe'

    def test_runtime_matches_pinned_revision_even_with_newer_and_lexically_later_leftovers(self):
        expected = self.runtime('1234')
        self.runtime('999');self.runtime('9999')
        with patch('app.browser_runtime.pinned_chromium', return_value=('1234', '151.0.7922.34')):
            self.assertEqual(expected, bundled_executable(self.root, windows=True))

    def test_missing_pinned_runtime_never_substitutes_another_version(self):
        self.runtime('9999')
        with patch('app.browser_runtime.pinned_chromium', return_value=('1234', '151.0.7922.34')):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                bundled_executable(self.root, windows=True)
        self.assertEqual('native_runtime_missing', caught.exception.details['reason'])

    def test_incomplete_windows_runtime_is_reported_before_launch(self):
        self.runtime('1234', complete=False)
        with patch('app.browser_runtime.pinned_chromium', return_value=('1234', '151.0.7922.34')):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                bundled_executable(self.root, windows=True)
        self.assertEqual('native_runtime_incomplete', caught.exception.details['reason'])
        self.assertIn('chrome.dll', caught.exception.details['missing'])

    def test_invalid_explicit_runtime_override_is_not_silently_ignored(self):
        with patch.dict(os.environ, {'IGAC_NATIVE_BROWSER_EXECUTABLE': str(self.root / 'absent.exe')}):
            with self.assertRaises(UpstreamUnavailableError) as caught:browser_executable()
        self.assertEqual('native_runtime_override_missing', caught.exception.details['reason'])

    def chrome(self):
        base = self.root / 'Program Files'
        exe = base / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
        exe.parent.mkdir(parents=True, exist_ok=True);exe.write_bytes(b'chrome fixture')
        self.enterContext(patch.dict(os.environ, {'PROGRAMFILES': str(base),
            'PROGRAMFILES(X86)': '', 'LOCALAPPDATA': '', 'IGAC_NATIVE_BROWSER_EXECUTABLE': ''}))
        return exe

    def profile(self):
        directory = self.native.directory(self.ident)
        directory.mkdir(parents=True)
        (directory / 'Cookies.fixture').write_bytes(b'preserve account data')
        return directory

    def test_windows_uses_installed_chrome_and_records_only_app_profile_engine(self):
        exe = self.chrome();directory = self.profile()
        with patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'), \
             patch('app.browser_runtime._bundled_or_development_executable') as bundled:
            chosen = select_browser_runtime(directory, windows=True)
        bundled.assert_not_called()
        self.assertEqual(str(exe), chosen['executable'])
        self.assertEqual('installed-chrome', chosen['source'])
        self.assertEqual(chosen, json.loads((directory / 'juxin-runtime.json').read_text(encoding='utf-8')))
        self.assertFalse((exe.parent / 'juxin-runtime.json').exists())
        self.assertEqual(b'preserve account data', (directory / 'Cookies.fixture').read_bytes())

    def test_machine_without_chrome_uses_bundled_engine(self):
        directory = self.profile();exe = self.runtime('1234')
        with patch.dict(os.environ, {'IGAC_NATIVE_BROWSER_EXECUTABLE': ''}), \
             patch('app.browser_runtime.installed_chrome', return_value=None), \
             patch('app.browser_runtime._bundled_or_development_executable', return_value=exe), \
             patch('app.browser_runtime.windows_file_version', return_value='151.0.7922.34'):
            chosen = select_browser_runtime(directory, windows=True)
        self.assertEqual('bundled', chosen['source'])
        self.assertEqual(str(exe), chosen['executable'])

    def test_missing_bound_chrome_never_falls_back_to_older_bundled_engine(self):
        exe = self.chrome();directory = self.profile()
        with patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'):
            select_browser_runtime(directory, windows=True)
        saved = (directory / 'juxin-runtime.json').read_bytes();exe.unlink()
        with patch('app.browser_runtime._bundled_or_development_executable') as bundled:
            with self.assertRaises(UpstreamUnavailableError) as caught:
                select_browser_runtime(directory, windows=True)
        self.assertEqual('native_runtime_bound_missing', caught.exception.details['reason'])
        bundled.assert_not_called()
        self.assertEqual(saved, (directory / 'juxin-runtime.json').read_bytes())

    def test_existing_chrome_last_version_blocks_downgrade_before_profile_is_written(self):
        self.chrome();directory = self.profile()
        (directory / 'Last Version').write_text('153.0.7999.10\n', encoding='utf-8')
        with patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                select_browser_runtime(directory, windows=True)
        self.assertEqual('native_runtime_downgrade', caught.exception.details['reason'])
        self.assertFalse((directory / 'juxin-runtime.json').exists())
        self.assertEqual(b'preserve account data', (directory / 'Cookies.fixture').read_bytes())

    def test_native_manager_applies_version_guard_before_spawning_process(self):
        self.chrome();directory = self.profile();self.native.executable = None
        (directory / 'Last Version').write_text('153.0.7999.10', encoding='utf-8')
        def resolve(profile):
            self.assertEqual(directory, profile)
            return Path(select_browser_runtime(profile, windows=True)['executable'])
        with patch('app.native_browser.browser_executable', side_effect=resolve), \
             patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'), \
             patch('app.native_browser._spawn_browser_process') as spawn:
            with self.assertRaises(UpstreamUnavailableError) as caught:
                self.native.connection_endpoint(self.ident, open_if_needed=True)
        self.assertEqual('native_runtime_downgrade', caught.exception.details['reason'])
        spawn.assert_not_called()
        self.assertNotIn(self.ident, self.native.processes)

    def test_bound_engine_accepts_updates_and_remembers_the_highest_version(self):
        self.chrome();directory = self.profile()
        with patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'):
            select_browser_runtime(directory, windows=True)
        with patch('app.browser_runtime.windows_file_version', return_value='153.0.7999.10'):
            select_browser_runtime(directory, windows=True)
        saved = (directory / 'juxin-runtime.json').read_bytes()
        with patch('app.browser_runtime.windows_file_version', return_value='152.0.7977.83'):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                select_browser_runtime(directory, windows=True)
        self.assertEqual('native_runtime_downgrade', caught.exception.details['reason'])
        self.assertEqual(saved, (directory / 'juxin-runtime.json').read_bytes())

    def test_bundled_profile_survives_install_path_change_without_switching_engines(self):
        self.chrome();directory = self.profile();exe = self.runtime('1234')
        record = {'source': 'bundled', 'executable': str(self.root / 'old-install' / 'chrome.exe'),
                  'version': '151.0.7922.34'}
        (directory / 'juxin-runtime.json').write_text(json.dumps(record), encoding='utf-8')
        with patch('app.browser_runtime._bundled_or_development_executable', return_value=exe), \
             patch('app.browser_runtime.windows_file_version', return_value='151.0.7922.34'), \
             patch('app.browser_runtime.installed_chrome') as installed:
            choice = select_browser_runtime(directory, windows=True)
        installed.assert_not_called()
        self.assertEqual('bundled', choice['source'])
        self.assertEqual(str(exe), choice['executable'])

    def test_corrupt_engine_record_is_not_overwritten(self):
        self.chrome();directory = self.profile();marker = directory / 'juxin-runtime.json'
        marker.write_text('broken fixture', encoding='utf-8')
        with self.assertRaises(UpstreamUnavailableError) as caught:
            select_browser_runtime(directory, windows=True)
        self.assertEqual('native_runtime_record_invalid', caught.exception.details['reason'])
        self.assertEqual('broken fixture', marker.read_text(encoding='utf-8'))

    def test_unknown_executable_version_does_not_touch_existing_profile(self):
        self.chrome();directory = self.profile()
        with patch('app.browser_runtime.windows_file_version', side_effect=OSError('fixture error')):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                select_browser_runtime(directory, windows=True)
        self.assertEqual('native_runtime_version_unavailable', caught.exception.details['reason'])
        self.assertFalse((directory / 'juxin-runtime.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows version resource API')
    def test_real_windows_version_resource_matches_running_python(self):
        version = windows_file_version(Path(sys.executable))
        self.assertEqual(tuple(map(int, version.split('.')[:2])), sys.version_info[:2])

    def test_exit_three_preserves_real_child_stderr_unicode_argv_and_exit_code(self):
        original_popen = subprocess.Popen
        commands = []
        # Write more than a pipe buffer. Undrained stderr would deadlock here.
        script = ("import json,os,sys; "
                  "os.write(2,b'x'*131072); "
                  "os.write(2,('CHILD_ARGV='+json.dumps(sys.argv[1:],ensure_ascii=False)+'\\n实际启动错误\\n').encode('utf-8')); "
                  "sys.exit(3)")

        def launch(args, **kwargs):
            commands.append((args, kwargs))
            return original_popen([sys.executable, '-c', script, *args[1:]], **kwargs)

        with patch('app.native_browser._spawn_browser_process', side_effect=launch):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                self.native.connection_endpoint(self.ident, open_if_needed=True)
        details = caught.exception.details
        self.assertEqual(3, details['exit_code'])
        report = json.loads(Path(details['startup_log']).read_text(encoding='utf-8'))
        output = Path(details['stderr_log']).read_bytes()
        self.assertLessEqual(len(output), BrowserLaunchLog.limit)
        text = output.decode('utf-8')
        self.assertIn('实际启动错误', text)
        child_args = json.loads(text.split('CHILD_ARGV=', 1)[1].splitlines()[0])
        directory = self.native.directory(self.ident)
        self.assertIn('--user-data-dir=' + str(directory), child_args)
        self.assertIn('--lang=zh-CN', child_args)
        preferences=json.loads((directory/'Default'/'Preferences').read_text(encoding='utf-8'))
        self.assertEqual('zh-CN,zh',preferences['intl']['accept_languages'])
        self.assertTrue(directory.is_absolute())
        self.assertEqual('startup_exited', report['status'])
        self.assertEqual(3, report['exit_code'])
        self.assertEqual(str(Path(sys.executable).resolve().parent), commands[0][1]['cwd'])
        self.assertNotIn('shell', commands[0][1])
        self.assertNotIn(self.ident, self.native.processes)
        self.assertNotIn('CHILD_ARGV', str(caught.exception))

    def test_spawn_failure_is_distinguished_from_chromium_exit(self):
        with patch('app.native_browser._spawn_browser_process', side_effect=OSError(13, 'denied')):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                self.native.connection_endpoint(self.ident, open_if_needed=True)
        self.assertEqual('native_browser_launch_failed', caught.exception.details['reason'])
        report = json.loads(Path(caught.exception.details['startup_log']).read_text(encoding='utf-8'))
        self.assertEqual('launch_failed', report['status'])
        self.assertNotIn(self.ident, self.native.processes)

    def test_fixture_file_log_captures_real_child_with_empty_stderr(self):
        original=subprocess.Popen
        self.native.startup_file_logging=True
        commands=[]
        parent_log=self.root/'parent-chrome.log';parent_log.write_text('preserve parent log')
        script=("import os,pathlib,sys; "
                "pathlib.Path(os.environ['CHROME_LOG_FILE']).write_text('FATAL: fixture file-only failure',encoding='utf-8'); "
                "sys.exit(3)")
        def launch(args,**kwargs):
            commands.append(args)
            return original([sys.executable,'-c',script,*args[1:]],**kwargs)
        with patch.dict(os.environ,{'CHROME_LOG_FILE':str(parent_log)}), \
             patch('app.native_browser._spawn_browser_process',side_effect=launch):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                self.native.connection_endpoint(self.ident,open_if_needed=True)
            self.assertEqual(str(parent_log),os.environ['CHROME_LOG_FILE'])
        self.assertEqual(3,caught.exception.details['exit_code'])
        self.assertEqual('preserve parent log',parent_log.read_text())
        report=json.loads(Path(caught.exception.details['startup_log']).read_text(encoding='utf-8'))
        self.assertEqual(b'',Path(report['stderr_file']).read_bytes())
        self.assertIn('file-only failure',Path(report['chrome_log']).read_text())
        self.assertIn('--enable-logging',commands[0])
        self.assertNotIn('--enable-logging=stderr',commands[0])
        self.assertNotIn('--no-sandbox',commands[0])
        self.assertNotIn(self.ident,self.native.processes)

    def test_fixture_collector_bundles_logs_after_original_profile_is_removed(self):
        import shutil
        from zipfile import ZipFile
        gate=self.gate()
        import repair_native_browser as repair
        directory=self.native.directory(self.ident);directory.mkdir(parents=True)
        (directory/'juxin-startup.json').write_text(json.dumps({'pid':123,'exit_code':3}))
        (directory/'juxin-startup.stderr.log').write_bytes(b'')
        (directory/'juxin-startup.chrome.log').write_bytes(b'first'+b'x'*(1024*1024+1)+b'last')
        (directory/'Cookies').write_bytes(b'private fixture data must not be packaged')
        run=self.root/'reports';run.mkdir()
        result=gate.collect_fixture_diagnostics(self.root,run,started_utc='fixture')
        self.assertEqual(3,len(result['files']))
        chrome=next(item for item in result['files'] if item['file'].endswith('.chrome.log'))
        self.assertTrue(chrome['truncated'])
        shutil.rmtree(self.root/'browser-profiles')
        archive=self.root/'repair.zip';repair.make_repair_bundle(run,archive)
        with ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            names=bundle.namelist()
            self.assertFalse(any('Cookies' in name for name in names))
            payload=bundle.read(next(name for name in names if name.endswith('.chrome.log')))
            self.assertTrue(payload.startswith(b'first'));self.assertTrue(payload.endswith(b'last'))
            self.assertLess(len(payload),1024*1024+100)
            self.assertIn('diagnostic-files.json',names)

    def test_fixture_diagnostic_failure_cannot_mask_launch_failure(self):
        gate=self.gate()
        output=self.root/'diagnostic-error'
        with patch.object(gate,'pinned_chromium',side_effect=RuntimeError('original launch failure')), \
             patch.object(gate,'collect_fixture_diagnostics',side_effect=PermissionError('log is locked')):
            with self.assertRaisesRegex(RuntimeError,'original launch failure'):
                gate.main(['--diagnostics-dir',str(output)])
        report=json.loads(next(output.glob('run-*/result.json')).read_text(encoding='utf-8'))
        self.assertEqual('original launch failure',report['error'])
        self.assertEqual('log is locked',report['diagnostic_error'])
        self.assertEqual('failed',report['status'])

    def test_failed_real_child_logs_reach_repair_archive(self):
        self.assert_failed_child_archive(event_exit_code=0)

    def test_failed_event_query_cannot_be_counted_as_a_browser_process(self):
        self.assert_failed_child_archive(event_exit_code=1)

    def assert_failed_child_archive(self,*,event_exit_code):
        import shutil
        import base64
        from zipfile import ZipFile
        gate=self.gate()
        import repair_native_browser as repair
        import diagnose_native_startup as diagnosis
        original=subprocess.Popen;children=[]
        event_queries=[]
        event_payload={'status':'collected','events':[{'id':1000,'message':'fixture-only event'}]}
        def query_events(args,**kwargs):
            # Exercise the actual Windows-only collector on every platform.
            # Replace only the PowerShell executable, using a real child. The
            # shared subprocess.run must not be captured by the browser mock.
            self.assertEqual('-EncodedCommand',args[-2])
            script_text=base64.b64decode(args[-1]).decode('utf-16le')
            self.assertIn('Get-WinEvent',script_text)
            self.assertEqual(15,kwargs['timeout'])
            self.assertEqual(str(children[0].pid),kwargs['env']['JUXIN_DIAGNOSTIC_PIDS'])
            child_script=('import json,sys;print(json.dumps('+repr(event_payload)+')); '
                          'sys.stderr.write("fixture event query failed") if '+str(event_exit_code)+' else None; '
                          'sys.exit('+str(event_exit_code)+')')
            response=subprocess.run([sys.executable,'-c',child_script],**kwargs)
            event_queries.append(response)
            return response
        script=("import pathlib,sys; "
                "root=pathlib.Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--user-data-dir='))); "
                "(root/'juxin-startup.chrome.log').write_text('FATAL: file-only exit-three fixture',encoding='utf-8'); "
                "sys.exit(3)")
        def launch(args,**kwargs):
            child=original([sys.executable,'-c',script,*args[1:]],**kwargs)
            children.append(child);return child
        output=self.root/'real-chain'
        with patch.object(gate,'pinned_chromium',return_value=('1234','151.0.7922.34')), \
             patch.object(gate,'select_browser_runtime',return_value={'executable':sys.executable,'version':None}), \
             patch('app.native_browser.browser_executable',return_value=Path(sys.executable)), \
             patch('app.native_browser._spawn_browser_process',side_effect=launch), \
             patch.object(diagnosis,'os',SimpleNamespace(name='nt',environ=os.environ)), \
             patch.object(diagnosis,'subprocess',SimpleNamespace(run=query_events)):
            with self.assertRaises(UpstreamUnavailableError) as caught:
                gate.main(['--no-headless','--diagnostics-dir',str(output)])
        report=json.loads(next(output.glob('run-*/result.json')).read_text(encoding='utf-8'))
        self.addCleanup(shutil.rmtree,Path(report['data_dir']).parent,ignore_errors=True)
        self.assertEqual(3,caught.exception.details['exit_code'])
        self.assertEqual([3],[child.poll() for child in children])
        self.assertEqual([event_exit_code],[response.returncode for response in event_queries])
        self.assertEqual(1,len(list(output.rglob('juxin-startup.chrome.log'))))
        bundle=self.root/'real-chain.zip';repair.make_repair_bundle(output,bundle)
        with ZipFile(bundle) as archive:
            logs=[name for name in archive.namelist() if name.endswith('.chrome.log')]
            self.assertEqual(1,len(logs))
            self.assertIn(b'file-only exit-three fixture',archive.read(logs[0]))
            stderr=next(name for name in archive.namelist() if name.endswith('.stderr.log'))
            self.assertEqual(b'',archive.read(stderr))
            startup=json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('juxin-startup.json'))))
            self.assertEqual(3,startup['exit_code'])
            events=json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('windows-events.json'))))
            expected_events=({'status':'unavailable','error':'fixture event query failed'}
                             if event_exit_code else event_payload)
            self.assertEqual(expected_events,events)

    def test_repair_can_verify_previous_project_without_new_constructor_keyword(self):
        gate=self.gate()
        class PreviousNativeBrowser:
            def __init__(self,db,root,*,headless):self.values=(db,root,headless)
        with patch.object(gate,'NativeBrowser',PreviousNativeBrowser):
            browser=gate.fixture_browser(None,self.root,False)
        self.assertEqual((None,self.root,False),browser.values)

    def test_fixture_diagnostics_records_missing_and_unreadable_files(self):
        gate=self.gate()
        directory=self.native.directory(self.ident);directory.mkdir(parents=True)
        (directory/'juxin-startup.json').write_text('{}')
        run=self.root/'reports';run.mkdir()
        original=gate.bounded_log
        def read(source,target):
            if source.name.endswith('.json'):raise PermissionError('locked report')
            return original(source,target)
        with patch.object(gate,'bounded_log',side_effect=read):
            result=gate.collect_fixture_diagnostics(self.root,run,started_utc='fixture')
        self.assertEqual(3,len(result['files']))
        self.assertEqual('locked report',result['files'][0]['error'])
        self.assertFalse(result['files'][1]['exists'])
        self.assertFalse(result['files'][2]['exists'])

    @unittest.skipIf(os.name=='nt','symlink privilege is not required by the Windows build')
    def test_fixture_diagnostics_never_follows_external_log_links(self):
        gate=self.gate()
        directory=self.native.directory(self.ident);directory.mkdir(parents=True)
        secret=self.root/'unrelated';secret.write_text('not a generated log')
        (directory/'juxin-startup.chrome.log').symlink_to(secret)
        run=self.root/'reports';run.mkdir()
        result=gate.collect_fixture_diagnostics(self.root,run,started_utc='fixture')
        self.assertEqual('linked diagnostic file omitted',result['files'][2]['error'])
        self.assertEqual([],list(run.rglob('juxin-startup.chrome.log')))

    def test_temporary_port_file_sharing_error_is_retried(self):
        port_file = self.native.directory(self.ident) / 'DevToolsActivePort'
        process = SimpleNamespace(pid=123, stderr=io.BytesIO(), poll=lambda: None)
        original_open = Path.open
        failures = []

        def launch(*args, **kwargs):
            port_file.write_text('12345\n/devtools/browser/fixture\n', encoding='utf-8')
            return process

        def open_port(path, *args, **kwargs):
            if path == port_file and kwargs.get('mode', 'r') == 'r' and kwargs.get('encoding') == 'utf-8' and not failures:
                failures.append(True)
                raise PermissionError('Windows file sharing during creation')
            return original_open(path, *args, **kwargs)

        try:
            with patch('app.native_browser._spawn_browser_process', side_effect=launch), patch.object(Path, 'open', open_port):
                result = self.native.connection_endpoint(self.ident, open_if_needed=True)
            self.assertEqual('ws://127.0.0.1:12345/devtools/browser/fixture', result['ws'])
            self.assertEqual([True], failures)
            report = json.loads((port_file.parent / 'juxin-startup.json').read_text(encoding='utf-8'))
            self.assertEqual('ready', report['status'])
        finally:
            state = self.native.processes.pop(self.ident, None)
            if state:state['diagnostics'].finish(0)

    def gate(self):
        path = Path(__file__).resolve().parents[2] / 'scripts' / 'verify_native_browser.py'
        spec = importlib.util.spec_from_file_location('native_gate', path)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.enterContext(patch.object(module, 'ensure_browser_sandbox_access', return_value={'status': 'fixture'}))
        return module

    def test_windows_verifies_production_visible_windows_by_default(self):
        gate = self.gate()
        self.assertFalse(gate.verification_headless(windows=True))
        self.assertTrue(gate.verification_headless(windows=False))
        self.assertTrue(gate.verification_headless(True, windows=True))
        self.assertFalse(gate.verification_headless(False, windows=False))

    def test_release_gate_failure_keeps_diagnostics_and_does_not_report_success(self):
        gate = self.gate()
        with patch.object(gate, 'pinned_chromium', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                gate.main(['--diagnostics-dir', str(self.root / 'diagnostics')])
        runs = list((self.root / 'diagnostics').glob('run-*'))
        self.assertEqual(1, len(runs))
        report = json.loads((runs[0] / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual('failed', report['status'])
        self.assertTrue((runs[0] / 'traceback.txt').is_file())
        self.assertTrue(Path(report['data_dir']).exists())

    def test_bundled_release_gate_rejects_missing_bundle_even_if_system_chrome_exists(self):
        gate = self.gate()
        self.chrome()
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': ''}), \
                patch.object(gate, 'select_browser_runtime') as select:
            with self.assertRaisesRegex(RuntimeError, 'IGAC_BROWSER_DIR'):
                gate.main(['--require-bundled', '--diagnostics-dir', str(self.root / 'missing-bundle')])
            select.assert_not_called()

    def test_bundled_release_gate_uses_exact_bundle_and_restores_environment(self):
        gate = self.gate()
        expected = self.runtime('1234')
        seen = []
        def select():
            seen.append(os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE'])
            return {'executable': str(expected), 'source': 'override', 'version': '151.0.7922.34'}
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.root), 'IGAC_NATIVE_BROWSER_EXECUTABLE': 'previous-override'}), \
                patch.object(gate, 'pinned_chromium', return_value=('1234', '151.0.7922.34')), \
                patch.object(gate, 'bundled_executable', return_value=expected), \
                patch.object(gate, 'select_browser_runtime', side_effect=select), \
                patch.object(gate, 'verify', new=AsyncMock(return_value={'actual_browser_versions': ['151.0.7922.34'] * 2})) as verify:
            gate.main(['--require-bundled', '--diagnostics-dir', str(self.root / 'bundle-success')])
            self.assertEqual('previous-override', os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE'])
        self.assertEqual([str(expected)], seen)
        verify.assert_awaited_once()
        report = json.loads(next((self.root / 'bundle-success').glob('run-*/result.json')).read_text(encoding='utf-8'))
        self.assertTrue(report['require_bundled'])
        self.assertEqual('passed', report['status'])

    def test_bundled_release_gate_rejects_wrong_version_before_starting_browser(self):
        gate = self.gate()
        expected = self.runtime('1234')
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.root)}), \
                patch.object(gate, 'pinned_chromium', return_value=('1234', '151.0.7922.34')), \
                patch.object(gate, 'bundled_executable', return_value=expected), \
                patch.object(gate, 'select_browser_runtime', return_value={'executable': str(expected), 'source': 'override', 'version': '150.0.0.1'}), \
                patch.object(gate, 'verify', new=AsyncMock()) as verify:
            with self.assertRaisesRegex(RuntimeError, 'pinned bundled'):
                gate.main(['--require-bundled', '--diagnostics-dir', str(self.root / 'wrong-version')])
            verify.assert_not_awaited()

    def test_bundled_gate_failure_restores_absent_override(self):
        gate = self.gate()
        expected = self.runtime('1234')
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.root)}):
            os.environ.pop('IGAC_NATIVE_BROWSER_EXECUTABLE', None)
            with patch.object(gate, 'bundled_executable', return_value=expected), \
                    patch.object(gate, 'pinned_chromium', side_effect=RuntimeError('injected metadata failure')):
                with self.assertRaisesRegex(RuntimeError, 'metadata failure'):
                    gate.main(['--require-bundled', '--diagnostics-dir', str(self.root / 'bundle-failure')])
            self.assertNotIn('IGAC_NATIVE_BROWSER_EXECUTABLE', os.environ)

    def test_compatible_gate_uses_recorded_actual_bundle_version(self):
        from app.browser_bundle import BUNDLE_DESCRIPTOR, official_archive_url
        gate = self.gate()
        expected = self.runtime('1234')
        version = '154.0.8037.57'
        (expected.parent.parent / BUNDLE_DESCRIPTOR).write_text(json.dumps({
            'format': 1, 'source': 'chrome-for-testing-stable',
            'driver_revision': '1234', 'driver_version': '151.0.7922.34',
            'version': version, 'download_url': official_archive_url(version), 'archive_sha256': 'a' * 64}))
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.root)}), \
                patch.object(gate, 'pinned_chromium', return_value=('1234', '151.0.7922.34')), \
                patch.object(gate, 'bundled_executable', return_value=expected), \
                patch.object(gate, 'select_browser_runtime', return_value={'executable': str(expected), 'source': 'override', 'version': version}), \
                patch.object(gate, 'verify', new=AsyncMock(return_value={'actual_browser_versions': [version] * 2, 'network_verified': True})):
            gate.main(['--require-bundled', '--diagnostics-dir', str(self.root / 'compatible')])
        report = json.loads(next((self.root / 'compatible').glob('run-*/result.json')).read_text())
        self.assertEqual('passed', report['status'])
        self.assertEqual(version, report['bundled_version'])
        self.assertEqual('151.0.7922.34', report['driver_browser_version'])

    def test_hung_verifier_writes_failure_and_restores_override_after_cleanup(self):
        gate = self.gate()
        expected = self.runtime('1234')
        events = []
        async def stalled(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                events.append('cleaned')
        output = self.root / 'hung-verifier'
        with patch.dict(os.environ, {'IGAC_BROWSER_DIR': str(self.root), 'IGAC_NATIVE_BROWSER_EXECUTABLE': 'previous-override'}), \
                patch.object(gate, 'pinned_chromium', return_value=('1234', '151.0.7922.34')), \
                patch.object(gate, 'bundled_executable', return_value=expected), \
                patch.object(gate, 'select_browser_runtime', return_value={'executable': str(expected), 'source': 'override', 'version': '151.0.7922.34'}), \
                patch.object(gate, 'VERIFICATION_TIMEOUT_SECONDS', 0.02), \
                patch.object(gate, 'verify', side_effect=stalled):
            with self.assertRaisesRegex(RuntimeError, 'verification timed out'):
                gate.main(['--require-bundled', '--diagnostics-dir', str(output)])
            self.assertEqual('previous-override', os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE'])
        self.assertEqual(['cleaned'], events)
        result = json.loads(next(output.glob('run-*/result.json')).read_text(encoding='utf-8'))
        self.assertEqual('failed', result['status'])
        self.assertIn('timed out', result['error'])
        self.assertNotIn('network_verified', result)


if __name__ == '__main__':unittest.main()
