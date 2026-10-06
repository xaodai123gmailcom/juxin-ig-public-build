import type { CoreWorkbenchSnapshot } from "./core-client";

type ReviewInputs = { snapshot: CoreWorkbenchSnapshot | null; run: unknown; disabled: unknown };
/** The window workspace consumes only this inventory from the global snapshot. */
export function sameAccountWorkspaceInputs(
  previous: { snapshot: CoreWorkbenchSnapshot | null; onChanged?: unknown },
  next: { snapshot: CoreWorkbenchSnapshot | null; onChanged?: unknown },
): boolean {
  return previous.snapshot?.windows === next.snapshot?.windows && previous.onChanged === next.onChanged;
}
const reviewCountKeys = [
  "pending_public", "pending_private_primary", "pending_private_secondary",
  "pending_private", "pending_review",
] as const;

/** Task-only heartbeats do not change any field rendered by the review page. */
export function sameReviewWorkspaceInputs(previous: ReviewInputs, next: ReviewInputs): boolean {
  if (!previous.snapshot || !next.snapshot) return previous.snapshot === next.snapshot
    && previous.run === next.run && previous.disabled === next.disabled;
  const beforeCounts = previous.snapshot.counts, afterCounts = next.snapshot.counts;
  return previous.run === next.run && previous.disabled === next.disabled
    && previous.snapshot.pending.public === next.snapshot.pending.public
    && previous.snapshot.pending.private === next.snapshot.pending.private
    && previous.snapshot.split_candidates === next.snapshot.split_candidates
    && previous.snapshot.has_more === next.snapshot.has_more
    && previous.snapshot.truncated === next.snapshot.truncated
    && previous.snapshot.dedupe.total === next.snapshot.dedupe.total
    && reviewCountKeys.every((key) => beforeCounts[key] === afterCounts[key]);
}

/** Compare every prop, including callbacks and permission/busy inputs. */
export function sameComponentProps(previous: object, next: object): boolean {
  const before = previous as Record<string, unknown>, after = next as Record<string, unknown>;
  const keys = Object.keys(before);
  return keys.length === Object.keys(after).length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(after, key) && Object.is(before[key], after[key]));
}

export function retainEqualOrder(previous: string[], next: string[]): string[] {
  return previous.length === next.length && previous.every((id, index) => id === next[index]) ? previous : next;
}

/** Preserve an operator's order, discard removed windows, append new windows. */
export function reconcileWindowOrder(previous: string[], liveIds: string[]): string[] {
  const live = new Set(liveIds);
  const retained = previous.filter((id) => live.has(id));
  const known = new Set(retained);
  return retainEqualOrder(previous, [...retained, ...liveIds.filter((id) => !known.has(id))]);
}

export function retainLiveSelection(previous: Set<string>, liveIds: Set<string>): Set<string> {
  const retained = [...previous].filter((id) => liveIds.has(id));
  return retained.length === previous.size ? previous : new Set(retained);
}

/** Build group labels/counts in one pass instead of scanning all windows per option. */
export function countWindowGroups(windows: ReadonlyArray<{ group?: unknown }>): Map<string, number> {
  const counts = new Map<string, number>();
  for (const window of windows) {
    const group = String(window.group || "").trim();
    if (group) counts.set(group, (counts.get(group) || 0) + 1);
  }
  return counts;
}

/** One read at a time, with fresh reads fenced after a completed command. */
export function createViewSnapshotReader<T>(options: {
  read: () => Promise<T>;
  onValue: (value: T) => void;
  onError: (reason: unknown) => void;
}) {
  let active = true, epoch = 0;
  let pending: Promise<void> | null = null;
  return {
    refresh(fresh = false): Promise<void> {
      if (!active) return Promise.resolve();
      if (!fresh && pending) return pending;
      if (fresh) epoch++;
      const generation = epoch;
      // A fresh read waits for the old transport to settle instead of adding
      // another concurrent request. Superseded queued reads never start.
      const request = (pending || Promise.resolve()).then(async () => {
        if (!active || generation !== epoch) return;
        try {
          const value = await options.read();
          if (active && generation === epoch) options.onValue(value);
        } catch (reason) {
          if (active && generation === epoch) options.onError(reason);
        }
      }).finally(() => { if (pending === request) pending = null; });
      pending = request;
      return request;
    },
    dispose() { active = false; epoch++; },
  };
}
