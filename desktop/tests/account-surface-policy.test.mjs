import test from 'node:test';
import assert from 'node:assert/strict';
import {surfaceRectangle} from '../../dist-electron/account-surface-policy.js';
test('the native pane stays inside the content area at current zoom',()=>{
 assert.deepEqual(surfaceRectangle({id:'one',visible:true,bounds:{x:250,y:150,width:900,height:600}},{width:1000,height:700}),{x:250,y:150,width:750,height:550});
 assert.deepEqual(surfaceRectangle({id:'one',visible:true,bounds:{x:100,y:50,width:300,height:200}},{width:1600,height:900},1.5),{x:150,y:75,width:450,height:300});
});
test('hide never requires or reuses stale bounds',()=>assert.equal(surfaceRectangle({visible:false},{width:1000,height:800}),null));
test('offscreen panes are hidden and malformed geometry is rejected',()=>{
 assert.equal(surfaceRectangle({id:'a',visible:true,bounds:{x:1200,y:1200,width:200,height:200}},{width:1000,height:800}),null);
 for(const input of [null,{visible:'yes'},{id:'a',visible:true,bounds:{x:NaN,y:0,width:100,height:100}},{id:'a',visible:true,bounds:{x:0,y:0,width:-10,height:100}}])assert.throws(()=>surfaceRectangle(input,{width:1000,height:800}));
});
