"""Screenshot-sized queues and source-local failures must not lose pending work."""
import asyncio
from collections import Counter
import unittest

from completion_wait import wait_for_collection_operation

from app.collection_coverage import describe_coverage
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.execution_manager import ExecutionManager
from app.errors import ConflictError
from test_core import FakeCollectionWorker
import test_parallel_relation_pipeline as fixture


class MissingCandidateDrainR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = fixture.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = fixture.ParallelRelationPipelineTests._task_and_control

    async def test_screenshot_backlogs_drain_without_inventing_header_only_accounts(self):
        for header, discovered, processed, duplicates in ((537, 411, 307, 94), (369, 275, 170, 33)):
            with self.subTest(header=header):
                task, target, control = self._task_and_control(3)
                names = [f'shot{header}_{i}' for i in range(discovered)]
                for start in range(0, discovered, 100):
                    self.service.append_task_mode_candidates(self.user['id'], task['id'], target['id'],
                        'followers', names[start:start + 100])
                for index, name in enumerate(names[:processed]):
                    self.service.finish_task_mode_candidate(self.user['id'], task['id'], target['id'],
                        'followers', name, state='deduped' if index < duplicates else 'recorded')
                state = fixture._PipelineState()
                state.allow_screen.set()
                class Source(fixture._RelationParent):
                    supports_single_candidate_handoff = True
                    collection_pipeline = PlaywrightWorker.collection_pipeline
                    async def collect_followers(self, *args, **kwargs):
                        raise AssertionError('confirmed source must not be rediscovered to fill a header quota')
                source = Source(state, child=fixture._ScreeningChild(state))
                manager = fixture._PipelineManager(self.service, fixture._NoopBitBrowser())
                stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')
                await manager._save_candidate_progress_checkpoint(control, target['id'], 'followers', stats,
                    discovery_complete=True, source_total=header)
                checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
                # This is a completeness/ownership fixture, not a throughput SLA.
                # Retain a 15-second deadline for no durable progress, allowing
                # real durable writes to advance on slow Windows build disks.
                # Only the pipeline's return (after child cleanup) is completion.
                outcome = await wait_for_collection_operation(
                    lambda: manager._execute_candidate_spooled_mode(
                        control, source, target, 'followers', task['settings'], checkpoint),
                    manager, self.service, self.user['id'], task['id'],
                    idle_timeout=15, overall_timeout=120,
                )
                self.assertEqual(1, source.child.disconnect_calls)
                self.assertEqual((discovered, discovered - duplicates, duplicates, 0),
                    tuple(outcome[key] for key in ('total', 'recorded', 'deduped', 'pending')))
                self.assertEqual(Counter(names[processed:]), Counter(name for _, name in state.screened))
                coverage = describe_coverage(dict(source_total=header, discovered=outcome['total'],
                    processed=outcome['recorded'] + outcome['deduped']), finished=True)
                self.assertEqual(('gap', header - discovered, header - discovered, 0),
                    tuple(coverage[key] for key in ('status', 'remaining_count', 'unobserved_count', 'pending_count')))

    async def test_source_local_stalls_drain_children_then_retry_only_unread_candidates(self):
        for reason in ('instagram_followers_list_incomplete', 'instagram_followers_list_not_rendered',
                       'instagram_page_recovery_exhausted'):
            with self.subTest(reason=reason):
                task, target, control = self._task_and_control(1)
                state = fixture._PipelineState(prefix=reason[-8:])
                late = f'late_{reason[-8:]}'
                failed = asyncio.Event()
                error = WorkerExecutionError('source-only list failure', reason=reason, pause_required=True)
                if reason == 'instagram_page_recovery_exhausted':
                    error.details['original_reason'] = 'instagram_followers_list_incomplete'
                class Source(fixture._RelationParent):
                    supports_single_candidate_handoff = True
                    collection_pipeline = PlaywrightWorker.collection_pipeline
                    async def collect_followers(self, _target, *, candidate_sink, progress_sink, **kwargs):
                        await progress_sink({'source_total': 5, 'resume_tail': state.candidates[-2:]})
                        await candidate_sink(state.candidates)
                        await state.screen_started.wait()
                        failed.set()
                        raise error
                child = fixture._ScreeningChild(state)
                source = Source(state, child=child)
                manager = fixture._PipelineManager(self.service, fixture._NoopBitBrowser())
                running = asyncio.create_task(manager._execute_candidate_spooled_mode(
                    control, source, target, 'followers', task['settings'], None))
                try:
                    await asyncio.wait_for(failed.wait(), 5)
                    await asyncio.sleep(.02)
                    self.assertFalse(running.done(), 'a source-only failure must not cancel a healthy drain')
                    self.assertEqual(0, child.disconnect_calls)
                    control.pause_event.clear()
                    state.allow_screen.set()
                    await asyncio.sleep(.03)
                    self.assertFalse(running.done(), 'paused siblings must retain ownership')
                    self.assertEqual(0, child.disconnect_calls)
                    control.pause_event.set()
                    with self.assertRaises(WorkerExecutionError) as caught:
                        await asyncio.wait_for(running, 8)
                    self.assertIs(error, caught.exception)
                    self.assertEqual(Counter(state.candidates), Counter(name for _, name in state.screened))
                    checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
                    self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
                    self.assertEqual(0, checkpoint['counters']['pending_candidates'])
                    self.assertEqual(3, checkpoint['counters']['processed'])
                    self.assertEqual(1, child.disconnect_calls)
                    class Recovered(Source):
                        async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                            await candidate_sink(state.candidates + [late])
                            return CollectionOutcome('followers', [], source_total=5)
                    outcome = await asyncio.wait_for(manager._execute_candidate_spooled_mode(
                        control, Recovered(state, child=fixture._ScreeningChild(state)), target,
                        'followers', task['settings'], checkpoint), 8)
                    self.assertTrue(outcome['discovery_complete'])
                    self.assertEqual((4, 0), (outcome['recorded'], outcome['pending']))
                    self.assertEqual(Counter(state.candidates + [late]),
                        Counter(name for _, name in state.screened))
                finally:
                    control.pause_event.set()
                    state.allow_screen.set()
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    async def test_account_intervention_still_interrupts_instead_of_draining(self):
        for reason, details in (
            ('instagram_login_required', {}),
            ('instagram_challenge', {}),
            ('instagram_rate_limited', {}),
            ('instagram_action_blocked', {}),
            ('instagram_followers_list_incomplete', {'recovery_scope': 'screening_child'}),
            ('instagram_page_recovery_exhausted', {'original_reason': 'instagram_challenge'}),
        ):
            with self.subTest(reason=reason, details=details):
                await self.assert_source_guard_interrupts(reason, details)

    async def test_stop_during_source_error_drain_keeps_pending_rows_and_incomplete_cursor(self):
        task, target, control = self._task_and_control(1)
        state = fixture._PipelineState(prefix='stopped_drain')
        source_failed = asyncio.Event()
        class Source(fixture._RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                await candidate_sink(state.candidates)
                await state.screen_started.wait()
                source_failed.set()
                raise WorkerExecutionError('source list stalled', reason='instagram_followers_list_incomplete')
        manager = fixture._PipelineManager(self.service, fixture._NoopBitBrowser())
        running = asyncio.create_task(manager._execute_candidate_spooled_mode(control,
            Source(state, child=fixture._ScreeningChild(state, block=True)),
            target, 'followers', task['settings'], None))
        try:
            await asyncio.wait_for(source_failed.wait(), 5)
            control.stop_event.set()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
            self.assertEqual((3, 0), (checkpoint['counters']['pending_candidates'], checkpoint['counters']['processed']))
            self.assertTrue(state.child_cancelled.is_set())
            control.stop_event.clear()
            state.allow_screen.set()
            class Recovered(Source):
                async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                    await candidate_sink(state.candidates)
                    return CollectionOutcome('followers', [], source_total=3)
            result = await asyncio.wait_for(manager._execute_candidate_spooled_mode(control,
                Recovered(state, child=fixture._ScreeningChild(state)),
                target, 'followers', task['settings'], checkpoint), 8)
            self.assertEqual((3, 0), (result['recorded'], result['pending']))
            self.assertEqual(Counter(state.candidates), Counter(name for _, name in state.screened))
        finally:
            state.allow_screen.set()
            control.pause_event.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def assert_source_guard_interrupts(self, reason, details):
        task, target, control = self._task_and_control(1)
        state = fixture._PipelineState(prefix=target['username'])
        class Source(fixture._RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                await candidate_sink(state.candidates)
                await state.screen_started.wait()
                error = WorkerExecutionError('account requires verification', reason=reason, pause_required=True)
                error.details.update(details)
                raise error
        child = fixture._ScreeningChild(state, block=True)
        manager = fixture._PipelineManager(self.service, fixture._NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError):
            await asyncio.wait_for(manager._execute_candidate_spooled_mode(
                control, Source(state, child=child), target, 'followers', task['settings'], None), 8)
        self.assertTrue(state.child_cancelled.is_set())
        self.assertEqual([], state.screened)
        stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')
        self.assertEqual((3, 0), (stats['pending'], stats['recorded']))

    async def test_list_failure_pause_drain_and_retry_hold_the_shared_window_until_cleanup(self):
        source_failed, read_started, release_read, close_started, release_close = [asyncio.Event() for _ in range(5)]
        names, reads, children, source_calls = ['lease_first', 'lease_second'], [], [], []
        source_owners = []
        case = self
        class Child(FakeCollectionWorker):
            async def read_visible_profile(self, username, **kwargs):
                reads.append(username)
                read_started.set()
                await release_read.wait()
                return dict(username=username, visibility='private', followers=10, following=10, posts=3)
            async def disconnect(self):
                close_started.set()
                await release_close.wait()
        class Source(FakeCollectionWorker):
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def create_parallel_screening_worker(self):
                child = Child(None)
                children.append(child)
                return child
            async def collect_followers(self, target, *, candidate_sink, progress_sink, **kwargs):
                source_calls.append(target)
                with case.service.database.read() as connection:
                    lease = connection.execute(
                        'SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',
                        ('window-shared',),
                    ).fetchone()
                case.assertIsNotNone(lease)
                source_owners.append((id(self), lease['lease_token']))
                if len(source_calls) == 3:
                    checkpoint = case.service.get_checkpoint(case.user['id'], task['id'],
                        task['targets'][0]['id'], 'followers')
                    cursor = ExecutionManager._checkpoint_resume_cursor(checkpoint)
                    case.assertTrue(cursor['automatic_gap_recheck_started'])
                    case.assertFalse(cursor['candidate_spool_complete'])
                    case.assertFalse(kwargs.get('initial_resume_tail'))
                await progress_sink({'source_total': 3})
                await candidate_sink(names)
                if len(source_calls) == 1:
                    await read_started.wait()
                    source_failed.set()
                    raise WorkerExecutionError('virtual list lost its retained neighbour',
                        reason='instagram_followers_list_incomplete', pause_required=True)
                return CollectionOutcome('followers', [], source_total=3)
            def request_page_replacement(self, *_args):
                pass
            async def connection_healthy(self):
                return True
        task = self.service.create_task(self.user['id'], name='shared-window drain', modes=['followers'],
            targets=['lease_source'], window_ids=['window-shared'], settings={
                'local_person_recognition': False, 'location_enabled': False, 'parallel_screening_workers': 1})
        other = self.service.create_task(self.user['id'], name='Another IG task waiting for same window', modes=['followers'],
            targets=['another.source'], window_ids=['window-shared'], settings={'platform': 'instagram'})
        browser = fixture._NoopBitBrowser()
        manager = ExecutionManager(self.service, browser, worker_factory=Source,
            network_retry_delays=(.001,), network_retry_stagger_seconds=0,
            recovery_cooldown_seconds=.001, lease_heartbeat_interval_seconds=.03)
        def assert_owned():
            self.assertEqual([], browser.closed)
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.user['id'], 'window-shared',
                    operation_type='collection', entity_id=other['id'])
        try:
            await manager.start(self.user['id'], task['id'])
            await asyncio.wait_for(source_failed.wait(), 8)
            assert_owned()
            await manager.pause(self.user['id'], task['id'])
            assert_owned()
            release_read.set()
            await asyncio.sleep(.03)
            self.assertFalse(close_started.is_set(), 'paused child must not be retired')
            await manager.resume(self.user['id'], task['id'])
            await asyncio.wait_for(close_started.wait(), 8)
            assert_owned()
            release_close.set()
            await asyncio.wait_for(manager.wait(task['id']), 12)
            self.assertEqual(Counter(names), Counter(reads))
            # Interrupted initial pass resumes once, then its confirmed natural
            # end permits exactly one fresh pass for the remaining header gap.
            self.assertEqual(3, len(source_calls))
            self.assertEqual(1, len(set(source_owners)), 'all passes keep the worker and lease')
            self.assertEqual(['window-shared'], browser.closed)
            self.assertEqual([], self.service.list_browser_lease_states(self.user['id']))
            token = self.service.acquire_browser_lease(self.user['id'], 'window-shared',
                operation_type='collection', entity_id=other['id'])
            self.service.release_browser_lease('window-shared', token)
        finally:
            release_read.set()
            release_close.set()
            await manager.shutdown()


if __name__ == '__main__':
    unittest.main()
