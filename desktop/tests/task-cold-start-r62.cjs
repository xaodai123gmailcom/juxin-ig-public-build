/* Install an offline-only session before the first task navigation, never pre-open. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
function fixtureHtml(){
  const source=fs.readFileSync(path.resolve(__dirname,'../../backend/tests/test_posting_dom.py'),'utf8');
  const match=source.match(/HTML=r'''([\s\S]*?)'''/);assert.ok(match,'existing offline composer fixture');
  const avatar='<a href="/fixture_own/" id="own-avatar"><img alt="" width="24" height="24" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2224%22 height=%2224%22/%3E"></a>';
  return match[1].replace('id="sidebar"','id="sidebar" role="navigation"')
    .replace('<div id="entry"',avatar+'<div id="entry"')
    .replace('</body>',`<script>
      window.fixtureShares=0;
      window.share=()=>{window.fixtureShares++;throw Error('Native cold-start gate must stop before Share')};
      if(location.pathname==='/fixture_own/')document.querySelector('main').innerHTML='<header><h2>fixture_own</h2><a href="/accounts/edit/">编辑主页</a><ul><li>0帖子</li><li><a href="/fixture_own/followers/">0粉丝</a></li><li><a href="/fixture_own/following/">0关注</a></li></ul></header>';
    </script></body>`);
}
async function run({host,python,runPythonProbe}){
  const ensure=host.ensure.bind(host),fixtures=new Map(),html=fixtureHtml();
  host.ensure=async function(profile,owner,proxy,open,name,...rest){
    const fresh=String(name||'').startsWith('OFFLINE COLD ')&&!fixtures.has(profile);
    if(fresh){assert.equal(host.profiles.has(profile),false,'real task must be first profile opener');assert.equal(open,true)}
    const result=await ensure(profile,owner,proxy,open,name,...rest);
    if(fresh){
      const record={profile,name,requests:[],external:[]};fixtures.set(profile,record);
      await result.session.cookies.set({url:'https://www.instagram.com/',name:'ds_user_id',value:'123456789',secure:true});
      await result.session.protocol.handle('https',request=>{
        const url=new URL(request.url);
        if(!['www.instagram.com','instagram.com'].includes(url.hostname)){record.external.push(url.hostname);return new Response('Blocked',{status:403})}
        record.requests.push(url.pathname);
        return new Response(html,{headers:{'content-type':'text/html; charset=utf-8'}});
      });
      await result.session.protocol.handle('http',()=>new Response('Blocked',{status:403}));
    }
    return result;
  };
  try{
    await runPythonProbe(python,'embedded_task_startup_probe_r62.py',{
      IGAC_EMBEDDED_BROWSER_URL:host.url,IGAC_EMBEDDED_BROWSER_TOKEN:host.token,
    });
    assert.equal(fixtures.size,2);
    for(const record of fixtures.values()){
      assert.ok(record.requests.includes('/'),'real worker navigated from blank to home');
      assert.ok(record.requests.includes('/fixture_own/'),'real worker verified own profile');
      assert.deepEqual(record.external,[],'no external service accessed');
      assert.equal(host.profiles.has(record.profile),false,'task cleanup closed its own native profile');
    }
    return true;
  }finally{host.ensure=ensure}
}
module.exports={run,fixtureHtml};
