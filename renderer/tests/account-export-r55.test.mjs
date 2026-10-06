import test from 'node:test';
import assert from 'node:assert/strict';
import { createCollectorCoreClient } from '../src/core-client.ts';
import { downloadAccountExport } from '../src/account-export.ts';

test('approved public/private exports request a complete database export or explicit selected IDs', async () => {
  const calls = [];
  const result = { row_count: 2200, csv: '\ufeff账号\r\n' };
  const client = createCollectorCoreClient({ request: async (path, options) => { calls.push({ path, options }); return result; },
    secureSet: async () => true, secureGet: async () => null, secureDelete: async () => true,
    configureIntegrations: async () => ({ restarted: false }) });
  const all = { visibility: 'public', scope: 'all' };
  const selected = { visibility: 'private', scope: 'selected', candidate_ids: ['id-a', 'id-b'] };
  assert.strictEqual(await client.exportApprovedAccounts(all), result);
  assert.strictEqual(await client.exportApprovedAccounts(selected), result);
  assert.deepEqual(calls, [all, selected].map(body => ({ path: '/api/workbench/accounts/export', options: { method: 'POST', body } })));
});

const file = { filename: 'Juxin-public-accounts-20260924-123000.csv', mime_type: 'text/csv;charset=utf-8',
  csv: '\ufeff账号,显示名称\r\nsomebody,中文\r\n', row_count: 1, skipped_count: 0, visibility: 'public', scope: 'all' };

test('CSV download sends BOM bytes and filename through a real download anchor with delayed URL cleanup', async () => {
  const previous = { document: globalThis.document, window: globalThis.window, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  let blob, appended, timer, clicked = false, removed = false, revoked;
  const link = { style: {}, click() { clicked = true; }, remove() { removed = true; } };
  try {
    globalThis.document = { createElement(tag) { assert.equal(tag, 'a'); return link; }, body: { appendChild(value) { appended = value; } } };
    globalThis.window = { setTimeout(fn, ms) { assert.equal(ms, 30_000); timer = fn; } };
    URL.createObjectURL = value => { blob = value; return 'blob:download-fixture'; };
    URL.revokeObjectURL = value => { revoked = value; };
    downloadAccountExport(file);
    assert.equal(link.download, file.filename);
    assert.equal(link.href, 'blob:download-fixture');
    assert.strictEqual(appended, link);
    assert.equal(clicked, true); assert.equal(removed, true);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    assert.deepEqual([...bytes.slice(0, 3)], [239, 187, 191]);
    assert.equal(blob.type, 'text/csv;charset=utf-8');
    assert.equal(revoked, undefined);
    timer(); assert.equal(revoked, 'blob:download-fixture');
  } finally {
    globalThis.document = previous.document; globalThis.window = previous.window;
    URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke;
  }
});

test('malformed/empty download responses cannot create files', () => {
  for (const changes of [{ filename: '../escape.csv' }, { row_count: 0 }, { row_count: -1 },
    { csv: '<script>' }, { mime_type: 'text/html' }]) {
    assert.throws(() => downloadAccountExport({ ...file, ...changes }), /导出文件无效/);
  }
});
