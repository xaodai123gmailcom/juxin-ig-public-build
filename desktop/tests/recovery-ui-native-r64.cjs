/* Mandatory offline R6.4 recovery UI gate. Production App and handlers; synthetic
 * transport and confirm-response seam. Native clicks/keys only, never DOM click.
 * Standalone: electron desktop/tests/recovery-ui-native-r64.cjs
 * Integrated: await require('./recovery-ui-native-r64.cjs')({outputDirectory})
 * JUXIN_REQUIRE_RECOVERY_UI_NATIVE=1 refuses non-Windows release evidence.
 * Does not claim installed-Core, user DB, live Instagram or OS-dialog proof. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {createHash,randomUUID}=require('node:crypto');
const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const {prepareVisibleFixture}=require('./visible-fixture.cjs');
const ROOT=path.resolve(__dirname,'../..'),ORIGIN='https://r64-recovery.invalid';
const SCENARIOS=Object.freeze(['nonpostingRoutesOnly', 'cleanupRefusesPausedBlocker', 'hiddenBlockerExactIdentity', 'repeatedLookupCollapsed', 'cancelStopNoMutation', 'liveBlockerStopDisabled', 'staleStopRejected', 'stopPreservesHoldAndHistory', 'stopFeedbackVisible', 'separateCleanupIntent', 'cleanedWindowAvailable']);
const sha256=bytes=>createHash('sha256').update(bytes).digest('hex');
async function buildFixture(){return require('esbuild').build({absWorkingDir:ROOT,entryPoints:['renderer/tests/fixtures/recovery-ui-r64.tsx'],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',metafile:true,define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});}
function assertProofComplete(proof){
 assert.equal(proof.schema,2);assert.equal(proof.gate,'r64-recovery-ui-native');assert.equal(proof.native_runtime,true);assert.equal(proof.synthetic_offline,true);assert.equal(proof.cleanup_verified,true);
 assert.deepEqual(proof.external_requests,[]);assert.deepEqual(proof.external_actions,[]);assert.deepEqual(proof.renderer_errors,[]);assert.deepEqual(proof.unexpected_endpoints,[]);
 assert.deepEqual(Object.keys(proof.scenarios).sort(),[...SCENARIOS].sort());for(const key of SCENARIOS)assert.equal(proof.scenarios[key],true,key);
 assert.ok(proof.native_input.length>=15);for(const event of proof.native_input)assert.equal(event.trusted,true,'untrusted UI input');
 assert.equal(proof.confirmation_mode,'production-window.confirm/controlled-response');assert.equal(proof.scope.os_dialog_automation,false);
 require('../../scripts/local_source_binding.cjs').assertSourceIdentity(proof,{root:ROOT,required:proof.required_mode});if(proof.github_sha)assert.equal(proof.source_commit,proof.github_sha);
 for(const file of ['renderer/src/App.tsx','renderer/src/formal-workbench.tsx','renderer/src/nurture-collection-blocker.tsx','renderer/src/standalone-nurture-workspace.tsx','renderer/tests/fixtures/recovery-ui-r64.tsx','desktop/tests/recovery-ui-native-r64.cjs'])assert.match(proof.source_sha256[file],/^[0-9a-f]{64}$/);
 assert.equal(proof.start_intents.length,0);assert.equal(proof.stop_intents.length,2);assert.equal(proof.successful_stops,1);assert.equal(proof.cleanup_successes,1);
 if(proof.required_mode){assert.equal(proof.platform,'win32','Windows release proof required');assert.equal(proof.windows_release_status,'passed');assert.equal(proof.headless,false);assert.equal(proof.native_window.visible,true);for(const shot of proof.screenshots){assert.equal(shot.native_visible,true);assert.equal(shot.renderer_visibility,'visible')}}
 else assert.equal(proof.windows_release_status,proof.platform==='win32'?'passed':'pending');
 assert.deepEqual(proof.screenshots.map(shot=>shot.file).sort(),['hidden-blocker','stopped-feedback','cleaned-window'].map(name=>'r64-recovery-ui-'+name+'.png').sort());for(const shot of proof.screenshots){assert.match(shot.sha256,/^[0-9a-f]{64}$/);assert.ok(shot.bytes>33);assert.ok(shot.size.width>0&&shot.size.height>0)}
}
async function run({outputDirectory=path.join(ROOT,'installer-output'),win:owner,timeoutMs=150000}={}){
 const {BrowserWindow,app}=require('electron');const headless=process.platform!=='win32'&&app.commandLine.getSwitchValue('ozone-platform')==='headless';assert.ok(process.versions.electron,'native UI gate must run in Electron');
 fs.mkdirSync(outputDirectory,{recursive:true});
 const resultPath=path.join(outputDirectory,'r64-recovery-ui-native-proof.json'),failurePath=path.join(outputDirectory,'r64-recovery-ui-native.failure.json');
 for(const file of [resultPath,failurePath])fs.rmSync(file,{force:true});
 const proof={schema:2,gate:'r64-recovery-ui-native',verified:false,synthetic_offline:true,native_runtime:true,required_mode:process.env.JUXIN_REQUIRE_RECOVERY_UI_NATIVE==='1',platform:process.platform,electron:process.versions.electron,chromium:process.versions.chrome,github_sha:process.env.GITHUB_SHA||null,windows_release_status:'pending',confirmation_mode:'production-window.confirm/controlled-response',scope:{production_app:true,production_handlers:true,native_input:true,installed_core:false,user_database:false,live_instagram:false,share:false,os_dialog_automation:false},scenarios:{},observations:[],external_requests:[],external_actions:[],renderer_errors:[],native_input:[],screenshots:[],source_sha256:{}};
 let win,timer;const started=performance.now();
 const remaining=()=>{const left=timeoutMs-(performance.now()-started);if(left<=0)throw Error('R6.4 recovery UI whole-process deadline exceeded');return Math.min(left,12000)};
 const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{if(win&&!win.isDestroyed())win.destroy();reject(Error('R6.4 recovery UI whole-process deadline exceeded'))},timeoutMs)});
 const execute=async()=>{
  if(proof.required_mode)assert.equal(process.platform,'win32','required R6.4 native UI release proof requires Windows');
  Object.assign(proof,require('../../scripts/local_source_binding.cjs').collectSourceIdentity(ROOT));
  if(proof.github_sha)assert.equal(proof.source_commit,proof.github_sha,'CI checkout matches GITHUB_SHA');
  console.log('CHECK R64 compiling production App');const built=await buildFixture();console.log('CHECK R64 production App compiled');proof.headless=headless;
  for(const file of [...Object.keys(built.metafile.inputs).filter(file=>!file.includes('node_modules')), 'desktop/tests/recovery-ui-native-r64.cjs','desktop/tests/renderer-fixture.cjs','desktop/tests/visible-fixture.cjs'])proof.source_sha256[file]=sha256(fs.readFileSync(path.resolve(ROOT,file)));
  const assets=new Map(built.outputFiles.map(file=>['/'+path.basename(file.path),file.contents]));
  win=new BrowserWindow({show:!headless,width:1440,height:1050,webPreferences:{offscreen:headless,sandbox:!headless,contextIsolation:true,nodeIntegration:false,partition:'r64-recovery-'+randomUUID()}});
  win.setContentSize(1440,1050);const wc=win.webContents,session=wc.session;
  session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
  session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']},(details,callback)=>{const allowed=new URL(details.url).origin===ORIGIN&&details.method==='GET';if(!allowed)proof.external_requests.push(details.url);callback({cancel:!allowed})});
  wc.on('render-process-gone',(_event,details)=>proof.renderer_errors.push('Renderer gone: '+details.reason));
  wc.setWindowOpenHandler(({url})=>{proof.external_actions.push({action:'window-open',url});return {action:'deny'}});
  wc.on('console-message',(_event,...args)=>{const data=args[0]&&typeof args[0]==='object'?args[0]:_event?.message?{level:_event.level,message:_event.message}:{level:args[0],message:args[1]};if(data.level==='error'||data.level===3)proof.renderer_errors.push(data.message)});
  await session.protocol.handle('https',request=>{const url=new URL(request.url);if(url.origin!==ORIGIN||request.method!=='GET'){proof.external_requests.push(request.url);return new Response('Offline only',{status:403})}if(url.pathname==='/')return new Response('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\' \'unsafe-inline\'; img-src data: \'self\'; connect-src \'none\'; font-src \'self\'; form-action \'none\'; base-uri \'none\'"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>',{headers:{'content-type':'text/html; charset=utf-8'}});if(url.pathname==='/war-wolf.svg')return new Response(fs.readFileSync(path.join(ROOT,'renderer/public/war-wolf.svg')),{headers:{'content-type':'image/svg+xml'}});if(url.pathname==='/favicon.ico')return new Response(null,{status:204});const bytes=assets.get(url.pathname);return new Response(bytes||'Missing fixture asset',{status:bytes?200:404,headers:{'content-type':url.pathname.endsWith('.css')?'text/css; charset=utf-8':'text/javascript; charset=utf-8'}})});
  const read=source=>rendererFixtureRead(wc,source,{label:'R64 recovery UI read',timeoutMs:remaining()});
  const wait=(source,label)=>waitRendererFixture(wc,source,{label,timeoutMs:remaining()});
  const expr=selector=>`document.querySelector(${JSON.stringify(selector)})`;
  const textButton=(label,root='document')=>`[...${root}.querySelectorAll('button')].find(n=>n.textContent.trim()===${JSON.stringify(label)})`;
  const buttons={cleanup:'[aria-label="核验窗口清理任务 held-r64"]'};
  async function point(target,disabled=false){await wait(`!!(${target})`,'native target exists');await read(`(${target}).scrollIntoView({block:'center',inline:'nearest'});true`);const p=await wait(`(()=>{const n=${target};const r=n.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);return r.width>0&&r.height>0&&x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(h===n||n.contains(h))&&(${disabled?'true':'!n.disabled'})?{x:Math.round(x),y:Math.round(y),disabled:!!n.disabled}:null})()`,'native hit test');return p}
  async function click(target,{disabled=false,repeat=1}={}){const p=await point(target,disabled);wc.focus();for(let i=0;i<repeat;i++){wc.sendInputEvent({type:'mouseMove',x:p.x,y:p.y});wc.sendInputEvent({type:'mouseDown',x:p.x,y:p.y,button:'left',clickCount:1});wc.sendInputEvent({type:'mouseUp',x:p.x,y:p.y,button:'left',clickCount:1})}await read('new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))');return p}
  const clickSelector=(selector,options)=>click(expr(selector),options);
  const clickText=(text,options)=>click(textButton(text),options);
  const mark=async(name,evidence={})=>{proof.scenarios[name]=true;proof.observations.push({scenario:name,...evidence});console.log('CHECK R64 recovery UI '+name)};
  const commands=()=>read('recoveryFixture.commands');
  const count=action=>read(`recoveryFixture.commands.filter(c=>c.body.action===${JSON.stringify(action)}).length`);
  const arm=action=>read(`recoveryFixture.holdAction=${JSON.stringify(action)};true`);
  const held=action=>wait(`recoveryFixture.pendingAction===${JSON.stringify(action)}&&typeof recoveryFixture.release==='function'`,'explicit deferred '+action+' acknowledgement');
  const release=()=>read('recoveryFixture.release();true');
  async function capture(name){await read('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))');const image=await wc.capturePage();assert.equal(image.isEmpty(),false);const png=image.toPNG(),file='r64-recovery-ui-'+name+'.png',renderer_visibility=await read('document.visibilityState');fs.writeFileSync(path.join(outputDirectory,file),png);proof.screenshots.push({file,bytes:png.length,sha256:sha256(png),size:image.getSize(),native_visible:win.isVisible(),renderer_visibility})}
  async function navigate(mode){await click(`document.querySelector('a[href="#/${mode}"]')`);await wait(`location.hash==='#/${mode}'`,'production navigation '+mode)}
  await win.loadURL(ORIGIN+'/#/nurture');if(!headless)await prepareVisibleFixture(win,{label:'R64 recovery UI',timeoutMs:remaining()});proof.native_window={visible:win.isVisible(),offscreen:headless,content_size:win.getContentSize()};
  await wait(`!!${expr(buttons.cleanup)}`,'production cleanup held card');
  const preserved=await read('({history:structuredClone(recoveryFixture.history)})');
  const navigation=await read("[...document.querySelectorAll('.formal-nav a')].map(node=>node.dataset.nav)");
  assert.equal(navigation.includes('posting'),false);assert.equal(await read("document.body.textContent.includes('发帖')"),false);await mark('nonpostingRoutesOnly');
  await clickSelector(buttons.cleanup);await wait("document.querySelector('[data-job-id=held-r64] [role=alert]')?.textContent.includes('关联采集任务仍已暂停')",'cleanup refused by paused blocker');assert.equal(await read('recoveryFixture.studio.jobs[0].result.window_hold'),true);await mark('cleanupRefusesPausedBlocker');
  await arm('locate_cleanup_collection');await clickText('定位关联采集任务',{repeat:2});await held('locate_cleanup_collection');await clickText('定位关联采集任务',{disabled:true});assert.equal(await count('locate_cleanup_collection'),1);await mark('repeatedLookupCollapsed');await release();
  await wait("document.querySelector('[data-job-id=held-r64]').textContent.includes('任务编号：86feb8fc-f2a6-404f-9459-11dd3f884103')",'exact hidden collection blocker rendered');
  const located=await read("document.querySelector('[data-job-id=held-r64]').textContent");assert.match(located,/关联窗口：1号窗口、2号窗口/);assert.match(located,/任务列表已归档/);assert.equal(await read('recoveryFixture.snapshot.tasks.length'),0);await mark('hiddenBlockerExactIdentity',{task_id:'86feb8fc-f2a6-404f-9459-11dd3f884103',windows:['1号窗口','2号窗口'],absent_from_task_list:true});await capture('hidden-blocker');
  await clickText('停止这条暂停采集任务');await wait('recoveryFixture.confirmations.length===1','cancel acknowledgement');assert.equal(await count('stop_cleanup_collection'),0);assert.equal(await read('recoveryFixture.confirmations[0].accepted'),false);await mark('cancelStopNoMutation');
  await read("recoveryFixture.mode='live';true");await clickText('定位关联采集任务');await wait(`${textButton('停止这条暂停采集任务')}.disabled`,'live blocker disabled');await clickText('停止这条暂停采集任务',{disabled:true});assert.equal(await count('stop_cleanup_collection'),0);await mark('liveBlockerStopDisabled');
  await read("recoveryFixture.mode='normal';true");await clickText('定位关联采集任务');await wait(`!${textButton('停止这条暂停采集任务')}.disabled`,'safe blocker restored');
  await read("recoveryFixture.mode='stale';recoveryFixture.confirmNext=true;true");await clickText('停止这条暂停采集任务');await wait("document.querySelector('[data-job-id=held-r64]').textContent.includes('关联任务已变化')&&!document.body.textContent.includes('停止这条暂停采集任务')",'stale target discarded');assert.equal(await read('recoveryFixture.stops'),0);await mark('staleStopRejected');
  await read("recoveryFixture.mode='normal';true");await clickText('定位关联采集任务');await wait(`!!${textButton('停止这条暂停采集任务')}&&!${textButton('停止这条暂停采集任务')}.disabled`,'fresh exact target');
  await arm('stop_cleanup_collection');await read('recoveryFixture.confirmNext=true;true');await clickText('停止这条暂停采集任务',{repeat:2});await held('stop_cleanup_collection');await clickText('停止这条暂停采集任务',{disabled:true});assert.equal(await count('stop_cleanup_collection'),2,'one stale refusal plus one accepted stop');await release();
  await wait("document.querySelector('[data-job-id=held-r64]').textContent.includes('已正常停止这条暂停采集任务，请再次核验窗口清理')",'safe stop feedback survives refresh');
  assert.equal(await read('recoveryFixture.studio.jobs[0].result.window_hold&&recoveryFixture.snapshot.windows[0].locked&&recoveryFixture.cleanups===0'),true);assert.deepEqual(await read('recoveryFixture.history'),preserved.history);await mark('stopPreservesHoldAndHistory');
  const confirmation=await read('recoveryFixture.confirmations.at(-1)');assert.equal(confirmation.accepted,true);assert.match(confirmation.message,/86feb8fc-f2a6-404f-9459-11dd3f884103/);assert.match(confirmation.message,/1号窗口、2号窗口/);assert.match(confirmation.message,/保留采集结果、历史、归档和去重记录/);await mark('stopFeedbackVisible',{confirmation});await capture('stopped-feedback');
  await arm('retry_cleanup');await clickSelector(buttons.cleanup,{repeat:2});await held('retry_cleanup');assert.equal(await read("recoveryFixture.commands.filter(c=>c.body.operation==='retry_cleanup').length"),2,'one refusal then separate explicit cleanup');await release();
  await wait("document.body.textContent.includes('已核验窗口关闭，本条历史清理占用已解除')&&!document.querySelector('[aria-label=\"核验窗口清理任务 held-r64\"]')",'separate cleanup acknowledged');assert.equal(await read('recoveryFixture.studio.jobs[0].result.window_hold'),false);assert.equal(await read('recoveryFixture.starts'),0);await mark('separateCleanupIntent');
  await wait("!!document.querySelector('[data-window-id=w1] input[type=checkbox]')&&!document.querySelector('[data-window-id=w1] input[type=checkbox]').disabled",'released window selection available');
  assert.equal(await read('recoveryFixture.snapshot.windows[0].locked'),false);await mark('cleanedWindowAvailable');await capture('cleaned-window');
  const all=await commands();proof.commands=all;proof.start_intents=all.filter(c=>c.body.action==='start');proof.stop_intents=all.filter(c=>c.body.action==='stop_cleanup_collection');
  for(const stop of proof.stop_intents)assert.deepEqual(stop.body,{action:'stop_cleanup_collection',job_id:'held-r64',task_id:'86feb8fc-f2a6-404f-9459-11dd3f884103',version:7});
  proof.native_input=await read('recoveryFixture.inputEvents');proof.unexpected_endpoints=await read('recoveryFixture.unexpected');proof.successful_stops=await read('recoveryFixture.stops');proof.cleanup_successes=await read('recoveryFixture.cleanups');proof.confirmations=await read('recoveryFixture.confirmations');
  assert.deepEqual(await read('recoveryFixture.history'),preserved.history);assert.equal(await read('recoveryFixture.starts'),0);
  win.destroy();assert.equal(win.isDestroyed(),true);proof.cleanup_verified=true;proof.windows_release_status=process.platform==='win32'?'passed':'pending';proof.timing={elapsed_ms:Math.round(performance.now()-started),overall_limit_ms:timeoutMs};assertProofComplete(proof);proof.verified=true;proof.checked_at=new Date().toISOString();fs.writeFileSync(resultPath,JSON.stringify(proof,null,2));console.log('PASS R64 native recovery UI proof: '+resultPath);return proof;
 };
 try{return await Promise.race([execute(),deadline])}catch(error){if(win&&!win.isDestroyed()){try{proof.failure_dom=await rendererFixtureRead(win.webContents,"({url:location.href,ready:document.readyState,text:document.body.innerText,html:document.body.innerHTML.slice(0,12000),fixture:typeof recoveryFixture,unexpected:window.recoveryFixture?.unexpected,commands:window.recoveryFixture?.commands,input:window.recoveryFixture?.inputEvents,active:document.activeElement?.outerHTML})",{timeoutMs:2000})}catch(diagnostic){proof.failure_dom_error=String(diagnostic)}}proof.error=String(error?.stack||error);proof.checked_at=new Date().toISOString();fs.writeFileSync(failurePath,JSON.stringify(proof,null,2));throw error}finally{clearTimeout(timer);if(win&&!win.isDestroyed())win.destroy();if(owner&&!owner.isDestroyed()){owner.show();owner.focus()}}
}
function isStandaloneEntry({isMain=require.main===module,electronVersion=process.versions.electron,entry=process.argv.slice(1).find(value=>!value.startsWith('-'))}={}){return isMain||Boolean(electronVersion&&entry&&path.resolve(entry)===__filename)}
module.exports=run;Object.assign(module.exports,{run,SCENARIOS,buildFixture,assertProofComplete,isStandaloneEntry});
if(isStandaloneEntry()){
 console.log('CHECK R64 native recovery UI entry');
 if(!process.versions.electron){console.error('Run this native UI gate with Electron');process.exitCode=1}
 else{const {app}=require('electron');app.setPath('userData',fs.mkdtempSync(path.join(require('node:os').tmpdir(),'r64-recovery-ui-')));app.commandLine.appendSwitch('disable-background-networking');app.on('window-all-closed',()=>{});const hardStop=setTimeout(()=>{console.error('R64 recovery UI startup/process deadline');app.exit(1)},180000);app.whenReady().then(()=>run()).then(()=>{clearTimeout(hardStop);app.exit(0)},error=>{clearTimeout(hardStop);console.error(error);app.exit(1)})}
}
