import test from 'node:test';
import assert from 'node:assert/strict';
import {sendWindowEvent} from '../../dist-electron/window-events.js';
test('floating-window close notifications skip a destroyed renderer and handle native destruction racing send',()=>{
 let sent=[];const wc={isDestroyed:()=>false,send:(...args)=>sent.push(args)},win={isDestroyed:()=>false,webContents:wc};
 assert.equal(sendWindowEvent(win,'translator:visibility',false),true);assert.deepEqual(sent,[['translator:visibility',false]]);
 wc.isDestroyed=()=>true;assert.equal(sendWindowEvent(win,'translator:visibility',false),false);assert.equal(sent.length,1);
 wc.isDestroyed=()=>false;wc.send=()=>{throw new TypeError('Object has been destroyed')};assert.equal(sendWindowEvent(win,'chatgpt:visibility',false),false);
 wc.send=()=>{throw new Error('unexpected error')};assert.throws(()=>sendWindowEvent(win,'chatgpt:visibility',false),/unexpected error/);
 assert.equal(sendWindowEvent(null,'translator:visibility',false),false);
});
test('close notifications skip a missing renderer even while the native window is not marked destroyed',()=>{
 for(const webContents of [null,undefined]){
  assert.equal(sendWindowEvent({isDestroyed:()=>false,webContents},'chatgpt:visibility',false),false);
 }
 const closed={isDestroyed:()=>true,get webContents(){throw new Error('must not inspect a destroyed window')}};
 assert.equal(sendWindowEvent(closed,'chatgpt:visibility',false),false);
});
test('close notifications send through the checked renderer without reading a disappearing property again',()=>{
 let reads=0;const sent=[];
 const contents={isDestroyed:()=>false,send:(...args)=>sent.push(args)};
 const win={isDestroyed:()=>false,get webContents(){return ++reads===1?contents:null}};
 assert.equal(sendWindowEvent(win,'chatgpt:visibility',false),true);
 assert.equal(reads,1);assert.deepEqual(sent,[['chatgpt:visibility',false]]);
});
