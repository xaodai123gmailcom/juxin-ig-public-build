/* Fixture safety/runner contracts only. These tests never count as native proof. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const {runInNewContext}=require('node:vm');
const fixture=require('./nurture-cleanup-native-r63.cjs');
const filename=require.resolve('./nurture-cleanup-native-r63.cjs');
const source=fs.readFileSync(filename,'utf8');

test('account fixture has no executable script, remote resource, or publish action',()=>{
  const html=fixture.fixtureHtml();
  assert.match(html,/default-src 'none'/);assert.match(html,/connect-src 'none'/);assert.match(html,/form-action 'none'/);
  assert.match(html,/<button disabled>Publish disabled/);
  assert.doesNotMatch(html,/<(?:script|img|iframe|form)\b|\bfetch\s*\(|XMLHttpRequest|WebSocket/i);
});

test('offline Session intercepts account navigation and refuses all external requests',async()=>{
  const handlers={},record={requests:[],external_requests:[]};let before;
  await fixture.installOfflineSession({webRequest:{onBeforeRequest(filter,callback){assert.deepEqual(filter.urls,['http://*/*','https://*/*','ws://*/*','wss://*/*']);before=callback;}},protocol:{async handle(scheme,callback){handlers[scheme]=callback;}}},record);
  const check=url=>new Promise(resolve=>before({url},resolve));
  assert.deepEqual(await check('https://www.instagram.com/'),{cancel:false});
  const response=await handlers.https(new Request('https://www.instagram.com/'));
  assert.equal(response.status,200);assert.match(await response.text(),/Offline cleanup recovery/);
  assert.deepEqual(record.requests,['/']);
  for(const url of ['https://example.invalid/','http://www.instagram.com/','wss://www.instagram.com/live','ws://127.0.0.1/','bad-url'])assert.deepEqual(await check(url),{cancel:true});
  for(const request of [new Request('https://example.invalid/'),new Request('https://www.instagram.com/',{method:'POST'})])assert.equal((await handlers.https(request)).status,403);
  assert.equal((await handlers.http()).status,403);assert.equal(record.external_requests.length,7);
});

test('positive evidence requires exact profile, owner, status and version',()=>{
  const id='native:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',owner='11111111-1111-4111-8111-111111111111';
  const response={status:200,body:{closed:true,profile_id:id,owner_user_id:owner,verification:'desktop-absence-v1'}};
  assert.deepEqual(fixture.assertAbsent(response,id),response.body);
  for(const bad of [{...response,status:409},{...response,body:{...response.body,closed:false}},{...response,body:{...response.body,profile_id:'native:other'}},{...response,body:{...response.body,owner_user_id:'other'}},{...response,body:{...response.body,verification:'inventory-empty'}},{...response,body:{...response.body,unverified:true}}])assert.throws(()=>fixture.assertAbsent(bad,id));
});

test('refusals cannot be a success-shaped body or an arbitrary error',()=>{
  const response={status:409,body:{error:'窗口尚未完全关闭'}};
  assert.equal(fixture.assertRefused(response),response.body.error);
  for(const bad of [{...response,status:200},{status:409,body:{}},{status:409,body:{error:'unrelated failure'}},{...response,body:{...response.body,closed:true}},{...response,body:{...response.body,verification:'desktop-absence-v1'}}])assert.throws(()=>fixture.assertRefused(bad));
});

test('deterministic provider wrappers call the real method and restore exact own/prototype descriptors',async()=>{
  let calls=0;
  const prototype={async read(value){calls++;return this.offset+value;}},provider=Object.create(prototype);provider.offset=2;
  const restore=fixture.replaceMethod(provider,'read',original=>async(...args)=>{return 10+await original(...args);});
  assert.equal(await provider.read(3),15);assert.equal(calls,1);restore();restore();
  assert.equal(Object.hasOwn(provider,'read'),false);assert.equal(await provider.read(3),5);
  Object.defineProperty(provider,'write',{value(){return this.offset;},writable:false,enumerable:false,configurable:true});
  const descriptor=Object.getOwnPropertyDescriptor(provider,'write');
  const restoreWrite=fixture.replaceMethod(provider,'write',()=>()=>{throw new Error('injected provider failure');});
  assert.throws(()=>provider.write(),/injected provider failure/);restoreWrite();
  assert.deepEqual(Object.getOwnPropertyDescriptor(provider,'write'),descriptor);assert.equal(provider.write(),2);
});

function completeProof(){
  return {schema:1,gate:'r63-nurture-cleanup-native',synthetic_offline:true,external_actions:[],cleanup_verified:true,native_runtime:true,electron:'44.4.5',chromium:'test',required_mode:true,platform:'win32',source_commit:'a'.repeat(40),github_sha:'a'.repeat(40),source_provenance:{schema:2,mode:'flat-git-ci',representation:'public-sanitized-source',repository:{full_name:'xaodai123gmailcom/juxin-ig-public-build',id:'1406784621',owner_id:'337452708'},source_commit:'a'.repeat(40),execution:'github-actions',git_checkout:true,github_actions:true,unit_test_authority:'native-contract-only'},scenarios:Object.fromEntries(fixture.SCENARIOS.map(name=>[name,true])),offline_sessions:[{profile:'synthetic',requests:['/'],external_requests:[]}],sha256:{host_source:'a'.repeat(64),host_compiled:'b'.repeat(64),fixture:'c'.repeat(64)},screenshot_capture:{completed:true,attempts:[{renderer:{painted:true}}]},screenshot:{file:'r63-nurture-cleanup-native.png',pixel_size:{width:900,height:700},viewport:{width:900,height:700},bytes:100,sha256:'e'.repeat(64),publish_disabled:true}};
}

test('completion contract requires every native scenario and clean teardown',()=>{
  fixture.assertProofComplete(completeProof());
  assert.equal(fixture.SCENARIOS.length,18);
  for(const scenario of fixture.SCENARIOS){const proof=completeProof();delete proof.scenarios[scenario];assert.throws(()=>fixture.assertProofComplete(proof),/native scenario contract/);}
  for(const patch of [{native_runtime:false},{cleanup_verified:false},{platform:'linux'},{source_provenance:null},{electron:null},{chromium:null},{synthetic_offline:false},{external_actions:['live request']},{offline_sessions:[]},{sha256:{}}])assert.throws(()=>fixture.assertProofComplete({...completeProof(),...patch}));
  const extra=completeProof();extra.scenarios.fakeScenario=true;assert.throws(()=>fixture.assertProofComplete(extra),/native scenario contract/);
  const external=completeProof();external.offline_sessions[0].external_requests.push('https://example.invalid/');assert.throws(()=>fixture.assertProofComplete(external));
  const sourceMismatch=completeProof();sourceMismatch.sha256.host_compiled='not-a-hash';assert.throws(()=>fixture.assertProofComplete(sourceMismatch));
  for(const patch of [{file:'shell.png'},{pixel_size:{width:0,height:700}},{viewport:{width:1280,height:900}},{bytes:0},{sha256:'invalid'},{publish_disabled:false}]){
    const proof=completeProof();Object.assign(proof.screenshot,patch);assert.throws(()=>fixture.assertProofComplete(proof));
  }
  const noCapture=completeProof();noCapture.screenshot_capture.completed=false;assert.throws(()=>fixture.assertProofComplete(noCapture));
});

test('standalone guard handles Electron ESM entry without starting imports',()=>{
  const guard=source.slice(source.indexOf('function isStandaloneEntry('));
  function starts({isMain=false,electronVersion='44.4.5',entry=filename}={}){
    let calls=0;const module={exports:{}},require=Object.assign(()=>{},{main:isMain?module:{}});
    runInNewContext(guard,{module,require,path,__filename:filename,process:{versions:{electron:electronVersion},argv:['electron',entry]},main(){calls++;},SCENARIOS:[],fixtureHtml(){},installOfflineSession(){},replaceMethod(){},rpc(){},assertAbsent(){},assertRefused(){},selectedNativePage(){},diagnosticUrl(){},observeReopenNavigation(){},collectReopenDiagnostics(){},retryRetainedProfile(){},assertProofComplete(){},run(){}});
    return calls;
  }
  assert.equal(starts(),1);assert.equal(starts({isMain:true}),1);
  assert.equal(starts({entry:require.resolve('./embedded-browser.integration.cjs')}),0);
  assert.equal(starts({entry:path.join(path.dirname(filename),'other',path.basename(filename))}),0);
  assert.equal(starts({electronVersion:null}),0);assert.equal(starts({entry:null}),0);
});

test('required standalone run with node exits nonzero, removes stale success, and writes failure',{timeout:10000},()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'r63-native-fixture-contract-'));
  try{
    const tests=path.join(temp,'desktop','tests'),output=path.join(temp,'installer-output');
    fs.mkdirSync(tests,{recursive:true});fs.mkdirSync(output);
    for(const name of ['nurture-cleanup-native-r63.cjs','renderer-fixture.cjs'])fs.copyFileSync(path.join(__dirname,name),path.join(tests,name));
    const resultFile=path.join(output,'r63-nurture-cleanup-native.json');fs.writeFileSync(resultFile,'{"verified":true}');
    const screenshotFile=path.join(output,'r63-nurture-cleanup-native.png');fs.writeFileSync(screenshotFile,'stale screenshot');
    const result=spawnSync(process.execPath,[path.join(tests,'nurture-cleanup-native-r63.cjs')],{encoding:'utf8',timeout:5000,env:{...process.env,JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE:'1'}});
    assert.equal(result.error,undefined);assert.equal(result.status,1);assert.equal(result.signal,null);
    assert.match(result.stdout,/standalone entry reached/);assert.match(result.stderr,/requires Electron, not node/);
    assert.equal(fs.existsSync(resultFile),false);
    assert.equal(fs.existsSync(screenshotFile),false);
    const failure=JSON.parse(fs.readFileSync(path.join(output,'r63-nurture-cleanup-native.failure.json'),'utf8'));
    assert.equal(failure.verified,false);assert.equal(failure.required_mode,true);assert.equal(failure.native_runtime,false);assert.equal(failure.gate,'r63-nurture-cleanup-native');assert.match(failure.error,/requires Electron/);
  }finally{fs.rmSync(temp,{recursive:true,force:true});}
});

test('native proof uses real RPC, native pages and CDP cleanup without forged lifecycle state',()=>{
  assert.match(source,/http\.request\(endpoint/);assert.match(source,/new WebSocket\(host\.endpoint\(profile\)\)/);
  assert.match(source,/Storage\.getCookies/);assert.match(source,/task\.client\.pending\.size>0/);
  assert.match(source,/\.webContents\.isDestroyed\(\)/);assert.match(source,/pane\.getVisible\(\)/);
  assert.match(source,/host\.requestSurface\(\)/);assert.match(source,/rpc\(host,'confirm-closed'/);
  assert.doesNotMatch(source,/host\.(?:profiles|opening|retiringProfiles|resetting)\.(?:set|add|delete|clear)\(/);
  assert.doesNotMatch(source,/\.clients\.(?:add|delete|clear)\(|patch\(host,\s*['"]control/);
  assert.ok(source.indexOf('await host.stop();assert.equal(host.profiles.size,0)')<source.indexOf('proof.verified=true'));
  assert.ok(source.indexOf('assertProofComplete(proof);')<source.indexOf('proof.verified=true'));
  assert.match(source,/absent_profile_owner_authorization:false,account_operation_lease:false/);
  assert.match(source,/capturePaintedPage\(\{/);assert.match(source,/nativeImage\.createFromBuffer\(png\)/);
  assert.match(source,/document\.visibilityState==='visible'&&document\.querySelector\('button'\)\.disabled/);
  assert.ok(source.indexOf('proof.screenshot={')<source.indexOf('await close(reopened)'));
  assert.doesNotMatch(source,/win\.webContents\.capturePage\(|set_viewport_size|setViewportSize/);
  assert.doesNotMatch(source,/\bSKIP\b|process\.exit\(0\)/);
});

function productionHostMethods(){
  const production=fs.readFileSync(path.join(__dirname,'../src/embedded-browser.ts'),'utf8');
  const control=production.slice(production.indexOf('  async control('),production.indexOf('  async ensure('));
  const close=production.slice(production.indexOf('  async closeProfile('),production.indexOf('  async stop('));
  const javascript=require('typescript').transpileModule(`class Host {${control}\n${close}}`,{compilerOptions:{target:9,module:99}}).outputText;
  return new Function('uuid',javascript+';return Host;')('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}');
}

test('retained-close retry preserves production RPC refusal and awaits real lifecycle teardown',{timeout:5000},async()=>{
  const Host=productionHostMethods();
  let release,entered=false,flushes=0,storageFlushes=0;
  const gate=new Promise(resolve=>{release=resolve;});
  const profile={id:'native:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',owner:'11111111-1111-4111-8111-111111111111',generation:1,closed:true,pages:new Map(),clients:new Set(),session:{cookies:{async flushStore(){flushes++;entered=true;await gate;}},flushStorageData(){storageFlushes++;}}};
  const host=Object.assign(new Host(),{profiles:new Map([[profile.id,profile]]),opening:new Map(),closing:new WeakMap(),retiringProfiles:new Map(),resetting:new Set(),pendingPages:new Map(),cancelPendingPage:new WeakMap(),sessionWrites:new Map(),hide(){}});
  const requests=[];
  const request=async(_host,method,body)=>{
    assert.equal(_host,host);requests.push(method);
    try{return {status:200,body:await host.control(method,body)};}catch(error){return {status:409,body:{error:error.message}};}
  };
  let settled=false;
  const result=fixture.retryRetainedProfile(host,profile,request).finally(()=>{settled=true;});
  try{
    for(let i=0;i<30&&!entered;i++)await Promise.resolve();
    assert.equal(entered,true);assert.equal(settled,false);assert.equal(host.profiles.get(profile.id),profile);
    assert.equal(host.retiringProfiles.get(profile.id),1);
    assert.deepEqual(requests,['close','confirm-closed']);
    await assert.rejects(host.control('confirm-closed',{profile:profile.id,owner:profile.owner}),/尚未完全关闭/);
  }finally{release();}
  const evidence=await result;
  assert.equal(evidence.public_close_refused,true);assert.equal(evidence.retry_path,'EmbeddedBrowserHost.closeProfile (ensure/stop lifecycle)');
  assert.equal(evidence.acknowledgement.verification,'desktop-absence-v1');
  assert.deepEqual(requests,['close','confirm-closed','confirm-closed']);
  assert.equal(flushes,1);assert.equal(storageFlushes,1);assert.equal(host.profiles.size,0);assert.equal(host.retiringProfiles.size,0);
});

test('ordinary open follows production selection when initial about:blank URL is not committed',async()=>{
  const Host=productionHostMethods(),navigations=[];
  const makePage=(targetId,url)=>({targetId,view:{webContents:{isDestroyed:()=>false,getURL:()=>url,loadURL:async destination=>{navigations.push({targetId,destination});}}}});
  const initial=makePage('initial-uncommitted',''),selected=makePage('selected-opened','about:blank');
  const profile={id:'native:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',owner:'11111111-1111-4111-8111-111111111111',generation:2,closed:false,clients:new Set(),pages:new Map([[initial.targetId,initial]]),selected:initial.targetId};
  const host=Object.assign(new Host(),{profiles:new Map(),pageDisplay:new WeakMap(),
    async ensure(){this.profiles.set(profile.id,profile);return profile;},
    async newPage(p,url){assert.equal(p,profile);assert.equal(url,'about:blank');p.pages.set(selected.targetId,selected);p.selected=selected.targetId;return selected;},
    pageDisplayStatus(){return {display_state:'blank'};},activate(p,page){p.selected=page.targetId;},endpoint(){return 'ws://127.0.0.1/synthetic';}});
  const result=await host.control('open-instagram',{profile:profile.id,owner:profile.owner,proxy:''});
  assert.equal(result.opened,true);assert.equal(profile.pages.size,2);
  assert.equal([...profile.pages.values()][0],initial,'the first map entry stays the initial blank page');
  assert.equal(fixture.selectedNativePage(profile),selected,'fixture observes the actual ordinary-open target');
  assert.deepEqual(navigations,[{targetId:selected.targetId,destination:'https://www.instagram.com/'}]);
  profile.selected='missing';assert.throws(()=>fixture.selectedNativePage(profile),/selects a published/);
  profile.selected=selected.targetId;profile.closed=true;assert.throws(()=>fixture.selectedNativePage(profile),/live native profile/);
});

test('failed reopen diagnostics read every native page, selected target and protocol state without navigation',async()=>{
  const accountSession={protocol:{async isProtocolHandled(scheme){assert.ok(['http','https'].includes(scheme));return true;}}};
  const wc=(id,url)=>({id,session:accountSession,isDestroyed:()=>false,getURL:()=>url,getTitle:()=>id===1?'':'OFFLINE R6.3 cleanup fixture',isLoading:()=>false,isLoadingMainFrame:()=>false});
  const first=wc(1,'about:blank'),second=wc(2,'https://www.instagram.com/?private=value#secret');
  const page=(target,contents)=>({targetId:target,view:{webContents:contents,getBounds:()=>({x:0,y:0,width:900,height:700})}});
  const profile={id:'synthetic-profile',selected:'selected',closed:false,clients:new Set(),session:accountSession,pages:new Map([['blank',page('blank',first)],['selected',page('selected',second)]])};
  const host={profiles:new Map([[profile.id,profile]]),opening:new Map(),retiringProfiles:new Map(),resetting:new Set(),pageReady:new WeakSet([second]),pageDisplay:new WeakMap([[second,{display_state:'ready'}]])};
  const reads=[];
  const result=await fixture.collectReopenDiagnostics({host,id:profile.id,accountSession,phase:'selected-document',events:[{kind:'navigation-failed',code:-3}],records:[{requests:['/'],external_requests:[]}],read:async(contents,script,options)=>{
    reads.push(contents.id);assert.equal(options.timeoutMs,1500);assert.doesNotMatch(script,/fetch|loadURL|\.src\s*=/);
    return {url:contents.getURL(),title:contents.getTitle(),readyState:'complete'};
  }});
  assert.deepEqual(reads,[1,2]);assert.equal(result.selected_target,'selected');assert.equal(result.session_matches,true);
  assert.deepEqual(result.pages.map(p=>p.selected),[false,true]);assert.equal(result.pages[1].renderer.readyState,'complete');
  assert.equal(result.pages[1].url,'https://www.instagram.com/');assert.equal(result.pages[1].renderer.url,'https://www.instagram.com/');
  assert.deepEqual(result.protocol,{http:true,https:true});assert.equal(result.navigation_events[0].code,-3);
  assert.equal(fixture.diagnosticUrl('https://name:password@example.invalid/path?secret=1'),'[other origin] https://example.invalid');
});

test('navigation observer records only the synthetic Session and removes its listeners',()=>{
  const {EventEmitter}=require('node:events');
  const app=new EventEmitter(),session={},events=[],own=Object.assign(new EventEmitter(),{session,id:7,getURL:()=> 'https://www.instagram.com/'}),foreign=Object.assign(new EventEmitter(),{session:{},id:8});
  const dispose=fixture.observeReopenNavigation(app,session,events);
  app.emit('web-contents-created',{},own);app.emit('web-contents-created',{},foreign);
  own.emit('did-start-navigation',{},'https://www.instagram.com/?secret=x',false,true);
  own.emit('did-fail-load',{},-3,'ignored','https://www.instagram.com/?secret=x',true);
  assert.deepEqual(events.map(event=>event.kind),['created','navigation-start','navigation-failed']);
  assert.equal(events[1].url,'https://www.instagram.com/');assert.equal(foreign.listenerCount('did-fail-load'),0);
  dispose();assert.equal(app.listenerCount('web-contents-created'),0);assert.equal(own.listenerCount('did-fail-load'),0);
});

// These cases manufacture synthetic receipts/trees. Isolate only this unit-test
// process, and restore the real CI environment after every case. Native build
// commands never use this seam and must retain their actual GitHub identity.
let savedCiUnitEnvironment;
const syntheticCiEnvironment={GITHUB_SHA:'a'.repeat(40),GITHUB_ACTIONS:'true',GITHUB_REPOSITORY:'xaodai123gmailcom/juxin-ig-public-build',GITHUB_REPOSITORY_ID:'1406784621',GITHUB_REPOSITORY_OWNER_ID:'337452708'};
test.beforeEach(()=>{savedCiUnitEnvironment=Object.fromEntries(Object.keys(syntheticCiEnvironment).map(key=>[key,process.env[key]]));Object.assign(process.env,syntheticCiEnvironment)});
test.afterEach(()=>{for(const [key,value]of Object.entries(savedCiUnitEnvironment)){if(value===undefined)delete process.env[key];else process.env[key]=value}});

// Structural unit cases use an explicit test authority. Production native
// assertions still call the real fresh verifier; no SHA-only gate is allowed.
const unitSourceBinding=require('../../scripts/local_source_binding.cjs');
const realSourceAssertion=unitSourceBinding.assertSourceIdentity;
const unitSourceAuthority={schema:2,mode:'flat-git-ci',representation:'public-sanitized-source',repository:{full_name:'xaodai123gmailcom/juxin-ig-public-build',id:'1406784621',owner_id:'337452708'},source_commit:'a'.repeat(40),execution:'github-actions',git_checkout:true,github_actions:true,unit_test_authority:'native-contract-only'};
test.beforeEach(()=>{test.mock.method(unitSourceBinding,'assertSourceIdentity',(proof,options)=>realSourceAssertion(proof,{...options,verifyCi:()=>unitSourceAuthority}))});
test.afterEach(()=>test.mock.restoreAll());
