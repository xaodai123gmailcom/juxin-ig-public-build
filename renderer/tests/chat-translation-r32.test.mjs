import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/chat-translation.tsx', import.meta.url), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const defaults = { engine: 'immersive', immersiveMode: 'manual', immersiveService: 'plugin', immersiveFallbacks: [], enabled: true, outgoing: false, incomingLang: 'en', outgoingLang: 'zh-CN', color: '#93c5fd', fontSize: 12, provider: 'google', region: '' };
const response = (settings = defaults, error = '') => ({ settings: { ...settings }, error });

function mountSettings(api, props = {}) {
  const start = source.indexOf('export function ChatTranslationSettings(');
  const end = source.indexOf(' return <section className="chat-translation-settings"', start);
  assert.ok(start >= 0 && end > start);
  // Execute real component hooks/handlers with dependency-aware rerenders.
  // JSX is excluded: this verifies request lifecycle, not browser rendering.
  const code = source.slice(start, end).replace('export function', 'function') + '\nreturn {run,save,editDraft:setDraft,disabled};\n}';
  const names = [...code.matchAll(/\[(\w+),\s*set\w+\]\s*=\s*useState/g)].map(match => match[1]);
  const states = [], refs = [], effects = [], timers = new Map(), busyChanges = [], saved = [], writes = [];
  const values = { id: 'account-a', locked: false, busy: false, onBusyChange: value => busyChanges.push(value), onSaved: value => saved.push(value), ...props };
  let stateIndex = 0, refIndex = 0, effectIndex = 0, timerId = 0;
  const sameDeps = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const render = runInNewContext(stripTypeScriptTypes(code) + '\n() => ChatTranslationSettings(props)', {
    props: values, window: { collectorCore: { chatTranslation: api } },
    useState(initial) { const slot = stateIndex++; if (!(slot in states)) states[slot] = typeof initial === 'function' ? initial() : initial; return [states[slot], next => { states[slot] = typeof next === 'function' ? next(states[slot]) : next; writes.push({ name: names[slot], value: states[slot] }); }]; },
    useRef(initial) { const slot = refIndex++; return refs[slot] ||= { current: initial }; },
    useEffect(effect, deps) { const slot = effectIndex++; if (!sameDeps(effects[slot]?.deps, deps)) { effects[slot]?.cleanup?.(); effects[slot] = { effect, deps, pending: true }; } },
    setInterval(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearInterval(id) { timers.delete(id); },
  });
  const view = { writes, saved, busyChanges,
    state: name => states[names.indexOf(name)],
    visibleError: () => view.state('error') || view.state('readError') || '',
    rerender(next = {}) { Object.assign(values, next); stateIndex = refIndex = effectIndex = 0; Object.assign(view, render()); for (const effect of effects) if (effect.pending) { effect.pending = false; effect.cleanup = effect.effect(); } return view; },
    tick() { for (const callback of timers.values()) callback(); },
    dispose() { for (const effect of effects) effect.cleanup?.(); },
  };
  return view.rerender();
}

test('a successful retry initializes settings after the first read failed', async () => {
  let reads = 0;
  const view = mountSettings(async () => { if (++reads === 1) throw new Error('temporary read failure'); return response(); });
  try {
    await flush();
    assert.equal(view.state('draft'), null);
    assert.match(view.visibleError(), /temporary read failure/);
    view.tick(); await flush();
    assert.equal(reads, 2);
    assert.equal(view.state('draft')?.incomingLang, 'en');
    assert.equal(view.visibleError(), '');
  } finally { view.dispose(); }
});

test('read recovery preserves the current unsaved draft after initialization', async () => {
  let reads = 0;
  const view = mountSettings(async () => { if (++reads === 2) throw new Error('offline'); return response(); });
  try {
    await flush(); view.rerender();
    view.editDraft({ ...view.state('draft'), incomingLang: 'de', fontSize: 28 });
    view.tick(); await flush(); assert.match(view.visibleError(), /offline/);
    view.tick(); await flush();
    assert.equal(view.state('draft').incomingLang, 'de');
    assert.equal(view.state('draft').fontSize, 28);
    assert.equal(view.visibleError(), '');
  } finally { view.dispose(); }
});

test('a pre-save poll cannot hide save rejection and later healthy polling preserves the command error', async () => {
  const old = deferred(); let reads = 0;
  const view = mountSettings(async input => { if (input.settings) throw new Error('save blocked by task lock'); return ++reads === 2 ? old.promise : response(); });
  try {
    await flush(); view.rerender(); view.tick(); await flush();
    await view.run(view.save);
    assert.match(view.visibleError(), /save blocked by task lock/);
    old.resolve(response()); await flush();
    assert.match(view.visibleError(), /save blocked by task lock/);
    view.tick(); await flush();
    assert.equal(reads, 3);
    assert.match(view.visibleError(), /save blocked by task lock/);
    assert.deepEqual(view.busyChanges, [true, false]);
  } finally { view.dispose(); }
});

test('a pre-save poll error cannot overwrite a successful save or its returned draft', async () => {
  const old = deferred(); let reads = 0;
  const view = mountSettings(async input => input.settings ? response({ ...input.settings, fontSize: 24 }) : ++reads === 2 ? old.promise : response());
  try {
    await flush(); view.rerender(); view.tick(); await flush();
    await view.run(view.save);
    old.reject(new Error('stale transport error')); await flush();
    assert.equal(view.visibleError(), '');
    assert.equal(view.state('draft').fontSize, 24);
    assert.equal(view.saved.length, 1);
    assert.match(view.state('message'), /已保存/);
    view.tick(); await flush();
    assert.equal(view.state('draft').fontSize, 24);
  } finally { view.dispose(); }
});

test('an explicit successful save retry clears the old command error', async () => {
  let saves = 0;
  const view = mountSettings(async input => { if (input.settings && ++saves === 1) throw new Error('temporary save failure'); return response(input.settings || defaults); });
  try {
    await flush(); view.rerender(); await view.run(view.save);
    assert.match(view.visibleError(), /temporary save failure/);
    view.rerender(); await view.run(view.save);
    assert.equal(saves, 2);
    assert.equal(view.visibleError(), '');
    assert.equal(view.saved.length, 1);
  } finally { view.dispose(); }
});

test('polling remains serialized and stops committing after the editor is closed', async () => {
  const read = deferred(); let reads = 0;
  const view = mountSettings(() => { reads++; return read.promise; });
  await flush(); for (let tick = 0; tick < 10; tick++) view.tick();
  assert.equal(reads, 1);
  view.dispose(); const count = view.writes.length;
  read.resolve(response()); await flush(); view.tick(); await flush();
  assert.equal(reads, 1);
  assert.equal(view.writes.length, count);
});

test('current lock and parent busy still block saves without issuing mutations', async () => {
  const mutations = [];
  const view = mountSettings(async input => { if (input.settings) mutations.push(input); return response(); });
  try {
    await flush(); view.rerender({ locked: true });
    assert.equal(view.disabled, true); await view.run(view.save);
    view.rerender({ locked: false, busy: true });
    assert.equal(view.disabled, true); await view.run(view.save);
    assert.equal(mutations.length, 0); assert.deepEqual(view.busyChanges, []);
    view.rerender({ busy: false }); await view.run(view.save);
    assert.equal(mutations.length, 1);
  } finally { view.dispose(); }
});

test('account identity keys and fieldset lock binding remain in the actual UI', () => {
  const workspace = readFileSync(new URL('../src/account-workspace.tsx', import.meta.url), 'utf8');
  assert.match(workspace, /<ChatTranslationControls key=\{translationId\|\|'none'\}/);
  assert.match(source, /<ChatTranslationSettings key=\{id\} id=\{id\} locked=\{locked\}/);
  assert.match(source, /<fieldset disabled=\{disabled\}>/);
});

test('an unavailable optional component keeps existing enabled preferences off when appearance is saved', async () => {
 const mutations=[];
 const view=mountSettings(async input=>{
  if(input.settings)mutations.push(input.settings);
  return {...response(input.settings||defaults),immersive:{available:false,version:'1.33.1',message:'可选翻译组件未安装'}};
 });
 try{
  await flush();view.rerender();await view.run(view.save);
  assert.equal(mutations.length,1);assert.equal(mutations[0].enabled,false);
  assert.equal(view.state('availability').available,false);
  assert.match(source,/导入官方脚本文件/);assert.match(source,/availability\?\.available===false/);
 }finally{view.dispose()}
});
