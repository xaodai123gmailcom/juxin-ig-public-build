"""Scoped nurture interactions. Counts require observed state, never just a click."""
import asyncio
import random
import re
import uuid
from .studio_worker import ResultUncertain
from .errors import ValidationError

# Read layout / semantic controls; mark only the selected scope, never send via JS.
SCOPE = r'''({token,profile}) => {
 const visible=e=>{const r=e.getBoundingClientRect();return e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth};
 const labels=e=>[e.getAttribute('aria-label'),e.getAttribute('title'),...[...e.querySelectorAll('svg[aria-label]')].map(n=>n.getAttribute('aria-label')),e.innerText].filter(Boolean).map(s=>s.trim());
 const like=/^(like|unlike|赞|讚|点赞|取消赞|取消讚|取消点赞)$/i;
 const related=/^(comment|comments|share|send|save|saved|remove from collection|remove from saved|评论|留言|分享|收藏|已收藏|取消收藏|儲存|移除)$/i;
 const controls=r=>[...r.querySelectorAll('button,[role="button"],svg[aria-label]')].map(e=>e.closest('button,[role="button"]')||e).filter((e,i,a)=>a.indexOf(e)===i&&visible(e));
 let candidates=[];
 if(profile){
  const name=location.pathname.split('/').filter(Boolean)[0]?.toLowerCase();
  candidates=[...document.querySelectorAll('main header')].filter(visible);
  if(!candidates.length)for(const h of document.querySelectorAll('main h1,main h2')){
   if(h.textContent.trim().toLowerCase()!==name||!visible(h))continue;
   for(let p=h.parentElement;p&&p.tagName!=='MAIN';p=p.parentElement){
    if(controls(p).some(e=>labels(e).some(s=>/^(follow|follow back|following|requested|关注|关注中|已关注|已请求|回关|追蹤|追蹤中|已追蹤)$/i.test(s)))){candidates.push(p);break;}
   }
  }
 }else{
  const all=[...document.querySelectorAll('article,[role="article"],[role="dialog"]')].filter(visible);
  const primary=e=>controls(e).some(b=>labels(b).some(s=>like.test(s)));
  candidates=all.filter(primary);
  // Current feed / Reels can omit article. Require media plus an action group,
  // and stop before a page-wide main/nav container or a second post's controls.
  if(!candidates.length)for(const b of controls(document.querySelector('main')||document)){
   if(!labels(b).some(s=>like.test(s))||b.closest('nav,aside,[role="navigation"]'))continue;
   if(candidates.some(r=>r.contains(b)))continue;
   for(let p=b.parentElement;p&&!['MAIN','BODY','HTML'].includes(p.tagName);p=p.parentElement){
    const buttons=controls(p),likes=buttons.filter(e=>labels(e).some(s=>like.test(s)));
    if(likes.length>1)break;
    if(p.querySelector('video,img')&&buttons.some(e=>labels(e).some(s=>related.test(s)))){candidates.push(p);break;}
   }
  }
 }
 candidates=[...new Set(candidates)].filter(e=>visible(e)&&!candidates.some(other=>other!==e&&e.contains(other)));
 candidates.sort((a,b)=>{const score=e=>{const r=e.getBoundingClientRect();return Math.min(r.bottom,innerHeight)-Math.max(r.top,0)};return score(b)-score(a)});
 const root=candidates[0];if(!root)return null;
 const id=root.getAttribute('data-juxin-nurture-scope')||token;root.setAttribute('data-juxin-nurture-scope',id);
 const link=[...root.querySelectorAll('a[href]')].map(a=>{try{return new URL(a.href,location.href).pathname}catch{return ''}}).find(p=>/^\/(p|reel)\/[^/]+\/?$/.test(p));
 return {id,key:link||(profile?location.pathname:id)};
}'''

CONTROL = r'''(root,{action,token}) => {
 const patterns={like:[/^(like|赞|讚|点赞)$/i,/^(unlike|取消赞|取消讚|取消点赞)$/i],save:[/^(save|收藏|儲存)$/i,/^(saved|remove|remove from collection|remove from saved|已收藏|取消收藏|移除|已儲存)$/i],follow:[/^(follow|follow back|关注|回关|追蹤|追踪)$/i,/^(following|requested|已关注|已请求|关注中|追蹤中|已追蹤|已发送请求|已發送邀請)$/i],comment:[/^(comment|comments|评论|留言)$/i],submit:[/^(post|send|发布|發佈|发送|傳送)$/i]};
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const buttons=[...root.querySelectorAll('button,[role="button"],svg[aria-label]')].map(e=>e.closest('button,[role="button"]')||e).filter((e,i,a)=>a.indexOf(e)===i&&visible(e));
 for(const b of buttons){
  const labels=[b.getAttribute('aria-label'),b.getAttribute('title'),...[...b.querySelectorAll('svg[aria-label]')].map(e=>e.getAttribute('aria-label')),b.innerText].filter(Boolean).map(s=>s.trim());
  const p=patterns[action],state=p[1]&&labels.some(s=>p[1].test(s))?'already':labels.some(s=>p[0].test(s))?'ready':null;
  if(!state)continue;
  b.setAttribute('data-juxin-nurture-control',token);
  return {state,disabled:!!b.disabled||b.getAttribute('aria-disabled')==='true'};
 }
 return null;
}'''

FIELD = r'''(root,token) => {
 const fields=[...root.querySelectorAll('textarea,input,[contenteditable="true"][role="textbox"],[contenteditable="true"][data-lexical-editor]')];
 const found=fields.find(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&/add a comment|write a comment|comment|添加评论|发表评论|撰寫留言|新增留言|留言/i.test([e.getAttribute('placeholder'),e.getAttribute('aria-label'),e.getAttribute('data-placeholder')].join(' ')));
 if(!found)return false;found.setAttribute('data-juxin-nurture-field',token);return true;
}'''
FIELD_VALUE = '(el)=>el.isContentEditable?(el.innerText||el.textContent||""):(el.value||"")'
COMMENT_COUNT = r'''(root,text)=>[...root.querySelectorAll('span,p,div')].filter(e=>!e.closest('textarea,input,[contenteditable="true"],[data-juxin-translation]')&&e.getClientRects().length&&e.textContent.trim()===text&&!Array.from(e.children).some(c=>c.textContent.trim()===text)).length'''
SCROLL = r'''() => {const roots=[...document.querySelectorAll('[data-juxin-nurture-scope]')];for(const root of roots){for(let e=root.parentElement;e&&e!==document.body;e=e.parentElement){if(e.scrollHeight>e.clientHeight+100&&/auto|scroll/.test(getComputedStyle(e).overflowY)){e.scrollBy(0,Math.max(250,e.clientHeight*.65));return;}}}window.scrollBy(0,Math.max(250,innerHeight*.65));}'''

ACTIONS = {'like':'点赞','save':'收藏','follow':'关注','comment':'评论'}


def gate(action, counts, config, draw=None):
    if config[action+'_probability'] <= 0 or config[action+'_limit'] <= 0:
        return 'disabled' if config[action+'_probability'] <= 0 else 'limit'
    if counts.get(action,0) >= config[action+'_limit']:
        return 'limit'
    if (draw or random.randrange)(100) >= config[action+'_probability']:
        return 'probability'
    return 'ready'


class NurtureInteractions:
    def __init__(self, browser):
        self.browser=browser
        self.commented=set()

    @property
    def page(self):return self.browser.page

    async def note(self,action,status,detail):
        callback=getattr(self.browser,'nurture_observation',None)
        if callback:await callback({'action':action,'status':status,'detail':detail})

    async def scope(self, profile=False):
        await self.browser.checkpoint()
        info=await self.page.evaluate(SCOPE,{'token':uuid.uuid4().hex,'profile':profile})
        return (self.page.locator(f'[data-juxin-nurture-scope="{info["id"]}"]'),info['key']) if info else (None,None)

    async def control(self,root,action):
        if root is None or not await root.count():return None,None
        token=uuid.uuid4().hex
        info=await root.evaluate(CONTROL,{'action':action,'token':token})
        return (root.locator(f'[data-juxin-nurture-control="{token}"]'),info) if info else (None,None)

    async def confirmed(self,counts,action):
        counts[action]=counts.get(action,0)+1
        pending=getattr(self,'pending_action',None)
        commit=getattr(self.browser,'nurture_action_confirmed',None)
        if pending and callable(commit):
            await commit(pending,dict(counts))
            self.pending_action=None
            await self.note(action,'confirmed','页面已确认'+ACTIONS[action])
            return
        callback=getattr(self.browser,'nurture_confirmed',None)
        if callback:await callback(dict(counts))
        await self.note(action,'confirmed','页面已确认'+ACTIONS[action])

    async def begin(self,action,key):
        if not key:
            await self.note(action,'missing','无法确认互动目标，保留当前页面');return False
        token=action+':'+str(key).rstrip('/')
        journal=getattr(self.browser,'nurture_actions',{})
        prior=journal.get(token,{})
        if prior.get('state')=='confirmed':
            await self.note(action,'already','该目标已完成，恢复任务时跳过');return False
        if prior.get('state')=='pending':
            raise ResultUncertain('该目标上次互动结果待确认，请先人工核验')
        callback=getattr(self.browser,'nurture_action_begin',None)
        if callable(callback) and not re.fullmatch(r'/(?:p/|reel/)?[\w.-]+/?',str(key)):
            await self.note(action,'missing','缺少可恢复的目标链接，跳过本次互动');return False
        if callable(callback):
            await callback(token,{'action':action,'target':key,'state':'pending'})
            self.pending_action=token
        return True

    async def stable(self,root,key):
        if key is None:return
        if root is None or not await root.count():raise ValidationError('当前互动区域已被替换，已停止任务')
        current=await root.evaluate(r"(root)=>[...root.querySelectorAll('a[href]')].map(a=>new URL(a.href,location.href).pathname).find(p=>/^\/(p|reel)\/[^/]+\/?$/.test(p))||location.pathname")
        if str(key).startswith('/') and current.rstrip('/')!=str(key).rstrip('/'):
            raise ValidationError('互动目标已切换，已停止任务')

    async def toggle(self,root,action,counts,key=None):
        target,info=await self.control(root,action)
        if not info:
            await self.note(action,'missing','当前内容未找到'+ACTIONS[action]+'按钮');return
        if info['state']=='already':
            await self.note(action,'already','已'+ACTIONS[action]+'，保持原状态');return
        if info['disabled']:
            await self.note(action,'unavailable',ACTIONS[action]+'按钮当前不可用');return
        await self.browser.guard();await self.stable(root,key)
        if not await self.begin(action,key):return
        await self.browser.before_effect(action)
        await self.stable(root,key)
        await target.click(timeout=10000)
        for _ in range(15):
            await self.browser.guard()
            await self.stable(root,key)
            _,state=await self.control(root,action)
            if state and state['state']=='already':
                await self.confirmed(counts,action);return
            await asyncio.sleep(.4)
        await self.note(action,'uncertain','已点击，但页面尚未确认'+ACTIONS[action])
        raise ResultUncertain(ACTIONS[action]+'已点击，尚未确认；停止任务以免重复操作')

    async def field(self,root):
        if root is None or not await root.count():return None
        token=uuid.uuid4().hex
        if await root.evaluate(FIELD,token):return root.locator(f'[data-juxin-nurture-field="{token}"]')
        return None

    async def comment(self,root,key,counts,config):
        field=await self.field(root)
        if field is None:
            opener,info=await self.control(root,'comment')
            if opener is not None and not info['disabled']:
                await self.browser.guard();await opener.click(timeout=10000)
                for _ in range(10):
                    await self.browser.checkpoint()
                    newroot,newkey=await self.scope()
                    # An unrelated post must never inherit the pending comment.
                    if newroot is not None and newkey==key:root=newroot
                    field=await self.field(root)
                    if field is not None:break
                    await asyncio.sleep(.3)
        if field is None:
            await self.note('comment','missing','当前帖子未找到评论输入框，可能未开放评论');return
        if (await field.evaluate(FIELD_VALUE)).strip():
            await self.note('comment','draft','评论框已有内容，保留原稿');return
        text=random.choice(config['comments'])
        previous=await root.evaluate(COMMENT_COUNT,text)
        if key in self.commented or previous:
            await self.note('comment','already','当前帖子已评论，避免重复提交');return
        if not await self.begin('comment',key):return
        await self.browser.guard();await self.stable(root,key);await field.fill(text)
        await self.browser.before_effect('comment')
        await self.stable(root,key)
        submit,info=await self.control(root,'submit')
        if submit is not None and not info['disabled']:await submit.click(timeout=10000)
        else:await field.press('Enter')
        for _ in range(25):
            await self.browser.guard()
            await self.stable(root,key)
            current=await self.field(root)
            if current is not None and not (await current.evaluate(FIELD_VALUE)).strip() and await root.evaluate(COMMENT_COUNT,text)>previous:
                self.commented.add(key);await self.confirmed(counts,'comment');return
            await asyncio.sleep(.4)
        await self.note('comment','uncertain','评论已提交，尚未确认新增评论')
        raise ResultUncertain('评论已提交，尚未确认新增评论；停止任务以免重复发送')

    async def run(self,step,counts,config):
        selected=[]
        for action in ACTIONS:
            state=gate(action,counts,config)
            if state=='ready':selected.append(action)
            else:await self.note(action,state,{'disabled':'未启用','limit':'已达每日上限或上限为 0','probability':'本步未命中设定概率'}[state])
        if not selected:return counts
        if step['surface']=='stories':
            for action in selected:await self.note(action,'unsupported','快拍步骤只浏览；帖子互动请选择首页动态、Reels 或指定帖子')
            return counts
        if step['surface']=='profile' and 'follow' in selected:
            root,key=await self.scope(profile=True)
            await self.toggle(root,'follow',counts,key);selected.remove('follow')
        if not selected:return counts
        root,key=await self.scope()
        # A profile/search grid is not a post. Open one visible, explicit post
        # link in this task's existing page, without touching other account tabs.
        if root is None and any(a in selected for a in ('like','save','comment')) and step['surface'] in {'profile','search'}:
            links=self.page.locator('main a[href^="/p/"],main a[href^="/reel/"]')
            for i in range(await links.count()):
                link=links.nth(i)
                if not await link.is_visible():continue
                href=await link.get_attribute('href')
                if not re.fullmatch(r'/(p|reel)/[A-Za-z0-9_-]+/?',href or ''):continue
                await self.browser.guard();await link.click(timeout=10000)
                for _ in range(15):
                    await self.browser.guard();root,key=await self.scope()
                    if root is not None:break
                    await asyncio.sleep(.3)
                break
        for action in selected:
            if root is None:await self.note(action,'missing','未找到可互动的帖子区域');continue
            if action=='comment':await self.comment(root,key,counts,config)
            else:await self.toggle(root,action,counts,key)
        return counts
