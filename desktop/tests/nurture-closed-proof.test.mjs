import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
// Execute the exact isolated production branch; no Electron, navigation or account.
const source=readFileSync(new URL('../src/embedded-browser.ts',import.meta.url),'utf8');
const branch=source.slice(source.indexOf("    if(method==='confirm-closed')"),source.indexOf("    if(method==='open-instagram')"));
const invoke=new Function('method','body','uuid',branch);
const profile='native:11111111-1111-4111-8111-111111111111',owner='22222222-2222-4222-8222-222222222222';
const uuid='[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}';
const state=()=>({profiles:new Map(),opening:new Map(),resetting:new Set(),retiringProfiles:new Map()});
const check=(s,body={profile,owner})=>invoke.call(s,'confirm-closed',body,uuid);
test('desktop confirms exact absent profile with explicit evidence, without browser effects',()=>{
 assert.deepEqual(check(state()),{closed:true,profile_id:profile,owner_user_id:owner,verification:'desktop-absence-v1'});
 assert.doesNotMatch(branch,/closeProfile\([^)]|\.delete\(|\.clear\(|await|\.ensure\(/);
});
test('inventory-invisible retiring profile is not confirmed closed',()=>{
 for(const closed of [false,true]){const s=state();s.profiles.set(profile,{id:profile,closed});assert.throws(()=>check(s),/尚未完全关闭/);assert.equal(s.profiles.size,1)}
});
test('opening unpublished, retiring orphan, and reset-in-progress each block confirmation',()=>{
 for(const field of ['opening','retiringProfiles','resetting']){const s=state();if(field==='retiringProfiles')s.retiringProfiles.set(profile,1);else if(field==='resetting')s.resetting.add(profile);else s.opening.set(profile,Promise.resolve());assert.throws(()=>check(s),/尚未完全关闭/)}
 const s=state();s.profiles.set('other',{});s.opening.set('other',{});assert.equal(check(s).closed,true);
});
test('malformed profile and owner identities never return positive evidence',()=>{
 for(const body of [{profile:'other',owner},{profile,owner:'other'},{profile:'',owner},{profile,owner:''}])assert.throws(()=>check(state(),body),/标识无效/);
});
