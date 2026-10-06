import type { DecisionCounts, ReviewDecision } from "./report-review-decision";
export type PrivateFollowReviewEntry = {
  id: string;
  review_decision?: ReviewDecision | null;
  username: string;
  completed_at: string;
  source_window_id: string | null;
  window_name?: string;
  executor_username?: string;
  followers?: number | null;
  following?: number | null;
  posts?: number | null;
  profile?: Record<string, unknown>;
  status: "confirmed";
  follow_state?: "requested" | "following" | "unknown";
  confirmation?: string;
};

export type PrivateFollowReviewPage = {
  items: PrivateFollowReviewEntry[];
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

export function privateFollowReviewStatusDisplay(value: unknown) {
  if (value === "requested") return {label: "请求已发送", title: "关注请求已成功发送；不代表对方已接受", status: "requested"};
  if (value === "following") return {label: "已关注", title: "执行时已确认显示为关注中；不代表当前实时状态", status: "following"};
  return {label: "历史状态未记录", title: "成功执行已记录，但当时的关注状态未明确记录", status: "unknown"};
}

export function mergePrivateFollowReviewEntries(previous: PrivateFollowReviewEntry[], next: PrivateFollowReviewEntry[]) {
  const seen = new Set(previous.map(item => item.id));
  return [...previous, ...next.filter(item => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  })];
}
