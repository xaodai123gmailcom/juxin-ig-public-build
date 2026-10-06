const assert=require('node:assert/strict');
/** Instagram retains its native layout; only WhatsApp exposes a drag divider. */
module.exports=async({host,owner})=>{
 const p=await host.ensure('native:12121212-1212-4212-8212-121212121212',owner,'',true);
 p.session.protocol.handle('https',()=>new Response(`<meta charset="utf-8"><style>
 *{box-sizing:border-box}body{margin:0}#app{display:grid;grid-template-columns:64px 340px minmax(0,1fr);height:700px}
 #contacts{min-width:280px;background:#eee}#chat{min-width:360px;display:flex;flex-direction:column}
 #messages{flex:1}header,footer{height:70px}a{display:block}
 </style><div id="app"><nav>IG</nav><section id="contacts"><header>联系人</header><a href="/direct/t/fixture/">Test chat</a></section><main id="chat"><header>Test chat</header><div id="messages"><div dir="auto">Hello from Instagram</div></div><footer><div role="textbox" contenteditable="true">保留草稿</div></footer></main></div>
 <script>localStorage.setItem('juxin.instagram.column-ratio.v1','0.8')</script>`,{headers:{'content-type':'text/html'}}));
 let wc,phase='open';
 const show=()=>host.show(p,{x:150,y:80,width:1000,height:700});
 const bounded=async(pending,ms=1500)=>{let timer;try{return await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('renderer operation timed out')),ms)})])}finally{clearTimeout(timer)}};
 const evaluate=script=>bounded(wc.executeJavaScript(script));
 const isolated=code=>bounded(wc.executeJavaScriptInIsolatedWorld(1005,[{code}]));
 const wait=async(stage,check)=>{
  phase=stage;const until=Date.now()+5000;
  while(Date.now()<until){show();if(await bounded(Promise.resolve().then(check),Math.min(1500,until-Date.now())))return;await new Promise(r=>setTimeout(r,30));}
  throw new Error('Instagram chat check failed at '+stage);
 };
 const geometry=()=>evaluate(`(()=>{
  const c=document.querySelector('#contacts'),chat=document.querySelector('#chat'),app=document.querySelector('#app'),message=document.querySelector('#messages [dir]');
  return {ready:document.readyState,viewport:[innerWidth,innerHeight],dividers:document.querySelectorAll('[data-juxin-wa-splitter]').length,
   contacts:c?.getBoundingClientRect().width,chat:chat?.getBoundingClientRect().width,inlineGrid:app?.style.gridTemplateColumns,inlineWidth:c?.style.width,
   selection:message?getComputedStyle(message).userSelect:null,draft:document.querySelector('[contenteditable]')?.textContent,
   oldRatio:localStorage.getItem('juxin.instagram.column-ratio.v1')};
 })()`);
 const checkNativeLayout=async(stage)=>{
  await wait(stage,async()=>await isolated("Boolean(globalThis.__juxinChatColumnsV2)")&&await evaluate("innerWidth===1000&&!!document.querySelector('#messages')"));
  const actual=await geometry();
  assert.equal(actual.dividers,0,'Instagram must not receive the WhatsApp divider');
  assert.equal(actual.contacts,340,'old saved IG ratios must not override native widths');
  assert.equal(actual.inlineGrid,'');assert.equal(actual.inlineWidth,'');
  assert.equal(actual.selection,'text','Instagram message selection remains available');
  assert.equal(actual.draft,'保留草稿');assert.equal(actual.oldRatio,'0.8','retired preference need not be deleted');
 };
 try{
  const page=await host.newPage(p,'https://www.instagram.com/direct/t/fixture/');wc=page.view.webContents;show();
  await checkNativeLayout('native-layout');
  phase='clicked-message';
  const {contextMessage}=await import('../../dist-electron/web-page-menu.js');
  const xy=await evaluate("(()=>{const r=document.querySelector('#messages [dir]').getBoundingClientRect();return {x:r.left+10,y:r.top+8}})()");
  assert.equal(await bounded(wc.executeJavaScriptInIsolatedWorld(1006,[{code:`(${contextMessage.toString()})(${xy.x},${xy.y})`}])),'Hello from Instagram');
  phase='task-pause';await isolated("globalThis.__juxinChatColumnsV2.setEnabled(false)");
  assert.equal(await evaluate("document.querySelectorAll('[data-juxin-wa-splitter]').length"),0);
  await isolated("globalThis.__juxinChatColumnsV2.setEnabled(true)");await checkNativeLayout('task-resume');
  phase='reload';await bounded(wc.loadURL('https://www.instagram.com/direct/t/fixture/'),10000);show();
  await checkNativeLayout('reload-native-layout');
  console.log('PASS Instagram native widths, no injected divider, text selection, preserved draft and clicked-message extraction');
 }catch(error){
  console.error('CHECK Instagram chat failure details',JSON.stringify({phase,page:wc&&!wc.isDestroyed()?await geometry().catch(()=>({unavailable:true})):null}));
  throw new Error('Instagram chat ['+phase+']: '+error.message,{cause:error});
 }finally{p.session.protocol.unhandle('https');await host.closeProfile(p)}
};
