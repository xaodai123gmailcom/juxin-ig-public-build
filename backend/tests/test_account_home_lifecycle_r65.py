"""Account homepage lifecycle preserves manual tabs and exact resource ownership."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

class AccountHomeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, fresh):
        from app.playwright_worker import PlaywrightWorker
        worker=PlaywrightWorker(object())
        worker.page=SimpleNamespace(close=AsyncMock())
        worker._context=SimpleNamespace(new_page=AsyncMock(return_value=fresh))
        worker._cdp_session=SimpleNamespace(detach=AsyncMock())
        worker._new_active_page_session=AsyncMock(return_value=SimpleNamespace(detach=AsyncMock()))
        return worker

    async def test_navigation_failure_keeps_original_page_and_disposes_new_resources(self):
        fresh=SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError('offline')),close=AsyncMock())
        worker=self.worker(fresh);old=worker.page;session=worker._cdp_session
        worker._worker_owned_page=old
        with self.assertRaisesRegex(RuntimeError,'offline'):await worker.open_account_home_page()
        self.assertIs(old,worker.page);self.assertIs(old,worker._worker_owned_page)
        fresh.close.assert_awaited_once();old.close.assert_not_awaited();session.detach.assert_not_awaited()
        worker._new_active_page_session.return_value.detach.assert_awaited_once()

    async def test_cancelled_navigation_closes_new_tab_and_preserves_operator_page(self):
        import asyncio
        entered=asyncio.Event()
        async def navigate(*a,**kw):entered.set();await asyncio.Event().wait()
        fresh=SimpleNamespace(goto=navigate,close=AsyncMock())
        worker=self.worker(fresh);old=worker.page
        task=asyncio.create_task(worker.open_account_home_page());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertIs(old,worker.page);old.close.assert_not_awaited();fresh.close.assert_awaited_once()

    async def test_page_created_after_timeout_is_reclaimed(self):
        import asyncio
        release=asyncio.Event();closed=asyncio.Event()
        fresh=SimpleNamespace(goto=AsyncMock(),close=AsyncMock(side_effect=closed.set))
        worker=self.worker(fresh);old=worker.page;worker.page_create_timeout_seconds=.01
        async def delayed():await release.wait();return fresh
        worker._context.new_page=delayed
        with self.assertRaises(TimeoutError):await worker.open_account_home_page()
        release.set();await asyncio.wait_for(closed.wait(),1)
        self.assertIs(old,worker.page);old.close.assert_not_awaited();fresh.goto.assert_not_awaited()


    async def test_label_new_target_before_navigation_then_activate(self):
        events=[]
        fresh=SimpleNamespace(goto=AsyncMock(side_effect=lambda *a,**k:events.append('goto')),close=AsyncMock())
        worker=self.worker(fresh);source=worker.page
        new_session=worker._new_active_page_session.return_value
        async def label(w,role,**kwargs):
            self.assertNotIn('viewport_mode',kwargs)
            if kwargs.get('cdp_session') is not None:
                events.append('candidate-label')
                self.assertIs(source,w.page)
                self.assertIs(new_session,kwargs['cdp_session'])
            else:events.append('activated-label')
        with patch('app.playwright_worker.label_task_page',side_effect=label):
            self.assertIs(fresh,await worker.open_account_home_page())
        self.assertEqual(['candidate-label','goto','activated-label'],events)
        source.close.assert_not_awaited()

    async def test_absent_new_session_never_labels_borrowed_source(self):
        fresh=SimpleNamespace(goto=AsyncMock(),close=AsyncMock())
        worker=self.worker(fresh);source_session=worker._cdp_session
        label=AsyncMock();worker._new_active_page_session.return_value=None
        with patch('app.playwright_worker.label_task_page',label):
            await worker.open_account_home_page()
        self.assertTrue(label.await_args_list)
        for call in label.await_args_list:
            self.assertNotIn('viewport_mode',call.kwargs)
            self.assertIsNone(call.kwargs.get('cdp_session'))
        self.assertIsNone(worker._cdp_session)
        source_session.detach.assert_awaited_once()


class AccountTaskLabelTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        bridge=SimpleNamespace(call=Mock())
        session=SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'old-source'}}))
        worker=SimpleNamespace(profile_id='native:profile',_cdp_session=session,
            bitbrowser=SimpleNamespace(native=SimpleNamespace(bridge=bridge)))
        return worker,bridge,session

    async def test_explicit_candidate_label_never_touches_source_session(self):
        from app.task_page_labels import label_task_page
        worker,bridge,old=self.worker()
        candidate=SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'new-account-home'}}))
        await label_task_page(worker,'task',cdp_session=candidate)
        old.send.assert_not_awaited()
        candidate.send.assert_awaited_once_with('Target.getTargetInfo')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='new-account-home',role='task')
        self.assertIs(worker._cdp_session,old)

    async def test_normal_relabel_uses_current_exact_target(self):
        from app.task_page_labels import label_task_page
        worker,bridge,_=self.worker()
        await label_task_page(worker,'task')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='old-source',role='task')

    async def test_screening_slot_and_legacy_browser_behavior_are_unchanged(self):
        from app.task_page_labels import label_task_page
        worker,bridge,old=self.worker();worker._task_page_slot=2
        await label_task_page(worker,'screening')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='old-source',role='screening',slot=2)
        bridge.call.reset_mock();old.send.reset_mock();worker.profile_id='legacy-profile'
        await label_task_page(worker,'task')
        bridge.call.assert_not_called();old.send.assert_not_awaited()
