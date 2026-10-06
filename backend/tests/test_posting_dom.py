"""Real Chromium fixture tests; never connect to an Instagram account."""
import asyncio,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.instagram_publisher import InstagramPublisher
from app.instagram_entry_dom import ENTRY_PROBE
from app.studio_worker import StudioBrowser
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError

HTML=r'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#101215;color:white;font-family:Arial}#sidebar{position:fixed;left:0;top:0;width:72px;height:100vh;display:flex;flex-direction:column;gap:22px;padding-top:24px;box-sizing:border-box}#sidebar a,#entry{display:block;margin-left:24px;width:24px;height:24px;color:white;cursor:pointer}svg{width:24px;height:24px}main{margin-left:260px;padding:50px}#story{margin-left:35px}#story svg{width:76px;height:76px}#composer{display:none;position:fixed;left:32%;top:14%;width:400px;background:#25272a;border-radius:12px;text-align:center;padding:16px}h2{font-size:18px}.content{height:240px;display:grid;place-items:center}button{padding:12px;background:#5965ef;color:white;border:0;cursor:pointer}img{width:80px;height:80px}.caption{background:#343434;min-height:70px}
</style></head><body><div id="sidebar">
<a href="/"><svg aria-label="首页"><circle cx="12" cy="12" r="8" fill="white"/></svg></a>
<a href="/reels/"><svg aria-label="Reels"><rect x="3" y="3" width="18" height="18" fill="white"/></svg></a>
<a href="/direct/inbox/"><svg aria-label="消息"><path d="M2 2L22 2L12 22Z" fill="white"/></svg></a>
<div id="entry" onclick="openComposer()"><svg viewBox="0 0 24 24"><path d="M12 4.5v15m7.5-7.5h-15" stroke="white" fill="none" stroke-width="2"/></svg></div>
</div><main><h1>示例主页</h1><button id="story" onclick="window.storyClicks++"><svg viewBox="0 0 24 24"><path d="M12 4.5v15m7.5-7.5h-15" stroke="white" fill="none"/></svg>新建</button><p>Feed post quotes: log in to Instagram</p></main>
<input hidden type="file" multiple id="files" onchange="window.uploaded=[...this.files].map(f=>f.name);crop()">
<div id="composer"></div>
<script>
window.storyClicks=0;window.shares=0;window.uploaded=[];
const composer=document.querySelector('#composer');
function openComposer(){composer.style.display='block';composer.innerHTML='<h2>创建新帖子</h2><div class="content"><p>把照片和视频拖放到这里</p><button onclick="document.querySelector(\'#files\').click()">从电脑中选择</button></div>'}
function crop(){composer.innerHTML='<h2>裁剪</h2><div class="content"><img src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E"></div><button aria-label="选择裁剪" onclick="ratios()"><svg aria-label="选择裁剪"></svg></button><div id="ratios"></div><button onclick="edit()">下一步</button>'}
function ratios(){document.querySelector('#ratios').innerHTML='<button onclick="window.originalChosen=true;this.parentElement.innerHTML=\'\'">原版</button><button onclick="window.wrongRatio=true">1:1</button>'} 
function edit(){composer.innerHTML='<h2>编辑</h2><div class="content"><canvas width="40" height="40"></canvas></div><button onclick="caption()">下一步</button>'}
function caption(){composer.innerHTML='<h2>创建新帖子</h2><div class="caption" role="textbox" contenteditable="true" aria-label="写下说明"></div><button onclick="share()">分享</button>'}
function share(){window.shares++;composer.innerHTML='<h2>帖子已分享</h2><p>Your post has been shared.</p>'}
</script></body></html>'''

class PostingDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p=await async_playwright().start()
        path=os.environ.get('IGAC_POSTING_TEST_BROWSER','')
        if not path and not Path(self.p.chromium.executable_path).is_file():
            await self.p.stop()
            if os.environ.get('IGAC_REQUIRE_POSTING_BROWSER')=='1':self.fail('Required Chromium runtime is missing')
            self.skipTest('Chromium is required for DOM fixtures; Windows build gate requires these tests')
        self.browser=await self.p.chromium.launch(**({'executable_path':path} if path else {'channel':'chromium'}),headless=True,args=['--no-sandbox'])
        self.context=await self.browser.new_context(viewport={'width':1264,'height':715})
        await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=HTML))
        self.page=await self.context.new_page();await self.page.goto('https://www.instagram.com/')
        self.tmp=tempfile.TemporaryDirectory();self.asset=Path(self.tmp.name)/'桌面素材.jpg';self.asset.write_bytes(b'fixture upload bytes')
        self.worker=PlaywrightWorker(object())
        self.worker.page=self.page;self.worker._context=self.context;self.worker._guard=AsyncMock()
        self.studio=StudioBrowser(self.worker,AsyncMock(),AsyncMock())
        self.pub=InstagramPublisher(self.studio);self.pub.finish=AsyncMock(return_value={});self.pub.step_timeout=2;self.pub.confirmation_timeout=2;self.pub.poll_seconds=.05

    async def asyncTearDown(self):
        if hasattr(self,'browser'):await self.browser.close();await self.p.stop();self.tmp.cleanup()

    async def test_unlabelled_sidebar_plus_ignores_story_and_uploads_via_file_chooser(self):
        result=await self.pub.publish([{'id':'one','path':str(self.asset),'media_type':'photo'}],'test caption','')
        self.assertEqual(1,result['published'])
        self.assertEqual(['桌面素材.jpg'],await self.worker.page.evaluate('window.uploaded'))
        self.assertEqual(0,await self.worker.page.evaluate('window.storyClicks'))
        self.assertEqual(1,await self.worker.page.evaluate('window.shares'))
        self.assertTrue(await self.worker.page.evaluate('window.originalChosen'))
        self.assertFalse(await self.worker.page.evaluate('Boolean(window.wrongRatio)'))
        self.assertEqual('sidebar_plus_shape',self.pub.entry_diagnostics['entry']['method'])

    async def test_crop_original_selected_before_continue(self):
        root=await self.pub.create()
        # Override only the crop fixture, preserving the real upload transition.
        await self.pub.page.evaluate("""() => {
          window.cropEvents=[];
          window.crop=()=>{composer.innerHTML='<h2>裁剪</h2><canvas width="40" height="40"></canvas><button aria-label="选择裁剪尺寸" id="ratio">比例</button><div id="ratios" hidden><button id="original">原版</button><span>1:1</span></div><button id="continue">继续</button>';
            document.querySelector('#ratio').onclick=()=>{cropEvents.push('menu');document.querySelector('#ratios').hidden=false};
            document.querySelector('#original').onclick=()=>{cropEvents.push('original');document.querySelector('#ratios').hidden=true};
            document.querySelector('#continue').onclick=()=>{cropEvents.push('next');edit()};
          };
        }""")
        await self.pub.upload(root,[{'path':str(self.asset)}])
        self.assertEqual(['menu','original','next'],await self.pub.page.evaluate('cropEvents'))
        self.assertEqual('original_selected',self.pub.entry_diagnostics['crop']['status'])
        self.assertEqual(0,await self.pub.page.evaluate('shares'))

    async def test_pure_text_plus_and_translated_label_are_detected(self):
        await self.page.locator('#entry').evaluate('(el)=>el.innerHTML="<span>＋</span>"')
        state=await self.pub.probe(self.page,mark=True)
        self.assertEqual(1,state['entry_count']);self.assertEqual('sidebar_plus_text',state['method'])
        await (await self.pub.find_create()).click()
        self.assertIsNotNone(await self.pub.upload_stage())

    async def test_crop_delayed_original_english_menu_uses_controls_without_red_marks(self):
        html=HTML.replace('选择裁剪','Select crop').replace('>原版</button>','>Original</button>')
        html=html.replace('onclick="ratios()"','onclick="setTimeout(ratios,150)"')
        html=html.replace('onclick="edit()">下一步','onclick="if(window.originalChosen)edit()">Continue')
        await self.load_fixture(html)
        result=await self.pub.publish([{'id':'one','path':str(self.asset),'media_type':'photo'}],'caption','')
        self.assertEqual(1,result['published'])
        self.assertTrue(await self.worker.page.evaluate('window.originalChosen'))
        self.assertEqual('selected',self.pub.entry_diagnostics['crop']['stage'])

    async def test_missing_original_stops_before_continuing_or_sharing(self):
        from app.errors import ValidationError
        await self.page.locator('#entry').click();await self.page.evaluate('crop()')
        await self.page.evaluate("ratios=()=>{document.querySelector('#ratios').innerHTML='<button>1:1</button>'}")
        root=await self.pub.dialog()
        original_wait=self.pub.wait_for
        async def short_wait(check,message,timeout=None):return await original_wait(check,message,timeout=.15)
        self.pub.wait_for=short_wait
        from app.instagram_crop import select_original
        with self.assertRaisesRegex(ValidationError,'原版'):
            await select_original(self.pub,root)
        self.assertTrue(await self.page.get_by_text('裁剪',exact=True).is_visible())
        self.assertEqual(0,await self.page.evaluate('window.shares'))

    async def test_original_click_without_selection_never_continues(self):
        from app.errors import ValidationError
        from app.instagram_crop import select_original
        await self.page.locator('#entry').click();await self.page.evaluate('crop();ratios()')
        await self.page.get_by_text('原版',exact=True).evaluate('el=>el.onclick=()=>{}')
        root=await self.pub.dialog();original_wait=self.pub.wait_for
        async def short_wait(check,message,timeout=None):return await original_wait(check,message,timeout=.15)
        self.pub.wait_for=short_wait
        with self.assertRaisesRegex(ValidationError,'菜单状态未确认'):
            await select_original(self.pub,root)
        self.assertTrue(await self.page.get_by_text('裁剪',exact=True).is_visible())
        self.assertEqual(0,await self.page.evaluate('window.shares'))

    async def test_new_home_tab_preserves_old_composer_and_account_context_then_closes(self):
        await self.context.add_cookies([{'name':'fixture_account','value':'selected-account','url':'https://www.instagram.com/'}])
        await self.page.goto('https://www.instagram.com/example_profile/')
        await self.page.locator('#entry').click()
        await self.page.evaluate('crop()')
        login=await self.context.new_page();await login.goto('https://www.instagram.com/accounts/login/')
        await login.set_content('<input type="password"><p>登录 Instagram</p>')
        self.worker.page=login;self.pub=InstagramPublisher(self.studio)
        root=await self.pub.create()
        fresh=self.worker.page
        self.assertIsNot(self.page,fresh);self.assertIsNot(login,fresh)
        self.assertIs(self.context,fresh.context)
        self.assertEqual('https://www.instagram.com/',fresh.url)
        self.assertIn('fixture_account=selected-account',await fresh.evaluate('document.cookie'))
        self.assertTrue(await root.get_by_role('button',name='从电脑中选择').is_visible())
        self.assertTrue(await self.page.get_by_text('裁剪',exact=True).is_visible())
        self.assertTrue(self.page.url.endswith('/example_profile/'))
        self.assertTrue(login.url.endswith('/accounts/login/'))
        self.assertEqual('new_tab_home',self.pub.entry_diagnostics['startup']['mode'])
        # A repeat replaces only the prior task tab; the two original tabs stay.
        await InstagramPublisher(self.studio).create()
        final=self.worker.page
        self.assertTrue(fresh.is_closed());self.assertEqual(3,len(self.context.pages))
        await self.worker.disconnect()
        self.assertTrue(final.is_closed());self.assertFalse(self.page.is_closed());self.assertFalse(login.is_closed())

    async def test_new_home_login_redirect_stops_before_upload(self):
        from app.errors import ValidationError
        await self.context.unroute('**/*')
        async def login_route(route):
            # Simulate the application's login router entirely in the fixture;
            # an HTTP redirect may bypass Playwright's request interception.
            await route.fulfill(content_type='text/html',body="<script>history.replaceState(null,'','/accounts/login/')</script><input type=password>")
        await self.context.route('**/*',login_route)
        with self.assertRaises(ValidationError):
            await self.pub.publish([{'id':'a','path':str(self.asset),'media_type':'photo'}],'caption','')
        self.assertTrue(self.worker.page.url.endswith('/accounts/login/'))
        self.assertEqual(0,await self.page.evaluate('window.shares'))
        self.assertEqual([],await self.page.evaluate('window.uploaded'))

    async def test_ambiguous_two_sidebar_pluses_fail_before_click(self):
        await self.page.locator('#entry').evaluate('(el)=>el.after(el.cloneNode(true))')
        from app.errors import ValidationError
        with self.assertRaises(ValidationError):await self.pub.find_create()
        self.assertEqual(0,await self.worker.page.evaluate('window.shares'))
        self.assertFalse(await self.page.locator('#composer').is_visible())

    async def test_post_text_does_not_override_signed_in_sidebar_but_login_form_does(self):
        self.worker._guard.side_effect=WorkerExecutionError('login text',reason='instagram_login_required')
        self.studio.posting_mode=True
        await self.studio.guard()
        await self.page.locator('main').evaluate('(el)=>el.innerHTML="<input type=password>"')
        from app.errors import ValidationError
        with self.assertRaises(ValidationError):await self.studio.guard()

    async def load_fixture(self, html):
        # A real navigation resets script globals; set_content retains the old
        # page's lexical bindings and would leave handlers on detached nodes.
        await self.context.unroute('**/*')
        await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
        await self.page.reload()

    async def test_simplified_continue_four_photos_ignores_carousel_next_arrow(self):
        html=HTML.replace('window.storyClicks=0;', 'window.carouselClicks=0;window.storyClicks=0;')
        arrow='<button aria-label="Next" onclick="window.carouselClicks++"><svg viewBox="0 0 24 24"><path d="M8 4l8 8-8 8"/></svg></button>'
        html=html.replace('<button onclick="edit()">下一步</button>',arrow+'<a href="#" onclick="edit();return false">继续</a>')
        html=html.replace('<button onclick="caption()">下一步</button>','<a href="#" onclick="caption();return false">继续</a>')
        await self.load_fixture(html)
        assets=[]
        for i in range(4):
            path=Path(self.tmp.name)/f'桌面素材-{i+1}.jpg';path.write_bytes(self.asset.read_bytes())
            assets.append({'id':str(i),'path':str(path),'media_type':'photo'})
        result=await self.pub.publish(assets,'四张照片的文案','')
        self.assertEqual(1,result['published'])
        self.assertEqual([Path(a['path']).name for a in assets],await self.worker.page.evaluate('window.uploaded'))
        self.assertEqual(0,await self.worker.page.evaluate('window.carouselClicks'))
        self.assertEqual(1,await self.worker.page.evaluate('window.shares'))

    async def test_continue_variants_survive_replaced_roleless_crop_and_edit_windows(self):
        for label in ('继续','繼續','Continue','Next','下一步'):
            with self.subTest(label=label):
                html=HTML.replace('const composer=', 'let composer=')
                swap='function swap(){const fresh=composer.cloneNode(false);fresh.removeAttribute("data-juxin-composer");composer.replaceWith(fresh);composer=fresh;} '
                html=html.replace('function crop(){',swap+'function crop(){swap();')
                html=html.replace('function edit(){','function edit(){swap();')
                html=html.replace('>下一步</button>','>'+label+'</button>')
                await self.load_fixture(html)
                publisher=InstagramPublisher(self.studio);publisher.finish=AsyncMock(return_value={});publisher.confirmation_timeout=2
                result=await publisher.publish([{'id':'a','path':str(self.asset),'media_type':'photo'}],'caption','')
                self.assertEqual(1,result['published'])
                self.assertEqual(1,await self.worker.page.evaluate('window.shares'))

    async def test_disabled_crop_continue_reports_stage_timeout_not_file_format(self):
        html=HTML.replace('<button onclick="edit()">下一步</button>','<button disabled onclick="edit()">继续</button>')
        await self.load_fixture(html)
        original=self.pub.wait_for
        async def short_wait(find,message,timeout=None):
            return await original(find,message,timeout=.4)
        self.pub.wait_for=short_wait
        from app.errors import ValidationError
        with self.assertRaises(ValidationError) as error:
            await self.pub.publish([{'id':'a','path':str(self.asset),'media_type':'photo'}],'caption','')
        self.assertIn('裁剪阶段等待超时',str(error.exception))
        self.assertNotIn('格式不支持',str(error.exception))
        self.assertEqual(0,await self.worker.page.evaluate('window.shares'))
        self.assertEqual(['桌面素材.jpg'],await self.worker.page.evaluate('window.uploaded'))


# Fixtures mirror the supplied Chinese screenshots, including a roleless Reels
# modal, the optional 帖子 menu and a location popup above its search field.
SCREENSHOT_SCRIPT = r'''
<script>
const report=(kind,data={})=>window.fixtureEvent({kind,...data});
report('home');
function replacePanel(html){const fresh=composer.cloneNode(false);fresh.removeAttribute('data-juxin-composer');composer.replaceWith(fresh);composer=fresh;composer.style.minHeight='260px';composer.innerHTML=html;}
function postMenu(){report('create');document.querySelector('#entry').insertAdjacentHTML('afterend','<a id="postmenu" href="#" onclick="report(\'post\');this.remove();openComposer();return false">帖子</a>');}
function crop(){
 report('upload',{files:window.uploaded});
 // Location popup cases reuse this flow with photos. Keep their crop controls
 // consistent with the uploaded media; video-only Reels need no ratio choice.
 const photo=window.uploaded.some(name=>/\.(jpe?g|png|webp|gif|heic|heif)$/i.test(name));
 const media=photo?'<img alt="照片预览" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E">':'<video></video>';
 const controls=photo?'<button aria-label="选择裁剪" onclick="photoRatios()"><svg aria-label="选择裁剪"></svg></button><div id="ratios"></div>':'';
 replacePanel('<h2>裁剪</h2><div class="content">'+media+'</div>'+controls+'<button onclick="report(\'continue-crop\');edit()">继续</button>');
}
function photoRatios(){
 report('crop-menu');
 document.querySelector('#ratios').innerHTML='<button onclick="window.originalChosen=true;report(\'crop-original\');this.parentElement.innerHTML=\'\'">原版</button><button onclick="window.wrongRatio=true;report(\'wrong-ratio\')">1:1</button>';
}
function edit(){replacePanel('<h2>编辑</h2><div class="content"><video></video><span>封面照片</span><button onclick="report(\'wrong-cover\')">从电脑中选择</button></div><button onclick="report(\'continue-edit\');caption()">继续</button>');}
function caption(){replacePanel('<h2>新 Reels</h2><textarea placeholder="添加配文..." style="width:90%;height:110px"></textarea><div id="location-section" style="position:relative"><input placeholder="添加地点" oninput="places()" style="width:90%;margin-top:100px"></div><button>添加合作作者</button><button>添加 AI 标签</button><button onclick="share()">分享</button>');}
function places(){document.querySelector('#places')?.remove();if(window.noResults)return;document.querySelector('#location-section').insertAdjacentHTML('afterbegin','<div id="places" style="position:absolute;bottom:30px;background:#141414;width:100%;z-index:4"><div role="button" style="height:40px;text-align:left" onclick="choosePlace(this)"><span>San Francisco Downtown</span></div><div role="button" style="height:40px;text-align:left" onclick="choosePlace(this)"><span>sf</span></div></div>');}
function choosePlace(el){report('location-click',{name:el.innerText});if(window.rejectSelection)return;document.querySelector('input[placeholder="添加地点"]').value=el.innerText.trim();window.place=el.innerText.trim();document.querySelector('#places').remove();}
function share(){report('share',{caption:composer.querySelector('textarea').value,place:window.place});replacePanel('<h2>正在分享</h2><div role="progressbar" style="height:230px">加载中</div>');if(window.outcome==='stuck')return;setTimeout(()=>{if(window.outcome==='failed'){replacePanel('<h2>分享失败</h2><p>请稍后重试</p>');return;}replacePanel('<h2>Reels 已分享</h2><p>你的 Reels 已分享。</p><button onclick="report(\'done\');composer.style.display=\'none\'">完成</button>');},350);}
</script>'''


class ScreenshotPostingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=PostingDOMTests.asyncSetUp
    asyncTearDown=PostingDOMTests.asyncTearDown
    load_fixture=PostingDOMTests.load_fixture

    async def setup_flow(self, *, menu=True, outcome='success', no_results=False, reject_selection=False, variant=''):
        import json
        self.events=[]
        await self.context.expose_binding('fixtureEvent',lambda source,event:self.events.append(event))
        settings=f'<script>window.outcome={json.dumps(outcome)};window.noResults={json.dumps(no_results)};window.rejectSelection={json.dumps(reject_selection)};'
        if menu:settings+="document.querySelector('#entry').onclick=postMenu;"
        settings+='</script>'
        html=HTML.replace('const composer=', 'let composer=').replace('</body>',SCREENSHOT_SCRIPT+settings+'</body>')
        if variant=='sibling':
            html=html.replace("document.querySelector('#location-section').insertAdjacentHTML('afterbegin'", "document.querySelector('#location-section').insertAdjacentHTML('afterend'")
            html=html.replace('bottom:30px;background:#141414', 'bottom:65px;background:#141414')
        if variant=='replacement':
            html=html.replace("document.querySelector('input[placeholder=\"添加地点\"]').value=el.innerText.trim();", "const old=document.querySelector('input[placeholder=\"添加地点\"]');const fresh=old.cloneNode(true);fresh.removeAttribute('placeholder');fresh.value=el.innerText.trim();old.replaceWith(fresh);")
        if variant=='address':
            html=html.replace('<span>San Francisco Downtown</span>', '<span>San Francisco Downtown</span><span>California, USA</span>')
            html=html.replace('el.innerText.trim()', "el.querySelector('span').innerText.trim()")
        if variant=='dismiss':
            html=html.replace('if(window.rejectSelection)return;', "if(window.rejectSelection){document.querySelector('#places').remove();return;}")
        if variant=='create_menu':
            html += r'''<script>
            function delayedPostMenu(){
                report('create');setTimeout(()=>{
                    const menu=document.createElement('div');menu.setAttribute('role','menu');
                    const post=document.createElement('a');post.href='#';post.style.cssText='width:180px;height:32px';
                    post.innerHTML='<svg aria-label="照片和视频"><rect width="10" height="10"/></svg><span>帖子</span>';
                    post.onclick=event=>{event.preventDefault();report('post');menu.remove();openComposer();};
                    menu.append(post);
                    for(const [label,kind] of [['直播视频','wrong-live'],['广告','wrong-ad']]){
                        const button=document.createElement('button');button.textContent=label;button.onclick=()=>report(kind);menu.append(button);
                    }
                    document.querySelector('#entry').after(menu);
                },150);
            }
            document.querySelector('#entry').onclick=delayedPostMenu;
            </script>'''
        await self.load_fixture(html)
        self.events.clear()
        self.pub.finish=InstagramPublisher.finish.__get__(self.pub)
        self.pub.step_timeout=.8;self.pub.confirmation_timeout=2
        async def confirmed(result):self.events.append({'kind':'confirmed','published':result['published']})
        self.studio.confirmed=confirmed
        self.studio.progress=AsyncMock()
        self.asset=Path(self.tmp.name)/'所选视频.mp4';self.asset.write_bytes(b'local video fixture')
        return [{'id':'video','path':str(self.asset),'media_type':'video'}]

    async def test_menu_reels_first_location_success_done_then_home(self):
        assets=await self.setup_flow()
        result=await self.pub.publish(assets,'示例文案 #生活','sf')
        self.assertEqual('San Francisco Downtown',result['location'])
        self.assertEqual('sf',result['location_query'])
        self.assertTrue(result['post_publish']['done_clicked']);self.assertTrue(result['post_publish']['home_refreshed'])
        kinds=[e['kind'] for e in self.events]
        self.assertEqual(1,kinds.count('post'));self.assertEqual(1,kinds.count('share'))
        self.assertEqual(1,kinds.count('continue-crop'));self.assertEqual(1,kinds.count('continue-edit'))
        self.assertNotIn('crop-menu',kinds);self.assertNotIn('crop-original',kinds)
        self.assertNotIn('wrong-cover',kinds)
        self.assertLess(kinds.index('confirmed'),kinds.index('done'));self.assertEqual('home',kinds[-1])
        share=next(e for e in self.events if e['kind']=='share')
        self.assertEqual('示例文案 #生活',share['caption']);self.assertEqual(result['location'],share['place'])
        self.assertEqual('create_post_upload',self.pub.entry_diagnostics['create_path'])
        self.assertIn('6/6 Instagram 正在分享，等待完成提示',[call.args[0] for call in self.studio.progress.await_args_list])

    async def test_delayed_menu_with_post_live_ad_and_icon_label(self):
        assets=await self.setup_flow(variant='create_menu')
        result=await self.pub.publish(assets,'文案','')
        self.assertEqual(1,result['published'])
        kinds=[e['kind'] for e in self.events]
        self.assertEqual(1,kinds.count('create'));self.assertEqual(1,kinds.count('post'))
        self.assertNotIn('wrong-live',kinds);self.assertNotIn('wrong-ad',kinds)
        self.assertEqual('create_post_upload',self.pub.entry_diagnostics['create_path'])

    async def test_direct_upload_without_post_menu_and_blank_location(self):
        assets=await self.setup_flow(menu=False)
        result=await self.pub.publish(assets,'文案','')
        self.assertEqual(1,result['published']);self.assertEqual('',result['location'])
        self.assertEqual('create_upload',self.pub.entry_diagnostics['create_path'])
        self.assertFalse(any(e['kind'] in {'post','location-click'} for e in self.events))

    async def test_sharing_spinner_only_never_finishes_or_refreshes(self):
        from app.studio_worker import ResultUncertain
        assets=await self.setup_flow(outcome='stuck');self.pub.confirmation_timeout=.5
        with self.assertRaises(ResultUncertain):await self.pub.publish(assets,'文案','')
        kinds=[e['kind'] for e in self.events]
        self.assertEqual(1,kinds.count('share'));self.assertEqual(1,kinds.count('home'))
        self.assertNotIn('confirmed',kinds);self.assertNotIn('done',kinds)
        self.assertTrue(self.asset.is_file())

    async def test_explicit_share_failure_records_message_without_retry(self):
        from app.studio_worker import PostingRejected
        assets=await self.setup_flow(outcome='failed')
        with self.assertRaisesRegex(PostingRejected,'分享失败'):await self.pub.publish(assets,'文案','')
        kinds=[e['kind'] for e in self.events]
        self.assertEqual(1,kinds.count('share'));self.assertNotIn('confirmed',kinds);self.assertNotIn('done',kinds)
        self.assertEqual('failed',self.pub.entry_diagnostics['submission']['state'])

    async def test_no_location_results_stops_before_share(self):
        from app.errors import ValidationError
        assets=await self.setup_flow(no_results=True)
        with self.assertRaisesRegex(ValidationError,'没有可选结果'):await self.pub.publish(assets,'文案','sf')
        self.assertFalse(any(e['kind']=='share' for e in self.events))

    async def test_clicked_but_unselected_location_stops_before_share(self):
        from app.errors import ValidationError
        assets=await self.setup_flow(reject_selection=True)
        with self.assertRaisesRegex(ValidationError,'未确认选中'):await self.pub.publish(assets,'文案','sf')
        self.assertFalse(any(e['kind']=='share' for e in self.events))
        self.assertEqual(1,sum(e['kind']=='location-click' for e in self.events))

    async def test_home_refresh_failure_retains_confirmed_success(self):
        assets=await self.setup_flow()
        async def confirmed(result):
            self.events.append({'kind':'confirmed'})
            self.worker.page.goto=AsyncMock(side_effect=RuntimeError('fixture network failure'))
        self.studio.confirmed=confirmed
        result=await self.pub.publish(assets,'文案','')
        self.assertEqual(1,result['published']);self.assertTrue(result['post_publish']['done_clicked'])
        self.assertFalse(result['post_publish']['home_refreshed'])
        self.assertIn('fixture network failure',result['post_publish']['note'])
        self.assertEqual(1,sum(e['kind']=='share' for e in self.events))

    async def test_caption_success_text_is_not_publication_confirmation(self):
        from app.studio_worker import ResultUncertain
        assets=await self.setup_flow();self.pub.confirmation_timeout=.5
        original=self.studio.before_effect
        async def hold_composer(message):
            await original(message)
            await self.worker.page.evaluate("""() => {window.share=()=>{const box=document.createElement('div');box.contentEditable='true';box.setAttribute('role','textbox');box.textContent='你的 Reels 已分享。';composer.append(box);report('share');};}""")
        self.studio.before_effect=hold_composer
        with self.assertRaises(ResultUncertain):await self.pub.publish(assets,'你的 Reels 已分享。','')
        self.assertFalse(any(e['kind']=='confirmed' for e in self.events))

    async def test_sibling_popup_is_found_without_clicking_other_composer_controls(self):
        assets=await self.setup_flow(variant='sibling')
        result=await self.pub.publish(assets,'文案','sf')
        self.assertEqual('San Francisco Downtown',result['location'])
        self.assertEqual(1,sum(e['kind']=='location-click' for e in self.events))

    async def test_selected_location_survives_replaced_input_without_placeholder(self):
        assets=await self.setup_flow(variant='replacement')
        result=await self.pub.publish(assets,'文案','sf')
        self.assertEqual('San Francisco Downtown',result['location'])
        self.assertTrue(self.pub.entry_diagnostics['location']['confirmed'])

    async def test_row_name_is_separate_from_inline_address(self):
        assets=await self.setup_flow(variant='address')
        result=await self.pub.publish(assets,'文案','sf')
        self.assertEqual('San Francisco Downtown',result['location'])

    async def test_dismissed_popup_and_caption_containing_place_are_not_selection(self):
        from app.errors import ValidationError
        assets=await self.setup_flow(variant='dismiss',reject_selection=True)
        with self.assertRaisesRegex(ValidationError,'未确认选中'):
            await self.pub.publish(assets,'San Francisco Downtown','sf')
        self.assertFalse(any(e['kind']=='share' for e in self.events))
        self.assertEqual('failed',self.pub.entry_diagnostics['location']['stage'])

    async def test_query_equal_to_result_and_dismissed_popup_are_not_selection(self):
        from app.errors import ValidationError
        assets=await self.setup_flow(variant='dismiss',reject_selection=True)
        with self.assertRaisesRegex(ValidationError,'未确认选中'):
            await self.pub.publish(assets,'文案','San Francisco Downtown')
        self.assertFalse(any(e['kind']=='share' for e in self.events))
