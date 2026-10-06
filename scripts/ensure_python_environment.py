#!/usr/bin/env python3
"""Repair only the project's build venv, using CPython's bundled offline pip.

The caller holds build_mutex.ps1. This does not install application dependencies
or write igac-runtime-ready.json; those checks still run at the end of install.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class PythonEnvironmentError(RuntimeError):
    pass


ISOLATED = ['-I', '-X', 'utf8']


RUNTIME_PROBE = """
import json, pathlib, platform, struct, sys, sysconfig
identity = {'version': sys.version.split()[0], 'implementation': platform.python_implementation(),
            'machine': platform.machine().casefold(), 'bits': struct.calcsize('P') * 8,
            'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
            'prefix': str(pathlib.Path(sys.prefix).resolve())}
valid = ((3, 11) <= sys.version_info[:2] < (3, 15) and identity['implementation'] == 'CPython'
         and identity['machine'] in {'amd64', 'x86_64'} and identity['bits'] == 64 and not identity['free_threaded'])
if len(sys.argv) > 1:
    valid = valid and pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve() and sys.prefix != sys.base_prefix
print(json.dumps(identity))
raise SystemExit(0 if valid else 1)
"""

PIP_PROBE = """
import importlib.metadata, json, pathlib, pip, pip.__main__, sys
root = pathlib.Path(sys.argv[1]).resolve()
module = pathlib.Path(pip.__file__).resolve()
metadata = pathlib.Path(importlib.metadata.distribution('pip').locate_file('')).resolve()
valid = (pathlib.Path(sys.prefix).resolve() == root and sys.prefix != sys.base_prefix
         and module.is_relative_to(root) and metadata.is_relative_to(root)
         and pip.__version__ == importlib.metadata.version('pip'))
print(json.dumps({'pip_version': pip.__version__, 'pip_module': str(module), 'local': valid}))
raise SystemExit(0 if valid else 1)
"""


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class PythonEnvironment:
    def __init__(self, project: Path, *, base_python: str = sys.executable, runner=subprocess.run, output=print):
        self.project = project.resolve()
        self.root = self.project / '.venv'
        self.python = self.root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        self.base_python = base_python
        self.runner = runner
        self.output = output
        self.events: list[dict] = []
        self.backups: list[str] = []

    def _run(self, stage: str, command: list[str], timeout: int = 30):
        self.output('PYTHON_ENV_STAGE=' + stage, flush=True)
        try:
            result = self.runner(command, cwd=self.project, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 encoding='utf-8', errors='replace', timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            self.events.append({'stage': stage, 'error': 'TimeoutExpired'})
            # ensurepip can have an installer child. Do not rename/rebuild the
            # environment while a timed-out process tree might still be writing.
            raise PythonEnvironmentError('Timed out during ' + stage + '; close processes using this build environment before retrying. Automatic rebuild was stopped.') from exc
        except OSError as exc:
            reason = type(exc).__name__
            self.events.append({'stage': stage, 'error': reason})
            self.output('PYTHON_ENV_COMMAND_FAILED=' + reason, flush=True)
            return None
        self.events.append({'stage': stage, 'exit_code': result.returncode})
        if result.stdout:
            self.output(result.stdout.rstrip(), flush=True)
        return result

    def _ok(self, stage: str, command: list[str], timeout: int = 30) -> bool:
        result = self._run(stage, command, timeout)
        return result is not None and result.returncode == 0

    def _runtime_valid(self) -> bool:
        if not self.python.is_file() or not (self.root / 'pyvenv.cfg').is_file():
            return False
        try:
            config = dict(line.split('=', 1) for line in (self.root / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines() if '=' in line)
            if {k.strip().casefold(): v.strip().casefold() for k, v in config.items()}.get('include-system-site-packages') != 'false':
                return False
        except (OSError, UnicodeError):
            return False
        return self._ok('verify-venv', [str(self.python), *ISOLATED, '-c', RUNTIME_PROBE, str(self.root)])

    def _pip_ready(self) -> bool:
        return (self._ok('verify-local-pip', [str(self.python), *ISOLATED, '-c', PIP_PROBE, str(self.root)])
                and self._ok('verify-pip-command', [str(self.python), *ISOLATED, '-m', 'pip', '--version']))

    def _backup(self):
        if not os.path.lexists(self.root):
            return
        if _reparse(self.root) or not self.root.is_dir():
            raise PythonEnvironmentError('Refusing to replace a linked or non-directory .venv.')
        backup = self.project / ('.venv-backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
        try:
            self.root.rename(backup)
        except OSError as exc:
            raise PythonEnvironmentError('Could not back up .venv; close processes using this build environment and retry. No files were deleted.') from exc
        self.backups.append(str(backup))
        self.output('PYTHON_ENV_BACKUP=' + str(backup), flush=True)

    def _create(self):
        # Separate interpreter creation from pip bootstrap so interrupted states
        # are detected explicitly. Never use --clear on an existing directory.
        if not self._ok('create-venv', [self.base_python, *ISOLATED, '-m', 'venv', '--without-pip', str(self.root)], 120):
            raise PythonEnvironmentError('Could not create the build environment. Repair the standard CPython installation and retry.')
        if not self._runtime_valid():
            raise PythonEnvironmentError('The new build interpreter failed isolation or compatibility checks.')

    def _bootstrap_pip(self) -> bool:
        # ensurepip uses wheels bundled with CPython; no get-pip download,
        # system pip, external index or user site is used to recover this venv.
        return (self._ok('bootstrap-pip-offline', [str(self.python), *ISOLATED, '-m', 'ensurepip', '--upgrade', '--default-pip'], 120)
                and self._pip_ready())

    def ensure(self) -> dict:
        if not self.project.is_dir():
            raise PythonEnvironmentError('Project directory does not exist.')
        if Path(sys.prefix).resolve() == self.root:
            raise PythonEnvironmentError('Run environment repair with the base Python interpreter, not the environment being repaired.')
        if os.path.lexists(self.root) and (_reparse(self.root) or not self.root.is_dir()):
            raise PythonEnvironmentError('Refusing a linked or non-directory .venv; use a normal build directory.')
        marker = self.root / 'igac-runtime-ready.json'
        if os.path.lexists(marker):
            marker.unlink()  # Remove a file/link only; never recursively delete.
        if not self._ok('verify-base-python', [self.base_python, *ISOLATED, '-c', RUNTIME_PROBE]):
            raise PythonEnvironmentError('Standard 64-bit CPython 3.11-3.14 is required; repair Python before retrying.')
        rebuilt = False
        if not self._runtime_valid():
            self._backup()
            self._create()
            rebuilt = True
        if self._pip_ready():
            return self.report(True)
        if self._bootstrap_pip():
            return self.report(True)
        # Stale pip dist-info can make ensurepip report "already satisfied"
        # while pip itself is missing. Preserve the old environment and rebuild
        # once, instead of retrying indefinitely or trusting that exit code.
        if not rebuilt:
            self._backup()
            self._create()
            if self._bootstrap_pip():
                return self.report(True)
        raise PythonEnvironmentError('Offline pip recovery failed. Repair the standard CPython installation (including ensurepip), then rerun START_HERE_NEWGEN.bat. Any old environment backup is preserved.')

    def report(self, ready: bool) -> dict:
        return {'verified': ready, 'python': str(self.python), 'backups': self.backups, 'events': self.events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    # -I ignores PYTHONIOENCODING as well; emit a consistent stream for the
    # Windows native logger without changing the caller's environment.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    environment = PythonEnvironment(args.project_root)
    try:
        result = environment.ensure()
    except (PythonEnvironmentError, OSError) as exc:
        result = environment.report(False)
        result['error'] = str(exc)
    print('PYTHON_ENV_RESULT=' + json.dumps(result, ensure_ascii=True), flush=True)
    print('PYTHON_BOOTSTRAP=' + ('PASS' if result['verified'] else 'FAILED'), flush=True)
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
