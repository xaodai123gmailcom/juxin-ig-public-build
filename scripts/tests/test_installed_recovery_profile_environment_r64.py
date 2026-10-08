"""Pure installed-launch contract tests; no application imports or processes.

Execute selected real function ASTs with mocked process/browser dependencies.
The Windows privacy gate and actual installed acceptance remain separate.
"""
import ast
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/verify_installed_recovery_r64.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
PROFILE = {'APPDATA': r'C:\Users\fixture\AppData\Roaming',
           'LOCALAPPDATA': r'C:\Users\fixture\AppData\Local',
           'USERPROFILE': r'C:\Users\fixture', 'HOME': r'C:\Users\fixture'}
POISON = {'IGAC_DB_PATH': 'actual-user-database', 'IGAC_DATA_DIR': 'actual-user-data',
          'IGAC_STARTUP_TOKEN': 'inherited-session', 'COLLECTOR_CORE_TOKEN': 'inherited-core',
          'COLLECTOR_CORE_PORT': '1234', 'PLAYWRIGHT_BROWSERS_PATH': 'inherited-browser',
          'PYTHONPATH': 'inherited-python', 'OPENAI_API_KEY': 'inherited-api-key',
          'PEXELS_API_KEY': 'inherited-api-key', 'ELECTRON_RUN_AS_NODE': '1',
          'OPENVINO_LIB_PATHS': 'inherited-runtime', '__PYVENV_LAUNCHER__': 'inherited-launcher'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def functions(*names, **dependencies):
    namespace = dict(os=os, Path=Path, json=json, math=math, require=require, contextmanager=contextmanager,
                     sys=SimpleNamespace(platform='win32'), **dependencies)
    nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    if 'diagnostic_phase' not in names:
        nodes += [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == 'diagnostic_phase']
    declarations = [node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name.startswith('InstalledRecovery')
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id.startswith('INSTALLED_') for target in node.targets)]
    exec(compile(ast.Module(body=declarations + nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in names})


class InstalledProfileEnvironmentTests(unittest.TestCase):
    def test_failed_cli_keeps_traceback_runner_local_and_removes_stale_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'proof.json'
            report.write_text('{"verified":true}', encoding='utf-8')
            args = SimpleNamespace(executable=Path('desktop.exe'), core_executable=Path('core.exe'),
                core_report=Path('core.json'), log=Path('runtime.log'), report=report, timeout=120)
            parser = Mock(); parser.parse_args.return_value = args
            problem = RuntimeError('runner-only-private-exception')
            probe = Mock(side_effect=problem)
            main = functions('main', argparse=SimpleNamespace(ArgumentParser=lambda **kwargs: parser),
                probe_installed=probe, traceback=traceback).main
            output, errors = io.StringIO(), io.StringIO()
            main.__globals__['sys'] = SimpleNamespace(stderr=errors)
            with patch('sys.stdout', output):
                self.assertEqual(main(), 1)
            self.assertFalse(report.exists())
            self.assertIn('Traceback (most recent call last):', errors.getvalue())
            self.assertIn(str(SOURCE), errors.getvalue())
            self.assertIn('RuntimeError: runner-only-private-exception', errors.getvalue())
            self.assertEqual(output.getvalue(), 'INSTALLED_RECOVERY_PHASE=started\n'
                'INSTALLED_RECOVERY_R64=FAIL runner-only-private-exception\n')
            self.assertNotIn('INSTALLED_RECOVERY_R64=PASS', output.getvalue())
            probe.assert_called_once_with(args.executable, args.core_executable, args.core_report,
                args.log, timeout=120)

    def test_failed_traceback_output_still_returns_failure_without_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(executable='desktop', core_executable='core', core_report='prior',
                log='log', report=Path(temporary) / 'proof.json', timeout=120)
            parser = Mock(); parser.parse_args.return_value = args
            trace = Mock(); trace.print_exc.side_effect = OSError('diagnostic stream unavailable')
            main = functions('main', argparse=SimpleNamespace(ArgumentParser=lambda **kwargs: parser),
                probe_installed=Mock(side_effect=RuntimeError('proof failed')), traceback=trace).main
            main.__globals__['sys'] = SimpleNamespace(stderr=io.StringIO())
            with patch('sys.stdout', io.StringIO()):
                self.assertEqual(main(), 1)
            trace.print_exc.assert_called_once()
            self.assertFalse(args.report.exists())

    def test_successful_cli_writes_receipt_without_failure_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(executable='desktop', core_executable='core', core_report='prior',
                log='log', report=Path(temporary) / 'proof.json', timeout=120)
            parser = Mock(); parser.parse_args.return_value = args
            proof = {'synthetic_test': True}
            trace = Mock()
            main = functions('main', argparse=SimpleNamespace(ArgumentParser=lambda **kwargs: parser),
                probe_installed=Mock(return_value=proof), traceback=trace).main
            output = io.StringIO()
            with patch('sys.stdout', output):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(args.report.read_text()), proof)
            self.assertEqual(output.getvalue().splitlines()[:2],
                ['INSTALLED_RECOVERY_PHASE=started', 'INSTALLED_RECOVERY_PHASE=complete'])
            self.assertTrue(output.getvalue().splitlines()[2].startswith('INSTALLED_RECOVERY_R64=PASS '))
            trace.print_exc.assert_not_called()

    def test_phase_markers_are_fixed_best_effort_observations(self):
        diagnostic = functions('diagnostic_phase').diagnostic_phase
        expected = {'started', 'source-binding', 'import-setup', 'seed', 'launch', 'debugger',
                    'renderer', 'preload', 'readiness', 'api', 'shutdown', 'validation', 'complete'}
        self.assertEqual(diagnostic.__globals__['INSTALLED_DIAGNOSTIC_PHASES'], expected)
        output = io.StringIO()
        with patch('sys.stdout', output):
            for phase in sorted(expected): diagnostic(phase)
            for value in ('private detail', 'api\nprivate detail', '', None, 3, [], object()):
                diagnostic(value)
        self.assertEqual(output.getvalue().splitlines(),
            ['INSTALLED_RECOVERY_PHASE=' + phase for phase in sorted(expected)])
        for failure in (OSError('stream closed'), KeyboardInterrupt(), SystemExit(99)):
            diagnostic.__globals__['print'] = Mock(side_effect=failure)
            self.assertIsNone(diagnostic('api'))

    def test_started_marker_never_changes_cli_parse_failure(self):
        parser = Mock(); parser.parse_args.side_effect = SystemExit(2)
        main = functions('main', argparse=SimpleNamespace(ArgumentParser=lambda **kwargs: parser)).main
        with patch('sys.stdout', io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.getvalue(), 'INSTALLED_RECOVERY_PHASE=started\n')

    def test_profile_values_survive_while_inherited_auth_and_runtime_controls_do_not(self):
        with patch.dict(os.environ, {**PROFILE, **POISON}, clear=True):
            before = dict(os.environ)
            env = functions('environment').environment()
            self.assertEqual(before, dict(os.environ))
        for key, value in PROFILE.items():
            self.assertEqual(env[key], value)
        self.assertTrue(set(POISON).isdisjoint(env))
        self.assertEqual(env['PYTHONUTF8'], '1')
        self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')

    def run_launch(self, profile, *, session_ready=True, exercise_error=None, fire_watchdogs=False,
                   late_teardown=False, cleanup_error=None, shutdown_hang=False, diagnostic_error=None):
        """Run the whole actual launch function, with every runtime edge fake."""
        with tempfile.TemporaryDirectory(prefix='r64-profile-contract-') as temporary:
            base = Path(temporary)
            executable = base / 'install/desktop.exe'
            core = executable.parent / 'resources/backend/collector_core/collector_core.exe'
            core.parent.mkdir(parents=True)
            for target in (executable, core, executable.parent / 'resources/app.asar'):
                target.write_bytes(b'pure-test-artifact')
            root = base / 'probe'; root.mkdir()
            user_data = root / 'roaming/juxin-ig-audience-collector-newgen'
            data = user_data / 'data'
            digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            manifest = {'seed_pid': 77, 'seed_completed_perf_ns': 1,
                        'chronology_clock': 'mock-clock', 'database_sha256': 'a' * 64, 'nonce': 'b' * 64}
            def seed(directory):
                self.assertEqual(directory, data)
                directory.mkdir(parents=True)
                (directory / 'collector.sqlite3').write_bytes(b'synthetic-seed-only')
                path = directory / 'recovery-input.json'
                path.write_text(json.dumps(manifest), encoding='utf-8')
                return path, manifest
            fixture = SimpleNamespace(seed=Mock(side_effect=seed), CONTRACT='mock-contract',
                inspect=Mock(return_value={'verified': True}), legacy=SimpleNamespace(file_sha256=digest))
            prior = {'verified': True, 'persisted_state': {'verified': True},
                     'runtime': {'frozen': True, 'windows': True, 'executable': str(core),
                                 'executable_sha256': digest(core)}, 'manifest_sha256': 'c' * 64}
            report = base / 'core-report.json'
            report.write_text(json.dumps({'nurture_cleanup_upgrade': prior}), encoding='utf-8')
            process = Mock(pid=100, returncode=0); process.poll.return_value = None
            captured = {}
            def launch(args, **kwargs):
                self.assertTrue((data / 'collector.sqlite3').is_file(), 'seed must precede launch')
                captured.update(args=args, **kwargs)
                return process
            native = SimpleNamespace(Popen=Mock(side_effect=launch), run=Mock(return_value=Mock(returncode=0)),
                                     STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired)
            page = Mock(url='file:///synthetic/resources/app.asar/renderer/dist/index.html')
            page.evaluate.return_value = {'ok': False, 'error':
                'Missing application session token' if session_ready else 'unexpected authenticated state'}
            browser = Mock(); playwright = Mock()
            playwright.chromium.connect_over_cdp.return_value = browser
            @contextmanager
            def playwright_session():
                yield playwright
                if late_teardown:
                    now[0] = 420
            fake_playwright = SimpleNamespace(sync_playwright=playwright_session, Error=type('PlaywrightError', (Exception,), {}))
            timers = []
            def timer(*args):
                watchdog = Mock(callback=args[1]); watchdog.is_alive.return_value = False; timers.append(watchdog); return watchdog
            threads = SimpleNamespace(Event=threading.Event, Lock=threading.Lock, Timer=timer)
            now = [0]
            clock = SimpleNamespace(perf_counter_ns=Mock(side_effect=[2, 3]),
                monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            def dispatch(*args):
                now[0] += 31
            runtime = {'pid': 101, 'parent_pid': process.pid, 'executable': str(core), 'sha256': digest(core)}
            observed_core = Mock(return_value=runtime)
            cleanup = Mock(side_effect=cleanup_error)
            def exercise_api(*args, **kwargs):
                if fire_watchdogs:
                    timers[-1].callback()
                    timers[0].callback()
                if exercise_error:
                    raise exercise_error
                return {'checks': {}}
            exercise = Mock(side_effect=exercise_api)
            if shutdown_hang:
                process.wait.side_effect = subprocess.TimeoutExpired('desktop', 60)
            binding = {'source_commit': 'd' * 40, 'source_provenance': {'schema': 1}}
            validate = Mock(side_effect=lambda proof, **kwargs: proof)
            probe = functions('environment', 'probe_installed', 'installed_phase', 'require_owned_process', fixture=fixture,
                core_probe=SimpleNamespace(probe_directory=lambda **kwargs: nullcontext(root)),
                source_binding=Mock(return_value=binding), reserve_port=Mock(side_effect=[31001, 31002, 31003]),
                subprocess=native, threading=threads, time=clock, exact_renderer=Mock(return_value=page),
                wait_installed_renderer=Mock(return_value=page), wait_installed_preload=Mock(return_value=page.url),
                dispatch_browser_events=dispatch,
                installed_core_process=observed_core, exercise=exercise,
                stop_owned_runtime=cleanup, validate_proof=validate)
            if diagnostic_error:
                probe.probe_installed.__globals__['print'] = Mock(side_effect=diagnostic_error)
            output = io.StringIO()
            with patch.dict(os.environ, {**profile, **POISON}, clear=True), \
                    patch.dict(sys.modules, {'playwright': SimpleNamespace(), 'playwright.sync_api': fake_playwright}), \
                    patch('sys.stdout', output):
                if exercise_error or not session_ready or fire_watchdogs or late_teardown or shutdown_hang:
                    expected = ('process deadline expired' if fire_watchdogs or late_teardown else
                        'did not shut down before deadline' if shutdown_hang else
                        str(exercise_error) if exercise_error else 'phase deadline expired')
                    with self.assertRaisesRegex(RuntimeError, expected):
                        probe.probe_installed(executable, core, report, base / 'log.txt')
                    cleanup.assert_called_once_with(process, runtime)
                    validate.assert_not_called()
                    if not session_ready:
                        exercise.assert_not_called()
                else:
                    proof = probe.probe_installed(executable, core, report, base / 'log.txt')
                    self.assertIs(proof['user_data_touched'], False)
                    self.assertIs(proof['live_accounts_tested'], False)
                    self.assertIs(proof['synthetic'], True)
                    self.assertEqual(proof['source_provenance'], binding['source_provenance'])
                    exercise.assert_called_once()
                    self.assertEqual(exercise.call_args.args[1:], (data, manifest))
                    fixture.inspect.assert_called_once_with(data, manifest, phase='complete')
                    browser.new_browser_cdp_session.return_value.send.assert_called_once_with('Browser.close')
                    process.wait.assert_called_once()
                    self.assertIn(str(runtime['pid']), native.run.call_args.args[0][-1])
                    cleanup.assert_not_called()
            phases = ['source-binding', 'import-setup', 'seed', 'launch', 'debugger', 'renderer', 'preload', 'readiness']
            if session_ready:
                phases.append('api')
                if not exercise_error and not fire_watchdogs:
                    phases.append('shutdown')
                    if not late_teardown and not shutdown_hang:
                        phases.append('validation')
            self.assertEqual(output.getvalue().splitlines(), [] if diagnostic_error else
                ['INSTALLED_RECOVERY_PHASE=' + phase for phase in phases])
            self.assertEqual(len(timers), 5 if not session_ready else 6 if exercise_error or fire_watchdogs else 7)
            for watchdog in timers:
                watchdog.start.assert_called_once(); watchdog.cancel.assert_called_once()
            self.assertEqual(captured['cwd'], root)
            self.assertEqual(captured['env']['IGAC_DB_PATH'], str(data / 'collector.sqlite3'))
            self.assertEqual(captured['env']['COLLECTOR_CORE_PORT'], '31001')
            self.assertTrue((set(POISON) - {'IGAC_DB_PATH', 'COLLECTOR_CORE_PORT'}).isdisjoint(captured['env']))
            self.assertIn('--user-data-dir=' + str(user_data), captured['args'])
            self.assertIn('--proxy-server=http://127.0.0.1:31003', captured['args'])
            self.assertIn('--disable-background-networking', captured['args'])
            for key in PROFILE:
                self.assertEqual(captured['env'].get(key), profile.get(key))
                self.assertEqual(key in captured['env'], key in profile)

    def test_actual_launch_preserves_all_four_inherited_profile_values(self):
        self.run_launch(PROFILE)

    def test_actual_launch_does_not_invent_missing_profile_values(self):
        self.run_launch({})

    def test_unavailable_phase_output_preserves_success_and_original_failure(self):
        self.run_launch(PROFILE, diagnostic_error=OSError('diagnostic unavailable'))
        self.run_launch(PROFILE, exercise_error=RuntimeError('original API failure'),
            diagnostic_error=OSError('diagnostic unavailable'))

    def test_missing_unauthenticated_session_guard_fails_and_cleans_up(self):
        self.run_launch(PROFILE, session_ready=False)

    def test_api_failure_still_reaps_owned_runtime_and_cancels_watchdog(self):
        self.run_launch(PROFILE, exercise_error=RuntimeError('mock API refusal'))

    def test_phase_global_and_finally_share_idempotent_owned_cleanup(self):
        self.run_launch(PROFILE, fire_watchdogs=True)

    def test_teardown_crossing_overall_deadline_cannot_publish_success(self):
        self.run_launch(PROFILE, late_teardown=True)

    def test_cleanup_failure_preserves_original_api_failure(self):
        self.run_launch(PROFILE, exercise_error=RuntimeError('mock API refusal'), cleanup_error=OSError('cleanup failed'))

    def test_shutdown_timeout_cleans_owned_runtime_and_writes_no_receipt(self):
        self.run_launch(PROFILE, shutdown_hang=True)

    def test_core_configuration_prefers_explicit_fixture_paths(self):
        source = ROOT / 'backend/app/config.py'
        config = ast.parse(source.read_text(encoding='utf-8'))
        settings = next(node for node in config.body if isinstance(node, ast.ClassDef) and node.name == 'Settings')
        method = copy.deepcopy(next(node for node in settings.body if isinstance(node, ast.FunctionDef) and node.name == 'from_env'))
        method.decorator_list = []
        scope = dict(os=os, Path=Path, _default_data_dir=lambda: Path('actual-user-default'),
                     _is_loopback_url=lambda value: True,
                     validate_cloud_configuration=lambda *values: values)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), scope)
        with patch.dict(os.environ, {**PROFILE, 'IGAC_STARTUP_TOKEN': 's' * 32,
                                    'IGAC_DATA_DIR': 'synthetic/data', 'IGAC_DB_PATH': 'synthetic/data/collector.sqlite3'}, clear=True):
            result = scope['from_env'](SimpleNamespace)
        self.assertEqual(result.data_dir, Path('synthetic/data'))
        self.assertEqual(result.database_path, Path('synthetic/data/collector.sqlite3'))

    def test_cleanup_fallback_remains_bounded_and_identity_checked(self):
        process = Mock(pid=100); process.poll.return_value = None
        core = {'pid': 101, 'parent_pid': 100, 'executable': 'C:/synthetic/core.exe'}
        native = SimpleNamespace(run=Mock(side_effect=[subprocess.CalledProcessError(1, ['taskkill']), Mock()]),
                                 CalledProcessError=subprocess.CalledProcessError, TimeoutExpired=subprocess.TimeoutExpired)
        stop = functions('stop_owned_runtime', subprocess=native).stop_owned_runtime
        stop(process, core)
        process.kill.assert_called_once(); process.wait.assert_called_once_with(timeout=10)
        self.assertEqual(native.run.call_args_list[0].args[0], ['taskkill.exe', '/PID', '100', '/T', '/F'])
        command = native.run.call_args_list[1].args[0][-1]
        for guard in ('ProcessId = 101', 'ParentProcessId -ne 100', 'ExecutablePath -ine', core['executable']):
            self.assertIn(guard, command)
        native.run.reset_mock(); process.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, 'unowned Core'):
            stop(process, {**core, 'parent_pid': 999})
        native.run.assert_not_called()

    def test_proof_validator_still_requires_strict_false_user_data_claim(self):
        function = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == 'validate_proof')
        keys = next(node.value for node in function.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'expected_keys' for target in node.targets))
        proof = dict.fromkeys(ast.literal_eval(keys))
        proof.update(verified=True, contract='contract', synthetic=True, live_accounts_tested=False,
                     user_data_touched=False, desktop_app_asar_sha256='hash', checks={})
        validate = functions('validate_proof', source_binding=lambda: {}, FLAGS=('mandatory',),
                             fixture=SimpleNamespace(CONTRACT='contract', legacy=SimpleNamespace(file_sha256=lambda path: 'hash'))).validate_proof
        for field, bad_values in {'user_data_touched': [True, 0, None],
                                  'live_accounts_tested': [True, 0, None], 'synthetic': [False, 1, None]}.items():
            for value in bad_values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(RuntimeError, 'isolation evidence'):
                    validate({**proof, field: value}, executable='desktop.exe', core_executable='core.exe')
        with self.assertRaisesRegex(RuntimeError, 'API coverage'):
            validate(proof, executable='desktop.exe', core_executable='core.exe')


class FakePlaywrightTimeout(Exception):
    pass


class InstalledRendererWaitTests(unittest.TestCase):
    root = Path('C:/Installed App')
    url = 'file:///C:/Installed%20App/resources/app.asar/renderer/dist/index.html'

    def make_wait(self, initial=(), callbacks=()):
        now, pending = [0.0], list(callbacks)
        context = SimpleNamespace(pages=list(initial))
        def dispatch(event, *, timeout):
            self.assertEqual(event, 'page')
            self.assertGreater(timeout, 0); self.assertLessEqual(timeout, 100)
            now[0] += timeout / 1000
            if pending:
                pending.pop(0)(context)
            raise FakePlaywrightTimeout()
        context.wait_for_event = Mock(side_effect=dispatch)
        browser = SimpleNamespace(contexts=[context])
        process = Mock(); process.poll.return_value = None
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=Mock(side_effect=AssertionError('must dispatch Playwright')))
        loaded = functions('exact_renderer', 'dispatch_browser_events', 'wait_installed_renderer',
            'wait_installed_preload', 'require_owned_process', time=clock)
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {'playwright.sync_api': SimpleNamespace(TimeoutError=FakePlaywrightTimeout)}).start()
        return loaded, browser, process, context, now

    def page(self, url=None, preload='function'):
        return Mock(url=self.url if url is None else url, evaluate=Mock(return_value=preload))

    def test_delayed_page_creation_is_dispatched_without_evaluating_other_tabs(self):
        foreign, page = self.page('https://www.instagram.com/'), self.page()
        loaded, browser, process, context, now = self.make_wait([foreign], [lambda context: None,
            lambda context: context.pages.append(page)])
        self.assertIs(loaded.wait_installed_renderer(browser, self.root, process, 1), page)
        self.assertEqual(context.wait_for_event.call_count, 2)
        foreign.evaluate.assert_not_called(); page.evaluate.assert_not_called()

    def test_delayed_navigation_is_dispatched_even_without_a_page_event(self):
        page = self.page('about:blank')
        loaded, browser, process, context, now = self.make_wait([page],
            [lambda context: setattr(page, 'url', self.url)])
        self.assertIs(loaded.wait_installed_renderer(browser, self.root, process, 1), page)
        context.wait_for_event.assert_called_once()
        page.evaluate.assert_not_called()

    def test_absent_renderer_expires_without_first_tab_fallback(self):
        foreign = self.page('https://www.instagram.com/')
        loaded, browser, process, context, now = self.make_wait([foreign])
        with self.assertRaisesRegex(RuntimeError, 'target was not observed') as caught:
            loaded.wait_installed_renderer(browser, self.root, process, .25)
        self.assertEqual(type(caught.exception).__name__, 'InstalledRecoveryRendererTimeout')
        self.assertEqual(context.wait_for_event.call_count, 3)
        self.assertAlmostEqual(now[0], .25)
        foreign.evaluate.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            loaded.exact_renderer(browser, self.root)

    def test_duplicate_exact_targets_fail_immediately_before_preload(self):
        pages = [self.page(), self.page()]
        loaded, browser, process, context, now = self.make_wait(pages)
        with self.assertRaisesRegex(RuntimeError, 'Multiple packaged') as caught:
            loaded.wait_installed_renderer(browser, self.root, process, 1)
        self.assertEqual(type(caught.exception).__name__, 'InstalledRecoveryRendererAmbiguous')
        context.wait_for_event.assert_not_called()
        for page in pages: page.evaluate.assert_not_called()

    def test_remote_authority_query_wrong_root_and_account_never_evaluated(self):
        for url in ('https://www.instagram.com/', 'file://foreign-host/C:/Installed%20App/resources/app.asar/renderer/dist/index.html',
                    self.url.replace('Installed%20App', 'Other%20App'), self.url + '?remote=1', 'about:blank'):
            with self.subTest(url=url):
                page = self.page(url)
                loaded, browser, process, context, now = self.make_wait([page])
                self.assertIsNone(loaded.exact_renderer(browser, self.root, check_preload=False))
                with self.assertRaisesRegex(RuntimeError, 'exactly one'):
                    loaded.exact_renderer(browser, self.root)
                page.evaluate.assert_not_called()

    def test_missing_preload_has_its_own_bounded_category(self):
        page = self.page(preload='undefined')
        page.wait_for_function.side_effect = FakePlaywrightTimeout()
        loaded, browser, process, context, now = self.make_wait([page])
        with self.assertRaisesRegex(RuntimeError, 'preload API was not observed') as caught:
            loaded.wait_installed_preload(browser, self.root, process, page, .75)
        self.assertEqual(type(caught.exception).__name__, 'InstalledRecoveryPreloadTimeout')
        self.assertEqual(page.wait_for_function.call_args.kwargs['timeout'], 750)
        page.evaluate.assert_not_called()

    def test_hash_navigation_keeps_same_packaged_document_and_preload_check(self):
        page = self.page(self.url + '#login')
        page.wait_for_function.side_effect = lambda *args, **kwargs: setattr(page, 'url', self.url + '#workspace')
        loaded, browser, process, context, now = self.make_wait([page])
        self.assertEqual(loaded.wait_installed_preload(browser, self.root, process, page, 1), self.url)
        self.assertEqual(page.wait_for_function.call_args.kwargs['arg'], self.url)
        self.assertIn("location.href.split('#', 1)[0] === url", page.wait_for_function.call_args.args[0])
        page.evaluate.assert_called_once_with('typeof window.collectorCore?.request')

    def test_duplicate_appearing_during_preload_is_rejected(self):
        page = self.page()
        loaded, browser, process, context, now = self.make_wait([page])
        page.wait_for_function.side_effect = lambda *args, **kwargs: context.pages.append(self.page())
        with self.assertRaisesRegex(RuntimeError, 'Multiple packaged'):
            loaded.wait_installed_preload(browser, self.root, process, page, 1)
        page.evaluate.assert_not_called()

    def test_process_exit_before_and_during_dispatch_is_prompt(self):
        for before in (True, False):
            with self.subTest(before=before):
                loaded, browser, process, context, now = self.make_wait()
                if before:
                    process.poll.return_value = 1
                else:
                    original = context.wait_for_event.side_effect
                    def dispatch(*args, **kwargs):
                        process.poll.return_value = 1
                        return original(*args, **kwargs)
                    context.wait_for_event.side_effect = dispatch
                with self.assertRaisesRegex(RuntimeError, 'Owned installed desktop exited') as caught:
                    loaded.wait_installed_renderer(browser, self.root, process, 1)
                self.assertEqual(type(caught.exception).__name__, 'InstalledRecoveryProcessExited')
                self.assertEqual(context.wait_for_event.call_count, 0 if before else 1)

    def test_unexpected_dispatch_error_is_not_hidden_as_a_poll_timeout(self):
        loaded, browser, process, context, now = self.make_wait()
        context.wait_for_event.side_effect = ValueError('protocol failure')
        with self.assertRaisesRegex(ValueError, 'protocol failure'):
            loaded.wait_installed_renderer(browser, self.root, process, 1)


class InstalledPhaseBudgetTests(unittest.TestCase):
    def phase(self):
        timers, now = [], [0.0]
        def timer(seconds, callback):
            timer = Mock(seconds=seconds, callback=callback)
            timer.is_alive.return_value = False
            timers.append(timer)
            return timer
        clock = SimpleNamespace(monotonic=lambda: now[0])
        threads = SimpleNamespace(Timer=timer, Event=threading.Event, Lock=threading.Lock)
        loaded = functions('installed_phase', 'require_owned_process', time=clock, threading=threads)
        scope = loaded.installed_phase.__wrapped__.__globals__
        process = Mock(); process.poll.return_value = None
        cleanup = Mock()
        return loaded.installed_phase, scope, process, cleanup, timers, now

    def test_each_phase_has_independent_cap_and_timer_is_joined_before_rollover(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        with phase(process, 420, 30, scope['InstalledRecoveryDebuggerTimeout'], cleanup=cleanup) as deadline:
            self.assertEqual(deadline, 30)
            now[0] = 20
        timers[0].join.assert_called_once_with(timeout=45)
        with phase(process, 420, 210, scope['InstalledRecoveryRendererTimeout'], cleanup=cleanup) as deadline:
            self.assertEqual(deadline, 230)
        self.assertEqual([timer.seconds for timer in timers], [30, 210])
        cleanup.assert_not_called()

    def test_callback_entering_after_cancel_cannot_kill_next_phase(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        with phase(process, 420, 30, scope['InstalledRecoveryDebuggerTimeout'], cleanup=cleanup):
            timers[0].cancel.side_effect = timers[0].callback
        with phase(process, 420, 210, scope['InstalledRecoveryRendererTimeout'], cleanup=cleanup):
            timers[0].callback()
        cleanup.assert_not_called()

    def test_already_entered_timer_callback_is_joined_and_cannot_report_success(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        with self.assertRaises(scope['InstalledRecoveryPreloadTimeout']):
            with phase(process, 420, 15, scope['InstalledRecoveryPreloadTimeout'], cleanup=cleanup):
                timers[0].callback()
        cleanup.assert_called_once_with()
        timers[0].cancel.assert_called_once(); timers[0].join.assert_called_once_with(timeout=45)

    def test_watchdog_failure_cleanup_does_not_mask_the_timeout(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        cleanup.side_effect = OSError('cleanup failed')
        with self.assertRaises(scope['InstalledRecoveryApiTimeout']):
            with phase(process, 420, 60, scope['InstalledRecoveryApiTimeout'], cleanup=cleanup):
                timers[0].callback()
                raise ValueError('CDP disconnected')

    def test_success_at_or_after_deadline_still_fails(self):
        for elapsed in (30, 31):
            with self.subTest(elapsed=elapsed):
                phase, scope, process, cleanup, timers, now = self.phase()
                with self.assertRaises(scope['InstalledRecoveryReadinessTimeout']):
                    with phase(process, 420, 30, scope['InstalledRecoveryReadinessTimeout'], cleanup=cleanup):
                        now[0] = elapsed

    def test_total_cap_truncates_phase_without_granting_extra_time(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        now[0] = 410
        with self.assertRaises(scope['InstalledRecoveryTimeout']):
            with phase(process, 420, 60, scope['InstalledRecoveryShutdownTimeout'], cleanup=cleanup) as deadline:
                self.assertEqual(deadline, 420)
                timers[0].callback()
        self.assertEqual(timers[0].seconds, 10)

    def test_nonfinite_nonpositive_and_exhausted_budgets_do_not_start_timers(self):
        for end, seconds in ((float('nan'), 30), (float('inf'), 30), (420, 0), (420, -1), (420, float('inf')), (0, 30)):
            with self.subTest(end=end, seconds=seconds):
                phase, scope, process, cleanup, timers, now = self.phase()
                with self.assertRaises(RuntimeError):
                    with phase(process, end, seconds, scope['InstalledRecoveryApiTimeout'], cleanup=cleanup):
                        self.fail('invalid budget entered')
                self.assertFalse(timers)

    def test_still_running_callback_cannot_advance_to_another_phase(self):
        phase, scope, process, cleanup, timers, now = self.phase()
        with self.assertRaises(scope['InstalledRecoveryApiTimeout']):
            with phase(process, 420, 60, scope['InstalledRecoveryApiTimeout'], cleanup=cleanup):
                timers[0].is_alive.return_value = True

    def test_budget_contract_covers_actual_app_startup_shutdown_and_outer_cleanup(self):
        _, scope, _, _, _, _ = self.phase()
        self.assertEqual(scope['INSTALLED_PHASE_SECONDS'], {'debugger': 30, 'renderer': 210, 'preload': 15,
            'readiness': 30, 'api': 60, 'shutdown': 60})
        self.assertEqual(scope['INSTALLED_TIMEOUT_SECONDS'], sum(scope['INSTALLED_PHASE_SECONDS'].values()) + 15)
        app = (ROOT / 'desktop/src/main.ts').read_text(encoding='utf-8')
        self.assertIn('readyTimeoutMs: 180_000', app)
        self.assertIn('stopGraceMs: 45_000', app)
        self.assertIn('stopForceMs: 5_000', app)
        wrapper = (ROOT / 'ci/public_ci_verify_installed.ps1').read_text(encoding='utf-8')
        self.assertIn("'--timeout', '420'", wrapper)
        self.assertIn('-LogPath $recoveryStdout -TimeoutSeconds 540', wrapper)


if __name__ == '__main__':
    unittest.main()
