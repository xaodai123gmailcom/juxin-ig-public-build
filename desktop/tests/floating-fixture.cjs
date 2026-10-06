/* Test-only geometry. Never used by production windows or task scheduling. */
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const {rendererFixtureRead} = require('./renderer-fixture.cjs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const keys = ['x', 'y', 'width', 'height'];
function inside(bounds, area) {
 return keys.every(key => Number.isFinite(bounds[key])) && bounds.width > 0 && bounds.height > 0 &&
  bounds.x >= area.x && bounds.y >= area.y &&
  bounds.x + bounds.width <= area.x + area.width && bounds.y + bounds.height <= area.y + area.height;
}
function near(actual, expected, tolerance = 1) {
 return keys.every(key => Number.isFinite(actual[key]) && Math.abs(actual[key] - expected[key]) <= tolerance);
}
function fixtureWindowBounds(area) {
 assert.ok(keys.every(key => Number.isFinite(area[key])) && area.width >= 392 && area.height >= 332,
  'Floating fixture needs a work area of at least 392 x 332 DIP: ' + JSON.stringify(area));
 return {x: Math.ceil(area.x + 16), y: Math.ceil(area.y + 16),
  width: Math.floor(Math.min(1120, area.width - 32)), height: Math.floor(Math.min(720, area.height - 32))};
}
async function settledBounds(win, accept = () => true, options = {}) {
 const {timeout = 3000, stableFor = 80, now = () => performance.now(), sleep = pause} = options;
 const started = now();
 let last, since = started;
 while (true) {
  assert.equal(win.isDestroyed(), false, 'Floating fixture window was destroyed before geometry settled');
  const bounds = win.getBounds(), time = now();
  if (!last || !near(bounds, last, 0) || !accept(bounds)) since = time;
  if (accept(bounds) && time - since >= stableFor) return {...bounds};
  if (time - started >= timeout) throw new Error('Floating fixture bounds did not settle: ' + JSON.stringify(bounds));
  last = {...bounds};
  await sleep(20);
 }
}
async function prepareFixtureWindow(win, screen, {rendererTimeoutMs = 5000} = {}) {
 const original = win.getBounds(), area = screen.getDisplayMatching(original).workArea;
 const minimum = typeof win.getMinimumSize === 'function' ? win.getMinimumSize() : null;
 const target = fixtureWindowBounds(area);
 const restore = async () => {
  if (win.isDestroyed()) return;
  if (minimum) win.setMinimumSize(...minimum);
  win.setBounds(original);
  await settledBounds(win, bounds => near(bounds, original));
 };
 try {
  // The desktop integration suite needs a large client area. Floating-page
  // cases temporarily exercise the actual work area, then restore that limit.
  if (minimum) win.setMinimumSize(0, 0);
  win.setBounds(target);
  const bounds = await settledBounds(win, value => inside(value, area) && near(value, target));
  console.log('CHECK floating fixture renderer layout (bounded, no animation frames)');
  // Reading layout flushes pending style/layout synchronously. Waiting for paint
  // is unsafe here: the build terminal may cover this native window completely.
  const layout = await rendererFixtureRead(win.webContents,
   '(()=>{const r=document.documentElement.getBoundingClientRect();return {width:innerWidth,height:innerHeight,documentWidth:r.width,ready:document.readyState}})()',
   {label:'floating parent layout',timeoutMs:rendererTimeoutMs});
  assert.ok(layout && Number.isFinite(layout.width) && layout.width>0 &&
   Number.isFinite(layout.height) && layout.height>0 &&
   Number.isFinite(layout.documentWidth) && layout.documentWidth>=0,
   'Floating fixture renderer returned invalid layout: '+JSON.stringify(layout));
  console.log('CHECK floating fixture geometry', JSON.stringify({area, bounds, content: win.getContentBounds(), zoom: win.webContents.getZoomFactor(), layout}));
 } catch (error) {
  try { await restore(); } catch (restoreError) { console.error('Floating fixture restore failed after setup error',restoreError); }
  throw error;
 }
 return restore;
}
function dragTarget(bounds, area, distance = 70) {
 assert.ok(inside(bounds, area), 'Floating fixture cannot drag an off-screen starting window');
 const left = bounds.x - area.x, right = area.x + area.width - bounds.x - bounds.width;
 const up = bounds.y - area.y, down = area.y + area.height - bounds.y - bounds.height;
 const dx = left >= right ? -Math.min(distance, left) : Math.min(distance, right);
 const dy = up >= down ? -Math.min(20, up) : Math.min(20, down);
 const target = {...bounds, x: Math.round(bounds.x + dx), y: Math.round(bounds.y + dy)};
 assert.ok(inside(target, area) && Math.max(Math.abs(target.x - bounds.x), Math.abs(target.y - bounds.y)) >= 8,
  'Floating fixture has insufficient visible space for a meaningful drag');
 return target;
}
function reviewFixtureLayout(viewport, zoom = 1) {
 assert.ok(Number.isFinite(zoom) && zoom > 0 && Number.isFinite(viewport.width) && Number.isFinite(viewport.height), 'Invalid review fixture viewport');
 const left = Math.min(116, Math.floor(viewport.width * .1));
 const top = Math.max(78, Math.min(276, Math.floor(viewport.height * .3)));
 const width = Math.min(900, Math.floor(viewport.width - left - 24));
 const height = Math.min(400, Math.floor(viewport.height - top - 16));
 const avatarOffset = Math.min(600, width - 48);
 assert.ok(avatarOffset >= 332 && avatarOffset * zoom >= 344 && height >= 40,
  'Review fixture cannot fit a real avatar boundary in this viewport: ' + JSON.stringify({viewport, zoom}));
 return {left, top, width, height, avatarOffset};
}
module.exports = {inside, near, fixtureWindowBounds, settledBounds, prepareFixtureWindow, dragTarget, reviewFixtureLayout};
