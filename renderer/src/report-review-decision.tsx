export type ReviewDecision = "passed" | "failed";
export type DecisionCounts = { passed: number; failed: number };
export type DecisionResult = { record_id: string; decision: ReviewDecision; decision_counts: DecisionCounts };

export function ReviewDecisionSummary({counts}: {counts: DecisionCounts | null}) {
  const passed = counts?.passed ?? 0;
  const failed = counts?.failed ?? 0;
  const total = passed + failed;
  return <div className="report-review-summary" role="status">
    <strong>当日合格率：{total ? `${(passed / total * 100).toFixed(1)}%` : "—"}</strong>
    <span>已通过 {passed} · 未通过 {failed} · 已判定 {total}</span>
  </div>;
}

export function ReviewDecisionControl({decision, disabled, onSelect}: {
  decision?: ReviewDecision | null;
  disabled: boolean;
  onSelect: (value: ReviewDecision) => void;
}) {
  return <div className="report-review-actions" role="group" aria-label="人工审查结果">
    <button type="button" className={decision === "passed" ? "is-passed" : ""} aria-pressed={decision === "passed"} disabled={disabled} onClick={() => onSelect("passed")}>已通过</button>
    <button type="button" className={decision === "failed" ? "is-failed" : ""} aria-pressed={decision === "failed"} disabled={disabled} onClick={() => onSelect("failed")}>未通过</button>
  </div>;
}
