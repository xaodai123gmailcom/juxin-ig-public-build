"""R25 interruption regressions with controlled page adapters, not real Chromium."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import (
    PlaywrightWorker, WorkerExecutionError, VisibleProfile,
    classify_profile_visibility, extract_visible_metrics, extract_public_empty_profile_metrics,
)


class Node:
    def __init__(self, text="", *, present=False, notices=()):
        self.text, self.present, self.notices = text, present, notices
        self.first = self

    async def count(self):
        return int(self.present)

    def nth(self, index):
        return self

    async def is_visible(self):
        return self.present

    async def inner_text(self, **kwargs):
        return self.text

    async def get_attribute(self, name):
        return None

    async def evaluate(self, expression):
        return list(self.notices)

    def locator(self, selector):
        return Node()

    async def wait_for(self, **kwargs):
        return None


class BrowserRecoveryR25Tests(unittest.IsolatedAsyncioTestCase):
    def zero_worker(self, *, body=None, url="https://www.instagram.com/sample_unicode01/", private=False, notices=()):
        body = body or (
            "sample_unicode01\nMẫu Kiểm Thử\n0帖子3粉丝48关注\n关注\n"
            "这里空荡荡~\n为你推荐\nsuggested\n900帖子800粉丝700关注"
        )
        worker = PlaywrightWorker(None)
        header = Node("sample_unicode01\nMẫu Kiểm Thử", present=True)
        worker.page = SimpleNamespace(
            url=url,
            locator=lambda selector: Node(body, present=True, notices=notices) if selector == "body" else Node(),
        )
        worker._navigate_profile_with_privacy = AsyncMock(return_value=("sample_unicode01", True if private else None))
        worker._visible_profile_header = AsyncMock(return_value=header)
        worker._visible_external_bio_url = AsyncMock(return_value=None)
        worker._visible_profile_stats_text = AsyncMock(return_value="")
        worker._has_visible_private_indicator = AsyncMock(return_value=private)
        worker._has_visible_loading_indicator = AsyncMock(return_value=True)
        worker._has_visible_target_story = AsyncMock(return_value=False)
        worker._recover_stalled_profile_page = AsyncMock(return_value=False)
        worker._read_inline_profile_evidence = AsyncMock(side_effect=AssertionError("ready zero profile must not repeat evidence reads"))
        worker._visible_unpinned_post_urls = AsyncMock(side_effect=AssertionError("zero posts must not wait for a grid"))
        return worker

    def test_compact_chinese_counters_match_synthetic_fixture(self):
        for text, expected in (
            ("0帖子3粉丝48关注", (3, 48, 0)),
            ("0貼文23粉絲8追蹤", (23, 8, 0)),
            ("12帖子1.2万粉丝48关注", (12000, 48, 12)),
            ("0帖子 3粉丝 48关注", (3, 48, 0)),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, extract_visible_metrics(text))

    def test_counter_boundary_does_not_read_unrelated_words(self):
        self.assertEqual((None, None, None), extract_visible_metrics("0帖子收藏 50粉丝团 20关注者"))
        self.assertEqual((None, None, None), extract_visible_metrics("12postscript 34followersclub 56followingme"))

    async def test_compact_zero_posts_finishes_without_replacement_or_grid_wait(self):
        worker = self.zero_worker()
        result = await worker.read_visible_profile("sample_unicode01", include_activity=True)
        self.assertEqual(("public", 3, 48, 0, "no_posts"),
            (result.visibility, result.followers, result.following, result.posts, result.activity_status))
        worker._navigate_profile_with_privacy.assert_awaited_once()
        worker._recover_stalled_profile_page.assert_not_awaited()
        worker._visible_unpinned_post_urls.assert_not_awaited()

    async def test_private_compact_zero_profile_stays_private(self):
        worker = self.zero_worker(private=True, body="sample_unicode01\n0帖子3粉丝48关注\n这是私密账户\n这里空荡荡~")
        worker._visible_profile_header = AsyncMock(return_value=Node("sample_unicode01\n0帖子3粉丝48关注", present=True))
        result = await worker.read_visible_profile("sample_unicode01", include_activity=True)
        self.assertEqual(("private", 0), (result.visibility, result.posts))
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_zero_posts_cannot_hide_network_failure_or_wrong_identity(self):
        offline = self.zero_worker(notices=["This site can't be reached ERR_INTERNET_DISCONNECTED"])
        with self.assertRaises(WorkerExecutionError) as caught:
            await offline.read_visible_profile("sample_unicode01")
        self.assertEqual("instagram_network_unavailable", caught.exception.details["original_reason"])
        wrong = self.zero_worker(url="https://www.instagram.com/another/")
        wrong._has_visible_loading_indicator = AsyncMock(return_value=False)
        with self.assertRaises(WorkerExecutionError) as caught:
            await wrong.read_visible_profile("sample_unicode01")
        self.assertEqual("instagram_content_not_visible", caught.exception.code)
        wrong._recover_stalled_profile_page.assert_not_awaited()

    def test_empty_grid_cannot_borrow_missing_counts_from_recommendations(self):
        for text in (
            "target\n0帖子\n这里空荡荡~\n为你推荐\nother\n100粉丝 200关注",
            "target\n0 posts\nNo posts yet\nSuggested for you\nother\n100 followers 200 following",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_public_empty_profile_metrics(text))
        self.assertEqual("unknown", classify_profile_visibility(
            "target\n这里空荡荡~\n为你推荐\nother\n0帖子 100粉丝 200关注",
            has_visible_posts=False,
        ))

    def test_recovery_wrapper_preserves_transport_and_nested_reason(self):
        for cause, expected in (
            (RuntimeError("net::ERR_INTERNET_DISCONNECTED"), "instagram_network_unavailable"),
            (RuntimeError("Target page, context or browser has been closed"), "worker_not_connected"),
            (WorkerExecutionError("temporarily loading", reason="instagram_profile_not_ready"), "instagram_profile_not_ready"),
        ):
            with self.subTest(expected=expected):
                first = PlaywrightWorker._page_recovery_exhausted(cause, "target")
                second = PlaywrightWorker._page_recovery_exhausted(first)
                self.assertEqual(expected, first.details["original_reason"])
                self.assertEqual(expected, second.details["original_reason"])
                self.assertEqual("target", second.details["recovery_target"])
                self.assertFalse(first.details["auto_retry"], "only the owning manager may schedule the next round")

    def test_recovery_wrapper_preserves_valid_cooldown(self):
        cause = WorkerExecutionError("wait for shared location cooldown", reason="instagram_location_temporarily_unavailable", retry_after_seconds=180)
        error = PlaywrightWorker._page_recovery_exhausted(cause, "target")
        nested = PlaywrightWorker._page_recovery_exhausted(error)
        self.assertEqual(180, error.details.get("retry_after_seconds"))
        self.assertEqual(180, nested.details.get("retry_after_seconds"))
        for invalid in (True, float("nan"), float("inf"), -5, "180"):
            cause.details["retry_after_seconds"] = invalid
            self.assertNotIn("retry_after_seconds", PlaywrightWorker._page_recovery_exhausted(cause).details)

    async def test_new_page_creation_timeout_remains_a_temporary_surface_failure(self):
        worker = PlaywrightWorker(None)
        old = SimpleNamespace(close=AsyncMock())
        worker.page = old
        worker._context = SimpleNamespace(new_page=AsyncMock(side_effect=TimeoutError("page creation deadline")))
        worker._read_visible_profile_once = AsyncMock(side_effect=WorkerExecutionError("old page blank", reason="instagram_profile_not_ready"))
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker.read_visible_profile("target")
        self.assertEqual("browser_window_surface_unstable", caught.exception.details["original_reason"])
        self.assertIs(old, worker.page)
        old.close.assert_not_awaited()
        worker._context.new_page.assert_awaited_once()

    async def test_successful_replacement_resets_only_its_own_target_budget(self):
        worker = PlaywrightWorker(None)
        worker._page_recovery_targets["other"] = None
        worker._recover_stalled_profile_page = AsyncMock(return_value=True)
        worker._read_visible_profile_once = AsyncMock(side_effect=[
            WorkerExecutionError("blank", reason="instagram_profile_not_ready"),
            VisibleProfile("target", "public", 10, 20, 0),
            WorkerExecutionError("another blank", reason="instagram_profile_not_ready"),
            VisibleProfile("target", "public", 10, 20, 0),
        ])
        await worker.read_visible_profile("target")
        await worker.read_visible_profile("target")
        self.assertEqual(2, worker._recover_stalled_profile_page.await_count)
        self.assertEqual({"other": None}, worker._page_recovery_targets)

    async def test_authoritative_guard_on_replacement_stays_precise(self):
        for reason in ("instagram_login_required", "instagram_challenge", "instagram_rate_limited", "instagram_action_blocked"):
            with self.subTest(reason=reason):
                worker = PlaywrightWorker(None)
                worker._read_visible_profile_once = AsyncMock(side_effect=WorkerExecutionError("blank", reason="instagram_profile_not_ready"))
                worker._recover_stalled_profile_page = AsyncMock(side_effect=WorkerExecutionError("guard", reason=reason))
                with self.assertRaises(WorkerExecutionError) as caught:
                    await worker.read_visible_profile("target")
                self.assertEqual(reason, caught.exception.code)
                worker._recover_stalled_profile_page.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
