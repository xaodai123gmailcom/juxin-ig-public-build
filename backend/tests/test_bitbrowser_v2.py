from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import threading
import time
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.bitbrowser_v2 import (
    BitBrowserClientV2,
    UrllibBitBrowserTransport,
    _TransportFailure,
)
from app.errors import UpstreamUnavailableError
from app.playwright_worker import PlaywrightWorker


Handler = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class ScenarioTransport:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []
        self._lock = threading.Lock()

    def post(
        self,
        base_url: str,
        path: str,
        payload: dict[str, Any],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del api_key, timeout_seconds
        with self._lock:
            self.calls.append(
                (base_url, path, dict(payload), threading.current_thread().name)
            )
        return self.handler(base_url, path, payload)

    def count(self, path: str) -> int:
        with self._lock:
            return sum(call_path == path for _, call_path, _, _ in self.calls)


def ok(data: Any = None, *, message: str = "ok") -> dict[str, Any]:
    return {"success": True, "msg": message, "data": data}


def profile_list(*profiles: dict[str, Any]) -> dict[str, Any]:
    return ok({"list": list(profiles), "total": len(profiles)})


def normal_handler(profiles: tuple[dict[str, Any], ...]) -> Handler:
    def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/health":
            return ok()
        if path == "/browser/list":
            return profile_list(*profiles)
        if path == "/browser/pids/all":
            return ok([])
        if path == "/browser/ports":
            return ok({})
        if path in {"/browser/open", "/browser/close"}:
            return ok()
        raise AssertionError(path)

    return handler


def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


class BitBrowserV2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clients: list[BitBrowserClientV2] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.shutdown()

    def client(
        self, transport: ScenarioTransport, **overrides: Any
    ) -> BitBrowserClientV2:
        settings: dict[str, Any] = {
            "transport": transport,
            "base_url": "http://127.0.0.1:54345",
            "port_candidates": (54345, 54346),
            "timeout_seconds": 0.1,
            "health_interval_seconds": 60.0,
            "inventory_cache_seconds": 0.0,
            "open_timeout_seconds": 0.5,
            "endpoint_poll_seconds": 0.01,
            "command_timeout_seconds": 2.0,
            "shutdown_timeout_seconds": 1.0,
        }
        settings.update(overrides)
        client = BitBrowserClientV2(**settings)
        self.clients.append(client)
        return client

    def test_auto_discovers_second_port_and_normalizes_metadata(self) -> None:
        expected = {
            "id": "profile-7",
            "name": "Window 07",
            "groupName": "IG",
            "serialNumber": 7,
        }

        def handler(base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if base_url.endswith(":54345"):
                raise _TransportFailure("offline", "nothing is listening")
            self.assertTrue(base_url.endswith(":54346"))
            if path == "/health":
                return {"code": 0, "data": {}}
            if path == "/browser/list":
                return {"code": 0, "data": {"list": [expected], "total": 1}}
            if path == "/browser/pids/all":
                return {"code": 0, "data": []}
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        listing = client.list_all_windows(force=True)
        window = listing["windows"][0]
        self.assertEqual("http://127.0.0.1:54346", client.base_url)
        self.assertEqual("ready", listing["connection"]["phase"])
        self.assertEqual(("profile-7", "IG", "7"), (
            window["id"], window["group"], window["serial_number"]
        ))

    def test_offline_refresh_keeps_last_inventory(self) -> None:
        transport = ScenarioTransport(
            normal_handler(({"id": "keep-me", "name": "Persistent window"},))
        )
        client = self.client(transport)
        client.list_all_windows(force=True)

        def offline(_base_url: str, _path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            raise _TransportFailure("offline", "BitBrowser was closed")

        transport.handler = offline
        stale = client.list_all_windows(force=True)
        self.assertEqual(["keep-me"], [item["id"] for item in stale["windows"]])
        self.assertTrue(stale["stale"])
        self.assertEqual("offline", stale["connection"]["phase"])

    def test_inventory_hard_floor_survives_connection_generation_changes(self) -> None:
        now = {"value": 100.0}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "hard-floor"})
            if path == "/browser/pids/all":
                raise _TransportFailure("offline", "pids unavailable")
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            clock=lambda: now["value"],
            inventory_cache_seconds=60.0,
        )
        first = client.list_all_windows(force=True)
        self.assertTrue(first["stale"])
        self.assertEqual(1, transport.count("/browser/list"))

        now["value"] = 103.0
        self.assertEqual("ready", client.health()["phase"])
        cached = client.list_all_windows(force=True)
        self.assertTrue(cached["stale"])
        self.assertEqual(1, transport.count("/browser/list"))

        now["value"] = 161.0
        client.list_all_windows(force=True)
        self.assertEqual(2, transport.count("/browser/list"))

    def test_local_api_redirect_does_not_forward_api_key(self) -> None:
        leaked_headers: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                leaked_headers.append(self.headers.get("x-api-key"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":true}')

            def do_POST(self) -> None:  # noqa: N802
                self._record()

            def do_GET(self) -> None:  # noqa: N802
                self._record()

            def log_message(self, *_args: Any) -> None:
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        sink_port = int(sink.server_address[1])

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink_port}/capture")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            transport = UrllibBitBrowserTransport()
            with self.assertRaises(_TransportFailure):
                transport.post(
                    f"http://127.0.0.1:{redirect.server_address[1]}",
                    "/health",
                    {},
                    api_key="do-not-leak",
                    timeout_seconds=1.0,
                )
            self.assertEqual([], leaked_headers)
        finally:
            redirect.shutdown()
            sink.shutdown()
            redirect.server_close()
            sink.server_close()
            redirect_thread.join(timeout=1.0)
            sink_thread.join(timeout=1.0)

    def test_http_cdp_resolution_rejects_redirect_and_accepts_local_ws(self) -> None:
        redirected_hits: list[str] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                redirected_hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9/remote"}')

            def log_message(self, *_args: Any) -> None:
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        target_port = int(target.server_address[1])

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/json/version")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()

        class LocalVersionHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("Upgrade", "").casefold() == "websocket":
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (
                                key
                                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).encode("ascii")
                        ).digest()
                    ).decode("ascii")
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    return
                port = int(self.server.server_address[1])
                body = json.dumps(
                    {
                        "webSocketDebuggerUrl": (
                            f"ws://127.0.0.1:{port}/devtools/browser/local-browser"
                        )
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        local = ThreadingHTTPServer(("127.0.0.1", 0), LocalVersionHandler)
        local_thread = threading.Thread(target=local.serve_forever, daemon=True)
        local_thread.start()
        try:
            # This probe uses a real loopback HTTP server rather than the in-memory
            # transport.  Give the CI scheduler enough time to dispatch its worker
            # thread when the complete suite is running under load.
            client = self.client(
                ScenarioTransport(normal_handler(())), timeout_seconds=1.0
            )
            with self.assertRaises(UpstreamUnavailableError):
                client.resolve_cdp_endpoint(
                    f"http://127.0.0.1:{redirect.server_address[1]}"
                )
            self.assertEqual([], redirected_hits)
            local_port = int(local.server_address[1])
            self.assertEqual(
                f"ws://127.0.0.1:{local_port}/devtools/browser/local-browser",
                client.resolve_cdp_endpoint(f"http://127.0.0.1:{local_port}"),
            )
            with self.assertRaises(UpstreamUnavailableError):
                client.resolve_cdp_endpoint(
                    f"ws://127.0.0.1:{local_port}/devtools/page/not-a-browser"
                )
            with self.assertRaises(UpstreamUnavailableError):
                client.resolve_cdp_endpoint(
                    f"ws://127.0.0.1:{local_port}/devtools/browser/local-browser?next=remote"
                )
            self.assertEqual(
                f"ws://127.0.0.1:{local_port}/devtools/browser/local-browser",
                client.resolve_cdp_endpoint(
                    f"ws://127.0.0.1:{local_port}/devtools/browser/local-browser"
                ),
            )
        finally:
            redirect.shutdown()
            target.shutdown()
            local.shutdown()
            redirect.server_close()
            target.server_close()
            local.server_close()
            redirect_thread.join(timeout=1.0)
            target_thread.join(timeout=1.0)
            local_thread.join(timeout=1.0)

    def test_auth_required_keeps_inventory_and_stops_background_probes(self) -> None:
        transport = ScenarioTransport(
            normal_handler(({"id": "selected", "name": "Selected window"},))
        )
        client = self.client(transport)
        client.list_all_windows(force=True)

        def signed_out(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            return (
                {"success": False, "msg": "Please login to BitBrowser"}
                if path == "/browser/list"
                else ok()
            )

        transport.handler = signed_out
        stale = client.list_all_windows(force=True)
        self.assertEqual(["selected"], [item["id"] for item in stale["windows"]])
        self.assertEqual("auth_required", stale["connection"]["phase"])
        call_count = len(transport.calls)
        client.health()
        self.assertEqual(call_count, len(transport.calls))

    def test_confirm_login_requires_authenticated_profile_list(self) -> None:
        signed_in = {"value": True}
        now = {"value": 100.0}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return (
                    profile_list({"id": "login-check"})
                    if signed_in["value"]
                    else {"success": False, "msg": "Login out!"}
                )
            if path == "/browser/pids/all":
                return ok([])
            raise AssertionError(path)

        client = self.client(
            ScenarioTransport(handler),
            clock=lambda: now["value"],
            inventory_cache_seconds=60.0,
            login_verification_interval_seconds=5.0,
        )
        client.list_all_windows(force=True)
        signed_in["value"] = False
        # Expire the test cache explicitly so the simulated logout is observed.
        client._inventory_attempt_at = None
        stale = client.list_all_windows(force=True)
        self.assertEqual("auth_required", stale["connection"]["phase"])

        still_signed_out = client.confirm_login()
        self.assertEqual(
            "auth_required", still_signed_out["connection"]["phase"]
        )
        self.assertTrue(still_signed_out["stale"])
        verification_calls = client.transport.count("/browser/list")  # type: ignore[union-attr]
        now["value"] = 101.0
        throttled = client.confirm_login()
        self.assertEqual("auth_required", throttled["connection"]["phase"])
        self.assertEqual(
            verification_calls,
            client.transport.count("/browser/list"),  # type: ignore[union-attr]
        )

        signed_in["value"] = True
        now["value"] = 106.0
        calls_before_recovery = client.transport.count("/browser/list")  # type: ignore[union-attr]
        recovered = client.confirm_login()
        self.assertEqual("ready", recovered["connection"]["phase"])
        self.assertEqual(["login-check"], [item["id"] for item in recovered["windows"]])
        self.assertFalse(recovered["stale"])
        self.assertEqual(
            calls_before_recovery + 1,
            client.transport.count("/browser/list"),  # type: ignore[union-attr]
        )
        recovery_payload = [
            payload
            for _, path, payload, _ in client.transport.calls  # type: ignore[union-attr]
            if path == "/browser/list"
        ][-1]
        self.assertEqual(100, recovery_payload["pageSize"])

    def test_ready_confirm_login_cannot_bypass_inventory_floor(self) -> None:
        transport = ScenarioTransport(
            normal_handler(({"id": "steady-window"},))
        )
        client = self.client(
            transport,
            inventory_cache_seconds=60.0,
        )
        client.list_all_windows(force=True)
        list_calls = transport.count("/browser/list")

        first = client.confirm_login()
        second = client.confirm_login()
        self.assertEqual(list_calls, transport.count("/browser/list"))
        self.assertEqual("ready", first["connection"]["phase"])
        self.assertEqual("ready", second["connection"]["phase"])

    def test_nonpositive_provider_total_does_not_truncate_full_first_page(self) -> None:
        rows = [{"id": f"window-{index:03d}"} for index in range(150)]

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                page = int(payload["page"])
                start = page * 100
                return ok({"list": rows[start : start + 100], "total": 0})
            if path == "/browser/pids/all":
                return ok([])
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        listing = self.client(transport).list_all_windows(force=True)
        self.assertEqual(150, listing["total"])
        self.assertEqual(2, transport.count("/browser/list"))

    def test_pids_failure_does_not_discard_profile_list(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "a", "isOpen": True}, {"id": "b"})
            if path == "/browser/pids/all":
                raise _TransportFailure("offline", "optional endpoint failed")
            raise AssertionError(path)

        listing = self.client(ScenarioTransport(handler)).list_all_windows(force=True)
        self.assertEqual(["a", "b"], [item["id"] for item in listing["windows"]])
        self.assertTrue(listing["windows"][0]["is_open"])
        self.assertTrue(listing["stale"])
        self.assertEqual("offline", listing["connection"]["phase"])

    def test_open_waits_until_ports_returns_cdp_endpoint(self) -> None:
        ports_calls = 0

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal ports_calls
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "slow"})
            if path == "/browser/ports":
                ports_calls += 1
                return ok({"slow": {"port": 9227}} if ports_calls >= 3 else {})
            if path == "/browser/open":
                return ok({"id": "slow"}, message="open queued")
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        result = client.open_profile("slow")
        self.assertTrue(result["endpoint_available"])
        self.assertGreaterEqual(ports_calls, 3)
        self.assertEqual(
            "http://127.0.0.1:9227", client.connection_endpoint("slow")["http"]
        )

    def test_close_during_late_open_triggers_compensating_close(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "race"})
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=1.0))
                return ok({"id": "race", "port": 9333})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        outcomes: dict[str, Any] = {}

        def open_worker() -> None:
            try:
                client.open_profile("race")
            except BaseException as exc:
                outcomes["open_error"] = exc

        def close_worker() -> None:
            outcomes["close"] = client.close_profile("race")

        opening = threading.Thread(target=open_worker)
        opening.start()
        self.assertTrue(open_entered.wait(timeout=1.0))
        closing = threading.Thread(target=close_worker)
        closing.start()
        wait_until(lambda: client._window_generation.get("race") == 1)
        release_open.set()
        opening.join(timeout=2.0)
        closing.join(timeout=2.0)
        self.assertIsInstance(outcomes.get("open_error"), UpstreamUnavailableError)
        self.assertEqual("closed", outcomes["close"]["window_state"])
        self.assertEqual(1, transport.count("/browser/close"))

    def test_close_while_ports_is_blocked_never_returns_stale_endpoint(self) -> None:
        ports_entered = threading.Event()
        release_ports = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "blocked"})
            if path == "/browser/ports":
                ports_entered.set()
                self.assertTrue(release_ports.wait(timeout=1.0))
                return ok({"blocked": 9222})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        result: dict[str, Any] = {}

        def connect() -> None:
            try:
                result["endpoint"] = client.connection_endpoint("blocked")
            except BaseException as exc:
                result["error"] = exc

        connector = threading.Thread(target=connect)
        connector.start()
        self.assertTrue(ports_entered.wait(timeout=1.0))
        closer = threading.Thread(target=lambda: client.close_profile("blocked"))
        closer.start()
        wait_until(lambda: client._window_generation.get("blocked") == 1)
        release_ports.set()
        connector.join(timeout=2.0)
        closer.join(timeout=2.0)
        self.assertNotIn("endpoint", result)
        self.assertIsInstance(result.get("error"), UpstreamUnavailableError)

    def test_remote_cdp_endpoint_is_rejected(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "remote"})
            if path == "/browser/ports":
                return ok({"remote": {"http": "http://192.0.2.10:9222"}})
            raise AssertionError(path)

        with self.assertRaises(UpstreamUnavailableError):
            self.client(ScenarioTransport(handler)).connection_endpoint("remote")

    def test_localhost_cdp_is_canonicalized_before_network_io(self) -> None:
        client = self.client(ScenarioTransport(normal_handler(())))
        self.assertEqual(
            "ws://127.0.0.1:9222/devtools/browser/local-test",
            client._safe_cdp_endpoint(
                "ws://localhost:9222/devtools/browser/local-test",
                allowed_schemes={"ws"},
            ),
        )

    def test_cooldown_is_not_bypassed_by_business_commands(self) -> None:
        rate_limit_ports = False

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "cool"})
            if path == "/browser/ports":
                if rate_limit_ports:
                    return {"success": False, "msg": "requests too frequent"}
                return ok({})
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        client.health()
        rate_limit_ports = True
        with self.assertRaises(UpstreamUnavailableError):
            client.connection_endpoint("cool")
        self.assertEqual("cooldown", client.status()["phase"])

        call_count = len(transport.calls)
        with self.assertRaises(UpstreamUnavailableError):
            client.connection_endpoint("cool")
        self.assertEqual(call_count, len(transport.calls))

    def test_profile_rejection_does_not_mark_all_windows_offline(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "missing"})
            if path == "/browser/ports":
                return {"success": False, "msg": "browser not found"}
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        client.health()
        with self.assertRaises(UpstreamUnavailableError):
            client.connection_endpoint("missing")
        self.assertEqual("ready", client.status()["phase"])

    def test_queued_open_timeout_has_no_side_effect_or_stuck_state(self) -> None:
        ports_entered = threading.Event()
        release_ports = threading.Event()

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "hold"}, {"id": "queued"})
            if path == "/browser/ports":
                ports_entered.set()
                self.assertTrue(release_ports.wait(timeout=1.0))
                return ok({})
            if path == "/browser/open":
                raise AssertionError(f"cancelled queued open reached provider: {payload}")
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            timeout_seconds=0.01,
            open_timeout_seconds=0.01,
            command_timeout_seconds=0.02,
        )
        client.health()
        holder = threading.Thread(
            target=lambda: self.assertRaises(
                UpstreamUnavailableError,
                client.connection_endpoint,
                "hold",
            )
        )
        holder.start()
        self.assertTrue(ports_entered.wait(timeout=1.0))

        with self.assertRaises(UpstreamUnavailableError):
            client.open_profile("queued")
        self.assertEqual(0, client._window_generation.get("queued", 0))
        self.assertNotEqual("closing", client._window_phase.get("queued"))
        release_ports.set()
        holder.join(timeout=1.0)
        wait_until(lambda: client._commands.empty())
        self.assertEqual(0, transport.count("/browser/open"))

    def test_caller_timeout_cancels_late_open_and_compensates(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "timeout"})
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=1.0))
                return ok({"id": "timeout", "port": 9444})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            timeout_seconds=0.01,
            open_timeout_seconds=0.01,
            command_timeout_seconds=0.02,
        )
        outcome: dict[str, Any] = {}

        def caller() -> None:
            try:
                client.open_profile("timeout")
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=caller)
        thread.start()
        self.assertTrue(open_entered.wait(timeout=1.0))
        thread.join(timeout=0.2)
        self.assertIsInstance(outcome.get("error"), UpstreamUnavailableError)
        release_open.set()
        wait_until(lambda: transport.count("/browser/close") == 1)
        self.assertEqual("closed", client._window_phase["timeout"])

    def test_open_wait_does_not_block_another_window_close(self) -> None:
        a_ready = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "a"}, {"id": "b"})
            if path == "/browser/ports":
                return ok({"a": 9333} if a_ready.is_set() else {})
            if path in {"/browser/open", "/browser/close"}:
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, open_timeout_seconds=2.5)
        result: dict[str, Any] = {}

        opening = threading.Thread(
            target=lambda: result.update(endpoint=client.connection_endpoint("a", open_if_needed=True))
        )
        opening.start()
        wait_until(lambda: transport.count("/browser/open") == 1)

        started = time.monotonic()
        closed = client.close_profile("b")
        elapsed = time.monotonic() - started
        self.assertEqual("closed", closed["window_state"])
        self.assertLess(elapsed, 0.3)

        a_ready.set()
        opening.join(timeout=3.0)
        self.assertFalse(opening.is_alive())
        self.assertEqual("http://127.0.0.1:9333", result["endpoint"]["http"])

    def test_many_open_waiters_share_ports_snapshot(self) -> None:
        publish_ports = threading.Event()
        requested: set[str] = set()
        requested_lock = threading.Lock()
        profile_ids = [f"window-{index}" for index in range(30)]

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(*( {"id": item} for item in profile_ids ))
            if path == "/browser/ports":
                if not publish_ports.is_set():
                    return ok({})
                with requested_lock:
                    return ok({item: 10_000 + index for index, item in enumerate(profile_ids) if item in requested})
            if path == "/browser/open":
                with requested_lock:
                    requested.add(str(payload["id"]))
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            open_timeout_seconds=3.0,
            command_timeout_seconds=3.0,
        )
        results: dict[str, dict[str, Any]] = {}
        threads = [
            threading.Thread(
                target=lambda profile_id=profile_id: results.update({
                    profile_id: client.connection_endpoint(profile_id, open_if_needed=True)
                })
            )
            for profile_id in profile_ids
        ]
        for thread in threads:
            thread.start()
        wait_until(lambda: transport.count("/browser/open") == len(profile_ids), timeout=2.0)
        publish_ports.set()
        for thread in threads:
            thread.join(timeout=4.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(profile_ids), len(results))
        # One initial snapshot, one post-open read per profile, then one shared
        # readiness snapshot for the whole waiter wave.
        self.assertLessEqual(transport.count("/browser/ports"), len(profile_ids) + 3)

    def test_queued_close_timeout_still_executes_after_actor_unblocks(self) -> None:
        ports_entered = threading.Event()
        release_ports = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "hold"}, {"id": "close-me"})
            if path == "/browser/ports":
                ports_entered.set()
                self.assertTrue(release_ports.wait(timeout=1.0))
                return ok({})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=0.02)
        client.health()
        def hold_actor() -> None:
            try:
                client.connection_endpoint("hold")
            except UpstreamUnavailableError:
                pass

        holder = threading.Thread(target=hold_actor)
        holder.start()
        self.assertTrue(ports_entered.wait(timeout=1.0))

        with self.assertRaises(UpstreamUnavailableError):
            client.close_profile("close-me")
        self.assertEqual("closing", client._window_phase["close-me"])
        release_ports.set()
        holder.join(timeout=1.0)
        wait_until(lambda: transport.count("/browser/close") == 1)
        self.assertEqual("closed", client._window_phase["close-me"])
        self.assertNotIn("close-me", client._pending_closes)

    def test_failed_close_is_replayed_after_explicit_reconnect(self) -> None:
        close_offline = {"value": True}
        provider_open = {"value": True}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "recover-close", "isOpen": provider_open["value"]})
            if path == "/browser/pids/all":
                return ok({"recover-close": 1234} if provider_open["value"] else {})
            if path == "/browser/close":
                if close_offline["value"]:
                    raise _TransportFailure("offline", "BitBrowser stopped")
                provider_open["value"] = False
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        client.health()
        with self.assertRaises(UpstreamUnavailableError):
            client.close_profile("recover-close")
        self.assertIn("recover-close", client._pending_closes)
        self.assertNotEqual("closed", client._window_phase["recover-close"])

        close_offline["value"] = False
        client.confirm_login()
        self.assertNotIn("recover-close", client._pending_closes)
        self.assertEqual("closed", client._window_phase["recover-close"])
        self.assertGreaterEqual(transport.count("/browser/close"), 2)

    def test_post_attach_verification_rejects_reused_port(self) -> None:
        owner = {"profile": "p"}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "p"}, {"id": "q"})
            if path == "/browser/ports":
                return ok({owner["profile"]: 9222})
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        endpoint = client.connection_endpoint("p")["http"]
        self.assertEqual("http://127.0.0.1:9222", endpoint)
        owner["profile"] = "q"
        with self.assertRaises(UpstreamUnavailableError):
            client.verify_connection_endpoint("p", endpoint or "")

    def test_connection_ticket_rejects_same_profile_same_port_after_reopen(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "p"})
            if path == "/browser/ports":
                return ok({"p": 9222})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        ticket = client.connection_endpoint("p")
        self.assertEqual(0, ticket["generation"])
        client.close_profile("p")

        with self.assertRaises(UpstreamUnavailableError) as context:
            client.verify_connection_endpoint(
                "p",
                ticket["http"] or "",
                ticket["generation"],
            )
        self.assertEqual(
            "cdp_connection_generation_changed",
            context.exception.details["reason"],
        )

    def test_destructive_action_fence_uses_original_connection_generation(self) -> None:
        calls: list[tuple[str, str, int | None]] = []

        class Verifier:
            def verify_connection_endpoint(
                self,
                profile_id: str,
                endpoint: str,
                generation: int | None,
            ) -> None:
                calls.append((profile_id, endpoint, generation))

        worker = PlaywrightWorker(Verifier())  # type: ignore[arg-type]
        worker.profile_id = "p"
        worker._connected_endpoint = "http://127.0.0.1:9222"
        worker._connection_generation = 17

        asyncio.run(worker._verify_destructive_action_window())
        self.assertEqual(
            [("p", "http://127.0.0.1:9222", 17)],
            calls,
        )

    def test_all_rejected_candidate_ports_back_off_without_spin(self) -> None:
        def handler(_base_url: str, _path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "msg": "not a BitBrowser API"}

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        status = client.health()
        self.assertEqual("offline", status["phase"])
        call_count = len(transport.calls)
        time.sleep(0.2)
        self.assertEqual(call_count, len(transport.calls))
        client.health()
        self.assertEqual(call_count, len(transport.calls))

    def test_fresh_inventory_corrects_external_close_and_endpoint_cache(self) -> None:
        opened = {"value": True}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "external", "isOpen": opened["value"]})
            if path == "/browser/pids/all":
                return ok({"external": 1234} if opened["value"] else {})
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        first = client.list_all_windows(force=True)["windows"][0]
        self.assertEqual("ready", first["window_state"])
        client._endpoint_cache["external"] = {
            "ws": None,
            "http": "http://127.0.0.1:9222",
        }
        opened["value"] = False
        second = client.list_all_windows(force=True)["windows"][0]
        self.assertEqual("closed", second["window_state"])
        self.assertNotIn("external", client._endpoint_cache)

    def test_same_profile_48_concurrent_open_waiters_issue_one_open(self) -> None:
        worker_count = 48
        start_barrier = threading.Barrier(worker_count + 1)
        open_entered = threading.Event()
        release_first_open = threading.Event()
        publish_endpoint = threading.Event()
        first_open = {"value": True}
        first_open_lock = threading.Lock()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "single-flight"})
            if path == "/browser/ports":
                return ok({"single-flight": 9922} if publish_endpoint.is_set() else {})
            if path == "/browser/open":
                with first_open_lock:
                    should_block = first_open["value"]
                    first_open["value"] = False
                if should_block:
                    open_entered.set()
                    self.assertTrue(release_first_open.wait(timeout=2.0))
                return ok()
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            open_timeout_seconds=3.0,
            command_timeout_seconds=3.0,
        )
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        outcome_lock = threading.Lock()

        def connect() -> None:
            start_barrier.wait(timeout=2.0)
            try:
                result = client.connection_endpoint(
                    "single-flight", open_if_needed=True
                )
                with outcome_lock:
                    results.append(result)
            except BaseException as exc:
                with outcome_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=connect, name=f"single-flight-{index}")
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        start_barrier.wait(timeout=2.0)
        self.assertTrue(open_entered.wait(timeout=2.0))
        # The actor is blocked in the first provider open, so all other initial
        # endpoint requests must be waiting in its queue before it is released.
        wait_until(lambda: client._commands.qsize() >= worker_count - 1, timeout=2.0)
        release_first_open.set()
        wait_until(lambda: client._commands.empty(), timeout=2.0)
        time.sleep(0.03)
        self.assertEqual(1, transport.count("/browser/open"))

        publish_endpoint.set()
        for thread in threads:
            thread.join(timeout=4.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(worker_count, len(results))
        self.assertTrue(
            all(result["http"] == "http://127.0.0.1:9922" for result in results)
        )
        self.assertEqual(1, transport.count("/browser/open"))

    def test_explicit_attempt_cancelled_during_open_closes_late_success_once(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "explicit-late"})
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=2.0))
                return ok({"id": "explicit-late", "port": 9923})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            open_timeout_seconds=2.0,
            command_timeout_seconds=2.0,
        )
        attempt_id = client.begin_connection_attempt("explicit-late")
        outcome: dict[str, Any] = {}

        def resolve_in_background() -> None:
            try:
                outcome["endpoint"] = client.connection_endpoint(
                    "explicit-late",
                    open_if_needed=True,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                outcome["error"] = exc

        resolver = threading.Thread(
            target=resolve_in_background,
            name="explicit-attempt-resolver",
        )
        resolver.start()
        self.assertTrue(open_entered.wait(timeout=2.0))

        cancellation = client.cancel_connection_attempt(attempt_id)
        self.assertTrue(cancellation["cancelled"])
        self.assertTrue(cancellation["close_scheduled"])
        self.assertEqual(1, client._window_generation["explicit-late"])

        release_open.set()
        resolver.join(timeout=3.0)
        self.assertFalse(resolver.is_alive())
        self.assertNotIn("endpoint", outcome)
        self.assertIsInstance(outcome.get("error"), UpstreamUnavailableError)
        wait_until(
            lambda: (
                transport.count("/browser/close") == 1
                and "explicit-late" not in client._pending_closes
            ),
            timeout=2.0,
        )
        time.sleep(0.03)
        self.assertEqual(1, transport.count("/browser/close"))

    def test_cancelling_attempt_for_external_open_window_does_not_close(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "external-open", "isOpen": True})
            if path == "/browser/ports":
                return ok({"external-open": 9924})
            if path == "/browser/close":
                raise AssertionError("an externally opened window must not be closed")
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        attempt_id = client.begin_connection_attempt("external-open")
        ticket = client.connection_endpoint(
            "external-open",
            open_if_needed=True,
            attempt_id=attempt_id,
        )

        self.assertEqual("http://127.0.0.1:9924", ticket["http"])
        self.assertEqual(0, ticket["generation"])
        cancellation = client.cancel_connection_attempt(attempt_id)
        self.assertEqual(
            {"cancelled": True, "close_scheduled": False},
            cancellation,
        )
        time.sleep(0.03)
        self.assertEqual(0, client._window_generation.get("external-open", 0))
        self.assertEqual(0, transport.count("/browser/open"))
        self.assertEqual(0, transport.count("/browser/close"))

    def test_shared_open_closes_only_after_all_explicit_attempts_cancel(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "shared-attempt"})
            if path == "/browser/ports":
                return ok({})
            if path in {"/browser/open", "/browser/close"}:
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            open_timeout_seconds=2.0,
            command_timeout_seconds=2.0,
        )
        attempt_ids = [
            client.begin_connection_attempt("shared-attempt") for _ in range(2)
        ]
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def resolve(attempt_id: str) -> None:
            try:
                client.connection_endpoint(
                    "shared-attempt",
                    open_if_needed=True,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        resolvers = [
            threading.Thread(
                target=resolve,
                args=(attempt_id,),
                name=f"shared-attempt-{index}",
            )
            for index, attempt_id in enumerate(attempt_ids)
        ]
        for resolver in resolvers:
            resolver.start()

        def both_attempts_joined_open() -> bool:
            with client._state_lock:
                return all(
                    client._connection_attempts[attempt_id].manager_open
                    for attempt_id in attempt_ids
                )

        wait_until(both_attempts_joined_open, timeout=2.0)
        self.assertEqual(1, transport.count("/browser/open"))

        first = client.cancel_connection_attempt(attempt_ids[0])
        self.assertEqual(
            {"cancelled": True, "close_scheduled": False},
            first,
        )
        time.sleep(0.03)
        self.assertEqual(0, transport.count("/browser/close"))
        self.assertEqual(0, client._window_generation.get("shared-attempt", 0))

        second = client.cancel_connection_attempt(attempt_ids[1])
        self.assertEqual(
            {"cancelled": True, "close_scheduled": True},
            second,
        )
        wait_until(lambda: transport.count("/browser/close") == 1, timeout=2.0)
        for resolver in resolvers:
            resolver.join(timeout=2.0)

        self.assertTrue(all(not resolver.is_alive() for resolver in resolvers))
        self.assertEqual(2, len(errors))
        self.assertTrue(all(isinstance(error, UpstreamUnavailableError) for error in errors))
        time.sleep(0.03)
        self.assertEqual(1, transport.count("/browser/open"))
        self.assertEqual(1, transport.count("/browser/close"))

    def test_close_waits_for_action_lease_to_end(self) -> None:
        close_entered = threading.Event()
        operations: list[str] = []

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "action-first", "isOpen": True})
            if path == "/browser/ports":
                return ok({"action-first": 9925})
            if path == "/browser/close":
                operations.append("close")
                close_entered.set()
                return ok()
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        ticket = client.connection_endpoint("action-first")
        endpoint = str(ticket["http"])
        lease_id = client.begin_action(
            "action-first",
            endpoint,
            ticket["generation"],
        )
        operations.append("action-start")
        outcome: dict[str, Any] = {}

        def close_while_action_runs() -> None:
            try:
                outcome["close"] = client.close_profile("action-first")
            except BaseException as exc:
                outcome["error"] = exc

        closer = threading.Thread(
            target=close_while_action_runs,
            name="close-waits-for-action",
        )
        closer.start()
        wait_until(lambda: client._window_generation.get("action-first") == 1)
        time.sleep(0.03)
        self.assertTrue(closer.is_alive())
        self.assertFalse(close_entered.is_set())

        operations.append("action-end")
        client.end_action(lease_id)
        closer.join(timeout=2.0)
        self.assertFalse(closer.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual("closed", outcome["close"]["window_state"])
        self.assertEqual(["action-start", "action-end", "close"], operations)

    def test_begin_action_rejects_generation_advanced_by_close(self) -> None:
        close_entered = threading.Event()
        release_close = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "close-first", "isOpen": True})
            if path == "/browser/ports":
                return ok({"close-first": 9926})
            if path == "/browser/close":
                close_entered.set()
                self.assertTrue(release_close.wait(timeout=2.0))
                return ok()
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        ticket = client.connection_endpoint("close-first")
        close_outcome: dict[str, Any] = {}
        action_outcome: dict[str, Any] = {}

        def close_first() -> None:
            try:
                close_outcome["result"] = client.close_profile("close-first")
            except BaseException as exc:
                close_outcome["error"] = exc

        def begin_late_action() -> None:
            try:
                action_outcome["lease"] = client.begin_action(
                    "close-first",
                    str(ticket["http"]),
                    ticket["generation"],
                )
            except BaseException as exc:
                action_outcome["error"] = exc

        closer = threading.Thread(target=close_first, name="close-wins-generation")
        closer.start()
        self.assertTrue(close_entered.wait(timeout=2.0))
        self.assertEqual(1, client._window_generation["close-first"])

        actor = threading.Thread(target=begin_late_action, name="late-action")
        actor.start()
        wait_until(lambda: client._commands.qsize() >= 1, timeout=2.0)
        release_close.set()
        closer.join(timeout=2.0)
        actor.join(timeout=2.0)

        self.assertFalse(closer.is_alive())
        self.assertFalse(actor.is_alive())
        self.assertNotIn("error", close_outcome)
        self.assertNotIn("lease", action_outcome)
        self.assertIsInstance(action_outcome.get("error"), UpstreamUnavailableError)
        error = action_outcome["error"]
        assert isinstance(error, UpstreamUnavailableError)
        self.assertIn(
            error.details["reason"],
            {"open_cancelled", "action_window_closing"},
        )

    def test_string_http_statuses_are_failures_and_not_provider_success(self) -> None:
        cases = (
            ("401", "auth_required", 401),
            ("429", "rate_limited", 429),
            ("500", "offline", 500),
        )
        for raw_status, expected_kind, expected_status in cases:
            with self.subTest(status=raw_status):
                payload = {"status": raw_status}
                failure = BitBrowserClientV2._payload_failure(payload)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(expected_kind, failure.kind)
                self.assertEqual(expected_status, failure.status)
                self.assertFalse(BitBrowserClientV2._provider_success(payload))

        self.assertTrue(BitBrowserClientV2._provider_success({"status": "200"}))
        self.assertTrue(BitBrowserClientV2._provider_success({"status": "0"}))
        nested_failures = (
            {
                "success": True,
                "error": {
                    "status": "failed",
                    "message": "cannot close browser",
                },
            },
            {
                "status": "success",
                "result": {
                    "status": "error",
                    "message": "provider rejected",
                },
            },
        )
        for payload in nested_failures:
            with self.subTest(payload=payload):
                failure = BitBrowserClientV2._payload_failure(payload)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual("provider_rejected", failure.kind)
                self.assertFalse(BitBrowserClientV2._provider_success(payload))

    def test_ambiguous_open_transport_timeout_is_eventually_closed(self) -> None:
        provider_open = {"value": False}
        fail_open_once = {"value": True}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(
                    {"id": "ambiguous-open", "isOpen": provider_open["value"]}
                )
            if path == "/browser/pids/all":
                return ok({"ambiguous-open": 4321} if provider_open["value"] else {})
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                provider_open["value"] = True
                if fail_open_once["value"]:
                    fail_open_once["value"] = False
                    # The request reached BitBrowser, but the response was lost.
                    raise _TransportFailure("offline", "timeout after request write")
                return ok()
            if path == "/browser/close":
                provider_open["value"] = False
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        with self.assertRaises(UpstreamUnavailableError):
            client.connection_endpoint("ambiguous-open", open_if_needed=True)

        # A direct compensating close is ideal; a durable pending close replayed
        # by the explicit reconnect is also valid after an offline transport error.
        if provider_open["value"]:
            client.confirm_login()
        wait_until(lambda: transport.count("/browser/close") >= 1, timeout=1.0)
        self.assertFalse(provider_open["value"])

    def test_post_open_ports_failure_is_eventually_closed(self) -> None:
        provider_open = {"value": False}
        ports_after_open_failed = {"value": False}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(
                    {"id": "ports-failed", "isOpen": provider_open["value"]}
                )
            if path == "/browser/pids/all":
                return ok({"ports-failed": 4322} if provider_open["value"] else {})
            if path == "/browser/ports":
                if provider_open["value"] and not ports_after_open_failed["value"]:
                    ports_after_open_failed["value"] = True
                    raise _TransportFailure("offline", "ports response was lost")
                return ok({})
            if path == "/browser/open":
                provider_open["value"] = True
                return ok()
            if path == "/browser/close":
                provider_open["value"] = False
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport)
        with self.assertRaises(UpstreamUnavailableError):
            client.connection_endpoint("ports-failed", open_if_needed=True)

        if provider_open["value"]:
            client.confirm_login()
        wait_until(lambda: transport.count("/browser/close") >= 1, timeout=1.0)
        self.assertFalse(provider_open["value"])

    def test_close_overtakes_queued_endpoint_open_commands(self) -> None:
        hold_ports_entered = threading.Event()
        release_hold_ports = threading.Event()
        requested: set[str] = set()
        operations: list[str] = []
        normal_ids = [f"ordinary-{index}" for index in range(16)]

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                profiles = [{"id": "hold"}, {"id": "priority-close"}]
                profiles.extend({"id": profile_id} for profile_id in normal_ids)
                return profile_list(*profiles)
            if path == "/browser/ports":
                if not release_hold_ports.is_set():
                    hold_ports_entered.set()
                    self.assertTrue(release_hold_ports.wait(timeout=2.0))
                    return ok({})
                return ok(
                    {
                        profile_id: 10_100 + index
                        for index, profile_id in enumerate(normal_ids)
                        if profile_id in requested
                    }
                )
            if path == "/browser/open":
                profile_id = str(payload["id"])
                operations.append(f"open:{profile_id}")
                requested.add(profile_id)
                return ok()
            if path == "/browser/close":
                operations.append(f"close:{payload['id']}")
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            ports_cache_seconds=10.0,
            open_timeout_seconds=2.0,
            command_timeout_seconds=3.0,
        )
        client.health()
        thread_errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def hold_actor() -> None:
            try:
                client.connection_endpoint("hold")
            except BaseException as exc:
                with errors_lock:
                    thread_errors.append(exc)

        holder = threading.Thread(target=hold_actor, name="priority-holder")
        holder.start()
        self.assertTrue(hold_ports_entered.wait(timeout=2.0))

        def open_ordinary(profile_id: str) -> None:
            try:
                client.connection_endpoint(profile_id, open_if_needed=True)
            except BaseException as exc:
                with errors_lock:
                    thread_errors.append(exc)

        ordinary_threads = [
            threading.Thread(
                target=open_ordinary,
                args=(profile_id,),
                name=f"queued-{profile_id}",
            )
            for profile_id in normal_ids
        ]
        for thread in ordinary_threads:
            thread.start()
        wait_until(lambda: client._commands.qsize() >= len(normal_ids), timeout=2.0)

        close_result: dict[str, Any] = {}
        close_error: list[BaseException] = []

        def close_priority_window() -> None:
            try:
                close_result.update(client.close_profile("priority-close"))
            except BaseException as exc:
                close_error.append(exc)

        closer = threading.Thread(target=close_priority_window, name="priority-close")
        closer.start()
        wait_until(
            lambda: client._commands.qsize() >= len(normal_ids) + 1,
            timeout=2.0,
        )
        release_hold_ports.set()

        holder.join(timeout=3.0)
        closer.join(timeout=3.0)
        for thread in ordinary_threads:
            thread.join(timeout=3.0)

        self.assertFalse(holder.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in ordinary_threads))
        self.assertEqual([], thread_errors)
        self.assertEqual([], close_error)
        self.assertEqual("closed", close_result["window_state"])
        self.assertTrue(operations)
        self.assertEqual("close:priority-close", operations[0])

    def test_submit_shutdown_race_finishes_promptly_and_can_restart(self) -> None:
        put_entered = threading.Event()
        release_put = threading.Event()

        client = self.client(
            ScenarioTransport(normal_handler(())),
            command_timeout_seconds=0.7,
            shutdown_timeout_seconds=0.2,
        )
        underlying_queue = client._commands

        class PausingQueue:
            def __init__(self) -> None:
                self.paused = False

            @staticmethod
            def command_name(item: Any) -> str | None:
                command = getattr(item, "command", item)
                return getattr(command, "name", None)

            def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
                if (
                    threading.current_thread().name == "submit-race"
                    and self.command_name(item) != "__stop__"
                    and not self.paused
                ):
                    self.paused = True
                    put_entered.set()
                    self.assert_release()
                underlying_queue.put(item, *args, **kwargs)

            @staticmethod
            def assert_release() -> None:
                if not release_put.wait(timeout=2.0):
                    raise AssertionError("submit queue insertion was not released")

            def get(self, *args: Any, **kwargs: Any) -> Any:
                return underlying_queue.get(*args, **kwargs)

            def get_nowait(self) -> Any:
                return underlying_queue.get_nowait()

            def empty(self) -> bool:
                return underlying_queue.empty()

            def qsize(self) -> int:
                return underlying_queue.qsize()

        client._commands = PausingQueue()  # type: ignore[assignment]
        submit_outcome: dict[str, Any] = {}

        def submit_inventory() -> None:
            try:
                submit_outcome["result"] = client.list_all_windows(force=True)
            except BaseException as exc:
                submit_outcome["error"] = exc

        submitter = threading.Thread(target=submit_inventory, name="submit-race")
        submitter.start()
        self.assertTrue(put_entered.wait(timeout=2.0))

        shutdown_thread = threading.Thread(target=client.shutdown, name="shutdown-race")
        shutdown_thread.start()
        time.sleep(0.05)
        released_at = time.monotonic()
        release_put.set()
        submitter.join(timeout=1.2)
        elapsed = time.monotonic() - released_at
        shutdown_thread.join(timeout=1.2)

        self.assertFalse(submitter.is_alive(), "submit waited on a dead actor")
        self.assertFalse(shutdown_thread.is_alive(), "shutdown did not converge")
        self.assertLess(elapsed, 0.45)
        self.assertTrue({"result", "error"}.intersection(submit_outcome))

        # A clean stop may be followed by one clean restart; no second actor may
        # have been created while the previous actor was still stopping.
        client.start()
        self.assertEqual("ready", client.health()["phase"])
        client.shutdown()
        self.assertIsNone(client._thread)

    def test_shutdown_never_starts_second_actor_while_blocked(self) -> None:
        blocked = threading.Event()
        release = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                blocked.set()
                self.assertTrue(release.wait(timeout=1.0))
                return ok()
            if path == "/browser/list":
                return profile_list()
            raise AssertionError(path)

        client = self.client(
            ScenarioTransport(handler), shutdown_timeout_seconds=0.05
        )
        client.start()
        self.assertTrue(blocked.wait(timeout=1.0))
        client.shutdown()
        old_thread = client._thread
        self.assertIsNotNone(old_thread)
        assert old_thread is not None
        self.assertTrue(old_thread.is_alive())
        with self.assertRaises(UpstreamUnavailableError):
            client.start()
        release.set()
        old_thread.join(timeout=1.0)
        client.shutdown()
        self.assertIsNone(client._thread)

    def test_shutdown_is_idempotent(self) -> None:
        client = self.client(ScenarioTransport(normal_handler(())))
        self.assertEqual("ready", client.health()["phase"])
        client.shutdown()
        self.assertEqual("stopped", client.status()["phase"])
        self.assertIsNone(client._thread)
        client.shutdown()

    def test_last_waiter_cancel_and_sibling_commit_are_linearized(self) -> None:
        provider_open: set[str] = set()

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(
                    {"id": "attempt-race"},
                    {"id": "commit-first"},
                    {"id": "cancel-first"},
                )
            if path == "/browser/ports":
                return ok(
                    {
                        profile_id: 10_300 + index
                        for index, profile_id in enumerate(sorted(provider_open))
                    }
                )
            if path == "/browser/open":
                provider_open.add(str(payload["id"]))
                return ok()
            if path == "/browser/close":
                provider_open.discard(str(payload["id"]))
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=2.0)

        def prepare(profile_id: str) -> tuple[str, str, dict[str, Any]]:
            leader_id = client.begin_connection_attempt(profile_id)
            sibling_id = client.begin_connection_attempt(profile_id)
            leader_ticket = client.connection_endpoint(
                profile_id,
                open_if_needed=True,
                attempt_id=leader_id,
            )
            sibling_ticket = client.connection_endpoint(
                profile_id,
                open_if_needed=True,
                attempt_id=sibling_id,
            )
            self.assertEqual(leader_ticket["http"], sibling_ticket["http"])
            client.verify_connection_endpoint(
                profile_id,
                str(sibling_ticket["http"]),
                sibling_ticket["generation"],
                attempt_id=sibling_id,
            )
            # The first waiter is no longer the last waiter, so cancelling it
            # must leave the shared manager-open cohort alive for the sibling.
            self.assertEqual(
                {"cancelled": True, "close_scheduled": False},
                client.cancel_connection_attempt(leader_id),
            )
            return leader_id, sibling_id, sibling_ticket

        leader_id, sibling_id, _ticket = prepare("attempt-race")
        race = threading.Barrier(3)
        outcome: dict[str, Any] = {}

        def cancel_last_waiter() -> None:
            race.wait(timeout=2.0)
            outcome["cancel"] = client.cancel_connection_attempt(sibling_id)

        def commit_sibling() -> None:
            race.wait(timeout=2.0)
            try:
                outcome["commit"] = client.commit_connection_attempt(sibling_id)
            except BaseException as exc:
                outcome["commit_error"] = exc

        canceller = threading.Thread(target=cancel_last_waiter, name="attempt-cancel-race")
        committer = threading.Thread(target=commit_sibling, name="attempt-commit-race")
        canceller.start()
        committer.start()
        race.wait(timeout=2.0)
        canceller.join(timeout=2.0)
        committer.join(timeout=2.0)
        self.assertFalse(canceller.is_alive())
        self.assertFalse(committer.is_alive())

        cancel_result = outcome["cancel"]
        commit_won = "commit" in outcome
        close_won = bool(cancel_result["close_scheduled"])
        self.assertNotEqual(
            commit_won,
            close_won,
            "commit and compensating close must be mutually exclusive",
        )
        if commit_won:
            self.assertNotIn("commit_error", outcome)
            self.assertEqual(
                {"cancelled": False, "close_scheduled": False},
                cancel_result,
            )
            time.sleep(0.03)
            self.assertFalse(
                any(
                    path == "/browser/close" and payload.get("id") == "attempt-race"
                    for _, path, payload, _ in transport.calls
                )
            )
        else:
            self.assertIsInstance(outcome.get("commit_error"), UpstreamUnavailableError)
            self.assertEqual(
                {"cancelled": True, "close_scheduled": True},
                cancel_result,
            )
            wait_until(
                lambda: any(
                    path == "/browser/close" and payload.get("id") == "attempt-race"
                    for _, path, payload, _ in transport.calls
                ),
                timeout=2.0,
            )
        client._discard_connection_attempt(leader_id)

        # Force the other lock order as a deterministic regression: once commit
        # completes, a later cancellation cannot close the committed window.
        leader_id, sibling_id, _ticket = prepare("commit-first")
        committed = client.commit_connection_attempt(sibling_id)
        self.assertTrue(committed["committed"])
        self.assertEqual(
            {"cancelled": False, "close_scheduled": False},
            client.cancel_connection_attempt(sibling_id),
        )
        time.sleep(0.03)
        self.assertFalse(
            any(
                path == "/browser/close" and payload.get("id") == "commit-first"
                for _, path, payload, _ in transport.calls
            )
        )
        self.assertEqual(0, client._window_generation.get("commit-first", 0))
        client._discard_connection_attempt(leader_id)

        # Force the inverse lock order too: once the last waiter cancellation
        # advances the generation, commit must fail and may never resurrect it.
        leader_id, sibling_id, _ticket = prepare("cancel-first")
        cancelled = client.cancel_connection_attempt(sibling_id)
        self.assertEqual(
            {"cancelled": True, "close_scheduled": True},
            cancelled,
        )
        with self.assertRaises(UpstreamUnavailableError):
            client.commit_connection_attempt(sibling_id)
        self.assertEqual(1, client._window_generation["cancel-first"])
        wait_until(
            lambda: any(
                path == "/browser/close" and payload.get("id") == "cancel-first"
                for _, path, payload, _ in transport.calls
            ),
            timeout=2.0,
        )
        client._discard_connection_attempt(leader_id)

    def test_queued_sibling_is_a_waiter_before_actor_registers_it(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()
        publish_endpoint = threading.Event()

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "queued-sibling"})
            if path == "/browser/ports":
                return ok({"queued-sibling": 10_311} if publish_endpoint.is_set() else {})
            if path == "/browser/open":
                self.assertEqual("queued-sibling", payload["id"])
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=2.0))
                return ok()
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=3.0)
        leader_id = client.begin_connection_attempt("queued-sibling")
        sibling_id = client.begin_connection_attempt("queued-sibling")
        outcomes: dict[str, Any] = {}

        def resolve(name: str, attempt_id: str) -> None:
            try:
                outcomes[name] = client.connection_endpoint(
                    "queued-sibling",
                    open_if_needed=True,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                outcomes[f"{name}_error"] = exc

        leader = threading.Thread(
            target=resolve,
            args=("leader", leader_id),
            name="queued-sibling-leader",
        )
        sibling = threading.Thread(
            target=resolve,
            args=("sibling", sibling_id),
            name="queued-sibling-follower",
        )
        leader.start()
        self.assertTrue(open_entered.wait(timeout=2.0))
        sibling.start()
        wait_until(lambda: client._commands.qsize() >= 1, timeout=2.0)
        with client._state_lock:
            self.assertTrue(client._connection_attempts[sibling_id].resolving)
            self.assertFalse(client._connection_attempts[sibling_id].manager_open)

        self.assertEqual(
            {"cancelled": True, "close_scheduled": False},
            client.cancel_connection_attempt(leader_id),
        )
        self.assertEqual(0, client._window_generation.get("queued-sibling", 0))
        self.assertNotIn("queued-sibling", client._pending_closes)
        publish_endpoint.set()
        release_open.set()
        leader.join(timeout=3.0)
        sibling.join(timeout=3.0)
        self.assertFalse(leader.is_alive())
        self.assertFalse(sibling.is_alive())
        self.assertIsInstance(outcomes.get("leader_error"), UpstreamUnavailableError)
        self.assertEqual("http://127.0.0.1:10311", outcomes["sibling"]["http"])
        client.verify_connection_endpoint(
            "queued-sibling",
            str(outcomes["sibling"]["http"]),
            outcomes["sibling"]["generation"],
            attempt_id=sibling_id,
        )
        client.commit_connection_attempt(sibling_id)
        time.sleep(0.03)
        self.assertEqual(0, transport.count("/browser/close"))

    def test_all_queued_open_waiters_cancel_closes_the_shared_late_open(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=2.0))
                return ok()
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=3.0)
        leader_id = client.begin_connection_attempt("double-cancel")
        sibling_id = client.begin_connection_attempt("double-cancel")
        outcomes: dict[str, BaseException] = {}

        def resolve(name: str, attempt_id: str) -> None:
            try:
                client.connection_endpoint(
                    "double-cancel",
                    open_if_needed=True,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                outcomes[name] = exc

        leader = threading.Thread(target=resolve, args=("leader", leader_id))
        sibling = threading.Thread(target=resolve, args=("sibling", sibling_id))
        leader.start()
        self.assertTrue(open_entered.wait(timeout=2.0))
        sibling.start()
        wait_until(lambda: client._commands.qsize() >= 1, timeout=2.0)

        self.assertEqual(
            {"cancelled": True, "close_scheduled": False},
            client.cancel_connection_attempt(leader_id),
        )
        self.assertEqual(
            {"cancelled": True, "close_scheduled": True},
            client.cancel_connection_attempt(sibling_id),
        )
        release_open.set()
        leader.join(timeout=3.0)
        sibling.join(timeout=3.0)
        self.assertFalse(leader.is_alive())
        self.assertFalse(sibling.is_alive())
        self.assertIsInstance(outcomes.get("leader"), UpstreamUnavailableError)
        self.assertIsInstance(outcomes.get("sibling"), UpstreamUnavailableError)
        wait_until(lambda: transport.count("/browser/close") == 1, timeout=2.0)
        self.assertEqual({}, client._pending_closes)
        self.assertEqual("closed", client._window_phase["double-cancel"])

    def test_cancelled_inflight_open_rejection_does_not_close_external_window(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/ports":
                return ok({})
            if path == "/browser/open":
                open_entered.set()
                self.assertTrue(release_open.wait(timeout=2.0))
                return {"success": False, "msg": "already running"}
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=3.0)
        attempt_id = client.begin_connection_attempt("externally-opened")
        outcome: dict[str, BaseException] = {}

        def resolve() -> None:
            try:
                client.connection_endpoint(
                    "externally-opened",
                    open_if_needed=True,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=resolve)
        thread.start()
        self.assertTrue(open_entered.wait(timeout=2.0))
        self.assertEqual(
            {"cancelled": True, "close_scheduled": True},
            client.cancel_connection_attempt(attempt_id),
        )
        self.assertNotIn("externally-opened", client._pending_closes)
        release_open.set()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("error"), UpstreamUnavailableError)
        time.sleep(0.03)
        self.assertEqual(0, transport.count("/browser/close"))
        self.assertNotIn("externally-opened", client._pending_closes)
        self.assertNotIn("externally-opened", client._open_may_have_effect)
        self.assertNotIn(("externally-opened", 0), client._cancelled_open_cohorts)

    def test_shutdown_drains_queued_close_then_restart_replays_it(self) -> None:
        ports_entered = threading.Event()
        release_ports = threading.Event()
        provider_open = {"value": True}

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(
                    {"id": "hold-shutdown"},
                    {"id": "replay-after-restart", "isOpen": provider_open["value"]},
                )
            if path == "/browser/ports":
                ports_entered.set()
                self.assertTrue(release_ports.wait(timeout=2.0))
                return ok({})
            if path == "/browser/close":
                provider_open["value"] = False
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            command_timeout_seconds=2.0,
            shutdown_timeout_seconds=2.0,
        )
        client.health()
        holder_outcome: dict[str, Any] = {}
        close_outcome: dict[str, Any] = {}

        def hold_actor() -> None:
            try:
                holder_outcome["result"] = client.connection_endpoint("hold-shutdown")
            except BaseException as exc:
                holder_outcome["error"] = exc

        def queue_close() -> None:
            try:
                close_outcome["result"] = client.close_profile("replay-after-restart")
            except BaseException as exc:
                close_outcome["error"] = exc

        holder = threading.Thread(target=hold_actor, name="shutdown-drain-holder")
        closer = threading.Thread(target=queue_close, name="shutdown-drain-close")
        holder.start()
        self.assertTrue(ports_entered.wait(timeout=2.0))
        closer.start()
        wait_until(
            lambda: "replay-after-restart" in client._pending_closes
            and client._commands.qsize() >= 1,
            timeout=2.0,
        )

        shutdown_thread = threading.Thread(target=client.shutdown, name="shutdown-drain")
        shutdown_thread.start()
        wait_until(lambda: client.status()["phase"] == "stopping", timeout=2.0)
        release_ports.set()
        shutdown_thread.join(timeout=3.0)
        holder.join(timeout=3.0)
        closer.join(timeout=3.0)
        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(holder.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(0, transport.count("/browser/close"))
        self.assertTrue(provider_open["value"])
        self.assertIn("replay-after-restart", client._pending_closes)
        self.assertIsInstance(close_outcome.get("error"), UpstreamUnavailableError)

        client.start()
        wait_until(lambda: transport.count("/browser/close") == 1, timeout=2.0)
        self.assertFalse(provider_open["value"])
        self.assertNotIn("replay-after-restart", client._pending_closes)
        self.assertEqual("closed", client._window_phase["replay-after-restart"])

    def test_rejected_replay_close_backs_off_and_does_not_starve_next(self) -> None:
        close_ids: list[str] = []

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/close":
                profile_id = str(payload["id"])
                close_ids.append(profile_id)
                if profile_id == "retry-a":
                    return {"success": False, "msg": "profile is busy"}
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(
            transport,
            close_retry_base_seconds=5.0,
            command_timeout_seconds=2.0,
        )
        client.health()
        with client._state_lock:
            client._window_generation.update({"retry-a": 1, "retry-b": 1})
            client._pending_closes.update({"retry-a": 1, "retry-b": 1})
            client._window_phase.update({"retry-a": "closing", "retry-b": "closing"})

        client._replay_pending_closes()
        wait_until(lambda: client._window_phase.get("retry-b") == "closed", timeout=2.0)
        self.assertEqual(1, close_ids.count("retry-a"))
        self.assertEqual(1, close_ids.count("retry-b"))
        self.assertIn("retry-a", client._pending_closes)
        self.assertNotIn("retry-b", client._pending_closes)
        retry_failures, retry_at = client._close_retry_after["retry-a"]
        self.assertEqual(1, retry_failures)
        self.assertGreater(retry_at, client.clock())

    def test_nested_provider_close_error_keeps_pending_close(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/close":
                return {
                    "success": True,
                    "error": {
                        "status": "failed",
                        "message": "cannot close browser",
                    },
                }
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        client.health()
        with self.assertRaises(UpstreamUnavailableError):
            client.close_profile("nested-close-error")
        self.assertIn("nested-close-error", client._pending_closes)
        self.assertEqual("error", client._window_phase["nested-close-error"])
        self.assertIn("nested-close-error", client._close_retry_after)

    def test_scalar_or_unacknowledged_close_error_keeps_pending_close(self) -> None:
        responses = iter(({"error": 1}, {"data": {"message": "maybe closed"}}))

        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/close":
                return next(responses)
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        client.health()
        for profile_id in ("scalar-close-error", "missing-close-ack"):
            with self.assertRaises(UpstreamUnavailableError):
                client.close_profile(profile_id)
            self.assertIn(profile_id, client._pending_closes)
            self.assertEqual("error", client._window_phase[profile_id])

    def test_provider_not_found_close_is_idempotent_and_clears_pending(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/close":
                return {"success": False, "msg": "browser not found"}
            raise AssertionError(path)

        client = self.client(ScenarioTransport(handler))
        client.health()
        result = client.close_profile("already-gone")
        self.assertEqual("closed", result["window_state"])
        self.assertNotIn("already-gone", client._pending_closes)

    def test_urgent_close_gate_stops_inventory_before_page_one(self) -> None:
        for failure_status, expected_phase in ((429, "cooldown"), (401, "auth_required")):
            with self.subTest(status=failure_status):
                page_zero_entered = threading.Event()
                release_page_zero = threading.Event()
                list_pages: list[int] = []

                def handler(
                    _base_url: str,
                    path: str,
                    payload: dict[str, Any],
                ) -> dict[str, Any]:
                    if path == "/health":
                        return ok()
                    if path == "/browser/list":
                        page = int(payload["page"])
                        list_pages.append(page)
                        if page == 0:
                            page_zero_entered.set()
                            self.assertTrue(release_page_zero.wait(timeout=2.0))
                            rows = [{"id": f"gate-{failure_status}-{index}"} for index in range(100)]
                            return ok({"list": rows, "total": 150})
                        raise AssertionError("inventory requested page 1 after provider gate")
                    if path == "/browser/close":
                        return {"status": failure_status}
                    if path == "/browser/pids/all":
                        raise AssertionError("inventory continued to pids after provider gate")
                    raise AssertionError(path)

                transport = ScenarioTransport(handler)
                client = self.client(
                    transport,
                    inventory_page_interval_seconds=0.2,
                    command_timeout_seconds=2.0,
                )
                inventory_outcome: dict[str, Any] = {}
                close_outcome: dict[str, Any] = {}

                def read_inventory() -> None:
                    try:
                        inventory_outcome["result"] = client.list_all_windows(force=True)
                    except BaseException as exc:
                        inventory_outcome["error"] = exc

                def urgent_close() -> None:
                    try:
                        close_outcome["result"] = client.close_profile(
                            f"gate-{failure_status}-0"
                        )
                    except BaseException as exc:
                        close_outcome["error"] = exc

                inventory = threading.Thread(
                    target=read_inventory,
                    name=f"inventory-gate-{failure_status}",
                )
                closer = threading.Thread(
                    target=urgent_close,
                    name=f"urgent-close-{failure_status}",
                )
                inventory.start()
                self.assertTrue(page_zero_entered.wait(timeout=2.0))
                closer.start()
                wait_until(lambda: client._commands.qsize() >= 1, timeout=2.0)
                release_page_zero.set()
                inventory.join(timeout=3.0)
                closer.join(timeout=3.0)
                self.assertFalse(inventory.is_alive())
                self.assertFalse(closer.is_alive())
                self.assertEqual([0], list_pages)
                self.assertEqual(expected_phase, client.status()["phase"])
                self.assertIn("result", inventory_outcome)
                self.assertTrue(inventory_outcome["result"]["stale"])
                self.assertIsInstance(close_outcome.get("error"), UpstreamUnavailableError)

    def test_bare_inventory_pages_read_all_150_windows(self) -> None:
        profiles = [{"id": f"bare-{index:03d}"} for index in range(150)]
        requested_pages: list[int] = []

        def handler(_base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                page = int(payload["page"])
                requested_pages.append(page)
                start = page * 100
                return ok(profiles[start : start + 100])
            if path == "/browser/pids/all":
                return ok([])
            raise AssertionError(path)

        listing = self.client(
            ScenarioTransport(handler), inventory_page_interval_seconds=0.0
        ).list_all_windows(force=True)
        self.assertEqual(150, listing["total"])
        self.assertEqual(150, len(listing["windows"]))
        self.assertEqual(["bare-000", "bare-149"], [
            listing["windows"][0]["id"],
            listing["windows"][-1]["id"],
        ])
        self.assertEqual([0, 1], requested_pages)

    def test_discovery_skips_gated_candidate_and_uses_next_valid_port(self) -> None:
        for first_status in (401, 429):
            with self.subTest(first_status=first_status):
                health_ports: list[str] = []

                def handler(
                    base_url: str,
                    path: str,
                    _payload: dict[str, Any],
                ) -> dict[str, Any]:
                    if path != "/health":
                        raise AssertionError(path)
                    health_ports.append(base_url)
                    if base_url.endswith(":54345"):
                        return {"status": first_status}
                    self.assertTrue(base_url.endswith(":54346"))
                    return ok()

                client = self.client(ScenarioTransport(handler))
                status = client.health()
                self.assertEqual("ready", status["phase"])
                self.assertEqual("http://127.0.0.1:54346", client.base_url)
                self.assertEqual(
                    ["http://127.0.0.1:54345", "http://127.0.0.1:54346"],
                    health_ports,
                )

    def test_worker_cancellation_during_action_begin_does_not_leak_lease(self) -> None:
        def handler(_base_url: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list({"id": "action-cancel", "isOpen": True})
            if path == "/browser/ports":
                return ok({"action-cancel": 10_321})
            if path == "/browser/close":
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = self.client(transport, command_timeout_seconds=2.0)

        async def exercise() -> None:
            ticket = await asyncio.to_thread(
                client.connection_endpoint,
                "action-cancel",
            )
            lease_created = threading.Event()
            release_resolve = threading.Event()
            resolve_returned = threading.Event()
            body_entered = asyncio.Event()

            class DelayedResolve:
                def __getattr__(self, name: str) -> Any:
                    return getattr(client, name)

                def resolve_action_attempt(self, attempt_id: str) -> str:
                    result = client.resolve_action_attempt(attempt_id)
                    lease_created.set()
                    try:
                        if not release_resolve.wait(timeout=2.0):
                            raise AssertionError("resolve_action_attempt was not released")
                        return result
                    finally:
                        resolve_returned.set()

            worker = PlaywrightWorker(DelayedResolve())  # type: ignore[arg-type]
            worker.profile_id = "action-cancel"
            worker._connected_endpoint = str(ticket["http"])
            worker._connection_generation = ticket["generation"]

            async def action() -> None:
                async with worker._destructive_action_lease():
                    body_entered.set()

            task = asyncio.create_task(action())
            try:
                self.assertTrue(
                    await asyncio.to_thread(lease_created.wait, 1.0),
                    "the actor never created an action lease",
                )
                self.assertEqual(1, len(client._action_attempts))
                self.assertEqual(1, len(client._action_leases))
                self.assertEqual(1, sum(client._active_actions.values()))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(body_entered.is_set())
                self.assertEqual({}, client._action_attempts)
                self.assertEqual({}, client._action_leases)
                self.assertEqual({}, client._active_actions)
            finally:
                release_resolve.set()
                self.assertTrue(
                    await asyncio.to_thread(resolve_returned.wait, 1.0),
                    "the cancelled to_thread worker did not finish",
                )

            closed = await asyncio.wait_for(
                asyncio.to_thread(client.close_profile, "action-cancel"),
                timeout=1.0,
            )
            self.assertEqual("closed", closed["window_state"])
            self.assertEqual(1, transport.count("/browser/close"))

        asyncio.run(exercise())

    def test_action_attempt_cancelled_before_actor_submission_is_removed(self) -> None:
        client = self.client(ScenarioTransport(lambda *_args: ok()))
        attempt_id = client.begin_action_attempt(
            "action-early-cancel",
            "http://127.0.0.1:10331",
            0,
        )

        self.assertEqual(1, len(client._action_attempts))
        self.assertEqual(
            {"cancelled": True, "lease_released": False},
            client.cancel_action_attempt(attempt_id),
        )
        self.assertEqual({}, client._action_attempts)
        self.assertEqual({}, client._action_leases)
        self.assertEqual({}, client._active_actions)
        with self.assertRaises(UpstreamUnavailableError):
            client.resolve_action_attempt(attempt_id)


if __name__ == "__main__":
    unittest.main()
