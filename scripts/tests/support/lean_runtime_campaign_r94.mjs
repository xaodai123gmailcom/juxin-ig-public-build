// 200 seeded runtime-graph checks for a build with bundled renderer packages.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { stagePortableResources } from '../../stage_portable_resources.mjs';

const output = resolve(process.argv[2] || 'lean-runtime-results');
mkdirSync(output, {recursive:true});
const log = join(output,'campaign.jsonl');
writeFileSync(log,'');
let passed = 0;
for (const kind of ['runtime_graph','missing_required']) {
  for (let seed = 1; seed <= 100; seed++) {
    const started = performance.now(), root = mkdtempSync(join(tmpdir(),`运行依赖 [${seed}] `));
    const source = join(root,'完整源码'), app = join(root,'独立 APP','resources','app');
    const put = (name, data) => {const path=join(source,name);mkdirSync(dirname(path),{recursive:true});writeFileSync(path,data);};
    const pkg = (folder, data, code='module.exports.value=42;') => {
      put(join(folder,'package.json'),JSON.stringify({main:'index.js',...data}));
      put(join(folder,'index.js'),code);
    };
    const count = 1 + seed % 7, nested = Boolean(seed % 2);
    const records = [];
    let status = 'passed', error;
    try {
      pkg('',{name:'lean-fixture',dependencies:{ws:'1',parent:'1'},devDependencies:{react:'1',vite:'1',typescript:'1'}});
      pkg('node_modules/ws',{name:'ws',peerDependencies:{'optional-native':'1'},peerDependenciesMeta:{'optional-native':{optional:true}}},
        'exports.WebSocket=class {};exports.WebSocketServer=class {};');
      const wanted = Object.fromEntries(Array.from({length:count},(_,i)=>[`child${i}`,'1']));
      pkg('node_modules/parent',{name:'parent',dependencies:wanted},'exports.value=require("child0").value;');
      for(let i=0;i<count;i++) {
        const folder = nested ? `node_modules/parent/node_modules/child${i}` : `node_modules/child${i}`;
        records.push(folder);
        pkg(folder,{name:`child${i}`,dependencies:i===count-1 ? {parent:'1'} : {}});
        put(join(folder,'许可证.txt'),`license-${seed}-${i}`);
      }
      for(const name of ['react','vite','typescript']) {
        pkg(`node_modules/${name}`,{name});
        put(`node_modules/${name}/.cache/old-build.bin`,Buffer.alloc(seed*37+1));
      }
      pkg('node_modules/parent/node_modules/unrelated-dev',{name:'unrelated-dev'});
      for(const asset of ['desktop/assets/war-wolf.ico','desktop/vendor/immersive-translate/host.html',
        'desktop/vendor/immersive-translate/NOTICE.txt']) put(asset,`asset-${seed}`);
      if(seed%2)put('desktop/vendor/immersive-translate/immersive-translate.user.js',`synthetic-unbundled-sentinel-${seed}`);
      if(kind==='missing_required') {
        rmSync(join(source,records[seed%records.length]),{recursive:true});
        assert.throws(()=>stagePortableResources(source,app),/Missing portable dependency/);
        assert.equal(existsSync(join(app,'node_modules')),false);
      } else {
        const result=stagePortableResources(source,app);
        assert.equal(result.packages.length,count+2);
        for(const name of ['react','vite','typescript','optional-native']) assert.equal(existsSync(join(app,'node_modules',name)),false);
        assert.equal(existsSync(join(app,'node_modules/parent/node_modules/unrelated-dev')),false);
        assert.equal(existsSync(join(app,'desktop/vendor/immersive-translate/immersive-translate.user.js')),false);
        for(const asset of ['host.html','NOTICE.txt'])assert.equal(readFileSync(join(app,'desktop/vendor/immersive-translate',asset),'utf8'),`asset-${seed}`);
        rmSync(source,{recursive:true});
        const require=createRequire(join(app,'package.json'));
        assert.equal(require('parent').value,42);
        assert.equal(typeof require('ws').WebSocket,'function');
        for(let i=0;i<count;i++) assert.equal(readFileSync(join(app,records[i],'许可证.txt'),'utf8'),`license-${seed}-${i}`);
      }
    } catch(e) {status='failed';error=String(e.stack||e);}
    finally {rmSync(root,{recursive:true,force:true});}
    const row={id:`L${String(passed+1).padStart(3,'0')}`,family:kind,seed,children:count,nested,status,seconds:(performance.now()-started)/1000,...(error?{error}:{})};
    appendFileSync(log,JSON.stringify(row)+'\n');
    if(status!=='passed') {console.error(JSON.stringify(row));process.exit(1);}
    passed++;
  }
}
writeFileSync(join(output,'campaign-summary.json'),JSON.stringify({passed,failed:0,skipped:0,native_windows_execution:false},null,2)+'\n');
console.log(`LEAN_RUNTIME_SCENARIOS=${passed} PASS`);
