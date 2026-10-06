import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '../../src/App';
import '../../src/global.css';
import '../../src/workbench-polish-r55.css';
import '../../src/workbench-density.css';
import '../../src/workbench-r93.css';
const fixture={snapshots:0,resumes:0,fail:false,successes:0,failures:0,revision:1,
 requests:[] as string[],browserCalls:[] as Array<{method:string,input?:any}>,snapshotStarts:[] as number[],holdNext:false,releaseSnapshot:null as (()=>void)|null};
Object.assign(window,{routeFixture:fixture});
const snapshot={revision:1,generated_at:new Date().toISOString(),counts:{pending_public:0,pending_private:0},dedupe:{total:0},pending:{public:[],private:[]},approved:{public:[],private:[]},history:{manual_rejections:[],collection_exclusions:[]},windows:[],sources:[],tasks:[],campaigns:[],split_candidates:[],source_revision:'stability-r94',truncated:true,has_more:{tasks:true,approval_history:true},connection:{connected:true,provider:'native'}};
(window as any).collectorCore={secureGet:async()=> 'fixture-token',secureSet:async()=>true,secureDelete:async()=>true,configureIntegrations:async()=>{fixture.browserCalls.push({method:'configureIntegrations'});return {restarted:true}},
 accountSurface:async(input:any)=>{fixture.browserCalls.push({method:'accountSurface',input});return {}},
 accountWindow:async()=>{fixture.browserCalls.push({method:'accountWindow'});return {}},
 async request(path:string){
  fixture.requests.push(path);
  if(path==='/api/session/resume'){fixture.resumes++;return {session_token:'fixture-token',user:{id:'fixture-owner',username:'fixture'}};}
  if(path.startsWith('/api/workbench/snapshot')){
   fixture.snapshots++;fixture.snapshotStarts.push(performance.now());
   if(fixture.holdNext){fixture.holdNext=false;await new Promise<void>(resolve=>{fixture.releaseSnapshot=resolve});fixture.releaseSnapshot=null;}
   if(fixture.fail){fixture.failures++;throw new Error('Temporary Core failure');}
   fixture.successes++;return structuredClone({...snapshot,platform:new URL('http://core'+path).searchParams.get('platform'),revision:fixture.revision,dedupe:{total:fixture.revision}});
  }
  if(path==='/api/accounts/snapshot')return {plans:[],windows:[],locks:{},events:[],platforms:[],unavailable_platforms:[],last_dm:{},last_following:{}};
  if(path==='/api/accounts/unread')return {windows:{},total:0,capped:false,unknown:0};
  throw new Error('Fixture has no optional data');
 }};
createRoot(document.getElementById('root')!).render(<App/>);
