#!/usr/bin/env python3
"""Create or verify the atomic Windows source-runtime readiness marker."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib
import json
import os
import platform
import struct
import sys
import sysconfig
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .download_person_models import load_manifest, verify_file
except ImportError:  # Direct execution: python scripts/verify_runtime_ready.py
    from download_person_models import load_manifest, verify_file


EXPECTED_OPENVINO_VERSION = "2025.4.1"
SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = PROJECT_ROOT / "backend"
if str(BACKEND_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIRECTORY))

# Safe to import before OpenVINO: this helper only inspects package metadata
# until its explicit prepare/load entry points are called.
from app.openvino_native_bootstrap import (  # noqa: E402
    OpenVinoNativeBootstrapError,
    assert_loaded_openvino_from_cache,
    load_openvino,
    prepare_openvino_native_runtime,
)

DEFAULT_MARKER = PROJECT_ROOT / ".venv" / "igac-runtime-ready.json"
FINGERPRINT_PATHS = (
    "package.json",
    "package-lock.json",
    "backend/requirements.txt",
    "backend/app/openvino_import_privacy.py",
    "backend/app/openvino_native_bootstrap.py",
    "backend/app/person_recognition.py",
    "backend/app/assets/person_classifier/MODEL_SOURCES.json",
    "scripts/verify_openvino_windows.py",
    "scripts/verify_runtime_ready.py",
    "scripts/install_python_dependencies.py",
)


class RuntimeReadinessError(RuntimeError):
    """Raised when an interrupted or stale source runtime cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeReadinessError(f"cannot hash required file {path}: {exc}") from exc
    return digest.hexdigest()


def _project_version() -> str:
    path = PROJECT_ROOT / "package.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeReadinessError(f"cannot read package version: {exc}") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise RuntimeReadinessError("package.json has no valid version")
    return version.strip()


def _fingerprints() -> dict[str, str]:
    return {
        relative: _sha256(PROJECT_ROOT / relative)
        for relative in FINGERPRINT_PATHS
    }


def _python_identity() -> dict[str, Any]:
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "machine": platform.machine().casefold(),
        "pointer_bits": struct.calcsize("P") * 8,
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "executable": str(Path(sys.executable).resolve()).casefold(),
    }


def _assert_supported_windows_python() -> None:
    identity = _python_identity()
    if (
        sys.platform != "win32"
        or not (3, 11) <= sys.version_info[:2] < (3, 15)
        or identity["implementation"] != "CPython"
        or identity["pointer_bits"] != 64
        or identity["machine"] not in {"amd64", "x86_64"}
        or identity["free_threaded"]
    ):
        raise RuntimeReadinessError(
            "standard 64-bit CPython 3.11-3.14 on Windows x64 is required"
        )


def _assert_telemetry_disabled() -> None:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise RuntimeReadinessError("LOCALAPPDATA is unavailable")
    consent = Path(local_app_data) / "Intel Corporation" / "openvino_telemetry"
    try:
        value = consent.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise RuntimeReadinessError(
            "the local-only OpenVINO telemetry setting is unavailable"
        ) from exc
    if value != "0":
        raise RuntimeReadinessError(
            "the local-only OpenVINO telemetry setting is not disabled"
        )


def _assert_models_valid() -> None:
    directory = (
        PROJECT_ROOT / "backend" / "app" / "assets" / "person_classifier"
    )
    failures = [
        f"{asset.filename}: {error}"
        for asset in load_manifest(directory / "MODEL_SOURCES.json")
        if (error := verify_file(directory / asset.filename, asset)) is not None
    ]
    if failures:
        raise RuntimeReadinessError(
            "local person-recognition models are missing or damaged: "
            + "; ".join(failures)
        )


def _assert_python_dependencies() -> None:
    requirements_path = PROJECT_ROOT / "backend" / "requirements.txt"
    try:
        from packaging.requirements import Requirement
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except (ImportError, OSError, UnicodeError) as exc:
        raise RuntimeReadinessError(
            f"cannot validate Python dependency requirements: {exc}"
        ) from exc
    failures: list[str] = []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            requirement = Requirement(value)
            installed = importlib.metadata.version(requirement.name)
        except (ValueError, importlib.metadata.PackageNotFoundError) as exc:
            failures.append(f"{value}: {exc}")
            continue
        if requirement.specifier and installed not in requirement.specifier:
            failures.append(f"{value}: installed {installed}")
    for module_name in (
        "fastapi",
        "uvicorn",
        "pydantic",
        "playwright.async_api",
        "openai",
        "httpx",
        "numpy",
        "PIL",
    ):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            failures.append(
                f"import {module_name}: {type(exc).__name__}: {exc}"
            )
    if failures:
        raise RuntimeReadinessError(
            "Python runtime dependencies are missing, incompatible, or damaged: "
            + "; ".join(failures)
        )


def _assert_openvino_loads() -> None:
    try:
        installed_version = importlib.metadata.version("openvino")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeReadinessError("openvino is not installed") from exc
    if installed_version != EXPECTED_OPENVINO_VERSION:
        raise RuntimeReadinessError(
            f"openvino {installed_version!r} is installed; "
            f"expected {EXPECTED_OPENVINO_VERSION!r}"
        )
    try:
        cache_libs = Path(prepare_openvino_native_runtime()).resolve()
        ov = load_openvino()
        assert_loaded_openvino_from_cache(cache_libs)
        from PIL import Image  # type: ignore[import-not-found]

        Image.new("RGB", (1, 1), "black")
        core = ov.Core()
        assert_loaded_openvino_from_cache(cache_libs)
        devices = {str(item).casefold() for item in core.available_devices}
        assert_loaded_openvino_from_cache(cache_libs)
    except OpenVinoNativeBootstrapError as exc:
        raise RuntimeReadinessError(
            f"OpenVINO verified ASCII native bootstrap failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeReadinessError(
            f"OpenVINO/Pillow runtime import failed: {type(exc).__name__}: {exc}"
        ) from exc
    if "cpu" not in devices:
        raise RuntimeReadinessError(
            "OpenVINO CPU device is unavailable; repair the local runtime"
        )


def _expected_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application_version": _project_version(),
        "openvino_version": EXPECTED_OPENVINO_VERSION,
        "python": _python_identity(),
        "fingerprints": _fingerprints(),
    }


def _validate_live_runtime() -> None:
    _assert_supported_windows_python()
    _assert_telemetry_disabled()
    _assert_python_dependencies()
    _assert_models_valid()
    _assert_openvino_loads()


def write_marker(path: Path) -> None:
    _validate_live_runtime()
    payload = _expected_payload()
    payload["verified_at_utc"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise RuntimeReadinessError(f"cannot write readiness marker {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    print(f"Runtime readiness marker written for v{payload['application_version']}.")


def check_marker(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeReadinessError("runtime readiness marker is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeReadinessError(f"runtime readiness marker is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeReadinessError("runtime readiness marker must be an object")
    expected = _expected_payload()
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeReadinessError(
                f"runtime readiness marker is stale or mismatched: {key}"
            )
    _validate_live_runtime()
    print(f"Runtime readiness verified for v{expected['application_version']}.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("write", "check"))
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "write":
            write_marker(args.marker.resolve())
        else:
            check_marker(args.marker.resolve())
    except RuntimeReadinessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
