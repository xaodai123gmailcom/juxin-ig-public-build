import type { CoreReviewQueuePage, CoreReviewStage } from "./core-client";

/** Shared by requests, page navigation and page-bound selection. */
export const REVIEW_PAGE_SIZE = 500;

export type ReviewQueueQuery = {
  platform?: "instagram";
  visibility: "public" | "private";
  review_stage: CoreReviewStage;
  offset: number;
  limit: number;
};

export function reviewQueueKey(query: ReviewQueueQuery): string {
  return `${query.platform || "instagram"}:${query.visibility}:${query.review_stage}:${query.offset}:${query.limit}`;
}

/** Explicit IDs keep incoming collection results out of an operator's selection. */
export function selectedReviewIds(items: ReadonlyArray<{ id: string }>, selected: ReadonlySet<string>): string[] {
  return items.filter((item) => selected.has(item.id)).map((item) => item.id);
}

/** Serialize reads and fence responses when the operator changes pages or mutates a queue. */
export function createReviewQueueReader(options: {
  query: () => ReviewQueueQuery;
  read: (query: ReviewQueueQuery) => Promise<CoreReviewQueuePage>;
  onValue: (page: CoreReviewQueuePage, query: ReviewQueueQuery) => void;
  onError: (reason: unknown, query: ReviewQueueQuery) => void;
}) {
  let active = true, epoch = 0;
  let pending: Promise<void> | null = null;
  return {
    invalidate() { epoch++; },
    refresh(fresh = false): Promise<void> {
      if (!active) return Promise.resolve();
      if (!fresh && pending) return pending;
      if (fresh) epoch++;
      const generation = epoch;
      const request = (pending || Promise.resolve()).then(async () => {
        if (!active || generation !== epoch) return;
        const query = { ...options.query() };
        try {
          const page = await options.read(query);
          if (active && generation === epoch) options.onValue(page, query);
        } catch (reason) {
          if (active && generation === epoch) options.onError(reason, query);
        }
      }).finally(() => { if (pending === request) pending = null; });
      pending = request;
      return request;
    },
    dispose() { active = false; epoch++; },
  };
}
