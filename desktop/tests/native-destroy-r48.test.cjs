const test=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {join}=require('node:path');
const nodeModule=require('node:module');
const {EventEmitter}=require('node:events');
const {runInNewContext}=require('node:vm');

// Run production teardown methods with Electron's disappearing native getter.
// These fixtures validate lifecycle ordering, not native Windows rendering.
function compile(name){
  const source=readFileSync(join(__dirname,'../src',name),'utf8').replace(/^import .*;\r?\n/gm,'');
  return (nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):require('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
}
const hostSource=compile('embedded-browser.ts'),cdpSource=compile('embedded-cdp.ts'),shieldSource=compile('task-watch-shield.ts');
const accountViewport=runInNewContext(compile('account-viewport.ts')+';accountViewport');
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve}}
function fixture({shieldLoadFailure=false}={}){
  let serial=0,sessionSerial=0,focused;
  const closed=[],calls=[],pageClosed=[];
  class Contents extends EventEmitter{
    id=++serial;dead=false;url='https://www.instagram.com/account/';closeFailure;
    debugger=Object.assign(new EventEmitter(),{attach(){},isAttached:()=>true,sendCommand:async(method,params)=>{
      calls.push({method,params});
      if(method==='Target.getTargetInfo')return {targetInfo:{targetId:'target-'+this.id}};
      if(method==='Target.attachToTarget')return {sessionId:'session-'+(++sessionSerial)};
      return {};
    }});
    isDestroyed(){return this.dead}getURL(){return this.url}getTitle(){return 'Account'}
    setWindowOpenHandler(){}loadURL(url){return shieldLoadFailure&&url?.startsWith('data:')?Promise.reject(new Error('shield load failed')):Promise.resolve()}
    focus(){focused=this}isFocused(){return focused===this}
    close(){if(this.closeFailure)throw this.closeFailure;assert.equal(this.dead,false,'must not close native contents twice');closed.push(this.id);this.dead=true;this.emit('destroyed')}
  }
  class View{
    children=[];bounds={x:0,y:0,width:1280,height:900};visible=true;contents=new Contents();
    get webContents(){return this.contents.dead?undefined:this.contents}
    getBounds(){return {...this.bounds}}setBounds(bounds){this.bounds={...bounds}}
    setVisible(value){this.visible=value}getVisible(){return this.visible}setBorderRadius(){}setBackgroundColor(){}
    addChildView(view,index){this.children=this.children.filter(v=>v!==view);index===undefined?this.children.push(view):this.children.splice(index,0,view)}
    removeChildView(view){this.children=this.children.filter(v=>v!==view)}
  }
  class Socket extends EventEmitter{
    static OPEN=1;readyState=1;sent=[];
    send(raw){this.sent.push(JSON.parse(raw))}close(){this.readyState=3;this.emit('close')}
  }
  const Shield=runInNewContext(shieldSource+';TaskWatchShield',{WebContentsView:View,encodeURIComponent});
  const shell=new View(),win=Object.assign(new EventEmitter(),{contentView:shell,webContents:shell.contents,
    isDestroyed:()=>false,isMinimized:()=>false,isFocused:()=>true,getContentSize:()=>[1600,1000],setContentView(view){this.contentView=view}});
  const Host=runInNewContext(hostSource+';EmbeddedBrowserHost',{
    URL,setTimeout,clearTimeout,clearInterval,setImmediate,accountViewport,
    randomBytes:()=>({toString:()=>String(++serial)}),WebSocketServer:class{},View,WebContentsView:View,
    TaskWatchShield:Shield,AccountUnreadCache:class{},whatsappColumnLayoutScript:()=>'',
  });
  const host=new Host(()=>win);host.attachWindow(win);
  const owner={pageCreated:async()=>{},pageClosed:page=>pageClosed.push(page.targetId),close(){},dispose:async()=>{}};
  const stored=[];
  const profile={id:'profile',owner:'owner',generation:1,session:{cookies:{flushStore:async()=>stored.push('cookies')},flushStorageData(){stored.push('storage')}},
    pages:new Map(),clients:new Set([owner]),closed:false};
  host.profiles.set(profile.id,profile);
  async function page(role='screening'){
    const view=new View(),contents=view.contents;
    const result=await host.registerPage(profile,view);result.role=role;
    return {page:result,contents,view};
  }
  const Cdp=runInNewContext(cdpSource+';AccountCdpConnection',{
    URL,process,WebSocket:Socket,setImmediate,randomBytes:()=>({toString:()=>String(++serial)}),
    permittedPageUrl:value=>/^https?:|^about:blank$/.test(value),
  });
  function connection(){const socket=new Socket(),client=new Cdp(host,profile,socket);profile.clients.add(client);return {socket,client}}
  const show=page=>host.show(profile,{x:100,y:80,width:1100,height:800},page.targetId,true);
  return {host,profile,owner,stored,page,closed,calls,pageClosed,show,Shield,View,connection,win};
}

test('destroyed selected page completes map, selection and surface cleanup while retaining siblings and task lease',async()=>{
  const f=fixture(),source=await f.page('source'),sibling=await f.page(),selected=await f.page();
  f.show(selected.page);const lease=f.host.visible,clients=f.profile.clients;
  assert.equal(f.host.attached,selected.page);
  assert.doesNotThrow(()=>selected.contents.close());
  assert.equal(selected.view.webContents,undefined);
  assert.equal(f.profile.pages.has(selected.page.targetId),false);
  assert.equal(f.profile.selected,sibling.page.targetId);
  assert.equal(f.host.attached,undefined);
  assert.equal(f.host.visible,lease,'destruction must not release or rewrite the viewing lease');
  assert.equal(f.host.visible.target,selected.page.targetId,'must not silently take over a sibling');
  assert.equal(f.profile.clients,clients);assert.equal(clients.size,1);assert.equal(f.profile.closed,false);
  assert.equal(f.profile.pages.get(source.page.targetId),source.page);
  assert.equal(f.profile.pages.get(sibling.page.targetId),sibling.page);
  assert.equal(source.contents.isDestroyed(),false);assert.equal(sibling.contents.isDestroyed(),false);
  assert.deepEqual(f.pageClosed,[selected.page.targetId]);
});

test('background page destruction preserves the healthy displayed sibling',async()=>{
  const f=fixture(),selected=await f.page('source'),background=await f.page();
  f.profile.selected=selected.page.targetId;f.show(selected.page);const lease=f.host.visible;
  background.contents.close();
  assert.equal(f.host.attached,selected.page);assert.equal(f.host.visible,lease);
  assert.equal(f.profile.selected,selected.page.targetId);assert.equal(f.profile.clients.size,1);
  assert.equal(f.profile.pages.has(background.page.targetId),false);
});

for(const external of [false,true])test(`task watch shield close tolerates vanished getter after ${external?'external':'own'} destruction`,()=>{
  const f=fixture(),shield=new f.Shield(()=>{}),contents=shield.view.webContents;
  if(external)contents.close();else shield.close();
  assert.equal(shield.view.webContents,undefined);
  assert.doesNotThrow(()=>shield.close());assert.doesNotThrow(()=>shield.close());
  assert.deepEqual(f.closed,[contents.id]);
});

test('closeProfile forgets already destroyed pending view and still joins live-page close and cookie persistence',async()=>{
  const f=fixture(),live=await f.page(),orphan=new f.View(),contents=orphan.webContents;
  f.host.pendingPages.set(f.profile,new Set([orphan]));contents.close();
  const gate=deferred();f.profile.session.cookies.flushStore=async()=>{f.stored.push('cookies-start');await gate.promise;f.stored.push('cookies-done')};
  const closing=f.host.closeProfile(f.profile);await flush();
  assert.equal(f.host.pendingPages.has(f.profile),false);assert.equal(live.contents.isDestroyed(),true);
  assert.equal(f.host.profiles.has(f.profile.id),true,'profile cleanup must wait for storage');
  assert.deepEqual(f.stored,['cookies-start']);
  gate.resolve();await closing;
  assert.deepEqual(f.stored,['cookies-start','cookies-done','storage']);
  assert.equal(f.host.profiles.has(f.profile.id),false);assert.equal(f.profile.clients.size,0);
});

test('CDP detach microtask tolerates page destruction before it executes without clearing sibling ownership',async()=>{
  const f=fixture(),first=await f.page(),sibling=await f.page(),{client}=f.connection();
  const sessionId=await client.attach(first.page);
  const detaching=client.detachSession(sessionId);first.contents.close();await detaching;
  assert.equal(client.sessions.size,0);assert.equal(client.roots.size,0);
  assert.equal(f.profile.pages.get(sibling.page.targetId),sibling.page);
  assert.equal(f.profile.clients.has(client),true);assert.equal(sibling.contents.isDestroyed(),false);
  assert.equal(f.calls.filter(c=>c.method==='Target.detachFromTarget').length,0,'destroyed native page must not receive another debugger command');
});

test('CDP failed-create cleanup recognizes already destroyed page and clears its owned-page record',async()=>{
  const f=fixture(),first=await f.page(),{client}=f.connection();
  client.createdPages.add(first.page);first.contents.close();
  await client.closeCreatedPage(first.page);await client.closeCreatedPage(first.page);
  assert.equal(client.createdPages.has(first.page),false);
  assert.equal(f.closed.filter(id=>id===first.contents.id).length,1);
});

test('live CDP detach failure is still rejected and retains the binding for retry',async()=>{
  const f=fixture(),first=await f.page(),{client}=f.connection();
  const sessionId=await client.attach(first.page);
  first.contents.debugger.sendCommand=async()=>{throw new Error('native detach failed')};
  await assert.rejects(client.detachSession(sessionId),/native detach failed/);
  assert.equal(client.sessions.has(sessionId),true);assert.equal(f.profile.clients.has(client),true);
});

test('live close failure still rejects, retains profile ownership and flushes storage',async()=>{
  const f=fixture(),first=await f.page();first.contents.closeFailure=new Error('native close failed');
  await assert.rejects(f.host.closeProfile(f.profile),/native close failed/);
  assert.equal(f.host.profiles.get(f.profile.id),f.profile);assert.equal(first.contents.isDestroyed(),false);
  assert.equal(f.profile.pages.get(first.page.targetId),first.page);assert.deepEqual(f.stored,['cookies','storage']);
});

test('r94 late debugger registration cannot publish a page whose native contents already closed',async()=>{
  const f=fixture(),sibling=await f.page('source'),view=new f.View(),wc=view.contents,gate=deferred();
  wc.debugger.sendCommand=()=>gate.promise;
  const registration=f.host.registerPage(f.profile,view);
  wc.close();gate.resolve({targetInfo:{targetId:'closed-before-registration'}});
  await assert.rejects(registration,/关闭/);
  assert.equal(f.profile.pages.has('closed-before-registration'),false);
  assert.equal(f.profile.selected,sibling.page.targetId);
  assert.equal(f.profile.clients.size,1);
});

for(const reason of ['destroyed','render-process-gone','unresponsive','did-fail-load'])test(`r94 failed task shield hides the surface and recovers without replacing the worker (${reason})`,async()=>{
  const f=fixture(),page=await f.page('source');f.show(page.page);
  const old=f.host.watchShield,wc=old.view.webContents,lease=f.host.visible;
  if(reason==='destroyed')wc.close();
  else if(reason==='did-fail-load')wc.emit(reason,{},-2,'failed','data:text/html',true);
  else wc.emit(reason,{}, {reason:'crashed',exitCode:1});
  assert.equal(f.host.attached,undefined,'a failed native input shield must immediately detach the task surface');
  assert.equal(f.host.visible,lease);assert.equal(f.profile.clients.size,1);assert.equal(page.contents.isDestroyed(),false);
  f.show(page.page);
  assert.notEqual(f.host.watchShield,old);assert.equal(f.host.attached,page.page);
  assert.equal(f.host.panes.get(f.win).children.at(-1),f.host.watchShield.view);
  assert.equal(f.host.panes.get(f.win).children.includes(old.view),false);
  assert.equal(f.profile.pages.size,1);assert.equal(f.profile.clients.size,1);assert.equal(page.contents.isDestroyed(),false);
});

test('r94 an obsolete shield event cannot hide a subsequently authorized manual surface',async()=>{
  const f=fixture(),page=await f.page('source');f.show(page.page);const old=f.host.watchShield;
  f.host.show(f.profile,{x:100,y:80,width:1100,height:800},page.page.targetId,false);
  old.view.webContents.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
  assert.equal(f.host.attached,page.page);assert.equal(f.host.visible.readOnly,false);
  assert.equal(f.profile.clients.size,1);
});

test('r94 rejected shield loading is handled and keeps the worker hidden and owned',async()=>{
  const f=fixture({shieldLoadFailure:true}),page=await f.page('source');f.show(page.page);await flush();
  assert.equal(f.host.attached,undefined);assert.equal(f.host.watchShield.isUsable(),false);
  assert.equal(f.profile.clients.size,1);assert.equal(page.contents.isDestroyed(),false);
  f.host.hide();await flush();assert.equal(f.host.visible,undefined);
});

test('r94 repeated shield recovery removes each old native guard and ignores its late events',async()=>{
  const f=fixture(),page=await f.page('source');f.show(page.page);
  const pane=f.host.panes.get(f.win),originalPages=f.profile.pages;
  for(let i=0;i<20;i++){
    const old=f.host.watchShield,wc=old.view.webContents;
    wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});f.show(page.page);
    wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
    assert.equal(f.host.attached,page.page);assert.equal(pane.children.length,2);
    assert.equal(pane.children.at(-1),f.host.watchShield.view);assert.equal(wc.isDestroyed(),true);
    assert.equal(f.profile.pages,originalPages);assert.equal(f.profile.clients.size,1);
    assert.equal(page.contents.isDestroyed(),false);
  }
});

for(const active of [true,false])for(const reason of ['hide','unresponsive'])test(`r94 hiding a focused task shield restores shell input only in the active app (${reason}, active=${active})`,async()=>{
  const f=fixture(),page=await f.page('source');f.show(page.page);
  const wc=f.host.watchShield.view.webContents;assert.equal(wc.isFocused(),true);
  f.win.isFocused=()=>active;
  if(reason==='hide')f.host.hide();else wc.emit('unresponsive');
  assert.equal(f.host.attached,undefined);
  assert.equal(f.win.webContents.isFocused(),active,'an active shell must recover keyboard input from its hidden shield');
  assert.equal(f.profile.clients.size,1);assert.equal(page.contents.isDestroyed(),false);
});
