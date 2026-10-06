/** Account-scoped Playwright CDP transport over Electron page debuggers.
 * Each connection gets its own real CDP sessions, including isolated iframe
 * sessions. The application UI and other accounts are never exposed as targets.
 */
import { randomBytes } from 'node:crypto';
import { WebSocket } from 'ws';
import { permittedPageUrl, type EmbeddedBrowserHost, type EmbeddedProfile, type AccountPage } from './embedded-browser.js';
type Binding = { page: AccountPage; parent?: string };
const pageDomains = new Set(['Page','Runtime','DOM','DOMSnapshot','CSS','Accessibility','Network','Fetch','Input','Emulation','Log','Console','Inspector','Performance','Security','Debugger','Profiler','HeapProfiler','Overlay','IO']);
// All connections to one physical page share its destruction notification.
// Concurrent CDP calls are normal (Playwright initializes many domains at once).
// Keep the real destroyed fence without one EventEmitter listener per request.
const pageDestructionObservers = new WeakMap<Electron.WebContents, { callbacks: Set<() => void>; closed: () => void }>();
function observePageDestruction(wc: Electron.WebContents, callback: () => void): () => void {
  let entry = pageDestructionObservers.get(wc);
  if (!entry) {
    const callbacks = new Set<() => void>();
    const closed = () => {
      pageDestructionObservers.delete(wc);
      wc.removeListener('destroyed', closed);
      const pending = [...callbacks]; callbacks.clear();
      for (const notify of pending) notify();
    };
    entry = { callbacks, closed };
    pageDestructionObservers.set(wc, entry);
    wc.once('destroyed', closed);
  }
  const current = entry;
  current.callbacks.add(callback);
  return () => {
    current.callbacks.delete(callback);
    if (!current.callbacks.size && pageDestructionObservers.get(wc) === current) {
      wc.removeListener('destroyed', current.closed);
      pageDestructionObservers.delete(wc);
    }
  };
}
export class AccountCdpConnection {
  private sessions = new Map<string, Binding>();
  private roots = new Map<string, string>();
  private attaching = new Map<string, Promise<string>>();
  private browserSessions = new Set<string>();
  private auto = false;
  private disposed = false;
  private disposing?: Promise<void>;
  private pending = new Set<Promise<unknown>>();
  private detaching = new Map<string, Promise<void>>();
  private createdPages = new Set<AccountPage>();
  private closingPages = new Map<AccountPage, Promise<void>>();
  constructor(private host: EmbeddedBrowserHost, private profile: EmbeddedProfile, private ws: WebSocket) {
    ws.on('message', raw => {
      let message: any;
      try { message = JSON.parse(raw.toString()); } catch { ws.close(1003); return; }
      void this.command(message.method, message.params || {}, message.sessionId).then(result => this.send({id:message.id, sessionId:message.sessionId, result}), error => this.send({id:message.id, sessionId:message.sessionId, error:{code:-32000,message:error instanceof Error ? error.message : '命令失败'}}));
    });
  }
  private send(message: object) { if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message)); }
  private info(page: AccountPage) { return { targetId: page.targetId, browserContextId: `account-${this.profile.id}`, type: 'page', title: page.view.webContents.getTitle(), url: page.view.webContents.getURL() || 'about:blank', attached: true, canAccessOpener: false, ...(page.openerId && this.profile.pages.has(page.openerId) ? {openerId:page.openerId} : {}) }; }
  private assertOpen() { if (this.disposed || this.profile.closed) throw new Error('账号窗口已关闭'); }
  private track<T>(operation: () => Promise<T>): Promise<T> {
    // Register before invoking code that can synchronously close this socket.
    const promise = Promise.resolve().then(operation);
    this.pending.add(promise);
    void promise.then(() => this.pending.delete(promise), () => this.pending.delete(promise));
    return promise;
  }
  private pageCommand(page: AccountPage, method: string, params: any, sessionId?: string): Promise<any> {
    const wc = page.view.webContents;
    if (wc.isDestroyed()) return Promise.reject(new Error('目标页面已关闭'));
    return new Promise((resolve, reject) => {
      const stopObserving = observePageDestruction(wc, () => reject(new Error('目标页面已关闭')));
      // Destruction is authoritative even when Electron leaves an IPC promise
      // pending. Keep both late success and rejection handled after that event.
      void Promise.resolve().then(() => {
        if (wc.isDestroyed()) throw new Error('目标页面已关闭');
        return wc.debugger.sendCommand(method, params, sessionId);
      }).then(value => { stopObserving(); resolve(value); }, error => { stopObserving(); reject(error); });
    });
  }
  attach(page: AccountPage, duplicate = false): Promise<string> {
    try { this.assertOpen(); } catch (error) { return Promise.reject(error); }
    if (!duplicate) {
      const existing = this.roots.get(page.targetId); if (existing) return Promise.resolve(existing);
      const pending = this.attaching.get(page.targetId); if (pending) return pending;
    }
    const operation = async () => {
      this.assertOpen();
      if (this.profile.pages.get(page.targetId) !== page || page.view.webContents.isDestroyed()) throw new Error('目标页面已关闭');
      const result = await this.pageCommand(page, 'Target.attachToTarget', { targetId: page.targetId, flatten: true });
      const sessionId = result.sessionId as string;
      this.sessions.set(sessionId, { page });
      if (this.disposed || this.profile.closed || this.profile.pages.get(page.targetId) !== page || page.view.webContents.isDestroyed()) {
        await this.detachSession(sessionId); throw new Error('连接或目标页面已关闭');
      }
      if (!duplicate) this.roots.set(page.targetId, sessionId);
      if (!duplicate) this.send({ method: 'Target.attachedToTarget', params: { sessionId, targetInfo: this.info(page), waitingForDebugger: false } });
      return sessionId;
    };
    const promise = this.track(operation);
    if (!duplicate) this.attaching.set(page.targetId, promise);
    const settled = () => { if (!duplicate && this.attaching.get(page.targetId) === promise) this.attaching.delete(page.targetId); };
    void promise.then(settled, settled);
    return promise;
  }
  event(page: AccountPage, method: string, params: any, sessionId?: string) {
    if (!sessionId || this.sessions.get(sessionId)?.page !== page) return;
    if (method === 'Target.detachedFromTarget') this.forgetSession(params.sessionId);
    if (this.disposed) return;
    if (method === 'Target.attachedToTarget') this.sessions.set(params.sessionId, { page, parent: sessionId });
    this.send({ method, params, sessionId });
  }
  async pageCreated(page: AccountPage) { if (this.auto) await this.attach(page); }
  pageClosed(page: AccountPage) {
    for (const [sessionId, binding] of this.sessions) if (binding.page === page) {
      this.send({ method: 'Target.detachedFromTarget', params: { sessionId, targetId: page.targetId } }); this.sessions.delete(sessionId);
    }
    this.roots.delete(page.targetId);
  }
  private ownPage(id: string) { const page = this.profile.pages.get(id); if (!page) throw new Error('目标不属于该账号'); return page; }
  command(method: string, params: any, sessionId?: string): Promise<any> {
    return this.track(() => this.runCommand(method, params, sessionId));
  }
  private async runCommand(method: string, params: any, sessionId?: string): Promise<any> {
    this.assertOpen();
    if (method === 'Target.attachToBrowserTarget') {
      const id = 'account-browser-' + randomBytes(16).toString('hex'); this.browserSessions.add(id);
      this.send({method:'Target.attachedToTarget',params:{sessionId:id,targetInfo:{targetId:`account-${this.profile.id}`,type:'browser',title:'Account',url:'',attached:true},waitingForDebugger:false}});
      return {sessionId:id};
    }
    if (method === 'Target.detachFromTarget' && this.browserSessions.delete(params.sessionId)) return {};
    if (sessionId && this.browserSessions.has(sessionId)) sessionId = undefined;
    const binding = sessionId ? this.sessions.get(sessionId) : undefined;
    if (sessionId && !binding) throw new Error('目标会话不属于该账号');
    if (params.browserContextId && params.browserContextId !== `account-${this.profile.id}`) throw new Error('不允许切换账号上下文');
    params = {...params}; delete params.browserContextId;
    if (method === 'Browser.getVersion') return { protocolVersion: '1.3', product: `Chrome/${process.versions.chrome}`, revision: '', userAgent: this.profile.session.getUserAgent(), jsVersion: process.versions.v8 };
    if (method === 'Browser.setDownloadBehavior') return {}; // Electron owns download UI; never apply browser-global settings.
    if (method === 'Browser.setPermission' || method === 'Browser.grantPermissions' || method === 'Browser.resetPermissions') {
      // Notifications are already denied on this Session, independent of other accounts.
      if (method === 'Browser.setPermission' && params.setting === 'denied') return {};
      if (method === 'Browser.resetPermissions') return {};
      throw new Error('当前内置会话未授权该浏览器权限');
    }
    if (method === 'Browser.close') {
      setImmediate(() => { void Promise.resolve().then(() => this.host.closeProfile(this.profile)).catch(error => this.host.recordConnectionFailure(this.profile, error)); });
      return {};
    }
    if (method === 'Storage.getCookies') {
      const cookies = await this.profile.session.cookies.get({}); this.assertOpen();
      return { cookies: cookies.map(c => ({ ...c, expires: c.expirationDate ?? -1, size: c.name.length + c.value.length, priority: 'Medium', sameParty: false, sourceScheme: c.secure ? 'Secure' : 'NonSecure', sameSite: ({unspecified:'None',no_restriction:'None',lax:'Lax',strict:'Strict'} as Record<string,string>)[c.sameSite] || 'None' })) };
    }
    if (method === 'Storage.setCookies') {
      for (const c of params.cookies || []) {
        const domain = String(c.domain || (c.url ? new URL(c.url).hostname : ''));
        const url = c.url || `${c.secure ? 'https' : 'http'}://${domain.replace(/^\./, '')}${c.path || '/'}`;
        if (!permittedPageUrl(url)) throw new Error('Cookie 地址无效');
        await this.profile.session.cookies.set({ url, name: c.name, value: c.value, ...(c.domain ? {domain:c.domain} : {}), path:c.path || '/', secure:c.secure, httpOnly:c.httpOnly, ...(c.expires > 0 ? {expirationDate:c.expires} : {}), sameSite: ({None:'no_restriction',Lax:'lax',Strict:'strict'} as any)[c.sameSite] || 'unspecified' });
        this.assertOpen();
      }
      await this.profile.session.cookies.flushStore(); this.assertOpen(); return {};
    }
    if (method === 'Storage.clearCookies') {
      const cookies = await this.profile.session.cookies.get({}); this.assertOpen();
      for (const c of cookies) { await this.profile.session.cookies.remove(`${c.secure ? 'https' : 'http'}://${(c.domain || '').replace(/^\./,'')}${c.path}`, c.name); this.assertOpen(); }
      return {};
    }
    if (method === 'Target.getBrowserContexts') return { browserContextIds: [] };
    if (method === 'Target.getTargets') return { targetInfos: [...this.profile.pages.values()].map(p => this.info(p)) };
    if (method === 'Target.getTargetInfo') return { targetInfo: params.targetId ? this.info(this.ownPage(params.targetId)) : binding ? this.info(binding.page) : { targetId:`account-${this.profile.id}`, type:'browser', title:'Account', url:'', attached:true } };
    if (method === 'Target.setDiscoverTargets') return {};
    if (method === 'Target.setAutoAttach' && !sessionId) {
      this.auto = Boolean(params.autoAttach);
      if (this.auto) await Promise.all([...this.profile.pages.values()].map(p => this.attach(p)));
      this.assertOpen();
      return {};
    }
    if (method === 'Target.createTarget') {
      const page = await this.host.newPage(this.profile, params.url || 'about:blank');
      this.createdPages.add(page);
      try {
        this.assertOpen(); await this.attach(page); this.assertOpen();
        this.createdPages.delete(page); return { targetId: page.targetId };
      } catch (error) { await this.closeCreatedPage(page); throw error; }
    }
    if (method === 'Target.closeTarget') { this.ownPage(params.targetId).view.webContents.close({waitForBeforeUnload:false}); return { success:true }; }
    if (method === 'Target.activateTarget' || method === 'Page.bringToFront') { const page = method.startsWith('Target.') ? this.ownPage(params.targetId) : binding?.page; if (!page) throw new Error('缺少页面会话'); this.host.activate(this.profile,page); return {}; }
    if (method === 'Target.attachToTarget') return { sessionId: await this.attach(this.ownPage(params.targetId), true) };
    if (method === 'Target.detachFromTarget') {
      const target = this.sessions.get(params.sessionId); if (!target) throw new Error('会话不属于该账号');
      await this.detachSession(params.sessionId); this.assertOpen();
      this.send({method:'Target.detachedFromTarget',params:{sessionId:params.sessionId}}); return {};
    }
    if (!binding) throw new Error(`不支持账号根命令 ${method}`);
    const domain = method.split('.')[0];
    if (!(pageDomains.has(domain) || method === 'Target.setAutoAttach')) throw new Error(`不支持账号页面命令 ${method}`);
    if (method === 'Page.navigate' && !permittedPageUrl(params.url)) throw new Error('不允许打开本机文件或软件内部页面');
    if (method === 'Network.loadNetworkResource' && !permittedPageUrl(params.url)) throw new Error('资源地址无效');
    // Electron window.open must complete before another debugger can attach.
    // Pausing a new popup here would block the main thread inside its callback.
    if (method === 'Target.setAutoAttach') params.waitForDebuggerOnStart = false;
    const result = await this.pageCommand(binding.page, method, params, sessionId);
    this.assertOpen(); return result;
  }
  private forgetSession(sessionId: string) {
    this.sessions.delete(sessionId);
    for (const [targetId, root] of this.roots) if (root === sessionId) this.roots.delete(targetId);
    for (const [child, binding] of this.sessions) if (binding.parent === sessionId) this.forgetSession(child);
  }
  private detachSession(sessionId: string): Promise<void> {
    const existing = this.detaching.get(sessionId); if (existing) return existing;
    const binding = this.sessions.get(sessionId); if (!binding) return Promise.resolve();
    const operation = Promise.resolve().then(async () => {
      const wc = binding.page.view.webContents;
      if (wc && !wc.isDestroyed()) {
        try { await this.pageCommand(binding.page, 'Target.detachFromTarget', { sessionId }); }
        catch (error) { if (!wc.isDestroyed() && this.sessions.has(sessionId)) throw error; }
      }
      this.forgetSession(sessionId);
    });
    this.detaching.set(sessionId, operation);
    const settled = () => { if (this.detaching.get(sessionId) === operation) this.detaching.delete(sessionId); };
    void operation.then(settled, settled);
    return operation;
  }
  private closeCreatedPage(page: AccountPage): Promise<void> {
    const existing = this.closingPages.get(page); if (existing) return existing;
    const wc = page.view.webContents;
    const operation = new Promise<void>((resolve, reject) => {
      const done = () => { this.createdPages.delete(page); resolve(); };
      if (!wc || wc.isDestroyed()) { done(); return; }
      wc.once('destroyed', done);
      try { wc.close({ waitForBeforeUnload: false }); }
      catch (error) { wc.removeListener('destroyed', done); if (wc.isDestroyed()) done(); else reject(error); }
    });
    this.closingPages.set(page, operation);
    const settled = () => { if (this.closingPages.get(page) === operation) this.closingPages.delete(page); };
    void operation.then(settled, settled);
    return operation;
  }
  close(): Promise<void> { this.ws.close(); return this.dispose(); }
  dispose(): Promise<void> {
    if (this.disposing) return this.disposing;
    this.disposed = true; this.auto = false;
    const operation = Promise.resolve().then(async () => {
      const attempted = new Set<string>(), attemptedPages = new Set<AccountPage>();
      const cleanup = () => {
        const work: Promise<void>[] = [];
        for (const [id, binding] of this.sessions) if (!binding.parent && !attempted.has(id)) { attempted.add(id); work.push(this.detachSession(id)); }
        for (const page of this.createdPages) if (!attemptedPages.has(page)) { attemptedPages.add(page); work.push(this.closeCreatedPage(page)); }
        return Promise.allSettled(work);
      };
      // Begin detach/owned-page close before waiting: they can unblock native
      // commands. Late attach/create results remain tracked and reclaimable.
      const early = cleanup();
      const checked = (results: PromiseSettledResult<void>[]) => { for (const result of results) if (result.status === 'rejected') throw result.reason; };
      await Promise.all([
        early.then(checked),
        (async () => { while (this.pending.size) await Promise.allSettled([...this.pending]); })(),
      ]);
      checked(await cleanup());
      this.sessions.clear(); this.roots.clear(); this.browserSessions.clear();
    });
    this.disposing = operation;
    // A failed detach retains its binding and profile occupancy. An explicit
    // retry may finish cleanup; the closed connection never accepts new work.
    void operation.then(undefined, () => { if (this.disposing === operation) this.disposing = undefined; });
    return operation;
  }
}
