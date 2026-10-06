import {NurtureCollectionBlocker} from './nurture-collection-blocker';
import {useCallback, useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {Leaf, Monitor, Pause, Play, RefreshCw, Square, Trash2} from 'lucide-react';
import {getCollectorCoreClient, type CoreWorkbenchSnapshot} from './core-client';
import {nurtureWaitLabel} from './nurture-schedule';
import {nurtureCleanupLabel, nurtureConfigIssue, nurtureCount, nurtureElapsed, nurtureNeedsCleanup, nurtureReservedWindows, nurtureSavedConcurrency, nurtureTerminalStatuses, nurtureTimestamp, nurtureVisibleJobs, standaloneNurtureDefaults, type NurtureAccountSnapshot, type NurtureJob} from './standalone-nurture-state';
import './standalone-nurture-workspace.css';

type WindowStats = NurtureAccountSnapshot & {profile_id: string; nurture_count?: number; last_nurture_at?: string};
type Snapshot = {
  jobs: NurtureJob[]; active_ids: string[]; window_stats?: WindowStats[]; scheduler_error?: string;
  templates?: Record<string, Record<string, unknown>>; totals: Array<{kind: string; status: string; count: number}>;
};
type Props = {snapshot: CoreWorkbenchSnapshot; refreshWindows?: () => Promise<unknown>};
const labels: Record<string, string> = {completed: '已完成', failed: '失败', cancelled: '已停止', needs_review: '结果待确认'};
function Panel({title, children}: {title: string; children: ReactNode}) {
  return <section className="formal-panel nurture-panel"><div className="formal-panel-header"><h2>{title}</h2></div><div className="formal-panel-body">{children}</div></section>;
}
function AccountMetrics({account, historic = false}: {account?: NurtureAccountSnapshot; historic?: boolean}) {
  return <div className="nurture-account-snapshot">
    <div className="nurture-account-identity"><strong>{account?.username ? `@${account.username}` : '账号尚未确认'}</strong><span>{account?.checked_at ? `读取于 ${nurtureTimestamp(account.checked_at)}` : historic ? '本次没有已读取的主页数据' : '尚未读取主页数据'}</span></div>
    <dl className="nurture-profile-counts">{[['粉丝', account?.followers_count], ['关注', account?.following_count], ['帖子', account?.posts_count]].map(([label, value]) => <div key={String(label)}><dt>{label}</dt><dd>{nurtureCount(value)}</dd></div>)}</dl>
    {account?.message && account.status !== 'ok' && account.status !== 'success' && <p className="nurture-metric-note">{account.message}</p>}
  </div>;
}
export function StandaloneNurtureWorkspace({snapshot, refreshWindows}: Props) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [readError, setReadError] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [minutes, setMinutes] = useState<number | string>(standaloneNurtureDefaults.minutes);
  const [concurrency, setConcurrency] = useState<number | string>(standaloneNurtureDefaults.concurrency);
  const [selected, setSelected] = useState<string[]>([]);
  const [group, setGroup] = useState('');
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState('plan');
  const [archiveFeedback, setArchiveFeedback] = useState<{text: string; failed: boolean} | null>(null);
  const [startFeedback, setStartFeedback] = useState<{text: string; failed: boolean} | null>(null);
  const [jobFeedback, setJobFeedback] = useState<{jobId: string; text: string; failed: boolean} | null>(null);
  const [recentJobIds, setRecentJobIds] = useState<string[]>([]);
  const alive = useRef(true), pending = useRef(false), initialized = useRef(false), generation = useRef(0), inFlight = useRef(0);
  const archived = useRef(new Map<string, string>());
  const refresh = useCallback(async (poll = false) => {
    if (poll && (inFlight.current || pending.current)) return;
    const current = ++generation.current;
    inFlight.current++;
    try {
      const result = await getCollectorCoreClient().studioSnapshot<Snapshot>();
      if (!alive.current || current !== generation.current) return;
      setData({...result, jobs: result.jobs.map(job => archived.current.has(job.id) ? {...job, deleted_at: job.deleted_at || archived.current.get(job.id)} : job)});
      setReadError('');
      if (!initialized.current) {
        // Old templates must never resurrect custom surfaces, likes or schedules.
        setConcurrency(nurtureSavedConcurrency(result.templates?.nurture?.concurrency));
        initialized.current = true;
      }
    } catch (reason) {
      if (alive.current && current === generation.current) setReadError(`养号数据读取失败：${String(reason)}`);
    } finally { inFlight.current--; }
  }, []);
  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(true), 4000);
    return () => {alive.current = false; generation.current++; clearInterval(timer);};
  }, [refresh]);
  const reserved = useMemo(() => nurtureReservedWindows(data?.jobs || [], data?.active_ids || []), [data]);
  const windowsById = useMemo(() => new Map(snapshot.windows.map(window => [window.id, window])), [snapshot.windows]);
  const blocked = useCallback((id: string) => reserved.has(id) || Boolean(windowsById.get(id)?.locked), [reserved, windowsById]);
  useEffect(() => setSelected(ids => ids.filter(id => windowsById.has(id) && !blocked(id))), [windowsById, blocked]);
  const windows = useMemo(() => snapshot.windows.filter(window => (!group || window.group === group) && [window.id, window.name, window.group, window.serial_number].join(' ').toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) => (a.provider_order ?? a.serial_number ?? 0) - (b.provider_order ?? b.serial_number ?? 0)), [snapshot.windows, group, query]);
  const windowStats = new Map((data?.window_stats || []).map(row => [row.profile_id, row]));
  const jobs = (data?.jobs || []).filter(job => job.kind === 'nurture');
  const cleanupJobs = jobs.filter(nurtureNeedsCleanup);
  const cleanupWindows = new Set(cleanupJobs.map(job => job.profile_id));
  const filteredJobs = nurtureVisibleJobs(jobs, tab, recentJobIds);
  const configIssue = nurtureConfigIssue(minutes, concurrency);
  const selectedIssue = selected.some(blocked) ? '所选窗口已被任务占用，请重新选择' : selected.length > 1000 && Number(concurrency) === 0 ? '全部所选同时执行最多支持 1000 个窗口，请设置同时执行数量' : '';
  const disabled = busy || !data || Boolean(readError);
  async function command(body: Record<string, unknown>, message: string, context?: 'start' | {jobId: string}) {
    if (pending.current) return;
    pending.current = true; generation.current++; setBusy(true); setError(''); setNote('');
    if (context === 'start') setStartFeedback(null);
    else if (context) setJobFeedback({jobId: context.jobId, text: '正在处理…', failed: false});
    try {
      const result = await getCollectorCoreClient().studioCommand(body);
      const feedback = body.operation === 'retry_cleanup' && result.cleanup_reconciled === true
        ? '已核验窗口关闭，本条历史清理占用已解除；原始执行记录已保留'
        : body.operation === 'retry_cleanup' && result.cleanup_pending === true
          ? '窗口清理处理中，确认关闭前将继续保留占用' : message;
      if (alive.current) {
        if (context === 'start') setStartFeedback({text: feedback, failed: false});
        else if (context) setJobFeedback({jobId: context.jobId, text: feedback, failed: false});
        else setNote(feedback);
      }
      // A cleared hold must also refresh the parent's window lock snapshot.
      if (body.operation === 'retry_cleanup') {
        try {await refreshWindows?.();}
        catch (reason) {if (alive.current && context && context !== 'start') setJobFeedback({jobId: context.jobId, text: `核验请求已处理，但窗口列表刷新失败：${String(reason)}。请刷新窗口后查看。`, failed: true});}
      }
      return result;
    } catch (reason) {
      if (alive.current) {
        if (context === 'start') setStartFeedback({text: String(reason), failed: true});
        else if (context) setJobFeedback({jobId: context.jobId, text: String(reason), failed: true});
        else setError(String(reason));
      }
    }
    finally { await refresh(); pending.current = false; if (alive.current) setBusy(false); }
  }
  async function start() {
    if (disabled || configIssue || selectedIssue || !selected.length || pending.current) return;
    const result = await command({action: 'start', kind: 'nurture', config: {minutes: Number(minutes), concurrency: Number(concurrency)}, profile_ids: selected, request_id: crypto.randomUUID()}, '任务已创建；本次任务和失败原因会保留在下方任务计划中', 'start');
    if (alive.current && Array.isArray(result?.job_ids) && result.job_ids.length) {
      const ids = result.job_ids.filter((id): id is string => typeof id === 'string');
      setRecentJobIds(current => [...new Set([...current, ...ids])]); setSelected([]); setTab('plan');
    }
  }
  async function refreshAll() {
    if (refreshing || pending.current) return;
    setRefreshing(true); setError('');
    try {await Promise.all([refresh(), refreshWindows?.()]);} catch (reason) {setError(`窗口刷新失败：${String(reason)}`);} finally {setRefreshing(false);}
  }
  async function archive(job: NurtureJob) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setArchiveFeedback({text: '正在移除异常任务…', failed: false}); setError('');
    try {
      const result = await getCollectorCoreClient().studioCommand({action: 'delete_failed_nurture', job_id: job.id}) as {deleted_ids: string[]; skipped: Array<{job_id: string; reason: string}>};
      generation.current++;
      const deleted = result.deleted_ids.includes(job.id);
      if (deleted) {archived.current.set(job.id, new Date().toISOString()); setData(current => current ? {...current, jobs: current.jobs.map(row => row.id === job.id ? {...row, deleted_at: row.deleted_at || archived.current.get(job.id)} : row)} : current);}
      setArchiveFeedback({text: deleted ? '已从异常列表删除，原始错误和执行记录仍可在历史记录查看。' : `未删除：${result.skipped.map(item => item.reason).join('；') || '任务状态已改变，请刷新后查看。'}`, failed: !deleted});
    } catch (reason) {setArchiveFeedback({text: `删除未完成：${String(reason)}`, failed: true});}
    finally {await refresh(); pending.current = false; setBusy(false);}
  }
  const operate = (job: NurtureJob, operation: string, message: string) => void command({action: 'control', job_id: job.id, operation}, message, {jobId: job.id});
  const total = (...statuses: string[]) => data?.totals.filter(row => row.kind === 'nurture' && statuses.includes(row.status)).reduce((sum, row) => sum + row.count, 0);
  const summaries = [['等待执行', total('queued', 'waiting_window')], ['正在执行', total('running')], ['已完成', total('completed')], ['需处理', data ? (total('failed', 'needs_review') || 0) + cleanupJobs.length : undefined]] as const;
  return <div className="standalone-nurture" aria-busy={!data || busy}>
    {data?.scheduler_error && <div className="formal-error-banner" role="alert">{data.scheduler_error}</div>}
    {readError && <div className="formal-error-banner" role="alert">{readError}{data ? '；当前保留上次记录，刷新成功后可启动新任务' : ''}</div>}
    {error && <div className="formal-error-banner" role="alert">{error}</div>}
    {note && <div className="studio-note" role="status">{note}</div>}
    <div className="nurture-summary">{summaries.map(([label, value]) => <div className="formal-stat" key={label}><span className="formal-stat-icon"><Leaf/></span><div><small>{label}</small><strong>{value == null ? readError ? '读取失败' : '读取中' : value.toLocaleString('zh-CN')}</strong></div></div>)}</div>
    {!!cleanupJobs.length && <div className="nurture-cleanup-notice" role="status"><span>{cleanupJobs.length} 个已完成任务的窗口清理尚待核验，相关窗口继续保留占用。请查看清理任务并核验；历史记录会保留。</span><button className="formal-button" onClick={() => setTab('errors')}>查看清理任务</button></div>}
    <Panel title="选择窗口">
      <div className="nurture-picker-toolbar">
        <label className="formal-field"><span>窗口分类</span><select className="formal-input" value={group} onChange={event => setGroup(event.target.value)}><option value="">全部分类</option>{[...new Set(snapshot.windows.map(window => window.group).filter(Boolean))].map(value => <option key={value} value={value!}>{value}</option>)}</select></label>
        <label className="formal-field"><span>名称、ID、序号或分类</span><input className="formal-input" placeholder="搜索窗口" value={query} onChange={event => setQuery(event.target.value)}/></label>
        <button className="formal-button" disabled={busy || refreshing} onClick={() => void refreshAll()}><RefreshCw size={16}/>{refreshing ? '正在刷新…' : '刷新窗口'}</button>
        <button className="formal-button" disabled={disabled} onClick={() => setSelected(windows.filter(window => !blocked(window.id)).map(window => window.id))}>选择当前筛选</button>
        <button className="formal-button" disabled={busy || !selected.length} onClick={() => setSelected([])}>清空</button>
      </div>
      <p className="nurture-selection-summary" role="status">已选 {selected.length} 个窗口 · 待执行或已占用窗口不可重复选择</p>
      <div className="nurture-window-list">{windows.map(window => {const stats = windowStats.get(window.id); const unavailable = blocked(window.id); return <label className={`nurture-window-card ${selected.includes(window.id) ? 'is-selected' : ''} ${unavailable ? 'is-unavailable' : ''}`} key={window.id} data-window-id={window.id}>
        <div className="nurture-window-heading"><input type="checkbox" aria-label={`选择窗口 ${window.name}`} checked={selected.includes(window.id)} disabled={disabled || unavailable} onChange={() => setSelected(ids => ids.includes(window.id) ? ids.filter(id => id !== window.id) : [...ids, window.id])}/><Monitor size={18}/><div><strong>{window.name}</strong><small>{window.serial_number == null ? '' : `序号 ${window.serial_number} · `}{window.group || '未分类'}</small></div><span className="nurture-availability">{cleanupWindows.has(window.id) ? '清理待核验' : unavailable ? '任务占用' : !data ? '读取中' : readError ? '待刷新' : '可选择'}</span></div>
        <AccountMetrics account={stats}/><div className="nurture-window-history"><span>上次养号：{stats?.last_nurture_at ? nurtureTimestamp(stats.last_nurture_at) : '暂无记录'}</span><span>养号执行：{stats?.nurture_count == null ? '未记录' : `${nurtureCount(stats.nurture_count)} 次`}</span></div>
      </label>;})}</div>
      {!windows.length && <p className="studio-empty">{snapshot.windows.length ? '没有符合筛选条件的窗口' : '暂无窗口，请刷新窗口列表'}</p>}
    </Panel>
    <Panel title="养号设置">
      <fieldset className="nurture-settings" disabled={disabled}>
        <div className="nurture-settings-grid">
          <label className="formal-field"><span>养号时长（分钟）</span><input className="formal-input" type="number" min={1} max={120} step={1} value={minutes} onChange={event => setMinutes(event.target.value === '' ? '' : Number(event.target.value))}/><small>每个窗口，默认 5 分钟</small></label>
          <label className="formal-field"><span>同时执行窗口数量</span><input className="formal-input" type="number" min={0} max={1000} step={1} value={concurrency} onChange={event => setConcurrency(event.target.value === '' ? '' : Number(event.target.value))}/><small>0 表示全部所选窗口</small></label>
        </div>
        <p className="nurture-start-help">启动后先回到当前登录账号的主页，读取粉丝、关注和帖子数量。每次执行都会保留独立历史记录。</p>
        <div className="nurture-start-row"><span role="status">{configIssue || selectedIssue || `已选 ${selected.length} 个窗口 · 最多同时执行 ${Math.min(selected.length, Number(concurrency) || selected.length)} 个`}</span><button className="formal-button primary" aria-describedby={startFeedback ? 'nurture-start-feedback' : undefined} disabled={disabled || !selected.length || Boolean(configIssue || selectedIssue)} onClick={() => void start()}><Play size={17}/>{busy ? '正在处理…' : '启动养号'}</button></div>
        {startFeedback && <p id="nurture-start-feedback" className={`nurture-command-feedback ${startFeedback.failed ? 'is-error' : ''}`} role={startFeedback.failed ? 'alert' : 'status'}>{startFeedback.text}</p>}
      </fieldset>
    </Panel>
    <div className="nurture-tabs" aria-label="养号记录筛选">{[['plan', '任务计划 / 正在执行'], ['errors', '异常任务'], ['history', '历史记录']].map(([key, label]) => <button className={`formal-button ${tab === key ? 'primary' : ''}`} aria-pressed={tab === key} key={key} onClick={() => setTab(key)}>{label}</button>)}<button className="formal-button" disabled={refreshing || busy} onClick={() => void refreshAll()}><RefreshCw size={16}/>刷新</button></div>
    <Panel title={tab === 'history' ? '历史记录' : tab === 'errors' ? '异常任务' : '任务计划'}>
      {archiveFeedback && <div className={`studio-archive-feedback ${archiveFeedback.failed ? 'is-error' : ''}`} role="status">{archiveFeedback.text}</div>}
      {jobFeedback && !filteredJobs.some(job => job.id === jobFeedback.jobId) && <p className={`nurture-command-feedback ${jobFeedback.failed ? 'is-error' : ''}`} role={jobFeedback.failed ? 'alert' : 'status'}>{jobFeedback.text}；任务记录可在历史记录查看。</p>}
      {tab === 'history' && <p className="studio-muted">主页数据为该次任务读取时的记录；缺失项显示“未读取”，不会用当前数据替代。</p>}
      <div className="nurture-job-list">{filteredJobs.map(job => <article className={`nurture-job-card ${job.deleted_at ? 'is-archived' : ''}`} key={job.id} data-job-id={job.id}>
        <div className="nurture-job-heading"><strong>{windowsById.get(job.profile_id)?.name || job.profile_id} · 养号</strong><span className={`formal-status ${nurtureNeedsCleanup(job) ? 'danger' : job.status === 'completed' ? 'success' : ['failed', 'needs_review'].includes(job.status) ? 'danger' : ''}`}>{nurtureNeedsCleanup(job) ? '已完成 · 清理待核验' : labels[job.status] || nurtureWaitLabel(job.status, job.wait_reason)}</span></div>
        <p className="nurture-job-message">{job.wait_message || job.message || '等待任务更新'}</p>
        {(job.result.window_cleanup || nurtureNeedsCleanup(job)) && <div className={`nurture-cleanup-state ${nurtureNeedsCleanup(job) ? 'is-pending' : ''}`}><strong>{nurtureCleanupLabel(job)}</strong>{nurtureNeedsCleanup(job) && <p>核验会检查窗口关闭状态及其他任务占用；只有安全条件满足后才会释放本条清理占用。</p>}{job.result.window_cleanup?.last_error && <p>最近清理记录：{job.result.window_cleanup.last_error}</p>}{job.result.window_cleanup?.confirmed_at && <p>确认于 {nurtureTimestamp(job.result.window_cleanup.confirmed_at)}</p>}{job.result.window_cleanup?.reconciled_at && <p>历史清理核验于 {nurtureTimestamp(job.result.window_cleanup.reconciled_at)}</p>}</div>}
        {nurtureNeedsCleanup(job) && <NurtureCollectionBlocker jobId={job.id} disabled={disabled || Boolean(data?.active_ids.includes(job.id))} windowNames={new Map(snapshot.windows.map(window => [window.id, window.name]))} onChanged={async () => {await refresh(); await refreshWindows?.();}}/>}
        <dl className="nurture-job-times"><div><dt>任务创建</dt><dd>{nurtureTimestamp(job.created_at)}</dd></div><div><dt>开始时间</dt><dd>{nurtureTimestamp(job.result.nurture_started_at)}</dd></div><div><dt>结束时间</dt><dd>{nurtureTimestamp(job.result.nurture_finished_at)}</dd></div><div><dt>实际养号时长</dt><dd>{nurtureElapsed(job.result.nurture_actual_seconds)}</dd></div></dl>
        <AccountMetrics account={job.result.account_snapshot} historic/>
        {job.result.failure && <p className="nurture-job-error">异常步骤：{job.result.failure.stage || '未记录'} · {job.result.failure.message || job.message}<br/>发生于 {nurtureTimestamp(job.result.failure.at)}</p>}
        {job.deleted_at && <p className="studio-archive-label">{nurtureNeedsCleanup(job) ? '已归档 · 清理占用仍需核验 · 历史保留' : '已移出异常列表 · 历史保留'}</p>}
        {job.result.counts && <p className="studio-muted">{Object.entries(job.result.counts).map(([key, value]) => `${({browse: '浏览', browse_seconds: '浏览秒数', skipped: '跳过', like: '点赞', save: '收藏', follow: '关注', comment: '评论'} as Record<string, string>)[key] || key} ${nurtureCount(value)}`).join(' · ')}</p>}
        {!!job.result.nurture_observations?.length && <details className="nurture-job-details"><summary>互动执行记录（含未执行原因）</summary><ol>{job.result.nurture_observations.map((entry, index) => <li key={index}>第 {entry.step} 步 · {entry.detail}</li>)}</ol></details>}
        <div className="nurture-job-actions">{!nurtureTerminalStatuses.has(job.status) && <><button className="formal-button" disabled={busy || job.status === 'paused'} onClick={() => operate(job, 'pause', '已暂停')}><Pause size={15}/>暂停</button><button className="formal-button" disabled={busy || job.status !== 'paused'} onClick={() => operate(job, 'resume', '继续执行')}><Play size={15}/>继续</button><button className="formal-button danger" disabled={busy} onClick={() => operate(job, 'cancel', '停止请求已处理')}><Square size={15}/>停止</button></>}
          {nurtureNeedsCleanup(job) && <button className="formal-button nurture-cleanup-action" aria-label={`核验窗口清理任务 ${job.id}`} disabled={disabled || data?.active_ids.includes(job.id)} onClick={() => operate(job, 'retry_cleanup', '窗口清理核验请求已处理，请查看最新清理状态')}><RefreshCw size={15}/>{data?.active_ids.includes(job.id) ? '窗口清理处理中…' : '核验窗口清理'}</button>}
          {job.status === 'needs_review' && <><button className="formal-button" disabled={busy} onClick={() => {if (window.confirm('已核验本次待确认互动确实成功？确认后仍需点击继续。')) operate(job, 'confirm_actions', '核验结果已保存');}}>确认已执行</button><button className="formal-button" disabled={busy} onClick={() => {if (window.confirm('已核验本次待确认互动未执行？确认后可继续未完成步骤。')) operate(job, 'confirm_no_actions', '核验结果已保存');}}>确认未执行</button><button className="formal-button danger" disabled={busy} onClick={() => operate(job, 'cancel_review', '已停止此轮')}>停止此轮</button></>}
          {job.status === 'failed' && !job.deleted_at && <><button className="formal-button" disabled={busy || data?.active_ids.includes(job.id)} onClick={() => operate(job, 'retry', '失败任务已加入重试')}>重试</button><button className="formal-button danger" disabled={busy || data?.active_ids.includes(job.id)} title={data?.active_ids.includes(job.id) ? '任务正在释放资源，完成后可删除' : '从异常列表删除，保留历史记录'} onClick={() => void archive(job)}><Trash2 size={15}/>删除异常</button></>}
        </div>
        {jobFeedback?.jobId === job.id && <p className={`nurture-command-feedback ${jobFeedback.failed ? 'is-error' : ''}`} role={jobFeedback.failed ? 'alert' : 'status'}>{jobFeedback.text}</p>}
      </article>)}</div>
      {!filteredJobs.length && <p className="studio-empty" role="status">{!data ? readError ? '暂时无法读取任务记录，请刷新重试' : '正在读取任务记录…' : '暂无记录'}</p>}
    </Panel>
  </div>;
}
