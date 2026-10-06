import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
import {webcrypto} from 'node:crypto';
import {createCollectorCoreClient,getCollectorCoreClient,startWorkbenchSnapshotPolling} from '../src/core-client.ts';
import {createReviewQueueReader} from '../src/review-queue-state.ts';

const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}};
const snap=(platform='instagram',revision=1)=>({platform,revision,generated_at:new Date().toISOString(),counts:{},dedupe:{total:0},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},windows:[],sources:[],tasks:[],campaigns:[],split_candidates:[],truncated:false});
const bridge=request=>({request,secureGet:async()=>null,secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({})});

test('production account browser fixture renders accessible wolf refresh even with obsolete display preference',async()=>{
 const {build}=await import('esbuild');
 const built=await build({entryPoints:[fileURLToPath(new URL('./fixtures/account-recovery.tsx',import.meta.url))],bundle:true,write:false,outfile:'account-fixture.cjs',platform:'node',format:'cjs',jsx:'automatic',external:['react','react/jsx-runtime','react-dom/server'],define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent',plugins:[{name:'render-fixture-with-real-react',setup(builder){builder.onResolve({filter:/^react-dom\/client$/},()=>({path:'fixture-root',namespace:'fixture'}));builder.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:"import {renderToString} from 'react-dom/server'; export function createRoot(){return {render(tree){globalThis.fixtureMarkup=renderToString(tree)}}}",loader:'js'}));}}]});
 const storage={getItem:()=> 'facebook',setItem(){},removeItem(){}},location={search:'',hash:'#/accounts',href:'http://fixture/account-ui#/accounts'};
 const context={require:createRequire(import.meta.url),module:{exports:{}},exports:{},console,URL,URLSearchParams,setTimeout,clearTimeout,setInterval,clearInterval,crypto:webcrypto,location,localStorage:storage,sessionStorage:storage,window:{location,localStorage:storage,sessionStorage:storage,crypto:webcrypto},document:{getElementById:()=>({})}};
 runInNewContext(built.outputFiles.find(file=>file.path.endsWith('.cjs')).text,context);
 assert.match(context.fixtureMarkup,/<button[^>]*class="formal-brand"[^>]*aria-label="刷新"[^>]*title="刷新"/);assert.match(context.fixtureMarkup,/war-wolf.svg/);
 assert.doesNotMatch(context.fixtureMarkup,/点击切换|Facebook|显示平台|workbench-platform-toggle/i);
 assert.match(context.fixtureMarkup,/data-nav="accounts"[^>]*aria-current="page"/);
});

test('IG snapshot, queue, reports, exports and commands preserve their captured scope and revision fence',async()=>{
 const calls=[],b=bridge(async(path,options)=>{calls.push({path,options});if(path.includes('/snapshot'))return snap();if(path.includes('/commands'))return {command:options.body.type,result:{},snapshot_seq:2};return {platform:'instagram',items:[],total:0}});
 const ig=getCollectorCoreClient(b,'instagram');assert.equal(ig,getCollectorCoreClient(b,'instagram'));
 await ig.snapshot({limit:2,historyLimit:3,compact:true});await ig.reviewQueue({visibility:'public',review_stage:1,offset:500,limit:500});await ig.exportApprovedAccounts({visibility:'private',scope:'selected',candidate_ids:['ig-one']});await ig.workReport('history','start','end');await ig.decideReview({candidate_id:'ig-one',decision:'approved'});
 assert.equal(calls[0].path,'/api/workbench/snapshot?limit=2&history_limit=3&platform=instagram&compact=1');assert.equal(calls[1].options.body.offset,500);
 for(const call of calls.slice(1,4))assert.equal(call.options.body.platform,'instagram');
 assert.equal(calls[4].options.body.payload.platform,'instagram');assert.equal(ig.lastCommandSnapshotSeq,getCollectorCoreClient(b).lastCommandSnapshotSeq);
});

test('unsupported platform clients, requests, commands and responses fail closed before rendering',async()=>{
 const calls=[],b=bridge(async(path,options)=>{calls.push(path);return path.includes('snapshot')?snap('facebook'):{platform:'facebook',items:[]}});
 assert.throws(()=>createCollectorCoreClient(b,'facebook'),/platform/);
 for(const client of [createCollectorCoreClient(b),createCollectorCoreClient(b,'instagram')]){
  await assert.rejects(client.snapshot(),/platform/);await assert.rejects(client.reviewQueue({visibility:'public',review_stage:1}),/platform/);await assert.rejects(client.exportApprovedAccounts({visibility:'public',scope:'all'}),/platform/);
  const count=calls.length;
  assert.throws(()=>client.snapshot({platform:'facebook'}),/platform/);
  await assert.rejects(client.createCollectionTask({platform:'facebook'}),/platform/);
  await assert.rejects(client.reviewQueue({platform:'facebook',visibility:'public',review_stage:1}),/platform/);
  assert.equal(calls.length,count);
 }
});

test('explicit IG queue and export responses still require a matching platform tag',async()=>{
 const client=createCollectorCoreClient(bridge(async()=>({items:[],total:0})),'instagram');
 await assert.rejects(client.reviewQueue({visibility:'public',review_stage:1}),/platform/);
 await assert.rejects(client.exportApprovedAccounts({visibility:'public',scope:'all'}),/platform/);
});

test('disposed polling and review requests cannot paint after route exit',async()=>{
 const old=deferred(),seen=[];const p=startWorkbenchSnapshotPolling({platform:'instagram',onSnapshot:s=>seen.push(s),intervalMs:300000},bridge(()=>old.promise));
 p.stop();old.resolve(snap());await new Promise(r=>setImmediate(r));assert.deepEqual(seen,[]);
 const held=deferred(),reader=createReviewQueueReader({query:()=>({platform:'instagram',visibility:'public',review_stage:1,offset:0,limit:500}),read:()=>held.promise,onValue:v=>seen.push(v),onError:e=>{throw e}});
 const running=reader.refresh();await Promise.resolve();reader.dispose();held.resolve({platform:'instagram',items:[{id:'legacy'}]});await running;assert.deepEqual(seen,[]);
});

test('route shell retains one subscription, compact requests and all IG reports',()=>{
 const code=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 assert.match(code,/function PlatformWorkbenchRoutes/);assert.match(code,/const core=useFormalCore\(\)/);assert.doesNotMatch(code,/<PlatformWorkbenchRoutes key=/);
 assert.match(code,/intervalMs: 5_000/);assert.match(code,/compact: true/);assert.match(code,/2_500/);
 assert.doesNotMatch(code,/facebook|setPlatform|点击狼头切换/i);
});

test('actual pure-IG React fixture compiles and remains in the mandatory Windows gate',async()=>{
 const {build}=await import('esbuild');const result=await build({entryPoints:[fileURLToPath(new URL('./fixtures/workbench-platform.tsx',import.meta.url))],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 assert.ok(result.outputFiles.some(x=>x.path.endsWith('.js')&&x.contents.length>1000));assert.ok(result.outputFiles.some(x=>x.path.endsWith('.css')));
 assert.match(readFileSync(new URL('../../desktop/tests/embedded-browser.integration.cjs',import.meta.url),'utf8'),/require\('\.\/workbench-platform\.integration\.cjs'\)\(\{win,host\}\)/);
});
