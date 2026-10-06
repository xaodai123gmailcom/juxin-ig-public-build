import test from 'node:test';
import assert from 'node:assert/strict';
import {CloudConfigurationController,readCloudConfiguration,validateCloudConfiguration} from '../../dist-electron/cloud-integration.js';
const configured={enabled:true,projectUrl:'https://fixture-cloud.example',publishableKey:'sb_publishable_synthetic_fixture'};
const reply=value=>({activated:true,configured:value.enabled,enabled:value.enabled,project_url:value.projectUrl});
function fixture(options={}){
 let saved=null;const writes=[],calls=[],remembered=[];
 const controller=new CloudConfigurationController({encryptionAvailable:()=>true,readEncrypted:()=>saved,
  writeEncrypted:value=>{saved=value;writes.push(value)},rememberSecret:key=>remembered.push(key),activate:async value=>{calls.push(value);return reply(value)},...options});
 return {controller,writes,calls,remembered,read:()=>saved};
}
test('cloud defaults to disabled without encrypted explicit configuration',()=>{
 for(const value of [null,'{}','invalid',JSON.stringify({...configured,enabled:'true'})])assert.equal(readCloudConfiguration(()=>value,true).enabled,false);
 assert.equal(readCloudConfiguration(()=>JSON.stringify(configured),false).enabled,false);
 assert.deepEqual(readCloudConfiguration(()=>JSON.stringify(configured),true),configured);
});
test('cloud accepts only bounded HTTPS origins and public keys, never privileged keys',()=>{
 for(const projectUrl of ['http://cloud.example','https://user:pass@cloud.example','https://cloud.example/path','https://cloud.example/?secret=value','https://localhost','https://127.0.0.1','https://cloud.internal','https://cloud.example:8443','https://cloud.example\\evil'])assert.throws(()=>validateCloudConfiguration({...configured,projectUrl}));
 const jwt=role=>['eyJhbGciOiJIUzI1NiJ9',Buffer.from(JSON.stringify({role})).toString('base64url'),'signature'].join('.');
 for(const publishableKey of ['sb_secret_fixture',jwt('service_role'),'key\nvalue'])assert.throws(()=>validateCloudConfiguration({...configured,publishableKey}));
 assert.equal(validateCloudConfiguration({...configured,publishableKey:jwt('anon')}).enabled,true);
 assert.equal(validateCloudConfiguration({...configured,projectUrl:configured.projectUrl+'/'}).projectUrl,configured.projectUrl);
 assert.throws(()=>validateCloudConfiguration({...configured,extra:true}));
});
test('saving stops the old destination, commits encrypted configuration, then activates with redaction',async()=>{
 const f=fixture();const result=await f.controller.save(configured);
 assert.equal(result.cloud_activated,true);assert.equal(result.restarted,false);assert.equal(f.calls.length,2);
 assert.deepEqual(f.calls[0],{enabled:false,projectUrl:'',publishableKey:''});assert.deepEqual(f.calls[1],configured);
 assert.deepEqual(JSON.parse(f.read()),configured);assert.deepEqual(f.remembered,[configured.publishableKey]);assert.equal(JSON.stringify(result).includes(configured.publishableKey),false);
 await f.controller.save({...configured,publishableKey:''});assert.equal(f.calls.at(-1).publishableKey,configured.publishableKey);
 await assert.rejects(f.controller.save({...configured,projectUrl:'https://different.example',publishableKey:''}),/公钥/);
});
test('failed old-destination stop or unavailable secure storage cannot change persistent settings',async()=>{
 for(const options of [{encryptionAvailable:()=>false},{activate:async()=>{throw new Error('offline')}},{activate:async()=>({activated:false})}]){
  const f=fixture(options);await assert.rejects(f.controller.save(configured));assert.equal(f.writes.length,0);
 }
});
test('failed encrypted write leaves cloud stopped and failed new activation is explicit',async()=>{
 const calls=[];const failed=fixture({writeEncrypted:()=>{throw new Error('private fixture details')},activate:async value=>{calls.push(value);return reply(value)}});
 await assert.rejects(failed.controller.save(configured),/配置未能安全保存/);assert.equal(calls.length,1);assert.equal(calls[0].enabled,false);
 let count=0;const pending=fixture({activate:async value=>{if(++count===2)throw new Error('private key');return reply(value)}});
 const result=await pending.controller.save(configured);assert.equal(result.cloud_activated,false);assert.equal(result.restart_required,true);assert.equal(pending.writes.length,1);
});
test('concurrent saves preserve stop/store/activate ordering and the newest destination',async()=>{
 let release;const calls=[],f=fixture({activate:async value=>{calls.push(value);if(calls.length===1)await new Promise(resolve=>release=resolve);return reply(value)}});
 const a=f.controller.save(configured),b=f.controller.save({...configured,projectUrl:'https://new-cloud.example'});
 await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,1);release();await Promise.all([a,b]);
 assert.deepEqual(calls.map(value=>value.projectUrl),['',configured.projectUrl,'','https://new-cloud.example']);assert.equal(JSON.parse(f.read()).projectUrl,'https://new-cloud.example');
});
