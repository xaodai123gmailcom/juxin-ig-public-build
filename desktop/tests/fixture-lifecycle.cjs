/* Shared test-only lifecycle contract. DOM readiness is not a paint claim.
   Only a visible native window, real animation callbacks and a nonempty native
   capture may satisfy capture(). No mutating action is retried by this helper. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {performance} = require('node:perf_hooks');
const {rendererFixtureRead, waitRendererFixture} = require('./renderer-fixture.cjs');
const {prepareVisibleFixture} = require('./visible-fixture.cjs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const errorRecord = error => ({name:error?.name || 'Error', message:String(error?.message || error), code:error?.code, stack:error?.stack, phase:error?.fixturePhase});
function deadlineError(label, timeoutMs) {
  const error = new Error(`Fixture phase timed out: ${label} (${Math.ceil(timeoutMs)} ms)`);
  error.code = 'FIXTURE_DEADLINE'; return error;
}
function bounded(operation, {label, timeoutMs=5000}={}) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error('Invalid fixture phase deadline');
  const deadline = performance.now() + timeoutMs;
  let timer;
  const timeout = new Promise((_, reject) => {timer = setTimeout(() => reject(deadlineError(label,timeoutMs)), timeoutMs)});
  return Promise.race([Promise.resolve().then(operation).then(value => {
    if (performance.now() >= deadline) throw deadlineError(label,timeoutMs);
    return value;
  }), timeout]).finally(() => clearTimeout(timer));
}
function closeFixtureServer(server, timeoutMs=3000) {
  return bounded(() => new Promise((resolve,reject) => {
    try {
      server.close(error => error && error.code !== 'ERR_SERVER_NOT_RUNNING' ? reject(error) : resolve());
      server.closeIdleConnections?.(); server.closeAllConnections?.();
    } catch(error) {reject(error)}
  }), {label:'Fixture HTTP cleanup',timeoutMs});
}
function nativeState(win) {
  if (win.isDestroyed()) return {destroyed:true};
  return {destroyed:false,visible:win.isVisible(),minimized:win.isMinimized(),focused:win.isFocused(),
    bounds:win.getBounds?.(),contentSize:win.getContentSize?.(),zoomFactor:win.webContents.getZoomFactor?.() ?? 1};
}
const rendererStateScript = `({url:location.href,ready:document.readyState,visibility:document.visibilityState,
  focused:document.hasFocus(),fonts:document.fonts?.status,width:innerWidth,height:innerHeight,devicePixelRatio})`;
const paintScript = `new Promise(resolve=>{
  const state=()=>({ready:document.readyState,visibility:document.visibilityState,width:innerWidth,height:innerHeight,devicePixelRatio,fonts:document.fonts?.status});
  const before=state();
  if(before.ready!=='complete'||before.visibility!=='visible'){resolve({...before,painted:false});return}
  requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({...state(),painted:true})));
})`;
function assertGeometryGroups(layout, groups={}) {
  for (const [name, expected] of Object.entries(groups)) {
    assert.ok(Array.isArray(layout?.[name]), name+' geometry is present');
    if (expected === 'nonempty') assert.ok(layout[name].length > 0, name+' geometry is nonempty');
    else assert.equal(layout[name].length,expected,name+' geometry count');
    for (const node of layout[name]) {
      assert.ok(Number.isFinite(node.width) && node.width > 0 && Number.isFinite(node.height) && node.height > 0,
        name+' geometry has positive finite dimensions');
    }
  }
}
async function stableGeometry(evaluate, source, width, {label='Fixture layout',timeoutMs=3000,stableMs=100,pollMs=25,widthKey='width',height,heightKey='height',groups={},nativeSize}={}) {
  if(nativeSize&&height===undefined)throw new Error('Native geometry requires requested height');
  if (height!==undefined&&(!Number.isFinite(height)||height<=0))throw new Error('Invalid geometry height');
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || !Number.isFinite(stableMs) || stableMs < 0 || !Number.isFinite(pollMs) || pollMs <= 0) throw new Error('Invalid geometry deadline or viewport');
  const deadline=performance.now()+timeoutMs;
  let previous, previousValid=false, stableSince=performance.now(), last;
  while (performance.now() < deadline) {
    const remaining=deadline-performance.now();
    // This bound applies even to an evaluator that never resolves.
    last=await bounded(() => evaluate(source,label,remaining),{label,timeoutMs:remaining});
    const signature=JSON.stringify(last), size=nativeSize?.();
    let valid=last?.[widthKey]===width && (height===undefined||last?.[heightKey]===height) && (!size || size[0]===width&&(height===undefined||size[1]===height));
    try {assertGeometryGroups(last,groups)} catch {valid=false}
    if (valid && previousValid && signature===previous) {
      if (performance.now()-stableSince>=stableMs) return last;
    } else {previous=signature;stableSince=performance.now()}
    previousValid=valid;
    await pause(Math.min(pollMs,Math.max(0,deadline-performance.now())));
  }
  const error=new Error(label+' did not settle at '+width+'px; last geometry: '+JSON.stringify(last));
  error.code='FIXTURE_GEOMETRY';error.requestedViewport={width,height};error.lastGeometry=last;throw error;
}
function semanticScript(selector) {
  return `(()=>{const root=document.querySelector(${JSON.stringify(selector)});if(document.readyState!=='complete'||!root)return null;
    const r=root.getBoundingClientRect();return {width:innerWidth,height:innerHeight,root:{width:r.width,height:r.height},
    text:root.textContent,controls:[...root.querySelectorAll('input,select,textarea,button')].map(n=>({value:n.value,checked:n.checked,disabled:n.disabled})),
    busy:root.getAttribute('aria-busy')};})()`;
}
function createFixtureLifecycle(win,{label='fixture',entryMode='embedded',outputDirectory=path.resolve('installer-output'),sourceFile}={}) {
  const started=performance.now(),listeners=[];
  const state={schema:1,label,entryMode,platform:process.platform,nativeRuntime:Boolean(process.versions.electron),versions:{...process.versions},startedAt:new Date().toISOString(),
    currentAction:null,lastCommittedAction:null,actions:[],events:[],rendererErrors:[],fatalRendererErrors:[],diagnosticErrors:[],cleanupErrors:[],primaryError:null,requestedWidth:null,captures:[],completed:false};
  const initialSize=win.getContentSize();state.requestedViewport={width:initialSize[0],height:initialSize[1]};
  state.sourceFiles={};
  for(const file of [sourceFile,__filename,require.resolve('./renderer-fixture.cjs'),require.resolve('./visible-fixture.cjs')].filter(Boolean))state.sourceFiles[path.basename(file)]=hash(fs.readFileSync(file));
  const sourceManifest=path.resolve(__dirname,'../../SOURCE_SHA256.json');
  if(fs.existsSync(sourceManifest))state.baselineManifestSha256=hash(fs.readFileSync(sourceManifest));
  if(process.versions.electron){try{state.display=require('electron').screen.getDisplayMatching(win.getBounds())}catch(error){state.diagnosticErrors.push(errorRecord(error))}}
  const observe=(target,event)=>{
    if(!target?.on)return;
    const listener=(...args)=>{
      const first=args[0];
      const values=event==='console-message'&&first?.message ? [{level:first.level,message:first.message,lineNumber:first.lineNumber,sourceId:first.sourceId}] : args.slice(1);
      const details=values.map(value=>{try{return value instanceof Error?JSON.stringify(errorRecord(value)):typeof value==='object'?String(JSON.stringify(value)):String(value)}catch{return '[unserializable event detail]'}});
      const record={event,elapsedMs:Math.round(performance.now()-started),details};
      state.events.push(record);if(state.events.length>100)state.events.shift();
      if(event==='render-process-gone'||event==='preload-error')state.fatalRendererErrors.push(record);
      if(event==='render-process-gone'||event==='preload-error'||event==='console-message'&&(first?.level==='error'||first?.level===3||args[1]==='error'||args[1]===3))state.rendererErrors.push(record);
    };
    target.on(event,listener);listeners.push(()=>target.removeListener(event,listener));
  };
  for(const event of ['show','hide','minimize','restore','focus','blur','unresponsive','responsive','closed'])observe(win,event);
  for(const event of ['render-process-gone','did-fail-load','console-message','preload-error','destroyed'])observe(win.webContents,event);
  function rendererFailure(){
    if(!state.fatalRendererErrors.length)return null;
    return Object.assign(new Error(label+' fatal renderer lifecycle failure: '+JSON.stringify(state.fatalRendererErrors)),{code:'FIXTURE_RENDERER_FATAL',rendererErrors:[...state.fatalRendererErrors],fixturePhase:state.currentAction});
  }
  function assertRendererHealthy(){const error=rendererFailure();if(error)throw error}
  function tag(error,action) {
    if(!error || typeof error!=='object')error=new Error(String(error));
    error.fixturePhase ||= action;
    return error;
  }
  async function phase(name,operation,{timeoutMs=5000,source}={}) {
    const action={id:label+':'+String(state.actions.length+1).padStart(3,'0'),name,timeoutMs,startedMs:Math.round(performance.now()-started),sourceHash:source?hash(source):undefined,source};
    state.currentAction=action;state.actions.push(action);
    try {assertRendererHealthy();const value=await bounded(operation,{label:action.id+' '+name,timeoutMs});assertRendererHealthy();action.outcome='passed';state.lastCommittedAction=action;return value}
    catch(error){action.outcome='failed';action.elapsedMs=Math.round(performance.now()-started-action.startedMs);throw tag(error,{...action})}
    finally{action.elapsedMs=Math.round(performance.now()-started-action.startedMs)}
  }
  const read=(source,name='read '+hash(source).slice(0,12),timeoutMs=5000)=>phase(name,()=>rendererFixtureRead(win.webContents,source,{label:state.currentAction.id+' '+name,timeoutMs}),{timeoutMs,source});
  const wait=(source,name,timeoutMs=5000)=>phase(name||'condition '+hash(source).slice(0,12),()=>waitRendererFixture(win.webContents,source,{label:state.currentAction.id+' '+(name||'condition'),timeoutMs}),{timeoutMs,source});
  async function semantic(selector,{label:phaseLabel='semantic readiness',timeoutMs=5000,stableMs=100,pollMs=20}={}) {
    const script=semanticScript(selector);
    return phase(phaseLabel,async()=>{
      const deadline=performance.now()+timeoutMs;let previous,stableSince=performance.now();
      for(;;){
        const remaining=deadline-performance.now();if(remaining<=0)throw deadlineError(phaseLabel,timeoutMs);
        const value=await rendererFixtureRead(win.webContents,script,{label:phaseLabel,timeoutMs:remaining});
        const signature=JSON.stringify(value);
        if(value && value.root.width>0 && value.root.height>0 && signature===previous){if(performance.now()-stableSince>=stableMs)return value}
        else{previous=signature;stableSince=performance.now()}
        await pause(Math.min(pollMs,Math.max(0,deadline-performance.now())));
      }
    },{timeoutMs,source:script});
  }
  async function visible(name='native foreground',timeoutMs=30000) {
    return phase(name,()=>prepareVisibleFixture(win,{label:name,timeoutMs}),{timeoutMs});
  }
  async function capture(name,{width=state.requestedViewport.width,height=state.requestedViewport.height,visibilityTimeoutMs=5000,paintTimeoutMs=5000,captureTimeoutMs=2000}={}) {
    assert.ok(Number.isFinite(width)&&width>0&&Number.isFinite(height)&&height>0,'capture requires a positive requested viewport');
    assertRendererHealthy();state.requestedWidth=width;state.requestedHeight=height;state.requestedViewport={width,height};
    await visible(name+' visibility',visibilityTimeoutMs);
    const painted=await read(paintScript,name+' genuine visible paint',paintTimeoutMs);
    assert.equal(painted.painted,true,name+' must receive real animation callbacks');
    assert.equal(painted.visibility,'visible',name+' renderer remains visible');
    assert.equal(painted.width,width,name+' painted viewport width matches request');
    assert.equal(painted.height,height,name+' painted viewport height matches request');
    const before=nativeState(win);
    assert.ok(before.visible&&!before.minimized&&!before.destroyed,name+' native window is visible');
    assert.equal(before.contentSize[0],width,name+' native viewport width matches request');
    assert.equal(before.contentSize[1],height,name+' native viewport height matches request');
    const pixelScale=painted.devicePixelRatio/before.zoomFactor;
    assert.ok(Number.isFinite(pixelScale)&&pixelScale>0,name+' observed device pixel scale');
    // Chromium ScaleToCeiledSize multiplies SizeF/float, not JS doubles.
    const physical=dimension=>Math.ceil(Math.fround(Math.fround(dimension)*Math.fround(pixelScale)));
    const expectedPixels={width:physical(width),height:physical(height)};
    const {image,png,size}=await phase(name+' capture',async()=>{
      const image=await (win.webContents.capturePage ? win.webContents.capturePage() : win.capturePage());
      assert.equal(image.isEmpty(),false,name+' capture must not be empty');
      const png=image.toPNG(),size=image.getSize();
      assert.ok(png.length>0 && size.width>0 && size.height>0,name+' capture has nonempty pixels');
      // See electron/electron v44.4.5 shell/browser/api/electron_api_web_contents.cc.
      // Electron 44.4.5 capturePage uses ceil(DIP * device scale) and
      // returns a 1x-tagged bitmap. NativeImage size here is pixel size, not DIPs.
      assert.deepEqual(size,expectedPixels,name+' capture pixels match requested width/height and observed scale');
      return {image,png,size};
    },{timeoutMs:captureTimeoutMs});
    const renderer=await read(rendererStateScript,name+' post-capture state',2000),native=nativeState(win);
    assert.ok(native.visible&&!native.minimized&&!native.destroyed,name+' native visibility survives capture');
    assert.equal(native.contentSize[0],width,name+' native viewport survives capture');
    assert.equal(native.contentSize[1],height,name+' native viewport height survives capture');
    assert.equal(renderer.visibility,'visible',name+' renderer visibility survives capture');assert.equal(renderer.width,width,name+' renderer viewport survives capture');
    assert.equal(renderer.height,height,name+' renderer viewport height survives capture');
    assert.equal(renderer.devicePixelRatio/native.zoomFactor,pixelScale,name+' observed device scale survives capture');
    assertRendererHealthy();
    state.captures.push({name,requestedViewport:{width,height},pixelScale,expectedPixels,bytes:png.length,sha256:hash(png),size,painted,native,renderer});
    return image;
  }
  async function reload(name='reload fixture',timeoutMs=30000){
    const wc=win.webContents;let remove=()=>{};
    try{return await phase(name,()=>new Promise((resolve,reject)=>{
      const done=()=>resolve(true),destroyed=()=>reject(new Error(name+': renderer destroyed'));
      const failed=(_event,code,description,url,isMainFrame)=>{if(isMainFrame!==false)reject(Object.assign(new Error(name+': navigation failed '+code+' '+description),{code:'FIXTURE_NAVIGATION_FAILED',url}))};
      remove=()=>{wc.removeListener('did-finish-load',done);wc.removeListener('did-fail-load',failed);wc.removeListener('destroyed',destroyed)};
      wc.once('did-finish-load',done);wc.on('did-fail-load',failed);wc.once('destroyed',destroyed);
      wc.reload();
    }),{timeoutMs})}finally{remove()}
  }
  const reportPath=path.join(outputDirectory,label.replace(/[^a-zA-Z0-9_-]+/g,'-').toLowerCase()+'-lifecycle.json');
  function save(){fs.mkdirSync(outputDirectory,{recursive:true});fs.writeFileSync(reportPath,JSON.stringify({...state,elapsedMs:Math.round(performance.now()-started)},null,2)+'\n')}
  async function diagnose(error) {
    state.primaryError=errorRecord(error);
    try{state.failureNative=nativeState(win)}catch(reason){state.diagnosticErrors.push(errorRecord(reason))}
    try{state.failureRenderer=await rendererFixtureRead(win.webContents,rendererStateScript,{label:label+' failure state',timeoutMs:2000})}catch(reason){state.diagnosticErrors.push(errorRecord(reason))}
    try{
      // Diagnostic captures are explicitly not accepted paint evidence.
      const image=await bounded(()=>win.webContents.capturePage?win.webContents.capturePage():win.capturePage(),{label:label+' failure screenshot',timeoutMs:2000});
      assert.equal(image.isEmpty(),false);const png=image.toPNG();assert.ok(png.length>0);
      fs.mkdirSync(outputDirectory,{recursive:true});const file=reportPath.replace(/\.json$/,'.png');fs.writeFileSync(file,png);state.failureScreenshot={file,bytes:png.length,sha256:hash(png),acceptedPaintEvidence:false};
    }catch(reason){state.diagnosticErrors.push(errorRecord(reason))}
    console.error('CHECK fixture failure',JSON.stringify({label,primaryError:state.primaryError,currentAction:state.currentAction,lastCommittedAction:state.lastCommittedAction,native:state.failureNative,renderer:state.failureRenderer,reportPath}));
    try{save()}catch(reason){state.diagnosticErrors.push(errorRecord(reason));console.error('CHECK fixture diagnostics unavailable',reason)}
  }
  async function finish(primaryError,cleanups=[]) {
    primaryError ||= rendererFailure();
    if(primaryError){state.primaryError=errorRecord(primaryError);state.cleanupErrors.push(...(primaryError.fixtureCleanupErrors||[]))}
    // Execute every cleanup independently; an early restoration failure must
    // neither skip HTTP close nor replace the original test failure.
    for(const [name,operation] of cleanups)try{await bounded(operation,{label:label+' cleanup '+name,timeoutMs:3000})}catch(error){state.cleanupErrors.push({...errorRecord(error),errorName:error?.name,name})}
    for(const remove of listeners)try{remove()}catch(error){state.cleanupErrors.push({...errorRecord(error),name:'event listener removal'})}
    primaryError ||= rendererFailure();if(primaryError)state.primaryError=errorRecord(primaryError);
    state.completed=!primaryError&&!state.cleanupErrors.length;
    try{save()}catch(error){state.completed=false;state.diagnosticErrors.push(errorRecord(error));if(!primaryError)state.cleanupErrors.push({...errorRecord(error),name:'lifecycle report'})}
    if(primaryError){primaryError.fixtureCleanupErrors=state.cleanupErrors;throw primaryError}
    if(state.cleanupErrors.length){const error=new Error(label+' cleanup failed: '+state.cleanupErrors.map(item=>item.name+': '+item.message).join('; '));error.fixtureCleanupErrors=state.cleanupErrors;throw error}
  }
  return {state,read,wait,phase,semantic,visible,capture,reload,diagnose,finish,save};
}
async function cleanupPreservingError(primaryError,cleanups) {
  const errors=[];
  for(const [name,operation] of cleanups)try{await bounded(operation,{label:'fixture wrapper cleanup '+name,timeoutMs:3000})}catch(error){errors.push({...errorRecord(error),errorName:error?.name,name})}
  if(primaryError){primaryError.fixtureCleanupErrors=[...(primaryError.fixtureCleanupErrors||[]),...errors];throw primaryError}
  if(errors.length){const error=new Error('Fixture wrapper cleanup failed: '+errors.map(item=>item.name+': '+item.message).join('; '));error.fixtureCleanupErrors=errors;throw error}
}
module.exports={bounded,closeFixtureServer,createFixtureLifecycle,cleanupPreservingError,stableGeometry,assertGeometryGroups,paintScript,semanticScript,rendererStateScript};
