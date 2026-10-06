import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/cloud-workspace.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
}}).outputText;
const disabled = () => ({ enabled: false, configured: false, project_url: '', signed_in: false,
  email: '', revision: 0, last_sync_at: null, message: '云端未启用', auto_sync: false });
const configured = () => ({ ...disabled(), enabled: true, configured: true, project_url: 'https://example.supabase.co' });
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function mount(overrides = {}) {
  const states = [], refs = [], effects = [], timers = new Map(), commands = [], saves = [];
  let stateIndex = 0, refIndex = 0, first = true, tree;
  const hooks = {
    useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }]; },
    useRef(initial) { const index = refIndex++; if (!(index in refs)) refs[index] = { current: initial }; return refs[index]; },
    useCallback(callback) { return callback; },
    useEffect(callback) { if (first) effects.push(callback); },
  };
  const client = {
    cloudStatus: async () => disabled(),
    cloudCommand: async input => { commands.push(input); return configured(); },
    configureIntegrations: async input => { saves.push(input); return { cloud_activated: true, cloud_configured: true }; },
    ...overrides,
  };
  const exports = {};
  runInNewContext(compiled, { exports, require(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: 'fragment' };
    if (name === 'lucide-react') return { Cloud: 'cloud-icon', RefreshCw: 'refresh-icon', LogOut: 'logout-icon' };
    if (name === './core-client') return { getCollectorCoreClient: () => client };
    throw Error('Unexpected module: ' + name);
  }, setInterval(callback) { timers.set(1, callback); return 1; }, clearInterval(id) { timers.delete(id); } });
  function render() { stateIndex = 0; refIndex = 0; tree = exports.CloudWorkspace(); first = false; return tree; }
  function all(predicate) { const results = []; function visit(node) { if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(visit); return; } if (predicate(node)) results.push(node); visit(node.props?.children); }
    visit(tree); return results; }
  function text(node) { if (node == null || typeof node === 'boolean') return '';
    if (Array.isArray(node)) return node.map(text).join(''); if (typeof node !== 'object') return String(node); return text(node.props?.children); }
  render(); const cleanups = effects.map(effect => effect());
  return { render, all, text: () => text(tree), client, saves, commands, timers,
    field: placeholder => all(node => node.type === 'input' && node.props.placeholder?.includes(placeholder))[0],
    button: label => all(node => node.type === 'button' && text(node).includes(label))[0],
    configForm: () => all(node => node.type === 'form')[0],
    unmount() { cleanups.forEach(cleanup => cleanup?.()); },
  };
}
const event = { preventDefault() {} };


test('fresh cloud panel cannot probe or authenticate before configuration, and does not contact cloud on mount', async () => {
  const view = mount();
  try {
    await flush(); view.render();
    assert.equal(view.commands.length, 0);
    assert.equal(view.saves.length, 0);
    assert.equal(view.button('检查连接').props.disabled, true);
    assert.equal(view.button('登录并开启自动备份'), undefined);
    assert.equal(view.all(node => node.type === 'input' && node.props.type === 'checkbox')[0].props.checked, false);
    assert.equal(view.field('https://').props.value, '');
    assert.equal(view.field('首次配置').props.value, '');
  } finally { view.unmount(); }
  assert.equal(view.timers.size, 0);
});

test('explicit opt-in is write-only, repeated clicks share one save, and stale polls cannot restore old cloud state', async () => {
  const initialPoll = deferred(), save = deferred(), saves = [];
  let statusReads = 0;
  const view = mount({
    cloudStatus: async () => { statusReads++; return statusReads === 1 ? disabled() : statusReads === 2 ? initialPoll.promise : configured(); },
    configureIntegrations: async input => { saves.push(input); return save.promise; },
  });
  try {
    await flush(); view.render();
    view.field('https://').props.onChange({ target: { value: 'https://example.supabase.co' } });
    view.field('首次配置').props.onChange({ target: { value: 'sb_publishable_fixture' } });
    view.all(node => node.type === 'input' && node.props.type === 'checkbox')[0].props.onChange({ target: { checked: true } });
    view.render();
    view.timers.get(1)();
    const submit = view.configForm().props.onSubmit;
    const firstSave = submit(event); const secondSave = submit(event);
    await flush();
    assert.equal(saves.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(saves[0])), { cloud: { enabled: true, projectUrl: 'https://example.supabase.co', publishableKey: 'sb_publishable_fixture' } });
    save.resolve({ cloud_activated: true, cloud_configured: true });
    await firstSave; await secondSave; await flush(); view.render();
    assert.equal(view.field('首次配置').props.value, '');
    assert.ok(view.button('登录并开启自动备份'));
    initialPoll.resolve(disabled()); await flush(); view.render();
    assert.ok(view.button('登录并开启自动备份'), 'the late pre-save poll must not restore disabled state');
    assert.equal(view.commands.length, 0, 'configuration alone does not probe or sign in');
  } finally { view.unmount(); }
});

test('failed saves erase typed keys without exposing the underlying error, and an unmounted panel stops polling', async () => {
  const secret = 'sb_publishable_fixture_private';
  const view = mount({ configureIntegrations: async () => { throw Error(secret); } });
  await flush(); view.render();
  view.field('首次配置').props.onChange({ target: { value: secret } }); view.render();
  await view.configForm().props.onSubmit(event); await flush(); view.render();
  assert.equal(view.field('首次配置').props.value, '');
  assert.ok(view.text().includes('配置未激活'));
  assert.equal(view.text().includes(secret), false);
  view.unmount(); assert.equal(view.timers.size, 0);
});

test('credentials are only shown for an enabled project and passwords clear after a failed login', async () => {
  const view = mount({ cloudStatus: async () => configured(), cloudCommand: async () => { throw Error('fixture-password'); } });
  try {
    await flush(); view.render();
    view.field('你的邮箱').props.onChange({ target: { value: 'owner@example.test' } });
    view.field('此项目').props.onChange({ target: { value: 'fixture-password' } }); view.render();
    view.all(node => node.type === 'form')[1].props.onSubmit(event); await flush(); view.render();
    assert.equal(view.field('此项目').props.value, '');
    assert.equal(view.text().includes('fixture-password'), false);
  } finally { view.unmount(); }
});
