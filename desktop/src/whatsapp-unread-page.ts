type Count={count:number;capped:boolean};

/** Navigation counts conversations. Only individual chat badges count messages. */
export function sumWhatsAppMessages(rows:Count[],conversations:Count|null,ambiguous=false):Count|null{
 if(conversations?.count===0&&!conversations.capped)return {count:0,capped:false};
 if(!rows.length)return null;
 const count=rows.reduce((total,row)=>total+row.count,0);
 if(!Number.isSafeInteger(count)||count>1_000_000)return null;
 return {count,capped:ambiguous||rows.some(row=>row.capped)||!conversations||conversations.capped||rows.length!==conversations.count};
}

/** Runs in the isolated page without scrolling, clicking, or reading message text. */
export function whatsAppMessageCounts(conversations:Count|null,sum:typeof sumWhatsAppMessages):Count|null{
 const visible=(e:Element)=>Boolean(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';
 const parse=(value:string)=>{const match=/^(\d[\d,，]*)(\+)?$/.exec(value.trim());if(!match)return null;const count=Number(match[1].replace(/[,，]/g,''));return Number.isSafeInteger(count)&&count>0&&count<=1_000_000?{count,capped:Boolean(match[2])}:null};
 const semantic=(value:string)=>{
  const m=/(\d[\d,，]*\+?)\s*(?:unread(?: messages?)?|条?未读(?:消息)?)/i.exec(value)||/(?:unread(?: messages?)?|未读(?:消息)?)\s*[:：,，]?\s*(\d[\d,，]*\+?)/i.exec(value);
  return m?parse(m[1]):null;
 };
 const byRow=new Map<Element,Count[]>();let ambiguous=false;
 for(const root of document.querySelectorAll('#pane-side,[data-testid="chat-list"]')){
  for(const node of root.querySelectorAll('[aria-label],[title],[data-testid*="unread"],[data-icon*="unread"]')){
   if(!visible(node))continue;
   const description=[node.getAttribute('aria-label'),node.getAttribute('title')].filter(Boolean).join(' ');
   const marker=[node.getAttribute('data-testid'),node.getAttribute('data-icon')].filter(Boolean).join(' ');
   if(!/unread|未读/i.test(description+' '+marker))continue;
   // Prefer the containing row over its nested wrappers. The same badge can
   // appear in aria-label, title and text, and must contribute only once.
   const row=node.closest('[role="row"],[role="listitem"]')||node.closest('[data-testid="cell-frame-container"]');
   if(!row||!root.contains(row)){ambiguous=true;continue}
   const values=byRow.get(row)||[];byRow.set(row,values);
   const count=semantic(description)||parse(node.textContent||'');
   if(count)values.push(count);
  }
 }
 const rows:Count[]=[];
 for(const values of byRow.values()){
  if(!values.length){ambiguous=true;continue}
  // Prefer an exact accessible value over a truncated visual badge.
  const exact=values.filter(v=>!v.capped),candidates=exact.length?exact:values;
  const first=candidates[0];
  if(candidates.some(v=>v.count!==first.count||v.capped!==first.capped)||values.some(v=>v.capped&&v.count>=first.count&&exact.length)){ambiguous=true;continue}
  rows.push(first);
 }
 return sum(rows,conversations,ambiguous);
}
