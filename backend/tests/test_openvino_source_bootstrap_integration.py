from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import verify_openvino_windows
from scripts import verify_runtime_ready


def _write_pe(path: Path) -> None:
    payload = bytearray(256)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x8664)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class _FakeCompiledModel:
    def input(self, _index: int) -> SimpleNamespace:
        return SimpleNamespace(shape=(1, 3, 1, 1))

    def __call__(self, _inputs: object) -> dict[str, object]:
        return {"output": object()}


class OpenVinoSourceBootstrapIntegrationTests(unittest.TestCase):
    def _model_fixture(self, root: Path) -> Path:
        model_directory = root / "models"
        model_directory.mkdir(parents=True)
        for filename in verify_openvino_windows.MODEL_FILENAMES:
            (model_directory / filename).write_bytes(b"fixture")
        return model_directory

    def test_source_gate_validates_record_before_bootstrap_and_seals_diagnostics(
        self,
    ) -> None:
        events: list[str] = []
        runtime_module = SimpleNamespace(__version__="2025.4.1")
        # A drive-qualified path is absolute only on Windows.  On POSIX it is
        # relative and resolve() would prepend a possibly non-ASCII checkout,
        # turning this Windows-path simulation into a false diagnostic failure.
        cache = (
            Path("C:/ProgramData/JuxinIGAC/OpenVINO/u-test/v2025.4.1-digest/libs")
            if os.name == "nt"
            else Path("/tmp/JuxinIGAC/OpenVINO/u-test/v2025.4.1-digest/libs")
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_directory = self._model_fixture(root)
            manifest = root / "openvino-native-manifest.json"

            def installed() -> tuple[str, list[dict[str, object]]]:
                events.append("record")
                return "2025.4.1", [{"path": "verified"}]

            def validate(_entries: object) -> str:
                events.append("entry-set")
                return "314"

            def prepare() -> Path:
                events.append("prepare")
                return cache

            def load() -> object:
                events.append("load")
                return runtime_module

            def assert_loaded(selected: Path) -> Path:
                events.append("assert-loaded")
                self.assertEqual(cache.resolve(), Path(selected).resolve())
                return Path(selected) / "openvino.dll"

            def infer(
                selected_models: Path,
                *,
                runtime_module: object,
                cache_libs: Path,
            ) -> str:
                events.append("infer")
                self.assertEqual(model_directory, selected_models)
                self.assertIs(runtime_module, runtime_module_fixture)
                self.assertEqual(cache.resolve(), cache_libs)
                return "2025.4.1"

            runtime_module_fixture = runtime_module
            with (
                patch.object(verify_openvino_windows, "_check_msvc_runtime"),
                patch.object(
                    verify_openvino_windows,
                    "_system_msvc_runtime_entries",
                    return_value=[],
                ),
                patch.object(
                    verify_openvino_windows,
                    "_installed_native_files",
                    side_effect=installed,
                ),
                patch.object(
                    verify_openvino_windows,
                    "_validate_openvino_entry_set",
                    side_effect=validate,
                ),
                patch.object(
                    verify_openvino_windows,
                    "_installed_openvino_dll_path",
                    side_effect=lambda: (
                        events.append("source-path")
                        or Path("C:/Users/test/中文环境/.venv/Lib/site-packages/openvino/libs/openvino.dll")
                    ),
                ),
                patch.object(
                    verify_openvino_windows,
                    "prepare_openvino_native_runtime",
                    side_effect=prepare,
                ),
                patch.object(
                    verify_openvino_windows, "load_openvino", side_effect=load
                ),
                patch.object(
                    verify_openvino_windows,
                    "assert_loaded_openvino_from_cache",
                    side_effect=assert_loaded,
                ),
                patch.object(
                    verify_openvino_windows,
                    "_model_directory",
                    return_value=model_directory,
                ),
                patch.object(verify_openvino_windows, "_assert_model_assets_valid"),
                patch.object(
                    verify_openvino_windows,
                    "_run_real_inference",
                    side_effect=infer,
                ),
                patch.dict(
                    os.environ,
                    {"IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE": "1"},
                    clear=False,
                ),
            ):
                verify_openvino_windows.verify_source(manifest)

            self.assertLess(events.index("record"), events.index("entry-set"))
            self.assertLess(events.index("entry-set"), events.index("prepare"))
            self.assertLess(events.index("prepare"), events.index("load"))
            self.assertLess(events.index("load"), events.index("infer"))
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            diagnostic = payload["native_bootstrap"]
            self.assertTrue(diagnostic["source_openvino_dll_non_ascii"])
            self.assertTrue(diagnostic["non_ascii_source_required"])
            self.assertTrue(diagnostic["cache_path_ascii"])
            self.assertTrue(diagnostic["loaded_openvino_dll_path_ascii"])
            self.assertTrue(diagnostic["loaded_from_verified_cache"])
            self.assertEqual("v2025.4.1-digest", diagnostic["cache_generation"])
            self.assertNotIn("中文环境", manifest.read_text(encoding="utf-8"))

    def test_required_non_ascii_source_topology_fails_before_bootstrap(self) -> None:
        prepare = Mock()
        with (
            patch.object(verify_openvino_windows, "_check_msvc_runtime"),
            patch.object(
                verify_openvino_windows,
                "_system_msvc_runtime_entries",
                return_value=[],
            ),
            patch.object(
                verify_openvino_windows,
                "_installed_native_files",
                return_value=("2025.4.1", [{"path": "verified"}]),
            ),
            patch.object(
                verify_openvino_windows,
                "_validate_openvino_entry_set",
                return_value="314",
            ),
            patch.object(
                verify_openvino_windows,
                "_installed_openvino_dll_path",
                return_value=Path("C:/ascii/venv/openvino/libs/openvino.dll"),
            ),
            patch.object(
                verify_openvino_windows,
                "prepare_openvino_native_runtime",
                prepare,
            ),
            patch.dict(
                os.environ,
                {"IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE": "1"},
                clear=False,
            ),
            self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "did not exercise the reported Windows failure topology",
            ),
        ):
            verify_openvino_windows.verify_source(Path("unused.json"))
        prepare.assert_not_called()

    def test_real_inference_asserts_loaded_dll_immediately_around_core(self) -> None:
        events: list[str] = []
        cache = Path("C:/ProgramData/JuxinIGAC/OpenVINO/cache/libs")

        class FakeCore:
            def read_model(self, **_kwargs: object) -> object:
                return object()

            def compile_model(self, _model: object, _device: str) -> _FakeCompiledModel:
                return _FakeCompiledModel()

        def make_core() -> FakeCore:
            events.append("core")
            return FakeCore()

        def assert_loaded(_cache: Path) -> Path:
            events.append("assert")
            return cache / "openvino.dll"

        runtime_module = SimpleNamespace(Core=make_core, __version__="2025.4.1")
        with (
            patch.object(
                verify_openvino_windows,
                "assert_loaded_openvino_from_cache",
                side_effect=assert_loaded,
            ),
            patch.object(
                verify_openvino_windows,
                "_read_ir_model_from_memory",
                return_value=object(),
            ),
            patch(
                "app.person_recognition.exercise_local_openvino_gender_branch"
            ) as production_branch,
        ):
            version = verify_openvino_windows._run_real_inference(
                Path("models"), runtime_module=runtime_module, cache_libs=cache
            )

        self.assertEqual("2025.4.1", version)
        self.assertEqual(["assert", "core", "assert"], events[:3])
        production_branch.assert_called_once()

    def test_frozen_layout_rejects_embedded_manifest_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "collector_core"
            native_root = dist / "_internal"
            native_root.mkdir(parents=True)
            _write_pe(dist / "collector_core.exe")
            payload = {
                "schema_version": 1,
                "openvino_version": "2025.4.1",
                "files": [{"path": "placeholder"}],
                "msvc_runtime_files": [
                    {"path": filename}
                    for filename in verify_openvino_windows.MSVC_RUNTIME_DLLS
                ],
            }
            external = root / "openvino-native-manifest.json"
            external.write_text(json.dumps(payload), encoding="utf-8")
            (native_root / verify_openvino_windows.PACKAGED_MANIFEST_NAME).write_text(
                json.dumps({**payload, "runtime_version": "tampered"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "differs from the independently verified source manifest",
            ):
                verify_openvino_windows.verify_frozen(external, dist)

    def test_readiness_core_is_guarded_by_the_same_bootstrap(self) -> None:
        events: list[str] = []
        cache = Path("C:/ProgramData/JuxinIGAC/OpenVINO/cache/libs")

        class FakeCore:
            available_devices = ("CPU",)

        def make_core() -> FakeCore:
            events.append("core")
            return FakeCore()

        runtime_module = SimpleNamespace(Core=make_core)

        def assert_loaded(_cache: Path) -> Path:
            events.append("assert")
            return cache / "openvino.dll"

        with (
            patch.object(
                verify_runtime_ready.importlib.metadata,
                "version",
                return_value="2025.4.1",
            ),
            patch.object(
                verify_runtime_ready,
                "prepare_openvino_native_runtime",
                return_value=cache,
            ),
            patch.object(
                verify_runtime_ready,
                "load_openvino",
                return_value=runtime_module,
            ),
            patch.object(
                verify_runtime_ready,
                "assert_loaded_openvino_from_cache",
                side_effect=assert_loaded,
            ),
        ):
            verify_runtime_ready._assert_openvino_loads()

        self.assertEqual(["assert", "core", "assert", "assert"], events)


if __name__ == "__main__":
    unittest.main()
