// Offline synthetic state injected through the real App's desktop bridge.
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';
const preview='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" fill="#274257"/><text x="20" y="85" fill="white">OFFLINE</text></svg>');
const job=(id:string,status='ready')=>({id,theme:'离线测试 · 山林',caption:'保持原始文案 '+id,profile_id:'free-a',expected_username:'fixture.account',asset_id:'asset-'+id,status,message:'仅离线测试',preview,source_url:'https://www.pexels.com/photo/fixture',photographer:'离线占位',provider_id:id,asset_state:'ready',window_held:false,submitted_at:null as string|null,queue_revision:0,can_modify:['preparing','ready','failed'].includes(status),can_withdraw:status==='queued'});
const state={jobs:[job('first'),job('second'),job('unknown','needs_review')],accounts:[{profile_id:'free-a',username:'fixture.account',checked_at:'now'},{profile_id:'locked',username:'busy.account',checked_at:'now'}],credentials:{pexels_configured:false},totals:{waiting:3,success:12,failed:2,today_success:4,needs_review:1},timezone:'UTC'};
const snapshot={platform:'instagram',revision:1,generated_at:new Date().toISOString(),counts:{pending_public:0,pending_private:0,total_collected:0,total_public:0,total_private:0},dedupe:{total:0},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},windows:[{id:'free-a',name:'窗口 01',group:'测试',serial_number:1,locked:false,opened:false},{id:'locked',name:'窗口 02',group:'测试',serial_number:2,locked:true,opened:false}],sources:[],tasks:[],campaigns:[],split_candidates:[],truncated:false,connection:{connected:true,provider:'native'}};
const fixture={state,snapshot,commands:[] as any[],reads:[] as string[],holdSnapshot:false,releaseSnapshot:null as null|(()=>void),poll:null as null|(()=>void)};
Object.assign(window,{postingFixture:fixture});
const interval=window.setInterval.bind(window);
window.setInterval=((handler:TimerHandler,delay?:number,...args:any[])=>{if(delay===3000){fixture.poll=handler as ()=>void;return 0}return interval(handler,delay,...args)}) as typeof window.setInterval;
(window as any).collectorCore={secureGet:async()=>'fixture-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>{state.credentials.pexels_configured=true;return {restarted:false,saved:true,pexels_activated:true}},async request(path:string,options:any={}){
 const body=options.body||{};
 if(path==='/api/session/resume')return {session_token:'fixture-token',user:{id:'fixture-owner',username:'fixture'}};
 if(path.startsWith('/api/workbench/snapshot'))return structuredClone(snapshot);
 if(path==='/api/accounts/unread')return {windows:{},total:0,capped:false,unknown:0};
 if(path==='/api/accounts/snapshot')return {plans:[],windows:[],locks:{},events:[],platforms:[],unavailable_platforms:[],last_dm:{},last_following:{}};
 if(path==='/api/workbench/live-status')return {revision:1,generated_at:new Date().toISOString(),tasks:[]};
 if(path.startsWith('/api/posting/snapshot')){
  fixture.reads.push(path);const next=path.includes('cursor=page-2');const captured=structuredClone({...state,jobs:next?[job('older','failed')]:state.jobs,pagination:{has_more:!next,next_cursor:next?null:'page-2',limit:50}});
  if(fixture.holdSnapshot){fixture.holdSnapshot=false;await new Promise<void>(resolve=>fixture.releaseSnapshot=resolve);fixture.releaseSnapshot=null}
  return captured;
 }
 if(path==='/api/posting/command'){
  fixture.commands.push(structuredClone(body));
  if(body.action==='start'){
   for(const reviewed of body.reviewed){const current=state.jobs.find(j=>j.id===reviewed.id)!;if(current.status!=='ready'||current.window_held||current.submitted_at||['caption','asset_id','profile_id','expected_username'].some(k=>(current as any)[k]!==reviewed[k])||(reviewed.queue_revision??0)!==current.queue_revision)throw Error('任务内容或排队状态在审阅后发生变化，请重新检查')}
   for(const id of body.job_ids){const row=state.jobs.find(j=>j.id===id)!;row.status='queued';row.queue_revision++;row.can_modify=false;row.can_withdraw=true;}
  }
  if(body.action==='withdraw'){
   const row=state.jobs.find(j=>j.id===body.job_id)!;
   if(row.status!=='queued'||row.window_held||row.submitted_at||!row.can_withdraw)throw Error('任务已开始，不能撤回排队');
   row.status='ready';row.profile_id='';row.expected_username='';row.queue_revision++;row.can_modify=true;row.can_withdraw=false;row.message='已撤回排队，保留素材、文案和历史记录；请重新选择窗口、检查并启动';
  }
  if(body.action==='edit')state.jobs.find(j=>j.id===body.job_id)!.caption=body.caption;
  if(body.action==='assign'){const row=state.jobs.find(j=>j.id===body.job_id)!;row.profile_id=body.profile_id;row.expected_username=body.expected_username}
  if(body.action==='cancel')state.jobs=state.jobs.filter(j=>j.id!==body.job_id);
  if(body.action==='retry'){
   const row=state.jobs.find(j=>j.id===body.job_id)! as any;
   if(row.status!=='failed'||row.window_held||!['prepare','review'].includes(row.retry_action))throw Error('该状态不允许重试');
   row.status=row.retry_action==='prepare'?'preparing':'ready';row.retry_action=null;row.failure_stage='';row.failure_code='';row.message='保留原内容，重新检查后才会发布';
  }
  if(body.action==='retry_cleanup'){
   const row=state.jobs.find(j=>j.id===body.job_id)! as any;
   if(!row.window_held||!row.can_retry_cleanup)throw Error('没有可关闭的占用');
   row.window_held=false;row.can_retry_cleanup=false;row.retry_action='review';row.retry_blocked_reason='';row.can_modify=true;
  }
  return {accepted:true};
 }
 if(path==='/api/workbench/command'&&body.command==='bitbrowser_refresh')return {windows:structuredClone(snapshot.windows),connection:snapshot.connection};
 throw Error('Unexpected offline fixture endpoint '+path);
}};
createRoot(document.getElementById('root')!).render(<App/>);
