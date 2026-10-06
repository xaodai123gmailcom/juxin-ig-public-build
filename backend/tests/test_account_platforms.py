"""Platform routing, session import privacy and task/management isolation."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.account_platforms import parse_cookies, belongs_to_platform, open_platform_window, PLATFORMS
from app.account_workspace import AccountWorkspace
from app.database import Database
from app.errors import ConflictError, ValidationError
from app.service import CoreService
from app.cloud_workspace import export_workspace, decode_workspace

class WhatsAppNativeOpeningTests(unittest.TestCase):
    def test_named_and_custom_whatsapp_both_open_without_playwright(self):
        native=SimpleNamespace(open_whatsapp=Mock(return_value={'page_loaded':True,'message':''}))
        provider=SimpleNamespace(native=native)
        for platform in ('whatsapp',{'id':'custom','url':'https://web.whatsapp.com/'}):
            with patch('app.playwright_worker.PlaywrightWorker',side_effect=AssertionError('must not create an automation worker')):
                result=asyncio.run(open_platform_window(provider,'native:fixture',platform))
            self.assertTrue(result['page_loaded']);self.assertFalse(result['login_verified'])
        self.assertEqual(2,native.open_whatsapp.call_count)

    def test_cookies_use_the_native_account_session_and_failures_do_not_fall_back(self):
        native=SimpleNamespace(open_whatsapp=Mock(return_value={'page_loaded':True,'message':''}))
        cookies=[{'name':'fixture','value':'local','domain':'.whatsapp.com'}]
        result=asyncio.run(open_platform_window(SimpleNamespace(native=native),'native:fixture','whatsapp',cookies))
        self.assertEqual(1,result['cookies_imported']);self.assertEqual(cookies,native.open_whatsapp.call_args.args[2])
        native.open_whatsapp.side_effect=ConflictError('native unavailable')
        with self.assertRaises(ConflictError):asyncio.run(open_platform_window(SimpleNamespace(native=native),'native:fixture','whatsapp'))


class CookieTests(unittest.TestCase):
    def test_json_export_normalizes_attributes_without_export_metadata(self):
        result=parse_cookies(json.dumps([{'name':'sessionid','value':'fixture','domain':'.instagram.com',
            'path':'/','httpOnly':True,'secure':True,'sameSite':'no_restriction',
            'expirationDate':time.time()+600,'storeId':'0','hostOnly':False}]),'instagram')
        self.assertEqual('None',result[0]['sameSite']);self.assertTrue(result[0]['httpOnly'])
        self.assertNotIn('storeId',result[0]);self.assertIn('expires',result[0])
    def test_netscape_and_header_formats(self):
        result=parse_cookies('# Netscape HTTP Cookie File\n#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t123','instagram')
        self.assertTrue(result[0]['httpOnly']);self.assertNotIn('expires',result[0])
        result=parse_cookies('Cookie: sessionid=encoded==; csrftoken=csrf-fixture','instagram')
        self.assertEqual('encoded==',result[0]['value']);self.assertEqual(2,len(result))
    def test_storage_state_import_only_takes_cookies(self):
        result=parse_cookies(json.dumps({'cookies':[{'name':'x','value':'fixture','url':'https://web.whatsapp.com/'}],
                                        'origins':[{'localStorage':[{'name':'never','value':'imported'}]}]}),'whatsapp')
        self.assertEqual('web.whatsapp.com',result[0]['domain']);self.assertNotIn('origins',result)
    def test_domains_expiry_duplicates_and_secrets_are_checked_before_import(self):
        invalid=[
            [{'name':'s','value':'private-fixture','domain':'.facebook.com'}],
            [{'name':'s','value':'private-fixture','domain':'instagram.com.evil.test'}],
            [{'name':'s','value':'private-fixture','url':'https://instagram.com@evil.test'}],
            [{'name':'s','value':'private-fixture','expires':1}],
            [{'name':'s','value':'private-fixture','secure':'false'}],
            [{'name':'s','value':'private-fixture'},{'name':'s','value':'different-fixture'}],
            [{'name':'s','value':'private-fixture','expires':'nan'}],
        ]
        for data in invalid:
            with self.subTest(data=data),self.assertRaises(ValidationError) as caught:
                parse_cookies(json.dumps(data),'instagram')
            self.assertNotIn('private-fixture',str(caught.exception))
        with self.assertRaises(ValidationError):parse_cookies('a='+'x'*524289,'instagram')
    def test_exporter_dot_domain_uses_only_selected_platform(self):
        rows=[{'name':name,'value':'fixture-only','domain':'.','path':'/',
               'hostOnly':False,'httpOnly':False,'session':False,'secure':False}
              for name in ('csrftoken','ds_user_id','sessionid')]
        result=parse_cookies('\ufeff'+json.dumps(rows),'instagram')
        self.assertEqual(3,len(result))
        self.assertTrue(all(c['domain']=='.instagram.com' for c in result))
        self.assertTrue(all(c['value']=='fixture-only' for c in result))
        result=parse_cookies(json.dumps([{'name':'s','value':'fixture','domain':'.','url':'https://www.instagram.com/'}]),'instagram')
        self.assertEqual('www.instagram.com',result[0]['domain'])

    def test_placeholder_does_not_accept_foreign_or_malformed_scope(self):
        for fields in ({'domain':'.','url':'https://evil.test/'},
                       {'domain':'.instagram.com','url':'https://facebook.com/'},
                       {'domain':'..instagram.com'}, {'domain':'sub..instagram.com'},
                       {'domain':False}, {'domain':'.facebook.com'}):
            with self.subTest(fields=fields),self.assertRaises(ValidationError):
                parse_cookies(json.dumps([{'name':'s','value':'private-fixture',**fields}]),'instagram')

    def test_identical_duplicates_merge_but_conflicts_remain_errors(self):
        result=parse_cookies('csrftoken=fixture-csrf;ds_user_id=123;sessionid=fixture-login;rur="quoted\\054123";csrftoken=fixture-csrf;ds_user_id=123;','instagram')
        self.assertEqual(['csrftoken','ds_user_id','sessionid','rur'],[c['name'] for c in result])
        self.assertEqual('"quoted\\054123"',result[-1]['value'])
        row={'name':'s','value':'same'}
        self.assertEqual(1,len(parse_cookies(json.dumps([row,row]),'instagram')))
        for other in ({'name':'s','value':'different'},dict(row,secure=False)):
            with self.assertRaises(ValidationError):parse_cookies(json.dumps([row,other]),'instagram')

    def test_cookie_errors_identify_item_and_field_without_secrets(self):
        base={'name':'s','value':'private-fixture'}
        for fields,expected in (({'expires':1},'第 2 项 Cookie 已过期'),
                                ({'secure':'false'},'第 2 项的 secure'),
                                ({'path':'broken'},'第 2 项的 path'),
                                ({'domain':'.facebook.com'},'第 2 项的域名')):
            with self.subTest(fields=fields),self.assertRaises(ValidationError) as caught:
                parse_cookies(json.dumps([{'name':'first','value':'fixture'},dict(base,**fields)]),'instagram')
            self.assertIn(expected,str(caught.exception));self.assertNotIn('private-fixture',str(caught.exception))
        with self.assertRaises(ValidationError) as caught:
            parse_cookies('[{"name": "secret-field", "value": "private-fixture",}]','instagram')
        self.assertIn('JSON 格式不正确',str(caught.exception))
        self.assertNotIn('private-fixture',str(caught.exception));self.assertNotIn('secret-field',str(caught.exception))

    def test_platform_origin_match_is_exact(self):
        self.assertTrue(belongs_to_platform('https://www.instagram.com/?hl=zh-cn','instagram'))
        for url in ('http://instagram.com/','https://instagram.com.evil.test/',
                    'https://evil.test/instagram.com','https://instagram.com:9999/'):
            self.assertFalse(belongs_to_platform(url,'instagram'))


class Browser:
    def __init__(self):self.opened=False;self.close_calls=[]
    def list_all_windows(self):return {'windows':[{'id':'w1','name':'one','is_open':self.opened}], 'stale':False}
    def close_profile(self,ident):self.close_calls.append(ident);return {'closed':True}


class PlatformManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=Database(self.root/'a.sqlite');self.db.initialize();self.service=CoreService(self.db)
        self.owner=self.service.register_user('platform-owner','correct horse battery staple')['id']
        self.provider=Browser();self.calls=[]
        def open_window(provider,profile,platform,cookies):
            self.calls.append((profile,platform,cookies))
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner,profile,operation_type='account',entity_id='competing')
            return {'opened':True,'login_verified':False,'cookies_imported':len(cookies or [])}
        self.workspace=AccountWorkspace(self.service,self.provider,opener=open_window)
    def tearDown(self):self.tmp.cleanup()
    def plan(self,platform='instagram',username=''):
        return self.workspace.save(self.owner,{'name':'平台窗口','platform':platform,'username':username,'profile_id':'w1'})['id']
    def test_platform_default_compatibility_and_non_ig_account_names(self):
        ident=self.plan('whatsapp','+66 123 456 789')
        row=self.workspace.snapshot(self.owner)['plans'][0]
        self.assertEqual('whatsapp',row['platform']);self.assertEqual('+66 123 456 789',row['username'])
        self.workspace.save(self.owner,{'id':ident,'revision':1,'name':'rename','profile_id':'w1'})
        self.assertEqual('whatsapp',self.workspace.snapshot(self.owner)['plans'][0]['platform'])
        with self.assertRaises(ValidationError):self.workspace.save(self.owner,{'name':'x','platform':'unknown'})
    def test_open_uses_platform_and_is_serialized_with_other_operations(self):
        ident=self.plan('whatsapp')
        result=self.workspace.command(self.owner,{'action':'open','id':ident})
        self.assertEqual(('w1','whatsapp',None),self.calls[0]);self.assertFalse(result['login_verified'])
    def test_non_ig_windows_are_not_usable_by_ig_tasks_even_when_archived(self):
        ident=self.plan('whatsapp')
        for operation in ('collection','action','monitor','studio'):
            with self.assertRaises(ValidationError):
                self.service.acquire_browser_lease(self.owner,'w1',operation_type=operation,entity_id='job')
        self.workspace.command(self.owner,{'action':'archive','id':ident,'revision':1})
        with self.assertRaises(ValidationError):
            self.service.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='job')
    def test_occupied_window_rejects_import_and_platform_change(self):
        ident=self.plan()
        token=self.service.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='job')
        try:
            with self.assertRaises(ConflictError):self.workspace.command(self.owner,{'action':'import_cookies','id':ident,'revision':1,'cookie_text':'sessionid=fixture'})
            with self.assertRaises(ConflictError):self.workspace.save(self.owner,{'id':ident,'revision':1,'name':'x','platform':'whatsapp','profile_id':'w1'})
        finally:self.service.release_browser_lease('w1',token)
        self.assertEqual([],self.calls)
    def test_open_window_must_be_closed_before_changing_platform(self):
        ident=self.plan();self.provider.opened=True
        with self.assertRaises(ConflictError):self.workspace.save(self.owner,{'id':ident,'revision':1,'name':'x','platform':'whatsapp','profile_id':'w1'})
        self.assertEqual('instagram',self.workspace.snapshot(self.owner)['plans'][0]['platform'])
    def test_cookie_import_does_not_persist_secrets_to_business_data_or_cloud(self):
        ident=self.plan();secret='not-a-real-session-fixture'
        result=self.workspace.command(self.owner,{'action':'import_cookies','id':ident,'revision':1,'cookie_text':'sessionid='+secret})
        self.assertEqual(1,result['cookies_imported']);self.assertFalse(result['login_verified'])
        self.assertEqual(secret,self.calls[0][2][0]['value'])
        self.assertNotIn(secret,json.dumps(self.workspace.snapshot(self.owner)))
        backup=decode_workspace(export_workspace(self.db,self.owner,self.root)[0])
        self.assertNotIn(secret,json.dumps(backup))
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
    def test_bad_cookie_or_stale_revision_never_opens_browser(self):
        ident=self.plan()
        with self.assertRaises(ValidationError):self.workspace.command(self.owner,{'action':'import_cookies','id':ident,'revision':1,'cookie_text':'broken'})
        with self.assertRaises(ConflictError):self.workspace.command(self.owner,{'action':'import_cookies','id':ident,'revision':0,'cookie_text':'sessionid=fixture'})
        self.assertEqual([],self.calls)


class Page:
    def __init__(self,url,events):self.url=url;self.events=events
    def is_closed(self):return False
    async def set_extra_http_headers(self,headers):self.events.append(('language',headers))
    async def goto(self,url,**kwargs):self.events.append(('goto',url));self.url=url
    async def evaluate(self,script):return 'Instagram'
    async def bring_to_front(self):self.events.append(('front',self.url))


class Worker:
    def __init__(self,provider):
        self.events=provider['events'];self.pages=provider['pages'];self._context=self
        self._worker_owned_page=self.pages[0] if provider.get('owned') else None
        self.fail=provider.get('cookie_failure',False)
    async def connect(self,profile):self.events.append(('connect',profile))
    @asynccontextmanager
    async def _destructive_action_lease(self):
        self.events.append(('lease','begin'))
        try:yield
        finally:self.events.append(('lease','end'))
    async def add_cookies(self,cookies):
        if self.fail:raise ValueError('private-fixture in driver exception')
        self.events.append(('cookies',len(cookies)))
    async def new_page(self):
        page=Page('about:blank',self.events);self.pages.append(page);return page
    async def disconnect(self):self.events.append(('disconnect','closes-tab' if self._worker_owned_page else 'preserves-tabs'))


class PlatformOpeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_precedes_navigation_and_manual_tab_survives_disconnect(self):
        events=[];provider={'events':events,'pages':[Page('about:blank',events)],'owned':True}
        result=await open_platform_window(provider,'owned-window','instagram',[{'name':'fixture'}],worker_factory=Worker)
        self.assertLess(events.index(('cookies',1)),events.index(('goto',PLATFORMS['instagram']['url'])))
        self.assertIn(('language',{'Accept-Language':'zh-CN,zh;q=0.9'}),events)
        self.assertEqual(('disconnect','preserves-tabs'),events[-1]);self.assertFalse(result['login_verified'])
    async def test_missing_instagram_session_is_explained_without_claiming_login(self):
        for cookies,missing in (([{'name':'csrftoken','value':'fixture'}],True),
                                ([{'name':'sessionid','value':''}],True),
                                ([{'name':'sessionid','value':'fixture-login'}],False)):
            events=[];provider={'events':events,'pages':[Page('about:blank',events)]}
            result=await open_platform_window(provider,'w','instagram',cookies,worker_factory=Worker)
            self.assertEqual(missing,'缺少 sessionid' in result.get('message',''))
            self.assertFalse(result['login_verified']);self.assertEqual(1,result['cookies_imported'])
            self.assertNotIn('fixture-login',result.get('message',''))

    async def test_repeat_open_preserves_existing_platform_chat_and_unrelated_tabs(self):
        events=[];provider={'events':events,'pages':[Page('https://evil.test/instagram.com',events),Page('https://www.instagram.com/direct/inbox/',events)]}
        await open_platform_window(provider,'w','instagram',worker_factory=Worker)
        self.assertFalse(any(e[0]=='goto' for e in events));self.assertEqual(2,len(provider['pages']))
        self.assertIn(('front','https://www.instagram.com/direct/inbox/'),events)
    async def test_missing_platform_tab_creates_one_without_replacing_other_site(self):
        events=[];provider={'events':events,'pages':[Page('https://example.test/',events)]}
        await open_platform_window(provider,'w','whatsapp',worker_factory=Worker)
        self.assertEqual('https://example.test/',provider['pages'][0].url)
        self.assertEqual(PLATFORMS['whatsapp']['url'],provider['pages'][1].url)
    async def test_driver_cookie_error_is_redacted_and_lease_is_released(self):
        events=[];provider={'events':events,'pages':[Page('about:blank',events)],'cookie_failure':True}
        with self.assertRaises(ValidationError) as caught:
            await open_platform_window(provider,'w','instagram',[{}],worker_factory=Worker)
        self.assertNotIn('private-fixture',str(caught.exception))
        self.assertIn(('lease','end'),events);self.assertEqual('disconnect',events[-1][0])


if __name__=='__main__':unittest.main()
