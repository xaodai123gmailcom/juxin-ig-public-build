import {shareUnchangedJson} from '../src/snapshot-sharing.ts';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';
import { createCollectorCoreClient, CollectorCoreProtocolError } from '../src/core-client.ts';
import { applyLiveStatusOverlay } from '../src/workbench-live-status.ts';
import { performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback } from '../src/workbench-command-state.ts';

const time = second => `2026-09-16T01:00:${String(second).padStart(2, '0')}Z`;
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
const base = () => ({ revision: 4, generated_at: time(4), tasks: [{ id: 't', status: 'running', updated_at: time(4), targets: [] }], windows: [], sources: [] });
const live = revision => ({ revision, generated_at: time(revision), tasks: [{ task: { id: 't', status: 'running', updated_at: time(revision), targets: [] }, runtime: {} }] });
const bridge = request => ({ request, secureSet: async () => true, secureGet: async () => null, secureDelete: async () => true, configureIntegrations: async () => ({ restarted: true }) });

test('an in-flight heartbeat cannot cross a completed task-control revision', async () => {
  const pending = deferred();
  const client = createCollectorCoreClient(bridge(path => path.endsWith('/live-status') ? pending.promise
    : Promise.resolve({ command: 'task_control', result: { task_id: 't', status: 'paused' }, snapshot_seq: 6 })));
  const request = client.liveStatus();
  await client.controlCollectionTask('t', 'pause');
  pending.resolve(live(5));
  await assert.rejects(request, error => error instanceof CollectorCoreProtocolError);
});

test('a heartbeat observed after the command is still accepted without extra reads', async () => {
  let calls = 0;
  const reply = live(6);
  const client = createCollectorCoreClient(bridge(path => {
    if (path.endsWith('/live-status')) { calls++; return Promise.resolve(reply); }
    return Promise.resolve({ command: 'task_control', result: { task_id: 't', status: 'paused' }, snapshot_seq: 6 });
  }));
  await client.controlCollectionTask('t', 'pause');
  assert.equal(await client.liveStatus(), reply);
  assert.equal(calls, 1);
});

test('the overlay also checks the command revision at actual delivery time', () => {
  const snapshot = base();
  assert.equal(applyLiveStatusOverlay(snapshot, live(5), 6), snapshot);
  assert.notEqual(applyLiveStatusOverlay(snapshot, live(6), 6), snapshot);
});

test('the real hook rechecks a command completed while a React updater was queued', async () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  const hook = source.slice(source.indexOf('function useFormalCore()'), source.indexOf('\ntype CollectionControls'));
  const effects = [], updates = [];
  let stateIndex = 0;
  const client = { lastCommandSnapshotSeq: 0, liveStatus: async () => live(5) };
  runInNewContext(stripTypeScriptTypes(hook) + '\nuseFormalCore();', {
    shareUnchangedJson,
    useState: initial => [initial, stateIndex++ === 0 ? value => updates.push(value) : () => {}],
    useRef: initial => ({ current: initial }), useEffect: effect => effects.push(effect), useCallback: fn => fn,
    getCollectorCoreClient: () => client, usePlatformCore: () => client, useWorkbenchPlatform: () => ({platform: "instagram"}), collectionPlatform: () => "instagram",
    startWorkbenchSnapshotPolling: () => ({ stop() {}, refresh: async () => base() }),
    applyLiveStatusOverlay, SNAPSHOT_PAGE_LIMIT: 2000,
    document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: { setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {} },
    performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback,
  });
  const dispose = effects[0]();
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(updates.length, 1);
    client.lastCommandSnapshotSeq = 6;
    const snapshot = base();
    assert.equal(updates[0](snapshot), snapshot, 'queued old running state must not be delivered after pause/stop');
  } finally { dispose(); }
});

test('r94 actual full-snapshot delivery reuses equal window data and rejects a queued pre-command response',()=>{
 const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 const hook=source.slice(source.indexOf('function useFormalCore()'),source.indexOf('\ntype CollectionControls'));
 const effects=[],updates=[];let stateIndex=0,polling;
 const client={lastCommandSnapshotSeq:0,liveStatus:()=>new Promise(()=>{})};
 runInNewContext(stripTypeScriptTypes(hook)+'\nuseFormalCore();',{
  shareUnchangedJson,useState:initial=>[initial,stateIndex++===0?value=>updates.push(value):()=>{}],
  useRef:initial=>({current:initial}),useEffect:effect=>effects.push(effect),useCallback:fn=>fn,
  getCollectorCoreClient:()=>client,usePlatformCore:()=>client,useWorkbenchPlatform:()=>({platform:"instagram"}),collectionPlatform:()=>"instagram",startWorkbenchSnapshotPolling:options=>{polling=options;return {stop(){},refresh:async()=>base()}},
  applyLiveStatusOverlay,SNAPSHOT_PAGE_LIMIT:2000,
  document:{visibilityState:'visible',addEventListener(){},removeEventListener(){}},
  window:{setInterval:()=>1,clearInterval(){},setTimeout:()=>2,clearTimeout(){}},
  performCollectionSafetyControls,runWithSnapshotRefresh,workbenchCommandFeedback,
 });
 const dispose=effects[0]();
 try{
  assert.equal(polling.compact,true,'the real workbench opts into alias-free transport');
  assert.equal(polling.intervalMs,5000,'the performance fix does not slow the refresh cadence');
  assert.equal(polling.limit,2000);
  assert.equal(polling.historyLimit,2000);
  const previous=base(),incoming=JSON.parse(JSON.stringify(previous));incoming.revision=5;
  polling.onSnapshot(incoming);
  const apply=updates.at(-1),next=apply(previous);
  assert.equal(next.revision,5);assert.equal(next.windows,previous.windows);
  client.lastCommandSnapshotSeq=6;
  assert.equal(apply(previous),previous,'a newly completed control fences the queued full snapshot too');
 }finally{dispose()}
});

test('ten-minute hook soak retains one subscription across 1200 route renders and clears all timers on hide/dispose',async()=>{
 const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 const hook=source.slice(source.indexOf('function useFormalCore()'),source.indexOf('\ntype CollectionControls'));
 const coreSource=stripTypeScriptTypes(readFileSync(new URL('../src/core-client.ts',import.meta.url),'utf8')).replace(/\bexport /g,'');
 let now=0,serial=0,fullReads=0,liveReads=0;
 const timers=new Map(),listeners=new Set();
 const addTimer=(fn,delay,repeat=false)=>{const id=++serial;timers.set(id,{fn,delay,due:now+delay,repeat});return id};
 const clock={setTimeout:(fn,delay)=>addTimer(fn,delay),clearTimeout:id=>timers.delete(id),setInterval:(fn,delay)=>addTimer(fn,delay,true),clearInterval:id=>timers.delete(id)};
 const document={visibilityState:'visible',addEventListener(name,fn){assert.equal(name,'visibilitychange');listeners.add(fn)},removeEventListener(name,fn){listeners.delete(fn)}};
 const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
 const fixture={platform:'instagram',revision:1,generated_at:time(4),counts:{pending_public:2000},dedupe:{total:240000},pending:{public:Array.from({length:2000},(_,i)=>({id:'p'+i})),private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},tasks:[],sources:[],windows:Array.from({length:50},(_,i)=>({id:'w'+i,locked:true,lock_entity_id:'task'})),campaigns:[],truncated:true,has_more:{pending_public_accounts:true}};
 const calls=[];
 const testBridge=bridge(async(path)=>{
  calls.push(path);
  if(path.startsWith('/api/workbench/snapshot?')){fullReads++;return JSON.parse(JSON.stringify(fixture))}
  if(path==='/api/workbench/live-status'){liveReads++;return {revision:1,generated_at:time(4),tasks:[]}}
  throw Error('unexpected mutation or route: '+path);
 });
 const api=runInNewContext(coreSource+'\n({getCollectorCoreClient,startWorkbenchSnapshotPolling})',{...clock});
 const states=[],refs=[],memos=[],effects=[];let si=0,ri=0,mi=0,ei=0;
 const equal=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const render=runInNewContext(stripTypeScriptTypes(hook)+'\n() => useFormalCore()',{document,window:clock,
  useState(initial){const slot=si++;if(!(slot in states))states[slot]=typeof initial==='function'?initial():initial;return[states[slot],value=>{states[slot]=typeof value==='function'?value(states[slot]):value}]},
  useRef(initial){return refs[ri++]||=( {current:initial})},
  useCallback(fn,deps){const slot=mi++;if(!equal(memos[slot]?.deps,deps))memos[slot]={value:fn,deps};return memos[slot].value},
  useEffect(fn,deps){const slot=ei++;if(!equal(effects[slot]?.deps,deps)){effects[slot]?.cleanup?.();effects[slot]={fn,deps,pending:true}}},
  getCollectorCoreClient:(_unused,platform)=>api.getCollectorCoreClient(testBridge,platform),startWorkbenchSnapshotPolling:options=>api.startWorkbenchSnapshotPolling(options,testBridge),
  useWorkbenchPlatform:()=>({platform:'instagram'}),collectionPlatform:()=> 'instagram',shareUnchangedJson,applyLiveStatusOverlay,SNAPSHOT_PAGE_LIMIT:2000,
  performCollectionSafetyControls,runWithSnapshotRefresh,workbenchCommandFeedback,
 });
 const rerender=()=>{si=ri=mi=ei=0;const result=render();for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.fn()}return result};
 const advance=async(delta)=>{const end=now+delta;while(true){const next=[...timers].filter(([,timer])=>timer.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];if(!next)break;const [id,timer]=next;now=timer.due;if(timer.repeat)timer.due+=timer.delay;else timers.delete(id);timer.fn();await flush()}now=end};
 rerender();await flush();
 const retained=states[0],initialSlots=[states.length,refs.length,memos.length,effects.length];
 for(let iteration=0;iteration<120;iteration++){
  for(let route=0;route<10;route++)rerender();
  await advance(5000);
  assert.equal(timers.size,2);assert.equal(listeners.size,1);
 }
 assert.equal(fullReads,121);assert.equal(liveReads,241);
 assert.deepEqual([states.length,refs.length,memos.length,effects.length],initialSlots);
 assert.equal(states[0],retained,'unchanged responses retain one displayed snapshot');
 assert.ok(calls.filter(path=>path.includes('/snapshot?')).every(path=>path==='/api/workbench/snapshot?limit=2000&history_limit=2000&platform=instagram&compact=1'));
 document.visibilityState='hidden';for(const listener of listeners)listener();await flush();
 assert.equal(timers.size,0);await advance(60000);assert.equal(fullReads,121);assert.equal(liveReads,241);
 document.visibilityState='visible';for(const listener of listeners)listener();await flush();assert.equal(timers.size,2);assert.equal(fullReads,122);
 for(const effect of effects)effect.cleanup?.();await flush();assert.equal(timers.size,0);assert.equal(listeners.size,0);
 console.log(JSON.stringify({case:'production-hook-simulated-soak',simulated_minutes:10,route_renders:1200,windows:50,rows:2000,full_reads:121,live_reads:241,peak_scheduled_timers:2,disposed_timers:timers.size,disposed_listeners:listeners.size}));
});
