#!/usr/bin/env python3
"""Install the pinned Core dependencies in the project venv, with one clean retry.

The PowerShell caller owns the build mutex and captures the complete output.
This helper never edits pip configuration, requirement pins or runtime readiness.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import io
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


OFFICIAL_INDEX = 'https://pypi.org/simple'
ISOLATED = ['-I', '-X', 'utf8']


def official_environment(inherited):
    # PIP_CONFIG_FILE=null disables global, user and venv pip.ini. Clearing
    # PIP_* also removes no-index, extra-index, find-links, ABI and old filters.
    # Standard proxy and CA environment settings remain available; TLS stays on.
    env = {k: v for k, v in inherited.items() if not k.upper().startswith('PIP_')}
    env['PIP_CONFIG_FILE'] = os.devnull
    return env


def verify_dependencies(project, *, packaging=False):
    # pip is already bootstrapped. Its vendored parser avoids depending on an
    # unrelated global packaging installation before the app is installed.
    from pip._vendor.packaging.requirements import Requirement

    failures = []
    root = (project / '.venv').resolve()
    requirement_path = project / ('scripts/windows-packaging-requirements.txt' if packaging else 'backend/requirements.txt')
    for line in requirement_path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        requirement = Requirement(line.strip())
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            dist = importlib.metadata.distribution(requirement.name)
            if not Path(dist.locate_file('')).resolve().is_relative_to(root):
                failures.append(requirement.name + ': outside project venv')
            if dist.version not in requirement.specifier:
                failures.append(str(requirement) + ': installed ' + dist.version)
        except importlib.metadata.PackageNotFoundError:
            failures.append(requirement.name + ': not installed')
    if failures:
        raise RuntimeError('; '.join(failures))

    if packaging:
        import PyInstaller
        import setuptools._distutils
        if not Path(PyInstaller.__file__).resolve().is_relative_to(root):
            raise RuntimeError('PyInstaller import is outside project venv')
        print('PYTHON_PACKAGING_CHECK=PASS', flush=True)
        return

    import PIL
    from PIL import Image
    if not Path(PIL.__file__).resolve().is_relative_to(root):
        raise RuntimeError('Pillow import is outside project venv')
    if PIL.__version__ != importlib.metadata.version('Pillow'):
        raise RuntimeError('Pillow module and installed metadata versions differ')
    # Actual codecs/native extension, not only dist-info or a successful pip exit.
    for fmt in ('PNG', 'JPEG', 'WEBP'):
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), (30, 60, 90)).save(stream, format=fmt)
        stream.seek(0)
        with Image.open(stream) as decoded:
            decoded.load()
            if decoded.size != (8, 8) or decoded.resize((4, 4)).size != (4, 4):
                raise RuntimeError('Pillow ' + fmt + ' round-trip failed')
    print('PILLOW_CODEC_CHECK=PASS (' + PIL.__version__ + '; PNG/JPEG/WEBP)', flush=True)


def install_dependencies(project, *, runner=subprocess.run, environ=None, output=print, packaging=False):
    project = Path(project).resolve()
    inherited = dict(os.environ if environ is None else environ)
    # Diagnose active override NAMES only. Never print proxy/index credentials.
    output('PYTHON_DEPENDENCIES_RUNTIME=' + json.dumps({
        'python': platform.python_version(), 'machine': platform.machine(),
        'pip_override_names': sorted(k for k in inherited if k.upper().startswith('PIP_')),
    }), flush=True)
    base = [sys.executable, *ISOLATED, '-m', 'pip', '--disable-pip-version-check',
            '--no-input', '--retries', '2', '--timeout', '30']
    requirement_path = project / ('scripts/windows-packaging-requirements.txt' if packaging else 'backend/requirements.txt')
    install = ['install', '--prefix', str(project / '.venv'),
               '--only-binary=:all:' if packaging else '--only-binary=Pillow',
               '-r', str(requirement_path)]
    verify = [sys.executable, *ISOLATED, str(Path(__file__).resolve()),
              '--project-root', str(project), '--verify-only']
    if packaging:
        verify.append('--packaging')

    def run(stage, command, env):
        output('PYTHON_DEPENDENCIES_STAGE=' + stage, flush=True)
        result = runner(command, cwd=project, env=env, stdin=subprocess.DEVNULL, check=False)
        output('PYTHON_DEPENDENCIES_EXIT=' + stage + ':' + str(result.returncode), flush=True)
        return result.returncode

    repair_installed_files = False
    for attempt in ('configured', 'official'):
        env = inherited if attempt == 'configured' else official_environment(inherited)
        command = base + install
        if attempt == 'official':
            output('Retrying once with official PyPI and fresh index data; requirement pins are unchanged.', flush=True)
            if repair_installed_files:
                # dist-info can satisfy the pins even when modules/DLLs are
                # missing. Repair once after real import/consistency failure.
                command += ['--force-reinstall']
                output('Reinstalling pinned dependencies after local file verification failed.', flush=True)
            command += ['--index-url', OFFICIAL_INDEX, '--no-cache-dir']
        code = run(attempt + '-install', command, env)
        if code != 0:
            continue
        # Fresh subprocess after installation avoids stale imported module caches.
        if run(attempt + '-verify', verify, env) != 0:
            repair_installed_files = True
            continue
        if run(attempt + '-pip-check', base + ['check'], env) != 0:
            repair_installed_files = True
            continue
        output('PYTHON_DEPENDENCIES_CHECK=PASS', flush=True)
        return 0
    output('PYTHON_DEPENDENCIES_CHECK=FAILED', flush=True)
    log_name = 'python-packaging-full.log' if packaging else 'python-dependencies-full.log'
    output('Both configured and official attempts failed. See ' + log_name + ' for the failing package and connection details. Check access to pypi.org and files.pythonhosted.org; versions were not relaxed and the build must stop.', flush=True)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--packaging', action='store_true')
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    project = args.project_root.resolve()
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != (project / '.venv').resolve():
        print('PYTHON_DEPENDENCIES_CHECK=FAILED: run with this project\'s .venv Python; global installations are not modified.', flush=True)
        return 1
    requirement_path = project / ('scripts/windows-packaging-requirements.txt' if args.packaging else 'backend/requirements.txt')
    if not requirement_path.is_file():
        print('PYTHON_DEPENDENCIES_CHECK=FAILED: required dependency file is missing: ' + str(requirement_path), flush=True)
        return 1
    try:
        if args.verify_only:
            verify_dependencies(project, packaging=args.packaging)
            return 0
        return install_dependencies(project, packaging=args.packaging)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print('PYTHON_DEPENDENCIES_CHECK=FAILED: ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
