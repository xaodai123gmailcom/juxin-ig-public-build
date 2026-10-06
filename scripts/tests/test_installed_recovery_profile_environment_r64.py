"""Pure installed-launch contract tests; no application imports or processes.

Execute selected real function ASTs with mocked process/browser dependencies.
The Windows privacy gate and actual installed acceptance remain separate.
"""
import ast
from contextlib import nullcontext
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    namespace = dict(os=os, Path=Path, json=json, math=math, require=require,
                     sys=SimpleNamespace(platform='win32'), **dependencies)
    nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in names})


class InstalledProfileEnvironmentTests(unittest.TestCase):
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

    def run_launch(self, profile, *, session_ready=True, exercise_error=None):
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
                                     STDOUT=subprocess.STDOUT)
            page = Mock()
            page.evaluate.return_value = {'ok': False, 'error':
                'Missing application session token' if session_ready else 'unexpected authenticated state'}
            browser = Mock(); playwright = Mock()
            playwright.chromium.connect_over_cdp.return_value = browser
            fake_playwright = SimpleNamespace(sync_playwright=lambda: nullcontext(playwright))
            event = Mock(); event.is_set.return_value = False
            watchdog = Mock()
            threads = SimpleNamespace(Event=lambda: event, Timer=Mock(return_value=watchdog))
            clock = SimpleNamespace(perf_counter_ns=Mock(side_effect=[2, 3]),
                monotonic=Mock(side_effect=[0, 0, 0, 121] if not session_ready else lambda: 0),
                sleep=Mock())
            runtime = {'pid': 101, 'parent_pid': process.pid, 'executable': str(core), 'sha256': digest(core)}
            observed_core = Mock(return_value=runtime)
            cleanup = Mock()
            exercise = Mock(return_value={'checks': {}}, side_effect=exercise_error)
            binding = {'source_commit': 'd' * 40, 'source_provenance': {'schema': 1}}
            validate = Mock(side_effect=lambda proof, **kwargs: proof)
            probe = functions('environment', 'probe_installed', fixture=fixture,
                core_probe=SimpleNamespace(probe_directory=lambda **kwargs: nullcontext(root)),
                source_binding=Mock(return_value=binding), reserve_port=Mock(side_effect=[31001, 31002, 31003]),
                subprocess=native, threading=threads, time=clock, exact_renderer=Mock(return_value=page),
                installed_core_process=observed_core, exercise=exercise,
                stop_owned_runtime=cleanup, validate_proof=validate)
            with patch.dict(os.environ, {**profile, **POISON}, clear=True), \
                    patch.dict(sys.modules, {'playwright': SimpleNamespace(), 'playwright.sync_api': fake_playwright}):
                if exercise_error or not session_ready:
                    with self.assertRaisesRegex(RuntimeError, str(exercise_error) if exercise_error else 'did not become ready'):
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

    def test_missing_unauthenticated_session_guard_fails_and_cleans_up(self):
        self.run_launch(PROFILE, session_ready=False)

    def test_api_failure_still_reaps_owned_runtime_and_cancels_watchdog(self):
        self.run_launch(PROFILE, exercise_error=RuntimeError('mock API refusal'))

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


if __name__ == '__main__':
    unittest.main()
