import test from 'node:test';
import assert from 'node:assert/strict';
import {accountViewport} from '../../dist-electron/account-viewport.js';
import {webPageMenu} from '../../dist-electron/web-page-menu.js';
test('collection coordinates stay fixed through route changes, previewing and sidebar resizing',()=>{
 const initial={x:0,y:0,width:1280,height:900};
 for(const workspace of [{width:950,height:700},{width:240,height:160},{width:1800,height:1000}]){
   assert.deepEqual(accountViewport(true,true,initial,workspace),initial);
   assert.deepEqual(accountViewport(true,false,initial,workspace),initial);
   assert.deepEqual(accountViewport(false,false,initial,workspace),initial);
   assert.deepEqual(accountViewport(false,true,initial,workspace),{x:0,y:0,...workspace});
 }
});
const params={selectionText:'原文与译文',isEditable:false,editFlags:{},linkURL:'https://instagram.com/example/',srcURL:'',mediaType:'none',x:10,y:20};
test('selection and link copy are separate; an acquired task fence makes every old action inert',()=>{
 let active=true;const actions=[];
 const rows=webPageMenu(params,(...args)=>actions.push(args),()=>active);
 rows.find(r=>r.label==='复制选中文字').click();
 rows.find(r=>r.label==='复制链接地址').click();
 assert.deepEqual(actions,[['copy-text','原文与译文'],['copy-text',params.linkURL]]);
 active=false;for(const row of rows)row.click?.();assert.equal(actions.length,2);
});
test('editable menu honors native edit flags and rejects non-web link/download schemes',()=>{
 const rows=webPageMenu({...params,selectionText:'',isEditable:true,editFlags:{canPaste:true,canCut:false},linkURL:'file:///secret',mediaType:'image',srcURL:'javascript:alert(1)'},()=>{},()=>true);
 assert.equal(rows.find(r=>r.label==='粘贴').enabled,true);
 assert.equal(rows.find(r=>r.label==='剪切').enabled,false);
 assert.ok(!rows.some(r=>r.label==='复制链接地址'||r.label==='图片另存为…'));
 assert.ok(rows.some(r=>r.label==='复制图片'));
});
