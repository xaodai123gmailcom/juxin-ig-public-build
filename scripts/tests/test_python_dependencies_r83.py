"""Offline regression checks for dependency-source recovery and failure gates."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install_python_dependencies as subject


class DependencyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Juxin 依赖 (83) ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backend').mkdir()
        self.requirements = self.root / 'backend/requirements.txt'
        self.requirements.write_text('Pillow==12.3.0\n', encoding='utf-8')
        self.logs, self.calls = [], []

    def run_sequence(self, codes, env=None):
        results = iter(codes)
        before = hashlib.sha256(self.requirements.read_bytes()).hexdigest()

        def runner(command, **kwargs):
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, next(results))

        result = subject.install_dependencies(self.root, runner=runner, environ=env or {},
                                               output=lambda text, **_: self.logs.append(text))
        self.assertEqual(hashlib.sha256(self.requirements.read_bytes()).hexdigest(), before)
        return result

    def test_success_requires_install_verification_and_pip_check_without_retry(self):
        self.assertEqual(self.run_sequence([0, 0, 0]), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('--force-reinstall', self.calls[0][0])
        self.assertIn('--verify-only', self.calls[1][0])
        self.assertEqual(self.calls[2][0][-1], 'check')
        self.assertIn('PYTHON_DEPENDENCIES_CHECK=PASS', self.logs)

    def test_missing_mirror_package_retries_same_pins_with_one_clean_official_source(self):
        env = {'PIP_INDEX_URL': 'https://user:secret@mirror.invalid/simple',
               'PIP_EXTRA_INDEX_URL': 'https://old.invalid/simple', 'PIP_NO_INDEX': '1',
               'PIP_FIND_LINKS': 'old-cache', 'PIP_NO_BINARY': ':all:',
               'PIP_PLATFORM': 'win32', 'PIP_PYTHON_VERSION': '310',
               'PIP_TARGET': 'elsewhere', 'PIP_CONSTRAINT': 'old.txt',
               'HTTPS_PROXY': 'http://proxy.invalid:80', 'REQUESTS_CA_BUNDLE': 'ca.pem'}
        original = dict(env)
        self.assertEqual(self.run_sequence([1, 0, 0, 0], env), 0)
        self.assertEqual(env, original)
        first, fallback = self.calls[:2]
        self.assertEqual(first[1]['env'], original)
        self.assertEqual(fallback[1]['env'], {'HTTPS_PROXY': env['HTTPS_PROXY'],
                         'REQUESTS_CA_BUNDLE': 'ca.pem', 'PIP_CONFIG_FILE': os.devnull})
        self.assertEqual(fallback[0][-3:], ['--index-url', subject.OFFICIAL_INDEX, '--no-cache-dir'])
        for command, kwargs in self.calls:
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[1:4], subject.ISOLATED)
            self.assertIs(kwargs['check'], False)
        self.assertIn('--only-binary=Pillow', fallback[0])
        self.assertNotIn('--force-reinstall', fallback[0])
        self.assertNotIn('--trusted-host', fallback[0])
        self.assertNotIn('--extra-index-url', fallback[0])
        self.assertNotIn('secret', '\n'.join(self.logs))
        self.assertNotIn('mirror.invalid', '\n'.join(self.logs))

    def test_both_failed_attempts_stop_without_success_or_verification(self):
        self.assertEqual(self.run_sequence([1, 1]), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('PYTHON_DEPENDENCIES_CHECK=PASS', self.logs)
        self.assertIn('PYTHON_DEPENDENCIES_CHECK=FAILED', self.logs)

    def test_pip_zero_exit_with_wrong_or_unloadable_dependency_is_not_success(self):
        self.assertEqual(self.run_sequence([0, 1, 0, 1]), 1)
        self.assertEqual(sum('--verify-only' in c for c, _ in self.calls), 2)
        self.assertIn('--force-reinstall', self.calls[2][0])
        self.assertNotIn('PYTHON_DEPENDENCIES_CHECK=PASS', self.logs)

    def test_conflicting_dependencies_after_install_still_fail(self):
        self.assertEqual(self.run_sequence([0, 0, 1, 0, 0, 1]), 1)
        self.assertEqual(sum(c[-1] == 'check' for c, _ in self.calls), 2)
        self.assertIn('--force-reinstall', self.calls[3][0])
        self.assertNotIn('PYTHON_DEPENDENCIES_CHECK=PASS', self.logs)

    def test_cancellation_does_not_start_another_install(self):
        def runner(command, **kwargs):
            self.calls.append(command)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            subject.install_dependencies(self.root, runner=runner, environ={}, output=lambda *a, **kw: None)
        self.assertEqual(len(self.calls), 1)

    def test_global_or_another_project_python_is_rejected_before_install(self):
        run = subprocess.run([sys.executable, *subject.ISOLATED, subject.__file__,
                              '--project-root', str(self.root)], capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('project\'s .venv Python', run.stdout)
        self.assertNotIn('PYTHON_DEPENDENCIES_STAGE=', run.stdout)

    def test_declared_pillow_pin_stays_identical_in_both_project_manifests(self):
        import tomllib
        project = Path(__file__).resolve().parents[2]
        req = (project / 'backend/requirements.txt').read_text().splitlines()
        pyproject = tomllib.loads((project / 'backend/pyproject.toml').read_text())
        self.assertIn('Pillow==12.3.0', req)
        self.assertIn('Pillow==12.3.0', pyproject['project']['dependencies'])


class RealPipConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='Juxin pip 源 (83) ')
        cls.root = Path(cls.temp.name)
        cls.venv = cls.root / '.venv'
        venv.EnvBuilder(with_pip=True).create(cls.venv)
        cls.python = cls.venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_pip_ignores_stale_site_config_and_environment_only_for_fallback(self):
        config = self.venv / ('pip.ini' if os.name == 'nt' else 'pip.conf')
        config.write_text('[global]\nno-index = true\nextra-index-url = https://stale.invalid/simple\n'
                          'find-links = old-cache\n[install]\nno-binary = :all:\n', encoding='utf-8')
        self.addCleanup(config.unlink)
        env = dict(os.environ, PIP_NO_INDEX='1', PIP_EXTRA_INDEX_URL='https://extra.invalid/simple',
                   PIP_FIND_LINKS='env-cache', PIP_CONFIG_FILE=str(config))
        code = """
import json
from pip._internal.commands import create_command
command = create_command('install')
options, args = command.parse_args(['--index-url', 'https://pypi.org/simple', '--only-binary=Pillow'])
print(json.dumps({'no_index': options.no_index, 'extras': options.extra_index_urls,
                  'find_links': options.find_links, 'index': options.index_url,
                  'no_binary': sorted(options.format_control.no_binary),
                  'only_binary': sorted(options.format_control.only_binary)}))
"""
        run = subprocess.run([str(self.python), *subject.ISOLATED, '-c', code],
                             env=subject.official_environment(env), capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {'no_index': False, 'extras': [], 'find_links': [],
                         'index': subject.OFFICIAL_INDEX, 'no_binary': [], 'only_binary': ['pillow']})
        self.assertIn('no-index = true', config.read_text())

    def test_real_pip_can_recover_missing_package_from_clean_index_with_exact_version(self):
        # A tiny local wheel/simple index tests actual pip resolution offline.
        # No PyPI package or native wheel is forged or downloaded by this test.
        index = self.root / 'simple'
        directory = index / 'igac-r83-fixture'
        directory.mkdir(parents=True, exist_ok=True)
        wheel = directory / 'igac_r83_fixture-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as z:
            z.writestr('igac_r83_fixture.py', 'VALUE = 83\n')
            z.writestr('igac_r83_fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: igac-r83-fixture\nVersion: 1.0\n')
            z.writestr('igac_r83_fixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: r83-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            z.writestr('igac_r83_fixture-1.0.dist-info/RECORD', '')
        (directory / 'index.html').write_text('<a href="' + wheel.name + '">' + wheel.name + '</a>')
        empty = self.root / 'empty'
        empty.mkdir(exist_ok=True)
        command = [str(self.python), *subject.ISOLATED, '-m', 'pip', '--disable-pip-version-check',
                   '--no-input', 'install', '--no-deps', '--no-cache-dir', '--only-binary=:all:',
                   '--prefix', str(self.venv), 'igac-r83-fixture==1.0']
        env = subject.official_environment(os.environ)
        failed = subprocess.run(command + ['--index-url', empty.as_uri()], env=env, capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(failed.returncode, 0)
        recovered = subprocess.run(command + ['--index-url', index.as_uri()], env=env, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        check = subprocess.run([str(self.python), *subject.ISOLATED, '-c',
                                "import importlib.metadata,igac_r83_fixture;assert importlib.metadata.version('igac-r83-fixture')=='1.0';assert igac_r83_fixture.VALUE==83"],
                               capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == '__main__':
    unittest.main()
