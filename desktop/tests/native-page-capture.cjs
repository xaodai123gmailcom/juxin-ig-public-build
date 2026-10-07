const assert=require('node:assert/strict');
const {rendererFixtureRead}=require('./renderer-fixture.cjs');
async function capturePaintedPage({win,pane,page,bounds,showSurface,evidence,timeoutMs=15000,read=rendererFixtureRead,pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=()=>performance.now(),timers=globalThis}) {
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid native capture deadline');
  const started=now(),wc=page.view.webContents;
  evidence.attempts=[];evidence.completed=false;
  const remaining=()=>{
    const ms=timeoutMs-(now()-started);
    if(ms<=0)throw new Error('Native page screenshot did not become paint-ready: '+JSON.stringify(evidence.attempts.at(-1)));
    return ms;
  };
  for(;;){
    remaining();
    if(win.isDestroyed()||wc.isDestroyed())throw new Error('Native page screenshot surface destroyed');
    // Renew the production three-second display lease. A transient compositor
    // rejection must not let the retry itself hide the native pane.
    await showSurface();
    const attempt={atMs:Math.round(now()-started),native:{windowVisible:win.isVisible(),minimized:win.isMinimized(),paneVisible:pane.getVisible(),paneBounds:pane.getBounds(),pageBounds:page.view.getBounds()}};
    evidence.attempts.push(attempt);
    assert.deepEqual(attempt.native.paneBounds,bounds,'capture keeps measured pane');
    assert.deepEqual(attempt.native.pageBounds,{x:0,y:0,width:bounds.width,height:bounds.height},'capture keeps page native bounds');
    if(attempt.native.windowVisible&&!attempt.native.minimized&&attempt.native.paneVisible){
      // DOM geometry can be ready before Viz owns a copyable compositor
      // surface. Require real visible renderer frames; never fake visibility.
      attempt.renderer=await read(wc,`new Promise(resolve=>{const state=()=>({ready:document.readyState,visibility:document.visibilityState,width:innerWidth,height:innerHeight});const first=state();if(first.visibility!=='visible'||first.ready!=='complete'){resolve({...first,painted:false});return}requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({...state(),painted:true})))})`,{label:'native page screenshot paint',timeoutMs:Math.min(2000,remaining())});
      const state=attempt.renderer;
      assert.equal(state.width,bounds.width,'capture keeps actual DOM width');
      assert.equal(state.height,bounds.height,'capture keeps actual DOM height');
      if(state.painted&&state.ready==='complete'&&state.visibility==='visible'){
        await showSurface();remaining();let timer;
        try{
          const image=await Promise.race([wc.capturePage(),new Promise((_,reject)=>{timer=timers.setTimeout(()=>reject(new Error('Native page screenshot capture timed out')),Math.min(2000,remaining()))})]);
          remaining();assert.equal(image.isEmpty(),false,'native screenshot has pixels');
          evidence.completed=true;return image;
        }catch(error){
          attempt.captureError=String(error);
          // Only this observed compositor readiness error is retryable. Empty
          // images, renderer errors, geometry mismatches and hung captures fail.
          if(error?.message!=='UnknownVizError'&&error?.name!=='UnknownVizError')throw error;
          console.log('CHECK native page screenshot compositor not ready; awaiting fresh paint');
        }finally{timers.clearTimeout(timer)}
      }
    }
    await pause(Math.min(50,remaining()));
  }
}

module.exports={capturePaintedPage};
