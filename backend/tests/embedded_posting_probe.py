"""Run the create-entry regressions in actual embedded Electron pages."""
import asyncio,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_posting_dom as fixtures

async def setup(self):
    from playwright.async_api import async_playwright
    self.p=await async_playwright().start()
    self.browser=await self.p.chromium.connect_over_cdp(os.environ['IGAC_POSTING_TEST_CDP'])
    self.context=self.browser.contexts[0]
    await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=fixtures.HTML))
    self.page=await self.context.new_page();await self.page.set_viewport_size({'width':1264,'height':715})
    await self.page.goto('https://www.instagram.com/')
    self.tmp=tempfile.TemporaryDirectory();self.asset=Path(self.tmp.name)/'桌面素材.jpg';self.asset.write_bytes(b'fixture upload bytes')
    self.worker=fixtures.PlaywrightWorker(object());self.worker.page=self.page;self.worker._context=self.context;self.worker._guard=AsyncMock()
    self.studio=fixtures.StudioBrowser(self.worker,AsyncMock(),AsyncMock())
    self.pub=fixtures.InstagramPublisher(self.studio);self.pub.finish=AsyncMock(return_value={});self.pub.confirmation_timeout=10;self.pub.poll_seconds=.05

async def teardown(self):
    result = self._outcome.result
    if any(test is self for test, _ in result.errors + result.failures):
        evidence = {'test': self.id(), 'entry': self.pub.entry_diagnostics,
                    'events': getattr(self, 'events', [])[-30:],
                    'step_timeout': self.pub.step_timeout}
        output = Path(os.environ.get('JUXIN_EMBEDDED_FAILURE') or Path(self.tmp.name)/'failure.json').parent
        page = self.worker.page
        try:
            evidence['page'] = await asyncio.wait_for(page.evaluate('''() => ({
                path: location.pathname,
                visibility: document.visibilityState,
                createEntry: Boolean(document.querySelector('[data-juxin-create-entry]')),
                postMenu: Boolean(document.querySelector('#postmenu,[data-juxin-post-menu]')),
                composer: (() => { const el=document.querySelector('#composer');
                    return el ? {connected:el.isConnected, display:getComputedStyle(el).display,
                        title:el.querySelector('h2')?.textContent,
                        controls:[...el.querySelectorAll('button')].map(b=>b.textContent.slice(0,80)),
                        files:el.querySelectorAll('input[type=file]').length} : null; })()
            })'''), 3)
        except Exception as exc:
            evidence['page_error'] = str(exc)[:400]
        print('CHECK posting failure '+json.dumps(evidence, ensure_ascii=False), flush=True)
        try:
            output.mkdir(parents=True, exist_ok=True)
            (output/'embedded-posting-failure.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        except OSError as exc:
            print('CHECK posting evidence write failed '+str(exc)[:200], flush=True)
        try:
            await asyncio.wait_for(page.screenshot(path=str(output/'embedded-posting-failure.png'), timeout=3000), 4)
        except Exception as exc:
            print('CHECK posting screenshot unavailable '+str(exc)[:200], flush=True)
    await self.context.unroute_all()
    for page in list(self.context.pages):await page.close()
    await self.browser.close();await self.p.stop();self.tmp.cleanup()

original_flow=fixtures.ScreenshotPostingTests.setup_flow
async def embedded_flow(self, **kwargs):
    assets=await original_flow(self, **kwargs)
    # This is a compatibility gate through the real scoped CDP bridge, not a
    # sub-second speed benchmark. Use the production per-stage bound; the
    # parent watchdog still bounds the entire suite and rejects any timeout.
    self.pub.step_timeout=fixtures.InstagramPublisher.step_timeout
    self.pub.confirmation_timeout=10
    return assets
fixtures.ScreenshotPostingTests.setup_flow=embedded_flow
fixtures.ScreenshotPostingTests.asyncSetUp=setup
fixtures.ScreenshotPostingTests.asyncTearDown=teardown
suite=unittest.TestSuite(fixtures.ScreenshotPostingTests(name) for name in (
    'test_menu_reels_first_location_success_done_then_home',
    'test_direct_upload_without_post_menu_and_blank_location',
    'test_delayed_menu_with_post_live_ad_and_icon_label',
))
fixtures.PostingDOMTests.asyncSetUp=setup
fixtures.PostingDOMTests.asyncTearDown=teardown
suite.addTest(fixtures.PostingDOMTests('test_crop_original_selected_before_continue'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():raise SystemExit(1)
print('PASS embedded posting: direct upload, optional Post menu, delayed icon menu; share confirmed once')
