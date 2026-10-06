"""The posting viewport label targets the new page, never its borrowed source."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_publisher_entry as fixtures


class PostingViewportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    worker=fixtures.PostingPageLifecycleTests.worker

    async def test_label_new_target_before_navigation_then_activate(self):
        events=[]
        fresh=SimpleNamespace(goto=AsyncMock(side_effect=lambda *a,**k:events.append('goto')),close=AsyncMock())
        worker=self.worker(fresh);source=worker.page
        new_session=worker._new_active_page_session.return_value
        async def label(w,role,**kwargs):
            if kwargs.get('viewport_mode')=='posting':
                events.append('posting-label')
                self.assertIs(source,w.page)
                self.assertIs(new_session,kwargs['cdp_session'])
            else:events.append('activated-label')
        with patch('app.playwright_worker.label_task_page',side_effect=label):
            self.assertIs(fresh,await worker.open_posting_page())
        self.assertEqual(['posting-label','goto','activated-label'],events)
        source.close.assert_not_awaited()

    async def test_absent_new_session_never_uses_source_for_posting_label(self):
        fresh=SimpleNamespace(goto=AsyncMock(),close=AsyncMock())
        worker=self.worker(fresh);source_session=worker._cdp_session
        worker._new_active_page_session.return_value=None
        label=AsyncMock()
        with patch('app.playwright_worker.label_task_page',label):
            await worker.open_posting_page()
        self.assertTrue(label.await_args_list)
        for call in label.await_args_list:
            self.assertIsNone(call.kwargs.get('viewport_mode'))
            self.assertIsNone(call.kwargs.get('cdp_session'))
        self.assertIsNone(worker._cdp_session)
        source_session.detach.assert_awaited_once()


if __name__=='__main__':unittest.main()
