import test from 'node:test';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
import {findWhatsAppColumns,findWhatsAppDrawers} from '../../dist-electron/whatsapp-columns.js';

// Geometry-only interface doubles. No browser, rendering engine or live site.
function fixture(){
 class Element{
  constructor(left,width,top=0,height=760){this.rect={left,right:left+width,width,top,bottom:top+height,height};this.children=[];this.parentElement=null;this.isConnected=true;this.css={display:'block',visibility:'visible',position:'static',flexDirection:'row'};this.attrs={};}
  add(n){this.children.push(n);n.parentElement=this;return n}
  getBoundingClientRect(){return this.rect}
  getAttribute(name){return this.attrs[name]??null}
 }
 const body=new Element(0,1150),app=body.add(new Element(0,1150));app.css.display='flex';
 const nav=app.add(new Element(0,64)),left=app.add(new Element(64,460)),right=app.add(new Element(524,626));
 const anchors={'#app':app};
 const scope={document:{body,querySelector:s=>anchors[s]??null},HTMLElement:Element,innerWidth:1150,innerHeight:760,getComputedStyle:n=>n.css};
 const find=runInNewContext('('+findWhatsAppColumns.toString()+')',scope);
 const drawers=runInNewContext('('+findWhatsAppDrawers.toString()+')',scope);
 const previous={left,right,parent:app};
 return {Element,body,app,nav,left,right,anchors,find,drawers,previous};
}
test('archive and welcome with neither side nor main still locate full columns on cold start',()=>{
 const f=fixture(),found=f.find();assert.equal(found.left,f.left);assert.equal(found.right,f.right);assert.equal(found.grid,false);
});
test('archive replacing only the left column keeps the welcome pane as its anchor',()=>{
 const f=fixture(),archive=new f.Element(64,460);f.left.isConnected=false;f.app.children.splice(1,1,archive);archive.parentElement=f.app;
 delete f.anchors['#app'];const found=f.find(f.previous);assert.equal(found.left,archive);assert.equal(found.right,f.right);
});
test('replacing both columns rediscovers their structural parent without depending on text or locale',()=>{
 const f=fixture();f.left.isConnected=f.right.isConnected=false;
 const list=new f.Element(64,310),welcome=new f.Element(374,776);f.app.children=[f.nav];f.app.add(list);f.app.add(welcome);
 const found=f.find(f.previous);assert.equal(found.left,list);assert.equal(found.right,welcome);
});
test('returning to the main list and entering a chat use the actual neighbouring full columns',()=>{
 const f=fixture(),side=f.left.add(new f.Element(64,460)),main=f.right.add(new f.Element(524,626));
 f.anchors['#side,#pane-side']=side;f.anchors['#main,[data-testid="conversation-panel-wrapper"]']=main;
 assert.equal(f.find().left,f.left);assert.equal(f.find().right,f.right);
});
test('hidden, narrow, vertical and non-adjacent panels never gain a false divider',()=>{
 for(const change of [f=>f.left.css.display='none',f=>f.app.css.flexDirection='column',f=>f.right.rect.left+=100,f=>f.left.rect.width=130]){
  const f=fixture();change(f);assert.equal(f.find(),null);
 }
});
test('grid columns and nested archive shells are supported without selecting the navigation rail',()=>{
 const f=fixture();f.app.css.display='grid';const found=f.find();assert.equal(found.grid,true);assert.notEqual(found.left,f.nav);
 const outer=f.app;f.body.children=[];const shell=f.body.add(new f.Element(0,1150));shell.add(outer);delete f.anchors['#app'];
 assert.equal(f.find().left,f.left);
});
test('an absolute archive overlay follows the left column while dialogs and right-side content are excluded',()=>{
 const f=fixture();f.left.rect.width=300;f.left.rect.right=364;f.right.rect.left=364;f.right.rect.width=786;
 const archive=f.app.add(new f.Element(64,460));archive.css.position='absolute';
 const dialog=f.app.add(new f.Element(64,460));dialog.css.position='absolute';dialog.attrs.role='dialog';
 const details=f.right.add(new f.Element(364,460));details.css.position='absolute';
 assert.deepEqual([...f.drawers(f.previous,460)],[archive]);
 archive.rect.width=300;archive.rect.right=364;
 assert.deepEqual([...f.drawers(f.previous,460)],[archive]);
 assert.equal(f.find().left,f.left,'absolute drawers cannot replace the actual flex track');
});
test('short message bubbles and narrow background stripes cannot be mistaken for archive drawers',()=>{
 const f=fixture(),short=f.left.add(new f.Element(64,460,50,200)),line=f.app.add(new f.Element(64,1));
 short.css.position=line.css.position='absolute';assert.equal(f.drawers(f.previous,460).length,0);
});
