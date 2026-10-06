import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {PassThrough} from 'node:stream';
import vm from 'node:vm';
import {PexelsCredentialController, secureCredentialStorageAvailable, validatePexelsKey} from '../../dist-electron/pexels-integration.js';
import {captureCoreLog} from '../../dist-electron/core-log.js';

function fixture(overrides={}) {
  const calls=[];
  const dependencies={
    encryptionAvailable:()=>true,
    writeEncrypted:value=>calls.push(['encrypted',value]),
    rememberSecret:value=>calls.push(['redaction',value]),
    activate:async value=>{calls.push(['activate',value]);return {activated:true,configured:Boolean(value)}},
    ...overrides,
  };
  return {controller:new PexelsCredentialController(dependencies),calls};
}

test('private Pexels save persists encrypted then redacts before in-memory activation, without restarting',async()=>{
  const {controller,calls}=fixture();
  const reply=await controller.save(' fixture-pexels-not-a-real-key ');
  assert.deepEqual(calls.map(x=>x[0]),['encrypted','redaction','activate']);
  assert(calls.every(x=>x[1]==='fixture-pexels-not-a-real-key'));
  assert.deepEqual(reply,{restarted:false,saved:true,pexels_configured:true,pexels_activated:true,restart_required:false});
  assert(!JSON.stringify(reply).includes('fixture-pexels'));
});

test('empty key explicitly removes the saved key and activates unconfigured state',async()=>{
  const {controller,calls}=fixture();
  assert.deepEqual(await controller.save(' '),{restarted:false,saved:true,pexels_configured:false,pexels_activated:true,restart_required:false});
  assert(calls.every(x=>x[1]===''));
});

test('OS key store unavailable fails before persistence, activation or plaintext fallback',async()=>{
  for(const encryptionAvailable of [()=>false,()=>{throw new Error('unavailable')}]) {
    const {controller,calls}=fixture({encryptionAvailable});
    await assert.rejects(controller.save('fixture-key'),error=>error.message==='系统密钥库不可用，未保存 Pexels 密钥');
    assert.deepEqual(calls,[]);
  }
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'basic_text'},'linux'),false);
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'unknown'},'linux'),false);
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true},'linux'),false);
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'gnome_libsecret'},'linux'),true);
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true},'win32'),true);
  assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>false},'win32'),false);
});

test('encrypted write failure exposes no secret and never activates Core',async()=>{
  const {controller,calls}=fixture({writeEncrypted:()=>{throw new Error('fixture-write-secret')}});
  await assert.rejects(controller.save('fixture-write-secret'),error=>!error.message.includes('fixture-write-secret'));
  assert.deepEqual(calls,[]);
});

test('activation errors are masked and deferred without killing live tasks or losing saved configuration',async()=>{
  for(const activate of [async()=>{throw new Error('echo fixture-private-key')},async()=>({activated:true,configured:false}),async()=>({activated:false,configured:true}),async()=>null]) {
    const {controller,calls}=fixture({activate});
    const reply=await controller.save('fixture-private-key');
    assert.deepEqual(reply,{restarted:false,saved:true,pexels_configured:true,pexels_activated:false,restart_required:true});
    assert.equal(calls[0][0],'encrypted');
    assert(!JSON.stringify(reply).includes('fixture-private-key'));
  }
});

test('concurrent saves serialize persistence and activation, preventing stale-key reactivation',async()=>{
  let release;
  const gate=new Promise(resolve=>{release=resolve});
  const calls=[];
  const {controller}=fixture({writeEncrypted:key=>calls.push('write:'+key),rememberSecret:key=>calls.push('redact:'+key),activate:async key=>{calls.push('activate:'+key);if(key==='first')await gate;return{activated:true,configured:true}}});
  const first=controller.save('first');
  const second=controller.save('second');
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,['write:first','redact:first','activate:first']);
  release();await Promise.all([first,second]);
  assert.deepEqual(calls,['write:first','redact:first','activate:first','write:second','redact:second','activate:second']);
});

test('invalid values cannot reach storage or HTTP and errors never echo their content',()=>{
  for(const value of [null,{},[],1,'x'.repeat(1025),'header\ninjection','header\rinjection','white space','密钥']) {
    assert.throws(()=>validatePexelsKey(value),error=>error.message==='Pexels 密钥格式无效');
  }
});

test('log redaction includes newly activated keys and keeps previous keys',()=>{
  const stream=new PassThrough(),lines=[],values=['prior-fixture-key'];
  captureCoreLog(stream,'stdout',line=>lines.push(line),values);
  values.push('new-fixture-key');
  stream.write('trace prior-fixture-key new-fixture-key\n');
  assert.equal(lines.length,1);
  assert(!lines[0].includes('fixture-key'));
  assert.match(lines[0],/\[REDACTED\].*\[REDACTED\]/);
  stream.end();
});

test('production bridge binds write-only secret flow and excludes internal route from renderer requests',()=>{
  const main=readFileSync(new URL('../src/main.ts',import.meta.url),'utf8');
  const preload=readFileSync(new URL('../src/preload.cts',import.meta.url),'utf8');
  const client=readFileSync(new URL('../../renderer/src/core-client.ts',import.meta.url),'utf8');
  const policy=readFileSync(new URL('../src/core-request-policy.ts',import.meta.url),'utf8');
  assert.match(main,/const rendererSecureSettingKeys = new Set\(\["session-token"\]\)/);
  assert.match(main,/IGAC_PEXELS_API_KEY: pexelsApiKey/);
  assert.match(main,/const privateValues = corePrivateValues/);
  assert.match(main,/rememberSecret: rememberCoreSecret/);
  assert.match(main,/headers: \{"x-startup-token": coreToken, "Content-Type": "application\/json"\}/);
  assert.match(main,/JSON\.stringify\(\{pexels_api_key: value\}\)/);
  assert.match(main,/return pexelsCredentials\.save\(input\.pexelsApiKey\)/);
  assert.match(preload,/pexelsApiKey\?: string/);
  assert.match(client,/pexelsApiKey\?: string/);
  assert(!policy.includes('/api/internal/integrations/pexels'));
  const branch=main.slice(main.indexOf('if (input.pexelsApiKey !== undefined)'),main.indexOf('if (typeof input.bitbrowserPort'));
  assert(!branch.includes('restartForConfiguration'));
});


test('actual generic secure-store IPC handlers reject integration reads, writes and deletes before storage access',()=>{
  const main=readFileSync(new URL('../../dist-electron/main.js',import.meta.url),'utf8');
  const allowlist=main.match(/const rendererSecureSettingKeys = new Set\(\[[^;]+;/)?.[0];
  assert(allowlist,'compiled main must retain its renderer secure-key allowlist');
  const start=main.indexOf('ipcMain.handle("secure:set"');
  const end=main.indexOf('const coreSupervisor',start);
  assert(start>0&&end>start,'compiled main secure handlers are present');
  const handlers=new Map(),access=[];
  const context={
    ipcMain:{handle:(name,handler)=>handlers.set(name,handler)},
    requireMainSender:event=>{if(event!==validSender)throw new Error('invalid sender')},
    readSecureValue:key=>{access.push(['read',key]);return 'session-only-value'},
    writeSecureValue:(key,value)=>access.push(['write',key,value]),
    readSecureStore:()=>{access.push(['read-store']);return {'session-token':'encrypted-session','pexels-api-key':'encrypted-private'}},
    writeSecureStore:store=>access.push(['write-store',store]),
  };
  const validSender={};
  vm.runInNewContext(allowlist+'\n'+main.slice(start,end),context);
  for(const key of ['pexels-api-key','openai-api-key','bitbrowser-api-key','__proto__','']) {
    for(const route of ['secure:get','secure:set','secure:delete']) {
      assert.throws(()=>handlers.get(route)(validSender,key,'fixture-secret'),/不允许/);
      assert.deepEqual(access,[],`${route} ${key} must never reach storage`);
    }
  }
  for(const route of ['secure:get','secure:set','secure:delete']) {
    assert.throws(()=>handlers.get(route)({},'session-token','value'),/invalid sender/);
    assert.deepEqual(access,[]);
  }
  assert.equal(handlers.get('secure:get')(validSender,'session-token'),'session-only-value');
  assert.deepEqual(access,[['read','session-token']]);
});
