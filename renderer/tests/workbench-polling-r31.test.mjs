import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { runInNewContext } from "node:vm";
import test from "node:test";
import { countWindowGroups, createViewSnapshotReader } from "../src/workbench-render-state.ts";

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let step = 0; step < 12; step++) await Promise.resolve(); };

function monitorSnapshot(status = "running") {
  const run = { id: "run", status, active_profile_ids: [], profile_ids: [], errors: [] };
  return { run, runs: { combined: run, following: null, dm: null }, accounts: [], dm_accounts: [], rounds: [], logs: [] };
}

function mountMonitor(client, initial = monitorSnapshot()) {
  const source = readFileSync(new URL("../src/formal-workbench.tsx", import.meta.url), "utf8");
  const start = source.indexOf("function FollowMonitorWorkspace(");
  const end = source.indexOf("  const counts = monitor?.counts", start);
  assert.ok(start >= 0 && end > start);
  // Execute the actual component's hooks and handlers before its JSX return.
  // This tests request lifecycle/command wiring, not a simulated React DOM/FPS.
  const code = source.slice(start, end) + "return { loadMonitor, startCheck, controlCheck, currentRun, anyRunning, paused, cancelling, starting, controlPending, awaitingStartSnapshot };\n}";
  const names = [...code.matchAll(/const \[(\w+),\s*\w+\]\s*=\s*useState/g)].map(match => match[1]);
  const writes = [], effects = [], timers = [], cleared = [], states = [], refs = [], memoized = [];
  let index = 0, refIndex = 0, memoIndex = 0, effectIndex = 0;
  const sameDeps = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const memo = (fn, deps) => { const slot = memoIndex++; if (!sameDeps(memoized[slot]?.deps, deps)) memoized[slot] = { value: fn(), deps }; return memoized[slot].value; };
  const render = runInNewContext(stripTypeScriptTypes(code) + "\n() => FollowMonitorWorkspace({ snapshot, disabled: () => false, refreshWindows: async () => {} });", {
    useState(value) {
      const slot = index++, name = names[slot];
      if (!(slot in states)) states[slot] = name === "monitor" ? initial : name === "selected" ? new Set(["free", "locked"])
        : typeof value === "function" ? value() : value;
      return [states[slot], next => { states[slot] = typeof next === "function" ? next(states[slot]) : next; writes.push({ name, value: states[slot] }); }];
    },
    useRef: value => { const slot = refIndex++; return refs[slot] ||= { current: value }; }, useMemo: memo, useCallback: (fn, deps) => memo(() => fn, deps),
    useEffect(effect, deps) { const slot = effectIndex++; if (!sameDeps(effects[slot]?.deps, deps)) { effects[slot]?.cleanup?.(); effects[slot] = { effect, deps, pending: true }; } }, createViewSnapshotReader,
    getCollectorCoreClient: () => client, usePlatformCore: () => client, useWorkbenchPlatform: () => ({platform: "instagram"}), collectionPlatform: () => "instagram",
    snapshot: { windows: [{ id: "free", name: "Free", opened: true, locked: false }, { id: "locked", name: "Locked", opened: true, locked: true }] },
    formatCount: value => String(value), windowSequenceLabel: () => "",
    window: { setInterval: callback => { timers.push(callback); return timers.length; }, clearInterval: id => cleared.push(id) },
  });
  const view = { writes, effects, timers, cleared, state: name => states[names.indexOf(name)], rerender() {
    index = refIndex = memoIndex = effectIndex = 0;
    Object.assign(view, render());
    for (const effect of effects) if (effect.pending) { effect.pending = false; effect.cleanup = effect.effect(); }
    return view;
  }, dispose() { effects.forEach(effect => effect.cleanup?.()); } };
  return view.rerender();
}

test("actual monitor polling never overlaps slow requests, even across repeated timer ticks", async () => {
  const request = deferred(); let reads = 0;
  const view = mountMonitor({ followMonitorSnapshot: () => { reads++; return request.promise; } });
  try {
    await flush();
    assert.equal(reads, 1);
    assert.equal(view.timers.length, 1);
    for (let tick = 0; tick < 30; tick++) view.timers[0]();
    await flush();
    assert.equal(reads, 1);
    request.resolve(monitorSnapshot());
    await flush();
    assert.equal(view.writes.filter(row => row.name === "monitor").length, 1);
  } finally { view.dispose(); }
  assert.deepEqual(view.cleared, [1]);
});

test("actual pause handler fences the old running reply and then reads fresh paused state", async () => {
  const requests = [], calls = [];
  const view = mountMonitor({
    followMonitorSnapshot() { const request = deferred(); requests.push(request); return request.promise; },
    async controlFollowMonitor(id, action) { calls.push([id, action]); return { run_id: id, status: "paused" }; },
  });
  try {
    await flush();
    const pause = view.controlCheck("pause");
    await flush();
    assert.equal(requests.length, 1, "command refresh must wait for old transport, not run concurrently");
    requests[0].resolve(monitorSnapshot("running"));
    await flush();
    assert.equal(requests.length, 2);
    assert.equal(view.writes.filter(row => row.name === "monitor").length, 1, "only the authoritative ACK is applied; pre-pause read is fenced");
    requests[1].resolve(monitorSnapshot("paused"));
    await pause; await flush();
    assert.deepEqual(calls, [["run", "pause"]]);
    assert.deepEqual(view.writes.filter(row => row.name === "monitor").map(row => row.value.run.status), ["paused", "paused"]);
  } finally { view.dispose(); }
});

test("actual start handler sends only currently unlocked windows and fences its follow-up read", async () => {
  const old = deferred(), fresh = deferred(); let reads = 0; const starts = [];
  const view = mountMonitor({
    followMonitorSnapshot: () => (++reads === 1 ? old.promise : fresh.promise),
    async startFollowMonitor(ids, concurrency, kind) { starts.push({ ids: [...ids], concurrency, kind }); },
  }, { ...monitorSnapshot(), run: null, runs: {} });
  try {
    await flush();
    const start = view.startCheck(); await flush();
    old.resolve(monitorSnapshot("completed")); await flush();
    assert.equal(reads, 2);
    assert.equal(view.writes.filter(row => row.name === "monitor").length, 0);
    fresh.resolve(monitorSnapshot("running")); await start; await flush();
    assert.deepEqual(starts, [{ ids: ["free"], concurrency: 2, kind: "combined" }]);
  } finally { view.dispose(); }
});

test("control stays busy until ACK, then releases immediately while an old snapshot remains blocked", async () => {
  const requests = [], ack = deferred();
  const initial = monitorSnapshot();
  initial.run.active_profile_ids = ["free"];
  initial.run.processed = 17;
  initial.runs.following = { ...initial.run, id: "different-run" };
  const view = mountMonitor({
    followMonitorSnapshot() { const request = deferred(); requests.push(request); return request.promise; },
    controlFollowMonitor: () => ack.promise,
  }, initial);
  try {
    await flush();
    let returned = false;
    const command = view.controlCheck("pause").then(() => { returned = true; });
    await flush();
    assert.equal(view.state("controlPending"), true);
    assert.equal(returned, false);
    ack.resolve({ run_id: "run", status: "paused" });
    await command;
    assert.equal(view.state("controlPending"), false);
    assert.equal(requests.length, 1, "old transport is still blocked when command finishes");
    view.rerender();
    assert.equal(view.paused, true);
    assert.equal(view.anyRunning, true, "pause does not release occupied windows");
    assert.equal(view.currentRun.processed, 17);
    assert.equal(view.currentRun.active_profile_ids, initial.run.active_profile_ids);
    assert.equal(view.state("monitor").runs.following, initial.runs.following);
    requests[0].resolve(monitorSnapshot("running")); await flush();
    assert.equal(view.state("monitor").run.status, "paused", "late pre-command state cannot re-enable pause");
    requests[1].resolve(monitorSnapshot("paused")); await flush();
  } finally { view.dispose(); }
});

test("start ACK clears command busy but retains start and previous-run control protection until fresh state", async () => {
  const requests = [], ack = deferred(); let controls = 0;
  const view = mountMonitor({
    followMonitorSnapshot() { const request = deferred(); requests.push(request); return request.promise; },
    startFollowMonitor: () => ack.promise,
    async controlFollowMonitor() { controls++; },
  }, monitorSnapshot("completed"));
  try {
    await flush();
    const command = view.startCheck(); await flush();
    assert.equal(view.state("starting"), true);
    ack.resolve({ run_id: "new-run", status: "running", total: 1 }); await command;
    view.rerender();
    assert.equal(view.starting, false);
    assert.equal(view.awaitingStartSnapshot, true);
    assert.equal(view.anyRunning, true, "confirmed start cannot be submitted again while old read hangs");
    await view.controlCheck("cancel");
    assert.equal(controls, 0, "the previous run must not receive the new run's cancel action");
    requests[0].resolve(monitorSnapshot("completed")); await flush();
    assert.equal(view.state("awaitingStartSnapshot"), true);
    const current = monitorSnapshot(); current.run.id = "new-run";
    requests[1].resolve(current); await flush(); view.rerender();
    assert.equal(view.awaitingStartSnapshot, false);
    assert.equal(view.currentRun.id, "new-run");
  } finally { view.dispose(); }
});

test("delayed control ACK cannot resurrect a run that a read already confirms is terminal", async () => {
  const first = deferred(), next = deferred(), ack = deferred(); let reads = 0;
  const view = mountMonitor({
    followMonitorSnapshot: () => (++reads === 1 ? first.promise : next.promise),
    controlFollowMonitor: () => ack.promise,
  });
  try {
    await flush();
    const command = view.controlCheck("cancel");
    first.resolve(monitorSnapshot("completed")); await flush();
    ack.resolve({ run_id: "run", status: "cancelling" }); await command;
    assert.equal(view.state("monitor").run.status, "completed");
    assert.equal(view.state("monitor").runs.combined.status, "completed");
    next.resolve(monitorSnapshot("completed")); await flush();
  } finally { view.dispose(); }
});

test("failed command releases busy only after its reply and background success preserves its error", async () => {
  const requests = [], ack = deferred();
  const view = mountMonitor({
    followMonitorSnapshot() { const request = deferred(); requests.push(request); return request.promise; },
    controlFollowMonitor: () => ack.promise,
  });
  try {
    await flush();
    const command = view.controlCheck("pause"); await flush();
    assert.equal(view.state("controlPending"), true);
    ack.reject("pause rejected"); await command;
    assert.equal(view.state("controlPending"), false);
    assert.equal(view.state("monitorError"), "pause rejected");
    requests[0].resolve(monitorSnapshot()); await flush();
    requests[1].resolve(monitorSnapshot()); await flush();
    assert.equal(view.state("monitorError"), "pause rejected");
    assert.equal(view.state("monitorReadError"), null);
  } finally { view.dispose(); }
});

test("a command that returns after unmount neither writes state nor queues another snapshot", async () => {
  const request = deferred(), ack = deferred(); let reads = 0;
  const view = mountMonitor({
    followMonitorSnapshot: () => { reads++; return request.promise; },
    controlFollowMonitor: () => ack.promise,
  });
  await flush();
  const command = view.controlCheck("cancel");
  view.dispose(); const count = view.writes.length;
  ack.resolve({ run_id: "run", status: "cancelling" }); await command;
  request.resolve(monitorSnapshot("completed")); await flush();
  assert.equal(view.writes.length, count);
  assert.equal(reads, 1);
});

test("unmount suppresses late monitor values/errors and does not start a queued command refresh", async () => {
  for (const fail of [false, true]) {
    const request = deferred(); let reads = 0;
    const view = mountMonitor({
      followMonitorSnapshot: () => { reads++; return request.promise; },
      async controlFollowMonitor() { return { run_id: "run", status: "cancelling" }; },
    });
    await flush();
    const command = view.controlCheck("cancel"); await flush();
    view.dispose();
    const count = view.writes.length;
    if (fail) request.reject(new Error("late failure")); else request.resolve(monitorSnapshot("completed"));
    await command; await flush();
    assert.equal(view.writes.length, count);
    assert.equal(reads, 1);
    await view.loadMonitor();
    assert.equal(reads, 1);
  }
});

test("superseded fresh reads collapse to one newest read and ignore old errors", async () => {
  const requests = [], values = [], errors = [];
  const reader = createViewSnapshotReader({ read() { const item = deferred(); requests.push(item); return item.promise; }, onValue: value => values.push(value), onError: reason => errors.push(reason) });
  const old = reader.refresh(); await flush();
  const firstFresh = reader.refresh(true), newest = reader.refresh(true);
  assert.equal(reader.refresh(), newest);
  requests[0].reject(new Error("old error")); await flush();
  assert.equal(requests.length, 2);
  requests[1].resolve("current"); await Promise.all([old, firstFresh, newest]);
  assert.deepEqual(values, ["current"]); assert.deepEqual(errors, []);
  reader.dispose();
});

test("current read errors remain visible and do not prevent the next automatic retry", async () => {
  let attempts = 0; const values = [], errors = [];
  const reader = createViewSnapshotReader({
    async read() { if (++attempts === 1) throw new Error("unavailable"); return "recovered"; },
    onValue: value => values.push(value), onError: error => errors.push(error.message),
  });
  await reader.refresh(); await reader.refresh();
  assert.deepEqual(errors, ["unavailable"]); assert.deepEqual(values, ["recovered"]);
});

test("window group counts use one property read per window with exact normalized counts", () => {
  let propertyReads = 0;
  const windows = Array.from({ length: 1000 }, (_, index) => ({ get group() { propertyReads++; return `group-${index}`; } }));
  const counts = countWindowGroups(windows);
  assert.equal(propertyReads, 1000);
  assert.equal(counts.size, 1000);
  assert.equal(counts.get("group-500"), 1);
  propertyReads = 0;
  for (const group of counts.keys()) windows.filter(window => window.group === group).length;
  assert.equal(propertyReads, 1000000, "old per-option scans: operation count, not frame rate");
  assert.deepEqual([...countWindowGroups([{ group: " A " }, { group: "A" }, { group: "" }, {}, { group: "B" }])], [["A", 2], ["B", 1]]);
});

test("both window selectors use memoized counts and the same normalization for filtering", () => {
  const source = readFileSync(new URL("../src/formal-workbench.tsx", import.meta.url), "utf8");
  assert.equal(source.match(/const windowGroupCounts = useMemo\(\(\) => countWindowGroups\(windows\), \[windows\]\)/g)?.length, 2);
  assert.equal(source.match(/windowGroupCounts\.get\(group\) \|\| 0/g)?.length, 2);
  assert.equal(source.match(/String\(window\.group \|\| ""\)\.trim\(\) === groupFilter/g)?.length, 2);
  assert.doesNotMatch(source, /windows\.filter\(\(window\) => window\.group === group\)\.length/);
});
