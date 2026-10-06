"""Posting viewport bridge metadata is exact-target and remains optional."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.task_page_labels import label_task_page

class PostingViewportLabelsTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        bridge=SimpleNamespace(call=Mock())
        session=SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'old-source'}}))
        worker=SimpleNamespace(profile_id='native:profile',_cdp_session=session,
            bitbrowser=SimpleNamespace(native=SimpleNamespace(bridge=bridge)))
        return worker,bridge,session

    async def test_explicit_candidate_gets_posting_mode_without_touching_source_session(self):
        worker,bridge,old=self.worker()
        candidate=SimpleNamespace(send=AsyncMock(return_value={'targetInfo':{'targetId':'new-composer'}}))
        await label_task_page(worker,'task',viewport_mode='posting',cdp_session=candidate)
        old.send.assert_not_awaited()
        candidate.send.assert_awaited_once_with('Target.getTargetInfo')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='new-composer',role='task',viewport_mode='posting')
        self.assertIs(worker._cdp_session,old)

    async def test_normal_relabel_omits_mode_so_frozen_posting_geometry_is_preserved(self):
        worker,bridge,old=self.worker()
        await label_task_page(worker,'task')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='old-source',role='task')

    async def test_screening_slot_and_legacy_browser_behavior_are_unchanged(self):
        worker,bridge,old=self.worker();worker._task_page_slot=2
        await label_task_page(worker,'screening')
        bridge.call.assert_called_once_with('label-task-page',profile='native:profile',target='old-source',role='screening',slot=2)
        bridge.call.reset_mock();old.send.reset_mock();worker.profile_id='legacy-profile'
        await label_task_page(worker,'task',viewport_mode='posting')
        bridge.call.assert_not_called();old.send.assert_not_awaited()

if __name__=='__main__':unittest.main()
