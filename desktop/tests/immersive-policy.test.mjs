import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {ImmersiveRouting,immersiveDefaults,immersivePartition,immersivePageConfig,immersiveRequestUrl,immersiveRetryDelay,officialImmersiveUrl,verifyImmersiveSource} from '../../dist-electron/immersive-policy.js';
test('public distribution excludes proprietary source and rejects unverified replacement scripts',()=>{
 assert.equal(existsSync(new URL('../vendor/immersive-translate/immersive-translate.user.js',import.meta.url)),false);
 for(const bytes of [Buffer.alloc(0),Buffer.from('// @version 1.33.1\nalert(1)'),Buffer.from('first-party fixture')])assert.throws(()=>verifyImmersiveSource(bytes),/校验失败/);
});
test('translation sessions are isolated from accounts and other application users',()=>{
 const a=immersivePartition('user-a','account-a');assert.match(a,/^persist:immersive-chat-[a-f0-9]{64}$/);
 assert.notEqual(a,immersivePartition('user-b','account-a'));assert.notEqual(a,immersivePartition('user-a','account-b'));
});
test('manual selection remains fixed; smart routing uses only selected candidates and respects cooldown',()=>{
 let now=0;const r=new ImmersiveRouting(()=>now),s={...immersiveDefaults,immersiveMode:'smart',immersiveService:'bing',immersiveFallbacks:['google']};
 assert.deepEqual(r.choose(s),['bing','google']);r.success('google',200);r.success('bing',900);assert.deepEqual(r.choose(s),['google','bing']);
 r.failure('google',120000);assert.deepEqual(r.choose(s),['bing']);
 assert.deepEqual(r.choose({...s,immersiveMode:'manual',immersiveService:'google'}),[],'fixed provider still respects its cooldown');
 r.failure('bing');assert.deepEqual(r.choose(s),[]);now=30001;assert.deepEqual(r.choose(s),['bing']);now=120001;assert.equal(r.choose(s).length,2);
 assert.equal(immersiveRetryDelay('120',0),120000);assert.equal(immersiveRetryDelay('Thu, 01 Jan 1970 00:02:00 GMT',0),120000);assert.equal(immersiveRetryDelay(null,0),30000);
 assert.ok(r.choose(s).every(x=>['google','bing'].includes(x)),'never opts into a paid provider');
});
test('translator network and settings navigation reject account sites, credentials in URLs and unrelated hosts',()=>{
 for(const u of ['https://web.whatsapp.com','https://www.instagram.com','http://translate.googleapis.com','https://127.0.0.1/x','https://user:password@api.openai.com/x','https://api.openai.com.evil.test/x'])assert.equal(immersiveRequestUrl(u),false,u);
 assert.equal(immersiveRequestUrl('https://translate.googleapis.com/x'),true);
 assert.equal(immersiveRequestUrl('https://custom.example/translate',['https://custom.example/v1']),true);
 assert.equal(immersiveRequestUrl('https://other.example/translate',['https://custom.example/v1']),false);
 assert.equal(officialImmersiveUrl('https://dash.immersivetranslate.com/#general'),true);
 for(const u of ['https://immersivetranslate.com.evil.test','https://user@immersivetranslate.com','file:///etc/passwd','https://web.whatsapp.com'])assert.equal(officialImmersiveUrl(u),false);
});
test('chat-only configuration keeps paid credentials intact and disables the input feature',()=>{
 const saved={enableInputTranslation:true,translationServices:{deepl:{apiKey:'fixture-key'}},generalRule:{fontSize:'16'}};
 const config=immersivePageConfig(saved);assert.equal(config.enableInputTranslation,false);assert.deepEqual(config.generalRule.selectors,['#juxin-messages .juxin-source']);assert.equal(config.translationServices.deepl.apiKey,'fixture-key');assert.equal(saved.enableInputTranslation,true);assert.equal(config.generalRule.allowInnerInvoke,undefined);
});
