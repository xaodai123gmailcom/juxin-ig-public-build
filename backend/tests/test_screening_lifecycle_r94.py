"""Fixed screening slots must include opening and closing native pages."""
import asyncio
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.playwright_worker import WorkerExecutionError, CollectionOutcome
from app.execution_manager import ExecutionManager
from app.errors import ConflictError
import test_parallel_relation_pipeline as pipeline
import test_manual_collection_r51 as manual
from test_parallel_screening_worker import _Page, _Session, _connected_parent


class ScreeningLifecycleR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_slot_handoffs_keep_native_count_bounded_and_close_all_pages(self):
        rng = random.Random(94)
        pages = []
        class Page(_Page):
            closed = False
            def is_closed(self):
                return self.closed
            async def close(self):
                self.close_calls += 1
                await asyncio.sleep(0)
                if int(self.name) % 5 == 0 and self.close_calls == 1:
                    raise RuntimeError('transient native close')
                self.closed = True
        async def new_page():
            page = Page(str(len(pages)))
            pages.append(page)
            self.assertLessEqual(sum(not item.closed for item in pages), 3)
            await asyncio.sleep(0)
            return page
        parent = _connected_parent(SimpleNamespace(new_page=new_page), _Page('source'))
        try:
            for _ in range(40):
                children = list(parent._screening_worker_pool.values())
                rng.shuffle(children)
                for child in children[:rng.randrange(4)]:
                    if rng.choice((True, False)):
                        parent.release_parallel_screening_worker(child)
                    else:
                        await child.disconnect()
                results = await asyncio.gather(*(parent.create_parallel_screening_worker()
                    for _ in range(rng.randrange(1, 6))), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        self.assertIsInstance(result, WorkerExecutionError)
                        self.assertEqual('screening_slots_busy', result.code)
                    else:
                        self.assertIs(parent._screening_worker_pool[result._task_page_slot], result)
                self.assertLessEqual(len(parent._screening_worker_pool), 3)
                self.assertLessEqual(sum(not item.closed for item in pages), 3)
        finally:
            await parent.disconnect()
            await parent.disconnect()  # Retry any final one-shot native failure.
        self.assertEqual({}, parent._screening_worker_pool)
        self.assertEqual({}, parent._screening_slots_opening)
        self.assertFalse(parent._late_lifecycle_tasks)
        self.assertTrue(all(page.closed for page in pages))

    async def settle(self, worker):
        for _ in range(100):
            if not worker._late_lifecycle_tasks:
                return
            await asyncio.sleep(.001)
        self.fail('late resource cleanup did not drain')

    async def test_concurrent_creations_cannot_overwrite_slots_or_exceed_three(self):
        pages = []
        async def new_page():
            page = _Page(str(len(pages)))
            pages.append(page)
            await asyncio.sleep(0)
            return page
        parent = _connected_parent(SimpleNamespace(new_page=new_page), _Page('source'))
        results = await asyncio.gather(*(parent.create_parallel_screening_worker()
            for _ in range(5)), return_exceptions=True)
        children = [item for item in results if not isinstance(item, BaseException)]
        try:
            self.assertEqual(3, len(pages))
            self.assertEqual([1, 2, 3], sorted(child._task_page_slot for child in children))
            self.assertEqual(2, sum(isinstance(item, WorkerExecutionError) for item in results))
            self.assertEqual(set(children), set(parent._screening_worker_pool.values()))
        finally:
            await asyncio.gather(*(child.disconnect() for child in children))
            await parent.disconnect()
        self.assertTrue(all(page.close_calls == 1 for page in pages))

    async def test_parent_disconnect_fences_late_page_and_session_creation(self):
        for phase in ('page', 'session'):
            with self.subTest(phase=phase):
                entered, release = asyncio.Event(), asyncio.Event()
                page, session = _Page('child'), _Session()
                async def new_page():
                    if phase == 'page':
                        entered.set()
                        await release.wait()
                    return page
                async def new_session(_page):
                    if phase == 'session':
                        entered.set()
                        await release.wait()
                    return session
                source = _Page('source')
                parent = _connected_parent(SimpleNamespace(new_page=new_page,
                    new_cdp_session=new_session), source)
                creation = asyncio.create_task(parent.create_parallel_screening_worker())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await parent.disconnect()
                    release.set()
                    with self.assertRaises(WorkerExecutionError):
                        await asyncio.wait_for(creation, 1)
                    self.assertEqual({}, parent._screening_worker_pool)
                    self.assertEqual(1, page.close_calls)
                    self.assertEqual(0, source.close_calls)
                    if phase == 'session':
                        self.assertEqual(1, session.detach_calls)
                finally:
                    release.set()
                    result = await asyncio.gather(creation, return_exceptions=True)
                    if not isinstance(result[0], BaseException):
                        await result[0].disconnect()
                    await parent.disconnect()

    async def test_closing_child_holds_its_slot_until_native_close_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class ClosingPage(_Page):
            async def close(self):
                entered.set()
                await release.wait()
                await super().close()
        pages = [ClosingPage('closing'), _Page('second')]
        async def new_page():
            return pages.pop(0)
        parent = _connected_parent(SimpleNamespace(new_page=new_page), _Page('source'))
        child = await parent.create_parallel_screening_worker()
        closing = asyncio.create_task(child.disconnect())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            replacement = await parent.create_parallel_screening_worker()
            self.assertEqual(2, replacement._task_page_slot,
                'slot 1 still belongs to its closing native page')
            self.assertIs(parent._screening_worker_pool[1], child)
        finally:
            release.set()
            await closing
            await parent.disconnect()

    async def test_timed_out_page_creations_still_count_toward_slot_limit(self):
        release = asyncio.Event()
        pages = []
        async def new_page():
            page = _Page(str(len(pages)))
            pages.append(page)
            await release.wait()
            return page
        parent = _connected_parent(SimpleNamespace(new_page=new_page), _Page('source'))
        parent.page_create_timeout_seconds = .01
        try:
            for _ in range(3):
                with self.assertRaises(WorkerExecutionError) as error:
                    await parent.create_parallel_screening_worker()
                self.assertEqual('parallel_screening_page_create_timeout', error.exception.code)
            with self.assertRaises(WorkerExecutionError) as error:
                await parent.create_parallel_screening_worker()
            self.assertEqual('screening_slots_busy', error.exception.code)
            self.assertEqual(3, len(pages))
        finally:
            release.set()
            await self.settle(parent)
            await parent.disconnect()
        self.assertTrue(all(page.close_calls == 1 for page in pages))

    async def test_late_broken_adapter_cannot_close_operator_source(self):
        release = asyncio.Event()
        source = _Page('operator')
        async def new_page():
            await release.wait()
            return source
        parent = _connected_parent(SimpleNamespace(new_page=new_page), source)
        parent.page_create_timeout_seconds = .01
        try:
            with self.assertRaises(WorkerExecutionError):
                await parent.create_parallel_screening_worker()
        finally:
            release.set()
            await self.settle(parent)
        self.assertEqual(0, source.close_calls)
        self.assertIs(parent.page, source)
        await parent.disconnect()

    async def test_concurrent_disconnect_closes_child_once_and_releases_slot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                entered.set()
                await release.wait()
        page = Page('child')
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
        child = await parent.create_parallel_screening_worker()
        closing = [asyncio.create_task(child.disconnect()) for _ in range(4)]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(1, page.close_calls)
            self.assertIs(parent._screening_worker_pool[1], child)
        finally:
            release.set()
            await asyncio.gather(*closing)
            await parent.disconnect()
        self.assertEqual(1, page.close_calls)

    async def test_completed_creator_must_drain_queued_cleanup_before_reconnect(self):
        parent = _connected_parent(SimpleNamespace(), _Page('source'))
        parent._page_stage_abandoned = True
        parent._connect_impl = AsyncMock()
        release, entered = asyncio.Event(), asyncio.Event()
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(object())
        async def cleanup(_result):
            entered.set()
            await release.wait()
        parent._track_late_lifecycle_task(completed, late_result_cleanup=cleanup)
        try:
            with self.assertRaises(WorkerExecutionError):
                await parent.connect('profile-17')
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(WorkerExecutionError):
                await parent.connect('profile-17')
            parent._connect_impl.assert_not_awaited()
        finally:
            release.set()
            await self.settle(parent)
        await parent.connect('profile-17')
        parent._connect_impl.assert_awaited_once()
        self.assertFalse(parent._page_stage_abandoned)

    async def test_hostile_child_close_blocks_reconnect_until_late_cleanup_finishes(self):
        release = asyncio.Event()
        class Page(_Page):
            async def close(self):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                await super().close()
        page = Page('child')
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
        parent.disconnect_timeout_seconds = .01
        child = await parent.create_parallel_screening_worker()
        parent._connect_impl = AsyncMock()
        try:
            await asyncio.wait_for(parent.disconnect(), 1)
            self.assertIs(parent._screening_worker_pool[1], child)
            self.assertTrue(parent._late_lifecycle_tasks)
            with self.assertRaises(WorkerExecutionError) as error:
                await parent.connect('profile-17')
            self.assertEqual('browser_operations_pending', error.exception.code)
            parent._connect_impl.assert_not_awaited()
        finally:
            release.set()
            await self.settle(child)
            await self.settle(parent)
        self.assertEqual({}, parent._screening_worker_pool)
        await parent.connect('profile-17')
        parent._connect_impl.assert_awaited_once()
        self.assertEqual(1, page.close_calls)

    async def test_failed_construction_keeps_late_closing_page_in_its_slot(self):
        release = asyncio.Event()
        class Page(_Page):
            async def close(self):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                await super().close()
        page = Page('failed-child')
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(return_value=page)), _Page('source'))
        parent.disconnect_timeout_seconds = .01
        parent._label_task_page_best_effort = AsyncMock(side_effect=RuntimeError('construction cancelled'))
        try:
            with self.assertRaisesRegex(RuntimeError, 'construction cancelled'):
                await parent.create_parallel_screening_worker()
            self.assertIn(1, parent._screening_worker_pool,
                'failed unpublished children still own a physical slot until close settles')
        finally:
            release.set()
            await self.settle(parent)
            await parent.disconnect()
        self.assertEqual(1, page.close_calls)
        self.assertEqual({}, parent._screening_worker_pool)

    async def test_failed_late_creators_release_slots_and_allow_subsequent_creation(self):
        release = asyncio.Event()
        async def new_page():
            await release.wait()
            raise RuntimeError('native creation failed late')
        context = SimpleNamespace(new_page=new_page)
        parent = _connected_parent(context, _Page('source'))
        parent.page_create_timeout_seconds = .01
        try:
            for _ in range(3):
                with self.assertRaises(WorkerExecutionError):
                    await parent.create_parallel_screening_worker()
            release.set()
            await self.settle(parent)
            context.new_page = AsyncMock(return_value=_Page('recovered'))
            child = await parent.create_parallel_screening_worker()
            self.assertEqual(1, child._task_page_slot)
        finally:
            release.set()
            await self.settle(parent)
            await parent.disconnect()

    async def test_native_close_failure_retains_page_for_retry(self):
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError('native close temporarily unavailable')
        page, replacement = Page('first'), _Page('replacement')
        parent = _connected_parent(SimpleNamespace(new_page=AsyncMock(
            side_effect=[page, replacement])), _Page('source'))
        child = await parent.create_parallel_screening_worker()
        try:
            await child.disconnect()
            self.assertIs(parent._screening_worker_pool.get(1), child)
            self.assertIs(child._worker_owned_page, page)
            fresh = await parent.create_parallel_screening_worker()
            self.assertEqual(2, page.close_calls)
            self.assertIs(fresh.page, replacement)
            self.assertEqual(1, fresh._task_page_slot)
        finally:
            await parent.disconnect()

    async def test_late_creator_close_failure_is_owned_until_parent_retries(self):
        release = asyncio.Event()
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError('late page close failed')
        page = Page('late')
        async def new_page():
            await release.wait()
            return page
        parent = _connected_parent(SimpleNamespace(new_page=new_page), _Page('source'))
        parent.page_create_timeout_seconds = .01
        try:
            with self.assertRaises(WorkerExecutionError):
                await parent.create_parallel_screening_worker()
            release.set()
            await self.settle(parent)
            self.assertIn(1, parent._screening_worker_pool)
            self.assertIs(parent._screening_worker_pool[1]._worker_owned_page, page)
        finally:
            release.set()
            await self.settle(parent)
            await parent.disconnect()
        self.assertEqual(2, page.close_calls)
        self.assertEqual({}, parent._screening_worker_pool)

    async def test_failed_source_close_prevents_connect_until_retry_reclaims_page(self):
        class Page(_Page):
            async def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError('source close unavailable')
        page = Page('owned-source')
        parent = _connected_parent(SimpleNamespace(), page)
        parent._worker_owned_page = page
        parent._connect_impl = AsyncMock()
        await parent.disconnect()
        with self.assertRaises(WorkerExecutionError):
            await parent.connect('profile-17')
        parent._connect_impl.assert_not_awaited()
        await parent.disconnect()
        await parent.connect('profile-17')
        parent._connect_impl.assert_awaited_once()
        self.assertEqual(2, page.close_calls)


class ScreeningRecoveryR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    make_run = manual.ManualCollectionR51Tests.make_run

    async def test_pending_local_cleanup_recovers_without_manual_continue(self):
        waiting = asyncio.Event()
        observed = []
        class Worker:
            connections = 0
            async def connect(self, *_args, **_kwargs):
                self.connections += 1
                if self.connections == 1:
                    raise WorkerExecutionError('old operations draining',
                        reason='browser_operations_pending', pause_required=True)
            async def disconnect(self):
                pass
            async def collect_followers(self, *_args, **_kwargs):
                return CollectionOutcome('followers', [], source_total=0)
        class Manager(ExecutionManager):
            async def _set_network_waiting(self, *args, **kwargs):
                result = await super()._set_network_waiting(*args, **kwargs)
                observed.append(kwargs['retry_delay'])
                waiting.set()
                return result
        task = self.service.create_task(self.user['id'], name='automatic cleanup recovery',
            modes=['followers'], targets=['cleanup_source'], window_ids=['cleanup_window'],
            settings={'local_person_recognition': False, 'location_enabled': False})
        worker = Worker()
        manager = Manager(self.service, pipeline._NoopBitBrowser(), worker_factory=lambda _: worker,
            network_retry_delays=(.01,), network_retry_stagger_seconds=0)
        try:
            await manager.start(self.user['id'], task['id'])
            await asyncio.wait_for(waiting.wait(), 5)
            self.assertIsNotNone(observed[0], 'internal cleanup must not require manual Continue')
            await asyncio.wait_for(manager.wait(task['id']), 5)
            self.assertEqual('completed', self.service.get_task(self.user['id'], task['id'])['status'])
            self.assertEqual(2, worker.connections)
            self.assertEqual(['cleanup_window'], manager.bitbrowser.closed)
            self.assertEqual([], self.service.list_browser_lease_states(self.user['id']))
        finally:
            await manager.shutdown()

    async def manual_during_lifecycle(self, phase):
        task, manager, control, seen, source, profiles, _, _ = await self.make_run()
        parent = control.profile_workers['manual_window']
        pending = asyncio.get_running_loop().create_future()
        if phase == 'opening':
            parent._screening_slots_opening = {1: object()}
        else:
            seen['children'][0]._disconnect_task = pending
        token = control.leases['manual_window']
        begin = asyncio.create_task(manager.begin_manual_control(self.user['id'], 'manual_window', token))
        try:
            await manual.until(lambda: 'manual_window' in control.manual_requests)
            source.set(); profiles.set()
            with self.assertRaises(ConflictError):
                await asyncio.wait_for(begin, 5)
            self.assertFalse(manager.manual_control_active(self.user['id'], 'manual_window', token))
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user['id'])))
        finally:
            pending.set_result(None)
            source.set(); profiles.set()
            await manager.shutdown()
            await asyncio.gather(begin, return_exceptions=True)

    async def test_opening_pages_prevent_premature_manual_input(self):
        await self.manual_during_lifecycle('opening')

    async def test_closing_pages_prevent_premature_manual_input(self):
        await self.manual_during_lifecycle('closing')
