from __future__ import annotations

import copy
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import urlparse

from app.errors import (
    BitBrowserAuthRequiredError,
    BitBrowserRateLimitedError,
    UpstreamUnavailableError,
    ValidationError,
)

_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(slots=True)
class _CachedFailure:
    created_at: float
    message: str
    details: dict[str, Any]

    def as_error(self) -> UpstreamUnavailableError:
        return UpstreamUnavailableError(self.message, details=copy.deepcopy(self.details))


@dataclass(slots=True)
class _SharedBitBrowserState:
    """One process-wide inventory gate for a configured BitBrowser endpoint."""

    inventory_lock: threading.RLock = field(default_factory=threading.RLock)
    health_lock: threading.RLock = field(default_factory=threading.RLock)
    connection_ports_lock: threading.RLock = field(default_factory=threading.RLock)
    request_lock: threading.RLock = field(default_factory=threading.RLock)
    guard: threading.RLock = field(default_factory=threading.RLock)
    profiles_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    inventory_failure: _CachedFailure | None = None
    stale_started_at: float | None = None
    next_inventory_refresh_not_before: float = 0.0
    health_cache: tuple[float, dict[str, Any]] | None = None
    health_failure: _CachedFailure | None = None
    last_success_epoch: float | None = None
    last_failure_epoch: float | None = None
    consecutive_failures: int = 0
    auth_required_since_epoch: float | None = None
    auth_message: str | None = None
    auth_probe_not_before: float = 0.0
    auth_recovery_confirmed: bool = False
    auth_full_refresh_not_before: float = 0.0
    rate_limited_since_epoch: float | None = None
    rate_limit_message: str | None = None
    rate_limit_probe_not_before: float = 0.0
    # Worker attachment is much hotter than renderer inventory polling. One
    # /browser/ports response contains the endpoints for every open profile, so keep
    # a short process-wide snapshot and let concurrent window workers share it.
    connection_ports_cache: tuple[float, Any] | None = None
    connection_endpoint_cache: dict[
        str, tuple[float, dict[str, str | None]]
    ] = field(default_factory=dict)
    connection_open_attempted_at: dict[str, float] = field(default_factory=dict)
    connection_failure: _CachedFailure | None = None


def validate_profile_id(profile_id: str) -> str:
    candidate = profile_id.strip()
    if not _PROFILE_ID_RE.fullmatch(candidate):
        raise ValidationError("Invalid BitBrowser profile id")
    return candidate


@dataclass(slots=True)
class BitBrowserClient:
    """Defensive adapter around the loopback-only BitBrowser Local API.

    BitBrowser releases have used slightly different envelope fields. Business code sees
    stable summaries while the raw websocket/debug endpoint remains inside this adapter
    for the future Playwright worker.
    """

    base_url: str = "http://127.0.0.1:54345"
    api_key: str | None = None
    timeout_seconds: float = 5.0
    auto_detect: bool = True
    read_retry_attempts: int = 2
    retry_backoff_seconds: float = 0.15
    health_cache_seconds: float = 10.0
    health_failure_cache_seconds: float = 5.0
    # A ports read returns every currently open BitBrowser endpoint. A very short
    # shared TTL collapses a 100-window attach/re-attach wave to one Local API call
    # without hiding a genuinely closed/restarted profile for long.
    connection_ports_cache_seconds: float = 2.0
    connection_failure_cache_seconds: float = 2.0
    # /browser/open may be queued asynchronously. Do not send it again merely because
    # the ports snapshot expires before BitBrowser has published the new endpoint.
    connection_open_cooldown_seconds: float = 30.0
    # A complete 680-window inventory costs at least seven /browser/list calls. Keep
    # renderer polling cheap while enforcing a process-wide, non-bypassable provider
    # refresh floor even when several pages or API routes ask at the same time.
    profiles_cache_seconds: float = 60.0
    profiles_min_refresh_interval_seconds: float = 60.0
    profiles_failure_cache_seconds: float = 30.0
    inventory_page_delay_seconds: float = 0.35
    # Keep the most recent successful inventory through long local-network outages.
    # It is always marked degraded/unreachable, so callers can preserve selections
    # without using stale rows to launch new work.
    profiles_stale_if_error_seconds: float = 86_400.0
    # Authentication recovery is deliberately much quieter than ordinary health
    # polling: first prove the Local Server is alive with /health, then issue only
    # one list page. A complete seven-page refresh happens in a later cycle.
    auth_probe_interval_seconds: float = 300.0
    auth_manual_probe_min_interval_seconds: float = 10.0
    auth_full_refresh_delay_seconds: float = 60.0
    _detect_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _health_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _profiles_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _cache_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _connection_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _health_cache: tuple[float, dict[str, Any]] | None = field(init=False, repr=False, default=None)
    _profiles_cache: dict[str, tuple[float, dict[str, Any]]] = field(
        init=False, repr=False, default_factory=dict
    )
    _last_success_epoch: float | None = field(init=False, repr=False, default=None)
    _last_failure_epoch: float | None = field(init=False, repr=False, default=None)
    _consecutive_failures: int = field(init=False, repr=False, default=0)
    _shared_state: _SharedBitBrowserState = field(init=False, repr=False)

    _PROCESS_STATES_LOCK: ClassVar[threading.Lock] = threading.Lock()
    _PROCESS_STATES: ClassVar[dict[tuple[str, str], _SharedBitBrowserState]] = {}

    _AUTH_MESSAGE_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "msg",
            "message",
            "error",
            "errormsg",
            "errormessage",
            "detail",
            "reason",
        }
    )
    _AUTH_MESSAGE_MARKERS: ClassVar[tuple[str, ...]] = (
        "login out",
        "logged out",
        "log out",
        "logout",
        "not login",
        "not logged in",
        "login required",
        "log in required",
        "please login",
        "please log in",
        "sign in required",
        "authentication failed",
        "unauthorized",
        "未登录",
        "请登录",
        "重新登录",
        "登录失效",
        "登录已失效",
        "登录过期",
    )
    _RATE_LIMIT_MESSAGE_MARKERS: ClassVar[tuple[str, ...]] = (
        "too frequent",
        "frequent request",
        "request frequent",
        "too many request",
        "rate limit",
        "请求频繁",
        "请求过于频繁",
        "请求太频繁",
        "请求过多",
        "频率过高",
    )

    _READ_ONLY_PATHS = frozenset({"/health", "/browser/list", "/browser/pids/all", "/browser/ports"})

    def __post_init__(self) -> None:
        # Production clients that point at the same Local API share one gate across
        # the entire process. Test doubles intentionally stay isolated unless they
        # exercise the real BitBrowserClient type.
        if type(self) is BitBrowserClient:
            key = (self.base_url.rstrip("/").casefold(), self.api_key or "")
            with self._PROCESS_STATES_LOCK:
                state = self._PROCESS_STATES.setdefault(key, _SharedBitBrowserState())
        else:
            state = _SharedBitBrowserState()
        self._shared_state = state
        # Retain these legacy private attributes as aliases for local extensions and
        # older tests; the actual state they expose is now process-shared.
        self._profiles_lock = state.inventory_lock
        self._health_lock = state.health_lock
        self._cache_lock = state.guard
        self._connection_lock = state.guard
        self._profiles_cache = state.profiles_cache

    def _auth_error(self, *, path: str, provider_message: str | None = None) -> BitBrowserAuthRequiredError:
        details: dict[str, Any] = {
            "path": path,
            "state": "auth_required",
            "reason": "bitbrowser_login_required",
            "hint": "请重新登录 BitBrowser，然后在采集器中点击继续或立即检测",
        }
        safe_message = self._safe_provider_message(provider_message)
        if safe_message:
            details["provider_message"] = safe_message
        return BitBrowserAuthRequiredError(
            "BitBrowser 已退出登录，请重新登录后继续",
            details=details,
        )

    def _auth_is_required(self) -> bool:
        with self._shared_state.guard:
            return self._shared_state.auth_required_since_epoch is not None

    def _trip_auth_circuit(self, *, path: str, provider_message: str | None = None) -> None:
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        safe_message = self._safe_provider_message(provider_message)
        with self._shared_state.guard:
            if self._shared_state.auth_required_since_epoch is None:
                self._shared_state.auth_required_since_epoch = now_epoch
            self._shared_state.auth_message = safe_message or self._shared_state.auth_message
            self._shared_state.auth_probe_not_before = max(
                self._shared_state.auth_probe_not_before,
                now_monotonic + max(1.0, self.auth_probe_interval_seconds),
            )
            self._shared_state.auth_recovery_confirmed = False
            self._shared_state.auth_full_refresh_not_before = 0.0
            self._shared_state.rate_limited_since_epoch = None
            self._shared_state.rate_limit_message = None
            self._shared_state.rate_limit_probe_not_before = 0.0
            self._shared_state.last_failure_epoch = now_epoch
            self._shared_state.consecutive_failures += 1
            # A pre-logout /health result only proves that the old process was alive.
            self._shared_state.health_cache = None
            self._shared_state.health_failure = None

    def _confirm_auth_recovery(self) -> None:
        now_monotonic = time.monotonic()
        with self._shared_state.guard:
            self._shared_state.auth_required_since_epoch = None
            self._shared_state.auth_message = None
            self._shared_state.auth_probe_not_before = 0.0
            self._shared_state.rate_limited_since_epoch = None
            self._shared_state.rate_limit_message = None
            self._shared_state.rate_limit_probe_not_before = 0.0
            self._shared_state.auth_recovery_confirmed = True
            self._shared_state.auth_full_refresh_not_before = (
                now_monotonic + max(1.0, self.auth_full_refresh_delay_seconds)
            )
            self._shared_state.consecutive_failures = 0

    def _auth_circuit_error(self, path: str) -> BitBrowserAuthRequiredError:
        with self._shared_state.guard:
            message = self._shared_state.auth_message
        error = self._auth_error(path=path, provider_message=message)
        error.details["provider_call_skipped"] = True
        return error

    def _raise_if_provider_circuit_open(self, path: str) -> None:
        """Fail locally while an auth/rate circuit owns provider recovery.

        This check is intentionally repeated inside ``request_lock`` immediately
        before ``urlopen``. A caller can pass the cheap outer check and then wait
        behind another request which trips a circuit; the locked check prevents that
        already-queued caller from becoming one more provider request.
        """
        with self._shared_state.guard:
            auth_required = self._shared_state.auth_required_since_epoch is not None
            rate_limited = self._shared_state.rate_limited_since_epoch is not None
        if auth_required:
            raise self._auth_circuit_error(path)
        if rate_limited:
            raise self._rate_limit_circuit_error(path)

    def _rate_limit_error(
        self,
        *,
        path: str,
        provider_message: str | None = None,
        http_status: int | None = None,
    ) -> BitBrowserRateLimitedError:
        details: dict[str, Any] = {
            "path": path,
            "state": "rate_limited",
            "reason": "bitbrowser_request_frequency_limit",
            "hint": "请保持采集器等待，不要反复刷新 BitBrowser 窗口列表",
        }
        if http_status is not None:
            details["http_status"] = http_status
        safe_message = self._safe_provider_message(provider_message)
        if safe_message:
            details["provider_message"] = safe_message
        return BitBrowserRateLimitedError(
            "BitBrowser 已限制频繁请求，请等待后再检测",
            details=details,
        )

    def _rate_limit_is_active(self) -> bool:
        with self._shared_state.guard:
            return self._shared_state.rate_limited_since_epoch is not None

    def _trip_rate_limit_circuit(
        self,
        *,
        path: str,
        provider_message: str | None = None,
    ) -> None:
        del path
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        safe_message = self._safe_provider_message(provider_message)
        with self._shared_state.guard:
            if self._shared_state.rate_limited_since_epoch is None:
                self._shared_state.rate_limited_since_epoch = now_epoch
            self._shared_state.rate_limit_message = (
                safe_message or self._shared_state.rate_limit_message
            )
            self._shared_state.rate_limit_probe_not_before = max(
                self._shared_state.rate_limit_probe_not_before,
                now_monotonic + max(1.0, self.auth_probe_interval_seconds),
            )
            # A new provider classification replaces the previous one. In
            # particular, an auth recovery probe may be answered with a frequency
            # limit (and a rate recovery probe may reveal that BitBrowser logged
            # out), so never leave both circuits active at once.
            self._shared_state.auth_required_since_epoch = None
            self._shared_state.auth_message = None
            self._shared_state.auth_probe_not_before = 0.0
            self._shared_state.auth_recovery_confirmed = False
            self._shared_state.auth_full_refresh_not_before = 0.0
            self._shared_state.last_failure_epoch = now_epoch
            self._shared_state.consecutive_failures += 1
            self._shared_state.health_cache = None
            self._shared_state.health_failure = None

    def _rate_limit_circuit_error(self, path: str) -> BitBrowserRateLimitedError:
        with self._shared_state.guard:
            message = self._shared_state.rate_limit_message
        error = self._rate_limit_error(path=path, provider_message=message)
        error.details["provider_call_skipped"] = True
        return error

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # Once BitBrowser says "Login out!", do not let workers repeatedly call
        # any Local API route. Inventory recovery owns the only low-frequency,
        # explicitly bounded bypass for its two-step probe.
        self._raise_if_provider_circuit_open(path)
        attempted_base_url = self.base_url
        attempts = max(1, self.read_retry_attempts if path in self._READ_ONLY_PATHS else 1)
        last_error: UpstreamUnavailableError | None = None
        for attempt in range(attempts):
            try:
                result = self._post_once(self.base_url, path, payload)
                auth_message = self._auth_required_message(result)
                if auth_message is not None:
                    self._trip_auth_circuit(path=path, provider_message=auth_message)
                    raise self._auth_error(path=path, provider_message=auth_message)
                rate_limit_message = self._rate_limit_message(result)
                if rate_limit_message is not None:
                    self._trip_rate_limit_circuit(
                        path=path,
                        provider_message=rate_limit_message,
                    )
                    raise self._rate_limit_error(
                        path=path,
                        provider_message=rate_limit_message,
                    )
                if (
                    path in self._READ_ONLY_PATHS
                    and not self._provider_success(result)
                    and attempt + 1 < attempts
                ):
                    time.sleep(max(0.0, self.retry_backoff_seconds) * (attempt + 1))
                    continue
                break
            except BitBrowserAuthRequiredError as exc:
                if not self._auth_is_required():
                    self._trip_auth_circuit(
                        path=str(exc.details.get("path") or path),
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                raise
            except BitBrowserRateLimitedError as exc:
                if not self._rate_limit_is_active():
                    self._trip_rate_limit_circuit(
                        path=str(exc.details.get("path") or path),
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                raise
            except UpstreamUnavailableError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(max(0.0, self.retry_backoff_seconds) * (attempt + 1))
        else:
            # Port discovery is meaningful only when the documented list probe cannot
            # reach BitBrowser. A transient pids/ports failure on an already verified
            # URL must not fan out into probes against every candidate port.
            if not self.auto_detect or path != "/browser/list":
                assert last_error is not None
                raise last_error
            with self._detect_lock:
                # Another profiles request may already have completed detection while
                # this request was waiting for the lock. Retry that newly selected URL
                # before launching another multi-port probe.
                if self.base_url.rstrip("/") != attempted_base_url.rstrip("/"):
                    result = self._post_once(self.base_url, path, payload)
                    auth_message = self._auth_required_message(result)
                    if auth_message is not None:
                        self._trip_auth_circuit(path=path, provider_message=auth_message)
                        raise self._auth_error(path=path, provider_message=auth_message)
                    rate_limit_message = self._rate_limit_message(result)
                    if rate_limit_message is not None:
                        self._trip_rate_limit_circuit(
                            path=path,
                            provider_message=rate_limit_message,
                        )
                        raise self._rate_limit_error(
                            path=path,
                            provider_message=rate_limit_message,
                        )
                    return result
                detected = self._detect_base_url(exclude_base_url=self.base_url)
                if not detected:
                    assert last_error is not None
                    raise last_error
                self.base_url = detected
            result = self._post_once(self.base_url, path, payload)
            auth_message = self._auth_required_message(result)
            if auth_message is not None:
                self._trip_auth_circuit(path=path, provider_message=auth_message)
                raise self._auth_error(path=path, provider_message=auth_message)
            rate_limit_message = self._rate_limit_message(result)
            if rate_limit_message is not None:
                self._trip_rate_limit_circuit(path=path, provider_message=rate_limit_message)
                raise self._rate_limit_error(path=path, provider_message=rate_limit_message)
        if (
            path == "/browser/list"
            and self.auto_detect
            and self._provider_success(result)
            and not self._has_profile_list(result)
        ):
            detected = self._detect_base_url()
            if detected and detected != self.base_url.rstrip("/"):
                self.base_url = detected
                result = self._post_once(self.base_url, path, payload)
                auth_message = self._auth_required_message(result)
                if auth_message is not None:
                    self._trip_auth_circuit(path=path, provider_message=auth_message)
                    raise self._auth_error(path=path, provider_message=auth_message)
                rate_limit_message = self._rate_limit_message(result)
                if rate_limit_message is not None:
                    self._trip_rate_limit_circuit(
                        path=path,
                        provider_message=rate_limit_message,
                    )
                    raise self._rate_limit_error(
                        path=path,
                        provider_message=rate_limit_message,
                    )
                return result
        return result

    def _post_once(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        recovery_probe: bool = False,
    ) -> dict[str, Any]:
        if recovery_probe:
            # The circuit bypass is deliberately incapable of opening/closing a
            # profile or reading a full inventory. It is reserved for the explicit
            # recovery flow: one health request followed by one list row.
            probe_payload = payload or {}
            is_health_probe = path == "/health"
            is_tiny_list_probe = (
                path == "/browser/list"
                and probe_payload.get("page") == 0
                and probe_payload.get("pageSize") == 1
            )
            if not (is_health_probe or is_tiny_list_probe):
                raise ValueError("Circuit recovery bypass only permits /health or one list row")
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if self.api_key:
            request.add_header("x-api-key", self.api_key)
        # BitBrowser's Local API is sensitive to overlapping calls. Serialize all
        # provider traffic process-wide, including worker open/ports operations. Keep
        # response classification under this same lock so a queued caller cannot
        # acquire it in the gap between a provider rejection and circuit activation.
        with self._shared_state.request_lock:
            if not recovery_probe:
                self._raise_if_provider_circuit_open(path)
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
                ) as response:
                    raw = response.read(4 * 1024 * 1024)
            except urllib.error.HTTPError as exc:
                # Do not include response bodies: they can contain local API details
                # or endpoints. Trip while still holding request_lock so waiters see
                # the circuit before they can issue another provider request.
                if exc.code in {401, 403}:
                    self._trip_auth_circuit(path=path)
                    error = self._auth_error(path=path)
                    error.details["http_status"] = exc.code
                    raise error from exc
                if exc.code == 429:
                    self._trip_rate_limit_circuit(path=path)
                    error = self._rate_limit_error(path=path, http_status=exc.code)
                    raise error from exc
                raise UpstreamUnavailableError(
                    "BitBrowser Local API rejected the request",
                    details={"http_status": exc.code, "path": path},
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise UpstreamUnavailableError(
                    "BitBrowser Local API is not reachable",
                    details={"path": path, "hint": "Start BitBrowser, sign in and enable Local API"},
                ) from exc
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpstreamUnavailableError(
                    "BitBrowser Local API returned invalid JSON", details={"path": path}
                ) from exc
            if not isinstance(result, dict):
                raise UpstreamUnavailableError(
                    "BitBrowser Local API returned an unexpected response", details={"path": path}
                )
            auth_message = self._auth_required_message(result)
            if auth_message is not None:
                self._trip_auth_circuit(path=path, provider_message=auth_message)
                raise self._auth_error(path=path, provider_message=auth_message)
            rate_limit_message = self._rate_limit_message(result)
            if rate_limit_message is not None:
                self._trip_rate_limit_circuit(path=path, provider_message=rate_limit_message)
                raise self._rate_limit_error(path=path, provider_message=rate_limit_message)
            return result

    def _detect_base_url(self, *, exclude_base_url: str | None = None) -> str | None:
        parsed = urlparse(self.base_url)
        current_port = parsed.port
        candidates = [
            current_port,
            54345,
            54346,
            54347,
            54348,
            54349,
            54350,
            54344,
        ]
        seen: set[int] = set()
        for port in candidates:
            if not isinstance(port, int) or port in seen:
                continue
            seen.add(port)
            base_url = f"http://127.0.0.1:{port}"
            if exclude_base_url and base_url.rstrip("/") == exclude_base_url.rstrip("/"):
                continue
            try:
                health_payload = self._post_once(
                    base_url,
                    "/health",
                    {},
                    timeout_seconds=min(self.timeout_seconds, 0.6),
                )
            except BitBrowserAuthRequiredError as exc:
                if exc.details.get("provider_call_skipped"):
                    raise
                self.base_url = base_url
                if not self._auth_is_required():
                    self._trip_auth_circuit(path="/health")
                raise self._auth_error(path="/health") from exc
            except BitBrowserRateLimitedError as exc:
                if exc.details.get("provider_call_skipped"):
                    raise
                self.base_url = base_url
                if not self._rate_limit_is_active():
                    self._trip_rate_limit_circuit(path="/health")
                raise self._rate_limit_error(path="/health") from exc
            except UpstreamUnavailableError:
                continue
            if not self._provider_success(health_payload):
                continue
            # Only a healthy candidate receives one tiny list verification. Never
            # launch a full inventory while discovering a port.
            try:
                payload = self._post_once(
                    base_url,
                    "/browser/list",
                    {"page": 0, "pageSize": 1},
                    timeout_seconds=min(self.timeout_seconds, 0.8),
                )
            except BitBrowserAuthRequiredError as exc:
                if exc.details.get("provider_call_skipped"):
                    raise
                self.base_url = base_url
                if not self._auth_is_required():
                    self._trip_auth_circuit(path="/browser/list")
                raise self._auth_error(path="/browser/list") from exc
            except BitBrowserRateLimitedError as exc:
                if exc.details.get("provider_call_skipped"):
                    raise
                self.base_url = base_url
                if not self._rate_limit_is_active():
                    self._trip_rate_limit_circuit(path="/browser/list")
                raise self._rate_limit_error(path="/browser/list") from exc
            except UpstreamUnavailableError:
                continue
            auth_message = self._auth_required_message(payload)
            if auth_message is not None:
                self.base_url = base_url
                self._trip_auth_circuit(path="/browser/list", provider_message=auth_message)
                raise self._auth_error(path="/browser/list", provider_message=auth_message)
            rate_limit_message = self._rate_limit_message(payload)
            if rate_limit_message is not None:
                self.base_url = base_url
                self._trip_rate_limit_circuit(
                    path="/browser/list",
                    provider_message=rate_limit_message,
                )
                raise self._rate_limit_error(
                    path="/browser/list",
                    provider_message=rate_limit_message,
                )
            if self._provider_success(payload) and self._has_profile_list(payload):
                return base_url
        return None

    def health(self) -> dict[str, Any]:
        """Probe the documented lightweight endpoint without consuming list quota."""
        request_started = time.monotonic()
        with self._shared_state.health_lock:
            cached = self._get_cached(
                self._shared_state.health_cache,
                self.health_cache_seconds,
            )
            if cached is not None:
                return self._with_health_diagnostics(
                    cached,
                    source="cache",
                    reachable=True,
                    started_at=request_started,
                )
            failure = self._shared_state.health_failure
            failure_ttl = max(0.0, self.health_failure_cache_seconds)
            if (
                failure is not None
                and failure_ttl > 0
                and time.monotonic() - failure.created_at
                <= failure_ttl
            ):
                if self._auth_is_required() or self._rate_limit_is_active():
                    return self._with_health_diagnostics(
                        {},
                        source="failure_cache",
                        reachable=False,
                        started_at=request_started,
                    )
                raise failure.as_error()
            try:
                payload = self._post("/health", {})
                self._require_provider_success(payload, "/health")
            except BitBrowserAuthRequiredError as exc:
                if not self._auth_is_required():
                    self._trip_auth_circuit(
                        path="/health",
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                return self._with_health_diagnostics(
                    {},
                    source="fresh",
                    reachable=True,
                    started_at=request_started,
                )
            except BitBrowserRateLimitedError as exc:
                if not self._rate_limit_is_active():
                    self._trip_rate_limit_circuit(
                        path="/health",
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                return self._with_health_diagnostics(
                    {},
                    source="fresh",
                    reachable=True,
                    started_at=request_started,
                )
            except UpstreamUnavailableError as exc:
                self._shared_state.health_failure = _CachedFailure(
                    time.monotonic(), exc.message, copy.deepcopy(exc.details)
                )
                if self._auth_is_required() or self._rate_limit_is_active():
                    return self._with_health_diagnostics(
                        {},
                        source="fresh",
                        reachable=False,
                        started_at=request_started,
                    )
                raise
            result = {
                "provider_code": self._first(payload, "code", "status", "statusCode"),
                "provider_message": self._first(payload, "msg", "message"),
            }
            self._shared_state.health_failure = None
            self._shared_state.health_cache = (time.monotonic(), copy.deepcopy(result))
            return self._with_health_diagnostics(
                result,
                source="fresh",
                reachable=True,
                started_at=request_started,
            )

    def list_windows(self, *, page: int = 0, page_size: int = 100, name: str = "") -> dict[str, Any]:
        if page < 0:
            raise ValidationError("page must be zero or greater")
        if not 1 <= page_size <= 100:
            raise ValidationError("page_size must be between 1 and 100")
        listing = self.list_all_windows(name=name)
        start = page * page_size
        windows = listing["windows"][start : start + page_size]
        return {
            "page": page,
            "page_size": page_size,
            "total": listing["total"],
            "windows": windows,
            "provider_success": listing.get("provider_success", False),
            "stale": bool(listing.get("stale", False)),
            "connection": listing.get("connection", {}),
        }

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        """Return one process-shared inventory, filtering searches in memory."""
        request_started = time.monotonic()
        with self._shared_state.inventory_lock:
            cached_entry = self._shared_state.profiles_cache.get("")
            now = time.monotonic()
            with self._shared_state.guard:
                auth_required = self._shared_state.auth_required_since_epoch is not None
                auth_probe_not_before = self._shared_state.auth_probe_not_before
                rate_limited = self._shared_state.rate_limited_since_epoch is not None
                rate_probe_not_before = self._shared_state.rate_limit_probe_not_before
                recovery_confirmed = self._shared_state.auth_recovery_confirmed
                full_refresh_not_before = self._shared_state.auth_full_refresh_not_before

            if auth_required:
                if now >= auth_probe_not_before:
                    return self._filter_listing(
                        self._probe_auth_recovery_locked(
                            started_at=request_started,
                            cache_entry=cached_entry,
                            manual=False,
                        ),
                        name,
                    )
                return self._filter_listing(
                    self._auth_required_listing(
                        started_at=request_started,
                        cache_entry=cached_entry,
                        reachable=False,
                        source="auth_cache",
                    ),
                    name,
                )

            if rate_limited:
                if now >= rate_probe_not_before:
                    return self._filter_listing(
                        self._probe_auth_recovery_locked(
                            started_at=request_started,
                            cache_entry=cached_entry,
                            manual=False,
                        ),
                        name,
                    )
                return self._filter_listing(
                    self._rate_limited_listing(
                        started_at=request_started,
                        cache_entry=cached_entry,
                        reachable=False,
                        source="rate_limit_cache",
                    ),
                    name,
                )

            if recovery_confirmed and now < full_refresh_not_before:
                return self._filter_listing(
                    self._recovery_listing(
                        started_at=request_started,
                        cache_entry=cached_entry,
                    ),
                    name,
                )
            if recovery_confirmed:
                with self._shared_state.guard:
                    self._shared_state.auth_recovery_confirmed = False
                    self._shared_state.auth_full_refresh_not_before = 0.0

            cached = self._get_cached(
                cached_entry,
                max(self.profiles_cache_seconds, self.profiles_min_refresh_interval_seconds),
            )
            if cached is not None or (
                cached_entry is not None
                and now < self._shared_state.next_inventory_refresh_not_before
            ):
                cached_value = cached if cached is not None else copy.deepcopy(cached_entry[1])
                return self._filter_listing(
                    self._with_connection_diagnostics(
                        cached_value,
                        source="cache",
                        started_at=request_started,
                        cache_entry=cached_entry,
                    ),
                    name,
                )

            failure = self._shared_state.inventory_failure
            failure_ttl = max(
                0.0,
                self.profiles_failure_cache_seconds,
                self.profiles_min_refresh_interval_seconds,
            )
            if (
                failure is not None
                and failure_ttl > 0
                and now - failure.created_at <= failure_ttl
            ):
                stale = self._ordinary_stale_snapshot(cached_entry)
                if stale is not None:
                    return self._filter_listing(
                        self._with_connection_diagnostics(
                            stale,
                            source="stale",
                            started_at=request_started,
                            cache_entry=cached_entry,
                        ),
                        name,
                    )
                raise failure.as_error()

            try:
                # Provider-side name queries would create one independent seven-page
                # refresh per search box. Always fetch once, then filter locally.
                result = self._list_all_windows_uncached(name="")
            except BitBrowserAuthRequiredError as exc:
                if not self._auth_is_required():
                    self._trip_auth_circuit(
                        path=str(exc.details.get("path") or "/browser/list"),
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                return self._filter_listing(
                    self._auth_required_listing(
                        started_at=request_started,
                        cache_entry=cached_entry,
                        reachable=False,
                        source="auth_cache",
                    ),
                    name,
                )
            except BitBrowserRateLimitedError as exc:
                if not self._rate_limit_is_active():
                    self._trip_rate_limit_circuit(
                        path=str(exc.details.get("path") or "/browser/list"),
                        provider_message=str(exc.details.get("provider_message") or ""),
                    )
                return self._filter_listing(
                    self._rate_limited_listing(
                        started_at=request_started,
                        cache_entry=cached_entry,
                        reachable=False,
                        source="rate_limit_cache",
                    ),
                    name,
                )
            except UpstreamUnavailableError as exc:
                self._mark_connection_failure(exc)
                stale = self._ordinary_stale_snapshot(cached_entry)
                if stale is None:
                    raise
                return self._filter_listing(
                    self._with_connection_diagnostics(
                        stale,
                        source="stale",
                        started_at=request_started,
                        cache_entry=cached_entry,
                    ),
                    name,
                )
            self._mark_connection_success()
            result["stale"] = False
            completed_at = time.monotonic()
            self._shared_state.profiles_cache[""] = (completed_at, copy.deepcopy(result))
            self._shared_state.inventory_failure = None
            self._shared_state.stale_started_at = None
            self._shared_state.next_inventory_refresh_not_before = (
                completed_at + max(0.0, self.profiles_min_refresh_interval_seconds)
            )
            stored_entry = self._shared_state.profiles_cache[""]
            return self._filter_listing(
                self._with_connection_diagnostics(
                    result,
                    source="fresh",
                    started_at=request_started,
                    cache_entry=stored_entry,
                ),
                name,
            )

    def _mark_connection_success(self) -> None:
        with self._shared_state.guard:
            self._shared_state.last_success_epoch = time.time()
            self._shared_state.consecutive_failures = 0

    def _mark_connection_failure(self, error: UpstreamUnavailableError) -> None:
        now_monotonic = time.monotonic()
        with self._shared_state.guard:
            self._shared_state.last_failure_epoch = time.time()
            self._shared_state.consecutive_failures += 1
            if self._shared_state.stale_started_at is None:
                self._shared_state.stale_started_at = now_monotonic
            self._shared_state.inventory_failure = _CachedFailure(
                now_monotonic,
                error.message,
                copy.deepcopy(error.details),
            )
            self._shared_state.next_inventory_refresh_not_before = (
                now_monotonic
                + max(
                    0.0,
                    self.profiles_min_refresh_interval_seconds,
                    self.profiles_failure_cache_seconds,
                )
            )

    def _ordinary_stale_snapshot(
        self,
        cache_entry: tuple[float, dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        if cache_entry is None or self.profiles_stale_if_error_seconds <= 0:
            return None
        with self._shared_state.guard:
            stale_started_at = self._shared_state.stale_started_at
        if stale_started_at is None:
            return None
        # The contract is the maximum age of the *last successful inventory*, not
        # an additional grace period starting at the first failure. Otherwise a
        # snapshot already 20 hours old when Wi-Fi drops could remain visible for
        # almost 44 hours under a nominal 24-hour limit.
        if time.monotonic() - cache_entry[0] > self.profiles_stale_if_error_seconds:
            return None
        stale = copy.deepcopy(cache_entry[1])
        stale["stale"] = True
        return stale

    @staticmethod
    def _diagnostic_time(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")

    def _with_connection_diagnostics(
        self,
        listing: dict[str, Any],
        *,
        source: str,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Attach non-sensitive, low-frequency connection facts for the renderer."""
        with self._shared_state.guard:
            last_success = self._shared_state.last_success_epoch
            last_failure = self._shared_state.last_failure_epoch
            failures = self._shared_state.consecutive_failures
            auth_required = self._shared_state.auth_required_since_epoch is not None
            rate_limited = self._shared_state.rate_limited_since_epoch is not None
            recovering = self._shared_state.auth_recovery_confirmed
            auth_probe_not_before = self._shared_state.auth_probe_not_before
            rate_probe_not_before = self._shared_state.rate_limit_probe_not_before
            full_refresh_not_before = self._shared_state.auth_full_refresh_not_before
        parsed = urlparse(self.base_url)
        now = time.monotonic()
        cache_age_ms = (
            max(0, round((now - cache_entry[0]) * 1000))
            if cache_entry is not None
            else None
        )
        if auth_required:
            state = "auth_required"
            reachable = False
            retry_at = auth_probe_not_before
        elif rate_limited:
            state = "rate_limited"
            reachable = False
            retry_at = rate_probe_not_before
        elif recovering:
            state = "recovering"
            reachable = True
            retry_at = full_refresh_not_before
        elif source == "stale":
            state = "degraded"
            reachable = False
            retry_at = self._shared_state.next_inventory_refresh_not_before
        else:
            state = "connected"
            reachable = True
            retry_at = self._shared_state.next_inventory_refresh_not_before
        result = copy.deepcopy(listing)
        result["connection"] = {
            "state": state,
            "reachable": reachable,
            "authenticated": not auth_required,
            "auth_required": auth_required,
            "rate_limited": rate_limited,
            "port": parsed.port,
            "last_success_at": self._diagnostic_time(last_success),
            "last_failure_at": self._diagnostic_time(last_failure),
            "consecutive_failures": failures,
            "latency_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            "source": source,
            "cache_age_ms": cache_age_ms,
            "retry_after_ms": max(0, round((retry_at - now) * 1000)),
            "reason": (
                "bitbrowser_login_required"
                if auth_required
                else ("bitbrowser_request_frequency_limit" if rate_limited else None)
            ),
            "recommended_poll_interval_ms": max(
                60_000,
                round(self.profiles_min_refresh_interval_seconds * 1000),
                round(self.profiles_cache_seconds * 1000),
            ),
        }
        return result

    def _with_health_diagnostics(
        self,
        health: dict[str, Any],
        *,
        source: str,
        reachable: bool,
        started_at: float,
    ) -> dict[str, Any]:
        with self._shared_state.guard:
            auth_required = self._shared_state.auth_required_since_epoch is not None
            rate_limited = self._shared_state.rate_limited_since_epoch is not None
            recovering = self._shared_state.auth_recovery_confirmed
            auth_probe_not_before = self._shared_state.auth_probe_not_before
            rate_probe_not_before = self._shared_state.rate_limit_probe_not_before
        state = (
            "auth_required"
            if auth_required
            else (
                "rate_limited"
                if rate_limited
                else ("recovering" if recovering else "connected")
            )
        )
        result = copy.deepcopy(health)
        result.update(
            {
                "connected": reachable and not auth_required and not rate_limited,
                "reachable": reachable,
                "authenticated": not auth_required,
                "auth_required": auth_required,
                "rate_limited": rate_limited,
                "state": state,
                "source": source,
                "latency_ms": max(0, round((time.monotonic() - started_at) * 1000)),
                "retry_after_ms": (
                    max(
                        0,
                        round(
                            (
                                auth_probe_not_before
                                if auth_required
                                else rate_probe_not_before
                            )
                            * 1000
                            - time.monotonic() * 1000
                        ),
                    )
                    if auth_required or rate_limited
                    else 0
                ),
            }
        )
        return result

    def _base_cached_listing(
        self,
        cache_entry: tuple[float, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if cache_entry is not None:
            result = copy.deepcopy(cache_entry[1])
            result["stale"] = True
            return result
        return {"total": 0, "windows": [], "provider_success": False, "stale": True}

    def _auth_required_listing(
        self,
        *,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
        reachable: bool,
        source: str,
    ) -> dict[str, Any]:
        listing = self._with_connection_diagnostics(
            self._base_cached_listing(cache_entry),
            source=source,
            started_at=started_at,
            cache_entry=cache_entry,
        )
        listing["connection"]["reachable"] = reachable
        listing["connection"]["authenticated"] = False
        listing["connection"]["auth_required"] = True
        return listing

    def _rate_limited_listing(
        self,
        *,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
        reachable: bool,
        source: str,
    ) -> dict[str, Any]:
        listing = self._with_connection_diagnostics(
            self._base_cached_listing(cache_entry),
            source=source,
            started_at=started_at,
            cache_entry=cache_entry,
        )
        listing["connection"]["state"] = "rate_limited"
        listing["connection"]["reachable"] = reachable
        listing["connection"]["rate_limited"] = True
        listing["connection"]["reason"] = "bitbrowser_request_frequency_limit"
        return listing

    def _blocked_listing(
        self,
        *,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
        reachable: bool,
        source: str,
    ) -> dict[str, Any]:
        if self._auth_is_required():
            return self._auth_required_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=reachable,
                source=source,
            )
        return self._rate_limited_listing(
            started_at=started_at,
            cache_entry=cache_entry,
            reachable=reachable,
            source=source,
        )

    def _recovery_listing(
        self,
        *,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        return self._with_connection_diagnostics(
            self._base_cached_listing(cache_entry),
            source="auth_probe",
            started_at=started_at,
            cache_entry=cache_entry,
        )

    def _filter_listing(self, listing: dict[str, Any], name: str) -> dict[str, Any]:
        query = name.strip().casefold()
        if not query:
            return listing
        result = copy.deepcopy(listing)
        result["windows"] = [
            item
            for item in result.get("windows", [])
            if query
            in " ".join(
                str(item.get(key) or "") for key in ("id", "name", "group")
            ).casefold()
        ]
        result["total"] = len(result["windows"])
        return result

    def confirm_login(self) -> dict[str, Any]:
        """User-triggered, throttled auth check; never performs a full inventory read."""
        request_started = time.monotonic()
        with self._shared_state.inventory_lock:
            cache_entry = self._shared_state.profiles_cache.get("")
            if not self._auth_is_required() and not self._rate_limit_is_active():
                if self._shared_state.auth_recovery_confirmed:
                    return self._recovery_listing(
                        started_at=request_started,
                        cache_entry=cache_entry,
                    )
                cached = self._get_cached(
                    cache_entry,
                    max(self.profiles_cache_seconds, self.profiles_min_refresh_interval_seconds),
                )
                if cached is not None:
                    return self._with_connection_diagnostics(
                        cached,
                        source="cache",
                        started_at=request_started,
                        cache_entry=cache_entry,
                    )
                # There is no auth circuit to confirm; normal inventory owns refreshes.
                return self.list_all_windows()
            return self._probe_auth_recovery_locked(
                started_at=request_started,
                cache_entry=cache_entry,
                manual=True,
            )

    def _probe_auth_recovery_locked(
        self,
        *,
        started_at: float,
        cache_entry: tuple[float, dict[str, Any]] | None,
        manual: bool,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._shared_state.guard:
            auth_required = self._shared_state.auth_required_since_epoch is not None
            not_before = (
                self._shared_state.auth_probe_not_before
                if auth_required
                else self._shared_state.rate_limit_probe_not_before
            )
            if manual:
                last_probe = not_before - max(1.0, self.auth_probe_interval_seconds)
                manual_not_before = (
                    last_probe + max(1.0, self.auth_manual_probe_min_interval_seconds)
                )
                if now < manual_not_before:
                    return self._blocked_listing(
                        started_at=started_at,
                        cache_entry=cache_entry,
                        reachable=False,
                        source="provider_cooldown",
                    )
            elif now < not_before:
                return self._blocked_listing(
                    started_at=started_at,
                    cache_entry=cache_entry,
                    reachable=False,
                    source="provider_cooldown",
                )
            if auth_required:
                self._shared_state.auth_probe_not_before = (
                    now + max(1.0, self.auth_probe_interval_seconds)
                )
            else:
                self._shared_state.rate_limit_probe_not_before = (
                    now + max(1.0, self.auth_probe_interval_seconds)
                )

        # /health proves only that the Local Server is reachable. It does not clear
        # auth_required. Login recovery needs exactly one tiny list request after it.
        try:
            health_payload = self._post_once(
                self.base_url,
                "/health",
                {},
                timeout_seconds=self.timeout_seconds,
                recovery_probe=True,
            )
            self._require_provider_success(health_payload, "/health")
        except BitBrowserAuthRequiredError as exc:
            if not self._auth_is_required():
                self._trip_auth_circuit(
                    path="/health",
                    provider_message=str(exc.details.get("provider_message") or ""),
                )
            return self._auth_required_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=True,
                source="auth_probe",
            )
        except BitBrowserRateLimitedError as exc:
            if not self._rate_limit_is_active():
                self._trip_rate_limit_circuit(
                    path="/health",
                    provider_message=str(exc.details.get("provider_message") or ""),
                )
            return self._rate_limited_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=True,
                source="provider_probe",
            )
        except UpstreamUnavailableError:
            return self._blocked_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=False,
                source="provider_probe",
            )

        try:
            list_payload = self._post_once(
                self.base_url,
                "/browser/list",
                {"page": 0, "pageSize": 1, "sort": "asc"},
                timeout_seconds=self.timeout_seconds,
                recovery_probe=True,
            )
            auth_message = self._auth_required_message(list_payload)
            if auth_message is not None:
                self._trip_auth_circuit(
                    path="/browser/list",
                    provider_message=auth_message,
                )
                return self._auth_required_listing(
                    started_at=started_at,
                    cache_entry=cache_entry,
                    reachable=True,
                    source="auth_probe",
                )
            rate_limit_message = self._rate_limit_message(list_payload)
            if rate_limit_message is not None:
                self._trip_rate_limit_circuit(
                    path="/browser/list",
                    provider_message=rate_limit_message,
                )
                return self._rate_limited_listing(
                    started_at=started_at,
                    cache_entry=cache_entry,
                    reachable=True,
                    source="provider_probe",
                )
            self._require_provider_success(list_payload, "/browser/list")
            if not self._has_profile_list(list_payload):
                raise UpstreamUnavailableError(
                    "已连接到本机端口，但无法识别 BitBrowser 窗口列表",
                    details={"path": "/browser/list"},
                )
        except BitBrowserAuthRequiredError as exc:
            if not self._auth_is_required():
                self._trip_auth_circuit(
                    path="/browser/list",
                    provider_message=str(exc.details.get("provider_message") or ""),
                )
            return self._auth_required_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=True,
                source="auth_probe",
            )
        except BitBrowserRateLimitedError as exc:
            if not self._rate_limit_is_active():
                self._trip_rate_limit_circuit(
                    path="/browser/list",
                    provider_message=str(exc.details.get("provider_message") or ""),
                )
            return self._rate_limited_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=True,
                source="provider_probe",
            )
        except UpstreamUnavailableError:
            return self._blocked_listing(
                started_at=started_at,
                cache_entry=cache_entry,
                reachable=True,
                source="provider_probe",
            )

        self._confirm_auth_recovery()
        self._mark_connection_success()
        return self._recovery_listing(
            started_at=started_at,
            cache_entry=cache_entry,
        )

    def _list_all_windows_uncached(self, *, name: str = "") -> dict[str, Any]:
        page_size = 100
        page = 0
        windows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        provider_success = True
        while True:
            list_payload = self._post(
                "/browser/list",
                {"page": page, "pageSize": page_size, "name": name.strip(), "sort": "asc"},
            )
            self._require_provider_success(list_payload, "/browser/list")
            if not self._has_profile_list(list_payload):
                raise UpstreamUnavailableError(
                    "已连接到本机端口，但无法识别 BitBrowser 窗口列表",
                    details={"path": "/browser/list"},
                )
            profiles, total = self._extract_profile_list(list_payload)
            provider_success = provider_success and self._provider_success(list_payload)
            page_windows = [item for item in profiles if isinstance(item, dict)]
            new_on_page = 0
            for item in page_windows:
                profile_id = self._first(item, "id", "browserId", "browser_id")
                if profile_id and profile_id not in seen_ids:
                    seen_ids.add(profile_id)
                    windows.append(item)
                    new_on_page += 1
            # A short page is the normal terminal condition when `total` is absent.
            # A repeated page is treated as terminal to avoid looping on a provider
            # that ignores its page argument.
            if (
                len(page_windows) < page_size
                or new_on_page == 0
                or (isinstance(total, int) and total > 0 and len(seen_ids) >= total)
            ):
                break
            if self.inventory_page_delay_seconds > 0:
                time.sleep(self.inventory_page_delay_seconds)
            page += 1
        pids_payload = self._post("/browser/pids/all", {})
        self._require_provider_success(pids_payload, "/browser/pids/all")
        open_ids = self._extract_open_ids(pids_payload)
        normalized_windows = []
        for profile in windows:
            normalized = self._normalize_profile(profile, open_ids)
            if normalized["id"]:
                normalized_windows.append(normalized)
        return {
            "total": len(normalized_windows),
            "windows": normalized_windows,
            "provider_success": provider_success,
        }

    def _connection_endpoint_from_data(
        self,
        data: Any,
        profile_id: str,
        *,
        allow_unscoped_port: bool = False,
    ) -> dict[str, str | None]:
        item = self._find_profile_data(data, profile_id)
        debug_port = self._extract_debug_port(
            data,
            profile_id,
            allow_unscoped=allow_unscoped_port,
        )
        return {
            "ws": self._first(item, "ws", "wsUrl", "webSocketDebuggerUrl"),
            "http": self._first(item, "http", "httpUrl", "debuggerAddress")
            or (
                f"http://127.0.0.1:{debug_port}"
                if debug_port is not None
                else None
            ),
        }

    @staticmethod
    def _connection_endpoint_available(endpoint: dict[str, str | None]) -> bool:
        return bool(endpoint.get("ws") or endpoint.get("http"))

    def _clear_connection_cache_locked(self) -> None:
        self._shared_state.connection_ports_cache = None
        self._shared_state.connection_endpoint_cache.clear()
        self._shared_state.connection_open_attempted_at.clear()

    def _record_connection_failure_locked(
        self, error: UpstreamUnavailableError
    ) -> None:
        # Never keep a possibly dead CDP address after the Local API that supplied it
        # has failed. The short negative cache makes every concurrent worker observe
        # the same failure instead of immediately starting another ports/open wave.
        self._clear_connection_cache_locked()
        self._shared_state.connection_failure = _CachedFailure(
            time.monotonic(), error.message, copy.deepcopy(error.details)
        )

    def _invalidate_connection_cache(self) -> None:
        with self._shared_state.connection_ports_lock:
            self._clear_connection_cache_locked()
            self._shared_state.connection_failure = None

    def _connection_ports_data_locked(self) -> Any:
        """Return one short-lived, process-shared /browser/ports snapshot.

        The caller owns ``connection_ports_lock``. Keeping the provider request under
        that lock makes the cache fill single-flight even when a large worker pool is
        attaching at once.
        """
        try:
            # Cached endpoints must never bypass a provider auth/rate-limit circuit.
            self._raise_if_provider_circuit_open("/browser/ports")
        except (BitBrowserAuthRequiredError, BitBrowserRateLimitedError):
            self._clear_connection_cache_locked()
            self._shared_state.connection_failure = None
            raise

        now = time.monotonic()
        failure = self._shared_state.connection_failure
        failure_ttl = max(0.0, self.connection_failure_cache_seconds)
        if (
            failure is not None
            and failure_ttl > 0
            and now - failure.created_at
            <= failure_ttl
        ):
            raise failure.as_error()
        if failure is not None:
            self._shared_state.connection_failure = None

        cached = self._shared_state.connection_ports_cache
        if (
            cached is not None
            and now - cached[0] <= max(0.0, self.connection_ports_cache_seconds)
        ):
            return cached[1]

        try:
            payload = self._post("/browser/ports", {})
            self._require_provider_success(payload, "/browser/ports")
        except (BitBrowserAuthRequiredError, BitBrowserRateLimitedError):
            self._clear_connection_cache_locked()
            self._shared_state.connection_failure = None
            raise
        except UpstreamUnavailableError as exc:
            self._record_connection_failure_locked(exc)
            raise

        data = copy.deepcopy(self._provider_data(payload))
        completed_at = time.monotonic()
        self._shared_state.connection_ports_cache = (completed_at, data)
        self._shared_state.connection_failure = None
        # Entries populated by an earlier ports/open response obey the same short
        # TTL. Prune them while the single-flight lock is already held.
        ttl = max(0.0, self.connection_ports_cache_seconds)
        self._shared_state.connection_endpoint_cache = {
            key: value
            for key, value in self._shared_state.connection_endpoint_cache.items()
            if completed_at - value[0] <= ttl
        }
        self._shared_state.connection_open_attempted_at = {
            key: value
            for key, value in self._shared_state.connection_open_attempted_at.items()
            if completed_at - value
            <= max(0.0, self.connection_open_cooldown_seconds)
        }
        return data

    def profile_ports(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        with self._shared_state.connection_ports_lock:
            data = self._connection_ports_data_locked()
            endpoint = self._connection_endpoint_from_data(data, profile_id)
        # Only report availability to UI. Debug addresses are reserved for a local worker.
        return {
            "profile_id": profile_id,
            "endpoint_available": self._connection_endpoint_available(endpoint),
            "provider_success": True,
        }

    def close_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        payload = self._post("/browser/close", {"id": profile_id})
        self._require_provider_success(payload, "/browser/close")
        self._invalidate_connection_cache()
        self._invalidate_status_caches(profile_id=profile_id, is_open=False)
        return {
            "profile_id": profile_id,
            "requested": True,
            "provider_success": self._provider_success(payload),
            "provider_message": self._first(payload, "msg", "message"),
        }

    def open_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        payload = self._post(
            "/browser/open",
            {
                "id": profile_id,
                "args": [],
                "queue": True,
                "ignoreDefaultUrls": True,
                "newPageUrl": "https://www.instagram.com/",
            },
        )
        self._require_provider_success(payload, "/browser/open")
        self._invalidate_connection_cache()
        self._invalidate_status_caches(profile_id=profile_id, is_open=True)
        data = self._provider_data(payload)
        return {
            "profile_id": profile_id,
            "requested": True,
            "provider_success": self._provider_success(payload),
            "endpoint_available": self._contains_endpoint(data, profile_id),
            "provider_message": self._first(payload, "msg", "message"),
        }

    def _invalidate_status_caches(
        self,
        *,
        profile_id: str | None = None,
        is_open: bool | None = None,
    ) -> None:
        # Never discard the expensive full inventory after open/close. Apart from
        # violating the hard refresh floor, bulk opening several windows would force
        # a new seven-page list cycle. Safely patch the known process state instead.
        if profile_id is None or is_open is None:
            return
        with self._shared_state.inventory_lock:
            for _, listing in self._shared_state.profiles_cache.values():
                for window in listing.get("windows", []):
                    if window.get("id") == profile_id:
                        window["is_open"] = is_open

    def _get_cached(
        self,
        entry: tuple[float, dict[str, Any]] | None,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        if entry is None or ttl_seconds <= 0:
            return None
        created_at, value = entry
        if time.monotonic() - created_at > ttl_seconds:
            return None
        return copy.deepcopy(value)

    def connection_endpoint(
        self, profile_id: str, *, open_if_needed: bool = False
    ) -> dict[str, str | None]:
        """Return an existing CDP endpoint before considering a profile open.

        ``/browser/ports`` describes all open profiles. Reading it once for a whole
        attach wave is both cheaper and safer than sending ``/browser/open`` for every
        reconnect. The lock also makes a missing-profile open single-flight: another
        caller for the same profile observes the cached open result/attempt instead of
        issuing a duplicate provider command.
        """
        profile_id = validate_profile_id(profile_id)
        with self._shared_state.connection_ports_lock:
            try:
                # Do this even when a positive endpoint is cached. Authentication and
                # provider-frequency circuits are authoritative process-wide.
                self._raise_if_provider_circuit_open("/browser/ports")
            except (BitBrowserAuthRequiredError, BitBrowserRateLimitedError):
                self._clear_connection_cache_locked()
                self._shared_state.connection_failure = None
                raise

            now = time.monotonic()
            cached_endpoint = self._shared_state.connection_endpoint_cache.get(
                profile_id
            )
            if (
                cached_endpoint is not None
                and now - cached_endpoint[0]
                <= max(0.0, self.connection_ports_cache_seconds)
            ):
                return copy.deepcopy(cached_endpoint[1])

            data = self._connection_ports_data_locked()
            endpoint = self._connection_endpoint_from_data(data, profile_id)
            if self._connection_endpoint_available(endpoint):
                self._shared_state.connection_endpoint_cache[profile_id] = (
                    time.monotonic(),
                    copy.deepcopy(endpoint),
                )
                return endpoint
            if not open_if_needed:
                return endpoint

            last_open = self._shared_state.connection_open_attempted_at.get(profile_id)
            if (
                last_open is not None
                and time.monotonic() - last_open
                <= max(0.0, self.connection_open_cooldown_seconds)
            ):
                # An earlier single-flight caller already asked BitBrowser to open
                # this profile. If its response did not yet contain a CDP endpoint,
                # let the short ports TTL expire rather than creating an open storm.
                return {"ws": None, "http": None}

            self._shared_state.connection_open_attempted_at[profile_id] = (
                time.monotonic()
            )
            try:
                payload = self._post(
                    "/browser/open",
                    {
                        "id": profile_id,
                        "args": [],
                        "queue": True,
                        "ignoreDefaultUrls": True,
                        "newPageUrl": "https://www.instagram.com/",
                    },
                )
                self._require_provider_success(payload, "/browser/open")
            except (BitBrowserAuthRequiredError, BitBrowserRateLimitedError):
                self._clear_connection_cache_locked()
                self._shared_state.connection_failure = None
                raise
            except UpstreamUnavailableError as exc:
                self._record_connection_failure_locked(exc)
                raise

            endpoint = self._connection_endpoint_from_data(
                self._provider_data(payload),
                profile_id,
                allow_unscoped_port=True,
            )
            completed_at = time.monotonic()
            self._shared_state.connection_open_attempted_at[profile_id] = completed_at
            if self._connection_endpoint_available(endpoint):
                self._shared_state.connection_endpoint_cache[profile_id] = (
                    completed_at,
                    copy.deepcopy(endpoint),
                )
            self._shared_state.connection_failure = None
            self._invalidate_status_caches(profile_id=profile_id, is_open=True)
            return endpoint

    @staticmethod
    def _first(value: Any, *keys: str) -> Any:
        if not isinstance(value, dict):
            return None
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        return None

    @classmethod
    def _auth_required_message(cls, payload: dict[str, Any]) -> str | None:
        """Extract only provider error text, never profile data or endpoints."""
        pending: list[Any] = [payload]
        visited = 0
        while pending and visited < 64:
            value = pending.pop()
            visited += 1
            if not isinstance(value, dict):
                continue
            for key, child in value.items():
                normalized_key = str(key).replace("_", "").casefold()
                if normalized_key in cls._AUTH_MESSAGE_KEYS and isinstance(child, str):
                    normalized = " ".join(child.strip().casefold().split())
                    if any(marker in normalized for marker in cls._AUTH_MESSAGE_MARKERS):
                        return child
                elif normalized_key in {"data", "error", "result"} and isinstance(child, dict):
                    pending.append(child)
        return None

    @classmethod
    def _rate_limit_message(cls, payload: dict[str, Any]) -> str | None:
        pending: list[Any] = [payload]
        visited = 0
        while pending and visited < 64:
            value = pending.pop()
            visited += 1
            if not isinstance(value, dict):
                continue
            for key, child in value.items():
                normalized_key = str(key).replace("_", "").casefold()
                if normalized_key in cls._AUTH_MESSAGE_KEYS and isinstance(child, str):
                    normalized = " ".join(child.strip().casefold().split())
                    if any(marker in normalized for marker in cls._RATE_LIMIT_MESSAGE_MARKERS):
                        return child
                elif normalized_key in {"data", "error", "result"} and isinstance(child, dict):
                    pending.append(child)
        return None

    @staticmethod
    def _safe_provider_message(message: str | None) -> str | None:
        if not isinstance(message, str):
            return None
        value = " ".join(message.strip().split())
        if not value:
            return None
        # Provider login errors are useful diagnostics, but cap them so unexpected
        # Local API content can never turn into a large renderer/error payload.
        return value[:160]

    @classmethod
    def _provider_success(cls, payload: dict[str, Any]) -> bool:
        explicit = cls._first(payload, "success", "ok")
        if explicit is not None:
            if isinstance(explicit, str):
                return explicit.strip().lower() in {"1", "true", "yes", "ok", "success"}
            return bool(explicit)
        status = cls._first(payload, "status")
        if isinstance(status, str):
            normalized = status.strip().lower()
            if normalized in {"ok", "success", "succeeded"}:
                return True
            if normalized in {"error", "fail", "failed", "failure"}:
                return False
        code = cls._first(payload, "code", "statusCode")
        return code in (None, 0, 200, "0", "200")

    @classmethod
    def _require_provider_success(cls, payload: dict[str, Any], path: str) -> None:
        if cls._provider_success(payload):
            return
        message = cls._first(payload, "msg", "message", "error")
        auth_message = cls._auth_required_message(payload)
        if auth_message is not None:
            raise BitBrowserAuthRequiredError(
                "BitBrowser 已退出登录，请重新登录后继续",
                details={
                    "path": path,
                    "state": "auth_required",
                    "reason": "bitbrowser_login_required",
                    "hint": "请重新登录 BitBrowser，然后在采集器中点击继续或立即检测",
                    "provider_message": cls._safe_provider_message(auth_message),
                },
            )
        rate_limit_message = cls._rate_limit_message(payload)
        if rate_limit_message is not None:
            raise BitBrowserRateLimitedError(
                "BitBrowser 已限制频繁请求，请等待后再检测",
                details={
                    "path": path,
                    "state": "rate_limited",
                    "reason": "bitbrowser_request_frequency_limit",
                    "hint": "请保持采集器等待，不要反复刷新 BitBrowser 窗口列表",
                    "provider_message": cls._safe_provider_message(rate_limit_message),
                },
            )
        raise UpstreamUnavailableError(
            "BitBrowser Local API 拒绝了连接请求" + (f"：{message}" if isinstance(message, str) else ""),
            details={"path": path},
        )

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
        if not isinstance(data, dict):
            return False
        return any(isinstance(data.get(key), list) for key in ("list", "items", "records", "rows", "browserList"))

    @classmethod
    def _extract_profile_list(cls, payload: dict[str, Any]) -> tuple[list[Any], int | None]:
        data = cls._provider_data(payload)
        total: int | None = None
        if isinstance(data, list):
            return data, len(data)
        if not isinstance(data, dict):
            return [], None
        candidates = ("list", "items", "records", "rows", "browserList")
        profiles: list[Any] = []
        for key in candidates:
            if isinstance(data.get(key), list):
                profiles = data[key]
                break
        raw_total = cls._first(data, "total", "count", "totalCount", "totalNum")
        if isinstance(raw_total, int) and not isinstance(raw_total, bool):
            total = raw_total
        return profiles, total

    @classmethod
    def _extract_open_ids(cls, payload: dict[str, Any]) -> set[str]:
        data = cls._provider_data(payload)
        found: set[str] = set()

        def visit(value: Any, key_hint: str | None = None) -> None:
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
                        visit(child, str(key))
                    elif key_hint and str(child).isdigit() and int(child) > 0:
                        found.add(key_hint)
                # Some versions return {profile_id: pid}.
                for key, child in value.items():
                    if isinstance(child, int) and child > 0:
                        found.add(str(key))

        visit(data)
        return found

    @classmethod
    def _normalize_profile(cls, profile: dict[str, Any], open_ids: set[str]) -> dict[str, Any]:
        profile_id = cls._first(profile, "id", "browserId", "profileId", "browser_id")
        profile_id_text = str(profile_id) if profile_id is not None else ""
        name = cls._first(profile, "name", "browserName", "profileName", "remark")
        group = cls._first(profile, "groupName", "group", "group_name")
        return {
            "id": profile_id_text,
            "name": str(name) if name is not None else profile_id_text,
            "group": str(group) if group is not None else None,
            "is_open": profile_id_text in open_ids,
        }

    @classmethod
    def _find_profile_data(cls, data: Any, profile_id: str) -> dict[str, Any]:
        if isinstance(data, dict):
            direct_id = cls._first(data, "id", "browserId", "profileId", "browser_id")
            if direct_id is None or str(direct_id) == profile_id:
                if cls._first(data, "ws", "wsUrl", "webSocketDebuggerUrl", "http", "httpUrl", "debuggerAddress"):
                    return data
            if profile_id in data and isinstance(data[profile_id], dict):
                return data[profile_id]
            for value in data.values():
                found = cls._find_profile_data(value, profile_id)
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = cls._find_profile_data(value, profile_id)
                if found:
                    return found
        return {}

    @classmethod
    def _contains_endpoint(cls, data: Any, profile_id: str) -> bool:
        item = cls._find_profile_data(data, profile_id)
        return bool(cls._first(item, "ws", "wsUrl", "webSocketDebuggerUrl", "http", "httpUrl", "debuggerAddress"))

    @classmethod
    def _extract_debug_port(
        cls,
        data: Any,
        profile_id: str,
        *,
        allow_unscoped: bool = False,
    ) -> int | None:
        """Accept both `{profile_id: port}` and object-style port responses."""
        if isinstance(data, dict):
            candidate = data.get(profile_id)
            if isinstance(candidate, dict):
                candidate = cls._first(candidate, "port", "debugPort", "debug_port")
            if isinstance(candidate, (str, int)) and str(candidate).isdigit():
                port = int(candidate)
                return port if 1 <= port <= 65535 else None
            direct_id = cls._first(
                data, "id", "browserId", "profileId", "browser_id"
            )
            direct = cls._first(data, "port", "debugPort", "debug_port")
            if (
                isinstance(direct, (str, int))
                and str(direct).isdigit()
                and (
                    (direct_id is not None and str(direct_id) == profile_id)
                    or (direct_id is None and allow_unscoped)
                )
            ):
                port = int(direct)
                return port if 1 <= port <= 65535 else None
            # Some API builds return one `{id: "64170"}` mapping where `id` is the
            # requested browser id placeholder rather than a literal field name. This
            # is safe only for /browser/open, whose request already identifies the
            # profile. A global /browser/ports snapshot must never lend one window's
            # sole scalar port to a different, actually closed window.
            scalar_values = [value for value in data.values() if isinstance(value, (str, int))]
            if (
                allow_unscoped
                and len(data) == 1
                and len(scalar_values) == 1
                and str(scalar_values[0]).isdigit()
            ):
                port = int(scalar_values[0])
                return port if 1 <= port <= 65535 else None
        return None
