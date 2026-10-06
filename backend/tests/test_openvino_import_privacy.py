"""Mock-only privacy preflight tests; never import OpenVINO or telemetry.

The filesystem, native loader, inference, enqueue and transport boundaries are
fakes.  These tests do not establish real-package network suppression and do not
authorize resuming a blocked integration run or changing the host's consent.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "backend"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))


class _ForbidRealOpenVinoImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"openvino", "openvino_telemetry"}:
            raise AssertionError("mock-only test attempted a real OpenVINO import")
        return None


@contextlib.contextmanager
def _mock_only_imports():
    # Refuse an already contaminated interpreter instead of clearing/reloading
    # an actual telemetry singleton and pretending that its consent was revoked.
    if any(
        name.split(".", 1)[0] in {"openvino", "openvino_telemetry"}
        for name in sys.modules
    ):
        raise AssertionError("mock-only privacy tests require a fresh interpreter")
    blocker = _ForbidRealOpenVinoImports()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)


# The verifier imports download_person_models, whose frozen hook can write or
# infer at import time.  Refuse that setup before importing the inspected chain.
if getattr(sys, "frozen", False) or os.environ.get(
    "IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS", ""
).strip():
    raise RuntimeError("mock-only privacy tests require an ordinary source interpreter")
with _mock_only_imports():
    from app import openvino_import_privacy as privacy
    from app import openvino_native_bootstrap as bootstrap
    from scripts import verify_openvino_windows as verifier
    from scripts import verify_runtime_ready as readiness


class OpenVinoImportPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(_mock_only_imports())
        bootstrap._reset_for_tests()
        self.addCleanup(bootstrap._reset_for_tests)
        self.system = self.stack.enter_context(
            patch.object(privacy.platform, "system", return_value="Linux")
        )
        self.home = Path("/mock/home")
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.base_is_dir = self.stack.enter_context(
            patch.object(Path, "is_dir", return_value=True)
        )
        self.opened: list[tuple[Path, str]] = []
        self.value = b"0"

        def read_only_open(path, mode="r", *args, **kwargs):
            self.assertEqual("rb", mode, "privacy guard must only open for reading")
            self.opened.append((path, mode))
            if isinstance(self.value, Exception):
                raise self.value
            return io.BytesIO(self.value)

        self.stack.enter_context(patch.object(Path, "open", new=read_only_open))
        self.writes = [
            self.stack.enter_context(
                patch.object(Path, method, side_effect=AssertionError("unexpected write"))
            )
            for method in ("mkdir", "write_bytes", "write_text", "rename", "replace", "unlink")
        ]
        self.stack.enter_context(
            patch.object(os, "access", side_effect=AssertionError("no writability probe"))
        )
        self.stack.enter_context(
            patch.object(os, "replace", side_effect=AssertionError("unexpected replace"))
        )
        self.stack.enter_context(
            patch.object(os, "putenv", side_effect=AssertionError("unexpected env write"))
        )
        compiled = Mock(return_value={"output": object()})
        compiled.input.return_value = SimpleNamespace(shape=(1, 3, 1, 1))
        core = SimpleNamespace(compile_model=Mock(return_value=compiled))
        self.runtime = SimpleNamespace(Core=Mock(return_value=core), __version__="2025.4.1")
        self.enqueue = Mock()
        self.transport = Mock()

        def unsafe_import(name):
            self.assertEqual("openvino", name)
            # An upstream-like fake would enqueue/send if it were reached with
            # bad consent.  Negative tests must prevent the import altogether.
            if self.value != b"0":
                self.enqueue("fake-import-event")
                self.transport("fake-import-event")
            return self.runtime

        self.importer = self.stack.enter_context(
            patch.object(verifier.importlib, "import_module", side_effect=unsafe_import)
        )
        self.cache = Path("/mock/native-cache/libs")

        def prepare():
            bootstrap._PREPARED_CACHE_LIBS = self.cache
            return self.cache

        self.prepare = self.stack.enter_context(
            patch.object(bootstrap, "prepare_openvino_native_runtime", side_effect=prepare)
        )
        self.native_assert = self.stack.enter_context(
            patch.object(bootstrap, "assert_loaded_openvino_from_cache", return_value=self.cache / "openvino.dll")
        )
        self.stack.enter_context(patch.dict(sys.modules, {
            "numpy": SimpleNamespace(zeros=Mock(return_value=object()), float32=object()),
            "app.person_recognition": SimpleNamespace(exercise_local_openvino_gender_branch=Mock()),
        }))
        self.stack.enter_context(
            patch.object(verifier, "_read_ir_model_from_memory", return_value=object())
        )

    def _assert_nothing_imported_or_sent(self) -> None:
        self.importer.assert_not_called()
        self.runtime.Core.assert_not_called()
        self.enqueue.assert_not_called()
        self.transport.assert_not_called()
        for writer in self.writes:
            writer.assert_not_called()

    def test_exact_disabled_consent_is_read_again_without_writes(self) -> None:
        for _ in range(3):
            self.assertEqual(self.home / "intel/openvino_telemetry", privacy.assert_openvino_telemetry_disabled())
            privacy.assert_fresh_openvino_import()
        self.assertEqual(3, len(self.opened))
        self._assert_nothing_imported_or_sent()

    def test_platform_paths_are_identical_for_source_and_frozen(self) -> None:
        local = "/mock/local-app-data"
        # Patch the mapping itself: no process environment or HOME is relocated.
        with patch.object(privacy.os, "environ", {"LOCALAPPDATA": local}):
            for system, directory in (("Windows", Path(local) / "Intel Corporation"),
                                      ("Linux", self.home / "intel"),
                                      ("Darwin", self.home / "intel")):
                for frozen in (False, True):
                    with self.subTest(system=system, frozen=frozen), patch.object(sys, "frozen", frozen, create=True):
                        self.system.return_value = system
                        self.assertEqual(directory / "openvino_telemetry", privacy.assert_openvino_telemetry_disabled())

    def test_windows_value_is_not_trimmed_into_a_different_path(self) -> None:
        self.system.return_value = "Windows"
        with patch.object(privacy.os, "environ", {"LOCALAPPDATA": "/mock/local "}):
            self.assertEqual(Path("/mock/local ") / "Intel Corporation/openvino_telemetry", privacy.assert_openvino_telemetry_disabled())

    def test_windows_missing_localappdata_fails_without_using_home(self) -> None:
        self.system.return_value = "Windows"
        for environment in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "   "}):
            with self.subTest(environment=environment), patch.object(privacy.os, "environ", environment):
                with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "LOCALAPPDATA is unavailable"):
                    privacy.assert_openvino_telemetry_disabled()
        self.assertEqual([], self.opened)
        self._assert_nothing_imported_or_sent()

    def test_unavailable_base_and_unsupported_platform_fail_closed(self) -> None:
        self.base_is_dir.return_value = False
        with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "base is unavailable"):
            privacy.assert_openvino_telemetry_disabled()
        self.system.return_value = "UnknownOS"
        with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "unsupported"):
            privacy.assert_openvino_telemetry_disabled()
        self.assertEqual([], self.opened)

    def test_home_resolution_failure_is_a_privacy_error(self) -> None:
        with patch.object(Path, "home", side_effect=RuntimeError("no home")):
            with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "path cannot be resolved"):
                privacy.assert_openvino_telemetry_disabled()
        self._assert_nothing_imported_or_sent()

    def test_every_nonzero_or_nonexact_value_is_rejected(self) -> None:
        for value in (b"", b"1", b"2", b"false", b"00", b"0\n", b"0\r\n", b" 0", b"0 ", b"\xef\xbb\xbf0", b"\x00", b"\xff"):
            with self.subTest(value=value):
                self.value = value
                with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "exactly ASCII 0"):
                    privacy.assert_openvino_telemetry_disabled()
        self._assert_nothing_imported_or_sent()

    def test_missing_unreadable_and_directory_consent_fail_closed(self) -> None:
        for error in (FileNotFoundError("missing"), PermissionError("unreadable"), IsADirectoryError("directory")):
            with self.subTest(error=type(error).__name__):
                self.value = error
                with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "missing or unreadable"):
                    privacy.assert_openvino_telemetry_disabled()
        self._assert_nothing_imported_or_sent()

    def test_cached_packages_or_submodules_require_a_fresh_interpreter(self) -> None:
        for name in ("openvino", "openvino.tools.ovc", "openvino_telemetry", "openvino_telemetry.main"):
            with self.subTest(name=name), patch.dict(sys.modules, {name: SimpleNamespace()}):
                privacy.assert_openvino_telemetry_disabled()
                with self.assertRaisesRegex(privacy.OpenVinoImportPrivacyError, "fresh interpreter is required"):
                    privacy.assert_fresh_openvino_import()
        self._assert_nothing_imported_or_sent()

    def test_native_boundary_is_prepare_then_privacy_then_freshness_then_import(self) -> None:
        events: list[str] = []
        original_open = Path.open

        def ordered_open(path, mode="r", *args, **kwargs):
            events.append("privacy")
            return original_open(path, mode, *args, **kwargs)

        def ordered_prepare():
            events.append("prepare")
            bootstrap._PREPARED_CACHE_LIBS = self.cache
            return self.cache

        self.prepare.side_effect = ordered_prepare
        self.importer.side_effect = lambda name: (events.append("import:" + name) or self.runtime)
        with patch.object(Path, "open", new=ordered_open), patch.object(
            bootstrap, "assert_fresh_openvino_import", side_effect=lambda: events.append("fresh")
        ):
            self.assertIs(self.runtime, bootstrap.load_openvino())
        self.assertEqual(["prepare", "privacy", "fresh", "import:openvino"], events)
        self.enqueue.assert_not_called()
        self.transport.assert_not_called()

    def test_native_boundary_blocks_every_bad_consent_before_import(self) -> None:
        for value in (b"1", b"0\n", b"\xff", FileNotFoundError("missing"), PermissionError("unreadable")):
            with self.subTest(value=value):
                self.value = value
                with self.assertRaisesRegex(bootstrap.OpenVinoNativeBootstrapError, "OpenVINO import blocked:"):
                    bootstrap.load_openvino()
        self.native_assert.assert_not_called()
        self._assert_nothing_imported_or_sent()

    def test_native_boundary_rejects_unowned_cached_telemetry(self) -> None:
        with patch.dict(sys.modules, {"openvino_telemetry": SimpleNamespace(consent=True)}):
            with self.assertRaisesRegex(bootstrap.OpenVinoNativeBootstrapError, "fresh interpreter is required"):
                bootstrap.load_openvino()
        self._assert_nothing_imported_or_sent()

    def test_native_owned_cache_rechecks_consent_without_reimport(self) -> None:
        self.assertIs(self.runtime, bootstrap.load_openvino())
        # Represent the real import populating sys.modules only after the guard.
        with patch.dict(sys.modules, {"openvino": self.runtime, "openvino_telemetry": SimpleNamespace(consent=False)}):
            self.assertIs(self.runtime, bootstrap.load_openvino())
        self.assertEqual(2, len(self.opened))
        self.importer.assert_called_once_with("openvino")
        self.prepare.assert_called_once()

    def test_native_owned_cache_refuses_changed_or_unreadable_consent(self) -> None:
        self.assertIs(self.runtime, bootstrap.load_openvino())
        self.importer.reset_mock()
        self.native_assert.reset_mock()
        for value in (b"1", PermissionError("unreadable")):
            with self.subTest(value=value):
                self.value = value
                with self.assertRaisesRegex(bootstrap.OpenVinoNativeBootstrapError, "OpenVINO import blocked:"):
                    bootstrap.load_openvino()
        self.native_assert.assert_not_called()
        self._assert_nothing_imported_or_sent()

    def test_non_windows_boundary_checks_consent_before_fake_inference(self) -> None:
        events: list[str] = []
        original_check = verifier.assert_openvino_telemetry_disabled
        def check():
            events.append("privacy")
            return original_check()
        self.importer.side_effect = lambda name: (events.append("import:" + name) or self.runtime)
        with patch.object(sys, "platform", "linux"), patch.object(verifier, "assert_openvino_telemetry_disabled", side_effect=check):
            self.assertEqual("2025.4.1", verifier._run_real_inference(Path("/mock/models")))
        self.assertEqual(["privacy", "import:openvino"], events)
        self.runtime.Core.assert_called_once()
        self.enqueue.assert_not_called()
        self.transport.assert_not_called()

    def test_non_windows_boundary_blocks_every_bad_consent_before_import(self) -> None:
        with patch.object(sys, "platform", "linux"):
            for value in (b"1", b"0\n", b"\xff", FileNotFoundError("missing"), PermissionError("unreadable")):
                with self.subTest(value=value):
                    self.value = value
                    with self.assertRaisesRegex(verifier.NativeRuntimeError, "OpenVinoImportPrivacyError: OpenVINO import blocked:"):
                        verifier._run_real_inference(Path("/mock/models"))
        self._assert_nothing_imported_or_sent()

    def test_windows_fallback_uses_shared_guard_in_source_and_frozen_modes(self) -> None:
        self.system.return_value = "Windows"
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.object(sys, "platform", "win32"), patch.object(
                sys, "frozen", frozen, create=True
            ), patch.object(privacy.os, "environ", {"LOCALAPPDATA": "/mock/local"}), patch.object(
                verifier, "prepare_openvino_native_runtime", self.prepare
            ), patch.object(verifier, "assert_loaded_openvino_from_cache", self.native_assert):
                bootstrap._reset_for_tests()
                self.assertEqual("2025.4.1", verifier._run_real_inference(Path("/mock/models")))
        self.assertEqual([(Path("/mock/local/Intel Corporation/openvino_telemetry"), "rb")] * 2, self.opened)
        self.assertEqual(2, self.importer.call_count)
        self.assertEqual(2, self.runtime.Core.call_count)
        self.enqueue.assert_not_called()
        self.transport.assert_not_called()

    def test_windows_fallback_failure_is_privacy_failure_not_native_error(self) -> None:
        self.system.return_value = "Windows"
        self.value = FileNotFoundError("missing Windows consent")
        with patch.object(sys, "platform", "win32"), patch.object(
            privacy.os, "environ", {"LOCALAPPDATA": "/mock/local"}
        ), patch.object(verifier, "prepare_openvino_native_runtime", self.prepare):
            with self.assertRaisesRegex(verifier.NativeRuntimeError, "OpenVinoNativeBootstrapError: OpenVINO import blocked:.*missing or unreadable"):
                verifier._run_real_inference(Path("/mock/models"))
        self.assertEqual(2, self.prepare.call_count)
        self._assert_nothing_imported_or_sent()

    def test_non_windows_boundary_rejects_unowned_cached_import(self) -> None:
        for name in ("openvino", "openvino_telemetry"):
            with self.subTest(name=name), patch.object(sys, "platform", "linux"), patch.dict(sys.modules, {name: SimpleNamespace()}):
                with self.assertRaisesRegex(verifier.NativeRuntimeError, "fresh interpreter is required"):
                    verifier._run_real_inference(Path("/mock/models"))
        self._assert_nothing_imported_or_sent()

    def test_failed_home_creation_regression_never_reaches_import_or_sender(self) -> None:
        # Upstream starts enabled and can leave that state intact if mkdir
        # fails.  We simulate its missing-file input without performing mkdir,
        # relocating HOME, or loading the upstream implementation.
        self.value = FileNotFoundError("consent absent under an unwritable HOME")
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(verifier.NativeRuntimeError, "missing or unreadable"):
                verifier._run_real_inference(Path("/mock/models"))
        self.assertEqual([(self.home / "intel/openvino_telemetry", "rb")], self.opened)
        self._assert_nothing_imported_or_sent()

    def test_injected_fake_runtime_never_imports_openvino(self) -> None:
        self.value = FileNotFoundError("mock inference does not need consent")
        self.assertEqual("2025.4.1", verifier._run_real_inference(Path("/mock/models"), runtime_module=self.runtime))
        self.assertEqual([], self.opened)
        self.importer.assert_not_called()
        self.enqueue.assert_not_called()
        self.transport.assert_not_called()

    def test_helper_only_change_invalidates_readiness_before_live_probe(self) -> None:
        helper = "backend/app/openvino_import_privacy.py"
        self.assertIn(helper, readiness.FINGERPRINT_PATHS)
        contents = {relative: relative.encode("ascii") for relative in readiness.FINGERPRINT_PATHS}

        def source_open(path, mode="r", *args, **kwargs):
            self.assertEqual("rb", mode)
            return io.BytesIO(contents[path.relative_to(readiness.PROJECT_ROOT).as_posix()])

        with patch.object(Path, "open", new=source_open), patch.object(
            readiness, "_project_version", return_value="mock-version"
        ), patch.object(readiness, "_python_identity", return_value={"version": "mock"}), patch.object(
            readiness, "_validate_live_runtime", return_value=None
        ) as live_probe:
            saved = readiness._expected_payload()
            contents[helper] = b"changed helper only"
            changed = readiness._expected_payload()
            differences = [key for key in changed["fingerprints"] if saved["fingerprints"][key] != changed["fingerprints"][key]]
            self.assertEqual([helper], differences)
            with patch.object(Path, "read_text", return_value=json.dumps(saved)):
                with self.assertRaisesRegex(readiness.RuntimeReadinessError, "stale or mismatched: fingerprints"):
                    readiness.check_marker(Path("/mock/runtime-ready.json"))
            live_probe.assert_not_called()
        self._assert_nothing_imported_or_sent()


if __name__ == "__main__":
    unittest.main(verbosity=2)
