import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,mkdirSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {whatsappSessionPath,whatsappErrorText} from '../../dist-electron/whatsapp-session.js';
import {accountCacheProfiles} from '../../dist-electron/storage-management.js';
const owner='11111111-1111-4111-8111-111111111111',profile='native:77777777-7777-4777-8777-777777777777';
test('rebuilt WhatsApp persists independently, rotating retains prior data and never touches the legacy partition',()=>{
 const root=mkdtempSync(join(tmpdir(),'wa-store-'));
 try{
  const legacy=join(root,'Partitions',`account-${owner}-${profile.slice(7)}`);mkdirSync(legacy,{recursive:true});writeFileSync(join(legacy,'old-login'),'keep');
  const first=whatsappSessionPath(root,owner,profile);writeFileSync(join(first,'login'),'new login');
  assert.ok(first.length<legacy.length);assert.equal(whatsappSessionPath(root,owner,profile),first);
  assert.notEqual(whatsappSessionPath(root,'22222222-2222-4222-8222-222222222222',profile),first);
  assert.throws(()=>whatsappSessionPath(root,'../escape',profile));
  const rotated=whatsappSessionPath(root,owner,profile,true);assert.notEqual(rotated,first);assert.equal(whatsappSessionPath(root,owner,profile),rotated);
  assert.equal(readFileSync(join(legacy,'old-login'),'utf8'),'keep');assert.equal(readFileSync(join(first,'login'),'utf8'),'new login');
 }finally{rmSync(root,{recursive:true,force:true})}
});
test('WhatsApp raw failure retains the actionable sentence and removes URL secrets',()=>{
 const text=whatsappErrorText('UnknownError: Opening backing store failed at https://web.whatsapp.com/db.js?token=SECRET#private');
 assert.match(text,/Opening backing store failed/);assert.match(text,/db.js/);assert.doesNotMatch(text,/SECRET|private/);
});
test('live WhatsApp storage stays out of cache cleanup; rotated backup is not a cleanup target',async()=>{
 const root=mkdtempSync(join(tmpdir(),'wa-cache-'));
 try{const old=whatsappSessionPath(root,owner,profile);const active=whatsappSessionPath(root,owner,profile,true);const rows=await accountCacheProfiles(root,new Set(),new Set([profile]));
 assert.equal(rows.length,1);assert.equal(rows[0].path,active);assert.notEqual(rows[0].path,old);assert.equal(rows[0].busy,true);assert.equal(rows[0].partition,'wa-path:'+active);
 }finally{rmSync(root,{recursive:true,force:true})}
});
