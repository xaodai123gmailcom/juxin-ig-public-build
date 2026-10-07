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
 const task=profile('task'),primary=page(task,'primary'),source=page(task,'source'),collector=profile('collection'),collectionPage=page(collector,'collection'),nurture=profile('nurture'),nurturePage=page(nurture,'nurture');
 const label=(p=task,target='primary',extra={})=>host.control('label-task-page',{profile:p.id,target,role:'task',...extra});
 return {host,win,metrics,task,primary,source,collector,collectionPage,nurture,nurturePage,page,label,bounds:{x:309,y:159,width:1275,height:756}};
}
test('collection and nurture keep stable task coordinates through pane changes',async()=>{
 const f=fixture();f.host.setWorkspaceBounds(f.bounds);await f.label();await flush();
 for(const p of [f.primary,f.source,f.collectionPage,f.nurturePage])assert.deepEqual(plain(p.view.bounds),{x:0,y:0,width:1280,height:900});
 f.host.setWorkspaceBounds({...f.bounds,width:950,height:600});await flush();
 assert.deepEqual(plain(f.primary.view.bounds),{x:0,y:0,width:1280,height:900});
});
test('retired and arbitrary viewport modes reject before labels or geometry mutate',async()=>{
 const f=fixture();const before=plain(f.primary.view.bounds);
 for(const viewport_mode of ['posting','anything',null])await assert.rejects(f.label(f.task,'primary',{viewport_mode}),/不受支持/);
 assert.equal(f.primary.role,undefined);assert.deepEqual(plain(f.primary.view.bounds),before);
});
test('current worker relabel preserves sibling pages and manual target selection',async()=>{
 const f=fixture();await f.label();f.page(f.task,'replacement');await f.label(f.task,'replacement');
 assert.equal(f.primary.role,undefined);assert.equal(f.task.pages.get('replacement').role,'task');
 assert.equal(f.task.selected,'primary');assert.equal(f.task.pages.size,3);
});
