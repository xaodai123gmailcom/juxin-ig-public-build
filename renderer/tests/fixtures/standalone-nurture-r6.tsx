// Synthetic local data for the production App/StudioWorkspace. No social accounts.
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';
const account = (username: string, followers_count: number | null, following_count: number | null, posts_count: number | null) => ({username, followers_count, following_count, posts_count, checked_at:'2026-10-02T17:30:00Z',status:'ok'});
const job = (id: string, profile_id: string, status: string, result: any = {}) => ({id, profile_id, kind:'nurture',status,cursor:99,total_steps:999,created_at:'2026-10-02T17:29:00Z',due_at:'2026-10-02T17:29:00Z',config:{minutes:5},message:status==='failed'?'主页未能确认当前登录账号，任务已停止并保留记录':'离线模拟任务状态',result});
const state = {jobs:[job('queue-job','queued','queued'),job('pause-job','paused','paused'),job('review-job','review','needs_review'),job('release-job','releasing','failed'),
  job('completed-job','free-a','completed',{account_snapshot:account('own.account.before',120,0,34),nurture_started_at:'2026-10-02T17:31:00Z',nurture_finished_at:'2026-10-02T17:36:04Z',nurture_actual_seconds:300,counts:{browse:22,like:15,browse_seconds:300,skipped:2}}),
  job('cancelled-job','free-b','cancelled',{nurture_started_at:'2026-10-02T17:31:00Z',nurture_finished_at:'2026-10-02T17:31:40Z',nurture_actual_seconds:40,counts:{browse:3,like:1}}),
  job('failed-job','free-b','failed',{account_snapshot:{username:'',followers_count:null,following_count:null,posts_count:null,status:'unavailable',message:'无法确认当前登录账号',checked_at:'2026-10-02T17:31:00Z'},failure:{stage:'读取自己的主页',message:'无法确认当前登录账号；没有继续浏览或互动',at:'2026-10-02T17:31:01Z'},nurture_actual_seconds:0})],
  active_ids:['release-job'],window_stats:[{profile_id:'free-a',...account('own.account.now',1234567890,0,345678),nurture_count:4,last_nurture_at:'2026-10-02T17:31:00Z'},
    {profile_id:'free-b',...account('另一个登录账号',null,null,null),nurture_count:3,last_nurture_at:'2026-10-02T17:31:00Z',status:'unavailable',message:'主页读取失败，请检查窗口登录状态'}],
  assets:[],templates:{nurture:{minutes:99,concurrency:0,rounds:19,surfaces:['feed','stories'],dwell_min:222,like_probability:0,interval_seconds:3600,scheduled_at:'2030-01-01T00:00:00Z'}},totals:[{kind:'nurture',status:'queued',count:1},{kind:'nurture',status:'completed',count:1},{kind:'nurture',status:'failed',count:2}],daily:[],monitor_totals:{added:0,repeated:0},credentials:{ai_configured:false,pexels_configured:false}};
const snapshot = {platform:'instagram',revision:1,generated_at:new Date().toISOString(),counts:{pending_public:0,pending_private:0,total_collected:0,total_public:0,total_private:0},dedupe:{total:0},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},
  windows:['free-a','free-b','locked','queued','paused','review','releasing'].map((id,index)=>({id,name:index===0?'窗口 01 · 这是一个用于验证中文长名称换行的窗口':`窗口 ${index+1}`,group:index<2?'日常账号':'任务占用',serial_number:index+1,locked:id==='locked',opened:false})),sources:[],tasks:[],campaigns:[],split_candidates:[],truncated:false,connection:{connected:true,provider:'native'}};
const fixture = {state,snapshot,commands:[] as any[],holdSnapshot:true,failSnapshot:false,holdCommand:false,failCommand:false,releaseSnapshot:null as null | (()=>void),releaseCommand:null as null | (()=>void),poll:null as null|(()=>void)};
Object.assign(window,{nurtureFixture:fixture});
const interval=window.setInterval.bind(window);
window.setInterval=((handler:TimerHandler,delay?:number,...args:any[])=>{if(delay===4000){fixture.poll=handler as ()=>void;return 0;}return interval(handler,delay,...args);}) as typeof window.setInterval;
(window as any).collectorCore = {secureGet:async()=>'fixture-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:false}),async request(path:string,options:any={}){
  const body=options.body||{};
  if(path==='/api/session/resume')return {session_token:'fixture-token',user:{id:'fixture-owner',username:'fixture'}};
  if(path.startsWith('/api/workbench/snapshot'))return structuredClone(snapshot);
  if(path==='/api/accounts/unread')return {windows:{},total:0,capped:false,unknown:0};
  if(path==='/api/accounts/snapshot')return {plans:[],windows:[],locks:{},events:[],platforms:[],unavailable_platforms:[],last_dm:{},last_following:{}};
  if(path==='/api/workbench/live-status')return {revision:1,generated_at:new Date().toISOString(),tasks:[]};
  if(path==='/api/studio/snapshot'){
    const captured=structuredClone(state);
    if(fixture.holdSnapshot){fixture.holdSnapshot=false;await new Promise<void>(resolve=>fixture.releaseSnapshot=resolve);fixture.releaseSnapshot=null;}
    if(fixture.failSnapshot)throw Error('离线模拟：读取失败，请稍后刷新');
    return captured;
  }
  if(path==='/api/studio/command'){
    fixture.commands.push(structuredClone(body));
    if(fixture.holdCommand){fixture.holdCommand=false;await new Promise<void>(resolve=>fixture.releaseCommand=resolve);fixture.releaseCommand=null;}
    if(fixture.failCommand){fixture.failCommand=false;throw Error('离线模拟：窗口刚刚被其他任务占用');}
    if(body.action==='start'){
      const ids=body.profile_ids.map((id:string)=>{const created=job('new-'+id,id,'queued');created.config=body.config;state.jobs.push(created);return created.id;});return {job_ids:ids};
    }
    if(body.action==='control'){
      const row=state.jobs.find(row=>row.id===body.job_id)!;
      row.status=({pause:'paused',resume:'running',cancel:'cancelled',retry:'queued',confirm_actions:'paused',confirm_no_actions:'paused',cancel_review:'cancelled'} as any)[body.operation];
      if(body.operation==='cancel')row.result={...row.result,nurture_finished_at:'2026-10-02T18:00:00Z',nurture_actual_seconds:17};
      return {status:row.status};
    }
    if(body.action==='delete_failed_nurture'){const row=state.jobs.find(row=>row.id===body.job_id)!;(row as any).deleted_at='2026-10-02T18:00:00Z';return {deleted_ids:[row.id],skipped:[]};}
    throw Error('Unexpected fixture command '+body.action);
  }
  if(path==='/api/workbench/command'&&body.command==='bitbrowser_refresh')return {windows:structuredClone(snapshot.windows),connection:snapshot.connection};
  throw Error('Fixture optional endpoint '+path);
}};
createRoot(document.getElementById('root')!).render(<App/>);
