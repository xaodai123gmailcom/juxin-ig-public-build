import test from 'node:test';import assert from 'node:assert/strict';
import {chatSettings,translateChat} from '../../dist-electron/chat-translation.js';
test('online provider requests and decoding, credentials stay in headers',async()=>{
 for(const provider of ['google','deepl','microsoft']){
  let request;const key=provider==='google'?'':'example:fx';const fake=async(url,options)=>{request={url,options};return Response.json(provider==='google'?[[['你好','Hello']]]:provider==='deepl'?{translations:[{text:'你好'}]}:[{translations:[{text:'你好'}]}])};
  assert.equal(await translateChat('Hello','zh-CN',chatSettings({provider,region:'eastasia'}),key,fake),'你好');assert.equal(request.options.redirect,'error');assert.equal(request.url.includes('example'),false);
 }
});
test('provider errors are visible; no fallback, fake result or raw credential response',async()=>{
 const s=chatSettings({provider:'deepl'});await assert.rejects(translateChat('hi','en',s,''),/密钥/);
 await assert.rejects(translateChat('hi','en',s,'key',async()=>new Response('sensitive upstream details',{status:429})),/限流/);
 await assert.rejects(translateChat('hi','en',s,'key',async()=>Response.json({})),/有效译文/);
 assert.throws(()=>chatSettings({color:'url(evil)'}));assert.throws(()=>chatSettings({provider:'local-ai'}));
 for(const fontSize of [0,7,49,12.5,'16',null,NaN,Infinity,true])assert.throws(()=>chatSettings({fontSize}));
 for(const fontSize of [8,12,48])assert.equal(chatSettings({fontSize}).fontSize,fontSize);
});

import {ChatTranslation} from '../../dist-electron/chat-translation.js';
import {mkdtempSync,rmSync,readFileSync,writeFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
test('disabling translation in a covered editor clears old page annotations after returning to the account',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-editor-'));let visible=false;const calls=[];
 const translation=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>visible?{id:'profile'}:undefined,async(_id,step)=>{if(step.kind==='check')return {profile:'profile'};if(!visible)throw new Error('covered');calls.push(step);return {messages:[]}},()=>null,()=>{});
 try{
  await translation.command('plan',{settings:chatSettings({enabled:false})},'session');assert.equal(calls.length,0);
  visible=true;await translation.tick();assert.equal(calls.length,1);assert.equal(calls[0].settings.enabled,false);
  await translation.tick();assert.equal(calls.length,1,'disabled cleanup is applied once');
 }finally{translation.stop();rmSync(directory,{recursive:true,force:true})}
});

test('provider Retry-After pauses every anonymous Google account and resumes without toggling',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-rate-'));let now=1000,target='a',calls=0;const applied=[];
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:target}),async(id,step)=>{
  if(step.kind==='check')return {profile:id};if(step.kind==='poll')return {conversation:id,messages:[{id:'1',original:'Hello'},{id:'2',original:'Good evening'}]};
  applied.push({account:id,...step});return {applied:true};
 },()=>null,()=>{},{now:()=>now,fetcher:async()=>{calls++;return calls===1?new Response('',{status:429,headers:{'retry-after':'120'}}):Response.json([[['你好','Hello']]])}});
 try{
  for(const id of ['a','b'])await service.command(id,{settings:chatSettings({enabled:true})},'session');
  await service.tick();assert.equal(calls,1);assert.equal(applied[0].retryable,true);assert.equal(applied[0].retryAt,121000);
  now=2000;await service.tick();target='b';await service.tick();assert.equal(calls,1,'other queued messages/accounts cannot bypass provider cooldown');
  const status=await service.command('b',{},'session');assert.equal(status.retryAt,121000);assert.match(status.error,/119 秒后自动重试/);
  now=121001;await service.tick();assert.equal(calls,2);assert.ok(applied.some(x=>x.account==='b'&&x.translation==='你好'));
  assert.equal((await service.command('b',{},'session')).retryAt,0);
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('duplicate messages and color/size/toggle changes reuse translations instead of requesting again',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-cache-'));let calls=0;const applied=[];
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(_id,step)=>{
  if(step.kind==='check')return {profile:'a'};if(step.kind==='poll')return {conversation:'archive',messages:[{id:'in',original:'Hello'},{id:'out',original:'Hello'}]};
  applied.push(step);return {applied:true};
 },()=>null,()=>{},{now:()=>1000,fetcher:async()=>{calls++;return Response.json([[['你好','Hello']]])}});
 try{
  await service.command('a',{settings:chatSettings({enabled:true})},'session');await service.tick();assert.equal(calls,1);assert.equal(applied.length,2);
  await service.command('a',{settings:chatSettings({enabled:false})},'session');
  await service.command('a',{settings:chatSettings({enabled:true,color:'#ffffff',fontSize:24})},'session');await service.tick();
  assert.equal(calls,1);assert.equal(applied.length,4);
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('legacy outgoing settings and draft jobs cannot translate or send input',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-no-send-'));let calls=0;const applied=[];
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(_id,step)=>{
  if(step.kind==='check')return {profile:'a'};if(step.kind==='poll')return {conversation:'a',messages:[],draft:{id:'draft-1',original:'你好'}};applied.push(step);return {applied:true};
 },()=>null,()=>{},{fetcher:async()=>{calls++;return Response.json([[['Hello','你好']]])}});
 try{const r=await service.command('a',{settings:chatSettings({enabled:true,outgoing:true})},'session');assert.equal(r.settings.outgoing,false);await service.tick();assert.equal(calls,0);assert.deepEqual(applied,[])}finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('each non-English message uses automatic source detection independently of outgoing English',async()=>{
 const samples=['Magandang umaga.','¿Cómo estás?','สวัสดีครับ','Bonjour, how are you?'];
 for(const provider of ['google','deepl','microsoft'])for(const key of provider==='google'?['','cloud-test']:['example:fx'])for(const text of samples){
  let request;
  await translateChat(text,'zh-CN',chatSettings({provider,outgoingLang:'en'}),key,async(url,options)=>{
   request={url:new URL(url),body:options.body?JSON.parse(options.body):null};
   return Response.json(provider==='google'?(key?{data:{translations:[{translatedText:'测试译文',detectedSourceLanguage:'tl'}]}}:[[['测试译文',text]],null,'tl']):provider==='deepl'?{translations:[{text:'测试译文',detected_source_language:'TL'}]}:[{detectedLanguage:{language:'tl'},translations:[{text:'测试译文'}]}]);
  });
  if(provider==='google'&&!key){assert.equal(request.url.searchParams.get('sl'),'auto');assert.equal(request.url.searchParams.get('q'),text);assert.equal(request.url.searchParams.get('tl'),'zh-CN')}
  else if(provider==='google'){assert.equal(request.body.source,undefined);assert.equal(request.body.q,text);assert.equal(request.body.target,'zh-CN')}
  else if(provider==='deepl'){assert.equal(request.body.source_lang,undefined);assert.deepEqual(request.body.text,[text])}
  else{assert.equal(request.url.searchParams.has('from'),false);assert.equal(request.body[0].Text,text)}
 }
});

test('appearance migrates legacy preferences, persists per account, and rejects invalid writes',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-appearance-')),file=join(directory,'settings.json');
 writeFileSync(file,JSON.stringify({legacy:{enabled:true,color:'#123456',provider:'google'}}));
 const make=()=>new ChatTranslation(file,()=> 'session',()=>undefined,async(id,step)=>step.kind==='check'?{profile:id}:{messages:[]},()=>null,()=>{});
 let service=make();
 try{
  const legacy=await service.command('legacy',{},'session');assert.equal(legacy.settings.fontSize,12);assert.equal(legacy.settings.color,'#123456');
  await service.command('a',{settings:chatSettings({color:'#ABCDEF',fontSize:28})},'session');
  await service.command('b',{settings:chatSettings({color:'#ff6699',fontSize:10})},'session');
  const before=readFileSync(file,'utf8');await assert.rejects(service.command('a',{settings:{fontSize:1000}},'session'));assert.equal(readFileSync(file,'utf8'),before);
  service.stop();service=make();
  const a=(await service.command('a',{},'session')).settings,b=(await service.command('b',{},'session')).settings;
  assert.equal(a.fontSize,28);assert.equal(a.color,'#ABCDEF');assert.equal(b.fontSize,10);assert.equal(b.color,'#ff6699');
  assert.equal((await service.command('legacy',{},'session')).settings.fontSize,12);
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('secure cache survives restart, remains account/provider/language scoped, and avoids network during cooldown',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-persist-')),secrets=new Map(),applied=[];let now=1000,calls=0,original='Hello',target='a';
 const make=()=>new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:target}),async(id,step)=>{
  if(step.kind==='check')return {profile:id};if(step.kind==='poll')return {conversation:'chat',messages:[{id:'message',original}]};applied.push({account:id,...step});return {applied:true};
 },k=>secrets.get(k)||null,(k,v)=>secrets.set(k,v),{now:()=>now,fetcher:async()=>{calls++;return original==='Limited'?new Response('',{status:429,headers:{'Retry-After':'120'}}):Response.json([[['你好',original]]])}});
 let service=make();
 try{
  await service.command('a',{settings:chatSettings({enabled:true})},'session');await service.tick();assert.equal(calls,1);service.stop();
  assert.ok(secrets.has('chat-translation-cache:a'));assert.equal(secrets.get('chat-translation-cache:a').includes('Hello'),false);
  service=make();await service.command('a',{},'session');await service.tick();assert.equal(calls,1,'restart uses the OS secure cache');
  original='Limited';now=3000;await service.tick();assert.equal(calls,2);
  original='Hello';await service.tick();assert.equal(calls,2);assert.equal(applied.at(-1).translation,'你好','cache applies during Retry-After');
  target='b';await service.command('b',{settings:chatSettings({enabled:true})},'session');await service.tick();assert.equal(calls,2);assert.notEqual(applied.at(-1).account,'b','account B must not borrow account A cache');
  now=123001;await service.tick();assert.equal(calls,3);
  target='a';await service.command('a',{settings:chatSettings({enabled:true,incomingLang:'ja'})},'session');now+=2000;await service.tick();assert.equal(calls,4,'new target language has its own cache');
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('a slow request never blocks cached visible messages or issues concurrent anonymous requests',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-flight-'));let now=1000,calls=0,release,original='Hello';const applied=[];
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(_id,step)=>{
  if(step.kind==='check')return {profile:'a'};if(step.kind==='poll')return {conversation:'chat',messages:[{id:'message',original}]};applied.push(step);return {applied:true};
 },()=>null,()=>{},{now:()=>now,fetcher:async()=>{calls++;if(calls===2)await new Promise(resolve=>release=resolve);return Response.json([[['你好','text']]])}});
 try{
  await service.command('a',{settings:chatSettings({enabled:true})},'session');await service.tick();
  original='Slow';now=3000;const pending=service.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,2);
  original='Hello';await service.tick();assert.equal(calls,2);assert.equal(applied.length,2,'cached result is not held behind the slow request');
  original='Another uncached';now=9000;await service.tick();assert.equal(calls,2,'only one anonymous request may be in flight');
  release();await pending;
 }finally{release?.();service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('free access errors and invalid responses do not request a paid key or echo upstream content',async()=>{
 await assert.rejects(translateChat('Hello','zh-CN',chatSettings({}), '',async()=>new Response('',{status:403})),e=>e.kind==='access'&&!/密钥|付费/.test(e.message));
 await assert.rejects(translateChat('Hello','zh-CN',chatSettings({}), '',async()=>new Response('private upstream content')),e=>e.kind==='service'&&!e.message.includes('private'));
});

test('one persisted engine makes toolbar modes mutually exclusive and rejects late Immersive results',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-mode-'));let release,fetches=0;const applied=[];
 const im={translate:()=>new Promise(resolve=>release=resolve),settings:async()=>{},cacheIdentity:()=> 'native-settings',status:()=>({version:'1.33.1'}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(id,step)=>{if(step.kind==='check')return {profile:id};if(step.kind==='poll')return {conversation:'a',messages:[{id:'1',original:'Hello'}]};applied.push(step);return {applied:true}},()=>null,()=>{},{immersive:im,fetcher:async()=>{fetches++;return Response.json([[['你好','Hello']]])}});
 try{
  let result=await service.command('a',{mode:'immersive'},'session');assert.equal(result.settings.enabled,true);assert.equal(result.settings.engine,'immersive');
  const pending=service.tick();await new Promise(resolve=>setImmediate(resolve));
  result=await service.command('a',{mode:'builtin'},'session');assert.equal(result.settings.engine,'builtin');release('旧插件译文');await pending;assert.equal(applied.length,0);
  await service.tick();assert.equal(fetches,1);assert.equal(applied[0].translation,'你好');
  result=await service.command('a',{mode:'off'},'session');assert.equal(result.settings.enabled,false);
  const saved=JSON.parse(readFileSync(join(directory,'settings.json'),'utf8')).a;assert.equal(saved.enabled,false);assert.equal(saved.engine,'builtin');
 }finally{release?.('结束');service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('Immersive requests stop applying after task ownership changes, and duplicate messages share one request',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-task-'));let target='a',release,calls=0;const applied=[];
 const im={translate:()=>{calls++;return new Promise(resolve=>release=resolve)},settings:async()=>{},cacheIdentity:()=> 'config',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>target?{id:target}:undefined,async(id,step)=>{if(step.kind==='check')return {profile:id};if(step.kind==='poll')return {conversation:'a',messages:[{id:'1',original:'Hello'},{id:'2',original:'Hello'}]};applied.push(step);return {applied:true}},()=>null,()=>{},{immersive:im});
 try{await service.command('a',{mode:'immersive'},'session');const pending=service.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,1);target='';release('你好');await pending;assert.deepEqual(applied,[])}finally{release?.('结束');service.stop();rmSync(directory,{recursive:true,force:true})}
});


test('native plugin configuration changes invalidate in-flight results and the page revision',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-native-config-'));let config='v1',release;const applied=[],polls=[];
 const im={translate:()=>new Promise(resolve=>release=resolve),settings:async()=>{},cacheIdentity:()=>config,status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(id,step)=>{if(step.kind==='check')return {profile:id};if(step.kind==='poll'){polls.push(step.settings.translationRevision);return {conversation:'a',messages:[{id:'1',original:'Hello'}]}}applied.push(step);return {applied:true}},()=>null,()=>{},{immersive:im});
 try{
  await service.command('a',{mode:'immersive'},'session');const pending=service.tick();await new Promise(resolve=>setImmediate(resolve));config='v2';release('obsolete');await pending;assert.equal(applied.length,0);
  const next=service.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(polls.at(-1),'v2');release('updated');await next;assert.equal(applied.at(-1).translation,'updated');
 }finally{release?.('cleanup');service.stop();rmSync(directory,{recursive:true,force:true})}
});


test('production migration disables the removed builtin engine and never sends hidden provider requests',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-retired-')),file=join(directory,'settings.json');let calls=0;
 writeFileSync(file,JSON.stringify({a:chatSettings({enabled:true,engine:'builtin'})}));
 const im={translate:async()=> '你好',settings:async()=>{},cacheIdentity:()=> 'cfg',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(file,()=> 'session',()=>({id:'a'}),async(id,step)=>step.kind==='check'?{profile:id}:step.kind==='poll'?{conversation:'a',messages:[{id:'1',original:'Hello'}]}:{applied:true},()=>null,()=>{},{immersiveOnly:true,immersive:im,fetcher:async()=>{calls++;throw new Error('retired provider')}});
 try{const migrated=await service.command('a',{},'session');assert.equal(migrated.settings.engine,'immersive');assert.equal(migrated.settings.enabled,false);await service.tick();assert.equal(calls,0);await assert.rejects(service.command('a',{mode:'builtin'},'session'),/已停用/);assert.equal(JSON.parse(readFileSync(file,'utf8')).a.enabled,false)}finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});

test('a translated message rejected by a covered/task-owned page never blocks the provider',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-apply-conflict-'));let message=0,calls=0;
 const im={translate:async()=>{calls++;return '译文'},settings:async()=>{},cacheIdentity:()=> 'cfg',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(id,step)=>{
  if(step.kind==='check')return {profile:id};if(step.kind==='poll')return {conversation:'a',messages:[{id:String(message),original:'message '+message}]};
  throw new Error('Core task lease changed before applying the translation');
 },()=>null,()=>{},{immersive:im});
 try{
  await service.command('a',{mode:'immersive'},'session');
  for(message=0;message<4;message++){await service.tick();await new Promise(resolve=>setImmediate(resolve))}
  assert.equal(calls,4);const status=await service.command('a',{},'session');assert.equal(status.error,'');assert.equal(status.retryAt,0);
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});
test('completion from before reset cannot remove the new sessions in-flight job reservation',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-reset-conflict-')),releases=[];let calls=0;
 const im={translate:()=>{calls++;return new Promise(resolve=>releases.push(resolve))},settings:async()=>{},cacheIdentity:()=> 'cfg',status:()=>({}),retry:()=>{},reset:()=>{}};
 const service=new ChatTranslation(join(directory,'settings.json'),()=> 'session',()=>({id:'a'}),async(id,step)=>step.kind==='check'?{profile:id}:step.kind==='poll'?{conversation:'a',messages:[{id:'1',original:'message'}]}:{applied:true},()=>null,()=>{},{immersive:im});
 let first,second;
 try{
  await service.command('a',{mode:'immersive'},'session');first=service.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,1);
  service.reset();await service.command('a',{},'session');second=service.tick();await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,2);
  releases[0]('old result');await first;await service.tick();assert.equal(calls,2,'old finally cannot make the new job look absent');
 }finally{service.stop();for(const release of releases)release('cleanup');await Promise.allSettled([first,second]);rmSync(directory,{recursive:true,force:true})}
});


test('temporary immersive failures remain retryable after three failures',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-retry-r93-'));let now=1000,calls=0;
 const im={translate:async()=>{calls++;throw Object.assign(new Error('temporary network timeout'),{retryAfterMs:3000})},settings:async()=>{},cacheIdentity:()=> 'cfg',status:()=>({}),retry:()=>{},reset:()=>{}};
 const core=async(_id,step)=>step.kind==='check'?{profile:'p'}:step.kind==='poll'?{conversation:'chat',messages:[{id:'m',original:'hello'}]}:{};
 const service=new ChatTranslation(join(directory,'prefs.json'),()=> 'session',()=>({id:'p'}),core,()=>null,()=>{},{now:()=>now,immersive:im});
 try{
  await service.command('a',{settings:chatSettings({engine:'immersive',enabled:true})},'session');
  for(let i=0;i<4;i++){await service.tick();now+=4000}
  assert.equal(calls,4);
  assert.ok([...service.gates.values()].every(g=>!g.blocked));
 }finally{service.stop();rmSync(directory,{recursive:true,force:true})}
});


test('a completed translation displays before vendor cleanup releases its job',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'translation-display-r93-'));let finish;const applied=[];
 const im={translate:async(_id,_text,_lang,_settings,_current,onResult)=>{await onResult('你好');await new Promise(r=>finish=r);return '你好'},settings:async()=>{},cacheIdentity:()=> 'cfg',status:()=>({}),retry:()=>{},reset:()=>{}};
 const core=async(_id,step)=>{if(step.kind==='check')return {profile:'p'};if(step.kind==='apply'){applied.push(step);return {}};return {conversation:'chat',messages:applied.length?[]:[{id:'m',original:'hello'}]}};
 const service=new ChatTranslation(join(directory,'prefs.json'),()=> 'session',()=>({id:'p'}),core,()=>null,()=>{},{immersive:im});
 let pending;
 try{
  await service.command('a',{settings:chatSettings({engine:'immersive',enabled:true})},'session');
  pending=service.tick();for(let i=0;i<8;i++)await Promise.resolve();
  assert.equal(applied[0].translation,'你好');assert.equal(service.immersiveJobs.size,1);
  finish();await pending;assert.equal(service.immersiveJobs.size,0);assert.equal(applied.length,1);
 }finally{finish?.();await pending;service.stop();rmSync(directory,{recursive:true,force:true})}
});
