import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {EventEmitter} from 'node:events';
import {runInNewContext} from 'node:vm';

// Actual host methods, with native getter/CDP/focus counters. These are ordering
// and call-count checks, not a Windows CPU, memory, or frame-rate benchmark.
function compile(name){
  const source=readFileSync(new URL('../src/'+name,import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
  return (nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
}
const hostCode=process.env.JUXIN_HOST_SOURCE
  ?(nodeModule.createRequire(import.meta.url)('typescript').transpileModule(readFileSync(process.env.JUXIN_HOST_SOURCE,'utf8').replace(/^import .*;\r?\n/gm,''),{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'')
  :compile('embedded-browser.ts');
const accountViewport=runInNewContext(compile('account-viewport.ts')+';accountViewport');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
function fixture(){
  let serial=0,focused;
  const jobs=[],calls={getBounds:0,setBounds:0,metrics:0,focus:[]};
  class Contents extends EventEmitter{
    id=++serial;dead=false;
    debugger=Object.assign(new EventEmitter(),{isAttached:()=>true,sendCommand:async method=>{if(method==='Emulation.setDeviceMetricsOverride')calls.metrics++;return {targetInfo:{targetId:'target-'+this.id}}}});
    isDestroyed(){return this.dead}getURL(){return 'https://www.instagram.com/'}getTitle(){return 'page'}getZoomFactor(){return 1}
    setUserAgent(){}setWindowOpenHandler(){}executeJavaScriptInIsolatedWorld(){return Promise.resolve()}loadURL(){return Promise.resolve()}
    isFocused(){return focused===this}focus(){focused=this;calls.focus.push(this.id);this.emit('focus')}
  }
  class View{
    children=[];bounds={x:0,y:0,width:1280,height:900};visible=true;webContents=new Contents();
    getBounds(){calls.getBounds++;return {...this.bounds}}setBounds(b){calls.setBounds++;this.bounds={...b}}
    setVisible(v){this.visible=v}getVisible(){return this.visible}setBorderRadius(){}setBackgroundColor(){}
    addChildView(v,index){this.children=this.children.filter(child=>child!==v);if(index===undefined)this.children.push(v);else this.children.splice(index,0,v)}
    removeChildView(v){this.children=this.children.filter(child=>child!==v)}
  }
  // Electron's overlay contentView and its main webContents are distinct.
  const shell=new View(),win=Object.assign(new EventEmitter(),{contentView:shell,webContents:new Contents(),isDestroyed:()=>false,isMinimized:()=>false,isFocused:()=>true,getContentSize:()=>[1600,1000],setContentView(v){this.contentView=v}});
  const TaskWatchShield=runInNewContext(compile('task-watch-shield.ts')+';TaskWatchShield',{WebContentsView:View,encodeURIComponent});
  const Host=runInNewContext(hostCode+';EmbeddedBrowserHost',{
    URL,setTimeout,clearTimeout,clearInterval,accountViewport,
    setImmediate:fn=>jobs.push(fn),randomBytes:()=>({toString:()=>String(++serial)}),WebSocketServer:class{},
    View,WebContentsView:View,TaskWatchShield,AccountUnreadCache:class{},whatsappColumnLayoutScript:()=>'',diagnosticCodes:()=>[],whatsappErrorText:String,
  });
  const host=new Host(()=>win);host.attachWindow(win);
  function profile(id,task=false,pages=1){
    const p={id,owner:'owner',session:{getUserAgent:()=>''},pages:new Map(),clients:new Set(task?[{}]:[]),closed:false};
    host.profiles.set(id,p);
    for(let i=0;i<pages;i++){
      const view=host.makeView(p),page={view,targetId:id+'-'+i};host.pageReady.add(view.webContents);
      host.panes.get(win).addChildView(view,0); // registerPage's native attachment
      p.pages.set(page.targetId,page);p.selected??=page.targetId;
    }
    return p;
  }
  return {host,win,shell,calls,jobs,profile,bounds:{x:200,y:80,width:1200,height:850},reset(){calls.getBounds=0;calls.setBounds=0;calls.metrics=0;calls.focus=[]}};
}

test('account display preserves the native overlay root through show, hide, navigation, and resize',()=>{
  const f=fixture(),root=f.shell,wc=f.win.webContents;
  assert.equal(f.win.contentView,root,'attach must not replace the native root');
  const pane=root.children[0];assert.equal(pane.getVisible(),false);
  const overlay={name:'independent-overlay'};root.addChildView(overlay);
  const p=f.profile('visible'),task=f.profile('task',true);
  for(const end of [()=>f.host.hide(),()=>f.win.emit('resize'),()=>wc.emit('did-start-navigation',{},'app://next',false,true)]){
    f.host.show(p,f.bounds);assert.equal(pane.getVisible(),true);
    end();assert.equal(pane.getVisible(),false);
    assert.equal(f.win.contentView,root);assert.equal(f.win.webContents,wc);
    assert.equal(root.children.includes(root),false);assert.ok(root.children.includes(overlay));
    assert.equal(task.clients.size,1,'display cannot release a background task');
    assert.ok(pane.children.includes(task.pages.get(task.selected).view));
  }
  const children=[...root.children];f.host.attachWindow(f.win);
  assert.deepEqual(root.children,children,'repeat attachment must not duplicate the pane');
});

test('identical display heartbeats avoid native bounds reads and unchanged writes across 60 pages',async()=>{
  const f=fixture(),p=f.profile('selected',false,3);for(let i=1;i<20;i++)f.profile('task-'+i,true,3);
  f.host.show(p,f.bounds);await flush();f.reset();
  for(let i=0;i<100;i++){f.host.setWorkspaceBounds(f.bounds);f.host.show(p,f.bounds)}await flush();
  assert.equal(f.calls.getBounds,0);assert.equal(f.calls.setBounds,0);assert.equal(f.calls.metrics,0);
});

test('workspace resize changes the visible idle page while task and hidden viewports retain their dimensions',async()=>{
  const f=fixture(),active=f.profile('visible'),task=f.profile('task',true),hidden=f.profile('hidden');
  f.host.show(active,f.bounds);await flush();
  const taskView=[...task.pages.values()][0].view,hiddenView=[...hidden.pages.values()][0].view;
  const hiddenBounds={...hiddenView.bounds};f.host.setWorkspaceBounds({...f.bounds,width:900,height:700});await flush();
  assert.deepEqual({...taskView.bounds},{x:0,y:0,width:1280,height:900});assert.deepEqual({...hiddenView.bounds},hiddenBounds);
  assert.deepEqual({...active.pages.get(active.selected).view.bounds},{x:0,y:0,width:900,height:700});
  assert.equal(task.clients.size,1,'layout cannot release or suspend a task to reduce work');
});

test('r94 task input shield follows every workspace size without resizing or refocusing the worker',async()=>{
  const f=fixture(),p=f.profile('task',true),page=p.pages.get(p.selected);
  f.host.show(p,{...f.bounds,width:700,height:600},page.targetId,true);await flush();f.reset();
  const shield=f.host.watchShield.view;
  for(const [width,height] of [[1200,850],[500,400],[900,700]]){
    const bounds={...f.bounds,width,height};f.host.setWorkspaceBounds(bounds);
    assert.deepEqual({...shield.bounds},{x:0,y:0,width,height},'shield must cover the pane before authorization completes');
    f.host.show(p,bounds,page.targetId,true);
    assert.deepEqual({...shield.bounds},{x:0,y:0,width,height});
    assert.deepEqual({...page.view.bounds},{x:0,y:0,width:1280,height:900});
    assert.equal(f.host.panes.get(f.win).children.at(-1),shield);
  }
  await flush();assert.equal(p.clients.size,1);assert.equal(f.calls.metrics,0);assert.deepEqual(f.calls.focus,[]);
  f.reset();for(let i=0;i<50;i++)f.host.show(p,{...f.bounds,width:900,height:700},page.targetId,true);
  assert.equal(f.calls.setBounds,0,'unchanged shield heartbeats must avoid native writes');
});

for(const newDocument of [false,true])test(`late resize failure cannot clear a newer successful ${newDocument?'same-size new-document':'different-size'} request`,async()=>{
  const f=fixture(),p=f.profile('visible'),view=p.pages.get(p.selected).view,old=deferred(),next=deferred();let requests=0;
  view.webContents.debugger.sendCommand=()=>++requests===1?old.promise:requests===2?next.promise:Promise.resolve();
  view.setBounds({...view.bounds,width:900});const first=f.host.sizePageViewport(view).catch(()=>{});
  if(newDocument){view.webContents.emit('did-start-navigation',{},view.webContents.getURL(),false,true);f.host.pageReady.add(view.webContents)}
  else view.setBounds({...view.bounds,width:1000});
  const second=f.host.sizePageViewport(view);next.resolve({});await second;old.reject(new Error('old size failed'));await first;
  await f.host.sizePageViewport(view);assert.equal(requests,2);
});

test('queued background-focus correction never focuses the old input page after it has been hidden',()=>{
  const f=fixture(),a=f.profile('visible'),b=f.profile('background');
  const wa=a.pages.get(a.selected).view.webContents,wb=b.pages.get(b.selected).view.webContents;
  f.host.show(a,f.bounds);wa.focus();f.calls.focus=[];wb.focus();assert.equal(f.jobs.length,1);
  f.host.hide();f.jobs.shift()();
  assert.equal(f.calls.focus.includes(wa.id),false,'an old input surface must still be the authorized visible page');
  assert.equal(f.calls.focus.at(-1),f.win.webContents.id);
});

test('queued background-focus correction leaves a later legitimate foreground focus alone',()=>{
  const f=fixture(),a=f.profile('old'),b=f.profile('background'),c=f.profile('new');
  const wa=a.pages.get(a.selected).view.webContents,wb=b.pages.get(b.selected).view.webContents,wc=c.pages.get(c.selected).view.webContents;
  f.host.show(a,f.bounds);wa.focus();wb.focus();assert.equal(f.jobs.length,1);
  f.host.show(c,f.bounds);wc.focus();f.calls.focus=[];f.jobs.shift()();
  assert.deepEqual(f.calls.focus,[]);assert.equal(wc.isFocused(),true);
});

test('several background focus events in one turn restore the last legitimate foreground input',()=>{
 const f=fixture(),a=f.profile('visible'),b=f.profile('background-b'),c=f.profile('background-c');
 const wa=a.pages.get(a.selected).view.webContents,wb=b.pages.get(b.selected).view.webContents,wc=c.pages.get(c.selected).view.webContents;
 f.host.show(a,f.bounds);wa.focus();wb.focus();wc.focus();
 assert.equal(f.jobs.length,1);f.jobs.shift()();assert.equal(wa.isFocused(),true);assert.equal(wc.isFocused(),false);
});

test('a legitimate focus change between background intrusions becomes the restoration target',()=>{
 const f=fixture(),a=f.profile('old'),b=f.profile('background-b'),c=f.profile('background-c'),d=f.profile('new');
 const wa=a.pages.get(a.selected).view.webContents,wb=b.pages.get(b.selected).view.webContents,wc=c.pages.get(c.selected).view.webContents,wd=d.pages.get(d.selected).view.webContents;
 f.host.show(a,f.bounds);wa.focus();wb.focus();f.host.show(d,f.bounds);wd.focus();wc.focus();
 assert.equal(f.jobs.length,1);f.jobs.shift()();assert.equal(wd.isFocused(),true);assert.equal(wa.isFocused(),false);
});

test('a queued focus intruder that becomes the authorized foreground keeps its input focus',()=>{
 const f=fixture(),a=f.profile('old'),b=f.profile('next');
 const wa=a.pages.get(a.selected).view.webContents,wb=b.pages.get(b.selected).view.webContents;
 f.host.show(a,f.bounds);wa.focus();wb.focus();f.host.show(b,f.bounds);wb.focus();f.calls.focus=[];
 assert.equal(f.jobs.length,1);f.jobs.shift()();assert.equal(wb.isFocused(),true);assert.deepEqual(f.calls.focus,[]);
});
