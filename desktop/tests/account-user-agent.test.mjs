import test from 'node:test';
import assert from 'node:assert/strict';
import {accountUserAgent} from '../../dist-electron/account-user-agent.js';
const prefix='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)';
test('localized and renamed products cannot leak malformed tokens into account requests',()=>{
 for(const product of ['聚鑫国际/1.0.72','juxin-ig-audience-collector-newgen/1.0.72','New Product/1.0']) {
  const ua=accountUserAgent(`${prefix} ${product} Chrome/134.0.6998.205 Electron/35.7.5 Safari/537.36`);
  assert.equal(ua,`${prefix} Chrome/134.0.6998.205 Safari/537.36`);
  assert.equal(accountUserAgent(ua),ua);
 }
});
test('invalid engine data fails without inventing a browser version',()=>{
 assert.throws(()=>accountUserAgent('unknown'));
});
