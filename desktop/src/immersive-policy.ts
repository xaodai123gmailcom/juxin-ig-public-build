import {createHash} from 'node:crypto';
import {immersiveDomains} from './immersive-domains.js';
export const IMMERSIVE_VERSION='1.33.1';
export const IMMERSIVE_SHA256='9ec40a25e5fd479a4c4a460905ba0bc25b888469f781c74df5e90016bec4ac81';
export const IMMERSIVE_WORLD=1009;
export const immersiveServices=['plugin','bing','google','deepl','babelfast-pro','deepseek-pro','gemini-pro','openai-pro','qwen-pro'] as const;
export type ImmersiveSettings={engine:'builtin'|'immersive';immersiveMode:'manual'|'smart';immersiveService:string;immersiveFallbacks:string[]};
export const immersiveDefaults:ImmersiveSettings={engine:'builtin',immersiveMode:'manual',immersiveService:'plugin',immersiveFallbacks:['bing','google']};
export function validateImmersive(value:ImmersiveSettings){
 if(!['builtin','immersive'].includes(value.engine)||!['manual','smart'].includes(value.immersiveMode)||!immersiveServices.includes(value.immersiveService as any)||!Array.isArray(value.immersiveFallbacks)||value.immersiveFallbacks.length>4||value.immersiveFallbacks.some(x=>!immersiveServices.includes(x as any)))throw new Error('沉浸式翻译设置无效');
}
export function immersivePartition(owner:string,id:string){return 'persist:immersive-chat-'+createHash('sha256').update(JSON.stringify([owner,id])).digest('hex')}
export function immersiveRetryDelay(value:string|null,now=Date.now()){
 const seconds=Number(value),date=Date.parse(value||'');
 return Math.max(30000,value&&Number.isFinite(seconds)&&seconds>=0?seconds*1000:Number.isFinite(date)?date-now:0);
}
export function verifyImmersiveSource(bytes:Buffer){if(createHash('sha256').update(bytes).digest('hex')!==IMMERSIVE_SHA256)throw new Error('可选沉浸式翻译文件校验失败，请自行提供受支持的官方原始文件');return bytes.toString('utf8')}
export function officialImmersiveUrl(value:string){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&(!u.port||u.port==='443')&&['immersivetranslate.com','imtintl.com','immersive-translate.owenyoung.com'].some(h=>u.hostname===h||u.hostname.endsWith('.'+h))}catch{return false}}
export function immersiveRequestUrl(value:string,configured:string[]=[]){
 try{const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.hostname==='localhost'||/^\d+\.\d+\.\d+\.\d+$/.test(u.hostname)||u.hostname.includes(':'))return false;
 return immersiveDomains.some(h=>u.hostname===h||u.hostname.endsWith('.'+h))||configured.some(url=>{try{return new URL(url).origin===u.origin}catch{return false}})}catch{return false}
}
/** Ordinary user configuration only; never opt into vendor private SDK APIs. */
export function immersiveHostConfig(saved:any={}){
 if(!saved||typeof saved!=='object'||Array.isArray(saved))saved={};
 // The public openPopup event is handled by the browser-popup renderer. With
 // the userscript floating ball enabled, the vendor does not mount that renderer.
 return {...saved,enableInputTranslation:false,enableWebViewInputTranslationDot:false,useOnlineOptions:true,
  monkeyH5FloatBall:{...saved.monkeyH5FloatBall,enable:false},pcFloatBall:{...saved.pcFloatBall,enable:false},
  'monkeyH5FloatBall.add':{...saved['monkeyH5FloatBall.add'],enable:false},'pcFloatBall.add':{...saved['pcFloatBall.add'],enable:false}};
}
export function immersivePageConfig(saved:any={}){
 if(!saved||typeof saved!=='object'||Array.isArray(saved))saved={};
 return {...immersiveHostConfig(saved),disableReport:true,cache:false,domReadyDetectTimeout:0,
  enableDefaultAlwaysTranslatedUrls:false,forceAutoTranslate:false,translationMode:'dual',
  generalRule:{...saved.generalRule,selectors:['#juxin-messages .juxin-source'],excludeSelectors:['#juxin-status','button','input','textarea','[contenteditable]'],paragraphMinTextCount:1,paragraphMinWordCount:1,blockMinTextCount:1,blockMinWordCount:1,containerMinTextCount:1,mainFrameMinTextCount:1,mainFrameMinWordCount:1,domCheckTimeout:50,waitForSelectorsTimeout:50,bodyRule:{enable:false},isTranslateTitle:false,detectParagraphLanguage:true},
  translationServices:Object.fromEntries(Object.entries(saved.translationServices||{}).map(([k,v])=>[k,v]))};
}
// Audited against the pinned 1.33.1 bundle's localConfig writers. These are
// background probes, UI state or unrelated page caches, not translation settings.
// Persist them normally, but never tear down chat workers when they change.
// Unknown fields stay in the identity so future credentials/settings fail closed.
const immersiveRuntimeFields=new Set(['managedHostProbes','managedHostAccelerationProbe',
 'accountLastSyncedAt','confirmSupportMouse','floatBallConfig','showMangaGuide',
 'subtitlePositions','downloadSubtitle','tempTranslationUrlMatches']);
export function immersiveConfigIdentity(key:string,saved:any){
 const value=key==='fullLocalUserConfig'?immersivePageConfig(saved):Object.fromEntries(
  Object.entries(saved&&typeof saved==='object'&&!Array.isArray(saved)?saved:{}).filter(([name])=>!immersiveRuntimeFields.has(name)));
 // Object property order does not affect configuration. Keep array order, and
 // compare effective chat configuration, including enforced input exclusions.
 return JSON.stringify(value,(_key,item)=>item&&typeof item==='object'&&!Array.isArray(item)
  ?Object.fromEntries(Object.keys(item).sort().map(name=>[name,item[name]])):item);
}
export class ImmersiveRouting {
 private health=new Map<string,{elapsed:number;retryAt:number}>();
 constructor(private now=Date.now){}
 choose(settings:ImmersiveSettings){
  if(settings.immersiveMode==='manual')return (this.health.get(settings.immersiveService)?.retryAt||0)>this.now()?[]:[settings.immersiveService];
  const list=[...new Set([settings.immersiveService,...settings.immersiveFallbacks])];
  return list.filter(x=>(this.health.get(x)?.retryAt||0)<=this.now()).sort((a,b)=>(this.health.get(a)?.elapsed??2000)-(this.health.get(b)?.elapsed??2000)).slice(0,2);
 }
 success(service:string,elapsed:number){const old=this.health.get(service);this.health.set(service,{elapsed:old?old.elapsed*.7+elapsed*.3:elapsed,retryAt:0})}
 failure(service:string,retryMs=30000){this.health.set(service,{elapsed:30000,retryAt:this.now()+Math.max(1000,retryMs)})}
 nextDelay(settings:ImmersiveSettings){const list=settings.immersiveMode==='manual'?[settings.immersiveService]:[settings.immersiveService,...settings.immersiveFallbacks];return Math.max(0,Math.min(...list.map(x=>(this.health.get(x)?.retryAt||0)-this.now())))}
 reset(){this.health.clear()}
}
