import assert from 'node:assert/strict';
import test, {after} from 'node:test';
import {readFileSync, mkdtempSync, rmSync} from 'node:fs';
import {stripTypeScriptTypes, createRequire} from 'node:module';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {build} from 'esbuild';

// Run production guards, handlers and JSX against local fixtures only.
// This suite never connects to a browser, server, or Instagram account.
const source=readFileSync(new URL('../src/posting-workspace.tsx',import.meta.url),'utf8');
const helperSource=source.slice(0,source.indexOf('function Panel(')).replace(/^import[^\n]*\n/gm,'');
const helpers=await import('data:text/javascript;base64,'+Buffer.from(stripTypeScriptTypes(helperSource)).toString('base64'));
const {postingCanWithdraw,postingCanModify,postingCanStart,postingReviewMatches,postingReviewCanStart}=helpers;
const windows=[{id:'window-a',name:'窗口 A',locked:false}];
const job=(overrides={})=>({id:'queued-a',status:'queued',queue_revision:1,theme:'山林',caption:'原始文案',asset_id:'asset-a',profile_id:'window-a',expected_username:'verified.account',preview:'data:image/png;base64,AA==',asset_state:'ready',message:'等待空闲窗口',window_held:false,submitted_at:null,can_modify:false,can_withdraw:true,...overrides});
const dataFor=jobs=>({jobs,accounts:[{profile_id:'window-a',username:'verified.account',checked_at:'2026-10-05'}],credentials:{pexels_configured:false},totals:{waiting:jobs.length,success:0,failed:0,today_success:0,needs_review:0},timezone:'UTC'});
const functionBody=(start,end)=>stripTypeScriptTypes(source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start))));
const withdrawBody=functionBody(' async function withdraw(', ' const withdrawControls=');
const actionBody=functionBody(' async function action(', ' const jobs=');
const makeWithdraw=context=>new Function(...Object.keys(context),withdrawBody+';return withdraw;')(...Object.values(context));

// The markup fixture compiles the actual component, replacing only I/O and React
// hooks. Tests invoke onClick from production JSX, not copied action callbacks.
const folder=mkdtempSync(join(tmpdir(),'posting-withdraw-r63-'));
const output=join(folder,'fixture.cjs');
after(()=>{delete globalThis.__postingWithdrawFixture;rmSync(folder,{recursive:true,force:true});});
await build({
 stdin:{contents:`import {PostingWorkspace} from './renderer/src/posting-workspace';import {resetHooks} from 'posting-test-hooks';export function render(fixture){globalThis.__postingWithdrawFixture=fixture;resetHooks();return PostingWorkspace({snapshot:{windows:fixture.windows},refreshWindows:fixture.refreshWindows});}`,resolveDir:resolve('.'),sourcefile:'posting-withdraw-test.tsx',loader:'tsx'},
 plugins:[{name:'posting-withdraw-fixture',setup(build){
  build.onResolve({filter:/^(react|posting-test-hooks)$/},args=>args.path==='posting-test-hooks'||args.importer.endsWith('posting-workspace.tsx')?{path:'hooks',namespace:'posting-withdraw-fixture'}:undefined);
  build.onResolve({filter:/^\.\/core-client$/},args=>args.importer.endsWith('posting-workspace.tsx')?{path:'client',namespace:'posting-withdraw-fixture'}:undefined);
  build.onLoad({filter:/.*/,namespace:'posting-withdraw-fixture'},args=>({contents:args.path==='client'?`export function getCollectorCoreClient(){return globalThis.__postingWithdrawFixture.client;}`:`let stateIndex=0,refIndex=0;export function resetHooks(){stateIndex=0;refIndex=0;}export function useState(initial){const fixture=globalThis.__postingWithdrawFixture,index=stateIndex++;if(!(index in fixture.states))fixture.states[index]=typeof initial==='function'?initial():initial;return [fixture.states[index],value=>{fixture.states[index]=typeof value==='function'?value(fixture.states[index]):value;}];}export function useRef(initial){const refs=globalThis.__postingWithdrawFixture.refs,index=refIndex++;return refs[index]||(refs[index]={current:initial});}export const useCallback=callback=>callback;export const useEffect=()=>{};`,loader:'js',resolveDir:resolve('.')}));
 }}],bundle:true,platform:'node',format:'cjs',outfile:output,jsx:'automatic',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'
});
const {render}=createRequire(import.meta.url)(output);
function fixture({jobs=[job()],modal=null,busy=false,nextJobs,command}={}){
 const states=[];states[0]=dataFor(jobs);states[5]=busy;states[8]=modal;
 const sent=[],refreshes=[];
 const value={states,refs:[],windows,sent,refreshes,client:{
  postingCommand:async body=>{sent.push(body);return command?command(body):{withdrawn:body.job_id,status:'ready'};},
  postingSnapshot:async()=>{refreshes.push('posting');return nextJobs?dataFor(nextJobs):states[0];}
 },refreshWindows:async()=>{refreshes.push('windows');}};
 value.render=()=>render(value);
 return value;
}
function nodes(tree){
 if(Array.isArray(tree))return tree.flatMap(nodes);
 if(!tree||typeof tree!=='object'||!tree.props)return [];
 return [tree,...nodes(tree.props.children)];
}
const text=tree=>Array.isArray(tree)?tree.map(text).join(''):tree&&typeof tree==='object'?text(tree.props?.children):typeof tree==='string'||typeof tree==='number'?String(tree):'';
const buttons=tree=>nodes(tree).filter(node=>node.type==='button');
const withdrawalButtons=tree=>buttons(tree).filter(node=>node.props['aria-label']?.startsWith('撤回排队任务 '));
const dialog=tree=>nodes(tree).find(node=>node.props.role==='dialog');
const settle=()=>new Promise(resolve=>setImmediate(resolve));

// Explicit capability metadata is required even when a local status looks safe.
test('withdraw requires queued, unheld, unsubmitted and explicit server permission',()=>{
 assert.equal(postingCanWithdraw(job()),true);
 for(const can_withdraw of [false,undefined,null,1,'true'])assert.equal(postingCanWithdraw(job({can_withdraw})),false,String(can_withdraw));
 for(const status of ['preparing','ready','running','submitting','needs_review','unknown_closed','cleanup_pending','completed','failed','cancelled'])assert.equal(postingCanWithdraw(job({status})),false,status);
 for(const patch of [{window_held:true},{submitted_at:'2026-10-05T05:00:00Z'},{window_held:true,submitted_at:'submitted'}])assert.equal(postingCanWithdraw(job(patch)),false,JSON.stringify(patch));
});

test('withdraw eligibility never grants generic editing, cancellation or starting',()=>{
 for(const can_modify of [true,false,undefined]){
  const queued=job({can_modify});
  assert.equal(postingCanWithdraw(queued),true);
  assert.equal(postingCanModify(queued),false);
  assert.equal(postingCanStart(queued,windows),false);
 }
});

test('real withdraw handler sends only withdraw and explains the required reselection',async()=>{
 const current=Object.freeze(job()),sent=[];
 const withdraw=makeWithdraw({postingCanWithdraw,pending:{current:false},liveJob:()=>current,action:async(body,message)=>{sent.push({body,message});return {withdrawn:current.id};}});
 await withdraw(current);
 assert.deepEqual(sent.map(call=>call.body),[{action:'withdraw',job_id:current.id}]);
 assert.match(sent[0].message,/素材、文案和历史记录保留/);
 assert.match(sent[0].message,/窗口绑定已清空/);
 assert.match(sent[0].message,/重新选择窗口、检查并启动/);
 assert.equal(current.profile_id,'window-a');assert.equal(current.caption,'原始文案');
});

test('real handler rejects busy, removed, stale, held and submitted live jobs',async()=>{
 for(const current of [undefined,job({status:'running'}),job({status:'ready'}),job({can_withdraw:false}),job({can_withdraw:undefined}),job({window_held:true}),job({submitted_at:'submitted'})]){
  await makeWithdraw({postingCanWithdraw,pending:{current:false},liveJob:()=>current,action:()=>assert.fail('stale withdrawal must not send a command')})(job());
 }
 await makeWithdraw({postingCanWithdraw,pending:{current:true},liveJob:()=>job(),action:()=>assert.fail('busy withdrawal must be inert')})(job());
 await makeWithdraw({postingCanWithdraw,pending:{current:false},liveJob:()=>job(),action:()=>assert.fail('ineligible clicked row must be inert')})(job({status:'running'}));
});

test('real command gate collapses repeated withdraw clicks through snapshot refresh',async()=>{
 const pending={current:false},mutationEpoch={current:0},sent=[],busy=[];let releaseCommand,releaseRefresh;
 const action=new Function('pending','mutationEpoch','client','setBusy','setError','setNote','refresh','refreshWindows',actionBody+';return action;')(pending,mutationEpoch,{postingCommand:async body=>{sent.push(body);await new Promise(resolve=>releaseCommand=resolve);return {withdrawn:body.job_id};}},value=>busy.push(value),()=>{},()=>{},()=>new Promise(resolve=>releaseRefresh=resolve),undefined);
 const withdraw=makeWithdraw({postingCanWithdraw,pending,liveJob:()=>job(),action});
 const first=withdraw(job());await withdraw(job());
 assert.deepEqual(sent,[{action:'withdraw',job_id:'queued-a'}]);
 releaseCommand();await settle();await withdraw(job());
 assert.equal(sent.length,1,'refresh must remain within the busy gate');
 releaseRefresh();await first;
 assert.equal(pending.current,false);assert.deepEqual(busy,[true,false]);assert.equal(mutationEpoch.current,1);
});

test('production row JSX exposes a visible withdrawal while edit/cancel/start stay disabled',()=>{
 const view=fixture().render(),row=nodes(view).find(node=>node.props['data-posting-job']==='queued-a');
 const withdrawal=withdrawalButtons(row);
 assert.equal(withdrawal.length,1);assert.equal(text(withdrawal[0]),'撤回排队');assert.equal(withdrawal[0].props.disabled,false);
 for(const label of ['编辑任务 queued-a','删除任务 queued-a','启动任务 queued-a'])assert.equal(buttons(row).find(node=>node.props['aria-label']===label)?.props.disabled,true,label);
 assert.equal(nodes(row).find(node=>node.type==='select')?.props.disabled,true);
 assert.match(text(view),/撤回排队会保留素材、文案和历史记录，并清空窗口绑定/);
});

test('production JSX never exposes withdraw for unsafe or legacy capability states',()=>{
 for(const patch of [{status:'ready'},{status:'running'},{status:'submitting'},{status:'needs_review'},{status:'failed'},{status:'completed'},{status:'cancelled'},{window_held:true},{submitted_at:'submitted'},{can_withdraw:false},{can_withdraw:undefined}]){
  const current=job(patch),f=fixture({jobs:[current],modal:{ids:['queued-a'],mode:'review',reviewed:[job()]}});
  assert.equal(withdrawalButtons(f.render()).length,0,JSON.stringify(patch));
 }
});

test('production row onClick sends exactly withdraw, then preserves contents with empty assignment',async()=>{
 const current=job(),ready={...current,status:'ready',queue_revision:2,profile_id:'',expected_username:'',can_modify:true,can_withdraw:false};
 const f=fixture({jobs:[current],nextJobs:[ready]});
 withdrawalButtons(f.render())[0].props.onClick();await settle();
 assert.deepEqual(f.sent,[{action:'withdraw',job_id:current.id}]);
 assert.deepEqual(f.refreshes,['posting','windows']);
 assert.equal(f.states[8],null,'row withdrawal must not open a start/review modal');
 assert.equal(f.states[0].jobs[0].asset_id,current.asset_id);assert.equal(f.states[0].jobs[0].caption,current.caption);assert.equal(f.states[0].jobs[0].preview,current.preview);
 const updated=f.render();assert.equal(withdrawalButtons(updated).length,0);
 assert.equal(buttons(updated).find(node=>node.props['aria-label']==='启动任务 queued-a').props.disabled,true);
 assert.equal(nodes(updated).find(node=>node.props['aria-label']==='任务 queued-a 选择窗口').props.value,'');
});

test('production review onClick only withdraws, keeps review open and blocks its old target',async()=>{
 const reviewed=Object.freeze(job()),modal={ids:[reviewed.id],mode:'review',reviewed:[reviewed]};
 const ready={...reviewed,status:'ready',queue_revision:2,profile_id:'',expected_username:'',can_modify:true,can_withdraw:false};
 const f=fixture({modal,nextJobs:[ready]});
 const initialDialog=dialog(f.render()),withdrawal=withdrawalButtons(initialDialog);
 assert.equal(withdrawal.length,1);assert.match(text(initialDialog),/素材、文案和历史记录保留，窗口绑定清空后需重新选择窗口、检查并启动/);
 withdrawal[0].props.onClick();await settle();
 assert.deepEqual(f.sent,[{action:'withdraw',job_id:reviewed.id}]);
 assert.equal(f.states[8],modal,'withdrawal must not close the dialog or swap its reviewed tuple');
 assert.equal(reviewed.profile_id,'window-a');assert.equal(reviewed.expected_username,'verified.account');
 const updated=dialog(f.render());assert.equal(withdrawalButtons(updated).length,0);
 assert.match(text(updated),/素材、文案或目标账号已变化，请返回重新检查后启动/);
 assert.equal(buttons(updated).find(node=>text(node).startsWith('确认向以上账号发布')).props.disabled,true);
 assert.equal(postingReviewCanStart(reviewed,ready,windows),false);
});

test('production review uses live state, never frozen queued eligibility',()=>{
 const reviewed=job(),modal={ids:[reviewed.id],mode:'review',reviewed:[reviewed]},f=fixture({modal});
 assert.equal(withdrawalButtons(dialog(f.render())).length,1);
 f.states[0]=dataFor([job({status:'running',window_held:true,can_withdraw:true})]);
 const updated=dialog(f.render());assert.equal(withdrawalButtons(updated).length,0);assert.match(text(updated),/当前状态：正在执行/);
 f.states[0]=dataFor([]);assert.equal(withdrawalButtons(dialog(f.render())).length,0);
});

test('production controls disable both row and dialog while another command is busy',async()=>{
 const f=fixture({busy:true,modal:{ids:['queued-a'],mode:'review',reviewed:[job()]}});
 f.refs[0]={current:true};
 const controls=withdrawalButtons(f.render());assert.equal(controls.length,2);
 for(const button of controls){assert.equal(button.props.disabled,true);button.props.onClick();}
 await settle();assert.deepEqual(f.sent,[]);
});

test('repeated production onClick sends one withdrawal and disables both visible controls',async()=>{
 let release;
 const f=fixture({modal:{ids:['queued-a'],mode:'review',reviewed:[job()]},command:()=>new Promise(resolve=>release=resolve)});
 const controls=withdrawalButtons(f.render());controls[0].props.onClick();controls[1].props.onClick();
 assert.equal(f.sent.length,1);for(const button of withdrawalButtons(f.render()))assert.equal(button.props.disabled,true);
 release({withdrawn:'queued-a'});await settle();assert.deepEqual(f.sent,[{action:'withdraw',job_id:'queued-a'}]);
});

test('server race rejection stays visible without start, close or optimistic state reset',async()=>{
 const current=job(),modal={ids:[current.id],mode:'review',reviewed:[current]},f=fixture({jobs:[current],modal,command:async()=>{throw Error('任务已开始，不能撤回排队');}});
 withdrawalButtons(dialog(f.render()))[0].props.onClick();await settle();
 assert.deepEqual(f.sent,[{action:'withdraw',job_id:current.id}]);assert.deepEqual(f.refreshes,[]);
 assert.equal(f.states[8],modal);assert.equal(f.states[0].jobs[0],current);assert.equal(f.states[5],false);
 assert.match(text(dialog(f.render())),/任务已开始，不能撤回排队/);
});

// A new queue cycle must invalidate review even when its visible tuple is restored.
test('review revision rejects replay while preserving untouched legacy and same-cycle retry',()=>{
 const reviewed=job({queue_revision:0,status:'failed'});
 const ready={...reviewed,status:'ready',can_modify:true,can_withdraw:false};
 assert.equal(postingReviewMatches(reviewed,ready),true);
 assert.equal(postingReviewCanStart(reviewed,ready,windows),true,'legitimate failed retry may keep its existing review');
 assert.equal(postingReviewMatches({...reviewed,queue_revision:undefined},ready),true,'legacy snapshots default to untouched revision zero');
 assert.equal(postingReviewMatches(reviewed,{...ready,queue_revision:undefined}),true);
 for(const queue_revision of [1,2,7]){
  assert.equal(postingReviewMatches(reviewed,{...ready,queue_revision}),false);
  assert.equal(postingReviewCanStart(reviewed,{...ready,queue_revision},windows),false);
  assert.equal(postingReviewCanStart({...reviewed,queue_revision:undefined},{...ready,queue_revision},windows),false,'missing revision must not match a later queue cycle');
 }
 const failed=job({status:'failed',queue_revision:7});
 assert.equal(postingReviewCanStart(failed,{...failed,status:'ready',can_modify:true},windows),true,'same-cycle failed retry remains reviewable');
});

test('same-target reassignment cannot revive stale production review; fresh review sends current revision',async()=>{
 const reviewed=Object.freeze(job({queue_revision:4}));
 const assigned={...reviewed,status:'ready',queue_revision:6,can_modify:true,can_withdraw:false};
 const modal={ids:[reviewed.id],mode:'review',reviewed:[reviewed]};
 const f=fixture({jobs:[assigned],modal});
 assert.equal(postingCanStart(assigned,windows),true,'current task is otherwise ready after same-target reassignment');
 const staleDialog=dialog(f.render());
 const staleConfirm=buttons(staleDialog).find(node=>text(node).startsWith('确认向以上账号发布'));
 assert.equal(staleConfirm.props.disabled,true);
 assert.match(text(staleDialog),/排队状态、素材、文案或目标账号已变化/);
 staleConfirm.props.onClick();await settle();assert.deepEqual(f.sent,[]);
 assert.equal(f.states[8],modal);
 buttons(staleDialog).find(node=>text(node)==='返回检查').props.onClick();
 buttons(f.render()).find(node=>node.props['aria-label']==='检查任务 queued-a').props.onClick();
 const freshConfirm=buttons(dialog(f.render())).find(node=>text(node).startsWith('确认向以上账号发布'));
 assert.equal(freshConfirm.props.disabled,false);
 freshConfirm.props.onClick();await settle();
 assert.deepEqual(f.sent,[{action:'start',job_ids:[reviewed.id],reviewed:[{id:assigned.id,caption:assigned.caption,asset_id:assigned.asset_id,profile_id:assigned.profile_id,expected_username:assigned.expected_username,queue_revision:6}]}]);
 assert.equal(f.states[8],null);
});

test('fresh legacy production review sends explicit revision zero',async()=>{
 const current=job({status:'ready',queue_revision:undefined,can_modify:true,can_withdraw:false});
 const f=fixture({jobs:[current],modal:{ids:[current.id],mode:'review',reviewed:[current]}});
 const confirm=buttons(dialog(f.render())).find(node=>text(node).startsWith('确认向以上账号发布'));
 assert.equal(confirm.props.disabled,false);confirm.props.onClick();await settle();
 assert.equal(f.sent.length,1);assert.equal(f.sent[0].action,'start');assert.equal(f.sent[0].reviewed[0].queue_revision,0);
});
