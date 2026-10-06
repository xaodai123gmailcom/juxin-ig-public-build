"""Stop the local Core promptly when its owning desktop process disappears.

The Electron process passes its PID through ``IGAC_PARENT_PID``.  The variable is
intentionally optional so the Core can still be run directly from a terminal for
development and diagnostics.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Protocol


PARENT_PID_ENV = "IGAC_PARENT_PID"
PARENT_EXIT_CODE = 0
DEFAULT_CHECK_INTERVAL_SECONDS = 1.0

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


class _WindowsProcessApi(Protocol):
    def open_process(self, pid: int) -> int | None: ...

    def wait_for_single_object(self, handle: int, timeout_ms: int) -> int: ...

    def close_handle(self, handle: int) -> None: ...


class _Kernel32ProcessApi:
    """Small, testable wrapper around the Windows process-waiting primitives."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32 = kernel32

    def open_process(self, pid: int) -> int | None:
        handle = self._kernel32.OpenProcess(_SYNCHRONIZE, 0, pid)
        return int(handle) if handle else None

    def wait_for_single_object(self, handle: int, timeout_ms: int) -> int:
        return int(self._kernel32.WaitForSingleObject(handle, timeout_ms))

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


class ParentProcessWatchdog:
    """A daemon watchdog tied to the exact process that launched the Core.

    On Windows an open process handle continues to identify the original Electron
    process even if its numeric PID is later reused.  POSIX children are re-parented
    when their parent exits, so comparing ``getppid()`` is sufficient there.
    """

    def __init__(
        self,
        parent_pid: int,
        *,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        exit_process: Callable[[int], object] = os._exit,
        platform_name: str = sys.platform,
        get_parent_pid: Callable[[], int] = os.getppid,
        windows_api: _WindowsProcessApi | None = None,
    ) -> None:
        if parent_pid <= 0:
            raise ValueError("parent_pid must be positive")
        if check_interval_seconds <= 0:
            raise ValueError("check_interval_seconds must be positive")
        self.parent_pid = parent_pid
        self.check_interval_seconds = check_interval_seconds
        self._exit_process = exit_process
        self._platform_name = platform_name
        self._get_parent_pid = get_parent_pid
        self._windows_api = windows_api
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="igac-parent-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop monitoring; intended for orderly shutdown and unit tests."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.check_interval_seconds * 2))

    def _run(self) -> None:
        if self._platform_name == "win32":
            self._watch_windows_parent()
        else:
            self._watch_posix_parent()

    def _watch_windows_parent(self) -> None:
        # Validate that the supplied PID really is our launcher before opening it.
        # This also catches a parent that died between spawning and Core startup.
        if self._get_parent_pid() != self.parent_pid:
            self._exit_now()
            return

        api = self._windows_api or _Kernel32ProcessApi()
        handle = api.open_process(self.parent_pid)
        if handle is None:
            # Same-user Electron processes permit SYNCHRONIZE.  If even that handle
            # cannot be opened, continuing would recreate the orphan-Core failure.
            self._exit_now()
            return

        timeout_ms = max(50, int(self.check_interval_seconds * 1000))
        try:
            while not self._stop_event.is_set():
                result = api.wait_for_single_object(handle, timeout_ms)
                if result == _WAIT_TIMEOUT:
                    continue
                # WAIT_OBJECT_0 means the parent exited.  Any other result means the
                # handle can no longer be trusted, so fail closed instead of leaving
                # an unowned service bound to the local API port.
                self._exit_now()
                return
        finally:
            api.close_handle(handle)

    def _watch_posix_parent(self) -> None:
        while not self._stop_event.is_set():
            if self._get_parent_pid() != self.parent_pid:
                self._exit_now()
                return
            self._stop_event.wait(self.check_interval_seconds)

    def _exit_now(self) -> None:
        self._exit_process(PARENT_EXIT_CODE)


def start_parent_watchdog_from_env(
    environ: Mapping[str, str] | None = None,
    **watchdog_options: object,
) -> ParentProcessWatchdog | None:
    """Start the desktop ownership watchdog when ``IGAC_PARENT_PID`` is present."""

    source = os.environ if environ is None else environ
    raw_parent_pid = source.get(PARENT_PID_ENV, "").strip()
    if not raw_parent_pid:
        return None
    try:
        parent_pid = int(raw_parent_pid, 10)
    except ValueError as error:
        raise ValueError(f"{PARENT_PID_ENV} must be a positive integer") from error
    if parent_pid <= 0 or parent_pid == os.getpid():
        raise ValueError(f"{PARENT_PID_ENV} must identify a different live process")

    watchdog = ParentProcessWatchdog(parent_pid, **watchdog_options)
    watchdog.start()
    return watchdog

