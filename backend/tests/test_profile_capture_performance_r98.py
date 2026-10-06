"""Bound passive profile response work without contacting Instagram."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.playwright_worker import EmbeddedProfileEvidence, PlaywrightWorker, VisibleProfile, _PROFILE_REUSABLE_IDENTITIES


class Page:
    def __init__(self):
        self.listeners = []

    def on(self, event, listener):
        self.listeners.append(listener)

    def remove_listener(self, event, listener):
        self.listeners.remove(listener)

    def emit(self, response):
        for listener in tuple(self.listeners):
            listener(response)


class Response:
    def __init__(self, *, username="target", private=True, url=None, headers=None,
                 state=None, delay=0, release=None, payload=None, status=200):
        self.url = url or "https://www.instagram.com/graphql/query"
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.status = status
        self.payload = payload if payload is not None else json.dumps({"data": {"user": {
            "username": username, "is_private": private, "id": "1234", "media_count": 3,
        }}}).encode()
        self.state, self.delay, self.release = state, delay, release
        self.reads = self.cancelled = 0

    async def body(self):
        self.reads += 1
        if self.state is not None:
            self.state["active"] += 1
            self.state["peak"] = max(self.state["peak"], self.state["active"])
        try:
            if self.release is not None:
                await self.release.wait()
            await asyncio.sleep(self.delay)
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            if self.state is not None:
                self.state["active"] -= 1


class ProfileCapturePerformanceR98Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self, responses, inline=None):
        worker = PlaywrightWorker(None)
        worker.page = Page()
        worker._read_inline_profile_evidence = AsyncMock(
            return_value=inline or EmbeddedProfileEvidence())
        async def navigate(target):
            for response in responses:
                worker.page.emit(response)
            return target
        worker._navigate_profile = navigate
        return worker

    async def read(self, worker, target="target"):
        with patch("app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS", .015):
            return await worker._navigate_profile_with_privacy(target)

    async def test_burst_has_four_active_reads_and_retains_latest_exact_profile(self):
        state = {"active": 0, "peak": 0}
        responses = [Response(username="other", state=state, delay=.003) for _ in range(300)]
        actual = Response(state=state)
        worker = self.worker([*responses, actual])
        self.assertEqual(("target", True), await self.read(worker))
        self.assertEqual(4, state["peak"])
        self.assertEqual(36, sum(response.reads for response in [*responses, actual]))
        self.assertEqual(1, actual.reads)
        self.assertEqual("1234", worker.get_cached_instagram_user_id("target"))
        self.assertEqual([], worker.page.listeners)

    async def test_unrelated_redirect_and_lookalike_responses_never_read_bodies(self):
        rejected = [Response(url=url) for url in (
            "https://www.instagram.com/api/v1/direct_v2/inbox/",
            "https://www.instagram.com/api/v1/feed/timeline/",
            "https://www.instagram.com.evil.invalid/graphql/query",
            "https://evilinstagram.com/graphql/query",
            "https://cdn.instagram.com/graphql/query",
            "http://www.instagram.com/graphql/query",
            "https://user:password@www.instagram.com/graphql/query",
            "https://www.instagram.com:8443/graphql/query",
        )]
        rejected.append(Response(status=302))
        accepted = Response(url="https://i.instagram.com/api/v1/users/web_profile_info/")
        worker = self.worker([*rejected, accepted])
        self.assertEqual(("target", True), await self.read(worker))
        self.assertTrue(all(response.reads == 0 for response in rejected))
        self.assertEqual(1, accepted.reads)

    async def test_declared_and_chunked_oversize_cannot_supply_false_evidence(self):
        large = json.dumps({"username": "target", "is_private": False,
                            "padding": "x" * 8_000_000}).encode()
        declared = Response(headers={"content-length": str(len(large))}, payload=large)
        chunked = Response(payload=large)
        invalid = Response(headers={"content-length": "invalid"}, payload=large)
        small = Response(headers={"content-length": "invalid"})
        worker = self.worker([declared, chunked, invalid, small])
        self.assertEqual(("target", True), await self.read(worker))
        self.assertEqual(0, declared.reads)
        self.assertEqual(1, chunked.reads)
        self.assertEqual(1, invalid.reads)

    async def test_absent_passive_evidence_is_unknown_and_inline_still_works(self):
        worker = self.worker([Response(username="other")])
        self.assertEqual(("target", None), await self.read(worker))
        worker = self.worker([], EmbeddedProfileEvidence(is_private=True))
        self.assertEqual(("target", True), await self.read(worker))

    async def test_repeated_navigations_and_closed_response_leave_no_listener(self):
        response = Response(payload=RuntimeError("page was closed"))
        worker = self.worker([response])
        for _ in range(100):
            self.assertEqual(("target", None), await self.read(worker))
            self.assertEqual([], worker.page.listeners)
        self.assertEqual(100, response.reads)
        self.assertEqual(set(), worker._late_lifecycle_tasks)

    async def test_stop_keeps_body_owned_until_cleanup_without_cancelling_it(self):
        release = asyncio.Event()
        response = Response(release=release)
        worker = self.worker([response])
        task = asyncio.create_task(self.read(worker))
        try:
            while not response.reads:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([], worker.page.listeners)
            self.assertEqual(0, response.cancelled)
            self.assertEqual(1, len(worker._late_lifecycle_tasks))
            cleanup = asyncio.create_task(worker.wait_for_cleanup())
            await asyncio.sleep(.01)
            self.assertFalse(cleanup.done())
            cleanup.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(cleanup.done())
        finally:
            release.set()
            await asyncio.gather(*tuple(worker._late_lifecycle_tasks), return_exceptions=True)
            if 'cleanup' in locals():
                with self.assertRaises(asyncio.CancelledError):
                    await cleanup
        self.assertEqual(0, response.cancelled)
        self.assertEqual(set(), worker._profile_capture_tasks)

    async def test_blocked_body_allows_prompt_inline_fallback_without_page_poisoning(self):
        release = asyncio.Event()
        response = Response(private=False, release=release)
        worker = self.worker([response], EmbeddedProfileEvidence(
            is_private=True, instagram_user_id="inline123"))
        try:
            with patch("app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS", .01):
                result = await worker._await_page_stage(
                    worker._navigate_profile_with_privacy("target"), timeout=1.4)
            self.assertEqual(("target", True), result)
            self.assertFalse(worker._page_stage_abandoned)
            worker._read_inline_profile_evidence.assert_awaited_once()
            self.assertEqual("inline123", worker.get_cached_instagram_user_id("target"))
            self.assertEqual(1, len(worker._late_lifecycle_tasks))
        finally:
            release.set()
            await asyncio.gather(*tuple(worker._late_lifecycle_tasks), return_exceptions=True)
        self.assertEqual("inline123", worker.get_cached_instagram_user_id("target"))
        self.assertIs(True, worker._profile_privacy_cache["target"])
        self.assertFalse(worker._profile_capture_tasks)

    async def test_late_body_limit_spans_repeated_navigations(self):
        release = asyncio.Event()
        state = {"active": 0, "peak": 0}
        responses = [Response(release=release, state=state) for _ in range(10)]
        worker = self.worker(responses, EmbeddedProfileEvidence(is_private=True))
        try:
            for index in range(100):
                self.assertEqual((f"target{index}", True), await self.read(worker, f"target{index}"))
                self.assertEqual(4, len(worker._profile_capture_tasks))
                self.assertEqual([], worker.page.listeners)
                self.assertFalse(worker._page_stage_abandoned)
            self.assertEqual(4, sum(response.reads for response in responses))
            self.assertEqual(4, state["peak"])
        finally:
            release.set()
            await asyncio.gather(*tuple(worker._late_lifecycle_tasks), return_exceptions=True)
        self.assertFalse(worker._profile_capture_tasks)

    async def test_owned_page_closes_before_cleanup_drains_stalled_response(self):
        body_release, close_entered, close_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        response = Response(release=body_release, payload=RuntimeError("page closed"))
        worker = self.worker([response], EmbeddedProfileEvidence(is_private=True))
        owned = worker._worker_owned_page = worker.page
        async def close():
            close_entered.set()
            await close_release.wait()
            body_release.set()
        owned.close = close
        self.assertEqual(("target", True), await self.read(worker))
        cleanup = asyncio.create_task(worker.wait_for_cleanup())
        try:
            await asyncio.wait_for(close_entered.wait(), 1)
            self.assertFalse(cleanup.done())
            self.assertIs(owned, worker._worker_owned_page)
            self.assertTrue(worker._profile_capture_tasks)
        finally:
            close_release.set()
            await asyncio.wait_for(cleanup, 1)
        self.assertIsNone(worker._worker_owned_page)
        self.assertFalse(worker._profile_capture_tasks)
        self.assertFalse(worker._late_lifecycle_tasks)

    async def test_ten_thousand_partial_reads_bound_all_metadata_without_success(self):
        worker = self.worker([])
        async def guard():
            pass
        async def evidence(username):
            return EmbeddedProfileEvidence(
                is_private=True, instagram_user_id="1234", posts_count=3,
                is_verified=False, is_professional_account=True,
                account_category="Creator", external_bio_url="https://example.test/",
                post_datetimes=("2026-09-01T00:00:00+00:00",))
        worker._guard = guard
        worker._is_current_profile = lambda username: True
        worker._read_inline_profile_evidence = evidence
        for index in range(10_000):
            username = f"incomplete{index}"
            await worker._navigate_profile_with_privacy(username, reuse_navigation=True)
        self.assertEqual({}, worker._profile_base_cache)
        for name in worker._PROFILE_CACHE_NAMES:
            self.assertLessEqual(len(getattr(worker, name)), _PROFILE_REUSABLE_IDENTITIES, name)
        self.assertEqual("1234", worker.get_cached_instagram_user_id("incomplete9999"))
        worker._forget_profile_attempt("incomplete9999")
        for name in worker._PROFILE_CACHE_NAMES:
            self.assertNotIn("incomplete9999", getattr(worker, name), name)
        self.assertEqual("1234", worker.get_cached_instagram_user_id("incomplete9998"))
        worker.invalidate_after_manual_control()
        for name in worker._PROFILE_CACHE_NAMES:
            self.assertFalse(getattr(worker, name), name)

    async def test_successful_cache_revisit_avoids_navigation_and_survives_eviction(self):
        worker = self.worker([])
        for index in range(_PROFILE_REUSABLE_IDENTITIES):
            username = f"saved{index}"
            worker._profile_user_id_cache[username] = str(index + 1)
            worker._remember_privacy(username, True)
            worker._remember_profile(username, VisibleProfile(username, "private", 2, 3, 4,
                                     avatar_image_bytes=b"never-cache-pixels"))
        worker._navigate_profile_with_privacy = AsyncMock(side_effect=AssertionError("cached profile navigated"))
        profile = await worker._read_visible_profile_once("saved0")
        self.assertEqual("saved0", profile.username)
        self.assertIsNone(profile.avatar_image_bytes)
        worker._remember_profile("newest", VisibleProfile("newest", "private", 2, 3, 4))
        self.assertEqual(_PROFILE_REUSABLE_IDENTITIES, len(worker._profile_base_cache))
        self.assertIn("saved0", worker._profile_base_cache)
        self.assertNotIn("saved1", worker._profile_base_cache)
        self.assertEqual("1", worker.get_cached_instagram_user_id("saved0"))
        worker._navigate_profile_with_privacy.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
