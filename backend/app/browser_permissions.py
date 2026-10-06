"""Prepare read/execute access for Windows Chrome's sandbox, within its runtime.

Chrome's own setup command is preferred. The documented icacls fallback only
adds sandbox-package read/execute rights to the selected browser directory.
https://pptr.dev/troubleshooting#chrome-reports-sandbox-errors-on-windows
"""
from __future__ import annotations

import locale
import os
from pathlib import Path
import re
import subprocess
import threading


class BrowserPermissionError(RuntimeError):
    pass


_lock = threading.Lock()
_prepared = {}


def _decode(value):
    if isinstance(value, str):
        return value[-4000:]
    try:
        return value.decode('utf-8')[-4000:]
    except UnicodeDecodeError:
        return value.decode(locale.getencoding(), errors='replace')[-4000:]


def _plain(path):
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise BrowserPermissionError('Browser permission preparation refuses links or reparse points: ' + str(path))
    if path.is_file() and info.st_nlink > 1:
        raise BrowserPermissionError('Browser permission preparation refuses hard-linked files: ' + str(path))


def _runtime_directory(executable):
    executable = Path(executable).absolute()
    directory = executable.parent
    if (executable.name.lower() != 'chrome.exe' or directory.name not in {'chrome-win64', 'chrome-win'}
            or not re.fullmatch(r'chromium-\d+', directory.parent.name)):
        raise BrowserPermissionError('Permission preparation only accepts a pinned bundled Chromium directory')
    # Check before resolve so a junction cannot silently redirect the operation.
    for parent in (directory, *directory.parents):
        _plain(parent)
    for name in ('chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'locales/en-US.pak'):
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise BrowserPermissionError('Browser runtime is incomplete: ' + name)
        _plain(path)
    return directory.resolve()


def ensure_browser_sandbox_access(executable, *, windows=None, runner=None, force=False):
    windows = os.name == 'nt' if windows is None else windows
    if not windows:
        return {'status': 'not_windows'}
    runner = subprocess.run if runner is None else runner
    try:
        return _prepare(executable, runner, force)
    except OSError as error:
        raise BrowserPermissionError('Cannot inspect the bundled browser permission scope: ' + str(error)) from error


def _walk_error(error):
    raise error


def _prepare(executable, runner, force):
    with _lock:
        directory = _runtime_directory(executable)
        directory_info = directory.stat()
        identity = ((directory_info.st_dev, directory_info.st_ino),) + tuple(
            (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
            for p in (directory / 'chrome.exe', directory / 'chrome.dll'))
        key = str(directory)
        if not force and _prepared.get(key) == identity:
            return {'status': 'prepared_in_this_process', 'directory': key}
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=_walk_error):
            for name in (*dirs, *files):
                _plain(Path(parent) / name)
        attempts = []
        setup = directory / 'setup.exe'
        if setup.is_file():
            try:
                result = runner([str(setup), '--configure-browser-in-directory=' + key],
                    cwd=directory, stdin=subprocess.DEVNULL, capture_output=True, timeout=45, shell=False)
                attempts.append({'method': 'chrome-setup', 'exit_code': result.returncode,
                                 'output': _decode(result.stdout + result.stderr)})
                if result.returncode == 0:
                    _prepared[key] = identity
                    return {'status': 'prepared', 'method': 'chrome-setup', 'directory': key, 'attempts': attempts}
            except (OSError, subprocess.TimeoutExpired) as error:
                attempts.append({'method': 'chrome-setup', 'error': str(error)})
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'icacls.exe'
        # No ownership reset, deny removal, write grant, elevation, or changes to
        # parent/source/account folders. /L is a second link traversal defense.
        command = [str(system), key, '/grant', '*S-1-15-2-1:(OI)(CI)(RX)', '/T', '/C', '/L']
        try:
            result = runner(command, cwd=directory, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=45, shell=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BrowserPermissionError('Cannot prepare sandbox read/execute access: ' + str(error)) from error
        detail = _decode(result.stdout + result.stderr)
        attempts.append({'method': 'icacls-read-execute', 'exit_code': result.returncode, 'output': detail})
        if result.returncode != 0:
            raise BrowserPermissionError(f'Sandbox read/execute permission preparation failed (exit {result.returncode}): {detail}')
        _prepared[key] = identity
        return {'status': 'prepared', 'method': 'icacls-read-execute', 'directory': key, 'attempts': attempts}
