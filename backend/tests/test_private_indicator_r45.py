"""Privacy evidence must not stall a ready profile on detachable optional nodes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


HEADINGS = 'main h1, main h2, main h3, main [role="heading"]'
TITLES = 'main header svg title, main svg title'
CONTROLS = 'main header button, main header [role="button"], header main button'


class Snapshot:
    def __init__(self, values=(), *, failure=None, pending=False):
        self.values = list(values)
        self.failure, self.pending = failure, pending
        self.reads = 0
        self.entered = asyncio.Event()

    async def count(self):
        return 0

    async def evaluate_all(self, expression):
        self.reads += 1
        self.entered.set()
        if self.pending:
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure
        return self.values

    def nth(self, _index):
        raise AssertionError('A snapshot-capable adapter must not reread detachable nodes')


class LegacyNodes:
    def __init__(self, *, pending=False):
        self.pending = pending
        self.timeouts = []

    async def count(self):
        return 30 if self.pending else 2

    def nth(self, index):
        owner = self

        class Node:
            async def is_visible(self):
                return True

            async def inner_text(self, **kwargs):
                owner.timeouts.append(kwargs.get('timeout'))
                if owner.pending:
                    await asyncio.Event().wait()
                if index == 0:
                    raise TimeoutError('node detached after count')
                return 'Private account'

        return Node()


class PrivateIndicatorR45Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self, *, headings=None, titles=None, controls=None):
        worker = PlaywrightWorker(None)
        surfaces = {
            HEADINGS: headings or Snapshot(),
            TITLES: titles or Snapshot(),
            CONTROLS: controls or Snapshot(),
        }
        worker.page = SimpleNamespace(locator=lambda selector: surfaces.get(selector, Snapshot()))
        return worker

    async def test_private_heading_is_read_in_one_snapshot(self):
        headings = Snapshot(['Account name', 'Private account'])
        controls = Snapshot(['Follow'])
        self.assertTrue(await self.worker(headings=headings, controls=controls)._has_visible_private_indicator())
        self.assertEqual(1, headings.reads)
        self.assertEqual(0, controls.reads)

    async def test_requested_header_control_keeps_private_evidence(self):
        controls = Snapshot(['Message', 'Requested'])
        self.assertTrue(await self.worker(controls=controls)._has_visible_private_indicator())
        self.assertEqual(1, controls.reads)

    async def test_public_empty_heading_and_follow_button_are_not_private(self):
        worker = self.worker(
            headings=Snapshot(['sample_reader01', '这里空荡荡~', '为你推荐']),
            controls=Snapshot(['关注']),
        )
        self.assertFalse(await worker._has_visible_private_indicator())

    async def test_unreadable_privacy_snapshot_is_not_false_public_evidence(self):
        for surface in ('headings', 'controls'):
            with self.subTest(surface=surface):
                worker = self.worker(**{surface: Snapshot(failure=RuntimeError('DOM generation changed'))})
                with self.assertRaises(WorkerExecutionError) as caught:
                    await worker._has_visible_private_indicator()
                self.assertEqual('instagram_profile_not_ready', caught.exception.code)

    async def test_browser_closed_during_snapshot_retains_connection_failure(self):
        worker = self.worker(headings=Snapshot(failure=RuntimeError(
            'Target page, context or browser has been closed'
        )))
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._has_visible_private_indicator()
        self.assertEqual('worker_not_connected', caught.exception.code)

    async def test_precise_worker_failure_is_not_replaced_by_not_ready(self):
        worker = self.worker(headings=Snapshot(failure=WorkerExecutionError(
            'blocked', reason='instagram_challenge', pause_required=True,
        )))
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._has_visible_private_indicator()
        self.assertEqual('instagram_challenge', caught.exception.code)

    async def test_browser_closed_in_initial_indicator_query_keeps_failure(self):
        class ClosedIndicator(Snapshot):
            async def count(self):
                raise RuntimeError('Target page, context or browser has been closed')

        worker = PlaywrightWorker(None)
        worker.page = SimpleNamespace(locator=lambda _selector: ClosedIndicator())
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._has_visible_private_indicator()
        self.assertEqual('worker_not_connected', caught.exception.code)

    async def test_stalled_snapshot_has_a_bounded_privacy_budget(self):
        worker = self.worker(headings=Snapshot(pending=True))
        worker.collection_dom_operation_timeout_seconds = 0.05
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker._has_visible_private_indicator(), timeout=2)
        self.assertEqual('instagram_profile_not_ready', caught.exception.code)

    async def test_legacy_detached_heading_does_not_hide_later_private_heading(self):
        headings = LegacyNodes()
        self.assertTrue(await self.worker(headings=headings)._has_visible_private_indicator())
        self.assertEqual(2, len(headings.timeouts))
        self.assertTrue(all(0 < value <= 250 for value in headings.timeouts))

    async def test_many_stalled_legacy_nodes_do_not_accumulate_profile_timeout(self):
        headings = LegacyNodes(pending=True)
        worker = self.worker(headings=headings)
        worker.collection_dom_operation_timeout_seconds = 0.05
        with self.assertRaises(WorkerExecutionError) as caught:
            await asyncio.wait_for(worker._has_visible_private_indicator(), timeout=2)
        self.assertEqual('instagram_profile_not_ready', caught.exception.code)
        self.assertEqual(1, len(headings.timeouts))

    async def test_stop_cancels_privacy_snapshot_without_a_false_result(self):
        headings = Snapshot(pending=True)
        task = asyncio.create_task(self.worker(headings=headings)._has_visible_private_indicator())
        try:
            await asyncio.wait_for(headings.entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
