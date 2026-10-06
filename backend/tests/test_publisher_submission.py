"""Exercise the real submission orchestrator at its external browser boundary.

These tests verify safety decisions, not Instagram's live DOM compatibility.
"""
import asyncio
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.errors import ValidationError
from app.instagram_publisher import InstagramPublisher
from app.studio_worker import ResultUncertain


class PublisherSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'asset.jpg';self.path.write_bytes(b'fixture')
        self.events=[]
        async def effect(message):self.events.append('fence')
        self.browser=SimpleNamespace(page=SimpleNamespace(),checkpoint=AsyncMock(),guard=AsyncMock(),before_effect=effect)
        self.publisher=InstagramPublisher(self.browser)
        self.publisher.create=AsyncMock(return_value=SimpleNamespace())
        self.box=SimpleNamespace(fill=AsyncMock(),evaluate=AsyncMock(return_value='caption'))
        self.root=SimpleNamespace()
        self.publisher.upload=AsyncMock(return_value=(self.root,self.box))
        self.share=SimpleNamespace(click=AsyncMock(side_effect=self.click))
        self.publisher.control=AsyncMock(return_value=self.share)
        self.assets=[{'id':'a','path':str(self.path),'media_type':'photo'}]
        self.publisher.location=AsyncMock()
        self.publisher.dialog=AsyncMock(return_value=None)
        self.publisher.finish=AsyncMock(return_value={})
        self.publisher.step_timeout=.01;self.publisher.confirmation_timeout=.01;self.publisher.poll_seconds=.001

    async def asyncTearDown(self):self.temp.cleanup()
    async def click(self,**kwargs):self.events.append('click')

    async def test_no_confirmation_fences_once_and_never_reclicks(self):
        with self.assertRaises(ResultUncertain):await self.publisher.publish(self.assets,'caption','')
        self.assertEqual(['fence','click'],self.events)
        self.share.click.assert_awaited_once()

    async def test_caption_mismatch_stops_before_side_effect(self):
        self.box.evaluate.return_value='modified by page'
        with self.assertRaises(ValidationError):await self.publisher.publish(self.assets,'caption','')
        self.assertEqual([],self.events)

    async def test_unmatched_location_stops_before_side_effect(self):
        self.publisher.location.side_effect=ValidationError('place missing')
        with self.assertRaises(ValidationError):await self.publisher.publish(self.assets,'caption','Exact Place')
        self.assertEqual([],self.events)

    async def test_caption_and_location_verified_before_submission(self):
        async def location(*args):self.events.append('location')
        self.publisher.location.side_effect=location
        with self.assertRaises(ResultUncertain):await self.publisher.publish(self.assets,'caption','Exact Place')
        self.box.fill.assert_awaited_once_with('caption',timeout=10000)
        self.assertEqual(['location','fence','click'],self.events)

    async def test_success_uses_composer_confirmation_and_does_not_invent_url(self):
        confirmed=SimpleNamespace(get_by_text=lambda _:object(),locator=lambda _:SimpleNamespace(count=AsyncMock(return_value=0)))
        self.publisher.dialog.return_value=confirmed
        self.publisher.status_text=AsyncMock(side_effect=[None,object()])
        result=await self.publisher.publish(self.assets,'caption','')
        self.assertEqual(1,result['published']);self.assertEqual('instagram_dialog',result['verification'])
        self.assertNotIn('post_url',result);self.assertEqual(['fence','click'],self.events)

    async def test_reject_missing_media_before_navigation(self):
        self.path.unlink()
        with self.assertRaises(ValidationError):await self.publisher.publish(self.assets,'caption','')
        self.publisher.create.assert_not_awaited()

    async def test_video_carousel_is_rejected_before_navigation(self):
        self.assets[0]['media_type']='video'
        with self.assertRaises(ValidationError):await self.publisher.publish(self.assets*2,'caption','')
        self.publisher.create.assert_not_awaited()
