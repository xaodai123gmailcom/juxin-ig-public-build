"""Real disposable venv checks; offline, no app/browser or user data involved."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ensure_python_environment import ISOLATED, PythonEnvironment, PythonEnvironmentError


class PythonEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='Juxin 环境 (3) ')
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.root = self.project / '.venv'
        self.log = []
        self.project.joinpath('account-data.fixture').write_text('preserve account data', encoding='utf-8')

    def env(self, **kwargs):
        return PythonEnvironment(self.project, output=lambda value, **_: self.log.append(value), **kwargs)

    def create(self, with_pip=False, system_site_packages=False):
        venv.EnvBuilder(with_pip=with_pip, system_site_packages=system_site_packages).create(self.root)

    def assert_ready(self, result):
        self.assertTrue(result['verified'])
        check = subprocess.run([result['python'], *ISOLATED, '-m', 'pip', '--version'], cwd=self.project,
                               capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn('.venv', check.stdout)
        self.assertEqual(self.project.joinpath('account-data.fixture').read_text(), 'preserve account data')
        self.assertFalse((self.root / 'igac-runtime-ready.json').exists(), 'bootstrap must not seal full runtime readiness')

    def test_missing_pip_is_repaired_offline_in_place_and_healthy_rerun_is_reused(self):
        self.create()
        marker = self.root / 'igac-runtime-ready.json'
        marker.write_text('{"stale":true}')
        keep = self.root / 'keep-dependency.fixture'
        keep.write_text('keep installed files')
        old = subprocess.run([str(self.env().python), *ISOLATED, '-m', 'pip', '--version'], capture_output=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn('No module named pip', old.stderr)
        result = self.env().ensure()
        self.assert_ready(result)
        self.assertEqual(result['backups'], [])
        self.assertEqual(keep.read_text(), 'keep installed files')
        self.assertEqual(sum(e['stage'] == 'bootstrap-pip-offline' for e in result['events']), 1)
        reused = self.env().ensure()
        self.assert_ready(reused)
        self.assertFalse(any(e['stage'] in {'bootstrap-pip-offline', 'create-venv'} for e in reused['events']))

    def test_stale_pip_metadata_causes_one_preserved_backup_then_fresh_rebuild(self):
        self.create(with_pip=True)
        executable = str(self.env().python)
        purelib = Path(subprocess.check_output([executable, *ISOLATED, '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'], encoding='utf-8').strip())
        shutil.rmtree(purelib / 'pip')  # Only this test's disposable package.
        self.assertTrue(list(purelib.glob('pip-*.dist-info')))
        keep = self.root / 'previous-dependency.fixture'
        keep.write_text('backup me')
        result = self.env().ensure()
        self.assert_ready(result)
        self.assertEqual(len(result['backups']), 1)
        self.assertEqual((Path(result['backups'][0]) / keep.name).read_text(), 'backup me')
        self.assertEqual(sum(e['stage'] == 'create-venv' for e in result['events']), 1)

    def test_partial_environment_without_an_interpreter_is_backed_up(self):
        self.root.mkdir()
        (self.root / 'partial.fixture').write_text('unfinished environment')
        result = self.env().ensure()
        self.assert_ready(result)
        self.assertEqual(len(result['backups']), 1)
        self.assertEqual((Path(result['backups'][0]) / 'partial.fixture').read_text(), 'unfinished environment')

    def test_project_pip_shadow_and_python_environment_variables_cannot_supply_global_pip(self):
        self.create()
        global_version = importlib.metadata.version('pip')
        (self.project / 'pip.py').write_text('raise RuntimeError("project pip must not be imported")')
        with patch.dict(os.environ, {'PYTHONPATH': str(self.project), 'PYTHONHOME': str(self.project / 'invalid-python-home')}):
            result = self.env().ensure()
            self.assert_ready(result)
        self.assertEqual(importlib.metadata.version('pip'), global_version)
        self.assertEqual(result['backups'], [])

    def test_system_site_packages_are_not_accepted_as_a_ready_local_environment(self):
        self.create(system_site_packages=True)
        result = self.env().ensure()
        self.assert_ready(result)
        self.assertEqual(len(result['backups']), 1)
        self.assertIn('include-system-site-packages = false', (self.root / 'pyvenv.cfg').read_text())

    def test_unavailable_bootstrap_is_bounded_and_does_not_leave_a_success_marker(self):
        self.create()
        (self.root / 'igac-runtime-ready.json').write_text('{}')
        attempts = []

        def unavailable(command, **kwargs):
            if 'ensurepip' in command:
                attempts.append(command)
                return subprocess.CompletedProcess(command, 1, 'No module named ensurepip')
            return subprocess.run(command, **kwargs)

        environment = self.env(runner=unavailable)
        with self.assertRaisesRegex(PythonEnvironmentError, 'Offline pip recovery failed'):
            environment.ensure()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(environment.backups), 1)
        self.assertFalse((self.root / 'igac-runtime-ready.json').exists())
        self.assertTrue(Path(environment.backups[0]).is_dir())

    def test_linked_environment_is_rejected_without_touching_the_destination(self):
        other = self.project / 'external'
        other.mkdir()
        marker = other / 'igac-runtime-ready.json'
        marker.write_text('preserve external marker')
        # Avoid relying on Windows symlink privilege; the production reparse
        # predicate is separately checked for a real link where supported.
        if os.name == 'nt':
            self.root.mkdir()
            with patch('ensure_python_environment._reparse', return_value=True):
                with self.assertRaisesRegex(PythonEnvironmentError, 'linked'):
                    self.env().ensure()
        else:
            self.root.symlink_to(other, target_is_directory=True)
            with self.assertRaisesRegex(PythonEnvironmentError, 'linked'):
                self.env().ensure()
        self.assertEqual(marker.read_text(), 'preserve external marker')
        self.assertFalse(list(self.project.glob('.venv-backup-*')))

    def test_failed_backup_does_not_delete_or_clear_the_old_environment(self):
        self.root.mkdir()
        keep = self.root / 'keep.fixture'
        keep.write_text('preserve original')
        with patch.object(Path, 'rename', side_effect=PermissionError('in use')):
            with self.assertRaisesRegex(PythonEnvironmentError, 'Could not back up'):
                self.env().ensure()
        self.assertEqual(keep.read_text(), 'preserve original')

    def test_bootstrap_timeout_stops_before_renaming_a_possibly_busy_environment(self):
        self.create()
        keep = self.root / 'keep.fixture'
        keep.write_text('possible pending writer')

        def stalled(command, **kwargs):
            if 'ensurepip' in command:
                raise subprocess.TimeoutExpired(command, 120)
            return subprocess.run(command, **kwargs)

        environment = self.env(runner=stalled)
        with self.assertRaisesRegex(PythonEnvironmentError, 'Automatic rebuild was stopped'):
            environment.ensure()
        self.assertEqual(keep.read_text(), 'possible pending writer')
        self.assertEqual(environment.backups, [])

    def test_activated_environment_resolves_base_python_before_cli_repair(self):
        self.create()
        python = str(self.env().python)
        script = str(Path(__file__).resolve().parents[1] / 'ensure_python_environment.py')
        marker = self.root / 'igac-runtime-ready.json'
        marker.write_text('not yet changed')
        wrong = subprocess.run([python, *ISOLATED, script, '--project-root', str(self.project)], capture_output=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('base Python interpreter', wrong.stdout)
        self.assertEqual(marker.read_text(), 'not yet changed')
        encoded = subprocess.check_output([python, *ISOLATED, '-c', "import json,sys;print(json.dumps(getattr(sys,'_base_executable',sys.executable)))"], encoding='ascii')
        repaired = subprocess.run([json.loads(encoded), *ISOLATED, script, '--project-root', str(self.project)], capture_output=True, encoding='utf-8', timeout=120)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertIn('PYTHON_BOOTSTRAP=PASS', repaired.stdout)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
