import test from 'node:test';import assert from 'node:assert/strict';
import {MessageReminders} from '../../dist-electron/message-reminders.js';
test('new unread increases notify once, initial/stale/read/disabled counts never notify',()=>{
 const sent=[],saved=[],r=new MessageReminders(true,(id,count)=>sent.push([id,count]),v=>saved.push(v));
 const observe=(id,count,status='live')=>r.observe(id,{count,status});
 observe('a',7);observe('a',7);observe('a',8);observe('a',8);observe('b',22);observe('a',2);observe('a',3);
 observe('a',99,'stale');observe('a',null,'unavailable');observe('a',3);
 assert.deepEqual(sent,[['a',8],['a',3]]);
 r.setEnabled(false);observe('a',33);r.setEnabled(true);observe('a',35);observe('a',36);
 assert.deepEqual(saved,[false,true]);assert.deepEqual(sent.at(-1),['a',36]);assert.equal(sent.length,3);
 r.reset();observe('a',36);r.forget('a');observe('a',38);observe('a',null,'signed_out');observe('a',50);
 assert.equal(sent.length,3);
});
test('failed persistence does not change the current setting',()=>{
 const r=new MessageReminders(true,()=>{},()=>{throw new Error('disk full')});assert.throws(()=>r.setEnabled(false));assert.equal(r.enabled,true);
});

test('same-count new arrivals notify once, including immediately-read arrivals; navigation and stale observations do not',()=>{
 const sent=[],r=new MessageReminders(true,(id,count)=>sent.push([id,count]),()=>{});
 const observe=(count,serial,epoch='document',status='live')=>r.observe('a',{count,status,activity:{epoch,serial}});
 observe(1,0);observe(1,1);observe(1,1);assert.deepEqual(sent,[['a',1]]);
 observe(0,1);observe(0,2);assert.deepEqual(sent.at(-1),['a',0]);
 observe(4,0,'reloaded');assert.equal(sent.length,2,'reload establishes a fresh baseline');
 observe(4,20,'reloaded','stale');assert.equal(sent.length,2);
 observe(5,1,'reloaded');assert.equal(sent.length,3,'count and activity increases produce one toast together');
 r.setEnabled(false);observe(5,2,'reloaded');assert.equal(sent.length,3);
});

test('loading more rows of a partial inbox does not cause a new-message notification',()=>{
 const sent=[],r=new MessageReminders(true,(id,count)=>sent.push([id,count]),()=>{});
 r.observe('a',{status:'live',count:2,capped:true});r.observe('a',{status:'live',count:3,capped:false});assert.equal(sent.length,0);
 r.observe('a',{status:'live',count:4,capped:false});assert.deepEqual(sent,[['a',4]]);
});
