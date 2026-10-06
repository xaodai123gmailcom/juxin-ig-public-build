/* R6.3 native desktop absence guard. Run with Electron after build:electron.
 * Windows release gate: JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE=1
 * All account documents are served by an in-process Session protocol handler.
 * Lifecycle state is produced by the production host, never inserted by tests.
 * Core owns account authorization, historical DB cleanup and operation leases;
 * the separate installed-Core gate proves those boundaries. This fixture does
 * not claim that a desktop absence acknowledgement authenticates an owner.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {createHash} = require('node:crypto');
const {rendererFixtureRead, waitRendererFixture} = require('./renderer-fixture.cjs');

const OWNER = '11111111-1111-4111-8111-111111111111';
const OTHER_OWNER = '22222222-2222-4222-8222-222222222222';
const ORIGIN = 'https://www.instagram.com';
const SCENARIOS = Object.freeze([
  'absentAcknowledgement', 'unpublishedOpeningRefused', 'openProfileRefused',
  'ownerMismatchOpenRefused', 'malformedIdentityRefused',
  'retiringProfileRefused', 'inventoryInvisibleRetirementRefused',
  'pendingCdpCleanupRefused', 'resetInProgressRefused',
  'nativeCloseFailureRetained', 'cookieFlushFailureRetained',
  'storageFlushFailureRetained', 'transportDisposeFailureRetained',
  'unauthenticatedProviderRefused', 'unknownProviderMethodRefused',
  'providerUnavailableRefused', 'normalOpenAfterAbsence', 'surfaceGrantEnforced',
]);

function fixtureHtml() {
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'">
  <title>OFFLINE R6.3 cleanup fixture</title><style>html,body{margin:0;background:#14253a;color:white;font:20px sans-serif}main{padding:30px}</style>
  <main><h1>Offline cleanup recovery</h1><p>Synthetic local document. No accounts or publishing.</p><button disabled>Publish disabled</button></main>`;
}

async function installOfflineSession(session, record) {
  session.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']}, (details, callback) => {
    let allowed = false;
    try { const url = new URL(details.url); allowed = url.protocol === 'https:' && url.origin === ORIGIN; } catch {}
    if (!allowed) record.external_requests.push(details.url);
    callback({cancel:!allowed});
  });
  await session.protocol.handle('https', request => {
    const url = new URL(request.url);
    if (url.origin !== ORIGIN || request.method !== 'GET') {
      record.external_requests.push(request.url);
      return new Response('Offline fixture rejects this request', {status:403});
    }
    record.requests.push(url.pathname);
    return new Response(fixtureHtml(), {headers:{'content-type':'text/html; charset=utf-8'}});
  });
  await session.protocol.handle('http', () => new Response('Offline fixture only', {status:403}));
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

async function waitFor(check, label, timeoutMs=15000) {
  const start = performance.now();
  while (!check()) {
    if (performance.now()-start >= timeoutMs) throw new Error('Native cleanup fixture timed out: '+label);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

// Faults and scheduling gates wrap real provider calls. Restoring the original
// property descriptor also removes a shadow property on prototype methods.
function replaceMethod(target, key, replacement) {
  const descriptor = Object.getOwnPropertyDescriptor(target, key);
  const original = target[key];
  assert.equal(typeof original, 'function', key+' provider exists');
  Object.defineProperty(target, key, {configurable:true,writable:true,value:replacement(original.bind(target))});
  assert.notEqual(target[key], original, key+' provider gate installed');
  let restored = false;
  return () => {
    if (restored) return;
    restored = true;
    if (descriptor) Object.defineProperty(target, key, descriptor); else delete target[key];
    assert.equal(target[key], original, key+' provider restored');
  };
}

function rpc(host, method, body={}, {token=host.token, timeoutMs=15000}={}) {
  const endpoint = new URL('/rpc', host.url);
  assert.equal(endpoint.hostname, '127.0.0.1', 'fixture RPC stays on its owned loopback host');
  return new Promise((resolve, reject) => {
    const request = http.request(endpoint, {method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json'}}, response => {
      let data='';
      response.on('data', chunk => { data += chunk; });
      response.once('error', reject);
      response.once('end', () => {
        try { resolve({status:response.statusCode,body:JSON.parse(data)}); } catch (error) { reject(error); }
      });
    });
    request.setTimeout(timeoutMs, () => request.destroy(new Error('Native cleanup RPC timed out')));
    request.once('error', reject);
    request.end(JSON.stringify({method,...body}));
  });
}

function assertAbsent(response, profile, owner=OWNER) {
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, {closed:true,profile_id:profile,owner_user_id:owner,verification:'desktop-absence-v1'});
  return response.body;
}

function assertRefused(response, pattern=/尚未完全关闭/) {
  assert.equal(response.status, 409, 'unsafe state must refuse with HTTP 409');
  assert.equal(typeof response.body.error, 'string');
  assert.match(response.body.error, pattern);
  assert.notEqual(response.body.closed, true);
  assert.equal(response.body.verification, undefined, 'refusal must not contain positive evidence');
  return response.body.error;
}

function stateOf(host, id) {
  const profile = host.profiles.get(id);
  return {
    published:host.profiles.has(id), closed:profile?.closed ?? null,
    opening:host.opening.has(id), retiring:host.retiringProfiles.has(id), resetting:host.resetting.has(id),
    clients:profile?.clients.size ?? 0, pages:profile?.pages.size ?? 0,
    live_pages:profile ? [...profile.pages.values()].filter(p => !p.view.webContents.isDestroyed()).length : 0,
  };
}

function selectedNativePage(profile) {
  assert.ok(profile&&!profile.closed,'ordinary open retains a live native profile');
  const page=profile.pages.get(profile.selected);
  assert.ok(page,'ordinary open selects a published native page');
  assert.ok(page.view.webContents&&!page.view.webContents.isDestroyed(),'selected native page is live');
  return page;
}

function diagnosticUrl(value) {
  if(!value||value==='about:blank')return value||'';
  try{const url=new URL(value);return url.origin===ORIGIN?url.origin+url.pathname:'[other origin] '+url.origin;}catch{return '[invalid URL]';}
}

function observeReopenNavigation(app,accountSession,events) {
  const listeners=[];
  const created=(_event,wc) => {
    if(wc.session!==accountSession)return;
    const add=(kind,details={}) => {events.push({kind,contents_id:wc.id,...details});if(events.length>40)events.shift();};
    const on=(name,listener) => {wc.on(name,listener);listeners.push(() => wc.removeListener(name,listener));};
    add('created');
    on('did-start-navigation',(_event,url,inPlace,main) => add('navigation-start',{url:diagnosticUrl(url),main,in_place:inPlace}));
    on('did-navigate',(_event,url,status) => add('navigation-commit',{url:diagnosticUrl(url),status}));
    on('did-fail-load',(_event,code,_description,url,main) => add('navigation-failed',{url:diagnosticUrl(url),main,code}));
    on('did-fail-provisional-load',(_event,code,_description,url,main) => add('provisional-navigation-failed',{url:diagnosticUrl(url),main,code}));
    on('dom-ready',() => add('dom-ready',{url:diagnosticUrl(wc.getURL())}));
    on('render-process-gone',(_event,details) => add('renderer-gone',{reason:details.reason,exit_code:details.exitCode}));
  };
  app.on('web-contents-created',created);
  return () => {app.removeListener('web-contents-created',created);for(const remove of listeners)remove();};
}

async function collectReopenDiagnostics({host,id,accountSession,phase,events,records,read=rendererFixtureRead}) {
  const profile=host.profiles.get(id);
  const evidence={phase,profile_id:id,state:stateOf(host,id),selected_target:profile?.selected||null,
    session_matches:profile?profile.session===accountSession:null,navigation_events:events.slice(-40),
    offline_sessions:records.map(record => ({...record,requests:[...record.requests],external_requests:[...record.external_requests]})),pages:[]};
  // Read only the synthetic document's state. A renderer diagnostic has its
  // own short bound and never navigates, opens a page, or extends the test wait.
  evidence.pages=await Promise.all([...(profile?.pages.values()||[])].map(async page => {
    const row={target:page.targetId,selected:page.targetId===profile.selected};let wc;
    try{
      wc=page.view.webContents;Object.assign(row,{destroyed:!wc||wc.isDestroyed(),bounds:page.view.getBounds()});
      if(!row.destroyed)Object.assign(row,{contents_id:wc.id,url:diagnosticUrl(wc.getURL()),title:wc.getTitle(),loading:wc.isLoading(),main_frame_loading:wc.isLoadingMainFrame(),host_page_ready:host.pageReady.has(wc),host_display:host.pageDisplay.get(wc)||null,session_matches:wc.session===accountSession});
    }catch(error){row.native_error=String(error);return row;}
    if(row.destroyed)return row;
    try{row.renderer=await read(wc,"({url:location.href,title:document.title,readyState:document.readyState,visibility:document.visibilityState,width:innerWidth,height:innerHeight,publishDisabled:document.querySelector('button')?.disabled??null})",{label:'failed native reopen state',timeoutMs:1500});row.renderer.url=diagnosticUrl(row.renderer.url);}catch(error){row.renderer_error=String(error);}
    return row;
  }));
  let timer;
  try{evidence.protocol=await Promise.race([Promise.all(['http','https'].map(async scheme => [scheme,await accountSession.protocol.isProtocolHandled(scheme)])).then(Object.fromEntries),new Promise((_,reject) => {timer=setTimeout(() => reject(new Error('protocol diagnostic timed out')),1000);})]);}
  catch(error){evidence.protocol_error=String(error);}finally{clearTimeout(timer);}
  return evidence;
}

async function retryRetainedProfile(host, profile, request=rpc) {
  assert.equal(profile.closed,true,'retry is for a failed retained closure');
  assert.equal(host.profiles.get(profile.id),profile,'failed closure remains owned');
  // The UI-facing close RPC accepts only open profiles. A retained closed
  // profile must stay refused there and by confirm-closed. The actual retry
  // primitive is the same lifecycle teardown called by ensure(existing.closed)
  // and stop(); invoking it here does not claim another UI Close can recover it.
  const publicClose=await request(host,'close',{profile:profile.id,generation:profile.generation});
  assertRefused(publicClose,/内置窗口尚未打开/);
  assertRefused(await request(host,'confirm-closed',{profile:profile.id,owner:OWNER}));
  await host.closeProfile(profile);
  assert.equal(host.profiles.has(profile.id),false,'real teardown removes the retained owner');
  assert.equal(host.closing.has(profile),false);assert.equal(host.retiringProfiles.has(profile.id),false);
  const acknowledgement=assertAbsent(await request(host,'confirm-closed',{profile:profile.id,owner:OWNER}),profile.id);
  return {retry_path:'EmbeddedBrowserHost.closeProfile (ensure/stop lifecycle)',public_close_refused:true,acknowledgement};
}

function observeOperation(promise) {
  // Attach rejection handling immediately, including while a native gate is
  // held, so a deliberate injected failure cannot become unhandledRejection.
  return promise.then(value => ({ok:true,value}), error => ({ok:false,error}));
}

async function requireOperation(observed) {
  const outcome = await observed;
  if (!outcome.ok) throw outcome.error;
  return outcome.value;
}

async function connectTask(host, profile, sockets) {
  const {WebSocket} = require('ws');
  const socket = new WebSocket(host.endpoint(profile));
  sockets.add(socket);
  await new Promise((resolve,reject) => {
    const timer=setTimeout(() => {socket.terminate();reject(new Error('Native task socket timed out'));},15000);
    socket.once('open',() => {clearTimeout(timer);resolve();});
    socket.once('error',error => {clearTimeout(timer);reject(error);});
  });
  await waitFor(() => profile.clients.size===1, 'actual AccountCdpConnection registration');
  let sequence=0;
  const command=(method,params={}) => new Promise((resolve,reject) => {
    const id=++sequence;
    const done=raw => {
      const message=JSON.parse(raw.toString());
      if(message.id!==id)return;
      cleanup();
      if(message.error)reject(new Error(message.error.message));else resolve(message.result);
    };
    const closed=() => {cleanup();reject(new Error('Native task socket closed'));};
    const timer=setTimeout(() => {cleanup();reject(new Error('Native task command timed out'));},15000);
    const cleanup=() => {clearTimeout(timer);socket.off('message',done);socket.off('close',closed);socket.off('error',closed);};
    socket.on('message',done);socket.once('close',closed);socket.once('error',closed);
    socket.send(JSON.stringify({id,method,params}));
  });
  assert.match((await command('Browser.getVersion')).product, /^Chrome\//);
  return {socket,command,client:[...profile.clients][0]};
}

async function run({host,win,session,outputDirectory,proof}) {
  const sockets=new Set(), restores=new Set(), gates=new Set(), records=[];
  const reopenEvents=[];let reopenedId,reopenedSession,reopenPhase,disposeReopenObserver=() => {};
  const mark=(scenario, evidence={}) => {
    proof.scenarios[scenario]=true;proof.observations.push({scenario,...evidence});
    console.log('PASS native cleanup '+scenario);
  };
  const patch=(target,key,replacement) => {
    const restore=replaceMethod(target,key,replacement);
    restores.add(restore);
    return () => {restore();restores.delete(restore);};
  };
  const gate=() => {const item=deferred();gates.add(item);return item;};
  const confirm=id => rpc(host,'confirm-closed',{profile:id,owner:OWNER});
  const success=async(method,body) => {
    const result=await rpc(host,method,body);assert.equal(result.status,200,JSON.stringify(result));return result.body;
  };
  const close=profile => success('close',{profile:profile.id,generation:profile.generation});
  const inventory=async() => (await success('inventory',{})).profiles;
  const prepare=async letter => {
    const id=`native:${letter.repeat(8)}-${letter.repeat(4)}-4${letter.repeat(3)}-8${letter.repeat(3)}-${letter.repeat(12)}`;
    const accountSession=session.fromPartition(`persist:account-${OWNER}-${id.slice(7)}`);
    const record={profile:id,requests:[],external_requests:[]};records.push(record);
    await installOfflineSession(accountSession,record);
    return {id,session:accountSession};
  };
  const open=async account => {
    const result=await success('ensure',{profile:account.id,owner:OWNER,proxy:'',open:true,name:'OFFLINE R6.3 CLEANUP'});
    const profile=host.profiles.get(account.id);
    assert.ok(profile);assert.equal(profile.generation,result.generation);
    assert.equal(profile.session,account.session);
    assert.equal(profile.pages.size,1);
    assert.equal([...profile.pages.values()][0].view.webContents.isDestroyed(),false);
    return profile;
  };
  const heldRefusal=async(id,scenario) => {
    const state=stateOf(host,id),response=await confirm(id);
    const error=assertRefused(response);mark(scenario,{state,error});
  };
  try {
    const account=await prepare('a');
    mark('absentAcknowledgement',{acknowledgement:assertAbsent(await confirm(account.id),account.id),state:stateOf(host,account.id)});
    const blocked=await rpc(host,'confirm-closed',{profile:account.id,owner:OWNER},{token:'synthetic-invalid-token'});
    assert.equal(blocked.status,403);assert.deepEqual(blocked.body,{});mark('unauthenticatedProviderRefused');
    assertRefused(await rpc(host,'nonexistent-cleanup-method',{profile:account.id,owner:OWNER}),/尚未打开/);
    mark('unknownProviderMethodRefused');
    for(const body of [{profile:'unknown-profile',owner:OWNER},{profile:account.id,owner:'unknown-owner'}]){
      assertRefused(await rpc(host,'confirm-closed',body),/标识无效/);
    }
    mark('malformedIdentityRefused');

    // setProxy is awaited before the profile is published. Leave the real
    // opening promise pending while checking over the actual RPC server.
    const openingGate=gate();let openingEntered=false;
    const restoreProxy=patch(account.session,'setProxy',original => async(...args) => {
      openingEntered=true;await openingGate.promise;return original(...args);
    });
    const opening=observeOperation(open(account));
    await waitFor(() => openingEntered&&host.opening.has(account.id),'unpublished opening');
    assert.equal(host.profiles.has(account.id),false);
    await heldRefusal(account.id,'unpublishedOpeningRefused');
    restoreProxy();openingGate.resolve();
    const profile=await requireOperation(opening);
    await heldRefusal(account.id,'openProfileRefused');
    assertRefused(await rpc(host,'open-instagram',{profile:profile.id,owner:OTHER_OWNER,proxy:''}),/不属于当前用户/);
    assertRefused(await rpc(host,'confirm-closed',{profile:profile.id,owner:OTHER_OWNER}));
    assert.equal(profile.owner,OWNER);mark('ownerMismatchOpenRefused',{scope:'existing desktop profile; absent-profile ownership is checked by Core'});

    // Real Storage.getCookies crosses the WebSocket transport and enters the
    // production AccountCdpConnection pending set before disconnect/close.
    const task=await connectTask(host,profile,sockets),cookieGate=gate();let cookieEntered=false;
    const restoreGet=patch(profile.session.cookies,'get',original => async(...args) => {
      cookieEntered=true;await cookieGate.promise;return original(...args);
    });
    const command=observeOperation(task.command('Storage.getCookies'));
    await waitFor(() => cookieEntered&&task.client.pending.size>0,'real in-flight CDP cookie read');
    task.socket.close();
    await waitFor(() => task.client.disposed,'real task disconnect cleanup');
    assert.equal(profile.clients.size,1,'client retained while operation is pending');
    assert.ok(task.client.pending.size>0);
    await heldRefusal(profile.id,'pendingCdpCleanupRefused');
    const closing=observeOperation(close(profile));
    await waitFor(() => profile.closed&&host.retiringProfiles.has(profile.id),'native retirement in progress');
    await waitFor(() => [...profile.pages.values()].every(page => page.view.webContents.isDestroyed())&&task.client.pending.size>0,'native pages destroyed while CDP work is still pending');
    assert.equal((await inventory()).some(p => p.id===profile.id),false,'closed profile omitted from inventory');
    await heldRefusal(profile.id,'retiringProfileRefused');

    // The real close function deletes the profile before its outer finally
    // releases retiringProfiles. Observe that exact microtask window without
    // inserting/deleting any lifecycle registry entries in this test.
    let retirementObservation;
    const retirementSeen=deferred();
    const restoreStorage=patch(profile.session,'flushStorageData',original => (...args) => {
      const value=original(...args);
      queueMicrotask(() => {
        const state=stateOf(host,profile.id);
        retirementObservation=observeOperation(host.control('confirm-closed',{profile:profile.id,owner:OWNER}));
        retirementSeen.resolve(state);
      });
      return value;
    });
    restoreGet();cookieGate.resolve();
    await requireOperation(closing);await command;
    const retirementState=await retirementSeen.promise,retirementResult=await retirementObservation;
    assert.equal(retirementState.published,false);assert.equal(retirementState.retiring,true);
    assert.equal(retirementResult.ok,false);assert.match(String(retirementResult.error),/尚未完全关闭/);
    restoreStorage();mark('inventoryInvisibleRetirementRefused',{state:retirementState,transport:'production control at real retirement microtask'});
    assertAbsent(await confirm(profile.id),profile.id);
    assert.equal(task.client.pending.size,0);assert.equal(profile.clients.size,0);

    // Fail individual real close providers. A closed flag and empty inventory
    // must never suffice; only an explicit successful retry can clear state.
    const failClose=async(letter,name,targetOf,key,afterOriginal=false) => {
      const candidate=await prepare(letter),p=await open(candidate);
      const target=targetOf(p);
      const restore=patch(target,key,original => afterOriginal
        ? async(...args) => {await original(...args);throw new Error('fixture '+name);}
        : () => {throw new Error('fixture '+name);});
      const response=await rpc(host,'close',{profile:p.id,generation:p.generation});
      assertRefused(response,new RegExp('fixture '+name));
      assert.equal(p.closed,true);assert.equal(host.profiles.get(p.id),p);
      assert.equal((await inventory()).some(row => row.id===p.id),false);
      const state=stateOf(host,p.id),refusal=assertRefused(await confirm(p.id));
      restore();const retry=await retryRetainedProfile(host,p);
      mark(name,{state,error:refusal,retry_closed:true,...retry});
    };
    await failClose('b','nativeCloseFailureRetained',p => [...p.pages.values()][0].view.webContents,'close');
    await failClose('c','cookieFlushFailureRetained',p => p.session.cookies,'flushStore');
    await failClose('d','storageFlushFailureRetained',p => p.session,'flushStorageData');

    const clientAccount=await prepare('e'),clientProfile=await open(clientAccount);
    const disposing=await connectTask(host,clientProfile,sockets);
    // Inject a failure after actual disposal; both transport invocation paths
    // see a handled rejection, while the production owner remains retained.
    const restoreDispose=patch(disposing.client,'dispose',original => async(...args) => {
      await original(...args);throw new Error('fixture transportDisposeFailureRetained');
    });
    const restoreClientClose=patch(disposing.client,'close',() => () => {disposing.socket.close();});
    assertRefused(await rpc(host,'close',{profile:clientProfile.id,generation:clientProfile.generation}),/fixture transportDisposeFailureRetained/);
    assert.equal(host.profiles.get(clientProfile.id),clientProfile);
    const clientState=stateOf(host,clientProfile.id);assertRefused(await confirm(clientProfile.id));
    restoreDispose();restoreClientClose();const clientRetry=await retryRetainedProfile(host,clientProfile);
    mark('transportDisposeFailureRetained',{state:clientState,retry_closed:true,...clientRetry});

    // A reset with no existing native profile is synchronous. The production
    // unread invalidation provider observes resetting before finally clears it;
    // call the exact control API there, with no fabricated reset-set entry.
    const resetAccount=await prepare('f');let resetObservation,resetState;
    const restoreUnread=patch(host.unread,'invalidate',original => (...args) => {
      if(args[0]===resetAccount.id){
        resetState=stateOf(host,resetAccount.id);
        resetObservation=observeOperation(host.control('confirm-closed',{profile:resetAccount.id,owner:OWNER}));
      }
      return original(...args);
    });
    assert.equal((await success('reset-whatsapp-storage',{profile:resetAccount.id,owner:OWNER})).reset,true);
    restoreUnread();assert.ok(resetObservation,'real reset provider reached');
    const resetResult=await resetObservation;
    assert.equal(resetState.published,false);assert.equal(resetState.resetting,true);
    assert.equal(resetResult.ok,false);assert.match(String(resetResult.error),/尚未完全关闭/);
    assertAbsent(await confirm(resetAccount.id),resetAccount.id);
    mark('resetInProgressRefused',{state:resetState,transport:'production control during synchronous reset provider'});

    // Desktop half of recovery: after positive absence, use ordinary open
    // and surface grant admission. The installed-Core gate covers clearing the
    // historical row and obtaining its account operation lease beforehand.
    const generation=profile.generation,endpoint=host.endpoint(profile);
    reopenedId=account.id;reopenedSession=account.session;reopenPhase='ordinary-open';
    disposeReopenObserver=observeReopenNavigation(require('electron').app,account.session,reopenEvents);
    const result=await success('open-instagram',{profile:account.id,owner:OWNER,proxy:''});
    assert.equal(result.opened,true);assert.ok(result.generation>generation);assert.notEqual(result.ws,endpoint);
    const reopened=host.profiles.get(account.id),page=selectedNativePage(reopened);
    // newPage starts about:blank without awaiting its navigation. If its URL
    // is still empty, open-instagram legitimately creates/selects another page.
    // Observe the command's selected target, never Map insertion order.
    proof.reopen_selection={selected_target:reopened.selected,observed_target:page.targetId,page_count:reopened.pages.size,generation:reopened.generation};
    reopenPhase='selected-document';
    await waitRendererFixture(page.view.webContents,"document.title==='OFFLINE R6.3 cleanup fixture'&&document.readyState==='complete'",{label:'ordinary reopened native document',timeoutMs:15000});
    reopenPhase='surface-admission';
    const bounds={x:100,y:90,width:900,height:700};host.setWorkspaceBounds(bounds);
    assert.equal((await success('show',{profile:reopened.id,target:page.targetId,bounds,grant:'invalid-fixture-grant'})).attached,false);
    const admitted=await success('show',{profile:reopened.id,target:page.targetId,bounds,grant:host.requestSurface(),read_only:false});
    assert.equal(admitted.attached,true);
    const pane=win.contentView.children.find(view => view.children.includes(page.view));
    assert.ok(pane);assert.equal(pane.getVisible(),true);assert.deepEqual(pane.getBounds(),bounds);
    reopenPhase='selected-viewport';
    await waitRendererFixture(page.view.webContents,'innerWidth===900&&innerHeight===700',{label:'ordinary reopened native viewport',timeoutMs:15000});
    const dom=await rendererFixtureRead(page.view.webContents,"({title:document.title,url:location.href,width:innerWidth,height:innerHeight,publishDisabled:document.querySelector('button').disabled})");
    assert.equal(dom.title,'OFFLINE R6.3 cleanup fixture');assert.equal(dom.url,ORIGIN+'/');assert.equal(dom.publishDisabled,true);
    assert.equal(dom.width,900);assert.equal(dom.height,700);
    assert.deepEqual(page.view.getBounds(),{x:0,y:0,width:900,height:700});
    // Save the native child page itself after real visible renderer frames.
    // The shared helper bounds UnknownVizError retries and renews the normal
    // production surface lease; it never adjusts the observed viewport.
    reopenPhase='foreground';
    await require('./visible-fixture.cjs').prepareVisibleFixture(win,{label:'R6.3 reopened native page',timeoutMs:15000});
    reopenPhase='native-page-capture';
    proof.screenshot_capture={};
    const image=await require('./posting-viewport-native-r62.cjs').capturePaintedPage({
      win,pane,page,bounds,evidence:proof.screenshot_capture,
      showSurface:async() => {
        const shown=await success('show',{profile:reopened.id,target:page.targetId,bounds,grant:host.requestSurface(),read_only:false});
        assert.equal(shown.attached,true,'reopened native page remains admitted during capture');
      },
    });
    assert.equal(image.isEmpty(),false,'reopened native page capture has pixels');
    assert.equal(await rendererFixtureRead(page.view.webContents,"document.visibilityState==='visible'&&document.querySelector('button').disabled"),true,'captured native page is visible with Publish disabled');
    const png=image.toPNG();
    assert.ok(png.length>33);assert.deepEqual(png.subarray(0,8),Buffer.from([137,80,78,71,13,10,26,10]));
    assert.equal(png.toString('ascii',12,16),'IHDR');
    const pixel_size={width:png.readUInt32BE(16),height:png.readUInt32BE(20)};
    assert.ok(pixel_size.width>0&&pixel_size.height>0);
    const decoded=require('electron').nativeImage.createFromBuffer(png);
    assert.equal(decoded.isEmpty(),false,'written PNG decodes to a valid native image');
    assert.deepEqual(decoded.getSize(),pixel_size,'PNG dimensions match decoded pixels');
    const screenshotFile='r63-nurture-cleanup-native.png';
    fs.writeFileSync(path.join(outputDirectory,screenshotFile),png);
    proof.screenshot={file:screenshotFile,pixel_size,bytes:png.length,sha256:createHash('sha256').update(png).digest('hex'),viewport:{width:900,height:700},publish_disabled:true};
    await heldRefusal(reopened.id,'openProfileRefused');
    mark('surfaceGrantEnforced',{bounds,native:page.view.getBounds(),dom});
    mark('normalOpenAfterAbsence',{generation:reopened.generation,previous_generation:generation,scope:'ordinary desktop open and surface; historical cleanup/Core lease proven separately'});
    reopenPhase='reopened-profile-close';
    await close(reopened);assertAbsent(await confirm(reopened.id),reopened.id);
    for(const record of records)assert.deepEqual(record.external_requests,[],'no external account request attempted');
    assert.ok(records.some(record => record.requests.includes('/')),'actual ordinary open document served offline');
    proof.offline_sessions=records;
  } catch(error) {
    if(reopenedId){
      try{proof.reopen_diagnostics=await collectReopenDiagnostics({host,id:reopenedId,accountSession:reopenedSession,phase:reopenPhase,events:reopenEvents,records});}
      catch(diagnosticError){proof.reopen_diagnostics={phase:reopenPhase,error:String(diagnosticError)};}
    }
    throw error;
  } finally {
    disposeReopenObserver();
    for(const restore of [...restores].reverse())restore();
    for(const item of gates)item.resolve();
    for(const socket of sockets)socket.terminate();
  }
}

function assertProofComplete(proof) {
  assert.equal(proof.schema,1);assert.equal(proof.gate,'r63-nurture-cleanup-native');
  assert.equal(proof.synthetic_offline,true);assert.deepEqual(proof.external_actions,[]);
  assert.equal(proof.cleanup_verified,true);
  assert.equal(proof.native_runtime,true);assert.ok(proof.electron);assert.ok(proof.chromium);
  if(proof.required_mode){
    assert.equal(proof.platform,'win32','required release proof must run on Windows');
    require('../../scripts/local_source_binding.cjs').assertSourceIdentity(proof,{root:path.resolve(__dirname,'../..'),required:true});
  }
  assert.deepEqual(Object.keys(proof.scenarios).sort(),[...SCENARIOS].sort(),'native scenario contract has no missing or unexpected keys');
  for(const name of SCENARIOS)assert.equal(proof.scenarios[name],true,'missing native scenario: '+name);
  assert.ok(proof.offline_sessions.length>0);
  for(const record of proof.offline_sessions)assert.deepEqual(record.external_requests,[]);
  for(const name of ['host_source','host_compiled','fixture'])assert.match(proof.sha256[name],/^[0-9a-f]{64}$/);
  assert.equal(proof.screenshot_capture.completed,true);assert.ok(proof.screenshot_capture.attempts.length>0);
  assert.equal(proof.screenshot.file,'r63-nurture-cleanup-native.png');
  assert.equal(proof.screenshot.publish_disabled,true);
  assert.deepEqual(proof.screenshot.viewport,{width:900,height:700});
  assert.ok(Number.isInteger(proof.screenshot.bytes)&&proof.screenshot.bytes>33);
  for(const dimension of ['width','height'])assert.ok(Number.isInteger(proof.screenshot.pixel_size[dimension])&&proof.screenshot.pixel_size[dimension]>0);
  assert.match(proof.screenshot.sha256,/^[0-9a-f]{64}$/);
}

async function main() {
  console.log('CHECK native nurture cleanup standalone entry reached');
  const root=path.resolve(__dirname,'../..'),outputDirectory=path.join(root,'installer-output');
  fs.mkdirSync(outputDirectory,{recursive:true});
  const resultPath=path.join(outputDirectory,'r63-nurture-cleanup-native.json');
  const failurePath=path.join(outputDirectory,'r63-nurture-cleanup-native.failure.json');
  for(const file of [resultPath,failurePath,path.join(outputDirectory,'r63-nurture-cleanup-native.png')])fs.rmSync(file,{force:true});
  const proof={schema:1,gate:'r63-nurture-cleanup-native',verified:false,required_mode:process.env.JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE==='1',synthetic_offline:true,external_actions:[],native_runtime:false,platform:process.platform,electron:process.versions.electron||null,chromium:process.versions.chrome||null,scenarios:{},observations:[],offline_sessions:[],scope:{desktop_native:true,installed_core_history:false,absent_profile_owner_authorization:false,account_operation_lease:false}};
  let app,win,host,watchdog,exiting=false;
  const fail=async error => {
    if(exiting)return;exiting=true;watchdog?.dispose();
    proof.verified=false;
    console.error('FAIL native nurture cleanup',error?.stack||error);
    fs.writeFileSync(failurePath,JSON.stringify({...proof,error:String(error?.stack||error),checked_at:new Date().toISOString()},null,2));
    if(!app){process.exitCode=1;return;}
    const timer=setTimeout(() => app.exit(1),5000);
    try{await host?.stop();}catch(cleanupError){console.error('FAIL owned native fixture cleanup',String(cleanupError));}
    clearTimeout(timer);app.exit(1);
  };
  try {
    assert.ok(process.versions.electron,'native proof requires Electron, not node');
    ({app}=require('electron'));
    const {BrowserWindow,session}=require('electron');
    const {createIntegrationWatchdog}=require('./integration-watchdog.cjs');
    watchdog=createIntegrationWatchdog({overallMs:180000,onTimeout:details => void fail(new Error(JSON.stringify(details)))});
    watchdog.begin('nurture-cleanup-native-r63',150000);
    process.on('unhandledRejection',error => void fail(error));
    process.on('uncaughtException',error => void fail(error));
    if(proof.required_mode)assert.equal(process.platform,'win32','required R6.3 native release proof requires Windows');
    const temp=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'juxin-nurture-cleanup-r63-'));
    app.setPath('userData',temp);app.setName('聚鑫国际');
    app.commandLine.appendSwitch('disable-background-networking');
    app.on('window-all-closed',() => {});
    Object.assign(proof,require('../../scripts/local_source_binding.cjs').collectSourceIdentity(root));
    if(process.env.GITHUB_SHA)assert.equal(proof.source_commit,process.env.GITHUB_SHA,'native proof matches the CI checkout');
    proof.sha256={};
    for(const [name,file] of Object.entries({host_source:'desktop/src/embedded-browser.ts',host_compiled:'dist-electron/embedded-browser.js',fixture:'desktop/tests/nurture-cleanup-native-r63.cjs'}))proof.sha256[name]=createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex');
    await app.whenReady();
    session.defaultSession.webRequest.onBeforeRequest({urls:['http://*/*','https://*/*','ws://*/*','wss://*/*']},(_details,callback) => callback({cancel:true}));
    win=new BrowserWindow({show:true,width:1200,height:900,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
    win.setMinimumSize(1200,900);win.setContentSize(1200,900);
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(fixtureHtml()));
    const size=win.getContentSize();assert.ok(size[0]>=1200&&size[1]>=900,'real native shell client area');
    const {EmbeddedBrowserHost}=await import(require('node:url').pathToFileURL(path.join(root,'dist-electron/embedded-browser.js')).href);
    host=new EmbeddedBrowserHost(() => win);await host.start();host.attachWindow(win);proof.native_runtime=true;
    await run({host,win,session,outputDirectory,proof});
    const stoppedUrl=host.url;
    await host.stop();assert.equal(host.profiles.size,0);assert.equal(host.opening.size,0);assert.equal(host.retiringProfiles.size,0);assert.equal(host.resetting.size,0);
    await assert.rejects(rpc({url:stoppedUrl,token:host.token},'confirm-closed',{profile:'native:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',owner:OWNER}),/ECONNREFUSED|ECONNRESET|socket hang up/);
    proof.scenarios.providerUnavailableRefused=true;proof.observations.push({scenario:'providerUnavailableRefused',transport:'real stopped loopback provider'});
    proof.cleanup_verified=true;proof.timing=watchdog.finish();assertProofComplete(proof);
    proof.verified=true;proof.checked_at=new Date().toISOString();
    fs.writeFileSync(resultPath,JSON.stringify(proof,null,2));
    console.log('PASS native nurture cleanup proof:',resultPath);
    exiting=true;win.destroy();app.exit(0);
  } catch(error) {await fail(error);}
}

function isStandaloneEntry({isMain=require.main===module,electronVersion=process.versions.electron,entry=process.argv[1]}={}) {
  return isMain||Boolean(electronVersion&&entry&&path.resolve(entry)===__filename);
}
module.exports={SCENARIOS,fixtureHtml,installOfflineSession,replaceMethod,rpc,assertAbsent,assertRefused,selectedNativePage,diagnosticUrl,observeReopenNavigation,collectReopenDiagnostics,retryRetainedProfile,assertProofComplete,isStandaloneEntry,run};
if(isStandaloneEntry())void main();
