/* Main-process deadlines for local fixtures. Never depend on animation frames:
   Chromium may stop painting a hidden, minimized, or occluded test window. */
const {performance}=require('node:perf_hooks');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function rendererFixtureRead(wc,source,{label='renderer read',timeoutMs=5000}={}){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid renderer fixture deadline');
 const deadline=performance.now()+timeoutMs;
 const expired=()=>Object.assign(new Error(`Renderer fixture timed out: ${label} (${Math.ceil(timeoutMs)} ms)`),{code:'FIXTURE_RENDERER_TIMEOUT',label,timeoutMs});
 let timer;
 const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(expired()),timeoutMs)});
 const read=Promise.resolve().then(()=>{
  if(wc.isDestroyed?.())throw Object.assign(new Error(`Renderer fixture destroyed: ${label}`),{code:'FIXTURE_RENDERER_DESTROYED',label});
  return wc.executeJavaScript(source);
 }).then(value=>{
  // A busy main thread can delay the timer callback; late results still fail.
  if(performance.now()>=deadline)throw expired();
  return value;
 });
 return Promise.race([read,timeout]).finally(()=>clearTimeout(timer));
}
async function waitRendererFixture(wc,source,{label='renderer condition',timeoutMs=5000}={}){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid renderer fixture deadline');
 const deadline=performance.now()+timeoutMs;
 for(;;){
  const remaining=deadline-performance.now();
  if(remaining<=0)throw Object.assign(new Error(`Renderer fixture condition not met: ${label} (${timeoutMs} ms)`),{code:'FIXTURE_CONDITION_UNMET',label,timeoutMs});
  const value=await rendererFixtureRead(wc,source,{label,timeoutMs:remaining});
  if(value)return value;
  await pause(Math.min(20,Math.max(0,deadline-performance.now())));
 }
}
module.exports={rendererFixtureRead,waitRendererFixture};
