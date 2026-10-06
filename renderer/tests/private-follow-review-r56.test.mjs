import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const compile = name => ts.transpileModule(readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8'), {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
}).outputText;
const helperModule = {exports: {}};
runInNewContext(compile('split-review-report.ts'), {module: helperModule, exports: helperModule.exports, Date, Set, Math, String});
const helpers = helperModule.exports;
const privateModule = {exports: {}};
runInNewContext(compile('private-follow-review-report.ts'), {module: privateModule, exports: privateModule.exports, Set});
const privateHelpers = privateModule.exports;
const componentSource = compile('private-follow-review-workspace.tsx');
const plain = value => JSON.parse(JSON.stringify(value));
const textOf = node => node == null || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node) : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.props?.children);
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no}); return {promise, resolve, reject}};
const flush = async () => {for(let i = 0; i < 16; i++) await Promise.resolve()};
function withTimezone(zone, callback) {const previous = process.env.TZ; process.env.TZ = zone; try {return callback()} finally {if(previous === undefined) delete process.env.TZ; else process.env.TZ = previous}}
const entry = (id, extra = {}) => ({id, username: `target.${id}`, completed_at: '2026-09-23T08:30:00+00:00', source_window_id: 'window-1', status: 'confirmed', ...extra});
const page = (items, extra = {}) => ({items, total: items.length, offset: 0, limit: 100, has_more: false, start: '', end: '', retention_days: 7, ...extra});

function mount(api) {
  const states = [], refs = [], effects = [], callbacks = [], listeners = new Map(), timers = new Map();
  let stateIndex = 0, refIndex = 0, effectIndex = 0, callbackIndex = 0, timerSerial = 0, dirty = false, mounted = true;
  const sameDeps = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(initial) {const slot = stateIndex++; if(!(slot in states)) states[slot] = typeof initial === 'function' ? initial() : initial; return [states[slot], next => {const value = typeof next === 'function' ? next(states[slot]) : next; if(!Object.is(states[slot], value)) {states[slot] = value; dirty = true}}]},
    useRef(initial) {const slot = refIndex++; return refs[slot] ??= {current: initial}},
    useEffect(effect, deps) {const slot = effectIndex++; if(!sameDeps(effects[slot]?.deps, deps)) effects[slot] = {effect, deps, cleanup: effects[slot]?.cleanup, pending: true}},
    useCallback(callback, deps) {const slot = callbackIndex++; if(!sameDeps(callbacks[slot]?.deps, deps)) callbacks[slot] = {callback, deps}; return callbacks[slot].callback},
  };
  const window = {
    setInterval(callback, delay) {timers.set(++timerSerial, {callback, delay}); return timerSerial}, clearInterval(id) {timers.delete(id)},
    addEventListener(name, fn) {if(!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn)}, removeEventListener(name, fn) {listeners.get(name)?.delete(fn)},
  };
  const module = {exports: {}}, jsx = (type, props) => typeof type === 'function' ? type(props) : ({type, props: props ?? {}});
  const review = {exports: {}};
  runInNewContext(compile('report-review-decision.tsx'), {module: review, exports: review.exports, Intl, require(name) {
    if(name === 'react/jsx-runtime') return {jsx, jsxs: jsx, Fragment: 'fragment'};
    throw new Error(name);
  }});
  runInNewContext(componentSource, {module, exports: module.exports, Date, Intl, Error, window, require(name) {
    if(name === 'react') return hooks;
    if(name === 'react/jsx-runtime') return {jsx, jsxs: jsx, Fragment: 'fragment'};
    if(name === 'lucide-react') return Object.fromEntries(['ArrowUpRight', 'CalendarDays', 'CheckCircle2', 'ChevronDown', 'Clock3', 'RefreshCw', 'ShieldCheck', 'UsersRound'].map(name => [name, name]));
    if(name === './report-review-decision') return review.exports;
    if(name === './workbench-platform') return {useWorkbenchPlatform:()=>({platform:'instagram'}),usePlatformCore: () => (api._platformClient ||= {privateFollowReviewReport: api, reportReviewDecision: api.decide})};
    if(name === './split-review-report') return helpers;
    if(name === './private-follow-review-report') return privateHelpers;
    throw new Error(`Unexpected production import ${name}`);
  }});
  const view = {
    tree: null, timers, listeners,
    render() {assert.ok(mounted); stateIndex = refIndex = effectIndex = callbackIndex = 0; dirty = false; view.tree = module.exports.PrivateFollowReviewWorkspace(); for(const item of effects) if(item.pending) {item.pending = false; item.cleanup?.(); item.cleanup = item.effect()} return view},
    async settle() {for(let i = 0; i < 30; i++) {await flush(); if(!dirty || !mounted) return view; view.render()} throw new Error('Hook commits did not settle')},
    all(type) {const found = []; const visit = node => {if(Array.isArray(node)) return node.forEach(visit); if(!node || typeof node !== 'object') return; if(node.type === type) found.push(node); visit(node.props?.children)}; visit(view.tree); return found},
    button(label) {const result = view.all('button').filter(node => textOf(node).includes(label)); assert.equal(result.length, 1, `one button for ${label}`); return result[0]},
    click(label) {const button = view.button(label); assert.equal(Boolean(button.props.disabled), false, `${label} enabled`); return button.props.onClick()},
    text() {return textOf(view.tree)},
    dispose() {if(!mounted) return; mounted = false; effects.forEach(item => item.cleanup?.())},
  };
  return view.render();
}

const countPills = view => view.all('span').filter(node => node.props.className === 'split-review-day-count').map(textOf);
const dailyCounts = values => Object.fromEntries(helpers.recentSplitReviewDays().map((day, index) => [day.key, values[index] ?? 0]));

test('follow states distinguish a sent request from confirmed following without guessing historical acceptance', () => {
  assert.equal(privateHelpers.privateFollowReviewStatusDisplay('requested').label, '请求已发送');
  assert.match(privateHelpers.privateFollowReviewStatusDisplay('requested').title, /不代表对方已接受/);
  assert.equal(privateHelpers.privateFollowReviewStatusDisplay('following').label, '已关注');
  for(const value of [undefined, null, 'confirmed', 'Requested', 'Following', 'unknown', '', true, {}]) {
    assert.equal(privateHelpers.privateFollowReviewStatusDisplay(value).label, '历史状态未记录');
    assert.equal(privateHelpers.privateFollowReviewStatusDisplay(value).status, 'unknown');
  }
});

test('only repeated record IDs merge; repeated account names retain their separate successful attempts', () => {
  const first = entry('one', {username: 'repeat.user'}), second = entry('two', {username: 'repeat.user'});
  assert.deepEqual(plain(privateHelpers.mergePrivateFollowReviewEntries([first], [first, second, second])), [first, second]);
});

test('actual private report queries its dedicated endpoint and displays only account, source, state, time and window fields', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(args); return page([
    entry('requested', {followers: 1234, following: 0, posts: 12, follow_state: 'requested', window_name: '执行窗口 9', executor_username: 'operator', profile: {full_name: 'Private Person'}, processed_count: 999, split_count: 888}),
    entry('following', {followers: 0, following: 15, posts: 0, follow_state: 'following'}),
  ], {total: 2087, has_more: true, daily_counts: dailyCounts([2087, 0, 3, 4, 5, 6, 7])})});
  try {
    await view.settle();
    assert.equal(calls.length, 1); assert.equal(calls[0][2], 0); assert.equal(calls[0][3], 100); assert.equal(calls[0][4], -new Date().getTimezoneOffset());
    assert.equal(calls[0][5].length, 7); assert.equal(calls[0][5][0].start, calls[0][0]); assert.equal(calls[0][5][0].end, calls[0][1]);
    assert.deepEqual(view.all('th').map(textOf), ['私密账号', '审查结果', '源账号数据', '关注状态', '成功确认时间', '执行窗口']);
    assert.deepEqual(view.all('dd').map(textOf), ['1,234', '0', '12', '0', '15', '0']);
    assert.deepEqual(view.all('dt').map(textOf), ['粉丝', '关注', '帖子', '粉丝', '关注', '帖子']);
    assert.deepEqual(view.all('span').filter(node => node.props.className?.startsWith('private-follow-status ')).map(textOf), ['请求已发送', '已关注']);
    assert.equal(view.all('a')[0].props.href, 'https://www.instagram.com/target.requested/'); assert.equal(view.all('a')[0].props.target, '_blank');
    assert.equal(view.all('time')[0].props.dateTime, '2026-09-23T08:30:00+00:00');
    assert.match(view.text(), /Private Person/); assert.match(view.text(), /执行窗口 9/); assert.match(view.text(), /@operator/);
    assert.doesNotMatch(view.text(), /分裂次数|本次采集|新入库|999|888/);
    assert.deepEqual(countPills(view), ['2,087 个', '0 个', '3 个', '4 个', '5 个', '6 个', '7 个']);
  } finally {view.dispose()}
});

test('missing and malformed direct metrics stay unknown and raw confirmation or profile hints cannot invent follow status', async () => {
  const view = mount(async () => page([
    entry('legacy', {confirmation: 'Following', profile: {followers: 9999, following: 9999, posts: 9999, follow_state: 'following'}, source_window_id: null}),
    entry('invalid', {followers: -1, following: '3', posts: Number.MAX_SAFE_INTEGER + 1, follow_state: 'unknown'}),
    entry('unsafe', {username: '../bad', followers: 0, following: null, posts: 1.5}),
  ]));
  try {
    await view.settle();
    assert.deepEqual(view.all('dd').map(textOf), ['—', '—', '—', '—', '—', '—', '0', '—', '—']);
    assert.deepEqual(view.all('span').filter(node => node.props.className?.startsWith('private-follow-status ')).map(textOf), Array(3).fill('历史状态未记录'));
    assert.equal(view.all('a').length, 2); assert.match(view.text(), /历史未记录/); assert.doesNotMatch(view.text(), /9,999/);
    assert.deepEqual(countPills(view), Array(7).fill('—'));
  } finally {view.dispose()}
});

test('paging freezes selected and daily boundaries, guards repeated clicks, merges records and refresh resets to offset zero', async () => {
  const pending = deferred(), calls = [], first = entry('first');
  const view = mount((...args) => {calls.push(args); return calls.length === 2 ? pending.promise : Promise.resolve(page([first], {total: 101, has_more: true, daily_counts: dailyCounts([101])}))});
  try {
    await view.settle(); const loadMore = view.button('加载更多').props.onClick;
    loadMore(); loadMore(); await view.settle(); assert.equal(calls.length, 2); assert.equal(calls[1][2], 1);
    assert.deepEqual(calls[1].slice(0, 2), calls[0].slice(0, 2)); assert.equal(calls[1][4], calls[0][4]); assert.deepEqual(calls[1][5], calls[0][5]);
    pending.resolve(page([first, entry('second')], {total: 101, offset: 1, has_more: true, daily_counts: dailyCounts([101])})); await view.settle();
    assert.equal(view.all('a').length, 2); assert.equal(countPills(view)[0], '101 个');
    view.click('刷新记录'); await view.settle(); assert.equal(calls[2][2], 0); assert.equal(view.all('a').length, 1);
  } finally {view.dispose()}
});

test('stale day success and failure cannot replace the selected day records or seven day counts', async () => {
  for(const fail of [false, true]) {
    const old = deferred(), calls = [], view = mount((...args) => {calls.push(args); return calls.length === 1 ? old.promise : Promise.resolve(page([entry('current')], {daily_counts: dailyCounts([12, 42])}))});
    try {
      await view.settle(); assert.deepEqual(countPills(view), Array(7).fill('—')); view.click('昨天'); await view.settle();
      if(fail) old.reject(new Error('stale failure')); else old.resolve(page([entry('stale')], {daily_counts: dailyCounts([999, 999])}));
      await view.settle(); assert.match(view.text(), /target.current/); assert.doesNotMatch(view.text(), /target.stale|stale failure/);
      assert.deepEqual(countPills(view).slice(0, 2), ['12 个', '42 个']); assert.notEqual(calls[0][0], calls[1][0]);
    } finally {view.dispose()}
  }
});

test('refresh counts are unknown while loading or failed, then replace with accepted real zeros without inferring page size', async () => {
  const failure = deferred(), calls = []; const view = mount((...args) => {calls.push(args); return calls.length === 2 ? failure.promise : Promise.resolve(page(calls.length === 1 ? [entry('one')] : [], {daily_counts: dailyCounts(calls.length === 1 ? [1000, 5] : [0, 0])}))});
  try {
    await view.settle(); assert.equal(countPills(view)[0], '1,000 个');
    view.click('刷新记录'); await view.settle(); assert.deepEqual(countPills(view), Array(7).fill('—')); assert.match(view.text(), /正在读取私密关注记录/);
    failure.reject(new Error('temporarily offline')); await view.settle(); assert.match(view.text(), /暂时无法显示记录/); assert.doesNotMatch(view.text(), /这一天暂无成功/); assert.deepEqual(countPills(view), Array(7).fill('—'));
    view.click('重试'); await view.settle(); assert.match(view.text(), /这一天暂无成功的私密关注记录/); assert.deepEqual(countPills(view), Array(7).fill('0 个'));
  } finally {view.dispose()}
});

test('failed later page keeps records and retries its offset; unmount ignores late replies and removes listeners', async () => {
  const calls = [], view = mount((...args) => {calls.push(args); return calls.length === 2 ? Promise.reject(new Error('page offline')) : Promise.resolve(page([entry(calls.length === 1 ? 'first' : 'second')], {total: 2, offset: calls.length === 1 ? 0 : 1, has_more: calls.length === 1, daily_counts: dailyCounts([2])}))});
  try {
    await view.settle(); view.click('加载更多'); await view.settle(); assert.match(view.text(), /target.first/); assert.match(view.text(), /page offline/);
    view.click('重试'); await view.settle(); assert.equal(calls[2][2], calls[1][2]); assert.equal(view.all('a').length, 2); assert.doesNotMatch(view.text(), /page offline/);
  } finally {view.dispose()}
  const pending = deferred(), late = mount(() => pending.promise); late.dispose();
  pending.resolve(page([entry('late')], {daily_counts: dailyCounts([999])})); await late.settle();
  assert.equal(late.timers.size, 0); assert.equal(late.listeners.get('focus').size, 0); assert.doesNotMatch(late.text(), /target.late|999 个/);
});
