"""Visible About-account outcomes against an actual, network-isolated Chromium DOM."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import (
    PlaywrightWorker, WorkerExecutionError, extract_about_account_location,
)


class LocationValueR45Tests(unittest.TestCase):
    def test_undisclosed_values_are_not_country_names(self):
        for value in ("Not shared", "Not public", "Not disclosed", "Not available",
                      "未公开", "不公开", "所在地不公开", "地区未公开", "未提供", "未分享"):
            with self.subTest(value=value):
                self.assertIsNone(extract_about_account_location(f"About this account\nAccount based in\n{value}"))

    def test_real_named_countries_remain_usable(self):
        self.assertEqual("美国", extract_about_account_location("Account based in\nUnited States"))
        self.assertEqual("加拿大", extract_about_account_location("账户所在地\nCanada"))
        self.assertEqual("Norge", extract_about_account_location("Account based in\nNorge"))

    def test_empty_location_row_cannot_use_panel_controls_as_country(self):
        for control in ("Close", "关闭", "About this account", "返回"):
            with self.subTest(control=control):
                self.assertIsNone(extract_about_account_location(
                    f"About this account\nAccount based in\n{control}"))


class LocationReadinessR45Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        runtime = await async_playwright().start()
        self.addAsyncCleanup(runtime.stop)
        executable = os.environ.get("IGAC_TEST_CHROMIUM_EXECUTABLE", runtime.chromium.executable_path)
        if not Path(executable).is_file():
            if os.name == "nt" or os.environ.get("IGAC_REQUIRE_PROFILE_BROWSER") == "1":
                self.fail("Chromium is required for location DOM regression tests")
            self.skipTest("Chromium is unavailable in this non-Windows environment")
        browser = await runtime.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        self.addAsyncCleanup(browser.close)
        self.context = await browser.new_context()
        self.addAsyncCleanup(self.context.close)

    async def worker(self, panel, *, recommended_loader=True, delayed_country=False):
        html = f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
          <main><header><h2 onclick="openAbout()">location_target</h2>
          <span>12帖子</span><span>108粉丝</span><span>679关注</span><button>关注</button></header>
          <a href="/p/actualpost/">Post</a><aside><h2 onclick="window.wrongClicks++">location_target_fan</h2>
          {"<div role='progressbar'>加载推荐</div>" if recommended_loader else ""}</aside></main>
          <script>window.opens=0;window.wrongClicks=0;
          window.openAbout=()=>{{window.opens++;let d=document.createElement('div');
          d.setAttribute('role','dialog');d.id='about';d.innerHTML={json.dumps(panel)};document.body.appendChild(d);
          {"setTimeout(()=>{d.innerHTML='<h2>About this account</h2><p>Account based in</p><p>United States</p>'},450);" if delayed_country else ""}
          }};
          document.addEventListener('keydown',event=>{{if(event.key==='Escape')document.querySelector('#about')?.remove()}});
          </script></body></html>'''
        await self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        worker = PlaywrightWorker(None)
        worker.page = await self.context.new_page()
        worker._context = self.context
        worker.location_request_min_interval_seconds = 0
        worker.location_request_max_interval_seconds = 0
        worker._recover_stalled_profile_page = AsyncMock(return_value=False)
        await worker.page.goto("https://www.instagram.com/location_target/")
        return worker

    async def assert_unknown_without_recovery(self, worker):
        self.assertIsNone(await worker.read_visible_account_location("location_target"))
        worker._recover_stalled_profile_page.assert_not_awaited()
        self.assertEqual(1, await worker.page.evaluate("window.opens"))
        self.assertEqual(0, await worker.page.evaluate("window.wrongClicks"))
        self.assertEqual(0, await worker.page.locator('[role="dialog"]').count())
        self.assertEqual(0, worker._location_request_coordinator.blocked_until)

    async def test_explicit_not_shared_is_final_unknown_with_background_loader(self):
        worker = await self.worker("<h2>About this account</h2><p>Account based in</p><p>Not shared</p>")
        await self.assert_unknown_without_recovery(worker)

    async def test_rendered_about_without_location_does_not_inherit_recommendation_loading(self):
        worker = await self.worker("<h2>About this account</h2><p>Date joined</p><p>May 2014</p>")
        await self.assert_unknown_without_recovery(worker)

    async def test_empty_location_row_before_close_is_unknown(self):
        for row in ("<p>Account based in</p><button>Close</button>",
                    "<div><p>Account based in</p><button>Close</button></div>"):
            with self.subTest(row=row):
                worker = await self.worker("<h2>About this account</h2>" + row)
                await self.assert_unknown_without_recovery(worker)

    async def test_delayed_country_is_read_without_reopening_panel(self):
        worker = await self.worker("<h2>About this account</h2><div role='progressbar'>Loading</div>", delayed_country=True)
        self.assertEqual("美国", await worker.read_visible_account_location("location_target"))
        worker._recover_stalled_profile_page.assert_not_awaited()
        self.assertEqual(1, await worker.page.evaluate("window.opens"))

    async def test_real_about_failure_remains_recoverable(self):
        worker = await self.worker("<h2>About this account</h2><p>Failed to load</p>")
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_location_once("location_target")
        self.assertEqual("instagram_location_load_failed", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])

    async def test_about_spinner_is_not_confirmed_missing_information(self):
        worker = await self.worker("<h2>About this account</h2><div role='progressbar'>Loading</div>", recommended_loader=False)
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_location_once("location_target")
        self.assertIn(raised.exception.code, {"instagram_profile_not_ready", "instagram_location_load_failed"})

    async def test_login_redirect_cannot_become_unknown_country(self):
        worker = await self.worker("<h2>About this account</h2><p>Date joined</p><p>May 2014</p>")
        await worker.page.evaluate("() => { window.openAbout=()=>{location.href='/accounts/login/'} }")
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_location_once("location_target")
        self.assertEqual("instagram_login_required", raised.exception.code)

    async def test_country_from_a_switched_target_is_rejected(self):
        worker = await self.worker("<h2>About this account</h2><p>Account based in</p><p>United States</p>")
        await worker.page.evaluate("() => { const open=window.openAbout;window.openAbout=()=>{open();history.pushState({},'', '/different_target/')} }")
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_location_once("location_target")
        self.assertEqual("instagram_wrong_profile_target", raised.exception.code)

    async def test_closed_about_surface_is_not_treated_as_settled_unknown(self):
        worker = await self.worker("<h2>About this account</h2><p>Date joined</p><p>May 2014</p>")
        await worker.page.evaluate("window.openAbout()")
        surface = worker.page.locator('[role="dialog"]')
        await worker.page.close()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._location_surface_snapshot(surface)
        self.assertEqual("instagram_location_load_failed", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
