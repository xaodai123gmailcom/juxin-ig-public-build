from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class _NoopBitBrowser:
    pass


class _Page:
    def __init__(self, name: str) -> None:
        self.name = name
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _Session:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.detach_calls = 0

    async def send(self, method: str, params: dict[str, Any]) -> None:
        self.commands.append((method, dict(params)))

    async def detach(self) -> None:
        self.detach_calls += 1


class _Context:
    def __init__(self, page: _Page, session: _Session | None = None) -> None:
        self.page = page
        self.session = session
        self.new_page_calls = 0
        self.new_session_calls = 0

    async def new_page(self) -> _Page:
        self.new_page_calls += 1
        return self.page

    async def new_cdp_session(self, page: _Page) -> _Session:
        self.new_session_calls += 1
        if page is not self.page or self.session is None:
            raise RuntimeError("optional CDP session unavailable")
        return self.session


class _ParentOwner:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class _Relay:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _connected_parent(context: Any, operator_page: _Page) -> PlaywrightWorker:
    parent = PlaywrightWorker(_NoopBitBrowser())
    parent._context = context
    parent.page = operator_page
    parent.profile_id = "profile-17"
    parent._connected_endpoint = "ws://127.0.0.1:9222/devtools/browser/id"
    parent._connection_generation = 42
    return parent


class ParallelScreeningWorkerTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_child_owns_only_one_new_page_and_its_session(self) -> None:
        operator_page = _Page("operator")
        screening_page = _Page("screening")
        session = _Session()
        context = _Context(screening_page, session)
        parent = _connected_parent(context, operator_page)
        parent_driver = _ParentOwner()
        parent_browser = object()
        parent_relay = _Relay()
        parent._playwright = parent_driver
        parent._browser = parent_browser
        parent._cdp_relay = parent_relay

        child = await parent.create_parallel_screening_worker()

        self.assertTrue(PlaywrightWorker.supports_parallel_screening_tab)
        self.assertEqual(1, context.new_page_calls)
        self.assertEqual(1, context.new_session_calls)
        self.assertIs(child._context, context)
        self.assertIs(child.page, screening_page)
        self.assertIs(child._worker_owned_page, screening_page)
        self.assertIs(child._cdp_session, session)
        self.assertIsNone(child._playwright)
        self.assertIsNone(child._browser)
        self.assertIsNone(child._cdp_relay)
        self.assertEqual(parent.profile_id, child.profile_id)
        self.assertEqual(parent._connected_endpoint, child._connected_endpoint)
        self.assertEqual(parent._connection_generation, child._connection_generation)
        self.assertIs(
            parent._location_request_coordinator,
            child._location_request_coordinator,
        )
        self.assertEqual(
            [
                ("Page.setWebLifecycleState", {"state": "active"}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ],
            session.commands,
        )

        await child.disconnect()

        self.assertEqual(1, session.detach_calls)
        self.assertEqual(1, screening_page.close_calls)
        self.assertEqual(0, operator_page.close_calls)
        self.assertEqual(0, parent_driver.stop_calls)
        self.assertEqual(0, parent_relay.stop_calls)
        self.assertIs(parent.page, operator_page)
        self.assertIs(parent._context, context)
        self.assertIs(parent._playwright, parent_driver)
        self.assertIs(parent._browser, parent_browser)
        self.assertIs(parent._cdp_relay, parent_relay)

    async def test_two_children_own_independent_pages_and_sessions(self) -> None:
        operator_page = _Page("operator")
        child_pages = [_Page("screening-1"), _Page("screening-2")]
        child_sessions = [_Session(), _Session()]

        class TwoChildContext:
            def __init__(self) -> None:
                self.new_page_calls = 0
                self.new_session_calls = 0

            async def new_page(self) -> _Page:
                page = child_pages[self.new_page_calls]
                self.new_page_calls += 1
                return page

            async def new_cdp_session(self, page: _Page) -> _Session:
                index = child_pages.index(page)
                self.new_session_calls += 1
                return child_sessions[index]

        context = TwoChildContext()
        parent = _connected_parent(context, operator_page)

        first = await parent.create_parallel_screening_worker()
        second = await parent.create_parallel_screening_worker()

        self.assertEqual(2, context.new_page_calls)
        self.assertEqual(2, context.new_session_calls)
        self.assertIs(first.page, child_pages[0])
        self.assertIs(second.page, child_pages[1])
        self.assertIs(first._cdp_session, child_sessions[0])
        self.assertIs(second._cdp_session, child_sessions[1])

        await first.disconnect()
        self.assertEqual(1, child_pages[0].close_calls)
        self.assertEqual(1, child_sessions[0].detach_calls)
        self.assertEqual(0, child_pages[1].close_calls)
        self.assertEqual(0, child_sessions[1].detach_calls)
        self.assertIs(second.page, child_pages[1])
        self.assertIs(parent.page, operator_page)

        await second.disconnect()
        self.assertEqual(1, child_pages[1].close_calls)
        self.assertEqual(1, child_sessions[1].detach_calls)
        self.assertEqual(0, operator_page.close_calls)
        self.assertIs(parent.page, operator_page)

    async def test_optional_session_failure_still_returns_safe_child(self) -> None:
        operator_page = _Page("operator")
        screening_page = _Page("screening")
        context = _Context(screening_page, None)
        parent = _connected_parent(context, operator_page)

        child = await parent.create_parallel_screening_worker()

        self.assertIsNone(child._cdp_session)
        self.assertIs(child.page, screening_page)
        await child.disconnect()
        self.assertEqual(1, screening_page.close_calls)
        self.assertEqual(0, operator_page.close_calls)

    async def test_late_page_after_hard_timeout_is_closed_without_touching_parent(self) -> None:
        operator_page = _Page("operator")
        late_page = _Page("late")
        release_page = asyncio.Event()

        class LateContext:
            async def new_page(self) -> _Page:
                await release_page.wait()
                return late_page

        parent = _connected_parent(LateContext(), operator_page)
        parent.page_create_timeout_seconds = 0.01

        with self.assertRaises(WorkerExecutionError) as raised:
            await parent.create_parallel_screening_worker()
        self.assertEqual("parallel_screening_page_create_timeout", raised.exception.code)
        self.assertEqual(0, late_page.close_calls)

        release_page.set()
        for _ in range(50):
            if late_page.close_calls:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(1, late_page.close_calls)
        self.assertEqual(0, operator_page.close_calls)
        self.assertIs(parent.page, operator_page)

    async def test_cancel_during_session_creation_closes_page_and_detaches_late_session(self) -> None:
        operator_page = _Page("operator")
        screening_page = _Page("screening")
        late_session = _Session()
        session_started = asyncio.Event()
        release_session = asyncio.Event()

        class LateSessionContext:
            async def new_page(self) -> _Page:
                return screening_page

            async def new_cdp_session(self, page: _Page) -> _Session:
                self.page_seen = page
                session_started.set()
                await release_session.wait()
                return late_session

        context = LateSessionContext()
        parent = _connected_parent(context, operator_page)
        creation = asyncio.create_task(parent.create_parallel_screening_worker())
        await asyncio.wait_for(session_started.wait(), timeout=0.5)

        creation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await creation

        self.assertEqual(1, screening_page.close_calls)
        self.assertEqual(0, operator_page.close_calls)
        release_session.set()
        for _ in range(50):
            if late_session.detach_calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, late_session.detach_calls)

    async def test_disconnected_parent_creates_no_page(self) -> None:
        parent = PlaywrightWorker(_NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError) as raised:
            await parent.create_parallel_screening_worker()
        self.assertEqual("worker_not_connected", raised.exception.code)

    async def test_context_returning_operator_page_is_rejected_without_closing_it(self) -> None:
        operator_page = _Page("operator")
        context = _Context(operator_page, _Session())
        parent = _connected_parent(context, operator_page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await parent.create_parallel_screening_worker()

        self.assertEqual(
            "parallel_screening_page_not_isolated",
            raised.exception.code,
        )
        self.assertEqual(0, operator_page.close_calls)
        self.assertIs(parent.page, operator_page)


if __name__ == "__main__":
    unittest.main()
