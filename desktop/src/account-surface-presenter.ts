import type { BrowserWindow } from 'electron';
import type { EmbeddedBrowserHost } from './embedded-browser.js';
import { surfaceRectangle, type SurfaceInput } from './account-surface-policy.js';

/** Renderer lifetime and route checks precede the original Core lease check. */
export class AccountSurfacePresenter {
  private queue: Promise<unknown> = Promise.resolve();
  private active?:{surfaceId?:string;id?:string;documentUrl?:string;target?:string;readOnly?:boolean};
  private pendingAuthorization?:AbortController;
  private cancelAuthorization(){const pending=this.pendingAuthorization;this.pendingAuthorization=undefined;pending?.abort()}
  currentPlanId(){return this.active?.id;}
  constructor(private host: EmbeddedBrowserHost,
    private session: () => string | null,
    private authorize: (body: Record<string, unknown>, token: string, signal: AbortSignal) => Promise<{attached: boolean; message?: string}>) {}

  update(win: BrowserWindow, input: SurfaceInput) {
    // Renderer cleanup can arrive after native window destruction. Inspect
    // lifetime before reading bounds, zoom or URL from an Electron object.
    const contents = win.isDestroyed() ? null : win.webContents;
    const alive = () => !win.isDestroyed() && contents != null && win.webContents === contents && !contents.isDestroyed();
    if (!contents || !alive()) { this.cancelAuthorization();this.host.hide(); return Promise.resolve({attached: false}); }
    const rectangle = surfaceRectangle(input, win.getContentBounds(), contents.getZoomFactor());
    // A hide is local and immediate. Never queue it behind a slow Core request,
    // or send a delayed hide to Core which could revoke a newer view's grant.
    if (!input.visible || !rectangle || win.isMinimized()) {
      // Unmount cleanup from another account/preview must not hide the new view.
      if(input.surfaceId&&this.active?.surfaceId&&input.surfaceId!==this.active.surfaceId)return Promise.resolve({attached:false});
      if(input.documentUrl&&input.documentUrl!==contents.getURL())return Promise.resolve({attached:false});
      const preview=input.capture?this.host.captureSurface():Promise.resolve('');
      this.cancelAuthorization();this.active=undefined;this.host.hide();
      return preview.then(image=>({attached:false,preview:image}));
    }
    const authorization = this.session();
    if (!authorization) { this.cancelAuthorization();this.host.hide(); return Promise.reject(new Error('请先登录软件')); }
    if (input.documentUrl !== contents.getURL()) return Promise.resolve({attached: false});
    // The old account must not remain interactive beneath a newly selected
    // account while Core authorizes its replacement. Heartbeats keep the same
    // page visible and continue through the normal lease check.
    if(this.active&&(this.active.id!==input.id||this.active.target!==input.viewTarget||this.active.readOnly!==(input.readOnly===true)))this.host.hide();
    this.active={surfaceId:input.surfaceId,id:input.id,documentUrl:input.documentUrl,target:input.viewTarget,readOnly:input.readOnly===true};
    this.cancelAuthorization();
    const controller=new AbortController();this.pendingAuthorization=controller;
    this.host.setWorkspaceBounds(rectangle);
    const grant = this.host.requestSurface();
    const current = () => !controller.signal.aborted && alive() && this.session() === authorization &&
      input.documentUrl === contents.getURL() && this.host.hasSurfaceGrant(grant);
    const perform = async () => {
      if (!current()) return {attached: false};
      let aborted:()=>void=()=>{};
      const cancelled=new Promise<{attached:boolean}>(resolve=>{aborted=()=>resolve({attached:false});controller.signal.addEventListener('abort',aborted,{once:true})});
      try {
        const result = await Promise.race([this.authorize({id: input.id, visible: true, bounds: rectangle, grant, interference_grant:input.interferenceGrant,view_target:input.viewTarget,read_only:input.readOnly===true}, authorization, controller.signal),cancelled]);
        if (current()) return result;
        if (this.host.hasSurfaceGrant(grant)) this.host.hide();
        return {attached: false};
      } catch (error) {
        if (!current()) return {attached: false};
        this.host.hide(); throw error;
      } finally {
        controller.signal.removeEventListener('abort',aborted);
        if(this.pendingAuthorization===controller)this.pendingAuthorization=undefined;
      }
    };
    const result = this.queue.then(perform, perform);
    this.queue = result.catch(() => {});
    return result;
  }
}
