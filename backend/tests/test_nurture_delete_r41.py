"""Failed nurture removal preserves receipts and cannot revive or unlock work."""
import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import test_studio as fixtures
from app.errors import ConflictError, NotFoundError, ValidationError
from app.studio import StudioManager


class NurtureDeleteR41Tests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start

    async def failed(self, key='failed-nurture-r41', profile='w1'):
        ident=(await self.start('nurture',[profile],{'minutes':1},key))['job_ids'][0]
        self.m.update(ident,status='failed',message='没有可浏览的快拍')
        return ident

    async def remove(self, ident, owner=None):
        return await self.m.command(owner or self.owner,{'action':'delete_failed_nurture','job_id':ident})

    def stored(self, ident):
        with self.db.read() as c:
            return dict(c.execute('SELECT * FROM studio_jobs WHERE id=?',(ident,)).fetchone())

    def test_removed_failure_keeps_receipts_counts_and_survives_restart(self):
        async def run():
            ident=await self.failed()
            receipt={'counts':{'browse':1,'like':1},'nurture_actions':{'once':{'state':'confirmed','action':'like'}},
                     'nurture_decisions':{'/reel/once':{'draw':0.1,'selected':True}},
                     'failure':{'message':'没有可浏览的快拍','step':1},'attempts':[{'message':'earlier'}]}
            self.m.commit_nurture_step(self.owner,ident,1,receipt)
            self.m.update(ident,status='failed',message='没有可浏览的快拍')
            before=self.stored(ident);counts=self.m.daily_action_counts(self.owner,'w1')
            response=await self.remove(ident)
            self.assertEqual({'deleted_ids':[ident],'skipped':[]},response)
            after=self.stored(ident)
            self.assertTrue(after.pop('deleted_at'));before.pop('deleted_at')
            self.assertEqual(before,after)
            self.assertEqual(counts,self.m.daily_action_counts(self.owner,'w1'))
            self.db.initialize()
            restored=StudioManager(self.s,self.browser);restored.recover()
            snapshot=restored.snapshot(self.owner)
            history=next(j for j in snapshot['jobs'] if j['id']==ident)
            self.assertTrue(history['deleted_at']);self.assertEqual('failed',history['status'])
            public_receipt=dict(receipt,nurture_actions={},nurture_decision_count=1)
            public_receipt.pop('nurture_decisions')
            self.assertEqual(public_receipt,history['result'])
            self.assertEqual(receipt,json.loads(self.stored(ident)['result_json']))
            self.assertEqual([],snapshot['totals']);self.assertEqual([],snapshot['active_ids'])
            with self.assertRaises(NotFoundError):await restored.control(self.owner,ident,'retry')
            with patch.object(restored,'_execute',new=AsyncMock()) as execute:
                restored._schedule_ready();await asyncio.sleep(0);execute.assert_not_awaited()
            replay=await self.start('nurture',['w1'],{'minutes':1},'failed-nurture-r41')
            self.assertEqual([ident],replay['job_ids'])
            self.assertEqual(response,await self.remove(ident))
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_only_failed_nurture_is_removable_and_retired_commands_are_rejected(self):
        async def run():
            for status in ('queued','waiting_window','running','paused','needs_review','cancelled','completed'):
                ident=await self.failed('guard-'+status,profile='guard-window-'+status)
                self.m.update(ident,status=status)
                before=self.stored(ident)
                with self.subTest(status=status):
                    reply=await self.remove(ident)
                    self.assertEqual([],reply['deleted_ids']);self.assertTrue(reply['skipped'])
                    self.assertEqual(before,self.stored(ident))
            for kind in ('posting','material'):
                with self.assertRaises(ValidationError):await self.start(kind,key='guard-kind-'+kind)
            nurture=await self.failed('guard-retired-command')
            with self.assertRaises(ValidationError):
                await self.m.command(self.owner,{'action':'delete_unfinished','job_id':nurture})
            self.assertIsNone(self.stored(nurture)['deleted_at'])
        asyncio.run(run())

    def test_inflight_and_unconfirmed_results_are_not_discarded(self):
        async def run():
            variants=[{'inflight':1},
                      {'result_json':json.dumps({'nurture_actions':{'x':{'state':'pending','action':'like'}}})},
                      {'result_json':'not-json'}]
            for index,fields in enumerate(variants):
                ident=await self.failed(f'uncertain-{index}')
                self.m.update(ident,**fields);before=self.stored(ident)
                with self.subTest(index=index):
                    reply=await self.remove(ident)
                    self.assertEqual([],reply['deleted_ids']);self.assertTrue(reply['skipped'])
                    self.assertEqual(before,self.stored(ident))
        asyncio.run(run())

    def test_stale_window_hold_without_live_work_can_be_removed(self):
        async def run():
            ident=await self.failed('stale-hold')
            self.m.update(ident,result_json=json.dumps({'window_hold':True}))
            self.assertEqual([ident],(await self.remove(ident))['deleted_ids'])
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_active_cleanup_and_own_lease_block_removal_without_cancelling(self):
        async def run():
            ident=await self.failed()
            release=asyncio.Event()
            task=asyncio.create_task(release.wait());self.m.tasks[ident]=task
            self.m.task_meta[ident]=(self.owner,'nurture');self.m.task_profiles[ident]='w1'
            lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id=ident,ttl_seconds=600)
            try:
                self.assertEqual([], (await self.remove(ident))['deleted_ids'])
                self.assertFalse(task.done());self.assertEqual([],self.browser.closed)
                release.set();await task
                self.assertEqual([], (await self.remove(ident))['deleted_ids'])
                with self.db.read() as c:
                    self.assertEqual(lease,c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
                self.s.release_browser_lease('w1',lease)
                self.assertEqual([ident],(await self.remove(ident))['deleted_ids'])
            finally:
                release.set();await task
        asyncio.run(run())

    def test_other_task_window_lease_and_sibling_failure_are_preserved(self):
        async def run():
            removed=await self.failed('remove-old-failure')
            sibling=await self.failed('keep-sibling-failure')
            before=self.stored(sibling)
            lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='collection',entity_id='current-collection',ttl_seconds=600)
            self.assertEqual([removed],(await self.remove(removed))['deleted_ids'])
            self.assertEqual(before,self.stored(sibling))
            with self.db.read() as c:
                self.assertEqual(lease,c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
            snapshot=self.m.snapshot(self.owner)
            self.assertEqual([sibling],[j['id'] for j in snapshot['jobs'] if not j['deleted_at']])
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_owner_and_required_single_job_identifier(self):
        async def run():
            ident=await self.failed();before=self.stored(ident)
            with self.assertRaises(NotFoundError):await self.remove(ident,self.other)
            with self.assertRaises(NotFoundError):await self.remove('missing-job')
            for invalid in (None,'', '   ',123,[],{}):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValidationError):await self.remove(invalid)
            self.assertEqual(before,self.stored(ident))
            self.assertEqual([],self.m.snapshot(self.other)['jobs'])
        asyncio.run(run())

    def test_remove_winning_shared_control_lock_cannot_be_retried(self):
        async def run():
            ident=await self.failed()
            await self.m.control_lock.acquire()
            deletion=asyncio.create_task(self.remove(ident))
            retry=asyncio.create_task(self.m.control(self.owner,ident,'retry'))
            await asyncio.sleep(0)
            self.assertFalse(deletion.done());self.assertFalse(retry.done())
            self.m.control_lock.release()
            deleted,retried=await asyncio.gather(deletion,retry,return_exceptions=True)
            self.assertEqual([ident],deleted['deleted_ids']);self.assertIsInstance(retried,NotFoundError)
            self.assertEqual('failed',self.stored(ident)['status']);self.assertTrue(self.stored(ident)['deleted_at'])
        asyncio.run(run())

    def test_retry_winning_shared_control_lock_cannot_be_deleted(self):
        async def run():
            ident=await self.failed()
            await self.m.control_lock.acquire()
            retry=asyncio.create_task(self.m.control(self.owner,ident,'retry'))
            deletion=asyncio.create_task(self.remove(ident))
            await asyncio.sleep(0);self.m.control_lock.release()
            retried,deleted=await asyncio.gather(retry,deletion)
            self.assertEqual('queued',retried['status']);self.assertEqual([],deleted['deleted_ids'])
            self.assertTrue(deleted['skipped']);self.assertIsNone(self.stored(ident)['deleted_at'])
        asyncio.run(run())

    def test_retry_rechecks_archival_inside_transaction_after_stale_read(self):
        ident=asyncio.run(self.failed())
        another=StudioManager(self.s,self.browser)
        read_finished=threading.Event();deleted=threading.Event();original=self.m.get
        def stale_get(owner,key):
            row=original(owner,key);read_finished.set()
            if not deleted.wait(3):raise RuntimeError('deletion did not complete')
            return row
        def remove_on_other_connection():
            if not read_finished.wait(3):raise RuntimeError('retry read did not finish')
            try:return asyncio.run(another.command(self.owner,{'action':'delete_failed_nurture','job_id':ident}))
            finally:deleted.set()
        with patch.object(self.m,'get',side_effect=stale_get),ThreadPoolExecutor(max_workers=2) as pool:
            removal=pool.submit(remove_on_other_connection)
            retry=pool.submit(asyncio.run,self.m.control(self.owner,ident,'retry'))
            self.assertEqual([ident],removal.result(timeout=5)['deleted_ids'])
            with self.assertRaises(NotFoundError):retry.result(timeout=5)
        self.assertEqual('failed',self.stored(ident)['status']);self.assertTrue(self.stored(ident)['deleted_at'])

    def test_archive_history_is_bounded_without_hiding_actionable_failures(self):
        async def run():
            newest=await self.failed('bounded-base')
            row=self.stored(newest)
            with self.db.write() as c:
                for index in range(505):
                    record=dict(row,id=f'archived-{index}',request_key=f'archived-key-{index}',deleted_at=row['created_at'])
                    columns=list(record)
                    c.execute('INSERT INTO studio_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[record[k] for k in columns])
            snapshot=self.m.snapshot(self.owner)
            self.assertEqual(500,sum(bool(j['deleted_at']) for j in snapshot['jobs']))
            self.assertEqual([newest],[j['id'] for j in snapshot['jobs'] if not j['deleted_at']])
            self.assertEqual(1,snapshot['totals'][0]['count'])
        asyncio.run(run())

    def test_removing_old_failure_keeps_it_visible_after_500_newer_completed_rounds(self):
        async def run():
            old=await self.failed('old-failure-history')
            with self.db.write() as c:
                c.execute("UPDATE studio_jobs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",(old,))
            row=self.stored(old)
            with self.db.write() as c:
                for index in range(500):
                    record=dict(row,id=f'completed-{index}',request_key=f'completed-key-{index}',status='completed',created_at='2021-01-01T00:00:00+00:00')
                    columns=list(record)
                    c.execute('INSERT INTO studio_jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[record[k] for k in columns])
            self.assertEqual([old],(await self.remove(old))['deleted_ids'])
            history=self.m.snapshot(self.owner)['jobs']
            self.assertEqual(500,len(history));self.assertEqual(old,history[0]['id'])
            self.assertTrue(history[0]['deleted_at']);self.assertEqual('failed',history[0]['status'])
            self.assertEqual(row['created_at'],self.stored(old)['created_at'])
        asyncio.run(run())


if __name__=='__main__':unittest.main()
