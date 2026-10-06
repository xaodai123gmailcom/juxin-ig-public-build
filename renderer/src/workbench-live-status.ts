import type { CoreBitBrowserWindow, CoreCollectionTask, CoreTaskTarget, CoreWorkbenchLiveStatus, CoreWorkbenchSnapshot } from "./core-client";

const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const normalizeStatus = (value: unknown): string => typeof value === "string" ? value.trim().toLowerCase() : "unknown";
const firstText = (row: Record<string, unknown>, keys: string[], fallback = "—"): string => {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return fallback;
};
const firstNumber = (row: Record<string, unknown>, keys: string[]): number | null => {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
};
const older = (incoming: unknown, saved: unknown): boolean => {
  const incomingAt = Date.parse(typeof incoming === "string" ? incoming : "");
  const savedAt = Date.parse(typeof saved === "string" ? saved : "");
  return Number.isFinite(savedAt) && (!Number.isFinite(incomingAt) || incomingAt < savedAt);
};

function mergeLiveTask(baseline: CoreCollectionTask, incoming: CoreCollectionTask, runtime?: Record<string, unknown>): CoreCollectionTask {
  if (incoming.targets_partial !== true) {
    return { ...baseline, ...incoming, targets_partial: incoming.targets_partial,
      runtime: runtime || incoming.runtime || baseline.runtime };
  }
  // A live projection is not a replacement for the historical target list.
  // Its omission of a target is never evidence of completion or released ownership.
  const replacements = new Map((incoming.targets || []).map((target) => [String(target.id), target]));
  const targets = (baseline.targets || []).map((target) => {
    const next = replacements.get(String(target.id));
    replacements.delete(String(target.id));
    return next ? { ...target, ...next } : target;
  });
  targets.push(...replacements.values());
  // Keep the last full snapshot's coverage flags. An unknown task is handled
  // separately and retains targets_partial=true until a full snapshot arrives.
  const { targets: _targets, targets_partial: _partial, targets_truncated: _truncated, ...fields } = incoming;
  return { ...baseline, ...fields, targets,
    runtime: runtime || incoming.runtime || baseline.runtime };
}

export function applyLiveStatusOverlay(snapshot: CoreWorkbenchSnapshot, live: CoreWorkbenchLiveStatus | null, minimumRevision = 0): CoreWorkbenchSnapshot {
  if (!live || !Array.isArray(live.tasks) || !live.tasks.length) return snapshot;
  const previous = asRecord(snapshot.collection_status_observation);
  const seenRevision = Math.max(snapshot.revision, firstNumber(previous, ["revision"]) ?? -1, minimumRevision);
  const seenAt = Math.max(Date.parse(snapshot.generated_at) || 0,
    Date.parse(firstText(previous, ["generated_at"], "")) || 0);
  const liveAt = Date.parse(live.generated_at);
  // Full snapshots and live replies arrive independently. Keep older replies
  // from reintroducing stopped tasks, stale counts, or former window ownership.
  if (!Number.isFinite(liveAt) || !Number.isFinite(live.revision)
    || live.revision < seenRevision
    || (live.revision === seenRevision && liveAt <= seenAt)) return snapshot;
  const baselineTasks = new Map(snapshot.tasks.map((task) => [String(task.id), task]));
  const usable = live.tasks.filter((entry) => {
    const baseline = baselineTasks.get(String(entry.task.id));
    if (!baseline) return true;
    const incomingVersion = firstNumber(asRecord(entry.task), ["version"]);
    const savedVersion = firstNumber(asRecord(baseline), ["version"]);
    if (incomingVersion !== null && savedVersion !== null && incomingVersion !== savedVersion)
      return incomingVersion > savedVersion;
    if (older(entry.task.updated_at, baseline.updated_at)) return false;
    // A single late target must not reject fresh siblings or runtime lock state.
    // Partial replies apply target timestamp checks individually below.
    if (entry.task.targets_partial === true) return true;
    const targets = new Map((baseline.targets || []).map((target) => [String(target.id), target]));
    return !(entry.task.targets || []).some((target) => {
      const saved = targets.get(String(target.id));
      return saved && older(target.updated_at, saved.updated_at);
    });
  }).map((entry) => {
    const baseline = baselineTasks.get(String(entry.task.id));
    if (!baseline || entry.task.targets_partial !== true) return entry;
    const savedTargets = new Map((baseline.targets || []).map((target) => [String(target.id), target]));
    const targets = (entry.task.targets || []).filter((target) => {
      const saved = savedTargets.get(String(target.id));
      return !saved || !older(target.updated_at, saved.updated_at);
    });
    return { ...entry, task: { ...entry.task, targets } };
  });
  live = { ...live, tasks: usable };
  const liveByTask = new Map(live.tasks.map((entry) => [String(entry.task.id), entry]));
  const mergedTasks = snapshot.tasks.map((task) => {
    const liveEntry = liveByTask.get(String(task.id));
    if (!liveEntry) return task;
    return mergeLiveTask(task, liveEntry.task, liveEntry.runtime);
  });
  const knownTaskIds = new Set(mergedTasks.map((task) => String(task.id)));
  for (const entry of live.tasks) {
    if (!knownTaskIds.has(String(entry.task.id))) mergedTasks.push({ ...entry.task, runtime: entry.runtime || entry.task.runtime });
  }

  const liveTargets = new Map<string, { task: CoreCollectionTask; target: CoreTaskTarget }>();
  for (const entry of live.tasks) {
    for (const target of Array.isArray(entry.task.targets) ? entry.task.targets : []) {
      liveTargets.set(String(target.id), { task: entry.task, target });
    }
  }
  const mergedSources = snapshot.sources.map((source) => {
    const liveTarget = liveTargets.get(String(source.id));
    if (!liveTarget) return source;
    const target = liveTarget.target;
    const modeProgress = target.mode_progress || {};
    const processed = Object.values(modeProgress).reduce<number>((sum, value) => {
      const row = asRecord(value);
      return sum + (firstNumber(row, ["processed", "discovered"]) || 0);
    }, 0);
    const totals = Object.values(modeProgress)
      .map((value) => firstNumber(asRecord(value), ["source_total"]))
      .filter((value): value is number => typeof value === "number");
    return {
      ...source,
      status: String(target.status || source.status),
      current_window_id: target.current_window_id === null ? null
        : typeof target.current_window_id === "string" ? target.current_window_id : source.current_window_id,
      processed,
      total: totals.length ? totals.reduce((sum, value) => sum + value, 0) : source.total,
      mode_progress: modeProgress,
      mode_coverage: target.mode_coverage || {},
      updated_at: typeof target.updated_at === "string" ? target.updated_at : source.updated_at,
    };
  });

  const windowOverlays = new Map<string, Partial<CoreBitBrowserWindow>>();
  for (const entry of live.tasks) {
    const runtime = asRecord(entry.runtime);
    const profiles = Array.isArray(runtime.profile_states) ? runtime.profile_states.map(asRecord) : [];
    for (const profile of profiles) {
      const profileId = firstText(profile, ["profile_id", "window_id"]);
      if (!profileId) continue;
      const state = normalizeStatus(firstText(profile, ["state"]) || "running");
      if (["stopped", "closed", "completed"].includes(state)) continue;
      const networkWaiting = ["waiting_network", "blocked_by_auth", "auth_required", "manual_intervention"].includes(state);
      windowOverlays.set(profileId, {
        opened: true,
        ready: !networkWaiting,
        opening: state === "starting" || state === "opening",
        window_state: state || "running",
        locked: true,
        lock_state: state || "running",
        lock_operation: "collection",
        lock_entity_id: String(entry.task.id),
        lock_owned: true,
      });
    }
  }
  let windowsChanged = false;
  const updatedWindows = snapshot.windows.map((window) => {
    const overlay = windowOverlays.get(String(window.id));
    if (!overlay || Object.entries(overlay).every(([key, value]) => asRecord(window)[key] === value)) return window;
    windowsChanged = true;
    return { ...window, ...overlay };
  });
  // Preserve memoized window ordering and selection work when a heartbeat only
  // changes task progress. Every changed lock field still replaces its window.
  const mergedWindows = windowsChanged ? updatedWindows : snapshot.windows;

  return { ...snapshot, tasks: mergedTasks, sources: mergedSources, windows: mergedWindows,
    collection_status_observation: { generated_at: live.generated_at, revision: live.revision } };
}
