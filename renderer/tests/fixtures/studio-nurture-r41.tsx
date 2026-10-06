// Local fixture only: exercises the real React UI without a Core or social account.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {StudioWorkspace} from '../../src/studio-workspace';
import '../../src/formal-workbench.css';
import '../../src/workbench-polish-r41.css';
const makeJob=(id:string,status='failed',deleted_at:string|null=null)=>({id,kind:'nurture',profile_id:id,status,deleted_at,cursor:1,total_steps:22,message:`${id}：没有可浏览的快拍`,created_at:'2026-09-12T00:37:08Z',due_at:'2026-09-12T00:37:08Z',config:{},result:{failure:{stage:'完成 1/22 步',message:'没有可浏览的快拍',at:'2026-09-12T00:38:01Z'},counts:{browse:1}}});
const state={jobs:[makeJob('failed-one'),makeJob('failed-two'),makeJob('active-cleanup'),makeJob('needs-confirmation','needs_review'),makeJob('archived','failed','2026-09-17T00:00:00Z'),makeJob('running','running')],assets:[],templates:{nurture:{}},totals:[],daily:[],monitor_totals:{added:0,repeated:0},credentials:{pexels_configured:false,ai_configured:false},active_ids:['active-cleanup','running']};
const fixture={mode:'success',holdNext:false,holdCommand:false,snapshots:0,release:undefined as undefined|(()=>void),releaseCommand:undefined as undefined|(()=>void),commands:[] as any[],poll:undefined as undefined|(()=>void)};
Object.assign(window,{fixture});
// Drive the four-second poll deterministically, without sleeping in the tests.
const originalInterval=window.setInterval.bind(window);
window.setInterval=((handler:TimerHandler,delay?:number,...args:any[])=>{if(delay===4000){fixture.poll=handler as ()=>void;return 0;}return originalInterval(handler,delay,...args)}) as typeof window.setInterval;
window.collectorCore={secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:true}),async request(path,options){
 if(path.endsWith('/snapshot')){fixture.snapshots++;const data=structuredClone(state);if(fixture.holdNext){fixture.holdNext=false;await new Promise<void>(resolve=>{fixture.release=resolve;});}return data as any;}
 const body=options?.body as any;fixture.commands.push(body);
 if(fixture.holdCommand){fixture.holdCommand=false;await new Promise<void>(resolve=>{fixture.releaseCommand=resolve;});}
 if(fixture.mode==='error')throw new Error('本机服务暂时不可用');
 if(body.action==='delete_failed_nurture'){
   if(fixture.mode==='blocked')return {deleted_ids:[],skipped:[{job_id:body.job_id,reason:'任务仍在释放窗口，请稍后重试'}]} as any;
   const job=state.jobs.find(j=>j.id===body.job_id)!;job.deleted_at=new Date().toISOString();
   return {deleted_ids:[job.id],skipped:[]} as any;
 }
 if(body.action==='control'&&body.operation==='retry'){state.jobs.find(j=>j.id===body.job_id)!.status='queued';return {status:'queued'} as any;}
 throw new Error('Unexpected fixture command');
}};
const snapshot={windows:state.jobs.map((j,i)=>({id:j.id,name:`窗口 ${i+1}`,group:'测试分组',serial_number:i+1,locked:j.status==='running'})),counts:{},dedupe:{total:23084},connection:{endpoint:'http://127.0.0.1:54345'}} as any;
createRoot(document.getElementById('root')!).render(<div className="formal-shell"><aside className="formal-rail"><div className="formal-brand">聚鑫</div><nav className="formal-nav"><a>首页</a><a>账号</a><a aria-current="page" data-nav="nurture">养号</a><a>采集</a><a>审核</a><a>报表</a><a>历史</a></nav></aside><main><header className="formal-header"><div><h1>养号</h1><p>离线交互预览 · 示例任务 · 不连接账户，不保存操作</p></div></header><div style={{padding:'24px'}}><StudioWorkspace mode="nurture" snapshot={snapshot}/></div></main></div>);
