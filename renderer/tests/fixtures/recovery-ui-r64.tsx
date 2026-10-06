// Synthetic transport only. Rendering, guards, review and recovery handlers are production App code.
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';

const preview='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" fill="#274257"/><text x="20" y="85" fill="white">OFFLINE R64</text></svg>');
const queued={id:'queued-r64',theme:'离线恢复测试',caption:'原始文案 · 保留原文',asset_id:'asset-r64',preview,provider_id:'offline-r64',asset_state:'ready',source_url:'https://www.pexels.com/photo/offline-r64',photographer:'离线素材',profile_id:'w1',expected_username:'fixture.one',status:'queued',queue_revision:11,window_held:false,submitted_at:null,can_modify:false,can_withdraw:true,message:'等待窗口；仅离线数据'};
const held={id:'held-r64',kind:'nurture',profile_id:'w1',status:'completed',deleted_at:'2026-10-04T10:00:00Z',created_at:'2026-10-04T09:00:00Z',message:'历史养号执行完成，清理待核验',result:{window_hold:true,window_cleanup:{state:'lease_lost',last_error:'存在关联暂停采集任务'},nurture_actual_seconds:300,failure:{stage:'原始步骤',message:'历史记录保留'}}};
const blocker={task_id:'86feb8fc-f2a6-404f-9459-11dd3f884103',name:'归档的暂停采集任务',status:'paused',version:7,window_ids:['w1','w2'],dismissed:true,can_stop:true,blocked_reason:'',message:'按任务编号定位，不受归档、筛选或分页影响'};
const snapshot={platform:'instagram',revision:1,generated_at:'2026-10-05T00:00:00Z',counts:{pending_public:0,pending_private:0,total_collected:0,total_public:0,total_private:0},dedupe:{total:3},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},windows:[{id:'w1',name:'1号窗口',group:'离线',serial_number:1,locked:true,opened:false},{id:'w2',name:'2号窗口',group:'离线',serial_number:2,locked:true,opened:false}],sources:[],tasks:[],campaigns:[],split_candidates:[],truncated:false,connection:{connected:true,provider:'native'}};
const posting={jobs:[queued],accounts:[{profile_id:'w1',username:'fixture.one',checked_at:'2026-10-05'},{profile_id:'w2',username:'fixture.two',checked_at:'2026-10-05'}],credentials:{pexels_configured:false},totals:{waiting:1,success:0,failed:0,today_success:0,needs_review:0},timezone:'UTC',pagination:{has_more:false,next_cursor:null,limit:50}};
const studio={jobs:[held],active_ids:[] as string[],templates:{nurture:{concurrency:0}},totals:[{kind:'nurture',status:'completed',count:1}],window_stats:[]};
const history={materials:['asset-r64'],captions:[queued.caption],collection_results:['result-preserved'],dedupe:['hash-preserved'],archive:held.deleted_at};
const fixture={posting,studio,snapshot,blocker,history,commands:[] as any[],reads:[] as string[],unexpected:[] as string[],confirmations:[] as {message:string,accepted:boolean}[],confirmNext:false,inputEvents:[] as any[],polls:{} as Record<string,()=>void>,holdAction:'',pendingAction:'',release:null as null|(()=>void),mode:'normal',cleanups:0,starts:0,stops:0,revision:0};
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
 if(path.startsWith('/api/posting/snapshot')){fixture.reads.push(path);return structuredClone(posting)}
 if(path==='/api/studio/snapshot'){fixture.reads.push(path);return structuredClone(studio)}
 if(path==='/api/workbench/command'&&body.command==='bitbrowser_refresh')return {windows:structuredClone(snapshot.windows),connection:snapshot.connection};
 if(path==='/api/posting/command'||path==='/api/studio/command'){
  fixture.commands.push({path,body:structuredClone(body)});await gate(body.action==='control'?body.operation:body.action);
  if(path==='/api/posting/command'){
   if(body.job_id&&body.job_id!==queued.id)reject('错误任务编号');
   if(body.action==='withdraw'){
    if(queued.status!=='queued'||queued.window_held||queued.submitted_at||!queued.can_withdraw)reject('任务已开始，不能撤回排队');
    Object.assign(queued,{status:'ready',profile_id:'',expected_username:'',queue_revision:queued.queue_revision+1,can_modify:true,can_withdraw:false,message:'已撤回排队；素材和文案保留，请重新选择窗口'});
   }else if(body.action==='assign'){
    if(queued.status!=='ready'||snapshot.windows.find(w=>w.id===body.profile_id)?.locked)reject('目标窗口仍被占用');
    Object.assign(queued,{profile_id:body.profile_id,expected_username:body.expected_username});
   }else if(body.action==='start'){
    const reviewed=body.reviewed?.[0];
    if(body.job_ids?.length!==1||body.job_ids[0]!==queued.id||queued.status!=='ready'||!reviewed||['id','caption','asset_id','profile_id','expected_username','queue_revision'].some(key=>(queued as any)[key]!==reviewed[key])||snapshot.windows.find(w=>w.id===queued.profile_id)?.locked)reject('任务内容或排队状态在审阅后发生变化，请重新检查');
    fixture.starts++;Object.assign(queued,{status:'queued',queue_revision:queued.queue_revision+1,can_modify:false,can_withdraw:true});
   }else{fixture.unexpected.push(path+':'+body.action);reject('不允许的离线发帖动作')}
  }else if(body.action==='locate_cleanup_collection'){
   if(body.job_id!==held.id)reject('错误养号任务');
   return {job_id:held.id,profile_id:held.profile_id,blocker:blocker.status==='paused'?{...structuredClone(blocker),can_stop:fixture.mode!=='live',blocked_reason:fixture.mode==='live'?'仍有活动工作线程，请先正常停止并等待收尾':''}:null,message:blocker.status==='paused'?'':'关联任务已正常停止，请再次核验窗口清理'};
  }else if(body.action==='stop_cleanup_collection'){
   if(fixture.mode==='stale')reject('关联任务已变化，请重新定位后再确认');
   if(fixture.mode==='live'||body.job_id!==held.id||body.task_id!==blocker.task_id||body.version!==blocker.version||blocker.status!=='paused')reject('不能安全停止此任务');
   blocker.status='stopped';blocker.version++;fixture.stops++;
   return {status:'stopped',message:'已正常停止这条暂停采集任务，请再次核验窗口清理'};
  }else if(body.action==='control'&&body.operation==='retry_cleanup'){
   if(body.job_id!==held.id)reject('错误养号任务');
   if(queued.status==='queued'&&queued.profile_id===held.profile_id)reject('发帖仍在排队，请先撤回排队');
   if(blocker.status==='paused')reject('关联采集任务仍已暂停，请先定位并正常停止');
   fixture.cleanups++;held.result.window_hold=false;held.result.window_cleanup={state:'reconciled_closed',last_error:''};snapshot.windows[0].locked=false;
   return {status:'completed',cleanup_pending:false,cleanup_reconciled:true};
  }else{fixture.unexpected.push(path+':'+body.action);reject('不允许的离线养号动作')}
  fixture.revision++;return {accepted:true};
 }
 fixture.unexpected.push(path);reject('Unexpected offline fixture endpoint '+path);
}};
createRoot(document.getElementById('root')!).render(<App/>);
