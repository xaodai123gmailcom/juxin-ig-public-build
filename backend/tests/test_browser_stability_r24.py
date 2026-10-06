"""Controlled page-adapter regressions; these do not launch real Chromium."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class BrowserStabilityR24Tests(unittest.IsolatedAsyncioTestCase):
    def page(self, *, url="https://www.instagram.com/target/", body="target", notices=()):
        node = SimpleNamespace(
            evaluate=AsyncMock(return_value=list(notices)),
            inner_text=AsyncMock(return_value=body),
            count=AsyncMock(return_value=0),
            wait_for=AsyncMock(),
        )
        node.first = node
        return SimpleNamespace(url=url, locator=lambda _: node)

    def worker(self, **kwargs):
        worker = PlaywrightWorker(None)
        worker.page = self.page(**kwargs)
        return worker

    async def test_system_error_with_explanation_is_transport_failure(self):
        for notice in (
            "This site can't be reached ERR_INTERNET_DISCONNECTED",
            "This site can’t be reached\nCheck your internet connection.",
            "No internet. Check your network cables, modem and router.",
            "无法访问此网站，请检查网络连接。",
        ):
            with self.subTest(notice=notice):
                worker = self.worker(notices=[notice])
                self.assertEqual("instagram_network_unavailable",
                    await worker._visible_transport_failure(worker.page, "target\nA normal biography"))

    async def test_biography_must_not_be_used_as_system_notice(self):
        for body in (
            "target\n20 posts 120 followers 40 following\nNo internet. My new song.",
            "This site can't be reached ERR_INTERNET_DISCONNECTED is my biography",
            "Biography: Something went wrong · Reload page",
        ):
            with self.subTest(body=body):
                worker = self.worker()
                self.assertIsNone(await worker._visible_transport_failure(worker.page, body))
        worker = self.worker(notices=["My song is called No internet"])
        self.assertIsNone(await worker._visible_transport_failure(worker.page, "target"))

    async def test_real_network_failure_wins_over_unrelated_soft_notice(self):
        worker = self.worker(notices=[
            "Something went wrong · Reload page", "No internet. Try reconnecting."])
        self.assertEqual("instagram_network_unavailable",
            await worker._profile_surface_is_transient("target", body_text="target",
                metrics=(100, 40, 0), confirmed_public_empty=True))

    async def test_soft_post_notice_preserves_ready_private_or_public_profile(self):
        worker = self.worker(notices=["Couldn't load posts · Reload page"])
        for metrics, flags in (
            ((100, 40, 5), {}),
            ((100, 40, 0), {"confirmed_private": True}),
            ((100, 40, 0), {"confirmed_public_empty": True}),
        ):
            with self.subTest(metrics=metrics, flags=flags):
                self.assertIsNone(await worker._profile_surface_is_transient(
                    "target", body_text="target", metrics=metrics, **flags))
        self.assertEqual("instagram_profile_not_ready",
            await worker._profile_surface_is_transient("target", body_text="target",
                metrics=(100, None, 0), confirmed_private=True))

    async def test_chromium_error_document_preserves_network_reason(self):
        worker = self.worker(url="chrome-error://chromewebdata/", body="")
        self.assertEqual("instagram_network_unavailable",
            await worker._profile_transport_failure("target", body_text=""))
        self.assertEqual("instagram_network_unavailable",
            await worker._profile_surface_is_transient("target", body_text="", metrics=(100, 40, 0),
                confirmed_private=True))

    async def test_replacement_chromium_error_does_not_become_account_not_visible(self):
        worker = self.worker(url="chrome-error://chromewebdata/", body="No connection")
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._validate_recovery_profile_page(worker.page, "target")
        self.assertEqual("instagram_network_unavailable", caught.exception.code)
        self.assertTrue(caught.exception.details["pause_required"])

    def test_exact_profile_requires_instagram_origin(self):
        for url in (
            "https://www.instagram.com/target/?locale=en_US",
            "https://instagram.com/target/", "https://m.instagram.com/target/",
        ):
            with self.subTest(url=url):
                self.assertTrue(PlaywrightWorker._page_matches_profile(SimpleNamespace(url=url), "target"))
        for url in (
            "https://proxy-error.example/target/", "https://instagram.com.evil.example/target/",
            "https://instagram.com@proxy-error.example/target/", "file:///target/",
            "chrome-error://chromewebdata/target/", "https://www.instagram.com/other/",
        ):
            with self.subTest(url=url):
                self.assertFalse(PlaywrightWorker._page_matches_profile(SimpleNamespace(url=url), "target"))

    async def test_offsite_profile_shaped_page_cannot_be_saved_as_target(self):
        worker = self.worker(url="https://proxy-error.example/target/")
        self.assertEqual("instagram_content_not_visible",
            await worker._profile_surface_is_transient("target",
                body_text="target\n20 posts 100 followers 40 following", metrics=(100, 40, 20)))

    async def test_profile_metrics_never_borrow_recommended_account_counts(self):
        class Node:
            def __init__(self, text="", *, href=None, present=False, children=()):
                self.text, self.href = text, href
                self.present, self.children = present, children
                self.first = self.children[0] if self.children else self

            async def count(self):
                return len(self.children) if self.children else int(self.present)

            def nth(self, index):
                return self.children[index]

            async def is_visible(self):
                return self.present

            async def inner_text(self, **kwargs):
                return self.text

            async def get_attribute(self, name):
                return self.href if name == "href" else None

            def locator(self, selector):
                return Node()

            async def evaluate(self, expression):
                return []

        for exact_links in (False, True):
            with self.subTest(exact_links=exact_links):
                followers = [Node("9,999 followers", href="/suggested/followers/", present=True)]
                following = [Node("8,888 following", href="/suggested/following/", present=True)]
                if exact_links:
                    followers.append(Node("123 followers", href="https://www.instagram.com/target/followers/?v=2", present=True))
                    following.append(Node("45 following", href="/target/following?variant=desktop", present=True))
                header = Node("target\n10 posts 120 followers 40 following", present=True)
                body = Node(header.text + "\nThis account is private", present=True)

                def locator(selector):
                    if selector == "body":
                        return body
                    if "/followers" in selector:
                        return Node(children=followers)
                    if "/following" in selector:
                        return Node(children=following)
                    return Node()

                worker = self.worker()
                worker.page.locator = locator
                worker._navigate_profile_with_privacy = AsyncMock(return_value=("target", True))
                worker._visible_profile_header = AsyncMock(return_value=header)
                worker._visible_external_bio_url = AsyncMock(return_value=None)
                result = await worker._read_visible_profile_once("target")
                self.assertEqual("private", result.visibility)
                self.assertEqual(10, result.posts)
                self.assertEqual(123 if exact_links else 120, result.followers)
                self.assertEqual(45 if exact_links else 40, result.following)


if __name__ == "__main__":
    unittest.main()
