import {app,session,WebContentsView,type Session,type WebContents} from 'electron';
import {whatsappSessionPath,whatsappErrorText} from './whatsapp-session.js';
import {accountUserAgent} from './account-user-agent.js';
import {whatsappColumnLayoutScript} from './whatsapp-layout.js';

export async function createWhatsAppSession(owner:string,profile:string,proxy:string){
  const ses=session.fromPath(whatsappSessionPath(app.getPath('userData'),owner,profile));
  await ses.setProxy(proxy?{proxyRules:proxy}:{mode:'direct'});
  // Keep Chromium's storage checks. Do not apply Instagram's blanket permission
  // check handler to service workers/OPFS in this independent profile.
  ses.setPermissionCheckHandler(null);
  ses.setPermissionRequestHandler((_wc,permission,callback,details)=>{
    let firstParty=false;try{firstParty=new URL(details.requestingUrl).origin==='https://web.whatsapp.com'}catch{}
    callback(firstParty&&['persistent-storage','storage-access'].includes(permission));
  });
  ses.setUserAgent(accountUserAgent(ses.getUserAgent()),'zh-CN,zh;q=0.9,en;q=0.8');
  return ses;
}
export function createWhatsAppView(ses:Session){
  const view=new WebContentsView({webPreferences:{session:ses,sandbox:true,contextIsolation:true,nodeIntegration:false,webSecurity:true,allowRunningInsecureContent:false,webviewTag:false,navigateOnDragDrop:false,backgroundThrottling:false,spellcheck:false}});
  view.setBackgroundColor('#0b1016');view.setBounds({x:0,y:0,width:1100,height:850});
  return view;
}
/** Read-only snapshot; no replacement of page APIs, Worker, IndexedDB or fetch. */
export function whatsappPageState(){
  if(location.origin!=='https://web.whatsapp.com')return {phase:'other-page',chatReady:false};
  const chatReady=!!document.querySelector('#pane-side,[data-testid="chat-list"],#main footer [contenteditable="true"]');
  // Chat messages are not login errors. Avoid forcing a full document layout
  // and reading every message on each translation/unread poll.
  if(chatReady)return {phase:'chat-ready',chatReady:true,readyState:document.readyState,viewport:{width:innerWidth,height:innerHeight}};
  const text=(document.body?.textContent||'').slice(0,30000);
  const visible=(e:Element)=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
  const storageError=/数据库错误|database error|database initialization failed/i.test(text);
  const qr=[...document.querySelectorAll('canvas')].some(e=>visible(e)&&e.width>=150&&e.height>=150);
  const phone=/使用电话号码登录|link with phone number|请改用电话号码/i.test(text);
  return {phase:storageError?'storage-error':chatReady?'chat-ready':qr?'qr-candidate':phone?'phone-login':'loading',chatReady:chatReady&&!storageError,
    qrCandidate:qr,phoneLogin:phone,readyState:document.readyState,
    viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight}};
}
export class WhatsAppPageRuntime{
  readonly events:Array<Record<string,unknown>>=[];
  state:Record<string,any>={phase:'created',chatReady:false};
  private opening?:Promise<any>;
  private reading?:Promise<any>;
  private disposed=false;
  private navigation=0;
  private layoutNavigation=-1;
  private inspectedAt=0;
  private firstFailure?:Record<string,unknown>;
  private workerLog:(_event:Electron.Event,details:any)=>void;
  constructor(readonly contents:WebContents){
    const wc=contents;
    this.workerLog=(_event,d)=>{if(String(d.sourceUrl||'').startsWith('https://web.whatsapp.com/'))this.record('worker-error',{level:d.level,message:whatsappErrorText(d.message),source:whatsappErrorText(d.sourceUrl),line:d.lineNumber})};
    const workers=wc.session.serviceWorkers;
    workers.on('console-message',this.workerLog);
    wc.on('console-message',(_event,level,message,line,source)=>{if(level>=2)this.record('page-error',{level,message:whatsappErrorText(message),source:whatsappErrorText(source),line})});
    wc.on('did-start-navigation',(_event,url,inPlace,main)=>{if(main&&!inPlace){this.invalidateInspection('loading');this.record('navigation-start',{source:whatsappErrorText(url)})}});
    wc.on('did-navigate',(_event,url,status)=>{
      if(status>=400)this.invalidateInspection('network-error');
      this.record('navigation-commit',{source:whatsappErrorText(url),status});
    });
    wc.on('did-fail-load',(_event,code,reason,url,main)=>{if(main&&code!==-3){this.invalidateInspection('network-error');this.record('load-error',{code,message:whatsappErrorText(reason),source:whatsappErrorText(url)})}});
    wc.on('render-process-gone',(_event,d)=>{this.invalidateInspection('renderer-error');this.record('renderer-error',{reason:d.reason,exitCode:d.exitCode})});
    wc.once('destroyed',()=>{this.disposed=true;this.invalidateInspection('closed');workers.removeListener('console-message',this.workerLog)});
  }
  private invalidateInspection(phase:string){
    // A read belongs to one document/renderer. Keep its rejection handled, but
    // let the next document inspect independently if the old IPC never settles.
    this.navigation++;this.reading=undefined;this.inspectedAt=0;this.layoutNavigation=-1;
    this.state={phase,chatReady:false};
  }
  record(kind:string,details:Record<string,unknown>){const row={at:new Date().toISOString(),kind,...details};if(!this.firstFailure&&/UnknownError|database|数据库|SQLITE_|IndexedDB|QuotaExceeded|SecurityError|backing store/i.test(String(details.message||'')))this.firstFailure=row;this.events.push(row);if(this.events.length>120)this.events.shift()}
  async inspect(force=false){
    if(this.disposed||this.contents.isDestroyed())return {phase:'closed',chatReady:false};
    // An HTTP/Chromium error document is not a fresh login page. Keep the
    // failure until a real main-document navigation starts; otherwise routine
    // status polling can erase it and make same-URL explicit retry a no-op.
    if(['renderer-error','network-error'].includes(this.state.phase))return this.state;
    if(!force&&this.state.chatReady&&Date.now()-this.inspectedAt<750)return this.state;
    let timer:ReturnType<typeof setTimeout>|undefined;
    if(!this.reading){
      const navigation=this.navigation,inspection=this.contents.executeJavaScriptInIsolatedWorld(1004,[{code:`(${whatsappPageState.toString()})()`}]);
      const reading:Promise<any>=inspection.then(state=>{
        if(!this.disposed&&navigation===this.navigation){
          this.state=state;this.inspectedAt=Date.now();
          if(state.chatReady&&this.layoutNavigation!==navigation){
            this.layoutNavigation=navigation;
            void this.contents.executeJavaScriptInIsolatedWorld(1005,[{code:whatsappColumnLayoutScript()}]).catch(error=>{
              if(!this.disposed&&navigation===this.navigation)this.layoutNavigation=-1;
              this.record('layout-error',{message:whatsappErrorText(error)});
            });
          }
        }
        return this.state;
      },error=>{this.record('inspection-error',{message:whatsappErrorText(error)});return this.state}).finally(()=>{if(this.reading===reading)this.reading=undefined});
      this.reading=reading;
    }
    try{return await Promise.race([this.reading,new Promise(resolve=>{timer=setTimeout(()=>resolve({...this.state,inspectionPending:true}),1800)})])}finally{if(timer)clearTimeout(timer)}
  }
  /** Commit ends the shell wait. Slow resources keep running; no stop(),
   * readiness guessing, automatic reload loop or Instagram preview helper. */
  open(url:string,action:'open'|'refresh'|'back'|'forward'='open'):Promise<any>{
    if(this.opening)return this.opening;
    if(this.disposed||this.contents.isDestroyed())return Promise.reject(new Error('WhatsApp 窗口已关闭'));
    const wc=this.contents;
    if(action==='open'&&wc.getURL()===url&&!['renderer-error','network-error'].includes(this.state.phase))return Promise.resolve({page_loaded:true,message:'WhatsApp 窗口已打开，登录状态以页面为准。'});
    if(action==='back'&&!wc.navigationHistory.canGoBack()||action==='forward'&&!wc.navigationHistory.canGoForward())return Promise.resolve({page_loaded:true,navigated:false});
    this.opening=new Promise(resolve=>{
      let settled=false;
      const done=(result:any)=>{if(settled)return;settled=true;clearTimeout(timer);wc.removeListener('did-navigate',committed);wc.removeListener('did-navigate-in-page',inPage);wc.removeListener('did-fail-load',failed);wc.removeListener('render-process-gone',crashed);wc.removeListener('destroyed',closed);resolve(result)};
      const committed=(_event:Electron.Event,_url:string,status:number)=>done({page_loaded:status<400,http_status:status,message:status>=400?`WhatsApp 返回 HTTP ${status}`:'WhatsApp 页面已打开，正在加载登录内容。'});
      const inPage=(_event:Electron.Event,_url:string,main:boolean)=>{if(main)done({page_loaded:true})};
      const failed=(_event:Electron.Event,code:number,message:string,_url:string,main:boolean)=>{if(main&&code!==-3)done({page_loaded:false,message:'WhatsApp 网络加载失败：'+whatsappErrorText(message)})};
      const crashed=()=>done({page_loaded:false,message:'WhatsApp 页面进程已退出，请刷新窗口重试。'});
      const closed=()=>done({page_loaded:false,message:'WhatsApp 窗口已关闭'});
      const timer=setTimeout(()=>{this.record('navigation-pending',{});done({page_loaded:true,loading:true,message:'WhatsApp 仍在加载，请等待或导出检查结果。'})},15000);
      wc.on('did-navigate',committed);wc.on('did-navigate-in-page',inPage);wc.on('did-fail-load',failed);wc.on('render-process-gone',crashed);wc.on('destroyed',closed);
      const launchFailed=(error:unknown)=>{if(!settled&&!String(error).includes('ERR_ABORTED')){this.invalidateInspection('network-error');done({page_loaded:false,message:'WhatsApp 网络加载失败：'+whatsappErrorText(error)})}};
      try{
        if(action==='refresh')wc.reload();else if(action==='back')wc.navigationHistory.goBack();else if(action==='forward')wc.navigationHistory.goForward();
        else void wc.loadURL(url).catch(launchFailed);
      }catch(error){launchFailed(error)}
    }).finally(()=>{this.opening=undefined});
    return this.opening;
  }
  async report(){return {runtime:'whatsapp-independent-v1',state:await this.inspect(true),firstFailure:this.firstFailure||null,events:this.events.slice(),debuggerAttached:!this.contents.isDestroyed()&&this.contents.debugger.isAttached()}}
}
