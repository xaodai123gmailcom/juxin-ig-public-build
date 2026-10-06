const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { loadPlaywrightRuntime } = require('./playwright-runtime.cjs');

test('loads the actual Python worker driver without an npm Playwright dependency', () => {
  const runtime = loadPlaywrightRuntime();
  assert.equal(typeof runtime.chromium.connectOverCDP, 'function');
  assert.ok(fs.existsSync(path.join(runtime.directory, 'index.js')));
});

test('real Python subprocess and require preserve Chinese, spaces and punctuation', (t) => {
  const { python } = loadPlaywrightRuntime();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'juxin-runtime-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const unicodeRoot = path.join(root, "深度Instagram 新建文件夹 (3) ' &");
  const packageRoot = path.join(unicodeRoot, 'playwright');
  const directory = path.join(packageRoot, 'driver', 'package');
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(packageRoot, '__init__.py'), '');
  fs.writeFileSync(path.join(directory, 'index.js'),
    'module.exports = { chromium: { connectOverCDP() {}, fixture: __filename } };');
  const env = { ...process.env, JUXIN_PYTHON: python, PYTHONPATH: unicodeRoot,
    PYTHONIOENCODING: 'gbk', JUXIN_PLAYWRIGHT_PATH: 'obsolete-unusable-path' };
  const runtime = loadPlaywrightRuntime({ root: unicodeRoot, env });
  assert.equal(runtime.directory, directory);
  assert.equal(runtime.chromium.fixture, path.join(directory, 'index.js'));

  // A broken Python driver must not silently use a globally installed npm copy.
  fs.rmSync(path.join(directory, 'index.js'));
  delete require.cache[runtime.chromium.fixture];
  assert.throws(() => loadPlaywrightRuntime({ root: unicodeRoot, env }),
    error => error.message.includes(directory) && error.message.includes('Cannot load'));
});

test('missing Python fails with the exact executable and install instructions', () => {
  const missing = path.join(os.tmpdir(), 'juxin-does-not-exist', 'python.exe');
  assert.throws(() => loadPlaywrightRuntime({ env: { ...process.env, JUXIN_PYTHON: missing } }),
    error => error.message.includes(missing) && error.message.includes('backend/requirements.txt'));
});
