const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {performance} = require('node:perf_hooks');
const {closeFixtureContents} = require('./task-watch-fixture.cjs');

function contents(close) {
  const wc = new EventEmitter();
  wc.isDestroyed = () => false;
  wc.close = () => close(wc);
  return wc;
}
const options = {label: 'fixture screen', timeoutMs: 50};

test('close subscribes before synchronous destruction and removes its listener', async () => {
  const wc = contents(wc => {assert.equal(wc.listenerCount('destroyed'), 1); wc.emit('destroyed')});
  await closeFixtureContents(wc, options);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

test('asynchronous close is pending until the destruction event arrives', async () => {
  let complete = false;
  const wc = contents(() => {});
  const closing = closeFixtureContents(wc, options).then(() => {complete = true});
  await Promise.resolve();
  assert.equal(complete, false);
  wc.emit('destroyed');
  await closing;
  assert.equal(complete, true);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

test('missing contents is an error, not successful page closure', async () => {
  await assert.rejects(closeFixtureContents(undefined, options), /missing before close/);
});

test('an unobserved destruction times out and removes the listener', async () => {
  const wc = contents(() => {});
  await assert.rejects(closeFixtureContents(wc, options), /destruction not observed/);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

for(const afterEvent of [false, true])test(`close errors remain failures (event emitted: ${afterEvent})`, async () => {
  const failure = new Error('native close failed');
  const wc = contents(wc => {if(afterEvent)wc.emit('destroyed'); throw failure});
  await assert.rejects(closeFixtureContents(wc, options), error => error === failure);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

test('an already destroyed captured object does not receive a second close', async () => {
  const wc = contents(() => assert.fail('already destroyed contents must not be closed again'));
  wc.isDestroyed = () => true;
  await closeFixtureContents(wc, options);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

for(const emittedInsideClose of [true, false])test(`a destruction observed after a blocked deadline remains a failure (inside close: ${emittedInsideClose})`, async () => {
  const block = () => {const until = performance.now() + 25; while(performance.now() < until){}};
  const wc = contents(wc => {if(emittedInsideClose){block(); wc.emit('destroyed')}});
  const closing = closeFixtureContents(wc, {...options, timeoutMs: 10});
  if(!emittedInsideClose){block(); wc.emit('destroyed')}
  await assert.rejects(closing, /destruction not observed/);
  assert.equal(wc.listenerCount('destroyed'), 0);
});
