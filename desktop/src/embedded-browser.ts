import {createWhatsAppSession,createWhatsAppView,WhatsAppPageRuntime} from './whatsapp-page.js';
import {whatsappSessionPath,whatsappErrorText} from './whatsapp-session.js';
import {whatsappStorageProbe,diagnosticCodes} from './whatsapp-diagnostics.js';
import {statfs} from 'node:fs/promises';
import {app} from 'electron';
import {TaskWatchShield} from './task-watch-shield.js';
/** Real account pages owned by Electron. No external Chrome process or HWND. */
import { BrowserWindow, WebContentsView, View, session, Menu, clipboard, type Session, type Rectangle, type WebContents } from 'electron';
import { createServer, type Server } from 'node:http';
import { randomBytes } from 'node:crypto';
import { WebSocketServer, WebSocket } from 'ws';
import { AccountCdpConnection } from './embedded-cdp.js';
import { loadProfilePreview, profilePreviewUrl } from './profile-preview.js';
import { accountUserAgent } from './account-user-agent.js';
import { navigateAccountPage } from './account-page-navigation.js';
import { accountStoragePermission } from './account-permissions.js';
import { AccountUnreadCache, unreadPageScript } from './account-unread.js';
import {accountViewport} from './account-viewport.js';
import {webPageMenu,contextMessage} from './web-page-menu.js';
import {whatsappColumnLayoutScript} from './whatsapp-layout.js';

export type AccountPage = { view: WebContentsView; targetId: string; role?:'source'|'screening'|'task'; roleIndex?:number; openerId?: string; postingViewport?: Pick<Rectangle,'width'|'height'> };
type PageDisplayState = 'blank'|'loading'|'ready'|'load_failed'|'crashed'|'unresponsive';
type PageDisplay = { display_state:PageDisplayState;display_message:string };
import {chatTranslationPage} from './chat-translation-page.js';
import {chatSettings} from './chat-translation.js';
export type EmbeddedProfile = {
  id: string; owner: string; name?:string; proxy: string; generation: number; capability: string;
  session: Session; pages: Map<string, AccountPage>; selected?: string;
  clients: Set<AccountCdpConnection>; closed: boolean; previewTarget?: string; diagnostics?:Array<Record<string,unknown>>;
  lastSurface?:{at:string;bounds:Rectangle;readOnly:boolean;shield:boolean};
  manualOnly?:boolean;
};
const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
export function permittedPageUrl(value: unknown): value is string {
  if (value === 'about:blank') return true;
  if (typeof value !== 'string') return false;
  try { return ['https:', 'http:', 'blob:'].includes(new URL(value).protocol); } catch { return false; }
}
export class EmbeddedBrowserHost {
  readonly token = randomBytes(32).toString('hex');
  url = '';
  readonly profiles = new Map<string, EmbeddedProfile>();
  private server?: Server;
  private sockets = new WebSocketServer({ noServer: true, maxPayload: 32 * 1024 * 1024 });
  private generations = new Map<string, number>();
  private attached?: AccountPage;
  private panes = new WeakMap<BrowserWindow, View>();
  private pageReady = new WeakSet<WebContents>();
  private pageDisplay = new WeakMap<WebContents, PageDisplay>();
  private pageUnresponsive = new WeakSet<WebContents>();
  private viewportSizes = new WeakMap<WebContentsView, string>();
  private viewportRequests = new WeakMap<WebContentsView, {promise:Promise<unknown>}>();
  private nativeBounds = new WeakMap<View, string>();
  private renderedReadOnly = false;
  private surfaceRaised = false;
  private inputFocus?:WebContents;
  private focusRestorePending=false;
  private focusIntruder?:WebContents;
  private workspaceBounds?: Rectangle;
  attachWindow(win: BrowserWindow) {
    if (this.panes.has(win)) return;
    // Electron 35 places BrowserWindow.webContents beside contentView, below
    // its children. contentView is the overlay container, NOT the shell page.
    // Keep that native root stable; only the account pane needs visibility and
    // stacking changes. A hidden pane still owns the background task views.
    const pane = new View(); pane.setBorderRadius(0); pane.setVisible(false);
    this.panes.set(win, pane); win.contentView.addChildView(pane);
    const resize = () => { if (!win.isDestroyed()) { this.hide(); this.layoutPane(win); } };
    resize(); win.on('resize', resize);
    win.webContents.on('focus',()=>{this.inputFocus=win.webContents});
    win.webContents.on('did-start-navigation', (_event, _url, _inPlace, main) => { if (main) this.hide(); });
    win.webContents.on('did-navigate-in-page', (_event, _url, main) => { if (main) this.hide(); });
  }
  private visible?: { profile: string; bounds: Rectangle; deadline: number; target?:string;readOnly?:boolean };
  private surfaceGrant = '';
  private translationPages=new Set<string>();
  private whatsappPages=new WeakMap<WebContentsView,WhatsAppPageRuntime>();
  chatTranslationTarget(){
    const v=this.visible,p=v&&this.profiles.get(v.profile);
    if(!v||v.readOnly||Date.now()>v.deadline||!p||p.closed||p.clients.size)return;
    const page=p.pages.get(v.target||p.selected||'');
    if(!page||page.view.webContents.isDestroyed())return;
    return p;
  }
  requestSurface() { return this.surfaceGrant = randomBytes(16).toString('hex'); }
  hasSurfaceGrant(grant: string) { return Boolean(grant && this.surfaceGrant === grant); }
  private expiry?: ReturnType<typeof setInterval>;
  private opening = new Map<string, Promise<EmbeddedProfile>>();
  private closing = new WeakMap<EmbeddedProfile, Promise<void>>();
  private retiringProfiles = new Map<string, number>();
  private resetting = new Set<string>();
  private stopping = false;
  private pendingPages = new Map<EmbeddedProfile, Set<WebContentsView>>();
  private cancelPendingPage = new WeakMap<WebContentsView, ()=>void>();
  private sessionWrites = new WeakMap<EmbeddedProfile, Set<Promise<void>>>();
  readonly unread = new AccountUnreadCache();
  onSurfaceRendered?:()=>void;
  onEscape?:()=>boolean;
  onPageNavigation?:(action:string)=>Promise<unknown>;
  private pageMenu?:Menu;
  private pageMenuEpoch=0;
  onUnread?:(profile:EmbeddedProfile,value:any)=>void;
  onUnreadReset?:(profile:string)=>void;
  onPageExit?:(details:{reason:string;exitCode:number;taskClients:number;pages:number})=>void;
  private unreadTimer?: ReturnType<typeof setInterval>;
  private readingUnread = new WeakMap<WebContents, object>();
  private pageEpoch = new WeakMap<WebContents, number>();
  private activeUnread = 0;
  private unreadDue = new WeakMap<EmbeddedProfile,number>();
  private watchedSessions = new WeakSet<Session>();
  constructor(private window: () => BrowserWindow | null) {}
  cacheActivity() {
    const opened=new Set<string>(),busy=new Set<string>();
    for(const p of this.profiles.values())if(!p.closed){opened.add(p.id);if(p.clients.size)busy.add(p.id)}
    for(const id of this.opening.keys())busy.add(id);
    for(const id of this.resetting)busy.add(id);
    for(const p of this.profiles.values())if(p.closed)busy.add(p.id);
    return {opened,busy};
  }
  private diagnostic(id:string,value:Record<string,unknown>){const p=this.profiles.get(id);if(!p)return;const rows=p.diagnostics||(p.diagnostics=[]);rows.push({at:new Date().toISOString(),...value});if(rows.length>50)rows.shift()}
  recordConnectionFailure(p: EmbeddedProfile,error: unknown) {
    this.diagnostic(p.id,{kind:'connection-cleanup',message:whatsappErrorText(error)});
  }
  async start() {
    this.server = createServer(async (req, res) => {
      res.setHeader('content-type', 'application/json');
      if (req.method !== 'POST' || req.url !== '/rpc' || req.headers.origin || req.headers.authorization !== `Bearer ${this.token}`) {
        res.writeHead(403).end('{}'); return;
      }
      try {
        let data = '';
        for await (const chunk of req) { data += chunk; if (data.length > 65536) throw new Error('请求过大'); }
        const { method, ...body } = JSON.parse(data);
        const result = await this.control(method, body);
        res.end(JSON.stringify(result));
      } catch (error) { res.writeHead(409).end(JSON.stringify({ error: error instanceof Error ? error.message : '内置窗口操作失败' })); }
    });
    this.server.on('upgrade', (req, socket, head) => {
      // A random per-account capability, never the global Electron debugger.
      const profile = [...this.profiles.values()].find(p => !p.closed && req.url === `/devtools/browser/${p.capability}`);
      if (this.stopping || !profile || profile.manualOnly || req.headers.origin) { socket.destroy(); return; }
      this.sockets.handleUpgrade(req, socket, head, async ws => {
        ws.pause();
        // Stop only adapters actually installed on these pages. A stalled
        // renderer must not leave the Playwright handshake waiting forever.
        let preparationTimer:ReturnType<typeof setTimeout>|undefined;
        const preparation=Promise.all([...profile.pages.values()].map(async page=>{
          const wc=page.view.webContents;if(wc.isDestroyed())return;
          if(this.translationPages.has(page.targetId))await wc.executeJavaScriptInIsolatedWorld(1002,[{code:"if(globalThis.__juxinChatTranslationV2){const state=globalThis.__juxinChatTranslationV2;if(state.suspend)state.suspend();else{state.until=0;state.pending=null}}"}]);
          if(this.columnReady.has(wc))await wc.executeJavaScriptInIsolatedWorld(1005,[{code:"globalThis.__juxinChatColumnsV2?.setEnabled(false)"}]);
        }));
        const prepared=await Promise.race([preparation.then(()=>true,()=>false),new Promise<boolean>(resolve=>{preparationTimer=setTimeout(()=>resolve(false),2000)})]);
        if(preparationTimer)clearTimeout(preparationTimer);
        if(this.stopping||!prepared||profile.closed||ws.readyState!==WebSocket.OPEN){
          if(!prepared)this.diagnostic(profile.id,{kind:'task-prepare',reason:'page-adapter-stop-timeout'});
          ws.terminate();return;
        }
        const client = new AccountCdpConnection(this, profile, ws);
        if(!profile.clients.size)this.resetTaskPageViewports(profile);
        profile.clients.add(client);
        if(this.visible?.profile===profile.id){this.pageMenuEpoch++;this.pageMenu?.closePopup();this.pageMenu=undefined}
        for(const page of profile.pages.values())this.columnReady.delete(page.view.webContents);
        const win=this.window();if(win&&!win.isDestroyed())this.layoutPane(win);
        ws.on('close', () => {
          // A disconnected socket is not proof that its CDP/cookie work has
          // finished. Keep the window occupied until real cleanup settles.
          void client.dispose().then(()=>{profile.clients.delete(client);if(!profile.clients.size)this.resetTaskPageViewports(profile)},error=>this.recordConnectionFailure(profile,error));
        });
        ws.resume();
      });
    });
    await new Promise<void>((resolve, reject) => { this.server!.once('error', reject); this.server!.listen(0, '127.0.0.1', resolve); });
    const address = this.server.address();
    if (!address || typeof address === 'string') throw new Error('内置窗口服务启动失败');
    this.url = `http://127.0.0.1:${address.port}`;
    this.expiry = setInterval(() => { if (this.visible && Date.now() > this.visible.deadline) this.hide(); }, 500);
    this.expiry.unref();
    this.unreadTimer = setInterval(() => this.scheduleUnread(), 500);
    this.unreadTimer.unref();
  }
  endpoint(p: EmbeddedProfile) { return this.url.replace('http:', 'ws:') + `/devtools/browser/${p.capability}`; }
  async control(method: string, body: Record<string, any>): Promise<any> {
    if(this.stopping)throw new Error('软件正在退出，窗口已停止接受操作');
    if(method==='confirm-closed'){
      if(!new RegExp(`^native:${uuid}$`,'i').test(body.profile)||!new RegExp(`^${uuid}$`,'i').test(body.owner))throw new Error('内置账号标识无效');
      // A closed flag or an omitted inventory row is not an acknowledgement:
      // closeProfile retains its profile until pages, transports and storage
      // have finished, and opening may not have published a profile yet.
      if(this.profiles.has(body.profile)||this.opening.has(body.profile)||this.resetting.has(body.profile)
        ||this.retiringProfiles.has(body.profile))throw new Error('窗口尚未完全关闭，清理占用保留；请确认没有执行中任务后正常退出并重新打开软件，再核验清理');
      return {closed:true,profile_id:body.profile,owner_user_id:body.owner,verification:'desktop-absence-v1'};
    }
    if(method==='open-instagram'){
      // Core holds the exclusive account operation lease. Ordinary opens do
      // not start a Playwright driver or a second profile-statistics page.
      const previous=this.profiles.get(body.profile);
      if(previous&&(previous.owner!==body.owner||previous.clients.size))throw new Error('账号不属于当前用户或任务占用');
      const p=await this.ensure(body.profile,body.owner,body.proxy||'',true,body.name);
      if(p.closed||p.clients.size||p.owner!==body.owner)throw new Error('窗口或任务状态已变化');
      const pages=[...p.pages.values()].filter(page=>!page.view.webContents.isDestroyed());
      const isInstagram=(page:AccountPage)=>{try{return ['instagram.com','www.instagram.com'].includes(new URL(page.view.webContents.getURL()).hostname)}catch{return false}};
      const selected=p.pages.get(p.selected||'');
      const page=(selected&&isInstagram(selected)?selected:pages.find(isInstagram))||pages.find(page=>page.view.webContents.getURL()==='about:blank')||await this.newPage(p,'about:blank');
      if(p.closed||p.clients.size)throw new Error('窗口或任务状态已变化');
      for(const idle of pages){idle.role=undefined;idle.roleIndex=undefined;delete idle.postingViewport}
      const status=this.pageDisplayStatus(page);
      if(!isInstagram(page)||['load_failed','crashed','unresponsive'].includes(status.display_state)){
        this.pageDisplay.set(page.view.webContents,{display_state:'loading',display_message:'正在加载 Instagram…'});
        void page.view.webContents.loadURL('https://www.instagram.com/').catch(()=>{});
      }
      this.activate(p,page);
      const display=this.pageDisplayStatus(page);
      // The workspace retains this one-shot command acknowledgement as a
      // banner. A transient about:blank/task display notice must not outlive
      // navigation there; live loading/errors come from the surface heartbeat.
      return {opened:true,page_loaded:display.display_state==='ready'?true:null,navigation_pending:display.display_state!=='ready',message:'Instagram 窗口已打开',ws:this.endpoint(p),generation:p.generation};
    }
    if(method==='open-whatsapp'){
      const url=new URL(body.url);
      if(url.origin!=='https://web.whatsapp.com'||url.username||url.password)throw new Error('WhatsApp 页面地址无效');
      if(body.cookies!=null&&(!Array.isArray(body.cookies)||body.cookies.some((c:any)=>typeof c.name!=='string'||typeof c.value!=='string'||!/^\.?([a-z0-9-]+\.)*whatsapp\.com$/i.test(c.domain||''))))throw new Error('WhatsApp Cookie 范围无效');
      const previous=this.profiles.get(body.profile);
      if(previous&&(previous.owner!==body.owner||previous.clients.size))throw new Error('账号不属于当前用户或任务占用');
      // Recreate only the page when upgrading an old open view; keep the same
      // persistent Session and all login data. Never clear storage implicitly.
      if(previous&&!previous.manualOnly)await this.closeProfile(previous);
      const p=await this.ensure(body.profile,body.owner,body.proxy||'',true,body.name,true);
      const current=()=>!this.stopping&&!p.closed&&!p.clients.size&&this.profiles.get(p.id)===p;
      if(!current())throw new Error('窗口或任务状态已变化');
      if(body.cookies!=null){
        const write=async()=>{
          for(const c of body.cookies){
            if(!current())throw new Error('窗口已关闭，登录资料导入已取消');
            await p.session.cookies.set({url:`https://${c.domain.replace(/^\./,'')}${c.path||'/'}`,name:c.name,value:c.value,domain:c.domain,path:c.path||'/',secure:c.secure,httpOnly:c.httpOnly,...(c.expires>0?{expirationDate:c.expires}:{}),sameSite:({None:'no_restriction',Lax:'lax',Strict:'strict'} as any)[c.sameSite]||'unspecified'});
            if(!current())throw new Error('窗口已关闭，登录资料导入已取消');
          }
          await p.session.cookies.flushStore();
          if(!current())throw new Error('窗口已关闭，登录资料导入已取消');
        };
        let writes=this.sessionWrites.get(p);if(!writes){writes=new Set();this.sessionWrites.set(p,writes)}
        const writing=write();writes.add(writing);
        try{await writing}finally{writes.delete(writing);if(!writes.size)this.sessionWrites.delete(p)}
      }
      const selected=p.pages.get(p.selected||'');
      const page=selected&&!selected.view.webContents.isDestroyed()?selected:await this.newPage(p,'about:blank'),wc=page.view.webContents;
      const existing=wc.getURL().startsWith('https://web.whatsapp.com/');
      // Ordinary Open shows the native viewport immediately. Navigation has
      // its own observable loading/error state; it must not hold the shell.
      const runtime=this.whatsappPages.get(page.view)!;
      const navigation=runtime.open(url.href,body.cookies!=null&&existing?'refresh':'open');
      const result=body.cookies!=null?await navigation:{page_loaded:null,navigation_pending:true,message:'窗口已打开，WhatsApp 正在加载'};
      if(body.cookies==null)void navigation.catch(()=>{});
      if(this.stopping||p.closed||this.profiles.get(p.id)!==p)throw new Error('窗口已关闭');
      return {...result,message:result.page_loaded===false?`WhatsApp 网页加载失败${result.http_status?`（HTTP ${result.http_status}）`:''}，请检查网络或代理。`:result.message||'',ws:this.endpoint(p),generation:p.generation};
    }
    if(method==='label-task-page'){
      const p=this.profiles.get(body.profile),page=p?.pages.get(body.target);
      if(!p||p.closed||!page||page.view.webContents.isDestroyed()||!['source','screening','task'].includes(body.role))throw new Error('任务页面无效');
      if(body.viewport_mode!==undefined&&(body.viewport_mode!=='posting'||body.role!=='task'||!p.clients.size))throw new Error('发帖页面尺寸请求无效');
      const previousViewport=page.postingViewport;
      if(body.role!=='task')delete page.postingViewport;
      if(body.viewport_mode==='posting'&&!page.postingViewport){
        // A collector keeps its long-standing 1280x900 coordinates. A new
        // posting composer instead snapshots the actual bounded pane once,
        // before its controls are used; later watch/resize cannot move them.
        const win=this.window(),pane=win&&!win.isDestroyed()?this.panes.get(win):undefined;
        const b=this.workspaceBounds&&pane?pane.getBounds():undefined;
        page.postingViewport=b&&Number.isInteger(b.width)&&Number.isInteger(b.height)&&b.width>=100&&b.height>=100
          ?{width:b.width,height:b.height}:{width:1280,height:720};
      }
      if(body.role==='screening'&&Number.isInteger(body.slot)&&body.slot>=1&&body.slot<=3){
        // Replacement pages inherit the same slot, hiding the retained old page
        // from the task tabs while its owner finishes recovery and cleanup.
        for(const other of p.pages.values())if(other!==page&&other.role==='screening'&&other.roleIndex===body.slot){other.role=undefined;other.roleIndex=undefined}
        page.role='screening';page.roleIndex=body.slot;
      }else if(page.role!==body.role){page.role=body.role;const used=new Set([...p.pages.values()].filter(x=>x!==page&&x.role===body.role).map(x=>x.roleIndex));let index=1;while(used.has(index))index++;page.roleIndex=index;}
      // Source and single-task labels name the current worker page. A fresh
      // posting tab replaces the initial connection tab's label, but never
      // closes or navigates that retained page or changes its manual selection.
      let retiredViewport=false;
      if(body.role==='source'||body.role==='task')for(const other of p.pages.values())if(other!==page&&other.role===body.role){other.role=undefined;other.roleIndex=undefined;if(other.postingViewport){delete other.postingViewport;retiredViewport=true}}
      if(retiredViewport||page.postingViewport!==previousViewport){const win=this.window();if(win&&!win.isDestroyed())this.layoutPane(win)}
      // Wait for this target's metric request if its renderer is ready. All
      // label writes are synchronous above: a late completion cannot restore
      // posting metadata after disconnect, replacement or manual reopening.
      if(body.viewport_mode==='posting')await this.sizePageViewport(page.view,page.postingViewport);
      return {labelled:true};
    }
    if(method==='watch-profile'){
      const p=this.profiles.get(body.profile);if(!p||p.closed||p.owner!==body.owner)throw new Error('任务网页尚未打开');
      const pages=[...p.pages.values()].filter(page=>!page.view.webContents.isDestroyed());
      const rank=(page:AccountPage)=>page.role==='source'?0:page.role==='screening'?page.roleIndex||1:10;
      const labelled=pages.filter(page=>page.role).sort((a,b)=>rank(a)-rank(b)),watched=labelled.length?labelled:pages;
      const target=body.target||watched.find(x=>x.role==='source')?.targetId||watched[0]?.targetId;
      const requested=watched.find(x=>x.targetId===target);if(!requested||requested.view.webContents.isDestroyed())throw new Error('任务页面不可用');
      if(p.owner!==body.owner)throw new Error('账号不属于当前用户');
      const fallback=body.prefer_ready&&!body.target&&this.pageDisplayStatus(requested).display_state==='loading'?
        watched.find(x=>x!==requested&&this.pageDisplayStatus(x).display_state==='ready'):undefined;
      const page=fallback||requested;
      const waitingLabel=fallback?(requested.role==='source'?'采集页':requested.role==='screening'?'1-'+requested.roleIndex:'任务页'):undefined;
      return {generation:p.generation,target:page.targetId,waiting_target:waitingLabel,pages:watched.map(x=>({id:x.targetId,label:x.role==='source'?'采集页':x.role==='screening'?'1-'+x.roleIndex:x.role==='task'?'任务页':'页面 '+(pages.indexOf(x)+1),title:x.view.webContents.getTitle(),...this.pageDisplayStatus(x)})),image:'',captured_at:new Date().toISOString(),title:page.view.webContents.getTitle(),...this.pageDisplayStatus(page)};
    }
    if(method==='close-task-page'){
      const p=this.profiles.get(body.profile),page=p?.pages.get(body.target);
      if(!p||p.closed||p.owner!==body.owner||p.generation!==body.generation||p.manualOnly||!page?.role)throw new Error('任务页面已变化，请重新查看');
      const wc=page.view.webContents;if(wc.isDestroyed())return {closed:true,target:body.target};
      await new Promise<void>((resolve,reject)=>{
        const done=()=>{clearTimeout(timer);resolve()};
        const timer=setTimeout(()=>{wc.removeListener('destroyed',done);reject(new Error('页面尚未完全关闭，任务已暂停，请稍后重试'))},4000);
        wc.once('destroyed',done);wc.close({waitForBeforeUnload:false});
      });
      return {closed:true,target:body.target};
    }

    if (method === 'reset-whatsapp-storage') {
      if (!new RegExp(`^native:${uuid}$`,'i').test(body.profile) || !new RegExp(`^${uuid}$`,'i').test(body.owner)) throw new Error('内置账号标识无效');
      const previous=this.profiles.get(body.profile);
      if ((previous&&(!previous.closed||previous.owner!==body.owner)) || this.opening.has(body.profile)) throw new Error('请先关闭此窗口后重置登录资料');
      if(this.resetting.has(body.profile))throw new Error('此窗口正在修复登录，请稍候');
      this.resetting.add(body.profile);
      try {
      if(previous)await this.closeProfile(previous);
      whatsappSessionPath(app.getPath('userData'),body.owner,body.profile,true);
      this.unread.invalidate(body.profile);
      return {reset:true,message:'已创建新的 WhatsApp 登录环境，原资料已保留。请重新关联。'};
      } finally {this.resetting.delete(body.profile)}
    }
    if (method === 'unread') return { windows: this.unread.snapshot(body.owner, new Set([...this.profiles.values()].filter(p=>!p.closed&&p.owner===body.owner).map(p=>p.id))) };
    if (method === 'hide') { this.hide(body.profile); return { hidden: true }; }
    if (method === 'inventory') return { profiles: [...this.profiles.values()].filter(p => !p.closed).map(p => ({ id: p.id, generation: p.generation, ws: this.endpoint(p) })) };
    if (method === 'ensure') {
      const p = await this.ensure(body.profile, body.owner, body.proxy || '', Boolean(body.open), body.name);
      return { ws: this.endpoint(p), generation: p.generation };
    }
    const p = this.profiles.get(body.profile);
    if (!p || p.closed) throw new Error('内置窗口尚未打开');
    if(method==='whatsapp-diagnostics'){
      if(p.owner!==body.owner||p.generation!==body.generation||p.clients.size)throw new Error('请等待此窗口空闲后检查');
      const page=p.pages.get(p.selected||''),wc=page?.view.webContents;
      if(!wc||wc.isDestroyed()||new URL(wc.getURL()).origin!=='https://web.whatsapp.com')throw new Error('请先打开 WhatsApp 登录页面');
      const storagePath=p.session.getStoragePath(),disk=storagePath?await statfs(storagePath).then(s=>({freeBytes:s.bavail*s.bsize})).catch(()=>({error:'StoragePathUnavailable'})):null;
      // The page's own timeout cannot run when its JS thread is blocked.
      // Bound the probe in the main process so an error report still exports.
      let probeTimer:ReturnType<typeof setTimeout>|undefined,checks;
      try{checks=await Promise.race([wc.executeJavaScriptInIsolatedWorld(1003,[{code:`(${whatsappStorageProbe.toString()})()`}]).catch(error=>({incomplete:true,error:whatsappErrorText(error)})),new Promise(resolve=>{probeTimer=setTimeout(()=>resolve({incomplete:true,error:'RendererProbeTimeout'}),9000)})])}finally{if(probeTimer)clearTimeout(probeTimer)}
      return {schema:3,runtime:await this.whatsappPages.get(page.view)?.report(),version:app.getVersion(),checkedAt:new Date().toISOString(),platform:process.platform,architecture:process.arch,versions:{electron:process.versions.electron,chromium:process.versions.chrome},storage:{persistent:Boolean(storagePath),pathLength:storagePath?.length||0,nonAsciiPath:Boolean(storagePath&&/[^\x00-\x7f]/.test(storagePath)),disk},surface:{openingMode:p.manualOnly?'native-page':'automation-page',debuggerAttached:wc.debugger.isAttached(),last:p.lastSurface,currentlyVisible:this.visible?.profile===p.id,viewBounds:page.view.getBounds(),pageZoom:wc.getZoomFactor(),configuredProxy:Boolean(p.proxy)},checks,events:p.diagnostics||[]};
    }
    if(method==='chat-translation'){
      const step=body.step;
      if(!step||!['poll','apply','export'].includes(step.kind)||JSON.stringify(step).length>50000)throw new Error('同步翻译请求无效');
      if(p.owner!==body.owner||p.generation!==body.generation||p.closed||p.clients.size)throw new Error('账号会话已变化或任务占用');
      // The editor covers the native page. Only its read-only export may run
      // while hidden; poll/apply keep their selected, visible, idle-page fence.
      if(step.kind!=='export'&&this.chatTranslationTarget()!==p)throw new Error('请在空闲的当前聊天窗口使用同步翻译');
      if(step.kind==='export'&&this.visible?.profile===p.id&&this.visible.readOnly)throw new Error('任务占用，暂时不能导出');
      if(step.kind==='poll')step.settings=chatSettings(step.settings);
      if(step.kind==='apply'&&(typeof step.id!=='string'||typeof step.conversation!=='string'||typeof step.original!=='string'||(!step.error&&typeof step.translation!=='string')))throw new Error('同步翻译结果无效');
      const target=step.kind==='export'?p.selected:this.visible?.target||p.selected;
      const page=p.pages.get(target||'');
      if(!page||page.view.webContents.isDestroyed())throw new Error('请先打开此窗口的聊天页面');
      const wc=page.view.webContents,epoch=this.pageEpoch.get(wc)||0,url=wc.getURL();
      const current=()=>!this.stopping&&!p.closed&&!p.clients.size&&this.profiles.get(p.id)===p&&
        p.pages.get(target||'')===page&&!wc.isDestroyed()&&wc.getURL()===url&&(this.pageEpoch.get(wc)||0)===epoch&&
        (step.kind==='export'?p.selected===target:this.chatTranslationTarget()===p&&(this.visible?.target||p.selected)===target);
      const whatsapp=this.whatsappPages.get(page.view);
      if(whatsapp){
        const state:any=await whatsapp.inspect();
        if(!current())throw new Error('窗口或任务状态已变化，请等待当前页面空闲');
        if(!state.chatReady)return {status:'请先完成 WhatsApp 登录并打开聊天',messages:[]};
      }
      if(!current())throw new Error('窗口或任务状态已变化，请等待当前页面空闲');
      if(step.kind!=='export')this.translationPages.add(page.targetId);
      return wc.executeJavaScriptInIsolatedWorld(1002,[{code:`(${chatTranslationPage.toString()})(${JSON.stringify(step)})`}]);
    }
    if (method === 'verify') {
      if (body.generation !== p.generation || body.ws !== this.endpoint(p)) throw new Error('内置窗口连接已变化');
      return { verified: true };
    }
    if (method === 'manual-navigation') {
      if (body.generation !== p.generation || body.owner !== p.owner) throw new Error('账号会话已变化，请重新打开窗口');
      const page = p.pages.get(p.selected || '');
      if (!page || page.view.webContents.isDestroyed()) throw new Error('当前网页已关闭，请重新打开窗口');
      const whatsapp=this.whatsappPages.get(page.view);
      if(whatsapp){if(!['refresh','back','forward'].includes(body.action))throw new Error('无效 WhatsApp 导航');return whatsapp.open('https://web.whatsapp.com/',body.action)}
      return navigateAccountPage(page.view.webContents, body.action, body.homeUrl);
    }
    if (method === 'profile-preview') {
      if (body.generation !== p.generation || body.owner !== p.owner) throw new Error('预览账号会话已变化');
      const url = profilePreviewUrl(body.username, body.action);
      // AccountWorkspace holds the ordinary exclusive operation lease through
      // navigation. This page shares only this account's session and proxy.
      let page = p.pages.get(p.previewTarget || '');
      if (!page || page.view.webContents.isDestroyed()) {
        page = await this.newPage(p, 'about:blank'); p.previewTarget = page.targetId;
      }
      this.activate(p, page);
      return { ...await loadProfilePreview(page.view.webContents, url), opened: true };
    }
    if (method === 'close') {
      if (body.generation !== p.generation) throw new Error('窗口已更新，不能关闭新的会话');
      await this.closeProfile(p); return { closed: true };
    }
    if (method === 'show') {
      if (!body.grant || body.grant !== this.surfaceGrant) return {attached:false};
      if(body.target&&!p.pages.has(body.target))throw new Error('任务页面已关闭');
      this.show(p, body.bounds, body.target, body.read_only===true);
      const page=p.pages.get(body.target||p.selected||'');
      const display=page?this.pageDisplayStatus(page):{display_state:'blank',display_message:'任务页面已关闭，正在更新页面列表。'};
      return {attached:Boolean(page&&this.attached===page&&this.surfaceRaised),...display,message:display.display_message};
    }
    throw new Error('不支持的内置窗口操作');
  }
  async ensure(id: string, owner: string, proxy: string, open: boolean, name?: string, manualOnly=false): Promise<EmbeddedProfile> {
    if(this.stopping)throw new Error('软件正在退出，窗口已关闭');
    if (!new RegExp(`^native:${uuid}$`, 'i').test(id) || !new RegExp(`^${uuid}$`, 'i').test(owner)) throw new Error('内置账号标识无效');
    if(this.resetting.has(id))throw new Error('此窗口正在修复登录，请稍候');
    const pending = this.opening.get(id);
    if (pending) { await pending; return this.ensure(id, owner, proxy, open, name,manualOnly); }
    const existing = this.profiles.get(id);
    if(existing?.closed){await this.closeProfile(existing);return this.ensure(id,owner,proxy,open,name,manualOnly)}
    if (existing && !existing.closed) {
      if (existing.owner !== owner || existing.proxy !== proxy) throw new Error('窗口所属账号或代理已变化');
      if(typeof name==='string')existing.name=name.slice(0,80);
      return existing;
    }
    if (!open) throw new Error('内置窗口尚未打开');
    const create = async () => {
      const accountSession = manualOnly?await createWhatsAppSession(owner,id,proxy):session.fromPartition(`persist:account-${owner}-${id.slice(7)}`);
      if(this.stopping)throw new Error('软件正在退出，窗口已关闭');
      if(!manualOnly){
      await accountSession.setProxy(proxy ? { proxyRules: proxy } : { mode: 'direct' });
      // No native notification prompts or remote page access to app IPC.
      accountSession.setPermissionCheckHandler((wc, permission, origin, details) => {const allowed=accountStoragePermission(permission,origin,wc?.getURL()||details.embeddingOrigin||'');if(['persistent-storage','storage-access'].includes(permission))this.diagnostic(id,{kind:'permission',permission,allowed,worker:!wc});return allowed});
      accountSession.setPermissionRequestHandler((wc, permission, cb, details) => cb(accountStoragePermission(permission, details.requestingUrl, wc?.getURL() || '')));
      // Drop app/Electron identification while retaining the actual Chromium version.
      accountSession.setUserAgent(accountUserAgent(accountSession.getUserAgent()), 'zh-CN,zh;q=0.9,en;q=0.8');
      }
      const generation = (this.generations.get(id) || 0) + 1;
      this.generations.set(id, generation);
      const p: EmbeddedProfile = { id, owner, name:typeof name==='string'?name.slice(0,80):undefined, proxy, generation, capability: randomBytes(32).toString('hex'), session: accountSession, pages: new Map(), clients: new Set(), closed: false,manualOnly };
      this.profiles.set(id, p);
      if(!this.watchedSessions.has(accountSession)) {
        this.watchedSessions.add(accountSession);
        accountSession.webRequest.onErrorOccurred({urls:['https://*.whatsapp.com/*','https://*.whatsapp.net/*','wss://*.whatsapp.com/*']},details=>{this.diagnostic(id,{kind:'network',host:new URL(details.url).hostname,type:details.resourceType,codes:diagnosticCodes(details.error),message:whatsappErrorText(details.error)})});
        accountSession.webRequest.onCompleted({urls:['https://*.whatsapp.com/*','https://*.whatsapp.net/*']},details=>{if(details.statusCode>=400)this.diagnostic(id,{kind:'http-error',host:new URL(details.url).hostname,type:details.resourceType,status:details.statusCode})});

        accountSession.cookies.on('changed', (_event, cookie) => {
          if (['sessionid','ds_user_id','c_user'].includes(cookie.name)) {this.unread.invalidate(id);this.onUnreadReset?.(id);}
        });
      }
      try { await this.newPage(p, 'about:blank'); return p; }
      catch (e) { await this.closeProfile(p); throw e; }
    };
    const promise = create(); this.opening.set(id, promise);
    try { return await promise; } finally { this.opening.delete(id); }
  }
  private makeView(p: EmbeddedProfile) {
    const preferences: Electron.WebPreferences = {
      session: p.session, contextIsolation: true, nodeIntegration: false,
      sandbox: true, webSecurity: true, allowRunningInsecureContent: false,
      webviewTag: false, navigateOnDragDrop: false, backgroundThrottling: false, spellcheck: false,
    };
    const view=p.manualOnly?createWhatsAppView(p.session):new WebContentsView({webPreferences:preferences});
    if(p.manualOnly)this.whatsappPages.set(view,new WhatsAppPageRuntime(view.webContents));
    // All pages are children of the bounded account pane, never top-level
    // siblings of the application shell.
    view.setBounds({ x: 0, y: 0, width: 1280, height: 900 });
    view.setBackgroundColor('#0b1016');
    const wc = view.webContents;
    type ReadyDocument = {url:string;processId:number;routingId:number};
    let navigation:{url:string;previous?:ReadyDocument;committed:boolean;cancelled:boolean}|undefined;
    const documentIdentity=():ReadyDocument|undefined=>{
      if(wc.isDestroyed())return;
      try{
        const frame=wc.mainFrame,url=wc.getURL();
        if(url&&url!=='about:blank'&&frame&&Number.isInteger(frame.processId)&&Number.isInteger(frame.routingId))return {url,processId:frame.processId,routingId:frame.routingId};
      }catch{/* A renderer being destroyed has no usable document. */}
    };
    const sameDocument=(before:ReadyDocument|undefined)=>{
      const current=documentIdentity();
      return Boolean(before&&current&&before.url===current.url&&before.processId===current.processId&&before.routingId===current.routingId);
    };
    const restoreCancelledDocument=()=>{
      // Cancelling a provisional navigation can retain the already-painted old
      // document and emit no further dom-ready. Restore it only with positive
      // identity evidence, no committed successor and no main-frame load left.
      if(!navigation?.cancelled||navigation.committed||!sameDocument(navigation.previous)||this.pageDisplay.get(wc)?.display_state!=='loading')return;
      if(typeof wc.isLoadingMainFrame!=='function'||wc.isLoadingMainFrame())return;
      this.pageReady.add(wc);this.pageDisplay.set(wc,{display_state:'ready',display_message:''});
      void this.sizePageViewport(view).catch(()=>{});this.render();
    };
    wc.on('context-menu',(_event,params)=>{
      const win=this.window(),url=wc.getURL(),menuEpoch=++this.pageMenuEpoch;
      const valid=()=>!wc.isDestroyed()&&!p.closed&&!p.clients.size&&this.visible?.profile===p.id&&
        !this.visible.readOnly&&this.attached?.view===view&&Boolean(this.surfaceGrant)&&this.pageMenuEpoch===menuEpoch&&wc.getURL()===url;
      if(!win||win.isDestroyed()||!valid())return;
      this.pageMenu?.closePopup();
      const menu=Menu.buildFromTemplate(webPageMenu(params,(action,value)=>{
        if(action==='copy-text'){clipboard.writeText(value||'');return}
        if(action==='copy-message'){
          void wc.executeJavaScriptInIsolatedWorld(1006,[{code:`(${contextMessage.toString()})(${params.x},${params.y})`}])
            .then(text=>{if(valid()&&typeof text==='string'&&text)clipboard.writeText(text)}).catch(()=>{});return;
        }
        if(action==='copy-image'){wc.copyImageAt(params.x,params.y);return}
        if(action==='save-image'){wc.downloadURL(value!);return}
        if(['undo','redo','cut','copy','paste','selectAll'].includes(action)){
          wc.focus();(wc[action as 'copy'] as ()=>void).call(wc);return;
        }
        void this.onPageNavigation?.(action).catch(()=>{});
      },valid));
      this.pageMenu=menu;menu.popup({window:win,callback:()=>{if(this.pageMenu===menu)this.pageMenu=undefined}});
    });
    wc.setUserAgent(p.session.getUserAgent());
    wc.on('console-message',(_event,level,message,line,source)=>{if(level>=2&&!wc.isDestroyed()&&wc.getURL().startsWith('https://web.whatsapp.com/')){const codes=diagnosticCodes(message);if(codes.length)this.diagnostic(p.id,{kind:'page-error',codes,message:whatsappErrorText(message),line,source:whatsappErrorText(source)})}});
    wc.on('render-process-gone',(_event,details)=>{
      navigation=undefined;
      this.resetUnreadPage(wc);this.pageReady.delete(wc);this.viewportSizes.delete(view);this.columnReady.delete(wc);
      this.pageDisplay.set(wc,{display_state:'crashed',display_message:'此网页进程已退出，等待当前任务恢复页面；任务窗口仍保持占用。'});
      this.diagnostic(p.id,{kind:'renderer',reason:details.reason,exitCode:details.exitCode});
      this.render();this.onPageExit?.({reason:details.reason,exitCode:details.exitCode,taskClients:p.clients.size,pages:p.pages.size});
    });
    wc.on('unresponsive',()=>{this.pageUnresponsive.add(wc);this.render()});
    wc.on('responsive',()=>{this.pageUnresponsive.delete(wc);this.render()});
    const failedNavigation=(errorCode:number,url:string,main:boolean)=>{
      if(!main)return;
      // A superseded URL may emit its failure after the next document has
      // loaded. It has no authority over that newer page's display state.
      if(url&&url!==(navigation?.url||wc.getURL()))return;
      if(errorCode===-3){
        if(navigation)navigation.cancelled=true;
        restoreCancelledDocument();return;
      }
      if(navigation)navigation.previous=undefined;
      this.pageDisplay.set(wc,{display_state:'load_failed',display_message:`网页加载失败（${errorCode}），等待当前任务恢复；可切换标签查看其他任务页。`});
      this.diagnostic(p.id,{kind:'page-load',errorCode});this.render();
    };
    wc.on('did-fail-load',(_event,code,_description,url,main)=>failedNavigation(code,url,main));
    // Electron documents this event for cancellations such as window.stop().
    wc.on('did-fail-provisional-load',(_event,code,_description,url,main)=>{if(code===-3)failedNavigation(code,url,main)});
    wc.on('did-stop-loading',restoreCancelledDocument);
    wc.on('did-redirect-navigation',(_event,url,inPlace,main)=>{if(main&&!inPlace&&navigation)navigation.url=url});
    wc.on('did-navigate',(_event,url)=>{if(navigation){navigation.url=url;navigation.committed=true;navigation.previous=undefined}});

    wc.on('focus', () => {
      if(this.visible?.readOnly&&this.attached?.view.webContents===wc){
        if(this.watchShield?.isUsable())this.watchShield.view.webContents.focus();else this.detach();
        return;
      }
      if (this.visible?.profile !== p.id || this.attached?.view.webContents !== wc) {
        const win=this.window();this.focusIntruder=wc;
        // A background task must not route the user's keystrokes to itself or
        // reorder the workspace. Restore the user's actual input surface.
        // Native focus callbacks can be reentrant while a view is being born or
        // destroyed. Never synchronously focus a second native view from here.
        if(!this.focusRestorePending){this.focusRestorePending=true;setImmediate(()=>{
          this.focusRestorePending=false;
          const intruder=this.focusIntruder;this.focusIntruder=undefined;
          if(!win||win.isDestroyed()||!win.isFocused()||!intruder||intruder.isDestroyed()||!intruder.isFocused())return;
          const visible=this.visible,profile=visible&&this.profiles.get(visible.profile);
          const interactive=visible&&!visible.readOnly&&Date.now()<=visible.deadline&&profile&&!profile.closed&&(!profile.clients.size||Boolean(this.surfaceGrant))
            ?this.attached?.view.webContents:undefined;
          // Multiple background pages can focus during one native turn. Follow
          // the most recent intrusion, and preserve a page that became the
          // legitimate foreground before this deferred correction executes.
          if(intruder===win.webContents||intruder===interactive)return;
          const prior=this.inputFocus,priorVisible=prior===win.webContents||prior===interactive;
          if(priorVisible&&prior&&prior!==intruder&&!prior.isDestroyed())prior.focus();
          else {const shell=win.webContents;if(shell&&!shell.isDestroyed())shell.focus();}
        });}
      }else this.inputFocus=wc;
    });
    wc.on('before-input-event',(event,input)=>{if(input.type==='keyDown'&&input.key==='Escape'&&this.onEscape?.())event.preventDefault()});
    wc.on('did-start-navigation', (_event,url,inPlace,main) => {
      if(main&&!inPlace){
        const previous=this.pageReady.has(wc)&&!['load_failed','crashed'].includes(this.pageDisplay.get(wc)?.display_state||'')
          ?documentIdentity():navigation&&!navigation.committed&&sameDocument(navigation.previous)?navigation.previous:undefined;
        navigation={url,previous,committed:false,cancelled:false};
        this.resetUnreadPage(wc);this.pageReady.delete(wc);this.viewportSizes.delete(view);this.columnReady.delete(wc);this.pageUnresponsive.delete(wc);
        this.pageDisplay.set(wc,{display_state:'loading',display_message:'当前任务页正在加载，等待网页内容就绪。'});this.render();
      }
    });
    wc.on('destroyed',()=>this.resetUnreadPage(wc));
    wc.on('dom-ready', () => {
      this.pageReady.add(wc);
      // Chromium may emit dom-ready for its network-error document too.
      // Only the next real navigation may clear a known main-frame failure.
      if(!['load_failed','crashed'].includes(this.pageDisplay.get(wc)?.display_state||''))this.pageDisplay.set(wc,{display_state:'ready',display_message:''});
      void this.sizePageViewport(view).catch(()=>{});
      this.render();
    });
    wc.on('did-finish-load', () => this.render());
    const guard = (e: Electron.Event, url: string) => { if (!permittedPageUrl(url)) e.preventDefault(); };
    wc.on('will-navigate', guard); wc.on('will-redirect', guard);
    wc.setWindowOpenHandler(({ url }) => {
      if (permittedPageUrl(url) && !p.closed) {
        const openerId = [...p.pages.values()].find(page => page.view.webContents === wc)?.targetId;
        // Route requested pages into the account workspace after Chromium's
        // synchronous window.open callback returns. Never create an OS window.
        // WindowProxy/opener scripting is intentionally not emulated.
        setImmediate(() => void this.newPage(p, url, openerId).catch(() => {}));
      }
      return { action: 'deny' };
    });
    return view;
  }
  private async registerPage(p: EmbeddedProfile, view: WebContentsView, openerId?: string): Promise<AccountPage> {
    const wc = view.webContents;
    let win = this.window();
    for (let i = 0; !win && i < 50 && !p.closed && !this.stopping; i++) { await new Promise(resolve => setTimeout(resolve, 100)); win = this.window(); }
    if (this.stopping || p.closed || !win || win.isDestroyed()) { wc.close({waitForBeforeUnload:false}); throw new Error('软件主窗口尚未就绪'); }
    this.attachWindow(win);this.panes.get(win)!.addChildView(view,0);this.layoutPane(win);if(!this.visible)this.raiseShell();
    
    // Manual WhatsApp pages use native layout and no debugger at all. The
    // opaque page key is internal to this profile, never a CDP target.
    let targetInfo:{targetId:string;openerId?:string}={targetId:`manual-${wc.id}`};
    if(!p.manualOnly){wc.debugger.attach('1.3');({targetInfo}=await wc.debugger.sendCommand('Target.getTargetInfo'));}
    
    const page: AccountPage = { view, targetId: targetInfo.targetId, openerId: openerId || targetInfo.openerId };
    // Target discovery may finish after native destruction. The destroyed
    // event has already passed, so publishing here would leave a dead page
    // permanently selected with no remaining cleanup listener.
    if (this.stopping || p.closed || wc.isDestroyed()) {
      if(!wc.isDestroyed())wc.close({ waitForBeforeUnload: false });
      throw new Error('窗口已关闭');
    }
    p.pages.set(page.targetId, page); p.selected = page.targetId;
    wc.debugger.on('message', (_event, method, params, sessionId) => {
      for (const client of p.clients) client.event(page, method, params, sessionId);
    });
    wc.on('destroyed', () => {
      const win = this.window();
      if (win && !win.isDestroyed()) this.panes.get(win)?.removeChildView(view);
      p.pages.delete(page.targetId);this.translationPages.delete(page.targetId);
      for (const client of p.clients) client.pageClosed(page);
      if (this.attached === page) { this.detach(); }
      if (p.selected === page.targetId) p.selected = [...p.pages.keys()].at(-1);
      this.render();
    });
    // A page opening a child must never create an independent OS window.
    await Promise.all([...p.clients].map(c => c.pageCreated(page)));
    this.render();
    return page;
  }
  async newPage(p: EmbeddedProfile, url: string, openerId?: string) {
    if(this.stopping)throw new Error('软件正在退出，窗口已关闭');
    if (!permittedPageUrl(url) || p.closed) throw new Error('页面地址无效');
    const view = this.makeView(p),wc=view.webContents;
    let pending=this.pendingPages.get(p);
    if(!pending){pending=new Set();this.pendingPages.set(p,pending)}
    pending.add(view);
    let onClosed:()=>void=()=>{};
    const closed=new Promise<never>((_resolve,reject)=>{
      onClosed=()=>reject(new Error('窗口已关闭，页面注册已取消'));
      wc.once('destroyed',onClosed);
    });
    this.cancelPendingPage.set(view,onClosed);
    const open=async()=>{
      const page = await this.registerPage(p, view, openerId);
      if(this.stopping||p.closed||wc.isDestroyed())throw new Error('窗口已关闭');
      if(!p.manualOnly||url!=='about:blank'){
        const whatsapp=this.whatsappPages.get(view);
        if(whatsapp)void whatsapp.open(url).catch(()=>{});else void wc.loadURL(url).catch(()=>{});
      }
      await this.sizePageViewport(view);
      if(this.stopping||p.closed||wc.isDestroyed())throw new Error('窗口已关闭');
      return page;
    };
    try {
      // Closing a half-registered view must finish the open even if Electron's
      // pending debugger command never rejects after renderer destruction.
      return await Promise.race([open(),closed]);
    } catch (error) {
      const win = this.window();
      if (win && !win.isDestroyed()) this.panes.get(win)?.removeChildView(view);
      if (!wc.isDestroyed()) wc.close({waitForBeforeUnload:false});
      throw error;
    } finally {
      wc.removeListener('destroyed',onClosed);this.cancelPendingPage.delete(view);
      // A failed native close is still an owned view. Keep it discoverable for
      // retry even after the waiting open was cancelled and its caller failed.
      if(wc.isDestroyed()||[...p.pages.values()].some(page=>page.view===view))pending.delete(view);
      if(!pending.size&&this.pendingPages.get(p)===pending)this.pendingPages.delete(p);
    }
  }
  activate(p: EmbeddedProfile, page: AccountPage) { p.selected = page.targetId;if(this.visible?.profile===p.id)this.render(); }
  private resetUnreadPage(wc: WebContents) {
    // A timed-out read remains single-flight for its document. Only a real
    // document lifecycle change permits a fresh read; old finalizers own no
    // new document's lock, even when the URL is identical after reload.
    this.pageEpoch.set(wc,(this.pageEpoch.get(wc)||0)+1);
    this.readingUnread.delete(wc);
  }
  private unreadPending(p: EmbeddedProfile) {
    const wc=p.pages.get(p.selected||'')?.view.webContents;
    return Boolean(wc&&this.readingUnread.has(wc));
  }
  private scheduleUnread() {
    // Spread work across ticks instead of waking every account simultaneously.
    // A stalled renderer occupies at most one of two slots, with no backlog.
    if(this.stopping||this.activeUnread>=2)return;
    const now=Date.now(),eligible=[...this.profiles.values()].filter(p=>!p.closed&&!p.clients.size&&!this.unreadPending(p)&&(this.unreadDue.get(p)||0)<=now);
    eligible.sort((a,b)=>Number(b.id===this.visible?.profile)-Number(a.id===this.visible?.profile)||(this.unreadDue.get(a)||0)-(this.unreadDue.get(b)||0));
    const next=eligible[0];if(!next)return;
    this.unreadDue.set(next,now+(next.id===this.visible?.profile?1500:4500));
    void this.readUnread(next);
  }
  async readUnread(p: EmbeddedProfile) {
    if (this.stopping || p.closed || p.clients.size || this.unreadPending(p)) return;
    const page = p.pages.get(p.selected || '');
    if (!page || page.view.webContents.isDestroyed()) return;
    const wc=page.view.webContents, epoch=this.pageEpoch.get(wc)||0, token={};
    const target=p.selected, url=wc.getURL();
    const current=()=>!this.stopping&&!p.closed&&!p.clients.size&&this.profiles.get(p.id)===p&&p.selected===target&&
      !wc.isDestroyed()&&wc.getURL()===url&&(this.pageEpoch.get(wc)||0)===epoch;
    this.readingUnread.set(wc,token);this.activeUnread++;
    let timer:ReturnType<typeof setTimeout>|undefined;
    const reading=Promise.resolve().then(async()=>{
      if(!current())return {status:'unavailable'};
      const whatsapp=this.whatsappPages.get(page.view);
      if(whatsapp){const state:any=await whatsapp.inspect();if(!state.chatReady)return {platform:'whatsapp',count:null,status:state.phase==='storage-error'?'storage_error':['qr-candidate','phone-login'].includes(state.phase)?'signed_out':'unavailable'};}
      if(!current())return {status:'unavailable'};
      return wc.executeJavaScriptInIsolatedWorld(1001,[{code:unreadPageScript}]);
    });
    const release=()=>{if(this.readingUnread.get(wc)===token)this.readingUnread.delete(wc)};
    void reading.then(release,release);
    try {
      const value = await Promise.race([
        reading,
        new Promise(resolve=>{timer=setTimeout(()=>resolve({status:'unavailable'}),2000)})
      ]);
      if(current()){this.unread.observe(p.owner,p.id,value);this.onUnread?.(p,value)}
    } catch { if(current())this.unread.observe(p.owner,p.id,{status:'unavailable'}); }
    finally { if(timer)clearTimeout(timer);this.activeUnread--; }
  }
  show(p: EmbeddedProfile, bounds: Rectangle, target?:string, readOnly=false) {
    const win = this.window();
    if (!win || win.isDestroyed() || win.isMinimized()) throw new Error('软件窗口不可见');
    const [width, height] = win.getContentSize();
    if (!bounds || !['x','y','width','height'].every(k => Number.isInteger((bounds as any)[k])) || bounds.x < 0 || bounds.y < 0 || bounds.width < 100 || bounds.height < 100 || bounds.x + bounds.width > width || bounds.y + bounds.height > height) throw new Error('网页显示区域无效');
    this.workspaceBounds = bounds;
    this.visible = { profile: p.id, bounds, deadline: Date.now() + 3000, target, readOnly };
    this.render();
  }
  setWorkspaceBounds(bounds: Rectangle) {
    this.workspaceBounds = bounds;
    const win = this.window(); if (win && !win.isDestroyed()) this.layoutPane(win);
  }
  private resetTaskPageViewports(p: EmbeddedProfile) {
    for(const page of p.pages.values())delete page.postingViewport;
  }
  private layoutPane(win: BrowserWindow) {
    const pane = this.panes.get(win); if (!pane) return;
    const [width,height] = win.getContentSize();
    const b = this.workspaceBounds || {x:0,y:0,width,height};
    const x = Math.max(0, Math.min(b.x,width-1)), y = Math.max(0, Math.min(b.y,height-1));
    const bounds = {x,y,width:Math.max(1,Math.min(b.width,width-x)),height:Math.max(1,Math.min(b.height,height-y))};
    // Cover an enlarged task pane before changing its native bounds. The
    // already-attached render path also comes here on sidebar/toolbar changes.
    if(this.visible?.readOnly&&this.watchShield)this.setNativeBounds(this.watchShield.view,{x:0,y:0,width:bounds.width,height:bounds.height});
    this.setNativeBounds(pane,bounds);
    for (const p of this.profiles.values()) for (const page of p.pages.values()) {
      const view=page.view;
      const active=this.visible?.profile===p.id&&(this.visible.target||p.selected)===page.targetId;
      const task=p.clients.size>0;
      // Hidden idle pages retain their existing dimensions and keep running.
      // Task and active dimensions are already known; avoid a native getter
      // for every page on every permission heartbeat or sidebar resize.
      if(!task&&!active)continue;
      const desired=accountViewport(task,active,bounds,bounds,page.postingViewport);
      this.setNativeBounds(view,desired);
      void this.sizePageViewport(view,desired).catch(() => {});
    }
  }
  private setNativeBounds(view:View,bounds:Rectangle) {
    const key=`${bounds.x}:${bounds.y}:${bounds.width}:${bounds.height}`;
    if(this.nativeBounds.get(view)===key)return;
    view.setBounds(bounds);this.nativeBounds.set(view,key);
  }
  private async sizePageViewport(view: WebContentsView, knownSize?: Pick<Rectangle,'width'|'height'>) {
    const wc = view.webContents;
    // Chromium can crash the entire main process when device metrics are sent
    // before a new page has a ready renderer. Native bounds may be set earlier;
    // defer CDP emulation until dom-ready, including after navigation/recovery.
    if (wc.isDestroyed() || !this.pageReady.has(wc) || !wc.debugger.isAttached()) return;
    const {width, height} = knownSize||view.getBounds(), key = `${width}:${height}`;
    if (this.viewportSizes.get(view) === key) {await this.viewportRequests.get(view)?.promise;return}
    // A view born inside a hidden pane has a 0x0 Chromium viewport even though
    // native bounds are nonzero. Give it the pane's real CSS dimensions so
    // ordinary Playwright hit-testing works without revealing a task window.
    const request={promise:Promise.resolve().then(()=>wc.debugger.sendCommand('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:0,mobile:false}))};this.viewportRequests.set(view,request);
    this.viewportSizes.set(view, key);
    try { await request.promise; }
    catch (error) { if(this.viewportRequests.get(view)===request)this.viewportSizes.delete(view); throw error; }
    finally {if(this.viewportRequests.get(view)===request)this.viewportRequests.delete(view)}
  }
  hide(profile?: string) {
    // Starting a task in B cannot revoke the foreground grant for A.
    if(profile&&this.visible?.profile!==profile)return;
    this.pageMenuEpoch++;this.pageMenu?.closePopup();this.pageMenu=undefined;
    this.surfaceGrant = '';
    if (!profile || this.visible?.profile === profile) { this.visible = undefined; this.detach(); }
  }
  async captureSurface():Promise<string>{
    const wc=this.attached?.view.webContents;
    if(!wc||wc.isDestroyed()||!this.visible)return '';
    let timer:ReturnType<typeof setTimeout>|undefined;
    try{return await Promise.race([
      wc.capturePage().then(image=>image.isEmpty()?'':image.toDataURL()).catch(()=>''),
      new Promise<string>(resolve=>{timer=setTimeout(()=>resolve(''),350)})
    ])}finally{if(timer)clearTimeout(timer)}
  }
  private raiseShell() {
    const win = this.window();
    if (!win || win.isDestroyed() || win.webContents.isDestroyed()) return;
    const pane = this.panes.get(win);
    if (!pane && !this.profiles.size) return;
    if (!pane) throw new Error('软件网页容器已丢失，无法保护任务窗口');
    // The shell is already below the overlay root. Hiding our own pane is
    // sufficient; reparenting contentView cannot raise the shell renderer.
    pane.setVisible(false);this.surfaceRaised=false;
  }
  private detach() {
    // Explicit native visibility is required on Windows: reordering alone can
    // leave Chromium's compositor surface painted over the next app page.
    // Keep the views attached and backgroundThrottling disabled for task input.
    const win = this.window();
    // Electron may already have cleared the view's getter in its destroyed
    // callback. Teardown must still detach and complete selected-page cleanup.
    const contents=this.attached?.view.webContents;
    const hadFocus=Boolean(contents&&!contents.isDestroyed()&&contents.isFocused()||this.watchShield?.hasFocus());
    this.raiseShell();
    if (hadFocus&&win&&!win.isDestroyed()&&win.isFocused()&&!win.webContents.isDestroyed())win.webContents.focus();
    this.attached = undefined;
  }
  private watchShield?:TaskWatchShield;
  private columnReady=new WeakSet<WebContents>();
  private installColumns(p:EmbeddedProfile,page:AccountPage){
    const wc=page.view.webContents;
    if(p.clients.size||wc.isDestroyed()||!this.pageReady.has(wc)||this.columnReady.has(wc))return;
    this.columnReady.add(wc);
    void wc.executeJavaScriptInIsolatedWorld(1005,[{code:whatsappColumnLayoutScript()}])
      .catch(()=>this.columnReady.delete(wc));
  }
  onTaskInterference?:(target:string)=>void;
  private pageDisplayStatus(page:AccountPage):PageDisplay {
    const wc=page.view.webContents,known=this.pageDisplay.get(wc);
    if(known?.display_state==='crashed'||known?.display_state==='load_failed')return known;
    if(this.pageUnresponsive.has(wc))return {display_state:'unresponsive',display_message:'当前网页暂时无响应，任务仍占用此窗口；请查看采集任务的恢复进度。'};
    const url=wc.getURL();
    if(!url||url==='about:blank')return {display_state:'blank',display_message:'此任务页尚未载入内容，可能正在创建或替换页面；可切换标签查看其他任务页。'};
    return known||{display_state:'ready',display_message:''};
  }
  private render() {
    const win = this.window(), visible = this.visible;
    if (!win || win.isDestroyed()) return;
    if (!visible) { this.detach(); return; }
    if(Date.now()>visible.deadline){this.hide();return;}
    const p = this.profiles.get(visible.profile), page = p?.pages.get(visible.target || p.selected || '');
    if (!page || page.view.webContents.isDestroyed()) { this.detach(); return; }
    // A live WebContents object is not evidence of a usable painted document.
    // Keep the exact task binding and lease, but expose the shell's status
    // instead of covering it with a blank or crashed native surface. The
    // worker alone decides whether/when to navigate or replace this page.
    if(this.pageDisplayStatus(page).display_state!=='ready'){this.detach();return;}
    if(visible.readOnly){
      if(this.watchShield&&!this.watchShield.isUsable()){
        // Retire only the failed display guard. The worker pages and CDP
        // connections keep their existing lifetime, coordinates and ownership.
        this.detach();
        this.panes.get(win)?.removeChildView(this.watchShield.view);
        this.watchShield.close();this.watchShield=undefined;
      }
      if(!this.watchShield)this.watchShield=new TaskWatchShield(
        ()=>{if(this.visible?.readOnly&&this.attached)this.onTaskInterference?.(this.attached.targetId)},
        failed=>{if(this.watchShield===failed&&this.visible?.readOnly)this.detach()},
      );
      if(!this.watchShield.isUsable()){this.detach();return;}
    }
    if(p&&!visible.readOnly)this.installColumns(p,page);
    const existingPane=this.panes.get(win);
    if(this.surfaceRaised&&this.attached===page&&this.renderedReadOnly===Boolean(visible.readOnly)&&existingPane?.getVisible()){
      // Core still validates every display lease. The same authorized view does
      // not need native reparenting or focus changes on every heartbeat.
      this.layoutPane(win);return;
    }
    if (this.attached !== page) this.detach();
    this.layoutPane(win);
    const pane = this.panes.get(win)!; const shieldWasAttached=Boolean(this.watchShield&&pane.children.includes(this.watchShield.view)&&this.attached===page); pane.addChildView(page.view);
    if(this.visible?.readOnly){
      this.setNativeBounds(this.watchShield!.view,{x:0,y:0,width:visible.bounds.width,height:visible.bounds.height});
      pane.addChildView(this.watchShield!.view);
    }else if(this.watchShield)pane.removeChildView(this.watchShield.view);
    this.attached = page; this.renderedReadOnly=Boolean(visible.readOnly);win.contentView.addChildView(pane); pane.setVisible(true);this.surfaceRaised=true;
    if(visible.readOnly&&!shieldWasAttached)this.watchShield?.view.webContents.focus();
    p!.lastSurface={at:new Date().toISOString(),bounds:{...visible.bounds},readOnly:Boolean(visible.readOnly),shield:Boolean(this.watchShield&&pane.children.includes(this.watchShield.view)&&this.watchShield.view.getVisible())};
    this.onSurfaceRendered?.();
  }
  async closeProfile(p: EmbeddedProfile) {
    const pending=this.closing.get(p);if(pending)return pending;
    const close=async()=>{
      p.closed = true;this.onUnreadReset?.(p.id); this.hide(p.id);
      const clients=Promise.allSettled([...p.clients].map(async client=>{
        try{client.close()}catch(error){await client.dispose();throw error}
        await client.dispose();p.clients.delete(client);
      }));
      // Opening pages are tracked before their first await, before they have a
      // CDP target. Destroy these too so pending registration can be cancelled.
      const views=new Set([...p.pages.values()].map(page=>page.view));
      for(const view of this.pendingPages.get(p)||[])views.add(view);
      for(const view of views)this.cancelPendingPage.get(view)?.();
      const pages=await Promise.allSettled([...views].map(view=>new Promise<void>((resolve,reject)=>{
        const wc=view.webContents;const forget=()=>{const pending=this.pendingPages.get(p);pending?.delete(view);if(pending&&!pending.size)this.pendingPages.delete(p)};
        if(!wc||wc.isDestroyed()){forget();resolve();return;}
        const done=()=>{clearTimeout(timer);forget();resolve()};
        const timer=setTimeout(()=>{wc.removeListener('destroyed',done);reject(new Error('窗口尚未完全关闭，请稍后重试'))},5000);
        wc.once('destroyed',done);
        try{wc.close({waitForBeforeUnload:false})}catch(error){clearTimeout(timer);wc.removeListener('destroyed',done);reject(error)}
      })));
      const clientResults=await clients;
      await Promise.allSettled([...(this.sessionWrites.get(p)||[])]);
      // Even a failed close on one transport must still attempt persistence.
      await p.session.cookies.flushStore();p.session.flushStorageData();
      for(const result of [...pages,...clientResults])if(result.status==='rejected')throw result.reason;
      if (this.profiles.get(p.id) === p) this.profiles.delete(p.id);
    };
    this.retiringProfiles.set(p.id,(this.retiringProfiles.get(p.id)||0)+1);
    const promise=close();this.closing.set(p,promise);
    try{await promise}finally{this.closing.delete(p);const remaining=(this.retiringProfiles.get(p.id)||1)-1;if(remaining)this.retiringProfiles.set(p.id,remaining);else this.retiringProfiles.delete(p.id)}
  }
  async stop() {
    this.stopping=true;
    this.watchShield?.close();this.watchShield=undefined;
    if (this.expiry) clearInterval(this.expiry);
    if (this.unreadTimer) clearInterval(this.unreadTimer);
    this.hide();
    // Close already-published profiles now: destroying their pending views
    // releases opens stuck in debugger registration. Also join session opens
    // that haven't published a profile yet, then sweep any late publication.
    const initial=new Set(this.profiles.values());
    const closing=Promise.allSettled([...initial].map(p=>this.closeProfile(p)));
    await Promise.allSettled([...this.opening.values()]);
    const closeResults=[...await closing,...await Promise.allSettled([...this.profiles.values()].filter(p=>!initial.has(p)).map(p=>this.closeProfile(p)))];
    for (const ws of this.sockets.clients) ws.terminate();
    this.sockets.close();
    await new Promise<void>(resolve => this.server ? this.server.close(() => resolve()) : resolve());
    // Finish shared transport cleanup as well, then preserve the close failure
    // for the caller after every account has completed its own cleanup attempt.
    for (const result of closeResults) if (result.status === "rejected") throw result.reason;
  }
}
