import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

function productionShutdownMethods() {
  const source = readFileSync(new URL('../src/embedded-browser.ts', import.meta.url), 'utf8');
  const begin = source.indexOf('  async closeProfile(p: EmbeddedProfile) {');
  const stop = source.indexOf('  async stop() {', begin);
  const end = source.lastIndexOf('\n}');
  assert.ok(begin >= 0 && stop > begin && end > stop);
  // Run the actual methods without Electron. Erase their type-only syntax;
  // session storage and window boundaries are controlled by the fixture below.
  const closeMethod = source.slice(begin, stop)
    .replace('p: EmbeddedProfile', 'p').replaceAll('new Promise<void>', 'new Promise');
  const stopMethod = source.slice(stop, end).replaceAll('new Promise<void>', 'new Promise');
  return new Function(`return { ${closeMethod}, ${stopMethod} };`)();
}

function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return {promise, resolve};
}

test('failed account close cannot end shutdown before another account saves its cookies', async () => {
  const methods = productionShutdownMethods();
  const cookieGate = deferred();
  let flushing = false, cookieSaved = false, storageSaved = false;
  let transportClosed = false, terminated = false;
  const failed = {
    id: 'failed', clients: new Set([{close() { throw new Error('window close failed'); },dispose:async()=>{}}]), pages: new Map(),
    session: {cookies: {flushStore: async () => {}}, flushStorageData() {}},
  };
  const saving = {
    id: 'saving', clients: [], pages: new Map(),
    session: {
      cookies: {flushStore: async () => {flushing = true; await cookieGate.promise; cookieSaved = true;}},
      flushStorageData() {storageSaved = true;},
    },
  };
  const host = {
    ...methods, sessionWrites: new Map(), cancelPendingPage: new Map(), pendingPages: new Map(), opening: new Map(), closing: new Map(), retiringProfiles: new Map(), profiles: new Map([[failed.id, failed], [saving.id, saving]]),
    hide() {},
    sockets: {clients: [{terminate() {terminated = true;}}], close() {}},
    server: {close(done) {transportClosed = true; done();}},
  };
  let settled = false;
  const stopped = host.stop().then(() => {settled = true; return null;}, error => {settled = true; return error;});
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(flushing, true, 'the second account must be saving cookies');
    assert.equal(host.retiringProfiles.get('saving'), 1, 'closure cannot be confirmed while cookie persistence is pending');
    assert.equal(settled, false, 'desktop must not proceed to exit while cookie persistence is pending');
    assert.equal(transportClosed, false);
  } finally {
    cookieGate.resolve();
  }
  const failure = await stopped;
  assert.match(failure?.message || '', /window close failed/);
  assert.equal(cookieSaved, true);
  assert.equal(storageSaved, true);
  assert.equal(transportClosed, true, 'transport cleanup still runs when an account fails');
  assert.equal(terminated, true);
  assert.equal(host.closing.size, 0);
  assert.equal(host.retiringProfiles.size, 0);
});

test('shutdown joins an existing account close without flushing the same owner twice', async () => {
  const methods = productionShutdownMethods();
  const gate = deferred();
  let flushes = 0, closed = false;
  const profile = {
    id: 'owner', clients: [], pages: new Map(),
    session: {cookies: {flushStore: async () => {flushes++; await gate.promise;}}, flushStorageData() {}},
  };
  const host = {
    ...methods, sessionWrites: new Map(), cancelPendingPage: new Map(), pendingPages: new Map(), opening: new Map(), closing: new Map(), retiringProfiles: new Map(), profiles: new Map([[profile.id, profile]]), hide() {},
    sockets: {clients: [], close() {}}, server: {close(done) {closed = true; done();}},
  };
  const existing = host.closeProfile(profile);
  const stopped = host.stop();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(flushes, 1);
  assert.equal(closed, false);
  gate.resolve();
  await Promise.all([existing, stopped]);
  assert.equal(flushes, 1);
  assert.equal(host.profiles.size, 0);
  assert.equal(host.closing.size, 0);
  assert.equal(host.retiringProfiles.size, 0);
  assert.equal(closed, true);
});
