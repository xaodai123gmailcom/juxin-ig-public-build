#!/usr/bin/env python3
"""Verify OpenVINO's installed and PyInstaller-frozen Windows native runtime."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from .download_person_models import ModelAssetError, load_manifest, verify_file
except ImportError:  # Direct execution: python scripts/verify_openvino_windows.py
    from download_person_models import ModelAssetError, load_manifest, verify_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = PROJECT_ROOT / "backend"
if str(BACKEND_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIRECTORY))

# This shared helper deliberately does not import OpenVINO.  It must be loaded
# before any code path can import the Python package so the Windows native DLL
# is verified, mirrored and preloaded from an ASCII-only directory first.
from app.openvino_native_bootstrap import (  # noqa: E402
    PACKAGED_MANIFEST_NAME,
    OpenVinoNativeBootstrapError,
    assert_loaded_openvino_from_cache,
    load_openvino,
    prepare_openvino_native_runtime,
)
from app.openvino_import_privacy import (  # noqa: E402
    assert_fresh_openvino_import,
    assert_openvino_telemetry_disabled,
)


EXPECTED_OPENVINO_VERSION = "2025.4.1"
EXPECTED_MACHINE = 0x8664
SUPPORTED_ABIS = ("311", "312", "313", "314")
OPENVINO_NATIVE_DLLS = (
    "openvino.dll",
    "tbb12.dll",
    "openvino_ir_frontend.dll",
    "openvino_intel_cpu_plugin.dll",
)
MSVC_RUNTIME_DLLS = (
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
)
MODEL_FILENAMES = (
    "MODEL_SOURCES.json",
    "face-detection-retail-0004.xml",
    "face-detection-retail-0004.bin",
    "age-gender-recognition-retail-0013.xml",
    "age-gender-recognition-retail-0013.bin",
    "person-detection-retail-0013.xml",
    "person-detection-retail-0013.bin",
    "person-attributes-recognition-crossroad-0230.xml",
    "person-attributes-recognition-crossroad-0230.bin",
)


class NativeRuntimeError(RuntimeError):
    """Raised when the native runtime cannot be trusted or executed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pe_machine(path: Path) -> int:
    """Read the COFF Machine field without loading an untrusted binary."""

    try:
        with path.open("rb") as source:
            if source.read(2) != b"MZ":
                raise NativeRuntimeError(f"not a PE file: {path}")
            source.seek(0x3C)
            offset_bytes = source.read(4)
            if len(offset_bytes) != 4:
                raise NativeRuntimeError(f"truncated DOS header: {path}")
            pe_offset = struct.unpack("<I", offset_bytes)[0]
            source.seek(pe_offset)
            if source.read(4) != b"PE\0\0":
                raise NativeRuntimeError(f"invalid PE signature: {path}")
            machine_bytes = source.read(2)
            if len(machine_bytes) != 2:
                raise NativeRuntimeError(f"truncated COFF header: {path}")
            return struct.unpack("<H", machine_bytes)[0]
    except OSError as exc:
        raise NativeRuntimeError(f"cannot read PE file {path}: {exc}") from exc


def _assert_x64_pe(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise NativeRuntimeError(f"required native file is missing or empty: {path}")
    machine = _pe_machine(path)
    if machine != EXPECTED_MACHINE:
        raise NativeRuntimeError(
            f"native file is not Windows x64 PE (machine=0x{machine:04x}): {path}"
        )


def _record_digest(record_hash: Any) -> str | None:
    if record_hash is None or getattr(record_hash, "mode", "") != "sha256":
        return None
    value = str(getattr(record_hash, "value", ""))
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).hex()
    except (ValueError, TypeError) as exc:
        raise NativeRuntimeError("openvino RECORD contains an invalid SHA-256 digest") from exc


def _installed_native_files() -> tuple[str, list[dict[str, Any]]]:
    try:
        distribution = importlib.metadata.distribution("openvino")
    except importlib.metadata.PackageNotFoundError as exc:
        raise NativeRuntimeError("openvino is not installed") from exc

    version = distribution.version
    if version != EXPECTED_OPENVINO_VERSION:
        raise NativeRuntimeError(
            f"openvino version {version!r} is installed; expected {EXPECTED_OPENVINO_VERSION!r}"
        )
    records = distribution.files
    if records is None:
        raise NativeRuntimeError("openvino installation has no RECORD file list")

    entries: list[dict[str, Any]] = []
    for record in records:
        relative = Path(str(record))
        normalized = relative.as_posix()
        lowered = normalized.casefold()
        if not lowered.startswith("openvino/"):
            continue
        if not (lowered.endswith(".pyd") or "/libs/" in lowered and lowered.endswith(".dll")):
            continue
        path = Path(distribution.locate_file(record)).resolve()
        _assert_x64_pe(path)
        expected_size = getattr(record, "size", None)
        actual_size = path.stat().st_size
        if expected_size is None:
            raise NativeRuntimeError(
                f"installed OpenVINO RECORD has no size for native file: {normalized}"
            )
        if actual_size != int(expected_size):
            raise NativeRuntimeError(
                f"installed OpenVINO file size differs from RECORD: {normalized}"
            )
        expected_digest = _record_digest(getattr(record, "hash", None))
        if expected_digest is None:
            raise NativeRuntimeError(
                "installed OpenVINO RECORD has no SHA-256 for native file: "
                + normalized
            )
        actual_digest = _sha256(path)
        if actual_digest != expected_digest:
            raise NativeRuntimeError(
                f"installed OpenVINO file SHA-256 differs from RECORD: {normalized}"
            )
        entries.append(
            {
                "path": normalized,
                "size": actual_size,
                "sha256": actual_digest,
                "machine": "0x8664",
            }
        )
    entries.sort(key=lambda item: item["path"].casefold())
    return version, entries


def _installed_openvino_dll_path() -> Path:
    """Resolve the original wheel DLL without importing the OpenVINO package."""

    try:
        distribution = importlib.metadata.distribution("openvino")
    except importlib.metadata.PackageNotFoundError as exc:
        raise NativeRuntimeError("openvino is not installed") from exc
    records = distribution.files
    if records is None:
        raise NativeRuntimeError("openvino installation has no RECORD file list")
    matches = [
        record
        for record in records
        if str(record).replace("\\", "/").casefold()
        == "openvino/libs/openvino.dll"
    ]
    if len(matches) != 1:
        raise NativeRuntimeError(
            "installed OpenVINO wheel must contain exactly one "
            f"openvino/libs/openvino.dll RECORD entry; found {len(matches)}"
        )
    path = Path(distribution.locate_file(matches[0])).resolve()
    _assert_x64_pe(path)
    return path


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _validate_openvino_entry_set(entries: Iterable[dict[str, Any]]) -> str:
    paths = [str(item["path"]).replace("\\", "/") for item in entries]
    pyopenvino = [path for path in paths if Path(path).name.casefold().startswith("_pyopenvino") and path.casefold().endswith(".pyd")]
    if len(pyopenvino) != 1:
        raise NativeRuntimeError(
            f"expected exactly one openvino/_pyopenvino*.pyd; found {len(pyopenvino)}"
        )
    match = re.fullmatch(
        r"openvino/_pyopenvino\.cp(311|312|313|314)-win_amd64\.pyd",
        pyopenvino[0],
        flags=re.IGNORECASE,
    )
    if match is None:
        raise NativeRuntimeError(
            "OpenVINO extension must use a supported standard CPython 3.11-3.14 "
            f"Windows x64 ABI: {pyopenvino[0]}"
        )
    path_keys = {path.casefold() for path in paths}
    missing = [
        f"openvino/libs/{filename}"
        for filename in OPENVINO_NATIVE_DLLS
        if f"openvino/libs/{filename}".casefold() not in path_keys
    ]
    if missing:
        raise NativeRuntimeError("installed OpenVINO wheel is missing: " + ", ".join(missing))
    return match.group(1)


def _check_msvc_runtime() -> None:
    if sys.platform != "win32":
        raise NativeRuntimeError("this release check must run on Windows")
    failures: list[str] = []
    for filename in MSVC_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(filename)  # type: ignore[attr-defined]
        except OSError as exc:
            failures.append(f"{filename}: {exc}")
    if failures:
        raise NativeRuntimeError(
            "Microsoft Visual C++ 2015-2022 x64 Runtime is unavailable:\n  - "
            + "\n  - ".join(failures)
            + "\nInstall the official x64 runtime and rerun the builder: "
            "https://aka.ms/vc14/vc_redist.x64.exe"
        )


def _system_msvc_runtime_entries() -> list[dict[str, Any]]:
    system_root = os.environ.get("SystemRoot", "").strip()
    if not system_root:
        raise NativeRuntimeError("SystemRoot is unavailable; cannot locate the x64 MSVC runtime")
    system_directory = Path(system_root) / "System32"
    entries: list[dict[str, Any]] = []
    for filename in MSVC_RUNTIME_DLLS:
        path = system_directory / filename
        _assert_x64_pe(path)
        entries.append(
            {
                "path": filename,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "machine": "0x8664",
            }
        )
    return entries


def _model_directory() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "app"
        / "assets"
        / "person_classifier"
    )


def _assert_model_assets_valid(model_directory: Path) -> None:
    """Recheck the exact model bytes immediately before native inference."""

    try:
        assets = load_manifest(model_directory / "MODEL_SOURCES.json")
    except ModelAssetError as exc:
        raise NativeRuntimeError(f"local model manifest is invalid: {exc}") from exc
    failures = [
        f"{asset.filename}: {error}"
        for asset in assets
        if (error := verify_file(model_directory / asset.filename, asset)) is not None
    ]
    if failures:
        raise NativeRuntimeError(
            "local model integrity check failed:\n  - " + "\n  - ".join(failures)
        )


def _read_ir_model_from_memory(core: Any, model_path: Path) -> Any:
    """Use OpenVINO's bytes/weights overload to remain Unicode-path safe."""

    weights_path = model_path.with_suffix(".bin")
    try:
        model_bytes = model_path.read_bytes()
        weights_bytes = weights_path.read_bytes()
    except OSError as exc:
        raise NativeRuntimeError(
            f"cannot read local OpenVINO IR bytes for {model_path.name}: {exc}"
        ) from exc
    if not model_bytes:
        raise NativeRuntimeError(f"local OpenVINO IR XML is empty: {model_path.name}")
    if not weights_bytes:
        raise NativeRuntimeError(f"local OpenVINO IR weights are empty: {weights_path.name}")
    return core.read_model(model=model_bytes, weights=weights_bytes)


def _exception_summary(exc: BaseException) -> str:
    try:
        detail = str(exc)
    except Exception as stringify_error:  # A native exception may itself be misencoded.
        detail = f"unprintable native error ({type(stringify_error).__name__})"
    return f"{type(exc).__name__}: {detail}"


def _run_real_inference(
    model_directory: Path,
    *,
    runtime_module: Any | None = None,
    cache_libs: Path | None = None,
) -> str:
    """Compile and infer both models with loaded-DLL assertions around Core.

    ``verify_source`` always injects the module returned by the shared native
    bootstrap after the wheel RECORD gate has passed.  The optional fallback
    remains solely for non-Windows development tests, where the upstream
    Unicode DLL defect and Win32 preload APIs do not exist.  That fallback must
    start in a fresh interpreter with existing disabled telemetry consent;
    checking a file cannot revoke consent cached by an earlier import.
    """

    try:
        import numpy  # type: ignore[import-not-found]
        if runtime_module is None:
            if sys.platform == "win32":
                cache_libs = prepare_openvino_native_runtime()
                runtime_module = load_openvino()
            else:
                assert_openvino_telemetry_disabled()
                assert_fresh_openvino_import()
                runtime_module = importlib.import_module("openvino")
        ov = runtime_module
    except Exception as exc:
        raise NativeRuntimeError(
            "the installed OpenVINO native runtime cannot be imported: "
            f"{type(exc).__name__}: {exc}\n"
            "If this is a Windows DLL load error, install or repair the official "
            "Microsoft Visual C++ x64 Runtime and rerun: "
            "https://aka.ms/vc14/vc_redist.x64.exe"
        ) from exc

    try:
        if cache_libs is not None:
            assert_loaded_openvino_from_cache(cache_libs)
        core = ov.Core()
        if cache_libs is not None:
            assert_loaded_openvino_from_cache(cache_libs)
    except Exception as exc:
        raise NativeRuntimeError(
            "installed OpenVINO failed while initializing Core: "
            + _exception_summary(exc)
        ) from exc

    compiled_gender: Any | None = None
    for filename in (
        "face-detection-retail-0004.xml",
        "age-gender-recognition-retail-0013.xml",
        "person-detection-retail-0013.xml",
        "person-attributes-recognition-crossroad-0230.xml",
    ):
        path = model_directory / filename
        try:
            model = _read_ir_model_from_memory(core, path)
        except NativeRuntimeError:
            raise
        except Exception as exc:
            raise NativeRuntimeError(
                f"installed OpenVINO failed while reading verified in-memory IR {filename}: "
                + _exception_summary(exc)
            ) from exc
        try:
            compiled = core.compile_model(model, "CPU")
            if filename == "age-gender-recognition-retail-0013.xml":
                compiled_gender = compiled
        except Exception as exc:
            raise NativeRuntimeError(
                f"installed OpenVINO failed while compiling {filename} for CPU: "
                + _exception_summary(exc)
            ) from exc
        try:
            shape = tuple(int(value) for value in compiled.input(0).shape)
            outputs = compiled([numpy.zeros(shape, dtype=numpy.float32)])
            if not outputs:
                raise NativeRuntimeError(f"OpenVINO returned no outputs for {filename}")
        except NativeRuntimeError:
            raise
        except Exception as exc:
            raise NativeRuntimeError(
                f"installed OpenVINO failed while inferring {filename} on CPU: "
                + _exception_summary(exc)
            ) from exc

    if compiled_gender is None:
        raise NativeRuntimeError("installed OpenVINO gender model was not compiled")
    try:
        backend_directory = Path(__file__).resolve().parents[1] / "backend"
        if str(backend_directory) not in sys.path:
            sys.path.insert(0, str(backend_directory))
        from app.person_recognition import exercise_local_openvino_gender_branch

        exercise_local_openvino_gender_branch(
            compiled_gender,
            face_model_path=model_directory / "face-detection-retail-0004.xml",
            gender_model_path=model_directory
            / "age-gender-recognition-retail-0013.xml",
        )
    except Exception as exc:
        raise NativeRuntimeError(
            "installed OpenVINO failed while exercising the production gender "
            "output parser: "
            + _exception_summary(exc)
        ) from exc
    return str(getattr(ov, "__version__", EXPECTED_OPENVINO_VERSION))


def verify_source(manifest_path: Path) -> None:
    _check_msvc_runtime()
    msvc_runtime_entries = _system_msvc_runtime_entries()
    version, entries = _installed_native_files()
    _validate_openvino_entry_set(entries)
    original_openvino_dll = _installed_openvino_dll_path()
    require_non_ascii_source = (
        os.environ.get("IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE", "").strip()
        == "1"
    )
    source_is_non_ascii = not _is_ascii_path(original_openvino_dll)
    if require_non_ascii_source and not source_is_non_ascii:
        raise NativeRuntimeError(
            "IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE=1 requires the original "
            "wheel openvino.dll to reside below a non-ASCII path; the test did "
            "not exercise the reported Windows failure topology"
        )

    # Do this only after the independent RECORD/PE gate above.  load_openvino()
    # cannot import the package until every wheel DLL has been verified and
    # atomically staged in the fixed-drive ASCII cache.
    try:
        cache_libs = Path(prepare_openvino_native_runtime()).resolve()
        ov = load_openvino()
        loaded_openvino_dll = assert_loaded_openvino_from_cache(cache_libs).resolve()
    except OpenVinoNativeBootstrapError as exc:
        raise NativeRuntimeError(
            "installed OpenVINO failed its verified ASCII native bootstrap: "
            + _exception_summary(exc)
        ) from exc

    model_directory = _model_directory()
    missing_models = [name for name in MODEL_FILENAMES if not (model_directory / name).is_file()]
    if missing_models:
        raise NativeRuntimeError("local model file(s) are missing: " + ", ".join(missing_models))
    _assert_model_assets_valid(model_directory)
    runtime_version = _run_real_inference(
        model_directory,
        runtime_module=ov,
        cache_libs=cache_libs,
    )
    try:
        loaded_openvino_dll = assert_loaded_openvino_from_cache(cache_libs).resolve()
    except OpenVinoNativeBootstrapError as exc:
        raise NativeRuntimeError(
            "OpenVINO left its verified ASCII native cache during inference: "
            + _exception_summary(exc)
        ) from exc
    payload = {
        "schema_version": 1,
        "openvino_version": version,
        "runtime_version": runtime_version,
        "files": entries,
        "msvc_runtime_files": msvc_runtime_entries,
        "native_bootstrap": {
            "schema_version": 1,
            "source_openvino_dll_non_ascii": source_is_non_ascii,
            "non_ascii_source_required": require_non_ascii_source,
            "cache_path_ascii": _is_ascii_path(cache_libs),
            "loaded_openvino_dll_path_ascii": _is_ascii_path(loaded_openvino_dll),
            "loaded_from_verified_cache": True,
            # Do not seal a developer username or absolute build path into the
            # release.  The generation name is content-addressed and is enough
            # to correlate builder/frozen diagnostics safely.
            "cache_generation": cache_libs.parent.name,
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path)
    print(
        f"Installed OpenVINO native smoke passed: {len(entries)} PE x64 files verified; "
        "two CPU models compiled and inferred; production gender outputs parsed."
    )
    print(
        "IGAC_SOURCE_OPENVINO_ASCII_OK:openvino_dll="
        f"{loaded_openvino_dll}"
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeRuntimeError(f"cannot read native manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise NativeRuntimeError(f"native manifest has an unsupported schema: {path}")
    if payload.get("openvino_version") != EXPECTED_OPENVINO_VERSION:
        raise NativeRuntimeError(f"native manifest has an unexpected OpenVINO version: {path}")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise NativeRuntimeError(f"native manifest has no files: {path}")
    msvc_runtime_files = payload.get("msvc_runtime_files")
    if not isinstance(msvc_runtime_files, list) or len(msvc_runtime_files) != len(MSVC_RUNTIME_DLLS):
        raise NativeRuntimeError(f"native manifest has an invalid MSVC runtime file list: {path}")
    return payload


def _frozen_native_root(dist_directory: Path) -> Path:
    modern = dist_directory / "_internal"
    if modern.is_dir():
        return modern
    raise NativeRuntimeError(f"PyInstaller native directory is missing: {modern}")


def verify_frozen(manifest_path: Path, dist_directory: Path) -> None:
    payload = _load_manifest(manifest_path)
    executable = dist_directory / "collector_core.exe"
    _assert_x64_pe(executable)
    native_root = _frozen_native_root(dist_directory)
    packaged_manifest = native_root / PACKAGED_MANIFEST_NAME
    try:
        packaged_manifest_size = packaged_manifest.stat().st_size
    except OSError as exc:
        raise NativeRuntimeError(
            f"frozen sealed native manifest is missing: {packaged_manifest}"
        ) from exc
    if not packaged_manifest.is_file() or packaged_manifest_size <= 0:
        raise NativeRuntimeError(
            f"frozen sealed native manifest is missing or empty: {packaged_manifest}"
        )
    if _sha256(packaged_manifest) != _sha256(manifest_path):
        raise NativeRuntimeError(
            "frozen sealed native manifest differs from the independently "
            "verified source manifest"
        )

    expected_entries = payload["files"]
    abi = _validate_openvino_entry_set(expected_entries)
    for entry in expected_entries:
        relative = Path(str(entry["path"]))
        path = native_root / relative
        _assert_x64_pe(path)
        size = path.stat().st_size
        digest = _sha256(path)
        if size != int(entry["size"]):
            raise NativeRuntimeError(f"frozen native file size mismatch: {relative.as_posix()}")
        if digest != str(entry["sha256"]):
            raise NativeRuntimeError(f"frozen native file SHA-256 mismatch: {relative.as_posix()}")

    msvc_entries = payload["msvc_runtime_files"]
    if {str(item.get("path", "")).casefold() for item in msvc_entries} != {
        filename.casefold() for filename in MSVC_RUNTIME_DLLS
    }:
        raise NativeRuntimeError("native manifest does not contain the exact MSVC runtime set")
    for entry in msvc_entries:
        filename = str(entry["path"])
        path = native_root / filename
        _assert_x64_pe(path)
        if path.stat().st_size != int(entry["size"]):
            raise NativeRuntimeError(f"frozen MSVC runtime size mismatch: {filename}")
        if _sha256(path) != str(entry["sha256"]):
            raise NativeRuntimeError(f"frozen MSVC runtime SHA-256 mismatch: {filename}")
    python_dlls = sorted(
        path for path in native_root.glob("python3*.dll")
        if re.fullmatch(r"python3\d{2}\.dll", path.name, flags=re.IGNORECASE)
    )
    expected_python_dll = native_root / f"python{abi}.dll"
    if len(python_dlls) != 1 or python_dlls[0].name.casefold() != expected_python_dll.name.casefold():
        raise NativeRuntimeError(
            f"expected exactly one matching app-local python{abi}.dll; found "
            + ", ".join(path.name for path in python_dlls)
        )
    _assert_x64_pe(python_dlls[0])

    model_directory = native_root / "app" / "assets" / "person_classifier"
    missing_models = [name for name in MODEL_FILENAMES if not (model_directory / name).is_file()]
    if missing_models:
        raise NativeRuntimeError(
            "frozen local model file(s) are missing from the exact package path: "
            + ", ".join(missing_models)
        )
    _assert_model_assets_valid(model_directory)
    print(
        f"Frozen OpenVINO layout passed: {len(expected_entries)} native files match the "
        "installed wheel; app-local MSVC/Python runtimes and model assets are present."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source", help="verify installed runtime and run real inference")
    source.add_argument("--manifest", type=Path, required=True)
    frozen = subparsers.add_parser("frozen", help="verify the exact PyInstaller onedir layout")
    frozen.add_argument("--manifest", type=Path, required=True)
    frozen.add_argument("--dist", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "source":
            verify_source(args.manifest.resolve())
        else:
            verify_frozen(args.manifest.resolve(), args.dist.resolve())
    except NativeRuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
