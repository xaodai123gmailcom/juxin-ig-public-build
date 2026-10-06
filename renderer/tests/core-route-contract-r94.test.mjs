import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url);
const {assertRouteState,assertSnapshotCadence}=require('../../desktop/tests/core-route-stability.integration.cjs');

// Render the production shell function with real React. Child workspaces are
// isolated here; their browser interactions remain in the full Electron gate.
const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
const parsed=ts.createSourceFile('workbench.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const declaration=parsed.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==='LiveFormalWorkbench');
assert.ok(declaration,'locate the production shell, not a duplicated test banner');
const compiled=ts.transpileModule(declaration.getText(parsed),{
 fileName:'workbench.tsx',compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022},reportDiagnostics:true,
});
assert.equal(compiled.diagnostics.filter(d=>d.category===ts.DiagnosticCategory.Error).length,0);
const Empty=()=>null;
const Account=()=>React.createElement('section',{'data-workspace':'accounts'});
const Review=()=>React.createElement('section',{'data-workspace':'review'});
const Body=()=>React.createElement('section',{'data-workspace':'collection'});
const View=runInNewContext(compiled.outputText+'\nLiveFormalWorkbench;',{
 React,useWorkbenchPlatform:()=>({platform:"instagram"}),PAGE_COPY:{accounts:{title:'账号管理'},review:{title:'目标审核'},collection:{title:'采集任务'}},
 truncatedSnapshotScopes:()=>[],formatCount:String,Rail:Empty,ChatGPTButton:Empty,GoogleTranslatorButton:Empty,
 Database:Empty,RefreshCw:Empty,LoaderCircle:Empty,AlertTriangle:Empty,StorageStatus:Empty,
 StableAccountWorkspace:Account,StableReviewWorkspace:Review,WorkbenchBody:Body,
});
const snapshot={source_revision:'stability-r94',connection:{connected:true,provider:'native'},dedupe:{total:1}};
const nodes=element=>!element||typeof element!=='object'?[]:Array.isArray(element)?element.flatMap(nodes):[element,...nodes(element.props?.children)];
const text=element=>element==null?'':typeof element!=='object'?String(element):Array.isArray(element)?element.map(text).join(''):text(element.props?.children);
function render(mode,{failed=false,loading=false,revision=1}={}){
 let retries=0;
 const core={snapshot:{...snapshot,dedupe:{total:revision}},error:failed?Error('Temporary Core failure'):null,
  loading,notice:null,run:()=>{},disabled:()=>false,refresh:async()=>{retries++},
  collectionControls:{},actionControls:{},reviewRun:()=>{},reviewDisabled:()=>false};
 const tree=View({mode,core}),all=nodes(tree),banner=all.find(n=>n.props?.className==='formal-error-banner');
 const retry=nodes(banner).find(n=>n.type==='button');
 const dedupe=all.find(n=>n.props?.className==='formal-chip'&&text(n).startsWith('IG 去重'));
 return {tree,html:renderToStaticMarkup(tree),retries:()=>retries,retry,core,all,
  state:{route:'#/'+mode,blocked:all.some(n=>n.props?.className==='formal-blocked'),dedupe:text(dedupe),
  banner:banner?text(nodes(banner).find(n=>n.type==='span')):'',retryEnabled:!!retry&&!retry.props.disabled}};
}
for(const mode of ['accounts','review','collection']){
 test(mode+': genuine snapshot error remains visible without replacing its workspace; retry is connected',async()=>{
  const view=render(mode,{failed:true});
  assertRouteState(view.state,{mode,failed:true});
  assert.match(view.html,new RegExp('data-workspace="'+mode+'"'));
  if(mode==='accounts'){
   assert.match(view.html,/总览暂未更新，账号窗口独立读取/);
   assert.equal(view.html.includes('本次快照更新失败'),false,'reproduce why the old cross-route assertion failed');
  }else assert.match(view.html,/Temporary Core failure/);
  await view.retry.props.onClick();assert.equal(view.retries(),1);
 });
}
test('pending refresh retains cached content; successful recovery removes warning and renders new data',()=>{
 for(const mode of ['accounts','review','collection']){
  assertRouteState(render(mode,{loading:true}).state,{mode});
  assertRouteState(render(mode,{revision:2}).state,{mode,revision:2});
 }
});
test('the release assertion still rejects missing warning, reconnect, wrong route, lost data and disabled retry',()=>{
 const good=render('accounts',{failed:true}).state;
 for(const bad of [{banner:''},{blocked:true},{route:'#/collection'},{dedupe:''},{retryEnabled:false}]){
  assert.throws(()=>assertRouteState({...good,...bad},{mode:'accounts',failed:true}),{name:'AssertionError'});
 }
 assert.throws(()=>assertRouteState(good,{mode:'accounts'}),/stale snapshot warning/);
});
test('ordinary five-second polls on a slow machine do not masquerade as route reconnects',()=>{
 for(const starts of [[100],[100,5100],[10,5010,10020,17000]])assertSnapshotCadence(starts);
});
test('initial read is required and extra immediate route-triggered snapshots still fail',()=>{
 for(const starts of [[],[100,120],[100,5100,5120]])assert.throws(()=>assertSnapshotCadence(starts),{name:'AssertionError'});
});
test('the real browser fixture compiles including both JavaScript and stylesheet',async()=>{
 const built=await require('esbuild').build({entryPoints:[fileURLToPath(new URL('./fixtures/core-route-stability.tsx',import.meta.url))],
  bundle:true,write:false,outfile:'route.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 assert.ok(built.outputFiles.some(f=>f.path.endsWith('.js')&&f.contents.length>0));
 assert.ok(built.outputFiles.some(f=>f.path.endsWith('.css')&&f.contents.length>0));
});

test('wolf is the accessible native refresh button and every route supplies the shared safe handler', async () => {
 const declaration=parsed.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==='Rail');
 const code=ts.transpileModule(declaration.getText(parsed).replace('export ',''),{compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022}}).outputText;
 const icons=Object.fromEntries([...declaration.getText(parsed).matchAll(/<(\w+) size=/g)].map(match=>[match[1],Empty]));
 const Rail=runInNewContext(code+'\nRail;',{React,UnreadBadge:Empty,...icons});
 let calls=0;const refresh=async()=>{calls++};
 for(const mode of ['accounts','collection','reports','home']){
  const tree=Rail({mode,refresh}),wolf=nodes(tree).find(n=>n.props?.className==='formal-brand');
  assert.equal(wolf.type,'button');assert.equal(wolf.props.type,'button');
  assert.equal(wolf.props['aria-label'],'刷新');assert.equal(wolf.props.title,'刷新');
  assert.equal(wolf.props.disabled,false);assert.equal(wolf.props['aria-busy'],false);
  await wolf.props.onClick();
  const busy=nodes(Rail({mode,refresh,refreshing:true})).find(n=>n.props?.className==='formal-brand');
  assert.equal(busy.props.disabled,true);assert.equal(busy.props['aria-busy'],true);
 }
 assert.equal(calls,4);
 assert.equal((source.match(/<Rail mode=\{mode\} refresh=\{core\.refresh\} refreshing=\{core\.loading \|\| core\.refreshing\} \/>/g)||[]).length,3,'home, reports and live workspace all use the same subscription');
});

test('requested shell badges and capacity controls are absent without losing dedupe or settings cleanup',()=>{
 for(const mode of ['accounts','review','collection']){
  const view=render(mode);
  assert.doesNotMatch(view.html,/界面 r94|Core r94|内置浏览器|刷新页面|部分列表显示最近|前往设置|仅清理预览缓存/);
  assert.match(view.html,/IG 去重 1/);
 }
 const settings=source.slice(source.indexOf('function SettingsWorkspace'),source.indexOf('function HomeWorkspace'));
 assert.match(settings,/仅清理预览缓存/);assert.match(settings,/client\.clearStorageCache\(\)/);
 assert.match(settings,/Core 修订版本/);
 assert.match(source,/limit: SNAPSHOT_PAGE_LIMIT,[\s\S]*historyLimit: SNAPSHOT_PAGE_LIMIT/);
});

test('manual refresh joins repeated clicks and preserves stale data/error guards without browser or command calls',async()=>{
 const {stripTypeScriptTypes}=await import('node:module');
 const {shareUnchangedJson}=await import('../src/snapshot-sharing.ts');
 const hook=source.slice(source.indexOf('function useFormalCore()'),source.indexOf('\ntype CollectionControls'));
 const effects=[],states=[],requests=[];let polling,resolve,reject,readCount=0;
 const client={lastCommandSnapshotSeq:0,liveStatus:()=>new Promise(()=>{})};
 const core=runInNewContext(stripTypeScriptTypes(hook)+'\nuseFormalCore();',{
  shareUnchangedJson,useState:initial=>{const slot={value:initial};states.push(slot);return [initial,value=>{slot.value=typeof value==='function'?value(slot.value):value}]},
  useRef:initial=>({current:initial}),useEffect:effect=>effects.push(effect),useCallback:fn=>fn,
  getCollectorCoreClient:()=>client,useWorkbenchPlatform:()=>({platform:'instagram'}),collectionPlatform:()=> 'instagram',
  startWorkbenchSnapshotPolling:options=>{polling=options;return {stop(){},refresh(){readCount++;return new Promise((yes,no)=>{resolve=yes;reject=no})}}},
  applyLiveStatusOverlay:snapshot=>snapshot,SNAPSHOT_PAGE_LIMIT:2000,
  document:{visibilityState:'visible',addEventListener(){},removeEventListener(){}},
  window:new Proxy({setInterval:()=>1,clearInterval(){},setTimeout:()=>2,clearTimeout(){}},{get(target,key){if(key in target)return target[key];throw Error('Unexpected browser access: '+String(key))}}),
  workbenchCommandFeedback:()=>({}),runWithSnapshotRefresh:()=>{throw Error('refresh must not execute mutations')},
 });
 const dispose=effects[0]();
 try{
  polling.onSnapshot({...snapshot,revision:8});
  const first=core.refresh();for(let i=0;i<25;i++)assert.equal(core.refresh(),first,'same-tick clicks join the identical promise');
  await Promise.resolve();assert.equal(readCount,1);assert.equal(states[0].value.revision,8);
  polling.onError(Error('stale fixture'));reject(Error('stale fixture'));await first;
  assert.equal(states[0].value.revision,8,'failed refresh retains accepted data');
  assert.match(states[1].value.message,/stale fixture/);
  assert.equal(await core.run('new-task',async()=>requests.push('unsafe')),false,'stale error still blocks mutations');
  const retry=core.refresh();await Promise.resolve();assert.equal(readCount,2);
  client.lastCommandSnapshotSeq=10;polling.onSnapshot({...snapshot,revision:9});
  assert.equal(states[0].value.revision,8,'late pre-command data cannot repaint');
  polling.onSnapshot({...snapshot,revision:10});resolve();await retry;
  assert.equal(states[0].value.revision,10);assert.equal(states[1].value,null);
  assert.deepEqual(requests,[]);
 }finally{dispose()}
});

test('native shell assertions reject missing wolf, restored badges, pending unlock and lost dedupe',()=>{
 const {assertCleanShell,shellStateScript}=require('../../desktop/tests/core-route-stability.integration.cjs');
 assert.doesNotThrow(()=>new Function('return '+shellStateScript),'native observation script must parse before the Windows gate');
 const good={wolfCount:1,title:'刷新',type:'button',imageLoaded:true,visible:true,badges:['IG 去重 1'],providerBadges:0,retiredCopy:false,topRefresh:false,busy:'false',disabled:false};
 assertCleanShell(good);
 for(const bad of [{wolfCount:0},{title:'平台'},{imageLoaded:false},{visible:false},{badges:[]},{badges:['界面 r94 / Core r94','IG 去重 1']},{providerBadges:1},{retiredCopy:true},{topRefresh:true}])
  assert.throws(()=>assertCleanShell({...good,...bad}),{name:'AssertionError'});
 assert.throws(()=>assertCleanShell(good,{busy:true}),{name:'AssertionError'});
});
