// Execute the production card reader and count layout-sensitive DOM operations.
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const counts = {rects: 0, styles: 0, texts: 0, descendants: 0};
class Element {
  constructor(text, width=320, height=220, children=[]) {
    this.text = text; this.width = width; this.height = height;
    this.children = children; this.hidden = false;
  }
  get innerText() { counts.texts++; return this.text; }
  get textContent() { return this.rawText ?? this.text.replace(/\n/g, ''); }
  getBoundingClientRect() { counts.rects++; return {width:this.width, height:this.height}; }
  querySelectorAll(selector) {
    if (selector === 'a[href]') return [];
    counts.descendants++;
    return this.children;
  }
}
global.getComputedStyle = node => {
  counts.styles++;
  return {display:node.hidden ? 'none' : 'block', visibility:'visible'};
};
global.location = {href:'https://www.instagram.com/source/'};
const fields = ['12 posts', '345 followers', '67 following'];
const children = fields.map(value=>new Element(value,80,24));
const card = new Element('target.user\n'+fields.join('\n'),320,220,children);
const unrelated = Array.from({length:input.rows ?? 2000},(_,index)=>
  new Element('other.'+index+'\n12 posts\n345 followers\n67 following'));
const read = eval('('+input.script+')');
global.document = {querySelectorAll:()=>[...unrelated,card]};
if (input.kind === 'hidden') card.hidden = true;
if (input.kind === 'hidden-identity') {
  card.text = 'other.user\n'+fields.join('\n');
  card.rawText = 'target.user'+card.text;
}
if (input.kind === 'hidden-split-identity') {
  // Hidden inline decoration splits raw text but not the visible username.
  card.rawText = 'target.' + 'hidden decoration' + 'user' + fields.join('');
}
const first = read('target.user');
const firstCounts = {...counts};
let second;
if (input.kind === 'repaint') {
  card.text = 'target.user\n12 posts\n987 followers\n67 following';
  children[1].text = '987 followers';
  second = read('target.user');
}
process.stdout.write(JSON.stringify({first,second,counts:firstCounts}));
