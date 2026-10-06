import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {verifyBuildSource, sourceRevision, requiredFiles} from '../verify_build_source.mjs';

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'juxin-source-中文 (4)-'));
  const put = (name, data) => {mkdirSync(dirname(join(root, name)), {recursive: true});writeFileSync(join(root, name), data)};
  for (const name of requiredFiles) put(name, 'fixture source: ' + name);
  put('BUILD_REVISION.txt', 'Product version: 3.0.4\nSource revision: ' + sourceRevision + '\n');
  put('package.json', JSON.stringify({name: 'juxin-ig-audience-collector-newgen', version: '3.0.4'}));
  put('scripts/verify_build_source.mjs', readFileSync(new URL('../verify_build_source.mjs', import.meta.url)));
  const seal = () => {
    const hashes = Object.fromEntries(requiredFiles.map(name => [name, createHash('sha256').update(readFileSync(join(root, name))).digest('hex')]));
    put('SOURCE_SHA256.json', JSON.stringify(hashes));return hashes;
  };
  seal();
  return {root, put, seal, close: () => rmSync(root, {recursive: true, force: true})};
}

test('complete source in a Unicode path verifies without changing any source bytes', () => {
  const f = fixture();
  try {
    const before = requiredFiles.map(name => readFileSync(join(f.root, name)));
    const result = verifyBuildSource(f.root);
    assert.equal(result.verified, true);assert.equal(result.revision, sourceRevision);assert.equal(result.checkedFiles, requiredFiles.length);
    assert.deepEqual(requiredFiles.map(name => readFileSync(join(f.root, name))), before);
  } finally {f.close()}
});
test('an older revision and a replaced recovery script are rejected', () => {
  const f = fixture();
  try {
    f.put('desktop/tests/review-recovery-fixture.cjs', 'old title-only readiness');
    assert.throws(() => verifyBuildSource(f.root), /review-recovery-fixture.cjs: checksum mismatch/);
    f.put('BUILD_REVISION.txt', 'Source revision: review-recovery-fixture-r2\n');f.seal();
    assert.throws(() => verifyBuildSource(f.root), /Source revision mismatch/);
  } finally {f.close()}
});
test('optional proprietary translator payload is rejected even when it is outside the source manifest', () => {
  const f = fixture();
  try {
    f.put('desktop/vendor/immersive-translate/immersive-translate.user.js', 'synthetic unbundled sentinel');
    assert.throws(() => verifyBuildSource(f.root), /Optional proprietary translator payload must not ship/);
  } finally {f.close()}
});
test('missing files, omitted required entries and paths outside the package fail', () => {
  const f = fixture();
  try {
    rmSync(join(f.root, 'desktop/tests/floating-pages.integration.cjs'));
    assert.throws(() => verifyBuildSource(f.root), /floating-pages.integration.cjs: missing/);
    f.put('desktop/tests/floating-pages.integration.cjs', 'restored fixture');
    let hashes = f.seal();delete hashes['scripts/build_windows.ps1'];f.put('SOURCE_SHA256.json', JSON.stringify(hashes));
    assert.throws(() => verifyBuildSource(f.root), /Required source file missing/);
    hashes = f.seal();hashes['../outside'] = '0'.repeat(64);f.put('SOURCE_SHA256.json', JSON.stringify(hashes));
    assert.throws(() => verifyBuildSource(f.root), /Invalid source manifest entry/);
  } finally {f.close()}
});
test('CLI records the exact revision and returns nonzero for a mixed extraction without logging file contents', () => {
  const f = fixture();
  try {
    const run = () => spawnSync(process.execPath, [join(f.root, 'scripts/verify_build_source.mjs')], {encoding: 'utf8'});
    let result = run();assert.equal(result.status, 0, result.stderr);assert.ok(result.stdout.split('\n').includes('BUILD_SOURCE_REVISION=' + sourceRevision));
    assert.equal(JSON.parse(readFileSync(join(f.root, 'installer-output/build-source.json'))).verified, true);
    f.put('scripts/build_windows.ps1', 'PRIVATE_FIXTURE_CONTENT_DO_NOT_LOG');
    result = run();assert.equal(result.status, 1);assert.match(result.stderr, /scripts\/build_windows.ps1: checksum mismatch/);
    const report = readFileSync(join(f.root, 'installer-output/build-source.json'), 'utf8');
    assert.equal(JSON.parse(report).verified, false);assert.ok(!(report + result.stdout + result.stderr).includes('PRIVATE_FIXTURE_CONTENT_DO_NOT_LOG'));
  } finally {f.close()}
});

// These cases manufacture synthetic receipts/trees. Isolate only this unit-test
// process, and restore the real CI environment after every case. Native build
// commands never use this seam and must retain their actual GitHub identity.
let savedCiUnitEnvironment;
test.beforeEach(()=>{savedCiUnitEnvironment={GITHUB_SHA:process.env.GITHUB_SHA,GITHUB_ACTIONS:process.env.GITHUB_ACTIONS};delete process.env.GITHUB_SHA;delete process.env.GITHUB_ACTIONS});
test.afterEach(()=>{for(const [key,value] of Object.entries(savedCiUnitEnvironment)){if(value===undefined)delete process.env[key];else process.env[key]=value}});
