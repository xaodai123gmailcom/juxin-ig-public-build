import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import {collectionPlatform} from '../src/collection-platform.ts';
import ts from 'typescript';
import { REVIEW_PAGE_SIZE, createReviewQueueReader, reviewQueueKey, selectedReviewIds } from '../src/review-queue-state.ts';

const workbench = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const start = workbench.indexOf('function ReviewWorkspace('), end = workbench.indexOf('\nconst StableReviewWorkspace', start);
const source = ts.transpileModule(`${workbench.slice(start, end)}\nexports.ReviewWorkspace = ReviewWorkspace;`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };
const textOf = (node) => node == null || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node) : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.props?.children);
const candidate = (id, visibility = 'public', stage = 1) => ({ id, username: id, queue: visibility, visibility, review_stage: stage, status: 'pending', profile: { posts: 10, followers: 100, following: 50 }, created_at: '2026-09-23T00:00:00Z' });

function coreFixture(items) {
  const calls = { queries: [], moves: [], decisions: [] }, data = items;
  const counts = () => Object.fromEntries(['public', 'private'].map(v => [v, { stage1: data.filter(x => x.status === 'pending' && x.visibility === v && x.review_stage === 1).length, stage2: data.filter(x => x.status === 'pending' && x.visibility === v && x.review_stage === 2).length }]));
  const api = {
    async reviewQueue(query) {
      calls.queries.push({ ...query });
      const matching = data.filter(x => x.status === 'pending' && x.visibility === query.visibility && x.review_stage === query.review_stage);
      return { items: matching.slice(query.offset, query.offset + query.limit).map(x => ({ ...x })), total: matching.length, offset: query.offset, limit: query.limit, has_more: matching.length > query.offset + query.limit, counts: counts(), snapshot_seq: 1 };
    },
    async moveReviewStage(payload) {
      calls.moves.push(JSON.parse(JSON.stringify(payload)));
      const moved_ids = [], skipped_ids = [];
      for (const id of payload.candidate_ids) {
        const item = data.find(x => x.id === id && x.status === 'pending' && x.visibility === payload.visibility && x.review_stage === payload.from_stage);
        if (item) { item.review_stage = payload.to_stage; moved_ids.push(id); } else skipped_ids.push(id);
      }
      return { moved_ids, skipped_ids, moved_count: moved_ids.length, snapshot_seq: 2 };
    },
    async decideReview(payload) {
      calls.decisions.push({ ...payload });
      const item = data.find(x => x.id === payload.candidate_id);
      if (item?.fail) throw new Error('暂时无法保存');
      item.status = payload.decision;
      return item;
    },
  };
  return { api, data, calls };
}

// Execute the actual production workspace and all its handlers. Deterministic
// hook commits and explicit timer ticks make concurrency assertions reproducible.
function mount(fixture, snapshotOverride = undefined) {
  const states = [], refs = [], effects = [], memos = [], timers = new Map(), notices = [];
  let stateIndex = 0, refIndex = 0, effectIndex = 0, memoIndex = 0, serial = 0, dirty = false, mounted = true;
  const sameDeps = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(initial) { const slot = stateIndex++; if (!(slot in states)) states[slot] = typeof initial === 'function' ? initial() : initial; return [states[slot], next => { if (!mounted) return; const value = typeof next === 'function' ? next(states[slot]) : next; if (!Object.is(value, states[slot])) { states[slot] = value; dirty = true; } }]; },
    useRef(initial) { const slot = refIndex++; return refs[slot] ??= { current: initial }; },
    useEffect(effect, deps) { const slot = effectIndex++; if (!sameDeps(effects[slot]?.deps, deps)) effects[slot] = { effect, deps, cleanup: effects[slot]?.cleanup, pending: true }; },
    useMemo(factory, deps) { const slot = memoIndex++; if (!sameDeps(memos[slot]?.deps, deps)) memos[slot] = { deps, value: factory() }; return memos[slot].value; },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
  };
  const window = { setInterval(fn) { timers.set(++serial, fn); return serial; }, clearInterval(id) { timers.delete(id); } };
  const exports = {}, jsx = (type, props) => ({ type, props: props ?? {} });
  const icons = ['Eye', 'LockKeyhole', 'Clock3', 'Database', 'RefreshCw', 'ShieldCheck', 'ListFilter', 'Inbox', 'ChevronDown', 'ArrowDown', 'RotateCcw', 'LoaderCircle', 'X', 'Check', 'AlertTriangle'];
  runInNewContext(source, { exports, window, Set, Map, Error, Number, String, Math, Promise, ...hooks,
    ...Object.fromEntries(icons.map(x => [x, x])), StatCard: 'StatCard', EmptyState: 'EmptyState', ReviewCandidateRow: 'ReviewCandidateRow',
    getCollectorCoreClient: () => fixture.api, usePlatformCore: () => fixture.api, useWorkbenchPlatform: () => ({platform: "instagram"}), REVIEW_PAGE_SIZE, createReviewQueueReader, reviewQueueKey, selectedReviewIds,
    formatCount: String, asRecord: value => value ?? {}, addCandidateToSplit: async () => {},
    retainLiveSelection(current, live) { const ids = [...current].filter(id => live.has(id)); return ids.length === current.size ? current : new Set(ids); },
    require(name) { if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' }; throw new Error(`Unexpected import ${name}`); },
  });
  const props = { snapshot: { pending: { public: [], private: [] }, counts: {}, split_candidates: [], dedupe: { total: 9000 } }, disabled: () => false,
    async run(key, action, success) { try { const value = await action(fixture.api); if (success) notices.push(typeof success === 'function' ? success(value) : success); return true; } catch (reason) { notices.push(reason.message); return false; } },
  };
  if (snapshotOverride !== undefined) props.snapshot = snapshotOverride;
  const view = { tree: null, notices, timers,
    render() { assert.ok(mounted); stateIndex = refIndex = effectIndex = memoIndex = 0; dirty = false; view.tree = exports.ReviewWorkspace(props); for (const item of effects) if (item.pending) { item.pending = false; item.cleanup?.(); item.cleanup = item.effect(); } return view; },
    async settle(maxCommits = 25) { for (let i = 0; i < maxCommits; i++) { await flush(); if (!dirty || !mounted) return view; view.render(); } throw new Error('Hook commits did not settle'); },
    all(type) { const found = []; const visit = node => { if (Array.isArray(node)) return node.forEach(visit); if (!node || typeof node !== 'object') return; if (node.type === type) found.push(node); visit(node.props?.children); }; visit(view.tree); return found; },
    button(label) { const buttons = view.all('button').filter(node => textOf(node).includes(label)); assert.equal(buttons.length, 1, `Expected one button ${label}`); return buttons[0]; },
    click(label) { const button = view.button(label); assert.equal(Boolean(button.props.disabled), false, `${label} must be enabled`); return button.props.onClick(); },
    rows() { return view.all('ReviewCandidateRow'); },
    rowIds() { return view.rows().map(row => row.props.item.id); },
    selected() { return view.rows().filter(row => row.props.selected).map(row => row.props.item.id); },
    tick() { for (const fn of [...timers.values()]) fn(); },
    text() { return textOf(view.tree); },
    dispose() { mounted = false; effects.forEach(item => item.cleanup?.()); },
  };
  return view.render();
}

test('selecting all freezes IDs; incoming accounts remain in first layer while a repeated move submits once', async () => {
  const f = coreFixture([candidate('a'), candidate('b'), candidate('earlier', 'public', 2)]), view = mount(f);
  try {
    await view.settle(); view.click('全选本页'); await view.settle();
    f.data.push(candidate('incoming')); view.tick(); await view.settle();
    assert.deepEqual(view.rowIds(), ['a', 'b', 'incoming']);
    assert.deepEqual(view.selected(), ['a', 'b']);
    const move = view.button('转入等待筛选').props.onClick; move(); move();
    await view.settle();
    assert.equal(f.calls.moves.length, 1);
    assert.deepEqual(f.calls.moves[0], { candidate_ids: ['a', 'b'], visibility: 'public', from_stage: 1, to_stage: 2 });
    assert.deepEqual(view.rowIds(), ['incoming']);
    view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes('等待筛选')).props.onClick(); await view.settle();
    assert.deepEqual(view.rowIds(), ['a', 'b', 'earlier']);
    f.data.push(candidate('newer')); view.tick(); await view.settle();
    assert.deepEqual(view.rowIds(), ['a', 'b', 'earlier']);
    assert.equal(f.data.find(item => item.id === 'newer').review_stage, 1);
  } finally { view.dispose(); }
});

test('private and public layers use distinct server queries and returned rows remain pending', async () => {
  const f = coreFixture([candidate('public-second', 'public', 2), candidate('private-second', 'private', 2)]), view = mount(f);
  try {
    await view.settle(); view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes('等待筛选')).props.onClick(); await view.settle();
    assert.deepEqual(view.rowIds(), ['public-second']);
    view.click('全选本页'); await view.settle(); view.click('私密审核'); await view.settle();
    assert.deepEqual(view.rowIds(), ['private-second']); assert.deepEqual(view.selected(), []);
    view.click('全选本页'); await view.settle(); view.click('退回第一层'); await view.settle();
    assert.equal(f.data[1].review_stage, 1); assert.equal(f.data[1].status, 'pending');
    assert.equal(f.data[0].review_stage, 2);
    view.click('新采集结果'); await view.settle(); assert.deepEqual(view.rowIds(), ['private-second']);
  } finally { view.dispose(); }
});

test('pagination reaches records beyond a snapshot cap and select-all acts on current page only', async () => {
  const f = coreFixture(Array.from({ length: 2501 }, (_, i) => candidate(`u${i}`))), view = mount(f);
  try {
    await view.settle(); assert.equal(view.rowIds().length, 500); assert.match(view.text(), /共 2501 个/);
    view.click('全选本页'); await view.settle(); view.click('下一页'); await view.settle();
    assert.deepEqual(view.selected(), []); assert.equal(view.rowIds()[0], 'u500');
    view.click('全选本页'); await view.settle(); view.click('转入等待筛选'); await view.settle();
    assert.equal(f.calls.moves[0].candidate_ids.length, 500); assert.equal(f.calls.moves[0].candidate_ids[0], 'u500');
    assert.equal(f.data[0].review_stage, 1); assert.equal(f.data[1000].review_stage, 1);
    for (let i = 0; i < 3; i++) { view.click('下一页'); await view.settle(); }
    assert.deepEqual(view.rowIds(), ['u2500']);
    assert.equal(view.button('下一页').props.disabled, true);
  } finally { view.dispose(); }
});

test('bulk review retains failed selections; successful rows are removed from server layer', async () => {
  const bad = candidate('retry-me', 'public', 2); bad.fail = true;
  const f = coreFixture([candidate('approve-me', 'public', 2), bad]), view = mount(f);
  try {
    await view.settle(); view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes('等待筛选')).props.onClick(); await view.settle(); view.click('全选本页'); await view.settle();
    view.click('批量合格'); await view.settle();
    assert.deepEqual(view.rowIds(), ['retry-me']); assert.deepEqual(view.selected(), ['retry-me']);
    assert.equal(f.data[0].status, 'approved'); assert.equal(f.data[1].review_stage, 2);
    assert.match(view.notices.at(-1), /成功 1 个，失败 1 个/);
  } finally { view.dispose(); }
});

test('after reviewing last page the list moves to the last remaining valid page', async () => {
  const f = coreFixture(Array.from({ length: 501 }, (_, i) => candidate(`u${i}`))), view = mount(f);
  try {
    await view.settle(); view.click('下一页'); await view.settle();
    const row = view.rows()[0]; await row.props.decideCandidate(row.props.item, 'public', 'rejected'); await view.settle();
    assert.equal(view.rowIds().length, 500); assert.equal(view.rowIds()[0], 'u0');
    assert.equal(view.button('上一页').props.disabled, true);
  } finally { view.dispose(); }
});

test('query failures keep previous rows visible but disable decisions until successful refresh', async () => {
  const f = coreFixture([candidate('a')]), view = mount(f);
  try {
    await view.settle(); const original = f.api.reviewQueue;
    f.api.reviewQueue = async () => { throw new Error('Core 暂时离线'); };
    view.tick(); await view.settle();
    assert.deepEqual(view.rowIds(), ['a']); assert.equal(view.rows()[0].props.reviewMutationBusy, true);
    assert.match(view.text(), /Core 暂时离线/); assert.equal(view.button('全选本页').props.disabled, true);
    f.api.reviewQueue = original; view.click('刷新名单'); await view.settle();
    assert.equal(view.rows()[0].props.reviewMutationBusy, false); assert.doesNotMatch(view.text(), /暂时离线/);
  } finally { view.dispose(); }
});

test('single-flight reader ignores stale pre-move results, scope switches and disposed callbacks', async () => {
  const pending = deferred(), values = [], errors = [], reads = [];
  let query = { visibility: 'public', review_stage: 1, offset: 0, limit: REVIEW_PAGE_SIZE };
  const reader = createReviewQueueReader({ query: () => query, read: q => { reads.push(q); return reads.length === 1 ? pending.promise : Promise.resolve({ items: [q.review_stage] }); }, onValue: (page, q) => values.push([page, q]), onError: error => errors.push(error) });
  const first = reader.refresh(); await flush();
  reader.invalidate(); query = { ...query, review_stage: 2 };
  const second = reader.refresh(true); const third = reader.refresh(true);
  assert.equal(reads.length, 1, 'reads do not overlap');
  pending.resolve({ items: ['stale'] }); await Promise.all([first, second, third]);
  assert.equal(reads.length, 2); assert.deepEqual(values.map(x => x[0].items), [[2]]);
  const late = deferred();
  const disposed = createReviewQueueReader({ query: () => query, read: () => late.promise, onValue: v => values.push(v), onError: e => errors.push(e) });
  const running = disposed.refresh(); await flush(); disposed.dispose(); late.reject(new Error('late read')); await running;
  assert.equal(errors.length, 0); assert.equal(values.length, 1);
});

test('the query scope is explicit and selected IDs exclude new or removed rows', () => {
  assert.notEqual(reviewQueueKey({ visibility: 'public', review_stage: 1, offset: 0, limit: REVIEW_PAGE_SIZE }), reviewQueueKey({ visibility: 'public', review_stage: 2, offset: 0, limit: REVIEW_PAGE_SIZE }));
  assert.deepEqual(selectedReviewIds([{ id: 'a' }, { id: 'new' }], new Set(['a', 'removed'])), ['a']);
  const area = workbench.slice(start, end);
  assert.doesNotMatch(area, /review_tier/);
  assert.match(area, /全选本页/);
  assert.match(area, /等待筛选/);
  assert.doesNotMatch(area, /二层筛选/);
  assert.equal(REVIEW_PAGE_SIZE, 500);
});

test('failed transfer preserves the exact selection and allows retry without moving new arrivals', async () => {
  const f = coreFixture([candidate('a'), candidate('b')]), view = mount(f);
  try {
    await view.settle(); view.click('全选本页'); await view.settle();
    const original = f.api.moveReviewStage;
    f.api.moveReviewStage = async () => { throw new Error('转移暂时失败'); };
    view.click('转入等待筛选'); await view.settle();
    assert.deepEqual(view.selected(), ['a', 'b']); assert.match(view.notices.at(-1), /转移暂时失败/);
    f.data.push(candidate('later')); view.tick(); await view.settle();
    f.api.moveReviewStage = original; view.click('转入等待筛选'); await view.settle();
    assert.deepEqual(f.calls.moves[0].candidate_ids, ['a', 'b']); assert.equal(f.data[2].review_stage, 1);
  } finally { view.dispose(); }
});

test('switching layer during a slow first read cannot display the old layer or let it overwrite an error', async () => {
  const f = coreFixture([candidate('first'), candidate('second', 'public', 2)]), oldRead = deferred();
  const query = f.api.reviewQueue; let first = true;
  f.api.reviewQueue = input => { if (first) { first = false; return oldRead.promise; } return query(input); };
  const view = mount(f);
  try {
    await view.settle(); view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes('等待筛选')).props.onClick(); await view.settle();
    oldRead.resolve({ items: [candidate('first')], total: 1, offset: 0, limit: REVIEW_PAGE_SIZE, has_more: false, counts: { public: { stage1: 1, stage2: 1 }, private: { stage1: 0, stage2: 0 } } });
    await view.settle(); assert.deepEqual(view.rowIds(), ['second']);
    assert.equal(f.calls.queries.at(-1).review_stage, 2);
  } finally { view.dispose(); }
});


test('500-row selection is explicit, excludes new arrivals and transfers exactly the selected page', async () => {
  const f = coreFixture(Array.from({ length: 530 }, (_, i) => candidate(`u${i}`))), view = mount(f);
  try {
    await view.settle();
    assert.equal(f.calls.queries[0].limit, 500);
    assert.equal(view.rowIds().length, 500);
    view.click('全选本页'); await view.settle();
    f.data.unshift(candidate('new-arrival')); view.tick(); await view.settle();
    // The newly arrived row has displaced the old page's last row; selection
    // remains the intersection with this page and never selects the newcomer.
    assert.equal(view.selected().length, 499);
    assert.equal(view.selected().includes('new-arrival'), false);
    view.click('转入等待筛选'); await view.settle();
    assert.equal(f.calls.moves[0].candidate_ids.length, 499);
    assert.equal(f.data[0].review_stage, 1);
    assert.equal(f.data.find(x => x.id === 'u499').review_stage, 1);
    const clean = coreFixture(Array.from({ length: 501 }, (_, i) => candidate(`p${i}`))), all = mount(clean);
    try {
      await all.settle(); all.click('全选本页'); await all.settle(); all.click('转入等待筛选'); await all.settle();
      assert.equal(clean.calls.moves[0].candidate_ids.length, 500);
      assert.equal(new Set(clean.calls.moves[0].candidate_ids).size, 500);
      assert.equal(clean.data[500].review_stage, 1);
    } finally { all.dispose(); }
  } finally { view.dispose(); }
});

test('large bulk review displays progress and keeps a failed row selected after processing all 500', async () => {
  const items = Array.from({ length: 500 }, (_, i) => candidate(`u${i}`, 'public', 2));
  items[499].fail = true;
  const f = coreFixture(items), waiting = deferred(), decide = f.api.decideReview;
  let first = true;
  f.api.decideReview = async payload => { if (first) { first = false; await waiting.promise; } return decide(payload); };
  const view = mount(f);
  try {
    await view.settle(); view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes('等待筛选')).props.onClick(); await view.settle();
    view.click('全选本页'); await view.settle(); view.click('批量合格'); await view.settle();
    assert.match(view.text(), /已处理 0 \/ 500/);
    assert.equal(view.button('下一页').props.disabled, true);
    waiting.resolve(); await view.settle(100);
    assert.equal(f.calls.decisions.length, 500);
    assert.deepEqual(view.rowIds(), ['u499']); assert.deepEqual(view.selected(), ['u499']);
    assert.match(view.notices.at(-1), /成功 499 个，失败 1 个/);
    assert.doesNotMatch(view.text(), /已处理/);
  } finally { view.dispose(); }
});


test('review reads and decides from its own page before any full snapshot exists', async () => {
  const f = coreFixture([candidate('a')]), view = mount(f, null);
  try {
    assert.equal(view.all('StatCard').find(x => x.props.label === '全局去重数量').props.value, undefined);
    await view.settle();
    assert.deepEqual(view.rowIds(), ['a']);
    assert.equal(view.all('StatCard').find(x => x.props.label === '公开待审核').props.value, 1);
    const row = view.rows()[0].props;
    await row.decideCandidate(row.item, 'public', 'approved'); await view.settle();
    assert.equal(f.calls.decisions.length, 1);
    assert.deepEqual(view.rowIds(), []);
  } finally { view.dispose(); }
});

test('workbench mounts independent review through initial full-snapshot loading or failure', () => {
  const begin = workbench.indexOf('function LiveFormalWorkbench(');
  const end = workbench.indexOf('\nfunction ', begin + 1);
  const code = ts.transpileModule(workbench.slice(begin, end) + '\nexports.view=LiveFormalWorkbench;', {
    compilerOptions: {target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX},
  }).outputText;
  const exports = {}, jsx = (type, props) => ({type, props: props || {}});
  const symbols = ['Rail','ChatGPTButton','GoogleTranslatorButton','RefreshCw','LoaderCircle','AlertTriangle',
    'StorageStatus','HomeWorkspace','WorkbenchBody','StableReviewWorkspace','Database','Settings','Trash2','RecoveryCollectionControls'];
  runInNewContext(code, {exports, ...Object.fromEntries(symbols.map(x => [x,x])),
    useWorkbenchPlatform:()=>({platform:"instagram"}), PAGE_COPY: {review:{title:'审核'},collection:{title:'采集'}}, truncatedSnapshotScopes: () => [], formatCount:String,
    require: () => ({jsx,jsxs:jsx,Fragment:'fragment'}),
  });
  for (const loading of [true,false]) {
    const core = {snapshot:null, error:loading?null:new Error('本机服务响应超时'), loading,
      refresh:async()=>{}, reviewRun:()=>{}, reviewDisabled:()=>false};
    const tree = exports.view({mode:'review',core});
    const nodes = [];
    const visit = node => { if(Array.isArray(node)) return node.forEach(visit);
      if(node && typeof node === 'object') {nodes.push(node); visit(node.props?.children);} };
    visit(tree);
    const row = nodes.find(n => n.type === 'StableReviewWorkspace');
    assert.ok(row, 'initial global snapshot must not prevent independent review from mounting');
    assert.equal(row.props.snapshot, null, 'missing global data must not become an invented empty snapshot');
    assert.equal(row.props.run, core.reviewRun);
    const collection = exports.view({mode:'collection',core});
    nodes.length = 0; visit(collection);
    assert.ok(nodes.find(n => n.type === 'RecoveryCollectionControls'),
      'initial full read must not block the independent task controls');
  }
});

test('initial recovery controls address only the observed task and expose no allocation or resume', async () => {
  const begin = workbench.indexOf('function RecoveryCollectionControls(');
  const end = workbench.indexOf('\nfunction ', begin + 1);
  const code = ts.transpileModule(workbench.slice(begin,end) + '\nexports.view=RecoveryCollectionControls;', {
    compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},
  }).outputText;
  const exports={}, jsx=(type,props)=>({type,props:props||{}}), calls=[];
  runInNewContext(code,{exports,formatTime:String,normalizeStatus:x=>x,
    ACTIVE_STATES:new Set(['running']),PAUSED_STATES:new Set(['paused']),
    StatusBadge:'StatusBadge',Pause:'Pause',Square:'Square',
    require:()=>({jsx,jsxs:jsx,Fragment:'fragment'})});
  const buttons=[];
  const visit=node=>{if(Array.isArray(node))return node.forEach(visit);
    if(node&&typeof node==='object'){if(node.type==='button')buttons.push(node);visit(node.props?.children);}};
  const controls={feedback:()=>({safetyControlDisabled:false}),run:async(...args)=>calls.push(args)};
  visit(exports.view({live:null,controls})); assert.equal(buttons.length,0);
  visit(exports.view({live:{generated_at:'observed',tasks:[{task:{id:'owned-task',name:'Current',status:'running',
    targets:[{id:'current-target',current_window_id:'owned-window',status:'running'},
      {id:'waiting-target',current_window_id:'waiting-window',status:'waiting_network'}]}}]},controls}));
  assert.equal(buttons.length,2);
  assert.deepEqual(buttons.map(textOf),['暂停','停止当前窗口']);
  for(const button of buttons)button.props.onClick();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.map(x=>x[1]))),[
    [{scope:'task',taskId:'owned-task',action:'pause'}],
    [{scope:'window',taskId:'owned-task',profileId:'owned-window',targetId:'current-target',action:'stop'},
      {scope:'window',taskId:'owned-task',profileId:'waiting-window',targetId:'waiting-target',action:'stop'}]]);
});

test('independent read failure fences row and batch handlers before React commits the error', async () => {
  const f = coreFixture([candidate('a')]), view = mount(f);
  try {
    await view.settle(); view.click('全选本页'); await view.settle();
    const previous = view.rows()[0].props;
    f.api.reviewQueue = async () => { throw new Error('review page unavailable'); };
    view.tick(); await flush(); // Deliver the failure without committing React state.
    await previous.decideCandidate(previous.item, 'public', 'approved');
    view.click('转入等待筛选'); await flush();
    assert.equal(f.calls.decisions.length, 0);
    assert.equal(f.calls.moves.length, 0);
  } finally { view.dispose(); }
});

test('switching review scope immediately invalidates old row handlers before React commits', async () => {
  for (const label of ['私密审核', '等待筛选', '下一页']) {
    const f = coreFixture(Array.from({length: 501}, (_, i) => candidate(`a${i}`))), view = mount(f);
    try {
      await view.settle();
      const previous = view.rows()[0].props;
      if (label === '等待筛选') {
        view.all('button').find(node => node.props.role === 'tab' && textOf(node).includes(label)).props.onClick();
      } else view.click(label);
      await previous.decideCandidate(previous.item, 'public', 'approved');
      assert.equal(f.calls.decisions.length, 0, label);
    } finally { view.dispose(); }
  }
});

test('refresh fences old decisions immediately and successful replacement waits for a render', async () => {
  for (const action of ['refresh', 'replacement']) {
    const f = coreFixture([candidate('a')]), view = mount(f);
    try {
      await view.settle(); view.click('全选本页'); await view.settle();
      const previous = view.rows()[0].props;
      if (action === 'refresh') view.click('刷新名单');
      else { f.data.splice(0, 1, candidate('b')); view.tick(); await flush(); }
      await previous.decideCandidate(previous.item, 'public', 'approved');
      view.click('批量合格'); await flush();
      assert.equal(f.calls.decisions.length, 0, action);
      await view.settle();
      const current = view.rows()[0].props;
      await current.decideCandidate(current.item, 'public', 'approved');
      await view.settle();
      assert.equal(f.calls.decisions.length, 1, 'current committed row must remain usable');
    } finally { view.dispose(); }
  }
});

test('a failed independent review read blocks stale single-row handlers until that page recovers', async () => {
  const f = coreFixture([candidate('a')]), read = f.api.reviewQueue, view = mount(f);
  try {
    await view.settle();
    const previous = view.rows()[0].props;
    f.api.reviewQueue = async () => { throw new Error('review page unavailable'); };
    view.tick(); await view.settle();
    await previous.decideCandidate(previous.item, 'public', 'approved');
    assert.equal(f.calls.decisions.length, 0);
    f.api.reviewQueue = read;
    view.tick(); await view.settle();
    const current = view.rows()[0].props;
    await current.decideCandidate(current.item, 'public', 'approved');
    await view.settle();
    assert.equal(f.calls.decisions.length, 1);
    assert.deepEqual(view.rowIds(), []);
  } finally { view.dispose(); }
});

test('review split shortcut confirms repeats and preserves active queue ownership', async () => {
  const begin = workbench.indexOf('async function addSplitTargetsWithConfirmation(');
  const end = workbench.indexOf('\nfunction CandidateRow(', begin);
  const js = ts.transpileModule(`${workbench.slice(begin,end)}\nexports.addCandidateToSplit=addCandidateToSplit;`, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  for (const disposition of ['completed','ignored','history','waiting','running']) for (const confirm of [false,true]) {
    const calls=[],notices=[],exports={};
    runInNewContext(js,{exports,collectionPlatform,window:{confirm:()=>confirm,alert:value=>notices.push(value)}});
    const api={async addWaitingSplitTargets(names,allow){calls.push({names,allow});return {candidates:[],accepted_ids:[],duplicates:allow?[]:[{username:'existing',disposition}]}}};
    await exports.addCandidateToSplit(candidate('existing'),async(_key,action)=>{await action(api)});
    const repeat=['completed','ignored','history'].includes(disposition);
    assert.equal(calls.length,repeat&&confirm?2:1);
    assert.equal(calls[0].allow,false);
    if(calls.length===2)assert.equal(calls[1].allow,true);
    if(!repeat)assert.equal(notices.length,1);
  }
});
