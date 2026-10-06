import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/account-browser-surface.tsx', import.meta.url), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };

function mountSurface({ bridge = async input => ({ attached: input.visible }), watch, close, interfere, resume, props = {} } = {}) {
  const start = source.indexOf('export function AccountBrowserSurface(');
  const end = source.indexOf('  return <div className="account-browser-surface"', start);
  assert.ok(start >= 0 && end > start);
  // Execute actual component hooks/handlers. Geometry and IPC are controlled;
  // this is a lifecycle/call-count test, not a browser rendering benchmark.
  const code = source.slice(start, end).replace('export function', 'function') + '\nreturn {show,watching,closeTaskPage,setViewTarget,chooseTaskPage,taskPages,frame,presentedTarget,currentDisplay,taskStatus,manualActive,manualEditable,beginManualControl,};\n}';
  const names = [...code.matchAll(/\[(\w+),\s*set\w+\]\s*=\s*useState/g)].map(match => match[1]);
  const states = [], refs = [], effects = [], writes = [], calls = [], watchCalls = [], observers = [];
  const timers = new Map(), listeners = new Map();
  const values = { id: 'account-a', native: true, opened: true, locked: false, visible: true, disabled: false, onOpen() {}, ...props };
  const geometry = { main: { x: 20, y: 70, width: 900, height: 700 }, live: { x: 20, y: 130, width: 900, height: 640 } };
  const main = { isConnected: true, getBoundingClientRect: () => geometry.main };
  const live = { isConnected: true, getBoundingClientRect: () => geometry.live };
  let stateIndex = 0, refIndex = 0, effectIndex = 0, timerId = 0;
  const sameDeps = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  function eventTarget(prefix) { return {
    addEventListener(name, callback) { const key = prefix + name; if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(callback); },
    removeEventListener(name, callback) { listeners.get(prefix + name)?.delete(callback); },
  }; }
  const location = { href: 'file:///app/index.html#accounts' };
  const document = { ...eventTarget('document:'), visibilityState: 'visible' };
  const window = { ...eventTarget('window:'), location, collectorCore: {
    ...(bridge === null ? {} : { accountSurface: input => { calls.push(input); return bridge(input); } }),
    ...(watch ? { accountTaskWatch: input => { watchCalls.push(input); return watch(input); } } : {}),
    ...(close ? { accountCloseTaskPage: close } : {}),
    ...(interfere ? { accountInterfere: interfere } : {}),
    ...(resume ? { accountResumeTask: resume } : {}),
    onTaskInterference: listener=>{listeners.set('task:interfere',new Set([listener]));return()=>listeners.delete('task:interfere')},
  } };
  function effectHook(layout) { return (effect, deps) => { const slot = effectIndex++; if (!sameDeps(effects[slot]?.deps, deps)) { effects[slot]?.cleanup?.(); effects[slot] = { effect, deps, layout, pending: true }; } }; }
  const render = runInNewContext(stripTypeScriptTypes(code) + '\n() => AccountBrowserSurface(props)', {
    props: values, window, document, location, crypto: { randomUUID: () => 'surface-instance' },
    useState(initial) { const slot = stateIndex++; if (!(slot in states)) states[slot] = typeof initial === 'function' ? initial() : initial; return [states[slot], next => { states[slot] = typeof next === 'function' ? next(states[slot]) : next; writes.push({ name: names[slot], value: states[slot] }); }]; },
    useRef(initial) { const slot = refIndex++; return refs[slot] ||= { current: slot === 0 ? main : slot === 1 ? live : initial }; },
    useEffect: effectHook(false), useLayoutEffect: effectHook(true),
    setInterval(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearInterval(id) { timers.delete(id); },
    ResizeObserver: class { constructor(callback) { this.callback = callback; this.active = false; this.observed = []; observers.push(this); } observe(element) { this.active = true; this.observed.push(element); } disconnect() { this.active = false; } },
  });
  const view = { calls, watchCalls, writes, timers, observers, geometry, document, main, live,
    state: name => states[names.indexOf(name)],
    listenerCount: () => [...listeners.values()].reduce((count, set) => count + set.size, 0),
    rerender(next = {}) { Object.assign(values, next); stateIndex = refIndex = effectIndex = 0; Object.assign(view, render()); for (const layout of [true, false]) for (const effect of effects) if (effect.layout === layout && effect.pending) { effect.pending = false; effect.cleanup = effect.effect(); } return view; },
    tick() { for (const callback of [...timers.values()]) callback(); },
    emit(name, ...args) { for (const callback of listeners.get(name) || []) callback(...args); },
    resize() { for (const observer of observers) if (observer.active) observer.callback(); },
    dispose() { for (const effect of effects) effect.cleanup?.(); },
  };
  return view.rerender();
}

test('hidden, closed, unselected, external, and locked surfaces perform one hide without ongoing observers', async () => {
  const scenarios = { overlay: { visible: false }, closed: { opened: false }, unselected: { id: '' }, external: { native: false }, locked: { locked: true } };
  const samples = [];
  for (const [name, props] of Object.entries(scenarios)) {
    const view = mountSurface({ props }); await flush();
    for (let cycle = 0; cycle < 5; cycle++) { view.tick(); await flush(); view.emit('window:scroll'); await flush(); view.resize(); await flush(); }
    const idleCalls = view.calls.length, timers = view.timers.size, activeObservers = view.observers.filter(observer => observer.active).length, listeners = view.listenerCount();
    view.dispose(); await flush();
    samples.push({ name, idleCalls, totalWithCleanup: view.calls.length, timers, activeObservers, listeners });
    assert.ok(view.calls.every(call => call.visible === false));
  }
  console.log('Surface bridge calls: ' + JSON.stringify(samples));
  for (const sample of samples) {
    assert.equal(sample.idleCalls, 1, sample.name + ': initial hide only');
    assert.equal(sample.totalWithCleanup, 2, sample.name + ': cleanup still hides');
    assert.equal(sample.timers, 0, sample.name + ': no hidden polling');
    assert.equal(sample.activeObservers, 0, sample.name + ': no hidden observer');
    assert.equal(sample.listeners, 0, sample.name + ': no global hidden listeners');
  }
});

test('a shown surface retains the permission heartbeat even with unchanged geometry', async () => {
  const view = mountSurface();
  try {
    await flush(); for (let tick = 0; tick < 5; tick++) { view.tick(); await flush(); }
    assert.equal(view.calls.length, 6);
    assert.ok(view.calls.every(call => call.visible && call.readOnly === false));
    assert.equal(view.timers.size, 1);
    assert.equal(view.observers.filter(observer => observer.active).length, 1);
    assert.equal(view.listenerCount(), 5);
  } finally { view.dispose(); }
});

test('rapid hide and show immediately restore a fresh surface while ignoring a pending old reply', async () => {
  const requests = [];
  const view = mountSurface({ bridge: input => { if (!input.visible) return Promise.resolve({ attached: false }); const pending = deferred(); requests.push(pending); return pending.promise; } });
  try {
    await flush(); const originalId = view.calls[0].surfaceId;
    view.rerender({ visible: false }); await flush();
    assert.equal(view.timers.size, 0);
    assert.equal(view.calls.length, 3, 'old cleanup and the new hidden synchronization both execute');
    view.rerender({ visible: true }); await flush();
    assert.equal(requests.length, 2, 'show does not wait for the old visible IPC');
    assert.equal(view.calls.length, 5);
    assert.notEqual(view.calls[4].surfaceId, originalId);
    requests[1].resolve({ attached: true, message: 'current surface' }); await flush();
    requests[0].resolve({ attached: false, message: 'late old surface' }); await flush();
    assert.equal(view.state('mounted'), true);
    assert.equal(view.state('error'), 'current surface');
    assert.equal(view.timers.size, 1);
  } finally { view.dispose(); }
});

test('switching accounts hides only the old surface and rejects its late bridge response', async () => {
  const requests = new Map();
  const view = mountSurface({ bridge: input => { if (!input.visible) return Promise.resolve({ attached: false }); const pending = deferred(); requests.set(input.id, pending); return pending.promise; } });
  try {
    await flush(); const oldSurface = view.calls[0].surfaceId;
    view.rerender({ id: 'account-b' }); await flush();
    assert.equal(view.calls[1].id, 'account-a'); assert.equal(view.calls[1].surfaceId, oldSurface); assert.equal(view.calls[1].visible, false);
    assert.equal(view.calls[2].id, 'account-b'); assert.notEqual(view.calls[2].surfaceId, oldSurface);
    requests.get('account-b').resolve({ attached: true, message: '' }); await flush();
    requests.get('account-a').reject(new Error('late A failure')); await flush();
    assert.equal(view.state('mounted'), true); assert.equal(view.state('error'), '');
    assert.equal(view.calls.filter(call => call.id === 'account-b' && !call.visible).length, 0);
  } finally { view.dispose(); }
});

test('task watching keeps its independent polling and attaches only as read-only after a frame arrives', async () => {
  const firstFrame = deferred(); let reads = 0;
  const frame = { target: 'page-1', pages: [{ id: 'page-1', label: '采集页', title: 'Task page' }], image: '', captured_at: '2026-09-16T10:00:00Z', task_key: 'task-key', title: 'Task page' };
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: () => ++reads === 1 ? firstFrame.promise : Promise.resolve(frame) });
  try {
    await flush(); assert.equal(view.show, false); assert.equal(view.watching, true);
    assert.equal(view.calls.length, 1); assert.equal(view.timers.size, 1, 'task-watch polling must continue while its first frame is loading');
    firstFrame.resolve(frame); await flush(); view.rerender(); await flush();
    assert.equal(view.show, true); assert.equal(view.timers.size, 2);
    const attached = view.calls.filter(call => call.visible);
    assert.equal(attached.length, 1); assert.equal(attached[0].readOnly, true); assert.equal(attached[0].viewTarget, 'page-1');
    assert.equal(attached[0].bounds.y, view.geometry.live.y);
    view.tick(); await flush(); assert.equal(reads, 2);
    assert.ok(view.calls.filter(call => call.visible).every(call => call.readOnly === true));
    view.document.visibilityState = 'hidden'; view.tick(); await flush();
    assert.equal(reads, 2, 'hidden documents still suppress task-frame reads');
    assert.equal(view.calls.at(-1).visible, false);
  } finally { view.dispose(); }
});

test('resize during pending IPC coalesces and sends the latest geometry without dropping the later heartbeat', async () => {
  const requests = [];
  const view = mountSurface({ bridge: input => { if (!input.visible) return Promise.resolve({ attached: false }); const pending = deferred(); requests.push(pending); return pending.promise; } });
  try {
    await flush();
    for (const width of [400, 700, 1050]) { view.geometry.main = { ...view.geometry.main, width }; view.resize(); }
    view.tick(); await flush(); assert.equal(requests.length, 1);
    requests[0].resolve({ attached: true }); await flush();
    assert.equal(requests.length, 2); assert.equal(view.calls[1].bounds.width, 1050);
    requests[1].resolve({ attached: true }); await flush();
    view.tick(); await flush(); assert.equal(requests.length, 3); assert.equal(view.calls[2].bounds.width, 1050);
    requests[2].resolve({ attached: true }); await flush();
  } finally { view.dispose(); }
});

test('missing bridge installs no surface work and reports the shown surface error', async () => {
  const view = mountSurface({ bridge: null });
  try { await flush(); assert.equal(view.timers.size, 0); assert.equal(view.listenerCount(), 0); assert.equal(view.observers.length, 0); assert.match(view.state('error'), /更新后的桌面程序/); }
  finally { view.dispose(); }
});

test('unmount removes shown listeners and prevents a pending bridge response from changing state', async () => {
  const pending = deferred();
  const view = mountSurface({ bridge: input => input.visible ? pending.promise : Promise.resolve({ attached: false }) });
  await flush(); view.dispose(); const writes = view.writes.length;
  pending.resolve({ attached: true, message: 'too late' }); await flush();
  assert.equal(view.writes.length, writes); assert.equal(view.timers.size, 0); assert.equal(view.listenerCount(), 0);
  assert.equal(view.observers.filter(observer => observer.active).length, 0);
  assert.equal(view.calls.length, 2); assert.equal(view.calls[1].capture, false);
});


test('r46 switching locked accounts never presents the previous account task target under the new owner', async () => {
  const second = deferred();
  const frame = { target: 'account-a-screen', pages: [{ id: 'account-a-screen', label: '筛选页 1', title: 'A' }], image: '', captured_at: '2026-09-17T10:00:00Z', task_key: 'task-a', title: 'A' };
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: input => input.id === 'account-a' ? Promise.resolve(frame) : second.promise });
  try {
    await flush(); view.rerender(); await flush();
    assert.ok(view.calls.some(call => call.visible && call.viewTarget === 'account-a-screen'));
    const before = view.calls.length;
    view.rerender({ id: 'account-b' }); await flush();
    assert.equal(view.calls.slice(before).some(call => call.id === 'account-b' && call.visible && call.viewTarget === 'account-a-screen'), false,
      'a layout effect runs before passive frame-reset effects; cached frames need an account owner');
    second.resolve({ ...frame, target: 'account-b-screen', pages: [{ id: 'account-b-screen', label: '筛选页 1', title: 'B' }], task_key: 'task-b' });
    await flush(); view.rerender(); await flush();
    assert.equal(view.calls.at(-1).viewTarget, 'account-b-screen');
    assert.equal(view.calls.at(-1).readOnly, true);
  } finally { view.dispose(); }
});

test('r46 a removed selected task target is detached before waiting for the current-task fallback', async () => {
  const fallback = deferred(); let defaults = 0;
  const frame = { target: 'screen-old', pages: [{ id: 'screen-old', label: '筛选页 1', title: 'Old' }], image: '', captured_at: '2026-09-17T10:00:00Z', task_key: 'task-a', title: 'Old' };
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: input => input.target ? Promise.reject(new Error('任务页面不可用')) : ++defaults === 1 ? Promise.resolve(frame) : fallback.promise });
  try {
    await flush(); view.rerender(); await flush();
    view.setViewTarget('screen-old'); view.rerender(); await flush(); view.rerender(); await flush();
    assert.equal(view.frame, null, 'failed selection must not keep the old frame attached through fallback metadata latency');
    assert.equal(view.show, false);
    fallback.resolve({ ...frame, target: 'screen-current', pages: [{ id: 'screen-current', label: '筛选页 1', title: 'Current' }] });
    await flush(); view.rerender(); await flush();
    assert.equal(view.calls.at(-1).viewTarget, 'screen-current');
  } finally { view.dispose(); }
});


test('r46 task display states follow the native surface and recover without navigating or changing the selected target', async () => {
  const frame = { target: 'screen-1', pages: [{ id: 'screen-1', label: '筛选页 1', title: 'Task' }], image: '', captured_at: '2026-09-17T10:00:00Z', task_key: 'task-a', title: 'Task', display_state: 'ready' };
  let nativeState = 'blank';
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: async () => frame,
    bridge: async input => ({ attached: input.visible && nativeState === 'ready', display_state: nativeState, display_message: 'native:' + nativeState }) });
  try {
    await flush(); view.rerender(); await flush(); view.rerender();
    for (const [state, title] of [['blank','任务页面正在准备'], ['loading','任务网页正在加载'], ['load_failed','任务网页加载失败'], ['crashed','任务网页渲染异常'], ['unresponsive','任务网页暂时无响应']]) {
      nativeState = state; view.tick(); await flush(); view.rerender(); await flush();
      assert.equal(view.taskStatus.title, title);
      assert.equal(view.currentDisplay.display_message, 'native:' + state);
      assert.equal(view.state('mounted'), false);
      assert.equal(view.state('error'), '', 'task state is presented in the viewport, not a repeated generic error');
      assert.equal(view.presentedTarget, 'screen-1');
    }
    nativeState = 'ready'; view.tick(); await flush(); view.rerender(); await flush();
    assert.equal(view.state('mounted'), true);
    assert.equal(view.taskStatus.title, '任务画面已显示');
    assert.ok(view.calls.filter(call => call.visible).every(call => call.readOnly && call.viewTarget === 'screen-1'));
  } finally { view.dispose(); }
});

test('r46 the live task viewport is observed independently when tabs or status text change its bounds', async () => {
  const frame = { target: 'screen-1', pages: [{ id: 'screen-1', label: '筛选页 1', title: 'Task' }], image: '', captured_at: '2026-09-17T10:00:00Z', task_key: 'task-a', title: 'Task' };
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: async () => frame });
  try {
    await flush(); view.rerender(); await flush();
    const observer = view.observers.find(observer => observer.active);
    assert.ok(observer.observed.includes(view.live), 'outer account bounds can remain unchanged as the toolbar grows');
    assert.ok(observer.observed.includes(view.main));
    const before = view.calls.length;
    view.geometry.live = { ...view.geometry.live, y: 180, height: 590 }; view.resize(); await flush();
    assert.ok(view.calls.length > before);
    assert.equal(view.calls.at(-1).bounds.y, 180);
    assert.equal(view.calls.at(-1).bounds.height, 590);
  } finally { view.dispose(); }
});

test('r46 normal account pages keep native failure diagnostics visible outside task watch', async () => {
  const view = mountSurface({ bridge: async () => ({ attached: false, display_state: 'crashed', display_message: '网页渲染异常' }) });
  try { await flush(); assert.equal(view.state('error'), '网页渲染异常'); }
  finally { view.dispose(); }
});


test('r46 a metadata response started before explicit task-page close cannot resurrect the closed target', async () => {
  const late = deferred(); let reads = 0;
  const frame = { target: 'screen-1', pages: [{ id: 'screen-1', label: '筛选页 1', title: 'Task' }], image: '', captured_at: '2026-09-17T10:00:00Z', task_key: 'task-a', title: 'Task' };
  const view = mountSurface({ props: { locked: true, taskWatch: true }, watch: () => ++reads === 1 ? Promise.resolve(frame) : late.promise,
    close: async input => ({ closed: true, target: input.target, message: '页面已关闭，任务已暂停' }) });
  try {
    await flush(); view.rerender(); await flush();
    view.tick(); await flush();
    await view.closeTaskPage('screen-1'); view.rerender(); await flush();
    assert.equal(view.frame, null);
    late.resolve(frame); await flush(); view.rerender(); await flush();
    assert.equal(view.frame, null, 'late metadata must not reattach the target whose close was acknowledged');
    assert.equal(view.show, false);
    assert.match(view.state('pageMessage'), /已关闭/);
  } finally { view.dispose(); }
});


const manualFrame = () => ({target:'source-1',pages:[{id:'source-1',label:'采集页',title:'Source'},{id:'screen-1',label:'筛选页 1',title:'Screen'}],image:'',captured_at:'2026-09-18T10:00:00Z',task_key:'task-a',title:'Source',operation:'collection'});
const manualGrant = () => ({active:true,grant:'lease-bound-grant',task_key:'task-a',target:'source-1'});

test('r51 current task remains read-only until the pause acknowledgement grants its exact page', async () => {
  const pause = deferred(), calls = [], view = mountSurface({props:{locked:true,taskWatch:true},watch:async()=>manualFrame(),interfere:input=>{calls.push(input);return pause.promise}});
  try {
    await flush(); view.rerender(); await flush();
    const beginning=view.beginManualControl();view.rerender();await flush();view.tick();await flush();
    assert.equal(view.manualEditable,false);assert.equal(calls.length,1);assert.ok(view.calls.filter(x=>x.visible).every(x=>x.readOnly));
    pause.resolve(manualGrant());await beginning;view.rerender();await flush();
    assert.equal(view.manualEditable,true);assert.equal(view.calls.at(-1).readOnly,false);
    assert.equal(view.calls.at(-1).interferenceGrant,'lease-bound-grant');assert.equal(view.calls.at(-1).viewTarget,'source-1');
  } finally {view.dispose();}
});

for(const invalid of [{...manualGrant(),task_key:'another-task'},{...manualGrant(),target:'another-page'},{...manualGrant(),grant:''}]) {
  test('r51 wrong or missing pause authorization never enables manual input: '+JSON.stringify(invalid), async () => {
    const view=mountSurface({props:{locked:true,taskWatch:true},watch:async()=>manualFrame(),interfere:async()=>invalid});
    try {await flush();view.rerender();await flush();await view.beginManualControl();view.rerender();await flush();assert.equal(view.manualEditable,false);assert.ok(view.calls.filter(x=>x.visible).every(x=>x.readOnly));assert.match(view.state('pageMessage'),/授权已变化/);}
    finally{view.dispose();}
  });
}

test('r51 late pause replies cannot authorize a different account', async()=>{
  const pause=deferred(),view=mountSurface({props:{locked:true,taskWatch:true},watch:async input=>({...manualFrame(),task_key:input.id==='account-a'?'task-a':'task-b'}),interfere:()=>pause.promise});
  try{await flush();view.rerender();await flush();const pending=view.beginManualControl();view.rerender({id:'account-b'});await flush();view.rerender();pause.resolve(manualGrant());await pending;view.rerender();await flush();assert.equal(view.manualEditable,false);assert.equal(view.calls.some(x=>x.id==='account-b'&&x.visible&&!x.readOnly),false);}
  finally{view.dispose();}
});

test('r51 manual grant survives revisiting its page but does not allow another page or ordinary task-page close', async()=>{
  let closes=0;
  const view=mountSurface({props:{locked:true,taskWatch:true},watch:async input=>({...manualFrame(),target:input.target||'source-1',manual_control:manualGrant()}),close:async()=>{closes++;return {closed:true}}});
  try{await flush();view.rerender();await flush();assert.equal(view.manualEditable,true);await view.closeTaskPage('source-1');assert.equal(closes,0);
    view.setViewTarget('screen-1');view.rerender();await flush();view.rerender();await flush();assert.equal(view.manualActive,true);assert.equal(view.manualEditable,false);assert.equal(view.calls.at(-1).readOnly,true);assert.equal(view.calls.at(-1).interferenceGrant,undefined);
    view.setViewTarget('source-1');view.rerender();await flush();view.rerender();await flush();assert.equal(view.manualEditable,true);
  }finally{view.dispose();}
});


test('r51 cancelling the confirmation keeps the task read-only and never requests a surface grant',async()=>{
 const view=mountSurface({props:{locked:true,taskWatch:true},watch:async()=>manualFrame(),interfere:async()=>({cancelled:true})});
 try{await flush();view.rerender();await flush();await view.beginManualControl();view.rerender();await flush();assert.equal(view.manualEditable,false);assert.ok(view.calls.filter(x=>x.visible).every(x=>x.readOnly));assert.equal(view.state('pageMessage'),'');}
 finally{view.dispose();}
});

test('r51 fresh watch metadata after ordinary resume removes manual input',async()=>{
 let manual=true;const view=mountSurface({props:{locked:true,taskWatch:true},watch:async()=>({...manualFrame(),manual_control:manual?manualGrant():null})});
 try{await flush();view.rerender();await flush();assert.equal(view.manualEditable,true);manual=false;view.tick();await flush();view.rerender();await flush();assert.equal(view.manualEditable,false);assert.equal(view.calls.filter(x=>x.visible).at(-1).readOnly,true);}
 finally{view.dispose();}
});


test('r51 native shield accepts only the selected page and repeated clicks share one pending confirmation',async()=>{
 const pause=deferred();let requests=0;const view=mountSurface({props:{locked:true,taskWatch:true},watch:async()=>manualFrame(),interfere:()=>{requests++;return pause.promise}});
 try{await flush();view.rerender();await flush();view.emit('task:interfere','another-page');assert.equal(requests,0);view.emit('task:interfere','source-1');view.emit('task:interfere','source-1');await flush();assert.equal(requests,1);assert.equal(view.manualEditable,false);pause.resolve({cancelled:true});await flush();view.rerender();assert.equal(view.manualEditable,false);}
 finally{view.dispose();}
});

test('r93 selected slot waits while absent and resumes on its replacement without showing a sibling',async()=>{
  let child='old-child';
  const sourcePage={id:'source',label:'采集页',title:'Source'};
  const view=mountSurface({props:{locked:true,taskWatch:true},watch:async input=>{
    const pages=[sourcePage,...(child?[{id:child,label:'1-1',title:'Child'}]:[])];
    if(input.target&&!pages.some(page=>page.id===input.target))throw new Error('任务页面不可用');
    return {target:input.target||'source',pages,image:'',captured_at:'2026-09-27T10:00:00Z',task_key:'task-a',operation:'collection',screening_slots:2,title:'Task',display_state:'ready'};
  }});
  try{
    await flush();view.rerender();await flush();
    view.chooseTaskPage({id:child,label:'1-1'});view.rerender();await flush();view.rerender();await flush();
    assert.equal(view.presentedTarget,'old-child');
    child='';view.tick();await flush();view.rerender();await flush();
    assert.equal(view.presentedTarget,'');assert.equal(view.show,false);
    assert.equal(view.frame.waiting_target,'1-1');
    assert.deepEqual(Array.from(view.taskPages,page=>page.label),['采集页','1-1','1-2']);
    child='new-child';view.tick();await flush();view.rerender();await flush();
    assert.equal(view.presentedTarget,'new-child');
    assert.equal(view.state('viewLabel'),'1-1');
    assert.equal(view.calls.filter(call=>call.visible).at(-1).viewTarget,'new-child');
  }finally{view.dispose();}
});

test('r94 rapid account switches do not capture departed native pages',async()=>{
 const view=mountSurface();
 try{
  await flush();
  for(let i=0;i<50;i++){view.rerender({id:'switch-'+i});await flush()}
  assert.equal(view.calls.filter(call=>call.capture).length,0);
  assert.equal(view.calls.at(-1).id,'switch-49');assert.equal(view.calls.at(-1).visible,true);
 }finally{view.dispose()}
});

test('r94 a same-account overlay still captures its preview once',async()=>{
 const view=mountSurface({bridge:async input=>({attached:input.visible,preview:input.capture?'saved-preview':''})});
 try{
  await flush();view.rerender({visible:false});await flush();
  assert.equal(view.calls.filter(call=>call.capture).length,1);
  assert.equal(view.state('preview').image,'saved-preview');
 }finally{view.dispose()}
});

test('r94 unchanged resize and nested-scroll events do not repeat permission IPC',async()=>{
 const view=mountSurface();
 try{
  await flush();const initial=view.calls.length;
  for(let i=0;i<50;i++){view.resize();view.emit('window:scroll');await flush()}
  assert.equal(view.calls.length,initial);
  view.tick();await flush();assert.equal(view.calls.length,initial+1,'security heartbeat remains active');
 }finally{view.dispose()}
});

test('r94 native window resize renews display permission immediately even at unchanged element size',async()=>{
 const view=mountSurface();
 try{await flush();const before=view.calls.length;view.emit('window:resize');await flush();
  assert.equal(view.calls.length,before+1,'native resize invalidates the old display grant');
  assert.equal(view.calls.at(-1).visible,true);
 }finally{view.dispose()}
});

test('r94 unchanged geometry during slow authorization does not create another forced authorization',async()=>{
 const first=deferred();let requests=0;
 const view=mountSurface({bridge:input=>input.visible?(++requests===1?first.promise:Promise.resolve({attached:true})):Promise.resolve({attached:false})});
 try{await flush();for(let i=0;i<100;i++){view.resize();view.emit('window:scroll')}
  first.resolve({attached:true});await flush();assert.equal(requests,1);
  view.tick();await flush();assert.equal(requests,2,'real permission heartbeat must remain');
 }finally{first.resolve({attached:true});view.dispose()}
});

test('r94 visibility loss hides immediately during pending IPC and resume ignores the old completion',async()=>{
 const requests=[];
 const view=mountSurface({bridge:input=>{if(!input.visible)return Promise.resolve({attached:false});const pending=deferred();requests.push(pending);return pending.promise}});
 try{
  await flush();view.document.visibilityState='hidden';view.emit('document:visibilitychange');await flush();
  assert.equal(view.calls.at(-1).visible,false,'hide cannot wait behind the blocked show');
  const hiddenCount=view.calls.length;for(let i=0;i<5;i++){view.tick();view.resize();view.emit('window:scroll');await flush()}
  assert.equal(view.calls.length,hiddenCount,'hidden windows must not poll native geometry or display grants');
  view.document.visibilityState='visible';view.emit('document:visibilitychange');await flush();
  assert.equal(requests.length,2,'restore must not wait for the stale IPC');
  requests[1].resolve({attached:true,message:'restored'});await flush();
  requests[0].resolve({attached:false,message:'obsolete'});await flush();
  assert.equal(view.state('mounted'),true);assert.equal(view.state('error'),'restored');
 }finally{requests.forEach(p=>p.resolve({attached:false}));view.dispose()}
});

test('r94 selecting another task slot detaches the old editable page before slow metadata arrives',async()=>{
 const pending=deferred(),base={...manualFrame(),manual_control:manualGrant()};
 const view=mountSurface({props:{locked:true,taskWatch:true},watch:input=>input.target?pending.promise:Promise.resolve(base)});
 try{
  await flush();view.rerender();await flush();assert.equal(view.manualEditable,true);
  view.chooseTaskPage({id:'screen-1',label:'筛选页 1'});view.rerender();await flush();
  assert.equal(view.show,false,'newly selected tab must not keep the old native page interactive');
  assert.equal(view.manualEditable,false);assert.equal(view.calls.at(-1).visible,false);
  pending.resolve({...base,target:'screen-1'});await flush();view.rerender();await flush();
  assert.equal(view.presentedTarget,'screen-1');assert.equal(view.show,true);assert.equal(view.manualEditable,false);
  assert.equal(view.calls.at(-1).readOnly,true);
 }finally{pending.resolve({...base,target:'screen-1'});view.dispose()}
});

for(const heartbeat of [false,true])test(`r94 queued resize returning to original bounds preserves only a real heartbeat (${heartbeat})`,async()=>{
 const first=deferred();let requests=0;
 const view=mountSurface({bridge:input=>input.visible?(++requests===1?first.promise:Promise.resolve({attached:true})):Promise.resolve({attached:false})});
 try{
  await flush();const width=view.geometry.main.width;
  view.geometry.main={...view.geometry.main,width:width-200};view.resize();
  view.geometry.main={...view.geometry.main,width};view.resize();if(heartbeat)view.tick();
  first.resolve({attached:true});await flush();assert.equal(requests,heartbeat?2:1);
 }finally{first.resolve({attached:false});view.dispose()}
});

test('r94 repeated hide/restore never accepts obsolete failures or loses the current pending request',async()=>{
 const requests=[];
 const view=mountSurface({bridge:input=>{if(!input.visible)return Promise.resolve({attached:false});const pending=deferred();requests.push(pending);return pending.promise}});
 try{
  await flush();
  for(let i=0;i<50;i++){
   view.document.visibilityState='hidden';view.emit('document:visibilitychange');
   view.document.visibilityState='visible';view.emit('document:visibilitychange');await flush();
  }
  assert.equal(requests.length,51);
  for(const pending of requests.slice(0,-1).reverse())pending.reject(new Error('obsolete window'));
  await flush();view.tick();await flush();assert.equal(requests.length,51,'old completions cannot release the current IPC guard');
  requests.at(-1).resolve({attached:true,message:'latest'});await flush();
  assert.equal(requests.length,52,'queued heartbeat runs after the current request');
  requests.at(-1).resolve({attached:true,message:'current'});await flush();
  assert.equal(view.state('mounted'),true);assert.equal(view.state('error'),'current');
 }finally{requests.forEach(p=>p.resolve({attached:false}));view.dispose()}
});
