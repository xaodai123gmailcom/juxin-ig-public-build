const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs');
const {run,fixtureHtml}=require('./task-cold-start-r62.cjs');
test('cold-start fixture contains icon-only self navigation, zero counts and no composition controls',()=>{
 const html=fixtureHtml();assert.match(html,/role="navigation"/);assert.match(html,/id="own-avatar"/);
 assert.match(html,/0帖子/);assert.match(html,/0粉丝/);assert.match(html,/0关注/);
 assert.doesNotMatch(html,/fixtureShares|id="composer"|Share/);assert.doesNotMatch(html,/api\.pexels|api\.instagram/);
});
test('native gate lets the task be the first opener and installs isolated offline routes',async()=>{
 const profiles=new Map(),created=[];
 const host={profiles,url:'http://127.0.0.1:12345',token:'fixture-token',async ensure(profile,owner,proxy,open,name){
  if(profiles.has(profile))return profiles.get(profile);
  const handlers={},cookies=[];const p={id:profile,session:{cookies:{set:async c=>cookies.push(c)},protocol:{handle:async(s,h)=>handlers[s]=h}},handlers,cookies};
  profiles.set(profile,p);created.push(p);return p;
 }};
 const result=await run({host,python:'python-fixture',runPythonProbe:async(python,script,env)=>{
  assert.equal(profiles.size,0);assert.equal(script,'embedded_task_startup_probe_r62.py');assert.equal(env.IGAC_EMBEDDED_BROWSER_URL,host.url);
  for(const kind of ['nurture']){
   const p=await host.ensure(kind,'owner','',true,'OFFLINE COLD '+kind);
   assert.equal(p.cookies[0].name,'ds_user_id');
   for(const suffix of ['/','/fixture_own/'])assert.equal((await p.handlers.https(new Request('https://www.instagram.com'+suffix))).status,200);
   assert.equal((await p.handlers.http(new Request('http://example.invalid'))).status,403);
   profiles.delete(kind);
  }
 }});
 assert.equal(result,true);assert.equal(created.length,1);assert.equal(profiles.size,0);
});
test('release result records the separate native startup proof after probe success',()=>{
 const source=fs.readFileSync(require.resolve('./embedded-browser.integration.cjs'),'utf8');
 assert.match(source,/let taskColdStart=false/);
 assert.match(source,/taskColdStart=await require\('\.\/task-cold-start-r62.cjs'\)\.run/);
 assert.match(source,/pythonWorker:Boolean\(process.env.JUXIN_PYTHON\),taskColdStart,timing/);
 const probe=fs.readFileSync(require('node:path').resolve(__dirname,'../../backend/tests/embedded_task_startup_probe_r62.py'),'utf8');
 assert.match(probe,/assert not any\(row\['is_open'\]/);assert.match(probe,/await studio\._execute/);
 assert.doesNotMatch(probe,/PostingExecutor|PostingManager/);
});
