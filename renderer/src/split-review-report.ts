import type { DecisionCounts, ReviewDecision } from "./report-review-decision";
export type SplitReviewDay = {
  key: string;
  label: string;
  weekday: string;
  start: string;
  end: string;
};

export type SplitReviewEntry = {
  id: string;
  review_decision?: ReviewDecision | null;
  username: string;
  completed_at: string;
  source_target_id: string;
  source_task_id: string;
  source_window_id: string;
  split_count?: number | null;
  split_count_recorded?: number;
  split_count_complete?: boolean;
  followers?: number | null;
  following?: number | null;
  posts?: number | null;
  processed_count?: number | null;
  new_count?: number | null;
  duplicate_count?: number | null;
  mode_coverage?: Record<string, CollectionCoverage>;
  profile?: Record<string, unknown>;
};

export type CollectionCoverage = {
  source_total?: number | null;
  discovered?: number | null;
  processed?: number | null;
  pending_count?: number | null;
  remaining_count?: number | null;
  unobserved_count?: number | null;
  discovery_finished?: boolean;
  status?: string;
  end_reason?: string | null;
};

export function collectionCoverageDisplay(mode: string, value: CollectionCoverage) {
  const label = mode === "followers" ? "粉丝" : mode === "following" ? "关注" : mode;
  const metric = (number: unknown) => splitReviewMetricDisplay(number);
  const ended = value.discovery_finished === true && value.end_reason === "visible_list_end";
  const pending = knownCount(value.pending_count) && value.pending_count > 0;
  const gap = knownCount(value.remaining_count) && value.remaining_count > 0;
  const mismatch = value.status === "count_mismatch";
  const reconciled = ended && value.status === "reconciled" && value.pending_count === 0 && value.remaining_count === 0;
  const title = mismatch ? "数量口径不一致 · 需核实" : ended && pending ? "名单已到底 · 仍有待处理账号"
    : ended && gap ? `可见名单处理结束 · ${metric(value.remaining_count)} 个未核实`
    : reconciled ? "可见名单处理结束 · 数量已对齐" : ended ? "可见名单处理结束 · 总量未核实" : "名单尚未确认结束";
  const reason = mismatch ? "主页数量与本次记录不一致，不能据此确认已采全。"
    : ended && pending ? "已发现账号尚未全部处理，不能作为完整采集结果。"
    : ended && gap ? "结束原因：可见名单已确认到底。差额账号未读取到，具体账号及原因无法确认；差额不计入去重或丢弃。"
    : reconciled ? "结束原因：可见名单已确认到底，已发现账号全部处理，数量与本次读取的主页总数对齐。"
    : ended ? "结束原因：可见名单已确认到底；缺少完整计数，无法确认是否采全。"
    : "尚无可见名单结束证据，请查看任务当前状态；历史记录可能未保存结束原因。";
  return { label, title, reason, warning: !reconciled,
    counts: `主页 ${metric(value.source_total)} · 已发现 ${metric(value.discovered)} · 已处理 ${metric(value.processed)} · 待处理 ${metric(value.pending_count)} · 未核实 ${metric(value.remaining_count)}` };
}

export type SplitReviewPage = {
  items: SplitReviewEntry[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  start: string;
  end: string;
  retention_days: number;
  daily_counts?: Record<string, number>;
  decision_counts?: DecisionCounts;
};

const knownCount = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function splitReviewMetricDisplay(value: unknown): string {
  return knownCount(value) ? new Intl.NumberFormat("zh-CN").format(value) : "—";
}

export function splitReviewDayCountDisplay(counts: Record<string, number> | null | undefined, key: string): string {
  const value = counts?.[key];
  return knownCount(value) ? `${splitReviewMetricDisplay(value)} 个` : "—";
}

export function splitReviewCountDisplay(entry: Pick<SplitReviewEntry, "split_count" | "split_count_recorded" | "split_count_complete">) {
  const format = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
  if (entry.split_count_complete !== false && knownCount(entry.split_count)) {
    return { label: `${format(entry.split_count)} 次`, title: `累计成功加入分裂号 ${format(entry.split_count)} 次`, status: "exact" as const };
  }
  if (knownCount(entry.split_count_recorded) && entry.split_count_recorded > 0) {
    const recorded = format(entry.split_count_recorded);
    return { label: `至少 ${recorded} 次`, title: `旧历史可能缺少之前的记录，已确认至少 ${recorded} 次；此数量为累计下界`, status: "minimum" as const };
  }
  return { label: "—", title: "历史累计次数暂未记录", status: "unknown" as const };
}

const pad = (value: number) => String(value).padStart(2, "0");
const dayKey = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

// Keep the local UTC offset: the report service needs calendar-day boundaries,
// including days whose two midnights have different daylight-saving offsets.
export function splitReviewLocalIso(date: Date): string {
  const offset = -date.getTimezoneOffset();
  const sign = offset < 0 ? "-" : "+";
  const absolute = Math.abs(offset);
  return `${dayKey(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${String(date.getMilliseconds()).padStart(3, "0")}${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

export function recentSplitReviewDays(now = new Date()): SplitReviewDay[] {
  return Array.from({ length: 7 }, (_, index) => {
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - index);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    return {
      key: dayKey(start),
      label: index === 0 ? "今天" : index === 1 ? "昨天" : `${start.getMonth() + 1} 月 ${start.getDate()} 日`,
      weekday: ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][start.getDay()],
      start: splitReviewLocalIso(start),
      end: splitReviewLocalIso(end),
    };
  });
}

export function splitReviewQueryBounds(day: SplitReviewDay, now = new Date()) {
  // Freeze today's upper bound until refresh so new completions cannot shift
  // the offsets while the user loads subsequent pages.
  const cutoff = Math.min(new Date(day.end).getTime(), now.getTime());
  return { start: day.start, end: splitReviewLocalIso(new Date(cutoff)), utcOffsetMinutes: -now.getTimezoneOffset() };
}

export function splitReviewDailyBounds(days: SplitReviewDay[], now = new Date()) {
  return days.map(day => {
    const {start, end} = splitReviewQueryBounds(day, now);
    return {key: day.key, start, end};
  });
}

export function mergeSplitReviewEntries(previous: SplitReviewEntry[], next: SplitReviewEntry[]) {
  const seen = new Set(previous.map(item => item.id));
  return [...previous, ...next.filter(item => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  })];
}

export function splitReviewProfileUrl(username: string): string | null {
  const value = username.trim().replace(/^@+/, "");
  const reserved = new Set(["about", "accounts", "challenge", "developer", "direct", "directory", "explore", "legal", "p", "reel", "reels", "stories", "tv", "web"]);
  return /^[A-Za-z0-9._]{1,30}$/.test(value) && !reserved.has(value.toLowerCase()) ? `https://www.instagram.com/${encodeURIComponent(value)}/` : null;
}
