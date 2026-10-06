import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Monitor, LockKeyhole, LogIn, X } from 'lucide-react';
import type { AccountPageDisplay, AccountTaskFrame } from './core-client';

type Props={id:string;native:boolean;opened:boolean;locked:boolean;visible:boolean;disabled:boolean;onOpen:()=>void;taskWatch?:boolean};
export function AccountBrowserSurface({id,native,opened,locked,visible,disabled,onOpen,taskWatch=false}:Props){
  const target=useRef<HTMLDivElement>(null),liveTarget=useRef<HTMLDivElement>(null);
  const latestPresentation=useRef({id,visible});
  latestPresentation.current={id,visible};
  const [error,setError]=useState('');
  const surfaceSerial=useRef(0),instance=useRef(crypto.randomUUID());
  const [preview,setPreview]=useState<{id:string;image:string}|null>(null);
  const closeEpoch=useRef(0),watchReadEpoch=useRef(0),pageClosePending=useRef(false);
  useEffect(()=>{closeEpoch.current++;watchReadEpoch.current++;pageClosePending.current=false;setClosingPage('');return()=>{closeEpoch.current++;watchReadEpoch.current++}},[id,locked]);
  const [closingPage,setClosingPage]=useState(''),[pageMessage,setPageMessage]=useState('');
  const [mounted,setMounted]=useState(false);
  const [manualPending,setManualPending]=useState<'begin'|''>('');
  const manualControlPending=useRef(false);
  useEffect(()=>{manualControlPending.current=false;setManualPending('')},[id,locked]);
  const [watchedFrame,setFrame]=useState<(AccountTaskFrame & {accountId:string})|null>(null);
  // A layout effect runs before the passive reset below on account changes.
  // Never present a cached target belonging to the previously selected account.
  const frame=watchedFrame?.accountId===id?watchedFrame:null;
  const [surfaceDisplay,setSurfaceDisplay]=useState<(AccountPageDisplay & {accountId:string;target?:string})|null>(null);
  const [viewTarget,setViewTarget]=useState('');
  const [viewLabel,setViewLabel]=useState('');
  function chooseTaskPage(page:{id:string;label:string}){
    if(viewLabel===page.label&&viewTarget===page.id)return;
    // The selected label changes immediately; the old task page and any manual
    // input grant must not remain presented while replacement metadata loads.
    watchReadEpoch.current++;setFrame(null);setViewLabel(page.label);setViewTarget(page.id);
  }
  const watching=Boolean(taskWatch&&id&&native&&locked&&visible);
  useEffect(()=>{setViewTarget('');setViewLabel('');setFrame(null);setPageMessage('')},[id,locked]);
  useEffect(()=>{
    if(!watching)return;
    let cancelled=false,inFlight=false,resolvedTarget=viewTarget;
    const update=async()=>{
      if(cancelled||inFlight||pageClosePending.current||manualControlPending.current||document.visibilityState!=='visible')return;inFlight=true;
      const readEpoch=watchReadEpoch.current;
      try{
        const api=window.collectorCore?.accountTaskWatch;if(!api)throw new Error('请使用新版桌面程序查看任务画面');
        let next:AccountTaskFrame;
        if(viewLabel){
          let exact:AccountTaskFrame|undefined;
          if(resolvedTarget){try{exact=await api({id,documentUrl:location.href,target:resolvedTarget})}catch{resolvedTarget=''}}
          if(exact&&exact.pages.some(page=>page.id===exact?.target&&page.label===viewLabel))next=exact;
          else{
            const listing=await api({id,documentUrl:location.href});
            const replacement=listing.pages.find(page=>page.label===viewLabel);
            resolvedTarget=replacement?.id||'';
            next=replacement?(listing.target===replacement.id?listing:await api({id,documentUrl:location.href,target:replacement.id})):
              {...listing,target:'',waiting_target:viewLabel,image:'',manual_control:null,display_state:'blank',display_message:`等待 ${viewLabel} 子页面就绪`};
          }
        }else next=await api({id,documentUrl:location.href,target:viewTarget||undefined});
        if(!cancelled&&readEpoch===watchReadEpoch.current){setFrame({...next,accountId:id});setError('')}
      }catch(e){if(!cancelled&&readEpoch===watchReadEpoch.current){setError(String(e));setFrame(f=>f?{...f,manual_control:null}:f);if(viewTarget&&!viewLabel){setFrame(null);setViewTarget('')}}}finally{inFlight=false}
    };
    void update();const timer=setInterval(()=>void update(),1000);return()=>{cancelled=true;clearInterval(timer)};
  },[watching,id,viewTarget,viewLabel]);
  async function closeTaskPage(target:string){
    if(pageClosePending.current||manualControlPending.current||frame?.manual_control?.active||!frame?.task_key)return;const epoch=closeEpoch.current;
    pageClosePending.current=true;watchReadEpoch.current++;setClosingPage(target);setPageMessage('');
    try{
      const api=window.collectorCore?.accountCloseTaskPage;if(!api)throw new Error('请使用新版桌面程序关闭任务页面');
      const result=await api({id,documentUrl:location.href,taskKey:frame.task_key,target});
      if(epoch!==closeEpoch.current)return;
      if(!result.closed)throw new Error('页面尚未完全关闭');
      setPageMessage(result.message);setFrame(null);setViewTarget('');
    }catch(e){if(epoch===closeEpoch.current)setPageMessage(String(e))}finally{if(epoch===closeEpoch.current){pageClosePending.current=false;setClosingPage('')}}
  }
  const manualControl=frame?.manual_control?.active===true?frame.manual_control:undefined;
  const manualActive=Boolean(watching&&manualControl?.active&&manualControl.grant&&manualControl.task_key===frame?.task_key);
  const manualEditable=Boolean(manualActive&&manualControl?.target===frame?.target&&!manualPending);
  async function beginManualControl(){
    if(manualControlPending.current||pageClosePending.current||manualActive||!frame?.task_key||!frame.target||frame.operation!=='collection')return;
    const epoch=closeEpoch.current,taskKey=frame.task_key,target=frame.target;
    manualControlPending.current=true;watchReadEpoch.current++;setManualPending('begin');setPageMessage('请确认是否干扰当前任务…');
    try{
      const api=window.collectorCore?.accountInterfere;if(!api)throw new Error('请使用新版桌面程序操作任务页面');
      const result=await api({id,documentUrl:location.href,taskKey,target});
      if(epoch!==closeEpoch.current)return;
      if(result.cancelled){setPageMessage('');return;}
      if(!result.grant||result.task_key!==taskKey||result.target!==target)throw new Error('任务操作授权已变化，请重新查看');
      const grant=result.grant;
      setFrame(f=>f&&f.accountId===id&&f.task_key===taskKey?{...f,manual_control:{active:true,grant,task_key:taskKey,target}}:f);
      setPageMessage(result.message||'任务已暂停，可操作当前页面；操作后可在采集任务中点击继续。');
    }catch(e){if(epoch===closeEpoch.current)setPageMessage(String(e))}
    finally{if(epoch===closeEpoch.current){manualControlPending.current=false;setManualPending('')}}
  }
  useEffect(()=>{
    if(!watching)return;
    return window.collectorCore?.onTaskInterference?.(target=>{
      if(target===frame?.target)void beginManualControl();
    });
  },[watching,id,frame?.target,frame?.task_key,manualActive]);
  const presentedTarget=watching?frame?.target:undefined;
  const show=Boolean(id&&native&&(opened||frame)&&(!locked||(watching&&frame))&&(!watching||frame?.target)&&visible);
  useLayoutEffect(()=>{
    let cancelled=false,inFlight:object|undefined,updatePending=false,pendingForce=false,lastBoundsKey='',documentHidden=false;
    const documentUrl=window.location.href;
    const surfaceId=instance.current+':'+(++surfaceSerial.current);
    setError('');setMounted(false);setSurfaceDisplay(null);
    const bridge=window.collectorCore?.accountSurface;
    if(!bridge){if(show)setError('请使用更新后的桌面程序打开内置窗口。');return}
    const update=async(force=true):Promise<void>=>{
      if(cancelled)return;
      if(show&&document.visibilityState!=='visible'){suspend();return}
      documentHidden=false;
      const rect=(watching?liveTarget.current:target.current)?.getBoundingClientRect();
      const bounds=rect?{x:rect.x,y:rect.y,width:rect.width,height:rect.height}:undefined;
      const surfaceVisible=show&&Boolean(target.current?.isConnected)&&window.location.href===documentUrl&&document.visibilityState==='visible';
      const boundsKey=JSON.stringify([surfaceVisible,bounds]);
      if(!force&&boundsKey===lastBoundsKey)return;
      if(inFlight){updatePending=true;pendingForce=pendingForce||force;return}
      const request={};inFlight=request;
      lastBoundsKey=boundsKey;
      try{
        const result=await bridge({surfaceId,id,readOnly:watching&&!manualEditable,interferenceGrant:manualEditable?manualControl?.grant:undefined,viewTarget:presentedTarget,visible:surfaceVisible,documentUrl,bounds});
        if(!cancelled&&inFlight===request){
          setMounted(Boolean(result.attached));
          setSurfaceDisplay({accountId:id,target:presentedTarget,display_state:result.display_state,display_message:result.display_message});
          setError(watching&&result.display_state&&result.display_state!=='ready'?'':result.message||result.display_message||'');
        }
      }catch(e){if(!cancelled&&inFlight===request){lastBoundsKey='';setMounted(false);setError(String(e))}}finally{
        if(inFlight===request){
          inFlight=undefined;
          // Re-read current geometry, retaining forced permission heartbeats
          // while discarding duplicate resize/scroll notifications.
          if(updatePending&&!cancelled){const forced=pendingForce;updatePending=false;pendingForce=false;void update(forced)}
        }
      }
    };
    const previewEligible=()=>show&&latestPresentation.current.id===id&&!latestPresentation.current.visible&&window.location.href===documentUrl&&document.visibilityState==='visible';
    // Only an overlay on this same account can display the saved preview.
    // Departed accounts/routes must hide immediately without screenshot encoding.
    const hide=()=>bridge({surfaceId,id,visible:false,documentUrl,capture:previewEligible()}).then(result=>{if(result.preview&&previewEligible())setPreview({id,image:result.preview})}).catch(()=>{});
    const suspend=()=>{
      if(documentHidden)return;
      documentHidden=true;inFlight=undefined;updatePending=false;pendingForce=false;lastBoundsKey='';
      setMounted(false);void hide();
    };
    void update();
    // A hidden surface needs one synchronization, not ongoing geometry work.
    // The shown branch retains its permission heartbeat and latest-bounds queue.
    if(!show)return()=>{cancelled=true;void hide()};
    const timer=setInterval(()=>void update(),1000);
    const updateGeometry=()=>void update(false);
    const updateVisibility=()=>void update();
    // Native BrowserWindow resize revokes its display grant even when a
    // fixed-size element's ResizeObserver does not report a changed size.
    const updateWindow=()=>void update();
    const observer=new ResizeObserver(updateGeometry);
    if(target.current)observer.observe(target.current);
    if(watching&&liveTarget.current)observer.observe(liveTarget.current);
    const leave=()=>{cancelled=true;void hide()};
    window.addEventListener('hashchange',leave);window.addEventListener('pagehide',leave);
    window.addEventListener('resize',updateWindow);window.addEventListener('scroll',updateGeometry,true);document.addEventListener('visibilitychange',updateVisibility);
    return()=>{cancelled=true;window.removeEventListener('hashchange',leave);window.removeEventListener('pagehide',leave);clearInterval(timer);observer.disconnect();window.removeEventListener('resize',updateWindow);window.removeEventListener('scroll',updateGeometry,true);document.removeEventListener('visibilitychange',updateVisibility);void hide()};
  },[id,show,presentedTarget,watching,manualEditable,manualControl?.grant]);
  const currentDisplay=surfaceDisplay?.accountId===id&&surfaceDisplay.target===presentedTarget&&surfaceDisplay.display_state?surfaceDisplay:frame;
  const taskStatuses={
    blank:{title:'任务页面正在准备',message:'这个页面尚未加载网址，等待当前任务使用。'},
    loading:{title:'任务网页正在加载',message:'网页内容尚未就绪，加载完成后会自动显示。'},
    load_failed:{title:'任务网页加载失败',message:'可在采集任务中查看重试进度；其他页面仍可继续查看。'},
    crashed:{title:'任务网页渲染异常',message:'此页面暂时无法显示，请查看对应任务的恢复状态。'},
    unresponsive:{title:'任务网页暂时无响应',message:'页面响应恢复后会自动显示，请查看对应任务的进度。'},
    ready:{title:'正在显示任务页面',message:'正在连接此任务页面的实时画面。'},
  };
  const taskStatus=mounted?{title:'任务画面已显示',message:''}:taskStatuses[currentDisplay?.display_state||'ready']||taskStatuses.ready;
  const taskPages=[...(frame?.pages||[])];
  for(let slot=1;slot<=Math.min(frame?.screening_slots||0,3);slot++){
    const label='1-'+slot;
    if(!taskPages.some(page=>page.label===label))taskPages.push({id:'',label,title:'等待子页面就绪',display_state:'blank'});
  }
  taskPages.sort((a,b)=>(a.label==='采集页'?0:/^1-[1-3]$/.test(a.label)?Number(a.label.slice(2)):10)-(b.label==='采集页'?0:/^1-[1-3]$/.test(b.label)?Number(b.label.slice(2)):10));
  return <div className="account-browser-surface" ref={target} aria-label="内置浏览器网页区域">
    {!visible&&preview?.id===id&&<img className="account-browser-preview" src={preview.image} alt="当前聊天画面" aria-hidden="true"/>}
    {watching?<div className="account-task-watch"><div className="account-task-watch-toolbar"><strong>任务画面</strong><span>{frame?.waiting_target?frame.target?`${frame.waiting_target}加载中`:`等待 ${frame.waiting_target} 子页就绪`:manualPending?'正在确认手动操作…':manualActive?'已进入手动操作 · 当前任务保持暂停':'任务执行中'}</span><small>{frame?'页面检查于 '+new Date(frame.captured_at).toLocaleTimeString():'正在获取画面…'}</small></div><div className="account-task-tabs" role="tablist" aria-label="任务执行页面">{taskPages.map(page=><div className="account-task-tab" key={page.label}><button role="tab" aria-selected={viewLabel?viewLabel===page.label:frame?.target===page.id} title={page.title} onClick={()=>chooseTaskPage(page)}>{page.label}</button><button className="account-task-tab-close" aria-label={'关闭'+page.label} title={'完全关闭'+page.label+'，并暂停该账号任务'} disabled={Boolean(closingPage)||Boolean(manualPending)||manualActive||!frame?.task_key||!page.id} onClick={()=>void closeTaskPage(page.id)}>{closingPage===page.id?'…':<X size={14}/>}</button></div>)}</div>{manualActive&&<p className="account-task-manual-note" role="status">{manualEditable?'当前页面可操作':'当前标签只读'}</p>}{(pageMessage||error)&&<p role="status" className="account-task-watch-error">{pageMessage||error}</p>}<div className="account-task-live" ref={liveTarget}><button className="account-task-watch-image" aria-label="任务画面，点击确认是否继续干扰" disabled={Boolean(manualPending)||manualActive||!frame?.task_key||!frame.target||frame.operation!=='collection'} onClick={()=>void beginManualControl()}><span className="account-task-surface-state" role="status" aria-live="polite"><Monitor size={32}/><strong>{taskStatus.title}</strong>{!mounted&&<span>{currentDisplay?.display_message||taskStatus.message}</span>}</span></button></div></div>:!mounted&&<div className="account-browser-placeholder">{locked?<LockKeyhole size={35}/>:<Monitor size={38}/>}<h3>{locked?'窗口正由任务使用':!id?'选择或添加窗口':!opened?'打开窗口并登录':!visible?'窗口已转入后台':error?'暂时无法显示窗口':'正在显示网页'}</h3>{error&&<p role="alert">{error}</p>}{id&&!locked&&(!opened||!visible||!native)&&<button className="formal-button primary" disabled={disabled} onClick={onOpen}><LogIn size={16}/>{opened?'显示窗口':'打开 / 登录'}</button>}</div>}
  </div>;
}
