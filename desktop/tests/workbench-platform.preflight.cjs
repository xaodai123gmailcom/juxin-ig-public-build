/* Fail fast on the real native platform UI, without replacing the full gate. */
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {captureShellFailure}=require('./visible-fixture.cjs');
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'juxin-platform-native-'));
const output=path.resolve('installer-output');
app.setPath('userData',temporary);app.setName('聚鑫国际');
app.on('window-all-closed',()=>{});
let win,finished=false;
const timer=setTimeout(()=>{if(!finished){console.error('FAIL native platform preflight deadline');app.exit(2)}},120000);
app.whenReady().then(async()=>{
 let code=1;
 try{
  win=new BrowserWindow({width:1500,height:950,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true}});
  await require('./workbench-platform.integration.cjs')({win,host:{hide(){}}});
  fs.mkdirSync(output,{recursive:true});
  const screenshot=await win.capturePage();fs.writeFileSync(path.join(output,'r4-platform-native-preflight.png'),screenshot.toPNG());
  console.log('PASS standalone native platform preflight; full embedded platform gate remains mandatory');code=0;
 }catch(error){
  console.error('FAIL native platform preflight',error?.stack||String(error));
  if(win&&!win.isDestroyed())await captureShellFailure(win,{condition:'native platform preflight',error,outputDirectory:output});
 }finally{
  finished=true;clearTimeout(timer);if(win&&!win.isDestroyed())win.destroy();app.exit(code);
 }
}).catch(error=>{console.error(error);clearTimeout(timer);app.exit(1)});
