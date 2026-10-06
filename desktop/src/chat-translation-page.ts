/** Runs only in the isolated world of the selected, unlocked account page. */
export function chatTranslationPage(input:any):any {
 const wa=location.hostname==='web.whatsapp.com';
 const ig=['instagram.com','www.instagram.com'].includes(location.hostname)&&location.pathname.startsWith('/direct/');
 if(!wa&&!ig)return {messages:[],status:'请打开 Instagram 或 WhatsApp 的具体聊天'};
 const g=globalThis as any,key='__juxinChatTranslationV2';
 const state=g[key]||(g[key]={serial:0,generation:0,records:new Map(),nodes:new WeakMap(),settings:{},until:0,conversation:'',pending:null});
 state.documentId??=crypto.randomUUID();
 state.generation??=0;
 const suspend=()=>{
  state.generation++;
  if(state.expiry)clearTimeout(state.expiry);state.expiry=undefined;
  if(state.scroll&&state.root)state.root.removeEventListener('scroll',state.scroll,true);
  state.observer?.disconnect();state.observer=null;state.scroll=null;state.until=0;state.pending=null;state.dirty=true;
 };
 // The host may release only this adapter before handing the page to a task.
 // Expiry also covers a hidden account that will no longer receive polls.
 state.suspend=suspend;
 const shown=(el:Element)=>Boolean(el.getClientRects().length);
 const ownedSelector='[data-juxin-chat-translation],[data-juxin-chat-draft],[data-juxin-chat-status]';
 // WhatsApp repeats selectable text inside reply previews. Those nodes are
 // context for a different message, not additional bodies of this message.
 // Use semantic quote markers, not obfuscated layout classes or bubble color.
 const waQuoteSelector='[data-testid="quoted-message"],[data-testid="quoted-msg"],[data-testid="reply-preview"],[class*="quoted-mention"],[aria-label*="Quoted message" i],[aria-label*="引用消息"],[aria-label*="引用的消息"]';
 const waNonBodySelector=waQuoteSelector+',time,[data-testid="msg-meta"],[data-testid="msg-time"],[data-testid="author"],[data-testid="msg-author"]';
 const waNonBody=(node:Element)=>Boolean(node.closest(waNonBodySelector));
 const discardRecord=(id:string,r:any)=>{r.label?.remove();r.statusLabel?.remove();state.records.delete(id)};
 const composer=()=>Array.from(document.querySelectorAll<HTMLElement>(wa?'#main footer [contenteditable="true"][role="textbox"], #main footer [contenteditable="true"]':'[contenteditable="true"][role="textbox"],[contenteditable="true"][data-lexical-editor="true"],textarea[placeholder]')).find(el=>shown(el)&&!el.closest('[role="dialog"],nav,aside'));
 const box=composer();
 // Instagram also renders bubbles without role=row, and may give the entire
 // inbox (including contact previews) one main element. Scope to the composer
 // column, then the nearest common ancestor containing actual message text.
 const igSelector='[dir="auto"],[data-lexical-text="true"],[role="row"] [role="gridcell"] div,[role="row"] [role="gridcell"] span,[role="row"] [role="gridcell"] p';
 const igCandidate=(node:HTMLElement)=>{
  if(!shown(node)||node.closest(ownedSelector+',nav,aside,header,button,a,[role="toolbar"],[role="textbox"],[role="dialog"],[contenteditable="true"],time,h1,h2,h3')||node.querySelector(igSelector))return false;
  const value=node.innerText.trim();
  if(!node.matches('[dir="auto"],[data-lexical-text="true"]')&&!Array.from(node.childNodes).some(n=>n.nodeType===Node.TEXT_NODE&&/\p{L}/u.test(n.textContent||'')))return false;
  if(!/\p{L}/u.test(value)||/^(?:https?:\/\/|www\.)\S+$/i.test(value)||/^(?:\d{1,4}[\/.-]){1,2}\d{1,4}(?:\s+\d{1,2}:\d{2})?$/.test(value))return false;
  if(box){const r=node.getBoundingClientRect(),b=box.getBoundingClientRect(),center=(r.left+r.right)/2;
   if(center<b.left-24||center>b.right+24||r.bottom>b.top+4)return false;
  }
  return true;
 };
 let root:Element|null=wa?document.querySelector('#main'):null;
 if(ig&&box){
  if(state.root?.isConnected&&state.root.contains(box)&&state.conversation.startsWith(location.pathname+'|'))root=state.root;
  for(let parent=root?null:box.parentElement;parent;parent=parent.parentElement){
   if(Array.from(parent.querySelectorAll<HTMLElement>(igSelector)).some(igCandidate)){root=parent;break}
  }
  root||=box.closest('main,[role="main"]');
 }
 const header=wa?root?.querySelector('header'):null;
 // A contact title is stable while header presence/typing text changes. Only
 // trust an explicit title that names its own text; unknown layouts retain the
 // previous conservative fallback rather than guessing localized status words.
 const titled=header?Array.from(header.querySelectorAll?.('[title]')||[]).find(node=>{
  const title=node.getAttribute('title')?.trim();return Boolean(title&&title===node.textContent?.trim());
 }):undefined;
 const conversation=location.pathname+'|'+(wa?titled?.getAttribute('title')?.trim()||header?.textContent||'':location.pathname);
 const text=(node:HTMLElement)=>node instanceof HTMLTextAreaElement?node.value:node.innerText.trim();
 const appearance=(label:HTMLElement)=>{label.style.color=state.settings.color;label.style.fontSize=(state.settings.fontSize??12)+'px'};
 if(state.conversation!==conversation||state.root!==root){
  suspend();state.records.clear();state.currentRecords=[];
  document.querySelectorAll('[data-juxin-chat-translation],[data-juxin-chat-draft],[data-juxin-chat-status]').forEach(n=>n.remove());state.conversation=conversation;state.root=root;
 }
 if(input.kind==='poll'){
  const previous=JSON.stringify([state.settings.engine,state.settings.provider,state.settings.incomingLang,state.settings.immersiveMode,state.settings.immersiveService,state.settings.immersiveFallbacks,state.settings.translationRevision]);
  const next=JSON.stringify([input.settings.engine,input.settings.provider,input.settings.incomingLang,input.settings.immersiveMode,input.settings.immersiveService,input.settings.immersiveFallbacks,input.settings.translationRevision]);
  if(previous!==next){state.generation++;document.querySelectorAll('[data-juxin-chat-translation],[data-juxin-chat-status]').forEach(n=>n.remove());state.records.clear();state.currentRecords=[];state.dirty=true}
  state.settings={...input.settings,outgoing:false};state.until=Date.now()+1800;
  const appearanceKey=state.settings.color+'|'+(state.settings.fontSize??12);
  if(state.appearanceKey!==appearanceKey){
   state.appearanceKey=appearanceKey;
   // Include older connected annotations outside the current message batch.
   document.querySelectorAll<HTMLElement>('[data-juxin-chat-translation]').forEach(appearance);
  }
  if(!input.settings.enabled){suspend();document.querySelectorAll('[data-juxin-chat-translation],[data-juxin-chat-draft],[data-juxin-chat-status]').forEach(n=>n.remove());return {messages:[],status:'同步翻译已关闭'}}
  if(root&&!state.observer){
   const owned=(n:Node)=>{const e=n.nodeType===Node.ELEMENT_NODE?n as Element:n.parentElement;return Boolean(e?.closest('[data-juxin-chat-translation],[data-juxin-chat-draft],[data-juxin-chat-status]'))};
   state.observer=new MutationObserver(changes=>{if(changes.some(change=>!owned(change.target)&&(change.type!=='childList'||[...change.addedNodes,...change.removedNodes].some(n=>!owned(n)))))state.dirty=true});
   state.observer.observe(root,{childList:true,subtree:true,characterData:true});
   state.scroll=()=>{state.dirty=true};root.addEventListener('scroll',state.scroll,true);state.dirty=true;
  }
  if(root&&!state.expiry){
   const expire=()=>{state.expiry=undefined;const remaining=state.until-Date.now();if(remaining>0)state.expiry=setTimeout(expire,remaining);else suspend()};
   state.expiry=setTimeout(expire,1800);
  }
 }
 const generation=state.documentId+':'+state.generation;
 let records:any[]=state.currentRecords||[];
 // Fast polling picks up new messages. A stable
 // conversation reuses its DOM index; applying one result never rescans history.
 if(input.kind!=='apply'&&(state.dirty||input.kind==='export'||Date.now()-(state.scannedAt||0)>2000)){
 if(wa){
  // Also repair annotations made before this exclusion, including records for
  // nodes that React moved/reused in a quote. Only our own annotations go away.
  for(const [id,r] of state.records)if(waNonBody(r.node))discardRecord(id,r);
  root?.querySelectorAll<HTMLElement>(ownedSelector).forEach(label=>{if(waNonBody(label))label.remove()});
 }
 const nodes=root?Array.from(root.querySelectorAll<HTMLElement>(wa?'.message-in .selectable-text.copyable-text,.message-out .selectable-text.copyable-text,[data-pre-plain-text] .selectable-text':igSelector)).filter(n=>wa?!waNonBody(n)&&!n.closest(ownedSelector+',nav,header,[contenteditable="true"],button,a')&&!n.parentElement?.closest('.selectable-text'):igCandidate(n)).slice(-160):[];
 records=[];
 for(const node of nodes.slice(-80)){
  if(!shown(node))continue;
  const original=text(node);if(!original||original.length>5000)continue;
  let id=state.nodes.get(node);if(!id){id='message-'+(++state.serial);state.nodes.set(node,id)}
  let r=state.records.get(id);if(!r||r.original!==original){r?.label?.remove();r?.statusLabel?.remove();r={id,original,node,conversation};state.records.set(id,r)}records.push(r);
 }
 for(const [id,r] of state.records)if(!r.node.isConnected||r.conversation!==conversation||state.records.size>500)state.records.delete(id);
 state.currentRecords=records;state.dirty=false;state.scannedAt=Date.now();
 }
 if(input.kind==='apply'){
  if(!state.settings.enabled||Date.now()>=state.until)return {applied:false};
  if(input.conversation!==conversation)return {applied:false};
  // Core may already be processing this request when the language/channel or
  // active adapter changes. Fence both success and error at the actual write.
  if(input.generation!==generation)return {applied:false};
  if(input.draft)return {applied:false};
  const r=state.records.get(input.id);if(!r||!r.node.isConnected)return {applied:false};
  if(wa&&waNonBody(r.node)){discardRecord(input.id,r);state.currentRecords=(state.currentRecords||[]).filter((row:any)=>row.id!==input.id);return {applied:false}}
  if(text(r.node)!==input.original)return {applied:false};
  if(input.error){
   r.retry=Number(input.retryAt)||Date.now()+30000;
   if(!r.statusLabel?.isConnected){r.statusLabel=document.createElement('div');r.statusLabel.dataset.juxinChatStatus='true';r.node.insertAdjacentElement('afterend',r.statusLabel)}
   r.statusLabel.style.cssText='font:12px/1.5 sans-serif;padding:4px 0;color:#fbbf24;white-space:normal;overflow-wrap:anywhere';
   r.statusLabel.textContent=input.retryable?'翻译暂缓，将在服务恢复后自动重试':String(input.status||'翻译失败，请查看翻译设置');
   return {applied:false};
  }
  r.statusLabel?.remove();r.retry=0;
  r.translation=String(input.translation);r.lang=state.settings.incomingLang;
  if(!r.label?.isConnected){r.label=document.createElement('div');r.label.dataset.juxinChatTranslation='true';r.node.insertAdjacentElement('afterend',r.label)}
  r.label.style.cssText='display:block;flex-basis:100%;min-width:0;max-width:100%;font:12px/1.5 sans-serif;padding:4px 0;white-space:pre-wrap;overflow-wrap:anywhere';appearance(r.label);
  r.label.textContent=r.translation;return {applied:true};
 }
 if(input.kind==='export')return {conversation,messages:records.map(r=>({original:r.original,translation:r.translation||''}))};
 // The main process owns retry deadlines. Keep jobs discoverable so a cached
 // result can be applied even while the shared online channel is cooling down.
 const pending=null;
 const untranslated=records.filter(r=>(!r.translation||!r.label?.isConnected||r.lang!==state.settings.incomingLang));
 const inViewport=(r:any)=>{
  const bounds=r.node.getBoundingClientRect();let top=0,bottom=box?.getBoundingClientRect().top??innerHeight;
  for(let p=r.node.parentElement;p&&p!==root?.parentElement;p=p.parentElement){if(/auto|scroll|hidden|clip/.test(getComputedStyle(p).overflowY)){const b=p.getBoundingClientRect();top=Math.max(top,b.top);bottom=Math.min(bottom,b.bottom)}}
  return bounds.bottom>top&&bounds.top<bottom;
 };
 // Never drain offscreen history after finishing the visible chat. A scroll
 // exposes the next messages; the newest visible messages get the first slot.
 const queue=untranslated.filter(inViewport).slice(-12).reverse();
 return {conversation,generation,messages:queue.map(r=>({id:r.id,original:r.original,generation})),draft:pending,status:!box?'请打开具体聊天，等待消息和输入框加载':!records.length&&!pending?'当前聊天尚未识别到可翻译的文字消息':''};
}
