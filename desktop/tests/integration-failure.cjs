/* Keep the first real cause visible even after native build-wrapper output. */
const fs=require('node:fs');
const bounded=(value,limit=1200)=>String(value??'').slice(0,limit);
const oneLine=value=>bounded(value).replace(/[\r\n]+/g,' ');
function pythonProbeFailure(script,code,output){
 const lines=String(output??'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
 const failures=lines.filter(line=>/^(ERROR|FAIL): /.test(line));
 const causes=lines.filter(line=>/^(?:[\w.]*(?:Error|Exception)|SystemExit|KeyboardInterrupt)(?::|$)/.test(line));
 const cause=causes.at(-1)||failures.at(-1)||lines.at(-1)||'probe produced no output';
 const failure=failures.at(-1)||'';
 // Put the actual Python exception first. Passing the entire unittest output
 // to assert.equal loses the cause when the release summary bounds its length.
 const error=new Error(`Python probe ${bounded(script,120)} exited ${code}: ${bounded(cause,700)}${failure?' ('+bounded(failure,260)+')':''}`);
 error.name='PythonProbeError';
 error.pythonProbe={script:bounded(script,120),exitCode:code,failure:bounded(failure,400),outputTail:String(output??'').slice(-8000)};
 return error;
}
function integrationFailureDetails(status,error){
 const details={...status};
 if(error!=null){
  details.checkpoint=bounded(error.integrationCheckpoint,160)||undefined;
  details.error={name:bounded(error.name||'Error',100),message:bounded(error.message??error),stack:bounded(error.stack,6000)};
  if(error.pythonProbe)details.probe=error.pythonProbe;
 }
 return details;
}
function integrationFailureSummary(details){
 const checkpoint=details.checkpoint||details.stage||'startup';
 const cause=details.error?.message||details.reason||'Unknown integration failure; see embedded-browser-full.log';
 return `FAIL embedded verification [${oneLine(checkpoint)}]: ${oneLine(cause)}`;
}
function writeIntegrationFailure(file,details){
 if(file)fs.writeFileSync(file,JSON.stringify(details,null,2)+'\n','utf8');
}
module.exports={integrationFailureDetails,integrationFailureSummary,writeIntegrationFailure,pythonProbeFailure};
