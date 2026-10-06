/* Adapter contracts; real temporary commits and adversarial Git cases are Python tests. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const binding=require('../local_source_binding.cjs');
function env(values,work){const saved={...process.env};Object.assign(process.env,{GITHUB_REPOSITORY:binding.REPOSITORY.full_name,GITHUB_REPOSITORY_ID:binding.REPOSITORY.id,GITHUB_REPOSITORY_OWNER_ID:binding.REPOSITORY.owner_id});for(const [key,value]of Object.entries(values)){if(value===null)delete process.env[key];else process.env[key]=value}try{return work()}finally{for(const key of Object.keys(process.env))if(!Object.hasOwn(saved,key))delete process.env[key];Object.assign(process.env,saved)}}
function temporary(work){const root=fs.mkdtempSync(path.join(os.tmpdir(),'ci-source-adapter-'));try{return work(root)}finally{fs.rmSync(root,{recursive:true,force:true})}}
const synthetic={schema:2,mode:'flat-git-ci',representation:binding.REPRESENTATION,repository:binding.REPOSITORY,execution:'github-actions',source_commit:'a'.repeat(40),source_manifest_sha256:'b'.repeat(64),git_checkout:true,github_actions:true};
test('explicit CI adapter carries full verified identity and never reads a convenient Git SHA',()=>temporary(root=>env({GITHUB_ACTIONS:'true',GITHUB_SHA:synthetic.source_commit},()=>{
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');
 const actual=binding.collectSourceIdentity(root,{getGit(){throw Error('must use full verifier')},verifyCi(received){assert.equal(received,root);return synthetic}});
 assert.deepEqual(actual,{source_commit:synthetic.source_commit,github_sha:synthetic.source_commit,source_provenance:synthetic});
 binding.assertSourceIdentity(actual,{root,required:true,verifyCi:()=>synthetic});
 for(const patch of [{source_commit:'c'.repeat(40)},{github_sha:null},{source_provenance:{...synthetic,source_manifest_sha256:'c'.repeat(64)}}])assert.throws(()=>binding.assertSourceIdentity({...actual,...patch},{root,verifyCi:()=>synthetic}));
})));
test('mixed markers and missing CI marker reject before a supplied verifier',()=>temporary(root=>env({GITHUB_ACTIONS:' TRUE ',GITHUB_SHA:synthetic.source_commit},()=>{
 const options={verifyCi(){throw Error('must not run')}};
 assert.throws(()=>binding.collectSourceIdentity(root,options),/explicit flat-Git/);
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');fs.writeFileSync(path.join(root,binding.PRIVATE_MARKERS[0]),'{}');
 assert.throws(()=>binding.collectSourceIdentity(root,options),/must not be mixed/);
})));
test('CI proof cannot drop provenance and fall back to matching SHA strings',()=>env({GITHUB_ACTIONS:'true',GITHUB_SHA:synthetic.source_commit},()=>{
 assert.throws(()=>binding.assertSourceIdentity({source_commit:synthetic.source_commit,github_sha:synthetic.source_commit},{required:true}),/full public CI provenance/);
}));
test('CI validator errors cannot downgrade into local or legacy mode',()=>temporary(root=>env({GITHUB_ACTIONS:'true',GITHUB_SHA:synthetic.source_commit},()=>{
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');
 assert.throws(()=>binding.collectSourceIdentity(root,{verifyCi(){throw Error('Git blob mismatch')},getGit(){throw Error('fallback forbidden')}}),/Git blob mismatch/);
})));
test('public marker requires real Actions execution',()=>temporary(root=>env({GITHUB_ACTIONS:null,GITHUB_SHA:null},()=>{
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');
 assert.throws(()=>binding.collectSourceIdentity(root),/actual Actions execution/);
})));

test('required acceptance rejects SHA-only receipts with either marker and without CI environment',()=>temporary(root=>env({GITHUB_ACTIONS:null,GITHUB_SHA:null},()=>{
 for(const marker of [binding.PRIVATE_MARKERS[0],binding.CI_MARKER]){
  fs.writeFileSync(path.join(root,marker),'{}');
  let calls=0;const options={root,required:true,verifyCi(){calls++;throw Error('missing receipt must not invoke authority')}};
  for(const proof of [{source_commit:null},{source_commit:'a'.repeat(40)},{source_commit:'a'.repeat(40),github_sha:'a'.repeat(40)}])assert.throws(()=>binding.assertSourceIdentity(proof,options),/full public CI provenance/);
  assert.throws(()=>binding.assertSourceIdentity({source_commit:'a'.repeat(40)},{...options,required:false}),/full public CI provenance/);
  assert.equal(calls,0);fs.unlinkSync(path.join(root,marker));
 }
 assert.throws(()=>binding.assertSourceIdentity({source_commit:'a'.repeat(40)},{root,required:true}),/full public CI provenance/);
 assert.throws(()=>binding.assertSourceIdentity({source_commit:'a'.repeat(40)},{root,required:false}),/cannot claim a source commit/);
 assert.deepEqual(binding.collectSourceIdentity(root,{getGit(){throw Error('No legacy Git lookup may run')}}),{source_commit:null});
})));
test('unknown provenance mode and absent CI execution cannot select a third required mode',()=>temporary(root=>env({GITHUB_ACTIONS:null,GITHUB_SHA:null},()=>{
 const options={root,required:true,verifyCi(){throw Error('must not invoke authority without CI environment')}};
 assert.throws(()=>binding.assertSourceIdentity({source_commit:null,source_provenance:{mode:'sha-only'}},options),/Unsupported source provenance mode/);
 assert.throws(()=>binding.assertSourceIdentity({source_commit:synthetic.source_commit,github_sha:synthetic.source_commit,source_provenance:synthetic},options),/actual Actions execution/);
})));

test('every repository environment field is mandatory and exact',()=>temporary(root=>{
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');
 for(const variable of ['GITHUB_REPOSITORY','GITHUB_REPOSITORY_ID','GITHUB_REPOSITORY_OWNER_ID'])for(const wrong of [null,'different'])env({GITHUB_ACTIONS:'true',GITHUB_SHA:synthetic.source_commit,[variable]:wrong},()=>{
  assert.throws(()=>binding.collectSourceIdentity(root,{verifyCi(){throw Error('must not execute wrong repository authority')}}),/repository identity mismatch/);
 });
}));
test('public receipt cannot substitute an old schema, repository, representation or execution claim',()=>temporary(root=>env({GITHUB_ACTIONS:'true',GITHUB_SHA:synthetic.source_commit},()=>{
 fs.writeFileSync(path.join(root,binding.CI_MARKER),'{}');
 for(const change of [{schema:1},{schema:true},{representation:'other'},{repository:{...binding.REPOSITORY,id:'other'}},{execution:'local'},{git_checkout:1},{github_actions:1}])assert.throws(()=>binding.collectSourceIdentity(root,{verifyCi:()=>({...synthetic,...change})}));
})));
