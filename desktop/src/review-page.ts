import {BrowserWindow,Menu,dialog,session} from 'electron';
import {reviewRecoveryHtml,reviewAccountAction,type ReviewAccountAction} from './review-recovery.js';
import {FloatingWebPage} from './floating-web-page.js';
import {profilePreviewUrl,previewFailureMessage,reviewPreviewAnchorScript,reviewPreviewBounds,type ReviewPreviewAnchor} from './profile-preview.js';

// Review instances can be replaced on software logout. Keep cleanup ownership
// at partition scope so an old reset can never clear a newly opened login.
type ReviewState={opened:boolean;lastTarget:string;message:string;httpStatus:number|null;resetting:boolean;resetRequired:boolean};
const resets=new Map<string,{owner:ReviewPage;promise:Promise<ReviewState>}>();
const resetFailures=new Map<string,string>();
const resetBusy='审核登录环境正在清空，请完成后再操作';
const resetFailed='审核登录环境清空失败，请重试“一键清空并重新登录”。清空完成前不会打开登录页。';

/** A dedicated manual review browser. It never attaches to an account/task. */
export class ReviewPage extends FloatingWebPage {
 private target='';
 private message='';
 private httpStatus:number|null=null;
 private recovering=false;
 private anchor:ReviewPreviewAnchor|null=null;
 private request=0;
 private lifetime=0;
 private placementRequest=0;
 private parent?:BrowserWindow;
 private resizeTimer?:ReturnType<typeof setTimeout>;
 private resized=()=>{if(this.resizeTimer)clearTimeout(this.resizeTimer);this.resizeTimer=setTimeout(()=>{void this.refreshPlacement().catch(()=>{})},60)};
 private parentMoved=()=>{try{this.raise()}catch{/* Parent can close during native move. */}};
 constructor(partition:string,private showSettings?:()=>void){super(()=>{},{partition,url:'https://www.instagram.com/',title:'独立审核',width:950,height:640,loginPopups:true,loginUrl:'https://www.instagram.com/accounts/login/'});this.options.bounds=(parent,area)=>reviewPreviewBounds(parent,area,this.anchor)}
 private bind(win:BrowserWindow){
  if(this.parent===win)return;
  this.parent?.removeListener('resize',this.resized);this.parent?.removeListener('move',this.parentMoved);
  this.parent=win;win.on('resize',this.resized);win.on('move',this.parentMoved);
 }
 private async measure(win:BrowserWindow):Promise<ReviewPreviewAnchor|null>{
  let timer:ReturnType<typeof setTimeout>|undefined;
  try{
   const value:any=await Promise.race([win.webContents.executeJavaScript(reviewPreviewAnchorScript),new Promise(resolve=>{timer=setTimeout(()=>resolve(null),250)})]);
   const zoom=win.webContents.getZoomFactor();
   return value&&[value.left,value.top,value.avatarLeft].every(Number.isFinite)?{left:value.left*zoom,top:value.top*zoom,avatarLeft:value.avatarLeft*zoom}:null;
  }catch{return null}finally{if(timer)clearTimeout(timer)}
 }
 async openFromRenderer(win:BrowserWindow,username:string){
  this.requireReady();profilePreviewUrl(username,'target');if(win.isDestroyed()||!win.webContents||win.webContents.isDestroyed())return {visible:false};const request=++this.request,url=win.webContents.getURL();
  const anchor=await this.measure(win);
  if(request!==this.request||win.isDestroyed()||!win.webContents||win.webContents.isDestroyed()||win.webContents.getURL()!==url)return {visible:false};
  this.anchor=anchor;return this.openTarget(win,username);
 }
 private async refreshPlacement(){
  const win=this.parent,request=this.request;if(!this.opened||!win||win.isDestroyed()||!win.webContents||win.webContents.isDestroyed())return;
  const placement=++this.placementRequest,url=win.webContents.getURL(),anchor=await this.measure(win);
  if(placement!==this.placementRequest||request!==this.request||!this.opened||win.isDestroyed()||!win.webContents||win.webContents.isDestroyed()||win.webContents.getURL()!==url)return;
  this.anchor=anchor;this.raise();
 }
 state():ReviewState{
  const resetting=resets.has(this.options.partition);
  return {opened:this.opened,lastTarget:this.target,message:resetting?'':resetFailures.get(this.options.partition)||this.message,httpStatus:this.httpStatus,resetting,resetRequired:!resetting&&resetFailures.has(this.options.partition)};
 }
 private requireReady(){if(resets.has(this.options.partition))throw new Error(resetBusy);if(resetFailures.has(this.options.partition))throw new Error(resetFailed)}
 protected override open(win:BrowserWindow){this.requireReady();return super.open(win)}
 override setPartition(partition:string){if(partition!==this.options.partition)throw new Error('审核登录环境归属不可更改，请重新登录软件')}
 private resetAccount(win:BrowserWindow){
  const partition=this.options.partition,existing=resets.get(partition);
  if(existing){if(existing.owner===this)return existing.promise;return Promise.reject(new Error(resetBusy))}
  // Only this explicitly isolated partition is eligible for destructive reset.
  if(!/^persist:review-[A-Za-z0-9_-]+$/.test(partition))return Promise.reject(new Error('审核登录环境未隔离，无法清空'));
  const lifetime=this.lifetime;this.request++;
  const alive=()=>lifetime===this.lifetime&&!win.isDestroyed()&&Boolean(win.webContents)&&!win.webContents.isDestroyed();
  const operation={owner:this,promise:Promise.resolve(this.state())};
  operation.promise=Promise.resolve().then(async()=>{
   if(!alive())return;
   const confirmationParent=this.opened&&this.popup&&!this.popup.isDestroyed()?this.popup:win;
   const {response}=await dialog.showMessageBox(confirmationParent,{type:'warning',title:'清空审核登录环境',message:'清空当前审核网页登录状态，并打开新的登录窗口？',detail:'将关闭审核网页及其登录弹窗，并清除审核专用的登录信息、网站数据和缓存。随后可登录另一个账号。\n采集窗口、审核名单、任务进度和去重库存不受影响。此操作不会解除 Instagram 对原账号的限制。',buttons:['取消','清空并重新登录'],defaultId:0,cancelId:0,noLink:true});
   if(response!==1||!alive())return;
   const ses=session.fromPartition(partition);
   resetFailures.set(partition,resetFailed);
   try{
    // Destroy every old renderer before any async cleanup; hidden OAuth child
    // windows otherwise keep writing old cookies back into the same session.
    super.dispose();this.request++;this.anchor=null;
    await ses.closeAllConnections();
    await ses.clearStorageData();
    await ses.clearCache();
    await ses.clearAuthCache();
    await ses.closeAllConnections();
    resetFailures.delete(partition);this.message='';this.httpStatus=null;this.recovering=false;
   }catch(error){throw new Error(resetFailed,{cause:error})}
   if(alive()){
    // The barrier stays held through window creation. No other instance can
    // open or navigate this partition between clearing it and showing login.
    this.bind(win);this.openPage(win,'https://www.instagram.com/accounts/login/','独立审核 · 重新登录',true);
   }
  }).finally(()=>{if(resets.get(partition)===operation)resets.delete(partition)}).then(()=>this.state());
  resets.set(partition,operation);return operation.promise;
 }
 async account(win:BrowserWindow,input:unknown){
  const action=reviewAccountAction(input);
  if(action==='reset')return this.resetAccount(win);
  if(action==='status')return this.state();
  if(action==='hide'){this.hide();return this.state()}
  this.requireReady();
  if(action==='target'){
   if(!this.target)throw new Error('请先在审核列表点击一个目标账号');
   await this.openFromRenderer(win,this.target);
  }else if(action==='login'||action==='home'){
   this.request++;this.anchor=null;this.bind(win);
   this.openPage(win,action==='login'?'https://www.instagram.com/accounts/login/':'https://www.instagram.com/','独立审核 · 登录 / 切换账号');
  }
  return this.state();
 }
 openTarget(win:BrowserWindow,username:string){
  this.requireReady();
  const url=profilePreviewUrl(username,'target');
  this.request++;this.bind(win);this.target=username;
  return this.openPage(win,url,'独立审核 · @'+username);
 }
 private openPage(win:BrowserWindow,url:string,title:string,resetOpening=false){
  if(!resetOpening)this.requireReady();
  const previous=this.popup;
  this.message='';this.httpStatus=null;this.recovering=false;
  this.options.url=url;this.options.title=title;
  if(resetOpening)super.open(win);else this.open(win); // Show now, without waiting for a Core request or page load.
  const popup=this.popup!;popup.setTitle(title);
  if(previous!==popup){
   const wc=popup.webContents;
   const showError=(error:unknown)=>{if(!win.isDestroyed())void dialog.showMessageBox(win,{type:'error',title:'审核账号',message:error instanceof Error?error.message:'审核账号操作失败，请重试',buttons:['知道了']}).catch(()=>{})};
   const command=(action:ReviewAccountAction)=>{void this.account(win,{action}).catch(showError)};
   popup.setMenu(Menu.buildFromTemplate([{label:'审核账号',submenu:[
    {label:'登录 / 检查',click:()=>command('login')},
    {label:'账号主页 / 切换',click:()=>command('home')},
    {label:'一键清空并重新登录',click:()=>command('reset')},
    {label:'返回目标主页',click:()=>command('target')},
    {label:'重新加载',accelerator:'CmdOrCtrl+R',click:()=>{try{this.requireReady();if(popup.isDestroyed())return;if(wc.getURL().startsWith('data:'))command(this.target?'target':'login');else wc.reload()}catch(error){showError(error)}}},
    ...(this.showSettings?[{label:'打开软件设置',click:()=>{this.hide();this.showSettings?.()}}]:[]),
    {type:'separator'},{label:'收起窗口',click:()=>this.hide()}
   ]}]));
   popup.setAutoHideMenuBar(false);popup.setMenuBarVisibility(true);
   const recover=(status:number,title:string,networkError='')=>{
    if(popup.isDestroyed()||this.popup!==popup||this.recovering)return;
    const message=previewFailureMessage(status,title,networkError);if(!message)return;
    this.message=message;this.httpStatus=status>0?status:null;this.recovering=true;
    const html=reviewRecoveryHtml(message,this.target);
    void popup.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(html)).catch(()=>{});
   };
   wc.on('did-start-navigation',(_e,url,inPlace,main)=>{
    if(!main||inPlace||popup.isDestroyed()||this.popup!==popup)return;
    popup.contentView.setVisible(false);
    if(/^https:\/\//.test(url)){this.recovering=false;this.message='';this.httpStatus=null}
   });
   wc.on('did-navigate',(_e,url,status)=>{
    if(this.popup!==popup||popup.isDestroyed()||!url.startsWith('https://'))return;
    this.httpStatus=status>0?status:null;
    if(status>=400)recover(status,'');
   });
   wc.on('dom-ready',()=>{if(this.popup===popup&&!popup.isDestroyed())popup.contentView.setVisible(true)});
   wc.on('did-finish-load',()=>{
    if(this.popup===popup&&!popup.isDestroyed()&&wc.getURL().startsWith('https://'))recover(this.httpStatus||0,wc.getTitle());
   });
   wc.on('did-fail-load',(_e,code,reason,url,main)=>{
    if(!main||code===-3||!url.startsWith('https://'))return;
    recover(0,'',reason||'network error');
   });
  }
  if(previous===popup){
   popup.contentView.setVisible(false);popup.webContents.stop();void popup.loadURL(url).catch(()=>{});
  }
  return {visible:true};
 }
 override dispose(){this.lifetime++;this.request++;if(this.resizeTimer)clearTimeout(this.resizeTimer);this.parent?.removeListener('resize',this.resized);this.parent?.removeListener('move',this.parentMoved);this.parent=undefined;super.dispose()}
}
