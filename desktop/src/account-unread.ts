import {messageActivityPage} from './message-activity-page.js';
import {sumWhatsAppMessages,whatsAppMessageCounts} from './whatsapp-unread-page.js';
type UnreadCount={count:number;capped:boolean};
/** Conversation count is a coverage check, never the displayed message total. */
export function chooseWhatsAppUnread(filters:UnreadCount[],navigation:UnreadCount[],title:UnreadCount|null,ambiguous=false):UnreadCount|null {
 const agree=(values:UnreadCount[])=>values.every(v=>v.count===values[0].count&&v.capped===values[0].capped)?values[0]:null;
 if(filters.length)return agree(filters);
 const positive=navigation.filter(v=>v.count>0);
 if(positive.length)return agree(positive);
 if(ambiguous)return null;
 if(navigation.length)return agree(navigation);
 return title;
}

/** Evaluated in an isolated world. Never clicks or scrolls an account page. */
export const unreadPageScript = `(() => { const read = () => {
  const host=location.hostname;
  const platform=/^(www\\.)?instagram\\.com$/.test(host)?'instagram':host==='web.whatsapp.com'?'whatsapp':null;
  const unknown={count:null,capped:false,status:'unavailable',platform};
  if(!platform||location.protocol!=='https:')return unknown;
  const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';
  if(document.querySelector('input[type=password]')||/\\/(accounts\\/login|checkpoint|challenge|login)(\\/|$)/.test(location.pathname))return {...unknown,status:'signed_out'};
  if(platform==='whatsapp'&&!document.querySelector('#pane-side,[data-testid="chat-list"],#main footer [contenteditable="true"]')&&/(数据库错误|database error)/i.test((document.body?.textContent||'').slice(0,30000)))return {...unknown,status:'storage_error'};
  const label=e=>[e.getAttribute('aria-label'),e.getAttribute('title')].filter(Boolean).join(' ').trim();
  const isMessage=s=>/^(messages?|messenger|direct|inbox|chats?|消息|私信|聊天|收件箱)(?:$|[\\s,，(（:：]|\\d)/i.test(s);
  if(platform==='whatsapp') {
    const choose=${chooseWhatsAppUnread.toString()};
    const parseNumber=s=>{const m=/^(\\d[\\d,，]*)(\\+)?$/.exec(s.trim());if(!m)return null;const count=Number(m[1].replace(/[,，]/g,''));return Number.isSafeInteger(count)&&count<=1000000?{count,capped:!!m[2]}:null};
    const filters=[],navigation=[];let ambiguous=false;
    const controls=[...document.querySelectorAll('button,[role=button],[role=tab],a[href],[role=link]')].filter(e=>visible(e)&&!e.closest('#pane-side,[data-testid="chat-list"],#main,[role=row],[data-testid="cell-frame-container"]'));
    for(const e of controls){
      // New WhatsApp puts the unread conversation count on its filter pill.
      for(const value of [label(e),e.textContent||'']){
        if(/^(?:Unread|未读)$/i.test(value.trim())&&!/\\d/.test((e.textContent||'')+' '+label(e))&&!e.querySelector('[data-testid*=unread],[aria-label*=unread]')){filters.push({count:0,capped:false});break}
        const match=/^(?:Unread|未读)\\s*[:：,，(（]?\\s*(\\d[\\d,，]*\\+?)\\s*[)）]?$/i.exec(value.trim());
        if(match){const n=parseNumber(match[1]);if(n)filters.push(n);break}
      }
      const icon=[...e.querySelectorAll('[data-icon],svg[aria-label],svg title')].some(x=>/^chats?(?:-(?:outline|filled))?$/.test(x.getAttribute('data-icon')||'')||isMessage(label(x)||x.textContent||''));
      if(!isMessage(label(e))&&!icon)continue;
      let found=false;
      for(const node of [e,...e.querySelectorAll('span,div,[aria-label],[title]')]){
        if(!visible(node))continue;
        const description=label(node);
        const semantic=/(\\d[\\d,，]*\\+?)\\s*(?:unread(?: messages?| chats?)?|条?未读(?:消息|聊天)?)/i.exec(description)||/(?:unread(?: messages?| chats?)?|未读(?:消息|聊天)?)\\s*[:：,，]?\\s*(\\d[\\d,，]*\\+?)/i.exec(description);
        const count=(!node.children.length?parseNumber(node.textContent||''):null)||(semantic?parseNumber(semantic[1]):null);
        if(count){navigation.push(count);found=true}
      }
      if(!found){if(e.querySelector('[data-testid*=unread],[aria-label="未读"],[aria-label="Unread"]'))ambiguous=true;else navigation.push({count:0,capped:false})}
    }
    const title=/^\\s*\\((\\d[\\d,，]*\\+?)\\)\\s*WhatsApp(?:\\s|$)/i.exec(document.title);
    const conversations=choose(filters,navigation,title?parseNumber(title[1]):null,ambiguous);
    const value=(${whatsAppMessageCounts.toString()})(conversations,(${sumWhatsAppMessages.toString()}));
    return value?{...value,status:'live',platform}:unknown;
  }
  const candidates=[];
  for(const e of document.querySelectorAll('a[href],button,[role=link],[role=button]')) {
    let path='';try{const u=new URL(e.getAttribute('href')||'',location.href);if(u.origin===location.origin)path=u.pathname}catch{}
    const exact=platform==='instagram'?/^\\/direct\\/inbox\\/?$/.test(path):false;
    const icon=[...e.querySelectorAll('svg[aria-label],svg title,[data-icon]')].some(x=>isMessage(label(x)||x.textContent||'')||(platform==='whatsapp'&&/^chats?(-outline)?$/.test(x.getAttribute('data-icon')||'')));
    if((exact||isMessage(label(e))||icon)&&visible(e))candidates.push(e);
  }
  if(!candidates.length)return unknown;
  const values=[];
  const parse=s=>{const m=/^(\\d[\\d,，]*)(\\+)?$/.exec(s.trim());if(!m)return null;const n=Number(m[1].replace(/[,，]/g,''));return Number.isSafeInteger(n)&&n<=1000000?{count:n,capped:!!m[2]}:null};
  for(const entry of candidates) {
    let found=null;
    for(const e of [entry,...entry.querySelectorAll('span,div,[aria-label]')]) {
      if(!visible(e))continue;
      const text=e.children.length?null:parse(e.textContent||'');
      const semantic=/(\\d[\\d,，]*\\+?)\\s*(?:unread (?:messages?|chats?)|条?未读(?:消息|聊天))/i.exec(label(e));
      const n=text||(semantic?parse(semantic[1]):null);
      if(n){if(found&&(found.count!==n.count||found.capped!==n.capped))return unknown;found=n}
    }
    if(!found&&entry.querySelector('[data-testid*=unread],[aria-label="未读"],[aria-label="Unread"]'))return unknown;
    values.push(found||{count:0,capped:false});
  }
  if(values.some(v=>v.count!==values[0].count||v.capped!==values[0].capped))return unknown;
  return {...values[0],status:'live',platform};
}; const result=read();
if(result.status==='live'){try{result.activity=(${messageActivityPage.toString()})();}catch{}}
return result;})()`;

export type UnreadValue = {count:number|null;capped:boolean;status:string;platform:string|null;observed_at:string|null};
type RecordValue = UnreadValue & {owner:string;profile:string};
export class AccountUnreadCache {
  private rows = new Map<string, RecordValue>();
  observe(owner:string,profile:string,input:any) {
    const old=this.rows.get(profile);
    const valid=input?.status==='live' && Number.isSafeInteger(input.count) && input.count>=0 && input.count<=1_000_000;
    const reset=input?.status==='signed_out';
    const retained=!reset&&old?.owner===owner&&(!input?.platform||old.platform===input.platform)?old:undefined;
    this.rows.delete(profile);
    if(this.rows.size>=10000)this.rows.delete(this.rows.keys().next().value!);
    this.rows.set(profile,{owner,profile,count:valid?input.count:retained?.count??null,capped:valid?input.capped===true:retained?.capped??false,
      platform:input?.platform||retained?.platform||null,status:valid?'live':reset?'signed_out':input?.status==='storage_error'?'storage_error':retained?.count!=null?'stale':'unavailable',observed_at:valid?new Date().toISOString():reset?null:retained?.observed_at??null});
  }
  invalidate(profile:string) { this.rows.delete(profile); }
  snapshot(owner:string,opened:Set<string>) {
    return Object.fromEntries([...this.rows.values()].filter(r=>r.owner===owner).map(({owner:_owner,profile,...r})=>[profile,{...r,status:!opened.has(profile)?'closed':r.status==='live'&&Date.now()-Date.parse(r.observed_at||'')>10_000?'stale':r.status}]));
  }
}
