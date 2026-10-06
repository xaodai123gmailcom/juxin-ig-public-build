import test from 'node:test';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
import {immersiveBootstrap} from '../../dist-electron/immersive-bootstrap.js';
import {ImmersiveWorkerPool,ImmersiveWorkerInvalidatedError} from '../../dist-electron/immersive-pool.js';
import {ImmersiveStartupError,startupStep,startupReason} from '../../dist-electron/immersive-startup.js';
import {immersiveHostConfig} from '../../dist-electron/immersive-policy.js';

// Only our adapter is run against small event/DOM doubles; this is not a vendor browser test.
function host(){
 class EventWithDetail extends Event {constructor(type,options){super(type);this.detail=options?.detail}}
 const nodes=new Map(),document=new EventTarget(),timers=new Set(),events=[];
 document.querySelector=selector=>nodes.get(selector)||null;
 document.createElement=tag=>({tag,textContent:'',attrs:{},children:[],setAttribute(k,v){this.attrs[k]=v},append(n){this.children.push(n)}});
 document.head=document.createElement('head');document.documentElement=document.createElement('html');
 const runtime={document,Event,CustomEvent:EventWithDetail,TextEncoder,Blob,ReadableStream,Date,Map,Set,Promise,
  setInterval:()=>0,clearInterval(){},setTimeout:(fn,ms)=>{const id=setTimeout(fn,ms);timers.add(id);return id},clearTimeout:id=>{clearTimeout(id);timers.delete(id)},getComputedStyle:()=>({visibility:'visible'}),
  __juxinImmersiveBridge:{storage:async()=>undefined,open:async()=>{},request:async()=>{},abort:async()=>{}}};
 document.addEventListener('immersiveTranslateDocumentMessageThirdPartyTell',event=>{
  const message=JSON.parse(event.detail);events.push(message);
  if(message.type==='getPageStatusAsync')document.dispatchEvent(new EventWithDetail('immersiveTranslateDocumentMessageTellThirdParty',{detail:JSON.stringify({...message,payload:'Original'})}));
 });
 runInNewContext('('+immersiveBootstrap.toString()+')()',runtime);
 return {runtime,nodes,events,close:()=>{for(const id of timers)clearTimeout(id)}};
}
test('GM.addElement writes style text as a DOM property, including the vendor ShadowRoot form',()=>{
 const h=host();try{const target={children:[],append(n){this.children.push(n)}};const node=h.runtime.GM.addElement(target,'style',{textContent:'.popup-container{display:block}',id:'vendor-style'});assert.equal(node.textContent,'.popup-container{display:block}');assert.equal(node.attrs.textContent,undefined);assert.equal(node.attrs.id,'vendor-style');assert.equal(target.children[0],node)}finally{h.close()}
});
test('popup opening reports no success until the native panel and controls are visible',()=>{
 const h=host();try{
  assert.equal(h.runtime.__juxinImmersiveCommand({kind:'popup'}).opened,false);assert.equal(h.events.length,0);
  let visible=false;const content={getBoundingClientRect:()=>({width:visible?316:0,height:visible?500:0}),querySelectorAll:()=>[{},{}]};
  h.nodes.set('#immersive-translate-browser-popup',{shadowRoot:{querySelector:selector=>selector==='#mount'?{}:content}});
  assert.equal(h.runtime.__juxinImmersiveCommand({kind:'popup'}).opened,false);assert.equal(h.events.at(-1).type,'openPopup');
  visible=true;const panel=h.runtime.__juxinImmersiveCommand({kind:'panel'});assert.equal(panel.visible,true);assert.equal(panel.controls,2);
 }finally{h.close()}
});
test('warm reuse restores the official page before clearing message text',async()=>{
 const h=host();try{let cleared=false;h.nodes.set('#juxin-messages',{replaceChildren(){cleared=true}});assert.equal((await h.runtime.__juxinImmersiveCommand({kind:'clear'})).cleared,true);assert.deepEqual(h.events.map(x=>x.type),['restorePage','getPageStatusAsync']);assert.equal(cleared,true)}finally{h.close()}
});
test('host settings select the native popup renderer without granting private SDK access',()=>{
 const c=immersiveHostConfig({monkeyH5FloatBall:{enable:true},'monkeyH5FloatBall.add':{enable:true},translationServices:{deepl:{apiKey:'fixture'}}});
 assert.equal(c.monkeyH5FloatBall.enable,false);assert.equal(c['monkeyH5FloatBall.add'].enable,false);assert.equal(c.pcFloatBall.enable,false);assert.equal(c.useOnlineOptions,true);assert.equal(c.translationServices.deepl.apiKey,'fixture');assert.equal(c.generalRule?.allowInnerInvoke,undefined);
});
test('ten consecutive messages reuse one initialized worker; different accounts never reuse it',async()=>{
 let created=0;const disposed=[];const pool=new ImmersiveWorkerPool(async key=>({key,id:++created}),v=>disposed.push(v));
 for(let i=0;i<10;i++){const lease=await pool.acquire('account-a/config-1',()=>true);assert.equal(lease.value.id,1);lease.release(true)}
 assert.equal(created,1);const other=await pool.acquire('account-b/config-1',()=>true);assert.equal(other.value.id,2);other.release(true);pool.invalidate();assert.equal(disposed.length,2);
});
test('warm workers stay bounded and an idle worker is evicted for a new account',async()=>{
 let created=0;const destroyed=[];const pool=new ImmersiveWorkerPool(async key=>({key,id:++created}),v=>destroyed.push(v.id));
 const a=await pool.acquire('a',()=>true),b=await pool.acquire('b',()=>true);await assert.rejects(pool.acquire('c',()=>true),/正在处理/);assert.equal(created,2);
 a.release(true);const c=await pool.acquire('c',()=>true);assert.deepEqual(destroyed,[a.value.id]);assert.equal(pool.stats().total,2);b.release(false);c.release(false);assert.equal(pool.stats().total,0);
});
test('task revocation, settings changes and idle expiry discard workers instead of retaining messages',async()=>{
 let now=0,live=true;const destroyed=[];const pool=new ImmersiveWorkerPool(async key=>({key}),v=>destroyed.push(v.key),()=>now);
 const a=await pool.acquire('a:old',()=>live);live=false;pool.prune();a.release(true);assert.deepEqual(destroyed,['a:old']);
 const b=await pool.acquire('a:new',()=>true);b.release(true);pool.invalidate('a:');assert.equal(pool.stats().total,0);
 const c=await pool.acquire('b',()=>true);c.release(true);now=60000;pool.prune();assert.equal(pool.stats().total,1);now=300000;pool.prune();assert.equal(pool.stats().total,0);
});
test('logout during cold startup disposes the completed worker exactly once',async()=>{
 let resolve;const destroyed=[];const pool=new ImmersiveWorkerPool(()=>new Promise(r=>resolve=r),v=>destroyed.push(v));
 let live=true;const pending=pool.acquire('a',()=>live);live=false;pool.invalidate();resolve('late-worker');await assert.rejects(pending,/暂停/);assert.deepEqual(destroyed,['late-worker']);assert.equal(pool.stats().total,0);
});

test('settings invalidating a rejected cold renderer retry once with the fresh configuration',async()=>{
 let rejectFirst,version='old';const created=[],destroyed=[];
 const pool=new ImmersiveWorkerPool(async key=>{created.push(key);if(created.length===1)return new Promise((_,reject)=>rejectFirst=reject);return {key}},value=>destroyed.push(value));
 const pending=pool.acquireCurrent(()=>version,()=>true);
 version='new';pool.invalidate();rejectFirst(new Error('Object has been destroyed'));
 const lease=await pending;assert.deepEqual(created,['old','new']);assert.equal(lease.value.key,'new');assert.equal(pool.stats().busy,1);
 lease.release(false);assert.equal(destroyed.length,1);assert.equal(pool.stats().total,0);
});
test('repeated setting changes are bounded and real startup errors are never hidden by retry',async()=>{
 let count=0;const pool=new ImmersiveWorkerPool(async()=>{count++;pool.invalidate();throw new Error('closed')},()=>{});
 await assert.rejects(pool.acquireCurrent(()=> 'cfg',()=>true),ImmersiveWorkerInvalidatedError);assert.equal(count,2);assert.equal(pool.stats().total,0);
 let failures=0;const broken=new ImmersiveWorkerPool(async()=>{failures++;throw new ImmersiveStartupError('preload','bridge-missing')},()=>{});
 await assert.rejects(broken.acquireCurrent(()=> 'cfg',()=>true),error=>error.phase==='preload');assert.equal(failures,1);
});
test('revocation during cold startup cannot recreate a worker even if initialization rejects',async()=>{
 let live=true,rejectFirst,count=0;const pool=new ImmersiveWorkerPool(async()=>{count++;return new Promise((_,reject)=>rejectFirst=reject)},()=>{});
 const pending=pool.acquireCurrent(()=> 'cfg',()=>live);live=false;pool.invalidate();rejectFirst(new Error('closed'));
 await assert.rejects(pending);assert.equal(count,1);assert.equal(pool.stats().total,0);
});
test('startup steps bound a stalled native call and diagnostics do not echo vendor secrets',async()=>{
 await assert.rejects(startupStep('ready',()=>new Promise(()=>{}),20),e=>e.phase==='ready'&&e.reason==='timeout');
 await assert.rejects(startupStep('inject',async()=>{throw new TypeError('private-key-and-message')}),e=>e.phase==='inject'&&e.reason==='TypeError'&&!e.message.includes('private-key'));
 assert.equal(startupReason({code:'https://private.example/key'}),'execution-failed');
 assert.equal(startupReason({code:'ERR_FILE_NOT_FOUND'}),'ERR_FILE_NOT_FOUND');
 assert.equal(await startupStep('load',async()=>42),42);
});

test('a slow startup metadata request cannot hold back a finished chat translation',async()=>{
 const h=host();let finishMetadata;try{
  const reply={text:'translated',status:200,url:'https://example.test/'};
  h.runtime.__juxinImmersiveBridge.request=async input=>input.url.endsWith('/metadata')?new Promise(resolve=>finishMetadata=resolve):reply;
  const background=h.runtime.GM.xmlHttpRequest({url:'https://example.test/metadata'});
  const state=h.runtime.__juxinImmersiveRuntime;assert.equal(state.pending,0);
  state.job='message-1';const translation=h.runtime.GM.xmlHttpRequest({url:'https://example.test/translate'});assert.equal(state.pending,1);await translation;assert.equal(state.pending,0);
  finishMetadata(reply);await background;assert.equal(state.pending,0,'late metadata completion cannot make the message counter negative');
 }finally{finishMetadata?.({text:'',status:200,url:'https://example.test/'});h.close()}
});
