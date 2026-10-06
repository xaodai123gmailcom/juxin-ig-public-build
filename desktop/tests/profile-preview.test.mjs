import test from 'node:test';
import assert from 'node:assert/strict';
import { profilePreviewUrl, previewFailureMessage } from '../../dist-electron/profile-preview.js';
import fixture from './floating-fixture.cjs';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from 'typescript';
import './review-recovery-fixture.test.cjs';
test('profile preview accepts only a target username and explicit login action', () => {
  assert.equal(profilePreviewUrl('sample.user_7', 'target'), 'https://www.instagram.com/sample.user_7/');
  assert.equal(profilePreviewUrl('sample.user_7', 'login'), 'https://www.instagram.com/accounts/login/');
  for (const name of ['../direct', 'https://evil.test/', 'accounts', 'user?x=1', '', null]) assert.throws(() => profilePreviewUrl(name, 'target'));
  assert.throws(() => profilePreviewUrl('sample', 'post'));
});
test('HTTP failure, proxy error page and network failure are actionable failures', () => {
  for (const status of [401,403,404,429,500]) assert.ok(previewFailureMessage(status));
  assert.match(previewFailureMessage(403), /403/);
  assert.match(previewFailureMessage(429), /稍后手动/);
  assert.ok(previewFailureMessage(200, '4xx Client Error'));
  assert.ok(previewFailureMessage(0, '', 'ERR_PROXY_CONNECTION_FAILED'));
  assert.equal(previewFailureMessage(200, 'Instagram'), '');
});

test('review preview stays before avatars at two window sizes and on a negative-coordinate monitor', async () => {
  const {reviewPreviewBounds}=await import('../../dist-electron/profile-preview.js');
  for(const [parent,area,anchor] of [
    [{x:0,y:0,width:1920,height:1080},{x:0,y:0,width:1920,height:1080},{left:116,top:276,avatarLeft:1075}],
    [{x:20,y:30,width:1280,height:760},{x:0,y:0,width:1440,height:900},{left:116,top:276,avatarLeft:620}],
    [{x:-1920,y:0,width:1920,height:1080},{x:-1920,y:0,width:1920,height:1080},{left:116,top:276,avatarLeft:1075}],
  ]) {
    const b=reviewPreviewBounds(parent,area,anchor);
    assert.equal(b.x,parent.x+anchor.left);
    assert.equal(b.y,parent.y+anchor.top);
    assert.ok(b.x+b.width<=parent.x+anchor.avatarLeft-12);
    assert.ok(b.y+b.height<=area.y+area.height);
  }
  assert.deepEqual(reviewPreviewBounds({x:0,y:0,width:1920,height:1080},{x:0,y:0,width:1920,height:1080},{left:116,top:276,avatarLeft:1075}),{x:116,y:276,width:947,height:640});
});
test('preview without a review table has a bounded left-side default', async () => {
  const {reviewPreviewBounds}=await import('../../dist-electron/profile-preview.js');
  const parent={x:50,y:50,width:1280,height:760},area={x:0,y:0,width:1440,height:900};
  for(const anchor of [null,{left:NaN,top:0,avatarLeft:900},{left:100,top:100,avatarLeft:150}]){
    const b=reviewPreviewBounds(parent,area,anchor);
    assert.equal(b.x,166);assert.ok(b.width<=parent.width*.4);
    assert.ok(Object.values(b).every(Number.isFinite));
  }
});


test('review account commands cannot accept session IDs, credentials or arbitrary URLs', async () => {
 const {reviewAccountAction}=await import('../../dist-electron/review-recovery.js');
 for(const action of ['status','login','home','target','hide'])assert.equal(reviewAccountAction({action}),action);
 for(const input of [null,[],{},{action:'logout'},{action:'login',url:'https://evil.test/'},{action:'home',partition:'other-user'},{action:'login',cookie:'private'}])assert.throws(()=>reviewAccountAction(input));
});
test('review error recovery is escaped, manual and returns only to validated target URLs', async () => {
 const {reviewRecoveryHtml}=await import('../../dist-electron/review-recovery.js');
 const html=reviewRecoveryHtml('<img src=x onerror=alert(1)> & error','some.user');
 assert.ok(html.includes('&lt;img'));assert.ok(!html.includes('<img'));assert.ok(html.includes('charset="utf-8"'));
 assert.ok(html.includes('href="https://www.instagram.com/some.user/"'));
 assert.ok(html.includes('href="https://www.instagram.com/accounts/login/"'));
 assert.ok(html.includes('账号主页 / 切换'));
 assert.ok(!html.includes('<script'));assert.ok(!html.includes('http-equiv="refresh"'));
 assert.ok(!reviewRecoveryHtml('Network error','').includes('重新打开 @'));
 assert.throws(()=>reviewRecoveryHtml('failed','../other'));
 assert.match(previewFailureMessage(401),/登录/);
 assert.match(previewFailureMessage(404),/用户名/);
 assert.match(previewFailureMessage(429),/稍后手动重试/);
});

test('test window fits small, ordinary and negative-coordinate work areas without assuming 1500 x 960', () => {
 for (const area of [{x:0,y:0,width:1024,height:728},{x:0,y:0,width:854,height:480},
  {x:0,y:0,width:1920,height:1040},{x:-1280,y:40,width:1280,height:680}]) {
  const bounds=fixture.fixtureWindowBounds(area);
  assert.ok(fixture.inside(bounds,area));assert.ok(bounds.width<=1120&&bounds.height<=720);
 }
 assert.deepEqual(fixture.fixtureWindowBounds({x:0,y:0,width:1024,height:728}),{x:16,y:16,width:992,height:696});
 for(const area of [{x:NaN,y:0,width:1024,height:728},{x:0,y:0,width:300,height:200}])assert.throws(()=>fixture.fixtureWindowBounds(area),/work area/);
});

test('fixture drag is genuinely different and stays visible at either screen edge and on a second monitor', () => {
 for(const [area,bounds] of [
  [{x:0,y:0,width:1024,height:728},{x:0,y:80,width:780,height:640}],
  [{x:0,y:0,width:854,height:480},{x:74,y:0,width:780,height:480}],
  [{x:-1280,y:40,width:1280,height:680},{x:-1264,y:118,width:780,height:580}],
 ]){
  const target=fixture.dragTarget(bounds,area);
  assert.ok(fixture.inside(target,area));assert.notDeepEqual(target,bounds);
 }
 const full={x:0,y:0,width:780,height:640};
 assert.throws(()=>fixture.dragTarget(full,full),/meaningful drag/);
 assert.throws(()=>fixture.dragTarget({...full,x:-20},full),/off-screen/);
});

test('responsive review fixture keeps a real visible avatar gap at multiple sizes and zooms', async () => {
 const {reviewPreviewBounds}=await import('../../dist-electron/profile-preview.js');
 for(const [width,height,zoom] of [[992,656,1],[822,408,1],[744,492,1.25],[600,380,1.5]]){
  const layout=fixture.reviewFixtureLayout({width,height},zoom);
  assert.ok(layout.left+layout.width<=width&&layout.top+layout.height<=height);
  assert.ok(layout.avatarOffset>=332&&layout.avatarOffset+48<=layout.width);
  const parent={x:16,y:48,width:Math.round(width*zoom),height:Math.round(height*zoom)};
  const area={x:0,y:0,width:parent.width+48,height:parent.height+80};
  const anchor={left:layout.left*zoom,top:layout.top*zoom,avatarLeft:(layout.left+layout.avatarOffset)*zoom};
  const placed=reviewPreviewBounds(parent,area,anchor);
  assert.ok(fixture.inside(placed,area));
  assert.ok(placed.x+placed.width<=parent.x+anchor.avatarLeft-10);
  assert.ok(Math.abs(placed.x-parent.x-anchor.left)<=1);
 }
 assert.throws(()=>fixture.reviewFixtureLayout({width:320,height:200}),/cannot fit/);
});

test('production placement reproduces the old fixed-position assertion failure on a small screen', () => {
 // Exercise the unchanged production geometry without starting a browser or
 // claiming that a simulated native window is a Windows integration run.
 const area={x:0,y:0,width:1024,height:728},native={screen:{getDisplayMatching:()=>({workArea:area})}};
 const exports={};
 const source=readFileSync(new URL('../src/floating-web-page.ts',import.meta.url),'utf8');
 const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
 runInNewContext(compiled,{exports,require:name=>{if(name==='electron')return native;if(name==='./account-user-agent.js')return {};throw new Error('unexpected import: '+name)}});
 function place(parent){
  let actual;
  const page=new exports.FloatingWebPage(()=>{},{width:660,height:578});
  page.win={isDestroyed:()=>false,getContentBounds:()=>parent};
  page.popup={isDestroyed:()=>false,setBounds:bounds=>{actual=bounds}};
  page.top=120;page.place();return actual;
 }
 const oversized={x:0,y:32,width:1500,height:930},clamped=place(oversized);
 assert.equal(clamped.y,150);assert.notEqual(clamped.y,oversized.y+120);
 assert.ok(fixture.inside(clamped,area),'production correctly clamps even though the old test would fail');
 const outer=fixture.fixtureWindowBounds(area),parent={x:outer.x,y:outer.y+32,width:outer.width,height:outer.height-32};
 const ready=place(parent);
 assert.ok(fixture.inside(ready,area));assert.equal(ready.y,parent.y+120);
 assert.ok(ready.y+ready.height<=parent.y+parent.height);
});

test('native bounds wait ignores intermediate geometry and requires a stable accepted result', async () => {
 let clock=0;
 const final={x:80,y:90,width:660,height:520};
 const win={isDestroyed:()=>false,getBounds:()=>({...final,x:clock<60?0:80})};
 const bounds=await fixture.settledBounds(win,b=>fixture.near(b,final),{now:()=>clock,sleep:async ms=>{clock+=ms}});
 assert.deepEqual(bounds,final);assert.ok(clock>=140);
});

test('wrong or continually changing geometry fails instead of weakening the placement checks', async () => {
 for(const changing of [false,true]){
  let clock=0;
  const win={isDestroyed:()=>false,getBounds:()=>({x:changing?clock:0,y:0,width:660,height:520})};
  await assert.rejects(fixture.settledBounds(win,changing?()=>true:b=>b.x===70,{timeout:200,now:()=>clock,sleep:async ms=>{clock+=ms}}),/did not settle/);
 }
 await assert.rejects(fixture.settledBounds({isDestroyed:()=>true}),/destroyed/);
});

test('fixture setup restores the original parent on success and renderer setup failure', async () => {
 const area={x:0,y:0,width:1024,height:728},original={x:5,y:5,width:1000,height:710};
 for(const fail of [false,true]){
  let current={...original};const requests=[];
  const win={isDestroyed:()=>false,getBounds:()=>({...current}),setBounds:value=>{current={...value};requests.push({...value})},getContentBounds:()=>current,
   webContents:{getZoomFactor:()=>1,executeJavaScript:async()=>{if(fail)throw new Error('fixture renderer failed');return {width:current.width,height:current.height,documentWidth:current.width}}}};
  if(fail)await assert.rejects(fixture.prepareFixtureWindow(win,{getDisplayMatching:()=>({workArea:area})}),/fixture renderer failed/);
  else{const restore=await fixture.prepareFixtureWindow(win,{getDisplayMatching:()=>({workArea:area})});assert.ok(fixture.inside(current,area));await restore();}
  assert.deepEqual(current,original);assert.equal(requests.length,2);
 }
});

test('small-screen fixture temporarily releases and restores the desktop minimum size, including on failure', async () => {
 const original={x:0,y:0,width:1516,height:1017},area={x:0,y:0,width:1024,height:728};
 for(const fail of [false,true]){
  let bounds={...original},minimum=[1500,960];
  const win={isDestroyed:()=>false,getBounds:()=>({...bounds}),getMinimumSize:()=>[...minimum],
   setMinimumSize:(width,height)=>{minimum=[width,height]},
   setBounds:value=>{bounds={...value,width:Math.max(value.width,minimum[0]),height:Math.max(value.height,minimum[1])}},
   getContentBounds:()=>bounds,webContents:{getZoomFactor:()=>1,executeJavaScript:async()=>{if(fail)throw new Error('fixture load failed');return {width:bounds.width,height:bounds.height,documentWidth:bounds.width}}}};
  if(fail)await assert.rejects(fixture.prepareFixtureWindow(win,{getDisplayMatching:()=>({workArea:area})}),/fixture load failed/);
  else{const restore=await fixture.prepareFixtureWindow(win,{getDisplayMatching:()=>({workArea:area})});assert.ok(fixture.inside(bounds,area));await restore();}
  assert.deepEqual(bounds,original);assert.deepEqual(minimum,[1500,960]);
 }
});
