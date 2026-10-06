/* Public source adapter. Required evidence always rechecks the complete real CI checkout. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const CI_MARKER='CI_SOURCE_PROVENANCE.json';
const PRIVATE_MARKERS=['LOCAL_SOURCE_PROVENANCE.json','LOCAL_BASE_SOURCE_SHA256.json','ARCHIVE_SOURCE_SHA256.json','ARCHIVE_SOURCE_PROVENANCE.json'];
const REPRESENTATION='public-sanitized-source';
const REPOSITORY=Object.freeze({full_name:'xaodai123gmailcom/juxin-ig-public-build',id:'1406784621',owner_id:'337452708'});
function present(file){try{fs.lstatSync(file);return true}catch(error){if(error.code==='ENOENT')return false;throw error}}
function ciIdentityPresent(env=process.env){return Boolean(env.GITHUB_SHA)||String(env.GITHUB_ACTIONS||'').trim().toLowerCase()==='true'}
function assertPublicRoot(root){assert.ok(!PRIVATE_MARKERS.some(name=>present(path.join(root,name))),'Private and public source controls must not be mixed')}
function assertCiEnvironment(){
 assert.equal(String(process.env.GITHUB_ACTIONS||'').trim().toLowerCase(),'true','CI proof requires actual Actions execution');
 assert.match(process.env.GITHUB_SHA||'',/^[0-9a-f]{40}$/,'CI identity requires an exact GITHUB_SHA');
 for(const [variable,field]of [['GITHUB_REPOSITORY','full_name'],['GITHUB_REPOSITORY_ID','id'],['GITHUB_REPOSITORY_OWNER_ID','owner_id']])assert.equal(process.env[variable],REPOSITORY[field],'CI repository identity mismatch: '+variable);
}
function assertPublicAuthority(result){
 assert.equal(result?.schema,2,'Public source provenance schema is required');
 assert.equal(result.mode,'flat-git-ci','Unsupported source provenance mode');
 assert.equal(result.representation,REPRESENTATION,'Public sanitized representation is required');
 assert.deepEqual(result.repository,REPOSITORY,'Public repository identity is required');
 assert.equal(result.execution,'github-actions');assert.equal(result.git_checkout,true);assert.equal(result.github_actions,true);
 assert.equal(result.source_commit,process.env.GITHUB_SHA,'native proof matches the actual CI checkout');
}
function verifyCiSourceBinding(root){
 assertPublicRoot(root);assertCiEnvironment();
 const candidates=process.platform==='win32'?[path.join(root,'.venv','Scripts','python.exe'),'python']:[path.join(root,'.venv','bin','python'),'python3'];
 const python=candidates.find(candidate=>candidate==='python3'||candidate==='python'||fs.existsSync(candidate));
 const text=execFileSync(python,['-I','-X','utf8',path.join(root,'scripts','ci_source_binding.py'),'--root',root],{cwd:root,encoding:'utf8',timeout:30000,windowsHide:true,maxBuffer:4*1024*1024,stdio:['ignore','pipe','pipe']});
 const result=JSON.parse(text);assertPublicAuthority(result);return result;
}
function collectSourceIdentity(root,{verifyCi=verifyCiSourceBinding}={}){
 assertPublicRoot(root);
 const ci=present(path.join(root,CI_MARKER));
 if(ci||ciIdentityPresent()){
  assertCiEnvironment();assert.ok(ci,'CI identity requires explicit flat-Git source marker');
  const provenance=verifyCi(root);assertPublicAuthority(provenance);
  return {source_commit:provenance.source_commit,github_sha:provenance.source_commit,source_provenance:provenance};
 }
 // Optional unmarked diagnostics make no source claim and cannot pass a required gate.
 return {source_commit:null};
}
function assertSourceIdentity(proof,{root,required=false,verifyCi=verifyCiSourceBinding}={}){
 if(Object.hasOwn(proof,'source_provenance')){
  assert.ok(root,'source proof requires the actual source root');
  assert.equal(proof.source_provenance?.mode,'flat-git-ci','Unsupported source provenance mode');
  assertPublicRoot(root);assertCiEnvironment();
  const current=verifyCi(root);assertPublicAuthority(current);
  assert.equal(proof.source_commit,current.source_commit,'proof matches actual CI checkout');
  assert.equal(proof.github_sha,current.source_commit,'proof matches actual GITHUB_SHA');
  assert.deepEqual(proof.source_provenance,current,'all effective source bytes still match public CI provenance');
 }else if(required||ciIdentityPresent()||proof.github_sha||(root&&[CI_MARKER,...PRIVATE_MARKERS].some(name=>present(path.join(root,name))))){
  throw Error('Source proof requires full public CI provenance');
 }else{
  assert.equal(proof.source_commit??null,null,'Optional unbound diagnostics cannot claim a source commit');
 }
}
module.exports={CI_MARKER,PRIVATE_MARKERS,REPRESENTATION,REPOSITORY,ciIdentityPresent,verifyCiSourceBinding,collectSourceIdentity,assertSourceIdentity};
