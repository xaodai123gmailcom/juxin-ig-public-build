/* Both distributions must carry the exact dependency seal from the real gate. */
const fs = require('node:fs');
const path = require('node:path');

function verifyPolicy(project, resources) {
  const binding=require('./local_source_binding.cjs');
  if(binding.ciIdentityPresent()||['LOCAL_SOURCE_PROVENANCE.json','CI_SOURCE_PROVENANCE.json'].some(name=>{try{fs.lstatSync(path.join(project,name));return true}catch(error){if(error.code==='ENOENT')return false;throw error}}))binding.collectSourceIdentity(project);
  const relative = 'browsers/juxin-runtime-requirement.json';
  const source = fs.readFileSync(path.join(project, 'build', relative));
  const packaged = fs.readFileSync(path.join(resources, relative));
  if (!source.equals(packaged)) throw new Error('Packaged browser requirement differs from the verified build');
  const policy = JSON.parse(packaged.toString('utf8'));
  if (policy.format !== 1 || !['bundled-available', 'installed-chrome-required'].includes(policy.mode)) {
    throw new Error('Invalid packaged browser requirement');
  }
  if (policy.mode === 'installed-chrome-required') {
    if (!/^\d{1,10}(?:\.\d{1,10}){3}$/.test(policy.minimum_version || '')) throw new Error('Missing required Chrome version');
  } else {
    const browsers = path.join(resources, 'browsers');
    if (!fs.readdirSync(browsers).some(name => /^chromium-\d+$/.test(name) &&
      ['chrome-win64', 'chrome-win'].some(folder => fs.existsSync(path.join(browsers, name, folder, 'chrome.exe'))))) {
      throw new Error('Bundled edition is missing its browser executable');
    }
  }
  return policy;
}

async function afterPack(context) {
  verifyPolicy(context.packager.projectDir, path.join(context.appOutDir, 'resources'));
}
module.exports = afterPack;
module.exports.verifyPolicy = verifyPolicy;
if (require.main === module) {
  if (process.argv.length !== 4) throw new Error('Expected project and packaged resources paths');
  console.log('PACKAGED_BROWSER_POLICY=PASS ' + JSON.stringify(verifyPolicy(process.argv[2], process.argv[3])));
}
