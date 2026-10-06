import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/formal-workbench.css';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';

// This bridge is the only test double. The card, platform context, command
// fencing, snapshot refresh and confirmation dialog are the production App.
// Persist only the mock Core's durable data, so a real renderer reload cannot
// hide a target through React state or a test-only optimistic deletion.
const storageKey = 'fixture.completed-card-delete.core.v1';
type Scenario = 'initial' | 'rechecked' | 'more-found' | 'pending' | 'running' | 'error' | 'automatic' | 'cleanup-error';
type DurableState = {scenario: Scenario; revision: number; dismissed: string[]};
const stored: DurableState | null = JSON.parse(localStorage.getItem(storageKey) || 'null');
const fixture = {
  scenario: stored?.scenario || 'initial' as Scenario,
  revision: stored?.revision || 1,
  dismissed: stored?.dismissed || [] as string[],
  commands: [] as any[],
  snapshotRequests: [] as string[],
  snapshots: 0,
  snapshotFail: false,
  fail: false,
  hold: false,
  release: null as (() => void) | null,
  persist() {
    localStorage.setItem(storageKey, JSON.stringify({scenario: this.scenario, revision: this.revision, dismissed: this.dismissed}));
  },
};

function snapshot(platform = 'instagram') {
  const active = ['pending', 'running', 'error'].includes(fixture.scenario);
  const status = fixture.scenario === 'pending' ? 'paused' : fixture.scenario === 'running' ? 'running' : fixture.scenario === 'error' ? 'error' : 'completed';
  const moreFound = fixture.scenario === 'more-found';
  const processed = moreFound ? 437 : 411;
  const saved = moreFound ? 343 : 317;
  const gap = 537 - processed;
  const recheckState = fixture.scenario === 'initial' ? null : active ? fixture.scenario : 'completed';
  const coverage = {
    source_total: 537, discovered: processed, processed,
    discovery_finished: !active, unobserved_count: gap,
    remaining_count: gap, pending_count: 0, status: 'unobserved', end_reason: !active ? 'visible_list_end' : null,
  };
  const primary = {
    id: 'completed-target', username: 'completed_source', status,
    current_window_id: active || ['automatic','cleanup-error'].includes(fixture.scenario) ? 'completed-window' : null,
    current_stage: active ? 'screening_accounts' : 'completed_archived',
    completion_policy: ['automatic','cleanup-error'].includes(fixture.scenario) ? 'automatic' : undefined,
    collection_list_dismissed: fixture.dismissed.includes('completed-target'),
    source_recheck: recheckState ? {mode: 'followers', state: recheckState, completed_at: recheckState === 'completed' ? '2026-10-01T10:00:00Z' : null} : null,
    mode_progress: {followers: {source_total: 537, discovered: processed, processed, saved, skipped_global_duplicates: 94}},
    mode_coverage: {followers: coverage},
  };
  const sibling = {
    id: 'sibling-target', username: 'retained_sibling', status: 'completed',
    current_window_id: null, current_stage: 'completed_archived', source_recheck: null,
    collection_list_dismissed: fixture.dismissed.includes('sibling-target'),
    mode_progress: {followers: {source_total: 73, discovered: 61, processed: 61, saved: 50, skipped_global_duplicates: 11}},
    mode_coverage: {followers: {source_total: 73, discovered: 61, processed: 61, discovery_finished: true, unobserved_count: 12, remaining_count: 12, pending_count: 0, status: 'unobserved', end_reason: 'visible_list_end'}},
  };
  const tasks = [{
    id: 'completed-task', status, modes: ['followers'],
    settings: {platform: 'instagram', parallel_screening_workers: 1},
    targets: [primary, ...Array.from({length: 150}, (_, index) => ({...sibling, id: `sibling-${index}`}))], windows: [{profile_id: 'completed-window'}],
    runtime: {profile_states: fixture.scenario === 'cleanup-error' ? [{profile_id:'completed-window',current_target_id:null,state:'manual_required',reason:'browser_close_failed',message:'关闭失败，请重试'}] : active ? [{profile_id: 'completed-window', current_target_id: 'completed-target', state: status}] : []},
  }];
  return {
    revision: fixture.revision, generated_at: new Date().toISOString(), platform,
    counts: {total_collected: saved + 50, total_public: saved + 30, total_private: 20, total_split: 2, pending_public: 0, pending_private: 0},
    dedupe: {total: 602831}, pending: {public: [], private: []},
    approved: {public: [{id: 'retained-account', username: 'retained_collected_account', platform: 'instagram', profile: {}}], private: []},
    history: {manual_rejections: [], collection_exclusions: [], approvals: [{id: 'retained-history', username: 'retained_history_account', platform: 'instagram', profile: {}}]},
    windows: [{id: 'completed-window', name: 'Window 1', ready: true, opened: true}],
    sources: [], tasks: platform === 'instagram' ? tasks : [], campaigns: [], split_candidates: [],
    truncated: false, connection: {connected: true, provider: 'native'},
  };
}

Object.assign(window, {completedCardFixture: Object.assign(fixture, {snapshot})});
(window as any).collectorCore = {
  secureGet: async () => 'fixture-token', secureSet: async () => true, secureDelete: async () => true,
  configureIntegrations: async () => ({restarted: true}),
  async request(path: string, options: any) {
    if (path === '/api/session/resume') return {session_token: 'fixture-token', user: {id: 'fixture-owner', username: 'fixture'}};
    if (path.startsWith('/api/workbench/snapshot')) {
      fixture.snapshots++;
      const platform = new URL('http://core' + path).searchParams.get('platform') || 'instagram';
      fixture.snapshotRequests.push(platform);
      if (fixture.snapshotFail) throw Error('completed-card snapshot fixture failure');
      fixture.persist();
      return structuredClone(snapshot(platform));
    }
    if (path === '/api/workbench/commands') {
      const command = structuredClone(options?.body);
      fixture.commands.push(command);
      const payload = command?.payload;
      if (payload?.task_id !== 'completed-task' || payload?.target_id !== 'completed-target' || payload?.platform !== 'instagram')
        throw Error('Unexpected fixture mutation target or platform');
      if (command.type !== 'task_source_recheck' && !(command.type === 'task_target_control' && payload.action === 'dismiss_completed'))
        throw Error('Unexpected fixture mutation');
      if (command.type === 'task_source_recheck' && payload.mode !== 'followers') throw Error('Unexpected fixture recheck mode');
      if (fixture.hold) await new Promise<void>(resolve => { fixture.release = () => {fixture.release = null; resolve();}; });
      if (fixture.fail) throw Error('completed-card mutation fixture failure');
      if (command.type === 'task_source_recheck') fixture.scenario = 'pending';
      else {
        if (snapshot().tasks[0].targets[0].status !== 'completed') throw Error('Only completed fixture targets can be dismissed');
        if (!fixture.dismissed.includes(payload.target_id)) fixture.dismissed.push(payload.target_id);
      }
      fixture.revision++;
      fixture.persist();
      return {command: command.type, snapshot_seq: fixture.revision, result: command.type === 'task_source_recheck'
        ? {status: 'paused', waiting_for_task_resume: true}
        : {status: 'completed', collection_list_dismissed: true}};
    }
    if (path === '/api/workbench/live-status') return {revision: fixture.revision, generated_at: new Date().toISOString(), tasks: []};
    throw Error('Fixture has no optional data');
  },
};
createRoot(document.getElementById('root')!).render(<App/>);
