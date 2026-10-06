import {ChatTranslationControls} from './chat-translation';
import {createPortal} from 'react-dom';
import {UnreadBadge,useAccountUnread} from './account-unread';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Pencil, Globe, Plus, Search, Monitor, Power, Trash2, RefreshCw, X, ArrowLeft, ArrowRight, PanelLeftClose, PanelLeftOpen, SlidersHorizontal, Cloud, History, Instagram, MessageCircle } from 'lucide-react';
import { getCollectorCoreClient, type CoreWorkbenchSnapshot, type CoreBitBrowserWindow } from './core-client';
import './account-workspace.css';
import { AccountCreationChoice, AccountBatchCreator } from './account-batch-creator';
import { CloudWorkspace } from './cloud-workspace';
import { AccountBrowserSurface } from './account-browser-surface';
import { createViewSnapshotReader } from './workbench-render-state';
import { shareUnchangedJson } from './snapshot-sharing';

type Plan = {
  id: string; platform: string; serial: number; name: string; username: string; group: string; profile_id: string;
  created_at?:string;last_opened_at?:string|null;custom_url?:string; notes: string; native: boolean; proxy_server: string; revision: number; updated_at: string;
  environment: {language: string; timezone: string; proxy_note: string};
};
type AccountData = {
  restore?:{restoring:boolean;failed:string[]};windows?:Array<Partial<CoreBitBrowserWindow>&{id:string;opened:boolean}>;inventory_stale?:boolean;platforms?:Array<{id:string;label:string;hint?:string}>;unavailable_platforms?:Array<{id:string;label:string;hint:string}>;plans: Plan[]; events: Array<{name:string;action:string;created_at:string}>;
  last_dm: Record<string,{count:number;checked_at:string|null}>;
  last_following: Record<string,{username:string;count:number;checked_at:string|null}>;
  locks: Record<string,{operation_type:string;state?:string;cleanup_required?:boolean}>; cloud_sync: string;
};
type Form = {custom_url:string;platform:string;native:boolean;proxy_server:string;id:string;revision:number;name:string;username:string;group:string;profile_id:string;notes:string;language:string;timezone:string;proxy_note:string};
const empty:Form={custom_url:'',platform:'instagram',native:true,proxy_server:'',id:'',revision:0,name:'',username:'',group:'',profile_id:'',notes:'',language:'简体中文',timezone:'跟随窗口',proxy_note:''};
const fallbackPlatforms:Record<string,string>={instagram:'照片墙（Instagram）',whatsapp:'即时通讯（WhatsApp）'};

const isWhatsApp=(p:Pick<Plan,'platform'|'custom_url'>)=>{if(p.platform==='whatsapp')return true;try{return p.platform==='custom'&&new URL(p.custom_url||'').origin==='https://web.whatsapp.com'}catch{return false}};

const time=(value:string|null|undefined)=>value?new Date(value).toLocaleString():'尚未检查';

export function AccountWorkspace({snapshot:sourceSnapshot,onChanged}:{snapshot:CoreWorkbenchSnapshot|null;onChanged?:()=>Promise<unknown>}) {
  const [data,setData]=useState<AccountData|null>(null);
  const windows=useMemo(()=>{
    const inventory=new Map<string,NonNullable<AccountData['windows']>[number]>();
    for(const item of data?.windows||[])if(!inventory.has(item.id))inventory.set(item.id,item);
    // Account reads supply enough window metadata to work before the global
    // history snapshot. Neither feed may clear a lock reported by the other.
    const result=(sourceSnapshot?.windows||[]).map(item=>{const observed=inventory.get(item.id);return observed?{...item,opened:observed.opened,window_state:observed.window_state??item.window_state}:item});
    const known=new Set(result.map(item=>item.id));
    for(const item of inventory.values())if(!known.has(item.id)&&typeof item.name==='string'){
      result.push({...item,name:item.name});known.add(item.id);
    }
    return result;
  },[sourceSnapshot?.windows,data?.windows]);
  const windowsById=useMemo(()=>{
    const index=new Map<string,CoreWorkbenchSnapshot['windows'][number]>();
    for(const item of windows)if(!index.has(item.id))index.set(item.id,item);
    return index;
  },[windows]);
  const snapshot={windows};
  const unread=useAccountUnread();
  const platforms:Record<string,string>=data?.platforms?Object.fromEntries(data.platforms.map(p=>[p.id,p.label])):fallbackPlatforms;
  const platformLabel=(key:string)=>platforms[key]||'未识别平台';
  const [cookieText,setCookieText]=useState(''),[cookieRetry,setCookieRetry]=useState(false);
  const [dragged,setDragged]=useState(''),[dragOver,setDragOver]=useState('');
  const fileReadVersion=useRef(0);
  function closeForm(){fileReadVersion.current++;setForm(null);setCookieText('');setCookieRetry(false)}
  function edit(p?:Plan){fileReadVersion.current++;setError('');setCookieText('');setCookieRetry(false);setForm(p?Object.fromEntries(Object.keys(empty).map(k=>[k,(p as any)[k]??(p.environment as any)[k]??(empty as any)[k]])) as Form:{...empty});setBindingQuery('')}

  const [query,setQuery]=useState(''),[group,setGroup]=useState(''),[state,setState]=useState('all');
  const [sidebarCollapsed,setSidebarCollapsed]=useState(()=>{try{return localStorage.getItem('account-sidebar-collapsed')==='true'}catch{return false}});
  const searchInput=useRef<HTMLInputElement>(null);
  useEffect(()=>{try{localStorage.setItem('account-sidebar-collapsed',String(sidebarCollapsed))}catch{}},[sidebarCollapsed]);
  function expandSearch(){setSidebarCollapsed(false);requestAnimationFrame(()=>searchInput.current?.focus())}
  const [form,setForm]=useState<Form|null>(null),[error,setError]=useState(''),[note,setNote]=useState('');
  const [readError,setReadError]=useState('');
  const [busy,setBusy]=useState(false),[bindingQuery,setBindingQuery]=useState('');
  const [selecting,setSelecting]=useState(false),[checked,setChecked]=useState<Set<string>>(new Set());
  const [batchNotes,setBatchNotes]=useState<string|null>(null),[batchResults,setBatchResults]=useState<Array<{name:string;message:string}>>([]);
  const [headerTarget,setHeaderTarget]=useState<HTMLElement|null>(null);
  useLayoutEffect(()=>{setHeaderTarget(document.getElementById('account-header-actions'))},[]);
  const [translationTarget,setTranslationTarget]=useState<HTMLElement|null>(null);
  const [translationOpen,setTranslationOpen]=useState(false);
  useLayoutEffect(()=>{setTranslationTarget(document.getElementById('account-translation-actions'))},[]);
  const [loginReport,setLoginReport]=useState<any>(null);
  const [resetTarget,setResetTarget]=useState<Plan|null>(null);
  const [deleteTarget,setDeleteTarget]=useState<Plan|null>(null);
  const [menu,setMenu]=useState<{id:string;x:number;y:number}|null>(null);
  const menuRef=useRef<HTMLDivElement>(null);
  const [platformFilter,setPlatformFilter]=useState('');
  const [creation,setCreation]=useState<'choice'|'batch'|null>(null);
  const [selectedId,setSelectedId]=useState(()=>{try{return localStorage.getItem('account-selected-plan')||''}catch{return ''}}),[filters,setFilters]=useState(false);
  useEffect(()=>{if(selectedId)try{localStorage.setItem('account-selected-plan',selectedId)}catch{}},[selectedId]);
  const [utility,setUtility]=useState<'cloud'|'history'|null>(null);
  const live=useRef(true),pending=useRef(false);
  const windowOperations=useRef(new Map<string,string>());
  const [pendingWindows,setPendingWindows]=useState<Record<string,string>>({});
  const snapshotReader=useRef<ReturnType<typeof createViewSnapshotReader<AccountData>>|null>(null);
  const refresh=useCallback((fresh=false)=>snapshotReader.current?.refresh(fresh)||Promise.resolve(),[]);
  useEffect(()=>{
    live.current=true;
    const reader=createViewSnapshotReader({read:()=>getCollectorCoreClient().accountSnapshot<AccountData>(),onValue:value=>{setData(previous=>shareUnchangedJson(previous,value));setReadError('')},onError:e=>setReadError(String(e))});
    snapshotReader.current=reader;void reader.refresh();
    const id=setInterval(()=>void reader.refresh(),3000);
    return()=>{live.current=false;reader.dispose();if(snapshotReader.current===reader)snapshotReader.current=null;clearInterval(id)};
  },[refresh]);
  async function command(body:Record<string,unknown>,message:string) {
    const id=String(body.id||''),action=String(body.action||'');
    if(id&&windowOperations.current.has(id))return false;
    if(id&&['open','close'].includes(action)){
      if(pending.current)return false;
      windowOperations.current.set(id,action);setPendingWindows(Object.fromEntries(windowOperations.current));
      const name=data?.plans.find(p=>p.id===id)?.name||'窗口';
      setError('');setNote(`${name}：正在${action==='open'?'打开':'关闭'}…`);
      try{
        const result=await getCollectorCoreClient().accountCommand(body);
        if(!live.current)return false;
        if(result.page_loaded===false){setNote('');setError(`${name}：${String(result.message||'网页加载未完成')}`);return false;}
        setNote(`${name}：${String(result.message||message)}`);return true;
      }catch(e){if(live.current){setNote('');setError(`${name}：${String(e)}`)}return false}
      finally{
        windowOperations.current.delete(id);
        if(live.current){
          setPendingWindows(Object.fromEntries(windowOperations.current));
          // Release the UI immediately; independent refreshes must not extend
          // the operation lock or block selection of unrelated accounts.
          void Promise.allSettled([refresh(true),unread.refresh(),onChanged?.()]);
        }
      }
    }
    if(pending.current)return false;pending.current=true;setBusy(true);setError('');setNote('');
    try {const result=await getCollectorCoreClient().accountCommand(body);if((['save','save_with_cookies','batch_create'].includes(String(body.action)))&&typeof result.id==='string'){setSelectedId(result.id);if(body.action==='save_with_cookies')setForm(f=>f?{...f,id:result.id as string,revision:Number(result.revision)}:f)}if(result.cookie_failed){setCookieRetry(true);setError(String(result.message||'窗口已保存，登录导入未完成，可重试导入。'));return false}if(result.page_loaded===false){setError(String(result.message||'网页未能加载，请检查网络后刷新或重新打开窗口。'));return false}setNote(String(result.message||message));return true}
    catch(e){setError(String(e));return false}
    finally {pending.current=false;if(live.current){setBusy(false);void Promise.allSettled([refresh(true),unread.refresh(),onChanged?.()])}}
  }
  // Both feeds poll independently. A slower account response must never clear
  // a task lock already reported by the workbench (or the other way around).
  function isLocked(profile:string) {return Boolean(data?.locks[profile]||windowsById.get(profile)?.locked)}
  const pendingProfiles=useMemo(()=>new Set((data?.plans||[])
    .filter(p=>Boolean(pendingWindows[p.id])).map(p=>p.profile_id)),[data?.plans,pendingWindows]);
  function controlLocked(profile:string) {return isLocked(profile)||pendingProfiles.has(profile)}
  function set<K extends keyof Form>(key:K,value:Form[K]) {setForm(f=>f?{...f,[key]:value}:f)}
  async function save(e:FormEvent) {
    e.preventDefault();if(!form)return;
    const body=cookieRetry?{action:'import_cookies',id:form.id,revision:form.revision,cookie_text:cookieText}:cookieText.trim()?{action:'save_with_cookies',plan:form,cookie_text:cookieText}:{action:'save',plan:form};
    if(await command(body,cookieText.trim()?'Cookie 已导入，请在网页确认登录状态':'窗口已保存'))closeForm();
  }
  async function checkLogin(p:Plan){
    if(pending.current)return;pending.current=true;setBusy(true);setError('');setLoginReport(null);
    try{const result=await getCollectorCoreClient().accountCommand({action:'whatsapp_diagnostics',id:p.id});setLoginReport(result);exportLoginReport(result);setNote('检查结果已导出，包含本次启动状态和完整错误描述。')}
    catch(e){setError(String(e))}finally{pending.current=false;setBusy(false)}
  }
  function exportLoginReport(value=loginReport){if(!value)return;const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='WhatsApp-Login-Diagnostic-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
  async function cookieFile(file?:File){
    if(!file)return;const version=++fileReadVersion.current;
    if(file.size>512*1024){setError('Cookie 文件不能超过 512 KB');return}
    try{const text=await file.text();if(version===fileReadVersion.current){setCookieText(text);setError('')}}catch{if(version===fileReadVersion.current)setError('文件无法读取')}
  }
  async function moveWindow(source:string,target:string){
    setDragged('');setDragOver('');if(busy||source===target)return;
    const ids=(data?.plans||[]).map(p=>p.id);if(!ids.includes(source)||!ids.includes(target))return;
    const destination=ids.indexOf(target);ids.splice(ids.indexOf(source),1);ids.splice(destination,0,source);
    await command({action:'reorder',ids},'窗口顺序已保存');
  }
  const plans=data?.plans||[];
  // Native menus and batch commands outlive the render that opened them.
  // Read current plans and both task-lock feeds immediately before dispatch.
  const interaction=useRef({plans,isLocked});
  interaction.current={plans,isLocked};
  useEffect(()=>{const select=()=>{const profile=sessionStorage.getItem('account-focus-profile'),plan=plans.find(p=>p.profile_id===profile);if(plan){setSelectedId(plan.id);sessionStorage.removeItem('account-focus-profile')}};select();window.addEventListener('account:focus',select);return()=>window.removeEventListener('account:focus',select)},[data]);
  const checkedPlans=plans.filter(p=>checked.has(p.id));
  const batchPending=useRef(false);
  const [batchRunning,setBatchRunning]=useState(false);
  async function runBatch(action:'open'|'close'|'refresh'|'notes'){
    if(pending.current||batchPending.current||!checkedPlans.length)return;
    const targets=[...checkedPlans],notes=batchNotes;
    batchPending.current=true;setBatchRunning(true);setError('');setNote('');setBatchResults([]);setBatchNotes(null);
    const results:Array<{name:string;message:string}>=[];
    try {
      for(const p of targets){
        if(!live.current)break;
        const current=interaction.current.plans.find(item=>item.id===p.id);
        if(!current){results.push({name:p.name,message:'已跳过：窗口已移除'});continue}
        if(windowOperations.current.has(p.id)){results.push({name:p.name,message:'已跳过：该窗口正在操作'});continue}
        if(interaction.current.isLocked(current.profile_id)){results.push({name:p.name,message:'已跳过：任务占用'});continue}
        windowOperations.current.set(p.id,action);setPendingWindows(Object.fromEntries(windowOperations.current));
        try {
          const result=await getCollectorCoreClient().accountCommand(action==='notes'?{action:'notes',id:p.id,revision:p.revision,notes}:{action,id:p.id});
          results.push({name:p.name,message:result.page_loaded===false?String(result.message||'网页加载失败'):action==='notes'?'备注已保存':action==='open'?'已打开（登录状态以网页为准）':action==='close'?'已关闭':'已刷新'});
        }catch(e){results.push({name:p.name,message:String(e)})}
        finally{windowOperations.current.delete(p.id);if(live.current)setPendingWindows(Object.fromEntries(windowOperations.current))}
        if(live.current)setBatchResults([...results]);
      }
      if(live.current){setBatchResults(results);setNote(`已处理 ${results.length} 个窗口，详见批量结果`)}
    }finally{batchPending.current=false;if(live.current){setBatchRunning(false);void Promise.allSettled([refresh(true),unread.refresh(),onChanged?.()])}}
  }
  const batchToolbar=<div className="account-batch-toolbar" aria-label="批量窗口操作"><button className="formal-button compact" disabled={busy} aria-pressed={selecting} onClick={()=>{if(!selecting)setSidebarCollapsed(false);setSelecting(!selecting)}}>选择窗口{checkedPlans.length?` · ${checkedPlans.length}`:''}</button><button className="formal-button compact" disabled={busy||batchRunning||!checkedPlans.length} onClick={()=>void runBatch('open')}>开启</button><button className="formal-button compact" disabled={busy||batchRunning||!checkedPlans.length} onClick={()=>void runBatch('close')}>关闭</button><button className="formal-button compact" disabled={busy||batchRunning||!checkedPlans.length} onClick={()=>void runBatch('refresh')}>刷新</button><button className="formal-button compact" disabled={busy||batchRunning||!checkedPlans.length} onClick={()=>setBatchNotes('')}>备注</button></div>;

  const rows=plans.filter(p=>{
    const w=windowsById.get(p.profile_id);
    const matches=[p.name,p.username,p.group,p.serial,platformLabel(p.platform),w?.name,w?.serial_number].join(' ').toLowerCase().includes(query.toLowerCase());
    return matches&&(!platformFilter||p.platform===platformFilter)&&(!group||p.group===group)&&(state==='all'||(state==='locked'&&isLocked(p.profile_id))||(state==='unbound'&&!p.profile_id)||(state==='ready'&&p.profile_id&&!isLocked(p.profile_id)));
  });
  const bindings=snapshot.windows.filter(w=>[w.name,w.serial_number,w.group].join(' ').toLowerCase().includes(bindingQuery.toLowerCase()));
  const selected=plans.find(p=>p.id===selectedId)||plans[0];
  const translationId=selected?.native&&(selected.platform==='instagram'||isWhatsApp(selected))?selected.id:undefined;
  useEffect(()=>{setTranslationOpen(false)},[translationId]);
  const selectedWindow=selected?windowsById.get(selected.profile_id):undefined;
  const locked=Boolean(selected&&isLocked(selected.profile_id));
  const disabled=busy||locked||!selected||Boolean(selected&&pendingWindows[selected.id]);
  const status=(p:Plan)=>pendingWindows[p.id]?({open:'正在打开',close:'正在关闭',refresh:'正在刷新',notes:'正在保存'}[pendingWindows[p.id]]||'正在处理'):data?.locks[p.profile_id]?.cleanup_required||windowsById.get(p.profile_id)?.lock_state==='cleanup_pending'?'养号清理待核验':controlLocked(p.profile_id)?'任务占用':!p.profile_id?'待绑定':windowsById.get(p.profile_id)?.window_state==='unknown'?'状态更新中':windowsById.get(p.profile_id)?.opened?'已打开':'已关闭';
  const opened=(p:Plan)=>Boolean(windowsById.get(p.profile_id)?.opened);
  // Native icon tooltip stays above embedded browser views without taking focus.
  function buildWindowHint(p:Plan) {
    const observation=unread.snapshot.windows[p.profile_id];
    const unreadText=observation?.count==null?'尚未识别':`${observation.count}${observation.capped?'+':''}${observation.status!=='live'?'（最近一次记录）':''}`;
    return [
      `窗口名称：${p.name}`,
      p.notes?.trim()?`备注：\n${p.notes.trim()}`:'备注：暂无备注',
      '',
      `平台：${platformLabel(p.platform)}`,
      `平台账号：${p.username||data?.last_following[p.profile_id]?.username||'尚未记录'}`,
      `分类：${p.group||'未分类'}`,
      `窗口状态：${pendingWindows[p.id]?status(p):opened(p)?'已打开':'已关闭'}${controlLocked(p.profile_id)||!p.profile_id?` · ${status(p)}`:''}`,
      `未读消息：${unreadText}`,
      ...(observation?.observed_at?[`未读检查时间：${time(observation.observed_at)}`]:[]),
      p.created_at?`创建时间：${time(p.created_at)}`:'创建时间暂无记录',
      p.last_opened_at?`最后打开时间：${time(p.last_opened_at)}`:'最后打开时间暂无记录',
      '',
      '拖动排序 · 右键操作 · Alt + ↑/↓ 调整顺序',
    ].join('\n');
  }
  // Selection and search do not change the saved account details. Build once
  // per inventory/status update and reuse for both accessibility and tooltip.
  const windowHints=useMemo(()=>new Map(plans.map(p=>[p.id,buildWindowHint(p)])),
    [data,windowsById,unread.snapshot.windows,pendingWindows]);
  function windowHint(p:Plan) {return windowHints.get(p.id)??buildWindowHint(p)}
  async function open(p:Plan) {await command({action:'open',id:p.id},'已打开窗口')}
  const menuPlan=plans.find(p=>p.id===menu?.id);
  const menuWindow=menuPlan?windowsById.get(menuPlan.profile_id):undefined;
  const menuDisabled=busy||!menuPlan||Boolean(menuPlan&&controlLocked(menuPlan.profile_id));
  useLayoutEffect(()=>{if(menu)menuRef.current?.focus()},[menu]);
  useEffect(()=>{if(menu&&!menuPlan)setMenu(null)},[menu,menuPlan]);
  useEffect(()=>{
    if(!menu)return;
    const outside=(event:PointerEvent)=>{if(!menuRef.current?.contains(event.target as Node))setMenu(null)};
    const close=()=>setMenu(null);
    document.addEventListener('pointerdown',outside);window.addEventListener('resize',close);window.addEventListener('scroll',close,true);
    return()=>{document.removeEventListener('pointerdown',outside);window.removeEventListener('resize',close);window.removeEventListener('scroll',close,true)};
  },[menu]);
  function contextMenu(p:Plan,x:number,y:number) {
    if(!busy){setSelectedId(p.id);setError('');setNote('')}
    const popup=window.collectorCore?.accountContextMenu;
    if(popup){void popup({name:p.name,opened:Boolean(windowsById.get(p.profile_id)?.opened),disabled:busy||controlLocked(p.profile_id),configDisabled:isLocked(p.profile_id),documentUrl:window.location.href}).then(action=>{if(action&&live.current)void menuAction(action,p)}).catch(()=>{if(live.current)setError('窗口菜单未能打开，请重试')});return}
    setMenu({id:p.id,x:Math.max(8,Math.min(x,window.innerWidth-210)),y:Math.max(8,Math.min(y,window.innerHeight-300))});
  }
  async function menuAction(action:string,p=menuPlan) {
    const current=interaction.current.plans.find(item=>item.id===p?.id);
    if(!current||pending.current||interaction.current.isLocked(current.profile_id))return;
    p=current;
    setMenu(null);
    if(action==='edit'){edit(p);return}
    if(action==='delete'){setDeleteTarget(p);return}
    if(action==='open'){await open(p);return}
    await command({action,id:p.id},{refresh:'已刷新网页',back:'已返回上一页',forward:'已进入下一页',close:'已关闭窗口'}[action]||'操作完成');
  }
  const platformIcon=(p:string)=>p==='instagram'?<Instagram size={21}/>:p==='whatsapp'?<MessageCircle size={21}/>:<Globe size={21}/>;
  return <><div className={`account-workspace account-docked-workspace${sidebarCollapsed?' account-sidebar-collapsed':''}`}>
    {translationTarget&&createPortal(<ChatTranslationControls key={translationId||'none'} id={translationId} name={selected?.name} locked={locked} busy={busy||Boolean(form||creation||menu||resetTarget||deleteTarget||utility)} open={translationOpen} onOpenChange={setTranslationOpen}/>,translationTarget)}
    {headerTarget?createPortal(batchToolbar,headerTarget):<div className="account-batch-fallback">{batchToolbar}</div>}
    <aside className="account-sidebar" aria-label="账号窗口">
      {data?.restore?.restoring&&<small role="status" className="account-footnote">正在恢复上次打开的窗口…</small>}
      {data?.inventory_stale&&<small role="status" className="account-footnote">窗口状态正在恢复，账号资料仍可查看。</small>}
      {!!data?.restore?.failed.length&&<small className="account-footnote">部分窗口恢复失败，可右键重新打开。</small>}
      <header><strong>账号窗口 <small>{plans.length}</small></strong><div className="account-sidebar-header-buttons"><button className="formal-button account-icon-button" aria-label={sidebarCollapsed?'展开账号侧栏':'收起账号侧栏'} title={sidebarCollapsed?'展开账号侧栏':'收起账号侧栏'} aria-expanded={!sidebarCollapsed} onClick={()=>{setSidebarCollapsed(!sidebarCollapsed);if(!sidebarCollapsed){setSelecting(false);setChecked(new Set())}}}>{sidebarCollapsed?<PanelLeftOpen size={18}/>:<PanelLeftClose size={18}/>}</button><button className="formal-button account-icon-button" aria-label="添加窗口" title="添加窗口" disabled={busy} onClick={()=>{setError('');setCreation('choice')}}><Plus size={19}/></button></div></header>
      {sidebarCollapsed?<button className="formal-button account-icon-button account-collapsed-search" aria-label="展开账号搜索" title={query||group||platformFilter||state!=='all'?'展开账号搜索（当前已筛选）':'展开账号搜索'} onClick={expandSearch}><Search size={17}/></button>:<div className="account-sidebar-search"><Search size={15}/><input ref={searchInput} aria-label="搜索账号窗口" placeholder="搜索窗口或账号" value={query} onChange={e=>setQuery(e.target.value)}/><button aria-label="筛选窗口" aria-expanded={filters} onClick={()=>setFilters(!filters)}><SlidersHorizontal size={15}/></button></div>}
      {filters&&!sidebarCollapsed&&<div className="account-sidebar-filters">
        <select aria-label="筛选平台" className="formal-input" value={platformFilter} onChange={e=>setPlatformFilter(e.target.value)}><option value="">全部平台</option>{Object.entries(platforms).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select>
        <select aria-label="筛选分类" className="formal-input" value={group} onChange={e=>setGroup(e.target.value)}><option value="">全部分类</option>{[...new Set(plans.map(p=>p.group).filter(Boolean))].map(g=><option key={g}>{g}</option>)}</select>
        <select aria-label="筛选状态" className="formal-input" value={state} onChange={e=>setState(e.target.value)}><option value="all">全部状态</option><option value="ready">可操作</option><option value="locked">任务占用</option><option value="unbound">待绑定</option></select>
      </div>}
      {selecting&&<div className="account-selection-tools"><button disabled={busy} onClick={()=>setChecked(new Set(rows.map(p=>p.id)))}>选中当前筛选</button><button disabled={busy} onClick={()=>setChecked(new Set())}>清空</button></div>}
      <nav className="account-sidebar-list" aria-label="切换窗口">{rows.map(p=><div key={p.id} className="account-window-select-row">{selecting&&<input type="checkbox" aria-label={`选择窗口 ${p.name}`} disabled={busy} checked={checked.has(p.id)} onChange={e=>setChecked(old=>{const next=new Set(old);e.target.checked?next.add(p.id):next.delete(p.id);return next})}/>}<button key={p.id} className={`account-sidebar-item ${selected?.id===p.id?'active':''} ${dragOver===p.id?'drag-over':''}`} draggable={!busy} onDragStart={e=>{setDragged(p.id);e.dataTransfer.setData('text/plain',p.id);e.dataTransfer.effectAllowed='move';setMenu(null)}} onDragOver={e=>{if(dragged){e.preventDefault();setDragOver(p.id)}}} onDrop={e=>{e.preventDefault();if(dragged===e.dataTransfer.getData('text/plain'))void moveWindow(dragged,p.id)}} onDragEnd={()=>{setDragged('');setDragOver('')}} aria-pressed={selected?.id===p.id} aria-label={`${p.name}，${pendingWindows[p.id]?status(p):opened(p)?'已打开':'已关闭'}`} aria-description={windowHint(p)} aria-haspopup="menu" aria-disabled={busy} data-plan-id={p.id} onClick={()=>{if(busy)return;setSelectedId(p.id);setMenu(null);setError('');setNote('')}} onContextMenu={e=>{e.preventDefault();contextMenu(p,e.clientX,e.clientY)}} onKeyDown={e=>{if(e.altKey&&['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();const ids=rows.map(x=>x.id),index=ids.indexOf(p.id),target=ids[index+(e.key==='ArrowUp'?-1:1)];if(target)void moveWindow(p.id,target);return}if(e.key==='ContextMenu'||e.shiftKey&&e.key==='F10'){e.preventDefault();const r=e.currentTarget.getBoundingClientRect();contextMenu(p,r.right-10,r.top+15)}}}>
        <span className={`account-platform-icon ${p.platform}`} title={windowHint(p)} role="img" aria-label={`${platformLabel(p.platform)}，悬停查看窗口详情`}>{platformIcon(p.platform)}<UnreadBadge profile={p.profile_id} compact inheritTitle/></span><span className="account-sidebar-identity"><strong>{p.name}</strong><span className="account-sidebar-status"><span className={opened(p)?'online':'offline'}/> {pendingWindows[p.id]?status(p):opened(p)?'已打开':'已关闭'}</span></span>
      </button></div>)}</nav>
      {!rows.length&&<p className="account-sidebar-empty">{plans.length?'没有匹配的窗口':'暂无窗口'}</p>}
      <footer><small>{plans.filter(p=>windowsById.get(p.profile_id)?.opened).length} 个打开 · {plans.filter(p=>isLocked(p.profile_id)).length} 个占用</small><div><button title="刷新窗口列表" aria-label="刷新窗口列表" disabled={busy} onClick={()=>void refresh()}><RefreshCw size={16}/></button><button title="云端同步" aria-label="云端同步" onClick={()=>setUtility('cloud')}><Cloud size={16}/></button><button title="窗口操作记录" aria-label="窗口操作记录" onClick={()=>setUtility('history')}><History size={16}/></button></div></footer>
    </aside>
    <section className="account-browser-workspace" aria-label="当前窗口">
      {readError&&<div className="formal-error-banner" role="status">账号列表暂未更新，正在自动重试：{readError}<button className="formal-button compact" onClick={()=>void refresh()}>重试列表</button></div>}
      {error&&<div className="formal-error-banner" role="alert">{error}<button aria-label="关闭错误提示" onClick={()=>setError('')}><X size={15}/></button></div>}
      {plans.some(p=>data?.locks[p.profile_id]?.cleanup_required||windowsById.get(p.profile_id)?.lock_state==='cleanup_pending')&&<div className="formal-error-banner" role="status">有窗口的养号清理待核验，请前往“养号 → 异常任务”，点击“核验窗口清理”。历史记录和登录资料会保留。</div>}
      {selected&&unread.snapshot.windows[selected.profile_id]?.status==='storage_error'&&<div className="formal-error-banner" role="alert"><span>WhatsApp 登录初始化失败。请检查并导出本次启动错误；重新建立环境可保留旧资料。</span><button className="formal-button compact" disabled={disabled} onClick={()=>{setError('');setResetTarget(selected)}}>重新建立登录环境</button></div>}
      {selected&&isWhatsApp(selected)&&<div className="account-batch-toolbar"><button className="formal-button compact" disabled={disabled||!selectedWindow?.opened} onClick={()=>void checkLogin(selected)}>检查登录环境</button>{loginReport&&<button className="formal-button compact" onClick={()=>exportLoginReport()}>导出检查结果</button>}</div>}
      {note&&<div className="account-note" role="status">{note}<button aria-label="关闭提示" onClick={()=>setNote('')}><X size={15}/></button></div>}
      {batchResults.length>0&&<details className="account-batch-results"><summary>批量结果 · {batchResults.length} 个窗口</summary>{batchResults.map((r,i)=><p key={i}><strong>{r.name}</strong> · {r.message}</p>)}</details>}
            <AccountBrowserSurface taskWatch id={selected?.id||''} native={Boolean(selected?.native)} opened={Boolean(selectedWindow?.opened)} locked={locked} visible={!translationOpen&&batchNotes===null&&!creation&&!form&&!utility&&!resetTarget&&!deleteTarget&&!menu} onOpen={()=>selected&&void open(selected)} disabled={disabled||!selectedWindow}/>
    </section>
    {menu&&menuPlan&&<div ref={menuRef} className="account-context-menu" role="menu" aria-label={`${menuPlan.name}窗口操作`} tabIndex={-1} style={{left:menu.x,top:menu.y}} onKeyDown={e=>{
      if(e.key==='Escape'){setMenu(null);document.querySelector<HTMLButtonElement>(`[data-plan-id="${menuPlan.id}"]`)?.focus();return}
      if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;
      e.preventDefault();const items=Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')||[]);if(!items.length)return;
      const index=items.indexOf(document.activeElement as HTMLButtonElement);items[e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowUp'?-1:1)+items.length)%items.length]?.focus();
    }}>
      <strong>{menuPlan.name}</strong>
      {menuPlan&&isLocked(menuPlan.profile_id)&&<small>任务占用，结束后可操作</small>}
      <button role="menuitem" disabled={menuDisabled||!menuWindow?.opened} onClick={()=>void menuAction('refresh')}><RefreshCw size={16}/>刷新</button>
      <button role="menuitem" disabled={menuDisabled||!menuWindow?.opened} onClick={()=>void menuAction('back')}><ArrowLeft size={16}/>上一页</button>
      <button role="menuitem" disabled={menuDisabled||!menuWindow?.opened} onClick={()=>void menuAction('forward')}><ArrowRight size={16}/>下一页</button>
      <button role="menuitem" disabled={menuDisabled||!menuWindow} onClick={()=>void menuAction(menuWindow?.opened?'close':'open')}><Power size={16}/>{menuWindow?.opened?'关闭':'打开'}</button>
      <button role="menuitem" disabled={menuDisabled||Boolean(menuPlan&&isLocked(menuPlan.profile_id))} onClick={()=>void menuAction('edit')}><Pencil size={16}/>编辑</button>
      <button role="menuitem" className="danger" disabled={menuDisabled||Boolean(menuPlan&&isLocked(menuPlan.profile_id))} onClick={()=>void menuAction('delete')}><Trash2 size={16}/>删除</button>
    </div>}
    {batchNotes!==null&&<div className="account-overlay account-delete-overlay"><section className="account-delete-dialog" role="dialog" aria-modal="true" aria-label="批量备注"><h2>备注 · {checkedPlans.length} 个窗口</h2><textarea className="formal-input" aria-label="批量备注内容" rows={4} maxLength={1000} value={batchNotes} onChange={e=>setBatchNotes(e.target.value)}/><footer><button className="formal-button" onClick={()=>setBatchNotes(null)}>取消</button><button className="formal-button primary" onClick={()=>void runBatch('notes')}>保存备注</button></footer></section></div>}
    {resetTarget&&<div className="account-overlay account-delete-overlay" style={{zIndex:130}}><section className="account-delete-dialog" role="dialog" aria-modal="true" aria-label="重置 WhatsApp 登录"><h2>重新建立“{resetTarget.name}”的登录环境？</h2><p>将关闭此窗口并创建独立的新登录环境。原 WhatsApp 资料会保留，需要在新环境重新扫码或关联电话号码。</p><footer><button className="formal-button" disabled={busy} onClick={()=>setResetTarget(null)}>取消</button><button className="formal-button danger" disabled={busy||isLocked(resetTarget.profile_id)} onClick={async()=>{const target=resetTarget;if(await command({action:'reset_whatsapp_storage',id:target.id,revision:target.revision,confirm:true},'新登录环境已创建')){setResetTarget(null);closeForm();await command({action:'open',id:target.id},'已打开新环境，请扫码或关联电话号码')}}}>创建新环境</button></footer>{error&&<p role="alert">{error}</p>}</section></div>}
    {creation==='choice'&&<AccountCreationChoice onClose={()=>setCreation(null)} onSingle={()=>{setCreation(null);edit()}} onBatch={()=>setCreation('batch')}/>}
    {creation==='batch'&&<AccountBatchCreator busy={busy} error={error} onClose={()=>setCreation(null)} onCreate={body=>command(body,'批量窗口已创建')}/>}
    {deleteTarget&&<div className="account-overlay account-delete-overlay"><section className="account-delete-dialog" role="dialog" aria-modal="true" aria-label="删除窗口"><h2>删除窗口“{deleteTarget.name}”？</h2><p>将关闭窗口并从列表移除，历史记录和登录资料保留。</p>{error&&<div role="alert" className="formal-error-banner">{error}</div>}<footer><button className="formal-button" disabled={busy} onClick={()=>setDeleteTarget(null)}>取消</button><button className="formal-button danger" disabled={busy||isLocked(deleteTarget.profile_id)} onClick={async()=>{if(await command({action:'delete',id:deleteTarget.id,revision:deleteTarget.revision},'窗口已删除'))setDeleteTarget(null)}}>确认删除</button></footer></section></div>}
    {utility&&<div className="account-overlay"><section className="account-editor account-utility" role="dialog" aria-modal="true" aria-label={utility==='cloud'?'云端同步':'窗口操作记录'}><header><h2>{utility==='cloud'?'云端同步':'窗口操作记录'}</h2><button className="formal-button" aria-label="关闭" onClick={()=>setUtility(null)}><X size={19}/></button></header>{utility==='cloud'?<CloudWorkspace/>:<div className="account-history">{data?.events.map((e,i)=><p key={i}><strong>{e.name}</strong><span>{e.action}</span><small>{time(e.created_at)}</small></p>)}{!data?.events.length&&<p>暂无操作记录</p>}</div>}</section></div>}
    {form&&<div className="account-overlay"><section className="account-editor" role="dialog" aria-modal="true" aria-label={form.id?'编辑窗口':'新建窗口'}><header><div><h2>{form.id?'编辑窗口':'新建窗口'}</h2></div><button className="formal-button" aria-label="关闭编辑" disabled={busy} onClick={closeForm}><X size={19}/></button></header><form onSubmit={e=>void save(e)}>{error&&<div className="formal-error-banner">{error}</div>}<fieldset className="account-config-fields" disabled={cookieRetry}><label className="formal-field"><span>窗口方式</span><select className="formal-input" disabled={Boolean(form.id&&form.profile_id.startsWith('native:'))} value={form.native?'native':'bitbrowser'} onChange={e=>set('native',e.target.value==='native')}><option value="native">软件内置网页（无需比特）</option><option value="bitbrowser">绑定现有 BitBrowser 窗口</option></select></label><label className="formal-field"><span>软件平台</span><select className="formal-input" disabled={cookieRetry||Boolean(form.id&&windowsById.get(form.profile_id)?.opened)} value={form.platform} onChange={e=>{set('platform',e.target.value);set('custom_url','');fileReadVersion.current++;setCookieText('')}}>{Object.entries(platforms).map(([key,label])=><option key={key} value={key}>{label}</option>)}{data?.unavailable_platforms?.map(p=><option key={p.id} disabled>{p.label}（需客户端或扩展）</option>)}</select></label>{form.platform==='custom'&&<label className="formal-field"><span>网页地址</span><input className="formal-input" type="url" required disabled={cookieRetry||Boolean(form.id&&windowsById.get(form.profile_id)?.opened)} placeholder="https://example.com/" value={form.custom_url} onChange={e=>{set('custom_url',e.target.value);setCookieText('')}}/></label>}<div className="account-form-grid"><label className="formal-field"><span>窗口名称</span><input required maxLength={80} className="formal-input" value={form.name} onChange={e=>set('name',e.target.value)}/></label><label className="formal-field"><span>{form.platform==='whatsapp'?'手机号或账号备注':'平台账号'}</span><input className="formal-input" maxLength={form.platform==='instagram'?30:160} placeholder="平台账号（可选）" value={form.username} onChange={e=>set('username',e.target.value)}/></label><label className="formal-field"><span>分类</span><input className="formal-input" maxLength={80} value={form.group} onChange={e=>set('group',e.target.value)}/></label><label className="formal-field"><span>{form.native?'代理地址（可选）':'代理备注'}</span><input className="formal-input" maxLength={240} placeholder={form.native?'http://主机:端口':'代理备注'} disabled={Boolean(form.native&&form.id&&windowsById.get(form.profile_id)?.opened)} value={form.native?form.proxy_server:form.proxy_note} onChange={e=>set(form.native?'proxy_server':'proxy_note',e.target.value)}/></label></div>
      {!form.native&&<div className="account-binding-picker"><h3>绑定现有窗口</h3><input className="formal-input" placeholder="搜索名称、序号或分类" value={bindingQuery} onChange={e=>setBindingQuery(e.target.value)}/><div className="account-binding-list"><label><input type="radio" name="binding" checked={!form.profile_id} onChange={()=>set('profile_id','')}/><span>暂不绑定 · 保存窗口方案</span></label>{bindings.map(w=>{const occupied=plans.some(p=>p.id!==form.id&&p.profile_id===w.id)||isLocked(w.id);return <label className={occupied?'disabled':''} key={w.id}><input type="radio" name="binding" disabled={occupied} checked={form.profile_id===w.id} onChange={()=>set('profile_id',w.id)}/><Monitor size={17}/><span><strong>{w.name}</strong><small>{w.serial_number==null?'':`序号 ${w.serial_number} · `}{w.group||'未分类'}</small></span><small>{isLocked(w.id)?'任务占用':occupied?'已绑定':w.opened?'已打开':'已关闭'}</small></label>})}</div></div>}
      <label className="formal-field"><span>备注</span><textarea className="formal-input" rows={3} maxLength={1000} value={form.notes} onChange={e=>set('notes',e.target.value)}/></label>{form.id&&isWhatsApp(form)&&<button type="button" className="formal-button danger" disabled={busy||isLocked(form.profile_id)} onClick={()=>setResetTarget(plans.find(p=>p.id===form.id)||null)}>重新建立 WhatsApp 登录环境</button>}</fieldset><details className="account-cookie-section" open={cookieRetry||undefined}><summary>Cookie 登录（可选）</summary><textarea aria-label="Cookie 内容" className="formal-input account-cookie-input" rows={4} spellCheck={false} autoComplete="off" value={cookieText} onChange={e=>setCookieText(e.target.value)} placeholder="粘贴 Cookie 内容"/><input aria-label="导入 Cookie 文件" type="file" accept=".json,.txt,application/json,text/plain" onChange={e=>{void cookieFile(e.target.files?.[0]);e.target.value=''}}/>{cookieText&&<button type="button" className="formal-button compact" onClick={()=>setCookieText('')}>清空 Cookie</button>}</details><footer><button type="button" className="formal-button" disabled={busy} onClick={closeForm}>取消</button><button type="submit" className="formal-button primary" disabled={busy||Boolean(form.profile_id&&isLocked(form.profile_id))||(cookieRetry&&!cookieText.trim())}>{busy?'正在处理…':cookieRetry?'重试导入':cookieText.trim()?'保存并导入打开':'保存窗口'}</button></footer></form></section></div>}
  </div></>;
}
