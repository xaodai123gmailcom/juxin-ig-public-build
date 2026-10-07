import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/report-data-overview.tsx',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
const studio=(overrides={})=>({
 totals:[{kind:'nurture',status:'completed',count:23},{kind:'posting',status:'completed',count:7},{kind:'posting',status:'needs_review',count:19},{kind:'material',status:'completed',count:5},{kind:'nurture',status:'failed',count:2}],
 daily:[{day:'2026-10-01',kind:'posting',count:3},{day:'2026-10-01',kind:'nurture',count:9}],
 monitor_totals:{added:140,repeated:17},...overrides,
});
const core=(overrides={})=>({counts:{total_collected:2345,total_public:2100,total_private:245},dedupe:{total:9876},...overrides});
const text=node=>node==null||typeof node==='boolean'?'':Array.isArray(node)?node.map(text).join(''):typeof node==='object'?text(node.props?.children):String(node);
function nodes(node,predicate,result=[]){
 if(Array.isArray(node)){node.forEach(child=>nodes(child,predicate,result));return result}
 if(node&&typeof node==='object'){if(predicate(node))result.push(node);nodes(node.props?.children,predicate,result)}
 return result;
}
function countText(tree){
 const list=nodes(tree,n=>n.type==='dl')[0];
 return Object.fromEntries(nodes(list,n=>n.type==='div').map(row=>[text(nodes(row,n=>n.type==='dt')[0]),text(nodes(row,n=>n.type==='dd')[0])]));
}

/** Execute the production component with React-compatible state/effect ordering. */
function mount({client,props={}}={}){
 const states=[],refs=[],effects=[];let si=0,ri=0,ei=0,writes=0;
 let activeClient=client||{studioSnapshot:async()=>studio()},disposed=false;
 let activeProps={snapshot:core(),snapshotError:null,snapshotLoading:false,refreshSnapshot:async()=>{},...props};
 const equal=(a,b)=>a&&b&&a.length===b.length&&a.every((value,i)=>Object.is(value,b[i]));
 const hooks={
  useState(initial){const i=si++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],next=>{writes++;states[i]=typeof next==='function'?next(states[i]):next}]},
  useRef(initial){return refs[ri++]??={current:initial}},
  useEffect(fn,deps){const i=ei++;if(!equal(effects[i]?.deps,deps)){effects[i]?.cleanup?.();effects[i]={fn,deps,pending:true}}},
 };
 const exports={};
 runInNewContext(compiled,{exports,require(name){
  if(name==='react')return hooks;
  if(name==='react/jsx-runtime')return {jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
  if(name==='lucide-react')return {RefreshCw:()=>null};
  if(name==='./workbench-platform')return {usePlatformCore:()=>activeClient};
  if(name.endsWith('.css'))return {};
  throw Error(`Unexpected import ${name}`);
 }});
 const view={exports,tree:null,
  render(nextProps={}){assert.equal(disposed,false);activeProps={...activeProps,...nextProps};si=ri=ei=0;this.tree=exports.ReportDataOverview(activeProps);for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.fn()}return this},
  replaceClient(next){activeClient=next;return this.render()},
  button(){return nodes(this.tree,n=>n.type==='button'&&text(n).includes('刷新数据概览'))[0]},
  click(){this.button().props.onClick()},
  writes:()=>writes,
  dispose(){effects.forEach(effect=>effect.cleanup?.());disposed=true},
 };
 return view.render();
}

test('lazy mount loads only studio once and reuses all four existing Core counters',async()=>{
 const response=deferred();let studioReads=0,coreReads=0;
 const view=mount({client:{studioSnapshot:()=>{studioReads++;return response.promise},snapshot:()=>{throw Error('duplicate Core request')},workReport:()=>{throw Error('primary report request')}},props:{refreshSnapshot:async()=>{coreReads++}}});
 try{
  assert.equal(studioReads,0,'no fetch during render; effect begins the lazy request');
  assert.equal(coreReads,0);
  const initial=countText(view.tree);
  assert.deepEqual(Object.keys(initial),['总采集','公开账号','私密账号','全局去重','累计检查新增','重复新增','已完成养号轮次']);
  assert.equal(initial['总采集'],'2,345');assert.equal(initial['全局去重'],'9,876');
  for(const label of ['累计检查新增','重复新增','已完成养号轮次'])assert.equal(initial[label],'读取中');
  assert.doesNotMatch(text(view.tree),/暂无/);
  await flush();assert.equal(studioReads,1);
  view.render();view.render();assert.equal(studioReads,1);
  response.resolve(studio());await flush();view.render();
  assert.deepEqual(countText(view.tree),{'总采集':'2,345','公开账号':'2,100','私密账号':'245','全局去重':'9,876','累计检查新增':'140','重复新增':'17','已完成养号轮次':'23'});
  assert.equal(coreReads,0);assert.equal(view.button().props.disabled,false);
  assert.match(text(view.tree),/不受上方当日 \/ 当周 \/ 当月筛选影响/);
  assert.match(text(view.tree),/每日完成记录（UTC）/);assert.doesNotMatch(text(view.tree),/发帖|备稿|posting|material/);

 }finally{view.dispose()}
});

test('missing Core data and failed studio load show unavailable values, never invented zeros or empty success',async()=>{
 const response=deferred();const view=mount({client:{studioSnapshot:()=>response.promise},props:{snapshot:null,snapshotLoading:true}});
 try{
  assert.ok(Object.values(countText(view.tree)).every(value=>value==='读取中'));
  await flush();response.reject(Error('studio offline'));await flush();view.render({snapshotLoading:false,snapshotError:Error('snapshot offline')});
  assert.ok(Object.values(countText(view.tree)).every(value=>value==='—'));
  assert.match(text(view.tree),/snapshot offline/);assert.match(text(view.tree),/studio offline/);
  assert.doesNotMatch(text(view.tree),/暂无/);assert.equal(view.button().props.disabled,false);
 }finally{view.dispose()}
});

test('absent, negative and non-finite Core counts stay unavailable; authoritative zero remains zero',async()=>{
 const view=mount({props:{snapshot:core({counts:{total_collected:0,total_public:NaN,total_private:-1},dedupe:{total:Infinity}})}});
 try{
  await flush();view.render();
  const counts=countText(view.tree);
  assert.equal(counts['总采集'],'0');for(const key of ['公开账号','私密账号','全局去重'])assert.equal(counts[key],'—');
  view.render({snapshot:core({counts:{},dedupe:{}})});
  for(const key of ['总采集','公开账号','私密账号','全局去重'])assert.equal(countText(view.tree)[key],'—');
 }finally{view.dispose()}
});

test('valid empty studio totals establish real zeros and permit honest empty-state messages',async()=>{
 const view=mount({client:{studioSnapshot:async()=>studio({totals:[],daily:[],monitor_totals:{added:0,repeated:0}})}});
 try{
  assert.doesNotMatch(text(view.tree),/暂无/);await flush();view.render();
  for(const key of ['累计检查新增','重复新增','已完成养号轮次'])assert.equal(countText(view.tree)[key],'0');
  assert.match(text(view.tree),/暂无养号任务/);assert.match(text(view.tree),/暂无完成记录/);
 }finally{view.dispose()}
});

test('invalid studio payloads cannot turn missing totals, missing monitoring metrics or invalid rows into zero',async()=>{
 for(const payload of [null,{},studio({totals:undefined}),studio({daily:undefined}),studio({monitor_totals:{added:0}}),studio({totals:[{kind:'nurture',status:'completed',count:NaN}]}),studio({daily:[{day:'2026-10-01',kind:'posting',count:-1}]})]){
  const view=mount({client:{studioSnapshot:async()=>payload}});
  try{await flush();view.render();assert.match(text(view.tree),/统计不完整/);assert.doesNotMatch(text(view.tree),/暂无/);for(const key of ['累计检查新增','重复新增','已完成养号轮次'])assert.equal(countText(view.tree)[key],'—')}
  finally{view.dispose()}
 }
});

test('manual refresh starts both independent sources once and guards synchronous duplicate clicks',async()=>{
 const nextStudio=deferred(),nextCore=deferred();let studioReads=0,coreReads=0;
 const view=mount({client:{studioSnapshot:()=>++studioReads===1?Promise.resolve(studio()):nextStudio.promise},props:{refreshSnapshot:()=>{coreReads++;return nextCore.promise}}});
 try{
  await flush();view.render();view.click();view.click();await flush();view.render();
  assert.equal(studioReads,2);assert.equal(coreReads,1);assert.equal(view.button().props.disabled,true);
  nextStudio.resolve(studio({monitor_totals:{added:151,repeated:18}}));await flush();view.render();
  assert.equal(countText(view.tree)['累计检查新增'],'151');assert.equal(view.button().props.disabled,true,'Core still in flight');
  nextCore.resolve(undefined);await flush();view.render();assert.equal(view.button().props.disabled,false);
 }finally{view.dispose()}
});

test('swallowed Core refresh failure remains visible after studio succeeds and clears only when the prop recovers',async()=>{
 let coreReads=0;const view=mount({props:{snapshotError:Error('authoritative Core failure'),refreshSnapshot:async()=>{coreReads++}}});
 try{
  await flush();view.render();view.click();await flush();view.render();
  assert.equal(coreReads,1);assert.match(text(view.tree),/authoritative Core failure/);
  assert.equal(countText(view.tree)['已完成养号轮次'],'23');assert.match(text(view.tree),/上次成功结果/);
  view.render({snapshotError:null,snapshot:core({counts:{total_collected:2350,total_public:2105,total_private:245}})});
  assert.doesNotMatch(text(view.tree),/authoritative Core failure/);assert.equal(countText(view.tree)['总采集'],'2,350');
 }finally{view.dispose()}
});

test('both refresh errors preserve last known values through retry and independently recover',async()=>{
 let phase=0;const retryStudio=deferred(),retryCore=deferred();
 const view=mount({client:{studioSnapshot:()=>phase===0?Promise.resolve(studio()):phase===1?Promise.reject(Error('studio refresh failed')):retryStudio.promise},props:{refreshSnapshot:()=>phase===1?Promise.reject(Error('Core refresh failed')):retryCore.promise}});
 try{
  await flush();view.render();phase=1;view.click();await flush();view.render();
  assert.match(text(view.tree),/studio refresh failed/);assert.match(text(view.tree),/Core refresh failed/);
  assert.equal(countText(view.tree)['已完成养号轮次'],'23');assert.doesNotMatch(text(view.tree),/暂无/);
  phase=2;view.click();await flush();view.render();
  assert.match(text(view.tree),/studio refresh failed/);assert.match(text(view.tree),/Core refresh failed/);
  retryCore.resolve(undefined);await flush();view.render();
  assert.doesNotMatch(text(view.tree),/Core refresh failed/);assert.match(text(view.tree),/studio refresh failed/);
  retryStudio.resolve(studio({totals:[],daily:[],monitor_totals:{added:0,repeated:0}}));await flush();view.render();
  assert.doesNotMatch(text(view.tree),/refresh failed/);assert.equal(countText(view.tree)['已完成养号轮次'],'0');assert.equal(view.button().props.disabled,false);
 }finally{view.dispose()}
});

test('late success and failure after collapse never write to the unmounted overview',async()=>{
 for(const failed of [false,true]){
  const response=deferred();const view=mount({client:{studioSnapshot:()=>response.promise}});
  await flush();view.dispose();const writes=view.writes();
  if(failed)response.reject(Error('late studio failure'));else response.resolve(studio());
  await flush();assert.equal(view.writes(),writes);
 }
});

test('a source replacement fences stale successes and failures without hiding current data',async()=>{
 for(const failed of [false,true]){
  const stale=deferred();const view=mount({client:{studioSnapshot:()=>stale.promise}});
  try{
   await flush();view.replaceClient({studioSnapshot:async()=>studio({monitor_totals:{added:999,repeated:2}})});
   await flush();view.render();const writes=view.writes();
   if(failed)stale.reject(Error('old source failed'));else stale.resolve(studio({monitor_totals:{added:1,repeated:1}}));
   await flush();assert.equal(view.writes(),writes);view.render();
   assert.equal(countText(view.tree)['累计检查新增'],'999');assert.doesNotMatch(text(view.tree),/old source failed/);
  }finally{view.dispose()}
 }
});

test('manual refresh settling after unmount cannot update errors, counters or busy state',async()=>{
 for(const failed of [false,true]){
  let calls=0;const pendingStudio=deferred(),pendingCore=deferred();
  const view=mount({client:{studioSnapshot:()=>++calls===1?Promise.resolve(studio()):pendingStudio.promise},props:{refreshSnapshot:()=>pendingCore.promise}});
  await flush();view.render();view.click();await flush();view.dispose();const writes=view.writes();
  if(failed){pendingStudio.reject(Error('late studio'));pendingCore.reject(Error('late Core'))}
  else{pendingStudio.resolve(studio());pendingCore.resolve(undefined)}
  await flush();assert.equal(view.writes(),writes);
 }
});

test('reader shares an in-flight refresh and disposed readers cannot start further requests',async()=>{
 const view=mount();await flush();let calls=0;const response=deferred(),seen=[];
 const reader=view.exports.createReportDataReader({read:()=>{calls++;return response.promise},onChange:value=>seen.push(value)});
 try{
  const first=reader.refresh(),second=reader.refresh();assert.equal(first,second);await flush();assert.equal(calls,1);
  reader.dispose();response.resolve(studio());await first;assert.equal(seen.length,1);
  await reader.refresh();assert.equal(calls,1);
  const never=view.exports.createReportDataReader({read:async()=>{throw Error('must not read after disposal')},onChange(){}});
  const request=never.refresh();never.dispose();await request;
 }finally{view.dispose()}
});

test('the overview is read-only, has no polling/subscription, and does not expose retired task statistics',()=>{
 assert.doesNotMatch(source,/studioCommand|configureIntegrations|startWorkbenchSnapshotPolling|setInterval|\.workReport\(|\.snapshot\(/);
 assert.match(source,/client\.studioSnapshot/);assert.doesNotMatch(source,/已确认发帖/);
 assert.match(source,/kind === "completed"|row\.status === "completed"/);
 const css=readFileSync(new URL('../src/report-data-overview.css',import.meta.url),'utf8');
 assert.match(css,/grid-template-columns: repeat\(4/);assert.match(css,/@media \(max-width: 600px\)/);
});
