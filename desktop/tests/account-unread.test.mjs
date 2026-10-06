import test from 'node:test';import assert from 'node:assert/strict';
import {AccountUnreadCache,chooseWhatsAppUnread,unreadPageScript} from '../../dist-electron/account-unread.js';
import {accountStoragePermission} from '../../dist-electron/account-permissions.js';
test('read decreases, zero, closed/stale values and owner isolation',()=>{
 const c=new AccountUnreadCache(),opened=new Set(['p']);
 c.observe('a','p',{status:'live',count:12,platform:'instagram'});
 assert.equal(c.snapshot('a',opened).p.count,12);assert.deepEqual(c.snapshot('b',opened),{});
 c.observe('a','p',{status:'live',count:1,platform:'instagram'});assert.equal(c.snapshot('a',opened).p.count,1);
 c.observe('a','p',{status:'unavailable'});assert.equal(c.snapshot('a',opened).p.status,'stale');
 assert.equal(c.snapshot('a',new Set()).p.status,'closed');
 c.observe('a','p',{status:'live',count:0,platform:'instagram'});assert.equal(c.snapshot('a',opened).p.count,0);
 c.observe('a','p',{status:'live',count:99,capped:true,platform:'instagram'});assert.equal(c.snapshot('a',opened).p.capped,true);
 c.observe('a','p',{status:'signed_out',platform:'instagram'});assert.equal(c.snapshot('a',opened).p.count,null);
 c.invalidate('p');assert.deepEqual(c.snapshot('a',opened),{});
});
test('only same-origin HTTPS storage; notifications and device requests denied',()=>{
 for(const origin of ['https://web.whatsapp.com/','https://web.telegram.org/k/','https://example.com/'])assert.equal(accountStoragePermission('persistent-storage',origin,origin),true);
 for(const permission of ['notifications','media','unknown','fileSystem','openExternal'])assert.equal(accountStoragePermission(permission,'https://web.whatsapp.com','https://web.whatsapp.com'),false);
 for(const [request,top] of [['https://evil.test','https://web.whatsapp.com'],['file:///tmp/x','file:///tmp/x'],['http://example.com','http://example.com']])assert.equal(accountStoragePermission('storage-access',request,top),false);
});

test('WhatsApp navigation establishes conversation coverage, separately from message totals',()=>{
 const n=count=>({count,capped:false});
 assert.deepEqual(chooseWhatsAppUnread([n(1)],[n(0),n(3)],n(3)),n(1));
 assert.deepEqual(chooseWhatsAppUnread([n(1),n(1)],[n(1)],n(1)),n(1));
 assert.deepEqual(chooseWhatsAppUnread([],[n(0),n(4),n(4)],null),n(4));
 assert.deepEqual(chooseWhatsAppUnread([],[n(0)],n(5)),n(0));
});
test('WhatsApp distinguishes known zero, uncertain badges, missing controls and conflicting signals',()=>{
 const n=count=>({count,capped:false});
 assert.deepEqual(chooseWhatsAppUnread([n(0)],[n(3)],n(3)),n(0));
 assert.deepEqual(chooseWhatsAppUnread([],[n(0)],null),n(0));
 assert.equal(chooseWhatsAppUnread([],[],null),null);
 assert.equal(chooseWhatsAppUnread([],[n(0)],null,true),null);
 assert.equal(chooseWhatsAppUnread([n(1),n(2)],[n(1)],null),null);
 assert.equal(chooseWhatsAppUnread([],[n(1),n(2)],null),null);
 assert.deepEqual(chooseWhatsAppUnread([{count:99,capped:true}],[],null),{count:99,capped:true});
 assert.doesNotThrow(()=>new Function(unreadPageScript),'isolated page script includes executable helper source');
});

import {sumWhatsAppMessages,whatsAppMessageCounts} from '../../dist-electron/whatsapp-unread-page.js';
import {runInNewContext} from 'node:vm';
test('two unread conversations with one and two messages produce three, then decrease to two and zero',()=>{
 const n=count=>({count,capped:false});
 assert.deepEqual(sumWhatsAppMessages([n(1),n(2)],n(2)),n(3));
 assert.deepEqual(sumWhatsAppMessages([n(2)],n(1)),n(2));
 assert.deepEqual(sumWhatsAppMessages([],n(0)),n(0));
 assert.deepEqual(sumWhatsAppMessages([n(123)],n(1)),n(123));
 assert.deepEqual(sumWhatsAppMessages([n(3)],n(2)),{count:3,capped:true});
 assert.equal(sumWhatsAppMessages([],n(2)),null,'a conversation count must not masquerade as a message count');
});

test('production unread script sums each row once, excluding navigation, calls, time and contact numbers',()=>{
 const row=()=>({});const a=row(),b=row();
 const node=(owner,label,text=label,marker='')=>({textContent:text,children:[],getClientRects:()=>[{}],getAttribute:k=>k==='aria-label'?label:k==='data-testid'?marker:null,closest:()=>owner});
 let badges=[node(a,'1 unread message','1'),node(a,'','1','unread-count'),node(b,'2 条未读消息','2'),node(b,'','2','unread-count'),node(b,'09:23'),node(a,'Contact 888')];
 const root={contains:r=>r===a||r===b,querySelectorAll:()=>badges};
 const control=label=>({textContent:label,children:[],getClientRects:()=>[{}],getAttribute:k=>k==='aria-label'?label:null,closest:()=>null,querySelector:()=>null,querySelectorAll:()=>[]});
 const context={location:{hostname:'web.whatsapp.com',pathname:'/',protocol:'https:'},getComputedStyle:()=>({visibility:'visible',display:'block'}),document:{title:'(2) WhatsApp',querySelector:()=>null,querySelectorAll:s=>s==='#pane-side,[data-testid="chat-list"]'?[root,root]:[control('Unread 2'),control('Calls 1')]}};
 let value=runInNewContext(unreadPageScript,context);assert.equal(value.count,3);assert.equal(value.capped,false);
 badges=[node(a,'1 unread message','1'),node(b,'123 unread messages','99+')];value=runInNewContext(unreadPageScript,context);assert.equal(value.count,124);assert.equal(value.capped,false,'exact accessible counts are not truncated');
 badges=[node(a,'1 unread message','1'),node(a,'3 unread messages','3')];value=runInNewContext(unreadPageScript,context);assert.equal(value.count,null,'conflicting copies of one row do not invent a total');
});
