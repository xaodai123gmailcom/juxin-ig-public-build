import assert from 'node:assert/strict';
import test from 'node:test';
import {provisionOpenAI} from '../../dist-electron/provision-integrations.js';
test('a fresh installation has no bundled integration credential',()=>{
 const reads=[];
 assert.equal(provisionOpenAI(key=>{reads.push(key);return null;}),'');
 assert.deepEqual(reads,['openai-api-key']);
});
test('only the explicitly configured secure-store value is used',()=>{
 assert.equal(provisionOpenAI(()=> 'owner-configured-fixture'),'owner-configured-fixture');
});
test('an empty or unavailable secure-store value never receives a default',()=>{
 assert.equal(provisionOpenAI(()=>''),'');
 assert.throws(()=>provisionOpenAI(()=>{throw new Error('key store unavailable');}),/key store unavailable/);
});
