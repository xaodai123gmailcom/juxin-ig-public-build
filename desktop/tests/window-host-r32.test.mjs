import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';

// Execute the production host with controlled Electron transports and clocks.
// This validates lifecycle ordering, not native Windows rendering or WA login.
const source=readFileSync(new URL('../src/embedded-browser.ts',import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
const compiled=(nodeModule.stripTypeScriptTypes
  ?nodeModule.stripTypeScriptTypes(source,{mode:'transform'})
  :nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
const owner='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',id='native:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

function fixture(){
  let now=0,sequence=0,shutdown=false;
  const timers=new Map(),observations=[],sessionGate=deferred();
  class Contents extends EventEmitter{
    destroyed=false;reads=[];url='https://web.whatsapp.com/';
    setUserAgent(){} setWindowOpenHandler(){} isDestroyed(){return this.destroyed} getURL(){return this.url}
    executeJavaScriptInIsolatedWorld(){const value=deferred();this.reads.push(value);return value.promise}
    close(){this.destroyed=true;this.emit('destroyed')}
  }
  class FakeView{webContents=new Contents();setBounds(){} setBackgroundColor(){}}
  class FakeSockets{clients=[];close(){shutdown=true}}
  const Host=runInNewContext(compiled+';EmbeddedBrowserHost',{
    URL,setImmediate,clearInterval,
    randomBytes:()=>({toString:()=>`capability-${++sequence}`}),WebSocketServer:FakeSockets,
    WebContentsView:FakeView,createWhatsAppView:()=>new FakeView(),
    createWhatsAppSession:()=>sessionGate.promise,session:{fromPartition:()=>session},accountUserAgent:()=>'',
    chatTranslationPage:function(){return {}},
    AccountUnreadCache:class{observe(...args){observations.push(args)}},unreadPageScript:'test',
    setTimeout(fn,ms){const key=++sequence;timers.set(key,{at:now+ms,fn});return key},clearTimeout(key){timers.delete(key)},
    WhatsAppPageRuntime:class{inspect(){return Promise.resolve({chatReady:true})}},
  });
  const host=new Host(()=>null);
  const session={getUserAgent:()=>'',setProxy:async()=>{},setUserAgent(){},setPermissionCheckHandler(){},setPermissionRequestHandler(){},webRequest:{onErrorOccurred(){},onCompleted(){}},cookies:{on(){},flushStore:async()=>{}},flushStorageData(){}};
  const p={id,owner,proxy:'',closed:false,clients:new Set(),pages:new Map(),session,selected:'page-1',generation:1};
  const view=host.makeView(p);p.pages.set(p.selected,{targetId:p.selected,view});host.profiles.set(id,p);
  return {host,p,wc:view.webContents,observations,sessionGate,session,
    get shutdown(){return shutdown},
    async advance(ms){now+=ms;for(const [key,timer]of [...timers])if(timer.at<=now){timers.delete(key);timer.fn()}await flush()},
  };
}

test('a timed-out renderer stays single-flight until that document changes',async()=>{
  const f=fixture();const first=f.host.readUnread(f.p);await flush();assert.equal(f.wc.reads.length,1);
  await f.advance(2000);await first;await f.host.readUnread(f.p);assert.equal(f.wc.reads.length,1);
  f.wc.reads[0].resolve({status:'live',count:1});await flush();
});

test('same-URL reload starts a new unread read and late old failure cannot stale its result',async()=>{
  const f=fixture();const old=f.host.readUnread(f.p);await flush();await f.advance(2000);await old;
  f.wc.emit('did-start-navigation',{},f.wc.url,false,true);
  const next=f.host.readUnread(f.p);await flush();
  assert.equal(f.wc.reads.length,2,'the new document must not be permanently blocked by an abandoned read');
  f.wc.reads[1].resolve({status:'live',count:7});await next;
  f.wc.reads[0].reject(new Error('old renderer gone'));await flush();
  assert.equal(f.observations.at(-1)[2].count,7);
});

test('old unread finalizer cannot release the new document single-flight lock',async()=>{
  const f=fixture();const old=f.host.readUnread(f.p);await flush();
  f.wc.emit('did-start-navigation',{},f.wc.url,false,true);
  const next=f.host.readUnread(f.p);await flush();assert.equal(f.wc.reads.length,2);
  f.wc.reads[0].resolve({status:'live',count:1});await old;
  const joined=f.host.readUnread(f.p);await flush();assert.equal(f.wc.reads.length,2);
  f.wc.reads[1].resolve({status:'live',count:7});await next;await joined;
  assert.equal(f.observations.length,1);assert.equal(f.observations[0][2].count,7);
});

for(const transition of ['selection','task','replacement','reload','crash'])test(`late unread rejection after ${transition} cannot overwrite current state`,async()=>{
  const f=fixture();const old=f.host.readUnread(f.p);await flush();
  if(transition==='selection')f.p.selected='page-2';
  else if(transition==='task')f.p.clients.add({});
  else if(transition==='replacement')f.host.profiles.set(id,{...f.p,generation:2});
  else if(transition==='reload')f.wc.emit('did-start-navigation',{},f.wc.url,false,true);
  else f.wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1});
  f.wc.reads[0].reject(new Error('old read failed'));await old;assert.equal(f.observations.length,0);
});

test('subframe navigation keeps the current unread read single-flight',async()=>{
  const f=fixture();const old=f.host.readUnread(f.p);await flush();
  f.wc.emit('did-start-navigation',{},'https://example.invalid/',false,false);
  await f.host.readUnread(f.p);assert.equal(f.wc.reads.length,1);
  f.wc.reads[0].resolve({status:'live',count:3});await old;assert.equal(f.observations[0][2].count,3);
});

test('shutdown waits for an in-progress session open and never creates its late page',async()=>{
  const f=fixture();f.host.profiles.clear();let created=0;
  f.host.newPage=async()=>{created++};
  const opening=f.host.ensure(id,owner,'',true,'WA',true).then(()=>null,error=>error);
  let stopped=false;const stopping=f.host.stop().then(()=>{stopped=true});await flush();
  assert.equal(stopped,false,'shutdown cannot finish while an opening session is unaccounted for');
  f.sessionGate.resolve(f.session);const error=await opening;await stopping;
  assert.match(error?.message||'',/关闭|退出/);assert.equal(created,0);assert.equal(f.host.profiles.size,0);assert.equal(f.shutdown,true);
});

test('new opens and page creation are refused after shutdown starts',async()=>{
  const f=fixture();f.host.profiles.clear();await f.host.stop();
  await assert.rejects(f.host.ensure(id,owner,'',true,'WA',true),/关闭|退出/);
  await assert.rejects(f.host.newPage(f.p,'about:blank'),/关闭|退出/);
});

for(const transition of ['task','selection','reload','hidden','replacement'])test(`translation cannot enter the page after inspection crosses ${transition}`,async()=>{
  const f=fixture(),inspection=deferred(),page=f.p.pages.get(f.p.selected);
  f.host.visible={profile:id,deadline:Date.now()+60000};
  f.host.whatsappPages.set(page.view,{inspect:()=>inspection.promise});
  const result=f.host.control('chat-translation',{profile:id,owner,generation:1,step:{kind:'apply',id:'message',conversation:'chat',original:'hello',translation:'你好'}});
  if(transition==='task')f.p.clients.add({});
  else if(transition==='selection')f.p.selected='new-page';
  else if(transition==='reload')f.wc.emit('did-start-navigation',{},f.wc.url,false,true);
  else if(transition==='hidden')f.host.visible=undefined;
  else f.host.profiles.set(id,{...f.p,generation:2});
  inspection.resolve({chatReady:true});await assert.rejects(result,/窗口|任务/);assert.equal(f.wc.reads.length,0);
});

test('unchanged idle chat can apply a translation after its inspection',async()=>{
  const f=fixture(),page=f.p.pages.get(f.p.selected);
  f.host.visible={profile:id,deadline:Date.now()+60000};
  f.host.whatsappPages.set(page.view,{inspect:async()=>({chatReady:true})});
  const result=f.host.control('chat-translation',{profile:id,owner,generation:1,step:{kind:'apply',id:'message',conversation:'chat',original:'hello',translation:'你好'}});
  await flush();assert.equal(f.wc.reads.length,1);f.wc.reads[0].resolve({applied:true});assert.equal((await result).applied,true);
});

test('shutdown destroys a half-registered page before waiting for its stalled debugger registration',async()=>{
  const f=fixture(),command=deferred(),win={isDestroyed:()=>false};let flushed=0,stopped=false;
  f.host.profiles.clear();f.host.window=()=>win;
  for(const method of ['attachWindow','layoutPane','raiseShell','render','hide'])f.host[method]=()=>{};
  f.host.sizePageViewport=async()=>{};
  f.host.panes.set(win,{addChildView(){},removeChildView(){}});
  f.wc.debugger={attach(){},sendCommand:()=>command.promise,on(){}};
  f.host.makeView=()=>({webContents:f.wc});f.session.cookies.flushStore=async()=>{flushed++};
  const opening=f.host.ensure(id,owner,'',true).then(()=>null,error=>error);
  await flush();assert.equal(f.host.profiles.get(id).pages.size,0);assert.equal(f.host.opening.size,1);
  const stopping=f.host.stop().then(()=>{stopped=true});
  try{
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(stopped,true,'destroyed must release registration even if its debugger promise never settles');
    assert.equal(f.wc.isDestroyed(),true);assert.equal(flushed,1);assert.equal(f.shutdown,true);
  }finally{command.resolve({targetInfo:{targetId:'late'}});await opening;await stopping}
  assert.equal(f.host.profiles.size,0);assert.equal(f.host.pendingPages.size,0);
});

test('profile shutdown waits for real client cleanup before saving or releasing the profile',async()=>{
  const f=fixture(),cleanup=deferred();let flushed=false,closed=false;
  f.session.cookies.flushStore=async()=>{flushed=true};
  f.p.clients.add({close(){},dispose:()=>cleanup.promise});
  const closing=f.host.closeProfile(f.p).then(()=>{closed=true});await flush();
  assert.equal(f.wc.isDestroyed(),true);assert.equal(closed,false);assert.equal(flushed,false);
  assert.equal(f.host.cacheActivity().busy.has(id),true);
  cleanup.resolve();await closing;assert.equal(flushed,true);assert.equal(f.host.profiles.size,0);
});

test('one client close failure still destroys the page and attempts its persistent storage flush',async()=>{
  const f=fixture();let flushed=0,stored=0;
  f.session.cookies.flushStore=async()=>{flushed++};f.session.flushStorageData=()=>{stored++};
  f.p.clients.add({close(){throw new Error('close failed')},dispose:async()=>{}});
  await assert.rejects(f.host.closeProfile(f.p),/close failed/);
  assert.equal(f.wc.isDestroyed(),true);assert.equal(flushed,1);assert.equal(stored,1);
  assert.equal(f.host.cacheActivity().busy.has(id),true,'failed cleanup must retain ownership for retry');
});

test('closing during cookie import joins the first write and prevents every following write',async()=>{
  const f=fixture(),first=deferred(),events=[];f.p.manualOnly=true;
  f.session.cookies.set=async cookie=>{events.push('set:'+cookie.name);await first.promise;events.push('written:'+cookie.name)};
  f.session.cookies.flushStore=async()=>{events.push('flush')};
  f.host.whatsappPages.set(f.p.pages.get(f.p.selected).view,{open:async()=>({page_loaded:true})});
  const opening=f.host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/',cookies:[{name:'first',value:'x',domain:'.whatsapp.com'},{name:'second',value:'y',domain:'.whatsapp.com'}]}).catch(error=>error);
  await flush();assert.deepEqual(events,['set:first']);
  let closed=false;const closing=f.host.closeProfile(f.p).then(()=>{closed=true});await flush();
  assert.equal(closed,false);assert.deepEqual(events,['set:first']);assert.equal(f.host.cacheActivity().busy.has(id),true);
  first.resolve();const error=await opening;await closing;
  assert.match(error.message,/关闭|取消/);assert.deepEqual(events,['set:first','written:first','flush']);
  assert.equal(f.host.profiles.size,0);
});

test('ordinary WhatsApp open returns loading while its navigation is still pending',async()=>{
  const f=fixture(),navigation=deferred();f.p.manualOnly=true;
  f.host.whatsappPages.set(f.p.pages.get(f.p.selected).view,{open:()=>navigation.promise});
  try{
    const result=await f.host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'});
    assert.equal(result.navigation_pending,true);
    assert.equal(result.page_loaded,null);
    assert.equal(f.wc.isDestroyed(),false);
  }finally{navigation.resolve({page_loaded:true});await flush()}
});

test('a late WhatsApp navigation rejection is handled after fast Open returns',async()=>{
  const f=fixture(),navigation=deferred();f.p.manualOnly=true;
  f.host.whatsappPages.set(f.p.pages.get(f.p.selected).view,{open:()=>navigation.promise});
  const result=await f.host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'});
  assert.equal(result.page_loaded,null);assert.equal(result.navigation_pending,true);
  navigation.reject(new Error('late network failure'));
  // The Node runner reports an unhandled rejection if the background operation
  // loses its catch handler after the shell response has already returned.
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.host.profiles.get(id),f.p);assert.equal(f.wc.isDestroyed(),false);
});

test('WhatsApp cookie import still waits for its navigation acknowledgement',async()=>{
  const f=fixture(),navigation=deferred(),actions=[];f.p.manualOnly=true;
  f.session.cookies.set=async()=>{};
  f.host.whatsappPages.set(f.p.pages.get(f.p.selected).view,{open:(url,action)=>{actions.push(action);return navigation.promise}});
  let finished=false;
  const opening=f.host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/',cookies:[{name:'fixture',value:'kept',domain:'.whatsapp.com'}]}).then(result=>{finished=true;return result});
  await flush();assert.deepEqual(actions,['refresh']);assert.equal(finished,false);
  navigation.resolve({page_loaded:true,http_status:200});
  assert.equal((await opening).page_loaded,true);assert.equal(finished,true);
});

test('fast WhatsApp Open still rejects task-owned and foreign windows',async()=>{
  const f=fixture();f.p.manualOnly=true;let opened=0;
  f.host.whatsappPages.set(f.p.pages.get(f.p.selected).view,{open:async()=>{opened++;return {page_loaded:true}}});
  await assert.rejects(f.host.control('open-whatsapp',{profile:id,owner:'22222222-2222-4222-8222-222222222222',url:'https://web.whatsapp.com/'}),/任务占用|不属于/);
  f.p.clients.add({});
  await assert.rejects(f.host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'}),/任务占用|不属于/);
  assert.equal(opened,0);assert.equal(f.wc.isDestroyed(),false);
});

test('a native close failure cancels a stalled open, reports failure, and retains the view for retry',async()=>{
  const f=fixture(),command=deferred(),win={isDestroyed:()=>false};
  f.host.profiles.clear();f.host.window=()=>win;
  for(const method of ['attachWindow','layoutPane','raiseShell','render','hide'])f.host[method]=()=>{};
  f.host.sizePageViewport=async()=>{};f.host.panes.set(win,{addChildView(){},removeChildView(){}});
  f.wc.debugger={attach(){},sendCommand:()=>command.promise,on(){}};f.host.makeView=()=>({webContents:f.wc});
  const nativeClose=f.wc.close.bind(f.wc);f.wc.close=()=>{throw new Error('native close failed')};
  const opening=f.host.ensure(id,owner,'',true).catch(error=>error);await flush();
  const profile=f.host.profiles.get(id);
  try{
    await assert.rejects(f.host.stop(),/native close failed/);await opening;
    assert.equal(f.host.pendingPages.get(profile).size,1);assert.equal(f.host.cacheActivity().busy.has(id),true);
    f.wc.close=nativeClose;await f.host.closeProfile(profile);
    assert.equal(f.host.pendingPages.size,0);assert.equal(f.host.profiles.size,0);
  }finally{f.wc.close=nativeClose;command.resolve({targetInfo:{targetId:'late'}})}
});
