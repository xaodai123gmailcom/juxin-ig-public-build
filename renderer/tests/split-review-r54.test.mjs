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
const componentSource = compile('split-review-workspace.tsx');
const plain = value => JSON.parse(JSON.stringify(value));
const textOf = node => node == null || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node) : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.props?.children);
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no}); return {promise, resolve, reject}};
const flush = async () => {for(let i = 0; i < 16; i++) await Promise.resolve()};
function withTimezone(zone, callback) {const previous = process.env.TZ; process.env.TZ = zone; try {return callback()} finally {if(previous === undefined) delete process.env.TZ; else process.env.TZ = previous}}
const entry = (id, extra = {}) => ({id, username: `target.${id}`, completed_at: '2026-09-23T08:30:00+00:00', source_window_id: 'window-1', source_task_id: 'task-1', source_target_id: 'target-1', ...extra});
const page = (items, extra = {}) => ({items, total: items.length, offset: 0, limit: 100, has_more: false, start: '', end: '', retention_days: 7, ...extra});

test('manual split judgment updates the selected day rate and persists on refresh', async () => {
  let decision = null;
  const api = async () => page([entry('judged', {review_decision: decision})], {decision_counts: decision === 'passed' ? {passed: 1, failed: 0} : {passed: 0, failed: 0}});
  api.decide = async (kind, id, next) => {assert.equal(kind, 'split'); assert.equal(id, 'judged'); decision = next; return {record_id: id, decision: next, decision_counts: next === 'passed' ? {passed: 1, failed: 0} : {passed: 0, failed: 1}}};
  const view = mount(api);
  try {
    await view.settle(); assert.match(view.text(), /当日合格率：—/);
    view.click('已通过'); await view.settle(); assert.match(view.text(), /当日合格率：100.0%/);
    view.click('刷新记录'); await view.settle(); assert.equal(view.button('已通过').props['aria-pressed'], true);
    view.click('未通过'); await view.settle(); assert.match(view.text(), /当日合格率：0.0%/);
  } finally {view.dispose()}
});

test('seven local calendar days cross month and year boundaries without an eighth date', () => withTimezone('Asia/Bangkok', () => {
  const days = helpers.recentSplitReviewDays(new Date('2027-01-02T01:00:00+07:00'));
  assert.deepEqual(plain(days.map(day => day.key)), ['2027-01-02', '2027-01-01', '2026-12-31', '2026-12-30', '2026-12-29', '2026-12-28', '2026-12-27']);
  assert.equal(days[0].start, '2027-01-02T00:00:00.000+07:00');
  assert.equal(days[0].end, '2027-01-03T00:00:00.000+07:00');
}));

test('daylight saving boundaries use calendar days, including 23 and 25 hour days', () => withTimezone('America/Los_Angeles', () => {
  for (const [now, duration, startOffset, endOffset] of [['2026-03-08T12:00:00-07:00', 23, '-08:00', '-07:00'], ['2026-11-01T12:00:00-08:00', 25, '-07:00', '-08:00']]) {
    const [day] = helpers.recentSplitReviewDays(new Date(now));
    assert.equal((new Date(day.end) - new Date(day.start)) / 3600000, duration);
    assert.ok(day.start.endsWith(startOffset)); assert.ok(day.end.endsWith(endOffset));
  }
}));

test('today query freezes at refresh time while historical days include their whole calendar day', () => withTimezone('Asia/Bangkok', () => {
  const now = new Date('2026-09-23T10:12:13.456+07:00'), days = helpers.recentSplitReviewDays(now);
  const today = helpers.splitReviewQueryBounds(days[0], now), yesterday = helpers.splitReviewQueryBounds(days[1], now);
  assert.equal(today.end, '2026-09-23T10:12:13.456+07:00');
  assert.equal(yesterday.end, days[1].end);
  assert.equal(today.utcOffsetMinutes, 420);
}));

test('daily badge bounds retain each local midnight across DST and share the frozen today cutoff', () => withTimezone('America/Los_Angeles', () => {
  const now = new Date('2026-11-02T18:12:13.456-08:00'), days = helpers.recentSplitReviewDays(now);
  const ranges = helpers.splitReviewDailyBounds(days, now);
  assert.equal(ranges.length, 7); assert.equal(ranges[0].end, helpers.splitReviewQueryBounds(days[0], now).end);
  assert.equal(ranges[1].key, '2026-11-01');
  assert.equal((new Date(ranges[1].end) - new Date(ranges[1].start)) / 3600000, 25);
  assert.equal(helpers.splitReviewDayCountDisplay({today: 0}, 'today'), '0 个');
  assert.equal(helpers.splitReviewDayCountDisplay({today: 1234}, 'today'), '1,234 个');
  for(const counts of [null, {}, {today: -1}, {today: '3'}, {today: 1.5}]) assert.equal(helpers.splitReviewDayCountDisplay(counts, 'today'), '—');
}));

test('historical DST dates carry the current offset separately for seven-day retention', () => withTimezone('America/Los_Angeles', () => {
  const now = new Date('2026-11-02T23:30:00-08:00'), days = helpers.recentSplitReviewDays(now);
  const oldest = helpers.splitReviewQueryBounds(days[6], now);
  assert.equal(days[6].key, '2026-10-27');
  assert.ok(oldest.start.endsWith('-07:00')); assert.ok(oldest.end.endsWith('-07:00'));
  assert.equal(oldest.utcOffsetMinutes, -480);
}));

test('same account may have multiple completion records; only repeated record IDs are merged', () => {
  const first = entry('one', {username: 'repeat.user'}), second = entry('two', {username: 'repeat.user'});
  assert.deepEqual(plain(helpers.mergeSplitReviewEntries([first], [first, second, second])), [first, second]);
});

test('audit links allow only direct Instagram profile paths accepted by desktop preview', () => {
  assert.equal(helpers.splitReviewProfileUrl(' @some.user_1 '), 'https://www.instagram.com/some.user_1/');
  for(const value of ['accounts', 'DIRECT', 'explore', 'stories', '../profile', 'https://evil.test', 'a?b=c', '', 'a'.repeat(31)]) assert.equal(helpers.splitReviewProfileUrl(value), null, value);
});

test('review metrics show recorded nonnegative safe integers, preserve zero and leave invalid values unknown', () => {
  for(const [value, expected] of [[0, '0'], [3, '3'], [1234567, '1,234,567'], [Number.MAX_SAFE_INTEGER, '9,007,199,254,740,991']]) {
    assert.equal(helpers.splitReviewMetricDisplay(value), expected);
  }
  for(const value of [undefined, null, -1, 1.5, '3', '', false, true, NaN, Infinity, -Infinity, Number.MAX_SAFE_INTEGER + 1, {}, []]) {
    assert.equal(helpers.splitReviewMetricDisplay(value), '—', String(value));
  }
});

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
    if(name === './workbench-platform') return {useWorkbenchPlatform:()=>({platform:'instagram'}),usePlatformCore: () => (api._platformClient ||= {splitReviewReport: api, reportReviewDecision: api.decide})};
    if(name === './split-review-report') return helpers;
    throw new Error(`Unexpected production import ${name}`);
  }});
  const view = {
    tree: null, timers, listeners,
    render() {assert.ok(mounted); stateIndex = refIndex = effectIndex = callbackIndex = 0; dirty = false; view.tree = module.exports.SplitReviewWorkspace(); for(const item of effects) if(item.pending) {item.pending = false; item.cleanup?.(); item.cleanup = item.effect()} return view},
    async settle() {for(let i = 0; i < 30; i++) {await flush(); if(!dirty || !mounted) return view; view.render()} throw new Error('Hook commits did not settle')},
    all(type) {const found = []; const visit = node => {if(Array.isArray(node)) return node.forEach(visit); if(!node || typeof node !== 'object') return; if(node.type === type) found.push(node); visit(node.props?.children)}; visit(view.tree); return found},
    button(label) {const result = view.all('button').filter(node => textOf(node).includes(label)); assert.equal(result.length, 1, `one button for ${label}`); return result[0]},
    click(label) {const button = view.button(label); assert.equal(Boolean(button.props.disabled), false, `${label} enabled`); return button.props.onClick()},
    text() {return textOf(view.tree)},
    dispose() {if(!mounted) return; mounted = false; effects.forEach(item => item.cleanup?.())},
  };
  return view.render();
}

test('production report opens account via independent review href and queries its own paginated endpoint', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(args); return page([entry('one')], {total: 2087, has_more: true})});
  try {
    await view.settle();
    assert.equal(calls.length, 1); assert.equal(calls[0][2], 0); assert.equal(calls[0][3], 100); assert.equal(calls[0][4], -new Date().getTimezoneOffset());
    assert.match(view.text(), /2,087/); assert.match(view.text(), /共 2,087 条/);
    assert.equal(view.all('a')[0].props.href, 'https://www.instagram.com/target.one/');
    assert.equal(view.all('a')[0].props.target, '_blank');
    assert.equal(view.all('button').filter(button => button.props['aria-pressed'] !== undefined).length, 9);
  } finally {view.dispose()}
});

test('split count is a separate account-adjacent column and keeps completion details intact', async () => {
  const view = mount(async () => page([entry('three', {split_count: 3}), entry('many', {split_count: 1024})]));
  try {
    await view.settle();
    assert.deepEqual(view.all('th').map(textOf), ['分裂号', '审查结果', '分裂次数', '源账号数据', '本次采集', '结束日期与时间', '采集来源窗口']);
    assert.equal(view.all('th')[2].props.scope, 'col');
    const badges = view.all('span').filter(node => node.props.className?.startsWith('split-review-count-badge'));
    assert.deepEqual(badges.map(textOf), ['3 次', '1,024 次']);
    assert.equal(view.all('time').length, 2);
    assert.equal(view.all('time')[0].props.dateTime, '2026-09-23T08:30:00+00:00');
    assert.equal(view.all('a').length, 2);
    assert.match(view.text(), /window-1/);
  } finally {view.dispose()}
});

test('source account and completion-run groups render separate recorded values beside unchanged account and completion details', async () => {
  const view = mount(async () => page([
    entry('source', {
      split_count: 3, followers: 1234, following: 567, posts: 0, processed_count: 90, new_count: 60, duplicate_count: 30,
      source_window_id: 'window-source', completed_at: '2026-09-23T06:07:08+00:00',
      profile: {full_name: 'Source Person', followers: 99999, following: 99999, posts: 99999},
    }),
    entry('other', {split_count: 4, followers: 0, following: 0, posts: 12, processed_count: 2001, new_count: 0, duplicate_count: 2001}),
  ]));
  try {
    await view.settle();
    assert.equal(view.all('dl').length, 4);
    assert.deepEqual(view.all('dt').map(textOf), ['粉丝', '关注', '帖子', '已处理', '新入库', '重复跳过', '粉丝', '关注', '帖子', '已处理', '新入库', '重复跳过']);
    assert.deepEqual(view.all('dd').map(textOf), ['1,234', '567', '0', '90', '60', '30', '0', '0', '12', '2,001', '0', '2,001']);
    assert.equal(view.all('a')[0].props.href, 'https://www.instagram.com/target.source/');
    assert.match(view.text(), /Source Person/); assert.match(view.text(), /window-source/); assert.doesNotMatch(view.text(), /99,999/);
    assert.equal(view.all('time')[0].props.dateTime, '2026-09-23T06:07:08+00:00');
    assert.deepEqual(view.all('span').filter(node => node.props.className?.startsWith('split-review-count-badge')).map(textOf), ['3 次', '4 次']);
  } finally {view.dispose()}
});

test('missing and malformed recorded metrics remain unknown despite numeric profile hints', async () => {
  const profile = {followers: 4321, following: 1234, posts: 42, follower_count: 4321, following_count: 1234, media_count: 42, processed_count: 90, new_count: 60, duplicate_count: 30};
  const view = mount(async () => page([
    entry('missing', {profile}),
    entry('malformed', {profile, followers: null, following: '1234', posts: -1, processed_count: 1.5, new_count: undefined, duplicate_count: Number.MAX_SAFE_INTEGER + 1}),
  ]));
  try {
    await view.settle();
    assert.equal(view.all('dl').length, 4);
    assert.deepEqual(view.all('dd').map(textOf), Array(12).fill('—'));
    assert.doesNotMatch(view.text(), /4,321|1,234/);
    assert.equal(view.all('a').length, 2); assert.equal(view.all('time').length, 2);
  } finally {view.dispose()}
});

test('unknown or malformed split counts stay unknown without fabricated zero or one', async () => {
  const values = [undefined, null, -1, 1.5, '3', Number.MAX_SAFE_INTEGER + 1, 0];
  const view = mount(async () => page(values.map((value, index) => entry(String(index), {split_count: value}))));
  try {
    await view.settle();
    const badges = view.all('span').filter(node => node.props.className?.startsWith('split-review-count-badge'));
    assert.deepEqual(badges.map(textOf), ['—', '—', '—', '—', '—', '—', '0 次']);
    assert.ok(badges.slice(0, 6).every(node => node.props.className.endsWith('is-unknown') && node.props.title === '历史累计次数暂未记录'));
  } finally {view.dispose()}
});

test('incomplete legacy count visibly reports its proven minimum rather than a fabricated exact count', async () => {
  const view = mount(async () => page([entry('legacy', {split_count: null, split_count_recorded: 2, split_count_complete: false}), entry('inconsistent', {split_count: 3, split_count_recorded: 1024, split_count_complete: false}), entry('no-evidence', {split_count: null, split_count_recorded: 0, split_count_complete: false})]));
  try {
    await view.settle();
    const badges = view.all('span').filter(node => node.props.className?.startsWith('split-review-count-badge'));
    assert.deepEqual(badges.map(textOf), ['至少 2 次', '至少 1,024 次', '—']);
    assert.equal(badges[0].props.title, '旧历史可能缺少之前的记录，已确认至少 2 次；此数量为累计下界');
    assert.equal(badges[1].props.title, '旧历史可能缺少之前的记录，已确认至少 1,024 次；此数量为累计下界');
    assert.equal(badges[2].props.title, '历史累计次数暂未记录');
  } finally {view.dispose()}
});

test('paging reuses frozen boundaries, guards repeated clicks and refresh resets to latest first page', async () => {
  const pending = deferred(), calls = [];
  const view = mount((...args) => {calls.push(args); return calls.length === 2 ? pending.promise : Promise.resolve(page([entry(`first${calls.length}`)], {total: 2, has_more: true}))});
  try {
    await view.settle(); const click = view.button('加载更多').props.onClick;
    click(); click(); await view.settle(); assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].slice(0, 2), calls[0].slice(0, 2)); assert.equal(calls[1][4], calls[0][4]); assert.deepEqual(calls[1][5], calls[0][5]); assert.equal(calls[1][2], 1);
    pending.resolve(page([entry('second')], {total: 2, offset: 1})); await view.settle();
    assert.match(view.text(), /当天全部 2 条/); assert.equal(view.all('a').length, 2);
    view.click('刷新记录'); await view.settle(); assert.equal(calls[2][2], 0); assert.equal(view.all('a').length, 1);
  } finally {view.dispose()}
});

test('all seven date badges use authoritative totals and discard stale counts on date change or refresh failure', async () => {
  const old = deferred(), refresh = deferred(), days = helpers.recentSplitReviewDays(), calls = [];
  const daily = Object.fromEntries(days.map((day, index) => [day.key, index === 0 ? 1234 : index]));
  const view = mount((...args) => {calls.push(args); return calls.length === 1 ? old.promise : calls.length === 3 ? refresh.promise : Promise.resolve(page([entry('one')], {daily_counts: daily, total: 1234}))});
  const badges = () => view.all('span').filter(node => node.props.className === 'split-review-day-count').map(textOf);
  try {
    await view.settle(); assert.deepEqual(badges(), Array(7).fill('—'));
    view.click('昨天'); await view.settle(); assert.deepEqual(badges(), ['1,234 个', '1 个', '2 个', '3 个', '4 个', '5 个', '6 个']);
    assert.equal(calls[1][5].length, 7); assert.equal(view.all('a').length, 1, 'badges are not loaded page lengths');
    old.resolve(page([], {daily_counts: Object.fromEntries(days.map(day => [day.key, 999]))})); await view.settle(); assert.equal(badges()[0], '1,234 个');
    view.click('刷新记录'); await view.settle(); assert.deepEqual(badges(), Array(7).fill('—'));
    refresh.reject(new Error('refresh failed')); await view.settle(); assert.deepEqual(badges(), Array(7).fill('—'));
  } finally {view.dispose()}
});

test('switching day prevents old day success or failure overwriting current records', async () => {
  for(const fail of [false, true]) {
    const old = deferred(), calls = [];
    const view = mount((...args) => {calls.push(args); return calls.length === 1 ? old.promise : Promise.resolve(page([entry('newday')]))});
    try {
      await view.settle(); view.click('昨天'); await view.settle();
      if(fail) old.reject(new Error('old day failure')); else old.resolve(page([entry('stale')]));
      await view.settle();
      assert.match(view.text(), /target.newday/); assert.doesNotMatch(view.text(), /target.stale|old day failure/);
      assert.notEqual(calls[0][0], calls[1][0]);
    } finally {view.dispose()}
  }
});

test('failed later page keeps visible records and retries the same offset', async () => {
  const calls = [];
  const view = mount((...args) => {calls.push(args); return calls.length === 2 ? Promise.reject(new Error('暂时离线')) : Promise.resolve(calls.length === 1 ? page([entry('one')], {total: 2, has_more: true}) : page([entry('two')], {total: 2, offset: 1}))});
  try {
    await view.settle(); view.click('加载更多'); await view.settle();
    assert.match(view.text(), /暂时离线/); assert.match(view.text(), /target.one/);
    view.click('重试'); await view.settle(); assert.equal(calls[2][2], calls[1][2]); assert.equal(view.all('a').length, 2); assert.doesNotMatch(view.text(), /暂时离线/);
  } finally {view.dispose()}
});

test('initial failure is distinct from an empty day and late unmount response is ignored', async () => {
  const view = mount(async () => {throw new Error('network down')});
  try {await view.settle(); assert.match(view.text(), /暂时无法显示记录/); assert.doesNotMatch(view.text(), /这一天暂无已完成/)} finally {view.dispose()}
  const pending = deferred(), late = mount(() => pending.promise);
  late.dispose(); pending.resolve(page([entry('late')])); await late.settle();
  assert.equal(late.timers.size, 0); assert.equal(late.listeners.get('focus').size, 0); assert.doesNotMatch(late.text(), /target.late/);
});


test('r94 a short visible list is not presented as full collection in the real report', async () => {
  const coverage = {source_total: 432, discovered: 326, processed: 326, pending_count: 0,
    remaining_count: 106, unobserved_count: 106, discovery_finished: true, status: 'gap', end_reason: 'visible_list_end'};
  const view = mount(async () => page([entry('gap', {processed_count: 326, new_count: 324, duplicate_count: 2,
    mode_coverage: {followers: coverage}})]));
  try {
    await view.settle();
    assert.match(view.text(), /106 个未核实/);
    assert.match(view.text(), /主页 432 · 已发现 326 · 已处理 326 · 待处理 0 · 未核实 106/);
    assert.match(view.text(), /具体账号及原因无法确认/);
    assert.doesNotMatch(view.text(), /数量已对齐/);
  } finally {view.dispose()}
});

test('r94 only ended and reconciled counters may display aligned coverage', () => {
  assert.equal(helpers.collectionCoverageDisplay('followers', {discovery_finished: true, end_reason: 'visible_list_end',
    status: 'reconciled', pending_count: 0, remaining_count: 0}).warning, false);
  for (const value of [{}, {discovery_finished: false, status: 'reconciled', pending_count: 0, remaining_count: 0},
    {discovery_finished: true, end_reason: 'visible_list_end', status: 'pending', pending_count: 3, remaining_count: 3},
    {discovery_finished: true, end_reason: 'visible_list_end', status: 'count_mismatch', pending_count: 0, remaining_count: 0}])
    assert.equal(helpers.collectionCoverageDisplay('followers', value).warning, true);
});
