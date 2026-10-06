const assert=require('node:assert/strict');
const test=require('node:test');
for(const name of ['work-report-summary-r6','nurture-reels-r6','posting-r6','core-route-stability','source-recheck']){
 const {closeFixtureServer}=require('./'+name+'.integration.cjs');
 test(name+' closes active and idle fixture connections',async()=>{
  let idle=0,active=0;await closeFixtureServer({close(cb){setImmediate(cb)},closeIdleConnections(){idle++},closeAllConnections(){active++}});
  assert.equal(idle,1);assert.equal(active,1);
 });
 test(name+' bounds a stalled close and preserves a close error',async()=>{
  await assert.rejects(closeFixtureServer({close(){},closeAllConnections(){}},10),/timed out/);
  await assert.rejects(closeFixtureServer({close(cb){cb(new Error('fixture failure'))},closeAllConnections(){}},10),/fixture failure/);
 });
}

test('native fixtures import the exact production global stylesheet order',()=>{
 const fs=require('node:fs'),path=require('node:path');
 const root=path.resolve(__dirname,'../..');
 const css=source=>[...source.matchAll(/import ["']([^"']+\.css)["'];/g)].map(m=>path.basename(m[1]));
 const expected=css(fs.readFileSync(path.join(root,'renderer/src/main.tsx'),'utf8'));
 assert.equal(expected.length,4);
 for(const file of ['desktop/tests/work-report-summary-r6.integration.cjs','renderer/tests/fixtures/posting-r6.tsx','renderer/tests/fixtures/standalone-nurture-r6.tsx','renderer/tests/fixtures/core-route-stability.tsx','renderer/tests/fixtures/source-recheck.tsx'])assert.deepEqual(css(fs.readFileSync(path.join(root,file),'utf8')),expected,file);
});

const {stableReportLayout,assertCardAlignment}=require('./work-report-summary-r6.integration.cjs');
test('report waits for stable resized geometry rather than one animation sample',async()=>{
 let reads=0;const result=await stableReportLayout(async()=>({viewportWidth:1000,sample:++reads<3?reads:3}),1000,{timeoutMs:200,stableMs:10,pollMs:1});
 assert.equal(result.sample,3);assert.ok(reads>3);
});
test('report wrong viewport cannot count as settled',async()=>{
 await assert.rejects(stableReportLayout(async()=>({viewportWidth:1598}),1000,{timeoutMs:20,stableMs:1,pollMs:1}),/did not stabilize/);
});
test('stable report overflow is not hidden by resize settling',async()=>{
 const bad={viewportWidth:1000,summary:{clientWidth:100,scrollWidth:200},cards:Array(5).fill({})};
 assert.equal(await stableReportLayout(async()=>bad,1000,{timeoutMs:100,stableMs:1,pollMs:1}),bad);
 assert.throws(()=>assertCardAlignment(bad),/summary has no horizontal overflow/);
});

const {runInNewContext}=require('node:vm');
const {waitWhatsAppGridLayout}=require('./whatsapp-layout.integration.cjs');
// Geometry/lifecycle doubles only: native rendering remains in the Electron
// integration gate. Execute its actual snapshot expression against each frame.
function gridFrame(width,ratio=.3,{leftWidth=(width-64)*ratio,rightWidth=width-64-leftWidth,scrollWidth=width,display='grid',rightLeft=64+leftWidth}={}){
 return {width,scrollWidth,display,rects:{
  nav:{left:0,right:64,width:64},
  '#contacts':{left:64,right:64+leftWidth,width:leftWidth},
  '#conversation':{left:rightLeft,right:rightLeft+rightWidth,width:rightWidth}
 }};
}
function gridRenderer(frames){
 let reads=0;
 return {isDestroyed:()=>false,reads:()=>reads,executeJavaScript:async source=>{
  const frame=frames[Math.min(reads++,frames.length-1)];
  return runInNewContext(source,{innerWidth:frame.width,
   document:{documentElement:{scrollWidth:frame.scrollWidth},querySelector:s=>({getBoundingClientRect:()=>frame.rects[s]})},
   getComputedStyle:()=>({display:frame.display})});
 }};
}
test('WhatsApp grid shrink rejects the old viewport and the intermediate resize frame',async()=>{
 const old=gridFrame(1150),intermediate=gridFrame(900,.3,{leftWidth:(1150-64)*.3});
 const oldCondition='document.documentElement.scrollWidth<=innerWidth&&Math.abs(document.querySelector("#contacts").getBoundingClientRect().width/(innerWidth-64)-.3)<.01';
 assert.equal(await gridRenderer([old]).executeJavaScript(oldCondition),true,'the former wait accepts the pre-resize frame');
 const wc=gridRenderer([old,intermediate,gridFrame(900)]);
 const settled=await waitWhatsAppGridLayout(wc,900,.3,{timeoutMs:1000});
 assert.equal(wc.reads(),3);assert.equal(settled.viewportWidth,900);assert.equal(settled.columnRatio,.3);
 assert.equal(settled.scrollWidth,900,'the assertion uses the accepted snapshot, not a later read');
});
test('WhatsApp grid restore also waits for the requested wide viewport and ratio',async()=>{
 const wc=gridRenderer([gridFrame(900),gridFrame(1150,.3,{leftWidth:(900-64)*.3}),gridFrame(1150)]);
 const settled=await waitWhatsAppGridLayout(wc,1150,.3,{timeoutMs:1000});
 assert.equal(wc.reads(),3);assert.equal(settled.viewportWidth,1150);assert.equal(settled.columnRatio,.3);
});
test('WhatsApp target viewport never hides sustained wrong ratio, overflow, non-grid or mismatched columns',async()=>{
 const cases=[
  ['wrong ratio',gridFrame(900,.5)],
  ['overflow',gridFrame(900,.3,{scrollWidth:1100})],
  ['non-grid',gridFrame(900,.3,{display:'flex'})],
  ['wrong conversation width',gridFrame(900,.3,{rightWidth:750})],
  ['columns fail to fill host',gridFrame(900,.3,{leftWidth:(900-64)*.3*.8,rightWidth:(900-64)*.7*.8})],
  ['non-adjacent columns',gridFrame(900,.3,{rightLeft:500})]
 ];
 for(const [name,frame] of cases){
  const wc=gridRenderer([frame]);
  await assert.rejects(waitWhatsAppGridLayout(wc,900,.3,{timeoutMs:60}),error=>{
   assert.match(error.message,/WhatsApp grid layout at 900px/);
   assert.match(error.message,/last geometry:.*"viewportWidth":900/);return true;
  },name);
 }
});
test('WhatsApp grid wait retains renderer failure and the bounded hung-read deadline',async()=>{
 await assert.rejects(waitWhatsAppGridLayout({executeJavaScript(){throw new Error('grid renderer failed')}},900,.3),/grid renderer failed/);
 await assert.rejects(waitWhatsAppGridLayout({executeJavaScript:()=>new Promise(()=>{})},900,.3,{timeoutMs:30}),/timed out: WhatsApp grid layout/);
});

const {stablePostingLayout,assertPostingLayout}=require('./posting-r6.integration.cjs');
function postingFrame(width=1280,rowHeights=[124,124,124]){
 const item=(height,id)=>({id,width:width-150,height,right:width-20});
 return {width,scroll:width,cards:Array.from({length:4},()=>item(78,null)),rows:rowHeights.map((h,i)=>item(h,['first','second','unknown'][i]))};
}
test('posting layout retains equal card and task-row height requirements with named diagnostics',()=>{
 assert.doesNotThrow(()=>assertPostingLayout(postingFrame(),1280));
 assert.throws(()=>assertPostingLayout(postingFrame(1280,[93,93,124]),1280),/rows\[2\] unknown at 1280px unequal height 124\/93/);
 const cards=postingFrame();cards.cards[2].height=110;
 assert.throws(()=>assertPostingLayout(cards,1280),/cards\[2\].*unequal height/);
 const overflow=postingFrame();overflow.scroll=1400;
 assert.throws(()=>assertPostingLayout(overflow,1280),/page overflow/);
});
test('posting resize settles the requested viewport rather than accepting a previous frame',async()=>{
 const frames=[postingFrame(1598),postingFrame(1280,[93,93,124]),postingFrame()];let reads=0;
 const result=await stablePostingLayout(async()=>frames[Math.min(reads++,frames.length-1)],1280,{timeoutMs:200,stableMs:4,pollMs:2});
 assert.ok(reads>=4);assert.deepEqual(result,postingFrame());
});
test('stable posting height mismatch is returned as evidence and still fails its assertion',async()=>{
 const bad=postingFrame(1280,[93,93,124]);
 const result=await stablePostingLayout(async()=>bad,1280,{timeoutMs:200,stableMs:4,pollMs:2});
 assert.deepEqual(result,bad);assert.throws(()=>assertPostingLayout(result,1280),/unequal height/);
});
test('posting wrong viewport retains last measured geometry in timeout diagnostics',async()=>{
 await assert.rejects(stablePostingLayout(async()=>postingFrame(1598),1280,{timeoutMs:15,stableMs:4,pollMs:2}),/did not settle at 1280px; last geometry:.*"width":1598/);
});
test('posting native diagnostics run before baseline and retry-layout assertions',()=>{
 const fs=require('node:fs'),path=require('node:path');
 const source=fs.readFileSync(require.resolve('./posting-r6.integration.cjs'),'utf8');
 assert.ok(source.indexOf("geometryEvidence('r63-posting-layout-'")<source.indexOf('assertPostingLayout(layout,width);'));
 assert.ok(source.indexOf("geometryEvidence('r63-posting-retry-layout-1280'")<source.indexOf('assertPostingLayout(retryLayout,1280)'));
 assert.match(source,/geometryEvidence\('r63-posting-failure'/);
 const css=fs.readFileSync(path.resolve(__dirname,'../../renderer/src/posting-workspace.css'),'utf8');
 assert.match(css,/\.inline-task-list\{display:grid;grid-template-columns:minmax\(0,1fr\);grid-auto-rows:minmax\(min-content,1fr\)\}/);
 assert.match(css,/\.task-failure-details p\{[^}]*white-space:normal;overflow:visible/);
});
for(const name of ['posting-r6','nurture-reels-r6'])test(name+' standalone entry handles Electron ESM import but not a parent suite require',()=>{
 const {isStandaloneEntry}=require('./'+name+'.integration.cjs');
 const entry=require.resolve('./'+name+'.integration.cjs');
 assert.equal(isStandaloneEntry({isMain:true,electronVersion:undefined,entry:undefined}),true);
 assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44.4.5',entry}),true);
 assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44.4.5',entry:require.resolve('./embedded-browser.integration.cjs')}),false);
 assert.equal(isStandaloneEntry({isMain:false,electronVersion:undefined,entry}),false);
 assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44.4.5',entry:undefined}),false);
});

const {assertHeldReceiptControls}=require('./nurture-reels-r6.integration.cjs');
test('held nurture receipt requires exactly the R6.4 locator and cleanup actions',()=>{
 const buttons=[{text:'定位关联采集任务',disabled:false},{text:'核验窗口清理',disabled:false}];
 assert.doesNotThrow(()=>assertHeldReceiptControls(buttons));
 for(const invalid of [[],buttons.slice(1),buttons.slice(0,1),[...buttons].reverse(),[...buttons,{text:'暂停',disabled:false}],[...buttons,{text:'继续',disabled:false}],[...buttons,{text:'停止',disabled:false}]]){
  assert.throws(()=>assertHeldReceiptControls(invalid),/held receipt exposes exactly/);
 }
});
test('active cleanup requires both receipt actions disabled and retains the busy label',()=>{
 const buttons=[{text:'定位关联采集任务',disabled:true},{text:'窗口清理处理中…',disabled:true}];
 assert.doesNotThrow(()=>assertHeldReceiptControls(buttons,{active:true}));
 for(const index of [0,1]){const invalid=structuredClone(buttons);invalid[index].disabled=false;assert.throws(()=>assertHeldReceiptControls(invalid,{active:true}));}
 assert.throws(()=>assertHeldReceiptControls([{text:'定位关联采集任务',disabled:true},{text:'核验窗口清理',disabled:true}],{active:true}));
});
test('posting native CAS expectation matches the actual offline transport refusal without queueing',async()=>{
 const fs=require('node:fs');
 const {transformSync}=require('esbuild');
 const {postingCasRejectionMessage}=require('./posting-r6.integration.cjs');
 const source=fs.readFileSync(require.resolve('../../renderer/tests/fixtures/posting-r6.tsx'),'utf8');
 // Exercise the same fixture request implementation; omit only React imports
 // and its document mount, which belong to the real Electron gate.
 const transport=source.replace(/^import .+;\r?\n/gm,'').replace(/^createRoot\(.+;\s*$/m,'');
 const window={setInterval(){return 0;}};
 runInNewContext(transformSync(transport,{loader:'tsx',format:'cjs'}).code,{window,structuredClone});
 const fixture=window.postingFixture,request=window.collectorCore.request;
 const review=structuredClone(fixture.state.jobs[0]);
 const body={action:'start',job_ids:[review.id],reviewed:[review]};
 for(const change of [row=>row.caption='changed after review',row=>row.queue_revision++,row=>row.status='queued',row=>row.window_held=true,row=>row.submitted_at='2026-10-05T00:00:00Z']){
  fixture.state.jobs[0]=structuredClone(review);change(fixture.state.jobs[0]);
  const before=structuredClone(fixture.state.jobs);
  await assert.rejects(request('/api/posting/command',{body}),error=>error.message===postingCasRejectionMessage);
  assert.deepEqual(structuredClone(fixture.state.jobs),before,'rejected review never changes task state');
 }
 assert.equal(fixture.commands.length,5,'each denied start remains an observable intent');
});
