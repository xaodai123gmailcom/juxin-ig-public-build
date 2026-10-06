"""R37 profile-read regressions with controlled DOM adapters, not Chromium.

Navigation is isolated here; the production privacy scan and profile reader run.
The SVG snapshot returns the browser-side visible-title projection. Rendering and
the JavaScript visibility predicate require the Windows embedded-browser gate.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_browser_recovery_r25 as browser_r25
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class TitleCollection(browser_r25.Node):
    def __init__(self, titles=(), *, fallback=False, failure=None, pending=False):
        super().__init__(present=True)
        # Pairs represent title text and the containing SVG's rendered visibility.
        self.titles = tuple(titles)
        self.fallback, self.failure, self.pending = fallback, failure, pending
        self.snapshot_calls = 0
        self.legacy_reads = []
        self.entered = asyncio.Event()

    async def evaluate_all(self, expression):
        self.snapshot_calls += 1
        if self.fallback:
            raise AttributeError("old adapter has no evaluate_all")
        self.entered.set()
        if self.failure:
            raise self.failure
        if self.pending:
            await asyncio.Event().wait()
        return [text for text, visible in self.titles[:40] if visible]

    def nth(self, index):
        owner = self

        class DetachedTitle(browser_r25.Node):
            async def text_content(self, **kwargs):
                timeout = kwargs.get("timeout")
                owner.legacy_reads.append(timeout)
                if timeout is not None and 0 < timeout <= 1000:
                    raise TimeoutError("title detached after count")
                # Playwright normally auto-waits up to its default timeout here.
                # A held event exposes that wait deterministically without 30 s tests.
                await asyncio.Event().wait()

        return DetachedTitle(present=True)


class ZeroPostReadsR37Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These frozen adapters never render a later state. Exercise their exact
        # evidence/error outcomes without spending the real DOM settle window;
        # r45 Chromium fixtures separately prove delayed same-page rendering.
        settle = patch("app.playwright_worker._PROFILE_DATA_SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)

    def worker(self, titles):
        body = (
            "sample_empty_02\n0帖子24粉丝36关注\n"
            "Sample profile, test content. Text, commas, and hyphen-test notes.\n"
            "这里空荡荡~\n为你推荐\nsuggested\n900帖子800粉丝700关注"
        )
        worker = browser_r25.BrowserRecoveryR25Tests().zero_worker(
            body=body, url="https://www.instagram.com/sample_empty_02/"
        )
        worker._navigate_profile_with_privacy = AsyncMock(return_value=("sample_empty_02", None))
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\nsample.display", present=True,
        ))
        original_locator = worker.page.locator
        worker.page.locator = lambda selector: (
            titles if selector == "main header svg title, main svg title"
            else original_locator(selector)
        )
        # Keep the actual scan; earlier zero-post fixtures bypassed this stage.
        worker._has_visible_private_indicator = (
            PlaywrightWorker._has_visible_private_indicator.__get__(worker, PlaywrightWorker)
        )
        worker._has_visible_loading_indicator = AsyncMock(return_value=False)
        return worker

    async def test_detached_recommendation_title_does_not_stall_zero_profile(self):
        titles = TitleCollection((("Camera", True),))
        worker = self.worker(titles)
        result = await asyncio.wait_for(
            worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
        )
        self.assertEqual(("public", 24, 36, 0, "no_posts"), (
            result.visibility, result.followers, result.following,
            result.posts, result.activity_status,
        ))
        self.assertEqual(1, titles.snapshot_calls)
        self.assertEqual([], titles.legacy_reads)
        worker._recover_stalled_profile_page.assert_not_awaited()
        worker._visible_unpinned_post_urls.assert_not_awaited()

    async def test_visible_private_icon_wins_over_empty_public_copy(self):
        titles = TitleCollection((("Private account", True),))
        worker = self.worker(titles)
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\n0帖子24粉丝36关注", present=True,
        ))
        result = await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual(("private", 0), (result.visibility, result.posts))
        self.assertEqual([], titles.legacy_reads)

    async def test_hidden_private_icon_is_not_profile_privacy_evidence(self):
        titles = TitleCollection((("Private account", False), ("Camera", True)))
        result = await asyncio.wait_for(
            self.worker(titles).read_visible_profile("sample_empty_02"), 1.0
        )
        self.assertEqual(("public", 0), (result.visibility, result.posts))
        self.assertEqual([], titles.legacy_reads)

    async def test_legacy_adapter_detached_title_has_bounded_wait(self):
        titles = TitleCollection(fallback=True)
        result = await asyncio.wait_for(
            self.worker(titles).read_visible_profile("sample_empty_02"), 1.0
        )
        self.assertEqual(("public", 0), (result.visibility, result.posts))
        self.assertTrue(titles.legacy_reads)
        self.assertTrue(all(timeout is not None and 0 < timeout <= 1000 for timeout in titles.legacy_reads))

    async def test_browser_closed_during_snapshot_does_not_become_public(self):
        worker = self.worker(TitleCollection(failure=RuntimeError(
            "Target page, context or browser has been closed"
        )))
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual("worker_not_connected", caught.exception.code)
        self.assertNotIn("sample_empty_02", worker._profile_base_cache)

    async def test_stop_cancels_snapshot_without_caching_partial_profile(self):
        titles = TitleCollection(pending=True)
        worker = self.worker(titles)
        task = asyncio.create_task(worker.read_visible_profile("sample_empty_02"))
        try:
            await asyncio.wait_for(titles.entered.wait(), 1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn("sample_empty_02", worker._profile_base_cache)
            self.assertEqual([], titles.legacy_reads)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unreadable_snapshot_keeps_zero_profile_pending(self):
        worker = self.worker(TitleCollection(failure=RuntimeError(
            "DOM snapshot invalidated while evaluating"
        )))
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual("instagram_page_recovery_exhausted", caught.exception.code)
        self.assertEqual("instagram_profile_not_ready", caught.exception.details["original_reason"])
        worker._recover_stalled_profile_page.assert_awaited_once()
        self.assertNotIn("sample_empty_02", worker._profile_base_cache)

    async def test_legacy_detached_title_does_not_hide_later_private_icon(self):
        class LegacyMixedTitles(TitleCollection):
            async def count(self):
                return 2

            def nth(self, index):
                if index == 0:
                    return super().nth(index)
                owner = self

                class PrivateTitle(browser_r25.Node):
                    async def text_content(self, **kwargs):
                        owner.legacy_reads.append(kwargs.get("timeout"))
                        return "Private account"

                    def locator(self, selector):
                        return browser_r25.Node(present=True)

                return PrivateTitle(present=True)

        titles = LegacyMixedTitles(fallback=True)
        worker = self.worker(titles)
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\n0帖子24粉丝36关注", present=True,
        ))
        result = await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual(("private", 0), (result.visibility, result.posts))
        self.assertEqual(2, len(titles.legacy_reads))
        self.assertTrue(all(timeout is not None and 0 < timeout <= 1000 for timeout in titles.legacy_reads))

    def private_worker(self, *, posts=0, structured_private=True, body=None, notices=()):
        worker = self.worker(TitleCollection())
        body = body if body is not None else (
            f"sample_empty_02\nsample.display\n{posts}帖子24粉丝36关注\n"
            "这是私密账户\n关注即可查看其照片和视频。\n"
            "这里空荡荡~\n为你推荐\nsuggested\n900帖子800粉丝700关注"
        )
        original_locator = worker.page.locator
        worker.page.locator = lambda selector: (
            browser_r25.Node(body, present=True, notices=notices)
            if selector == "body" else original_locator(selector)
        )
        worker._navigate_profile_with_privacy = AsyncMock(
            return_value=("sample_empty_02", structured_private)
        )
        worker._has_visible_loading_indicator = AsyncMock(return_value=True)
        return worker

    async def test_private_body_counters_terminate_without_grid_or_recovery(self):
        for posts in (0, 12):
            for structured_private in (True, None):
                with self.subTest(posts=posts, structured_private=structured_private):
                    worker = self.private_worker(posts=posts, structured_private=structured_private)
                    result = await asyncio.wait_for(
                        worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
                    )
                    self.assertEqual(("private", 24, 36, posts), (
                        result.visibility, result.followers, result.following, result.posts,
                    ))
                    self.assertEqual("no_posts" if posts == 0 else "private_not_visible", result.activity_status)
                    worker._navigate_profile_with_privacy.assert_awaited_once()
                    worker._recover_stalled_profile_page.assert_not_awaited()
                    worker._visible_unpinned_post_urls.assert_not_awaited()
                    worker._read_inline_profile_evidence.assert_not_awaited()

    async def test_private_body_missing_counts_cannot_borrow_recommendation_metrics(self):
        for counter in ("0帖子", "12帖子24粉丝", ""):
            with self.subTest(counter=counter):
                worker = self.private_worker(body=(
                    f"sample_empty_02\n{counter}\n这是私密账户\n"
                    "关注即可查看其照片和视频。\n为你推荐\n"
                    "suggested\n900帖子800粉丝700关注"
                ))
                with self.assertRaises(WorkerExecutionError) as caught:
                    await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
                self.assertEqual("instagram_page_recovery_exhausted", caught.exception.code)
                self.assertNotIn("sample_empty_02", worker._profile_base_cache)

    async def test_private_unknown_post_count_does_not_wait_for_post_grid(self):
        worker = self.private_worker(body="sample_empty_02\n24粉丝36关注\n这是私密账户")
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\n24粉丝36关注", present=True,
        ))
        worker._has_visible_loading_indicator = AsyncMock(return_value=False)
        result = await asyncio.wait_for(
            worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
        )
        self.assertEqual(("private", None, "private_not_visible"), (
            result.visibility, result.posts, result.activity_status,
        ))
        worker._visible_unpinned_post_urls.assert_not_awaited()
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_private_body_terminal_counts_cannot_hide_network_or_guard(self):
        worker = self.private_worker(notices=["This site can't be reached ERR_INTERNET_DISCONNECTED"])
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual("instagram_network_unavailable", caught.exception.details["original_reason"])
        self.assertNotIn("sample_empty_02", worker._profile_base_cache)

        worker = self.private_worker()
        worker.page.url = "https://www.instagram.com/accounts/login/"
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual("instagram_login_required", caught.exception.code)
        self.assertNotIn("sample_empty_02", worker._profile_base_cache)
        worker._recover_stalled_profile_page.assert_not_awaited()

    @staticmethod
    def add_stale_meta(worker):
        class StaleMeta(browser_r25.Node):
            def __init__(self):
                super().__init__(present=True)

            async def get_attribute(self, name):
                return "99 posts 999 followers 888 following"

        original_locator = worker.page.locator
        worker.page.locator = lambda selector: (
            StaleMeta() if selector == 'meta[property="og:description"], meta[name="description"]'
            else original_locator(selector)
        )

    async def test_zero_body_counters_override_stale_metadata_before_activity(self):
        worker = self.worker(TitleCollection())
        self.add_stale_meta(worker)
        result = await asyncio.wait_for(
            worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
        )
        self.assertEqual(("public", 24, 36, 0, "no_posts"), (
            result.visibility, result.followers, result.following,
            result.posts, result.activity_status,
        ))
        worker._visible_unpinned_post_urls.assert_not_awaited()
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_visible_chinese_header_counters_beat_english_metadata(self):
        worker = self.worker(TitleCollection())
        self.add_stale_meta(worker)
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\n0帖子24粉丝36关注", present=True,
        ))
        result = await asyncio.wait_for(
            worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
        )
        self.assertEqual((24, 36, 0, "no_posts"), (
            result.followers, result.following, result.posts, result.activity_status,
        ))
        worker._visible_unpinned_post_urls.assert_not_awaited()

    async def test_private_terminal_body_counters_override_stale_metadata(self):
        for posts in (0, 12):
            with self.subTest(posts=posts):
                worker = self.private_worker(posts=posts)
                self.add_stale_meta(worker)
                result = await asyncio.wait_for(
                    worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
                )
                self.assertEqual(("private", 24, 36, posts), (
                    result.visibility, result.followers, result.following, result.posts,
                ))
                self.assertEqual("no_posts" if posts == 0 else "private_not_visible", result.activity_status)
                worker._visible_unpinned_post_urls.assert_not_awaited()
                worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_visible_header_is_not_overwritten_by_conflicting_body_counters(self):
        worker = self.worker(TitleCollection())
        worker._visible_profile_header = AsyncMock(return_value=browser_r25.Node(
            "sample_empty_02\n7帖子25粉丝35关注", present=True,
        ))
        result = await asyncio.wait_for(worker.read_visible_profile("sample_empty_02"), 1.0)
        self.assertEqual((25, 35, 7), (result.followers, result.following, result.posts))

    async def test_metadata_counts_remain_fallback_without_visible_counters(self):
        worker = self.private_worker(body="sample_empty_02\n这是私密账户")
        self.add_stale_meta(worker)
        result = await asyncio.wait_for(
            worker.read_visible_profile("sample_empty_02", include_activity=True), 1.0
        )
        self.assertEqual(("private", 999, 888, 99, "private_not_visible"), (
            result.visibility, result.followers, result.following,
            result.posts, result.activity_status,
        ))
        worker._recover_stalled_profile_page.assert_not_awaited()
        worker._visible_unpinned_post_urls.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
