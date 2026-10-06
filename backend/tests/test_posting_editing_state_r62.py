"""Deterministic async composer contracts independent of browser availability.

Models delayed renders/acks, not a successful post. Production upload/wait loop is
under test, and every case asserts no submit fence or real external effect.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from app.errors import ValidationError
from app.instagram_publisher import InstagramPublisher


class EditingStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser=SimpleNamespace(page=object(),checkpoint=AsyncMock(),guard=AsyncMock(),before_effect=AsyncMock())
        self.pub=InstagramPublisher(self.browser);self.pub.poll_seconds=.02
        self.stage='crop';self.pending=[];self.clicks=[];self.ready=True
        self.input=SimpleNamespace(set_input_files=AsyncMock())
        self.inputs=SimpleNamespace(count=AsyncMock(return_value=1),first=self.input)
        self.root=SimpleNamespace(locator=lambda _:self.inputs,get_by_text=lambda *args,**kwargs:self.heading,
                                  evaluate=AsyncMock(side_effect=lambda *args:self.ready))
        self.heading=SimpleNamespace(inner_text=AsyncMock(side_effect=lambda:'裁剪' if self.stage=='crop' else '编辑'))
        self.box=object();self.next=SimpleNamespace(click=AsyncMock(side_effect=self.click))
        self.pub.pin_composer=AsyncMock(return_value=self.root)
        self.pub.dialog=AsyncMock(return_value=self.root)
        self.pub.visible=AsyncMock(side_effect=lambda _:self.heading)
        self.pub.caption_box=AsyncMock(side_effect=lambda _:self.box if self.stage=='caption' else None)
        self.pub.forward=AsyncMock(return_value=self.next)
        self.original_wait=self.pub.wait_for
        async def bounded(find,message,timeout=None):return await self.original_wait(find,message,timeout=self.budget)
        self.pub.wait_for=bounded;self.budget=3
        self.delays={'crop':1.6,'edit':.65}
        self.crop=patch('app.instagram_crop.select_original',new=AsyncMock())
        self.crop.start()

    async def asyncTearDown(self):
        self.crop.stop()
        for task in self.pending:task.cancel()
        await asyncio.gather(*self.pending,return_exceptions=True)

    async def click(self,**kwargs):
        self.clicks.append(self.stage)
        # A React handler may debounce a render. Reclicking postpones it;
        # repeated control actions cannot be treated as forward progress.
        for task in self.pending:task.cancel()
        if self.stage not in self.delays:return
        async def render(stage):
            await asyncio.sleep(self.delays[stage]);self.stage='edit' if stage=='crop' else 'caption'
        self.pending.append(asyncio.create_task(render(self.stage)))

    async def upload(self):
        try:return await self.pub.upload(self.root,[{'path':'fixture.jpg'}])
        finally:
            self.browser.before_effect.assert_not_awaited()
            self.assertFalse(self.pub.submitted)

    async def test_slow_crop_and_edit_receive_one_click_each(self):
        _,box=await self.upload()
        self.assertIs(self.box,box)
        self.assertEqual(['crop','edit'],self.clicks)

    async def test_stuck_crop_is_finite_without_reclick(self):
        self.delays={};self.budget=.15
        with self.assertRaises(ValidationError):await self.upload()
        self.assertEqual(['crop'],self.clicks)

    async def test_upload_busy_state_blocks_continue(self):
        self.ready=False;self.budget=.15
        with self.assertRaises(ValidationError):await self.upload()
        self.assertEqual([],self.clicks)

    async def test_guard_failure_is_not_hidden_by_transient_dom_retry(self):
        self.browser.guard.side_effect=ValidationError('账号已变化')
        with self.assertRaisesRegex(ValidationError,'账号已变化'):await self.upload()
        self.assertEqual([],self.clicks)

    async def test_successful_dom_click_followed_by_backward_stage_is_not_repeated(self):
        self.delays={'crop':.03,'edit':.03}
        async def click(**kwargs):
            self.clicks.append(self.stage)
            self.stage='edit' if self.stage=='crop' else 'crop'
        self.next.click.side_effect=click
        with self.assertRaises(ValidationError):await self.upload()
        self.assertEqual(['crop','edit'],self.clicks)


if __name__=='__main__':unittest.main()
