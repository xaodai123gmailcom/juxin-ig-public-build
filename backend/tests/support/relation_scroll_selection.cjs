// Count DOM containment work independently of machine timing. Real browsers
// have row wrappers even when thousands of rows are preloaded offscreen.
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const total = input.total;
let containsChecks = 0;
const doc = {defaultView: {innerHeight: 720}};
const rect = (top, height) => ({top, bottom: top + height, left: 0,
  right: 400, width: 400, height});
const outer = {clientHeight: 260, scrollHeight: 280, offsetHeight: 260,
  scrollTop: 0, ownerDocument: doc, overflow: 'auto',
  getBoundingClientRect: () => rect(0, 260)};
const inner = {clientHeight: 240, scrollHeight: total * 40, offsetHeight: 240,
  scrollTop: 0, ownerDocument: doc, parentElement: outer, overflow: 'auto',
  getBoundingClientRect: () => rect(40, 240)};
const wrappers = [], rows = [];
for (let index = 0; index < total; index++) {
  const wrapper = {parentElement: inner, ownerDocument: doc, clientHeight: 40,
    scrollHeight: 40, getBoundingClientRect: () => rect(40 + index * 40, 40)};
  const row = {parentElement: wrapper, ownerDocument: doc,
    getAttribute: name => name === 'href' ? `/account_${index}/` : null,
    getBoundingClientRect: () => rect(40 + index * 40, 18)};
  wrapper.contains = el => {containsChecks++; return el === row || el === wrapper;};
  wrappers.push(wrapper); rows.push(row);
}
const descendants = new Set([...wrappers, ...rows]);
inner.contains = el => {containsChecks++; return el === inner || descendants.has(el);};
outer.contains = el => {containsChecks++; return el === outer || el === inner || descendants.has(el);};
inner.querySelectorAll = selector => selector === '*' ? [...wrappers, ...rows] : rows;
outer.querySelectorAll = selector => selector === '*' ? [inner, ...wrappers, ...rows] : rows;
global.getComputedStyle = node => ({display: 'block', visibility: 'visible',
  overflowY: node.overflow ?? 'visible', overflowX: 'visible'});
const measurement = eval('(' + input.measure + ')')(outer);
process.stdout.write(JSON.stringify({total, containsChecks, measurement}));
