"""Offline policy/lifecycle tests. No account or network access."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.parent_reels import run_parent_reels, ReelsUnavailable
from app.execution_manager import ExecutionManager
from app.playwright_worker import WorkerExecutionError


class Adapter:
    def __init__(self, *, liked=False, fail=False):
        self.state = dict(key='/reel/one', source='video-one', liked=liked)
        self.opens = self.likes = self.advances = 0
        self.fail = fail
    async def open(self): self.opens += 1
    async def current(self): return dict(self.state)
    async def like(self, current, checkpoint):
        await checkpoint()
        self.likes += 1
        if self.fail: raise ReelsUnavailable('ambiguous click')
        self.state['liked'] = True
    async def advance(self, key, checkpoint):
        await checkpoint()
        self.advances += 1
        raise asyncio.CancelledError


async def okay(): pass


class ParentReelsPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def run_once(self, adapter, **kw):
        slept = []
        now = [0.0]
        async def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
            await asyncio.sleep(0)
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(adapter, okay, sleep=sleep, clock=lambda: now[0], **kw)
        return sum(slept) - .15

    async def test_probability_boundary_and_dwell_bounds(self):
        for draw, expected in [(0, 1), (.499, 1), (.5, 0), (.999, 0)]:
            for dwell, seconds in [(0, 8), (15, 15), (30, 20)]:
                adapter = Adapter()
                elapsed = await self.run_once(adapter, draw=lambda: draw, dwell=lambda: dwell)
                self.assertAlmostEqual(elapsed, seconds)
                self.assertEqual(adapter.likes, expected)
                self.assertEqual(adapter.advances, 1)

    async def test_already_liked_never_toggled(self):
        adapter = Adapter(liked=True)
        await self.run_once(adapter, draw=lambda: 0)
        self.assertEqual(adapter.likes, 0)

    async def test_durable_claim_precedes_click_and_restart_skips(self):
        ledger = set()
        async def claim(key, selected):
            self.assertEqual(first.likes, 0)
            if key in ledger: return False
            ledger.add(key)
            self.assertTrue(selected)
            return True
        first = Adapter(fail=True)
        with self.assertRaises(ReelsUnavailable):
            await run_parent_reels(first, okay, claim=claim, draw=lambda: 0, sleep=lambda _: asyncio.sleep(0))
        self.assertEqual(first.likes, 1)
        second = Adapter()
        async def recovered_claim(key, selected): return key not in ledger
        await self.run_once(second, claim=recovered_claim, draw=lambda: 0)
        self.assertEqual(second.likes, 0)

    async def test_failed_durable_claim_permits_no_click(self):
        adapter = Adapter()
        async def claim(*_): raise RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            await run_parent_reels(adapter, okay, claim=claim, draw=lambda: 0, sleep=lambda _: asyncio.sleep(0))
        self.assertEqual(adapter.likes, 0)

    async def test_unstable_video_gets_no_probability_decision(self):
        adapter = Adapter()
        count = 0
        async def current():
            nonlocal count
            count += 1
            return dict(key='/reel/'+str(count), source='video', liked=False)
        adapter.current = current
        with self.assertRaises(ReelsUnavailable):
            await run_parent_reels(adapter, okay, draw=lambda: self.fail('must not draw'), sleep=lambda _: asyncio.sleep(0))
        self.assertEqual(adapter.likes, 0)

    async def test_same_video_decision_is_not_repeated(self):
        decisions = set()
        first = Adapter()
        await self.run_once(first, decisions=decisions, draw=lambda: .9)
        second = Adapter()
        await self.run_once(second, decisions=decisions, draw=lambda: self.fail('duplicate draw'))
        self.assertEqual(second.likes, 0)


class ParentReelsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ExecutionManager.__new__(ExecutionManager)
        self.manager.service = SimpleNamespace(claim_parent_reel_decision=lambda *_: True)
        self.control = SimpleNamespace(profile_id=None, owner_user_id='o', task_id='t',
            pause_event=asyncio.Event(), stop_event=asyncio.Event())
        self.control.pause_event.set()
        self.worker = SimpleNamespace(parent_reels_adapter=lambda: object())
        self.child = asyncio.create_task(asyncio.Event().wait())
        self.intervention = asyncio.get_running_loop().create_future()
        self.entered = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def asyncTearDown(self):
        self.child.cancel()
        await asyncio.gather(self.child, return_exceptions=True)

    async def blocked(self, adapter, checkpoint, **kw):
        await checkpoint()
        self.entered.set()
        try: await asyncio.Event().wait()
        finally: self.cleaned.set()

    async def test_activity_cancelled_and_joined_on_each_boundary(self):
        self.child.cancel(); await asyncio.gather(self.child, return_exceptions=True)
        for boundary in ['pause', 'stop', 'child_done', 'failure', 'owner_cancel']:
            with self.subTest(boundary=boundary):
                self.control.pause_event.set(); self.control.stop_event.clear()
                self.entered.clear(); self.cleaned.clear()
                self.child = asyncio.create_task(asyncio.Event().wait())
                failed = False
                with patch('app.parent_reels.run_parent_reels', self.blocked):
                    activity = asyncio.create_task(self.manager._browse_parent_reels(
                        self.control, self.worker, [self.child], self.intervention, lambda: failed, 'target'))
                    await asyncio.wait_for(self.entered.wait(), 1)
                    if boundary == 'pause': self.control.pause_event.clear()
                    if boundary == 'stop': self.control.stop_event.set()
                    if boundary == 'child_done': self.child.cancel()
                    if boundary == 'failure': failed = True
                    if boundary == 'owner_cancel': activity.cancel()
                    await asyncio.wait_for(activity, 1)
                    self.assertTrue(self.cleaned.is_set())
                self.child.cancel(); await asyncio.gather(self.child, return_exceptions=True)

    async def test_account_guard_propagates_but_optional_error_does_not(self):
        for guarded in [False, True]:
            async def fail(*_, **kw):
                if guarded: raise WorkerExecutionError('verify account', reason='instagram_challenge')
                raise ReelsUnavailable('no video')
            intervention = asyncio.get_running_loop().create_future()
            with patch('app.parent_reels.run_parent_reels', fail):
                await self.manager._browse_parent_reels(self.control, self.worker, [self.child], intervention, lambda: False, 'target')
            self.assertEqual(intervention.done(), guarded)

    async def test_no_activity_after_children_finished_or_pause(self):
        for paused in [False, True]:
            if paused: self.control.pause_event.clear()
            else:
                self.child.cancel(); await asyncio.gather(self.child, return_exceptions=True)
            with patch('app.parent_reels.run_parent_reels', self.blocked):
                await self.manager._browse_parent_reels(self.control, self.worker, [self.child], self.intervention, lambda: False, 'target')
            self.assertFalse(self.entered.is_set())


class ParentReelsPipelineHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_final_mode_idle_slot_starts_and_joins(self):
        from test_parallel_relation_pipeline import (ParallelRelationPipelineTests,
            _PipelineState, _ScreeningChild, _RelationParent, _PipelineManager, _NoopBitBrowser)
        fixture = ParallelRelationPipelineTests()
        await fixture.asyncSetUp()
        try:
            for eligible in [False, True]:
                task, target, control = fixture._task_and_control(1)
                state = _PipelineState('reels_'+str(eligible))
                child = _ScreeningChild(state)
                release = asyncio.Event()
                original_screen = child.screen
                async def screen(username):
                    await original_screen(username)
                    await release.wait()
                child.screen = screen
                parent = _RelationParent(state, child=child)
                parent.collection_pipeline = 'r59-batch'
                parent.parent_reels_adapter = lambda: object()
                manager = _PipelineManager(fixture.service, _NoopBitBrowser())
                entered = asyncio.Event(); settled = asyncio.Event()
                async def browse(*args):
                    self.assertFalse(state.source_running)
                    checkpoint = fixture.service.get_checkpoint(control.owner_user_id, control.task_id, target['id'], 'followers')
                    self.assertTrue(checkpoint['cursor']['candidate_spool_natural_end'])
                    self.assertFalse(checkpoint['cursor'].get('pending_relation_usernames'))
                    entered.set(); release.set()
                    try: await asyncio.Event().wait()
                    finally: settled.set()
                manager._browse_parent_reels = browse
                run = asyncio.create_task(manager._execute_candidate_spooled_mode(
                    control, parent, target, 'followers', task['settings'], None,
                    parent_reels_eligible=eligible))
                if eligible:
                    await asyncio.wait_for(entered.wait(), 2)
                else:
                    await asyncio.wait_for(state.screen_started.wait(), 2)
                    release.set()
                result = await asyncio.wait_for(run, 2)
                self.assertEqual(result['pending'], 0)
                self.assertEqual(entered.is_set(), eligible)
                self.assertEqual(settled.is_set(), eligible)
        finally:
            await fixture.asyncTearDown()
