const test=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const path=require('node:path');
const {EventEmitter}=require('node:events');
const ts=require('typescript');
const realFloatingFixture=require('./floating-fixture.cjs');

// Run the complete translator integration against the production floating-page
// classes, controlling native view teardown only. Windows may clear a view's
// getter while the retained WebContents can still report its destruction.
const compile=name=>ts.transpileModule(
 readFileSync(path.join(__dirname,'../src',name),'utf8').replace(/^import .*;\r?\n/gm,''),
 {compilerOptions:{target:9,module:99}}
).outputText.replace(/^export /gm,'');
const floatingCode=compile('floating-web-page.ts');
const translatorCode=compile('google-translator.ts');
const integrationSource=readFileSync(path.join(__dirname,'google-translator.integration.cjs'),'utf8');

async function fixture({getter='undefined',delayedClose=false,accountNeverDestroys=false,cleanupFailure=false,timeoutMs=2000,source=integrationSource}={}){
 const timers=new Set(),evidence={accountCloseRequests:0,accountDestroyed:0,lateGetterReads:0,removed:0,restored:0,unhandled:0};
 const later=fn=>{const t=setTimeout(()=>{timers.delete(t);fn()},30);timers.add(t)};
 const sessionValue={
  protocol:{handle(){},unhandle(){evidence.unhandled++}},
  getUserAgent:()=>'',setUserAgent(){},setPermissionRequestHandler(){},setPermissionCheckHandler(){},
 };
 class Contents extends EventEmitter {
  dead=false;closing=false;url='about:blank';draft='';session=sessionValue;
  isDestroyed(){return this.dead}
  getTitle(){return 'Translation fixture'}
  getURL(){return this.url}
  setUserAgent(){}setWindowOpenHandler(){}focus(){}
  loadURL(url){this.url=url;return Promise.resolve()}
  executeJavaScript(source){if(source.includes(".value='draft'")){this.draft='draft';return Promise.resolve('draft')}return Promise.resolve(this.draft)}
  sendInputEvent(input){this.emit('before-input-event',{preventDefault(){}},{type:input.type,key:input.keyCode})}
  close(){
   if(this.dead||this.closing)return;
   this.closing=true;
   if(this.account)evidence.accountCloseRequests++;
   if(this.account&&accountNeverDestroys)return;
   const finish=()=>{this.dead=true;if(this.account)evidence.accountDestroyed++;this.emit('destroyed')};
   if(this.account&&delayedClose)later(finish);else finish();
  }
 }
 class NativeWindow extends EventEmitter {
  dead=false;visible=false;bounds={x:20,y:20,width:1120,height:720};
  constructor(options={}){super();this.options=options;this.webContents=new Contents()}
  isDestroyed(){return this.dead}getBounds(){return {...this.bounds}}getContentBounds(){return this.getBounds()}
  setBounds(bounds){this.bounds={...bounds};this.emit('move');this.emit('resize')}
  setMenu(){}show(){this.visible=true}hide(){this.visible=false}focus(){}
  isVisible(){return this.visible}getParentWindow(){return this.options.parent}isModal(){return !!this.options.modal}isResizable(){return this.options.resizable}
  loadURL(url){return this.webContents.loadURL(url)}
  close(){let prevented=false;this.emit('close',{preventDefault(){prevented=true}});if(!prevented)this.destroy()}
  destroy(){if(this.dead)return;this.dead=true;this.webContents.close();this.emit('closed')}
 }
 class NativeView {
  constructor(){this.contents=new Contents();this.contents.account=true}
  get webContents(){
   if(this.contents.closing){evidence.lateGetterReads++;if(getter==='throw')throw new Error('Object has been destroyed');return getter==='null'?null:undefined}
   return this.contents;
  }
 }
 const screen={getDisplayMatching:()=>({workArea:{x:0,y:0,width:1920,height:1080}})};
 const session={fromPartition:()=>sessionValue};
 const FloatingWebPage=Function('BrowserWindow','screen','session','accountUserAgent',floatingCode+';return FloatingWebPage')(NativeWindow,screen,session,x=>x);
 const GoogleTranslator=Function('FloatingWebPage',translatorCode+';return GoogleTranslator')(FloatingWebPage);
 const win=new NativeWindow();
 win.contentView={addChildView(){},removeChildView(){if(cleanupFailure)throw new Error('native detach failed');evidence.removed++}};
 const fakeRequire=id=>{
  if(id==='electron')return {session,WebContentsView:NativeView,screen};
  if(id==='./floating-fixture.cjs')return {...realFloatingFixture,
   prepareFixtureWindow:async()=>async()=>{evidence.restored++},
   settledBounds:async(w,accept=()=>true)=>{const bounds=w.getBounds();assert.ok(accept(bounds));return bounds},
  };
  if(id==='./renderer-fixture.cjs')return require('./renderer-fixture.cjs');
  if(id==='./task-watch-fixture.cjs')return require('./task-watch-fixture.cjs');
  return require(id);
 };
 const translated=source.replace("await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/google-translator.js')))","await loadTranslator()");
 assert.notEqual(translated,source,'replace only the dynamic module loader; all integration assertions and cleanup execute unchanged');
 const module={exports:{}};
 Function('require','module','__dirname','loadTranslator',translated)(fakeRequire,module,__dirname,async()=>({GoogleTranslator}));
 const lines=[],oldLog=console.log,oldError=console.error;
 console.log=(...v)=>lines.push(v.map(String).join(' '));console.error=(...v)=>lines.push(v.map(String).join(' '));
 try {
  await module.exports({win,timeoutMs});
  // Completion is established by the integration's own destroyed-event wait.
  // No post-run sleep can make a prematurely successful fixture appear correct.
  return {evidence,lines};
 } finally {console.log=oldLog;console.error=oldError;for(const timer of timers)clearTimeout(timer)}
}

for(const getter of ['undefined','null','throw'])test(`translator cleanup preserves its captured contents when the destroyed view getter returns ${getter}`,async()=>{
 const {evidence}=await fixture({getter});
 assert.equal(evidence.accountCloseRequests,1);assert.equal(evidence.accountDestroyed,1);
 assert.equal(evidence.lateGetterReads,0,'no close-completed view getter is read');
 assert.equal(evidence.removed,1);assert.equal(evidence.restored,1);assert.equal(evidence.unhandled,1);
});
test('view getter clearing before delayed native destruction still cleans the original contents',async()=>{
 const {evidence}=await fixture({delayedClose:true});
 assert.equal(evidence.accountCloseRequests,1);assert.equal(evidence.accountDestroyed,1);
 assert.equal(evidence.lateGetterReads,0);
});
test('a genuine native cleanup failure still fails the integration',async()=>{
 await assert.rejects(fixture({cleanupFailure:true}),error=>{
  assert.equal(error.name,'AggregateError');assert.equal(error.message,'Translator fixture cleanup failed');
  assert.equal(error.errors[0].cause.message,'native detach failed');return true;
 });
});
test('the regression reproduces the old getter failure instead of accepting missing contents as closed',async()=>{
 const oldSource=integrationSource.replace("if(accountContents)return closeFixtureContents(accountContents,{label:'translator account cleanup',timeoutMs});",'if(account&&!account.webContents.isDestroyed())account.webContents.close();');
 assert.notEqual(oldSource,integrationSource);
 await assert.rejects(fixture({source:oldSource}),error=>{
  assert.equal(error.name,'AggregateError');assert.match(error.errors[0].cause.message,/undefined.*isDestroyed/);return true;
 });
});

test('an account view that never destroys fails the integration despite a missing getter',async()=>{
 await assert.rejects(fixture({accountNeverDestroys:true,timeoutMs:35}),/destruction not observed: translator account view/);
});
