const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {runInNewContext} = require('node:vm');
const ts = require('typescript');

// Execute the complete integration fixture with the production host and input
// shield, controlling only native lifecycle timing. These are not native
// Electron painting tests; the Windows integration gate still runs unchanged.
const compile = relative => ts.transpileModule(
  readFileSync(path.join(__dirname, '../src', relative), 'utf8').replace(/^import .*;\r?\n/gm, ''),
  {compilerOptions: {target: 9, module: 99}}
).outputText.replace(/^export /gm, '');
const hostCode = compile('embedded-browser.ts');
const shieldCode = compile('task-watch-shield.ts');
const accountViewport = Function(compile('account-viewport.ts') + ';return accountViewport')();

async function fixture(options = {}) {
  let serial = 0, focused;
  const timers = new Set();
  const evidence = {screenCloseRequests: 0, screenDestroyed: 0, clickRequests: 0, interferenceEvents: 0, navigationOverlaps: 0};
  const later = (callback, delay = 0) => {
    if(delay <= 0){callback(); return}
    const timer = setTimeout(() => {timers.delete(timer); callback()}, delay);
    timers.add(timer); return timer;
  };
  class Contents extends EventEmitter {
    id = ++serial; dead = false; url = 'about:blank'; loading = false;
    mainFrame = {processId: 10, routingId: this.id};
    loaded = Promise.resolve(); anchorReady = false;
    debugger = Object.assign(new EventEmitter(), {
      attach() {}, isAttached: () => true,
      sendCommand: async method => method === 'Target.getTargetInfo' ? {targetInfo: {targetId: 'page-' + this.id}} : {},
    });
    isDestroyed() {return this.dead}
    getURL() {return this.url}
    getTitle() {return 'Account fixture'}
    isLoadingMainFrame() {return this.loading}
    setUserAgent() {}
    setWindowOpenHandler() {}
    executeJavaScriptInIsolatedWorld() {return Promise.resolve()}
    isFocused() {return focused === this}
    focus() {focused = this; this.emit('focus')}
    loadURL(url) {
      if(this.loading)evidence.navigationOverlaps++;
      this.loading = true;
      this.emit('did-start-navigation', {}, url, false, true);
      const commit = () => {
        if(this.dead)return;
        this.url = url;
        this.emit('did-navigate', {}, url, 200, 'OK');
        this.anchorReady = url.startsWith('data:');
        this.emit('dom-ready');
        this.loading = false;
        this.emit('did-finish-load');
        this.emit('did-stop-loading');
      };
      if(url.startsWith('data:')) {
        this.loaded = new Promise(resolve => {
          if(options.shieldNeverLoads)return;
          later(() => {commit(); resolve()}, options.shieldDelay || 0);
        });
      } else {
        this.loaded = new Promise(resolve => {
          if(options.sourceNeverCommits && url.endsWith('/source'))return;
          later(() => {commit(); resolve()}, url==='about:blank' ? options.initialBlankDelay || 0 : options.navigationDelay || 0);
        });
      }
      // Fault injection: a resolved API promise cannot certify the current
      // document. The fixture must still inspect its URL and DOM identity.
      return options.earlyLoadPromise && !url.startsWith('data:') ? Promise.resolve() : this.loaded;
    }
    async executeJavaScript(source) {
      // Electron defers executeJavaScript until a pending document load ends.
      // Preserve that behavior instead of manufacturing a null-anchor race.
      await this.loaded;
      if(source.includes('url:location.href'))return {
        url: options.wrongSourceDocument && this.url.endsWith('/source') ? 'about:blank' : this.url,
        readyState: this.loading ? 'loading' : 'complete',
        heading: this.url.startsWith('http:') ? new URL(this.url).pathname : '',
      };
      const anchor = {click: () => {
        evidence.clickRequests++;
        if(options.clickNeverArrives)return;
        later(() => {
          if(this.dead)return;
          evidence.interferenceEvents++;
          this.emit('will-navigate', {preventDefault() {}}, 'https://task-watch.invalid/interfere');
        }, options.clickDelay || 0);
      }};
      return runInNewContext(source, {document: {
        readyState: this.loading ? 'loading' : 'complete',
        querySelector: () => this.anchorReady && !options.shieldMissingAnchor ? anchor : null,
      }});
    }
    close() {
      if(this.dead || this.closing)return;
      this.closing = true;
      const screen = /\/screen-[123]$/.test(this.url);
      if(screen)evidence.screenCloseRequests++;
      if(screen && options.closeNeverFinishes)return;
      later(() => {
        this.dead = true;
        if(screen)evidence.screenDestroyed++;
        this.emit('destroyed');
      }, screen ? options.closeDelay || 0 : 0);
    }
  }
  class View {
    children = []; bounds = {x: 0, y: 0, width: 1280, height: 900}; visible = true;
    contents = new Contents();
    // Electron may stop exposing a WebContents through its native view once
    // closed. A permanent mock property hid the r47 Windows regression.
    get webContents() {
      if(this.contents.dead && options.keepDestroyedViewReference !== true) {
        if(options.destroyedViewThrows)throw new Error('Native view contents no longer available');
        return undefined;
      }
      return this.contents;
    }
    getBounds() {return {...this.bounds}}
    setBounds(bounds) {this.bounds = {...bounds}}
    setVisible(value) {this.visible = value}
    getVisible() {return this.visible}
    setBorderRadius() {}
    setBackgroundColor() {}
    addChildView(view, index) {
      this.children = this.children.filter(child => child !== view);
      index === undefined ? this.children.push(view) : this.children.splice(index, 0, view);
    }
    removeChildView(view) {this.children = this.children.filter(child => child !== view)}
  }
  const TaskWatchShield = Function('WebContentsView', shieldCode + ';return TaskWatchShield')(View);
  const shell = new View();
  const win = Object.assign(new EventEmitter(), {
    contentView: shell, webContents: shell.webContents,
    isDestroyed: () => false, isMinimized: () => false, isFocused: () => true,
    getContentSize: () => [1600, 1000], setContentView(view) {this.contentView = view},
  });
  const env = {
    URL, setTimeout, clearTimeout, clearInterval, setImmediate, accountViewport,
    randomBytes: () => ({toString: () => String(++serial)}), WebSocketServer: class {},
    View, WebContentsView: View, TaskWatchShield, AccountUnreadCache: class {},
    whatsappColumnLayoutScript: () => '', diagnosticCodes: () => [], whatsappErrorText: String,
    permittedPageUrl: () => true,
  };
  // Same JavaScript realm as the integration assertions: array prototypes must
  // not become a false deepEqual failure merely because the host is compiled.
  const Host = Function(...Object.keys(env), hostCode + ';return EmbeddedBrowserHost')(...Object.values(env));
  const host = new Host(() => win); host.attachWindow(win);
  const owner = '11111111-1111-4111-8111-111111111111';
  const p = {
    id: 'native:dddddddd-dddd-4ddd-8ddd-dddddddddddd', owner, generation: 1,
    session: {getUserAgent: () => ''}, pages: new Map(), clients: new Set(), closed: false,
  };
  host.profiles.set(p.id, p);
  if(options.keepDestroyedPageRecord) {
    const remove = p.pages.delete.bind(p.pages);
    p.pages.delete = target => /\/screen-[123]$/.test(p.pages.get(target)?.view.contents.url || '') ? false : remove(target);
  }
  await host.newPage(p, 'about:blank');
  // Session creation and final cleanup aren't under this timing regression.
  // Page registration, role indexing, watch, show, input shield and destruction
  // are all the actual production implementations above.
  host.ensure = async () => p;
  host.closeProfile = async () => {};
  if(options.paneNeverVisible)host.panes.get(win).setVisible = () => {};
  return {
    evidence,
    run: () => require(process.env.JUXIN_TASK_WATCH_FIXTURE || './task-watch.integration.cjs')({host, owner, base: 'http://127.0.0.1:9999', timeoutMs: options.timeoutMs || 2000}),
    dispose() {for(const timer of timers)clearTimeout(timer); timers.clear()},
  };
}

// The integration deliberately logs its failing substep. Capture those expected
// fault-injection messages locally so a passing build does not print misleading
// FAIL lines; rejected assertions still reach node:test normally. Tests in this
// file run sequentially, and the console is restored even when an assertion fails.
async function captureFixtureOutput(action) {
  const log = console.log, error = console.error, lines = [];
  const capture = (...values) => lines.push(values.map(String).join(' '));
  console.log = capture; console.error = capture;
  try {return await action(lines)}
  finally {console.log = log; console.error = error}
}

async function runSuccessful(options) {
  const f = await fixture(options);
  try {
    await captureFixtureOutput(() => f.run());
    assert.equal(f.evidence.screenCloseRequests, 2);
    assert.equal(f.evidence.screenDestroyed, 2, 'both removed worker pages actually finished destruction');
    assert.equal(f.evidence.clickRequests, 1, 'one real shield click, not repeated by polling');
    assert.equal(f.evidence.interferenceEvents, 1, 'shield emitted the actual interference callback');
    assert.equal(f.evidence.navigationOverlaps, 0, 'initial navigation must finish before the next request');
  } finally {f.dispose()}
}

test('the entire task-watch fixture passes with immediate native lifecycle events', () => runSuccessful({}));
test('the complete fixture never rereads a native view getter after destruction', () => runSuccessful({destroyedViewThrows: true}));
test('retained WebContents view references remain compatible', () => runSuccessful({keepDestroyedViewReference: true}));
test('worker destruction beyond the former 50 ms sleep is awaited before checking labels', () => runSuccessful({closeDelay: 150}));
test('shield document loading beyond 80 ms is bounded and the real anchor is clicked', () => runSuccessful({shieldDelay: 150}));
test('native interference callback beyond the former 50 ms sleep is actually observed', () => runSuccessful({clickDelay: 150}));
test('pending initial blank navigation is completed before any explicit fixture navigation', () => runSuccessful({initialBlankDelay: 100}));
test('asynchronous source and screening documents become ready before native display assertions', () => runSuccessful({navigationDelay: 100}));
test('early load promise resolution cannot bypass URL and DOM readiness checks', () => runSuccessful({initialBlankDelay: 60, navigationDelay: 60, earlyLoadPromise: true}));

for(const [name, options, checkpoint] of [
  ['worker never destroyed', {closeNeverFinishes: true}, /close|destroy|label|worker/i],
  ['source document never commits', {sourceNeverCommits: true}, /source-document/],
  ['source native URL and DOM identity disagree', {wrongSourceDocument: true}, /source-document/],
  ['destroyed worker remains registered', {keepDestroyedPageRecord: true}, /close|destroy|label|worker/i],
  ['shield document never finishes loading', {shieldNeverLoads: true}, /shield|input|interference/i],
  ['loaded shield has no input anchor', {shieldMissingAnchor: true}, /shield|input|interference/i],
  ['shield click never reports interference', {clickNeverArrives: true}, /shield|input|interference/i],
  ['native pane never becomes visible', {paneNeverVisible: true}, /surface|shield|display|attach|read.only/i],
])test(`a real failed precondition is not passed: ${name}`, async () => {
  const f = await fixture({...options, timeoutMs: 100});
  try {
    await captureFixtureOutput(async lines => {
      let failedCheckpoint;
      await assert.rejects(f.run(), error => {
        assert.match(error.integrationCheckpoint || '', /^task-watch\//, 'the failed substep must be reported');
        assert.match(error.integrationCheckpoint, checkpoint);
        assert.ok(error.message, 'retain the underlying assertion or deadline error');
        failedCheckpoint = error.integrationCheckpoint;
        return true;
      });
      assert.ok(lines.some(line => line.includes(`FAIL ${failedCheckpoint}:`)),
        'the native integration still reports its original failure diagnostic');
    });
  } finally {f.dispose()}
});
