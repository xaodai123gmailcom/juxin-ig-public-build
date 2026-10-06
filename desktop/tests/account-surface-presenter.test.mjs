import test from 'node:test';
import assert from 'node:assert/strict';
import {AccountSurfacePresenter} from '../../dist-electron/account-surface-presenter.js';
function fixture(authorize) {
 let grant='',serial=0;
 const state={url:'file:///app/index.html#/accounts',session:'owner',visible:false,destroyed:false,contentsDestroyed:false,contentsMissing:false};
 const host={captureSurface:async()=>state.visible?'data:image/png;base64,fixture':'',hide(){grant='';state.visible=false},setWorkspaceBounds(){},requestSurface(){return grant=String(++serial)},hasSurfaceGrant(value){return value===grant&&!!grant},show(value){if(this.hasSurfaceGrant(value)){state.visible=true;return {attached:true}}return {attached:false}}};
 const checkAlive=()=>{if(state.destroyed||state.contentsDestroyed||state.contentsMissing)throw new Error('Object has been destroyed')};
 const contents={isDestroyed:()=>state.contentsDestroyed,getZoomFactor:()=>{checkAlive();return 1},getURL:()=>{checkAlive();return state.url}};
 const win={getContentBounds:()=>{checkAlive();return {width:1000,height:800}},get webContents(){return state.contentsMissing?null:contents},isDestroyed:()=>state.destroyed,isMinimized:()=>false};
 const presenter=new AccountSurfacePresenter(host,()=>state.session,(body,token)=>authorize(body,token,host));
 const input=()=>({id:'one',visible:true,documentUrl:state.url,bounds:{x:200,y:80,width:800,height:720}});
 return {state,host,win,presenter,input};
}
test('hide is immediate during authorization and stale completion cannot restore the pane',async()=>{
 let release;
 const f=fixture(async(body,token,host)=>{await new Promise(r=>release=r);return host.show(body.grant)});
 const pending=f.presenter.update(f.win,f.input());await Promise.resolve();
 await f.presenter.update(f.win,{visible:false});assert.equal(f.state.visible,false);
 release();assert.equal((await pending).attached,false);assert.equal(f.state.visible,false);
});
test('departed-route messages and queued shows cannot appear over the new page',async()=>{
 let release;const calls=[];
 const f=fixture(async(body,token,host)=>{calls.push(body);await new Promise(r=>release=r);return host.show(body.grant)});
 const old=f.input();const pending=f.presenter.update(f.win,old);await Promise.resolve();
 const queued=f.presenter.update(f.win,old);f.state.url='file:///app/index.html#/posting';f.host.hide();
 assert.equal((await f.presenter.update(f.win,old)).attached,false);
 release();assert.equal((await pending).attached,false);assert.equal((await queued).attached,false);
 assert.equal(calls.length,1);assert.equal(f.state.visible,false);
});
test('a fresh route still needs authorization; authorization error hides the surface',async()=>{
 let fail=false;
 const f=fixture(async(body,token,host)=>{assert.equal(token,'owner');if(fail)throw new Error('task locked');return host.show(body.grant)});
 assert.equal((await f.presenter.update(f.win,f.input())).attached,true);
 fail=true;await assert.rejects(f.presenter.update(f.win,f.input()),/task locked/);assert.equal(f.state.visible,false);
});
test('session changes during authorization cannot leave the old account visible',async()=>{
 let release;
 const f=fixture(async(body,token,host)=>{await new Promise(r=>release=r);return host.show(body.grant)});
 const pending=f.presenter.update(f.win,f.input());await Promise.resolve();
 f.state.session=null;release();assert.equal((await pending).attached,false);assert.equal(f.state.visible,false);
});
for(const flag of ['destroyed','contentsDestroyed','contentsMissing']) {
 test(`late show and hide are harmless when ${flag}`,async()=>{
  let calls=0;
  const f=fixture(async(body,token,host)=>{calls++;return host.show(body.grant)});
  const input=f.input();assert.equal((await f.presenter.update(f.win,input)).attached,true);
  f.state[flag]=true;
  assert.equal((await f.presenter.update(f.win,input)).attached,false);
  assert.equal((await f.presenter.update(f.win,{visible:false})).attached,false);
  assert.equal(calls,1);assert.equal(f.state.visible,false);
 });
 test(`queued and in-flight shows cannot survive ${flag}`,async()=>{
  let release,calls=0;
  const f=fixture(async(body,token,host)=>{calls++;await new Promise(r=>release=r);return host.show(body.grant)});
  const input=f.input(),pending=f.presenter.update(f.win,input);await Promise.resolve();
  const queued=f.presenter.update(f.win,input);f.state[flag]=true;release();
  assert.equal((await pending).attached,false);assert.equal((await queued).attached,false);
  assert.equal(calls,1);assert.equal(f.state.visible,false);
 });
}

test('old component cleanup cannot hide the next account or return its image',async()=>{
 const f=fixture(async(body,token,host)=>host.show(body.grant));
 await f.presenter.update(f.win,{...f.input(),surfaceId:'old'});
 await f.presenter.update(f.win,{...f.input(),id:'two',surfaceId:'new'});
 const result=await f.presenter.update(f.win,{id:'one',surfaceId:'old',visible:false,capture:true});
 assert.equal(f.state.visible,true);assert.equal(result.preview,undefined);
 assert.equal(f.presenter.currentPlanId(),'two');
});
test('matching overlay hide is immediate and returns a frame without exposing the native view',async()=>{
 const f=fixture(async(body,token,host)=>host.show(body.grant));
 await f.presenter.update(f.win,{...f.input(),surfaceId:'one'});
 const pending=f.presenter.update(f.win,{surfaceId:'one',visible:false,capture:true});
 assert.equal(f.state.visible,false);
 assert.match((await pending).preview,/^data:image/);
 assert.equal(f.presenter.currentPlanId(),undefined);
});
