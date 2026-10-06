import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';

// Production runtime, controlled transport and clock. This is not a live
// WhatsApp, native Chromium, or Windows compatibility test.
const source=readFileSync(new URL('../src/whatsapp-page.ts',import.meta.url),'utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/\bexport (?=(?:async )?(?:function|class))/g,'');
const compiled=nodeModule.stripTypeScriptTypes
  ?nodeModule.stripTypeScriptTypes(source,{mode:'transform'})
  :nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:1}}).outputText;
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const url='https://web.whatsapp.com/';

function fixture(){
  let destroyed=false,sequence=0;
  const timers=new Map(),reads=[],loads=[],workers=new EventEmitter(),wc=new EventEmitter();
  Object.assign(wc,{
    session:{serviceWorkers:workers},debugger:{isAttached:()=>false},
    isDestroyed:()=>destroyed,getURL:()=>url,
    executeJavaScriptInIsolatedWorld(world){
      if(world===1005)return Promise.resolve();
      reads.push(world);return Promise.resolve({phase:'loading',chatReady:false});
    },
    loadURL(value){loads.push(value);wc.emit('did-start-navigation',{},value,false,true);return new Promise(()=>{})},
    reload(){loads.push('reload');wc.emit('did-start-navigation',{},url,false,true)},
    navigationHistory:{canGoBack:()=>false,canGoForward:()=>false},
  });
  const {WhatsAppPageRuntime}=runInNewContext(compiled+';({WhatsAppPageRuntime})',{
    setTimeout(fn,delay){const id=++sequence;timers.set(id,{fn,delay});return id},
    clearTimeout(id){timers.delete(id)},
    whatsappErrorText:value=>String(value??''),whatsappColumnLayoutScript:()=>'',
  });
  const runtime=new WhatsAppPageRuntime(wc);
  return {runtime,wc,reads,loads,timers,
    fail(){wc.emit('did-fail-load',{},-105,'ERR_NAME_NOT_RESOLVED',url,true)},
    commit(status=200){wc.emit('did-navigate',{},url,status)},
    close(){destroyed=true;wc.emit('destroyed')},
  };
}

test('later status polling cannot erase a main-document network failure',async()=>{
  const f=fixture();try{
    f.fail();
    for(let i=0;i<3;i++)assert.equal((await f.runtime.inspect(true)).phase,'network-error');
    assert.equal(f.reads.length,0,'an error document must not be reclassified as the login page');
    assert.equal(f.loads.length,0,'polling must not automatically reload a draft or reset login');
  }finally{f.close()}
});

test('same-URL explicit reopen still navigates after an error report has polled the failed page',async()=>{
  const f=fixture();try{
    f.fail();await f.runtime.report();
    const opening=f.runtime.open(url);await flush();
    assert.equal(f.loads.length,1,'error inspection must not turn an explicit reopen into a no-op');
    f.commit();assert.equal((await opening).page_loaded,true);
    assert.equal((await f.runtime.inspect()).phase,'loading');assert.equal(f.reads.length,1);
    assert.equal(f.timers.size,0);
  }finally{f.close()}
});

test('HTTP error remains failed after commit and can be explicitly retried',async()=>{
  const f=fixture();try{
    const opening=f.runtime.open(url,'refresh');f.commit(503);
    assert.equal((await opening).page_loaded,false);
    assert.equal((await f.runtime.inspect(true)).phase,'network-error');
    const retry=f.runtime.open(url);assert.equal(f.loads.length,2);
    f.commit(200);assert.equal((await retry).page_loaded,true);
    assert.equal((await f.runtime.inspect()).phase,'loading');
  }finally{f.close()}
});

test('HTTP failure after the bounded shell wait is still reflected by runtime state',async()=>{
  const f=fixture();try{
    const opening=f.runtime.open(url,'refresh');
    const pending=[...f.timers.values()].find(timer=>timer.delay===15000);pending.fn();
    assert.equal((await opening).loading,true);f.commit(429);
    assert.equal((await f.runtime.inspect()).phase,'network-error');
    assert.equal(f.loads.length,1,'no automatic rate-limit retry');
  }finally{f.close()}
});

test('subframe failure does not prevent inspection of the current main document',async()=>{
  const f=fixture();try{
    f.wc.emit('did-fail-load',{},-105,'ERR_NAME_NOT_RESOLVED','https://cdn.example.invalid/',false);
    assert.equal((await f.runtime.inspect()).phase,'loading');assert.equal(f.reads.length,1);
  }finally{f.close()}
});
