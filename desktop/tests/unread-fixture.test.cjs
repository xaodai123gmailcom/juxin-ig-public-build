const test=require('node:test'),assert=require('node:assert/strict');
const {fixtureResponse,readUnreadFixture}=require('./unread-fixture.cjs');

test('local fixture declares UTF-8 in transport and markup; Chinese bytes stay intact',async()=>{
 const body='<button aria-label="聊天">未读 <span>1</span></button>';
 const response=fixtureResponse(body);
 assert.match(response.headers.get('content-type'),/charset=utf-8/i);
 const bytes=await response.arrayBuffer();
 assert.ok(new TextDecoder('utf-8',{fatal:true}).decode(bytes).endsWith(body));
 assert.ok(new TextDecoder('utf-8').decode(bytes).startsWith('<!doctype html><meta charset="utf-8">'));
 assert.equal(new TextDecoder('windows-1252').decode(bytes).includes('未读'),false,'missing encoding can destroy the semantic selector text');
});

test('initial blank and previous same-URL documents cannot satisfy unread readiness',async()=>{
 let html,reads=0,productionCalls=0;
 const wc={isDestroyed:()=>false,loadURL:async()=>{},executeJavaScriptInIsolatedWorld:async(_world,[{code}])=>{
  if(code==='PRODUCTION_READER'){productionCalls++;assert.equal(reads,3);return {count:1,status:'live'}}
  reads++;
  return {url:reads===1?'about:blank':'https://web.whatsapp.com/',charset:'UTF-8',ready:'complete',token:reads<3?'old-document':/content="([^"]+)"/.exec(html)[1]};
 }};
 const result=await readUnreadFixture(wc,{body:'test',url:'https://web.whatsapp.com/',script:'PRODUCTION_READER',setHtml:value=>{html=value},name:'readiness'});
 assert.equal(result.count,1);assert.equal(productionCalls,1);
});

test('wrong encoding fails before reading; unknown count is returned unchanged without retry',async()=>{
 let html,charset='windows-1252',productionCalls=0;
 const wc={isDestroyed:()=>false,loadURL:async()=>{},executeJavaScriptInIsolatedWorld:async(_world,[{code}])=>{
  if(code==='PRODUCTION_READER'){productionCalls++;return {count:null,status:'unavailable'}}
  return {url:'https://web.whatsapp.com/',charset,ready:'complete',token:/content="([^"]+)"/.exec(html)[1]};
 }};
 const options={body:'test',url:'https://web.whatsapp.com/',script:'PRODUCTION_READER',setHtml:value=>{html=value},name:'encoding'};
 await assert.rejects(readUnreadFixture(wc,options),/expected UTF-8/);assert.equal(productionCalls,0);
 charset='UTF-8';assert.equal((await readUnreadFixture(wc,options)).count,null);assert.equal(productionCalls,1);
});

test('wrong document times out with its URL and case; no unread result is manufactured',async()=>{
 const wc={isDestroyed:()=>false,loadURL:async()=>{},executeJavaScriptInIsolatedWorld:async()=>({url:'about:blank',token:'',ready:'complete',charset:'UTF-8'})};
 await assert.rejects(readUnreadFixture(wc,{body:'test',url:'https://web.whatsapp.com/',script:'PRODUCTION_READER',setHtml:()=>{},name:'wrong-document',timeout:0}),/wrong-document: requested document not ready.*about:blank/);
});
