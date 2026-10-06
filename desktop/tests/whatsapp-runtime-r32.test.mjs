import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';

// Run the actual runtime source with an Electron transport double. No live
// WhatsApp login, native Chromium, or Windows behavior is claimed by this suite.
const source=readFileSync(new URL('../src/whatsapp-page.ts',import.meta.url),'utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/\bexport (?=(?:async )?(?:function|class))/g,'');
const compiled=nodeModule.stripTypeScriptTypes
  ?nodeModule.stripTypeScriptTypes(source,{mode:'transform'})
  :nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:1}}).outputText;
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};

function fixture(){
  let now=0,sequence=0,destroyed=false,url='https://web.whatsapp.com/';
  const timers=new Map(),reads=[],loads=[],layouts=[],workers=new EventEmitter(),wc=new EventEmitter();
  const clock={
    setTimeout(fn,delay){const id=++sequence;timers.set(id,{at:now+delay,fn});return id},
    clearTimeout(id){timers.delete(id)},
    async advance(ms){now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}await flush()},
  };
  Object.assign(wc,{
    session:{serviceWorkers:workers},debugger:{isAttached:()=>false},
    isDestroyed:()=>destroyed,getURL:()=>url,
    executeJavaScriptInIsolatedWorld(world){if(world===1005){layouts.push(world);return Promise.resolve()};const read=deferred();reads.push(read);return read.promise},
    loadURL(value){loads.push(value);url=value;wc.emit('did-start-navigation',{},value,false,true);return new Promise(()=>{})},
    reload(){loads.push('reload');wc.emit('did-start-navigation',{},url,false,true)},
    navigationHistory:{canGoBack:()=>false,canGoForward:()=>false},
  });
  const {WhatsAppPageRuntime}=runInNewContext(compiled+';({WhatsAppPageRuntime})',{
    setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,
    whatsappErrorText:value=>String(value??''),whatsappColumnLayoutScript:()=>'',
  });
  const runtime=new WhatsAppPageRuntime(wc);
  return {runtime,wc,workers,reads,loads,layouts,timers,clock,
    navigate(){wc.emit('did-start-navigation',{},url,false,true)},
    crash(){wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:1})},
    close(){destroyed=true;wc.emit('destroyed')},
    async release(){for(const read of reads)read.resolve({phase:'loading',chatReady:false});await flush();await clock.advance(20000)},
  };
}

test('same-document polling shares one inspection and each caller keeps a bounded wait',async()=>{
  const f=fixture();try{
    const first=f.runtime.inspect(),second=f.runtime.inspect(true);
    assert.equal(f.reads.length,1);await f.clock.advance(1800);
    assert.equal((await first).inspectionPending,true);assert.equal((await second).inspectionPending,true);
    const third=f.runtime.inspect();assert.equal(f.reads.length,1);await f.clock.advance(1800);await third;
  }finally{await f.release();f.close()}
});

test('refresh can inspect its new document while the old renderer read never answers',async()=>{
  const f=fixture();try{
    const old=f.runtime.inspect();f.navigate();const current=f.runtime.inspect();
    assert.equal(f.reads.length,2,'new document must not join the abandoned renderer read');
    f.reads[1].resolve({phase:'chat-ready',chatReady:true});assert.equal((await current).chatReady,true);
    f.reads[0].resolve({phase:'storage-error',chatReady:false});await old;
    assert.equal(f.runtime.state.phase,'chat-ready');assert.equal(f.layouts.length,1);
  }finally{await f.release();f.close()}
});

test('old inspection finalizer cannot clear the pending new-document inspection',async()=>{
  const f=fixture();try{
    const old=f.runtime.inspect();f.navigate();const current=f.runtime.inspect();
    assert.equal(f.reads.length,2);f.reads[0].resolve({phase:'loading',chatReady:false});await old;
    const joined=f.runtime.inspect(true);assert.equal(f.reads.length,2,'a late old result must not spawn a duplicate read');
    f.reads[1].resolve({phase:'phone-login',chatReady:false});
    assert.equal((await current).phase,'phone-login');assert.equal((await joined).phase,'phone-login');
  }finally{await f.release();f.close()}
});

for(const [event,phase] of [['crash','renderer-error'],['network','network-error'],['close','closed']]){
  test(`${event} remains authoritative over a late chat-ready inspection`,async()=>{
    const f=fixture();try{
      const pending=f.runtime.inspect();
      if(event==='crash')f.crash();else if(event==='close')f.close();
      else f.wc.emit('did-fail-load',{},-105,'ERR_NAME_NOT_RESOLVED','https://web.whatsapp.com/',true);
      f.reads[0].resolve({phase:'chat-ready',chatReady:true});
      assert.equal((await pending).phase,phase);assert.equal(f.runtime.state.chatReady,false);assert.equal(f.layouts.length,0);
      if(event==='crash'){assert.equal((await f.runtime.inspect(true)).phase,'renderer-error');assert.equal(f.reads.length,1)}
    }finally{await f.release();f.close()}
  });
}

test('renderer crash ends the navigation wait and explicit reopen retries the same URL',async()=>{
  const f=fixture();try{
    let result;const pending=f.runtime.open('https://web.whatsapp.com/','refresh').then(value=>{result=value;return value});
    f.crash();await flush();assert.equal(result?.page_loaded,false,'crash must not remain a successful loading wait');
    await pending;assert.equal(f.timers.size,0);assert.equal(f.wc.listenerCount('render-process-gone'),1);
    const retry=f.runtime.open('https://web.whatsapp.com/');assert.equal(f.loads.length,2,'a crashed same-URL page requires explicit reload');
    f.wc.emit('did-navigate',{},'https://web.whatsapp.com/',200);assert.equal((await retry).page_loaded,true);
  }finally{await f.release();f.close()}
});

test('synchronous navigation failure removes temporary listeners and timer',async()=>{
  const f=fixture();try{
    f.wc.reload=()=>{throw new Error('renderer unavailable')};
    const result=await f.runtime.open('https://web.whatsapp.com/','refresh').catch(error=>({rejected:error}));
    assert.equal(f.timers.size,0,'a throwing launch must not retain its 15-second navigation waiter');
    assert.equal(f.wc.listenerCount('did-navigate'),1);assert.equal(f.wc.listenerCount('did-fail-load'),1);
    assert.equal(result.page_loaded,false);assert.equal(f.runtime.state.phase,'network-error');
    const retry=f.runtime.open('https://web.whatsapp.com/');assert.equal(f.loads.length,1);
    f.wc.emit('did-navigate',{},'https://web.whatsapp.com/',200);assert.equal((await retry).page_loaded,true);
  }finally{await f.release();f.close()}
});

test('open callers share one navigation and commit returns without stopping slow resources',async()=>{
  const f=fixture();try{
    const first=f.runtime.open('https://web.whatsapp.com/','refresh'),second=f.runtime.open('https://web.whatsapp.com/','refresh');
    assert.equal(first,second);assert.equal(f.loads.length,1);
    f.wc.emit('did-navigate',{},'https://web.whatsapp.com/',200);
    assert.equal((await first).page_loaded,true);assert.equal(f.timers.size,0);
    assert.equal(f.wc.listenerCount('did-navigate'),1);assert.equal(f.wc.listenerCount('destroyed'),1);
  }finally{await f.release();f.close()}
});

test('subframe navigation does not discard the current main-document inspection',async()=>{
  const f=fixture();try{
    const first=f.runtime.inspect();f.wc.emit('did-start-navigation',{},'https://example.invalid/',false,false);
    const second=f.runtime.inspect(true);assert.equal(f.reads.length,1);
    f.reads[0].resolve({phase:'phone-login',chatReady:false});await first;await second;
  }finally{await f.release();f.close()}
});

test('destroying a runtime settles open and unregisters the shared service-worker listener',async()=>{
  const f=fixture();const pending=f.runtime.open('https://web.whatsapp.com/','refresh');
  assert.equal(f.workers.listenerCount('console-message'),1);f.close();
  assert.equal((await pending).page_loaded,false);assert.equal(f.workers.listenerCount('console-message'),0);assert.equal(f.timers.size,0);
});

test('explicit same-URL crash recovery can inspect without waiting for the crashed renderer read',async()=>{
  const f=fixture();try{
    const previous=f.runtime.inspect();f.crash();
    const opening=f.runtime.open('https://web.whatsapp.com/');assert.equal(f.loads.length,1);
    f.wc.emit('did-navigate',{},'https://web.whatsapp.com/',200);await opening;
    const current=f.runtime.inspect();assert.equal(f.reads.length,2);
    f.reads[1].resolve({phase:'qr-candidate',chatReady:false});assert.equal((await current).phase,'qr-candidate');
    f.reads[0].reject(new Error('old renderer terminated'));await previous;
    assert.equal(f.runtime.state.phase,'qr-candidate');assert.equal(f.loads.length,1,'polling never automatically reloads or resets login');
  }finally{await f.release();f.close()}
});

test('a slow navigation returns pending at the deadline without stopping or repeating the load',async()=>{
  const f=fixture();try{
    const pending=f.runtime.open('https://web.whatsapp.com/','refresh');await f.clock.advance(15000);
    const result=await pending;assert.equal(result.loading,true);assert.equal(f.loads.length,1);assert.equal(f.timers.size,0);
    assert.equal(f.wc.listenerCount('render-process-gone'),1);assert.equal(f.wc.listenerCount('did-navigate'),1);
    f.wc.emit('did-navigate',{},'https://web.whatsapp.com/',200);
    await f.runtime.open('https://web.whatsapp.com/');assert.equal(f.loads.length,1,'reopening a healthy current URL must preserve its page');
  }finally{await f.release();f.close()}
});
