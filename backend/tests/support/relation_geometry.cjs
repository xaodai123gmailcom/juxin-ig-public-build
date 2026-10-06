// Geometry adapter, not a browser: run production visibility and scroll scripts
// against independently calculated row boxes and clamped scrollTop properties.
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const options = input.options;
const scale = options.scale ?? 1;
const total = options.total ?? 469;
const client = options.client ?? 600;
const header = options.header ?? 0;
let clip = options.clip ?? 260;
const rowHeight = options.rowHeight ?? 40;
const content = Math.max(client, total * rowHeight);
const box = (top, height) => ({top, bottom: top + height, left: 0, right: 400,
  width: 400, height});
const view = {innerHeight: options.windowHeight ?? 1200, innerWidth: 800};
const doc = {defaultView: view};
global.getComputedStyle = node => ({display: 'block', visibility: 'visible',
  overflowY: node.overflow ?? 'visible', overflowX: 'visible'});
function scrollProperty(node, maximum) {
  let value = 0;
  Object.defineProperty(node, 'scrollTop', {get: () => value,
    set: next => {value = options.stuck ? value : Math.min(maximum(), Math.max(0, next));}});
}
const outer = {overflow: options.overflow ?? 'hidden', clientHeight: clip,
  scrollHeight: options.outerContent ?? header + client, ownerDocument: doc, clientTop: 0, offsetHeight: clip,
  getBoundingClientRect: () => box(0, clip * scale)};
scrollProperty(outer, () => outer.overflow === 'clip' ? 0 : Math.max(0, outer.scrollHeight - clip));
const inner = {overflow: 'auto', clientHeight: client, scrollHeight: content,
  parentElement: outer, ownerDocument: doc, clientTop: 0, offsetHeight: client,
  getBoundingClientRect: () => box((header - outer.scrollTop) * scale, client * scale)};
scrollProperty(inner, () => Math.max(0, content - client));
const rows = Array.from({length: total}, (_, index) => ({
  parentElement: inner, ownerDocument: doc,
  textContent: `account_${index}`, innerText: `account_${index}`,
  getAttribute: key => key === 'href' ? `/account_${index}/` : null,
  getBoundingClientRect: () => box((header + index * rowHeight - inner.scrollTop - outer.scrollTop) * scale,
    (options.linkHeight ?? rowHeight) * scale),
}));
const headerHrefs = options.headerHrefs ?? (options.duplicateHeaderLinks ? ['/source/', '/source/'] : []);
const headerLinks = headerHrefs.map((href, index) => ({
  parentElement: outer, ownerDocument: doc, textContent: 'source', innerText: 'source',
  getAttribute: key => key === 'href' ? href : null,
  getBoundingClientRect: () => box(((options.headerLinkTops?.[index] ?? index * 20) - outer.scrollTop) * scale, 18 * scale),
}));
const cards = rows.map((row, index) => ({
  parentElement: inner, ownerDocument: doc, textContent: row.textContent, innerText: row.innerText,
  getBoundingClientRect: () => box((header + index * rowHeight - inner.scrollTop - outer.scrollTop) * scale, rowHeight * scale),
  contains: node => node === row,
  querySelector: () => null,
}));
if (options.accountCards) rows.forEach((row, index) => {row.parentElement = cards[index]});
let rowsDetached = false;
const currentRows = () => {
  if (rowsDetached) return [];
  if (!options.virtualRows) return rows;
  const start = Math.floor(inner.scrollTop / rowHeight);
  return rows.slice(start, start + options.virtualRows);
};
const currentElements = () => currentRows().flatMap(row => options.accountCards ? [row.parentElement, row] : [row]);
inner.contains = node => node === inner || currentElements().includes(node);
outer.contains = node => node === outer || inner.contains(node) || headerLinks.includes(node);
inner.querySelectorAll = selector => selector === "*" ? currentElements() : currentRows();
outer.querySelectorAll = selector => selector === '*'
  ? [...headerLinks, inner, ...currentElements()] : [...headerLinks, ...currentRows()];
inner.querySelector = () => null;
outer.querySelector = () => null;
const project = eval('(' + input.projection + ')');
const advance = node => {
  const names = options.acknowledgedOverride ?? project(node).positioned_rows.map(row => row.username);
  const script = options.positionGuard ? input.guarded_advance.replace('new Set([])', 'new Set(' + JSON.stringify(names) + ')') : input.advance;
  return eval('(' + script + ')')(node);
};
const measure = eval('(' + input.measure + ')');
const reset = eval('(' + input.reset + ')');
if (options.startAtBottom) inner.scrollTop = content;
if (options.initialOuter) outer.scrollTop = options.initialOuter;
if (options.reset) reset(outer);
const snapshot = () => ({...project(outer),
  measurement: measure(outer), inner: inner.scrollTop, outer: outer.scrollTop});
const frames = [];
if (options.single) {
  frames.push(snapshot());
  frames[0].movement = advance(outer);
  if (options.detachRowsAfterAdvance) rowsDetached = true;
  frames.push(snapshot());
  if (options.restoreRowsAfterGap) {rowsDetached = false; frames.push(snapshot());}
} else {
  for (let step = 0; step < 4000; step++) {
    const frame = snapshot(); frames.push(frame);
    if (frame.measurement.bottom || !frame.measurement.valid) break;
    frame.movement = advance(outer);
    if (!frame.movement.moved) {frames.push(snapshot()); break;}
    if (options.resizeAt === step) {
      clip = options.resizeTo; outer.clientHeight = clip; outer.offsetHeight = clip;
      // Browsers clamp the outer offset when the visible region grows.
      outer.scrollTop = outer.scrollTop;
    }
  }
}
const seen = new Set(frames.flatMap(frame => frame.hrefs));
process.stdout.write(JSON.stringify({total, seen: seen.size, frames}));
