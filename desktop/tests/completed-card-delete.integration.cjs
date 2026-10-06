/* Production React list: historical completions cannot rehydrate as active cards. */
const {createFixtureLifecycle,closeFixtureServer} = require('./fixture-lifecycle.cjs');
const assert = require('node:assert/strict'), path = require('node:path'), http = require('node:http'), fs = require('node:fs');
module.exports = async ({win, host}) => {
  let built;
  const server=http.createServer((req,res)=>{
    const suffix=req.url==='/fixture.js'?'.js':req.url==='/fixture.css'?'.css':null;
    res.setHeader('content-type',suffix==='.js'?'text/javascript; charset=utf-8':suffix==='.css'?'text/css; charset=utf-8':'text/html; charset=utf-8');
    res.end(suffix?built.outputFiles.find(x=>x.path.endsWith(suffix)).contents:'<meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>');
  });
  const lifecycle=createFixtureLifecycle(win,{label:'Automatic completed cleanup',sourceFile:__filename});
  const evaluate=lifecycle.read,wait=lifecycle.wait;
  let primaryError;
  const cards=`document.querySelectorAll('[data-completed-target-id]')`;
  const refresh=`document.querySelector('button.formal-brand[aria-label=刷新]').click()`;
  const load=async scenario=>{
    const previous=await evaluate('completedCardFixture.snapshots');
    await evaluate(`completedCardFixture.scenario=${JSON.stringify(scenario)};completedCardFixture.revision++;${refresh};true`);
    await wait(`completedCardFixture.snapshots>${previous}`,'authoritative fixture snapshot refreshed');
  };
  const capture=async name=>{
    const screenshot=await lifecycle.capture(name,{captureTimeoutMs:10000});
    const directory=path.resolve(__dirname,'../../installer-output');fs.mkdirSync(directory,{recursive:true});
    fs.writeFileSync(path.join(directory,name),screenshot.toPNG());
  };
  try {
    built=await lifecycle.phase('bundle production fixture',()=>require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/completed-card-delete.tsx')],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'}),{timeoutMs:30000});
  await lifecycle.phase('listen on loopback',()=>new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)}));host.hide();
    await lifecycle.phase('load fixture route',()=>win.loadURL('http://127.0.0.1:'+server.address().port+'/#/collection'),{timeoutMs:30000});
    await lifecycle.visible('Automatic completion cleanup');
    await wait('completedCardFixture.snapshots>0','initial snapshot');
    await evaluate('completedCardFixture.dismissed=[];true');
    await load('initial');
    await wait(`${cards}.length===0&&document.body.textContent.includes('暂无采集任务')`,'151 legacy completed gaps never flood the task list');
    assert.equal(await evaluate('completedCardFixture.snapshot().tasks[0].targets.length'),151,'history records remain intact');
    assert.equal(await evaluate('completedCardFixture.commands.length'),0,'render never mutates historical data');
    await capture('r4-legacy-history-hidden.png');
    await load('automatic');
    await wait(`${cards}.length===0&&document.body.textContent.includes('暂无采集任务')`,'normal completion disappears before window release');
    assert.equal(await evaluate('completedCardFixture.snapshot().tasks[0].targets[0].current_window_id'), 'completed-window','presentation did not clear window ownership');
    assert.equal(await evaluate('completedCardFixture.commands.length'),0,'presentation does not release or cancel window');
    await capture('r4-normal-completion-hidden.png');
    await load('cleanup-error');
    await wait(`${cards}.length===1&&${cards}[0].textContent.includes('重试关闭')`,'failed cleanup remains visible and recoverable');
    await capture('r4-cleanup-failure-retained.png');
    await evaluate(`completedCardFixture.dismissed.push('completed-target');completedCardFixture.revision++;${refresh};true`);
    await wait(`${cards}.length===0`,'authoritative cleanup dismissal removes the current card');
    const before=await evaluate('JSON.stringify(completedCardFixture.snapshot().history)');
    await lifecycle.reload('completed-card history reload');
    await lifecycle.visible('Completion reload');
    await wait(`completedCardFixture.snapshots>0&&${cards}.length===0&&document.body.textContent.includes('暂无采集任务')`,'reload never resurrects dismissed or legacy completion cards');
    assert.equal(await evaluate('JSON.stringify(completedCardFixture.snapshot().history)'),before,'history is unchanged');
    assert.equal(await evaluate('completedCardFixture.snapshot().dedupe.total'),602831);
    assert.equal(await evaluate('completedCardFixture.commands.length'),0);
    console.log('PASS production completion list: legacy suppression, immediate completion hiding, failure recovery control, durable dismissal, retained history/dedupe');
  } catch(error) {primaryError=error;await lifecycle.diagnose(error)}
  finally {await lifecycle.finish(primaryError,[['HTTP server',()=>closeFixtureServer(server)]]);}
};
