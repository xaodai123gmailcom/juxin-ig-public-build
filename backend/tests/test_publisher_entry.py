"""Composer boundary regression tests; these do not assert live Instagram DOM."""
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.instagram_publisher import InstagramPublisher


class Nodes:
    def __init__(self,*items):self.items=list(items)
    async def count(self):return len(self.items)
    def nth(self,index):return self.items[index]
    @property
    def first(self):return self.items[0]


class PublisherEntryTests(unittest.IsolatedAsyncioTestCase):
    def publisher(self,page):
        browser=SimpleNamespace(page=page,checkpoint=AsyncMock(),guard=AsyncMock())
        return InstagramPublisher(browser)

    async def test_startup_finishes_new_home_navigation_before_entry_detection(self):
        old=SimpleNamespace(goto=AsyncMock())
        fresh=SimpleNamespace(url='https://www.instagram.com/')
        publisher=self.publisher(old);root=object();order=[]
        async def open_page():order.append('new-home');return fresh
        async def upload_stage():
            self.assertIs(publisher.page,fresh)
            order.append('entry');return root
        publisher.browser.worker=SimpleNamespace(open_posting_page=AsyncMock(side_effect=open_page))
        publisher.upload_stage=AsyncMock(side_effect=upload_stage)
        publisher.find_create=AsyncMock()
        with patch("app.instagram_home.prepare_instagram_home",new=AsyncMock(return_value=[])):
            self.assertIs(root,await publisher.create())
        self.assertEqual(['new-home','entry'],order)
        old.goto.assert_not_awaited();publisher.find_create.assert_not_awaited()

    async def test_icon_label_or_svg_title_targets_clickable_ancestor(self):
        for label,title in [('新建',''),('','创建新帖子'),('New post','')]:
            with self.subTest(label=label,title=title):
                parent=SimpleNamespace(is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True))
                icon=SimpleNamespace(is_visible=AsyncMock(return_value=True),get_attribute=AsyncMock(return_value=label))
                icon.locator=lambda selector:Nodes(SimpleNamespace(text_content=AsyncMock(return_value=title))) if selector=='title' else Nodes(parent)
                publisher=self.publisher(SimpleNamespace(locator=lambda _:Nodes(icon)))
                publisher.control=AsyncMock()
                self.assertIs(parent,await publisher.find_create())
                publisher.control.assert_not_awaited()

    async def test_unlabelled_plus_and_profile_story_new_are_not_selected(self):
        icon=SimpleNamespace(is_visible=AsyncMock(return_value=True),get_attribute=AsyncMock(return_value='Plus icon'),locator=lambda _:Nodes())
        navigation=object()
        page=SimpleNamespace(locator=lambda selector:Nodes(icon) if selector=='svg' else navigation)
        publisher=self.publisher(page)
        async def controls(root,pattern):
            import re
            if root is page:self.assertIsNone(re.match(pattern,'新建'))
            else:self.assertIs(root,navigation)
            return None
        publisher.control=AsyncMock(side_effect=controls)
        self.assertIsNone(await publisher.find_create())

    async def test_local_files_sent_directly_to_input_in_order(self):
        upload=SimpleNamespace(set_input_files=AsyncMock())
        root=SimpleNamespace(locator=lambda _:Nodes(upload),get_by_text=lambda *a,**kw:Nodes())
        page=SimpleNamespace(expect_file_chooser=lambda **_:self.fail('file chooser unnecessary'))
        publisher=self.publisher(page);publisher.dialog=AsyncMock(return_value=root)
        box=object();publisher.caption_box=AsyncMock(return_value=box)
        assets=[{'path':'C:/Users/用户/Desktop/聚鑫国际素材/01.jpg'},{'path':'C:/Users/用户/Desktop/聚鑫国际素材/02.jpg'}]
        self.assertEqual((root,box),await publisher.upload(root,assets))
        upload.set_input_files.assert_awaited_once_with([a['path'] for a in assets],timeout=20000)

    async def test_chinese_upload_button_uses_file_chooser_when_input_is_absent(self):
        import re
        chosen=SimpleNamespace(set_files=AsyncMock())
        class Chooser:
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            @property
            def value(self):
                async def result():return chosen
                return result()
        page=SimpleNamespace(expect_file_chooser=lambda **_:Chooser())
        publisher=self.publisher(page)
        root=SimpleNamespace(locator=lambda _:Nodes(),get_by_text=lambda *a,**kw:Nodes());button=SimpleNamespace(click=AsyncMock())
        async def control(_root,pattern):
            self.assertIsNotNone(re.match(pattern,'从电脑中选择'))
            return button
        publisher.control=AsyncMock(side_effect=control)
        publisher.dialog=AsyncMock(return_value=root);publisher.caption_box=AsyncMock(return_value=object())
        await publisher.upload(root,[{'path':'C:/Desktop/素材.jpg'}])
        button.click.assert_awaited_once()
        chosen.set_files.assert_awaited_once_with(['C:/Desktop/素材.jpg'],timeout=20000)


class PostingPageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, fresh):
        from app.playwright_worker import PlaywrightWorker
        worker=PlaywrightWorker(object())
        worker.page=SimpleNamespace(close=AsyncMock())
        worker._context=SimpleNamespace(new_page=AsyncMock(return_value=fresh))
        worker._cdp_session=SimpleNamespace(detach=AsyncMock())
        worker._new_active_page_session=AsyncMock(return_value=SimpleNamespace(detach=AsyncMock()))
        return worker

    async def test_navigation_failure_keeps_original_page_and_disposes_new_resources(self):
        fresh=SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError('offline')),close=AsyncMock())
        worker=self.worker(fresh);old=worker.page;session=worker._cdp_session
        worker._worker_owned_page=old
        with self.assertRaisesRegex(RuntimeError,'offline'):await worker.open_posting_page()
        self.assertIs(old,worker.page);self.assertIs(old,worker._worker_owned_page)
        fresh.close.assert_awaited_once();old.close.assert_not_awaited();session.detach.assert_not_awaited()
        worker._new_active_page_session.return_value.detach.assert_awaited_once()

    async def test_cancelled_navigation_closes_new_tab_and_preserves_operator_page(self):
        import asyncio
        entered=asyncio.Event()
        async def navigate(*a,**kw):entered.set();await asyncio.Event().wait()
        fresh=SimpleNamespace(goto=navigate,close=AsyncMock())
        worker=self.worker(fresh);old=worker.page
        task=asyncio.create_task(worker.open_posting_page());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertIs(old,worker.page);old.close.assert_not_awaited();fresh.close.assert_awaited_once()

    async def test_page_created_after_timeout_is_reclaimed(self):
        import asyncio
        release=asyncio.Event();closed=asyncio.Event()
        fresh=SimpleNamespace(goto=AsyncMock(),close=AsyncMock(side_effect=closed.set))
        worker=self.worker(fresh);old=worker.page;worker.page_create_timeout_seconds=.01
        async def delayed():await release.wait();return fresh
        worker._context.new_page=delayed
        with self.assertRaises(TimeoutError):await worker.open_posting_page()
        release.set();await asyncio.wait_for(closed.wait(),1)
        self.assertIs(old,worker.page);old.close.assert_not_awaited();fresh.goto.assert_not_awaited()
