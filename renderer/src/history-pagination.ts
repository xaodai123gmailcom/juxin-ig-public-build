/** Bound DOM work without discarding any records from the loaded history. */
export const HISTORY_RENDER_PAGE_SIZE = 100;
export function historyPage<T>(records: readonly T[], requestedPage: number) {
  const pages = Math.max(1, Math.ceil(records.length / HISTORY_RENDER_PAGE_SIZE));
  const page = Math.min(pages - 1, Math.max(0, Math.trunc(requestedPage) || 0));
  const start = page * HISTORY_RENDER_PAGE_SIZE;
  return { items: records.slice(start, start + HISTORY_RENDER_PAGE_SIZE), page, pages, total: records.length, start };
}
