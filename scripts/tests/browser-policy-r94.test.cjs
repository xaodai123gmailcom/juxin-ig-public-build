const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const afterPack = require('../verify_packaged_browser_policy.cjs');

function fixture(t, mode = 'installed-chrome-required') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'Chrome 依赖 (6) '));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const resources = path.join(root, 'app/resources');
  const source = path.join(root, 'build/browsers/juxin-runtime-requirement.json');
  const target = path.join(resources, 'browsers/juxin-runtime-requirement.json');
  const record = {format: 1, mode, minimum_version: '154.0.8037.58'};
  for (const file of [source, target]) {
    fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, JSON.stringify(record));
  }
  return {root, resources, source, target, record};
}
test('installed Chrome seal survives the actual afterPack hook', async t => {
  const f = fixture(t);
  await afterPack({packager: {projectDir: f.root}, appOutDir: path.join(f.root, 'app')});
  assert.deepEqual(afterPack.verifyPolicy(f.root, f.resources), f.record);
});
test('missing packaged dependency cannot be distributed', t => {
  const f = fixture(t); fs.unlinkSync(f.target);
  assert.throws(() => afterPack.verifyPolicy(f.root, f.resources));
});
test('stale or substituted dependency cannot be distributed', t => {
  const f = fixture(t); fs.writeFileSync(f.target, JSON.stringify({...f.record, mode: 'bundled-available'}));
  assert.throws(() => afterPack.verifyPolicy(f.root, f.resources), /differs/);
});
test('bundled mode still requires a packaged browser', t => {
  const f = fixture(t, 'bundled-available');
  assert.throws(() => afterPack.verifyPolicy(f.root, f.resources), /missing its browser/);
  const exe = path.join(f.resources, 'browsers/chromium-1234/chrome-win64/chrome.exe');
  fs.mkdirSync(path.dirname(exe), {recursive: true}); fs.writeFileSync(exe, 'fixture');
  assert.equal(afterPack.verifyPolicy(f.root, f.resources).mode, 'bundled-available');
});
test('unknown mode or missing version is rejected', t => {
  const f = fixture(t);
  for (const record of [{format: 1, mode: 'unknown'}, {format: 1, mode: 'installed-chrome-required'}]) {
    for (const file of [f.source, f.target]) fs.writeFileSync(file, JSON.stringify(record));
    assert.throws(() => afterPack.verifyPolicy(f.root, f.resources));
  }
});
test('explicit entry and both release paths enforce the browser contract', () => {
  const root = path.resolve(__dirname, '../..');
  const read = p => fs.readFileSync(path.join(root, p), 'utf8');
  assert.match(read('BUILD_WITH_INSTALLED_CHROME.bat'), /call "%~dp0build_installer_windows.bat" --installed-chrome/);
  const builder = read('scripts/build_windows.ps1');
  assert.match(builder, /\$BrowserMode = "bundled"/);
  assert.match(builder, /--require-installed-chrome/);
  assert.match(builder, /\$env:IGAC_TEST_CHROMIUM_EXECUTABLE = \[string\]\$ChromeCandidate.executable/);
  assert.match(builder, /BROWSER_MODE=\$BrowserMode/);
  assert.equal(JSON.parse(read('package.json')).build.afterPack, 'scripts/verify_packaged_browser_policy.cjs');
  assert.match(read('scripts/build_portable_windows.ps1'), /verify_packaged_browser_policy.cjs/);
  assert.match(read('desktop/src/main.ts'), /IGAC_RUNTIME_REQUIREMENT: join\(process.resourcesPath,"browsers","juxin-runtime-requirement.json"\)/);
});

// These cases manufacture synthetic receipts/trees. Isolate only this unit-test
// process, and restore the real CI environment after every case. Native build
// commands never use this seam and must retain their actual GitHub identity.
let savedCiUnitEnvironment;
test.beforeEach(()=>{savedCiUnitEnvironment={GITHUB_SHA:process.env.GITHUB_SHA,GITHUB_ACTIONS:process.env.GITHUB_ACTIONS};delete process.env.GITHUB_SHA;delete process.env.GITHUB_ACTIONS});
test.afterEach(()=>{for(const [key,value] of Object.entries(savedCiUnitEnvironment)){if(value===undefined)delete process.env[key];else process.env[key]=value}});
