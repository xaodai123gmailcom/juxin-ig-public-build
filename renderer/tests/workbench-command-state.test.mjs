import {shareUnchangedJson} from '../src/snapshot-sharing.ts';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';
import { performActionSafetyControl, performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback } from '../src/workbench-command-state.ts';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test('snapshot failure blocks new work without inventing a pending start or blocking bound controls', () => {
  const state = workbenchCommandFeedback(true, false, false);
  assert.equal(state.disabled, true);
  assert.equal(state.pending, false);
  assert.equal(state.refreshing, false);
  assert.equal(state.safetyControlDisabled, false);
});

test('an actual command remains pending and serialized even if a snapshot fails concurrently', () => {
  const state = workbenchCommandFeedback(true, true, true);
  assert.equal(state.pending, true);
  assert.equal(state.disabled, true);
  assert.equal(state.safetyControlDisabled, true);
});

test('normal commands distinguish accepted work from waiting for a refreshed list', async () => {
  const command = deferred(), refresh = deferred();
  let pending = true, finished = false, refreshCalls = 0;
  const run = runWithSnapshotRefresh({
    action: () => command.promise,
    refresh: () => { refreshCalls++; return refresh.promise; },
    onCommandSettled: () => { pending = false; },
  }).then(result => { finished = true; return result; });
  assert.equal(pending, true);
  assert.equal(refreshCalls, 0);
  command.resolve({ task_id: 'task-1' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(pending, false);
  assert.equal(finished, false);
  const state = workbenchCommandFeedback(false, true, pending);
  assert.equal(state.refreshing, true);
  assert.equal(state.pending, false);
  assert.equal(state.disabled, true);
  refresh.resolve();
  assert.deepEqual(await run, { task_id: 'task-1' });
});

test('bound pause or stop completes without waiting for a slow full snapshot', async () => {
  const refresh = deferred();
  let pending = true, refreshCalls = 0;
  const result = await runWithSnapshotRefresh({
    action: async () => 'stopped',
    refresh: () => { refreshCalls++; return refresh.promise; },
    onCommandSettled: () => { pending = false; },
    backgroundRefresh: true,
  });
  assert.equal(result, 'stopped');
  assert.equal(pending, false);
  assert.equal(refreshCalls, 1);
  refresh.reject(new Error('snapshot unavailable'));
  await Promise.resolve();
});

test('refresh failure never turns a successful command into failure or replaces its real error', async () => {
  for (const backgroundRefresh of [false, true]) {
    let settled = 0;
    const base = { refresh: async () => { throw new Error('refresh failed'); },
      onCommandSettled: () => { settled++; }, backgroundRefresh };
    assert.equal(await runWithSnapshotRefresh({ ...base, action: async () => 7 }), 7);
    const commandError = new Error('ownership changed');
    await assert.rejects(runWithSnapshotRefresh({ ...base, action: async () => { throw commandError; } }),
      error => error === commandError);
    assert.equal(settled, 2);
  }
});

function clientSpy() {
  const calls = [];
  return { calls, client: {
    controlCollectionTask: async (...args) => { calls.push(['task', ...args]); },
    controlCollectionWindow: async (...args) => { calls.push(['window', ...args]); },
  } };
}

test('the restricted control path always sends the exact task, window and expected target', async () => {
  const { client, calls } = clientSpy();
  await performCollectionSafetyControls(client, [
    { scope: 'task', taskId: 'task-a', action: 'pause' },
    { scope: 'window', taskId: 'task-b', profileId: 'w4', targetId: 'target-b', action: 'stop' },
    { scope: 'window', taskId: 'task-c', profileId: 'w5', targetId: 'target-c', action: 'pause' },
  ]);
  assert.deepEqual(calls, [['task', 'task-a', 'pause'], ['window', 'task-b', 'w4', 'stop', 'target-b'],
    ['window', 'task-c', 'w5', 'pause', 'target-c']]);
});

test('resume, delete, restart and incomplete bindings cannot use the snapshot-error exception', async () => {
  const good = { scope: 'window', taskId: 't', profileId: 'w', targetId: 'x', action: 'pause' };
  for (const invalid of [
    ...['resume', 'delete', 'restart', 'start'].map(action => ({ ...good, action })),
    { ...good, taskId: '' }, { ...good, profileId: '' }, { ...good, targetId: '' },
    { ...good, scope: 'unknown' }, { scope: 'task', taskId: 't', action: 'stop' },
  ]) {
    const { client, calls } = clientSpy();
    await assert.rejects(performCollectionSafetyControls(client, [good, invalid]), /当前任务绑定/);
    assert.deepEqual(calls, [], 'invalid batches must fail before their first command');
  }
});

test('one stale task cannot prevent a bulk pause reaching the remaining tasks or claim complete success', async () => {
  const calls = [];
  const client = { controlCollectionTask: async (id) => {
    calls.push(id);
    if (id === 'old-task') throw new Error('Task no longer active');
  } };
  await assert.rejects(performCollectionSafetyControls(client,
    ['old-task', 'active-task', 'another-task'].map(taskId => ({ scope: 'task', taskId, action: 'pause' }))),
    /3 项控制请求，其中 1 项未成功：old-task：Task no longer active/);
  assert.deepEqual(calls, ['old-task', 'active-task', 'another-task']);
});

test('the real hook blocks an async follow-up mutation after refresh fails but still permits bound stop', async () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  const hook = source.slice(source.indexOf('function useFormalCore()'), source.indexOf('\ntype CollectionControls'));
  assert.ok(hook.startsWith('function useFormalCore()'));
  const effects = [], calls = [];
  let polling;
  const client = {
    liveStatus: async () => ({}),
    controlCollectionWindow: async (...args) => { calls.push(args); },
  };
  // Run the actual hook body with minimal hook scheduling; this is a command
  // lifecycle regression, not a React DOM or browser integration test.
  const core = runInNewContext(stripTypeScriptTypes(hook) + '\nuseFormalCore();', {
    shareUnchangedJson,
    useState: initial => [initial, () => {}], useRef: initial => ({ current: initial }),
    useEffect: effect => effects.push(effect), useCallback: fn => fn,
    getCollectorCoreClient: () => client, usePlatformCore: () => client, useWorkbenchPlatform: () => ({platform: "instagram"}), collectionPlatform: () => "instagram",
    startWorkbenchSnapshotPolling: options => {
      polling = options;
      return { stop() {}, refresh: async () => {
        const error = new Error('Full snapshot timeout');
        options.onError(error);
        throw error;
      } };
    },
    applyLiveStatusOverlay: snapshot => snapshot,
    SNAPSHOT_PAGE_LIMIT: 500,
    document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: { setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {} },
    performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback,
  });
  const dispose = effects[0]();
  try {
    let mutations = 0;
    assert.equal(await core.run('first', async () => { mutations++; }), true);
    assert.equal(await core.run('follow-up', async () => { mutations++; }), false);
    assert.equal(mutations, 1, 'the failed refresh must block a later continuation before React renders');
    assert.equal(await core.reviewRun('fresh-review-page', async () => { mutations++; }), true);
    assert.equal(mutations, 2, 'the independently loaded review page must remain usable');
    assert.equal(await core.collectionControls.run('bound-stop', [
      { scope: 'window', taskId: 't', profileId: 'w', targetId: 'target', action: 'stop' },
    ], 'Stopped'), true);
    assert.deepEqual(calls, [['t', 'w', 'stop', 'target']]);
    polling.onSnapshot({});
    assert.equal(await core.run('after-recovery', async () => { mutations++; }), true);
    assert.equal(mutations, 3);
  } finally { dispose(); }
});

test('UI uses actual pending state and restricts snapshot-independent dispatch to bound controls and review', () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  assert.ok(source.includes('collectionControls.feedback("collection-create").pending ? <LoaderCircle'));
  assert.ok(!source.includes('{disabled("collection-create") ? <LoaderCircle'));
  assert.ok(source.includes('collectionControls.feedback("collection-create").refreshing ? "更新列表中"'));
  assert.ok(source.includes('collectionControls.snapshotStale ? "列表更新失败"'));
  assert.ok(source.includes('snapshotErrorRef.current = reason;'));
  assert.ok(source.includes('snapshotErrorRef.current = null;'));
  assert.ok(source.includes('if (!backgroundRefresh && snapshotErrorRef.current)'));
  assert.match(source, /const run = useCallback\([\s\S]*execute\(key, action, success\), \[execute\]\);/);
  assert.ok(source.includes('<StableReviewWorkspace snapshot={snapshot} run={reviewRun} disabled={reviewDisabled} />'));
  assert.ok(source.includes('<WorkbenchBody mode={mode} snapshot={snapshot} run={run} disabled={disabled}'));
  const start = source.indexOf('function CollectionTaskRow');
  const row = source.slice(start, source.indexOf('\ntype WorkspaceProps', start));
  assert.match(row, /action === "pause" \|\| action === "stop"/);
  assert.match(row, /\{ scope: "window", taskId: task.id, profileId, targetId, action \}/);
  for (const action of ['pause', 'stop'])
    assert.ok(row.includes(`disabled={safetyControlDisabled} onClick={() => void controlWindow("${action}"`));
  assert.ok(row.includes('onClick={() => void deleteWindowTask("delete_only")}'));
  assert.ok(row.includes('onClick={() => void deleteWindowTask("delete")}'));
});

test('action safety controls retain exact campaign and target IDs and reject new work', async () => {
  const calls = [];
  const client = {
    controlActionCampaign: async (...args) => calls.push(['campaign', ...args]),
    controlActionTarget: async (...args) => calls.push(['target', ...args]),
  };
  await performActionSafetyControl(client, { scope: 'campaign', campaignId: 'a', action: 'pause' });
  await performActionSafetyControl(client, { scope: 'campaign', campaignId: 'b', action: 'stop' });
  await performActionSafetyControl(client, { scope: 'target', campaignId: 'c', targetId: 'exact-target', action: 'cancel' });
  await performActionSafetyControl(client, { scope: 'target', campaignId: 'd', targetId: 'other-target', action: 'pause' });
  assert.deepEqual(calls, [['campaign', 'a', 'pause'], ['campaign', 'b', 'stop'],
    ['target', 'c', 'exact-target', 'cancel'], ['target', 'd', 'other-target', 'pause']]);
  for (const request of [
    { scope: 'campaign', campaignId: 'a', action: 'resume' },
    { scope: 'target', campaignId: 'a', targetId: 't', action: 'start' },
    { scope: 'target', campaignId: 'a', targetId: '', action: 'cancel' },
    { scope: 'campaign', campaignId: '', action: 'stop' },
    { scope: 'window', campaignId: 'a', action: 'pause' },
  ]) await assert.rejects(performActionSafetyControl(client, request), /当前任务绑定/);
  assert.equal(calls.length, 4);
});

test('actual hook permits bound action stop during snapshot failure without waiting or admitting resume', async () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  const hook = source.slice(source.indexOf('function useFormalCore()'), source.indexOf('\ntype CollectionControls'));
  const effects = [], calls = [], command = deferred(), refresh = deferred();
  let polling;
  const client = { lastCommandSnapshotSeq: 0, liveStatus: async () => ({}),
    controlActionCampaign: async (...args) => { calls.push(args); return command.promise; } };
  const core = runInNewContext(stripTypeScriptTypes(hook) + '\nuseFormalCore();', {
    shareUnchangedJson, useState: initial => [initial, () => {}], useRef: initial => ({ current: initial }),
    useEffect: effect => effects.push(effect), useCallback: fn => fn,
    getCollectorCoreClient: () => client, usePlatformCore: () => client, useWorkbenchPlatform: () => ({platform: "instagram"}), collectionPlatform: () => "instagram",
    startWorkbenchSnapshotPolling: options => { polling = options; return { stop() {}, refresh: () => refresh.promise }; },
    applyLiveStatusOverlay: snapshot => snapshot, SNAPSHOT_PAGE_LIMIT: 500,
    document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: { setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {} },
    performActionSafetyControl, performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback,
  });
  const dispose = effects[0]();
  try {
    polling.onError(new Error('Snapshot timeout'));
    let unsafe = 0;
    assert.equal(await core.run('resume', async () => { unsafe++; }), false);
    const request = { scope: 'campaign', campaignId: 'exact-campaign', action: 'stop' };
    const stopping = core.actionControls.run('same-key', request, 'Stopped');
    assert.equal(await core.actionControls.run('same-key', request, 'Stopped'), false);
    assert.equal(calls.length, 1);
    command.resolve({ status: 'stopped' });
    assert.equal(await stopping, true, 'stop acknowledgement must not wait for a full snapshot');
    assert.deepEqual(calls, [['exact-campaign', 'stop']]);
    assert.equal(unsafe, 0);
    refresh.reject(new Error('Still unavailable'));
    await Promise.resolve();
  } finally { dispose(); }
});

test('both action panels route pause and cancellation through the bounded safety path', () => {
  const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
  const list = source.slice(source.indexOf('function CampaignList('), source.indexOf('function FailureList('));
  assert.equal((list.match(/actionControls\.run\(/g) || []).length, 4);
  assert.match(list, /scope: "target", campaignId: campaign.id, targetId: target.id \|\| "", action: "cancel"/);
  assert.match(list, /disabled=\{disabled\(key\)\}[^\n]*controlActionCampaign\(campaign.id, "resume"\)/);
  assert.match(source, /operation="greet" actionControls=\{actionControls\}/);
  assert.match(source, /operation="follow" actionControls=\{actionControls\}/);
});
