"""Real browser notices, own-profile count and permission isolation."""
import unittest
from unittest.mock import AsyncMock
from support import account_browser_fixture as fixtures
from app.instagram_home import prepare_instagram_home,read_account_posts,block_notification_prompt
from app.errors import ValidationError

class HomeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AccountDOMFixture.asyncSetUp
    asyncTearDown=fixtures.AccountDOMFixture.asyncTearDown
    load_fixture=fixtures.AccountDOMFixture.load_fixture

    async def test_notification_later_button_allows_account_home(self):
        notice='<div role="dialog" style="position:fixed;left:30%;top:100px;background:white;color:black;z-index:999"><h2>打开通知</h2><button onclick="window.badPermission=true">打开</button><button onclick="this.parentElement.remove()">以后再说</button></div>'
        await self.load_fixture(fixtures.HTML.replace('</body>',notice+'</body>'))
        await prepare_instagram_home(self.page)
        self.assertFalse(await self.page.evaluate('Boolean(window.badPermission)'))
        self.assertEqual(0,await self.page.get_by_text('以后再说',exact=True).count())

    async def test_restriction_and_unknown_confirmation_are_not_clicked(self):
        for title in ('操作受限，请稍后再试','确认你的身份','新的服务条款'):
            await self.page.set_content('<div role="dialog"><h2>'+title+'</h2><button onclick="window.clicked=true">确定</button></div>')
            with self.assertRaisesRegex(ValidationError,'需手动处理'):await prepare_instagram_home(self.page)
            self.assertFalse(await self.page.evaluate('Boolean(window.clicked)'))

    async def test_notification_permission_is_scoped_to_one_browser_context(self):
        other=await self.browser.new_context();p=await other.new_page()
        await p.route('**/*',lambda route:route.fulfill(content_type='text/html',body='fixture'))
        await p.goto('https://www.instagram.com/')
        await other.grant_permissions(['notifications'],origin='https://www.instagram.com')
        try:
            self.assertEqual('granted',await p.evaluate("async()=>(await navigator.permissions.query({name:'notifications'})).state"))
            self.assertTrue(await block_notification_prompt(self.page))
            self.assertEqual('denied',await self.page.evaluate("async()=>(await navigator.permissions.query({name:'notifications'})).state"))
            self.assertEqual('granted',await p.evaluate("async()=>(await navigator.permissions.query({name:'notifications'})).state"))
            self.assertEqual('denied',await self.page.evaluate('Notification.requestPermission()'))
        finally:await other.close()

    async def test_current_account_count_in_temporary_tab_and_zero_is_valid(self):
        for number in (0,1234):
            await self.context.unroute('**/*')
            async def route(r):
                if r.request.url.endswith('/own_account/'):
                    html=f'<main><header><h2>own_account</h2><ul><li><span>{number:,}</span> 帖子</li><li>999 粉丝</li></ul></header></main>'
                else:html='<nav><a href="/own_account/" aria-label="个人主页"><img alt="个人主页"></a></nav><main style="margin-left:300px"><a href="/other_account/"><img alt="头像"></a></main>'
                await r.fulfill(content_type='text/html; charset=utf-8',body=html)
            await self.context.route('**/*',route);await self.page.reload()
            before=list(self.context.pages)
            result=await read_account_posts(self.page)
            self.assertEqual(number,result['posts_count']);self.assertEqual('own_account',result['username'])
            self.assertEqual(before,self.context.pages);self.assertEqual('https://www.instagram.com/',self.page.url)

    async def test_other_account_in_feed_is_not_treated_as_owner(self):
        await self.page.set_content('<main><a href="/another_user/"><img alt="profile"></a></main>')
        # Move the feed link away from the sidebar; it must not become owner evidence.
        await self.page.locator('main').evaluate("el=>el.style.marginLeft='300px'")
        result=await read_account_posts(self.page)
        self.assertIsNone(result['posts_count']);self.assertEqual('unavailable',result['status'])
        self.assertIn('未识别',result['message'])

    async def test_roleless_notification_inside_fixed_overlay(self):
        await self.page.set_content('''<div style="position:fixed;inset:0;display:grid;place-items:center"><div style="width:560px;height:316px"><h2>打开通知</h2><p>照片得到关注、评论和点赞时，立即接收通知。</p><div onclick="this.parentElement.parentElement.remove()">以后再说</div></div></div>''')
        await prepare_instagram_home(self.page)
        self.assertEqual(0,await self.page.get_by_text('以后再说',exact=True).count())


    async def test_repeated_account_home_preserves_manual_tabs_cookies_and_draft(self):
        await self.context.add_cookies([{'name':'fixture_account','value':'selected-account','url':'https://www.instagram.com/'}])
        await self.page.goto('https://www.instagram.com/manual_account/')
        await self.page.set_content('<textarea aria-label="manual draft">operator-owned text</textarea>')
        login=await self.context.new_page();await login.goto('https://www.instagram.com/accounts/login/')
        await login.set_content('<input type="password"><p>登录 Instagram</p>')
        self.worker.page=login
        fresh=await self.worker.open_account_home_page()
        self.assertIsNot(self.page,fresh);self.assertIsNot(login,fresh)
        self.assertIs(self.context,fresh.context)
        self.assertEqual('https://www.instagram.com/',fresh.url)
        self.assertIn('fixture_account=selected-account',await fresh.evaluate('document.cookie'))
        self.assertEqual('operator-owned text',await self.page.get_by_role('textbox',name='manual draft').input_value())
        self.assertTrue(self.page.url.endswith('/manual_account/'))
        self.assertTrue(login.url.endswith('/accounts/login/'))
        final=await self.worker.open_account_home_page()
        self.assertIsNot(fresh,final);self.assertTrue(fresh.is_closed())
        self.assertEqual(3,len(self.context.pages))
        await self.worker.disconnect()
        self.assertTrue(final.is_closed());self.assertFalse(self.page.is_closed());self.assertFalse(login.is_closed())
        self.assertEqual('operator-owned text',await self.page.get_by_role('textbox',name='manual draft').input_value())

    async def test_account_home_login_route_stops_before_nurture_effect(self):
        await self.context.unroute('**/*')
        async def login_route(route):
            await route.fulfill(content_type='text/html',body="<script>history.replaceState(null,'','/accounts/login/')</script><input type=password>")
        await self.context.route('**/*',login_route)
        await self.worker.open_account_home_page()
        self.studio.nurture_mode=True
        with self.assertRaises(ValidationError):await self.studio.guard()
        self.assertTrue(self.worker.page.url.endswith('/accounts/login/'))
        self.studio.before_effect.assert_not_awaited()

    async def test_feed_login_text_does_not_override_signed_in_sidebar_but_password_form_does(self):
        from app.playwright_worker import WorkerExecutionError
        self.worker._guard.side_effect=WorkerExecutionError('login text',reason='instagram_login_required')
        self.studio.nurture_mode=True
        await self.studio.guard()
        await self.page.locator('main').evaluate('(el)=>el.innerHTML="<input type=password>"')
        with self.assertRaises(ValidationError):await self.studio.guard()
        self.studio.before_effect.assert_not_awaited()
