const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {waitFixtureDocument}=require('./navigation-fixture.cjs');
const {waitRendererFixture}=require('./renderer-fixture.cjs');
// setBounds reaches Chromium asynchronously. A ratio-only condition can accept
// the old viewport, then a second measurement lands halfway through the resize.
// Keep viewport, both columns and overflow in one bounded renderer snapshot.
async function waitWhatsAppGridLayout(wc,width,ratio,{timeoutMs=5000}={}){
 let last;
 const source=`(()=>{
  const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {left:r.left,right:r.right,width:r.width}};
  const left=rect('#contacts'),right=rect('#conversation');
  return {viewportWidth:innerWidth,scrollWidth:document.documentElement.scrollWidth,
   display:getComputedStyle(document.querySelector('#app')).display,navWidth:rect('nav').width,
   left,right,columnRatio:left.width/(left.width+right.width),availableRatio:left.width/(innerWidth-64)};
 })()`;
 try{
  return await waitRendererFixture({
   isDestroyed:()=>wc.isDestroyed?.(),
   executeJavaScript:async script=>{
    last=await wc.executeJavaScript(script);
    return last.viewportWidth===width&&last.display==='grid'&&last.navWidth===64&&
     last.scrollWidth<=width&&Math.abs(last.columnRatio-ratio)<.01&&
     Math.abs(last.availableRatio-ratio)<.01&&Math.abs(last.left.right-last.right.left)<2?last:false;
   }
  },source,{label:`WhatsApp grid layout at ${width}px, ratio ${ratio}`,timeoutMs});
 }catch(error){error.message+='; last geometry: '+JSON.stringify(last);throw error}
}
module.exports=async({host,owner,temp})=>{
 const id='native:dddddddd-dddd-4ddd-8ddd-dddddddddddd';
 const {createWhatsAppSession}=await import('../../dist-electron/whatsapp-page.js');
 const ses=await createWhatsAppSession(owner,id,'');
 const html=`<meta charset="utf-8"><style>
 *{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#111b21;color:#eee;font:15px sans-serif}
 #app{display:flex;width:100%;height:100%;position:relative}nav{flex:0 0 64px;background:#202c33}
 #app::before{content:"";position:absolute;left:calc(64px + 40%);top:0;bottom:0;width:0;border-left:1px solid #82959e;pointer-events:none;z-index:2}
 #contacts{flex:0 0 40%;min-width:300px;display:flex;flex-direction:column;background:#111b21}
 #side,.archive{display:flex;flex:1;min-height:0;flex-direction:column}
 .left-header,header{padding:20px;flex:none;background:#202c33}#pane-side,.archive-rows{flex:1;overflow:auto;padding:16px}
 #conversation{flex:1;min-width:360px;display:flex}#main{display:flex;flex:1;min-width:0;flex-direction:column}
 #messages{flex:1;overflow:auto;min-height:0;padding:20px}.message-in{margin-top:100px}.bubble{max-width:90%;background:#202c33;padding:12px;overflow-wrap:anywhere}
 footer{padding:15px;background:#202c33}input,[contenteditable]{width:100%;min-height:38px;background:#2a3942;color:white;border:0;padding:10px}button{padding:12px;color:white;background:#075e54;border:0}
 @media(max-width:600px){#contacts{flex:0 0 45%;min-width:0}#conversation{min-width:0}}
 </style><div id="app"><nav>WA</nav><div id="contacts"><section id="side"><header class="left-header">WhatsApp<input placeholder="搜索联系人"></header><div id="pane-side"><button id="archive">查看已归档的聊天</button></div><footer class="left-footer">联系人列表底部</footer></section></div><div id="conversation"><main id="main"><header class="chat-header">Alice</header><div id="messages"><div class="message-in"><div class="bubble"><span class="selectable-text copyable-text">Hello, this message should wrap with the whole conversation column when its width changes.</span></div></div></div><footer class="chat-footer"><div role="textbox" contenteditable="true">尚未发送的草稿</div></footer></main></div></div>
 <script>window.loads=1;window.sent=0;document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.isContentEditable)window.sent++});window.archive=()=>{const c=document.querySelector('#contacts');c.outerHTML='<div id="contacts" style="flex:0 0 40%;min-width:300px;display:flex;flex-direction:column"><section class="archive"><header class="left-header"><button id="back">返回</button> 已归档</header><div class="archive-rows"><button id="archived-chat">Alice</button></div><footer class="left-footer">归档列表底部</footer></section></div>';document.querySelector('#archived-chat').onclick=()=>{document.querySelector('#main header').textContent='Archived Alice';document.querySelector('.selectable-text').textContent='Hello from the archive';};document.querySelector('#back').onclick=()=>location.reload();};document.querySelector('#archive').onclick=window.archive;</script>`;
 ses.protocol.handle('https',()=>new Response(html,{headers:{'content-type':'text/html'}}));
 let p,wc;
 const wait=script=>waitRendererFixture(wc,script,{label:'WhatsApp layout: '+script,timeoutMs:5000});
 const show=(width=1150,height=760)=>host.show(p,{x:150,y:80,width,height});
 const geometry=()=>wc.executeJavaScript(`(()=>{const r=s=>{const x=document.querySelector(s).getBoundingClientRect();return {left:x.left,right:x.right,width:x.width,top:x.top,bottom:x.bottom}};return {left:r('#contacts'),right:r('#conversation'),header:r('.chat-header'),footer:r('.chat-footer'),leftHeader:r('.left-header'),leftFooter:r('.left-footer'),nav:r('nav'),count:document.querySelectorAll('[data-juxin-wa-splitter]').length,draft:document.querySelector('[contenteditable]').textContent}})()`);
 const drag=async(delta)=>{
  const pos=await wc.executeJavaScript(`(()=>{const r=document.querySelector('[data-juxin-wa-splitter]').getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+120)}})()`);
  wc.sendInputEvent({type:'mouseMove',...pos});wc.sendInputEvent({type:'mouseDown',...pos,button:'left',clickCount:1,modifiers:['leftButtonDown']});
  for(let i=1;i<=8;i++){wc.sendInputEvent({type:'mouseMove',x:pos.x+Math.round(delta*i/8),y:pos.y,button:'left',modifiers:['leftButtonDown']});await new Promise(r=>setTimeout(r,12));}
  wc.sendInputEvent({type:'mouseUp',x:pos.x+delta,y:pos.y,button:'left',clickCount:1});await new Promise(r=>setTimeout(r,180));
 };
 try{
  await host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'});p=host.profiles.get(id);wc=p.pages.get(p.selected).view.webContents;show();
  await waitFixtureDocument(wc,'https://web.whatsapp.com/',{label:'WhatsApp initial layout fixture',timeoutMs:10000});
  await host.readUnread(p);await wait("!!document.querySelector('[data-juxin-wa-splitter]')");
  const before=await geometry();await drag(-160);let after=await geometry();
  assert.ok(Math.abs(after.left.width-(before.left.width-160))<4,'real pointer drag resizes the contacts column: '+JSON.stringify({before,after}));
  assert.equal(after.nav.width,64);assert.equal(after.count,1);assert.equal(after.draft,before.draft);
  assert.equal(await wc.executeJavaScript("getComputedStyle(document.querySelector('#app'),'::before').borderLeftColor"),'rgba(0, 0, 0, 0)','old native border no longer remains at its default percentage');
  assert.ok(Math.abs(after.left.right-after.right.left)<2,'contacts and conversation remain adjacent after dragging');
  for(const area of ['header','footer'])assert.ok(Math.abs(after[area].width-after.right.width)<1);
  for(const area of ['leftHeader','leftFooter'])assert.ok(Math.abs(after[area].width-after.left.width)<1);
  const ratio=after.left.width/(after.left.width+after.right.width);
  await wc.executeJavaScript('window.archive()');await wait("!!document.querySelector('.archive')&&document.querySelectorAll('[data-juxin-wa-splitter]').length===1");
  await new Promise(r=>setTimeout(r,180));after=await geometry();
  assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-ratio)<.01,'archive replacement keeps the ratio');
  await drag(130);after=await geometry();const archivedRatio=after.left.width/(after.left.width+after.right.width);
  assert.ok(archivedRatio>ratio+.08,'archive column itself remains draggable');
  const bounds=await wc.executeJavaScript("(()=>{const r=document.querySelector('#archived-chat').getBoundingClientRect();return {x:Math.round(r.left+20),y:Math.round(r.top+20)}})()");
  wc.sendInputEvent({type:'mouseDown',...bounds,button:'left',clickCount:1});wc.sendInputEvent({type:'mouseUp',...bounds,button:'left',clickCount:1});
  await wait("document.querySelector('#main header').textContent==='Archived Alice'");
  const settings={enabled:true,outgoing:false,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',provider:'google',region:''};
  const translation=step=>{show();return host.control('chat-translation',{profile:id,owner,generation:p.generation,step})};
  const batch=await translation({kind:'poll',settings});assert.deepEqual(batch.messages.map(x=>x.original),['Hello from the archive']);
  await translation({kind:'apply',conversation:batch.conversation,...batch.messages[0],translation:'来自归档聊天的问候'});
  assert.equal((await translation({kind:'export'})).messages[0].translation,'来自归档聊天的问候');
  after=await geometry();assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-archivedRatio)<.01);
  show(900);await new Promise(r=>setTimeout(r,180));after=await geometry();assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-archivedRatio)<.01);
  assert.equal(await wc.executeJavaScript('document.documentElement.scrollWidth<=innerWidth'),true);
  show(450);await new Promise(r=>setTimeout(r,180));assert.equal(await wc.executeJavaScript("document.querySelectorAll('[data-juxin-wa-splitter]').length"),0,'narrow native layout remains usable');
  show();await wait("!!document.querySelector('[data-juxin-wa-splitter]')");after=await geometry();assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-archivedRatio)<.01);
  if(temp)fs.writeFileSync(path.join(temp,'whatsapp-resized-archive.png'),(await wc.capturePage()).toPNG());
  await wc.loadURL('https://web.whatsapp.com/');await host.readUnread(p);await wait("!!document.querySelector('[data-juxin-wa-splitter]')");
  after=await geometry();assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-archivedRatio)<.01,'reload retains preference in normal list');
  await host.closeProfile(p);await host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'});p=host.profiles.get(id);wc=p.pages.get(p.selected).view.webContents;show();
  await waitFixtureDocument(wc,'https://web.whatsapp.com/',{label:'WhatsApp reopened layout fixture',timeoutMs:10000});
  await host.readUnread(p);await wait("!!document.querySelector('[data-juxin-wa-splitter]')");
  after=await geometry();assert.ok(Math.abs(after.left.width/(after.left.width+after.right.width)-archivedRatio)<.01,'window reopen retains preference');
  // Reproduce the reported archive + welcome page: neither #side nor #main
  // exists. Previously the archive fixture always kept #main, hiding the bug.
  const columnRatio=()=>wc.executeJavaScript("(()=>{const a=document.querySelector('#contacts').getBoundingClientRect(),b=document.querySelector('#conversation').getBoundingClientRect();return a.width/(a.width+b.width)})()");
  await wc.executeJavaScript("const normal=document.querySelector('#contacts').cloneNode(true);normal.removeAttribute('style');window.normalList=normal.outerHTML;window.chatMarkup=document.querySelector('#conversation').innerHTML;document.querySelector('#conversation').innerHTML='<section class=welcome style=flex:1>欢迎使用 WhatsApp</section>';window.archive()");
  await wait("!document.querySelector('#side,#main')&&document.querySelectorAll('[data-juxin-wa-splitter]').length===1");
  await wait(`Math.abs(document.querySelector('#contacts').getBoundingClientRect().width/(document.querySelector('#contacts').getBoundingClientRect().width+document.querySelector('#conversation').getBoundingClientRect().width)-${archivedRatio})<.01`);
  assert.ok(Math.abs(await columnRatio()-archivedRatio)<.01,'archive welcome keeps the saved ratio without side or main');
  await drag(-60);const welcomeRatio=await columnRatio();assert.ok(welcomeRatio<archivedRatio-.03,'archive welcome divider remains draggable');
  await wc.executeJavaScript("document.querySelector('#contacts').outerHTML=window.normalList");
  await wait(`Math.abs(document.querySelector('#contacts').getBoundingClientRect().width/(document.querySelector('#contacts').getBoundingClientRect().width+document.querySelector('#conversation').getBoundingClientRect().width)-${welcomeRatio})<.01`);
  assert.ok(Math.abs(await columnRatio()-welcomeRatio)<.01,'returning from archive to normal list preserves the archive width');
  // A late native paint layer must not survive at the old split.
  await wc.executeJavaScript("const layer=document.createElement('div');layer.id='late-boundary';layer.style.cssText='position:absolute;left:calc(64px + 40%);top:0;bottom:0;width:1px;background:linear-gradient(#82959e,#82959e);pointer-events:none';document.querySelector('#app').append(layer)");
  await wait("getComputedStyle(document.querySelector('#late-boundary')).backgroundImage==='none'");
  assert.equal(await wc.executeJavaScript("getComputedStyle(document.querySelector('#late-boundary')).backgroundImage"),'none','late native boundary is cleared after navigation');
  await wc.executeJavaScript("document.querySelector('#conversation').innerHTML=window.chatMarkup");
  await wait("!!document.querySelector('#main')&&document.querySelectorAll('[data-juxin-wa-splitter]').length===1");
  // Grid-based revisions must reflow from the host's available width. Two
  // fixed pixel tracks kept their old total forever when the window shrank.
  await wc.executeJavaScript("const app=document.querySelector('#app');app.style.display='grid';app.style.gridTemplateColumns='64px 40% minmax(0,1fr)'");
  await waitWhatsAppGridLayout(wc,1150,welcomeRatio);
  show(900);
  const shrunkGrid=await waitWhatsAppGridLayout(wc,900,welcomeRatio);
  assert.ok(Math.abs(shrunkGrid.columnRatio-welcomeRatio)<.01,'grid host shrink keeps ratio without horizontal overflow');
  assert.ok(shrunkGrid.scrollWidth<=shrunkGrid.viewportWidth,'shrunk grid has no horizontal overflow');
  await wc.executeJavaScript('window.archive()');
  await wait("!!document.querySelector('.archive')");
  await waitWhatsAppGridLayout(wc,900,welcomeRatio);
  await drag(50);const gridArchiveRatio=await columnRatio();
  assert.ok(gridArchiveRatio>welcomeRatio+.03,'grid archive remains draggable');
  show();
  const restoredGrid=await waitWhatsAppGridLayout(wc,1150,gridArchiveRatio);
  assert.ok(Math.abs(restoredGrid.columnRatio-gridArchiveRatio)<.01,'grid host restore keeps the archive ratio');
  assert.ok(restoredGrid.scrollWidth<=restoredGrid.viewportWidth,'restored grid has no horizontal overflow');
  const other=await createWhatsAppSession(owner,'native:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee','');assert.notEqual(other.storagePath,ses.storagePath);
  assert.equal(await wc.executeJavaScript('window.sent'),0);assert.equal(wc.debugger.isAttached(),false);
  console.log('PASS full WhatsApp columns: real drag, native boundary cleanup including late paint, headers/footers, archive welcome/normal transitions, inline translation/export, flex/grid narrow/wide viewport, refresh/reopen and draft preserved');
 }finally{if(p&&!p.closed)await host.closeProfile(p);ses.protocol.unhandle('https')}
};
module.exports.waitWhatsAppGridLayout=waitWhatsAppGridLayout;
