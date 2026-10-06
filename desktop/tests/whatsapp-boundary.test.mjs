import test from 'node:test';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
import {whatsappBoundaryCleanup} from '../../dist-electron/whatsapp-boundary.js';

// Geometry/CSS interface doubles: no browser, layout engine or live account.
// The Windows fixture separately checks a real full-height pseudo-border.
const defaults={display:'block',visibility:'visible',opacity:'1',position:'static',transform:'none',content:'none',
 left:'auto',right:'auto',top:'auto',bottom:'auto',width:'auto',height:'auto',boxSizing:'border-box',backgroundColor:'rgba(0, 0, 0, 0)',
 borderLeftWidth:'0px',borderRightWidth:'0px',borderTopWidth:'0px',borderBottomWidth:'0px',borderLeftStyle:'none',borderRightStyle:'none',
 borderLeftColor:'rgb(200, 200, 200)',borderRightColor:'rgb(200, 200, 200)'};
function fixture(){
 const sheets=new Set();
 class Element{
  constructor(left=0,width=1150,top=0,height=760){this.rect={left,right:left+width,width,top,bottom:top+height,height};this.children=[];this.parentElement=null;this.isConnected=true;this.style={...defaults};this.pseudos={};this.attrs=new Map();this.textContent='';}
  getBoundingClientRect(){return this.rect}
  getAttribute(name){return this.attrs.get(name)??null}
  hasAttribute(name){return this.attrs.has(name)}
  setAttribute(name,value){this.attrs.set(name,value)}
  removeAttribute(name){this.attrs.delete(name)}
  add(child){this.children.push(child);child.parentElement=this;return child}
 }
 const body=new Element(),parent=body.add(new Element()),left=parent.add(new Element(64,460)),right=parent.add(new Element(524,626));
 parent.style.position='relative';left.style.borderRightWidth='1px';left.style.borderRightStyle='solid';
 const native={...defaults,position:'absolute',content:'""',left:'calc(40% + 64px)',top:'0px',height:'100%',width:'0px',boxSizing:'content-box',borderLeftWidth:'1px',borderLeftStyle:'solid'};
 parent.pseudos['::before']=native;
 const document={body,head:{append(node){sheets.add(node);node.isConnected=true}},createElement(){return {textContent:'',isConnected:false,remove(){sheets.delete(this);this.isConnected=false}}}};
 const scope={document,HTMLElement:Element,innerWidth:1150,innerHeight:760,getComputedStyle:(node,pseudo)=>pseudo?(node.pseudos[pseudo]??defaults):node.style};
 const adapter=runInNewContext('('+whatsappBoundaryCleanup.toString()+')()',scope);
 const capture=()=>adapter.capture(left,right,parent);
 const resize=(width=300)=>{left.rect={...left.rect,width,right:64+width};right.rect={...right.rect,left:64+width,width:1150-64-width};adapter.sync(left.rect.right)};
 const css=()=>[...sheets].map(s=>s.textContent).join('\n');
 return {adapter,capture,resize,css,sheets,body,parent,left,right,native,Element};
}
test('only the native pseudo-border left behind at the original split is suppressed after resizing',()=>{
 const f=fixture();f.capture();f.adapter.sync(524);assert.equal(f.css(),'');
 f.resize();assert.match(f.css(),/::before\{border-left-color:transparent!important\}/);
 assert.equal(f.left.getAttribute('data-juxin-wa-native-boundary'),null,'moving contact border is retained');
 assert.equal(f.native.borderLeftWidth,'1px','native box dimensions remain unchanged');
 assert.doesNotMatch(f.css(),/display|width|visibility/);
});
test('reset restores the native boundary and pause removes all owned styles and attributes',()=>{
 const f=fixture();f.parent.setAttribute('data-juxin-wa-native-boundary','prior-value');f.capture();f.resize();
 assert.notEqual(f.css(),'');f.resize(460);assert.equal(f.css(),'');
 f.resize();f.adapter.clear();assert.equal(f.css(),'');
 assert.equal(f.parent.getAttribute('data-juxin-wa-native-boundary'),'prior-value');
});
test('full-height empty strokes are handled while message, content and unrelated borders stay unchanged',()=>{
 const f=fixture(),line=f.parent.add(new f.Element(524,1));line.style.backgroundColor='rgb(200, 200, 200)';
 const short=f.parent.add(new f.Element(524,1,120,100));short.style.backgroundColor='red';
 const unrelated=f.parent.add(new f.Element(1000,1));unrelated.style.backgroundColor='red';
 const content=f.parent.add(new f.Element(524,1));content.textContent='content';content.style.backgroundColor='red';
 f.capture();f.resize();assert.ok(line.hasAttribute('data-juxin-wa-native-boundary'));assert.match(f.css(),/background-color:transparent/);
 for(const node of [short,unrelated,content])assert.equal(node.hasAttribute('data-juxin-wa-native-boundary'),false);
});
test('native decorations that follow the divider remain visible and unsupported transforms are untouched',()=>{
 const f=fixture();f.native.transform='translateX(10px)';f.capture();f.resize();assert.equal(f.css(),'');
 f.native.transform='none';f.resize(460);f.capture();f.native.left='364px';f.resize();assert.equal(f.css(),'');
 f.native.left='524px';f.adapter.sync(364);assert.notEqual(f.css(),'');
 f.native.height='60px';f.adapter.sync(364);assert.equal(f.css(),'','a repurposed short decoration is no longer hidden');
});
test('column replacement releases the previous decorations and captures the new native split',()=>{
 const f=fixture();f.capture();f.resize();assert.notEqual(f.css(),'');
 f.parent.pseudos['::before']={...f.native,left:'364px'};f.capture();assert.equal(f.css(),'');
 assert.equal(f.parent.hasAttribute('data-juxin-wa-native-boundary'),false);
 f.resize(220);assert.match(f.css(),/border-left-color:transparent/);
 // A concurrent site change to the attribute must not be overwritten.
 f.parent.setAttribute('data-juxin-wa-native-boundary','site-change');f.adapter.clear();
 assert.equal(f.parent.getAttribute('data-juxin-wa-native-boundary'),'site-change');
});
test('responsive percentage decorations are remeasured without scanning newly added message rows',()=>{
 const f=fixture();f.capture();f.resize();assert.notEqual(f.css(),'');
 const row=f.parent.add(new f.Element(364,1));row.style.backgroundColor='red';
 f.parent.rect={...f.parent.rect,width:900,right:900};f.native.left='calc(33.333333% + 64px)';
 f.adapter.sync(364);assert.equal(f.css(),'');assert.equal(row.hasAttribute('data-juxin-wa-native-boundary'),false);
});

test('a late-mounted native line is discovered at its original position after the split has already moved',()=>{
 const f=fixture();delete f.parent.pseudos['::before'];f.capture();f.resize();assert.equal(f.css(),'');
 f.parent.pseudos['::before']=f.native;f.adapter.rescan();f.adapter.sync(364);assert.match(f.css(),/border-left-color:transparent/);
});
test('full-height nested structural decorations are handled without touching nested short message borders',()=>{
 const f=fixture();delete f.parent.pseudos['::before'];
 const layer=f.parent.add(new f.Element()),nested=layer.add(new f.Element());nested.pseudos['::after']={...f.native};
 const row=nested.add(new f.Element(524,100,100,70));row.style.borderLeftWidth='1px';row.style.borderLeftStyle='solid';
 f.capture();f.resize();assert.match(f.css(),/::after\{border-left-color/);assert.equal(row.hasAttribute('data-juxin-wa-native-boundary'),false);
});
test('translated thin pseudo paint at the original split is removed, while rotation is left alone',()=>{
 const f=fixture();f.native.left='525px';f.native.transform='matrix(1, 0, 0, 1, -1, 0)';f.capture();f.resize();assert.match(f.css(),/border-left-color/);
 f.adapter.clear();f.native.transform='matrix(0, 1, -1, 0, 0, 0)';f.capture();f.adapter.sync(364);assert.equal(f.css(),'');
});
test('one-pixel gradient paint can be cleared without covering the welcome pane',()=>{
 const f=fixture();f.native.width='1px';f.native.borderLeftWidth='0px';f.native.backgroundImage='linear-gradient(red, red)';f.capture();f.resize();
 assert.match(f.css(),/background-image:none!important/);assert.doesNotMatch(f.css(),/background-color|position|width|opacity|display/);
});
test('hard vertical boundary shadows are cleared while blurred and multi-layer shadows are preserved',()=>{
 for(const [shadow,expected] of [['rgb(100, 100, 100) 1px 0px 0px 0px inset',true],['rgb(100, 100, 100) 1px 0px 4px 0px inset',false],['rgb(100, 100, 100) 1px 0px, rgb(0, 0, 0) 2px 2px',false]]){
  const f=fixture();delete f.parent.pseudos['::before'];const nativePane=f.parent.add(new f.Element(524,626));nativePane.style.boxShadow=shadow;
  f.capture();f.resize();assert.equal(/box-shadow:none!important/.test(f.css()),expected);
 }
});
test('repeated rescans neither duplicate owned rules nor hide content that repurposes a paint node',()=>{
 const f=fixture(),line=f.parent.add(new f.Element(524,1));line.style.backgroundColor='rgb(120, 120, 120)';f.capture();f.resize();const css=f.css();
 for(let i=0;i<5;i++){f.adapter.rescan();f.adapter.sync(364);}assert.equal(f.css(),css);
 line.textContent='new content';f.adapter.sync(364);assert.doesNotMatch(f.css(),/background-color:transparent/);
});
test('empty auto-width pseudo border with margin offsets is measured at the actual native edge',()=>{
 const f=fixture();f.native.width='auto';f.native.left='523px';f.native.marginLeft='1px';f.capture();f.resize();assert.match(f.css(),/border-left-color:transparent/);
});
test('returning to the native split restores late decorations, and disabling releases all marks',()=>{
 const f=fixture();delete f.parent.pseudos['::before'];f.capture();f.resize();f.parent.pseudos['::before']=f.native;f.adapter.rescan();f.adapter.sync(364);
 assert.notEqual(f.css(),'');f.resize(460);assert.equal(f.css(),'');f.adapter.clear();assert.equal(f.parent.hasAttribute('data-juxin-wa-native-boundary'),false);
});
