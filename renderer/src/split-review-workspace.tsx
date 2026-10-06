import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, CalendarDays, CheckCircle2, ChevronDown, Clock3, RefreshCw, ShieldCheck, UsersRound } from "lucide-react";
import { usePlatformCore, useWorkbenchPlatform } from "./workbench-platform";
import { ReviewDecisionControl, ReviewDecisionSummary, type DecisionCounts, type DecisionResult, type ReviewDecision } from "./report-review-decision";
import { collectionCoverageDisplay, mergeSplitReviewEntries, recentSplitReviewDays, splitReviewCountDisplay, splitReviewDailyBounds, splitReviewDayCountDisplay, splitReviewMetricDisplay, splitReviewProfileUrl, splitReviewQueryBounds, type SplitReviewEntry, type SplitReviewPage } from "./split-review-report";

const PAGE_SIZE = 100;
const count = (value: number) => new Intl.NumberFormat("zh-CN").format(value);
const completedTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });

export function SplitReviewWorkspace() {
  const client = usePlatformCore();
  const [days, setDays] = useState(recentSplitReviewDays);
  const [selected, setSelected] = useState(() => recentSplitReviewDays()[0].key);
  const [items, setItems] = useState<SplitReviewEntry[]>([]);
  const [page, setPage] = useState<SplitReviewPage | null>(null);
  const [dailyCounts, setDailyCounts] = useState<Record<string, number> | null>(null);
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
      bounds.current = {...splitReviewQueryBounds(day, now), dailyBounds: splitReviewDailyBounds(days, now)};
      setItems([]);
      setPage(null);
      setDecisionCounts(null);
      setDecisionError("");
      setDailyCounts(null);
    }
    const range = bounds.current;
    if (!range) return;
    try {
      const result = await client.splitReviewReport<SplitReviewPage>(range.start, range.end, offset, PAGE_SIZE, range.utcOffsetMinutes, range.dailyBounds);
      if (revision !== request.current) return;
      setItems(previous => append ? mergeSplitReviewEntries(previous, result.items) : result.items);
      setPage(result);
      setDecisionCounts(result.decision_counts || {passed: 0, failed: 0});
      setDailyCounts(result.daily_counts || null);
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
      const result = await client.reportReviewDecision<DecisionResult>("split", id, decision, range.start, range.end, range.utcOffsetMinutes);
      if (revision !== request.current) return;
      setItems(previous => previous.map(item => item.id === id ? {...item, review_decision: result.decision} : item));
      setDecisionCounts(result.decision_counts);
    } catch (reason) {
      if (revision === request.current) setDecisionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(null);
    }
  };

  return <section className="split-review-workspace" aria-label="分裂号审查">
    <header className="split-review-intro">
      <div className="split-review-heading"><span className="split-review-icon"><ShieldCheck size={23} /></span><div><h2>分裂号审查</h2></div></div>
      <span className="split-review-retention"><CalendarDays size={15} />最近 7 天 · 本地日期</span>
    </header>

    <div className="split-review-days" role="group" aria-label="选择完成日期">{days.map(value => <button key={value.key} type="button" className={day.key === value.key ? "is-selected" : ""} aria-pressed={day.key === value.key} onClick={() => setSelected(value.key)}><strong>{value.label}</strong><span>{value.key.slice(5)} · {value.weekday}</span><span className="split-review-day-count">{splitReviewDayCountDisplay(dailyCounts, value.key)}</span></button>)}</div>

    <section className="formal-panel split-review-panel" aria-busy={Boolean(loading)}>
      <header className="split-review-list-header"><div><h3><CalendarDays size={18} />{day.key} 处理记录</h3><p>{page ? <>共 <strong>{count(page.total)}</strong> 条 · 已显示 {count(items.length)} 条</> : loading ? "正在读取完整处理记录…" : "处理记录暂未读取"}</p></div><button className="formal-button" disabled={Boolean(loading)} onClick={() => void load()}><RefreshCw size={15} className={loading ? "split-review-spinning" : ""} />刷新记录</button></header>
      <ReviewDecisionSummary counts={decisionCounts} />
      {decisionError && <div className="formal-error-banner split-review-error" role="alert">{decisionError}</div>}
      {error && <div className="formal-error-banner split-review-error" role="alert"><span>分裂号记录读取失败：{error}</span><button className="formal-button compact" disabled={Boolean(loading)} onClick={() => void load(page ? page.offset + page.items.length : 0)}>重试</button></div>}
      {items.length > 0 ? <div className="split-review-table-scroll"><table className="split-review-table"><thead><tr><th scope="col">分裂号</th><th scope="col">审查结果</th><th scope="col" className="split-review-count-column">分裂次数</th><th scope="col">源账号数据</th><th scope="col">本次采集</th><th scope="col">结束日期与时间</th><th scope="col">采集来源窗口</th></tr></thead><tbody>{items.map(item => {
        const href = splitReviewProfileUrl(item.username);
        const fullName = typeof item.profile?.full_name === "string" ? item.profile.full_name : "";
        const splitCount = splitReviewCountDisplay(item);
        return <tr key={item.id}>
          <td className="split-review-source-cell"><div className="split-review-account"><span className="split-review-avatar" aria-hidden="true"><UsersRound size={19} /></span><div>{href ? <a href={href} target="_blank" rel="noreferrer" title={`审查 @${item.username}`}>@{item.username}<ArrowUpRight size={14} aria-hidden="true" /></a> : <strong>{item.username || "账号未记录"}</strong>}{fullName && <small>{fullName}</small>}</div></div></td>
          <td data-label="审查结果"><ReviewDecisionControl decision={item.review_decision} disabled={Boolean(saving) || Boolean(loading)} onSelect={value => void decide(item.id, value)} /></td>
          <td className="split-review-count-column" data-label="分裂次数"><span className={`split-review-count-badge is-${splitCount.status}`} title={splitCount.title}>{splitCount.label}</span></td>
          <td className="split-review-metrics-cell" data-label="源账号数据"><dl className="split-review-metrics split-review-source-metrics">{([["粉丝", item.followers], ["关注", item.following], ["帖子", item.posts]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{splitReviewMetricDisplay(value)}</dd></div>)}</dl></td>
          <td className="split-review-metrics-cell" data-label="本次采集"><dl className="split-review-metrics split-review-collection-metrics">{([["已处理", item.processed_count], ["新入库", item.new_count], ["重复跳过", item.duplicate_count]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{splitReviewMetricDisplay(value)}</dd></div>)}</dl><div className="split-review-coverage">{Object.entries(item.mode_coverage || {}).length ? Object.entries(item.mode_coverage || {}).map(([mode, value]) => {
            const detail = collectionCoverageDisplay(mode, value);
            return <div key={mode} className={detail.warning ? "is-warning" : "is-reconciled"}><strong>{detail.label}：{detail.title}</strong><span>{detail.counts}</span><small>{detail.reason}</small></div>;
          }) : <small>历史未记录分模式计数与结束原因，无法确认是否采全。</small>}</div></td>
          <td className="split-review-completed-cell" data-label="结束日期与时间"><span className="split-review-completed"><CheckCircle2 size={14} /><time dateTime={item.completed_at}>{completedTime(item.completed_at)}</time></span></td>
          <td className="split-review-window-cell" data-label="采集来源窗口"><span className="split-review-window" title={item.source_window_id}>{item.source_window_id || "历史未记录"}</span></td>
        </tr>;
      })}</tbody></table></div> : <div className="split-review-empty" role="status"><span>{loading ? <Clock3 size={28} /> : <CalendarDays size={28} />}</span><strong>{loading ? "正在读取分裂号记录" : error ? "暂时无法显示记录" : "这一天暂无已结束的分裂号"}</strong></div>}
      {page && items.length > 0 && <footer className="split-review-list-footer"><span>{page.has_more ? `已显示 ${count(items.length)} / ${count(page.total)} 条` : `已显示当天全部 ${count(page.total)} 条记录`}</span>{page.has_more && <button className="formal-button" disabled={Boolean(loading)} onClick={() => void load(page.offset + page.items.length)}><ChevronDown size={15} />{loading === "more" ? "正在读取…" : "加载更多"}</button>}</footer>}
    </section>

  </section>;
}
