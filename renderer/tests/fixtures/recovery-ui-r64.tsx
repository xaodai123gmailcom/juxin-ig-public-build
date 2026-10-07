// Synthetic transport only. Rendering, guards, review and recovery handlers are production App code.
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';

const held={id:'held-r64',kind:'nurture',profile_id:'w1',status:'completed',deleted_at:'2026-10-04T10:00:00Z',created_at:'2026-10-04T09:00:00Z',message:'历史养号执行完成，清理待核验',result:{window_hold:true,window_cleanup:{state:'lease_lost',last_error:'存在关联暂停采集任务'},nurture_actual_seconds:300,failure:{stage:'原始步骤',message:'历史记录保留'}}};
const blocker={task_id:'86feb8fc-f2a6-404f-9459-11dd3f884103',name:'归档的暂停采集任务',status:'paused',version:7,window_ids:['w1','w2'],dismissed:true,can_stop:true,blocked_reason:'',message:'按任务编号定位，不受归档、筛选或分页影响'};
const snapshot={platform:'instagram',revision:1,generated_at:'2026-10-05T00:00:00Z',counts:{pending_public:0,pending_private:0,total_collected:0,total_public:0,total_private:0},dedupe:{total:3},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},windows:[{id:'w1',name:'1号窗口',group:'离线',serial_number:1,locked:true,opened:false},{id:'w2',name:'2号窗口',group:'离线',serial_number:2,locked:true,opened:false}],sources:[],tasks:[],campaigns:[],split_candidates:[],truncated:false,connection:{connected:true,provider:'native'}};
const studio={jobs:[held],active_ids:[] as string[],templates:{nurture:{concurrency:0}},totals:[{kind:'nurture',status:'completed',count:1}],window_stats:[]};
const history={collection_results:['result-preserved'],dedupe:['hash-preserved'],archive:held.deleted_at};
const fixture={studio,snapshot,blocker,history,commands:[] as any[],reads:[] as string[],unexpected:[] as string[],confirmations:[] as {message:string,accepted:boolean}[],confirmNext:false,inputEvents:[] as any[],polls:{} as Record<string,()=>void>,holdAction:'',pendingAction:'',release:null as null|(()=>void),mode:'normal',cleanups:0,starts:0,stops:0,revision:0};
Object.assign(window,{recoveryFixture:fixture});
// Narrow confirmation seam: production constructs and calls confirm; only the user's response is controlled.
window.confirm=(message?:string)=>{const accepted=fixture.confirmNext;fixture.confirmNext=false;fixture.confirmations.push({message:String(message),accepted});return accepted;};
for(const type of ['pointerdown','click','keydown','change'])document.addEventListener(type,event=>{const target=(event.target as Element)?.closest('button,select,input,a');if(target)fixture.inputEvents.push({type,trusted:event.isTrusted,label:target.getAttribute('aria-label')||target.textContent?.trim().slice(0,120),key:(event as KeyboardEvent).key});},true);
const interval=window.setInterval.bind(window);
window.setInterval=((handler:TimerHandler,delay?:number,...args:any[])=>{if(delay===3000||delay===4000){fixture.polls[String(delay)]=handler as ()=>void;return -Number(delay)}return interval(handler,delay,...args)}) as typeof window.setInterval;
async function gate(action:string){if(fixture.holdAction!==action)return;fixture.holdAction='';fixture.pendingAction=action;await new Promise<void>(resolve=>fixture.release=resolve);fixture.release=null;fixture.pendingAction='';}
function reject(message:string):never{throw Error(message)}
(window as any).collectorCore={secureGet:async()=>'offline-r64-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>reject('离线测试禁止配置凭据'),async request(path:string,options:any={}){
 const body=options.body||{};
 if(path==='/api/session/resume')return {session_token:'offline-r64-token',user:{id:'offline-r64-owner',username:'fixture'}};
 if(path.startsWith('/api/workbench/snapshot'))return structuredClone(snapshot);
 if(path==='/api/accounts/unread')return {windows:{},total:0,capped:false,unknown:0};
 if(path==='/api/accounts/snapshot')return {plans:[],windows:[],locks:{},events:[],platforms:[],unavailable_platforms:[],last_dm:{},last_following:{}};
 if(path==='/api/workbench/live-status')return {revision:1,generated_at:'2026-10-05T00:00:00Z',tasks:[]};
 if(path==='/api/studio/snapshot'){fixture.reads.push(path);return structuredClone(studio)}
 if(path==='/api/workbench/command'&&body.command==='bitbrowser_refresh')return {windows:structuredClone(snapshot.windows),connection:snapshot.connection};
 if(path==='/api/studio/command'){
  fixture.commands.push({path,body:structuredClone(body)});await gate(body.action==='control'?body.operation:body.action);
  if(body.action==='locate_cleanup_collection'){   if(body.job_id!==held.id)reject('错误养号任务');
   return {job_id:held.id,profile_id:held.profile_id,blocker:blocker.status==='paused'?{...structuredClone(blocker),can_stop:fixture.mode!=='live',blocked_reason:fixture.mode==='live'?'仍有活动工作线程，请先正常停止并等待收尾':''}:null,message:blocker.status==='paused'?'':'关联任务已正常停止，请再次核验窗口清理'};
  }else if(body.action==='stop_cleanup_collection'){
   if(fixture.mode==='stale')reject('关联任务已变化，请重新定位后再确认');
   if(fixture.mode==='live'||body.job_id!==held.id||body.task_id!==blocker.task_id||body.version!==blocker.version||blocker.status!=='paused')reject('不能安全停止此任务');
   blocker.status='stopped';blocker.version++;fixture.stops++;
   return {status:'stopped',message:'已正常停止这条暂停采集任务，请再次核验窗口清理'};
  }else if(body.action==='control'&&body.operation==='retry_cleanup'){
   if(body.job_id!==held.id)reject('错误养号任务');
   if(blocker.status==='paused')reject('关联采集任务仍已暂停，请先定位并正常停止');
   fixture.cleanups++;held.result.window_hold=false;held.result.window_cleanup={state:'reconciled_closed',last_error:''};snapshot.windows[0].locked=false;
   return {status:'completed',cleanup_pending:false,cleanup_reconciled:true};
  }else{fixture.unexpected.push(path+':'+body.action);reject('不允许的离线养号动作')}
  fixture.revision++;return {accepted:true};
 }
 fixture.unexpected.push(path);reject('Unexpected offline fixture endpoint '+path);
}};
createRoot(document.getElementById('root')!).render(<App/>);
