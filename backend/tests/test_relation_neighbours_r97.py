"""Per-account continuity checks; pure Python, also run in the Windows gate."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.collection_surface import relation_neighbour_status
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


def rows(names):
    return [dict(username=name, y=20 + index * 40) for index, name in enumerate(names)]


class RelationNeighbourIdentityTests(unittest.TestCase):
    def test_endpoints_middle_and_duplicate_links(self):
        for name in 'abc':
            self.assertEqual('confirmed', relation_neighbour_status('abc', name, rows('aabbcc')))
        self.assertEqual('confirmed', relation_neighbour_status('a', 'a', rows('a')))

    def test_missing_inserted_and_reordered_neighbours(self):
        self.assertEqual('missing_anchor', relation_neighbour_status('abc', 'b', rows('bc')))
        self.assertEqual('missing_anchor', relation_neighbour_status('abc', 'b', rows('ac')))
        self.assertEqual('neighbours_changed', relation_neighbour_status('abc', 'b', rows('acb')))
        self.assertEqual('neighbours_changed', relation_neighbour_status('abc', 'b', rows('axbc')))

    def test_identity_not_pixel_position_is_local_evidence(self):
        shifted = rows('abc')
        for row in shifted:
            row['y'] += 600
        self.assertEqual('confirmed', relation_neighbour_status('abc', 'b', shifted))

    def test_recommendation_churn_is_not_a_missing_source_neighbour(self):
        positions = rows('axbc')
        positions[1]['recommended'] = True
        self.assertEqual('confirmed', relation_neighbour_status('abc', 'b', positions))


class RelationNeighbourReaderTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, frames):
        worker = PlaywrightWorker(None)
        worker._collection_checkpoint = AsyncMock()
        worker._guard = AsyncMock()
        worker.collection_poll_interval_seconds = 0
        worker.collection_loading_grace_seconds = .2
        ticks = iter(index / 10 for index in range(10000))
        worker._collection_monotonic = lambda: next(ticks)
        states = iter(frames)
        last = frames[-1]
        async def read(*args, **kwargs):
            nonlocal last
            last = next(states, last)
            worker._relation_positioned_rows = rows(last)
            return [f'/{name}/' for name in last]
        worker._read_visible_account_hrefs = AsyncMock(side_effect=read)
        worker._read_relation_hover_preview = AsyncMock(side_effect=lambda dialog, name: {
            'username': name, 'posts': 1, 'followers': 2, 'following': 3,
            'evidence': 'relationship_hover_card'})
        return worker

    async def collect(self, worker, total=3, **kwargs):
        saved = []
        async def sink(names, evidence):
            saved.extend(name for name in names if name not in saved)
            return len(saved)
        dialog = SimpleNamespace(evaluate=AsyncMock())
        await worker._read_visible_account_dialog(dialog, None, candidate_sink=sink,
            candidate_total_limit=total, hover_precheck=True, **kwargs)
        dialog.evaluate.assert_not_awaited()
        return saved

    async def test_a_then_b_checks_a_c_and_recovers_delayed_neighbour(self):
        worker = self.worker(['abc', 'abc', 'abc', 'bc', 'abc', 'abc'])
        self.assertEqual(list('abc'), await self.collect(worker))
        self.assertEqual(8, worker._read_visible_account_hrefs.await_count)
        self.assertEqual(list('abc'), [call.args[1] for call in worker._read_relation_hover_preview.await_args_list])

    async def test_same_set_reorder_stabilizes_without_scrolling_or_dropping(self):
        worker = self.worker(['abc', 'abc', 'acb', 'acb', 'acb', 'acb'])
        self.assertEqual(list('abc'), await self.collect(worker, surface_kind='following'))
        self.assertEqual(12, worker._read_visible_account_hrefs.await_count)

    async def test_inserted_account_seen_between_items_is_consumed_before_scroll(self):
        worker = self.worker(['abc', 'axbc', 'axbc', 'axbc', 'axbc', 'axbc', 'axbc'])
        self.assertEqual(list('abcx'), await self.collect(worker, total=4))

    async def test_transient_insertion_reappears_and_uses_its_original_neighbours(self):
        worker = self.worker(['abc', 'axbc', 'axbc'] + ['abc'] * 7 + ['axbc'])
        self.assertEqual(list('abcx'), await self.collect(worker, total=4))

    async def test_neighbour_disappearing_during_final_hover_recovers_before_return(self):
        worker = self.worker(['ab', 'ab', 'ab', 'ab', 'b', 'ab'])
        self.assertEqual(list('ab'), await self.collect(worker, total=2))
        self.assertEqual(6, worker._read_visible_account_hrefs.await_count)

    async def test_neighbour_lost_during_final_hover_preserves_confirmed_rows_and_pauses(self):
        worker = self.worker(['ab', 'ab', 'ab', 'ab', 'b'])
        checkpoint = {}
        saved = []
        async def progress(payload):
            checkpoint.update(payload)
        async def sink(names, evidence):
            saved.extend(names)
            return len(saved)
        with self.assertRaises(WorkerExecutionError):
            await worker._read_visible_account_dialog(SimpleNamespace(), None,
                candidate_sink=sink, candidate_total_limit=2, hover_precheck=True,
                scan_progress_sink=progress)
        self.assertEqual(list('ab'), saved)
        self.assertEqual([], checkpoint['pending_relation_usernames'])

    async def test_interrupted_insertion_is_checkpointed_and_consumed_after_restart(self):
        checkpoint = {}
        saved = set()
        async def progress(payload):
            checkpoint.update(payload)
        async def sink(names, evidence):
            saved.update(names)
            return len(saved)
        worker = self.worker(['abc', 'axbc', 'axbc', 'abc'])
        with self.assertRaises(WorkerExecutionError):
            await worker._read_visible_account_dialog(SimpleNamespace(), None,
                candidate_sink=sink, hover_precheck=True, scan_progress_sink=progress)
        self.assertEqual(set('abc'), saved)
        self.assertEqual(['x'], checkpoint['pending_relation_usernames'])
        # A new reader and new frame; existing saved rows go through durable
        # duplicate handling, while the unconfirmed identity stays uncounted.
        restored = self.worker(['axbc'])
        restored.hover_duplicate_check = AsyncMock(side_effect=lambda name: name in saved)
        await restored._read_visible_account_dialog(SimpleNamespace(), None,
            candidate_sink=sink, candidate_total_limit=4, initial_candidate_count=3,
            hover_precheck=True, scan_progress_sink=progress,
            initial_pending_relation_usernames=checkpoint['pending_relation_usernames'])
        self.assertEqual(set('abcx'), saved)
        self.assertEqual([], checkpoint['pending_relation_usernames'])
        self.assertEqual(['x'], [call.args[1] for call in restored._read_relation_hover_preview.await_args_list])

    async def test_restored_unresolved_identity_prevents_false_natural_completion(self):
        worker = self.worker(['abc'])
        read = worker._read_visible_account_hrefs.side_effect
        async def recommended_tail(*args, **kwargs):
            result = await read(*args, **kwargs)
            worker._relation_recommendations_reached = True
            return result
        worker._read_visible_account_hrefs.side_effect = recommended_tail
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        worker._confirm_relation_list_end = AsyncMock(return_value=True)
        worker._relation_list_end_is_current = AsyncMock(return_value=True)
        checkpoint = {}
        async def progress(payload):
            checkpoint.update(payload)
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.collect(worker, total=10, initial_pending_relation_usernames=['x'],
                scan_progress_sink=progress)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        self.assertEqual(['x'], checkpoint['pending_relation_usernames'])

    async def test_same_source_page_replacement_carries_pending_identity(self):
        worker = self.worker(['abc'])
        worker._replace_stuck_page_once = AsyncMock()
        worker._finish_page_recovery = AsyncMock()
        attempts = []
        completed = object()
        async def attempt(target, **kwargs):
            attempts.append(kwargs['initial_pending_relation_usernames'])
            if len(attempts) == 1:
                await kwargs['progress_sink']({'pending_relation_usernames': ['x']})
                raise WorkerExecutionError('temporary gap', reason='instagram_followers_list_incomplete')
            await kwargs['progress_sink']({'pending_relation_usernames': []})
            return completed
        worker._collect_relation_once = AsyncMock(side_effect=attempt)
        self.assertIs(completed, await worker.collect_followers('source', limit=None,
            initial_pending_relation_usernames=['prior']))
        self.assertEqual([['prior'], ['x']], attempts)
        worker._replace_stuck_page_once.assert_awaited_once()

    async def test_zero_header_does_not_override_restored_pending_identity(self):
        worker = self.worker([''])
        worker._navigate_profile = AsyncMock()
        worker._assert_relation_source = Mock()
        worker._visible_relation_count = AsyncMock(return_value=0)
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._collect_relation_once('source', relation='followers', limit=None,
                candidate_sink=AsyncMock(), initial_candidate_count=3,
                initial_pending_relation_usernames=['x'], hover_precheck=True)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)

    async def test_absent_restored_durable_identity_is_reconciled_before_zero_header(self):
        for use_durable in (True, False):
            with self.subTest(use_durable=use_durable):
                worker = self.worker([''])
                worker._navigate_profile = AsyncMock()
                worker._assert_relation_source = Mock()
                worker._visible_relation_count = AsyncMock(return_value=0)
                worker._source_profile_metrics = AsyncMock(return_value=None)
                if use_durable:
                    worker.relation_pending_confirmed_usernames = AsyncMock(return_value=['x'])
                else:
                    worker.hover_duplicate_check = AsyncMock(return_value=True)
                progress = AsyncMock()
                outcome = await worker._collect_relation_once('source', relation='followers', limit=None,
                    candidate_sink=AsyncMock(), initial_candidate_count=3, progress_sink=progress,
                    initial_pending_relation_usernames=['x'], hover_precheck=True)
                self.assertEqual(3, outcome.candidate_count)
                self.assertEqual({'pending_relation_usernames': []}, progress.await_args_list[0].args[0])

    async def test_absent_restored_duplicate_is_cleared_without_candidate_inflation(self):
        worker = self.worker(['abc'])
        worker.hover_duplicate_check = AsyncMock(side_effect=lambda name: name == 'x')
        progress = AsyncMock()
        self.assertEqual(list('abc'), await self.collect(worker,
            initial_pending_relation_usernames=['x'], scan_progress_sink=progress))
        self.assertEqual({'pending_relation_usernames': []}, progress.await_args_list[0].args[0])
        self.assertEqual('x', worker.hover_duplicate_check.await_args_list[0].args[0])

    async def test_explicit_empty_surface_does_not_override_restored_pending_identity(self):
        worker = self.worker([''])
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        worker._confirm_relation_list_end = AsyncMock(return_value=True)
        worker._relation_surface_failure = AsyncMock(return_value=None)
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._read_visible_account_dialog(
                SimpleNamespace(inner_text=AsyncMock(return_value='No followers'), evaluate=AsyncMock()),
                None, candidate_sink=AsyncMock(return_value=0), known_zero=True,
                initial_pending_relation_usernames=['x'], hover_precheck=True)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)

    async def test_transient_insertion_cannot_disappear_silently_before_scroll(self):
        worker = self.worker(['abc', 'axbc', 'axbc', 'abc', 'abc', 'abc'])
        saved = []
        async def sink(names, evidence):
            saved.extend(names)
            return len(set(saved))
        dialog = SimpleNamespace(evaluate=AsyncMock())
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._read_visible_account_dialog(dialog, None, candidate_sink=sink,
                hover_precheck=True)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        self.assertEqual(list('abc'), saved)
        dialog.evaluate.assert_not_awaited()

    async def test_known_duplicate_is_still_an_anchor_and_is_not_hovered(self):
        worker = self.worker(['abc', 'abc', 'bc', 'abc', 'abc'])
        worker.hover_duplicate_check = AsyncMock(side_effect=lambda name: name == 'a')
        self.assertEqual(set('abc'), set(await self.collect(worker)))
        self.assertEqual(list('bc'), [call.args[1] for call in worker._read_relation_hover_preview.await_args_list])
        self.assertEqual(7, worker._read_visible_account_hrefs.await_count)

    async def test_missing_neighbour_is_bounded_and_keeps_already_committed_a(self):
        worker = self.worker(['abc', 'abc', 'bc'])
        saved = []
        async def sink(names, evidence):
            saved.extend(names)
            return len(saved)
        dialog = SimpleNamespace(evaluate=AsyncMock())
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._read_visible_account_dialog(dialog, None, candidate_sink=sink,
                hover_precheck=True)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        self.assertEqual(['a'], saved)
        self.assertLess(worker._read_visible_account_hrefs.await_count, 30)
        dialog.evaluate.assert_not_awaited()

    async def test_continuously_reordered_frame_cannot_retry_forever(self):
        worker = self.worker(['abc', 'abc'] + ['acb', 'bac'] * 30)
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.collect(worker)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        self.assertLess(worker._read_visible_account_hrefs.await_count, 30)

    async def test_restart_replays_known_rows_and_establishes_fresh_neighbours(self):
        worker = self.worker(['abc', 'abc', 'abc', 'abc'])
        self.assertEqual(list('abc'), await self.collect(worker, initial_resume_tail=['a', 'b']))

    async def test_text_only_adapter_retains_existing_behavior(self):
        worker = self.worker(['abc'])
        worker._read_visible_account_hrefs = AsyncMock(return_value=['/a/', '/b/', '/c/'])
        self.assertEqual(list('abc'), await self.collect(worker))
        self.assertEqual(1, worker._read_visible_account_hrefs.await_count)

    async def test_post_likers_do_not_enter_relationship_neighbour_guard(self):
        worker = self.worker(['abc'])
        saved = []
        async def sink(names):
            saved.extend(names)
            return len(saved)
        await worker._read_visible_account_dialog(SimpleNamespace(), None, candidate_sink=sink,
            candidate_total_limit=3, surface_kind='post_likers')
        self.assertEqual(list('abc'), saved)
        self.assertEqual(1, worker._read_visible_account_hrefs.await_count)


if __name__ == '__main__':
    unittest.main()
