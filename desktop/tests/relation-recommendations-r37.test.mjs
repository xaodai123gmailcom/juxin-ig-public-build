// Execute the production relationship projection against a controlled DOM tree.
// Layout/visibility are interface doubles; this does not launch Chromium.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
const source = readFileSync(new URL('../../backend/app/collection_surface.py', import.meta.url), 'utf8');
// Production composes this projection from shared raw-string constants. Resolve
// that restricted Python string expression, including helpers, rather than
// silently executing only the first triple-quoted fragment (`root => {`).
function stringConstant(name, resolving=new Set()) {
  assert.match(name, /^[A-Z_]+$/);
  assert.ok(!resolving.has(name), `cyclic production constant: ${name}`);
  const assignment = new RegExp(`^${name} = `, 'm').exec(source);
  assert.ok(assignment, `missing production constant: ${name}`);
  const active = new Set([...resolving, name]);
  let cursor = assignment.index + assignment[0].length, value = '';
  while (true) {
    while (/\s/.test(source[cursor] || '') && cursor < source.length) cursor++;
    if (source.startsWith('r"""', cursor)) {
      const end = source.indexOf('"""', cursor + 4);
      assert.ok(end >= 0, `unterminated production string: ${name}`);
      value += source.slice(cursor + 4, end);
      cursor = end + 3;
    } else {
      const reference = /^[A-Z_]+/.exec(source.slice(cursor));
      assert.ok(reference, `unsupported production string expression: ${name}`);
      value += stringConstant(reference[0], active);
      cursor += reference[0].length;
    }
    while (/\s/.test(source[cursor] || '') && cursor < source.length) cursor++;
    if (source[cursor] !== '+') break;
    cursor++;
  }
  return value;
}
const script = stringConstant('RELATION_ROWS_SCRIPT');
class Element {
  constructor(tag, text='', attrs={}) { this.tag=tag; this.text=text; this.attrs=attrs; this.children=[]; this.parentElement=null; this.style={display:'block',visibility:'visible'}; }
  append(...children) { for(const child of children) { child.parentElement=this; this.children.push(child); } return this; }
  get innerText() { return [this.text,...this.children.map(child=>child.innerText)].join(' ').trim(); }
  get textContent() { return this.innerText; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  contains(node) { return node===this || this.children.some(child=>child.contains(node)); }
  matches(selector) { return selector.split(',').some(part=> { part=part.trim(); return part==='*' || part==='a[href]' && this.tag==='a' && Boolean(this.attrs.href) || ['button','img','input'].includes(part) && this.tag===part || part==='[role="button"]' && this.attrs.role==='button'; }); }
  querySelectorAll(selector) { return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) ?? null; }
  getBoundingClientRect() { let current=this; while(current) { if(current.style.display==='none') return {width:0,height:0}; current=current.parentElement; } return {width:300,height:40}; }
}
const node = (tag,text='',attrs={}) => new Element(tag,text,attrs);
const link = name => node('a',name,{href:`/${name}/`});
function row(name, label='', {insideLink=false, hiddenLabel=false}={}) {
  const labelNode=node('span',label); if(hiddenLabel) labelNode.style.display='none';
  const accountLink=link(name); if(insideLink) accountLink.append(labelNode);
  return node('div').append(accountLink,...(insideLink?[]:[labelNode]),node('button','Follow'));
}
const project=runInNewContext(`(${script})`,{URL,getComputedStyle:el=>el.style});
const read=root=>{
  const {hrefs, recommendations_reached, diagnostics} = JSON.parse(JSON.stringify(project(root)));
  assert.ok(diagnostics, 'production projection includes bounded count diagnostics');
  for (const key of ['profile_links','visible_profile_links','context_links','candidate_accounts','recommended_accounts']) {
    assert.ok(Number.isInteger(diagnostics[key]) && diagnostics[key] >= 0, `invalid diagnostic count: ${key}`);
  }
  // These existing cases assert recommendation semantics; additional diagnostic
  // fields must not force a duplicate production projection in this fixture.
  return {hrefs, recommendations_reached};
};
const dialog=(...children)=>node('section').append(...children);

test('screenshot: per-row Suggested for you plus Chinese footer excludes every recommendation',()=>{
  const result=read(dialog(row('real_follower'),row('suggested_one','Suggested for you'),row('suggested_two','Suggested for you'),node('button','查看所有推荐用户')));
  assert.deepEqual(result,{hrefs:['/real_follower/'],recommendations_reached:true});
});
test('translated recommendation section is terminal without row metadata',()=>{
  const result=read(dialog(row('real_follower'),node('h2','为你推荐'),row('suggested_one'),row('suggested_two')));
  assert.deepEqual(result,{hrefs:['/real_follower/'],recommendations_reached:true});
});
test('one interleaved suggestion is removed without ending the remaining real list',()=>{
  const result=read(dialog(row('first'),row('suggested','Suggested for you'),row('last'),node('button','查看所有推荐用户')));
  assert.deepEqual(result,{hrefs:['/first/','/last/'],recommendations_reached:false});
});
test('a single recommended row without footer or heading cannot establish the tail',()=>{
  assert.deepEqual(read(dialog(row('real'),row('suggested','Suggested for you'))),{hrefs:['/real/'],recommendations_reached:false});
});
test('two distinct terminal recommendation cards provide a boundary',()=>{
  assert.equal(read(dialog(row('real'),row('one','Suggested for you'),row('two','Suggested for you'))).recommendations_reached,true);
});
test('duplicate avatar/name anchors of one recommendation do not become two cards',()=>{
  const suggested=row('same','Suggested for you'); suggested.append(link('same'));
  assert.deepEqual(read(dialog(row('real'),suggested)),{hrefs:['/real/'],recommendations_reached:false});
});
test('a display name inside an account link is not recommendation evidence',()=>{
  assert.deepEqual(read(dialog(row('real','Suggested for you',{insideLink:true}),row('last'))),{hrefs:['/real/','/last/'],recommendations_reached:false});
});
test('hidden labels and arbitrary substrings never establish recommendations',()=>{
  assert.deepEqual(read(dialog(row('first','Suggested for you',{hiddenLabel:true}),row('last','I dislike suggested for you'))),{hrefs:['/first/','/last/'],recommendations_reached:false});
});
test('footer by itself does not erase genuine followers',()=>{
  assert.deepEqual(read(dialog(row('first'),row('last'),node('button','查看所有推荐用户'))),{hrefs:['/first/','/last/'],recommendations_reached:false});
});
test('empty skeleton has no recommendation end evidence',()=>{
  assert.deepEqual(read(dialog(node('div','Loading…'))),{hrefs:[],recommendations_reached:false});
});
