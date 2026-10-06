"""Click the actual React cleanup UI using a test-only bridge and Chromium."""
import os,subprocess,tempfile,unittest
from pathlib import Path

class StudioCleanupUITests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.bundle=Path(cls.tmp.name)/'fixture.js'
        project=Path(__file__).resolve().parents[2]
        subprocess.run(['node','renderer/tests/build-cleanup-fixture.mjs',str(cls.bundle)],cwd=project,check=True,capture_output=True,encoding="utf-8",timeout=30)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p=await async_playwright().start()
        path=os.environ.get('IGAC_POSTING_TEST_BROWSER','')
        if not path and not Path(self.p.chromium.executable_path).is_file():
            await self.p.stop()
            if os.environ.get('IGAC_REQUIRE_POSTING_BROWSER')=='1':self.fail('Chromium runtime missing')
            self.skipTest('Chromium required')
        self.browser=await self.p.chromium.launch(**({'executable_path':path} if path else {'channel':'chromium'}),headless=True,args=['--no-sandbox'])
        self.page=await self.browser.new_page(viewport={'width':1264,'height':715})
        async def route(request):
            url=request.request.url
            if url.endswith('.js'):await request.fulfill(content_type='text/javascript',body=self.bundle.read_text())
            elif url.endswith('.css'):await request.fulfill(content_type='text/css',body=self.bundle.with_suffix('.css').read_text())
            else:await request.fulfill(content_type='text/html',body='<link rel="stylesheet" href="/fixture.css"><div id="root" style="margin:40px"></div><script src="/fixture.js"></script>')
        await self.page.route('**/*',route)
        await self.page.goto('https://cleanup.fixture/')
        await self.page.get_by_role('button',name='删除素材',exact=True).first.wait_for()

    async def asyncTearDown(self):
        if hasattr(self,'browser'):await self.browser.close();await self.p.stop()

    async def test_success_removes_card_selection_and_disables_old_retry(self):
        from playwright.async_api import expect
        await self.page.get_by_role('button',name='删除素材',exact=True).first.click()
        await expect(self.page.locator('.studio-media-grid .studio-media')).to_have_count(1)
        await expect(self.page.locator('.studio-cleanup-notice')).to_contain_text('3 个本地文件')
        await expect(self.page.locator('.studio-media-grid input:checked')).to_have_count(0)
        await self.page.get_by_role('button',name='异常任务',exact=True).click()
        await expect(self.page.get_by_role('button',name='重试',exact=True)).to_be_disabled()
        await expect(self.page.get_by_text('原始失败记录',exact=True)).to_be_visible()

    async def test_blocking_task_reason_visible_at_materials_and_card_stays(self):
        from playwright.async_api import expect
        await self.page.evaluate("window.fixture.mode='blocked'")
        await self.page.get_by_role('button',name='删除素材',exact=True).first.click()
        notice=self.page.locator('.studio-cleanup-notice')
        await expect(notice).to_contain_text('窗口一 · 任务 abc12345')
        await expect(notice).to_contain_text('核验发布结果')
        await expect(self.page.locator('.studio-media-grid .studio-media')).to_have_count(2)
        rect=await notice.bounding_box();self.assertGreater(rect['y']+rect['height'],0);self.assertLess(rect['y'],715)

    async def test_file_error_is_reported_next_to_materials(self):
        from playwright.async_api import expect
        await self.page.evaluate("window.fixture.mode='error'")
        await self.page.get_by_role('button',name='删除素材',exact=True).first.click()
        await expect(self.page.locator('.studio-cleanup-notice')).to_contain_text('文件被占用')
        await expect(self.page.locator('.studio-media-grid .studio-media')).to_have_count(2)

    async def test_older_snapshot_cannot_restore_deleted_card(self):
        from playwright.async_api import expect
        await self.page.evaluate('window.fixture.holdNext=true')
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await self.page.wait_for_function('!!window.fixture.release')
        await self.page.get_by_role('button',name='删除素材',exact=True).first.click()
        await expect(self.page.locator('.studio-media-grid .studio-media')).to_have_count(1)
        await self.page.evaluate('window.fixture.release()')
        await self.page.evaluate('() => new Promise(requestAnimationFrame)')
        await expect(self.page.locator('.studio-media-grid .studio-media')).to_have_count(1)

    async def test_auto_defaults_custom_count_and_two_window_submission(self):
        from playwright.async_api import expect
        await self.page.goto('https://cleanup.fixture/?automatic=1')
        await expect(self.page.get_by_role('combobox',name='素材来源',exact=True)).to_have_value('pexels')
        count=self.page.get_by_label('每帖图片数量',exact=False)
        await expect(count).to_have_value('3')
        await self.page.get_by_label('搜索 Pexels 素材关键词').fill('美食')
        await self.page.get_by_text('窗口一',exact=True).click()
        await self.page.get_by_text('窗口二',exact=True).click()
        start=self.page.get_by_role('button',name='开始发帖 · 2 个窗口')
        await count.fill('0');await expect(start).to_be_disabled()
        await count.fill('4');await expect(start).to_be_enabled();await start.click()
        commands=await self.page.evaluate('window.fixture.commands')
        cfg=commands[-1]['config']
        self.assertEqual(4,cfg['image_count']);self.assertEqual('pexels',cfg['source'])
        self.assertFalse(cfg['auto_caption']);self.assertEqual(['window-one','window-two'],commands[-1]['profile_ids'])
        await count.fill('1');await start.click()
        self.assertEqual(1,(await self.page.evaluate('window.fixture.commands'))[-1]['config']['image_count'])

    async def test_optional_ai_models_and_preferences_saved_with_template(self):
        from playwright.async_api import expect
        await self.page.goto('https://cleanup.fixture/?automatic=1')
        await self.page.get_by_role('combobox',name='素材来源',exact=True).select_option('ai')
        await self.page.get_by_label('画面描述',exact=True).fill('午餐')
        await self.page.get_by_label('图片模型 ID',exact=False).fill('custom-image-model')
        await self.page.get_by_label('自动生成文案和标签',exact=False).check()
        await self.page.get_by_label('文案模型 ID',exact=False).fill('custom-vision-model')
        await self.page.get_by_label('AI 文案要求',exact=False).fill('轻松、简短')
        await self.page.get_by_role('button',name='保存模板',exact=True).click()
        cfg=(await self.page.evaluate('window.fixture.commands'))[-1]['config']
        self.assertEqual('custom-image-model',cfg['image_model']);self.assertEqual('custom-vision-model',cfg['caption_model'])
        self.assertEqual('轻松、简短',cfg['caption_instructions']);self.assertTrue(cfg['auto_caption'])
        await self.page.get_by_label('自动生成文案和标签',exact=False).uncheck()
        await self.page.get_by_role('combobox',name='素材来源',exact=True).select_option('pexels')
        await expect(self.page.get_by_label('文案模型 ID',exact=False)).to_have_count(0)
        await self.page.get_by_role('button',name='保存模板',exact=True).click()
        self.assertFalse((await self.page.evaluate('window.fixture.commands'))[-1]['config']['auto_caption'])

    async def test_per_window_post_counts_successes_and_failed_read_are_separate(self):
        from playwright.async_api import expect
        await self.page.goto('https://cleanup.fixture/?automatic=1')
        row=self.page.locator('.studio-windows .formal-row').filter(has_text='窗口一')
        await expect(row).to_contain_text('主页帖子数：12')
        await expect(row).to_contain_text('本软件成功发帖：3')
        await expect(row).to_contain_text('@my_ig')
        row=self.page.locator('.studio-windows .formal-row').filter(has_text='窗口二')
        await expect(row).to_contain_text('主页帖子数：未读取')
        await expect(row).to_contain_text('本软件成功发帖：0')
        await expect(row).to_contain_text('请登录后重试')

    async def test_history_is_text_only_no_retry_and_deleted_record_stays_gone(self):
        from playwright.async_api import expect
        await self.page.get_by_role('button',name='历史记录',exact=True).click()
        panel=self.page.locator('.studio-panel').filter(has=self.page.get_by_role('heading',name='历史记录',exact=True))
        await expect(panel.locator('img')).to_have_count(0)
        await expect(panel.get_by_role('button',name='重试',exact=True)).to_have_count(0)
        await expect(panel).to_contain_text('成功 0 条')
        await panel.get_by_role('button',name='删除记录',exact=True).click()
        await expect(panel.get_by_text('原始失败记录',exact=True)).to_have_count(0)
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await expect(panel).to_contain_text('暂无记录')

    async def test_multiple_drafts_have_saved_window_targets_and_explicit_publish(self):
        from playwright.async_api import expect
        await self.page.goto('https://cleanup.fixture/?drafts=1')
        await self.page.get_by_role('button',name='素材备稿',exact=True).click()
        rows=self.page.locator('.studio-job')
        for i in range(2):
            await rows.nth(i).get_by_label('选择这份备稿').check()
            await rows.nth(i).get_by_label('指定窗口').select_option(['window-one','window-two'][i])
            await expect(self.page.get_by_text('指定窗口已保存',exact=True)).to_be_visible()
        await self.page.get_by_role('button',name='刷新',exact=True).click()
        await expect(rows.nth(0).get_by_label('指定窗口')).to_have_value('window-one')
        commands=await self.page.evaluate('window.fixture.commands')
        self.assertFalse(any(c['action'] in ('start','start_drafts') for c in commands))
        await self.page.get_by_role('button',name='发布已选备稿 · 2 个窗口',exact=True).click()
        commands=await self.page.evaluate('window.fixture.commands')
        self.assertEqual([{'draft_id':'draft-0','profile_id':'window-one'},{'draft_id':'draft-1','profile_id':'window-two'}],commands[-1]['assignments'])
        await self.page.get_by_role('button',name='素材备稿',exact=True).click()
        await expect(rows.nth(0).get_by_label('选择这份备稿')).to_be_disabled()
