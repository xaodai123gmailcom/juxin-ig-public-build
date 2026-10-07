const assert=require('node:assert/strict');
const test=require('node:test');
function captureFixture({states,failures=[]}={}){
  const bounds={x:250,y:100,width:900,height:700},evidence={};let reads=0,captures=0,shows=0;
  // Logical time controls the small deadline boundaries. The test runner's
  // independent 5s wall-clock timeout still catches a genuinely hung test.
  let clock=0,nextTimer=0;const pending=new Map();
  const timers={setTimeout(fn,delay){const id=++nextTimer;pending.set(id,{fn,at:clock+delay});return id},clearTimeout(id){pending.delete(id)}};
  const fireNextTimer=()=>{const [id,timer]=[...pending].sort((a,b)=>a[1].at-b[1].at)[0]||[];assert.ok(timer,'capture has an outstanding deadline');pending.delete(id);clock=timer.at;timer.fn()};
  const ready={ready:'complete',visibility:'visible',width:900,height:700,painted:true};
  const image={isEmpty:()=>false};
  const wc={isDestroyed:()=>false,async capturePage(){const failure=failures[captures++];if(failure)throw failure;return image}};
  return {bounds,evidence,image,stats:()=>({reads,captures,shows}),win:{isDestroyed:()=>false,isVisible:()=>true,isMinimized:()=>false},pane:{getVisible:()=>true,getBounds:()=>({...bounds})},page:{view:{webContents:wc,getBounds:()=>({x:0,y:0,width:900,height:700})}},showSurface:async()=>{shows++},read:async(_wc,source)=>{assert.match(source,/requestAnimationFrame\(\(\)=>requestAnimationFrame/);return states?.[Math.min(reads++,states.length-1)]||ready},pause:async ms=>{clock+=ms},now:()=>clock,timers,fireNextTimer,pendingTimers:()=>pending.size};
}

test('native capture waits for real visibility and paint, retries UnknownVizError, and renews the surface lease',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./native-page-capture.cjs');
  const f=captureFixture({states:[{ready:'complete',visibility:'hidden',width:900,height:700,painted:false},{ready:'complete',visibility:'visible',width:900,height:700,painted:true}],failures:[new Error('UnknownVizError')]});
  assert.equal(await capturePaintedPage({...f,timeoutMs:500}),f.image);
  assert.equal(f.evidence.completed,true);assert.equal(f.evidence.attempts.length,3);
  assert.equal(f.stats().captures,2);assert.equal(f.stats().shows,5);
  assert.match(f.evidence.attempts[1].captureError,/UnknownVizError/);
  assert.deepEqual(f.evidence.attempts.map(x=>x.atMs),[0,50,100]);assert.equal(f.pendingTimers(),0);
});

test('native capture retains persistent compositor and never-visible failures within its deadline',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./native-page-capture.cjs');
  const f=captureFixture({failures:Array(100).fill(new Error('UnknownVizError'))});
  await assert.rejects(capturePaintedPage({...f,timeoutMs:25}),/did not become paint-ready.*UnknownVizError/);
  assert.equal(f.evidence.completed,false);assert.equal(f.stats().captures,1);assert.equal(f.now(),25);assert.equal(f.pendingTimers(),0);
  const hidden=captureFixture({states:[{ready:'complete',visibility:'hidden',width:900,height:700,painted:false}]});
  await assert.rejects(capturePaintedPage({...hidden,timeoutMs:25}),/did not become paint-ready/);
  assert.equal(hidden.stats().captures,0);assert.equal(hidden.evidence.completed,false);assert.equal(hidden.now(),25);
});

test('native capture does not retry wrong geometry, renderer failure, empty images, or other capture errors',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./native-page-capture.cjs');
  const wrong=captureFixture({states:[{ready:'complete',visibility:'visible',width:1280,height:900,painted:true}]});
  await assert.rejects(capturePaintedPage({...wrong,timeoutMs:500}),/actual DOM width/);assert.equal(wrong.stats().captures,0);
  const renderer=captureFixture();renderer.read=async()=>{throw new Error('renderer crashed')};
  await assert.rejects(capturePaintedPage({...renderer,timeoutMs:500}),/renderer crashed/);assert.equal(renderer.stats().captures,0);
  const empty=captureFixture();empty.image.isEmpty=()=>true;
  await assert.rejects(capturePaintedPage({...empty,timeoutMs:500}),/screenshot has pixels/);assert.equal(empty.stats().captures,1);
  const other=captureFixture({failures:[new Error('Unexpected capture failure')]});
  await assert.rejects(capturePaintedPage({...other,timeoutMs:500}),/Unexpected capture failure/);assert.equal(other.stats().captures,1);
});

test('native capture bounds a hung capture without overlapping screenshot requests',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./native-page-capture.cjs');
  const f=captureFixture();let captures=0;f.page.view.webContents.capturePage=()=>{captures++;return new Promise(()=>{})};
  const outcome=capturePaintedPage({...f,timeoutMs:25});
  for(let turn=0;turn<20&&!f.pendingTimers();turn++)await Promise.resolve();
  assert.equal(f.pendingTimers(),1);assert.equal(captures,1);f.fireNextTimer();
  await assert.rejects(outcome,/capture timed out/);
  assert.equal(captures,1);assert.equal(f.evidence.completed,false);assert.equal(f.now(),25);assert.equal(f.pendingTimers(),0);
});

