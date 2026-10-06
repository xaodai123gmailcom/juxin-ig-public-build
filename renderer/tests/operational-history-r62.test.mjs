import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import React from 'react';
import ts from 'typescript';
import {collectionOperationalHistoryRows, operationalHistoryGroups, operationalHistoryTypeLabel} from '../src/operational-history.ts';
import {historyPage, HISTORY_RENDER_PAGE_SIZE} from '../src/history-pagination.ts';
import {formatCount, formatTime} from '../src/workbench-format.ts';
import {resolveSuccessfulGreetingMessage} from '../src/greeting-messages.ts';

const copy = value => JSON.parse(JSON.stringify(value));
const task = (id, overrides = {}) => ({id, status:'completed', updated_at:'2026-10-03T10:00:00Z', ...overrides});
const campaign = (operation, id = operation, status = 'confirmed') => ({id, operation, profile_id:'w1', updated_at:'2026-10-03T11:00:00Z',
  targets:[{id:`target-${id}`, username:`person_${id}`, status, updated_at:'2026-10-03T11:00:00Z'}], message:operation === 'greet' ? 'Hello' : null});

test('collection history uses source/mode durable processed and qualified counts, never identified or task totals', () => {
  const input = [task('one', {modes:['followers','following'], counts:{processed:900, qualified:800}, windows:[{profile_id:'unrelated'}], targets:[
    {id:'first', username:'first_user', status:'completed', current_window_id:'w1', preferred_window_id:'wrong', mode_progress:{
      followers:{source_total:500, discovered:250, processed:200, saved:150, qualified_for_review:90},
      following:{source_total:50, discovered:35, processed:30, saved:20, qualified_for_review:10},
    }},
    {id:'second', username:'@second_user', status:'stopped', current_window_id:'w2', mode_progress:{followers:{processed:0,qualified_for_review:0}}},
  ]})];
  const before = copy(input), rows = collectionOperationalHistoryRows(input, new Map([['w1','窗口 3'],['w2','窗口 4']]));
  assert.equal(rows.length, 4); assert.equal(new Set(rows.map(row=>row.id)).size, 4);
  assert.deepEqual(rows.map(({target,window,mode,processed,qualified,status})=>({target,window,mode,processed,qualified,status})), [
    {target:'@first_user',window:'窗口 3',mode:'粉丝',processed:200,qualified:90,status:'completed'},
    {target:'@first_user',window:'窗口 3',mode:'关注',processed:30,qualified:10,status:'completed'},
    {target:'@second_user',window:'窗口 4',mode:'粉丝',processed:0,qualified:0,status:'stopped'},
    {target:'@second_user',window:'窗口 4',mode:'关注',processed:null,qualified:null,status:'stopped'},
  ]);
  assert.deepEqual(input, before);
});

test('legacy rows and retired modes remain visible; missing or invalid evidence never becomes zero or an assigned window', () => {
  const legacy = collectionOperationalHistoryRows([
    task('legacy', {target_account:'old_user', window_id:'legacy-window', task_status:'old-status'}),
    task('missing', {modes:['followers'], window_ids:['not-proof'], targets:[{id:'t',username:'old_target',preferred_window_id:'not-proof',mode_progress:{followers:{discovered:50,saved:20}}}]}),
    task('retired', {modes:['post_likers'], targets:[{id:'t',username:'retired_target',mode_progress:{post_likers:{processed:7,qualified_for_review:2}}}]}),
  ], new Map());
  assert.equal(legacy.length, 3);
  assert.deepEqual([legacy[0].type,legacy[0].mode,legacy[0].processed,legacy[0].qualified,legacy[0].status], ['collection','—',null,null,'old-status']);
  assert.equal(legacy[0].window,'legacy-window'); assert.equal(legacy[1].window,'—');
  assert.equal(legacy[1].processed,null); assert.equal(legacy[1].qualified,null);
  assert.deepEqual([legacy[2].mode,legacy[2].processed,legacy[2].qualified],['post_likers',7,2]);
  for(const invalid of [undefined,null,'7',-1,1.2,NaN,Infinity,true]) {
    const [row] = collectionOperationalHistoryRows([task('invalid',{mode_progress:{followers:{processed:invalid,qualified_for_review:invalid}}})], new Map());
    assert.equal(row.processed,null); assert.equal(row.qualified,null);
  }
});

test('type groups count the loaded rows, preserve within-type order and keep unknown operations separate', () => {
  const rows = Object.freeze([
    Object.freeze({id:'f2',type:'follow',status:'confirmed'}), Object.freeze({id:'c2',type:'collection',status:'stopped'}),
    Object.freeze({id:'g1',type:'greet',status:'confirmed'}), Object.freeze({id:'old',type:'legacy_action',status:'unknown'}),
    Object.freeze({id:'f1',type:'follow',status:'confirmed'}), Object.freeze({id:'c1',type:'collection',status:'completed'}),
  ]);
  const groups = operationalHistoryGroups(rows);
  assert.deepEqual(groups.map(group=>[group.id,group.rows.map(row=>row.id)]),[
    ['collection',['c2','c1']],['follow',['f2','f1']],['greet',['g1']],['other',['old']],
  ]);
  assert.equal(groups.reduce((sum,group)=>sum+group.rows.length,0),rows.length);
  assert.equal(groups[0].rows[0],rows[1]); assert.equal(operationalHistoryTypeLabel('legacy_action'),'其他任务');
  assert.deepEqual(operationalHistoryGroups([]).map(group=>group.rows.length),[0,0,0]);
});

test('status filters and type grouping intersect without changing statuses or restoring excluded rows', () => {
  const rows = ['collection','follow','greet','legacy'].flatMap(type=>['completed','stopped','running','unknown'].map(status=>({type,status,id:`${type}:${status}`})));
  const selectedStatuses = new Set(['completed','stopped']);
  for(const group of operationalHistoryGroups(rows.filter(row=>selectedStatuses.has(row.status)))) {
    const allGroup = operationalHistoryGroups(rows).find(item=>item.id===group.id);
    assert.deepEqual(group.rows,allGroup.rows.filter(row=>selectedStatuses.has(row.status)));
    assert.ok(group.rows.every(row=>selectedStatuses.has(row.status)));
  }
});

function nodes(node) {
  if(Array.isArray(node)) return node.flatMap(nodes);
  if(!node || typeof node!=='object') return [];
  return [node,...nodes(node.props?.children)];
}
function text(node) {
  if(Array.isArray(node)) return node.map(text).join('');
  if(node && typeof node==='object') return text(node.props?.children);
  return node === null || node === undefined || typeof node === 'boolean' ? '' : String(node);
}

// Run actual production rendering and event handlers, including its real
// immutable-action success predicate, without a browser or copied mock UI.
function mount(initialProps) {
  const source = readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
  const ast = ts.createSourceFile('history.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const names = new Set(['ImmutableOperationalHistory','useHistoryPage','HistoryPagination','successfulActionRows','asRecord','firstText','normalizeStatus']);
  const selected = ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.has(node.name?.text));
  assert.equal(selected.length,names.size);
  const code = ts.transpileModule(selected.map(node=>node.getText(ast)).join('\n'),{
    fileName:'history.tsx',compilerOptions:{jsx:ts.JsxEmit.React,target:ts.ScriptTarget.ES2022},
  }).outputText;
  const states=[]; let index=0,props=initialProps;
  const component = runInNewContext(code+'\nImmutableOperationalHistory;', {
    React,Map,Set,String,Array,formatCount,formatTime,historyPage,HISTORY_RENDER_PAGE_SIZE,
    collectionOperationalHistoryRows,operationalHistoryGroups,operationalHistoryTypeLabel,resolveSuccessfulGreetingMessage,
    ACTION_SUCCESS_STATES:new Set(['confirmed','already_done']),useMemo:fn=>fn(),
    useState(initial) {const slot=index++;if(!(slot in states))states[slot]=initial;return [states[slot],next=>{states[slot]=typeof next==='function'?next(states[slot]):next}]},
    FileClock:()=>null,Panel:({title,actions,children})=>React.createElement('section',{},React.createElement('h2',{},title),actions,children),
    StatusBadge:({status})=>React.createElement('span',{'data-status':status},status),
  });
  function resolve(node) {
    if(Array.isArray(node)) return node.map(resolve);
    if(!node || typeof node!=='object') return node;
    if(typeof node.type==='function') return resolve(node.type(node.props));
    return {...node,props:{...node.props,children:resolve(node.props?.children)}};
  }
  const view={
    render(next=props){props=next;index=0;view.tree=resolve(component(props));return view},
    all(type){return nodes(view.tree).filter(node=>node.type===type)},
    click(label){const button=view.all('button').find(node=>text(node).startsWith(label));assert.ok(button,label);assert.ok(!button.props.disabled);button.props.onClick();return view.render()},
    headers(){return view.all('th').map(text)},
    rows(){return view.all('tbody').flatMap(node=>nodes(node).filter(item=>item.type==='tr'))},
  };
  return view.render();
}

test('actual history switches type-specific columns, counts, statuses, windows and confirmed greeting text', () => {
  const props={tasks:[task('one',{targets:[{id:'source',username:'source_user',current_window_id:'w1',status:'stopped',mode_progress:{followers:{processed:12,qualified_for_review:3}}}]})],
    actions:[campaign('follow'),campaign('greet'),campaign('follow','failed','failed'),campaign('legacy')],windows:[{id:'w1',name:'窗口 3'}]};
  const before=copy(props),view=mount(props);
  assert.deepEqual(view.headers(),['完成时间','目标账号','窗口','粉丝 / 关注','采集完成数量','合格数量','状态']);
  assert.equal(view.rows().length,1); assert.match(text(view.rows()[0]),/@source_user窗口 3粉丝123stopped/);
  assert.match(text(view.tree),/采集任务 · 1/); assert.match(text(view.tree),/自动点关注 · 1/); assert.match(text(view.tree),/自动打招呼 · 1/);
  view.click('自动点关注');assert.deepEqual(view.headers(),['完成时间','账号','执行成功窗口','状态']);
  assert.match(text(view.rows()[0]),/@person_follow窗口 3confirmed/); assert.doesNotMatch(text(view.tree),/person_failed/);
  view.click('自动打招呼');assert.deepEqual(view.headers(),['完成时间','账号','执行成功窗口','实际话术','状态']);assert.match(text(view.rows()[0]),/Hello/);
  view.click('其他任务');assert.equal(view.rows().length,1);assert.match(text(view.rows()[0]),/其他任务@person_legacy/);
  assert.deepEqual(view.headers(),['完成时间','类型','账号','执行成功窗口','状态']);
  assert.equal(view.all('button').filter(node=>node.props['aria-pressed']===true).length,1);
  assert.deepEqual(props,before);
});

test('pagination is per category and resets on category change, preserving every loaded record', () => {
  const props={tasks:Array.from({length:230},(_,i)=>task(`c${i}`,{target_account:`source_${i}`})),
    actions:Array.from({length:140},(_,i)=>campaign('follow',String(i))),windows:[]};
  const view=mount(props),ids=[];
  assert.equal(view.rows().length,100);ids.push(...view.rows().map(text));
  view.click('下一页');assert.match(text(view.tree),/第 2 \/ 3 页/);ids.push(...view.rows().map(text));
  view.click('下一页');assert.equal(view.rows().length,30);ids.push(...view.rows().map(text));
  assert.equal(new Set(ids).size,230);
  view.click('自动点关注');assert.equal(view.rows().length,100);assert.match(text(view.tree),/第 1 \/ 2 页/);
  view.click('下一页');assert.equal(view.rows().length,40);
  view.click('采集任务');assert.match(text(view.tree),/第 1 \/ 3 页/);
  view.click('自动打招呼');assert.match(text(view.tree),/暂无自动打招呼历史/);assert.equal(view.all('td')[0].props.colSpan,5);
});

test('empty, action-only and truncated snapshots show honest category counts and missing-data notices', () => {
  const view=mount({tasks:[],actions:[],windows:[]});
  assert.match(text(view.tree),/暂无采集任务历史/);assert.equal(view.all('td')[0].props.colSpan,7);
  view.render({tasks:[],actions:[campaign('follow')],windows:[]});
  assert.deepEqual(view.headers(),['完成时间','账号','执行成功窗口','状态']);
  view.render({tasks:[task('old',{targets_truncated:true})],actions:[],windows:[]});
  assert.match(text(view.tree),/部分目标明细尚未载入/);assert.equal(view.rows().length,1);
  assert.deepEqual(view.all('td').slice(1,6).map(text),['—','—','—','—','—']);
});
