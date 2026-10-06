import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from 'typescript';
import {createCollectorCoreClient} from '../src/core-client.ts';
import {parseCollectionSeed, parseCollectionSeedDraft, collectionSeedIdentity,
  collectionPlatform, collectionPlatformWindows, collectionProfileLabel, collectionProfileUrl} from '../src/collection-platform.ts';
import {readDiscardCountLimits, discardCountLimitsPayload} from '../src/collection-discard-limits.ts';
import {collectionRowFixture, buttonByText, elementText} from './collection-row-r43-fixture.mjs';
import {renderToStaticMarkup} from 'react-dom/server';

const source = readFileSync(new URL('../src/formal-workbench.tsx', import.meta.url), 'utf8');
const ast = ts.createSourceFile('workbench.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function actualHandler(name) {
  let found;
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node; ts.forEachChild(node, visit); }
  visit(ast); assert.ok(found, `actual ${name} handler`);
  return ts.transpileModule(found.getText(ast), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
}
const createCode = actualHandler('createTask');
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return {promise, resolve}; };
function createFixture(overrides = {}) {
  const state = {draft: 'https://www.instagram.com/alice/', platform: 'instagram', ...overrides};
  const calls = [], errors = [];
  const ctx = {followers: true, following: true, platform: state.platform,
    targetDraft: state.draft, draftSeeds: parseCollectionSeedDraft(state.draft, state.platform),
    createTaskRef: {current: false}, claimableWaitingCount: 1, selectedWindowOrder: ['w1'],
    collectionWindows: [{id: 'w1', name: 'Window 1'}], filtersValid: true,
    autoClassify: true, openAiReview: true, excludeVerified: true, excludePublicZeroPosts: true,
    discardLimits: readDiscardCountLimits({}), discardCountLimitsPayload, parallelScreeningWorkers: 3,
    setTargetDraft: update => {state.draft = update(state.draft);}, ...overrides.context,
    run: async (_key, action) => {try {await action(client); return true;} catch (error) {errors.push(error.message);return false;}},
  };
  const client = {
    async createCollectionTask(payload) {calls.push(['create', JSON.parse(JSON.stringify(payload))]); if (state.gate) await state.gate.promise; return {task_id: 'ig-task'};},
    async controlCollectionTask(...args) {calls.push(['control', ...args]); if (state.startError) throw Error('Start failed');},
  };
  return {state, ctx, calls, errors, create: runInNewContext(createCode + '\ncreateTask;', ctx)};
}

test('Instagram defaults preserve usernames, profile links, identity and locked windows', () => {
  assert.equal(collectionPlatform({}), 'instagram');
  assert.deepEqual(parseCollectionSeed('@alice'), {platform:'instagram',target:'alice',label:'alice'});
  assert.equal(parseCollectionSeed('https://instagram.com/alice/?igsh=abc').target,'alice');
  assert.equal(collectionSeedIdentity('https://www.instagram.com/Alice/'),'alice');
  assert.equal(collectionProfileLabel({username:'@alice'}),'alice');
  assert.equal(collectionProfileUrl({username:'alice',profile_url:'https://evil.test/foo'}),'https://www.instagram.com/alice/');
  const locked={id:'busy',platform:'instagram',locked:true};
  assert.deepEqual(collectionPlatformWindows([{id:'legacy'},locked,{id:'fb',platform:'facebook'},{id:'wa',platform:'whatsapp'}]),[{id:'legacy'},locked]);
});

test('Facebook profiles, foreign records, unsupported platforms, posts and credentials fail closed', () => {
  for(const input of ['https://facebook.com/alice','facebook.com/alice','https://m.facebook.com/profile.php?id=123','https://facebook.com/people/Alice/123','fb:alice','https://instagram.com.evil.test/alice','https://alice:secret@instagram.com/alice','https://instagram.com:4431/alice','https://instagram.com/p/post','https://instagram.com/reel/reel','https://instagram.com/accounts/login','javascript:alert(1)'])assert.equal(parseCollectionSeed(input),null,input);
  assert.equal(parseCollectionSeed('alice','facebook'),null);
  for(const record of [{platform:'facebook'},{settings:{platform:'facebook'}},{profile:{platform:'facebook'}},{username:'fb:alice'}]){
    assert.equal(collectionPlatform(record),null);assert.equal(collectionProfileUrl(record),'#');assert.equal(collectionProfileLabel(record),'');
  }
});

test('seed batches retain every valid Instagram target without an application cap', () => {
  const draft=Array.from({length:2501},(_,i)=>`person.${i}`).join('\n');
  assert.equal(parseCollectionSeedDraft(draft).targets.length,2501);
  assert.equal(parseCollectionSeedDraft('alice https://instagram.com/alice/').targets.length,1);
  assert.equal(parseCollectionSeedDraft('alice https://facebook.com/bob/').valid,false);
});

test('actual IG task creation retains modes, waiting queue, filters and parallel settings', async () => {
  const f=createFixture();await f.create();const payload=f.calls[0][1];
  assert.deepEqual(f.calls.map(c=>c[0]),['create','control']);
  assert.equal(payload.platform,'instagram');assert.deepEqual(payload.targets,[]);assert.deepEqual(payload.modes,['followers','following']);
  assert.equal(payload.discard_count_limits_enabled,true);assert.equal(payload.parallel_screening_workers,3);
  for(const field of ['auto_classify','gpt_review','exclude_verified','exclude_public_zero_posts'])assert.equal(payload[field],true,field);
  assert.equal(f.state.draft,'https://www.instagram.com/alice/');
});

test('double-click task creation stays single-flight and newer draft edits survive completion',async()=>{
  const f=createFixture();f.state.gate=deferred();const first=f.create();await f.create();assert.equal(f.calls.length,1);
  f.state.draft='new.target';f.state.gate.resolve();await first;
  assert.deepEqual(f.calls.map(c=>c[0]),['create','control']);assert.equal(f.state.draft,'new.target');assert.equal(f.ctx.createTaskRef.current,false);
});

test('failed task start retains input; locked windows and empty queue never dispatch',async()=>{
 const failed=createFixture();failed.state.startError=true;await failed.create();assert.deepEqual(failed.errors,['Start failed']);assert.equal(failed.ctx.createTaskRef.current,false);assert.equal(failed.state.draft,'https://www.instagram.com/alice/');
 for(const f of [createFixture({context:{claimableWaitingCount:0}}),createFixture({context:{collectionWindows:[{id:'w1',locked:true}]}})]){await f.create();assert.deepEqual(f.calls,[])}
});

test('draft changes and responsive layout never cancel a running task',()=>{
 const handler=actualHandler('updateTargetDraft'),changes=[];
 runInNewContext(handler+'\nupdateTargetDraft;',{setTargetDraft:v=>changes.push(v)})('alice');assert.deepEqual(changes,['alice']);
 assert.doesNotMatch(handler,/controlCollection|pause|stop|delete/);
 const footer=readFileSync(new URL('../src/collection-fixed-footer-layout.ts',import.meta.url),'utf8');assert.match(footer,/addEventListener\("resize", schedule\)/);assert.doesNotMatch(footer,/controlCollection|task_control/);
});

test('actual waiting handler canonicalizes IG URLs and rejects unsupported seeds before transport',async()=>{
 for(const [draft,save] of [['https://www.instagram.com/seed.profile/',true],['https://www.facebook.com/seed.profile/',false]]){
  const state={draft},calls=[],errors=[];
  const ctx={platform:'instagram',targetDraft:draft,parseCollectionSeedDraft,collectionSeedIdentity,draftAddRef:{current:false},setTargetDraft:update=>{state.draft=update(state.draft)},addSplitTargetsWithConfirmation:async(_client,names,_ids,onSaved)=>{calls.push([...names]);onSaved([{username:'seed.profile'}])},run:async(_key,action)=>{try{await action({});return true}catch(error){errors.push(error.message);return false}},ensureAssignedWindowsJoined:async()=>true};
  const add=runInNewContext(actualHandler('parsedDraftTargets')+actualHandler('addDraftTargets')+'\naddDraftTargets;',ctx);
  assert.equal(await add(),save);assert.equal(calls.length,save?1:0);assert.equal(state.draft,save?'':draft);if(!save)assert.match(errors[0],/Instagram/);assert.equal(ctx.draftAddRef.current,false);
 }
});

test('wolf refresh and all IG controls remain while FB interface and platform switching are absent',()=>{
 assert.match(source,/<button type="button" className="formal-brand" aria-label="刷新" title="刷新"[^>]*onClick=\{\(\) => void refresh\(\)\}><img src=".\/war-wolf.svg"/);
 assert.match(source,/\(\[1, 2, 3\] as ParallelScreeningWorkers\[\]\)/);
 assert.match(source,/value === 2 \|\| value === 3 \? value : 1/);
 for(const name of ['formal-workbench.tsx','workbench-platform.tsx','collection-platform.css','account-workspace.tsx','account-batch-creator.tsx','reports-workspace.tsx'])assert.doesNotMatch(readFileSync(new URL('../src/'+name,import.meta.url),'utf8'),/facebook|workbench-platform-toggle|\bsetPlatform\b|显示平台/i,name);
});

test('whole-task deletion retains collected results and dedupe instead of reimporting sources',()=>{
 const start=source.indexOf('<CollectionTaskList snapshot={snapshot}');const callback=source.slice(start,source.indexOf('}} /></div>',start));
 assert.match(callback,/client.deleteCollectionTask\(task.id\)/);assert.doesNotMatch(callback,/addWaitingSplitTargets|已退回|returnedTargets/);assert.match(callback,/采集结果和去重记录已保留/);
});
