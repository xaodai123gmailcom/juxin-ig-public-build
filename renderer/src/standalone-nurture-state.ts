/** Standalone nurture only. Collection/Reels settings have a separate contract. */
export type NurtureAccountSnapshot = {
  username?: string; followers_count?: number | null; following_count?: number | null;
  posts_count?: number | null; checked_at?: string; status?: string; message?: string;
};
export type NurtureJob = {
  id: string; kind: string; profile_id: string; status: string; deleted_at?: string | null;
  wait_reason?: string; wait_message?: string; message?: string; created_at: string; due_at?: string;
  cursor?: number; total_steps?: number; config?: Record<string, unknown>;
  result: {
    window_hold?: boolean;
    window_cleanup?: {state?: string; last_error?: string; confirmed_at?: string; reconciled_at?: string};
    account_snapshot?: NurtureAccountSnapshot; nurture_started_at?: string; nurture_finished_at?: string;
    nurture_actual_seconds?: number; counts?: Record<string, number>;
    failure?: {stage?: string; message?: string; at?: string};
    nurture_observations?: Array<{action: string; status: string; detail: string; step: number}>;
  };
};
export const standaloneNurtureDefaults = {minutes: 5, concurrency: 0};
export const nurtureTerminalStatuses = new Set(['completed', 'failed', 'cancelled', 'needs_review']);
const reservedStatuses = new Set(['queued', 'waiting_window', 'running', 'paused', 'needs_review']);
export function nurtureNeedsCleanup(job: NurtureJob): boolean {
  return job.kind === 'nurture' && job.status === 'completed' && Boolean(job.result.window_hold);
}
export function nurtureCleanupLabel(job: NurtureJob): string {
  const state = job.result.window_cleanup?.state;
  if (nurtureNeedsCleanup(job)) return state === 'lease_lost' ? '占用凭证已失效，等待安全核验' : '窗口清理尚待确认';
  if (state === 'reconciled_closed') return '已核验窗口关闭，本条历史清理占用已解除';
  return state === 'closed' ? '窗口已关闭并释放' : state ? `清理状态：${state}` : '';
}
export function nurtureVisibleJobs(jobs: NurtureJob[], tab: string, recentIds: string[] = []): NurtureJob[] {
  const recent = new Set(recentIds);
  return jobs.filter(job => job.kind === 'nurture' && (tab === 'history'
    ? nurtureTerminalStatuses.has(job.status)
    : nurtureNeedsCleanup(job) || (!job.deleted_at && (tab === 'errors'
      ? ['failed', 'needs_review'].includes(job.status)
      : !nurtureTerminalStatuses.has(job.status) || recent.has(job.id)))));
}
export function nurtureReservedWindows(jobs: NurtureJob[], activeIds: string[]): Set<string> {
  const active = new Set(activeIds);
  // All studio jobs reserve their window; pending work cannot accept a second task.
  // Match admission's completed-nurture cleanup fence. Legacy flags on failed,
  // cancelled or other-kind history do not independently reserve a window.
  return new Set(jobs.filter(job => reservedStatuses.has(job.status) || active.has(job.id) || nurtureNeedsCleanup(job)).map(job => job.profile_id).filter(Boolean));
}
export function nurtureConfigIssue(minutes: number | string, concurrency: number | string): string {
  if (minutes === '' || !Number.isInteger(Number(minutes)) || Number(minutes) < 1 || Number(minutes) > 120) return '养号时长须为 1–120 分钟的整数';
  if (concurrency === '' || !Number.isInteger(Number(concurrency)) || Number(concurrency) < 0 || Number(concurrency) > 1000) return '同时执行窗口数量须为 0–1000 的整数，0 表示全部所选';
  return '';
}
export function nurtureSavedConcurrency(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 1000 ? value : 0;
}
export function nurtureCount(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未读取';
}
export function nurtureTimestamp(value?: string): string {
  if (!value) return '未记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', {hour12: false});
}
export function nurtureElapsed(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '未记录';
  const seconds = Math.floor(value);
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
