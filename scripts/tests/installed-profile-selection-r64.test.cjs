/* Pure source-function mocks. No Electron, Core, OpenVINO, or native process is loaded. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const root=path.resolve(__dirname,'../..');
const read=file=>fs.readFileSync(path.join(root,file),'utf8');
const main=ts.createSourceFile('main.ts',read('desktop/src/main.ts'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TS);
const transpile=source=>ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function functionSource(name){
 const node=main.statements.find(statement=>ts.isFunctionDeclaration(statement)&&statement.name?.text===name);
 assert.ok(node,`missing real ${name}`);return node.getText(main);
}
function identity(){
 const exports={};
 vm.runInNewContext(transpile(read('desktop/src/app-identity.ts')),{exports,require:name=>{
  assert.equal(name,'node:path');return path.win32;
 }});
 return exports;
}
const inherited={APPDATA:'C:\\Users\\fixture\\AppData\\Roaming',LOCALAPPDATA:'C:\\Users\\fixture\\AppData\\Local',USERPROFILE:'C:\\Users\\fixture',HOME:'C:\\Users\\fixture'};
const seeded='C:\\Temp\\Juxin-InstalledRecoveryR64-test\\roaming\\juxin-ig-audience-collector-newgen';

test('actual main selection keeps explicit seeded userData ahead of existing real-profile databases',()=>{
 const ids=identity();
 const first=main.statements.findIndex(statement=>statement.getText(main).startsWith('const originalUserData ='));
 const last=main.statements.findIndex(statement=>statement.getText(main)==='app.setPath("userData", selectedUserData);');
 assert.ok(first>=0&&last>first);
 const existing=new Set([seeded,path.win32.join(inherited.APPDATA,'聚鑫国际 新一代 IG 采集器'),path.win32.join(inherited.APPDATA,'juxin-ig-audience-collector-newgen')].map(directory=>path.win32.join(directory,'data','collector.sqlite3')));
 const reads=[],writes=[];
 let selected=seeded;
 const app={getPath:name=>{assert.ok(['userData','appData'].includes(name));return name==='userData'?selected:inherited.APPDATA},setPath:(name,value)=>{assert.equal(name,'userData');selected=value}};
 const source=main.statements.slice(first,last+1).map(statement=>statement.getText(main)).join('\n');
 vm.runInNewContext(transpile(source),{app,join:path.win32.join,selectUserDataDirectory:ids.selectUserDataDirectory,
  existsSync:file=>{reads.push(file);return existing.has(file)},mkdirSync:(directory,options)=>{assert.equal(options.recursive,true);writes.push(directory)}});
 assert.equal(selected,seeded);
 assert.deepEqual(writes,[seeded]);
 assert.deepEqual(reads,[path.win32.join(seeded,'data','collector.sqlite3')]);
 assert.equal(vm.runInNewContext(transpile(functionSource('secureFile'))+'\nsecureFile()', {app,join:path.win32.join}),path.win32.join(seeded,'secure-store.json'));
});

test('actual Core spawn inherits the OS profile and pinned database while using selected synthetic data',()=>{
 const fixtureDb=path.win32.join(seeded,'data','collector.sqlite3');
 const env={...inherited,IGAC_DB_PATH:fixtureDb};
 const writes=[],children=[];
 const child={stdout:{},stderr:{}};
 const scope={process:{env,platform:'win32',pid:123,resourcesPath:'C:\\Installed\\resources'},
  app:{getPath:name=>name==='userData'?seeded:'C:\\Users\\fixture\\Desktop',isPackaged:true},join:path.win32.join,
  configuredBitbrowserPort:null,readSecureValue:()=>'',provisionOpenAI:()=>'',secureCredentialStorageAvailable:()=>false,safeStorage:{},
  readCloudConfiguration:()=>({enabled:false,projectUrl:'',publishableKey:''}),coreToken:'fresh-startup-token',corePort:31001,
  embeddedBrowser:{url:'http://127.0.0.1:31004',token:'fresh-bridge-token'},corePrivateValues:[],
  rememberCoreSecret:value=>writes.push(value),captureCoreLog:()=>{},storageManagement:{log:()=>{}},
  spawn:(executable,args,options)=>{children.push({executable,args,options});return child}};
 const result=vm.runInNewContext(transpile(functionSource('spawnCore'))+'\nspawnCore()',scope);
 assert.equal(result,child);assert.equal(children.length,1);
 const actual=children[0].options.env;
 for(const key of Object.keys(inherited))assert.equal(actual[key],inherited[key]);
 assert.equal(actual.IGAC_DB_PATH,fixtureDb);
 assert.equal(actual.IGAC_DATA_DIR,path.win32.join(seeded,'data'));
 assert.equal(actual.COLLECTOR_DATA_DIR,path.win32.join(seeded,'data'));
 assert.equal(actual.IGAC_STARTUP_TOKEN,'fresh-startup-token');
 assert.equal(actual.COLLECTOR_CORE_TOKEN,'fresh-startup-token');
 assert.equal(actual.IGAC_PARENT_PID,'123');
 assert.equal(actual.IGAC_EMBEDDED_BROWSER_TOKEN,'fresh-bridge-token');
 assert.equal(children[0].executable,'C:\\Installed\\resources\\backend\\collector_core\\collector_core.exe');
 assert.deepEqual(env,{...inherited,IGAC_DB_PATH:fixtureDb});
 assert.ok(writes.includes('fresh-startup-token'));assert.ok(writes.includes('fresh-bridge-token'));
});
