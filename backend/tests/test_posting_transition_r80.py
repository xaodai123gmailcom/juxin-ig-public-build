"""Bounded create/upload transitions with a deterministic slow-CDP clock."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.errors import ValidationError
from app.instagram_publisher import InstagramPublisher


class PostingTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 0.0
        self.browser = SimpleNamespace(page=object(), checkpoint=AsyncMock(), guard=AsyncMock())
        self.pub = InstagramPublisher(self.browser)
        self.pub.step_timeout = 1
        self.pub.poll_seconds = .1
        self.pub.open_posting_page = AsyncMock()
        self.pub.record_diagnostics = AsyncMock()
        self.create = SimpleNamespace(click=AsyncMock())
        self.menu = SimpleNamespace(click=AsyncMock())
        self.upload = object()
        self.pub.find_create = AsyncMock(return_value=self.create)
        self.pub.post_menu_item = AsyncMock(return_value=self.menu)

    async def sleep(self, seconds):
        self.clock += seconds

    async def run_create(self):
        loop = SimpleNamespace(time=lambda: self.clock)
        with patch('app.instagram_publisher.asyncio.get_running_loop', return_value=loop), \
             patch('app.instagram_publisher.asyncio.sleep', side_effect=self.sleep):
            return await self.pub.create()

    async def test_slow_post_menu_click_gets_a_fresh_upload_confirmation_budget(self):
        clicked = False
        post_click_reads = 0

        async def menu_click(**kwargs):
            nonlocal clicked
            self.clock += 1.2  # A successful CDP click consumes the old deadline.
            clicked = True

        async def upload_stage():
            nonlocal post_click_reads
            if clicked:
                post_click_reads += 1
                if post_click_reads == 2:
                    return self.upload
            return None

        self.menu.click.side_effect = menu_click
        self.pub.upload_stage = AsyncMock(side_effect=upload_stage)
        self.assertIs(self.upload, await self.run_create())
        self.assertEqual(2, post_click_reads)
        self.create.click.assert_awaited_once()
        self.menu.click.assert_awaited_once()
        self.assertEqual('create_post_upload', self.pub.entry_diagnostics['create_path'])

    async def test_post_menu_discovered_in_final_poll_still_confirms_upload(self):
        async def delayed_menu():
            self.clock += 1.1
            return self.menu

        self.pub.post_menu_item.side_effect = delayed_menu
        self.pub.upload_stage = AsyncMock(side_effect=[None, None, self.upload])
        self.assertIs(self.upload, await self.run_create())
        self.menu.click.assert_awaited_once()

    async def test_direct_upload_does_not_click_optional_post_menu(self):
        self.pub.upload_stage = AsyncMock(side_effect=[None, self.upload])
        self.assertIs(self.upload, await self.run_create())
        self.create.click.assert_awaited_once()
        self.pub.post_menu_item.assert_not_awaited()
        self.assertEqual('create_upload', self.pub.entry_diagnostics['create_path'])

    async def test_menu_without_upload_times_out_without_reclick_or_publish(self):
        self.pub.upload_stage = AsyncMock(return_value=None)
        with self.assertRaisesRegex(ValidationError, '未进入上传素材窗口'):
            await self.run_create()
        self.create.click.assert_awaited_once()
        self.menu.click.assert_awaited_once()
        self.assertEqual('post_menu_clicked_waiting_upload', self.pub.entry_diagnostics['create_path'])
        self.assertFalse(self.pub.submitted)
        self.assertLess(self.clock, 2)
        self.assertEqual(1, self.pub.entry_diagnostics['wait_timeout']['budget_seconds'])
        self.assertIn('点击帖子后', self.pub.entry_diagnostics['wait_timeout']['message'])
        self.assertGreater(self.pub.entry_diagnostics['wait_timeout']['polls'], 0)

    async def test_missing_menu_and_upload_remains_bounded(self):
        self.pub.upload_stage = AsyncMock(return_value=None)
        self.pub.post_menu_item.return_value = None
        with self.assertRaisesRegex(ValidationError, '点击创建后'):
            await self.run_create()
        self.create.click.assert_awaited_once()
        self.menu.click.assert_not_awaited()
        self.assertLess(self.clock, 2)

    async def test_guard_failure_after_menu_is_not_swallowed(self):
        self.pub.upload_stage = AsyncMock(return_value=None)
        async def clicked(**kwargs):
            self.browser.guard.side_effect = ValidationError('账号需要验证')
        self.menu.click.side_effect = clicked
        with self.assertRaisesRegex(ValidationError, '账号需要验证'):
            await self.run_create()
        self.menu.click.assert_awaited_once()
        self.assertFalse(self.pub.submitted)


if __name__ == '__main__':
    unittest.main()
