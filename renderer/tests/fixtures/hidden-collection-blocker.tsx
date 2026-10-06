import React from 'react';
import {createRoot} from 'react-dom/client';
import {NurtureCollectionBlocker} from '../../src/nurture-collection-blocker';
import '../../src/formal-workbench.css';
import '../../src/standalone-nurture-workspace.css';
const w=window as any;
w.calls=[];w.mode='normal';w.refreshes=0;w.delay=0;
w.collectorCore={request:async(path:string,options:any)=>{
  w.calls.push({path,body:options.body});
  if(w.delay)await new Promise(resolve=>setTimeout(resolve,w.delay));
  if(w.mode==='error')throw Error('关联任务已变化，请重新定位后再确认');
  if(options.body.action==='stop_cleanup_collection')return {status:'stopped',message:'已正常停止这条暂停采集任务，请再次核验窗口清理'};
  return {job_id:'held',profile_id:'w1',blocker:{task_id:'86feb8fc-f2a6-404f-9459-11dd3f884103',name:'旧采集任务',status:'paused',version:7,window_ids:['w1'],dismissed:true,can_stop:w.mode!=='live',blocked_reason:w.mode==='live'?'仍有活动工作线程，请先通过正常任务控制停止并等待收尾':'',message:'此入口按任务编号读取，不受列表归档、筛选、分页或任务卡显示条件影响'}};
}};
const root=createRoot(document.getElementById('root')!);
w.unmount=()=>root.unmount();
root.render(<main className="formal-content" style={{maxWidth:880,margin:'40px auto'}}><h2>1号窗口 · 已完成 · 清理待核验</h2><NurtureCollectionBlocker jobId="held" disabled={false} windowNames={new Map([['w1','1号窗口']])} onChanged={async()=>{w.refreshes++;}}/></main>);
