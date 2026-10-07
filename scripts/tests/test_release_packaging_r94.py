"""Fault injection for archive publication and isolated Core process checks."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import importlib.util
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import venv
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ci'))
from public_build_contract import (assert_public_build_chain, assert_ui_dependencies,
    assert_runtime_imports, assert_browser_prerequisites, read_sources)
# New Python processes and newly created venvs can cold-start slowly on Windows.
# This is a ceiling, not a sleep; explicit hang/shutdown tests keep short limits.
FIXTURE_STARTUP_TIMEOUT = 20.0


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive_tool = load('package_portable_archive')
core_tool = load('verify_frozen_core_service')


class PortableArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='聚鑫 构建 [19] (4)-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / '便携 APP'
        self.source.mkdir()
        (self.source / 'app.exe').write_bytes(b'MZ' * 4096)
        self.output = self.root / '便携.zip'

    def test_roundtrip_keeps_hidden_files_empty_dirs_unicode_and_old_dates(self):
        hidden = self.source / '.runtime'
        hidden.mkdir()
        file = hidden / '依赖.bin'
        file.write_bytes(bytes(range(256)) * 32)
        os.utime(file, (0, 0))
        (self.source / 'empty').mkdir()
        result = archive_tool.package_archive(self.source, self.output)
        self.assertTrue(result['verified'])
        self.assertEqual(2, result['files'])
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), result['sha256'])
        with zipfile.ZipFile(self.output) as zipped:
            self.assertIsNone(zipped.testzip())
            self.assertEqual(file.read_bytes(), zipped.read('便携 APP/.runtime/依赖.bin'))
            self.assertIn('便携 APP/empty/', zipped.namelist())

    def test_zip64_path_works_above_injected_legacy_limit(self):
        with patch.object(zipfile, 'ZIP64_LIMIT', 64):
            archive_tool.package_archive(self.source, self.output)
        self.assertIn(b'PK\x06\x06', self.output.read_bytes())
        with zipfile.ZipFile(self.output) as zipped:
            self.assertEqual(b'MZ' * 4096, zipped.read('便携 APP/app.exe'))

    def test_disk_full_does_not_publish_partial_output_or_overwrite_previous(self):
        self.output.write_bytes(b'previous')
        with patch.object(zipfile.ZipFile, 'open', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(OSError):
                archive_tool.package_archive(self.source, self.output)
        self.assertEqual(b'previous', self.output.read_bytes())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_verification_failure_cannot_publish(self):
        with patch.object(archive_tool, 'verify_archive', side_effect=RuntimeError('corruption')):
            with self.assertRaisesRegex(RuntimeError, 'corruption'):
                archive_tool.package_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_publish_permission_error_preserves_previous_archive(self):
        self.output.write_bytes(b'previous')
        with patch.object(os, 'replace', side_effect=PermissionError('archive is locked')):
            with self.assertRaises(PermissionError):
                archive_tool.package_archive(self.source, self.output)
        self.assertEqual(b'previous', self.output.read_bytes())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_locked_cleanup_cannot_mask_original_failure_or_cancel(self):
        original = Path.unlink
        for failure in (RuntimeError('original archive verification failure'),
                        OSError(28, 'original disk full'), KeyboardInterrupt('cancel packaging')):
            with self.subTest(failure=type(failure).__name__):
                self.output.write_bytes(b'previous')
                def locked(path, *args, **kwargs):
                    if path.suffix == '.tmp':raise PermissionError('temporary archive scanner lock')
                    return original(path, *args, **kwargs)
                with patch.object(archive_tool, 'verify_archive', side_effect=failure), patch.object(Path, 'unlink', locked):
                    with self.assertRaises(type(failure)) as caught:
                        archive_tool.package_archive(self.source, self.output)
                self.assertIs(failure, caught.exception)
                self.assertEqual(b'previous', self.output.read_bytes())
                for path in self.root.glob('*.tmp'):path.unlink()

    def test_source_mutation_cannot_publish(self):
        original = archive_tool.inventory
        def changed(root):
            result = original(root)
            (self.source / 'late.dll').write_text('changed')
            return result
        with patch.object(archive_tool, 'inventory', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'source changed'):
                archive_tool.package_archive(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_duplicate_and_wrong_content_are_rejected(self):
        for mode in ['missing', 'duplicate', 'changed']:
            with self.subTest(mode=mode):
                with zipfile.ZipFile(self.output, 'w') as zipped:
                    if mode != 'missing':
                        zipped.writestr('app.exe', b'wrong')
                    if mode == 'duplicate':
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore', UserWarning)
                            zipped.writestr('app.exe', b'right')
                with self.assertRaises(RuntimeError):
                    archive_tool.verify_archive(self.output, {'app.exe': hashlib.sha256(b'right').hexdigest()})

    def test_output_inside_source_and_empty_source_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            archive_tool.package_archive(self.source, self.source / 'result.zip')
        (self.source / 'app.exe').unlink()
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            archive_tool.package_archive(self.source, self.output)

    def test_windows_reparse_source_is_rejected(self):
        original = Path.lstat
        def reparse(path):
            result = original(path)
            if path == self.source / 'app.exe':
                from types import SimpleNamespace
                return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
            return result
        with patch.object(Path, 'lstat', reparse):
            with self.assertRaisesRegex(RuntimeError, 'reparse'):
                archive_tool.package_archive(self.source, self.output)


FAKE_CORE = '''
from contextlib import closing
import json, os, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
mode = sys.argv[1]
print('FAKE_CORE_IDENTITY=' + json.dumps(dict(pid=os.getpid(), prefix=sys.prefix)), flush=True)
assert not any(key in os.environ for key in ('IGAC_PARENT_PID', 'IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS', 'PYTHONPATH', 'PYTHONHOME'))
if mode == 'exit': raise SystemExit(23)
if mode == 'hang': time.sleep(60)
if mode != 'missing-db':
    with closing(sqlite3.connect(os.environ['IGAC_DB_PATH'])) as db, db:
        for name in ['app_users','tasks','task_targets','schema_migrations']:
            db.execute('CREATE TABLE ' + name + '(id TEXT)')
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, code, value):
        self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(value).encode())
    def do_GET(self):
        if self.headers.get('X-Startup-Token') != os.environ['IGAC_STARTUP_TOKEN'] and mode != 'no-auth':
            return self.reply(401, {})
        if self.path != '/api/health':
            return self.reply(200 if mode == 'posting-survives' else 401 if mode == 'posting-unauthorized' else 404, {})
        self.reply(200, dict(status='ready', source_revision='old' if mode=='old' else 'stability-r94', database='ok'))
    def do_POST(self):
        if self.path != '/api/internal/shutdown':
            return self.reply(200 if mode == 'posting-survives' else 401 if mode == 'posting-unauthorized' else 404, {})
        self.reply(200, dict(accepted=True))
        if mode != 'stuck-shutdown': threading.Thread(target=self.server.shutdown).start()
server = HTTPServer(('127.0.0.1', int(os.environ['IGAC_PORT'])), Handler)
server.serve_forever()
server.server_close()
'''


class FrozenCoreProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='core-probe-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / 'fake_core.py'
        self.fixture.write_text(FAKE_CORE, encoding='utf-8')

    def run_probe(self, mode, *, startup_timeout=None, **kwargs):
        startup_timeout = FIXTURE_STARTUP_TIMEOUT if startup_timeout is None else startup_timeout
        processes, original = [], subprocess.Popen
        primary = None
        def capture(*args, **options):
            process = original(*args, **options)
            processes.append(process)
            return process
        try:
            with patch.object(core_tool.subprocess, 'Popen', side_effect=capture):
                return core_tool.probe_core([sys.executable, str(self.fixture), mode],
                                           self.root / 'probe.log', timeout=startup_timeout, **kwargs)
        except BaseException as error:
            primary = error
            raise
        finally:
            log = self.root / 'probe.log'
            output = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
            if primary is not None:
                # Temporary fixture directories are removed by unittest cleanup.
                # Keep the actual child output in the permanent build-test log.
                primary.add_note('Core fixture startup budget: ' + str(startup_timeout) + ' seconds; child output:\n'
                                 + (output[-6000:] or '(child produced no startup output)'))
            try:
                self.assertEqual(1, len(processes))
                self.assertIsNotNone(processes[0].poll(), 'probe must reap its own child')
                records = [json.loads(line.split('=', 1)[1]) for line in output.splitlines()
                           if line.startswith('FAKE_CORE_IDENTITY=')]
                # A deadline can fire before Python reaches the identity marker.
                # Keep that timeout rather than masking it with "1 != 0".
                before_identity_timeout = primary is not None and 'Core startup timed out' in str(primary) and not records
                if not before_identity_timeout and (mode != 'hang' or records):
                    self.assertEqual(1, len(records), output)
                    self.assertEqual(processes[0].pid, records[0]['pid'], 'must own actual interpreter, not venv launcher')
                    self.assertEqual(os.path.normcase(sys.prefix), os.path.normcase(records[0]['prefix']))
            except Exception as ownership_error:
                if primary is not None:
                    raise BaseExceptionGroup('Core probe and child ownership validation failed',
                                             [primary, ownership_error]) from None
                raise

    def test_auth_database_and_orderly_shutdown_with_contaminated_parent_env(self):
        original = self.root / 'user-data'
        original.mkdir()
        (original / 'collector.sqlite3').write_bytes(b'user data must stay untouched')
        with patch.dict(os.environ, {'IGAC_DB_PATH': str(original / 'collector.sqlite3'),
                                   'IGAC_PARENT_PID': '1', 'PYTHONPATH': 'untrusted',
                                   'IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS': 'old'}):
            result = self.run_probe('ok')
        self.assertTrue(result['orderly_shutdown'])
        self.assertEqual(b'user data must stay untouched', (original / 'collector.sqlite3').read_bytes())

    def test_removed_endpoint_survival_or_authentication_only_refusal_fails(self):
        for mode in ('posting-survives', 'posting-unauthorized'):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, 'Removed posting endpoint'):
                self.run_probe(mode)

    def test_premature_exit_is_not_a_successful_model_only_probe(self):
        with self.assertRaisesRegex(RuntimeError, 'exit 23'):
            self.run_probe('exit')

    def test_old_revision_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'payload is invalid'):
            self.run_probe('old')

    def test_missing_auth_cannot_pass(self):
        with self.assertRaisesRegex(RuntimeError, 'without its startup token'):
            self.run_probe('no-auth')

    def test_health_without_real_database_cannot_pass(self):
        with self.assertRaisesRegex(RuntimeError, 'isolated database'):
            self.run_probe('missing-db')

    def test_acknowledged_but_stuck_shutdown_fails_and_reaps_child(self):
        with self.assertRaisesRegex(RuntimeError, 'did not exit'):
            self.run_probe('stuck-shutdown', shutdown_timeout=.2)

    def test_startup_hang_has_bounded_deadline(self):
        with self.assertRaisesRegex(RuntimeError, 'startup timed out'):
            self.run_probe('hang', startup_timeout=.2)

    def test_probe_closes_real_sqlite_before_windows_style_directory_cleanup(self):
        connections, original = [], sqlite3.connect
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connections.append(connection)
            return connection
        def cleanup(directory):
            for connection in connections:
                try:
                    connection.execute('SELECT 1')
                except sqlite3.ProgrammingError:
                    continue
                raise PermissionError(32, 'Windows-style cleanup: collector.sqlite3 is still open')
            return original_cleanup(directory)
        try:
            with patch.object(core_tool.sqlite3, 'connect', side_effect=connect), \
                    patch.object(core_tool.tempfile.TemporaryDirectory, 'cleanup', cleanup):
                self.assertTrue(self.run_probe('ok')['verified'])
            self.assertEqual(1, len(connections))
        finally:
            for connection in connections:
                connection.close()

    def test_probe_closes_sqlite_when_schema_validation_fails(self):
        connections, original = [], sqlite3.connect
        class BrokenSchema(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if 'sqlite_master' in sql:
                    raise sqlite3.DatabaseError('injected schema read failure')
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            connection = original(*args, factory=BrokenSchema, **kwargs)
            connections.append(connection)
            return connection
        try:
            with patch.object(core_tool.sqlite3, 'connect', side_effect=connect):
                with self.assertRaisesRegex(sqlite3.DatabaseError, 'injected schema read failure'):
                    self.run_probe('ok')
            self.assertEqual(1, len(connections))
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute('SELECT 1')
        finally:
            for connection in connections:
                connection.close()


class ProbeChildIdentityTests(unittest.TestCase):
    def test_windows_venv_uses_direct_interpreter_and_preserves_venv(self):
        environment = {'PATH': 'system'}
        with patch.object(core_tool, 'IS_WINDOWS', True, create=True), \
                patch.object(sys, 'executable', '/fixture/venv/python.exe'), \
                patch.object(sys, '_base_executable', '/fixture/base/python.exe'):
            command = core_tool.direct_child_command(['/fixture/venv/python.exe', 'fixture.py'], environment)
        self.assertEqual(['/fixture/base/python.exe', 'fixture.py'], command)
        self.assertEqual('/fixture/venv/python.exe', environment['__PYVENV_LAUNCHER__'])

    def test_frozen_executable_and_other_python_are_never_replaced(self):
        with patch.object(core_tool, 'IS_WINDOWS', True, create=True):
            for executable in ['/fixture/collector_core.exe', '/unrelated/python.exe']:
                environment = {}
                command = [executable, '--port', '12345']
                self.assertEqual(command, core_tool.direct_child_command(command, environment))
                self.assertNotIn('__PYVENV_LAUNCHER__', environment)

    def test_real_offline_venv_runs_all_probe_failure_and_release_cases(self):
        with tempfile.TemporaryDirectory(prefix='Juxin venv (20)-') as temporary:
            environment = Path(temporary) / 'venv'
            venv.EnvBuilder(with_pip=False).create(environment)
            executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            count = unittest.defaultTestLoader.loadTestsFromTestCase(FrozenCoreProbeTests).countTestCases()
            # The enclosing deadline must allow each bounded cold start plus
            # cleanup. Otherwise it kills the suite before it prints its cause.
            deadline = count * (FIXTURE_STARTUP_TIMEOUT + 10) + 30
            result = run_offline_suite(executable, deadline)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn(f'Ran {count} tests', result.stderr)


def run_offline_suite(executable, timeout):
    environment = os.environ.copy()
    environment.pop('__PYVENV_LAUNCHER__', None)
    command = [str(executable), '-I', '-X', 'utf8', str(Path(__file__).resolve()), 'FrozenCoreProbeTests', '-v']
    if core_tool.IS_WINDOWS:
        # As with direct_child_command, own the real interpreter instead of
        # killing only the Windows venv redirector on a subprocess timeout.
        command[0] = getattr(sys, '_base_executable', sys.executable)
        environment['__PYVENV_LAUNCHER__'] = str(executable)
    try:
        return subprocess.run(command, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        def text(value):
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
        raise AssertionError(f'Offline venv probe suite timed out after {timeout:g} seconds. Partial child output:\n'
                             + text(error.stdout)[-6000:] + '\n' + text(error.stderr)[-6000:]) from error


class ProbeCleanupTests(unittest.TestCase):
    def test_short_windows_share_lock_is_retried_then_removed(self):
        lock = PermissionError('temporary share lock')
        lock.winerror = 32
        directory = Mock()
        directory.cleanup.side_effect = [lock, lock, None]
        with patch.object(core_tool.time, 'sleep') as delay:
            core_tool.cleanup_probe_directory(directory)
        self.assertEqual(3, directory.cleanup.call_count)
        self.assertEqual([.05, .1], [call.args[0] for call in delay.call_args_list])

    def test_permanent_share_lock_fails_after_finite_retries(self):
        lock = PermissionError('still in use')
        lock.winerror = 32
        directory = Mock()
        directory.cleanup.side_effect = lock
        with patch.object(core_tool.time, 'sleep') as delay:
            with self.assertRaises(PermissionError) as raised:
                core_tool.cleanup_probe_directory(directory)
        self.assertIs(lock, raised.exception)
        self.assertEqual(6, directory.cleanup.call_count)
        self.assertLess(sum(call.args[0] for call in delay.call_args_list), 2)

    def test_non_share_permission_error_is_not_hidden_or_retried(self):
        directory = Mock()
        directory.cleanup.side_effect = PermissionError('no access')
        with patch.object(core_tool.time, 'sleep') as delay:
            with self.assertRaises(PermissionError):
                core_tool.cleanup_probe_directory(directory)
        self.assertEqual(1, directory.cleanup.call_count)
        delay.assert_not_called()

    def test_cleanup_failure_keeps_original_probe_error_and_still_fails(self):
        probe_error, cleanup_error = RuntimeError('missing authentication'), PermissionError('locked')
        directory = tempfile.TemporaryDirectory(prefix='probe-cleanup-fault-')
        try:
            with patch.object(core_tool.tempfile, 'TemporaryDirectory', return_value=directory), \
                    patch.object(core_tool, 'cleanup_probe_directory', side_effect=cleanup_error):
                with self.assertRaises(ExceptionGroup) as raised:
                    with core_tool.probe_directory():
                        raise probe_error
            self.assertEqual((probe_error, cleanup_error), raised.exception.exceptions)
        finally:
            directory.cleanup()

    def test_nonfinite_deadlines_cannot_create_unbounded_probe(self):
        for value in [float('nan'), float('inf'), 0, -1]:
            with self.subTest(value=value), patch.object(core_tool.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(ValueError, 'finite and positive'):
                    core_tool.probe_core(['unused'], Path('unused.log'), timeout=value)
                popen.assert_not_called()


class ColdStartRegressionTests(unittest.TestCase):
    def fixture_case(self):
        case = FrozenCoreProbeTests('test_auth_database_and_orderly_shutdown_with_contaminated_parent_env')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_healthy_cold_start_longer_than_four_seconds_still_checks_auth_and_shutdown(self):
        case = self.fixture_case()
        case.fixture.write_text('import time; time.sleep(4.5)\n' + FAKE_CORE, encoding='utf-8')
        self.assertTrue(case.run_probe('ok')['orderly_shutdown'])

    def test_timeout_before_identity_preserves_original_error_and_startup_output(self):
        case = self.fixture_case()
        case.fixture.write_text("print('COLD_START_STAGE=before-imports', flush=True)\nimport time; time.sleep(60)\n" + FAKE_CORE, encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'startup timed out') as raised:
            case.run_probe('ok', startup_timeout=0.5)
        self.assertIn('COLD_START_STAGE=before-imports', '\n'.join(raised.exception.__notes__))

    def test_ownership_failure_cannot_hide_or_turn_original_failure_into_success(self):
        case = self.fixture_case()
        original = RuntimeError('Core startup timed out')
        with patch.object(core_tool, 'probe_core', side_effect=original), \
                self.assertRaises(ExceptionGroup) as caught:
            case.run_probe('ok', startup_timeout=0.5)
        self.assertIs(original, caught.exception.exceptions[0])
        self.assertIsInstance(caught.exception.exceptions[1], AssertionError)

    def test_outer_suite_timeout_keeps_partial_output(self):
        failure = subprocess.TimeoutExpired('fixture', 1, output=b'fixture stdout', stderr=b'original Core startup timed out')
        with patch.object(subprocess, 'run', side_effect=failure), \
                self.assertRaisesRegex(AssertionError, 'original Core startup timed out') as caught:
            run_offline_suite(Path('fixture-python'), 1)
        self.assertIn('fixture stdout', str(caught.exception))

    def test_windows_outer_suite_owns_base_interpreter_and_new_venv(self):
        # Path renders separators for the host OS. Compare the actual path
        # contract, and exercise Windows spellings even on a POSIX test host.
        executables = [
            Path('/fixture/new-venv/Scripts/python.exe'),
            PurePosixPath('/fixture/new-venv/Scripts/python.exe'),
            PureWindowsPath('/fixture/new-venv/Scripts/python.exe'),
            PureWindowsPath('D:/IG实验室/新建文件夹 (3)/.venv/Scripts/python.exe'),
            PureWindowsPath('//build-server/共享目录/IG (3)/.venv/Scripts/python.exe'),
        ]
        for executable in executables:
            with self.subTest(executable=str(executable)), \
                    patch.object(core_tool, 'IS_WINDOWS', True), \
                    patch.object(sys, '_base_executable', '/fixture/base/python.exe'), \
                    patch.object(subprocess, 'run', return_value=Mock()) as launch:
                run_offline_suite(executable, 30)
            self.assertEqual('/fixture/base/python.exe', launch.call_args.args[0][0])
            self.assertEqual(str(executable), launch.call_args.kwargs['env']['__PYVENV_LAUNCHER__'])
            self.assertEqual(30, launch.call_args.kwargs['timeout'])


class ReleaseWiringTests(unittest.TestCase):
    def test_release_installs_ui_fixture_dependencies_before_backend_discovery(self):
        sources = read_sources(ROOT)
        assert_public_build_chain(sources['workflow'], sources['wrapper'], sources['common'])
        assert_ui_dependencies(sources['wrapper'], sources['build'], sources['install'])
        self.assertIn("from 'esbuild'", (ROOT / 'renderer/tests/build-nurture-r41-fixture.mjs').read_text())

    def test_public_and_local_freezes_keep_runtime_imports(self):
        sources = read_sources(ROOT)
        # Public verification invokes the original freeze. Its output feeds
        # both packagers; there are no separate inline CI freeze jobs.
        assert_public_build_chain(sources['workflow'], sources['wrapper'], sources['common'])
        assert_runtime_imports(sources['build'])

    def test_release_job_prepares_browser_before_backend_tests(self):
        sources = read_sources(ROOT)
        assert_public_build_chain(sources['workflow'], sources['wrapper'], sources['common'])
        assert_browser_prerequisites(sources['early'], sources['build'])

    def test_frozen_service_and_verified_archive_are_required_before_publication(self):
        source = (ROOT / 'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(source.index('verify_frozen_core_service.py'), source.index('$InstallerSucceeded = $false'))
        portable = (ROOT / 'scripts/build_portable_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('package_portable_archive.py', portable)
        self.assertNotIn('Compress-Archive ', portable)
        self.assertIn('Enter-IgacBuildMutex', portable)
        self.assertIn('Exit-IgacBuildMutex', portable)

    def test_collection_completion_gate_runs_once_before_desktop_compilation(self):
        source = (ROOT / 'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(1, source.count('"test_*r44.py"'))
        self.assertLess(source.index('$NativeBrowserExitCode -ne 0'),
                        source.index('$CollectionCompletionR44ExitCode ='))
        self.assertLess(source.index('$CollectionCompletionR44ExitCode -ne 0'),
                        source.index('$DesktopBuildExitCode ='))


if __name__ == '__main__':
    unittest.main()
