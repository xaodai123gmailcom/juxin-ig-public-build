import {useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {Settings2,X} from 'lucide-react';
type Engine='builtin'|'immersive';
type Availability={available:boolean;message:string;version:string};
type Settings={engine:Engine;immersiveMode:'manual'|'smart';immersiveService:string;immersiveFallbacks:string[];enabled:boolean;outgoing:boolean;incomingLang:string;outgoingLang:string;color:string;fontSize:number;provider:string;region:string};
const immersiveChannels=[['plugin','插件中选择的通道'],['bing','Microsoft / Bing'],['google','Google'],['deepl','DeepL（需配置）'],['babelfast-pro','BabelFast（会员）'],['deepseek-pro','DeepSeek（会员）'],['gemini-pro','Gemini（会员）'],['openai-pro','OpenAI（会员）'],['qwen-pro','通义千问（会员）']];
const languages=[['zh-CN','简体中文'],['zh-TW','繁体中文'],['en','英语'],['de','德语'],['fr','法语'],['es','西班牙语'],['pt','葡萄牙语'],['ja','日语'],['ko','韩语'],['ar','阿拉伯语'],['ru','俄语'],['th','泰语'],['vi','越南语'],['id','印尼语']];

// Register the selected account each session, even when its editor is closed.
export function ChatTranslationControls({id,name,locked,busy:parentBusy,open,onOpenChange}:{id?:string;name?:string;locked:boolean;busy:boolean;open:boolean;onOpenChange:(value:boolean)=>void}){
 const [availability,setAvailability]=useState<Availability|null>(null);
 const [settings,setSettings]=useState<Settings|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[retryAt,setRetryAt]=useState(0);
 const pending=useRef(false),revision=useRef(0),api=window.collectorCore?.chatTranslation;
 const [clock,setClock]=useState(Date.now());
 useEffect(()=>{if(!retryAt)return;const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer)},[retryAt]);
 useEffect(()=>{setSettings(null);setAvailability(null);setError('');setRetryAt(0);if(!id||!api)return;let live=true,running=false;
  const bind=async()=>{if(!live||running||pending.current)return;running=true;const version=revision.current;
   try{const result=await api({id});if(live&&version===revision.current){setSettings(result.settings);setAvailability(result.immersive||null);setError(result.error||'');setRetryAt(result.retryAt||0)}}
   catch(e){if(live&&version===revision.current)setError(String(e))}finally{running=false}
  };
  void bind();const timer=setInterval(()=>void bind(),3000);return()=>{live=false;clearInterval(timer)};
 },[id]);
 useEffect(()=>{if(!open)return;const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'&&!pending.current)onOpenChange(false)};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[open,onOpenChange]);
 const disabled=parentBusy||locked||busy||!id||!api;
 async function toggle(enabled:boolean,engine:Engine){
  if(disabled||!settings||pending.current||!id||!api)return;
  pending.current=true;revision.current++;setBusy(true);setError('');
  try{const result=await api({id,mode:enabled?engine:'off'});setSettings(result.settings);setAvailability(result.immersive||null);setError(result.error||'');setRetryAt(result.retryAt||0);setClock(Date.now())}
  catch(e){setError(String(e))}finally{pending.current=false;setBusy(false)}
 }
 return <>
  <div className="chat-translation-controls immersive-translation-controls" aria-label="当前窗口沉浸式翻译">
   <label className={`chat-translation-toggle ${settings?.enabled&&settings.engine==='immersive'?'is-enabled':''}`}><input type="checkbox" aria-label="沉浸式翻译" checked={Boolean(id&&availability?.available!==false&&settings?.enabled&&settings.engine==='immersive')} disabled={disabled||!settings||availability?.available===false} onChange={e=>void toggle(e.target.checked,'immersive')}/><span>{availability?.available===false?'可选翻译未安装':'沉浸式翻译'}</span></label>
   <button type="button" className="formal-button compact" aria-label="沉浸式翻译设置" aria-expanded={open} disabled={disabled} onClick={()=>onOpenChange(true)}><Settings2 size={15}/><span>设置</span></button>
   {error&&<span role="status" className="chat-translation-indicator" title={error}>{retryAt>clock?`等待 ${Math.ceil((retryAt-clock)/1000)}秒`:retryAt?'正在重试':'查看提示'}</span>}
  </div>
  {open&&id&&createPortal(<div className="account-overlay chat-translation-overlay"><section className="account-editor chat-translation-dialog" role="dialog" aria-modal="true" aria-label="翻译设置窗口"><header><div><h2>沉浸式翻译设置</h2><p>{name}</p></div><button type="button" className="formal-button" aria-label="关闭翻译设置" disabled={busy} onClick={()=>onOpenChange(false)}><X size={19}/></button></header><ChatTranslationSettings key={id} id={id} locked={locked} busy={parentBusy} onBusyChange={value=>{pending.current=value;if(value)revision.current++;setBusy(value)}} onSaved={value=>{setSettings(value);setError('')}}/></section></div>,document.body)}
 </>;
}

export function ChatTranslationSettings({id,locked,busy:parentBusy,onBusyChange,onSaved}:{id:string;locked:boolean;busy:boolean;onBusyChange:(value:boolean)=>void;onSaved?:(value:Settings)=>void}){
 const engine:Engine='immersive';
 const [draft,setDraft]=useState<Settings|null>(null),[availability,setAvailability]=useState<Availability|null>(null);
 const [error,setError]=useState(''),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[readError,setReadError]=useState('');
 const pending=useRef(false),initialized=useRef(false),revision=useRef(0),api=window.collectorCore?.chatTranslation;
 useEffect(()=>{let live=true,running=false;initialized.current=false;
  const load=async()=>{if(!api||running||pending.current)return;running=true;const version=revision.current;
   try{const r=await api({id});if(live&&version===revision.current){
    // The first successful read initializes the editor; later polls preserve edits.
    if(!initialized.current){initialized.current=true;setDraft({...r.settings,fontSize:r.settings.fontSize??12})}
    setAvailability(r.immersive||null);setReadError(r.error||'')
   }}catch(e){if(live&&version===revision.current)setReadError(String(e))}finally{running=false}
  };
  void load();const timer=setInterval(()=>void load(),3000);
  return()=>{live=false;clearInterval(timer)};
 },[id]);
 const disabled=busy||parentBusy||locked;
 const validColor=Boolean(draft&&/^#[\da-f]{6}$/i.test(draft.color));
 const validSize=Boolean(draft&&Number.isInteger(draft.fontSize)&&draft.fontSize>=8&&draft.fontSize<=48);
 const appearanceValid=validColor&&validSize;
 const previewColor=validColor?draft!.color:'#93c5fd',previewSize=validSize?draft!.fontSize:12;
 async function run(action:()=>Promise<void>){if(!api||disabled||pending.current)return;pending.current=true;revision.current++;setBusy(true);onBusyChange(true);setError('');setReadError('');setMessage('');try{await action()}catch(e){setError(String(e))}finally{pending.current=false;setBusy(false);onBusyChange(false)}}
 async function save(){if(!api||!draft||!appearanceValid)return;const result=await api({id,settings:{...draft,enabled:draft.enabled&&availability?.available!==false,engine:'immersive',outgoing:false}});setDraft(result.settings);onSaved?.(result.settings);setMessage('此窗口的翻译设置已保存，返回聊天后生效')}
 async function exportChat(format:string){if(!api)return;const result=await api({id,export:format});if(!result.count)throw new Error('当前没有可导出的已加载消息');const url=URL.createObjectURL(new Blob([result.text],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='chat-'+new Date().toISOString().replace(/[:.]/g,'-')+'.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);setMessage('已导出当前已加载的 '+result.count+' 条消息')}
 return <section className="chat-translation-settings" aria-label="聊天翻译设置">
  <h3>沉浸式翻译（可选）</h3>
  {availability?.available===false&&<p role="status">{availability.message}</p>}
  <p>此版本不附带第三方翻译脚本。请自行从官方渠道取得受支持的 1.33.1 原始文件，再导入；程序会检查文件完整性。</p>
  <button type="button" className="formal-button" disabled={disabled||!api} onClick={()=>void run(async()=>{const result=await api!({id,installImmersive:true});setAvailability(result.immersive||null);setMessage(result.installed?'已导入可选组件，可手动开启翻译':'已取消导入')})}>导入官方脚本文件</button>
  {!api&&<p role="alert">请使用新版桌面程序。</p>}
  {api&&!draft&&!error&&!readError&&<p role="status">正在读取翻译设置…</p>}
  {draft&&<fieldset disabled={disabled}>
   <label className="chat-translation-check"><input type="checkbox" disabled={availability?.available===false} checked={availability?.available!==false&&draft.enabled&&draft.engine===engine} onChange={e=>setDraft({...draft,enabled:e.target.checked,engine})}/>开启沉浸式翻译</label>
   <div className="account-form-grid">
    <label className="formal-field"><span>聊天消息译为</span><select className="formal-input" value={draft.incomingLang} onChange={e=>setDraft({...draft,incomingLang:e.target.value})}>{languages.map(([code,name])=><option key={code} value={code}>{name}</option>)}</select></label>
   </div>
   <div className="immersive-channel-settings">
    <div className="account-form-grid">
     <label className="formal-field"><span>通道选择方式</span><select className="formal-input" value={draft.immersiveMode||'manual'} onChange={e=>setDraft({...draft,immersiveMode:e.target.value as 'manual'|'smart'})}><option value="manual">手动选择</option><option value="smart">智能选择</option></select></label>
     <label className="formal-field"><span>{draft.immersiveMode==='smart'?'首选通道':'使用通道'}</span><select className="formal-input" value={draft.immersiveService||'plugin'} onChange={e=>setDraft({...draft,immersiveService:e.target.value})}>{immersiveChannels.map(([code,name])=><option key={code} value={code}>{name}</option>)}</select></label>
    </div>
    {draft.immersiveMode==='smart'&&<div><span>候选通道（最多 4 个）</span><div className="immersive-channel-options">{immersiveChannels.map(([code,name])=><label className="chat-translation-check" key={code}><input type="checkbox" checked={(draft.immersiveFallbacks||[]).includes(code)} disabled={!(draft.immersiveFallbacks||[]).includes(code)&&(draft.immersiveFallbacks||[]).length>=4} onChange={e=>setDraft({...draft,immersiveFallbacks:e.target.checked?[...(draft.immersiveFallbacks||[]),code]:(draft.immersiveFallbacks||[]).filter(x=>x!==code)})}/>{name}</label>)}</div></div>}
    <div className="chat-translation-actions"><button type="button" className="formal-button" disabled={availability?.available===false} onClick={()=>void run(async()=>{await api!({id,openImmersive:true});setMessage('已打开当前账号的沉浸式翻译插件设置')})}>打开插件设置</button><button type="button" className="formal-button" disabled={availability?.available===false} onClick={()=>void run(async()=>{await api!({id,retry:true});setMessage('已重置重试状态，返回聊天后继续翻译')})}>重新尝试</button></div>
   </div>
   <div className="chat-translation-appearance">
    <h4>译文外观</h4>
    <div className="account-form-grid">
     <div className="formal-field"><span>译文颜色</span><div className="chat-translation-color-inputs">
      <input type="color" aria-label="选择译文颜色" value={previewColor} onChange={e=>setDraft({...draft,color:e.target.value})}/>
      <input className="formal-input" aria-label="译文颜色代码" value={draft.color} maxLength={7} spellCheck={false} aria-invalid={!validColor} onChange={e=>setDraft({...draft,color:e.target.value.trim()})} placeholder="#93c5fd"/>
     </div></div>
     <label className="formal-field"><span>译文字号（8–48 px）</span><input className="formal-input" type="number" min={8} max={48} step={1} value={draft.fontSize||''} aria-invalid={!validSize} onChange={e=>setDraft({...draft,fontSize:Number(e.target.value)})}/></label>
    </div>
    <input className="chat-translation-size-slider" type="range" min={8} max={48} step={1} value={previewSize} aria-label="拖动调整译文字号" aria-valuetext={`${previewSize} 像素`} onChange={e=>setDraft({...draft,fontSize:Number(e.target.value)})}/>
    <div className="chat-translation-preview" aria-label="译文外观预览"><span>译文预览</span><p style={{color:previewColor,fontSize:previewSize}}>你好，很高兴收到你的消息。<br/>Hello, it's good to hear from you.</p></div>
    {!appearanceValid&&<p role="alert" className="chat-translation-error">颜色请填写 # 加 6 位十六进制代码，字号请填写 8–48 的整数。</p>}
    <button type="button" className="formal-button compact" onClick={()=>setDraft({...draft,color:'#93c5fd',fontSize:12})}>恢复默认外观</button>
   </div>
   <div className="chat-translation-actions"><button type="button" className="formal-button" disabled={!appearanceValid} onClick={()=>void run(save)}>保存翻译设置</button></div>
  </fieldset>}
  {locked&&<p role="status">任务占用，翻译已暂停，暂时不能修改或导出。</p>}
  <div className="chat-translation-export"><strong>聊天导出</strong><div className="chat-translation-actions"><button type="button" className="formal-button compact" disabled={disabled||!api} onClick={()=>void run(()=>exportChat('original'))}>导出原文</button><button type="button" className="formal-button compact" disabled={disabled||!api} onClick={()=>void run(()=>exportChat('bilingual'))}>导出双语</button></div></div>
  {(error||readError)&&<p role="alert" className="chat-translation-error">{error||readError}</p>}{message&&<p role="status" className="chat-translation-success">{message}</p>}
 </section>;
}
