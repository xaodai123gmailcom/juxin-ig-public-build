"""Fresh-page recovery ordering with page/session doubles; no real browser."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import PlaywrightWorker, VisibleProfile, CollectionOutcome, WorkerExecutionError


class Page:
    def __init__(self, name, events):
        self.name, self.events, self.closed = name, events, False
        self.url = 'about:blank'

    async def goto(self, url, **kwargs):
        self.events.append(('navigate', self.name))
        self.url = url

    async def reload(self, **kwargs):
        raise AssertionError('recovery must not refresh the old page')

    async def close(self):
        self.events.append(('close', self.name))
        self.closed = True


class RecoveryWorker(PlaywrightWorker):
    def __init__(self):
        super().__init__(None)
        self.events=[]
        self.old=Page('old',self.events)
        self.page=self.old
        self.fresh=Page('fresh',self.events)
        self._context=type('Context',(),{'new_page':AsyncMock(return_value=self.fresh)})()

    async def _new_active_page_session(self, page):
        return None

    async def _validate_recovery_profile_page(self, page, target, **kwargs):
        if page.url!=f'https://www.instagram.com/{target}/':
            raise AssertionError('wrong recovery target')
        self.events.append(('validated',page.name))


class NewPageFirstTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonempty_failed_profile_uses_one_fresh_page_before_any_old_page_retry(self):
        class Worker(RecoveryWorker):
            async def _read_visible_profile_once(self,target,**kwargs):
                self.events.append(('read',self.page.name))
                if self.page is self.old:
                    raise WorkerExecutionError('DOM not ready',reason='instagram_profile_not_ready')
                self_test.assertFalse(self.old.closed)
                return VisibleProfile(target,'public',1,2,3)
        self_test=self
        worker=Worker()
        await worker.read_visible_profile('target')
        self.assertEqual([('read','old'),('navigate','fresh'),('validated','fresh'),('read','fresh'),('close','old')],worker.events)
        worker._context.new_page.assert_awaited_once()

    async def test_first_new_batch_is_saved_before_old_page_is_closed(self):
        class Worker(RecoveryWorker):
            async def _collect_relation_once(self,target,**kwargs):
                if self.page is self.old:
                    raise WorkerExecutionError('stalled',reason='instagram_followers_list_incomplete')
                self_test.assertEqual(147,kwargs['initial_candidate_count'])
                self_test.assertEqual(['saved_146','saved_147'],list(kwargs['initial_resume_tail']))
                await kwargs['progress_sink']({'discovered_count':148,'resume_tail':['saved_147','new_148']})
                return CollectionOutcome('followers',[],source_total=296)
        self_test=self
        worker=Worker()
        async def save(payload):
            self.assertFalse(worker.old.closed)
            worker.events.append(('saved',payload['discovered_count']))
        await worker.collect_followers('target',limit=None,initial_candidate_count=147,
                                       initial_resume_tail=['saved_146','saved_147'],progress_sink=save)
        self.assertLess(worker.events.index(('saved',148)),worker.events.index(('close','old')))
        self.assertIs(worker.page,worker.fresh)

    async def test_checkpoint_save_failure_restores_old_page_and_closes_only_candidate(self):
        class Worker(RecoveryWorker):
            async def _collect_relation_once(self,target,**kwargs):
                if self.page is self.old:
                    raise WorkerExecutionError('stalled',reason='instagram_following_list_incomplete')
                await kwargs['progress_sink']({'discovered_count':148})
                raise AssertionError('failed save must stop the candidate')
        worker=Worker()
        async def cannot_save(payload):
            raise RuntimeError('checkpoint unavailable')
        with self.assertRaisesRegex(RuntimeError,'checkpoint unavailable'):
            await worker.collect_following('target',limit=None,initial_candidate_count=147,progress_sink=cannot_save)
        self.assertIs(worker.page,worker.old)
        self.assertFalse(worker.old.closed)
        self.assertTrue(worker.fresh.closed)

    async def test_same_page_returned_as_new_is_rejected_without_closing_old(self):
        worker=RecoveryWorker()
        worker._context.new_page=AsyncMock(return_value=worker.old)
        self.assertFalse(await worker._recover_stalled_profile_page('target'))
        self.assertIs(worker.page,worker.old)
        self.assertFalse(worker.old.closed)

    async def test_cancellation_after_candidate_publication_restores_old_page(self):
        worker=RecoveryWorker()
        with patch('app.playwright_worker.label_task_page',AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await worker._recover_stalled_profile_page('target')
        self.assertIs(worker.page,worker.old)
        self.assertFalse(worker.old.closed)
        self.assertTrue(worker.fresh.closed)
        self.assertIsNone(worker._pending_recovery_old)

    async def test_new_page_without_new_data_pauses_without_reopening_again(self):
        class Worker(RecoveryWorker):
            async def _collect_relation_once(self,target,**kwargs):
                await kwargs['progress_sink']({'discovered_count':147})
                raise WorkerExecutionError('still stalled',reason='instagram_followers_list_incomplete')
        worker=Worker()
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker.collect_followers('target',limit=None,initial_candidate_count=147)
        self.assertEqual('instagram_page_recovery_exhausted',caught.exception.code)
        self.assertFalse(caught.exception.details['auto_retry'])
        self.assertIs(worker.page,worker.old)
        self.assertFalse(worker.old.closed)
        self.assertTrue(worker.fresh.closed)
        worker._context.new_page.assert_awaited_once()

    async def test_post_likers_resume_in_new_page_and_save_progress_before_close(self):
        class Worker(RecoveryWorker):
            async def _collect_post_likers_once(self,target,**kwargs):
                self_test.assertEqual(40,kwargs['initial_candidate_count'])
                if self.page is self.old:
                    raise WorkerExecutionError('post grid unavailable',reason='instagram_post_grid_not_rendered')
                await kwargs['progress_sink']({'discovered_count':41})
                return CollectionOutcome('post_likers',[],candidate_count=41)
        self_test=self
        worker=Worker()
        async def save(payload):
            self.assertFalse(worker.old.closed)
            worker.events.append(('saved',payload['discovered_count']))
        result=await worker.collect_post_likers('target',max_posts=2,per_post_limit=100,initial_candidate_count=40,progress_sink=save)
        self.assertEqual(41,result.candidate_count)
        self.assertLess(worker.events.index(('saved',41)),worker.events.index(('close','old')))

    async def test_post_likers_failure_in_new_page_keeps_old_and_requires_intervention(self):
        class Worker(RecoveryWorker):
            async def _collect_post_likers_once(self,target,**kwargs):
                raise WorkerExecutionError('list unavailable',reason='instagram_post_likers_list_not_rendered')
        worker=Worker()
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker.collect_post_likers('target',max_posts=2,per_post_limit=100)
        self.assertFalse(caught.exception.details['auto_retry'])
        self.assertIs(worker.page,worker.old)
        self.assertFalse(worker.old.closed)
        self.assertTrue(worker.fresh.closed)
        worker._context.new_page.assert_awaited_once()

    async def test_failed_candidate_reports_its_own_reason_instead_of_the_old_page_reason(self):
        class Worker(RecoveryWorker):
            async def _read_visible_profile_once(self,target,**kwargs):
                raise WorkerExecutionError('old DOM stalled',reason='instagram_profile_not_ready')
            async def _validate_recovery_profile_page(self,page,target,**kwargs):
                raise WorkerExecutionError('fresh page network unavailable',reason='instagram_network_unavailable')
        worker=Worker()
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker.read_visible_profile('target')
        self.assertEqual('instagram_network_unavailable',caught.exception.details['original_reason'])
        self.assertFalse(caught.exception.details['auto_retry'])
        self.assertFalse(worker.old.closed)
        self.assertTrue(worker.fresh.closed)


if __name__=='__main__':unittest.main()
