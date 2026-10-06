"""R30 regression tests for late-rendered relation rows and natural completion."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.collection_surface import RELATION_ROWS_SCRIPT


class _EmptyLocator:
    async def count(self):
        return 0


class _EndChangingSurface:
    """Virtualized rows can change while the scroll viewport stays identical."""

    def __init__(self, initial, replacements=()):
        self.names = list(initial)
        self.replacements = list(replacements)
        self.reads = 0
        self.measurements = 0

    def locator(self, selector):
        return self if "a[href" in selector else _EmptyLocator()

    async def evaluate_all(self, expression):
        self.reads += 1
        if self.reads > 20:
            raise AssertionError("unstable terminal rows exceeded their no-progress budget")
        return [f"/{name}/" for name in self.names]

    async def evaluate(self, expression):
        if expression == RELATION_ROWS_SCRIPT:
            return {"hrefs": await self.evaluate_all(expression),
                    "recommendations_reached": False}
        if "relation-action: measure" in expression:
            self.measurements += 1
            was_populated = bool(self.names)
            # The geometry result describes the old frame; rows repaint before
            # the caller finishes waiting and decides whether the scan is done.
            if self.measurements % 2 == 0 and self.replacements:
                self.names = list(self.replacements.pop(0))
            return {
                "valid": True, "bottom": was_populated,
                "height": 800, "client": 400, "top": 400,
            }
        return {"valid": True, "moved": False, "top": 400}

    async def inner_text(self, **kwargs):
        return "Followers"


class BrowserReadsR30Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        worker = PlaywrightWorker(None)
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 1
        worker.collection_initial_idle_rounds = 1
        worker.collection_loading_grace_seconds = 0
        return worker

    async def test_late_final_row_is_saved_before_confirming_unchanged_geometry(self):
        worker = self.worker()
        surface = _EndChangingSurface(["first"], [["first", "late_tail"]])
        saved = set()

        async def sink(batch):
            saved.update(batch)
            return len(saved)

        await worker._read_visible_account_dialog(
            surface, None, candidate_sink=sink, expected_minimum=1,
        )
        self.assertEqual({"first", "late_tail"}, saved)

    async def test_virtualized_tail_replacement_is_consumed_before_completion(self):
        worker = self.worker()
        surface = _EndChangingSurface(["previous_tail"], [["last_account"]])
        saved = set()

        async def sink(batch):
            saved.update(batch)
            return len(saved)

        await worker._read_visible_account_dialog(
            surface, None, candidate_sink=sink, expected_minimum=1,
        )
        self.assertEqual({"previous_tail", "last_account"}, saved)

    async def test_in_memory_unlimited_reader_also_collects_late_tail(self):
        result = await self.worker()._read_visible_account_dialog(
            _EndChangingSurface(["first"], [["first", "last"]]),
            None, expected_minimum=1,
        )
        self.assertEqual(["first", "last"], result)

    async def test_in_memory_unlimited_reader_requires_proven_physical_end(self):
        worker = self.worker()
        worker._confirm_relation_list_end = AsyncMock(return_value=False)
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_dialog(_EndChangingSurface(["first"]), None)
        self.assertEqual("instagram_followers_list_incomplete", raised.exception.code)

    async def test_alternating_already_saved_terminal_rows_cannot_extend_retry_forever(self):
        worker = self.worker()
        surface = _EndChangingSurface(["old_a"], [["old_b"], ["old_a"]] * 10)
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_dialog(
                surface, None, candidate_sink=AsyncMock(return_value=100),
                initial_candidate_count=100, expected_minimum=99,
            )
        self.assertEqual("instagram_followers_list_incomplete", raised.exception.code)
        self.assertLess(surface.reads, 10)

    async def test_budget_exhaustion_still_persists_the_last_observed_new_frame(self):
        worker = self.worker()
        surface = _EndChangingSurface(["old_a"], [["old_b"], ["new_c"]])
        historical = {"old_a", "old_b"}
        saved = set(historical)

        async def sink(batch):
            saved.update(batch)
            return 100 + len(saved - historical)

        try:
            await worker._read_visible_account_dialog(
                surface, None, candidate_sink=sink,
                initial_candidate_count=100, expected_minimum=99,
            )
        except WorkerExecutionError as exc:
            self.assertEqual("instagram_followers_list_incomplete", exc.code)
        # Even an incomplete scan must commit the frame it already observed.
        self.assertEqual({"old_a", "old_b", "new_c"}, saved)

    async def test_disappearing_terminal_rows_are_not_an_empty_success(self):
        with self.assertRaises(WorkerExecutionError) as raised:
            await self.worker()._read_visible_account_dialog(
                _EndChangingSurface(["first"], [[]]), None,
                candidate_sink=AsyncMock(return_value=1), expected_minimum=1,
            )
        self.assertIn(raised.exception.code, {
            "instagram_followers_list_incomplete", "instagram_followers_list_not_rendered",
        })

    async def test_explicit_empty_relation_still_finishes_without_rows(self):
        for known_zero in (True, False):
            with self.subTest(known_zero=known_zero):
                surface = _EndChangingSurface([])
                surface.inner_text = AsyncMock(return_value="No followers yet")
                names = await self.worker()._read_visible_account_dialog(
                    surface, None, expected_minimum=0, known_zero=known_zero,
                )
                self.assertEqual([], names)

    async def test_monitor_final_recommendation_boundary_is_not_an_empty_failure(self):
        worker = self.worker()
        worker._confirm_relation_list_end = AsyncMock(return_value=True)
        reads = 0

        async def read_rows(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 3:
                worker._monitor_recommendations_reached = True
                return []
            return ["/confirmed_following/"]

        worker._read_visible_account_hrefs = read_rows
        names = await worker._read_visible_account_dialog(
            _EndChangingSurface([]), None, expected_minimum=1,
            surface_kind="following", monitor_observation=True,
        )
        self.assertEqual(["confirmed_following"], names)
        self.assertEqual(3, reads)


class RecoveryPageCleanupR30Tests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_failed_child_factory_joins_page_close_before_returning(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_close():
            entered.set()
            await release.wait()
            closed.set()

        candidate = SimpleNamespace(close=slow_close)
        operator_page = SimpleNamespace(close=AsyncMock())
        parent = PlaywrightWorker(None)
        parent._context = SimpleNamespace(new_page=AsyncMock(return_value=candidate))
        parent.page = operator_page
        parent.profile_id = "profile"
        operation = None
        try:
            with patch.object(PlaywrightWorker, "_new_active_page_session",
                              new=AsyncMock(side_effect=RuntimeError("session creation failed"))):
                operation = asyncio.create_task(parent.create_parallel_screening_worker())
                await asyncio.wait_for(entered.wait(), timeout=1)
                operation.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(operation.done(), "factory abandoned a child still closing")
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
            self.assertTrue(closed.is_set())
            operator_page.close.assert_not_awaited()
            self.assertIs(parent.page, operator_page)
        finally:
            release.set()
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)
                await asyncio.wait_for(closed.wait(), timeout=1)

    async def check_cancel_during_recovery_cleanup(self, progressed):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_detach():
            entered.set()
            await release.wait()

        old_page = SimpleNamespace(close=AsyncMock())
        new_page = SimpleNamespace(close=AsyncMock())
        old_session = SimpleNamespace(detach=AsyncMock())
        new_session = SimpleNamespace(detach=AsyncMock())
        disposing_session = old_session if progressed else new_session
        disposing_session.detach = slow_detach
        worker = PlaywrightWorker(None)
        worker._pending_recovery_old = (old_page, old_session, old_page)
        worker._pending_recovery_target = "target"
        worker.page, worker._worker_owned_page = new_page, new_page
        worker._cdp_session = new_session
        operation = asyncio.create_task(worker._finish_page_recovery(progressed=progressed))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()  # Repeated Stop must not cut the cleanup short.
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            closed = old_page if progressed else new_page
            retained = new_page if progressed else old_page
            closed.close.assert_awaited_once()
            retained.close.assert_not_awaited()
            self.assertIs(worker.page, retained)
            self.assertIsNone(worker._pending_recovery_old)
        finally:
            release.set()
            if not operation.done():
                await asyncio.gather(operation, return_exceptions=True)

    async def test_cancel_after_replacement_progress_still_closes_old_page(self):
        await self.check_cancel_during_recovery_cleanup(True)

    async def test_cancel_during_replacement_rollback_closes_failed_page_only(self):
        await self.check_cancel_during_recovery_cleanup(False)


if __name__ == "__main__":
    unittest.main()
