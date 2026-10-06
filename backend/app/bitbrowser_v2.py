from __future__ import annotations

import copy
import base64
import hashlib
import ipaddress
import itertools
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import (
    Future,
    InvalidStateError,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlparse

from .cdp_relay import StrictLoopbackCdpRelay
from .errors import (
    BitBrowserAuthRequiredError,
    BitBrowserRateLimitedError,
    DomainError,
    UpstreamUnavailableError,
    ValidationError,
)


_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_AUTH_MARKERS = (
    "login out",
    "logged out",
    "logout",
    "not login",
    "not logged in",
    "login required",
    "please login",
    "please log in",
    "unauthorized",
    "未登录",
    "请登录",
    "重新登录",
    "登录失效",
    "登录过期",
)
_RATE_MARKERS = (
    "too frequent",
    "frequent request",
    "too many request",
    "rate limit",
    "throttl",
    "请求频繁",
    "请求过于频繁",
    "请求太频繁",
    "请求过多",
    "频率过高",
)


def validate_profile_id(profile_id: str) -> str:
    candidate = profile_id.strip()
    if not _PROFILE_ID_RE.fullmatch(candidate):
        raise ValidationError("Invalid BitBrowser profile id")
    return candidate


def _restore_windows_process_to_front(process_id: int) -> dict[str, Any]:
    """Restore one top-level Windows window owned by ``process_id``.

    This is intentionally an explicit operator-handoff primitive, not part of
    normal browser attachment.  Non-Windows source/test environments return a
    structured unsupported result without importing Win32-only symbols.
    """

    if os.name != "nt":
        return {
            "supported": False,
            "restored": False,
            "foregrounded": False,
            "reason": "platform_not_windows",
        }
    if process_id <= 0:
        return {
            "supported": True,
            "restored": False,
            "foregrounded": False,
            "reason": "invalid_process_id",
        }
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        user32.EnumWindows.argtypes = [enum_callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        matches: list[tuple[int, bool]] = []

        @enum_callback_type
        def collect_window(hwnd: int, _lparam: int) -> bool:
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) != process_id:
                return True
            class_name = ctypes.create_unicode_buffer(256)
            if user32.GetClassNameW(hwnd, class_name, len(class_name)) <= 0:
                return True
            # Chromium auxiliary/message windows can share the browser PID. Only
            # its root-owned browser frame is safe to restore.
            if class_name.value != "Chrome_WidgetWin_1":
                return True
            hwnd_value = int(hwnd)
            root_owner = user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER
            if root_owner and int(root_owner) != hwnd_value:
                return True
            matches.append((hwnd_value, bool(user32.IsWindowVisible(hwnd))))
            return True

        user32.EnumWindows(collect_window, 0)
        if not matches:
            return {
                "supported": True,
                "restored": False,
                "foregrounded": False,
                "reason": "top_level_window_not_found",
            }
        # Prefer the visible browser frame if Chromium owns auxiliary hidden
        # top-level windows under the same process.
        hwnd = next((value for value, visible in matches if visible), matches[0][0])
        restored = bool(user32.ShowWindowAsync(hwnd, 9))  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        foregrounded = int(user32.GetForegroundWindow() or 0) == hwnd
        if not foregrounded:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            foregrounded = int(user32.GetForegroundWindow() or 0) == hwnd
        return {
            "supported": True,
            "restored": restored,
            "foregrounded": foregrounded,
            "window_handle": hwnd,
        }
    except Exception as exc:
        return {
            "supported": True,
            "restored": False,
            "foregrounded": False,
            "reason": "win32_foreground_failed",
            "detail": f"{type(exc).__name__}: {exc}"[:240],
        }


class _TransportFailure(Exception):
    def __init__(
        self,
        kind: Literal[
            "offline",
            "auth_required",
            "rate_limited",
            "invalid_response",
            "provider_rejected",
        ],
        message: str,
        *,
        status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class BitBrowserTransport(Protocol):
    def post(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class UrllibBitBrowserTransport:
    def __init__(self) -> None:
        # Local API credentials must never follow an HTTP redirect or traverse an
        # environment-configured proxy.  Both rules are security boundaries, not
        # connection optimizations.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def post(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise _TransportFailure(
                    "auth_required", "BitBrowser 已退出登录", status=exc.code
                ) from exc
            if exc.code == 429:
                retry_after: float | None = None
                try:
                    retry_after = float(exc.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after = None
                raise _TransportFailure(
                    "rate_limited",
                    "BitBrowser 请求过于频繁",
                    status=exc.code,
                    retry_after_seconds=retry_after,
                ) from exc
            raise _TransportFailure(
                "provider_rejected" if 400 <= exc.code < 500 else "offline",
                f"BitBrowser Local API 拒绝请求（HTTP {exc.code}）",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _TransportFailure("offline", "BitBrowser Local API 无法连接") from exc
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _TransportFailure(
                "invalid_response", "BitBrowser Local API 返回了无效数据"
            ) from exc
        if not isinstance(decoded, dict):
            raise _TransportFailure(
                "invalid_response", "BitBrowser Local API 返回格式无法识别"
            )
        return decoded


ConnectionPhase = Literal[
    "probing",
    "ready",
    "offline",
    "auth_required",
    "cooldown",
    "stopping",
    "stopped",
]
WindowPhase = Literal["closed", "opening", "ready", "closing", "error"]


@dataclass(slots=True)
class _ConnectionState:
    phase: ConnectionPhase = "probing"
    endpoint: str | None = None
    generation: int = 0
    consecutive_failures: int = 0
    last_success_epoch: float | None = None
    last_failure_epoch: float | None = None
    next_probe_monotonic: float = 0.0
    detail: str = "正在自动查找 BitBrowser Local API"


@dataclass(slots=True)
class _Command:
    name: str
    payload: dict[str, Any]
    future: Future[Any]
    profile_id: str | None = None
    generation: int | None = None


@dataclass(order=True, slots=True)
class _QueuedCommand:
    priority: int
    sequence: int
    command: _Command = field(compare=False)


@dataclass(slots=True)
class _ConnectionAttempt:
    profile_id: str
    generation: int
    explicit: bool
    resolving: bool = False
    cancelled: bool = False
    manager_open: bool = False
    verified: bool = False
    committed: bool = False


@dataclass(slots=True)
class _ActionAttempt:
    profile_id: str
    generation: int
    expected_endpoint: str
    cancelled: bool = False
    lease_id: str | None = None


LifecycleState = Literal["new", "running", "stopping", "stopped"]


@dataclass(slots=True)
class BitBrowserClientV2:
    """One actor owns every BitBrowser Local API request.

    The previous connection layer mixed page polling, provider cooldowns, inventory
    caches and worker attachment across several locks. V2 has one command queue and
    one provider-calling thread. A close request immediately advances a per-window
    generation, so a late open response cannot resurrect a cancelled window.
    """

    base_url: str = "http://127.0.0.1:54345"
    api_key: str | None = None
    timeout_seconds: float = 5.0
    discovery_timeout_seconds: float = 0.8
    auto_detect: bool = True
    transport: BitBrowserTransport | None = None
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    health_interval_seconds: float = 15.0
    inventory_cache_seconds: float = 60.0
    inventory_page_interval_seconds: float = 0.35
    open_timeout_seconds: float = 20.0
    endpoint_poll_seconds: float = 0.75
    ports_cache_seconds: float = 0.4
    auth_probe_seconds: float = 30.0
    rate_limit_default_seconds: float = 60.0
    login_verification_interval_seconds: float = 5.0
    close_retry_base_seconds: float = 5.0
    command_timeout_seconds: float = 45.0
    shutdown_timeout_seconds: float = 30.0
    port_candidates: tuple[int, ...] = (54345, 54346, 54347, 54348, 54349, 54350, 54344)
    _commands: queue.PriorityQueue[_QueuedCommand] = field(
        init=False, repr=False, default_factory=queue.PriorityQueue
    )
    _command_sequence: Any = field(
        init=False, repr=False, default_factory=itertools.count
    )
    _stop: threading.Event = field(init=False, repr=False, default_factory=threading.Event)
    _urgent_command: threading.Event = field(
        init=False, repr=False, default_factory=threading.Event
    )
    _actor_accepting: threading.Event = field(
        init=False, repr=False, default_factory=threading.Event
    )
    _thread: threading.Thread | None = field(init=False, repr=False, default=None)
    _lifecycle_state: LifecycleState = field(
        init=False, repr=False, default="new"
    )
    _lifecycle_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _admission_lock: threading.RLock = field(
        init=False, repr=False, default_factory=threading.RLock
    )
    _state_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _state: _ConnectionState = field(init=False, repr=False, default_factory=_ConnectionState)
    _inventory: dict[str, Any] | None = field(init=False, repr=False, default=None)
    _inventory_at: float = field(init=False, repr=False, default=0.0)
    _inventory_attempt_at: float | None = field(init=False, repr=False, default=None)
    _inventory_degraded: bool = field(init=False, repr=False, default=False)
    _login_verification_at: float | None = field(
        init=False, repr=False, default=None
    )
    _inventory_version: int = field(init=False, repr=False, default=0)
    _inventory_state_generation: int = field(init=False, repr=False, default=-1)
    _window_phase: dict[str, WindowPhase] = field(init=False, repr=False, default_factory=dict)
    _window_generation: dict[str, int] = field(init=False, repr=False, default_factory=dict)
    _opening_generation: dict[str, int] = field(init=False, repr=False, default_factory=dict)
    _open_may_have_effect: dict[str, int] = field(
        init=False, repr=False, default_factory=dict
    )
    _open_confirmed_generation: dict[str, int] = field(
        init=False, repr=False, default_factory=dict
    )
    _cancelled_open_cohorts: set[tuple[str, int]] = field(
        init=False, repr=False, default_factory=set
    )
    _connection_attempts: dict[str, _ConnectionAttempt] = field(
        init=False, repr=False, default_factory=dict
    )
    _open_waiters: dict[tuple[str, int], set[str]] = field(
        init=False, repr=False, default_factory=dict
    )
    _pending_closes: dict[str, int] = field(init=False, repr=False, default_factory=dict)
    _close_retry_after: dict[str, tuple[int, float]] = field(
        init=False, repr=False, default_factory=dict
    )
    _close_enqueued: set[tuple[str, int]] = field(
        init=False, repr=False, default_factory=set
    )
    _replay_close_scheduled: bool = field(
        init=False, repr=False, default=False
    )
    _deferred_action_closes: set[str] = field(
        init=False, repr=False, default_factory=set
    )
    _action_leases: dict[str, tuple[str, int]] = field(
        init=False, repr=False, default_factory=dict
    )
    _action_attempts: dict[str, _ActionAttempt] = field(
        init=False, repr=False, default_factory=dict
    )
    _active_actions: dict[tuple[str, int], int] = field(
        init=False, repr=False, default_factory=dict
    )
    _action_condition: threading.Condition = field(init=False, repr=False)
    _endpoint_cache: dict[str, dict[str, str | None]] = field(init=False, repr=False, default_factory=dict)
    _ports_snapshot: Any = field(init=False, repr=False, default=None)
    _ports_snapshot_at: float = field(init=False, repr=False, default=0.0)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValidationError("BitBrowser Local API must use a loopback HTTP address")
        self.base_url = self.base_url.rstrip("/")
        self.transport = self.transport or UrllibBitBrowserTransport()
        self._action_condition = threading.Condition(self._state_lock)

    # Lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        thread = self._thread
        if self._lifecycle_state == "stopping" or (
            thread is not None and thread.is_alive() and self._stop.is_set()
        ):
            raise UpstreamUnavailableError(
                "BitBrowser 连接管理器正在停止",
                details={"state": "stopping", "reason": "connection_manager_stopping"},
            )
        if thread is not None and thread.is_alive():
            self._lifecycle_state = "running"
            return
        # A dead actor has fully left its provider call. It is now safe to replace
        # it; the lifecycle lock also makes start + command admission atomic with
        # shutdown.
        self._thread = None
        self._stop.clear()
        self._actor_accepting.set()
        self._lifecycle_state = "running"
        thread = threading.Thread(
            target=self._run,
            name="bitbrowser-connection-v2",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if not thread:
                self._lifecycle_state = "stopped"
                with self._state_lock:
                    self._state.phase = "stopped"
                    self._state.detail = "BitBrowser 连接管理器已停止"
                return
            if self._lifecycle_state != "stopping":
                self._lifecycle_state = "stopping"
                self._stop.set()
                with self._admission_lock:
                    self._actor_accepting.clear()
                with self._state_lock:
                    self._state.phase = "stopping"
                    self._state.detail = "正在停止 BitBrowser 连接管理器"
                self._fail_pending_commands(
                    UpstreamUnavailableError(
                        "BitBrowser 连接管理器正在停止",
                        details={
                            "state": "stopping",
                            "reason": "connection_manager_stopping",
                        },
                    )
                )
                self._enqueue_command(_Command("__stop__", {}, Future[Any]()))
            # Keep the lifecycle lock while joining. The actor never acquires it,
            # so this only linearizes concurrent start/submit/shutdown callers.
            thread.join(timeout=max(0.1, self.shutdown_timeout_seconds))
            if thread.is_alive():
                # Keep the reference and the stopping state. A later shutdown can
                # finish cleanup; start/submit must not create a second actor.
                return
            if self._thread is thread:
                self._thread = None
            self._lifecycle_state = "stopped"
            with self._state_lock:
                self._state.phase = "stopped"
                self._state.detail = "BitBrowser 连接管理器已停止"

    def _run(self) -> None:
        try:
            self._background_probe(force=True)
            while not self._stop.is_set():
                with self._state_lock:
                    next_probe = self._state.next_probe_monotonic
                timeout = max(0.1, min(2.0, next_probe - self.clock()))
                try:
                    queued = self._commands.get(timeout=timeout)
                except queue.Empty:
                    # Failed provider closes remain durable but are retried with a
                    # per-profile backoff.  Checking them from the idle loop makes
                    # progress without a Timer thread or a hot retry loop.
                    self._replay_pending_closes(urgent=False)
                    self._background_probe(force=False)
                    continue
                command = queued.command
                if command.name == "__stop__":
                    break
                self._execute_command(command)
        finally:
            with self._admission_lock:
                self._actor_accepting.clear()
                self._fail_pending_commands(
                    UpstreamUnavailableError(
                        "BitBrowser 连接管理器已停止",
                        details={
                            "state": "stopped",
                            "reason": "connection_manager_stopped",
                        },
                    )
                )
            with self._state_lock:
                self._state.phase = "stopped"
                self._state.detail = "BitBrowser 连接管理器已停止"

    def _execute_command(self, command: _Command) -> None:
        if not command.future.set_running_or_notify_cancel():
            if command.name == "close" and command.profile_id is not None:
                with self._state_lock:
                    self._close_enqueued.discard(
                        (command.profile_id, command.generation or 0)
                    )
            return
        try:
            try:
                result = self._dispatch(command)
            except BaseException as exc:
                if command.name == "close" and command.profile_id is not None:
                    with self._state_lock:
                        self._close_enqueued.discard(
                            (command.profile_id, command.generation or 0)
                        )
                self._try_set_exception(command.future, exc)
            else:
                self._try_set_result(command.future, result)
        finally:
            if command.name == "close" and command.payload.get("replay"):
                with self._state_lock:
                    self._replay_close_scheduled = False
                self._replay_pending_closes(urgent=False)

    @staticmethod
    def _try_set_result(future: Future[Any], value: Any) -> None:
        try:
            future.set_result(value)
        except InvalidStateError:
            pass

    @staticmethod
    def _try_set_exception(future: Future[Any], error: BaseException) -> None:
        try:
            future.set_exception(error)
        except InvalidStateError:
            pass

    @staticmethod
    def _command_priority(command: _Command) -> int:
        if command.name == "__stop__":
            return -100
        if command.name == "close":
            if not command.payload.get("replay"):
                return -50
            return -40 if command.payload.get("urgent") else 30
        if command.name == "reconnect":
            return 0
        if command.name in {"endpoint", "begin_action", "profile_pid"}:
            return 10
        if command.name == "health":
            return 20
        return 40

    def _enqueue_command(self, command: _Command) -> None:
        priority = self._command_priority(command)
        if command.name == "close" and command.profile_id is not None:
            with self._state_lock:
                self._close_enqueued.add(
                    (command.profile_id, command.generation or 0)
                )
        self._commands.put(
            _QueuedCommand(priority, next(self._command_sequence), command)
        )
        if priority <= -50:
            self._urgent_command.set()

    def _fail_pending_commands(self, error: DomainError) -> None:
        while True:
            try:
                queued = self._commands.get_nowait()
            except queue.Empty:
                return
            command = queued.command
            if command.name == "close" and command.profile_id is not None:
                with self._state_lock:
                    self._close_enqueued.discard(
                        (command.profile_id, command.generation or 0)
                    )
                    if command.payload.get("replay"):
                        self._replay_close_scheduled = False
            if command.name == "__stop__" or command.future.done():
                continue
            self._try_set_exception(command.future, error)

    def _submit(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        profile_id: str | None = None,
        generation: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        future: Future[Any] = Future()
        command = _Command(
            name,
            payload or {},
            future,
            profile_id=profile_id,
            generation=generation,
        )
        with self._lifecycle_lock:
            self._start_locked()
            if self._lifecycle_state != "running" or self._stop.is_set():
                raise UpstreamUnavailableError(
                    "BitBrowser 连接管理器正在停止",
                    details={
                        "state": "stopping",
                        "reason": "connection_manager_stopping",
                    },
                )
            with self._admission_lock:
                if not self._actor_accepting.is_set():
                    raise UpstreamUnavailableError(
                        "BitBrowser 连接管理器未接受新请求",
                        details={
                            "state": "stopping",
                            "reason": "connection_manager_not_accepting",
                        },
                    )
                self._enqueue_command(command)
        try:
            return future.result(timeout=timeout or self.command_timeout_seconds)
        except FutureTimeoutError as exc:
            # Close is a convergence command: even if the caller loses its ACK,
            # the queued close must still run. Other queued commands can be safely
            # cancelled before they reach the provider.
            cancelled_before_start = False if name == "close" else future.cancel()
            attempt_id = str((payload or {}).get("attempt_id") or "")
            if attempt_id:
                self.cancel_connection_attempt(attempt_id)
            elif not cancelled_before_start and profile_id is not None and (
                name == "endpoint" and bool((payload or {}).get("open_if_needed"))
            ):
                self._schedule_compensating_close(profile_id, generation or 0)
            raise UpstreamUnavailableError(
                "BitBrowser 连接管理器响应超时",
                details={"state": "offline", "reason": "connection_manager_timeout"},
            ) from exc

    # Public compatibility surface ----------------------------------
    def health(self) -> dict[str, Any]:
        return self._submit("health", timeout=max(self.command_timeout_seconds, self.timeout_seconds + 5))

    def status(self) -> dict[str, Any]:
        return self._status_snapshot(source="memory")

    def list_windows(self, *, page: int = 0, page_size: int = 100, name: str = "") -> dict[str, Any]:
        if page < 0:
            raise ValidationError("page must be zero or greater")
        if not 1 <= page_size <= 100:
            raise ValidationError("page_size must be between 1 and 100")
        listing = self.list_all_windows(name=name)
        start = page * page_size
        return {
            "page": page,
            "page_size": page_size,
            "total": listing["total"],
            "windows": listing["windows"][start : start + page_size],
            "provider_success": listing.get("provider_success", False),
            "stale": listing.get("stale", False),
            "connection": listing.get("connection", {}),
            "inventory_version": listing.get("inventory_version", 0),
        }

    def list_all_windows(self, *, name: str = "", force: bool = False) -> dict[str, Any]:
        return self._submit("inventory", {"name": name, "force": force})

    def confirm_login(self) -> dict[str, Any]:
        return self._submit("reconnect", {"force": True})

    def profile_ports(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        with self._state_lock:
            generation = self._window_generation.get(profile_id, 0)
        endpoint = self._submit(
            "endpoint",
            {"open_if_needed": False},
            profile_id=profile_id,
            generation=generation,
        )
        return {
            "profile_id": profile_id,
            "endpoint_available": bool(endpoint.get("ws") or endpoint.get("http")),
            "provider_success": True,
        }

    def resolve_cdp_endpoint(self, endpoint: str) -> str:
        """Return a browser-scoped loopback WebSocket without following redirects."""
        try:
            safe = self._safe_cdp_endpoint(
                endpoint,
                allowed_schemes={"ws", "wss", "http", "https"},
            )
        except _TransportFailure as exc:
            raise UpstreamUnavailableError(
                exc.message,
                details={"reason": "invalid_cdp_endpoint"},
            ) from exc
        if safe is None:
            raise UpstreamUnavailableError(
                "BitBrowser 调试连接地址无效",
                details={"reason": "invalid_cdp_endpoint"},
            )
        parsed = urlparse(safe)
        expected_websocket = safe if parsed.scheme in {"ws", "wss"} else None
        if expected_websocket is not None and not self._is_browser_scoped_ws_endpoint(
            expected_websocket
        ):
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 不是浏览器级调试端点",
                details={"reason": "cdp_websocket_path_invalid"},
            )
        http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
        version_url = parsed._replace(
            scheme=http_scheme,
            path="/json/version", params="", query="", fragment=""
        ).geturl()
        request = urllib.request.Request(
            version_url,
            method="GET",
            headers={"Accept": "application/json"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.geturl() != version_url:
                    raise UpstreamUnavailableError(
                        "BitBrowser 调试端点发生了不安全的重定向",
                        details={"reason": "cdp_redirect_rejected"},
                    )
                raw = response.read(1024 * 1024)
        except UpstreamUnavailableError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamUnavailableError(
                "无法安全解析 BitBrowser 调试端点",
                details={"reason": "cdp_endpoint_resolution_failed"},
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamUnavailableError(
                "BitBrowser 调试端点返回了无效数据",
                details={"reason": "cdp_endpoint_invalid_response"},
            ) from exc
        websocket = (
            payload.get("webSocketDebuggerUrl")
            if isinstance(payload, dict)
            else None
        )
        try:
            resolved = self._safe_cdp_endpoint(
                websocket,
                allowed_schemes={"ws", "wss"},
            )
        except _TransportFailure as exc:
            raise UpstreamUnavailableError(
                exc.message,
                details={"reason": "cdp_websocket_endpoint_invalid"},
            ) from exc
        if resolved is None:
            raise UpstreamUnavailableError(
                "BitBrowser 未返回安全的本机 WebSocket 调试端点",
                details={"reason": "cdp_websocket_endpoint_invalid"},
            )
        if not self._is_browser_scoped_ws_endpoint(resolved):
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 不是浏览器级调试端点",
                details={"reason": "cdp_websocket_path_invalid"},
            )
        if urlparse(resolved).port != parsed.port:
            raise UpstreamUnavailableError(
                "BitBrowser 调试端点端口发生变化",
                details={"reason": "cdp_websocket_port_changed"},
            )
        if expected_websocket is not None and not self._endpoint_matches(
            resolved, expected_websocket
        ):
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 调试路径发生变化",
                details={"reason": "cdp_websocket_path_changed"},
            )
        self._preflight_cdp_websocket(resolved)
        return resolved

    def create_cdp_relay(self, endpoint: str) -> StrictLoopbackCdpRelay:
        """Create the no-redirect relay used by the actual Playwright socket."""
        return StrictLoopbackCdpRelay(
            endpoint,
            timeout_seconds=max(0.5, self.timeout_seconds),
        )

    @staticmethod
    def _is_browser_scoped_ws_endpoint(value: str) -> bool:
        parsed = urlparse(value)
        return bool(
            parsed.scheme in {"ws", "wss"}
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(
                r"/devtools/browser/[A-Za-z0-9._:-]{1,256}",
                parsed.path,
            )
        )

    def _preflight_cdp_websocket(self, endpoint: str) -> None:
        """Reject HTTP redirects before Playwright opens its own WebSocket."""
        parsed = urlparse(endpoint)
        assert parsed.hostname is not None and parsed.port is not None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = (
            f"[{parsed.hostname}]:{parsed.port}"
            if ":" in parsed.hostname
            else f"{parsed.hostname}:{parsed.port}"
        )
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        connection: socket.socket | ssl.SSLSocket | None = None
        try:
            connection = socket.create_connection(
                (parsed.hostname, parsed.port),
                timeout=self.timeout_seconds,
            )
            peer = str(connection.getpeername()[0])
            if not ipaddress.ip_address(peer).is_loopback:
                raise UpstreamUnavailableError(
                    "BitBrowser WebSocket 实际连接不是本机地址",
                    details={"reason": "cdp_websocket_peer_not_loopback"},
                )
            if parsed.scheme == "wss":
                connection = ssl.create_default_context().wrap_socket(
                    connection,
                    server_hostname=parsed.hostname,
                )
            connection.settimeout(self.timeout_seconds)
            connection.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 64 * 1024:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 安全握手失败",
                details={"reason": "cdp_websocket_preflight_failed"},
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        header_block = bytes(response).split(b"\r\n\r\n", 1)[0]
        lines = header_block.split(b"\r\n")
        if not lines or not re.match(rb"^HTTP/1\.[01] 101(?: |$)", lines[0]):
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 拒绝连接或尝试重定向",
                details={"reason": "cdp_websocket_redirect_rejected"},
            )
        headers: dict[bytes, bytes] = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        )
        if headers.get(b"sec-websocket-accept") != expected_accept:
            raise UpstreamUnavailableError(
                "BitBrowser WebSocket 握手校验失败",
                details={"reason": "cdp_websocket_handshake_invalid"},
            )

    def open_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        endpoint = self.connection_endpoint(profile_id, open_if_needed=True)
        return {
            "profile_id": profile_id,
            "requested": True,
            "provider_success": True,
            "provider_message": "CDP endpoint ready",
            "endpoint_available": bool(endpoint.get("ws") or endpoint.get("http")),
            "window_state": self._window_phase.get(profile_id, "error"),
        }

    def bring_profile_to_front(self, profile_id: str) -> dict[str, Any]:
        """Restore and foreground one BitBrowser profile for manual repair."""

        profile_id = validate_profile_id(profile_id)
        pid_result = self._submit("profile_pid", profile_id=profile_id)
        process_id = pid_result.get("process_id")
        if not isinstance(process_id, int) or process_id <= 0:
            return {
                "profile_id": profile_id,
                "process_id": None,
                "supported": os.name == "nt",
                "restored": False,
                "foregrounded": False,
                "reason": "profile_process_not_found",
            }
        return {
            "profile_id": profile_id,
            "process_id": process_id,
            **_restore_windows_process_to_front(process_id),
        }

    def begin_connection_attempt(self, profile_id: str) -> str:
        """Create a cancellation ticket before an async caller enters to_thread()."""
        profile_id = validate_profile_id(profile_id)
        with self._state_lock:
            generation = self._window_generation.get(profile_id, 0)
            attempt_id = uuid.uuid4().hex
            self._connection_attempts[attempt_id] = _ConnectionAttempt(
                profile_id=profile_id,
                generation=generation,
                explicit=True,
            )
        return attempt_id

    def cancel_connection_attempt(self, attempt_id: str) -> dict[str, Any]:
        """Cancel an attach attempt without closing a window opened by the user.

        A close is scheduled only when this manager dispatched ``/browser/open``
        for the attempt's generation, no sibling attempt still wants that open,
        and no attempt has committed after a successful Playwright attachment.
        """
        close_target: tuple[str, int] | None = None
        compensation_planned = False
        with self._state_lock:
            attempt = self._connection_attempts.get(attempt_id)
            if attempt is None:
                return {"cancelled": False, "close_scheduled": False}
            if attempt.committed:
                self._connection_attempts.pop(attempt_id, None)
                return {"cancelled": False, "close_scheduled": False}
            attempt.cancelled = True
            active_waiters = [
                waiter_id
                for waiter_id, waiter in self._connection_attempts.items()
                if waiter_id != attempt_id
                and waiter.profile_id == attempt.profile_id
                and waiter.generation == attempt.generation
                and waiter.resolving
                and not waiter.cancelled
                and not waiter.committed
            ]
            if (
                not active_waiters
                and self._open_may_have_effect.get(attempt.profile_id)
                == attempt.generation
            ):
                # The last-waiter decision and generation advance are one atomic
                # state transition with commit().  The obligation belongs to the
                # whole open cohort, not only the waiter that happened to dispatch
                # /browser/open.  This also covers a queued sibling being the last
                # cancellation before the actor registers it as manager-open.
                compensation_planned = True
                if (
                    self._open_confirmed_generation.get(attempt.profile_id)
                    == attempt.generation
                ):
                    close_target = self._prepare_compensating_close_locked(
                        attempt.profile_id,
                        attempt.generation,
                        require_open_obligation=True,
                    )
                else:
                    # /browser/open is still in flight. Advancing the generation
                    # prevents a late result from being accepted, but no close is
                    # queued until the outcome is known. A definite provider
                    # rejection can mean the window was already user-opened.
                    if (
                        self._window_generation.get(attempt.profile_id, 0)
                        == attempt.generation
                    ):
                        self._window_generation[attempt.profile_id] = (
                            attempt.generation + 1
                        )
                    self._cancelled_open_cohorts.add(
                        (attempt.profile_id, attempt.generation)
                    )
                    self._window_phase[attempt.profile_id] = "closing"
            self._connection_attempts.pop(attempt_id, None)
            cohort = (attempt.profile_id, attempt.generation)
            cohort_waiters = self._open_waiters.get(cohort)
            if cohort_waiters is not None:
                cohort_waiters.discard(attempt_id)
                if not cohort_waiters:
                    self._open_waiters.pop(cohort, None)
        if close_target is not None:
            self._enqueue_close_if_running(*close_target)
        return {
            "cancelled": True,
            "close_scheduled": compensation_planned,
        }

    def _new_internal_attempt(self, profile_id: str) -> str:
        with self._state_lock:
            attempt_id = uuid.uuid4().hex
            self._connection_attempts[attempt_id] = _ConnectionAttempt(
                profile_id=profile_id,
                generation=self._window_generation.get(profile_id, 0),
                explicit=False,
            )
        return attempt_id

    def _connection_attempt(
        self, attempt_id: str, profile_id: str
    ) -> _ConnectionAttempt:
        with self._state_lock:
            attempt = self._connection_attempts.get(attempt_id)
            if attempt is None or attempt.profile_id != profile_id:
                raise UpstreamUnavailableError(
                    "BitBrowser 连接尝试票据无效",
                    details={
                        "profile_id": profile_id,
                        "reason": "connection_attempt_invalid",
                    },
                )
            if attempt.cancelled:
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口连接已取消",
                    details={
                        "profile_id": profile_id,
                        "reason": "open_cancelled",
                    },
                )
            return attempt

    def _commit_connection_attempt(self, attempt_id: str) -> tuple[str, int]:
        with self._state_lock:
            attempt = self._connection_attempts.get(attempt_id)
            if attempt is None:
                raise UpstreamUnavailableError(
                    "BitBrowser 连接尝试票据已经失效",
                    details={"reason": "connection_attempt_invalid"},
                )
            if attempt.cancelled:
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口连接已取消",
                    details={
                        "profile_id": attempt.profile_id,
                        "reason": "open_cancelled",
                    },
                )
            if (
                self._window_generation.get(attempt.profile_id, 0)
                != attempt.generation
                or attempt.profile_id in self._pending_closes
            ):
                attempt.cancelled = True
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口连接代次已经变化",
                    details={
                        "profile_id": attempt.profile_id,
                        "reason": "cdp_connection_generation_changed",
                        "pause_required": True,
                    },
                )
            if attempt.explicit and not attempt.verified:
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口连接尚未完成归属验证",
                    details={
                        "profile_id": attempt.profile_id,
                        "reason": "connection_attempt_not_verified",
                    },
                )
            attempt.committed = True
            profile_id = attempt.profile_id
            generation = attempt.generation
            if (
                attempt.manager_open
                and self._open_may_have_effect.get(attempt.profile_id)
                == attempt.generation
            ):
                self._open_may_have_effect.pop(attempt.profile_id, None)
                self._open_confirmed_generation.pop(attempt.profile_id, None)
                self._cancelled_open_cohorts.discard(
                    (attempt.profile_id, attempt.generation)
                )
                self._opening_generation.pop(attempt.profile_id, None)
                self._open_waiters.pop(
                    (attempt.profile_id, attempt.generation), None
                )
            self._connection_attempts.pop(attempt_id, None)
            return profile_id, generation

    def _discard_connection_attempt(self, attempt_id: str) -> None:
        with self._state_lock:
            attempt = self._connection_attempts.pop(attempt_id, None)
            if attempt is None:
                return
            cohort = (attempt.profile_id, attempt.generation)
            waiters = self._open_waiters.get(cohort)
            if waiters is not None:
                waiters.discard(attempt_id)
                if not waiters:
                    self._open_waiters.pop(cohort, None)

    def close_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        # Advance immediately, before the close command waits in the actor queue.
        # An in-flight open checks this generation after every provider response.
        with self._action_condition:
            generation = self._window_generation.get(profile_id, 0) + 1
            self._window_generation[profile_id] = generation
            self._pending_closes[profile_id] = generation
            self._close_retry_after.pop(profile_id, None)
            self._window_phase[profile_id] = "closing"
            deadline = self.clock() + max(0.1, self.command_timeout_seconds)
            while any(
                active_profile == profile_id and count > 0
                for (active_profile, _), count in self._active_actions.items()
            ):
                remaining = deadline - self.clock()
                if remaining <= 0:
                    self._deferred_action_closes.add(profile_id)
                    raise UpstreamUnavailableError(
                        "BitBrowser 窗口正在等待已开始的操作安全结束",
                        details={
                            "profile_id": profile_id,
                            "reason": "action_in_progress",
                            "close_pending": True,
                        },
                    )
                self._action_condition.wait(timeout=remaining)
        return self._submit(
            "close", profile_id=profile_id, generation=generation
        )

    def connection_endpoint(
        self,
        profile_id: str,
        *,
        open_if_needed: bool = False,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        internal_attempt = attempt_id is None
        attempt_id = attempt_id or self._new_internal_attempt(profile_id)
        attempt = self._connection_attempt(attempt_id, profile_id)
        with self._state_lock:
            attempt.resolving = True
        generation = attempt.generation
        try:
            result = self._submit(
                "endpoint",
                {
                    "open_if_needed": open_if_needed,
                    "attempt_id": attempt_id,
                },
                profile_id=profile_id,
                generation=generation,
            )
            endpoint = self._public_endpoint(result)
            if self._endpoint_available(endpoint) or not open_if_needed:
                if internal_attempt:
                    self._commit_connection_attempt(attempt_id)
                return {
                    **endpoint,
                    "generation": generation,
                    "attempt_id": attempt_id if not internal_attempt else None,
                }

            deadline = self.clock() + max(0.1, self.open_timeout_seconds)
            delays = (0.5, 1.0, 2.0, 4.0)
            poll_attempt = 0
            while self.clock() < deadline:
                self._connection_attempt(attempt_id, profile_id)
                self._raise_if_generation_stale(profile_id, generation)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                time.sleep(
                    min(
                        delays[min(poll_attempt, len(delays) - 1)],
                        remaining,
                    )
                )
                poll_attempt += 1
                result = self._submit(
                    "endpoint",
                    {"open_if_needed": False, "attempt_id": attempt_id},
                    profile_id=profile_id,
                    generation=generation,
                )
                endpoint = self._public_endpoint(result)
                if self._endpoint_available(endpoint):
                    if internal_attempt:
                        self._commit_connection_attempt(attempt_id)
                    return {
                        **endpoint,
                        "generation": generation,
                        "attempt_id": attempt_id if not internal_attempt else None,
                    }

            with self._state_lock:
                if self._window_phase.get(profile_id) == "opening":
                    self._window_phase[profile_id] = "error"
            raise UpstreamUnavailableError(
                "BitBrowser 已接受打开指令，但调试连接尚未就绪",
                details={
                    "profile_id": profile_id,
                    "state": "opening",
                    "reason": "cdp_endpoint_not_ready",
                    "retryable": True,
                },
            )
        except BaseException:
            self.cancel_connection_attempt(attempt_id)
            self._discard_connection_attempt(attempt_id)
            raise

    def verify_connection_endpoint(
        self,
        profile_id: str,
        expected_endpoint: str,
        expected_generation: int | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-check profile-to-CDP ownership after Playwright attaches."""
        profile_id = validate_profile_id(profile_id)
        expected = self._safe_cdp_endpoint(
            expected_endpoint,
            allowed_schemes={"ws", "wss", "http", "https"},
        )
        assert expected is not None
        with self._state_lock:
            current_generation = self._window_generation.get(profile_id, 0)
        generation = (
            current_generation
            if expected_generation is None
            else expected_generation
        )
        if current_generation != generation:
            raise UpstreamUnavailableError(
                "BitBrowser 窗口连接代次已经变化",
                details={
                    "profile_id": profile_id,
                    "reason": "cdp_connection_generation_changed",
                    "pause_required": True,
                },
            )
        current = self._submit(
            "endpoint",
            {
                "open_if_needed": False,
                "force_ports": True,
                "attempt_id": attempt_id,
            },
            profile_id=profile_id,
            generation=generation,
        )
        current_endpoint = self._public_endpoint(current)
        actual = current_endpoint.get("ws") or current_endpoint.get("http")
        if not actual or not self._endpoint_matches(actual, expected):
            raise UpstreamUnavailableError(
                "BitBrowser 调试端口已不再属于所选窗口",
                details={
                    "profile_id": profile_id,
                    "reason": "cdp_profile_mapping_changed",
                    "pause_required": True,
                },
            )
        if attempt_id:
            with self._state_lock:
                attempt = self._connection_attempts.get(attempt_id)
                if (
                    attempt is None
                    or attempt.cancelled
                    or attempt.profile_id != profile_id
                    or attempt.generation != generation
                    or self._window_generation.get(profile_id, 0) != generation
                ):
                    raise UpstreamUnavailableError(
                        "BitBrowser 窗口连接已取消",
                        details={
                            "profile_id": profile_id,
                            "reason": "open_cancelled",
                        },
                    )
                attempt.verified = True
        return {
            "profile_id": profile_id,
            "generation": generation,
            "verified": True,
        }

    def commit_connection_attempt(self, attempt_id: str) -> dict[str, Any]:
        # Read, validate, commit and remove under one state-lock acquisition.
        # Otherwise cancel could remove/schedule close between a public pre-read
        # and the internal commit while this method still reported success.
        profile_id, generation = self._commit_connection_attempt(attempt_id)
        return {
            "profile_id": profile_id,
            "generation": generation,
            "committed": True,
        }

    def begin_action(
        self,
        profile_id: str,
        expected_endpoint: str,
        expected_generation: int | None,
    ) -> str:
        attempt_id = self.begin_action_attempt(
            profile_id,
            expected_endpoint,
            expected_generation,
        )
        try:
            self.resolve_action_attempt(attempt_id)
            return self.adopt_action_attempt(attempt_id)
        except BaseException:
            self.cancel_action_attempt(attempt_id)
            raise

    def begin_action_attempt(
        self,
        profile_id: str,
        expected_endpoint: str,
        expected_generation: int | None,
    ) -> str:
        profile_id = validate_profile_id(profile_id)
        endpoint = self._safe_cdp_endpoint(
            expected_endpoint,
            allowed_schemes={"ws", "wss", "http", "https"},
        )
        if endpoint is None:
            raise UpstreamUnavailableError(
                "BitBrowser 调试连接地址无效",
                details={"profile_id": profile_id, "reason": "invalid_cdp_endpoint"},
            )
        with self._state_lock:
            generation = (
                self._window_generation.get(profile_id, 0)
                if expected_generation is None
                else expected_generation
            )
            attempt_id = uuid.uuid4().hex
            self._action_attempts[attempt_id] = _ActionAttempt(
                profile_id=profile_id,
                generation=generation,
                expected_endpoint=endpoint,
            )
            return attempt_id

    def resolve_action_attempt(self, attempt_id: str) -> str:
        with self._state_lock:
            attempt = self._action_attempts.get(attempt_id)
            if attempt is None or attempt.cancelled:
                raise UpstreamUnavailableError(
                    "BitBrowser 操作票据已取消",
                    details={"reason": "action_attempt_cancelled"},
                )
            profile_id = attempt.profile_id
            generation = attempt.generation
            expected_endpoint = attempt.expected_endpoint
        return self._submit(
            "begin_action",
            {
                "expected_endpoint": expected_endpoint,
                "action_attempt_id": attempt_id,
            },
            profile_id=profile_id,
            generation=generation,
        )

    def adopt_action_attempt(self, attempt_id: str) -> str:
        with self._action_condition:
            attempt = self._action_attempts.get(attempt_id)
            if attempt is None or attempt.cancelled or attempt.lease_id is None:
                raise UpstreamUnavailableError(
                    "BitBrowser 操作票据未能建立租约",
                    details={"reason": "action_attempt_not_ready"},
                )
            lease_id = attempt.lease_id
            self._action_attempts.pop(attempt_id, None)
            return lease_id

    def cancel_action_attempt(self, attempt_id: str) -> dict[str, Any]:
        deferred: tuple[str, int] | None = None
        lease_id: str | None = None
        with self._action_condition:
            attempt = self._action_attempts.get(attempt_id)
            if attempt is None:
                return {"cancelled": False, "lease_released": False}
            attempt.cancelled = True
            lease_id = attempt.lease_id
            # Remove the ticket even when cancellation wins before the actor has
            # created a lease.  A concurrently queued begin_action command will
            # then fail closed on the missing ticket instead of retaining one
            # cancelled entry for every early-cancelled task.
            self._action_attempts.pop(attempt_id, None)
            if lease_id is not None:
                deferred = self._release_action_lease_locked(lease_id)
        if deferred is not None:
            self._enqueue_close_if_running(*deferred)
        return {"cancelled": True, "lease_released": lease_id is not None}

    def end_action(self, lease_id: str) -> None:
        with self._action_condition:
            deferred = self._release_action_lease_locked(lease_id)
        if deferred is not None:
            self._enqueue_close_if_running(*deferred)

    def _release_action_lease_locked(
        self, lease_id: str
    ) -> tuple[str, int] | None:
        lease = self._action_leases.pop(lease_id, None)
        if lease is None:
            return None
        count = self._active_actions.get(lease, 0)
        if count <= 1:
            self._active_actions.pop(lease, None)
        else:
            self._active_actions[lease] = count - 1
        profile_id, _ = lease
        if any(
            active_profile == profile_id and active_count > 0
            for (active_profile, _), active_count in self._active_actions.items()
        ):
            return None
        self._action_condition.notify_all()
        if profile_id not in self._deferred_action_closes:
            return None
        self._deferred_action_closes.discard(profile_id)
        pending_generation = self._pending_closes.get(profile_id)
        return (
            (profile_id, pending_generation)
            if pending_generation is not None
            else None
        )

    # Actor commands --------------------------------------------------
    def _dispatch(self, command: _Command) -> Any:
        if command.name == "health":
            self._background_probe(force=False)
            return self._status_snapshot(source="actor")
        if command.name == "inventory":
            return self._inventory_command(
                name=str(command.payload.get("name") or ""),
                force=bool(command.payload.get("force")),
            )
        if command.name == "reconnect":
            phase = self._phase()
            if phase == "ready":
                # A normal Refresh action must obey the same hard inventory floor.
                return self._inventory_command(name="", force=False)
            now = self.clock()
            with self._state_lock:
                retry_after = self._state.next_probe_monotonic
            if phase == "cooldown" and now < retry_after:
                return self._cached_listing(
                    name="", source="cooldown", stale=True
                )
            if (
                self._login_verification_at is not None
                and now - self._login_verification_at
                < max(0.1, self.login_verification_interval_seconds)
            ):
                return self._cached_listing(
                    name="", source="reconnect_throttled", stale=True
                )
            self._login_verification_at = now
            self._background_probe(force=True, verify_login=True)
            if self._phase() == "ready":
                return self._inventory_command(name="", force=True)
            return self._cached_listing(name="", source="reconnect", stale=True)
        if command.name == "close" and command.profile_id is not None:
            return self._close_command(command.profile_id, command.generation or 0)
        if command.name == "begin_action" and command.profile_id is not None:
            return self._begin_action_command(
                command.profile_id,
                command.generation or 0,
                str(command.payload.get("expected_endpoint") or ""),
                (
                    str(command.payload.get("action_attempt_id"))
                    if command.payload.get("action_attempt_id")
                    else None
                ),
            )
        if command.name == "profile_pid" and command.profile_id is not None:
            return self._profile_pid_command(command.profile_id)
        if command.name == "endpoint" and command.profile_id is not None:
            return self._endpoint_command(
                command.profile_id,
                command.generation or 0,
                open_if_needed=bool(command.payload.get("open_if_needed")),
                force_ports=bool(command.payload.get("force_ports")),
                attempt_id=(
                    str(command.payload.get("attempt_id"))
                    if command.payload.get("attempt_id")
                    else None
                ),
            )
        raise RuntimeError(f"Unknown BitBrowser command: {command.name}")

    def _profile_pid_command(self, profile_id: str) -> dict[str, Any]:
        """Resolve one provider profile PID on the serialized API actor."""

        try:
            payload = self._call(self.base_url, "/browser/pids/all", {})
        except _TransportFailure as exc:
            self._record_failure(exc)
            self._raise_domain_failure(exc, path="/browser/pids/all")
        return {
            "profile_id": profile_id,
            "process_id": self._extract_profile_pids(payload).get(profile_id),
        }

    def _phase(self) -> ConnectionPhase:
        with self._state_lock:
            return self._state.phase

    def _background_probe(self, *, force: bool, verify_login: bool = False) -> None:
        now = self.clock()
        with self._state_lock:
            phase = self._state.phase
            # A signed-out desktop session needs a human action.  Background
            # probes would only create request noise and can trip provider rate
            # limits.  ``confirm_login`` is the sole force path out of this state.
            if phase == "auth_required" and not force:
                return
            if not force and now < self._state.next_probe_monotonic:
                return
        if phase == "ready" and not force:
            try:
                self._call(self.base_url, "/health", {})
            except _TransportFailure as exc:
                if exc.kind == "provider_rejected":
                    exc = _TransportFailure(
                        "invalid_response", "BitBrowser 健康检查被拒绝"
                    )
                self._record_failure(exc)
                return
            self._record_ready(self.base_url, detail="BitBrowser Local API 连接正常")
            self._replay_pending_closes()
            return
        self._discover(force=force, verify_login=verify_login or phase in {"auth_required", "cooldown"})

    def _discover(self, *, force: bool, verify_login: bool) -> bool:
        del force
        with self._state_lock:
            self._state.phase = "probing"
            self._state.detail = "正在自动查找 BitBrowser Local API"
        last_failure: _TransportFailure | None = None
        gated_failure: tuple[str, _TransportFailure] | None = None
        for endpoint in self._candidate_urls():
            verified_inventory_page: dict[str, Any] | None = None
            try:
                health = self._call(
                    endpoint,
                    "/health",
                    {},
                    timeout_seconds=min(
                        self.timeout_seconds, self.discovery_timeout_seconds
                    ),
                )
                if not self._has_health_signature(health):
                    raise _TransportFailure(
                        "invalid_response",
                        "端口可达，但不是可识别的 BitBrowser Local API",
                    )
                if verify_login:
                    # /health can stay green while BitBrowser's authenticated
                    # business APIs still return Login out.  Start one complete
                    # inventory round here and reuse its first page after the
                    # endpoint is accepted.  This avoids issuing a pageSize=1
                    # login probe immediately followed by another /browser/list
                    # call, which can trip BitBrowser's provider rate limit.
                    self._inventory_attempt_at = self.clock()
                    verified_inventory_page = self._call(
                        endpoint,
                        "/browser/list",
                        {"page": 0, "pageSize": 100, "sort": "asc"},
                    )
                    if not self._has_profile_list(verified_inventory_page):
                        raise _TransportFailure(
                            "invalid_response",
                            "BitBrowser 登录验证返回格式无法识别",
                        )
            except _TransportFailure as exc:
                last_failure = exc
                if exc.kind in {"auth_required", "rate_limited"}:
                    # A random local service can also answer 401/429. Keep scanning
                    # the remaining loopback candidates; if none is healthy, report
                    # the first gated endpoint instead of hiding the login/cooldown.
                    if gated_failure is None:
                        gated_failure = (endpoint, exc)
                continue
            self._record_ready(endpoint, detail="已自动连接 BitBrowser Local API")
            if verified_inventory_page is not None:
                try:
                    listing = self._read_inventory(
                        first_page=verified_inventory_page,
                        attempt_already_started=True,
                    )
                except _TransportFailure as exc:
                    # The protected first page proves this is the selected
                    # BitBrowser endpoint.  A later pagination/PID failure is a
                    # connection failure for that endpoint, not a reason to scan
                    # unrelated loopback services and duplicate requests.
                    self._record_failure(exc)
                    return False
                self._store_inventory(listing)
                if self._phase() != "ready":
                    return False
            self._replay_pending_closes()
            return True
        if gated_failure is not None:
            self.base_url, failure = gated_failure
            self._record_failure(failure)
            return False
        terminal_failure = last_failure or _TransportFailure(
            "offline", "未找到 BitBrowser Local API"
        )
        if terminal_failure.kind == "provider_rejected":
            terminal_failure = _TransportFailure(
                "invalid_response", "候选端口不是可用的 BitBrowser Local API"
            )
        self._record_failure(terminal_failure)
        return False

    def _inventory_command(self, *, name: str, force: bool) -> dict[str, Any]:
        now = self.clock()
        if self._phase() != "ready":
            # ``force`` refreshes a connected inventory only. It must not bypass
            # connection backoff, cooldown or the explicit-login gate.
            self._background_probe(force=False)
        if self._phase() != "ready":
            return self._cached_listing(name=name, source="stale", stale=True)
        cache_seconds = max(0.0, self.inventory_cache_seconds)
        if (
            cache_seconds > 0
            and self._inventory_attempt_at is not None
            and now - self._inventory_attempt_at < cache_seconds
        ):
            return self._cached_listing(
                name=name,
                source="cache",
                stale=self._inventory is None or self._inventory_degraded,
            )
        try:
            listing = self._read_inventory()
        except _TransportFailure as exc:
            if exc.kind == "provider_rejected":
                exc = _TransportFailure(
                    "invalid_response", "BitBrowser 窗口列表请求被拒绝"
                )
            self._record_failure(exc)
            self._inventory_degraded = True
            return self._cached_listing(name=name, source="stale", stale=True)
        connection_degraded = self._store_inventory(listing)
        return self._cached_listing(
            name=name,
            source="partial" if connection_degraded else "fresh",
            stale=connection_degraded,
        )

    def _store_inventory(self, listing: dict[str, Any]) -> bool:
        connection_degraded = bool(listing.pop("_connection_degraded", False))
        self._inventory = listing
        self._inventory_at = self.clock()
        self._inventory_degraded = connection_degraded
        self._inventory_version += 1
        if not connection_degraded:
            self._record_ready(self.base_url, detail="BitBrowser 窗口列表已同步")
        self._inventory_state_generation = self._state.generation
        return connection_degraded

    def _read_inventory(
        self,
        *,
        first_page: dict[str, Any] | None = None,
        attempt_already_started: bool = False,
    ) -> dict[str, Any]:
        # This timestamp is independent of connection generation and is written
        # before the first list request.  Failures therefore cannot turn the
        # required 60-second inventory floor into a rapid reconnect loop.
        if not attempt_already_started:
            self._inventory_attempt_at = self.clock()
        page = 0
        page_size = 100
        windows: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            if page == 0 and first_page is not None:
                payload = first_page
            else:
                payload = self._call(
                    self.base_url,
                    "/browser/list",
                    {"page": page, "pageSize": page_size, "sort": "asc"},
                )
            if not self._has_profile_list(payload):
                raise _TransportFailure("invalid_response", "无法识别 BitBrowser 窗口列表")
            profiles, total = self._extract_profile_list(payload)
            added = 0
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                profile_id = str(self._first(profile, "id", "browserId", "profileId", "browser_id") or "")
                if (
                    not _PROFILE_ID_RE.fullmatch(profile_id)
                    or profile_id in seen
                ):
                    continue
                seen.add(profile_id)
                windows.append(profile)
                added += 1
            self._service_urgent_commands()
            self._require_inventory_phase_ready()
            if (
                len(profiles) < page_size
                or added == 0
                or (total is not None and total > 0 and len(seen) >= total)
            ):
                break
            page += 1
            if page >= 1_000:
                raise _TransportFailure(
                    "invalid_response",
                    "BitBrowser 窗口列表分页超过安全上限",
                )
            self._interruptible_inventory_wait(
                max(0.0, self.inventory_page_interval_seconds)
            )
        open_ids: set[str] = set()
        connection_degraded = False
        self._service_urgent_commands()
        self._require_inventory_phase_ready()
        try:
            pids = self._call(self.base_url, "/browser/pids/all", {})
            open_ids = self._extract_open_ids(pids)
        except _TransportFailure as exc:
            if exc.kind in {"auth_required", "rate_limited"}:
                raise
            if exc.kind in {"offline", "invalid_response"}:
                self._record_failure(exc)
                connection_degraded = True
            # PIDs are supplemental. A successful window inventory remains useful;
            # per-row open flags and the V2 window phase fill the gap.
        normalized = [
            self._normalize_profile(profile, open_ids, provider_order=index)
            for index, profile in enumerate(windows)
        ]
        return {
            "total": len(normalized),
            "windows": [item for item in normalized if item["id"]],
            "provider_success": True,
            "_connection_degraded": connection_degraded,
        }

    def _interruptible_inventory_wait(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self._urgent_command.wait(timeout=min(0.05, remaining))
            self._service_urgent_commands()
            self._require_inventory_phase_ready()

    def _require_inventory_phase_ready(self) -> None:
        phase = self._phase()
        if phase == "ready":
            return
        if phase == "auth_required":
            raise _TransportFailure(
                "auth_required", "BitBrowser 已退出登录"
            )
        if phase == "cooldown":
            raise _TransportFailure(
                "rate_limited", "BitBrowser 请求正在冷却"
            )
        raise _TransportFailure(
            "offline", "BitBrowser Local API 连接已中断"
        )

    def _endpoint_command(
        self,
        profile_id: str,
        generation: int,
        *,
        open_if_needed: bool,
        force_ports: bool = False,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if attempt_id is not None:
            self._connection_attempt(attempt_id, profile_id)
        self._raise_if_generation_stale(profile_id, generation)
        with self._state_lock:
            if open_if_needed and profile_id in self._pending_closes:
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口正在关闭",
                    details={"profile_id": profile_id, "reason": "close_pending"},
                )
            if self._open_may_have_effect.get(profile_id) == generation:
                self._mark_attempt_manager_open_locked(
                    attempt_id, profile_id, generation
                )
        self._require_ready()
        endpoint = self._ports_endpoint(profile_id, force=force_ports)
        if attempt_id is not None:
            self._connection_attempt(attempt_id, profile_id)
        self._raise_if_generation_stale(profile_id, generation)
        if self._endpoint_available(endpoint):
            self._accept_endpoint(profile_id, generation, endpoint)
            return {
                **endpoint,
                "_pending": False,
                "_opened_by_us": self._open_may_have_effect.get(profile_id)
                == generation,
            }
        if not open_if_needed:
            if self._window_phase.get(profile_id) not in {"opening", "closing", "error"}:
                self._window_phase[profile_id] = "closed"
            return {**endpoint, "_pending": False, "_opened_by_us": False}
        with self._state_lock:
            if self._opening_generation.get(profile_id) == generation:
                self._mark_attempt_manager_open_locked(
                    attempt_id, profile_id, generation
                )
                return {**endpoint, "_pending": True, "_opened_by_us": True}
        if not self._generation_matches(profile_id, generation):
            raise UpstreamUnavailableError(
                "BitBrowser 窗口打开已取消",
                details={"profile_id": profile_id, "reason": "open_cancelled"},
            )
        with self._state_lock:
            if attempt_id is not None:
                self._connection_attempt(attempt_id, profile_id)
            self._opening_generation[profile_id] = generation
            # This flag is set before the transport call. A socket timeout can
            # mean the provider executed the request but its response was lost.
            self._open_may_have_effect[profile_id] = generation
            self._mark_attempt_manager_open_locked(
                attempt_id, profile_id, generation
            )
            self._window_phase[profile_id] = "opening"
        try:
            payload = self._call(
                self.base_url,
                "/browser/open",
                {
                    "id": profile_id,
                    "args": [],
                    "queue": True,
                    "ignoreDefaultUrls": True,
                    "newPageUrl": "https://www.instagram.com/",
                },
            )
        except _TransportFailure as exc:
            self._window_phase[profile_id] = "error"
            self._record_failure(exc)
            if exc.kind == "provider_rejected":
                # A definite provider rejection means no open side effect.
                self._clear_open_obligation(profile_id, generation)
            else:
                self._schedule_compensating_close(profile_id, generation)
            self._raise_domain_failure(exc, path="/browser/open")
        with self._state_lock:
            if self._open_may_have_effect.get(profile_id) == generation:
                self._open_confirmed_generation[profile_id] = generation
        if not self._generation_matches(profile_id, generation):
            self._schedule_compensating_close(profile_id, generation)
            raise UpstreamUnavailableError(
                "BitBrowser 窗口打开已取消",
                details={"profile_id": profile_id, "reason": "open_cancelled"},
            )
        try:
            # Validate any immediate endpoint, but do not attach to it yet.  Ports
            # is the authoritative profile mapping and may lag a queued open.
            self._connection_endpoint_from_data(
                self._provider_data(payload), profile_id, allow_unscoped=True
            )
        except _TransportFailure as exc:
            self._window_phase[profile_id] = "error"
            self._record_failure(exc)
            self._schedule_compensating_close(profile_id, generation)
            self._raise_domain_failure(exc, path="/browser/open")
        self._invalidate_ports_snapshot()
        try:
            endpoint = self._ports_endpoint(profile_id, force=True)
        except BaseException:
            self._schedule_compensating_close(profile_id, generation)
            raise
        if attempt_id is not None:
            self._connection_attempt(attempt_id, profile_id)
        self._raise_if_generation_stale_after_open(profile_id, generation)
        if not self._endpoint_available(endpoint):
            return {**endpoint, "_pending": True, "_opened_by_us": True}
        try:
            self._accept_endpoint(profile_id, generation, endpoint)
        except UpstreamUnavailableError:
            self._schedule_compensating_close(profile_id, generation)
            raise
        return {**endpoint, "_pending": False, "_opened_by_us": True}

    def _mark_attempt_manager_open_locked(
        self,
        attempt_id: str | None,
        profile_id: str,
        generation: int,
    ) -> None:
        if attempt_id is None:
            return
        attempt = self._connection_attempts.get(attempt_id)
        if attempt is None or attempt.cancelled:
            raise UpstreamUnavailableError(
                "BitBrowser 窗口连接已取消",
                details={"profile_id": profile_id, "reason": "open_cancelled"},
            )
        attempt.manager_open = True
        self._open_waiters.setdefault((profile_id, generation), set()).add(
            attempt_id
        )

    def _clear_open_obligation(self, profile_id: str, generation: int) -> None:
        with self._state_lock:
            if self._open_may_have_effect.get(profile_id) == generation:
                self._open_may_have_effect.pop(profile_id, None)
            if self._open_confirmed_generation.get(profile_id) == generation:
                self._open_confirmed_generation.pop(profile_id, None)
            if self._opening_generation.get(profile_id) == generation:
                self._opening_generation.pop(profile_id, None)
            self._cancelled_open_cohorts.discard((profile_id, generation))
            self._open_waiters.pop((profile_id, generation), None)

    def _schedule_compensating_close(
        self, profile_id: str, generation: int
    ) -> None:
        with self._state_lock:
            close_target = self._prepare_compensating_close_locked(
                profile_id,
                generation,
                require_open_obligation=False,
            )
        if close_target is not None:
            self._enqueue_close_if_running(*close_target)

    def _prepare_compensating_close_locked(
        self,
        profile_id: str,
        generation: int,
        *,
        require_open_obligation: bool,
    ) -> tuple[str, int] | None:
        if (
            require_open_obligation
            and self._open_may_have_effect.get(profile_id) != generation
        ):
            return None
        self._cancelled_open_cohorts.discard((profile_id, generation))
        if self._open_confirmed_generation.get(profile_id) == generation:
            self._open_confirmed_generation.pop(profile_id, None)
        current_generation = self._window_generation.get(profile_id, 0)
        if current_generation == generation:
            current_generation += 1
            self._window_generation[profile_id] = current_generation
        pending_generation = self._pending_closes.get(profile_id)
        if pending_generation is None or pending_generation < current_generation:
            self._pending_closes[profile_id] = current_generation
            self._close_retry_after.pop(profile_id, None)
        self._window_phase[profile_id] = "closing"
        return (profile_id, self._pending_closes[profile_id])

    def _enqueue_close_if_running(
        self,
        profile_id: str,
        generation: int,
        *,
        replay: bool = False,
        urgent: bool = False,
    ) -> None:
        with self._state_lock:
            key = (profile_id, generation)
            if key in self._close_enqueued or self._stop.is_set():
                return
            if replay:
                if self._replay_close_scheduled:
                    return
                self._replay_close_scheduled = True
        self._enqueue_command(
            _Command(
                "close",
                {"replay": replay, "urgent": urgent},
                Future[Any](),
                profile_id=profile_id,
                generation=generation,
            )
        )

    def _service_urgent_commands(self) -> None:
        # Clear before draining. Any close submitted while the queue is being
        # inspected sets the event again, so an ordinary command put back into the
        # queue can never erase the wake-up for that newly arrived close.
        while self._urgent_command.is_set():
            self._urgent_command.clear()
            while True:
                try:
                    queued = self._commands.get_nowait()
                except queue.Empty:
                    break
                if queued.priority > -50:
                    self._commands.put(queued)
                    break
                if queued.command.name == "__stop__":
                    self._commands.put(queued)
                    raise _TransportFailure(
                        "offline", "BitBrowser 连接管理器正在停止"
                    )
                self._execute_command(queued.command)

    def _close_command(self, profile_id: str, generation: int) -> dict[str, Any]:
        with self._action_condition:
            if any(
                active_profile == profile_id and count > 0
                for (active_profile, _), count in self._active_actions.items()
            ):
                self._deferred_action_closes.add(profile_id)
                self._close_enqueued.discard((profile_id, generation))
                return {
                    "profile_id": profile_id,
                    "requested": True,
                    "provider_success": False,
                    "provider_message": "waiting for active action",
                    "window_state": "closing",
                }
        self._require_ready()
        with self._state_lock:
            if (
                profile_id not in self._pending_closes
                and self._window_phase.get(profile_id) == "closed"
            ):
                self._close_enqueued.discard((profile_id, generation))
                return {
                    "profile_id": profile_id,
                    "requested": True,
                    "provider_success": True,
                    "provider_message": "already closed",
                    "window_state": "closed",
                }
        try:
            payload = self._call(
                self.base_url, "/browser/close", {"id": profile_id}
            )
        except _TransportFailure as exc:
            if exc.kind == "provider_rejected" and self._is_already_closed(exc.message):
                payload = {"success": True, "msg": exc.message}
            else:
                self._window_phase[profile_id] = "error"
                self._record_failure(exc)
                self._mark_close_retry(profile_id)
                with self._state_lock:
                    self._close_enqueued.discard((profile_id, generation))
                self._raise_domain_failure(exc, path="/browser/close")
        self._complete_close(profile_id, generation)
        return {
            "profile_id": profile_id,
            "requested": True,
            "provider_success": True,
            "provider_message": self._first(payload, "msg", "message"),
            "window_state": "closed",
        }

    def _begin_action_command(
        self,
        profile_id: str,
        generation: int,
        expected_endpoint: str,
        action_attempt_id: str | None,
    ) -> str:
        if action_attempt_id is not None:
            with self._state_lock:
                attempt = self._action_attempts.get(action_attempt_id)
                if (
                    attempt is None
                    or attempt.cancelled
                    or attempt.profile_id != profile_id
                    or attempt.generation != generation
                ):
                    self._action_attempts.pop(action_attempt_id, None)
                    raise UpstreamUnavailableError(
                        "BitBrowser 操作票据已取消",
                        details={"reason": "action_attempt_cancelled"},
                    )
        expected = self._safe_cdp_endpoint(
            expected_endpoint,
            allowed_schemes={"ws", "wss", "http", "https"},
        )
        if expected is None:
            raise UpstreamUnavailableError(
                "BitBrowser 调试连接地址无效",
                details={"profile_id": profile_id, "reason": "invalid_cdp_endpoint"},
            )
        self._raise_if_generation_stale(profile_id, generation)
        self._require_ready()
        current = self._ports_endpoint(profile_id, force=True)
        actual = current.get("ws") or current.get("http")
        if not actual or not self._endpoint_matches(actual, expected):
            raise UpstreamUnavailableError(
                "BitBrowser 调试端口已不再属于所选窗口",
                details={
                    "profile_id": profile_id,
                    "reason": "cdp_profile_mapping_changed",
                    "pause_required": True,
                },
            )
        with self._action_condition:
            if action_attempt_id is not None:
                attempt = self._action_attempts.get(action_attempt_id)
                if (
                    attempt is None
                    or attempt.cancelled
                    or attempt.profile_id != profile_id
                    or attempt.generation != generation
                ):
                    self._action_attempts.pop(action_attempt_id, None)
                    raise UpstreamUnavailableError(
                        "BitBrowser 操作票据已取消",
                        details={"reason": "action_attempt_cancelled"},
                    )
            if (
                self._window_generation.get(profile_id, 0) != generation
                or profile_id in self._pending_closes
            ):
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口已开始关闭，操作未执行",
                    details={
                        "profile_id": profile_id,
                        "reason": "action_window_closing",
                        "pause_required": True,
                    },
                )
            lease_id = uuid.uuid4().hex
            key = (profile_id, generation)
            self._action_leases[lease_id] = key
            self._active_actions[key] = self._active_actions.get(key, 0) + 1
            if action_attempt_id is not None:
                attempt.lease_id = lease_id
            return lease_id

    def _complete_close(self, profile_id: str, generation: int) -> None:
        with self._state_lock:
            pending_generation = self._pending_closes.get(profile_id)
            if pending_generation is None or pending_generation <= generation:
                self._pending_closes.pop(profile_id, None)
            self._endpoint_cache.pop(profile_id, None)
            self._close_retry_after.pop(profile_id, None)
            self._opening_generation.pop(profile_id, None)
            self._open_may_have_effect.pop(profile_id, None)
            self._open_confirmed_generation.pop(profile_id, None)
            self._cancelled_open_cohorts = {
                cohort
                for cohort in self._cancelled_open_cohorts
                if cohort[0] != profile_id
            }
            self._open_waiters = {
                cohort: waiters
                for cohort, waiters in self._open_waiters.items()
                if cohort[0] != profile_id
            }
            self._close_enqueued = {
                key for key in self._close_enqueued if key[0] != profile_id
            }
            self._window_phase[profile_id] = "closed"
        self._invalidate_ports_snapshot()
        self._patch_inventory_open(profile_id, False)

    @staticmethod
    def _is_already_closed(message: str) -> bool:
        normalized = message.casefold()
        return any(
            marker in normalized
            for marker in (
                "already closed",
                "not running",
                "not open",
                "not found",
                "does not exist",
                "未打开",
                "未运行",
                "不存在",
                "已关闭",
            )
        )

    def _compensating_close(self, profile_id: str) -> None:
        with self._state_lock:
            generation = self._window_generation.get(profile_id, 0)
        self._schedule_compensating_close(profile_id, generation)

    def _replay_pending_closes(self, *, urgent: bool = True) -> None:
        if self._phase() != "ready":
            return
        now = self.clock()
        with self._state_lock:
            pending = list(self._pending_closes.items())
        for profile_id, generation in pending:
            if self._stop.is_set():
                return
            with self._state_lock:
                retry = self._close_retry_after.get(profile_id)
                if (
                    (profile_id, generation) in self._close_enqueued
                    or profile_id in self._deferred_action_closes
                    or (
                        not urgent
                        and retry is not None
                        and now < retry[1]
                    )
                ):
                    continue
            self._enqueue_close_if_running(
                profile_id,
                generation,
                replay=True,
                urgent=urgent,
            )
            return

    def _mark_close_retry(self, profile_id: str) -> None:
        with self._state_lock:
            if profile_id not in self._pending_closes:
                return
            previous_failures, _ = self._close_retry_after.get(
                profile_id, (0, 0.0)
            )
            failures = previous_failures + 1
            delay = min(
                60.0,
                max(0.1, self.close_retry_base_seconds)
                * (2 ** min(previous_failures, 4)),
            )
            self._close_retry_after[profile_id] = (
                failures,
                self.clock() + delay,
            )

    # Provider and state helpers -------------------------------------
    def _require_ready(self) -> None:
        if self._phase() != "ready":
            # Business operations never bypass offline backoff, provider cooldown
            # or the explicit-login gate. Only confirm_login() is a force probe.
            self._background_probe(force=False)
        if self._phase() == "ready":
            return
        phase = self._phase()
        if phase == "auth_required":
            raise BitBrowserAuthRequiredError(
                "BitBrowser 已退出登录，请重新登录后继续",
                details={"state": "auth_required", "reason": "bitbrowser_login_required"},
            )
        if phase == "cooldown":
            raise BitBrowserRateLimitedError(
                "BitBrowser 请求正在冷却，请稍后继续",
                details={"state": "rate_limited", "reason": "bitbrowser_request_frequency_limit"},
            )
        raise UpstreamUnavailableError(
            "BitBrowser 当前未连接",
            details={"state": "offline", "reason": "bitbrowser_offline"},
        )

    def _call(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        assert self.transport is not None
        result = self.transport.post(
            base_url,
            path,
            payload,
            api_key=self.api_key,
            timeout_seconds=(
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
        )
        classification = self._payload_failure(result)
        if classification is not None:
            raise classification
        if path == "/browser/close" and not self._has_provider_ack(result):
            # Closing is the compensating/durable side of the lifecycle.  An
            # unacknowledged object must never clear _pending_closes merely
            # because it happened not to contain a familiar error string.
            raise _TransportFailure(
                "invalid_response",
                "BitBrowser 未返回明确的关闭成功确认",
            )
        if not self._provider_success(result):
            message = str(self._first(result, "msg", "message", "error") or "BitBrowser 拒绝了请求")
            raise _TransportFailure("provider_rejected", message[:160])
        return result

    def _record_ready(self, endpoint: str, *, detail: str) -> None:
        now = self.clock()
        with self._state_lock:
            changed = self._state.endpoint != endpoint or self._state.phase != "ready"
            self.base_url = endpoint
            self._state.endpoint = endpoint
            self._state.phase = "ready"
            self._state.consecutive_failures = 0
            self._state.last_success_epoch = self.wall_clock()
            self._state.next_probe_monotonic = now + max(1.0, self.health_interval_seconds)
            self._state.detail = detail
            if changed:
                self._state.generation += 1

    def _record_failure(self, failure: _TransportFailure) -> None:
        if failure.kind == "provider_rejected":
            # A profile may already be closed, missing or busy. That is a
            # per-command outcome and must not mark every BitBrowser window offline.
            return
        now = self.clock()
        with self._state_lock:
            self._inventory_degraded = True
            previous_phase = self._state.phase
            self._state.consecutive_failures += 1
            self._state.last_failure_epoch = self.wall_clock()
            if failure.kind == "auth_required":
                self._state.phase = "auth_required"
                delay = max(5.0, self.auth_probe_seconds)
            elif failure.kind == "rate_limited":
                self._state.phase = "cooldown"
                delay = max(
                    self.rate_limit_default_seconds,
                    failure.retry_after_seconds or 0.0,
                )
            else:
                self._state.phase = "offline"
                backoffs = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
                delay = backoffs[min(self._state.consecutive_failures - 1, len(backoffs) - 1)]
            self._state.next_probe_monotonic = now + delay
            self._state.detail = failure.message[:160]
            if self._state.phase != previous_phase:
                self._state.generation += 1

    def _raise_domain_failure(self, failure: _TransportFailure, *, path: str) -> None:
        details = {
            "path": path,
            "state": self._phase(),
            "retry_after_ms": self._retry_after_ms(),
        }
        if failure.kind == "auth_required":
            raise BitBrowserAuthRequiredError(failure.message, details=details)
        if failure.kind == "rate_limited":
            raise BitBrowserRateLimitedError(failure.message, details=details)
        raise UpstreamUnavailableError(failure.message, details=details)

    def _candidate_urls(self) -> list[str]:
        parsed = urlparse(self.base_url)
        ports: list[int] = []
        if parsed.port:
            ports.append(parsed.port)
        if self.auto_detect:
            ports.extend(self.port_candidates)
        unique: list[int] = []
        for port in ports:
            if 1 <= port <= 65535 and port not in unique:
                unique.append(port)
        return [f"http://127.0.0.1:{port}" for port in unique]

    def _status_snapshot(self, *, source: str) -> dict[str, Any]:
        with self._state_lock:
            phase = self._state.phase
            failures = self._state.consecutive_failures
            endpoint = self._state.endpoint or self.base_url
            compatible_state = (
                "connected"
                if phase == "ready"
                else "auth_required"
                if phase == "auth_required"
                else "rate_limited"
                if phase == "cooldown"
                else "recovering"
                if phase == "probing" or (phase == "offline" and failures < 3)
                else "disconnected"
                if phase in {"stopping", "stopped"}
                else "disconnected"
            )
            parsed = urlparse(endpoint)
            return {
                "state": compatible_state,
                "phase": phase,
                "connected": phase == "ready",
                "reachable": phase in {"ready", "auth_required", "cooldown"},
                "authenticated": phase in {"ready", "cooldown"},
                "auth_required": phase == "auth_required",
                "rate_limited": phase == "cooldown",
                "port": parsed.port,
                "generation": self._state.generation,
                "inventory_version": self._inventory_version,
                "consecutive_failures": failures,
                "last_success_at": self._diagnostic_time(self._state.last_success_epoch),
                "last_failure_at": self._diagnostic_time(self._state.last_failure_epoch),
                "retry_after_ms": self._retry_after_ms(),
                "recommended_poll_interval_ms": max(5_000, round(self.health_interval_seconds * 1000)),
                "detail": self._state.detail,
                "provider_code": 0 if phase == "ready" else None,
                "provider_message": self._state.detail,
                "latency_ms": 0,
                "inventory_freshness": (
                    "none"
                    if self._inventory is None
                    else "fresh"
                    if phase == "ready"
                    else "stale"
                ),
                "source": source,
            }

    def _retry_after_ms(self) -> int:
        return max(0, round((self._state.next_probe_monotonic - self.clock()) * 1000))

    def _cached_listing(self, *, name: str, source: str, stale: bool) -> dict[str, Any]:
        base = copy.deepcopy(
            self._inventory
            or {"total": 0, "windows": [], "provider_success": False}
        )
        query = name.strip().casefold()
        if query:
            base["windows"] = [
                item
                for item in base.get("windows", [])
                if query
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("id", "name", "group", "serial_number")
                ).casefold()
            ]
            base["total"] = len(base["windows"])
        base["stale"] = stale
        base["connection"] = self._status_snapshot(source=source)
        base["inventory_version"] = self._inventory_version
        return base

    def _ports_endpoint(
        self,
        profile_id: str,
        *,
        force: bool = False,
    ) -> dict[str, str | None]:
        try:
            now = self.clock()
            if (
                not force
                and self._ports_snapshot is not None
                and now - self._ports_snapshot_at <= max(0.0, self.ports_cache_seconds)
            ):
                data = self._ports_snapshot
            else:
                payload = self._call(self.base_url, "/browser/ports", {})
                data = self._provider_data(payload)
                self._ports_snapshot = copy.deepcopy(data)
                self._ports_snapshot_at = now
            endpoint = self._connection_endpoint_from_data(
                data, profile_id, allow_unscoped=False
            )
        except _TransportFailure as exc:
            self._record_failure(exc)
            self._raise_domain_failure(exc, path="/browser/ports")
        return endpoint

    def _invalidate_ports_snapshot(self) -> None:
        self._ports_snapshot = None
        self._ports_snapshot_at = 0.0

    def _generation_matches(self, profile_id: str, generation: int) -> bool:
        with self._state_lock:
            return self._window_generation.get(profile_id, 0) == generation

    def _raise_if_generation_stale(self, profile_id: str, generation: int) -> None:
        if self._generation_matches(profile_id, generation):
            return
        with self._state_lock:
            if self._window_phase.get(profile_id) == "opening":
                self._window_phase[profile_id] = "closed"
        raise UpstreamUnavailableError(
            "BitBrowser 窗口连接已取消",
            details={"profile_id": profile_id, "reason": "open_cancelled"},
        )

    def _raise_if_generation_stale_after_open(
        self, profile_id: str, generation: int
    ) -> None:
        if self._generation_matches(profile_id, generation):
            return
        self._schedule_compensating_close(profile_id, generation)
        raise UpstreamUnavailableError(
            "BitBrowser 窗口打开已取消",
            details={"profile_id": profile_id, "reason": "open_cancelled"},
        )

    @staticmethod
    def _public_endpoint(value: dict[str, Any]) -> dict[str, str | None]:
        return {
            "ws": value.get("ws") if isinstance(value.get("ws"), str) else None,
            "http": value.get("http") if isinstance(value.get("http"), str) else None,
        }

    @staticmethod
    def _endpoint_identity(value: str) -> tuple[str, int, str | None]:
        parsed = urlparse(value)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
            raise UpstreamUnavailableError(
                "BitBrowser 调试连接地址无效",
                details={"reason": "invalid_cdp_endpoint"},
            )
        ws_path = (
            parsed.path.rstrip("/") or "/"
            if parsed.scheme in {"ws", "wss"}
            else None
        )
        return ("loopback", parsed.port, ws_path)

    @classmethod
    def _endpoint_matches(cls, actual: str, expected: str) -> bool:
        actual_identity = cls._endpoint_identity(actual)
        expected_identity = cls._endpoint_identity(expected)
        if actual_identity[:2] != expected_identity[:2]:
            return False
        actual_path = actual_identity[2]
        expected_path = expected_identity[2]
        # When both sides provide a browser-scoped WebSocket URL, the UUID/path
        # is part of the ownership identity. HTTP debugger addresses have no
        # equivalent path, so a same-port HTTP/WS comparison remains compatible.
        return not (
            actual_path is not None
            and expected_path is not None
            and actual_path != expected_path
        )

    def _accept_endpoint(
        self,
        profile_id: str,
        generation: int,
        endpoint: dict[str, str | None],
    ) -> None:
        with self._state_lock:
            if self._window_generation.get(profile_id, 0) != generation:
                raise UpstreamUnavailableError(
                    "BitBrowser 窗口连接已取消",
                    details={"profile_id": profile_id, "reason": "open_cancelled"},
                )
            self._endpoint_cache[profile_id] = copy.deepcopy(endpoint)
            if self._open_may_have_effect.get(profile_id) != generation:
                self._opening_generation.pop(profile_id, None)
            self._window_phase[profile_id] = "ready"
            self._patch_inventory_open(profile_id, True)

    def _patch_inventory_open(self, profile_id: str, is_open: bool) -> None:
        if not self._inventory:
            return
        for window in self._inventory.get("windows", []):
            if window.get("id") == profile_id:
                window["is_open"] = is_open
                window["window_state"] = self._window_phase.get(profile_id)

    @staticmethod
    def _diagnostic_time(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")

    @classmethod
    def _payload_failure(cls, payload: dict[str, Any]) -> _TransportFailure | None:
        text = cls._diagnostic_text(payload).casefold()
        if any(marker in text for marker in _AUTH_MARKERS):
            return _TransportFailure("auth_required", "BitBrowser 已退出登录")
        if any(marker in text for marker in _RATE_MARKERS):
            return _TransportFailure("rate_limited", "BitBrowser 请求正在冷却")
        for envelope in cls._response_envelopes(payload):
            raw_status = cls._first(
                envelope, "status", "statusCode", "code"
            )
            if isinstance(raw_status, str) and raw_status.strip().casefold() in {
                "error",
                "fail",
                "failed",
                "failure",
                "false",
                "no",
            }:
                message = str(
                    cls._first(envelope, "msg", "message", "detail", "reason")
                    or "BitBrowser Local API 拒绝了请求"
                )
                return _TransportFailure("provider_rejected", message[:160])
            numeric_status = cls._numeric_status(raw_status)
            if numeric_status in {401, 403}:
                return _TransportFailure(
                    "auth_required",
                    "BitBrowser 已退出登录",
                    status=numeric_status,
                )
            if numeric_status == 429:
                return _TransportFailure(
                    "rate_limited", "BitBrowser 请求正在冷却", status=429
                )
            if numeric_status is not None and numeric_status not in {0, 200}:
                message = str(
                    cls._first(envelope, "msg", "message", "detail", "reason")
                    or f"BitBrowser Local API 返回状态 {numeric_status}"
                )
                return _TransportFailure(
                    "offline"
                    if numeric_status >= 500
                    else "provider_rejected",
                    message[:160],
                    status=numeric_status,
                )
            explicit = cls._first(envelope, "success", "ok")
            if explicit is not None and not cls._truthy_success(explicit):
                message = str(
                    cls._first(envelope, "msg", "message", "detail", "reason")
                    or "BitBrowser Local API 拒绝了请求"
                )
                return _TransportFailure(
                    "provider_rejected", message[:160]
                )
            error_value = envelope.get("error")
            if isinstance(error_value, str) and error_value.strip():
                return _TransportFailure(
                    "provider_rejected", error_value.strip()[:160]
                )
            if isinstance(error_value, (dict, list)) and bool(error_value):
                error_message = (
                    cls._first(
                        error_value,
                        "msg",
                        "message",
                        "detail",
                        "reason",
                    )
                    if isinstance(error_value, dict)
                    else None
                )
                return _TransportFailure(
                    "provider_rejected",
                    str(error_message or "BitBrowser Local API 返回错误信息")[:160],
                )
            if error_value not in (None, "", 0, False, {}, []):
                return _TransportFailure(
                    "provider_rejected",
                    f"BitBrowser Local API 返回错误标记 {str(error_value)[:80]}",
                )
        return None

    @classmethod
    def _has_provider_ack(cls, payload: dict[str, Any]) -> bool:
        return any(
            any(key in envelope for key in ("success", "ok", "status", "statusCode", "code"))
            for envelope in cls._response_envelopes(payload)
        )

    @classmethod
    def _response_envelopes(
        cls, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        pending: list[Any] = [payload]
        visited = 0
        while pending and visited < 32:
            value = pending.pop(0)
            visited += 1
            if not isinstance(value, dict):
                continue
            found.append(value)
            for key in ("data", "result", "error"):
                child = value.get(key)
                if isinstance(child, dict):
                    pending.append(child)
        return found

    @staticmethod
    def _truthy_success(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().casefold() in {
                "1",
                "true",
                "yes",
                "ok",
                "success",
            }
        return bool(value)

    @staticmethod
    def _numeric_status(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            normalized = value.strip()
            if re.fullmatch(r"-?\d+", normalized):
                return int(normalized)
        return None

    @classmethod
    def _diagnostic_text(cls, payload: dict[str, Any]) -> str:
        pending: list[Any] = [payload]
        values: list[str] = []
        visited = 0
        while pending and visited < 64:
            value = pending.pop()
            visited += 1
            if not isinstance(value, dict):
                continue
            for key, child in value.items():
                normalized = str(key).replace("_", "").casefold()
                if normalized in {"msg", "message", "error", "errormsg", "detail", "reason"} and isinstance(child, (str, int)):
                    values.append(str(child))
                elif normalized in {"data", "error", "result"} and isinstance(child, dict):
                    pending.append(child)
        return " ".join(values)

    @classmethod
    def _provider_success(cls, payload: dict[str, Any]) -> bool:
        if cls._payload_failure(payload) is not None:
            return False
        explicit = cls._first(payload, "success", "ok")
        if explicit is not None:
            return cls._truthy_success(explicit)
        status = cls._first(payload, "status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in {"ok", "success", "succeeded"}:
                return True
            if normalized in {"error", "fail", "failed", "failure", "false", "no"}:
                return False
            numeric_status = cls._numeric_status(status)
            if numeric_status is not None:
                return numeric_status in {0, 200}
            return False
        if isinstance(status, bool):
            return status
        if isinstance(status, (int, float)):
            return status in {0, 200}
        code = cls._first(payload, "code", "statusCode")
        if code is None:
            return True
        numeric_code = cls._numeric_status(code)
        return numeric_code in {0, 200}

    @classmethod
    def _provider_data(cls, payload: dict[str, Any]) -> Any:
        value: Any = payload
        for _ in range(3):
            if not isinstance(value, dict) or "data" not in value:
                break
            nested = value.get("data")
            if nested is value:
                break
            value = nested
        return value

    @classmethod
    def _has_profile_list(cls, payload: dict[str, Any]) -> bool:
        data = cls._provider_data(payload)
        if isinstance(data, list):
            return True
        return isinstance(data, dict) and any(
            isinstance(data.get(key), list)
            for key in ("list", "items", "records", "rows", "browserList")
        )

    @classmethod
    def _has_health_signature(cls, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        return cls._first(
            payload,
            "success",
            "ok",
            "status",
            "code",
            "statusCode",
        ) is not None

    @classmethod
    def _extract_profile_list(cls, payload: dict[str, Any]) -> tuple[list[Any], int | None]:
        data = cls._provider_data(payload)
        if isinstance(data, list):
            # A bare list describes this page, not the provider's global total.
            # Treating len(page) as total silently truncated a full 100-row first
            # page when more rows existed.
            return data, None
        if not isinstance(data, dict):
            return [], None
        profiles: list[Any] = []
        for key in ("list", "items", "records", "rows", "browserList"):
            if isinstance(data.get(key), list):
                profiles = data[key]
                break
        raw_total = cls._first(data, "total", "count", "totalCount", "totalNum")
        total = (
            int(raw_total)
            if isinstance(raw_total, (str, int))
            and not isinstance(raw_total, bool)
            and str(raw_total).isdigit()
            else None
        )
        return profiles, total

    @classmethod
    def _extract_open_ids(cls, payload: dict[str, Any]) -> set[str]:
        data = cls._provider_data(payload)
        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                profile_id = cls._first(value, "id", "browserId", "profileId", "browser_id")
                pid = cls._first(value, "pid", "processId", "process_id")
                if profile_id is not None and pid not in (None, 0, "0", ""):
                    found.add(str(profile_id))
                for key, child in value.items():
                    if isinstance(child, (dict, list)):
                        visit(child)
                    elif (
                        isinstance(child, int)
                        and not isinstance(child, bool)
                        and child > 0
                    ) or (
                        isinstance(child, str)
                        and child.isdigit()
                        and int(child) > 0
                    ):
                        found.add(str(key))

        visit(data)
        return found

    @classmethod
    def _extract_profile_pids(cls, payload: dict[str, Any]) -> dict[str, int]:
        """Normalize both map- and record-shaped ``/browser/pids/all`` replies."""

        data = cls._provider_data(payload)
        found: dict[str, int] = {}

        def positive_pid(value: Any) -> int | None:
            if isinstance(value, bool):
                return None
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)
            return None

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            profile_id = cls._first(
                value, "id", "browserId", "profileId", "browser_id"
            )
            process_id = positive_pid(
                cls._first(value, "pid", "processId", "process_id")
            )
            if profile_id is not None and process_id is not None:
                found[str(profile_id)] = process_id
            for key, child in value.items():
                mapped_pid = positive_pid(child)
                if (
                    mapped_pid is not None
                    and key not in {"pid", "processId", "process_id"}
                    and _PROFILE_ID_RE.fullmatch(str(key))
                ):
                    found[str(key)] = mapped_pid
                elif isinstance(child, (dict, list)):
                    visit(child)

        visit(data)
        return found

    def _normalize_profile(
        self,
        profile: dict[str, Any],
        open_ids: set[str],
        *,
        provider_order: int,
    ) -> dict[str, Any]:
        profile_id = str(self._first(profile, "id", "browserId", "profileId", "browser_id") or "")
        name = self._first(profile, "name", "browserName", "profileName", "remark")
        group = self._first(profile, "groupName", "group", "group_name")
        serial_number = self._first(
            profile,
            "serialNumber",
            "serial_number",
            "browserNo",
            "browser_no",
            "seq",
        )
        explicit_open = self._first(profile, "isOpen", "is_open", "opened", "open")
        is_open = profile_id in open_ids or explicit_open is True or str(explicit_open).casefold() in {"1", "true", "open", "opened", "running"}
        previous_phase = self._window_phase.get(profile_id)
        if profile_id in self._pending_closes:
            phase: WindowPhase = "closing"
        elif previous_phase == "opening":
            phase = "opening"
        else:
            # A fresh provider inventory is authoritative for externally opened or
            # closed windows. Never keep an old ready/error flag against fresh data.
            phase = "ready" if is_open else "closed"
        self._window_phase[profile_id] = phase
        if phase == "closed":
            self._endpoint_cache.pop(profile_id, None)
        return {
            "id": profile_id,
            "name": str(name) if name is not None else profile_id,
            "group": str(group) if group is not None else None,
            "serial_number": str(serial_number) if serial_number is not None else None,
            "provider_order": provider_order,
            "is_open": is_open,
            "window_state": phase,
            "generation": self._window_generation.get(profile_id, 0),
            "ready": phase == "ready",
            "opening": phase == "opening",
        }

    @staticmethod
    def _endpoint_available(endpoint: dict[str, str | None]) -> bool:
        return bool(endpoint.get("ws") or endpoint.get("http"))

    @classmethod
    def _connection_endpoint_from_data(
        cls,
        data: Any,
        profile_id: str,
        *,
        allow_unscoped: bool,
    ) -> dict[str, str | None]:
        item = cls._find_profile_data(
            data, profile_id, allow_unscoped=allow_unscoped
        )
        port = cls._extract_debug_port(data, profile_id, allow_unscoped=allow_unscoped)
        ws = cls._safe_cdp_endpoint(
            cls._first(item, "ws", "wsUrl", "webSocketDebuggerUrl"),
            allowed_schemes={"ws", "wss"},
        )
        http = cls._safe_cdp_endpoint(
            cls._first(item, "http", "httpUrl", "debuggerAddress"),
            allowed_schemes={"http", "https"},
        )
        return {
            "ws": ws,
            "http": http
            or (f"http://127.0.0.1:{port}" if port is not None else None),
        }

    @classmethod
    def _find_profile_data(
        cls,
        data: Any,
        profile_id: str,
        *,
        allow_unscoped: bool,
    ) -> dict[str, Any]:
        if isinstance(data, dict):
            direct_id = cls._first(data, "id", "browserId", "profileId", "browser_id")
            if str(direct_id) == profile_id or (direct_id is None and allow_unscoped):
                if cls._first(data, "ws", "wsUrl", "webSocketDebuggerUrl", "http", "httpUrl", "debuggerAddress"):
                    return data
            if profile_id in data and isinstance(data[profile_id], dict):
                return data[profile_id]
            for value in data.values():
                found = cls._find_profile_data(
                    value,
                    profile_id,
                    allow_unscoped=False,
                )
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = cls._find_profile_data(
                    value,
                    profile_id,
                    allow_unscoped=False,
                )
                if found:
                    return found
        return {}

    @staticmethod
    def _safe_cdp_endpoint(
        value: Any,
        *,
        allowed_schemes: set[str],
    ) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise _TransportFailure(
                "invalid_response", "BitBrowser 返回了无效的调试连接地址"
            )
        candidate = value.strip()
        if "://" not in candidate and {"http", "https"}.intersection(allowed_schemes):
            candidate = f"http://{candidate}"
        try:
            parsed = urlparse(candidate)
            port = parsed.port
        except ValueError as exc:
            raise _TransportFailure(
                "invalid_response", "BitBrowser 返回了无效的调试连接地址"
            ) from exc
        if (
            parsed.scheme.casefold() not in allowed_schemes
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
        ):
            raise _TransportFailure(
                "invalid_response", "BitBrowser 返回的调试连接不是安全的本机地址"
            )
        if parsed.hostname == "localhost":
            # Never leave the actual HTTP/preflight socket destination to local
            # name resolution.  A modified hosts file must not turn a nominal
            # localhost CDP endpoint into network egress.
            candidate = parsed._replace(netloc=f"127.0.0.1:{port}").geturl()
        return candidate

    @classmethod
    def _extract_debug_port(
        cls, data: Any, profile_id: str, *, allow_unscoped: bool
    ) -> int | None:
        if isinstance(data, list):
            for item in data:
                port = cls._extract_debug_port(
                    item,
                    profile_id,
                    allow_unscoped=False,
                )
                if port is not None:
                    return port
            return None
        if not isinstance(data, dict):
            return None
        candidate = data.get(profile_id)
        if isinstance(candidate, dict):
            candidate = cls._first(candidate, "port", "debugPort", "debug_port")
        if isinstance(candidate, (str, int)) and str(candidate).isdigit():
            value = int(candidate)
            return value if 1 <= value <= 65535 else None
        direct_id = cls._first(data, "id", "browserId", "profileId", "browser_id")
        direct = cls._first(data, "port", "debugPort", "debug_port")
        if isinstance(direct, (str, int)) and str(direct).isdigit() and (
            (direct_id is not None and str(direct_id) == profile_id)
            or (direct_id is None and allow_unscoped)
        ):
            value = int(direct)
            return value if 1 <= value <= 65535 else None
        for child in data.values():
            if isinstance(child, (dict, list)):
                port = cls._extract_debug_port(
                    child,
                    profile_id,
                    allow_unscoped=False,
                )
                if port is not None:
                    return port
        if allow_unscoped and len(data) == 1:
            only = next(iter(data.values()))
            if isinstance(only, (str, int)) and str(only).isdigit():
                value = int(only)
                return value if 1 <= value <= 65535 else None
        return None

    @staticmethod
    def _first(value: Any, *keys: str) -> Any:
        if not isinstance(value, dict):
            return None
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        return None


# Production imports retain the old public name while using the V2 actor.
BitBrowserClient = BitBrowserClientV2

__all__ = ["BitBrowserClient", "BitBrowserClientV2", "BitBrowserTransport", "validate_profile_id"]
