import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {collectionSourceRecheckModes,collectionRecheckCompleted,collectionCompletedGapVisible} from '../src/collection-task-rows.ts';
import {createCollectorCoreClient} from '../src/core-client.ts';
const completed = (extra={}) => ({id:'target-one',status:'completed',mode_coverage:{followers:{discovery_finished:true,unobserved_count:126}},...extra});

test('initial completed gaps offer explicit recheck, finished rechecks are complete without a repeated loop',()=>{
 const initial=completed();assert.equal(collectionCompletedGapVisible(initial),true);
 assert.equal(collectionRecheckCompleted(initial),false);assert.deepEqual(collectionSourceRecheckModes(initial),['followers']);
 const rechecked=completed({source_recheck:{mode:'followers',state:'completed',completed_at:'2026-10-01T22:00:00Z'}});
 assert.equal(collectionRecheckCompleted(rechecked),true);assert.equal(collectionCompletedGapVisible(rechecked),true);
 assert.deepEqual(collectionSourceRecheckModes(rechecked),[]);assert.equal(rechecked.mode_coverage.followers.unobserved_count,126);
 for(const status of ['running','pending','paused','recoverable','failed','stopped']){
  const unfinished={...rechecked,status};assert.equal(collectionCompletedGapVisible(unfinished),false,status);
  assert.equal(collectionRecheckCompleted(unfinished),false,status);
 }
});

test('dismissed cards stay hidden and completed recheck cards do not force alternating modes',()=>{
 const hidden=completed({collection_list_dismissed:true});
 assert.equal(collectionCompletedGapVisible(hidden),false);assert.deepEqual(collectionSourceRecheckModes(hidden),[]);
 const mixed=completed({source_recheck:{mode:'followers',state:'completed',completed_at:'now'},mode_coverage:{followers:{discovery_finished:true,unobserved_count:126},following:{discovery_finished:true,unobserved_count:7}}});
 assert.deepEqual(collectionSourceRecheckModes(mixed),[]);
 for(const coverage of [{}, {followers:{discovery_finished:false,unobserved_count:126}}, {followers:{discovery_finished:true,unobserved_count:0}}])
  assert.equal(collectionCompletedGapVisible(completed({mode_coverage:coverage})),false);
});

test('completed-card dismissal is exact-target platform-scoped and advances command fences',async()=>{
 const calls=[];const client=createCollectorCoreClient({secureGet:async()=>null,secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({}),request:async(path,options)=>{calls.push({path,options});return {command:'task_target_control',snapshot_seq:63,result:{status:'dismissed'}};}},'instagram');
 await client.controlCollectionTarget('task-one','target-two','dismiss_completed');
 assert.deepEqual(calls,[{path:'/api/workbench/commands',options:{method:'POST',body:{type:'task_target_control',payload:{task_id:'task-one',target_id:'target-two',action:'dismiss_completed',platform:'instagram'}}}}]);
 assert.equal(client.lastCommandSnapshotSeq,63);
});

test('production card offers clear scoped confirmation and shares recheck busy identity',()=>{
 const src=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 assert.match(src,/function CompletedCollectionTargetCard/);assert.match(src,/只从采集任务列表移除这一张已完成卡片/);
 assert.match(src,/检查点和全局去重全部保留/);assert.match(src,/if \(!confirming \|\| !eligible \|\| deleting.current \|\| disabled\(actionKey\)\) return/);
 assert.match(src,/client\.controlCollectionTarget\(task\.id, target\.id, "dismiss_completed"\)/);
});

test('actual React completed-card fixture compiles and is a required native Windows UI gate',async()=>{
 const {build}=await import('esbuild');
 const result=await build({entryPoints:[fileURLToPath(new URL('./fixtures/completed-card-delete.tsx',import.meta.url))],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 assert.ok(result.outputFiles.some(file=>file.path.endsWith('.js')&&file.contents.length>1000));
 const gate=readFileSync(new URL('../../desktop/tests/embedded-browser.integration.cjs',import.meta.url),'utf8');
 assert.match(gate,/watchdog\.begin\('completed-card-delete',90000\);await require\('\.\/completed-card-delete\.integration\.cjs'\)\(\{win,host\}\)/);
});
