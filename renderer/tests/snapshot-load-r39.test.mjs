import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { runInNewContext } from "node:vm";
import test from "node:test";
import ts from 'typescript';

const source = stripTypeScriptTypes(readFileSync(new URL("../src/core-client.ts", import.meta.url), "utf8"))
  .replace(/\bexport /g, "");
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const snapshot = (revision = 1) => ({
  revision, generated_at: "2026-09-17T01:00:00Z", counts: { pending_private: 345 },
  dedupe: { total: 240352 }, pending: { private: [], public: [] }, approved: { private: [], public: [] },
  history: { manual_rejections: [], collection_exclusions: [] }, windows: [{ id: "busy", locked: true }],
  sources: [], tasks: [{ id: "running-task", status: "running" }], campaigns: [], truncated: false,
});

function mount(read) {
  const timers = new Map(), delivered = [], errors = [], calls = [];
  let serial = 0;
  const api = runInNewContext(source + "\n({ startWorkbenchSnapshotPolling, getCollectorCoreClient })", {
    setTimeout(callback, delay) { timers.set(++serial, { callback, delay }); return serial; },
    clearTimeout(id) { timers.delete(id); },
  });
  const bridge = {
    request(path, options) { calls.push({ path, options }); return read(path, options); },
    secureSet: async () => true, secureGet: async () => null,
    secureDelete: async () => true, configureIntegrations: async () => ({}),
  };
  const controller = api.startWorkbenchSnapshotPolling({
    intervalMs: 5_000, immediate: false, limit: 2_000, historyLimit: 2_000,
    onSnapshot: value => delivered.push(value), onError: error => errors.push(error),
  }, bridge);
  return {
    controller, calls, delivered, errors, timers, client: api.getCollectorCoreClient(bridge),
    delay() { assert.equal(timers.size, 1); return [...timers.values()][0].delay; },
    fire() { assert.equal(timers.size, 1); const [id, timer] = [...timers][0]; timers.delete(id); timer.callback(); },
  };
}

test("automatic failures back off, retain running task/lock/counts, then reset after recovery", async () => {
  let fail = false, revision = 1;
  const view = mount(async () => { if (fail) throw new Error("本机服务响应超时"); return snapshot(revision); });
  try {
    await view.controller.refresh();
    const saved = view.controller.current;
    assert.equal(view.delay(), 5_000);
    fail = true;
    for (const expectedDelay of [10_000, 20_000, 30_000, 30_000]) {
      view.fire(); await flush();
      assert.equal(view.delay(), expectedDelay);
      assert.equal(view.controller.current, saved);
      assert.equal(saved.tasks[0].status, "running");
      assert.equal(saved.windows[0].locked, true);
      assert.equal(saved.counts.pending_private, 345);
      assert.equal(saved.dedupe.total, 240352);
      assert.equal(view.delivered.length, 1);
    }
    assert.equal(view.calls.length, 5);
    assert.equal(view.errors.length, 4);
    fail = false; revision = 2;
    view.fire(); await flush();
    assert.equal(view.controller.current.revision, 2);
    assert.equal(view.delay(), 5_000);
    assert.equal(view.delivered.length, 2);
  } finally { view.controller.stop(); }
  assert.equal(view.timers.size, 0);
});

test("manual refresh cancels its old schedule and repeated clicks share one request", async () => {
  const pending = deferred();
  const view = mount(() => pending.promise);
  try {
    assert.equal(view.delay(), 5_000);
    const first = view.controller.refresh();
    assert.equal(view.timers.size, 0, "old periodic timer cannot fire just after manual success");
    for (let i = 0; i < 30; i++) assert.equal(view.controller.refresh(), first);
    assert.equal(view.calls.length, 1);
    pending.resolve(snapshot());
    await first;
    assert.equal(view.delay(), 5_000);
    assert.equal(view.delivered.length, 1);
  } finally { view.controller.stop(); }
});

test("manual refresh can retry immediately during automatic backoff", async () => {
  let fails = true;
  const view = mount(async () => { if (fails) throw new Error("timeout"); return snapshot(); });
  try {
    await assert.rejects(view.controller.refresh());
    assert.equal(view.delay(), 10_000);
    fails = false;
    await view.controller.refresh();
    assert.equal(view.calls.length, 2);
    assert.equal(view.delay(), 5_000);
  } finally { view.controller.stop(); }
});

test("a stopped poller does not publish late failures or schedule another read", async () => {
  const pending = deferred();
  const view = mount(() => pending.promise);
  const first = view.controller.refresh();
  const rejected = assert.rejects(first);
  view.controller.stop();
  pending.reject(new Error("late failure"));
  await rejected;
  await flush();
  assert.equal(view.calls.length, 1);
  assert.equal(view.timers.size, 0);
  assert.equal(view.errors.length, 0);
  assert.equal(view.delivered.length, 0);
});

test("snapshot retry never replays a timed-out action command", async () => {
  const view = mount(async (path) => {
    if (path === "/api/workbench/commands") throw new Error("response lost after action accepted");
    return snapshot();
  });
  try {
    await view.controller.refresh();
    await assert.rejects(view.client.command("action_campaign_start", { visibility: "private" }));
    view.fire(); await flush();
    assert.equal(view.calls.filter(call => call.path === "/api/workbench/commands").length, 1);
    assert.equal(view.calls.filter(call => call.path.startsWith("/api/workbench/snapshot?")).length, 2);
    assert.equal(view.controller.current.tasks[0].status, "running");
  } finally { view.controller.stop(); }
});

test("inventory-only degradation recovers a failed poll and publishes fresh business data and locks", async () => {
  let stage = 'failed';
  const view = mount(async () => {
    if (stage === 'failed') throw new Error('previous full snapshot timeout');
    return { ...snapshot(stage === 'degraded' ? 7 : 8),
      counts: { pending_private: 123 },
      windows: [{ id: 'busy', locked: true, lock_entity_id: 'current-task', ready: stage === 'ready' }],
      connection: { connected: stage === 'ready', inventory_stale: stage === 'degraded' },
    };
  });
  try {
    await assert.rejects(view.controller.refresh());
    stage = 'degraded';
    const recovered = await view.controller.refresh();
    assert.equal(recovered.connection.inventory_stale, true);
    assert.equal(recovered.counts.pending_private, 123);
    assert.equal(recovered.windows[0].locked, true);
    assert.equal(recovered.windows[0].lock_entity_id, 'current-task');
    assert.equal(recovered.windows[0].ready, false);
    assert.equal(view.delay(), 5_000, 'fresh business snapshot ends full-read backoff');
    assert.equal(view.errors.length, 1);
    assert.equal(view.delivered.length, 1);
    stage = 'ready';
    await view.controller.refresh();
    assert.equal(view.controller.current.connection.inventory_stale, false);
    assert.equal(view.controller.current.windows[0].locked, true, 'inventory recovery cannot unlock a task');
  } finally { view.controller.stop(); }
});

test("inventory degradation never bypasses command revision fencing or replays a command", async () => {
  let reads = 0;
  const view = mount(async path => {
    if (path === '/api/workbench/commands') return { command: 'action_campaign_start', result: {}, snapshot_seq: 9 };
    return { ...snapshot(++reads === 1 ? 8 : 9), connection: { inventory_stale: true, connected: false } };
  });
  try {
    await view.client.command('action_campaign_start', { visibility: 'private' });
    const value = await view.controller.refresh();
    assert.equal(value.revision, 9);
    assert.equal(reads, 2);
    assert.equal(view.delivered.length, 1);
    assert.equal(view.calls.filter(call => call.path === '/api/workbench/commands').length, 1);
  } finally { view.controller.stop(); }
});

test('storage diagnosis distinguishes an unknown measurement from an observed full disk', () => {
  const exports = {};
  const code = ts.transpileModule(readFileSync(new URL('../src/storage-status.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  runInNewContext(code, { exports, require: () => ({}) });
  for (const value of [null, {}, { disk_probe_available: false, disk_free_bytes: 0 },
    { disk_probe_available: true, disk_free_bytes: NaN }])
    assert.match(exports.storageDiagnosis(value), /尚无法确认/);
  assert.match(exports.storageDiagnosis({ disk_probe_available: true, disk_free_bytes: 0, low_space_warning: true }), /0 B，空间不足/);
  assert.match(exports.storageDiagnosis({ disk_probe_available: true, disk_free_bytes: 20 * 1024 ** 3, low_space_warning: false }), /20.00 GB/);
});

test('disk diagnostics use an independent read while the full snapshot is still held', async () => {
  const pending = deferred();
  const view = mount(path => path === '/api/workbench/storage'
    ? Promise.resolve({ disk_probe_available: true, disk_free_bytes: 0, low_space_warning: true }) : pending.promise);
  const full = view.controller.refresh();
  try {
    const status = await view.client.storageStatus();
    assert.equal(status.disk_free_bytes, 0);
    assert.equal(view.delivered.length, 0, 'held full snapshot is still outstanding');
    assert.equal(view.calls.length, 2);
    assert.equal(view.calls[1].path, '/api/workbench/storage');
    assert.equal(view.calls[1].options, undefined, 'diagnosis must not issue a mutation');
  } finally { pending.resolve(snapshot()); await full; view.controller.stop(); }
});
