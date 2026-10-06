import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { stagePortableResources } from '../stage_portable_resources.mjs';

function fixture(t) {
  const temporary = mkdtempSync(join(tmpdir(), '聚鑫 独立便携 (94)-'));
  t.after(() => rmSync(temporary, { recursive: true, force: true }));
  const root = join(temporary, '源码'), destination = join(temporary, '便携包/resources/app');
  const put = (name, value) => { const path = join(root, name); mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, value); };
  const pkg = (path, value, code = '') => {
    put(join(path, 'package.json'), JSON.stringify(value));
    if (code) put(join(path, 'index.js'), code);
  };
  pkg('', { name: 'fixture', dependencies: { ws: '1', '@scope/parent': '1' }, devDependencies: { 'dev-only': '1' } });
  pkg('node_modules/ws', { name: 'ws', main: 'index.js', peerDependencies: { 'optional-native': '1' }, peerDependenciesMeta: { 'optional-native': { optional: true } } },
    'exports.WebSocket = class {}; exports.WebSocketServer = class {};');
  pkg('node_modules/@scope/parent', { name: '@scope/parent', main: 'index.js', dependencies: { nested: '1', shared: '1' } },
    'exports.value = require("nested").value + require("shared").value;');
  pkg('node_modules/@scope/parent/node_modules/nested', { name: 'nested', main: 'index.js', dependencies: { shared: '1' } }, 'exports.value = "nested+";');
  pkg('node_modules/shared', { name: 'shared', main: 'index.js' }, 'exports.value = "hoisted";');
  pkg('node_modules/dev-only', { name: 'dev-only' });
  for (const asset of ['desktop/assets/war-wolf.ico', 'desktop/vendor/immersive-translate/host.html',
    'desktop/vendor/immersive-translate/NOTICE.txt']) put(asset, '原始资源 ' + asset);
  return { root, destination, put, pkg };
}

test('portable app resolves its own runtime graph and assets after the source directory is removed', t => {
  const f = fixture(t), result = stagePortableResources(f.root, f.destination);
  assert.equal(result.packages.length, 4);
  assert.equal(existsSync(join(f.destination, 'node_modules/dev-only')), false);
  assert.match(readFileSync(join(f.destination, 'desktop/assets/war-wolf.ico'), 'utf8'), /原始资源/);
  assert.match(readFileSync(join(f.destination, 'desktop/vendor/immersive-translate/host.html'), 'utf8'), /原始资源/);
  assert.match(readFileSync(join(f.destination, 'desktop/vendor/immersive-translate/NOTICE.txt'), 'utf8'), /原始资源/);
  assert.equal(existsSync(join(f.destination, 'desktop/vendor/immersive-translate/immersive-translate.user.js')), false);
  rmSync(f.root, { recursive: true });
  writeFileSync(join(f.destination, 'probe.cjs'), 'if (require("@scope/parent").value !== "nested+hoisted" || typeof require("ws").WebSocket !== "function") process.exit(1);');
  const run = spawnSync(process.execPath, [join(f.destination, 'probe.cjs')], { cwd: tmpdir(), encoding: 'utf8', timeout: 10_000 });
  assert.equal(run.status, 0, run.stdout + run.stderr);
});

test('missing transitive runtime dependency blocks packaging', t => {
  const f = fixture(t); rmSync(join(f.root, 'node_modules/shared'), { recursive: true });
  assert.throws(() => stagePortableResources(f.root, f.destination), /Missing portable dependency: shared/);
});

test('optional peer metadata cannot hide a missing required runtime dependency', t => {
  const f = fixture(t);
  f.pkg('node_modules/ws', { name: 'ws', dependencies: { required: '1' },
    peerDependencies: { required: '1' }, peerDependenciesMeta: { required: { optional: true } } });
  assert.throws(() => stagePortableResources(f.root, f.destination), /Missing portable dependency: required/);
});

test('missing first-party translator host, notice or icon blocks packaging', t => {
  for (const asset of ['desktop/assets/war-wolf.ico', 'desktop/vendor/immersive-translate/host.html',
    'desktop/vendor/immersive-translate/NOTICE.txt']) {
    const f = fixture(t); rmSync(join(f.root, asset));
    assert.throws(() => stagePortableResources(f.root, f.destination), /Missing portable asset/);
  }
});

test('locally present optional payload and other vendor-folder files are never packaged', t => {
  const f = fixture(t);
  for (const name of ['immersive-translate.user.js', 'local-copy.js', 'private/settings.json']) {
    f.put('desktop/vendor/immersive-translate/' + name, 'synthetic forbidden packaging sentinel');
  }
  const result = stagePortableResources(f.root, f.destination);
  assert.deepEqual(result.assets, ['desktop/assets', 'desktop/vendor/immersive-translate/host.html',
    'desktop/vendor/immersive-translate/NOTICE.txt']);
  for (const name of ['immersive-translate.user.js', 'local-copy.js', 'private/settings.json']) {
    assert.equal(existsSync(join(f.destination, 'desktop/vendor/immersive-translate', name)), false);
    assert.equal(existsSync(join(f.root, 'desktop/vendor/immersive-translate', name)), true);
  }
});

test('stale destination translator payload blocks packaging before any resources are copied', t => {
  const f = fixture(t), payload = join(f.destination, 'desktop/vendor/immersive-translate/immersive-translate.user.js');
  mkdirSync(dirname(payload), { recursive: true });writeFileSync(payload, 'synthetic stale payload');
  assert.throws(() => stagePortableResources(f.root, f.destination), /translator resources; use a fresh build directory/);
  assert.equal(existsSync(join(f.destination, 'node_modules')), false);
  assert.equal(existsSync(join(f.destination, 'desktop/assets')), false);
});

test('unrelated source dependencies cannot mask an absent declared ws runtime', t => {
  const f = fixture(t); f.pkg('', { name: 'fixture', dependencies: { '@scope/parent': '1' } });
  assert.throws(() => stagePortableResources(f.root, f.destination), /Cannot find module 'ws'|outside the packaged application/);
});

test('dependency path escapes are rejected before copying', t => {
  const f = fixture(t); f.pkg('', { dependencies: { '../outside': '1' } });
  assert.throws(() => stagePortableResources(f.root, f.destination), /Invalid portable dependency/);
});

test('existing destination dependencies cannot mask a failed fresh package', t => {
  const f = fixture(t); mkdirSync(join(f.destination, 'node_modules'), { recursive: true });
  assert.throws(() => stagePortableResources(f.root, f.destination), /fresh build directory/);
});
