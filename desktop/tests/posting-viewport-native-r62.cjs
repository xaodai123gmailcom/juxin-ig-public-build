/* Focused native Electron proof. All account HTTP(S) is served in-process;
   no Instagram account, credentials, publishing, likes or external API calls.
   Run with Electron, not node. Node may import the pure fixture helpers. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {rendererFixtureRead, waitRendererFixture} = require('./renderer-fixture.cjs');
const origin = 'https://www.instagram.com';
const owner = '11111111-1111-4111-8111-111111111111';
const measured = {x:250,y:100,width:900,height:700};

function fixtureHtml() {
  return `<!doctype html><html><head><meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'">
  <title>OFFLINE posting viewport fixture</title><style>
  *{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#152335;color:#fff;font:18px sans-serif}
  #composer{position:fixed;inset:12px;border:2px solid #8aa4c8;border-radius:12px;background:#243a55}
  h1{margin:24px;font-size:24px}#preview{position:absolute;inset:80px 24px 88px;background:linear-gradient(135deg,#4261c7,#cc783e);display:grid;place-items:center}
  footer{position:absolute;left:24px;right:24px;bottom:18px;display:flex;gap:16px;justify-content:flex-end}
  button{height:42px;width:120px;font:18px sans-serif}#next{background:#99d9ff;color:#10223c;border:0}
  </style></head><body><main id="composer" role="dialog" aria-label="Offline composer">
  <h1>Offline posting viewport</h1><div id="preview">Synthetic local fixture</div>
  <footer><button id="share" disabled>Share disabled</button><button id="next">Next</button></footer></main>
  <script>window.fixtureShares=0;window.share=()=>{window.fixtureShares++;throw Error('Offline geometry gate never publishes')};</script>
  </body></html>`;
}

function shellHtml() {
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
  <style>html,body{margin:0;background:#101823;color:white;font:18px sans-serif}#workspace{position:absolute;left:250px;top:100px;width:900px;height:700px;background:#1c2c40}</style>
  <h1>Offline native viewport gate</h1><div id="workspace"></div>`;
}

// Install before leaving about:blank. Unknown URLs are cancelled before a
// network request; even allowed account URLs use protocol.handle, never fetch.
async function installOfflineSession(session, record) {
  session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']}, (details, callback) => {
    const url = new URL(details.url);
    const allowed = url.protocol === 'https:' && url.origin === origin;
    if (!allowed) record.external_requests.push(details.url);
    callback({cancel:!allowed});
  });
  await session.protocol.handle('https', request => {
    const url = new URL(request.url);
    if (url.origin !== origin || request.method !== 'GET') {
      record.external_requests.push(request.url);
      return new Response('Offline fixture rejects this request', {status:403});
    }
    record.requests.push(url.pathname);
    return new Response(fixtureHtml(), {headers:{'content-type':'text/html; charset=utf-8'}});
  });
  await session.protocol.handle('http', () => new Response('Offline fixture only', {status:403}));
}

async function waitFor(check, label, timeoutMs=15000) {
  const started = performance.now();
  while (!check()) {
    if (performance.now()-started >= timeoutMs) throw new Error('Native viewport fixture timed out: '+label);
    await new Promise(resolve => setTimeout(resolve,20));
  }
}

async function connectTask(host, profile, sockets) {
  const {WebSocket} = require('ws');
  const expected = profile.clients.size+1;
  const socket = new WebSocket(host.endpoint(profile));
  sockets.add(socket);
  await new Promise((resolve,reject) => {
    const timer=setTimeout(()=>{socket.terminate();reject(new Error('Task socket open timed out'))},15000);
    socket.once('open',()=>{clearTimeout(timer);resolve()});
    socket.once('error',error=>{clearTimeout(timer);reject(error)});
  });
  await waitFor(()=>profile.clients.size===expected,'real task connection registered');
  // Confirm the real AccountCdpConnection speaks the account-scoped protocol.
  const version=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{socket.terminate();reject(new Error('Task CDP handshake timed out'))},15000);
    const done=raw=>{
      const message=JSON.parse(raw.toString());if(message.id!==1)return;
      clearTimeout(timer);socket.off('message',done);
      if(message.error)reject(new Error(message.error.message));else resolve(message.result);
    };
    socket.on('message',done);socket.send(JSON.stringify({id:1,method:'Browser.getVersion'}));
  });
  assert.match(version.product,/^Chrome\//);
  return socket;
}

async function disconnectTask(socket, profile, remaining, sockets) {
  socket.close();
  await waitFor(()=>profile.clients.size===remaining,'task connection disposal settled');
  sockets.delete(socket);
}

async function capturePaintedPage({win,pane,page,bounds,showSurface,evidence,timeoutMs=15000,read=rendererFixtureRead,pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=()=>performance.now(),timers=globalThis}) {
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid native capture deadline');
  const started=now(),wc=page.view.webContents;
  evidence.attempts=[];evidence.completed=false;
  const remaining=()=>{
    const ms=timeoutMs-(now()-started);
    if(ms<=0)throw new Error('Native posting screenshot did not become paint-ready: '+JSON.stringify(evidence.attempts.at(-1)));
    return ms;
  };
  for(;;){
    remaining();
    if(win.isDestroyed()||wc.isDestroyed())throw new Error('Native posting screenshot surface destroyed');
    // Renew the production three-second display lease. A transient compositor
    // rejection must not let the retry itself hide the native pane.
    await showSurface();
    const attempt={atMs:Math.round(now()-started),native:{windowVisible:win.isVisible(),minimized:win.isMinimized(),paneVisible:pane.getVisible(),paneBounds:pane.getBounds(),pageBounds:page.view.getBounds()}};
    evidence.attempts.push(attempt);
    assert.deepEqual(attempt.native.paneBounds,bounds,'capture keeps measured pane');
    assert.deepEqual(attempt.native.pageBounds,{x:0,y:0,width:bounds.width,height:bounds.height},'capture keeps posting native bounds');
    if(attempt.native.windowVisible&&!attempt.native.minimized&&attempt.native.paneVisible){
      // DOM geometry can be ready before Viz owns a copyable compositor
      // surface. Require real visible renderer frames; never fake visibility.
      attempt.renderer=await read(wc,`new Promise(resolve=>{const state=()=>({ready:document.readyState,visibility:document.visibilityState,width:innerWidth,height:innerHeight});const first=state();if(first.visibility!=='visible'||first.ready!=='complete'){resolve({...first,painted:false});return}requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({...state(),painted:true})))})`,{label:'native posting screenshot paint',timeoutMs:Math.min(2000,remaining())});
      const state=attempt.renderer;
      assert.equal(state.width,bounds.width,'capture keeps actual DOM width');
      assert.equal(state.height,bounds.height,'capture keeps actual DOM height');
      if(state.painted&&state.ready==='complete'&&state.visibility==='visible'){
        await showSurface();remaining();let timer;
        try{
          const image=await Promise.race([wc.capturePage(),new Promise((_,reject)=>{timer=timers.setTimeout(()=>reject(new Error('Native posting screenshot capture timed out')),Math.min(2000,remaining()))})]);
          remaining();assert.equal(image.isEmpty(),false,'native screenshot has pixels');
          evidence.completed=true;return image;
        }catch(error){
          attempt.captureError=String(error);
          // Only this observed compositor readiness error is retryable. Empty
          // images, renderer errors, geometry mismatches and hung captures fail.
          if(error?.message!=='UnknownVizError'&&error?.name!=='UnknownVizError')throw error;
          console.log('CHECK native posting screenshot compositor not ready; awaiting fresh paint');
        }finally{timers.clearTimeout(timer)}
      }
    }
    await pause(Math.min(50,remaining()));
  }
}

async function run({host,win,outputDirectory,proof}) {
  const sockets=new Set(),sessions=new Set(),records=[];
  const measure=async(width,height)=>{
    if(width)await rendererFixtureRead(win.webContents,`Object.assign(document.querySelector('#workspace').style,{width:'${width}px',height:'${height}px'});true`);
    const bounds=await rendererFixtureRead(win.webContents,`(()=>{const b=document.querySelector('#workspace').getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height}})()`);
    host.setWorkspaceBounds(bounds);
    const pane=win.contentView.children.find(view=>view.getBounds().x===bounds.x&&view.getBounds().y===bounds.y);
    assert.ok(pane,'real bounded native pane exists');assert.deepEqual(pane.getBounds(),bounds);
    return {bounds,pane};
  };
  const account=async(letter,name)=>{
    const id=`native:${letter.repeat(8)}-${letter.repeat(4)}-4${letter.repeat(3)}-8${letter.repeat(3)}-${letter.repeat(12)}`;
    const p=await host.ensure(id,owner,'',true,'OFFLINE VIEWPORT '+name);
    if(!sessions.has(p.session)){
      const record={profile:id,requests:[],external_requests:[]};records.push(record);
      await installOfflineSession(p.session,record);sessions.add(p.session);
    }
    return p;
  };
  const label=(p,page,extra={})=>host.control('label-task-page',{profile:p.id,target:page.targetId,role:'task',...extra});
  const show=async(p,page,bounds,readOnly=true)=>{
    const result=await host.control('show',{profile:p.id,target:page.targetId,bounds,grant:host.requestSurface(),read_only:readOnly});
    assert.equal(result.attached,true,'native surface attached');
  };
  const geometry=async(page,width,height,labelName,fitPane)=>{
    const wc=page.view.webContents;
    await waitRendererFixture(wc,`innerWidth===${width}&&innerHeight===${height}&&!!document.querySelector('#next')`,{label:labelName,timeoutMs:15000});
    const dom=await rendererFixtureRead(wc,`(()=>{const next=document.querySelector('#next'),r=next.getBoundingClientRect();return {width:innerWidth,height:innerHeight,visualWidth:visualViewport.width,visualHeight:visualViewport.height,next:{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom},hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.id,shares:window.fixtureShares,shareDisabled:document.querySelector('#share').disabled}})()`);
    const native=page.view.getBounds();assert.deepEqual(native,{x:0,y:0,width,height},labelName+' native bounds');
    assert.equal(dom.width,width);assert.equal(dom.height,height);assert.equal(dom.visualWidth,width);assert.equal(dom.visualHeight,height);
    assert.equal(dom.hit,'next','composer footer participates in actual DOM hit-testing');
    assert.equal(dom.shares,0);assert.equal(dom.shareDisabled,true);
    assert.ok(dom.next.x>=0&&dom.next.y>=0&&dom.next.right<=width&&dom.next.bottom<=height,'composer controls fit DOM viewport');
    if(fitPane)assert.ok(dom.next.right<=fitPane.width&&dom.next.bottom<=fitPane.height,'composer controls fit the measured native pane');
    proof.measurements.push({scenario:labelName,native,dom,...(fitPane?{measured_pane:fitPane}:{})});
    return dom;
  };
  const load=async(page,name)=>{await page.view.webContents.loadURL(origin+'/'+name+'/')};
  const unchanged=async(includeSibling=true)=>{
    if(includeSibling)await geometry(source,1280,900,'posting sibling retains baseline');
    await geometry(collectionPage,1280,900,'collection retains baseline');
    await geometry(nurturePage,1280,900,'nurture retains baseline');
  };
  let source,collectionPage,nurturePage;
  try {
    const first=await measure();assert.deepEqual(first.bounds,measured);proof.initial_measured_pane=first.bounds;
    const posting=await account('a','posting'),collection=await account('b','collection'),nurture=await account('c','nurture');
    const composer=[...posting.pages.values()][0];collectionPage=[...collection.pages.values()][0];nurturePage=[...nurture.pages.values()][0];
    const postingSocket=await connectTask(host,posting,sockets);
    await connectTask(host,collection,sockets);await connectTask(host,nurture,sockets);
    source=await host.newPage(posting,'about:blank');
    await label(posting,composer,{viewport_mode:'posting'});
    assert.deepEqual(composer.view.getBounds(),{x:0,y:0,width:900,height:700},'posting native dimensions established before fixture navigation');
    await load(composer,'posting');await load(source,'source');await load(collectionPage,'collection');await load(nurturePage,'nurture');
    await label(collection,collectionPage,{role:'source'});await label(nurture,nurturePage);
    await geometry(composer,900,700,'posting uses measured pane',first.bounds);await unchanged();
    await assert.rejects(label(posting,collectionPage,{viewport_mode:'posting'}));
    await assert.rejects(label(posting,composer,{role:'source',viewport_mode:'posting'}));
    await require('./visible-fixture.cjs').prepareVisibleFixture(win,{label:'native posting screenshot',timeoutMs:15000});
    await show(posting,composer,first.bounds);
    assert.equal(first.pane.getVisible(),true);
    await geometry(composer,900,700,'visible posting matches measured pane',first.bounds);
    proof.screenshot_capture={};
    const image=await capturePaintedPage({win,pane:first.pane,page:composer,bounds:first.bounds,showSurface:()=>show(posting,composer,first.bounds),evidence:proof.screenshot_capture});
    fs.writeFileSync(path.join(outputDirectory,'r62-posting-viewport-native.png'),image.toPNG());
    proof.screenshot={file:'r62-posting-viewport-native.png',size:image.getSize()};
    console.log('PASS native posting pane and DOM are 900x700; collection/nurture/sibling remain 1280x900');

    await host.control('hide',{profile:posting.id});assert.equal(first.pane.getVisible(),false);
    const second=await measure(800,620);
    await label(posting,composer);await label(posting,composer,{viewport_mode:'posting'});
    await geometry(composer,900,700,'posting frozen after hide resize and relabel');await unchanged();
    const oldUrl=composer.view.webContents.getURL();
    const replacement=await host.newPage(posting,'about:blank');
    // Creating a native page normally selects it. Establish an explicit manual
    // selection first, then prove that applying task metadata cannot change it.
    host.activate(posting,composer);const oldSelected=posting.selected;
    await label(posting,replacement,{viewport_mode:'posting'});await load(replacement,'replacement');
    assert.equal(posting.selected,oldSelected,'replacement label preserves manual selection');
    assert.equal(composer.view.webContents.getURL(),oldUrl,'replacement leaves old document intact');
    assert.equal(composer.postingViewport,undefined,'replacement retires old posting metadata');
    await geometry(composer,1280,900,'retired posting restores baseline');
    await geometry(replacement,800,620,'replacement snapshots fresh pane',second.bounds);
    await label(posting,replacement,{role:'source'});assert.equal(replacement.postingViewport,undefined);
    await geometry(replacement,1280,900,'role change clears posting dimensions');
    await label(posting,replacement,{viewport_mode:'posting'});await geometry(replacement,800,620,'posting relabel after role change',second.bounds);
    console.log('PASS posting freeze, fresh replacement and role-change reset verified in native DOM');

    const observer=await connectTask(host,posting,sockets);
    await disconnectTask(postingSocket,posting,1,sockets);
    assert.deepEqual(replacement.postingViewport,{width:800,height:620},'one remaining client retains posting geometry');
    await disconnectTask(observer,posting,0,sockets);
    for(const page of posting.pages.values())assert.equal(page.postingViewport,undefined,'last real disconnect clears every posting viewport');
    await assert.rejects(label(posting,replacement,{viewport_mode:'posting'}),'idle page cannot request posting geometry');
    const manual=await measure(760,560);host.activate(posting,replacement);
    await host.control('open-instagram',{profile:posting.id,owner,proxy:''});
    assert.equal(posting.selected,replacement.targetId);
    for(const page of posting.pages.values()){assert.equal(page.role,undefined);assert.equal(page.postingViewport,undefined)}
    await show(posting,replacement,manual.bounds,false);
    await geometry(replacement,760,560,'manual reopen follows current pane',manual.bounds);
    const collectorSocket=await connectTask(host,posting,sockets);await label(posting,replacement);
    await geometry(replacement,1280,900,'later non-posting task restores baseline');
    await disconnectTask(collectorSocket,posting,0,sockets);
    console.log('PASS real disconnect cleanup, manual reopen and later collection baseline');

    const oldGeneration=posting.generation,oldEndpoint=host.endpoint(posting);
    await host.closeProfile(posting);
    const reopened=await account('a','posting reopened');
    assert.ok(reopened.generation>oldGeneration);assert.notEqual(host.endpoint(reopened),oldEndpoint);
    const reopenedPage=[...reopened.pages.values()][0];assert.equal(reopenedPage.postingViewport,undefined);
    await connectTask(host,reopened,sockets);await label(reopened,reopenedPage,{viewport_mode:'posting'});await load(reopenedPage,'reopened');
    await geometry(reopenedPage,760,560,'new profile generation snapshots current pane',manual.bounds);await unchanged(false);
    for(const record of records){assert.ok(record.requests.length);assert.deepEqual(record.external_requests,[])}
    proof.offline_sessions=records;
    proof.scenarios={measuredPane:true,domHitTesting:true,frozenResize:true,replacement:true,roleReset:true,lastClientCleanup:true,manualReopen:true,nonPostingBaseline:true,profileGeneration:true};
    console.log('PASS profile replacement and fully offline account navigation');
    return proof;
  } finally {
    // Only this fixture's sockets; host.stop owns the actual page teardown.
    for(const socket of sockets)socket.terminate();
  }
}

async function main() {
  console.log('CHECK native posting viewport standalone entry reached');
  const {app,BrowserWindow,session}=require('electron');
  const os=require('node:os');const {pathToFileURL}=require('node:url');
  const {execFileSync}=require('node:child_process');
  const {createIntegrationWatchdog}=require('./integration-watchdog.cjs');
  const root=path.resolve(__dirname,'../..'),outputDirectory=path.join(root,'installer-output');
  fs.mkdirSync(outputDirectory,{recursive:true});
  const resultPath=path.join(outputDirectory,'r62-posting-viewport-native.json');
  const failurePath=path.join(outputDirectory,'r62-posting-viewport-native.failure.json');
  for(const file of [resultPath,failurePath,path.join(outputDirectory,'r62-posting-viewport-native.png')])fs.rmSync(file,{force:true});
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'juxin-posting-viewport-r62-'));
  app.setPath('userData',temp);app.setName('聚鑫国际');
  app.commandLine.appendSwitch('disable-background-networking');
  app.on('window-all-closed',()=>{});
  let host,win,exiting=false;
  const proof={schema:1,verified:false,synthetic_offline:true,external_actions:[],measurements:[],platform:process.platform,electron:process.versions.electron,chromium:process.versions.chrome};
  const fail=async error=>{
    if(exiting)return;exiting=true;watchdog.dispose();
    console.error('FAIL native posting viewport',error?.stack||error);
    fs.writeFileSync(failurePath,JSON.stringify({...proof,error:String(error?.stack||error),checked_at:new Date().toISOString()},null,2));
    // A failing renderer or native cleanup must still terminate with failure.
    const limit=setTimeout(()=>app.exit(1),5000);
    try{await host?.stop()}catch(cleanupError){console.error('FAIL owned fixture cleanup',String(cleanupError))}
    clearTimeout(limit);app.exit(1);
  };
  const watchdog=createIntegrationWatchdog({overallMs:300000,onTimeout:details=>void fail(new Error(JSON.stringify(details)))});
  watchdog.begin('posting-viewport-native-r62',270000);
  process.on('unhandledRejection',error=>void fail(error));
  process.on('uncaughtException',error=>void fail(error));
  try {
    Object.assign(proof,require('../../scripts/local_source_binding.cjs').collectSourceIdentity(root));
    if(process.env.GITHUB_SHA)assert.equal(proof.source_commit,process.env.GITHUB_SHA,'native proof must match the checked-out CI commit');
    await app.whenReady();
    // The shell and the read-only task input shield need no network either.
    session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']},(_details,callback)=>callback({cancel:true}));
    win=new BrowserWindow({show:true,width:1400,height:960,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
    win.setMinimumSize(1400,960);win.setContentSize(1400,960);
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(shellHtml()));
    const client=win.getContentSize();assert.ok(client[0]>=1400&&client[1]>=960,'native fixture requires the real desktop client area');
    const {EmbeddedBrowserHost}=await import(pathToFileURL(path.join(root,'dist-electron/embedded-browser.js')).href);
    host=new EmbeddedBrowserHost(()=>win);await host.start();host.attachWindow(win);
    await run({host,win,outputDirectory,proof});
    await host.stop();assert.equal(host.profiles.size,0,'all fixture-owned native profiles closed');
    proof.cleanup_verified=true;proof.timing=watchdog.finish();require('../../scripts/local_source_binding.cjs').assertSourceIdentity(proof,{root});proof.verified=true;proof.checked_at=new Date().toISOString();
    fs.writeFileSync(resultPath,JSON.stringify(proof,null,2));
    console.log('PASS native posting viewport proof:',resultPath);
    exiting=true;win.destroy();app.exit(0);
  } catch(error) {await fail(error)}
}

function isStandaloneEntry({isMain=require.main===module,electronVersion=process.versions.electron,entry=process.argv[1]}={}) {
  // Electron may import this CJS entry through its ESM loader in type:module
  // packages. Match this exact entry, never a parent suite importing helpers.
  return isMain||Boolean(electronVersion&&entry&&path.resolve(entry)===__filename);
}
module.exports={fixtureHtml,shellHtml,installOfflineSession,capturePaintedPage,run,isStandaloneEntry};
if(isStandaloneEntry())void main();
