import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';
import { DISCARD_LIMIT_KEYS, parseDiscardLimit, readDiscardCountLimits, discardCountLimitsError, discardCountLimitsPayload } from '../src/collection-discard-limits.ts';
import { createCollectorCoreClient } from '../src/core-client.ts';

const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const numbers = Object.fromEntries(DISCARD_LIMIT_KEYS.map((key, index) => [key, index ? 4100 + index : 0]));

test('legacy settings default to enabled / 4000 without borrowing any qualification limit', () => {
  const settings = { source_limits: { followers: { followersMax: 25 } }, mode_limits: { followers: { followers_max: 12 } } };
  const draft = readDiscardCountLimits(settings);
  assert.equal(draft.enabled, true);
  assert.deepEqual(Object.values(draft.limits), ['4000', '4000', '4000', '4000', '4000', '4000', '0']);
  assert.equal(settings.mode_limits.followers.followers_max, 12);
  for (const value of [undefined, null, 'invalid', []]) assert.deepEqual(readDiscardCountLimits(value), draft);
});

test('seven separate limits, explicit zero and disabled state round-trip through task settings', () => {
  const saved = { discard_count_limits_enabled: false, ...numbers };
  const draft = readDiscardCountLimits(saved);
  assert.equal(draft.enabled, false);
  assert.equal(draft.limits.private_discard_followers_max, '0');
  assert.deepEqual(discardCountLimitsPayload(draft), saved);
  assert.deepEqual(readDiscardCountLimits(discardCountLimitsPayload(draft)), draft);
  assert.deepEqual(saved, { discard_count_limits_enabled: false, ...numbers }, 'restoration does not mutate the Core snapshot');
});

test('nonnegative integer input accepts zero but does not coerce missing, fractional or nonnumeric input to unlimited', () => {
  for (const value of [undefined, null, true, false, '', ' ', '-1', -1, '0.1', 0.1, '4,000', '4e3', 'Infinity', Infinity, NaN, {}, [], '9007199254740992']) assert.equal(parseDiscardLimit(value), null, String(value));
  for (const [value, expected] of [[0, 0], ['0', 0], [' 4000 ', 4000], ['0004001', 4001], [Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]]) assert.equal(parseDiscardLimit(value), expected);
});

test('malformed persisted limits fall back independently, never disabling the entire rule', () => {
  const saved = { ...numbers, private_discard_posts_max: -1, public_discard_following_max: null, discard_count_limits_enabled: 'false' };
  const payload = discardCountLimitsPayload(readDiscardCountLimits(saved));
  assert.equal(payload.discard_count_limits_enabled, true);
  assert.equal(payload.private_discard_posts_max, 4000);
  assert.equal(payload.public_discard_following_max, 4000);
  assert.equal(payload.private_discard_followers_max, 0);
  assert.equal(payload.public_discard_posts_max, 4105);
});

test('invalid active edits block submission, while switching off remains possible and preserves valid limits', () => {
  const draft = readDiscardCountLimits({ ...numbers });
  draft.limits.public_discard_posts_max = '';
  assert.ok(discardCountLimitsError(draft));
  assert.throws(() => discardCountLimitsPayload(draft), /非负整数/);
  draft.enabled = false;
  assert.equal(discardCountLimitsError(draft), '');
  const payload = discardCountLimitsPayload(draft);
  assert.equal(payload.discard_count_limits_enabled, false);
  assert.equal(payload.public_discard_posts_max, 4000);
  assert.equal(payload.private_discard_followers_max, 0);
  assert.equal(payload.private_discard_following_max, 4101);
});

function createHandler(draft = readDiscardCountLimits({ ...numbers })) {
  const calls = [];
  const client = {
    async createCollectionTask(payload) { calls.push(['create', payload]); return { task_id: 'saved-task' }; },
    async controlCollectionTask(...args) { calls.push(['control', ...args]); },
  };
  const context = {
    facebook: false, platform: "instagram", targetDraft: "", createTaskRef: { current: false }, collectionWindows: [{ id: "window-4" }], followers: true, following: true, targets: ['queued-target'], claimableWaitingCount: 1, selectedWindowOrder: ['window-4'],
    filtersValid: !discardCountLimitsError(draft), modeLimitsValid: true,
    filterValues: { followersMin: 0, followersMax: 91, followingMin: 0, followingMax: 92, postsMin: 0, postsMax: 93, activityDays: 7 },
    localPersonRecognition: false, excludeMaleAvatar: false, autoClassify: true, openAiReview: false, excludeVerified: false,
    excludePublicZeroPosts: true, parallelScreeningWorkers: 2, discardLimits: draft, discardCountLimitsPayload,
    run: async (_key, action) => { await action(client); return true; },
  };
  const begin = source.indexOf('  async function createTask(event?: FormEvent)');
  assert.ok(begin >= 0);
  const code = source.slice(begin, source.indexOf('\n  return (', begin));
  return { calls, createTask: runInNewContext(stripTypeScriptTypes(code) + '\ncreateTask;', context) };
}

test('actual create handler sends independent discard fields without submitting retired qualification limits', async () => {
  const handler = createHandler();
  await handler.createTask();
  assert.deepEqual(handler.calls.map(call => call[0]), ['create', 'control']);
  const payload = JSON.parse(JSON.stringify(handler.calls[0][1]));
  for (const key of DISCARD_LIMIT_KEYS) assert.equal(payload[key], numbers[key], key);
  assert.equal(payload.discard_count_limits_enabled, true);
  assert.deepEqual(payload.source_limits, {});
  assert.deepEqual(handler.calls[1], ['control', 'saved-task', 'start']);
});

test('actual create handler blocks invalid active limits before creating or starting a task', async () => {
  const draft = readDiscardCountLimits({ ...numbers });
  draft.limits.private_discard_posts_max = '-2';
  const handler = createHandler(draft);
  await handler.createTask();
  assert.equal(handler.calls.length, 0);
});

test('real Core client serializes all fields to task_create, preserving false and zero', async () => {
  const calls = [];
  const client = createCollectorCoreClient({
    request: async (...args) => { calls.push(args); return { command: 'task_create', snapshot_seq: 1, result: { task_id: 'stored', status: 'created' } }; },
    secureSet: async () => true,
    secureGet: async () => null,
    secureDelete: async () => true,
    configureIntegrations: async () => ({ restarted: true }),
  });
  const payload = { targets: [], window_ids: ['window-4'], modes: ['followers'], source_limits: {}, ...discardCountLimitsPayload(readDiscardCountLimits({ discard_count_limits_enabled: false, ...numbers })) };
  await client.createCollectionTask(payload);
  assert.equal(calls.length, 1);
  const serialized = typeof calls[0][1].body === 'string' ? JSON.parse(calls[0][1].body) : calls[0][1].body;
  assert.equal(serialized.type, 'task_create');
  assert.equal(serialized.payload.discard_count_limits_enabled, false);
  for (const key of DISCARD_LIMIT_KEYS) assert.equal(serialized.payload[key], numbers[key]);
});

test('UI restoration and unified settings panel are wired to the tested discard mapper rather than qualification values', () => {
  assert.match(source, /discardCountLimits: readDiscardCountLimits\(settings\)/);
  assert.match(source, /saved\.discardCountLimits as ReturnType<typeof readDiscardCountLimits>/);
  const panel = source.slice(source.indexOf('<Panel className="collection-settings-panel"'), source.indexOf('<div className="collection-workbench-grid">'));
  assert.match(panel, /title="采集设置"/);
  assert.match(panel, /id="collection-discard-title"/);
  assert.match(panel, /公开帖子活跃度上限（天）/);
  assert.match(panel, /disabled=\{!discardLimits\.enabled\}/);
  assert.doesNotMatch(panel, /filterValues|setFollowersMax|setFollowingMax|setPostsMax|source_limits/);
});
