import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const source=ts.transpileModule(readFileSync(new URL('../src/review-account-settings.tsx',import.meta.url),'utf8'),{
 compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},
}).outputText;
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve()};
const status=(extra={})=>({opened:true,lastTarget:'saved_review_target',message:'',httpStatus:200,resetting:false,resetRequired:false,...extra});
const textOf=node=>node==null||typeof node==='boolean'?'':typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(textOf).join(''):textOf(node.props?.children);

// Execute the complete production TSX, including button handlers and disabled
// states. The harness supplies deterministic hook commits and timer delivery;
// it does not replace the review commands or emulate native Electron cleanup.
function mount(api){
 const states=[],refs=[],effects=[],writes=[],listeners=new Map(),timers=new Map();
 let stateIndex=0,refIndex=0,effectIndex=0,timerSerial=0,dirty=false,mounted=true;
 const sameDeps=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const hooks={
  useState(initial){const slot=stateIndex++;if(!(slot in states))states[slot]=typeof initial==='function'?initial():initial;return [states[slot],next=>{const value=typeof next==='function'?next(states[slot]):next;writes.push({slot,value});if(!Object.is(states[slot],value)){states[slot]=value;dirty=true}}]},
  useRef(initial){const slot=refIndex++;return refs[slot]??={current:initial}},
  useEffect(effect,deps){const slot=effectIndex++;if(!sameDeps(effects[slot]?.deps,deps))effects[slot]={effect,deps,cleanup:effects[slot]?.cleanup,pending:true}},
 };
 const window={
  collectorCore:api?{reviewAccount:api}:undefined,
  addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn)},
  removeEventListener(name,fn){listeners.get(name)?.delete(fn)},
  setInterval(callback,delay){timers.set(++timerSerial,{callback,delay});return timerSerial},
  clearInterval(id){timers.delete(id)},
 };
 const module={exports:{}};
 const jsx=(type,props)=>({type,props:props??{}});
 runInNewContext(source,{module,exports:module.exports,window,Error,require(name){
  if(name==='react')return hooks;
  if(name==='react/jsx-runtime')return {jsx,jsxs:jsx,Fragment:'fragment'};
  if(name==='lucide-react')return Object.fromEntries(['LogIn','RefreshCw','Users','X','Trash2'].map(name=>[name,name]));
  throw new Error(`Unexpected production import: ${name}`);
 }});
 const view={window,writes,timers,listeners,tree:null,
  render(){assert.ok(mounted);stateIndex=refIndex=effectIndex=0;dirty=false;view.tree=module.exports.ReviewAccountSettings();for(const item of effects)if(item.pending){item.pending=false;item.cleanup?.();item.cleanup=item.effect()}return view},
  async settle(){for(let i=0;i<20;i++){await flush();if(!dirty||!mounted)return view;view.render()}throw new Error('Hook commits did not settle')},
  all(type){const found=[];const visit=node=>{if(Array.isArray(node)){node.forEach(visit);return}if(!node||typeof node!=='object')return;if(node.type===type)found.push(node);visit(node.props?.children)};visit(view.tree);return found},
  button(label){const found=view.all('button').filter(node=>textOf(node).includes(label));assert.equal(found.length,1,`Expected one button: ${label}`);return found[0]},
  click(label){const button=view.button(label);assert.equal(Boolean(button.props.disabled),false,`Button must be usable: ${label}`);return button.props.onClick()},
  focus(){for(const callback of listeners.get('focus')??[])callback()},
  tick(){for(const {callback} of [...timers.values()])callback()},
  text(){return textOf(view.tree)},
  dispose(){if(!mounted)return;mounted=false;effects.forEach(item=>item.cleanup?.())},
 };
 return view.render();
}

test('review reset button dispatches only the reset action and keeps the saved target available',async()=>{
 const calls=[];
 const view=mount(async input=>{calls.push(JSON.parse(JSON.stringify(input)));return status()});
 try{
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  assert.deepEqual(calls,[{action:'status'},{action:'reset'}]);
  assert.equal(view.button('返回目标主页').props.disabled,false);
  assert.match(textOf(view.button('返回目标主页')),/@saved_review_target/);
  assert.equal(view.button('收起审核窗口').props.disabled,false);
 }finally{view.dispose()}
});

test('rapid repeated reset clicks submit one native command and block navigation until it settles',async()=>{
 const ack=deferred(),calls=[];
 const view=mount(input=>{calls.push(input.action);return input.action==='status'?Promise.resolve(status()):ack.promise});
 try{
  await view.settle();const handler=view.button('一键清空并重新登录').props.onClick;
  handler();handler();handler();await view.settle();
  assert.deepEqual(calls,['status','reset']);
  assert.ok(view.all('button').every(button=>button.props.disabled));
  assert.match(view.text(),/正在清空审核登录状态/);
  view.focus();assert.deepEqual(calls,['status','reset']);
  ack.resolve(status({lastTarget:'preserved_target'}));await view.settle();
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  assert.match(textOf(view.button('返回目标主页')),/@preserved_target/);
 }finally{view.dispose()}
});

test('a stale pre-reset status or status error cannot overwrite the reset result',async()=>{
 for(const fail of [false,true]){
  const old=deferred(),ack=deferred();
  const view=mount(input=>input.action==='status'?old.promise:ack.promise);
  try{
   view.click('一键清空并重新登录');await view.settle();
   ack.resolve(status({lastTarget:'current_target'}));await view.settle();
   if(fail)old.reject(new Error('stale status failure'));else old.resolve(status({lastTarget:'old_target',resetRequired:true}));
   await view.settle();
   assert.match(textOf(view.button('返回目标主页')),/@current_target/);
   assert.equal(view.button('登录 / 检查').props.disabled,false);
   assert.doesNotMatch(view.text(),/stale status failure|状态读取失败/);
  }finally{view.dispose()}
 }
});

test('cleanup failure stays visible and permits a fresh reset attempt',async()=>{
 let resets=0;
 const view=mount(async input=>{
  if(input.action==='status')return status();
  if(++resets===1)throw new Error('审核会话清理失败，请重新清空');
  return status({lastTarget:'saved_review_target'});
 });
 try{
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  assert.match(view.text(),/审核会话清理失败，请重新清空/);
  assert.equal(view.all('p').filter(node=>node.props.role==='alert').length,1);
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  view.click('一键清空并重新登录');await view.settle();
  assert.equal(resets,2);assert.doesNotMatch(view.text(),/审核会话清理失败/);
  assert.equal(view.button('返回目标主页').props.disabled,false);
 }finally{view.dispose()}
});

test('a required cleanup blocks review navigation while the reset retry remains available',async()=>{
 const view=mount(async()=>status({opened:false,resetRequired:true,message:'上次清理尚未成功，请重新清空'}));
 try{
  await view.settle();
  for(const label of ['登录 / 检查','账号主页 / 切换','返回目标主页','收起审核窗口'])assert.equal(view.button(label).props.disabled,true);
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  assert.match(view.text(),/上次清理尚未成功/);
 }finally{view.dispose()}
});

test('failed reset refreshes the native cleanup lock without hiding the original error',async()=>{
 let reads=0;
 const view=mount(async input=>{
  if(input.action==='reset')throw new Error('审核会话缓存清理失败');
  reads++;return status({opened:reads===1,resetRequired:reads>1});
 });
 try{
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  assert.equal(reads,2,'Failure must refresh the authoritative native cleanup state');
  assert.equal(view.button('登录 / 检查').props.disabled,true);
  assert.equal(view.button('返回目标主页').props.disabled,true);
  assert.equal(view.button('收起审核窗口').props.disabled,true);
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  assert.match(view.text(),/审核会话缓存清理失败/);
 }finally{view.dispose()}
});

test('a failed status refresh after reset failure preserves its original reason and allows retry',async()=>{
 let reads=0;
 const view=mount(async input=>{
  if(input.action==='reset')throw new Error('具体清理错误');
  if(++reads===1)return status();
  throw new Error('次要状态查询错误');
 });
 try{
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  assert.match(view.text(),/具体清理错误/);assert.doesNotMatch(view.text(),/次要状态查询错误/);
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
 }finally{view.dispose()}
});

test('reset started in the native review menu is polled until it completes',async()=>{
 let current=status({resetting:true}),reads=0;
 const view=mount(async input=>{assert.equal(input.action,'status');reads++;return current});
 try{
  await view.settle();assert.ok(view.all('button').every(button=>button.props.disabled));
  assert.equal(view.timers.size,1);assert.equal([...view.timers.values()][0].delay,1000);
  const before=reads;view.tick();await view.settle();assert.equal(reads,before+1);
  current=status();view.tick();await view.settle();
  assert.equal(view.timers.size,0);assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  assert.equal(view.button('返回目标主页').props.disabled,false);
 }finally{view.dispose()}
});

test('slow native-reset status reads never overlap or starve completion on repeated timer ticks',async()=>{
 const waiting=deferred();let reads=0;
 const view=mount(()=>{reads++;return reads===1?Promise.resolve(status({resetting:true})):waiting.promise});
 try{
  await view.settle();assert.equal(view.timers.size,1);
  for(let tick=0;tick<20;tick++)view.tick();
  assert.equal(reads,2,'Only the pending status read may own the timer refresh');
  waiting.resolve(status());await view.settle();
  assert.equal(view.timers.size,0);assert.equal(view.button('一键清空并重新登录').props.disabled,false);
 }finally{view.dispose()}
});

test('a transient native-reset status failure does not stop polling or enable conflicting commands',async()=>{
 let mode='running';
 const view=mount(async()=>{
  if(mode==='error')throw new Error('暂时无法读取');
  return status({resetting:mode==='running'});
 });
 try{
  await view.settle();mode='error';view.tick();await view.settle();
  assert.equal(view.timers.size,1);
  assert.ok(view.all('button').every(button=>button.props.disabled));
  assert.match(view.text(),/审核账号状态读取失败/);
  mode='done';view.tick();await view.settle();
  assert.equal(view.timers.size,0);assert.equal(view.button('一键清空并重新登录').props.disabled,false);
 }finally{view.dispose()}
});

test('cancelled native confirmation restores controls without clearing the target',async()=>{
 const initial=status({message:'原网页安全验证提示'});
 const view=mount(async()=>initial);
 try{
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  assert.equal(view.button('一键清空并重新登录').props.disabled,false);
  assert.match(textOf(view.button('返回目标主页')),/@saved_review_target/);
  assert.match(view.text(),/原网页安全验证提示/);
  assert.equal(view.all('p').filter(node=>node.props.role==='alert').length,0);
 }finally{view.dispose()}
});

test('unmount suppresses late reset success or failure and releases focus/timer listeners',async()=>{
 for(const fail of [false,true]){
  const ack=deferred();
  const view=mount(input=>input.action==='status'?Promise.resolve(status()):ack.promise);
  await view.settle();view.click('一键清空并重新登录');await view.settle();
  view.dispose();const writes=view.writes.length;
  if(fail)ack.reject(new Error('late reset failure'));else ack.resolve(status());
  await flush();assert.equal(view.writes.length,writes);
  assert.equal(view.listeners.get('focus')?.size??0,0);assert.equal(view.timers.size,0);
 }
});

test('unmount suppresses pending status success and failure',async()=>{
 for(const fail of [false,true]){
  const response=deferred(),view=mount(()=>response.promise);
  view.dispose();const writes=view.writes.length;
  if(fail)response.reject(new Error('late status failure'));else response.resolve(status({resetting:true}));
  await flush();assert.equal(view.writes.length,writes);assert.equal(view.timers.size,0);
 }
});

test('missing desktop bridge shows a usable explanation and never enables reset',async()=>{
 const view=mount(undefined);
 try{
  await view.settle();assert.ok(view.all('button').every(button=>button.props.disabled));
  assert.match(view.text(),/请使用新版桌面程序管理审核账号/);
 }finally{view.dispose()}
});
