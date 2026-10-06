from __future__ import annotations

import asyncio
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.bitbrowser_v2 import BitBrowserClientV2
from app.playwright_worker import PlaywrightWorker
from tests.test_bitbrowser_v2 import ScenarioTransport, ok, profile_list, wait_until


class BitBrowserV2WorkerCancellationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_to_thread_open_is_compensated(self) -> None:
        open_entered = threading.Event()
        release_open = threading.Event()
        provider_open = {"value": False}

        def handler(
            _base_url: str,
            path: str,
            _payload: dict[str, object],
        ) -> dict[str, object]:
            if path == "/health":
                return ok()
            if path == "/browser/list":
                return profile_list(
                    {"id": "cancelled-worker", "isOpen": provider_open["value"]}
                )
            if path == "/browser/ports":
                return ok(
                    {"cancelled-worker": 9555} if provider_open["value"] else {}
                )
            if path == "/browser/open":
                open_entered.set()
                if not release_open.wait(timeout=2.0):
                    raise AssertionError("test did not release the delayed open")
                provider_open["value"] = True
                return ok({"id": "cancelled-worker", "port": 9555})
            if path == "/browser/close":
                provider_open["value"] = False
                return ok()
            raise AssertionError(path)

        transport = ScenarioTransport(handler)
        client = BitBrowserClientV2(
            transport=transport,
            base_url="http://127.0.0.1:54345",
            port_candidates=(54345,),
            timeout_seconds=0.1,
            health_interval_seconds=60.0,
            open_timeout_seconds=1.0,
            command_timeout_seconds=2.0,
            shutdown_timeout_seconds=1.0,
        )
        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: None
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        try:
            with patch.dict(
                sys.modules,
                {"playwright": fake_package, "playwright.async_api": fake_async_api},
            ):
                task = asyncio.create_task(
                    PlaywrightWorker(client).connect("cancelled-worker")
                )
                entered = await asyncio.to_thread(open_entered.wait, 1.0)
                self.assertTrue(entered)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release_open.set()
                await asyncio.to_thread(
                    wait_until,
                    lambda: transport.count("/browser/close") == 1,
                    timeout=2.0,
                )
            self.assertFalse(provider_open["value"])
            self.assertEqual(1, transport.count("/browser/open"))
            self.assertEqual(1, transport.count("/browser/close"))
        finally:
            release_open.set()
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
