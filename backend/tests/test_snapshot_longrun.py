from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import JSONResponse


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.database import Database
from app.errors import UpstreamUnavailableError
from app.main import create_app
from app.service import CoreService


PASSWORD = "correct horse battery staple"


def _empty_inventory() -> dict[str, Any]:
    return {
        "windows": [],
        "total": 0,
        "provider_success": True,
        "stale": False,
        "connection": {
            "state": "connected",
            "connected": True,
            "source": "snapshot-longrun-test",
        },
    }


class _SlowOverlapDetectingBitBrowser:
    """Hold the first inventory call long enough for a racing call to enter."""

    def __init__(self) -> None:
        self.first_entered = threading.Event()
        self.overlap_detected = threading.Event()
        self._state_lock = threading.Lock()
        self.calls = 0
        self.active_calls = 0
        self.max_active_calls = 0

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        with self._state_lock:
            self.calls += 1
            call_number = self.calls
            self.active_calls += 1
            self.max_active_calls = max(
                self.max_active_calls, self.active_calls
            )
            if self.active_calls > 1:
                self.overlap_detected.set()
            if call_number == 1:
                self.first_entered.set()
        try:
            if call_number == 1:
                # Without the owner lock the second request enters inventory and
                # releases this wait. With serialization this bounded wait makes
                # the first build deliberately slow before the second can start.
                self.overlap_detected.wait(timeout=1.0)
            return _empty_inventory()
        finally:
            with self._state_lock:
                self.active_calls -= 1


class _GatedBitBrowser:
    def __init__(self) -> None:
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._state_lock = threading.Lock()
        self.calls = 0

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        with self._state_lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=5.0):
                raise TimeoutError("test did not release the first inventory call")
        return _empty_inventory()


class _ControllableRequest:
    def __init__(self) -> None:
        self.disconnected = False
        self.disconnect_checks = 0

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return self.disconnected


class SnapshotLongRunSerializationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = Database(self.data_dir / "collector.sqlite3")
        self.database.initialize()
        service = CoreService(self.database, session_hours=1)
        self.owner = service.register_user("snapshot-owner", PASSWORD)
        self.login = service.login("snapshot-owner", PASSWORD)
        self.settings = Settings(
            startup_token="snapshot-longrun-test-startup-token-456",
            database_path=self.database.path,
            data_dir=self.data_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    async def _wait_for_thread_event(
        event: threading.Event, *, timeout: float = 3.0
    ) -> None:
        observed = await asyncio.wait_for(
            asyncio.to_thread(event.wait, timeout), timeout=timeout + 0.5
        )
        if not observed:
            raise AssertionError("timed out waiting for test worker event")

    @staticmethod
    def _snapshot_endpoint(app: Any) -> Any:
        for route in app.routes:
            if (
                getattr(route, "path", None) == "/api/workbench/snapshot"
                and "GET" in (getattr(route, "methods", None) or set())
            ):
                return route.endpoint
        raise AssertionError("workbench snapshot route is missing")

    async def test_same_owner_slow_snapshot_builds_never_overlap(self) -> None:
        bitbrowser = _SlowOverlapDetectingBitBrowser()
        app = create_app(
            self.settings,
            database=self.database,
            bitbrowser=bitbrowser,  # type: ignore[arg-type]
        )
        headers = {
            "X-Startup-Token": self.settings.startup_token,
            "Authorization": f"Bearer {self.login['token']}",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://snapshot.test"
        ) as client:
            first = asyncio.create_task(
                client.get("/api/workbench/snapshot", headers=headers)
            )
            await self._wait_for_thread_event(bitbrowser.first_entered)
            second = asyncio.create_task(
                client.get("/api/workbench/snapshot", headers=headers)
            )
            first_response, second_response = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=8.0
            )

        self.assertEqual(200, first_response.status_code, first_response.text)
        self.assertEqual(200, second_response.status_code, second_response.text)
        self.assertEqual(2, bitbrowser.calls)
        self.assertEqual(1, bitbrowser.max_active_calls)
        self.assertFalse(bitbrowser.overlap_detected.is_set())
        self.assertEqual(
            first_response.json()["revision"],
            second_response.json()["revision"],
        )

    async def test_disconnected_waiter_does_not_start_another_build(self) -> None:
        bitbrowser = _GatedBitBrowser()
        app = create_app(
            self.settings,
            database=self.database,
            bitbrowser=bitbrowser,  # type: ignore[arg-type]
        )
        endpoint = self._snapshot_endpoint(app)
        first_request = _ControllableRequest()
        waiting_request = _ControllableRequest()
        session = (self.owner, self.login["token"])

        first = asyncio.create_task(
            endpoint(
                first_request,
                session,
                limit=10,
                history_limit=10,
            )
        )
        second: asyncio.Task[Any] | None = None
        try:
            await self._wait_for_thread_event(bitbrowser.first_entered)
            second = asyncio.create_task(
                endpoint(
                    waiting_request,
                    session,
                    limit=10,
                    history_limit=10,
                )
            )
            # Direct route invocation keeps both handlers on this event loop.
            # Once scheduled, the second handler can only be suspended on the
            # first owner's lock; it has not begun a build or disconnect check.
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            self.assertEqual(0, waiting_request.disconnect_checks)
            waiting_request.disconnected = True
        finally:
            bitbrowser.release_first.set()

        assert second is not None
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=8.0
        )
        self.assertIsInstance(first_result, JSONResponse)
        self.assertIsInstance(second_result, UpstreamUnavailableError)
        assert isinstance(second_result, UpstreamUnavailableError)
        self.assertEqual(
            "workbench_snapshot_client_disconnected",
            second_result.details.get("reason"),
        )
        self.assertEqual(1, waiting_request.disconnect_checks)
        self.assertEqual(1, bitbrowser.calls)


if __name__ == "__main__":
    unittest.main()
