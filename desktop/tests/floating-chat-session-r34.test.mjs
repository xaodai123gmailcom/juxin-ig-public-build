import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';

// Actual classes with controlled Electron windows and clock. Draft values stand
// for in-memory DOM state, not a claim about ChatGPT's current live selectors.
function compile(name){
  const source=readFileSync(new URL('../src/'+name+'.ts',import.meta.url),'utf8')
    .replace(/^import .*;\r?\n/gm,'').replace(/\bexport (?=(?:type|class))/g,'');
  return nodeModule.stripTypeScriptTypes
    ?nodeModule.stripTypeScriptTypes(source,{mode:'transform'})
    :nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:1}}).outputText;
}
const floatingSource=compile('floating-web-page'),chatgptSource=compile('chatgpt-page');

function fixture(){
  let now=0,serial=0;
  const timers=new Map(),windows=[],sessions=new Map(),states=[];
  const session={fromPartition(name){
    if(!sessions.has(name))sessions.set(name,{name,setUserAgent(){},getUserAgent:()=> 'test Chromium',setPermissionRequestHandler(){},setPermissionCheckHandler(){}});
    return sessions.get(name);
  }};
  class Window extends EventEmitter{
    constructor(options={}){
      super();this.options=options;this.destroyed=false;this.visible=false;this.bounds={x:0,y:0,width:1400,height:1000};
      const wc=this.webContents=new EventEmitter();
      Object.assign(wc,{session:options.webPreferences?.session||session.fromPartition('shell'),draft:'',loadCount:0,
        isDestroyed:()=>this.destroyed,focus(){},setUserAgent(){},getURL:()=>wc.url||'about:blank',
        setWindowOpenHandler(handler){wc.openHandler=handler},reload(){wc.loadCount++;wc.draft=''},
      });windows.push(this);
    }
    isDestroyed(){return this.destroyed}
    setMenu(){}
    getContentBounds(){return this.bounds}
    getBounds(){return this.bounds}
    setBounds(bounds){this.bounds=bounds}
    setTitle(){}
    show(){this.visible=true}
    hide(){this.visible=false}
    focus(){}
    loadURL(url){this.webContents.url=url;this.webContents.loadCount++;this.webContents.draft='';return Promise.resolve()}
    destroy(){this.destroyed=true;this.visible=false;this.webContents.draft='';if(!this.delayClosed)this.emit('closed')}
  }
  const values=runInNewContext(floatingSource+';'+chatgptSource+';({FloatingWebPage,ChatGPTPage})',{
    BrowserWindow:Window,session,Menu:{buildFromTemplate:value=>value},shell:{openExternal:async()=>{}},
    screen:{getDisplayMatching:()=>({workArea:{x:0,y:0,width:1920,height:1080}})},
    accountUserAgent:value=>value,URL,
    setTimeout(fn,delay){const id={n:++serial,unref(){}};timers.set(id,{fn,at:now+delay});return id},
    clearTimeout(id){timers.delete(id)},
  });
  const parent=new Window(),page=new values.ChatGPTPage(value=>states.push(value));
  return {parent,page,windows,states,timers,session,...values,
    advance(ms){now+=ms;for(const[id,timer]of [...timers])if(timer.at<=now){timers.delete(id);timer.fn()}},
    login(popup=page.popup){const child=new Window({webPreferences:{session:popup.webContents.session}});child.visible=true;popup.webContents.emit('did-create-window',child);return child},
  };
}

test('hidden ChatGPT retains its draft and login child beyond the old five-minute eviction',()=>{
  const f=fixture();try{
    f.page.toggle(f.parent);const popup=f.page.popup,wc=popup.webContents,login=f.login();
    wc.draft='unfinished message';login.webContents.draft='pending login';
    f.page.hide();assert.equal(popup.visible,false);assert.equal(login.visible,false);f.advance(6*60*1000);
    assert.equal(popup.isDestroyed(),false,'hiding a chat is not consent to discard its unsent input');
    assert.equal(login.isDestroyed(),false);assert.equal(f.timers.size,0);
    f.page.toggle(f.parent);assert.equal(f.page.popup,popup);assert.equal(wc.loadCount,1);
    assert.equal(wc.draft,'unfinished message');assert.equal(login.webContents.draft,'pending login');
    assert.equal(login.visible,true);assert.equal(popup.options.webPreferences.backgroundThrottling,true);
  }finally{f.page.dispose()}
});

test('default floating tools keep their bounded idle eviction policy',()=>{
  const f=fixture(),tool=new f.FloatingWebPage(()=>{},{partition:'translate',url:'https://translate.google.com/',title:'Translate',width:700,height:700});
  try{
    tool.toggle(f.parent);const popup=tool.popup;tool.hide();f.advance(5*60*1000);
    assert.equal(popup.isDestroyed(),true);assert.equal(f.timers.size,0);
  }finally{tool.dispose();f.page.dispose()}
});

test('explicit disposal releases the retained chat and login children',()=>{
  const f=fixture();f.page.toggle(f.parent);const popup=f.page.popup,login=f.login();
  f.page.hide();f.page.dispose();
  assert.equal(popup.isDestroyed(),true);assert.equal(login.isDestroyed(),true);
  assert.equal(f.page.children.size,0);assert.equal(f.page.opened,false);assert.equal(f.timers.size,0);
});

test('account partition switch cannot reuse the old draft or be hidden by its late close',()=>{
  const f=fixture();try{
    f.page.setPartition('persist:chatgpt-owner-a');f.page.toggle(f.parent);
    const old=f.page.popup,login=f.login();old.delayClosed=true;login.delayClosed=true;old.webContents.draft='account A';
    f.page.setPartition('persist:chatgpt-owner-b');f.page.toggle(f.parent);const current=f.page.popup;
    assert.equal(old.isDestroyed(),true);assert.equal(login.isDestroyed(),true);assert.notEqual(current,old);
    assert.notEqual(current.webContents.session,old.webContents.session);assert.equal(current.webContents.draft,'');
    old.emit('closed');login.emit('closed');assert.equal(f.page.popup,current);assert.equal(f.page.opened,true);
  }finally{f.page.dispose()}
});

test('a login child created by an obsolete page is closed without entering the new account',()=>{
  const f=fixture();try{
    f.page.setPartition('persist:chatgpt-owner-a');f.page.toggle(f.parent);const old=f.page.popup;
    f.page.setPartition('persist:chatgpt-owner-b');f.page.toggle(f.parent);
    assert.equal(old.webContents.openHandler({url:'https://auth.openai.com/login'}).action,'deny');
    const child=f.login(old);assert.equal(child.isDestroyed(),true);
    assert.equal(f.page.children.size,0);assert.equal(f.page.opened,true);
  }finally{f.page.dispose()}
});

test('parent shutdown disposal releases hidden chat instead of leaving an orphan',()=>{
  const f=fixture();f.page.toggle(f.parent);const popup=f.page.popup,login=f.login();
  f.parent.on('closed',()=>f.page.dispose());f.page.hide();f.parent.destroy();
  assert.equal(popup.isDestroyed(),true);assert.equal(login.isDestroyed(),true);assert.equal(f.timers.size,0);
  assert.equal(f.page.opened,false);assert.equal(f.page.win,undefined);
});
