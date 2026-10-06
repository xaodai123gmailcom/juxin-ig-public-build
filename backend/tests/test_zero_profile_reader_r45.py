"""Actual Chromium profile fixtures; every request is intercepted locally."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


def profile_html(username="sample_reader01", *, private=False, posts=0,
                 delayed=False, loader=True, no_header=False, unknown=False,
                 initial_content=""):
    counter = "" if unknown else f"<span>{posts}帖子</span><span>240粉丝</span><span>680关注</span>"
    notice = ("这是私密账户" if private else "这里空荡荡～") if not unknown else ""
    header_tag = "section" if no_header else "header"
    profile = f'''<{header_tag}><h2>{username}</h2><p>Sample Reader</p>
      <div>{counter}</div><p>@{username}</p><button>关注</button></{header_tag}>
      <section id="grid"><h2>{notice}</h2></section>
      <aside><h3>为你推荐</h3><a href="/another_account/">another_account</a>
      <span>999帖子 900粉丝 800关注</span>
      {"<div role='progressbar'>加载推荐</div>" if loader else ""}</aside>'''
    return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
      <nav>Instagram</nav><main id="profile" style="min-height:100px">{initial_content if delayed else profile}</main>
      <script>window.renderProfile=()=>{{document.querySelector('#profile').innerHTML={json.dumps(profile)}}};</script>
      </body></html>'''


async def launch_fixture_browser(test):
    from playwright.async_api import async_playwright
    runtime = await async_playwright().start()
    test.addAsyncCleanup(runtime.stop)
    executable = os.environ.get("IGAC_TEST_CHROMIUM_EXECUTABLE", runtime.chromium.executable_path)
    if not Path(executable).is_file():
        if os.name == "nt" or os.environ.get("IGAC_REQUIRE_PROFILE_BROWSER") == "1":
            test.fail("Chromium is required for profile DOM regression tests")
        test.skipTest("Chromium is unavailable in this non-Windows environment")
    browser = await runtime.chromium.launch(executable_path=executable, args=["--no-sandbox"])
    test.addAsyncCleanup(browser.close)
    context = await browser.new_context()
    test.addAsyncCleanup(context.close)
    return context


class ZeroProfileReaderR45Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = await launch_fixture_browser(self)

    async def worker(self, html):
        await self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        worker = PlaywrightWorker(None)
        worker.page = await self.context.new_page()
        worker._context = self.context
        # A replacement is an observable failure in these tests: all target content
        # is already available in the original, owned tab.
        worker._recover_stalled_profile_page = AsyncMock(return_value=False)
        return worker

    async def test_complete_public_zero_with_recommendation_loader_is_terminal(self):
        for no_header in (False, True):
            with self.subTest(no_header=no_header):
                worker = await self.worker(profile_html(no_header=no_header))
                result = await worker.read_visible_profile("sample_reader01", include_post_activity=True)
                self.assertEqual(("public", 0, 240, 680, "no_posts"), (
                    result.visibility, result.posts, result.followers, result.following,
                    result.post_activity_status))
                worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_profile_committed_after_initial_sample_is_read_in_same_tab(self):
        worker = await self.worker(profile_html(delayed=True))
        original = worker._profile_surface_is_transient
        first_sample = True

        async def finish_render(username, **kwargs):
            nonlocal first_sample
            if first_sample:
                first_sample = False
                self.assertIsNone(kwargs["metrics"][2])
                # Reproduce the render race without a machine-dependent timeout:
                # the body snapshot precedes the settled, visible zero-post grid.
                await worker.page.evaluate("renderProfile()")
            return await original(username, **kwargs)

        worker._profile_surface_is_transient = finish_render
        result = await worker.read_visible_profile("sample_reader01", include_post_activity=True)
        self.assertEqual(("public", 0, 240, 680, "no_posts"), (
            result.visibility, result.posts, result.followers, result.following,
            result.post_activity_status))
        worker._recover_stalled_profile_page.assert_not_awaited()
        self.assertEqual(1, len(self.context.pages))

    async def test_partial_title_counters_or_notice_are_resampled_without_reload(self):
        for initial in (
            "<h2>sample_reader01</h2>",
            "<h2>sample_reader01</h2><span>0帖子240粉丝</span>",
            "<header>sample_reader01 0帖子240粉丝680关注</header>",
            "<h2>sample_reader01</h2><h2>这里空荡荡～</h2>",
        ):
            with self.subTest(initial=initial):
                worker = await self.worker(profile_html(delayed=True, initial_content=initial))
                original = worker._profile_surface_is_transient
                rendered = False

                async def render(username, **kwargs):
                    nonlocal rendered
                    if not rendered:
                        rendered = True
                        await worker.page.evaluate("renderProfile()")
                    return await original(username, **kwargs)

                worker._profile_surface_is_transient = render
                result = await worker.read_visible_profile("sample_reader01", include_post_activity=True)
                self.assertEqual(("public", 0, 240, 680), (
                    result.visibility, result.posts, result.followers, result.following))
                worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_late_private_zero_keeps_private_precedence(self):
        worker = await self.worker(profile_html(private=True, delayed=True, no_header=True))
        original = worker._profile_surface_is_transient
        rendered = False

        async def render(username, **kwargs):
            nonlocal rendered
            if not rendered:
                rendered = True
                await worker.page.evaluate("renderProfile()")
            return await original(username, **kwargs)

        worker._profile_surface_is_transient = render
        result = await worker.read_visible_profile("sample_reader01", include_post_activity=True)
        self.assertEqual(("private", 0, 240, 680, "private_not_visible"), (
            result.visibility, result.posts, result.followers, result.following,
            result.post_activity_status))
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_permanent_shell_is_bounded_and_never_saved_as_zero(self):
        worker = await self.worker(profile_html(delayed=True))
        with patch("app.playwright_worker._PROFILE_DATA_SETTLE_SECONDS", .25):
            with self.assertRaises(WorkerExecutionError) as caught:
                await worker.read_visible_profile("sample_reader01")
        self.assertEqual("instagram_page_recovery_exhausted", caught.exception.code)
        self.assertEqual("instagram_profile_not_ready", caught.exception.details["original_reason"])
        worker._recover_stalled_profile_page.assert_awaited_once()
        self.assertNotIn("sample_reader01", worker._profile_base_cache)

    async def test_unknown_post_count_is_never_inferred_from_empty_copy(self):
        html = profile_html(loader=False).replace("<span>0帖子</span>", "")
        worker = await self.worker(html)
        result = await worker.read_visible_profile("sample_reader01")
        self.assertIsNone(result.posts)
        self.assertNotEqual("public", result.visibility)
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_zero_layout_without_semantic_main_or_header_is_read(self):
        html = profile_html(no_header=True).replace('<main id="profile"', '<div id="profile"').replace("</main>", "</div>")
        worker = await self.worker(html)
        result = await worker.read_visible_profile("sample_reader01")
        self.assertEqual(("public", 0), (result.visibility, result.posts))
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_structured_public_visible_zero_does_not_wait_for_empty_caption(self):
        html = profile_html().replace("这里空荡荡～", "")
        html = html.replace("</head>", '<script type="application/json">{"user":{"username":"sample_reader01","is_private":false,"media_count":0}}</script></head>')
        worker = await self.worker(html)
        result = await worker.read_visible_profile("sample_reader01", include_post_activity=True)
        self.assertEqual(("public", 0, "no_posts"), (
            result.visibility, result.posts, result.post_activity_status))
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_validated_recovery_page_is_read_without_restarting_navigation(self):
        worker = await self.worker(profile_html())
        await worker.page.goto("https://www.instagram.com/sample_reader01/")
        worker._validated_recovery_profile = (worker.page, "sample_reader01")
        await worker.page.evaluate("window.recoverySentinel=17")
        result = await worker.read_visible_profile("sample_reader01")
        self.assertEqual(("public", 0), (result.visibility, result.posts))
        self.assertEqual(17, await worker.page.evaluate("window.recoverySentinel"))
        worker._recover_stalled_profile_page.assert_not_awaited()

    async def test_login_redirect_is_not_delayed_by_same_page_settle(self):
        worker = await self.worker(profile_html(delayed=True))
        original = worker._profile_surface_is_transient

        async def redirect(username, **kwargs):
            await worker.page.goto("https://www.instagram.com/accounts/login/")
            return await original(username, **kwargs)

        worker._profile_surface_is_transient = redirect
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker.read_visible_profile("sample_reader01")
        self.assertEqual("instagram_login_required", caught.exception.code)
        worker._recover_stalled_profile_page.assert_not_awaited()
        self.assertNotIn("sample_reader01", worker._profile_base_cache)

    async def test_stop_during_settle_does_not_cache_or_replace_page(self):
        worker = await self.worker(profile_html(delayed=True))
        sampled = asyncio.Event()
        original = worker._profile_surface_is_transient

        async def sample(username, **kwargs):
            result = await original(username, **kwargs)
            sampled.set()
            return result

        worker._profile_surface_is_transient = sample
        reading = asyncio.create_task(worker.read_visible_profile("sample_reader01"))
        await asyncio.wait_for(sampled.wait(), 5)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
        worker._recover_stalled_profile_page.assert_not_awaited()
        self.assertNotIn("sample_reader01", worker._profile_base_cache)

    async def test_optional_nodes_removed_during_read_cannot_block_zero_result(self):
        for selector in (
            'meta[property="og:description"], meta[name="description"]',
            'a[href*="/following"]',
        ):
            with self.subTest(selector=selector):
                html = profile_html().replace("</head>", '<meta property="og:description" content="99 posts"></head>')
                html = html.replace("</header>", '<a href="/sample_reader01/following/">680关注</a></header>')
                worker = await self.worker(html)
                actual_page = worker.page
                removed = []

                class RaceLocator:
                    def __init__(self, locator):
                        self.locator = locator

                    def __getattr__(self, name):
                        return getattr(self.locator, name)

                    async def evaluate_all(self, expression, *args):
                        if not removed:
                            removed.append(True)
                            await self.locator.evaluate_all("nodes => nodes.forEach(node => node.remove())")
                        return await self.locator.evaluate_all(expression, *args)

                class RacePage:
                    def __getattr__(self, name):
                        return getattr(actual_page, name)

                    def locator(self, value):
                        locator = actual_page.locator(value)
                        return RaceLocator(locator) if value == selector else locator

                worker.page = RacePage()
                result = await worker.read_visible_profile("sample_reader01")
                self.assertEqual(("public", 0, 240, 680), (
                    result.visibility, result.posts, result.followers, result.following))
                self.assertTrue(removed)
                worker._recover_stalled_profile_page.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
