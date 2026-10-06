import test from 'node:test';
import assert from 'node:assert/strict';
import {renderToStaticMarkup} from 'react-dom/server';
import {collectionTaskRows} from '../src/collection-task-rows.ts';
import {collectionRowFixture, buttonByText, elementText} from './collection-row-r43-fixture.mjs';

test('actual collection card separates direct deletion and requeue with target-bound commands', async () => {
  const f = collectionRowFixture();
  const row = f.render();
  const direct = buttonByText(row, '直接删除');
  const requeue = buttonByText(row, '退回等待');
  assert.ok(direct && requeue);
  assert.equal(buttonByText(row, '删除并退回等待'), undefined);
  assert.equal(elementText(direct), '直接删除');
  assert.equal(elementText(requeue), '退回等待');
  direct.props.onClick();
  await Promise.resolve();
  requeue.props.onClick();
  await Promise.resolve();
  assert.deepEqual(f.commands, [
    ['collection-1', 'window-9', 'delete_only', 'target-9'],
    ['collection-1', 'window-9', 'delete', 'target-9'],
  ]);
  assert.equal(f.runs[0].key, f.runs[1].key);
  assert.match(f.runs[0].message, /不退回等待/);
  assert.match(f.runs[1].message, /目标已退回等待/);
});

test('both removal choices use the same pending key as other window mutations', () => {
  const f = collectionRowFixture({pending: true});
  const row = f.render();
  for (const label of ['直接删除', '退回等待', '检查点重启', '暂停', '停止'])
    assert.equal(buttonByText(row, label)?.props.disabled, true, label);
  assert.ok(f.disabledKeys.length >= 3);
  assert.deepEqual([...new Set(f.disabledKeys)], ['collection-window-collection-1-window-9']);
});

test('completion and empty pool rows only remove their record and never offer requeue', async () => {
  for (const [overrides, label, targetId] of [
    [{target: {status: 'completed'}}, '移除完成记录', 'target-9'],
    [{target: null, profile: {current_target_id: '', state: 'idle'}}, '移除等待窗口', undefined],
  ]) {
    const f = collectionRowFixture(overrides);
    const row = f.render();
    assert.equal(buttonByText(row, '退回等待'), undefined);
    assert.equal(buttonByText(row, '直接删除'), undefined);
    const remove = buttonByText(row, label);
    assert.ok(remove);
    assert.match(remove.props.title, /不退回等待/);
    remove.props.onClick();
    await Promise.resolve();
    assert.deepEqual(f.commands, [['collection-1', 'window-9', 'delete_only', targetId]]);
  }
});

test('a durably deleted target stays hidden despite a stale live runtime worker', () => {
  const target = {id: 'deleted', status: 'stopped', current_stage: 'deleted_archived',
    current_window_id: '', preferred_window_id: ''};
  const input = {id: 'task', status: 'running', targets: [target], windows: [{profile_id: 'w1'}],
    runtime: {profile_states: [{profile_id: 'w1', current_target_id: 'deleted', state: 'working'}]}};
  assert.deepEqual(collectionTaskRows(input), []);
  assert.equal(input.targets.length, 1, 'history remains available');
});

test('actual recovery card warns on stalled progress, gives cause, and preserves pause and stop', () => {
  const row = collectionRowFixture().render();
  const html = renderToStaticMarkup(row);
  assert.match(html, /名单暂时没有继续推进/);
  assert.match(html, /超过 3 分钟没有确认新的采集进展/);
  for (const label of ['检查点重启', '暂停', '停止', '直接删除', '退回等待']) assert.ok(buttonByText(row, label));
  assert.doesNotMatch(elementText(row), /已采集完成|全部采集成功/);
});

test('close failure retries only closure through the existing target-bound resume command', async () => {
  for(const target of [null, {status: 'running'}]) {
    const f = collectionRowFixture({target, profile: {state: 'manual_required', reason: 'browser_close_failed',
      current_stage: 'disconnecting', recovery_in_progress: false, current_target_id: target ? 'target-9' : ''}});
    const row = f.render(), retry = buttonByText(row, '重试关闭');
    assert.ok(retry); assert.equal(retry.props.disabled, false);
    assert.match(retry.props.title, /仅重试关闭窗口/);
    assert.match(retry.props.title, /关闭确认前保留窗口占用/);
    assert.match(retry.props.title, /不会开始新的采集/);
    assert.equal(buttonByText(row, '处理后继续'), undefined);
    assert.equal(buttonByText(row, '检查点重启'), undefined);
    assert.match(elementText(row), /窗口尚未确认关闭/);
    assert.doesNotMatch(elementText(row), /等待领取下一个分裂号|等待处理页面提示|处理提示后继续/);
    if(!target) assert.match(elementText(row), /采集已完成，等待关闭窗口/);
    retry.props.onClick(); await Promise.resolve();
    assert.deepEqual(f.commands, [['collection-1', 'window-9', 'resume', target ? 'target-9' : undefined]]);
    assert.equal(f.runs[0].key, 'collection-window-collection-1-window-9');
    assert.equal(f.runs[0].message, '已请求重试关闭窗口；关闭确认前保留窗口占用');
    assert.ok(buttonByText(f.render(), '重试关闭'), 'closure remains unconfirmed until the authoritative response changes state');
  }
});

test('retry-close shares pending disablement while unrelated manual failures keep normal recovery', () => {
  const closing = collectionRowFixture({pending: true, profile: {state: 'manual_required', reason: 'browser_close_failed'}});
  assert.equal(buttonByText(closing.render(), '重试关闭').props.disabled, true);
  assert.deepEqual([...new Set(closing.disabledKeys)], ['collection-window-collection-1-window-9']);
  const ordinary = collectionRowFixture({profile: {state: 'manual_required', reason: 'instagram_challenge', original_reason: 'instagram_challenge'}}).render();
  assert.equal(buttonByText(ordinary, '重试关闭'), undefined);
  assert.ok(buttonByText(ordinary, '处理后继续'));
  assert.match(elementText(ordinary), /完成 Instagram 验证后继续/);
  const paused = collectionRowFixture({target: {status: 'paused'}, profile: {state: 'paused', reason: 'browser_close_failed'}}).render();
  assert.equal(buttonByText(paused, '重试关闭'), undefined);
  assert.ok(buttonByText(paused, '继续'));
  assert.doesNotMatch(elementText(paused), /窗口尚未确认关闭/);
});
