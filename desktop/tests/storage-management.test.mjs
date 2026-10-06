import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,stat,symlink,utimes,readdir,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {StorageManagement,clearManagedFiles,appendRotatingLog} from '../../dist-electron/storage-management.js';

test('all-cache cleanup preserves login, business records, manual screenshots and task-owned caches',async()=>{
 const root=await mkdtemp(join(tmpdir(),'juxin-storage-'));
 try{
  const put=async(relative,data='protected')=>{const p=join(root,relative);await mkdir(join(p,'..'),{recursive:true});await writeFile(p,data);return p};
  const protectedPaths=['Partitions/idle/Cookies','Partitions/idle/IndexedDB/login','Partitions/idle/Local Storage/token','Partitions/busy/Cache/task','data/collector.sqlite3','Cache/Screenshots/Manual/user.jpg','data/studio-media/post.jpg'];
  for(const p of protectedPaths)await put(p);
  // Manual cleanup must not rely on filesystem and wall-clock timestamp
  // resolution matching, or on the machine clock never having changed.
  for(const p of ['Cache/Temp/used','Cache/Screenshots/Errors/error.jpg','Cache/Translation/old','crashes/dump','Partitions/idle/Code Cache/old','Partitions/idle/GPUCache/old','Logs/app.1.log']){
   const file=await put(p);await utimes(file,new Date('2099-01-01'),new Date('2099-01-01'));
  }
  const calls=[];const profiles=[{id:'idle',partition:'idle',path:join(root,'Partitions/idle'),opened:false,busy:false},{id:'busy',partition:'busy',path:join(root,'Partitions/busy'),opened:true,busy:true}];
  const storage=new StorageManagement(root,join(root,'App'),join(root,'crashes'),async()=>profiles,p=>({getCacheSize:async()=>0,clearCache:async()=>calls.push(p)}));
  const result=await storage.clean('all');assert.equal(result.skipped,1);assert.deepEqual(calls.sort(),['','idle','persist:translation']);
  for(const p of protectedPaths)assert.equal(await readFile(join(root,p),'utf8'),'protected',p);
  for(const p of ['Cache/Temp/used','Cache/Screenshots/Errors/error.jpg','Cache/Translation/old','crashes/dump','Partitions/idle/Code Cache/old','Partitions/idle/GPUCache/old','Logs/app.1.log'])await assert.rejects(stat(join(root,p)),{code:'ENOENT'},p);
  storage.setAuto(false);const reload=new StorageManagement(root,'','',async()=>[],()=>({}));assert.equal(reload.policy.auto,false);
 }finally{await rm(root,{recursive:true,force:true})}
});

test('automatic cleanup expires only old managed files; symbolic links never escape cleanup root',async()=>{
 const root=await mkdtemp(join(tmpdir(),'juxin-expiry-'));
 try{
  await mkdir(join(root,'Cache/Temp'),{recursive:true});await mkdir(join(root,'UserData'),{recursive:true});
  for(const name of ['old','recent'])await writeFile(join(root,'Cache/Temp',name),'temp');
  await utimes(join(root,'Cache/Temp/old'),new Date(0),new Date(0));
  const storage=new StorageManagement(root,'','',async()=>[],()=>({clearCache:async()=>{}}));await storage.clean('auto');
  await assert.rejects(stat(join(root,'Cache/Temp/old')));assert.equal(await readFile(join(root,'Cache/Temp/recent'),'utf8'),'temp');
  await writeFile(join(root,'UserData/keep'),'login');
  try{await symlink(join(root,'UserData'),join(root,'Cache/link'),'dir')}catch(e){if(e.code==='EPERM')return;throw e}
  assert.equal(await clearManagedFiles(join(root,'Cache/link'),Date.now()+1000),0);
  assert.equal(await clearManagedFiles(join(root,'Cache/link/subdir'),Date.now()+1000),0);
  assert.equal(await readFile(join(root,'UserData/keep'),'utf8'),'login');
 }finally{await rm(root,{recursive:true,force:true})}
});

test('logs rotate into a fixed five-file set',async()=>{
 const root=await mkdtemp(join(tmpdir(),'juxin-logs-'));
 try{for(let i=0;i<80;i++)appendRotatingLog(root,'event '+i+' '+'.'.repeat(80),250,5);
 const names=await readdir(root);assert.equal(names.length,5);for(const p of names)assert.ok((await stat(join(root,p))).size<=250);assert.match(await readFile(join(root,'app.log'),'utf8'),/event 79/);
 }finally{await rm(root,{recursive:true,force:true})}
});
