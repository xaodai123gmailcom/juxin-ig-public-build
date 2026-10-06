import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync,mkdirSync,renameSync,readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {runInNewContext} from 'node:vm';
const {stripTypeScriptTypes}=await import('node:module');
let moduleRoot=new URL('../../dist-electron/',import.meta.url);
if(typeof stripTypeScriptTypes==='function'){
 const directory=mkdtempSync(join(tmpdir(),'chat-r34-source-'));
 after(()=>rmSync(directory,{recursive:true,force:true}));
 writeFileSync(join(directory,'package.json'),JSON.stringify({type:'module'}));
 for(const name of ['chat-translation','chat-translation-page','immersive-policy','immersive-source','immersive-domains','atomic-secure-store']){
  writeFileSync(join(directory,name+'.js'),stripTypeScriptTypes(readFileSync(new URL('../src/'+name+'.ts',import.meta.url),'utf8'),{mode:'transform'}));
 }
 moduleRoot=pathToFileURL(directory+'/');
}
const {ChatTranslation,chatSettings}=await import(new URL('chat-translation.js',moduleRoot));
const {chatTranslationPage}=await import(new URL('chat-translation-page.js',moduleRoot));
const pageCode='const chatTranslationPage='+chatTranslationPage.toString();
const nextTurn=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
// Small DOM tree with selector/ancestor behavior, not a captured WhatsApp DOM.
// Assertions exercise the real page adapter; native Electron layout remains in
// the Windows integration gate. No account is opened and no message is sent.
function selectorParts(selector){
 const result=[];let value='',depth=0,quote='';
 for(const char of selector){if(quote){value+=char;if(char===quote)quote='';continue}if(char==='"'||char==="'"){quote=char;value+=char;continue}if(char==='[')depth++;if(char===']')depth--;if(/\s/.test(char)&&!depth){if(value){result.push(value);value=''}}else value+=char}
 if(value)result.push(value);return result;
}
class Element {
 constructor(tag='div',attributes={},text=''){this.tagName=tag;this.attributes={...attributes};this.ownText=text;this.children=[];this.parentElement=null;this.style={};this.dataset=new Proxy({}, {set:(_obj,key,value)=>{this.attributes['data-'+String(key).replace(/[A-Z]/g,c=>'-'+c.toLowerCase())]=value;return true}});this.nodeType=1}
 getAttribute(name){return this.attributes[name]??null}
 get childNodes(){return [...(this.ownText?[{nodeType:3,textContent:this.ownText,parentElement:this}]:[]),...this.children]}
 get textContent(){return this.ownText+this.children.map(child=>child.textContent).join('')}
 set textContent(value){this.ownText=String(value);for(const child of this.children)child.parentElement=null;this.children=[]}
 get innerText(){return this.textContent}
 get isConnected(){return this.connected===true||Boolean(this.parentElement?.isConnected)}
 append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node)}return this}
 remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(node=>node!==this);this.parentElement=null}}
 insertAdjacentElement(position,node){assert.equal(position,'afterend');const parent=this.parentElement,index=parent.children.indexOf(this);node.remove();node.parentElement=parent;parent.children.splice(index+1,0,node)}
 contains(node){return node===this||this.children.some(child=>child.contains(node))}
 getClientRects(){return this.isConnected?[{}]:[]}
 getBoundingClientRect(){return this.tagName==='footer'||this.getAttribute('contenteditable')==='true'?{top:500,bottom:540,left:0,right:600}:{top:100,bottom:120,left:0,right:600}}
 addEventListener(){} removeEventListener(){}
 simple(selector){
  const attributes=[...selector.matchAll(/\[[^\]]+\]/g)].map(match=>match[0]);let rest=selector.replace(/\[[^\]]+\]/g,'');
  const tag=/^[\w-]+/.exec(rest);if(tag&&this.tagName!==tag[0])return false;
  for(const match of rest.matchAll(/([.#])([\w-]+)/g)){if(match[1]==='#'&&this.getAttribute('id')!==match[2])return false;if(match[1]==='.'&&!String(this.getAttribute('class')||'').split(/\s+/).includes(match[2]))return false}
  for(const attr of attributes){const match=/^\[([\w-]+)(?:\s*(\*=|\^=|\$=|~=|=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*(i)?)?\]$/.exec(attr);assert.ok(match,'supported fixture selector '+attr);let actual=this.getAttribute(match[1]);if(actual===null)return false;if(!match[2])continue;let expected=match[3]??match[4]??match[5];if(match[6]){actual=actual.toLowerCase();expected=expected.toLowerCase()}if(match[2]==='='&&actual!==expected)return false;if(match[2]==='*='&&!actual.includes(expected))return false;if(match[2]==='^='&&!actual.startsWith(expected))return false;if(match[2]==='$='&&!actual.endsWith(expected))return false;if(match[2]==='~='&&!actual.split(/\s+/).includes(expected))return false}
  return true;
 }
 matches(selector){return selector.split(',').some(part=>{const parts=selectorParts(part.trim());let node=this;if(!node.simple(parts.pop()))return false;while(parts.length){const next=parts.pop();node=node.parentElement;while(node&&!node.simple(next))node=node.parentElement;if(!node)return false}return true})}
 closest(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null}
 querySelectorAll(selector){const result=[];const walk=node=>{for(const child of node.children){if(child.matches(selector))result.push(child);walk(child)}};walk(this);return result}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null}
}
function fixture(){
 const document=new Element('document');document.connected=true;document.createElement=tag=>new Element(tag);
 const main=new Element('main',{id:'main'}),header=new Element('header',{},'Alice'),footer=new Element('footer'),box=new Element('div',{contenteditable:'true',role:'textbox'},'Unsent draft');footer.append(box);main.append(header,footer);document.append(main);
 const timers=new Map();let serial=0,now=1000;
 const context={crypto:globalThis.crypto,document,location:{hostname:'web.whatsapp.com',pathname:'/'},innerHeight:600,HTMLTextAreaElement:class{},Node:{ELEMENT_NODE:1,TEXT_NODE:3},MutationObserver:class{observe(){}disconnect(){}},getComputedStyle:()=>({overflowY:'visible'}),Date:class extends Date{static now(){return now}},setTimeout:(callback,ms)=>{const id=++serial;timers.set(id,{callback,at:now+ms});return id},clearTimeout:id=>timers.delete(id)};
 runInNewContext(pageCode,context);
 const run=input=>{context.input=input;return runInNewContext('chatTranslationPage(input)',context)};
 const settings={enabled:true,engine:'immersive',incomingLang:'zh-CN',color:'#993333',fontSize:12};
 const poll=()=>run({kind:'poll',settings});
 const text=value=>new Element('span',{class:'selectable-text copyable-text',dir:'auto'},value);
 const message=(value,quoteAttributes)=>{
  const bubble=new Element('div',{class:'message-in'}),content=new Element('div',{'data-pre-plain-text':'[10:04, Alice]'});bubble.append(content);
  let quote;if(quoteAttributes){quote=new Element('div',quoteAttributes).append(new Element('span',{},'Sharon Ann'),text('No I do that later'));content.append(quote)}
  const body=text(value);content.append(body,new Element('time',{},'10:04'));main.children.splice(main.children.indexOf(footer),0,bubble);bubble.parentElement=main;
  return {bubble,content,body,quote};
 };
 return {run,poll,settings,message,text,main,box,context,document,Element,header,advance(ms){now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.callback()}}};
}

const apply=(f,batch,extra={})=>f.run({kind:'apply',conversation:batch.conversation,...batch.messages[0],translation:'旧语言译文',...extra});

test('a late success or failure cannot apply after target language changes, including A to B to A',()=>{
 const f=fixture(),message=f.message('Hello'),first=f.poll();
 const second=f.run({kind:'poll',settings:{...f.settings,incomingLang:'fr'}});
 assert.equal(apply(f,first).applied,false,'old Chinese translation must not become a French translation');
 assert.equal(apply(f,first,{error:true,status:'old failure',retryable:false}).applied,false);
 assert.equal(f.document.querySelectorAll('[data-juxin-chat-status]').length,0);
 const third=f.poll();
 assert.equal(apply(f,first).applied,false,'returning to the same language must not revive departed work');
 assert.equal(apply(f,second).applied,false);
 assert.equal(apply(f,third,{translation:'你好'}).applied,true);
 assert.equal(f.poll().messages.length,0);
 assert.equal(message.body.innerText,'Hello');assert.equal(f.box.innerText,'Unsent draft');
});

test('provider, selected channel and configuration revisions revoke old message batches',()=>{
 for(const changed of [{provider:'microsoft'},{immersiveService:'deepl'},{translationRevision:'config-v2'}]){
  const f=fixture();f.message('Hello');const first=f.poll();
  const fresh=f.run({kind:'poll',settings:{...f.settings,...changed}});
  assert.equal(apply(f,first).applied,false,JSON.stringify(changed));
  assert.equal(apply(f,fresh).applied,true);
 }
});

test('suspending and resuming the same chat revokes old successes and statuses without changing the draft',()=>{
 const f=fixture();f.message('Hello');const before=f.poll();
 f.context.__juxinChatTranslationV2.suspend();const fresh=f.poll();
 assert.equal(apply(f,before).applied,false);
 assert.equal(apply(f,before,{error:true,status:'expired'}).applied,false);
 assert.equal(apply(f,fresh).applied,true);assert.equal(f.box.innerText,'Unsent draft');
});

test('disabled then enabled and an expired then renewed lease each reject their old batch',()=>{
 for(const suspend of [f=>f.run({kind:'poll',settings:{...f.settings,enabled:false}}),f=>f.advance(1801)]){
  const f=fixture();f.message('Hello');const before=f.poll();suspend(f);const fresh=f.poll();
  assert.equal(apply(f,before).applied,false);assert.equal(apply(f,fresh).applied,true);
 }
});

test('appearance changes and read-only export preserve valid work and apply current styling',()=>{
 const f=fixture();f.message('Hello');const before=f.poll();f.run({kind:'export'});
 f.run({kind:'poll',settings:{...f.settings,color:'#abcdef',fontSize:24}});
 assert.equal(apply(f,before).applied,true);
 const label=f.document.querySelector('[data-juxin-chat-translation]');assert.equal(label.style.color,'#abcdef');assert.equal(label.style.fontSize,'24px');
});

test('an apply already waiting inside Core cannot overwrite a later settings command',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'chat-r34-core-')),f=fixture(),release=deferred(),arrived=deferred();f.message('Hello');
 const attempts=[];let block=true;
 const runtime={translate:async(_id,_text,lang)=>lang==='fr'?'Bonjour':'你好',settings:async()=>{},cacheIdentity:()=> 'config',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'account'}),async(id,step)=>{
  if(step.kind==='check')return {profile:id};
  if(step.kind==='apply'){if(block){block=false;arrived.resolve();await release.promise}const result=f.run(step);attempts.push({step,result});return result}
  return f.run(step);
 },()=>null,()=>{},{immersive:runtime});
 let first;
 try{
  await service.command('account',{settings:chatSettings({...f.settings,immersiveMode:'manual',immersiveService:'plugin',incomingLang:'zh-CN'})},'session');
  first=service.tick();await arrived.promise;
  await service.command('account',{settings:chatSettings({...f.settings,immersiveMode:'manual',immersiveService:'plugin',incomingLang:'fr'})},'session');
  release.resolve();await first;await service.tick();await nextTurn();
  assert.equal(attempts[0].result.applied,false,'server-side delayed old apply must fail its page fence');
  assert.ok(attempts.some(({step,result})=>step.translation==='Bonjour'&&result.applied));
  assert.equal(f.document.querySelector('[data-juxin-chat-translation]')?.textContent,'Bonjour');
  assert.equal(f.box.innerText,'Unsent draft');
 }finally{release.resolve();await first;service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('renewing the same chat adapter invalidates provider work so it can release its worker slot',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'chat-r34-renew-')),f=fixture(),jobs=[];f.message('Hello');
 const runtime={translate:(_id,text,_lang,_settings,current)=>new Promise(resolve=>jobs.push({text,current,resolve})),settings:async()=>{},cacheIdentity:()=> 'config',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'account'}),async(id,step)=>step.kind==='check'?{profile:id}:f.run(step),()=>null,()=>{},{immersive:runtime});
 let first;
 try{
  await service.command('account',{settings:chatSettings(f.settings)},'session');first=service.tick();await nextTurn();assert.equal(jobs.length,1);
  f.context.__juxinChatTranslationV2.suspend();await service.tick();
  assert.equal(jobs[0].current(),false,'same conversation text after a new adapter lease is still a different request lifetime');
  jobs[0].resolve('old');await first;
  const fresh=service.tick();await nextTurn();assert.equal(jobs.length,2);jobs[1].resolve('new');await fresh;
  assert.equal(f.document.querySelector('[data-juxin-chat-translation]')?.textContent,'new');
 }finally{service.stop();jobs.forEach(job=>job.resolve('cleanup'));await first;rmSync(directory,{recursive:true,force:true})}
});

test('known contact titles keep translated messages stable while online and typing status changes',()=>{
 const f=fixture();f.header.textContent='';
 const title=new Element('span',{title:'Alice',dir:'auto'},'Alice'),status=new Element('span',{},'online');f.header.append(title,status);f.message('Hello');
 const before=f.poll();assert.equal(apply(f,before,{translation:'你好'}).applied,true);
 const label=f.document.querySelector('[data-juxin-chat-translation]');status.textContent='typing…';
 const during=f.poll();assert.equal(during.conversation,before.conversation);assert.equal(during.generation,before.generation);
 assert.equal(during.messages.length,0,'presence updates must not requeue all translated messages');assert.equal(label.isConnected,true);assert.equal(f.document.querySelector('[data-juxin-chat-translation]'),label);
 title.attributes.title='Bob';title.textContent='Bob';const switched=f.poll();
 assert.notEqual(switched.conversation,before.conversation);assert.equal(label.isConnected,false);assert.equal(apply(f,before).applied,false);assert.equal(apply(f,switched).applied,true);
 assert.equal(f.box.innerText,'Unsent draft');
});

test('unrecognized headers keep their previous fallback and unrelated tooltip titles are not contact identities',()=>{
 const f=fixture();f.header.textContent='';const unrelated=new Element('button',{title:'Contact information'},'Alice'),status=new Element('span',{},'online');f.header.append(unrelated,status);f.message('Hello');
 const before=f.poll();assert.equal(before.conversation,'/|Aliceonline');status.textContent='typing…';const after=f.poll();
 assert.equal(after.conversation,'/|Alicetyping…');assert.notEqual(after.conversation,before.conversation);
});

test('missing or forged page generation cannot install a translation or status label',()=>{
 const f=fixture();f.message('Hello');const batch=f.poll();
 for(const generation of [undefined,NaN,-1,'2',batch.generation+1]){
  assert.equal(apply(f,batch,{generation}).applied,false);
  assert.equal(apply(f,batch,{generation,error:true,status:'stale'}).applied,false);
 }
 assert.equal(f.document.querySelectorAll('[data-juxin-chat-translation],[data-juxin-chat-status]').length,0);
 assert.equal(apply(f,batch).applied,true);
});

test('same URL document reload cannot reuse the previous document batch, even with matching message IDs',()=>{
 const old=fixture(),fresh=fixture();old.message('Hello');fresh.message('Hello');
 const before=old.poll(),after=fresh.poll();assert.equal(before.messages[0].id,after.messages[0].id);
 assert.equal(apply(fresh,before).applied,false,'document-local serial numbers alone must not authorize old results');
 assert.equal(apply(fresh,before,{error:true,status:'old document failure'}).applied,false);
 assert.equal(apply(fresh,after).applied,true);
});

test('failed settings-file commit leaves runtime preferences unchanged and can be retried',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'chat-r34-save-')),file=join(directory,'settings.json'),backup=join(directory,'previous.json');
 const make=()=>new ChatTranslation(file,()=> 'session',()=>undefined,async(id,step)=>step.kind==='check'?{profile:id}:{messages:[]},()=>null,()=>{});
 let service=make();
 try{
  await service.command('account',{settings:chatSettings({enabled:false,incomingLang:'zh-CN'})},'session');const saved=readFileSync(file,'utf8');
  renameSync(file,backup);mkdirSync(file);
  await assert.rejects(service.command('account',{settings:chatSettings({enabled:true,incomingLang:'fr'})},'session'));
  const current=await service.command('account',{},'session');assert.equal(current.settings.enabled,false,'a rejected commit must not enable translation in memory');assert.equal(current.settings.incomingLang,'zh-CN');
  assert.equal(readFileSync(backup,'utf8'),saved);assert.deepEqual(readdirSync(directory).sort(),['previous.json','settings.json']);
  rmSync(file,{recursive:true});renameSync(backup,file);
  await service.command('account',{settings:chatSettings({enabled:true,incomingLang:'fr'})},'session');service.stop();service=make();
  const restarted=await service.command('account',{},'session');assert.equal(restarted.settings.enabled,true);assert.equal(restarted.settings.incomingLang,'fr');
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});
