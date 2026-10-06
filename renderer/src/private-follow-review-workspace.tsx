import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, CalendarDays, CheckCircle2, ChevronDown, Clock3, RefreshCw, ShieldCheck, UsersRound } from "lucide-react";
import { usePlatformCore, useWorkbenchPlatform } from "./workbench-platform";
import { ReviewDecisionControl, ReviewDecisionSummary, type DecisionCounts, type DecisionResult, type ReviewDecision } from "./report-review-decision";
import { recentSplitReviewDays, splitReviewDailyBounds, splitReviewDayCountDisplay, splitReviewMetricDisplay, splitReviewProfileUrl, splitReviewQueryBounds } from "./split-review-report";
import { mergePrivateFollowReviewEntries, privateFollowReviewStatusDisplay, type PrivateFollowReviewEntry, type PrivateFollowReviewPage } from "./private-follow-review-report";

const PAGE_SIZE = 100;
const count = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
const completedTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });

export function PrivateFollowReviewWorkspace() {
  const client = usePlatformCore();
  const [days, setDays] = useState(recentSplitReviewDays);
  const [selected, setSelected] = useState(() => recentSplitReviewDays()[0].key);
  const [items, setItems] = useState<PrivateFollowReviewEntry[]>([]);
  const [page, setPage] = useState<PrivateFollowReviewPage | null>(null);
  const [dailyCounts, setDailyCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState<"initial" | "more" | null>(null);
  const [error, setError] = useState("");
  const [decisionError, setDecisionError] = useState("");
  const [decisionCounts, setDecisionCounts] = useState<DecisionCounts | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const request = useRef(0), active = useRef(false);
  const bounds = useRef<{ start: string; end: string; utcOffsetMinutes: number; dailyBounds: ReturnType<typeof splitReviewDailyBounds> } | null>(null);
  const day = days.find(value => value.key === selected) || days[0];

  useEffect(() => {
    const updateDays = () => {
      const next = recentSplitReviewDays();
      setDays(previous => previous[0].key === next[0].key ? previous : next);
      setSelected(previous => next.some(value => value.key === previous) ? previous : next[0].key);
    };
    const timer = window.setInterval(updateDays, 60_000);
    window.addEventListener("focus", updateDays);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", updateDays); };
  }, []);

  const load = useCallback(async (offset = 0) => {
    const append = offset > 0;
    if (append && active.current) return;
    const revision = ++request.current;
    active.current = true;
    setLoading(append ? "more" : "initial");
    setError("");
    if (!append) {
      const now = new Date();
      bounds.current = { ...splitReviewQueryBounds(day, now), dailyBounds: splitReviewDailyBounds(days, now) };
      setItems([]);
      setPage(null);
      setDecisionCounts(null);
      setDecisionError("");
      setDailyCounts({});
    }
    const range = bounds.current;
    if (!range) return;
    try {
      const result = await client.privateFollowReviewReport<PrivateFollowReviewPage>(range.start, range.end, offset, PAGE_SIZE, range.utcOffsetMinutes, range.dailyBounds);
      if (revision !== request.current) return;
      setItems(previous => append ? mergePrivateFollowReviewEntries(previous, result.items) : result.items);
      setPage(result);
      setDecisionCounts(result.decision_counts || {passed: 0, failed: 0});
      setDailyCounts(result.daily_counts || {});
    } catch (reason) {
      if (revision === request.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (revision === request.current) { active.current = false; setLoading(null); }
    }
  }, [day, days]);

  useEffect(() => {
    void load();
    return () => { request.current++; active.current = false; };
  }, [load]);

  const decide = async (id: string, decision: ReviewDecision) => {
    const range = bounds.current;
    if (!range || saving || loading) return;
    const revision = request.current;
    setSaving(id);
    setDecisionError("");
    try {
      const result = await client.reportReviewDecision<DecisionResult>("private_follow", id, decision, range.start, range.end, range.utcOffsetMinutes);
      if (revision !== request.current) return;
      setItems(previous => previous.map(item => item.id === id ? {...item, review_decision: result.decision} : item));
      setDecisionCounts(result.decision_counts);
    } catch (reason) {
      if (revision === request.current) setDecisionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(null);
    }
  };

  return <section className="split-review-workspace private-follow-review-workspace" aria-label="私密关注检查">
    <header className="split-review-intro">
      <div className="split-review-heading"><span className="split-review-icon"><ShieldCheck size={23} /></span><div><h2>私密关注检查</h2></div></div>
      <span className="split-review-retention"><CalendarDays size={15} />最近 7 天 · 本地日期</span>
    </header>

    <div className="split-review-days" role="group" aria-label="选择关注日期">{days.map(value => {
      return <button key={value.key} type="button" className={day.key === value.key ? "is-selected" : ""} aria-pressed={day.key === value.key} onClick={() => setSelected(value.key)}><strong>{value.label}</strong><span>{value.key.slice(5)} · {value.weekday}</span><span className="split-review-day-count">{splitReviewDayCountDisplay(dailyCounts, value.key)}</span></button>;
    })}</div>

    <section className="formal-panel split-review-panel" aria-busy={Boolean(loading)}>
      <header className="split-review-list-header"><div><h3><CalendarDays size={18} />{day.key} 关注记录</h3><p>{page ? <>共 <strong>{count(page.total)}</strong> 条 · 已显示 {count(items.length)} 条</> : loading ? "正在读取完整关注记录…" : "关注记录暂未读取"}</p></div><button className="formal-button" disabled={Boolean(loading)} onClick={() => void load()}><RefreshCw size={15} className={loading ? "split-review-spinning" : ""} />刷新记录</button></header>
      <ReviewDecisionSummary counts={decisionCounts} />
      {decisionError && <div className="formal-error-banner split-review-error" role="alert">{decisionError}</div>}
      {error && <div className="formal-error-banner split-review-error" role="alert"><span>私密关注记录读取失败：{error}</span><button className="formal-button compact" disabled={Boolean(loading)} onClick={() => void load(page ? page.offset + page.items.length : 0)}>重试</button></div>}
      {items.length > 0 ? <div className="split-review-table-scroll"><table className="split-review-table private-follow-review-table"><thead><tr><th scope="col">私密账号</th><th scope="col">审查结果</th><th scope="col">源账号数据</th><th scope="col">关注状态</th><th scope="col">成功确认时间</th><th scope="col">执行窗口</th></tr></thead><tbody>{items.map(item => {
        const href = splitReviewProfileUrl(item.username);
        const fullName = typeof item.profile?.full_name === "string" ? item.profile.full_name : "";
        const status = privateFollowReviewStatusDisplay(item.follow_state);
        return <tr key={item.id}>
          <td className="split-review-source-cell"><div className="split-review-account"><span className="split-review-avatar" aria-hidden="true"><UsersRound size={19} /></span><div>{href ? <a href={href} target="_blank" rel="noreferrer" title={`审查 @${item.username}`}>@{item.username}<ArrowUpRight size={14} aria-hidden="true" /></a> : <strong>{item.username || "账号未记录"}</strong>}{fullName && <small>{fullName}</small>}</div></div></td>
          <td data-label="审查结果"><ReviewDecisionControl decision={item.review_decision} disabled={Boolean(saving) || Boolean(loading)} onSelect={value => void decide(item.id, value)} /></td>
          <td className="split-review-metrics-cell" data-label="源账号数据"><dl className="split-review-metrics split-review-source-metrics">{([["粉丝", item.followers], ["关注", item.following], ["帖子", item.posts]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{splitReviewMetricDisplay(value)}</dd></div>)}</dl></td>
          <td className="private-follow-status-cell" data-label="关注状态"><span className={`private-follow-status is-${status.status}`} title={status.title}>{status.label}</span></td>
          <td className="split-review-completed-cell" data-label="成功确认时间"><span className="split-review-completed"><CheckCircle2 size={14} /><time dateTime={item.completed_at}>{completedTime(item.completed_at)}</time></span></td>
          <td className="split-review-window-cell" data-label="执行窗口"><span className="split-review-window" title={item.source_window_id || undefined}>{item.window_name || item.source_window_id || "历史未记录"}</span>{item.executor_username && <small className="private-follow-executor">@{item.executor_username.replace(/^@+/, "")}</small>}</td>
        </tr>;
      })}</tbody></table></div> : <div className="split-review-empty" role="status"><span>{loading ? <Clock3 size={28} /> : <CalendarDays size={28} />}</span><strong>{loading ? "正在读取私密关注记录" : error ? "暂时无法显示记录" : "这一天暂无成功的私密关注记录"}</strong></div>}
      {page && items.length > 0 && <footer className="split-review-list-footer"><span>{page.has_more ? `已显示 ${count(items.length)} / ${count(page.total)} 条` : `已显示当天全部 ${count(page.total)} 条记录`}</span>{page.has_more && <button className="formal-button" disabled={Boolean(loading)} onClick={() => void load(page.offset + page.items.length)}><ChevronDown size={15} />{loading === "more" ? "正在读取…" : "加载更多"}</button>}</footer>}
    </section>

  </section>;
}
