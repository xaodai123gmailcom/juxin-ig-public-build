"""Synthetic filesystem and mocked Windows metadata tests; no runtime imports."""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import public_ci as ci
import public_ci_common as common
import public_ci_runtime as runtime
REAL_FIXED_VERSIONS = runtime.fixed_versions


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RuntimeInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / 'installed'
        self.source = self.root / 'source'
        self.system = self.root / 'system'
        self.python = self.root / 'python'
        for directory in (self.install, self.source, self.system, self.python):
            directory.mkdir()
        entries = []
        for name in runtime.TRIO:
            # Deliberately non-executable bytes, never passed to a real Win32 API.
            data = b'synthetic CRT ' + name.encode()
            self.write(self.install / runtime.CORE / name, data)
            self.write(self.system / name, data)
            entries.append({'path': name, 'size': len(data), 'sha256': sha(data), 'machine': '0x8664'})
        self.alias_bytes = b'synthetic NumPy CRT'
        self.write(self.install / runtime.CORE / runtime.NUMPY_MEMBER, self.alias_bytes)
        self.manifest = {'schema_version': 1, 'openvino_version': '2025.4.1', 'msvc_runtime_files': entries,
            'runtime_version': '/private/user/secret-token-do-not-export', 'files': []}
        self.seal_manifest()
        self.write(self.python / 'LICENSE.txt', b'Synthetic notice, never exported')
        self.write(self.install / 'LICENSE.electron.txt', b'Synthetic Electron notice')
        self.write(self.python / 'VCRUNTIME140.dll', (self.system / 'VCRUNTIME140.dll').read_bytes())
        self.addCleanup(patch.stopall)
        patch.object(runtime, 'NUMPY_SHA256', sha(self.alias_bytes)).start()
        patch.object(runtime, 'fixed_versions', return_value={
            'kind': 'embedded-pe-fixed-resource', 'file_version': [14, 44, 35112, 0],
            'product_version': [14, 44, 35112, 0]}).start()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def seal_manifest(self):
        data = json.dumps(self.manifest).encode()
        self.write(self.install / runtime.MANIFEST, data)
        self.write(self.source / 'build/openvino-native-manifest.json', data)

    def collect(self):
        return runtime.collect_inventory(self.install, self.source,
            system_directory=self.system, python_root=self.python)

    def test_exact_sorted_safe_records_and_provenance_is_not_license_permission(self):
        value = self.collect()
        self.assertEqual([row['path'] for row in value['crt_files']], list(runtime.CRT_PATHS))
        self.assertEqual(value, self.collect())
        self.assertEqual(value['status'], 'complete')
        self.assertEqual(value['reason'], 'reviewed-layout')
        self.assertEqual(value['unreviewed_crt_file_count'], 0)
        self.assertEqual(value['license_basis'], 'unresolved')
        self.assertFalse(value['redistribution_rights_assessed'])
        self.assertFalse(value['signature_verified_by_inventory'])
        for row in value['crt_files']:
            self.assertEqual(set(row), {'path', 'size', 'sha256', 'versions', 'provenance'})
            self.assertEqual(row['sha256'], sha((self.install / row['path']).read_bytes()))
        alias = next(row for row in value['crt_files'] if row['path'].endswith(runtime.NUMPY_MEMBER))
        self.assertEqual(alias['provenance']['category'], 'verified-pinned-wheel-member-bytes')
        self.assertFalse(alias['provenance']['archive_verified_on_runner'])
        cpython = next(row for row in value['crt_files'] if row['path'].endswith('/VCRUNTIME140.dll'))
        self.assertEqual(cpython['provenance']['category'], 'runner-runtime-bytes')
        self.assertTrue(cpython['provenance']['cpython_distribution_byte_match'])
        text = json.dumps(value)
        for forbidden in (str(self.root), '/private', 'secret-token', 'Synthetic notice', 'RuntimeEULA'):
            self.assertNotIn(forbidden, text)
        self.assertLess(len(json.dumps(value, indent=2).encode()), 16384)
        notices = value['license_notices']
        self.assertEqual(notices, sorted(notices, key=lambda row: (row['location'], row['path'])))
        self.assertEqual(next(row for row in notices if row['location'] == 'cpython-distribution')['sha256'],
                         sha(b'Synthetic notice, never exported'))
        self.assertTrue(any(row['status'] == 'absent' for row in notices))

    def test_mismatched_known_bytes_fail_but_missing_layout_is_explicitly_incomplete(self):
        path = self.install / runtime.CORE / runtime.NUMPY_MEMBER
        path.write_bytes(b'wrong')
        with self.assertRaises(RuntimeError): self.collect()
        path.unlink()
        value = self.collect()
        self.assertEqual(value['status'], 'incomplete')
        self.assertEqual(value['reason'], 'unreviewed-crt-layout')
        self.assertEqual(value['missing_reviewed_crt_file_count'], 1)
        self.assertEqual(len(value['crt_files']), 3)

    def test_runner_source_and_native_manifest_mismatch_fail(self):
        path = self.system / runtime.TRIO[0]
        path.write_bytes(b'changed runner DLL')
        with self.assertRaises(RuntimeError): self.collect()
        path.write_bytes((self.install / runtime.CORE / runtime.TRIO[0]).read_bytes())
        (self.source / 'build/openvino-native-manifest.json').write_bytes(b'{}')
        with self.assertRaises(RuntimeError): self.collect()

    def test_extra_safe_crt_paths_report_incomplete_without_exporting_names(self):
        for relative in ('elsewhere/vcruntime140.dll', runtime.CORE + 'MSVCR120.dll',
                         runtime.CORE + '../private/msvcp140-secret.dll'):
            with self.subTest(relative=relative):
                path = self.install / relative
                self.write(path, b'non-executable')
                value = self.collect()
                self.assertEqual(value['status'], 'incomplete')
                self.assertEqual(value['reason'], 'unreviewed-crt-layout')
                self.assertEqual(value['unreviewed_crt_file_count'], 1)
                self.assertEqual(value['missing_reviewed_crt_file_count'], 0)
                self.assertNotIn(relative, json.dumps(value))
                path.unlink()

    def test_incomplete_layout_never_suppresses_expected_byte_or_manifest_failures(self):
        self.write(self.install / 'extra/vcruntime140-unreviewed.dll', b'extra synthetic CRT')
        expected = self.install / runtime.CORE / runtime.NUMPY_MEMBER
        expected.write_bytes(b'wrong expected member')
        with self.assertRaisesRegex(RuntimeError, 'differs from reviewed'): self.collect()
        expected.write_bytes(self.alias_bytes)
        self.manifest['msvc_runtime_files'][0]['path'] = '../private-secret.dll'
        self.seal_manifest()
        with self.assertRaisesRegex(RuntimeError, 'manifest CRT path'): self.collect()

    def test_case_aliases_fail_without_overwriting_case_insensitive_files(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
        entries = [SimpleNamespace(name=name, path=str(self.install / runtime.CORE / name),
            stat=lambda **kwargs: info) for name in ('MSVCP140.dll', 'msvcp140.dll')]
        class Scan:
            def __enter__(self): return iter(entries)
            def __exit__(self, *args): pass
        with patch.object(runtime.os, 'scandir', return_value=Scan()), self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            runtime.installed_crt_files(self.install)

    def test_file_growth_cannot_turn_a_small_stat_into_unbounded_hashing(self):
        target = self.root / 'growing'
        target.write_bytes(b'x')
        original_open = Path.open
        def growing_open(path, *args, **kwargs):
            if path == target and args == ('rb',):
                with original_open(path, 'ab') as stream: stream.write(b'y' * 20)
            return original_open(path, *args, **kwargs)
        with patch.object(runtime, 'MAX_FILE_BYTES', 8), patch.object(Path, 'open', new=growing_open), \
             self.assertRaisesRegex(RuntimeError, 'grew beyond read bound'):
            runtime.file_evidence(target)

    def test_malicious_manifest_paths_types_duplicates_and_unbounded_fields_fail(self):
        original = copy.deepcopy(self.manifest)
        for value in ('../MSVCP140.dll', '/MSVCP140.dll', 'C:\\private\\MSVCP140.dll',
                      'MSVCP140.dll:secret', 'MSVCP140.dll\x00', 'MSVCP140.dll/' , 'X' * 10000):
            self.manifest = copy.deepcopy(original)
            self.manifest['msvc_runtime_files'][0]['path'] = value
            self.seal_manifest()
            with self.subTest(path=value[:40]), self.assertRaises(RuntimeError): self.collect()
        for key, value in (('size', True), ('size', 0), ('size', runtime.MAX_FILE_BYTES + 1),
                           ('sha256', 'A' * 64), ('machine', 'private-data'), ('secret', 'runner-user')):
            self.manifest = copy.deepcopy(original)
            self.manifest['msvc_runtime_files'][0][key] = value
            self.seal_manifest()
            with self.subTest(key=key), self.assertRaises(RuntimeError): self.collect()
        self.manifest = copy.deepcopy(original)
        self.manifest['msvc_runtime_files'][1] = self.manifest['msvc_runtime_files'][0]
        self.seal_manifest()
        with self.assertRaises(RuntimeError): self.collect()

    def test_duplicate_json_keys_and_oversize_manifest_fail(self):
        path = self.install / runtime.MANIFEST
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(RuntimeError): self.collect()
        path.write_bytes(b' ' * (runtime.MAX_MANIFEST_BYTES + 1))
        with self.assertRaises(RuntimeError): self.collect()

    def test_symlinks_broken_symlinks_and_reparse_points_fail(self):
        link = self.install / 'linked'
        try:
            link.symlink_to(self.system, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(RuntimeError): self.collect()
        link.unlink()
        notice = self.install / 'LICENSES.chromium.html'
        notice.symlink_to(self.root / 'absent')
        with self.assertRaises(RuntimeError): self.collect()
        notice.unlink()

    def test_reparse_points_fail_even_when_platform_cannot_create_symlinks(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        entry = SimpleNamespace(stat=lambda **kwargs: info)
        class Scan:
            def __enter__(self): return iter([entry])
            def __exit__(self, *args): pass
        with patch.object(runtime.os, 'scandir', return_value=Scan()), self.assertRaises(RuntimeError): self.collect()

    def test_file_directory_depth_and_total_summary_bounds_fail_closed(self):
        with patch.object(runtime, 'MAX_FILE_BYTES', 10), self.assertRaises(RuntimeError): self.collect()
        with patch.object(runtime, 'MAX_ENTRIES', 2), self.assertRaises(RuntimeError): self.collect()
        with patch.object(runtime, 'MAX_DEPTH', 1), self.assertRaises(RuntimeError): self.collect()
        with patch.object(runtime, 'MAX_INVENTORY_BYTES', 10), self.assertRaises(RuntimeError): self.collect()
        with self.assertRaises(RuntimeError): runtime.inventory_digest({'private': 'x' * 65536})

    def test_numeric_metadata_cannot_smuggle_strings_paths_booleans_or_free_text(self):
        good = {'kind': 'embedded-pe-fixed-resource', 'file_version': [1, 2, 3, 4], 'product_version': [1, 2, 3, 4]}
        mutations = [dict(good, kind='C:/Users/private'), dict(good, file_version='14.44.35112.0'),
            dict(good, product_version=[1, 2, 3, 'private']), dict(good, file_version=[True, 0, 0, 0]),
            dict(good, file_version=[65536, 0, 0, 0]), dict(good, private='metadata')]
        for mutation in mutations:
            with patch.object(runtime, 'fixed_versions', return_value=mutation), self.assertRaises(RuntimeError): self.collect()
        with patch.object(runtime, 'fixed_versions', return_value={'kind': 'unavailable', 'file_version': None,
                'product_version': None}):
            self.assertIsNone(self.collect()['crt_files'][0]['versions']['file_version'])

    def test_inaccessible_notices_and_cpython_candidates_are_not_reported_absent(self):
        original_lstat = Path.lstat
        for target in (self.python / 'LICENSE.txt', self.python / 'VCRUNTIME140.dll'):
            def blocked_lstat(path, *args, **kwargs):
                if path == target: raise PermissionError('synthetic denied file')
                return original_lstat(path, *args, **kwargs)
            with self.subTest(candidate=target.name), patch.object(Path, 'lstat', new=blocked_lstat), \
                 self.assertRaises(PermissionError):
                self.collect()

    def test_mocked_win32_fixed_resource_api_accepts_numbers_and_rejects_unsafe_buffers(self):
        fields = [0xFEEF04BD, 0x10000, (14 << 16) | 44, (35112 << 16), (14 << 16) | 40, (33810 << 16)] + [0] * 7
        payload = struct.pack('<13I', *fields)
        def size_fn(path, unused): return len(payload)
        def read_fn(path, handle, size, data):
            ctypes.memmove(data, payload, len(payload))
            return 1
        def query_fn(data, key, address, length):
            self.assertEqual(key, '\\')
            address._obj.value = ctypes.addressof(data)
            length._obj.value = len(payload)
            return 1
        api = SimpleNamespace(GetFileVersionInfoSizeW=size_fn, GetFileVersionInfoW=read_fn, VerQueryValueW=query_fn)
        target = self.install / runtime.CORE / runtime.TRIO[0]
        with patch.object(runtime.sys, 'platform', 'win32'), patch.object(runtime.ctypes, 'WinDLL', return_value=api, create=True):
            result = REAL_FIXED_VERSIONS(target)
            self.assertEqual(result['file_version'], [14, 44, 35112, 0])
            self.assertEqual(result['product_version'], [14, 40, 33810, 0])
            api.GetFileVersionInfoSizeW = lambda *args: 0
            self.assertEqual(REAL_FIXED_VERSIONS(target)['kind'], 'unavailable')
            api.GetFileVersionInfoSizeW = lambda *args: 65537
            with self.assertRaises(RuntimeError): REAL_FIXED_VERSIONS(target)
            api.GetFileVersionInfoSizeW = size_fn
            def invalid_query(data, key, address, length):
                address._obj.value = ctypes.addressof(data) + len(payload)
                length._obj.value = len(payload)
                return 1
            api.VerQueryValueW = invalid_query
            with self.assertRaises(RuntimeError): REAL_FIXED_VERSIONS(target)

    def test_changed_bytes_while_reading_versions_fail(self):
        def mutate(path):
            path.write_bytes(b'changed while querying version resource')
            return {'kind': 'unavailable', 'file_version': None, 'product_version': None}
        with patch.object(runtime, 'fixed_versions', side_effect=mutate), self.assertRaises(RuntimeError): self.collect()

    def test_inventory_digest_detects_byte_version_and_notice_changes(self):
        value = self.collect()
        baseline = runtime.inventory_digest(value)
        for key in ('size', 'sha256', 'versions', 'provenance'):
            changed = copy.deepcopy(value)
            changed['crt_files'][0][key] = None
            self.assertNotEqual(baseline, runtime.inventory_digest(changed))
        for key in ('status', 'reason', 'unreviewed_crt_file_count', 'missing_reviewed_crt_file_count'):
            changed = copy.deepcopy(value)
            changed[key] = None
            self.assertNotEqual(baseline, runtime.inventory_digest(changed))
        self.write(self.python / 'LICENSE.txt', b'changed notice')
        self.assertNotEqual(baseline, runtime.inventory_digest(self.collect()))

    def test_export_rechecks_inventory_freshness_and_keeps_exact_three_bounded_jsons(self):
        from test_public_ci import identity
        state = {'run': {'run_id': '1'}, 'source_provenance': identity(), 'nonce': 'f' * 32, 'started_ns': 1}
        self.write(self.install / 'unreviewed/vcruntime140-private-name.dll', b'synthetic extra')
        value = self.collect()
        self.assertEqual(value['status'], 'incomplete')
        hashes = {'installed_core_sha256': 'a' * 64, 'runtime_inventory_sha256': runtime.inventory_digest(value)}
        stage = {'hashes': hashes}
        with patch.object(ci, 'verify_run_state', return_value=state), \
             patch.object(ci, 'phase_status', side_effect=lambda state, name: 'passed' if name == 'installed' else 'not-run'), \
             patch.object(ci, 'state_root', return_value=self.root), patch.object(ci, 'source_identity', return_value=identity()), \
             patch.object(ci, 'installed_inventory', return_value=value), patch.object(ci, 'installed_hashes', return_value=hashes) as actual, \
             patch.object(ci, 'read_json', return_value=stage), patch.object(ci.runpy, 'run_path'), \
             patch.object(ci, 'failure_diagnostics', return_value=[]):
            ci.export()
            actual.assert_called_once_with(value)
        public = self.root / 'public'
        self.assertEqual(set(p.name for p in public.iterdir()), set(common.PUBLIC_FILES))
        for path in public.iterdir(): self.assertLessEqual(path.stat().st_size, 65536)
        proof = json.loads((public / 'installed-acceptance-proof.json').read_text())
        self.assertEqual(proof['runtime_inventory'], value)
        self.assertEqual(proof['status'], 'passed')
        self.assertEqual(proof['runtime_inventory']['status'], 'incomplete')
        self.assertNotIn('private-name', json.dumps(proof))
        self.assertEqual(proof['hashes']['runtime_inventory_sha256'], runtime.inventory_digest(value))
        with patch.object(ci, 'verify_run_state', return_value=state), \
             patch.object(ci, 'phase_status', side_effect=lambda state, name: 'passed' if name == 'installed' else 'not-run'), \
             patch.object(ci, 'installed_inventory', return_value=value), patch.object(ci, 'installed_hashes', return_value=hashes), \
             patch.object(ci, 'state_root', return_value=self.root), \
             patch.object(ci, 'read_json', return_value={'hashes': dict(hashes, runtime_inventory_sha256='0' * 64)}), \
             patch.object(ci.runpy, 'run_path'), self.assertRaisesRegex(RuntimeError, 'Installed bytes changed'):
            ci.export()

    def test_no_product_import_or_packaging_change_and_new_contract_is_registered(self):
        text = (HERE / 'public_ci_runtime.py').read_text()
        for forbidden in ('import openvino', 'import numpy', 'opt_out(', 'subprocess', 'SystemRoot='):
            self.assertNotIn(forbidden, text)
        self.assertIn("'test_public_ci_runtime.py'", (HERE / 'public_ci.py').read_text())
        self.assertEqual(common.PUBLIC_FILES, ('run-summary.json', 'source-build-proof.json', 'installed-acceptance-proof.json'))


if __name__ == '__main__':
    unittest.main()
