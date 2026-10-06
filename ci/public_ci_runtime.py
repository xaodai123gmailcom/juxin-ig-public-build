"""Bounded installed CRT evidence. Never load DLLs or infer redistribution rights."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from public_ci_common import regular, require

CORE = 'resources/backend/collector_core/_internal/'
TRIO = ('MSVCP140.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll')
NUMPY_MEMBER = 'numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll'
NUMPY_SHA256 = 'a4c2229bdc2a2a630acdc095b4d86008e5c3e3bc7773174354f3da4f5beb9cde'
# Reviewed reference, not a claim that this runner downloaded/verified this wheel.
NUMPY_WHEEL = 'numpy-2.3.5-cp312-cp312-win_amd64.whl'
NUMPY_WHEEL_SHA256 = '86945f2ee6d10cdfd67bcb4069c1662dd711f7e2a4343db5cecec06b87cf31aa'
MANIFEST = CORE + 'openvino-native-manifest.json'
CRT_PATHS = tuple(sorted([CORE + name for name in TRIO] + [CORE + NUMPY_MEMBER]))
CRT_NAMES = re.compile(r'^(?:msvcp|msvcr|vcruntime|vccorlib|concrt|ucrtbase|api-ms-win-crt)', re.I)
MAX_ENTRIES = 20000
MAX_DEPTH = 20
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INVENTORY_BYTES = 16384
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def inventory_digest(value):
    require(len(encoded(value)) <= MAX_INVENTORY_BYTES, 'CRT inventory exceeds bound')
    return hashlib.sha256(encoded(value)).hexdigest()


def file_evidence(path):
    path = regular(path)
    before = path.stat()
    require(0 < before.st_size <= MAX_FILE_BYTES, 'CRT evidence file exceeds bound or is empty')
    checksum, total = hashlib.sha256(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        while chunk := stream.read(min(1024 * 1024, MAX_FILE_BYTES - total + 1)):
            total += len(chunk)
            require(total <= MAX_FILE_BYTES, 'CRT evidence grew beyond read bound')
            checksum.update(chunk)
        finished = os.fstat(stream.fileno())
    after = regular(path).stat()
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    require(identity(before) == identity(opened) == identity(finished) == identity(after) and total == before.st_size,
            'CRT evidence changed while reading')
    return {'size': total, 'sha256': checksum.hexdigest()}


def installed_crt_files(root):
    """Inspect the whole installed tree; only four literal paths may be exported.

    Unexpected safe CRT paths are counted privately, making coverage incomplete.
    Their names and bytes are never exported. Unsafe traversal remains fatal.
    """
    root = regular(root, directory=True)
    allowed = {name.casefold(): name for name in CRT_PATHS}
    found = {}
    pending = [(root, 0)]
    seen = unreviewed = 0
    while pending:
        directory, depth = pending.pop()
        require(depth <= MAX_DEPTH, 'Installed tree exceeds depth bound')
        regular(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                require(seen <= MAX_ENTRIES, 'Installed tree exceeds entry bound')
                info = entry.stat(follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400),
                        'Linked installed entry is not accepted')
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append((path, depth + 1))
                else:
                    require(stat.S_ISREG(info.st_mode), 'Invalid installed entry type')
                    if entry.name.lower().endswith('.dll') and CRT_NAMES.match(entry.name):
                        key = path.relative_to(root).as_posix().casefold()
                        if key not in allowed:
                            unreviewed += 1
                        else:
                            require(key not in found, 'Duplicate installed CRT path')
                            found[key] = path
    return {allowed[key]: found[key] for key in sorted(found)}, unreviewed


def manifest_entries(path):
    regular(path)
    require(path.stat().st_size <= MAX_MANIFEST_BYTES, 'Native manifest exceeds bound')
    def unique_pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate native manifest key')
            result[key] = value
        return result
    with path.open('rb') as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    require(len(data) <= MAX_MANIFEST_BYTES, 'Native manifest grew beyond read bound')
    require(file_evidence(path) == {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
            'Native manifest changed while reading')
    payload = json.loads(data, object_pairs_hook=unique_pairs)
    require(type(payload) is dict and type(payload.get('schema_version')) is int and
            payload['schema_version'] == 1 and payload.get('openvino_version') == '2025.4.1',
            'Unexpected native manifest identity')
    rows = payload.get('msvc_runtime_files')
    require(type(rows) is list and len(rows) == len(TRIO), 'Unexpected native manifest CRT list')
    result = {}
    for row in rows:
        require(type(row) is dict and set(row) == {'path', 'size', 'sha256', 'machine'},
                'Unexpected native manifest CRT fields')
        name = row['path']
        require(type(name) is str and name in TRIO and name not in result, 'Unsafe or duplicate manifest CRT path')
        require(type(row['size']) is int and 0 < row['size'] <= MAX_FILE_BYTES and
                type(row['sha256']) is str and SHA256.fullmatch(row['sha256']) and row['machine'] == '0x8664',
                'Invalid manifest CRT evidence')
        result[name] = {'size': row['size'], 'sha256': row['sha256']}
    require(set(result) == set(TRIO), 'Native manifest CRT set is incomplete')
    return result


def fixed_versions(path):
    """Numeric PE fixed version resource only; no free text or signature assertion."""
    unknown = {'kind': 'unavailable', 'file_version': None, 'product_version': None}
    if sys.platform != 'win32':
        return unknown
    version = ctypes.WinDLL('version', use_last_error=True)
    size_fn = version.GetFileVersionInfoSizeW
    size_fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    size_fn.restype = ctypes.c_uint32
    unused = ctypes.c_uint32()
    size = size_fn(str(regular(path)), ctypes.byref(unused))
    if size == 0:
        return unknown
    require(size <= 65536, 'PE version resource exceeds bound')
    data = ctypes.create_string_buffer(size)
    read_fn = version.GetFileVersionInfoW
    read_fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    read_fn.restype = ctypes.c_int
    if not read_fn(str(path), 0, size, data):
        return unknown
    query = version.VerQueryValueW
    query.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
    query.restype = ctypes.c_int
    address, length = ctypes.c_void_p(), ctypes.c_uint32()
    if not query(data, '\\', ctypes.byref(address), ctypes.byref(length)):
        return unknown
    start = ctypes.addressof(data)
    require(address.value is not None and start <= address.value <= start + size - 52 and length.value >= 52,
            'Invalid PE fixed version resource')
    values = (ctypes.c_uint32 * 13).from_address(address.value)
    require(values[0] == 0xFEEF04BD and values[1] == 0x10000, 'Invalid PE fixed version header')
    def parts(high, low):
        return [high >> 16, high & 0xffff, low >> 16, low & 0xffff]
    return {'kind': 'embedded-pe-fixed-resource', 'file_version': parts(values[2], values[3]),
            'product_version': parts(values[4], values[5])}


def checked_versions(path):
    value = fixed_versions(path)
    require(type(value) is dict and set(value) == {'kind', 'file_version', 'product_version'}, 'Invalid version fields')
    require(value['kind'] in ('unavailable', 'embedded-pe-fixed-resource'), 'Invalid version kind')
    for key in ('file_version', 'product_version'):
        numbers = value[key]
        require(numbers is None if value['kind'] == 'unavailable' else
                type(numbers) is list and len(numbers) == 4 and
                all(type(part) is int and 0 <= part <= 65535 for part in numbers), 'Invalid numeric version')
    return value


# Fixed candidates only. Absence says nothing about legal permission. These are
# package notices, not claimed CRT licenses; no notice text is read or exported.
NOTICE_CANDIDATES = (
    ('installed', 'LICENSE.electron.txt'),
    ('installed', 'LICENSES.chromium.html'),
    ('installed', CORE + 'numpy-2.3.5.dist-info/LICENSE.txt'),
    ('installed', CORE + 'numpy-2.3.5.dist-info/licenses/LICENSE.txt'),
    ('installed', CORE + 'openvino-2025.4.1.dist-info/LICENSE'),
    ('installed', CORE + 'openvino-2025.4.1.dist-info/licenses/LICENSE'),
    ('cpython-distribution', 'LICENSE.txt'),
    ('build-environment', '.venv/Lib/site-packages/numpy-2.3.5.dist-info/LICENSE.txt'),
    ('build-environment', '.venv/Lib/site-packages/numpy-2.3.5.dist-info/licenses/LICENSE.txt'),
    ('build-environment', '.venv/Lib/site-packages/openvino-2025.4.1.dist-info/LICENSE'),
    ('build-environment', '.venv/Lib/site-packages/openvino-2025.4.1.dist-info/licenses/LICENSE'),
)


def exists(path):
    # Permission errors are not evidence of absence. Broken links still exist.
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    return True


def notice_evidence(roots):
    records = []
    for location, relative in NOTICE_CANDIDATES:
        path = roots[location] / relative
        # Broken links are invalid evidence, never reported as absent.
        record = {'location': location, 'path': relative, 'status': 'absent'}
        if exists(path):
            record.update(status='present', **file_evidence(path))
        else:
            parent = path.parent
            while not exists(parent):
                parent = parent.parent
            regular(parent, directory=True)
        records.append(record)
    return sorted(records, key=lambda item: (item['location'], item['path']))


def collect_inventory(installation, source_root, *, system_directory=None, python_root=None):
    installation, source_root = Path(installation), Path(source_root)
    if system_directory is None:
        require(os.name == 'nt' and os.environ.get('SystemRoot'), 'Windows CRT source is unavailable')
        system_directory = Path(os.environ['SystemRoot']) / 'System32'
    system_directory = regular(system_directory, directory=True)
    python_root = regular(Path(python_root) if python_root is not None else Path(sys.base_prefix), directory=True)
    files, unreviewed = installed_crt_files(installation)
    packaged_manifest = installation / MANIFEST
    source_manifest = source_root / 'build/openvino-native-manifest.json'
    expected = manifest_entries(packaged_manifest)
    require(file_evidence(packaged_manifest) == file_evidence(source_manifest), 'Packaged native manifest differs from build')
    manifest_hash = file_evidence(packaged_manifest)['sha256']
    records = []
    for relative in CRT_PATHS:
        if relative not in files:
            continue
        path = files[relative]
        evidence = file_evidence(path)
        versions = checked_versions(path)
        require(file_evidence(path) == evidence, 'Installed CRT changed while reading version')
        if relative == CORE + NUMPY_MEMBER:
            require(evidence['sha256'] == NUMPY_SHA256, 'NumPy CRT differs from reviewed wheel member')
            provenance = {'category': 'verified-pinned-wheel-member-bytes', 'package': 'numpy', 'version': '2.3.5',
                'member': NUMPY_MEMBER, 'reference_archive': NUMPY_WHEEL,
                'reference_archive_sha256': NUMPY_WHEEL_SHA256,
                'archive_verified_on_runner': False,
                'reference_package_notice': {'member': 'numpy-2.3.5.dist-info/LICENSE.txt',
                    'sha256': '596b953e1dcbe829b32ae444387efa15003fc6abdeca8a2179f364a6364c286e'},
                'reference_package_metadata_sha256': '7f8a1bfc2b82fee89e1ceef1038aa174ee814832b54c39b4b1b93028ce3f27b1'}
        else:
            name = relative[len(CORE):]
            require(evidence == expected[name] == file_evidence(system_directory / name),
                    'Installed CRT differs from sealed runner source')
            provenance = {'category': 'runner-runtime-bytes', 'source': 'system32/' + name,
                'native_manifest_sha256': manifest_hash, 'cpython_distribution_byte_match': False}
            candidate = python_root / name
            if exists(candidate):
                provenance['cpython_distribution_byte_match'] = file_evidence(candidate) == evidence
        records.append(dict(path=relative, **evidence, versions=versions, provenance=provenance))
    missing = len(CRT_PATHS) - len(files)
    complete = unreviewed == 0 and missing == 0
    inventory = {'schema': 1, 'scope': 'installed-msvc-crt-allowlist-v1',
        'status': 'complete' if complete else 'incomplete',
        'reason': 'reviewed-layout' if complete else 'unreviewed-crt-layout',
        'unreviewed_crt_file_count': unreviewed, 'missing_reviewed_crt_file_count': missing,
        'paths_relative_to': 'installed-product-root', 'native_manifest_sha256': manifest_hash,
        'signature_verified_by_inventory': False, 'redistribution_rights_assessed': False,
        'license_basis': 'unresolved', 'crt_files': records,
        'notice_scope': 'fixed-package-notice-candidates-only',
        'license_notices': notice_evidence({'installed': installation, 'build-environment': source_root,
            'cpython-distribution': python_root})}
    inventory_digest(inventory)
    return inventory
