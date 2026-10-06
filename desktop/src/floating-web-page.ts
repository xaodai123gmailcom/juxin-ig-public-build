import {accountUserAgent} from './account-user-agent.js';
import {BrowserWindow, Menu, shell, session, screen, type Rectangle, type WebContents} from 'electron';

/** A nonmodal child window: its native title bar can be dragged without page IPC. */
export type FloatingPageOptions={partition:string;url:string;title:string;width:number;height:number;loginPopups?:boolean;loginUrl?:string;retainWhileHidden?:boolean;bounds?:(parent:Rectangle,workArea:Rectangle)=>Rectangle};
export class FloatingWebPage {
 protected popup?:BrowserWindow;
 private visible=false;
 private top=78;
 private win?:BrowserWindow;
 private idle?:ReturnType<typeof setTimeout>;
 private automaticBounds?:Rectangle;
 private moved=false;
 private children=new Set<BrowserWindow>();
 constructor(private changed:(visible:boolean)=>void,protected options:FloatingPageOptions){}
 get opened(){return this.visible}
 setTop(top:number){this.top=Math.max(0,Math.round(top));this.raise()}
 private place(){
  const win=this.win,popup=this.popup;
  if(!win||win.isDestroyed()||!popup||popup.isDestroyed())return;
  const parent=win.getContentBounds(),area=screen.getDisplayMatching(parent).workArea;
  const width=Math.min(this.options.width,Math.max(320,parent.width-24),area.width);
  const height=Math.min(this.options.height,Math.max(260,parent.height-this.top-12),area.height);
  const bounds=this.options.bounds?.(parent,area)||{x:Math.max(area.x,Math.min(parent.x+parent.width-width-12,area.x+area.width-width)),y:Math.max(area.y,Math.min(parent.y+this.top,area.y+area.height-height)),width,height};
  this.automaticBounds=bounds;popup.setBounds(bounds);
 }
 toggle(win:BrowserWindow){if(this.visible){this.hide();return false}return this.open(win)}
 protected open(win:BrowserWindow){
  if(win.isDestroyed())throw new Error('主窗口已关闭');
  const contents=win.webContents;
  if(!contents||contents.isDestroyed())throw new Error('主窗口已关闭');
  if(this.idle)clearTimeout(this.idle);this.idle=undefined;
  this.win=win;
  if(!this.popup||this.popup.isDestroyed()){
   const ses=session.fromPartition(this.options.partition);
   ses.setUserAgent(accountUserAgent(ses.getUserAgent()),'zh-CN,zh;q=0.9,en;q=0.8');
   const storage=(permission:string,origin:string)=>permission==='persistent-storage'&&origin==='https://chatgpt.com';
   ses.setPermissionRequestHandler((wc,permission,cb)=>{let origin='';try{origin=new URL(wc.getURL()).origin}catch{}cb(storage(permission,origin))});ses.setPermissionCheckHandler((_wc,permission,origin)=>storage(permission,origin));
   const popup=new BrowserWindow({parent:win,modal:false,show:false,width:this.options.width,height:this.options.height,minWidth:320,minHeight:260,title:this.options.title+' · 拖动标题栏移动',autoHideMenuBar:true,movable:true,resizable:true,minimizable:false,maximizable:false,fullscreenable:false,skipTaskbar:true,backgroundColor:'#ffffff',webPreferences:{session:ses,contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true,backgroundThrottling:true}});
   this.popup=popup;popup.setMenu(null);popup.webContents.setUserAgent(ses.getUserAgent());
   this.configurePage(popup.webContents,popup);
   if(this.options.loginPopups)popup.setMenu(Menu.buildFromTemplate([{label:'页面',submenu:[
    {label:'重新加载',accelerator:'CmdOrCtrl+R',click:()=>popup.webContents.reload()},
    {label:'返回目标页面',click:()=>{void popup.loadURL(this.options.url).catch(()=>{})}},
    ...(this.options.loginUrl?[{label:'登录 / 检查',click:()=>{void popup.loadURL(this.options.loginUrl!).catch(()=>{})}}]:[]),
    {label:'在默认浏览器打开',click:()=>{void shell.openExternal(this.options.url).catch(()=>{})}},
    {type:'separator'},{label:'收起',click:()=>this.hide()}
   ]}]));
   const markMoved=()=>{if(popup.isDestroyed())return;const b=popup.getBounds(),a=this.automaticBounds;if(a&&(b.x!==a.x||b.y!==a.y||b.width!==a.width||b.height!==a.height))this.moved=true};
   popup.on('move',markMoved);popup.on('resize',markMoved);
   popup.on('close',event=>{event.preventDefault();this.hide()});
   popup.on('closed',()=>{if(this.popup===popup){this.popup=undefined;this.visible=false;this.changed(false)}});
   void popup.loadURL(this.options.url).catch(()=>{});
  }
  this.moved=false;this.place();this.visible=true;this.popup.show();for(const child of this.children)if(!child.isDestroyed())child.show();this.popup.focus();this.changed(true);return true;
 }
 setPartition(partition:string){if(partition!==this.options.partition){this.dispose();this.options.partition=partition}}
 private allowed(raw:string){
  try{const u=new URL(raw);return u.protocol==='https:'&&!u.username&&!u.password&&(this.options.loginPopups||u.hostname==='google.com'||u.hostname.endsWith('.google.com'))}catch{return false}
 }
 private configurePage(wc:WebContents,win:BrowserWindow){
  const root=this.popup;
  const current=()=>Boolean(root&&this.popup===root&&!root.isDestroyed()&&!wc.isDestroyed());
  wc.setUserAgent(wc.session.getUserAgent());
  wc.on('page-title-updated',event=>event.preventDefault());
  const title=()=>{if(!this.options.loginPopups||win.isDestroyed())return;let host='';try{host=new URL(wc.getURL()).hostname}catch{}win.setTitle(this.options.title+' · '+host+' · 拖动标题栏移动')};
  wc.on('did-navigate',title);wc.on('did-navigate-in-page',title);
  wc.on('will-navigate',(event,url)=>{if(!this.allowed(url))event.preventDefault()});
  wc.on('will-redirect',(event,url)=>{if(!this.allowed(url))event.preventDefault()});
  wc.setWindowOpenHandler(({url})=>{
   if(!current()||!this.allowed(url))return {action:'deny'};
   if(!this.options.loginPopups){void win.loadURL(url).catch(()=>{});return {action:'deny'}};
   // Preserve the real opener and same dedicated session for official login
   // redirects/popups. Remote pages never receive application preload or IPC.
   return {action:'allow',overrideBrowserWindowOptions:{parent:this.popup,modal:false,width:600,height:720,autoHideMenuBar:true,webPreferences:{session:wc.session,contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true,preload:undefined}}};
  });
  wc.on('did-create-window',child=>{if(!current()){if(!child.isDestroyed())child.destroy();return}this.children.add(child);this.configurePage(child.webContents,child);child.on('closed',()=>this.children.delete(child));if(!this.visible)child.hide()});
  wc.on('before-input-event',(event,input)=>{if(input.type==='keyDown'&&input.key==='Escape'){event.preventDefault();this.hide()}});
 }
 // A child window naturally stays above its parent; never steal account focus.
 raise(){if(this.visible&&!this.moved)this.place()}
 hide(){
  this.visible=false;for(const child of this.children)if(!child.isDestroyed())child.hide();if(this.popup&&!this.popup.isDestroyed())this.popup.hide();
  const contents=this.win&&!this.win.isDestroyed()?this.win.webContents:null;
  if(contents&&!contents.isDestroyed())contents.focus();
  this.changed(false);if(this.idle)clearTimeout(this.idle);this.idle=undefined;
  // A hidden conversation can contain an unsent draft or an unfinished login.
  // Keep that page (with Chromium's existing background throttling) until an
  // explicit disposal or account change; other tools retain idle eviction.
  if(!this.options.retainWhileHidden){this.idle=setTimeout(()=>this.dispose(),5*60*1000);this.idle.unref()}
 }
 dispose(){for(const child of this.children)if(!child.isDestroyed())child.destroy();this.children.clear();if(this.idle)clearTimeout(this.idle);this.idle=undefined;const popup=this.popup;this.popup=undefined;this.win=undefined;this.visible=false;if(popup&&!popup.isDestroyed())popup.destroy();this.changed(false)}
}
