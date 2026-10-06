"""Offline build retry regressions: actual pip files and browser staging."""
from pathlib import Path
import json
import io
import locale
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import install_python_dependencies as dependencies
import prune_browser_runtime as browsers
sys.path.insert(0, str(ROOT / 'ci'))
from public_build_contract import assert_public_build_chain, assert_recovery_gate, read_sources


def run_fixture_command(command, *, timeout=45, **kwargs):
    # Windows drains subprocess pipes on reader threads. Decoding there can
    # terminate a thread and leave stdout=None, hiding the real exit status.
    # Receive bytes first; Python children use -X utf8, but cmd/native tools
    # can still print using the machine's ANSI code page.
    result = subprocess.run(command, capture_output=True, timeout=timeout, **kwargs)
    def decode(value):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return value.decode(locale.getencoding(), errors='backslashreplace')
    result.stdout = decode(result.stdout)
    result.stderr = decode(result.stderr)
    return result


def load_browser_fixture(root, browsers_root):
    # Restore only our Playwright entry. patch.dict(sys.modules) restores the
    # whole cache, removing newly imported urllib modules while functions keep
    # references to them; patching the reimported name then misses real I/O.
    missing = object()
    previous = sys.modules.get('playwright', missing)
    try:
        sys.modules['playwright'] = SimpleNamespace(__file__=str(root / 'playwright/__init__.py'))
        scope = runpy.run_path(str(ROOT / 'scripts/install_native_browser.py'))
    finally:
        if previous is missing:
            sys.modules.pop('playwright', None)
        else:
            sys.modules['playwright'] = previous
    main = scope['main']
    main.__globals__['os'] = SimpleNamespace(name='nt', environ={'PLAYWRIGHT_BROWSERS_PATH': str(browsers_root)})
    main.__globals__['platform'] = SimpleNamespace(machine=lambda: 'AMD64')
    return main


class RealNpmEnvironmentTests(unittest.TestCase):
    def test_inherited_npm_settings_cannot_skip_runtime_installation(self):
        npm = shutil.which('npm')
        self.assertIsNotNone(npm)
        source = (ROOT / 'scripts/install_windows.ps1').read_text(encoding='utf-8-sig')
        commands = [line.strip().split()[1:] for line in source.splitlines()
                    if re.match(r'^\s*npm (ci|install)(?:\s|$)', line)]
        self.assertEqual(2, len(commands))
        env = {k: v for k, v in os.environ.items() if not k.lower().startswith('npm_')}
        env.update(NODE_ENV='production', NPM_CONFIG_OMIT='optional',
                   NPM_CONFIG_IGNORE_SCRIPTS='true', NPM_CONFIG_BIN_LINKS='false')
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory(prefix='Juxin npm [29] ') as folder:
                root = Path(folder)
                for name in ('dev-tool', 'native-runtime'):
                    package = root / name
                    package.mkdir()
                    (package / 'package.json').write_text(json.dumps({'name': name, 'version': '1.0.0',
                        'bin': {name: 'tool.js'}}))
                    (package / 'tool.js').write_text('#!/usr/bin/env node\n')
                (root / 'package.json').write_text(json.dumps({'name': 'build-fixture', 'version': '1.0.0',
                    'devDependencies': {'dev-tool': 'file:dev-tool'},
                    'optionalDependencies': {'native-runtime': 'file:native-runtime'},
                    'scripts': {'postinstall': 'node install.cjs'}}))
                (root / 'install.cjs').write_text("require('fs').writeFileSync('runtime-installed', 'ok');")
                def run(args):
                    result = run_fixture_command([npm, *args, '--offline', '--no-audit', '--no-fund'],
                        cwd=root, env=env, shell=os.name == 'nt', timeout=40)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                run(['install', '--package-lock-only', '--ignore-scripts'])
                run(command)
                self.assertTrue((root / 'runtime-installed').is_file(), 'npm succeeded but skipped runtime installation')
                for name in ('dev-tool', 'native-runtime'):
                    self.assertTrue((root / 'node_modules' / name / 'package.json').is_file(), name)
                    suffix = '.cmd' if os.name == 'nt' else ''
                    self.assertTrue((root / 'node_modules/.bin' / (name + suffix)).is_file(), name)


class RealDependencyRepairTests(unittest.TestCase):
    def test_matching_metadata_with_missing_module_is_reinstalled_once(self):
        with tempfile.TemporaryDirectory(prefix='Juxin 修复 [29] ') as folder:
            root = Path(folder)
            environment = root / '.venv'
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            (root / 'backend').mkdir()
            (root / 'backend/requirements.txt').write_text('igac-build-repair-fixture==1.0\n')
            index = root / 'index'
            package = index / 'igac-build-repair-fixture'
            package.mkdir(parents=True)
            wheel = package / 'igac_build_repair_fixture-1.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('igac_build_repair_fixture.py', 'VALUE = 29\n')
                info = 'igac_build_repair_fixture-1.0.dist-info/'
                archive.writestr(info + 'METADATA', 'Metadata-Version: 2.1\nName: igac-build-repair-fixture\nVersion: 1.0\n')
                archive.writestr(info + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr(info + 'RECORD', '')
            (package / 'index.html').write_text('<a href="' + wheel.name + '">wheel</a>')
            env = dependencies.official_environment(os.environ)
            env['PIP_INDEX_URL'] = index.as_uri()

            def actual(command, **kwargs):
                self.assertEqual(dependencies.ISOLATED, command[1:4], 'Every isolated Python child must enable UTF-8 explicitly')
                result = run_fixture_command(command, timeout=45, **kwargs)
                return result

            installed = actual([str(python), *dependencies.ISOLATED, '-m', 'pip', 'install', '--no-deps', str(wheel)], env=env)
            self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
            damaged = actual([str(python), *dependencies.ISOLATED, '-c',
                "import pathlib,igac_build_repair_fixture;pathlib.Path(igac_build_repair_fixture.__file__).unlink()"], env=env)
            self.assertEqual(0, damaged.returncode, damaged.stderr)
            # Dist-info remains valid: this is the interrupted/removed-file case.
            metadata = actual([str(python), *dependencies.ISOLATED, '-c',
                "import importlib.metadata;assert importlib.metadata.version('igac-build-repair-fixture')=='1.0'"], env=env)
            self.assertEqual(0, metadata.returncode, metadata.stderr)
            calls, logs = [], []
            def runner(command, **kwargs):
                calls.append(list(command))
                if '--verify-only' in command:
                    # Test our small wheel, never impersonate an actual native dependency.
                    command = [str(python), *dependencies.ISOLATED, '-c',
                        "import igac_build_repair_fixture;assert igac_build_repair_fixture.VALUE==29"]
                result = actual(command, **kwargs)
                logs.append(result.stdout + result.stderr)
                return result
            with patch.object(sys, 'executable', str(python)), patch.object(dependencies, 'OFFICIAL_INDEX', index.as_uri()):
                result = dependencies.install_dependencies(root, runner=runner, environ=env, output=lambda *a, **kw: None)
            self.assertEqual(0, result, '\n'.join(logs))
            installs = [c for c in calls if 'install' in c]
            self.assertEqual(2, len(installs))
            self.assertNotIn('--force-reinstall', installs[0])
            self.assertIn('--force-reinstall', installs[1])
            self.assertEqual('igac-build-repair-fixture==1.0\n', (root / 'backend/requirements.txt').read_text())


class BrowserStagingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Juxin 浏览器 [29] ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'build' / 'browsers'
        self.root.mkdir(parents=True)
        self.registry = {'browsers': [{'name': 'chromium', 'revision': '123'}]}
        self.current = self.root / 'chromium-123'
        self.current.mkdir()
        (self.current / 'chrome.exe').write_bytes(b'fixture')

    def test_interrupted_legacy_download_no_longer_blocks_valid_runtime(self):
        stale = self.root / 'juxin-browser-download-abc_1234'
        stale.mkdir()
        (stale / 'chrome.zip').write_bytes(b'partial-download')
        removed = browsers.prune(self.root, self.registry)
        self.assertIn(stale.name, removed)
        self.assertFalse(stale.exists())
        self.assertEqual(b'fixture', (self.current / 'chrome.exe').read_bytes())
        preserved = list(self.root.parent.rglob('chrome.zip'))
        self.assertEqual(1, len(preserved))
        self.assertEqual(b'partial-download', preserved[0].read_bytes())
        self.assertFalse(preserved[0].is_relative_to(self.root))
        self.assertEqual([], browsers.prune(self.root, self.registry))

    def test_kept_revision_reparse_is_rejected_before_packaging(self):
        original = Path.lstat
        from types import SimpleNamespace
        def info(path):
            result = original(path)
            if path == self.current:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
            return result
        with patch.object(Path, 'lstat', info):
            with self.assertRaisesRegex(RuntimeError, 'reparse|link'):
                browsers.prune(self.root, self.registry)

    def test_unknown_content_does_not_trigger_partial_cleanup(self):
        old = self.root / 'chromium-122'
        old.mkdir()
        user = self.root / 'operator-notes.txt'
        user.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
            browsers.prune(self.root, self.registry)
        self.assertTrue(old.is_dir())
        self.assertEqual('keep', user.read_text())

    def test_old_runtime_removed_but_external_symlink_target_is_preserved(self):
        external = Path(self.temp.name) / 'external'
        external.mkdir()
        (external / 'keep').write_bytes(b'untouched')
        old = self.root / 'chromium-122'
        try:
            old.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('OS does not permit symlink creation')
        self.assertEqual([old.name], browsers.prune(self.root, self.registry))
        self.assertEqual(b'untouched', (external / 'keep').read_bytes())

    def test_failed_recovery_move_preserves_partial_download_and_current_browser(self):
        stale = self.root / 'juxin-browser-download-abc_1234'
        stale.mkdir()
        (stale / 'chrome.zip').write_bytes(b'partial')
        with patch.object(Path, 'rename', side_effect=PermissionError('scanner lock')):
            with self.assertRaises(PermissionError):
                browsers.prune(self.root, self.registry)
        self.assertEqual(b'partial', (stale / 'chrome.zip').read_bytes())
        self.assertEqual(b'fixture', (self.current / 'chrome.exe').read_bytes())


class BrowserDownloadPlacementTests(unittest.TestCase):
    def test_download_is_verified_outside_packaged_browser_tree(self):
        with tempfile.TemporaryDirectory(prefix='Juxin 下载 [29] ') as folder:
            root = Path(folder)
            browsers_root = root / 'build/browsers'
            registry = root / 'playwright/driver/package'
            registry.mkdir(parents=True)
            (registry / 'browsers.json').write_text(json.dumps({'browsers': [
                {'name': 'chromium', 'revision': '123', 'browserVersion': '1.2.3.4'}]}))
            main = load_browser_fixture(root, browsers_root)
            content = io.BytesIO()
            with zipfile.ZipFile(content, 'w') as archive:
                archive.writestr('chrome-win64/chrome.exe', b'fixture-browser')
                for name in ('chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
                    archive.writestr('chrome-win64/' + name, b'fixture-resource')
            original = tempfile.mkdtemp
            created = []
            def temporary(*args, **kwargs):
                directory = original(*args, **kwargs)
                created.append(Path(directory))
                return directory
            with patch.object(main.__globals__['urllib'].request, 'urlopen', return_value=io.BytesIO(content.getvalue())) as download, \
                    patch.object(tempfile, 'mkdtemp', side_effect=temporary):
                main()
            self.assertEqual(1, len(created))
            self.assertFalse(created[0].is_relative_to(browsers_root))
            self.assertFalse(created[0].exists())
            download.assert_called_once()
            self.assertEqual('https://storage.googleapis.com/chrome-for-testing-public/1.2.3.4/win64/chrome-win64.zip', download.call_args.args[0])
            self.assertEqual(b'fixture-browser', (browsers_root / 'chromium-123/chrome-win64/chrome.exe').read_bytes())
            self.assertTrue((browsers_root / 'chromium-123/INSTALLATION_COMPLETE').is_file())


class BuildRecoveryWiringTests(unittest.TestCase):
    def test_local_and_ci_builds_require_recovery_regressions(self):
        sources = read_sources(ROOT)
        # Public CI delegates to the same mandatory local build gate; the
        # retired private workflow no longer embeds a second discovery call.
        assert_public_build_chain(sources['workflow'], sources['wrapper'], sources['common'])
        assert_recovery_gate(sources['build'])

    def test_frozen_gate_does_not_interpret_brackets_in_paths_as_wildcards(self):
        source = (ROOT / 'scripts/test_frozen_openvino.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('(Resolve-Path -LiteralPath $Executable).Path', source)
        self.assertIn('Test-Path -LiteralPath $WorkingDirectory', source)
        self.assertIn('Remove-Item -LiteralPath $WorkingDirectory', source)


class BrowserPublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='Juxin 发布 [30] ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.browsers = self.root / 'build/browsers'
        self.destination = self.browsers / 'chromium-123'
        self.destination.mkdir(parents=True)
        (self.destination / 'old-runtime').write_bytes(b'previous usable runtime')
        registry = self.root / 'playwright/driver/package'
        registry.mkdir(parents=True)
        (registry / 'browsers.json').write_text(json.dumps({'browsers': [
            {'name': 'chromium', 'revision': '123', 'browserVersion': '1.2.3.4'}]}))
        self.main = load_browser_fixture(self.root, self.browsers)

    def run_download(self, *, executable=b'new verified runtime', cached=False):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('chrome-win64/chrome.exe', executable)
            for name in ('chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
                archive.writestr('chrome-win64/' + name, b'fixture-resource')
        with patch.object(self.main.__globals__['urllib'].request, 'urlopen', return_value=io.BytesIO(content.getvalue())) as download:
            self.main()
            if cached:
                download.assert_not_called()
            else:
                download.assert_called_once()

    def test_failed_publication_restores_previous_runtime(self):
        original = Path.rename
        def rename(path, target):
            if path.name == 'extracted':
                raise PermissionError('injected publication lock')
            return original(path, target)
        with patch.object(Path, 'rename', rename), patch.object(shutil, 'move', side_effect=PermissionError('injected publication lock')):
            with self.assertRaisesRegex(PermissionError, 'publication lock'):
                self.run_download()
        self.assertEqual(b'previous usable runtime', (self.destination / 'old-runtime').read_bytes())

    def test_failed_completion_marker_cannot_remove_previous_runtime(self):
        original = Path.touch
        def touch(path, *args, **kwargs):
            if path.name == 'INSTALLATION_COMPLETE':
                raise OSError('injected marker disk failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'touch', touch):
            with self.assertRaisesRegex(OSError, 'marker disk failure'):
                self.run_download()
        self.assertEqual(b'previous usable runtime', (self.destination / 'old-runtime').read_bytes())

    def test_empty_executable_cannot_replace_previous_runtime(self):
        with self.assertRaisesRegex(RuntimeError, 'executable'):
            self.run_download(executable=b'')
        self.assertEqual(b'previous usable runtime', (self.destination / 'old-runtime').read_bytes())

    def test_success_replaces_runtime_and_seals_before_publication(self):
        original = Path.rename
        published = []
        def rename(path, target):
            if path.name == 'extracted':
                self.assertTrue((path / 'INSTALLATION_COMPLETE').is_file())
                published.append(str(target))
            return original(path, target)
        with patch.object(Path, 'rename', rename):
            self.run_download()
        self.assertEqual([str(self.destination)], published)
        self.assertFalse((self.destination / 'old-runtime').exists())
        self.assertEqual(b'new verified runtime', (self.destination / 'chrome-win64/chrome.exe').read_bytes())
        self.assertTrue((self.destination / 'INSTALLATION_COMPLETE').is_file())

    def test_failed_backup_rename_leaves_previous_runtime_untouched(self):
        original = Path.rename
        def rename(path, target):
            if path == self.destination:
                raise PermissionError('old runtime locked')
            return original(path, target)
        with patch.object(Path, 'rename', rename):
            with self.assertRaisesRegex(PermissionError, 'old runtime locked'):
                self.run_download()
        self.assertEqual(b'previous usable runtime', (self.destination / 'old-runtime').read_bytes())

    def test_rollback_failure_preserves_backup_outside_packaged_tree(self):
        original = Path.rename
        def rename(path, target):
            if path.name == 'extracted' or path.name.startswith('juxin-browser-previous-'):
                raise PermissionError('injected publication and rollback lock')
            return original(path, target)
        with patch.object(Path, 'rename', rename):
            with self.assertRaisesRegex(RuntimeError, 'previous runtime preserved'):
                self.run_download()
        previous = list(self.browsers.parent.glob('juxin-browser-previous-*/old-runtime'))
        self.assertEqual(1, len(previous))
        self.assertEqual(b'previous usable runtime', previous[0].read_bytes())
        self.assertFalse(self.destination.exists())
        # A subsequent build can publish successfully without including backup.
        self.run_download(cached=True)
        self.assertEqual(b'new verified runtime', (self.destination / 'chrome-win64/chrome.exe').read_bytes())
        self.assertEqual(b'previous usable runtime', previous[0].read_bytes())

    def test_obsolete_backup_cleanup_lock_does_not_invalidate_new_runtime(self):
        original = shutil.rmtree
        def cleanup(path, *args, **kwargs):
            if Path(path).name.startswith('juxin-browser-previous-'):
                raise PermissionError('old backup scanner lock')
            return original(path, *args, **kwargs)
        with patch.object(shutil, 'rmtree', cleanup):
            self.run_download()
        self.assertEqual(b'new verified runtime', (self.destination / 'chrome-win64/chrome.exe').read_bytes())
        self.assertTrue((self.destination / 'INSTALLATION_COMPLETE').is_file())
        self.assertEqual(1, len(list(self.browsers.parent.glob('juxin-browser-previous-*/old-runtime'))))

    def test_reparse_destination_rejected_before_download_or_mutation(self):
        original = Path.lstat
        def info(path):
            result = original(path)
            if path == self.destination:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
            return result
        with patch.object(Path, 'lstat', info), patch.object(self.main.__globals__['urllib'].request, 'urlopen') as download:
            with self.assertRaisesRegex(RuntimeError, 'reparse'):
                self.main()
            download.assert_not_called()
        self.assertEqual(b'previous usable runtime', (self.destination / 'old-runtime').read_bytes())


class BuildFixtureCompatibilityTests(unittest.TestCase):
    def test_gbk_output_and_original_failure_code_are_retained(self):
        code = "import sys;sys.stdout.buffer.write('安装完成，中文路径'.encode('gbk'));sys.stderr.buffer.write('原始错误'.encode('gbk'));sys.exit(17)"
        with patch.object(locale, 'getencoding', return_value='gbk'):
            result = run_fixture_command([sys.executable, *dependencies.ISOLATED, '-c', code])
        self.assertEqual(17, result.returncode)
        self.assertEqual('安装完成，中文路径', result.stdout)
        self.assertEqual('原始错误', result.stderr)

    def test_invalid_bytes_preserve_diagnostics_without_decoding_thread_failure(self):
        code = "import sys;sys.stdout.buffer.write(b'out\\xff');sys.stderr.buffer.write(b'err\\xfe');sys.exit(23)"
        with patch.object(locale, 'getencoding', return_value='utf-8'):
            result = run_fixture_command([sys.executable, *dependencies.ISOLATED, '-c', code])
        self.assertEqual(23, result.returncode)
        self.assertEqual('out\\xff', result.stdout)
        self.assertEqual('err\\xfe', result.stderr)

    def test_utf8_output_is_not_misdecoded_using_windows_ansi_fallback(self):
        code = "import sys;print('中文路径');print('中文错误',file=sys.stderr)"
        with patch.object(locale, 'getencoding', return_value='cp1252'):
            result = run_fixture_command([sys.executable, *dependencies.ISOLATED, '-c', code])
        self.assertEqual(0, result.returncode)
        self.assertEqual('中文路径', result.stdout.strip())
        self.assertEqual('中文错误', result.stderr.strip())

    def test_cold_browser_imports_run_offline_in_an_isolated_python_process(self):
        from verify_frozen_core_service import direct_child_command
        code = '''import sys, unittest
sys.path.insert(0, TEST_PATH)
import test_build_recovery_r94
# Simulate Windows/Python versions that have not preloaded urllib during
# unittest/dependency imports. Never let a missing mock contact a server.
for name in list(sys.modules):
    if name == 'urllib' or name.startswith('urllib.'):
        sys.modules.pop(name)
events = []
def forbid_network(event, args):
    if event in {'urllib.Request', 'socket.connect', 'socket.getaddrinfo'}:
        events.append(event)
        raise RuntimeError('Unexpected real network in offline build regression: ' + event)
sys.addaudithook(forbid_network)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'test_build_recovery_r94.BrowserDownloadPlacementTests',
    'test_build_recovery_r94.BrowserPublicationRecoveryTests',
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert not events, events
if not result.wasSuccessful(): raise SystemExit(1)
print('COLD_BROWSER_FIXTURE_OFFLINE=PASS cases=' + str(result.testsRun))
'''.replace('TEST_PATH', repr(str(ROOT / 'scripts/tests')))
        env = dict(os.environ)
        command = direct_child_command([sys.executable, *dependencies.ISOLATED, '-c', code], env)
        result = run_fixture_command(command, env=env, cwd=ROOT.parent, timeout=45)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('COLD_BROWSER_FIXTURE_OFFLINE=PASS cases=9', result.stdout)


if __name__ == '__main__':
    unittest.main()
