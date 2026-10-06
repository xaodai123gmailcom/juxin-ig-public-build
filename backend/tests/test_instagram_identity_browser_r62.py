"""Offline Chromium coverage for collapsed IG navigation and true zero counts.

Use IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER=1 for mandatory runtime verification.
Routes and cookies are synthetic; no live website/account or social action.
"""
import unittest
from unittest.mock import AsyncMock
from app.errors import ValidationError
from app.instagram_identity import OWN_LINK,OWN_METRICS,resolve_own_identity,wait_for_own_profile
from app.standalone_nurture import exact_count
import test_standalone_nurture_browser_r6 as fixtures

AVATAR='<aside style="width:72px"><a href="/real_owner/"><img width="28" height="28" alt="tyra bell 的头像" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E"></a></aside>'
ZERO_PROFILE='''<main><section><div><h2>real_owner</h2></div><p>tyra bell</p><div>
<span><span>0</span>帖子</span><span><span>0</span>粉丝</span><span><span>0</span>关注</span>
</div><a href="/accounts/edit/">编辑主页</a><a href="/archive/stories/">查看私密文件夹</a>
</section><section><h2>分享照片</h2><p>分享的照片会显示在你的主页中。</p></section></main>'''


class IdentityBrowserR62Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.StandaloneBrowserR6Tests.asyncSetUp

    async def test_collapsed_avatar_home_resolves_without_profile_aria_label(self):
        await self.page.set_content(AVATAR+'<main><a href="/seed_account/"><img width="28" height="28">Profile</a></main>')
        self.assertEqual('real_owner',await self.page.evaluate(OWN_LINK))
        self.assertEqual({'username':'real_owner','instagram_user_id':'123'},await resolve_own_identity(self.page,AsyncMock()))

    async def test_roleless_left_rail_requires_real_navigation_neighbors(self):
        rail=AVATAR.replace('<aside style="width:72px">','<div style="width:72px">').replace('</aside>','</div>')
        await self.page.set_content(rail)
        self.assertEqual('',await self.page.evaluate(OWN_LINK))
        await self.page.set_content('<a href="/">Home</a><a href="/reels/">Reels</a>'+rail)
        self.assertEqual('real_owner',await self.page.evaluate(OWN_LINK))

    async def test_zero_span_counters_and_own_edit_control_without_sidebar(self):
        self.profile_html=ZERO_PROFILE
        await self.page.goto('https://www.instagram.com/real_owner/')
        self.assertEqual('real_owner',await self.page.evaluate(OWN_LINK))
        raw=await wait_for_own_profile(self.page,'real_owner','123',AsyncMock())
        self.assertEqual([0,0,0],[exact_count(raw.get(key)) for key in ('posts_count','followers_count','following_count')])

    async def test_delayed_avatar_render_waits_without_selecting_feed_profile(self):
        await self.page.set_content('<main><a href="/seed_account/"><img width="28" height="28">Profile</a></main>')
        await self.page.evaluate('(html)=>setTimeout(()=>document.body.insertAdjacentHTML("afterbegin",html),150)',AVATAR)
        identity=await resolve_own_identity(self.page,AsyncMock(),timeout=2,poll=.01)
        self.assertEqual('real_owner',identity['username'])

    async def test_foreign_edit_link_and_feed_caption_never_become_owner_proof(self):
        self.profile_html=ZERO_PROFILE.replace('href="/accounts/edit/"','href="https://evil.example/accounts/edit/"')
        await self.page.goto('https://www.instagram.com/real_owner/')
        self.assertIsNone(await self.page.evaluate(OWN_METRICS,'real_owner'))
        with self.assertRaises(ValidationError):
            await wait_for_own_profile(self.page,'real_owner','123',AsyncMock(),timeout=.1,poll=.01)

    async def test_nurture_zero_account_prepares_without_like_and_reports_correct_stage(self):
        await self.page.set_content(AVATAR+'<main>Fixture feed</main>')
        self.profile_html=ZERO_PROFILE
        self.b.progress=AsyncMock()
        await self.n.prepare()
        snapshot=self.b.account_snapshot.await_args.args[0]
        self.assertEqual(('real_owner','123',0,0,0,'ok'),tuple(snapshot[key] for key in ('username','instagram_user_id','posts_count','followers_count','following_count','status')))
        self.b.nurture_action_begin.assert_not_awaited()
        self.assertIn('正在打开任务主页',[call.args[0] for call in self.b.progress.await_args_list])
        self.assertIn('正在核验当前登录账号',[call.args[0] for call in self.b.progress.await_args_list])
