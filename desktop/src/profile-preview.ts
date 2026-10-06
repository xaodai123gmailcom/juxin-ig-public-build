import type { WebContents } from 'electron';
import { normalizedInstagramProfilePreviewUrl } from './navigation-policy.js';

type PreviewRect={x:number;y:number;width:number;height:number};
export type ReviewPreviewAnchor={left:number;top:number;avatarLeft:number};
/** Reads only the local review layout, never the remote profile or account data. */
export const reviewPreviewAnchorScript=`(() => {
 const panel=document.querySelector('.public-review-list[open]');
 const avatar=[...(panel?.querySelectorAll('.quick-review-avatar-head,.quick-review-avatar-link')||[])].find(el=>el.getClientRects().length);
 if(!panel||!avatar||!avatar.getClientRects().length)return null;
 const p=panel.getBoundingClientRect(),a=avatar.getBoundingClientRect();
 if(a.left-p.left<320)return null;
 return {left:p.left,top:Math.max(78,p.top),avatarLeft:a.left};
})()`;
export function reviewPreviewBounds(parent:PreviewRect,area:PreviewRect,anchor?:ReviewPreviewAnchor|null):PreviewRect {
 const valid=anchor&&[anchor.left,anchor.top,anchor.avatarLeft].every(Number.isFinite)&&anchor.left>=0&&anchor.avatarLeft-anchor.left>=320;
 const left=parent.x+(valid?anchor.left:Math.min(116,parent.width*.1));
 const right=Math.min(area.x+area.width,valid?parent.x+anchor.avatarLeft-12:parent.x+parent.width-12);
 const width=Math.round(Math.min(valid?950:Math.min(700,parent.width*.4),Math.max(320,right-Math.max(area.x,left)),area.width));
 const x=Math.round(Math.max(area.x,Math.min(left,right-width)));
 const top=parent.y+(valid?anchor.top:Math.min(276,parent.height*.32));
 const height=Math.round(Math.min(640,Math.max(260,parent.y+parent.height-top-16),area.height));
 const y=Math.round(Math.max(area.y,Math.min(top,area.y+area.height-height)));
 return {x,y,width,height};
}

export function profilePreviewUrl(username: unknown, action: unknown): string {
  if (typeof username !== 'string' || !/^[A-Za-z0-9._]{1,30}$/.test(username)) throw new Error('目标账号无效');
  const target = normalizedInstagramProfilePreviewUrl(`https://www.instagram.com/${username}/`);
  if (!target || !['target', 'login'].includes(String(action))) throw new Error('目标账号或预览操作无效');
  return action === 'login' ? 'https://www.instagram.com/accounts/login/' : target;
}

export function previewFailureMessage(status: number, title = '', networkError = ''): string {
  if (status === 401) return 'Instagram 要求登录。请点击“登录 / 检查”，完成后重新打开目标主页。';
  if (status === 403) return 'Instagram 或代理拒绝了请求（HTTP 403）。请检查当前窗口的登录、验证提示和代理设置。';
  if (status === 404) return '目标主页不存在或当前账号无法查看（HTTP 404）。请核对目标用户名。';
  if (status === 429) return '请求过于频繁（HTTP 429）。请稍后手动重试。';
  if (status >= 400) return `网页请求失败（HTTP ${status}）。请检查登录状态或网络后重新加载。`;
  if (/\b[45]xx\s+(?:Client|Server)\s+Error\b/i.test(title)) return '网页返回了错误页面。请检查当前窗口的登录状态和代理设置，再重新加载。';
  if (networkError) return networkError.includes('timeout') ? '目标主页加载超时，请检查网络后重新加载。' : '目标主页加载失败，请检查网络或代理后重新加载。';
  return '';
}

export async function loadProfilePreview(contents: WebContents, url: string, readyEvent:'did-finish-load'|'did-navigate'='did-finish-load') {
  // Ignore completion of the initial about:blank navigation. loadURL's promise
  // alone may resolve on that old did-finish-load while the target is starting.
  contents.stop();
  await new Promise<void>(resolve => setImmediate(resolve));
  let status = 0, committed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let finish!: () => void;
  let fail!: (error: Error) => void;
  const done = new Promise<void>((resolve, reject) => { finish = resolve; fail = reject; });
  const navigated = (_event: Electron.Event, value: string, code: number) => {
    if (!/^https?:/.test(value)) return;
    committed = true; status = code;
    if(readyEvent==='did-navigate')finish();
  };
  const finished = () => { if (committed) finish(); };
  const failed = (_event: Electron.Event, code: number, reason: string, value: string, main: boolean) => {
    if (main && code !== -3 && (value === url || committed)) fail(new Error(reason));
  };
  const destroyed = () => fail(new Error('preview closed'));
  contents.on('did-navigate', navigated);
  if(readyEvent==='did-finish-load')contents.on('did-finish-load',finished);
  contents.on('did-fail-load', failed);
  contents.on('destroyed', destroyed);
  timer = setTimeout(() => {
    fail(new Error('preview timeout'));
    // A manual login page must keep loading its application resources even
    // when the shell's navigation wait expires.
    if (readyEvent==='did-finish-load' && !contents.isDestroyed()) contents.stop();
  }, 25000);
  let networkError = '';
  try {
    void contents.loadURL(url).catch(error => {
      if (!String(error).includes('ERR_ABORTED')) fail(error);
    });
    await done;
  } catch (error) {
    networkError = error instanceof Error ? error.message : String(error);
  } finally {
    clearTimeout(timer);
    if (!contents.isDestroyed()) {
      contents.removeListener('did-navigate', navigated);
      if(readyEvent==='did-finish-load')contents.removeListener('did-finish-load',finished);
      contents.removeListener('did-fail-load', failed);
      contents.removeListener('destroyed', destroyed);
    }
  }
  const message = previewFailureMessage(status, contents.isDestroyed() ? '' : contents.getTitle(), networkError);
  return { page_loaded: !message, http_status: status > 0 ? status : null, message };
}
