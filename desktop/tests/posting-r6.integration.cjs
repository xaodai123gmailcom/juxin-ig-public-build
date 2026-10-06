/* Real production App / PostingWorkspace with isolated synthetic data.
   Windows gate: require('./posting-r6.integration.cjs')({win,host})
   Standalone: npx electron desktop/tests/posting-r6.integration.cjs */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {createFixtureLifecycle,closeFixtureServer,cleanupPreservingError,stableGeometry,assertGeometryGroups}=require('./fixture-lifecycle.cjs');
async function buildFixture(){return require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/posting-r6.tsx')],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});}
const postingLayoutScript=`(()=>{
 const rect=n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);return {id:n.getAttribute('data-posting-job'),width:r.width,height:r.height,left:r.left,right:r.right,top:r.top,bottom:r.bottom,clientWidth:n.clientWidth,scrollWidth:n.scrollWidth,clientHeight:n.clientHeight,scrollHeight:n.scrollHeight,display:s.display,padding:s.padding,minHeight:s.minHeight,children:[...n.children].map(c=>{const b=c.getBoundingClientRect();return {className:c.className,width:b.width,height:b.height,top:b.top,bottom:b.bottom,text:c.textContent?.slice(0,180)}})}};
 return {width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,cards:[...document.querySelectorAll('.posting-total')].map(rect),rows:[...document.querySelectorAll('.inline-task')].map(rect)};
})()`;
function stablePostingLayout(evaluate,width,options={}){return stableGeometry(evaluate,postingLayoutScript,width,{label:'Posting layout',groups:{cards:4,rows:3},...options})}
function assertPostingLayout(layout,width,height){
 if(height!==undefined)assert.equal(layout.height,height,'posting viewport must match requested height');
 assert.equal(layout.width,width,'posting viewport at '+width+'px');
 assertGeometryGroups(layout,{cards:4,rows:3});
 assert.ok(layout.scroll<=layout.width+1,'posting page overflow at '+width+'px: '+layout.scroll+'/'+layout.width);
 for(const [name,group] of [['cards',layout.cards],['rows',layout.rows]])for(const [index,r] of group.entries()){
  const label=name+'['+index+']'+(r.id?' '+r.id:'')+' at '+width+'px';
  assert.ok(Math.abs(r.width-group[0].width)<=1,label+' unequal width '+r.width+'/'+group[0].width);
  assert.ok(Math.abs(r.height-group[0].height)<=1,label+' unequal height '+r.height+'/'+group[0].height);
  assert.ok(r.right<=layout.width+1,label+' right edge outside viewport');
 }
}
const postingCasRejectionMessage='任务内容或排队状态在审阅后发生变化，请重新检查';
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
  const settled=()=>lifecycle.semantic('.posting-workspace');
  const screenshot=async(name,selector)=>{if(selector)await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start'});true`);await settled();fs.mkdirSync('installer-output',{recursive:true});fs.writeFileSync(path.resolve('installer-output/'+name),(await lifecycle.capture(name,{width:requestedWidth,height:1100})).toPNG());};
  const geometryEvidence=async(name,width,layout,error)=>{
    fs.mkdirSync('installer-output',{recursive:true});
    lifecycle.state.lastGeometry=layout;
    const evidence={requestedWidth:width,nativeSize:win.getContentSize(),layout,error:error?String(error):null};
    fs.writeFileSync(path.resolve('installer-output/'+name+'.json'),JSON.stringify(evidence,null,2));
    if(!error)fs.writeFileSync(path.resolve('installer-output/'+name+'.png'),(await lifecycle.capture(name,{width,height:1100})).toPNG());
    console.log('CHECK posting geometry '+JSON.stringify(evidence));
  };
  let requestedWidth=1598;
  const originalSize=win.getContentSize(),originalMinimum=win.getMinimumSize();
  let primaryError;
  try{
    await lifecycle.phase('listen on loopback',()=>new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)}));host?.hide();
    win.setMinimumSize(0,0);win.setContentSize(1598,1000);
    await lifecycle.phase('load posting route',()=>win.loadURL('http://127.0.0.1:'+server.address().port+'/#/posting'),{timeoutMs:30000});
    await lifecycle.visible('R6 posting foreground');
    await wait("document.querySelectorAll('.inline-task').length===3",'real posting rows');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.posting-total strong')].map(n=>n.textContent)"),['3','12','2','4']);
    assert.equal(await evaluate("[...document.querySelectorAll('.inline-window select')].every(s=>s.querySelector('option[value=locked]').disabled)"),true);
    assert.equal(await evaluate("[...document.querySelectorAll('button')].find(n=>n.textContent.includes('生成任务')).disabled"),true,'unconfigured key blocks generation');
    assert.equal(await evaluate('postingFixture.commands.length'),0,'no automatic validation');
    for(const width of [1598,1440,1280]){
      requestedWidth=width;lifecycle.state.requestedWidth=width;win.setContentSize(width,1100);await settled();
      const layout=await stablePostingLayout(evaluate,width,{height:1100,nativeSize:()=>win.getContentSize()});
      await geometryEvidence('r63-posting-layout-'+width,width,layout);
      assertPostingLayout(layout,width);
    }
    requestedWidth=1598;win.setContentSize(1598,1100);await stablePostingLayout(evaluate,1598,{height:1100,nativeSize:()=>win.getContentSize()});await screenshot('r6-posting-actual.png','.posting-workspace');
    const aria=label=>evaluate('document.querySelector('+JSON.stringify('button[aria-label="'+label+'"]')+').click();true');
    await aria('启动任务 first');await wait("document.querySelector('.review-dialog')?.textContent.includes('保持原始文案 first')",'review');
    await evaluate("postingFixture.state.jobs[0].caption='changed after review';true");
    await click('确认向以上账号发布 1 条');await wait(`document.querySelector('.review-dialog [role=alert]')?.textContent===${JSON.stringify('Error: '+postingCasRejectionMessage)}`,'CAS rejection');
    assert.equal(await evaluate('postingFixture.commands.at(-1).reviewed[0].caption'),'保持原始文案 first');
    await click('返回检查');
    await evaluate('postingFixture.holdSnapshot=true;postingFixture.poll();true');await wait("typeof postingFixture.releaseSnapshot==='function'",'held stale read');
    await aria('编辑任务 second');
    await evaluate("(()=>{const n=document.querySelector('.edit-caption');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(n,'fresh edit');n.dispatchEvent(new Event('input',{bubbles:true}));return true})()");
    await click('保存文案');await wait("!document.querySelector('.review-dialog')",'edit saved');
    await evaluate('postingFixture.releaseSnapshot();true','release held snapshot');await wait('postingFixture.releaseSnapshot===null','held snapshot response released');await settled();
    assert.equal(await evaluate("document.querySelector('.inline-task-list').textContent.includes('fresh edit')"),true,'stale response discarded');
    await click('下一页');await wait("document.querySelector('[aria-label=\"检查任务 older\"]')!==null",'older task accessible');
    assert.equal(await evaluate('postingFixture.reads.at(-1).includes("cursor=page-2")'),true);
    await click('上一页');await wait("document.querySelectorAll('.inline-task').length===3",'first page restored');
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="start").length'),1);
    await evaluate('postingFixture.holdSnapshot=true;postingFixture.poll();true');await wait("typeof postingFixture.releaseSnapshot==='function'",'held credential read');
    await click('安全配置');
    await evaluate("(()=>{const n=document.querySelector('input[type=password]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(n,'offline-fixture-only');n.dispatchEvent(new Event('input',{bubbles:true}));return true})()");
    await click('安全保存');await wait("!document.querySelector('input[type=password]')",'write-only input cleared');
    await evaluate('postingFixture.releaseSnapshot();true','release held snapshot');await wait('postingFixture.releaseSnapshot===null','held snapshot response released');await settled();
    assert.equal(await evaluate("document.querySelector('.posting-workspace .notice').textContent.includes('Pexels 已配置')"),true,'old credential read discarded');
    // Actual production-style long identity and large-counter stress, not short-name inference.
    await evaluate("postingFixture.snapshot.windows[0].name='这是一个用于验证中文长窗口名称完整可读与对齐的测试窗口';postingFixture.state.accounts[0].username='abcdefghijklmnopqrstuvwx123456';postingFixture.state.jobs.forEach(j=>j.expected_username='abcdefghijklmnopqrstuvwx123456');postingFixture.state.totals={waiting:1234567890123,success:1234567890123,failed:1234567890123,today_success:1234567890123,needs_review:1};document.querySelector('.formal-brand').click();postingFixture.poll();true");
    await wait("document.querySelector('.inline-window select').textContent.includes('这是一个用于验证')",'long window refreshed');
    requestedWidth=1280;win.setContentSize(1280,1100);await stablePostingLayout(evaluate,1280,{height:1100,nativeSize:()=>win.getContentSize()});
    await screenshot('r6-posting-stress.png','.posting-workspace');
    const stress=await evaluate("(()=>{const groups=['.inline-window small','.inline-window select','.posting-total'];return groups.flatMap(selector=>[...document.querySelectorAll(selector)].map(n=>({selector,client:n.clientWidth,scroll:n.scrollWidth,rect:n.getBoundingClientRect().toJSON()})))})()");
    fs.writeFileSync('installer-output/r6-posting-stress.json',JSON.stringify(stress,null,2));
    const bounds=await evaluate("(()=>{const rect=n=>n.getBoundingClientRect().toJSON();return {windows:[...document.querySelectorAll('.inline-task')].map(n=>({window:rect(n.querySelector('.inline-window')),material:rect(n.querySelector('.material-state'))})),cards:[...document.querySelectorAll('.posting-total')].map(n=>({card:rect(n),children:[...n.querySelectorAll('span,small,strong')].map(rect)}))}})()");
    fs.writeFileSync('installer-output/r6-posting-stress-bounds.json',JSON.stringify(bounds,null,2));
    for(const row of bounds.windows)assert.ok(row.window.right<=row.material.x,'window cell must not overlap material cell');
    for(const row of bounds.cards)for(const child of row.children)assert.ok(child.x>=row.card.x&&child.right<=row.card.right&&child.y>=row.card.y&&child.bottom<=row.card.bottom,'large metric child must stay inside card');
    assert.equal(await evaluate("document.querySelector('.inline-window select').title.includes('这是一个用于验证')&&document.querySelector('.inline-window small').title.includes('abcdefghijklmnopqrstuvwx123456')"),true,'full identity accessible through title');

    for(const item of stress)assert.ok(item.scroll<=item.client+1,'long content overflows '+item.selector+': '+item.scroll+'/'+item.client);
    // R6.2: render the actual missing-retry scenario and operate real React
    // controls. Fixture commands stay offline and must never imply a Share.
    await evaluate(`Object.assign(postingFixture.state.jobs[0],{status:'failed',failure_stage:'pre_submit',failure_code:'browser_open_failed',message:'任务浏览器未能打开或连接；请检查窗口后重试，尚未发布',retry_action:'review',retry_blocked_reason:'',can_modify:true,can_retry_cleanup:false});
      Object.assign(postingFixture.state.jobs[1],{status:'failed',failure_stage:'pre_submit',window_held:true,can_modify:false,can_retry_cleanup:true,retry_action:null,retry_blocked_reason:'请先关闭任务窗口',message:'窗口关闭尚未确认，保留占用'});
      Object.assign(postingFixture.state.jobs[2],{status:'needs_review',failure_stage:'unknown',submitted_at:'2026-10-03T00:00:00Z',window_held:true,retry_action:null,can_modify:false,can_retry_cleanup:false,message:'提交结果待核验，禁止重发'});
      postingFixture.poll();true`);
    await wait("document.querySelector('[aria-label=\"重试并检查任务 first\"]')!==null",'safe retry text action');
    assert.equal(await evaluate("document.querySelector('[data-posting-job=first] .task-failure-message').textContent.includes('尚未发布')"),true);
    assert.equal(await evaluate("document.querySelector('[data-posting-job=unknown] .posting-retry')===null"),true,'unknown never offers resend');
    assert.equal(await evaluate("document.querySelector('[aria-label=\"启动任务 unknown\"]').disabled"),true);
    const retryLayout=await stablePostingLayout(evaluate,1280,{height:1100,nativeSize:()=>win.getContentSize()});
    await geometryEvidence('r63-posting-retry-layout-1280',1280,retryLayout);
    assertPostingLayout(retryLayout,1280);
    await screenshot('r62-posting-retry-actual.png','.inline-task-list');
    const startsBeforeRetry=await evaluate('postingFixture.commands.filter(c=>c.action==="start").length');
    await evaluate("(()=>{const button=document.querySelector('[aria-label=\"重试并检查任务 first\"]');button.click();button.click();return true})()");
    await wait("document.querySelector('.review-dialog')?.textContent.includes('当前状态：待启动')",'retry returns to live review');
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="retry").length'),1,'repeated clicks admit one retry');
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="start").length'),startsBeforeRetry,'retry itself cannot publish');
    await click('返回检查');await aria('重试关闭任务 second');
    await wait("document.querySelector('[aria-label=\"重试并检查任务 second\"]')!==null",'close-only returns retry eligibility');
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="retry_cleanup").length'),1);
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="start").length'),startsBeforeRetry,'cleanup never publishes');
    await evaluate("Object.assign(postingFixture.state.jobs[0],{status:'failed',failure_stage:'preparation',retry_action:'prepare',asset_id:'',asset_state:'reserved',preview:'',message:'素材准备失败，请检查网络后重试'});postingFixture.poll();true");
    await wait("document.querySelector('[aria-label=\"重试准备任务 first\"]')!==null",'preparation retry action');
    await aria('重试准备任务 first');
    await wait("document.querySelector('[data-posting-job=first] .task-state').textContent==='后台准备中'",'retry only prepares');
    assert.equal(await evaluate('document.querySelector(".review-dialog")===null'),true);
    assert.equal(await evaluate('postingFixture.commands.filter(c=>c.action==="start").length'),startsBeforeRetry);
    console.log('PASS R6 posting: real App, four metrics, inline windows, missing key, review CAS, stale reads, paging, aligned desktop');
  }catch(error){
    primaryError=error;
    try{await geometryEvidence('r63-posting-failure',requestedWidth,lifecycle.state.lastGeometry,error)}catch(diagnosticError){console.error('CHECK posting geometry capture failed',String(diagnosticError))}
    await lifecycle.diagnose(error);throw error;
  }finally{await cleanupPreservingError(primaryError,[['restore dimensions',()=>{if(!win.isDestroyed()){win.setMinimumSize(...originalMinimum);win.setContentSize(...originalSize)}}],['HTTP server',()=>closeFixtureServer(server)]]);}
}
async function run({win:owner,host}={}){
  // A fresh nonpersistent session keeps synthetic account data isolated and gives
  // this fixture its own network guard without replacing the main app's policy.
  const {BrowserWindow}=require('electron');
  const isolated=new BrowserWindow({show:true,width:1598,height:1000,webPreferences:{sandbox:true,partition:'r6-posting-'+require('node:crypto').randomUUID()}});
  const external=[];
  isolated.webContents.session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(details,callback)=>{
    const url=new URL(details.url);const local=url.protocol==='http:'&&url.hostname==='127.0.0.1';
    if(!local)external.push(details.url);callback({cancel:!local});
  });
  isolated.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  const lifecycle=createFixtureLifecycle(isolated,{label:'R6 posting',entryMode:owner?'embedded':'standalone',sourceFile:__filename});
  let primaryError;
  try{await runInWindow({win:isolated,host,lifecycle});assert.deepEqual(external,[],'offline fixture never requests external services');}
  catch(error){primaryError=error;if(!lifecycle.state.primaryError)await lifecycle.diagnose(error)}
  finally{await lifecycle.finish(primaryError,[['destroy isolated window',()=>{if(!isolated.isDestroyed())isolated.destroy();assert.equal(isolated.isDestroyed(),true,'isolated fixture window is destroyed')}],['restore owner',()=>{if(owner&&!owner.isDestroyed()){owner.show();owner.focus()}}]]);}
}
module.exports=run;module.exports.buildFixture=buildFixture;
function isStandaloneEntry({isMain=require.main===module,electronVersion=process.versions.electron,entry=process.argv[1]}={}){
  // Electron may import a CJS entry through its ESM loader in a type:module
  // package. Exact argv identity also starts that entry, but never a helper
  // required by embedded-browser.integration.cjs or an explicit launcher.
  return isMain||Boolean(electronVersion&&entry&&path.resolve(entry)===__filename);
}
if(isStandaloneEntry()){const{app}=require('electron');app.setPath('userData',fs.mkdtempSync(path.join(require('node:os').tmpdir(),'posting-r6-')));const deadline=setTimeout(()=>{console.error('R6 posting timed out');app.exit(1);},120000);app.whenReady().then(async()=>{await run();clearTimeout(deadline);app.exit(0);}).catch(error=>{clearTimeout(deadline);console.error(error);app.exit(1);});}

module.exports.closeFixtureServer=closeFixtureServer;
module.exports.stablePostingLayout=stablePostingLayout;
module.exports.assertPostingLayout=assertPostingLayout;
module.exports.isStandaloneEntry=isStandaloneEntry;

module.exports.postingCasRejectionMessage=postingCasRejectionMessage;
