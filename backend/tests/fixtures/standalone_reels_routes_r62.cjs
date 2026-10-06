// Minimal synthetic DOM unit fixture. Does not claim native browser coverage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(0, 'utf8');
class Element {
  constructor(tag, attrs={}, children=[]) { this.tagName=tag; this.attrs={...attrs}; this.children=children; children.forEach(c=>c.parentElement=this); this.rect={left:0,right:300,top:0,bottom:500,width:300,height:500}; }
  getAttribute(k){return this.attrs[k] ?? null;}
  setAttribute(k,v){this.attrs[k]=v;}
  removeAttribute(k){delete this.attrs[k];}
  getBoundingClientRect(){return this.rect;}
  getClientRects(){return [this.rect];}
  closest(){return null;}
  querySelectorAll(selector){
    const all=[]; const visit=e=>{for(const c of e.children){all.push(c);visit(c)}};visit(this);
    return all.filter(e=>{
      if(selector==='main video'||selector==='video')return e.tagName==='VIDEO';
      if(selector==='svg[aria-label]')return e.tagName==='SVG'&&e.getAttribute('aria-label');
      if(selector==='button,[role="button"]')return e.tagName==='BUTTON'||e.getAttribute('role')==='button';
      if(selector==='a[href]')return e.tagName==='A'&&e.href;
      if(selector==='[data-standalone-video]')return e.getAttribute('data-standalone-video')!==null;
      if(selector.startsWith('[data-collector-reel]'))return ['data-collector-reel','data-collector-reel-heart','data-collector-reel-video'].some(k=>e.getAttribute(k)!==null);
      throw new Error('Unexpected selector '+selector);
    });
  }
}
function probe({link='/reel/A/', extraLinks=[], url='https://www.instagram.com/reels/', heart='Like', sourceUrl='blob:video-A', left=0}={}){
  const video=new Element('VIDEO');Object.assign(video,{paused:false,readyState:4,videoWidth:310,currentSrc:sourceUrl,currentTime:3});video.rect.left=left;video.rect.right=left+300;
  const children=[video];if(heart)children.push(new Element('BUTTON',{'aria-label':heart}));
  for(const href of [link,...extraLinks].filter(Boolean)){const a=new Element('A');a.href=new URL(href,url).href;children.push(a)}
  const article=new Element('ARTICLE',{},children);global.document=new Element('BODY',{},[new Element('MAIN',{},[article])]);
  global.location={href:url};global.innerWidth=1000;global.innerHeight=700;global.getComputedStyle=()=>({visibility:'visible'});
  return Function('return ('+source+')()')();
}

let passed=0;
for(const path of ['/reel/A','/reel/A/','/reels/A','/reels/A/','/reels/A/?igsh=fixture','https://instagram.com/reels/A/']){
  const value=probe({link:path,url:'https://www.instagram.com/reels/A/'});
  assert.equal(value?.key,'/reel/A');assert.equal(value.state,'unliked');passed++;
}
assert.equal(probe({link:'/reels/A/',extraLinks:['/reel/A/?x=1']}).key,'/reel/A');passed++;
for(const link of ['/p/A/','https://evil.example/reels/A/','http://instagram.com/reels/A/','/reels/A/comments/']){assert.equal(probe({link}),null);passed++}
assert.equal(probe({link:'/reels/A/',extraLinks:['/reel/B/']}),null);passed++;
assert.equal(probe({link:'/reels/A/',heart:'Unlike'}).state,'liked');passed++;
assert.equal(probe({link:'/reels/A/',heart:'Heart'}),null);passed++;
assert.equal(probe({link:'/reels/A/',left:1500}),null);passed++;
console.log(JSON.stringify({passed,synthetic_dom:true,native_browser:false}));
