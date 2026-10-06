"""Strict, read-only source-byte utilities; no repository or archive identity."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
IS_WINDOWS = os.name == 'nt'
IGNORED_DIRECTORY_NAMES = frozenset({'__pycache__', 'node_modules', '.pytest_cache'})
EXECUTABLE_SUFFIXES = frozenset({
    '.py', '.pyw', '.pyc', '.pyo', '.js', '.cjs', '.mjs', '.jsx', '.ts',
    '.tsx', '.cts', '.mts', '.ps1', '.psm1', '.psd1', '.bat', '.cmd',
    '.sh', '.bash', '.zsh', '.vbs', '.vbe', '.wsf', '.wsh', '.html', '.htm',
    '.wasm', '.node', '.exe', '.dll', '.pyd', '.so', '.dylib', '.com', '.scr',
    '.reg', '.spec', '.pth',
})
DIGEST_RE = re.compile(r'[a-f0-9]{64}')
RESERVED_NAME_RE = re.compile(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', re.I)
REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class SourceBindingError(RuntimeError):
    """Source bytes cannot be read or bound safely."""


def _require(condition, message):
    if not condition:
        raise SourceBindingError(message)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _safe_name(name):
    _require(isinstance(name, str) and name and not any(
        char in name for char in '\\:*?"<>|') and not any(ord(char) < 32 for char in name),
        'Invalid source path')
    parts = name.split('/')
    _require(all(part and part not in {'.', '..'} and not part.endswith((' ', '.'))
                 and not RESERVED_NAME_RE.fullmatch(part) for part in parts),
             'Invalid source path: ' + name)
    return parts


def _digest(value, label):
    _require(isinstance(value, str) and DIGEST_RE.fullmatch(value),
             'Invalid SHA-256: ' + label)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _nonfinite(value):
    raise SourceBindingError('Non-finite JSON value: ' + value)


def _finite_float(value):
    number = float(value)
    _require(math.isfinite(number), 'Non-finite JSON value: ' + value)
    return number


def _json(data, label):
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_object_pairs,
                           parse_constant=_nonfinite, parse_float=_finite_float)
    except (UnicodeError, ValueError) as error:
        raise SourceBindingError('Invalid JSON: ' + label) from error
    _require(type(value) is dict, 'JSON must be an object: ' + label)
    return value


def _manifest(data, label):
    manifest = _json(data, label)
    folded = set()
    for name, digest in manifest.items():
        _safe_name(name)
        _digest(digest, name)
        _require(name.casefold() not in folded, 'Case-colliding source path: ' + name)
        folded.add(name.casefold())
    return manifest


def _inspect(path, *, directory=False):
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode) and not (
        getattr(info, 'st_file_attributes', 0) & REPARSE_POINT),
        'Symlink/reparse point is forbidden: ' + str(path))
    if directory:
        _require(stat.S_ISDIR(info.st_mode), 'Source parent is not a directory: ' + str(path))
    else:
        _require(stat.S_ISREG(info.st_mode), 'Source is not a regular file: ' + str(path))
        _require(info.st_nlink == 1, 'Hardlinked source is forbidden: ' + str(path))
    return info


def _parents(path):
    for parent in reversed(path.parents):
        _inspect(parent, directory=True)


def _identity(info):
    # Same-API before/after snapshots retain every timestamp, including ctime.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, 'st_birthtime_ns', None))


def _open_identity(info):
    if IS_WINDOWS:
        # CPython's Windows lstat reports creation time as ctime, while fstat
        # can report ChangeTime (cpython#157671, including Python 3.13.15).
        # Compare only cross-API-compatible fields; ctime is still checked
        # independently in each API's before/after snapshots below.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                getattr(info, 'st_birthtime_ns', None))
    return _identity(info)


def _read(root, name):
    path = root.joinpath(*_safe_name(name))
    _parents(path)
    before = _inspect(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(_open_identity(opened) == _open_identity(before) and opened.st_nlink == 1,
                 'Source changed while opening: ' + name)
        data = stream.read()
        _require(_identity(os.fstat(stream.fileno())) == _identity(opened),
                 'Source changed while reading: ' + name)
    _parents(path)
    _require(_identity(_inspect(path)) == _identity(before), 'Source changed while reading: ' + name)
    return data

