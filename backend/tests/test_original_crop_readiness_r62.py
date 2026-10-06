"""Original-ratio acknowledgement must survive modal swaps and never fake success."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.errors import ValidationError
from app.instagram_crop import select_original
from app.instagram_publisher import InstagramPublisher


class OriginalCropReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser=SimpleNamespace(page=object(),checkpoint=AsyncMock(),guard=AsyncMock())
        self.pub=InstagramPublisher(self.browser);self.pub.poll_seconds=.01
        self.option_visible=True;self.confirmed=False;self.detached=False;self.replacement=False
        self.option=SimpleNamespace(click=AsyncMock(side_effect=self.click),evaluate=AsyncMock(side_effect=lambda *args:self.confirmed))
        self.heading=object()
        self.root=SimpleNamespace(get_by_text=lambda pattern,**kw:self.heading if 'Crop' in pattern.pattern else self.option)
        self.newroot=SimpleNamespace(get_by_text=self.root.get_by_text)
        self.pub.dialog=AsyncMock(side_effect=lambda:None if self.detached else self.newroot if self.replacement else self.root)
        self.pub.pin_composer=AsyncMock(side_effect=lambda root:root)
        self.pub.visible=AsyncMock(side_effect=lambda loc:loc if loc is self.heading or self.option_visible else None)
        self.original_wait=self.pub.wait_for
        async def bounded(find,message,timeout=None):return await self.original_wait(find,message,timeout=.15)
        self.pub.wait_for=bounded;self.task=None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

    async def click(self,**kwargs):
        self.option_visible=False;self.detached=True

    async def test_missing_crop_dialog_is_not_ratio_selection_success(self):
        with self.assertRaisesRegex(ValidationError,'菜单状态未确认'):
            await select_original(self.pub,self.root)
        self.option.click.assert_awaited_once()
        self.assertNotEqual('original_selected',self.pub.entry_diagnostics['crop'].get('status'))

    async def test_replacement_crop_returns_before_menu_dismissal_counts(self):
        async def click(**kwargs):
            await self.click()
            async def replace():
                await asyncio.sleep(.04);self.replacement=True;self.detached=False
            self.task=asyncio.create_task(replace())
        self.option.click.side_effect=click
        await select_original(self.pub,self.root)
        self.assertTrue(self.replacement)
        self.assertEqual('original_selected',self.pub.entry_diagnostics['crop']['status'])
        self.option.click.assert_awaited_once()

    async def test_open_menu_requires_explicit_selected_state(self):
        self.option.click.side_effect=lambda **kwargs:None
        with self.assertRaisesRegex(ValidationError,'菜单状态未确认'):
            await select_original(self.pub,self.root)
        self.assertNotEqual('original_selected',self.pub.entry_diagnostics['crop'].get('status'))
        self.confirmed=True
        await select_original(self.pub,self.root)
        self.assertEqual('original_selected',self.pub.entry_diagnostics['crop']['status'])


if __name__=='__main__':unittest.main()
