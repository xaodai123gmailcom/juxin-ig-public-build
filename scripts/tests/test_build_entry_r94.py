"""Build environment failures reproduced without network or user data."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import backend_test_process as backend
from scripts import verify_frozen_core_service as core
from scripts import install_python_dependencies as dependencies


class BuildEntryTests(unittest.TestCase):
    def test_fresh_venv_owns_real_child_pid_prefix_and_timeout(self):
        with tempfile.TemporaryDirectory(prefix='Juxin [IG 22]-') as temp:
            root = Path(temp)
            environment = root / 'venv'
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            driver = root / 'driver.py'
            driver.write_text('''import io, json, os, pathlib, subprocess, sys
from unittest.mock import patch
sys.path.insert(0, ROOT)
from scripts import backend_test_process as backend
root = pathlib.Path(__file__).parent
assert pathlib.Path(sys.prefix).resolve() == (root / 'venv').resolve()
runner = root / 'child.py'
marker = root / 'started'
runner.write_text("import json, os, pathlib, sys, threading\\n"
    "print(json.dumps({'pid':os.getpid(),'prefix':sys.prefix}),flush=True)\\n"
    "if '--block' in sys.argv:\\n"
    "    pathlib.Path(" + repr(str(marker)) + ").touch()\\n"
    "    threading.Event().wait()\\n")
original = subprocess.Popen
for blocked in (False, True):
    spawned = []
    def record(*args, **kwargs):
        child = original(*args, **kwargs)
        spawned.append(child)
        return child
    stream = io.StringIO()
    with patch.object(backend.subprocess, 'Popen', side_effect=record), \\
            patch.object(backend.time, 'monotonic', side_effect=lambda:1000 if marker.exists() else 0):
        try:
            result = backend.run_backend_process(runner, ['--block'] if blocked else [],
                cwd=root, stream=stream, case_timeout=1, overall_timeout=2)
        except AssertionError as error:
            assert blocked and 'overall limit' in str(error), str(error)
        else:
            assert not blocked and result.returncode == 0, stream.getvalue()
    actual = json.loads(stream.getvalue().splitlines()[0])
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert actual['pid'] == spawned[0].pid, (actual, spawned[0].pid)
    assert pathlib.Path(actual['prefix']).resolve() == pathlib.Path(sys.prefix).resolve()
print('FRESH_VENV_SUCCESS_AND_TIMEOUT=PASS')
'''.replace('sys.path.insert(0, ROOT)', 'sys.path.insert(0, ' + repr(str(ROOT)) + ')'), encoding='utf-8')
            child_env = {k: v for k, v in os.environ.items() if k.upper() != '__PYVENV_LAUNCHER__'}
            executable = str(python)
            if os.name == 'nt':
                # The fixture driver must also be directly owned on timeout.
                executable = sys._base_executable
                child_env['__PYVENV_LAUNCHER__'] = str(python)
            result = subprocess.run([executable, '-I', '-X', 'utf8', str(driver)],
                cwd=root, env=child_env, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn('FRESH_VENV_SUCCESS_AND_TIMEOUT=PASS', result.stdout)

    def test_backend_timeout_supervisor_owns_windows_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Mock()
            child.poll.return_value = 0
            child.wait.return_value = 0
            with patch.object(core, 'IS_WINDOWS', True), \
                    patch.object(sys, 'executable', '/fixture/venv/python.exe'), \
                    patch.object(sys, '_base_executable', '/fixture/base/python.exe'), \
                    patch.object(backend.subprocess, 'Popen', return_value=child) as spawn:
                backend.run_backend_process(Path(tmp) / 'test.py', [], cwd=tmp)
            command = spawn.call_args.args[0]
            self.assertEqual('/fixture/base/python.exe', command[0])
            self.assertEqual('/fixture/venv/python.exe', spawn.call_args.kwargs['env']['__PYVENV_LAUNCHER__'])
            child.kill.assert_not_called()

    def test_desktop_install_keeps_build_tools_with_production_env(self):
        npm = shutil.which('npm')
        self.assertIsNotNone(npm, 'npm is required by the Windows build')
        text = (ROOT / 'scripts/install_windows.ps1').read_text(encoding='utf-8-sig')
        command = next(line.strip().split() for line in text.splitlines() if re.match(r'^\s*npm ci(?:\s|$)', line))
        env = {k:v for k,v in os.environ.items() if not k.lower().startswith('npm_')}
        env.update(NODE_ENV='production', NPM_CONFIG_OMIT='dev')
        with tempfile.TemporaryDirectory(prefix='Juxin npm (22)-') as temp:
            root = Path(temp); dev = root / 'local-dev'; dev.mkdir()
            (dev / 'package.json').write_text(json.dumps({'name':'build-tool-fixture','version':'1.0.0'}))
            (root / 'package.json').write_text(json.dumps({'name':'fixture','version':'1.0.0',
                'devDependencies':{'build-tool-fixture':'file:local-dev'}}))
            def run(args):
                result = subprocess.run([npm, *args, '--offline', '--ignore-scripts', '--no-audit', '--no-fund'],
                    cwd=root, env=env, shell=os.name=='nt', capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            run(['install','--package-lock-only'])
            run(command[1:])
            self.assertTrue((root/'node_modules/build-tool-fixture/package.json').exists(),
                'A successful npm ci silently omitted the tools required to build the app')

    def test_packaging_dependencies_recover_from_stale_mirror_without_relaxing_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'scripts').mkdir()
            req = root/'scripts/windows-packaging-requirements.txt'
            req.write_text('pyinstaller==6.21.0\n')
            calls=[]; codes=iter([1,0,0,0])
            def run(command, **kwargs):
                calls.append((command,kwargs));return subprocess.CompletedProcess(command,next(codes))
            result=dependencies.install_dependencies(root,packaging=True,runner=run,
                environ={'PIP_NO_INDEX':'1','PIP_INDEX_URL':'https://private.invalid/simple'},output=lambda *a,**k:None)
            self.assertEqual(0,result)
            self.assertEqual(4,len(calls))
            self.assertIn('--only-binary=:all:',calls[1][0])
            self.assertEqual(['--index-url',dependencies.OFFICIAL_INDEX,'--no-cache-dir'],calls[1][0][-3:])
            self.assertNotIn('PIP_NO_INDEX',calls[1][1]['env'])
            self.assertIn('--packaging',calls[2][0])
            self.assertEqual('check',calls[3][0][-1])
            self.assertEqual('pyinstaller==6.21.0\n',req.read_text())

    def test_packaging_never_passes_on_install_import_or_consistency_failure(self):
        for codes in ([1, 1], [0, 1, 0, 1], [0, 0, 1, 0, 0, 1]):
            with self.subTest(codes=codes), tempfile.TemporaryDirectory() as temp:
                calls = []
                output = []
                results = iter(codes)
                def run(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, next(results))
                result = dependencies.install_dependencies(Path(temp), packaging=True,
                    runner=run, environ={}, output=lambda message, **kw: output.append(message))
                self.assertEqual(1, result)
                self.assertEqual(len(codes), len(calls))
                self.assertNotIn('PYTHON_DEPENDENCIES_CHECK=PASS', output)
                self.assertIn('python-packaging-full.log', output[-1])


if __name__ == '__main__': unittest.main()
