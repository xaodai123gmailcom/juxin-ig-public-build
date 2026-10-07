import {ChatGPTButton} from '../../src/chatgpt-button';
import {HomeWorkspace} from '../../src/home-workspace';
import {StorageManagementSettings} from '../../src/storage-management-settings';
import {GoogleTranslatorButton} from '../../src/google-translator-button';
import {MessageNotificationSettings} from '../../src/message-notification-settings';
import {AccountUnreadProvider} from '../../src/account-unread';
import {WorkbenchPlatformProvider} from '../../src/workbench-platform';
import {Rail} from '../../src/formal-workbench';
import React, {useState,useEffect} from 'react';
import {createRoot} from 'react-dom/client';
import {AccountWorkspace} from '../../src/account-workspace';
import '../../src/formal-workbench.css';
const fixture={manual:null as null|{active:true;grant:string;task_key:string;target:string},closedPages:[] as string[],closePageCalls:[] as any[],translationCalls:[] as any[],chatgpt:false,chatgptListener:null as null|((v:boolean)=>void),whatsapp:false,omitUsername:false,storageError:false,translation:{engine:'immersive',immersiveMode:'manual',immersiveService:'plugin',immersiveFallbacks:['bing','google'],enabled:false,outgoing:false,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',provider:'google',region:''},watchCalls:[] as string[],interfereCalls:0,allowInterference:false,storageAuto:true,cleanModes:[] as string[],translator:false,translatorListener:null as null|((v:boolean)=>void),notifyEnabled:true,notifyTests:0,focusReceiver:null as null|((profile:string)=>void),independent:false,failProfile:'',unread:1,extra:false,order:['plan','plan2'],cookieFailed:false,fail:true,throwError:false,locked:false,active:false,snapshots:0,opened:true,deleted:false,commands:[] as any[],surfaces:[] as any[]};
if(new URLSearchParams(location.search).has('closed'))fixture.opened=false;
Object.assign(window,{fixture});
const snapshot=()=>({windows:[{id:'profile',name:'恢复验证账号',opened:fixture.opened,locked:fixture.locked}]}) as any;
window.collectorCore={
 async chatTranslation(input){fixture.translationCalls.push(input);if(input.export)return {text:"Hello",count:1};if(input.settings)fixture.translation=input.settings as any;if(input.mode)fixture.translation={...fixture.translation,enabled:input.mode!=='off',engine:input.mode==='off'?fixture.translation.engine:input.mode};return {settings:fixture.translation,hasKey:false,error:''}},
 onChatGPTVisibility(listener){fixture.chatgptListener=listener;return()=>{fixture.chatgptListener=null}},
 async chatgptPage(input){if(!input.boundsOnly)fixture.chatgpt=input.hide?false:!fixture.chatgpt;fixture.chatgptListener?.(fixture.chatgpt);return {visible:fixture.chatgpt}},
 async accountTaskWatch(input){fixture.watchCalls.push(input.target||'source');if(input.target&&fixture.closedPages.includes(input.target))throw new Error('任务页面不可用');return {target:input.target||'source',image:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=',captured_at:new Date().toISOString(),task_key:'fixture-task',operation:'collection',screening_slots:3,manual_control:fixture.manual,title:'Task fixture',pages:[{id:'source',label:'采集页',title:'source'},...Array.from({length:3},(_,i)=>({id:'screen-'+i,label:'1-'+(i+1),title:'screen'}))].filter(p=>!fixture.closedPages.includes(p.id))}},
 async accountCloseTaskPage(input){fixture.closePageCalls.push(input);fixture.closedPages.push(input.target);return {closed:true,message:'已完全关闭此页；该账号任务已暂停。'}},
 async accountInterfere(input){fixture.interfereCalls++;if(!fixture.allowInterference)return {cancelled:true};fixture.manual={active:true,grant:'fixture-grant',task_key:input.taskKey,target:input.target};return fixture.manual},
 onTranslatorVisibility(listener){fixture.translatorListener=listener;return()=>{fixture.translatorListener=null}},
 async googleTranslator(input){if(!input.boundsOnly)fixture.translator=input.hide?false:!fixture.translator;fixture.translatorListener?.(fixture.translator);return {visible:fixture.translator}},
 async storageManagement(input){if(typeof input.auto==='boolean')fixture.storageAuto=input.auto;if(input.clean)fixture.cleanModes.push(String(input.clean));return {policy:{auto:fixture.storageAuto},lastResult:input.clean?'已清理缓存并保留登录资料':'尚未清理',cache:1048576}},
 onAccountFocus(listener){fixture.focusReceiver=listener;return()=>{fixture.focusReceiver=null}},
 async messageNotifications(input){if(input.enabled!==undefined)fixture.notifyEnabled=input.enabled;if(input.test)fixture.notifyTests++;return {enabled:fixture.notifyEnabled,supported:true}},
 secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,configureIntegrations:async()=>({restarted:true}),
 async accountSurface(body){
  const observed:any={...body};fixture.surfaces.push(observed);
  try{
   const result=await ((window as any).testSurface?(window as any).testSurface(body):{attached:body.visible&&!fixture.locked&&!fixture.active});
   observed.attached=Boolean(result.attached);observed.display_state=result.display_state;observed.message=result.message;
   return result;
  }catch(error){observed.error=String(error);throw error}
 },
 async request(path,options){
  if(path.endsWith('/unread'))return {windows:fixture.deleted?{}:{profile:{count:fixture.unread,capped:false,status:fixture.storageError?'storage_error':'live',observed_at:new Date().toISOString()}},total:fixture.deleted?0:fixture.unread,capped:false,unknown:0} as any;
  if(path.endsWith('/snapshot')) {fixture.snapshots++;return {independent_tasks:fixture.independent,windows:fixture.independent?[{id:'profile',opened:fixture.opened}]:[],platforms:[{id:'instagram',label:'Instagram'},{id:'whatsapp',label:'WhatsApp'},{id:'telegram_k',label:'Telegram WebK'},{id:'custom',label:'自定义网页'}],unavailable_platforms:[{id:'signal',label:'Signal',hint:'需客户端'}],plans:fixture.deleted?[]:[{id:'plan',created_at:'2026-09-10T10:20:30Z',last_opened_at:'2026-09-12T11:30:40Z',name:'恢复验证账号',username:fixture.omitUsername?'':'test.user',platform:fixture.whatsapp?'whatsapp':'instagram',native:true,profile_id:'profile',serial:1,revision:1,group:'',notes:'',proxy_server:'',environment:{}},...(fixture.extra?[{id:'plan2',name:'窗口二',profile_id:'profile2',platform:'whatsapp',native:true,revision:1,environment:{}}]:[])].sort((a,b)=>fixture.order.indexOf(a.id)-fixture.order.indexOf(b.id)),events:[],last_dm:{},last_following:{},locks:fixture.locked?{profile:{operation_type:'studio'}}:fixture.active?{profile:{operation_type:'account'}}:{}} as any;}
  fixture.commands.push(options?.body);fixture.active=true;
  await new Promise(r=>setTimeout(r,100));fixture.active=false;
  const action=(options?.body as any)?.action;if(action==='reorder')fixture.order=(options?.body as any).ids;if(action==='save_with_cookies')return {saved:true,id:'cookie-plan',revision:1,cookie_failed:fixture.cookieFailed,login_verified:false,message:fixture.cookieFailed?'已保存，导入失败':''} as any;if(action==='import_cookies')return {opened:true,login_verified:false} as any;if(!fixture.fail&&!fixture.throwError){if(action==='close'||action==='reset_whatsapp_storage')fixture.opened=false;if(action==='open')fixture.opened=true;if(action==='delete'){fixture.opened=false;fixture.deleted=true}}
  if((options?.body as any)?.id===fixture.failProfile)throw new Error('此窗口失败');
  if(fixture.throwError)throw new Error('连接失败');
  return {opened:true,page_loaded:!fixture.fail,message:fixture.fail?'网页请求失败（HTTP 403）':''} as any;
 }
};
function Fixture(){
 const [value,setValue]=useState(snapshot);
 const [route,setRoute]=useState(location.hash);
 useEffect(()=>{const update=()=>setRoute(location.hash);window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update)},[]);
 return <AccountUnreadProvider><div className="formal-shell" style={{height:'100vh',display:'flex'}}><Rail mode={(!route||route==='#/accounts')?'accounts':'collection'}/><div style={{flex:1,minWidth:0}}><nav className="formal-header" style={{height:60,display:'flex',alignItems:'center'}}><div id="account-header-actions"/><div id="account-translation-actions"/><ChatGPTButton/><GoogleTranslatorButton/><a href="#/accounts">账号</a><a href="#/collection">采集</a><a href="#/review">审核</a><a href="#/nurture">养号</a><a href="#/settings">设置</a></nav>{(!route||route==='#/accounts')?<AccountWorkspace snapshot={value} onChanged={async()=>setValue(snapshot())}/>:route==='#/'?<HomeWorkspace/>:route==='#/settings'?<><MessageNotificationSettings/><StorageManagementSettings/></>:<section id="other-page" style={{height:'90vh',background:'#19334c'}}>其他任务页面 {route}</section>}</div></div></AccountUnreadProvider>;
}
createRoot(document.getElementById('root')!).render(<WorkbenchPlatformProvider><Fixture/></WorkbenchPlatformProvider>);
