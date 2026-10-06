import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as nodeModule from 'node:module';
import {runInNewContext} from 'node:vm';
import {EventEmitter} from 'node:events';

// Execute production callbacks with controlled native lifetimes. No Windows
// toast display or live WhatsApp DOM is claimed by this suite.
const transform=source=>(nodeModule.stripTypeScriptTypes?nodeModule.stripTypeScriptTypes(source,{mode:'transform'}):nodeModule.createRequire(import.meta.url)('typescript').transpileModule(source,{compilerOptions:{target:9,module:99}}).outputText).replace(/^export /gm,'');
const read=name=>readFileSync(new URL('../src/'+name,import.meta.url),'utf8');
const activity=transform(read('message-activity-page.ts'));
const main=read('main.ts');
const notifications=transform(main.slice(main.indexOf('function closeMessageToast(')>=0?main.indexOf('function closeMessageToast('):main.indexOf('function clearNotificationSession('),main.indexOf('const messageReminders=')));
const windowEvents=transform(read('window-events.ts').replace(/^import .*;\r?\n/gm,''));

function nativeFixture(){
 const sent=[],made=[];let dead=false,sendRace=false,closeError=false,constructError=false,showError=false;
 class Toast extends EventEmitter{
  static isSupported(){return true}
  constructor(options){super();if(constructError)throw new Error('native notification unavailable');this.options=options;made.push(this)}
  close(){if(closeError)throw new Error('Object has been destroyed');this.emit('close')}
  show(){if(showError)throw new Error('native notification show failed');this.emit('show')}
 }
 const profile={name:'account',closed:false};
 const win={isDestroyed:()=>false,isMinimized:()=>false,restore(){},show(){},focus(){},webContents:{isDestroyed:()=>dead,send(...args){if(dead||sendRace)throw new Error('Object has been destroyed');sent.push(args)}}};
 const context={Notification:Toast,Error,Map,Set,Date,process:{platform:'linux'},app:{},shell:{},ensureNotificationRegistration(){},
  activeSessionToken:'session',activeSessionOwner:'owner',notificationEpoch:0,notificationProfiles:new Set(['account']),notificationRegistered:true,notificationError:'',notificationLastShown:0,toasts:new Map(),notificationActivations:new Map(),
  embeddedBrowser:{profiles:new Map([['account',profile]])},messageReminders:{enabled:true,reset(){}},mainWindow:win,
  chatTranslation:{reset(){}},googleTranslator:{hide(){}},chatgptPage:{dispose(){}},reviewPage:undefined};
 runInNewContext(windowEvents+'\n'+notifications+'\nglobalThis.remind=sendMessageReminder;globalThis.clearSession=clearNotificationSession;',context);
 return {context,made,sent,profile,send:()=>context.remind('account',1),setDead:v=>dead=v,setRace:v=>sendRace=v,setCloseError:v=>closeError=v,setConstructError:v=>constructError=v,setShowError:v=>showError=v};
}

test('notification click survives a destroyed renderer and destruction racing native send',()=>{
 for(const mode of ['dead','race']){const f=nativeFixture();f.send();if(mode==='dead')f.setDead(true);else f.setRace(true);assert.doesNotThrow(()=>f.made[0].emit('click'));assert.equal(f.sent.length,0)}
});
test('replaced or disabled notifications cannot route a later click or overwrite current status',()=>{
 const f=nativeFixture();f.send();const old=f.made[0];f.send();const current=f.made[1];
 old.emit('failed');old.emit('click');assert.equal(f.context.notificationError,'');assert.equal(f.sent.length,0);
 current.emit('click');assert.equal(f.sent.length,1);
 f.context.messageReminders.enabled=false;current.emit('click');assert.equal(f.sent.length,1);
});
test('a reopened account generation cannot be selected by its old toast',()=>{
 const f=nativeFixture();f.send();f.context.embeddedBrowser.profiles.set('account',{name:'reopened',closed:false});
 f.made[0].emit('click');assert.equal(f.sent.length,0);
});
test('a naturally closed Action Center reminder stays actionable until explicitly replaced or cleared',()=>{
 const f=nativeFixture();f.send();const first=f.made[0];first.emit('close');
 assert.equal(f.context.toasts.size,0,'do not retain the native toast');
 first.emit('click');assert.equal(f.sent.length,1);
 f.send();first.emit('click');assert.equal(f.sent.length,1,'replacement revokes its old activation');
 const second=f.made[1];second.emit('close');f.context.clearSession();
 assert.equal(f.context.notificationActivations.size,0);second.emit('click');assert.equal(f.sent.length,1);
});
test('a native notification close failure cannot abort replacement or session cleanup',()=>{
 const f=nativeFixture();f.send();f.setCloseError(true);
 assert.doesNotThrow(()=>f.send());assert.equal(f.made.length,2);
 assert.doesNotThrow(()=>f.context.clearSession());assert.equal(f.context.toasts.size,0);assert.equal(f.context.activeSessionOwner,null);
 f.made[0].emit('click');f.made[1].emit('click');assert.equal(f.sent.length,0);
});
test('native notification construction and show failures remain an explicit local status',()=>{
 for(const phase of ['construct','show']){const f=nativeFixture();if(phase==='construct')f.setConstructError(true);else f.setShowError(true);assert.doesNotThrow(()=>f.send());assert.match(f.context.notificationError,/通知/);assert.equal(f.context.toasts.size,0)}
});

test('incoming activity keeps the stable chat title when online or typing status changes',()=>{
 let name='Alice',status='online',ids=['a1','a2'];
 const header={get textContent(){return name+status},querySelectorAll:()=>[{get textContent(){return name},getAttribute:()=>name}]};
 const main={querySelector:()=>header,querySelectorAll:()=>ids.map(id=>({closest:()=>({getAttribute:()=>id})}))};
 const context={location:{hostname:'web.whatsapp.com'},crypto:{randomUUID:()=> 'document'},document:{querySelector:()=>main,querySelectorAll:()=>[]}};
 const observe=runInNewContext(activity+';messageActivityPage',context);
 assert.equal(observe().serial,0);status='typing';ids.push('a3');assert.equal(observe().serial,1);
 status='online';assert.equal(observe().serial,1);
 name='Bob';ids=['b1','b2'];assert.equal(observe().serial,1,'another conversation starts a baseline');
 status='last seen today';ids.push('b3');assert.equal(observe().serial,2);
 assert.deepEqual(Object.keys(observe()).sort(),['epoch','serial']);
});
