import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_navigation import navigate_account, page_load_result
from app.account_platforms import open_platform_window, PLATFORMS
import test_account_platforms as platform_tests
from app.errors import ConflictError

class Page:
    def __init__(self, url='about:blank', status=200, text='Instagram', failure=False):
        self.url=url;self.status=status;self.text=text;self.failure=failure;self.visits=[];self.front=False
    def is_closed(self):return False
    async def evaluate(self, script):return self.text
    async def goto(self, url, **kwargs):
        self.visits.append(url)
        if self.failure:raise TimeoutError('network private details')
        self.url=url
        return SimpleNamespace(status=self.status)
    async def reload(self, **kwargs):return await self.goto(self.url, **kwargs)
    async def bring_to_front(self):self.front=True
    async def set_extra_http_headers(self, headers):pass

class Worker:
    def __init__(self, provider):
        self.provider=provider;self._context=self;self.pages=provider['pages'];self._worker_owned_page=None
    async def connect(self, profile):
        if not self.pages:
            self._worker_owned_page=await self.new_page()
    async def new_page(self):
        p=Page();self.pages.append(p);return p
    @asynccontextmanager
    async def _destructive_action_lease(self):
        self.provider['locked']=True
        try:yield
        finally:self.provider['locked']=False
    async def disconnect(self):
        self.provider['disconnected']=True
        if self._worker_owned_page:self.pages.remove(self._worker_owned_page)

class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_and_refresh_recover_blank_error_or_missing_page(self):
        for url in ('about:blank','chrome-error://chromewebdata/',None):
            for action in ('home','refresh'):
                with self.subTest(url=url,action=action):
                    state={'pages':[] if url is None else [Page(url)]}
                    result=await navigate_account(state,'window','instagram',action,worker_factory=Worker)
                    self.assertTrue(result['page_loaded']);self.assertEqual(1,len(state['pages']))
                    self.assertEqual(PLATFORMS['instagram']['url'],state['pages'][0].url)
                    self.assertTrue(state['disconnected']);self.assertFalse(state['locked'])
    async def test_unrelated_tabs_preserved_and_inbox_uses_requested_platform(self):
        p=Page('https://example.test/');state={'pages':[p]}
        await navigate_account(state,'w','instagram','inbox',worker_factory=Worker)
        self.assertEqual('https://example.test/',p.url)
        self.assertEqual('https://www.instagram.com/direct/inbox/',state['pages'][1].url)
    async def test_http_errors_proxy_error_body_and_timeout_are_not_success(self):
        for status,text,failure in [(403,'Denied',False),(429,'Rate limited',False),(200,'4xx Client Error',False),(200,'Instagram',True)]:
            with self.subTest(status=status,text=text,failure=failure):
                p=Page(PLATFORMS['instagram']['url'],status,text,failure);state={'pages':[p]}
                result=await navigate_account(state,'w','instagram','home',worker_factory=Worker)
                self.assertFalse(result['page_loaded']);self.assertTrue(result['message'])
                self.assertEqual(1,len(p.visits));self.assertFalse(state['locked']);self.assertTrue(state['disconnected'])
                self.assertNotIn('private details',str(result))
    async def test_existing_error_page_is_not_reported_as_successful_open(self):
        state={'pages':[Page(PLATFORMS['instagram']['url'],200,'4xx Client Error')]}
        with patch('app.instagram_home.read_account_posts') as stats:
            result=await open_platform_window(state,'w','instagram',worker_factory=Worker,inspect_instagram=True)
        self.assertTrue(result['opened']);self.assertFalse(result['page_loaded']);stats.assert_not_called()
    async def test_renderer_failure_is_reported(self):
        class Crashed(Page):
            async def evaluate(self,script):raise RuntimeError('renderer gone')
        self.assertFalse((await page_load_result(Crashed()))['page_loaded'])

# Reuse the actual database/lease fixture without inheriting all its test cases.
class NavigationLeaseTests(unittest.TestCase):
    setUp=platform_tests.PlatformManagementTests.setUp
    tearDown=platform_tests.PlatformManagementTests.tearDown
    plan=platform_tests.PlatformManagementTests.plan
    def test_failure_reports_reason_and_releases_only_own_management_lease(self):
        ident=self.plan()
        with patch('app.account_navigation.navigate_account',return_value={'page_loaded':False,'message':'HTTP 403'}) as nav:
            result=self.workspace.command(self.owner,{'id':ident,'action':'home'})
        self.assertFalse(result['page_loaded'])
        snapshot=self.workspace.snapshot(self.owner)
        self.assertEqual({},snapshot['locks']);self.assertIn('HTTP 403',snapshot['events'][0]['action'])
        token=self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='running')
        try:
            with patch('app.account_navigation.navigate_account') as nav:
                with self.assertRaises(ConflictError):self.workspace.command(self.owner,{'id':ident,'action':'home'})
                nav.assert_not_called()
            self.assertEqual('studio',self.workspace.snapshot(self.owner)['locks']['w1']['operation_type'])
        finally:self.service.release_browser_lease('w1',token)

if __name__=='__main__':unittest.main()
