import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from 'typescript';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import * as coverage from '../src/split-review-report.ts';
import * as rows from '../src/collection-task-rows.ts';

test('cancelled whole-list repeat campaign has no renderer helper or metadata predicate', () => {
  assert.equal(coverage.collectionAutomaticRecheckText, undefined);
  for (const name of ['split-review-report.ts','collection-task-rows.ts','formal-workbench.tsx']) {
    const production=readFileSync(new URL('../src/'+name,import.meta.url),'utf8');
    assert.doesNotMatch(production,/automatic_recheck|collectionAutomaticRecheckText|collection-automatic-recheck/);
  }
});

test('explicit manual recheck still requires real end proof and is unaffected by retired campaign metadata', () => {
  for (const state of ['running','paused','recoverable','stopped','failed','error','completed']) {
    const target={id:'target',status:state,mode_coverage:{followers:{discovery_finished:true,unobserved_count:126,automatic_recheck:{state:'active'}}}};
    assert.deepEqual(rows.collectionSourceRecheckModes(target),['followers']);
    target.mode_coverage.followers.discovery_finished=false;
    assert.deepEqual(rows.collectionSourceRecheckModes(target),[]);
  }
});

const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
const ast=ts.createSourceFile('formal-workbench.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const selected=ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&['CollectionProgressValues','asRecord'].includes(node.name?.text));
const compiled=ts.transpileModule(selected.map(node=>node.getText(ast)).join('\n'),{compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022}}).outputText;
const Progress=runInNewContext(compiled+'\nCollectionProgressValues;',{React,...coverage,...rows});

test('actual progress ignores retired campaign hints while preserving every real counter', () => {
  const progress={followers:{source_total:537,discovered:411,processed:307,saved:213,skipped_global_duplicates:94}};
  const evidence={followers:{discovery_finished:false,unobserved_count:126,automatic_recheck:{state:'active',passes_started:4,passes_completed:3,pass_limit:5}}};
  const before=JSON.stringify({progress,evidence});
  const html=renderToStaticMarkup(React.createElement(Progress,{modes:['followers'],progress,coverage:evidence}));
  for(const expected of ['粉丝总数 537','已识别 411','去重 94','未识别 126','队列 104']) assert.ok(html.includes(expected),expected);
  assert.equal(JSON.stringify({progress,evidence}),before);
  assert.ok(!html.includes('数量已对齐'));
  assert.doesNotMatch(html,/自动复查|第 4\/5 轮|collection-automatic-recheck/);
});

test('completed sources stay hidden over repeated snapshots while a sibling retains its window and failure controls', () => {
  const legacy=Array.from({length:300},(_,i)=>({id:`legacy-${i}`,status:'completed',mode_coverage:{followers:{discovery_finished:true,unobserved_count:126}}}));
  const completed={id:'A',status:'completed',completion_policy:'automatic',current_window_id:'window'};
  const sibling={id:'B',status:'running',current_window_id:'window'};
  const task={id:'task',status:'running',targets:[...legacy,completed,sibling],windows:[{profile_id:'window'}],runtime:{profile_states:[{profile_id:'window',current_target_id:'B',state:'working'}]}};
  const before=JSON.stringify(task);
  for(let refresh=0;refresh<4;refresh++){
    const snapshot=JSON.parse(JSON.stringify(task));
    assert.deepEqual(snapshot.targets.filter(target=>rows.collectionCompletionCleanupProfile(snapshot,target)),[]);
    assert.deepEqual(rows.collectionTaskRows(snapshot).map(row=>row.target.id),['B']);
    assert.equal(snapshot.runtime.profile_states[0].current_target_id,'B');
  }
  assert.equal(JSON.stringify(task),before);
  sibling.status='failed';task.runtime.profile_states[0].state='manual_required';
  assert.deepEqual(rows.collectionTaskRows(task).map(row=>row.target.id),['B']);
  assert.deepEqual(task.targets.filter(target=>rows.collectionCompletionCleanupProfile(task,target)),[]);
  assert.ok(!source.includes('historicalGaps'));
  assert.match(source,/collectionCompletionCleanupRows\(sorted\)/);
});

const cardNames=['CompletedCollectionTargetCard','asRecord','firstText'];
const cardCompiled=ts.transpileModule(ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&cardNames.includes(node.name?.text)).map(node=>node.getText(ast)).join('\n'),{compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022}}).outputText;
const Icon=()=>null;
const cardContext={
  React,...rows,useState:React.useState,useRef:React.useRef,useEffect:React.useEffect,
  useWorkbenchPlatform:()=>({platform:'instagram'}),collectionPlatform:task=>task.settings?.platform||'instagram',
  CollectionProgressValues:Progress,StatusBadge:({status})=>React.createElement('span',null,status),
  CollectionSourceRecheck:()=>null,Check:Icon,Users:Icon,Trash2:Icon,RefreshCw:Icon,
};
const Card=runInNewContext(cardCompiled+'\nCompletedCollectionTargetCard;',cardContext);
const DirectCard=runInNewContext(cardCompiled+'\nCompletedCollectionTargetCard;', {...cardContext,
  useState:()=>[false,()=>{}],useRef:value=>({current:value}),useEffect:()=>{}});
test('actual automatic completion card keeps failed window-only cleanup actionable and never requests manual deletion', () => {
  const target={id:'target',username:'current',status:'completed',completion_policy:'automatic',current_window_id:'window',mode_progress:{},mode_coverage:{}};
  const props={target,task:{id:'task',modes:['followers'],runtime:{profile_states:[]}},disabled:()=>false,run:()=>Promise.resolve(true)};
  const html=renderToStaticMarkup(React.createElement(Card,props));
  assert.match(html,/等待窗口释放/);assert.match(html,/确认清理释放后自动移除/);assert.doesNotMatch(html,/删除任务卡|复查未发现/);
  props.task.runtime.profile_states=[{profile_id:'window',current_target_id:null,state:'manual_required',reason:'browser_close_failed'}];
  const failed=renderToStaticMarkup(React.createElement(Card,props));
  assert.match(failed,/窗口清理待处理/);assert.match(failed,/重试关闭/);assert.match(failed,/成功前保留此卡和窗口占用/);
  assert.doesNotMatch(failed,/删除任务卡/);
  props.task.runtime.profile_states[0].reason='source_recheck_cleanup_failed';
  assert.match(renderToStaticMarkup(React.createElement(Card,props)),/重试清理/);
  assert.match(source,/driverCleanupFailed \? "stop" : "resume"/);
  props.task.runtime.profile_states[0].current_target_id='different-target';
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(Card,props)),/重试关闭/);
});


test('actual cleanup recovery buttons dispatch exact owned-window stop/resume without data deletion', async () => {
  const calls=[];
  const props={target:{id:'target',status:'completed',completion_policy:'automatic',current_window_id:'window'},
    task:{id:'task',modes:[],runtime:{profile_states:[]}},disabled:()=>false,
    run:async(key,action)=>{await action({controlCollectionWindow:async(...args)=>calls.push(args)});return true;}};
  const nodes=value=>!value||typeof value!=='object'?[]:Array.isArray(value)?value.flatMap(nodes):[value,...nodes(value.props?.children)];
  for(const [reason,action] of [['source_recheck_cleanup_failed','stop'],['browser_close_failed','resume']]){
    props.task.runtime.profile_states=[{profile_id:'window',current_target_id:null,state:'manual_required',reason}];
    const button=nodes(DirectCard(props)).find(node=>node.type==='button');
    assert.ok(button);await button.props.onClick();
    assert.deepEqual(calls.at(-1),['task','window',action,'target']);
  }
});


test('one failed window yields one recovery card even after many normal sources completed there', () => {
  const task={id:'task',status:'completed',targets:Array.from({length:100},(_,i)=>({id:`done-${i}`,status:'completed',completion_policy:'automatic',current_window_id:'window'})),runtime:{profile_states:[{profile_id:'window',current_target_id:null,state:'manual_required',reason:'browser_close_failed'}]}};
  assert.equal(rows.collectionCompletionCleanupRows([task]).length,1);
  task.runtime.profile_states[0].state='closed';
  assert.deepEqual(rows.collectionCompletionCleanupRows([task]),[]);
});


test('R6 screenshot renders seven truthful counters with accessible unseen/queue distinction', () => {
  const progress={followers:{source_total:418,discovered:395,processed:365,saved:332,
    skipped_global_duplicates:33,discarded:201,hover_discarded:7,qualified_for_review:130}};
  const items=rows.collectionModeProgressItems('followers',progress.followers);
  assert.deepEqual(items.map(item=>item.text),['粉丝总数 418','已识别 395','未识别 23','去重 33','队列 30','丢弃 201','合格 130']);
  const before=JSON.stringify(progress);
  const html=renderToStaticMarkup(React.createElement(Progress,{modes:['followers'],progress}));
  assert.match(html,/aria-label="队列 30：[^"]*不计入未识别/);
  assert.match(html,/aria-label="丢弃 201：[^"]*已包含悬浮卡丢弃/);
  assert.doesNotMatch(html,/新增记录|入库丢弃|悬浮卡丢弃 7|丢弃 208/);
  assert.equal(JSON.stringify(progress),before);
  // The 1-record gap between processed and terminal categories is not smoothed.
  assert.equal(365-(33+201+130),1);
});

test('R6 changed headers and missing counters never cap real identities or invent completion', () => {
  const items=rows.collectionModeProgressItems('following',{source_total:10,discovered:15,processed:12,discarded:2});
  assert.ok(items.some(item=>item.text==='已识别 15'));
  assert.ok(items.some(item=>item.text==='未识别 0'));
  assert.ok(items.some(item=>item.text==='队列 3'));
  assert.ok(items.some(item=>item.key==='count-warning'));
  assert.ok(items.some(item=>item.text==='合格 —'));
  assert.ok(!items.some(item=>item.text.includes('数量已对齐')));
});


test('actual task counters render separate aligned labels and values in both mode rows', () => {
  const progress={followers:{source_total:456,discovered:419,processed:342,skipped_global_duplicates:19,discarded:249,qualified_for_review:74},following:{source_total:10,discovered:15,processed:12}};
  const html=renderToStaticMarkup(React.createElement(Progress,{modes:['followers','following'],progress,coverage:{followers:{discovery_finished:true,unobserved_count:37,pending_count:77,status:'pending'}}}));
  const labels=[...html.matchAll(/class="collection-progress-label">([^<]+)</g)].map(match=>match[1]);
  assert.deepEqual(labels, ['粉丝总数','已识别','未识别','去重','队列','丢弃','合格','关注总数','已识别','未识别','去重','队列','丢弃','合格']);
  assert.equal([...html.matchAll(/class="collection-progress-number"/g)].length,14);
  assert.match(html,/collection-coverage-note/);
  assert.match(html,/collection-progress-value is-count-warning/);
  assert.doesNotMatch(html,/is-remaining|剩余待采集/);
  const css=readFileSync(new URL('../src/formal-workbench.css',import.meta.url),'utf8');
  assert.match(css,/grid-template-columns: minmax\(0, 1.6fr\) repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css,/grid-template-rows: subgrid/);
  assert.match(css,/\.collection-mode-progress > \.collection-coverage-note \{ grid-column: 1 \/ -1/);
});
