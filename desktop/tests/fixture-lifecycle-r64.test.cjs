/* Code-level lifecycle/geometry fault models. These are not native acceptance. */
const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {EventEmitter}=require('node:events');
const {createFixtureLifecycle,stableGeometry,bounded,cleanupPreservingError,paintScript,semanticScript}=require('./fixture-lifecycle.cjs');
const {prepareVisibleFixture}=require('./visible-fixture.cjs');
const {rendererFixtureRead}=require('./renderer-fixture.cjs');
const {assertLayout,stableNurtureLayout}=require('./nurture-reels-r6.integration.cjs');
function clock(t) {
  let now=0;t.mock.method(require('node:perf_hooks').performance,'now',()=>now);t.mock.timers.enable({apis:['setTimeout']});
  return ms=>{now+=ms;t.mock.timers.tick(ms)};
}
async function advance(promise,tick) {
  let complete=false;
  const observed=promise.then(value=>({value}),error=>({error})).then(value=>{complete=true;return value});
  for(let i=0;i<1500&&!complete;i++){await new Promise(setImmediate);if(!complete)tick(5)}
  assert.equal(complete,true,'bounded operation never settled');
  const result=await observed;if(result.error)throw result.error;return result.value;
}
function model(t,{frames=true,hidden=false,pendingFonts=false,stalled=false,capture='good',recoverOnShow=true,nativeHeight=800,rendererHeight=800,imageHeight=800,pixelScale=1}={}) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'r64-lifecycle-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const state={width:1000,height:rendererHeight,nativeHeight,imageHeight,pixelScale,visible:true,hidden,frames:0,reads:0,captures:0,hide:0,show:0,mutations:0,fontsReads:0};
  const fonts={status:pendingFonts?'loading':'loaded'};
  Object.defineProperty(fonts,'ready',{get(){state.fontsReads++;return pendingFonts?new Promise(()=>{}):Promise.resolve()}});
  const doc={readyState:'complete',get visibilityState(){return state.hidden?'hidden':'visible'},fonts,hasFocus:()=>true,
    querySelector:()=>({textContent:'committed state',getBoundingClientRect:()=>({width:900,height:600}),querySelectorAll:()=>[],getAttribute:()=>null})};
  const wc=new EventEmitter();wc.isDestroyed=()=>false;wc.focus=()=>{};
  wc.executeJavaScript=async source=>{state.reads++;if(stalled)return new Promise(()=>{});return vm.runInNewContext(source,{document:doc,innerWidth:state.width,innerHeight:state.height,devicePixelRatio:state.pixelScale,location:{href:'http://127.0.0.1/fixture'},requestAnimationFrame(callback){state.frames++;if(frames)setImmediate(callback)}})};
  const image={isEmpty:()=>capture==='empty',toPNG:()=>capture==='empty-bytes'?Buffer.alloc(0):Buffer.from('nonempty-native-image-model'),getSize:()=>({width:capture==='empty-size'?0:Math.ceil(Math.fround(Math.fround(1000)*Math.fround(state.pixelScale))),height:Math.ceil(Math.fround(Math.fround(state.imageHeight)*Math.fround(state.pixelScale)))})};
  wc.capturePage=async()=>{state.captures++;if(capture==='hang')return new Promise(()=>{});if(capture==='throw')throw Error('native capture failed');return image};
  const win=new EventEmitter();Object.assign(win,{webContents:wc,isDestroyed:()=>false,isVisible:()=>state.visible,isMinimized:()=>false,isFocused:()=>true,focus(){},getContentSize:()=>[state.width,state.nativeHeight],getBounds:()=>({x:0,y:0,width:state.width,height:state.height}),show(){state.show++;state.visible=true;if(recoverOnShow)state.hidden=false},hide(){state.hide++;state.visible=false;state.hidden=true}});
  const lifecycle=createFixtureLifecycle(win,{label:'fault model',outputDirectory:directory});
  return {win,wc,state,lifecycle,directory,image};
}
function nurtureFrame(width=1598){const node={x:0,y:0,width:100,height:50,right:100,bottom:50,client:100,scroll:100};return {width,height:1000,document:width,summary:Array(4).fill(node),windows:Array(7).fill(node),settings:Array(2).fill(node),metrics:[node],buttons:[node]}}
test('semantic readiness never awaits pending fonts or suspended frames and does not claim paint',async t=>{
  const tick=clock(t),f=model(t,{frames:false,pendingFonts:true,hidden:true});
  const value=await advance(f.lifecycle.semantic('#root',{timeoutMs:400,stableMs:20}),tick);
  assert.equal(value.text,'committed state');assert.equal(f.state.frames,0);assert.equal(f.state.fontsReads,0);assert.equal(f.state.captures,0);assert.equal(f.lifecycle.state.captures.length,0);
  assert.doesNotMatch(semanticScript('#root'),/requestAnimationFrame|fonts\.ready|setTimeout/);
});
test('capture requires actual frame callbacks even for a responsive visible renderer',async t=>{
  const tick=clock(t),f=model(t,{frames:false});
  await assert.rejects(advance(f.lifecycle.capture('occluded model',{paintTimeoutMs:50}),tick),error=>{
    assert.match(error.fixturePhase.name,/genuine visible paint/);assert.match(error.message,/timed out/);return true;
  });
  assert.equal(f.state.frames,1);assert.equal(f.state.captures,0);assert.equal(f.lifecycle.state.captures.length,0);
  assert.equal(await f.lifecycle.read('true','renderer still responds'),true);
});
test('real callback model plus pending fonts permits visible capture without a font promise',async t=>{
  const f=model(t,{pendingFonts:true});assert.equal(await f.lifecycle.capture('visible model'),f.image);
  assert.equal(f.state.frames,2);assert.equal(f.state.fontsReads,0);assert.equal(f.lifecycle.state.captures.length,1);
  assert.equal(f.lifecycle.state.captures[0].painted.painted,true);
});
test('hidden renderer must recover through one real hide/show before capture',async t=>{
  const tick=clock(t),f=model(t,{hidden:true});
  await advance(f.lifecycle.capture('recovered model',{visibilityTimeoutMs:400}),tick);
  assert.equal(f.state.hide,1);assert.equal(f.state.show,1);assert.equal(f.state.hidden,false);assert.equal(f.state.frames,2);
});
test('persistent hidden model cannot capture or record accepted pixels',async t=>{
  const tick=clock(t),f=model(t,{hidden:true,recoverOnShow:false});
  await assert.rejects(advance(f.lifecycle.capture('still hidden',{visibilityTimeoutMs:100}),tick),/visibility|timed out/);
  assert.equal(f.state.captures,0);assert.equal(f.state.frames,0);
});
test('finite delayed visibility reads may cross the observation grace but still recover',async t=>{
  const tick=clock(t),f=model(t,{hidden:true});
  const original=f.wc.executeJavaScript;f.wc.executeJavaScript=async source=>{await new Promise(resolve=>setTimeout(resolve,20));return original(source)};
  const result=await advance(prepareVisibleFixture(f.win,{label:'delayed responsive',timeoutMs:400}),tick);
  assert.equal(result.recovered,true);assert.equal(f.state.hide,1);assert.equal(f.state.show,1);
});
test('a genuine hung visibility read fails without recovery or a second read',async t=>{
  const tick=clock(t),f=model(t,{stalled:true});
  await assert.rejects(advance(prepareVisibleFixture(f.win,{timeoutMs:50}),tick),error=>error.code==='FIXTURE_RENDERER_TIMEOUT');
  assert.equal(f.state.reads,1);assert.equal(f.state.hide,0);assert.equal(f.state.show,0);
});
test('destroyed renderer has a distinct tagged failure and never executes JavaScript',async()=>{
  await assert.rejects(rendererFixtureRead({isDestroyed:()=>true,executeJavaScript(){assert.fail('destroyed read')}},'true',{label:'destroyed proof'}),error=>error.code==='FIXTURE_RENDERER_DESTROYED'&&error.label==='destroyed proof');
});
test('nurture differential rejects old valid geometry at every other requested width',()=>{
  const old=nurtureFrame();assert.doesNotThrow(()=>assertLayout(old,1598));
  for(const width of [1000,800,560])assert.throws(()=>assertLayout(old,width),/requested width/);
});
test('nurture refuses empty or incomplete groups and preserves overflow assertions',()=>{
  for(const group of ['summary','windows','settings','metrics','buttons']){const frame=nurtureFrame();frame[group]=[];assert.throws(()=>assertLayout(frame,1598),/geometry/)}
  const frame=nurtureFrame();frame.settings=[frame.settings[0]];assert.throws(()=>assertLayout(frame,1598),/count/);
  const bad=nurtureFrame();bad.document=1700;assert.throws(()=>assertLayout(bad,1598),/overflow/);
});
test('nurture stable geometry requires matching native AND renderer widths',async t=>{
  const tick=clock(t),frame=nurtureFrame(1000);
  await assert.rejects(advance(stableNurtureLayout(async()=>frame,1000,{timeoutMs:50,stableMs:10,pollMs:5,nativeSize:()=>[1598,1000]}),tick),/did not settle/);
  let reads=0;const frames=[nurtureFrame(1598),nurtureFrame(1000)];
  const accepted=await advance(stableNurtureLayout(async()=>frames[Math.min(reads++,1)],1000,{timeoutMs:150,stableMs:10,pollMs:5,nativeSize:()=>[1000,1000]}),tick);
  assert.equal(accepted.width,1000);assert.ok(reads>=3);
});
test('stable geometry rejects permanently empty groups and bounds hung evaluator',async t=>{
  const tick=clock(t);
  await assert.rejects(advance(stableGeometry(async()=>({width:1000,cards:[]}),'',1000,{groups:{cards:'nonempty'},timeoutMs:40,stableMs:10,pollMs:5}),tick),/last geometry/);
  await assert.rejects(advance(stableGeometry(()=>new Promise(()=>{}),'',1000,{timeoutMs:40}),tick),/timed out/);
});
for(const failure of ['empty','empty-bytes','empty-size','throw','hang'])test('capture failure '+failure+' never produces acceptance',async t=>{
  const tick=clock(t),f=model(t,{capture:failure});
  await assert.rejects(advance(f.lifecycle.capture('capture '+failure,{captureTimeoutMs:50}),tick));
  assert.equal(f.lifecycle.state.captures.length,0);
});
test('capture rejects a viewport that reverts during native capture',async t=>{
  const f=model(t);f.wc.capturePage=async()=>{f.state.width=900;return f.image};
  await assert.rejects(f.lifecycle.capture('resize raced'),/viewport survives capture/);assert.equal(f.lifecycle.state.captures.length,0);
});
test('tagged read phases retain exact expression, unique IDs and last committed action',async t=>{
  const f=model(t);await f.lifecycle.read('true','initial DOM');await f.lifecycle.read('true','same expression');
  const original=Error('renderer evaluator broke');f.wc.executeJavaScript=()=>{throw original};
  await assert.rejects(f.lifecycle.read('throw 1','failing action'),error=>error===original&&error.fixturePhase.name==='failing action');
  assert.equal(f.lifecycle.state.lastCommittedAction.name,'same expression');assert.equal(new Set(f.lifecycle.state.actions.map(action=>action.id)).size,3);
  assert.equal(f.lifecycle.state.currentAction.source,'throw 1');assert.match(f.lifecycle.state.currentAction.sourceHash,/^[a-f0-9]{64}$/);
});
test('diagnostic and all cleanup failures stay secondary to the identical primary error',async t=>{
  const tick=clock(t),f=model(t,{capture:'hang'}),primary=Error('business assertion failed');
  f.wc.executeJavaScript=()=>{throw Error('diagnostic renderer failed')};
  await advance(f.lifecycle.diagnose(primary),tick);
  const calls=[];
  await assert.rejects(f.lifecycle.finish(primary,[['restore',()=>{calls.push('restore');throw Error('restore failed')}],['HTTP',()=>{calls.push('HTTP');throw Error('HTTP failed')}]]),error=>error===primary&&error.fixtureCleanupErrors.length===2);
  assert.deepEqual(calls,['restore','HTTP']);
  const saved=JSON.parse(fs.readFileSync(path.join(f.directory,'fault-model-lifecycle.json'),'utf8'));
  assert.equal(saved.primaryError.message,'business assertion failed');assert.equal(saved.cleanupErrors.length,2);assert.equal(saved.diagnosticErrors.length,2);assert.equal(saved.completed,false);
});
test('cleanup errors on an otherwise passing fixture fail the run',async t=>{
  const f=model(t);await assert.rejects(f.lifecycle.finish(null,[['HTTP',()=>{throw Error('close refused')}]]),/cleanup failed.*close refused/);
  assert.equal(f.lifecycle.state.completed,false);
});
test('wrapper cleanup runs every operation without replacing the primary',async()=>{
  const primary=Error('primary'),calls=[];
  await assert.rejects(cleanupPreservingError(primary,[['destroy',()=>{calls.push('destroy');throw Error('destroy failed')}],['owner',()=>calls.push('owner')]]),error=>error===primary&&error.fixtureCleanupErrors[0].name==='destroy');
  assert.deepEqual(calls,['destroy','owner']);
});
test('diagnostic event serialization cannot crash the fixture on circular details',async t=>{
  const f=model(t),details={reason:'crashed'};details.self=details;
  assert.doesNotThrow(()=>f.wc.emit('render-process-gone',{},details));assert.equal(f.lifecycle.state.events[0].event,'render-process-gone');
  await assert.rejects(f.lifecycle.finish(),/fatal renderer lifecycle failure/);assert.equal(f.wc.listenerCount('render-process-gone'),0);
});
test('positive completion persists phase evidence and actual observed helper identities',async t=>{
  const f=model(t);await f.lifecycle.read('true','initial state');await f.lifecycle.capture('pixels');await f.lifecycle.finish();
  const saved=JSON.parse(fs.readFileSync(path.join(f.directory,'fault-model-lifecycle.json'),'utf8'));
  assert.equal(saved.completed,true);assert.equal(saved.entryMode,'embedded');assert.equal(saved.captures.length,1);assert.match(saved.sourceFiles['fixture-lifecycle.cjs'],/^[a-f0-9]{64}$/);
});
test('main-thread late completion cannot outrun a phase deadline',async t=>{
  let now=0;t.mock.method(require('node:perf_hooks').performance,'now',()=>now);
  await assert.rejects(bounded(()=>{now=20;return true},{label:'late phase',timeoutMs:10}),/timed out/);
});
test('migrated semantic fixtures have no font-ready promises or unbounded acceptance capture',()=>{
  for(const file of ['nurture-reels-r6','work-report-summary-r6','source-recheck','completed-card-delete']){
    const source=fs.readFileSync(path.join(__dirname,file+'.integration.cjs'),'utf8');
    assert.doesNotMatch(source,/document\.fonts\.ready|requestAnimationFrame/);assert.match(source,/lifecycle\.capture/);assert.match(source,/lifecycle\.finish/);
  }
  assert.match(paintScript,/requestAnimationFrame\(\(\)=>requestAnimationFrame/);assert.doesNotMatch(paintScript,/setTimeout|fonts\.ready/);
});
test('a timed-out mutating renderer action executes once and is never retried',async t=>{
  const tick=clock(t),f=model(t);let resolve,calls=0;
  f.wc.executeJavaScript=()=>{calls++;return new Promise(done=>{resolve=done})};
  await assert.rejects(advance(f.lifecycle.read('mutate()','single mutation',50),tick),/timed out/);
  resolve(true);await new Promise(setImmediate);
  assert.equal(calls,1);assert.equal(f.lifecycle.state.lastCommittedAction,null);assert.equal(f.lifecycle.state.actions.length,1);
});
test('native-width recovery must itself remain stable for the requested interval',async t=>{
  const tick=clock(t);let nativeWidth=1598,nativeBecameValid,returnedAt;
  const perf=require('node:perf_hooks').performance;
  const result=await advance(stableGeometry(async()=>({width:1000,height:800}), '',1000,{height:800,timeoutMs:200,stableMs:40,pollMs:5,nativeSize:()=>{if(perf.now()>=25 && nativeWidth!==1000){nativeWidth=1000;nativeBecameValid=perf.now()}return [nativeWidth,800]}}).then(value=>{returnedAt=perf.now();return value}),tick);
  assert.equal(result.width,1000);assert.ok(returnedAt-nativeBecameValid>=40);
});
test('Electron console event fields and renderer-gone reason survive in failure evidence',async t=>{
  const f=model(t);
  f.wc.emit('console-message',{level:'error',message:'production render failed',sourceId:'fixture.js',lineNumber:42});
  f.wc.emit('render-process-gone',{}, {reason:'crashed',exitCode:9});
  assert.equal(f.lifecycle.state.rendererErrors.length,2);
  assert.match(f.lifecycle.state.rendererErrors[0].details[0],/production render failed/);
  assert.match(f.lifecycle.state.rendererErrors[1].details[0],/crashed/);
});
test('explicit requested capture height rejects stale native and renderer heights',async t=>{
 const f=model(t);
 await assert.rejects(f.lifecycle.capture('requested height',{width:1000,height:1100}),/painted viewport height matches request/);
 assert.equal(f.state.captures,0);assert.equal(f.lifecycle.state.captures.length,0);
});
test('capture rejects native, renderer and image height mismatches independently',async t=>{
 for(const [options,pattern] of [[{nativeHeight:600},/native viewport height/],[{rendererHeight:600},/painted viewport height/],[{imageHeight:400},/capture pixels match/]]){
  const f=model(t,options);await assert.rejects(f.lifecycle.capture('height mismatch',{width:1000,height:800}),pattern);assert.equal(f.lifecycle.state.captures.length,0);
 }
});
test('capture checks physical pixel dimensions using observed HiDPI scale rather than DIP size',async t=>{
 for(const scale of [1,1.25,1.5,2]){
  const f=model(t,{pixelScale:scale});await f.lifecycle.capture('scaled screenshot',{width:1000,height:800});
  assert.deepEqual(f.lifecycle.state.captures[0].expectedPixels,{width:Math.ceil(1000*scale),height:Math.ceil(800*scale)});
 }
});
test('native height changes during capture are rejected',async t=>{
 const f=model(t);f.wc.capturePage=async()=>{f.state.nativeHeight=600;return f.image};
 await assert.rejects(f.lifecycle.capture('height race'),/native viewport height survives capture/);assert.equal(f.lifecycle.state.captures.length,0);
});
test('stable geometry rejects stale requested renderer or native heights',async t=>{
 const tick=clock(t);
 for(const [height,nativeHeight] of [[800,1100],[1100,800]])await assert.rejects(advance(stableGeometry(async()=>({width:1000,height}),'',1000,{height:1100,nativeSize:()=>[1000,nativeHeight],timeoutMs:40,stableMs:10,pollMs:5}),tick),/did not settle/);
});
for(const event of ['render-process-gone','preload-error'])test(event+' is fatal to completion and later reads',async t=>{
 const f=model(t);await f.lifecycle.read('true','final business assertion');f.wc.emit(event,{}, {reason:'crashed',exitCode:9});
 await assert.rejects(f.lifecycle.read('true','must not continue'),error=>error.code==='FIXTURE_RENDERER_FATAL');
 await assert.rejects(f.lifecycle.finish(),error=>error.code==='FIXTURE_RENDERER_FATAL');assert.equal(f.lifecycle.state.completed,false);
 const saved=JSON.parse(fs.readFileSync(path.join(f.directory,'fault-model-lifecycle.json'),'utf8'));assert.equal(saved.completed,false);assert.equal(saved.fatalRendererErrors.length,1);
});
test('fatal renderer event during cleanup fails completion but does not skip independent cleanup',async t=>{
 const f=model(t),calls=[];
 await assert.rejects(f.lifecycle.finish(null,[['first',()=>{calls.push('first');f.wc.emit('render-process-gone',{}, {reason:'crashed'})}],['second',()=>calls.push('second')]]),/fatal renderer lifecycle/);
 assert.deepEqual(calls,['first','second']);assert.equal(f.lifecycle.state.completed,false);
});
test('fatal renderer evidence preserves an existing primary business error',async t=>{
 const f=model(t),original=Error('CAS mismatch');f.wc.emit('render-process-gone',{}, {reason:'crashed'});
 await assert.rejects(f.lifecycle.finish(original),error=>error===original);assert.equal(f.lifecycle.state.primaryError.message,'CAS mismatch');
});
test('reload success removes only its temporary listeners',async t=>{
 const f=model(t),before=f.wc.listenerCount('did-fail-load');f.wc.reload=()=>setImmediate(()=>f.wc.emit('did-finish-load'));
 assert.equal(await f.lifecycle.reload('history reload',100),true);assert.equal(f.wc.listenerCount('did-finish-load'),0);assert.equal(f.wc.listenerCount('did-fail-load'),before);
});
test('reload missing completion, navigation failure and destruction have named bounded failures and remove listeners',async t=>{
 const tick=clock(t);
 for(const failure of ['hang','navigation','destroyed']){
  const f=model(t),before=f.wc.listenerCount('did-fail-load');
  f.wc.reload=()=>{if(failure==='navigation')setImmediate(()=>f.wc.emit('did-fail-load',{},-2,'FAILED','http://127.0.0.1',true));if(failure==='destroyed')setImmediate(()=>f.wc.emit('destroyed'))};
  await assert.rejects(advance(f.lifecycle.reload('history reload '+failure,50),tick),error=>error.fixturePhase.name==='history reload '+failure);
  assert.equal(f.wc.listenerCount('did-finish-load'),0);assert.equal(f.wc.listenerCount('did-fail-load'),before);assert.equal(f.wc.listenerCount('destroyed'),1,'permanent lifecycle observer remains until finish');
 }
});
test('related fixtures put bundling, navigation and reload inside named lifecycle phases',()=>{
 for(const name of ['work-report-summary-r6','source-recheck','completed-card-delete']){
  const source=fs.readFileSync(path.join(__dirname,name+'.integration.cjs'),'utf8');
  assert.match(source,/lifecycle\.phase\('bundle production fixture'/);assert.match(source,/lifecycle\.phase\('load fixture route'/);
 }
 const source=fs.readFileSync(path.join(__dirname,'completed-card-delete.integration.cjs'),'utf8');assert.match(source,/lifecycle\.reload\('completed-card history reload'\)/);assert.doesNotMatch(source,/once\('did-finish-load'/);
});
test('failure to persist a successful lifecycle report cannot leave completed true',async t=>{
 const f=model(t);await f.lifecycle.read('true','last check');
 t.mock.method(fs,'writeFileSync',()=>{throw Error('report write failed')});
 await assert.rejects(f.lifecycle.finish(),/lifecycle report.*report write failed/);assert.equal(f.lifecycle.state.completed,false);
});

test('110 percent float32 display scale preserves exact native pixel rounding',async t=>{
 const f=model(t,{pixelScale:Math.fround(1.1)});await f.lifecycle.capture('custom 110 percent');
 assert.deepEqual(f.lifecycle.state.captures[0].expectedPixels,{width:1100,height:880});
});
