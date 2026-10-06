const assert=require('node:assert/strict');
const {session,BrowserWindow,screen,dialog}=require('electron');
const {inside,near,settledBounds,prepareFixtureWindow,dragTarget,reviewFixtureLayout}=require('./floating-fixture.cjs');
const {readReviewRecoveryFixture}=require('./review-recovery-fixture.cjs');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const until=async(check)=>{const deadline=Date.now()+5000;while(Date.now()<deadline){if(await check())return;await pause(20)}throw new Error('Floating page check timed out')};
module.exports=async({win,host})=>{
 const restore=await prepareFixtureWindow(win,screen);
 let gpt,review,closing,closingTool,missingTool,gptSession,reviewSession,releaseSlow;
 let gptProtocol=false,reviewProtocol=false,failed=false,resetCookie=false;
 try {
 const {ChatGPTPage}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/chatgpt-page.js')));
 const {ReviewPage}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/review-page.js')));
 const {sendWindowEvent}=await import(pathToFileURL(path.resolve(__dirname,'../../dist-electron/window-events.js')));
 gptSession=session.fromPartition('persist:chatgpt');
 gptSession.protocol.handle('https',req=>new Response(new URL(req.url).hostname==='chatgpt.com'?'<title>ChatGPT fixture</title><button id="login">登录</button><button id="google">Google</button><textarea></textarea>':'<title>Official login fixture</title><p>Login choice fixture</p>',{headers:{'content-type':'text/html;charset=utf-8'}}));
 gptProtocol=true;
 gpt=new ChatGPTPage(v=>sendWindowEvent(win,'chatgpt:visibility',v));
 await gpt.toggle(win);const popup=gpt.popup,wc=popup.webContents;
 await until(()=>wc.getTitle()==='ChatGPT fixture');
 assert.equal(popup.getParentWindow(),win);assert.equal(popup.isModal(),false);
 assert.equal(await wc.executeJavaScript("typeof window.collectorCore"),'undefined');
 assert.equal(await wc.executeJavaScript("document.querySelector('#login').textContent"),'登录');
 await wc.executeJavaScript("localStorage.setItem('fixture-login','preserved');document.querySelector('textarea').value='draft';window.open('https://auth.openai.com/login','official-login');true");
 await until(()=>gpt.children.size===1);
 const login=[...gpt.children][0];await until(()=>login.webContents.getTitle()==='Official login fixture');
 assert.equal(login.webContents.session,gptSession);assert.equal(login.webContents.getUserAgent(),gptSession.getUserAgent());assert.ok(!/[^\x00-\x7f]/.test(login.webContents.getUserAgent()));assert.equal(await login.webContents.executeJavaScript('Boolean(window.opener)'),true);
 assert.equal(await login.webContents.executeJavaScript('typeof window.collectorCore'),'undefined');
 gpt.hide();assert.equal(login.isVisible(),false);await gpt.toggle(win);assert.equal(login.isVisible(),true);login.close();
 const start=await settledBounds(popup),area=screen.getDisplayMatching(win.getContentBounds()).workArea,target=dragTarget(start,area);
 popup.setBounds(target);const moved=await settledBounds(popup,b=>inside(b,area)&&near(b,target));
 assert.notDeepEqual(moved,start,'native window must actually move before raise is checked');
 gpt.raise();assert.deepEqual(await settledBounds(popup),moved);
 gpt.hide();await gpt.toggle(win);assert.deepEqual(await settledBounds(popup),start);assert.equal(await wc.executeJavaScript("document.querySelector('textarea').value"),'draft');
 gpt.dispose();await gpt.toggle(win);await until(()=>gpt.popup.webContents.getTitle()==='ChatGPT fixture');
 assert.equal(await gpt.popup.webContents.executeJavaScript("localStorage.getItem('fixture-login')"),'preserved');gpt.dispose();
 console.log('PASS actual ChatGPT floating page, official-page login controls untouched, same-session OAuth opener, no app bridge, drag/reopen, popup hide and persisted dedicated session (controlled pages)');

 const partition='persist:review-integration';reviewSession=session.fromPartition(partition);review=new ReviewPage(partition);
 const slow=new Promise(r=>{releaseSlow=r});
 const html=(name,status=200)=>new Response('<meta charset="utf-8"><title>'+name+'</title><h1>'+name+'</h1>',{status,headers:{'content-type':'text/html;charset=utf-8'}});
 reviewSession.protocol.handle('https',async req=>{const name=new URL(req.url).pathname;if(name==='/slow.user/')await slow;if(name==='/missing.user/')return html('4xx Client Error',404);if(name==='/limited.user/')return html('4xx Client Error',429);if(name==='/proxy.error/')return html('4xx Client Error');return html(name)});
 reviewProtocol=true;
 const before=[...host.profiles.values()].map(p=>({id:p.id,pages:[...p.pages.values()].map(a=>a.view.webContents.getURL()),clients:p.clients.size}));
 const result=review.openTarget(win,'slow.user'),reviewWindow=review.popup;
 assert.equal(result.visible,true);assert.equal(reviewWindow.isVisible(),true,'shown before server releases page');
 assert.notEqual(reviewWindow.webContents.getTitle(),'/slow.user/');
 // A second click must be accepted even while the first target has not loaded.
 review.openTarget(win,'next.user');assert.equal(review.popup,reviewWindow);
 await until(()=>reviewWindow.webContents.getTitle()==='/next.user/');releaseSlow();await pause(100);
 assert.equal(new URL(reviewWindow.webContents.getURL()).pathname,'/next.user/');
 assert.deepEqual([...host.profiles.values()].map(p=>({id:p.id,pages:[...p.pages.values()].map(a=>a.view.webContents.getURL()),clients:p.clients.size})),before,'no account page or task connection touched');
 assert.ok([...host.profiles.values()].every(p=>p.session!==reviewSession));
 await reviewWindow.webContents.executeJavaScript("localStorage.setItem('review-login','yes')");
 review.hide();review.openTarget(win,'third.user');assert.equal(review.popup,reviewWindow);await until(()=>reviewWindow.webContents.getTitle()==='/third.user/');
 assert.equal(await reviewWindow.webContents.executeJavaScript("localStorage.getItem('review-login')"),'yes');
 await reviewWindow.webContents.executeJavaScript("location.href='https://www.instagram.com/accounts/login/'");await until(()=>reviewWindow.webContents.getTitle()==='/accounts/login/');
 review.openTarget(win,'third.user');await until(()=>reviewWindow.webContents.getTitle()==='/third.user/');
 assert.throws(()=>review.openTarget(win,'accounts'),/无效/);assert.throws(()=>review.openTarget(win,'bad/name'),/无效/);
 const layout=reviewFixtureLayout(await win.webContents.executeJavaScript('({width:innerWidth,height:innerHeight})'),win.webContents.getZoomFactor());
 await win.webContents.executeJavaScript(`(()=>{const p=${JSON.stringify(layout)};const panel=document.createElement('details');panel.id='review-placement-fixture';panel.className='public-review-list';panel.open=true;panel.style.cssText='position:fixed;box-sizing:border-box;border:0;padding:0;margin:0;left:'+p.left+'px;top:'+p.top+'px;width:'+p.width+'px;height:'+p.height+'px';panel.innerHTML='<span class="quick-review-avatar-head" style="position:absolute;left:'+p.avatarOffset+'px;top:16px">头像</span>';document.body.append(panel);return true})()`);
 const anchor=await win.webContents.executeJavaScript(`(()=>{const panel=document.querySelector('#review-placement-fixture'),avatar=panel.querySelector('.quick-review-avatar-head');return {left:panel.getBoundingClientRect().left,avatarLeft:avatar.getBoundingClientRect().left}})()`);
 await review.openFromRenderer(win,'placed.user');
 const placed=await settledBounds(review.popup),parent=win.getContentBounds(),zoom=win.webContents.getZoomFactor();
 assert.ok(inside(placed,screen.getDisplayMatching(parent).workArea),'review remains visible on the active screen');
 assert.ok(placed.x+placed.width<=parent.x+anchor.avatarLeft*zoom-10,'review leaves avatar and approval columns visible');
 assert.ok(Math.abs(placed.x-Math.round(parent.x+anchor.left*zoom))<=1,'review aligns with the measured table');
 console.log('CHECK review fixture geometry',JSON.stringify({layout,anchor,parent,zoom,placed}));
 await Promise.all([review.openFromRenderer(win,'superseded.user'),review.openFromRenderer(win,'latest.user')]);
 await until(()=>review.popup.webContents.getTitle()==='/latest.user/');
 await win.webContents.executeJavaScript("document.querySelector('#review-placement-fixture').remove();true");
 // Same dedicated account controls as Settings; no software bridge in the page.
 const control=await review.account(win,{action:'login'});
 assert.equal(control.lastTarget,'latest.user');
 await until(()=>review.popup.webContents.getTitle()==='/accounts/login/');
 assert.equal(review.popup.webContents.session,reviewSession);
 assert.equal(await review.popup.webContents.executeJavaScript('typeof window.collectorCore'),'undefined');
 assert.equal(await review.popup.webContents.executeJavaScript("localStorage.getItem('review-login')"),'yes');
 await review.account(win,{action:'home'});await until(()=>review.popup.webContents.getTitle()==='/');
 await review.account(win,{action:'target'});await until(()=>review.popup.webContents.getTitle()==='/latest.user/');
 for(const [name,status] of [['missing.user',404],['limited.user',429],['proxy.error',200]]){
  const recovery=await readReviewRecoveryFixture(review.popup.webContents,{
   target:name,start:()=>review.openTarget(win,name),readState:()=>review.state()
  });
  assert.equal(recovery.responseStatus,status,'fixture response status for '+name);
  assert.equal(recovery.state.httpStatus,status,'review HTTP status for '+name);assert.equal(recovery.state.lastTarget,name);
  assert.ok(await review.popup.webContents.executeJavaScript("document.querySelector('a[href=\"https://www.instagram.com/accounts/login/\"]')!==null"));
  assert.match(recovery.state.message,status===429?/稍后手动重试/:/主页|错误页面/);
  await pause(50);assert.equal(review.popup.webContents.getTitle(),'审核网页暂时无法打开','no automatic retry');
 }
 await review.account(win,{action:'login'});await until(()=>review.popup.webContents.getTitle()==='/accounts/login/');
 assert.equal(review.state().lastTarget,'proxy.error');assert.equal(review.state().message,'');
 review.openTarget(win,'recovered.user');await until(()=>review.popup.webContents.getTitle()==='/recovered.user/');
 assert.deepEqual([...host.profiles.values()].map(p=>({id:p.id,pages:[...p.pages.values()].map(a=>a.view.webContents.getURL()),clients:p.clients.size})),before,'review login/recovery cannot touch task profiles');
 console.log('PASS same-session Settings review controls, target preserved through login/home, HTTP 404/429 and HTTP 200 error-title recovery, no automatic retry and no remote app IPC');
 // Exercise real Chromium storage and actual root/child BrowserWindows. Only
 // the trusted native confirmation response is replaced; cleanup is production.
 await reviewSession.cookies.set({url:'https://www.instagram.com/',name:'r52-review-login',value:'old',path:'/'});
 await gptSession.cookies.set({url:'https://chatgpt.com/',name:'r52-other-login',value:'preserved',path:'/'});resetCookie=true;
 const oldReview=review.popup,oldReviewContents=oldReview.webContents,resetTarget=review.state().lastTarget;
 await oldReviewContents.executeJavaScript("localStorage.setItem('review-login','old-review');window.open('https://www.instagram.com/accounts/login/?reset-fixture=1','review-reset-child');true");
 await until(()=>review.children.size===1);const reviewChild=[...review.children][0];
 await until(()=>reviewChild.webContents.getTitle()==='/accounts/login/');
 await reviewChild.webContents.executeJavaScript("localStorage.setItem('review-child-login','old-child');true");
 const originalShowMessageBox=dialog.showMessageBox;let resetDecision=0,resetPrompts=0;
 try{
  dialog.showMessageBox=async(parent,options)=>{assert.equal(parent,oldReview,'confirmation is above the current review page');assert.equal(options.type,'warning');assert.equal(options.defaultId,0);assert.equal(options.cancelId,0);resetPrompts++;return {response:resetDecision,checkboxChecked:false}};
  const cancelled=await review.account(win,{action:'reset'});
  assert.equal(cancelled.resetting,false);assert.equal(review.popup,oldReview);assert.equal(oldReview.isDestroyed(),false);assert.equal(reviewChild.isDestroyed(),false);
  assert.equal((await reviewSession.cookies.get({name:'r52-review-login'}))[0]?.value,'old');
  assert.equal(await oldReviewContents.executeJavaScript("localStorage.getItem('review-login')"),'old-review');
  resetDecision=1;const reset=await review.account(win,{action:'reset'}),freshReview=review.popup;
  assert.equal(resetPrompts,2);assert.equal(reset.resetting,false);assert.equal(reset.resetRequired,false);assert.equal(reset.lastTarget,resetTarget);
  assert.ok(oldReview.isDestroyed());assert.ok(reviewChild.isDestroyed());assert.notEqual(freshReview,oldReview);assert.equal(review.children.size,0);
  assert.equal(freshReview.webContents.session,reviewSession);await until(()=>freshReview.webContents.getTitle()==='/accounts/login/');
  assert.equal(freshReview.isVisible(),true);assert.equal(freshReview.getParentWindow(),win);
  assert.equal((await reviewSession.cookies.get({name:'r52-review-login'})).length,0);
  assert.deepEqual(await freshReview.webContents.executeJavaScript("[localStorage.getItem('review-login'),localStorage.getItem('review-child-login')]"),[null,null]);
  assert.equal(await freshReview.webContents.executeJavaScript('typeof window.collectorCore'),'undefined');
  assert.equal((await gptSession.cookies.get({name:'r52-other-login'}))[0]?.value,'preserved');
  await gpt.toggle(win);await until(()=>gpt.popup.webContents.getTitle()==='ChatGPT fixture');
  assert.equal(await gpt.popup.webContents.executeJavaScript("localStorage.getItem('fixture-login')"),'preserved');gpt.dispose();
  await review.account(win,{action:'target'});await until(()=>freshReview.webContents.getTitle()==='/'+resetTarget+'/');
  assert.deepEqual([...host.profiles.values()].map(p=>({id:p.id,pages:[...p.pages.values()].map(a=>a.view.webContents.getURL()),clients:p.clients.size})),before,'review reset cannot change account pages or task connections');
 }finally{dialog.showMessageBox=originalShowMessageBox}
 console.log('PASS review reset: cancel preserves session, confirm destroys root/login child, actual cookies and localStorage cleared, fresh isolated login, target return, other sessions and task profiles preserved');
 review.dispose();console.log('PASS independent review displays before slow navigation finishes, latest click wins, same window/session reused, login/return works and no task profiles changed');

 closing=new BrowserWindow({show:false});closingTool=new ChatGPTPage(v=>sendWindowEvent(closing,'chatgpt:visibility',v));
 await closingTool.toggle(closing);closing.on('closed',()=>closingTool.dispose());closing.destroy();closingTool.hide();closingTool.dispose();
 console.log('PASS floating page close/dispose during actual parent destruction without destroyed-renderer IPC exception');
 // Windows may clear webContents before marking the native window destroyed.
 // Exercise that event ordering deterministically on every build platform.
 const missingParent={isDestroyed:()=>false,webContents:null};
 missingTool=new ChatGPTPage(v=>sendWindowEvent(missingParent,'chatgpt:visibility',v));
 assert.throws(()=>missingTool.toggle(missingParent),/主窗口已关闭/);
 missingTool.win=missingParent;
 assert.doesNotThrow(()=>missingTool.hide());assert.equal(missingTool.opened,false);
 assert.doesNotThrow(()=>missingTool.dispose());assert.equal(missingTool.win,undefined);
 console.log('PASS missing parent renderer safely rejects open and allows floating hide/dispose during native close');
 } catch(error) {failed=true;throw error;} finally {
  // Every resource belongs to this fixture. A failed assertion must neither
  // leak native windows/protocol handlers nor be hidden by a cleanup failure.
  const cleanupErrors=[];
  const cleanup=async(label,operation)=>{try{await operation();}catch(error){cleanupErrors.push(new Error('Floating pages cleanup failed: '+label,{cause:error}));}};
  await cleanup('release slow response',()=>releaseSlow?.());
  await cleanup('missing-renderer tool',()=>missingTool?.dispose());
  await cleanup('closing-parent tool',()=>closingTool?.dispose());
  await cleanup('owned closing parent',()=>{if(closing&&!closing.isDestroyed())closing.destroy();});
  await cleanup('ChatGPT tool',()=>gpt?.dispose());
  await cleanup('review tool',()=>review?.dispose());
  await cleanup('review reset isolation cookie',async()=>{if(resetCookie)await gptSession.cookies.remove('https://chatgpt.com/','r52-other-login');});
  await cleanup('ChatGPT protocol',()=>{if(gptProtocol)gptSession.protocol.unhandle('https');});
  await cleanup('review protocol',()=>{if(reviewProtocol)reviewSession.protocol.unhandle('https');});
  await cleanup('parent geometry',restore);
  if(cleanupErrors.length){
   if(failed)for(const error of cleanupErrors)console.error(error);
   else throw new AggregateError(cleanupErrors,'Floating pages fixture cleanup failed');
  }
 }
};
