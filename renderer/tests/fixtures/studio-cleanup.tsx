// Test-only bridge. Production StudioWorkspace still requires the Electron bridge.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {StudioWorkspace} from '../../src/studio-workspace';
import '../../src/formal-workbench.css';
const asset=(id:string)=>({id,name:id,source:'pexels',media_type:'photo',preview:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',attribution:'fixture',source_url:'',desktop_path:'fixture'});
const state={assigned_draft_ids:[] as string[],window_stats:[{profile_id:'window-one',username:'my_ig',posts_count:12,published_count:3,status:'ok',message:'主页读取',checked_at:'2026-09-11T07:00:00Z'},{profile_id:'window-two',username:'',posts_count:null,published_count:0,status:'unavailable',message:'请登录后重试',checked_at:'2026-09-11T07:00:00Z'}],assets:[asset('old-failed-material'),asset('other-material')],jobs:[{id:'failed-job',kind:'posting',profile_id:'window-one',status:'failed',cursor:0,total_steps:1,message:'原始失败记录',created_at:'2026-09-11T01:00:00Z',due_at:'2026-09-11T01:00:00Z',config:{media_type:'photo'},result:{prepared:{asset_ids:['old-failed-material'],caption:'保留的文案',location:''}}}],templates:{posting:{source:'manual',asset_ids:['old-failed-material'],media_type:'photo',query:'',caption:'保留的文案',hashtags:'',location:'',language:'中文',scheduled_at:''}},active_ids:[],totals:[],daily:[],credentials:{pexels_configured:true,ai_configured:true}};
const drafts=new URLSearchParams(location.search).has('drafts');
if(drafts)state.jobs=[0,1].map(i=>({...state.jobs[0],id:'draft-'+i,kind:'material',status:'completed',cursor:1,message:'备稿已保存',result:{prepared:{asset_ids:[state.assets[i].id],caption:'draft caption '+i,location:''}}}));
const automatic=drafts||new URLSearchParams(location.search).has('automatic');
if(automatic)state.templates={} as typeof state.templates;
const fixture={mode:'success',holdNext:false,release:undefined as undefined|(()=>void),commands:[] as unknown[]};
Object.assign(window,{fixture});
window.collectorCore={
 secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:true}),
 async request(path,options){
  if(path.endsWith('/snapshot')){
   const data=structuredClone(state);
   if(fixture.holdNext){fixture.holdNext=false;await new Promise<void>(resolve=>{fixture.release=resolve;});}
   return data as any;
  }
  const body=options?.body as any;fixture.commands.push(body);
  if(body.action==='set_draft_targets'){body.assignments.forEach((a:any)=>Object.assign(state.jobs.find(j=>j.id===a.draft_id)!,{draft_target_profile_id:a.profile_id}));return {saved:body.assignments.length} as any;}
  if(body.action==='start_drafts'){state.assigned_draft_ids.push(...body.assignments.map((a:any)=>a.draft_id));return {job_ids:['publish-0','publish-1']} as any;}
  if(body.action==='delete_unfinished'){const ids=state.jobs.filter(j=>['failed','cancelled'].includes(j.status)&&(!body.job_id||j.id===body.job_id)).map(j=>j.id);state.jobs=state.jobs.filter(j=>!ids.includes(j.id));return {deleted_ids:ids,skipped:[]} as any;}
  if(body.action==='start')return {job_ids:['fixture-job']} as any;
  if(body.action==='save_template'){state.templates.posting=body.config;return {saved:true} as any;}
  if(fixture.mode==='error')throw new Error('文件被占用，请关闭占用程序后重试');
  if(fixture.mode==='blocked')return {deleted_ids:[],deleted_files:0,skipped:[{asset_id:body.asset_id,reason:'窗口一 · 任务 abc12345：结果待确认，请在异常任务中核验发布结果'}]} as any;
  state.assets=state.assets.filter(a=>a.id!==body.asset_id);
  Object.assign(state.jobs[0].result,{cleaned_asset_ids:[body.asset_id]});
  return {deleted_ids:[body.asset_id],deleted_files:3,skipped:[]} as any;
 }
};
const snapshot={windows:automatic?[{id:"window-one",name:"窗口一",group:"测试",locked:false},{id:"window-two",name:"窗口二",group:"测试",locked:false}]:[],counts:{},dedupe:{total:0},connection:{endpoint:'http://127.0.0.1:54345'}} as any;
createRoot(document.getElementById('root')!).render(<StudioWorkspace mode="posting" snapshot={snapshot}/>);
