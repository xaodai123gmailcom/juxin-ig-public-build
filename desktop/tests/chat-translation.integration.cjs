const assert=require('node:assert/strict');
module.exports=async({host,owner})=>{
 const p=await host.ensure('native:cccccccc-cccc-4ccc-8ccc-cccccccccccc',owner,'',true),wc=[...p.pages.values()][0].view.webContents;
 let html='';p.session.protocol.handle('https',()=>new Response(html,{headers:{'content-type':'text/html;charset=utf-8'}}));
 const settings={enabled:true,outgoing:true,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',provider:'google',region:''};
 const step=input=>{host.show(p,{x:250,y:90,width:1000,height:760});return host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:input})};
 const read=()=>step({kind:'poll',settings});
 for(const site of ['whatsapp','instagram']){
  html=site==='whatsapp'?'<main id="main"><header>Alice</header><div class="message-in"><span class="selectable-text copyable-text"><span dir="auto">Hello</span></span></div><footer><div><div role="textbox" contenteditable="true"></div></div></footer></main>':'<nav><div role="row"><span dir="auto">Sidebar preview</span></div></nav><main><div role="row"><span dir="auto">Hello</span></div><div><div role="textbox" contenteditable="true"></div></div></main>';
  await wc.loadURL(site==='whatsapp'?'https://web.whatsapp.com/':'https://www.instagram.com/direct/t/123/');
  await wc.executeJavaScript("window.sent=[];document.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.defaultPrevented){sent.push(document.querySelector('[contenteditable]').textContent);e.preventDefault()}})");
  let batch=await read();assert.deepEqual(batch.messages.map(m=>m.original),['Hello']);
  const message=batch.messages[0];await step({kind:'apply',conversation:batch.conversation,...message,translation:'你好 <script>literal</script>'});
  assert.equal(await wc.executeJavaScript("document.querySelector('[data-juxin-chat-translation]').textContent"),'你好 <script>literal</script>');
  assert.equal(await wc.executeJavaScript("document.querySelectorAll('[data-juxin-chat-translation] script').length"),0);
  assert.equal((await step({kind:'export'})).messages[0].translation,'你好 <script>literal</script>');
  // Appearance changes update existing and older annotations without translating
  // again, changing the website's original text, or rebuilding the chat input.
  await wc.executeJavaScript("window.originalMessage=document.querySelector('.selectable-text,[dir=auto]');window.originalFont=originalMessage.style.fontSize;window.chatBox=document.querySelector('[contenteditable]');const old=document.createElement('div');old.dataset.juxinChatTranslation='true';old.dataset.appearanceFixture='true';old.textContent='旧译文';document.querySelector('main').append(old);true");
  const styled=await step({kind:'poll',settings:{...settings,color:'#ff6699',fontSize:28}});
  assert.equal(styled.messages.length,0,'appearance must not queue translated messages again');
  assert.deepEqual(await wc.executeJavaScript("Array.from(document.querySelectorAll('[data-juxin-chat-translation]')).map(el=>[getComputedStyle(el).color,getComputedStyle(el).fontSize])"),[['rgb(255, 102, 153)','28px'],['rgb(255, 102, 153)','28px']]);
  assert.equal(await wc.executeJavaScript("originalMessage.style.fontSize===originalFont&&chatBox===document.querySelector('[contenteditable]')"),true);
  await wc.executeJavaScript("document.querySelector('[data-appearance-fixture]').remove();true");
  await read();
  assert.equal(await wc.executeJavaScript("getComputedStyle(document.querySelector('[data-juxin-chat-translation]')).fontSize"),'12px');
  // A service cooldown is visible in the actual bubble and recovers in place.
  await step({kind:'apply',conversation:batch.conversation,...message,error:true,retryable:true,retryAt:Date.now()+60000,status:'翻译服务限流'});
  assert.match(await wc.executeJavaScript("document.querySelector('[data-juxin-chat-status]').textContent"),/自动重试/);
  await step({kind:'apply',conversation:batch.conversation,...message,translation:'你好，已恢复'});
  assert.equal(await wc.executeJavaScript("document.querySelectorAll('[data-juxin-chat-status]').length"),0);
  assert.equal((await step({kind:'export'})).messages[0].translation,'你好，已恢复');
  await wc.executeJavaScript("document.querySelector('[contenteditable]').textContent='你好';document.querySelector('[contenteditable]').focus()");
  // Polling calls host.show(), which may move the native focus away from this
  // WebContentsView. Finish the poll first, then focus the actual input page
  // before testing its native Enter dispatch. Wait for the DOM acknowledgement
  // instead of assuming the Windows renderer delivered the key in 60 ms.
  await read();host.window().focus();wc.focus();
  await wc.executeJavaScript("document.querySelector('[contenteditable]').focus();true");
  wc.sendInputEvent({type:'keyDown',keyCode:'Enter'});
  let sent=[];
  for(let attempt=0;attempt<20;attempt++){
   sent=await wc.executeJavaScript('sent');
   if(sent.length)break;
   await new Promise(r=>setTimeout(r,50));
  }
  assert.deepEqual(sent,['你好'],'native Enter must reach the focused chat input');
  batch=await read();
  assert.equal(batch.draft,null);assert.deepEqual(await wc.executeJavaScript('sent'),['你好']);
  assert.equal((await step({kind:'apply',conversation:batch.conversation,id:'draft-1',original:'你好',draft:true,translation:'Hello'})).applied,false);
  assert.equal(await wc.executeJavaScript("document.querySelector('[contenteditable]').textContent"),'你好');
  await step({kind:'poll',settings:{...settings,enabled:false}});assert.equal(await wc.executeJavaScript("document.querySelectorAll('[data-juxin-chat-translation]').length"),0);assert.equal((await read()).messages.length,1);
  const stale=await read();await wc.executeJavaScript(site==='whatsapp'?"document.querySelector('header').textContent='Bob'":"history.pushState({},'', '/direct/t/456/')");
  assert.equal((await step({kind:'apply',conversation:stale.conversation,...stale.messages[0],translation:'Must not enter other conversation'})).applied,false);
 }
 // Current IG inbox layouts need not expose role=row or a nested main.
 // Contact previews share the same outer main; only the composer column counts.
 for(const lexical of [false,true]){
  html=`<style>body{margin:0}main{display:grid;grid-template-columns:300px 1fr;height:700px}section{display:flex;flex-direction:column}#messages{flex:1;overflow:auto;padding:12px}.bubble{margin:12px 0}[contenteditable]{min-height:40px;border:1px solid}header{height:50px}</style><main><div id="contacts"><div dir="auto">Contact preview must stay private</div><div role="row"><span dir="auto">Another contact</span></div></div><section><header><span dir="auto">Contact display name</span></header><div id="messages"><time dir="auto">Yesterday</time><div class="bubble"><div ${lexical?'data-lexical-text="true"':'dir="auto"'}>Magandang umaga.</div></div><div class="bubble"><span dir="auto">¿Cómo estás?</span></div><div class="bubble"><div dir="auto">สวัสดีครับ</div></div><div role="toolbar"><span dir="auto">Reply</span></div></div><footer><div><div contenteditable="true" ${lexical?'data-lexical-editor="true"':'role="textbox"'}></div></div></footer></section></main>`;
  await wc.loadURL('https://www.instagram.com/direct/t/modern-'+lexical+'/');
  let batch=await read();assert.deepEqual(batch.messages.map(m=>m.original),['สวัสดีครับ','¿Cómo estás?','Magandang umaga.']);assert.equal(batch.status,'');
  for(const [i,message] of batch.messages.entries())await step({kind:'apply',conversation:batch.conversation,...message,translation:['你好。','你好吗？','早上好。'][i]});
  assert.equal((await read()).messages.length,0,'own annotations never become new translation input');
  assert.equal(await wc.executeJavaScript("document.querySelectorAll('#contacts [data-juxin-chat-translation],header [data-juxin-chat-translation]').length"),0);
  await wc.executeJavaScript("document.querySelector('[contenteditable]').textContent='keep my unsent draft';document.querySelector('[contenteditable]').focus();document.querySelector('#messages').insertAdjacentHTML('beforeend','<div class=\"bubble\"><div dir=\"auto\">Bonjour, how are you?</div></div>')");
  batch=await read();assert.deepEqual(batch.messages.map(m=>m.original),['Bonjour, how are you?']);
  await step({kind:'apply',conversation:batch.conversation,...batch.messages[0],translation:'你好，你怎么样？'});
  assert.equal(await wc.executeJavaScript("document.activeElement.textContent"),'keep my unsent draft','incoming annotations do not steal composer focus');
  // React can recycle a message node. Remove the old translation before reuse.
  await wc.executeJavaScript("document.querySelector('#messages .bubble [dir=auto],#messages .bubble [data-lexical-text]').textContent='Guten Morgen.'");
  batch=await read();assert.deepEqual(batch.messages.map(m=>m.original),['Guten Morgen.']);
  assert.equal((await step({kind:'export'})).messages[0].translation,'');
  await step({kind:'poll',settings:{...settings,enabled:false}});assert.equal(await wc.executeJavaScript("document.querySelectorAll('[data-juxin-chat-translation]').length"),0);
 }
 console.log('PASS rowless/lexical IG bilingual bubbles, per-message languages, shared-main contact exclusion, dynamic messages, recycled nodes and draft focus');
 // Message grid cells may contain plain spans without dir/lexical attributes.
 html='<main><header><span>Contact profile</span></header><div role="row"><div role="gridcell"><div><span>Chào buổi sáng.</span></div></div></div><div role="row"><div role="gridcell"><p>Доброе утро.</p></div></div><div><div role="textbox" contenteditable="true"></div></div></main>';
 await wc.loadURL('https://www.instagram.com/direct/t/gridcells/');
 assert.deepEqual((await read()).messages.map(m=>m.original),['Доброе утро.','Chào buổi sáng.']);
 html='<style>#history{height:90px;overflow:auto}.message-in{min-height:32px}footer{height:50px}</style><main id="main"><header>History</header><div id="history">'+Array.from({length:60},(_,i)=>'<div class="message-in"><span class="selectable-text copyable-text">History message '+i+'</span></div>').join('')+'</div><footer><div><div role="textbox" contenteditable="true"></div></div></footer></main>';
 await wc.loadURL('https://web.whatsapp.com/');
 await wc.executeJavaScript('document.querySelector("#history").scrollTop=10000;true');
 let visibleBatch=await read();assert.equal(visibleBatch.messages[0].original,'History message 59');assert.ok(visibleBatch.messages.length<10);
 for(const message of visibleBatch.messages)await step({kind:'apply',conversation:visibleBatch.conversation,...message,translation:'历史译文'});
 assert.equal((await read()).messages.length,0,'finished viewport must not drain offscreen history');
 await wc.executeJavaScript('document.querySelector("#history").scrollTop=0;true');
 assert.ok((await read()).messages.some(m=>m.original==='History message 0'),'scrolling exposes history on demand');
 console.log('PASS nested WhatsApp text, plain IG grid cells, newest-first visible messages and no offscreen history requests');
 html='<main><div role="row"><span dir="auto">Hello</span></div><div><div role="textbox" contenteditable="true"></div></div></main>';
 await wc.loadURL('https://www.instagram.com/direct/t/guards/');await read();
 await assert.rejects(host.control('chat-translation',{profile:p.id,owner:'22222222-2222-4222-8222-222222222222',generation:p.generation,step:{kind:'poll',settings}}));
 host.show(p,{x:250,y:90,width:1000,height:760},undefined,true);await assert.rejects(host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:{kind:'poll',settings}}));
 host.hide();await assert.rejects(host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:{kind:'poll',settings}}));
 const hiddenExport=await host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:{kind:'export'}});assert.equal(hiddenExport.messages[0].original,'Hello');
 await assert.rejects(host.control('chat-translation',{profile:p.id,owner:'foreign',generation:p.generation,step:{kind:'export'}}));
 await assert.rejects(host.control('chat-translation',{profile:p.id,owner,generation:p.generation-1,step:{kind:'export'}}));
 const lease={};p.clients.add(lease);await assert.rejects(host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:{kind:'export'}}));p.clients.delete(lease);
 console.log('PASS hidden editor export reads only owned idle profile; stale/foreign and task clients still blocked');
 await host.closeProfile(p);console.log('PASS actual Instagram/WhatsApp inline translation, escaped text, export, native Enter send without interception, draft translation rejected, toggle, conversation fencing and owner/read-only/hidden guards');
};
