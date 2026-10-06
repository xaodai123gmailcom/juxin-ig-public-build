import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import ts from 'typescript';
import {shareUnchangedJson} from '../src/snapshot-sharing.ts';
import {sameAccountWorkspaceInputs} from '../src/workbench-render-state.ts';
import {collectionOperationalHistoryRows, operationalHistoryGroups, operationalHistoryTypeLabel} from '../src/operational-history.ts';

const clone=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};

test('equal authoritative JSON preserves every branch without mutating inputs',()=>{
 const previous={revision:1,windows:[{id:'a',locked:true}],pending:{public:[{id:'p',profile:{posts:1}}]}};
 const incoming=clone(previous),saved=clone(incoming);
 const result=shareUnchangedJson(previous,incoming);
 assert.equal(result,previous);assert.deepEqual(incoming,saved);
 const changed=clone(previous);changed.revision=2;changed.pending.public[0].profile.posts=2;
 const next=shareUnchangedJson(previous,changed);
 assert.equal(next.windows,previous.windows);assert.notEqual(next.pending,previous.pending);
 assert.equal(previous.pending.public[0].profile.posts,1);assert.equal(next.pending.public[0].profile.posts,2);
});

test('removed fields, cleared data, reordered accounts and new protocol fields remain authoritative',()=>{
 const previous={windows:[{id:'a',locked:true},{id:'b',locked:false}],obsolete:true,details:{x:1},future:null};
 for(const incoming of [
  {windows:[{id:'b',locked:false},{id:'a',locked:true}],details:null,future:{new_field:1}},
  {windows:[],details:{},future:null},
  {windows:[{id:'a',locked:false}],details:{x:2},future:false},
 ]){
  const result=shareUnchangedJson(previous,incoming);
  assert.deepEqual(result,incoming);assert.equal(Object.hasOwn(result,'obsolete'),false);
 }
 assert.equal(shareUnchangedJson([1],null),null);
 assert.deepEqual(shareUnchangedJson([1],{0:1}),{0:1});
});

test('500-window unchanged full snapshots and task heartbeats preserve account memo while real locks invalidate it',()=>{
 const onChanged=async()=>{};
 let current={revision:1,windows:Array.from({length:500},(_,i)=>({id:String(i),locked:false})),tasks:[{id:'task',processed:0}]};
 let props={snapshot:current,onChanged},renders=1;
 for(let i=1;i<=100;i++){
  const incoming=clone(current);incoming.revision++;incoming.tasks[0].processed=i;
  current=shareUnchangedJson(current,incoming);
  const next={snapshot:current,onChanged};
  if(!sameAccountWorkspaceInputs(props,next))renders++;
  props=next;
 }
 assert.equal(renders,1);
 const changed=clone(current);changed.windows[250].locked=true;
 const next=shareUnchangedJson(current,changed);
 assert.equal(sameAccountWorkspaceInputs(props,{snapshot:next,onChanged}),false);
 assert.equal(next.windows[249],current.windows[249]);assert.notEqual(next.windows[250],current.windows[250]);
 assert.equal(sameAccountWorkspaceInputs(props,{...props,onChanged:()=>{}}),false);
 console.log(JSON.stringify({case:'account-memo-inputs',windows:500,unchangedUpdates:100,renders}));
});

function mountUnread(initial){
 const code=ts.transpileModule(readFileSync(new URL('../src/account-unread.tsx',import.meta.url),'utf8'),{
  compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}
 }).outputText;
 const states=[],refs=[],memos=[],effects=[];let si=0,ri=0,mi=0,ei=0,writes=0,value=initial,error=false;
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((x,i)=>Object.is(x,b[i]));
 const memo=(fn,deps)=>{const i=mi++;if(!same(memos[i]?.deps,deps))memos[i]={deps,value:fn()};return memos[i].value};
 const hooks={createContext:()=>({Provider:'Provider'}),useContext:()=>null,
  useState(initial){const i=si++;if(!(i in states))states[i]=initial;return [states[i],next=>{const result=typeof next==='function'?next(states[i]):next;if(!Object.is(result,states[i]))writes++;states[i]=result}]},
  useRef(initial){return refs[ri++]??={current:initial}},useMemo:memo,useCallback:(fn,deps)=>memo(()=>fn,deps),
  useEffect(fn,deps){const i=ei++;if(!same(effects[i]?.deps,deps))effects[i]={deps,fn,pending:true}},
 };
 const exports={};
 runInNewContext(code,{exports,window:{collectorCore:{}},setInterval:()=>1,clearInterval(){},
  require(name){
   if(name==='react')return hooks;
   if(name==='react/jsx-runtime')return {jsx:(type,props)=>({type,props})};
   if(name==='./snapshot-sharing')return {shareUnchangedJson};
   if(name==='./core-client')return {getCollectorCoreClient:()=>({accountUnread:async()=>{if(error)throw Error('offline');return clone(value)}})};
   if(name.endsWith('.css'))return {};
   throw Error(name);
  }
 });
 const view={render(){si=ri=mi=ei=0;this.tree=exports.AccountUnreadProvider({children:'content'});for(const e of effects)if(e.pending){e.pending=false;e.cleanup=e.fn()}return this},
  async refresh(next=value,failed=false){value=next;error=failed;await this.tree.props.value.refresh();await flush();this.render()},
  writes:()=>writes,dispose(){effects.forEach(e=>e.cleanup?.())}};
 return view.render();
}

test('actual unread provider does not redraw identical polls; count, error and recovery changes still propagate',async()=>{
 const initial={windows:{a:{count:3,capped:false,status:'live',observed_at:'saved'}},total:3,capped:false,unknown:0};
 const view=mountUnread(initial);
 try{
  await flush();view.render();const original=view.tree.props.value,savedWrites=view.writes();
  for(let i=0;i<30;i++)await view.refresh();
  assert.equal(view.writes(),savedWrites);assert.equal(view.tree.props.value,original);
  const changed=clone(initial);changed.windows.a.count=4;changed.total=4;await view.refresh(changed);
  assert.equal(view.tree.props.value.snapshot.total,4);
  await view.refresh(changed,true);assert.equal(view.tree.props.value.snapshot.windows.a.status,'stale');
  const stale=view.tree.props.value;await view.refresh(changed,true);assert.equal(view.tree.props.value,stale);
  await view.refresh(changed);assert.equal(view.tree.props.value.snapshot.windows.a.status,'live');
  await view.refresh({...changed,windows:{},total:0});assert.equal(Object.keys(view.tree.props.value.snapshot.windows).length,0);
 }finally{view.dispose()}
});

test('history presentation pages remain bounded and preserve access to every loaded record',async()=>{
 const {historyPage,HISTORY_RENDER_PAGE_SIZE}=await import('../src/history-pagination.ts');
 for(const size of [0,99,100,101,2000,20000,100000]){
  const records=Array.from({length:size},(_,id)=>({id})),first=historyPage(records,0),seen=[];
  for(let page=0;page<first.pages;page++){
   const value=historyPage(records,page);assert.ok(value.items.length<=HISTORY_RENDER_PAGE_SIZE);assert.equal(value.total,size);seen.push(...value.items.map(x=>x.id));
  }
  assert.deepEqual(seen,records.map(x=>x.id));assert.equal(records.length,size);
  assert.equal(historyPage(records,999999).page,first.pages-1);assert.equal(historyPage(records,-1).page,0);
 }
});

test('actual production history components mount bounded rows at 1k, 10k and 100k inventory sizes',async()=>{
 const React=await import('react'),{renderToStaticMarkup}=await import('react-dom/server');
 const {historyPage,HISTORY_RENDER_PAGE_SIZE}=await import('../src/history-pagination.ts');
 const code=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 const ast=ts.createSourceFile('workbench.tsx',code,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 const names=new Set(['useHistoryPage','HistoryPagination','HistoryWorkspace','ImmutableOperationalHistory','ActionHistory']);
 const chosen=ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.has(n.name?.text));assert.equal(chosen.length,names.size);
 const compiled=ts.transpileModule(chosen.map(n=>n.getText(ast)).join('\n'),{fileName:'history.tsx',compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022}}).outputText;
 const el=React.createElement,icon=()=>null;
 const ctx={React,useMemo:React.useMemo,useState:React.useState,historyPage,HISTORY_RENDER_PAGE_SIZE,Map,Set,String,Array,
  collectionOperationalHistoryRows,operationalHistoryGroups,operationalHistoryTypeLabel,
  History:icon,ChevronDown:icon,X:icon,Archive:icon,ShieldCheck:icon,FileClock:icon,
  Panel:({title,actions,children})=>el('section',{},el('h2',{},title),actions,children),
  CandidateRow:({candidate,actions})=>el('article',{'data-history-candidate':candidate.id},candidate.username,actions),
  StatusBadge:({status})=>el('span',{},status),EmptyState:({title})=>el('p',{},title),HistoryTotals:()=>null,
  asRecord:v=>v||{},formatCount:String,formatTime:String,normalizeStatus:v=>v,
  SUCCESS_STATES:new Set(['completed']),FAILED_STATES:new Set(['failed']),
  approvalHistoryOf:s=>s.history.approvals,retainedHistoryOf:s=>s.retained_history,
  mergedCampaignHistory:s=>s.history.actions,successfulActionRows:campaigns=>campaigns.flatMap(c=>c.successes||[]),collectionExclusionReason:r=>r.reason,
 };
 const components=runInNewContext(compiled+'\n({HistoryWorkspace,ActionHistory});',ctx);
 for(const size of [1000,10000,100000]){
  const records=Array.from({length:size},(_,i)=>({id:String(i),username:'person_'+i,visibility:'public',profile:{},screening:{},status:'completed',reviewed_at:'2026-10-01',created_at:'2026-10-01',reason:'excluded'}));
  const snapshot={history:{manual_rejections:records,collection_exclusions:records,approvals:records,tasks:records,actions:[]},tasks:[],campaigns:[],approved:{public:[],private:[]},windows:[],counts:{rejected:size,collection_excluded:size},storage:{retained_history:{approved:size}}};
  const html=renderToStaticMarkup(el(components.HistoryWorkspace,{snapshot}));
  assert.equal((html.match(/data-history-candidate=/g)||[]).length,200,'only two candidate pages mount');
  assert.equal((html.match(/class="formal-exclusion-reason"/g)||[]).length,100,'one exclusion page mounts');
  assert.equal((html.match(/<tr>/g)||[]).length,101,'one task page plus header mounts');
  assert.equal((html.match(/翻页"/g)||[]).length,4,'all four history categories remain navigable');
  assert.equal(snapshot.history.approvals.length,size);assert.match(html,new RegExp('总数 '+size));
 }
 const collapsed=renderToStaticMarkup(el(components.ActionHistory,{campaigns:[{successes:Array.from({length:10000},(_,i)=>({campaignId:'c',targetId:String(i),username:'p'+i,operation:'greet'}))}],windows:[],operation:'greet'}));
 assert.equal((collapsed.match(/<tr>/g)||[]).length,0,'closed action history does not mount thousands of hidden rows');assert.match(collapsed,/10000/);
});
