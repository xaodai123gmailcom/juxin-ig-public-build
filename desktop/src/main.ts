import {createAccountTaskControl} from './account-task-control.js';
import {ImmersiveTranslator} from './immersive-translator.js';
import {OptionalImmersiveSource} from './immersive-source.js';
import {verifyImmersiveSource} from './immersive-policy.js';
import {sendWindowEvent} from './window-events.js';
import {accountUserAgent} from './account-user-agent.js';
import {ReviewPage} from './review-page.js';
import {ChatGPTPage} from './chatgpt-page.js';
import {GoogleTranslator} from "./google-translator.js";
import {ChatTranslation} from "./chat-translation.js";
import {StorageManagement,accountCacheProfiles} from "./storage-management.js";
import {MessageReminders} from "./message-reminders.js";
import {ensureNotificationRegistration} from './notification-registration.js';
import {AccountContextMenu,type AccountMenuInput} from "./account-context-menu.js";
import { app, BrowserWindow, Notification, net, session, dialog, ipcMain, safeStorage, shell, crashReporter } from "electron";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { PRODUCT_NAME, STABLE_APPLICATION_ID, selectUserDataDirectory } from "./app-identity.js";
import { captureCoreLog } from "./core-log.js";
import { CoreSupervisor } from "./core-supervisor.js";
import { writePrivateFileAtomically } from "./atomic-secure-store.js";
import { CloudConfigurationController, readCloudConfiguration } from "./cloud-integration.js";
import { provisionOpenAI } from "./provision-integrations.js";
import { PexelsCredentialController, secureCredentialStorageAvailable } from "./pexels-integration.js";
import { CoreSessionFence, assertDirectCoreResponse, coreFetchRedirectMode, coreRequestTimeoutMs, isAllowedCoreRequest, normalizeCoreMethod, resumeTokenFromBody, sessionTokenFromResponse, shouldClearSessionTokenAfterRequest } from "./core-request-policy.js";
import { classifyRendererNavigation, isAllowedDevelopmentRendererUrl, normalizedExternalHttpsUrl, normalizedInstagramProfilePreviewUrl, normalizedInstagramPreviewNavigationUrl } from "./navigation-policy.js";

import { EmbeddedBrowserHost } from "./embedded-browser.js";
import { type SurfaceInput } from "./account-surface-policy.js";
import { AccountSurfacePresenter } from "./account-surface-presenter.js";

const originalUserData = app.getPath("userData");
const hasCollectorDatabase = (directory: string) => existsSync(join(directory, "data", "collector.sqlite3"));
const selectedUserData = selectUserDataDirectory(app.getPath("appData"), originalUserData,
  (directory) => hasCollectorDatabase(directory) || existsSync(join(directory,"secure-store.json")),
  hasCollectorDatabase);
mkdirSync(selectedUserData, { recursive: true });
app.setPath("userData", selectedUserData);
app.setName(PRODUCT_NAME);
// Apply the same wolf brand to the main window, tools and native login popups.
// The EXE and installer embed the multi-resolution ICO from these assets.
app.on("browser-window-created", (_event, win) => {
  win.setIcon(join(app.getAppPath(), "desktop", "assets", "war-wolf.png"));
});
// Preserve native main/renderer crash evidence on this machine. No upload endpoint.
crashReporter.start({uploadToServer:false,ignoreSystemCrashHandler:false});
// Workers and login popups can use Electron's global fallback instead of a
// page override. A localized product name is not a valid HTTP header value.
app.userAgentFallback=accountUserAgent(app.userAgentFallback);
app.commandLine.appendSwitch("disk-cache-size",String(500*1024*1024));
if (process.platform === "win32") app.setAppUserModelId(STABLE_APPLICATION_ID);

const corePort = Number(process.env.COLLECTOR_CORE_PORT || 17831);
const coreToken = randomBytes(32).toString("hex");
let activeSessionToken: string | null = null;
let activeSessionOwner: string | null = null;
let reviewPage: ReviewPage | undefined;
let configuredBitbrowserPort: string | null = null;
let mainWindow: BrowserWindow | null = null;
const accountContextMenu=new AccountContextMenu();
const embeddedBrowser = new EmbeddedBrowserHost(() => mainWindow);
const googleTranslator=new GoogleTranslator(visible=>{sendWindowEvent(mainWindow,"translator:visibility",visible)});
const chatgptPage=new ChatGPTPage(visible=>{sendWindowEvent(mainWindow,"chatgpt:visibility",visible)});
const immersiveInstallFolder=join(app.getPath('userData'),'optional-integrations');
const immersiveTranslator=new ImmersiveTranslator(()=>activeSessionOwner,()=>mainWindow,readSecureValue,writeSecureValue,new OptionalImmersiveSource(join(immersiveInstallFolder,'immersive-translate.user.js')));
const chatTranslation=new ChatTranslation(join(app.getPath('userData'),'chat-translation.json'),()=>activeSessionToken,()=>embeddedBrowser.chatTranslationTarget(),(id,step,authorization)=>accountWatchCore('/api/accounts/command',{action:'chat_translation',id,step},authorization),readSecureValue,writeSecureValue,{immersiveOnly:true,fetcher:(input,init)=>net.fetch(String(input),init),immersive:{install:installOptionalTranslation,translate:(...args)=>immersiveTranslator.translate(...args),settings:id=>immersiveTranslator.settings(id),cacheIdentity:id=>immersiveTranslator.cacheIdentity(id),status:id=>immersiveTranslator.status(id),retry:id=>immersiveTranslator.retry(id),reset:()=>immersiveTranslator.reset()}});
async function installOptionalTranslation() {
 const win=mainWindow,authorization=activeSessionToken;
 if(!win||win.isDestroyed()||!authorization)throw new Error('窗口来源无效');
 const selected=await dialog.showOpenDialog(win,{title:'选择自行取得的官方沉浸式翻译脚本',properties:['openFile'],filters:[{name:'用户脚本',extensions:['js']}]});
 if(selected.canceled||selected.filePaths.length!==1)return false;
 if(authorization!==activeSessionToken||win!==mainWindow||win.isDestroyed())throw new Error('登录状态已变化');
 // The chooser is explicit. Only the supported pinned official artifact can be
 // installed; unknown JavaScript never executes and is never copied into builds.
 let selectedBytes:Buffer;
 try {
  const info=statSync(selected.filePaths[0]);if(!info.isFile()||info.size>10_000_000)throw new Error('invalid file');
  selectedBytes=readFileSync(selected.filePaths[0]);
 } catch {throw new Error('无法读取所选翻译脚本，请确认文件有效并重试');}
 const source=verifyImmersiveSource(selectedBytes);
 try {
  mkdirSync(immersiveInstallFolder,{recursive:true});
  writePrivateFileAtomically(join(immersiveInstallFolder,'immersive-translate.user.js'),source);
 } catch {throw new Error('无法保存可选翻译组件，请检查本机存储后重试');}
 immersiveTranslator.reset();return true;
}
ipcMain.handle('chat:translation',async(event,input:any)=>{
 const win=mainWindow,authorization=activeSessionToken;
 if(!win||win.isDestroyed()||!authorization||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame||typeof input?.id!=='string'||input.id.length>128)throw new Error('窗口来源无效');
 return chatTranslation.command(input.id,input,authorization);
});
embeddedBrowser.onPageExit=details=>storageManagement.log('Account renderer exit: '+JSON.stringify(details));
app.on('child-process-gone',(_event,details)=>{
  if(details.reason==='clean-exit')return;
  storageManagement.log('Desktop child exit: '+JSON.stringify({type:details.type,reason:details.reason,exitCode:details.exitCode}));
});
embeddedBrowser.onSurfaceRendered=()=>{googleTranslator.raise();chatgptPage.raise()};embeddedBrowser.onEscape=()=>{if(chatgptPage.opened){chatgptPage.hide();return true}if(!googleTranslator.opened)return false;googleTranslator.hide();return true};
ipcMain.handle("translator:toggle",async(event,input:{hide?:boolean;top?:number;boundsOnly?:boolean})=>{const win=mainWindow;if(!win||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");if(input?.top!==undefined){if(!Number.isFinite(input.top)||input.top<0||input.top>10000)throw new Error("翻译器位置无效");googleTranslator.setTop(input.top*win.webContents.getZoomFactor())}if(input?.boundsOnly)return {visible:googleTranslator.opened};if(input?.hide){googleTranslator.hide();return {visible:false}}if(!googleTranslator.opened)chatgptPage.hide();return {visible:await googleTranslator.toggle(win)}});
ipcMain.handle("chatgpt:toggle",async(event,input:{hide?:boolean;top?:number;boundsOnly?:boolean})=>{const win=mainWindow;if(!win||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");if(input?.top!==undefined){if(!Number.isFinite(input.top)||input.top<0||input.top>10000)throw new Error("ChatGPT 窗口位置无效");chatgptPage.setTop(input.top*win.webContents.getZoomFactor())}if(input?.boundsOnly)return {visible:chatgptPage.opened};if(input?.hide){chatgptPage.hide();return {visible:false}}if(!activeSessionOwner)throw new Error("请重新登录软件");chatgptPage.setPartition("persist:chatgpt-"+activeSessionOwner);if(!chatgptPage.opened)googleTranslator.hide();return {visible:await chatgptPage.toggle(win)}});
function getReviewPage(){
 if(!activeSessionOwner)throw new Error('请重新登录软件');
 return reviewPage??=new ReviewPage('persist:review-'+activeSessionOwner,()=>{
  const win=mainWindow;if(!win||win.isDestroyed()||!win.webContents||win.webContents.isDestroyed())return;
  // This callback is main-owned. Remote review pages have no application IPC.
  void win.webContents.executeJavaScript("location.hash='/settings'").catch(()=>{});
 });
}
ipcMain.handle('review:account',async(event,input:unknown)=>{
 const win=mainWindow;
 if(!win||win.isDestroyed()||!activeSessionToken||!activeSessionOwner||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error('窗口来源无效');
 return getReviewPage().account(win,input);
});
const notificationFile=join(app.getPath("userData"),"message-notifications.json");
let notifyEnabled=true;try{notifyEnabled=JSON.parse(readFileSync(notificationFile,"utf8")).enabled!==false}catch{}
let notificationProfiles=new Set<string>(),notificationEpoch=0,notificationError="";
let notificationRegistered=false,notificationLastShown=0;
const toasts=new Map<string,Notification>();
const notificationActivations=new Map<string,object>();
function closeMessageToast(profile:string){
  const toast=toasts.get(profile);toasts.delete(profile);notificationActivations.delete(profile);
  // A native toast can already be gone while its Electron object still exists.
  // Revoke callbacks first so failed cleanup cannot keep an old account active.
  try{toast?.close()}catch{}
}
function closeMessageToasts(){for(const profile of new Set([...toasts.keys(),...notificationActivations.keys()]))closeMessageToast(profile)}
function clearNotificationSession(){chatTranslation.reset();googleTranslator.hide();chatgptPage.dispose();reviewPage?.dispose();reviewPage=undefined;activeSessionOwner=null;notificationProfiles.clear();notificationEpoch++;notificationError='';notificationLastShown=0;messageReminders.reset();closeMessageToasts()}
function sendMessageReminder(profile:string,count:number){
  if(!activeSessionToken||!Notification.isSupported()||!messageReminders.enabled)return;
  const epoch=notificationEpoch,p=embeddedBrowser.profiles.get(profile);
  if(profile!=="test"&&(!p||!notificationProfiles.has(profile)))return;
  if(!notificationRegistered){try{ensureNotificationRegistration({platform:process.platform,packaged:app.isPackaged,appData:app.getPath('appData'),executable:process.execPath},shell);notificationRegistered=true}catch(e){notificationError=e instanceof Error?e.message:'Windows 提醒注册失败';return}}
  closeMessageToast(profile);
  let toast:Notification|undefined;
  try{
    toast=new Notification({title:profile==="test"?"聚鑫国际 · 测试提醒":`新消息 · ${p?.name||"账号窗口"}`,body:profile==="test"?"消息提醒已开启。":count>0?"收到新消息，当前有 "+count+" 条未读提示。点击查看账号窗口。":"收到新消息，点击查看账号窗口。"});
    toasts.set(profile,toast);
    // Windows can close a toast while retaining its entry in Action Center.
    // Keep one activation token per profile without retaining the native toast;
    // replacement, disabling, removal and logout explicitly revoke that token.
    const activation={};notificationActivations.set(profile,activation);
    const current=()=>epoch===notificationEpoch&&Boolean(activeSessionToken)&&messageReminders.enabled&&notificationActivations.get(profile)===activation&&
      (profile==='test'||notificationProfiles.has(profile)&&embeddedBrowser.profiles.get(profile)===p&&!p?.closed);
    toast.on("click",()=>{
      if(!current())return;
      try{const win=mainWindow;if(!win||win.isDestroyed())return;if(win.isMinimized())win.restore();win.show();win.focus();if(profile!=="test")sendWindowEvent(win,"account:focus",profile)}
      catch{if(current())notificationError='打开消息窗口失败，请从账号页查看。'}
    });
    toast.on("failed",()=>{if(current())notificationError="系统未能显示通知，请检查 Windows 通知权限、开始菜单快捷方式和勿扰模式。"});
    toast.on('show',()=>{if(current()){notificationLastShown=Date.now();notificationError=''}});
    toast.on("close",()=>{if(toasts.get(profile)===toast)toasts.delete(profile)});
    toast.show();
  }catch{
    if(toast&&toasts.get(profile)===toast)closeMessageToast(profile);
    if(epoch===notificationEpoch)notificationError='系统通知发送失败，请在设置中发送测试提醒检查。';
  }
}
const messageReminders=new MessageReminders(notifyEnabled,sendMessageReminder,enabled=>writePrivateFileAtomically(notificationFile,JSON.stringify({enabled})));
embeddedBrowser.onUnread=(p,value)=>{if(activeSessionToken&&notificationProfiles.has(p.id))messageReminders.observe(p.id,value)};
embeddedBrowser.onUnreadReset=id=>messageReminders.forget(id);
ipcMain.handle("messages:settings",(event,input:{enabled?:boolean;test?:boolean})=>{
  const win=mainWindow;if(!win||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");
  if(!input||typeof input!=="object"||Object.keys(input).some(k=>!["enabled","test"].includes(k)))throw new Error("消息提醒设置无效");
  if(input.enabled!==undefined){if(typeof input.enabled!=="boolean")throw new Error("消息提醒设置无效");messageReminders.setEnabled(input.enabled);if(!input.enabled)closeMessageToasts()}
  if(input.test===true){if(!messageReminders.enabled)throw new Error("请先开启消息提醒");notificationError="";sendMessageReminder("test",0)}
  return {enabled:messageReminders.enabled,supported:Notification.isSupported(),error:notificationError,lastShownAt:notificationLastShown};
});
const storageManagement=new StorageManagement(app.getPath("userData"),app.isPackaged?process.resourcesPath:app.getAppPath(),app.getPath("crashDumps"),async()=>{const state=embeddedBrowser.cacheActivity();return accountCacheProfiles(app.getPath("userData"),state.busy,state.opened)},partition=>partition.startsWith('wa-path:')?session.fromPath(partition.slice(8)):session.fromPartition(partition));
process.on('uncaughtExceptionMonitor',(error,origin)=>{
  // Observe fatal exceptions without swallowing them or continuing a corrupt process.
  storageManagement.log('Main process fatal '+origin+' '+error.name+' '+(error.stack||'').split('\n').slice(1,6).join(' | '));
});
ipcMain.handle("storage:manage",async(event,input:Record<string,unknown>)=>{
  const win=mainWindow;if(!win||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");
  if(!input||typeof input!=="object"||Object.keys(input).some(k=>!["auto","clean"].includes(k)))throw new Error("存储操作无效");
  if(input.auto!==undefined){if(typeof input.auto!=="boolean")throw new Error("存储设置无效");storageManagement.setAuto(input.auto)}
  if(input.clean!==undefined){if(input.clean==="all"&&!googleTranslator.opened)googleTranslator.dispose();await storageManagement.clean(String(input.clean));}
  const result=await storageManagement.snapshot();return {...result,memory:app.getAppMetrics().reduce((sum,p)=>sum+p.memory.workingSetSize*1024,0)};
});
// Renderer code only needs its encrypted session token. Integration secrets are
// write-only from the renderer through the validated `core:configure` command
// and are read solely by this main process when it spawns Core.  Keeping them
// out of the generic secure-store IPC prevents a renderer compromise from
// extracting the BitBrowser/OpenAI credentials.
const rendererSecureSettingKeys = new Set(["session-token"]);

function coreBaseUrl() { return `http://127.0.0.1:${corePort}`; }

// Shared by every Core pipe, including a retiring generation. Runtime key
// updates append before activation; prior keys remain redacted until app exit.
const corePrivateValues: string[] = [];
function rememberCoreSecret(value: string) {
  if (value && !corePrivateValues.includes(value)) corePrivateValues.push(value);
}

function spawnCore() {
  const dataDir = join(app.getPath("userData"), "data");
  // This value is only V2's preferred first candidate.  The Python connection
  // manager is the sole owner of discovery and will safely scan its loopback
  // candidates when the saved port moved or BitBrowser was reconfigured.
  const bitbrowserPort = configuredBitbrowserPort || readSecureValue("bitbrowser-port") || "54345";
  const bitbrowserApiKey = readSecureValue("bitbrowser-api-key") || "";
  const openaiApiKey = provisionOpenAI(readSecureValue);
  const pexelsApiKey = secureCredentialStorageAvailable(safeStorage, process.platform)
    ? readSecureValue("pexels-api-key") || "" : "";
  const cloud = readCloudConfiguration(() => readSecureValue("cloud-configuration-v1"), secureCredentialStorageAvailable(safeStorage, process.platform));
  const env = {
    ...process.env,
    COLLECTOR_CORE_TOKEN: coreToken,
    COLLECTOR_CORE_PORT: String(corePort),
    COLLECTOR_DATA_DIR: dataDir,
    IGAC_STARTUP_TOKEN: coreToken,
    IGAC_PORT: String(corePort),
    IGAC_DATA_DIR: dataDir,
    IGAC_DESKTOP_DIR: app.getPath("desktop"),
    IGAC_PARENT_PID: String(process.pid),
    IGAC_EMBEDDED_BROWSER_URL: embeddedBrowser.url,
    IGAC_EMBEDDED_BROWSER_TOKEN: embeddedBrowser.token,
    IGAC_BITBROWSER_URL: `http://127.0.0.1:${bitbrowserPort}`,
    IGAC_BITBROWSER_API_KEY: bitbrowserApiKey,
    OPENAI_API_KEY: openaiApiKey,
    IGAC_PEXELS_API_KEY: pexelsApiKey,
    IGAC_CLOUD_ENABLED: cloud.enabled ? "1" : "0",
    IGAC_SUPABASE_URL: cloud.projectUrl,
    IGAC_SUPABASE_PUBLISHABLE_KEY: cloud.publishableKey,
    ...(app.isPackaged ? { IGAC_BROWSER_DIR: join(process.resourcesPath,"browsers"), PLAYWRIGHT_BROWSERS_PATH: join(process.resourcesPath,"browsers"), IGAC_RUNTIME_REQUIREMENT: join(process.resourcesPath,"browsers","juxin-runtime-requirement.json") } : {}),
  };
  const python = process.env.PYTHON_EXECUTABLE || (process.platform === "win32" ? "python" : "python3");
  const child = app.isPackaged
    ? spawn(join(process.resourcesPath, "backend", "collector_core", "collector_core.exe"), [], { env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] })
    : spawn(python, ["-m", "backend.main"], { cwd: app.getAppPath(), env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  for (const secret of [coreToken, embeddedBrowser.token, bitbrowserApiKey, openaiApiKey, pexelsApiKey, cloud.publishableKey]) rememberCoreSecret(secret);
  const privateValues = corePrivateValues;
  captureCoreLog(child.stdout, "stdout", line => storageManagement.log(line), privateValues);
  captureCoreLog(child.stderr, "stderr", line => storageManagement.log(line), privateValues);
  return child;
}

async function checkCoreHealth(signal: AbortSignal) {
  const response = await fetch(`${coreBaseUrl()}/api/health`, {
    headers: { "x-startup-token": coreToken },
    signal,
    redirect: coreFetchRedirectMode,
  });
  assertDirectCoreResponse(response);
  return response.ok;
}

function createWindow() {
  const win = new BrowserWindow({
    title: PRODUCT_NAME,
    width: 1600,
    height: 980,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: "#020d1c",
    show: false,
    webPreferences: {
      preload: join(app.getAppPath(), "dist-electron", "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow = win;
  win.on("resize",()=>{googleTranslator.raise();chatgptPage.raise()});win.webContents.on("did-start-navigation",()=>{googleTranslator.hide();chatgptPage.hide()});win.webContents.on("did-navigate-in-page",()=>{googleTranslator.hide();chatgptPage.hide()});
  embeddedBrowser.attachWindow(win);
  win.on("hide", () => embeddedBrowser.hide());
  win.on("minimize", () => embeddedBrowser.hide());
  win.on("closed", () => {embeddedBrowser.hide();googleTranslator.dispose();chatgptPage.dispose();reviewPage?.dispose()});
  win.webContents.on("did-start-navigation", (_event, _url, _inPlace, isMainFrame) => { if (isMainFrame) embeddedBrowser.hide(); });
  win.webContents.on("render-process-gone", () => embeddedBrowser.hide());
  win.on("page-title-updated", (event) => { event.preventDefault(); win.setTitle(PRODUCT_NAME); });
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl && !isAllowedDevelopmentRendererUrl(devUrl)) {
    throw new Error("开发渲染器必须使用 127.0.0.1 HTTP 地址");
  }
  const rendererFile = join(app.getAppPath(), "renderer", "dist", "index.html");
  const trustedRendererUrl = devUrl || pathToFileURL(rendererFile).href;
  const openExternalHttps = (url: string) => {
    const safeUrl = normalizedExternalHttpsUrl(url);
    if (!safeUrl) return;
    void shell.openExternal(safeUrl).catch((error) => console.error("无法打开外部 HTTPS 链接", error));
  };
  const openRendererLink = (url: string) => {
    if (normalizedInstagramPreviewNavigationUrl(url)) {
      const profileUrl = normalizedInstagramProfilePreviewUrl(url);
      if (profileUrl && activeSessionToken && activeSessionOwner) {
        googleTranslator.hide();chatgptPage.hide();
        reviewPage=getReviewPage();
        void reviewPage.openFromRenderer(win,new URL(profileUrl).pathname.split('/')[1]).catch(()=>{storageManagement.log('Review preview could not open')});
      }
      return;
    }
    openExternalHttps(url);
  };
  win.webContents.on("will-navigate", (event, url) => {
    const disposition = classifyRendererNavigation(url, trustedRendererUrl);
    if (disposition === "local") return;
    event.preventDefault();
    if (disposition === "external-https") openRendererLink(url);
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    openRendererLink(url);
    return { action: "deny" };
  });
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
  });
  win.once("ready-to-show", () => win.show());
  let rendererRecoveries: number[] = [];
  let recoveryPromptOpen = false;
  let unresponsiveTimer: ReturnType<typeof setTimeout> | undefined;
  const reloadRenderer = () => {
    if(quitShutdownStarted||win.isDestroyed())return;
    const contents=win.webContents;
    if(!contents||contents.isDestroyed())return;
    try{contents.reload()}catch{storageManagement.log('Renderer reload skipped after window destruction')}
  };
  const offerRendererRecovery = async () => {
    if (quitShutdownStarted||win.isDestroyed() || recoveryPromptOpen) return;
    recoveryPromptOpen = true;
    try {
      const result = await dialog.showMessageBox(win, {
        type: "warning", title: "界面暂时没有响应",
        message: "可以重新加载界面，已保存的采集记录和进度会保留。",
        buttons: ["重新加载界面", "继续等待"], defaultId: 0, cancelId: 1,
      });
      if (result.response === 0) reloadRenderer();
    } catch { storageManagement.log('Renderer recovery dialog closed with its owner'); }
    finally { recoveryPromptOpen = false; }
  };
  win.webContents.on("render-process-gone", (_event, details) => {
    storageManagement.log(`Main renderer exited: ${details.reason}; exitCode=${details.exitCode}`);
    if (quitShutdownStarted||details.reason === "clean-exit" || win.isDestroyed()) return;
    clearTimeout(unresponsiveTimer);
    const now = Date.now();
    rendererRecoveries = rendererRecoveries.filter((time) => now - time < 60_000);
    if (rendererRecoveries.length >= 2) { void offerRendererRecovery(); return; }
    rendererRecoveries.push(now);
    reloadRenderer();
  });
  win.on("unresponsive", () => {
    if (unresponsiveTimer) return;
    storageManagement.log('Main renderer unresponsive; waiting for recovery');
    unresponsiveTimer = setTimeout(() => {
      unresponsiveTimer = undefined;
      void offerRendererRecovery();
    }, 15_000);
  });
  win.on("responsive", () => { clearTimeout(unresponsiveTimer); unresponsiveTimer = undefined; });
  win.on("closed", () => clearTimeout(unresponsiveTimer));
  if (devUrl) void win.loadURL(devUrl);
  else void win.loadFile(rendererFile);
}

function requireMainSender(event: Electron.IpcMainInvokeEvent) {
  const win = mainWindow;
  if (!win || win.isDestroyed() || event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame) {
    throw new Error("窗口来源无效");
  }
}

const coreSessionFence = new CoreSessionFence();

ipcMain.handle("core:request", async (_event, input: { path: string; method?: string; body?: unknown }) => {
  requireMainSender(_event);
  if (!input) throw new Error("不允许访问该本机接口");
  const method = normalizeCoreMethod(input.method);
  if (!isAllowedCoreRequest(input.path, method)) throw new Error("不允许访问该本机接口");
  if (method === "GET" && input.body !== undefined) throw new Error("该请求不允许携带正文");
  const resumeToken = resumeTokenFromBody(input.path, input.body);
  const headers: Record<string, string> = { "content-type": "application/json", "x-collector-token": coreToken, "x-startup-token": coreToken };
  const authorizationToken = resumeToken || activeSessionToken;
  if (authorizationToken) headers.authorization = `Bearer ${authorizationToken}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), coreRequestTimeoutMs(input.path, input.body));
  const clearSessionAfterRequest = shouldClearSessionTokenAfterRequest(input.path);
  const sessionRevision = coreSessionFence.begin(input.path);
  // Capture the outgoing authorization above, then end local access immediately.
  // A slow old logout response must not clear a newer successful login.
  if (clearSessionAfterRequest) { clearNotificationSession();activeSessionToken = null; embeddedBrowser.hide(); }
  try {
    let response: Response;
    try {
      response = await fetch(`${coreBaseUrl()}${input.path}`, {
        method,
        headers,
        body: input.body === undefined ? undefined : JSON.stringify(input.body),
        signal: controller.signal,
        redirect: coreFetchRedirectMode,
      });
      assertDirectCoreResponse(response);
    } catch (error) {
      if (controller.signal.aborted) throw new Error("本机服务响应超时");
      throw error;
    }
    coreSessionFence.assertCurrent(sessionRevision);
    if (response.status === 204) return null;
    let payload;
    try {
      payload = await response.json();
    } catch {
      coreSessionFence.assertCurrent(sessionRevision);
      // The timeout covers the complete body too. A 200 response with a
      // stalled/invalid body must never be returned as successful command data.
      if (controller.signal.aborted) throw new Error("本机服务响应超时");
      throw new Error("本机服务返回了无效响应");
    }
    // JSON decoding is asynchronous too: another session may begin after headers.
    coreSessionFence.assertCurrent(sessionRevision);
    if (!response.ok) {
      if (input.path === "/api/session/resume") { clearNotificationSession();activeSessionToken = null;embeddedBrowser.hide(); }
      throw new Error(String(payload.detail || payload.message || `请求失败：${response.status}`));
    }
    const returnedToken = sessionTokenFromResponse(input.path, payload);
    if (returnedToken) {if(returnedToken!==activeSessionToken)clearNotificationSession();activeSessionToken = returnedToken;activeSessionOwner=typeof payload.user?.id==='string'&&/^[a-zA-Z0-9-]{1,80}$/.test(payload.user.id)?payload.user.id:null;}
    if(input.path==="/api/accounts/unread"&&authorizationToken===activeSessionToken){
      notificationProfiles=new Set(Object.keys(payload.windows||{}));
      for(const profile of notificationActivations.keys())if(profile!=='test'&&!notificationProfiles.has(profile))closeMessageToast(profile);
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
});

// The backend authorizes account ownership and task leases. The main process
// supplies a clipped DIP rectangle inside this window, never an external HWND.
const surfacePresenter = new AccountSurfacePresenter(embeddedBrowser, () => activeSessionToken, async (payload, authorization, signal) => {
  const response=await fetch(`${coreBaseUrl()}/api/accounts/surface`,{method:"POST",headers:{"content-type":"application/json","x-startup-token":coreToken,authorization:`Bearer ${authorization}`},body:JSON.stringify(payload),signal:AbortSignal.any([signal,AbortSignal.timeout(4000)]),redirect:coreFetchRedirectMode});
  assertDirectCoreResponse(response);
  const result=await response.json();
  if(!response.ok)throw new Error(String(result.detail||"无法显示内置网页"));
  return result;
});
async function accountWatchCore(path:string,payload:unknown,authorization:string,timeoutMs=12000){
  const response=await fetch(`${coreBaseUrl()}${path}`,{method:"POST",headers:{"content-type":"application/json","x-startup-token":coreToken,authorization:`Bearer ${authorization}`},body:JSON.stringify(payload),signal:AbortSignal.timeout(timeoutMs),redirect:coreFetchRedirectMode});
  assertDirectCoreResponse(response);const result=await response.json();if(!response.ok)throw new Error(String(result.detail||"任务画面暂不可用"));return result;
}
embeddedBrowser.onPageNavigation=async action=>{
  const id=surfacePresenter.currentPlanId(),authorization=activeSessionToken;
  if(!id||!authorization||!['back','forward','refresh'].includes(action))return;
  // The same Core lease used by the toolbar fences native-menu navigation.
  return accountWatchCore('/api/accounts/command',{id,action},authorization);
};
function taskWatchSender(event:Electron.IpcMainInvokeEvent,input:{id:string;documentUrl:string;target?:string}){
  const win=mainWindow;if(!win||win.isDestroyed()||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame||input?.documentUrl!==win.webContents.getURL()||typeof input.id!=="string"||input.id.length>128)throw new Error("窗口来源无效");return win;
}
ipcMain.handle("accounts:watch",async(event,input:{id:string;documentUrl:string;target?:string})=>{
  taskWatchSender(event,input);const authorization=activeSessionToken!;const result=await accountWatchCore("/api/accounts/watch",{id:input.id,target:input.target},authorization);
  taskWatchSender(event,input);if(authorization!==activeSessionToken)throw new Error("登录状态已变化");return result;
});
ipcMain.handle("accounts:close-task-page",async(event,input:{id:string;documentUrl:string;taskKey:string;target:string})=>{
  taskWatchSender(event,input);const authorization=activeSessionToken!;
  if(typeof input.target!=="string"||!input.target||input.target.length>128||typeof input.taskKey!=="string"||!/^[a-f0-9]{24}$/.test(input.taskKey))throw new Error("任务页面标识无效");
  const result=await accountWatchCore("/api/accounts/close-task-page",{id:input.id,task_key:input.taskKey,target:input.target},authorization);
  taskWatchSender(event,input);if(authorization!==activeSessionToken)throw new Error("登录状态已变化");return result;
});
const accountTaskControl=createAccountTaskControl({
  validate:(event,input)=>{taskWatchSender(event as Electron.IpcMainInvokeEvent,input)},
  session:()=>activeSessionToken,
  confirm:async()=>{const win=mainWindow;if(!win||win.isDestroyed())throw new Error('窗口已关闭');const result=await dialog.showMessageBox(win,{type:'question',title:'手动干预任务',message:'是否继续干扰？',detail:'确认后会先暂停此窗口的采集，停稳后允许手动操作。窗口仍由当前任务占用；操作完成后可在采集任务中点击继续。',buttons:['取消','继续干扰'],defaultId:0,cancelId:0,noLink:true});return result.response===1},
  hide:async input=>{const win=mainWindow;if(win&&surfacePresenter.currentPlanId()===input.id)await surfacePresenter.update(win,{id:input.id,documentUrl:input.documentUrl,visible:false})},
  request:(path,payload,authorization)=>accountWatchCore(path,payload,authorization,25000),
});
embeddedBrowser.onTaskInterference=target=>{
  const win=mainWindow;if(win&&!win.isDestroyed()&&activeSessionToken&&surfacePresenter.currentPlanId())win.webContents.send('account:interfere-request',target);
};
ipcMain.handle("accounts:interfere",accountTaskControl.begin);
ipcMain.handle("accounts:resume-task",accountTaskControl.resume);
ipcMain.handle("accounts:context-menu", async (event,input:AccountMenuInput) => {
  const win=mainWindow;
  if(!win||win.isDestroyed()||!activeSessionToken||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");
  const documentUrl=win.webContents.getURL(),sessionToken=activeSessionToken;
  const close=()=>accountContextMenu.close();
  win.webContents.on('did-start-navigation',close);win.webContents.on('did-navigate-in-page',close);win.once('closed',close);
  try {const action=await accountContextMenu.show(win,input);return !win.isDestroyed()&&documentUrl===win.webContents.getURL()&&sessionToken===activeSessionToken?action:null}
  finally {if(!win.isDestroyed()){win.webContents.removeListener('did-start-navigation',close);win.webContents.removeListener('did-navigate-in-page',close);win.removeListener('closed',close)}}
});
ipcMain.handle("accounts:surface", (event, input: SurfaceInput) => {
  const win=mainWindow;
  if(!win||win.isDestroyed()||event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame)throw new Error("窗口来源无效");
  return surfacePresenter.update(win, input);
});

function secureFile() { return join(app.getPath("userData"), "secure-store.json"); }
function readSecureStore(): Record<string, string> {
  const file = secureFile();
  if (!existsSync(file)) return {};
  try {
    const parsed = JSON.parse(readFileSync(file, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, string>
      : {};
  } catch {
    // A damaged optional settings file must never prevent the local Core and UI
    // from starting. The connection dialog can safely write a fresh encrypted store.
    return {};
  }
}
function readSecureValue(key: string) {
  try {
    const value = readSecureStore()[key];
    return value && safeStorage.isEncryptionAvailable()
      ? safeStorage.decryptString(Buffer.from(value, "base64"))
      : null;
  } catch {
    return null;
  }
}
function writeSecureStore(store: Record<string, string>) {
  writePrivateFileAtomically(secureFile(), JSON.stringify(store));
}
function writeSecureValue(key: string, value: string) { if (!safeStorage.isEncryptionAvailable()) throw new Error("系统密钥库不可用"); const store = readSecureStore(); if (value) store[key] = safeStorage.encryptString(value).toString("base64"); else delete store[key]; writeSecureStore(store); }
ipcMain.handle("secure:set", (_event, key: string, value: string) => {
  requireMainSender(_event);
  if (!rendererSecureSettingKeys.has(key)) throw new Error("不允许写入该安全设置");
  writeSecureValue(key, value); return true;
});
ipcMain.handle("secure:get", (_event, key: string) => {
  requireMainSender(_event);
  if (!rendererSecureSettingKeys.has(key)) throw new Error("不允许读取该安全设置");
  return readSecureValue(key);
});
ipcMain.handle("secure:delete", (_event, key: string) => {
  requireMainSender(_event);
  if (!rendererSecureSettingKeys.has(key)) throw new Error("不允许删除该安全设置");
  const store = readSecureStore(); delete store[key]; writeSecureStore(store); return true;
});

const coreSupervisor = new CoreSupervisor({
  spawnChild: spawnCore,
  healthCheck: checkCoreHealth,
  requestShutdown: async signal => {
    const response = await fetch(`${coreBaseUrl()}/api/internal/shutdown`, {
      method: "POST", headers: { "x-startup-token": coreToken }, signal, redirect: coreFetchRedirectMode,
    });
    assertDirectCoreResponse(response);
    return response.ok && (await response.json()).accepted === true;
  },
  // The first frozen start verifies and atomically mirrors roughly 110 MB of
  // native OpenVINO DLLs.  Slow disks and real-time antivirus can make that
  // one-time fail-closed bootstrap substantially slower than later starts.
  readyTimeoutMs: 180_000,
  stableResetMs: 30_000,
  heartbeatIntervalMs: 10_000,
  // Collection can legitimately keep SQLite/OpenVINO/antivirus busy for several
  // seconds. A slow probe must not kill a healthy Core and interrupt every window.
  healthTimeoutMs: 15_000,
  heartbeatFailureThreshold: 6,
  stopGraceMs: 45_000,
  stopForceMs: 5_000,
  restartDelaysMs: [1000, 2000, 5000, 10000, 30000],
  log: (message, error) => {storageManagement.log(message);error === undefined ? console.error(message) : console.error(message, error)},
});

const pexelsCredentials = new PexelsCredentialController({
  encryptionAvailable: () => secureCredentialStorageAvailable(safeStorage, process.platform),
  writeEncrypted: value => writeSecureValue("pexels-api-key", value),
  rememberSecret: rememberCoreSecret,
  activate: async value => {
    const response = await fetch(`${coreBaseUrl()}/api/internal/integrations/pexels`, {
      method: "POST",
      headers: {"x-startup-token": coreToken, "Content-Type": "application/json"},
      body: JSON.stringify({pexels_api_key: value}),
      signal: AbortSignal.timeout(5_000),
      redirect: coreFetchRedirectMode,
    });
    assertDirectCoreResponse(response);
    if (!response.ok) throw new Error("本机 Pexels 配置暂未生效");
    return response.json();
  },
});

const cloudConfiguration = new CloudConfigurationController({
  encryptionAvailable: () => secureCredentialStorageAvailable(safeStorage, process.platform),
  readEncrypted: () => readSecureValue("cloud-configuration-v1"),
  writeEncrypted: value => writeSecureValue("cloud-configuration-v1", value),
  rememberSecret: rememberCoreSecret,
  activate: async value => {
    const response = await fetch(`${coreBaseUrl()}/api/internal/integrations/cloud`, {
      method: "POST", headers: {"x-startup-token": coreToken, "Content-Type": "application/json"},
      body: JSON.stringify({enabled: value.enabled, project_url: value.projectUrl, publishable_key: value.publishableKey}),
      signal: AbortSignal.timeout(5_000), redirect: coreFetchRedirectMode,
    });
    assertDirectCoreResponse(response);
    if (!response.ok) throw new Error("本机云端配置暂未生效");
    return response.json();
  },
});

ipcMain.handle("core:configure", async (_event, input: { bitbrowserPort?: number; bitbrowserApiKey?: string; openaiApiKey?: string; pexelsApiKey?: string; cloud?: {enabled:boolean;projectUrl:string;publishableKey:string} }) => {
  requireMainSender(_event);
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some(key => !['bitbrowserPort', 'bitbrowserApiKey', 'openaiApiKey', 'pexelsApiKey', 'cloud'].includes(key)))
    throw new Error("集成配置格式无效");
  if (input.cloud !== undefined) {
    if (Object.keys(input).length !== 1) throw new Error("云端配置必须单独保存");
    return cloudConfiguration.save(input.cloud);
  }
  if (input.pexelsApiKey !== undefined) {
    if (input.bitbrowserPort !== undefined || input.bitbrowserApiKey !== undefined || input.openaiApiKey !== undefined)
      throw new Error("请单独保存 Pexels 密钥，避免重启正在执行任务的 Core");
    return pexelsCredentials.save(input.pexelsApiKey);
  }
  if (typeof input.bitbrowserPort !== 'number' || !Number.isInteger(input.bitbrowserPort) || input.bitbrowserPort < 1 || input.bitbrowserPort > 65535) throw new Error("BitBrowser Local API 端口无效");
  if ((input.bitbrowserApiKey !== undefined && typeof input.bitbrowserApiKey !== 'string')
      || (input.openaiApiKey !== undefined && typeof input.openaiApiKey !== 'string')) throw new Error("集成密钥格式无效");
  configuredBitbrowserPort = String(input.bitbrowserPort);
  writeSecureValue("bitbrowser-port", String(input.bitbrowserPort));
  if (input.bitbrowserApiKey !== undefined) writeSecureValue("bitbrowser-api-key", input.bitbrowserApiKey.trim());
  if (input.openaiApiKey !== undefined) writeSecureValue("openai-api-key", input.openaiApiKey.trim());
  await coreSupervisor.restartForConfiguration();
  return { restarted: true };
});

let quitShutdownStarted = false;
app.on("before-quit", (event) => {
  // Electron does not await async event listeners. Hold the quit until the owned
  // Core has really exited so a new app instance can never race an orphan Core.
  event.preventDefault();
  if (quitShutdownStarted) return;
  quitShutdownStarted = true;
  void coreSupervisor.shutdown()
    .catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error("退出时无法确认本机采集服务已停止", error);
      dialog.showErrorBox("本机采集服务未能正常退出", `${message}\n应用退出后，后台守护将继续清理该进程。`);
    })
    .finally(async () => { chatTranslation.stop();immersiveTranslator.stop();storageManagement.stop();googleTranslator.dispose();chatgptPage.dispose();reviewPage?.dispose();await embeddedBrowser.stop().catch(() => {}); app.exit(0); });
});

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  // A second copy would start another Core with an independent in-memory cache and
  // multiply BitBrowser Local API traffic. Keep exactly one desktop/Core pair.
  app.quit();
} else {
  app.on("second-instance", () => {
    const existing = mainWindow;
    if (!existing) return;
    if (existing.isMinimized()) existing.restore();
    existing.show();
    existing.focus();
  });
  app.whenReady().then(async () => {
    try { await embeddedBrowser.start();storageManagement.start(); await coreSupervisor.start(); } catch (error) { console.error(error); }
    createWindow();
    app.on("activate", () => { if (!mainWindow || mainWindow.isDestroyed()) createWindow(); });
  });
}
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
