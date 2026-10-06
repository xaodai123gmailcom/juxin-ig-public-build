"""Verified playback and one-action advancement; synthetic clock/DOM only."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from app.errors import ValidationError
from app.standalone_nurture import StandaloneNurture

class Clock:
    def __init__(self):self.now=0.;self.media=0.;self.credited=0.;self.paused=0.
    async def sleep(self,seconds):self.now+=seconds;self.media+=seconds
    async def credit(self,seconds):self.credited+=seconds
    def current(self):return {'key':'/reel/A','source':'media:A','state':'unliked','time':self.media,'duration':20.}
    def engine(self):
        b=SimpleNamespace(nurture_clock=lambda:self.now-self.paused,nurture_watched=self.credit,
            nurture_remaining=lambda:60-self.credited,nurture_elapsed=lambda:self.now-self.paused)
        n=StandaloneNurture(b);n.guard=AsyncMock();n.current=AsyncMock(side_effect=self.current)
        return n

class VerifiedWatchR62Tests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_media_never_earns_duration_and_fails_bounded(self):
        clock=Clock();n=clock.engine()
        async def sleep(seconds):clock.now+=seconds
        with patch('app.standalone_nurture.asyncio.sleep',sleep):
            with self.assertRaisesRegex(ValidationError,'播放'):
                await n.dwell(8)
        self.assertEqual(0,clock.credited);self.assertLessEqual(clock.now,6.25)

    async def test_buffering_is_excluded_then_eight_seconds_are_observed(self):
        clock=Clock();n=clock.engine()
        async def sleep(seconds):
            clock.now+=seconds
            if clock.now>2:clock.media+=seconds
        with patch('app.standalone_nurture.asyncio.sleep',sleep):actual=await n.dwell(8)
        self.assertAlmostEqual(8,actual);self.assertAlmostEqual(8,clock.credited);self.assertAlmostEqual(10,clock.now)

    async def test_looping_media_and_pause_count_only_playback(self):
        clock=Clock();clock.media=18.;n=clock.engine();paused=False
        async def current():
            nonlocal paused
            if clock.now>=2 and not paused:
                clock.now+=40;clock.paused+=40;paused=True
            return clock.current()
        async def sleep(seconds):clock.now+=seconds;clock.media=(clock.media+seconds)%20
        n.current=AsyncMock(side_effect=current)
        with patch('app.standalone_nurture.asyncio.sleep',sleep):actual=await n.dwell(8)
        self.assertAlmostEqual(8,actual);self.assertAlmostEqual(8,clock.credited);self.assertAlmostEqual(48,clock.now)

    async def test_replaced_video_never_earns_the_old_video_credit(self):
        clock=Clock();n=clock.engine()
        async def current():
            value=clock.current()
            if clock.now>=2:value['source']='different'
            return value
        n.current=AsyncMock(side_effect=current)
        with patch('app.standalone_nurture.asyncio.sleep',clock.sleep):
            with self.assertRaises(ValidationError):await n.dwell(8)
        self.assertLess(clock.credited,2)

    async def test_fractional_final_budget_is_never_exceeded(self):
        clock=Clock();n=clock.engine();n.browser.nurture_remaining=lambda:8.1-clock.credited
        with patch('app.standalone_nurture.asyncio.sleep',clock.sleep):actual=await n.dwell(20)
        self.assertAlmostEqual(8.1,actual);self.assertAlmostEqual(8.1,clock.credited)

class Locator:
    def __init__(self,page,count):self.page=page;self.n=count
    async def count(self):return self.n
    def nth(self,index):return self
    async def is_visible(self):return True
    async def click(self,**kw):self.page.effects.append('click')
    async def press(self,key,**kw):self.page.effects.append(key)

class Page:
    def __init__(self,buttons=0):self.buttons=buttons;self.effects=[];self.safe=True;self.label='Next reel'
    def get_by_role(self,role,name):return Locator(self,self.buttons if name.fullmatch(self.label) else 0)
    def locator(self,selector):return Locator(self,1)
    async def evaluate(self,*args):return self.safe

class SafeAdvanceR62Tests(unittest.IsolatedAsyncioTestCase):
    def make(self,buttons=0):
        clock=Clock();page=Page(buttons);n=StandaloneNurture(SimpleNamespace(page=page,nurture_clock=lambda:clock.now));n.guard=AsyncMock()
        async def current():return dict(key='/reel/B' if page.effects else '/reel/A',source='media',state='unliked')
        n.current=AsyncMock(side_effect=current)
        return clock,page,n

    async def test_icon_only_uses_one_native_key_and_two_stable_observations(self):
        clock,page,n=self.make()
        with patch('app.standalone_nurture.asyncio.sleep',clock.sleep):await n.advance(await n.current())
        self.assertEqual(['ArrowDown'],page.effects);self.assertGreaterEqual(n.current.await_count,5)

    async def test_supported_next_labels_use_one_click_and_no_key(self):
        for label in ('Next reel','Next video','Down chevron','Arrow down','向下滚动'):
            clock,page,n=self.make(1);page.label=label
            with patch('app.standalone_nurture.asyncio.sleep',clock.sleep):await n.advance(await n.current())
            self.assertEqual(['click'],page.effects)

    async def test_noop_transient_identity_and_unsafe_focus_fail_without_retry(self):
        for mode in ('noop','transient','focus','multiple'):
            clock,page,n=self.make(2 if mode=='multiple' else 1);page.safe=mode!='focus';reads=0
            async def current():
                nonlocal reads
                reads+=1
                return dict(key='/reel/B' if mode=='transient' and reads==4 else '/reel/A',source='media',state='unliked')
            n.current=AsyncMock(side_effect=current)
            with patch('app.standalone_nurture.asyncio.sleep',clock.sleep):
                with self.assertRaises(ValidationError):await n.advance(await n.current())
            self.assertEqual(0 if mode in ('focus','multiple') else 1,len(page.effects))
            self.assertLessEqual(clock.now,6.001)

    async def test_missing_or_changed_preaction_video_is_not_silent_success(self):
        for current in (None,{'key':'/reel/B','source':'media','state':'unliked'}):
            clock,page,n=self.make(1)
            n.current=AsyncMock(return_value=current)
            with self.assertRaises(ValidationError):await n.advance({'key':'/reel/A','source':'media','state':'unliked'})
            self.assertEqual([],page.effects)
