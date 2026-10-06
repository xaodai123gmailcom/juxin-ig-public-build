import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync, mkdtempSync, rmSync} from 'node:fs';
import {stripTypeScriptTypes, createRequire} from 'node:module';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

// Run the production safety helpers and command handlers without network, a browser,
// or npm dependencies. The optional SSR test also checks the real React markup.
const source=readFileSync(new URL('../src/posting-workspace.tsx',import.meta.url),'utf8');
const css=readFileSync(new URL('../src/posting-workspace.css',import.meta.url),'utf8');
const helperSource=source.slice(0,source.indexOf('function Panel(')).replace(/^import[^\n]*\n/gm,'');
const helpers=await import('data:text/javascript;base64,'+Buffer.from(stripTypeScriptTypes(helperSource)).toString('base64'));
const {postingRetryAction,postingCanRetryCleanup,postingCanModify,postingCanStart,postingReviewMatches,postingReviewCanStart,postingStatusText,postingNeedsAttention,postingRetryLabel}=helpers;
const windows=[{id:'window-a',locked:false},{id:'window-b',locked:true}];
const job=(overrides={})=>({id:'task-a',status:'failed',queue_revision:0,theme:'山林',caption:'原始文案',asset_id:'asset-a',profile_id:'window-a',expected_username:'verified.account',preview:'data:image/png;base64,AA==',message:'提交前账号检查未完成，请核对后重试',window_held:false,submitted_at:null,failure_stage:'pre_submit',failure_code:'account_check_failed',retry_action:'review',retry_blocked_reason:'',can_modify:true,can_retry_cleanup:false,...overrides});
const functionBody=(start,end)=>stripTypeScriptTypes(source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start))));
const retryBody=functionBody(' async function retry(', ' async function confirmModal(');
const confirmBody=functionBody(' async function confirmModal(', ' const retryControls=');
const actionBody=functionBody(' async function action(', ' const jobs=');
const makeRetry=(context)=>new Function(...Object.keys(context),retryBody+';return retry;')(...Object.values(context));
const makeConfirm=(context)=>new Function(...Object.keys(context),confirmBody+';return confirmModal;')(...Object.values(context));

// Inconsistent or stale metadata must never turn a pending/unknown/successful job into a resend.
test('retry requires a failed job plus explicit safe server eligibility',()=>{
 for(const retry_action of ['prepare','review'])assert.equal(postingRetryAction(job({retry_action})),retry_action);
 for(const status of ['preparing','ready','queued','running','submitting','needs_review','unknown_closed','cleanup_pending','completed','cancelled'])assert.equal(postingRetryAction(job({status})),null,status);
 for(const retry_action of [undefined,null,'start','retry',''])assert.equal(postingRetryAction(job({retry_action})),null);
 for(const overrides of [{window_held:true},{can_retry_cleanup:true},{retry_blocked_reason:'目标窗口占用'},{failure_stage:'unknown'},{submitted_at:'2026-10-03T12:00:00Z'}])assert.equal(postingRetryAction(job(overrides)),null,JSON.stringify(overrides));
 assert.equal(postingRetryAction(job({retry_blocked_reason:'  '})),'review');
});

test('verified negative outcome can recover for review with original audit fields still present',()=>{
 const rejected=job({failure_stage:'rejected',submitted_at:'2026-10-03T12:00:00Z',can_modify:false});
 assert.equal(postingRetryAction(rejected),'review');
 assert.equal(postingCanModify(rejected),false);
 assert.equal(postingCanStart(rejected,windows),false);
 assert.equal(postingStatusText(rejected),'提交被明确拒绝');
});

test('held failed jobs offer close-only cleanup while unknown/pending/success never offer resend',()=>{
 const held=job({window_held:true,can_retry_cleanup:true});
 assert.equal(postingRetryAction(held),null);
 assert.equal(postingCanRetryCleanup(held),true);
 assert.equal(postingCanModify(held),false);
 assert.equal(postingCanStart(held,windows),false);
 assert.equal(postingStatusText(held),'失败 · 待关闭窗口');
 assert.equal(postingCanRetryCleanup(job({status:'cleanup_pending',window_held:true,can_retry_cleanup:true})),true);
 assert.equal(postingCanRetryCleanup(job({status:'cancelled',window_held:true,can_retry_cleanup:true})),true);
 for(const status of ['queued','running','submitting','needs_review','unknown_closed','completed'])assert.equal(postingCanRetryCleanup(job({status,window_held:true,can_retry_cleanup:true})),false);
 assert.equal(postingCanRetryCleanup(job({window_held:true,can_retry_cleanup:false})),false,'running cleanup must disable another cleanup command');
});

test('modification permissions honor server denial and cannot unlock non-editable states',()=>{
 for(const status of ['preparing','ready','failed'])assert.equal(postingCanModify(job({status})),true);
 for(const status of ['queued','running','submitting','needs_review','unknown_closed','cleanup_pending','completed','cancelled'])assert.equal(postingCanModify(job({status})),false);
 assert.equal(postingCanModify(job({can_modify:false})),false);
 assert.equal(postingCanModify(job({can_modify:undefined,submitted_at:'submitted'})),false);
 assert.equal(postingCanModify(job({can_modify:undefined,submitted_at:null})),true,'legacy snapshots retain safe edit behavior');
});

test('start requires the current ready job, preview, identity and an existing unlocked window',()=>{
 const ready=job({status:'ready',retry_action:null});
 assert.equal(postingCanStart(ready,windows),true);
 for(const overrides of [{status:'failed'},{status:'queued'},{window_held:true},{can_modify:false},{submitted_at:'submitted'},{preview:''},{profile_id:''},{expected_username:''},{profile_id:'window-b'},{profile_id:'missing-window'}])assert.equal(postingCanStart({...ready,...overrides},windows),false,JSON.stringify(overrides));
});

test('frozen review content accepts live safe reset but does not inherit live tuple mutations',()=>{
 const reviewed=Object.freeze(job());
 const ready={...reviewed,status:'ready',message:'已恢复待启动',retry_action:null};
 assert.equal(postingReviewMatches(reviewed,ready),true);
 assert.equal(postingReviewCanStart(reviewed,ready,windows),true);
 assert.equal(reviewed.status,'failed');assert.equal(reviewed.caption,'原始文案');
 for(const key of ['id','caption','asset_id','profile_id','expected_username','queue_revision']){
  const changed={...ready,[key]:'changed'};
  assert.equal(postingReviewMatches(reviewed,changed),false,key);
  assert.equal(postingReviewCanStart(reviewed,changed,windows),false,key);
 }
 assert.equal(postingReviewCanStart(reviewed,undefined,windows),false,'removed task cannot start');
 for(const status of ['preparing','failed','queued','running','submitting','needs_review','unknown_closed','completed'])assert.equal(postingReviewCanStart(reviewed,{...ready,status},windows),false,status);
});

test('status and retry labels distinguish material preparation, pre-submit, rejected and unknown outcomes',()=>{
 assert.equal(postingStatusText(job({failure_stage:'preparation',retry_action:'prepare'})),'素材准备失败');
 assert.equal(postingRetryLabel(job({retry_action:'prepare'})),'重试准备');
 assert.equal(postingStatusText(job()),'提交前失败');
 assert.equal(postingRetryLabel(job()),'重试并检查');
 assert.equal(postingStatusText(job({status:'needs_review',failure_stage:'unknown'})),'结果待核验');
 assert.equal(postingStatusText(job({status:'unknown_closed',failure_stage:'unknown'})),'结果未知 · 已关闭');
 assert.equal(postingStatusText(job({status:'cleanup_pending'})),'成功 · 清理中');
 assert.equal(postingStatusText(job({status:'preparing'})),'后台准备中');
 for(const status of ['failed','needs_review','unknown_closed','cleanup_pending'])assert.equal(postingNeedsAttention(job({status})),true);
 assert.equal(postingNeedsAttention(job({status:'ready'})),false);
});

test('real retry handler issues only retry, and opens review before a separate explicit start',async()=>{
 for(const retry_action of ['prepare','review']){
  const calls=[],opened=[];
  const retry=makeRetry({postingRetryAction,pending:{current:false},modal:null,openReview:ids=>opened.push(ids),action:async(body,message)=>{calls.push({body,message});return {retried:'task-a',status:retry_action==='prepare'?'preparing':'ready'};}});
  await retry(job({retry_action}));
  assert.deepEqual(calls.map(c=>c.body),[{action:'retry',job_id:'task-a'}]);
  assert.deepEqual(opened,retry_action==='review'?[['task-a']]:[]);
  assert.match(calls[0].message,/准备完成后请检查并启动|不会自动发布/);
 }
});

test('retry from an already-open modal preserves its original reviewed tuple',async()=>{
 const reviewed=Object.freeze(job({caption:'已检查的原文'}));const opened=[],commands=[];
 const retry=makeRetry({postingRetryAction,pending:{current:false},modal:{ids:[reviewed.id],mode:'review',reviewed:[reviewed]},openReview:ids=>opened.push(ids),action:async body=>{commands.push(body);return {retried:reviewed.id};}});
 await retry(job({caption:'后台变化后的文案'}));
 assert.deepEqual(opened,[]);assert.equal(reviewed.caption,'已检查的原文');
 assert.deepEqual(commands,[{action:'retry',job_id:reviewed.id}]);
});

test('busy and unsafe retry handler paths send no command and never open review',async()=>{
 for(const [pending,row] of [[true,job()],[false,job({status:'needs_review'})],[false,job({window_held:true})],[false,job({retry_blocked_reason:'窗口占用'})]]){
  const unexpected=()=>{assert.fail('unsafe retry must be inert')};
  await makeRetry({postingRetryAction,pending:{current:pending},modal:null,openReview:unexpected,action:unexpected})(row);
 }
});

test('the real command gate collapses repeated clicks until command and refresh settle',async()=>{
 const pending={current:false},mutationEpoch={current:0},sent=[],busy=[],notes=[];let release;
 const action=new Function('pending','mutationEpoch','client','setBusy','setError','setNote','refresh','refreshWindows',actionBody+';return action;')(pending,mutationEpoch,{postingCommand:async body=>{sent.push(body);await new Promise(resolve=>release=resolve);return {accepted:true};}},value=>busy.push(value),()=>{},value=>notes.push(value),async()=>{},undefined);
 const retry=makeRetry({postingRetryAction,pending,modal:null,openReview:()=>{},action});
 const first=retry(job());await retry(job());
 assert.equal(sent.length,1);assert.equal(mutationEpoch.current,1);assert.equal(pending.current,true);
 release();await first;
 assert.equal(pending.current,false);assert.deepEqual(busy,[true,false]);assert.equal(notes.length,1);
});

test('the real confirmation handler sends only the immutable reviewed tuple',async()=>{
 const reviewed=Object.freeze(job({status:'ready',caption:'最初确认的原文'})),sent=[],closed=[];
 const confirm=makeConfirm({modal:{ids:[reviewed.id],mode:'review'},pending:{current:false},editing:'',active:[reviewed],canEdit:postingCanModify,canConfirmReview:true,action:async body=>{sent.push(body);return {queued:[reviewed.id]};},setModal:value=>closed.push(value)});
 await confirm();
 assert.deepEqual(sent,[{action:'start',job_ids:[reviewed.id],reviewed:[{id:reviewed.id,caption:reviewed.caption,asset_id:reviewed.asset_id,profile_id:reviewed.profile_id,expected_username:reviewed.expected_username,queue_revision:reviewed.queue_revision}]}]);
 assert.deepEqual(closed,[null]);
});

test('stale live state and invalid edit state block confirmation even if handler is invoked directly',async()=>{
 for(const overrides of [{canConfirmReview:false},{pending:{current:true}},{modal:null},{modal:{ids:['task-a'],mode:'edit'},editing:''},{modal:{ids:['task-a'],mode:'edit'},editing:'changed',active:[job({can_modify:false})]}]){
  const unexpected=()=>assert.fail('blocked confirmation must not mutate');
  await makeConfirm({modal:{ids:['task-a'],mode:'review'},pending:{current:false},editing:'',active:[job()],canEdit:postingCanModify,canConfirmReview:true,action:unexpected,setModal:unexpected,...overrides})();
 }
});

test('actual JSX wires visible retry controls and current status without replacing frozen content',()=>{
 assert.match(source,/reviewed:jobs.filter[^\n]*map\(t=>\(\{\.\.\.t\}\)\)/);
 assert.match(source,/const current=liveJob\(t\)/);
 assert.match(source,/postingReviewCanStart\(t,liveJob\(t\),snapshot.windows\)/);
 assert.match(source,/<pre>\{t.caption\}<\/pre>/);
 assert.match(source,/postingStatusText\(current\)/);
 assert.match(source,/current&&retryControls\(current\)/);
 assert.match(source,/\{retryControls\(t\)\}<\/div><\/article>/);
 assert.match(source,/aria-label=\{`\$\{postingRetryLabel\(t\)\}任务 \$\{t.id\}`\}/);
 assert.match(source,/className="task-failure-message">\{t.message\|\|/);
 assert.match(source,/action:'retry_cleanup',job_id:t.id/);
 assert.match(source,/current\?\.status==='needs_review'/);
 assert.match(source,/confirm_no_repost:true/);
 assert.match(source,/epoch===mutationEpoch.current&&generation===readGeneration.current/);
 assert.match(css,/\.task-failure-details p\{[^}]*white-space:normal;[^}]*overflow-wrap:anywhere/);
 assert.match(css,/\.inline-actions \.posting-retry\{[^}]*grid-column:1\/-1;[^}]*width:100%/);
});

// This is a markup test, not a claim of browser layout or live-publishing verification.
let esbuild;
try{esbuild=await import('esbuild');}catch{}
test('production React markup exposes retries, visible failure reasons and live modal status', {skip:!esbuild?'Local esbuild/React toolchain is not installed':false},async()=>{
 const folder=mkdtempSync(join(tmpdir(),'posting-retry-r61-'));const output=join(folder,'fixture.cjs');
 const rows=[job({id:'preparing-failed',failure_stage:'preparation',retry_action:'prepare',message:'下载未完成，请重试素材准备'}),job(),job({id:'held',window_held:true,can_retry_cleanup:true}),job({id:'unknown',status:'needs_review',retry_action:null,can_modify:false}),job({id:'ready',status:'ready',retry_action:null}),job({id:'closed',status:'unknown_closed',retry_action:null,can_modify:false})];
 const data={jobs:rows,accounts:[],credentials:{pexels_configured:false},totals:{waiting:1,success:0,failed:3,today_success:0,needs_review:1},timezone:'UTC'};
 try{
  await esbuild.build({stdin:{contents:`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {PostingWorkspace} from './renderer/src/posting-workspace';import {resetHooks} from 'posting-test-react';export function render(data,modal){globalThis.__postingRetrySSR={data,modal};resetHooks();return renderToStaticMarkup(<PostingWorkspace snapshot={{windows:${JSON.stringify(windows)}}}/>);}`,resolveDir:resolve('.'),sourcefile:'posting-retry-test.tsx',loader:'tsx'},plugins:[{name:'posting-ssr-state',setup(build){build.onResolve({filter:/^(react|posting-test-react)$/},args=>args.path==='posting-test-react'||args.importer.endsWith('posting-workspace.tsx')?{path:'posting-test-react',namespace:'posting-ssr-state'}:undefined);build.onLoad({filter:/.*/,namespace:'posting-ssr-state'},()=>({contents:`import {useState as originalUseState} from 'react';export {useCallback,useEffect,useRef} from 'react';let calls=0;export function resetHooks(){calls=0;}export function useState(value){const index=calls++;return originalUseState(index===0?globalThis.__postingRetrySSR.data:index===8?globalThis.__postingRetrySSR.modal:value);}`,loader:'js',resolveDir:resolve('.')}));}}],bundle:true,platform:'node',format:'cjs',outfile:output,jsx:'automatic',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
  globalThis.window={collectorCore:{request:async()=>{throw Error('No network or live publish in SSR')},secureGet:async()=>null,secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({})}};
  const {render}=createRequire(import.meta.url)(output);const html=render(data,null);
  for(const text of ['重试准备','重试并检查','重试关闭','下载未完成，请重试素材准备'])assert.ok(html.includes(text),text);
  assert.doesNotMatch(html,/aria-label="重试(?:准备|并检查|关闭)任务 (?:unknown|ready|closed)"/);
  const reviewed=job({caption:'冻结的原始文案'});const live=job({status:'preparing',caption:'后台新文案',retry_action:null,message:'后台素材重新准备中'});
  const modalHtml=render({...data,jobs:[live]},{ids:[reviewed.id],mode:'review',reviewed:[reviewed]});
  assert.match(modalHtml,/<pre>冻结的原始文案<\/pre>/);assert.match(modalHtml,/当前状态：后台准备中/);assert.match(modalHtml,/素材、文案或目标账号已变化/);
  const dialog=modalHtml.slice(modalHtml.indexOf('role="dialog"'));
  assert.doesNotMatch(dialog,/aria-label="重试并检查任务/);assert.match(dialog,/disabled=""[^>]*>确认向以上账号发布 1 条/);
 }finally{delete globalThis.window;delete globalThis.__postingRetrySSR;rmSync(folder,{recursive:true,force:true});}
});
