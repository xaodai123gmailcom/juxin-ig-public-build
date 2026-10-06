const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const syntheticSource=require('./support/immersive-synthetic-source.cjs');
/** Windows release check: first-party synthetic adapter with real GM preload/IPC.
 * Checks native startup, isolation and settings; does not execute vendor code. */
module.exports=async function({win}){
 const {ImmersiveTranslator}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/immersive-translator.js')).href);
 const {immersivePartition}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/immersive-policy.js')).href);
 const {app,BrowserWindow,session}=require('electron');let owner='immersive-release-fixture';const secrets=new Map();
 const missing=new ImmersiveTranslator(()=>owner,()=>win,k=>secrets.get(k)||null,(k,v)=>secrets.set(k,v));
 const beforeWindows=BrowserWindow.getAllWindows().map(window=>window.id).sort();let createdWindows=0;
 const onWindowCreated=()=>createdWindows++;app.on('browser-window-created',onWindowCreated);
 try{
  const status=missing.status('missing-source');assert.equal(status.available,false);assert.ok(status.message);
  await assert.rejects(missing.settings('missing-source'),error=>error.message===status.message);
  await assert.rejects(missing.translate('missing-source','offline fixture','zh-CN',{
   engine:'immersive',immersiveMode:'manual',immersiveService:'plugin',immersiveFallbacks:[]
  },()=>true),error=>error.message===status.message);
  assert.equal(missing.windows.size,0);assert.equal(createdWindows,0,'absent optional source must reject before creating any window');
  assert.deepEqual(BrowserWindow.getAllWindows().map(window=>window.id).sort(),beforeWindows);
  console.log('CHECK immersive absent optional source rejects settings and translation without opening a window');
 }finally{app.removeListener('browser-window-created',onWindowCreated);missing.stop()}
 const id='fixture-window',ses=session.fromPartition(immersivePartition(owner,id));
 // No external requests, credentials, account sessions or user messages in this check.
 console.log('CHECK immersive synthetic fixture is offline: no vendor code or external requests; real preload, storage and IPC');
 ses.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*']},(_details,callback)=>callback({cancel:true}));
 const translator=new ImmersiveTranslator(()=>owner,()=>win,k=>secrets.get(k)||null,(k,v)=>secrets.set(k,v),syntheticSource);
 try{
  await Promise.all([translator.settings(id),translator.settings(id)]);
  const contexts=[...translator.windows.values()];assert.equal(contexts.length,1);
  const c=contexts[0];assert.equal(c.win.webContents.session,ses);assert.notEqual(c.win.webContents.session,win.webContents.session);
  const status=await c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:"globalThis.__juxinImmersiveCommand({kind:'status'})"}]);
  assert.equal(status.ready,true);assert.ok(status.menus>0,'synthetic adapter registers a menu through the real GM bridge');
  const panel=await c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:"globalThis.__juxinImmersiveCommand({kind:'panel'})"}]);
  assert.equal(panel.visible,true,'a running script or a floating ball is not an opened settings panel');assert.ok(panel.controls>=2,'synthetic panel controls must be present in the actual native renderer');
  assert.equal(await c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:"Array.from(document.querySelector('#immersive-translate-browser-popup').shadowRoot.querySelectorAll('style')).some(s=>s.textContent.includes('.popup-container'))"}]),true,'native popup styles have real CSS text');
  console.log('CHECK immersive settings-ready');
  const first=await translator.acquireWorker(id,'zh-CN','plugin',()=>owner!==null);const firstId=first.value.win.webContents.id;
  await translator.command(first.value,{kind:'clear'});first.release(true);
  const identity=translator.cacheIdentity(id);
  const metadataWrite=()=>c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:`(async()=>{
   const config=await GM.getValue('localConfig',{});
   await GM.setValue('localConfig',{...config,managedHostProbes:{...config.managedHostProbes,
    s:{hostName:'immersivetranslate.com',checkedAt:Date.now(),hostNamesSignature:'release-fixture'}},
    confirmSupportMouse:true,accountLastSyncedAt:Date.now()});return true;
  })()`}]);
  await metadataWrite();assert.equal(translator.cacheIdentity(id),identity,'background probe writes keep the chat configuration revision');
  const second=await translator.acquireWorker(id,'zh-CN','plugin',()=>owner!==null);assert.equal(second.value.win.webContents.id,firstId,'same ready Electron renderer is reused');second.release(true);
  console.log('CHECK immersive warm-reuse after real GM storage metadata writes');

  // Change actual settings through the real GM preload/IPC during cold startup,
  // then write representative background metadata during replacement startup.
  // This must cause exactly one restart, without weakening the retry limit.
  translator.pool.invalidate();const originalCreate=translator.create.bind(translator);let coldStarts=0;
  translator.create=(...args)=>{
   const pending=originalCreate(...args);if(args[1])return pending;
   const write=++coldStarts===1?c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:`(async()=>{
    const config=await GM.getValue('fullLocalUserConfig');
    await GM.setValue('fullLocalUserConfig',{...config,translationService:config.translationService==='bing'?'google':'bing'});return true;
   })()`}]):metadataWrite();
   return Promise.all([pending,write]).then(([context])=>context);
  };
  try{
   const recovered=await translator.acquireWorker(id,'zh-CN','plugin',()=>owner!==null);
   assert.equal(coldStarts,2,'a revoked startup is retried once');
   assert.equal((await translator.command(recovered.value,{kind:'status'})).ready,true,'the replacement must be the actual ready synthetic adapter renderer');
   recovered.release(false);
  }finally{translator.create=originalCreate}
  console.log('CHECK immersive cold-invalidation recovery after real GM settings and metadata writes',JSON.stringify(translator.diagnostics()));

  assert.equal(await c.win.webContents.executeJavaScriptInIsolatedWorld(1009,[{code:"GM.getValue('fullLocalUserConfig').then(c=>c.enableInputTranslation)"}]),false);
  owner=null;translator.reset();assert.equal(translator.windows.size,0);
  console.log('PASS optional translation absence plus SYNTHETIC adapter startup, VISIBLE native settings and CSS, concurrent settings, warm reuse, cold invalidation recovery, isolated session, disabled input translation and logout cleanup; vendor execution NOT RUN');
 }catch(error){console.error('CHECK immersive failure details',JSON.stringify(translator.diagnostics()));throw error}
 finally{translator.stop();ses.webRequest.onBeforeRequest(null)}
};
