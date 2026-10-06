const {rendererFixtureRead,waitRendererFixture}=require('./renderer-fixture.cjs');
const {prepareVisibleFixture}=require('./visible-fixture.cjs');
const assert=require('node:assert/strict'),path=require('node:path'),http=require('node:http');
module.exports=async({win,host})=>{
 const built=await require('esbuild').build({entryPoints:[path.resolve(__dirname,'../../renderer/tests/fixtures/workbench-platform.tsx')],bundle:true,write:false,outfile:'fixture.js',format:'iife',jsx:'automatic',define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 const server=http.createServer((req,res)=>{const suffix=req.url==='/fixture.js'?'.js':req.url==='/fixture.css'?'.css':null;res.setHeader('content-type',suffix==='.js'?'text/javascript':suffix==='.css'?'text/css':'text/html; charset=utf-8');res.end(suffix?built.outputFiles.find(x=>x.path.endsWith(suffix)).contents:'<meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><div id="root"></div><script src="/fixture.js"></script>')});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));host.hide();
 const evaluate=s=>rendererFixtureRead(win.webContents,s,{label:'Pure IG workbench fixture'}),wait=(s,label)=>waitRendererFixture(win.webContents,s,{label});
 const navigate=async route=>{await evaluate(`location.hash=${JSON.stringify('#/'+route)};true`);await wait(`document.querySelector('[data-nav="${route}"]')?.getAttribute('aria-current')==='page'`,'route selected')};
 const button=t=>`[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(t)})`;
 const click=async t=>{await wait(`!!${button(t)}&&!${button(t)}.disabled`,'button available '+t);await evaluate(`${button(t)}.click();true`)};
 const enterDraft=async text=>{await evaluate(`(()=>{const input=document.querySelector('.collection-target-input');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,${JSON.stringify(text)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`)};
 const noOtherPlatform=async()=>{assert.equal(await evaluate(`!!document.querySelector('.workbench-platform-toggle,[aria-label="显示平台"]')||/Facebook|点击切换到 FB/.test(document.body.textContent)`),false);assert.equal(await evaluate(`!!document.querySelector('.formal-brand img[src="./war-wolf.svg"]')`),true)};
 try{
  await win.loadURL('http://127.0.0.1:'+server.address().port+'/#/review');await prepareVisibleFixture(win,{label:'Pure IG workbench'});
  await wait(`document.body.textContent.includes('IG legacy 0')`,'populated IG review page');await noOtherPlatform();
  await click('下一页');await wait(`document.body.textContent.includes('IG legacy 502')`,'IG second page');
  assert.equal(await evaluate(`platformFixture.requests.filter(x=>x.path==='/api/workbench/review/query').at(-1).body.offset`),500);
  await navigate('public');await wait(`document.body.textContent.includes('IG legacy 900')`,'IG approved rows');
  assert.equal(await evaluate(`document.body.textContent.includes('启动自动打招呼')`),true);
  await click('导出全部');await wait('platformFixture.downloads.length===1','IG export initiated');
  assert.equal(await evaluate(`platformFixture.requests.filter(x=>x.path==='/api/workbench/accounts/export').at(-1).platform`),'instagram');
  await navigate('collection');await wait(`!!document.querySelector('.collection-target-input')`,'collection loaded');
  assert.equal(await evaluate("document.querySelectorAll('.collection-mode-row input').length"),2);
  await enterDraft('https://www.facebook.com/seed.profile/');await click('自动加入');await wait("document.body.textContent.includes('请输入有效的 Instagram')",'foreign URL validation');
  assert.equal(await evaluate('platformFixture.commands.length'),0,'foreign URL never reaches transport');
  await enterDraft('https://www.instagram.com/seed.profile/');
  for(let i=0;i<3;i++){
    await navigate('history');await wait(`document.body.textContent.includes('IG legacy 900')`,'IG history remains populated');
    await navigate('reports');await wait(`document.body.textContent.includes('分裂号审查')&&document.body.textContent.includes('私密关注检查')`,'all IG reports available');
    await navigate('accounts');await wait(`document.body.textContent.includes('暂无窗口')||document.querySelector('.account-sidebar-empty')!==null`,'empty account inventory distinct from failure');
    await navigate('collection');await wait(`!!document.querySelector('.collection-target-input')`,'collection returns');
    assert.equal(await evaluate("document.querySelector('.collection-target-input').value"),'https://www.instagram.com/seed.profile/');await noOtherPlatform();
  }
  await click('自动加入');await wait("document.querySelector('.collection-waiting-row')?.textContent.includes('seed.profile')&&document.querySelector('.collection-target-input').value===''",'canonical IG waiting target retained');
  assert.equal(await evaluate('platformFixture.commands.length'),1);assert.equal(await evaluate('platformFixture.commands[0].payload.platform'),'instagram');
  const snapshots=await evaluate(`platformFixture.requests.filter(x=>x.path.startsWith('/api/workbench/snapshot'))`);
  assert.equal(snapshots.every(x=>x.path.includes('limit=2000&history_limit=2000&platform=instagram&compact=1')),true,'bounded compact snapshots preserved');
  assert.ok(snapshots.length<10,'repeated navigation must not create a polling subscription per route');
  await navigate('review');await wait(`document.body.textContent.includes('IG legacy 0')`,'review navigation resets offset');
  await evaluate(`platformFixture.empty=true;document.querySelector('button.formal-brand[aria-label=刷新]').click();true`);
  await navigate('history');await wait(`document.body.textContent.includes('没有审核合格记录')`,'authoritative empty history');
  await evaluate(`localStorage.setItem('juxin.workbench.display-platform.v1','facebook');true`);
  await new Promise(resolve=>{win.webContents.once('did-finish-load',resolve);win.webContents.reload()});
  await wait(`document.body.textContent.includes('IG legacy 900')`,'reload ignores retired display preference');await noOtherPlatform();
  console.log('PASS pure IG UI: populated/empty states, 503-row paging, export, rejected FB input, static wolf, repeated navigation, draft retention, compact single subscription and reload');
 }finally{await new Promise(r=>server.close(r))}
};
