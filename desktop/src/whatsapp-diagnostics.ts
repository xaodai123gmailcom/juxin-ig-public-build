/** Uses unique temporary storage only; never reads message bodies, login keys or cookies. */
export function observeWhatsAppInput(){
 if(location.origin!=='https://web.whatsapp.com')return;
 const world=globalThis as any;if(world.__juxinInputCounts)return;
 const counts=world.__juxinInputCounts={pointerDown:0,click:0,trustedPointerDown:0,trustedClick:0};
 addEventListener('pointerdown',e=>{counts.pointerDown++;if(e.isTrusted)counts.trustedPointerDown++},{capture:true,passive:true});
 addEventListener('click',e=>{counts.click++;if(e.isTrusted)counts.trustedClick++},{capture:true,passive:true});
}
export async function whatsappStorageProbe(){
 const results:Record<string,any>={secureContext:isSecureContext,origin:location.origin,isolated:crossOriginIsolated,
  inputEvents:(globalThis as any).__juxinInputCounts||null,
  viewport:{innerWidth,innerHeight,outerWidth,outerHeight,devicePixelRatio,visualWidth:visualViewport?.width,visualHeight:visualViewport?.height,visualScale:visualViewport?.scale,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,visibility:document.visibilityState,focused:document.hasFocus(),ready:document.readyState}};
 let stopped=false,active='',timer:ReturnType<typeof setTimeout>|undefined;
 const run=async()=>{
 const name='juxin-diagnostic-'+crypto.randomUUID();
 async function check(label:string,fn:()=>Promise<any>){if(stopped)return;active=label;try{results[label]={ok:true,value:await fn()}}catch(e){results[label]={ok:false,error:e instanceof Error?e.name:'Error'}}}
 await check('quota',async()=>{const e=await navigator.storage.estimate();return {quota:e.quota,usage:e.usage}});
 await check('persistentStorage',()=>navigator.storage.persisted());
 await check('localStorage',async()=>{try{localStorage.setItem(name,'probe');if(localStorage.getItem(name)!=='probe')throw new Error('RoundtripError');return true}finally{localStorage.removeItem(name)}});
 await check('indexedDBEncryption',async()=>{
  let db:IDBDatabase|undefined;
  try{
   db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open(name,1);const timer=setTimeout(()=>reject(new DOMException('Timeout','TimeoutError')),4000);r.onupgradeneeded=()=>r.result.createObjectStore('keys');r.onsuccess=()=>{clearTimeout(timer);resolve(r.result)};r.onerror=()=>{clearTimeout(timer);reject(r.error)};r.onblocked=()=>{clearTimeout(timer);reject(new DOMException('Blocked','InvalidStateError'))}});
   const base=await crypto.subtle.importKey('raw',crypto.getRandomValues(new Uint8Array(128)),{name:'HKDF'},false,['deriveKey']);
   await new Promise<void>((resolve,reject)=>{const t=db!.transaction('keys','readwrite',{durability:'relaxed'});t.objectStore('keys').put(base,'temporary');t.oncomplete=()=>resolve();t.onerror=()=>reject(t.error);t.onabort=()=>reject(t.error)});
   const saved=await new Promise<CryptoKey>((resolve,reject)=>{const r=db!.transaction('keys').objectStore('keys').get('temporary');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error)});
   const key=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:new Uint8Array(16),info:new Uint8Array(1)},saved,{name:'AES-CBC',length:128},false,['encrypt','decrypt']);
   const iv=crypto.getRandomValues(new Uint8Array(16)),cipher=await crypto.subtle.encrypt({name:'AES-CBC',iv},key,new TextEncoder().encode('diagnostic'));
   if(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-CBC',iv},key,cipher))!=='diagnostic')throw new Error('RoundtripError');return true;
  }finally{db?.close();indexedDB.deleteDatabase(name)}
 });
 await check('fileStorage',async()=>{const root=await navigator.storage.getDirectory();try{const f=await root.getFileHandle(name,{create:true}),w=await f.createWritable();await w.write('diagnostic');await w.close();if(await (await f.getFile()).text()!=='diagnostic')throw new Error('RoundtripError');return true}finally{await root.removeEntry(name).catch(()=>{})}});
 await check('workerFileStorage',async()=>{
  const source=`onmessage=async e=>{let root;try{root=await navigator.storage.getDirectory();const f=await root.getFileHandle(e.data,{create:true}),h=await f.createSyncAccessHandle();try{h.write(new TextEncoder().encode('probe'));h.flush();const b=new Uint8Array(5);h.read(b,{at:0});if(new TextDecoder().decode(b)!=='probe')throw new Error('RoundtripError')}finally{h.close()}postMessage({ok:true})}catch(e){postMessage({ok:false,error:e.name})}finally{if(root)await root.removeEntry(e.data).catch(()=>{})}}`;
  const url=URL.createObjectURL(new Blob([source],{type:'text/javascript'}));let worker:Worker|undefined;
  try{return await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new DOMException('Timeout','TimeoutError')),4000);try{worker=new Worker(url);worker.onmessage=e=>{clearTimeout(timer);e.data.ok?resolve(true):reject(new DOMException('Worker storage',e.data.error))};worker.onerror=()=>{clearTimeout(timer);reject(new DOMException('Worker blocked','WorkerStartError'))};worker.postMessage(name+'-worker')}catch(e){clearTimeout(timer);reject(e)}})}finally{worker?.terminate();URL.revokeObjectURL(url);await navigator.storage.getDirectory().then(root=>root.removeEntry(name+'-worker')).catch(()=>{})}
 });
 // A page policy can forbid the diagnostic's Blob worker while its own
 // first-party workers work normally. Do not mislabel that as disk failure.
 if(results.workerFileStorage?.error==='SecurityError'||results.workerFileStorage?.error==='WorkerStartError')results.workerFileStorage.storageFailureUnconfirmed=true;
 return results;
 };
 try{return await Promise.race([run(),new Promise<typeof results>(resolve=>{timer=setTimeout(()=>{stopped=true;if(active)results[active]={ok:false,error:'TimeoutError'};results.incomplete=true;resolve(results)},9000)})])}finally{if(timer)clearTimeout(timer)}
}
export function diagnosticCodes(message:string){return [...new Set(message.match(/\b(?:QuotaExceededError|DataCloneError|InvalidStateError|SecurityError|UnknownError|AbortError|NotAllowedError|NotFoundError|NotReadableError|VersionError|ConstraintError|TransactionInactiveError|ReadOnlyError|InvalidAccessError|DatabaseClosedError|OpenFailedError|PrematureCommitError|TypeError|ReferenceError|SyntaxError|EvalError|OperationError|TimeoutError|DbEncKeyNotLoaded|DbMsgEncKeyNotLoaded|DBInvalidFtsHMACKey|SQLITE_[A-Z_]+|ERR_[A-Z_]+)\b/g)||[])].slice(0,8)}
