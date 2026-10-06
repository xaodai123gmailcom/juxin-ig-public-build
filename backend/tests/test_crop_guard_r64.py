"""Guard callback failures must be observed immediately before crop actions."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.errors import ValidationError
from app.instagram_crop import select_original
from app.instagram_publisher import InstagramPublisher


class CropGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.allowed=True;self.option_visible=False
        self.browser=SimpleNamespace(page=object(),checkpoint=AsyncMock(),guard=AsyncMock(side_effect=self.guard),progress=AsyncMock(side_effect=self.progress))
        self.pub=InstagramPublisher(self.browser);self.pub.poll_seconds=.01
        self.heading=object()
        self.option=SimpleNamespace(click=AsyncMock(side_effect=self.select),evaluate=AsyncMock(return_value=False))
        self.button=SimpleNamespace(click=AsyncMock(side_effect=self.open_menu))
        self.root=SimpleNamespace(get_by_text=lambda pattern,**kw:self.heading if 'Crop' in pattern.pattern else self.option,
            locator=lambda _:self.button,evaluate=AsyncMock(return_value={'count':1,'method':'crop_label'}))
        self.pub.dialog=AsyncMock(return_value=self.root);self.pub.pin_composer=AsyncMock(return_value=self.root)
        self.pub.visible=AsyncMock(side_effect=lambda loc:loc if loc is self.heading or self.option_visible else None)
        wait=self.pub.wait_for
        async def bounded(find,message,timeout=None):return await wait(find,message,timeout=.15)
        self.pub.wait_for=bounded

    async def guard(self):
        if not self.allowed:raise ValidationError('账号或窗口占用已变化')
    async def progress(self,message):
        if self.fail_at in message:self.allowed=False
    async def open_menu(self,**kwargs):self.option_visible=True
    async def select(self,**kwargs):self.option_visible=False

    async def test_guard_change_during_menu_progress_prevents_ratio_click(self):
        self.fail_at='正在打开裁剪比例'
        with self.assertRaisesRegex(ValidationError,'账号或窗口占用已变化'):
            await select_original(self.pub,self.root)
        self.button.click.assert_not_awaited();self.option.click.assert_not_awaited()
        self.assertFalse(self.pub.submitted)

    async def test_guard_change_during_original_progress_prevents_option_click(self):
        self.fail_at='正在选择原版比例'
        with self.assertRaisesRegex(ValidationError,'账号或窗口占用已变化'):
            await select_original(self.pub,self.root)
        self.button.click.assert_awaited_once();self.option.click.assert_not_awaited()
        self.assertFalse(self.pub.submitted)

    async def test_healthy_guard_allows_exactly_one_ratio_and_original_click(self):
        self.fail_at='never'
        await select_original(self.pub,self.root)
        self.button.click.assert_awaited_once();self.option.click.assert_awaited_once()
        self.assertEqual('original_selected',self.pub.entry_diagnostics['crop']['status'])
        self.assertFalse(self.pub.submitted)

if __name__=='__main__':unittest.main()
