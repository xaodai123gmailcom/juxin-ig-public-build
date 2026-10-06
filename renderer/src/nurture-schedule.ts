export const nurtureScheduleDefaults = {concurrency:0, interval_seconds:0};
type WaitingJob = {queue_order?:number;id:string;kind:string;profile_id:string;status:string;due_at:string;created_at:string};
const unfinished = new Set(['queued','waiting_window','running','paused']);

// Start only the first unfinished round of each free account. Later rounds keep
// their original schedule, and paused/running tasks are never resumed implicitly.
export function waitingNurtureIds(jobs:WaitingJob[], freeProfiles:string[], activeIds:string[]):string[] {
  const free=new Set(freeProfiles),active=new Set(activeIds),seen=new Set<string>(),ids:string[]=[];
  const sorted=jobs.filter(j=>j.kind==='nurture'&&unfinished.has(j.status)).sort((a,b)=>
    a.due_at.localeCompare(b.due_at)||a.created_at.localeCompare(b.created_at)||(a.queue_order??0)-(b.queue_order??0)||a.id.localeCompare(b.id));
  for(const job of sorted){
    if(seen.has(job.profile_id))continue;
    seen.add(job.profile_id);
    if(free.has(job.profile_id)&&!active.has(job.id)&&['queued','waiting_window'].includes(job.status))ids.push(job.id);
  }
  return ids;
}

export function nurtureScheduleSummary(selected:number, concurrency:number, interval:number, scheduled:string):string {
  const parallel=Math.min(selected,concurrency===0?selected:concurrency);
  return `已选 ${selected} 个窗口 · 最多同时执行 ${parallel} 个 · ${scheduled?'按指定时间启动':'立即启动'}${interval>0?` · 相邻计划错开 ${interval} 秒`:' · 窗口不额外错开'}`;
}

export function nurtureWaitLabel(status:string, reason?:string):string {
  if(status==='queued'||status==='waiting_window')return ({scheduled:'等待计划时间',capacity:'等待并发名额',window_busy:'等待窗口释放',previous_round:'等待前一轮',starting:'正在启动',ready:'等待调度'} as Record<string,string>)[reason||'']||'等待执行';
  return ({running:'执行中',paused:'已暂停',cancelled:'已取消',completed:'已完成',failed:'失败',needs_review:'结果待确认'} as Record<string,string>)[status]||status;
}
