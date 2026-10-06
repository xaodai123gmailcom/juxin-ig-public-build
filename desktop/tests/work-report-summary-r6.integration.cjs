/* Real Electron/React report gate. Local synthetic data only; no live accounts.
   Standalone: npx electron desktop/tests/work-report-summary-r6.integration.cjs
   Shared Windows gate: require('./work-report-summary-r6.integration.cjs')({win,host}) */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {createFixtureLifecycle,closeFixtureServer,stableGeometry} = require('./fixture-lifecycle.cjs');

// Bundle the production App and reports rather than reproducing their markup.
const fixtureSource = String.raw`
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './renderer/src/App';
import './renderer/src/global.css';
import './renderer/src/workbench-polish-r55.css';
import './renderer/src/workbench-density.css';
import './renderer/src/workbench-r93.css';
const fixture = {requests: [] as any[], downloads: [] as any[], holdSummary: true, holdExport: false,
  summaryValues: {collection: 433, follow: 0, split: 2, added: 0, confirmed_posting: 7},
  failExport: false, holdStudio: false, releaseStudio: null as (() => void) | null, releaseSummary: null as (() => void) | null, releaseExport: null as (() => void) | null};
Object.assign(window, {reportFixture: fixture});
const blobs = new Map<string, Blob>();
const createURL = URL.createObjectURL.bind(URL), revokeURL = URL.revokeObjectURL.bind(URL);
URL.createObjectURL = blob => {const url = createURL(blob); blobs.set(url, blob as Blob); return url};
URL.revokeObjectURL = url => {blobs.delete(url); revokeURL(url)};
const click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function() {
  if (!this.download) {click.call(this); return}
  const blob = blobs.get(this.href); if (!blob) throw Error('Missing CSV Blob');
  const item = {filename: this.download, bytes: [] as number[], text: '', ready: false};
  fixture.downloads.push(item);
  void Promise.all([blob.text(), blob.arrayBuffer()]).then(([text, bytes]) => {
    item.text = text; item.bytes = Array.from(new Uint8Array(bytes).slice(0, 3)); item.ready = true;
  });
};
const zero = {follow: 0, greet: 0, split: 0, added: 0, posting: 0, collection: 0, check: 0, nurture: 0, approved: 0, confirmed_posting: 0};
const snapshot = {platform: 'instagram', revision: 1, generated_at: new Date().toISOString(),
  counts: {pending_public: 0, pending_private: 0, total_collected: 10000, total_public: 8000, total_private: 2000}, dedupe: {total: 9000}, pending: {public: [], private: []}, approved: {public: [], private: []},
  history: {manual_rejections: [], collection_exclusions: []}, windows: [], sources: [], tasks: [], campaigns: [], split_candidates: [],
  truncated: false, connection: {connected: true, provider: 'native'}};
(window as any).collectorCore = {secureGet: async () => 'fixture-token', secureSet: async () => true,
  secureDelete: async () => true, configureIntegrations: async () => ({restarted: false}),
  async request(path: string, options: any = {}) {
    const body = options.body || {};
    if (path === '/api/session/resume') return {session_token: 'fixture-token', user: {id: 'fixture-owner', username: 'fixture'}};
    if (path.startsWith('/api/workbench/snapshot')) return structuredClone(snapshot);
    if (path === '/api/accounts/unread') return {windows: {}, total: 0, capped: false, unknown: 0};
    if (path === '/api/accounts/snapshot') return {plans: [], windows: [], locks: {}, events: [], platforms: [], unavailable_platforms: [], last_dm: {}, last_following: {}};
    if (path === '/api/workbench/live-status') return {revision: 1, generated_at: new Date().toISOString(), tasks: []};
    fixture.requests.push({path, body: structuredClone(body)});
    if (path === '/api/studio/snapshot') {
      if (fixture.holdStudio) {fixture.holdStudio = false; await new Promise<void>(resolve => fixture.releaseStudio = resolve); fixture.releaseStudio = null}
      return {jobs: [], assets: [], templates: {}, active_ids: [], credentials: {pexels_configured: false, ai_configured: false},
        monitor_totals: {added: 17, repeated: 3}, totals: [{kind: 'nurture', status: 'completed', count: 11}, {kind: 'posting', status: 'completed', count: 7}, {kind: 'nurture', status: 'failed', count: 2}],
        daily: [{day: '2026-03-08', kind: 'nurture', count: 11}, {day: '2026-03-08', kind: 'posting', count: 7}]};
    }
    if (path === '/api/reports/query') {
      if (body.platform !== 'instagram' || body.kind !== 'activity') throw Error('Unexpected report scope');
      if (body.summary_only === true) {
        if (fixture.holdSummary) {fixture.holdSummary = false; await new Promise<void>(resolve => fixture.releaseSummary = resolve); fixture.releaseSummary = null}
        return {start: body.start, end: body.end, totals: {...fixture.summaryValues}};
      }
      if ('summary_only' in body) throw Error('Full export must omit summary_only');
      if (fixture.holdExport) {fixture.holdExport = false; await new Promise<void>(resolve => fixture.releaseExport = resolve); fixture.releaseExport = null}
      if (fixture.failExport) {fixture.failExport = false; throw Error('Synthetic CSV failure')}
      const rows = Array.from({length: 2501}, (_, index) => ({profile_id: 'window-' + index,
        window_name: index === 0 ? '=SUM(1,2)' : '窗口 ' + index, username: 'operator.' + index, instagram_user_id: 'ig-' + index,
        ...zero, collection: 1, split: index < 2 ? 1 : 0}));
      return {start: body.start, end: body.end, totals: {...zero, collection: 2501, split: 2}, rows, unattributed: 0};
    }
    if (path === '/api/reports/split-review' || path === '/api/reports/private-follow-review')
      return {items: [], total: 0, offset: 0, limit: 100, has_more: false, daily_counts: {}, decision_counts: {passed: 0, failed: 0}};
    throw Error('Fixture has no optional endpoint: ' + path);
  }};
createRoot(document.getElementById('root')!).render(<App/>);
`;

async function buildFixture() {
  return require('esbuild').build({stdin: {contents: fixtureSource, resolveDir: path.resolve(__dirname, '../..'), sourcefile: 'work-report-summary-r6.tsx', loader: 'tsx'},
    bundle: true, write: false, outfile: 'fixture.js', format: 'iife', jsx: 'automatic', define: {'process.env.NODE_ENV': '"production"'}, logLevel: 'silent'});
}
const stateScript = `(() => ({
  cards: [...document.querySelectorAll('.report-card')].map(node => ({label: node.querySelector('span')?.textContent, value: node.querySelector('strong')?.textContent})),
  details: document.body.textContent.includes('窗口与账号明细'), tables: document.querySelectorAll('.report-metrics-table,.report-table-scroll').length,
  searches: document.querySelectorAll('.report-search,[aria-label="搜索报表"]').length,
  date: document.querySelector('.report-toolbar input[type=date]')?.value,
  controls: [...document.querySelectorAll('.report-toolbar button')].map(node => node.textContent.trim()),
  tabs: [...document.querySelectorAll('.report-workspace-tabs button')].map(node => node.textContent.trim())
}))()`;
function assertSummaryState(state) {
  assert.deepEqual(state.cards, [{label: '采集总数', value: '433'}, {label: '私密点关注成功数', value: '0'}, {label: '分裂数量', value: '2'}, {label: '新增数量', value: '0'}, {label: '发帖数量', value: '7'}]);
  assert.equal(state.details, false); assert.equal(state.tables, 0); assert.equal(state.searches, 0);
  assert.match(state.date, /^\d{4}-\d{2}-\d{2}$/);
  assert.deepEqual(state.controls, ['当日', '当周', '当月', '回到今天', '刷新报表', '导出 CSV']);
  assert.deepEqual(state.tabs, ['工作统计', '分裂号审查', '私密关注检查']);
}
const cardLayoutScript = `(() => {
  const rect = node => {const value=node.getBoundingClientRect();return {left:value.left,top:value.top,right:value.right,bottom:value.bottom,width:value.width,height:value.height}};
  const summary=document.querySelector('.report-summary');
  return {viewportWidth:innerWidth,viewportHeight:innerHeight, summary:{...rect(summary),clientWidth:summary.clientWidth,scrollWidth:summary.scrollWidth},
    cards:[...summary.querySelectorAll('.report-card')].map(node=>({
      ...rect(node), clientWidth:node.clientWidth, scrollWidth:node.scrollWidth,
      label:rect(node.querySelector('span')), value:{...rect(node.querySelector('strong')),clientWidth:node.querySelector('strong').clientWidth,scrollWidth:node.querySelector('strong').scrollWidth}, unit:rect(node.querySelector('small'))
    }))};
})()`;
// OS resize and container-query font layout can settle on different frames.
// Require stable geometry, not a successful assertion: stable overflow still fails.
async function stableReportLayout(evaluate,width,options={}) {
  try{return await stableGeometry(evaluate,cardLayoutScript,width,{label:'Report resize geometry',widthKey:'viewportWidth',heightKey:'viewportHeight',...options})}
  catch(error){if(error.code==='FIXTURE_GEOMETRY')error.message='Report resize geometry did not stabilize: '+JSON.stringify({width,last:error.lastGeometry});throw error}
}
function assertCardAlignment(layout, columns = 5) {
  const near = (actual, expected, message) => assert.ok(Math.abs(actual - expected) <= 1, message + ': ' + actual + ' vs ' + expected);
  const cards = layout.cards; assert.equal(cards.length, 5);
  assert.ok(layout.summary.scrollWidth <= layout.summary.clientWidth + 1, 'summary has no horizontal overflow');
  const first = cards[0];
  for (const [index, card] of cards.entries()) {
    near(card.width, first.width, 'equal card widths'); near(card.height, first.height, 'equal card heights');
    assert.ok(card.scrollWidth <= card.clientWidth + 1, 'card text never overflows');
    assert.ok(card.value.scrollWidth <= card.value.clientWidth + 1, 'complete number fits without clipping');
    assert.ok(card.left >= layout.summary.left - 1 && card.right <= layout.summary.right + 1, 'card stays within report width');
    const rowFirst = cards[Math.floor(index / columns) * columns];
    near(card.top, rowFirst.top, 'equal tops within each row');
    near(card.label.top, rowFirst.label.top, 'aligned label track');
    near(card.value.top, rowFirst.value.top, 'aligned number track');
    near(card.value.height, first.value.height, 'equal number line boxes');
    near(card.unit.top, rowFirst.unit.top, 'aligned unit/status track');
    if (index >= columns) assert.ok(card.top > cards[index - columns].bottom, 'responsive rows have consistent positive spacing');
    if (index % columns) assert.ok(card.left > cards[index - 1].right, 'columns have positive gaps');
    if (index % columns > 1) near(card.left - cards[index - 1].right, cards[1].left - first.right, 'equal column gaps');
  }
}
async function run({win, host}) {
  let built;
  const server = http.createServer((request, response) => {
    if (request.url === '/war-wolf.svg') {response.setHeader('content-type', 'image/svg+xml'); response.end(fs.readFileSync(path.resolve(__dirname, '../../renderer/public/war-wolf.svg'))); return}
    const suffix = request.url === '/fixture.js' ? '.js' : request.url === '/fixture.css' ? '.css' : null;
    response.setHeader('content-type', suffix === '.js' ? 'text/javascript; charset=utf-8' : suffix === '.css' ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
    response.end(suffix ? built.outputFiles.find(file => file.path.endsWith(suffix)).contents : '<meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>');
  });
  const lifecycle=createFixtureLifecycle(win,{label:'R6 work report',entryMode:host?'embedded':'standalone',sourceFile:__filename});
  const evaluate=lifecycle.read,wait=lifecycle.wait;
  let primaryError;
  const button = label => `[...document.querySelectorAll('button')].find(node => node.textContent.trim()===${JSON.stringify(label)})`;
  const click = async label => {await wait(`!!${button(label)}&&!${button(label)}.disabled`, 'enabled ' + label); await evaluate(`${button(label)}.click();true`)};
  const settled = () => lifecycle.semantic('#root');
  const ready = () => wait(`document.querySelector('.report-summary')?.getAttribute('aria-busy')==='false'&&document.querySelector('.report-collection strong')?.textContent==='433'`, 'five real report totals rendered');
  const queryList = `reportFixture.requests.filter(item=>item.path==='/api/reports/query')`;
  const fullList = `${queryList}.filter(item=>item.body.summary_only!==true)`;
  const date = async value => {
    await evaluate(`(()=>{const input=document.querySelector('.report-toolbar input[type=date]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  };
  const originalSize = win.getContentSize(), originalMinimum = win.getMinimumSize();
  try {
    built=await lifecycle.phase('bundle production fixture',buildFixture,{timeoutMs:30000});
    await lifecycle.phase('listen on loopback',()=>new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)}));host?.hide();
    win.setMinimumSize(0, 0); win.setContentSize(1598, Math.max(originalSize[1], 960));
    await lifecycle.phase('load fixture route',()=>win.loadURL('http://127.0.0.1:' + server.address().port + '/#/data'),{timeoutMs:30000});
    await lifecycle.visible('R6 merged work report');
    await wait("location.hash==='#/reports'&&document.querySelector('.formal-header h1')?.textContent==='报表'", 'legacy data bookmark replaced by merged report route');
    assert.equal(await evaluate("document.querySelector('.formal-header h1').textContent"), '报表');
    const navigation = await evaluate("[...document.querySelectorAll('.formal-nav a')].map(node=>node.dataset.nav)");
    assert.equal(navigation.includes('data'), false); assert.equal(navigation.filter(route => route === 'reports').length, 1);
    assert.equal(navigation.indexOf('reports') + 1, navigation.indexOf('history'), 'merged reports sit immediately above history');
    assert.equal(await evaluate("reportFixture.requests.filter(item=>item.path==='/api/studio/snapshot').length"), 0, 'collapsed data overview is not fetched');
    await wait('typeof reportFixture.releaseSummary===\'function\'', 'initial summary request held');
    assert.equal(await evaluate(`${fullList}.length`), 0, 'first load does not fetch detail rows');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.report-card strong')].map(node=>node.textContent)"), ['读取中', '读取中', '读取中', '读取中', '读取中']);
    assert.equal(await evaluate(`${button('导出 CSV')}.disabled`), true);
    await evaluate('reportFixture.releaseSummary();true'); await ready();
    assertSummaryState(await evaluate(stateScript));
    assert.match(await evaluate("document.querySelector('.report-posting').getAttribute('aria-label')"), /确认.*回执/);
    assert.match(await evaluate("document.querySelector('.report-posting').title"), /确认.*回执/);
    assert.equal(await evaluate(`${queryList}[0].body.summary_only`), true);
    assert.equal(await evaluate('document.querySelectorAll(".formal-content table").length'), 0);
    await settled();
    assertCardAlignment(await evaluate(cardLayoutScript), 5);
    const image = await lifecycle.capture('R6 report summary',{width:1598,height:Math.max(originalSize[1],960),captureTimeoutMs:10000});
    fs.mkdirSync(path.resolve('installer-output'), {recursive: true});
    fs.writeFileSync(path.resolve('installer-output/r6-work-report-summary.png'), image.toPNG());

    // Auxiliary lifetime data loads only when opened, without holding up the report.
    await evaluate('reportFixture.holdStudio=true;true'); await click('展开数据概览');
    await wait("typeof reportFixture.releaseStudio==='function'", 'lazy studio aggregates held');
    assert.equal(await evaluate("document.querySelector('.report-collection strong').textContent"), '433');
    assert.equal(await evaluate(`${button('导出 CSV')}.disabled`), false);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('#report-data-overview dd')].slice(4).map(node=>node.textContent)"), ['读取中', '读取中', '读取中', '读取中']);
    await evaluate('reportFixture.releaseStudio();true');
    await wait("document.querySelectorAll('#report-data-overview dd')[4]?.textContent==='17'", 'both legacy aggregate tables retained with loaded data');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('#report-data-overview dd')].map(node=>node.textContent)"), ['10,000', '8,000', '2,000', '9,000', '17', '3', '11', '7']);
    const detailText = await evaluate("document.querySelector('#report-data-overview').textContent");
    for (const label of ['总采集', '公开账号', '私密账号', '全局去重', '累计检查新增', '重复新增', '已完成养号轮次', '已确认发帖']) assert.ok(detailText.includes(label), label);
    assert.equal(await evaluate("document.querySelectorAll('#report-data-overview table').length"), 2);
    // Deliver a second native image with the preserved secondary data visible.
    await settled();
    const expandedImage = await lifecycle.capture('R6 expanded data',{width:1598,height:Math.max(originalSize[1],960),captureTimeoutMs:10000});
    fs.writeFileSync(path.resolve('installer-output/r6-work-report-data-overview.png'), expandedImage.toPNG());
    const studioReads = await evaluate("reportFixture.requests.filter(item=>item.path==='/api/studio/snapshot').length");
    await date('2026-10-01'); await ready();
    assert.equal(await evaluate("reportFixture.requests.filter(item=>item.path==='/api/studio/snapshot').length"), studioReads, 'period changes do not reset cumulative data');
    assert.equal(await evaluate("document.querySelector('#report-data-overview').textContent.includes('任务状态汇总')"), true);
    await click('收起数据概览');
    await wait("!document.querySelector('#report-data-overview')", 'secondary section collapsed');

    // Every desktop card retains the same tracks, even with large truthful counts.
    await evaluate('reportFixture.summaryValues={collection:1234567890,follow:1234567890,split:1234567890,added:1234567890,confirmed_posting:1234567890};true');
    await click('刷新报表');
    await wait("document.querySelector('.report-collection strong')?.textContent==='1,234,567,890'", 'large totals rendered without abbreviation');
    for (const [width, columns] of [[1598, 5], [1000, 5], [800, 2], [560, 1]]) {
      win.setContentSize(width, Math.max(originalSize[1], 960)); await settled();
      let layout;
      try {
        layout = await stableReportLayout(evaluate,width,{height:Math.max(originalSize[1],960),nativeSize:()=>win.getContentSize()}); assert.equal(layout.viewportWidth, width);
        assertCardAlignment(layout, columns);
      } catch(error) {
        try {
          fs.mkdirSync('installer-output',{recursive:true});
          fs.writeFileSync('installer-output/r6-report-layout-failure.json',JSON.stringify({width,columns,error:String(error),layout,latest:await evaluate(cardLayoutScript)},null,2));
          let captureTimer;
          const capture=await Promise.race([win.webContents.capturePage(),new Promise((_,reject)=>{captureTimer=setTimeout(()=>reject(Error('Report diagnostic capture timeout')),2000)})]).finally(()=>clearTimeout(captureTimer));
          fs.writeFileSync('installer-output/r6-report-layout-failure.png',capture.toPNG());
        } catch(diagnosticError) {console.error('Report layout diagnostic unavailable',String(diagnosticError));}
        throw error;
      }
      assert.deepEqual(await evaluate("[...document.querySelectorAll('.report-card strong')].map(node=>node.textContent)"), ['1,234,567,890', '1,234,567,890', '1,234,567,890', '1,234,567,890', '1,234,567,890']);
    }
    win.setContentSize(1598, Math.max(originalSize[1], 960));
    await evaluate('reportFixture.summaryValues={collection:433,follow:0,split:2,added:0,confirmed_posting:7};true');
    await click('刷新报表'); await ready(); await settled();

    // Calendar controls and refresh stay summary-only, with no cached count flash.
    await evaluate('reportFixture.holdSummary=true;true'); await date('2026-03-08');
    await wait('typeof reportFixture.releaseSummary===\'function\'', 'changed date summary held');
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.report-card strong')].map(node=>node.textContent)"), ['读取中', '读取中', '读取中', '读取中', '读取中']);
    await evaluate('reportFixture.releaseSummary();true'); await ready();
    for (const label of ['当周', '当月', '刷新报表', '回到今天']) {
      const before = await evaluate(`${queryList}.length`); await click(label);
      await wait(`${queryList}.length>${before}`, label + ' queried summary'); await ready();
      assert.equal(await evaluate(`${queryList}.at(-1).body.summary_only`), true);
    }
    assert.equal(await evaluate(`${fullList}.length`), 0);

    // Full export is lazy, complete, single-flight, and has a separate error/retry.
    await evaluate(`reportFixture.holdExport=true;${button('导出 CSV')}.click();${button('导出 CSV')}.click();true`);
    await wait('typeof reportFixture.releaseExport===\'function\'', 'one full export held');
    assert.equal(await evaluate(`${fullList}.length`), 1);
    assert.equal(await evaluate("document.querySelector('.report-summary').getAttribute('aria-busy')"), 'false');
    assert.equal(await evaluate(`${button('正在导出 CSV…')}.disabled`), true);
    await evaluate('reportFixture.releaseExport();true'); await wait('reportFixture.downloads[0]?.ready', 'complete CSV Blob produced');
    const first = await evaluate('reportFixture.downloads[0]');
    assert.deepEqual(first.bytes, [239, 187, 191]); assert.match(first.filename, /-day-工作报表\.csv$/);
    assert.equal(first.text.split('\r\n').length, 2504); assert.ok(first.text.includes('operator.2500'));
    assert.ok(first.text.includes('"\'=SUM(1,2)"')); assert.ok(first.text.includes('"分裂数量"')); assert.ok(first.text.includes('"公开打招呼"'));
    assertSummaryState(await evaluate(stateScript));
    await evaluate('reportFixture.failExport=true;true'); await click('导出 CSV');
    await wait("document.body.textContent.includes('CSV 导出失败')", 'separate export failure visible');
    assert.equal(await evaluate("document.querySelector('.report-collection strong').textContent"), '433');
    await click('导出 CSV'); await wait('reportFixture.downloads.length===2&&reportFixture.downloads[1].ready', 'export retry succeeds');
    assert.equal(await evaluate("document.body.textContent.includes('CSV 导出失败')"), false);

    // Date and navigation intent invalidate an already-started full export.
    for (const leave of ['date', '分裂号审查', '私密关注检查']) {
      const downloads = await evaluate('reportFixture.downloads.length');
      await evaluate('reportFixture.holdExport=true;true'); await click('导出 CSV');
      await wait('typeof reportFixture.releaseExport===\'function\'', 'obsolete export held');
      if (leave === 'date') {await date('2026-03-09'); await ready()}
      else {await click(leave); await wait(`!!document.querySelector('section[aria-label="${leave}"]')`, 'review tab rendered')}
      await evaluate('reportFixture.releaseExport();true','release obsolete export'); await wait('reportFixture.releaseExport===null','obsolete export response released'); await settled();
      assert.equal(await evaluate('reportFixture.downloads.length'), downloads, 'no late download after ' + leave);
      if (leave !== 'date') {await click('工作统计'); await ready()}
      assertSummaryState(await evaluate(stateScript));
    }
    console.log('PASS R6 native merged report: one report nav above history; /data alias; lazy cumulative data; five aligned cards (confirmed posting receipts); no details/search/table; date/week/month/refresh; summary-only queries; complete lazy CSV; duplicate/failure/stale-date/tab guards; screenshot installer-output/r6-work-report-summary.png');
  } catch(error) {primaryError=error;await lifecycle.diagnose(error)}
  finally {await lifecycle.finish(primaryError,[['restore dimensions',()=>{if(!win.isDestroyed()){win.setMinimumSize(...originalMinimum);win.setContentSize(...originalSize)}}],['HTTP server',()=>closeFixtureServer(server)]]);}
}
module.exports = run;
module.exports.buildFixture = buildFixture;
module.exports.assertSummaryState = assertSummaryState;
module.exports.assertCardAlignment = assertCardAlignment;

if (require.main === module) {
  const {app, BrowserWindow} = require('electron');
  const directory = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'juxin-report-r6-'));
  app.setPath('userData', directory);
  let window;
  const deadline = setTimeout(() => {console.error('R6 native report fixture timed out'); app.exit(1)}, 120000);
  app.whenReady().then(async () => {
    window = new BrowserWindow({show: true, width: 1598, height: 960, webPreferences: {sandbox: true}});
    await run({win: window}); clearTimeout(deadline); window.destroy(); app.exit(0);
  }).catch(error => {clearTimeout(deadline); console.error(error); app.exit(1)});
}

module.exports.closeFixtureServer=closeFixtureServer;

module.exports.stableReportLayout=stableReportLayout;
