"""Controlled browser nurture actions and durable counts; no live accounts."""
import asyncio,json,unittest
from unittest.mock import AsyncMock,patch
from support import account_browser_fixture as browser_fixtures
import test_studio as db_fixtures
from app.instagram_nurture import NurtureInteractions
from app.instagram_home import prepare_instagram_home
from app.errors import ValidationError

def legacy_interaction_config(**overrides):
    """Raw helper fixture, independent of the product's fixed standalone policy."""
    config={key:0 for action in ('like','save','follow','comment')
            for key in (action+'_probability',action+'_limit')}
    return {**config,'comments':[],**overrides}


HTML='''<!doctype html><meta charset="utf-8"><nav><a href="/">首页</a></nav><main><article style="height:600px"><p>测试内容</p><button onclick="this.firstElementChild.setAttribute('aria-label','取消赞')"><svg aria-label="赞" width="30" height="30"><rect width="30" height="30"/></svg></button><button onclick="this.firstElementChild.setAttribute('aria-label','取消收藏')"><svg aria-label="收藏" width="30" height="30"><rect width="30" height="30"/></svg></button></article></main>'''

class NurtureDOMTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=browser_fixtures.AccountDOMFixture.asyncSetUp
    asyncTearDown=browser_fixtures.AccountDOMFixture.asyncTearDown
    load_fixture=browser_fixtures.AccountDOMFixture.load_fixture

    async def test_legacy_helper_new_home_dismisses_notice_confirms_actions_without_toggling_off(self):
        notice='<div role="dialog" style="position:fixed;top:50px;left:100px"><h2>打开通知</h2><button onclick="this.parentElement.remove()">以后再说</button></div>'
        await self.load_fixture(HTML+notice)
        self.studio.wait=AsyncMock();self.studio.nurture_confirmed=AsyncMock()
        cfg=legacy_interaction_config(**{'like_limit':2,'like_probability':100,'save_limit':2,'save_probability':100})
        step={'surface':'feed','url':'https://www.instagram.com/','seconds':1}
        await self.worker.open_account_home_page()
        await prepare_instagram_home(self.studio.page,self.studio.checkpoint)
        actions=NurtureInteractions(self.studio)
        counts=await actions.run(step,{},cfg)
        self.assertIsNot(self.page,self.studio.page)
        self.assertEqual({'like':1,'save':1},counts)
        self.assertEqual(2,self.studio.nurture_confirmed.await_count)
        await self.studio.page.evaluate('window.scrollTo(0,0)')
        counts=await actions.run(step,counts,cfg)
        self.assertEqual({'like':1,'save':1},counts)
        self.assertEqual(2,self.studio.before_effect.await_count)
        self.assertTrue(await self.page.get_by_text('打开通知',exact=True).is_visible())

    async def test_restriction_stops_before_any_nurture_effect(self):
        await self.load_fixture(HTML+'<div role="dialog"><h2>操作受限</h2><button>确定</button></div>')
        self.studio.wait=AsyncMock()
        with self.assertRaisesRegex(ValidationError,'手动处理'):
            await prepare_instagram_home(self.studio.page,self.studio.checkpoint)
            await NurtureInteractions(self.studio).run({'surface':'feed'},{},legacy_interaction_config(like_probability=100,like_limit=2))
        self.studio.before_effect.assert_not_awaited()

    async def test_legacy_helper_rowless_buttons_follow_and_contenteditable_comment(self):
        html='''<!doctype html><meta charset="utf-8"><main><section style="height:620px"><img alt="post" style="width:100px;height:100px"><a href="/p/fixture/">帖子</a>
        <button aria-label="Like" onclick="this.setAttribute('aria-label','Unlike')">心形</button>
        <button aria-label="Save" onclick="this.setAttribute('aria-label','Remove from saved')">书签</button>
        <button onclick="this.textContent='Following'">Follow</button>
        <button aria-label="Comment" onclick="document.querySelector('#composer').hidden=false">评论</button>
        <div id="composer" hidden><div id="field" style="min-height:24px" contenteditable="true" role="textbox" aria-label="Add a comment"></div>
        <button onclick="const p=document.createElement('p');p.textContent=document.querySelector('#field').textContent;document.querySelector('#messages').append(p);document.querySelector('#field').textContent=''">Post</button></div><div id="messages"></div></section></main>'''
        await self.load_fixture(html)
        self.studio.wait=AsyncMock();self.studio.nurture_observation=AsyncMock()
        cfg=legacy_interaction_config(**{'like_probability':100,'like_limit':2,'save_probability':100,'save_limit':2,'follow_probability':100,'follow_limit':2,'comment_probability':100,'comment_limit':2,'comments':['本地评论']})
        await self.worker.open_account_home_page()
        counts=await NurtureInteractions(self.studio).run({'surface':'feed'},{},cfg)
        self.assertEqual({'like':1,'save':1,'follow':1,'comment':1},counts)
        self.assertEqual('本地评论',await self.studio.page.locator('#messages').text_content())
        self.assertEqual(0,await self.page.locator('#messages p').count(),'manual account page stays untouched')

    async def test_unlike_in_comment_does_not_confirm_unclicked_post(self):
        from app.instagram_nurture import NurtureInteractions
        from app.studio_worker import ResultUncertain
        await self.load_fixture(HTML.replace("this.firstElementChild.setAttribute('aria-label','取消赞')", "window.clicked=(window.clicked||0)+1").replace('</article>','<a href="/p/unchanged-post/">帖子</a><button><svg aria-label="取消赞" width="20" height="20"></svg></button></article>'))
        self.studio.nurture_confirmed=AsyncMock()
        actions=NurtureInteractions(self.studio)
        root,key=await actions.scope()
        self.assertEqual('/p/unchanged-post/',key)
        counts={}
        with patch('app.instagram_nurture.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(ResultUncertain):await actions.toggle(root,'like',counts,key=key)
        self.assertEqual(1,await self.page.evaluate('window.clicked'))
        self.studio.before_effect.assert_awaited_once_with('like')
        self.studio.nurture_confirmed.assert_not_awaited()
        self.assertEqual({},counts)

    async def test_legacy_helper_profile_follow_request_is_confirmed_without_unfollowing(self):
        await self.load_fixture('<main><header><h2>target.user</h2><button onclick="this.textContent=\'Requested\'">Follow</button></header></main>')
        self.studio.wait=AsyncMock()
        cfg=legacy_interaction_config(**{'follow_probability':100,'follow_limit':2})
        step={'surface':'profile','url':'https://www.instagram.com/target.user/','seconds':1}
        await self.page.goto(step['url'])
        actions=NurtureInteractions(self.studio)
        counts=await actions.run(step,{},cfg)
        self.assertEqual(1,counts['follow'])
        counts=await actions.run(step,counts,cfg)
        self.assertEqual(1,counts['follow'])

class NurtureCountsTests(unittest.TestCase):
    setUp=db_fixtures.StudioTests.setUp
    tearDown=db_fixtures.StudioTests.tearDown
    start=db_fixtures.StudioTests.start
    execute=db_fixtures.StudioTests.execute

    def test_confirmed_action_survives_later_step_failure_and_daily_limit(self):
        async def run():
            ident=(await self.start('nurture',config={'rounds':1,'minutes':1,'like_limit':2}))['job_ids'][0]
            async def step(browser,*_):
                await browser.before_effect('like')
                await browser.nurture_confirmed({'like':1})
                raise RuntimeError('后续页面加载失败')
            with patch('app.studio.PlaywrightWorker',db_fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ident)
            row=self.m.get(self.owner,ident)
            self.assertEqual('failed',row['status']);self.assertEqual(0,row['cursor']);self.assertEqual(0,row['inflight'])
            self.assertEqual(1,json.loads(row['result_json'])['counts']['like'])
            self.assertEqual(1,self.m.daily_action_counts(self.owner,'w1')['like'])
        asyncio.run(run())

    def test_diagnostics_are_bounded_and_do_not_clear_pending_effect(self):
        async def run():
            ident=(await self.start('nurture',config={'rounds':1,'minutes':1,'like_limit':2}))['job_ids'][0]
            async def step(browser,*_):
                for _ in range(45):await browser.nurture_observation({'action':'like','status':'missing','detail':'未找到按钮'})
                await browser.before_effect('like')
                await browser.nurture_observation({'action':'like','status':'uncertain','detail':'结果未知'})
                raise RuntimeError('未确认')
            with patch('app.studio.PlaywrightWorker',db_fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ident)
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            self.assertEqual('needs_review',row['status']);self.assertEqual(1,row['inflight'])
            self.assertEqual(40,len(result['nurture_observations']));self.assertEqual(45,result['nurture_summary']['like:missing'])
            self.assertEqual(0,self.m.daily_action_counts(self.owner,'w1').get('like',0))
        asyncio.run(run())

class NurtureInteractionTests(unittest.IsolatedAsyncioTestCase):
    """Deterministic action/confirmation boundaries, without sending real actions."""
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from app.instagram_nurture import NurtureInteractions
        self.b=SimpleNamespace(page=SimpleNamespace(),guard=AsyncMock(),checkpoint=AsyncMock(),before_effect=AsyncMock(),nurture_confirmed=AsyncMock(),nurture_observation=AsyncMock())
        self.actions=NurtureInteractions(self.b);self.actions.stable=AsyncMock()
        self.root=object();self.target=AsyncMock();self.counts={}
        self.cfg=legacy_interaction_config(**{'like_probability':100,'like_limit':2,'follow_probability':100,'follow_limit':2,'comment_probability':100,'comment_limit':2,'comments':['本地测试评论']})

    async def test_probability_boundaries_and_daily_limit_never_click(self):
        from app.instagram_nurture import gate
        self.assertEqual('ready',gate('like',{},self.cfg,lambda _:99))
        self.assertEqual('limit',gate('like',{'like':2},self.cfg))
        self.assertEqual('disabled',gate('save',{},self.cfg))
        self.assertEqual('probability',gate('like',{},dict(self.cfg,like_probability=20),lambda _:20))
        self.assertEqual('ready',gate('like',{},dict(self.cfg,like_probability=20),lambda _:19))
        self.actions.scope=AsyncMock(side_effect=AssertionError('disabled actions must not scan or click'))
        await self.actions.run({'surface':'feed'},self.counts,legacy_interaction_config(**{}))
        self.b.before_effect.assert_not_awaited()

    async def test_confirmation_is_required_for_like_save_follow(self):
        for action in ('like','save','follow'):
            self.actions.control=AsyncMock(side_effect=[(self.target,{'state':'ready','disabled':False}),(None,{'state':'already','disabled':False})])
            await self.actions.toggle(self.root,action,self.counts,key="/p/local-fixture/")
            self.assertEqual(1,self.counts[action])
        self.assertEqual(3,self.b.nurture_confirmed.await_count)
        self.assertEqual(['like','save','follow'],[c.args[0] for c in self.b.before_effect.await_args_list])

    async def test_existing_missing_disabled_and_cancelled_controls_do_not_click(self):
        for state in [None,{'state':'already','disabled':False},{'state':'ready','disabled':True}]:
            self.actions.control=AsyncMock(return_value=(self.target,state))
            await self.actions.toggle(self.root,'like',self.counts,key='/p/local-fixture/')
        self.target.click.assert_not_awaited();self.b.before_effect.assert_not_awaited()
        self.actions.control=AsyncMock(return_value=(self.target,{'state':'ready','disabled':False}))
        self.b.guard.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):await self.actions.toggle(self.root,'follow',self.counts,key='/profile_fixture/')
        self.target.click.assert_not_awaited()
        self.assertEqual({},self.counts)

    async def test_uncertain_click_never_increments_or_repeats(self):
        from app.studio_worker import ResultUncertain
        self.actions.control=AsyncMock(return_value=(self.target,{'state':'ready','disabled':False}))
        with patch('app.instagram_nurture.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(ResultUncertain):await self.actions.toggle(self.root,'like',self.counts,key='/p/local-fixture/')
        self.target.click.assert_awaited_once();self.b.nurture_confirmed.assert_not_awaited()
        self.assertEqual({},self.counts)

    async def test_feed_follow_is_scoped_and_profile_follow_uses_header(self):
        self.actions.scope=AsyncMock(return_value=(self.root,'post-key'))
        self.actions.toggle=AsyncMock()
        self.actions.comment=AsyncMock()
        await self.actions.run({'surface':'feed'},self.counts,self.cfg)
        self.assertIn('follow',[c.args[1] for c in self.actions.toggle.await_args_list])
        self.actions.scope.reset_mock();self.actions.toggle.reset_mock()
        await self.actions.run({'surface':'profile'},self.counts,self.cfg)
        self.assertEqual(True,self.actions.scope.await_args_list[0].kwargs['profile'])
        self.assertEqual(1,len([c for c in self.actions.toggle.await_args_list if c.args[1]=='follow']))

    async def test_comment_preserves_draft_and_confirmed_comment_is_not_repeated(self):
        field=AsyncMock();field.evaluate.return_value='正在编辑的原稿'
        self.actions.field=AsyncMock(return_value=field)
        root=AsyncMock();root.evaluate.return_value=0
        await self.actions.comment(root,'same-post',self.counts,self.cfg)
        field.fill.assert_not_awaited();self.b.before_effect.assert_not_awaited()
        field.evaluate.return_value=''
        root.evaluate.side_effect=[0,1,1]
        self.actions.control=AsyncMock(return_value=(None,None))
        await self.actions.comment(root,'same-post',self.counts,self.cfg)
        self.assertEqual(1,self.counts['comment']);field.press.assert_awaited_once_with('Enter')
        await self.actions.comment(root,'same-post',self.counts,self.cfg)
        field.press.assert_awaited_once()

    async def test_comment_empty_composer_without_new_comment_is_not_success(self):
        from app.studio_worker import ResultUncertain
        field=AsyncMock();field.evaluate.return_value=''
        self.actions.field=AsyncMock(return_value=field);self.actions.control=AsyncMock(return_value=(None,None))
        root=AsyncMock();root.evaluate.return_value=0
        with patch('app.instagram_nurture.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(ResultUncertain):await self.actions.comment(root,'post',self.counts,self.cfg)
        self.assertEqual({},self.counts);field.press.assert_awaited_once()

    async def test_stories_report_unsupported_without_clicks(self):
        self.actions.scope=AsyncMock()
        await self.actions.run({'surface':'stories'},self.counts,self.cfg)
        self.actions.scope.assert_not_awaited();self.b.before_effect.assert_not_awaited()
        statuses=[c.args[0]['status'] for c in self.b.nurture_observation.await_args_list]
        self.assertEqual(3,statuses.count('unsupported'))

    async def test_recycled_post_identity_stops_before_effect(self):
        from app.instagram_nurture import NurtureInteractions
        self.actions.stable=NurtureInteractions.stable.__get__(self.actions)
        root=AsyncMock();root.count.return_value=1;root.evaluate.return_value='/p/different/'
        self.actions.control=AsyncMock(return_value=(self.target,{'state':'ready','disabled':False}))
        with self.assertRaisesRegex(ValidationError,'互动目标已切换'):
            await self.actions.toggle(root,'like',self.counts,'/p/original/')
        self.b.before_effect.assert_not_awaited();self.target.click.assert_not_awaited()
