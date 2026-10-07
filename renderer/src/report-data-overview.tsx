import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { CoreWorkbenchSnapshot } from "./core-client";
import { usePlatformCore } from "./workbench-platform";
import "./report-data-overview.css";

export type ReportDataOverviewProps = {
  snapshot: CoreWorkbenchSnapshot | null;
  snapshotError: Error | null;
  snapshotLoading: boolean;
  refreshSnapshot: () => Promise<unknown>;
};

type StudioOverview = {
  totals: Array<{kind: string; status: string; count: number}>;
  daily: Array<{day: string; kind: string; count: number}>;
  monitor_totals: {added: number; repeated: number};
  scheduler_error?: string;
};
type StudioOverviewState = {data: StudioOverview | null; error: string; loading: boolean};
const emptyStudioState: StudioOverviewState = {data: null, error: "", loading: true};
const kindText: Record<string, string> = {nurture: "养号"};
const statusText: Record<string, string> = {
  queued: "等待执行", waiting_window: "等待窗口", running: "执行中", paused: "已暂停",
  cancelled: "已取消", completed: "已完成", failed: "失败", needs_review: "结果待确认",
};

function actualCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/** An incomplete response is unavailable, rather than evidence for zero activity. */
export function validateStudioOverview(value: StudioOverview): StudioOverview {
  if (!value || !Array.isArray(value.totals) || !Array.isArray(value.daily) ||
      !actualCount(value.monitor_totals?.added) || !actualCount(value.monitor_totals?.repeated) ||
      !value.totals.every(row => row && typeof row.kind === "string" && typeof row.status === "string" && actualCount(row.count)) ||
      !value.daily.every(row => row && typeof row.day === "string" && typeof row.kind === "string" && actualCount(row.count))) {
    throw new Error("任务与检查统计不完整，请刷新数据概览重试");
  }
  return {...value, totals: value.totals.filter(row => row.kind === "nurture"), daily: value.daily.filter(row => row.kind === "nurture")};
}

/** One reader belongs to one mounted overview. Disposal fences both success and failure. */
export function createReportDataReader({read, onChange}: {
  read: () => Promise<StudioOverview>;
  onChange: (state: StudioOverviewState) => void;
}) {
  let state = emptyStudioState;
  let disposed = false;
  let pending: Promise<void> | null = null;
  const publish = (next: StudioOverviewState) => {
    if (disposed) return;
    state = next;
    onChange(next);
  };
  return {
    refresh(): Promise<void> {
      if (disposed) return Promise.resolve();
      if (pending) return pending;
      // Keep the previous error until this source actually recovers.
      publish({...state, loading: true});
      pending = Promise.resolve().then(async () => {
        if (disposed) return;
        const data = validateStudioOverview(await read());
        publish({data, error: "", loading: false});
      }).catch(reason => {
        publish({...state, error: String(reason), loading: false});
      }).finally(() => {pending = null});
      return pending;
    },
    dispose() {disposed = true},
  };
}

export function ReportDataOverview({snapshot, snapshotError, snapshotLoading, refreshSnapshot}: ReportDataOverviewProps) {
  const client = usePlatformCore();
  const [studioState, setStudioState] = useState<{client: typeof client; value: StudioOverviewState} | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const readerRef = useRef<ReturnType<typeof createReportDataReader> | null>(null);
  const refreshRequest = useRef<object | null>(null);

  useEffect(() => {
    const reader = createReportDataReader({
      read: () => client.studioSnapshot<StudioOverview>(),
      onChange: value => setStudioState({client, value}),
    });
    readerRef.current = reader;
    setRefreshing(false);
    setRefreshError("");
    // The parent mounts this only after expansion. Reuse its Core snapshot;
    // neither a Core refresh nor another snapshot subscription starts here.
    void reader.refresh();
    return () => {
      reader.dispose();
      if (readerRef.current === reader) readerRef.current = null;
      refreshRequest.current = null;
    };
  }, [client]);

  const studio = studioState?.client === client ? studioState.value : emptyStudioState;
  async function refreshOverview() {
    const reader = readerRef.current;
    // Guard the event itself as well as disabling its button, including double clicks
    // in the same render and completions after collapse/navigation.
    if (!reader || refreshRequest.current) return;
    const request = {};
    refreshRequest.current = request;
    const current = () => readerRef.current === reader && refreshRequest.current === request;
    setRefreshing(true);
    const coreRefresh = Promise.resolve().then(refreshSnapshot).then(() => {
      if (current()) setRefreshError("");
    }).catch(reason => {
      if (current()) setRefreshError(String(reason));
    });
    try {
      // Independent sources: either side may succeed while the other is unavailable.
      await Promise.all([reader.refresh(), coreRefresh]);
    } finally {
      if (current()) {refreshRequest.current = null; setRefreshing(false)}
    }
  }

  const completed = (kind: string) => studio.data
    ? studio.data.totals.filter(row => row.kind === kind && row.status === "completed").reduce((sum, row) => sum + row.count, 0)
    : undefined;
  const counters: Array<{label: string; value: unknown; loading: boolean}> = [
    {label: "总采集", value: snapshot?.counts?.total_collected, loading: snapshotLoading},
    {label: "公开账号", value: snapshot?.counts?.total_public, loading: snapshotLoading},
    {label: "私密账号", value: snapshot?.counts?.total_private, loading: snapshotLoading},
    {label: "全局去重", value: snapshot?.dedupe?.total, loading: snapshotLoading},
    {label: "累计检查新增", value: studio.data?.monitor_totals.added, loading: studio.loading},
    {label: "重复新增", value: studio.data?.monitor_totals.repeated, loading: studio.loading},
    {label: "已完成养号轮次", value: completed("nurture"), loading: studio.loading},
  ];
  // refreshSnapshot may intentionally swallow a polling error. Its authoritative
  // error prop remains visible even when that refresh promise resolves normally.
  const coreError = snapshotError ? String(snapshotError) : refreshError;
  const tableState = studio.loading ? "正在读取任务与完成记录…" : studio.error ? "任务与完成记录暂不可用，请刷新重试" : "";
  const busy = refreshing || studio.loading || snapshotLoading;

  return <section className="report-data-overview" aria-label="累计数据概览" aria-busy={busy}>
    <div className="report-data-toolbar">
      <p>累计数据与任务汇总，不受上方当日 / 当周 / 当月筛选影响；每日完成记录按 UTC 日期统计</p>
      <button className="formal-button" disabled={refreshing || studio.loading} aria-busy={refreshing} onClick={() => void refreshOverview()}><RefreshCw size={16} />刷新数据概览</button>
    </div>
    {coreError && <div className="formal-error-banner" role="alert">采集与去重统计暂未更新：{coreError}{snapshot && "；已读取数值为上次成功结果"}</div>}
    {studio.error && <div className="formal-error-banner" role="alert">任务与检查统计暂未更新：{studio.error}{studio.data && "；显示上次成功结果"}</div>}
    {studio.data?.scheduler_error && <div className="formal-error-banner" role="alert">{studio.data.scheduler_error}</div>}
    <dl className="report-data-counters">{counters.map(({label, value, loading}) => <div key={label}>
      <dt>{label}</dt><dd>{actualCount(value) ? value.toLocaleString("zh-CN") : loading ? "读取中" : "—"}</dd>
    </div>)}</dl>
    <div className="report-data-tables">
      <section className="report-data-panel" aria-label="任务状态汇总">
        <h3>任务状态汇总</h3>
        <div className="report-data-table-scroll"><table>
          <thead><tr><th scope="col">模块</th><th scope="col">状态</th><th scope="col">任务数</th></tr></thead>
          <tbody>{studio.data?.totals.map(row => <tr key={`${row.kind}:${row.status}`}><td>{kindText[row.kind] || row.kind}</td><td>{statusText[row.status] || row.status}</td><td>{row.count.toLocaleString("zh-CN")}</td></tr>)}</tbody>
        </table></div>
        {tableState ? <p className="report-data-state" role="status">{tableState}</p> : !studio.data?.totals.length && <p className="report-data-state">暂无养号任务</p>}
      </section>
      <section className="report-data-panel" aria-label="每日完成记录（UTC）">
        <h3>每日完成记录（UTC）</h3>
        <div className="report-data-table-scroll"><table>
          <thead><tr><th scope="col">日期</th><th scope="col">模块</th><th scope="col">完成数量</th></tr></thead>
          <tbody>{studio.data?.daily.map(row => <tr key={`${row.day}:${row.kind}`}><td>{row.day}</td><td>{kindText[row.kind] || row.kind}</td><td>{row.count.toLocaleString("zh-CN")}</td></tr>)}</tbody>
        </table></div>
        {tableState ? <p className="report-data-state" role="status">{tableState}</p> : !studio.data?.daily.length && <p className="report-data-state">暂无完成记录</p>}
      </section>
    </div>
  </section>;
}
