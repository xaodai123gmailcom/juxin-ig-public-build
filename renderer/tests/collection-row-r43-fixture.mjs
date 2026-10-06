import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import React from 'react';
import * as icons from 'lucide-react';
import ts from 'typescript';
import {collectionPlatform} from '../src/collection-platform.ts';
import * as rows from '../src/collection-task-rows.ts';
import * as coverage from '../src/split-review-report.ts';
import {formatTime} from '../src/workbench-format.ts';
import {assignmentHasJoinedWindow} from '../src/collection-window-assignment.ts';

// Execute the actual production component and helpers, without copying its
// rendering or event handlers into a separate demo implementation.
const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const ast = ts.createSourceFile('formal-workbench.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(['asRecord', 'firstText', 'firstNumber', 'normalizeStatus', 'statusClass',
  'StatusBadge', 'CollectionProgressValues', 'CollectionTaskRow', 'CollectionSourceRecheck', 'ACTIVE_STATES', 'PAUSED_STATES',
  'RESUMABLE_STATES', 'FAILED_STATES', 'SUCCESS_STATES', 'CollectionClaimLockControl', 'WaitingSplitTargetRow']);
const chosen = ast.statements.filter(node => (ts.isFunctionDeclaration(node) && names.has(node.name?.text))
  || (ts.isVariableStatement(node) && node.declarationList.declarations.some(d => names.has(d.name.getText(ast)))));
assert.equal(chosen.length, names.size);
const compiled = ts.transpileModule(chosen.map(node => node.getText(ast)).join('\n'), {
  fileName: 'actual-collection-row.tsx', compilerOptions: {jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022},
  reportDiagnostics: true,
});
assert.equal(compiled.diagnostics?.filter(d => d.category === ts.DiagnosticCategory.Error).length, 0);
const {Row, ClaimLock, WaitingRow} = runInNewContext(compiled.outputText + '\n({Row:CollectionTaskRow,ClaimLock:CollectionClaimLockControl,WaitingRow:WaitingSplitTargetRow});',
  {React, ...icons, ...rows, ...coverage, collectionPlatform, formatTime, assignmentHasJoinedWindow});

export function collectionClaimLockFixture({globallyLocked = false, locked = false, pending = false, specified = false} = {}) {
  const calls = [], runs = [], disabledKeys = [];
  const candidate = {id:'waiting-1', username:'waiting_account', kind:'manual', queue_state:'queued', locked,
    allowed_window_ids:specified ? ['w1','w2'] : []};
  const client = {setSplitClaimLocked:async value=>calls.push(['all',value]),
    setWaitingSplitTargetLocked:async (id,value)=>calls.push(['candidate',id,value])};
  const run = async (key, action, message) => {runs.push({key,message});await action(client);return true;};
  const disabled = key => {disabledKeys.push(key);return pending;};
  return {candidate,calls,runs,disabledKeys,
    global:()=>ClaimLock({locked:globallyLocked,run,disabled}),
    waiting:()=>WaitingRow({candidate,index:0,tasks:[],globallyLocked,run,disabled,
      onAssign:candidate=>calls.push(['assign',candidate.id]), onDelete:async id=>calls.push(['delete',id])}),
  };
}

export function collectionRowFixture(overrides = {}) {
  const commands = [], runs = [], disabledKeys = [];
  const profile = {profile_id: 'window-9', current_target_id: 'target-9', state: 'working',
    last_progress_at: new Date(Date.now() - 17 * 60_000).toISOString(),
    current_stage: 'recovering_page', recovery_in_progress: true, progress_confirmed: false,
    reason: 'instagram_page_recovery_exhausted', original_reason: 'instagram_relationship_list_no_progress',
    ...overrides.profile};
  const target = overrides.target === null ? null : {id: 'target-9', username: 'sample_account',
    status: 'running', current_window_id: 'window-9',
    mode_progress: {followers: {source_total: 167, processed: 164, saved: 118}}, ...overrides.target};
  const task = {id: 'collection-1', status: 'running', modes: ['followers'], settings: {},
    runtime: {profile_states: [profile]}, updated_at: new Date().toISOString(), ...overrides.task};
  const client = {controlCollectionWindow: async (...args) => { commands.push(args); }};
  let pending = Boolean(overrides.pending);
  const props = {task, target, profileIdOverride: 'window-9', windowNames: new Map([['window-9', '9']]),
    run: async (key, action, message) => {runs.push({key, message}); await action(client); return true;},
    disabled: key => {disabledKeys.push(key); return pending;}, onDeleteTask: async () => {},
    collectionControls: {feedback: () => ({safetyControlDisabled: pending}),
      run: async (key, requests, message) => {runs.push({key, requests, message}); return true;}},
  };
  return {commands, runs, disabledKeys, props, render: () => Row(props), setPending: value => {pending = value;}};
}

export function elementNodes(element) {
  if (!element || typeof element !== 'object') return [];
  if (Array.isArray(element)) return element.flatMap(elementNodes);
  return [element, ...elementNodes(element.props?.children)];
}
export function elementText(element) {
  if (typeof element === 'string' || typeof element === 'number') return String(element);
  if (Array.isArray(element)) return element.map(elementText).join('');
  return element && typeof element === 'object' ? elementText(element.props?.children) : '';
}
export function buttonByText(element, label) {
  return elementNodes(element).find(node => node.type === 'button' && elementText(node) === label);
}
