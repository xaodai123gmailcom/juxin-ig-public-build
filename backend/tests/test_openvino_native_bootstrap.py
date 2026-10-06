from __future__ import annotations

import contextlib
import base64
import ctypes
import hashlib
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import openvino_native_bootstrap as bootstrap


def _pe_payload(marker: bytes = b"", *, machine: int = 0x8664) -> bytes:
    payload = bytearray(256)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    return bytes(payload) + marker


def _entry(source_root: Path, filename: str, payload: bytes) -> bootstrap.NativeDll:
    path = source_root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    pe_payload = _pe_payload(payload)
    path.write_bytes(pe_payload)
    return bootstrap.NativeDll(
        relative_path=f"openvino/libs/{filename}",
        size=len(pe_payload),
        sha256=hashlib.sha256(pe_payload).hexdigest(),
        source_path=path,
    )


def _source(root: Path) -> bootstrap.NativeSource:
    return bootstrap._verified_native_source(
        bootstrap.EXPECTED_OPENVINO_VERSION,
        (
            _entry(root, "tbb12.dll", b"trusted-tbb"),
            _entry(root, "openvino.dll", b"trusted-openvino"),
            _entry(root, "openvino_ir_frontend.dll", b"trusted-ir"),
            _entry(root, "openvino_intel_cpu_plugin.dll", b"trusted-cpu"),
        ),
    )


@contextlib.contextmanager
def _unlocked_cache(_root: Path):
    yield


class _DirectoryHandle:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeWinFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class _RecordPath(str):
    def __new__(cls, value: str, payload: bytes):
        instance = str.__new__(cls, value)
        digest = hashlib.sha256(payload).digest()
        instance.hash = SimpleNamespace(
            mode="sha256",
            value=base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
        instance.size = len(payload)
        return instance


class OpenVinoNativeBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        bootstrap._reset_for_tests()

    def tearDown(self) -> None:
        bootstrap._reset_for_tests()
        sys.modules.pop("openvino", None)

    def test_windows_caps_onednn_dispatch_at_avx2_before_openvino_load(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(
                os.environ,
                {"ONEDNN_MAX_CPU_ISA": "AVX512_CORE_AMX"},
                clear=False,
            ),
        ):
            bootstrap._configure_windows_onednn_cpu_isa()
            self.assertEqual("AVX2", os.environ["ONEDNN_MAX_CPU_ISA"])

    def test_non_windows_does_not_change_onednn_dispatch(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(
                os.environ,
                {"ONEDNN_MAX_CPU_ISA": "AVX512_CORE"},
                clear=False,
            ),
        ):
            bootstrap._configure_windows_onednn_cpu_isa()
            self.assertEqual("AVX512_CORE", os.environ["ONEDNN_MAX_CPU_ISA"])

    def test_non_windows_entry_point_fails_closed_without_importing_openvino(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(bootstrap.importlib, "import_module") as importer,
        ):
            with self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "requires Windows",
            ):
                bootstrap.load_openvino()
        importer.assert_not_called()

    def test_cache_path_selection_skips_unicode_and_uses_per_user_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "中文-cache"
            safe = root / "ascii-cache"
            guards: list[_DirectoryHandle] = []

            def policy(path: Path) -> bool:
                return all(ord(character) < 128 for character in str(path))

            def secure(
                base: Path,
                *,
                user_key: str,
                user_sid: str,
            ) -> tuple[Path, tuple[_DirectoryHandle, ...]]:
                del user_sid
                selected = base / f"JuxinIGAC-{user_key}" / "OpenVINO"
                selected.mkdir(parents=True)
                handles = (_DirectoryHandle(), _DirectoryHandle())
                guards.extend(handles)
                return selected.resolve(), handles

            with (
                patch.object(sys, "platform", "win32"),
                patch.object(bootstrap, "_is_strict_ascii_local_path", policy),
                patch.object(bootstrap, "_secure_cache_candidate", side_effect=secure),
            ):
                selected = bootstrap._select_cache_root(
                    [unsafe, safe],
                    user_key="u-" + "a" * 24,
                    user_sid="S-1-5-21-1000",
                )
        self.assertIn("ascii-cache", str(selected))
        self.assertEqual("JuxinIGAC-u-" + "a" * 24, selected.parent.name)
        self.assertNotIn("中文", str(selected))
        self.assertEqual(guards, bootstrap._CACHE_DIRECTORY_HANDLES)

    def test_cache_candidates_include_public_before_system_temp_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": "C:/Users/name/AppData/Local",
                "PUBLIC": "C:/Users/Public",
                "SystemRoot": "C:/Windows",
            },
            clear=True,
        ):
            candidates = [
                str(path).replace("\\", "/")
                for path in bootstrap._cache_base_candidates()
            ]
        self.assertIn("C:/Users/Public", candidates)
        self.assertLess(
            candidates.index("C:/Users/Public"),
            candidates.index("C:/Windows/Temp"),
        )

    def test_cache_path_selection_rejects_no_safe_writable_location(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(bootstrap, "_is_strict_ascii_local_path", return_value=False),
        ):
            with self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "cannot create a private",
            ):
                bootstrap._select_cache_root(
                    [Path("C:/用户")],
                    user_key="u-" + "b" * 24,
                    user_sid="S-1-5-21-1000",
                )

    def test_cache_path_rejects_parent_traversal_and_resolved_unicode_junction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traversal = root / "ascii" / ".." / "elsewhere"
            real = root / "中文-real"
            real.mkdir()
            junction = root / "ascii-link"
            junction.symlink_to(real, target_is_directory=True)

            def policy(path: Path) -> bool:
                return all(ord(character) < 128 for character in str(path))

            with (
                patch.object(sys, "platform", "win32"),
                patch.object(bootstrap, "_is_strict_ascii_local_path", side_effect=policy),
                patch.object(
                    bootstrap,
                    "_secure_cache_candidate",
                    side_effect=bootstrap.OpenVinoNativeBootstrapError(
                        "reparse point"
                    ),
                ),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "cannot create a private",
                ),
            ):
                bootstrap._select_cache_root(
                    [traversal, junction],
                    user_key="u-" + "c" * 24,
                    user_sid="S-1-5-21-1000",
                )

    def test_secure_cache_candidate_rejects_shared_base_reparse_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "real-base"
            target.mkdir()
            linked_base = root / "linked-base"
            linked_base.symlink_to(target, target_is_directory=True)
            with (
                patch.object(
                    bootstrap,
                    "_is_strict_ascii_local_path",
                    return_value=True,
                ),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "reparse point",
                ),
            ):
                bootstrap._secure_cache_candidate(
                    linked_base,
                    user_key="u-" + "f" * 24,
                    user_sid="S-1-5-21-1000",
                )

    def test_atomic_stage_publishes_only_a_fully_verified_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            with patch.object(bootstrap, "_cache_file_lock", _unlocked_cache):
                cache_libs = bootstrap._stage_cache_generation(cache_root, source)

            self.assertTrue(bootstrap._cache_is_valid(cache_libs, source))
            self.assertTrue((cache_libs.parent / bootstrap.CACHE_MANIFEST_NAME).is_file())
            self.assertEqual(
                _pe_payload(b"trusted-openvino"),
                (cache_libs / "openvino.dll").read_bytes(),
            )
            self.assertEqual([], list(cache_root.glob(".*.stage-*")))

    def test_cache_maintenance_keeps_current_and_one_recent_safe_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary) / "cache"
            cache_root.mkdir()
            current = cache_root / ("v2025.4.1-" + "a" * 20)
            previous = cache_root / ("v2025.4.0-" + "b" * 20)
            oldest = cache_root / ("v2024.6.0-" + "c" * 20)
            for path in (current, previous, oldest):
                path.mkdir()
            os.utime(oldest, ns=(1, 1))
            os.utime(previous, ns=(2, 2))
            os.utime(current, ns=(3, 3))

            outside = Path(temporary) / "outside"
            outside.mkdir()
            linked = cache_root / ("v2023.0.0-" + "d" * 20)
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError:
                linked = None

            bootstrap._cleanup_stale_cache_generations_locked(
                cache_root, current, keep_previous=1
            )

            self.assertTrue(current.is_dir())
            self.assertTrue(previous.is_dir())
            self.assertFalse(oldest.exists())
            if linked is not None:
                self.assertTrue(linked.is_symlink())

    def test_cache_validation_rejects_every_extra_or_linked_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            with patch.object(bootstrap, "_cache_file_lock", _unlocked_cache):
                cache_libs = bootstrap._stage_cache_generation(cache_root, source)

            extra_file = cache_libs / "VCRUNTIME140.dll"
            extra_file.write_bytes(b"unmanifested")
            self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
            self.assertIn(
                "unexpected cache file",
                bootstrap._cache_validation_error(cache_libs, source) or "",
            )
            extra_file.unlink()

            extra_directory = cache_libs / "unmanifested"
            extra_directory.mkdir()
            self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
            extra_directory.rmdir()

            linked = cache_libs / "linked.dll"
            try:
                linked.symlink_to(cache_libs / "openvino.dll")
            except OSError as exc:
                # Non-elevated Windows machines may not grant
                # SeCreateSymbolicLinkPrivilege.  The mocked reparse tests still
                # cover rejection there; do not turn an OS policy into a false
                # packaging failure.
                if sys.platform != "win32" or getattr(exc, "winerror", None) != 1314:
                    raise
            else:
                self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
                linked.unlink()

            hard_link = cache_libs / "hard-linked.dll"
            os.link(cache_libs / "openvino.dll", hard_link)
            self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
            hard_link.unlink()
            self.assertTrue(bootstrap._cache_is_valid(cache_libs, source))

            # Keep the extra link outside the generation so tree membership is
            # otherwise exact.  This proves the original file's real link
            # count, rather than only the rejection of an extra cache entry.
            external_hard_link = root / "external-openvino-hard-link.dll"
            os.link(cache_libs / "openvino.dll", external_hard_link)
            self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
            self.assertIn(
                "hard-link count must be 1",
                bootstrap._cache_validation_error(cache_libs, source) or "",
            )
            external_hard_link.unlink()
            self.assertTrue(bootstrap._cache_is_valid(cache_libs, source))

            manifest = cache_libs.parent / bootstrap.CACHE_MANIFEST_NAME
            payload = json.loads(manifest.read_text(encoding="ascii"))
            payload["unsealed"] = True
            manifest.write_text(json.dumps(payload), encoding="ascii")
            self.assertFalse(bootstrap._cache_is_valid(cache_libs, source))
            self.assertIn(
                "manifest content does not match",
                bootstrap._cache_validation_error(cache_libs, source) or "",
            )

    def test_cache_validation_does_not_use_zero_link_direntry_stat_on_windows(self) -> None:
        """Model Windows DirEntry.stat(), whose st_nlink is always zero."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            with patch.object(bootstrap, "_cache_file_lock", _unlocked_cache):
                cache_libs = bootstrap._stage_cache_generation(cache_root, source)

            real_scandir = os.scandir

            class ZeroLinkDirEntry:
                def __init__(self, entry: os.DirEntry[str]) -> None:
                    self._entry = entry
                    self.name = entry.name
                    self.path = entry.path

                def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
                    metadata = self._entry.stat(follow_symlinks=follow_symlinks)
                    values = list(metadata)
                    values[3] = 0
                    return os.stat_result(values)

            @contextlib.contextmanager
            def windows_like_scandir(path: os.PathLike[str] | str):
                with real_scandir(path) as entries:
                    yield iter(ZeroLinkDirEntry(entry) for entry in entries)

            with patch.object(bootstrap.os, "scandir", windows_like_scandir):
                self.assertTrue(bootstrap._cache_is_valid(cache_libs, source))

    def test_private_cache_sddl_is_protected_and_has_only_three_principals(self) -> None:
        sid = "S-1-5-21-1000"
        sddl = bootstrap._private_cache_sddl(sid)
        self.assertTrue(sddl.startswith(f"O:{sid}D:P"))
        self.assertEqual(3, sddl.count("(A;OICI;FA;;;"))
        self.assertIn(f"(A;OICI;FA;;;{sid})", sddl)
        self.assertIn("(A;OICI;FA;;;SY)", sddl)
        self.assertIn("(A;OICI;FA;;;BA)", sddl)

    def test_secure_cache_candidate_protects_first_user_unique_child_and_guards_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            sid = "S-1-5-21-1000"
            key = "u-" + "d" * 24
            created: list[Path] = []
            verified: list[Path] = []
            guards: list[_DirectoryHandle] = []
            events: list[tuple[str, Path]] = []

            def create(path: Path, user_sid: str) -> None:
                self.assertEqual(sid, user_sid)
                path.mkdir()
                created.append(path)
                events.append(("create", path))

            def verify(path: Path, user_sid: str) -> None:
                self.assertEqual(sid, user_sid)
                verified.append(path)
                events.append(("verify", path))

            def guard(path: Path) -> _DirectoryHandle:
                handle = _DirectoryHandle()
                guards.append(handle)
                events.append(("guard", path))
                return handle

            with (
                patch.object(
                    bootstrap,
                    "_is_strict_ascii_local_path",
                    return_value=True,
                ),
                patch.object(bootstrap, "_assert_no_reparse_components"),
                patch.object(bootstrap, "_create_private_directory", side_effect=create),
                patch.object(
                    bootstrap,
                    "_verify_private_directory_acl",
                    side_effect=verify,
                ),
                patch.object(bootstrap, "_has_reparse_attribute", return_value=False),
                patch.object(bootstrap, "_open_directory_guard", side_effect=guard),
                patch.object(bootstrap, "_assert_volume_supports_persistent_acls"),
            ):
                selected, retained = bootstrap._secure_cache_candidate(
                    base,
                    user_key=key,
                    user_sid=sid,
                )

        user_root = base / f"JuxinIGAC-{key}"
        self.assertEqual([user_root, user_root / "OpenVINO"], created)
        self.assertEqual(
            [user_root, user_root / "OpenVINO"],
            verified,
        )
        self.assertEqual(
            [
                ("guard", base),
                ("create", user_root),
                ("guard", user_root),
                ("verify", user_root),
                ("create", user_root / "OpenVINO"),
                ("guard", user_root / "OpenVINO"),
                ("verify", user_root / "OpenVINO"),
            ],
            events,
        )
        self.assertEqual(user_root / "OpenVINO", selected)
        # The base handle does not alter its shared ACL.  Retaining all three
        # guards blocks ancestor rename and FILE_DELETE_CHILD replacement after
        # the exact private ACL validation.
        self.assertEqual(tuple(guards), retained)
        self.assertTrue(all(not handle.closed for handle in guards))

    def test_cache_selection_skips_an_existing_insecure_user_tree(self) -> None:
        sid = "S-1-5-21-1000"
        key = "u-" + "e" * 24
        first = Path("C:/ProgramData")
        second = Path("C:/Users/Public")
        expected = second / f"JuxinIGAC-{key}" / "OpenVINO"
        guards = (_DirectoryHandle(), _DirectoryHandle())
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(bootstrap, "_is_strict_ascii_local_path", return_value=True),
            patch.object(
                bootstrap,
                "_secure_cache_candidate",
                side_effect=[
                    bootstrap.OpenVinoNativeBootstrapError("unsafe owner or inherited DACL"),
                    (expected, guards),
                ],
            ),
        ):
            selected = bootstrap._select_cache_root(
                [first, second],
                user_key=key,
                user_sid=sid,
            )
        self.assertEqual(expected, selected)
        self.assertEqual(list(guards), bootstrap._CACHE_DIRECTORY_HANDLES)

    def test_corrupt_existing_cache_is_repaired_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            with patch.object(bootstrap, "_cache_file_lock", _unlocked_cache):
                first = bootstrap._stage_cache_generation(cache_root, source)
                corrupted = first / "openvino.dll"
                corrupted.write_bytes(b"x" * corrupted.stat().st_size)
                second = bootstrap._stage_cache_generation(cache_root, source)

            self.assertEqual(first, second)
            self.assertEqual(_pe_payload(b"trusted-openvino"), corrupted.read_bytes())
            self.assertTrue(bootstrap._cache_is_valid(second, source))

    def test_source_change_during_copy_never_publishes_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            (root / "source" / "openvino.dll").write_bytes(
                _pe_payload(b"changed-after-check")
            )
            with (
                patch.object(bootstrap, "_cache_file_lock", _unlocked_cache),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError, "changed while staging"
                ),
            ):
                bootstrap._stage_cache_generation(cache_root, source)
            self.assertEqual([], [path for path in cache_root.iterdir()])

    def test_native_source_rejects_non_pe_and_non_x64_dlls(self) -> None:
        for payload, error in (
            (b"not-a-pe", "not a PE file"),
            (_pe_payload(b"arm", machine=0xAA64), "not Windows x64 PE"),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                entries = list(_source(root).entries)
                plugin = root / "openvino_intel_cpu_plugin.dll"
                plugin.write_bytes(payload)
                entries = [
                    bootstrap.NativeDll(
                        entry.relative_path,
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                        plugin,
                    )
                    if entry.relative_path.endswith("openvino_intel_cpu_plugin.dll")
                    else entry
                    for entry in entries
                ]
                with self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError, error
                ):
                    bootstrap._verified_native_source(
                        bootstrap.EXPECTED_OPENVINO_VERSION, entries
                    )

    def test_native_source_requires_ir_frontend_and_cpu_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _source(Path(temporary))
            for missing_name in (
                "openvino_ir_frontend.dll",
                "openvino_intel_cpu_plugin.dll",
            ):
                with self.subTest(missing=missing_name), self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError, missing_name
                ):
                    bootstrap._validate_entry_set(
                        [
                            entry
                            for entry in source.entries
                            if not entry.relative_path.endswith(missing_name)
                        ]
                    )

    def test_installed_source_uses_record_without_importing_openvino(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records: list[_RecordPath] = []
            for name in bootstrap.REQUIRED_RUNTIME_DLLS:
                payload = _pe_payload(name.encode("ascii"))
                path = root / "openvino" / "libs" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                records.append(_RecordPath(f"openvino/libs/{name}", payload))
            # pip legitimately records Windows console launchers relative to
            # site-packages.  They are not part of the OpenVINO DLL trust set.
            records.extend(
                (
                    _RecordPath("../../../Scripts/benchmark_app.exe", b"launcher"),
                    _RecordPath("../../../Scripts/ovc.exe", b"launcher"),
                )
            )
            distribution = SimpleNamespace(
                version=bootstrap.EXPECTED_OPENVINO_VERSION,
                files=records,
                locate_file=lambda record: root / str(record),
            )
            with (
                patch.object(
                    bootstrap.importlib.metadata,
                    "distribution",
                    return_value=distribution,
                ),
                patch.object(bootstrap.importlib, "import_module") as importer,
            ):
                source = bootstrap._installed_native_source()
        self.assertEqual(len(bootstrap.REQUIRED_RUNTIME_DLLS), len(source.entries))
        importer.assert_not_called()

    def test_installed_record_still_rejects_traversal_below_dll_prefix(self) -> None:
        payload = _pe_payload(b"escape")
        distribution = SimpleNamespace(
            version=bootstrap.EXPECTED_OPENVINO_VERSION,
            files=[_RecordPath("openvino/libs/../escape.dll", payload)],
            locate_file=lambda record: Path("unused") / str(record),
        )
        with (
            patch.object(
                bootstrap.importlib.metadata,
                "distribution",
                return_value=distribution,
            ),
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "unsafe path",
            ),
        ):
            bootstrap._installed_native_source()

    def test_installed_record_selector_ignores_lookalikes_but_keeps_dlls(self) -> None:
        self.assertTrue(
            bootstrap._is_installed_dll_record("OpenVINO\\Libs\\openvino.dll")
        )
        for path in (
            "../../../Scripts/benchmark_app.exe",
            "../../../Scripts/not-loaded.dll",
            "openvino/libs-evil/openvino.dll",
            "openvino/libs/openvino.dll.exe",
            "openvino/_pyopenvino.pyd",
            "openvino/libſ/openvino.dll",
            "openvino/libs/ſ.dll",
        ):
            with self.subTest(path=path):
                self.assertFalse(bootstrap._is_installed_dll_record(path))

    def test_native_path_normalizer_rejects_aliases_and_absolute_paths(self) -> None:
        for path in (
            "openvino/libs//openvino.dll",
            "openvino/libs/./openvino.dll",
            "openvino/libs/../openvino.dll",
            "openvino/libs/nested/openvino.dll",
            "openvino/libs/openvino.dll:shadow.dll",
            "openvino/libs/openvino .dll",
            "openvino/libſ/openvino.dll",
            "openvino/libs/ſ.dll",
            "C:/openvino/libs/openvino.dll",
            "/openvino/libs/openvino.dll",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                r"unsafe (?:path|DLL name)",
            ):
                bootstrap._normalized_dll_path(path)

    def test_installed_record_rejects_invalid_size_hash_and_duplicate(self) -> None:
        payload = _pe_payload(b"metadata")
        bad_size = _RecordPath("openvino/libs/openvino.dll", payload)
        bad_size.size = 0
        bad_hash = _RecordPath("openvino/libs/openvino.dll", payload)
        bad_hash.hash = SimpleNamespace(mode="sha256", value="!" * 43)
        for record, error in (
            (bad_size, "no valid size"),
            (bad_hash, "invalid SHA-256"),
        ):
            distribution = SimpleNamespace(
                version=bootstrap.EXPECTED_OPENVINO_VERSION,
                files=[record],
                locate_file=lambda selected: Path("unused") / str(selected),
            )
            with (
                self.subTest(error=error),
                patch.object(
                    bootstrap.importlib.metadata,
                    "distribution",
                    return_value=distribution,
                ),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    error,
                ),
            ):
                bootstrap._installed_native_source()

        digest = base64.urlsafe_b64encode(b"\x00" * 32).decode("ascii").rstrip("=")
        noncanonical = digest[:-1] + "B"
        self.assertEqual(
            base64.urlsafe_b64decode(digest + "="),
            base64.urlsafe_b64decode(noncanonical + "="),
        )
        with self.assertRaisesRegex(
            bootstrap.OpenVinoNativeBootstrapError,
            "non-canonical SHA-256",
        ):
            bootstrap._decode_record_hash(
                SimpleNamespace(mode="sha256", value=noncanonical)
            )

        with tempfile.TemporaryDirectory() as temporary:
            source = _source(Path(temporary))
            with self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "duplicate DLL paths",
            ):
                bootstrap._validate_entry_set([*source.entries, source.entries[0]])

    def test_required_runtime_cannot_be_satisfied_by_nested_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _source(Path(temporary))
            entries = list(source.entries)
            target = next(
                entry
                for entry in entries
                if entry.relative_path.endswith("openvino.dll")
            )
            entries.remove(target)
            entries.append(
                bootstrap.NativeDll(
                    "openvino/libs/nested/openvino.dll",
                    target.size,
                    target.sha256,
                    target.source_path,
                )
            )
            with self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "missing: openvino.dll",
            ):
                bootstrap._validate_entry_set(entries)

    def test_non_ascii_source_test_gate_proves_original_dll_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ascii_source = _source(root / "ascii-source")
            unicode_source = _source(root / "中文-source")
            with (
                patch.object(bootstrap, "_installed_native_source", return_value=ascii_source),
                patch.dict(
                    os.environ,
                    {"IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE": "1"},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "requires the original openvino.dll source path",
                ),
            ):
                bootstrap._resolve_native_source()
            with (
                patch.object(bootstrap, "_installed_native_source", return_value=unicode_source),
                patch.dict(
                    os.environ,
                    {"IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE": "1"},
                    clear=False,
                ),
            ):
                self.assertIs(unicode_source, bootstrap._resolve_native_source())

    def test_frozen_manifest_rejects_traversal_invalid_hash_and_missing_preload(self) -> None:
        cases = (
            (
                [
                    {"path": "openvino/libs/../escape.dll", "size": 1, "sha256": "0" * 64}
                ],
                "unsafe path",
            ),
            (
                [
                    {"path": "openvino/libs/tbb12.dll", "size": 1, "sha256": "bad"},
                    {"path": "openvino/libs/openvino.dll", "size": 1, "sha256": "0" * 64},
                ],
                "invalid SHA-256",
            ),
            (
                [
                    {"path": "openvino/libs/tbb12.dll", "size": 1, "sha256": "0" * 64}
                ],
                "missing: openvino.dll",
            ),
        )
        for files, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                libs = root / "openvino" / "libs"
                libs.mkdir(parents=True)
                for raw in files:
                    relative = str(raw["path"])
                    if ".." not in relative:
                        (libs / Path(relative).name).write_bytes(b"x")
                (root / bootstrap.PACKAGED_MANIFEST_NAME).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "openvino_version": bootstrap.EXPECTED_OPENVINO_VERSION,
                            "files": files,
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    patch.object(sys, "_MEIPASS", str(root), create=True),
                    self.assertRaisesRegex(
                        bootstrap.OpenVinoNativeBootstrapError, error
                    ),
                ):
                    bootstrap._frozen_native_source()

    def test_frozen_manifest_is_bound_only_to_meipass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native_root = root / "meipass"
            native_root.mkdir()
            outside = root / bootstrap.PACKAGED_MANIFEST_NAME
            outside.write_text("{}", encoding="utf-8")
            with (
                patch.object(sys, "executable", str(root / "collector_core.exe")),
                patch.dict(
                    os.environ,
                    {"IGAC_OPENVINO_NATIVE_MANIFEST": str(outside)},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "is missing",
                ),
            ):
                bootstrap._packaged_manifest_path(native_root)

    def test_frozen_bootstrap_rechecks_exact_app_local_msvc_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native_root = Path(temporary)
            source = _source(native_root / "openvino" / "libs")
            files = [
                {
                    "path": entry.relative_path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                }
                for entry in source.entries
            ]
            msvc_entries = []
            for filename in bootstrap.PACKAGED_MSVC_RUNTIME_DLLS:
                payload = _pe_payload(filename.encode("ascii"))
                path = native_root / filename
                path.write_bytes(payload)
                msvc_entries.append(
                    {
                        "path": filename,
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            manifest = {
                "schema_version": 1,
                "openvino_version": bootstrap.EXPECTED_OPENVINO_VERSION,
                "files": files,
                "msvc_runtime_files": msvc_entries,
            }
            (native_root / bootstrap.PACKAGED_MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with patch.object(sys, "_MEIPASS", str(native_root), create=True):
                verified = bootstrap._frozen_native_source()
            self.assertEqual(source.digest, verified.digest)

            tampered = native_root / bootstrap.PACKAGED_MSVC_RUNTIME_DLLS[0]
            damaged = bytearray(tampered.read_bytes())
            damaged[-1] ^= 0xFF
            tampered.write_bytes(damaged)
            with (
                patch.object(sys, "_MEIPASS", str(native_root), create=True),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "MSVC runtime SHA-256 mismatch",
                ),
            ):
                bootstrap._frozen_native_source()

            tampered.unlink()
            with (
                patch.object(sys, "_MEIPASS", str(native_root), create=True),
                self.assertRaisesRegex(
                    bootstrap.OpenVinoNativeBootstrapError,
                    "required app-local MSVC runtime is unavailable",
                ),
            ):
                bootstrap._frozen_native_source()

    def test_loaded_path_assertion_requires_exact_ascii_cache_file(self) -> None:
        cache = Path("C:/ProgramData/Juxin/cache/libs")
        expected = cache / "openvino.dll"
        with (
            patch.object(bootstrap, "_loaded_module_path", return_value=expected),
            patch.object(bootstrap, "_is_strict_ascii_local_path", return_value=True),
        ):
            self.assertEqual(expected, bootstrap.assert_loaded_openvino_from_cache(cache))
        with (
            patch.object(
                bootstrap,
                "_loaded_module_path",
                return_value=Path("C:/Users/name/中文/openvino.dll"),
            ),
            patch.object(bootstrap, "_is_strict_ascii_local_path", return_value=False),
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError, "unexpected path"
            ),
        ):
            bootstrap.assert_loaded_openvino_from_cache(cache)

    def test_loaded_module_winapi_uses_pointer_sized_hmodule_signature(self) -> None:
        large_handle = 0x1234567887654321
        observed: list[int] = []

        def get_filename(module, buffer, size):
            del size
            observed.append(module)
            buffer.value = "C:\\ProgramData\\Juxin\\openvino.dll"
            return len(buffer.value)

        kernel32 = SimpleNamespace(
            GetModuleHandleW=_FakeWinFunction(lambda filename: large_handle),
            GetModuleFileNameW=_FakeWinFunction(get_filename),
        )
        with patch.object(bootstrap.ctypes, "WinDLL", return_value=kernel32, create=True):
            loaded = bootstrap._loaded_module_path("openvino.dll")

        self.assertEqual(Path("C:\\ProgramData\\Juxin\\openvino.dll"), loaded)
        self.assertEqual([large_handle], observed)
        self.assertIs(wintypes.HMODULE, kernel32.GetModuleHandleW.restype)
        self.assertEqual(
            [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD],
            kernel32.GetModuleFileNameW.argtypes,
        )

    def test_directory_guard_pins_normal_directory_without_delete_share(self) -> None:
        large_handle = 0x1234567887654321
        observed: list[tuple[str, int]] = []

        def create_file(path, access, sharing, security, disposition, flags, template):
            del path, security, disposition, template
            observed.extend(
                [
                    ("access", int(access)),
                    ("sharing", int(sharing)),
                    ("flags", int(flags)),
                ]
            )
            return large_handle

        def file_information(handle, info_class, output, size):
            del info_class, size
            observed.append(("inspect", int(handle)))
            information = ctypes.cast(
                output,
                ctypes.POINTER(bootstrap._FileAttributeTagInformation),
            ).contents
            information.FileAttributes = bootstrap._FILE_ATTRIBUTE_DIRECTORY
            information.ReparseTag = 0
            return 1

        kernel32 = SimpleNamespace(
            CreateFileW=_FakeWinFunction(create_file),
            GetFileInformationByHandleEx=_FakeWinFunction(file_information),
            CloseHandle=_FakeWinFunction(
                lambda handle: observed.append(("close", int(handle))) or 1
            ),
        )
        with patch.object(
            bootstrap.ctypes,
            "WinDLL",
            return_value=kernel32,
            create=True,
        ):
            guard = bootstrap._open_directory_guard(Path("C:/ProgramData/private"))
            self.assertFalse(guard.closed)
            guard.close()

        self.assertEqual(
            [
                (
                    "access",
                    bootstrap._FILE_READ_ATTRIBUTES | bootstrap._READ_CONTROL,
                ),
                (
                    "sharing",
                    bootstrap._FILE_SHARE_READ | bootstrap._FILE_SHARE_WRITE,
                ),
                (
                    "flags",
                    bootstrap._FILE_FLAG_BACKUP_SEMANTICS
                    | bootstrap._FILE_FLAG_OPEN_REPARSE_POINT,
                ),
                ("inspect", large_handle),
                ("close", large_handle),
            ],
            observed,
        )
        self.assertIs(wintypes.HANDLE, kernel32.CreateFileW.restype)
        self.assertEqual(wintypes.HANDLE, kernel32.CreateFileW.argtypes[-1])
        self.assertEqual(
            wintypes.HANDLE,
            kernel32.GetFileInformationByHandleEx.argtypes[0],
        )

    def test_cache_volume_must_enforce_persistent_acls(self) -> None:
        large_handle = 0x1234567887654321

        def volume_information(
            handle,
            volume_name,
            volume_name_size,
            serial,
            maximum_component,
            flags,
            filesystem_name,
            filesystem_name_size,
        ):
            del (
                volume_name,
                volume_name_size,
                serial,
                maximum_component,
                filesystem_name,
                filesystem_name_size,
            )
            self.assertEqual(large_handle, int(handle))
            ctypes.cast(flags, ctypes.POINTER(wintypes.DWORD)).contents.value = 0
            return 1

        kernel32 = SimpleNamespace(
            GetVolumeInformationByHandleW=_FakeWinFunction(volume_information),
        )
        guard = SimpleNamespace(handle=large_handle)
        with (
            patch.object(
                bootstrap.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "does not enforce persistent",
            ),
        ):
            bootstrap._assert_volume_supports_persistent_acls(
                guard,
                Path("C:/ProgramData"),
            )

        def secure_volume_information(*args):
            flags = args[5]
            ctypes.cast(flags, ctypes.POINTER(wintypes.DWORD)).contents.value = (
                bootstrap._FILE_PERSISTENT_ACLS
            )
            return 1

        kernel32.GetVolumeInformationByHandleW.implementation = (
            secure_volume_information
        )
        with patch.object(
            bootstrap.ctypes,
            "WinDLL",
            return_value=kernel32,
            create=True,
        ):
            bootstrap._assert_volume_supports_persistent_acls(
                guard,
                Path("C:/ProgramData"),
            )
        self.assertIs(
            wintypes.BOOL,
            kernel32.GetVolumeInformationByHandleW.restype,
        )

    def test_cache_file_lock_is_cross_session_and_uses_no_delete_share(self) -> None:
        large_handle = 0x1234567887654321
        observed: list[tuple[str, int]] = []

        def create_file(path, access, sharing, security, disposition, flags, template):
            del path, access, security, disposition, flags, template
            observed.append(("sharing", int(sharing)))
            return large_handle

        def file_information(handle, info_class, output, size):
            del info_class, size
            observed.append(("inspect", int(handle)))
            information = ctypes.cast(
                output,
                ctypes.POINTER(bootstrap._FileAttributeTagInformation),
            ).contents
            information.FileAttributes = 0
            information.ReparseTag = 0
            return 1

        kernel32 = SimpleNamespace(
            CreateFileW=_FakeWinFunction(create_file),
            GetFileInformationByHandleEx=_FakeWinFunction(file_information),
            LockFileEx=_FakeWinFunction(
                lambda handle, flags, reserved, low, high, overlapped: observed.append(
                    ("lock", int(handle))
                )
                or 1
            ),
            UnlockFileEx=_FakeWinFunction(
                lambda handle, reserved, low, high, overlapped: observed.append(
                    ("unlock", int(handle))
                )
                or 1
            ),
            CloseHandle=_FakeWinFunction(
                lambda handle: observed.append(("close", int(handle))) or 1
            ),
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(bootstrap.ctypes, "WinDLL", return_value=kernel32, create=True),
            bootstrap._cache_file_lock(Path("C:/ProgramData/private-cache")),
        ):
            pass
        self.assertEqual(
            [
                ("sharing", bootstrap._FILE_SHARE_READ | bootstrap._FILE_SHARE_WRITE),
                ("inspect", large_handle),
                ("lock", large_handle),
                ("unlock", large_handle),
                ("close", large_handle),
            ],
            observed,
        )
        self.assertIs(wintypes.HANDLE, kernel32.CreateFileW.restype)
        self.assertEqual(wintypes.HANDLE, kernel32.LockFileEx.argtypes[0])
        self.assertEqual(
            ctypes.POINTER(bootstrap._Overlapped),
            kernel32.LockFileEx.argtypes[-1],
        )

    def test_concurrent_stage_is_serialized_and_copies_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root / "source")
            cache_root = root / "cache"
            cache_root.mkdir()
            mutex = threading.Lock()
            copy_calls: list[str] = []
            real_copy = bootstrap._copy_verified

            @contextlib.contextmanager
            def locked(_name: str):
                with mutex:
                    yield

            def recording_copy(entry: bootstrap.NativeDll, destination: Path) -> None:
                copy_calls.append(entry.relative_path)
                real_copy(entry, destination)

            results: list[Path] = []

            def run() -> None:
                results.append(bootstrap._stage_cache_generation(cache_root, source))

            with (
                patch.object(bootstrap, "_cache_file_lock", locked),
                patch.object(bootstrap, "_copy_verified", recording_copy),
            ):
                threads = [threading.Thread(target=run) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(2, len(results))
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(source.entries), len(copy_calls))

    def test_prepare_preloads_tbb_then_openvino_and_retains_handles(self) -> None:
        cache = Path("C:/ProgramData/Juxin/cache/libs")
        loaded_names: list[str] = []
        directory_handle = _DirectoryHandle()
        generation_guards = [_DirectoryHandle(), _DirectoryHandle()]
        events: list[str] = []

        @contextlib.contextmanager
        def locked(_root: Path):
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        def stage(_root: Path, _source: object) -> Path:
            events.append("stage")
            return cache

        def guard(path: Path) -> _DirectoryHandle:
            events.append(f"guard:{path.name}")
            return generation_guards.pop(0)

        def validation_error(_cache: Path, _source: object) -> None:
            events.append("verify")
            return None

        def load(path: Path) -> object:
            events.append(f"load:{path.name}")
            loaded_names.append(path.name)
            return SimpleNamespace(path=path)

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(
                bootstrap,
                "_loaded_module_path",
                side_effect=[
                    None,
                    None,
                    cache / "tbb12.dll",
                    cache / "openvino.dll",
                ],
            ),
            patch.object(bootstrap, "_resolve_native_source", return_value=Mock()),
            patch.object(bootstrap, "_select_cache_root", return_value=cache.parent),
            patch.object(bootstrap, "_cache_file_lock", locked),
            patch.object(bootstrap, "_stage_cache_generation_locked", side_effect=stage),
            patch.object(bootstrap, "_open_directory_guard", side_effect=guard),
            patch.object(
                bootstrap,
                "_cache_validation_error",
                side_effect=validation_error,
            ),
            patch.object(bootstrap, "_is_strict_ascii_local_path", return_value=True),
            patch.object(bootstrap, "_load_native_dll", side_effect=load),
            patch.object(os, "add_dll_directory", return_value=directory_handle, create=True),
        ):
            selected = bootstrap.prepare_openvino_native_runtime()

        self.assertEqual(cache, selected)
        self.assertEqual(["tbb12.dll", "openvino.dll"], loaded_names)
        self.assertIn(directory_handle, bootstrap._DLL_DIRECTORY_HANDLES)
        self.assertEqual(2, len(bootstrap._NATIVE_LIBRARY_HANDLES))
        self.assertEqual(2, len(bootstrap._CACHE_DIRECTORY_HANDLES))
        self.assertEqual(
            [
                "lock-enter",
                "stage",
                "guard:cache",
                "guard:libs",
                "verify",
                "load:tbb12.dll",
                "load:openvino.dll",
                "lock-exit",
            ],
            events,
        )

    def test_frozen_prepare_registers_meipass_before_ascii_cache(self) -> None:
        cache = Path("C:/ProgramData/Juxin/cache/libs")
        registered: list[str] = []
        directory_handles: list[_DirectoryHandle] = []

        def add_directory(path: str) -> _DirectoryHandle:
            registered.append(path)
            handle = _DirectoryHandle()
            directory_handles.append(handle)
            return handle

        with tempfile.TemporaryDirectory() as temporary:
            native_root = Path(temporary).resolve()
            with (
                patch.object(sys, "platform", "win32"),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(native_root), create=True),
                patch.object(
                    bootstrap,
                    "_loaded_module_path",
                    side_effect=[
                        None,
                        None,
                        cache / "tbb12.dll",
                        cache / "openvino.dll",
                    ],
                ),
                patch.object(bootstrap, "_resolve_native_source", return_value=Mock()),
                patch.object(bootstrap, "_select_cache_root", return_value=cache.parent),
                patch.object(bootstrap, "_cache_file_lock", _unlocked_cache),
                patch.object(
                    bootstrap,
                    "_stage_cache_generation_locked",
                    return_value=cache,
                ),
                patch.object(
                    bootstrap,
                    "_open_directory_guard",
                    side_effect=[_DirectoryHandle(), _DirectoryHandle()],
                ),
                patch.object(
                    bootstrap,
                    "_cache_validation_error",
                    return_value=None,
                ),
                patch.object(
                    bootstrap, "_is_strict_ascii_local_path", return_value=True
                ),
                patch.object(
                    bootstrap,
                    "_load_native_dll",
                    side_effect=lambda path: SimpleNamespace(path=path),
                ),
                patch.object(
                    os,
                    "add_dll_directory",
                    side_effect=add_directory,
                    create=True,
                ),
            ):
                selected = bootstrap.prepare_openvino_native_runtime()

        self.assertEqual(cache, selected)
        self.assertEqual([str(native_root), str(cache)], registered)
        self.assertEqual(directory_handles, bootstrap._DLL_DIRECTORY_HANDLES)
        self.assertTrue(all(not handle.closed for handle in directory_handles))
        self.assertEqual(
            directory_handles,
            getattr(sys, "_igac_openvino_dll_directory_handles"),
        )

    def test_prepare_rejects_openvino_loaded_before_bootstrap(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(
                bootstrap,
                "_loaded_module_path",
                side_effect=[None, Path("C:/unsafe/openvino.dll")],
            ),
            patch.object(bootstrap, "_resolve_native_source") as resolver,
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError, "loaded before"
            ),
        ):
            bootstrap.prepare_openvino_native_runtime()
        resolver.assert_not_called()

    def test_prepare_rejects_tbb_loaded_before_bootstrap(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(
                bootstrap,
                "_loaded_module_path",
                return_value=Path("C:/unsafe/tbb12.dll"),
            ),
            patch.object(bootstrap, "_resolve_native_source") as resolver,
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError,
                "tbb12.dll was loaded before",
            ),
        ):
            bootstrap.prepare_openvino_native_runtime()
        resolver.assert_not_called()

    def test_prepare_rejects_python_package_imported_before_bootstrap(self) -> None:
        sys.modules["openvino"] = SimpleNamespace()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(bootstrap, "_loaded_module_path") as loaded_path,
            self.assertRaisesRegex(
                bootstrap.OpenVinoNativeBootstrapError, "imported before"
            ),
        ):
            bootstrap.prepare_openvino_native_runtime()
        loaded_path.assert_not_called()

    def test_load_openvino_imports_only_after_successful_prepare(self) -> None:
        cache = Path("C:/ProgramData/Juxin/cache/libs")
        module = SimpleNamespace(__version__=bootstrap.EXPECTED_OPENVINO_VERSION)
        events: list[str] = []

        def prepare() -> Path:
            events.append("prepare")
            return cache

        def importer(name: str) -> object:
            events.append(f"import:{name}")
            return module

        with (
            patch.object(bootstrap, "prepare_openvino_native_runtime", side_effect=prepare),
            patch.object(
                bootstrap,
                "assert_openvino_telemetry_disabled",
                side_effect=lambda: events.append("privacy"),
            ),
            patch.object(
                bootstrap,
                "assert_fresh_openvino_import",
                side_effect=lambda: events.append("fresh"),
            ),
            patch.object(bootstrap.importlib, "import_module", side_effect=importer),
            patch.object(
                bootstrap,
                "assert_loaded_openvino_from_cache",
                return_value=cache / "openvino.dll",
            ),
        ):
            self.assertIs(module, bootstrap.load_openvino())
        self.assertEqual(["prepare", "privacy", "fresh", "import:openvino"], events)


if __name__ == "__main__":
    unittest.main()
