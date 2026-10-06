/** Bounded reusable translators. Account/configuration keys never share a worker. */
type Entry<T>={key:string;value?:T;busy:boolean;releasedAt:number;live:()=>boolean;closed:boolean};
/** Configuration changed while a renderer was being created. This is not a provider failure. */
export class ImmersiveWorkerInvalidatedError extends Error {
 constructor(){super('翻译设置已更新，正在重新准备翻译');this.name='ImmersiveWorkerInvalidatedError'}
}
export class ImmersiveWorkerPool<T>{
 private entries=new Set<Entry<T>>();
 constructor(private create:(key:string,live:()=>boolean)=>Promise<T>,private destroy:(value:T)=>void,private now=Date.now,private limit=2,private idleMs=300000){}
 private close(entry:Entry<T>){if(entry.closed)return;entry.closed=true;this.entries.delete(entry);if(entry.value!==undefined)this.destroy(entry.value)}
 prune(){for(const e of this.entries)if((e.busy&&!e.live())||(!e.busy&&this.now()-e.releasedAt>=this.idleMs))this.close(e)}
 invalidate(prefix=''){for(const e of this.entries)if(e.key.startsWith(prefix))this.close(e)}
 async acquireCurrent(key:()=>string,live:()=>boolean){
  for(let attempt=0;;attempt++){
   if(!live())throw new Error('翻译已暂停');
   try{return await this.acquire(key(),live)}catch(error){
    // Re-read the configuration key. Never resurrect a revoked account/task,
    // retry arbitrary startup errors, or loop indefinitely while settings change.
    if(!(error instanceof ImmersiveWorkerInvalidatedError)||attempt>=1||!live())throw error;
   }
  }
 }
 async acquire(key:string,live:()=>boolean){
  this.prune();if(!live())throw new Error('翻译已暂停');
  let entry=[...this.entries].find(e=>!e.busy&&e.key===key);
  if(!entry){
   if(this.entries.size>=this.limit){const idle=[...this.entries].filter(e=>!e.busy).sort((a,b)=>a.releasedAt-b.releasedAt)[0];if(idle)this.close(idle)}
   if(this.entries.size>=this.limit)throw new Error('翻译正在处理上一条消息');
   entry={key,busy:true,releasedAt:0,live,closed:false};this.entries.add(entry);
   const reserved=entry;
   try{
    const value=await this.create(key,()=>!reserved.closed&&(!reserved.busy||reserved.live()));
    if(reserved.closed||!live()){this.destroy(value);if(reserved.closed&&live())throw new ImmersiveWorkerInvalidatedError();throw new Error('翻译已暂停')}
    reserved.value=value;
   }catch(error){const invalidated=reserved.closed;this.close(reserved);if(invalidated&&live())throw new ImmersiveWorkerInvalidatedError();throw error}
  }else{entry.busy=true;entry.live=live}
  const owned=entry;let released=false;
  return {value:entry.value!,release:(reusable:boolean)=>{if(released)return;released=true;if(!reusable||!owned.live()){this.close(owned);return}owned.busy=false;owned.live=()=>true;owned.releasedAt=this.now()}};
 }
 stats(){return {total:this.entries.size,busy:[...this.entries].filter(e=>e.busy).length}}
}
