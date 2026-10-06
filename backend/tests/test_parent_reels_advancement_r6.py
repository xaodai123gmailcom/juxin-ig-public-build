"""Deterministic playback/cadence regressions; no account/network effects."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.parent_reels import ParentReelsAdapter, ReelsUnavailable, REEL_PROBE, run_parent_reels


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    async def sleep(self, seconds): self.now += seconds


class RunAdapter:
    def __init__(self, clock):
        self.clock = clock
        self.state = dict(key='/reel/A', source='media-A', liked=False)
        self.advances, self.likes, self.stops = [], [], 0
        self.change = True
        self.stop_at = 3
    async def open(self): pass
    async def current(self): return dict(self.state)
    async def like(self, expected, checkpoint):
        await checkpoint()
        self.likes.append(expected['key'])
        self.state['liked'] = True
    async def advance(self, key, checkpoint):
        await checkpoint()
        self.advances.append((key, self.clock()))
        if len(self.advances) == self.stop_at: raise asyncio.CancelledError
        if self.change:
            n = len(self.advances)
            self.state = dict(key='/reel/'+str(n), source='media-'+str(n), liked=False)
    async def stop(self): self.stops += 1


class ParentReelsCadenceR6Tests(unittest.IsolatedAsyncioTestCase):
    async def test_default_8_to_20_seconds_and_50_percent_stay_separate(self):
        clock = Clock(); adapter = RunAdapter(clock)
        durations = iter((8.0, 14.0, 20.0)); draws = iter((.499, .5, .9))
        with patch('app.parent_reels.random.uniform', side_effect=lambda low, high: self.assertEqual((8,20),(low,high)) or next(durations)):
            with self.assertRaises(asyncio.CancelledError):
                await run_parent_reels(adapter, AsyncMock(), draw=lambda:next(draws), sleep=clock.sleep, clock=clock)
        self.assertEqual(['/reel/A'], adapter.likes)
        self.assertEqual(1, adapter.stops)
        self.assertEqual(['/reel/A','/reel/1','/reel/2'], [x[0] for x in adapter.advances])
        expected = [8.15, 22.3, 42.45]
        for actual, target in zip([x[1] for x in adapter.advances], expected): self.assertAlmostEqual(actual, target)

    async def test_real_event_loop_holds_video_for_eight_seconds(self):
        adapter = RunAdapter(time.monotonic); adapter.stop_at = 1
        started = time.monotonic()
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(adapter, AsyncMock(), draw=lambda:.9, dwell=lambda:0)
        elapsed = adapter.advances[0][1] - started
        self.assertGreaterEqual(elapsed, 8.0)
        self.assertLess(elapsed, 20.0)
        self.assertEqual(1, adapter.stops)

    async def test_checkpoint_pause_during_decision_and_dwell_is_excluded(self):
        clock = Clock(); adapter = RunAdapter(clock); adapter.stop_at = 1
        slept_pause = False; claim_pause = False
        async def checkpoint():
            nonlocal slept_pause, claim_pause
            if adapter.likes and not claim_pause:
                clock.now += 41; claim_pause = True
            if clock.now > 43 and not slept_pause:
                clock.now += 60; slept_pause = True
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(adapter, checkpoint, draw=lambda:0, dwell=lambda:8, sleep=clock.sleep, clock=clock)
        self.assertAlmostEqual(109.15, adapter.advances[0][1])
        self.assertEqual(1, adapter.stops)

    async def test_noop_acknowledgement_stops_instead_of_repeating_video(self):
        clock = Clock(); adapter = RunAdapter(clock); adapter.change = False
        with self.assertRaisesRegex(ReelsUnavailable, 'identity not confirmed'):
            await run_parent_reels(adapter, AsyncMock(), draw=lambda:.9, dwell=lambda:8, sleep=clock.sleep, clock=clock)
        self.assertEqual(1, len(adapter.advances))
        self.assertEqual(1, adapter.stops)
        self.assertAlmostEqual(8.15, clock())

    async def test_cancelled_or_changed_media_never_advances(self):
        for cancel in (False, True):
            clock = Clock(); adapter = RunAdapter(clock)
            async def sleep(seconds):
                await clock.sleep(seconds)
                if clock() > 2:
                    if cancel: raise asyncio.CancelledError
                    adapter.state['source'] = 'different-media'
            with self.assertRaises((asyncio.CancelledError, ReelsUnavailable)):
                await run_parent_reels(adapter, AsyncMock(), draw=lambda:.9, dwell=lambda:8, sleep=sleep, clock=clock)
            self.assertEqual([], adapter.advances)
            self.assertEqual(1, adapter.stops)

    async def test_frozen_playback_is_bounded_and_does_not_earn_watch_time(self):
        clock = Clock(); adapter = RunAdapter(clock); adapter.state['time'] = 4.0
        with self.assertRaisesRegex(ReelsUnavailable, 'playback stalled'):
            await run_parent_reels(adapter, AsyncMock(), draw=lambda:.9, dwell=lambda:8, sleep=clock.sleep, clock=clock)
        self.assertEqual([], adapter.advances)
        self.assertAlmostEqual(6.15, clock())
        self.assertEqual(1, adapter.stops)

    async def test_short_buffer_stall_does_not_count_toward_watch_duration(self):
        clock = Clock(); adapter = RunAdapter(clock); adapter.stop_at = 1; adapter.state['time'] = 0.0
        async def sleep(seconds):
            await clock.sleep(seconds)
            if clock() > 2.15: adapter.state['time'] += seconds
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(adapter, AsyncMock(), draw=lambda:.9, dwell=lambda:8, sleep=sleep, clock=clock)
        self.assertAlmostEqual(10.15, adapter.advances[0][1])

    async def test_media_only_identity_browses_without_claiming_or_liking(self):
        clock = Clock(); adapter = RunAdapter(clock); adapter.stop_at = 1
        adapter.state.update(key='media:blob:local', can_like=False)
        claim = AsyncMock(side_effect=AssertionError('Invalid permalink must not be persisted'))
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(adapter, AsyncMock(), claim=claim, draw=lambda:self.fail('Unknown heart must not draw'), dwell=lambda:8, sleep=clock.sleep, clock=clock)
        self.assertEqual([], adapter.likes)
        claim.assert_not_called()


class Locator:
    def __init__(self, page, count, action): self.page,self.n,self.action=page,count,action
    async def count(self): return self.n
    def nth(self, index): return self
    async def is_visible(self): return True
    async def click(self, **kwargs): self.page.effects.append('click')
    async def press(self, key, **kwargs): self.page.effects.append(key)


class FakePage:
    url='https://www.instagram.com/reels/'
    def __init__(self, buttons=0): self.buttons,self.effects,self.safe,self.label=buttons,[],True,'Next reel'
    def get_by_role(self, role, name):
        return Locator(self,self.buttons if name.fullmatch(self.label) else 0,'click')
    def locator(self, selector): return Locator(self,1,'key')
    async def evaluate(self, *args): return self.safe


class ParentReelsAdvanceR6Tests(unittest.IsolatedAsyncioTestCase):
    async def make(self, buttons=0):
        page=FakePage(buttons); adapter=ParentReelsAdapter(SimpleNamespace(page=page,_guard=AsyncMock()))
        return page,adapter

    async def test_icon_only_control_uses_one_native_key_then_confirms_change(self):
        page,adapter=await self.make(); reads=[]
        async def current():
            reads.append(True)
            return dict(key='/reel/B' if page.effects else '/reel/A',source='media',liked=False)
        adapter.current=current
        with patch('app.parent_reels.asyncio.sleep',new=AsyncMock()):
            await adapter.advance('/reel/A',AsyncMock())
        self.assertEqual(['ArrowDown'],page.effects)
        self.assertEqual(4,len(reads))

    async def test_delayed_next_waits_without_extra_navigation(self):
        page,adapter=await self.make(1); page.label='Down chevron'; clock=Clock()
        async def current(): return dict(key='/reel/B' if clock()>2 else '/reel/A',source='media',liked=False)
        adapter.current=current
        with patch('app.parent_reels.asyncio.sleep',new=clock.sleep): await adapter.advance('/reel/A',AsyncMock())
        self.assertEqual(['click'],page.effects)
        self.assertGreater(clock(),2)
        self.assertLess(clock(),3)

    async def test_failed_next_is_bounded_and_never_blindly_retried(self):
        for buttons in (0,1):
            page,adapter=await self.make(buttons); clock=Clock()
            adapter.current=AsyncMock(return_value=dict(key='/reel/A',source='media',liked=False))
            with patch('app.parent_reels.asyncio.sleep',new=clock.sleep):
                with self.assertRaisesRegex(ReelsUnavailable,'no repeat action'): await adapter.advance('/reel/A',AsyncMock())
            self.assertEqual(1,len(page.effects))
            self.assertAlmostEqual(6,clock())

    async def test_ambiguous_buttons_or_text_focus_do_not_act(self):
        for buttons,safe in ((2,True),(0,False),(1,False)):
            page,adapter=await self.make(buttons);page.safe=safe
            adapter.current=AsyncMock(return_value=dict(key='/reel/A',source='media',liked=False))
            with self.assertRaises(ReelsUnavailable): await adapter.advance('/reel/A',AsyncMock())
            self.assertEqual([],page.effects)

    async def test_single_transient_new_identity_is_not_acknowledged(self):
        page,adapter=await self.make(1);calls=0;clock=Clock()
        async def current():
            nonlocal calls
            calls+=1
            return dict(key='/reel/B' if calls==3 else '/reel/A',source='media',liked=False)
        adapter.current=current
        with patch('app.parent_reels.asyncio.sleep',new=clock.sleep):
            with self.assertRaises(ReelsUnavailable): await adapter.advance('/reel/A',AsyncMock())
        self.assertEqual(['click'],page.effects)

    async def test_slow_media_load_has_overall_budget(self):
        page, adapter = await self.make()
        page.goto = AsyncMock(); adapter.guard = AsyncMock()
        clock = Clock(); adapter.clock = clock
        probes = []
        async def current():
            probes.append(True); clock.now += 1.0
            return None
        adapter.current = current
        with patch('app.parent_reels.asyncio.sleep', new=clock.sleep):
            with self.assertRaises(ReelsUnavailable): await adapter.open()
        self.assertEqual(3, len(probes))
        self.assertLess(clock(), 4.0)

    async def test_raw_probe_timeout_tracks_hostile_transport_until_cleanup(self):
        from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
        entered, release = asyncio.Event(), asyncio.Event()
        async def hostile(*args):
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
            return dict(key='/reel/A', source='media', liked=False)
        page = SimpleNamespace(url='https://www.instagram.com/reels/', evaluate=AsyncMock(side_effect=hostile))
        worker = PlaywrightWorker(None); worker.page = page; worker._guard = AsyncMock()
        adapter = ParentReelsAdapter(worker); adapter.dom_timeout = .01
        cleanup = None
        probe = asyncio.create_task(adapter.current())
        try:
            done, _ = await asyncio.wait({probe}, timeout=1)
            self.assertIn(probe, done, 'Raw probe must finish at its own hard deadline')
            with self.assertRaises(WorkerExecutionError) as error:
                probe.result()
            self.assertEqual('worker_not_connected', error.exception.code)
            self.assertTrue(worker._page_stage_abandoned)
            self.assertEqual(1, len(worker._late_lifecycle_tasks))
            await adapter.stop()  # No new raw evaluation on a poisoned transport.
            self.assertEqual(1, page.evaluate.await_count)
            with self.assertRaises(WorkerExecutionError): await adapter.current()
            worker.disconnect = AsyncMock()
            cleanup = asyncio.create_task(worker.wait_for_cleanup())
            await asyncio.sleep(.03)
            self.assertFalse(cleanup.done(), 'Cleanup must retain the hung old operation')
        finally:
            release.set()
            await asyncio.gather(probe, return_exceptions=True)
            if cleanup is not None: await asyncio.wait_for(cleanup, 1)
            await asyncio.sleep(0)
        self.assertFalse(worker._late_lifecycle_tasks)

    async def test_raw_next_control_lookup_timeout_cannot_send_a_key(self):
        from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
        page = FakePage(1)
        async def count(): await asyncio.Event().wait()
        page.get_by_role = lambda *args, **kwargs: SimpleNamespace(count=count)
        worker = PlaywrightWorker(None); worker.page = page
        adapter = ParentReelsAdapter(worker); adapter.dom_timeout = .01
        adapter.current = AsyncMock(return_value=dict(key='/reel/A',source='media',liked=False))
        with self.assertRaises(WorkerExecutionError):
            await asyncio.wait_for(adapter.advance('/reel/A',AsyncMock()), 1)
        self.assertEqual([], page.effects)
        self.assertFalse(worker._page_stage_abandoned)

    async def test_page_replacement_cannot_be_touched_by_stop(self):
        page,adapter=await self.make();page.evaluate=AsyncMock()
        adapter.worker.page=object()
        await adapter.stop()
        page.evaluate.assert_not_called()


class ParentReelsProbeR6Tests(unittest.TestCase):
    def test_real_probe_script_with_minimal_synthetic_dom(self):
        if not shutil.which('node'): self.skipTest('Node missing for JS probe unit fixture')
        script=Path(__file__).parent/'fixtures'/'parent_reels_probe_r6.cjs'
        result=subprocess.run(['node',str(script)],input=REEL_PROBE,text=True,capture_output=True,check=True)
        self.assertEqual(7,json.loads(result.stdout)['passed'])
