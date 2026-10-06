import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {EventEmitter} from 'node:events';
import {runInNewContext} from 'node:vm';

// Execute the production host and browser lifecycle handlers. Native painting
// remains an Electron/Windows integration gate, not a claim of these fixtures.
function compile(path){
  const source=readFileSync(path,'utf8').replace(/^import .*;\r?\n/gm,'');
  return (nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
}
const hostCode=compile(process.env.JUXIN_TASK_DISPLAY_HOST_SOURCE||new URL('../src/embedded-browser.ts',import.meta.url));
const accountViewport=runInNewContext(compile(new URL('../src/account-viewport.ts',import.meta.url))+';accountViewport');
function fixture(){
  let serial=0,focused;
  const effects={navigations:0,closed:0};
  class Contents extends EventEmitter{
    id=++serial;dead=false;url='https://www.instagram.com/example/';
    mainFrame={processId:10,routingId:this.id};loading=false;
    debugger=Object.assign(new EventEmitter(),{isAttached:()=>true,sendCommand:async()=>({})});
    isDestroyed(){return this.dead}getURL(){return this.url}getTitle(){return 'Account'}
    isLoadingMainFrame(){return this.loading}
    setUserAgent(){}setWindowOpenHandler(){}executeJavaScriptInIsolatedWorld(){return Promise.resolve()}
    isFocused(){return focused===this}focus(){focused=this;this.emit('focus')}
    loadURL(){effects.navigations++;return Promise.resolve()}
    close(){effects.closed++;this.dead=true;this.emit('destroyed')}
  }
  class View{
    children=[];bounds={x:0,y:0,width:1280,height:900};visible=true;webContents=new Contents();
    getBounds(){return {...this.bounds}}setBounds(b){this.bounds={...b}}
    setVisible(v){this.visible=v}getVisible(){return this.visible}setBorderRadius(){}setBackgroundColor(){}
    addChildView(v,index){this.children=this.children.filter(x=>x!==v);index===undefined?this.children.push(v):this.children.splice(index,0,v)}
    removeChildView(v){this.children=this.children.filter(x=>x!==v)}
  }
  const shell=new View(),win=Object.assign(new EventEmitter(),{contentView:shell,webContents:shell.webContents,isDestroyed:()=>false,isMinimized:()=>false,isFocused:()=>true,getContentSize:()=>[1600,1000],setContentView(v){this.contentView=v}});
  const Host=runInNewContext(hostCode+';EmbeddedBrowserHost',{
    URL,setTimeout,clearTimeout,clearInterval,setImmediate,accountViewport,
    randomBytes:()=>({toString:()=>String(++serial)}),WebSocketServer:class{},View,WebContentsView:View,
    TaskWatchShield:class{view=new View();isUsable(){return true}hasFocus(){return this.view.webContents.isFocused()}},AccountUnreadCache:class{},whatsappColumnLayoutScript:()=>'',diagnosticCodes:()=>[],whatsappErrorText:String,
  });
  const host=new Host(()=>win);host.attachWindow(win);
  const p={id:'profile',owner:'owner',generation:1,session:{getUserAgent:()=>''},pages:new Map(),clients:new Set([{}]),closed:false,selected:'source'};
  host.profiles.set(p.id,p);
  for(const id of ['source','screen']){
    const view=host.makeView(p);p.pages.set(id,{targetId:id,view,role:id==='source'?'source':'screening',roleIndex:1});host.pageReady.add(view.webContents);
  }
  const page=p.pages.get('screen'),wc=page.view.webContents;
  const show=(target='screen')=>host.control('show',{profile:p.id,grant:host.requestSurface(),bounds:{x:100,y:80,width:1100,height:800},target,read_only:true});
  const watch=(target='screen',owner='owner')=>host.control('watch-profile',{profile:p.id,target,owner});
  return {host,p,page,wc,win,effects,show,watch};
}

test('a blank task target reports blank instead of claiming a black native surface is attached',async()=>{
  const f=fixture();f.wc.url='about:blank';
  const result=await f.show();assert.equal(result.attached,false);assert.equal(result.display_state,'blank');
  const frame=await f.watch();assert.equal(frame.target,'screen');assert.equal(frame.display_state,'blank');
  assert.equal(frame.pages.find(p=>p.id==='screen').display_state,'blank');assert.match(frame.display_message,/载入内容/);
  assert.equal(f.host.visible.target,'screen');assert.equal(f.p.selected,'source');assert.equal(f.p.clients.size,1);
  assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('about:blank dom-ready cannot hide the preparation notice or claim a ready profile',async()=>{
  const f=fixture();f.wc.url='about:blank';await f.show();f.wc.emit('dom-ready');
  assert.equal((await f.show()).attached,false);assert.equal((await f.watch()).display_state,'blank');
});

test('ordinary Instagram Open acknowledges the window without persisting the transient blank task notice',async()=>{
  const f=fixture();f.p.clients.clear();f.host.ensure=async()=>f.p;
  for(const page of f.p.pages.values())page.view.webContents.url='about:blank';
  const page=f.p.pages.get('source'),wc=page.view.webContents;
  let finishNavigation;
  wc.loadURL=url=>{f.effects.navigations++;assert.equal(url,'https://www.instagram.com/');return new Promise(resolve=>{finishNavigation=resolve})};
  const result=await f.host.control('open-instagram',{profile:f.p.id,owner:f.p.owner});
  try{
    assert.equal(result.opened,true);assert.equal(result.page_loaded,null);assert.equal(result.navigation_pending,true);
    assert.equal(result.message,'Instagram 窗口已打开');assert.equal(result.generation,f.p.generation);
    assert.equal(f.p.selected,'source');assert.equal((await f.show('source')).display_state,'blank');
    wc.emit('did-start-navigation',{},'https://www.instagram.com/',false,true);
    wc.url='https://www.instagram.com/';wc.emit('dom-ready');
    assert.equal((await f.show('source')).attached,true);assert.equal((await f.watch('source')).display_state,'ready');
    assert.equal(result.message,'Instagram 窗口已打开','the retained command acknowledgement is still true after navigation');
    const reopened=await f.host.control('open-instagram',{profile:f.p.id,owner:f.p.owner});
    assert.equal(reopened.page_loaded,true);assert.equal(reopened.navigation_pending,false);
    assert.equal(reopened.message,result.message);assert.deepEqual(f.effects,{navigations:1,closed:0});
  }finally{finishNavigation()}
});

test('fast Instagram Open keeps owner and task-occupancy checks before any navigation or relabelling',async()=>{
  const f=fixture();f.host.ensure=async()=>f.p;
  await assert.rejects(f.host.control('open-instagram',{profile:f.p.id,owner:'foreign'}),/任务占用|不属于/);
  await assert.rejects(f.host.control('open-instagram',{profile:f.p.id,owner:f.p.owner}),/任务占用/);
  assert.equal(f.p.pages.get('source').role,'source');assert.equal(f.page.role,'screening');
  assert.equal(f.p.selected,'source');assert.equal(f.p.clients.size,1);assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('a replacement task label retires the initial blank label without closing or selecting either page',async()=>{
  const f=fixture(),initial=f.p.pages.get('source');initial.view.webContents.url='about:blank';
  f.page.role=undefined;f.page.roleIndex=undefined;
  await f.host.control('label-task-page',{profile:f.p.id,target:initial.targetId,role:'task'});
  assert.equal((await f.host.control('watch-profile',{profile:f.p.id,owner:f.p.owner})).display_state,'blank');
  await f.host.control('label-task-page',{profile:f.p.id,target:f.page.targetId,role:'task'});
  const frame=await f.host.control('watch-profile',{profile:f.p.id,owner:f.p.owner,prefer_ready:true});
  assert.equal(frame.target,f.page.targetId);assert.equal(frame.display_state,'ready');
  assert.deepEqual(Array.from(frame.pages,x=>[x.id,x.label]),[['screen','任务页']]);
  assert.equal(initial.role,undefined);assert.equal(initial.roleIndex,undefined);
  await assert.rejects(f.watch(initial.targetId),/任务页面不可用/);
  await assert.rejects(f.watch(f.page.targetId,'foreign'),/任务网页尚未打开/);
  assert.equal(f.p.selected,'source');assert.equal(f.p.generation,1);assert.equal(f.p.clients.size,1);
  assert.equal(f.host.visible,undefined);assert.equal(f.p.pages.size,2);assert.deepEqual(f.effects,{navigations:0,closed:0});
  assert.equal((await f.show()).attached,true);assert.equal(f.host.visible.target,f.page.targetId);assert.equal(f.host.visible.readOnly,true);
});

test('task-label replacement preserves source and screening roles and invalid targets cannot retire a label',async()=>{
  const f=fixture();
  for(const id of ['old-task','new-task'])f.p.pages.set(id,{targetId:id,view:f.host.makeView(f.p)});
  await f.host.control('label-task-page',{profile:f.p.id,target:'old-task',role:'task'});
  await assert.rejects(f.host.control('label-task-page',{profile:f.p.id,target:'missing',role:'task'}),/任务页面无效/);
  assert.equal(f.p.pages.get('old-task').role,'task');
  const retired={targetId:'destroyed-task',view:f.host.makeView(f.p)};retired.view.webContents.dead=true;f.p.pages.set(retired.targetId,retired);
  await assert.rejects(f.host.control('label-task-page',{profile:f.p.id,target:retired.targetId,role:'task'}),/任务页面无效/);
  assert.equal(f.p.pages.get('old-task').role,'task');f.p.pages.delete(retired.targetId);
  await f.host.control('label-task-page',{profile:f.p.id,target:'new-task',role:'task'});
  assert.equal(f.p.pages.get('source').role,'source');assert.equal(f.page.role,'screening');assert.equal(f.page.roleIndex,1);
  const frame=await f.host.control('watch-profile',{profile:f.p.id,owner:f.p.owner});
  assert.deepEqual(Array.from(frame.pages,x=>[x.id,x.label]),[['source','采集页'],['screen','1-1'],['new-task','任务页']]);
  assert.equal(frame.target,'source');assert.equal(f.p.pages.get('old-task').role,undefined);
  assert.deepEqual(f.effects,{navigations:0,closed:0});assert.equal(f.p.pages.size,4);
});

test('owner navigation exposes loading until dom-ready then restores the same read-only task surface',async()=>{
  const f=fixture();assert.equal((await f.show()).attached,true);
  f.wc.emit('did-start-navigation',{},'https://www.instagram.com/next/',false,true);
  assert.equal(f.host.attached,undefined);assert.equal((await f.watch()).display_state,'loading');
  f.wc.url='https://www.instagram.com/next/';assert.equal((await f.show()).attached,false);
  f.wc.emit('dom-ready');assert.equal(f.host.attached,f.page);assert.equal(f.host.visible.readOnly,true);
  assert.equal((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,true);
  assert.equal(f.p.clients.size,1);assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('a crashed renderer with a still-live WebContents is detached and surfaced, without clearing the task lease',async()=>{
  const f=fixture();await f.show();f.wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
  assert.equal(f.wc.isDestroyed(),false);assert.equal(f.host.attached,undefined);
  assert.equal((await f.watch()).display_state,'crashed');assert.equal((await f.show()).attached,false);
  assert.equal(f.host.visible.target,'screen');assert.equal(f.p.pages.get('screen'),f.page);assert.equal(f.p.clients.size,1);
  f.wc.emit('dom-ready');assert.equal((await f.watch()).display_state,'crashed','late dom-ready cannot erase the exit');
  f.wc.emit('did-start-navigation',{},f.wc.url,false,true);f.wc.emit('dom-ready');assert.equal((await f.show()).attached,true);
  assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('main-frame load failure survives Chromium error-document dom-ready but a worker retry can recover',async()=>{
  const f=fixture();await f.show();f.wc.emit('did-fail-load',{},-105,'NAME_NOT_RESOLVED',f.wc.url,true);
  const frame=await f.watch();assert.equal(frame.display_state,'load_failed');assert.match(frame.display_message,/-105/);
  f.wc.emit('dom-ready');assert.equal((await f.show()).attached,false);
  f.wc.emit('did-start-navigation',{},f.wc.url,false,true);f.wc.emit('dom-ready');assert.equal((await f.show()).attached,true);
});

for(const [code,main]of [[-3,true],[-105,false]])test(`navigation error ${code}, main=${main}, does not suppress a healthy task page`,async()=>{
  const f=fixture();await f.show();f.wc.emit('did-fail-load',{},code,'error',f.wc.url,main);
  assert.equal((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,true);
});

test('unresponsive page shows a reason and responsive restores it without taking over navigation',async()=>{
  const f=fixture();await f.show();f.wc.emit('unresponsive');assert.equal((await f.show()).attached,false);
  assert.equal((await f.watch()).display_state,'unresponsive');f.wc.emit('responsive');assert.equal((await f.show()).attached,true);
  assert.equal(f.p.clients.size,1);assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('a hidden or expired grant cannot be restored by a late dom-ready event',async()=>{
  const f=fixture();await f.show();f.host.visible.deadline=Date.now()-1;f.wc.emit('dom-ready');
  assert.equal(f.host.attached,undefined);assert.equal(f.host.visible,undefined);
  await f.show();f.host.hide();f.wc.emit('dom-ready');assert.equal(f.host.attached,undefined);
});

test('background failure never hides the healthy selected sibling or changes the task selection',async()=>{
  const f=fixture();await f.show('source');const attached=f.host.attached;
  f.wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
  assert.equal(f.host.attached,attached);assert.equal(f.host.visible.target,'source');assert.equal(f.p.selected,'source');
  assert.equal((await f.watch('source')).display_state,'ready');assert.equal(f.p.clients.size,1);
});

test('read-only diagnostics keep exact-target and owner isolation, including after target removal',async()=>{
  const f=fixture();await assert.rejects(f.watch('screen','foreign'));await assert.rejects(f.watch('foreign-target'));
  f.p.pages.delete('screen');await assert.rejects(f.watch());await assert.rejects(f.show());
  assert.equal((await f.watch('source')).target,'source');assert.equal(f.p.clients.size,1);
});

for(const event of ['did-fail-load','did-fail-provisional-load'])test(`${event} cancellation restores the same ready old document without requiring another dom-ready`,async()=>{
  const f=fixture();await f.show();const previous=f.wc.url,next='https://www.instagram.com/cancelled/';
  f.wc.emit('did-start-navigation',{},next,false,true);assert.equal((await f.show()).attached,false);
  f.wc.emit(event,{},-3,'ERR_ABORTED',next,true);
  assert.equal(f.wc.url,previous);assert.equal((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,true);
  assert.equal(f.host.attached,f.page);assert.equal(f.p.clients.size,1);assert.deepEqual(f.effects,{navigations:0,closed:0});
});

test('cancellation waits for confirmed main-frame stop rather than exposing a still-loading document',async()=>{
  const f=fixture();await f.show();const next='https://www.instagram.com/cancelled/';f.wc.loading=true;
  f.wc.emit('did-start-navigation',{},next,false,true);f.wc.emit('did-fail-provisional-load',{},-3,'ERR_ABORTED',next,true);
  assert.equal((await f.watch()).display_state,'loading');assert.equal((await f.show()).attached,false);
  f.wc.loading=false;f.wc.emit('did-stop-loading');assert.equal((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,true);
});

for(const change of ['committed-same-url','new-frame','blank-before','crash-before'])test(`cancelled navigation cannot invent readiness after ${change}`,async()=>{
  const f=fixture();await f.show();let next='https://www.instagram.com/cancelled/';
  if(change==='blank-before'){f.wc.url='about:blank';f.wc.emit('dom-ready')}
  if(change==='crash-before')f.wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
  if(change==='committed-same-url')next=f.wc.url;
  f.wc.emit('did-start-navigation',{},next,false,true);
  if(change==='committed-same-url')f.wc.emit('did-navigate',{},next,200,'OK');
  if(change==='new-frame')f.wc.mainFrame={processId:20,routingId:99};
  f.wc.emit('did-fail-provisional-load',{},-3,'ERR_ABORTED',next,true);f.wc.emit('did-stop-loading');
  assert.notEqual((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,false);
});

test('late failure from a superseded navigation cannot overwrite a newer ready document',async()=>{
  const f=fixture();await f.show();const old='https://www.instagram.com/old/',next='https://www.instagram.com/next/';
  f.wc.emit('did-start-navigation',{},old,false,true);f.wc.emit('did-start-navigation',{},next,false,true);
  f.wc.url=next;f.wc.emit('did-navigate',{},next,200,'OK');f.wc.emit('dom-ready');
  for(const code of [-3,-105])f.wc.emit('did-fail-load',{},code,'late old failure',old,true);
  assert.equal((await f.watch()).display_state,'ready');assert.equal((await f.show()).attached,true);
});

test('old abort cannot mark its still-loading replacement cancelled',async()=>{
  const f=fixture();await f.show();const old='https://www.instagram.com/old/',next='https://www.instagram.com/next/';
  f.wc.loading=true;f.wc.emit('did-start-navigation',{},old,false,true);f.wc.emit('did-start-navigation',{},next,false,true);
  f.wc.emit('did-fail-provisional-load',{},-3,'ERR_ABORTED',old,true);f.wc.loading=false;f.wc.emit('did-stop-loading');
  assert.equal((await f.watch()).display_state,'loading');assert.equal((await f.show()).attached,false);
});

test('redirected main-frame failure is still reported for the current navigation',async()=>{
  const f=fixture();await f.show();const first='https://www.instagram.com/first/',redirect='https://www.instagram.com/redirect/';
  f.wc.emit('did-start-navigation',{},first,false,true);f.wc.emit('did-redirect-navigation',{},redirect,false,true);
  f.wc.emit('did-fail-load',{},-105,'failure',redirect,true);
  assert.equal((await f.watch()).display_state,'load_failed');assert.equal((await f.show()).attached,false);
});


test('r51 Core-authorized manual task page retains keyboard focus while its CDP client and task binding remain',async()=>{
 const f=fixture();await f.show('screen');
 await f.host.control('show',{profile:f.p.id,grant:f.host.requestSurface(),bounds:{x:100,y:80,width:1100,height:800},target:'screen',read_only:false});
 f.wc.focus();assert.equal(f.wc.isFocused(),true);
 const background=f.p.pages.get('source').view.webContents;background.focus();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.wc.isFocused(),true);assert.equal(background.isFocused(),false);assert.equal(f.p.clients.size,1);assert.equal(f.p.selected,'source');assert.equal(f.host.visible.readOnly,false);
});

test('r51 revoking manual surface leaves keyboard at the shell, never the old task page',async()=>{
 const f=fixture();await f.host.control('show',{profile:f.p.id,grant:f.host.requestSurface(),bounds:{x:100,y:80,width:1100,height:800},target:'screen',read_only:false});
 f.wc.focus();f.host.hide();f.p.pages.get('source').view.webContents.focus();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.win.webContents.isFocused(),true);assert.equal(f.wc.isFocused(),false);assert.equal(f.p.clients.size,1);
});
