import assert from "node:assert/strict";
import test from "node:test";
import { applyLiveStatusOverlay } from "../src/workbench-live-status.ts";

const time = (second) => `2026-09-15T12:00:${String(second).padStart(2, "0")}Z`;
function snapshot() {
  return { generated_at: time(20), revision: 10,
    tasks: [{ id: "task", status: "paused", updated_at: time(20), targets: [
      { id: "target", status: "recoverable", updated_at: time(20), current_window_id: null,
        mode_progress: { followers: { processed: 12, source_total: 30 } } },
    ] }],
    sources: [{ id: "target", status: "recoverable", processed: 12, total: 30, current_window_id: null }],
    windows: [{ id: "w1", locked: false, lock_entity_id: null }],
  };
}
function live(second = 21, revision = 11) {
  return { generated_at: time(second), revision, tasks: [{
    task: { id: "task", status: "running", updated_at: time(second), targets: [
      { id: "target", status: "running", updated_at: time(second), current_window_id: "w1",
        mode_progress: { followers: { processed: 13, source_total: 30 } } },
    ] },
    runtime: { profile_states: [{ profile_id: "w1", state: "working", current_target_id: "target" }] },
  }] };
}

test("an older live reply cannot restore running status or overwrite newer counters and locks", () => {
  const base = snapshot();
  assert.equal(applyLiveStatusOverlay(base, live(10, 9)), base);
  assert.equal(applyLiveStatusOverlay(base, live(10, 10)), base);
});

test("a fresh live reply updates task, source and window together without mutating the snapshot", () => {
  const base = snapshot(), original = structuredClone(base);
  const merged = applyLiveStatusOverlay(base, live());
  assert.equal(merged.tasks[0].status, "running");
  assert.equal(merged.sources[0].processed, 13);
  assert.equal(merged.windows[0].locked, true);
  assert.deepEqual(base, original);
});

test("a delayed reply cannot replace an already displayed newer live observation", () => {
  const merged = applyLiveStatusOverlay(snapshot(), live(30, 12));
  assert.equal(applyLiveStatusOverlay(merged, live(25, 11)), merged);
});

test("a freshly generated response with older task rows cannot roll back the durable state", () => {
  const newer = live(30, 11);
  newer.tasks[0].task.updated_at = time(10);
  newer.tasks[0].task.targets[0].updated_at = time(10);
  assert.equal(applyLiveStatusOverlay(snapshot(), newer).tasks[0].status, "paused");
});

test("a fresh cleared assignment removes the previous source window", () => {
  const base = snapshot();
  base.sources[0].current_window_id = "w1";
  const next = live();
  next.tasks[0].task.targets[0].current_window_id = null;
  next.tasks[0].runtime.profile_states = [];
  assert.equal(applyLiveStatusOverlay(base, next).sources[0].current_window_id, null);
});

test("invalid observation timestamps cannot resurrect a task absent from the current snapshot", () => {
  const base = snapshot();
  base.tasks = [];
  const next = live(); next.generated_at = "invalid";
  assert.equal(applyLiveStatusOverlay(base, next), base);
});

test("a task heartbeat retains untouched windows and does not invalidate unchanged window lists", () => {
  const base = snapshot();
  const unrelated = { id: "other", locked: false, opened: true };
  base.windows.push(unrelated);
  const first = applyLiveStatusOverlay(base, live());
  assert.notEqual(first.windows, base.windows);
  assert.notEqual(first.windows[0], base.windows[0]);
  assert.equal(first.windows[1], unrelated);
  const second = applyLiveStatusOverlay(first, live(22, 12));
  assert.equal(second.windows, first.windows);
  assert.equal(second.sources[0].processed, 13);
  assert.equal(second.tasks[0].updated_at, time(22));
});

test("every actual window lock or readiness change is still applied immediately", () => {
  const first = applyLiveStatusOverlay(snapshot(), live());
  const waiting = live(22, 12);
  waiting.tasks[0].runtime.profile_states[0].state = "waiting_network";
  const next = applyLiveStatusOverlay(first, waiting);
  assert.notEqual(next.windows, first.windows);
  assert.notEqual(next.windows[0], first.windows[0]);
  assert.equal(next.windows[0].ready, false);
  assert.equal(next.windows[0].locked, true);
  assert.equal(next.windows[0].lock_state, "waiting_network");
  assert.equal(next.windows[0].lock_entity_id, "task");
  assert.equal(first.windows[0].ready, true);
});

test("partial targets merge by ID without dropping historical or temporarily absent active targets", () => {
  const base = snapshot();
  const historical = { id: "old", status: "completed", updated_at: time(10) };
  const omitted = { id: "other-active", status: "running", current_window_id: "w2", updated_at: time(20) };
  base.tasks[0].targets.push(historical, omitted);
  base.tasks[0].targets_truncated = true;
  base.tasks[0].targets_total = 9000;
  const otherWindow = { id: "w2", locked: true, lock_entity_id: "task", lock_operation: "collection" };
  base.windows.push(otherWindow);
  const original = structuredClone(base);
  const next = live();
  next.tasks[0].task.targets_partial = true;
  next.tasks[0].task.targets_truncated = false;
  next.tasks[0].task.targets.push({ id: "new", status: "pending", updated_at: time(21) });
  const merged = applyLiveStatusOverlay(base, next);
  assert.deepEqual(merged.tasks[0].targets.map((target) => target.id), ["target", "old", "other-active", "new"]);
  assert.equal(merged.tasks[0].targets[1], historical);
  assert.equal(merged.tasks[0].targets[2], omitted);
  assert.equal(merged.tasks[0].targets_truncated, true);
  assert.equal(merged.tasks[0].targets_total, 9000);
  assert.notEqual(merged.tasks[0].targets_partial, true);
  assert.equal(merged.windows[1], otherWindow);
  assert.equal(merged.windows[1].locked, true);
  assert.deepEqual(base, original);
});

test("partial terminal updates and explicit unassignment reach both task and source rows", () => {
  const base = snapshot();
  base.tasks[0].targets[0].current_window_id = "w1";
  base.sources[0].current_window_id = "w1";
  const next = live();
  next.tasks[0].task.targets_partial = true;
  next.tasks[0].task.targets[0] = {
    id: "target", status: "completed", current_window_id: null, updated_at: time(21),
    mode_progress: { followers: { processed: 30, source_total: 30 } },
  };
  next.tasks[0].runtime.profile_states = [];
  const merged = applyLiveStatusOverlay(base, next);
  assert.equal(merged.tasks[0].targets[0].status, "completed");
  assert.equal(merged.tasks[0].targets[0].current_window_id, null);
  assert.equal(merged.sources[0].status, "completed");
  assert.equal(merged.sources[0].current_window_id, null);
  assert.equal(merged.sources[0].processed, 30);
});

test("one stale partial target cannot roll back its counters or block fresh targets and runtime locks", () => {
  const base = snapshot();
  base.tasks[0].version = 1;
  const second = { id: "second", status: "pending", updated_at: time(20), current_window_id: null };
  base.tasks[0].targets.push(second);
  base.sources.push({ id: "second", status: "pending", processed: 0, current_window_id: null });
  const next = live();
  next.tasks[0].task.version = 1;
  next.tasks[0].task.targets_partial = true;
  next.tasks[0].task.targets[0].updated_at = time(19);
  next.tasks[0].task.targets[0].mode_progress.followers.processed = 1;
  next.tasks[0].task.targets.push({
    id: "second", status: "running", updated_at: time(21), current_window_id: "w1",
    mode_progress: { followers: { processed: 3, source_total: 30 } },
  });
  next.tasks[0].runtime.profile_states[0].state = "waiting_network";
  const merged = applyLiveStatusOverlay(base, next);
  assert.equal(merged.tasks[0].targets[0], base.tasks[0].targets[0]);
  assert.equal(merged.sources[0], base.sources[0]);
  assert.equal(merged.sources[0].processed, 12);
  assert.equal(merged.tasks[0].targets[1].status, "running");
  assert.equal(merged.sources[1].processed, 3);
  assert.equal(merged.windows[0].locked, true);
  assert.equal(merged.windows[0].lock_state, "waiting_network");
  assert.equal(merged.windows[0].ready, false);
  next.tasks[0].task.version = 2;
  const newerTask = applyLiveStatusOverlay(base, next);
  assert.equal(newerTask.tasks[0].targets[0], base.tasks[0].targets[0]);
  assert.equal(newerTask.sources[0], base.sources[0]);
  assert.equal(newerTask.sources[1].processed, 3);
});

test("partial targets with missing timestamps preserve newer saved target evidence", () => {
  const next = live();
  next.tasks[0].task.targets_partial = true;
  delete next.tasks[0].task.targets[0].updated_at;
  const base = snapshot();
  const merged = applyLiveStatusOverlay(base, next);
  assert.equal(merged.tasks[0].targets[0], base.tasks[0].targets[0]);
  assert.equal(merged.sources[0], base.sources[0]);
  assert.equal(merged.windows[0].locked, true);
});

test("unknown tasks remain explicitly partial until a full task replaces the projection", () => {
  const base = snapshot();
  base.tasks = [];
  const first = live();
  first.tasks[0].task.targets_partial = true;
  const projected = applyLiveStatusOverlay(base, first);
  assert.equal(projected.tasks[0].targets_partial, true);
  const partial = live(22, 12);
  partial.tasks[0].task.targets_partial = true;
  partial.tasks[0].task.targets = [{ id: "second", status: "completed", updated_at: time(22) }];
  const enriched = applyLiveStatusOverlay(projected, partial);
  assert.equal(enriched.tasks[0].targets_partial, true);
  assert.deepEqual(enriched.tasks[0].targets.map((target) => target.id), ["target", "second"]);
  const full = live(23, 13);
  full.tasks[0].task.targets = [{ id: "authoritative", status: "completed", updated_at: time(23) }];
  const authoritative = applyLiveStatusOverlay(enriched, full);
  assert.notEqual(authoritative.tasks[0].targets_partial, true);
  assert.deepEqual(authoritative.tasks[0].targets.map((target) => target.id), ["authoritative"]);
});
