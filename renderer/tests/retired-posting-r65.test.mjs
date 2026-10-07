import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync,readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from 'typescript';
import {build} from 'esbuild';

const source=name=>readFileSync(new URL('../src/'+name,import.meta.url),'utf8');
test('retired posting bookmarks resolve safely and retained routes stay available',()=>{
 const code=ts.transpileModule(source('App.tsx'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const exports={};runInNewContext(code,{exports,require:()=>({})});
 for(const hash of ['#/posting','#/posting/'])assert.equal(exports.modeFromHash(hash),'home');
 for(const route of ['accounts','collection','nurture','review','reports','history'])assert.equal(exports.modeFromHash('#/'+route),route);
 assert.equal(exports.modeFromHash('#/data'),'reports');
});
test('navigation, reports and integration types contain no retired product capability',()=>{
 const workbench=source('formal-workbench.tsx');
 assert.doesNotMatch(workbench,/PostingWorkspace|mode: "posting"|mode === "posting"/);
 for(const file of ['reports-workspace.tsx','report-data-overview.tsx','core-client.ts','studio-workspace.tsx'])assert.doesNotMatch(source(file),/pexels|posting|已确认发帖|发帖数量|创建发帖任务/i,file);
 for(const file of ['posting-workspace.tsx','posting-workspace.css','studio-search.ts','studio-workspace.css'])assert.equal(existsSync(new URL('../src/'+file,import.meta.url)),false,file);
 assert.match(source('standalone-nurture-workspace.tsx'),/account\?\.posts_count/,'account metadata remains available');
});
test('retired material hosts are absent from the browser content policy',()=>{
 assert.doesNotMatch(readFileSync(new URL('../index.html',import.meta.url),'utf8'),/pexels/i);
});

test('production App bundle cannot include the deleted publisher or material search',async()=>{
 const bundle=await build({entryPoints:['renderer/src/App.tsx'],bundle:true,write:false,outfile:'app.js',metafile:true,jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 const inputs=Object.keys(bundle.metafile.inputs).join('\n');
 assert.doesNotMatch(inputs,/posting-workspace|studio-search|pexels-integration/);
 for(const module of ['standalone-nurture-workspace','reports-workspace','account-workspace','nurture-collection-blocker'])assert.ok(inputs.includes(module),module);
 const js=bundle.outputFiles.find(file=>file.path.endsWith('.js')).text;
 assert.doesNotMatch(js,/\/api\/posting\/|\/api\/internal\/integrations\/pexels|pexelsApiKey|创建发帖任务|发帖数量|素材与文案/);
});

test('closed-window reconciliation requires explicit eligibility and a fresh closed observation', async()=>{
 const {accountWindowReconciliationEligible:eligible,accountWindowReconciliationReady:ready}=await import('../src/account-window-reconciliation.ts');
 const lock={operation_type:'account',state:'occupied',entity_id:null,can_reconcile_window_state:true};
 assert.equal(eligible(lock),true);assert.equal(ready(lock,{opened:false,window_state:'closed'}),true);
 for(const patch of [{can_reconcile_window_state:false},{can_reconcile_window_state:undefined},{operation_type:'nurture'},{state:'running'},{entity_id:'live-task'},{entity_id:undefined}])assert.equal(eligible({...lock,...patch}),false);
 for(const window of [undefined,{}, {opened:true}, {opened:false,window_state:'unknown'}])assert.equal(ready(lock,window),false);
 assert.equal(ready(lock,{opened:false},true),false);
 const workspace=source('account-workspace.tsx');
 assert.match(workspace,/result\.reconciled!==true/);assert.match(workspace,/action:'reconcile_window_state',id:selected\.id/);
 const handler=workspace.slice(workspace.indexOf('  async function reconcileWindowState()'),workspace.indexOf('  const disabled='));
 assert.match(handler,/pending\.current/);assert.match(handler,/windowOperations\.current\.has/);
 assert.doesNotMatch(handler,/action:'close'|action:'start'|delete|unlock/);
});

test('actual reconciliation command collapses duplicate clicks, preserves locks and rejects unverified replies',async()=>{
 const {stripTypeScriptTypes}=await import('node:module');
 const workspace=source('account-workspace.tsx');
 const section=workspace.slice(workspace.indexOf('  async function command('),workspace.indexOf('  // Both feeds poll independently.'));
 const code=stripTypeScriptTypes(section);
 for(const outcome of [{reconciled:true},{reconciled:false},new Error('窗口仍在运行')]){
  let release;const deferred=new Promise(resolve=>release=resolve),calls=[],notes=[],errors=[],busy=[],refreshes=[];
  const pending={current:false},live={current:true},windowOperations={current:new Map()};
  const context={pending,live,windowOperations,getCollectorCoreClient:()=>({accountCommand:async body=>{calls.push(body);await deferred;if(outcome instanceof Error)throw outcome;return outcome}}),setBusy:value=>busy.push(value),setError:value=>errors.push(value),setNote:value=>notes.push(value),refresh:()=>refreshes.push('account'),unread:{refresh:()=>refreshes.push('unread')},onChanged:()=>refreshes.push('windows')};
  const command=new Function(...Object.keys(context),code+';return command;')(...Object.values(context));
  const body={action:'reconcile_window_state',id:'account-one'},first=command(body,'窗口已核验关闭');
  assert.equal(await command(body,'duplicate'),false);assert.equal(calls.length,1);assert.equal(pending.current,true);
  release();const result=await first;
  assert.deepEqual(calls,[body]);assert.equal(windowOperations.current.size,0);
  assert.deepEqual(busy,[true,false]);assert.equal(pending.current,false);assert.deepEqual(refreshes,['account','unread','windows']);
  if(outcome.reconciled===true){assert.equal(result,true);assert.equal(notes.at(-1),'窗口已核验关闭')}
  else {assert.equal(result,false);assert.ok(errors.at(-1));assert.ok(!notes.includes('窗口已核验关闭'))}
 }
});
