"""Final source admission preserves the real child-factory recovery boundary."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.errors import BitBrowserAuthRequiredError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
import test_collection_drain_r56 as drain
import test_parallel_relation_pipeline as pipeline


class FactoryCauseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    async def execute_failure(self, reason, *, after_child=False):
        task, target, control = self._task_and_control(2)
        errors = [value if isinstance(value, Exception) else
                  WorkerExecutionError('original child factory diagnostic', reason=value)
                  for value in (reason if isinstance(reason, tuple) else (reason,))]
        error = errors[0]
        child = SimpleNamespace(disconnect=AsyncMock())
        class Worker:
            supports_single_candidate_handoff = True
            supports_parallel_screening_tab = True
            def __init__(self): self.calls = 0
            async def create_parallel_screening_worker(self):
                self.calls += 1
                if after_child and self.calls == 1: return child
                raise errors[min(self.calls - 1, len(errors) - 1)]
        worker = Worker()
        manager = ExecutionManager(self.service, object())
        with self.assertRaises(type(error)) as caught:
            await manager._execute_candidate_spooled_mode(
                control, worker, target, 'followers', task['settings'], None)
        if after_child:
            child.disconnect.assert_awaited_once()
        self.assertEqual(0, self.service.task_mode_candidate_stats(
            self.user['id'], task['id'], target['id'], 'followers')['total'])
        return error, caught.exception

    async def test_transport_and_account_guards_are_never_replaced_by_page_instability(self):
        for reason in ('worker_not_connected', 'browser_context_missing', 'cdp_profile_verification_failed',
                       'instagram_login_required', 'instagram_challenge', 'instagram_rate_limited',
                       'instagram_action_blocked'):
            for after_child in (False, True):
                with self.subTest(reason=reason, after_child=after_child):
                    original, observed = await self.execute_failure(reason, after_child=after_child)
                    self.assertIs(original, observed)

    async def test_actual_local_api_auth_failure_cleans_partial_pool_and_remains_manual(self):
        failure = BitBrowserAuthRequiredError('fixture desktop signed out')
        original, observed = await self.execute_failure(failure, after_child=True)
        self.assertIs(original, observed)
        self.assertTrue(ExecutionManager._is_auth_required_error(observed))

    async def test_retryable_capacity_and_creation_timeout_keep_their_actual_cause(self):
        for reason in ('screening_slots_busy', 'parallel_screening_page_create_timeout'):
            with self.subTest(reason=reason):
                original, observed = await self.execute_failure(reason)
                self.assertEqual('instagram_page_recovery_exhausted', observed.code)
                self.assertEqual(reason, observed.details['original_reason'])
                self.assertEqual('screening_child', observed.details['recovery_scope'])
                self.assertTrue(ExecutionManager._is_instagram_surface_retry_error(observed))

    async def test_unknown_factory_failure_is_retained_without_automatic_retry(self):
        _, observed = await self.execute_failure('unclassified_factory_failure')
        self.assertFalse(ExecutionManager._is_instagram_surface_retry_error(observed))
        self.assertTrue(ExecutionManager._is_profile_intervention_error(observed))

    async def test_mixed_factory_failures_never_hide_unknown_cause_behind_transient(self):
        for reasons in (('parallel_screening_page_create_timeout', 'parallel_screening_page_create_failed'),
                        ('parallel_screening_page_create_failed', 'screening_slots_busy')):
            with self.subTest(reasons=reasons):
                _, observed = await self.execute_failure(reasons)
                self.assertEqual('parallel_screening_page_create_failed', observed.details['original_reason'])
                self.assertFalse(ExecutionManager._is_instagram_surface_retry_error(observed))


class FinalFactoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain.CollectionDrainR56Tests.asyncTearDown
    task = drain.CollectionDrainR56Tests.task
    leases = drain.CollectionDrainR56Tests.leases

    async def test_final_seed_reconnects_once_after_prior_seed_and_closes_truthfully(self):
        for reason in ('worker_not_connected', 'browser_context_missing'):
            with self.subTest(reason=reason):
                task = self.task(sources=(reason + '_first', reason + '_last'))
                workers = []
                class Child:
                    async def disconnect(self): pass
                class Worker:
                    supports_candidate_batch_sink = True
                    supports_parallel_screening_tab = True
                    supports_single_candidate_handoff = True
                    def __init__(self, provider):
                        self.connects = self.disconnects = self.failures = self.probes = 0
                        self.reads = []
                        workers.append(self)
                    async def connect(self, profile_id, **kwargs):
                        self.connects += 1
                        self.profile_id = profile_id
                    async def disconnect(self): self.disconnects += 1
                    async def connection_healthy(self):
                        self.probes += 1
                        return True
                    async def create_parallel_screening_worker(self):
                        if self.reads and self.connects == 1:
                            self.failures += 1
                            raise WorkerExecutionError('context requires reconnect', reason=reason)
                        return Child()
                    async def collect_followers(self, source, **kwargs):
                        self.reads.append(source)
                        return CollectionOutcome('followers', [], source_total=0)
                provider = drain.CloseProvider()
                manager = ExecutionManager(self.service, provider, worker_factory=Worker,
                    network_retry_delays=(.001,), network_retry_stagger_seconds=0,
                    lease_heartbeat_interval_seconds=.03)
                self.managers.append(manager)
                await manager.start(self.owner, task['id'])
                await asyncio.wait_for(manager.wait(task['id']), 5)
                worker = workers[0]
                self.assertEqual(2, worker.connects)
                self.assertEqual(1, worker.failures)
                self.assertEqual(0, worker.probes)
                self.assertEqual([reason + '_first', reason + '_last'], worker.reads)
                self.assertEqual(['window-a'], provider.closed)
                final = self.service.get_task(self.owner, task['id'])
                self.assertEqual('completed', final['status'])
                self.assertTrue(all(row['status'] == 'completed' and row['collection_list_dismissed']
                    for row in final['targets']))
                self.assertEqual({}, self.leases())
                self.assertEqual([], (await manager.runtime_diagnostics(self.owner, task['id']))['network_waiters'])


class RetirementCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_waits_for_owned_native_retirement_and_keeps_slot(self):
        from test_parallel_screening_worker import _Page, _connected_parent
        entered, close_release = asyncio.Event(), asyncio.Event()
        late = asyncio.get_running_loop().create_future()
        class Page(_Page):
            closed = False
            def is_closed(self): return self.closed
            async def close(self):
                self.close_calls += 1
                entered.set()
                await close_release.wait()
                self.closed = True
                if not late.done(): late.set_result(None)
        old = Page('retiring')
        source = _Page('operator')
        pages = iter((old, _Page('replacement')))
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(side_effect=lambda: next(pages))), source)
        child = await parent.create_parallel_screening_worker()
        parent.release_parallel_screening_worker(child)
        child._track_late_lifecycle_task(late)
        retiring = asyncio.create_task(parent.create_parallel_screening_worker())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            retiring.cancel()
            await asyncio.sleep(0)
            retiring.cancel()
            await asyncio.sleep(0)
            self.assertFalse(retiring.done())
            self.assertIs(parent._screening_worker_pool[1], child)
            self.assertIn(1, parent._screening_slots_in_use)
            self.assertFalse(old.closed)
            self.assertEqual(0, source.close_calls)
            close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(retiring, 1)
            await asyncio.wait_for(child.wait_for_cleanup(), 1)
            self.assertNotIn(1, parent._screening_worker_pool)
            self.assertEqual(1, old.close_calls)
            self.assertEqual(0, source.close_calls)
        finally:
            close_release.set()
            if not late.done(): late.set_result(None)
            await asyncio.gather(retiring, return_exceptions=True)
            await parent.disconnect()



class FailurePublicationRetirementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    async def test_source_abort_cannot_return_failed_children_before_error_checkpoint(self):
        import test_single_handoff_r94 as handoff
        task, target, control, state, source, job = handoff.SingleHandoffR94Tests.pipeline(
            self, 2, fail=True, pooled=True)
        manager, _ = state.completion_context
        original_call = manager._await_durable_thread_call
        blocked_failures = []
        checkpoint_gate = asyncio.Event()
        async def blocked_call(function, *args, **kwargs):
            if function.__name__ == 'reconcile_task_mode_candidates' and kwargs.get('usernames'):
                blocked_failures.append(kwargs['usernames'][0])
                # Before the real write begins, source exhaustion may cancel this
                # consumer. Its synchronous failure must already forbid reuse.
                await checkpoint_gate.wait()
            return await original_call(function, *args, **kwargs)
        manager._await_durable_thread_call = blocked_call
        try:
            async with asyncio.timeout(5):
                while self.service.task_mode_candidate_stats(
                        self.user['id'], task['id'], target['id'], 'followers')['total'] < 3:
                    await asyncio.sleep(.005)
                for name in state.candidates[:2]: state.gates[name].set()
                while len(blocked_failures) < 2: await asyncio.sleep(.005)
                with self.assertRaises(WorkerExecutionError) as caught: await job
            self.assertEqual('instagram_profile_not_ready', caught.exception.details['original_reason'])
            self.assertEqual('screening_child', caught.exception.details['recovery_scope'])
            self.assertEqual([1, 1], [child.disconnect_calls for child in state.children])
            self.assertEqual([], state.pool_releases)
            self.assertEqual(3, self.service.task_mode_candidate_stats(
                self.user['id'], task['id'], target['id'], 'followers')['pending'])
        finally:
            checkpoint_gate.set()
            await handoff.SingleHandoffR94Tests.cleanup(self, state, job)


if __name__ == '__main__': unittest.main()
