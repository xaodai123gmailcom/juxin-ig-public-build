"""Real Chrome, fully routed/offline own-profile and standalone Reel safety.

Windows release gate: IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER=1.
Every URL is fulfilled from test fixtures or aborted. No live account or action.
"""
import os
import shutil
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.errors import ValidationError
from app.standalone_nurture import OWN_LINK, OWN_METRICS, REEL, StandaloneNurture, exact_count
from app.studio_worker import ResultUncertain, StudioBrowser
from support.browser_fixture import browser_fixture_launch_options

SIDEBAR='<aside><a href="/real_owner/" aria-label="Profile">Profile</a></aside>'
PROFILE=SIDEBAR+'''<main><header><h2>real_owner</h2><a href="/accounts/edit/">Edit profile</a>
<ul><li><span title="1,234">1.2K</span> followers</li><li>0 following</li><li>42 posts</li></ul></header>
<article><h2>last_seed</h2><p>9999 followers</p><a href="/wrong_person/">Profile</a></article></main>'''
BASE=Path(__file__).parent/'fixtures'/'parent_reels_r98.html'
REELS=BASE.read_text(encoding='utf-8').replace('<main>',SIDEBAR+'<main>').replace('</script>', '''
window.mediaIdentity='offline-canvas:A';
Object.defineProperty(video,'currentSrc',{get:()=>window.mediaIdentity});
</script>''')


class StandaloneBrowserR6Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        required=os.environ.get('IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER')=='1'
        try:from playwright.async_api import async_playwright
        except ImportError:
            if required:self.fail('Required standalone fixture needs Playwright')
            self.skipTest('Playwright unavailable; mandatory Windows fixture must run')
        self.runtime=await async_playwright().start();self.addAsyncCleanup(self.runtime.stop)
        candidates=[os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE'),self.runtime.chromium.executable_path]
        if os.name=='nt':
            for root in ('PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA'):
                if os.environ.get(root):candidates.extend(str(Path(os.environ[root])/name) for name in ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'))
        else:candidates.extend(shutil.which(name) for name in ('chromium','google-chrome','chromium-browser'))
        exe=next((p for p in candidates if p and Path(p).is_file()),None)
        if not exe:
            if required:self.fail('Required standalone fixture Chrome is missing')
            self.skipTest('Chrome unavailable; mandatory Windows fixture must run')
        try:self.chrome=await self.runtime.chromium.launch(executable_path=exe,**browser_fixture_launch_options())
        except Exception as exc:
            if not required and ('Operation not permitted' in str(exc) or 'Permission denied' in str(exc)):
                self.skipTest('Local sandbox blocks Chrome; mandatory Windows fixture must run')
            raise
        self.addAsyncCleanup(self.chrome.close)
        self.context=await self.chrome.new_context(viewport={'width':1100,'height':760},service_workers='block')
        self.requests=[];self.profile_html=PROFILE;self.reels_html=REELS
        async def route(r):
            url=r.request.url;self.requests.append(url)
            if url=='https://www.instagram.com/reels/':body=self.reels_html
            elif url=='https://www.instagram.com/real_owner/':body=self.profile_html
            elif url=='https://www.instagram.com/':body=SIDEBAR+'<main><h2>last_seed</h2><p>98,765 followers</p></main>'
            else:await r.abort();return
            await r.fulfill(status=200,content_type='text/html; charset=utf-8',body=body)
        await self.context.route('**/*',route)
        await self.context.add_cookies([{'name':'ds_user_id','value':'123','domain':'.instagram.com','path':'/','secure':True}])
        self.page=await self.context.new_page();await self.page.goto('https://www.instagram.com/')
        self.leases=0
        @asynccontextmanager
        async def lease():
            self.leases+=1
            yield
        self.worker=SimpleNamespace(page=self.page,_guard=AsyncMock(),open_account_home_page=AsyncMock(),_destructive_action_lease=lease)
        self.b=StudioBrowser(self.worker,AsyncMock(),AsyncMock());self.b.nurture_mode=True
        self.b.account_snapshot=AsyncMock();self.b.nurture_ready=AsyncMock();self.b.nurture_decisions={};self.b.nurture_actions={}
        self.b.nurture_observation=AsyncMock();self.b.nurture_decision=AsyncMock()
        async def begin(token,data):self.b.nurture_actions[token]=dict(data)
        async def confirmed(token,counts):self.b.nurture_actions[token]['state']='confirmed'
        self.b.nurture_action_begin=AsyncMock(side_effect=begin);self.b.nurture_action_confirmed=AsyncMock(side_effect=confirmed)
        self.n=StandaloneNurture(self.b)

    async def prepare(self):
        await self.n.prepare()
        await self.page.wait_for_function("() => {const v=document.querySelector('video');return !v.paused&&v.readyState>=2&&v.videoWidth>0}")

    async def assert_no_unlike_or_other_action(self):
        stats=await self.page.evaluate('stats')
        self.assertEqual((0,0,0),(stats['unlikes'],stats['comments'],stats['follows']))
        return stats

    async def test_own_profile_before_reels_exact_metrics_and_real_like(self):
        await self.prepare()
        snap=self.b.account_snapshot.await_args.args[0]
        self.assertEqual(('real_owner','123',1234,0,42,'ok'),tuple(snap[k] for k in ('username','instagram_user_id','followers_count','following_count','posts_count','status')))
        self.assertLess(self.requests.index('https://www.instagram.com/real_owner/'),self.requests.index('https://www.instagram.com/reels/'))
        current=await self.n.current();self.n.draw=Mock(return_value=.69);counts={}
        await self.n.decide(current,counts);await self.n.decide(current,counts)
        self.assertEqual(1,counts['like']);self.assertEqual(1,self.leases)
        self.n.draw.assert_called_once();self.b.nurture_action_confirmed.assert_awaited_once()
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(1,stats['likes']);self.assertTrue(all(stats['trusted']))
        directory=os.environ.get('IGAC_STANDALONE_NURTURE_FIXTURE_ARTIFACT_DIR')
        if directory:
            Path(directory).mkdir(parents=True,exist_ok=True)
            await self.page.screenshot(path=str(Path(directory)/'standalone-nurture-confirmed-offline.png'))

    async def test_abbreviated_unknown_count_and_fake_caption_do_not_become_zero_or_metrics(self):
        self.profile_html=PROFILE.replace('title="1,234"','').replace('<li>42 posts</li>','')
        await self.prepare()
        snap=self.b.account_snapshot.await_args.args[0]
        self.assertEqual('partial',snap['status']);self.assertIsNone(snap['followers_count']);self.assertIsNone(snap['posts_count']);self.assertEqual(0,snap['following_count'])
        self.assertEqual(0,exact_count('0'))

    async def test_profile_header_required_and_foreign_edit_link_rejected(self):
        self.profile_html=SIDEBAR+'<main><h2>real_owner</h2><p>999 followers</p><a href="https://evil.example/accounts/edit/">Settings</a><article>Edit profile</article></main>'
        with self.assertRaisesRegex(ValidationError,'主页'):await self.n.prepare()
        self.assertNotIn('https://www.instagram.com/reels/',self.requests)
        self.assertEqual('unavailable',self.b.account_snapshot.await_args.args[0]['status'])

    async def test_wrong_prior_actor_and_mid_run_cookie_change_stop_without_click(self):
        self.b.expected_nurture_identity={'username':'last_seed','instagram_user_id':'456'}
        with self.assertRaisesRegex(ValidationError,'历史账号'):await self.n.prepare()
        self.assertNotIn('https://www.instagram.com/reels/',self.requests)
        self.b.expected_nurture_identity={};await self.prepare()
        expected=await self.n.current()
        await self.context.add_cookies([{'name':'ds_user_id','value':'456','domain':'.instagram.com','path':'/','secure':True}])
        with self.assertRaisesRegex(ValidationError,'账号已变化'):await self.n.like(expected,{})
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(0,stats['likes'])

    async def test_negative_draw_already_liked_unknown_and_media_switch_skip(self):
        await self.prepare()
        current=await self.n.current();self.n.draw=Mock(return_value=.70)
        await self.n.decide(current,{})
        self.assertFalse(self.b.nurture_decisions[current['key']]['selected'])
        await self.page.evaluate("showReel('LIKED',true)")
        current=await self.n.current();await self.n.decide(current,{})
        self.assertEqual('liked',self.b.nurture_decisions[current['key']]['state'])
        await self.page.evaluate("showReel('UNKNOWN');heart.setAttribute('aria-pressed','true')")
        current=await self.n.current();self.assertEqual('unknown',current['state']);await self.n.decide(current,{})
        await self.page.evaluate("heart.removeAttribute('aria-pressed');showReel('SWITCH')")
        current=await self.n.current();await self.page.evaluate("mediaIdentity='offline-canvas:changed'")
        await self.n.like(current,{})
        self.b.nurture_action_begin.assert_not_awaited()
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(0,stats['likes'])

    async def test_title_unlike_race_and_ambiguous_click_never_replayed(self):
        await self.prepare();current=await self.n.current()
        original=self.n.current;calls=0
        async def race():
            nonlocal calls
            calls+=1
            value=await original()
            if calls==2:await self.page.evaluate("heart.title=' unlike '")
            return value
        self.n.current=race
        with self.assertRaises(Exception):await self.n.like(current,{})
        self.assertEqual('pending',self.b.nurture_actions['like:'+current['key']]['state'])
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(0,stats['likes'])
        self.n.current=original
        with self.assertRaises(ResultUncertain):await self.n.decide(current,{})
        await self.page.evaluate("heart.removeAttribute('title');showReel('AMBIGUOUS');window.ambiguous=true")
        current=await self.n.current()
        with self.assertRaises(ResultUncertain):await self.n.like(current,{})
        with self.assertRaises(ResultUncertain):await self.n.decide(current,{})
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(1,stats['likes'])

    async def test_plural_shortcode_redirect_and_links_prepare_like_advance_and_dedup(self):
        self.reels_html=REELS.replace('href="/reel/FIXTURE_A/"','href="/reels/FIXTURE_A/?igsh=fixture"').replace(
            "permalink.href='/reel/'+key+'/'", "permalink.href='/reels/'+key+'/?igsh=fixture'").replace(
            '</script>', "history.replaceState({},'', '/reels/FIXTURE_A/?igsh=fixture');</script>")
        await self.prepare()
        self.assertIn('/reels/FIXTURE_A/',self.page.url)
        self.b.nurture_ready.assert_awaited_once()
        current=await self.n.current();self.assertEqual('/reel/FIXTURE_A',current['key'])
        self.n.draw=Mock(return_value=.69);counts={}
        await self.n.decide(current,counts)
        await self.n.advance(current)
        self.assertEqual('/reel/FIXTURE_B',(await self.n.current())['key'])
        # A later singular alias of the same video is the same persisted target.
        await self.page.evaluate("showReel('FIXTURE_A',false);permalink.href='/reel/FIXTURE_A/'")
        await self.n.decide(await self.n.current(),counts)
        self.n.draw.assert_called_once()
        self.assertEqual(1,counts['like']);self.assertEqual(1,len(self.b.nurture_decisions))
        stats=await self.assert_no_unlike_or_other_action()
        self.assertEqual((1,1),(stats['likes'],stats['next']));self.assertTrue(all(stats['trusted']))

    async def test_plural_alias_does_not_authorize_other_video_or_foreign_permalink(self):
        await self.prepare()
        for href in ('https://evil.example/reels/FIXTURE_A/','/reels/FIXTURE_A/comments/'):
            await self.page.evaluate('(href)=>permalink.href=href',href)
            self.assertIsNone(await self.n.current())
        await self.page.evaluate("permalink.href='/reels/FIXTURE_A/';const a=document.createElement('a');a.href='/reel/DIFFERENT/';document.querySelector('article').append(a)")
        self.assertIsNone(await self.n.current())
        self.b.nurture_action_begin.assert_not_awaited()
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(0,stats['likes'])

    async def test_icon_only_next_uses_native_arrow_once_and_rejects_input_focus(self):
        await self.prepare()
        await self.page.evaluate("""() => {
          next.removeAttribute('aria-label');next.textContent='';
          window.keyEvents=[];
          document.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){
            keyEvents.push({trusted:e.isTrusted,key:e.key});showReel('KEYBOARD_B')}});
        }""")
        await self.n.advance(await self.n.current())
        self.assertEqual('/reel/KEYBOARD_B',(await self.n.current())['key'])
        self.assertEqual([{'trusted':True,'key':'ArrowDown'}],await self.page.evaluate('keyEvents'))
        await self.page.evaluate("const input=document.createElement('textarea');document.body.append(input);input.focus()")
        with self.assertRaisesRegex(ValidationError,'焦点'):await self.n.advance(await self.n.current())
        self.assertEqual(1,len(await self.page.evaluate('keyEvents')))
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual((0,0),(stats['likes'],stats['next']))

    async def test_frozen_media_time_does_not_count_despite_playing_flag(self):
        await self.prepare()
        await self.page.evaluate("Object.defineProperty(video,'currentTime',{get:()=>4,configurable:true})")
        self.b.nurture_watched=AsyncMock();start=time.monotonic()
        with self.assertRaisesRegex(ValidationError,'播放停滞'):await self.n.dwell(8)
        self.b.nurture_watched.assert_not_awaited()
        self.assertLess(time.monotonic()-start,15)
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual((0,0),(stats['likes'],stats['next']))

    async def test_native_playback_progress_records_eight_effective_seconds(self):
        await self.prepare()
        self.b.nurture_watched=AsyncMock();start=time.monotonic()
        actual=await self.n.dwell(8)
        total=sum(call.args[0] for call in self.b.nurture_watched.await_args_list)
        self.assertAlmostEqual(8,actual,places=4);self.assertAlmostEqual(8,total,places=4)
        self.assertGreaterEqual(time.monotonic()-start,8)
        stats=await self.assert_no_unlike_or_other_action();self.assertEqual(0,stats['likes'])
