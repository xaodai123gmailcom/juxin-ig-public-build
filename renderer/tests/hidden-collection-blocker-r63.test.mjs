import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {collectionTaskRows} from '../src/collection-task-rows.ts';
const source=readFileSync(new URL('../src/nurture-collection-blocker.tsx',import.meta.url),'utf8');
const blocker={task_id:'exact-paused',name:'旧任务',status:'paused',version:7,window_ids:['w1'],dismissed:true,can_stop:true,blocked_reason:'',message:'按任务编号读取'};
function harness({request=async()=>({message:'已停止，请再次核验'}),confirm=true,canStop=true,refreshError=null}={}) {
 const pending={current:false},epoch={current:0},alive={current:true},state={lookup:null,message:'',busy:false,calls:[],confirmations:[],refreshes:0};
 const context={pending,epoch,alive,disabled:false,lookup:{blocker:{...blocker,can_stop:canStop}},jobId:'held',windowNames:new Map([['w1','窗口1']]),
  getCollectorCoreClient:()=>({studioCommand:async body=>{state.calls.push(body);return request(body);}}),
  setBusy:value=>state.busy=value,setMessage:value=>state.message=value,setLookup:value=>state.lookup=value,
  window:{confirm:text=>{state.confirmations.push(text);return confirm;}},onChanged:async()=>{state.refreshes++;if(refreshError)throw refreshError;}};
 const code=stripTypeScriptTypes(source.slice(source.indexOf('  async function locate()'),source.indexOf('  return <div')));
 const handlers=new Function(...Object.keys(context),code+';return {locate,stop};')(...Object.values(context));
 return {...handlers,pending,epoch,alive,state};
}
test('reproduces invisible paused task with only archived completion and no runtime',()=>{
 const task={id:'exact-paused',status:'paused',windows:[{profile_id:'w1'}],targets:[{id:'old-target',status:'completed',current_window_id:'w1',current_stage:'completed_archived'}]};
 assert.deepEqual(collectionTaskRows(task),[]);assert.equal(task.status,'paused');
});
test('lookup sends only exact held job and collapses repeated clicks',async()=>{
 let finish;const h=harness({request:()=>new Promise(resolve=>finish=resolve)});const running=h.locate();await h.locate();assert.equal(h.state.calls.length,1);
 finish({job_id:'held',profile_id:'w1',blocker});await running;
 assert.deepEqual(h.state.calls,[{action:'locate_cleanup_collection',job_id:'held'}]);assert.equal(h.state.lookup.blocker.task_id,'exact-paused');assert.equal(h.pending.current,false);
});
test('cancel and blocked tasks never send stop or refresh',async()=>{
 for(const options of [{confirm:false},{canStop:false}]){const h=harness(options);await h.stop();assert.deepEqual(h.state.calls,[]);assert.equal(h.state.refreshes,0);}
});
test('confirmed stop includes task ID/version and retains separate cleanup verification',async()=>{
 const h=harness();await h.stop();assert.deepEqual(h.state.calls,[{action:'stop_cleanup_collection',job_id:'held',task_id:'exact-paused',version:7}]);
 assert.match(h.state.confirmations[0],/exact-paused/);assert.match(h.state.confirmations[0],/窗口1/);assert.match(h.state.message,/再次核验/);assert.equal(h.state.refreshes,1);assert.equal(h.state.lookup,null);
});
test('repeat stop is collapsed; stale refusal discards the old target and does not report release',async()=>{
 let fail;const h=harness({request:()=>new Promise((_resolve,reject)=>fail=reject)});const running=h.stop();await h.stop();assert.equal(h.state.calls.length,1);
 fail(Error('关联任务已变化'));await running;assert.equal(h.state.lookup,null);assert.equal(h.state.refreshes,0);assert.match(h.state.message,/已变化/);assert.equal(h.state.busy,false);
});
test('delayed lookup cannot repopulate unmounted or newer job state',async()=>{
 for(const change of [h=>h.alive.current=false,h=>h.epoch.current++]){
  let finish;const h=harness({request:()=>new Promise(resolve=>finish=resolve)});const running=h.locate();change(h);finish({blocker});await running;assert.equal(h.state.lookup,null);
 }
});
test('held cards expose a narrow locator and preserve existing cleanup operation',()=>{
 const workspace=readFileSync(new URL('../src/standalone-nurture-workspace.tsx',import.meta.url),'utf8');
 assert.match(workspace,/nurtureNeedsCleanup\(job\) && <NurtureCollectionBlocker/);assert.match(workspace,/'retry_cleanup'/);
 assert.doesNotMatch(source,/delete|release|unlock|start_cleanup|import_history/);
});

test('refresh failure preserves committed stop feedback and never invites repeated mutation',async()=>{
 const h=harness({refreshError:Error('offline')});await h.stop();assert.equal(h.state.calls.length,1);assert.equal(h.state.lookup,null);
 assert.match(h.state.message,/已停止.*刷新失败.*offline.*再次核验/);assert.equal(h.pending.current,false);
});
