from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from app.cdp_relay import CdpRelayError, StrictLoopbackCdpRelay
from app.errors import UpstreamUnavailableError
from app.playwright_worker import PlaywrightWorker


def read_headers(connection: socket.socket) -> bytes:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = connection.recv(4096)
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def websocket_request(path: str, port: int, key: bytes) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key.decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")


class OneShotServer:
    def __init__(self, handler: object) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = int(self.listener.getsockname()[1])
        self.error: BaseException | None = None

        def run() -> None:
            try:
                connection, _ = self.listener.accept()
                with connection:
                    handler(connection)  # type: ignore[operator]
            except BaseException as exc:
                self.error = exc
            finally:
                self.listener.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise AssertionError("one-shot server did not stop")
        if self.error is not None:
            raise self.error


class StrictLoopbackCdpRelayTestCase(unittest.TestCase):
    path = "/devtools/browser/relay-test"

    def test_relay_requires_browser_scoped_loopback_endpoint(self) -> None:
        for endpoint in (
            "ws://example.com:9222/devtools/browser/test",
            "ws://127.0.0.1:9222/devtools/page/test",
            "ws://127.0.0.1:9222/devtools/browser/test?redirect=1",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(CdpRelayError):
                StrictLoopbackCdpRelay(endpoint)

    def test_windows_listener_requests_exclusive_port_before_bind(self) -> None:
        events: list[tuple[object, ...]] = []

        class FakeListener:
            def setsockopt(self, *args: object) -> None:
                events.append(("setsockopt", *args))

            def bind(self, address: object) -> None:
                events.append(("bind", address))

            def listen(self, backlog: int) -> None:
                events.append(("listen", backlog))

            def settimeout(self, timeout: float) -> None:
                events.append(("settimeout", timeout))

            def close(self) -> None:
                events.append(("close",))

        fake = FakeListener()
        with (
            patch.object(socket, "SO_EXCLUSIVEADDRUSE", 98765, create=True),
            patch("app.cdp_relay.socket.socket", return_value=fake),
        ):
            listener = StrictLoopbackCdpRelay._create_listener()
        self.assertIs(fake, listener)
        self.assertEqual(
            ("setsockopt", socket.SOL_SOCKET, 98765, 1),
            events[0],
        )
        self.assertEqual(("bind", ("127.0.0.1", 0)), events[1])
        self.assertFalse(
            any(
                event[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR)
                for event in events
            )
        )

    def test_actual_upstream_redirect_is_not_forwarded_to_playwright(self) -> None:
        def redirect(connection: socket.socket) -> None:
            read_headers(connection)
            connection.sendall(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: ws://203.0.113.10/devtools/browser/leak\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

        upstream = OneShotServer(redirect)
        relay = StrictLoopbackCdpRelay(
            f"ws://127.0.0.1:{upstream.port}{self.path}",
            timeout_seconds=0.5,
        )
        local_endpoint = relay.start()
        local_port = int(local_endpoint.split(":")[2].split("/")[0])
        key = base64.b64encode(b"0123456789abcdef")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=1.0) as client:
                client.sendall(websocket_request(self.path, local_port, key))
                client.settimeout(1.0)
                response = client.recv(4096)
            self.assertNotIn(b"302", response)
            self.assertNotIn(b"Location", response)
            deadline = time.monotonic() + 1.0
            while relay.failure is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("重定向", relay.failure or "")
        finally:
            relay.stop()
            upstream.join()

    def test_valid_handshake_and_bytes_are_relayed_bidirectionally(self) -> None:
        observed: list[bytes] = []

        def websocket(connection: socket.socket) -> None:
            request = read_headers(connection)
            key_line = next(
                line for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            key = key_line.split(b":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1(
                    key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).digest()
            )
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            observed.append(connection.recv(len(b"client-wire")))
            connection.sendall(b"server-wire")

        upstream = OneShotServer(websocket)
        relay = StrictLoopbackCdpRelay(
            f"ws://127.0.0.1:{upstream.port}{self.path}",
            timeout_seconds=1.0,
        )
        local_endpoint = relay.start()
        local_port = int(local_endpoint.split(":")[2].split("/")[0])
        key = base64.b64encode(b"fedcba9876543210")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=1.0) as client:
                client.sendall(websocket_request(self.path, local_port, key))
                response = read_headers(client)
                self.assertTrue(response.startswith(b"HTTP/1.1 101"))
                client.sendall(b"client-wire")
                self.assertEqual(b"server-wire", client.recv(len(b"server-wire")))
            upstream.join()
            self.assertEqual([b"client-wire"], observed)
            self.assertIsNone(relay.failure)
        finally:
            relay.stop()


class PlaywrightRelayIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_worker_connects_to_relay_and_verifies_original_endpoint(self) -> None:
        original = "ws://127.0.0.1:9222/devtools/browser/original"
        relayed = "ws://127.0.0.1:37777/devtools/browser/original"

        class FakeRelay:
            failure = None

            def __init__(self) -> None:
                self.stopped = False

            def start(self) -> str:
                return relayed

            def stop(self) -> None:
                self.stopped = True

        relay = FakeRelay()

        class FakeBitBrowser:
            def __init__(self) -> None:
                self.verified: tuple[object, ...] | None = None

            @staticmethod
            def connection_endpoint(
                profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del profile_id, open_if_needed
                return {"ws": original, "generation": 7}

            @staticmethod
            def resolve_cdp_endpoint(endpoint: str) -> str:
                return endpoint

            @staticmethod
            def create_cdp_relay(endpoint: str) -> FakeRelay:
                if endpoint != original:
                    raise AssertionError(endpoint)
                return relay

            def verify_connection_endpoint(self, *args: object) -> None:
                self.verified = args

        class FakePage:
            url = "https://www.instagram.com/"

            @staticmethod
            def is_closed() -> bool:
                return False

            async def bring_to_front(self) -> None:
                return None

        class FakeContext:
            pages = [FakePage()]

        class FakeBrowser:
            contexts = [FakeContext()]

        class FakeChromium:
            def __init__(self) -> None:
                self.call: tuple[str, int] | None = None

            async def connect_over_cdp(
                self, endpoint: str, timeout: int
            ) -> FakeBrowser:
                self.call = (endpoint, timeout)
                return FakeBrowser()

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

            async def stop(self) -> None:
                return None

        driver = FakePlaywright()

        class FakeStarter:
            async def start(self) -> FakePlaywright:
                return driver

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: FakeStarter()
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        bitbrowser = FakeBitBrowser()
        worker = PlaywrightWorker(bitbrowser)  # type: ignore[arg-type]
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            await worker.connect("window-7", open_if_needed=False)
            self.assertEqual((relayed, 30_000), driver.chromium.call)
            self.assertEqual(("window-7", original, 7), bitbrowser.verified)
            self.assertEqual(original, worker._connected_endpoint)
            await worker.disconnect()
        self.assertTrue(relay.stopped)

    async def test_cancel_during_relay_start_waits_and_stops_owned_relay(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class SlowRelay:
            failure = None

            def __init__(self) -> None:
                self.stop_count = 0

            def start(self) -> str:
                entered.set()
                if not release.wait(timeout=2.0):
                    raise AssertionError("relay start was not released")
                return "ws://127.0.0.1:37777/devtools/browser/cancel"

            def stop(self) -> None:
                self.stop_count += 1

        relay = SlowRelay()

        class FakeBitBrowser:
            @staticmethod
            def connection_endpoint(
                profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del profile_id, open_if_needed
                return {
                    "ws": "ws://127.0.0.1:9222/devtools/browser/cancel",
                    "generation": 1,
                }

            @staticmethod
            def resolve_cdp_endpoint(endpoint: str) -> str:
                return endpoint

            @staticmethod
            def create_cdp_relay(endpoint: str) -> SlowRelay:
                del endpoint
                return relay

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: None
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(FakeBitBrowser())  # type: ignore[arg-type]
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            task = asyncio.create_task(
                worker.connect("cancel-relay", open_if_needed=False)
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(relay.stop_count, 1)
        self.assertIsNone(worker._cdp_relay)

    async def test_cancelled_disconnect_still_closes_real_relay_listener(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingSession:
            async def detach(self) -> None:
                entered.set()
                await release.wait()

        class FakePlaywright:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        relay = StrictLoopbackCdpRelay(
            "ws://127.0.0.1:9/devtools/browser/disconnect-cancel",
            timeout_seconds=0.2,
        )
        relay.start()
        driver = FakePlaywright()
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker._cdp_relay = relay
        worker._cdp_session = BlockingSession()
        worker._playwright = driver
        worker.disconnect_timeout_seconds = 1.0

        task = asyncio.create_task(worker.disconnect())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(relay._listener)
        self.assertIsNone(relay._thread)
        self.assertTrue(driver.stopped)

    async def test_cancel_during_playwright_start_stops_late_driver(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class FakeBitBrowser:
            @staticmethod
            def connection_endpoint(
                profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del profile_id, open_if_needed
                return {
                    "ws": "ws://127.0.0.1:9222/devtools/browser/slow-driver",
                    "generation": 1,
                }

        class LatePlaywright:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        driver = LatePlaywright()

        class SlowManager:
            async def start(self) -> LatePlaywright:
                entered.set()
                await release.wait()
                return driver

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: SlowManager()
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(FakeBitBrowser())  # type: ignore[arg-type]
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            task = asyncio.create_task(
                worker.connect("slow-driver", open_if_needed=False)
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(driver.stopped)
        self.assertIsNone(worker._playwright)

    async def test_cancelled_hung_playwright_start_has_bounded_cleanup(self) -> None:
        entered = asyncio.Event()

        class FakeBitBrowser:
            @staticmethod
            def connection_endpoint(
                profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del profile_id, open_if_needed
                return {
                    "ws": "ws://127.0.0.1:9222/devtools/browser/hung-driver",
                    "generation": 1,
                }

        class HungManager:
            def __init__(self) -> None:
                self.exited = False

            async def start(self) -> object:
                entered.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

            async def __aexit__(self, *_args: object) -> None:
                self.exited = True

        manager = HungManager()
        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: manager
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(FakeBitBrowser())  # type: ignore[arg-type]
        worker.disconnect_timeout_seconds = 0.05
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            task = asyncio.create_task(
                worker.connect("hung-driver", open_if_needed=False)
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(manager.exited)
        self.assertIsNone(worker._playwright)

    async def test_internal_cleanup_timeout_is_not_reported_as_caller_cancel(self) -> None:
        class FakeBitBrowser:
            @staticmethod
            def connection_endpoint(
                profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del profile_id, open_if_needed
                return {
                    "ws": "ws://127.0.0.1:9222/devtools/browser/start-error",
                    "generation": 1,
                }

        class BrokenManager:
            async def start(self) -> object:
                raise RuntimeError("driver start failed")

            async def __aexit__(self, *_args: object) -> None:
                await asyncio.Future()

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: BrokenManager()
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(FakeBitBrowser())  # type: ignore[arg-type]
        worker.disconnect_timeout_seconds = 0.05
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            with self.assertRaises(UpstreamUnavailableError) as raised:
                await asyncio.wait_for(
                    worker.connect("start-error", open_if_needed=False),
                    timeout=0.5,
                )
        self.assertEqual("playwright_start_failed", raised.exception.details["reason"])


if __name__ == "__main__":
    unittest.main()
