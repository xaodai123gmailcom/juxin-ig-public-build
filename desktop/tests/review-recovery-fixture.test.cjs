const test=require('node:test'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const ts=require('typescript');
const {readReviewRecoveryFixture}=require('./review-recovery-fixture.cjs');

async function harness(){
 const recovery=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/review-recovery.js')));
 const preview=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/profile-preview.js')));
 const wc=new EventEmitter(),win=new EventEmitter(),loads=[];
 let url='about:blank',doc={protocol:'about:',title:'',ready:'complete',charset:'UTF-8',links:[]},destroyed=false;
 Object.assign(wc,{isDestroyed:()=>destroyed,getURL:()=>url,getTitle:()=>doc.title,stop:()=>{},executeJavaScript:async()=>({...doc,links:[...doc.links]})});
 const popup={webContents:wc,isDestroyed:()=>destroyed,contentView:{setVisible:()=>{}},setTitle:()=>{},setMenu:()=>{},setAutoHideMenuBar:()=>{},setMenuBarVisibility:()=>{},
  loadURL:value=>{loads.push(value);return Promise.resolve()}};
 // The native window transport is simulated; ReviewPage state transitions and
 // recovery HTML below are the actual, unchanged production implementation.
 class FloatingWebPage{
  constructor(_changed,options){this.options=options}
  open(){if(!this.popup){this.popup=popup;void popup.loadURL(this.options.url)}return true}
  dispose(){} hide(){} raise(){}
 }
 const exports={};
 const source=readFileSync(path.resolve(__dirname,'../src/review-page.ts'),'utf8');
 const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
 runInNewContext(compiled,{exports,setTimeout,clearTimeout,require:name=>{
  if(name==='electron')return {Menu:{buildFromTemplate:()=>({})}};
  if(name==='./floating-web-page.js')return {FloatingWebPage};
  if(name==='./review-recovery.js')return recovery;
  if(name==='./profile-preview.js')return preview;
  throw new Error('Unexpected production dependency: '+name);
 }});
 const review=new exports.ReviewPage('test-only');
 function response(target,status){
  url='https://www.instagram.com/'+target+'/';
  wc.emit('did-start-navigation',{},url,false,true);
  wc.emit('did-navigate',{},url,status);
  if(status===200){doc={protocol:'https:',title:'4xx Client Error',ready:'complete',charset:'UTF-8',links:[]};wc.emit('did-finish-load')}
 }
 function showRecovery(){
  url=loads.findLast(value=>value.startsWith('data:text/html'));
  assert.ok(url,'production must actually generate a recovery page');
  const html=decodeURIComponent(url.slice(url.indexOf(',')+1));
  doc={protocol:'data:',title:/<title>(.*?)<\/title>/.exec(html)[1],ready:'complete',charset:'UTF-8',links:[...html.matchAll(/<a href="([^"]+)"/g)].map(match=>match[1])};
  wc.emit('did-navigate',{},url,200);wc.emit('dom-ready');wc.emit('did-finish-load');
 }
 return {review,wc,win,response,showRecovery,close:()=>{destroyed=true},setDocument:value=>{doc={...doc,...value}}};
}

async function runCase(h,target,status,{afterDocument=()=>{},extraSteps=[]}={}){
 let clock=0,reads=0;
 const steps=[...extraSteps,()=>h.response(target,status),()=>{h.showRecovery();afterDocument()}];
 const result=await readReviewRecoveryFixture(h.wc,{target,start:()=>h.review.openTarget(h.win,target),readState:()=>{reads++;return h.review.state()},
  now:()=>clock,sleep:async ms=>{clock+=ms;steps.shift()?.()},timeout:200});
 return {result,reads,clock};
}

test('unchanged ReviewPage reproduces actual:null expected:429 when the previous error title is reused',async()=>{
 const h=await harness();h.review.openTarget(h.win,'missing.user');h.response('missing.user',404);h.showRecovery();
 h.review.openTarget(h.win,'limited.user');
 assert.equal(h.wc.getTitle(),'审核网页暂时无法打开','old title makes the previous wait succeed too early');
 assert.equal(h.review.state().httpStatus,null,'new request correctly has no response yet');
 assert.throws(()=>assert.equal(h.review.state().httpStatus,429),error=>error.code==='ERR_ASSERTION'&&error.actual===null&&error.expected===429);
});

test('current response and its recovery document precede strict 404, 429 and HTTP-200 error checks',async()=>{
 const h=await harness();
 for(const [target,status] of [['missing.user',404],['limited.user',429],['proxy.error',200]]){
  const {result,reads}=await runCase(h,target,status);
  assert.equal(result.responseStatus,status);assert.equal(result.state.httpStatus,status);assert.equal(result.state.lastTarget,target);
  assert.ok(result.document.links.includes('https://www.instagram.com/'+target+'/'));assert.equal(reads,1);
  assert.equal(h.wc.listenerCount('did-navigate'),1,'only the original production listener remains');
 }
});

test('same-target retry must observe a new response and new data-document commit, not its old title or links',async()=>{
 const h=await harness();await runCase(h,'limited.user',429);
 let clock=0,reads=0;
 const steps=[()=>{},()=>h.response('limited.user',429),()=>{},()=>h.showRecovery()];
 const result=await readReviewRecoveryFixture(h.wc,{target:'limited.user',start:()=>h.review.openTarget(h.win,'limited.user'),readState:()=>{reads++;assert.equal(clock,80);return h.review.state()},
  now:()=>clock,sleep:async ms=>{clock+=ms;steps.shift()?.()}});
 assert.equal(result.state.httpStatus,429);assert.equal(reads,1);
});

test('a ready document cannot replace null or wrong production state with the expected response status',async()=>{
 for(const bad of [null,404]){
  const h=await harness();
  const {result,reads}=await runCase(h,'limited.user',429,{afterDocument:()=>{h.review.httpStatus=bad}});
  assert.equal(result.responseStatus,429);assert.equal(result.state.httpStatus,bad);assert.equal(reads,1);
  assert.throws(()=>assert.equal(result.state.httpStatus,429),error=>error.code==='ERR_ASSERTION');
 }
});

test('wrong target document times out and cannot satisfy readiness even after a data navigation',async()=>{
 const h=await harness();let clock=0,reads=0;
 await assert.rejects(readReviewRecoveryFixture(h.wc,{target:'limited.user',start:()=>{h.review.openTarget(h.win,'limited.user');h.response('limited.user',429);h.showRecovery();h.setDocument({links:['https://www.instagram.com/missing.user/']})},
  readState:()=>{reads++;return h.review.state()},timeout:60,now:()=>clock,sleep:async ms=>{clock+=ms}}),/limited.user: current response\/document not ready/);
 assert.equal(reads,0);assert.equal(h.wc.listenerCount('did-navigate'),1);
});

test('start failure, closed renderer and wrong encoding fail with listener cleanup',async()=>{
 const h=await harness();
 await assert.rejects(readReviewRecoveryFixture(h.wc,{target:'limited.user',start:()=>{throw new Error('start failed')},readState:()=>assert.fail('must not read')}),/start failed/);
 assert.equal(h.wc.listenerCount('did-navigate'),0);
 await assert.rejects(runCase(h,'limited.user',429,{afterDocument:()=>h.setDocument({charset:'windows-1252'})}),/expected UTF-8/);
 assert.equal(h.wc.listenerCount('did-navigate'),1);
 h.close();await assert.rejects(readReviewRecoveryFixture(h.wc,{target:'limited.user',start:()=>{},readState:()=>assert.fail('must not read')}),/renderer closed/);
 assert.equal(h.wc.listenerCount('did-navigate'),1);
});

test('stalled renderer evaluation has a real deadline and late completion cannot read state',async()=>{
 const h=await harness();let resolveProbe,reads=0;
 h.wc.executeJavaScript=()=>new Promise(resolve=>{resolveProbe=resolve});
 const pending=readReviewRecoveryFixture(h.wc,{target:'limited.user',timeout:40,
  start:()=>{h.review.openTarget(h.win,'limited.user');h.response('limited.user',429);h.showRecovery()},
  readState:()=>{reads++;return h.review.state()}});
 await assert.rejects(pending,/renderer\/readiness deadline exceeded/);
 assert.equal(h.wc.listenerCount('did-navigate'),1);assert.equal(reads,0);
 resolveProbe({protocol:'data:',title:'审核网页暂时无法打开',ready:'complete',charset:'UTF-8',links:['https://www.instagram.com/limited.user/']});
 await new Promise(resolve=>setImmediate(resolve));assert.equal(reads,0);
});

test('a renderer result arriving after the deadline fails even before the timer callback runs',async()=>{
 const h=await harness();let clock=0,reads=0;
 const evaluate=h.wc.executeJavaScript;
 h.wc.executeJavaScript=async()=>{clock=60;return evaluate()};
 await assert.rejects(readReviewRecoveryFixture(h.wc,{target:'limited.user',timeout:40,now:()=>clock,
  start:()=>{h.review.openTarget(h.win,'limited.user');h.response('limited.user',429);h.showRecovery()},
  readState:()=>{reads++;return h.review.state()}}),/renderer\/readiness deadline exceeded/);
 assert.equal(reads,0);assert.equal(h.wc.listenerCount('did-navigate'),1);
});
