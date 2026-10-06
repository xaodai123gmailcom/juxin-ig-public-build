import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';
import { DISCARD_LIMIT_KEYS, readDiscardCountLimits, discardCountLimitsPayload, discardCountLimitsError } from '../src/collection-discard-limits.ts';
import { createCollectorCoreClient } from '../src/core-client.ts';
const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const field = 'public_discard_active_days_max';

test('upgrade keeps new post-age unlimited and does not import retired limits', () => {
  const draft = readDiscardCountLimits({ active_days_max: 7, mode_limits: { followers: { active_days_max: 1 } } });
  assert.equal(draft.limits[field], '0');
  for (const key of DISCARD_LIMIT_KEYS.filter(key => key !== field)) assert.equal(draft.limits[key], '4000');
  assert.equal(DISCARD_LIMIT_KEYS.length, 7);
});

test('post-age saves and restores zero, a chosen day limit and disabled state independently', () => {
  for (const enabled of [true, false]) for (const days of [0, 30, 365]) {
    const draft = readDiscardCountLimits({ discard_count_limits_enabled: enabled, [field]: days, public_discard_posts_max: 55 });
    const payload = discardCountLimitsPayload(draft);
    assert.equal(payload[field], days);
    assert.equal(payload.public_discard_posts_max, 55);
    assert.equal(payload.discard_count_limits_enabled, enabled);
    assert.deepEqual(readDiscardCountLimits(payload), draft);
  }
});

test('invalid active post-age is rejected; disabling the rule falls back to unlimited', () => {
  for (const value of ['', '-1', '1.5', 'abc']) {
    const draft = readDiscardCountLimits({}); draft.limits[field] = value;
    assert.ok(discardCountLimitsError(draft));
    assert.throws(() => discardCountLimitsPayload(draft), /非负整数/);
    draft.enabled = false;
    assert.equal(discardCountLimitsPayload(draft)[field], 0);
  }
});

test('actual create handler and real Core request carry public post-age, no retired qualification limits', async () => {
  const calls = [];
  const core = createCollectorCoreClient({ request: async (...args) => {
    calls.push(args); return { command: 'task_create', snapshot_seq: 1, result: { task_id: 'r38', status: 'created' } };
  }, secureSet: async () => true, secureGet: async () => null, secureDelete: async () => true, configureIntegrations: async () => ({ restarted: true }) });
  const client = { createCollectionTask: core.createCollectionTask.bind(core), controlCollectionTask: async () => {} };
  const begin = source.indexOf('  async function createTask(event?: FormEvent)');
  const fn = source.slice(begin, source.indexOf('\n  return (', begin));
  const ctx = { facebook: false, platform: "instagram", targetDraft: "", createTaskRef: { current: false }, collectionWindows: [{ id: "w1" }], followers: true, following: true, targets: ['next'], claimableWaitingCount: 1, selectedWindowOrder: ['w1'], filtersValid: true,
    localPersonRecognition: false, excludeMaleAvatar: false, autoClassify: true, openAiReview: false,
    excludeVerified: false, excludePublicZeroPosts: true, parallelScreeningWorkers: 1,
    discardLimits: readDiscardCountLimits({ [field]: 30 }), discardCountLimitsPayload,
    run: async (_key, action) => action(client) };
  await runInNewContext(stripTypeScriptTypes(fn) + '\ncreateTask;', ctx)();
  assert.equal(calls.length, 1);
  const command = typeof calls[0][1].body === 'string' ? JSON.parse(calls[0][1].body) : calls[0][1].body;
  assert.equal(command.payload[field], 30);
  assert.deepEqual(JSON.parse(JSON.stringify(command.payload.source_limits)), {});
  assert.equal(command.payload.public_discard_posts_max, 4000);
});

test('public-only activity field is visible and editable in the unified discard panel', () => {
  const panel = source.slice(source.indexOf('<Panel className="collection-settings-panel"'), source.indexOf('<div className="collection-workbench-grid">'));
  assert.match(panel, /visibility === "public" \? <>/);
  assert.match(panel, /value=\{discardLimits\.limits\.public_discard_active_days_max\}/);
  assert.match(panel, /public_discard_active_days_max: event\.target\.value/);
  assert.match(panel, /公开帖子活跃度上限（天）/);
  assert.match(panel, /disabled=\{!discardLimits\.enabled\}/);
  assert.match(panel, /min="0" step="1"/);
  assert.doesNotMatch(panel, /原合格规则/);
  assert.match(panel, /aria-label="公开帖子活跃度上限（天）"/);
});

test('all-unlimited button actually resets all seven inputs while retaining the switch', () => {
  let current = readDiscardCountLimits({ [field]: 30 });
  const begin = source.indexOf('  function setAllDiscardLimitsUnlimited()');
  const fn = source.slice(begin, source.indexOf('\n  const runningTasks', begin));
  runInNewContext(stripTypeScriptTypes(fn) + '\nsetAllDiscardLimitsUnlimited();', {
    DISCARD_LIMIT_KEYS, setDiscardLimits: update => { current = update(current); },
  });
  assert.equal(current.enabled, true);
  assert.ok(Object.values(current.limits).every(value => value === '0'));
  assert.equal(Object.keys(current.limits).length, 7);
});

test('review displays measured post age instead of a newer story and keeps unknown evidence unknown', () => {
  const begin = source.indexOf('function reviewedPostActivity(');
  const fn = source.slice(begin, source.indexOf('\nfunction reviewFailureFlags(', begin));
  const label = runInNewContext(stripTypeScriptTypes(fn) + '\nreviewActivityLabel;', {
    firstText: (record, keys, fallback = '—') => keys.map(k => record[k]).find(v => typeof v === 'string' && v) || fallback,
    firstNumber: (record, keys) => keys.map(k => record[k]).find(v => typeof v === 'number' && Number.isFinite(v)) ?? null,
  });
  assert.equal(label({ activity_status: 'story_today', activity_days: 0, post_activity_days: 60, post_activity_status: 'identified' }), '60 天前发帖');
  assert.equal(label({ activity_status: 'story_today', activity_days: 0, post_activity_days: 60, post_activity_status: 'timestamp_unavailable' }), '发帖时间未知');
  assert.equal(label({ post_activity_status: 'no_posts', post_activity_days: null }), '0 帖（待人工）');
  assert.equal(label({ activity_status: 'story_today', activity_days: 0, post_activity_status: 'not_checked' }), '当天活跃（快拍）');
  assert.equal(label({ post_activity_status: 'identified', post_activity_days: 0 }), '当天发帖');
  for (const status of ['timestamp_found', 'timestamp_read']) assert.equal(label({ post_activity_status: status, post_activity_days: 7 }), '7 天前发帖');
  assert.equal(label({ post_activity_status: 'identified', post_activity_days: -1 }), '发帖时间未知');
});
