const test=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {waitFixtureDocument,loadFixtureDocument}=require('./navigation-fixture.cjs');

const URL='http://127.0.0.1:9999/source';
const options={label:'independent source fixture',heading:'/source',timeoutMs:1000};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
class Contents extends EventEmitter {
 url=URL;loading=false;dead=false;document={url:URL,readyState:'complete',heading:'/source'};
 loadCalls=[];readCalls=0;
 isDestroyed(){return this.dead}
 getURL(){assert.equal(this.dead,false,'must not read native URL after destruction');return this.url}
 isLoadingMainFrame(){assert.equal(this.dead,false,'must not read loading state after destruction');return this.loading}
 executeJavaScript(){this.readCalls++;return this.read?this.read():Promise.resolve({...this.document})}
 loadURL(url){this.loadCalls.push(url);return this.load?this.load(url):Promise.resolve()}
}
const snapshot=contents=>new Map(contents.eventNames().map(name=>[name,contents.listeners(name)]));
const unchangedListeners=(contents,before)=>{
 assert.deepEqual(contents.eventNames(),[...before.keys()]);
 for(const [name,listeners]of before)assert.deepEqual(contents.listeners(name),listeners,`${name} listeners restored`);
};
function existingListener(contents){const fn=()=>{};contents.on('did-finish-load',fn);return snapshot(contents)}
async function failure(contents,operation,pattern=/Fixture document not ready/){
 const before=existingListener(contents);
 await assert.rejects(operation(),pattern);
 unchangedListeners(contents,before);
}

// Each test controls native conditions independently. A fulfilled load promise
// and a matching URL alone must never bypass document identity/readiness.
test('observation accepts the exact completed document without navigating',async()=>{
 const contents=new Contents(),before=existingListener(contents);
 assert.deepEqual(await waitFixtureDocument(contents,URL,options),contents.document);
 assert.deepEqual(contents.loadCalls,[]);assert.equal(contents.readCalls,1);
 unchangedListeners(contents,before);
});

test('load mode requests exactly one navigation and retains existing listeners',async()=>{
 const contents=new Contents(),before=existingListener(contents);
 await loadFixtureDocument(contents,URL,options);
 assert.deepEqual(contents.loadCalls,[URL]);unchangedListeners(contents,before);
});

test('completed blank document can be observed without starting navigation',async()=>{
 const contents=new Contents();contents.url='about:blank';contents.document={url:'about:blank',readyState:'complete',heading:''};
 await waitFixtureDocument(contents,'about:blank',{timeoutMs:1000});
 assert.deepEqual(contents.loadCalls,[]);
});

test('matching DOM under a different native URL is rejected',async()=>{
 const contents=new Contents();contents.url='about:blank';
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"url":"about:blank"/);
 assert.equal(contents.readCalls,0);assert.deepEqual(contents.loadCalls,[]);
});

test('matching native URL with a different DOM document is rejected',async()=>{
 const contents=new Contents();contents.document.url='about:blank';
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"document":\{"url":"about:blank"/);
 assert.ok(contents.readCalls>0);assert.deepEqual(contents.loadCalls,[]);
});

test('wrong fixture heading is rejected even when URL and readyState match',async()=>{
 const contents=new Contents();contents.document.heading='/other';
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"heading":"\/other"/);
});

test('an indefinitely loading page fails without entering the renderer',async()=>{
 const contents=new Contents();contents.loading=true;
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"loading":true/);
 assert.equal(contents.readCalls,0);assert.deepEqual(contents.loadCalls,[]);
});

test('interactive document is not accepted before it becomes complete',async()=>{
 const contents=new Contents(),firstRead=deferred();contents.document.readyState='interactive';
 contents.read=()=>{firstRead.resolve();return Promise.resolve({...contents.document})};
 let finished=false;
 const waiting=waitFixtureDocument(contents,URL,options).then(value=>{finished=true;return value});
 await firstRead.promise;await Promise.resolve();assert.equal(finished,false);
 contents.document.readyState='complete';
 assert.equal((await waiting).readyState,'complete');assert.deepEqual(contents.loadCalls,[]);
});

test('a document that never reaches complete times out',async()=>{
 const contents=new Contents();contents.document.readyState='interactive';
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"readyState":"interactive"/);
});

test('fulfilled load promise is insufficient when the destination never commits',async()=>{
 const contents=new Contents();contents.url='about:blank';contents.document.url='about:blank';
 await failure(contents,()=>loadFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"navigationSettled":true/);
 assert.deepEqual(contents.loadCalls,[URL]);
});

test('destination is not accepted while its native load promise is still pending',async()=>{
 const contents=new Contents(),load=deferred();contents.load=()=>load.promise;
 let finished=false;
 const waiting=loadFixtureDocument(contents,URL,options).then(value=>{finished=true;return value});
 await Promise.resolve();assert.equal(finished,false);assert.equal(contents.readCalls,0);
 load.resolve();await waiting;assert.deepEqual(contents.loadCalls,[URL]);
});

test('synchronous native load exception propagates and listeners are removed',async()=>{
 const contents=new Contents(),error=new Error('native synchronous navigation failure');contents.load=()=>{throw error};
 const before=existingListener(contents);
 await assert.rejects(loadFixtureDocument(contents,URL,options),caught=>caught===error);
 assert.deepEqual(contents.loadCalls,[URL]);unchangedListeners(contents,before);
});

test('asynchronous native load rejection retains its original cause',async()=>{
 const contents=new Contents(),error=new Error('native async navigation failure');contents.load=()=>Promise.reject(error);
 const before=existingListener(contents);
 await assert.rejects(loadFixtureDocument(contents,URL,options),caught=>{
  assert.equal(caught.cause,error);assert.match(caught.message,/native async navigation failure/);return true;
 });
 assert.deepEqual(contents.loadCalls,[URL]);unchangedListeners(contents,before);
});

test('native load rejection arriving after timeout remains handled',async()=>{
 const contents=new Contents(),load=deferred();contents.load=()=>load.promise;
 await failure(contents,()=>loadFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"navigationSettled":false/);
 load.reject(new Error('late native rejection'));
 // Node's test runner itself flags unhandled rejections; yield through its turn.
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(contents.loadCalls,[URL]);
});

test('renderer read that never settles remains bounded and cleans listeners',async()=>{
 const contents=new Contents();contents.read=()=>new Promise(()=>{});
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/Renderer fixture timed out/);
});

test('native contents destroyed before observation are not read or passed',async()=>{
 const contents=new Contents();contents.dead=true;
 await failure(contents,()=>waitFixtureDocument(contents,URL,options),/"destroyed":true/);
 assert.equal(contents.readCalls,0);assert.deepEqual(contents.loadCalls,[]);
});

test('native destruction during a renderer read cannot pass with stale data',async()=>{
 const contents=new Contents(),reading=deferred();contents.read=()=>{contents.dead=true;contents.emit('destroyed');return reading.promise};
 const before=existingListener(contents),waiting=waitFixtureDocument(contents,URL,options);
 await Promise.resolve();reading.resolve({...contents.document});
 await assert.rejects(waiting,/"destroyed":true/);unchangedListeners(contents,before);
});

test('a changed native URL after the DOM read is rechecked and rejected',async()=>{
 const contents=new Contents();contents.read=()=>{contents.url='about:blank';return Promise.resolve({...contents.document})};
 await failure(contents,()=>waitFixtureDocument(contents,URL,{...options,timeoutMs:100}),/"url":"about:blank"/);
});

test('renderer read rejection preserves its cause and cleans all observation listeners',async()=>{
 const contents=new Contents(),error=new Error('fixture context gone');contents.read=()=>Promise.reject(error);
 const before=existingListener(contents);
 await assert.rejects(waitFixtureDocument(contents,URL,options),caught=>{assert.equal(caught.cause,error);return true});
 unchangedListeners(contents,before);
});

test('invalid deadlines reject before any listener or navigation is installed',async()=>{
 for(const timeoutMs of [0,-1,NaN,Infinity]){
  const contents=new Contents(),before=snapshot(contents);
  await assert.rejects(loadFixtureDocument(contents,URL,{...options,timeoutMs}),/Invalid fixture document deadline/);
  unchangedListeners(contents,before);assert.deepEqual(contents.loadCalls,[]);
 }
});
