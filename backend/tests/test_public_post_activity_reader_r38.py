"""R38 post-age reading through the production worker with controlled DOM adapters.

These tests exercise cached reads, exact post selection and timestamp parsing without
launching Chromium or contacting Instagram. Native embedded-browser coverage is a
separate Windows build gate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import (
    EmbeddedProfileEvidence, PlaywrightWorker, VisibleProfile, extract_embedded_profile_evidence,
)


class Tile:
    def __init__(self, href, *, pinned=False):
        self.href, self.pinned = href, pinned

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def evaluate(self, _expression):
        return "Pinned post 置顶" if self.pinned else ""


class Tiles:
    def __init__(self, nodes):
        self.nodes = nodes

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class Page:
    url = "https://www.instagram.com/target/"

    def __init__(self, tiles):
        self.tiles = tiles

    def locator(self, _selector):
        return Tiles(self.tiles)


class PublicPostActivityReaderR38Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self, *, story=False, post_age=45, tiles=None):
        worker = PlaywrightWorker(object())
        worker.page = Page(tiles if tiles is not None else [Tile("/p/NEWEST/")])
        worker._has_visible_target_story = AsyncMock(return_value=story)
        worker._navigate_profile_with_privacy = AsyncMock(
            side_effect=AssertionError("An exact cached profile must not navigate again")
        )
        worker._replace_stuck_page_once = AsyncMock(
            side_effect=AssertionError("Terminal/business outcomes must not restart a page")
        )

        async def read_time(_url, *, now):
            return None if post_age is None else now - timedelta(days=post_age, hours=1)

        worker._read_original_post_datetime = AsyncMock(side_effect=read_time)
        return worker

    async def read(self, worker, profile=None, *, include_activity=True):
        worker._remember_profile("target", profile or VisibleProfile("target", "public", 10, 20, 4))
        return await asyncio.wait_for(worker.read_visible_profile(
            "target", include_activity=include_activity, include_post_activity=True,
        ), 1.0)

    async def test_story_today_does_not_reset_old_post_age(self):
        worker = self.worker(story=True, post_age=45)
        result = await self.read(worker)
        self.assertEqual((0, "story_today"), (result.activity_days, result.activity_status))
        self.assertEqual((45, "identified"), (result.post_activity_days, result.post_activity_status))
        self.assertIsNotNone(result.recent_post_datetime)
        worker._read_original_post_datetime.assert_awaited_once()
        self.assertEqual(45, result.as_dict()["post_activity_days"])

    async def test_default_account_activity_keeps_story_shortcut(self):
        worker = self.worker(story=True)
        worker._remember_profile("target", VisibleProfile("target", "public", 10, 20, 4))
        result = await worker.read_visible_profile("target", include_activity=True)
        self.assertEqual((0, "story_today", None, "not_checked"), (
            result.activity_days, result.activity_status,
            result.post_activity_days, result.post_activity_status,
        ))
        worker._read_original_post_datetime.assert_not_awaited()

    async def test_cached_story_is_revisited_once_for_explicit_post_request(self):
        worker = self.worker(story=True, post_age=60)
        profile = VisibleProfile("target", "public", 10, 20, 4, activity_days=0, activity_status="story_today")
        result = await self.read(worker, profile)
        self.assertEqual(60, result.post_activity_days)
        cached = await worker.read_visible_profile("target", include_activity=True, include_post_activity=True)
        self.assertEqual((0, 60, "identified"), (cached.activity_days, cached.post_activity_days, cached.post_activity_status))
        worker._read_original_post_datetime.assert_awaited_once()

    async def test_post_request_works_without_general_activity_flag(self):
        worker = self.worker(post_age=12)
        result = await self.read(worker, include_activity=False)
        self.assertEqual((12, "identified"), (result.post_activity_days, result.post_activity_status))

    async def test_unpinned_latest_post_wins_over_pinned_old_inline_evidence(self):
        worker = self.worker(post_age=2, tiles=[Tile("/p/OLD_PINNED/", pinned=True), Tile("/p/NEWEST/")])
        worker._profile_post_datetime_cache["target"] = (
            (datetime.now(timezone.utc) - timedelta(days=300)).isoformat(),
        )
        result = await self.read(worker)
        self.assertEqual(2, result.post_activity_days)
        self.assertEqual("https://www.instagram.com/p/NEWEST/", worker._read_original_post_datetime.await_args.args[0])
        worker._read_original_post_datetime.assert_awaited_once()

    async def test_already_identified_post_specific_cache_is_reused_without_new_reads(self):
        worker = self.worker(story=True)
        timestamp = (datetime.now(timezone.utc) - timedelta(days=30, hours=1)).isoformat()
        profile = VisibleProfile("target", "public", 10, 20, 4, recent_post_datetime=timestamp, activity_days=0, activity_status="story_today", post_activity_days=30, post_activity_status="identified")
        result = await self.read(worker, profile)
        self.assertEqual((0, "story_today", 30, "identified"), (
            result.activity_days, result.activity_status, result.post_activity_days, result.post_activity_status,
        ))
        worker._has_visible_target_story.assert_not_awaited()
        worker._read_original_post_datetime.assert_not_awaited()

    async def test_general_activity_datetime_cannot_bypass_first_post_specific_read(self):
        worker = self.worker(post_age=2, tiles=[Tile("/p/PIN/", pinned=True), Tile("/p/NEWEST/")])
        timestamp = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
        profile = VisibleProfile("target", "public", 10, 20, 4, recent_post_datetime=timestamp, activity_days=300, activity_status="identified")
        worker._profile_post_datetime_cache["target"] = (timestamp,)
        result = await self.read(worker, profile)
        self.assertEqual((2, "identified"), (result.post_activity_days, result.post_activity_status))
        worker._read_original_post_datetime.assert_awaited_once()
        self.assertEqual("https://www.instagram.com/p/NEWEST/", worker._read_original_post_datetime.await_args.args[0])

    async def test_zero_posts_returns_immediately_even_when_story_exists(self):
        worker = self.worker(story=True)
        result = await self.read(worker, VisibleProfile("target", "public", 10, 20, 0))
        self.assertEqual((None, "no_posts"), (result.post_activity_days, result.post_activity_status))
        worker._has_visible_target_story.assert_not_awaited()
        worker._read_original_post_datetime.assert_not_awaited()
        worker._replace_stuck_page_once.assert_not_awaited()

    async def test_private_returns_immediately_even_with_cached_media(self):
        worker = self.worker(story=True)
        worker._profile_post_datetime_cache["target"] = (datetime.now(timezone.utc).isoformat(),)
        worker._remember_profile("target", VisibleProfile("target", "private", 10, 20, 4), ("https://www.instagram.com/p/OLD/",))
        result = await asyncio.wait_for(worker.read_visible_profile("target", include_activity=True, include_post_activity=True), 1.0)
        self.assertEqual((None, "private_not_visible"), (result.post_activity_days, result.post_activity_status))
        worker._has_visible_target_story.assert_not_awaited()
        worker._read_original_post_datetime.assert_not_awaited()
        worker._replace_stuck_page_once.assert_not_awaited()

    async def test_unknown_timestamp_is_not_zero_despite_story(self):
        worker = self.worker(story=True, post_age=None)
        result = await self.read(worker)
        self.assertEqual((0, "story_today"), (result.activity_days, result.activity_status))
        self.assertEqual((None, "timestamp_unavailable", None), (
            result.post_activity_days, result.post_activity_status, result.recent_post_datetime,
        ))

    async def test_unreadable_latest_post_is_not_replaced_by_old_pinned_time(self):
        worker = self.worker(post_age=None, tiles=[Tile("/p/PIN/", pinned=True), Tile("/p/NEWEST/")])
        worker._profile_post_datetime_cache["target"] = ((datetime.now(timezone.utc) - timedelta(days=500)).isoformat(),)
        result = await self.read(worker)
        self.assertEqual((None, "timestamp_unavailable"), (result.post_activity_days, result.post_activity_status))

    async def test_incomplete_pinned_only_inline_evidence_is_unknown_not_old(self):
        worker = self.worker(tiles=[Tile("/p/PIN/", pinned=True)])
        worker._profile_post_datetime_cache["target"] = ((datetime.now(timezone.utc) - timedelta(days=500)).isoformat(),)
        worker._read_inline_profile_evidence = AsyncMock(return_value=EmbeddedProfileEvidence())
        result = await self.read(worker)
        self.assertEqual((None, "latest_post_unconfirmed"), (result.post_activity_days, result.post_activity_status))
        worker._read_original_post_datetime.assert_not_awaited()
        worker._replace_stuck_page_once.assert_not_awaited()

    async def test_complete_single_post_timestamp_needs_no_post_navigation(self):
        worker = self.worker(tiles=[Tile("/p/PIN/", pinned=True)])
        worker._profile_post_datetime_cache["target"] = ((datetime.now(timezone.utc) - timedelta(days=50, hours=1)).isoformat(),)
        result = await self.read(worker, VisibleProfile("target", "public", 10, 20, 1))
        self.assertEqual((50, "identified"), (result.post_activity_days, result.post_activity_status))
        worker._read_original_post_datetime.assert_not_awaited()

    async def test_unknown_visibility_has_no_guessed_post_age(self):
        worker = self.worker(story=True)
        result = await self.read(worker, VisibleProfile("target", "unknown", None, None, None))
        self.assertEqual((None, "visibility_unknown"), (result.post_activity_days, result.post_activity_status))
        worker._has_visible_target_story.assert_not_awaited()
        worker._read_original_post_datetime.assert_not_awaited()

    def test_inline_story_and_highlight_times_are_not_post_timestamps(self):
        now = datetime.now(timezone.utc)
        old_post = now - timedelta(days=90)
        evidence = extract_embedded_profile_evidence([{"data": {"user": {
            "username": "target", "is_private": False, "media_count": 1,
            "edge_owner_to_timeline_media": {"edges": [{"node": {"shortcode": "OLD", "taken_at_timestamp": int(old_post.timestamp())}}]},
            "stories": {"items": [{"media_type": 1, "taken_at": int(now.timestamp())}]},
            "highlight_reels": {"items": [{"media_type": 1, "taken_at": int(now.timestamp())}]},
        }}}], "target", now=now)
        self.assertEqual(1, len(evidence.post_datetimes))
        self.assertEqual(int(old_post.timestamp()), int(datetime.fromisoformat(evidence.post_datetimes[0]).timestamp()))


if __name__ == "__main__":
    unittest.main()
