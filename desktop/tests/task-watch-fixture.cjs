/* Local integration waits use observed lifecycle state, never a fixed sleep
   as proof of completion. Waiting does not grant access or change a task. */
const {performance}=require('node:perf_hooks');
async function waitTaskWatchCondition(check,{label,timeoutMs=5000}={}){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw new Error('Invalid task-watch fixture deadline');
 const deadline=performance.now()+timeoutMs;
 for(;;){
  const value=check();
  if(performance.now()>=deadline)throw new Error(`Task-watch condition not met: ${label} (${timeoutMs} ms)`);
  if(value)return value;
  await new Promise(resolve=>setTimeout(resolve,Math.min(20,Math.max(0,deadline-performance.now()))));
 }
}
// Capture the WebContents BEFORE closing its native view. Subscribe first so
// synchronous destruction cannot be missed. Missing view getters are never
// used as evidence of success: this wait requires the captured object's event.
function closeFixtureContents(contents,{label,timeoutMs=5000}={}){
 if(!Number.isFinite(timeoutMs)||timeoutMs<=0)return Promise.reject(new Error('Invalid task-watch fixture deadline'));
 if(!contents)return Promise.reject(new Error(`Native fixture contents missing before close: ${label}`));
 return new Promise((resolve,reject)=>{
  let timer,settled=false,closeReturned=false,destructionObserved=false;
  const deadline=performance.now()+timeoutMs;
  const timeoutError=()=>new Error(`Native fixture destruction not observed: ${label} (${timeoutMs} ms)`);
  const finish=error=>{
   if(settled)return;settled=true;
   clearTimeout(timer);contents.removeListener('destroyed',destroyed);
   if(!error && performance.now()>=deadline)error=timeoutError();
   error?reject(error):resolve();
  };
  const destroyed=()=>{destructionObserved=true;if(closeReturned)finish()};
  try{
   if(contents.isDestroyed()){finish();return}
   contents.once('destroyed',destroyed);
   timer=setTimeout(()=>finish(timeoutError()),timeoutMs);
   contents.close();
   closeReturned=true;if(destructionObserved)finish();
  }catch(error){finish(error)}
 });
}
module.exports={waitTaskWatchCondition,closeFixtureContents};
