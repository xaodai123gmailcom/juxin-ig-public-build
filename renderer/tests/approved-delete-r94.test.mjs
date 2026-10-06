import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import {collectionPlatform} from '../src/collection-platform.ts';
import ts from 'typescript';
const workbench=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
const start=workbench.indexOf('function ActionWorkspace('),end=workbench.indexOf('\ntype SuccessfulActionRow',start);
const source=ts.transpileModule(workbench.slice(start,end)+'\nexports.ActionWorkspace=ActionWorkspace;',{
 compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}
}).outputText;
const textOf=n=>n==null||typeof n==='boolean'?'':typeof n==='string'||typeof n==='number'?String(n):Array.isArray(n)?n.map(textOf).join(''):textOf(n.props?.children);
const flush=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}};
const candidate=id=>({id,username:id,status:'approved',profile:{}});
function mount(operation='greet',platform='instagram'){
 const states=[],refs=[],effects=[],notices=[],calls=[];
 let si=0,ri=0,ei=0,dirty=false;
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((x,i)=>Object.is(x,b[i]));
 const hooks={
  useState(initial){const i=si++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],next=>{const v=typeof next==='function'?next(states[i]):next;if(!Object.is(states[i],v)){states[i]=v;dirty=true}}]},
  useRef(initial){return refs[ri++]??={current:initial}},
  useEffect(fn,deps){const i=ei++;if(!same(effects[i]?.deps,deps))effects[i]={fn,deps,pending:true}},
 };
 const data={public:[candidate('public-a'),candidate('public-b')],private:[candidate('private-a'),candidate('private-b')]};
 const history=[];
 const snapshot={approved:data,counts:{},campaigns:[],split_candidates:[],windows:[],dedupe:{}};
 const api={async dismissApprovedCandidate(id){calls.push(id);for(const lane of ['public','private'])data[lane]=data[lane].filter(x=>x.id!==id);dirty=true;return {candidate_id:id,global_dedupe_retained:true}}};
 api.markApprovedCandidateUsed=async id=>{history.push(id);return api.dismissApprovedCandidate(id)};
 const props={snapshot,operation,disabled:()=>false,async run(key,action,success){try{await action(api);if(success)notices.push(success);return true}catch(e){notices.push(e.message);return false}}};
 const jsx=(type,props)=>({type,props:props??{}}),exports={};
 const icons=['MessageCircle','LockKeyhole','Play','AlertTriangle','Check','UserCheck','Download','Plus','Trash2','Clock3'];
 runInNewContext(source,{exports,collectionPlatform,usePlatformCore:()=>api,useWorkbenchPlatform:()=>({platform}),...hooks,Set,Map,Number,String,Promise,Math,Error,
  ...Object.fromEntries(icons.map(x=>[x,x])),Panel:'Panel',CandidateRow:'CandidateRow',WindowSelector:'WindowSelector',StatCard:'StatCard',CampaignList:'CampaignList',FailureList:'FailureList',ActionHistory:'ActionHistory',EmptyState:'EmptyState',
  mergedCampaignHistory:()=>[],parseGreetingMessages:()=>({ok:true,messages:['hi']}),normalizeStatus:x=>x,
  ACTIVE_STATES:new Set(),PAUSED_STATES:new Set(),SUCCESS_STATES:new Set(),FAILURE_TARGET_STATES:new Set(),
  successfulActionRows:()=>[],candidateName:c=>c.username,retainLiveSelection:(current,live)=>{const ids=[...current].filter(x=>live.has(x));return ids.length===current.size?current:new Set(ids)},
  firstNumber:()=>null,asRecord:x=>x,formatCount:String,countSuccessfulTargets:()=>0,MAX_GREETING_MESSAGE_CHARACTERS:1000,
  require(name){if(name==='react/jsx-runtime')return {jsx,jsxs:jsx,Fragment:'fragment'};throw new Error(name)}
 });
 const view={tree:null,data,props,api,calls,notices,history,
  render(){si=ri=ei=0;dirty=false;this.tree=exports.ActionWorkspace(props);for(const e of effects)if(e.pending){e.pending=false;e.fn()}return this},
  async settle(){for(let i=0;i<50;i++){await flush();if(!dirty)return this;this.render()}throw new Error('not settled')},
  all(type){const out=[];function visit(n){if(Array.isArray(n))return n.forEach(visit);if(!n||typeof n!=='object')return;if(n.type===type)out.push(n);visit(n.props.children);visit(n.props.actions)}visit(this.tree);return out},
  button(label){const found=this.all('button').filter(n=>textOf(n).includes(label));assert.equal(found.length,1,label);return found[0]},
  click(label){const b=this.button(label);assert.equal(!!b.props.disabled,false,label);return b.props.onClick()},
  selected(){return this.all('CandidateRow').filter(n=>n.props.selected).map(n=>n.props.candidate.id)},
  text(){return textOf(this.tree)},
 };
 return view.render();
}
for(const operation of ['greet','follow'])test(`${operation} selects displayed accounts and deletes frozen selection once, retaining newcomers and other lane`,async()=>{
 const v=mount(operation),lane=operation==='greet'?'public':'private',other=lane==='public'?'private':'public';await v.settle();
 assert.equal(v.button('一键删除选中').props.disabled,true);v.click('一键全选');await v.settle();
 v.data[lane]=[...v.data[lane],candidate('incoming')];v.render();await v.settle();
 assert.deepEqual(v.selected(),[lane+'-a',lane+'-b']);
 const handler=v.button('一键删除选中').props.onClick;handler();handler();await v.settle();
 assert.deepEqual(v.calls,[lane+'-a',lane+'-b']);assert.deepEqual(v.data[lane].map(x=>x.id),['incoming']);assert.equal(v.data[other].length,2);
 assert.match(v.notices.at(-1),/已删除选中 2 个/);assert.deepEqual(v.selected(),[]);
});
test('partial failures retain only failed checkboxes and retry only failed IDs',async()=>{
 const v=mount();await v.settle();v.click('一键全选');await v.settle();const original=v.api.dismissApprovedCandidate;
 v.api.dismissApprovedCandidate=async id=>{if(id==='public-b')throw new Error('保存失败');return original(id)};
 v.click('一键删除选中');await v.settle();assert.deepEqual(v.selected(),['public-b']);assert.match(v.notices.at(-1),/成功 1 个，失败 1 个/);
 v.api.dismissApprovedCandidate=original;v.click('一键删除选中');await v.settle();assert.deepEqual(v.calls,['public-a','public-b']);assert.equal(v.data.public.length,0);
});
test('batch progress disables conflicting actions; lane switch cannot expand or redirect in-flight IDs',async()=>{
 const v=mount(),hold=deferred();await v.settle();v.click('一键全选');await v.settle();const original=v.api.dismissApprovedCandidate;
 v.api.dismissApprovedCandidate=async id=>{await hold.promise;return original(id)};
 v.click('一键删除选中');await v.settle();assert.match(v.text(),/已处理 0 \/ 2/);
 assert.equal(v.button('正在删除').props.disabled,true);assert.equal(v.button('取消全选').props.disabled,true);
 assert.equal(v.all('CandidateRow').every(x=>x.props.selectionDisabled),true);assert.equal(v.button('启动自动').props.disabled,true);
 v.props.operation='follow';v.render();await v.settle();hold.resolve();await v.settle();
 assert.deepEqual(v.calls,['public-a','public-b']);assert.equal(v.data.private.length,2);assert.equal(v.data.public.length,0);assert.deepEqual(v.selected(),[]);
});
test('one-click select and checkbox both honor search results, and cancel clears visible selection',async()=>{
 const v=mount();await v.settle();const input=v.all('input').find(x=>x.props.placeholder==='输入名称或用户名');input.props.onChange({target:{value:'public-b'}});await v.settle();
 v.click('一键全选');await v.settle();assert.deepEqual(v.selected(),['public-b']);v.click('取消全选');await v.settle();assert.deepEqual(v.selected(),[]);
 const checkbox=v.all('input').find(x=>x.props.type==='checkbox');checkbox.props.onChange({target:{checked:true}});await v.settle();v.click('一键删除选中');await v.settle();
 assert.deepEqual(v.calls,['public-b']);assert.deepEqual(v.data.public.map(x=>x.id),['public-a']);
});
test('all selected accounts are attempted and progress is shown for a 500-account list',async()=>{
 const v=mount(),hold=deferred();v.data.public=Array.from({length:500},(_,i)=>candidate('account-'+i));v.render();await v.settle();v.click('一键全选');await v.settle();
 const original=v.api.dismissApprovedCandidate;v.api.dismissApprovedCandidate=async id=>{if(id==='account-250')await hold.promise;return original(id)};
 v.click('一键删除选中');await v.settle();assert.match(v.text(),/已处理 250 \/ 500/);hold.resolve();await v.settle();assert.equal(v.calls.length,500);assert.equal(v.data.public.length,0);
});

const countInput=v=>v.all('input').find(n=>n.props['aria-label']==='选择账号数量');
async function enterCount(v,value){countInput(v).props.onChange({target:{value}});await v.settle()}

for(const operation of ['greet','follow'])test(`${operation} quantity selection replaces the total with the first N displayed accounts and ignores new arrivals`,async()=>{
 const v=mount(operation),lane=operation==='greet'?'public':'private';
 v.data[lane]=Array.from({length:190},(_,i)=>candidate('account-'+i));v.render();await v.settle();
 v.click('一键全选');await v.settle();await enterCount(v,'50');v.click('按数量选择');await v.settle();
 assert.deepEqual(v.selected(),Array.from({length:50},(_,i)=>'account-'+i));
 await enterCount(v,'10');v.click('按数量选择');v.click('按数量选择');await v.settle();
 assert.deepEqual(v.selected(),Array.from({length:10},(_,i)=>'account-'+i));
 v.data[lane]=[candidate('newcomer'),...v.data[lane]];v.render();await v.settle();
 assert.equal(v.selected().length,10);assert.equal(v.selected().includes('newcomer'),false);assert.deepEqual(v.calls,[]);
});

test('quantity selection uses search order and clears previous selections hidden by the filter',async()=>{
 const v=mount();await v.settle();v.click('一键全选');await v.settle();
 const search=()=>v.all('input').find(n=>n.props.placeholder==='输入名称或用户名');
 search().props.onChange({target:{value:'public-b'}});await v.settle();
 await enterCount(v,'1');v.click('按数量选择');await v.settle();
 search().props.onChange({target:{value:''}});await v.settle();
 assert.deepEqual(v.selected(),['public-b']);
 v.click('一键删除选中');await v.settle();assert.deepEqual(v.calls,['public-b']);
 assert.deepEqual(v.data.public.map(n=>n.id),['public-a']);
});

test('invalid quantities do not alter selection; Enter selects, shortage reports actual count, and empty lists stay disabled',async()=>{
 const v=mount();await v.settle();await enterCount(v,'1');v.click('按数量选择');await v.settle();
 for(const value of ['', '0', '-1', '1.5', '1e2', 'NaN', '9007199254740992']){
  await enterCount(v,value);assert.equal(v.button('按数量选择').props.disabled,true);
  v.button('按数量选择').props.onClick();await v.settle();assert.deepEqual(v.selected(),['public-a']);
 }
 await enterCount(v,'2');let prevented=false;
 countInput(v).props.onKeyDown({key:'Enter',preventDefault(){prevented=true}});await v.settle();
 assert.equal(prevented,true);assert.deepEqual(v.selected(),['public-a','public-b']);
 await enterCount(v,'50');v.click('按数量选择');await v.settle();
 assert.match(v.text(),/当前列表仅 2 个，已全部选中/);assert.equal(v.selected().length,2);
 v.data.public=[];v.render();await v.settle();assert.equal(v.button('按数量选择').props.disabled,true);
 assert.deepEqual(v.selected(),[]);assert.doesNotMatch(v.text(),/当前列表仅/);
});

test('in-flight deletion blocks quantity changes from adding other accounts to the frozen batch',async()=>{
 const v=mount(),hold=deferred();await v.settle();await enterCount(v,'1');v.click('按数量选择');await v.settle();
 await enterCount(v,'2');const selectHandler=v.button('按数量选择').props.onClick;
 const original=v.api.dismissApprovedCandidate;v.api.dismissApprovedCandidate=async id=>{await hold.promise;return original(id)};
 v.click('一键删除选中');await v.settle();
 assert.equal(countInput(v).props.disabled,true);assert.equal(v.button('按数量选择').props.disabled,true);
 selectHandler();await v.settle();assert.deepEqual(v.selected(),['public-a']);
 hold.resolve();await v.settle();assert.deepEqual(v.calls,['public-a']);assert.deepEqual(v.data.public.map(n=>n.id),['public-b']);
});
