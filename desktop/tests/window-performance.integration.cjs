const assert=require('node:assert/strict');
module.exports=async({host,owner})=>{
 const p=await host.ensure('native:'+require('node:crypto').randomUUID(),owner,'',true);
 p.session.protocol.handle('https',()=>new Response('<main><div role="row"><span dir="auto">Hello</span></div><div role="textbox" contenteditable="true"></div></main>',{headers:{'content-type':'text/html'}}));
 const pages=[...p.pages.values()];
 for(let i=0;i<3;i++){pages.push(await host.newPage(p,'https://www.instagram.com/direct/t/performance/'));}
 const bounds={x:150,y:100,width:1050,height:720},selected=pages[1];
 host.activate(p,selected);await selected.view.webContents.loadURL('https://www.instagram.com/direct/t/performance/');host.show(p,bounds);
 await new Promise(r=>setTimeout(r,80));
 const patched=[];let sizes=0,reorders=0;
 const wrap=(object,key,count)=>{const original=object[key];object[key]=function(...args){count();return original.apply(this,args)};patched.push(()=>object[key]=original)};
 for(const page of pages)wrap(page.view,'setBounds',()=>sizes++);
 const pane=host.window().contentView.children.find(v=>v.children.includes(selected.view));
 wrap(pane,'addChildView',()=>reorders++);wrap(pane,'setBounds',()=>sizes++);
 for(let i=0;i<30;i++){host.setWorkspaceBounds(bounds);host.show(p,bounds);}
 assert.equal(sizes,0,'unchanged authorized heartbeats never resize any account page');assert.equal(reorders,0,'unchanged heartbeats never reorder native children');
 host.show(p,{...bounds,width:1000});assert.ok(sizes>0);for(const undo of patched)undo();
 const wc=selected.view.webContents;
 const settings={enabled:true,outgoing:false,incomingLang:'zh-CN',outgoingLang:'en',color:'#93c5fd',provider:'google',region:''};
 const poll=()=>{host.show(p,bounds);return host.control('chat-translation',{profile:p.id,owner,generation:p.generation,step:{kind:'poll',settings}})};
 await poll();
 await wc.executeJavaScriptInIsolatedWorld(1002,[{code:"globalThis.scans=0;const root=document.querySelector('main'),query=root.querySelectorAll.bind(root);root.querySelectorAll=(...args)=>{globalThis.scans++;return query(...args)};void 0"}]);
 for(let i=0;i<12;i++)await poll();
 assert.equal(await wc.executeJavaScriptInIsolatedWorld(1002,[{code:'globalThis.scans'}]),0,'unchanged messages reuse the DOM index');
 await wc.executeJavaScript("document.querySelector('span').textContent='New message'");
 assert.equal((await poll()).messages[0].original,'New message','new content is picked up on next poll');
 for(let i=0;i<pages.length;i++)await host.control('label-task-page',{profile:p.id,target:pages[i].targetId,role:i?'screening':'source'});
 await assert.rejects(host.control('close-task-page',{profile:p.id,owner:'foreign',generation:p.generation,target:selected.targetId}));
 await assert.rejects(host.control('close-task-page',{profile:p.id,owner,generation:p.generation-1,target:selected.targetId}));
 await host.control('close-task-page',{profile:p.id,owner,generation:p.generation,target:selected.targetId});
 assert.equal(wc.isDestroyed(),true);assert.equal(p.closed,false);assert.equal(p.pages.size,3);
 assert.ok(pages.filter(x=>x!==selected).every(x=>!x.view.webContents.isDestroyed()),'only the requested native page is destroyed');
 assert.equal((await host.control('watch-profile',{profile:p.id,owner})).pages.some(x=>x.id===selected.targetId),false);
 // Background task page creation/navigation/destruction cannot steal manual input.
 const foreground=pages[0],fwc=foreground.view.webContents;
 await fwc.loadURL('https://www.instagram.com/direct/t/typing/');host.activate(p,foreground);host.show(p,bounds);host.window().focus();fwc.focus();
 await fwc.executeJavaScript("document.querySelector('[contenteditable]').textContent='draft';document.querySelector('[contenteditable]').focus();void 0");
 const background=await host.ensure('native:'+require('node:crypto').randomUUID(),owner,'',true);
 background.session.protocol.handle('https',()=>new Response('<input id="task"><script>document.querySelector("input").focus()</script>',{headers:{'content-type':'text/html'}}));
 let taskLoads=0;
 for(let i=0;i<8;i++){
  const taskPage=await host.newPage(background,'https://www.instagram.com/task/'+i);host.activate(background,taskPage);
  await taskPage.view.webContents.executeJavaScript('document.readyState');
  assert.equal(fwc.isDestroyed(),false);assert.equal(fwc.isFocused(),true,'foreground input remains focused during task page churn');
  assert.equal(await fwc.executeJavaScript("document.activeElement.getAttribute('contenteditable')"),'true');
  taskPage.view.webContents.close({waitForBeforeUnload:false});taskLoads++;
 }
 assert.equal(await fwc.executeJavaScript("document.querySelector('[contenteditable]').textContent"),'draft');
 await host.closeProfile(background);
 console.log('PASS '+taskLoads+' background task page create/activate/close cycles preserve foreground focus and draft');
 console.log('PASS 30 heartbeats with four pages: zero native resizes/reorders; stable translation index, next-poll new text; exact single-page destruction with ownership/generation guards and surviving siblings');
 await host.closeProfile(p);
};
