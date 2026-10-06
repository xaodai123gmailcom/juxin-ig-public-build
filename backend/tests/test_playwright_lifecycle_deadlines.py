from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from app.errors import UpstreamUnavailableError
from app.playwright_worker import PlaywrightWorker


class PlaywrightLifecycleDeadlineTestCase(unittest.IsolatedAsyncioTestCase):
    async def _wait_until(self, predicate, *, timeout: float = 0.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("late lifecycle cleanup did not finish")
            await asyncio.sleep(0.005)

    async def test_hard_deadline_does_not_wait_for_cancellation_acknowledgement(
        self,
    ) -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def cancellation_hostile_call() -> str:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return "settled"

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await worker._await_lifecycle_operation(
                cancellation_hostile_call(),
                timeout=0.01,
            )
        self.assertTrue(entered.is_set())
        self.assertLess(time.monotonic() - started, 0.2)

        release.set()
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_disconnect_has_hard_deadline_for_permanently_wedged_cleanup(
        self,
    ) -> None:
        release = asyncio.Event()

        async def wedged() -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        class Session:
            detach = staticmethod(wedged)

        class Page:
            close = staticmethod(wedged)

        class Driver:
            stop = staticmethod(wedged)

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.disconnect_timeout_seconds = 0.01
        worker._cdp_session = Session()
        worker._worker_owned_page = Page()
        worker.page = worker._worker_owned_page
        worker._playwright = Driver()
        worker._profile_verified_cache["old"] = True
        worker._profile_professional_cache["old"] = True
        worker._profile_category_cache["old"] = "Business"
        worker._profile_external_bio_url_cache["old"] = "https://example.test"

        started = time.monotonic()
        await asyncio.wait_for(worker.disconnect(), timeout=0.3)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIsNone(worker.page)
        self.assertIsNone(worker._cdp_session)
        self.assertIsNone(worker._playwright)
        self.assertFalse(worker._profile_verified_cache)
        self.assertFalse(worker._profile_professional_cache)
        self.assertFalse(worker._profile_category_cache)
        self.assertFalse(worker._profile_external_bio_url_cache)

        release.set()
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_late_cdp_session_is_detached_after_creation_timeout(self) -> None:
        release = asyncio.Event()

        class Session:
            def __init__(self) -> None:
                self.detached = False

            async def detach(self) -> None:
                self.detached = True

        session = Session()

        class Context:
            async def new_cdp_session(self, _page: object) -> Session:
                await release.wait()
                return session

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker._context = Context()
        worker.cdp_session_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.02

        result = await worker._new_active_page_session(object())
        self.assertIsNone(result)
        release.set()
        await self._wait_until(lambda: session.detached)
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_late_recovery_page_is_closed_after_creation_timeout(self) -> None:
        release = asyncio.Event()

        class Page:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        page = Page()

        class Context:
            async def new_page(self) -> Page:
                await release.wait()
                return page

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker._context = Context()
        worker.page_create_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.02

        self.assertFalse(await worker._recover_stalled_profile_page("target"))
        release.set()
        await self._wait_until(lambda: page.closed)
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_raw_cdp_send_timeout_invalidates_session_without_hanging(self) -> None:
        release = asyncio.Event()

        class Session:
            def __init__(self) -> None:
                self.detached = False

            async def send(self, _method: str, _params: dict[str, object]) -> None:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

            async def detach(self) -> None:
                self.detached = True

        session = Session()
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = object()
        worker._cdp_session = session
        worker.cdp_command_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.02

        started = time.monotonic()
        await asyncio.wait_for(worker._ensure_window_surface_stable(), timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertIsNone(worker._cdp_session)
        self.assertTrue(session.detached)

        release.set()
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_playwright_start_timeout_releases_connect_and_stops_late_owner(
        self,
    ) -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        class BitBrowser:
            @staticmethod
            def connection_endpoint(
                _profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del open_if_needed
                return {"ws": "ws://127.0.0.1:9222/devtools/browser/hung"}

        class Driver:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        driver = Driver()

        class Manager:
            async def start(self) -> Driver:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                return driver

            async def __aexit__(self, *_args: object) -> None:
                return None

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: Manager()
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(BitBrowser())  # type: ignore[arg-type]
        worker.playwright_start_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.01
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            started = time.monotonic()
            with self.assertRaises(UpstreamUnavailableError) as raised:
                await asyncio.wait_for(
                    worker.connect("hung-driver", open_if_needed=False),
                    timeout=0.4,
                )
            self.assertLess(time.monotonic() - started, 0.35)
        self.assertTrue(entered.is_set())
        self.assertEqual("playwright_start_failed", raised.exception.details["reason"])

        release.set()
        await self._wait_until(lambda: driver.stopped)
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)

    async def test_relay_start_timeout_releases_connect_and_reclaims_late_relay(
        self,
    ) -> None:
        release = threading.Event()
        entered = threading.Event()

        class Relay:
            failure = None

            def __init__(self) -> None:
                self.stop_count = 0

            def start(self) -> str:
                entered.set()
                release.wait(timeout=1.0)
                return "ws://127.0.0.1:37777/devtools/browser/late"

            def stop(self) -> None:
                self.stop_count += 1

        relay = Relay()

        class BitBrowser:
            @staticmethod
            def connection_endpoint(
                _profile_id: str, *, open_if_needed: bool = True
            ) -> dict[str, object]:
                del open_if_needed
                return {"ws": "ws://127.0.0.1:9222/devtools/browser/hung"}

            @staticmethod
            def create_cdp_relay(_endpoint: str) -> Relay:
                return relay

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []  # type: ignore[attr-defined]
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: None
        fake_package.async_api = fake_async_api  # type: ignore[attr-defined]

        worker = PlaywrightWorker(BitBrowser())  # type: ignore[arg-type]
        worker.relay_start_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.02
        try:
            with patch.dict(
                sys.modules,
                {"playwright": fake_package, "playwright.async_api": fake_async_api},
            ):
                started = time.monotonic()
                with self.assertRaises(UpstreamUnavailableError) as raised:
                    await asyncio.wait_for(
                        worker.connect("hung-relay", open_if_needed=False),
                        timeout=0.25,
                    )
                self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(entered.is_set())
            self.assertEqual(
                "cdp_relay_start_failed", raised.exception.details["reason"]
            )
            self.assertGreaterEqual(relay.stop_count, 1)
        finally:
            release.set()
        await self._wait_until(lambda: relay.stop_count >= 2)
        await self._wait_until(lambda: not worker._late_lifecycle_tasks)


if __name__ == "__main__":
    unittest.main()
