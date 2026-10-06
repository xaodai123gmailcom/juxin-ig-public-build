"""Fail-closed Windows bootstrap for OpenVINO native libraries.

OpenVINO 2025.4.1 can fail while constructing ``ov.Core`` when its wheel is
installed below a non-ASCII Windows path.  The native runtime resolves plugin
paths from the loaded ``openvino.dll`` module and some of those paths pass
through a narrow-string conversion.  This module therefore verifies every DLL
listed by the installed wheel RECORD (or the frozen build manifest), atomically
mirrors those files into a per-user ASCII-only cache, and explicitly loads TBB
and OpenVINO from that cache *before* importing the Python package.

The public entry point is :func:`load_openvino`.  Code must not import
``openvino`` directly before calling it.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import ntpath
import os
import re
import secrets
import shutil
import stat as stat_module
import struct
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from .openvino_import_privacy import (
    OpenVinoImportPrivacyError,
    assert_fresh_openvino_import,
    assert_openvino_telemetry_disabled,
)


EXPECTED_OPENVINO_VERSION = "2025.4.1"
PACKAGED_MANIFEST_NAME = "openvino-native-manifest.json"
CACHE_MANIFEST_NAME = "native-manifest.json"
REQUIRED_PRELOAD_DLLS = ("tbb12.dll", "openvino.dll")
REQUIRED_RUNTIME_DLLS = (
    *REQUIRED_PRELOAD_DLLS,
    "openvino_ir_frontend.dll",
    "openvino_intel_cpu_plugin.dll",
)
PACKAGED_MSVC_RUNTIME_DLLS = (
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
)
_DLL_PATH_PREFIX = ("openvino", "libs")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CACHE_GENERATION_RE = re.compile(
    r"v[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}-[0-9a-f]{20}\Z"
)
_EXPECTED_PE_MACHINE = 0x8664
_WINDOWS_ONEDNN_MAX_CPU_ISA = "AVX2"


def _configure_windows_onednn_cpu_isa() -> None:
    """Cap oneDNN JIT dispatch before any OpenVINO native library is loaded.

    Some Windows Server 2025 hosts advertise AMX/BF16 support that oneDNN later
    cannot execute reliably, terminating the process with 0xC000001D.  AVX2 is
    the stable upper bound for both those hosts and normal Windows desktops.
    """

    if sys.platform == "win32":
        os.environ["ONEDNN_MAX_CPU_ISA"] = _WINDOWS_ONEDNN_MAX_CPU_ISA


_configure_windows_onednn_cpu_isa()


class OpenVinoNativeBootstrapError(RuntimeError):
    """Raised when the native runtime cannot be placed or loaded safely."""


@dataclass(frozen=True)
class NativeDll:
    """One trusted DLL and its immutable integrity metadata."""

    relative_path: str
    size: int
    sha256: str
    source_path: Path


@dataclass(frozen=True)
class NativeSource:
    """Verified source DLL set used to create one cache generation."""

    version: str
    entries: tuple[NativeDll, ...]
    digest: str


_PROCESS_LOCK = threading.RLock()
_PREPARED_CACHE_LIBS: Path | None = None
_OPENVINO_MODULE: Any | None = None
_CACHE_DIRECTORY_HANDLES: list[Any] = []
_DLL_DIRECTORY_HANDLES: list[Any] = []
_NATIVE_LIBRARY_HANDLES: list[Any] = []


# Win32 constants used by the cache security boundary.  Keep these local rather
# than depending on pywin32: the frozen Core intentionally ships only the pinned
# application dependencies.
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_PERSISTENT_ACLS = 0x00000008
_FILE_READ_ATTRIBUTES = 0x00000080
_READ_CONTROL = 0x00020000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_ALREADY_EXISTS = 183
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERITED_ACE = 0x10
_FILE_ALL_ACCESS = 0x001F01FF
_SECURITY_DESCRIPTOR_REVISION = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _Overlapped(ctypes.Structure):
    # The Offset/OffsetHigh pair occupies the same union slot as Pointer.  The
    # lock uses a zeroed OVERLAPPED, so spelling out the DWORD pair preserves
    # the correct x64 layout without a platform-specific ctypes union.
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", wintypes.HANDLE),
    ]


class _FileAttributeTagInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_uint32),
        ("ReparseTag", ctypes.c_uint32),
    ]


class _OwnedWinHandle:
    """One non-inheritable Win32 handle with deterministic close semantics."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot read native runtime file {path.name}: {exc}"
        ) from exc
    return digest.hexdigest()


def _verify_file(path: Path, entry: NativeDll) -> None:
    try:
        stat = path.stat()
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"required OpenVINO DLL is unavailable: {entry.relative_path}: {exc}"
        ) from exc
    if not path.is_file() or stat.st_size != entry.size:
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO DLL size mismatch: {entry.relative_path}"
        )
    if _sha256(path) != entry.sha256:
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO DLL SHA-256 mismatch: {entry.relative_path}"
        )
    _assert_x64_pe(path, entry.relative_path)


def _assert_x64_pe(path: Path, label: str | None = None) -> None:
    """Verify the PE signature and x64 COFF machine without loading the DLL."""

    display = label or path.name
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO DLL is not a PE file: {display}"
                )
            stream.seek(0x3C)
            offset_bytes = stream.read(4)
            if len(offset_bytes) != 4:
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO DLL has a truncated DOS header: {display}"
                )
            pe_offset = struct.unpack("<I", offset_bytes)[0]
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO DLL has an invalid PE signature: {display}"
                )
            machine_bytes = stream.read(2)
            if len(machine_bytes) != 2:
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO DLL has a truncated COFF header: {display}"
                )
            machine = struct.unpack("<H", machine_bytes)[0]
    except OpenVinoNativeBootstrapError:
        raise
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot inspect OpenVINO PE file {display}: {exc}"
        ) from exc
    if machine != _EXPECTED_PE_MACHINE:
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO DLL is not Windows x64 PE "
            f"(machine=0x{machine:04x}): {display}"
        )


def _decode_record_hash(value: Any) -> str:
    if value is None or getattr(value, "mode", "") != "sha256":
        raise OpenVinoNativeBootstrapError(
            "OpenVINO RECORD must contain SHA-256 for every native DLL"
        )
    encoded = str(getattr(value, "value", ""))
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded) is None:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO RECORD contains an invalid SHA-256 digest"
        )
    try:
        decoded = base64.b64decode(
            encoded + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO RECORD contains an invalid SHA-256 digest"
        ) from exc
    if len(decoded) != 32:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO RECORD contains an invalid SHA-256 digest"
        )
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != encoded:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO RECORD contains a non-canonical SHA-256 digest"
        )
    return decoded.hex()


def _normalized_dll_path(raw_path: Any) -> str | None:
    value = str(raw_path).replace("\\", "/")
    raw_parts = value.split("/")
    pure = PurePosixPath(value)
    if (
        not value
        or not value.isascii()
        or pure.is_absolute()
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise OpenVinoNativeBootstrapError(
            f"native manifest contains an unsafe path: {value!r}"
        )
    if len(pure.parts) < 3 or tuple(part.casefold() for part in pure.parts[:2]) != (
        "openvino",
        "libs",
    ):
        return None
    if pure.suffix.casefold() != ".dll":
        return None
    if len(pure.parts) != 3 or re.fullmatch(
        r"[A-Za-z0-9_.-]+\.dll",
        pure.parts[2],
        flags=re.IGNORECASE | re.ASCII,
    ) is None:
        raise OpenVinoNativeBootstrapError(
            f"native manifest contains an unsafe DLL name: {value!r}"
        )
    return pure.as_posix()


def _is_installed_dll_record(raw_path: Any) -> bool:
    """Select only wheel RECORD entries owned by ``openvino/libs``.

    Python installers legitimately expose console launchers through RECORD
    paths such as ``../../../Scripts/benchmark_app.exe``.  Those entries are
    outside this bootstrap's native DLL trust set and must be ignored before
    the strict manifest path validator sees them.  A path that claims to be an
    ``openvino/libs`` DLL is still passed to :func:`_normalized_dll_path`, so a
    traversal hidden below that prefix remains fail-closed.
    """

    value = str(raw_path).replace("\\", "/")
    if not value.isascii():
        return False
    value = value.casefold()
    return value.startswith("openvino/libs/") and value.endswith(".dll")


def _entry_from_manifest(raw: Any, source_root: Path) -> NativeDll | None:
    if not isinstance(raw, dict):
        raise OpenVinoNativeBootstrapError("native manifest files[] must be objects")
    relative = _normalized_dll_path(raw.get("path", ""))
    if relative is None:
        return None
    size = raw.get("size")
    digest = str(raw.get("sha256", "")).casefold()
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise OpenVinoNativeBootstrapError(
            f"native manifest has invalid size for {relative}"
        )
    if _SHA256_RE.fullmatch(digest) is None:
        raise OpenVinoNativeBootstrapError(
            f"native manifest has invalid SHA-256 for {relative}"
        )
    subpath = Path(*PurePosixPath(relative).parts[2:])
    path = (source_root / subpath).resolve()
    try:
        path.relative_to(source_root.resolve())
    except ValueError as exc:
        raise OpenVinoNativeBootstrapError(
            f"native manifest DLL escapes its source directory: {relative}"
        ) from exc
    return NativeDll(relative, size, digest, path)


def _validate_entry_set(entries: Sequence[NativeDll]) -> tuple[NativeDll, ...]:
    if not entries:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO native metadata contains no openvino/libs DLLs"
        )
    ordered = tuple(sorted(entries, key=lambda item: item.relative_path.casefold()))
    keys = [item.relative_path.casefold() for item in ordered]
    if len(keys) != len(set(keys)):
        raise OpenVinoNativeBootstrapError(
            "OpenVINO native metadata contains duplicate DLL paths"
        )
    paths = set(keys)
    missing = [
        name
        for name in REQUIRED_RUNTIME_DLLS
        if f"openvino/libs/{name}".casefold() not in paths
    ]
    if missing:
        raise OpenVinoNativeBootstrapError(
            "OpenVINO native metadata is missing: " + ", ".join(missing)
        )
    return ordered


def _source_digest(version: str, entries: Sequence[NativeDll]) -> str:
    payload = {
        "schema_version": 1,
        "openvino_version": version,
        "files": [
            {
                "path": item.relative_path,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in entries
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _verified_native_source(
    version: str, entries: Sequence[NativeDll]
) -> NativeSource:
    if version != EXPECTED_OPENVINO_VERSION:
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO version {version!r} is not the pinned "
            f"{EXPECTED_OPENVINO_VERSION!r} runtime"
        )
    ordered = _validate_entry_set(entries)
    for entry in ordered:
        _verify_file(entry.source_path, entry)
    return NativeSource(version, ordered, _source_digest(version, ordered))


def _installed_native_source() -> NativeSource:
    try:
        distribution = importlib.metadata.distribution("openvino")
    except importlib.metadata.PackageNotFoundError as exc:
        raise OpenVinoNativeBootstrapError("OpenVINO is not installed") from exc
    records = distribution.files
    if records is None:
        raise OpenVinoNativeBootstrapError("OpenVINO installation has no RECORD list")

    source_root = Path(distribution.locate_file("openvino/libs")).resolve()
    entries: list[NativeDll] = []
    for record in records:
        if not _is_installed_dll_record(record):
            continue
        relative = _normalized_dll_path(record)
        if relative is None:  # Defensive: the selector and normalizer must agree.
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO RECORD DLL is outside openvino/libs: {record!s}"
            )
        size = getattr(record, "size", None)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO RECORD has no valid size for {relative}"
            )
        digest = _decode_record_hash(getattr(record, "hash", None))
        path = Path(distribution.locate_file(record)).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO RECORD DLL escapes its source directory: {relative}"
            ) from exc
        entries.append(NativeDll(relative, size, digest, path))
    return _verified_native_source(str(distribution.version), entries)


def _packaged_manifest_path(native_root: Path) -> Path:
    candidate = native_root / PACKAGED_MANIFEST_NAME
    if candidate.is_file():
        return candidate
    raise OpenVinoNativeBootstrapError(
        f"frozen OpenVINO manifest {PACKAGED_MANIFEST_NAME!r} is missing"
    )


def _verify_frozen_msvc_runtime(payload: dict[str, Any], native_root: Path) -> None:
    """Bind the frozen process to the exact app-local VC++ runtime files."""

    raw_entries = payload.get("msvc_runtime_files")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(
        PACKAGED_MSVC_RUNTIME_DLLS
    ):
        raise OpenVinoNativeBootstrapError(
            "frozen native manifest has an invalid MSVC runtime file list"
        )
    expected = {filename.casefold() for filename in PACKAGED_MSVC_RUNTIME_DLLS}
    seen: set[str] = set()
    root = native_root.resolve()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise OpenVinoNativeBootstrapError(
                "frozen MSVC runtime manifest entries must be objects"
            )
        filename = raw.get("path")
        if (
            not isinstance(filename, str)
            or not filename
            or "/" in filename
            or "\\" in filename
            or filename.casefold() not in expected
            or filename.casefold() in seen
        ):
            raise OpenVinoNativeBootstrapError(
                "frozen native manifest does not contain the exact MSVC runtime set"
            )
        size = raw.get("size")
        digest = str(raw.get("sha256", "")).casefold()
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise OpenVinoNativeBootstrapError(
                f"frozen MSVC runtime has invalid size metadata: {filename}"
            )
        if _SHA256_RE.fullmatch(digest) is None:
            raise OpenVinoNativeBootstrapError(
                f"frozen MSVC runtime has invalid SHA-256 metadata: {filename}"
            )
        path = (root / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise OpenVinoNativeBootstrapError(
                f"frozen MSVC runtime escapes its native directory: {filename}"
            ) from exc
        try:
            stat = path.stat()
        except OSError as exc:
            raise OpenVinoNativeBootstrapError(
                f"required app-local MSVC runtime is unavailable: {filename}: {exc}"
            ) from exc
        if not path.is_file() or stat.st_size != size:
            raise OpenVinoNativeBootstrapError(
                f"frozen MSVC runtime size mismatch: {filename}"
            )
        if _sha256(path) != digest:
            raise OpenVinoNativeBootstrapError(
                f"frozen MSVC runtime SHA-256 mismatch: {filename}"
            )
        _assert_x64_pe(path, filename)
        seen.add(filename.casefold())
    if seen != expected:
        raise OpenVinoNativeBootstrapError(
            "frozen native manifest does not contain the exact MSVC runtime set"
        )


def _frozen_native_source() -> NativeSource:
    raw_root = getattr(sys, "_MEIPASS", None)
    if not raw_root:
        raise OpenVinoNativeBootstrapError("frozen runtime has no sys._MEIPASS")
    native_root = Path(raw_root).resolve()
    manifest_path = _packaged_manifest_path(native_root)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot read frozen OpenVINO manifest: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise OpenVinoNativeBootstrapError(
            "frozen OpenVINO manifest has an unsupported schema"
        )
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise OpenVinoNativeBootstrapError(
            "frozen OpenVINO manifest has no files[] entries"
        )
    source_root = (native_root / "openvino" / "libs").resolve()
    entries = [
        entry
        for raw in raw_files
        if (entry := _entry_from_manifest(raw, source_root)) is not None
    ]
    source = _verified_native_source(
        str(payload.get("openvino_version", "")), entries
    )
    # This is executed by the earliest PyInstaller hook.  Re-check the sealed
    # app-local VC++ runtime here, not only during the build, so a missing or
    # modified packaged DLL cannot silently fall back to a system copy.
    _verify_frozen_msvc_runtime(payload, native_root)
    return source


def _resolve_native_source() -> NativeSource:
    source = (
        _frozen_native_source()
        if getattr(sys, "frozen", False)
        else _installed_native_source()
    )
    gate = os.environ.get("IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE", "").strip()
    if gate not in ("", "0", "1"):
        raise OpenVinoNativeBootstrapError(
            "IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE must be 0 or 1"
        )
    if gate == "1":
        openvino_entry = next(
            (
                entry
                for entry in source.entries
                if PurePosixPath(entry.relative_path).name.casefold() == "openvino.dll"
            ),
            None,
        )
        if openvino_entry is None or all(
            ord(character) <= 0x7F for character in str(openvino_entry.source_path)
        ):
            raise OpenVinoNativeBootstrapError(
                "test gate requires the original openvino.dll source path to contain "
                "at least one non-ASCII character"
            )
    return source


def _windows_user_sid() -> str:
    """Return the current token's canonical SID string through pointer-safe APIs."""

    if sys.platform != "win32":
        raise OpenVinoNativeBootstrapError("Windows x64 is required")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            if required.value <= 0:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token, 1, buffer, required, ctypes.byref(required)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            class SidAndAttributes(ctypes.Structure):
                _fields_ = [
                    ("sid", ctypes.c_void_p),
                    ("attributes", wintypes.DWORD),
                ]

            token_user = ctypes.cast(
                buffer, ctypes.POINTER(SidAndAttributes)
            ).contents
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                token_user.sid, ctypes.byref(sid_text)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                sid = str(sid_text.value or "")
            finally:
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError) as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot resolve the current Windows user SID: {exc}"
        ) from exc
    if not sid.startswith("S-"):
        raise OpenVinoNativeBootstrapError("Windows returned an invalid current-user SID")
    return sid


def _windows_user_key(sid: str | None = None) -> str:
    """Return a non-identifying, stable key derived from the current Windows SID."""

    value = sid or _windows_user_sid()
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OpenVinoNativeBootstrapError("Windows returned a non-ASCII SID") from exc
    if not value.startswith("S-"):
        raise OpenVinoNativeBootstrapError("Windows returned an invalid current-user SID")
    return "u-" + hashlib.sha256(encoded).hexdigest()[:24]


def _cache_base_candidates() -> list[Path]:
    values: list[str] = []
    override = os.environ.get("IGAC_OPENVINO_CACHE_ROOT", "").strip()
    if override:
        values.append(override)
    for name in ("LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC"):
        value = os.environ.get(name, "").strip()
        if value:
            values.append(value)
    system_root = os.environ.get("SystemRoot", "").strip()
    if system_root:
        values.append(str(Path(system_root) / "Temp"))
    temporary = os.environ.get("TEMP", "").strip()
    if temporary:
        values.append(temporary)
    unique: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = ntpath.normcase(ntpath.normpath(value))
        if key not in seen:
            seen.add(key)
            unique.append(Path(value))
    return unique


def _is_strict_ascii_local_path(path: Path) -> bool:
    value = str(path)
    if not value or any(ord(character) > 0x7F for character in value):
        return False
    drive, tail = ntpath.splitdrive(value)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        root = drive + "\\"
        return int(kernel32.GetDriveTypeW(root)) == 3
    except (AttributeError, OSError):
        return False


def _probe_writable(directory: Path) -> None:
    probe = directory / f".igac-write-{os.getpid()}-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"ok")
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _security_descriptor_from_sddl(sddl: str) -> Iterator[ctypes.c_void_p]:
    """Allocate one self-relative Windows security descriptor from trusted SDDL."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SECURITY_DESCRIPTOR_REVISION,
        ctypes.byref(descriptor),
        ctypes.byref(size),
    ):
        raise OpenVinoNativeBootstrapError(
            "cannot construct the private OpenVINO cache security descriptor: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        yield descriptor
    finally:
        kernel32.LocalFree(descriptor)


def _private_cache_sddl(user_sid: str) -> str:
    # The user tree is the first child below a potentially shared ASCII base.
    # Inheritance is disabled at that boundary and only the current token,
    # LocalSystem and built-in Administrators receive inheritable full access.
    return (
        f"O:{user_sid}D:P"
        f"(A;OICI;FA;;;{user_sid})"
        "(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)"
    )


def _create_private_directory(path: Path, user_sid: str) -> None:
    """Create a protected directory without an inheritable insecure window."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    with _security_descriptor_from_sddl(_private_cache_sddl(user_sid)) as descriptor:
        security = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            descriptor,
            False,
        )
        if kernel32.CreateDirectoryW(str(path), ctypes.byref(security)):
            return
        error = ctypes.get_last_error()
        # Do not inspect an existing name here: following it before the
        # OPEN_REPARSE_POINT/no-share-delete guard would reintroduce a name
        # replacement window.  The guarded handle performs the type check.
        if error == _ERROR_ALREADY_EXISTS:
            return
        raise OpenVinoNativeBootstrapError(
            f"cannot create private OpenVINO cache directory {path}: "
            f"{ctypes.WinError(error)}"
        )


def _sid_string(pointer: ctypes.c_void_p) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
        raise OpenVinoNativeBootstrapError(
            "cannot inspect an OpenVINO cache ACL SID: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        return str(text.value or "")
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _verify_private_directory_acl(path: Path, user_sid: str) -> None:
    """Require an exact protected current-user/System/Administrators DACL."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = int(
        advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != 0:
        raise OpenVinoNativeBootstrapError(
            f"cannot read OpenVINO cache directory security for {path}: "
            f"{ctypes.WinError(result)}"
        )
    try:
        owner_defaulted = wintypes.BOOL()
        actual_owner = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(actual_owner),
            ctypes.byref(owner_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        actual_dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(actual_dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            not actual_owner.value
            or bool(owner_defaulted.value)
            or _sid_string(actual_owner) != user_sid
            or not bool(dacl_present.value)
            or bool(dacl_defaulted.value)
            or not actual_dacl.value
            or not (int(control.value) & _SE_DACL_PROTECTED)
        ):
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO cache directory has an unsafe owner or inherited DACL: {path}"
            )

        information = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            actual_dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(information.AceCount) != 3:
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO cache DACL must contain exactly three access entries: {path}"
            )
        expected_sids = {user_sid, "S-1-5-18", "S-1-5-32-544"}
        seen_sids: set[str] = set()
        for index in range(int(information.AceCount)):
            raw_ace = ctypes.c_void_p()
            if not advapi32.GetAce(
                actual_dacl,
                index,
                ctypes.byref(raw_ace),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            ace = ctypes.cast(raw_ace, ctypes.POINTER(_AccessAllowedAce)).contents
            if (
                int(ace.Header.AceType) != _ACCESS_ALLOWED_ACE_TYPE
                or int(ace.Header.AceFlags)
                != (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
                or int(ace.Header.AceFlags) & _INHERITED_ACE
                or int(ace.Mask) != _FILE_ALL_ACCESS
            ):
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO cache DACL contains an unsafe access entry: {path}"
                )
            sid_pointer = ctypes.c_void_p(
                int(raw_ace.value) + _AccessAllowedAce.SidStart.offset
            )
            sid = _sid_string(sid_pointer)
            if sid not in expected_sids or sid in seen_sids:
                raise OpenVinoNativeBootstrapError(
                    f"OpenVINO cache DACL contains an unexpected principal: {path}"
                )
            seen_sids.add(sid)
        if seen_sids != expected_sids:
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO cache DACL is missing a required principal: {path}"
            )
    except OpenVinoNativeBootstrapError:
        raise
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot validate OpenVINO cache directory security for {path}: {exc}"
        ) from exc
    finally:
        kernel32.LocalFree(descriptor)


def _has_reparse_attribute(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot inspect OpenVINO cache path component {path}: {exc}"
        ) from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat_module.S_ISLNK(metadata.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_no_reparse_components(path: Path) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if _has_reparse_attribute(current):
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO cache path contains a reparse point: {current}"
            )


def _open_directory_guard(path: Path) -> _OwnedWinHandle:
    """Open a directory without FILE_SHARE_DELETE and reject reparse handles."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES | _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not handle or int(handle) == _INVALID_HANDLE_VALUE:
        raise OpenVinoNativeBootstrapError(
            f"cannot guard OpenVINO cache directory {path}: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    owned = _OwnedWinHandle(handle)
    information = _FileAttributeTagInformation()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        9,  # FileAttributeTagInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        owned.close()
        raise OpenVinoNativeBootstrapError(
            f"cannot inspect guarded OpenVINO cache directory {path}: {error}"
        )
    if (
        not int(information.FileAttributes) & _FILE_ATTRIBUTE_DIRECTORY
        or int(information.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        owned.close()
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO cache guard did not open a normal directory: {path}"
        )
    return owned


def _assert_volume_supports_persistent_acls(
    guard: _OwnedWinHandle,
    path: Path,
) -> None:
    """Reject fixed volumes that do not persist/enforce Windows DACLs."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetVolumeInformationByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    serial = wintypes.DWORD()
    maximum_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationByHandleW(
        guard.handle,
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        None,
        0,
    ):
        raise OpenVinoNativeBootstrapError(
            f"cannot inspect cache volume security capabilities for {path}: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    if not int(flags.value) & _FILE_PERSISTENT_ACLS:
        raise OpenVinoNativeBootstrapError(
            f"cache volume does not enforce persistent Windows ACLs: {path}"
        )


def _retain_cache_directory_handles(handles: Sequence[Any]) -> None:
    _CACHE_DIRECTORY_HANDLES.extend(handles)
    retained = getattr(sys, "_igac_openvino_cache_directory_handles", None)
    if not isinstance(retained, list):
        retained = []
        setattr(sys, "_igac_openvino_cache_directory_handles", retained)
    retained.extend(handles)


def _secure_cache_candidate(
    base: Path,
    *,
    user_key: str,
    user_sid: str,
) -> tuple[Path, tuple[_OwnedWinHandle, ...]]:
    """Create and guard a private user tree below one possibly shared base."""

    guards: list[_OwnedWinHandle] = []
    try:
        base.mkdir(parents=True, exist_ok=True)
        # Check the spelling supplied by the candidate before resolving it;
        # resolve() alone would silently follow a junction in the shared base.
        _assert_no_reparse_components(base)
        resolved_base = base.resolve(strict=True)
        if not _is_strict_ascii_local_path(resolved_base):
            raise OpenVinoNativeBootstrapError(
                f"cache base is not a fixed-drive ASCII path: {resolved_base}"
            )
        _assert_no_reparse_components(resolved_base)
        # Keep the shared base name stable for process lifetime as well.  The
        # handle does not change its ACL or deny creating children; it only
        # prevents an ancestor-name replacement from detaching the verified
        # private tree from the path later supplied to LoadLibrary.
        guards.append(_open_directory_guard(resolved_base))
        _assert_volume_supports_persistent_acls(guards[-1], resolved_base)

        # Never create a shared JuxinIGAC parent and then make its first user's
        # ACL exclusive.  The first child below the shared base is user-unique.
        user_root = resolved_base / f"JuxinIGAC-{user_key}"
        cache_root = user_root / "OpenVINO"
        for directory in (user_root, cache_root):
            _create_private_directory(directory, user_sid)
            # OPEN_REPARSE_POINT rejects a junction and the missing
            # FILE_SHARE_DELETE keeps this exact directory object from being
            # renamed or removed, including via FILE_DELETE_CHILD on its
            # shared parent.  Only then is a name-based ACL query safe.
            guards.append(_open_directory_guard(directory))
            _verify_private_directory_acl(directory, user_sid)

        _probe_writable(cache_root)
        resolved_cache = cache_root.resolve(strict=True)
        if (
            resolved_cache != cache_root
            or not _is_strict_ascii_local_path(resolved_cache)
            or _has_reparse_attribute(resolved_cache)
        ):
            raise OpenVinoNativeBootstrapError(
                f"OpenVINO cache changed while its secure guard was established: {cache_root}"
            )
        return resolved_cache, tuple(guards)
    except Exception:
        for guard in reversed(guards):
            guard.close()
        raise


def _select_cache_root(
    base_candidates: Sequence[Path] | None = None,
    *,
    user_key: str | None = None,
    user_sid: str | None = None,
) -> Path:
    if sys.platform != "win32":
        raise OpenVinoNativeBootstrapError("Windows x64 is required")
    sid = user_sid or _windows_user_sid()
    key = user_key or _windows_user_key(sid)
    if re.fullmatch(r"u-[0-9a-f]{24}", key) is None:
        raise OpenVinoNativeBootstrapError("native cache user key is invalid")
    failures: list[str] = []
    for base in base_candidates if base_candidates is not None else _cache_base_candidates():
        candidate = base / f"JuxinIGAC-{key}" / "OpenVINO"
        if ".." in re.split(r"[\\/]+", str(candidate)):
            failures.append(f"{base}: parent traversal is not allowed")
            continue
        if not _is_strict_ascii_local_path(candidate):
            failures.append(f"{base}: not a fixed-drive ASCII path")
            continue
        try:
            resolved, guards = _secure_cache_candidate(
                base,
                user_key=key,
                user_sid=sid,
            )
        except (OSError, RuntimeError, OpenVinoNativeBootstrapError) as exc:
            failures.append(f"{base}: {exc}")
            continue
        _retain_cache_directory_handles(guards)
        return resolved
    detail = "; ".join(failures) if failures else "no cache candidates are configured"
    raise OpenVinoNativeBootstrapError(
        "cannot create a private, writable, fixed-drive, ASCII-only per-user "
        "OpenVINO cache: "
        + detail
    )


@contextlib.contextmanager
def _cache_file_lock(cache_root: Path) -> Iterator[None]:
    """Serialize one protected cache across console and RDP Windows sessions."""

    if sys.platform != "win32":
        raise OpenVinoNativeBootstrapError("Windows x64 is required")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    lock_path = cache_root / ".igac-openvino-cache.lock"
    handle = kernel32.CreateFileW(
        str(lock_path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_ALWAYS,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not handle or int(handle) == _INVALID_HANDLE_VALUE:
        raise OpenVinoNativeBootstrapError(
            f"cannot open the protected OpenVINO cache lock: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    owned = _OwnedWinHandle(handle)
    information = _FileAttributeTagInformation()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        9,  # FileAttributeTagInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        owned.close()
        raise OpenVinoNativeBootstrapError(
            f"cannot inspect the protected OpenVINO cache lock: {error}"
        )
    if int(information.FileAttributes) & (
        _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        owned.close()
        raise OpenVinoNativeBootstrapError(
            "the protected OpenVINO cache lock is not a normal file"
        )
    overlapped = _Overlapped()
    acquired = False
    try:
        if not kernel32.LockFileEx(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        ):
            raise OpenVinoNativeBootstrapError(
                "cannot acquire the cross-session OpenVINO cache lock: "
                f"{ctypes.WinError(ctypes.get_last_error())}"
            )
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.UnlockFileEx(
                handle,
                0,
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(overlapped),
            )
        owned.close()


def _cache_relative_path(entry: NativeDll) -> Path:
    return Path(*PurePosixPath(entry.relative_path).parts[2:])


def _cache_manifest_payload(source: NativeSource) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "openvino_version": source.version,
        "source_digest": source.digest,
        "files": [
            {
                "path": entry.relative_path,
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in source.entries
        ],
    }


def _cache_tree_error(cache_libs: Path, source: NativeSource) -> str | None:
    """Describe the first unsafe cache-tree difference, or return ``None``.

    ``os.DirEntry.stat()`` deliberately reports ``st_nlink == 0`` on Windows.
    Use a fresh path-based ``os.stat()`` call so the hard-link check sees the
    real link count on every supported CPython version.
    """

    generation = cache_libs.parent
    expected_files = {CACHE_MANIFEST_NAME.casefold()}
    expected_directories = {"libs"}
    for entry in source.entries:
        parts = PurePosixPath(entry.relative_path).parts[2:]
        expected_files.add("/".join(("libs", *parts)).casefold())
        for length in range(1, len(parts)):
            expected_directories.add(
                "/".join(("libs", *parts[:length])).casefold()
            )

    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    stack: list[tuple[Path, tuple[str, ...]]] = [(generation, ())]
    try:
        root_metadata = generation.lstat()
        root_attributes = int(getattr(root_metadata, "st_file_attributes", 0))
        if (
            not stat_module.S_ISDIR(root_metadata.st_mode)
            or stat_module.S_ISLNK(root_metadata.st_mode)
            or root_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return "generation root is not a normal non-reparse directory"
        while stack:
            directory, relative_parts = stack.pop()
            with os.scandir(directory) as children:
                for child in children:
                    # DirEntry.stat() returns st_nlink=0 on Windows by design.
                    # os.stat(path) performs the metadata query required for a
                    # trustworthy hard-link count and also avoids stale cached
                    # DirEntry metadata during the final verification pass.
                    metadata = os.stat(child.path, follow_symlinks=False)
                    attributes = int(getattr(metadata, "st_file_attributes", 0))
                    child_parts = (*relative_parts, child.name)
                    key = "/".join(child_parts).casefold()
                    display = "/".join(child_parts)
                    if (
                        stat_module.S_ISLNK(metadata.st_mode)
                        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        return f"cache entry is a link or reparse point: {display!r}"
                    if stat_module.S_ISDIR(metadata.st_mode):
                        if key not in expected_directories:
                            return f"unexpected cache directory: {display!r}"
                        if key in seen_directories:
                            return f"duplicate cache directory: {display!r}"
                        seen_directories.add(key)
                        stack.append((Path(child.path), child_parts))
                    elif stat_module.S_ISREG(metadata.st_mode):
                        if key not in expected_files:
                            return f"unexpected cache file: {display!r}"
                        if key in seen_files:
                            return f"duplicate cache file: {display!r}"
                        link_count = int(metadata.st_nlink)
                        if link_count != 1:
                            return (
                                f"cache file hard-link count must be 1: {display!r} "
                                f"(got {link_count})"
                            )
                        seen_files.add(key)
                    else:
                        return f"cache entry is not a regular file or directory: {display!r}"
    except OSError as exc:
        return f"cannot inspect cache tree: {exc}"
    missing_files = sorted(expected_files - seen_files)
    if missing_files:
        return "cache tree is missing files: " + ", ".join(missing_files)
    missing_directories = sorted(expected_directories - seen_directories)
    if missing_directories:
        return "cache tree is missing directories: " + ", ".join(missing_directories)
    return None


def _cache_tree_is_exact(cache_libs: Path, source: NativeSource) -> bool:
    """Reject every unmanifested file, directory, hard link and reparse point."""

    return _cache_tree_error(cache_libs, source) is None


def _cache_validation_error(cache_libs: Path, source: NativeSource) -> str | None:
    tree_error = _cache_tree_error(cache_libs, source)
    if tree_error is not None:
        return tree_error

    manifest = cache_libs.parent / CACHE_MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"cannot read the cache manifest: {exc}"
    if payload != _cache_manifest_payload(source):
        return "cache manifest content does not match the verified OpenVINO source"
    try:
        for entry in source.entries:
            _verify_file(cache_libs / _cache_relative_path(entry), entry)
    except OpenVinoNativeBootstrapError as exc:
        return str(exc)
    return None


def _cache_is_valid(cache_libs: Path, source: NativeSource) -> bool:
    return _cache_validation_error(cache_libs, source) is None


def _copy_verified(source: NativeDll, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.source_path.open("rb") as input_stream, destination.open("xb") as output:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot stage OpenVINO DLL {source.relative_path}: {exc}"
        ) from exc
    if size != source.size or digest.hexdigest() != source.sha256:
        raise OpenVinoNativeBootstrapError(
            f"OpenVINO source changed while staging: {source.relative_path}"
        )


def _write_cache_manifest(directory: Path, source: NativeSource) -> None:
    payload = _cache_manifest_payload(source)
    path = directory / CACHE_MANIFEST_NAME
    temporary = directory / (CACHE_MANIFEST_NAME + ".tmp")
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise OpenVinoNativeBootstrapError(
            f"cannot seal the OpenVINO cache generation: {exc}"
        ) from exc


def _cleanup_stale_cache_generations_locked(
    cache_root: Path,
    current_generation: Path,
    *,
    keep_previous: int = 1,
) -> None:
    """Best-effort, bounded cleanup of old verified-generation directories.

    The caller owns the cross-process cache lock.  Only direct, normal,
    non-reparse children with the exact generation naming convention are ever
    considered.  The current generation and the newest ``keep_previous`` old
    generations are retained so a recently upgraded installation still has a
    rollback generation.  Unknown files and unsafe path types are deliberately
    left untouched.
    """

    if current_generation.parent != cache_root or not _CACHE_GENERATION_RE.fullmatch(
        current_generation.name
    ):
        raise OpenVinoNativeBootstrapError(
            "refusing to clean an invalid OpenVINO cache generation path"
        )
    candidates: list[tuple[int, str, Path]] = []
    try:
        children = list(cache_root.iterdir())
    except OSError:
        return
    for child in children:
        if child == current_generation:
            continue
        if child.parent != cache_root or not _CACHE_GENERATION_RE.fullmatch(child.name):
            continue
        try:
            metadata = child.lstat()
            if not stat_module.S_ISDIR(metadata.st_mode) or _has_reparse_attribute(child):
                continue
        except (OSError, OpenVinoNativeBootstrapError):
            continue
        candidates.append((metadata.st_mtime_ns, child.name, child))

    candidates.sort(reverse=True)
    for _, _, stale in candidates[max(0, keep_previous) :]:
        quarantine = cache_root / (
            f".{stale.name}.gc-{os.getpid()}-{secrets.token_hex(8)}"
        )
        try:
            os.replace(stale, quarantine)
            metadata = quarantine.lstat()
            if (
                quarantine.parent != cache_root
                or not stat_module.S_ISDIR(metadata.st_mode)
                or _has_reparse_attribute(quarantine)
            ):
                if not stale.exists():
                    os.replace(quarantine, stale)
                continue
            shutil.rmtree(quarantine)
        except (OSError, OpenVinoNativeBootstrapError):
            # A loaded DLL can keep a previous generation busy on Windows.
            # Cleanup is maintenance only and must never block application
            # startup; a later startup will retry it.
            try:
                if quarantine.exists() and not stale.exists():
                    os.replace(quarantine, stale)
            except OSError:
                pass


def _stage_cache_generation_locked(cache_root: Path, source: NativeSource) -> Path:
    """Publish or reuse a generation while the caller owns the cache lock."""

    generation = cache_root / f"v{source.version}-{source.digest[:20]}"
    cache_libs = generation / "libs"
    if _cache_is_valid(cache_libs, source):
        _cleanup_stale_cache_generations_locked(cache_root, generation)
        return cache_libs
    for stale in cache_root.glob(f".{generation.name}.stage-*"):
        shutil.rmtree(stale, ignore_errors=True)

    stage = cache_root / (
        f".{generation.name}.stage-{os.getpid()}-{secrets.token_hex(8)}"
    )
    bad: Path | None = None
    try:
        stage.mkdir()
        stage_libs = stage / "libs"
        stage_libs.mkdir()
        for entry in source.entries:
            _copy_verified(entry, stage_libs / _cache_relative_path(entry))
        _write_cache_manifest(stage, source)
        stage_error = _cache_validation_error(stage_libs, source)
        if stage_error is not None:
            raise OpenVinoNativeBootstrapError(
                "new OpenVINO cache generation failed its final integrity check: "
                + stage_error
            )
        if generation.exists():
            bad = cache_root / (
                f".{generation.name}.bad-{os.getpid()}-{secrets.token_hex(8)}"
            )
            os.replace(generation, bad)
        try:
            os.replace(stage, generation)
        except Exception:
            if bad is not None and bad.exists() and not generation.exists():
                os.replace(bad, generation)
                bad = None
            raise
        if bad is not None:
            shutil.rmtree(bad, ignore_errors=True)
    except OpenVinoNativeBootstrapError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise OpenVinoNativeBootstrapError(
            f"cannot atomically publish the OpenVINO cache: {exc}"
        ) from exc
    published_error = _cache_validation_error(cache_libs, source)
    if published_error is not None:
        raise OpenVinoNativeBootstrapError(
            "published OpenVINO cache generation failed verification: "
            + published_error
        )
    _cleanup_stale_cache_generations_locked(cache_root, generation)
    return cache_libs


def _stage_cache_generation(cache_root: Path, source: NativeSource) -> Path:
    """Compatibility wrapper that holds the cross-session publication lock."""

    with _cache_file_lock(cache_root):
        return _stage_cache_generation_locked(cache_root, source)


def _loaded_module_path(filename: str) -> Path | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleFileNameW.argtypes = [
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetModuleFileNameW.restype = wintypes.DWORD
    module = kernel32.GetModuleHandleW(filename)
    if not module:
        return None
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = int(kernel32.GetModuleFileNameW(module, buffer, size))
    if length <= 0 or length >= size - 1:
        raise OpenVinoNativeBootstrapError(
            f"cannot resolve the loaded path for {filename}"
        )
    return Path(buffer.value)


def _same_windows_path(left: Path, right: Path) -> bool:
    return ntpath.normcase(ntpath.abspath(str(left))) == ntpath.normcase(
        ntpath.abspath(str(right))
    )


def _assert_loaded_native_from_cache(cache_libs: Path, filename: str) -> Path:
    loaded = _loaded_module_path(filename)
    expected = cache_libs / filename
    if loaded is None:
        raise OpenVinoNativeBootstrapError(
            f"{filename} was not loaded from the verified ASCII cache"
        )
    if not _is_strict_ascii_local_path(loaded) or not _same_windows_path(loaded, expected):
        raise OpenVinoNativeBootstrapError(
            f"{filename} loaded from an unexpected path; expected the verified "
            f"ASCII cache, got {loaded}"
        )
    return loaded


def assert_loaded_openvino_from_cache(cache_libs: Path) -> Path:
    return _assert_loaded_native_from_cache(cache_libs, "openvino.dll")


def _load_native_dll(path: Path) -> Any:
    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS.
    return ctypes.WinDLL(str(path), winmode=0x00001100)  # type: ignore[attr-defined]


def _retain_handles(
    directory_handles_to_add: Sequence[Any],
    native_handles: Sequence[Any],
) -> None:
    _DLL_DIRECTORY_HANDLES.extend(directory_handles_to_add)
    _NATIVE_LIBRARY_HANDLES.extend(native_handles)
    directory_handles = getattr(sys, "_igac_openvino_dll_directory_handles", None)
    if not isinstance(directory_handles, list):
        directory_handles = []
        setattr(sys, "_igac_openvino_dll_directory_handles", directory_handles)
    directory_handles.extend(directory_handles_to_add)
    library_handles = getattr(sys, "_igac_openvino_native_library_handles", None)
    if not isinstance(library_handles, list):
        library_handles = []
        setattr(sys, "_igac_openvino_native_library_handles", library_handles)
    library_handles.extend(native_handles)


def prepare_openvino_native_runtime() -> Path:
    """Verify, mirror and preload native OpenVINO without importing its package."""

    global _PREPARED_CACHE_LIBS
    if sys.platform != "win32":
        raise OpenVinoNativeBootstrapError(
            "local OpenVINO person recognition requires Windows x64"
        )
    with _PROCESS_LOCK:
        if _PREPARED_CACHE_LIBS is not None:
            assert_loaded_openvino_from_cache(_PREPARED_CACHE_LIBS)
            return _PREPARED_CACHE_LIBS
        if "openvino" in sys.modules:
            raise OpenVinoNativeBootstrapError(
                "OpenVINO was imported before its verified native bootstrap"
            )
        for filename in REQUIRED_PRELOAD_DLLS:
            already_loaded = _loaded_module_path(filename)
            if already_loaded is not None:
                raise OpenVinoNativeBootstrapError(
                    f"{filename} was loaded before its verified native bootstrap: "
                    f"{already_loaded}"
                )

        source = _resolve_native_source()
        cache_root = _select_cache_root()
        # The same per-cache lock covers publication, final verification,
        # directory pinning and both native loads.  Another console/RDP session
        # therefore cannot replace a generation between verification and load.
        with _cache_file_lock(cache_root):
            cache_libs = _stage_cache_generation_locked(cache_root, source)
            if not _is_strict_ascii_local_path(cache_libs):
                raise OpenVinoNativeBootstrapError(
                    "verified OpenVINO cache is not a fixed-drive ASCII-only path"
                )
            add_dll_directory = getattr(os, "add_dll_directory", None)
            if add_dll_directory is None:
                raise OpenVinoNativeBootstrapError(
                    "os.add_dll_directory is unavailable"
                )

            generation_guards: list[_OwnedWinHandle] = []
            directory_handles: list[Any] = []
            native_handles: list[Any] = []
            try:
                # Pin both names without FILE_SHARE_DELETE before the final
                # exact-tree/hash pass.  Retaining these guards for process
                # lifetime prevents a later app instance from moving the
                # loaded generation or its libs directory out from underneath
                # the process.
                for guarded_directory in (cache_libs.parent, cache_libs):
                    generation_guards.append(
                        _open_directory_guard(guarded_directory)
                    )
                guarded_error = _cache_validation_error(cache_libs, source)
                if guarded_error is not None:
                    raise OpenVinoNativeBootstrapError(
                        "guarded OpenVINO cache generation failed final verification: "
                        + guarded_error
                    )

                directory_paths: list[Path] = []
                if getattr(sys, "frozen", False):
                    raw_native_root = getattr(sys, "_MEIPASS", None)
                    if not raw_native_root:
                        raise OpenVinoNativeBootstrapError(
                            "frozen runtime has no native dependency directory"
                        )
                    native_root = Path(raw_native_root).resolve()
                    if not native_root.is_dir():
                        raise OpenVinoNativeBootstrapError(
                            "frozen native dependency directory is missing: "
                            f"{native_root}"
                        )
                    # PyInstaller places the app-local MSVC runtime at
                    # _MEIPASS.  Keep that directory registered explicitly so
                    # loading the ASCII-staged OpenVINO DLL does not depend on
                    # a system-wide VC++ runtime.  openvino.dll itself is never
                    # loaded from here; its exact module path is asserted.
                    directory_paths.append(native_root)
                directory_paths.append(cache_libs)
                for directory in directory_paths:
                    directory_handles.append(add_dll_directory(str(directory)))
                for filename in REQUIRED_PRELOAD_DLLS:
                    native_handles.append(_load_native_dll(cache_libs / filename))
                    _assert_loaded_native_from_cache(cache_libs, filename)
            except Exception as exc:
                for directory_handle in reversed(directory_handles):
                    try:
                        directory_handle.close()
                    except Exception:
                        pass
                for generation_guard in reversed(generation_guards):
                    generation_guard.close()
                if isinstance(exc, OpenVinoNativeBootstrapError):
                    raise
                raise OpenVinoNativeBootstrapError(
                    f"cannot preload verified OpenVINO native libraries: {exc}"
                ) from exc
            _retain_cache_directory_handles(generation_guards)
            _retain_handles(directory_handles, native_handles)
            _PREPARED_CACHE_LIBS = cache_libs
            return cache_libs


def load_openvino() -> Any:
    """Return OpenVINO only after native and check-only privacy preflights."""

    global _OPENVINO_MODULE
    with _PROCESS_LOCK:
        if _OPENVINO_MODULE is not None:
            if _PREPARED_CACHE_LIBS is None:
                raise OpenVinoNativeBootstrapError(
                    "OpenVINO bootstrap state is internally inconsistent"
                )
            # This cache is owned: it is assigned only after the guarded import
            # below.  Rechecking the file does not revoke previously cached
            # consent, but prevents reuse when disabled state is no longer
            # verifiable.  An unowned module never reaches this branch.
            try:
                assert_openvino_telemetry_disabled()
            except OpenVinoImportPrivacyError as exc:
                raise OpenVinoNativeBootstrapError(str(exc)) from exc
            assert_loaded_openvino_from_cache(_PREPARED_CACHE_LIBS)
            return _OPENVINO_MODULE
        cache_libs = prepare_openvino_native_runtime()
        try:
            assert_openvino_telemetry_disabled()
            assert_fresh_openvino_import()
        except OpenVinoImportPrivacyError as exc:
            raise OpenVinoNativeBootstrapError(str(exc)) from exc
        try:
            module = importlib.import_module("openvino")
        except Exception as exc:
            raise OpenVinoNativeBootstrapError(
                f"verified OpenVINO Python package cannot be imported: {exc}"
            ) from exc
        assert_loaded_openvino_from_cache(cache_libs)
        _OPENVINO_MODULE = module
        return module


def _reset_for_tests() -> None:
    """Reset process-only state; tests must provide fake native handles."""

    global _PREPARED_CACHE_LIBS, _OPENVINO_MODULE
    _PREPARED_CACHE_LIBS = None
    _OPENVINO_MODULE = None
    for handle in reversed(_CACHE_DIRECTORY_HANDLES):
        try:
            handle.close()
        except Exception:
            pass
    _CACHE_DIRECTORY_HANDLES.clear()
    _DLL_DIRECTORY_HANDLES.clear()
    _NATIVE_LIBRARY_HANDLES.clear()
    for attribute in (
        "_igac_openvino_cache_directory_handles",
        "_igac_openvino_dll_directory_handles",
        "_igac_openvino_native_library_handles",
    ):
        try:
            delattr(sys, attribute)
        except AttributeError:
            pass
