from __future__ import annotations

import ast
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import download_person_models
from scripts import pyi_rth_openvino
from scripts import verify_openvino_windows
from scripts import verify_runtime_ready

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_pe(path: Path, *, machine: int = 0x8664, suffix: bytes = b"") -> None:
    payload = bytearray(256)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(payload) + suffix)


class OpenVinoWindowsPackagingTests(unittest.TestCase):
    def test_pe_gate_accepts_x64_and_rejects_other_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            x64 = Path(temporary) / "x64.dll"
            arm64 = Path(temporary) / "arm64.dll"
            _write_pe(x64)
            _write_pe(arm64, machine=0xAA64)
            verify_openvino_windows._assert_x64_pe(x64)
            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "not Windows x64 PE",
            ):
                verify_openvino_windows._assert_x64_pe(arm64)

    def _frozen_fixture(self, root: Path, *, abi: str = "314") -> tuple[Path, Path]:
        dist = root / "collector_core"
        native = dist / "_internal"
        _write_pe(dist / "collector_core.exe")
        relative_files = [
            f"openvino/_pyopenvino.cp{abi}-win_amd64.pyd",
            *[
                f"openvino/libs/{name}"
                for name in verify_openvino_windows.OPENVINO_NATIVE_DLLS
            ],
        ]
        manifest_entries = []
        for index, relative in enumerate(relative_files):
            path = native / relative
            _write_pe(path, suffix=str(index).encode("ascii"))
            manifest_entries.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "machine": "0x8664",
                }
            )
        msvc_entries = []
        for name in verify_openvino_windows.MSVC_RUNTIME_DLLS:
            runtime_path = native / name
            _write_pe(runtime_path)
            msvc_entries.append(
                {
                    "path": name,
                    "size": runtime_path.stat().st_size,
                    "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
                    "machine": "0x8664",
                }
            )
        _write_pe(native / f"python{abi}.dll")
        models = native / "app" / "assets" / "person_classifier"
        models.mkdir(parents=True)
        source_models = verify_openvino_windows._model_directory()
        for name in verify_openvino_windows.MODEL_FILENAMES:
            shutil.copy2(source_models / name, models / name)
        manifest = root / "openvino-native-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "openvino_version": "2025.4.1",
                    "runtime_version": "2025.4.1",
                    "files": manifest_entries,
                    "msvc_runtime_files": msvc_entries,
                }
            ),
            encoding="utf-8",
        )
        shutil.copy2(manifest, native / "openvino-native-manifest.json")
        return manifest, dist

    def test_frozen_layout_requires_exact_app_local_native_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, dist = self._frozen_fixture(Path(temporary))
            verify_openvino_windows.verify_frozen(manifest, dist)
            missing = dist / "_internal" / "MSVCP140.dll"
            missing.unlink()
            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "MSVCP140.dll",
            ):
                verify_openvino_windows.verify_frozen(manifest, dist)

    def test_frozen_layout_accepts_each_supported_standard_cpython_abi(self) -> None:
        for abi in verify_openvino_windows.SUPPORTED_ABIS:
            with self.subTest(abi=abi), tempfile.TemporaryDirectory() as temporary:
                manifest, dist = self._frozen_fixture(Path(temporary), abi=abi)
                verify_openvino_windows.verify_frozen(manifest, dist)

    def test_frozen_layout_rejects_mismatched_python_abi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, dist = self._frozen_fixture(Path(temporary), abi="314")
            python_dll = dist / "_internal" / "python314.dll"
            python_dll.rename(dist / "_internal" / "python313.dll")
            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "matching app-local python314.dll",
            ):
                verify_openvino_windows.verify_frozen(manifest, dist)

    def test_frozen_layout_rejects_changed_openvino_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, dist = self._frozen_fixture(Path(temporary))
            changed = dist / "_internal" / "openvino" / "libs" / "openvino.dll"
            changed.write_bytes(changed.read_bytes() + b"changed")
            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "size mismatch",
            ):
                verify_openvino_windows.verify_frozen(manifest, dist)

    def test_frozen_layout_rejects_changed_packaged_native_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, dist = self._frozen_fixture(Path(temporary))
            packaged = dist / "_internal" / "openvino-native-manifest.json"
            packaged.write_bytes(packaged.read_bytes() + b" ")
            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "manifest",
            ):
                verify_openvino_windows.verify_frozen(manifest, dist)

    def test_runtime_hook_prepares_and_retains_ascii_frozen_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native = Path(temporary)
            (native / "openvino-native-manifest.json").write_text(
                '{"schema_version":1}', encoding="ascii"
            )
            cache = native / "ascii-cache" / "libs"
            cache.mkdir(parents=True)
            from app import openvino_native_bootstrap

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(native), create=True),
                patch.object(sys, "platform", "win32"),
                patch.object(pyi_rth_openvino, "_openvino_was_imported", return_value=False),
                patch.object(
                    openvino_native_bootstrap,
                    "prepare_openvino_native_runtime",
                    return_value=cache,
                ) as prepare,
            ):
                prepared = pyi_rth_openvino._prepare_frozen_openvino_native_runtime()
                retained = sys._igac_openvino_native_cache_libs

            prepare.assert_called_once_with()
            self.assertEqual(prepared, cache.resolve())
            self.assertEqual(retained, str(cache.resolve()))

    def test_runtime_hook_fails_before_openvino_import_when_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native = Path(temporary)
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(native), create=True),
                patch.object(sys, "platform", "win32"),
            ):
                with self.assertRaises(SystemExit):
                    pyi_rth_openvino._prepare_frozen_openvino_native_runtime()

    def test_frozen_smoke_rejects_stale_boolean_flag_without_importing_openvino(self) -> None:
        with patch.dict(
            os.environ,
            {"IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS": "1"},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                download_person_models._run_frozen_model_smoke_if_requested()

    def test_real_cpu_models_compile_from_unicode_directory_in_memory(self) -> None:
        source = verify_openvino_windows._model_directory()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "聚鑫测试" / "新建文件夹（4）" / "人物模型"
            destination.mkdir(parents=True)
            for filename in verify_openvino_windows.MODEL_FILENAMES:
                shutil.copy2(source / filename, destination / filename)

            verify_openvino_windows._assert_model_assets_valid(destination)
            runtime_version = verify_openvino_windows._run_real_inference(destination)

        self.assertTrue(runtime_version.startswith("2025.4.1"), runtime_version)

    def test_model_integrity_is_rechecked_immediately_before_inference(self) -> None:
        source = verify_openvino_windows._model_directory()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "中文路径" / "models"
            destination.mkdir(parents=True)
            for filename in verify_openvino_windows.MODEL_FILENAMES:
                shutil.copy2(source / filename, destination / filename)
            changed = destination / "face-detection-retail-0004.bin"
            payload = bytearray(changed.read_bytes())
            payload[0] ^= 0xFF
            changed.write_bytes(payload)

            with self.assertRaisesRegex(
                verify_openvino_windows.NativeRuntimeError,
                "SHA-384 mismatch",
            ):
                verify_openvino_windows._assert_model_assets_valid(destination)

    def test_runtime_readiness_marker_is_atomic_and_checkable(self) -> None:
        expected = {
            "schema_version": 1,
            "application_version": "1.0.0",
            "openvino_version": "2025.4.1",
            "python": {"version": "test"},
            "fingerprints": {"package.json": "abc"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "runtime-ready.json"
            with (
                patch.object(verify_runtime_ready, "_validate_live_runtime"),
                patch.object(verify_runtime_ready, "_expected_payload", return_value=expected),
            ):
                verify_runtime_ready.write_marker(marker)
                self.assertTrue(marker.is_file())
                self.assertFalse(any(marker.parent.glob(f".{marker.name}.*.tmp")))
                verify_runtime_ready.check_marker(marker)

    def test_runtime_readiness_marker_rejects_stale_fingerprint_before_live_probe(self) -> None:
        expected = {
            "schema_version": 1,
            "application_version": "1.0.0",
            "openvino_version": "2025.4.1",
            "python": {"version": "test"},
            "fingerprints": {"package.json": "new"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "runtime-ready.json"
            stale = {**expected, "fingerprints": {"package.json": "old"}}
            marker.write_text(json.dumps(stale), encoding="utf-8")
            live_probe = Mock()
            with (
                patch.object(verify_runtime_ready, "_expected_payload", return_value=expected),
                patch.object(verify_runtime_ready, "_validate_live_runtime", live_probe),
                self.assertRaisesRegex(
                    verify_runtime_ready.RuntimeReadinessError,
                    "stale or mismatched: fingerprints",
                ),
            ):
                verify_runtime_ready.check_marker(marker)
            live_probe.assert_not_called()

    def _native_logging_sources(self) -> tuple[str, str, str]:
        return tuple((PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8") for name in (
            "invoke_native_logged.ps1", "owned_process.ps1", "owned_process.py",
        ))

    def _assert_native_logging_contract(self, wrapper: str, adapter: str, owner: str) -> None:
        # This is a source contract, supplemented by executable owner/adapter
        # fault tests and the unchanged mandatory native PowerShell behavior probe.
        wrapper = re.sub(r"(?m)^\s*#.*$", "", wrapper)
        adapter = re.sub(r"(?m)^\s*#.*$", "", adapter)
        preferences = r"(?im)^\s*\$(?:(?:global|script|local):)?(?:ErrorActionPreference|PSNativeCommandUseErrorActionPreference)\s*="
        self.assertNotRegex(wrapper + adapter, preferences, "caller preferences must never be mutated")
        self.assertNotRegex(wrapper, r"(?im)^\s*\$LASTEXITCODE\s*=", "do not shadow the global native exit")
        steps = (
            '$global:LASTEXITCODE = $null',
            '$Receipt = Invoke-IgacOwnedProcess',
            '$NativeExitCode = [int]$Receipt.targetExitCode',
            '$global:LASTEXITCODE = $NativeExitCode',
            'return $NativeExitCode',
        )
        for step in steps:
            self.assertIn(step, wrapper, "reset, capture and return the actual target exit")
        self.assertEqual(sorted(wrapper.index(step) for step in steps),
                         [wrapper.index(step) for step in steps], "native exit reset/capture/return order")
        for token in ('-StreamOutput -Request @{', 'stdoutPath = $FullLogPath', 'stderrPath = $FullLogPath'):
            self.assertIn(token, wrapper, "both native streams retain their complete owned log")
        for token in ('self.stdout = self.open_log(request.stdoutPath)',
                      'self.stderr = self.stdout if', "receipt['outcome'] = 'log-size-limit'; break"):
            self.assertIn(token, owner, "owned logs must not be silently discarded or truncated")
        self.assertNotRegex(wrapper + adapter, r"Remove-Item[^\n]*(?:FullLogPath|stdoutPath|stderrPath)",
                            "retain the complete owned log on presentation failure")
        for token in ('$Process.StartInfo.RedirectStandardOutput = $false',
                      '$Process.StartInfo.RedirectStandardError = $false'):
            self.assertIn(token, adapter, "completion must not wait for inherited output-pipe EOF")
        self.assertNotRegex(adapter, r"\.WaitForExit\(\s*\)|\.Result\b", "process/output waits must remain bounded")
        for token in ('$ProgressTask.Wait(2000)', '$ProgressTask.Wait(10000)',
                      '$Task.Wait($TimeoutMilliseconds)'):
            self.assertIn(token, adapter, "bounded output reads must propagate failure")
        hosts = re.findall(r"Write-Host[^\n]*", adapter)
        self.assertEqual(len(hosts), 2, "both live and final output must be presented")
        for call in hosts:
            self.assertIn('-ErrorAction Stop', call, "host output failures must terminate even for a Continue caller")
        self.assertRegex(adapter, r"catch\s*\{\s*\$PrimaryFailure\s*=\s*\$_\s+throw\s*\}",
                         "log/host errors must be rethrown, not swallowed")
        terminal = 'if (-not $MatchesRequest -or -not (Test-IgacOwnedTerminalReceipt $Receipt) -or $Process.ExitCode -ne 0)'
        self.assertIn(terminal, adapter, "only a bound, validated terminal receipt can succeed")
        self.assertLess(adapter.index(terminal), adapter.index('return $Receipt'),
                        "validate cleanup and target exit before returning")
        for token in ('targetExitCode -lt -2147483648', 'targetExitCode -gt 2147483647',
                      '$ExpectedUnsigned += 4294967296', '$Receipt.targetExitCodeUnsigned -ne $ExpectedUnsigned'):
            self.assertIn(token, adapter, "signed PowerShell exit must match the retained raw DWORD")
        # Execute the exact normalization function rather than merely looking
        # for a high-bit literal. It must retain all bits and a safe signed int.
        tree = ast.parse(owner)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'record_target_exit']
        self.assertEqual(len(functions), 1, "one exact native exit normalization contract")
        namespace = {'ctypes': ctypes}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<owned-exit-contract>', 'exec'), namespace)
        for raw, signed in ((0, 0), (23, 23), (0xC0000005, -1073741819), (0xFFFFFFFF, -1)):
            receipt = {}
            namespace['record_target_exit'](receipt, raw)
            self.assertEqual(receipt, {'targetExitCodeUnsigned': raw, 'targetExitCode': signed},
                             "preserve exact raw and signed native exit values")

    def test_windows_native_logging_wrapper_preserves_exit_code_and_strict_errors(self) -> None:
        self._assert_native_logging_contract(*self._native_logging_sources())

    def test_native_logging_contract_has_passing_baseline_and_rejects_weakened_guards(self) -> None:
        sources = self._native_logging_sources()
        # An unrelated baseline failure must never make every negative test pass.
        self._assert_native_logging_contract(*sources)
        mutations = (
            (0, '$global:LASTEXITCODE = $null', '$ErrorActionPreference = "Continue"\n    $global:LASTEXITCODE = $null', 'preferences'),
            (0, '$global:LASTEXITCODE = $null', '$LASTEXITCODE = $null', 'global native exit'),
            (0, '$NativeExitCode = [int]$Receipt.targetExitCode', '$NativeExitCode = [int]$Process.ExitCode', 'actual target exit'),
            (0, 'stderrPath = $FullLogPath', 'stderrPath = $null', 'complete owned log'),
            (1, '$Process.StartInfo.RedirectStandardOutput = $false', '$Process.StartInfo.RedirectStandardOutput = $true', 'pipe EOF'),
            (1, '$ProgressTask.Wait(2000)', '$ProgressTask.Wait()', 'bounded output reads'),
            (1, 'Write-Host -ErrorAction Stop -NoNewline', 'Write-Host -ErrorAction Continue -NoNewline', 'host output failures'),
            (1, '$PrimaryFailure = $_\n        throw', '$PrimaryFailure = $_\n        # swallowed', 'must be rethrown'),
            (1, '$ExpectedUnsigned += 4294967296', '$ExpectedUnsigned += 0', 'raw DWORD'),
            (1, 'if (-not $MatchesRequest -or -not (Test-IgacOwnedTerminalReceipt $Receipt)', 'if (-not (Test-IgacOwnedTerminalReceipt $Receipt)', 'bound, validated'),
            (2, "receipt['outcome'] = 'log-size-limit'; break", "receipt['outcome'] = 'completed'; break", 'silently discarded'),
            (2, 'raw_code & 0xFFFFFFFF', 'raw_code & 0x7FFFFFFF', 'exact raw and signed'),
            (2, "ctypes.c_int32(receipt['targetExitCodeUnsigned']).value", "receipt['targetExitCodeUnsigned']", 'exact raw and signed'),
        )
        for index, old, new, failure in mutations:
            with self.subTest(guard=failure, mutation=old):
                self.assertIn(old, sources[index], "negative mutation must actually change the passing baseline")
                altered = list(sources)
                altered[index] = altered[index].replace(old, new, 1)
                with self.assertRaisesRegex(AssertionError, failure):
                    self._assert_native_logging_contract(*altered)
        altered = list(sources)
        altered[0] = altered[0].replace('$global:LASTEXITCODE = $null', '', 1).replace(
            'return $NativeExitCode', '$global:LASTEXITCODE = $null\n    return $NativeExitCode', 1)
        with self.assertRaisesRegex(AssertionError, 'reset/capture/return order'):
            self._assert_native_logging_contract(*altered)

    def test_windows_build_uses_safe_native_logging_for_both_packagers(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('. "$PSScriptRoot\\invoke_native_logged.ps1"', source)
        self.assertIn('& "$PSScriptRoot\\test_native_command_logging.ps1"', source)
        self.assertEqual(1, source.count("$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog"))
        self.assertEqual(1, source.count("$BuilderExitCode = Invoke-IgacNativeCommandWithLog"))
        self.assertIn("-ArgumentList $PyInstallerArguments", source)
        self.assertIn(
            '-ArgumentList @("--win", "nsis", "--x64", "--publish", "never")',
            source,
        )
        self.assertNotIn(
            "& .venv\\Scripts\\pyinstaller.exe @PyInstallerArguments 2>&1",
            source,
        )
        self.assertNotIn(
            "& $ElectronBuilder --win nsis --x64 2>&1",
            source,
        )
        self.assertIn("if ($PyInstallerExitCode -ne 0)", source)
        self.assertIn("if ($BuilderExitCode -eq 0)", source)
        self.assertIn("if ($PortableOnly -or -not $InstallerSucceeded)", source)
        self.assertLess(
            source.index('& "$PSScriptRoot\\test_native_command_logging.ps1"'),
            source.index('& "$PSScriptRoot\\install_windows.ps1"'),
        )

    def test_windows_native_logging_behavior_probe_covers_success_failure_and_log_error(self) -> None:
        source = (
            PROJECT_ROOT / "scripts" / "test_native_command_logging.ps1"
        ).read_text(encoding="utf-8")
        for expected in (
            "[char]0x539F, [char]0x751F, [char]0x547D, [char]0x4EE4",
            "IGAC_STDOUT_OK",
            "IGAC_STDERR_INFO 1>&2",
            "exit /b 0",
            "IGAC_STDERR_FAILURE 1>&2",
            "exit /b 23",
            "$SuccessExitCode -isnot [int]",
            "$FailureExitCode -isnot [int]",
            "-LogPath $TestRoot",
            "$LogWriteFailureWasCaught",
            "function Assert-IgacLoggingPreferencesRestored",
            "$OriginalNativeErrorPreference",
            "$global:LASTEXITCODE = 91",
            "$global:LASTEXITCODE -ne 0",
            "$global:LASTEXITCODE -ne 23",
            'Assert-IgacLoggingPreferencesRestored "the success path"',
            'Assert-IgacLoggingPreferencesRestored "the nonzero-exit path"',
            'Assert-IgacLoggingPreferencesRestored "the log-write failure path"',
        ):
            self.assertIn(expected, source)
        self.assertEqual(2, source.count("$global:LASTEXITCODE = 91"))

    def test_windows_workflow_uses_safe_native_logging_in_both_freeze_jobs(self) -> None:
        source = (
            PROJECT_ROOT / ".github" / "workflows" / "windows-installer.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(2, source.count("$pyinstallerExitCode = Invoke-IgacNativeCommandWithLog"))
        self.assertGreaterEqual(
            source.count("Verify Windows PowerShell native stderr logging"), 2
        )
        self.assertIn('"scripts\\invoke_native_logged.ps1"', source)
        self.assertIn('"scripts\\test_native_command_logging.ps1"', source)
        self.assertIn("Verify PowerShell 7 native exit preference restoration", source)
        self.assertNotIn(
            "& pyinstaller @arguments 2>&1 | Tee-Object",
            source,
        )

    def test_unicode_classifier_smoke_unpacks_all_four_compiled_models(self) -> None:
        source = (
            PROJECT_ROOT / ".github" / "workflows" / "windows-installer.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("_,gender,_,_=c._get_compiled_models()", source)
        self.assertNotIn("_,gender=c._get_compiled_models()", source)


if __name__ == "__main__":
    unittest.main()
