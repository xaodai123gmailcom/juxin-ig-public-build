/* Deterministic local documents for the real unread reader; no count retries. */
let sequence=0;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function fixtureResponse(html){
 return new Response('<!doctype html><meta charset="utf-8">'+html,{headers:{'content-type':'text/html; charset=utf-8','cache-control':'no-store'}});
}
async function readUnreadFixture(wc,{body,url,script,setHtml,name='unread',timeout=5000}){
 const token='unread-fixture-'+(++sequence);
 setHtml('<meta name="juxin-unread-fixture" content="'+token+'">'+body);
 await wc.loadURL(url);
 const expectedUrl=new URL(url).href,deadline=Date.now()+timeout;
 let page;
 // loadURL can settle while the initial about:blank load is still completing.
 // Readiness is a unique document token, URL and DOM state, never expected count.
 const probe="(() => ({url:location.href,charset:document.characterSet,ready:document.readyState,token:document.querySelector('meta[name=\"juxin-unread-fixture\"]')?.content||''}))()";
 while(true){
  if(wc.isDestroyed())throw new Error('Unread fixture '+name+': renderer closed');
  page=await wc.executeJavaScriptInIsolatedWorld(1001,[{code:probe}]);
  if(page?.url===expectedUrl&&page.token===token&&page.ready!=='loading'){
   if(String(page.charset).toUpperCase()!=='UTF-8')throw new Error('Unread fixture '+name+': expected UTF-8 document; '+JSON.stringify(page));
   break;
  }
  if(Date.now()>=deadline)throw new Error('Unread fixture '+name+': requested document not ready; '+JSON.stringify(page));
  await pause(20);
 }
 const result=await wc.executeJavaScriptInIsolatedWorld(1001,[{code:script}]);
 console.log('CHECK unread fixture '+JSON.stringify({case:name,url:page.url,charset:page.charset,result}));
 return result;
}
module.exports={fixtureResponse,readUnreadFixture};
