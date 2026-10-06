import test from 'node:test';import assert from 'node:assert/strict';import {runInNewContext} from 'node:vm';
import {messageActivityPage} from '../../dist-electron/message-activity-page.js';
import {unreadPageScript} from '../../dist-electron/account-unread.js';

// DOM observations are modelled here; native selector/layout cases also run in
// the Electron integration gate when building Windows.
test('incoming tail observations ignore initial history, prepends, scrollback and return, but detect a new appended incoming ID',()=>{
 let ids=['1','2'],conversation='chat-a';
 const main={querySelector:()=>({textContent:conversation}),querySelectorAll:()=>ids.map(id=>({closest:()=>({getAttribute:()=>id})}))};
 const context={location:{hostname:'web.whatsapp.com'},crypto:{randomUUID:()=> 'document'},document:{querySelector:()=>main,querySelectorAll:()=>[]}};
 const read=()=>runInNewContext('('+messageActivityPage.toString()+')()',context);
 assert.equal(read().serial,0);ids=['0','1','2'];assert.equal(read().serial,0);
 ids=['0','1'];assert.equal(read().serial,0);ids=['1','2'];assert.equal(read().serial,0);
 ids=['1','2','3'];assert.equal(read().serial,1);assert.equal(read().serial,1);
 conversation='chat-b';ids=['one','two'];assert.equal(read().serial,1);
 assert.deepEqual(Object.keys(read()).sort(),['epoch','serial'],'no contact, message text or message ID leaves the page');
});

test('same unread chat preview changing is an arrival; outgoing preview and unrelated rows do not signal',()=>{
 let preview='First message',outgoing=false;
 const row={getClientRects:()=>[{}],querySelector:s=>s==='[title]'?{getAttribute:()=> 'Contact'}:outgoing?{}:null,querySelectorAll:s=>s==='[aria-label],[data-testid]'?[{getAttribute:k=>k==='aria-label'?'1 unread message':''}]:[{querySelector:()=>null,innerText:preview}]};
 const context={location:{hostname:'web.whatsapp.com'},crypto:{randomUUID:()=> 'document'},document:{querySelector:()=>null,querySelectorAll:()=>[row]}};
 const read=()=>runInNewContext('('+messageActivityPage.toString()+')()',context);
 assert.equal(read().serial,0);preview='Second message';assert.equal(read().serial,1);assert.equal(read().serial,1);
 outgoing=true;preview='My reply';assert.equal(read().serial,1);
});

test('production unread parser handles a numeric child under an Unread label and clears a stale title on zero',()=>{
 let content='Unread 2';
 const control={textContent:content,getClientRects:()=>[{}],closest:()=>null,querySelector:()=>null,querySelectorAll:()=>[],getAttribute:k=>k==='aria-label'?'Unread':''};
 const context={location:{hostname:'web.whatsapp.com',protocol:'https:',pathname:'/'},getComputedStyle:()=>({visibility:'visible',display:'block'}),document:{title:'(9) WhatsApp',querySelector:()=>null,querySelectorAll:s=>s==='#pane-side,[data-testid="chat-list"]'?[]:[control]}};
 const read=()=>runInNewContext(unreadPageScript,context);
 assert.equal(read().count,null,'conversation count alone is not a message total');control.textContent='Unread';assert.equal(read().count,0,'the stale page title must not keep a red 9 badge');
});
