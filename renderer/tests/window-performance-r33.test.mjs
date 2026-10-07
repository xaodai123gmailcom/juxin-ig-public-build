import {accountWindowReconciliationEligible,accountWindowReconciliationReady} from '../src/account-window-reconciliation.ts';
import {shareUnchangedJson} from '../src/snapshot-sharing.ts';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import ts from 'typescript';
import {createViewSnapshotReader} from '../src/workbench-render-state.ts';

const file=process.env.JUXIN_R33_WORKSPACE_SOURCE||new URL('../src/account-workspace.tsx',import.meta.url);
const source=readFileSync(file,'utf8');
const plan=id=>({id,profile_id:'native:'+id,name:id,platform:'instagram',serial:1,native:true,notes:'saved',username:'',group:'',revision:1,environment:{}});
const data=plans=>({plans,windows:[],locks:{},events:[],last_dm:{},last_following:{},cloud_sync:''});

// Run the real component's hooks, derived values and handlers up to the JSX.
// Counts cover those operations, not React reconciliation, native paint or FPS.
function workspace(initial,sourceSnapshot,client={},onChanged){
 let code=source.slice(source.indexOf('export function AccountWorkspace('),source.indexOf('  const platformIcon='));
 const toolbar=code.indexOf('  const batchToolbar=');
 code=code.slice(0,toolbar)+code.slice(code.indexOf('\n\n  const rows=',toolbar));
 code=code.replace('export function','function')+'return {rows,opened,isLocked,windowHint,edit,set,setQuery,setSelectedId,setData,selectedWindow,locked,command,refresh};\n}';
 const names=[...code.matchAll(/\[(\w+),\s*\w+\]\s*=\s*useState/g)].map(match=>match[1]);
 const states=[],refs=[],memo=[],effects=[];let si=0,ri=0,mi=0,ei=0;
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const props={snapshot:sourceSnapshot,onChanged};
 const memoize=(fn,deps)=>{const slot=mi++;if(!same(memo[slot]?.deps,deps))memo[slot]={value:fn(),deps};return memo[slot].value};
 const render=runInNewContext(stripTypeScriptTypes(code)+'\n() => AccountWorkspace(props)',{accountWindowReconciliationEligible,accountWindowReconciliationReady,
  props,createViewSnapshotReader,shareUnchangedJson,
  useState(value){const slot=si++;if(!(slot in states))states[slot]=names[slot]==='data'?initial:typeof value==='function'?value():value;return[states[slot],next=>{states[slot]=typeof next==='function'?next(states[slot]):next}]},
  useRef(value){return refs[ri++]||=( {current:value})},useMemo:memoize,useCallback:(fn,deps)=>memoize(()=>fn,deps),
  useEffect:effect,useLayoutEffect:effect,
  fallbackPlatforms:{instagram:'Instagram'},isWhatsApp:()=>false,empty:{notes:'',id:'',name:'',profile_id:'',revision:0},time:value=>value||'',
  useAccountUnread:()=>({snapshot:{windows:{}},refresh:async()=>{}}),getCollectorCoreClient:()=>({accountSnapshot:()=>new Promise(()=>{}),...client}),
  document:{getElementById:()=>null,addEventListener(){},removeEventListener(){}},window:{collectorCore:{},addEventListener(){},removeEventListener(){},location:{href:'app://index/#/accounts'}},
  localStorage:{getItem:()=>'',setItem(){}},sessionStorage:{getItem:()=>null,removeItem(){}},setInterval:()=>1,clearInterval(){},
 });
 function effect(fn,deps){const slot=ei++;if(!same(effects[slot]?.deps,deps)){effects[slot]?.cleanup?.();effects[slot]={fn,deps,pending:true}}}
 const view={props,state:name=>states[names.indexOf(name)],render(next){if(next)props.snapshot=next;si=ri=mi=ei=0;Object.assign(view,render());for(const e of effects)if(e.pending){e.pending=false;e.cleanup=e.fn()}return view},dispose(){effects.forEach(e=>e.cleanup?.())}};
 return view.render();
}

function measuredWindows(count){
 let accountIdReads=0,lookupPredicates=0;
 const plans=Array.from({length:count},(_,i)=>plan('window-'+i));
 const account=data(plans);account.windows=plans.map(p=>({get id(){accountIdReads++;return p.profile_id},opened:true}));
 const windows=plans.map(p=>({id:p.profile_id,name:p.name,opened:false,locked:false}));
 // The old component creates merged windows with map(). Instrument only those
 // arrays' real find predicates, without changing global Array behavior.
 windows.map=function(fn,thisArg){const merged=Array.prototype.map.call(this,fn,thisArg);merged.find=function(predicate,context){return Array.prototype.find.call(this,(item,index,array)=>{lookupPredicates++;return predicate.call(context,item,index,array)})};return merged};
 return {account,snapshot:{windows},metrics:()=>({accountIdReads,lookupPredicates}),reset(){accountIdReads=lookupPredicates=0}};
}

test('large-window merging and lookups are linear, while typing reuses the unchanged window index',()=>{
 const f=measuredWindows(1000),view=workspace(f.account,f.snapshot);
 try{
  const initial=f.metrics();f.reset();
  for(const query of ['w','wi','win','wind','windo','window','window-','window-9']){view.setQuery(query);view.render()}
  const typing=f.metrics();
  console.log(JSON.stringify({case:'actual-workspace-derived-work',windows:1000,initial,typing,keystrokes:8,remainingRows:view.rows.length}));
  assert.ok(initial.accountIdReads<=3000,'one account inventory pass, not a find for each Core window');
  assert.equal(initial.lookupPredicates,0,'row and control lookups use the current id index');
  assert.equal(typing.accountIdReads,0,'search input does not rebuild an unchanged window join');
  assert.equal(typing.lookupPredicates,0);assert.equal(view.rows.length,111);
 }finally{view.dispose()}
});

test('an authoritative window update immediately refreshes opened state and both lock feeds remain authoritative',()=>{
 const initial=data([plan('a'),plan('b')]);initial.windows=[{id:'native:a',opened:false}];
 const first={windows:[{id:'native:a',name:'a',opened:true,locked:true},{id:'native:b',name:'b',opened:true,locked:false}]};
 const view=workspace(initial,first);
 try{
  assert.equal(view.opened(plan('a')),false);assert.equal(view.isLocked('native:a'),true);
  const account={...initial,windows:[{id:'native:a',opened:true}],locks:{'native:b':{operation_type:'collection'}}};view.setData(account);view.render();
  assert.equal(view.opened(plan('a')),true);assert.equal(view.isLocked('native:a'),true);assert.equal(view.isLocked('native:b'),true);
  view.render({windows:first.windows.map(w=>({...w,locked:false}))});
  assert.equal(view.isLocked('native:a'),false);assert.equal(view.isLocked('native:b'),true);
  view.setData({...account,locks:{}});view.render();assert.equal(view.isLocked('native:b'),false);
 }finally{view.dispose()}
});

test('indexing preserves first-match behavior and does not let account inventory overwrite task-lock metadata',()=>{
 const initial=data([plan('a')]);initial.windows=[{id:'native:a',opened:false},{id:'native:a',opened:true}];
 const first={windows:[{id:'native:a',name:'first',opened:true,locked:true},{id:'native:a',name:'second',opened:true,locked:false}]};
 const view=workspace(initial,first);
 try{
  assert.equal(view.selectedWindow.name,'first');assert.equal(view.opened(plan('a')),false);assert.equal(view.isLocked('native:a'),true);
  view.setData({...initial,windows:[{id:'native:a',opened:true,locked:false}]});view.render();
  assert.equal(view.isLocked('native:a'),true,'opened inventory cannot weaken the Core task lock');
 }finally{view.dispose()}
});

test('search, current selection and an unsaved note survive inventory and task updates',()=>{
 const initial=data([plan('a'),plan('b')]);const first={windows:[{id:'native:a',name:'a',opened:true,locked:false},{id:'native:b',name:'b',opened:false,locked:false}]};
 const view=workspace(initial,first);
 try{
  view.setQuery('b');view.setSelectedId('b');view.edit(plan('b'));view.set('notes','unsaved note');view.render();
  view.setData({...initial,windows:[{id:'native:b',opened:true}]});view.render({windows:first.windows.map(w=>({...w,locked:w.id==='native:b'}))});
  assert.equal(view.state('query'),'b');assert.equal(view.state('selectedId'),'b');assert.equal(view.state('form').notes,'unsaved note');
  assert.equal(view.rows.length,1);assert.equal(view.selectedWindow.id,'native:b');assert.equal(view.locked,true);
 }finally{view.dispose()}
});

test('r94 switching among 500 windows does not rescan every plan for every tooltip',()=>{
 let profileReads=0;
 const plans=Array.from({length:500},(_,i)=>({...plan('switch-'+i),get profile_id(){profileReads++;return 'native:switch-'+i}}));
 const initial=data(plans),first={windows:plans.map(p=>({id:p.profile_id,name:p.name,opened:true,locked:false}))};
 const view=workspace(initial,first);
 try{
  profileReads=0;
  for(let selection=0;selection<5;selection++){
   view.setSelectedId('switch-'+selection);view.render();
   for(const row of view.rows){view.windowHint(row);view.windowHint(row)}
  }
  console.log(JSON.stringify({case:'switching-plan-reads',windows:500,switches:5,profileReads}));
  assert.ok(profileReads<40000,'switching must remain linear rather than scan 500 plans inside each tooltip');
 }finally{view.dispose()}
});

test('account data renders usable windows before the first global snapshot and preserves account locks',()=>{
 const initial=data([plan('a')]);
 initial.windows=[{id:'native:a',name:'Independent',opened:true}];
 initial.locks={'native:a':{operation_type:'collection'}};
 const view=workspace(initial,null);
 try{
  assert.equal(view.rows.length,1);assert.equal(view.selectedWindow.name,'Independent');
  assert.equal(view.opened(plan('a')),true);assert.equal(view.isLocked('native:a'),true);
  view.render({windows:[{id:'native:a',name:'Global',opened:true,locked:false}]});
  assert.equal(view.isLocked('native:a'),true,'global recovery cannot clear independently read lease');
 }finally{view.dispose()}
});

test('saved account command releases controls while account, unread and global refreshes are delayed',async()=>{
 let resolveCommand,acknowledged=false,refreshes=0;
 const held=new Promise(resolve=>{resolveCommand=resolve});
 const view=workspace(data([plan('a')]),{windows:[]},{accountCommand:()=>held},()=>{refreshes++;return new Promise(()=>{})});
 try{
  const command=view.command({action:'notes',id:'a',revision:1,notes:'saved'},'Saved').then(value=>{acknowledged=value});
  view.render();assert.equal(view.state('busy'),true);
  resolveCommand({saved:true});
  for(let i=0;i<30;i++)await Promise.resolve();
  view.render();assert.equal(acknowledged,true,'save acknowledgement waits for unrelated snapshot');
  assert.equal(view.state('busy'),false);assert.equal(refreshes,1);
  await command;
 }finally{resolveCommand({saved:true});view.dispose()}
});

test('account read errors clear after recovery without clearing an unrelated command error',async()=>{
 let fail=true;
 const initial=data([plan('a')]);
 const view=workspace(initial,{windows:[]},{accountSnapshot:async()=>{if(fail)throw Error('read failed');return initial},accountCommand:async()=>{throw Error('save rejected')}});
 try{
  await view.refresh();view.render();assert.match(view.state('readError'),/read failed/);
  assert.equal(await view.command({action:'notes',id:'a'},'Saved'),false);
  fail=false;await view.refresh(true);view.render();
  assert.equal(view.state('readError'),'');assert.match(view.state('error'),/save rejected/);
 }finally{view.dispose()}
});

test('actual account route mounts independently during first load and total snapshot failure',()=>{
 const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
 const component=source.slice(source.indexOf('function LiveFormalWorkbench('),source.indexOf('\nfunction WorkbenchRoutes('));
 const code=ts.transpileModule(component+'\nexport {LiveFormalWorkbench}',{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const exports={};
 runInNewContext(code,{exports,useWorkbenchPlatform:()=>({platform:"instagram"}),PAGE_COPY:{accounts:{title:'Accounts'}},truncatedSnapshotScopes:()=>[],
  Rail:'Rail',ChatGPTButton:'ChatGPT',GoogleTranslatorButton:'Translate',RefreshCw:'Refresh',StableAccountWorkspace:'Account',
  require(name){assert.equal(name,'react/jsx-runtime');return {jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})}}});
 function all(node){return node&&typeof node==='object'?[node,...[node.props?.children].flat().flatMap(all)]:[]}
 for(const loading of [true,false]){
  const tree=exports.LiveFormalWorkbench({mode:'accounts',core:{snapshot:null,error:loading?null:Error('timeout'),loading,notice:null,refresh:async()=>{}}});
  const account=all(tree).find(node=>node.type==='Account');
  assert.ok(account,'account page is hidden behind the global snapshot');assert.equal(account.props.snapshot,null);
 }
});
