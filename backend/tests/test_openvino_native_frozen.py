from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import openvino_native_bootstrap
from scripts import pyi_rth_openvino


class FrozenOpenVinoNativeIntegrationTests(unittest.TestCase):
    def test_runtime_hook_caps_onednn_dispatch_before_app_bootstrap_import(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(
                os.environ,
                {"ONEDNN_MAX_CPU_ISA": "AVX512_CORE_AMX"},
                clear=False,
            ),
        ):
            pyi_rth_openvino._configure_windows_onednn_cpu_isa()
            self.assertEqual("AVX2", os.environ["ONEDNN_MAX_CPU_ISA"])

        source = (PROJECT_ROOT / "scripts" / "pyi_rth_openvino.py").read_text(
            encoding="utf-8"
        )
        cap_call = source.rindex("_configure_windows_onednn_cpu_isa()")
        prepare_call = source.rindex("_prepare_frozen_openvino_native_runtime()")
        self.assertLess(cap_call, prepare_call)

    def test_source_process_is_a_strict_runtime_hook_noop(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(
                pyi_rth_openvino._prepare_frozen_openvino_native_runtime()
            )

    def test_runtime_hook_rejects_an_earlier_openvino_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native_root = Path(temporary)
            (native_root / pyi_rth_openvino.PACKAGED_NATIVE_MANIFEST).write_text(
                '{"schema_version":1}', encoding="ascii"
            )
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(native_root), create=True),
                patch.object(sys, "platform", "win32"),
                patch.object(
                    pyi_rth_openvino,
                    "_openvino_was_imported",
                    return_value=True,
                ),
                patch.object(
                    openvino_native_bootstrap,
                    "prepare_openvino_native_runtime",
                ) as prepare,
                self.assertRaises(SystemExit),
            ):
                pyi_rth_openvino._prepare_frozen_openvino_native_runtime()
            prepare.assert_not_called()

    def test_runtime_hook_rejects_non_ascii_bootstrap_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native_root = Path(temporary)
            (native_root / pyi_rth_openvino.PACKAGED_NATIVE_MANIFEST).write_text(
                '{"schema_version":1}', encoding="ascii"
            )
            non_ascii_cache = native_root / "中文缓存" / "libs"
            non_ascii_cache.mkdir(parents=True)
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(native_root), create=True),
                patch.object(sys, "platform", "win32"),
                patch.object(
                    pyi_rth_openvino,
                    "_openvino_was_imported",
                    return_value=False,
                ),
                patch.object(
                    openvino_native_bootstrap,
                    "prepare_openvino_native_runtime",
                    return_value=non_ascii_cache,
                ),
                self.assertRaises(SystemExit),
            ):
                pyi_rth_openvino._prepare_frozen_openvino_native_runtime()

    def test_build_seals_manifest_before_any_frozen_execution(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )
        add_data = source.index('"--add-data", "$NativeManifest;."')
        package = source.index("$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog")
        packaged_hash = source.index("$FrozenNativeManifestHash")
        frozen_layout = source.index("verify_openvino_windows.py frozen")
        frozen_process = source.index("test_frozen_openvino.ps1")
        self.assertLess(add_data, package)
        self.assertLess(package, packaged_hash)
        self.assertLess(packaged_hash, frozen_layout)
        self.assertLess(frozen_layout, frozen_process)
        self.assertIn("Get-FileHash -Algorithm SHA256", source)

    def test_model_hook_loads_only_after_telemetry_and_native_prepare(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "download_person_models.py").read_text(
            encoding="utf-8"
        )
        smoke = source.index("def _run_frozen_model_smoke_if_requested")
        prepare = source.index("cache_libs = prepare_openvino_native_runtime()", smoke)
        load = source.index("ov = load_openvino()", prepare)
        classifier_import = source.index("from app import person_recognition", load)
        core = source.index("core = ov.Core()", classifier_import)
        telemetry_call = source.rindex("_disable_frozen_openvino_telemetry()")
        smoke_call = source.rindex("_run_frozen_model_smoke_if_requested()")
        self.assertLess(telemetry_call, smoke_call)
        self.assertLess(prepare, load)
        self.assertLess(load, classifier_import)
        self.assertLess(classifier_import, core)
        self.assertNotIn("import openvino as ov", source)
        self.assertIn("IGAC_FROZEN_OPENVINO_ASCII_OK:", source)
        self.assertIn("IGAC_FROZEN_OPENVINO_SOURCE_B64_OK:", source)
        self.assertIn('base64.b64encode(str(source_openvino_dll).encode("utf-8"))', source)

    def _assert_clean_process_gate_contract(self, source: str) -> None:
        for expected in (
            "IGAC_FROZEN_OPENVINO_ASCII_OK:",
            "IGAC_FROZEN_OPENVINO_SOURCE_B64_OK:",
            "[switch]$RequireNonAsciiSource",
            '[string]$ExpectedCacheRoot = ""',
            "IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE",
            'IGAC_OPENVINO_CACHE_ROOT = $null',
            '$EnvironmentChanges.IGAC_OPENVINO_CACHE_ROOT = $ResolvedExpectedCacheRoot',
            'environment = $EnvironmentChanges',
            '$EnvironmentChanges.IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE = "1"',
            'if ($ExitCode -ne 0 -or $StandardOutput -notmatch [regex]::Escape($SuccessMarker))',
            'if (-not $LoadedOpenVinoDllFull.StartsWith($ExpectedCachePrefix, [StringComparison]::OrdinalIgnoreCase))',
            'throw $PrimaryFailure',
            'timeoutSeconds = $TimeoutSeconds',
            'PYTHONHOME = $null',
            'PYTHONPATH = $null',
            'OPENVINO_LIB_PATHS = $null',
            "$AsciiMarkerMatch.Success",
            "$HasNonAsciiCharacter",
            "$SourceHasNonAsciiCharacter",
            "[Convert]::FromBase64String",
            "$ExpectedCachePrefix",
            "EndsWith(\"\\openvino.dll\"",
        ):
            self.assertIn(expected, source, "clean frozen process environment, deadline and real DLL proof guards")

    def test_clean_process_gate_requires_ascii_loaded_dll_marker(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "test_frozen_openvino.ps1").read_text(encoding="utf-8")
        self._assert_clean_process_gate_contract(source)

    def test_clean_process_contract_has_passing_baseline_and_rejects_lost_guards(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "test_frozen_openvino.ps1").read_text(encoding="utf-8")
        self._assert_clean_process_gate_contract(source)
        for old, new in (
            ('IGAC_OPENVINO_CACHE_ROOT = $null', 'IGAC_OPENVINO_CACHE_ROOT = "stale-cache"'),
            ('$EnvironmentChanges.IGAC_OPENVINO_CACHE_ROOT = $ResolvedExpectedCacheRoot', '$EnvironmentChanges.IGAC_OPENVINO_CACHE_ROOT = "different-cache"'),
            ('environment = $EnvironmentChanges', 'environment = @{}'),
            ('$EnvironmentChanges.IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE = "1"', '$EnvironmentChanges.IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE = "0"'),
            ('if ($ExitCode -ne 0 -or $StandardOutput -notmatch', 'if ($ExitCode -ne 0 -and $StandardOutput -notmatch'),
            ('StartsWith($ExpectedCachePrefix, [StringComparison]::OrdinalIgnoreCase)', 'Contains($ExpectedCachePrefix)'),
            ('[Convert]::FromBase64String', '[Convert]::ToString'),
            ('throw $PrimaryFailure', '# swallowed primary failure'),
            ('timeoutSeconds = $TimeoutSeconds', 'timeoutSeconds = 86400'),
        ):
            with self.subTest(guard=old):
                self.assertIn(old, source, "negative mutation must change the passing baseline")
                with self.assertRaisesRegex(AssertionError, 'clean frozen process'):
                    self._assert_clean_process_gate_contract(source.replace(old, new, 1))


if __name__ == "__main__":
    unittest.main()
