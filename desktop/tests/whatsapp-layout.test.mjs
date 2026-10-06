import test from 'node:test';
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
import {whatsappColumnLayoutScript} from '../../dist-electron/whatsapp-layout.js';

// Policy/lifecycle doubles only. Native pointer drag remains covered by the
// Windows WhatsApp fixture; these tests never start or emulate a browser.
function fixture(url){
 const nodes=new Set(),calls={created:[],storage:0,resize:0,mutations:0,frames:0};
 const document={head:{append(node){nodes.add(node);node.isConnected=true}},body:{},documentElement:{},addEventListener(){},
  createElement(tag){calls.created.push(tag);return {textContent:'',isConnected:false,remove(){nodes.delete(this);this.isConnected=false}}}};
 const scope={document,location:new URL(url),
  localStorage:{getItem(){calls.storage++;return '0.8'}},
  ResizeObserver:class{constructor(){calls.resize++}observe(){}disconnect(){}},
  MutationObserver:class{constructor(){calls.mutations++}observe(){}},
  requestAnimationFrame(){calls.frames++;return calls.frames},cancelAnimationFrame(){},addEventListener(){},
  setTimeout,clearTimeout};
 const run=()=>runInNewContext(whatsappColumnLayoutScript(),scope);
 return {run,scope,nodes,calls};
}
test('Instagram keeps text selection without creating a divider, reading its old ratio or observing layout',()=>{
 for(const url of ['https://www.instagram.com/direct/t/fixture/','https://instagram.com/direct/inbox/']){
  const f=fixture(url);assert.equal(f.run(),true);
  assert.deepEqual(f.calls,{created:['style'],storage:0,resize:0,mutations:0,frames:0});
  assert.equal(f.nodes.size,1);assert.match([...f.nodes][0].textContent,/user-select:text!important/);
 }
});
test('Instagram task pause/resume and repeated installation reuse only the text selection style',()=>{
 const f=fixture('https://www.instagram.com/direct/inbox/');f.run();f.run();
 assert.equal(f.nodes.size,1);assert.equal(f.calls.created.length,1);
 f.scope.__juxinChatColumnsV2.setEnabled(false);assert.equal(f.nodes.size,0);
 f.scope.__juxinChatColumnsV2.setEnabled(true);f.scope.__juxinChatColumnsV2.refresh();f.run();
 assert.equal(f.nodes.size,1);assert.equal(f.calls.resize,0);assert.equal(f.calls.frames,0);assert.equal(f.calls.storage,0);
});
test('WhatsApp continues to initialize its saved ratio and resize lifecycle exactly once',()=>{
 const f=fixture('https://web.whatsapp.com/');assert.equal(f.run(),true);
 assert.deepEqual(f.calls,{created:['style'],storage:1,resize:1,mutations:1,frames:1});
 f.run();assert.equal(f.calls.storage,1);assert.equal(f.calls.resize,1);assert.equal(f.calls.mutations,1);assert.equal(f.nodes.size,1);
});
test('unrelated and lookalike sites receive neither selection styles nor a divider',()=>{
 for(const url of ['https://www.instagram.com.evil.test/','https://web.whatsapp.com.evil.test/','https://example.com/']){
  const f=fixture(url);assert.equal(f.run(),false);assert.equal(f.nodes.size,0);assert.equal(f.scope.__juxinChatColumnsV2,undefined);
 }
});
