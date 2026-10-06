const {test}=require('node:test');
const assert=require('node:assert/strict');
const {runInNewContext}=require('node:vm');
const {prepareFixtureWindow,inside,near}=require('./floating-fixture.cjs');
const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');

function fixture(mode='no-frames'){
 const original={x:0,y:0,width:1516,height:1017},area={x:0,y:0,width:1024,height:728};
 let bounds={...original},minimum=[1500,960],frames=0,reads=0,release;
 const win={isDestroyed:()=>false,getBounds:()=>({...bounds}),getMinimumSize:()=>[...minimum],
  setMinimumSize:(w,h)=>{minimum=[w,h]},setBounds:b=>{bounds={...b,width:Math.max(b.width,minimum[0]),height:Math.max(b.height,minimum[1])}},getContentBounds:()=>({...bounds}),
  webContents:{getZoomFactor:()=>1,isDestroyed:()=>false,executeJavaScript:source=>{
   reads++;
   if(mode==='stalled')return new Promise(resolve=>{release=resolve});
   if(mode==='throw')throw new Error('renderer crashed');
   if(mode==='reject')return Promise.reject(new Error('renderer rejected'));
   if(mode==='invalid')return Promise.resolve({width:0,height:0,documentWidth:0});
   return Promise.resolve(runInNewContext(source,{innerWidth:bounds.width,innerHeight:bounds.height,
    document:{readyState:'complete',documentElement:{getBoundingClientRect:()=>({width:bounds.width})}},
    requestAnimationFrame:()=>{frames++}}));
  }} };
 return {win,screen:{getDisplayMatching:()=>({workArea:area})},original,area,
  state:()=>({bounds,minimum,frames,reads}),release:()=>release?.({width:800,height:600,documentWidth:800})};
}
test('occluded parent completes native preparation without any animation frame',async()=>{
 const f=fixture();const restore=await prepareFixtureWindow(f.win,f.screen,{rendererTimeoutMs:200});
 assert.ok(inside(f.state().bounds,f.area));assert.equal(f.state().frames,0);assert.equal(f.state().reads,1);
 await restore();assert.deepEqual(f.state().bounds,f.original);assert.deepEqual(f.state().minimum,[1500,960]);
});
test('old double-frame expression remains pending when frames are suspended',async()=>{
 const f=fixture();let settled=false;
 f.win.webContents.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))').then(()=>{settled=true});
 await new Promise(resolve=>setTimeout(resolve,30));assert.equal(settled,false);assert.equal(f.state().frames,1);
});
test('unresponsive renderer fails on main-process deadline and restores parent',async()=>{
 const f=fixture('stalled');
 await assert.rejects(prepareFixtureWindow(f.win,f.screen,{rendererTimeoutMs:30}),/timed out: floating parent layout/);
 assert.deepEqual(f.state().bounds,f.original);assert.deepEqual(f.state().minimum,[1500,960]);
 f.release();await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(f.state().bounds,f.original,'late renderer result cannot resize the restored parent');
});
test('renderer exceptions, rejection and invalid layout all restore bounds and minimum',async()=>{
 for(const mode of ['throw','reject','invalid']){
  const f=fixture(mode);await assert.rejects(prepareFixtureWindow(f.win,f.screen),/renderer crashed|renderer rejected|invalid layout/);
  assert.ok(near(f.state().bounds,f.original,0));assert.deepEqual(f.state().minimum,[1500,960]);
 }
});
test('reads reject destroyed renderers and keep the original evaluation error',async()=>{
 await assert.rejects(rendererFixtureRead({isDestroyed:()=>true},'true',{label:'destroyed page'}),/destroyed page/);
 await assert.rejects(rendererFixtureRead({executeJavaScript(){throw new Error('native read failed')}},'true'),/native read failed/);
});
test('condition waits for the real state rather than a frame or fixed delay',async()=>{
 let reads=0;const value=await waitRendererFixture({executeJavaScript:async()=>++reads===3?'committed':false},'condition',{timeoutMs:500,label:'route'});
 assert.equal(value,'committed');assert.equal(reads,3);
});
test('a false condition and a hung evaluation both fail within the overall deadline',async()=>{
 await assert.rejects(waitRendererFixture({executeJavaScript:async()=>false},'condition',{timeoutMs:30,label:'never committed'}),/never committed/);
 let reads=0;
 await assert.rejects(waitRendererFixture({executeJavaScript:()=>{reads++;return new Promise(()=>{})}},'condition',{timeoutMs:30,label:'hung read'}),/hung read/);
 assert.equal(reads,1);
});
test('invalid time budgets cannot silently disable deadlines',async()=>{
 for(const timeoutMs of [0,-1,NaN,Infinity]){
  assert.throws(()=>rendererFixtureRead({},'',{timeoutMs}),/Invalid/);
  await assert.rejects(waitRendererFixture({},'',{timeoutMs}),/Invalid/);
 }
});

test('late result cannot beat an overdue timer on a blocked main thread',async()=>{
 const {performance}=require('node:perf_hooks');
 await assert.rejects(rendererFixtureRead({executeJavaScript(){
  const until=performance.now()+20;while(performance.now()<until){}return true;
 }},'true',{timeoutMs:5,label:'late result'}),/timed out: late result/);
});
