/* Observe local fixture documents without changing production display gates.
   A loadURL promise alone does not establish the document currently on screen. */
const {performance}=require('node:perf_hooks');
const {rendererFixtureRead}=require('./renderer-fixture.cjs');
async function observeFixtureDocument(contents,expectedUrl,{label='fixture page',timeoutMs=5000,heading}={},navigate=false){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid fixture document deadline');
 const deadline=performance.now()+timeoutMs,events=[];
 let document,lastReadError,navigationError,navigationSettled=!navigate;
 const record=(event,details={})=>{events.push({event,...details});if(events.length>12)events.shift()};
 const listeners={
  'did-start-navigation':(_event,url,_inPlace,main)=>{if(main)record('start',{url})},
  'did-navigate':(_event,url)=>record('commit',{url}),
  'dom-ready':()=>record('dom-ready'),
  'did-finish-load':()=>record('finish'),
  'did-fail-load':(_event,code,_description,url,main)=>{if(main)record('fail',{code,url})},
  'render-process-gone':(_event,details)=>record('renderer-exit',{reason:details?.reason}),
 };
 const state=()=>contents.isDestroyed()?{destroyed:true}:{url:contents.getURL(),loading:contents.isLoadingMainFrame()};
 const failure=cause=>new Error(`Fixture document not ready: ${label}; ${JSON.stringify({expectedUrl,expectedHeading:heading,...state(),navigationSettled,document,events,lastReadError})}`,cause?{cause}:undefined);
 for(const [event,listener]of Object.entries(listeners))contents.on(event,listener);
 try{
  if(navigate){
   // The main-process deadline also bounds a native load promise that hangs.
   // Retain a rejection handler after timeout; do not issue another navigation.
   Promise.resolve(contents.loadURL(expectedUrl)).then(()=>{navigationSettled=true},error=>{navigationError=error});
  }
  for(;;){
   const native=state(),remaining=deadline-performance.now();
   if(navigationError){lastReadError=String(navigationError?.message||navigationError);throw failure(navigationError)}
   if(native.destroyed||remaining<=0)throw failure();
   if(native.url===expectedUrl&&!native.loading&&navigationSettled){
    try{
     document=await rendererFixtureRead(contents,"({url:location.href,readyState:document.readyState,heading:document.querySelector('h1')?.textContent?.trim()||''})",{label,timeoutMs:remaining});
    }catch(error){lastReadError=String(error?.message||error);throw failure(error)}
    const current=state();
    if(performance.now()>=deadline)throw failure();
    if(!current.destroyed&&current.url===expectedUrl&&!current.loading&&document?.url===expectedUrl&&document.readyState==='complete'&&(heading===undefined||document.heading===heading)){
     console.log('CHECK fixture document '+JSON.stringify({label,url:current.url,readyState:document.readyState,heading:document.heading}));
     return document;
    }
   }
   await new Promise(resolve=>setTimeout(resolve,Math.min(20,Math.max(0,deadline-performance.now()))));
  }
 }finally{for(const [event,listener]of Object.entries(listeners))contents.removeListener(event,listener)}
}
const waitFixtureDocument=(contents,expectedUrl,options)=>observeFixtureDocument(contents,expectedUrl,options);
const loadFixtureDocument=(contents,expectedUrl,options)=>observeFixtureDocument(contents,expectedUrl,options,true);
module.exports={waitFixtureDocument,loadFixtureDocument};
