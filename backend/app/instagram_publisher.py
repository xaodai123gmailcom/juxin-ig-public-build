"""Instagram web composer. A click is never treated as publication evidence."""
from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from .errors import ValidationError
from .instagram_entry_dom import ENTRY_PROBE, POST_MENU_PROBE
from .instagram_posting_dom import LOCATION_BEGIN, LOCATION_OPTIONS_PROBE, LOCATION_SELECTED_PROBE, RESULT_PANEL

UPLOAD_NAMES = r'^(Select from computer|Select from Computer|从电脑上选择|从电脑中选择|从计算机选择|從電腦選擇|從電腦中選擇)$'
CREATE_NAMES = r'^(Create|New post|Create new post|创建|建立|新建|创建新帖子|新建帖子|新增貼文|建立新貼文|新貼文)$'
COMPOSER_NAMES = r'^(Create new post|创建新帖子|新建帖子|建立新貼文|新增貼文|Crop|裁剪|裁切|Edit|编辑|編輯|New post|新建貼文|New reels?|新\s*Reels?)$'
FORWARD_NAMES = r'^(Next|Continue|下一步|继续|繼續)$'
SUCCESS_NAMES = r'^(Your (?:post|reels?) has been shared[.!]?|(?:Post|Reels?) shared[.!]?|(?:帖子|貼文|Reels?)\s*已(?:分享|发布)[！!。]?|(?:你|您)的\s*(?:帖子|貼文|Reels?)\s*已分享[！!。]?|已分享[你您]的\s*(?:帖子|貼文|连发|連續短片|Reels?)[！!。]?)$'
SHARING_NAMES = r'^(Sharing(?:\.\.\.)?|正在分享(?:…|\.\.\.)?|正在發布|正在发布)$'
FAILURE_NAMES = r'^(分享失败|发布失败|分享失敗|發佈失敗|无法分享(?:你的)?(?:帖子|Reels?)|无法发布(?:你的)?帖子|Your (?:post|reels?) (?:could not|couldn.t) be shared|(?:Post|Reels?) (?:failed to share|not shared)|Something went wrong(?:\. Please try again\.)?)[！!。.]*$'


# Discovery fallback for translated React headers whose visible Continue text
# has no button/link role. Only a click-capable control beside a composer stage
# title is eligible; feed text, carousel arrows and arbitrary page divs are not.
FORWARD_HEADER = r'''(root, marker) => {
 const visible=el=>el&&el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
 const names=/^(Next|Continue|下一步|继续|繼續)$/i;
 const stages=/^(Crop|裁剪|裁切|Edit|编辑|編輯)$/i;
 const text=el=>(el.innerText||'').trim();
 const titles=[...root.querySelectorAll('h1,h2,h3,div,span')].filter(el=>visible(el)&&stages.test(text(el))&&!Array.from(el.children).some(child=>stages.test(text(child))));
 const found=[];
 for(const el of root.querySelectorAll('button,a,div,span')){
  if(!visible(el)||!names.test(text(el)))continue;
  let target=el.closest('button,a,[role="button"],[role="link"],[tabindex]');
  if(!target){for(let p=el;p&&p!==root;p=p.parentElement){if(getComputedStyle(p).cursor==='pointer'){target=p;break;}}}
  if(!target||!root.contains(target)||!visible(target)||!names.test(text(target))||target.disabled||target.getAttribute('aria-disabled')==='true')continue;
  if(target.closest('[role="menu"],[role="listbox"]'))continue;
  const rect=target.getBoundingClientRect();
  if(!titles.some(title=>{const h=title.getBoundingClientRect();return Math.abs(rect.y+rect.height/2-h.y-h.height/2)<=40&&rect.x+rect.width/2>h.x+h.width/2;}))continue;
  if(!found.includes(target))found.push(target);
 }
 if(found.length!==1)return false;
 found[0].setAttribute('data-juxin-forward',marker);return true;
}'''

EDITING_READY = r'''root => {
 const visible=el=>el&&el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
 if(root.getAttribute('aria-busy')==='true')return false;
 return ![...root.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible);
}'''


class InstagramPublisher:
    poll_seconds = 0.25
    step_timeout = 30
    confirmation_timeout = 600

    def __init__(self, browser):
        self.browser = browser
        self.page = browser.page
        self.entry_marker = uuid.uuid4().hex
        self.entry_diagnostics = {}
        self.composer_root = None
        self.editing_stage = '裁剪 / 编辑'
        self.submitted = False

    async def pin_composer(self, root):
        if callable(getattr(root,'evaluate',None)):
            await root.evaluate('(el, marker) => el.setAttribute("data-juxin-composer", marker)',self.entry_marker)
            self.composer_root=self.page.locator(f'[data-juxin-composer="{self.entry_marker}"]')
            return self.composer_root
        return root

    async def probe(self, page, *, mark=False):
        try:
            return await page.evaluate(ENTRY_PROBE, {'marker': self.entry_marker if mark else ''})
        except Exception as exc:
            return {'entry_count':0,'nav_labels':[], 'probe_error':type(exc).__name__}

    async def record_diagnostics(self):
        callback = getattr(self.browser, 'diagnostic', None)
        if callback:
            await callback(self.entry_diagnostics)

    async def open_posting_page(self):
        await self.browser.checkpoint()
        self.entry_diagnostics['startup'] = {'mode': 'new_tab_home', 'home': 'https://www.instagram.com/'}
        await self.record_diagnostics()
        self.page = await self.browser.worker.open_posting_page()
        self.composer_root = None
        await self.browser.checkpoint()
        from .instagram_home import prepare_instagram_home
        handled=await prepare_instagram_home(self.page,self.browser.checkpoint)
        if handled:self.entry_diagnostics['home_notices']=handled
        await self.browser.guard()
        await self.read_account_snapshot()
        parsed = urlparse(self.page.url)
        if parsed.scheme != 'https' or parsed.hostname not in {'instagram.com', 'www.instagram.com'} or parsed.path not in {'', '/'}:
            raise ValidationError('新标签页未进入 Instagram 首页，尚未发布')
        self.entry_diagnostics['selected'] = await self.probe(self.page)
        await self.record_diagnostics()

    async def read_account_snapshot(self):
        callback=getattr(self.browser,'account_snapshot',None)
        if callback:
            from .instagram_home import read_account_posts
            await callback(await read_account_posts(self.page,self.browser.checkpoint))

    async def progress(self, text):
        callback = getattr(self.browser, 'progress', None)
        if callback:
            await callback(text)
        else:
            await self.browser.checkpoint()

    async def visible(self, locator, enabled=False):
        for i in range(await locator.count()):
            item = locator.nth(i)
            if await item.is_visible() and (not enabled or await item.is_enabled()):
                return item
        return None

    async def wait_for(self, find, message, timeout=None):
        remaining = self.step_timeout if timeout is None else timeout
        budget = remaining
        polls = 0
        last_poll_seconds = 0
        while remaining > 0:
            # Time spent intentionally paused must not consume a page deadline.
            await self.browser.checkpoint()
            started = asyncio.get_running_loop().time()
            polls += 1
            await self.browser.guard()
            found = await find()
            if found is not None:
                return found
            await asyncio.sleep(self.poll_seconds)
            last_poll_seconds = asyncio.get_running_loop().time() - started
            remaining -= last_poll_seconds
        self.entry_diagnostics['wait_timeout'] = {
            'message': message, 'budget_seconds': budget, 'polls': polls,
            'active_elapsed_seconds': round(budget - remaining, 3),
            'last_poll_seconds': round(last_poll_seconds, 3),
        }
        raise ValidationError(message)

    async def dialog(self):
        dialogs = self.page.locator('[role="dialog"], [aria-modal="true"]')
        for i in reversed(range(await dialogs.count())):
            if await dialogs.nth(i).is_visible():
                return dialogs.nth(i)
        if self.composer_root is not None and await self.composer_root.count() and await self.composer_root.is_visible():
            return self.composer_root
        # Some Instagram builds omit the modal role. Anchor to the composer
        # title and its controls, never the whole page or a profile's story '+'.
        patterns = COMPOSER_NAMES
        if self.submitted:
            patterns = '|'.join((patterns, SUCCESS_NAMES, SHARING_NAMES, FAILURE_NAMES))
        headings = self.page.get_by_text(re.compile(patterns, re.I), exact=True)
        for i in reversed(range(await headings.count())):
            heading = headings.nth(i)
            if not await heading.is_visible():
                continue
            ancestors = heading.locator('xpath=ancestor::*[not(self::body or self::html)]')
            for j in reversed(range(await ancestors.count())):
                candidate = ancestors.nth(j)
                if self.submitted and await candidate.evaluate(RESULT_PANEL):
                    return await self.pin_composer(candidate)
                if await candidate.locator('input[type="file"]').count() or await self.control(candidate, UPLOAD_NAMES) is not None:
                    return candidate
                has_content = await candidate.locator('canvas,video,img[src],textarea,[contenteditable="true"]').count()
                if has_content and (await self.forward(candidate) is not None or await self.control(candidate, r'^(Share|分享|发布|發佈)$') is not None):
                    return candidate
        return None

    async def control(self, root, pattern):
        names = re.compile(pattern, re.I)
        for role in ('button', 'link', 'menuitem'):
            candidate = await self.visible(root.get_by_role(role, name=names), enabled=True)
            if candidate is not None:
                return candidate
        return None

    async def forward(self, root):
        names=re.compile(FORWARD_NAMES,re.I)
        candidates=[]
        for role in ('button','link','menuitem'):
            controls=root.get_by_role(role,name=names)
            for i in range(await controls.count()):
                item=controls.nth(i)
                if not await item.is_visible() or not await item.is_enabled():continue
                # A carousel arrow can also be labelled "Next". The stage
                # control has visible text; an icon-only arrow changes photos.
                if not names.fullmatch((await item.inner_text()).strip()):continue
                rect=await item.bounding_box()
                if rect:candidates.append((rect['y'],rect['x'],item))
        if candidates:
            return min(candidates,key=lambda row:(row[0],row[1]))[2]
        if await root.evaluate(FORWARD_HEADER, self.entry_marker):
            return await self.visible(root.locator(f'[data-juxin-forward="{self.entry_marker}"]'), enabled=True)
        return None

    async def caption_box(self, root):
        for selector in (
            'textarea[placeholder*="caption" i],textarea[aria-label*="caption" i]',
            '[contenteditable="true"][aria-label*="caption" i]',
            '[contenteditable="true"][aria-label*="说明"],textarea[aria-label*="说明"]',
            '[contenteditable="true"][aria-label*="說明"],textarea[placeholder*="说明"]',
            'textarea[placeholder*="配文"],textarea[aria-label*="配文"]',
            '[contenteditable="true"][aria-label*="配文"],[contenteditable="true"][data-placeholder*="配文"]',
            '[contenteditable="true"][role="textbox"]',
        ):
            candidate = await self.visible(root.locator(selector))
            if candidate is not None:
                return candidate
        return None

    async def status_text(self, root, pattern):
        matches = root.get_by_text(re.compile(pattern, re.I))
        for i in range(await matches.count()):
            item = matches.nth(i)
            if await item.is_visible() and await item.evaluate('el => !el.closest("textarea,[contenteditable=true],[role=textbox]")'):
                return item
        return None

    async def find_create(self):
        state = await self.probe(self.page, mark=True)
        self.entry_diagnostics['entry'] = state
        if state.get('entry_count') == 1:
            return await self.visible(self.page.locator(f'[data-juxin-create-entry="{self.entry_marker}"]'),enabled=True)
        if state.get('entry_count',0) > 1:
            raise ValidationError('左侧检测到多个新建入口，已停止以免点错；请展开任务里的发帖诊断')
        names = re.compile(CREATE_NAMES, re.I)
        icons = self.page.locator('svg')
        for i in range(await icons.count()):
            icon = icons.nth(i)
            if not await icon.is_visible():
                continue
            label = await icon.get_attribute('aria-label') or ''
            title = icon.locator('title')
            title_text = (await title.first.text_content()) if await title.count() else ''
            if not names.fullmatch(label.strip()) and not names.fullmatch((title_text or '').strip()):
                continue
            parent = await self.visible(icon.locator('xpath=ancestor::*[self::a or self::button or @role="button" or @role="link"][1]'), enabled=True)
            return parent if parent is not None else icon
        candidate = await self.control(self.page, r'^(Create|New post|Create new post|创建|建立|创建新帖子|新建帖子|新增貼文|建立新貼文|新貼文)$')
        if candidate is not None:
            return candidate
        # Plain “新建” also labels story highlights on the profile: only accept
        # that text inside navigation when no labelled create icon exists.
        return await self.control(self.page.locator('nav, [role="navigation"], aside'), r'^新建$')

    async def post_menu_item(self):
        state = await self.page.evaluate(POST_MENU_PROBE, {'marker': self.entry_marker})
        self.entry_diagnostics['post_menu'] = state
        if state.get('count', 0) > 1:
            raise ValidationError('创建菜单中有多个帖子入口，尚未发布；请检查当前页面')
        if state.get('count') == 1:
            return await self.visible(self.page.locator(f'[data-juxin-post-menu="{self.entry_marker}"]'), enabled=True)
        return None

    async def upload_stage(self):
        root = await self.dialog()
        if root is not None:
            if await root.locator('input[type="file"]').count() and await self.caption_box(root) is None:
                return root
            if await self.control(root, UPLOAD_NAMES) is not None:
                return root
        return None

    async def create(self):
        await self.progress('1/6 正在新建标签页并加载 Instagram 首页')
        await self.open_posting_page()
        await self.browser.guard()
        root = await self.upload_stage()
        if root is not None:
            return root
        create = await self.find_create()
        if create is None:
            # Wait for the fresh homepage to render its navigation controls.
            async def entry_or_upload():
                root=await self.upload_stage()
                if root is not None:return ('upload',root)
                candidate=await self.find_create()
                return ('entry',candidate) if candidate is not None else None
            try:
                stage,create=await self.wait_for(entry_or_upload, '未识别新建入口，尚未发布；任务已保存发帖诊断，可展开查看')
            finally:
                await self.record_diagnostics()
            if stage=='upload': return create
        await self.record_diagnostics()
        await create.click(timeout=10000)

        self.entry_diagnostics['create_path'] = 'waiting_upload_or_post_menu'
        async def upload_or_post_menu():
            root = await self.upload_stage()
            if root is not None:
                return ('upload', root)
            menu = await self.post_menu_item()
            return ('menu', menu) if menu is not None else None

        try:
            stage, root = await self.wait_for(upload_or_post_menu, '点击创建后未进入上传素材窗口，尚未发布；请查看创建菜单诊断')
            if stage == 'menu':
                self.entry_diagnostics['create_path'] = 'post_menu_ready'
                await self.record_diagnostics()
                await self.browser.checkpoint()
                await self.browser.guard()
                await root.click(timeout=10000)
                self.entry_diagnostics['create_path'] = 'post_menu_clicked_waiting_upload'
                await self.record_diagnostics()
                # Menu discovery, the CDP click and the next render are separate
                # transitions. A successful click near the discovery deadline
                # must still get a bounded wait for its upload acknowledgement.
                # Never click the menu again merely because rendering is slow.
                root = await self.wait_for(self.upload_stage, '点击帖子后未进入上传素材窗口，尚未发布；请查看创建菜单诊断')
            self.entry_diagnostics['create_path'] = 'create_post_upload' if stage == 'menu' else 'create_upload'
            return root
        finally:
            await self.record_diagnostics()

    async def original_crop(self, root):
        from .instagram_crop import select_original
        await select_original(self, root)

    async def upload(self, root, assets):
        await self.progress('2/6 正在从桌面素材文件夹选择并上传')
        root=await self.pin_composer(root)
        paths = [a['path'] for a in assets]
        original_selected = False
        photos = any(Path(path).suffix.lower() not in {'.mp4','.mov','.m4v','.webm'} for path in paths)
        inputs = root.locator('input[type="file"]')
        if await inputs.count():
            await inputs.first.set_input_files(paths, timeout=20000)
        else:
            choose = await self.control(root, UPLOAD_NAMES)
            if choose is None:
                raise ValidationError('未找到素材上传按钮')
            async with self.page.expect_file_chooser(timeout=10000) as pending:
                await choose.click(timeout=10000)
            await (await pending.value).set_files(paths, timeout=20000)

        await self.progress('3/6 正在等待裁剪、编辑完成')
        previous_stage = None
        transitions = []
        # A successful click only starts a transition. Never click the same
        # stage again while React/upload processing still shows that stage.
        # Crop -> optional Edit -> Caption is bounded independently of polls.
        for step in range(4):
            async def stage():
                from playwright.async_api import Error as BrowserError
                try:
                    return await asyncio.wait_for(self.editing_state(), timeout=2)
                except (asyncio.TimeoutError, BrowserError):
                    # Read-only discovery may race a complete modal swap. The
                    # guard in wait_for remains outside this catch.
                    return None

            async def advanced():
                state = await stage()
                if state is None or state[0] == previous_stage:
                    return None
                return state

            timeout_message = '等待素材编辑步骤超时'
            try:
                kind, current, target = await self.wait_for(advanced, timeout_message, timeout=90)
            except ValidationError as exc:
                if str(exc) != timeout_message:
                    raise
                self.entry_diagnostics['editing'] = {
                    'stage': self.editing_stage, 'step': step + 1,
                    'waiting_after_click': previous_stage, 'transitions': transitions,
                }
                await self.record_diagnostics()
                detail = '已点击继续，但未确认进入下一阶段' if previous_stage else '未找到可用的“继续 / 下一步”或文案输入框'
                raise ValidationError(f'{self.editing_stage}阶段等待超时，{detail}；尚未发布') from None
            self.entry_diagnostics['editing'] = {'stage': self.editing_stage, 'step': step + 1, 'transitions': transitions}
            await self.record_diagnostics()
            if kind == 'caption':
                self.entry_diagnostics['editing']['stage'] = '文案'
                return current, target
            if kind in transitions:
                raise ValidationError('素材编辑步骤返回了已完成阶段，已停止以免重复点击；尚未发布')
            if photos and kind == 'crop' and not original_selected:
                await self.original_crop(current)
                original_selected = True
                # Ratio selection may re-render the entire panel. Reacquire
                # the same stage and its current enabled Continue control.
                async def crop_ready():
                    state = await stage()
                    return state if state is not None and state[0] == kind else None
                _, current, target = await self.wait_for(crop_ready, '选择原版后裁剪页未就绪，尚未发布', timeout=90)
            await self.progress(f'3/6 正在继续{self.editing_stage}步骤')
            await self.browser.guard()
            transitions.append(kind)
            self.entry_diagnostics['editing']['waiting_after_click'] = kind
            await self.record_diagnostics()
            await target.click(timeout=10000)
            previous_stage = kind
        raise ValidationError('素材编辑步骤超出预期，尚未发布')

    async def editing_state(self):
        """Observe one current ready stage without clicking controls while polling."""
        current = await self.dialog()
        if current is None:
            return None
        current = await self.pin_composer(current)
        box = await self.caption_box(current)
        if box is not None:
            return ('caption', current, box)
        heading = await self.visible(current.get_by_text(re.compile(r'^(Crop|裁剪|裁切|Edit|编辑|編輯)$', re.I), exact=True))
        if heading is None:
            return None
        self.editing_stage = (await heading.inner_text()).strip()
        kind = 'crop' if re.fullmatch(r'Crop|裁剪|裁切', self.editing_stage, re.I) else 'edit'
        if not await current.evaluate(EDITING_READY):
            return None
        target = await self.forward(current)
        return (kind, current, target) if target is not None else None

    async def location(self, root, name):
        field = await self.visible(root.get_by_placeholder(re.compile(
            r'Add location|添加地点|添加位置|新增地點|新增位置', re.I)))
        if field is None:
            field = await self.visible(root.get_by_role('textbox', name=re.compile(
                r'Add location|添加地点|添加位置|新增地點|新增位置', re.I)))
        if field is None:
            raise ValidationError('未找到定位输入框，尚未发布')
        location_state={'query': name, 'policy': 'first_visible_result', 'stage': 'searching', 'confirmed': False}
        self.entry_diagnostics['location']=location_state
        await self.progress(f'4/6 正在搜索地点「{name}」')
        await self.record_diagnostics()
        await field.evaluate(LOCATION_BEGIN, self.entry_marker)
        await field.fill(name, timeout=10000)

        async def first_place():
            if not await field.count():return None
            state = await field.evaluate(LOCATION_OPTIONS_PROBE, self.entry_marker)
            if not state:return None
            option = self.page.locator(f'[data-juxin-location-option="{self.entry_marker}"]')
            if not await option.is_visible():return None
            return option, state

        try:
            option, state = await self.wait_for(first_place, f'地点搜索「{name}」没有可选结果，尚未发布')
            place=state['name']
            location_state.update(selected=place,stage='selecting',method=state['method'],visible_results=state['count'])
            await self.progress(f'4/6 正在选择首条地点「{place}」')
            await self.record_diagnostics()
            await option.click(timeout=4000,position=state['point'])

            async def selected():
                return await root.evaluate(LOCATION_SELECTED_PROBE, {'marker':self.entry_marker,'place':place,'query':name})

            evidence=await self.wait_for(selected, '地点已点击，但未确认选中，尚未发布')
            location_state.update(confirmed=True,stage='confirmed',evidence=evidence)
            await self.record_diagnostics()
            return place
        except Exception as exc:
            location_state.update(stage='failed',message=str(exc)[:240])
            await self.record_diagnostics()
            raise

    async def finish(self, confirmed):
        """Run after success has been saved; cleanup trouble cannot unpublish it."""
        outcome = {'done_clicked': False, 'home_refreshed': False}
        try:
            await self.progress('6/6 分享已确认，正在完成并刷新首页')
            await self.browser.guard()
            done = await self.control(confirmed, r'^(Done|完成)$')
            if done is not None:
                await done.click(timeout=10000)
                outcome['done_clicked'] = True
            else:
                outcome['note'] = '已发布；未找到完成按钮'
            await self.browser.checkpoint()
            await self.page.goto('https://www.instagram.com/', wait_until='domcontentloaded', timeout=45000)
            await self.browser.guard()
            if urlparse(self.page.url).path not in {'', '/'}:
                raise ValidationError('页面未返回首页')
            outcome['home_refreshed'] = True
            await self.read_account_snapshot()
        except Exception as exc:
            outcome['note'] = '已发布；完成或刷新首页未完成：' + str(exc)[:180]
        return outcome

    async def publish(self, assets, caption, location):
        from .studio_worker import ResultUncertain
        if not assets or len(assets) > 10:
            raise ValidationError('请选择 1–10 张图片，或一个 MP4 视频')
        if any(not Path(a['path']).is_file() for a in assets):
            raise ValidationError('素材文件不存在，请重新导入')
        if len(assets)>1 and any(a['media_type']=='video' for a in assets):
            raise ValidationError('视频每次只能使用一个素材')
        if len(caption)>2200:
            raise ValidationError('文案与标签超过 2200 字符')
        root = await self.create()
        root, box = await self.upload(root, assets)
        await self.progress('4/6 正在填写文案与定位')
        await box.fill(caption, timeout=10000)
        actual = await box.evaluate('(el)=>el.value===undefined?el.innerText:el.value')
        if actual.replace('\r\n','\n').strip() != caption.replace('\r\n','\n').strip():
            raise ValidationError('文案回读与待发内容不一致，尚未发布')
        selected_location = await self.location(root, location) if location else ''

        async def share_button():
            return await self.control(root, r'^(Share|分享|发布|發佈)$')

        share = await self.wait_for(share_button, '发布按钮尚未就绪，尚未提交')
        await self.browser.guard()
        # The database fence must be durable before the only publishing click.
        await self.browser.before_effect('5/6 正在提交发帖，请勿重复操作')
        self.submitted = True
        await share.click(timeout=10000)
        await self.progress('6/6 已提交，正在核验发布结果')

        async def read_confirmation():
            current=await self.dialog()
            if current is None:return None
            for state,pattern in (('failed',FAILURE_NAMES),('confirmed',SUCCESS_NAMES),('sharing',SHARING_NAMES)):
                match=await self.status_text(current,pattern)
                if match is not None:
                    return state,current,((await match.inner_text()).strip()[:240] if state=='failed' else '')
            return None

        async def confirmation():
            from .studio_worker import PostingRejected
            from playwright.async_api import Error as BrowserError
            try:
                # React can replace the whole panel between two locator reads.
                # Bound this read-only probe and rediscover it on the next poll.
                # The publishing click remains outside this loop and runs once.
                observed=await asyncio.wait_for(read_confirmation(),timeout=1)
            except (asyncio.TimeoutError,BrowserError):return None
            if observed is None:return None
            state,current,message=observed
            if state=='failed':
                self.entry_diagnostics['submission']={'state':'failed','message':message}
                await self.record_diagnostics()
                raise PostingRejected('分享阶段失败：'+message)
            if state=='confirmed':
                self.entry_diagnostics['submission']={'state':'confirmed'}
                return current
            if self.entry_diagnostics.get('submission',{}).get('state')!='sharing':
                self.entry_diagnostics['submission']={'state':'sharing'}
                await self.record_diagnostics()
                await self.progress('6/6 Instagram 正在分享，等待完成提示')
            return None

        from .studio_worker import PostingRejected
        try:
            confirmed = await self.wait_for(confirmation, '没有获得发布成功确认', timeout=self.confirmation_timeout)
        except PostingRejected:
            raise
        except Exception:
            raise ResultUncertain('分享结果待确认：已提交但没有获得分享完成提示，请查看账号主页；不会自动重发') from None
        result = {'published':1,'verification':'instagram_dialog','confirmed_at':datetime.now(timezone.utc).isoformat(),
                  'caption':caption,'location':selected_location,'location_query':location,'asset_ids':[a['id'] for a in assets]}
        try:
            links = confirmed.locator('a[href]')
            for i in range(await links.count()):
                href = await links.nth(i).get_attribute('href',timeout=1000) or ''
                if re.fullmatch(r'/(?:p|reel)/[A-Za-z0-9_-]+/?', href):
                    result['post_url'] = 'https://www.instagram.com'+href
                    break
                if re.fullmatch(r'https://www\.instagram\.com/(?:p|reel)/[A-Za-z0-9_-]+/?',href):
                    result['post_url'] = href
                    break
        except Exception:
            # The confirmed success remains valid if the optional link vanishes.
            pass
        save_confirmed = getattr(self.browser, 'confirmed', None)
        if save_confirmed is not None:
            await save_confirmed(result)
        result['post_publish'] = await self.finish(confirmed)
        return result
