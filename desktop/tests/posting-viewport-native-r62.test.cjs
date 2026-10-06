/* These are fixture safety/source contracts, not a substitute for Electron. */
const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');
const {fixtureHtml,shellHtml,installOfflineSession}=require('./posting-viewport-native-r62.cjs');

test('native geometry fixture is inline-only and cannot publish',()=>{
  const html=fixtureHtml();
  assert.match(html,/connect-src 'none'/);assert.match(html,/form-action 'none'/);
  assert.match(html,/id="share" disabled/);assert.match(html,/never publishes/);
  assert.doesNotMatch(html,/<(?:script|img|iframe)[^>]*src\s*=|\bfetch\s*\(|XMLHttpRequest|WebSocket|<form/i);
  assert.match(shellHtml(),/left:250px;top:100px;width:900px;height:700px/);
});

test('offline session intercepts account navigation and rejects every external destination',async()=>{
  const handlers={},record={requests:[],external_requests:[]};let before;
  await installOfflineSession({webRequest:{onBeforeRequest(filter,fn){before=fn;assert.deepEqual(filter.urls,['http://*/*','https://*/*','ws://*/*','wss://*/*'])}},protocol:{async handle(scheme,fn){handlers[scheme]=fn}}},record);
  const check=url=>new Promise(resolve=>before({url},resolve));
  assert.deepEqual(await check('https://www.instagram.com/posting/'),{cancel:false});
  const response=await handlers.https(new Request('https://www.instagram.com/posting/'));
  assert.equal(response.status,200);assert.match(await response.text(),/OFFLINE posting viewport fixture/);assert.deepEqual(record.requests,['/posting/']);
  for(const url of ['https://example.invalid/','http://www.instagram.com/','wss://www.instagram.com/live','ws://127.0.0.1/'])assert.deepEqual(await check(url),{cancel:true});
  assert.equal((await handlers.https(new Request('https://example.invalid/'))).status,403);
  assert.equal((await handlers.https(new Request('https://www.instagram.com/',{method:'POST'}))).status,403);
  assert.equal((await handlers.http(new Request('http://www.instagram.com/'))).status,403);
  assert.equal(record.external_requests.length,6);
});

test('native proof observes production geometry and real connection cleanup without viewport setters',()=>{
  const source=fs.readFileSync(require.resolve('./posting-viewport-native-r62.cjs'),'utf8');
  assert.match(source,/getBoundingClientRect/);assert.match(source,/page\.view\.getBounds\(\)/);
  assert.match(source,/innerWidth===/);assert.match(source,/visualViewport\.width/);assert.match(source,/elementFromPoint/);
  assert.match(source,/new WebSocket\(host\.endpoint\(profile\)\)/);
  assert.match(source,/profile\.clients\.size===remaining/);
  assert.doesNotMatch(source,/set_viewport_size|setViewportSize|setDeviceMetricsOverride|\.clients\.(?:add|delete|clear)\(/);
  assert.match(source,/synthetic_offline:true,external_actions:\[\]/);
  assert.ok(source.indexOf('await host.stop();assert.equal(host.profiles.size,0')<source.indexOf('proof.verified=true'),'native cleanup precedes successful proof');
});

test('standalone guard starts the exact Electron ESM entry but never an imported helper',()=>{
  const {runInNewContext}=require('node:vm'),path=require('node:path');
  const filename=require.resolve('./posting-viewport-native-r62.cjs');
  const source=fs.readFileSync(filename,'utf8');
  const guard=source.slice(source.indexOf('function isStandaloneEntry('));
  assert.match(guard,/if\(isStandaloneEntry\(\)\)void main\(\)/);
  function starts({isMain=false,electronVersion='44.4.5',entry=filename}={}){
    let calls=0;const module={exports:{}},require=Object.assign(()=>{}, {main:isMain?module:{}});
    runInNewContext(guard,{module,require,path,__filename:filename,process:{versions:{electron:electronVersion},argv:['electron',entry]},main(){calls++},fixtureHtml(){},shellHtml(){},installOfflineSession(){},capturePaintedPage(){},run(){}});
    return calls;
  }
  assert.equal(starts(),1,'Electron ESM entry invokes main despite require.main mismatch');
  assert.equal(starts({isMain:true}),1,'ordinary CJS entry invokes main once');
  assert.equal(starts({entry:require.resolve('./embedded-browser.integration.cjs')}),0,'parent suite may import fixture without starting it');
  assert.equal(starts({entry:path.join(path.dirname(filename),'other',path.basename(filename))}),0,'same basename is not exact entry identity');
  assert.equal(starts({electronVersion:null}),0,'node helper import does not launch Electron');
  assert.equal(starts({entry:null}),0,'missing argv entry does not launch');
});

test('standalone startup emits an entry marker before Electron initialization',()=>{
  const source=fs.readFileSync(require.resolve('./posting-viewport-native-r62.cjs'),'utf8');
  const main=source.slice(source.indexOf('async function main()'));
  assert.ok(main.indexOf("console.log('CHECK native posting viewport standalone entry reached')")<main.indexOf("require('electron')"));
});

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
  const {capturePaintedPage}=require('./posting-viewport-native-r62.cjs');
  const f=captureFixture({states:[{ready:'complete',visibility:'hidden',width:900,height:700,painted:false},{ready:'complete',visibility:'visible',width:900,height:700,painted:true}],failures:[new Error('UnknownVizError')]});
  assert.equal(await capturePaintedPage({...f,timeoutMs:500}),f.image);
  assert.equal(f.evidence.completed,true);assert.equal(f.evidence.attempts.length,3);
  assert.equal(f.stats().captures,2);assert.equal(f.stats().shows,5);
  assert.match(f.evidence.attempts[1].captureError,/UnknownVizError/);
  assert.deepEqual(f.evidence.attempts.map(x=>x.atMs),[0,50,100]);assert.equal(f.pendingTimers(),0);
});

test('native capture retains persistent compositor and never-visible failures within its deadline',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./posting-viewport-native-r62.cjs');
  const f=captureFixture({failures:Array(100).fill(new Error('UnknownVizError'))});
  await assert.rejects(capturePaintedPage({...f,timeoutMs:25}),/did not become paint-ready.*UnknownVizError/);
  assert.equal(f.evidence.completed,false);assert.equal(f.stats().captures,1);assert.equal(f.now(),25);assert.equal(f.pendingTimers(),0);
  const hidden=captureFixture({states:[{ready:'complete',visibility:'hidden',width:900,height:700,painted:false}]});
  await assert.rejects(capturePaintedPage({...hidden,timeoutMs:25}),/did not become paint-ready/);
  assert.equal(hidden.stats().captures,0);assert.equal(hidden.evidence.completed,false);assert.equal(hidden.now(),25);
});

test('native capture does not retry wrong geometry, renderer failure, empty images, or other capture errors',{timeout:5000},async()=>{
  const {capturePaintedPage}=require('./posting-viewport-native-r62.cjs');
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
  const {capturePaintedPage}=require('./posting-viewport-native-r62.cjs');
  const f=captureFixture();let captures=0;f.page.view.webContents.capturePage=()=>{captures++;return new Promise(()=>{})};
  const outcome=capturePaintedPage({...f,timeoutMs:25});
  for(let turn=0;turn<20&&!f.pendingTimers();turn++)await Promise.resolve();
  assert.equal(f.pendingTimers(),1);assert.equal(captures,1);f.fireNextTimer();
  await assert.rejects(outcome,/capture timed out/);
  assert.equal(captures,1);assert.equal(f.evidence.completed,false);assert.equal(f.now(),25);assert.equal(f.pendingTimers(),0);
});

test('native capture defaults retain real production time and the independent native watchdog',()=>{
  const source=fs.readFileSync(require.resolve('./posting-viewport-native-r62.cjs'),'utf8');
  assert.match(source,/now=\(\)=>performance\.now\(\),timers=globalThis/);
  assert.match(source,/pause=ms=>new Promise\(resolve=>setTimeout\(resolve,ms\)\)/);
  assert.match(source,/createIntegrationWatchdog\(\{overallMs:300000/);
  assert.match(source,/await capturePaintedPage\(\{win,pane:first\.pane,page:composer,bounds:first\.bounds,showSurface:\(\)=>show\(posting,composer,first\.bounds\),evidence:proof\.screenshot_capture\}\)/);
});
