import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {collectionSourceRecheckModes, collectionRecheckNeedsPause} from '../src/collection-task-rows.ts';
import {createCollectorCoreClient} from '../src/core-client.ts';
import {collectionRowFixture,elementNodes,elementText} from './collection-row-r43-fixture.mjs';

test('source recheck requires authoritative end evidence and a positive safe gap', () => {
  const make = (coverage) => ({id:'target',status:'completed',mode_coverage:{followers:coverage}});
  assert.deepEqual(collectionSourceRecheckModes(make({discovery_finished:true,unobserved_count:126})), ['followers']);
  for (const value of [undefined,null,0,-1,'94',1.2,Infinity])
    assert.deepEqual(collectionSourceRecheckModes(make({discovery_finished:true,unobserved_count:value})), []);
  assert.deepEqual(collectionSourceRecheckModes(make({discovery_finished:false,unobserved_count:126})), []);
  assert.deepEqual(collectionSourceRecheckModes({id:'target',status:'running',mode_progress:{followers:{source_total:537,processed:307}}}), []);
  assert.deepEqual(collectionSourceRecheckModes({...make({discovery_finished:true,unobserved_count:126}),current_stage:'deleted_archived'}), []);
});

test('running source recheck is independent; recovery and unknown states stay blocked', () => {
  for (const state of ['starting','waiting_network','manual_required','unknown',''])
    assert.equal(collectionRecheckNeedsPause(state), true, state);
  for (const state of ['running','working','collecting','processing','active','completed','paused','stopped','recoverable','failed'])
    assert.equal(collectionRecheckNeedsPause(state), false, state);
});

test('explicit recheck uses exact source identity and participates in command revision fences', async () => {
  const requests=[];
  const client=createCollectorCoreClient({request:async(path,options)=>{
    requests.push({path,options});
    return {command:'task_source_recheck',snapshot_seq:41,result:{status:'paused',waiting_for_task_resume:true}};
  },secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,configureIntegrations:async()=>({})});
  const result=await client.recheckCollectionSource('task-one','target-two','followers');
  assert.equal(result.waiting_for_task_resume,true);
  assert.equal(client.lastCommandSnapshotSeq,41);
  assert.deepEqual(requests,[{path:'/api/workbench/commands',options:{method:'POST',body:{type:'task_source_recheck',payload:{task_id:'task-one',target_id:'target-two',mode:'followers'}}}}]);
});

test('historical completed-gap cards never rehydrate or automatically rescan', () => {
  const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
  assert.match(source,/collectionCompletionCleanupRows\(sorted\)/);
  assert.doesNotMatch(source,/historicalGaps|historicalGapLimit/);
  assert.match(source,/disabled=\{blocked \|\| disabled\(actionKey\)\}/);
  assert.doesNotMatch(source,/先暂停，再复查未发现部分/);
  assert.match(source,/差额：/);
  assert.match(source,/collectionModeUnseenCount\(asRecord\(target.mode_progress\?\.\[mode\]\)\)/);
  assert.match(source,/onClick=\{\(\) => void run\(actionKey, client => client\.recheckCollectionSource\(task\.id, target\.id, mode\)/);
});

test('failed recheck cleanup exposes Stop-based retry and never an unsafe Continue', async () => {
 for (const status of ['paused','recoverable','stopped','failed']) {
  const fixture=collectionRowFixture({profile:{state:'manual_required',reason:'source_recheck_cleanup_failed'},
    target:{status}});
  const buttons=elementNodes(fixture.render()).filter(node=>node.type==='button');
  const retry=buttons.find(button=>elementText(button)==='重试清理');
  assert.ok(retry,'retained failed cleanup has a usable recovery control');
  assert.equal(retry.props.disabled,false);
  assert.equal(buttons.some(button=>elementText(button)==='继续'),false);
  await retry.props.onClick();
  assert.equal(fixture.runs.at(-1).requests[0].action,'stop');
  assert.equal(fixture.runs.at(-1).requests[0].targetId,'target-9');
 }
});


test('real React recheck fixture compiles and is required by the Windows browser gate', async () => {
  const {build}=await import('esbuild');
  const built=await build({entryPoints:[fileURLToPath(new URL('./fixtures/source-recheck.tsx',import.meta.url))],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
  assert.ok(built.outputFiles.some(file=>file.path.endsWith('.js')&&file.contents.length>1000));
  assert.ok(built.outputFiles.some(file=>file.path.endsWith('.css')));
  const gate=readFileSync(new URL('../../desktop/tests/embedded-browser.integration.cjs',import.meta.url),'utf8');
  assert.match(gate,/watchdog\.begin\('source-recheck',90000\);await require\('\.\/source-recheck\.integration\.cjs'\)\(\{win,host\}\)/);
});

// Windows drive letters and escaped spaces/non-ASCII characters are decoded by
// Node's native URL conversion; URL.pathname is not a filesystem path.
test('fixture URL conversion preserves Windows drive and Unicode paths', () => {
  assert.equal(fileURLToPath(new URL('file:///C:/fixture%20dir/%E8%81%9A%E9%91%AB.tsx'), {windows:true}), 'C:\\fixture dir\\聚鑫.tsx');
});

test('actual recheck control renders the source gap independently of child queue', async () => {
  const React=(await import('react')).default;
  const {renderToStaticMarkup}=await import('react-dom/server');
  const ts=(await import('typescript')).default;
  const {runInNewContext}=await import('node:vm');
  const rows=await import('../src/collection-task-rows.ts');
  const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
  const ast=ts.createSourceFile('source.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const selected=ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&['CollectionSourceRecheck','asRecord'].includes(n.name?.text));
  const compiled=ts.transpileModule(selected.map(n=>n.getText(ast)).join('\n'),{compilerOptions:{jsx:ts.JsxEmit.React}}).outputText;
  const Control=runInNewContext(compiled+'\nCollectionSourceRecheck',{React,...rows,collectionPlatform:()=> 'instagram',RefreshCw:()=>null});
  const target={id:'t',status:'running',mode_coverage:{followers:{discovery_finished:true,unobserved_count:10}},
    mode_progress:{followers:{source_total:216,discovered:206,processed:160}}};
  const props={task:{id:'task'},target,state:'running',actionKey:'recheck',run:()=>{},disabled:()=>false};
  const html=renderToStaticMarkup(React.createElement(Control,props));
  assert.match(html,/差额：10/);
  assert.doesNotMatch(html,/disabled|先暂停|差额：56|差额：46/);
  assert.match(html,/父页独立复查可见名单，子页继续采集/);
  target.mode_progress.followers.source_total=null;
  assert.match(renderToStaticMarkup(React.createElement(Control,props)),/差额：未知/);
  assert.match(renderToStaticMarkup(React.createElement(Control,{...props,state:'manual_required'})),/disabled/);
  assert.match(renderToStaticMarkup(React.createElement(Control,{...props,disabled:()=>true})),/disabled/);
  // The live runtime identifies the owned source, even with zero/unknown gap
  // or a still-running scanner. Historical rows retain their old evidence gate.
  props.task.runtime={profile_states:[{current_target_id:'t',source_recheck_mode:'followers'}]};
  target.mode_coverage.followers={discovery_finished:false,unobserved_count:0};
  target.mode_progress.followers.source_total=206;
  const live=renderToStaticMarkup(React.createElement(Control,props));
  assert.match(live,/复查未发现（粉丝）/);
  assert.doesNotMatch(live,/disabled/);
  target.source_recheck={mode:'followers',state:'prepared',requested_at:'same-generation'};
  const pending=renderToStaticMarkup(React.createElement(Control,props));
  assert.match(pending,/等待父页完成当前操作后复查/);
  assert.match(pending,/disabled/);
  assert.match(pending,/子页继续采集/);
  assert.doesNotMatch(pending,/请稍后再试|Error invoking|先暂停/);
  target.source_recheck=null;
  const requests=[];
  let message='';
  props.run=async(key,action,format)=>{
    const response=await action({recheckCollectionSource:async(...args)=>{
      requests.push(args);return {parent_only:true,waiting_for_safe_point:true};
    }});
    message=format(response);
  };
  const nodes=value=>!value||typeof value!=='object'?[]:Array.isArray(value)?value.flatMap(nodes):[value,...nodes(value.props?.children)];
  nodes(Control(props)).find(node=>node.type==='button').props.onClick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(requests,[['task','t','followers']]);
  assert.match(message,/请求已保存，等待父页完成当前操作后复查；子页继续采集/);

});
