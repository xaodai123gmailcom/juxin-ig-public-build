import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {standaloneNurtureDefaults, nurtureCleanupLabel, nurtureConfigIssue, nurtureNeedsCleanup, nurtureSavedConcurrency, nurtureReservedWindows, nurtureVisibleJobs, nurtureCount, nurtureElapsed, nurtureTimestamp} from '../src/standalone-nurture-state.ts';
const job = (id, status, kind='nurture') => ({id, profile_id:id, kind, status, created_at:'2026-10-02T00:00:00Z', result:{}});
test('standalone duration defaults 5 and concurrency preserves all-selected and valid saved values', () => {
  assert.deepEqual(standaloneNurtureDefaults, {minutes:5, concurrency:0});
  for (const value of [0,1,17,1000]) assert.equal(nurtureSavedConcurrency(value),value);
  for (const value of [undefined,null,'1',-1,1.5,1001,Infinity]) assert.equal(nurtureSavedConcurrency(value),0);
});
test('only whole in-range user parameters are accepted without clamping', () => {
  for (const minutes of [1,5,120]) for (const concurrency of [0,1,1000]) assert.equal(nurtureConfigIssue(minutes,concurrency),'');
  for (const minutes of ['',0,121,5.5,NaN,Infinity]) assert.match(nurtureConfigIssue(minutes,0),/时长/);
  for (const concurrency of ['',-1,1001,1.5,NaN,Infinity]) assert.match(nurtureConfigIssue(5,concurrency),/窗口数量/);
});
test('queued, paused, review and still-releasing jobs reserve windows across studio kinds', () => {
  const jobs=['queued','waiting_window','running','paused','needs_review','failed','cancelled','completed'].map(state=>job(state,state));
  jobs.push(job('posting-pending','queued','posting'),job('releasing','completed'));
  assert.deepEqual([...nurtureReservedWindows(jobs,['releasing'])],['queued','waiting_window','running','paused','needs_review','posting-pending','releasing']);
});
test('unknown data never becomes zero and elapsed never comes from planned steps', () => {
  for (const value of [undefined,null,NaN,Infinity,-1,'0']) assert.equal(nurtureCount(value),'未读取');
  assert.equal(nurtureCount(0),'0');assert.equal(nurtureCount(1234567890),'1,234,567,890');
  assert.equal(nurtureElapsed(undefined),'未记录');assert.equal(nurtureElapsed(null),'未记录');
  assert.equal(nurtureElapsed(0),'0 分 0 秒');assert.equal(nurtureElapsed(302.9),'5 分 2 秒');
  assert.equal(nurtureTimestamp(),'未记录');assert.equal(nurtureTimestamp('bad'),'时间未知');
});
test('actual nurture route uses simplified component with no fixed controls or legacy templates', () => {
  const router=readFileSync(new URL('../src/studio-workspace.tsx',import.meta.url),'utf8');
  assert.match(router,/props\.mode==='nurture'\?<StandaloneNurtureWorkspace/);
  const source=readFileSync(new URL('../src/standalone-nurture-workspace.tsx',import.meta.url),'utf8');
  assert.equal((source.match(/type="number"/g)||[]).length,2);
  assert.match(source,/config: \{minutes: Number\(minutes\), concurrency: Number\(concurrency\)\}/);
  assert.doesNotMatch(source,/dwell_min|dwell_max|like_probability|养号模板|datetime-local|start_waiting_nurture/);
  assert.match(source,/job\.result\.account_snapshot/);assert.match(source,/job\.result\.nurture_actual_seconds/);
  assert.doesNotMatch(source,/cursor\/|total_steps\}/);assert.match(source,/养号执行/);
});
test('real React initial screen renders just two numeric settings with explicit unknown/loading states', async () => {
  const {build}=await import('esbuild');
  const {mkdtempSync,rmSync}=await import('node:fs');
  const {tmpdir}=await import('node:os');
  const {join,resolve}=await import('node:path');
  const {createRequire}=await import('node:module');
  const folder=mkdtempSync(join(tmpdir(),'nurture-r6-react-'));
  try {
    const output=join(folder,'fixture.cjs');
    await build({stdin:{contents:`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {StudioWorkspace} from './renderer/src/studio-workspace';export const html=renderToStaticMarkup(<StudioWorkspace mode="nurture" snapshot={{windows:[{id:'free',name:'中文长窗口名称',locked:false}],counts:{},dedupe:{total:0}} as any}/>);`,resolveDir:resolve('.'),sourcefile:'r6-react-test.tsx',loader:'tsx'},bundle:true,platform:'node',format:'cjs',outfile:output,jsx:'automatic',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
    const {html}=createRequire(import.meta.url)(output);
    assert.equal((html.match(/type="number"/g)||[]).length,2);
    assert.match(html,/value="5"/);assert.match(html,/value="0"/);
    assert.equal((html.match(/<dd>未读取<\/dd>/g)||[]).length,3);
    assert.equal((html.match(/<strong>读取中<\/strong>/g)||[]).length,4);
    assert.match(html,/type="checkbox"[^>]*disabled/);
    assert.match(html,/启动养号/);assert.doesNotMatch(html,/养号模板|Reels|最短停留|点赞概率|每窗口轮次|相邻计划错开/);
  } finally {rmSync(folder,{recursive:true,force:true});}
});

const heldJob = (id = 'held', extra = {}) => ({...job(id, 'completed'), message:'执行已完成；窗口占用凭证已失效，保留清理记录等待核验', result:{window_hold:true,window_cleanup:{state:'lease_lost',last_error:'未能确认关闭'},failure:{stage:'旧失败步骤',message:'原始历史错误'}}, ...extra});
test('durable holds reserve windows with zero active workers, including archived history', () => {
  const rows = [heldJob(),heldJob('archived',{deleted_at:'2026-10-03T00:00:00Z'}),job('free','completed'),{...job('stopped','cancelled'),result:{window_hold:true}}];
  const before = JSON.stringify(rows);
  assert.deepEqual([...nurtureReservedWindows(rows,[])],['held','archived']);
  assert.equal(JSON.stringify(rows),before,'read helpers must preserve failure and cleanup history');
});
test('legacy failed, cancelled and posting hold flags match backend admission and do not reserve idle windows', () => {
  const rows=[{...job('failed','failed'),result:{window_hold:true}},
    {...job('cancelled','cancelled'),result:{window_hold:true}},
    {...job('posting','completed','posting'),result:{window_hold:true}},
    heldJob('archived-held',{deleted_at:'2026-10-03T00:00:00Z'})];
  assert.deepEqual([...nurtureReservedWindows(rows,[])],['archived-held']);
  assert.deepEqual([...nurtureReservedWindows(rows,['failed','cancelled','posting'])],['failed','cancelled','posting','archived-held'],'active cleanup workers remain authoritative regardless of historical status');
});
test('held history is actionable in both plan and exceptions regardless of archive flag', () => {
  const rows=[heldJob(),heldJob('archived',{deleted_at:'2026-10-03T00:00:00Z'}),job('normal','completed'),job('failed','failed'),{...job('archived-failed','failed'),deleted_at:'2026-10-03T00:00:00Z'},job('queued','queued')];
  assert.deepEqual(nurtureVisibleJobs(rows,'plan').map(row=>row.id),['held','archived','queued']);
  assert.deepEqual(nurtureVisibleJobs(rows,'errors').map(row=>row.id),['held','archived','failed']);
  assert.deepEqual(nurtureVisibleJobs(rows,'history').map(row=>row.id),['held','archived','normal','failed','archived-failed']);
  assert.equal(nurtureNeedsCleanup({...heldJob(),kind:'posting'}),false);
  assert.equal(nurtureNeedsCleanup({...heldJob(),status:'needs_review'}),false);
  assert.equal(nurtureNeedsCleanup({...heldJob(),result:{window_hold:false}}),false);
});
test('recent accepted jobs remain visible when they fail or complete before the first refresh', () => {
  const rows=[job('old','failed'),job('new-failed','failed'),job('new-completed','completed'),{...job('removed','failed'),deleted_at:'2026-10-03T00:00:00Z'}];
  assert.deepEqual(nurtureVisibleJobs(rows,'plan',['new-failed','new-completed','removed']).map(row=>row.id),['new-failed','new-completed']);
  assert.deepEqual(nurtureVisibleJobs(rows,'errors',['new-completed']).map(row=>row.id),['old','new-failed']);
});
test('a recent stopped result stays visible without reserving its released window or entering exceptions', () => {
  const stopped={...job('new-stopped','cancelled'),result:{nurture_finished_at:'2026-10-02T18:00:00Z',nurture_actual_seconds:17}};
  const held=heldJob('held');
  const rows=[stopped,held];
  assert.deepEqual(nurtureVisibleJobs(rows,'plan',['new-stopped']).map(row=>row.id),['new-stopped','held']);
  assert.deepEqual(nurtureVisibleJobs(rows,'plan').map(row=>row.id),['held']);
  assert.deepEqual(nurtureVisibleJobs(rows,'errors',['new-stopped']).map(row=>row.id),['held']);
  assert.deepEqual(nurtureVisibleJobs(rows,'history',['new-stopped']),rows);
  assert.deepEqual([...nurtureReservedWindows(rows,[])],['held']);
  assert.equal(nurtureNeedsCleanup(stopped),false);assert.equal(nurtureElapsed(stopped.result.nurture_actual_seconds),'0 分 17 秒');
});
test('cleanup labels distinguish held receipts from confirmed release without guessing success', () => {
  assert.match(nurtureCleanupLabel(heldJob()),/凭证已失效/);
  assert.match(nurtureCleanupLabel(heldJob('pending',{result:{window_hold:true}})),/尚待确认/);
  assert.match(nurtureCleanupLabel(heldJob('closed-but-held',{result:{window_hold:true,window_cleanup:{state:'closed'}}})),/尚待确认/);
  assert.equal(nurtureCleanupLabel(heldJob('closed',{result:{window_hold:false,window_cleanup:{state:'closed'}}})),'窗口已关闭并释放');
  assert.equal(nurtureCleanupLabel(heldJob('reconciled',{result:{window_hold:false,window_cleanup:{state:'reconciled_closed'}}})),'已核验窗口关闭，本条历史清理占用已解除');
});

const recoverySource=readFileSync(new URL('../src/standalone-nurture-workspace.tsx',import.meta.url),'utf8');
const handlerCode=(name,next)=>stripTypeScriptTypes(recoverySource.slice(recoverySource.indexOf(`  async function ${name}(`),recoverySource.indexOf(`  async function ${next}(`)));
const loadHandler=(name,next,context)=>new Function(...Object.keys(context),handlerCode(name,next)+`;return ${name};`)(...Object.values(context));
function commandHarness({request=async()=>({status:'completed'}),refresh=async()=>{},refreshWindows=async()=>{}}={}) {
  const feedback={busy:[],start:[],job:[],errors:[],notes:[]}, pending={current:false},alive={current:true},generation={current:0};
  const command=loadHandler('command','start',{
    pending,alive,generation,refresh,refreshWindows,getCollectorCoreClient:()=>({studioCommand:request}),
    setBusy:value=>feedback.busy.push(value),setStartFeedback:value=>feedback.start.push(value),
    setJobFeedback:value=>feedback.job.push(value),setError:value=>feedback.errors.push(value),setNote:value=>feedback.notes.push(value),
  });
  return {command,pending,alive,generation,feedback};
}
test('cleanup command sends the bounded server operation and collapses repeated clicks until both snapshots settle',async()=>{
  let finishRequest,finishWindows,finishSnapshot;
  const calls=[],order=[];
  const harness=commandHarness({request:async body=>{calls.push(body);return new Promise(resolve=>finishRequest=resolve);},refreshWindows:async()=>{order.push('windows');await new Promise(resolve=>finishWindows=resolve);},refresh:async()=>{order.push('studio');await new Promise(resolve=>finishSnapshot=resolve);}});
  const body={action:'control',job_id:'held',operation:'retry_cleanup'};
  const running=harness.command(body,'核验请求已处理',{jobId:'held'});
  await harness.command(body,'duplicate',{jobId:'held'});
  assert.equal(calls.length,1);assert.equal(harness.pending.current,true);assert.equal(harness.generation.current,1);
  finishRequest({status:'completed',cleanup_pending:false});await new Promise(setImmediate);
  assert.deepEqual(order,['windows']);assert.equal(harness.pending.current,true);
  finishWindows();await new Promise(setImmediate);assert.deepEqual(order,['windows','studio']);
  await harness.command(body,'duplicate',{jobId:'held'});assert.equal(calls.length,1);
  finishSnapshot();await running;
  assert.deepEqual(calls,[body]);assert.equal(harness.pending.current,false);assert.deepEqual(harness.feedback.busy,[true,false]);
  assert.equal(harness.feedback.job.at(-1).failed,false);
});
test('server cleanup refusal stays attached to its task and never claims or performs an unlock',async()=>{
  let refreshes=0,windowReads=0;
  const harness=commandHarness({request:async()=>{throw Error('窗口仍在运行，不能核验释放');},refresh:async()=>{refreshes++;},refreshWindows:async()=>{windowReads++;}});
  assert.equal(await harness.command({action:'control',job_id:'held',operation:'retry_cleanup'},'核验请求已处理',{jobId:'held'}),undefined);
  assert.deepEqual(harness.feedback.job.at(-1),{jobId:'held',text:'Error: 窗口仍在运行，不能核验释放',failed:true});
  assert.equal(refreshes,1);assert.equal(windowReads,0);assert.equal(harness.pending.current,false);
});
test('cleanup feedback distinguishes an in-progress close from an affirmative historical reconciliation',async()=>{
  for(const reconciled of [true,false]){
    const harness=commandHarness({request:async()=>({status:'completed',cleanup_pending:!reconciled,cleanup_reconciled:reconciled})});
    await harness.command({operation:'retry_cleanup'},'通用提示',{jobId:'held'});
    assert.match(harness.feedback.job.at(-1).text,reconciled?/本条历史清理占用已解除/:/清理处理中.*继续保留占用/);
  }
});
test('start rejections are local to the start button; a subsequent accepted start clears the prior error',async()=>{
  let reject=true;
  const harness=commandHarness({request:async()=>{if(reject)throw Error('养号窗口清理仍待确认，该窗口不能被其他操作接管');return {job_ids:['new']};}});
  await harness.command({action:'start'},'任务已创建','start');
  assert.match(harness.feedback.start.at(-1).text,/清理仍待确认/);assert.equal(harness.feedback.start.at(-1).failed,true);
  assert.deepEqual(harness.feedback.errors,[''],'start error should not be only an offscreen banner');
  reject=false;await harness.command({action:'start'},'任务已创建','start');
  assert.deepEqual(harness.feedback.start.slice(-2),[null,{text:'任务已创建',failed:false}]);
});
test('accepted start keeps failed job identifiers in the plan and a rejected start preserves selection',async()=>{
  for(const accepted of [true,false]) {
    let ids=['previous'],selected=['free'],tab='errors';const calls=[];
    const start=loadHandler('start','refreshAll',{
      disabled:false,configIssue:'',selectedIssue:'',selected,pending:{current:false},alive:{current:true},minutes:5,concurrency:0,
      crypto:{randomUUID:()=> 'offline-request-id'},command:async(...args)=>{calls.push(args);return accepted?{job_ids:['failed-before-refresh']}:undefined;},
      setRecentJobIds:next=>{ids=next(ids);},setSelected:value=>{selected=value;},setTab:value=>{tab=value;},
    });
    await start();assert.equal(calls.length,1);assert.equal(calls[0][2],'start');
    assert.deepEqual(calls[0][0],{action:'start',kind:'nurture',config:{minutes:5,concurrency:0},profile_ids:['free'],request_id:'offline-request-id'});
    assert.deepEqual(ids,accepted?['previous','failed-before-refresh']:['previous']);
    assert.deepEqual(selected,accepted?[]:['free']);assert.equal(tab,accepted?'plan':'errors');
  }
});
test('cleanup snapshot refresh failure stays honest and unmounted completion does not update feedback',async()=>{
  const harness=commandHarness({refreshWindows:async()=>{throw Error('offline');}});
  const result=await harness.command({operation:'retry_cleanup'},'已处理',{jobId:'held'});
  assert.equal(result.status,'completed');assert.match(harness.feedback.job.at(-1).text,/窗口列表刷新失败/);
  const late=commandHarness();late.alive.current=false;
  await late.command({action:'start'},'已创建','start');
  assert.deepEqual(late.feedback.start,[null]);assert.deepEqual(late.feedback.busy,[true]);
});

test('production React markup exposes archived cleanup actions, safe disabled states and nearby command errors', async () => {
  const {build}=await import('esbuild');const {mkdtempSync,rmSync}=await import('node:fs');
  const {tmpdir}=await import('node:os');const {join,resolve}=await import('node:path');const {createRequire}=await import('node:module');
  const folder=mkdtempSync(join(tmpdir(),'nurture-recovery-r62-'));const output=join(folder,'fixture.cjs');
  try {
    await build({stdin:{contents:`import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {StandaloneNurtureWorkspace} from './renderer/src/standalone-nurture-workspace';import {resetHooks} from 'nurture-test-react';export function render(state){globalThis.__nurtureRecoverySSR=state;resetHooks();return renderToStaticMarkup(<StandaloneNurtureWorkspace snapshot={{windows:state.data.jobs.map(job=>({id:job.profile_id,name:job.profile_id,locked:false}))} as any}/>);}`,resolveDir:resolve('.'),sourcefile:'nurture-recovery-test.tsx',loader:'tsx'},plugins:[{name:'nurture-ssr-state',setup(build){
      build.onResolve({filter:/^(react|nurture-test-react)$/},args=>args.path==='nurture-test-react'||args.importer.endsWith('standalone-nurture-workspace.tsx')?{path:'nurture-test-react',namespace:'nurture-ssr-state'}:undefined);
      build.onLoad({filter:/.*/,namespace:'nurture-ssr-state'},()=>({contents:`import {useState as originalUseState} from 'react';export {useCallback,useEffect,useMemo,useRef} from 'react';let calls=0;export function resetHooks(){calls=0;}export function useState(value){const index=calls++,state=globalThis.__nurtureRecoverySSR;const overrides={0:state.data,11:state.tab||'plan',13:state.startFeedback||null,14:state.jobFeedback||null,15:state.recentIds||[]};return originalUseState(Object.hasOwn(overrides,index)?overrides[index]:value);}`,loader:'js',resolveDir:resolve('.')}));
    }}],bundle:true,platform:'node',format:'cjs',outfile:output,jsx:'automatic',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
    const {render}=createRequire(import.meta.url)(output);
    const archived=heldJob('archived',{deleted_at:'2026-10-03T00:00:00Z'});
    const active=heldJob('active');const failed={...job('new-failed','failed'),message:'刚创建就失败：主页身份无法确认',result:{failure:{stage:'读取主页',message:'原始失败原因'}}};
    const data={jobs:[archived,active,failed],active_ids:['active'],totals:[{kind:'nurture',status:'failed',count:1}]};
    const html=render({data,recentIds:['new-failed'],startFeedback:{text:'启动拒绝：窗口清理仍待确认',failed:true},jobFeedback:{jobId:'archived',text:'窗口仍在运行，不能释放',failed:true}});
    assert.match(html,/data-job-id="archived"/);assert.match(html,/已归档 · 清理占用仍需核验 · 历史保留/);
    assert.match(html,/aria-label="核验窗口清理任务 archived"(?! disabled)/);
    assert.match(html,/aria-label="核验窗口清理任务 active" disabled=""/);
    assert.match(html,/选择窗口 archived" disabled=""/);assert.match(html,/清理待核验/);
    assert.match(html,/最近清理记录：未能确认关闭/);assert.match(html,/原始历史错误/);
    assert.match(html,/data-job-id="new-failed"/);assert.match(html,/刚创建就失败/);assert.match(html,/原始失败原因/);
    const startSection=html.slice(html.indexOf('nurture-start-row'),html.indexOf('</fieldset>'));
    assert.match(startSection,/aria-describedby="nurture-start-feedback"/);assert.match(startSection,/id="nurture-start-feedback"[^>]*role="alert"/);assert.match(startSection,/启动拒绝：窗口清理仍待确认/);
    const archivedCard=html.slice(html.indexOf('data-job-id="archived"'),html.indexOf('</article>'));
    assert.match(archivedCard,/窗口仍在运行，不能释放/);assert.doesNotMatch(archivedCard,/删除异常|重试<|重置|强制释放/);
    const released={...archived,result:{...archived.result,window_hold:false,window_cleanup:{state:'closed',confirmed_at:'2026-10-04T00:00:00Z'}}};
    const releasedData={...data,jobs:[released],active_ids:[]};
    const errors=render({data:releasedData,tab:'errors',jobFeedback:{jobId:released.id,text:'窗口清理核验请求已处理',failed:false}});
    assert.doesNotMatch(errors,/核验窗口清理任务/);assert.match(errors,/核验请求已处理/);assert.match(errors,/历史记录查看/);
    const history=render({data:releasedData,tab:'history'});
    assert.match(history,/原始历史错误/);assert.match(history,/窗口已关闭并释放/);assert.doesNotMatch(history,/核验窗口清理任务/);
    assert.doesNotMatch(history,/选择窗口 archived" disabled=""/,'only an authoritative cleared hold makes the window selectable');
    const reconciled=render({data:{...releasedData,jobs:[{...released,result:{...released.result,window_cleanup:{state:'reconciled_closed',reconciled_at:'2026-10-04T00:00:00Z'}}}]},tab:'history'});
    assert.match(reconciled,/本条历史清理占用已解除/);assert.match(reconciled,/历史清理核验于/);assert.match(reconciled,/原始历史错误/);
  } finally {delete globalThis.__nurtureRecoverySSR;rmSync(folder,{recursive:true,force:true});}
});
