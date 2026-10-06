// Offline fixture for the real component. No account, provider, or network calls.
// Fixture controls allow browser QA of rejection, recovery, fast failure and races.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {StandaloneNurtureWorkspace} from '../../src/standalone-nurture-workspace';
import type {NurtureJob} from '../../src/standalone-nurture-state';
import '../../src/formal-workbench.css';
import '../../src/workbench-polish-r41.css';

const job = (id: string, profile_id: string, status: string, extra: Partial<NurtureJob> = {}): NurtureJob => ({
  id, profile_id, kind:'nurture', status, created_at:'2026-10-04T10:00:00Z', message:'离线任务记录', result:{}, ...extra,
});
const held = (id: string, profile: string, archived = false) => job(id,profile,'completed',{
  deleted_at:archived ? '2026-10-04T10:10:00Z' : undefined,
  message:'执行已完成；窗口占用凭证已失效，保留清理记录等待核验',
  result:{window_hold:true,window_cleanup:{state:'lease_lost',last_error:'历史关闭确认缺失'},
    failure:{stage:'原始异常步骤',message:'原始异常记录保留'},nurture_actual_seconds:300},
});
const state={jobs:[held('held-job','held'),held('archived-job','archived',true)],active_ids:[] as string[],templates:{nurture:{concurrency:0}},totals:[{kind:'nurture',status:'completed',count:2}],window_stats:[]};
const fixture={state,commands:[] as Record<string,unknown>[],startMode:'fail-fast',cleanupRefusal:'',holdCommand:false,releaseCommand:null as null|(()=>void),poll:null as null|(()=>void),windowRefreshes:0};
Object.assign(window,{nurtureRecoveryFixture:fixture});
window.setInterval=((handler:TimerHandler)=>{fixture.poll=handler as ()=>void;return 0;}) as typeof window.setInterval;
(window as any).collectorCore={secureGet:async()=>null,secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({}),async request(path:string,options:any={}){
  if(path==='/api/studio/snapshot')return structuredClone(state);
  if(path!=='/api/studio/command')throw Error(`Unexpected offline endpoint ${path}`);
  const body=options.body||{};fixture.commands.push(structuredClone(body));
  if(fixture.holdCommand){fixture.holdCommand=false;await new Promise<void>(resolve=>fixture.releaseCommand=resolve);fixture.releaseCommand=null;}
  if(body.action==='control'&&body.operation==='retry_cleanup'){
    const row=state.jobs.find(item=>item.id===body.job_id);
    if(!row||!row.result.window_hold)throw Error('仅可核验仍保留占用的清理任务');
    if(fixture.cleanupRefusal)throw Error(fixture.cleanupRefusal);
    row.result.window_hold=false;
    row.result.window_cleanup={...row.result.window_cleanup,state:'reconciled_closed',reconciled_at:'2026-10-04T11:00:00Z'};
    row.message='已核验窗口关闭，清理占用已释放；历史保留';
    return {status:'completed',cleanup_pending:false,cleanup_reconciled:true};
  }
  if(body.action==='start'){
    if(fixture.startMode==='reject')throw Error('养号窗口清理仍待确认，该窗口不能被其他操作接管');
    const ids=body.profile_ids.map((profile:string,index:number)=>{
      const id=`accepted-${fixture.commands.length}-${index}`;
      state.jobs.unshift(job(id,profile,fixture.startMode==='fail-fast'?'failed':'queued',{
        message:fixture.startMode==='fail-fast'?'读取当前登录账号失败，已停止并保留记录':'已加入队列',
        result:fixture.startMode==='fail-fast'?{failure:{stage:'读取自己的主页',message:'离线模拟：未确认登录账号'}}:{},
      }));return id;
    });return {job_ids:ids};
  }
  throw Error(`Unexpected offline command ${body.action}/${body.operation}`);
}};
const snapshot={windows:['free','held','archived'].map((id,index)=>({id,name:`窗口 ${index+1}`,group:'离线测试',serial_number:index+1,locked:false})),counts:{},dedupe:{total:0}} as any;
createRoot(document.getElementById('root')!).render(<div className="formal-shell"><main style={{padding:24,minWidth:0}}><StandaloneNurtureWorkspace snapshot={snapshot} refreshWindows={async()=>{fixture.windowRefreshes++;}}/></main></div>);
