import {shareUnchangedJson} from '../src/snapshot-sharing.ts';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import {createViewSnapshotReader} from '../src/workbench-render-state.ts';

const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve()};
const plan=(id)=>({id,profile_id:'native:'+id,name:id,platform:'instagram',serial:1,native:true,notes:'saved',username:'',group:'',revision:1,environment:{}});
const data=()=>({plans:[plan('a'),plan('b')],windows:[],locks:{},events:[],last_dm:{},last_following:{},cloud_sync:''});
const snapshot=()=>({windows:['a','b'].map(id=>({id:'native:'+id,name:id,opened:true,locked:false}))});

function mountWorkspace(client,{initial=data(),source=snapshot(),popup=()=>Promise.resolve(null)}={}){
  const full=readFileSync(new URL('../src/account-workspace.tsx',import.meta.url),'utf8');
  let code=full.slice(full.indexOf('export function AccountWorkspace('),full.indexOf('  const platformIcon='));
  const toolbar=code.indexOf('  const batchToolbar=');
  code=code.slice(0,toolbar)+code.slice(code.indexOf('\n\n  const rows=',toolbar));
  code=code.replace('export function','function')+'return {refresh,command,contextMenu,menuAction,runBatch,isLocked,edit,set,locked,disabled};\n}';
  const names=[...code.matchAll(/\[(\w+),\s*\w+\]\s*=\s*useState/g)].map(match=>match[1]);
  const states=[],refs=[],memos=[],effects=[],timers=new Map(),writes=[];
  let stateIndex=0,refIndex=0,memoIndex=0,effectIndex=0,timerId=0;
  const sameDeps=(a,b)=>a&&b&&a.length===b.length&&a.every((value,i)=>Object.is(value,b[i]));
  const props={snapshot:source,onChanged:async()=>{}};
  const render=runInNewContext(stripTypeScriptTypes(code)+'\n() => AccountWorkspace(props);',{
    props,createViewSnapshotReader,shareUnchangedJson,
    useState(value){const slot=stateIndex++,name=names[slot];if(!(slot in states))states[slot]=name==='data'?initial:name==='checked'?new Set(['a','b']):typeof value==='function'?value():value;
      return[states[slot],next=>{states[slot]=typeof next==='function'?next(states[slot]):next;writes.push({name,value:states[slot]})}]},
    useRef(value){const slot=refIndex++;return refs[slot]||=( {current:value})},
    useCallback(fn,deps){const slot=memoIndex++;if(!sameDeps(memos[slot]?.deps,deps))memos[slot]={value:fn,deps};return memos[slot].value},
    useMemo(fn,deps){const slot=memoIndex++;if(!sameDeps(memos[slot]?.deps,deps))memos[slot]={value:fn(),deps};return memos[slot].value},
    useEffect:effectHook,useLayoutEffect:effectHook,
    getCollectorCoreClient:()=>client,useAccountUnread:()=>({snapshot:{windows:{}},refresh:async()=>{}}),
    fallbackPlatforms:{instagram:'Instagram'},isWhatsApp:()=>false,empty:{notes:'',id:'',name:'',profile_id:'',revision:0},
    document:{getElementById:()=>null,addEventListener(){},removeEventListener(){}},
    window:{location:{href:'app://index/#/accounts'},collectorCore:{accountContextMenu:popup},addEventListener(){},removeEventListener(){},innerWidth:1200,innerHeight:800},
    localStorage:{getItem:()=>'',setItem(){}},sessionStorage:{getItem:()=>null,removeItem(){}},
    setInterval(callback){timers.set(++timerId,callback);return timerId},clearInterval(id){timers.delete(id)},
  });
  function effectHook(fn,deps){const slot=effectIndex++;if(!sameDeps(effects[slot]?.deps,deps)){effects[slot]?.cleanup?.();effects[slot]={fn,deps,pending:true}}}
  const view={writes,timers,props,state:name=>states[names.indexOf(name)],rerender(next){if(next)props.snapshot=next;stateIndex=refIndex=memoIndex=effectIndex=0;Object.assign(view,render());for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.fn()}return view},dispose(){effects.forEach(effect=>effect.cleanup?.())}};
  return view.rerender();
}

test('slow account reads coalesce timer ticks and remain deliverable instead of starving the window list',async()=>{
 const requests=[];const view=mountWorkspace({accountSnapshot(){const request=deferred();requests.push(request);return request.promise}});
 try{
  await flush();
  for(let i=0;i<6;i++)for(const tick of view.timers.values())tick();
  await flush();assert.equal(requests.length,1);
  const fresh=data();fresh.plans[0].name='renamed';requests[0].resolve(fresh);await flush();
  assert.equal(view.state('data').plans[0].name,'renamed');
 }finally{view.dispose()}
});

test('a native context-menu reply rechecks both current lock feeds before dispatch',async()=>{
 for(const feed of ['workbench','accounts']){
  const menu=deferred(),calls=[];let accountValue=data();
  const view=mountWorkspace({accountSnapshot:async()=>accountValue,accountCommand:async body=>{calls.push(body);return{}}},{popup:()=>menu.promise});
  try{
   await flush();view.contextMenu(plan('a'),20,20);
   if(feed==='workbench'){const next=snapshot();next.windows[0].locked=true;view.rerender(next)}
   else{accountValue=data();accountValue.locks['native:a']={operation_type:'collection'};await view.refresh();view.rerender()}
   menu.resolve('close');await flush();
   assert.deepEqual(calls,[],feed+' reported a new occupied window while its native menu was open');
  }finally{view.dispose()}
 }
});

test('an account command fences the pending pre-command read and waits for an authoritative follow-up',async()=>{
 const requests=[];const view=mountWorkspace({accountSnapshot(){const request=deferred();requests.push(request);return request.promise},accountCommand:async()=>({message:'closed'})});
 try{
  await flush();const command=view.command({action:'close',id:'a'},'closed');await flush();
  assert.equal(requests.length,1);
  const stale=data();stale.windows=[{id:'native:a',opened:true}];requests[0].resolve(stale);await flush();
  assert.equal(requests.length,2);
  assert.equal(view.writes.filter(row=>row.name==='data').length,0,'pre-close response cannot reopen the display');
  const fresh=data();fresh.windows=[{id:'native:a',opened:false}];requests[1].resolve(fresh);await command;
  assert.equal(view.state('data').windows[0].opened,false);
  assert.equal(view.state('busy'),false);
 }finally{view.dispose()}
});

test('unmount clears account timer, ignores late values and errors, and prevents queued reads',async()=>{
 for(const failed of [false,true]){
  const requests=[];const view=mountWorkspace({accountSnapshot(){const request=deferred();requests.push(request);return request.promise}});
  await flush();const queued=view.refresh(true);view.dispose();const count=view.writes.length;
  if(failed)requests[0].reject(new Error('late read failure'));else requests[0].resolve(data());
  await queued;await flush();await view.refresh();
  assert.equal(requests.length,1);assert.equal(view.writes.length,count);assert.equal(view.timers.size,0);
 }
});

test('native menu revalidation keeps unlocked actions usable and skips a removed plan',async()=>{
 for(const removed of [false,true]){
  const menu=deferred(),calls=[];let accountValue=data();
  const view=mountWorkspace({accountSnapshot:async()=>accountValue,accountCommand:async body=>{calls.push(body);return{}}},{popup:()=>menu.promise});
  try{
   await flush();view.contextMenu(plan('a'),20,20);
   if(removed){accountValue=data();accountValue.plans=[plan('b')];await view.refresh();view.rerender()}
   menu.resolve('close');await flush();
   assert.deepEqual(calls.map(row=>row.id),removed?[]:['a']);
  }finally{view.dispose()}
 }
});

test('batch commands recheck new task locks after each awaited window command',async()=>{
 const first=deferred(),calls=[];const view=mountWorkspace({accountSnapshot:async()=>data(),accountCommand(body){calls.push(body);return calls.length===1?first.promise:Promise.resolve({})}});
 try{
  await flush();const batch=view.runBatch('close');await flush();assert.equal(calls.length,1);
  const next=snapshot();next.windows[1].locked=true;view.rerender(next);
  first.resolve({});await batch;
  assert.deepEqual(calls.map(row=>row.id),['a']);
  assert.ok(view.state('batchResults').some(row=>row.name==='b'&&row.message.includes('任务占用')));
 }finally{view.dispose()}
});

test('new account reads preserve editing draft and independent workbench task locks',async()=>{
 const requests=[];const view=mountWorkspace({accountSnapshot(){const request=deferred();requests.push(request);return request.promise}});
 try{
  await flush();view.edit(plan('a'));view.set('notes','unsaved operator note');
  const next=snapshot();next.windows[0].locked=true;view.rerender(next);
  requests[0].resolve(data());await flush();view.rerender();
  assert.equal(view.state('form').notes,'unsaved operator note');
  assert.equal(view.isLocked('native:a'),true);
 }finally{view.dispose()}
});


test('slow open and close reserve only their own account and do not freeze the workspace',async()=>{
 const gates=new Map(),calls=[];
 const view=mountWorkspace({accountSnapshot:async()=>data(),accountCommand:body=>{calls.push(body);const gate=deferred();gates.set(body.id,gate);return gate.promise}});
 try{
  await flush();
  const opening=view.command({action:'open',id:'a'},'opened');await flush();view.rerender();
  assert.equal(view.state('busy'),false);
  const closing=view.command({action:'close',id:'b'},'closed');await flush();
  await view.command({action:'close',id:'a'},'duplicate');
  assert.deepEqual(calls.map(c=>c.id),['a','b']);
  gates.get('b').resolve({closed:true});await closing;
  assert.equal(view.state('pendingWindows').a,'open');
  gates.get('a').resolve({opened:true});await opening;
  assert.equal(Object.keys(view.state('pendingWindows')).length,0);
 }finally{for(const g of gates.values())g.resolve({});view.dispose()}
});

test('failed account open clears the stale opening banner and retains the actual refusal',async()=>{
 for(const response of ['throw','page-failed']){
  const gate=deferred();
  const view=mountWorkspace({accountSnapshot:async()=>data(),accountCommand:()=>gate.promise});
  try{
   await flush();
   const pending=view.command({action:'open',id:'a'},'opened');await flush();view.rerender();
   assert.match(view.state('note'),/正在打开/);
   if(response==='throw')gate.reject(new Error('养号窗口清理仍待确认'));
   else gate.resolve({page_loaded:false,message:'窗口核验尚未完成'});
   assert.equal(await pending,false);view.rerender();
   assert.equal(view.state('note'),'');
   assert.match(view.state('error'),/清理仍待确认|核验尚未完成/);
   assert.equal(Object.keys(view.state('pendingWindows')).length,0);
  }finally{gate.resolve({});view.dispose()}
 }
});
