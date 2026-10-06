/* Local Electron DOM/input proof, never a Windows/CDP account integration pass.
 * Run under Electron. All HTTP(S)/WebSocket requests are denied. The viewport is
 * a controlled CDP-emulated fixture so this also works on headless Linux. */
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const {createHash}=require('node:crypto');
const root=path.resolve(__dirname,'../..');
const zip=process.env.IGAC_CROP_BASELINE_ZIP;
const exported=spawnSync(process.env.PYTHON||'python',[path.join(root,'scripts/crop_icon_fixture.py'),...(zip?[zip]:[])],{encoding:'utf8'});
if(exported.status!==0)throw Error(exported.stderr||'Fixture export failed');
const data=JSON.parse(exported.stdout);
const sourceFiles=['backend/app/instagram_crop.py','backend/app/instagram_crop_dom.py','backend/app/instagram_publisher.py','backend/tests/test_crop_icon_r64.py','scripts/crop_icon_fixture.py','desktop/tests/crop-icon-r64.cjs'];
const report={schema:1,verified:false,synthetic_offline:true,live_accounts_tested:false,source_commit:process.env.GITHUB_SHA||null,platform:process.platform,offline:true,emulated_viewport:{width:1274,height:717},baseline:data.baseline,electron:process.versions.electron,chromium:process.versions.chrome,external_requests:[],scenarios:[],probe_sha256:createHash('sha256').update(data.probe).digest('hex'),source_hashes:Object.fromEntries(sourceFiles.map(name=>[name,createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex')]))};
Object.assign(report,require('../../scripts/local_source_binding.cjs').collectSourceIdentity(root));
app.disableHardwareAcceleration();
const deadline=setTimeout(()=>{console.error('Crop DOM fixture exceeded its deadline');app.exit(2)},45000);
app.whenReady().then(async()=>{
 const w=new BrowserWindow({show:false,width:1274,height:717,webPreferences:{sandbox:false,offscreen:true}});
 w.webContents.session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']},(d,c)=>{report.external_requests.push(d.url);c({cancel:true});});
 w.webContents.debugger.attach('1.3');
 const run=code=>w.webContents.executeJavaScript(code);
 const click=async selector=>{
  const r=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const r=e.getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})()`);
  w.webContents.sendInputEvent({type:'mouseDown',button:'left',clickCount:1,...r});w.webContents.sendInputEvent({type:'mouseUp',button:'left',clickCount:1,...r});
  const start=performance.now(),before=await run('trustedClicks.length');
  // Input acknowledgement is a recorded trusted click, not a fixed sleep.
  for(;;){const after=await run('trustedClicks.length');if(after>=before&&after>click.count){click.count=after;break;}if(performance.now()-start>3000)throw Error('Native click was not acknowledged: '+selector);await new Promise(r=>setTimeout(r,10));}
 };
 for(const [variant,html] of Object.entries(data.fixtures)){
  await w.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(html));
  await w.webContents.debugger.sendCommand('Emulation.setDeviceMetricsOverride',{width:1274,height:717,deviceScaleFactor:1,mobile:false});
  await run('openComposer();crop();window.trustedClicks=[];document.addEventListener("click",e=>trustedClicks.push(e.isTrusted))');click.count=0;
  if(variant==='duplicate')await run("document.querySelector('#ratio').setAttribute('data-juxin-crop','stale')");
  if(variant==='unlabelled'&&process.env.IGAC_CROP_FIXTURE_SCREENSHOT){const shot=await w.webContents.debugger.sendCommand('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(process.env.IGAC_CROP_FIXTURE_SCREENSHOT,Buffer.from(shot.data,'base64'));}
  const probe=await run(`(${data.probe})(document.querySelector('#composer'),'fixture')`);
  const shouldPass=data.baseline?['aria','labelled_precedence'].includes(variant):['unlabelled','title','aria','roleless','labelled_precedence','scaled'].includes(variant);
  if(data.baseline)assert.equal(probe,shouldPass,variant+' baseline discovery');
  else assert.equal(probe.count,variant==='duplicate'?2:shouldPass?1:0,variant+' discovery');
  if(shouldPass){
   await click('[data-juxin-crop="fixture"]');assert.equal(await run("document.querySelector('#ratios').hidden"),false);
   await click('#original');assert.equal(await run("document.querySelector('#ratios').hidden"),true);
   assert.equal(await run(`(${data.forward})(document.querySelector('#composer'),'fixture')`),true);
   await click('[data-juxin-forward="fixture"]');assert.deepEqual(await run('cropEvents'),['menu','original','next','edit-stage']);
   assert.deepEqual(await run('trustedClicks'),[true,true,true]);
  }else{
   assert.deepEqual(await run('cropEvents'),[]);
   if(!data.baseline)assert.equal(await run("document.querySelectorAll('[data-juxin-crop]').length"),0,'stale or ambiguous markers must be cleared');
  }
  assert.deepEqual(await run('[wrongClicks,shares]'),[0,0]);
  report.scenarios.push({variant,probe,clicked:shouldPass,trusted_clicks:await run('trustedClicks')});
  console.log('PASS',variant,JSON.stringify(probe));
 }
 assert.deepEqual(report.external_requests,[]);
 if(process.env.IGAC_CROP_FIXTURE_SCREENSHOT){const bytes=fs.readFileSync(process.env.IGAC_CROP_FIXTURE_SCREENSHOT);assert.ok(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));report.capture={file:path.basename(process.env.IGAC_CROP_FIXTURE_SCREENSHOT),sha256:createHash('sha256').update(bytes).digest('hex')};}
 require('../../scripts/local_source_binding.cjs').assertSourceIdentity(report,{root});
 report.verified=true;
 if(process.env.IGAC_CROP_FIXTURE_REPORT)fs.writeFileSync(process.env.IGAC_CROP_FIXTURE_REPORT,JSON.stringify(report,null,2));
 console.log('PASS all '+report.scenarios.length+' offline DOM/input scenarios; no external requests or shares');
 w.destroy();clearTimeout(deadline);app.exit(0);
}).catch(e=>{console.error(e);clearTimeout(deadline);app.exit(1)});
