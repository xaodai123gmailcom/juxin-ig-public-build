import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OptionalImmersiveSource} from '../../dist-electron/immersive-source.js';
import {ChatTranslation, chatSettings} from '../../dist-electron/chat-translation.js';

test('absent, non-file and modified optional scripts fail closed without exposing local paths', t => {
 const root=mkdtempSync(join(tmpdir(),'optional-translation-'));t.after(()=>rmSync(root,{recursive:true,force:true}));
 const file=join(root,'private-user-location.js');writeFileSync(file,'// unverified script');
 for(const path of [undefined,join(root,'missing.js'),root,file]){
  const source=new OptionalImmersiveSource(path),state=source.status();
  assert.equal(state.available,false);assert.match(state.message,/可选翻译组件未安装/);assert.equal(state.message.includes(root),false);
  assert.throws(()=>source.load(),error=>error.requiresConfiguration===true);
 }
});

test('missing optional component rejects enabling before preference writes and does not block chat export', async t => {
 const root=mkdtempSync(join(tmpdir(),'optional-chat-'));t.after(()=>rmSync(root,{recursive:true,force:true}));
 const source=new OptionalImmersiveSource();let translated=0;const steps=[];
 const runtime={status:()=>source.status(),cacheIdentity:()=>'',translate:async()=>{translated++;throw new Error('must not run')},settings:async()=>source.load(),retry(){},reset(){}};
 const service=new ChatTranslation(join(root,'settings.json'),()=> 'session',()=>({id:'profile'}),async(_id,step)=>{
  steps.push(step);if(step.kind==='check')return {profile:'profile'};
  return {messages:[{id:'message',original:'offline fixture'}]};
 },()=>null,()=>{},{immersiveOnly:true,immersive:runtime});t.after(()=>service.stop());
 const state=await service.command('account',{},'session');assert.equal(state.immersive.available,false);
 await assert.rejects(service.command('account',{mode:'immersive'},'session'),/可选翻译组件未安装/);
 await assert.rejects(service.command('account',{openImmersive:true},'session'),/可选翻译组件未安装/);
 assert.equal((await service.command('account',{export:'original'},'session')).text,'offline fixture');
 await service.tick();assert.equal(translated,0);assert.equal(steps.some(step=>step.settings?.enabled===true),false);
});

test('a previously enabled account clears annotations without executing an absent optional component', async t => {
 const root=mkdtempSync(join(tmpdir(),'optional-saved-chat-'));t.after(()=>rmSync(root,{recursive:true,force:true}));
 const file=join(root,'settings.json');writeFileSync(file,JSON.stringify({account:chatSettings({engine:'immersive',enabled:true})}));
 const steps=[],source=new OptionalImmersiveSource();let translated=0;
 const service=new ChatTranslation(file,()=> 'session',()=>({id:'profile'}),async(_id,step)=>{steps.push(step);return step.kind==='check'?{profile:'profile'}:{messages:[{original:'fixture'}]};},()=>null,()=>{},
  {immersiveOnly:true,immersive:{status:()=>source.status(),cacheIdentity:()=>'',translate:async()=>{translated++;return 'unexpected'},settings:async()=>{},retry(){},reset(){}}});t.after(()=>service.stop());
 await service.command('account',{},'session');await service.tick();assert.equal(translated,0);
 assert.equal(steps.find(step=>step.kind==='poll').settings.enabled,false);
});
