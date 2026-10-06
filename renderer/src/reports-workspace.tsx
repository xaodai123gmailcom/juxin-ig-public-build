import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { BarChart3, Download, RefreshCw, ShieldCheck, UserRoundCheck } from "lucide-react";
import { usePlatformCore } from "./workbench-platform";
import { type CoreWorkbenchSnapshot } from "./core-client";
import { ReportDataOverview } from "./report-data-overview";
import { SplitReviewWorkspace } from "./split-review-workspace";
import { PrivateFollowReviewWorkspace } from "./private-follow-review-workspace";
import "./reports-workspace.css";

type Period = "day" | "week" | "month";
const metrics = ["follow", "greet", "split", "added", "posting", "collection", "check", "nurture", "approved", "confirmed_posting"] as const;
type Metric = typeof metrics[number];
const labels: Record<Metric, string> = { follow:"私密点关注", greet:"公开打招呼", split:"分裂数量", posting:"历史发帖（原功能）", confirmed_posting:"新队列确认发帖", added:"新增数量", collection:"采集账号", check:"关注检查", nurture:"养号完成", approved:"审核合格" };
type Row = {profile_id:string;window_name:string;username:string;instagram_user_id:string} & Record<Metric,number>;
const summaryMetrics = ["collection", "follow", "split", "added"] as const;
const summaryCards = [...summaryMetrics, "confirmed_posting"] as const;
const postingSummaryHint = "仅统计当前发帖队列已确认的发布成功回执；结果待核验和历史人工确认不计入";
type Summary = {start:string;end:string;totals:Record<typeof summaryMetrics[number],number> & {confirmed_posting?:number}};
type Report = Summary & {totals:Record<Metric,number>;rows:Row[];unattributed:number};
type Totals = {total_collected:number;today_collected:number;global_dedupe:number;approved:number};
const n = (value:number) => new Intl.NumberFormat("zh-CN").format(value);
function localDate() { const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; }
export function reportBounds(period:Period, day:string) {
  const [year,month,date]=day.split("-").map(Number);
  const start=new Date(year,month-1,date);
  if(period==="week")start.setDate(start.getDate()-((start.getDay()+6)%7));
  if(period==="month")start.setDate(1);
  const end=new Date(start);
  if(period==="month")end.setMonth(end.getMonth()+1); else end.setDate(end.getDate()+(period==="week"?7:1));
  return {start:start.toISOString(),end:end.toISOString()};
}

export function HistoryTotals() {
  const client = usePlatformCore();
  const [value,setValue]=useState<Totals|null>(null),[error,setError]=useState("");
  const request=useRef(0);
  const refresh=useCallback(async()=>{const version=++request.current;const b=reportBounds("day",localDate());try{const r=await client.workReport<Totals>("history",b.start,b.end);if(version===request.current){setValue(r);setError("")}}catch(e){if(version===request.current)setError(String(e))}},[client]);
  useEffect(()=>{void refresh();const id=setInterval(()=>{if(!document.hidden)void refresh()},30000);return()=>{request.current++;clearInterval(id)}},[refresh]);
  return <div><div className="report-history-totals">{[["总采集数",value?.total_collected],["总去重数",value?.global_dedupe],["今日采集数",value?.today_collected],["合格数",value?.approved]].map(([label,count])=><div className="report-history-total" key={String(label)}><span>{label}</span><strong>{typeof count==="number"?n(count):"—"}</strong></div>)}</div>{error&&<p className="formal-error-banner" role="alert">统计暂未更新 <button className="formal-button compact" onClick={()=>void refresh()}>重试</button></p>}</div>;
}

type ReportsWorkspaceProps = {
  snapshot?: CoreWorkbenchSnapshot | null;
  snapshotError?: Error | null;
  snapshotLoading?: boolean;
  refreshSnapshot?: () => Promise<unknown>;
};
const noSnapshotRefresh = async () => undefined;

export function ReportsWorkspace({snapshot = null, snapshotError = null, snapshotLoading = true, refreshSnapshot = noSnapshotRefresh}: ReportsWorkspaceProps = {}) {
  const [view, setView] = useState<"activity" | "split-review" | "private-follow-review">("activity");
  const [dataExpanded, setDataExpanded] = useState(false);
  // Invalidate immediately on navigation, before the activity effect is cleaned up.
  const navigationRevision = useRef(0);
  function selectView(next: typeof view) {
    if (next !== view) navigationRevision.current++;
    setView(next);
  }
  return <>
    <nav className="report-workspace-tabs" aria-label="报表类型">
      <button className={view === "activity" ? "is-selected" : ""} aria-pressed={view === "activity"} onClick={() => selectView("activity")}><BarChart3 size={18} /><span>工作统计</span></button>
      {(<><button className={view === "split-review" ? "is-selected" : ""} aria-pressed={view === "split-review"} onClick={() => selectView("split-review")}><ShieldCheck size={18} /><span>分裂号审查</span></button>
      <button className={view === "private-follow-review" ? "is-selected" : ""} aria-pressed={view === "private-follow-review"} onClick={() => selectView("private-follow-review")}><UserRoundCheck size={18} /><span>私密关注检查</span></button></>)}
    </nav>
    {view === "activity" ? <ActivityReports navigationRevision={navigationRevision} /> : view === "split-review" ? <SplitReviewWorkspace /> : <PrivateFollowReviewWorkspace />}
    <section className="report-data-section" aria-labelledby="report-data-heading">
      <header className="report-data-section-header"><div><h2 id="report-data-heading">数据概览</h2><p>累计账号、任务状态与每日完成记录（UTC），不随上方统计日期变化</p></div><button className="formal-button" aria-expanded={dataExpanded} aria-controls="report-data-overview" onClick={() => setDataExpanded(value => !value)}>{dataExpanded ? "收起数据概览" : "展开数据概览"}</button></header>
      {dataExpanded && <div id="report-data-overview"><ReportDataOverview snapshot={snapshot} snapshotError={snapshotError} snapshotLoading={snapshotLoading} refreshSnapshot={refreshSnapshot} /></div>}
    </section>
  </>;
}

function ActivityReports({navigationRevision}: {navigationRevision: {current: number}}) {
  const client = usePlatformCore();
  const [period, setPeriod] = useState<Period>("day"), [day, setDay] = useState(localDate);
  const [report, setReport] = useState<{data: Summary; revision: number; navigation: number} | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false), [exportError, setExportError] = useState("");
  const revision = useRef(0), exportRequest = useRef<object | null>(null);
  const reset = useCallback(() => {
    revision.current++;
    exportRequest.current = null;
    setReport(null);
    setError("");
    setLoading(true);
    setExporting(false);
    setExportError("");
    return revision.current;
  }, []);
  const refresh = useCallback(async () => {
    const version = reset(), navigation = navigationRevision.current;
    const isCurrent = () => version === revision.current && navigation === navigationRevision.current;
    try {
      const bounds = reportBounds(period, day);
      const data = await client.workReport<Summary>("activity", bounds.start, bounds.end, {summaryOnly: true});
      if (!isCurrent()) return;
      if (!summaryMetrics.every(key => typeof data?.totals?.[key] === "number" && Number.isFinite(data.totals[key]))) {
        throw new Error("报表汇总不完整，请刷新重试");
      }
      setReport({data, revision: version, navigation});
    } catch (e) {
      if (isCurrent()) setError(String(e));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [period, day, client, reset, navigationRevision]);
  useEffect(() => {
    void refresh();
    return () => {revision.current++; exportRequest.current = null};
  }, [refresh]);
  const currentReport = report?.revision === revision.current && report.navigation === navigationRevision.current ? report.data : null;
  function choosePeriod(next: Period) {
    if (next === period) return;
    reset();
    setPeriod(next);
  }
  function chooseDay(next: string) {
    if (!next || next === day) return;
    reset();
    setDay(next);
  }
  function today() {
    const next = localDate();
    if (next === day && period === "day") return;
    reset();
    setDay(next);
    setPeriod("day");
  }
  async function exportCsv() {
    // The ref also blocks double clicks before React has rendered the disabled button.
    if (!report || report.revision !== revision.current || report.navigation !== navigationRevision.current || loading || exportRequest.current) return;
    const token = {}, version = revision.current, navigation = navigationRevision.current;
    const isCurrent = () => exportRequest.current === token && version === revision.current && navigation === navigationRevision.current;
    exportRequest.current = token;
    setExporting(true);
    setExportError("");
    try {
      const bounds = reportBounds(period, day);
      // Fetch the complete period only on demand. No summary option, row cap or UI filter.
      const full = await client.workReport<Report>("activity", bounds.start, bounds.end);
      if (!isCurrent()) return;
      if (!Array.isArray(full?.rows) || !metrics.every(key => Number.isFinite(full?.totals?.[key])) ||
          !full.rows.every(row => metrics.every(key => Number.isFinite(row?.[key])))) {
        throw new Error("完整报表不可用，请重试导出");
      }
      const cell = (value: unknown) => {
        let text = String(value ?? "");
        if (/^\s*[=+@\-]|^[\t\r\n]/.test(text)) text = "'" + text;
        return '"' + text.replaceAll('"', '""') + '"';
      };
      const data = [
        ["统计开始", full.start, "统计截止（不含）", full.end],
        ["窗口名称", "窗口 ID", "执行 IG 账号", "IG 账号 ID", ...metrics.map(key => labels[key])],
        ...full.rows.map(row => [row.window_name || "历史未记录", row.profile_id, row.username || "历史未记录", row.instagram_user_id, ...metrics.map(key => row[key])]),
        ["合计", "", "", "", ...metrics.map(key => full.totals[key])],
      ];
      const url = URL.createObjectURL(new Blob(["\uFEFF" + data.map(row => row.map(cell).join(",")).join("\r\n")], {type: "text/csv;charset=utf-8"}));
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = `聚鑫国际-${day}-${period}-工作报表.csv`;
        link.click();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (e) {
      if (isCurrent()) setExportError(String(e));
    } finally {
      if (isCurrent()) {exportRequest.current = null; setExporting(false)}
    }
  }
  return <>
    <section className="report-toolbar">
      <div className="report-periods" aria-label="统计周期">{([["day", "当日"], ["week", "当周"], ["month", "当月"]] as const).map(([key, label]) => <button className={`formal-button ${period === key ? "primary" : ""}`} aria-pressed={period === key} key={key} onClick={() => choosePeriod(key)}>{label}</button>)}</div>
      <label className="formal-field"><span>选择日期</span><input type="date" className="formal-input" value={day} onChange={e => chooseDay(e.target.value)} /></label>
      <button className="formal-button" onClick={today}>回到今天</button><span className="formal-grow" />
      <button className="formal-button" disabled={loading} onClick={() => void refresh()}><RefreshCw size={16} />刷新报表</button>
      <button className="formal-button" disabled={!currentReport || loading || exporting} aria-busy={exporting} onClick={() => void exportCsv()}><Download size={16} />{exporting ? "正在导出 CSV…" : "导出 CSV"}</button>
    </section>
    {error && <div className="formal-error-banner" role="alert">{error}</div>}
    {exportError && <div className="formal-error-banner" role="alert">CSV 导出失败：{exportError}</div>}
    <div className="report-summary" aria-busy={loading}>{summaryCards.map(key => {
      const posting = key === "confirmed_posting";
      const measured = currentReport?.totals[key];
      const known = typeof measured === "number" && Number.isSafeInteger(measured) && measured >= 0;
      const value = known ? n(measured) : loading ? "读取中" : "—";
      const label = posting ? "发帖数量" : key === "collection" ? "采集总数" : key === "added" || key === "split" ? labels[key] : `${labels[key]}成功数`;
      return <div className={`report-card report-${posting ? "posting" : key}`} key={key} role={posting ? "group" : undefined} aria-label={posting ? `发帖数量：${postingSummaryHint}` : undefined} title={posting ? postingSummaryHint : undefined}>
        <span>{label}</span>
        <strong title={posting ? postingSummaryHint : value} style={{"--report-value-length": Math.max(value.length, 6)} as CSSProperties}>{value}</strong>
        <small>{posting ? known ? "次确认发布" : "记录不可用" : key === "added" ? "次新增" : key === "split" ? "次完成" : "个账号"}</small>
      </div>;
    })}</div>
  </>;
}
