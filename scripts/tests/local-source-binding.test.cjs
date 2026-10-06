/* Real temporary public Git commits exercise fresh CJS/native authority.
 * Native receipts here are synthetic contract data, never runtime evidence. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const vm=require('node:vm');
const {createHash}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const binding=require('../local_source_binding.cjs');
const sourceRoot=path.resolve(__dirname,'../..');
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const encoded=value=>Buffer.from(JSON.stringify(value,null,2)+'\n');
function temporaryGit(work){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'public-native-authority-'));
 const saved={...process.env};
 try{
  for(const key of Object.keys(process.env))if(key.startsWith('GIT_'))delete process.env[key];
  const files={'package.json':encoded({version:'3.0.4'}),'BUILD_REVISION.txt':Buffer.from('Source revision: stability-r94\n'),'.gitattributes':Buffer.from('* -text\n')};
  for(const name of ['scripts/ci_source_binding.py','scripts/source_binding_io.py'])files[name]=fs.readFileSync(path.join(sourceRoot,name));
  for(const [name,bytes]of Object.entries(files)){const target=path.join(root,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,bytes)}
  const manifest=encoded(Object.fromEntries(Object.entries(files).map(([name,bytes])=>[name,sha(bytes)])));
  fs.writeFileSync(path.join(root,'SOURCE_SHA256.json'),manifest);
  fs.writeFileSync(path.join(root,binding.CI_MARKER),encoded({schema:2,mode:'flat-git-ci',representation:binding.REPRESENTATION,repository:binding.REPOSITORY,product_version:'3.0.4',source_revision:'stability-r94',source_manifest_sha256:sha(manifest)}));
  const git=(...args)=>execFileSync('git',['-c','core.autocrlf=false','-c','commit.gpgsign=false','-c','user.name=Public source unit test','-c','user.email=fixture@example.invalid','-C',root,...args],{encoding:'utf8',stdio:['ignore','pipe','pipe']}).trim();
  git('init','--quiet');git('add','--all');git('commit','--quiet','-m','Synthetic local provenance fixture');
  Object.assign(process.env,{GITHUB_ACTIONS:'true',GITHUB_SHA:git('rev-parse','HEAD'),GITHUB_REPOSITORY:binding.REPOSITORY.full_name,GITHUB_REPOSITORY_ID:binding.REPOSITORY.id,GITHUB_REPOSITORY_OWNER_ID:binding.REPOSITORY.owner_id});
  return work(root);
 }finally{
  for(const key of Object.keys(process.env))if(!Object.hasOwn(saved,key))delete process.env[key];Object.assign(process.env,saved);
  fs.rmSync(root,{recursive:true,force:true});
 }
}
function originalFixtureProof(testFile,functionName,context){
 const text=fs.readFileSync(testFile,'utf8');const start=text.indexOf('function '+functionName+'(');assert.ok(start>=0);
 const end=text.indexOf('\ntest(',start);assert.ok(end>start);
 return JSON.parse(JSON.stringify(vm.runInNewContext(text.slice(start,end)+';'+functionName+'()',context)));
}
function withNativeRoot(root,work){
 const original=binding.assertSourceIdentity;
 binding.assertSourceIdentity=(proof,options)=>original(proof,{...options,root});
 try{return work()}finally{binding.assertSourceIdentity=original}
}
test('actual temporary public commit carries complete bytes and rechecks all receipt fields',()=>temporaryGit(root=>{
 const identity=binding.collectSourceIdentity(root);
 assert.equal(identity.source_commit,process.env.GITHUB_SHA);assert.equal(identity.source_provenance.effective_files_verified,5);
 binding.assertSourceIdentity(identity,{root,required:true});
 for(const field of Object.keys(identity.source_provenance)){
  const changed=structuredClone(identity);changed.source_provenance[field]='changed';assert.throws(()=>binding.assertSourceIdentity(changed,{root,required:true}));
 }
 fs.appendFileSync(path.join(root,'BUILD_REVISION.txt'),'changed after receipt\n');
 assert.throws(()=>binding.assertSourceIdentity(identity,{root,required:true}));
}));
test('cleanup contract uses actual source authority and keeps mandatory native scenarios',()=>temporaryGit(root=>withNativeRoot(root,()=>{
 const fixture=require('../../desktop/tests/nurture-cleanup-native-r63.cjs');
 const proof=originalFixtureProof(path.join(sourceRoot,'desktop/tests/nurture-cleanup-native-r63.test.cjs'),'completeProof',{fixture});
 Object.assign(proof,binding.collectSourceIdentity(root));fixture.assertProofComplete(proof);
 const missing=structuredClone(proof);delete missing.scenarios[fixture.SCENARIOS[0]];assert.throws(()=>fixture.assertProofComplete(missing),/native scenario contract/);
 const changed=structuredClone(proof);changed.source_provenance.repository.id='different';assert.throws(()=>fixture.assertProofComplete(changed),/effective source bytes/);
 fs.appendFileSync(path.join(root,'BUILD_REVISION.txt'),'tampered\n');assert.throws(()=>fixture.assertProofComplete(proof));
})));
test('recovery UI keeps native Windows, trusted-input, screenshot and fresh-source requirements',()=>temporaryGit(root=>withNativeRoot(root,()=>{
 const fixture=require('../../desktop/tests/recovery-ui-native-r64.cjs');
 const proof=originalFixtureProof(path.join(sourceRoot,'desktop/tests/recovery-ui-native-r64.test.cjs'),'complete',{SCENARIOS:fixture.SCENARIOS});
 Object.assign(proof,binding.collectSourceIdentity(root),{required_mode:true,platform:'win32',windows_release_status:'passed'});fixture.assertProofComplete(proof);
 for(const patch of [{platform:'linux'},{headless:true},{native_window:{visible:false}},{source_commit:'a'.repeat(40)}])assert.throws(()=>fixture.assertProofComplete({...proof,...patch}));
 const incomplete=structuredClone(proof);incomplete.screenshots.pop();assert.throws(()=>fixture.assertProofComplete(incomplete));
 const untrusted=structuredClone(proof);untrusted.native_input[0].trusted=false;assert.throws(()=>fixture.assertProofComplete(untrusted));
 const missing=structuredClone(proof);delete missing.source_provenance;assert.throws(()=>fixture.assertProofComplete(missing));
 fs.appendFileSync(path.join(root,'BUILD_REVISION.txt'),'tampered\n');assert.throws(()=>fixture.assertProofComplete(proof));
})));
