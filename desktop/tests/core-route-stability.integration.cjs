const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const {prepareVisibleFixture}=require('./visible-fixture.cjs');
const assert=require('node:assert/strict'),path=require('node:path'),http=require('node:http'),fs=require('node:fs'),os=require('node:os');

// Accounts/review remain independently usable; collection retains its snapshot.
const failureCopy={accounts:'总览暂未更新',review:'总览暂未更新',collection:'本次快照更新失败'};
function assertRouteState(state,{mode,failed=false,revision=1}){
 assert.equal(state.route,'#/'+mode,'React has committed the requested route');
 assert.equal(state.blocked,false,'a refresh must not replace cached content with a reconnect screen');
 assert.equal(state.dedupe,'IG 去重 '+revision,'the accepted snapshot stays visible');
 if(failed){
  assert.ok(failureCopy[mode],'failure assertion requires a supported route');
  assert.ok(state.banner.includes(failureCopy[mode]),mode+' Core failure stays visible: '+JSON.stringify(state));
  assert.equal(state.retryEnabled,true,'the displayed failure has an enabled retry action');
 }else assert.equal(state.banner,'','healthy/recovered route has no stale snapshot warning');
}
function assertSnapshotCadence(starts){
 assert.ok(starts.length>0,'initial snapshot was read');
 // A slow machine can cross the production 5-second poll interval during route
 // changes. Only an extra immediate read is a reconnect, not an ordinary poll.
 for(let i=1;i<starts.length;i++)assert.ok(starts[i]-starts[i-1]>=4900,'route changes must not create extra immediate snapshots');
}
const routeStateScript=`(()=>{
 const banner=document.querySelector('.formal-content > .formal-error-banner');
 return {route:document.querySelector('.formal-nav a[aria-current="page"]')?.getAttribute('href'),
 blocked:!!document.querySelector('.formal-blocked'),
 dedupe:[...document.querySelectorAll('.formal-header-meta .formal-chip')].find(e=>e.textContent.startsWith('IG 去重'))?.textContent,
 banner:banner?.querySelector('span')?.textContent||'',retryEnabled:!!banner?.querySelector('button:not(:disabled)')};
})()`;
const wolfSelector='button.formal-brand[aria-label="刷新"]';
const shellStateScript=`(()=>{
 const wolf=document.querySelector('${wolfSelector}'),image=wolf?.querySelector('img'),rect=wolf?.getBoundingClientRect();
 return {wolfCount:document.querySelectorAll('${wolfSelector}').length,title:wolf?.title,type:wolf?.type,
  imageLoaded:!!image?.complete&&image.naturalWidth>0,visible:!!rect&&rect.width>=44&&rect.height>=44&&rect.left>=0&&rect.top>=0,
  badges:[...document.querySelectorAll('.formal-header-meta .formal-chip')].map(node=>node.textContent),
  providerBadges:document.querySelectorAll('.formal-header-meta .formal-live').length,
  retiredCopy:['界面 r94 / Core','刷新页面','部分列表显示最近','前往设置'].some(text=>document.body.textContent.includes(text)),
  topRefresh:!!document.querySelector('.formal-header button[aria-label="刷新"]'),
  busy:wolf?.getAttribute('aria-busy'),disabled:wolf?.disabled};
})()`;
function assertCleanShell(state,{revision=1,busy=false}={}){
 assert.equal(state.wolfCount,1);assert.equal(state.type,'button');assert.equal(state.title,'刷新');
 assert.equal(state.imageLoaded,true,'wolf image is loaded for visual evidence');assert.equal(state.visible,true);
 assert.deepEqual(state.badges,['IG 去重 '+revision]);assert.equal(state.providerBadges,0);
 assert.equal(state.retiredCopy,false);assert.equal(state.topRefresh,false);
 assert.equal(state.busy,String(busy));assert.equal(state.disabled,busy);
}
// Bounded teardown for this disposable localhost fixture server only.
function closeFixtureServer(server,timeoutMs=3000){
  return new Promise((resolve,reject)=>{
    let settled=false;
    const finish=error=>{if(settled)return;settled=true;clearTimeout(timer);error?reject(error):resolve()};
    const timer=setTimeout(()=>finish(new Error('Fixture HTTP cleanup timed out')),timeoutMs);
    try{
      server.close(error=>finish(error&&error.code!=='ERR_SERVER_NOT_RUNNING'?error:null));
      server.closeIdleConnections?.();
      server.closeAllConnections?.();
    }catch(error){finish(error)}
  });
}
module.exports=async({win,host})=>{
 const built=await require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/core-route-stability.tsx')],bundle:true,write:false,outfile:'route.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 const server=http.createServer((req,res)=>{
  if(req.url==='/war-wolf.svg'){res.setHeader('content-type','image/svg+xml');res.end(fs.readFileSync(path.resolve(__dirname,'../../renderer/public/war-wolf.svg')));return;}
  const name=req.url==='/route.js'?'.js':req.url==='/route.css'?'.css':null;
  res.setHeader('content-type',name==='.js'?'text/javascript; charset=utf-8':name==='.css'?'text/css; charset=utf-8':'text/html; charset=utf-8');
  res.end(name?built.outputFiles.find(x=>x.path.endsWith(name)).contents:'<meta charset="utf-8"><link rel="stylesheet" href="/route.css"><div id="root"></div><script src="/route.js"></script>');
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));host.hide();
 const evaluate=s=>rendererFixtureRead(win.webContents,s,{label:'Core route fixture'});
 const wait=(source,label)=>waitRendererFixture(win.webContents,source,{label});
 let documentLoads=0;const countDocumentLoad=()=>{documentLoads++};
 const capture=async name=>{
  await wait(`(${shellStateScript}).imageLoaded`,'wolf image loaded');
  assertCleanShell(await evaluate(shellStateScript));
  const destination=process.env.JUXIN_SHELL_SCREENSHOT_DIR||(process.env.JUXIN_EMBEDDED_RESULT?path.dirname(process.env.JUXIN_EMBEDDED_RESULT):path.join(os.tmpdir(),'juxin-r6-shell-evidence'));
  fs.mkdirSync(destination,{recursive:true});
  const shot=await win.capturePage();assert.equal(shot.isEmpty(),false);
  fs.writeFileSync(path.join(destination,name+'.png'),shot.toPNG());
  console.log('PASS shell screenshot',path.join(destination,name+'.png'));
 };
 const navigate=async route=>{
  await evaluate(`location.hash=${JSON.stringify(route)};true`);
  await wait(`document.querySelector('.formal-nav a[aria-current="page"]')?.getAttribute('href')===${JSON.stringify(route)}`,'React route committed '+route);
 };
 try{
  await win.loadURL('http://127.0.0.1:'+server.address().port+'/#/accounts');
  await prepareVisibleFixture(win,{label:'Core route stability'});
  await wait(`window.routeFixture?.successes>=1 && (${routeStateScript}).dedupe==='IG 去重 1'`,'initial Core snapshot rendered');
  win.webContents.on('did-finish-load',countDocumentLoad);
  await capture('r6-shell-wolf-accounts');
  await navigate('#/collection');await capture('r6-shell-wolf-collection');
  await navigate('#/accounts');
  for(let i=0;i<10;i++)for(const route of ['#/accounts','#/','#/reports','#/accounts']){
   await navigate(route);
   assert.equal(await evaluate("!!document.querySelector('.formal-blocked')"),false,'route '+route+' keeps authenticated Core snapshot');
  }
  assert.equal(await evaluate('routeFixture.resumes'),1,'one authenticated session across all route changes');
  assertSnapshotCadence(await evaluate('routeFixture.snapshotStarts'));
  assertRouteState(await evaluate(routeStateScript),{mode:'accounts'});
  const successes=await evaluate('routeFixture.successes');
  // Native keyboard activation uses the real button. Hold the read, then spam
  // same-tick clicks and navigate while pending: only one data request may run.
  const browserCallsBefore=await evaluate('routeFixture.browserCalls.length');
  const before=await evaluate(`(()=>{routeFixture.fail=true;routeFixture.holdNext=true;document.querySelector('${wolfSelector}').focus();return routeFixture.snapshots})()`);
  win.webContents.sendInputEvent({type:'keyDown',keyCode:'Return'});
  win.webContents.sendInputEvent({type:'keyUp',keyCode:'Return'});
  await wait("typeof routeFixture.releaseSnapshot==='function'",'keyboard wolf refresh entered bridge');
  assert.equal(await evaluate('routeFixture.snapshots'),before+1);
  await evaluate(`for(let i=0;i<25;i++)document.querySelector('${wolfSelector}').click();true`);
  assert.equal(await evaluate('routeFixture.snapshots'),before+1,'repeated wolf clicks share one request');
  assert.equal(await evaluate('routeFixture.browserCalls.length'),browserCallsBefore,'wolf does not hide, reload, open or take over an account browser');
  assertCleanShell(await evaluate(shellStateScript),{busy:true});
  assertRouteState(await evaluate(routeStateScript),{mode:'accounts'});
  await navigate('#/collection');
  assertCleanShell(await evaluate(shellStateScript),{busy:true});
  assertRouteState(await evaluate(routeStateScript),{mode:'collection'});
  await navigate('#/accounts');
  await evaluate('routeFixture.releaseSnapshot();true');
  await wait(`routeFixture.failures>0 && (${routeStateScript}).banner.includes(${JSON.stringify(failureCopy.accounts)})`,'accounts snapshot failure rendered');
  assertRouteState(await evaluate(routeStateScript),{mode:'accounts',failed:true});
  await wait(`!document.querySelector('${wolfSelector}').disabled`,'wolf unlocks after failed refresh');
  await navigate('#/collection');
  assertRouteState(await evaluate(routeStateScript),{mode:'collection',failed:true});
  await capture('r6-shell-refresh-failure');
  await navigate('#/accounts');
  assertRouteState(await evaluate(routeStateScript),{mode:'accounts',failed:true});
  await evaluate("routeFixture.fail=false;routeFixture.revision=2;document.querySelector('.formal-content > .formal-error-banner button').click();true");
  await wait(`routeFixture.successes>${successes} && (${routeStateScript}).dedupe==='IG 去重 2' && !(${routeStateScript}).banner`,'manual retry delivered fresh snapshot');
  assertRouteState(await evaluate(routeStateScript),{mode:'accounts',revision:2});
  await navigate('#/collection');
  assertRouteState(await evaluate(routeStateScript),{mode:'collection',revision:2});
  assert.equal(await evaluate('routeFixture.resumes'),1,'failure and recovery do not restart authentication');
  assert.equal(documentLoads,0,'wolf refresh never reloads the application document');
  assert.deepEqual(await evaluate("routeFixture.browserCalls.filter(call=>call.method!=='accountSurface')"),[],'wolf refresh never issues browser actions or Core restart');
  assert.equal(await evaluate("routeFixture.browserCalls.some(call=>call.method==='accountSurface'&&call.input.visible)"),false,'route cleanup never opens or takes over an account window');
  assert.equal(await evaluate("routeFixture.requests.some(path=>path.includes('/commands'))"),false,'wolf refresh never restarts tasks or reassigns windows');
  assertCleanShell(await evaluate(shellStateScript),{revision:2});
  console.log('PASS wolf keyboard activation, single-flight, hidden badges, bounded-banner absence, no document/browser/task restart; 40 real React route changes; retained data during pending/failed refresh, route-specific warnings and retry recovery');
 }catch(error){
  try{console.error('CHECK Core route failure state',JSON.stringify(await evaluate(`({ui:(${routeStateScript}),snapshots:window.routeFixture?.snapshots,failures:window.routeFixture?.failures,successes:window.routeFixture?.successes,resumes:window.routeFixture?.resumes,visibility:document.visibilityState})`)))}catch{}
  throw error;
 }finally{win.webContents.removeListener('did-finish-load',countDocumentLoad);await closeFixtureServer(server)}
};
module.exports.assertRouteState=assertRouteState;
module.exports.assertSnapshotCadence=assertSnapshotCadence;

module.exports.assertCleanShell=assertCleanShell;
module.exports.shellStateScript=shellStateScript;

// Fast local-only native gate, also included in the full embedded Windows gate.
if(require.main===module){
 const {app,BrowserWindow}=require('electron');
 app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'juxin-r6-shell-')));
 app.whenReady().then(async()=>{
  const win=new BrowserWindow({show:true,width:1500,height:960,webPreferences:{sandbox:true}});
  try{await module.exports({win,host:{hide(){}}});app.exit(0)}
  catch(error){console.error(error);app.exit(1)}
 });
}

module.exports.closeFixtureServer=closeFixtureServer;
