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
      if(selector==='video')return e.tagName==='VIDEO';
      if(selector==='svg[aria-label]')return e.tagName==='SVG'&&e.getAttribute('aria-label');
      if(selector==='button,[role="button"]')return e.tagName==='BUTTON'||e.getAttribute('role')==='button';
      if(selector==='a[href]')return e.tagName==='A'&&e.href;
      if(selector.startsWith('[data-collector-reel]'))return ['data-collector-reel','data-collector-reel-heart','data-collector-reel-video'].some(k=>e.getAttribute(k)!==null);
      throw new Error('Unexpected selector '+selector);
    });
  }
}
function probe({link='/reel/A/', url='https://www.instagram.com/reels/', heart='Like', sourceUrl='blob:video-A', left=0}={}){
  const video=new Element('VIDEO');Object.assign(video,{paused:false,readyState:4,videoWidth:310,currentSrc:sourceUrl,currentTime:3});video.rect.left=left;video.rect.right=left+300;
  const children=[video];if(heart)children.push(new Element('BUTTON',{'aria-label':heart}));
  if(link){const a=new Element('A');a.href=new URL(link,url).href;children.push(a)}
  const article=new Element('ARTICLE',{},children);global.document=new Element('BODY',{},[new Element('MAIN',{},[article])]);
  global.location={href:url};global.innerWidth=1000;global.innerHeight=700;global.getComputedStyle=()=>({visibility:'visible'});
  return Function('return ('+source+')()')();
}
assert.equal(probe().key,'/reel/A');assert.equal(probe().can_like,true);
assert.equal(probe({link:'/reels/B/'}).key,'/reel/B');
const route=probe({link:null,url:'https://www.instagram.com/reels/C/'});assert.equal(route.key,'/reel/C');assert.equal(route.can_like,false);
const media=probe({link:null,heart:null});assert.equal(media.key,'media:blob:video-A');assert.equal(media.can_like,false);
assert.equal(probe({heart:'Heart'}).can_like,false);
assert.equal(probe({left:1500}),null);
assert.equal(probe({heart:'Unlike'}).liked,true);assert.equal(probe({heart:'Unlike'}).can_like,false);
console.log(JSON.stringify({passed:7,synthetic_dom:true,native_browser:false}));
