"""Actual React click regressions. All browser requests stay in the local fixture."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

class NurtureDeleteUIR41Tests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.bundle=Path(cls.tmp.name)/'fixture.js'
        subprocess.run(['node','renderer/tests/build-nurture-r41-fixture.mjs',str(cls.bundle)],cwd=Path(__file__).resolve().parents[2],check=True,capture_output=True,encoding="utf-8",timeout=30)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p=await async_playwright().start()
        self.addAsyncCleanup(self.p.stop)
        path=os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE','')
        self.browser=await self.p.chromium.launch(**({'executable_path':path} if path else {'channel':'chromium'}),headless=True,args=['--no-sandbox'])
        self.addAsyncCleanup(self.browser.close)
        self.page=await self.browser.new_page(viewport={'width':1440,'height':960})
        async def route(request):
            url=request.request.url
            if url.endswith('.js'):await request.fulfill(content_type='text/javascript',body=self.bundle.read_text())
            elif url.endswith('.css'):await request.fulfill(content_type='text/css',body=self.bundle.with_suffix('.css').read_text())
            else:await request.fulfill(content_type='text/html',body='<meta charset="utf-8"><style>*{box-sizing:border-box}body{margin:0;font:14px Arial,sans-serif;background:#0b1220}</style><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>')
        await self.page.route('**/*',route)
        await self.page.goto('https://nurture.fixture/')
        await self.page.get_by_role('button',name='异常任务',exact=True).click()
        await self.row('failed-one').wait_for()

    def row(self,ident):return self.page.locator('.nurture-job-card').filter(has_text=ident+'：')

    async def test_delete_preserves_error_history_and_other_tasks(self):
        from playwright.async_api import expect
        await self.row('failed-one').get_by_role('button',name='删除异常',exact=True).click()
        await expect(self.row('failed-one')).to_have_count(0)
        await expect(self.row('failed-two')).to_have_count(1)
        await expect(self.page.locator('.studio-archive-feedback')).to_contain_text('历史记录查看')
        self.assertEqual([{'action':'delete_failed_nurture','job_id':'failed-one'}],await self.page.evaluate('window.fixture.commands'))
        await self.page.get_by_role('button',name='历史记录',exact=True).click()
        row=self.row('failed-one');await expect(row).to_contain_text('已移出异常列表')
        await expect(row).to_contain_text('没有可浏览的快拍')
        await expect(row).to_contain_text('1/22 步')
        await expect(row.get_by_role('button')).to_have_count(0)
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await expect(row).to_have_count(1)
        await self.page.get_by_role('button',name='异常任务',exact=True).click()
        await expect(self.row('failed-one')).to_have_count(0)

    async def test_block_and_network_error_do_not_remove_or_claim_success(self):
        from playwright.async_api import expect
        for mode,text in [('blocked','任务仍在释放窗口'),('error','本机服务暂时不可用')]:
            await self.page.evaluate('(mode)=>window.fixture.mode=mode',mode)
            await self.row('failed-one').get_by_role('button',name='删除异常',exact=True).click()
            await expect(self.page.locator('.studio-archive-feedback.is-error')).to_contain_text(text)
            await expect(self.row('failed-one')).to_have_count(1)
            await expect(self.row('failed-one').get_by_role('button',name='重试',exact=True)).to_be_enabled()

    async def test_active_cleanup_and_unknown_results_are_protected(self):
        from playwright.async_api import expect
        await expect(self.row('active-cleanup').get_by_role('button',name='删除异常',exact=True)).to_be_disabled()
        await expect(self.row('active-cleanup').get_by_role('button',name='重试',exact=True)).to_be_disabled()
        await expect(self.row('needs-confirmation').get_by_role('button',name='删除异常',exact=True)).to_have_count(0)
        await expect(self.row('needs-confirmation').get_by_role('button',name='确认未执行',exact=True)).to_be_visible()
        await expect(self.row('archived')).to_have_count(0)

    async def test_pending_delete_blocks_retry_and_double_submission(self):
        from playwright.async_api import expect
        await self.page.evaluate('window.fixture.holdCommand=true')
        button=self.row('failed-one').get_by_role('button',name='删除异常',exact=True)
        await button.click();await self.page.wait_for_function('!!window.fixture.releaseCommand')
        await expect(button).to_be_disabled()
        await expect(self.row('failed-one').get_by_role('button',name='重试',exact=True)).to_be_disabled()
        await self.page.evaluate('window.fixture.releaseCommand()')
        await expect(self.row('failed-one')).to_have_count(0)
        self.assertEqual(1,len(await self.page.evaluate('window.fixture.commands')))

    async def test_older_snapshot_cannot_restore_archived_failure(self):
        from playwright.async_api import expect
        await self.page.evaluate('window.fixture.holdNext=true')
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await self.page.wait_for_function('!!window.fixture.release')
        await self.row('failed-one').get_by_role('button',name='删除异常',exact=True).click()
        await expect(self.row('failed-one')).to_have_count(0)
        await self.page.evaluate('window.fixture.release()')
        await self.page.evaluate('() => new Promise(requestAnimationFrame)')
        await expect(self.row('failed-one')).to_have_count(0)

    async def test_slow_refresh_is_not_starved_by_repeated_poll_ticks(self):
        from playwright.async_api import expect
        await self.page.evaluate('window.fixture.holdNext=true')
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await self.page.wait_for_function('!!window.fixture.release')
        before=await self.page.evaluate('window.fixture.snapshots')
        await self.page.evaluate('()=>{window.fixture.poll();window.fixture.poll();window.fixture.poll()}')
        self.assertEqual(before,await self.page.evaluate('window.fixture.snapshots'))
        await self.page.evaluate('window.fixture.release()')
        await self.page.evaluate('() => new Promise(requestAnimationFrame)')
        await self.page.evaluate('window.fixture.poll()')
        await self.page.wait_for_function('(before)=>window.fixture.snapshots>before',arg=before)
        await expect(self.row('failed-one')).to_have_count(1)

    async def test_narrow_layout_keeps_exception_controls_reachable(self):
        from playwright.async_api import expect
        await self.page.set_viewport_size({'width':900,'height':900})
        button=self.row('failed-one').get_by_role('button',name='删除异常',exact=True)
        await button.scroll_into_view_if_needed();await expect(button).to_be_visible()
        box=await button.bounding_box();self.assertGreaterEqual(box['x'],0);self.assertLessEqual(box['x']+box['width'],900)
        self.assertLessEqual(await self.page.evaluate('document.documentElement.scrollWidth'),900)
        folder=os.environ.get('IGAC_R41_SCREENSHOTS')
        if folder:
            Path(folder).mkdir(parents=True,exist_ok=True)
            await self.page.screenshot(path=str(Path(folder)/'r41-nurture-exceptions-900.png'))
            await self.page.set_viewport_size({'width':1440,'height':960})
            await self.row('failed-one').scroll_into_view_if_needed()
            await self.page.screenshot(path=str(Path(folder)/'r41-nurture-exceptions.png'))
