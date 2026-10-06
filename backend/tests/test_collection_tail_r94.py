"""Read delayed list rows in place; never restart or rewind to fill a quota."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import test_relation_completion_r43 as fixtures
from app.collection_surface import RELATION_ROWS_SCRIPT
from app.collection_coverage import describe_coverage
from app.playwright_worker import WorkerExecutionError, _visible_relation_count_estimate


class TailWorker(fixtures.Worker):
    def __init__(self, *, header=100):
        super().__init__()
        self.header = header
        self.collection_loading_grace_seconds = 4

    def _collection_monotonic(self):
        return self.logical

    async def _navigate_profile(self, target):
        return target

    async def _visible_relation_count(self, target, relation):
        return _visible_relation_count_estimate(str(self.header), self.header)

    async def _read_relation_hover_preview(self, dialog, username):
        return {'username': username, 'followers': 100, 'following': 20, 'posts': 10}

    async def _open_relation_surface(self, target, relation):
        return self.surface


class TailSurface:
    def __init__(self, worker, count=99, *, late_at=3, recommendations=False):
        self.worker = worker
        worker.surface = self
        self.count = count
        self.late_at = late_at
        self.recommendations = recommendations
        self.resets = 0
        self.measure_times = []
        self.advances = 0

    async def evaluate(self, script):
        if script == RELATION_ROWS_SCRIPT:
            self.worker.logical += .25
            count = self.worker.header if self.late_at is not None and self.worker.logical >= self.late_at else self.count
            return fixtures.frame(*(f'user_{i}' for i in range(count)), recommendations=self.recommendations)
        if 'relation-action: reset' in script:
            self.resets += 1
            return {'valid': True, 'top': 0}
        if 'relation-action: advance' in script:
            self.advances += 1
        if 'relation-action: measure' in script:
            self.measure_times.append(self.worker.logical)
        return {'valid': True, 'top': 600, 'height': 1000, 'client': 400,
                'bottom': True, 'moved': False}

    async def inner_text(self, **kwargs):
        return 'Followers'


class CollectionTailR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_child_failure_resumes_exact_source_frame_without_navigation_or_reset(self):
        worker = TailWorker(header=3)
        worker.page = SimpleNamespace(url='https://www.instagram.com/source/')
        surface = TailSurface(worker, count=3, late_at=None)
        worker._navigate_profile = AsyncMock()
        worker._replace_stuck_page_once = AsyncMock(side_effect=AssertionError('child failure replaced parent'))
        async def failed_sink(batch):
            error = WorkerExecutionError('child profile unavailable', reason='instagram_profile_not_ready')
            error.details['recovery_scope'] = 'screening_child'
            raise error
        with self.assertRaises(WorkerExecutionError):
            await worker._collect_relation('source', relation='followers', limit=None, candidate_sink=failed_sink)
        self.assertEqual(1, surface.resets)
        saved = set()
        async def sink(batch): saved.update(batch); return len(saved)
        outcome = await worker._collect_relation('source', relation='followers', limit=None, candidate_sink=sink)
        self.assertEqual(3, outcome.candidate_count)
        self.assertEqual(1, surface.resets)
        self.assertEqual(1, worker._navigate_profile.await_count)
        worker._replace_stuck_page_once.assert_not_awaited()
        self.assertIsNone(worker._paused_relation_surface)

    async def test_replaced_page_cannot_reuse_previous_child_failure_surface(self):
        worker = TailWorker(header=3)
        surface = TailSurface(worker, count=3, late_at=None)
        worker._paused_relation_surface = (object(), 'source', 'followers', object(), 9000)
        worker._navigate_profile = AsyncMock()
        saved = set()
        async def sink(batch): saved.update(batch); return len(saved)
        outcome = await worker._collect_relation_once('source', relation='followers', limit=None,
            candidate_sink=sink, capture_source_profile=False)
        self.assertEqual(3, outcome.source_total)
        self.assertEqual(1, surface.resets)
        worker._navigate_profile.assert_awaited_once()

    async def collect(self, worker, *, initial=(), relation='followers', sink_error=False, hover=False):
        saved = set(initial)
        self.saved = saved
        batches = []

        async def sink(batch, previews=None):
            if sink_error:
                raise RuntimeError('controlled durable write failure')
            saved.update(batch)
            batches.append(list(batch))
            return len(saved)

        outcome = await asyncio.wait_for(worker._collect_relation_once(
            'source', relation=relation, limit=None, candidate_sink=sink,
            initial_candidate_count=len(saved), capture_source_profile=False,
            hover_precheck=hover,
        ), 5)
        self.assertEqual(1, worker.surface.resets, 'only the initial list opening may reset; no gap-driven rewind')
        return outcome, saved, batches

    async def test_100_header_99_rows_wait_for_delayed_last_row_in_place(self):
        worker = TailWorker()
        surface = TailSurface(worker)
        outcome, saved, _ = await self.collect(worker)
        self.assertEqual(100, outcome.candidate_count)
        self.assertEqual({f'user_{i}' for i in range(100)}, saved)
        self.assertGreaterEqual(surface.measure_times[0], 3)

    async def test_recommendation_tail_cannot_skip_five_delayed_source_rows(self):
        for relation in ('followers', 'following'):
            with self.subTest(relation=relation):
                worker = TailWorker()
                surface = TailSurface(worker, count=95, recommendations=True)
                outcome, saved, _ = await self.collect(worker, relation=relation)
                self.assertEqual(100, outcome.candidate_count)
                self.assertEqual(100, len(saved))
                self.assertGreaterEqual(surface.measure_times[0], 3)

    async def test_persistent_gap_is_bounded_and_reported_without_rewind(self):
        worker = TailWorker()
        surface = TailSurface(worker, count=95, late_at=None, recommendations=True)
        outcome, saved, _ = await self.collect(worker)
        self.assertEqual((100, 95), (outcome.source_total, outcome.candidate_count))
        self.assertGreaterEqual(surface.measure_times[0], 4)
        self.assertLess(worker.logical, 7)
        coverage = describe_coverage(dict(source_total=100, discovered=len(saved), processed=len(saved)), finished=True)
        self.assertEqual(('gap', 5, 5), (coverage['status'], coverage['remaining_count'], coverage['unobserved_count']))

    async def test_already_saved_99_rows_do_not_become_a_completion_quota(self):
        worker = TailWorker()
        TailSurface(worker)
        outcome, saved, batches = await self.collect(worker, initial={f'user_{i}' for i in range(99)})
        self.assertEqual(100, len(saved))
        self.assertEqual(100, outcome.candidate_count)
        self.assertEqual(1, sum(batch.count('user_99') for batch in batches))

    async def test_full_count_still_needs_healthy_bottom(self):
        worker = TailWorker()
        TailSurface(worker, count=100, late_at=None)
        worker.failure = 'instagram_network_unavailable'
        with self.assertRaises(WorkerExecutionError) as error:
            await self.collect(worker)
        self.assertEqual('instagram_network_unavailable', error.exception.code)

    async def test_failed_commit_cannot_scroll_or_confirm_end(self):
        worker = TailWorker()
        surface = TailSurface(worker)
        with self.assertRaisesRegex(RuntimeError, 'controlled durable write failure'):
            await self.collect(worker, sink_error=True)
        self.assertEqual(0, surface.advances)
        self.assertEqual([], surface.measure_times)

    async def test_hover_screening_also_waits_for_last_account_without_replay(self):
        worker = TailWorker()
        TailSurface(worker)
        outcome, saved, batches = await self.collect(worker, hover=True)
        self.assertEqual(100, outcome.candidate_count)
        self.assertEqual(100, len(saved))
        self.assertEqual(100, sum(map(len, batches)))

    async def test_stop_during_tail_wait_keeps_committed_rows_and_does_not_rewind(self):
        worker = TailWorker()
        surface = TailSurface(worker, late_at=None)

        async def stop():
            if worker.logical >= 1:
                raise asyncio.CancelledError

        worker.collection_checkpoint = stop
        with self.assertRaises(asyncio.CancelledError):
            await self.collect(worker)
        self.assertEqual(99, len(self.saved))
        self.assertEqual(1, surface.resets)
        self.assertEqual([], surface.measure_times)

    async def test_visible_loader_prevents_completion_even_when_counts_match(self):
        worker = TailWorker()
        worker.loading = True
        surface = TailSurface(worker, count=100, late_at=None)
        with self.assertRaises(WorkerExecutionError) as error:
            await self.collect(worker)
        self.assertEqual('instagram_followers_list_incomplete', error.exception.code)
        self.assertEqual(100, len(self.saved))
        self.assertEqual([], surface.measure_times)

    async def test_rows_repainted_during_durable_commit_are_read_before_scroll(self):
        worker = TailWorker(header=3)
        committed = set()
        events = []

        class RepaintingSurface:
            advanced = False

            async def evaluate(surface, script):
                if script == RELATION_ROWS_SCRIPT:
                    worker.logical += .25
                    names = ['last'] if surface.advanced else ['first', 'late'] if committed else ['first']
                    return fixtures.frame(*names)
                if 'relation-action: reset' in script:
                    raise AssertionError('automatic rewind is forbidden')
                if 'relation-action: advance' in script:
                    events.append('advance')
                    surface.advanced = True
                return {'valid': True, 'bottom': surface.advanced, 'moved': False,
                        'top': 100, 'height': 200, 'client': 100}

        async def sink(batch):
            committed.update(batch)
            events.extend(batch)
            await asyncio.sleep(0)
            return len(committed)

        await asyncio.wait_for(worker._read_visible_account_dialog(
            RepaintingSurface(), None, candidate_sink=sink,
            candidate_total_limit=3, expected_minimum=3,
        ), 5)
        self.assertEqual({'first', 'late', 'last'}, committed)
        self.assertLess(events.index('late'), events.index('advance'))


if __name__ == '__main__':
    unittest.main()
