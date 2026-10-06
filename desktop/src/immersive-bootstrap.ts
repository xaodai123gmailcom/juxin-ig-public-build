/** Isolated-world userscript host. Runs only in the dedicated translator window. */
export function immersiveBootstrap(){
 const g=globalThis as any,bridge=g.__juxinImmersiveBridge;
 if(!bridge||g.__juxinImmersiveRuntime)return;
 const changes=new Map<number,{key:string;callback:Function}>(),menus=new Map<string,Function>();let serial=0;
 const state={ready:false,service:'',error:'',job:'',pending:0,lastText:'',changedAt:0};g.__juxinImmersiveRuntime=state;
 const request=(o:any)=>{
  const id=String(++serial),job=state.job;let aborted=false;if(job)state.pending++;
  const task=bridge.request({id,url:o.url,method:o.method||'GET',headers:o.headers||{},data:typeof o.data==='string'?o.data:undefined,timeout:Math.min(Number(o.timeout)||12000,20000)}).then(async(r:any)=>{
   if(aborted)return;const response:any={...r,readyState:4,responseText:r.text,response:r.text,finalUrl:r.url};
   if(o.responseType==='json'){try{response.response=JSON.parse(r.text)}catch{response.response=null}}
   if(o.responseType==='arraybuffer')response.response=new TextEncoder().encode(r.text).buffer;
   if(o.responseType==='blob')response.response=new Blob([r.text]);
   if(o.responseType==='stream'){const bytes=new TextEncoder().encode(r.text);response.response=new ReadableStream({start(c){c.enqueue(bytes);c.close()}});await o.onloadstart?.(response)}
   await o.onreadystatechange?.(response);await o.onload?.(response);return response;
  }).catch(()=>{if(!aborted)o.onerror?.({status:0,statusText:'Translation connection failed'});return undefined}).finally(()=>{if(job&&job===state.job)state.pending--});
  return Object.assign(task,{abort(){aborted=true;void bridge.abort(id);o.onabort?.({status:0})}});
 };
 const notify=(key:string,old:any,value:any,remote:boolean)=>{for(const item of changes.values())if(item.key===key)item.callback(key,old,value,remote)};
 const storage={getValue:async(k:string,d:any)=>{const v=await bridge.storage('get',k);return v===undefined?d:v},setValue:async(k:string,v:any)=>{const old=await bridge.storage('get',k);await bridge.storage('set',k,v);notify(k,old,v,false)},deleteValue:async(k:string)=>{await bridge.storage('delete',k)},listValues:()=>bridge.storage('list','')};
 g.GM={...storage,info:{script:{name:'Immersive Translate',version:'1.33.1'}},xmlHttpRequest:request,
  addValueChangeListener:(key:string,callback:Function)=>{const id=++serial;changes.set(id,{key,callback});return id},removeValueChangeListener:(id:number)=>changes.delete(id),
  registerMenuCommand:(name:string,fn:Function)=>{menus.set(name,fn);return name},unregisterMenuCommand:(name:string)=>menus.delete(name),
  addStyle:(css:string)=>{const s=document.createElement('style');s.textContent=css;(document.head||document.documentElement).append(s);return s},
  addElement:(parent:any,tag:any,attrs:any)=>{if(typeof parent==='string'){attrs=tag;tag=parent;parent=document.head||document.documentElement}const node=document.createElement(tag);for(const [k,v] of Object.entries(attrs||{})){if(k==='textContent')node.textContent=String(v);else node.setAttribute(k,String(v))}parent.append(node);return node},
  openInTab:(url:string)=>{void bridge.open(url);return {close(){},closed:false}}
 };
 const tell=(type:string,data?:any,id?:string)=>document.dispatchEvent(new CustomEvent('immersiveTranslateDocumentMessageThirdPartyTell',{detail:JSON.stringify({type,data,id})}));
 const query=(type:string)=>new Promise<any>((resolve,reject)=>{
  const id='juxin-query-'+(++serial),name='immersiveTranslateDocumentMessageTellThirdParty';
  const done=(value:any)=>{clearTimeout(timer);document.removeEventListener(name,listener);resolve(value)};
  const listener=(event:any)=>{try{const r=JSON.parse(event.detail);if(r.id===id&&r.type===type)done(r.payload)}catch{}};
  const timer=setTimeout(()=>{document.removeEventListener(name,listener);reject(new Error('插件没有确认页面状态'))},1500);
  document.addEventListener(name,listener);tell(type,undefined,id);
 });
 const panel=()=>{
  const root=document.querySelector('#immersive-translate-browser-popup')?.shadowRoot;
  const content=root?.querySelector('.popup-container') as HTMLElement|null;
  const rect=content?.getBoundingClientRect(),controls=content?.querySelectorAll('button,select,input,[role="button"]').length||0;
  const visible=Boolean(content&&rect&&rect.width>100&&rect.height>100&&getComputedStyle(content).visibility!=='hidden');
  return {mounted:Boolean(root?.querySelector('#mount')),visible,controls};
 };
 document.addEventListener('immersiveTranslateDocumentMessageTellThirdParty',(event:any)=>{try{const r=JSON.parse(event.detail);if(r.type==='getAsyncTranslationMeta'&&r.id==='juxin-ready'){state.ready=true;state.service=r.payload?.translationService||''}}catch{}});
 document.addEventListener('immersiveTranslateDocumentMessagePluginReady',()=>tell('getAsyncTranslationMeta',undefined,'juxin-ready'));
 const readyTimer=setInterval(()=>{if(state.ready)clearInterval(readyTimer);else tell('getAsyncTranslationMeta',undefined,'juxin-ready')},100);
 setTimeout(()=>clearInterval(readyTimer),15000);
 g.__juxinImmersiveCommand=(input:any)=>{
  if(input.kind==='status')return {ready:state.ready,service:state.service,menus:menus.size};
  if(input.kind==='panel')return panel();
  if(input.kind==='popup'){if(!panel().mounted)return {opened:false};tell('openPopup',{style:'top: 24px; left: 50%; transform: translateX(-50%); max-height: calc(100vh - 48px); overflow: auto;',overlayStyle:'background-color: transparent;'});return {opened:panel().visible}}
  if(input.kind==='clear')return (async()=>{
   tell('restorePage');const until=Date.now()+2000;
   while(Date.now()<until){if(await query('getPageStatusAsync')==='Original'){document.querySelector('#juxin-messages')?.replaceChildren();state.job='';state.pending=0;state.lastText='';state.changedAt=0;return {cleared:true}}await new Promise(r=>setTimeout(r,30))}
   throw new Error('插件没有清理上一条消息');
  })();
  if(input.kind==='translate'){
   document.querySelector('#juxin-status')?.remove();document.querySelector('#juxin-host-actions')?.remove();document.documentElement.removeAttribute('lang');document.title='Chat';
   const root=document.querySelector('#juxin-messages');if(!root)throw new Error('Missing translator document');
   root.replaceChildren();const item=document.createElement('section'),p=document.createElement('p');item.id='juxin-job';p.className='juxin-source';p.textContent=input.text;item.append(p);root.append(item);state.job=input.id;state.lastText='';state.changedAt=0;
   tell('translatePage',{...(input.service==='plugin'?{}:{translationService:input.service}),targetLanguage:input.lang,translationMode:'dual',translationStartMode:'immediate',trigger:'user'});
   return {started:true};
  }
  if(input.kind==='result'){
   if(input.id!==state.job)return {stale:true};
   const root=document.querySelector('#juxin-job');
   const labels=Array.from(root?.querySelectorAll('.immersive-translate-target-inner')||[]);
   if(labels.length){const text=labels.map(n=>n.textContent||'').join('\n').trim();if(text!==state.lastText){state.lastText=text;state.changedAt=Date.now()}if(text&&!state.pending&&Date.now()-state.changedAt>=240)return {text}}
   if(root?.querySelector('[data-immersive-translate-error-id]'))return {error:true};
   return {pending:true};
  }
 };
 // The host buttons are usable even if the vendor popup fails to mount.
 const showStatus=(text:string)=>{const node=document.querySelector('#juxin-status');if(node)node.textContent=text};
 document.querySelector('#juxin-open-panel')?.addEventListener('click',()=>{g.__juxinImmersiveCommand({kind:'popup'});showStatus('正在展开插件面板；若未显示，可点击“完整插件设置”。')});
 document.querySelector('#juxin-open-options')?.addEventListener('click',()=>{void bridge.open('https://dash.immersivetranslate.com/#general').catch(()=>showStatus('插件设置页面打开失败，请检查网络后重试。'))});
}
