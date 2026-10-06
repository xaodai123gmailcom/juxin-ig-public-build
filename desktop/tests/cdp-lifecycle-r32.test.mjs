import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';

// Production host + CDP connection; Electron/WebSocket transports are controlled
// doubles. No native browser, real account, or Windows cleanup is claimed here.
function compile(name){
  const source=readFileSync(new URL('../src/'+name,import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
  return (nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
}
const cdpSource=compile('embedded-cdp.ts'),hostSource=compile('embedded-browser.ts');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
const outcome=promise=>promise.then(value=>({value}),error=>({error}));
async function bounded(promise){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('fixture did not settle')),1000)})])}finally{clearTimeout(timer)}}

async function fixture(){
  let server,upgraded,sessionId=0;
  const jobs=[],calls=[];
  class Socket extends EventEmitter{
    static OPEN=1;readyState=1;sent=[];
    pause(){}resume(){}send(raw){this.sent.push(JSON.parse(raw))}
    close(){if(this.readyState===3)return;this.readyState=3;this.emit('close')}
    terminate(){this.close()}
  }
  const Cdp=runInNewContext(cdpSource+';AccountCdpConnection',{
    URL,process,WebSocket:Socket,setImmediate:fn=>jobs.push(fn),
    randomBytes:()=>({toString:()=> 'browser-session'}),permittedPageUrl:value=>/^https?:|^about:blank$/.test(value),
  });
  class Server extends EventEmitter{listen(_port,_address,done){done()}address(){return{port:12345}}close(done){done()}}
  class Sockets{clients=new Set();handleUpgrade(_req,ws,_head,fn){this.clients.add(ws);upgraded=fn(ws)}close(){}}
  const Host=runInNewContext(hostSource+';EmbeddedBrowserHost',{
    URL,process,setTimeout,clearTimeout,clearInterval,setImmediate,
    setInterval:()=>({unref(){}}),randomBytes:()=>({toString:()=> 'capability'}),
    WebSocket:Socket,WebSocketServer:Sockets,AccountCdpConnection:Cdp,
    createServer:()=>server=new Server(),AccountUnreadCache:class{},whatsappErrorText:value=>String(value),
  });
  const host=new Host(()=>null),ws=new Socket();
  const profile={id:'profile-1',owner:'owner-1',capability:'token',closed:false,clients:new Set(),pages:new Map(),
    session:{cookies:{get:async()=>[],set:async()=>{},remove:async()=>{},flushStore:async()=>{}},flushStorageData(){}}};
  function page(id){
    const wc=new EventEmitter();
    Object.assign(wc,{destroyed:false,isDestroyed:()=>wc.destroyed,getTitle:()=> 'Account',getURL:()=> 'https://www.instagram.com/',
      close(){wc.destroyed=true;wc.emit('destroyed')},
      debugger:{async sendCommand(method,params){calls.push({method,params});return method==='Target.attachToTarget'?{sessionId:'session-'+(++sessionId)}:{}}},
    });
    const page={targetId:id,view:{webContents:wc}};profile.pages.set(id,page);
    wc.on('destroyed',()=>{profile.pages.delete(id);for(const client of profile.clients)client.pageClosed(page)});
    return page;
  }
  const first=page('target-1');host.profiles.set(profile.id,profile);await host.start();
  server.emit('upgrade',{url:'/devtools/browser/token',headers:{}},ws,Buffer.alloc(0));await upgraded;
  return {host,profile,ws,client:[...profile.clients][0],page,first,jobs,calls,Cdp};
}

test('websocket close retains profile occupancy until the real debugger detach completes',async()=>{
  const f=await fixture(),gate=deferred();await f.client.attach(f.first);
  f.first.view.webContents.debugger.sendCommand=()=>gate.promise;
  f.ws.close();await flush();
  assert.equal(f.profile.clients.size,1);assert.equal(f.host.cacheActivity().busy.has(f.profile.id),true);
  const first=f.client.dispose(),second=f.client.dispose();assert.equal(first,second);
  let finished=false;void first.then(()=>{finished=true});await flush();assert.equal(finished,false);
  gate.resolve({});await bounded(first);await flush();
  assert.equal(f.profile.clients.size,0);assert.equal(f.host.cacheActivity().busy.has(f.profile.id),false);
});

test('old cookie writes stop after their current await and cleanup joins that in-flight write',async()=>{
  const f=await fixture(),gate=deferred(),writes=[];
  f.profile.session.cookies.set=async cookie=>{writes.push(cookie.name);await gate.promise};
  f.profile.session.cookies.flushStore=async()=>writes.push('flush');
  const writing=outcome(f.client.command('Storage.setCookies',{cookies:[{name:'first',value:'x',url:'https://www.instagram.com/'},{name:'late',value:'y',url:'https://www.instagram.com/'}]}));
  await flush();f.ws.close();let disposed=false;const cleanup=f.client.dispose().then(()=>{disposed=true});await flush();
  assert.equal(disposed,false);assert.equal(f.profile.clients.size,1);
  gate.resolve();assert.match((await writing).error?.message||'',/关闭/);await bounded(cleanup);await flush();
  assert.deepEqual(writes,['first']);assert.equal(f.profile.clients.size,0);
});

test('cookie removal cannot resume its second deletion after the client closes',async()=>{
  const f=await fixture(),gate=deferred(),removed=[];
  f.profile.session.cookies.get=async()=>[{name:'first',domain:'instagram.com',path:'/'},{name:'second',domain:'instagram.com',path:'/'}];
  f.profile.session.cookies.remove=async(_url,name)=>{removed.push(name);await gate.promise};
  const writing=outcome(f.client.command('Storage.clearCookies',{}));await flush();f.ws.close();
  gate.resolve();assert.match((await writing).error?.message||'',/关闭/);await bounded(f.client.dispose());assert.deepEqual(removed,['first']);
});

test('late attach is detached before disposal completes without publishing a new target',async()=>{
  const f=await fixture(),attach=deferred(),detach=deferred();
  f.first.view.webContents.debugger.sendCommand=method=>method==='Target.attachToTarget'?attach.promise:detach.promise;
  const attaching=outcome(f.client.attach(f.first));await flush();f.ws.close();
  let done=false;const cleanup=f.client.dispose().then(()=>{done=true});await flush();assert.equal(done,false);
  attach.resolve({sessionId:'late-session'});await flush();assert.equal(done,false);
  detach.resolve({});await bounded(cleanup);assert.match((await attaching).error?.message||'',/关闭/);
  assert.equal(f.client.sessions.size,0);assert.equal(f.ws.sent.length,0);
});

test('page destruction releases a hung native attach and a late reply cannot restore its session',async()=>{
  const f=await fixture(),attach=deferred();f.first.view.webContents.debugger.sendCommand=()=>attach.promise;
  const attaching=outcome(f.client.attach(f.first));await flush();f.ws.close();f.first.view.webContents.close();
  await bounded(f.client.dispose());assert.match((await attaching).error?.message||'',/关闭/);
  attach.resolve({sessionId:'dead-page-session'});await flush();assert.equal(f.client.sessions.size,0);assert.equal(f.client.roots.size,0);
});

test('pageClosed during attach cannot publish a stale root even while the socket stays open',async()=>{
  const f=await fixture(),attach=deferred();f.first.view.webContents.debugger.sendCommand=()=>attach.promise;
  const attaching=outcome(f.client.attach(f.first));await flush();f.first.view.webContents.close();
  attach.resolve({sessionId:'dead-page-session'});assert.match((await attaching).error?.message||'',/关闭/);
  assert.equal(f.client.sessions.size,0);assert.equal(f.client.roots.size,0);await f.client.dispose();
});

test('late createTarget is closed and disposal waits for its actual destroyed event',async()=>{
  const f=await fixture(),created=deferred();f.host.newPage=()=>created.promise;
  const creating=outcome(f.client.command('Target.createTarget',{url:'https://www.instagram.com/'}));await flush();f.ws.close();
  const late=f.page('late-target'),wc=late.view.webContents;let closeRequested=false;wc.close=()=>{closeRequested=true};
  let done=false;const cleanup=f.client.dispose().then(()=>{done=true});created.resolve(late);await flush();
  assert.equal(closeRequested,true);assert.equal(done,false);assert.equal(f.profile.clients.size,1);
  wc.destroyed=true;wc.emit('destroyed');await bounded(cleanup);assert.match((await creating).error?.message||'',/关闭/);
  assert.equal(f.profile.pages.has(late.targetId),false);assert.equal(f.client.createdPages.size,0);
});

test('cleanup failure retains occupancy and binding for a subsequent successful retry',async()=>{
  const f=await fixture();await f.client.attach(f.first);let attempts=0;
  f.first.view.webContents.debugger.sendCommand=async()=>{attempts++;if(attempts===1)throw new Error('detach failed');return{}};
  f.ws.close();await flush();
  assert.equal(f.profile.clients.size,1);assert.equal(f.client.sessions.size,1);
  assert.match(f.profile.diagnostics?.at(-1)?.message||'',/detach failed/);
  await bounded(f.client.dispose());assert.equal(f.client.sessions.size,0);assert.equal(attempts,2);
  await assert.rejects(f.client.command('Browser.getVersion',{}),/关闭/);
});

test('destroying the page unblocks a detach whose native promise never resolves',async()=>{
  const f=await fixture();await f.client.attach(f.first);f.first.view.webContents.debugger.sendCommand=()=>new Promise(()=>{});
  f.ws.close();await flush();assert.equal(f.profile.clients.size,1);
  f.first.view.webContents.close();await bounded(f.client.dispose());await flush();assert.equal(f.profile.clients.size,0);
});

test('a failed detach remains retryable even when an old page command is still blocked',async()=>{
  const f=await fixture();await f.client.attach(f.first);let fail=true;
  f.first.view.webContents.debugger.sendCommand=async method=>{
    if(method==='Target.detachFromTarget'){if(fail)throw new Error('detach unavailable');return{}}
    return new Promise(()=>{});
  };
  const reading=outcome(f.client.command('Runtime.evaluate',{expression:'1'},'session-1'));await flush();f.ws.close();
  await assert.rejects(bounded(f.client.dispose()),/detach unavailable/);assert.equal(f.profile.clients.size,1);
  fail=false;const retry=f.client.dispose();f.first.view.webContents.close();await bounded(retry);
  assert.match((await reading).error?.message||'',/关闭/);assert.equal(f.client.sessions.size,0);
});

test('host close destroys pages concurrently with draining blocked CDP commands',async()=>{
  const f=await fixture();await f.client.attach(f.first);f.first.view.webContents.debugger.sendCommand=()=>new Promise(()=>{});
  const reading=outcome(f.client.command('Runtime.evaluate',{expression:'1'},'session-1'));await flush();
  await bounded(f.host.closeProfile(f.profile));assert.match((await reading).error?.message||'',/关闭/);
  assert.equal(f.profile.clients.size,0);assert.equal(f.host.profiles.size,0);
});

test('Browser.close acknowledges first and reports asynchronous close failure without an unhandled rejection',async()=>{
  const f=await fixture(),error=new Error('cookie flush failed'),reported=[];
  f.host.closeProfile=async()=>{throw error};f.host.recordConnectionFailure=(profile,failure)=>reported.push({profile,failure});
  const reply=await f.client.command('Browser.close',{});assert.equal(Object.keys(reply).length,0);assert.equal(reported.length,0);
  assert.equal(f.jobs.length,1);f.jobs.shift()();await flush();
  assert.equal(reported.length,1);assert.equal(reported[0].profile,f.profile);assert.equal(reported[0].failure,error);await f.client.dispose();
});

test('duplicate attach deduplicates work and explicit detach removes its root mapping',async()=>{
  const f=await fixture(),first=f.client.attach(f.first),second=f.client.attach(f.first);assert.equal(first,second);
  const session=await first;assert.equal(f.calls.filter(row=>row.method==='Target.attachToTarget').length,1);
  await f.client.command('Target.detachFromTarget',{sessionId:session});assert.equal(f.client.roots.size,0);
  assert.notEqual(await f.client.attach(f.first),session);await f.client.dispose();
});

test('parallel debugger commands share one destruction subscription and clean it after replies',async()=>{
  const f=await fixture(),session=await f.client.attach(f.first),wc=f.first.view.webContents,gates=[];
  const baseline=wc.listenerCount('destroyed');
  wc.debugger.sendCommand=method=>{if(method!=='Runtime.evaluate')return Promise.resolve({});const gate=deferred();gates.push(gate);return gate.promise};
  const commands=Array.from({length:32},()=>outcome(f.client.command('Runtime.evaluate',{expression:'1'},session)));
  await flush();
  try{
    assert.equal(wc.listenerCount('destroyed'),baseline+1,'parallel requests need one shared close listener');
  }finally{
    gates.forEach((gate,i)=>i%2?gate.reject(new Error('fixture rejection')):gate.resolve({result:{value:i}}));
    await Promise.all(commands);await flush();
  }
  assert.equal(wc.listenerCount('destroyed'),baseline,'all per-page waiters are removed after success/rejection');
  await f.client.dispose();
});

test('shared page destruction rejects requests from both connections and ignores late native replies',async()=>{
  const f=await fixture(),second=new f.Cdp(f.host,f.profile,new f.ws.constructor());
  f.profile.clients.add(second);
  const a=await f.client.attach(f.first),b=await second.attach(f.first),wc=f.first.view.webContents,gates=[];
  const baseline=wc.listenerCount('destroyed');
  wc.debugger.sendCommand=method=>{if(method!=='Runtime.evaluate')return Promise.resolve({});const gate=deferred();gates.push(gate);return gate.promise};
  const jobs=Array.from({length:24},(_,i)=>outcome((i%2?second:f.client).command('Runtime.evaluate',{expression:'1'},i%2?b:a)));
  await flush();
  assert.equal(wc.listenerCount('destroyed'),baseline+1,'connections must share the physical page listener');
  wc.close();
  const results=await bounded(Promise.all(jobs));
  assert.ok(results.every(r=>r.error?.message.includes('关闭')));
  assert.equal(wc.listenerCount('destroyed'),baseline,'external page cleanup listener remains');
  gates.forEach((gate,i)=>i%2?gate.reject(new Error('late native failure')):gate.resolve({result:{value:99}}));
  await flush();assert.equal(f.client.sessions.size,0);assert.equal(second.sessions.size,0);
  await bounded(Promise.all([f.client.dispose(),second.dispose()]));
});
