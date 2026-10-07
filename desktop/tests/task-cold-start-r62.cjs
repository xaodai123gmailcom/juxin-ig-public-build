/* Install an offline-only session before the first task navigation, never pre-open. */
const assert=require('node:assert/strict');
function fixtureHtml(){
  return `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;background:#101215;color:white;font-family:Arial}nav{position:fixed;left:0;top:0;width:72px;height:100vh;display:flex;flex-direction:column;gap:22px;padding-top:24px}nav a{display:block;margin-left:24px;width:24px;height:24px;color:white}svg{width:24px;height:24px}main{margin-left:260px;padding:50px}</style></head><body>
    <nav role="navigation"><a href="/"><svg aria-label="首页"><circle cx="12" cy="12" r="8"/></svg></a><a href="/reels/"><svg aria-label="Reels"><rect width="18" height="18"/></svg></a><a href="/direct/inbox/"><svg aria-label="消息"><path d="M2 2L22 2L12 22Z"/></svg></a><a href="/fixture_own/" id="own-avatar"><img alt="" width="24" height="24" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2224%22 height=%2224%22/%3E"></a></nav>
    <main><h1>示例主页</h1></main><script>
      if(location.pathname==='/fixture_own/')document.querySelector('main').innerHTML='<header><h2>fixture_own</h2><a href="/accounts/edit/">编辑主页</a><ul><li>0帖子</li><li><a href="/fixture_own/followers/">0粉丝</a></li><li><a href="/fixture_own/following/">0关注</a></li></ul></header>';
    </script></body></html>`;
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
    assert.equal(fixtures.size,1);
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
