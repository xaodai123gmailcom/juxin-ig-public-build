/* Runs real Electron views against local fixture pages, never live accounts. */
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const {startOwnedWindowsProcess,stopOwnedProbe,bounded}=require('../../scripts/owned_process.cjs');
const { createIntegrationWatchdog } = require('./integration-watchdog.cjs');
const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const {prepareVisibleFixture,captureShellFailure}=require('./visible-fixture.cjs');
const { integrationFailureDetails, integrationFailureSummary, writeIntegrationFailure, pythonProbeFailure } = require('./integration-failure.cjs');
const {assertRetainedNavigation}=require('./navigation-contract-r65.cjs');
const temp = fs.mkdtempSync(path.join(os.tmpdir(),'juxin-embedded-test-'));
app.setPath('userData',temp);
// Match the packaged application, including its localized product name.
app.setName('聚鑫国际');
// Keep a prematurely closed shell from turning an unfinished test into exit 0.
app.on('window-all-closed',()=>{});
if(process.env.JUXIN_EMBEDDED_RESULT)fs.rmSync(process.env.JUXIN_EMBEDDED_RESULT,{force:true});
if(process.env.JUXIN_EMBEDDED_FAILURE)fs.rmSync(process.env.JUXIN_EMBEDDED_FAILURE,{force:true});
if(process.env.JUXIN_EMBEDDED_FAILURE){
  const output=path.dirname(process.env.JUXIN_EMBEDDED_FAILURE);
  fs.rmSync(path.join(output,'embedded-account-surface.json'),{force:true});
  fs.rmSync(path.join(output,'embedded-account-surface.png'),{force:true});
  fs.rmSync(path.join(output,'embedded-shell-failure.json'),{force:true});
  fs.rmSync(path.join(output,'embedded-shell-failure.png'),{force:true});
}
let activeProbe=null, exiting=false;
async function exitFailed(code,status,error) {
  if(exiting)return;exiting=true;
  watchdog.dispose();
  const details=integrationFailureDetails(status,error);
  try{writeIntegrationFailure(process.env.JUXIN_EMBEDDED_FAILURE,details)}catch(writeError){console.error('CHECK failure report write failed',String(writeError))}
  console.error('CHECK incomplete embedded verification',JSON.stringify(details));
  const child=activeProbe;
  // A failed termination request or wrapper exit is not proof of tree death.
  // Windows probes retain launch-time private-job ownership even after the
  // direct Python/venv parent has exited and descendants still hold resources.
  if(child) {
    const cleanup=await stopOwnedProbe(child);
    details.ownedProbeCleanup=cleanup;
    if(!cleanup.confirmedTreeEmpty)console.error('CHECK owned probe cleanup UNCONFIRMED',JSON.stringify(cleanup));
    try{writeIntegrationFailure(process.env.JUXIN_EMBEDDED_FAILURE,details)}catch(writeError){console.error('CHECK failure report write failed',String(writeError))}
  }
  console.error(integrationFailureSummary(details));
  app.exit(code);
}
const watchdog=createIntegrationWatchdog({overallMs:45*60*1000,onTimeout:details=>{void exitFailed(2,details)}});
watchdog.begin('electron-startup-and-fixtures',270000);
const runPythonProbe=async(python,script,extraEnv)=>{
  const status=watchdog.status();
  const remainingMs=Math.min(status.stageLimitMs-status.stageElapsedMs,status.overallLimitMs-status.totalElapsedMs);
  if(remainingMs<=0)throw new Error('Python probe has no remaining embedded execution budget');
  if(process.platform==='win32') {
    const probe=startOwnedWindowsProcess({python,executable:python,args:[path.resolve(__dirname,'../../backend/tests',script)],
      cwd:path.resolve(__dirname,'../..'),env:{PYTHONIOENCODING:'utf-8',PYTHONUTF8:'1',...extraEnv},
      timeoutMs:remainingMs,label:'Embedded existing stage/overall remainder: '+status.stage,
      logDirectory:path.join(temp,'owned-probes'),onData:d=>{if(process.stdout.writableLength<1024*1024)process.stdout.write(d)},
    });
    activeProbe=probe;
    console.log('CHECK Python probe supervisor launched',script,'supervisor launch pid',probe.supervisorLaunchPid);
    try {
      const result=await probe.done;
      console.log('CHECK Python probe owned launch exited',script,'target launch pid',result.receipt.launchTargetPid,'code',result.code);
      if(result.code!==0)throw pythonProbeFailure(script,result.code,result.output);
    } finally {if(activeProbe===probe&&probe.cleanupConfirmed===true)activeProbe=null}
    return;
  }
  // Existing non-Windows detached group path; no process-name enumeration.
  const child=spawn(python,[path.resolve(__dirname,'../../backend/tests',script)],{
    env:{...process.env,PYTHONIOENCODING:'utf-8',PYTHONUTF8:'1',...extraEnv},
    stdio:['ignore','pipe','pipe'],detached:true,windowsHide:true,
  });
  let output='';
  const exited=new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',resolve)});
  exited.catch(()=>{});
  const probe={stop:async()=>{
    if(!Number.isInteger(child.pid)||child.pid<=0)return{confirmedTreeEmpty:false,error:'Owned POSIX probe did not obtain a PID'};
    try{process.kill(-child.pid,'SIGKILL')}catch(error){if(error.code!=='ESRCH')return{confirmedTreeEmpty:false,error:String(error)}}
    try{
      await bounded(exited,10000,'Owned POSIX probe exit');
      const until=performance.now()+10000;
      while(performance.now()<until){
        try{process.kill(-child.pid,0)}catch(error){if(error.code==='ESRCH')return{confirmedTreeEmpty:true};throw error}
        await new Promise(resolve=>setTimeout(resolve,50));
      }
      return{confirmedTreeEmpty:false,error:'Owned POSIX process group still exists after cleanup deadline'};
    }catch(error){return{confirmedTreeEmpty:false,error:String(error)}}
  }};
  activeProbe=probe;
  child.stdout.on('data',d=>{output=(output+d).slice(-64000);process.stdout.write(d)});
  child.stderr.on('data',d=>{output=(output+d).slice(-64000);process.stderr.write(d)});
  try {
    const code=await bounded(exited,remainingMs,'Embedded Python existing stage deadline');
    const cleanup=await probe.stop();
    if(!cleanup.confirmedTreeEmpty)throw new Error('Owned Python cleanup unconfirmed: '+cleanup.error);
    console.log('CHECK Python probe exited',script,'code',code);
    if(code!==0)throw pythonProbeFailure(script,code,output);
  } finally {await probe.stop();if(activeProbe===probe)activeProbe=null}
};
app.whenReady().then(async()=>{
  const {accountUserAgent}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/account-user-agent.js')));app.userAgentFallback=accountUserAgent(app.userAgentFallback);
  console.log('CHECK Electron ready');
  const {EmbeddedBrowserHost}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/embedded-browser.js')).href);
  const {chromium,python}=require('./playwright-runtime.cjs').loadPlaywrightRuntime();
  console.log('PASS original Python Playwright driver loaded');
  const ui=await require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/profile-preview.tsx')],bundle:true,write:false,outfile:path.join(temp,'review-ui.js'),format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
  const accountUi=await require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/account-recovery.tsx')],bundle:true,write:false,outfile:path.join(temp,'account-ui.js'),format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'\"production\"'},logLevel:'silent'});
  let requestUserAgent='';
  const fixture=http.createServer((req,res)=>{
    requestUserAgent=req.headers['user-agent']||'';
    if(req.url==='/war-wolf.svg'){res.setHeader('Content-Type','image/svg+xml');res.end(fs.readFileSync(path.resolve(__dirname,'../../renderer/public/war-wolf.svg')));return;}
    if(req.url==='/account-ui') {res.setHeader('Content-Type','text/html');res.end('<meta charset=\"utf-8\"><link rel=\"stylesheet\" href=\"/account-ui.css\"><div id=\"root\"></div><script src=\"/account-ui.js\"></script>');return;}
    if(req.url==='/account-ui.js'||req.url==='/account-ui.css'){res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(accountUi.outputFiles.find(f=>f.path.endsWith(req.url.slice(1))).contents);return;}
    if(req.url==='/review-ui') { res.setHeader('Content-Type','text/html');res.end('<meta charset="utf-8"><link rel="stylesheet" href="/review-ui.css"><div id="root"></div><script src="/review-ui.js"></script>');return; }
    if(req.url==='/review-ui.js'||req.url==='/review-ui.css') { res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(ui.outputFiles.find(f=>f.path.endsWith(req.url.slice(1))).contents);return; }
    if(req.url==='/preview-denied') { res.writeHead(403,{'Content-Type':'text/html'});res.end('<title>4xx Client Error</title><h1>4xx Client Error</h1>');return; }
    if(req.url==='/preview-proxy') { res.setHeader('Content-Type','text/html');res.end('<title>4xx Client Error</title><h1>4xx Client Error</h1>');return; }
    res.setHeader('Content-Type','text/html');res.end(`<title>Account fixture</title><h1>${req.url}</h1><input id="upload" type="file" multiple><button id="action" onclick="document.querySelector('#result').textContent='collected'">Collect</button><output id="result"></output><button id="popup" onclick="window.open('/child','_blank')">Open child</button><input id="typing"><iframe src="/frame"></iframe>` .replace(req.url==='/frame'?/<iframe.*iframe>/:'NO_MATCH',''));
  });
  await new Promise(r=>fixture.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+fixture.address().port;
  console.log('CHECK local fixture ready');
  const preload=path.join(temp,'test-preload.cjs');
  fs.writeFileSync(preload,`const {contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('testSurface',input=>ipcRenderer.invoke('test:surface',input));contextBridge.exposeInMainWorld('testMenu',input=>ipcRenderer.invoke('test:menu',input));`);
  const win=new BrowserWindow({show:true,width:1500,height:960,webPreferences:{sandbox:true,preload}});
  // Native Windows creation may constrain a new window to the runner display.
  // This suite exercises a desktop layout with a 900x700 pane at (250,100).
  // Establish its real client area before issuing any production surface grant;
  // do not weaken the host's bounds validation to fit a test machine.
  const initialClientSize=win.getContentSize();
  win.setMinimumSize(1500,960);
  win.setContentSize(1500,960);
  await win.loadURL('data:text/html,<h1>App shell</h1>');
  const clientSize=win.getContentSize();
  console.log('CHECK fixture client area',JSON.stringify({initialClientSize,clientSize,windowBounds:win.getBounds()}));
  assert.ok(clientSize[0]>=1500&&clientSize[1]>=960,'Desktop fixture requires a real 1500x960 client area: '+JSON.stringify(clientSize));
  console.log('CHECK shell loaded');
  const shellView=win.contentView;
  const accountRendererErrors=[];
  const visibleShell=async label=>{
    try{const state=await prepareVisibleFixture(win,{label});console.log('CHECK visible shell',label,JSON.stringify(state))}
    catch(error){
      const output=process.env.JUXIN_EMBEDDED_FAILURE?path.dirname(process.env.JUXIN_EMBEDDED_FAILURE):temp;
      try{await captureShellFailure(win,{condition:label,error,outputDirectory:output,rendererErrors:accountRendererErrors})}
      catch(diagnosticError){console.error('CHECK shell evidence failed',String(diagnosticError))}
      throw error;
    }
  };
  const host=new EmbeddedBrowserHost(()=>win);await host.start();let taskColdStart=false;
  // Exercise the actual native shell before the long Python probes. Keep the
  // original overlay root and verify an actual hide/show visibility roundtrip.
  host.attachWindow(win);assert.equal(win.contentView,shellView);
  await visibleShell('native shell attachment');
  win.hide();
  await waitRendererFixture(win.webContents,"document.visibilityState==='hidden'",{label:'native shell hide',timeoutMs:30000});
  await visibleShell('native shell restore');
  const owner='11111111-1111-4111-8111-111111111111';
  watchdog.begin('native-browser-and-account-isolation',540000);
  const a=await host.ensure('native:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',owner,'',true);
  const b=await host.ensure('native:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',owner,'',true);
  console.log('PASS two real WebContentsView profiles created');
  assert.equal(win.contentView,shellView);await visibleShell('background account attachment');
  const browser=await chromium.connectOverCDP(host.endpoint(a));
  console.log('PASS Playwright connected');
  const ctx=browser.contexts()[0];assert.equal(ctx.pages().length,1);
  const page=ctx.pages()[0];await page.goto(base+'/a');
  assert.ok(requestUserAgent.includes('Chrome/'+process.versions.chrome));assert.doesNotMatch(requestUserAgent,/[^\x20-\x7e]|Electron|juxin/i);
  assert.equal(await page.evaluate(()=>navigator.userAgent),a.session.getUserAgent());
  console.log('PASS localized product user agent corrected in actual HTTP requests and page navigator');
  await page.evaluate(()=>{localStorage.setItem('account','A');document.cookie='account=A;path=/';});
  await page.click('#action');assert.equal(await page.textContent('#result'),'collected');
  await page.locator('#typing').fill('inside');assert.equal(await page.inputValue('#typing'),'inside');
  await page.locator('#upload').setInputFiles([{name:'one.txt',mimeType:'text/plain',buffer:Buffer.from('one')},{name:'two.txt',mimeType:'text/plain',buffer:Buffer.from('two')}]);
  assert.equal(await page.locator('#upload').evaluate(e=>e.files.length),2);
  await page.screenshot({path:path.join(temp,'actual-embedded-page.png')});
  assert.equal(await page.frameLocator('iframe').locator('h1').textContent(),'/frame');
  const cdp=await ctx.newCDPSession(page);assert.equal((await cdp.send('Target.getTargetInfo')).targetInfo.type,'page');await cdp.detach();
  const root=await browser.newBrowserCDPSession();
  await root.send('Browser.setPermission',{permission:{name:'notifications'},setting:'denied',origin:base});
  const targets=(await root.send('Target.getTargets')).targetInfos;
  assert.equal(targets.length,1);assert.equal(targets[0].url,base+'/a');
  const otherTarget=[...b.pages.keys()][0];
  await assert.rejects(root.send('Target.attachToTarget',{targetId:otherTarget,flatten:true}));
  await assert.rejects(root.send('Target.closeTarget',{targetId:otherTarget}));
  await assert.rejects(root.send('Target.createTarget',{url:'file:///etc/passwd'}));
  console.log('PASS tasks: navigate, click, type, multi-file upload, screenshot, iframe, scoped CDP');
  // A second simultaneous Playwright client must get independent event sessions.
  const second=await chromium.connectOverCDP(host.endpoint(a));
  assert.equal(await second.contexts()[0].pages()[0].textContent('h1'),'/a');await second.close();
  await page.click('#action');assert.equal(await page.textContent('#result'),'collected');
  const browserB=await chromium.connectOverCDP(host.endpoint(b)),pb=browserB.contexts()[0].pages()[0];
  await pb.goto(base+'/b');assert.equal(await pb.evaluate(()=>localStorage.getItem('account')),null);assert.equal((await browserB.contexts()[0].cookies()).length,0);
  await ctx.addCookies([{name:'imported',value:'yes',url:base}]);assert.equal((await ctx.cookies()).find(c=>c.name==='imported').value,'yes');
  assert.equal((await browserB.contexts()[0].cookies()).length,0);
  console.log('PASS simultaneous connections, account storage and target isolation');
  console.log('CHECK creating task page');const taskPage=await ctx.newPage();console.log('CHECK task page created');await taskPage.goto(base+'/task');console.log('CHECK closing task page');await taskPage.close();assert.equal(ctx.pages().length,1);console.log('CHECK temporary task page closed');
  console.log('CHECK clicking popup');const popupPromise=page.waitForEvent('popup');await page.click('#popup');console.log('CHECK popup click complete');const popup=await popupPromise;console.log('CHECK popup event received');await popup.waitForLoadState();assert.equal(popup.url(),base+'/child');
  assert.equal(BrowserWindow.getAllWindows().length,1);await popup.close();
  const grant=host.requestSurface();await host.control('show',{profile:a.id,bounds:{x:250,y:100,width:900,height:700},grant});
  const pane=win.contentView.children.at(-1);
  assert.equal(pane.children.at(-1).webContents.id,[...a.pages.values()][0].view.webContents.id);
  assert.equal(pane.getVisible(),true);
  assert.deepEqual(pane.getBounds(),{x:250,y:100,width:900,height:700});
  assert.deepEqual([...a.pages.values()][0].view.getBounds(),{x:0,y:0,width:1280,height:900});
  await host.control('hide',{profile:b.id});
  assert.equal(pane.getVisible(),true,'another profile cannot hide this account');
  assert.equal(host.hasSurfaceGrant(grant),true);
  const beforeTaskViewport=await page.evaluate(()=>({width:innerWidth,height:innerHeight}));
  host.setWorkspaceBounds({x:150,y:80,width:600,height:500});
  assert.deepEqual(await page.evaluate(()=>({width:innerWidth,height:innerHeight})),beforeTaskViewport,'changing foreground layout cannot resize active collection');
  await page.click('#action');assert.equal(await page.textContent('#result'),'collected');
  host.setWorkspaceBounds({x:250,y:100,width:900,height:700});
  [...a.pages.values()][0].view.webContents.focus();
  await host.control('hide',{profile:a.id});assert.equal(pane.getVisible(),false);
  assert.equal([...a.pages.values()][0].view.webContents.isFocused(),false);assert.equal(win.contentView,shellView);
  assert.equal((await host.control('show',{profile:a.id,bounds:{x:250,y:100,width:900,height:700},grant})).attached,false);
  await page.click('#action');assert.equal(await page.textContent('#result'),'collected');
  for(const profile of host.profiles.values())for(const item of profile.pages.values())assert.ok(!win.contentView.children.includes(item.view));
  const background=await ctx.newPage();await background.goto(base+'/preview-denied');
  for(const item of a.pages.values())assert.ok(!win.contentView.children.includes(item.view));
  assert.equal(win.contentView,shellView);
  assert.deepEqual(pane.getBounds(),{x:250,y:100,width:900,height:700});
  assert.ok(pane.children.every(v=>v.getBounds().x===0 && v.getBounds().width===1280));
  await background.close();assert.equal(pane.getVisible(),false);
  await visibleShell('account pane hide');
  console.log('PASS task page and popup stay embedded; hidden view remains automatable; stale surface blocked; background error pages confined to a separate pane');
  // Navigation must use the selected page rather than whichever tab was last.
  await page.goto(base+'/history-one');await page.goto(base+'/history-two');
  const selectedPage=[...a.pages.values()][0];
  const extra=await ctx.newPage();await extra.goto(base+'/unselected-tab');host.activate(a,selectedPage);
  const navigation={profile:a.id,owner,generation:a.generation,homeUrl:'https://www.instagram.com/'};
  await assert.rejects(host.control('manual-navigation',{...navigation,action:'back',generation:0}));
  assert.equal((await host.control('manual-navigation',{...navigation,action:'back'})).page_loaded,true);
  assert.equal(selectedPage.view.webContents.getURL(),base+'/history-one');await page.waitForURL(base+'/history-one');
  assert.equal(page.url(),base+'/history-one');assert.equal(extra.url(),base+'/unselected-tab');
  assert.equal((await host.control('manual-navigation',{...navigation,action:'forward'})).page_loaded,true);
  assert.equal(selectedPage.view.webContents.getURL(),base+'/history-two');await page.waitForURL(base+'/history-two');
  assert.equal(page.url(),base+'/history-two');
  await page.evaluate(()=>history.pushState({},'',location.pathname+'#section'));
  assert.equal((await host.control('manual-navigation',{...navigation,action:'back'})).page_loaded,true);
  assert.equal(selectedPage.view.webContents.getURL(),base+'/history-two');await page.waitForURL(base+'/history-two');
  assert.equal(page.url(),base+'/history-two');
  assert.equal((await host.control('manual-navigation',{...navigation,action:'forward'})).page_loaded,true);
  assert.equal(selectedPage.view.webContents.getURL(),base+'/history-two#section');await page.waitForURL(base+'/history-two#section');
  assert.equal(page.url(),base+'/history-two#section');
  assert.equal((await host.control('manual-navigation',{...navigation,action:'forward'})).navigated,false);
  await page.goto(base+'/preview-denied');
  assert.equal((await host.control('manual-navigation',{...navigation,action:'refresh'})).http_status,403);
  await extra.close();assert.equal(pane.getVisible(),false);
  console.log('PASS selected-page back/forward, same-document history, no-history and failed refresh; hidden task clicks remain operational');
  await browser.close();await browserB.close();
  const oldEndpoint=host.endpoint(a),oldGeneration=a.generation;await host.closeProfile(a);
  const reopened=await host.ensure(a.id,owner,'',true);assert.ok(reopened.generation>oldGeneration);assert.notEqual(host.endpoint(reopened),oldEndpoint);
  const again=await chromium.connectOverCDP(host.endpoint(reopened));const pa=again.contexts()[0].pages()[0];await pa.goto(base+'/reopen');
  assert.equal(await pa.evaluate(()=>localStorage.getItem('account')),'A');assert.equal((await again.contexts()[0].cookies()).find(c=>c.name==='imported').value,'yes');
  await again.close();
  console.log('PASS close/reopen retains login storage and invalidates old generation');
  // Intercept only in this local test; no request reaches real Instagram.
  reopened.session.webRequest.onBeforeRequest({urls:['https://www.instagram.com/*']},(details,callback)=>{
    const suffix=details.url.includes('/denied/')?'denied':details.url.includes('/proxy_error/')?'proxy':'ok';
    callback({redirectURL:base+'/preview-'+suffix});
  });
  const previewBody={profile:reopened.id,owner,generation:reopened.generation,username:'denied',action:'target'};
  await assert.rejects(host.control('profile-preview',{...previewBody,owner:'22222222-2222-4222-8222-222222222222'}));
  await assert.rejects(host.control('profile-preview',{...previewBody,generation:oldGeneration}));
  const denied=await host.control('profile-preview',previewBody);
  assert.equal(denied.page_loaded,false,JSON.stringify({result:denied,url:reopened.pages.get(reopened.previewTarget).view.webContents.getURL(),title:reopened.pages.get(reopened.previewTarget).view.webContents.getTitle()}));assert.equal(denied.http_status,403);assert.match(denied.message,/403/);
  const previewTarget=reopened.previewTarget;
  const proxyError=await host.control('profile-preview',{...previewBody,username:'proxy_error'});
  assert.equal(proxyError.page_loaded,false);
  const success=await host.control('profile-preview',{...previewBody,username:'valid_target'});
  assert.equal(success.page_loaded,true);assert.equal(reopened.previewTarget,previewTarget);
  const previewPage=reopened.pages.get(previewTarget);
  assert.equal(previewPage.view.webContents.session,reopened.session);
  assert.equal(await previewPage.view.webContents.executeJavaScript("localStorage.getItem('account')"),'A');
  assert.match(await previewPage.view.webContents.executeJavaScript('document.cookie'),/imported=yes/);
  assert.equal([...reopened.pages.values()][0].view.webContents.getURL(),base+'/reopen');
  assert.equal(BrowserWindow.getAllWindows().length,1);
  reopened.session.webRequest.onBeforeRequest(null);
  console.log('PASS target preview shares account storage, reuses its page, rejects stale/foreign sessions and reports HTTP/proxy errors');
  if (process.env.JUXIN_PYTHON) {
    watchdog.begin('python-worker-adapter',540000);
    await runPythonProbe(python,'embedded_worker_probe.py',{IGAC_EMBEDDED_BROWSER_URL:host.url,IGAC_EMBEDDED_BROWSER_TOKEN:host.token,JUXIN_FIXTURE_URL:base});
  }
  if(process.env.JUXIN_PYTHON) {
    watchdog.begin('python-cold-task-startup',540000);
    taskColdStart=await require('./task-cold-start-r62.cjs').run({host,python,runPythonProbe});
  }
  watchdog.begin('review-preview-ui',270000);
  win.webContents.on('console-message',(_event,level,message)=>{
    const entry=level&&typeof level==='object'?level:{level,message};
    if(entry.level==='error'||entry.level>=2){accountRendererErrors.push(String(entry.message));accountRendererErrors.splice(0,Math.max(0,accountRendererErrors.length-12));console.error('UI console',entry.message)}
  });
  win.webContents.on('did-fail-load',(_event,errorCode,errorDescription,validatedURL)=>{
    accountRendererErrors.push(`load ${validatedURL}: ${errorCode} ${errorDescription}`);
    accountRendererErrors.splice(0,Math.max(0,accountRendererErrors.length-12));
  });
  win.webContents.on('render-process-gone',(_event,details)=>{
    accountRendererErrors.push('render process gone: '+JSON.stringify(details));
    accountRendererErrors.splice(0,Math.max(0,accountRendererErrors.length-12));
  });
  const shellEval=async script=>{try{return await rendererFixtureRead(win.webContents,script,{label:'React shell expression',timeoutMs:30000})}catch(error){console.error('FAIL shell expression',script);throw error}};
  const shellFailure=async(script,error)=>{
    const output=process.env.JUXIN_EMBEDDED_FAILURE?path.dirname(process.env.JUXIN_EMBEDDED_FAILURE):temp;
    try{await captureShellFailure(win,{condition:script,error,outputDirectory:output,rendererErrors:accountRendererErrors})}
    catch(diagnosticError){console.error('CHECK shell evidence failed',String(diagnosticError))}
  };
  const waitShell=async script=>{
    try{return await waitRendererFixture(win.webContents,script,{label:'React UI check: '+script,timeoutMs:30000})}
    catch(error){await shellFailure(script,error);throw error}
  };
  host.hide();await win.loadURL(base+'/review-ui');
  await visibleShell('review-preview-ui entry');
  await waitShell("Boolean(document.querySelector('#open-target'))");
  await shellEval("document.querySelector('#open-target').click()");
  await waitShell("Boolean(document.querySelector('[role=alert]')?.textContent.includes('403'))");
  assert.deepEqual(await shellEval('window.fixture.commands[0]'),{action:'profile_preview',id:'idle',username:'target.user',preview_action:'target'});
  assert.equal(await shellEval("document.querySelector('option[value=busy]').disabled"),true);
  // getBoundingClientRect below flushes layout even when this shell is occluded.
  const panel=await shellEval("(()=>{const r=document.querySelector('[role=dialog]').getBoundingClientRect();return {width:r.width,height:r.height,x:r.x,y:r.y}})()");
  assert.ok(panel.width>700&&panel.height>500&&panel.x>=0&&panel.y>=0);
  await new Promise(r=>setTimeout(r,150));
  if(process.env.JUXIN_PREVIEW_SCREENSHOT)fs.writeFileSync(process.env.JUXIN_PREVIEW_SCREENSHOT,(await win.capturePage()).toPNG());
  // Reproduce a hidden shell during the login transition. The real React
  // surface must stay hidden, then renew its request when the shell is restored.
  const loginSurfaceStart=await shellEval('window.fixture.surfaces.length');
  win.hide();await waitShell("document.visibilityState==='hidden'");
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('登录 / 检查')).click()");
  await waitShell(`Boolean(document.querySelector('.account-browser-surface'))&&window.fixture.surfaces.length>${loginSurfaceStart}`);
  assert.equal(await shellEval(`window.fixture.surfaces.slice(${loginSurfaceStart}).some(s=>s.visible)`),false,'hidden preview cannot grant a visible surface');
  await visibleShell('review-preview-ui login restore');
  await waitShell(`window.fixture.surfaces.slice(${loginSurfaceStart}).some(s=>s.visible&&s.attached)`);
  await shellEval('window.fixture.status=200;window.fixture.locked=true');
  await waitShell("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('重新打开目标'))?.disabled");
  assert.equal(await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('登录 / 检查')).disabled"),true);
  await shellEval("document.querySelector('[aria-label=关闭目标主页]').click()");
  await waitShell("!document.querySelector('[role=dialog]')");
  assert.equal((await shellEval('window.fixture.surfaces.at(-1)')).visible,false);
  console.log('PASS actual React target preview: correct window, visible HTTP error, login control, locked controls and close/hide');
  assert.equal(BrowserWindow.getAllWindows().length,1);
  await shellEval("fixture.locked=false;fixture.listMode='empty';document.querySelector('#open-target').click()");
  await waitShell("document.querySelector('.profile-preview-empty')?.textContent.includes('当前账号列表中没有')");
  await shellEval("fixture.listMode='error';[...document.querySelectorAll('button')].find(b=>b.textContent.includes('刷新账号列表')).click()");
  await waitShell("document.querySelector('.profile-preview-empty')?.textContent.includes('暂时不可用')");
  assert.equal(await shellEval("document.querySelector('.profile-preview-empty').textContent.includes('添加')"),false);
  await shellEval("fixture.listMode='normal';[...document.querySelectorAll('button')].find(b=>b.textContent.includes('刷新账号列表')).click()");
  await waitShell("document.querySelector('select[aria-label=预览账号窗口]')?.value==='idle'");
  await shellEval("document.querySelector('[aria-label=关闭目标主页]').click()");
  console.log('PASS preview differentiates empty and failed account lists and refreshes newly available accounts');
  watchdog.begin('account-controls-and-translation-ui',540000);
  const {AccountSurfacePresenter}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/account-surface-presenter.js')).href);
  const presenter=new AccountSurfacePresenter(host,()=> 'fixture-owner',async body=>{
    if(await shellEval('fixture.locked||fixture.active')&&!body.read_only&&!body.interference_grant)throw new Error('task locked');
    return host.control('show',{...body,view_target:body.read_only?reopened.selected:body.view_target,profile:reopened.id});
  });
  ipcMain.handle('test:surface',(_event,input)=>presenter.update(win,input));
  host.hide();await win.loadURL(base+'/account-ui');
  await visibleShell('account-controls-and-translation-ui entry');
  const rightClick=`document.querySelector('[data-plan-id=plan]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:180,clientY:120}))`;
  const menuButton=text=>`[...document.querySelectorAll('[role=menuitem]')].find(b=>b.textContent.trim()===${JSON.stringify(text)})`;
  const openMenu=async()=>{await shellEval(rightClick);await waitShell("Boolean(document.querySelector('[role=menu]'))")};
  const choose=async text=>{await openMenu();await shellEval(`${menuButton(text)}.click()`)};
  // Collect the same React and native state when the first visible request is
  // missing as when an overlay later fails to restore the view.
  const captureAccountSurfaceFailure=async(checkpoint,cause)=>{
    const bounded=async(promise,label)=>{
      let timer;
      try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(label+' timed out')),2500)})])}
      finally{clearTimeout(timer)}
    };
    let renderer,native;
    try{renderer=await bounded(shellEval(`(()=>{const surface=document.querySelector('.account-browser-surface'),rect=surface?.getBoundingClientRect(),selected=document.querySelector('[data-plan-id=plan]'),style=surface&&getComputedStyle(surface),f=window.fixture;return {url:location.href,visibility:document.visibilityState,selected:selected?.getAttribute('aria-pressed'),status:selected?.querySelector('.account-sidebar-status')?.textContent?.trim(),alerts:[...document.querySelectorAll('[role=alert]')].map(e=>e.textContent?.trim()),dialogs:[...document.querySelectorAll('[role=dialog]')].map(d=>d.getAttribute('aria-label')),menu:Boolean(document.querySelector('[role=menu]')),translationOpen:Boolean(document.querySelector('[aria-label=聊天翻译设置]')),placeholder:document.querySelector('.account-browser-placeholder')?.textContent?.trim(),bridge:{accountSurface:typeof window.collectorCore?.accountSurface,testSurface:typeof window.testSurface},fixture:f?{opened:f.opened,locked:f.locked,active:f.active,independent:f.independent,snapshots:f.snapshots,requestCount:f.surfaces.length,visibleRequestCount:f.surfaces.filter(s=>s.visible===true).length}:null,surface:rect?{connected:surface.isConnected,display:style.display,visibility:style.visibility,x:rect.x,y:rect.y,width:rect.width,height:rect.height}:null,requests:f?.surfaces.slice(-20).map(s=>({id:s.id,surfaceId:s.surfaceId,documentUrl:s.documentUrl,visible:s.visible,readOnly:s.readOnly,attached:s.attached,display_state:s.display_state,message:s.message,error:s.error,bounds:s.bounds}))||[]}})()`),'renderer evidence')}
    catch(error){renderer={evidenceError:String(error)}}
    try{
      const profile=host.visible&&host.profiles.get(host.visible.profile);
      const page=profile?.pages.get(host.visible.target||profile.selected||'');
      native={windowVisible:win.isVisible(),windowMinimized:win.isMinimized(),windowFocused:win.isFocused(),paneVisible:pane.getVisible(),paneBounds:pane.getBounds(),clientSize:win.getContentSize(),activePlan:presenter.currentPlanId(),visible:host.visible?{profile:host.visible.profile,target:host.visible.target,readOnly:host.visible.readOnly,leaseRemainingMs:host.visible.deadline-Date.now()}:null,attachedTarget:host.attached?.targetId,selectedTarget:profile?.selected,pageUrl:page?.view.webContents.getURL(),pageStatus:page?host.pageDisplayStatus(page):null};
    }catch(error){native={paneVisible:false,evidenceError:String(error)}}
    const requests=renderer.requests||[];
    const reason=renderer.evidenceError?'renderer evidence unavailable':!native.windowVisible||native.windowMinimized?'test window is hidden or minimized':renderer.visibility==='hidden'?'account document is hidden':renderer.bridge?.accountSurface!=='function'?'account surface bridge is missing':!renderer.surface?.connected?'account surface element is not mounted':renderer.fixture?.requestCount===0?'React has made zero account surface requests':!renderer.fixture?.visibleRequestCount?'React sent only visible=false surface requests':requests.at(-1)?.error?'surface bridge rejected the last request':requests.at(-1)?.attached===false?'native presenter declined the last surface request':'native pane is hidden after a visible=true surface request';
    const evidence={checkpoint,reason,cause:cause?String(cause):null,renderer,native,rendererErrors:accountRendererErrors};
    console.error('CHECK account surface failure',reason,JSON.stringify({checkpoint,windowVisible:native.windowVisible,windowMinimized:native.windowMinimized,windowFocused:native.windowFocused,documentVisibility:renderer.visibility,selected:renderer.selected,status:renderer.status,surface:renderer.surface,fixture:renderer.fixture,alerts:renderer.alerts,lastRequest:requests.at(-1),rendererErrors:accountRendererErrors}));
    const output=process.env.JUXIN_EMBEDDED_FAILURE?path.dirname(process.env.JUXIN_EMBEDDED_FAILURE):temp;
    fs.mkdirSync(output,{recursive:true});
    try{const report=path.join(output,'embedded-account-surface.json');fs.writeFileSync(report,JSON.stringify(evidence,null,2)+'\n');console.error('CHECK account surface report',report)}catch(error){console.error('CHECK account surface report failed',String(error))}
    try{const screenshot=path.join(output,'embedded-account-surface.png');const image=await bounded(win.capturePage(),'native screenshot');fs.writeFileSync(screenshot,image.toPNG());console.error('CHECK account surface screenshot',screenshot)}catch(error){console.error('CHECK account surface screenshot failed',String(error))}
    assert.fail('Account page unavailable after '+checkpoint+': '+reason+'; '+JSON.stringify(evidence));
  };
  // A dismissed React overlay is only the first half of a native-view restore.
  const waitForAccountPane=async checkpoint=>{
    const deadline=Date.now()+4000;
    while(!pane.getVisible()&&Date.now()<deadline)await new Promise(r=>setTimeout(r,50));
    if(!pane.getVisible())await captureAccountSurfaceFailure(checkpoint);
  };
  const openCreation=async label=>{
    await shellEval("document.querySelector('[aria-label=添加窗口]').click()");
    await waitShell("Boolean(document.querySelector('[role=dialog][aria-label=选择创建方式]'))");
    assert.equal(pane.getVisible(),false,'creation choices cover the account view');
    assert.deepEqual(await shellEval("[...document.querySelectorAll('.account-create-options strong')].map(e=>e.textContent)"),['单个窗口创建','批量窗口创建']);
    assert.equal(await shellEval("Boolean(document.querySelector('[aria-label=新建窗口],[aria-label=批量创建窗口]'))"),false);
    await shellEval(`[...document.querySelectorAll('.account-create-options button')].find(e=>e.querySelector('strong')?.textContent===${JSON.stringify(label)}).click()`);
    await waitShell(`Boolean(document.querySelector('[role=dialog][aria-label=${label==='单个窗口创建'?'新建窗口':'批量创建窗口'}]'))`);
    assert.equal(pane.getVisible(),false,'creation editor covers the account view');
  };
  const waitForAccountCondition=async(script,checkpoint)=>{
    const deadline=Date.now()+8000;
    while(Date.now()<deadline){
      try{if(await shellEval(script))return}
      catch(error){await captureAccountSurfaceFailure(checkpoint,error)}
      await new Promise(r=>setTimeout(r,50));
    }
    await captureAccountSurfaceFailure(checkpoint,new Error('Account UI condition timed out after 8 seconds: '+script));
  };
  await waitForAccountCondition("Boolean(document.querySelector('[data-plan-id=plan]'))",'initial account view: plan row mounted');
  await waitForAccountCondition('fixture.surfaces.some(s=>s.visible)','initial account view: first visible=true request');
  await waitForAccountPane('initial account view: native pane attached');
  assert.equal(await shellEval("Boolean(document.querySelector('.account-window-toolbar'))"),false);
  await openMenu();assert.deepEqual(await shellEval("[...document.querySelectorAll('[role=menuitem]')].map(b=>b.textContent.trim())"),['刷新','上一页','下一页','关闭','编辑','删除']);
  assert.equal(pane.getVisible(),false);
  await shellEval(`${menuButton('刷新')}.click()`);
  await waitShell("Boolean(document.querySelector('[role=alert]')?.textContent.includes('403'))");
  await openMenu();assert.equal(await shellEval(`${menuButton('刷新')}.disabled`),false);
  await shellEval(`fixture.throwError=true;${menuButton('刷新')}.click()`);
  await waitShell("Boolean(document.querySelector('[role=alert]')?.textContent.includes('连接失败'))");
  assert.ok(await shellEval('fixture.snapshots>=3'));
  await shellEval('fixture.throwError=false;fixture.fail=false');
  await choose('上一页');await waitShell("fixture.commands.at(-1).action==='back' && !!document.querySelector('[role=status]')");
  await choose('下一页');await waitShell("fixture.commands.at(-1).action==='forward' && !!document.querySelector('[role=status]')");
  // All account surface calls now pass through the production main presenter
  // and a real WebContentsView, rather than a mocked attached=true response.
  for(const route of ['collection','review','nurture']) {
    await shellEval(`document.querySelector('a[href="#/${route}"]').click()`);
    await waitShell("Boolean(document.querySelector('#other-page'))");
    assert.equal(pane.getVisible(),false,route);
    await previewPage.view.webContents.loadURL(base+'/late-load-'+route);
    assert.equal(pane.getVisible(),false,'late background load '+route);
    const oldUrl=base+'/account-ui#/accounts';
    assert.equal((await presenter.update(win,{id:'plan',visible:true,documentUrl:oldUrl,bounds:{x:200,y:80,width:900,height:700}})).attached,false);
    assert.equal(pane.getVisible(),false);
    await shellEval(`document.querySelector('a[href="#/accounts"]').click()`);
    await waitShell("Boolean(document.querySelector('[data-plan-id=plan]'))");
    const deadline=Date.now()+4000;while(!pane.getVisible()&&Date.now()<deadline)await new Promise(r=>setTimeout(r,50));
    assert.equal(pane.getVisible(),true,'return to accounts '+route);
  }
  // Shared unread count survives route changes; actual UI edit/Cookie/drag paths.
  await waitShell("document.querySelector('.total-unread-badge')?.textContent==='1' && document.querySelector('[data-plan-id=plan] .account-platform-icon')?.title.includes('未读消息：1')");
  await shellEval('fixture.unread=123');await waitShell("document.querySelector('.total-unread-badge')?.textContent==='123'");
  await waitShell("document.querySelector('[data-plan-id=plan] .account-platform-icon')?.title.includes('未读消息：123')");
  const compact=await shellEval("(()=>{const row=document.querySelector('[data-plan-id=plan]'),icon=row.querySelector('.account-platform-icon');return {name:row.querySelector('strong').textContent,status:row.querySelector('.account-sidebar-status').textContent.trim(),text:row.textContent,details:icon.title,secondary:row.querySelectorAll('.account-window-time').length,height:row.getBoundingClientRect().height}})()");
  assert.equal(compact.name,'恢复验证账号');assert.equal(compact.status,'已打开');assert.equal(compact.secondary,0);assert.ok(compact.height<=70);
  assert.equal(compact.text.includes('test.user'),false);assert.equal(compact.text.includes('创建'),false);
  assert.equal(await shellEval("document.querySelector('[data-plan-id=plan] .account-platform-icon .window-unread-badge').textContent"),'123');
  for(const detail of ['备注：暂无备注','平台账号：test.user','创建时间：','最后打开时间：','未读消息：123'])assert.ok(compact.details.includes(detail),detail);
  fs.writeFileSync(path.join(temp,'v75-account-badges.png'),(await win.capturePage()).toPNG());console.log('CHECK badge screenshot',path.join(temp,'v75-account-badges.png'));
  const badgeStyle=await shellEval("(()=>{const e=document.querySelector('.total-unread-badge'),s=getComputedStyle(e);return {font:s.fontSize,color:s.color,background:s.backgroundColor,width:e.getBoundingClientRect().width}})()");
  assert.equal(badgeStyle.font,'14px');assert.equal(badgeStyle.color,'rgb(255, 255, 255)');assert.ok(badgeStyle.width>=25);
  // Collapse changes the actual native viewport, preserving the loaded page.
  const expandedBounds=pane.getBounds(),sidebarPageUrl=previewPage.view.webContents.getURL();
  await previewPage.view.webContents.executeJavaScript("document.querySelector('#typing').value='preserve unsent sidebar draft'");
  await shellEval("document.querySelector('[aria-label=收起账号侧栏]').click()");
  await waitShell("Boolean(document.querySelector('.account-sidebar-collapsed')) && localStorage.getItem('account-sidebar-collapsed')==='true'");
  const compactBounds=await shellEval("(()=>{const r=document.querySelector('.account-browser-surface').getBoundingClientRect();return {x:r.x,width:r.width}})()");
  const resizeDeadline=Date.now()+4000;
  while(Math.abs(pane.getBounds().width-compactBounds.width)>2&&Date.now()<resizeDeadline)await new Promise(r=>setTimeout(r,25));
  assert.ok(pane.getBounds().width>expandedBounds.width+90);assert.ok(Math.abs(pane.getBounds().x-compactBounds.x)<=2);
  assert.equal(pane.getVisible(),true);assert.equal(previewPage.view.webContents.getURL(),sidebarPageUrl);
  assert.equal(await previewPage.view.webContents.executeJavaScript("document.querySelector('#typing').value"),'preserve unsent sidebar draft');
  assert.equal(await shellEval("getComputedStyle(document.querySelector('[data-plan-id=plan] .account-sidebar-identity')).display"),'none');
  assert.equal(await shellEval("document.querySelector('[data-plan-id=plan] .account-platform-icon .window-unread-badge').textContent"),'123','incoming badge remains in the collapsed sidebar');
  assert.ok(await shellEval("document.querySelector('[data-plan-id=plan] .account-platform-icon').title.includes('窗口名称：恢复验证账号')"));
  await shellEval("document.querySelector('a[href=\"#/review\"]').click()");await waitShell("Boolean(document.querySelector('#other-page'))");
  await shellEval("document.querySelector('a[href=\"#/accounts\"]').click()");await waitShell("Boolean(document.querySelector('.account-sidebar-collapsed'))");
  await shellEval("document.querySelector('[aria-label=展开账号侧栏]').click()");
  await waitShell("!document.querySelector('.account-sidebar-collapsed') && localStorage.getItem('account-sidebar-collapsed')==='false'");
  assert.notEqual(await shellEval("getComputedStyle(document.querySelector('[data-plan-id=plan] .account-sidebar-identity')).display"),'none');
  console.log('PASS compact account metadata tooltip, remembered icon sidebar and native resize without reloading or clearing drafts');
  await choose('编辑');await waitShell("Boolean(document.querySelector('[aria-label=编辑窗口]'))");
  assert.equal(pane.getVisible(),false);
  assert.equal(await shellEval(`document.querySelector('[aria-label=编辑窗口] input[maxlength="80"]').value`),'恢复验证账号');
  await shellEval("document.querySelector('[aria-label=关闭编辑]').click()");
  await openCreation('批量窗口创建');
  assert.equal(await shellEval("document.querySelector('[aria-label=批量创建窗口] input[type=number]').value"),'10');
  const beforeCancel=await shellEval('fixture.commands.length');
  await shellEval("document.querySelector('[aria-label=关闭批量创建]').click()");
  await waitShell("!document.querySelector('.account-editor')");
  assert.equal(await shellEval('fixture.commands.length'),beforeCancel,'cancelling batch creation does not create windows');
  await openCreation('单个窗口创建');
  const setInput=(selector,value)=>shellEval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}))})()`);
  await setInput('[aria-label=新建窗口] input[maxlength="80"]','Cookie测试窗口');
  await shellEval("document.querySelector('.account-cookie-section').open=true;fixture.cookieFailed=true");
  await setInput('[aria-label="Cookie 内容"]','sessionid=local-fixture');
  await shellEval("document.querySelector('[aria-label=新建窗口] form').requestSubmit()");
  await waitShell("fixture.commands.at(-1)?.action==='save_with_cookies' && document.querySelector('[aria-label=编辑窗口] button[type=submit]')?.textContent==='重试导入'");
  await shellEval("document.querySelector('[aria-label=编辑窗口] form').requestSubmit()");
  await waitShell("fixture.commands.at(-1)?.action==='import_cookies' && !document.querySelector('.account-editor')");
  assert.equal(await shellEval("fixture.commands.filter(x=>x.action==='save_with_cookies').length"),1);
  await openCreation('单个窗口创建');
  assert.equal(await shellEval("document.querySelector('[aria-label=\"Cookie 内容\"]').value"),'');
  await shellEval("document.querySelector('[aria-label=关闭编辑]').click();fixture.extra=true");
  await waitShell("Boolean(document.querySelector('[data-plan-id=plan2]'))");
  await shellEval("(()=>{const a=document.querySelector('[data-plan-id=plan]');a.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',altKey:true,bubbles:true}))})()");
  await waitShell("fixture.order[0]==='plan2' && document.querySelector('[data-plan-id]')?.getAttribute('data-plan-id')==='plan2'");
  await shellEval("(()=>{const a=document.querySelector('[data-plan-id=plan]'),b=document.querySelector('[data-plan-id=plan2]');const data=new DataTransfer();a.dispatchEvent(new DragEvent('dragstart',{dataTransfer:data,bubbles:true}));setTimeout(()=>{b.dispatchEvent(new DragEvent('drop',{dataTransfer:data,bubbles:true}));a.dispatchEvent(new DragEvent('dragend',{dataTransfer:data,bubbles:true}))},50)})()");
  await waitShell("fixture.order[0]==='plan'");
  // Header multi-selection, per-window failure handling, notes and independent controls.
  await waitShell("Boolean(document.querySelector('#account-header-actions .account-batch-toolbar'))");
  await shellEval("document.querySelector('.account-batch-toolbar button').click()");
  await shellEval("document.querySelectorAll('.account-window-select-row input[type=checkbox]').forEach(e=>e.click())");
  await waitShell("document.querySelector('.account-batch-toolbar button').textContent.includes('2')");
  const batchClick=label=>shellEval(`[...document.querySelectorAll('.account-batch-toolbar button')].find(e=>e.textContent===${JSON.stringify(label)}).click()`);
  await shellEval("fixture.failProfile='plan';fixture.commands=[]");await batchClick('刷新');
  await waitShell("fixture.commands.filter(x=>x.action==='refresh').length===2 && document.querySelector('[role=status]')?.textContent.includes('已处理')");
  assert.equal(await shellEval("document.querySelector('.account-batch-results').textContent.includes('此窗口失败')"),true);
  await shellEval("fixture.failProfile='';fixture.commands=[];fixture.locked=false");
  await new Promise(r=>setTimeout(r,3300));
  await batchClick('备注');await waitShell("Boolean(document.querySelector('[aria-label=批量备注]'))");
  await setInput('[aria-label=批量备注内容]','批量测试备注');
  await shellEval("[...document.querySelectorAll('[aria-label=批量备注] button')].find(e=>e.textContent==='保存备注').click()");
  await waitShell("fixture.commands.filter(x=>x.action==='notes').length===2 && document.querySelector('[role=status]')?.textContent.includes('已处理')");
  assert.equal(await shellEval("fixture.commands.every(x=>x.action!=='notes'||x.notes==='批量测试备注')"),true);
  await batchClick('关闭');await waitShell("fixture.opened===false && document.querySelector('[role=status]')?.textContent.includes('已处理')");
  await batchClick('开启');await waitShell("fixture.opened===true && document.querySelector('[role=status]')?.textContent.includes('已处理')");
  await openMenu();assert.equal(await shellEval(`${menuButton('刷新')}.disabled`),false);assert.equal(await shellEval(`${menuButton('编辑')}.disabled`),false);assert.equal(await shellEval(`${menuButton('删除')}.disabled`),false);
  await shellEval("document.querySelector('[role=menu]').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));fixture.locked=false");
  await new Promise(r=>setTimeout(r,3300));
  console.log('PASS header multi-selection, sequential batch controls and notes, per-window failures; ordinary controls retain original task locks');
  await shellEval("fixture.focusReceiver('profile2')");await waitShell("document.querySelector('[data-plan-id=plan2]')?.getAttribute('aria-pressed')==='true'");
  await shellEval("document.querySelector('a[href=\"#/settings\"]').click()");await waitShell("Boolean(document.querySelector('[role=switch]'))");
  await shellEval("document.querySelector('[role=switch]').click()");await waitShell("fixture.notifyEnabled===false");
  await shellEval("document.querySelector('a[href=\"#/accounts\"]').click()");await waitShell("Boolean(document.querySelector('[data-plan-id=plan]'))");
  await shellEval("document.querySelector('a[href=\"#/settings\"]').click()");await waitShell("Boolean(document.querySelector('[role=switch]'))");
  assert.equal(await shellEval("document.querySelector('[role=switch]').checked"),false);
  await shellEval("document.querySelector('[role=switch]').click()");await waitShell("fixture.notifyEnabled===true");
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='发送测试提醒').click()");await waitShell('fixture.notifyTests===1');
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='清空所有缓存').click()");await waitShell("fixture.cleanModes.includes('all')");
  await shellEval("document.querySelector('[aria-label=自动清理缓存]').click()");await waitShell("fixture.storageAuto===false");
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='谷歌翻译').click()");await waitShell('fixture.translator===true');
  await shellEval("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))");await waitShell('fixture.translator===false');
  console.log('PASS storage settings clear-all and automatic switch; header Google Translate toggles and Escape hides');
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='ChatGPT').click()");await waitShell('fixture.chatgpt===true');
  await shellEval("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))");await waitShell('fixture.chatgpt===false');
  const navColors=await shellEval("[...document.querySelectorAll('.formal-nav a[data-nav]')].map(a=>({id:a.dataset.nav,href:a.getAttribute('href'),label:a.querySelector('span')?.textContent.trim(),iconCount:a.querySelectorAll('svg').length,anchor:getComputedStyle(a).color,text:a.querySelector('span')?getComputedStyle(a.querySelector('span')).color:null,icon:a.querySelector('svg')?getComputedStyle(a.querySelector('svg')).color:null}))");
  assertRetainedNavigation(navColors);
  console.log('PASS ChatGPT header toggle/Escape and distinct navigation text/icon accents');

  await shellEval("document.querySelector('a[href=\"#/accounts\"]').click()");await waitShell("Boolean(document.querySelector('[data-plan-id=plan]'))");
  console.log('PASS message reminder setting toggles and restores, test reminder action, notification selects its account window');
  await shellEval('fixture.extra=false');await waitShell("!document.querySelector('[data-plan-id=plan2]')");
  await shellEval("document.querySelector('a[href=\"#/review\"]').click();fixture.unread=8");
  await waitShell("document.querySelector('.total-unread-badge')?.textContent==='8'");
  await shellEval("document.querySelector('a[href=\"#/accounts\"]').click()");await waitShell("document.querySelector('[data-plan-id=plan] .account-platform-icon')?.title.includes('未读消息：8')");
  await shellEval('fixture.unread=0');await waitShell("!document.querySelector('.unread-badge')");
  await shellEval("fixture.whatsapp=true;fixture.omitUsername=true;fixture.storageError=true");
  await waitShell("[...document.querySelectorAll('button')].some(b=>b.textContent==='重新建立登录环境')");
  assert.equal(await shellEval("document.querySelector('.account-sidebar-list').textContent.includes('等待登录')"),false);
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='重新建立登录环境').click()");await waitShell("Boolean(document.querySelector('[aria-label=\"重置 WhatsApp 登录\"]'))");
  const resetBefore=await shellEval("fixture.commands.filter(c=>c.action==='reset_whatsapp_storage').length");
  await shellEval("[...document.querySelectorAll('[aria-label=\"重置 WhatsApp 登录\"] button')].find(b=>b.textContent==='取消').click()");
  assert.equal(await shellEval("fixture.commands.filter(c=>c.action==='reset_whatsapp_storage').length"),resetBefore);
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='重新建立登录环境').click()");await waitShell("Boolean(document.querySelector('[aria-label=\"重置 WhatsApp 登录\"]'))");
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='创建新环境').click()");await waitShell("fixture.commands.some(c=>c.action==='reset_whatsapp_storage'&&c.confirm===true)&&fixture.commands.at(-1).action==='open'&&!fixture.active");
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='检查登录环境').click()");await waitShell("[...document.querySelectorAll('button')].some(b=>b.textContent==='导出检查结果')");
  assert.equal(await shellEval('fixture.commands.at(-1).action'),'whatsapp_diagnostics');
  assert.equal(await shellEval("document.querySelectorAll('.chat-translation-bar').length"),0);
  assert.equal(await shellEval("fixture.translationCalls.some(c=>c.id==='plan'&&!c.settings&&!c.export)"),true,'background binding runs with editor closed');
  const windowSaves=await shellEval("fixture.commands.filter(c=>c.action==='save').length");
  assert.equal(await shellEval("document.querySelector('#account-translation-actions').nextElementSibling.textContent"),'ChatGPT');
  const surfaceCount=await shellEval('fixture.surfaces.length');
  assert.equal(await shellEval("document.querySelectorAll('[aria-label=同步翻译],[aria-label=翻译设置]').length"),0,'retired controls removed');
  await shellEval("document.querySelector('[aria-label=沉浸式翻译]').click()");await waitShell("fixture.translation.engine==='immersive'&&fixture.translation.enabled===true");
  assert.equal(await shellEval('fixture.surfaces.slice('+surfaceCount+').some(s=>s.visible===false)'),false,'quick toggle keeps chat visible');
  await shellEval("document.querySelector('[aria-label=沉浸式翻译设置]').click()");await waitShell("Boolean(document.querySelector('.account-editor [aria-label=聊天翻译设置] select'))");
  assert.equal(await shellEval("document.querySelector('.chat-translation-dialog').textContent.includes('发送前翻译')"),false);
  await waitShell("fixture.surfaces.at(-1).visible===false");
  assert.equal(await shellEval("(()=>{const d=document.querySelector('.chat-translation-dialog'),b=[...d.querySelectorAll('button')].find(x=>x.textContent==='保存翻译设置');b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();return !d.closest('.formal-header')&&d.getBoundingClientRect().height>600&&b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()"),true,'settings escape the actual sticky/blurred header and accept clicks over the workspace');
  await shellEval("[...document.querySelectorAll('[aria-label=聊天翻译设置] label')].find(l=>l.textContent.startsWith('聊天消息译为')).querySelector('select').value='de';[...document.querySelectorAll('[aria-label=聊天翻译设置] label')].find(l=>l.textContent.startsWith('聊天消息译为')).querySelector('select').dispatchEvent(new Event('change',{bubbles:true}))");
  await shellEval("[...document.querySelectorAll('[aria-label=聊天翻译设置] button')].find(b=>b.textContent==='保存翻译设置').click()");await waitShell("fixture.translation.incomingLang==='de'&&Boolean(document.querySelector('.chat-translation-success'))");
  assert.equal(await shellEval("fixture.commands.filter(c=>c.action==='save').length"),windowSaves,'translation save never submits window metadata');
  await shellEval("[...document.querySelectorAll('[aria-label=聊天翻译设置] button')].find(b=>b.textContent==='导出原文').click()");await waitShell("fixture.translationCalls.some(c=>c.id==='plan'&&c.export==='original')");
  await waitShell("!document.querySelector('[aria-label=关闭翻译设置]').disabled");
  await shellEval("document.querySelector('[aria-label=关闭翻译设置]').click()");await waitShell("!document.querySelector('[aria-label=聊天翻译设置]')");
  await waitForAccountPane('saving and closing translation settings');
  await shellEval("document.querySelector('[aria-label=沉浸式翻译设置]').click()");await waitShell("Boolean(document.querySelector('[aria-label=聊天翻译设置] select'))");
  assert.equal(await shellEval("document.querySelector('[aria-label=聊天翻译设置] select').value"),'de');
  if(process.env.JUXIN_EDITOR_SCREENSHOT){await shellEval("document.querySelector('[aria-label=聊天翻译设置]').scrollIntoView({block:'center'})");fs.writeFileSync(process.env.JUXIN_EDITOR_SCREENSHOT,(await win.webContents.capturePage()).toPNG())}
  await shellEval("document.querySelector('[aria-label=聊天翻译设置] select').value='ja';document.querySelector('[aria-label=聊天翻译设置] select').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('[aria-label=关闭翻译设置]').click()");
  assert.equal(await shellEval('fixture.translation.incomingLang'),'de','closing discards unsaved translation edits');
  await waitForAccountPane('discarding translation edits');
  await openMenu();await shellEval(`${menuButton('编辑')}.click()`);await waitShell("Boolean(document.querySelector('[aria-label=关闭编辑]'))");
  assert.equal(await shellEval("document.querySelectorAll('[aria-label=聊天翻译设置]').length"),0,'translation settings moved out of window editor');
  await shellEval("document.querySelector('[aria-label=关闭编辑]').click()");
  await waitForAccountPane('closing account editor');
  if(process.env.JUXIN_HEADER_SCREENSHOT)fs.writeFileSync(process.env.JUXIN_HEADER_SCREENSHOT,(await win.webContents.capturePage()).toPNG());
  console.log('PASS top translation switch/save/export, draft cancellation, no window-form submission and per-window session binding');
  await shellEval("fixture.whatsapp=false;fixture.omitUsername=false;fixture.storageError=false");await waitShell("![...document.querySelectorAll('button')].some(b=>b.textContent==='重新建立登录环境')");
  console.log('PASS actual repair confirmation/cancel/reopen, first-open diagnostics export entry, removal of false login placeholder, per-window inline-translation settings and native surface coverage');
  const {AccountContextMenu}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/account-context-menu.js')));
  const nativeMenu=new AccountContextMenu();
  await waitForAccountPane('returning from WhatsApp diagnostics to the native context menu');
  ipcMain.handle('test:menu',(_event,input)=>nativeMenu.show(win,input));
  await shellEval('window.collectorCore.accountContextMenu=window.testMenu;true');await shellEval(rightClick);
  await new Promise(r=>setTimeout(r,3500));assert.equal(pane.getVisible(),true);nativeMenu.close();
  await shellEval('delete window.collectorCore.accountContextMenu');
  console.log('PASS native context menu keeps the actual account page visible');
  console.log('PASS actual red badges update, zero hides, global badge survives routes; edit populates; single/batch creation choice, batch cancellation; Cookie retry reuses saved window and clears on close; keyboard and drag ordering');
  await choose('关闭');await waitShell('fixture.opened===false');
  await openMenu();assert.equal(await shellEval(`${menuButton('刷新')}.disabled`),true);
  assert.equal(await shellEval(`${menuButton('上一页')}.disabled`),true);
  await shellEval(`${menuButton('打开')}.click()`);await waitShell('fixture.opened===true');
  await shellEval('fixture.locked=true');
  await waitShell("Boolean(document.querySelector('.account-task-live'))");
  assert.notEqual(await shellEval("document.querySelector('[data-plan-id=plan]')?.getAttribute('aria-disabled')"),'true');
  assert.equal(await shellEval("document.querySelectorAll('[role=tab]').length"),4);
  await waitShell('fixture.surfaces.some(s=>s.readOnly&&s.visible)');
  const watchDeadline=Date.now()+4000;while(!pane.getVisible()&&Date.now()<watchDeadline)await new Promise(r=>setTimeout(r,30));
  assert.equal(pane.getVisible(),true,'actual task page is visible');
  assert.equal(host.visible.readOnly,true);
  await shellEval("document.querySelectorAll('[role=tab]')[2].click()");await waitShell("fixture.watchCalls.at(-1)==='screen-1'");
  assert.equal(await shellEval('fixture.interfereCalls'),0);
  assert.equal(await shellEval("document.querySelector('.account-task-watch-image').disabled"),false,'task screen offers explicit confirmation while its lease stays active');
  await shellEval("document.querySelector('.account-task-watch-image').click()");
  await waitShell('fixture.interfereCalls===1');
  assert.equal(await shellEval('fixture.manual'),null,'cancelling confirmation keeps automatic ownership unchanged');
  assert.equal(await shellEval("Boolean(document.querySelector('.account-task-watch'))"),true);
  assert.equal(host.visible.readOnly,true,'task screen remains read-only after a click');
  console.log('PASS task screen click requests confirmation; cancellation leaves it read-only with the same lease');
  // Use the actual accessible close control, not a direct fixture API call.
  if(!await shellEval('fixture.closePageCalls.length'))await shellEval("[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')==='关闭1-2').click()");
  await waitShell('fixture.closePageCalls.length===1');
  assert.equal(await shellEval('fixture.closePageCalls[0].target'),'screen-1');
  await waitShell("document.querySelector('.account-task-watch-toolbar')?.textContent.includes('等待 1-2 子页就绪')");
  assert.equal(await shellEval("document.querySelectorAll('[role=tab]').length"),4,'missing fixed slot remains selectable');
  await waitShell('fixture.surfaces.at(-1)?.visible===false');
  assert.equal(await shellEval('fixture.interfereCalls'),1,'closing a tab does not request manual interference');
  console.log('PASS task close button targets the selected subpage and preserves its waiting slot without switching siblings');
  await shellEval("[...document.querySelectorAll('[role=tab]')].find(b=>b.textContent==='采集页').click()");
  await waitShell("fixture.watchCalls.at(-1)==='source'");
  await shellEval('fixture.allowInterference=true');
  await waitShell("Boolean(document.querySelector('.account-task-watch-image'))&&!document.querySelector('.account-task-watch-image').disabled");
  await shellEval("document.querySelector('.account-task-watch-image').click()");
  await waitShell('fixture.interfereCalls===2&&Boolean(fixture.manual)');
  await waitShell('fixture.surfaces.some(s=>s.visible&&!s.readOnly&&s.interferenceGrant==="fixture-grant")');
  assert.equal(await shellEval("[...document.querySelectorAll('.account-task-watch button')].some(b=>/暂停并操作|继续采集/.test(b.textContent))"),false,'manual operation adds no new task-control buttons');
  await shellEval('fixture.manual=null');
  await waitShell('fixture.surfaces.filter(s=>s.visible).at(-1)?.readOnly===true');
  console.log('PASS confirmed task operation gets the exact page grant; ordinary resume revokes input without releasing task occupancy');


  await openMenu();assert.equal(await shellEval("[...document.querySelectorAll('[role=menuitem]')].every(b=>b.disabled)"),true);
  assert.equal(pane.getVisible(),false);
  await shellEval('fixture.locked=false');await waitShell(`!${menuButton('删除')}.disabled`);
  await shellEval(`${menuButton('删除')}.click()`);await waitShell("Boolean(document.querySelector('[aria-label=删除窗口]'))");
  assert.equal(pane.getVisible(),false);
  await shellEval("[...document.querySelectorAll('button')].find(b=>b.textContent==='确认删除').click()");
  await waitShell("!document.querySelector('[data-plan-id=plan]') && !document.querySelector('[aria-label=删除窗口]')");
  assert.equal(await shellEval('fixture.commands.at(-1).action'),'delete');
  assert.equal(pane.getVisible(),false);
  console.log('PASS real React context menu, error recovery, route changes and delayed page loads, close/open/delete, locked controls; production presenter controls native pane visibility');
  await shellEval("document.querySelector('a[href=\"#/\"]').click()");await waitShell("Boolean(document.querySelector('#home-title'))");
  assert.equal(await shellEval("document.querySelector('#home-title').textContent"),'聚鑫国际');
  assert.equal(await shellEval("document.querySelectorAll('.home-guide-card').length"),4);
  assert.equal(await shellEval("document.querySelectorAll('.home-metrics,.home-status').length"),0);
  assert.equal(pane.getVisible(),false);
  assert.deepEqual(await shellEval("[...document.querySelectorAll('[aria-label=主导航] a>span:first-of-type')].map(x=>x.textContent)"),['首页','账号','检查','养号','采集','审核','公开','私密','报表','历史']);
  console.log('PASS current home brand, four existing operation guides, merged reports and nurture navigation');
  await new Promise(r=>setTimeout(r,150));
  if(process.env.JUXIN_HOME_SCREENSHOT)fs.writeFileSync(process.env.JUXIN_HOME_SCREENSHOT,(await win.webContents.capturePage()).toPNG());
  watchdog.begin('task-watch',270000);await require('./task-watch.integration.cjs')({host,owner,base});
  watchdog.begin('account-updates',270000);await require('./account-updates.integration.cjs')({host,owner});
  watchdog.begin('google-translator-popup',270000);await require('./google-translator.integration.cjs')({win});
  watchdog.begin('floating-pages',270000);await require('./floating-pages.integration.cjs')({win,host,owner});
  watchdog.begin('chat-translation',270000);await require('./chat-translation.integration.cjs')({host,owner});
  watchdog.begin('immersive-runtime',135000);await require('./immersive-runtime.integration.cjs')({win});
  watchdog.begin('whatsapp-native',270000);await require('./whatsapp-native.integration.cjs')({host,owner});
  watchdog.begin('whatsapp-layout',270000);await require('./whatsapp-layout.integration.cjs')({host,owner,temp});
  watchdog.begin('instagram-chat',270000);await require('./instagram-layout.integration.cjs')({host,owner});
  watchdog.begin('window-performance',360000);await require('./window-performance.integration.cjs')({host,owner});
  watchdog.begin('core-route-stability',270000);await require('./core-route-stability.integration.cjs')({win,host});
  watchdog.begin('source-recheck',90000);await require('./source-recheck.integration.cjs')({win,host});
  watchdog.begin('work-report-summary-r6',120000);await require('./work-report-summary-r6.integration.cjs')({win,host});
  watchdog.begin('nurture-reels-r6',120000);await require('./nurture-reels-r6.integration.cjs')({win,host});
  watchdog.begin('completed-card-delete',90000);await require('./completed-card-delete.integration.cjs')({win,host});
  watchdog.begin('workbench-platform',90000);await require('./workbench-platform.integration.cjs')({win,host});
  watchdog.begin('final-cleanup',180000);
  await host.stop();await new Promise(r=>fixture.close(r));win.destroy();
  assert.equal((await presenter.update(win,{visible:false})).attached,false);
  assert.equal((await presenter.update(win,{id:'plan',visible:true,documentUrl:base+'/account-ui',bounds:{x:200,y:80,width:900,height:700}})).attached,false);
  console.log('PASS late surface messages after actual BrowserWindow destruction');
  const timing=watchdog.finish();console.log('PASS all embedded browser integration checks');if(process.env.JUXIN_EMBEDDED_RESULT)fs.writeFileSync(process.env.JUXIN_EMBEDDED_RESULT,JSON.stringify({verified:true,pythonWorker:Boolean(process.env.JUXIN_PYTHON),taskColdStart,timing}));app.exit(0);
}).catch(e=>{console.error('FAIL',e);void exitFailed(1,watchdog.status(),e)});
