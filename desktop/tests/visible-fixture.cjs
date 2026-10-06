/* A foreground UI assertion requires both a native and a renderer-visible
   test window. Never fake visibilityState or disable background throttling. */
const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const fs=require('node:fs');
const path=require('node:path');
const {performance}=require('node:perf_hooks');
async function prepareVisibleFixture(win,{label='foreground fixture',timeoutMs=30000}={}){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid foreground fixture deadline');
 const deadline=performance.now()+timeoutMs;
 const remaining=()=>{const ms=deadline-performance.now();if(ms<=0)throw new Error(label+': document visibility timed out');return ms};
 if(win.isDestroyed())throw new Error(label+': native window is destroyed');
 if(win.isMinimized())win.restore();
 if(!win.isVisible())win.show();
 const focus=()=>{win.moveTop?.();win.focus();win.webContents.focus()};
 focus();
 const condition="document.visibilityState==='visible'";
 const visible=await rendererFixtureRead(win.webContents,condition,{label:label+' document visibility',timeoutMs:remaining()});
 let recovered=false;
 if(!visible){
  // focus()/isVisible() alone do not produce a new native show notification
  // when Windows already reports the window as shown. Allow ordinary async
  // propagation, then perform one real lifecycle transition on this fixture.
  // A stalled JS evaluation fails; it is never treated as a visibility result.
  // A grace period limits observation, never an individual read. A finite
  // read that crosses this soft boundary still returns an honest hidden state;
  // a genuinely hung read consumes the hard deadline and cannot trigger recovery.
  const graceDeadline=performance.now()+Math.min(500,remaining()/3);
  let propagated=false;
  while(performance.now()<graceDeadline){
   propagated=await rendererFixtureRead(win.webContents,condition,{label:label+' initial visibility',timeoutMs:remaining()});
   if(propagated)break;
   const delay=Math.min(20,Math.max(0,graceDeadline-performance.now()));
   if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
  }
  if(!propagated){
   if(win.isDestroyed())throw new Error(label+': native window is destroyed');
   remaining();win.hide();
   try{
    await waitRendererFixture(win.webContents,"document.visibilityState==='hidden'",{label:label+' hidden transition',timeoutMs:remaining()});
   }finally{if(!win.isDestroyed()){win.show();focus()}}
   recovered=true;
   console.log('CHECK native foreground recovery',label);
   await waitRendererFixture(win.webContents,condition,{label:label+' document visibility after show',timeoutMs:remaining()});
  }
 }
 if(win.isDestroyed()||!win.isVisible()||win.isMinimized())throw new Error(label+': native window is still hidden/minimized');
 return {visible:win.isVisible(),minimized:win.isMinimized(),focused:win.isFocused(),recovered};
}
const surfaceEvidenceScript=`(()=>{
 const f=window.fixture,surface=document.querySelector('.account-browser-surface'),rect=surface?.getBoundingClientRect();
  return {url:location.href,visibility:document.visibilityState,focus:document.hasFocus(),ready:document.readyState,
  viewport:{width:innerWidth,height:innerHeight},
  dialogs:[...document.querySelectorAll('[role=dialog]')].map(e=>e.getAttribute('aria-label')),
  alerts:[...document.querySelectorAll('[role=alert]')].map(e=>e.textContent?.slice(0,600)),
  placeholder:document.querySelector('.profile-preview-empty')?.textContent?.slice(0,600),
  selected:document.querySelector('[aria-label=预览账号窗口]')?.value,
  bridge:typeof window.collectorCore?.accountSurface,
  surface:rect?{connected:surface.isConnected,x:rect.x,y:rect.y,width:rect.width,height:rect.height}:null,
  requestCount:f?.surfaces?.length,requests:f?.surfaces?.slice(-12),commands:f?.commands?.slice(-6)};
})()`;
async function captureShellFailure(win,{condition,error,outputDirectory,rendererErrors=[]}){
 const evidence={condition,error:String(error),rendererErrors:rendererErrors.slice(-12)};
 try{
  let remaining=12;
  const viewState=(view,depth=0)=>{
   if(!view||remaining--<=0)return null;
   return {type:view.constructor?.name,visible:view.getVisible?.(),bounds:view.getBounds?.(),
    contentsId:view.webContents?.id,children:depth<3?Array.from(view.children||[]).slice(0,12).map(v=>viewState(v,depth+1)):[]};
  };
  evidence.native={visible:win.isVisible(),minimized:win.isMinimized(),focused:win.isFocused(),clientSize:win.getContentSize(),overlay:viewState(win.contentView)};
 }
 catch(reason){evidence.native={error:String(reason)}}
 try{evidence.renderer=await rendererFixtureRead(win.webContents,surfaceEvidenceScript,{label:'failed shell evidence',timeoutMs:2000})}
 catch(reason){evidence.renderer={error:String(reason)}}
 fs.mkdirSync(outputDirectory,{recursive:true});
 const report=path.join(outputDirectory,'embedded-shell-failure.json');
 fs.writeFileSync(report,JSON.stringify(evidence,null,2)+'\n');
 let timer;
 try{
  const image=await Promise.race([win.capturePage(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('screenshot timed out')),2000)})]);
  fs.writeFileSync(path.join(outputDirectory,'embedded-shell-failure.png'),image.toPNG());
 }catch(reason){console.error('CHECK failed shell screenshot',String(reason))}finally{clearTimeout(timer)}
 console.error('CHECK failed React shell',JSON.stringify(evidence),'report',report);
 return evidence;
}
module.exports={prepareVisibleFixture,surfaceEvidenceScript,captureShellFailure};
