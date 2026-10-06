import {useEffect,useRef,useState} from 'react';
import {LogIn,RefreshCw,Users,X,Trash2} from 'lucide-react';
import type {ReviewAccountState} from './core-client';

export function ReviewAccountSettings(){
 const api=window.collectorCore?.reviewAccount;
 const [value,setValue]=useState<ReviewAccountState|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[resetting,setResetting]=useState(false);
 const live=useRef(true),pending=useRef(false),revision=useRef(0);
 useEffect(()=>{
  live.current=true;
  let reading=false;
  const refresh=()=>{if(!api||pending.current||reading)return;reading=true;const ticket=++revision.current;void api({action:'status'}).then(v=>{if(live.current&&ticket===revision.current)setValue(v)}).catch(()=>{if(live.current&&ticket===revision.current)setError('审核账号状态读取失败，可重新点击下方入口。')}).finally(()=>{reading=false})};
  refresh();window.addEventListener('focus',refresh);
  const timer=value?.resetting?window.setInterval(refresh,1000):undefined;
  return()=>{live.current=false;revision.current++;window.removeEventListener('focus',refresh);if(timer!==undefined)window.clearInterval(timer)};
 },[api,Boolean(value?.resetting)]);
 async function command(action:'login'|'home'|'target'|'hide'|'reset'){
  if(!api||pending.current||value?.resetting)return;pending.current=true;revision.current++;setBusy(true);setResetting(action==='reset');setError('');
  try{const result=await api({action});if(live.current)setValue(result)}catch(e){
   if(live.current){
    setError(e instanceof Error?e.message:String(e));
    const ticket=++revision.current;
    try{const status=await api({action:'status'});if(live.current&&ticket===revision.current)setValue(status)}catch{/* Preserve the original command error. */}
   }
  }
  finally{pending.current=false;if(live.current){setBusy(false);setResetting(false)}}
 }
 const unavailable=!api||busy||Boolean(value?.resetting);
 const navigationDisabled=unavailable||Boolean(value?.resetRequired);
 return <section className="formal-panel" style={{marginBottom:20}} aria-label="审核账号设置"><div className="formal-panel-body">
  <h3>审核账号 · Instagram</h3>
  <div className="formal-toolbar" style={{margin:'16px 0',flexWrap:'wrap'}}>
   <button className="formal-button primary" disabled={navigationDisabled} onClick={()=>void command('login')}><LogIn size={16}/>登录 / 检查</button>
   <button className="formal-button" disabled={navigationDisabled} onClick={()=>void command('home')}><Users size={16}/>账号主页 / 切换</button>
   <button className="formal-button" disabled={navigationDisabled||!value?.lastTarget} onClick={()=>void command('target')}><RefreshCw size={16}/>返回目标主页{value?.lastTarget?` · @${value.lastTarget}`:''}</button>
   <button className="formal-button" disabled={unavailable||!value?.opened} onClick={()=>void command('hide')}><X size={16}/>收起审核窗口</button>
  </div>
  <div style={{border:'1px solid var(--ui-border, #34445c)',borderRadius:12,padding:16,marginBottom:16}}>
   <div className="formal-toolbar" style={{justifyContent:'space-between',flexWrap:'wrap',gap:12}}>
    <strong>审核网页异常 · 重新登录</strong>
    <button className="formal-button danger" disabled={unavailable} onClick={()=>void command('reset')}><Trash2 size={16}/>{resetting||value?.resetting?'正在清空审核登录状态…':'一键清空并重新登录'}</button>
   </div>
   {(resetting||value?.resetting)&&<p role="status">请先处理清空确认提示；清理完成后将自动打开登录页。</p>}
  </div>
  {!api&&<p role="status">请使用新版桌面程序管理审核账号。</p>}
  {value?.message&&<p role="status">上次审核页面提示：{value.message}</p>}
  {error&&<p className="formal-error-banner" role="alert">{error}</p>}
 </div></section>;
}
