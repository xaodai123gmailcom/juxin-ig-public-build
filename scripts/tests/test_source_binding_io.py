"""Adversarial shared byte-reader tests; no network, application imports, or native runtime."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding = load('source_binding_io')
class GenericByteFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='public-source-bytes-中文-')))
        target = self.root / 'scripts/added.py'; target.parent.mkdir(); target.write_bytes(b'# synthetic helper\n')
        self.expected = {'scripts/added.py': binding._sha256(target.read_bytes())}
    def verify(self):
        for name, expected in self.expected.items():
            binding._require(binding._sha256(binding._read(self.root, name)) == expected, 'Effective source checksum mismatch')
        return dict(self.expected)
    def reject(self, pattern):
        with self.assertRaisesRegex(binding.SourceBindingError, pattern): self.verify()

class ByteReaderTests(GenericByteFixture):
    def test_duplicate_keys_nonfinite_and_nonobject_json_rejected(self):
        for bad in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}', b'[]', b'null'):
            with self.subTest(data=bad), self.assertRaises(binding.SourceBindingError): binding._json(bad, 'fixture')
    def test_windows_aliases_escape_and_case_collision_rejected(self):
        for name in ('../escape.py', '/absolute.py', 'C:/escape.py', 'C:escape.py',
                     'scripts\\escape.py', 'scripts//escape.py', 'scripts/./escape.py',
                     'scripts/escape.py:stream', 'scripts/CON', 'scripts/escape.py.', 'scripts/a\x00.py',
                     'scripts/a?.py', 'scripts/added.py/..'):
            with self.subTest(name=name), self.assertRaisesRegex(binding.SourceBindingError, 'Invalid source path'): binding._safe_name(name)
        with self.assertRaisesRegex(binding.SourceBindingError, 'Case-colliding'):
            binding._manifest(json.dumps({'scripts/a.py':'a'*64,'scripts/A.py':'a'*64}).encode(), 'fixture')
    def test_symlinked_file_parent_and_root_fail(self):
        target = self.root / 'scripts/added.py'; original=target.read_bytes()
        outside=self.root/'outside.py';outside.write_bytes(original);target.unlink()
        try: target.symlink_to(outside)
        except OSError as error: self.skipTest('Symlink creation unavailable: '+str(error))
        self.reject('Symlink/reparse');target.unlink();target.write_bytes(original)
        scripts=self.root/'scripts';moved=self.root/'actual-scripts';scripts.rename(moved);scripts.symlink_to(moved,target_is_directory=True)
        self.reject('Symlink/reparse');scripts.unlink();moved.rename(scripts)
        link=self.root/'linked-root';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(binding.SourceBindingError,'Symlink/reparse'): binding._read(link,'scripts/added.py')
    def test_hardlinked_file_rejected(self):
        linked=self.root/'extra-link';os.link(self.root/'scripts/added.py',linked);self.reject('Hardlinked')
    def test_nonregular_file_rejected_without_blocking(self):
        if not hasattr(os,'mkfifo'): self.skipTest('FIFO unavailable')
        target=self.root/'scripts/added.py';target.unlink();os.mkfifo(target);self.reject('not a regular file')
    def test_reparse_point_attributes_on_file_or_parent_fail(self):
        original = Path.lstat
        for target in (self.root / 'scripts/added.py', self.root / 'scripts', self.root):
            def fake_lstat(path):
                value = original(path)
                if path == target:
                    names = ('st_mode', 'st_nlink', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                    return SimpleNamespace(**{key: getattr(value, key) for key in names},
                        st_file_attributes=binding.REPARSE_POINT)
                return value
            with self.subTest(target=target), patch.object(Path, 'lstat', fake_lstat):
                self.reject('Symlink/reparse')

class WindowsStatSemanticsTests(GenericByteFixture):
    """Reproduce CPython Windows lstat creation-time vs fstat ChangeTime."""
    TARGET = 'scripts/added.py'

    @contextmanager
    def windows_semantics(self, *, path_mutator=None, fd_mutator=None, birthtime=True):
        real_lstat, real_fstat = Path.lstat, os.fstat
        counts = {'path': 0, 'fd': 0}
        target = self.root / self.TARGET

        def snapshot(value, *, by_fd):
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode', 'st_nlink')
            info = SimpleNamespace(**{name: getattr(value, name) for name in fields},
                st_ctime_ns=2_000_000_000 if by_fd else 1_000_000_000,
                st_file_attributes=getattr(value, 'st_file_attributes', 0))
            if birthtime:
                info.st_birthtime_ns = 1_000_000_000
            return info

        def lstat(path):
            info = snapshot(real_lstat(path), by_fd=False)
            if path == target:
                index = counts['path']; counts['path'] += 1
                if path_mutator:
                    path_mutator(info, index)
            return info

        def fstat(descriptor):
            info = snapshot(real_fstat(descriptor), by_fd=True)
            index = counts['fd']; counts['fd'] += 1
            if fd_mutator:
                fd_mutator(info, index)
            return info

        with patch.object(binding, 'IS_WINDOWS', True), patch.object(Path, 'lstat', lstat), \
                patch.object(os, 'fstat', fstat):
            yield

    @staticmethod
    def change(field, *, at=0):
        def mutate(info, index):
            if index == at:
                setattr(info, field, getattr(info, field) + 1)
        return mutate

    def test_windows_creation_vs_change_time_passes_full_inventory_and_real_reads(self):
        expected = self.verify()
        with self.windows_semantics():
            path = self.root / self.TARGET
            with path.open('rb') as source:
                by_path, by_fd = path.lstat(), os.fstat(source.fileno())
            self.assertNotEqual(by_path.st_ctime_ns, by_fd.st_ctime_ns)
            self.assertEqual(by_path.st_birthtime_ns, by_fd.st_birthtime_ns)
            self.assertEqual(binding._open_identity(by_path), binding._open_identity(by_fd))
            self.assertNotEqual(binding._identity(by_path), binding._identity(by_fd))
            self.assertEqual(expected, self.verify())
            self.assertEqual(path.read_bytes(), binding._read(self.root, self.TARGET))

    def test_windows_without_birthtime_still_checks_compatible_open_fields(self):
        with self.windows_semantics(birthtime=False):
            self.assertEqual((self.root / self.TARGET).read_bytes(), binding._read(self.root, self.TARGET))

    def test_windows_open_rejects_changed_device_inode_size_mtime_or_birthtime(self):
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field), self.windows_semantics(fd_mutator=self.change(field)), \
                    self.assertRaisesRegex(binding.SourceBindingError, 'changed while opening'):
                binding._read(self.root, self.TARGET)

    def test_windows_fd_before_after_still_rejects_ctime_and_identity_changes(self):
        for field in ('st_ctime_ns', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field), self.windows_semantics(fd_mutator=self.change(field, at=1)), \
                    self.assertRaisesRegex(binding.SourceBindingError, 'changed while reading'):
                binding._read(self.root, self.TARGET)

    def test_windows_path_before_after_still_rejects_ctime_and_identity_changes(self):
        for field in ('st_ctime_ns', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field), self.windows_semantics(path_mutator=self.change(field, at=1)), \
                    self.assertRaisesRegex(binding.SourceBindingError, 'changed while reading'):
                binding._read(self.root, self.TARGET)

    def test_windows_open_rejects_a_real_swapped_target_with_identical_contents(self):
        target = self.root / self.TARGET
        replacement = self.root.parent / 'replacement.py'
        replacement.write_bytes(target.read_bytes())
        os.utime(replacement, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns))
        real_open = os.open
        def swapped_open(path, flags, *args, **kwargs):
            if Path(path) == target:
                replacement.replace(target)
            return real_open(path, flags, *args, **kwargs)
        with self.windows_semantics(), patch.object(os, 'open', swapped_open), \
                self.assertRaisesRegex(binding.SourceBindingError, 'changed while opening'):
            binding._read(self.root, self.TARGET)

    def test_windows_ctime_compatibility_does_not_weaken_link_or_file_type_guards(self):
        cases = (
            ('st_mode', stat.S_IFLNK | 0o644, 'Symlink/reparse'),
            ('st_mode', stat.S_IFDIR | 0o755, 'not a regular file'),
            ('st_file_attributes', binding.REPARSE_POINT, 'Symlink/reparse'),
            ('st_nlink', 2, 'Hardlinked'),
        )
        for field, value, message in cases:
            def mutate(info, index):
                if index == 0:
                    setattr(info, field, value)
            with self.subTest(field=field, value=value), self.windows_semantics(path_mutator=mutate), \
                    self.assertRaisesRegex(binding.SourceBindingError, message):
                binding._read(self.root, self.TARGET)
        with self.windows_semantics(fd_mutator=self.change('st_nlink')), \
                self.assertRaisesRegex(binding.SourceBindingError, 'changed while opening'):
            binding._read(self.root, self.TARGET)

    def test_windows_creation_time_difference_does_not_hide_byte_corruption(self):
        target = self.root / self.TARGET
        data = target.read_bytes()
        target.write_bytes(b'!' + data[1:])
        with self.windows_semantics():
            self.reject('Effective source checksum mismatch')

    def test_nonwindows_cross_api_ctime_comparison_is_unchanged(self):
        with self.windows_semantics(), patch.object(binding, 'IS_WINDOWS', False), \
                self.assertRaisesRegex(binding.SourceBindingError, 'changed while opening'):
            binding._read(self.root, self.TARGET)


if __name__ == "__main__": unittest.main()
