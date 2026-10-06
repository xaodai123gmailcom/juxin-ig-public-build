import assert from 'node:assert/strict';
import test from 'node:test';
import {nurtureScheduleDefaults,waitingNurtureIds,nurtureWaitLabel,nurtureScheduleSummary} from '../src/nurture-schedule.ts';
const job=(id,profile_id,status='queued',due_at='2026-09-15T10:00:00Z')=>({id,profile_id,status,due_at,created_at:due_at,kind:'nurture'});
test('default selection of four means four simultaneous windows with no one-hour stagger',()=>{
  assert.deepEqual(nurtureScheduleDefaults,{concurrency:0,interval_seconds:0});
  assert.match(nurtureScheduleSummary(4,0,0,''),/同时执行 4 个.*立即启动.*不额外错开/);
  assert.match(nurtureScheduleSummary(4,1,3600,'2026-09-16T12:00:00Z'),/同时执行 1 个.*指定时间.*3600 秒/);
});
test('immediate action selects one earliest round per free profile without mutating the job list',()=>{
  const list=[job('later','a','queued','2026-09-15T13:00:00Z'),job('first','a'),job('b','b'),job('locked','c')];
  assert.deepEqual(waitingNurtureIds(list,['a','b'],[]),['first','b'].sort((a,b)=>a.localeCompare(b)));
  assert.equal(list[0].id,'later');
});
test('paused running active and finished work cannot be resumed by the waiting action',()=>{
  const list=[job('paused','a','paused'),job('later','a','queued','2026-09-16T10:00:00Z'),job('running','b','running'),job('active','c'),job('done','d','completed'),{...job('post','e'),kind:'posting'},job('waiting','f','waiting_window')];
  assert.deepEqual(waitingNurtureIds(list,['a','b','c','d','e','f'],['active']),['waiting']);
  const sameSecond=[{...job('a-later','x'),queue_order:20},{...job('z-first','x','paused'),queue_order:19}];
  assert.deepEqual(waitingNurtureIds(sameSecond,['x'],[]),[],'insertion order must break same-second ties, not random task IDs');
});
test('waiting labels distinguish scheduled time concurrency account lock and previous round',()=>{
  assert.equal(nurtureWaitLabel('queued','scheduled'),'等待计划时间');
  assert.equal(nurtureWaitLabel('queued','capacity'),'等待并发名额');
  assert.equal(nurtureWaitLabel('waiting_window','window_busy'),'等待窗口释放');
  assert.equal(nurtureWaitLabel('queued','previous_round'),'等待前一轮');
  assert.equal(nurtureWaitLabel('paused','scheduled'),'已暂停');
});
