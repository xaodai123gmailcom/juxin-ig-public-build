import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {runInNewContext} from 'node:vm';

// Node 22.13+/24 can exercise the current TypeScript source without installed
// desktop dependencies. Older supported build runners use their tsc output.
const {stripTypeScriptTypes}=await import('node:module');
let moduleRoot=new URL('../../dist-electron/',import.meta.url);
if(typeof stripTypeScriptTypes==='function'){
 const directory=mkdtempSync(join(tmpdir(),'messaging-r32-source-'));
 after(()=>rmSync(directory,{recursive:true,force:true}));
 writeFileSync(join(directory,'package.json'),JSON.stringify({type:'module'}));
 for(const name of ['chat-translation','chat-translation-page','immersive-policy','immersive-source','immersive-domains','atomic-secure-store']){
  const source=readFileSync(new URL('../src/'+name+'.ts',import.meta.url),'utf8');
  writeFileSync(join(directory,name+'.js'),stripTypeScriptTypes(source,{mode:'transform'}));
 }
 moduleRoot=pathToFileURL(directory+'/');
}
const {ChatTranslation,chatSettings}=await import(new URL('chat-translation.js',moduleRoot));
const {chatTranslationPage}=await import(new URL('chat-translation-page.js',moduleRoot));

const nextTurn=()=>new Promise(resolve=>setImmediate(resolve));

test('switching chats in one account invalidates slow jobs before they can apply or occupy the next chat',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'messaging-r32-chat-'));let conversation='first-chat';
 const jobs=[],applies=[];
 const runtime={translate:(_id,text,_lang,_settings,current)=>new Promise((resolve,reject)=>jobs.push({text,current,resolve,reject})),settings:async()=>{},cacheIdentity:()=> 'config',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'account'}),async(id,step)=>{
  if(step.kind==='check')return {profile:id};
  if(step.kind==='poll')return {conversation,messages:[{id:'1',original:conversation+' message one'},{id:'2',original:conversation+' message two'}]};
  applies.push(step);return {applied:step.conversation===conversation};
 },()=>null,()=>{},{immersive:runtime});
 const running=[];
 try{
  await service.command('account',{mode:'immersive'},'session');
  running.push(service.tick());await nextTurn();assert.equal(jobs.length,2);
  await service.tick();assert.ok(jobs.every(job=>job.current()),'polling the same chat must not revoke its requests');
  conversation='second-chat';await service.tick();
  const oldStillCurrent=jobs.filter(job=>job.current()).length;
  jobs[0].resolve('late old translation');jobs[1].reject(new Error('old chat request was cancelled'));await running[0];
  console.log(JSON.stringify({case:'same-account-chat-switch',oldStillCurrent,oldApplyAttempts:applies.filter(step=>step.conversation==='first-chat').length}));
  assert.equal(oldStillCurrent,0,'the provider cancellation predicate must revoke the old conversation');
  assert.equal(applies.length,0,'late results must not even enter Core for the wrong chat');
  const status=await service.command('account',{},'session');assert.equal(status.error,'');assert.equal(status.retryAt,0,'cancelling the departed chat must not cool down the current chat');
  running.push(service.tick());await nextTurn();assert.equal(jobs.length,4);
  assert.ok(jobs.slice(2).every(job=>job.current()));
  conversation='first-chat';await service.tick();assert.ok(jobs.slice(2).every(job=>!job.current()),'returning to an earlier chat still revokes the departed chat');
 }finally{service.stop();jobs.forEach(job=>job.resolve('cleanup'));await Promise.allSettled(running);rmSync(directory,{recursive:true,force:true})}
});

// A deterministic isolated-page DOM model exercises the production function,
// including installed observer/listener/timer resources. No network or messages
// are sent. Existing Electron integration tests cover native DOM layout.
function pageFixture(){
 let now=1000,sequence=0,createdObservers=0;const activeObservers=new Set(),listeners=new Set(),timers=new Map(),labels=[];
 const root={parentElement:null,isConnected:true,querySelector:s=>s==='header'?{textContent:'Alice'}:null,querySelectorAll:()=>[node],addEventListener:(_name,fn)=>listeners.add(fn),removeEventListener:(_name,fn)=>listeners.delete(fn),contains:()=>true};
 const box={getClientRects:()=>[{}],closest:()=>null,getBoundingClientRect:()=>({top:500,left:0,right:600}),parentElement:root};
 const node={innerText:'Hello',isConnected:true,parentElement:root,closest:()=>null,getClientRects:()=>[{}],getBoundingClientRect:()=>({top:100,bottom:120,left:0,right:100}),insertAdjacentElement:(_where,label)=>{label.isConnected=true;labels.push(label)}};
 root.closest=()=>null;
 const context={crypto:globalThis.crypto,location:{hostname:'web.whatsapp.com',pathname:'/'},innerHeight:600,
  HTMLTextAreaElement:class {},Node:{ELEMENT_NODE:1,TEXT_NODE:3},Date:class extends Date{static now(){return now}},
  getComputedStyle:()=>({overflowY:'visible'}),
  MutationObserver:class{constructor(callback){this.callback=callback;createdObservers++}observe(){activeObservers.add(this)}disconnect(){activeObservers.delete(this)}},
  setTimeout:(callback,ms)=>{const id=++sequence;timers.set(id,{callback,at:now+ms});return id},clearTimeout:id=>timers.delete(id),
  document:{querySelector:s=>s==='#main'?root:null,querySelectorAll:s=>s.includes('contenteditable')?[box]:labels.filter(label=>label.isConnected),createElement:()=>{const label={dataset:{},style:{},isConnected:false,remove(){this.isConnected=false}};return label}}
 };
 // Use one VM global for the page state across all invocations.
 const run=input=>{context.input=input;return runInNewContext('('+chatTranslationPage.toString()+')(input)',context)};
 return {run,suspend:()=>context.__juxinChatTranslationV2?.suspend?.(),stats:()=>({observers:activeObservers.size,listeners:listeners.size,timers:timers.size,createdObservers,labels:labels.filter(label=>label.isConnected).length}),advance(ms){now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.callback()}},node};
}

test('turning translation off disconnects its observer and rejects delayed apply; enabling resumes exactly once',()=>{
 const page=pageFixture(),settings=chatSettings({enabled:true}),batch=page.run({kind:'poll',settings});
 assert.equal(batch.messages.length,1);assert.equal(page.stats().observers,1);
 page.run({kind:'poll',settings:{...settings,enabled:false}});
 const disabled=page.stats(),result=page.run({kind:'apply',conversation:batch.conversation,...batch.messages[0],translation:'late'});
 console.log(JSON.stringify({case:'disabled-page',disabled,lateApplied:result.applied}));
 assert.equal(disabled.observers,0);assert.equal(disabled.listeners,0);assert.equal(disabled.timers,0);assert.equal(result.applied,false);
 page.run({kind:'poll',settings});page.run({kind:'poll',settings});
 assert.equal(page.stats().observers,1);assert.equal(page.stats().listeners,1);assert.equal(page.stats().timers,1);
});

test('an unpolled hidden translation page releases only its own DOM watchers after the active lease expires',()=>{
 const page=pageFixture(),settings=chatSettings({enabled:true}),batch=page.run({kind:'poll',settings});
 page.advance(1000);page.run({kind:'poll',settings});page.advance(1000);
 assert.equal(page.stats().observers,1,'a renewed visible page remains observed');
 page.advance(1000);const expired=page.stats();
 const late=page.run({kind:'apply',conversation:batch.conversation,...batch.messages[0],translation:'late'});
 console.log(JSON.stringify({case:'unpolled-page',expired,lateApplied:late.applied}));
 assert.equal(expired.observers,0);assert.equal(expired.listeners,0);assert.equal(expired.timers,0);assert.equal(late.applied,false);
 const fresh=page.run({kind:'poll',settings});assert.equal(fresh.messages.length,1);assert.equal(page.stats().observers,1);
 assert.equal(page.node.innerText,'Hello','the website message and its account session stay untouched');
});

test('read-only export does not install translation watchers on an inactive page',()=>{
 const page=pageFixture();assert.equal(page.run({kind:'export'}).messages.length,1);
 console.log(JSON.stringify({case:'export-only-page',resources:page.stats()}));
 assert.equal(page.stats().observers,0);assert.equal(page.stats().listeners,0);assert.equal(page.stats().timers,0);
});

test('repeated polls keep one observer and timer; task handoff can suspend it idempotently',()=>{
 const page=pageFixture(),settings=chatSettings({enabled:true});
 for(let i=0;i<100;i++){page.run({kind:'poll',settings});page.advance(300)}
 assert.equal(page.stats().createdObservers,1);assert.equal(page.stats().observers,1);assert.equal(page.stats().listeners,1);assert.equal(page.stats().timers,1);
 page.suspend();page.suspend();assert.equal(page.stats().observers,0);assert.equal(page.stats().listeners,0);assert.equal(page.stats().timers,0);
 page.advance(3000);assert.equal(page.stats().observers,0,'an old timer cannot restart the adapter');
 page.run({kind:'poll',settings});assert.equal(page.stats().createdObservers,2);assert.equal(page.stats().observers,1);assert.equal(page.stats().timers,1);
});
