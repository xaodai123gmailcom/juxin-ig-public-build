"""Earliest fail-closed bootstrap for a frozen Windows OpenVINO runtime.

OpenVINO 2025.4.1 cannot reliably initialize ``ov.Core`` when its native DLLs
live below a Windows path containing non-ASCII characters.  PyInstaller runs
this hook before the backend entry point and before the model smoke hook.  It
therefore delegates to the application's shared native bootstrap while it is
still safe to copy, verify, and preload the sealed DLL set from an ASCII-only
cache.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PACKAGED_NATIVE_MANIFEST = "openvino-native-manifest.json"
WINDOWS_ONEDNN_MAX_CPU_ISA = "AVX2"


def _configure_windows_onednn_cpu_isa() -> None:
    """Apply the Windows oneDNN compatibility ceiling before app imports."""

    if sys.platform == "win32":
        os.environ["ONEDNN_MAX_CPU_ISA"] = WINDOWS_ONEDNN_MAX_CPU_ISA


def _fail(message: str) -> None:
    print(f"ERROR: frozen OpenVINO native bootstrap failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _openvino_was_imported() -> bool:
    return any(
        name == "openvino" or name.startswith("openvino.")
        for name in sys.modules
    )


def _prepare_frozen_openvino_native_runtime() -> Path | None:
    """Validate the packaged manifest and prepare the ASCII native DLL cache."""

    if not getattr(sys, "frozen", False):
        return None
    if sys.platform != "win32":
        _fail(f"unsupported frozen platform {sys.platform!r}; Windows x64 is required")

    raw_root = getattr(sys, "_MEIPASS", None)
    if not raw_root:
        _fail("sys._MEIPASS is unavailable")
    native_root = Path(raw_root).resolve()
    manifest_path = native_root / PACKAGED_NATIVE_MANIFEST
    source_openvino_dll = native_root / "openvino" / "libs" / "openvino.dll"
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
            _fail(f"packaged native manifest is missing or empty: {manifest_path}")
    except OSError as exc:
        _fail(f"cannot inspect packaged native manifest {manifest_path}: {exc}")

    require_non_ascii_source = os.environ.get(
        "IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE", ""
    ).strip()
    if require_non_ascii_source not in {"", "0", "1"}:
        _fail("invalid non-ASCII source verification flag")
    if require_non_ascii_source == "1" and _is_ascii_path(source_openvino_dll):
        _fail(
            "the Unicode-path smoke requested a non-ASCII packaged OpenVINO "
            f"source, but got {source_openvino_dll}"
        )

    # Importing the helper package is safe: app/__init__.py intentionally has
    # no OpenVINO imports.  Any OpenVINO module already present means this
    # earliest hook ran too late and the unsafe DLL location may already have
    # been selected, so fail closed instead of pretending to repair it.
    if _openvino_was_imported():
        _fail("OpenVINO was imported before the native ASCII bootstrap")
    try:
        from app.openvino_native_bootstrap import prepare_openvino_native_runtime

        cache_libs = Path(prepare_openvino_native_runtime()).resolve()
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"cannot prepare the sealed ASCII native runtime: {type(exc).__name__}: {exc}")

    if not cache_libs.is_dir():
        _fail(f"native bootstrap returned a missing cache directory: {cache_libs}")
    if not _is_ascii_path(cache_libs):
        _fail(f"native bootstrap returned a non-ASCII cache directory: {cache_libs}")

    # Keep an immutable diagnostic breadcrumb for the later frozen smoke and
    # for support logs.  The shared helper retains DLL directory/load handles.
    setattr(sys, "_igac_openvino_native_cache_libs", str(cache_libs))
    setattr(sys, "_igac_openvino_native_source_dll", str(source_openvino_dll))
    return cache_libs


_configure_windows_onednn_cpu_isa()
_prepare_frozen_openvino_native_runtime()
