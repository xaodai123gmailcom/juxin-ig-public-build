const {test}=require('node:test');
const assert=require('node:assert/strict');
const {prepareVisibleFixture,captureShellFailure}=require('./visible-fixture.cjs');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
// Real wall-clock 40 ms budgets become flaky while other build suites run.
// Drive the actual deadline helpers with one shared controlled clock instead.
function clock(t){
 let now=0;
 t.mock.method(require('node:perf_hooks').performance,'now',()=>now);
 t.mock.timers.enable({apis:['setTimeout']});
 return ms=>{now+=ms;t.mock.timers.tick(ms)};
}
async function advanceToResult(promise,tick){
 let settled=false;
 const observed=promise.then(value=>({value}),error=>({error})).then(result=>{settled=true;return result});
 for(let turn=0;turn<250&&!settled;turn++){
  await new Promise(setImmediate);
  if(!settled)tick(5);
 }
 assert.equal(settled,true,'fixture operation did not respect its controlled deadline');
 const result=await observed;
 if(Object.hasOwn(result,'error'))throw result.error;
 return result.value;
}
function fixture({visible=false,minimized=false,documentVisible=false,stalled=false,recoverOnShow=false}={}){
 const calls=[];
 const state={visible,minimized,documentVisible,focused:false};
 const win={isDestroyed:()=>false,isVisible:()=>state.visible,isMinimized:()=>state.minimized,isFocused:()=>state.focused,
  restore(){calls.push('restore');state.minimized=false},show(){calls.push('show');state.visible=true;if(recoverOnShow)state.documentVisible=true},
  hide(){calls.push('hide');state.visible=false;state.documentVisible=false},
  focus(){calls.push('focus');state.focused=true},
  webContents:{isDestroyed:()=>false,focus(){calls.push('contents-focus')},executeJavaScript:async source=>{
   assert.ok(["document.visibilityState==='visible'","document.visibilityState==='hidden'"].includes(source));calls.push('read');
   return stalled?new Promise(()=>{}):source.includes("==='visible'")?state.documentVisible:!state.documentVisible;
  }}};
 return {win,state,calls};
}
test('hidden/minimized shell is restored before document visibility can pass',async t=>{
 const tick=clock(t);
 const f=fixture({minimized:true});
 const result=prepareVisibleFixture(f.win,{timeoutMs:500});
 assert.deepEqual(f.calls,['restore','show','focus','contents-focus']);
 f.state.documentVisible=true;
 assert.deepEqual(await advanceToResult(result,tick),{visible:true,minimized:false,focused:true,recovered:false});
});
test('native visible alone does not pass while renderer visibility stays hidden',async t=>{
 const tick=clock(t);
 const f=fixture({visible:true});
 await assert.rejects(advanceToResult(prepareVisibleFixture(f.win,{label:'review',timeoutMs:40}),tick),/review(?::)? document visibility/);
 assert.equal(f.state.documentVisible,false);
 assert.equal(f.calls.filter(x=>x==='hide').length,1,'bounded recovery cannot loop');
});
test('a responsive hidden renderer can recover only through a real native show transition',async t=>{
 const tick=clock(t);
 const f=fixture({visible:true,recoverOnShow:true});
 const result=await advanceToResult(prepareVisibleFixture(f.win,{timeoutMs:300}),tick);
 assert.equal(result.recovered,true);assert.equal(f.state.documentVisible,true);
 assert.equal(f.calls.filter(x=>x==='hide').length,1);assert.equal(f.calls.filter(x=>x==='show').length,1);
 assert.ok(f.calls.indexOf('hide')<f.calls.indexOf('show'));
});
test('ordinary asynchronous visibility propagation does not cycle a healthy window',async t=>{
 const tick=clock(t);
 const f=fixture({visible:true});
 const timer=setTimeout(()=>{f.state.documentVisible=true},20);
 try{assert.equal((await advanceToResult(prepareVisibleFixture(f.win,{timeoutMs:300}),tick)).recovered,false)}finally{clearTimeout(timer)}
 assert.equal(f.calls.includes('hide'),false);assert.equal(f.calls.includes('show'),false);
});
test('stalled visibility evaluation is bounded by main-process deadline',async t=>{
 const tick=clock(t);
 const f=fixture({visible:true,stalled:true});
 await assert.rejects(advanceToResult(prepareVisibleFixture(f.win,{timeoutMs:40}),tick),/timed out/);
 assert.equal(f.calls.filter(x=>x==='read').length,1);
});
test('destroyed or re-hidden native shell cannot produce a foreground pass',async()=>{
 const f=fixture({visible:true,documentVisible:true});
 f.win.isDestroyed=()=>true;
 await assert.rejects(prepareVisibleFixture(f.win),/destroyed/);
 assert.deepEqual(f.calls,[]);
 const other=fixture({visible:true,documentVisible:true});
 other.win.webContents.executeJavaScript=async()=>{other.state.minimized=true;return true};
 await assert.rejects(prepareVisibleFixture(other.win),/still hidden\/minimized/);
});
test('an already visible shell needs no hide/show transition',async()=>{
 const f=fixture({visible:true,documentVisible:true});
 assert.equal((await prepareVisibleFixture(f.win)).visible,true);
 assert.deepEqual(f.calls,['focus','contents-focus','read']);
});
test('failure report survives screenshot failure and preserves native and renderer evidence',async()=>{
 const output=fs.mkdtempSync(path.join(os.tmpdir(),'shell-evidence-'));
 try{
  const f=fixture({visible:false});f.win.getContentSize=()=>[1000,800];
  f.win.webContents.executeJavaScript=async()=>({visibility:'hidden',requests:[{visible:false}]});
  f.win.capturePage=async()=>{throw new Error('capture unavailable')};
  const original=new Error('surface never became visible');
  await captureShellFailure(f.win,{condition:'visible surface',error:original,outputDirectory:output,rendererErrors:['bridge failed']});
  const saved=JSON.parse(fs.readFileSync(path.join(output,'embedded-shell-failure.json'),'utf8'));
  assert.equal(saved.native.visible,false);assert.equal(saved.renderer.visibility,'hidden');
  assert.equal(saved.error,String(original));assert.deepEqual(saved.rendererErrors,['bridge failed']);
  assert.equal(fs.existsSync(path.join(output,'embedded-shell-failure.png')),false);
 }finally{fs.rmSync(output,{recursive:true,force:true})}
});

// Exercise the real native-input helper with event records; no renderer click or
// synthetic KeyboardEvent can satisfy its trusted-click checks.
function keyboardFixture({trusted=true,nativeFocused=true}={}){
 const calls=[],events=[];
 const wc={isDestroyed:()=>false,isFocused:()=>nativeFocused,
  focus(){calls.push('contents-focus')},
  sendInputEvent(event){
   calls.push(event);
   const type={keyDown:'keydown',char:'keypress',keyUp:'keyup'}[event.type];
   events.push({type,trusted,key:event.keyCode});
   if(event.type==='char')events.push({type:'click',trusted});
  },
  async executeJavaScript(source){
   calls.push(source);
   if(source==="document.visibilityState==='visible'")return true;
   if(source.includes('window.__nativeButtonKeyEvents.some'))return events.some(event=>event.type==='click'&&event.trusted);
   if(source==='window.__nativeButtonKeyEvents')return events;
   if(source.startsWith('({visibility:'))return {events};
   return true;
  }};
 const win={isDestroyed:()=>false,isVisible:()=>true,isMinimized:()=>false,isFocused:()=>nativeFocused,
  focus(){calls.push('window-focus')},webContents:wc};
 return {win,calls};
}
test('native button helper refocuses and sends full Enter and Space sequences without a DOM click',async()=>{
 const {activateNativeButton}=require('./native-keyboard-fixture.cjs');
 for(const key of ['Enter','Space']){
  const {win,calls}=keyboardFixture();
  await activateNativeButton(win,'button.wolf',key);
  const inputs=calls.filter(value=>value&&typeof value==='object');
  assert.deepEqual(inputs,[{type:'keyDown',keyCode:key},{type:'char',keyCode:key==='Enter'?'\r':' '},{type:'keyUp',keyCode:key}]);
  assert.ok(calls.indexOf('window-focus')<calls.indexOf(inputs[0]));
  assert.ok(calls.some(source=>typeof source==='string'&&source.includes('document.hasFocus()&&document.activeElement===')));
  assert.equal(calls.some(source=>typeof source==='string'&&(/\.click\(|dispatchEvent|new KeyboardEvent/.test(source))),false);
 }
});
test('native button helper refuses to send keys without native window focus',async()=>{
 const {activateNativeButton}=require('./native-keyboard-fixture.cjs');
 const {win,calls}=keyboardFixture({nativeFocused:false});
 await assert.rejects(activateNativeButton(win,'button.wolf','Enter'),/BrowserWindow must own native focus/);
 assert.equal(calls.some(value=>value&&typeof value==='object'),false);
});
test('synthetic or missing keyboard clicks cannot pass the native button assertion',async t=>{
 const tick=clock(t);
 const {activateNativeButton}=require('./native-keyboard-fixture.cjs');
 const {win,calls}=keyboardFixture({trusted:false});
 await assert.rejects(advanceToResult(activateNativeButton(win,'button.wolf','Enter',{timeoutMs:500}),tick),/trusted keyboard click/);
 assert.equal(calls.filter(value=>value&&typeof value==='object').length,3,'failed native action is not repeated');
});
