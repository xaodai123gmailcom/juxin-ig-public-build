import {useEffect, useRef, useState} from 'react';
import {getCollectorCoreClient} from './core-client';

type Blocker = {task_id:string;name:string;status:string;version:number;window_ids:string[];dismissed:boolean;can_stop:boolean;blocked_reason:string;message:string};
type Lookup = {job_id:string;profile_id:string;blocker:Blocker|null;message?:string};
export function NurtureCollectionBlocker({jobId, disabled, windowNames, onChanged}:{jobId:string;disabled:boolean;windowNames:Map<string,string>;onChanged:()=>Promise<unknown>}) {
  const [lookup,setLookup]=useState<Lookup|null>(null),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const pending=useRef(false),epoch=useRef(0),alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;epoch.current++;};},[]);
  useEffect(()=>{epoch.current++;setLookup(null);setMessage('');},[jobId]);
  async function locate() {
    if(disabled||pending.current)return;
    pending.current=true;setBusy(true);setMessage('');const current=++epoch.current;
    try {
      const result=await getCollectorCoreClient().studioCommand({action:'locate_cleanup_collection',job_id:jobId}) as Lookup;
      if(alive.current&&current===epoch.current){setLookup(result);setMessage(result.message||'');}
    } catch(error) {if(alive.current&&current===epoch.current){setLookup(null);setMessage(String(error));}}
    finally {pending.current=false;if(alive.current)setBusy(false);}
  }
  async function stop() {
    const task=lookup?.blocker;
    if(disabled||pending.current||!task?.can_stop)return;
    const affected=task.window_ids.map(id=>windowNames.get(id)||id).join('、')||'无窗口';
    if(!window.confirm(`确认停止这条暂停的采集任务？\n任务：${task.name||task.task_id}\n编号：${task.task_id}\n关联窗口：${affected}\n仅处理此任务。保留采集结果、历史、归档和去重记录；之后还需核验养号窗口清理。`))return;
    pending.current=true;setBusy(true);setMessage('');const current=++epoch.current;
    try {
      const result=await getCollectorCoreClient().studioCommand({action:'stop_cleanup_collection',job_id:jobId,task_id:task.task_id,version:task.version});
      if(alive.current&&current===epoch.current){setLookup(null);setMessage(String(result.message||'采集任务已停止，请再次核验窗口清理'));}
      try {await onChanged();}
      catch(error) {if(alive.current&&current===epoch.current)setMessage(`采集任务已停止，但列表刷新失败：${String(error)}。请刷新后再次核验窗口清理。`);}
    } catch(error) {if(alive.current&&current===epoch.current){setLookup(null);setMessage(String(error));}}
    finally {pending.current=false;if(alive.current)setBusy(false);}
  }
  return <div className="nurture-cleanup-state" aria-busy={busy}>
    <button className="formal-button" disabled={disabled||busy} onClick={()=>void locate()}>定位关联采集任务</button>
    {lookup?.blocker&&<div role="status">
      <p>关联采集任务：{lookup.blocker.name||lookup.blocker.task_id} · {lookup.blocker.status==='paused'?'已暂停':lookup.blocker.status}</p>
      <p>任务编号：{lookup.blocker.task_id}</p>
      <p>关联窗口：{lookup.blocker.window_ids.map(id=>windowNames.get(id)||id).join('、')||'无窗口'}{lookup.blocker.dismissed?' · 任务列表已归档':''}</p>
      <p>{lookup.blocker.message}</p>
      {lookup.blocker.blocked_reason&&<p>{lookup.blocker.blocked_reason}</p>}
      <button className="formal-button danger" disabled={disabled||busy||!lookup.blocker.can_stop} onClick={()=>void stop()}>停止这条暂停采集任务</button>
    </div>}
    {message&&<p role="status">{message}</p>}
  </div>;
}
