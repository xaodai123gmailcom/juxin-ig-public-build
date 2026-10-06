"""Retry an incomplete source without replacing or overbooking retained children."""
import asyncio
import unittest

import test_parallel_relation_pipeline as fixtures
from completion_wait import wait_for_collection_operation
from app.playwright_worker import CollectionOutcome, WorkerExecutionError


class _Child:
    def __init__(self, parent, name, *, unavailable=False):
        self.parent, self.name = parent, name
        self.unavailable = unavailable
        self.disconnect_calls = 0

    async def screen(self, username):
        self.parent.reads.append((self.name, username))
        if self.unavailable or (username.startswith('held.') and self.name.startswith('fresh.')):
            raise WorkerExecutionError('profile is temporarily unavailable',
                                       reason='instagram_content_not_visible')

    async def disconnect(self):
        self.disconnect_calls += 1


class _Parent:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    supports_parallel_screening_tab = True

    def __init__(self, *, fail_source=False, source_total=2):
        self._deferred_screening_workers = {}
        self.children = []
        self.reads = []
        self.peak_children = 0
        self.create_calls = 0
        self.fail_source = fail_source
        self.source_total = source_total

    def retain(self, username, *, unavailable=False):
        child = _Child(self, 'retained.'+username, unavailable=unavailable)
        self._deferred_screening_workers[username] = child
        self.children.append(child)
        return child

    async def create_parallel_screening_worker(self):
        self.create_calls += 1
        child = _Child(self, f'fresh.{self.create_calls}')
        self.children.append(child)
        self.peak_children = max(self.peak_children, sum(c.disconnect_calls == 0 for c in self.children))
        return child

    async def collect_followers(self, username, *, candidate_sink, **kwargs):
        await candidate_sink(['healthy.next'])
        if self.fail_source:
            raise WorkerExecutionError('hover card not ready', reason='instagram_hover_card_unavailable')
        return CollectionOutcome('followers', [], source_total=self.source_total)

    async def screen(self, username):
        self.reads.append(('source', username))


class RetainedPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = fixtures.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = fixtures.ParallelRelationPipelineTests._task_and_control

    def setup_retry(self, slots, *, retained_count=1, unavailable=False, fail_source=False):
        task, target, control = self._task_and_control(slots)
        parent = _Parent(fail_source=fail_source, source_total=retained_count+1)
        for index in range(retained_count):
            parent.retain(f'held.{index}', unavailable=unavailable)
        self.service.append_task_mode_candidates(self.user['id'], task['id'], target['id'],
                                                'followers', list(parent._deferred_screening_workers))
        manager = fixtures._PipelineManager(self.service, fixtures._NoopBitBrowser())
        return task, target, control, parent, manager

    async def run_mode(self, parts):
        task, target, control, parent, manager = parts
        checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
        return await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                'followers', task['settings'], checkpoint,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )

    async def test_one_slot_retries_its_retained_page_without_opening_another(self):
        parts = self.setup_retry(1)
        parent = parts[3]
        retained = parent._deferred_screening_workers['held.0']
        result = await self.run_mode(parts)
        self.assertEqual(0, parent.create_calls)
        self.assertIn(('retained.held.0', 'held.0'), parent.reads)
        self.assertEqual(1, retained.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)
        self.assertEqual((0, 2), (result['pending'], result['recorded']))

    async def assert_slot_bound(self, slots):
        parts = self.setup_retry(slots, retained_count=slots-1)
        parent = parts[3]
        old = list(parent._deferred_screening_workers.values())
        result = await self.run_mode(parts)
        self.assertLessEqual(parent.peak_children, slots)
        self.assertEqual(1, parent.create_calls)
        for i, child in enumerate(old):
            self.assertIn((f'retained.held.{i}', f'held.{i}'), parent.reads)
            self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)
        self.assertEqual(0, result['pending'])

    async def test_retained_page_counts_toward_two_slot_limit(self):
        await self.assert_slot_bound(2)

    async def test_retained_pages_count_toward_three_slot_limit(self):
        await self.assert_slot_bound(3)

    async def test_repeated_source_and_child_failures_keep_the_original_page_and_pending_row(self):
        parts = self.setup_retry(2, unavailable=True, fail_source=True)
        task, target, _, parent, manager = parts
        retained = parent._deferred_screening_workers['held.0']
        for attempt in range(2):
            with self.assertRaises(WorkerExecutionError):
                await self.run_mode(parts)
            self.assertIs(retained, parent._deferred_screening_workers['held.0'])
            self.assertEqual(0, retained.disconnect_calls)
            self.assertLessEqual(parent.peak_children, 2)
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
            pending = self.service.list_pending_task_mode_candidates(self.user['id'], task['id'], target['id'], 'followers')
            self.assertEqual(['held.0'], [item['username'] for item in pending])
        self.assertEqual(2, parent.reads.count(('retained.held.0', 'held.0')))
        self.assertEqual(1, sum(name == 'healthy.next' for _, name in parent.reads))
        parent.fail_source = False
        retained.unavailable = False
        result = await self.run_mode(parts)
        self.assertTrue(result['discovery_complete'])
        self.assertEqual((0, 2), (result['pending'], result['recorded']))
        self.assertEqual(1, retained.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)
        self.assertLessEqual(parent.peak_children, 2)
        self.assertEqual(1, sum(name == 'healthy.next' for _, name in parent.reads))

    async def test_all_slots_retained_and_unread_still_process_healthy_new_rows(self):
        parts = self.setup_retry(1, unavailable=True)
        task, target, _, parent, manager = parts
        with self.assertRaises(WorkerExecutionError) as raised:
            await self.run_mode(parts)
        self.assertEqual(0, parent.create_calls)
        self.assertIn(('source', 'healthy.next'), parent.reads)
        self.assertEqual(1, parent.reads.count(('retained.held.0', 'held.0')))
        self.assertTrue(raised.exception.details['source_discovery_complete'])
        stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')
        self.assertEqual((1, 1), (stats['pending'], stats['recorded']))


if __name__ == '__main__':
    unittest.main()
