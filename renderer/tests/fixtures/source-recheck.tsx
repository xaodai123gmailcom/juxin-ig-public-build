import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';

const fixture={state:'running',revision:1,fail:false,hold:false,release:null as (()=>void)|null,
  commands:[] as any[],snapshots:0,snapshotFail:false};
Object.assign(window,{sourceRecheckFixture:fixture});
const snapshot=()=>{
 const platform=fixture.state==='unsupported'?'unsupported':'instagram';
 const status=fixture.state==='completed'?'completed':fixture.state==='running'?'running':fixture.state==='cleanup'?'recoverable':'paused';
 const finished=fixture.state!=='rechecking';
 const target={id:'recheck-target',username:'screenshot_source',status,current_window_id:status==='completed'?null:'recheck-window',
  current_stage:status==='completed'?'completed_archived':'screening_accounts',mode_progress:{followers:{source_total:418,discovered:395,processed:365,saved:332,skipped_global_duplicates:33,discarded:201,hover_discarded:7,qualified_for_review:130}},
  mode_coverage:{followers:{discovery_finished:finished,unobserved_count:23,remaining_count:53,pending_count:30,status:'pending'}}};
 return {revision:fixture.revision,generated_at:new Date().toISOString(),counts:{pending_public:0,pending_private:0},dedupe:{total:602831},
  pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},
  windows:[{id:'recheck-window',name:'Window 1',ready:true,opened:true}],sources:[],
  tasks:[{id:'recheck-task',status,modes:['followers'],settings:{platform,parallel_screening_workers:1},targets:[target],windows:[{profile_id:'recheck-window'}],
   runtime:{profile_states:status==='completed'?[]:[{profile_id:'recheck-window',current_target_id:'recheck-target',
    state:fixture.state==='cleanup'?'manual_required':status,
    reason:fixture.state==='cleanup'?'source_recheck_cleanup_failed':undefined}]}}],
  campaigns:[],split_candidates:[],truncated:false,connection:{connected:true,provider:'native'}};
};
(window as any).collectorCore={secureGet:async()=> 'fixture-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:true}),
 async request(path:string,options:any){
  if(path==='/api/session/resume')return {session_token:'fixture-token',user:{id:'fixture-owner',username:'fixture'}};
  if(path.startsWith('/api/workbench/snapshot')){fixture.snapshots++;if(fixture.snapshotFail)throw Error('snapshot fixture failure');return structuredClone({...snapshot(),platform:new URL('http://core'+path).searchParams.get('platform')});}
  if(path==='/api/workbench/commands'){
   const command=options?.body;
   fixture.commands.push(structuredClone(command));
   if(command?.type!=='task_source_recheck')throw Error('Unexpected fixture mutation');
   if(fixture.hold)await new Promise<void>(resolve=>{fixture.release=resolve});
   if(fixture.fail)throw Error('source recheck fixture failure');
   const paused=fixture.state==='paused';
   fixture.state='rechecking';fixture.revision++;
   return {command:command.type,snapshot_seq:fixture.revision,result:{status:paused?'paused':'running',waiting_for_task_resume:paused,parent_only:true}};
  }
  if(path==='/api/workbench/live-status')return {revision:fixture.revision,generated_at:new Date().toISOString(),tasks:[]};
  throw Error('Fixture has no optional data');
 }};
createRoot(document.getElementById('root')!).render(<App/>);
