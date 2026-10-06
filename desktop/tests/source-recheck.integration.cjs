const {createFixtureLifecycle,closeFixtureServer,stableGeometry}=require('./fixture-lifecycle.cjs');
const assert=require('node:assert/strict'),path=require('node:path'),http=require('node:http');

module.exports=async({win,host})=>{
 let built;
 const server=http.createServer((req,res)=>{
  if(req.url==='/war-wolf.svg'){res.setHeader('content-type','image/svg+xml');res.end(require('node:fs').readFileSync(path.resolve(__dirname,'../../renderer/public/war-wolf.svg')));return;}
  const suffix=req.url==='/fixture.js'?'.js':req.url==='/fixture.css'?'.css':null;
  res.setHeader('content-type',suffix==='.js'?'text/javascript; charset=utf-8':suffix==='.css'?'text/css; charset=utf-8':'text/html; charset=utf-8');
  res.end(suffix?built.outputFiles.find(x=>x.path.endsWith(suffix)).contents:'<meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>');
 });
 const lifecycle=createFixtureLifecycle(win,{label:'Source recheck',sourceFile:__filename});
 const evaluate=lifecycle.read,wait=lifecycle.wait;
 let primaryError;
 const buttons=`[...document.querySelectorAll('button')].filter(x=>x.textContent.includes('复查未发现'))`;
 const refresh=`document.querySelector('button.formal-brand[aria-label=刷新]').click()`;
 const load=async state=>{
  const previous=await evaluate('sourceRecheckFixture.snapshots');
  await evaluate(`sourceRecheckFixture.state=${JSON.stringify(state)};sourceRecheckFixture.revision++;${refresh};true`);
  await wait(`sourceRecheckFixture.snapshots>${previous}`,'new fixture snapshot fetched');
 };
 try{
  built=await lifecycle.phase('bundle production fixture',()=>require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/source-recheck.tsx')],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'}),{timeoutMs:30000});
  await lifecycle.phase('listen on loopback',()=>new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)}));host.hide();
  await lifecycle.phase('load fixture route',()=>win.loadURL('http://127.0.0.1:'+server.address().port+'/#/collection'),{timeoutMs:30000});
  await lifecycle.visible('Source recheck');
  await wait(`${buttons}.length===1`,'running source gap control rendered');
  assert.equal(await evaluate(`${buttons}[0].disabled`),false,'running source parent can recheck without pausing children');
  assert.equal(await evaluate("document.querySelector('.collection-source-recheck-gap').textContent"),'差额：23');
  assert.equal(await evaluate("document.body.textContent.includes('先暂停，再复查未发现部分')"),false);
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.collection-mode-progress .collection-progress-value')].map(x=>[x.querySelector('.collection-progress-label')?.textContent,x.querySelector('.collection-progress-number')?.textContent].join(' '))"),
    ['粉丝总数 418','已识别 395','未识别 23','去重 33','队列 30','丢弃 201','合格 130']);
  assert.match(await evaluate("document.querySelector('.collection-progress-value.is-pending-work').getAttribute('aria-label')"),/不计入未识别/);
  // Measure the real production layout at compact and wide task-card widths.
  for (const width of [280, 380, 640]) {
    const oldStyle=await evaluate(`(()=>{const container=document.querySelector('.collection-task-progress');const oldStyle=container.getAttribute('style');container.style.width='${width}px';container.style.maxWidth='none';return oldStyle})()`,'set counter width '+width);
    let geometry,geometryError;
    try {
      geometry=await stableGeometry(evaluate,`(()=>{
        const container=document.querySelector('.collection-task-progress'),mode=container.querySelector('.collection-mode-progress'),box=mode.getBoundingClientRect();
        const cells=[...mode.querySelectorAll('.collection-progress-value')];
        return {width:parseFloat(getComputedStyle(container).width),count:cells.length,display:getComputedStyle(mode).display,
          labels:cells.map(cell=>cell.querySelector('.collection-progress-label').getBoundingClientRect().bottom),
          values:cells.map(cell=>cell.querySelector('.collection-progress-number').getBoundingClientRect().top),
          within:cells.every(cell=>{const r=cell.getBoundingClientRect();return r.left>=box.left-1&&r.right<=box.right+1&&cell.scrollWidth<=cell.clientWidth+1})};
      })()`,width,{label:'Source counter geometry'});
    } catch(error) {geometryError=error;throw error}
    finally {
      try{await evaluate(`(()=>{const container=document.querySelector('.collection-task-progress');const oldStyle=${JSON.stringify(oldStyle)};oldStyle===null?container.removeAttribute('style'):container.setAttribute('style',oldStyle);return true})()`,'restore counter width')}
      catch(error){if(geometryError){geometryError.fixtureCleanupErrors=[{name:'restore counter width',message:String(error)}]}else throw error}
    }
    assert.equal(geometry.count,7);assert.equal(geometry.display,'grid');assert.equal(geometry.within,true,`counter overflow at ${width}px`);
    assert.ok(Math.max(...geometry.labels)-Math.min(...geometry.labels)<1,`unaligned labels at ${width}px`);
    assert.ok(Math.max(...geometry.values)-Math.min(...geometry.values)<1,`unaligned values at ${width}px`);
  }
  require('node:fs').mkdirSync(path.resolve('installer-output'),{recursive:true});
  const countersImage=await lifecycle.capture('R6 collection counters',{captureTimeoutMs:10000});
  require('node:fs').writeFileSync(path.resolve('installer-output/r6-collection-counters.png'),countersImage.toPNG());

  assert.equal(await evaluate('sourceRecheckFixture.commands.length'),0,'render never automatically rescans');
  await load('cleanup');
  await wait(`[...document.querySelectorAll('button')].some(x=>x.textContent==='重试清理'&&!x.disabled)`,'failed cleanup has an enabled safe retry');
  assert.equal(await evaluate(`${buttons}[0].disabled`),true,'recoverable durable status cannot bypass live cleanup ownership');
  assert.equal(await evaluate(`[...document.querySelectorAll('.collection-task-row button')].some(x=>x.textContent==='继续')`),false,'cleanup must finish before Continue');
  await load('running');await wait(`${buttons}.length===1&&!${buttons}[0].disabled`,'running source can be explicitly rechecked');
  await evaluate(`sourceRecheckFixture.hold=true;${buttons}[0].click();${buttons}[0].click();true`);
  await wait(`sourceRecheckFixture.commands.length===1&&typeof sourceRecheckFixture.release==='function'`,'one recheck in flight');
  assert.equal(await evaluate(`${buttons}[0].disabled`),true,'pending mutation disables repeated clicks');
  assert.deepEqual(await evaluate('sourceRecheckFixture.commands[0]'),{type:'task_source_recheck',payload:{task_id:'recheck-task',target_id:'recheck-target',mode:'followers',platform:'instagram'}});
  await evaluate('sourceRecheckFixture.release();sourceRecheckFixture.hold=false;true');
  await wait(`${buttons}.length===0`,'successful command removes stale finished-source action');
  await load('completed');await wait(`${buttons}.length===0`,'legacy completed history stays out of collection list');
  assert.equal(await evaluate("document.body.textContent.includes('可见名单已处理，仍有未发现差额')"),false);
  await load('paused');await wait(`${buttons}.length===1&&!${buttons}[0].disabled`,'paused recovery stays available');
  await evaluate(`sourceRecheckFixture.fail=true;${buttons}[0].click();true`);
  await wait(`sourceRecheckFixture.commands.length===2&&${buttons}.length===1&&!${buttons}[0].disabled`,'failed recovery recheck allows retry');
  await evaluate(`sourceRecheckFixture.snapshotFail=true;${refresh};true`);
  await wait(`!!document.querySelector('.formal-content > .formal-error-banner')`,'snapshot error visible');
  assert.equal(await evaluate(`${buttons}[0].disabled`),true,'stale snapshot never authorizes recheck');
  await evaluate('sourceRecheckFixture.snapshotFail=false;sourceRecheckFixture.fail=false;true');
  await load('unsupported');await wait(`${buttons}.length===0`,'Unsupported records never get the IG recheck command');
  assert.equal(await evaluate('sourceRecheckFixture.commands.length'),2);
  console.log('PASS actual React source-gap recheck: independent running parent, true gap, exact source/mode, double-click fence, completion, failure, stale snapshot and foreign-record rejection');
 }catch(error){primaryError=error;await lifecycle.diagnose(error)}
 finally{await lifecycle.finish(primaryError,[['HTTP server',()=>closeFixtureServer(server)]]);}
};

module.exports.closeFixtureServer=closeFixtureServer;
