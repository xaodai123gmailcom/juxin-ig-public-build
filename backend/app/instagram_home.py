"""Own-account counts and narrowly scoped homepage notices."""
import asyncio,re
from urllib.parse import urlparse
from .errors import ValidationError, ConflictError

HOME_NOTICE = r'''() => {
 const visible=el=>el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
 const blocked=/try again later|we restrict certain activity|account (?:has been |is )?(?:suspended|disabled|restricted)|账号受限|帐号受限|操作受限|稍后再试|稍後再試|账号已停用|帳號已停用|确认你的身份|確認你的身分|verify your identity|confirm your identity/i;
 const safe=/turn on notifications|开启通知|打开通知|啟用通知|开启推送通知|save your login info|保存登录信息|儲存登入資料|保存你的登录信息/i;
 const dismiss=/^(not now|暂不|暫不|以后再说|稍后再说|稍後再說|以后|下次再说)$/i;
 const roots=[...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[role="alertdialog"]')].filter(visible);
 // Some notices have no dialog role. Restrict fallback to a centred fixed panel.
 for(const el of document.querySelectorAll('section,div')){
  if(!visible(el)||!safe.test(el.innerText||''))continue;
  const r=el.getBoundingClientRect();
  if(r.width<240||r.width>600||r.height<100||r.height>500||r.left>innerWidth/2||r.right<innerWidth/2)continue;
  for(let p=el;p&&p!==document.body;p=p.parentElement){if(getComputedStyle(p).position==='fixed'){roots.push(el);break;}}
 }
 for(const root of roots.reverse()){
  const text=(root.innerText||'').trim();
  if(blocked.test(text))return {state:'blocked',message:text.slice(0,220)};
  if(safe.test(text)){
   const target=[...root.querySelectorAll('button,[role="button"],a,div,span')].find(el=>visible(el)&&dismiss.test(el.innerText?.trim()||''));
   if(target){document.querySelectorAll('[data-juxin-dismiss-notice]').forEach(el=>el.removeAttribute('data-juxin-dismiss-notice'));target.setAttribute('data-juxin-dismiss-notice','true');return {state:'dismiss',message:text.slice(0,120)};}
  }
  // A visible unknown modal must be handled by the operator, not guessed.
  if(text&&!/^(创建新帖子|Create new post)/i.test(text))return {state:'unknown',message:text.slice(0,220)};
 }
 return {state:'ready'};
}'''

async def block_notification_prompt(page,checkpoint=None):
    """Scope the browser permission to this account context and Instagram origin."""
    # Detaching a CDP session resets its permission overrides. Keep a browser
    # session for the worker connection, including across temporary profile tabs.
    probe=None
    try:
        if checkpoint:await checkpoint()
        probe=await page.context.new_cdp_session(page)
        info=(await probe.send('Target.getTargetInfo'))['targetInfo']
        context_id=info.get('browserContextId')
        session=getattr(page.context,'_juxin_notifications_session',None)
        if session is None:
            session=await page.context.browser.new_browser_cdp_session()
            page.context._juxin_notifications_session=session
        for origin in ('https://www.instagram.com','https://instagram.com'):
            if checkpoint:await checkpoint()
            params={'permission':{'name':'notifications'},'setting':'denied','origin':origin}
            if context_id:params['browserContextId']=context_id
            await session.send('Browser.setPermission',params)
        return True
    except ConflictError:raise
    except Exception:return False
    finally:
        if probe:
            try:await probe.detach()
            except Exception:pass

async def prepare_instagram_home(page,checkpoint=None):
    handled=[]
    await block_notification_prompt(page,checkpoint)
    for _ in range(4):
        if checkpoint:await checkpoint()
        if any(p in urlparse(page.url).path for p in ('/accounts/login','/challenge','/checkpoint','/two_factor','/suspended')):
            raise ValidationError('账号需要登录或验证，请在窗口内处理后继续')
        state=await page.evaluate(HOME_NOTICE)
        if state['state']=='ready':return handled
        if state['state'] in {'blocked','unknown'}:
            raise ValidationError('首页提示需手动处理，尚未发帖：'+state['message'])
        await page.locator('[data-juxin-dismiss-notice="true"]').click(timeout=3000)
        handled.append(state['message']);await asyncio.sleep(.2)
    raise ValidationError('首页提示未关闭，请在窗口内处理后继续')

# Shared candidate resolver supports collapsed icon-only navigation.
from .instagram_identity import OWN_LINK as OWN_PROFILE


PROFILE_COUNT = r'''username => {
 const heading=[...document.querySelectorAll('main header h1,main header h2,header h1,header h2,main h1,main h2')].find(el=>el.innerText?.trim().toLowerCase()===username);
 if(!heading)return null;
 const header=heading.closest('header')||heading.closest('main');
 const items=[...header.querySelectorAll('li,span,div')];
 for(const el of items){
  if(!el.getClientRects().length)continue;
  const text=el.innerText?.trim();if(!text||text.length>80)continue;
  let m=text.match(/^([\d.,\s]+(?:万|萬|千|[KMBkmb])?)\s*(?:posts?|帖子|則貼文|篇帖子|篇貼文|貼文)$/i);
  if(m){const exact=el.querySelector('[title]')?.getAttribute('title');return exact||m[1];}
 }
 return null;
}'''

PROFILE_RELATIONS = r'''username => {
 const heading=[...document.querySelectorAll('header h1,header h2,main h1,main h2')].find(el=>el.innerText?.trim().toLowerCase()===username);
 if(!heading)return {};
 const scope=heading.closest('header')||heading.closest('main');
 const result={};
 for(const [key,route,words] of [['followers_count','followers','followers?|粉丝|粉絲|位追蹤者|追蹤者'],['following_count','following','following|关注|關注|追蹤中']]){
   const links=[...scope.querySelectorAll('a[href]')].filter(a=>{try{return new URL(a.href,location.href).pathname.replace(/\/$/,'')==='/'+username+'/'+route}catch{return false}});
   for(const el of [...links,...scope.querySelectorAll('li')]){
     if(!el.getClientRects().length)continue;
     const text=el.innerText?.trim();if(!text||text.length>90)continue;
     const m=text.match(new RegExp('^([\\d.,\\s]+(?:万|萬|千|[KMBkmb])?)\\s*(?:'+words+')$','i'));
     if(m){result[key]=el.querySelector('[title]')?.getAttribute('title')||m[1];break;}
   }
 }
 return result;
}'''

async def read_account_posts(page,checkpoint=None):
    """Read a temporary own-profile tab; never navigate an operator's current tab."""
    snapshot={'username':'','posts_count':None,'followers_count':None,'following_count':None,'status':'unavailable','message':''};tab=None
    try:
        if checkpoint:await checkpoint()
        username=""
        for _ in range(12):
            if checkpoint:await checkpoint()
            username=await page.evaluate(OWN_PROFILE)
            if username:break
            await asyncio.sleep(.25)
        if not username:
            snapshot['message']='未识别当前登录账号的个人主页入口';return snapshot
        snapshot['username']=username
        tab=await page.context.new_page()
        await tab.goto('https://www.instagram.com/'+username+'/',wait_until='domcontentloaded',timeout=15000)
        await prepare_instagram_home(tab,checkpoint)
        from .playwright_worker import parse_visible_count
        for _ in range(12):
            if checkpoint:await checkpoint()
            raw=await tab.evaluate(PROFILE_COUNT,username)
            count=parse_visible_count(raw) if raw is not None else None
            if count is not None:
                snapshot.update(posts_count=count,status='ok',message='主页读取')
                relation_counts=await tab.evaluate(PROFILE_RELATIONS,username)
                if isinstance(relation_counts,dict):
                    for key in ('followers_count','following_count'):
                        raw_value=relation_counts.get(key)
                        snapshot[key]=parse_visible_count(raw_value) if raw_value is not None else None
                return snapshot
            await asyncio.sleep(.25)
        snapshot['message']='未读到当前账号的主页帖子数'
    except Exception as exc:snapshot['message']=str(exc)[:220]
    finally:
        if tab:
            try:
                await tab.close()
            except Exception:pass
    return snapshot
