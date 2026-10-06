const {session}=require('electron');
const assert=require('node:assert/strict');
const {WebSocket}=require('ws');
const {waitFixtureDocument}=require('./navigation-fixture.cjs');
module.exports=async({host,owner})=>{
 const id='native:77777777-7777-4777-8777-777777777777';
 const {createWhatsAppSession,WhatsAppPageRuntime}=await import('../../dist-electron/whatsapp-page.js');
 const {EventEmitter}=require('node:events');const stalled=new EventEmitter();Object.assign(stalled,{session:{serviceWorkers:new EventEmitter()},isDestroyed:()=>false,executeJavaScriptInIsolatedWorld:()=>new Promise(()=>{})});
 const stalledRuntime=new WhatsAppPageRuntime(stalled);let deadline;
 try{const values=await Promise.race([Promise.all([stalledRuntime.inspect(),stalledRuntime.inspect()]),new Promise((_,reject)=>{deadline=setTimeout(()=>reject(new Error('concurrent diagnostic hung')),5000)})]);assert.ok(values.every(x=>x.inspectionPending));}finally{clearTimeout(deadline);stalled.emit('destroyed');}
 console.log('PASS concurrent diagnostics remain bounded when the page does not answer');
 const ses=await createWhatsAppSession(owner,id,'');
 // Hold the real main-document response until Open has acknowledged the
 // native window. This proves the asynchronous contract without timing a
 // machine-dependent three-second resource delay.
 let releaseDocument,releaseResource,documentResponseReleased=false,slowResourceFinished=false;
 const documentGate=new Promise(resolve=>{releaseDocument=resolve});
 const resourceGate=new Promise(resolve=>{releaseResource=resolve});
 ses.protocol.handle('https',async request=>{
  if(new URL(request.url).pathname==='/slow-resource.png'){
   await resourceGate;slowResourceFinished=true;
   return new Response('',{status:404});
  }
  if(new URL(request.url).pathname==='/db-worker.js')return new Response(`onmessage=async()=>{let root;try{root=await navigator.storage.getDirectory();const f=await root.getFileHandle('worker-probe',{create:true}),h=await f.createSyncAccessHandle();h.write(new TextEncoder().encode('persisted'));h.flush();const b=new Uint8Array(9);h.read(b,{at:0});h.close();postMessage(new TextDecoder().decode(b))}catch(e){postMessage(e.name+': '+e.message)}finally{if(root)await root.removeEntry('worker-probe').catch(()=>{})}}`,{headers:{'content-type':'text/javascript'}});
  await documentGate;
  documentResponseReleased=true;
  return new Response('<meta charset="utf-8"><h1 hidden>whatsapp-native-fixture</h1><style>body{margin:0}button{position:absolute;left:40px;top:40px;width:180px;height:60px}</style><button onclick="document.body.dataset.clicks=+(document.body.dataset.clicks||0)+1">关联电话号码</button><img src="/slow-resource.png">',{headers:{'content-type':'text/html'}});
 });
 const body={profile:id,owner,url:'https://web.whatsapp.com/?lang=zh_cn'};
 try{
  let openDeadline,result;
  try{result=await Promise.race([host.control('open-whatsapp',body),new Promise((_,reject)=>{openDeadline=setTimeout(()=>reject(new Error('WhatsApp Open blocked on the withheld document response')),10000)})]);}finally{clearTimeout(openDeadline)}
  assert.equal(result.page_loaded,null,'ordinary Open acknowledges the window before page readiness');
  assert.equal(result.navigation_pending,true,'page readiness remains explicitly pending');
  assert.equal(slowResourceFinished,false,'window opens before slow resources finish');
  let p=host.profiles.get(id),page=p.pages.get(p.selected),wc=page.view.webContents;
  assert.equal(p.manualOnly,true);assert.equal(wc.debugger.isAttached(),false,'manual login must not attach the automation debugger');
  assert.equal(documentResponseReleased,false,'Open does not wait for the withheld main document response');
  releaseDocument();releaseResource();
  await waitFixtureDocument(wc,body.url,{label:'WhatsApp initial login fixture',heading:'whatsapp-native-fixture',timeoutMs:10000});
  console.log('PASS WhatsApp asynchronous Open acknowledges a real window before navigation; exact fixture document becomes ready afterward');
  await wc.executeJavaScript("localStorage.setItem('fixture-login','kept')");
  const bounds={x:250,y:90,width:950,height:700};host.show(p,bounds);
  await new Promise(r=>setTimeout(r,200));
  const viewport=await wc.executeJavaScript('({width:innerWidth,height:innerHeight})');
  assert.deepEqual(viewport,{width:950,height:700},'manual page uses native viewport dimensions');
  wc.sendInputEvent({type:'mouseDown',x:110,y:70,button:'left',clickCount:1});wc.sendInputEvent({type:'mouseUp',x:110,y:70,button:'left',clickCount:1});
  await new Promise(r=>setTimeout(r,50));assert.equal(await wc.executeJavaScript('document.body.dataset.clicks'),'1');
  assert.ok(ses.storagePath.includes('WA'),'uses the independent short storage root');
  assert.equal(await wc.executeJavaScript(`new Promise(resolve=>{const w=new Worker('/db-worker.js');w.onmessage=e=>{resolve(e.data);w.terminate()};w.onerror=e=>resolve(e.message);w.postMessage('start')})`),'persisted','real first-party worker OPFS synchronous write/read');
  const prelogin=await host.control('chat-translation',{profile:id,owner,generation:p.generation,step:{kind:'poll',settings:{enabled:true,outgoing:true,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',provider:'google',region:''}}});
  assert.deepEqual(prelogin.messages,[]);assert.match(prelogin.status,/登录/);
  await wc.executeJavaScript(`console.error('UnknownError: Opening backing store failed at https://web.whatsapp.com/db.js?secret=DO_NOT_EXPORT');document.body.insertAdjacentHTML('beforeend','<p>数据库错误</p>')`);
  const diagnostic=await host.control('whatsapp-diagnostics',{profile:id,owner,generation:p.generation});
  assert.equal(diagnostic.runtime.runtime,'whatsapp-independent-v1');assert.equal(diagnostic.runtime.state.phase,'storage-error');
  assert.match(diagnostic.runtime.firstFailure.message,/Opening backing store failed/);assert.ok(!JSON.stringify(diagnostic).includes('DO_NOT_EXPORT'));
  const evaluate=wc.executeJavaScriptInIsolatedWorld.bind(wc);wc.executeJavaScriptInIsolatedWorld=(world,...args)=>world===1003?new Promise(()=>{}):evaluate(world,...args);
  try{const stalledReport=await host.control('whatsapp-diagnostics',{profile:id,owner,generation:p.generation});assert.equal(stalledReport.checks.error,'RendererProbeTimeout');assert.match(stalledReport.runtime.firstFailure.message,/Opening backing store failed/);}finally{wc.executeJavaScriptInIsolatedWorld=evaluate;}
  console.log('PASS main-process diagnostic timeout still returns original failure when renderer probe stalls');
  let stopped=0;const originalStop=wc.stop;wc.stop=()=>{stopped++;return originalStop.call(wc)};
  const navigated=await host.control('manual-navigation',{profile:id,owner,generation:p.generation,action:'refresh',homeUrl:'https://web.whatsapp.com/'});assert.equal(navigated.page_loaded,true);assert.equal(stopped,0,'WA refresh does not call the Instagram stop routine');wc.stop=originalStop;
  const generation=p.generation;
  await assert.rejects(host.control('open-whatsapp',{...body,owner:'22222222-2222-4222-8222-222222222222'}));
  await assert.rejects(host.control('open-whatsapp',{...body,url:'https://web.whatsapp.com.evil.test/'}));
  const connected=await new Promise(resolve=>{const ws=new WebSocket(host.endpoint(p));ws.on('open',()=>{ws.close();resolve(true)});ws.on('error',()=>resolve(false));setTimeout(()=>{ws.terminate();resolve(false)},1500).unref()});
  assert.equal(connected,false,'manual WhatsApp session never becomes a task CDP endpoint');
  await host.closeProfile(p);
  await host.control('open-whatsapp',{...body,cookies:[{name:'native-import',value:'fixture',domain:'.whatsapp.com',path:'/',secure:true,httpOnly:true}]});
  p=host.profiles.get(id);wc=p.pages.get(p.selected).view.webContents;
  assert.ok(p.generation>generation);assert.equal(wc.debugger.isAttached(),false);
  await waitFixtureDocument(wc,body.url,{label:'WhatsApp reopened cookie fixture',heading:'whatsapp-native-fixture',timeoutMs:10000});
  assert.equal(await wc.executeJavaScript("localStorage.getItem('fixture-login')"),'kept');
  assert.equal((await ses.cookies.get({name:'native-import'}))[0].value,'fixture');
  console.log('PASS WhatsApp rebuilt session, worker OPFS, first-failure detail, pre-login translation guard and refresh lifecycle');
  console.log('PASS WhatsApp native loading before slow resources finish, debugger-free viewport/input, cookie import, retained login data and rejected foreign/task access');
 }finally{releaseDocument();releaseResource();const p=host.profiles.get(id);if(p)await host.closeProfile(p);ses.protocol.unhandle('https')}
};
