"""Real scheduler and SQLite, with worker/page interfaces replaced; no browser."""
import asyncio
import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_studio as fixtures
from app.errors import NotFoundError
from app.studio import build_nurture_steps, config_for
from app.standalone_nurture import POLICY
from studio_wait import wait_for_studio_completion


class NamedWorker(fixtures.Worker):
    def __init__(self,*args):
        super().__init__(*args)
        async def connect(profile,**kwargs): self.profile=profile
        self.connect.side_effect=connect


class NurtureSchedulerTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown

    async def asyncSetUp(self):
        worker_patch=patch('app.studio.PlaywrightWorker',NamedWorker)
        worker_patch.start();self.addCleanup(worker_patch.stop)

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def create(self,profiles,**config):
        return (await self.m.command(self.owner,{'action':'start','kind':'nurture',
            'request_id':'nurture-scheduler-'+str(len(self.m.snapshot(self.owner)['jobs'])),
            'profile_ids':profiles,'config':{'minutes':1,**config}}))['job_ids']

    async def create_persisted_queue(self,profiles,*,rounds=1,scheduled_at='',interval_seconds=0,**config):
        """Exercise stored queue integrity without reviving removed API scheduling.

        These explicitly synthetic, marked fixed-policy rows represent multiple
        pending jobs. Unmarked pre-standalone rows have separate fail-closed tests.
        """
        profiles=list(dict.fromkeys(profiles))
        cfg=config_for('nurture',{'minutes':1,**config})
        cfg['concurrency']=cfg['concurrency'] or len(profiles)
        now=datetime.now(timezone.utc)
        due=datetime.fromisoformat(scheduled_at.replace('Z','+00:00')) if scheduled_at else now
        ids=[]
        with self.db.write() as c:
            for round_index in range(rounds):
                for index,profile in enumerate(profiles):
                    ident='persisted-queue-'+uuid.uuid4().hex
                    stored=dict(cfg,steps=build_nurture_steps(cfg),standalone_policy=POLICY)
                    planned=due+timedelta(seconds=interval_seconds*(round_index*len(profiles)+index))
                    c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (ident,self.owner,ident,'nurture',profile,json.dumps(stored),len(stored['steps']),planned.isoformat(),now.isoformat(),now.isoformat()))
                    ids.append(ident)
        self.assertEqual(len(profiles)*rounds,len(ids))
        return ids

    async def wait_for(self,predicate):
        try:
            async with asyncio.timeout(30):
                while not predicate():await asyncio.sleep(.05)
        except TimeoutError:
            states=[{key:row[key] for key in ('id','profile_id','status','cursor','message')}
                    for row in self.m.snapshot(self.owner)['jobs'][:16]]
            self.fail('Studio scheduler condition timed out: '+json.dumps({
                'jobs':states,'active_ids':sorted(self.m.active_ids())},ensure_ascii=False))

    async def finish(self,ids):
        await wait_for_studio_completion(self.m,self.owner,ids)

    async def test_four_default_windows_overlap_and_next_round_waits_for_its_own_window(self):
        ids=await self.create_persisted_queue(['w1','w2','w3','w4'],rounds=2)
        rows=[self.m.get(self.owner,i) for i in ids]
        self.assertEqual(1,len({r['due_at'] for r in rows}))
        self.assertTrue(all(json.loads(r['config_json'])['concurrency']==4 for r in rows))
        entered=set();active=set();maximum=0;release=asyncio.Event();calls=[]
        async def step(browser,step,counts,config):
            nonlocal maximum
            profile=browser.worker.profile
            self.assertNotIn(profile,active,'a second round must not enter the same window')
            active.add(profile);entered.add(profile);maximum=max(maximum,len(active));calls.append(profile)
            await release.wait();active.remove(profile)
            return {'browse':counts.get('browse',0)+1}
        with patch('app.studio.PlaywrightWorker',NamedWorker),patch('app.studio.StudioBrowser.nurture_step',step):
            try:
                self.m._schedule_ready();await self.wait_for(lambda:len(entered)==4)
                self.assertEqual(set(ids[:4]),self.m.active_ids())
                self.assertTrue(all(self.m.get(self.owner,i)['cursor']==0 for i in ids[4:]))
                release.set();await self.finish(ids)
            finally:release.set();await self.m.shutdown()
        self.assertEqual(4,maximum)
        self.assertEqual(sum(r['total_steps'] for r in rows),len(calls))
        for profile in ('w1','w2','w3','w4'):
            self.assertEqual(sum(r['total_steps'] for r in rows if r['profile_id']==profile),calls.count(profile))
        self.assertEqual(8,len(self.browser.closed))

    async def test_explicit_single_concurrency_starts_following_windows_after_release(self):
        ids=await self.create(['w1','w2','w3'],concurrency=1)
        active=0;maximum=0
        async def step(browser,*args):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);await asyncio.sleep(.01);active-=1
            return {'browse':1}
        with patch('app.studio.PlaywrightWorker',NamedWorker),patch('app.studio.StudioBrowser.nurture_step',step):
            try:await self.finish(ids)
            finally:await self.m.shutdown()
        self.assertEqual(1,maximum);self.assertEqual({'w1','w2','w3'},set(self.browser.closed))

    async def test_persisted_future_queue_times_survive_but_template_schedule_is_removed(self):
        cfg={'concurrency':1,'interval_seconds':3600,'scheduled_at':'2099-01-01T10:00:00+00:00'}
        await self.m.command(self.owner,{'action':'save_template','kind':'nurture','config':cfg})
        ids=await self.create_persisted_queue(['w1','w2','w3','w4'],**cfg)
        dates=[datetime.fromisoformat(self.m.get(self.owner,i)['due_at']) for i in ids]
        self.assertEqual([3600]*3,[(b-a).total_seconds() for a,b in zip(dates,dates[1:])])
        self.m._schedule_ready();self.assertFalse(self.m.active_ids())
        snap=self.m.snapshot(self.owner)
        self.assertTrue(all(j['wait_reason']=='scheduled' for j in snap['jobs']))
        self.assertEqual(0,snap['templates']['nurture']['interval_seconds'])
        self.assertEqual('',snap['templates']['nurture']['scheduled_at'])
        self.assertEqual(1,snap['templates']['nurture']['concurrency'])

    async def test_immediate_batch_advances_existing_waiters_without_resetting_progress(self):
        ids=await self.create_persisted_queue(['w1','w2','w3','w4'],rounds=2,concurrency=1,interval_seconds=3600,scheduled_at='2099-01-01T10:00:00Z')
        self.m.update(ids[0],status='completed')
        self.m.update(ids[1],cursor=1,result_json=json.dumps({'counts':{'browse':1,'like':1}}))
        before=[self.m.get(self.owner,i) for i in ids]
        result=self.m.start_waiting_nurture(self.owner,ids[1:4])
        self.assertEqual(set(ids[1:4]),set(result['job_ids']));self.assertFalse(result['skipped'])
        changed=[self.m.get(self.owner,i) for i in ids[1:4]]
        self.assertEqual(1,len({r['due_at'] for r in changed}))
        self.assertTrue(all(json.loads(r['config_json'])['concurrency']==3 for r in changed))
        self.assertEqual((1,before[1]['result_json']),(changed[0]['cursor'],changed[0]['result_json']))
        self.assertEqual([r['due_at'] for r in before[4:]],[self.m.get(self.owner,i)['due_at'] for i in ids[4:]])

    async def test_immediate_command_skips_locked_paused_finished_and_later_rounds(self):
        ids=await self.create_persisted_queue(['w1','w2','w3'],rounds=2,scheduled_at='2099-01-01T10:00:00Z')
        self.m.update(ids[1],status='paused');self.m.update(ids[2],status='completed')
        lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='other-task',ttl_seconds=600)
        try:
            before=self.m.get(self.owner,ids[0])
            result=self.m.start_waiting_nurture(self.owner,ids[:5])
            self.assertFalse(result['job_ids']);self.assertEqual(5,len(result['skipped']))
            self.assertEqual(before,self.m.get(self.owner,ids[0]));self.assertEqual([],self.browser.closed)
            with self.db.read() as c:self.assertEqual(lease,c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',('w1',)).fetchone()[0])
        finally:self.s.release_browser_lease('w1',lease)

    async def test_foreign_job_in_immediate_batch_rolls_back_all_changes(self):
        ids=await self.create_persisted_queue(['w1'],scheduled_at='2099-01-01T10:00:00Z')
        other=(await self.m.command(self.other,{'action':'start','kind':'nurture','request_id':'foreign-owner-test','profile_ids':['other'],'config':{}}))['job_ids'][0]
        before=self.m.get(self.owner,ids[0])
        with self.assertRaises(NotFoundError):self.m.start_waiting_nurture(self.owner,[ids[0],other])
        self.assertEqual(before,self.m.get(self.owner,ids[0]))

    async def test_busy_first_201_jobs_do_not_starve_free_window_or_consume_its_slot(self):
        ids=await self.create(['w1','w2'],concurrency=1)
        source=self.m.get(self.owner,ids[0])
        self.m.update(ids[1],due_at='2001-01-01T00:00:00+00:00')
        with self.db.write() as c:
            for n in range(201):
                c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (f'busy-{n}',self.owner,f'busy-{n}','nurture','w1',source['config_json'],1,'2000-01-01T00:00:00+00:00',source['created_at'],source['updated_at']))
        lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='collector',ttl_seconds=600)
        entered=[]
        async def step(browser,*args):entered.append(browser.worker.profile);return {'browse':1}
        with patch('app.studio.PlaywrightWorker',NamedWorker),patch('app.studio.StudioBrowser.nurture_step',step):
            try:self.m._schedule_ready();await self.wait_for(lambda:self.m.get(self.owner,ids[1])['status']=='completed')
            finally:await self.m.shutdown();self.s.release_browser_lease('w1',lease)
        self.assertEqual(['w2']*self.m.get(self.owner,ids[1])['total_steps'],entered)
        self.assertEqual(['w2'],self.browser.closed)

    async def test_bad_job_configuration_does_not_block_other_accounts(self):
        ids=await self.create(['w1','w2'])
        self.m.update(ids[0],config_json='invalid json')
        async def step(*args):return {'browse':1}
        with patch('app.studio.PlaywrightWorker',NamedWorker),patch('app.studio.StudioBrowser.nurture_step',step):
            try:self.m._schedule_ready();await self.wait_for(lambda:self.m.get(self.owner,ids[1])['status']=='completed')
            finally:await self.m.shutdown()
        self.assertEqual('failed',self.m.get(self.owner,ids[0])['status'])
        self.assertEqual(['w2'],self.browser.closed)
        snapshot=self.m.snapshot(self.owner)
        self.assertEqual(2,len(snapshot['jobs']),'the damaged record must not break the task list')
        self.assertEqual({},next(j['config'] for j in snapshot['jobs'] if j['id']==ids[0]))

    async def test_paused_earlier_round_prevents_later_round_after_restart(self):
        ids=await self.create_persisted_queue(['w1'],rounds=2)
        self.m.update(ids[0],status='paused')
        self.m._schedule_ready();self.assertFalse(self.m.active_ids())
        row=next(j for j in self.m.snapshot(self.owner)['jobs'] if j['id']==ids[1])
        self.assertEqual('previous_round',row['wait_reason'])

    async def test_finished_task_metadata_does_not_hold_capacity_and_scheduler_starts_once(self):
        ids=await self.create(['w1'],concurrency=1)
        done=asyncio.create_task(asyncio.sleep(0));await done
        self.m.tasks['stale']=done;self.m.task_meta['stale']=(self.owner,'nurture');self.m.task_profiles['stale']='w1'
        async def step(*args):return {'browse':1}
        with patch('app.studio.PlaywrightWorker',NamedWorker),patch('app.studio.StudioBrowser.nurture_step',step):
            try:
                self.m.start_scheduler();first=self.m.scheduler;self.m.start_scheduler()
                self.assertIs(first,self.m.scheduler)
                await self.wait_for(lambda:self.m.get(self.owner,ids[0])['status']=='completed')
            finally:await self.m.shutdown()
        self.assertEqual(['w1'],self.browser.closed)

    async def test_default_auto_concurrency_is_resolved_at_creation_but_template_keeps_auto(self):
        self.assertEqual(0,config_for('nurture',{})['concurrency'])
        await self.m.command(self.owner,{'action':'save_template','kind':'nurture','config':{}})
        ids=await self.create(['w1','w2','w2'])
        self.assertEqual(2,len(ids))
        self.assertEqual(2,json.loads(self.m.get(self.owner,ids[0])['config_json'])['concurrency'])
        self.assertEqual(0,self.m.snapshot(self.owner)['templates']['nurture']['concurrency'])


if __name__=='__main__':unittest.main()
