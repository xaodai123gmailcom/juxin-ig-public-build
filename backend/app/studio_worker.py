from __future__ import annotations
import asyncio
import random
import re
from urllib.parse import urlparse, quote
from .errors import ValidationError

class ResultUncertain(Exception):
    pass

def instagram_target(value):
    value=value.strip()
    if value.startswith('@'): value=value[1:]
    if re.fullmatch(r'[A-Za-z0-9._]{1,30}',value): return 'https://www.instagram.com/'+value+'/'
    u=urlparse(value)
    if u.scheme=='https' and u.hostname in {'instagram.com','www.instagram.com'} and re.fullmatch(r'/(?:p|reel)/[A-Za-z0-9_-]+/?',u.path):
        return 'https://www.instagram.com'+u.path
    raise ValidationError('目标必须是 Instagram 用户名或帖子链接')

class StudioBrowser:
    def __init__(self, worker, checkpoint, before_effect):
        self.worker=worker; self.checkpoint=checkpoint; self.before_effect=before_effect
    @property
    def page(self): return self.worker.page

    async def guard(self):
        await self.checkpoint()
        url=self.page.url.lower()
        if getattr(self,'nurture_mode',False) and urlparse(url).hostname not in {'instagram.com','www.instagram.com'}:
            raise ValidationError('当前标签页已离开 Instagram，已停止任务')
        if any(s in url for s in ('/challenge','/checkpoint','/accounts/login','/accounts/onetap','/accounts/password/reset','/two_factor','/accounts/confirm','/suspended')):
            raise ValidationError('账号需要登录或验证，请处理后再继续')
        try:
            await self.worker._guard()
        except Exception as exc:
            # Feed posts or translation extensions can contain the phrase
            # "log in to Instagram". The nurture module disambiguates
            # that text using a visible signed-in sidebar.
            if not getattr(self,'nurture_mode',False) or getattr(exc,'code',None)!='instagram_login_required': raise
            from .instagram_account_dom import ACCOUNT_SESSION_PROBE
            state=await self.page.evaluate(ACCOUNT_SESSION_PROBE)
            if state.get('login') or len(state.get('nav_labels',[]))<3:
                raise ValidationError('当前所选窗口需要登录 Instagram，请在该窗口完成登录后重试') from None
        blocked=self.page.get_by_text(re.compile(r'^(Try again later|稍后再试|操作受限|We restrict certain activity)',re.I))
        if await blocked.count() and await blocked.first.is_visible(): raise ValidationError('Instagram 提示操作受限，本任务已停止')

    async def wait(self, seconds):
        for _ in range(max(1,int(seconds))):
            await self.checkpoint(); await asyncio.sleep(1)

    async def button(self, root, pattern, required=True):
        buttons=root.get_by_role('button',name=re.compile(pattern,re.I))
        for i in range(await buttons.count()):
            if await buttons.nth(i).is_visible(): return buttons.nth(i)
        if required: raise ValidationError('页面未找到所需按钮，请检查当前页面是否加载完成')
        return None

    async def nurture_step(self, step, counts, config):
        from .standalone_nurture import StandaloneNurture
        self.nurture_mode=True
        if not getattr(self,'standalone_nurture',None):
            self.standalone_nurture=StandaloneNurture(self)
        return await self.standalone_nurture.step(step,counts)
