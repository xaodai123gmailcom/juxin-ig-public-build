"""Real-Chrome, entirely routed/offline parent Reels regression fixtures.

Windows release gate: IGAC_REQUIRE_PARENT_REELS_BROWSER=1.
IGAC_PARENT_REELS_FIXTURE_ARTIFACT_DIR optionally saves screenshot evidence.
No Instagram account, live media, or external network is involved.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.parent_reels import ParentReelsAdapter, ReelsUnavailable, run_parent_reels
from support.browser_fixture import browser_fixture_launch_options

FIXTURE = Path(__file__).parent / 'fixtures' / 'parent_reels_r98.html'


class ParentReelsBrowserR98Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        required = os.environ.get('IGAC_REQUIRE_PARENT_REELS_BROWSER') == '1'
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if required:
                self.fail('Required parent Reels browser fixture needs Playwright')
            self.skipTest('Playwright unavailable; Windows required-browser gate must run this fixture')
        self.runtime = await async_playwright().start()
        self.addAsyncCleanup(self.runtime.stop)
        candidates = [os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE'), self.runtime.chromium.executable_path]
        if os.name == 'nt':
            for base in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')):
                if base:
                    candidates.extend(str(Path(base) / suffix) for suffix in
                        ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'))
        else:
            candidates.extend(shutil.which(name) for name in ('google-chrome', 'chromium', 'chromium-browser'))
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            if required:
                self.fail('Required parent Reels Chrome missing; set IGAC_TEST_CHROMIUM_EXECUTABLE')
            self.skipTest('Chrome unavailable locally; Windows required-browser gate must run this fixture')
        try:
            self.browser = await self.runtime.chromium.launch(executable_path=executable,
                **browser_fixture_launch_options())
        except Exception as exc:
            if not required and 'socket() failed: Operation not permitted' in str(exc):
                self.skipTest('Local sandbox blocks Chrome sockets; required-browser gate remains mandatory')
            raise
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(viewport={'width': 1000, 'height': 650}, service_workers='block')
        self.requests = []
        self.fulfilled = []
        async def route(request):
            self.requests.append(request.request.url)
            if request.request.is_navigation_request() and request.request.url == 'https://www.instagram.com/reels/':
                self.fulfilled.append(request.request.url)
                await request.fulfill(status=200, content_type='text/html', body=FIXTURE.read_text(encoding='utf-8'))
            else:
                await request.abort()
        await self.context.route('**/*', route)
        self.page = await self.context.new_page()
        self.leases = 0
        @asynccontextmanager
        async def lease():
            self.leases += 1
            yield
        self.worker = SimpleNamespace(page=self.page, _guard=AsyncMock(), _destructive_action_lease=lease)
        self.adapter = ParentReelsAdapter(self.worker)
        self.checkpoint = AsyncMock()
        await self.page.goto('https://www.instagram.com/reels/')
        await self.page.wait_for_function("() => {const v=document.querySelector('video');return !v.paused && v.readyState>=2 && v.videoWidth>0}")

    def virtualize_media_time(self, clock):
        # Policy cases virtualize playback time together with wall time; DOM
        # identity and native click/key behavior remain real browser operations.
        current = self.adapter.current
        async def virtual_current():
            state = await current()
            if state is not None: state['time'] = clock()
            return state
        self.adapter.current = virtual_current

    async def stats(self):
        value = await self.page.evaluate('stats')
        self.assertEqual(0, value['unlikes'])
        self.assertEqual(0, value['comments'])
        self.assertEqual(0, value['follows'])
        self.assertTrue(all(url == 'https://www.instagram.com/reels/' for url in self.fulfilled))
        return value

    async def test_native_like_proof_identity_and_screenshot(self):
        current = await self.adapter.current()
        self.assertEqual('/reel/FIXTURE_A', current['key'])
        self.assertFalse(current['liked'])
        directory = os.environ.get('IGAC_PARENT_REELS_FIXTURE_ARTIFACT_DIR')
        if directory:
            destination = Path(directory);destination.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(destination / 'parent-reels-unliked-r98.png'))
        await self.adapter.like(current, self.checkpoint)
        self.assertTrue((await self.adapter.current())['liked'])
        self.assertEqual(1, self.leases)
        self.assertEqual([True], (await self.stats())['trusted'])
        directory = os.environ.get('IGAC_PARENT_REELS_FIXTURE_ARTIFACT_DIR')
        if directory:
            destination = Path(directory);destination.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(destination / 'parent-reels-liked-r98.png'))
            (destination / 'parent-reels-proof-r98.json').write_text(json.dumps({
                'browser_version': self.browser.version, 'synthetic_offline': True,
                'identity': current['key'], 'stats': await self.stats()}, indent=2), encoding='utf-8')

    async def test_already_liked_never_toggles(self):
        await self.page.evaluate('setLiked(true)')
        current = await self.adapter.current()
        self.assertTrue(current['liked'])
        await self.adapter.like(current, self.checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_unknown_heart_state_has_no_eligible_identity(self):
        await self.page.evaluate("setLiked('unknown')")
        current = await self.adapter.current()
        self.assertTrue(current is None or not current['can_like'])
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_permalink_and_playing_video_are_required(self):
        await self.page.locator('#permalink').evaluate("a=>a.href='/unrelated/'")
        self.assertIsNone(await self.adapter.current())
        await self.page.evaluate("showReel('FIXTURE_A');document.querySelector('video').pause()")
        self.assertIsNone(await self.adapter.current())

    async def test_identity_change_before_native_like_is_fenced(self):
        current = await self.adapter.current()
        await self.page.evaluate("showReel('REPLACEMENT')")
        await self.adapter.like(current, self.checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_native_pointer_overlay_does_not_click_wrong_target(self):
        current = await self.adapter.current()
        # Keep the original nav children in place: removing Follow from the
        # bottom-anchored grid changes its height and moves the heart, making
        # an overlay positioned from the old heart rectangle miss its target.
        # An independent fixed-position button cannot change the nav layout.
        await self.page.evaluate("""() => {
            const rect = heart.getBoundingClientRect();
            const overlay = document.createElement('button');
            overlay.id = 'wrong-target-overlay';
            overlay.setAttribute('aria-label', 'Follow');
            overlay.textContent = 'Follow';
            overlay.onclick = () => stats.follows++;
            Object.assign(overlay.style, {
                position:'fixed', left:rect.x+'px', top:rect.y+'px',
                width:rect.width+'px', height:rect.height+'px',
                boxSizing:'border-box', margin:'0', padding:'0',
                zIndex:'100', background:'#333', pointerEvents:'auto'
            });
            document.body.append(overlay);
        }""")
        async def assert_overlay_intercepts_heart():
            target = await self.page.evaluate("""() => {
                const rect = heart.getBoundingClientRect();
                return document.elementFromPoint(
                    rect.left + rect.width / 2, rect.top + rect.height / 2
                )?.id;
            }""")
            self.assertEqual('wrong-target-overlay', target,
                             'Fixture must actually intercept the current heart center')
        await assert_overlay_intercepts_heart()
        from playwright.async_api import TimeoutError as BrowserTimeout
        with self.assertRaises((BrowserTimeout, ReelsUnavailable)):
            await self.adapter.like(current, self.checkpoint)
        await assert_overlay_intercepts_heart()
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_ambiguous_native_like_is_not_retried_for_same_identity(self):
        await self.page.add_init_script('window.ambiguous=true')
        decisions = set()
        now = [0.0]
        self.virtualize_media_time(lambda:now[0])
        async def immediate(seconds):
            now[0] += seconds
        with self.assertRaises(ReelsUnavailable):
            await run_parent_reels(self.adapter, self.checkpoint, decisions=decisions, draw=lambda:0, sleep=immediate, clock=lambda:now[0])
        self.assertEqual({'/reel/FIXTURE_A'}, decisions)
        self.assertEqual(1, (await self.stats())['likes'])
        self.adapter.open = AsyncMock()
        await self.page.locator('video').evaluate('v=>v.play()')
        async def stop(*_): raise asyncio.CancelledError()
        self.adapter.advance = stop
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(self.adapter, self.checkpoint, decisions=decisions,
                                   draw=lambda:self.fail('Duplicate decision was redrawn'), sleep=immediate, clock=lambda:now[0])
        self.assertEqual(1, (await self.stats())['likes'])

    async def test_final_checkpoint_identity_and_liked_state_races_are_fenced(self):
        for mutation in ("showReel('REPLACEMENT')", 'setLiked(true)'):
            with self.subTest(mutation=mutation):
                await self.page.evaluate("showReel('FIXTURE_A',false)")
                current = await self.adapter.current()
                calls = 0
                async def checkpoint():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        await self.page.evaluate(mutation)
                await self.adapter.like(current, checkpoint)
                self.assertEqual(0, (await self.stats())['likes'])

    async def test_conflicting_like_and_unlike_labels_cannot_toggle(self):
        await self.page.evaluate("heart.setAttribute('aria-label','Unlike')")
        current = await self.adapter.current()
        # A conservative probe may reject contradictory labels or treat them
        # as already liked; neither may result in any native toggle.
        if current:
            self.assertTrue(current['liked'])
            await self.adapter.like(current, self.checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_pressed_true_with_stale_like_label_never_toggles(self):
        await self.page.locator('#heart').evaluate("button=>button.setAttribute('aria-pressed','true')")
        current = await self.adapter.current()
        if current:
            self.assertTrue(current['liked'])
            await self.adapter.like(current, self.checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])
        self.assertEqual([], (await self.stats())['trusted'])

    async def test_final_checkpoint_pressed_state_change_is_fenced(self):
        current = await self.adapter.current()
        self.assertFalse(current['liked'])
        calls = 0
        async def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 2:
                await self.page.locator('#heart').evaluate("button=>button.setAttribute('aria-pressed','true')")
        await self.adapter.like(current, checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])
        self.assertEqual([], (await self.stats())['trusted'])

    async def test_global_unrelated_like_is_not_a_reel_heart(self):
        await self.page.evaluate("""() => {setLiked('unknown');const b=document.createElement('button');b.setAttribute('aria-label','Like');b.textContent='Unrelated Like';b.onclick=()=>stats.follows++;document.body.append(b)}""")
        current = await self.adapter.current()
        self.assertTrue(current is None or not current['can_like'])
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_next_control_advances_real_dom_identity(self):
        current = await self.adapter.current()
        await self.adapter.advance(current['key'], self.checkpoint)
        self.assertEqual('/reel/FIXTURE_B', (await self.adapter.current())['key'])
        self.assertEqual(1, (await self.stats())['next'])

    async def test_next_final_checkpoint_identity_change_is_fenced(self):
        current = await self.adapter.current()
        calls = 0
        async def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 2:
                await self.page.evaluate("showReel('REPLACEMENT')")
        with self.assertRaises(ReelsUnavailable):
            await self.adapter.advance(current['key'], checkpoint)
        self.assertEqual(0, (await self.stats())['next'])

    async def test_heart_moved_outside_video_at_final_checkpoint_is_fenced(self):
        current = await self.adapter.current()
        calls = 0
        async def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 2:
                await self.page.evaluate('document.body.append(heart)')
        await self.adapter.like(current, checkpoint)
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_replayed_same_video_stops_after_one_8_second_dwell(self):
        # Use the real DOM/click adapter; virtualize only timer passage and the
        # source's next-video acknowledgement to replay an existing identity.
        decisions, draws, slept = set(), [], []
        self.virtualize_media_time(lambda:sum(slept))
        advances = 0
        original_advance = self.adapter.advance
        async def replay(key, checkpoint):
            nonlocal advances
            advances += 1
            if advances == 2:
                raise asyncio.CancelledError()
            await original_advance(key, checkpoint)
            await self.page.evaluate("showReel('FIXTURE_A',false)")
        self.adapter.advance = replay
        async def immediate(seconds):
            slept.append(seconds)
        def draw():
            draws.append(True)
            return 0.49
        with self.assertRaises(ReelsUnavailable):
            await run_parent_reels(self.adapter, self.checkpoint, decisions=decisions, draw=draw,
                                   dwell=lambda:0, sleep=immediate, clock=lambda:sum(slept))
        self.assertEqual(1, len(draws))
        self.assertEqual({'/reel/FIXTURE_A'}, decisions)
        self.assertEqual(1, (await self.stats())['likes'])
        self.assertAlmostEqual(8.15, sum(slept), places=5)

    async def test_probability_boundary_half_skips_like(self):
        draws, slept = [], []
        self.virtualize_media_time(lambda:sum(slept))
        async def immediate(seconds): slept.append(seconds)
        async def stop(*_): raise asyncio.CancelledError()
        self.adapter.advance = stop
        def draw():
            draws.append(True)
            return .5
        with self.assertRaises(asyncio.CancelledError):
            await run_parent_reels(self.adapter, self.checkpoint, draw=draw, dwell=lambda:30, sleep=immediate, clock=lambda:sum(slept))
        self.assertEqual([True], draws)
        self.assertAlmostEqual(20.15, sum(slept), places=5)
        self.assertEqual(0, (await self.stats())['likes'])

    async def test_down_chevron_control_advances_without_english_next_label(self):
        await self.page.locator('#next').evaluate("e=>e.setAttribute('aria-label','Down chevron')")
        await self.adapter.advance('/reel/FIXTURE_A', self.checkpoint)
        self.assertEqual('/reel/FIXTURE_B', (await self.adapter.current())['key'])
        self.assertEqual(1, (await self.stats())['next'])

    async def test_unlabelled_arrow_uses_one_trusted_keyboard_advance(self):
        await self.page.locator('#next').evaluate("e=>e.removeAttribute('aria-label')")
        await self.page.evaluate("""() => {window.nextKeys=[];document.addEventListener('keydown',e=>{
            if(e.key==='ArrowDown'){nextKeys.push(e.isTrusted);stats.next++;showReel('KEYBOARD_B')}})}""")
        await self.adapter.advance('/reel/FIXTURE_A', self.checkpoint)
        self.assertEqual('/reel/KEYBOARD_B', (await self.adapter.current())['key'])
        self.assertEqual([True], await self.page.evaluate('nextKeys'))
        self.assertEqual(1, (await self.stats())['next'])

    async def test_focused_comment_input_blocks_navigation(self):
        await self.page.evaluate("""() => {const input=document.createElement('input');document.body.append(input);input.focus()}""")
        with self.assertRaises(ReelsUnavailable):
            await self.adapter.advance('/reel/FIXTURE_A', self.checkpoint)
        self.assertEqual(0, (await self.stats())['next'])

    async def test_plural_reels_permalink_has_canonical_identity(self):
        await self.page.locator('#permalink').evaluate("a=>a.href='/reels/FIXTURE_A/'")
        current=await self.adapter.current()
        self.assertEqual('/reel/FIXTURE_A',current['key'])
        await self.adapter.like(current,self.checkpoint)
        self.assertEqual(1,(await self.stats())['likes'])

    async def test_stop_pauses_owned_media_but_not_a_new_document_owner(self):
        await self.adapter.open()
        await self.adapter.stop()
        self.assertTrue(await self.page.locator('video').evaluate('v=>v.paused'))
        await self.page.evaluate("""() => {document.documentElement.dataset.collectorReelsOwner='next-owner';document.querySelector('video').play()}""")
        await self.adapter.stop()
        self.assertFalse(await self.page.locator('video').evaluate('v=>v.paused'))
