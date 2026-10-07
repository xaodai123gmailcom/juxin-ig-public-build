"""Replacement-page cleanup must survive failure and late native completion."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from test_parallel_screening_worker import _Page, _connected_parent


class RecoveryCleanupR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_late_page_factories_keep_transport_until_candidate_is_reclaimed(self):
        for role in ('account-home', 'recovery', 'screening'):
            for late in (False, True):
                with self.subTest(role=role, timeout=late):
                    release, entered = asyncio.Event(), asyncio.Event()
                    state = SimpleNamespace(stopped=False, closed=False)
                    async def close():
                        self.assertFalse(state.stopped, 'page cleanup lost its original driver')
                        state.closed = True
                    page = SimpleNamespace(close=close, is_closed=lambda: state.closed)
                    async def create():
                        entered.set()
                        await release.wait()
                        return page
                    parent = _connected_parent(SimpleNamespace(new_page=create), _Page('source'))
                    parent.page_create_timeout_seconds = .01 if late else 1
                    async def stop():
                        state.stopped = True
                    driver = SimpleNamespace(stop=AsyncMock(side_effect=stop))
                    relay = SimpleNamespace(stop=Mock())
                    parent._playwright, parent._cdp_relay = driver, relay
                    operation = (parent.open_account_home_page() if role == 'account-home' else
                                 parent._recover_stalled_profile_page('target') if role == 'recovery' else
                                 parent.create_parallel_screening_worker())
                    opening = asyncio.create_task(operation)
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        if late:
                            await asyncio.gather(opening, return_exceptions=True)
                        await parent.disconnect()
                        driver.stop.assert_not_awaited()
                        relay.stop.assert_not_called()
                        release.set()
                        await asyncio.gather(opening, return_exceptions=True)
                        for _ in range(150):
                            if not parent._late_lifecycle_tasks:
                                break
                            await asyncio.sleep(.01)
                        self.assertTrue(state.closed)
                        self.assertFalse(parent._page_factories)
                        self.assertFalse(parent._late_resource_tasks)
                        self.assertFalse(parent._late_lifecycle_tasks)
                        driver.stop.assert_awaited_once()
                        relay.stop.assert_called_once()
                    finally:
                        release.set()
                        await asyncio.gather(opening, return_exceptions=True)
                        await parent.disconnect()

    async def test_native_close_cancellation_retains_driver_until_close_retry_succeeds(self):
        page = SimpleNamespace(close=AsyncMock(side_effect=asyncio.CancelledError()))
        driver = SimpleNamespace(stop=AsyncMock())
        worker = PlaywrightWorker(None)
        worker.page = worker._worker_owned_page = page
        worker._playwright = driver
        with self.assertRaises(asyncio.CancelledError):
            await worker.disconnect()
        driver.stop.assert_not_awaited()
        self.assertIsNone(worker.page)
        self.assertIs(worker._worker_owned_page, page)
        page.close.side_effect = None
        await worker.disconnect()
        for _ in range(100):
            if not worker._late_lifecycle_tasks:
                break
            await asyncio.sleep(.01)
        driver.stop.assert_awaited_once()
        self.assertIsNone(worker._worker_owned_page)

    async def test_failed_child_close_keeps_transport_alive_and_retries_automatically(self):
        for role in ('source', 'child'):
            with self.subTest(role=role):
                state = SimpleNamespace(stopped=False, attempts=0, closed=False)
                async def close():
                    state.attempts += 1
                    if state.stopped:
                        raise RuntimeError('transport already lost; cannot close native page')
                    if state.attempts == 1:
                        raise RuntimeError('transient native close failure')
                    state.closed = True
                page = SimpleNamespace(close=close, is_closed=lambda: state.closed)
                parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('borrowed'))
                async def stop():
                    self.assertTrue(state.closed, 'driver stopped before its native pages closed')
                    state.stopped = True
                driver = SimpleNamespace(stop=AsyncMock(side_effect=stop))
                parent._playwright = driver
                if role == 'child':
                    await parent.create_parallel_screening_worker()
                else:
                    parent.page = parent._worker_owned_page = page
                await parent.disconnect()
                driver.stop.assert_not_awaited()
                for _ in range(100):
                    if not parent._late_lifecycle_tasks:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(state.closed)
                self.assertTrue(state.stopped)
                self.assertFalse(parent._late_lifecycle_tasks)
                self.assertFalse(parent._screening_worker_pool)
                driver.stop.assert_awaited_once()

    async def test_native_session_cancel_still_closes_retired_page(self):
        page = _Page('retired')
        session = SimpleNamespace(detach=AsyncMock(side_effect=asyncio.CancelledError()))
        worker = PlaywrightWorker(None)
        with self.assertRaises(asyncio.CancelledError):
            await worker._dispose_page_resources(page, session, close_page=True)
        self.assertEqual(1, page.close_calls)

    async def test_native_recovery_cleanup_cancel_still_closes_current_page_and_driver(self):
        current, old = _Page('failed-recovery'), _Page('owned-old')
        bad_session = SimpleNamespace(detach=AsyncMock(side_effect=asyncio.CancelledError()))
        driver = SimpleNamespace(stop=AsyncMock())
        worker = PlaywrightWorker(None)
        worker.page = worker._worker_owned_page = current
        worker._cdp_session = bad_session
        worker._playwright = driver
        worker._pending_recovery_old = (old, None, old)
        with self.assertRaises(asyncio.CancelledError):
            await worker.disconnect()
        self.assertEqual((1, 1), (current.close_calls, old.close_calls))
        driver.stop.assert_awaited_once()

    async def test_account_home_cannot_activate_after_disconnect(self):
        for phase in ('navigation', 'label'):
            with self.subTest(phase=phase):
                entered, release = asyncio.Event(), asyncio.Event()
                page = _Page('account-home')
                page.is_closed = lambda: page.close_calls > 0
                async def blocked(*_args, **_kwargs):
                    entered.set()
                    await release.wait()
                page.goto = blocked if phase == 'navigation' else AsyncMock()
                parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
                if phase == 'label':
                    parent._label_task_page_best_effort = blocked
                opening = asyncio.create_task(parent.open_account_home_page())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await parent.disconnect()
                    release.set()
                    with self.assertRaises(WorkerExecutionError):
                        await asyncio.wait_for(opening, 1)
                    self.assertIsNone(parent.page)
                    self.assertIsNone(parent._worker_owned_page)
                    self.assertEqual(1, page.close_calls)
                finally:
                    release.set()
                    await asyncio.gather(opening, return_exceptions=True)
                    await parent.disconnect()

    async def test_account_home_broken_adapter_never_navigates_or_closes_borrowed_source(self):
        source = _Page('operator')
        source.goto = AsyncMock()
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=source)), source)
        try:
            with self.assertRaises(WorkerExecutionError):
                await parent.open_account_home_page()
            source.goto.assert_not_awaited()
            self.assertEqual(0, source.close_calls)
            self.assertIs(parent.page, source)
        finally:
            await parent.disconnect()

    async def test_recovery_label_reply_after_disconnect_does_not_report_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        page = _Page('replacement')
        page.goto = AsyncMock()
        page.is_closed = lambda: page.close_calls > 0
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
        parent._validate_recovery_profile_page = AsyncMock()
        async def label(*_args):
            entered.set()
            await release.wait()
        parent._label_task_page_best_effort = label
        opening = asyncio.create_task(parent._recover_stalled_profile_page('target'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            # Rollback also refreshes a label; it need not share the delayed reply.
            parent._label_task_page_best_effort = AsyncMock()
            await parent.disconnect()
            release.set()
            self.assertFalse(await asyncio.wait_for(opening, 1))
            self.assertIsNone(parent.page)
            self.assertEqual(1, page.close_calls)
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
            await parent.disconnect()

    async def test_failed_disposal_is_retried_at_disconnect(self):
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError('native close unavailable')
        old = Page('old-recovery-page')
        parent = _connected_parent(SimpleNamespace(), _Page('source'))
        await parent._dispose_page_resources(old, None, close_page=True)
        await parent.disconnect()
        self.assertEqual(2, old.close_calls)

    async def test_failed_old_page_close_prevents_accumulating_replacements(self):
        old = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('still open')))
        context = SimpleNamespace(new_page=AsyncMock(return_value=_Page('new')))
        parent = _connected_parent(context, _Page('source'))
        await parent._dispose_page_resources(old, None, close_page=True)
        self.assertFalse(await parent._recover_stalled_profile_page('target'))
        context.new_page.assert_not_awaited()
        self.assertEqual('browser_operations_pending', parent._last_page_recovery_error.code)
        old.close.side_effect = None
        await parent.disconnect()

    async def test_parent_disconnect_prevents_late_recovery_publication(self):
        entered, release = asyncio.Event(), asyncio.Event()
        page = _Page('replacement')
        async def navigate(*_args, **_kwargs):
            entered.set()
            await release.wait()
        page.goto = navigate
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
        parent._validate_recovery_profile_page = AsyncMock()
        recovering = asyncio.create_task(parent._recover_stalled_profile_page('target'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await parent.disconnect()
            release.set()
            self.assertFalse(await asyncio.wait_for(recovering, 1))
            self.assertIsNone(parent.page)
            self.assertIsNone(parent._pending_recovery_old)
            self.assertEqual(1, page.close_calls)
        finally:
            release.set()
            await asyncio.gather(recovering, return_exceptions=True)
            await parent.disconnect()

    async def test_timed_out_recovery_cannot_close_borrowed_source_returned_late(self):
        source, release = _Page('borrowed'), asyncio.Event()
        async def new_page():
            await release.wait()
            return source
        parent = _connected_parent(SimpleNamespace(new_page=new_page), source)
        parent.page_create_timeout_seconds = .01
        try:
            self.assertFalse(await parent._recover_stalled_profile_page('target'))
            release.set()
            for _ in range(100):
                if not parent._late_lifecycle_tasks:
                    break
                await asyncio.sleep(.001)
            self.assertEqual(0, source.close_calls)
        finally:
            release.set()
            await parent.disconnect()

    async def test_repeated_cleanup_attempts_do_not_duplicate_hostile_close(self):
        release, entered = asyncio.Event(), asyncio.Event()
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
        old = Page('retired')
        worker = PlaywrightWorker(None)
        worker.disconnect_timeout_seconds = .01
        try:
            for _ in range(3):
                await worker._dispose_page_resources(old, None, close_page=True)
            self.assertEqual(1, old.close_calls)
        finally:
            release.set()
            for _ in range(100):
                if not worker._late_lifecycle_tasks:
                    break
                await asyncio.sleep(.001)
            await worker.disconnect()
