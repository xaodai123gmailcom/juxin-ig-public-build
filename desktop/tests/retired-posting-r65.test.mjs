import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {secureCredentialStorageAvailable} from '../../dist-electron/secure-credential-storage.js';

test('OS-backed credential storage fails closed on unavailable and plaintext fallbacks',()=>{
 for(const getSelectedStorageBackend of [undefined,()=> 'basic_text',()=> 'unknown'])assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true,getSelectedStorageBackend},'linux'),false);
 assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'gnome_libsecret'},'linux'),true);
 assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>true},'win32'),true);
 assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>false},'win32'),false);
 assert.equal(secureCredentialStorageAvailable({isEncryptionAvailable:()=>{throw Error('unavailable')}},'win32'),false);
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


test('actual integration IPC rejects retired configuration before all persistence or activation', async()=>{
 const main=readFileSync(new URL('../../dist-electron/main.js',import.meta.url),'utf8');
 const start=main.indexOf('ipcMain.handle("core:configure"'),end=main.indexOf('let quitShutdownStarted',start);
 const handlers=new Map(),writes=[];
 vm.runInNewContext(main.slice(start,end),{
  ipcMain:{handle:(name,handler)=>handlers.set(name,handler)},requireMainSender(){},
  writeSecureValue:(...args)=>writes.push(args),cloudConfiguration:{save:()=>writes.push('cloud')},
  coreSupervisor:{restartForConfiguration:()=>writes.push('restart')},
 });
 for(const input of [{pexelsApiKey:'fixture'},{bitbrowserPort:54345,pexelsApiKey:'fixture'}])await assert.rejects(handlers.get('core:configure')({},input),/集成配置格式无效/);
 assert.deepEqual(writes,[]);
});

test('desktop production modules expose no publisher or material-provider capability',()=>{
 for(const name of ['main.ts','preload.cts','embedded-browser.ts','account-viewport.ts','core-request-policy.ts']){
  const text=readFileSync(new URL('../src/'+name,import.meta.url),'utf8');
  assert.doesNotMatch(text,/pexelsApiKey|PexelsCredentialController|postingViewport|\/api\/posting\/|\/api\/internal\/integrations\/pexels/,name);
 }
 const main=readFileSync(new URL('../src/main.ts',import.meta.url),'utf8');
 assert.match(main,/secureCredentialStorageAvailable/);assert.match(main,/CloudConfigurationController/);assert.match(main,/provisionOpenAI/);
});

test('retired inherited credentials are deleted before children without reading their values',()=>{
 const main=readFileSync(new URL('../src/main.ts',import.meta.url),'utf8');
 const start=main.indexOf('// Retired material-provider credentials');
 const end=main.indexOf('const originalUserData =',start);
 assert(start>0&&end>start&&end<main.indexOf('function spawnCore()'));
 const env={OPENAI_API_KEY:'retained-other-integration',PATH:'retained-path'};
 for(const name of ['IGAC_PEXELS_API_KEY','PEXELS_API_KEY'])Object.defineProperty(env,name,{
  configurable:true,enumerable:true,get(){throw Error('retired credential was read')},
 });
 vm.runInNewContext(main.slice(start,end),{process:{env}});
 assert.deepEqual({...env},{OPENAI_API_KEY:'retained-other-integration',PATH:'retained-path'});
 const references=main.split('\n').filter(line=>/IGAC_PEXELS_API_KEY|\bPEXELS_API_KEY\b/.test(line));
 assert.deepEqual(references,['delete process.env.IGAC_PEXELS_API_KEY;','delete process.env.PEXELS_API_KEY;']);
});
