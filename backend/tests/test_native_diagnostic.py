"""Local child fixtures for the standalone Windows diagnostic collector."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

path = Path(__file__).resolve().parents[2] / 'scripts' / 'diagnose_native_startup.py'
spec = importlib.util.spec_from_file_location('native_diagnostic', path)
diag = importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


class NativeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / '新建文件夹 (3)'
        self.root.mkdir()
    def tearDown(self):self.tmp.cleanup()

    def project_fixture(self):
        project = self.root / '原项目 ! (3)'
        for name in ('START_HERE_NEWGEN.bat', '.venv/Scripts/python.exe', 'backend/app/__init__.py'):
            file = project / name;file.parent.mkdir(parents=True, exist_ok=True);file.touch()
        (project / 'package.json').write_text(json.dumps({'name': 'juxin-ig-audience-collector-newgen'}))
        (project / 'backend/app/browser_runtime.py').write_text(
            "def bundled_executable(root):\n    raise RuntimeError('SELECTED_PROJECT_BACKEND')\n"
            "def pinned_chromium():\n    return ('1234', '151.0.7922.34')\n")
        (project / 'build/browsers').mkdir(parents=True)
        return project

    def test_missing_environment_is_reported_without_creating_output(self):
        with self.assertRaisesRegex(ValueError, '.venv/Scripts/python.exe'):
            diag.resolve_project(self.root)
        self.assertFalse((self.root / 'installer-output').exists())

    def test_project_validation_rejects_unrelated_folders(self):
        project = self.project_fixture()
        self.assertEqual(project.resolve(), diag.resolve_project(project))
        (project / 'package.json').write_text('{"name":"unrelated-app"}')
        with self.assertRaisesRegex(ValueError, '不是聚鑫国际'):
            diag.resolve_project(project)

    def test_external_diagnostic_uses_selected_backend_and_output_with_unicode_path(self):
        project = self.project_fixture()
        unrelated = self.root / '诊断包';unrelated.mkdir()
        process = subprocess.run([sys.executable, str(path), '--project-dir', str(project)],
                                 cwd=unrelated, capture_output=True, timeout=15)
        self.assertEqual(1, process.returncode, process.stderr.decode(errors='replace'))
        archives = list((project / 'installer-output').glob('native-startup-check-*.zip'))
        self.assertEqual(1, len(archives))
        with ZipFile(archives[0]) as archive:
            result = json.loads(archive.read('summary.json'))
            self.assertEqual('SELECTED_PROJECT_BACKEND', result['error'])
        self.assertFalse((unrelated / 'installer-output').exists())

    def test_environment_handoff_uses_original_project_with_trailing_separator(self):
        project = self.project_fixture()
        renamed = self.root / '原项目 ! & (15)'
        project.rename(renamed)
        unrelated = self.root / '独立诊断工具';unrelated.mkdir()
        env = {**os.environ, 'JUXIN_DIAGNOSTIC_PROJECT_DIR': str(renamed) + os.sep}
        process = subprocess.run([sys.executable, str(path), '--project-dir-env'],
                                 env=env, cwd=unrelated, capture_output=True, timeout=15)
        self.assertEqual(1, process.returncode, process.stderr.decode(errors='replace'))
        archives = list((renamed / 'installer-output').glob('native-startup-check-*.zip'))
        self.assertEqual(1, len(archives), process.stdout.decode(errors='replace'))
        with ZipFile(archives[0]) as archive:
            result = json.loads(archive.read('summary.json'))
            self.assertEqual('SELECTED_PROJECT_BACKEND', result['error'])
        self.assertFalse((unrelated / 'installer-output').exists())

    def test_environment_handoff_keeps_windows_path_verbatim(self):
        value = 'D:\\IG实验室\\新建文件夹 (15)\\'
        with patch.dict(os.environ, {'JUXIN_DIAGNOSTIC_PROJECT_DIR': value}), \
                patch.object(diag, 'main', return_value=7) as run:
            self.assertEqual(7, diag.cli(['--project-dir-env']))
        run.assert_called_once_with(Path(value))

    def test_missing_environment_handoff_cannot_silently_use_another_project(self):
        for value in ('', '   '):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {'JUXIN_DIAGNOSTIC_PROJECT_DIR': value}), \
                    patch.object(diag, 'main') as run:
                with self.assertRaises(SystemExit) as caught:
                    diag.cli(['--project-dir-env'])
                self.assertEqual(2, caught.exception.code)
                run.assert_not_called()

    def test_explicit_project_is_not_overridden_by_stale_environment(self):
        project = self.root / '明确选择'
        with patch.dict(os.environ, {'JUXIN_DIAGNOSTIC_PROJECT_DIR': 'stale-directory'}), \
                patch.object(diag, 'main', return_value=0) as run:
            self.assertEqual(0, diag.cli(['--project-dir', str(project)]))
        run.assert_called_once_with(project)

    def test_conflicting_project_sources_are_rejected_before_diagnosis(self):
        with patch.object(diag, 'main') as run:
            with self.assertRaises(SystemExit) as caught:
                diag.cli(['--project-dir-env', '--project-dir', str(self.root)])
        self.assertEqual(2, caught.exception.code)
        run.assert_not_called()

    def child(self, script):
        original = subprocess.Popen
        def start(args, **kwargs):
            return original([sys.executable, '-c', script, *args[1:]], **kwargs)
        return patch.object(diag.subprocess, 'Popen', side_effect=start)

    def test_file_logging_survives_empty_stderr_and_retains_unicode_paths(self):
        script = ("import json,os,pathlib,sys; "
                  "pathlib.Path(os.environ['CHROME_LOG_FILE']).write_text('FATAL: 模拟原始错误\\n'+json.dumps(sys.argv[1:],ensure_ascii=False),encoding='utf-8'); "
                  "sys.exit(3)")
        parent_log = os.environ.get('CHROME_LOG_FILE')
        with self.child(script):
            result = diag.probe(Path(sys.executable), self.root / '临时数据', self.root / 'report', timeout=3)
        self.assertEqual('startup_exited', result['status'])
        self.assertEqual(3, result['exit_code'])
        self.assertEqual(0, result['console_log']['bytes'])
        content = (self.root / 'report' / 'chrome.log').read_text(encoding='utf-8')
        self.assertIn('FATAL: 模拟原始错误', content)
        args = json.loads(content.splitlines()[1])
        self.assertIn('--user-data-dir=' + str(self.root / '临时数据'), args)
        self.assertIn('--enable-logging', args)
        self.assertNotIn('--enable-logging=stderr', args)
        self.assertNotIn('--no-sandbox', args)
        self.assertEqual(parent_log, os.environ.get('CHROME_LOG_FILE'))

    def test_exit_zero_before_cdp_is_not_a_success(self):
        with self.child('import sys;sys.exit(0)'):
            result = diag.probe(Path(sys.executable), self.root / 'p', self.root / 'report', timeout=3)
        self.assertEqual('startup_exited', result['status'])
        self.assertEqual(0, result['exit_code'])
        self.assertFalse(result['chrome_log']['exists'])

    def test_full_settings_reproduce_language_and_minimal_probe_omits_it(self):
        for minimal in (False,True):
            with self.subTest(minimal=minimal),self.child('import sys;sys.exit(3)'):
                profile=self.root/str(minimal)
                result=diag.probe(Path(sys.executable),profile,self.root/('report-'+str(minimal)),minimal=minimal,timeout=3)
            self.assertEqual(not minimal,'--lang=zh-CN' in result['args'])
            self.assertEqual(not minimal,(profile/'Default'/'Preferences').exists())
            if not minimal:
                settings=json.loads((profile/'Default'/'Preferences').read_text(encoding='utf-8'))
                self.assertEqual('zh-CN,zh',settings['intl']['accept_languages'])

    def test_timeout_stops_only_its_child_and_distinguishes_cleanup_exit(self):
        script = 'import time;time.sleep(30)'
        with self.child(script):
            result = diag.probe(Path(sys.executable), self.root / 'p', self.root / 'report', timeout=.1)
        self.assertEqual('startup_timeout', result['status'])
        self.assertNotIn('exit_code', result)
        self.assertIsNotNone(result['exit_after_cleanup'])

    def test_bundle_is_allowlisted_and_reports_omitted_logs(self):
        run = self.root / 'run';run.mkdir()
        for name in ('summary.json', 'windows-events.json', 'chrome.log'):
            (run / name).write_text('fixture')
        for name in ('Cookies', 'test.sqlite3', 'BrowserMetrics.pma', 'credentials.txt'):
            (run / name).write_text('never included')
        large = self.root / 'large.log';large.write_bytes(b'first' + b'x' * (diag.REPORT_LIMIT + 50) + b'last')
        info = diag.bounded_log(large, run / 'console.log')
        self.assertTrue(info['truncated'])
        result = (run / 'console.log').read_bytes()
        self.assertTrue(result.startswith(b'first'));self.assertTrue(result.endswith(b'last'))
        target = self.root / 'report.zip';diag.make_bundle(run, target)
        with ZipFile(target) as z:
            self.assertEqual({'summary.json', 'windows-events.json', 'chrome.log', 'console.log'}, set(z.namelist()))
            self.assertIsNone(z.testzip())

    def test_standalone_main_keeps_business_data_and_prior_diagnostics(self):
        output = self.root / 'installer-output';output.mkdir()
        old = output / 'native-browser-diagnostics' / 'existing-run'
        old.mkdir(parents=True);(old / 'result.json').write_text('old')
        business = self.root / 'data';business.mkdir();(business / 'app.sqlite').write_text('unchanged')
        def run_probe(executable, profile, report, **kwargs):
            profile.mkdir(parents=True);report.mkdir()
            result = {'status': 'startup_exited', 'exit_code': 3, 'pid': 321, 'profile': str(profile), 'args': [str(executable)]}
            diag.save_json(report / 'result.json', result)
            return result
        with patch.object(diag, 'ROOT', self.root), \
             patch('app.browser_runtime.bundled_executable', return_value=Path(sys.executable)), \
             patch('app.browser_runtime.pinned_chromium', return_value=('1234','151.0.7922.34')), \
             patch.object(diag, 'installed_comparison_browser', return_value=None), \
             patch.object(diag, 'probe', side_effect=run_probe), \
             patch.object(diag, 'windows_events', return_value={'status': 'no_matching_events'}):
            self.assertEqual(0, diag.main())
        self.assertEqual('unchanged', (business / 'app.sqlite').read_text())
        self.assertEqual('old', (old / 'result.json').read_text())
        archives = list(output.glob('native-startup-check-*.zip'));self.assertEqual(1, len(archives))
        with ZipFile(archives[0]) as z:
            summary = json.loads(z.read('summary.json'))
            self.assertEqual('collected', summary['status'])
            self.assertEqual(['app-settings','short-data-path','minimal-settings'], [r['name'] for r in summary['cases']])
            self.assertTrue(all(r['status'] == 'startup_exited' for r in summary['cases']))
            self.assertTrue(all(not Path(r['profile']).exists() for r in summary['cases']))


if __name__ == '__main__':unittest.main()
