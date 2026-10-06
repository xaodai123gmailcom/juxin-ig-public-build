// Offline geometry, not a browser. Production DOM functions are supplied unchanged.
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const o = input.options, names = input.names;
const scale = o.scale, rowHeight = o.row_height, client = o.client;
let clip = o.clip;
const header = o.header;
const doc = {defaultView: {innerHeight: 1400, innerWidth: 900}};
const box = (top, height) => ({top, bottom:top+height,left:0,right:500,width:500,height});
global.getComputedStyle = n => ({display:'block',visibility:'visible',overflowY:n.overflow||'visible',overflowX:'visible'});
class E {
  constructor(tag, attrs={}, text='') {Object.assign(this,{tag,attrs,text,children:[],ownerDocument:doc,clientHeight:0,offsetHeight:0,scrollHeight:0,clientTop:0});}
  add(n) {n.parentElement=this;this.children.push(n);return n;}
  getAttribute(k) {return this.attrs[k]??null;}
  get textContent() {return [this.text,...this.children.map(c=>c.textContent)].join('\n');}
  get innerText() {return this.textContent;}
  matches(selector) {return selector.split(',').some(s=>{s=s.trim();return s==='*'||s===this.tag||s==='a[href]'&&this.tag==='a'&&!!this.attrs.href||s==='[role="button"]'&&this.attrs.role==='button';});}
  querySelectorAll(s) {return this.children.flatMap(c=>[...(c.matches(s)?[c]:[]),...c.querySelectorAll(s)]);}
  querySelector(s) {return this.querySelectorAll(s)[0]||null;}
  closest(s) {for(let n=this;n;n=n.parentElement)if(n.matches(s))return n;return null;}
  contains(n) {for(;n;n=n.parentElement)if(n===this)return true;return false;}
  getBoundingClientRect(){return this.rect();}
}
function scrollProperty(n, max) {let x=0;Object.defineProperty(n,'scrollTop',{get:()=>x,set:v=>{x=Math.max(0,Math.min(max(),v));}});}
const outer=new E('div');outer.overflow=o.overflow;outer.clientHeight=clip;outer.offsetHeight=clip;outer.scrollHeight=header+client;
outer.rect=()=>box(0,clip*scale);scrollProperty(outer,()=>Math.max(0,outer.scrollHeight-clip));
const inner=outer.add(new E('section'));inner.overflow='auto';inner.clientHeight=client;inner.offsetHeight=client;
const count=names.length+(o.recommendations?3:0);inner.scrollHeight=count*rowHeight;
inner.rect=()=>box((header-outer.scrollTop)*scale,client*scale);scrollProperty(inner,()=>Math.max(0,inner.scrollHeight-client));
const cards=Array.from({length:count},(_,i)=>{const username=names[i]||`suggested_${i-names.length}`;
 const c=new E('div');c.parentElement=inner;c.rect=()=>box((header+i*rowHeight-inner.scrollTop-outer.scrollTop)*scale,rowHeight*scale);
 const a=c.add(new E('a',{href:`/${username}/`},username));a.rect=()=>box(c.rect().top+o.link_offset*scale,o.link_height*scale);
 if(o.duplicate_links){const avatar=c.add(new E('a',{href:`/${username}/`},''));avatar.rect=()=>box(c.rect().top+3*scale,Math.min(rowHeight-6,36)*scale);}
 const button=c.add(new E('button',{},'Follow'));button.rect=()=>box(c.rect().top+3*scale,18*scale);
 if(i>=names.length){const marker=c.add(new E('span',{},'Suggested for you'));marker.rect=()=>box(c.rect().top+20*scale,16*scale);}
 return c;
});
function paint(){const start=Math.max(0,Math.floor(inner.scrollTop/rowHeight)-2),end=Math.min(count,Math.ceil((inner.scrollTop+client)/rowHeight)+2);inner.children=o.virtual?cards.slice(start,end):cards;}
const project=eval('('+input.projection+')'), measure=eval('('+input.measure+')');
const frames=[];paint();
for(let step=0;step<10000;step++){
 paint();const f={...project(outer),measurement:measure(outer),inner:inner.scrollTop,outer:outer.scrollTop};frames.push(f);
 if(f.measurement.bottom||!f.measurement.valid)break;
 const script=input.advance.replace('new Set([])','new Set('+JSON.stringify(f.positioned_rows.map(r=>r.username))+')');
 f.movement=eval('('+script+')')(outer);
 if(!f.movement.moved){paint();frames.push({...project(outer),measurement:measure(outer),inner:inner.scrollTop,outer:outer.scrollTop});break;}
 if(o.resize_at===step){clip=o.resize_to;outer.clientHeight=clip;outer.offsetHeight=clip;outer.scrollTop=outer.scrollTop;}
}
process.stdout.write(JSON.stringify({frames,expected:names.length,observed:[...new Set(frames.flatMap(f=>f.hrefs))],kind:'production-js/offline-geometry'}));
