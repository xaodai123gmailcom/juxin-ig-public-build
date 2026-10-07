import test from 'node:test';
import assert from 'node:assert/strict';
import { copyFileSync, existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const project = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const script = 'scripts/verify_desktop_build.mjs';
const run = (root, args = ['--source-only']) => spawnSync(process.execPath,
  [join(root, script), ...args], { cwd: tmpdir(), encoding: 'utf8', timeout: 30_000 });
const output = result => result.stdout + result.stderr;
const fullPass = 'NewGen desktop build verification: PASS';

// Copy actual shipped source bytes, not mocked gate helpers or compiled files.
// Unicode, spaces, parentheses and a different cwd exercise the Windows layout.
function fixture() {
  const root = mkdtempSync(join(tmpdir(), '聚鑫源码 (26)-'));
  for (const name of Object.keys(JSON.parse(readFileSync(join(project, 'SOURCE_SHA256.json'), 'utf8')))) {
    if (name.startsWith('renderer/dist/') || name.startsWith('dist-electron/')) continue;
    mkdirSync(dirname(join(root, name)), { recursive: true });
    copyFileSync(join(project, name), join(root, name));
  }
  return { root, close: () => rmSync(root, { recursive: true, force: true }) };
}

function rejectMutation(file, from, to, message, args) {
  const f = fixture();
  try {
    const path = join(f.root, file), before = readFileSync(path, 'utf8');
    assert.ok(before.includes(from), 'mutation must change a real source contract');
    writeFileSync(path, before.replaceAll(from, to));
    const result = run(f.root, args);
    assert.equal(result.status, 1, output(result));
    assert.match(output(result), message);
    assert.ok(!output(result).includes(fullPass), output(result));
  } finally { f.close(); }
}

test('real current source passes without dependencies or compiled output and never reports build success', () => {
  const f = fixture();
  try {
    assert.equal(existsSync(join(f.root, 'node_modules')), false);
    assert.equal(existsSync(join(f.root, 'renderer/dist')), false);
    const result = run(f.root);
    assert.equal(result.status, 0, output(result));
    assert.match(result.stdout, /NewGen desktop source verification: PASS/);
    assert.match(result.stdout, /DESKTOP_ARTIFACTS_CHECK=NOT_RUN/);
    assert.ok(!result.stdout.includes(fullPass));
    assert.equal(existsSync(join(f.root, 'dist-electron')), false);
    assert.equal(existsSync(join(f.root, 'renderer/dist')), false);
  } finally { f.close(); }
});

test('default release mode still rejects missing real artifacts after source checks', () => {
  const f = fixture();
  try {
    const result = run(f.root, []);
    assert.equal(result.status, 1, output(result));
    assert.match(result.stdout, /NewGen desktop source verification: PASS/);
    assert.match(result.stderr, /required file is missing: renderer\/dist\/index.html/);
    assert.ok(!result.stdout.includes(fullPass));
    assert.ok(!result.stdout.includes('DESKTOP_ARTIFACTS_CHECK=NOT_RUN'));
  } finally { f.close(); }
});

test('old progress markup is rejected before compilation rather than forced back into the UI', () => {
  rejectMutation('renderer/src/formal-workbench.tsx',
    'collectionModeProgressText(displayMode, asRecord(progress[mode]))',
    '`${modeLabel}总数 ${sourceTotal ?? "读取中"} · 已采集 ${collected ?? 0}`',
    /formal workbench is missing contract marker: collectionModeProgressText/, []);
});

test('storage safety checks formerly interleaved with bundle checks run in source preflight', () => {
  rejectMutation('desktop/src/storage-management.ts', 'clearCache()', 'clearStorageData()',
    /safe desktop storage management is missing contract marker: clearCache/);
});

test('chat build gate cannot silently omit slow-disk and actual-lock fault controls', () => {
  rejectMutation('scripts/build_windows.ps1',
    '"test_chat_concurrency_r34.py"', '"test_missing_chat.py"',
    /chat concurrency and task lease isolation release gate is missing contract marker/);
});

test('installer gate cannot omit cumulative R94 recovery, ownership and persistence checks', () => {
  rejectMutation('scripts/build_windows.ps1',
    '"test_*r94.py"', '"test_collection_*r94.py"',
    /missing contract marker: "test_\*r94.py"/);
});

test('source preflight rejects an early native gate with no compiler or a source-only substitute', () => {
  rejectMutation('scripts/build_windows.ps1',
    '$NativePreflightBuildExitCode = Invoke-IgacNativeCommandWithLog',
    '$SkippedNativeBuild = Invoke-IgacNativeCommandWithLog',
    /R6\.3 native cleanup compilation prerequisite/);
  rejectMutation('scripts/build_windows.ps1',
    '-ArgumentList @("run", "build:electron")',
    '-ArgumentList @("run", "test:source")',
    /R6\.3 native cleanup compilation prerequisite/);
});

test('source preflight rejects ignored compiler failure and compilation after its native consumer', () => {
  rejectMutation('scripts/build_windows.ps1',
    'if ($NativePreflightBuildExitCode -ne 0) {',
    'if ($false) {',
    /R6\.3 native cleanup compilation prerequisite/);
  const f=fixture();
  try {
    const path=join(f.root,'scripts/build_windows.ps1'),before=readFileSync(path,'utf8');
    const begin=before.indexOf('$NativePreflightBuildExitCode =');
    const end=before.indexOf('# R6.3: verify authoritative absence',begin);
    assert.ok(begin>=0&&end>begin);
    const compiler=before.slice(begin,end),without=before.slice(0,begin)+before.slice(end);
    const cutoff=without.indexOf('# Pure IG deliberately retires');assert.ok(cutoff>0);
    writeFileSync(path,without.slice(0,cutoff)+compiler+without.slice(cutoff));
    const result=run(f.root);
    assert.equal(result.status,1,output(result));assert.match(output(result),/R6\.3 native cleanup compilation prerequisite/);
    assert.ok(!output(result).includes(fullPass));
  } finally {f.close();}
});

test('workbench commands keep storage off the collection loop and drain on cancellation', () => {
  rejectMutation('backend/app/main.py',
    'await asyncio.to_thread(service.clear_workbench_cache, user["id"])',
    'service.clear_workbench_cache(user["id"])',
    /NewGen command branches and bounded snapshot is missing contract marker/);
  rejectMutation('backend/app/main.py',
    'return await finish_owned(execute_owned_command())',
    'return await execute_owned_command()',
    /owned workbench command completion is missing contract marker/);
});

test('backend history protections formerly after compiled imports run in source preflight', () => {
  rejectMutation('backend/app/database.py', 'workbench review decisions are immutable',
    'review decisions mutable', /immutable NewGen history is missing contract marker/);
});

test('last source checks cannot be masked by an earlier success log', () => {
  rejectMutation('scripts/install_windows.ps1', 'verify_runtime_ready.py write',
    'verify_runtime_ready.py read', /verify local pip before dependency installation and runtime readiness/);
});

test('source-only mode is rejected as a replacement for the final release check', () => {
  const metadata = readFileSync(join(project, 'package.json'), 'utf8');
  const build = JSON.parse(metadata).scripts.build;
  rejectMutation('package.json', JSON.stringify(build), JSON.stringify(build + ' --source-only'),
    /release build must end with the full desktop verifier/);
});

test('unknown and repeated arguments fail closed', () => {
  for (const args of [['--skip-artifacts'], ['--source-only', '--source-only']]) {
    const result = run(project, args);
    assert.equal(result.status, 1, output(result));
    assert.match(result.stderr, /usage: node scripts\/verify_desktop_build.mjs/);
    assert.ok(!result.stdout.includes('verification: PASS'));
  }
});


test('direct-discard controls cannot silently restore legacy qualification submissions', () => {
  rejectMutation('renderer/src/formal-workbench.tsx', 'source_limits: {}',
    'source_limits: { followersMax: 4000 }',
    /single visible direct-discard settings and public post activity is missing contract marker: source_limits/);
});

test('public post activity setting is required in the actual controls and Core serializer', () => {
  rejectMutation('renderer/src/formal-workbench.tsx', 'discardLimits.limits.public_discard_active_days_max',
    'discardLimits.limits.public_discard_posts_max',
    /single visible direct-discard settings and public post activity is missing contract marker/);
  rejectMutation('renderer/src/core-client.ts', 'public_discard_active_days_max',
    'missing_public_activity_setting',
    /Core client direct-discard fields is missing contract marker: public_discard_active_days_max/);
});

test('private review cannot hide retained candidates behind a retired second-stage filter', () => {
  rejectMutation('renderer/src/formal-workbench.tsx',
    'const candidates = page?.items ?? [];',
    'const candidates = (page?.items ?? []).filter((item) => !isSecondary(item));',
    /manual review layers with independently paginated retained queues is missing contract marker/);
});

test('Windows builder cannot omit the new discard and private-review regression suite', () => {
  rejectMutation('scripts/build_windows.ps1', '"test_*r38.py"', '"test_nonexistent_r38.py"',
    /direct-discard workflow regression gate is missing contract marker: test_/);
});


test('discard settings keep the enabled switch connected to their inputs', () => {
  rejectMutation('renderer/src/formal-workbench.tsx',
    'disabled={!discardLimits.enabled}',
    'disabled={false}',
    /single visible direct-discard settings and public post activity is missing contract marker/);
});

test('Windows builder cannot omit duplicate prevention before profile navigation', () => {
  rejectMutation('scripts/build_windows.ps1', '"test_*r39.py"', '"test_nonexistent_r39.py"',
    /durable discard and pre-navigation dedupe regression gate is missing contract marker: test_/);
});

test('recovery verification must stay before native browser and desktop builds', () => {
  const f = fixture();
  try {
    const path = join(f.root, 'scripts/build_windows.ps1');
    const before = readFileSync(path, 'utf8');
    const start = before.indexOf('$RecoveryRegressionExitCode =');
    assert.ok(start >= 0);
    const end = before.indexOf('\n}\n', start) + 3;
    assert.ok(end > start);
    const block = before.slice(start, end);
    writeFileSync(path, before.slice(0, start) + before.slice(end) + '\n' + block);
    const result = run(f.root);
    assert.equal(result.status, 1, output(result));
    assert.match(output(result), /r91 early recovery, watchdog and queue gates/);
  } finally { f.close(); }
});

// Controlled files exercise the *actual final verifier path*. These are text
// fixtures, not a Vite/Electron build, and never stand in for native validation.
function artifactTextFixture() {
  const f = fixture();
  const sourceFiles = Object.keys(JSON.parse(readFileSync(join(project, 'SOURCE_SHA256.json'), 'utf8')))
    .filter(name => name.startsWith('desktop/src/') && /\.(?:ts|cts|mts)$/.test(name) && !/\.d\.(?:ts|cts|mts)$/.test(name));
  for (const source of sourceFiles) {
    const name = source.slice('desktop/src/'.length).replace(/\.cts$/, '.cjs').replace(/\.mts$/, '.mjs').replace(/\.ts$/, '.js');
    const target = join(f.root, 'dist-electron', name);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, '// controlled release-gate fixture; never executed\n');
  }
  const bundle = join(f.root, 'renderer/dist/assets/fixture.js');
  mkdirSync(dirname(bundle), { recursive: true });
  writeFileSync(join(f.root, 'renderer/dist/index.html'), '<script type="module" src="./assets/fixture.js"></script>');
  const copy = ['formal-workbench.tsx', 'greeting-messages.ts',
    'storage-management-settings.tsx', 'google-translator-button.tsx']
    .map(name => readFileSync(join(f.root, 'renderer/src', name), 'utf8')).join('\n');
  writeFileSync(bundle, 'export const fixtureText = ' + JSON.stringify(copy) + ';\n');
  return { ...f, bundle };
}

test('stale or partial desktop output cannot pass the final artifact gate', () => {
  for (const mode of ['extra', 'missing']) {
    const f = artifactTextFixture();
    try {
      if (mode === 'extra') writeFileSync(join(f.root, 'dist-electron/stale-r94.js'), 'old deleted source');
      else rmSync(join(f.root, 'dist-electron/account-viewport.js'));
      const result = run(f.root, []);
      assert.equal(result.status, 1, output(result));
      assert.match(output(result), mode === 'extra' ? /unexpected desktop artifact:.*stale-r94/ : /compiled desktop source is missing:.*account-viewport/);
      assert.ok(!output(result).includes(fullPass));
    } finally { f.close(); }
  }
});

test('desktop cleanup removes nested stale output from a foreign working directory', () => {
  const f = fixture();
  try {
    mkdirSync(join(f.root, 'dist-electron/nested'), {recursive: true});
    writeFileSync(join(f.root, 'dist-electron/nested/old.js'), 'stale');
    writeFileSync(join(f.root, 'keep-user-file'), 'keep');
    const result = spawnSync(process.execPath, [join(f.root, 'scripts/clean_desktop_output.mjs')], {cwd: tmpdir(), encoding: 'utf8'});
    assert.equal(result.status, 0, output(result));
    assert.equal(existsSync(join(f.root, 'dist-electron')), false);
    assert.equal(readFileSync(join(f.root, 'keep-user-file'), 'utf8'), 'keep');
  } finally { f.close(); }
});

test('a cleanup call returning without removing files cannot authorize compilation', () => {
  const f = fixture();
  try {
    mkdirSync(join(f.root, 'dist-electron'));
    writeFileSync(join(f.root, 'dist-electron/old.js'), 'stale');
    const hook = "import fs from 'node:fs';import {syncBuiltinESMExports} from 'node:module';fs.rmSync=()=>{};syncBuiltinESMExports();";
    const result = spawnSync(process.execPath, ['--import', 'data:text/javascript,' + encodeURIComponent(hook),
      join(f.root, 'scripts/clean_desktop_output.mjs')], {cwd: tmpdir(), encoding: 'utf8'});
    assert.equal(result.status, 1, output(result));
    assert.match(output(result), /refusing to compile over stale files/);
    assert.equal(readFileSync(join(f.root, 'dist-electron/old.js'), 'utf8'), 'stale');
  } finally { f.close(); }
});

test('final artifact gate accepts current discard copy and still requires a stylesheet', () => {
  const f = artifactTextFixture();
  try {
    assert.ok(!readFileSync(f.bundle, 'utf8').includes('0=不限'));
    const result = run(f.root, []);
    assert.equal(result.status, 1, output(result));
    assert.match(result.stderr, /DESKTOP_ARTIFACTS_CHECK=FAILED:.*renderer production stylesheet is missing/);
    assert.ok(!output(result).includes('missing contract marker'));
    assert.ok(!output(result).includes(fullPass));
  } finally { f.close(); }
});

test('fresh source cannot conceal a stale compiled public activity control', () => {
  const f = artifactTextFixture();
  try {
    const sourceOnly = run(f.root);
    assert.equal(sourceOnly.status, 0, output(sourceOnly));
    writeFileSync(f.bundle, readFileSync(f.bundle, 'utf8').replaceAll('公开帖子活跃度上限（天）', 'removed field'));
    const result = run(f.root, []);
    assert.equal(result.status, 1, output(result));
    assert.match(result.stderr, /DESKTOP_ARTIFACTS_CHECK=FAILED:.*production capacity and safe cleanup copy is missing contract marker: 公开帖子活跃度上限（天）/);
    assert.ok(!output(result).includes(fullPass));
  } finally { f.close(); }
});

test('new artifact copy requirements must be present during source preflight too', () => {
  rejectMutation('scripts/renderer_release_contract.mjs', '"立即重试",',
    '"立即重试", "future-artifact-label-not-yet-in-renderer",',
    /renderer source release copy is missing contract marker: future-artifact-label-not-yet-in-renderer/);
});


test('Windows timezone dependency and early package fallback cannot disappear', () => {
  rejectMutation('backend/requirements.txt', 'tzdata==2026.4', '# timezone omitted',
    /r57 Windows timezone dependency/);
  rejectMutation('scripts/install_windows.ps1', 'scripts\\verify_timezone_data.py', 'scripts\\unused_timezone_probe.py',
    /r57 early build timezone preflight/);
});


test('dependency recovery and pinned Pillow remain mandatory', () => {
  rejectMutation('scripts/install_windows.ps1', 'scripts\\install_python_dependencies.py', 'scripts\\old_dependency_installer.py', /r83 dependency recovery install path/);
  rejectMutation('backend/requirements.txt', 'Pillow==12.3.0', 'Pillow>=10', /r83 retained Pillow release pin/);
  rejectMutation('scripts/install_python_dependencies.py', 'PILLOW_CODEC_CHECK=PASS', 'metadata-only', /r83 bounded official source fallback/);
});

// Catch stale native expectations before a slow full desktop/Windows build.
test('r93 source preflight rejects old task labels and outdated native close controls', () => {
  rejectMutation('desktop/tests/task-watch.integration.cjs',
    "['采集页','1-1','1-2','1-3'].slice(0,workers+1)",
    "['采集页','筛选页 1','筛选页 2','筛选页 3'].slice(0,workers+1)",
    /r93 fixed-slot task-watch fixture/);
  rejectMutation('desktop/tests/embedded-browser.integration.cjs',
    '关闭1-2', '关闭筛选页 2', /r93 fixed-slot native UI fixture/);
});


test('pure IG contract rejects changing the fixed platform or source queue', () => {
  rejectMutation('renderer/src/workbench-platform.tsx',
    'const INSTAGRAM = { platform: "instagram" as const }', 'const INSTAGRAM = { platform: "removed" as const }',
    /pure IG fixed platform client is missing contract marker/);
  rejectMutation('renderer/src/formal-workbench.tsx',
    'targets: []', 'targets: draftSeeds.targets',
    /pure IG collection contract is missing contract marker/);
});

test('installed recovery retains its finite whole-runtime and outer cleanup budgets', () => {
  rejectMutation('ci/public_ci_verify_installed.ps1',
    "'--timeout', '420'", "'--timeout', '120'",
    /actual installed source-independent Core, migration, scale and recovery gates/);
  rejectMutation('ci/public_ci_verify_installed.ps1',
    '-LogPath $recoveryStdout -TimeoutSeconds 540', '-LogPath $recoveryStdout -TimeoutSeconds 180',
    /actual installed source-independent Core, migration, scale and recovery gates/);
});
