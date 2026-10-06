import test from 'node:test';
import assert from 'node:assert/strict';
import { createCollectorCoreClient, CollectorCoreProtocolError } from '../src/core-client.ts';

function clientFor(request) {
  return createCollectorCoreClient({request, secureSet: async () => true,
    secureGet: async () => null, secureDelete: async () => true,
    configureIntegrations: async () => ({restarted: false})});
}

test('review queries carry the exact layer and pagination through the authenticated Core bridge', async () => {
  const calls = [];
  const response = {items: [], total: 2300, offset: 2000, limit: 100, has_more: true,
    counts: {public: {stage1: 120, stage2: 2300}, private: {stage1: 9, stage2: 2}}, snapshot_seq: 45};
  const client = clientFor(async (path, options) => {calls.push({path, options}); return response;});
  const query = {visibility: 'public', review_stage: 2, offset: 2000, limit: 100};
  assert.strictEqual(await client.reviewQueue(query), response);
  assert.deepEqual(calls, [{path: '/api/workbench/review/query', options: {method: 'POST', body: query}}]);
});

test('a transfer uses explicit selected IDs and validates the command envelope', async () => {
  const payload = {candidate_ids: ['selected-a', 'selected-b'], visibility: 'private', from_stage: 1, to_stage: 2};
  const calls = [];
  const result = {moved_ids: ['selected-a'], skipped_ids: ['selected-b'], moved_count: 1};
  const client = clientFor(async (path, options) => {
    calls.push({path, options});
    return {command: 'review_stage_move', result, snapshot_seq: 46};
  });
  assert.strictEqual(await client.moveReviewStage(payload), result);
  assert.deepEqual(calls, [{path: '/api/workbench/commands', options: {method: 'POST', body: {type: 'review_stage_move', payload}}}]);
  const malformed = clientFor(async () => ({command: 'review_decision', result, snapshot_seq: 47}));
  await assert.rejects(malformed.moveReviewStage(payload), CollectorCoreProtocolError);
});

test('split audit queries preserve timezone boundaries and are independent of the snapshot cap', async () => {
  const calls = [];
  const client = clientFor(async (path, options) => {calls.push({path, options}); return {items: [], total: 0};});
  await client.splitReviewReport('2026-09-23T00:00:00-07:00', '2026-09-24T00:00:00-07:00', 2100, 100, -420);
  assert.deepEqual(calls, [{path: '/api/reports/split-review', options: {method: 'POST', body: {
    start: '2026-09-23T00:00:00-07:00', end: '2026-09-24T00:00:00-07:00', offset: 2100, limit: 100, utc_offset_minutes: -420,
  }}}]);
});

test('both review reports forward exact daily calendar boundaries in the same paginated query', async () => {
  const calls = [], client = clientFor(async (path, options) => {calls.push({path, options}); return {items: [], total: 0, daily_counts: {}};});
  const dailyBounds = [{key: '2026-11-01', start: '2026-11-01T00:00:00-07:00', end: '2026-11-02T00:00:00-08:00'}];
  await client.splitReviewReport(dailyBounds[0].start, dailyBounds[0].end, 100, 100, -480, dailyBounds);
  await client.privateFollowReviewReport(dailyBounds[0].start, dailyBounds[0].end, 100, 100, -480, dailyBounds);
  for(const [index, endpoint] of ['/api/reports/split-review', '/api/reports/private-follow-review'].entries())
    assert.deepEqual(calls[index], {path: endpoint, options: {method: 'POST', body: {start: dailyBounds[0].start, end: dailyBounds[0].end, offset: 100, limit: 100, utc_offset_minutes: -480, daily_bounds: dailyBounds}}});
});
