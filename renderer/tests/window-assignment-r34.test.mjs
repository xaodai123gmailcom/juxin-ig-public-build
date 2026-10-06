import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';
import { parseCollectionSeedDraft, collectionSeedIdentity } from '../src/collection-platform.ts';
import { collectionAssignmentTasks, assignmentSourceTask, assignmentWindowReason, assignmentHasJoinedWindow, planAssignmentJoins } from '../src/collection-window-assignment.ts';

const source = readFileSync(process.env.JUXIN_ASSIGNMENT_SOURCE || new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const task = (id, ids, status = 'running', updated_at = id) => ({ id, status, updated_at, settings: { live_queue_enabled: true }, windows: ids.map(profile_id => ({ profile_id })) });
const win = (id, owner, operation = 'collection') => ({ id, name: id, locked: Boolean(owner), lock_entity_id: owner, lock_operation: operation });
const candidate = (id = 'candidate', extra = {}) => ({ id, username: id, kind: 'manual', queue_state: 'queued', ...extra });
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };

test('future affinity accepts other owned live queue tasks and waiting-network without accepting unrelated task locks', () => {
  const tasks = [task('old', ['a']), task('new', ['b']), task('network', ['c'], 'waiting_network'), { ...task('legacy', ['d']), settings: {} }];
  assert.equal(assignmentWindowReason(win('a', 'old'), tasks), '');
  assert.equal(assignmentWindowReason(win('c', 'network'), tasks), '');
  assert.ok(assignmentWindowReason(win('a', 'old', 'greet'), tasks));
  assert.ok(assignmentWindowReason(win('d', 'legacy'), tasks));
  assert.ok(assignmentWindowReason(win('x', 'unknown'), tasks));
  assert.equal(assignmentHasJoinedWindow(tasks, ['a']), true);
  assert.deepEqual(planAssignmentJoins(tasks, [win('a', 'old')], ['a']), []);
  assert.equal(collectionAssignmentTasks(tasks).length, 3);
});

test('recovery joins its original task, never the latest task or two recovery tasks at once', () => {
  const tasks = [task('old', ['a'], 'paused', '1'), task('new', ['b'], 'running', '9')];
  const recovery = candidate('c', { source_task_id: 'old', source_target_id: 'checkpoint' });
  assert.equal(assignmentSourceTask(tasks, recovery).id, 'old');
  assert.deepEqual(planAssignmentJoins(tasks, [win('free')], ['free'], [recovery]), [{ taskId: 'old', windowIds: ['free'] }]);
  assert.ok(assignmentWindowReason(win('b', 'new'), tasks, recovery));
  assert.throws(() => planAssignmentJoins(tasks, [win('free')], ['free'], [recovery, candidate('d', { source_task_id: 'new', source_target_id: 'another' })]), /不同恢复任务/);
  assert.throws(() => planAssignmentJoins([{ ...tasks[0], status: 'stopped' }, tasks[1]], [win('free')], ['free'], [recovery]), /原采集任务/);
  assert.deepEqual(planAssignmentJoins(tasks, [win('free')], ['free'], [{ ...recovery, requires_source_task: false }]), [{ taskId: 'new', windowIds: ['free'] }]);
  assert.deepEqual(planAssignmentJoins(tasks, [win('a', 'old'), win('b', 'new')], ['a', 'b'], [recovery, candidate('d', { source_task_id: 'new', source_target_id: 'another' })]), []);
});

test('a drained released historical window explicitly rejoins while another window remains active', () => {
  const live = {...task('live', ['closed-a', 'active-b']), runtime: {profile_states: [
    {profile_id: 'closed-a', state: 'closed', reason: 'collection_queue_drained'},
    {profile_id: 'active-b', state: 'working'},
  ]}};
  assert.equal(assignmentHasJoinedWindow([live], ['closed-a']), false);
  assert.equal(assignmentHasJoinedWindow([live], ['active-b']), true);
  assert.deepEqual(planAssignmentJoins([live], [win('closed-a'), win('active-b', 'live')], ['closed-a', 'active-b']),
    [{taskId: 'live', windowIds: ['closed-a']}]);
  assert.deepEqual(planAssignmentJoins([live], [win('closed-a', 'live')], ['closed-a']), [], 'current owned lease wins over stale closed runtime');
  assert.throws(() => planAssignmentJoins([live], [win('closed-a', 'unrelated', 'follow')], ['closed-a']), /占用/);
  for(const state of ['paused', 'stopped', 'failed', 'manual_required']) {
    const retained = {...live, runtime: {profile_states: [{profile_id: 'closed-a', state, reason: 'collection_queue_drained'}]}};
    assert.equal(assignmentHasJoinedWindow([retained], ['closed-a']), true, state);
    assert.deepEqual(planAssignmentJoins([retained], [win('closed-a', 'live')], ['closed-a']), [], state);
  }
});

test('historical closed membership does not block a different recovery task or override its real lease', () => {
  const old = {...task('old', ['a'], 'running', '1'), runtime: {profile_states: [{profile_id: 'a', state: 'closed', reason: 'collection_queue_drained'}]}};
  const recoveryTask = task('recovery', ['b'], 'running', '9');
  const recovery = candidate('returning', {source_task_id: 'recovery', source_target_id: 'checkpoint'});
  assert.deepEqual(planAssignmentJoins([old, recoveryTask], [win('a')], ['a'], [recovery]), [{taskId: 'recovery', windowIds: ['a']}]);
  assert.throws(() => planAssignmentJoins([old, recoveryTask], [win('a', 'old')], ['a'], [recovery]), /原任务/);
});

function handlers({ tasks = [task('live', ['a'])], windows = [win('a', 'live'), win('free')], item = candidate(), draft = 'candidate', existing = true } = {}) {
  const calls = [], errors = [];
  const state = { editor: existing ? { candidateId: item.id, candidate: item, usernames: [item.username], initialWindowIds: [] } : { candidateId: null, usernames: [draft], initialWindowIds: [] }, draft, snapshot: { tasks, windows, split_candidates: [item] } };
  const client = {
    async assignWaitingSplitWindows(id, ids) { calls.push(['assign', id, [...ids]]); return { ...item, allowed_window_ids: ids }; },
    async addCollectionWindows(id, ids) { calls.push(['join', id, [...ids]]); if (state.joinGate) await state.joinGate.promise; if (state.failJoin) throw new Error('lease changed'); state.snapshot.tasks.find(t => t.id === id).windows.push(...ids.map(profile_id => ({ profile_id }))); },
    async checkCompletedCollectionTargets() { calls.push(['check']); if (state.addGate) await state.addGate.promise; return { completed: [] }; },
    async addWaitingSplitTargets(names, _allow, ids) { calls.push(['add', [...names], [...ids]]); if (state.addGate) await state.addGate.promise; return { candidates: [item], accepted_ids: [item.id], duplicates: [] }; },
  };
  const context = {
    Error, platform: "instagram", collectionSeedIdentity, parseCollectionSeedDraft, assignmentSaveRef: { current: false }, draftAddRef: { current: false }, assignmentSnapshotRef: { get current() { return state.snapshot; } },
    planAssignmentJoins, assignmentWindowReason, assignmentHasJoinedWindow, collectionAssignmentTasks,
    setSelectedWindows() {}, setTargetDraft(next) { state.draft = typeof next === 'function' ? next(state.draft) : next; },
    setAssignmentEditor(next) { state.editor = typeof next === 'function' ? next(state.editor) : next; },
    run: async (_key, action) => { try { await action(client); return true; } catch (error) { errors.push(error.message); return false; } },
    window: { confirm: () => true }, selectedWindowOrder: [], normalizeStatus: s => s,
  };
  Object.defineProperties(context, { assignmentEditor: { get: () => state.editor }, targetDraft: { get: () => state.draft }, snapshot: { get: () => state.snapshot }, acceptingTask: { get: () => state.snapshot.tasks.at(-1) } });
  const code = source.slice(source.indexOf('  function parsedDraftTargets()'), source.indexOf('  async function removeWaitingTarget('));
  const methods = runInNewContext(stripTypeScriptTypes(source.slice(source.indexOf('async function addSplitTargetsWithConfirmation('), source.indexOf('async function addCandidateToSplit(')) + code) + '\n({saveWindowAssignment,addDraftTargets});', context);
  return { state, calls, errors, client, ...methods };
}

test('the real handler saves affinity before joining and retains a retryable committed dialog on join failure', async () => {
  const h = handlers(); h.state.failJoin = true;
  await h.saveWindowAssignment(['free']);
  assert.deepEqual(h.calls.map(call => call[0]), ['assign', 'join']);
  assert.deepEqual([...h.state.editor.savedWindowIds], ['free']);
  assert.match(h.errors[0], /指派已保存/);
  h.state.failJoin = false;
  await h.saveWindowAssignment(['free']);
  assert.deepEqual(h.calls.map(call => call[0]), ['assign', 'join', 'join']);
  assert.equal(h.state.editor, null);
});

test('new draft retry never enqueues again and a same-turn double submit is fenced across check, save and join', { timeout: 1000 }, async () => {
  const h = handlers({ existing: false }); h.state.addGate = deferred(); h.state.failJoin = true;
  const first = h.saveWindowAssignment(['free']);
  await h.saveWindowAssignment(['free']);
  assert.equal(h.calls.length, 1);
  h.state.addGate.resolve(); await first;
  assert.equal(h.state.draft, '');
  assert.ok(h.state.editor.savedWindowIds);
  h.state.failJoin = false; await h.saveWindowAssignment(['free']);
  assert.deepEqual(h.calls.map(call => call[0]), ['add', 'join', 'join']);
  assert.equal(h.state.editor, null);
});

test('existing candidate dispatch rechecks the latest snapshot and refuses claimed targets or newly unrelated locks', async () => {
  const h = handlers(); h.state.snapshot = { ...h.state.snapshot, split_candidates: [candidate('candidate', { queue_state: 'claimed' })] };
  await h.saveWindowAssignment(['free']); assert.equal(h.calls.length, 0); assert.match(h.errors[0], /已被领取/);
  h.state.snapshot = { ...h.state.snapshot, split_candidates: [candidate()], windows: [win('free', 'greet', 'greet')] };
  await h.saveWindowAssignment(['free']); assert.equal(h.calls.length, 0); assert.ok(h.state.editor);
});

test('actual handler keeps recovery checkpoint scope while a newer task is accepting ordinary targets', async () => {
  const item = candidate('recovery', { source_task_id: 'old', source_target_id: 'checkpoint' });
  const h = handlers({ item, tasks: [task('old', ['a'], 'paused', '1'), task('new', ['b'], 'running', '9')] });
  await h.saveWindowAssignment(['free']);
  assert.deepEqual(h.calls.map(call => [call[0], call[1]]), [['assign', 'recovery'], ['join', 'old']]);
});

test('draft updates during an in-flight add are preserved and the add guard lasts through joining', { timeout: 1000 }, async () => {
  const h = handlers({ existing: false }); h.state.joinGate = deferred();
  const first = h.addDraftTargets(['free']);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  h.state.draft = 'next-target';
  assert.equal(await h.addDraftTargets(['free']), false);
  h.state.joinGate.resolve(); await first;
  assert.equal(h.state.draft, 'next-target');
  assert.equal(h.calls.filter(call => call[0] === 'add').length, 1);
});

test('a failed later recovery preserves confirmed batch writes and leaves unadded input for an explicit later retry', async () => {
  const h = handlers({ existing: false, draft: 'first second', item: candidate('first') });
  h.state.editor.usernames = ['first', 'second'];
  h.client.addWaitingSplitTargets = async () => {
    h.calls.push(['add']);
    return { candidates: [candidate('first')], accepted_ids: ['first'], duplicates: [{ username: 'second', disposition: 'failure', candidate_ids: ['failure'] }] };
  };
  h.client.requeueFailedSplitTarget = async () => { h.calls.push(['requeue']); throw new Error('stale failure'); };
  await h.saveWindowAssignment(['free']);
  assert.equal(h.state.draft, 'second');
  assert.deepEqual([...h.state.editor.savedCandidates].map(c => c.id), ['first']);
  assert.match(h.errors[0], /stale failure/);
  await h.saveWindowAssignment(['free']);
  assert.deepEqual(h.calls.map(call => call[0]), ['add', 'requeue', 'join']);
  assert.equal(h.state.editor, null);
  assert.equal(h.state.draft, 'second');
});

test('real dialog toggle can remove an old selection after it becomes locked, while new locked choices remain blocked', () => {
  const body = source.slice(source.indexOf('  function toggle(window: CoreBitBrowserWindow)'), source.indexOf('\n  return (', source.indexOf('  function toggle(window: CoreBitBrowserWindow)')));
  let selected = new Set(['old']);
  const context = { saving: false, committed: false, tasks: [], editor: {}, acceptingTaskId: 'none', assignmentWindowReason, setSelected: value => { selected = value; } };
  Object.defineProperty(context, 'selected', { get: () => selected });
  const toggle = runInNewContext(stripTypeScriptTypes(body) + '\ntoggle;', context);
  toggle(win('old', 'greet', 'greet')); assert.equal(selected.has('old'), false);
  toggle(win('new', 'greet', 'greet')); assert.equal(selected.has('new'), false);
});

test('a late join reply cannot close a newer assignment editor', async () => {
  const h = handlers(); h.state.joinGate = deferred();
  const first = h.saveWindowAssignment(['free']);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  const replacement = { candidateId: 'next', candidate: candidate('next'), usernames: ['next'], initialWindowIds: [] };
  h.state.editor = replacement;
  h.state.joinGate.resolve(); await first;
  assert.equal(h.state.editor, replacement);
});
