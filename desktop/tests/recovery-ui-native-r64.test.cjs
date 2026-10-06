const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {SCENARIOS,buildFixture,assertProofComplete,isStandaloneEntry}=require('./recovery-ui-native-r64.cjs');
const nativeFile=path.join(__dirname,'recovery-ui-native-r64.cjs');
const rendererFile=path.join(__dirname,'../../renderer/tests/fixtures/recovery-ui-r64.tsx');
function complete(){return {schema:1,gate:'r64-recovery-ui-native',native_runtime:true,synthetic_offline:true,cleanup_verified:true,required_mode:false,platform:'linux',headless:false,native_window:{visible:true},windows_release_status:'pending',external_requests:[],external_actions:[],renderer_errors:[],unexpected_endpoints:[],scenarios:Object.fromEntries(SCENARIOS.map(key=>[key,true])),native_input:Array.from({length:20},()=>({trusted:true})),confirmation_mode:'production-window.confirm/controlled-response',scope:{os_dialog_automation:false},source_commit:'a'.repeat(40),github_sha:'a'.repeat(40),source_provenance:{schema:2,mode:'flat-git-ci',representation:'public-sanitized-source',repository:{full_name:'xaodai123gmailcom/juxin-ig-public-build',id:'1406784621',owner_id:'337452708'},source_commit:'a'.repeat(40),execution:'github-actions',git_checkout:true,github_actions:true,unit_test_authority:'native-contract-only'},source_sha256:Object.fromEntries(['renderer/src/App.tsx','renderer/src/posting-workspace.tsx','renderer/src/nurture-collection-blocker.tsx','renderer/src/standalone-nurture-workspace.tsx','renderer/tests/fixtures/recovery-ui-r64.tsx','desktop/tests/recovery-ui-native-r64.cjs'].map(file=>[file,'a'.repeat(64)])),start_intents:[{}],stop_intents:[{},{}],successful_stops:1,cleanup_successes:1,screenshots:['withdrawn-review','hidden-blocker','stopped-feedback','fresh-review'].map(name=>({file:'r64-recovery-ui-'+name+'.png',sha256:'b'.repeat(64),bytes:1024,size:{width:1440,height:1050},native_visible:true,renderer_visibility:'visible'}))}}
test('proof allows a truthful Linux native run but preserves Windows release pending',()=>assert.doesNotThrow(()=>assertProofComplete(complete())));
test('mandatory Windows mode refuses Linux and unidentified checkouts',()=>{
 const valid={...complete(),required_mode:true,platform:'win32',windows_release_status:'passed'};
 assert.doesNotThrow(()=>assertProofComplete(valid));
 const unidentified={...valid};delete unidentified.source_provenance;
 assert.throws(()=>assertProofComplete(unidentified));
 assert.throws(()=>assertProofComplete({...valid,platform:'linux',windows_release_status:'pending'}),/Windows release proof required/);
});
test('every required scenario is mandatory, unknown scenarios are rejected',()=>{
 for(const name of SCENARIOS){const missing=complete();delete missing.scenarios[name];assert.throws(()=>assertProofComplete(missing),name);const falseResult=complete();falseResult.scenarios[name]=false;assert.throws(()=>assertProofComplete(falseResult),name)}
 const extra=complete();extra.scenarios.unreviewed=true;assert.throws(()=>assertProofComplete(extra));
});
test('proof rejects external access, renderer errors, unexpected commands and synthetic DOM events',()=>{
 for(const key of ['external_requests','external_actions','renderer_errors','unexpected_endpoints']){const proof=complete();proof[key].push('forbidden');assert.throws(()=>assertProofComplete(proof),key)}
 const proof=complete();proof.native_input[0].trusted=false;assert.throws(()=>assertProofComplete(proof));
});
test('proof rejects duplicate start, missing stop and merged stop/cleanup evidence',()=>{
 for(const [key,value] of [['start_intents',[{},{}]],['stop_intents',[]],['successful_stops',0],['cleanup_successes',0],['cleanup_verified',false]]){const proof=complete();proof[key]=value;assert.throws(()=>assertProofComplete(proof),key)}
});
test('proof is bound to explicit verified provenance and production source hashes',()=>{
 const proof=complete();proof.source_provenance.unit_test_authority='changed';assert.throws(()=>assertProofComplete(proof));proof.source_provenance=complete().source_provenance;assert.doesNotThrow(()=>assertProofComplete(proof));
 for(const sha of ['1'.repeat(40),'2'.repeat(40)])assert.throws(()=>assertProofComplete({...proof,source_commit:sha}),/actual CI checkout/);
 for(const key of Object.keys(proof.source_sha256)){const broken=structuredClone(proof);delete broken.source_sha256[key];assert.throws(()=>assertProofComplete(broken),key)}
});
test('fixture bundles unmodified production App, workspaces, guards and handlers',async()=>{
 const bundle=await buildFixture();const files=Object.keys(bundle.metafile.inputs);for(const file of ['renderer/src/App.tsx','renderer/src/posting-workspace.tsx','renderer/src/standalone-nurture-workspace.tsx','renderer/src/nurture-collection-blocker.tsx','renderer/src/core-client.ts'])assert.ok(files.includes(file),file);
 assert.ok(bundle.outputFiles.some(file=>file.path.endsWith('.css')));assert.ok(bundle.outputFiles.some(file=>file.path.endsWith('.js')));
});
test('native runner never substitutes DOM click or synthetic dispatched events',()=>{
 const source=fs.readFileSync(nativeFile,'utf8');assert.doesNotMatch(source,/\.click\s*\(|dispatchEvent\s*\(/);assert.match(source,/sendInputEvent\(\{type:'mouseDown'/);assert.match(source,/sendInputEvent\(\{type:'keyDown'/);assert.match(source,/\.trusted/);
 assert.match(source,/whole-process deadline/);assert.match(source,/setTimeout\(.*app\.exit\(1\).*180000/);assert.match(source,/isStandaloneEntry/);
});
test('fixture has explicit completion gates, guarded mutations and a declared confirm seam',()=>{
 const source=fs.readFileSync(rendererFile,'utf8');assert.match(source,/await gate\(/);assert.match(source,/fixture\.release=resolve/);assert.doesNotMatch(source,/setTimeout\(/);assert.match(source,/window\.confirm=/);assert.match(source,/fixture\.confirmations\.push/);assert.match(source,/queued\.profile_id===held\.profile_id/);assert.match(source,/blocker\.status==='paused'/);assert.match(source,/body\.version!==blocker\.version/);assert.match(source,/fixture\.unexpected\.push/);
});
test('standalone detection supports Electron CJS loader without launching imported gates',()=>{
 assert.equal(isStandaloneEntry({isMain:true,electronVersion:undefined,entry:'/other'}),true);assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44',entry:nativeFile}),true);assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44',entry:'/other'}),false);assert.equal(isStandaloneEntry({isMain:false,electronVersion:undefined,entry:nativeFile}),false);
});

test('proof rejects missing captures and offscreen/hidden Windows release evidence',()=>{
 const missing=complete();missing.screenshots.pop();assert.throws(()=>assertProofComplete(missing));
 for(const patch of [{headless:true},{native_window:{visible:false}},{screenshots:complete().screenshots.map(s=>({...s,native_visible:false}))},{screenshots:complete().screenshots.map(s=>({...s,renderer_visibility:'hidden'}))}]){const valid={...complete(),required_mode:true,platform:'win32',windows_release_status:'passed'};assert.doesNotThrow(()=>assertProofComplete(valid));const proof={...valid,...patch};assert.throws(()=>assertProofComplete(proof));}
});
test('standalone entry accepts native flags before the Electron script',()=>{const original=process.argv;try{process.argv=['electron','--no-sandbox','--ozone-platform=headless',nativeFile];assert.equal(isStandaloneEntry({isMain:false,electronVersion:'44'}),true)}finally{process.argv=original}});

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
