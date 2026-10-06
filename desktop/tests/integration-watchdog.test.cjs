const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createIntegrationWatchdog}=require('./integration-watchdog.cjs');

function fixture(options={}) {
 let clock=0,sequence=0;const queue=new Map(),failures=[],logs=[];
 const timers={
  setTimeout(fn,ms){const id=++sequence;queue.set(id,{fn,at:clock+ms});return id},
  clearTimeout(id){queue.delete(id)},
  setInterval(fn,ms){const id=++sequence;queue.set(id,{fn,at:clock+ms,interval:ms});return id},
  clearInterval(id){queue.delete(id)},
 };
 const watchdog=createIntegrationWatchdog({onTimeout:r=>failures.push(r),log:s=>logs.push(s),now:()=>clock,timers,...options});
 return {watchdog,failures,logs,queue,
  advance(ms){const end=clock+ms;for(;;){const next=[...queue].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;const[id,t]=next;clock=t.at;if(t.interval)t.at+=t.interval;else queue.delete(id);t.fn()}clock=end},
  block(ms){clock+=ms},
 };
}
test('healthy independent stages can exceed the former 180-second suite limit',()=>{
 const f=fixture();f.watchdog.begin('browser',180000);f.advance(90000);
 f.watchdog.begin('python-posting',180000);f.advance(59000);
 f.watchdog.begin('account-ui',180000);f.advance(70000);
 const proof=f.watchdog.finish();assert.equal(proof.totalElapsedMs,219000);
 assert.equal(proof.completed.length,3);assert.deepEqual(f.failures,[]);assert.equal(f.queue.size,0);
});
test('heartbeat does not keep a stalled stage alive, and failure names that stage',()=>{
 const f=fixture();f.watchdog.begin('python-posting',180000);f.advance(180000);
 assert.equal(f.failures.length,1);assert.equal(f.failures[0].stage,'python-posting');
 assert.equal(f.failures[0].reason,'stage deadline');assert.ok(f.logs.some(s=>s.startsWith('WAIT')));
 assert.equal(f.queue.size,0);assert.throws(()=>f.watchdog.finish(),/stopped/);
});
test('a new stage gets its own budget and the old timer cannot terminate it',()=>{
 const f=fixture();f.watchdog.begin('one',60000);f.advance(59000);
 f.watchdog.begin('two',90000);f.advance(85000);assert.deepEqual(f.failures,[]);
 assert.equal(f.watchdog.finish().completed.at(-1).stage,'two');
});
test('overall deadline remains bounded despite repeated successful stages',()=>{
 const f=fixture({overallMs:200000});for(let i=0;i<3;i++){f.watchdog.begin('part-'+i,100000);f.advance(60000)}
 f.watchdog.begin('last',100000);f.advance(20000);assert.equal(f.failures.length,1);
 assert.equal(f.failures[0].reason,'overall deadline');assert.equal(f.failures[0].completed.length,3);
});
test('cleanup after another assertion fails never emits a stage success or timeout',()=>{
 const f=fixture();f.watchdog.begin('assertion',10000);f.watchdog.dispose();f.advance(1000000);
 assert.deepEqual(f.failures,[]);assert.equal(f.queue.size,0);assert.ok(!f.logs.some(s=>s.startsWith('PASS')));
});
test('blocked event loop cannot accept an overdue stage or final success',()=>{
 for(const action of ['begin','finish']){const f=fixture();f.watchdog.begin('blocked',1000);f.block(2000);
 assert.throws(()=>action==='begin'?f.watchdog.begin('next'):f.watchdog.finish(),/deadline/);
 assert.equal(f.failures[0].stage,'blocked');assert.equal(f.failures[0].completed.length,0)}
});
test('timeout details retain completed phase timings for diagnosis',()=>{
 const f=fixture();f.watchdog.begin('worker',180000);f.advance(62000);
 f.watchdog.begin('posting',180000);f.advance(59000);f.watchdog.begin('review-ui',90000);f.advance(90000);
 assert.deepEqual(f.failures[0].completed,[{stage:'worker',elapsedMs:62000},{stage:'posting',elapsedMs:59000}]);
 assert.equal(f.failures[0].totalElapsedMs,211000);
});
