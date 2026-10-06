import test from 'node:test';
import assert from 'node:assert/strict';
import {createAccountTaskControl} from '../../dist-electron/account-task-control.js';
const taskKey='a'.repeat(24);
const input=()=>({id:'account-a',documentUrl:'file:///app/#accounts',taskKey,target:'source-a',grant:'grant-a'});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {resolve,reject,promise}};
function fixture(){
 const state={session:'owner-a',url:input().documentUrl,owner:'account-a',visible:true},calls=[];
 let resolve=async()=>({resumed:true});
 const control=createAccountTaskControl({validate:(event,value)=>{calls.push('validate');if(event!=='shell'||value.documentUrl!==state.url||value.id!==state.owner)throw new Error('wrong source')},session:()=>state.session,
   confirm:async()=>{calls.push('confirm');return true},hide:async()=>{state.visible=false;calls.push('hide')},request:async(...args)=>{calls.push(['request',...args]);return resolve(...args)}});
 return {state,calls,control,setRequest:value=>resolve=value};
}
test('r51 begin forwards only owner-scoped task/page fields and waits for Core acknowledgement',async()=>{
 const f=fixture(),pending=deferred();f.setRequest(()=>pending.promise);
 let done=false;const result=f.control.begin('shell',input()).then(x=>{done=true;return x});await Promise.resolve();
 assert.equal(done,false);assert.equal(f.calls.includes('hide'),false);assert.deepEqual(f.calls.find(Array.isArray),['request','/api/accounts/interference',{id:'account-a',task_key:taskKey,target:'source-a'},'owner-a']);
 pending.resolve({grant:'issued',task_key:taskKey,target:'source-a'});assert.equal((await result).grant,'issued');
});
test('r51 resume removes native input before sending the lease-bound resume request',async()=>{
 const f=fixture();f.setRequest(async(path,body)=>{assert.equal(f.state.visible,false);assert.equal(path,'/api/accounts/interference/resume');assert.deepEqual(body,{id:'account-a',task_key:taskKey,grant:'grant-a'});return {resumed:true}});
 assert.equal((await f.control.resume('shell',input())).resumed,true);assert.ok(f.calls.indexOf('hide')<f.calls.findIndex(Array.isArray));
});
test('r51 rejected resume cannot put the interactive native view back',async()=>{
 const f=fixture();f.setRequest(async()=>{throw new Error('stale lease')});await assert.rejects(f.control.resume('shell',input()),/stale lease/);assert.equal(f.state.visible,false);
});
for(const operation of ['begin','resume']){
 for(const field of ['session','url'])test(`r51 ${operation} ignores responses after ${field} changes`,async()=>{
   const f=fixture(),pending=deferred();f.setRequest(()=>pending.promise);const result=f.control[operation]('shell',input());await Promise.resolve();await Promise.resolve();f.state[field]='changed';pending.resolve({grant:'old'});await assert.rejects(result,/状态已变化|wrong source/);
 });
 test(`r51 ${operation} rejects a wrong sender and malformed task key without touching the task`,async()=>{
   const f=fixture();await assert.rejects(f.control[operation]('foreign',input()),/wrong source/);await assert.rejects(f.control[operation]('shell',{...input(),taskKey:'invalid'}),/任务标识无效/);assert.equal(f.calls.some(Array.isArray),false);assert.equal(f.calls.includes('hide'),false);
 });
}
test('r51 begin rejects empty page and resume rejects empty or oversized grant',async()=>{
 const f=fixture();await assert.rejects(f.control.begin('shell',{...input(),target:''}),/页面标识/);
 for(const grant of ['', 'a'.repeat(257)])await assert.rejects(f.control.resume('shell',{...input(),grant}),/授权无效/);
 assert.equal(f.calls.some(Array.isArray),false);assert.equal(f.calls.includes('hide'),false);
});


test('r51 cancelling the native confirmation never asks Core to pause or grant the page',async()=>{
 let requests=0;const control=createAccountTaskControl({validate(){},session:()=> 'owner',confirm:async()=>false,hide:async()=>{throw new Error('must not hide')},request:async()=>{requests++;throw new Error('must not run')}});
 assert.deepEqual(await control.begin('shell',input()),{cancelled:true});assert.equal(requests,0);
});

test('r51 confirmation cannot authorize a route or session that changed while the dialog was open',async()=>{
 const pause=deferred();let current=true,requests=0;
 const control=createAccountTaskControl({validate(){if(!current)throw new Error('changed')},session:()=> 'owner',confirm:()=>pause.promise,hide:async()=>{},request:async()=>{requests++;return {grant:'bad'}}});
 const pending=control.begin('shell',input());current=false;pause.resolve(true);await assert.rejects(pending,/changed/);assert.equal(requests,0);
});
