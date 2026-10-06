const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {integrationFailureDetails,integrationFailureSummary,writeIntegrationFailure,pythonProbeFailure}=require('./integration-failure.cjs');

test('a failed assertion retains its substep and actual cause in the final summary and JSON',()=>{
 let error;try{assert.equal(4,3)}catch(e){error=e}
 error.integrationCheckpoint='task-watch/labels-2-workers';
 const details=integrationFailureDetails({stage:'task-watch',stageElapsedMs:556,completed:[{stage:'startup'}]},error);
 const summary=integrationFailureSummary(details);
 assert.match(summary,/task-watch\/labels-2-workers/);assert.match(summary,/4 !== 3/);assert.doesNotMatch(summary,/[\r\n]/);
 assert.equal(details.error.name,'AssertionError');assert.match(details.error.stack,/AssertionError/);
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'juxin-failure-'));
 try{
  const file=path.join(dir,'failure.json');writeIntegrationFailure(file,details);
  assert.deepEqual(JSON.parse(fs.readFileSync(file,'utf8')),details);
 }finally{fs.rmSync(dir,{recursive:true,force:true})}
});

test('watchdog deadline reports preserve the reason without inventing an assertion',()=>{
 const details=integrationFailureDetails({stage:'task-watch',reason:'stage deadline',stageElapsedMs:90000});
 assert.equal(details.error,undefined);assert.match(integrationFailureSummary(details),/task-watch.*stage deadline/);
});

test('non-Error failures remain actionable and excessively large data is bounded',()=>{
 assert.match(integrationFailureSummary(integrationFailureDetails({stage:'task-watch'},'closed renderer')),/closed renderer/);
 const error=new Error('x'.repeat(30000));error.stack='s'.repeat(30000);
 const details=integrationFailureDetails({stage:'task-watch'},error);
 assert.equal(details.error.message.length,1200);assert.equal(details.error.stack.length,6000);
});

test('Python probe summary retains the final exception after long successful test output and traceback',()=>{
 const output='test_other ... ok\r\n'.repeat(2000)+'ERROR: test_menu_reels_first_location_success_done_then_home (test_posting_dom.ScreenshotPostingTests)\r\n'
  +'Traceback (most recent call last):\r\n'+'  File "D:\\实验室\\新建文件夹 (15)\\backend\\app\\instagram_publisher.py"\r\n'.repeat(100)
  +'app.errors.ValidationError: 点击帖子后未进入上传素材窗口，尚未发布\r\nRan 4 tests in 46.707s\r\nFAILED (errors=1)\r\n';
 const details=integrationFailureDetails({stage:'python-posting-adapter'},pythonProbeFailure('embedded_posting_probe.py',1,output));
 assert.equal(details.error.name,'PythonProbeError');
 assert.match(integrationFailureSummary(details),/python-posting-adapter.*ValidationError: 点击帖子后未进入上传素材窗口/);
 assert.match(details.error.message,/test_menu_reels_first_location_success_done_then_home/);
 assert.equal(details.probe.exitCode,1);assert.equal(details.probe.script,'embedded_posting_probe.py');
 assert.ok(details.probe.outputTail.length<=8000);assert.match(details.probe.outputTail,/FAILED \(errors=1\)/);
});

test('Python probe failures without a traceback or exit code remain failures with bounded evidence',()=>{
 const killed=pythonProbeFailure('embedded_worker_probe.py',null,'CHECK probe started\n');
 assert.match(killed.message,/exited null.*CHECK probe started/);
 const empty=pythonProbeFailure('embedded_posting_probe.py',1,'');
 assert.match(empty.message,/probe produced no output/);
 const syntax=pythonProbeFailure('embedded_posting_probe.py',1,'File "fixture.py", line 1\nSyntaxError: invalid syntax\n');
 assert.match(syntax.message,/SyntaxError: invalid syntax/);
 const assertion=pythonProbeFailure('probe.py',1,'FAIL: test_once\nAssertionError: 2 != 1\nFAILED (failures=1)\n');
 assert.match(assertion.message,/AssertionError: 2 != 1/);
});
