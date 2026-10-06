import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/formal-workbench.css';

type Platform='instagram';
const state={revision:1,requests:[] as any[],commands:[] as any[],downloads:[] as string[],empty:false,holdPlatform:null as Platform|null,release:null as (()=>void)|null};
const person=(index:number,decision:string|null=null,visibility='public')=>({id:`instagram-${decision||'pending'}-${visibility}-${index}`,username:`ig_person_${index}`,visibility,profile:{full_name:`IG legacy ${index}`},screening:{} as any,review_stage:1,decision,created_at:'2026-10-01T12:00:00Z'});
const waiting:any[]=[];
const candidates:any[]=[...Array.from({length:503},(_,i)=>person(i)),person(900,'approved'),person(901,'approved'),person(902,'approved','private')];
const scoped=(_platform:Platform)=>state.empty?[]:candidates;
function snapshot(platform:Platform){
 const all=scoped(platform),pending=all.filter(x=>!x.decision),approved=all.filter(x=>x.decision==='approved'&&!x.dismissed_at);
 const lanes=(rows:any[])=>({public:rows.filter(x=>x.visibility==='public'),private:rows.filter(x=>x.visibility==='private')});
 return {platform,revision:state.revision,generated_at:new Date().toISOString(),counts:{total_collected:all.length,total_public:all.filter(x=>x.visibility==='public').length,total_private:1,total_split:0,pending_public:pending.length,pending_private:0,approved_public:approved.filter(x=>x.visibility==='public').length,approved_private:approved.filter(x=>x.visibility==='private').length},dedupe:{total:all.length},pending:lanes(pending),approved:lanes(approved),history:{approvals:all.filter(x=>x.decision==='approved'),manual_rejections:all.filter(x=>x.decision==='rejected'),collection_exclusions:[],tasks:[],actions:[]},windows:[{id:'shared',name:'Shared window',platform:'instagram',ready:true}],sources:[],tasks:[],campaigns:[],split_candidates:waiting,truncated:false,connection:{provider:'native',connected:true},storage:{retained_history:{approved:all.filter(x=>x.decision==='approved').length}}};
}
Object.assign(window,{platformFixture:state});
const originalClick=HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click=function(){if(this.download){state.downloads.push(this.download);return;}originalClick.call(this)};
(window as any).collectorCore={secureGet:async()=> 'fixture-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:true}),
 async request(path:string,options:any={}){
  if(path==='/api/session/resume')return {session_token:'fixture-token',user:{id:'fixture-owner',username:'fixture'}};
  if(path==='/api/accounts/unread')return {windows:{},total:0,capped:false,unknown:0};
  if(path==='/api/workbench/live-status')return {revision:state.revision,generated_at:new Date().toISOString(),tasks:[]};
  if(path==='/api/accounts/snapshot')return {plans:[],events:[],windows:[],last_dm:{},last_following:{},locks:{},platforms:[{id:'instagram',label:'Instagram'},{id:'whatsapp',label:'WhatsApp'}]};
  const body=options.body||{};
  const platform=(path.startsWith('/api/workbench/snapshot')?new URL('http://core'+path).searchParams.get('platform'):body.platform||body.payload?.platform) as Platform;
  state.requests.push({path,body:structuredClone(body),platform});
  if(platform!=='instagram')throw Error('Fixture requires explicit platform');
  if(path.startsWith('/api/workbench/snapshot')){
   const value=structuredClone(snapshot(platform));
   if(state.holdPlatform===platform){state.holdPlatform=null;await new Promise<void>(resolve=>state.release=resolve);state.release=null;}
   return value;
  }
  if(path==='/api/workbench/review/query'){
   const all=scoped(platform).filter(x=>!x.decision),items=all.filter(x=>x.visibility===body.visibility&&x.review_stage===body.review_stage);
   const counts=Object.fromEntries(['public','private'].map(v=>[v,{stage1:all.filter(x=>x.visibility===v&&x.review_stage===1).length,stage2:all.filter(x=>x.visibility===v&&x.review_stage===2).length}]));
   return {platform,items:structuredClone(items.slice(body.offset,body.offset+body.limit)),total:items.length,offset:body.offset,limit:body.limit,has_more:body.offset+body.limit<items.length,counts,snapshot_seq:state.revision};
  }
  if(path==='/api/workbench/commands'){
   const {type,payload}=body;state.commands.push(structuredClone(body));
   const ids=payload.candidate_ids||[payload.candidate_id],items=candidates.filter(x=>ids.includes(x.id));
   if(type==='split_waiting_add'){
    const added=payload.targets.map((url:string)=>({id:'seed-'+platform+'-'+waiting.length,username:url,kind:'manual',queue_state:'queued',allowed_window_ids:payload.allowed_window_ids||[]}));
    waiting.push(...added);
    return {command:type,result:{candidates:added,accepted_ids:added.map((x:any)=>x.id),accepted_count:added.length,duplicates:[]},snapshot_seq:++state.revision};
   }
   if(type==='review_decision')Object.assign(items[0],{decision:payload.decision,reviewed_at:new Date().toISOString()});
   else if(type==='review_stage_move')items.forEach(x=>x.review_stage=payload.to_stage);
   else if(type==='approved_candidate_dismiss')items.forEach(x=>{x.dismissed_at=new Date().toISOString();if(payload.mark_used)x.screening.manual_usage={status:'used'}});
   else throw Error('Unexpected fixture command '+type);
   return {command:type,result:{moved_ids:ids,skipped_ids:[],moved_count:ids.length},snapshot_seq:++state.revision};
  }
  if(path==='/api/workbench/accounts/export'){
   const items=scoped(platform).filter(x=>x.decision==='approved'&&!x.dismissed_at&&x.visibility===body.visibility&&(body.scope==='all'||body.candidate_ids.includes(x.id)));
   return {platform,filename:`Juxin-${body.visibility}-accounts-20261001-120000.csv`,mime_type:'text/csv;charset=utf-8',csv:'\ufeffusername\r\n'+items.map(x=>x.username).join('\r\n'),row_count:items.length,skipped_count:0,visibility:body.visibility,scope:body.scope};
  }
  if(path==='/api/reports/query')return {platform,start:body.start,end:body.end,rows:[],totals:{follow:0,greet:0,split:0,added:0,posting:0,collection:0,check:0,nurture:0,approved:0},total_collected:scoped(platform).length,today_collected:scoped(platform).length,global_dedupe:scoped(platform).length,approved:scoped(platform).filter(x=>x.decision==='approved').length};
  throw Error('Fixture has no optional endpoint: '+path);
 }};
createRoot(document.getElementById('root')!).render(<App/>);
