"""A manual view never overlaps an in-flight collection page operation."""
from __future__ import annotations

import asyncio
import unittest

from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
import test_parallel_relation_pipeline as fixture


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(.005)


class ManualCollectionR51Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = fixture.ParallelRelationPipelineTests.asyncTearDown

    def assert_completed_and_released(self, task, manager, control):
        self.assertTrue(control.coordinator.done())
        self.assertEqual('completed', self.service.get_task(self.user['id'], task['id'])['status'])
        self.assertEqual(['manual_window'], manager.bitbrowser.closed)
        self.assertEqual({}, control.leases)
        self.assertEqual([], self.service.list_browser_lease_states(self.user['id']))

    async def make_run(self, *, empty=False, source_finishes=False, poison=False, children=1,
                       source_total=710):
        source_entered, source_release = asyncio.Event(), asyncio.Event()
        profile_entered, profile_release = asyncio.Event(), asyncio.Event()
        source_safe, profile_safe = asyncio.Event(), asyncio.Event()
        names = [] if empty else ['manual_candidate_a', 'manual_candidate_b']
        observed = {'source_calls': 0, 'source_owners': [], 'reads': [], 'children': [], 'events': []}

        class Child:
            _page_stage_abandoned = poison

            async def read_visible_profile(self, username, **kwargs):
                observed['reads'].append(username)
                observed['events'].append('read')
                profile_entered.set()
                await profile_release.wait()
                profile_safe.set()
                return {'username': username, 'visibility': 'private',
                        'followers': 12, 'following': 9, 'posts': 2}

            async def disconnect(self):
                observed['events'].append('child_close')

        class Parent(Child):
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True

            async def connect(self, *_args, **_kwargs):
                pass

            async def create_parallel_screening_worker(self):
                child = Child()
                observed['children'].append(child)
                return child

            async def collect_followers(self, target, *, candidate_sink, progress_sink, **kwargs):
                observed['source_calls'] += 1
                observed['source_owners'].append((id(self), manager._runs[task['id']].leases['manual_window']))
                await progress_sink({'source_total': source_total, 'discovered_count': 0})
                if names:
                    await candidate_sink(names)
                source_entered.set()
                if observed['source_calls'] == 1:
                    await source_release.wait()
                source_safe.set()
                if not source_finishes:
                    await self.manual_control_checkpoint()
                return CollectionOutcome('followers', [], source_total=source_total)

        task = self.service.create_task(self.user['id'], name='safe manual handoff',
            modes=['followers'], targets=['manual_source'], window_ids=['manual_window'],
            settings={'parallel_screening_workers': children, 'local_person_recognition': False,
                      'location_enabled': False})
        manager = ExecutionManager(self.service, fixture._NoopBitBrowser(),
                                   worker_factory=lambda _: Parent())
        manager.manual_control_revoke = lambda owner, profile: observed['events'].append('revoke')
        await manager.start(self.user['id'], task['id'])
        control = manager._runs[task['id']]
        await asyncio.wait_for(source_entered.wait(), 10)
        if names:
            await asyncio.wait_for(profile_entered.wait(), 10)
        return task, manager, control, observed, source_release, profile_release, source_safe, profile_safe

    async def test_inflight_source_and_child_must_both_return_before_grant_and_resume_restarts_cursor(self):
        task, manager, control, seen, release_source, release_profile, safe_source, safe_profile = await self.make_run()
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            self.assertFalse(begin.done())
            self.assertFalse(safe_source.is_set())
            self.assertFalse(safe_profile.is_set())
            release_source.set()
            await safe_source.wait()
            await asyncio.sleep(.02)
            self.assertFalse(begin.done(), 'one remaining profile operation still owns the page')
            self.assertNotIn('child_close', seen['events'])
            release_profile.set()
            await asyncio.wait_for(begin, 10)
            self.assertTrue(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
            self.assertFalse(control.coordinator.done())
            self.assertNotIn('child_close', seen['events'])
            before = len(seen['reads'])
            await asyncio.sleep(.02)
            self.assertEqual(before, len(seen['reads']))
            await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            # Manual interruption resumes the initial pass, then its gap gets one extra.
            self.assertEqual(3, seen['source_calls'])
            self.assertEqual(1, len(set(seen['source_owners'])))
            self.assertLess(seen['events'].index('revoke'), seen['events'].index('child_close'))
            stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertEqual((2, 0), (stats['recorded'], stats['pending']))
            self.assert_completed_and_released(task, manager, control)
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_timeout_does_not_cancel_inflight_page_and_later_retry_can_grant(self):
        task, manager, control, seen, release_source, release_profile, safe_source, safe_profile = await self.make_run()
        manager.manual_control_wait_seconds = .03
        token = control.leases['manual_window']
        try:
            with self.assertRaises(ConflictError):
                await manager.begin_manual_control(self.user['id'], 'manual_window', token)
            self.assertFalse(safe_source.is_set())
            self.assertFalse(safe_profile.is_set())
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertNotIn('child_close', seen['events'])
            release_source.set(); release_profile.set()
            await until(lambda: control.manual_requests['manual_window']['settled'].is_set())
            await manager.begin_manual_control(self.user['id'], 'manual_window', token)
            self.assertTrue(manager.manual_control_active(self.user['id'], 'manual_window', token))
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()

    async def test_empty_queue_consumer_wakes_for_manual_without_faking_source_completion(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(empty=True)
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set()
            await asyncio.wait_for(begin, 10)
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
            self.assertEqual([], seen['reads'])
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_completed_source_pending_profiles_resume_without_rediscovery_and_global_continue_revokes(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(source_finishes=True, source_total=2)
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertTrue(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
            await manager.resume(self.user['id'], task['id'])
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assertEqual(1, seen['source_calls'])
            self.assertIn('revoke', seen['events'])
            self.assert_completed_and_released(task, manager, control)
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_manual_after_first_bottom_preserves_pending_extra_without_regranting_budget(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(source_finishes=True)
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            cursor = manager._checkpoint_resume_cursor(checkpoint)
            self.assertTrue(cursor['automatic_gap_recheck_started'])
            self.assertFalse(manager._candidate_spool_complete(checkpoint, require_natural_end=True))
            self.assertEqual(1, seen['source_calls'])
            self.assertEqual(token, control.leases['manual_window'])
            self.assertTrue(manager.manual_control_active(self.user['id'], 'manual_window', token))
            await asyncio.sleep(.02)
            self.assertEqual(1, seen['source_calls'], 'manual grant cannot start the extra pass')
            await manager.resume(self.user['id'], task['id'])
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assertEqual(2, seen['source_calls'])
            self.assertEqual(1, len(set(seen['source_owners'])))
            final = self.service.get_checkpoint(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertTrue(manager._candidate_spool_complete(final, require_natural_end=True))
            self.assertTrue(final['cursor']['automatic_gap_recheck_started'])
            self.assertEqual(2, final['counters']['processed'])
            self.assertEqual(710, final['counters']['source_total'])
            self.assertLess(seen['events'].index('revoke'), seen['events'].index('child_close'))
            self.assert_completed_and_released(task, manager, control)
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_unsettled_browser_work_stays_readonly_even_after_python_coroutines_join(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(poison=True)
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            with self.assertRaises(ConflictError):
                await asyncio.wait_for(begin, 10)
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_two_children_both_exit_and_no_candidate_is_terminalized_at_manual_boundary(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(children=2)
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            self.assertEqual(2, len(seen['children']))
            self.assertEqual(2, len(control.manual_requests['manual_window']['children']))
            stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertEqual((2, 0, 0), (stats['pending'], stats['recorded'], stats['deduped']))
            await manager.stop_window(self.user['id'], task['id'], 'manual_window')
            self.assertLess(seen['events'].index('revoke'), seen['events'].index('child_close'))
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_already_paused_producer_and_idle_child_can_reach_manual_barrier(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(empty=True)
        token = control.leases['manual_window']
        try:
            await manager.pause(self.user['id'], task['id'])
            begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set()
            await asyncio.wait_for(begin, 10)
            self.assertFalse(control.pause_event.is_set(), 'manual control must not resume sibling task gates')
            self.assertTrue(manager.manual_control_active(self.user['id'], 'manual_window', token))
            await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            await asyncio.sleep(.02)
            self.assertFalse(control.coordinator.done())
            self.assertFalse(control.pause_event.is_set())
            await manager.resume(self.user['id'], task['id'])
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assert_completed_and_released(task, manager, control)
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()

    async def test_revoke_failure_cannot_allow_any_resumed_page_operation(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run()
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            def reject(*args):
                raise ConflictError('native surface did not acknowledge revoke')
            manager.manual_control_revoke = reject
            with self.assertRaises(ConflictError):
                await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            self.assertTrue(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertEqual(1, seen['source_calls'])
            self.assertNotIn('child_close', seen['events'])
        finally:
            manager.manual_control_revoke = lambda *args: seen['events'].append('revoke')
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_idle_and_both_recovery_waits_wake_without_waiting_for_cooldown(self):
        for index, reason in enumerate((None, 'instagram_followers_list_incomplete', 'instagram_page_recovery_exhausted')):
            with self.subTest(reason=reason):
                class Parent:
                    calls = 0

                    async def connect(self, *args, **kwargs):
                        pass

                    async def disconnect(self):
                        pass

                    async def collect_followers(self, *args, **kwargs):
                        self.calls += 1
                        if reason and self.calls == 1:
                            error = WorkerExecutionError('fixture source wait', reason=reason)
                            if reason == 'instagram_page_recovery_exhausted':
                                error.details['original_reason'] = 'instagram_followers_list_not_rendered'
                            raise error
                        return CollectionOutcome('followers', [], source_total=0)

                parent = Parent()
                task = self.service.create_task(self.user['id'], name='manual during wait',
                    modes=['followers'], targets=[f'waiting_source_{index}'], window_ids=['waiting_window'],
                    settings={'local_person_recognition': False, 'location_enabled': False,
                              'live_queue_enabled': True})
                manager = ExecutionManager(self.service, fixture._NoopBitBrowser(),
                    worker_factory=lambda _: parent, network_retry_delays=(300,))
                try:
                    if reason is None:
                        # Idle ownership now requires unfinished task work. Keep
                        # this source pending behind the operator's dispatch lock.
                        self.service.set_split_claim_locked(self.user['id'], True)
                    await manager.start(self.user['id'], task['id'])
                    control = manager._runs[task['id']]
                    await until(lambda: control.profile_states.get('waiting_window', {}).get('state') in
                                ({'idle'} if reason is None else {'degraded', 'waiting_network'}))
                    token = control.leases['waiting_window']
                    await asyncio.wait_for(manager.begin_manual_control(self.user['id'], 'waiting_window', token), 2)
                    self.assertTrue(manager.manual_control_active(self.user['id'], 'waiting_window', token))
                    self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
                finally:
                    await manager.shutdown()
                    self.service.set_split_claim_locked(self.user['id'], False)

    async def test_stale_or_foreign_lease_never_requests_manual_control(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run()
        try:
            with self.assertRaises(ConflictError):
                await manager.begin_manual_control(self.user['id'], 'manual_window', 'stale-token')
            with self.assertRaises(ConflictError):
                await manager.begin_manual_control('another-owner', 'manual_window', control.leases['manual_window'])
            self.assertEqual({}, control.manual_requests)
            self.assertTrue(control.profile_pause_events['manual_window'].is_set())
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()

    async def test_continue_while_manual_request_is_still_preparing_cannot_leave_a_hidden_pause(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run()
        manager.manual_control_wait_seconds = .02
        token = control.leases['manual_window']
        try:
            with self.assertRaises(ConflictError):
                await manager.begin_manual_control(self.user['id'], 'manual_window', token)
            await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            self.assertTrue(control.profile_pause_events['manual_window'].is_set())
            release_source.set(); release_profile.set()
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assertEqual({}, control.manual_requests)
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            # Manual interruption resumes the initial pass, then its gap gets one extra.
            self.assertEqual(3, seen['source_calls'])
            self.assertEqual(1, len(set(seen['source_owners'])))
            self.assertLess(seen['events'].index('revoke'), seen['events'].index('child_close'))
            self.assert_completed_and_released(task, manager, control)
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()

    async def test_resumed_task_keeps_lease_after_negative_close_and_retry_does_not_revisit(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run()
        token = control.leases['manual_window']
        provider = manager.bitbrowser
        acknowledge_close = provider.close_profile

        def reject_close(profile_id):
            provider.closed.append(profile_id)
            return {'closed': False}

        provider.close_profile = reject_close
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            await until(lambda: control.profile_states.get('manual_window', {}).get('reason') == 'browser_close_failed')
            self.assertFalse(control.coordinator.done())
            self.assertEqual(token, control.leases['manual_window'])
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.user['id'], 'manual_window',
                    operation_type='account', entity_id='unrelated-operation')
            stats = self.service.task_mode_candidate_stats(self.user['id'], task['id'], task['targets'][0]['id'], 'followers')
            self.assertEqual((2, 0), (stats['recorded'], stats['pending']))
            source_calls, reads = seen['source_calls'], list(seen['reads'])
            self.assertEqual(3, source_calls)  # interrupted, resumed, one extra
            self.assertEqual(1, len(set(seen['source_owners'])))
            provider.close_profile = acknowledge_close
            retry = await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            self.assertEqual('closed', retry['status'])
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assertEqual(['manual_window', 'manual_window'], provider.closed)
            self.assertEqual((source_calls, reads), (seen['source_calls'], seen['reads']))
            self.assertEqual('completed', self.service.get_task(self.user['id'], task['id'])['status'])
            self.assertEqual({}, control.leases)
            self.assertEqual([], self.service.list_browser_lease_states(self.user['id']))
        finally:
            provider.close_profile = acknowledge_close
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_first_child_close_failure_still_joins_second_and_clears_manual_state(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run(children=2)
        token = control.leases['manual_window']
        closed = []
        second_started, second_release = asyncio.Event(), asyncio.Event()
        async def first_close():
            closed.append('first')
            raise RuntimeError('first child close failed')
        async def second_close():
            second_started.set()
            await second_release.wait()
            closed.append('second')
        seen['children'][0].disconnect = first_close
        seen['children'][1].disconnect = second_close
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            await asyncio.wait_for(begin, 10)
            await manager.resume_window(self.user['id'], task['id'], 'manual_window')
            await asyncio.wait_for(second_started.wait(), 10)
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])),
                             'failed first close must not release ownership while the second is still closing')
            self.assertFalse(control.coordinator.done())
            second_release.set()
            await asyncio.wait_for(asyncio.shield(control.coordinator), 10)
            self.assertEqual(['first', 'second'], closed)
            self.assertEqual({}, control.manual_requests)
            self.assertFalse(control.manual_events['manual_window'].is_set())
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertNotEqual('completed', self.service.get_task(self.user['id'], task['id'])['status'])
        finally:
            second_release.set(); release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_manual_request_during_health_probe_releases_shared_recovery_slot_before_grant(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Worker:
            supports_candidate_batch_sink = True
            async def connect(self, *args, **kwargs):
                pass
            async def disconnect(self):
                pass
            async def collect_followers(self, *args, **kwargs):
                raise WorkerExecutionError('fixture WAN timeout', reason='instagram_network_unavailable')
            async def connection_healthy(self):
                entered.set()
                await release.wait()
                return True
        task = self.service.create_task(self.user['id'], name='manual recovery slot',
            modes=['followers'], targets=['source'], window_ids=['probe_window'],
            settings={'local_person_recognition': False, 'location_enabled': False})
        manager = ExecutionManager(self.service, fixture._NoopBitBrowser(),
            worker_factory=lambda _: Worker(), network_retry_delays=(0,),
            network_retry_concurrency=1, network_retry_stagger_seconds=0)
        begin = None
        try:
            await manager.start(self.user['id'], task['id'])
            control = manager._runs[task['id']]
            await asyncio.wait_for(entered.wait(), 10)
            token = control.leases['probe_window']
            begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'probe_window', token))
            await until(lambda: 'probe_window' in control.manual_requests)
            self.assertFalse(begin.done())
            release.set()
            await asyncio.wait_for(begin, 10)
            self.assertTrue(manager.manual_control_active(self.user['id'], 'probe_window', token))
            await asyncio.wait_for(control.network_retry_gate.acquire(), 1)
            control.network_retry_gate.release()
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
        finally:
            release.set()
            await manager.shutdown()
            if begin:
                await asyncio.gather(begin, return_exceptions=True)

    async def test_done_lifecycle_task_not_yet_consumed_by_cleanup_callback_stays_readonly(self):
        task, manager, control, seen, release_source, release_profile, _, _ = await self.make_run()
        token = control.leases['manual_window']
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        seen['children'][0]._late_lifecycle_tasks = {completed}
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await until(lambda: 'manual_window' in control.manual_requests)
            release_source.set(); release_profile.set()
            with self.assertRaises(ConflictError):
                await asyncio.wait_for(begin, 10)
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
        finally:
            release_source.set(); release_profile.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)
