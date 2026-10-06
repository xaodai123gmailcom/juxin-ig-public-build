import {promises as fs,existsSync,readFileSync,appendFileSync,statSync,renameSync,unlinkSync,mkdirSync} from 'node:fs';
import {join,dirname,resolve} from 'node:path';
import {writePrivateFileAtomically} from './atomic-secure-store.js';
const MB=1024*1024,DAY=86400000;
export async function directoryBytes(path:string):Promise<number>{
 try{const stat=await fs.lstat(path);if(stat.isSymbolicLink())return 0;if(stat.isFile())return stat.size;if(!stat.isDirectory())return 0;let size=0;for(const entry of await fs.readdir(path))size+=await directoryBytes(join(path,entry));return size}catch{return 0}
}
export async function clearManagedFiles(path:string,olderThan:number):Promise<number>{
 let current=resolve(path);
 while(true){try{if((await fs.lstat(current)).isSymbolicLink())return 0}catch{}const parent=dirname(current);if(parent===current)break;current=parent}
 let count=0;try{const stat=await fs.lstat(path);if(stat.isSymbolicLink()||!stat.isDirectory())return 0;for(const entry of await fs.readdir(path)){const full=join(path,entry),info=await fs.lstat(full);if(info.isSymbolicLink())continue;if(info.isDirectory())count+=await clearManagedFiles(full,olderThan);else if(info.isFile()&&info.mtimeMs<olderThan){try{await fs.unlink(full);count++}catch{}}}}catch{}return count
}
export function appendRotatingLog(root:string,message:string,max=20*MB,files=5){
 mkdirSync(root,{recursive:true});const file=join(root,'app.log');const line=new Date().toISOString()+' '+message.replace(/[\r\n]/g,' ').slice(0,4096)+'\n';
 if(existsSync(file)&&statSync(file).size+Buffer.byteLength(line)>max){for(let i=files-1;i>=1;i--){const dest=join(root,`app.${i}.log`),src=join(root,i===1?'app.log':`app.${i-1}.log`);if(existsSync(dest))unlinkSync(dest);if(existsSync(src))renameSync(src,dest)}}appendFileSync(file,line,{encoding:'utf8',mode:0o600});
}
type Profile={id:string;partition:string;path:string;busy:boolean;opened:boolean};
type CacheSession={getCacheSize:()=>Promise<number>;clearCache:()=>Promise<void>};
export class StorageManagement{
 readonly policy={auto:true,perAccountBytes:500*MB,totalBytes:5*1024*MB,checkHours:6,tempDays:7};
 private running=false;private scan?:Promise<any>;private timer?:ReturnType<typeof setInterval>;lastResult='尚未清理';
 constructor(readonly root:string,private appPath:string,private crashPath:string,private profiles:()=>Promise<Profile[]>,private session:(partition:string)=>CacheSession){try{const saved=JSON.parse(readFileSync(join(root,'storage-policy.json'),'utf8'));if(typeof saved.auto==='boolean')this.policy.auto=saved.auto}catch{}}
 log(message:string){try{appendRotatingLog(join(this.root,'Logs'),message)}catch{}}
 start(){if(this.policy.auto)void this.clean('auto').catch(()=>{});this.timer=setInterval(()=>{if(this.policy.auto)void this.clean('auto').catch(()=>{})},6*3600000);this.timer.unref()}
 stop(){if(this.timer)clearInterval(this.timer)}
 setAuto(auto:boolean){writePrivateFileAtomically(join(this.root,'storage-policy.json'),JSON.stringify({auto}));this.policy.auto=auto}
 snapshot(){if(!this.scan)this.scan=this.inspect().finally(()=>{this.scan=undefined});return this.scan}
 private async inspect(){
  const profiles=await this.profiles();let cache=await directoryBytes(join(this.root,'Cache','Cache_Data')),account=0;const rows=[];
  for(const p of profiles){const bytes=await directoryBytes(p.path),http=await directoryBytes(join(p.path,'Cache')),code=await directoryBytes(join(p.path,'Code Cache')),gpu=await directoryBytes(join(p.path,'GPUCache'));const disposable=http+code+gpu;cache+=disposable;account+=Math.max(0,bytes-disposable);rows.push({id:p.id,bytes:disposable,busy:p.busy})}
  return {policy:this.policy,cache,account,profiles:rows,program:await directoryBytes(this.appPath),database:await directoryBytes(join(this.root,'data','collector.sqlite3'))+await directoryBytes(join(this.root,'data','collector.sqlite3-wal'))+await directoryBytes(join(this.root,'data','collector.sqlite3-shm')),temp:await directoryBytes(join(this.root,'Cache','Temp')),screenshots:await directoryBytes(join(this.root,'Cache','Screenshots')),translation:await directoryBytes(join(this.root,'Cache','Translation'))+await directoryBytes(join(this.root,'Partitions','translation','Cache')),logs:await directoryBytes(join(this.root,'Logs')),crashes:await directoryBytes(this.crashPath),lastResult:this.lastResult};
 }
 async clean(kind:string){
  if(!['all','auto','temp','media','logs','translation'].includes(kind))throw new Error('无效清理类型');if(this.running)throw new Error('正在清理，请稍后查看结果');this.running=true;
  let cleaned=0,skipped=0;
  try{
   if(kind==='all'||kind==='auto'||kind==='temp'){
    // Manual cleanup includes every managed file, including timestamps rounded
    // ahead of Date.now() on Windows or left by a previous clock setting.
    const cutoff=kind==='auto'?Date.now()-7*DAY:Number.POSITIVE_INFINITY;
    cleaned+=await clearManagedFiles(join(this.root,'Cache','Temp'),cutoff);
    cleaned+=await clearManagedFiles(join(this.root,'Cache','Screenshots','Errors'),cutoff);
    cleaned+=await clearManagedFiles(this.crashPath,cutoff);
   }
   if(kind==='all'||kind==='translation'){cleaned+=await clearManagedFiles(join(this.root,'Cache','Translation'),Number.POSITIVE_INFINITY);await this.session('persist:translation').clearCache();}
   if(kind==='all'||kind==='logs'){for(let i=1;i<5;i++){try{await fs.unlink(join(this.root,'Logs',`app.${i}.log`));cleaned++}catch{}}}
   if(kind==='all'||kind==='auto'||kind==='media'){
    const shellBytes=await directoryBytes(join(this.root,'Cache','Cache_Data'));
    if(kind!=='auto'||shellBytes>this.policy.perAccountBytes)await this.session('').clearCache();
    const profiles=await this.profiles(),sizes=[];
    for(const p of profiles){const cache=await directoryBytes(join(p.path,'Cache'))+await directoryBytes(join(p.path,'Code Cache'))+await directoryBytes(join(p.path,'GPUCache'));let age=0;try{age=(await fs.stat(join(p.path,'Cache'))).mtimeMs}catch{}sizes.push({...p,cache,age})}
    let total=sizes.reduce((sum,p)=>sum+p.cache,0);
    for(const p of sizes.sort((a,b)=>a.age-b.age)){
     if(kind==='auto'&&p.cache<=this.policy.perAccountBytes&&total<=this.policy.totalBytes)continue;
     if(p.busy){skipped++;continue}
     try{
      // HTTP cache API never calls clearStorageData: cookies/IndexedDB stay intact.
      await this.session(p.partition).clearCache();
      if(!p.opened)for(const name of ['Code Cache','GPUCache'])await clearManagedFiles(join(p.path,name),Number.POSITIVE_INFINITY);
      const remaining=await directoryBytes(join(p.path,'Cache'))+await directoryBytes(join(p.path,'Code Cache'))+await directoryBytes(join(p.path,'GPUCache'));
      total-=Math.max(0,p.cache-remaining);cleaned++;
     }catch{skipped++}
    }
   }
   this.lastResult=`清理 ${cleaned} 项，${skipped} 个使用中或暂不可清理的窗口已跳过`;this.log(this.lastResult);return {cleaned,skipped,message:this.lastResult};
  }finally{this.running=false}
 }
}
export async function accountCacheProfiles(root:string,busy:Set<string>,opened:Set<string>):Promise<Profile[]>{
 const rows:Profile[]=[];const path=join(root,'Partitions');
 try{for(const name of await fs.readdir(path)){const match=/^account-([0-9a-f-]{36})-([0-9a-f-]{36})$/i.exec(name);if(!match)continue;const full=join(path,name),stat=await fs.lstat(full);if(stat.isSymbolicLink()||!stat.isDirectory())continue;const id='native:'+match[2];rows.push({id,path:full,partition:'persist:'+name,busy:busy.has(id),opened:opened.has(id)})}}catch{}
 // Only the active rebuilt WhatsApp profile is a cache-cleanup candidate.
 // Retained previous environments are never deleted automatically.
 const wa=join(root,'WA');
 try{if(!(await fs.lstat(wa)).isSymbolicLink())for(const name of await fs.readdir(wa)){
   if(!/^[0-9a-f]{32}\.json$/.test(name))continue;
   try{
     const marker=join(wa,name);if((await fs.lstat(marker)).isSymbolicLink())continue;
     const saved=JSON.parse(await fs.readFile(marker,'utf8')),key=name.slice(0,-5);
     if(!/^native:[0-9a-f-]{36}$/i.test(saved.profile)||!new RegExp('^'+key+'(?:-[0-9a-f]{16})?$').test(saved.active))continue;
     const full=join(wa,saved.active),stat=await fs.lstat(full);if(stat.isSymbolicLink()||!stat.isDirectory())continue;
     rows.push({id:saved.profile,path:full,partition:'wa-path:'+full,busy:busy.has(saved.profile)||opened.has(saved.profile),opened:opened.has(saved.profile)});
   }catch{}
 }}catch{}
 return rows;
}
