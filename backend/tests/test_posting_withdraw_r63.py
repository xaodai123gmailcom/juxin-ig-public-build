"""Offline queue withdrawal and admission races; no browser or network effects."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.errors import ConflictError, NotFoundError
from app.posting_workflow import PostingManager
import test_posting_workflow_v2 as fixtures


class PostingWithdrawTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown
    prepared=fixtures.Tests.prepared
    start_body=fixtures.Tests.start_body

    async def queued(self):
        ident=(await self.prepared())[0]
        await self.manager.command('owner',self.start_body([ident]))
        return ident

    def row(self,ident):return self.manager.get('owner',ident)
    def visible(self,ident):return next(row for row in self.manager.snapshot('owner')['jobs'] if row['id']==ident)
    async def withdraw(self,ident):return await self.manager.command('owner',{'action':'withdraw','job_id':ident})
    def audit(self):
        with self.db.read() as c:return [dict(row) for row in c.execute('SELECT * FROM posting_withdraw_history')]

    async def test_withdraw_preserves_asset_text_and_history_without_window_or_cleanup_calls(self):
        ident=await self.queued();before=self.row(ident)
        with self.db.read() as c:asset=dict(c.execute('SELECT * FROM posting_assets').fetchone())
        self.service.acquire_browser_lease_async=AsyncMock(side_effect=AssertionError('must not acquire'))
        self.service.release_browser_lease=Mock(side_effect=AssertionError('must not release'))
        self.provider.cleanup=Mock(side_effect=AssertionError('must not touch material'))
        self.assertTrue(self.visible(ident)['can_withdraw'])
        result=await self.withdraw(ident);after=self.row(ident)
        self.assertTrue(result['requires_assignment']);self.assertTrue(result['requires_review']);self.assertFalse(result['will_repost'])
        self.assertEqual('ready',after['status'])
        for key in ('profile_id','expected_username','expected_actor_id'):self.assertEqual('',after[key])
        for key in ('id','request_key','theme','caption','asset_id','attempt_id','submitted_at','lease_token','created_at','hidden_at'):
            self.assertEqual(before[key],after[key],key)
        self.assertGreater(after['queue_revision'],before['queue_revision'])
        with self.db.read() as c:self.assertEqual(asset,dict(c.execute('SELECT * FROM posting_assets').fetchone()))
        self.assertEqual(before['profile_id'],self.audit()[0]['profile_id'])
        self.assertEqual(before['expected_username'],self.audit()[0]['expected_username'])
        self.assertEqual([],self.manager._runnable_jobs());self.assertFalse(self.visible(ident)['can_withdraw'])
        self.service.acquire_browser_lease_async.assert_not_called();self.service.release_browser_lease.assert_not_called();self.provider.cleanup.assert_not_called()

    async def test_other_owner_or_operation_lease_is_untouched(self):
        ident=await self.queued()
        with self.db.write() as c:c.execute("INSERT INTO browser_operation_leases VALUES('window','other','studio','live-other','do-not-touch')")
        with self.db.read() as c:before=[tuple(row) for row in c.execute('SELECT * FROM browser_operation_leases')]
        await self.withdraw(ident)
        with self.db.read() as c:self.assertEqual(before,[tuple(row) for row in c.execute('SELECT * FROM browser_operation_leases')])

    async def test_repeat_withdraw_changes_only_once(self):
        ident=await self.queued()
        results=await asyncio.gather(self.withdraw(ident),self.withdraw(ident),return_exceptions=True)
        self.assertEqual(1,sum(isinstance(row,dict) for row in results))
        self.assertEqual(1,sum(isinstance(row,ConflictError) for row in results));self.assertEqual(1,len(self.audit()))

    async def test_foreign_owner_cannot_withdraw(self):
        ident=await self.queued();before=self.row(ident)
        with self.assertRaises(NotFoundError):await self.manager.command('other',{'action':'withdraw','job_id':ident})
        self.assertEqual(before,self.row(ident));self.assertEqual([],self.audit())

    async def test_generic_queued_edit_cancel_and_assign_remain_forbidden(self):
        ident=await self.queued()
        for action in ('edit','cancel','assign'):
            with self.subTest(action=action),self.assertRaises(ConflictError):
                await self.manager.command('owner',{'action':action,'job_id':ident,'caption':'changed','profile_id':'window','expected_username':'correct_user'})
        self.assertFalse(self.visible(ident)['can_modify'])

    async def test_all_nonqueued_states_reject_without_mutation(self):
        ident=await self.queued()
        for status in ('preparing','ready','running','submitting','needs_review','unknown_closed','cleanup_pending','failed','completed','cancelled'):
            with self.db.write() as c:c.execute('UPDATE posting_jobs SET status=? WHERE id=?',(status,ident))
            before=self.row(ident)
            with self.subTest(status=status),self.assertRaises(ConflictError):await self.withdraw(ident)
            self.assertEqual(before,self.row(ident));self.assertFalse(self.visible(ident)['can_withdraw'])
        self.assertEqual([],self.audit())

    async def test_submission_receipt_hidden_or_durable_lease_fences_reject(self):
        ident=await self.queued()
        for key,value in (('attempt_id','submitted-once'),('submitted_at','2026-10-04'),('lease_token','lost-token'),('hidden_at','2026-10-04')):
            with self.db.write() as c:c.execute(f'UPDATE posting_jobs SET {key}=? WHERE id=?',(value,ident))
            before=self.row(ident)
            with self.subTest(key=key),self.assertRaises(ConflictError):await self.withdraw(ident)
            self.assertEqual(before,self.row(ident))
            if key!='hidden_at':self.assertFalse(self.visible(ident)['can_withdraw'])
            with self.db.write() as c:c.execute(f'UPDATE posting_jobs SET {key}=? WHERE id=?',(None if key in ('submitted_at','hidden_at') else '',ident))
        with self.db.write() as c:c.execute("INSERT INTO posting_receipts VALUES(?,'owner','window','correct_user','','2026-10-04','2026-10-04','{}')",(ident,))
        with self.assertRaises(ConflictError):await self.withdraw(ident)
        self.assertFalse(self.visible(ident)['can_withdraw']);self.assertEqual([],self.audit())

    async def test_unpersisted_or_mismatched_claim_fences_reject(self):
        ident=await self.queued()
        for owner,profile in (('owner','window'),('other','window'),('owner','mismatched-window')):
            with self.db.write() as c:c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?)',(profile,owner,'posting',ident,'claim-seam'))
            with self.subTest(owner=owner,profile=profile),self.assertRaises(ConflictError):await self.withdraw(ident)
            self.assertFalse(self.visible(ident)['can_withdraw'])
            with self.db.write() as c:
                self.assertEqual('claim-seam',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
                c.execute('DELETE FROM browser_operation_leases')
        self.assertEqual([],self.audit())

    async def test_active_task_or_executor_prevents_withdrawal(self):
        ident=await self.queued();task=asyncio.create_task(asyncio.Event().wait());self.manager.tasks[ident]=task
        try:
            with self.assertRaises(ConflictError):await self.withdraw(ident)
            self.assertFalse(self.visible(ident)['can_withdraw'])
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True);self.manager.tasks.pop(ident,None)
        self.manager.executors[ident]=object()
        with self.assertRaises(ConflictError):await self.withdraw(ident)
        self.assertFalse(self.visible(ident)['can_withdraw']);self.assertEqual([],self.audit())

    async def test_stale_queue_cannot_acquire_after_withdraw_even_when_requeued_in_same_millisecond(self):
        with patch('app.posting_workflow.isoformat',return_value='2026-10-05T00:00:00.000+00:00'):
            ident=await self.queued();stale=self.row(ident)
            await self.withdraw(ident)
            with self.assertRaises(ConflictError):await self.manager.command('owner',self.start_body([ident]))
            await self.manager.command('owner',{'action':'assign','job_id':ident,'profile_id':'window','expected_username':'correct_user'})
            await self.manager.command('owner',self.start_body([ident]))
        self.assertEqual(stale['updated_at'],self.row(ident)['updated_at'])
        self.assertNotEqual(stale['queue_revision'],self.row(ident)['queue_revision'])
        original=self.service.acquire_browser_lease_async
        self.service.acquire_browser_lease_async=AsyncMock(side_effect=original)
        await self.manager._execute(stale);self.service.acquire_browser_lease_async.assert_not_called()
        await self.manager._execute(self.row(ident));self.service.acquire_browser_lease_async.assert_awaited_once()
        self.assertEqual('completed',self.row(ident)['status'])

    async def test_ready_snapshot_returns_before_any_admission(self):
        ident=(await self.prepared())[0]
        self.service.acquire_browser_lease_async=AsyncMock(side_effect=AssertionError('must not acquire'))
        await self.manager._execute(self.row(ident));self.service.acquire_browser_lease_async.assert_not_called()
        self.assertEqual('ready',self.row(ident)['status'])

    async def test_review_revision_requires_integer_and_cannot_replay_after_withdrawal(self):
        ident=(await self.prepared())[0];original=self.start_body([ident])
        for revision in (False,None,'0',1):
            body=self.start_body([ident]);body['reviewed'][0]['queue_revision']=revision
            with self.subTest(revision=revision),self.assertRaises(ConflictError):await self.manager.command('owner',body)
        # Untouched legacy drafts may omit the new zero-valued field once.
        legacy=self.start_body([ident]);legacy['reviewed'][0].pop('queue_revision')
        await self.manager.command('owner',legacy);await self.withdraw(ident)
        await self.manager.command('owner',{'action':'assign','job_id':ident,'profile_id':'window','expected_username':'correct_user'})
        for stale in (legacy,original):
            with self.assertRaises(ConflictError):await self.manager.command('owner',stale)
        self.assertEqual('ready',self.row(ident)['status'])
        await self.manager.command('owner',self.start_body([ident]));self.assertEqual('queued',self.row(ident)['status'])

    async def test_cancelled_worker_waiting_for_admission_cannot_mutate_withdrawn_draft(self):
        ident=await self.queued();stale=self.row(ident);await self.withdraw(ident);before=self.row(ident)
        self.service.acquire_browser_lease_async=AsyncMock(side_effect=AssertionError('must not acquire'))
        async with self.manager.control:
            worker=asyncio.create_task(self.manager._execute(stale));await asyncio.sleep(0)
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        self.assertEqual(before,self.row(ident));self.service.acquire_browser_lease_async.assert_not_called()

    async def test_admission_already_in_flight_cannot_be_withdrawn(self):
        ident=await self.queued();entered=asyncio.Event();release=asyncio.Event();publishing=asyncio.Event();finish=asyncio.Event()
        original=self.service.acquire_browser_lease_async
        async def slow_acquire(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        class PausedExecutor(fixtures.Executor):
            async def publish(self,*args):publishing.set();await finish.wait();return await super().publish(*args)
        self.service.acquire_browser_lease_async=slow_acquire;self.manager.executor_factory=PausedExecutor
        self.manager._spawn(ident,self.manager._execute(self.row(ident)));worker=self.manager.tasks[ident]
        await entered.wait();withdrawal=asyncio.create_task(self.withdraw(ident))
        try:
            await asyncio.sleep(0);self.assertFalse(withdrawal.done());self.assertFalse(self.visible(ident)['can_withdraw'])
            release.set();await publishing.wait()
            with self.assertRaises(ConflictError):await withdrawal
            self.assertEqual('running',self.row(ident)['status']);self.assertTrue(self.row(ident)['lease_token'])
            self.assertEqual([],self.audit())
        finally:release.set();finish.set();await asyncio.gather(worker,withdrawal,return_exceptions=True)

    async def test_restart_preserves_withdrawn_draft_and_audit_without_scheduling_it(self):
        ident=await self.queued();self.manager.recover();self.assertEqual('queued',self.row(ident)['status'])
        await self.withdraw(ident);before=self.row(ident);audit=self.audit()
        fresh=PostingManager(self.service,None,provider=self.provider,executor_factory=fixtures.Executor);fresh.recover()
        self.assertEqual(before,fresh.get('owner',ident));self.assertEqual(audit,self.audit());self.assertEqual([],fresh._runnable_jobs())
        self.assertEqual(0,fixtures.Executor.calls)


if __name__=='__main__':unittest.main()
