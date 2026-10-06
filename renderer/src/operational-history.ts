import type { CoreHistoryRecord } from "./core-client";

const HISTORY_TYPES = [
  { id: "collection", label: "采集任务" },
  { id: "follow", label: "自动点关注" },
  { id: "greet", label: "自动打招呼" },
  { id: "other", label: "其他任务" },
] as const;

export function operationalHistoryTypeLabel(type: string): string {
  return HISTORY_TYPES.find(group => group.id === type)?.label || "其他任务";
}

/** Group the already-authorized, already-status-filtered history in its existing
 * order. Unknown types stay visible instead of becoming follow successes. */
export function operationalHistoryGroups<T extends { type: string }>(rows: readonly T[]) {
  const groups = HISTORY_TYPES.map(group => ({ ...group, rows: [] as T[] }));
  const byType = new Map<string, typeof groups[number]>(groups.map(group => [group.id, group]));
  for (const row of rows) (byType.get(row.type) || byType.get("other")!).rows.push(row);
  return groups.filter(group => group.id !== "other" || group.rows.length > 0);
}

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const count = (value: unknown): number | null =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;

export type OperationalHistoryRow = {
  id: string;
  type: string;
  time: unknown;
  target: string;
  window: string;
  mode?: string;
  processed?: number | null;
  qualified?: number | null;
  message?: string | null;
  status: unknown;
};

/** This feed is collection-only. Legacy tasks need no new type discriminator.
 * Project existing source/mode evidence without task-wide totals, preferred
 * window assignments, invented zeros, or any backend/history mutation. */
export function collectionOperationalHistoryRows(
  tasks: readonly CoreHistoryRecord[], windowNames: ReadonlyMap<string, string>,
): OperationalHistoryRow[] {
  return tasks.flatMap(task => {
    const targets = Array.isArray(task.targets) ? task.targets.filter(target => target && typeof target === "object" && !Array.isArray(target)).map(record) : [];
    return (targets.length ? targets : [task]).flatMap((target, targetIndex) => {
      const progress = record(target.mode_progress);
      const modes = [...new Set([
        ...(Array.isArray(task.modes) ? task.modes.map(text).filter(Boolean) : []),
        ...Object.keys(progress),
      ])];
      const profileId = text(target.current_window_id) || text(target.window_id);
      const username = text(target.username) || text(target.username_display)
        || text(target.target_account) || text(target.source_target);
      return (modes.length ? modes : [""]).map(mode => {
        const values = record(progress[mode]);
        return {
          id: `task-${task.id}-${text(target.id) || targetIndex}-${mode || "legacy"}`,
          type: "collection",
          time: target.updated_at || task.finished_at || task.updated_at || task.created_at,
          target: username ? `@${username.replace(/^@/, "")}` : "—",
          window: profileId ? windowNames.get(profileId) || profileId : "—",
          mode: mode === "followers" ? "粉丝" : mode === "following" ? "关注" : mode || "—",
          processed: count(values.processed),
          qualified: count(values.qualified_for_review),
          status: target.task_status || target.status || task.task_status || task.status,
        };
      });
    });
  });
}
