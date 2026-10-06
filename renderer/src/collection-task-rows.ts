import type { CoreCollectionTask, CoreSplitCandidate, CoreTaskTarget } from "./core-client";

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === "string" ? value : "";
const status = (value: unknown): string => text(value).toLowerCase();

export type CollectionWindowRow = { task: CoreCollectionTask; target: CoreTaskTarget | null; profileId: string };

export function collectionRecheckCompleted(target: CoreTaskTarget | null): boolean {
  return target?.status === "completed" && target.source_recheck?.state === "completed";
}

/** Legacy coverage helper; collection task cards no longer render history. */
export function collectionCompletedGapVisible(target: CoreTaskTarget): boolean {
  return target.status === "completed" && target.collection_list_dismissed !== true
    && Object.values(target.mode_coverage || {}).some(evidence => evidence.discovery_finished === true
      && Number.isSafeInteger(evidence.unobserved_count) && Number(evidence.unobserved_count) > 0);
}

/** Normal completed sources disappear immediately; only a real owned-window
 * cleanup failure may retain a control. Rendering never mutates/release leases. */
export function collectionCompletionCleanupProfile(task: CoreCollectionTask, target: CoreTaskTarget): Record<string, unknown> | undefined {
  if (target.status !== "completed" || target.completion_policy !== "automatic" || target.collection_list_dismissed === true) return;
  const profiles = record(task.runtime).profile_states;
  return (Array.isArray(profiles) ? profiles.map(record) : []).find(profile =>
    ["browser_close_failed", "source_recheck_cleanup_failed"].includes(text(profile.reason))
      && ["manual_required", "manual_intervention", "stopped", "failed", "recoverable"].includes(status(profile.state || profile.status))
      && (profile.current_target_id === target.id || (!profile.current_target_id
        && (text(profile.profile_id) || text(profile.window_id)) === target.current_window_id)));
}

export function collectionCompletionCleanupRows(tasks: CoreCollectionTask[]): Array<{task: CoreCollectionTask; target: CoreTaskTarget}> {
  const windows = new Map<string, {task: CoreCollectionTask; target: CoreTaskTarget}>();
  for (const task of tasks) for (const target of task.targets || []) {
    const profile = collectionCompletionCleanupProfile(task, target);
    if (profile) windows.set(`${task.id}\u0000${text(profile.profile_id) || text(profile.window_id)}`, {task, target});
  }
  return [...windows.values()];
}

/** A header difference alone never authorizes a rescan: require Core's end proof. */
export function collectionSourceRecheckModes(target: CoreTaskTarget | null): Array<"followers" | "following"> {
  if (!target || target.current_stage === "deleted_archived" || target.collection_list_dismissed === true || collectionRecheckCompleted(target)) return [];
  return (["followers", "following"] as const).filter(mode => {
    const evidence = target.mode_coverage?.[mode];
    return evidence?.discovery_finished === true && Number.isSafeInteger(evidence.unobserved_count)
      && Number(evidence.unobserved_count) > 0;
  });
}

export function collectionRecheckNeedsPause(state: string): boolean {
  return !["running", "working", "collecting", "processing", "active", "completed", "paused", "recoverable", "stopped", "failed", "error"].includes(status(state));
}
export type CollectionFailurePresentation = {
  rows: Array<CollectionWindowRow & { failure: CoreSplitCandidate | null }>;
  orphanFailures: CoreSplitCandidate[];
};

/** A pending failure and its retained execution target are two views of one source. */
export function collectionFailurePresentation(
  rows: CollectionWindowRow[], candidates: CoreSplitCandidate[],
): CollectionFailurePresentation {
  const pending = candidates.filter(candidate => candidate.kind === "failure" && candidate.queue_state === "available");
  const bySource = new Map<string, CoreSplitCandidate>();
  for (const candidate of pending) {
    if (candidate.source_task_id && candidate.source_target_id)
      bySource.set(`${candidate.source_task_id}\u0000${candidate.source_target_id}`, candidate);
  }
  const shown = new Set<string>();
  const linkedRows = rows.map(row => {
    const target = row.target;
    const candidate = target?.manual_recovery_required === true
      ? bySource.get(`${row.task.id}\u0000${target.id}`) || null : null;
    if (candidate) shown.add(candidate.id);
    return { ...row, failure: candidate };
  });
  return { rows: linkedRows, orphanFailures: pending.filter(candidate => !shown.has(candidate.id)) };
}

const collectionCount = (value: unknown): number | null =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;

/** A display-only unseen gap, shared by the counters and manual recheck entry. */
export function collectionModeUnseenCount(progress: Record<string, unknown>): number | null {
  const total = collectionCount(progress.source_total), discovered = collectionCount(progress.discovered);
  return total !== null && discovered !== null ? Math.max(0, total - discovered) : null;
}

export type CollectionProgressItem = { key: string; text: string; title?: string; label?: string; value?: number | string };

export function collectionModeProgressItems(mode: string, progress: Record<string, unknown>): CollectionProgressItem[] {
  const count = (key: string): number | null => collectionCount(progress[key]);
  const sourceTotal = count("source_total"), processed = count("processed");
  const discovered = count("discovered"), deduped = count("skipped_global_duplicates");
  const discarded = count("discarded"), qualified = count("qualified_for_review");
  const modeLabel = mode === "followers" ? "粉丝" : mode === "following" ? "关注" : mode;
  // Project the durable counters without modifying their backend semantics.
  // Unseen excludes every identified account, including the pending queue.
  // Header totals can be rounded or change; never cap actual identity counts.
  // Discarded already includes hover exclusions, so do not add them twice.
  const unseen = collectionModeUnseenCount(progress);
  const queue = discovered !== null && processed !== null && processed <= discovered ? discovered - processed : null;
  const metric = (key: string, label: string, value: number | string, title: string): CollectionProgressItem =>
    ({ key, label, value, text: `${label} ${value}`, title });
  const parts = [
    metric("source", `${modeLabel}总数`, sourceTotal ?? "读取中", "主页本次读取的显示数量，可能经过平台取整或随时间变化；未读取时不记为0"),
    metric("discovered", "已识别", discovered ?? "—", "列表已识别并持久保存的不同账号数，包含已处理账号和队列，不重复累计同一账号"),
    metric("unobserved", "未识别", unseen ?? "—", "主页显示总数减已识别数（最低为0）；不包含已识别的队列，平台取整或数量变化时仅表示显示差额"),
    metric("deduped", "去重", deduped ?? "—", "持久去重确认跳过的账号数，不包含未识别差额"),
    metric("pending-work", "队列", queue ?? "—", "已识别但尚未完成处理的账号数，包含正在处理的账号，不计入未识别"),
    metric("discarded", "丢弃", discarded ?? "—", "规则确认不合格的账号总数，已包含悬浮卡丢弃"),
    metric("qualified", "合格", qualified ?? "—", "实际通过采集筛选并进入审核的账号数"),
  ];
  if ((sourceTotal !== null && (processed !== null && processed > sourceTotal || discovered !== null && discovered > sourceTotal))
    || (discovered !== null && processed !== null && processed > discovered))
    parts.push({ key: "count-warning", text: "数量口径不一致，需核实", title: "保留实际记录；主页总数可能变化，不强行对齐" });
  return parts;
}

export function collectionModeProgressText(mode: string, progress: Record<string, unknown>): string {
  return collectionModeProgressItems(mode, progress).map(item => item.text).join(" · ");
}

export function collectionTaskRows(task: CoreCollectionTask): CollectionWindowRow[] {
  if (status(task.status) === "completed") return [];
  const targets = (Array.isArray(task.targets) ? task.targets : []).filter((target) => {
    if (status(target.status) === "completed") return false;
    if (status(target.current_stage) === "deleted_archived") return false;
    return !(status(target.status) === "stopped"
      && status(target.current_stage) === "interrupted_recoverable"
      && !target.manual_recovery_required && !target.current_window_id && !target.preferred_window_id);
  });
  const runtime = record(task.runtime);
  const profiles = Array.isArray(runtime.profile_states) ? runtime.profile_states.map(record) : [];
  const windows = new Set<string>();
  for (const window of task.windows || []) if (window.profile_id) windows.add(window.profile_id);
  for (const profile of profiles) {
    const id = text(profile.profile_id) || text(profile.window_id);
    if (id) windows.add(id);
  }
  for (const target of targets) {
    const id = target.current_window_id || target.preferred_window_id;
    if (id) windows.add(id);
  }
  return [...windows].flatMap((profileId) => {
    const profile = profiles.find((item) => (text(item.profile_id) || text(item.window_id)) === profileId);
    // A preferred window or stale runtime target cannot override the window
    // that actually owns the target after a handoff/recovery.
    const target = targets.find((item) => item.current_window_id === profileId)
      || targets.find((item) => item.id === profile?.current_target_id && !item.current_window_id)
      || targets.find((item) => !item.current_window_id && item.preferred_window_id === profileId)
      || null;
    if (!target) {
      // Idle pool windows have no execution card. Keep a real in-flight worker
      // visible when its target is absent from a bounded snapshot detail page.
      const active = new Set(["working", "running", "starting", "opening", "collecting",
        "waiting_network", "paused", "manual_required", "manual_intervention", "auth_required", "blocked_by_auth"]);
      if (!profile || !active.has(status(profile.state || profile.status))) return [];
      const formerTarget = (task.targets || []).find((item) => item.id === profile.current_target_id);
      if (formerTarget?.status === "completed" || status(formerTarget?.current_stage) === "deleted_archived") return [];
    }
    return [{ task, target, profileId }];
  });
}

export function collectionRowStatus(targetStatus: unknown, profileState: unknown, taskStatus: unknown): string {
  const durable = status(targetStatus);
  if (["completed", "recoverable", "failed", "stopped"].includes(durable)) return durable;
  const live = status(profileState);
  if (["working", "starting", "opening", "collecting"].includes(live)) return "running";
  if (durable && (!live || ["idle", "closed", "stopped"].includes(live))) return durable;
  return live || durable || status(taskStatus);
}

export function collectionWindowProgress(
  profile: Record<string, unknown> | undefined,
  target: Record<string, unknown> | null,
  rowStatus: string,
  now = Date.now(),
): { lastProgressAt: string | null; stalled: boolean } {
  // Activity in a sibling window, a heartbeat or a task status change cannot
  // prove this window advanced. A new target must not inherit its predecessor.
  const sameTarget = !profile?.current_target_id || profile.current_target_id === target?.id;
  const raw = sameTarget && profile ? profile.last_progress_at : target?.last_success_at;
  const lastProgressAt = typeof raw === "string" && Number.isFinite(Date.parse(raw)) ? raw : null;
  const running = ["running", "working", "collecting", "processing", "active"].includes(status(rowStatus));
  // Starting another recovery attempt does not itself confirm progress. Keep
  // the same target's last real advance visible even during recovery checks.
  return { lastProgressAt, stalled: Boolean(target && running && lastProgressAt
    && now - Date.parse(lastProgressAt) > 180_000) };
}

export function collectionRecoveryAction(rowStatus: string, options: {
  stalled: boolean; authRequired: boolean; pageRecoveryExhausted: boolean; manualRequired?: boolean;
}): { action: "resume" | "restart"; label: string; message: string } | null {
  const state = status(rowStatus);
  if (["completed", "success", "succeeded"].includes(state)) return null;
  if (options.authRequired || options.manualRequired)
    return { action: "resume", label: "处理后继续", message: "已请求重新检查该窗口，并从检查点继续" };
  if (state === "waiting_network") return { action: "resume", label: "立即重试", message: "已请求该窗口提前执行本次自动恢复" };
  if (options.stalled)
    return { action: "restart", label: "检查点重启", message: "已请求该窗口从检查点重新启动" };
  if (["failed", "error", "stopped"].includes(state)) return { action: "restart", label: "继续", message: "已请求继续该窗口任务" };
  if (["paused", "recoverable"].includes(state)) return { action: "resume", label: "继续", message: "已请求继续该窗口任务" };
  return null;
}

export function collectionRecoveryDetails(profile: Record<string, unknown> | undefined, rowStatus: string) {
  const state = status(rowStatus);
  const kind = text(profile?.recovery_kind);
  const reason = text(profile?.reason);
  const originalReason = text(profile?.original_reason) || reason;
  // A reason describes a past failure. Only the current state can authorize an
  // automatic retry; paused/stopped rows must not inherit an old retry promise.
  const manualRequired = ["auth_required", "blocked_by_auth", "manual_intervention", "manual_required", "challenge"].includes(state)
    || (state === "waiting_network" && ["global_auth", "profile_intervention"].includes(kind));
  const automatic = state === "waiting_network" && status(profile?.state || profile?.status) === "waiting_network"
    && !manualRequired;
  const verifyingRecovery = ["running", "working", "collecting", "processing", "active"].includes(state)
    && profile?.recovery_in_progress === true && profile?.progress_confirmed !== true;
  const pageRecovery = (automatic || verifyingRecovery) && (kind === "instagram_page" || reason === "instagram_page_recovery_exhausted"
    || /^instagram_(profile|location|relationship|surface|followers_list|following_list)_/.test(reason));
  const reasonLabels: Record<string, string> = {
    instagram_network_unavailable: "网络暂时不可用",
    instagram_hover_card_unavailable: "当前账号悬浮卡未能确认，等待继续读取",
    worker_not_connected: "浏览器读取连接尚未恢复",
    browser_context_missing: "浏览器页面会话暂时不可用",
    browser_operations_pending: "上一轮页面操作仍在收尾",
    screening_slots_busy: "筛选子页仍在收尾，等待页槽释放",
    parallel_screening_page_create_timeout: "筛选子页创建超时，等待自动重试",
    instagram_profile_not_ready: "账号主页尚未加载完成",
    instagram_profile_wrong_target: "页面尚未切换到当前账号",
    instagram_profile_dom_unrecognized: "账号资料暂时无法确认",
    instagram_profile_temporarily_unavailable: "账号主页暂时无法读取",
    instagram_location_load_failed: "账号地区资料暂时加载失败",
    instagram_location_temporarily_unavailable: "账号地区资料暂时无法读取",
    instagram_relationship_list_no_progress: "名单暂时没有继续推进",
    instagram_surface_no_progress: "页面暂时没有继续推进",
    instagram_followers_list_incomplete: "粉丝名单尚未读取完成",
    instagram_following_list_incomplete: "关注名单尚未读取完成",
    instagram_followers_list_not_rendered: "粉丝名单尚未加载出来",
    instagram_following_list_not_rendered: "关注名单尚未加载出来",
    browser_window_surface_unstable: "浏览器页面暂时不稳定",
    instagram_page_recovery_exhausted: "本轮页面恢复尚未成功",
  };
  let manualMessage = "请检查该窗口页面，处理提示后继续；原任务与进度已保留";
  if (reason === "browser_close_failed")
    manualMessage = "采集已完成，窗口尚未确认关闭；请重试关闭，关闭确认前保留窗口占用，不会开始新的采集";
  else if (reason === "source_recheck_cleanup_failed")
    manualMessage = "复查前的页面清理未成功；点击“重试清理”，成功前保留窗口占用和原检查点";
  else if (["auth_required", "blocked_by_auth"].includes(state) || kind === "global_auth")
    manualMessage = "浏览器服务登录已失效，请在设置中重新登录后继续";
  else if (originalReason === "instagram_login_required") manualMessage = "请在该窗口完成 Instagram 登录后继续";
  else if (originalReason === "instagram_challenge") manualMessage = "请在该窗口完成 Instagram 验证后继续";
  else if (["instagram_rate_limited", "instagram_action_blocked"].includes(originalReason))
    manualMessage = "Instagram 已提示操作限制，请按页面要求等待，确认解除后继续";
  else if (originalReason === "browser_operations_pending")
    manualMessage = "该窗口上一轮页面操作尚未结束，请等待清理完成后继续";
  const retryAt = text(profile?.next_retry_at);
  return {
    automatic,
    verifyingRecovery,
    manualRequired,
    pageRecovery,
    nextRetryAt: automatic && Number.isFinite(Date.parse(retryAt)) ? retryAt : null,
    reasonLabel: automatic || verifyingRecovery ? reasonLabels[originalReason] || reasonLabels[reason]
      || text(profile?.message) || (pageRecovery ? "页面暂时不可用" : "连接暂时不可用") : "",
    manualMessage: manualRequired ? manualMessage : "",
    candidateUsername: automatic || verifyingRecovery ? text(profile?.candidate_username).replace(/^@+/, "") : "",
    sourceDiscoveryComplete: (automatic || verifyingRecovery) && profile?.source_discovery_complete === true,
  };
}
