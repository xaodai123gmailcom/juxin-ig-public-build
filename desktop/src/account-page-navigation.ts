import type { WebContents } from 'electron';
import { previewFailureMessage } from './profile-preview.js';

/** Operates on the selected page, including a login or popup tab. Core holds
 * the ordinary exclusive account lease for the whole navigation. */
export async function navigateAccountPage(contents: WebContents, action: unknown, homeUrl: unknown) {
  if (!['refresh', 'back', 'forward'].includes(String(action))) throw new Error('无效网页操作');
  if (typeof homeUrl !== 'string' || !/^https:\/\//.test(homeUrl)) throw new Error('平台主页地址无效');
  const history = contents.navigationHistory;
  if (action === 'back' && !history.canGoBack() || action === 'forward' && !history.canGoForward()) {
    return {page_loaded: true, navigated: false, message: action === 'back' ? '已经是第一页' : '已经是最后一页'};
  }
  const entry = action === 'refresh' ? null : history.getEntryAtIndex(history.getActiveIndex() + (action === 'back' ? -1 : 1));
  if (entry && !/^(https?:|about:blank$)/.test(entry.url)) throw new Error('不能进入此历史页面');
  contents.stop();
  await new Promise<void>(resolve => setImmediate(resolve));
  let status = 0, committed = false, networkError = '';
  let finish!: () => void, fail!: (error: Error) => void;
  const done = new Promise<void>((resolve, reject) => {finish = resolve; fail = reject;});
  const navigated = (_event: Electron.Event, _url: string, code: number) => {committed = true; status = code;};
  const inPage = (_event: Electron.Event, url: string, main: boolean) => {if (main && entry?.url === url) finish();};
  const finished = () => {if (committed) finish();};
  const failed = (_event: Electron.Event, code: number, reason: string, _url: string, main: boolean) => {
    if (main && code !== -3) fail(new Error(reason));
  };
  const destroyed = () => fail(new Error('page closed'));
  contents.on('did-navigate', navigated); contents.on('did-navigate-in-page', inPage);
  contents.on('did-finish-load', finished); contents.on('did-fail-load', failed); contents.on('destroyed', destroyed);
  const timer = setTimeout(() => {fail(new Error('navigation timeout')); if (!contents.isDestroyed()) contents.stop();}, 25000);
  try {
    if (action === 'back') history.goBack();
    else if (action === 'forward') history.goForward();
    else if (/^https?:/.test(contents.getURL())) contents.reload();
    else void contents.loadURL(homeUrl).catch(fail);
    await done;
  } catch (error) {networkError = error instanceof Error ? error.message : String(error);}
  finally {
    clearTimeout(timer);
    contents.removeListener('did-navigate', navigated); contents.removeListener('did-navigate-in-page', inPage);
    contents.removeListener('did-finish-load', finished); contents.removeListener('did-fail-load', failed); contents.removeListener('destroyed', destroyed);
  }
  const message = previewFailureMessage(status, contents.isDestroyed() ? '' : contents.getTitle(), networkError);
  return {page_loaded: !message, navigated: true, http_status: status > 0 ? status : null, message};
}
