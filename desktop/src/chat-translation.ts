import {IMMERSIVE_UNAVAILABLE} from './immersive-source.js';
import {immersiveDefaults,validateImmersive,type ImmersiveSettings} from './immersive-policy.js';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {writePrivateFileAtomically} from './atomic-secure-store.js';
export type ChatSettings=ImmersiveSettings&{translationRevision:string;enabled:boolean;outgoing:boolean;incomingLang:string;outgoingLang:string;color:string;fontSize:number;provider:'google'|'deepl'|'microsoft';region:string};
const defaults:ChatSettings={...immersiveDefaults,translationRevision:'',enabled:false,outgoing:false,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',fontSize:12,provider:'google',region:''};
export class TranslationServiceError extends Error{
 constructor(message:string,readonly kind:'rate'|'network'|'access'|'service',readonly retryAfterMs=0){super(message)}
}
function retryAfter(value:string|null){
 if(!value)return 0;
 const seconds=Number(value);if(Number.isFinite(seconds)&&seconds>=0)return seconds*1000;
 const date=Date.parse(value);return Number.isFinite(date)?Math.max(0,date-Date.now()):0;
}
export function chatSettings(value:any):ChatSettings{
 const result={...defaults,...value,outgoing:false};
 validateImmersive(result);
 if(typeof result.translationRevision!=='string'||!/^[a-z0-9:_-]{0,128}$/i.test(result.translationRevision))throw new Error('翻译设置版本无效');
 if(typeof result.enabled!=='boolean'||typeof result.outgoing!=='boolean'||!['google','deepl','microsoft'].includes(result.provider)||![result.incomingLang,result.outgoingLang].every(x=>typeof x==='string'&&/^[a-z]{2,3}(-[a-z]{2,4})?$/i.test(x))||!/^#[\da-f]{6}$/i.test(result.color)||!Number.isInteger(result.fontSize)||result.fontSize<8||result.fontSize>48||typeof result.region!=='string'||!/^[a-z0-9-]{0,40}$/.test(result.region))throw new Error('翻译设置无效');
 return Object.fromEntries(Object.keys(defaults).map(k=>[k,result[k as keyof ChatSettings]])) as ChatSettings;
}
/** Online providers only. No model download, silent fallback or fabricated translation. */
export async function translateChat(text:string,lang:string,settings:ChatSettings,key:string,fetcher:typeof fetch=fetch):Promise<string>{
 if(!text||text.length>5000)throw new Error('单条翻译最多 5000 字');
 let url:string,body:any,headers:Record<string,string>={'content-type':'application/json'};
 if(settings.provider==='google'){
  if(key){url='https://translation.googleapis.com/language/translate/v2';headers['x-goog-api-key']=key;body={q:text,target:lang,format:'text'}}
  else{url='https://translate.googleapis.com/translate_a/single?'+new URLSearchParams({client:'gtx',sl:'auto',tl:lang,dt:'t',q:text})}
 }else if(settings.provider==='deepl'){
  if(!key)throw new Error('请在同步翻译设置中填写 DeepL API 密钥');
  url=key.endsWith(':fx')?'https://api-free.deepl.com/v2/translate':'https://api.deepl.com/v2/translate';headers.authorization='DeepL-Auth-Key '+key;body={text:[text],target_lang:lang.toUpperCase().replace('ZH-CN','ZH').replace('ZH-TW','ZH')};
 }else{
  if(!key)throw new Error('请在同步翻译设置中填写 Microsoft API 密钥');
  url='https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&to='+encodeURIComponent(lang);headers['Ocp-Apim-Subscription-Key']=key;if(settings.region)headers['Ocp-Apim-Subscription-Region']=settings.region;body=[{Text:text}];
 }
 let response:Response;
 try{response=await fetcher(url,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined,redirect:'error',signal:AbortSignal.timeout(12000)})}catch{throw new TranslationServiceError('翻译服务连接失败','network')}
 if(!response.ok){
  if(response.status===429)throw new TranslationServiceError('翻译服务限流','rate',retryAfter(response.headers.get('retry-after')));
  if(response.status===401||response.status===403)throw new TranslationServiceError(settings.provider==='google'&&!key?'Google 免费通道拒绝访问，当前网络暂时无法使用该通道':'翻译服务拒绝访问，请检查所选服务的密钥和权限','access');
  throw new TranslationServiceError('翻译服务暂不可用（'+response.status+'）','service',retryAfter(response.headers.get('retry-after')));
 }
 let data:any;try{data=await response.json()}catch{throw new TranslationServiceError('翻译服务返回了无法识别的数据','service')}
 const value=settings.provider==='google'?(key?data?.data?.translations?.[0]?.translatedText:Array.isArray(data?.[0])?data[0].map((r:any)=>typeof r?.[0]==='string'?r[0]:'').join(''):''):settings.provider==='deepl'?data?.translations?.[0]?.text:data?.[0]?.translations?.[0]?.text;
 if(typeof value!=='string'||!value.trim()||value.length>30000)throw new Error('翻译服务没有返回有效译文');
 return settings.provider==='google'&&key?value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi,(all,entity)=>{const names:Record<string,string>={amp:'&',lt:'<',gt:'>',quot:'\"',apos:"'"};if(entity[0]!=='#')return names[entity.toLowerCase()]||all;const n=entity[1].toLowerCase()==='x'?parseInt(entity.slice(2),16):parseInt(entity.slice(1),10);return n>0&&n<=0x10ffff?String.fromCodePoint(n):all}):value;
}
export class ChatTranslation {
 private prefs:Record<string,ChatSettings>={};
 private allowed=new Map<string,{id:string;authorization:string}>();
 private errors=new Map<string,string>();
 private cache=new Map<string,string>();
 private pendingSettings=new Set<string>();
 private cacheLoaded=new Set<string>();
 private cacheDirty=new Set<string>();
 private cacheTimer?:ReturnType<typeof setTimeout>;
 private polling=false;
 private conversation?:{profile:string;key:string;generation?:string};
 private immersiveJobs=new Map<string,symbol>();
 private epoch=0;
 private gates=new Map<string,{nextAt:number;retryAt:number;failures:number;reason:string;blocked:boolean;busy:boolean}>();
 private timer:ReturnType<typeof setInterval>;
 constructor(private file:string,private token:()=>string|null,private target:()=>{id:string}|undefined,private core:(id:string,step:any,authorization:string)=>Promise<any>,private secret:(key:string)=>string|null,private saveSecret:(key:string,value:string)=>void,private runtime:{fetcher?:typeof fetch;now?:()=>number;immersiveOnly?:boolean;immersive?:{install?:()=>Promise<boolean>;translate:(id:string,text:string,lang:string,settings:ImmersiveSettings,current:()=>boolean,onResult?:(text:string)=>Promise<void>)=>Promise<string>;settings:(id:string)=>Promise<void>;cacheIdentity:(id:string)=>string;status:(id:string)=>any;retry:(id:string)=>void;reset:()=>void}}={}){
  try{const saved=JSON.parse(readFileSync(file,'utf8'));for(const [id,value] of Object.entries(saved))this.prefs[id]=chatSettings(value)}catch{}
  this.timer=setInterval(()=>void this.tick(),300);this.timer.unref();
 }
 reset(){this.runtime.immersive?.reset();this.immersiveJobs.clear();this.flushCache();this.epoch++;this.conversation=undefined;this.allowed.clear();this.errors.clear();this.cache.clear();this.cacheLoaded.clear();this.pendingSettings.clear();this.gates.clear()}
 stop(){clearInterval(this.timer);this.reset()}
 private now(){return (this.runtime.now||Date.now)()}
 private channel(id:string,settings:ChatSettings){
  const secret=this.secret('chat-translation:'+id+':'+settings.provider)||'';
  // Anonymous Google requests share the same server limit across all windows.
  const key=createHash('sha256').update(JSON.stringify(settings.engine==='immersive'?['immersive',id,settings.immersiveMode,settings.immersiveService,settings.immersiveFallbacks,this.runtime.immersive?.cacheIdentity(id)]:[settings.provider,settings.region,secret])).digest('hex');
  let gate=this.gates.get(key);if(!gate){gate={nextAt:0,retryAt:0,failures:0,reason:'',blocked:false,busy:false};this.gates.set(key,gate)}
  return {secret,gate,key};
 }
 // Reuse a window's results after restarting. The existing OS-encrypted secret
 // store holds translations; message text is only present in a SHA-256 key.
 private loadCache(id:string){
  if(this.cacheLoaded.has(id))return;this.cacheLoaded.add(id);
  try{const rows=JSON.parse(this.secret('chat-translation-cache:'+id)||'[]');
   if(Array.isArray(rows))for(const row of rows.slice(-500))if(Array.isArray(row)&&/^[a-f0-9]{64}$/.test(row[0])&&typeof row[1]==='string'&&row[1].length<=30000)this.cache.set(id+':'+row[0],row[1]);
   while(this.cache.size>3000)this.cache.delete(this.cache.keys().next().value!);
  }catch{/* A missing or damaged cache never prevents live translation. */}
 }
 private remember(id:string,key:string,value:string){
  this.cache.delete(key);this.cache.set(key,value);
  const own=[...this.cache.keys()].filter(k=>k.startsWith(id+':')).reverse();let bytes=0;
  for(const [i,k] of own.entries()){bytes+=Buffer.byteLength(this.cache.get(k)!,'utf8')+k.length;if(i>=500||bytes>500000)this.cache.delete(k)}
  while(this.cache.size>3000)this.cache.delete(this.cache.keys().next().value!);
  this.cacheDirty.add(id);
  if(!this.cacheTimer){this.cacheTimer=setTimeout(()=>this.flushCache(),1000);this.cacheTimer.unref()}
 }
 private flushCache(){
  if(this.cacheTimer)clearTimeout(this.cacheTimer);this.cacheTimer=undefined;
  for(const id of this.cacheDirty){const prefix=id+':';try{this.saveSecret('chat-translation-cache:'+id,JSON.stringify([...this.cache].filter(([k])=>k.startsWith(prefix)).map(([k,v])=>[k.slice(prefix.length),v])))}catch{/* Memory caching still works if secure storage is unavailable. */}}
  this.cacheDirty.clear();
 }
 private waiting(gate:{retryAt:number;reason:string;blocked:boolean}){
  if(gate.blocked)return gate.reason;
  return gate.retryAt>this.now()?`${gate.reason}，${Math.ceil((gate.retryAt-this.now())/1000)} 秒后自动重试`:'';
 }
 async command(id:string,input:any,authorization:string){
  const check=await this.core(id,{kind:'check'},authorization);if(this.token()!==authorization)throw new Error('登录状态已变化');
  this.allowed.set(check.profile,{id,authorization});
  this.loadCache(id);
  let settings=this.prefs[id]||{...defaults};
  if(this.runtime.immersiveOnly){
   if(input.mode==='builtin'||(input.settings&&input.settings.engine!=='immersive'))throw new Error('旧同步翻译已停用，请使用沉浸式翻译');
   if(settings.engine!=='immersive'){settings=chatSettings({...settings,engine:'immersive',enabled:false});input={...input,settings:input.settings??settings}}
  }
  if(input.mode!==undefined){if(!['off','builtin','immersive'].includes(input.mode))throw new Error('翻译模式无效');input={...input,settings:{...settings,enabled:input.mode!=='off',engine:input.mode==='off'?settings.engine:input.mode}}}
  let installed:boolean|undefined;
  if(input.installImmersive){if(!this.runtime.immersive?.install)throw new Error('此版本不支持导入可选组件');installed=await this.runtime.immersive.install();if(this.token()!==authorization)throw new Error('登录状态已变化');if(installed){this.errors.delete(id);input={...input,retry:true,settings:chatSettings({...settings,engine:'immersive',enabled:false})}}}
  const immersiveStatus=this.runtime.immersive?.status(id);
  if(immersiveStatus?.available===false&&settings.engine==='immersive'&&settings.enabled&&input.settings===undefined)input={...input,settings:chatSettings({...settings,enabled:false})};
  if(input.settings?.enabled&&input.settings.engine==='immersive'&&immersiveStatus?.available===false)throw new Error(immersiveStatus.message||IMMERSIVE_UNAVAILABLE);
  if(input.openImmersive){if(!this.runtime.immersive)throw new Error('此版本未加载沉浸式翻译');await this.runtime.immersive.settings(id)}
  if(input.retry){const {gate}=this.channel(id,settings);gate.blocked=false;gate.retryAt=0;gate.failures=0;gate.reason='';this.errors.delete(id);this.runtime.immersive?.retry(id)}
  const secretKey=(provider:string)=>'chat-translation:'+id+':'+provider;
  if(input.settings!==undefined){const next=chatSettings(input.settings);if(input.apiKey!==undefined){if(typeof input.apiKey!=='string'||input.apiKey.length>2048||/[\r\n]/.test(input.apiKey))throw new Error('翻译密钥无效');this.saveSecret(secretKey(next.provider),input.apiKey.trim())}
   // Publish runtime preferences only after the durable replacement commits.
   // A failed rename/write must not secretly enable a rejected configuration.
   const prefs={...this.prefs,[id]:next};writePrivateFileAtomically(this.file,JSON.stringify(prefs));this.prefs=prefs;this.epoch++;this.errors.delete(id);
   this.pendingSettings.add(check.profile);
   const epoch=this.epoch;
   try{await this.core(id,{kind:'poll',settings:next},authorization);if(this.epoch===epoch)this.pendingSettings.delete(check.profile)}catch{/* Apply settings once the covered or task-owned page becomes available. */}
  }
  if(input.export){if(!['original','bilingual'].includes(input.export))throw new Error('导出格式无效');const result=await this.core(id,{kind:'export'},authorization);if(this.token()!==authorization)throw new Error('登录状态已变化');return {text:result.messages.map((r:any)=>r.original+(input.export==='bilingual'&&r.translation?'\n译文：'+r.translation:'')).join('\n\n'),count:result.messages.length}}
  const current=this.prefs[id]||settings,{gate}=this.channel(id,current);
  return {settings:current,...(installed===undefined?{}:{installed}),immersive:this.runtime.immersive?.status(id),hasKey:Boolean(this.secret(secretKey(current.provider))),error:current.engine==='immersive'&&immersiveStatus?.available===false?(immersiveStatus.message||IMMERSIVE_UNAVAILABLE):current.enabled?(this.errors.get(id)||this.waiting(gate)||(gate.reason?'等待自动重试翻译':'')):'',retryAt:immersiveStatus?.available!==false&&current.enabled&&!this.errors.get(id)&&gate.retryAt>this.now()?gate.retryAt:0};
 }
 private async tick(){
  if(this.polling)return;const profile=this.target(),authorization=this.token(),entry=profile&&this.allowed.get(profile.id);
  if(!profile||!authorization||!entry||entry.authorization!==authorization)return;
  const saved=this.prefs[entry.id];if(this.runtime.immersiveOnly&&saved?.engine!=='immersive')return;if(!saved||(!saved.enabled&&!this.pendingSettings.has(profile.id)))return;
  const identity=()=>saved.engine==='immersive'?(this.runtime.immersive?.cacheIdentity(entry.id)||''):'';
  const unavailable=saved.engine==='immersive'&&this.runtime.immersive?.status(entry.id)?.available===false;
  const settings={...saved,enabled:saved.enabled&&!unavailable,translationRevision:identity()};
  const epoch=this.epoch;this.polling=true;
  let conversation:typeof this.conversation;
  const current=()=>epoch===this.epoch&&this.token()===authorization&&this.target()?.id===profile.id&&settings.translationRevision===identity()&&(!conversation||this.conversation===conversation);
  let batch:any;
  try{batch=await this.core(entry.id,{kind:'poll',settings},authorization)}catch{return}finally{this.polling=false}
  if(!current())return;this.pendingSettings.delete(profile.id);
  // A profile can switch chats without changing account/window identity. Revoke
  // the old provider's live predicate as soon as a different chat is observed;
  // its own finally still owns releasing the in-flight reservation and worker.
  const key=typeof batch?.conversation==='string'?batch.conversation:'';
  const generation=typeof batch?.generation==='string'?batch.generation:undefined;
  if(this.conversation?.profile!==profile.id||this.conversation.key!==key||this.conversation.generation!==generation)this.conversation={profile:profile.id,key,generation};
  conversation=this.conversation;
  if(unavailable)this.errors.set(entry.id,IMMERSIVE_UNAVAILABLE);
  if(!settings.enabled)return;
  try{
   if(batch.status){this.errors.set(entry.id,batch.status);return}
   this.errors.delete(entry.id);
   const jobs=batch.messages||[];
   if(settings.engine==='immersive'){await this.translateImmersive(entry.id,batch,settings,authorization,current);return}
   for(const job of jobs){
    if(!current())return;const lang=settings.incomingLang;
    const {secret,gate,key:channelKey}=this.channel(entry.id,settings);
    const cacheKey=entry.id+':'+createHash('sha256').update(JSON.stringify([channelKey,lang,job.original])).digest('hex'),cached=this.cache.get(cacheKey);
    if(!cached&&(gate.busy||gate.blocked||this.now()<gate.retryAt||this.now()<gate.nextAt))continue;
    let providerComplete=Boolean(cached);
    try{
     let translation=cached;
     if(!translation){
      gate.busy=true;
      gate.nextAt=this.now()+(settings.provider==='google'&&!secret?1800:500);
      try{translation=await translateChat(job.original,lang,settings,secret,this.runtime.fetcher)}finally{gate.busy=false}
      gate.failures=0;gate.retryAt=0;gate.reason='';
     }
     providerComplete=true;
     if(this.token()===authorization&&epoch===this.epoch&&!cached)this.remember(entry.id,cacheKey,translation);
     if(!current())return;
     await this.core(entry.id,{kind:'apply',conversation:batch.conversation,...job,translation},authorization);this.errors.delete(entry.id);
    }catch(e){
     if(providerComplete)return; // A covered/stale page is not a provider failure.
     gate.failures++;gate.reason=e instanceof Error?e.message:'翻译失败';
     gate.blocked=e instanceof TranslationServiceError&&e.kind==='access';
     const base=e instanceof TranslationServiceError&&e.kind==='rate'?30000:5000;
     gate.retryAt=gate.blocked?0:this.now()+Math.max(e instanceof TranslationServiceError?e.retryAfterMs:0,Math.min(300000,base*2**Math.min(gate.failures-1,6)));
     if(!current())return;
     const message=this.waiting(gate)||gate.reason;
     await this.core(entry.id,{kind:'apply',conversation:batch.conversation,...job,error:true,retryable:!gate.blocked,retryAt:gate.retryAt,status:message},authorization).catch(()=>{});break;
    }
   }
  }catch{/* Core task leases take priority; retry only after the window is free. */}
 }
 private async translateImmersive(id:string,batch:any,settings:ChatSettings,authorization:string,current:()=>boolean){
  const runtime=this.runtime.immersive;if(!runtime){this.errors.set(id,'沉浸式翻译组件未加载');return}
  const {gate,key}=this.channel(id,settings),tasks:Promise<void>[]=[];
  for(const job of batch.messages||[]){
   if(!current())return;
   const cacheKey=id+':'+createHash('sha256').update(JSON.stringify([key,settings.incomingLang,job.original])).digest('hex'),cached=this.cache.get(cacheKey);
   if(cached){await this.core(id,{kind:'apply',conversation:batch.conversation,...job,translation:cached},authorization);continue}
   if(gate.blocked||gate.retryAt>this.now()||this.immersiveJobs.has(cacheKey)||this.immersiveJobs.size>=2)continue;
   const jobToken=Symbol();this.immersiveJobs.set(cacheKey,jobToken);
   tasks.push((async()=>{
    let providerComplete=false;
    try{
     let published=false;
     const publish=async(result:string)=>{
      providerComplete=true;
      if(published||!current())return;published=true;
      this.remember(id,cacheKey,result);gate.failures=0;gate.retryAt=0;gate.reason='';
      await this.core(id,{kind:'apply',conversation:batch.conversation,...job,translation:result},authorization);this.errors.delete(id);
     };
     // Present the completed translation before the isolated vendor document
     // finishes clearing. Keep its job reservation until cleanup really ends.
     const result=await runtime.translate(id,job.original,settings.incomingLang,settings,current,publish);
     await publish(result);
    }catch(error){
     if(providerComplete)return; // A task lock/covered view is not a provider failure.
     if(!current())return;gate.failures++;gate.blocked=(error as any)?.requiresConfiguration===true;gate.reason=error instanceof Error?error.message:'沉浸式翻译失败';const retry=Number((error as any)?.retryAfterMs);gate.retryAt=gate.blocked?0:this.now()+Math.max(3000,Number.isFinite(retry)?retry:0);
     await this.core(id,{kind:'apply',conversation:batch.conversation,...job,error:true,retryable:!gate.blocked,retryAt:gate.retryAt,status:gate.reason},authorization).catch(()=>{});
    }finally{if(this.immersiveJobs.get(cacheKey)===jobToken)this.immersiveJobs.delete(cacheKey);if(current()&&!gate.failures)queueMicrotask(()=>{if(current())void this.tick()})}
   })());
  }
  await Promise.all(tasks);
 }

}
