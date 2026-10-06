"""Check-only privacy preflight for the pinned OpenVINO import paths.

Importing OpenVINO 2025.4.1 initializes openvino-telemetry 2025.2.0.  Its
no-dialog default can remain enabled when creation of a missing consent file
fails.  Before importing either package, require the existing consent file to
contain exactly ASCII ``0``.  This module uses only the standard library and
never writes settings, changes HOME, calls the opt-out API, or sends an event.

The check does not revoke consent cached by an earlier import.  Unowned imports
must also pass ``assert_fresh_openvino_import`` in a fresh interpreter.  These
checks are a precondition, not protection against other code concurrently
changing the consent file or importing/reconfiguring telemetry.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


class OpenVinoImportPrivacyError(RuntimeError):
    """OpenVINO must not be imported with unverified telemetry consent."""


def _consent_path() -> Path:
    """Match the pinned telemetry package's OS-specific base directory."""

    try:
        system = platform.system()
        if system == "Windows":
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if not local_app_data.strip():
                raise OpenVinoImportPrivacyError(
                    "OpenVINO import blocked: LOCALAPPDATA is unavailable"
                )
            # Do not strip or relocate an existing value: upstream expands the
            # environment variable verbatim when selecting its consent file.
            base = Path(local_app_data)
            directory = base / "Intel Corporation"
        elif system in {"Linux", "Darwin"}:
            base = Path.home()
            directory = base / "intel"
        else:
            raise OpenVinoImportPrivacyError(
                "OpenVINO import blocked: unsupported telemetry consent platform"
            )
        if not base.is_dir():
            raise OpenVinoImportPrivacyError(
                "OpenVINO import blocked: telemetry consent base is unavailable"
            )
        return directory / "openvino_telemetry"
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, OpenVinoImportPrivacyError):
            raise
        raise OpenVinoImportPrivacyError(
            "OpenVINO import blocked: telemetry consent path cannot be resolved"
        ) from exc


def assert_openvino_telemetry_disabled() -> Path:
    """Require existing exact ``b'0'`` consent without modifying any state.

    Readability is sufficient; a read-only HOME with an existing disabled file
    is valid.  Missing/unreadable/enabled/malformed state is never repaired here.
    Recheck on every call rather than caching an earlier filesystem result.
    """

    consent = _consent_path()
    try:
        with consent.open("rb") as source:
            value = source.read(2)
    except OSError as exc:
        raise OpenVinoImportPrivacyError(
            "OpenVINO import blocked: existing disabled telemetry consent "
            "is missing or unreadable; no setting was changed"
        ) from exc
    if value != b"0":
        raise OpenVinoImportPrivacyError(
            "OpenVINO import blocked: telemetry consent must contain exactly "
            "ASCII 0; no setting was changed"
        )
    return consent


def assert_fresh_openvino_import() -> None:
    """Reject previously imported packages whose cached consent is unowned."""

    roots = ("openvino", "openvino_telemetry")
    if any(
        name == root or name.startswith(root + ".")
        for name in tuple(sys.modules)
        for root in roots
    ):
        raise OpenVinoImportPrivacyError(
            "OpenVINO import blocked: OpenVINO or telemetry was already imported "
            "outside this guarded load; a fresh interpreter is required"
        )
