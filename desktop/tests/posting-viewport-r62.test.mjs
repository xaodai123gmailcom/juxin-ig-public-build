import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
import {EventEmitter} from 'node:events';
const ts=createRequire(import.meta.url)('typescript');
const compile=name=>ts.transpileModule(readFileSync(new URL('../src/'+name,import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,''),{compilerOptions:{target:9,module:99}}).outputText.replace(/^export /gm,'');
const accountViewport=runInNewContext(compile('account-viewport.ts')+';accountViewport');
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
function fixture(){
 let serial=0;const metrics=[];
 class Contents extends EventEmitter{
  id=++serial;dead=false;debugger={isAttached:()=>true,sendCommand:async(method,params)=>{if(method==='Emulation.setDeviceMetricsOverride')metrics.push({id:this.id,...params});return {}}};
  isDestroyed(){return this.dead}getURL(){return 'https://www.instagram.com/'}getTitle(){return 'fixture'}
 }
 class View{
  children=[];bounds={x:0,y:0,width:1280,height:900};webContents=new Contents();
  getBounds(){return {...this.bounds}}setBounds(b){this.bounds={...b}}setVisible(){}setBorderRadius(){}
  addChildView(v){this.children=this.children.filter(x=>x!==v);this.children.push(v)}removeChildView(v){this.children=this.children.filter(x=>x!==v)}
 }
 const win=Object.assign(new EventEmitter(),{contentView:new View(),webContents:new Contents(),isDestroyed:()=>false,isMinimized:()=>false,getContentSize:()=>[1584,915]});
 const Host=runInNewContext(compile('embedded-browser.ts')+';EmbeddedBrowserHost',{URL,setTimeout,clearTimeout,clearInterval,setImmediate,accountViewport,View,WebContentsView:View,WebSocketServer:class{},AccountUnreadCache:class{},randomBytes:()=>({toString:()=>String(++serial)})});
 const host=new Host(()=>win);host.attachWindow(win);
 const profile=(id,task=true)=>{const p={id,owner:'owner',pages:new Map(),clients:new Set(task?[{}]:[]),closed:false};host.profiles.set(id,p);return p};
 const page=(p,id)=>{const view=new View(),page={targetId:id,view};p.pages.set(id,page);p.selected??=id;host.pageReady.add(view.webContents);host.panes.get(win).addChildView(view);return page};
 const posting=profile('posting'),composer=page(posting,'composer'),source=page(posting,'source'),collector=profile('collection'),collectionPage=page(collector,'collection'),nurture=profile('nurture'),nurturePage=page(nurture,'nurture');
 const label=(p=posting,target='composer',extra={})=>host.control('label-task-page',{profile:p.id,target,role:'task',...extra});
 return {host,win,metrics,posting,composer,source,collector,collectionPage,nurture,nurturePage,page,label,bounds:{x:309,y:159,width:1275,height:756}};
}
test('posting page matches the usable pane while collection, nurture and sibling targets retain their coordinates',async()=>{
 const f=fixture();f.host.setWorkspaceBounds(f.bounds);await f.label(f.posting,'composer',{viewport_mode:'posting'});await flush();
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1275,height:756});
 for(const p of [f.source,f.collectionPage,f.nurturePage])assert.deepEqual(plain(p.view.bounds),{x:0,y:0,width:1280,height:900});
 const metric=f.metrics.filter(x=>x.id===f.composer.view.webContents.id).at(-1);assert.equal(metric.width,1275);assert.equal(metric.height,756);
 assert.equal(f.posting.selected,'composer');assert.equal(f.posting.clients.size,1);
});
test('posting dimensions freeze through relabel, hide and workspace resize; a new posting page gets fresh bounds',async()=>{
 const f=fixture();f.host.setWorkspaceBounds(f.bounds);await f.label(f.posting,'composer',{viewport_mode:'posting'});
 f.host.hide();f.host.setWorkspaceBounds({...f.bounds,width:950,height:600});await f.label();await f.label(f.posting,'composer',{viewport_mode:'posting'});await flush();
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1275,height:756});
 f.page(f.posting,'next');await f.label(f.posting,'next',{viewport_mode:'posting'});await flush();
 assert.deepEqual(plain(f.posting.pages.get('next').view.bounds),{x:0,y:0,width:950,height:600});
 assert.equal(f.posting.selected,'composer','metadata must not select another target');
 assert.equal(f.composer.postingViewport,undefined,'retired task cannot retain posting geometry');
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1280,height:900});
});
test('posting starts at a bounded desktop fallback without a measured workspace',async()=>{
 const f=fixture();await f.label(f.posting,'composer',{viewport_mode:'posting'});await flush();
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1280,height:720});
});
test('role change and connection cleanup discard posting-only geometry; manual and later collector sizes recover',async()=>{
 const f=fixture();f.host.setWorkspaceBounds(f.bounds);await f.label(f.posting,'composer',{viewport_mode:'posting'});
 await f.label(f.posting,'composer',{role:'source'});await flush();assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1280,height:900});
 await f.label(f.posting,'composer',{viewport_mode:'posting'});f.posting.clients.clear();f.host.resetTaskPageViewports(f.posting);
 f.host.visible={profile:f.posting.id,target:'composer'};f.host.setWorkspaceBounds({...f.bounds,width:1000,height:700});await flush();
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1000,height:700});
 f.posting.clients.add({});await f.label();f.host.setWorkspaceBounds(f.bounds);await flush();assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1280,height:900});
});
test('posting mode rejects foreign target, wrong role and idle page before geometry changes',async()=>{
 const f=fixture();await assert.rejects(f.label(f.posting,'collection',{viewport_mode:'posting'}));
 await assert.rejects(f.label(f.posting,'composer',{role:'source',viewport_mode:'posting'}));
 f.posting.clients.clear();await assert.rejects(f.label(f.posting,'composer',{viewport_mode:'posting'}));
 assert.equal(f.composer.postingViewport,undefined);
});

test('a not-yet-ready candidate keeps native posting size but defers metrics until the renderer is ready',async()=>{
 const f=fixture(),wc=f.composer.view.webContents;f.host.pageReady.delete(wc);f.host.setWorkspaceBounds(f.bounds);
 await f.label(f.posting,'composer',{viewport_mode:'posting'});await flush();
 assert.deepEqual(plain(f.composer.view.bounds),{x:0,y:0,width:1275,height:756});assert.equal(f.metrics.some(x=>x.id===wc.id),false);
 f.host.pageReady.add(wc);await f.host.sizePageViewport(f.composer.view);
 assert.equal(f.metrics.find(x=>x.id===wc.id).height,756);
});
test('label waits for its pending metrics without restoring retired metadata after late completion',async()=>{
 const f=fixture();f.host.setWorkspaceBounds(f.bounds);await flush();
 let release;const pending=new Promise(resolve=>{release=resolve});f.composer.view.webContents.debugger.sendCommand=()=>pending;
 let done=false;const label=f.label(f.posting,'composer',{viewport_mode:'posting'}).then(()=>{done=true});await flush();assert.equal(done,false);
 f.posting.clients.clear();f.host.resetTaskPageViewports(f.posting);f.composer.role=undefined;
 release({});await label;assert.equal(f.composer.postingViewport,undefined);assert.equal(f.composer.role,undefined);
});
