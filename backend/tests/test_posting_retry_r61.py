"""Offline lifecycle regressions: a retry restores review, never repeats a share."""
import asyncio
import unittest
from unittest.mock import Mock

from app.errors import ConflictError, ValidationError
from app.posting_workflow import PostingManager
from app.posting_pexels import PexelsProvider
import test_posting_workflow_v2 as fixtures
from test_posting_workflow_v2 import Executor


class PostingRetryR61(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown
    prepared=fixtures.Tests.prepared
    start_body=fixtures.Tests.start_body

    def row(self,ident):return self.manager.get('owner',ident)
    def visible(self,ident):return next(j for j in self.manager.snapshot('owner')['jobs'] if j['id']==ident)
    async def retry(self,ident):return await self.manager.command('owner',{'action':'retry','job_id':ident})
    async def fail_before_submit(self):
        ident=(await self.prepared())[0]
        class Fail(Executor):
            async def publish(self,job,asset,checkpoint,before,confirmed):
                type(self).calls+=1
                job['_posting_phase']='browser_open'
                raise RuntimeError('secret-do-not-display https://example.invalid/?token=private')
        self.manager.executor_factory=Fail
        await self.manager.command('owner',self.start_body([ident]))
        await self.manager._execute(self.row(ident))
        return ident

    async def test_presubmit_retry_keeps_exact_content_and_requires_fresh_confirmation(self):
        ident=await self.fail_before_submit();before=self.row(ident)
        self.assertEqual('review',self.visible(ident)['retry_action'])
        self.assertEqual('browser_open_failed',before['failure_code'])
        self.assertNotIn('secret',before['message'])
        result=await self.retry(ident);after=self.row(ident)
        self.assertEqual('ready',after['status']);self.assertFalse(result['will_repost'])
        self.assertTrue(result['requires_review'])
        for field in ('id','request_key','caption','asset_id','profile_id','expected_username','expected_actor_id'):
            self.assertEqual(before[field],after[field],field)
        self.assertEqual([],self.manager._runnable_jobs())
        with self.db.read() as c:self.assertEqual(1,c.execute('SELECT COUNT(*) FROM posting_retry_history').fetchone()[0])
        self.manager.executor_factory=Executor
        await self.manager.command('owner',self.start_body([ident]));await self.manager._execute(self.row(ident))
        self.assertEqual('completed',self.row(ident)['status'])
        self.assertEqual(1,self.manager.snapshot('owner')['totals']['success'])

    async def test_repeat_clicks_queue_only_one_retry(self):
        ident=await self.fail_before_submit()
        results=await asyncio.gather(self.retry(ident),self.retry(ident),return_exceptions=True)
        self.assertEqual(1,sum(isinstance(r,dict) for r in results))
        self.assertEqual(1,sum(isinstance(r,ConflictError) for r in results))
        with self.db.read() as c:self.assertEqual(1,c.execute('SELECT COUNT(*) FROM posting_retry_history').fetchone()[0])

    async def test_preparation_failure_without_asset_retries_preparation_only(self):
        ident=self.manager.generate('owner',{'request_id':'prepare-failure-1','theme':'forest','caption':'exact 原文','count':1})['job_ids'][0]
        prepare=self.provider.prepare
        self.provider.prepare=Mock(side_effect=ValidationError('network fixture secret'))
        await self.manager._prepare(self.row(ident));self.provider.prepare=prepare
        self.assertEqual('prepare',self.visible(ident)['retry_action'])
        result=await self.retry(ident);self.assertEqual('preparing',result['status'])
        await self.manager._prepare(self.row(ident))
        self.assertEqual('ready',self.row(ident)['status']);self.assertEqual(0,Executor.calls)

    async def test_reserved_asset_is_reused_without_research_or_new_identity(self):
        ident=(await self.prepared())[0];asset=self.row(ident)['asset_id']
        with self.db.write() as c:
            c.execute("UPDATE posting_jobs SET status='failed',failure_stage='preparation' WHERE id=?",(ident,))
            c.execute("UPDATE posting_assets SET state='reserved' WHERE id=?",(asset,))
        provider=PexelsProvider(self.db,lambda:'')
        provider.search=Mock(side_effect=AssertionError('must not search again'))
        def download(record):
            with self.db.write() as c:c.execute("UPDATE posting_assets SET state='ready' WHERE id=?",(record['id'],))
        provider.download=Mock(side_effect=download);self.manager.provider=provider
        self.assertEqual('preparing',(await self.retry(ident))['status'])
        await self.manager._prepare(self.row(ident))
        self.assertEqual(asset,self.row(ident)['asset_id']);provider.search.assert_not_called()
        with self.db.read() as c:self.assertEqual(1,c.execute('SELECT COUNT(*) FROM posting_assets').fetchone()[0])

    async def test_missing_credentials_actionable_and_does_not_mutate(self):
        ident=self.manager.generate('owner',{'request_id':'prepare-missing-1','theme':'forest','caption':'exact','count':1})['job_ids'][0]
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed' WHERE id=?",(ident,))
        self.manager.provider=PexelsProvider(self.db,lambda:'')
        with self.assertRaisesRegex(ValidationError,'安全配置'):await self.retry(ident)
        self.assertEqual('failed',self.row(ident)['status'])
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM posting_retry_history').fetchone()[0])

    async def test_unknown_and_confirmed_never_retry_even_with_tampered_failed_status(self):
        ident=(await self.prepared())[0]
        await self.manager.command('owner',self.start_body([ident]));Executor.mode='unknown'
        await self.manager._execute(self.row(ident))
        with self.assertRaises(ConflictError):await self.retry(ident)
        await self.manager.command('owner',{'action':'close_unknown','job_id':ident,'confirm_no_repost':True});await self.manager.tasks[ident]
        with self.assertRaises(ConflictError):await self.retry(ident)
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed' WHERE id=?",(ident,))
        with self.assertRaises(ConflictError):await self.retry(ident)
        self.assertEqual(1,Executor.calls)

    async def test_known_rejection_archives_attempt_before_explicit_retry(self):
        ident=(await self.prepared())[0]
        await self.manager.command('owner',self.start_body([ident]));Executor.mode='reject'
        await self.manager._execute(self.row(ident));before=self.row(ident)
        self.assertTrue(before['attempt_id']);self.assertEqual('rejected',before['failure_stage'])
        await self.retry(ident)
        self.assertEqual('',self.row(ident)['attempt_id']);self.assertIsNone(self.row(ident)['submitted_at'])
        with self.db.read() as c:
            archived=c.execute('SELECT * FROM posting_retry_history WHERE job_id=?',(ident,)).fetchone()
        self.assertEqual(before['attempt_id'],archived['attempt_id']);self.assertEqual(before['submitted_at'],archived['submitted_at'])
        self.assertEqual(1,Executor.calls)

    async def test_cleanup_retry_only_closes_and_does_not_share(self):
        ident=await self.fail_before_submit()
        # Recreate an interrupted close using the real durable lease protocol.
        token=await self.service.acquire_browser_lease_async('owner','window',operation_type='posting',entity_id=ident)
        with self.db.write() as c:c.execute('UPDATE posting_jobs SET lease_token=? WHERE id=?',(token,ident))
        self.assertIsNone(self.visible(ident)['retry_action']);self.assertTrue(self.visible(ident)['can_retry_cleanup'])
        with self.assertRaises(ConflictError):await self.retry(ident)
        for action in ('assign','edit'):
            with self.assertRaises(ConflictError):await self.manager.command('owner',{'action':action,'job_id':ident,'profile_id':'window','expected_username':'correct_user','caption':'changed'})
        await self.manager.command('owner',{'action':'retry_cleanup','job_id':ident});await self.manager.tasks[ident]
        self.assertFalse(self.row(ident)['lease_token']);self.assertEqual('failed',self.row(ident)['status'])
        self.assertEqual('ready',(await self.retry(ident))['status'])

    async def test_success_cleanup_failure_never_offers_publish_retry(self):
        ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]))
        Executor.close_fail=True;await self.manager._execute(self.row(ident))
        self.assertEqual('cleanup_pending',self.row(ident)['status'])
        with self.assertRaises(ConflictError):await self.retry(ident)
        Executor.close_fail=False;await self.manager._cleanup(self.row(ident))
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed',hidden_at=NULL,attempt_id='',submitted_at=NULL WHERE id=?",(ident,))
        with self.assertRaises(ConflictError):await self.retry(ident)
        self.assertEqual(1,Executor.calls)

    async def test_foreign_lease_owner_or_live_operation_blocks_retry(self):
        ident=await self.fail_before_submit()
        with self.assertRaises(Exception):await self.manager.command('other',{'action':'retry','job_id':ident})
        token=await self.service.acquire_browser_lease_async('owner','window',operation_type='nurture',entity_id='other')
        with self.assertRaises(ConflictError):await self.retry(ident)
        self.service.release_browser_lease('window',token)
        done=asyncio.Event();task=asyncio.create_task(done.wait());self.manager.tasks[ident]=task
        try:
            with self.assertRaises(ConflictError):await self.retry(ident)
        finally:done.set();await task

    async def test_restart_and_stop_do_not_auto_publish(self):
        ident=await self.fail_before_submit();self.manager.recover()
        self.manager.stopping=True
        with self.assertRaises(ConflictError):await self.retry(ident)
        self.manager.stopping=False;await self.retry(ident);self.manager.recover()
        self.assertEqual('ready',self.row(ident)['status']);self.assertEqual([],self.manager._runnable_jobs())
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='running' WHERE id=?",(ident,))
        self.manager.recover();self.assertEqual('failed',self.row(ident)['status'])
        self.assertEqual('interrupted',self.row(ident)['failure_code'])

    async def test_legacy_failed_without_submit_can_retry_but_legacy_attempt_cannot(self):
        ident=await self.fail_before_submit()
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET failure_stage='',failure_code='' WHERE id=?",(ident,))
        self.assertEqual('review',self.visible(ident)['retry_action'])
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET attempt_id='old-unknown' WHERE id=?",(ident,))
        with self.assertRaises(ConflictError):await self.retry(ident)

    async def test_duplicate_retired_and_used_materials_never_reenter_queue(self):
        ident=await self.fail_before_submit();asset=self.row(ident)['asset_id']
        for state in ('duplicate','retired','used'):
            with self.db.write() as c:c.execute('UPDATE posting_assets SET state=? WHERE id=?',(state,asset))
            with self.assertRaises(ConflictError):await self.retry(ident)


if __name__=='__main__':unittest.main()
