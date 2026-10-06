"""Standalone, duration-bounded Reels nurture. No collector-parent policy is used.

The account is verified in the worker-owned tab before any Reels navigation.
Probability decisions and pending effects are committed before native clicks.
"""
from __future__ import annotations

import asyncio
import random
import re
import math
import time
from urllib.parse import urlparse

from .errors import ValidationError
from .service import isoformat
from .studio_worker import ResultUncertain

POLICY = 'standalone-reels-8-20-70-v1'
REELS_URL = 'https://www.instagram.com/reels/'

from .instagram_identity import OWN_LINK, OWN_METRICS, signed_in_id, resolve_own_identity, wait_for_own_profile

# A playing video with exactly one local permalink and one explicit heart.
# Like state is tri-state; unknown or contradictory labels never mean unliked.
REEL = r'''() => {
 const visible=e=>{const r=e.getBoundingClientRect();return e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0};
 const labels=e=>[e.getAttribute('aria-label'),e.getAttribute('title'),...[...e.querySelectorAll('svg[aria-label]')].map(s=>s.getAttribute('aria-label'))].filter(Boolean).map(s=>s.trim());
 const yes=/^(unlike|取消赞|取消讚|取消点赞)$/i,no=/^(like|赞|讚|点赞)$/i;
 const videos=[...document.querySelectorAll('main video')].filter(v=>visible(v)&&!v.paused&&v.readyState>=2&&v.videoWidth>0&&!v.closest('[role="dialog"]')).sort((a,b)=>{const score=v=>{const r=v.getBoundingClientRect();return Math.min(r.bottom,innerHeight)-Math.max(r.top,0)};return score(b)-score(a)});
 const v=videos[0];if(!v)return null;
 for(let root=v.parentElement;root&&!['MAIN','BODY','HTML'].includes(root.tagName);root=root.parentElement){
  if([...root.querySelectorAll('video')].filter(visible).length!==1)break;
  const hearts=[...root.querySelectorAll('button,[role="button"]')].filter(e=>visible(e)&&labels(e).some(s=>yes.test(s)||no.test(s)));
  if(hearts.length!==1)continue;
  // Both feed permalink forms name the same video and share one receipt.
  const paths=[...root.querySelectorAll('a[href]')].map(a=>{try{const u=new URL(a.href,location.href),m=u.pathname.match(/^\/reels?\/([A-Za-z0-9_-]+)\/?$/);return u.protocol==='https:'&&['www.instagram.com','instagram.com'].includes(u.hostname)&&m?'/reel/'+m[1]:null}catch{return null}}).filter(Boolean);
  if(new Set(paths).size!==1)continue;
  const heart=hearts[0],values=labels(heart),on=values.some(s=>yes.test(s)),off=values.some(s=>no.test(s));
  let state=on&&!off?'liked':off&&!on?'unliked':'unknown';
  if(heart.getAttribute('aria-pressed')==='true'&&state==='unliked')state='unknown';
  if(heart.disabled||heart.getAttribute('aria-disabled')==='true')state='unknown';
  const key=paths[0];root.setAttribute('data-standalone-reel',key);heart.setAttribute('data-standalone-heart',key);
  for(const item of document.querySelectorAll('[data-standalone-video]'))item.removeAttribute('data-standalone-video');
  v.setAttribute('data-standalone-video',key);
  return {key,state,source:v.currentSrc||v.src||'',time:v.currentTime,duration:v.duration};
 }return null;
}'''

ADVANCE_GUARD = r'''() => {
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const focus=document.activeElement;
 return ![...document.querySelectorAll('[role="dialog"],[aria-modal="true"]')].some(visible)&&
  !(focus&&(focus.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(focus.tagName)));
}'''


def playback_seconds(first,current,interval):
    """Only media progress earns watch time; a finite loop reset is supported."""
    before,after=first.get('time'),current.get('time')
    if any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in (before,after)):
        return 0.0
    delta=after-before
    if delta<0:
        duration=first.get('duration')
        if (type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0 or
                before>duration or after>duration):return 0.0
        delta=duration-before+after
    return min(max(0.0,interval),max(0.0,delta))


def exact_count(value):
    if type(value) is int:
        return value if 0<=value<=9223372036854775807 else None
    if not isinstance(value,str):return None
    value=value.strip()
    if re.fullmatch(r'\d+',value):return exact_count(int(value))
    if re.fullmatch(r'\d{1,3}(?:[,\.\s]\d{3})+',value):
        return exact_count(int(re.sub(r'[,\.\s]','',value)))
    return None




class StandaloneNurture:
    def __init__(self,browser):
        self.browser=browser
        self.identity=None
        self.decisions=getattr(browser,'nurture_decisions',{})
        self.actions=getattr(browser,'nurture_actions',{})
        self.draw=random.random

    @property
    def page(self):return self.browser.page

    async def note(self,status,detail):
        callback=getattr(self.browser,'nurture_observation',None)
        if callback:await callback({'action':'like','status':status,'detail':detail})

    async def progress(self,text):
        callback=getattr(self.browser,'progress',None)
        if callback:await callback(text)
        else:await self.browser.checkpoint()

    async def guard(self):
        await self.browser.guard()
        if self.identity:
            if await signed_in_id(self.page)!=self.identity['instagram_user_id']:
                raise ValidationError('运行中登录账号已变化，已停止养号')
            await resolve_own_identity(self.page,self.browser.checkpoint,expected=self.identity)

    async def prepare(self):
        from .instagram_home import prepare_instagram_home
        snapshot={'username':'','instagram_user_id':'','posts_count':None,'followers_count':None,'following_count':None,
                  'checked_at':isoformat(),'status':'unavailable','message':''}
        try:
            await self.browser.checkpoint()
            await self.progress('正在打开任务主页')
            await self.browser.worker.open_posting_page()
            await prepare_instagram_home(self.page,self.browser.checkpoint)
            await self.browser.guard()
            await self.progress('正在核验当前登录账号')
            resolved=await resolve_own_identity(self.page,self.browser.checkpoint)
            uid=resolved['instagram_user_id'];username=resolved['username']
            expected=getattr(self.browser,'expected_nurture_identity',{})
            if expected.get('username') and (username!=expected['username'] or uid!=expected.get('instagram_user_id')):
                raise ValidationError('当前账号与本轮历史账号不一致，请停止原任务后重新创建养号任务')
            await self.progress('正在读取自己的主页信息')
            await self.page.goto('https://www.instagram.com/'+username+'/',wait_until='domcontentloaded',timeout=45000)
            await prepare_instagram_home(self.page,self.browser.checkpoint)
            await self.browser.guard()
            raw=await wait_for_own_profile(self.page,username,uid,self.browser.checkpoint,
                metrics_ready=lambda data:all(exact_count(data.get(key)) is not None
                    for key in ('posts_count','followers_count','following_count')))
            snapshot.update(username=username,instagram_user_id=uid,checked_at=isoformat())
            for key in ('posts_count','followers_count','following_count'):
                snapshot[key]=exact_count(raw.get(key))
            snapshot['status']='ok' if all(snapshot[k] is not None for k in ('posts_count','followers_count','following_count')) else 'partial'
            snapshot['message']='自己的主页读取' if snapshot['status']=='ok' else '部分主页数字未能精确读取，保留为未知'
            self.identity={'username':username,'instagram_user_id':uid}
            await self.browser.account_snapshot(snapshot)
        except (Exception,asyncio.CancelledError) as exc:
            if not self.identity:
                snapshot.update(checked_at=isoformat(),message=str(exc)[:220] or '读取自己的主页时中断')
                # Cancellation/lease loss must not be swallowed by a snapshot write.
                if not isinstance(exc,asyncio.CancelledError):await self.browser.account_snapshot(snapshot)
            raise
        await self.guard()
        await self.progress('正在打开 Reels 并核验播放')
        await self.page.goto(REELS_URL,wait_until='domcontentloaded',timeout=45000)
        await prepare_instagram_home(self.page,self.browser.checkpoint)
        await self.guard()
        previous=None
        for _ in range(30):
            current=await self.current()
            if self.same(previous,current):break
            previous=current
            await asyncio.sleep(.1)
        else:raise ValidationError('Reels 未加载出稳定的播放视频，未开始养号计时')
        callback=getattr(self.browser,'nurture_ready',None)
        if callback:await callback()

    async def current(self):
        await self.guard()
        url=urlparse(self.page.url)
        if (url.scheme!='https' or url.hostname not in {'instagram.com','www.instagram.com'} or
                not re.fullmatch(r'/(?:reels(?:/[A-Za-z0-9_-]+)?|reel/[A-Za-z0-9_-]+)/?',url.path)):
            raise ValidationError('当前页面已离开 Reels，已停止养号')
        return await self.page.evaluate(REEL)

    @staticmethod
    def same(a,b):
        return bool(a and b and a.get('source') and a.get('key')==b.get('key') and a.get('source')==b.get('source'))

    async def decide(self,current,counts):
        key=current['key'];token='like:'+key
        prior=self.actions.get(token,{})
        if prior.get('state') in {'pending','unresolved_stopped'}:
            raise ResultUncertain('该视频上次点赞结果待核验，未重复执行')
        if key in self.decisions or prior.get('state')=='confirmed':return
        selected=current['state']=='unliked' and self.draw()<.70
        data={'selected':selected,'state':current['state'],'decided_at':isoformat()}
        callback=getattr(self.browser,'nurture_decision',None)
        if callback:await callback(key,data)
        self.decisions[key]=data
        if current.get('state') not in {'unliked','liked'}:
            await self.note('unknown','点赞状态未知，跳过本视频');return
        if not selected:
            await self.note('already' if current['state']=='liked' else 'probability','已点赞，保持原状态' if current['state']=='liked' else '本视频未命中固定概率');return
        await self.like(current,counts)

    async def like(self,expected,counts):
        async with self.browser.worker._destructive_action_lease():
            await self.browser.checkpoint()
            await self._like_owned(expected,counts)

    async def _like_owned(self,expected,counts):
        key=expected['key'];token='like:'+key
        if not re.fullmatch(r'/reel/[A-Za-z0-9_-]+',key):return
        current=await self.current()
        if not self.same(current,expected) or current.get('state')!='unliked':return
        # Commit an uncertain boundary before any native click. A failed or
        # interrupted click is never automatically replayed after restart.
        await self.browser.nurture_action_begin(token,{'action':'like','target':key,'state':'pending'})
        await self.browser.before_effect('正在点赞当前 Reels')
        final=await self.current()
        if not self.same(final,expected) or final.get('state')!='unliked':
            raise ResultUncertain('点赞前视频或状态变化，请核验后继续')
        # Re-resolve either permalink form at native-click time. Query strings
        # may vary, but the shortcode path must still be exact (never a prefix).
        paths=(key,key.replace('/reel/','/reels/',1))
        hrefs=[origin+path+slash for origin in ('','https://www.instagram.com','https://instagram.com')
               for path in paths for slash in ('','/')]
        links=','.join(selector for href in hrefs for selector in
                       ('a[href="'+href+'"]','a[href^="'+href+'?"]'))
        scope=self.page.locator('[data-standalone-reel="'+key+'"]').filter(has=self.page.locator(links))
        unlike=('Unlike','取消赞','取消讚','取消点赞')
        excluded=''.join(':not(['+attr+'*="'+label+'" i])' for attr in ('aria-label','title') for label in unlike)
        heart=scope.locator('[data-standalone-heart="'+key+'"]:not([aria-pressed="true" i]):not([aria-disabled="true" i]):not([disabled])'+excluded).filter(
            has_not=self.page.locator(','.join('svg['+attr+'*="'+label+'" i]' for attr in ('aria-label','title') for label in unlike)))
        labels=('Like','赞','讚','点赞')
        direct=heart.and_(self.page.locator(','.join('['+attr+'="'+label+'" i]' for attr in ('aria-label','title') for label in labels)))
        nested=heart.filter(has=self.page.locator(','.join('svg[aria-label="'+label+'" i]' for label in labels)))
        button=direct.or_(nested)
        if await button.count()!=1:raise ResultUncertain('点赞控件无法唯一核验，已停止以免重复操作')
        await button.click(timeout=1500)
        for _ in range(12):
            current=await self.current()
            if not self.same(current,expected):break
            if current.get('state')=='liked':
                counts['like']=counts.get('like',0)+1
                await self.browser.nurture_action_confirmed(token,dict(counts))
                await self.note('confirmed','页面已确认点赞')
                return
            await asyncio.sleep(.25)
        raise ResultUncertain('点赞已尝试但结果未知，停止任务等待人工核验')

    def elapsed(self):
        callback=getattr(self.browser,'nurture_elapsed',None)
        return callback() if callback else time.monotonic()

    def remaining(self):
        callback=getattr(self.browser,'nurture_remaining',None)
        return callback() if callback else float('inf')

    def clock(self):
        callback=getattr(self.browser,'nurture_clock',None)
        return callback() if callback else time.monotonic()

    async def dwell(self,seconds):
        expected=sampled=await self.current()
        if not expected:raise ValidationError('Reels 未提供可核验的播放视频，未开始停留')
        sampled_at=self.clock();watched=stalled=0.0
        while seconds-watched>1e-6 and self.remaining()>1e-6:
            await asyncio.sleep(min(.25,seconds-watched,self.remaining()))
            current=await self.current();now=self.clock()
            interval=max(0.0,now-sampled_at)
            if current and not self.same(expected,current):
                raise ValidationError('停留期间视频已变化，已停止养号并保留窗口')
            credit=playback_seconds(sampled,current,interval) if sampled and current else 0.0
            credit=min(credit,seconds-watched,self.remaining())
            if credit>1e-6:
                watched+=credit;stalled=0.0
                callback=getattr(self.browser,'nurture_watched',None)
                if callback:await callback(credit)
            else:
                stalled+=interval
                if stalled>=6.0:raise ValidationError('Reels 播放停滞，已停止养号；停滞时间未计入实际时长')
            sampled,sampled_at=current,now
        return watched

    async def advance(self,expected):
        current=await self.current()
        if not self.same(current,expected):raise ValidationError('切换前视频已变化或不可核验，已停止养号')
        controls=self.page.get_by_role('button',name=re.compile(r'^(Next|Next reel|Next video|Scroll down|Down chevron|Arrow down|下一个|下一個|下一条|下一則|向下滚动|向下捲動)$',re.I))
        visible=[controls.nth(i) for i in range(await controls.count()) if await controls.nth(i).is_visible()]
        if len(visible)>1:
            raise ValidationError('下一条视频按钮不唯一，已停止养号并保留窗口')
        await self.guard()
        current=await self.current()
        if not self.same(current,expected):raise ValidationError('切换前视频已变化或不可核验，已停止养号')
        if not await self.page.evaluate(ADVANCE_GUARD):
            raise ValidationError('页面弹窗或输入焦点不允许切换视频，已停止养号')
        if visible:await visible[0].click(timeout=1500)
        else:
            video=self.page.locator('video[data-standalone-video="'+expected['key']+'"]')
            if await video.count()!=1:raise ValidationError('无法唯一核验当前视频，已停止养号')
            await video.press('ArrowDown',timeout=1500)
        previous=None;deadline=self.clock()+6.0
        for _ in range(40):
            if self.clock()>=deadline:break
            await asyncio.sleep(min(.15,max(0.0,deadline-self.clock())))
            current=await self.current()
            if current and current['key']!=expected['key'] and self.same(previous,current):return
            previous=current
        raise ValidationError('下一条 Reels 未能确认，已停止养号并保留窗口')

    async def step(self,step,counts):
        if self.identity is None:await self.prepare()
        if self.remaining()<8:return counts
        first=await self.current()
        await asyncio.sleep(.15)
        current=await self.current()
        if not self.same(first,current):
            await self.note('unknown','没有稳定的播放视频，停止本次任务')
            raise ValidationError('Reels 未提供可核验的播放视频，未执行点赞')
        seconds=min(step['seconds'],self.remaining())
        if seconds<8:return counts
        actual=await self.dwell(seconds)
        after=await self.current()
        if not self.same(current,after):
            await self.note('changed','停留期间视频变化，跳过点赞')
            counts['skipped']=counts.get('skipped',0)+1
            return counts
        counts['browse']=counts.get('browse',0)+1
        counts['browse_seconds']=round(counts.get('browse_seconds',0)+actual,3)
        callback=getattr(self.browser,'nurture_browsed',None)
        if callback:await callback(dict(counts))
        if self.remaining()>0:await self.decide(after,counts)
        if self.remaining()>=8:await self.advance(after)
        return counts
