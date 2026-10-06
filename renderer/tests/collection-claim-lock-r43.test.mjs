import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import ts from 'typescript';
import {createCollectorCoreClient} from '../src/core-client.ts';
import {collectionClaimLockFixture,buttonByText,elementNodes,elementText} from './collection-row-r43-fixture.mjs';

test('global claim lock toggles only new claiming and never changes individual locks or running tasks', async () => {
  for(const locked of [false,true]) {
    const f=collectionClaimLockFixture({globallyLocked:locked,locked:true});
    const panel=f.global();
    const button=buttonByText(panel,locked?'解锁领取':'锁定领取');
    assert.ok(button);
    assert.equal(button.props['aria-pressed'],locked);
    assert.match(elementText(panel),locked?/新目标领取已锁定/:/新目标可自动领取/);
    button.props.onClick();await Promise.resolve();
    assert.deepEqual(f.calls,[['all',!locked]]);
    assert.equal(f.candidate.locked,true);
    assert.equal(f.runs[0].key,'collection-claim-lock');
  }
});

test('individual locking stays independent of the global lock and its actual waiting row remains explicit', async () => {
  for(const globallyLocked of [false,true]) for(const locked of [false,true]) {
    const f=collectionClaimLockFixture({globallyLocked,locked});
    const row=f.waiting();
    const button=buttonByText(row,locked?'解锁':'锁定');
    assert.equal(button.props['aria-pressed'],locked);
    if(locked) assert.match(elementText(row),/已单独锁定 · 不会被领取/);
    else if(globallyLocked) assert.match(elementText(row),/全局领取已锁定 · 保持等待/);
    button.props.onClick();await Promise.resolve();
    assert.deepEqual(f.calls,[['candidate','waiting-1',!locked]]);
    assert.equal(f.runs[0].key,'collection-waiting-waiting-1');
  }
});

test('locked waiting entries can still edit assignment and delete, but all row controls share pending state', async () => {
  const f=collectionClaimLockFixture({globallyLocked:true,locked:true,specified:true});
  const row=f.waiting();
  buttonByText(row,'指定 2').props.onClick();
  const remove=elementNodes(row).find(n=>n.props?.['aria-label']==='删除 waiting_account');
  remove.props.onClick();await Promise.resolve();
  assert.deepEqual(f.calls,[['assign','waiting-1'],['delete','waiting-1']]);
  const busy=collectionClaimLockFixture({globallyLocked:true,locked:true,pending:true});
  assert.ok(elementNodes(busy.waiting()).filter(n=>n.type==='button').every(b=>b.props.disabled===true));
  assert.deepEqual([...new Set(busy.disabledKeys)],['collection-waiting-waiting-1']);
  assert.equal(buttonByText(busy.global(),'解锁领取').props.disabled,true);
});

test('client dispatches exact boolean claim-lock payloads and accepts unlock false without coercion', async () => {
  const calls=[];
  const client=createCollectorCoreClient({secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,
    configureIntegrations:async()=>({restarted:true}),request:async(path,options)=>{
    calls.push({path,body:options.body});
    return {command:options.body.type,result:{locked:options.body.payload.locked},snapshot_seq:calls.length};
  }});
  await client.setWaitingSplitTargetLocked('candidate-1',true);
  await client.setWaitingSplitTargetLocked('candidate-1',false);
  await client.setSplitClaimLocked(true);
  await client.setSplitClaimLocked(false);
  assert.deepEqual(calls.map(c=>c.body),[
    {type:'split_waiting_lock',payload:{candidate_id:'candidate-1',locked:true}},
    {type:'split_waiting_lock',payload:{candidate_id:'candidate-1',locked:false}},
    {type:'split_claim_lock',payload:{locked:true}},
    {type:'split_claim_lock',payload:{locked:false}},
  ]);
  assert.ok(calls.every(c=>c.path==='/api/workbench/commands'));
});

test('starting a new task exits before any mutation when global or all individual locks leave zero claimable entries', async () => {
  const source=readFileSync(new URL('../src/formal-workbench.tsx',import.meta.url),'utf8');
  const ast=ts.createSourceFile('workbench.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  let create;
  function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text==='createTask')create=node;ts.forEachChild(node,visit);}
  visit(ast);assert.ok(create);
  const compiled=ts.transpileModule(create.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  let mutations=0;
  const fn=runInNewContext(compiled+'\ncreateTask;',{
    facebook:false,createTaskRef:{current:false},followers:true,following:false,claimableWaitingCount:0,selectedWindowOrder:['w1'],filtersValid:true,
    run:async()=>{mutations++;},
  });
  await fn();assert.equal(mutations,0);
});

test('claim-lock snapshot state remains boolean and malformed state cannot silently unlock the UI', async () => {
  const base={revision:1,generated_at:new Date().toISOString(),counts:{},dedupe:{total:0},
    pending:{public:[],private:[]},approved:{public:[],private:[]},
    history:{manual_rejections:[],collection_exclusions:[]},windows:[],sources:[],tasks:[],campaigns:[],truncated:false};
  const bridge=value=>({secureSet:async()=>true,secureGet:async()=>null,secureDelete:async()=>true,
    configureIntegrations:async()=>({restarted:true}),request:async()=>({...base,split_claim_locked:value})});
  assert.equal((await createCollectorCoreClient(bridge(true)).snapshot()).split_claim_locked,true);
  assert.equal((await createCollectorCoreClient(bridge(false)).snapshot()).split_claim_locked,false);
  await assert.rejects(createCollectorCoreClient(bridge('false')).snapshot(),/Invalid split_claim_locked/);
});
