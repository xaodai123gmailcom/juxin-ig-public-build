const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const path=require('node:path');

// Run the integration fixture's real catch/finally with injected resources and
// a controlled assertion failure. Native behavior remains covered by the
// Electron integration itself; these cases check failure-path ownership.
const source=readFileSync(path.join(__dirname,'floating-pages.integration.cjs'),'utf8');
const start=source.indexOf(' try {\n');
const end=source.indexOf(' } catch(error) {failed=true;throw error;} finally {',start);
assert.ok(start>=0&&end>start,'Locate the actual floating-page fixture cleanup');
const harness=source.slice(0,start)+` try {
 ({gpt,review,closing,closingTool,missingTool,gptSession,reviewSession,releaseSlow}=fixture.resources);
 gptProtocol=fixture.protocols;reviewProtocol=fixture.protocols;
 if(fixture.primary)throw fixture.primary;
`+source.slice(end);

async function run({primary,cleanupFailure=false,protocols=true}={}){
 const calls=[],warnings=[],cleanupError=new Error('dispose failed');
 const tool=name=>({dispose(){calls.push(name);if(name==='gpt'&&cleanupFailure)throw cleanupError;}});
 const fixture={primary,protocols,resources:{
  gpt:tool('gpt'),review:tool('review'),closingTool:tool('closingTool'),missingTool:tool('missingTool'),
  closing:{isDestroyed:()=>false,destroy(){calls.push('closing');}},releaseSlow(){calls.push('slow');},
  gptSession:{protocol:{unhandle(){calls.push('gptProtocol');}}},
  reviewSession:{protocol:{unhandle(){calls.push('reviewProtocol');}}},
 }};
 const context={module:{exports:{}},fixture,__dirname,setTimeout,clearTimeout,
  console:{error:error=>warnings.push(error)},
  require:name=>name==='electron'?{}:
   name==='./floating-fixture.cjs'?{prepareFixtureWindow:async()=>async()=>{calls.push('restore');}}:
   name==='./review-recovery-fixture.cjs'?{}:require(name),
 };
 runInNewContext(harness,context);
 let caught;
 try{await context.module.exports({win:{},host:{}});}catch(error){caught=error;}
 return {calls,warnings,caught,cleanupError};
}

test('cleanup failure preserves the original assertion and still releases all owned resources',async()=>{
 const primary=new Error('original assertion failure');
 const result=await run({primary,cleanupFailure:true});
 assert.equal(result.caught,primary);
 assert.equal(result.warnings.length,1);
 assert.equal(result.warnings[0].cause,result.cleanupError);
 assert.deepEqual(result.calls,['slow','missingTool','closingTool','closing','gpt','review','gptProtocol','reviewProtocol','restore']);
});

test('cleanup-only errors fail the fixture and do not skip parent restoration',async()=>{
 const result=await run({cleanupFailure:true});
 assert.equal(result.caught.name,'AggregateError');
 assert.equal(result.caught.errors.length,1);
 assert.equal(result.caught.errors[0].cause,result.cleanupError);
 assert.equal(result.calls.at(-1),'restore');
});

test('a failure before protocol registration cannot remove an unowned handler',async()=>{
 const primary=new Error('registration did not finish');
 const result=await run({primary,protocols:false});
 assert.equal(result.caught,primary);
 assert.equal(result.calls.some(name=>name.endsWith('Protocol')),false);
 assert.ok(result.calls.includes('gpt')&&result.calls.includes('review'));
 assert.equal(result.calls.at(-1),'restore');
});

test('successful cleanup releases response waiters and tools before restoring geometry',async()=>{
 const result=await run();
 assert.equal(result.caught,undefined);
 assert.deepEqual(result.warnings,[]);
 assert.equal(result.calls[0],'slow');
 assert.ok(result.calls.indexOf('review')<result.calls.indexOf('restore'));
 assert.ok(result.calls.includes('gptProtocol')&&result.calls.includes('reviewProtocol'));
 assert.equal(result.calls.at(-1),'restore');
});
