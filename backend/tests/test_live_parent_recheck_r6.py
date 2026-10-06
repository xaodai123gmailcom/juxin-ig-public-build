"""Manual source passes retain the original child pipeline and lease."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.errors import ConflictError
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from test_core import FakeCollectionWorker
from test_parallel_relation_pipeline import _PipelineManager
import test_collection_drain_r56 as drain


class LiveParentRecheckTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain.CollectionDrainR56Tests.asyncTearDown
    task = drain.CollectionDrainR56Tests.task
    manager = drain.CollectionDrainR56Tests.manager
    gate = drain.CollectionDrainR56Tests.gate
    until = drain.CollectionDrainR56Tests.until
    leases = drain.CollectionDrainR56Tests.leases

    async def running_gap(self, *, block_parent_stop=False, block_producer_return=False, block_initial_scan=False, isolated_child_failure=False, child_failure_reason="instagram_profile_not_ready", child_count=1, total=5, mode="followers"):
        case = self
        state = SimpleNamespace(calls=0, screened=[], disconnected=0, children=[], parents=[], producer=None,
            source_entered=self.gate(), source_release=self.gate(), child_release=self.gate(),
            producer_finalized=self.gate(), producer_release=self.gate(),
            initial_entered=self.gate(), initial_release=self.gate(), reels_calls=0,
            source_failed=self.gate(), child_cancelled=self.gate(), source_failure=False, child_failure=False, child_failure_raised=self.gate(),
            reels_entered=self.gate(), reels_joined=self.gate(), reels_stop=self.gate(),
            child_entered=self.gate(), new_child_entered=self.gate())
        if block_producer_return:
            from app import execution_manager as execution_module
            original_finish = execution_module.finish_owned
            async def gated_finish(awaitable):
                name = getattr(getattr(awaitable, 'cr_code', None), 'co_name', None)
                result = await original_finish(awaitable)
                if name == 'persist_source_return' and result is False and not state.producer_finalized.is_set():
                    state.producer_finalized.set()
                    await state.producer_release.wait()
                return result
            patcher = patch.object(execution_module, 'finish_owned', gated_finish)
            patcher.start()
            self.addCleanup(patcher.stop)
        if not block_parent_stop:
            state.reels_stop.set()

        class Child:
            def __init__(self, number):
                self.number = number
            async def screen(self, username):
                state.child_entered.set()
                if isolated_child_failure and self.number == 0:
                    state.isolated_failure_task = asyncio.current_task()
                    raise WorkerExecutionError('isolated child page failed', reason=child_failure_reason)
                try:
                    await state.child_release.wait()
                except asyncio.CancelledError:
                    state.child_cancelled.set()
                    raise
                if state.child_failure:
                    state.child_failure_raised.set()
                    raise RuntimeError('screening child unavailable')
                state.screened.append(username)
                if username == 'new_one':
                    state.new_child_entered.set()
            async def disconnect(self):
                state.disconnected += 1

        class Worker(FakeCollectionWorker):
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True
            collection_pipeline = 'r59-batch'
            async def connect(self, *args, **kwargs):
                await super().connect(*args, **kwargs)
                self.page = object()
                state.parents.append(self)
            async def create_parallel_screening_worker(self):
                child = Child(len(state.children))
                state.children.append(child)
                return child
            async def collect_followers(self, username, *, candidate_sink, progress_sink, initial_resume_tail=None, **kwargs):
                state.calls += 1
                if state.calls <= 2:
                    await candidate_sink(['old_one','old_two'])
                    await progress_sink({'source_total':total, 'resume_tail':['old_two']})
                    if state.calls == 1 and block_initial_scan:
                        state.initial_entered.set()
                        await state.initial_release.wait()
                else:
                    if mode == 'followers' and state.reels_entered.is_set():
                        case.assertTrue(state.reels_joined.is_set())
                    case.assertIsNone(initial_resume_tail)
                    state.source_entered.set()
                    await state.source_release.wait()
                    if state.source_failure:
                        state.source_failed.set()
                        raise WorkerExecutionError('incomplete manual source', reason='instagram_followers_list_incomplete')
                    await candidate_sink(['old_one','new_one'])
                return CollectionOutcome(mode, [], source_total=total)
            collect_following = collect_followers
            def parent_reels_adapter(self):
                return object()
            async def screen(self, username):
                raise AssertionError('parent must not screen while children exist')

        task = self.service.create_task(self.owner,name='live parent',modes=[mode],targets=['source'],
            window_ids=['window-a'],settings={'live_queue_enabled':True,'location_enabled':False,
                'local_person_recognition':False,'parallel_screening_workers':2 if isolated_child_failure else child_count})
        class FixtureManager(_PipelineManager):
            async def _collect_mode(self, *args, **kwargs):
                # Observe the real source coroutine, without replacing its work
                # or weakening the production request's producer.done() fence.
                state.producer = asyncio.current_task()
                return await super()._collect_mode(*args, **kwargs)

        manager, provider = self.manager(Worker, manager_type=FixtureManager)
        async def reels(*args):
            state.reels_calls += 1
            state.reels_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await state.reels_stop.wait()
                state.reels_joined.set()
        manager._browse_parent_reels = reels
        await manager.start(self.owner, task['id'])
        if block_initial_scan and isolated_child_failure and child_failure_reason == "cdp_profile_verification_failed":
            # An authoritative failure is published synchronously and may stop
            # the producer before its progress callback reaches the scan gate.
            await self.until(lambda:bool(getattr(state,'isolated_failure_task',None)))
        elif block_initial_scan:
            await asyncio.wait_for(state.initial_entered.wait(),3)
        elif block_producer_return:
            await asyncio.wait_for(state.producer_finalized.wait(),3)
        elif mode == 'followers':
            await asyncio.wait_for(state.reels_entered.wait(),3)
        else:
            await self.until(lambda:state.calls == 2)
            self.assertIsNotNone(state.producer)
            # SQLite's natural-end write becomes visible before its thread and
            # producer have returned. Wait for that exact producer to settle;
            # elapsed time (especially on Windows) cannot prove safe admission.
            await asyncio.wait_for(asyncio.shield(state.producer), 5)
            self.assertTrue(state.producer.done())
            self.assertTrue(self.service.get_checkpoint(
                self.owner,task['id'],task['targets'][0]['id'],mode)['cursor'].get('candidate_spool_natural_end'))
        target = task['targets'][0]['id']
        control = manager._runs[task['id']]
        token = control.leases['window-a']
        return task, target, control, token, manager, provider, state

    async def request(self, task, target, manager):
        return await manager.recheck_source(self.owner, task['id'], target, 'followers')

    def checkpoint(self, task, target):
        return self.service.get_checkpoint(self.owner, task['id'], target, 'followers')

    async def test_request_before_producer_return_is_durable_and_drains_without_another_click(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_producer_return=True)
        self.assertFalse(state.producer.done())
        before = self.checkpoint(task,target)
        results = await asyncio.gather(*(self.request(task,target,manager) for _ in range(4)))
        self.assertTrue(all(row['waiting_for_safe_point'] for row in results))
        self.assertEqual(1,len({row['target']['source_recheck_requested_at'] for row in results}))
        self.assertEqual('prepared',self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']['state'])
        self.assertEqual(before,self.checkpoint(task,target))
        state.child_release.set()
        await self.until(lambda:len(state.screened)==2)
        self.assertEqual(0,state.disconnected)
        self.assertEqual(2,state.calls)
        self.assertEqual(token,control.leases['window-a'])
        state.producer_release.set()
        await asyncio.wait_for(state.source_entered.wait(),3)
        state.source_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(3,state.calls)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)
        self.assertEqual({},self.leases())

    async def test_initial_scan_request_keeps_one_automatic_pass_then_one_manual_pass(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_initial_scan=True)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        first = await self.request(task,target,manager)
        self.assertTrue(first['waiting_for_safe_point'])
        state.initial_release.set()
        await asyncio.wait_for(state.source_entered.wait(),3)
        self.assertEqual(3,state.calls)  # initial + mandatory automatic + explicit
        self.assertTrue(self.checkpoint(task,target)['cursor']['automatic_gap_recheck_started'])
        self.assertFalse(state.reels_entered.is_set())
        state.source_release.set()
        await self.until(lambda:state.reels_calls == 1)
        self.assertEqual(3,state.calls)
        self.assertEqual(token,control.leases['window-a'])
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)

    async def test_manual_pass_returns_to_reels_without_waiting_for_children(self):
        task,target,control,token,manager,provider,state = await self.running_gap()
        self.assertEqual(1,state.reels_calls)
        await self.request(task,target,manager)
        await asyncio.wait_for(state.source_entered.wait(),3)
        state.source_release.set()
        await self.until(lambda:state.reels_calls == 2)
        self.assertEqual(3,state.calls)
        self.assertFalse(state.child_release.is_set())
        self.assertEqual(0,state.disconnected)
        self.assertEqual(token,control.leases['window-a'])
        await self.request(task,target,manager)
        await self.until(lambda:state.reels_calls == 3)
        self.assertEqual(4,state.calls)
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)

    async def test_no_gap_live_source_still_accepts_explicit_request(self):
        task,target,control,token,manager,provider,state = await self.running_gap(total=2)
        self.assertEqual(1,state.calls)
        result = await self.request(task,target,manager)
        self.assertTrue(result['parent_only'])
        await self.until(lambda:state.reels_calls == 2)
        self.assertEqual(2,state.calls) # zero automatic passes; exactly one explicit
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)

    async def test_unknown_total_live_source_still_accepts_explicit_request(self):
        task,target,control,token,manager,provider,state = await self.running_gap(total=None)
        self.assertEqual(1,state.calls)
        await self.request(task,target,manager)
        await self.until(lambda:state.reels_calls == 2)
        self.assertEqual(2,state.calls)
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)

    async def test_isolated_child_failure_does_not_block_reels_or_manual_with_healthy_sibling(self):
        task,target,control,token,manager,provider,state = await self.running_gap(
            block_initial_scan=True, isolated_child_failure=True)
        await self.until(lambda:bool(getattr(state,'isolated_failure_task',None)) and state.isolated_failure_task.done())
        state.initial_release.set()
        await asyncio.wait_for(state.reels_entered.wait(),3)
        self.assertEqual(2,state.calls)
        result = await self.request(task,target,manager)
        self.assertTrue(result['waiting_for_safe_point'])
        await asyncio.wait_for(state.source_entered.wait(),3)
        state.source_release.set()
        await self.until(lambda:state.reels_calls == 2)
        self.assertEqual(3,state.calls)
        self.assertEqual(token,control.leases['window-a'])
        self.assertEqual(1,state.disconnected) # failed page retired; healthy sibling still owned
        self.assertEqual('pending',self.service.list_pending_task_mode_candidates(
            self.owner,task['id'],target,'followers')[0]['state'])
        await manager.stop(self.owner,task['id'])

    async def test_child_identity_failure_never_starts_reels_or_manual_navigation(self):
        task,target,control,token,manager,provider,state = await self.running_gap(
            block_initial_scan=True, isolated_child_failure=True,
            child_failure_reason="cdp_profile_verification_failed")
        await self.until(lambda:'window-a' not in control.live_source_rechecks)
        state.initial_release.set()
        with self.assertRaises(ConflictError):
            await self.request(task,target,manager)
        self.assertEqual(0,state.reels_calls)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(1,state.calls)
        await manager.stop(self.owner,task['id'])

    async def test_early_pending_restart_preserves_owed_automatic_pass(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_initial_scan=True)
        result = await self.request(task,target,manager)
        await manager.stop(self.owner,task['id'])
        self.assertEqual(1,state.calls)
        self.database.initialize()
        state.initial_release.set(); state.source_release.set(); state.child_release.set()
        await manager.retry_target(self.owner,task['id'],target)
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(4,state.calls) # cancelled initial + resumed initial + automatic + manual
        final = self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']
        self.assertEqual('completed',final['state'])
        self.assertEqual(result['target']['source_recheck_requested_at'],final['requested_at'])
        self.assertEqual({},self.leases())

    async def test_pending_request_survives_stop_and_reopen_without_auto_resurrection(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True)
        result = await self.request(task,target,manager)
        stopped = asyncio.create_task(manager.stop(self.owner,task['id']))
        state.reels_stop.set()
        await asyncio.wait_for(stopped,5)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(2,state.calls)
        self.database.initialize()
        pending = self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']
        self.assertEqual('prepared',pending['state'])
        self.assertEqual(result['target']['source_recheck_requested_at'],pending['requested_at'])
        self.assertEqual({},self.leases())
        state.source_release.set(); state.child_release.set()
        await manager.retry_target(self.owner,task['id'],target)
        await asyncio.wait_for(manager.wait(task['id']),5)
        final = self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']
        self.assertEqual('completed',final['state'])
        self.assertEqual(pending['requested_at'],final['requested_at'])
        self.assertEqual(3,state.calls)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)

    async def test_running_recheck_joins_parent_and_keeps_children_lease_and_dedupe(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True)
        result = await self.request(task,target,manager)
        self.assertTrue(result['parent_only'])
        self.assertFalse(result['waiting_for_task_resume'])
        self.assertTrue(control.pause_event.is_set())
        before = self.checkpoint(task,target)
        self.assertTrue(before['cursor']['candidate_spool_complete'])
        self.assertNotIn('source_recheck_requested',before['cursor'])
        self.assertTrue(result['waiting_for_safe_point'])
        duplicate = await self.request(task,target,manager)
        self.assertEqual(result['target']['source_recheck_requested_at'],duplicate['target']['source_recheck_requested_at'])
        self.assertEqual(before, self.checkpoint(task,target))
        state.child_release.set()
        await self.until(lambda: len(state.screened)==2)
        self.assertEqual(2,state.calls)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(0,state.disconnected)
        self.assertEqual(token,control.leases['window-a'])
        self.assertEqual([],provider.closed)
        state.reels_stop.set()
        await asyncio.wait_for(state.source_entered.wait(),3)
        self.assertEqual(1,len(state.parents))
        self.assertEqual(1,len(state.children))
        self.assertEqual(0,state.disconnected)
        state.source_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)
        self.assertEqual(3,state.calls)
        self.assertEqual(1,state.disconnected)
        self.assertEqual(['window-a'],provider.closed)
        self.assertEqual({},self.leases())
        final=self.service.get_task(self.owner,task['id'])
        self.assertEqual('completed',final['targets'][0]['source_recheck']['state'])
        self.assertEqual(3,final['targets'][0]['mode_progress']['followers']['discovered'])
        self.assertTrue(self.checkpoint(task,target)['cursor']['automatic_gap_recheck_started'])

    async def test_pause_queued_recheck_preserves_global_pause_until_resume(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        await manager.pause(self.owner,task['id'])
        result=await self.request(task,target,manager)
        self.assertTrue(result['waiting_for_task_resume'])
        await asyncio.sleep(.03)
        self.assertFalse(state.source_entered.is_set())
        self.assertFalse(control.pause_event.is_set())
        state.child_release.set();state.source_release.set()
        await manager.resume(self.owner,task['id'])
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(3,state.calls)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)

    async def test_stop_during_parent_join_never_navigates_or_releases_early(self):
        task,target,control,token,manager,provider,state=await self.running_gap(block_parent_stop=True)
        await self.request(task,target,manager)
        stopped=asyncio.create_task(manager.stop(self.owner,task['id']))
        await asyncio.sleep(.03)
        self.assertEqual([],provider.closed)
        self.assertEqual(token,control.leases['window-a'])
        state.reels_stop.set()
        await asyncio.wait_for(stopped,5)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(2,state.calls)
        self.assertEqual({},self.leases())
        cursor=self.checkpoint(task,target)['cursor']
        self.assertNotIn('source_recheck_requested',cursor)
        self.assertTrue(cursor['candidate_spool_complete'])
        self.assertEqual('prepared',self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']['state'])

    async def test_stop_mid_recheck_restart_uses_existing_spool_once(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        await self.request(task,target,manager)
        await asyncio.wait_for(state.source_entered.wait(),3)
        state.child_release.set()
        await self.until(lambda:len(state.screened)==2)
        await manager.stop(self.owner,task['id'])
        checkpoint=self.checkpoint(task,target)
        self.assertFalse(checkpoint['cursor']['candidate_spool_complete'])
        self.assertTrue(checkpoint['cursor']['automatic_gap_recheck_started'])
        state.source_release.set()
        await manager.retry_target(self.owner,task['id'],target)
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(4,state.calls) # one cancelled explicit pass, one resumed pass; no auto extra
        self.assertEqual(['old_one','old_two','new_one'],state.screened)
        self.assertEqual({},self.leases())

    async def test_resumed_active_manual_coalesces_then_allows_next_pass_at_source_end(self):
        task,target,control,token,manager,provider,state = await self.running_gap()
        first = await self.request(task,target,manager)
        await asyncio.wait_for(state.source_entered.wait(),3)
        await manager.stop(self.owner,task['id'])
        await manager.retry_target(self.owner,task['id'],target)
        await self.until(lambda:state.calls == 4)
        duplicate = await self.request(task,target,manager)
        self.assertEqual(first['target']['source_recheck_requested_at'],duplicate['target']['source_recheck_requested_at'])
        self.assertFalse(duplicate['waiting_for_safe_point'])
        state.source_release.set()
        await self.until(lambda:state.reels_calls == 2)
        self.assertEqual('completed',self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']['state'])
        await self.request(task,target,manager)
        await self.until(lambda:state.calls == 5)
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)
        self.assertEqual({},self.leases())

    async def test_last_child_retirement_during_activation_prevents_navigation(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True)
        await self.request(task,target,manager)
        entered = self.gate()
        release = self.gate(threaded=True)
        loop = asyncio.get_running_loop()
        original = self.service.recheck_task_source
        original_stats = self.service.task_mode_candidate_stats
        failed = False
        def slow_activation(*args, **kwargs):
            if kwargs.get('pending_requested_at'):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError('fixture activation was not released')
            return original(*args, **kwargs)
        def fail_child_stats(*args, **kwargs):
            nonlocal failed
            if entered.is_set() and not failed:
                failed = True
                raise WorkerExecutionError('isolated child retired',reason='browser_window_surface_unstable')
            return original_stats(*args, **kwargs)
        with patch.object(self.service,'recheck_task_source',slow_activation), patch.object(self.service,'task_mode_candidate_stats',fail_child_stats):
            state.reels_stop.set()
            await asyncio.wait_for(entered.wait(),3)
            state.child_release.set()
            await self.until(lambda:state.disconnected == 1)
            self.assertEqual(token,control.leases['window-a'])
            self.assertEqual([],provider.closed)
            release.set()
            await self.until(lambda:'window-a' not in control.live_source_rechecks)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(2,state.calls)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        await manager.stop(self.owner,task['id'])

    async def test_child_failure_before_checkpoint_join_is_visible_to_parent_activation(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True)
        await self.request(task,target,manager)
        entered = self.gate()
        release = self.gate(threaded=True)
        loop = asyncio.get_running_loop()
        original = self.service.recheck_task_source
        def slow_activation(*args, **kwargs):
            if kwargs.get('pending_requested_at'):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError('fixture activation was not released')
            return original(*args, **kwargs)
        with patch.object(self.service,'recheck_task_source',slow_activation):
            state.reels_stop.set()
            await asyncio.wait_for(entered.wait(),3)
            state.child_failure = True
            state.child_release.set()
            await asyncio.wait_for(state.child_failure_raised.wait(),3)
            self.assertEqual(token,control.leases['window-a'])
            self.assertEqual([],provider.closed)
            release.set()
            await self.until(lambda:'window-a' not in control.live_source_rechecks)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(2,state.calls)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        await manager.stop(self.owner,task['id'])

    async def test_unknown_child_failure_during_activation_blocks_parent_with_healthy_sibling(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True,child_count=2)
        await self.request(task,target,manager)
        entered = self.gate()
        release = self.gate(threaded=True)
        loop = asyncio.get_running_loop()
        original = self.service.recheck_task_source
        original_stats = self.service.task_mode_candidate_stats
        failed = False
        def slow_activation(*args, **kwargs):
            if kwargs.get('pending_requested_at'):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError('fixture activation was not released')
            return original(*args, **kwargs)
        def fail_child_stats(*args, **kwargs):
            nonlocal failed
            if entered.is_set() and not failed:
                failed = True
                raise RuntimeError('unclassified child failure')
            return original_stats(*args, **kwargs)
        with patch.object(self.service,'recheck_task_source',slow_activation), patch.object(self.service,'task_mode_candidate_stats',fail_child_stats):
            state.reels_stop.set()
            await asyncio.wait_for(entered.wait(),3)
            state.child_release.set()
            await self.until(lambda:state.disconnected == 1)
            self.assertEqual(token,control.leases['window-a'])
            self.assertEqual([],provider.closed)
            release.set()
            await self.until(lambda:'window-a' not in control.live_source_rechecks)
        self.assertFalse(state.source_entered.is_set())
        self.assertEqual(2,state.calls)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        await manager.stop(self.owner,task['id'])

    async def test_prepared_activation_rejects_wrong_generation_without_rewind(self):
        task,target,control,token,manager,provider,state = await self.running_gap(block_parent_stop=True)
        first = await self.request(task,target,manager)
        before = self.checkpoint(task,target)
        with self.assertRaises(ConflictError):
            self.service.recheck_task_source(self.owner,task['id'],target,'followers',
                profile_id='window-a',lease_token=token,live_parent=True,pending_requested_at='wrong-generation')
        with self.assertRaises(ConflictError):
            self.service.recheck_task_source(self.owner,task['id'],target,'followers',
                profile_id='window-a',lease_token='wrong-lease',live_parent=True,
                pending_requested_at=first['target']['source_recheck_requested_at'])
        self.assertEqual(before,self.checkpoint(task,target))
        state.reels_stop.set()
        await manager.stop(self.owner,task['id'])

    async def test_late_request_cannot_resurrect_completed_task(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        request_callback=control.live_source_rechecks['window-a']['request']
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        with self.assertRaises(ConflictError):
            await request_callback()
        with self.assertRaises(ConflictError):
            await self.request(task,target,manager)
        self.assertEqual(2,state.calls)
        self.assertEqual({},self.leases())

    async def test_poisoned_parent_rejects_before_rewind(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        before=self.checkpoint(task,target)
        state.parents[0]._page_stage_abandoned=True
        with self.assertRaises(ConflictError):
            await self.request(task,target,manager)
        self.assertEqual(before,self.checkpoint(task,target))
        self.assertEqual(token,control.leases['window-a'])

    async def test_second_explicit_pass_allowed_after_first_source_finishes(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        first=await self.request(task,target,manager)
        state.source_release.set()
        await self.until(lambda:self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']['state']=='completed')
        # Child remains blocked so source-only ownership is still live.
        second=await self.request(task,target,manager)
        self.assertNotEqual(first['target']['source_recheck_requested_at'],second['target']['source_recheck_requested_at'])
        await self.until(lambda:state.calls==4)
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(4,state.calls)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)

    async def test_children_commit_while_manual_source_is_still_running(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        await self.request(task,target,manager)
        await asyncio.wait_for(state.source_entered.wait(),3)
        self.assertEqual([],state.screened)
        state.child_release.set()
        await self.until(lambda:len(state.screened)==2)
        self.assertFalse(state.source_release.is_set())
        self.assertEqual(0,state.disconnected)
        self.assertEqual([],provider.closed)
        self.assertEqual(token,control.leases['window-a'])
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        state.source_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)

    async def test_concurrent_repeated_requests_share_one_durable_generation(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        results=await asyncio.gather(*(self.request(task,target,manager) for _ in range(8)))
        self.assertEqual(1,len({row['target']['source_recheck_requested_at'] for row in results}))
        self.assertEqual(3,state.calls)
        state.source_release.set();state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(3,state.calls)

    async def test_live_transaction_failure_leaves_children_and_cursor_unchanged(self):
        import sqlite3
        task,target,control,token,manager,provider,state=await self.running_gap()
        before=self.checkpoint(task,target)
        with self.database.write() as connection:
            connection.execute("CREATE TRIGGER fail_live_request BEFORE INSERT ON task_source_rechecks BEGIN SELECT RAISE(ABORT,'live request fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.request(task,target,manager)
        self.assertEqual(before,self.checkpoint(task,target))
        self.assertEqual(token,control.leases['window-a'])
        self.assertTrue(control.pause_event.is_set())
        state.child_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(2,state.calls)
        self.assertEqual(['old_one','old_two'],state.screened)

    async def test_incomplete_manual_source_does_not_cancel_healthy_children(self):
        task,target,control,token,manager,provider,state=await self.running_gap()
        state.source_failure=True
        await self.request(task,target,manager)
        state.source_release.set()
        await asyncio.wait_for(state.source_failed.wait(),3)
        # The old child still owns an unfinished read while the source has failed.
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(state.child_cancelled.is_set())
        self.assertEqual(0,state.disconnected)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])
        self.assertEqual(token,control.leases['window-a'])
        state.child_release.set()
        await self.until(lambda:len(state.screened)==2)
        await manager.stop(self.owner,task['id'])
        self.assertEqual(['old_one','old_two'],state.screened)
        self.assertFalse(manager._checkpoint_resume_cursor(self.checkpoint(task,target))['candidate_spool_complete'])

    async def test_last_child_failure_during_rewind_cannot_complete_or_navigate_parent(self):
        import threading
        task,target,control,token,manager,provider,state=await self.running_gap(block_parent_stop=True)
        entered=asyncio.Event()
        release=threading.Event()
        loop=asyncio.get_running_loop()
        original=self.service.recheck_task_source
        original_stats=self.service.task_mode_candidate_stats
        failed=False
        def slow_commit(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError('test failed to release commit')
            return original(*args,**kwargs)
        def fail_one_stats_read(*args, **kwargs):
            nonlocal failed
            if entered.is_set() and not failed:
                failed=True
                raise RuntimeError('transient candidate stats failure')
            return original_stats(*args,**kwargs)
        with patch.object(self.service,'recheck_task_source',slow_commit), patch.object(
                self.service,'task_mode_candidate_stats',fail_one_stats_read):
            request=asyncio.create_task(self.request(task,target,manager))
            try:
                await asyncio.wait_for(entered.wait(),3)
                state.child_release.set()
                await self.until(lambda:state.disconnected==1)
                # Completion must wait on admission's checkpoint lock even though
                # the last child has exited and no new source event exists yet.
                self.assertFalse(state.source_entered.is_set())
                self.assertEqual(token,control.leases['window-a'])
                self.assertEqual([],provider.closed)
            finally:
                release.set()
            await asyncio.wait_for(request,3)
        state.reels_stop.set()
        await self.until(lambda:'window-a' not in control.live_source_rechecks)
        await manager.stop(self.owner,task['id'])
        self.assertEqual(2,state.calls)
        self.assertFalse(state.source_entered.is_set())
        cursor=manager._checkpoint_resume_cursor(self.checkpoint(task,target))
        self.assertTrue(cursor['candidate_spool_complete'])
        self.assertNotIn('source_recheck_requested',cursor)
        self.assertEqual('prepared',self.service.get_task(self.owner,task['id'])['targets'][0]['source_recheck']['state'])
        self.assertNotEqual('completed',self.service.get_task(self.owner,task['id'])['targets'][0]['status'])

    async def test_following_recheck_without_optional_reels_keeps_child_pool(self):
        task,target,control,token,manager,provider,state=await self.running_gap(mode='following')
        result=await manager.recheck_source(self.owner,task['id'],target,'following')
        self.assertTrue(result['parent_only'])
        await asyncio.wait_for(state.source_entered.wait(),3)
        state.child_release.set()
        await self.until(lambda:len(state.screened)==2)
        self.assertEqual(0,state.disconnected)
        self.assertEqual(token,control.leases['window-a'])
        state.source_release.set()
        await asyncio.wait_for(manager.wait(task['id']),5)
        self.assertEqual(['old_one','old_two','new_one'],state.screened)
        self.assertEqual(3,state.calls)
        self.assertFalse(state.reels_entered.is_set())

if __name__=='__main__': unittest.main()
