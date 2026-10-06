"""Read-only account identity proof shared by posting and standalone nurture."""
import asyncio
from .errors import ValidationError

# The sidebar may collapse to an avatar without a Profile aria-label. That is
# only a candidate username: a numeric session actor and exact own-profile edit
# controls are still required before a task is allowed to act.
_HELPERS = r'''
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const host=u=>['instagram.com','www.instagram.com'].includes(u.hostname);
 const reserved=new Set(['accounts','direct','explore','reels','stories','about','legal','web','p','reel','create']);
 const profileName=u=>{const m=u.pathname.match(/^\/([A-Za-z0-9._]{1,30})\/?$/);return host(u)&&m&&!reserved.has(m[1].toLowerCase())?m[1].toLowerCase():''};
 const ownControl=scope=>[...scope.querySelectorAll('a,button,[role="button"]')].some(e=>{
   if(!visible(e)||e.closest('article,[role="article"],[role="dialog"],[aria-modal="true"]'))return false;
   let u;try{u=new URL(e.getAttribute('href')||'',location.href)}catch{return false}
   if(e.tagName==='A'&&!host(u))return false;
   return /^(edit profile|编辑主页|編輯個人檔案|编辑个人资料|編輯個人資料|编辑个人主页)$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim())||
     (e.tagName==='A'&&host(u)&&u.pathname.replace(/\/$/,'')==='/accounts/edit');
 });
 const ownHeader=name=>{
   if(location.pathname.replace(/\/$/,'').toLowerCase()!=='/'+name)return null;
   const main=document.querySelector('main');if(!main||main.closest('[role="dialog"],[aria-modal="true"]'))return null;
   const heading=[...main.querySelectorAll('h1,h2,[role="heading"]')].find(e=>visible(e)&&!e.closest('article,[role="article"],[role="dialog"]')&&(e.innerText||'').trim().toLowerCase()===name);
   if(!heading)return null;
   const header=heading.closest('header');
   if(header&&ownControl(header))return header;
   for(let p=heading.parentElement;p&&p!==main;p=p.parentElement){
     if(p.querySelector('article,video,a[href^="/p/"],a[href^="/reel/"]'))break;
     if(ownControl(p))return p;
   }
   return null;
 };
'''

OWN_LINK = '() => {' + _HELPERS + r'''
 const names=new Set();
 for(const a of document.querySelectorAll('a[href]')){
   if(!visible(a)||a.closest('main,article,[role="article"],[role="dialog"],[aria-modal="true"]'))continue;
   let u;try{u=new URL(a.href,location.href)}catch{continue}
   const name=profileName(u);if(!name)continue;
   const labels=[a.getAttribute('aria-label'),a.getAttribute('title'),a.innerText,
     ...[...a.querySelectorAll('[aria-label],img')].flatMap(e=>[e.getAttribute('aria-label'),e.getAttribute('alt')])];
   const labelled=labels.some(s=>/^(profile|个人主页|個人檔案|个人资料|個人主頁)$/i.test((s||'').trim()));
   const r=a.getBoundingClientRect(),nav=a.closest('nav,aside,[role="navigation"]');
   const avatar=[...a.querySelectorAll('img')].some(e=>{const q=e.getBoundingClientRect();return visible(e)&&q.width>=12&&q.width<=96&&q.height>=12&&q.height<=96&&Math.abs(q.width-q.height)<=12});
   // Narrow left-rail fallback excludes homepage suggestions on the right and
   // feed/story avatars under main. Require navigation siblings, not any image.
   const rail=r.left>=0&&r.left<100&&r.width<=160&&[...document.querySelectorAll('a[href]')].filter(e=>{
     if(!visible(e)||e.closest('main,article,[role="dialog"]')||e.getBoundingClientRect().left>=100)return false;
     try{const v=new URL(e.href,location.href);return host(v)&&['/','/reels/','/direct/inbox/','/explore/'].includes(v.pathname)}catch{return false}
   }).length>=2;
   if(labelled||(avatar&&(nav||rail)))names.add(name);
 }
 // On an already-open own profile the exact route + heading + edit control is
 // stronger evidence than a missing/collapsed navigation label.
 const current=profileName(new URL(location.href));if(current&&ownHeader(current))names.add(current);
 return names.size===1?[...names][0]:'';
}'''

OWN_METRICS = 'username => {' + _HELPERS + r'''
 const scope=ownHeader(username);if(!scope)return null;
 const result={};
 for(const [key,words] of [['posts_count','posts?|帖子|則貼文|篇帖子|篇貼文|貼文'],['followers_count','followers?|粉丝|粉絲|位追蹤者|追蹤者'],['following_count','following|关注|關注|追蹤中']]){
   const route=key==='followers_count'?'followers':key==='following_count'?'following':'';
   const links=route?[...scope.querySelectorAll('a[href]')].filter(e=>{try{const u=new URL(e.href,location.href);return host(u)&&u.pathname.replace(/\/$/,'').toLowerCase()==='/'+username+'/'+route}catch{return false}}):[];
   // Current profiles render their 0 counters as spans/divs instead of li rows.
   // An anchored entire metric label is required; feed captions never qualify.
   for(const e of [...links,...scope.querySelectorAll('li,span,div,a')]){
     if(!visible(e)||e.closest('article,[role="article"],[role="dialog"]'))continue;
     const text=(e.innerText||'').trim();if(text.length>90)continue;
     const m=text.match(new RegExp('^([\\d.,\\s]+(?:万|萬|千|[KMBkmb])?)\\s*(?:'+words+')$','i'));
     if(m){result[key]=e.getAttribute('title')||e.querySelector('[title]')?.getAttribute('title')||m[1];break;}
   }
 }
 return result;
}'''


def _unverified():
    return ValidationError('无法核验当前登录账号或自己的主页，已停止当前操作',details={'reason':'posting_identity_unverified'})


def _mismatch():
    return ValidationError('当前登录账号与任务目标不一致，已停止当前操作',details={'reason':'posting_account_mismatch'})


async def signed_in_id(page):
    cookies=await page.context.cookies('https://www.instagram.com/')
    ids={str(c.get('value','')) for c in cookies if c.get('name')=='ds_user_id'}
    if len(ids)!=1 or not next(iter(ids),'').isdigit():
        raise ValidationError('无法核验当前登录账号，请在窗口内确认登录状态',details={'reason':'posting_identity_unverified'})
    return next(iter(ids))


async def resolve_own_identity(page,checkpoint,*,expected=None,timeout=10.0,poll=.25):
    expected=expected or {}
    uid=await signed_in_id(page)
    if expected.get('instagram_user_id') and uid!=expected['instagram_user_id']:raise _mismatch()
    try:
        async with asyncio.timeout(timeout):
            while True:
                await checkpoint()
                if await signed_in_id(page)!=uid:raise _mismatch()
                name=await page.evaluate(OWN_LINK)
                if name:
                    if expected.get('username') and name!=expected['username'].casefold():raise _mismatch()
                    if await signed_in_id(page)!=uid:raise _mismatch()
                    return {'username':name,'instagram_user_id':uid}
                await asyncio.sleep(poll)
    except TimeoutError:raise _unverified() from None


async def wait_for_own_profile(page,name,uid,checkpoint,*,timeout=10.0,poll=.25,metrics_ready=None):
    """Wait for identity proof; optional unread metrics stay unknown, never zero."""
    raw=None;metrics_deadline=None
    try:
        async with asyncio.timeout(timeout):
            while True:
                await checkpoint()
                if await signed_in_id(page)!=uid:raise _mismatch()
                current=await page.evaluate(OWN_LINK)
                if current and current!=name:raise _mismatch()
                raw=await page.evaluate(OWN_METRICS,name) if current==name else None
                if raw is not None:
                    if await signed_in_id(page)!=uid:raise _mismatch()
                    if metrics_ready is None or metrics_ready(raw):return raw
                    if metrics_deadline is None:metrics_deadline=asyncio.get_running_loop().time()+min(3.0,timeout/2)
                    if asyncio.get_running_loop().time()>=metrics_deadline:return raw
                else:metrics_deadline=None
                await asyncio.sleep(poll)
    except TimeoutError:raise _unverified() from None
