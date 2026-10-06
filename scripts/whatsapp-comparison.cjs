'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {pathToFileURL} = require('node:url');

// Retain the actual error sentence/stack. Never export cookies, database
// records, page HTML, QR contents, request headers or network response bodies.
function redact(value) {
  return String(value ?? '').slice(0,12000)
    .replace(/\b(?:https?|wss?):\/\/[^\s<>"']+/gi,raw=>{
      try {const u=new URL(raw);return u.origin+u.pathname.replace(/\/\d{7,}(?=\/|$)/g,'/[id]');}catch{return '[url]';}
    })
    .replace(/[A-Z]:\\Users\\[^\\\s]+/gi,'[USER]')
    .replace(/\/home\/[^/\s]+/g,'/[USER]')
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi,'[email]')
    .replace(/\b(?:Bearer\s+)?[A-Za-z0-9_+/=-]{64,}\b/g,'[long-value]')
    .replace(/\b\d{8,}\b/g,'[number]').slice(0,6000);
}
function errorText(error) {return redact(error?.stack || error?.message || error);}
function pageState() {
  const body=document.body?.innerText||'';
  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {
    origin:location.origin, ready:document.readyState,
    databaseError:/数据库错误|database error|database initialization failed/i.test(body),
    phoneFallback:/请改用电话号码|use (?:your )?phone number instead/i.test(body),
    phoneLoginVisible:/使用电话号码登录|link with phone number/i.test(body),
    qrCanvasCandidate:[...document.querySelectorAll('canvas')].some(e=>visible(e)&&e.width>=150&&e.height>=150),
    qrSvgCandidate:[...document.querySelectorAll('[data-ref] svg')].some(visible),
    viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,dpr:devicePixelRatio,visibility:document.visibilityState},
    secureContext:isSecureContext,crossOriginIsolated,
    capabilities:{indexedDB:typeof indexedDB==='object',crypto:!!crypto.subtle,opfs:typeof navigator.storage?.getDirectory==='function',wasm:typeof WebAssembly==='object'},
  };
}
async function deadline(promise,ms,label) {
  let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(label+' timeout')),ms);})]);}finally{clearTimeout(timer);}
}
async function run() {
  const {app,BrowserWindow,session,shell} = require('electron');
  const source=path.resolve(process.env.JUXIN_WA_CHECK_SOURCE||path.join(__dirname,'..'));
  const output=path.resolve(process.env.JUXIN_WA_CHECK_OUTPUT||path.join(source,'whatsapp-check-result.json'));
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),'Juxin-WA-check-'));
  const report={schema:1,tool:'WhatsApp same-engine comparison',startedAt:new Date().toISOString(),
    sourceVersion:null,platform:process.platform,architecture:process.arch,versions:process.versions,
    temporaryDataOnly:true,realLoginTested:false,controlledFixture:!!process.env.JUXIN_WA_CHECK_FIXTURE,cases:[],completed:false};
  app.setPath('userData',dataRoot);app.setPath('sessionData',dataRoot);
  app.setName('WhatsApp 登录对照检查');
  let exiting=false,host,activeWindow,heartbeat;
  const save=()=>{fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,JSON.stringify(report,null,2),'utf8');};
  const finish=async(code)=>{
    if(exiting)return;exiting=true;clearInterval(heartbeat);clearTimeout(totalTimer);
    report.finishedAt=new Date().toISOString();save();
    try{await deadline(host?.stop()||Promise.resolve(),8000,'close');}catch{}
    for(const win of BrowserWindow.getAllWindows())if(!win.isDestroyed())win.destroy();
    // Chromium may still hold temporary files on Windows until process exit.
    try{fs.rmSync(dataRoot,{recursive:true,force:true});}catch{report.temporaryCleanupPending=true;save();}
    console.log('REPORT: '+output);
    if(!process.env.JUXIN_WA_CHECK_FIXTURE)shell.showItemInFolder(output);
    app.exit(code);
  };
  const totalTimer=setTimeout(()=>{report.error='检查超时，已保留已完成项目';void finish(1);},300000);
  app.on('window-all-closed',()=>{});
  process.on('uncaughtException',e=>{report.error=errorText(e);void finish(1);});
  process.on('unhandledRejection',e=>{report.error=errorText(e);void finish(1);});
  try {
    report.sourceVersion=JSON.parse(fs.readFileSync(path.join(source,'package.json'),'utf8')).version;
    const {accountUserAgent}=await import(pathToFileURL(path.join(source,'dist-electron/account-user-agent.js')));
    const {accountStoragePermission}=await import(pathToFileURL(path.join(source,'dist-electron/account-permissions.js')));
    const {EmbeddedBrowserHost}=await import(pathToFileURL(path.join(source,'dist-electron/embedded-browser.js')));
    app.userAgentFallback=accountUserAgent(app.userAgentFallback);
    app.commandLine.appendSwitch('disk-cache-size',String(500*1024*1024));
    await app.whenReady();
    // Fixture is a local validation module, never used by the Windows launcher.
    const fixture=process.env.JUXIN_WA_CHECK_FIXTURE?require(path.resolve(process.env.JUXIN_WA_CHECK_FIXTURE)):null;
    const cases=[
      {id:'embedded',label:'软件嵌入方式',embedded:true,long:true,policy:true},
      {id:'plain-long',label:'独立窗口，相同配置',long:true,policy:true},
      {id:'plain-short',label:'独立窗口，简短存储路径',long:false,policy:true},
      {id:'plain-default-storage',label:'独立窗口，浏览器默认存储权限',long:false,policy:false},
    ];
    for(const [index,mode] of cases.entries()) {
      if(exiting)break;
      const row={id:mode.id,label:mode.label,startedAt:new Date().toISOString(),events:[],state:null};
      report.cases.push(row);save();
      const add=(kind,details)=>{if(row.events.length<100)row.events.push({at:new Date().toISOString(),kind,...details});};
      let cleanup=async()=>{},wc;
      try {
        const owner=randomUUID(),id='native:'+randomUUID();
        const rebuiltModule=path.join(source,'dist-electron/whatsapp-page.js');
        const ses=mode.embedded&&fs.existsSync(rebuiltModule)?await (await import(pathToFileURL(rebuiltModule))).createWhatsAppSession(owner,id,''):mode.long?session.fromPartition(`persist:account-${owner}-${id.slice(7)}`):session.fromPath(path.join(dataRoot,'s'+index));
        const storagePath=ses.storagePath;
        row.storage={pathLength:storagePath?.length,nonAsciiPath:!!storagePath&&/[^\x00-\x7f]/.test(storagePath),persistent:!!storagePath};
        await ses.setProxy({mode:'direct'});
        // All cases keep the same honest Chrome user agent and safe web prefs.
        ses.setUserAgent(accountUserAgent(ses.getUserAgent()),'zh-CN,zh;q=0.9,en;q=0.8');
        if(mode.policy){
          ses.setPermissionCheckHandler((page,permission,origin,details)=>{
            const allowed=accountStoragePermission(permission,origin,page?.getURL()||details.embeddingOrigin||'');
            add('permission',{permission,allowed,worker:!page});return allowed;
          });
          ses.setPermissionRequestHandler((page,permission,cb,details)=>cb(accountStoragePermission(permission,details.requestingUrl,page?.getURL()||'')));
        }else{
          // Match the app's refusal of device/notification requests; only the
          // storage permission-check behavior returns to Chromium's default.
          ses.setPermissionRequestHandler((page,permission,cb,details)=>cb(accountStoragePermission(permission,details.requestingUrl,page?.getURL()||'')));
        }
        if(fixture)await fixture.prepare(ses,index);
        const workerLog=(_e,d)=>add('service-worker-console',{level:d.level,message:redact(d.message),source:redact(d.sourceUrl),line:d.lineNumber});
        ses.serviceWorkers.on('console-message',workerLog);
        const captureNetwork=()=>{ses.webRequest.onErrorOccurred({urls:['https://*.whatsapp.com/*','https://*.whatsapp.net/*','wss://*.whatsapp.com/*']},d=>add('network-error',{host:new URL(d.url).hostname,type:d.resourceType,error:redact(d.error)}));
        ses.webRequest.onCompleted({urls:['https://*.whatsapp.com/*','https://*.whatsapp.net/*']},d=>{if(d.statusCode>=400)add('http-error',{host:new URL(d.url).hostname,type:d.resourceType,status:d.statusCode});});};captureNetwork();
        const observe=contents=>{
          contents.on('console-message',(_event,level,message,line,source)=>{if(level>=2)add('page-console',{level,message:redact(message),source:redact(source),line});});
          contents.on('did-fail-load',(_event,code,reason,url,main)=>add('load-error',{code,reason:redact(reason),origin:redact(url),main}));
          contents.on('render-process-gone',(_e,d)=>add('renderer-exit',{reason:d.reason,exitCode:d.exitCode}));
        };
        activeWindow=new BrowserWindow({show:true,width:1250,height:950,title:`WhatsApp 检查 ${index+1}/4：${mode.label}（无需扫码）`,backgroundColor:'#0b1016',webPreferences:mode.embedded?{sandbox:true,contextIsolation:true,nodeIntegration:false}:{session:ses,contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true,allowRunningInsecureContent:false,webviewTag:false,navigateOnDragDrop:false,backgroundThrottling:false,spellcheck:false}});
        let closingNormally=false;
        activeWindow.on('page-title-updated',event=>event.preventDefault());
        activeWindow.on('close',()=>{if(!closingNormally&&!exiting){row.cancelled=true;void finish(2);}});
        const caseWindow=activeWindow;
        cleanup=async()=>{clearInterval(heartbeat);closingNormally=true;try{if(host){const closingHost=host;host=undefined;await closingHost.stop();}}finally{if(!caseWindow.isDestroyed())caseWindow.destroy();}};
        if(mode.embedded) {
          host=new EmbeddedBrowserHost(()=>activeWindow);await host.start();host.attachWindow(activeWindow);
          const p=await host.ensure(id,owner,'',true,'WhatsApp 临时检查',true);
          wc=p.pages.get(p.selected).view.webContents;observe(wc);captureNetwork();
          const show=()=>{if(!activeWindow||activeWindow.isDestroyed())return;const [width,height]=activeWindow.getContentSize();host.show(p,{x:0,y:0,width,height});};
          // Follow the app: hidden during open, then authorize the native view.
          const opened=await host.control('open-whatsapp',{profile:id,owner,url:'https://web.whatsapp.com/'});
          row.opened={page_loaded:opened.page_loaded,http_status:opened.http_status};show();heartbeat=setInterval(show,1000);

        }else{
          wc=activeWindow.webContents;observe(wc);wc.setUserAgent(ses.getUserAgent());
          void wc.loadURL('https://web.whatsapp.com/').catch(e=>add('navigation-error',{message:errorText(e)}));

        }
        row.debuggerAttachedBeforeRetry=wc.debugger.isAttached();
        const seconds=fixture?1:35;
        for(let left=seconds;left>0&&!exiting;left--){activeWindow.setTitle(`WhatsApp 检查 ${index+1}/4：${mode.label} · ${left} 秒（无需扫码）`);await new Promise(r=>setTimeout(r,1000));}
        if(exiting)break;
        row.state=await deadline(wc.executeJavaScript(`(${pageState.toString()})()`),5000,'page state');
        row.databases=await deadline(wc.executeJavaScript('indexedDB.databases().then(xs=>xs.map(x=>({name:x.name,version:x.version})))'),5000,'database inventory').catch(e=>({error:errorText(e)}));
        if(row.state.databaseError){
          // The uninstrumented first-open result is already recorded. Only
          // this disposable failed page is reloaded for detailed exceptions.
          row.instrumentedRetry=true;
          wc.debugger.attach('1.3');
          wc.debugger.on('message',(_e,method,params,sessionId)=>{
            if(method==='Runtime.exceptionThrown'){const d=params.exceptionDetails;add('runtime-exception',{message:redact(d.exception?.description||d.text),source:redact(d.url),line:d.lineNumber,stack:d.stackTrace?.callFrames?.slice(0,10).map(f=>({function:redact(f.functionName),source:redact(f.url),line:f.lineNumber})),worker:!!sessionId});}
            if(method==='Log.entryAdded')add('browser-log',{message:redact(params.entry.text),source:redact(params.entry.url),level:params.entry.level});
            if(method==='Target.attachedToTarget')void wc.debugger.sendCommand('Runtime.enable',{},params.sessionId).catch(()=>{});
          });
          await wc.debugger.sendCommand('Runtime.enable');await wc.debugger.sendCommand('Log.enable');
          await wc.debugger.sendCommand('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:false,flatten:true}).catch(()=>{});
          wc.reload();activeWindow.setTitle(`WhatsApp 检查 ${index+1}/4：捕获具体错误（无需扫码）`);
          await new Promise(r=>setTimeout(r,fixture?800:12000));
          row.retryState=await deadline(wc.executeJavaScript(`(${pageState.toString()})()`),5000,'retry state');
          wc.debugger.detach();
        }
        ses.serviceWorkers.removeListener('console-message',workerLog);
        ses.webRequest.onErrorOccurred(null);ses.webRequest.onCompleted(null);
      }catch(e){row.error=errorText(e);}
      finally{row.finishedAt=new Date().toISOString();save();try{await deadline(cleanup(),8000,'cleanup');}catch(e){row.cleanupError=errorText(e);save();}}
    }
    report.completed=!exiting&&report.cases.length===4&&report.cases.every(x=>x.state&&!x.error);
    await finish(report.completed?0:1);
  }catch(e){report.error=errorText(e);await finish(1);}
}
module.exports={redact,pageState};
if(process.versions.electron&&process.type==='browser')void run();
