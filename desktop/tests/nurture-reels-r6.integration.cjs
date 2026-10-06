/* Real production App / StudioWorkspace with isolated synthetic data.
   Windows gate: require('./nurture-reels-r6.integration.cjs')({win,host})
   Standalone: npx electron desktop/tests/nurture-reels-r6.integration.cjs */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {createFixtureLifecycle,closeFixtureServer,cleanupPreservingError,stableGeometry,assertGeometryGroups}=require('./fixture-lifecycle.cjs');
async function buildFixture(){return require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/standalone-nurture-r6.tsx')],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});}
const layoutScript=`(()=>{const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,client:n.clientWidth,scroll:n.scrollWidth}};return {width:innerWidth,height:innerHeight,document:document.documentElement.scrollWidth,summary:[...document.querySelectorAll('.nurture-summary>.formal-stat')].map(rect),windows:[...document.querySelectorAll('.nurture-window-card')].map(rect),settings:[...document.querySelectorAll('.nurture-settings-grid>.formal-field')].map(rect),metrics:[...document.querySelectorAll('.nurture-profile-counts>div')].map(rect),buttons:[...document.querySelectorAll('.standalone-nurture button')].map(rect)}})()`;
const nurtureGroups={summary:4,windows:7,settings:2,metrics:'nonempty',buttons:'nonempty'};
function stableNurtureLayout(evaluate,width,options={}){return stableGeometry(evaluate,layoutScript,width,{label:'Nurture layout',groups:nurtureGroups,height:1000,...options})}
function assertLayout(layout,width,height=1000){
  assert.equal(layout.height,height,'nurture viewport must match requested height');
  assert.equal(layout.width,width,'nurture viewport must match requested width');
  assertGeometryGroups(layout,nurtureGroups);
  assert.ok(layout.document<=layout.width+1,'page must not overflow horizontally');
  const near=(a,b,message)=>assert.ok(Math.abs(a-b)<=1,message+': '+a+' / '+b);
  for(const group of [layout.summary,layout.windows,layout.settings])for(const node of group){near(node.width,group[0].width,'equal widths');assert.ok(node.scroll<=node.client+1,'content fits grid');assert.ok(node.x>=0&&node.right<=layout.width+1,'grid stays on screen');}
  for(const node of [...layout.metrics,...layout.buttons]){assert.ok(node.scroll<=node.client+1,'metric/button content fits');assert.ok(node.x>=0&&node.right<=layout.width+1,'metric/button reachable');}
  for(let i=0;i<layout.summary.length;i++)for(let j=i+1;j<layout.summary.length;j++)if(Math.abs(layout.summary[i].y-layout.summary[j].y)<1)near(layout.summary[i].height,layout.summary[j].height,'equal card heights');
}
// R6.4 adds a narrow locator beside cleanup; neither is a running-task control.
function assertHeldReceiptControls(buttons,{active=false}={}){
  assert.deepEqual(buttons,[
    {text:'定位关联采集任务',disabled:active},
    {text:active?'窗口清理处理中…':'核验窗口清理',disabled:active},
  ],'held receipt exposes exactly the locator and cleanup controls; both are disabled during active cleanup');
}
async function runInWindow({win,host,lifecycle}){
  const built=await lifecycle.phase('bundle production fixture',buildFixture,{timeoutMs:30000});
  const server=http.createServer((request,response)=>{
    if(request.url==='/war-wolf.svg'){response.setHeader('content-type','image/svg+xml');response.end(fs.readFileSync(path.resolve(__dirname,'../../renderer/public/war-wolf.svg')));return;}
    const suffix=request.url==='/fixture.js'?'.js':request.url==='/fixture.css'?'.css':null;
    response.setHeader('content-type',suffix==='.js'?'text/javascript; charset=utf-8':suffix==='.css'?'text/css; charset=utf-8':'text/html; charset=utf-8');
    response.end(suffix?built.outputFiles.find(file=>file.path.endsWith(suffix)).contents:'<meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>');
  });
  const evaluate=lifecycle.read;
  const wait=lifecycle.wait;
  const button=(label,root='document')=>`[...${root}.querySelectorAll('button')].find(node=>node.textContent.trim()===${JSON.stringify(label)})`;
  const click=async(label,root)=>{await wait(`!!${button(label,root)}&&!${button(label,root)}.disabled`,'enabled '+label);await evaluate(`${button(label,root)}.click();true`,'click '+label);};
  const settled=()=>lifecycle.semantic('.standalone-nurture');
  const select=id=>evaluate(`document.querySelector('[data-window-id="${id}"] input').click();true`);
  const input=async(index,value)=>{await evaluate(`(()=>{const input=document.querySelectorAll('.nurture-settings input')[${index}];Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(String(value))});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);};
  const screenshot=async(name,selector)=>{if(selector)await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start'});true`);await settled();fs.mkdirSync('installer-output',{recursive:true});fs.writeFileSync(path.resolve('installer-output/'+name),(await lifecycle.capture(name,{width:requestedWidth,height:1000})).toPNG());};
  let requestedWidth=1598;
  const originalSize=win.getContentSize(),originalMinimum=win.getMinimumSize();
  let primaryError;
  try{
    await lifecycle.phase('listen on loopback',()=>new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)}));host?.hide();
    win.setMinimumSize(0,0);win.setContentSize(1598,1000);
    await lifecycle.phase('load nurture route',()=>win.loadURL('http://127.0.0.1:'+server.address().port+'/#/nurture'),{timeoutMs:30000});
    await lifecycle.visible('R6 nurture foreground');
    await wait('typeof nurtureFixture.releaseSnapshot===\'function\'','held initial studio load');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.nurture-summary strong')].map(n=>n.textContent)"),['读取中','读取中','读取中','读取中']);
    assert.equal(await evaluate("[...document.querySelectorAll('.nurture-window-card input')].every(n=>n.disabled)"),true);
    await evaluate('nurtureFixture.releaseSnapshot();true');
    await wait("document.querySelector('.standalone-nurture')?.getAttribute('aria-busy')==='false'",'loaded nurture');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.nurture-settings input')].map(n=>n.value)"),['5','0']);
    const text=await evaluate("document.querySelector('.standalone-nurture').textContent");
    assert.doesNotMatch(text,/养号模板|适应期|稳定期|维护期|最短停留|最长停留|点赞概率|Reels|开始时间（留空|每窗口轮次|相邻计划错开/);
    for(const id of ['locked','queued','paused','review','releasing'])assert.equal(await evaluate(`document.querySelector('[data-window-id="${id}"] input').disabled`),true,id+' blocked');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-window-id=free-a] .nurture-profile-counts dd')].map(n=>n.textContent)"),['1,234,567,890','0','345,678']);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-window-id=free-b] .nurture-profile-counts dd')].map(n=>n.textContent)"),['未读取','未读取','未读取']);
    await select('free-a');await select('free-b');await settled();
    await screenshot('r6-nurture-settings.png','.nurture-settings');
    for(const width of [1598,1000,800,560]){requestedWidth=width;lifecycle.state.requestedWidth=width;win.setContentSize(width,1000);const layout=await stableNurtureLayout(evaluate,width,{nativeSize:()=>win.getContentSize()});assertLayout(layout,width);}
    await screenshot('r6-nurture-narrow.png','.nurture-settings');
    requestedWidth=1598;win.setContentSize(1598,1000);assertLayout(await stableNurtureLayout(evaluate,1598,{nativeSize:()=>win.getContentSize()}),1598);
    // Invalid edits remain visible and cannot submit; defaults are never silently clamped.
    await input(0,'');assert.equal(await evaluate(`${button('启动养号')}.disabled`),true);
    await input(0,121);assert.equal(await evaluate(`${button('启动养号')}.disabled`),true);
    await input(0,5);await input(1,-1);assert.equal(await evaluate(`${button('启动养号')}.disabled`),true);
    await input(1,0);
    // Reservation arriving after selection removes that selection before start.
    await evaluate("nurtureFixture.state.jobs.push({id:'late-reservation',kind:'posting',profile_id:'free-b',status:'queued',created_at:'2026-10-02T18:00:00Z',result:{}});nurtureFixture.poll();true");
    await wait("document.querySelector('[data-window-id=free-b] input').disabled&&!document.querySelector('[data-window-id=free-b] input').checked",'late pending reservation removed');
    // Read failures preserve prior real values but disable new work, then recover.
    await evaluate('nurtureFixture.failSnapshot=true;nurtureFixture.poll();true');
    await wait("document.body.textContent.includes('养号数据读取失败')",'read failure shown');
    assert.equal(await evaluate(`${button('启动养号')}.disabled`),true);
    await screenshot('r6-nurture-read-error.png','.standalone-nurture');
    await evaluate('nurtureFixture.failSnapshot=false;nurtureFixture.poll();true');
    await wait(`!${button('启动养号')}.disabled`,'read recovered');
    await evaluate('nurtureFixture.failCommand=true;true');await click('启动养号');
    await wait("document.body.textContent.includes('窗口刚刚被其他任务占用')",'atomic backend rejection visible');
    assert.equal(await evaluate("document.querySelector('[data-window-id=free-a] input').checked"),true);
    // Two same-turn clicks create only one request while the command is pending.
    await wait(`!!${button('启动养号')}&&!${button('启动养号')}.disabled`,'failed command cleanup complete');
    const before=await evaluate('nurtureFixture.commands.length');
    await evaluate(`nurtureFixture.holdCommand=true;${button('启动养号')}.click();${button('启动养号')}.click();true`);
    await wait('typeof nurtureFixture.releaseCommand===\'function\'','pending start');
    assert.equal(await evaluate('nurtureFixture.commands.length'),before+1);
    const sent=await evaluate('nurtureFixture.commands.at(-1)');
    assert.deepEqual(sent.config,{minutes:5,concurrency:0});assert.deepEqual(sent.profile_ids,['free-a']);assert.equal(sent.kind,'nurture');assert.ok(sent.request_id);
    await evaluate('nurtureFixture.releaseCommand();true');
    const root="document.querySelector('[data-job-id=\"new-free-a\"]')";
    await wait(`!!${root}&&!${button('暂停',root)}.disabled`,'new job available');
    await click('暂停',root);await wait(`${root}.textContent.includes('已暂停')`,'paused status');
    await click('继续',root);await wait(`${root}.textContent.includes('执行中')`,'resumed status');
    await click('停止',root);
    // Recent results stay visible in the plan, including an acknowledged stop.
    // Terminal visibility must not keep stale controls or reserve a free window.
    await wait(`${root}?.querySelector('.formal-status')?.textContent==='已停止'&&document.querySelector('.standalone-nurture').getAttribute('aria-busy')==='false'`,'stopped recent result retained');
    const stopped=await evaluate("nurtureFixture.state.jobs.find(job=>job.id==='new-free-a')");
    assert.equal(stopped.status,'cancelled');assert.equal(stopped.result.nurture_actual_seconds,17);
    assert.equal(stopped.result.nurture_finished_at,'2026-10-02T18:00:00Z');
    assert.equal(Boolean(stopped.result.window_hold),false);
    assert.match(await evaluate(`${root}.textContent`),/已停止.*0 分 17 秒.*停止请求已处理/);
    assert.equal(await evaluate(`${root}.querySelectorAll('button').length`),0,'stopped recent result has no active controls');
    assert.equal(await evaluate("document.querySelector('[data-window-id=free-a] input').disabled"),false,'stopped unheld window is selectable');
    for(const id of ['free-b','releasing'])assert.equal(await evaluate(`document.querySelector('[data-window-id="${id}"] input').disabled`),true,id+' remains reserved');
    assert.equal(await evaluate("nurtureFixture.commands.filter(command=>command.job_id==='new-free-a'&&command.operation==='cancel').length"),1);
    await click('历史记录');
    await wait("!!document.querySelector('[data-job-id=completed-job]')",'history displayed');
    assert.match(await evaluate(`${root}.textContent`),/已停止.*0 分 17 秒/);
    assert.deepEqual(await evaluate("nurtureFixture.state.jobs.find(job=>job.id==='new-free-a')"),stopped,'stopped history preserves the exact result');
    assert.equal(await evaluate(`${root}.querySelectorAll('button').length`),0,'stopped history has no active controls');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-job-id=completed-job] .nurture-profile-counts dd')].map(n=>n.textContent)"),['120','0','34']);
    const historical=await evaluate("document.querySelector('[data-job-id=completed-job]').textContent");
    assert.match(historical,/@own.account.before/);assert.match(historical,/5 分 0 秒/);assert.doesNotMatch(historical,/99\/999|browse_seconds/);
    assert.match(await evaluate("document.querySelector('[data-job-id=cancelled-job]').textContent"),/已停止.*0 分 40 秒/);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-job-id=cancelled-job] .nurture-profile-counts dd')].map(n=>n.textContent)"),['未读取','未读取','未读取']);
    assert.match(await evaluate("document.querySelector('[data-job-id=failed-job]').textContent"),/失败.*无法确认当前登录账号/);
    await screenshot('r6-nurture-history.png','.nurture-tabs');
    await click('异常任务');
    await click('删除异常',"document.querySelector('[data-job-id=failed-job]')");
    await wait("!document.querySelector('[data-job-id=failed-job]')",'archived failure removed');
    await click('历史记录');
    await wait("document.querySelector('[data-job-id=failed-job]')?.textContent.includes('历史保留')",'archived failure still in history');
    assert.equal(await evaluate("document.querySelectorAll('[data-job-id=failed-job] button').length"),0);
    // A completed receipt with a durable hold is a separate, actionable state.
    // Supply only synthetic snapshots; backend/native proof tests own release.
    await wait("document.querySelector('.standalone-nurture').getAttribute('aria-busy')==='false'",'archive command refresh settled before held snapshot');
    await evaluate("(()=>{const job=nurtureFixture.state.jobs.find(job=>job.id==='completed-job');job.result={...job.result,window_hold:true,window_cleanup:{state:'lease_lost',last_error:'离线模拟：关闭待核验'}};nurtureFixture.poll();return true})()");
    await click('任务计划 / 正在执行');
    const held="document.querySelector('[data-job-id=completed-job]')";
    await wait(`!!${held}&&${held}.textContent.includes('已完成 · 清理待核验')&&document.querySelector('[data-window-id=free-a] input').disabled`,'held completed receipt remains visible and reserved');
    assert.equal(await evaluate("nurtureFixture.state.active_ids.includes('completed-job')"),false,'durable hold survives without an active worker');
    assertHeldReceiptControls(await evaluate(`[...${held}.querySelectorAll('button')].map(node=>({text:node.textContent.trim(),disabled:node.disabled}))`));
    await click('异常任务');
    await wait(`!!${held}&&!${button('核验窗口清理',held)}.disabled`,'held completed receipt actionable in exceptions');
    assert.equal(await evaluate("document.querySelector('[data-window-id=free-a] input').disabled"),true);
    await evaluate("nurtureFixture.state.active_ids.push('completed-job');nurtureFixture.poll();true");
    await wait(`!!${button('窗口清理处理中…',held)}&&${button('窗口清理处理中…',held)}.disabled`,'active cleanup prevents duplicate retry');
    assertHeldReceiptControls(await evaluate(`[...${held}.querySelectorAll('button')].map(node=>({text:node.textContent.trim(),disabled:node.disabled}))`),{active:true});
    await evaluate("(()=>{nurtureFixture.state.active_ids=nurtureFixture.state.active_ids.filter(id=>id!=='completed-job');const job=nurtureFixture.state.jobs.find(job=>job.id==='completed-job');job.result={...job.result,window_hold:false,window_cleanup:{state:'closed',confirmed_at:'2026-10-02T18:00:00Z'}};nurtureFixture.poll();return true})()");
    await wait(`!${held}&&!document.querySelector('[data-window-id=free-a] input').disabled`,'cleared hold leaves exceptions and releases only its window');
    await click('历史记录');await wait(`!!${held}`,'released receipt retains history');
    assert.match(await evaluate(`${held}.textContent`),/已完成.*窗口已关闭并释放.*5 分 0 秒/);
    assert.deepEqual(await evaluate(`[...${held}.querySelectorAll('.nurture-profile-counts dd')].map(node=>node.textContent)`),['120','0','34']);
    assert.equal(await evaluate(`${held}.querySelectorAll('button').length`),0);
    console.log('PASS R6 native standalone nurture: production App; two editable settings; legacy fixed parameters hidden; pending/busy windows; default 5/0; validation; single-flight start; read/command failures; pause/resume/stop with recent terminal result and released window; held completed receipt remains blocked/actionable until cleared; independent historical identity/counts/elapsed; equal responsive grids; four offline screenshots');
  }catch(error){primaryError=error;await lifecycle.diagnose(error);throw error}
  finally{await cleanupPreservingError(primaryError,[['restore dimensions',()=>{if(!win.isDestroyed()){win.setMinimumSize(...originalMinimum);win.setContentSize(...originalSize)}}],['HTTP server',()=>closeFixtureServer(server)]]);}
}
async function run({win:owner,host}={}){
  // A fresh nonpersistent session keeps synthetic account data isolated and gives
  // this fixture its own network guard without replacing the main app's policy.
  const {BrowserWindow}=require('electron');
  const isolated=new BrowserWindow({show:true,width:1598,height:1000,webPreferences:{sandbox:true,partition:'r6-nurture-'+require('node:crypto').randomUUID()}});
  const external=[];
  isolated.webContents.session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(details,callback)=>{
    const url=new URL(details.url);const local=url.protocol==='http:'&&url.hostname==='127.0.0.1';
    if(!local)external.push(details.url);callback({cancel:!local});
  });
  isolated.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  const lifecycle=createFixtureLifecycle(isolated,{label:'R6 nurture',entryMode:owner?'embedded':'standalone',sourceFile:__filename});
  let primaryError;
  try{await runInWindow({win:isolated,host,lifecycle});assert.deepEqual(external,[],'offline fixture never requests external services');}
  catch(error){primaryError=error;if(!lifecycle.state.primaryError)await lifecycle.diagnose(error)}
  finally{await lifecycle.finish(primaryError,[['destroy isolated window',()=>{if(!isolated.isDestroyed())isolated.destroy();assert.equal(isolated.isDestroyed(),true,'isolated fixture window is destroyed')}],['restore owner',()=>{if(owner&&!owner.isDestroyed()){owner.show();owner.focus()}}]]);}
}
module.exports=run;module.exports.buildFixture=buildFixture;module.exports.assertLayout=assertLayout;
function isStandaloneEntry({isMain=require.main===module,electronVersion=process.versions.electron,entry=process.argv[1]}={}){
  // Electron may load this CJS entry through ESM in this type:module package.
  // Exact entry identity starts only this fixture, never the embedding suite.
  return isMain||Boolean(electronVersion&&entry&&path.resolve(entry)===__filename);
}
if(isStandaloneEntry()){const{app}=require('electron');app.setPath('userData',fs.mkdtempSync(path.join(require('node:os').tmpdir(),'nurture-r6-')));const deadline=setTimeout(()=>{console.error('R6 native nurture timed out');app.exit(1);},120000);app.whenReady().then(async()=>{await run();clearTimeout(deadline);app.exit(0);}).catch(error=>{clearTimeout(deadline);console.error(error);app.exit(1);});}

module.exports.closeFixtureServer=closeFixtureServer;
module.exports.isStandaloneEntry=isStandaloneEntry;

module.exports.assertHeldReceiptControls=assertHeldReceiptControls;

module.exports.stableNurtureLayout=stableNurtureLayout;
module.exports.layoutScript=layoutScript;
