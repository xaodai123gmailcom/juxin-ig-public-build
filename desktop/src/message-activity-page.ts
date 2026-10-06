/** Read-only observation. Only an opaque revision leaves the isolated page. */
export function messageActivityPage(){
 const g=globalThis as any;
 const state=g.__juxinMessageActivityV1||(g.__juxinMessageActivityV1={epoch:crypto.randomUUID(),serial:0,previews:new Map(),tails:new Map()});
 const wa=location.hostname==='web.whatsapp.com';
 const hash=(value:string)=>{let h=2166136261;for(let i=0;i<value.length;i++){h^=value.charCodeAt(i);h=Math.imul(h,16777619)}return (h>>>0).toString(16)};
 const visible=(e:Element)=>Boolean(e.getClientRects().length);
 const unread=(row:Element)=>Array.from(row.querySelectorAll('[aria-label],[data-testid]')).some(e=>/unread|未读/i.test((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('data-testid')||'')));
 const rows=Array.from(document.querySelectorAll(wa?'#pane-side [role="row"],#pane-side [data-testid="cell-frame-container"]':'a[href^="/direct/t/"]'));
 const next=new Map();
 for(const row of rows){
  if(!visible(row)||!unread(row)||row.querySelector('[data-icon="msg-check"],[data-icon="msg-dblcheck"]'))continue;
  const identity=wa?row.querySelector('[title]')?.getAttribute('title'):row.getAttribute('href');
  const texts=Array.from(row.querySelectorAll<HTMLElement>('[dir="auto"]')).filter(e=>!e.querySelector('[dir="auto"]')).map(e=>e.innerText.trim()).filter(t=>/\p{L}/u.test(t));
  const preview=texts.at(-1);if(!identity||!preview||/^(typing|recording|正在输入|正在录音)/i.test(preview))continue;
  const key=hash(identity),value=hash(preview),old=state.previews.get(key);next.set(key,value);
  if(old!==undefined&&old!==value)state.serial++;
 }
 state.previews=next;
 // A new incoming bubble in an open WhatsApp chat can be marked read instantly.
 // Require the previous tail to remain before it; scrolling/reloading history
 // or entering another chat establishes a baseline and never makes a toast.
 if(wa){
  const main=document.querySelector('#main'),header=main?.querySelector('header');
  // Presence text changes independently of the selected conversation. Prefer
  // a title that actually labels its node; keep the legacy fallback for an
  // unknown DOM instead of inventing a contact identity from toolbar tooltips.
  const title=Array.from(header?.querySelectorAll?.('[title]')||[]).find(node=>{
   const value=node.getAttribute('title')?.trim();return value&&value===node.textContent?.trim();
  });
  const conversation=title?.getAttribute('title')?.trim()||header?.textContent;
  if(main&&conversation){
   const bubbles=Array.from(main.querySelectorAll<HTMLElement>('.message-in'));
   const ids=bubbles.map(e=>e.closest('[data-id]')?.getAttribute('data-id')).filter((id):id is string=>Boolean(id));
   const key=hash(conversation),history=state.tails.get(key)||{last:'',seen:new Set()},last=ids.at(-1);
   if(last&&!history.seen.has(last)){
    if(history.last&&ids.includes(history.last))state.serial++;
    history.last=last;
   }
   for(const id of ids)history.seen.add(id);
   while(history.seen.size>1000)history.seen.delete(history.seen.values().next().value);
   state.tails.set(key,history);
   while(state.tails.size>100)state.tails.delete(state.tails.keys().next().value);
  }
 }
 return {epoch:state.epoch,serial:state.serial};
}
