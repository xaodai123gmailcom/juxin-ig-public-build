import test from 'node:test';import assert from 'node:assert/strict';
import {ensureNotificationRegistration} from '../../dist-electron/notification-registration.js';
import {STABLE_APPLICATION_ID} from '../../dist-electron/app-identity.js';
const input={platform:'win32',packaged:true,appData:'/test/AppData',executable:'/test/Juxin.exe'};
test('portable registration uses the stable notification identity; installed shortcuts stay intact',()=>{
 const writes=[],directories=[];const links={readShortcutLink:()=>{throw Error('missing')},writeShortcutLink:(...args)=>{writes.push(args);return true}};
 ensureNotificationRegistration(input,links,p=>directories.push(p));assert.equal(writes.length,1);assert.equal(writes[0][2].appUserModelId,STABLE_APPLICATION_ID);assert.equal(writes[0][2].target,input.executable);assert.match(writes[0][0],/Start Menu.*Programs/);
 ensureNotificationRegistration(input,{...links,readShortcutLink:()=>({target:input.executable,appUserModelId:STABLE_APPLICATION_ID})},()=>assert.fail('already registered'));assert.equal(writes.length,1);
 ensureNotificationRegistration({...input,platform:'linux'},links);assert.equal(writes.length,1);
});
test('registration failure is surfaced, never reported as a delivered notification',()=>{
 assert.throws(()=>ensureNotificationRegistration(input,{readShortcutLink:()=>({target:'/other.exe',appUserModelId:'other-app'}),writeShortcutLink:()=>assert.fail('foreign shortcut must stay intact')},()=>{}),/已被占用/);
 assert.throws(()=>ensureNotificationRegistration(input,{readShortcutLink:()=>{throw Error('missing')},writeShortcutLink:()=>false},()=>{}),/无法注册/);
});
