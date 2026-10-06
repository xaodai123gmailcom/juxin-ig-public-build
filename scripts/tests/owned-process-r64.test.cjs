const test=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const {startOwnedWindowsProcess,stopOwnedProbe,terminalReceipt,cleanupReceipt}=require('../owned_process.cjs');

function fixture(overrides={}) {
 const child=new EventEmitter(); child.pid=1001; child.stdin=new EventEmitter(); let endCalls=0;
 child.stdin.end=()=>{endCalls++;overrides.onEnd?.(child)};
 const receipt={schemaVersion:1,supervisorPid:1002,launchTargetPid:2001,targetExitCode:0,outcome:'completed',confirmedTreeEmpty:true,errors:[],requestedExecutable:'C:\\python.exe',launchExecutable:'C:\\python.exe',budgetLabel:'existing test budget',executionLimitSeconds:.06,elapsedSeconds:.01,...overrides.receipt};
 if(!Object.hasOwn(overrides.receipt||{},'targetExitCodeUnsigned'))receipt.targetExitCodeUnsigned=receipt.targetExitCode===null?null:receipt.targetExitCode>>>0;
 const events=[];
 const probe=startOwnedWindowsProcess({python:'C:\\python.exe',executable:'C:\\python.exe',args:['C:\\测试\\fixture.py'],cwd:'C:\\测试',
  timeoutMs:60,drainMs:10,terminationMs:10,ioMs:15,stopGraceMs:10,outerGraceMs:10,pollMs:5,label:'existing test budget',logDirectory:'C:\\logs',...overrides.options}, {
  makeDirectory:()=>'/fake-owned',
  spawn:(file,args,options)=>{if(receipt.requestId===undefined)receipt.requestId=JSON.parse(Buffer.from(args.at(-1),'base64').toString()).requestId;events.push({file,args,options});setImmediate(()=>overrides.start ? overrides.start(child) : child.emit('exit',0,null));return child},
  readSegment:overrides.readSegment|| (async()=>({data:Buffer.from('genuine output\n'),next:15,size:15})),
  readReceipt:overrides.readReceipt|| (async()=>receipt),
 });
 return {probe,child,receipt,events,get endCalls(){return endCalls}};
}

test('success is tied to root-exit plus owned-tree receipt, not output close',async()=>{
 const f=fixture(); const result=await f.probe.done;
 assert.equal(result.code,0);assert.equal(result.receipt.launchTargetPid,2001);
 assert.equal(f.probe.supervisorLaunchPid,1001);assert.equal(result.receipt.supervisorPid,1002);
 const launch=f.events[0];assert.deepEqual(launch.options.stdio,['pipe','ignore','inherit']);assert.equal(launch.options.shell,undefined);
 const request=JSON.parse(Buffer.from(launch.args.at(-1),'base64').toString('utf8'));
 assert.deepEqual(request.arguments,['C:\\测试\\fixture.py']);assert.equal(request.timeoutSeconds,.06);
 assert.equal(request.controlInput,true);assert.equal(request.stdoutPath,request.stderrPath);
 assert.ok(f.endCalls>0);
});

test('a nonzero target code is preserved independently of the supervisor code',async()=>{
 const f=fixture({receipt:{targetExitCode:23,outcome:'target-exited-nonzero'}});
 const result=await f.probe.done;assert.equal(result.code,23);assert.equal(terminalReceipt(result.receipt),true);
});

test('missing Python fails cleanly before any unowned target fallback',async()=>{
 const f=fixture({start:child=>child.emit('error',Object.assign(new Error('spawn python ENOENT'),{code:'ENOENT'})),readReceipt:async()=>{throw new Error('no receipt')}});
 await assert.rejects(f.probe.done,/ENOENT/);assert.equal(f.events.length,1);assert.ok(f.endCalls>0);
 assert.equal((await f.probe.stop()).confirmedTreeEmpty,false);
});

test('supervisor exit without receipt never claims target termination',async()=>{
 const f=fixture({readReceipt:async()=>{throw new Error('missing receipt')}});
 await assert.rejects(f.probe.done,error=>{assert.equal(error.ownedCleanup.confirmedTreeEmpty,false);return /missing receipt/.test(error.message)});
});

test('error or nonzero termination outcomes remain unconfirmed and preserve failure',async()=>{
 for(const reason of ['termination API failed','termination budget expired']) {
  const f=fixture({receipt:{outcome:'execution-timeout',confirmedTreeEmpty:false,errors:[reason]},start:child=>child.emit('exit',125,null)});
  await assert.rejects(f.probe.done,error=>{assert.equal(error.ownedCleanup.confirmedTreeEmpty,false);assert.deepEqual(error.ownedReceipt.errors,[reason]);return true});
  assert.ok(f.endCalls>0);
 }
});

test('an exited root with surviving descendants fails despite code zero',async()=>{
 const f=fixture({receipt:{outcome:'descendant-drain-timeout',confirmedTreeEmpty:true},start:child=>child.emit('exit',125,null)});
 await assert.rejects(f.probe.done,/descendant-drain-timeout/);
 assert.equal((await f.probe.stop()).confirmedTreeEmpty,true);
});

test('stalled file IO triggers bounded failure and cancellation while logs remain owned',async()=>{
 const f=fixture({readSegment:()=>new Promise(()=>{}),start:()=>{},onEnd:child=>setImmediate(()=>child.emit('exit',0,null))});
 await assert.rejects(f.probe.done,/Owned final output read|Owned log read/);
 assert.ok(f.endCalls>0);assert.equal((await f.probe.stop()).confirmedTreeEmpty,true);
});

test('output close without process exit cannot pass and cancellation stays bounded',async()=>{
 const f=fixture({start:child=>child.emit('close',0,null)});
 await assert.rejects(f.probe.done,error=>{assert.equal(error.ownedCleanup.confirmedTreeEmpty,false);return /Owned supervisor exceeded/.test(error.message)});
 assert.ok(f.endCalls>0);
});

test('late termination without a receipt is reported unconfirmed',async()=>{
 const f=fixture({start:()=>{},onEnd:child=>setImmediate(()=>child.emit('exit',125,null)),readReceipt:async()=>{throw new Error('no terminal receipt')}});
 const stopped=await f.probe.stop();assert.equal(stopped.confirmedTreeEmpty,false);
 await assert.rejects(f.probe.done,/no terminal receipt/);
});

test('invalid receipt and target errors cannot be transformed into success by EOF',()=>{
 for(const receipt of [null,{}, {schemaVersion:1,confirmedTreeEmpty:true,errors:[],outcome:'completed',targetExitCode:null},
   {schemaVersion:1,confirmedTreeEmpty:true,errors:['close failed'],outcome:'completed',targetExitCode:0}]) assert.equal(Boolean(terminalReceipt(receipt)),false);
});

test('actual embedded failure handler retains cleanup failure and exits nonzero once',async()=>{
 const source=fs.readFileSync(path.resolve(__dirname,'../../desktop/tests/embedded-browser.integration.cjs'),'utf8');
 const implementation=source.slice(source.indexOf('async function exitFailed('),source.indexOf('\nconst watchdog='));
 for(const cleanup of [{confirmedTreeEmpty:false,error:'injected termination error'}, {confirmedTreeEmpty:true,receipt:{launchTargetPid:1234}}]) {
  const exits=[],reports=[];let stops=0;
  const context={exiting:false,activeProbe:{stop:async()=>{stops++;return cleanup}},stopOwnedProbe,
   watchdog:{dispose(){}},integrationFailureDetails:()=>({error:{message:'original assertion'}}),writeIntegrationFailure:(_,details)=>reports.push(structuredClone(details)),
   integrationFailureSummary:()=> 'original assertion',process:{env:{}},console:{error(){}},app:{exit:code=>exits.push(code)}};
  vm.createContext(context);vm.runInContext(implementation+'\nthis.fail=exitFailed;',context);
  await context.fail(1,{stage:'fixture'},new Error('original assertion'));await context.fail(1,{});
  assert.equal(stops,1);assert.deepEqual(exits,[1]);assert.equal(reports.at(-1).ownedProbeCleanup.confirmedTreeEmpty,cleanup.confirmedTreeEmpty);
  assert.equal(reports.at(-1).error.message,'original assertion');
 }
});

test('malformed cleanup claims are rejected while valid cancellation can prove emptiness',async()=>{
 for(const changes of [{schemaVersion:2},{supervisorPid:'1002'},{launchTargetPid:null},{errors:null},{errors:'none'},{outcome:'invented'},{confirmedTreeEmpty:'true'},{elapsedSeconds:-1},{launchExecutable:null}]) {
  const f=fixture({receipt:changes});await assert.rejects(f.probe.done);assert.equal((await f.probe.stop()).confirmedTreeEmpty,false);assert.equal(f.probe.cleanupConfirmed,false);
 }
 const f=fixture({receipt:{outcome:'cancelled',targetExitCode:1},start:child=>child.emit('exit',125,null)});
 await assert.rejects(f.probe.done);assert.equal(cleanupReceipt(f.receipt),true);assert.equal(terminalReceipt(f.receipt),false);assert.equal((await f.probe.stop()).confirmedTreeEmpty,true);
});

test('a receipt must bind this exact request and keep target and supervisor identities distinct',async()=>{
 for(const changes of [{requestedExecutable:'C:\\unrelated.exe'},{budgetLabel:'other request'},{executionLimitSeconds:999999},{requestId:'f'.repeat(32)},{supervisorPid:2001}]) {
  const f=fixture({receipt:changes});await assert.rejects(f.probe.done);assert.equal((await f.probe.stop()).confirmedTreeEmpty,false);assert.equal(f.probe.cleanupConfirmed,false);
 }
});

test('high-bit native exit keeps both raw DWORD and safe signed exit',async()=>{
 const f=fixture({receipt:{targetExitCode:-1073741819,targetExitCodeUnsigned:3221225477,outcome:'target-exited-nonzero'}});
 const result=await f.probe.done;assert.equal(result.code,-1073741819);assert.equal(result.unsignedCode,0xC0000005);assert.equal(result.receipt.confirmedTreeEmpty,true);
});

test('a stalled progress read does not accumulate more file readers before cancellation exits',async()=>{
 let reads=0;
 const f=fixture({readSegment:()=>{reads++;return new Promise(()=>{})},start:()=>{},onEnd:child=>setTimeout(()=>child.emit('exit',0,null),40),options:{stopGraceMs:70}});
 await assert.rejects(f.probe.done,/Owned log read/);assert.equal(reads,1);assert.ok(f.endCalls>0);
});

test('actual Windows runPythonProbe passes zero and rejects nonzero independently of the POSIX source marker',async()=>{
 const source=fs.readFileSync(path.resolve(__dirname,'../../desktop/tests/embedded-browser.integration.cjs'),'utf8');
 const implementation=source.slice(source.indexOf('const runPythonProbe=async('),source.indexOf('\napp.whenReady()'));
 async function outcome(text,code){
  const calls=[];
  const probe={supervisorLaunchPid:101,cleanupConfirmed:true,receipt:{launchTargetPid:202},
   done:Promise.resolve({code,output:'native failure detail',receipt:{launchTargetPid:202}})};
  const context={__dirname:path.resolve(__dirname,'../../desktop/tests'),path,temp:'/owned-fixture',activeProbe:null,
   watchdog:{status:()=>({stage:'python-worker-adapter',stageLimitMs:1000,stageElapsedMs:100,overallLimitMs:2000,totalElapsedMs:200})},
   process:{platform:'win32',stdout:{writableLength:0,write(){}}},console:{log(){}},
   startOwnedWindowsProcess:options=>{calls.push(options);return probe},
   pythonProbeFailure:(script,exitCode,output)=>Object.assign(new Error('actual Python nonzero '+exitCode),{exitCode,script,output})};
  vm.createContext(context);vm.runInContext(text+'\nthis.invoke=runPythonProbe;',context);
  let error=null;try{await context.invoke('C:\\Python\\python.exe','fixture.py',{})}catch(caught){error=caught}
  assert.equal(calls.length,1);assert.equal(calls[0].timeoutMs,900);assert.equal(context.activeProbe,null);
  return error;
 }
 async function assertWindowsContract(text){
  assert.equal(await outcome(text,0),null,'new Windows baseline must actually pass');
  const error=await outcome(text,23);
  assert.equal(error?.exitCode,23,'nonzero Windows Python probe must reject');
  assert.equal(error.output,'native failure detail');
 }
 await assertWindowsContract(implementation);
 const guard='if(result.code!==0)throw pythonProbeFailure(script,result.code,result.output);';
 assert.ok(implementation.includes(guard));
 await assert.rejects(assertWindowsContract(implementation.replace(guard,'')),/nonzero Windows Python probe must reject/);
});
