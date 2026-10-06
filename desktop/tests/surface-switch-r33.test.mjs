import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';
const compile=name=>{
 const source=readFileSync(new URL('../src/'+name,import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
 return (nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
};
const rectangle=runInNewContext(compile('account-surface-policy.ts')+';surfaceRectangle');
const Presenter=runInNewContext(compile('account-surface-presenter.ts')+';AccountSurfacePresenter',{AbortController,surfaceRectangle:rectangle});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
function fixture(){
 const state={grant:'',serial:0,shown:'',owner:'owner',url:'file:///app/#accounts',hidden:0};
 const requests=[];
 const host={hide(){state.hidden++;state.shown='';state.grant=''},captureSurface:async()=>'',setWorkspaceBounds(){},requestSurface(){return state.grant=String(++state.serial)},hasSurfaceGrant:g=>Boolean(g&&g===state.grant)};
 const contents={isDestroyed:()=>false,getZoomFactor:()=>1,getURL:()=>state.url};
 const win={isDestroyed:()=>false,isMinimized:()=>false,webContents:contents,getContentBounds:()=>({width:1200,height:900})};
 const presenter=new Presenter(host,()=>state.owner,(body,_token,signal)=>{const gate=deferred();requests.push({body,signal,...gate});return gate.promise.then(()=>{const attached=host.hasSurfaceGrant(body.grant);if(attached)state.shown=body.id;return{attached}})});
 const input=id=>({id,surfaceId:'surface-'+id,visible:true,documentUrl:state.url,bounds:{x:100,y:100,width:1000,height:700}});
 return{state,requests,host,win,presenter,input,async release(){for(let i=0;i<15;i++){requests.forEach(r=>r.resolve());await flush()}}};
}

test('a newer window is authorized without waiting for a departed window response',async()=>{
 const f=fixture(),old=f.presenter.update(f.win,f.input('old'));await flush();
 const next=f.presenter.update(f.win,f.input('next'));await flush();
 try{
  assert.equal(f.requests.length,2,'a stale window response must not hold up the selected window');
  assert.equal(f.requests[0].signal.aborted,true);f.requests[1].resolve();
  assert.equal((await next).attached,true);assert.equal((await old).attached,false);
  f.requests[0].resolve();await flush();assert.equal(f.state.shown,'next');
 }finally{await f.release();await old;await next}
});

test('switching selection immediately removes the previously interactive account pane',async()=>{
 const f=fixture();const old=f.presenter.update(f.win,f.input('old'));await flush();f.requests[0].resolve();await old;
 assert.equal(f.state.shown,'old');const next=f.presenter.update(f.win,f.input('next'));
 try{assert.equal(f.state.shown,'','the old account cannot remain interactive under the newly selected account');}
 finally{await flush();f.requests.at(-1).resolve();await next}
});

test('hiding cancels only the matching surface request and settles its caller',async()=>{
 const f=fixture(),pending=f.presenter.update(f.win,f.input('one'));await flush();
 await f.presenter.update(f.win,{surfaceId:'surface-old',documentUrl:f.state.url,visible:false});
 assert.notEqual(f.requests[0].signal?.aborted,true);
 await f.presenter.update(f.win,{surfaceId:'surface-one',documentUrl:f.state.url,visible:false});
 let settled=false;void pending.then(()=>{settled=true});await flush();
 try{assert.equal(settled,true);assert.equal(f.requests[0].signal.aborted,true)}
 finally{f.requests[0].resolve();await pending}
});

test('a burst of superseded window updates starts only the latest queued authorization',async()=>{
 const f=fixture(),first=f.presenter.update(f.win,f.input('first'));await flush();
 const queued=[];for(let i=0;i<100;i++)queued.push(f.presenter.update(f.win,f.input('next-'+i)));
 for(let i=0;i<20;i++)await flush();
 try{
  assert.equal(f.requests.length,2);assert.equal(f.requests[1].body.id,'next-99');
  f.requests[1].resolve();await Promise.all(queued);await first;
  assert.equal(f.state.shown,'next-99');
 }finally{await f.release();await first;await Promise.all(queued)}
});

test('same-surface heartbeat preserves the displayed pane and still checks authorization',async()=>{
 const f=fixture();const first=f.presenter.update(f.win,f.input('one'));await flush();f.requests[0].resolve();await first;
 const hidden=f.state.hidden,next=f.presenter.update(f.win,f.input('one'));await flush();
 assert.equal(f.state.shown,'one');assert.equal(f.state.hidden,hidden);assert.equal(f.requests.length,2);
 f.requests[1].reject(new Error('task owns this window'));await assert.rejects(next,/task owns/);assert.equal(f.state.shown,'');
});
