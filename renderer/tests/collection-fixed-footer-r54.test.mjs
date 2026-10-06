import test from 'node:test';
import assert from 'node:assert/strict';
import { bindCollectionFixedFooter } from '../src/collection-fixed-footer-layout.ts';

function fixture() {
  const events = new Map(), viewportEvents = new Map(), frames = new Map();
  const rect = { left: 116, width: 1427, height: 0 }, dimensions = { height: 134, bottom: 10 };
  let frameId = 0;
  const view = {
    getComputedStyle: () => ({ bottom: `${dimensions.bottom}px` }),
    requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame(id) { frames.delete(id); },
    addEventListener(name, fn) { events.set(name, fn); },
    removeEventListener(name, fn) { if (events.get(name) === fn) events.delete(name); },
    visualViewport: {
      addEventListener(name, fn) { viewportEvents.set(name, fn); },
      removeEventListener(name, fn) { if (viewportEvents.get(name) === fn) viewportEvents.delete(name); },
    },
  };
  const style = () => ({ removeProperty(name) { delete this[name]; } });
  const anchor = { style: style(), isConnected: true, parentElement: {},
    ownerDocument: { defaultView: view }, getBoundingClientRect: () => rect };
  const bar = { style: style(), dataset: {}, isConnected: true,
    getBoundingClientRect: () => ({ height: dimensions.height }) };
  const observations = new Set();
  let notify, disconnected = false;
  const previous = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(callback) { notify = callback; }
    observe(element) { observations.add(element); }
    disconnect() { observations.clear(); disconnected = true; }
  };
  const flush = () => { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn()); };
  const restore = () => {
    if (previous === undefined) delete globalThis.ResizeObserver;
    else globalThis.ResizeObserver = previous;
  };
  return { anchor, bar, view, rect, dimensions, events, viewportEvents, frames, observations,
    flush, restore, notify: () => notify(), disconnected: () => disconnected };
}

test('fixed controls follow the content edges and reserve real toolbar height plus the bottom gap', () => {
  const f = fixture();
  try {
    const cleanup = bindCollectionFixedFooter(f.anchor, f.bar);
    assert.equal(f.bar.dataset.fixed, 'true');
    assert.equal(f.bar.style.left, '116px');
    assert.equal(f.bar.style.width, '1427px');
    assert.equal(f.anchor.style.height, '144px');
    assert.ok(f.observations.has(f.anchor.parentElement));
    cleanup();
  } finally { f.restore(); }
});

test('sidebar resizing, wrapped hints and mobile navigation update the same mounted toolbar', () => {
  const f = fixture();
  try {
    const cleanup = bindCollectionFixedFooter(f.anchor, f.bar);
    f.rect.left = 14;
    f.rect.width = 362;
    f.dimensions.height = 305.3;
    f.dimensions.bottom = 84;
    f.notify();
    f.events.get('resize')();
    f.viewportEvents.get('resize')();
    assert.equal(f.frames.size, 1, 'simultaneous layout events are coalesced');
    f.flush();
    assert.equal(f.bar.style.left, '14px');
    assert.equal(f.bar.style.width, '362px');
    assert.equal(f.anchor.style.height, '390px');
    cleanup();
  } finally { f.restore(); }
});

test('vertical scrolling keeps placement and horizontal scrolling realigns without creating duplicate controls', () => {
  const f = fixture();
  try {
    const cleanup = bindCollectionFixedFooter(f.anchor, f.bar);
    f.events.get('scroll')(); f.flush();
    assert.equal(f.bar.style.left, '116px');
    f.rect.left = 76;
    f.events.get('scroll')(); f.flush();
    assert.equal(f.bar.style.left, '76px');
    assert.equal(f.anchor.style.height, '144px');
    cleanup();
  } finally { f.restore(); }
});

test('route unmount disconnects observers, removes listeners and cancels a pending frame', () => {
  const f = fixture();
  try {
    const cleanup = bindCollectionFixedFooter(f.anchor, f.bar);
    f.notify();
    assert.equal(f.frames.size, 1);
    cleanup();
    assert.equal(f.disconnected(), true);
    assert.equal(f.events.size, 0);
    assert.equal(f.viewportEvents.size, 0);
    assert.equal(f.frames.size, 0);
    assert.equal(f.bar.dataset.fixed, undefined);
    assert.equal(f.anchor.style.height, undefined);
    f.notify();
    assert.equal(f.frames.size, 0, 'late observer callbacks cannot reattach a footer');
  } finally { f.restore(); }
});

test('disconnected controls are never remeasured by queued layout work', () => {
  const f = fixture();
  try {
    const cleanup = bindCollectionFixedFooter(f.anchor, f.bar);
    f.notify();
    f.anchor.isConnected = false;
    f.rect.width = 5;
    f.flush();
    assert.equal(f.bar.style.width, '1427px');
    cleanup();
  } finally { f.restore(); }
});
