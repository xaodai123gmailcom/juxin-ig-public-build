import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {createRequire} from 'node:module';
import test from 'node:test';
// Keep the merged secondary overview in the existing renderer build gate.
import './report-data-overview-r6.test.mjs';
import ts from 'typescript';
import {createCollectorCoreClient} from '../src/core-client.ts';

const componentSource = ts.transpileModule(readFileSync(new URL('../src/reports-workspace.tsx', import.meta.url), 'utf8'), {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
}).outputText;
const textOf = node => node == null || typeof node === 'boolean' ? '' : typeof node === 'string' || typeof node === 'number' ? String(node) : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.props?.children);
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no}); return {promise, resolve, reject}};
const flush = async () => {for(let i = 0; i < 16; i++) await Promise.resolve()};
async function withTimezone(zone, callback) {
  const previous = process.env.TZ; process.env.TZ = zone;
  try {return await callback()} finally {if(previous === undefined) delete process.env.TZ; else process.env.TZ = previous}
}
const counts = extra => ({follow: 0, greet: 0, split: 0, added: 0, posting: 0, collection: 0, check: 0, nurture: 0, approved: 0, confirmed_posting: 0, ...extra});
const row = (id, extra = {}) => ({profile_id: `window-${id}`, window_name: `窗口 ${id}`, username: `operator.${id}`, instagram_user_id: `ig-${id}`, ...counts(), ...extra});
const report = (rows = [], extra = {}) => ({start: '2026-09-23T00:00:00.000Z', end: '2026-09-24T00:00:00.000Z', rows, totals: counts(), unattributed: 0, ...extra});
const summary = (totals = {}, extra = {}) => ({start: '2026-09-23T00:00:00.000Z', end: '2026-09-24T00:00:00.000Z', totals: {collection: 0, follow: 0, split: 0, added: 0, confirmed_posting: 0, ...totals}, ...extra});
const plain = value => JSON.parse(JSON.stringify(value));
const metricLabels = {follow: '私密点关注', greet: '公开打招呼', split: '分裂数量', added: '新增数量', posting: '历史发帖（原功能）', confirmed_posting: '新队列确认发帖', collection: '采集账号', check: '关注检查（轮）', nurture: '养号完成（轮）', approved: '审核合格'};

function loadComponent(hooks = {}, api = () => Promise.resolve(report()), browser = {}) {
  const module = {exports: {}}, jsx = (type, props) => ({type, props: props ?? {}});
  runInNewContext(componentSource, {module, exports: module.exports, Date, Intl, Error, Blob, ...browser, require(name) {
    if(name === 'react') return hooks;
    if(name === 'react/jsx-runtime') return {jsx, jsxs: jsx, Fragment: 'fragment'};
    if(name === 'lucide-react') return Object.fromEntries(['BarChart3', 'Download', 'RefreshCw', 'ShieldCheck', 'UserRoundCheck'].map(name => [name, name]));
    if(name === './report-data-overview') return {ReportDataOverview: () => jsx('section', {'data-testid': 'data-overview', children: '累计数据概览内容'})};
    if(name === './workbench-platform') return {useWorkbenchPlatform:()=>({platform:'instagram'}),usePlatformCore: () => (api._platformClient ||= {workReport: api})};
    if(name === './split-review-workspace') return {SplitReviewWorkspace: () => jsx('section', {'data-testid': 'split-review', children: '分裂号审查内容'})};
    if(name === './private-follow-review-workspace') return {PrivateFollowReviewWorkspace: () => jsx('section', {'data-testid': 'private-follow-review', children: '私密关注检查内容'})};
    if(name === './reports-workspace.css') return {};
    throw new Error(`Unexpected production import ${name}`);
  }});
  return module.exports;
}
const {reportBounds} = loadComponent();

// Each function component gets its own hooks, including the actual nested ActivityReports.
function mount(api, now = '2026-09-23T12:00:00.000Z') {
  const frames = new Map(), downloads = [], blobs = new Map(), timers = [], revoked = [];
  let active, dirty = false, mounted = true, serial = 0;
  const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const hooks = {
    useState(initial) {const frame = active, slot = frame.stateIndex++; if(!(slot in frame.states)) frame.states[slot] = typeof initial === 'function' ? initial() : initial; return [frame.states[slot], next => {if(!frame.mounted) return; const value = typeof next === 'function' ? next(frame.states[slot]) : next; if(!Object.is(frame.states[slot], value)) {frame.states[slot] = value; dirty = true}}]},
    useRef(initial) {const slot = active.refIndex++; return active.refs[slot] ??= {current: initial}},
    useEffect(effect, deps) {const slot = active.effectIndex++; if(!sameDeps(active.effects[slot]?.deps, deps)) active.effects[slot] = {effect, deps, cleanup: active.effects[slot]?.cleanup, pending: true}},
    useCallback(callback, deps) {const slot = active.callbackIndex++; if(!sameDeps(active.callbacks[slot]?.deps, deps)) active.callbacks[slot] = {callback, deps}; return active.callbacks[slot].callback},
  };
  class Clock extends Date {constructor(...args) {super(...(args.length ? args : [now]))} static now() {return new Date(now).getTime()}}
  const component = loadComponent(hooks, api, {
    Date: Clock,
    URL: {createObjectURL(blob) {const url = `blob:report-${++serial}`; blobs.set(url, blob); return url}, revokeObjectURL(url) {revoked.push(url); blobs.delete(url)}},
    document: {createElement(type) {assert.equal(type, 'a'); return {click() {downloads.push({href: this.href, filename: this.download, blob: blobs.get(this.href)})}}}},
    setTimeout(callback, delay) {timers.push({callback, delay}); return timers.length},
  });
  const cleanup = frame => {frame.mounted = false; frame.effects.forEach(effect => effect.cleanup?.())};
  function resolve(node, path, seen) {
    if(Array.isArray(node)) return node.map((child, i) => resolve(child, `${path}.${i}`, seen));
    if(!node || typeof node !== 'object') return node;
    if(typeof node.type === 'function') {
      let frame = frames.get(path);
      if(frame && frame.type !== node.type) {cleanup(frame); frames.delete(path); frame = null}
      if(!frame) {frame = {type: node.type, mounted: true, states: [], refs: [], effects: [], callbacks: []}; frames.set(path, frame)}
      frame.stateIndex = frame.refIndex = frame.effectIndex = frame.callbackIndex = 0;
      seen.add(path); active = frame;
      const child = node.type(node.props);
      return resolve(child, `${path}.render`, seen);
    }
    return {...node, props: {...node.props, children: resolve(node.props.children, `${path}.children`, seen)}};
  }
  const view = {
    tree: null, downloads, timers, revoked,
    render() {
      assert.ok(mounted); dirty = false;
      const seen = new Set(); view.tree = resolve({type: component.ReportsWorkspace, props: {}}, 'root', seen);
      for(const [path, frame] of frames) if(!seen.has(path)) {cleanup(frame); frames.delete(path)}
      for(const frame of frames.values()) for(const effect of frame.effects) if(effect.pending) {effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.effect()}
      return view;
    },
    async settle() {for(let i = 0; i < 30; i++) {await flush(); if(!dirty || !mounted) return view; view.render()} throw new Error('Hook commits did not settle')},
    all(type) {const found = []; const visit = node => {if(Array.isArray(node)) return node.forEach(visit); if(!node || typeof node !== 'object') return; if(node.type === type) found.push(node); visit(node.props?.children)}; visit(view.tree); return found},
    button(label) {const found = view.all('button').filter(node => textOf(node).includes(label)); assert.equal(found.length, 1, `one button for ${label}`); return found[0]},
    click(label) {const button = view.button(label); assert.equal(Boolean(button.props.disabled), false, `${label} enabled`); button.props.onClick()},
    date(value) {view.all('input').find(node => node.props.type === 'date').props.onChange({target: {value}})},
    knownCards() {return Object.fromEntries(Object.entries(view.cards()).filter(([key]) => key !== 'posting'))},
    cards() {return Object.fromEntries(view.all('div').filter(node => node.props.className?.startsWith('report-card ')).map(node => [node.props.className.match(/report-card report-(\w+)/)[1], {label: textOf(node.props.children[0]), value: textOf(node.props.children[1])}]))},
    text() {return textOf(view.tree)},
    dispose() {if(!mounted) return; mounted = false; frames.forEach(cleanup)},
  };
  return view.render();
}

function parseCsv(text) {
  const rows = []; let cells = [], value = '', quoted = false;
  for(let i = 0; i < text.length; i++) {
    const char = text[i];
    if(char === '"') {if(quoted && text[i + 1] === '"') {value += '"'; i++} else quoted = !quoted}
    else if(!quoted && char === ',') {cells.push(value); value = ''}
    else if(!quoted && char === '\r' && text[i + 1] === '\n') {cells.push(value); rows.push(cells); cells = []; value = ''; i++}
    else value += char;
  }
  cells.push(value); rows.push(cells); return rows;
}

test('local report dates cover a day, Monday-based week and calendar month across year and leap boundaries', async () => withTimezone('Asia/Bangkok', () => {
  for(const [period, day, start, end] of [
    ['day', '2027-01-01', '2026-12-31T17:00:00.000Z', '2027-01-01T17:00:00.000Z'],
    ['week', '2027-01-02', '2026-12-27T17:00:00.000Z', '2027-01-03T17:00:00.000Z'],
    ['week', '2027-01-04', '2027-01-03T17:00:00.000Z', '2027-01-10T17:00:00.000Z'],
    ['month', '2024-02-29', '2024-01-31T17:00:00.000Z', '2024-02-29T17:00:00.000Z'],
    ['month', '2026-12-31', '2026-11-30T17:00:00.000Z', '2026-12-31T17:00:00.000Z'],
  ]) {
    const bounds = reportBounds(period, day); assert.equal(bounds.start, start); assert.equal(bounds.end, end);
  }
}));

test('DST reports follow local midnight boundaries instead of fixed 24-hour periods', async () => withTimezone('America/Los_Angeles', () => {
  for(const [period, day, hours, start, end] of [
    ['day', '2026-03-08', 23, '2026-03-08T08:00:00.000Z', '2026-03-09T07:00:00.000Z'],
    ['day', '2026-11-01', 25, '2026-11-01T07:00:00.000Z', '2026-11-02T08:00:00.000Z'],
    ['week', '2026-03-08', 167, '2026-03-02T08:00:00.000Z', '2026-03-09T07:00:00.000Z'],
    ['week', '2026-11-01', 169, '2026-10-26T07:00:00.000Z', '2026-11-02T08:00:00.000Z'],
    ['month', '2026-03-20', 743, '2026-03-01T08:00:00.000Z', '2026-04-01T07:00:00.000Z'],
    ['month', '2026-11-20', 721, '2026-11-01T07:00:00.000Z', '2026-12-01T08:00:00.000Z'],
  ]) {
    const bounds = reportBounds(period, day); assert.equal(bounds.start, start); assert.equal(bounds.end, end);
    assert.equal((new Date(bounds.end) - new Date(bounds.start)) / 3600000, hours);
  }
}));

test('workReport opts into summaries without changing existing full activity or history payloads', async () => {
  const calls = [], client = createCollectorCoreClient({request: async (...args) => {calls.push(args); return {}},
    secureSet: async () => true, secureGet: async () => null, secureDelete: async () => true, configureIntegrations: async () => ({restarted: false})});
  await client.workReport('activity', 'start', 'end', {summaryOnly: true});
  await client.workReport('activity', 'start', 'end');
  await client.workReport('activity', 'start', 'end', {summaryOnly: false});
  await client.workReport('activity', 'start', 'end', {});
  await client.workReport('history', 'start', 'end');
  assert.deepEqual(calls[0], ['/api/reports/query', {method: 'POST', body: {kind: 'activity', start: 'start', end: 'end', summary_only: true}}]);
  for(const call of calls.slice(1, 4)) assert.deepEqual(call, ['/api/reports/query', {method: 'POST', body: {kind: 'activity', start: 'start', end: 'end'}}]);
  assert.deepEqual(calls[4], ['/api/reports/query', {method: 'POST', body: {kind: 'history', start: 'start', end: 'end'}}]);
});

test('workspace loads only four summary totals and removes the whole details panel while retaining review navigation', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(plain(args)); return summary({collection: 10001, follow: 21, split: 31, added: 41})});
  try {
    await view.settle(); assert.equal(calls.length, 1); assert.equal(calls[0][0], 'activity'); assert.deepEqual(calls[0][3], {summaryOnly: true});
    assert.deepEqual(view.cards(), {collection: {label: '采集总数', value: '10,001'}, follow: {label: '私密点关注成功数', value: '21'}, split: {label: '分裂数量', value: '31'}, added: {label: '新增数量', value: '41'}, posting: {label: '发帖数量', value: '0'}});
    assert.doesNotMatch(view.text(), /窗口与账号明细|执行窗口|执行账号|本地时区|暂无完成记录/);
    assert.equal(view.button('展开数据概览').props['aria-expanded'], false); assert.doesNotMatch(view.text(), /累计数据概览内容/);
    assert.equal(view.all('table').length, 0); assert.deepEqual(view.all('h2').map(textOf), ['数据概览']);
    assert.equal(view.all('section').filter(node => node.props.className === 'formal-panel').length, 0);
    assert.equal(view.all('input').length, 1); assert.equal(view.all('input')[0].props.type, 'date');
    for(const label of ['当日', '当周', '当月', '回到今天', '刷新报表', '导出 CSV']) assert.ok(view.button(label));
    view.click('分裂号审查'); await view.settle(); assert.match(view.text(), /分裂号审查内容/);
    view.click('私密关注检查'); await view.settle(); assert.match(view.text(), /私密关注检查内容/); assert.doesNotMatch(view.text(), /分裂号审查内容/);
    view.click('工作统计'); await view.settle(); assert.equal(calls.length, 2); assert.equal(view.all('table').length, 0);
    assert.ok(calls.every(call => call[3]?.summaryOnly === true)); assert.equal(view.downloads.length, 0);
  } finally {view.dispose()}
});

test('pending or incomplete summaries never display invented zero counts or enable export', async () => {
  const pending = deferred(), view = mount(() => pending.promise);
  try {
    await view.settle(); assert.ok(Object.values(view.knownCards()).every(card => card.value === '读取中')); assert.equal(view.button('导出 CSV').props.disabled, true);
    pending.resolve({totals: {collection: 10}}); await view.settle();
    assert.ok(Object.values(view.cards()).every(card => card.value === '—')); assert.match(view.text(), /报表汇总不完整/); assert.equal(view.button('导出 CSV').props.disabled, true);
    assert.doesNotMatch(view.text(), /NaN/);
  } finally {view.dispose()}
});

test('CSV lazily fetches a complete fresh report with all nine metrics and aggregate totals', async () => {
  const first = row('a', counts({collection: 3, follow: 5, split: 7, added: 9, greet: 11, posting: 13, check: 15, nurture: 17, approved: 19}));
  const second = row('b', counts({collection: 2, follow: 4, split: 6, added: 8, greet: 10, posting: 12, check: 14, nurture: 16, approved: 18}));
  const totals = Object.fromEntries(Object.keys(metricLabels).map(key => [key, first[key] + second[key]]));
  const calls = [], pending = deferred(), view = mount((...args) => {calls.push(plain(args)); return args[3]?.summaryOnly ? Promise.resolve(summary({split: 4})) : pending.promise});
  try {
    await view.settle(); assert.equal(calls.length, 1); assert.equal(view.cards().split.value, '4');
    view.click('导出 CSV'); await view.settle();
    assert.equal(calls.length, 2); assert.equal(calls[1].length, 3); assert.deepEqual(calls[1], calls[0].slice(0, 3));
    assert.equal(view.downloads.length, 0); assert.equal(view.button('导出 CSV').props.disabled, true); assert.equal(view.button('导出 CSV').props['aria-busy'], true);
    assert.match(view.text(), /正在导出 CSV/); assert.equal(view.cards().split.value, '4'); assert.equal(view.button('刷新报表').props.disabled, false);
    pending.resolve(report([first, second], {totals})); await view.settle();
    assert.equal(view.downloads.length, 1); assert.equal(view.cards().split.value, '4'); assert.equal(view.button('导出 CSV').props.disabled, false);
    const lines = parseCsv((await view.downloads[0].blob.text()).replace(/^\uFEFF/, ''));
    assert.equal(lines.length, 5); assert.equal(lines[4][0], '合计');
    for(const [key, label] of Object.entries(metricLabels)) {
      const column = lines[1].indexOf(label.replace('（轮）', '')); assert.ok(column >= 4, label);
      assert.equal(lines[2][column], String(first[key])); assert.equal(lines[3][column], String(second[key])); assert.equal(lines[4][column], String(totals[key]));
    }
  } finally {view.dispose()}
});

test('CSV preserves every row, UTF-8 BOM, quoted text, formula safety, and revokes its download URL', async () => {
  const dangerous = ['=SUM(1,2)', '+1', '-1', '@SUM(1)', '\tformula', '\rformula', '\nformula', '  =SUM(1)'];
  const rows = dangerous.map((window_name, i) => row(String(i), {window_name, username: 'account,"quoted"\r\nline', ...counts({split: i + 1, greet: i + 10})}));
  rows.push(row('ordinary', {window_name: '', username: '', raw_accounts: ['never-export-collected-account']}));
  const full = report(rows, {totals: counts({split: 36, greet: 108}), raw_accounts: ['never-export-global-account']});
  const view = mount(async (...args) => args[3]?.summaryOnly ? summary() : full);
  try {
    await view.settle(); view.click('导出 CSV'); await view.settle();
    assert.equal(view.downloads.length, 1); const {blob, filename, href} = view.downloads[0];
    assert.equal(filename, '聚鑫国际-2026-09-23-day-工作报表.csv'); assert.equal(blob.type, 'text/csv;charset=utf-8');
    assert.deepEqual([...new Uint8Array(await blob.arrayBuffer()).slice(0, 3)], [239, 187, 191]);
    const csv = await blob.text(), lines = parseCsv(csv.replace(/^\uFEFF/, ''));
    assert.equal(lines.length, rows.length + 3); assert.deepEqual(lines[0], ['统计开始', full.start, '统计截止（不含）', full.end]);
    assert.deepEqual(lines[1].slice(0, 4), ['窗口名称', '窗口 ID', '执行 IG 账号', 'IG 账号 ID']);
    for(const [i, value] of dangerous.entries()) {
      assert.equal(lines[i + 2][0], "'" + value); assert.equal(lines[i + 2][2], 'account,"quoted"\r\nline');
    }
    assert.deepEqual(lines.at(-2).slice(0, 4), ['历史未记录', 'window-ordinary', '历史未记录', 'ig-ordinary']);
    assert.doesNotMatch(csv, /never-export/);
    assert.equal(view.timers[0].delay, 1000); assert.deepEqual(view.revoked, []); view.timers[0].callback(); assert.deepEqual(view.revoked, [href]);
  } finally {view.dispose()}
});

test('CSV does not silently cap grouped rows at snapshot or pagination limits', async () => {
  const rows = Array.from({length: 12001}, (_, i) => row(String(i), counts({collection: 1, split: i % 3})));
  const totals = counts({collection: rows.length, split: rows.reduce((sum, item) => sum + item.split, 0)});
  const calls = [], view = mount(async (...args) => {calls.push(plain(args)); return args[3]?.summaryOnly ? summary(totals) : report(rows, {totals})});
  try {
    await view.settle(); view.click('导出 CSV'); await view.settle();
    const lines = parseCsv(await view.downloads[0].blob.text());
    assert.equal(lines.length, 12004); assert.equal(lines[2][1], 'window-0'); assert.equal(lines.at(-2)[1], 'window-12000');
    assert.equal(lines.at(-1)[lines[1].indexOf('采集账号')], '12001'); assert.equal(calls[1].length, 3);
  } finally {view.dispose()}
});

test('date, refresh, week, month and today use summary queries while CSV uses the same complete local bounds', async () => withTimezone('Asia/Bangkok', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(plain(args)); return args[3]?.summaryOnly ? summary() : report([], {start: args[1], end: args[2]})}, '2027-01-01T18:30:00.000Z');
  const check = async (start, end, period, day) => {
    assert.deepEqual(calls.at(-1), ['activity', start, end, {summaryOnly: true}]);
    view.click('导出 CSV'); await view.settle(); assert.deepEqual(calls.at(-1), ['activity', start, end]);
    assert.equal(view.downloads.at(-1).filename, `聚鑫国际-${day}-${period}-工作报表.csv`);
    assert.deepEqual(parseCsv(await view.downloads.at(-1).blob.text())[0].slice(1), [start, '统计截止（不含）', end]);
  };
  try {
    await view.settle(); await check('2027-01-01T17:00:00.000Z', '2027-01-02T17:00:00.000Z', 'day', '2027-01-02');
    view.click('刷新报表'); await view.settle(); await check('2027-01-01T17:00:00.000Z', '2027-01-02T17:00:00.000Z', 'day', '2027-01-02');
    view.click('当周'); await view.settle(); await check('2026-12-27T17:00:00.000Z', '2027-01-03T17:00:00.000Z', 'week', '2027-01-02');
    view.date('2024-02-29'); await view.settle(); assert.equal(calls.at(-1)[3].summaryOnly, true);
    view.click('当月'); await view.settle(); await check('2024-01-31T17:00:00.000Z', '2024-02-29T17:00:00.000Z', 'month', '2024-02-29');
    const before = calls.length; view.date(''); await view.settle(); assert.equal(calls.length, before);
    view.click('回到今天'); await view.settle(); await check('2027-01-01T17:00:00.000Z', '2027-01-02T17:00:00.000Z', 'day', '2027-01-02');
  } finally {view.dispose()}
}));

test('summary and full CSV both preserve DST day, week and month bounds', async () => withTimezone('America/Los_Angeles', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(plain(args)); return args[3]?.summaryOnly ? summary() : report([], {start: args[1], end: args[2]})}, '2026-03-08T18:00:00.000Z');
  try {
    for(const [button, hours] of [[null, 23], ['当周', 167], ['当月', 743]]) {
      if(button) view.click(button);
      await view.settle(); const summaryCall = calls.at(-1);
      assert.equal((new Date(summaryCall[2]) - new Date(summaryCall[1])) / 3600000, hours);
      view.click('导出 CSV'); await view.settle(); assert.deepEqual(calls.at(-1), summaryCall.slice(0, 3));
    }
  } finally {view.dispose()}
}));

test('old date and period successes or failures cannot replace the current summary even before effect cleanup', async () => {
  for(const change of [view => view.date('2026-09-01'), view => view.click('当月')]) for(const fail of [false, true]) {
    const pending = deferred(), calls = [], view = mount((...args) => {calls.push(plain(args)); return calls.length === 1 ? pending.promise : Promise.resolve(summary({split: 42}))});
    try {
      await view.settle(); assert.equal(view.button('导出 CSV').props.disabled, true);
      change(view);
      if(fail) pending.reject(new Error('stale request failure')); else pending.resolve(summary({split: 999}));
      await view.settle(); assert.equal(calls.length, 2); assert.notDeepEqual(calls[1], calls[0]);
      assert.doesNotMatch(view.text(), /stale request failure/); assert.equal(view.cards().split.value, '42'); assert.equal(view.button('导出 CSV').props.disabled, false);
    } finally {view.dispose()}
  }
});

test('date change hides previous counts throughout loading and has no cache when returning to a date', async () => {
  const second = deferred(), third = deferred(); let calls = 0;
  const view = mount(() => ++calls === 1 ? Promise.resolve(summary({split: 15})) : calls === 2 ? second.promise : third.promise);
  try {
    await view.settle(); assert.equal(view.cards().split.value, '15');
    view.date('2026-09-01'); await view.settle(); assert.ok(Object.values(view.knownCards()).every(card => card.value === '读取中'));
    view.date('2026-09-23'); await view.settle(); assert.ok(Object.values(view.knownCards()).every(card => card.value === '读取中'));
    second.resolve(summary({split: 100})); await view.settle(); assert.equal(view.cards().split.value, '读取中');
    third.resolve(summary({split: 16})); await view.settle(); assert.equal(calls, 3); assert.equal(view.cards().split.value, '16');
  } finally {view.dispose()}
});

test('refresh clears previous counts, disables export, reports failures and recovers with real zero totals', async () => {
  const fresh = deferred(), failure = deferred(); let calls = 0;
  const view = mount(() => {calls++; if(calls === 2) return fresh.promise; if(calls === 3) return failure.promise; return Promise.resolve(summary(calls === 1 ? {split: 5} : {}))});
  try {
    await view.settle(); assert.equal(view.cards().split.value, '5');
    view.click('刷新报表'); await view.settle(); assert.equal(view.button('刷新报表').props.disabled, true); assert.equal(view.button('导出 CSV').props.disabled, true);
    assert.ok(Object.values(view.knownCards()).every(card => card.value === '读取中'));
    fresh.resolve(summary({split: 15})); await view.settle(); assert.equal(view.cards().split.value, '15');
    view.click('刷新报表'); await view.settle(); failure.reject(new Error('report offline')); await view.settle();
    assert.match(view.text(), /report offline/); assert.ok(Object.values(view.cards()).every(card => card.value === '—')); assert.equal(view.button('导出 CSV').props.disabled, true);
    view.click('刷新报表'); await view.settle(); assert.doesNotMatch(view.text(), /report offline/);
    assert.ok(Object.values(view.knownCards()).every(card => card.value === '0')); assert.equal(view.button('导出 CSV').props.disabled, false);
  } finally {view.dispose()}
});

test('CSV double clicks share one in-flight export and failures remain separate from summary with retry recovery', async () => {
  let exports = 0; const pending = deferred(), view = mount((...args) => args[3]?.summaryOnly ? Promise.resolve(summary({split: 42})) : ++exports === 1 ? pending.promise : Promise.resolve(report()));
  try {
    await view.settle(); view.click('导出 CSV'); view.click('导出 CSV'); await view.settle(); assert.equal(exports, 1);
    assert.equal(view.button('导出 CSV').props.disabled, true); assert.equal(view.cards().split.value, '42');
    pending.reject(new Error('export offline')); await view.settle(); assert.match(view.text(), /CSV 导出失败：Error: export offline/);
    assert.equal(view.cards().split.value, '42'); assert.equal(view.button('导出 CSV').props.disabled, false); assert.equal(view.downloads.length, 0);
    view.click('导出 CSV'); await view.settle(); assert.equal(exports, 2); assert.equal(view.downloads.length, 1); assert.doesNotMatch(view.text(), /export offline/);
  } finally {view.dispose()}
});

test('incomplete full export response produces an error instead of a silently empty or partial CSV', async () => {
  for(const invalid of [summary(), report([{...row('bad'), split: undefined}]), report([], {totals: {split: 1}})]) {
    let exports = 0; const view = mount(async (...args) => args[3]?.summaryOnly ? summary({split: 9}) : ++exports === 1 ? invalid : report());
    try {
      await view.settle(); view.click('导出 CSV'); await view.settle(); assert.match(view.text(), /完整报表不可用/);
      assert.equal(view.downloads.length, 0); assert.equal(view.cards().split.value, '9'); assert.equal(view.button('导出 CSV').props.disabled, false);
      view.click('导出 CSV'); await view.settle(); assert.equal(view.downloads.length, 1); assert.doesNotMatch(view.text(), /完整报表不可用/);
    } finally {view.dispose()}
  }
});

test('date, period and refresh cancel late export success or failure before a new summary and export', async () => {
  for(const change of [view => view.date('2026-09-01'), view => view.click('当月'), view => view.click('刷新报表')]) for(const fail of [false, true]) {
    let exports = 0; const pending = deferred(), calls = [];
    const view = mount((...args) => {calls.push(plain(args)); return args[3]?.summaryOnly ? Promise.resolve(summary({split: 42})) : ++exports === 1 ? pending.promise : Promise.resolve(report([], {start: args[1], end: args[2]}))});
    try {
      await view.settle(); view.click('导出 CSV'); await view.settle(); change(view);
      if(fail) pending.reject(new Error('stale export failure')); else pending.resolve(report([row('stale')]));
      await view.settle(); assert.equal(view.downloads.length, 0); assert.doesNotMatch(view.text(), /stale export failure/);
      assert.equal(view.cards().split.value, '42'); assert.equal(view.button('导出 CSV').props.disabled, false);
      view.click('导出 CSV'); await view.settle(); assert.equal(view.downloads.length, 1); assert.equal(exports, 2);
      assert.deepEqual(calls.at(-1), calls.at(-2).slice(0, 3));
    } finally {view.dispose()}
  }
});

test('late obsolete export cannot unlock or replace a newer export in progress', async () => {
  const old = deferred(), current = deferred(); let exports = 0;
  const view = mount((...args) => args[3]?.summaryOnly ? Promise.resolve(summary()) : ++exports === 1 ? old.promise : current.promise);
  try {
    await view.settle(); view.click('导出 CSV'); await view.settle(); view.date('2026-09-01'); await view.settle();
    view.click('导出 CSV'); await view.settle(); old.resolve(report([row('stale')])); await view.settle();
    assert.equal(view.downloads.length, 0); assert.equal(view.button('导出 CSV').props.disabled, true); assert.match(view.text(), /正在导出 CSV/);
    current.resolve(report([row('current')])); await view.settle(); assert.equal(view.downloads.length, 1);
    const csv = await view.downloads[0].blob.text(); assert.match(csv, /operator.current/); assert.doesNotMatch(csv, /operator.stale/);
  } finally {view.dispose()}
});

test('a stale export handler cannot start a download after date or navigation intent but before rerender', async () => {
  for(const change of [view => view.date('2026-09-01'), view => view.click('分裂号审查')]) {
    let exports = 0; const view = mount(async (...args) => args[3]?.summaryOnly ? summary() : (exports++, report()));
    try {
      await view.settle(); const staleClick = view.button('导出 CSV').props.onClick; change(view); staleClick(); await view.settle();
      assert.equal(exports, 0); assert.equal(view.downloads.length, 0);
    } finally {view.dispose()}
  }
});

test('switching to either review tab ignores pending summary and export success or failure, including immediate replies', async () => {
  for(const tab of ['分裂号审查', '私密关注检查']) for(const exporting of [false, true]) for(const fail of [false, true]) {
    const pending = deferred(); let calls = 0;
    const view = mount((...args) => {calls++; return calls === 1 && !exporting || args.length === 3 ? pending.promise : Promise.resolve(summary({split: 17}))});
    try {
      await view.settle(); if(exporting) {view.click('导出 CSV'); await view.settle()}
      view.click(tab);
      if(fail) pending.reject(new Error('stale navigation failure')); else pending.resolve(exporting ? report([row('late')]) : summary({split: 999}));
      await view.settle(); assert.match(view.text(), new RegExp(`${tab}内容`)); assert.doesNotMatch(view.text(), /stale navigation failure/); assert.equal(view.downloads.length, 0);
      view.click('工作统计'); await view.settle(); assert.equal(view.cards().split.value, '17'); assert.equal(view.button('导出 CSV').props.disabled, false);
    } finally {view.dispose()}
  }
});

test('unmount cancels pending summary and export outcomes without creating download URLs', async () => {
  for(const exporting of [false, true]) for(const fail of [false, true]) {
    const pending = deferred(), view = mount((...args) => exporting && args[3]?.summaryOnly ? Promise.resolve(summary()) : pending.promise);
    await view.settle(); if(exporting) {view.click('导出 CSV'); await view.settle()}
    view.dispose(); if(fail) pending.reject(new Error('unmounted failure')); else pending.resolve(report([row('late')]));
    await view.settle(); assert.equal(view.downloads.length, 0); assert.equal(view.timers.length, 0);
  }
});


test('native R6 report fixture bundles the actual App and checks the complete removed-panel contract', async () => {
  const require = createRequire(import.meta.url);
  const {buildFixture, assertSummaryState} = require('../../desktop/tests/work-report-summary-r6.integration.cjs');
  const built = await buildFixture();
  assert.ok(built.outputFiles.some(file => file.path.endsWith('.js') && file.contents.length > 1000));
  assert.ok(built.outputFiles.some(file => file.path.endsWith('.css') && file.contents.length > 0));
  const valid = {cards: [{label: '采集总数', value: '433'}, {label: '私密点关注成功数', value: '0'}, {label: '分裂数量', value: '2'}, {label: '新增数量', value: '0'}, {label: '发帖数量', value: '7'}],
    details: false, tables: 0, searches: 0, date: '2026-10-03', controls: ['当日', '当周', '当月', '回到今天', '刷新报表', '导出 CSV'], tabs: ['工作统计', '分裂号审查', '私密关注检查']};
  assertSummaryState(valid);
  for(const invalid of [{details: true}, {tables: 1}, {searches: 1}, {cards: valid.cards.slice(0, 3)}, {controls: ['当日']}, {tabs: ['工作统计']}])
    assert.throws(() => assertSummaryState({...valid, ...invalid}), {name: 'AssertionError'});
});


test('merged cumulative data is collapsed initially and keeps its expansion across report date, refresh and review tabs', async () => {
  const calls = [], view = mount(async (...args) => {calls.push(plain(args)); return summary({collection: 433})});
  try {
    await view.settle(); assert.doesNotMatch(view.text(), /累计数据概览内容/); assert.equal(calls.length, 1);
    view.click('展开数据概览'); await view.settle(); assert.match(view.text(), /累计数据概览内容/); assert.equal(calls.length, 1);
    assert.equal(view.button('收起数据概览').props['aria-expanded'], true);
    view.date('2026-09-01'); await view.settle(); assert.match(view.text(), /累计数据概览内容/);
    view.click('刷新报表'); await view.settle(); assert.match(view.text(), /累计数据概览内容/);
    view.click('分裂号审查'); await view.settle(); assert.match(view.text(), /累计数据概览内容/);
    view.click('工作统计'); await view.settle(); assert.match(view.text(), /累计数据概览内容/);
    view.click('收起数据概览'); await view.settle(); assert.doesNotMatch(view.text(), /累计数据概览内容/);
  } finally {view.dispose()}
});

test('the merged sidebar has one report entry immediately above history and no standalone data entry', () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  const parsed = ts.createSourceFile('formal-workbench.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declaration = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'Rail');
  assert.ok(declaration);
  const code = ts.transpileModule(declaration.getText(parsed), {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  const jsx = (type, props) => ({type, props: props || {}}), module = {exports: {}};
  const icons = Object.fromEntries([...declaration.getText(parsed).matchAll(/<(\w+) size=/g)].map(match => [match[1], match[1]]));
  runInNewContext(code, {module, exports: module.exports, UnreadBadge: 'UnreadBadge', ...icons, require(name) {assert.equal(name, 'react/jsx-runtime'); return {jsx, jsxs: jsx}}});
  const all = [], visit = node => {if(Array.isArray(node)) return node.forEach(visit); if(!node || typeof node !== 'object') return; all.push(node); visit(node.props?.children)};
  visit(module.exports.Rail({mode: 'reports'}));
  const links = all.filter(node => node.type === 'a'), routes = links.map(node => node.props['data-nav']);
  assert.equal(routes.includes('data'), false); assert.equal(routes.filter(route => route === 'reports').length, 1);
  assert.equal(routes.indexOf('reports') + 1, routes.indexOf('history'));
  assert.equal(links.find(node => node.props['data-nav'] === 'reports').props['aria-current'], 'page');
  assert.equal(links.filter(node => node.props['aria-current'] === 'page').length, 1);
  assert.doesNotMatch(source, /mode === "data"|mode: "data"/);
  assert.match(source, /<ReportsWorkspace snapshot=\{core\.snapshot\} snapshotError=\{core\.error\} snapshotLoading=\{core\.loading\} refreshSnapshot=\{core\.refresh\}/);
});

test('legacy data URLs replace their history entry with the single report page on initial load and navigation', () => {
  const source = ts.transpileModule(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
  }).outputText;
  for(const initial of ['#/data', '#/data/', '#/reports']) {
    const module = {exports: {}}, effects = [], handlers = new Map(), changes = [], states = [];
    const window = {location: {hash: initial}, history: {state: {retained: true}, replaceState(state, title, hash) {changes.push({state, title, hash}); window.location.hash = hash}},
      addEventListener(name, listener) {handlers.set(name, listener)}, removeEventListener(name) {handlers.delete(name)}};
    const jsx = (type, props) => ({type, props});
    runInNewContext(source, {module, exports: module.exports, window, require(name) {
      if(name === 'react') return {useState(initial) {return [typeof initial === 'function' ? initial() : initial, value => states.push(value)]}, useEffect(effect) {effects.push(effect)}};
      if(name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if(name === './auth-gate') return {AuthGate: 'AuthGate'};
      if(name === './formal-workbench') return {FormalWorkbench: 'FormalWorkbench'};
      throw Error(name);
    }});
    const tree = module.exports.default(); assert.equal(tree.props.children.props.mode, 'reports');
    const cleanup = effects[0](); assert.equal(window.location.hash, '#/reports'); assert.equal(states.at(-1), 'reports');
    assert.equal(changes.length, initial === '#/reports' ? 0 : 1);
    window.location.hash = '#/history'; handlers.get('hashchange')(); assert.equal(states.at(-1), 'history');
    window.location.hash = '#/data'; handlers.get('hashchange')(); assert.equal(states.at(-1), 'reports'); assert.equal(window.location.hash, '#/reports');
    assert.equal(changes.at(-1).state, window.history.state); assert.equal(changes.at(-1).title, '');
    cleanup(); assert.equal(handlers.has('hashchange'), false);
  }
});

test('the fifth card uses only confirmed receipts and preserves loading, failure and legacy-Core unknown states', async () => {
  const pending = deferred(), calls = []; let reads = 0;
  const view = mount((...args) => {
    calls.push(plain(args));
    if(!args[3]?.summaryOnly) return Promise.resolve(report([], {totals: counts({posting: 123, confirmed_posting: 7})}));
    if(++reads === 1) return pending.promise;
    if(reads === 2) return Promise.reject(new Error('summary offline'));
    return Promise.resolve(summary({posting: 999, confirmed_posting: undefined}));
  });
  const check = value => {
    assert.deepEqual(Object.keys(view.cards()), ['collection', 'follow', 'split', 'added', 'posting']);
    assert.deepEqual(view.cards().posting, {label: '发帖数量', value});
    const card = view.all('div').find(node => node.props.className === 'report-card report-posting');
    assert.equal(card.props.role, 'group'); assert.match(card.props['aria-label'], /确认.*回执/); assert.match(card.props.title, /历史人工确认不计入/);
  };
  try {
    await view.settle(); check('读取中'); assert.equal(calls.length, 1);
    pending.resolve(summary({collection: 1234567890, follow: 1234567890, split: 1234567890, added: 1234567890, confirmed_posting: 7}));
    await view.settle(); check('7');
    assert.ok(Object.values(view.knownCards()).every(card => card.value === '1,234,567,890'));
    for(const card of view.all('div').filter(node => node.props.className?.startsWith('report-card '))) {
      const strong = card.props.children[1];
      assert.equal(strong.props.style['--report-value-length'], Math.max(textOf(strong).length, 6));
    }
    view.click('导出 CSV'); await view.settle(); check('7'); assert.equal(calls.length, 2);
    view.click('刷新报表'); await view.settle(); assert.match(view.text(), /summary offline/);
    view.click('刷新报表'); await view.settle(); check('—'); assert.ok(Object.values(view.knownCards()).every(card => card.value === '0'));
    assert.equal(calls.length, 4); assert.ok(calls.filter(call => call.length === 4).every(call => JSON.stringify(call[3]) === '{"summaryOnly":true}'));
  } finally {view.dispose()}
});

test('five-card grid keeps fixed tracks and full numbers, with desktop and narrow native geometry guards', () => {
  const css = readFileSync(new URL('../src/reports-workspace.css', import.meta.url), 'utf8');
  assert.match(css, /\.report-summary\s*\{[^}]*grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /grid-template-rows:\s*32px 44px 18px/);
  assert.match(css, /font-size:\s*min\(36px, calc\(150cqw \/ var\(--report-value-length, 6\)\)\)/);
  assert.match(css, /@media \(max-width: 900px\)\s*\{\s*\.report-summary/);
  const {assertCardAlignment} = createRequire(import.meta.url)('../../desktop/tests/work-report-summary-r6.integration.cjs');
  const make = columns => {
    const width = (1000 - (columns - 1) * 14) / columns;
    return {viewportWidth: 1100, summary: {left: 0, right: 1000, clientWidth: 1000, scrollWidth: 1000}, cards: Array.from({length: 5}, (_, index) => {
      const left = index % columns * (width + 14), top = Math.floor(index / columns) * 163;
      return {left, top, right: left + width, bottom: top + 149, width, height: 149, clientWidth: width - 2, scrollWidth: width - 2,
        label: {top: top + 20}, value: {top: top + 60, height: 44, clientWidth: width - 38, scrollWidth: width - 38}, unit: {top: top + 112}};
    })};
  };
  for(const columns of [5, 2, 1]) assertCardAlignment(make(columns), columns);
  for(const mutate of [value => value.cards.pop(), value => value.cards[1].top++, value => value.cards[1].height += 3,
    value => value.cards[1].width += 3, value => value.cards[1].value.top += 3, value => value.cards[4].value.scrollWidth += 3]) {
    const invalid = make(5); mutate(invalid);
    // One-pixel rounding is allowed; make top-only mutations exceed that tolerance.
    if(invalid.cards[1]?.top === 1) invalid.cards[1].top = 3;
    assert.throws(() => assertCardAlignment(invalid, 5), {name: 'AssertionError'});
  }
});
