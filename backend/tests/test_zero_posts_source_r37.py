"""Recommendation tails finish only healthy, actually read relation sources.

The production DOM script has Node execution tests. These fixtures exercise the
real Python snapshot adapter, spool acknowledgements and physical-end check.
"""
from __future__ import annotations

import asyncio
import unittest

from app.collection_surface import RELATION_ROWS_SCRIPT
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError, _visible_relation_count_estimate


class _Surface:
    def __init__(self, worker, frames, *, bottom=True, failure=None):
        self.worker = worker
        self.frames = frames
        self.reads = 0
        self.measures = 0
        self.bottom = bottom
        self.failure = failure
        self.events = []

    async def evaluate(self, script):
        if script == RELATION_ROWS_SCRIPT:
            if self.failure:
                raise self.failure
            self.worker.logical += .5
            frame = self.frames[min(self.reads, len(self.frames) - 1)]
            self.reads += 1
            observed = tuple(frame['hrefs']) if isinstance(frame, dict) and isinstance(frame.get('hrefs'), list) else frame
            self.events.append(('read', observed))
            return frame
        geometry = {'valid': True, 'bottom': self.bottom, 'top': 600,
                    'height': 1000, 'client': 400, 'moved': False}
        if 'relation-action: measure' in script:
            self.measures += 1
            self.events.append(('measure', dict(geometry)))
        return geometry

    async def inner_text(self, **_kwargs):
        return 'Suggested for you\n查看所有推荐用户'


class _Worker(PlaywrightWorker):
    def __init__(self, *, loading=False, guard_error=None):
        super().__init__(None)
        self.logical = 0.
        self.collection_loading_grace_seconds = 2
        self.collection_poll_interval_seconds = 0
        self.loading = loading
        self.guard_error = guard_error

    def _collection_monotonic(self):
        return self.logical + super()._collection_monotonic()

    async def _guard(self):
        if self.guard_error:
            raise self.guard_error

    async def _has_visible_relation_loading_indicator(self, _dialog):
        return self.loading

    async def _relation_surface_failure(self, _dialog):
        return None


def snapshot(*names, recommendations=True):
    return {'hrefs': [f'/{name}/' for name in names],
            'recommendations_reached': recommendations}


class ZeroPostsSourceR37Tests(unittest.IsolatedAsyncioTestCase):
    def assert_healthy_final_snapshot(self, surface):
        """Completion follows durable rows and a freshly revalidated bottom.

        r44 added a final measurement after refreshing the rows. Checking a fixed
        total of two measurements would reject that protection, while only
        increasing the expected count would miss a measurement in the wrong order.
        """
        events = surface.events
        self.assertEqual('complete', events[-1][0])
        final_read = max(index for index, event in enumerate(events) if event[0] == 'read')
        before_read = [(index, value) for index, (kind, value) in enumerate(events[:final_read])
                       if kind == 'measure']
        after_read = [(index, value) for index, (kind, value) in enumerate(events)
                      if index > final_read and kind == 'measure']
        self.assertGreaterEqual(len(before_read), 2, 'prove a settled bottom before refreshing rows')
        self.assertTrue(after_read, 'revalidate the bottom after the final DOM refresh')
        measurements = [value for _, value in before_read[-2:]] + [after_read[-1][1]]
        for value in measurements:
            self.assertTrue(value['valid'])
            self.assertTrue(value['bottom'])
            self.assertEqual(tuple(measurements[0][key] for key in ('height', 'client', 'top')),
                             tuple(value[key] for key in ('height', 'client', 'top')))
        commits = [index for index, event in enumerate(events) if event[0] == 'commit']
        self.assertTrue(commits, 'source rows must receive a durable sink acknowledgement')
        self.assertLess(max(commits), final_read, 'refresh the final frame after its rows are durable')
        self.assertLess(final_read, after_read[-1][0])
        self.assertLess(after_read[-1][0], len(events) - 1)

    async def run_source(self, worker, surface, *, initial=470, kind='followers'):
        saved = set()
        batches = []

        async def sink(batch):
            batches.append(list(batch))
            saved.update(batch)
            surface.events.append(('commit', tuple(batch)))
            return {'total': initial + len(saved)}

        result = await asyncio.wait_for(worker._read_visible_account_dialog(
            surface, None, surface_kind=kind, candidate_sink=sink,
            initial_candidate_count=initial,
            expected_minimum=_visible_relation_count_estimate('818', 818).completion_floor,
        ), 5)
        surface.events.append(('complete', None))
        return result, saved, batches

    async def test_818_header_can_finish_real_474_at_confirmed_recommendation_tail(self):
        worker = _Worker()
        surface = _Surface(worker, [snapshot('real_a', 'real_b', 'real_c', 'real_d')])
        result, saved, batches = await self.run_source(worker, surface)
        self.assertEqual([], result)
        self.assertEqual({'real_a', 'real_b', 'real_c', 'real_d'}, saved)
        self.assertEqual(1, len(batches))
        self.assert_healthy_final_snapshot(surface)

    async def test_following_collection_uses_same_healthy_tail_rule(self):
        worker = _Worker()
        surface = _Surface(worker, [snapshot('real_following')])
        _, saved, _ = await self.run_source(worker, surface, kind='following')
        self.assertEqual({'real_following'}, saved)
        self.assert_healthy_final_snapshot(surface)

    async def test_short_list_without_recommendations_finishes_at_confirmed_bottom(self):
        worker = _Worker()
        surface = _Surface(worker, [snapshot('real_a', recommendations=False)])
        # r43: the profile count no longer vetoes a fully settled physical end.
        _, saved, _ = await self.run_source(worker, surface)
        self.assertEqual({'real_a'}, saved)
        self.assert_healthy_final_snapshot(surface)

    async def test_recommendations_above_physical_bottom_do_not_finish(self):
        worker = _Worker()
        surface = _Surface(worker, [snapshot('real_a')], bottom=False)
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_source(worker, surface)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)

    async def test_visible_loading_prevents_recommendation_tail_completion(self):
        worker = _Worker(loading=True)
        surface = _Surface(worker, [snapshot('real_a')])
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_source(worker, surface)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        self.assertEqual(0, surface.measures)

    async def test_recommendations_only_cannot_complete_an_unread_source(self):
        for initial in (0, 470):
            with self.subTest(initial=initial):
                worker = _Worker()
                surface = _Surface(worker, [snapshot()])
                with self.assertRaises(WorkerExecutionError):
                    await self.run_source(worker, surface, initial=initial)
                self.assertLessEqual(surface.reads, worker.collection_initial_idle_rounds + 1,
                                'an unread source must stop with an error, not loop forever')

    async def test_final_repaint_is_persisted_before_natural_end(self):
        worker = _Worker()
        surface = _Surface(worker, [snapshot('first'), snapshot('last')])
        _, saved, _ = await self.run_source(worker, surface)
        self.assertEqual({'first', 'last'}, saved)
        self.assert_healthy_final_snapshot(surface)
        final_row_read = next(index for index, event in enumerate(surface.events)
                              if event == ('read', ('/last/',)))
        final_row_commit = next(index for index, event in enumerate(surface.events)
                                if event == ('commit', ('last',)))
        final_measure = max(index for index, event in enumerate(surface.events)
                            if event[0] == 'measure')
        self.assertLess(final_row_read, final_row_commit)
        self.assertLess(final_row_commit, final_measure,
                        'a virtualized final row must be durable before confirming completion')

    async def test_disappearing_recommendation_flag_does_not_leak_to_next_frame(self):
        worker = _Worker()
        # A non-bottom surface cannot finish once its recommendation hint vanishes.
        # A healthy stable bottom now finishes independently of header ratios.
        surface = _Surface(worker, [snapshot('first'), snapshot('last', recommendations=False)], bottom=False)
        with self.assertRaises(WorkerExecutionError):
            await self.run_source(worker, surface)
        self.assertFalse(worker._relation_recommendations_reached)

    async def test_network_guard_error_is_not_hidden_by_recommendation_tail(self):
        error = WorkerExecutionError('offline', reason='instagram_network_unavailable')
        worker = _Worker(guard_error=error)
        surface = _Surface(worker, [snapshot('real_a')])
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_source(worker, surface)
        self.assertEqual('instagram_network_unavailable', caught.exception.code)

    async def test_projection_failure_is_not_a_successful_empty_snapshot(self):
        worker = _Worker()
        worker._relation_recommendations_reached = True
        surface = _Surface(worker, [snapshot()], failure=RuntimeError('DOM gone'))
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._read_visible_account_hrefs(surface, surface_kind='followers')
        self.assertEqual('instagram_followers_list_not_rendered', caught.exception.code)
        self.assertFalse(worker._relation_recommendations_reached)

    async def test_malformed_projection_is_not_empty_success(self):
        for invalid in ({'hrefs': [None], 'recommendations_reached': True},
                        {'recommendations_reached': True}, [], False):
            with self.subTest(invalid=invalid):
                worker = _Worker()
                surface = _Surface(worker, [invalid])
                with self.assertRaises(WorkerExecutionError):
                    await worker._read_visible_account_hrefs(surface, surface_kind='followers')
                self.assertFalse(worker._relation_recommendations_reached)

    async def test_recycled_final_frames_cannot_extend_recovery_forever(self):
        worker = _Worker()
        class Recycled(_Surface):
            async def evaluate(self, script):
                if script == RELATION_ROWS_SCRIPT:
                    self.worker.logical += .5
                    self.reads += 1
                    return snapshot('saved_a' if self.reads % 2 else 'saved_b')
                return await super().evaluate(script)
        surface = Recycled(worker, [])
        # The first two rows are already durable. Recycled text is not new work.
        async def sink(_batch):
            return 470
        with self.assertRaises(WorkerExecutionError):
            await asyncio.wait_for(worker._read_visible_account_dialog(
                surface, None, candidate_sink=sink, initial_candidate_count=470,
                expected_minimum=809), 5)
        self.assertLess(surface.reads, 10)


if __name__ == '__main__':
    unittest.main()
