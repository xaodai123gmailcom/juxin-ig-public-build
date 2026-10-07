"""Offline account/nurture browser fixture; never connects to a live account."""
import os
from pathlib import Path
from unittest.mock import AsyncMock
from app.playwright_worker import PlaywrightWorker
from app.studio_worker import StudioBrowser
from support.browser_fixture import browser_fixture_launch_options

HTML='''<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{margin:0;background:#101215;color:white;font-family:Arial}#sidebar{position:fixed;left:0;top:0;width:72px;height:100vh;display:flex;flex-direction:column;gap:22px;padding-top:24px}#sidebar a{display:block;margin-left:24px;width:24px;height:24px;color:white}svg{width:24px;height:24px}main{margin-left:260px;padding:50px}</style></head><body><div id="sidebar"><a href="/"><svg aria-label="首页"><circle cx="12" cy="12" r="8"/></svg></a><a href="/reels/"><svg aria-label="Reels"><rect width="18" height="18"/></svg></a><a href="/direct/inbox/"><svg aria-label="消息"><path d="M2 2L22 2L12 22Z"/></svg></a></div><main><h1>示例主页</h1><p>Feed post quotes: log in to Instagram</p></main></body></html>'''

class AccountDOMFixture:
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p=await async_playwright().start()
        self.addAsyncCleanup(self.p.stop)
        path=os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE','')
        if not path and not Path(self.p.chromium.executable_path).is_file():
            if os.environ.get('IGAC_REQUIRE_NURTURE_BROWSER')=='1':self.fail('Required Chromium runtime is missing')
            self.skipTest('Chromium is required for offline account fixtures')
        self.browser=await self.p.chromium.launch(**({'executable_path':path} if path else {}),**browser_fixture_launch_options())
        self.addAsyncCleanup(self.browser.close)
        self.context=await self.browser.new_context(viewport={'width':1264,'height':715},service_workers='block')
        await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=HTML))
        self.page=await self.context.new_page();await self.page.goto('https://www.instagram.com/')
        self.worker=PlaywrightWorker(object())
        self.worker.page=self.page;self.worker._context=self.context;self.worker._guard=AsyncMock()
        self.studio=StudioBrowser(self.worker,AsyncMock(),AsyncMock())
    async def asyncTearDown(self):
        pass
    async def load_fixture(self, html):
        await self.context.unroute('**/*')
        await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
        await self.page.reload()
