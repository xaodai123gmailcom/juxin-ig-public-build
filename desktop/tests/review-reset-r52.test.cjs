const test=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const path=require('node:path');
const ts=require('typescript');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));

// Only Electron's native transport is substituted. Both ReviewPage and its
// actual FloatingWebPage parent (including login-child disposal) run unchanged.
function harness(){
 const windows=[],sessions=new Map(),trace=[],dialogs=[],modules=new Map();
 let confirm=async()=>({response:1}),step=async()=>{};
 const fromPartition=partition=>{
  if(sessions.has(partition))return sessions.get(partition);
  const ses={partition,cookies:new Map([['login','old']]),storage:new Map([['state','old']]),cache:true,auth:true,
   getUserAgent:()=> 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',setUserAgent(){},setPermissionRequestHandler(){},setPermissionCheckHandler(){}};
  for(const method of ['closeAllConnections','clearStorageData','clearCache','clearAuthCache'])ses[method]=async()=>{
   trace.push([method,partition]);
   assert.ok(windows.filter(w=>w.webContents.session===ses).every(w=>w.isDestroyed()),'all old review windows must close before any cleanup');
   await step(method,ses);
   if(method==='clearStorageData'){ses.cookies.clear();ses.storage.clear()}
   if(method==='clearCache')ses.cache=false;
   if(method==='clearAuthCache')ses.auth=false;
  };
  sessions.set(partition,ses);return ses;
 };
 class NativeWindow extends EventEmitter{
  constructor(options={}){
   super();this.options=options;this.destroyed=false;this.visible=false;this.bounds={x:50,y:50,width:800,height:600};this.contentView={setVisible(){}};
   const wc=new EventEmitter();wc.session=options.webPreferences?.session||fromPartition('software');wc.url='about:blank';wc.title='';
   Object.assign(wc,{isDestroyed:()=>this.destroyed,getURL:()=>wc.url,getTitle:()=>wc.title,getZoomFactor:()=>1,executeJavaScript:async()=>null,setUserAgent(){},stop(){},focus(){},reload(){trace.push(['reload',wc.session.partition])},setWindowOpenHandler(handler){wc.openHandler=handler}});
   this.webContents=wc;windows.push(this);
  }
  isDestroyed(){return this.destroyed}getContentBounds(){return {x:0,y:0,width:1500,height:950}}getBounds(){return this.bounds}setBounds(value){this.bounds=value}
  setTitle(){}setMenu(value){this.menu=value}setAutoHideMenuBar(){}setMenuBarVisibility(){}show(){this.visible=true}focus(){}hide(){this.visible=false}
  loadURL(url){assert.equal(this.destroyed,false);this.webContents.url=url;trace.push(['load',this.webContents.session.partition,url]);return Promise.resolve()}
  destroy(){if(this.destroyed)return;this.destroyed=true;this.visible=false;trace.push(['destroy',this.webContents.session.partition]);this.emit('closed')}
 }
 const electron={BrowserWindow:NativeWindow,Menu:{buildFromTemplate:value=>value},session:{fromPartition},screen:{getDisplayMatching:()=>({workArea:{x:0,y:0,width:1920,height:1080}})},shell:{openExternal:async()=>{}},dialog:{showMessageBox:async(win,options)=>{dialogs.push({win,options});return options.type==='warning'?confirm(win,options):{response:0}}}};
 function load(name){
  const file=path.resolve(__dirname,'../src',name.replace(/\.js$/,'.ts'));
  if(modules.has(file))return modules.get(file);
  const exports={};modules.set(file,exports);
  const compiled=ts.transpileModule(readFileSync(file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  runInNewContext(compiled,{exports,URL,setTimeout,clearTimeout,require:dependency=>dependency==='electron'?electron:dependency.startsWith('./')?load(dependency):require(dependency)});
  return exports;
 }
 const {ReviewPage}=load('review-page.ts'),win=new NativeWindow(),partition='persist:review-owner1',review=new ReviewPage(partition);
 const child=owner=>{const popup=new NativeWindow({webPreferences:{session:owner.webContents.session}});owner.webContents.emit('did-create-window',popup);return popup};
 return {ReviewPage,win,review,partition,fromPartition,windows,trace,dialogs,child,setConfirm:value=>{confirm=value},setStep:value=>{step=value}};
}

test('cancel is non-destructive: existing review window, login children, target and session remain intact',async()=>{
 const h=harness();h.review.openTarget(h.win,'target.user');const old=h.review.popup,child=h.child(old),ses=old.webContents.session;
 h.setConfirm(async()=>({response:0}));const before=h.trace.length,result=await h.review.account(h.win,{action:'reset'});
 assert.equal(h.review.popup,old);assert.equal(old.isDestroyed(),false);assert.equal(child.isDestroyed(),false);
 assert.equal(ses.cookies.get('login'),'old');assert.equal(ses.storage.get('state'),'old');assert.equal(h.trace.length,before);
 assert.equal(result.lastTarget,'target.user');assert.equal(result.resetting,false);assert.equal(result.resetRequired,false);
 assert.equal(h.dialogs[0].win,old);assert.equal(h.dialogs[0].options.defaultId,0);assert.equal(h.dialogs[0].options.cancelId,0);
 h.review.dispose();
});

test('confirmed reset destroys actual tracked root and nested login children before isolated cleanup and fresh login',async()=>{
 const h=harness();h.review.openTarget(h.win,'target.user');const old=h.review.popup,child=h.child(old),nested=h.child(child),ses=old.webContents.session;
 h.review.hide();const unrelated=h.fromPartition('persist:account-task');const before=h.trace.length;
 const result=await h.review.account(h.win,{action:'reset'}),fresh=h.review.popup;
 assert.notEqual(fresh,old);assert.ok([old,child,nested].every(w=>w.isDestroyed()));assert.equal(fresh.webContents.session,ses);
 assert.equal(fresh.webContents.getURL(),'https://www.instagram.com/accounts/login/');assert.equal(fresh.visible,true);
 assert.equal(ses.cookies.size,0);assert.equal(ses.storage.size,0);assert.equal(ses.cache,false);assert.equal(ses.auth,false);
 assert.equal(unrelated.cookies.get('login'),'old');assert.equal(unrelated.storage.get('state'),'old');assert.equal(unrelated.cache,true);
 const actions=h.trace.slice(before);assert.deepEqual(actions.map(v=>v[0]),['destroy','destroy','destroy','closeAllConnections','clearStorageData','clearCache','clearAuthCache','closeAllConnections','load']);
 assert.ok(actions.every(v=>v[1]===h.partition));assert.equal(result.lastTarget,'target.user');assert.equal(result.resetting,false);assert.equal(result.resetRequired,false);
 await h.review.account(h.win,{action:'target'});assert.equal(fresh.webContents.getURL(),'https://www.instagram.com/target.user/');h.review.dispose();
});

test('menu and IPC resets coalesce; normal navigation, inherited open and reload remain blocked until cleanup finishes',async()=>{
 const h=harness();h.review.openTarget(h.win,'target.user');const original=h.review.popup,answer=deferred();h.setConfirm(()=>answer.promise);
 const resetItem=original.menu[0].submenu.find(item=>item.label==='一键清空并重新登录');resetItem.click();
 const reset=h.review.account(h.win,{action:'reset'});await tick();assert.equal(h.dialogs.filter(d=>d.options.type==='warning').length,1);
 assert.equal(h.review.state().resetting,true);await assert.rejects(h.review.account(h.win,{action:'login'}),/正在清空/);
 await assert.rejects(h.review.account(h.win,{action:'home'}),/正在清空/);await assert.rejects(h.review.account(h.win,{action:'target'}),/正在清空/);
 assert.throws(()=>h.review.openTarget(h.win,'other.user'),/正在清空/);await assert.rejects(h.review.openFromRenderer(h.win,'other.user'),/正在清空/);
 h.review.hide();assert.throws(()=>h.review.toggle(h.win),/正在清空/);
 original.menu[0].submenu.find(item=>item.label==='重新加载').click();await tick();assert.equal(h.trace.some(v=>v[0]==='reload'),false);
 assert.equal(h.dialogs.filter(d=>d.options.type==='error').length,1);
 answer.resolve({response:1});await reset;assert.equal(h.trace.filter(v=>v[0]==='clearStorageData').length,1);h.review.dispose();
});

test('logout while confirmation is pending cancels destructive work and never reopens old account',async()=>{
 const h=harness(),answer=deferred();h.review.openTarget(h.win,'target.user');h.setConfirm(()=>answer.promise);
 const reset=h.review.account(h.win,{action:'reset'});await tick();h.review.dispose();
 const next=new h.ReviewPage(h.partition);await assert.rejects(next.account(h.win,{action:'login'}),/正在清空/);
 answer.resolve({response:1});await reset;assert.equal(h.trace.some(v=>v[0]==='clearStorageData'),false);assert.equal(h.review.popup,undefined);
 await next.account(h.win,{action:'login'});assert.equal(next.popup.webContents.session.cookies.get('login'),'old');next.dispose();
});

test('same-owner re-login cannot open while previous instance clears, and old cleanup cannot erase the new login',async()=>{
 const h=harness(),hold=deferred();h.review.openTarget(h.win,'target.user');h.setStep(async name=>{if(name==='clearStorageData')await hold.promise});
 const reset=h.review.account(h.win,{action:'reset'});await tick();assert.equal(h.review.popup,undefined);assert.equal(h.review.state().resetRequired,false);assert.equal(h.review.state().message,'');h.review.dispose();
 const next=new h.ReviewPage(h.partition);assert.equal(next.state().resetting,true);
 await assert.rejects(next.account(h.win,{action:'login'}),/正在清空/);await assert.rejects(next.account(h.win,{action:'reset'}),/正在清空/);
 assert.throws(()=>next.toggle(h.win),/正在清空/);hold.resolve();await reset;assert.equal(h.review.popup,undefined);
 await next.account(h.win,{action:'login'});next.popup.webContents.session.cookies.set('login','new-owner-session');await tick();
 assert.equal(next.popup.webContents.session.cookies.get('login'),'new-owner-session');next.dispose();
});

test('failed cleanup locks even a replacement instance until successful reset; no partially cleared login is opened',async()=>{
 const h=harness();h.review.openTarget(h.win,'target.user');h.setStep(async name=>{if(name==='clearCache')throw Error('cache unavailable')});
 await assert.rejects(h.review.account(h.win,{action:'reset'}),/清空失败/);assert.equal(h.review.popup,undefined);assert.equal(h.review.state().resetting,false);assert.equal(h.review.state().resetRequired,true);
 assert.match(h.review.state().message,/重试/);const next=new h.ReviewPage(h.partition);
 await assert.rejects(next.account(h.win,{action:'home'}),/清空失败/);assert.throws(()=>next.toggle(h.win),/清空失败/);
 h.setStep(async()=>{});const result=await next.account(h.win,{action:'reset'});assert.equal(result.resetRequired,false);assert.equal(result.resetting,false);assert.equal(result.message,'');assert.equal(next.popup.webContents.getURL(),'https://www.instagram.com/accounts/login/');next.dispose();
});

test('native-menu cleanup failure is visible and every cleanup stage failure prevents dirty login',async()=>{
 for(const failedStage of ['closeAllConnections','clearStorageData','clearCache','clearAuthCache']){
  const h=harness();h.review.openTarget(h.win,'target.user');const menu=h.review.popup.menu[0].submenu;
  h.setStep(async name=>{if(name===failedStage)throw Error('failure at '+failedStage)});
  menu.find(item=>item.label==='一键清空并重新登录').click();await tick();
  assert.equal(h.review.popup,undefined);assert.equal(h.review.state().resetRequired,true);assert.ok(h.dialogs.some(d=>d.options.type==='error'&&/清空失败/.test(d.options.message)));
  h.review.dispose();
 }
});

test('events from destroyed review page cannot overwrite fresh login state',async()=>{
 const h=harness();h.review.openTarget(h.win,'target.user');const old=h.review.popup;
 await h.review.account(h.win,{action:'reset'});const fresh=h.review.popup,before=h.review.state();
 old.webContents.emit('did-navigate',{},'https://www.instagram.com/old.user/',429);old.webContents.emit('did-start-navigation',{},'https://www.instagram.com/old.user/',false,true);old.webContents.emit('did-finish-load');old.webContents.emit('did-fail-load',{},-7,'timed out','https://www.instagram.com/old.user/',true);
 assert.deepEqual(h.review.state(),before);assert.equal(h.review.popup,fresh);assert.equal(fresh.webContents.getURL(),'https://www.instagram.com/accounts/login/');h.review.dispose();
});

test('reset rejects shared/unknown partitions and caller-supplied URL, account or partition parameters',async()=>{
 const h=harness();for(const partition of ['persist:account-task','default','persist:review-']){
  const other=new h.ReviewPage(partition);await assert.rejects(other.account(h.win,{action:'reset'}),/未隔离/);
 }
 for(const input of [{action:'reset',partition:'persist:account-task'},{action:'reset',url:'https://example.com/'},{action:'reset',confirmed:true}])await assert.rejects(h.review.account(h.win,input),/操作无效/);
 assert.equal(h.dialogs.length,0);assert.equal(h.trace.some(v=>v[0]==='clearStorageData'),false);assert.throws(()=>h.review.setPartition('persist:review-owner2'),/不可更改/);
});

test('a pre-reset target measurement cannot navigate after reset even if it resolves late',async()=>{
 const h=harness(),measure=deferred();h.review.openTarget(h.win,'original.user');h.win.webContents.executeJavaScript=()=>measure.promise;
 const pending=h.review.openFromRenderer(h.win,'stale.user');await h.review.account(h.win,{action:'reset'});
 measure.resolve(null);const result=await pending;assert.equal(result.visible,false);assert.equal(h.review.state().lastTarget,'original.user');assert.equal(h.review.popup.webContents.getURL(),'https://www.instagram.com/accounts/login/');h.review.dispose();
});
