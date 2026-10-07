import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("collectorCore", {
  onProfilePreview: (listener: (url: string) => void) => {
    const receive = (_event: Electron.IpcRendererEvent, url: string) => listener(url);
    ipcRenderer.on("profile:open", receive);
    return () => ipcRenderer.removeListener("profile:open", receive);
  },
  reviewAccount:(input:{action:'status'|'login'|'home'|'target'|'hide'|'reset'})=>ipcRenderer.invoke("review:account",input),
  chatgptPage:(input:{hide?:boolean;top?:number;boundsOnly?:boolean})=>ipcRenderer.invoke("chatgpt:toggle",input),
  onChatGPTVisibility:(listener:(visible:boolean)=>void)=>{const receive=(_event:Electron.IpcRendererEvent,visible:boolean)=>listener(visible);ipcRenderer.on("chatgpt:visibility",receive);return()=>ipcRenderer.removeListener("chatgpt:visibility",receive)},
  googleTranslator:(input:{hide?:boolean})=>ipcRenderer.invoke("translator:toggle",input),
  chatTranslation:(input:Record<string,unknown>)=>ipcRenderer.invoke('chat:translation',input),
  onTranslatorVisibility:(listener:(visible:boolean)=>void)=>{const receive=(_event:Electron.IpcRendererEvent,visible:boolean)=>listener(visible);ipcRenderer.on("translator:visibility",receive);return()=>ipcRenderer.removeListener("translator:visibility",receive)},
  storageManagement: (input:Record<string,unknown>)=>ipcRenderer.invoke("storage:manage",input),
  messageNotifications: (input:{enabled?:boolean;test?:boolean})=>ipcRenderer.invoke("messages:settings",input),
  onAccountFocus: (listener:(profile:string)=>void)=>{const receive=(_event:Electron.IpcRendererEvent,profile:string)=>listener(profile);ipcRenderer.on("account:focus",receive);return()=>ipcRenderer.removeListener("account:focus",receive)},
  accountContextMenu: (input:{name:string;opened:boolean;disabled:boolean;configDisabled?:boolean;documentUrl:string}) => ipcRenderer.invoke("accounts:context-menu",input),
  onTaskInterference:(listener:(target:string)=>void)=>{const receive=(_event:Electron.IpcRendererEvent,target:string)=>listener(target);ipcRenderer.on("account:interfere-request",receive);return()=>ipcRenderer.removeListener("account:interfere-request",receive)},
  accountTaskWatch:(input:{id:string;documentUrl:string;target?:string})=>ipcRenderer.invoke("accounts:watch",input),
  accountInterfere:(input:{id:string;documentUrl:string;taskKey:string;target:string})=>ipcRenderer.invoke("accounts:interfere",input),
  accountResumeTask:(input:{id:string;documentUrl:string;taskKey:string;grant:string})=>ipcRenderer.invoke("accounts:resume-task",input),
  accountCloseTaskPage:(input:{id:string;documentUrl:string;taskKey:string;target:string})=>ipcRenderer.invoke("accounts:close-task-page",input),
  accountSurface: (input: {id?:string;visible:boolean;interferenceGrant?:string;viewTarget?:string;documentUrl?:string;bounds?:{x:number;y:number;width:number;height:number}}) => ipcRenderer.invoke("accounts:surface", input),
  request: (path: string, options: { method?: string; body?: unknown } = {}) => ipcRenderer.invoke("core:request", { path, ...options }),
  secureSet: (key: string, value: string) => ipcRenderer.invoke("secure:set", key, value),
  secureGet: (key: string) => ipcRenderer.invoke("secure:get", key),
  secureDelete: (key: string) => ipcRenderer.invoke("secure:delete", key),
  configureIntegrations: (input: { bitbrowserPort?: number; bitbrowserApiKey?: string; openaiApiKey?: string; cloud?: {enabled:boolean;projectUrl:string;publishableKey:string} }) => ipcRenderer.invoke("core:configure", input)
});
