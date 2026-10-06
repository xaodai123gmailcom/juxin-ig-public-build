// Execute the actual Python-embedded DOM projection with controlled SVG nodes.
// This checks JS behavior, not Chromium layout or Windows integration.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../../backend/app/playwright_worker.py', import.meta.url), 'utf8');
const privacyReader = source.split('    async def _has_visible_private_indicator(')[1]
  ?.split('\n    async def ')[0];
assert.ok(privacyReader, 'production privacy reader must be present');
const template = privacyReader.match(/script = """([\s\S]*?)"""\.replace\("MAXIMUM", str\(maximum\)\)/)?.[1];
assert.ok(template, 'production visible-text projection must be present');
assert.match(privacyReader, /icon_titles, maximum=40,[\s\S]*?svg_titles=True/,
  'privacy titles must use the bounded SVG projection');
assert.match(privacyReader, /element_expression = "node\.parentElement" if svg_titles else "node"/);
assert.match(privacyReader, /text_expression = "node\.textContent" if svg_titles else "node\.innerText"/);
const script = template.replaceAll('MAXIMUM', '40')
  .replaceAll('ELEMENT', 'node.parentElement').replaceAll('TEXT', 'node.textContent');
const project = runInNewContext(`(${script})`, {getComputedStyle: icon => icon.style});
const title = (text, {connected = true, display = 'block', visibility = 'visible', width = 24, height = 24, opacity = '1'} = {}) => ({
  textContent: text,
  parentElement: {
    isConnected: connected,
    style: {display, visibility, opacity},
    getBoundingClientRect: () => ({width, height}),
  },
});
const read = nodes => Array.from(project(nodes));

test('SVG title text survives despite the title itself having no layout box', () => {
  const node = title('Private account');
  node.getBoundingClientRect = () => ({width: 0, height: 0});
  assert.deepEqual(read([node]), ['Private account']);
});

test('hidden, collapsed, detached and zero-area icons supply no privacy evidence', () => {
  assert.deepEqual(read([
    title('Private hidden', {visibility: 'hidden'}),
    title('Private collapsed', {visibility: 'collapse'}),
    title('Private display none', {display: 'none'}),
    title('Private detached', {connected: false}),
    title('Private zero width', {width: 0}),
    title('Private zero height', {height: 0}),
    {textContent: 'Private orphan', parentElement: null},
    title('Camera'),
  ]), ['Camera']);
});

test('a visible private sibling survives vanished recommendation nodes', () => {
  assert.deepEqual(read([
    title('Recommendation', {connected: false}), title('Private account'), title('Camera'),
  ]), ['Private account', 'Camera']);
});

test('opacity alone keeps the previous Playwright visible-element semantics', () => {
  assert.deepEqual(read([title('Private account', {opacity: '0'})]), ['Private account']);
});

test('projection remains bounded and never waits for missing title text', () => {
  const nodes = Array.from({length: 40}, (_, i) => title(`Icon ${i}`));
  nodes.push({get parentElement() { throw Error('outside bounded scan'); }});
  assert.equal(read(nodes).length, 40);
  assert.deepEqual(read([title(null), title('')]), ['', '']);
});
