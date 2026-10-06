import test from 'node:test';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
import {readFileSync} from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import {join} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {immersiveBootstrap} from '../../dist-electron/immersive-bootstrap.js';
import {ImmersiveWorkerPool,ImmersiveWorkerInvalidatedError} from '../../dist-electron/immersive-pool.js';
import {ImmersiveStartupError,startupStep,startupReason} from '../../dist-electron/immersive-startup.js';
import {OptionalImmersiveSource} from '../../dist-electron/immersive-source.js';
import * as policy from '../../dist-electron/immersive-policy.js';

// Exercise the compiled application controller with Electron IPC/window doubles.
// No browser process, vendor script, external request or real user data is used.
const controllerUrl=new URL('../../dist-electron/immersive-translator.js',import.meta.url);
const compiled=readFileSync(controllerUrl,'utf8').replace(/^import .*;\r?\n/gm,'')
 .replaceAll('import.meta.url',JSON.stringify(controllerUrl.href)).replace('export class ImmersiveTranslator','class ImmersiveTranslator');
function fixture(){
 const handlers=new Map(),secrets=new Map();let owner='owner',failSave=false;
 const scope={...policy,OptionalImmersiveSource,ImmersiveWorkerPool,ImmersiveWorkerInvalidatedError,ImmersiveStartupError,startupStep,startupReason,
  immersiveBootstrap,readFileSync,createHash,randomUUID,join,pathToFileURL,fileURLToPath,
  setInterval,clearInterval,setTimeout,clearTimeout,URL,Headers,AbortController,Buffer,console,Error,TypeError,SyntaxError,
  ipcMain:{handle:(name,fn)=>handlers.set(name,fn),removeHandler:name=>handlers.delete(name)},
  BrowserWindow:class {constructor(){throw new Error('A native window must never be created by this unit test')}},session:{}};
 const Controller=runInNewContext(compiled+'\nImmersiveTranslator',scope);
 const service=new Controller(()=>owner,()=>null,k=>secrets.get(k)||null,(k,v)=>{if(failSave)throw new Error('Encrypted store unavailable');secrets.set(k,v)},{load:()=>'',status:()=>({available:true,version:'synthetic',message:''})});
 let serial=0;
 function context(id='account',settings=true,live=()=>owner==='owner'){
  const key=policy.immersivePartition(owner,id),frame={},contents={id:++serial,mainFrame:frame,getURL:()=>service.fileUrl};let destroyed=false;
  const c={id,key,settings,requests:new Map(),live,phase:'running',
   win:{webContents:contents,isDestroyed:()=>destroyed,destroy(){destroyed=true;service.windows.delete(contents.id)},show(){},focus(){}}};
  service.windows.set(contents.id,c);return {c,event:{sender:contents,senderFrame:frame}};
 }
 return {service,handlers,secrets,context,setOwner:value=>{owner=value},failSave:()=>{failSave=true},close:()=>service.stop()};
}
const turn=()=>new Promise(resolve=>setImmediate(resolve));

test('rapid settings clicks wait for the same initialized window instead of opening duplicates',async()=>{
 const f=fixture();let release,created=0,opened=0;
 try{
  f.service.create=async()=>{created++;const {c}=f.context();await new Promise(resolve=>release=resolve);return c};
  f.service.openPanel=async()=>{opened++};
  const a=f.service.settings('account'),b=f.service.settings('account');await turn();
  assert.equal(created,1);assert.equal(opened,0);release();await Promise.all([a,b]);assert.equal(created,1);assert.equal(opened,2);
 }finally{release?.();f.close()}
});
test('logout cancels queued settings actions, including login again as the same owner',async()=>{
 const f=fixture();let release,created=0;
 try{
  f.service.create=async()=>{created++;const {c}=f.context();await new Promise(resolve=>release=resolve);return c};
  f.service.openPanel=async()=>assert.fail('old session may not open a panel');
  const a=f.service.settings('account'),b=f.service.settings('account');const results=Promise.allSettled([a,b]);await turn();
  f.setOwner(null);f.service.reset();f.setOwner('owner');release();
  assert.ok((await results).every(r=>r.status==='rejected'));assert.equal(created,1);assert.equal(f.service.windows.size,0);
 }finally{release?.();f.close()}
});
test('encrypted storage failure keeps the old settings/revision and does not revoke workers',()=>{
 const f=fixture();try{
  const {event,c}=f.context();const store=f.handlers.get('immersive:storage');let invalidations=0;
  f.service.pool.invalidate=()=>{invalidations++};
  store(event,{op:'set',key:'fullLocalUserConfig',value:{translationService:'bing'}});
  const before=f.secrets.get(c.key),identity=f.service.cacheIdentity('account');assert.equal(invalidations,1);
  f.failSave();assert.throws(()=>store(event,{op:'set',key:'fullLocalUserConfig',value:{translationService:'google'}}));
  assert.equal(f.secrets.get(c.key),before);assert.equal(f.service.cacheIdentity('account'),identity);
  assert.equal(store(event,{op:'get',key:'fullLocalUserConfig'}).translationService,'bing');assert.equal(invalidations,1);
 }finally{f.close()}
});
test('translator storage rejects child frames, unknown senders, wrong origins and reserved keys',()=>{
 const f=fixture();try{
  const {event}=f.context(),store=f.handlers.get('immersive:storage'),input={op:'get',key:'fullLocalUserConfig'};
  assert.throws(()=>store({...event,senderFrame:{}},input));
  assert.throws(()=>store({...event,sender:{...event.sender,id:999}},input));
  for(const key of ['__proto__','constructor','prototype','__juxinConfigRevision'])assert.throws(()=>store(event,{op:'set',key,value:'bad'}));
  event.sender.getURL=()=> 'https://instagram.com/';assert.throws(()=>store(event,input));
  assert.equal(f.secrets.size,0);
 }finally{f.close()}
});
test('a startup failure is surfaced without cooling down an unused translation provider',async()=>{
 const f=fixture();try{
  const failure=new ImmersiveStartupError('preload','bridge-missing');f.service.acquireWorker=async()=>{throw failure};
  const settings={...policy.immersiveDefaults,engine:'immersive'};
  await assert.rejects(f.service.translate('account','fixture text','zh-CN',settings,()=>true),e=>e===failure&&e.retryAfterMs===3000);
  const router=f.service.routers.get(policy.immersivePartition('owner','account'));assert.deepEqual(router.choose(settings),['plugin']);
 }finally{f.close()}
});
test('a real setting change during translation cancels the stale worker without cooling down the provider',async()=>{
 const f=fixture();try{
  const {event}=f.context(),store=f.handlers.get('immersive:storage');
  f.service.create=async(id,settings,live)=>f.context(id,settings,live).c;
  f.service.command=async(c,input)=>{
   if(input.kind==='status')return {service:'bing'};
   assert.equal(input.kind,'translate');
   store(event,{op:'set',key:'fullLocalUserConfig',value:{translationServices:{bing:{apiKey:'fixture-new-key'}}}});
   assert.equal(c.live(),false);throw new Error('Old renderer was closed');
  };
  const settings={...policy.immersiveDefaults,engine:'immersive',immersiveService:'bing'};
  await assert.rejects(f.service.translate('account','fixture text','zh-CN',settings,()=>true),e=>e instanceof ImmersiveWorkerInvalidatedError&&e.retryAfterMs===3000);
  assert.deepEqual(f.service.routers.get(policy.immersivePartition('owner','account')).choose(settings),['bing']);
  assert.equal(f.service.pool.stats().total,0);
 }finally{f.close()}
});

test('real settings IPC invalidates a cold worker once while vendor probe writes leave its replacement alive',async()=>{
 const f=fixture();try{
  const {event}=f.context(),store=f.handlers.get('immersive:storage');let starts=0;
  f.service.create=async(id,settings,live)=>{
   const {c}=f.context(id,settings,live);starts++;
   if(starts===1)store(event,{op:'set',key:'fullLocalUserConfig',value:{translationService:'bing'}});
   // The pinned vendor saves probe timestamps and host caches after asynchronous
   // requests, even when the native release fixture intentionally blocks HTTP.
   for(let checkedAt=1;checkedAt<=3;checkedAt++)store(event,{op:'set',key:'localConfig',value:{
    managedHostProbes:{s:{hostName:'immersivetranslate.com',checkedAt,hostNamesSignature:'fixture'}},
    managedHostAccelerationProbe:{hostName:'imtintl.com',checkedAt,hostNamesSignature:'fixture'},
    confirmSupportMouse:true,accountLastSyncedAt:checkedAt,
   }});
   await turn();return c;
  };
  const lease=await f.service.acquireWorker('account','zh-CN','plugin',()=>true);
  assert.equal(starts,2);assert.equal(lease.value.live(),true);assert.equal(lease.value.win.isDestroyed(),false);
  const identity=f.service.cacheIdentity('account'),id=lease.value.win.webContents.id;lease.release(true);
  store(event,{op:'delete',key:'localConfig'});
  assert.equal(f.service.cacheIdentity('account'),identity,'discarding only probe metadata must not clear translation caches');
  const warm=await f.service.acquireWorker('account','zh-CN','plugin',()=>true);
  assert.equal(warm.value.win.webContents.id,id);assert.equal(starts,2);warm.release(false);
 }finally{f.close()}
});
test('equivalent configuration saves and vendor UI state preserve a warmed worker',async()=>{
 const f=fixture();try{
  const {event}=f.context(),store=f.handlers.get('immersive:storage');let starts=0;
  const services={google:{apiKey:'fixture-key',apiUrl:'https://translation.googleapis.com'}};
  store(event,{op:'set',key:'fullLocalUserConfig',value:{translationService:'google',translationServices:services}});
  f.service.create=async(id,settings,live)=>{starts++;return f.context(id,settings,live).c};
  const first=await f.service.acquireWorker('account','zh-CN','plugin',()=>true),identity=f.service.cacheIdentity('account');first.release(true);
  // JSON key order and enforced chat-only defaults have no effect on workers.
  store(event,{op:'set',key:'fullLocalUserConfig',value:{enableInputTranslation:true,translationServices:{google:{apiUrl:services.google.apiUrl,apiKey:'fixture-key'}},translationService:'google'}});
  store(event,{op:'set',key:'localConfig',value:{tempTranslationUrlMatches:[{match:'example.com',expiredAt:42}],downloadSubtitle:{subtitleItems:[]},showMangaGuide:true,floatBallConfig:{top:10},subtitlePositions:{fixture:{topPercent:10}}}});
  assert.equal(f.service.cacheIdentity('account'),identity);
  const second=await f.service.acquireWorker('account','zh-CN','plugin',()=>true);
  assert.equal(second.value,first.value);assert.equal(starts,1);second.release(false);
  assert.equal(store(event,{op:'get',key:'fullLocalUserConfig'}).enableInputTranslation,false);
  assert.equal(store(event,{op:'get',key:'localConfig'}).floatBallConfig.top,10,'ignored metadata must still persist');
 }finally{f.close()}
});
test('malformed saved configuration is read with safe chat defaults and never enables input translation',()=>{
 const f=fixture();try{
  const {event,c}=f.context(),store=f.handlers.get('immersive:storage');
  for(const value of [null,[],false,'damaged-config']){
   f.service.stores.clear();f.secrets.set(c.key,JSON.stringify({fullLocalUserConfig:value}));
   const config=store(event,{op:'get',key:'fullLocalUserConfig'});
   assert.equal(config.enableInputTranslation,false);assert.equal(config.pcFloatBall.enable,false);
   store(event,{op:'set',key:'fullLocalUserConfig',value:{translationService:'bing'}});
   assert.equal(store(event,{op:'get',key:'fullLocalUserConfig'}).translationService,'bing');
  }
 }finally{f.close()}
});
test('service, credentials, language and unknown local settings still revoke only the affected account',async()=>{
 const f=fixture();try{
  const {event}=f.context(),store=f.handlers.get('immersive:storage');let starts=0;
  f.service.create=async(id,settings,live)=>{starts++;return f.context(id,settings,live).c};
  const other=await f.service.acquireWorker('other','zh-CN','plugin',()=>true);other.release(true);
  for(const [key,value] of [
   ['fullLocalUserConfig',{translationService:'google'}],
   ['fullLocalUserConfig',{translationService:'google',translationServices:{google:{apiKey:'fixture-key'}}}],
   ['fullLocalUserConfig',{translationService:'google',translationServices:{google:{apiKey:'fixture-new-key'}},targetLanguage:'en'}],
   ['localConfig',{translationServices:{custom:{baseUrl:'https://api.example.com',apiKey:'fixture-custom'}}}],
   ['localConfig',{futureTranslationSetting:{enabled:true},managedHostProbes:{s:{checkedAt:2}}}],
  ]){
   const worker=await f.service.acquireWorker('account','zh-CN','plugin',()=>true),before=f.service.cacheIdentity('account');worker.release(true);
   store(event,{op:'set',key,value});assert.notEqual(f.service.cacheIdentity('account'),before);assert.equal(worker.value.win.isDestroyed(),true);
   assert.equal(other.value.win.isDestroyed(),false);
  }
  const before=f.service.cacheIdentity('account');store(event,{op:'delete',key:'localConfig'});
  assert.notEqual(f.service.cacheIdentity('account'),before,'deleting an actual setting must invalidate');
  const after=await f.service.acquireWorker('other','zh-CN','plugin',()=>true);assert.equal(after.value,other.value);after.release(false);
  assert.equal(starts,6);
 }finally{f.close()}
});
