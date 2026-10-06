"""A completed source requires healthy final DOM evidence and durable rows."""
from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.collection_surface import RELATION_ROWS_SCRIPT, relation_scroll_script
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class Worker(PlaywrightWorker):
    def __init__(self):
        super().__init__(None)
        self.logical = 0.0
        self.collection_loading_grace_seconds = 2.0
        self.collection_poll_interval_seconds = 0.0
        self.collection_initial_idle_rounds = 2
        self.collection_settled_idle_rounds = 2
        self.loading = False
        self.failure = None

    def _collection_monotonic(self):
        return self.logical

    async def _guard(self):
        pass

    async def _has_visible_relation_loading_indicator(self, _dialog):
        return self.loading

    async def _relation_surface_failure(self, _dialog):
        return self.failure


class Surface:
    def __init__(self, worker, *, names=("real_row",), recommendations=False,
                 mutate_final=None, advance_error=False, reset_error=False,
                 text="Followers"):
        self.worker = worker
        self.names = list(names)
        self.recommendations = recommendations
        self.mutate_final = mutate_final
        self.advance_error = advance_error
        self.reset_error = reset_error
        self.text = text
        self.measures = 0
        self.reads = 0
        self.height = 1000
        self.events = []

    async def evaluate(self, script):
        if script == RELATION_ROWS_SCRIPT:
            self.worker.logical += .5
            self.reads += 1
            if self.measures >= 2 and self.mutate_final:
                mutate, self.mutate_final = self.mutate_final, None
                mutate(self)
            self.events.append(("read", tuple(self.names)))
            return {"hrefs": [f"/{name}/" for name in self.names],
                    "recommendations_reached": self.recommendations}
        if "relation-action: reset" in script:
            if self.reset_error:
                raise RuntimeError("reset detached during execution")
            return {"valid": True, "top": 0}
        if "relation-action: advance" in script and self.advance_error:
            raise RuntimeError("scroll detached during execution")
        if "relation-action: measure" in script:
            self.measures += 1
            self.events.append(("measure", self.measures))
        return {"valid": True, "bottom": self.height == 1000, "moved": False,
                "height": self.height, "client": 400, "top": 600}

    async def inner_text(self, **_kwargs):
        return self.text


class SourceCompletionR44Tests(unittest.IsolatedAsyncioTestCase):
    async def read(self, worker, surface, *, sink=None, **kwargs):
        saved = set()

        async def persist(batch):
            saved.update(batch)
            surface.events.append(("commit", tuple(batch)))
            return len(saved)

        with patch("app.playwright_worker.asyncio.sleep", new=AsyncMock()):
            result = await asyncio.wait_for(worker._read_visible_account_dialog(
                surface, None, surface_kind="followers",
                candidate_sink=sink or persist, expected_minimum=165, **kwargs,
            ), 3)
        return result, saved

    async def assert_incomplete(self, worker, surface, **kwargs):
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.read(worker, surface, **kwargs)
        self.assertEqual("instagram_followers_list_incomplete", caught.exception.code)

    async def test_late_loader_after_final_row_read_is_not_completion(self):
        for recommendations in (False, True):
            with self.subTest(recommendations=recommendations):
                worker = Worker()
                surface = Surface(worker, recommendations=recommendations,
                    mutate_final=lambda _surface: setattr(worker, "loading", True))
                await self.assert_incomplete(worker, surface)

    async def test_late_geometry_growth_with_same_rows_is_not_completion(self):
        for recommendations in (False, True):
            with self.subTest(recommendations=recommendations):
                worker = Worker()
                surface = Surface(worker, recommendations=recommendations,
                    mutate_final=lambda current: setattr(current, "height", 1600))
                await self.assert_incomplete(worker, surface)

    async def test_failed_advance_cannot_be_hidden_by_later_bottom(self):
        worker = Worker()
        surface = Surface(worker, advance_error=True)
        await self.assert_incomplete(worker, surface)
        self.assertEqual(0, surface.measures)

    async def test_failed_initial_reset_never_reads_middle_rows(self):
        worker = Worker()
        surface = Surface(worker, reset_error=True)
        worker._navigate_profile = AsyncMock()
        worker._visible_relation_count = AsyncMock(return_value=167)
        worker._open_relation_surface = AsyncMock(return_value=surface)
        worker._read_visible_account_dialog = AsyncMock(return_value=["middle"])
        with patch("app.playwright_worker.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(WorkerExecutionError) as caught:
                await worker._collect_relation_once("source", relation="followers", limit=None)
        self.assertEqual("instagram_followers_list_not_rendered", caught.exception.code)
        worker._read_visible_account_dialog.assert_not_awaited()

    async def test_navigation_during_final_frame_does_not_complete_source(self):
        worker = Worker()
        worker.page = SimpleNamespace(url="https://www.instagram.com/source/followers/")
        surface = Surface(worker, mutate_final=lambda _surface: setattr(
            worker.page, "url", "https://www.instagram.com/other/followers/"))
        worker._navigate_profile = AsyncMock()
        worker._visible_relation_count = AsyncMock(return_value=167)
        worker._open_relation_surface = AsyncMock(return_value=surface)
        with patch("app.playwright_worker.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(WorkerExecutionError) as caught:
                await worker._collect_relation_once("source", relation="followers", limit=None)
        self.assertEqual("instagram_followers_list_incomplete", caught.exception.code)

    async def test_empty_copy_does_not_override_source_loading_or_failure(self):
        for loading, failure in ((True, None), (False, "instagram_network_unavailable")):
            with self.subTest(loading=loading, failure=failure):
                worker = Worker()
                worker.loading, worker.failure = loading, failure
                surface = Surface(worker, names=(), text="No followers yet")
                with self.assertRaises(WorkerExecutionError):
                    await self.read(worker, surface)

    async def test_late_candidate_write_failure_propagates_without_completion(self):
        worker = Worker()
        surface = Surface(worker, mutate_final=lambda current: current.names.append("late_row"))
        saved = set()

        async def persist(batch):
            if "late_row" in batch:
                raise RuntimeError("late candidate commit failed")
            saved.update(batch)
            return len(saved)

        with self.assertRaisesRegex(RuntimeError, "late candidate commit failed"):
            await self.read(worker, surface, sink=persist)
        self.assertEqual({"real_row"}, saved)

    async def test_confirmed_recommendation_only_tail_keeps_saved_real_rows(self):
        worker = Worker()
        surface = Surface(worker, recommendations=True,
            mutate_final=lambda current: current.names.clear())
        result, saved = await self.read(worker, surface)
        self.assertEqual([], result)
        self.assertEqual({"real_row"}, saved)

    async def test_healthy_164_rows_complete_despite_167_header_hint(self):
        worker = Worker()
        names = tuple(f"row_{index}" for index in range(164))
        surface = Surface(worker, names=names)
        result, saved = await self.read(worker, surface)
        self.assertEqual([], result)
        self.assertEqual(set(names), saved)

    async def test_late_row_is_durable_before_final_measurement(self):
        worker = Worker()
        surface = Surface(worker, mutate_final=lambda current: current.names.append("late_row"))
        _, saved = await self.read(worker, surface)
        self.assertEqual({"real_row", "late_row"}, saved)
        last_commit = max(i for i, event in enumerate(surface.events) if event[0] == "commit")
        last_measure = max(i for i, event in enumerate(surface.events) if event[0] == "measure")
        self.assertLess(last_commit, last_measure)

    async def test_bounded_relation_without_count_cannot_return_stalled_partial(self):
        for use_sink in (False, True):
            with self.subTest(use_sink=use_sink):
                worker = Worker()
                worker.loading = True
                surface = Surface(worker)
                sink = AsyncMock(return_value=1) if use_sink else None
                with patch("app.playwright_worker.asyncio.sleep", new=AsyncMock()):
                    with self.assertRaises(WorkerExecutionError) as caught:
                        await worker._read_visible_account_dialog(
                            surface, 100, surface_kind="followers", expected_minimum=None,
                            candidate_sink=sink, candidate_total_limit=100 if use_sink else None,
                        )
                self.assertEqual("instagram_followers_list_incomplete", caught.exception.code)

    async def test_navigation_before_rows_are_committed_cannot_contaminate_spool(self):
        worker = Worker()
        worker.page = SimpleNamespace(url="https://www.instagram.com/source/followers/")
        surface = Surface(worker)
        sink = AsyncMock(return_value=1)
        worker.collection_checkpoint = AsyncMock(side_effect=lambda: setattr(
            worker.page, "url", "https://www.instagram.com/other/followers/"))
        await self.assert_incomplete(worker, surface, sink=sink, expected_source_username="source")
        sink.assert_not_awaited()
        self.assertEqual(0, surface.reads)

    async def test_same_source_profile_and_relation_routes_are_valid(self):
        worker = Worker()
        for url in ("https://www.instagram.com/source/", "https://instagram.com/source/followers/?hl=en"):
            with self.subTest(url=url):
                worker.page = SimpleNamespace(url=url)
                worker._assert_relation_source("source", "followers")

    async def test_loader_inspection_failures_cannot_prove_loader_absent(self):
        for stage in ("locator", "count", "visibility"):
            with self.subTest(stage=stage):
                worker = PlaywrightWorker(None)
                locator = SimpleNamespace(count=AsyncMock(return_value=1))
                node = SimpleNamespace(is_visible=AsyncMock(return_value=False))
                locator.nth = lambda _: node
                surface = SimpleNamespace(locator=lambda _: locator)
                if stage == "locator":
                    def broken_locator(_):
                        raise RuntimeError("loader query detached")
                    surface.locator = broken_locator
                elif stage == "count":
                    locator.count.side_effect = RuntimeError("loader count detached")
                else:
                    node.is_visible.side_effect = RuntimeError("loader visibility detached")
                self.assertTrue(await worker._has_visible_relation_loading_indicator(surface))


class SourceScrollerDOMR44Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        runtime = await async_playwright().start()
        self.addAsyncCleanup(runtime.stop)
        executable = os.environ.get("IGAC_TEST_CHROMIUM_EXECUTABLE", runtime.chromium.executable_path)
        if not Path(executable).is_file() and os.name != "nt":
            if os.environ.get("IGAC_REQUIRE_POSTING_BROWSER") == "1":
                self.fail("Required Chromium runtime is missing")
            self.skipTest("Chromium is not installed in this non-Windows environment")
        browser = await runtime.chromium.launch(executable_path=executable)
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()

    async def fixture(self, header=40):
        await self.page.set_content(f'''
          <div id="outer" style="width:400px;height:260px;overflow-y:auto">
            <header style="height:{header}px">
              <a href="/source/">avatar</a><a href="/source/">source</a>
            </header>
            <section id="viewport" style="height:240px;overflow-y:auto">
              <div id="rows" style="height:1000px;position:relative"><a href="/first_real/">first</a></div>
            </section>
          </div>''')
        return self.page.locator("#outer")

    async def test_duplicate_header_links_cannot_prove_an_unscanned_list_ended(self):
        for header, visible, step in ((20, 240, 156), (40, 220, 143), (80, 180, 117)):
            with self.subTest(header=header):
                surface = await self.fixture(header)
                movement = await surface.evaluate(relation_scroll_script("advance"))
                self.assertTrue(movement["valid"])
                self.assertTrue(movement["moved"])
                self.assertFalse(movement["bottom"])
                self.assertEqual(240, movement["client"])
                self.assertEqual(visible, movement["visible_height"])
                self.assertEqual(0, await surface.evaluate("node => node.scrollTop"))
                self.assertEqual(step, movement["top"])
                self.assertEqual(step, await self.page.locator("#viewport").evaluate("node => node.scrollTop"))
                snapshot = await surface.evaluate(RELATION_ROWS_SCRIPT)
                self.assertNotIn("/first_real/", snapshot["hrefs"])
                after = await surface.evaluate(relation_scroll_script("measure"))
                self.assertEqual(1000, after["height"])
                self.assertEqual(step, after["top"])
                self.assertFalse(await Worker()._confirm_relation_list_end(surface))

    async def test_empty_repaint_keeps_incomplete_then_recovers_at_current_position(self):
        surface = await self.fixture(20)
        await surface.evaluate(relation_scroll_script("advance"))
        await self.page.locator("#rows").evaluate("node => node.replaceChildren()")
        gap = await surface.evaluate(relation_scroll_script("measure"))
        self.assertFalse(gap["valid"])
        self.assertFalse(gap["bottom"])
        self.assertFalse(await Worker()._confirm_relation_list_end(surface))
        await self.page.locator("#rows").evaluate('''node => {
          node.innerHTML = '<a href="/next_real/" style="position:absolute;top:200px">next</a>';
        }''')
        restored = await surface.evaluate(relation_scroll_script("measure"))
        self.assertTrue(restored["valid"])
        self.assertFalse(restored["bottom"])
        self.assertEqual(156, restored["top"])
        self.assertEqual(0, await surface.evaluate("node => node.scrollTop"))
        self.assertIn("/next_real/", (await surface.evaluate(RELATION_ROWS_SCRIPT))["hrefs"])


if __name__ == "__main__":
    unittest.main()
