import {BrowserWindow,ipcMain,session,type IpcMainInvokeEvent} from 'electron';
import {OptionalImmersiveSource,type ImmersiveSource} from './immersive-source.js';
import {createHash,randomUUID} from 'node:crypto';
import {join} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {immersiveBootstrap} from './immersive-bootstrap.js';
import {ImmersiveWorkerPool,ImmersiveWorkerInvalidatedError} from './immersive-pool.js';
import {ImmersiveStartupError,startupReason,startupStep} from './immersive-startup.js';
import {IMMERSIVE_WORLD,IMMERSIVE_VERSION,immersivePageConfig,immersiveHostConfig,immersiveConfigIdentity,immersivePartition,immersiveRequestUrl,immersiveRetryDelay,officialImmersiveUrl,ImmersiveRouting,type ImmersiveSettings} from './immersive-policy.js';
type Context={win:BrowserWindow;key:string;id:string;settings:boolean;live:()=>boolean;requests:Map<string,AbortController>;httpStatus:number;retryMs:number;injecting?:Promise<void>;startupFailure?:ImmersiveStartupError;phase:string};
const delay=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
/** The official unmodified userscript executes outside every account session.
 * Only plain chat text enters a temporary document; no account cookies, task
 * handles, composer nodes or Chromium extension permissions cross this boundary. */
export class ImmersiveTranslator {
 private windows=new Map<number,Context>();
 private routers=new Map<string,ImmersiveRouting>();
 private stores=new Map<string,Record<string,any>>();
 private statuses=new Map<string,{service:string;elapsed:number}>();
 private source?:string;
 private pool:ImmersiveWorkerPool<Context>;
 private settingsOpening=new Map<string,Promise<void>>();
 private epoch=0;
 private configWrites={userSettingsChanges:0,localSettingsChanges:0,runtimeOnlyWrites:0};
 private readonly root=fileURLToPath(new URL('../',import.meta.url));
 private readonly folder=join(this.root,'desktop','vendor','immersive-translate');
 private readonly fileUrl=pathToFileURL(join(this.folder,'host.html')).href;
 private timer:ReturnType<typeof setInterval>;
 constructor(private owner:()=>string|null,private parent:()=>BrowserWindow|null,private secret:(key:string)=>string|null,private save:(key:string,value:string)=>void,private readonly sourceProvider:ImmersiveSource=new OptionalImmersiveSource()){
  ipcMain.handle('immersive:storage',(e,input)=>this.storage(e,input));
  ipcMain.handle('immersive:request',(e,input)=>this.request(e,input));
  ipcMain.handle('immersive:abort',(e,id)=>{const c=this.context(e);c.requests.get(String(id))?.abort();return true});
  ipcMain.handle('immersive:open',async(e,url)=>{const c=this.context(e);if(typeof url!=='string'||!officialImmersiveUrl(url))throw new Error('请从插件官方设置中打开此页面');await this.settings(c.id,url);return true});
  this.pool=new ImmersiveWorkerPool(async(poolKey,live)=>{
   const [partition,id]=JSON.parse(poolKey);if(this.account(id)!==partition)throw new Error('登录状态已变化');
   return this.create(id,false,()=>this.owner()!==null&&this.account(id)===partition&&live());
  },c=>this.close(c));
  this.timer=setInterval(()=>{this.pool.prune();for(const c of this.windows.values())if(!c.live())this.close(c)},200);this.timer.unref();
 }
 private account(id:string){const owner=this.owner();if(!owner)throw new Error('登录状态已变化');return immersivePartition(owner,id)}
 private data(key:string){let data=this.stores.get(key);if(!data){try{data=JSON.parse(this.secret(key)||'{}')}catch{data={}};if(!data||Array.isArray(data)||typeof data!=='object')data={};this.stores.set(key,data!)}return data!}
 private context(event:IpcMainInvokeEvent){const c=this.windows.get(event.sender.id);if(!c||event.senderFrame!==event.sender.mainFrame||!c.live()||c.key!==this.account(c.id))throw new Error('翻译页面会话已失效');const url=event.sender.getURL();if(url!==this.fileUrl&&!officialImmersiveUrl(url))throw new Error('翻译页面来源无效');return c}
 private storage(event:IpcMainInvokeEvent,input:any){
  const c=this.context(event),data=this.data(c.key);
  if(!input||!['get','set','delete','list'].includes(input.op)||typeof input.key!=='string'||input.key.length>200||['__proto__','constructor','prototype'].includes(input.key))throw new Error('插件存储请求无效');
  if(input.op==='list')return Object.keys(data);
  if(input.op==='get'){
   const value=data[input.key];
   if(input.key==='fullLocalUserConfig')return c.settings?immersiveHostConfig(value):immersivePageConfig(value);
   return value;
  }
  const before=JSON.stringify(data[input.key]),next={...data};
  if(input.key.startsWith('__juxin'))throw new Error('插件存储键无效');
  if(input.op==='delete')delete next[input.key];
  else {const serialized=JSON.stringify(input.value);if(serialized===undefined||serialized.length>4000000)throw new Error('插件设置过大');next[input.key]=JSON.parse(serialized)}
  const configWrite=c.settings&&['fullLocalUserConfig','localConfig'].includes(input.key);
  const changed=configWrite&&immersiveConfigIdentity(input.key,data[input.key])!==immersiveConfigIdentity(input.key,next[input.key]);
  if(changed)next.__juxinConfigRevision=randomUUID();
  // A failed encrypted write must not mutate active settings or revoke workers.
  this.save(c.key,JSON.stringify(next));this.stores.set(c.key,next);
  // Diagnostics contain counters only: never config values, API keys or text.
  if(changed)this.configWrites[input.key==='fullLocalUserConfig'?'userSettingsChanges':'localSettingsChanges']++;
  else if(configWrite&&before!==JSON.stringify(next[input.key]))this.configWrites.runtimeOnlyWrites++;
  if(changed)this.pool.invalidate('['+JSON.stringify(c.key)+',');return true;
 }
 private configuredUrls(data:Record<string,any>){
  const urls:string[]=[];
  for(const root of [data.fullLocalUserConfig,data.localConfig])for(const service of Object.values(root?.translationServices||{}) as any[])for(const name of ['apiUrl','baseUrl'])if(typeof service?.[name]==='string')urls.push(service[name]);
  return urls;
 }
 private async request(event:IpcMainInvokeEvent,input:any){
  const c=this.context(event);
  if(!input||typeof input.id!=='string'||typeof input.url!=='string'||!immersiveRequestUrl(input.url,this.configuredUrls(this.data(c.key)))||!['GET','POST','PUT','DELETE','PATCH','HEAD'].includes(input.method)||typeof input.headers!=='object'||(input.data!==undefined&&(typeof input.data!=='string'||input.data.length>500000)))throw new Error('翻译网络请求无效');
  if(c.requests.size>=8)throw new Error('翻译请求过多');
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),Math.max(1000,Math.min(input.timeout||12000,20000)));c.requests.set(input.id,controller);
  try{
   const headers=new Headers(input.headers);let url=input.url,method=input.method,body=input.data,response:Response;
   for(let redirects=0;;redirects++){
    response=await c.win.webContents.session.fetch(url,{method,headers,body,signal:controller.signal,redirect:'manual'});
    if(![301,302,303,307,308].includes(response.status))break;
    if(redirects>=3)throw new Error('Too many redirects');
    const next=new URL(response.headers.get('location')||'',url);
    if(!immersiveRequestUrl(next.href,this.configuredUrls(this.data(c.key))))throw new Error('Redirect outside translator permissions');
    if(next.origin!==new URL(url).origin)for(const name of [...headers.keys()])if(/authorization|api.?key|cookie|token/i.test(name))headers.delete(name);
    if(response.status===303||([301,302].includes(response.status)&&method==='POST')){method='GET';body=undefined;headers.delete('content-type')}
    await response.body?.cancel();url=next.href;
   }
   if(!response.ok)c.httpStatus=response.status;
   if(response.status===429||response.status===503)c.retryMs=immersiveRetryDelay(response.headers.get('retry-after'));
   const reader=response.body?.getReader(),chunks:Uint8Array[]=[];let length=0;
   if(reader)for(;;){const r=await reader.read();if(r.done)break;length+=r.value.byteLength;if(length>4000000){await reader.cancel();throw new Error('Response too large')}chunks.push(r.value)}
   if(!c.live())throw new Error('Stale translator');
   return {status:response.status,statusText:response.statusText,responseHeaders:[...response.headers].map(([k,v])=>k+': '+v).join('\r\n'),url:response.url,text:Buffer.concat(chunks).toString('utf8')};
  }catch{throw new Error('沉浸式翻译连接失败，请检查系统网络或插件通道设置')}finally{clearTimeout(timer);c.requests.delete(input.id)}
 }
 private close(c:Context){if(c.win.isDestroyed())return;for(const r of c.requests.values())r.abort();c.requests.clear();this.windows.delete(c.win.webContents.id);if(!c.win.isDestroyed())c.win.destroy()}
 private async create(id:string,settings:boolean,live:()=>boolean){
  const key=this.account(id);this.source=this.sourceProvider.load();
  if(!live())throw new Error('翻译已暂停');
  const ses=session.fromPartition(key);ses.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));ses.setPermissionCheckHandler(()=>false);
  const win=new BrowserWindow({width:900,height:760,show:settings,parent:settings?this.parent()||undefined:undefined,title:'沉浸式翻译 · 当前账号',autoHideMenuBar:true,webPreferences:{session:ses,preload:join(this.root,'dist-electron','immersive-preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,backgroundThrottling:false}});
  const c:Context={win,key,id,settings,live,requests:new Map(),httpStatus:0,retryMs:30000,phase:'load'};this.windows.set(win.webContents.id,c);
  win.webContents.setWindowOpenHandler(({url})=>{if(officialImmersiveUrl(url))void this.settings(id,url).catch(()=>{});return {action:'deny'}});
  win.webContents.on('will-navigate',(e,url)=>{if(url!==this.fileUrl&&!officialImmersiveUrl(url))e.preventDefault()});
  win.webContents.on('will-redirect',(e,url)=>{if(!officialImmersiveUrl(url))e.preventDefault()});
  win.webContents.on('will-attach-webview',event=>event.preventDefault());
  const contentsId=win.webContents.id;win.on('closed',()=>{for(const r of c.requests.values())r.abort();this.windows.delete(contentsId)});
  win.webContents.on('preload-error',(_event,_path,error)=>{c.startupFailure=new ImmersiveStartupError('preload',startupReason(error))});
  win.webContents.on('render-process-gone',(_event,details)=>{c.startupFailure=new ImmersiveStartupError('renderer',details.reason)});
  win.webContents.on('did-fail-load',(_event,code,_description,_url,main)=>{if(main)c.startupFailure=new ImmersiveStartupError('load',String(code))});
  win.webContents.on('dom-ready',()=>{
   c.phase='inject';c.injecting=startupStep('inject',()=>this.inject(c)).catch(error=>{c.startupFailure=error;throw error});
   // The readiness path observes this rejection; this handler only prevents an
   // unhandled rejection before loadURL has settled.
   void c.injecting.catch(()=>{});
  });
  try{await startupStep('load',()=>win.loadURL(this.fileUrl),10000);await this.ready(c);return c}catch(error){
   const failure=c.startupFailure||error;
   console.error('IMMERSIVE_STARTUP_FAILED',JSON.stringify({mode:settings?'settings':'worker',phase:failure instanceof ImmersiveStartupError?failure.phase:c.phase,reason:startupReason(failure),revoked:!live(),destroyed:win.isDestroyed()}));
   if(settings)await this.hostStatus(c,failure instanceof Error?failure.message:'插件启动未完成，请关闭后重试。');else this.close(c);throw failure;
  }
 }
 private async inject(c:Context){
  if(c.win.isDestroyed()||!c.live())return;
  const url=c.win.webContents.getURL();if(url!==this.fileUrl&&!officialImmersiveUrl(url))return;
  const bridge=await c.win.webContents.executeJavaScriptInIsolatedWorld(IMMERSIVE_WORLD,[{code:'Boolean(globalThis.__juxinImmersiveBridge)'}]);
  if(!bridge)throw new ImmersiveStartupError('preload','bridge-missing');
  await c.win.webContents.executeJavaScriptInIsolatedWorld(IMMERSIVE_WORLD,[{code:`if(!globalThis.__juxinImmersiveVendorStarted){globalThis.__juxinImmersiveVendorStarted=true;(${immersiveBootstrap.toString()})();\n${this.source!}\n};void 0;`}]);
 }
 private async command(c:Context,input:any){if(c.win.isDestroyed()||!c.live())throw new Error('翻译已暂停');return startupStep('runtime-command',()=>c.win.webContents.executeJavaScriptInIsolatedWorld(IMMERSIVE_WORLD,[{code:`globalThis.__juxinImmersiveCommand?.(${JSON.stringify(input)})`}]),4000)}
 private async hostStatus(c:Context,message:string){if(!c.win.isDestroyed())await startupStep('status-display',()=>c.win.webContents.executeJavaScriptInIsolatedWorld(IMMERSIVE_WORLD,[{code:`{const n=document.querySelector('#juxin-status');if(n)n.textContent=${JSON.stringify(message)}}` }]),2000).catch(()=>{})}
 private async openPanel(c:Context){
  c.win.show();c.win.focus();await this.ready(c);
  const until=Date.now()+8000;
  while(Date.now()<until){
   const panel=await this.command(c,{kind:'panel'});
   if(panel?.visible&&panel.controls>0){await this.hostStatus(c,'插件面板已展开，可选择通道；登录或 API 配置也可使用“完整插件设置”。');return}
   await this.command(c,{kind:'popup'});await delay(150);
  }
  await this.hostStatus(c,'插件面板没有展开，请点击“完整插件设置”继续配置。');
  throw new Error('插件面板未展开，可在已打开窗口中点击“完整插件设置”');
 }
 private async ready(c:Context){
  if(c.startupFailure)throw c.startupFailure;
  if(!c.injecting)throw new ImmersiveStartupError('inject','document-not-ready');
  await c.injecting;c.phase='ready';
  const until=Date.now()+15000;while(Date.now()<until){
   if(c.startupFailure)throw c.startupFailure;
   if(!c.live()||c.win.isDestroyed())throw new ImmersiveStartupError('ready','revoked');
   if((await startupStep('ready',()=>this.command(c,{kind:'status'}),Math.min(3000,until-Date.now())))?.ready){c.phase='running';return}await delay(100);
  }throw new ImmersiveStartupError('ready','vendor-not-ready');
 }
 private async acquireWorker(id:string,lang:string,service:string,current:()=>boolean){
  const partition=this.account(id),live=()=>current()&&this.owner()!==null&&this.account(id)===partition;
  return this.pool.acquireCurrent(()=>JSON.stringify([partition,id,this.cacheIdentity(id),lang,service]),live);
 }
 diagnostics(){return {pool:this.pool.stats(),configWrites:{...this.configWrites},windows:[...this.windows.values()].map(c=>({mode:c.settings?'settings':'worker',phase:c.phase,destroyed:c.win.isDestroyed(),failure:c.startupFailure?{phase:c.startupFailure.phase,reason:c.startupFailure.reason}:undefined}))}}
 cacheIdentity(id:string){const data=this.data(this.account(id));return createHash('sha256').update(JSON.stringify([IMMERSIVE_VERSION,data.__juxinConfigRevision||'initial'])).digest('hex')}
 status(id:string){return {...this.statuses.get(this.account(id)),...this.sourceProvider.status()}}
 async settings(id:string,url?:string){
  this.sourceProvider.load();
  if(url&&!officialImmersiveUrl(url))throw new Error('插件设置地址无效');
  const key=this.account(id),epoch=this.epoch,live=()=>epoch===this.epoch&&this.owner()!==null&&this.account(id)===key;
  const previous=this.settingsOpening.get(key)||Promise.resolve();
  const pending=previous.catch(()=>{}).then(async()=>{
   if(!live())throw new Error('翻译页面会话已失效');
   let c=[...this.windows.values()].find(item=>item.key===key&&item.settings&&!item.win.isDestroyed());
   if(c?.startupFailure){this.close(c);c=undefined}
   if(!c)c=await this.create(id,true,live);
   if(!live()){this.close(c);throw new Error('翻译页面会话已失效')}
   c.win.show();c.win.focus();
   if(url){try{await startupStep('options-load',()=>c!.win.loadURL(url),15000)}catch{throw new Error('完整插件设置加载失败，请检查网络后重试')}return}
   if(c.win.webContents.getURL()!==this.fileUrl){c.startupFailure=undefined;c.injecting=undefined;await startupStep('load',()=>c!.win.loadURL(this.fileUrl),10000)}
   await this.openPanel(c);
  });
  this.settingsOpening.set(key,pending);
  try{await pending}finally{if(this.settingsOpening.get(key)===pending)this.settingsOpening.delete(key)}
 }
 async translate(id:string,text:string,lang:string,settings:ImmersiveSettings,current:()=>boolean,onResult?:(text:string)=>Promise<void>){
  this.sourceProvider.load();
  if(!text.trim()||text.length>5000)throw new Error('单条翻译最多 5000 字');
  const key=this.account(id);let router=this.routers.get(key);if(!router){router=new ImmersiveRouting();this.routers.set(key,router)}
  const candidates=router.choose(settings);if(!candidates.length)throw Object.assign(new Error('所选翻译通道冷却中，可等待或在设置中切换通道'),{retryAfterMs:router.nextDelay(settings)});
  const tried=new Set<string>();let retryAfterMs=3000;
  let message='翻译失败，请打开沉浸式翻译设置检查通道';let requiresConfiguration=false;
  for(const service of candidates){
   if(service!=='plugin'&&tried.has(service))continue;
   if(!current())throw new Error('翻译已暂停');
   let c:Context|undefined,actual=service,lease:Awaited<ReturnType<ImmersiveWorkerPool<Context>['acquire']>>|undefined,reusable=false;const started=Date.now();
   try{
    lease=await this.acquireWorker(id,lang,service,current);c=lease.value;c.httpStatus=0;c.retryMs=30000;
    actual=service==='plugin'?(await this.command(c,{kind:'status'}))?.service||'plugin':service;
    if(tried.has(actual)){reusable=true;continue}tried.add(actual);
    const job=randomUUID();await this.command(c,{kind:'translate',id:job,text,lang,service});
    const until=Date.now()+22000;
    while(Date.now()<until){
     if(!current())throw new Error('翻译已暂停');
     const result=await this.command(c,{kind:'result',id:job});
     if(result?.text&&typeof result.text==='string'&&result.text.length<=30000){router.success(service,Date.now()-started);this.statuses.set(key,{service,elapsed:Date.now()-started});reusable=true;await onResult?.(result.text).catch(()=>{});return result.text as string}
     if(result?.error||c.httpStatus>=400)throw new Error('插件返回翻译错误');await delay(80);
    }
    throw new Error('翻译超时');
   }catch(error){
    if(!current())throw new Error('翻译已暂停');
    // Startup/configuration failures happen before any translation request.
    // Keep their reason and avoid a false 30-second provider cooldown.
    const invalidated=c&&!c.live();
    if(!lease||invalidated||error instanceof ImmersiveStartupError||error instanceof ImmersiveWorkerInvalidatedError)throw Object.assign(invalidated?new ImmersiveWorkerInvalidatedError():error instanceof Error?error:new ImmersiveStartupError('worker','execution-failed'),{retryAfterMs:3000});
    requiresConfiguration=c?.httpStatus===401||c?.httpStatus===403;
    router.failure(service,c?.retryMs);if(actual!==service)router.failure(actual,c?.retryMs);retryAfterMs=Math.max(retryAfterMs,c?.retryMs||30000);
    message=c?.httpStatus===429?'沉浸式翻译通道限流，请切换通道或稍后重试':c?.httpStatus===401||c?.httpStatus===403?'沉浸式翻译通道拒绝访问，请在插件设置中检查登录、额度或密钥':'沉浸式翻译未返回译文，请打开插件设置检查通道或首次使用提示';
   }finally{
    if(c&&lease){if(reusable&&current()){try{reusable=Boolean((await this.command(c,{kind:'clear'}))?.cleared)}catch{reusable=false}}else reusable=false;lease.release(reusable)}
   }
  }
  throw Object.assign(new Error(message),{retryAfterMs,requiresConfiguration});
 }
 retry(id:string){this.statuses.delete(this.account(id))} // Explicit retries still respect provider cooldowns.
 reset(){this.epoch++;this.settingsOpening.clear();this.pool.invalidate();for(const c of [...this.windows.values()])this.close(c);this.routers.clear();this.stores.clear();this.statuses.clear();this.configWrites={userSettingsChanges:0,localSettingsChanges:0,runtimeOnlyWrites:0}}
 stop(){clearInterval(this.timer);this.reset();for(const name of ['immersive:storage','immersive:request','immersive:abort','immersive:open'])ipcMain.removeHandler(name)}
}
