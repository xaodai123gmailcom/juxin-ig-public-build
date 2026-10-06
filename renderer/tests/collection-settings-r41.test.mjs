import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import React from 'react';
import ts from 'typescript';
import { renderToStaticMarkup } from 'react-dom/server';
import { readDiscardCountLimits, parseDiscardLimit, discardCountLimitsPayload } from '../src/collection-discard-limits.ts';

const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const begin = source.indexOf('<Panel className="collection-settings-panel"');
const end = source.indexOf('<div className="collection-workbench-grid">', begin);
assert.ok(begin >= 0 && end > begin, 'The actual collection settings must precede the task workbench');
const panelSource = source.slice(begin, end).trim();
const compiled = ts.transpileModule(`const actualSettingsPanel = (${panelSource});`, {
  fileName: 'collection-settings.tsx',
  compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
});
assert.equal(compiled.diagnostics?.filter(d => d.category === ts.DiagnosticCategory.Error).length, 0);

function Panel({ title, actions, children, className }) {
  return React.createElement('section', { className }, React.createElement('h2', null, title), actions, children);
}
const Icon = () => null;
function fixture(overrides = {}) {
  const state = { followers: true, following: false, parallelScreeningWorkers: 1,
    discardLimits: readDiscardCountLimits({ public_discard_active_days_max: 30 }), ...overrides };
  const render = () => runInNewContext(compiled.outputText + '\nactualSettingsPanel;', {
    React, Panel, ListFilter: Icon, Trash2: Icon, RotateCcw: Icon, ...state,
    setFollowers: value => { state.followers = value; },
    setFollowing: value => { state.following = value; },
    setParallelScreeningWorkers: value => { state.parallelScreeningWorkers = value; },
    setDiscardLimits: update => { state.discardLimits = update(state.discardLimits); },
    parseDiscardLimit, discardLimitsError: '', setAllDiscardLimitsUnlimited: () => {},
  });
  return { state, render };
}
function nodes(element, inheritedDisabled = false) {
  if (!element || typeof element !== 'object') return [];
  if (Array.isArray(element)) return element.flatMap(child => nodes(child, inheritedDisabled));
  const disabled = inheritedDisabled || (element.type === 'fieldset' && element.props.disabled);
  return [{ ...element, effectiveDisabled: Boolean(disabled || element.props.disabled) },
    ...nodes(element.props.actions, disabled), ...nodes(element.props.children, disabled)];
}
const inputs = panel => nodes(panel).filter(node => node.type === 'input');

test('the actual unified panel contains one settings header, both source choices, fixed pages and all seven limits', () => {
  const panel = fixture().render();
  assert.equal(nodes(panel).filter(node => node.type === Panel).length, 1);
  assert.equal(panel.props.title, '采集设置');
  assert.equal(inputs(panel).filter(node => node.props.type === 'checkbox').length, 3);
  assert.deepEqual(inputs(panel).filter(node => node.props.type === 'radio').map(node => node.props.value), [1, 2, 3]);
  assert.equal(inputs(panel).filter(node => node.props.type === 'number').length, 7);
  const fields = nodes(panel).filter(node => node.type === 'fieldset');
  assert.equal(fields.length, 2);
  assert.equal(inputs(fields[0]).filter(node => node.props.type === 'number').length, 3);
  assert.equal(inputs(fields[1]).filter(node => node.props.type === 'number').length, 4);
  const html = renderToStaticMarkup(panel);
  assert.match(html, /公开帖子活跃度上限（天）/);
  assert.match(html, /启用直接丢弃/);
  assert.match(html, /全部不限/);
  assert.equal((html.match(/id="collection-discard-title"/g) || []).length, 1);
});

test('turning discard off disables only its limits, retains chosen values and leaves source and concurrency controls usable', () => {
  const f = fixture();
  const toggle = inputs(f.render().props.actions).find(node => node.props.type === 'checkbox');
  toggle.props.onChange({ target: { checked: false } });
  const current = inputs(f.render());
  assert.ok(current.filter(node => node.props.type === 'number').every(node => node.effectiveDisabled));
  assert.ok(current.filter(node => node.props.type !== 'number').every(node => !node.effectiveDisabled));
  assert.equal(f.state.discardLimits.limits.public_discard_active_days_max, '30');
  assert.equal(f.state.discardLimits.limits.private_discard_followers_max, '4000');
  toggle.props.onChange({ target: { checked: true } });
  assert.ok(inputs(f.render()).every(node => !node.effectiveDisabled));
});

test('merged controls still update independent source, concurrency and public/private discard settings', () => {
  const f = fixture();
  const sourceInputs = inputs(f.render().props.children).filter(node => node.props.type === 'checkbox');
  sourceInputs[0].props.onChange({ target: { checked: false } });
  sourceInputs[1].props.onChange({ target: { checked: true } });
  inputs(f.render()).find(node => node.props.type === 'radio' && node.props.value === 3).props.onChange();
  const updateLimit = (label, value) => inputs(f.render()).find(node => node.props['aria-label'] === label).props.onChange({ target: { value } });
  updateLimit('公开帖子活跃度上限（天）', '60');
  updateLimit('私密账号直接丢弃粉丝数量上限', '900');
  updateLimit('公开账号直接丢弃粉丝数量上限', '0');
  assert.equal(f.state.followers, false);
  assert.equal(f.state.following, true);
  assert.equal(f.state.parallelScreeningWorkers, 3);
  const payload = discardCountLimitsPayload(f.state.discardLimits);
  assert.equal(payload.public_discard_active_days_max, 60);
  assert.equal(payload.private_discard_followers_max, 900);
  assert.equal(payload.public_discard_followers_max, 0);
  assert.equal(payload.public_discard_posts_max, 4000);
});
