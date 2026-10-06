const assert=require('node:assert/strict');
const {session,WebContentsView,screen}=require('electron');
const {inside,near,settledBounds,prepareFixtureWindow,dragTarget}=require('./floating-fixture.cjs');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {rendererFixtureRead}=require('./renderer-fixture.cjs');
const {closeFixtureContents}=require('./task-watch-fixture.cjs');
module.exports=async({win,timeoutMs=2000})=>{
 const restore=await prepareFixtureWindow(win,screen);
 let ses,translator,account,accountContents,protocolInstalled=false,failed=false;
 try {
 const {GoogleTranslator}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/google-translator.js')));
 ses=session.fromPartition('persist:translation');
 ses.protocol.handle('https',()=>new Response('<title>Translation fixture</title><textarea aria-label="翻译文字"></textarea>',{headers:{'content-type':'text/html;charset=utf-8'}}));protocolInstalled=true;
 const events=[];translator=new GoogleTranslator(v=>events.push(v));
 console.log('CHECK translator create and local fixture load');
 await translator.toggle(win);const popup=translator.popup;
 const deadline=Date.now()+5000;while(popup.webContents.getTitle()!=='Translation fixture'&&Date.now()<deadline)await new Promise(r=>setTimeout(r,30));
 assert.equal(popup.webContents.getTitle(),'Translation fixture');assert.equal(new URL(popup.webContents.getURL()).hostname,'translate.google.com');
 assert.equal(popup.getParentWindow(),win);assert.equal(popup.isModal(),false);assert.equal(popup.isResizable(),true);
 await rendererFixtureRead(popup.webContents,"document.querySelector('textarea').value='draft'",{label:'translator draft write'});
 translator.setTop(120);const home=await settledBounds(popup),parent=win.getContentBounds(),area=screen.getDisplayMatching(parent).workArea;
 assert.ok(inside(home,area),'translator stays in the visible work area');
 assert.ok(Math.abs(home.y-Math.max(area.y,Math.min(parent.y+120,area.y+area.height-home.height)))<=1,'translator keeps the requested top unless the work area clamps it');
 assert.ok(Math.abs(home.height-Math.min(578,Math.max(260,parent.height-132),area.height))<=1,'translator retains its actual height rule');
 assert.ok(Math.abs(home.width-Math.min(660,Math.max(320,parent.width-24),area.width))<=1,'translator retains its actual width rule');
 assert.ok(Math.abs(home.x-Math.max(area.x,Math.min(parent.x+parent.width-home.width-12,area.x+area.width-home.width)))<=1,'translator opens at the bounded upper-right');
 const target=dragTarget(home,area,90);popup.setBounds(target);const moved=await settledBounds(popup,b=>inside(b,area)&&near(b,target));
 assert.notDeepEqual(moved,home,'native window must actually move before raise is checked');
 account=new WebContentsView();accountContents=account.webContents;assert.ok(accountContents,'account fixture has native contents before attachment');win.contentView.addChildView(account);translator.raise();assert.deepEqual(await settledBounds(popup),moved);
 await translator.toggle(win);assert.equal(translator.opened,false);assert.equal(popup.isVisible(),false);
 await translator.toggle(win);assert.equal(translator.popup,popup);assert.deepEqual(await settledBounds(popup),home);
 assert.equal(await rendererFixtureRead(popup.webContents,"document.querySelector('textarea').value",{label:'translator retained draft'}),'draft');
 popup.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});await new Promise(r=>setTimeout(r,80));assert.equal(translator.opened,false);
 await translator.toggle(win);popup.close();assert.equal(translator.opened,false);assert.equal(popup.isDestroyed(),false);
 await translator.toggle(win);const wc=popup.webContents;translator.dispose();const closeDeadline=Date.now()+2000;while(!wc.isDestroyed()&&Date.now()<closeDeadline)await new Promise(r=>setTimeout(r,20));assert.equal(wc.isDestroyed(),true);assert.equal(translator.opened,false);
 await closeFixtureContents(accountContents,{label:'translator account view',timeoutMs});assert.equal(events.at(-1),false);
 console.log('PASS movable nonmodal translator resets to upper-right on every open, preserves drafts, keeps dragged bounds above account, supports native close/Escape and disposal');
 } catch(error) {failed=true;throw error;} finally {
  const cleanupErrors=[];
  const cleanup=async(label,operation)=>{try{await operation();}catch(error){cleanupErrors.push(new Error('Translator fixture cleanup failed: '+label,{cause:error}));}};
  await cleanup('translator',()=>translator?.dispose());
  await cleanup('account view attachment',()=>{if(account&&!win.isDestroyed())win.contentView.removeChildView(account);});
  // Electron may clear the view getter when contents close; retain the object
  // captured at creation so cleanup still checks actual native destruction.
  await cleanup('account view contents',()=>{if(accountContents)return closeFixtureContents(accountContents,{label:'translator account cleanup',timeoutMs});});
  await cleanup('protocol',()=>{if(protocolInstalled)ses.protocol.unhandle('https');});
  await cleanup('parent geometry',restore);
  if(cleanupErrors.length){if(failed)for(const error of cleanupErrors)console.error(error);else throw new AggregateError(cleanupErrors,'Translator fixture cleanup failed');}
 }
};
