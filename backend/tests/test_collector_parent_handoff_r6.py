"""Offline regressions for parent activity yielding the original collection page."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.execution_manager import ExecutionManager
from app.playwright_worker import PlaywrightWorker
from app.errors import ConflictError
from app.playwright_worker import CollectionOutcome
import test_collection_drain_r56 as drain_cases
from test_parallel_relation_pipeline import _PipelineManager


class CollectorParentHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_handoff_joins_guard_without_poisoning_next_seed(self):
        for boundary in ('child_done', 'pause', 'stop', 'owner_cancel'):
            with self.subTest(boundary=boundary):
                entered, release, child_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                worker = PlaywrightWorker(None)
                page = SimpleNamespace(url='https://www.instagram.com/reels/')
                worker.page = page
                async def read_body():
                    entered.set()
                    await release.wait()
                    return 'healthy Instagram page'
                async def current():
                    await worker._await_page_stage(read_body(), timeout=2)
                    return {'key': '/reel/a', 'source': 'a', 'liked': True}
                worker.parent_reels_adapter = lambda: SimpleNamespace(current=current)
                control = SimpleNamespace(profile_id='window-a', owner_user_id='o', task_id='t',
                    pause_event=asyncio.Event(), stop_event=asyncio.Event(), manual_requests={},
                    leases={'window-a': 'generation-a'})
                control.pause_event.set()
                manager = ExecutionManager.__new__(ExecutionManager)
                child = asyncio.create_task(child_release.wait())
                intervention = asyncio.get_running_loop().create_future()
                async def browse(adapter, checkpoint, **kwargs):
                    await checkpoint()
                    await adapter.current()
                    await checkpoint()
                with patch('app.parent_reels.run_parent_reels', browse):
                    activity = asyncio.create_task(manager._browse_parent_reels(
                        control, worker, [child], intervention, lambda: False, 'seed.one'))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        if boundary == 'child_done': child_release.set()
                        elif boundary == 'pause': control.pause_event.clear()
                        elif boundary == 'stop': control.stop_event.set()
                        else: activity.cancel()
                        await asyncio.sleep(.08)
                        # Repeated Cancel must not release native ownership early.
                        activity.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(worker._page_stage_abandoned,
                            'Normal parent handoff poisoned the collection transport')
                        self.assertFalse(activity.done(), 'Handoff passed an in-flight old page operation')
                    finally:
                        release.set(); child_release.set()
                        await asyncio.gather(activity, child, return_exceptions=True)
                self.assertFalse(worker._page_stage_abandoned)
                self.assertFalse(worker._late_lifecycle_tasks)
                # Same page, next seed: no reconnect or automatic-recovery path.
                self.assertEqual('seed.two', await worker._await_page_stage(
                    asyncio.sleep(0, result='seed.two'), timeout=1))

    async def test_revoked_page_or_lease_cannot_start_another_operation(self):
        for replace in ('page', 'lease'):
            with self.subTest(replace=replace):
                operation = AsyncMock()
                cleanup = AsyncMock()
                page = object()
                worker = SimpleNamespace(page=page,
                    parent_reels_adapter=lambda: SimpleNamespace(open=operation, stop=cleanup))
                control = SimpleNamespace(profile_id='window-a', owner_user_id='o', task_id='t',
                    pause_event=asyncio.Event(), stop_event=asyncio.Event(), manual_requests={},
                    leases={'window-a': 'generation-a'})
                control.pause_event.set()
                manager = ExecutionManager.__new__(ExecutionManager)
                child = asyncio.create_task(asyncio.Event().wait())
                async def browse(adapter, checkpoint, **kwargs):
                    if replace == 'page': worker.page = object()
                    else: control.leases['window-a'] = 'generation-b'
                    try:
                        await adapter.open()
                    finally:
                        await adapter.stop()
                try:
                    with patch('app.parent_reels.run_parent_reels', browse):
                        await manager._browse_parent_reels(control, worker, [child],
                            asyncio.get_running_loop().create_future(), lambda: False, 'seed')
                    operation.assert_not_awaited()
                    cleanup.assert_not_awaited()
                finally:
                    child.cancel(); await asyncio.gather(child, return_exceptions=True)

    async def test_unconfirmed_page_close_keeps_original_cleanup_owner(self):
        state = SimpleNamespace(closed=False)
        page = SimpleNamespace(close=AsyncMock(), is_closed=lambda: state.closed)
        worker = PlaywrightWorker(None)
        worker.page = worker._worker_owned_page = page
        await worker._close_page_for_cleanup(page)
        self.assertIs(worker._worker_owned_page, page)
        self.assertIs(worker._retired_pages[id(page)], page)
        state.closed = True
        await worker._close_page_for_cleanup(page)
        self.assertIsNone(worker._worker_owned_page)
        self.assertFalse(worker._retired_pages)
        page.close.assert_awaited_once()

    async def test_cancel_rejects_next_effect_but_joins_document_cleanup(self):
        for operation in ('like', 'advance'):
            with self.subTest(operation=operation):
                entered, release, cleanup_entered, cleanup_release = [asyncio.Event() for _ in range(4)]
                effects = []
                worker = PlaywrightWorker(None)
                worker.page = object()
                async def body_read():
                    entered.set()
                    await release.wait()
                async def action(expected, checkpoint):
                    await worker._await_page_stage(body_read(), timeout=2)
                    await checkpoint()
                    effects.append(operation)
                async def stop():
                    cleanup_entered.set()
                    await cleanup_release.wait()
                worker.parent_reels_adapter = lambda: SimpleNamespace(**{operation: action}, stop=stop)
                control = SimpleNamespace(profile_id=None, owner_user_id='o', task_id='t',
                    pause_event=asyncio.Event(), stop_event=asyncio.Event())
                control.pause_event.set()
                child = asyncio.create_task(asyncio.Event().wait())
                manager = ExecutionManager.__new__(ExecutionManager)
                async def browse(adapter, checkpoint, **kwargs):
                    try:
                        await getattr(adapter, operation)('/reel/a', checkpoint)
                    finally:
                        await adapter.stop()
                with patch('app.parent_reels.run_parent_reels', browse):
                    activity = asyncio.create_task(manager._browse_parent_reels(control, worker, [child],
                        asyncio.get_running_loop().create_future(), lambda: False, 'seed'))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        activity.cancel()
                        await asyncio.sleep(0)
                        release.set()
                        await asyncio.wait_for(cleanup_entered.wait(), 1)
                        activity.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(activity.done())
                        self.assertEqual([], effects)
                        self.assertFalse(worker._page_stage_abandoned)
                    finally:
                        release.set(); cleanup_release.set(); child.cancel()
                        await asyncio.gather(activity, child, return_exceptions=True)


class CollectorSequentialWindowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain_cases.CollectionDrainR56Tests.asyncSetUp
    task = drain_cases.CollectionDrainR56Tests.task
    manager = drain_cases.CollectionDrainR56Tests.manager
    gate = drain_cases.CollectionDrainR56Tests.gate
    until = drain_cases.CollectionDrainR56Tests.until
    leases = drain_cases.CollectionDrainR56Tests.leases

    async def asyncTearDown(self):
        if hasattr(self, 'close_provider'):
            self.close_provider.result = {'closed': True}
        await drain_cases.CollectionDrainR56Tests.asyncTearDown(self)

    def lease_token(self):
        with self.database.read() as connection:
            return connection.execute('SELECT lease_token FROM browser_operation_leases '
                'WHERE profile_id=?', ('window-a',)).fetchone()[0]

    async def test_two_seed_pipeline_then_close_failure_and_next_job_same_window(self):
        entered, release = self.gate(), self.gate()
        collected, children, workers, events = [], [], [], []
        operator_page = SimpleNamespace(close=AsyncMock())
        class Child:
            def __init__(self, gate):
                self.gate = gate
                self.stopped = False
            async def screen(self, username):
                await self.gate.wait()
                events.append(('screened', username))
            async def disconnect(self):
                self.stopped = True
                events.append(('child_closed', None))

        class Worker(PlaywrightWorker):
            async def connect(self, profile_id, **kwargs):
                self.profile_id = profile_id
                state = SimpleNamespace(closed=False)
                async def close():
                    events.append(('page_closed', self.target))
                    state.closed = True
                self.page = self._worker_owned_page = SimpleNamespace(
                    close=close, is_closed=lambda: state.closed)
                self.operator_page = operator_page
                workers.append(self)
            async def create_parallel_screening_worker(self):
                self.child_gate = asyncio.Event()
                child = Child(self.child_gate)
                children.append(child)
                return child
            async def collect_followers(self, target, *, candidate_sink, **kwargs):
                # The next real source executes the same page-stage gate.
                await self._await_page_stage(asyncio.sleep(0), timeout=1)
                self.target = target
                collected.append(target)
                await candidate_sink([target + '.fan'])
                return CollectionOutcome('followers', [], source_total=1)
            def parent_reels_adapter(self):
                async def read_body():
                    self.child_gate.set()
                    entered.set()
                    await release.wait()
                    events.append(('parent_operation_joined', self.target))
                async def open_reels():
                    await self._await_page_stage(read_body(), timeout=5)
                return SimpleNamespace(open=open_reels,
                    current=AsyncMock(return_value={'key':'/reel/a', 'source':'a', 'liked':True}),
                    stop=AsyncMock())

        task = self.task(sources=('seed_one', 'seed_two'), parallel_screening_workers=1)
        provider = self.close_provider = drain_cases.CloseProvider()
        provider.result = {'closed': False}
        manager, _ = self.manager(Worker, provider, _PipelineManager)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(entered.wait(), 3)
        control = manager._runs[task['id']]
        token = control.leases['window-a']
        await self.until(lambda: children and children[0].stopped)
        self.assertEqual(['seed_one'], collected)
        self.assertEqual(token, self.lease_token())
        self.assertEqual([], provider.closed)
        # A duplicate start cannot bypass the cleanup/operation boundary.
        with self.assertRaises(ConflictError):
            await manager.start(self.owner, task['id'])
        release.set()
        await self.until(lambda: control.profile_states.get('window-a', {}).get('reason') == 'browser_close_failed')
        self.assertEqual(['seed_one', 'seed_two'], collected)
        self.assertEqual(1, len(workers))
        self.assertTrue(all(child.stopped for child in children))
        self.assertFalse(workers[0]._page_stage_abandoned)
        self.assertIsNone(workers[0]._worker_owned_page)
        operator_page.close.assert_not_awaited()
        self.assertEqual(token, self.lease_token())
        final = self.service.get_task(self.owner, task['id'])
        self.assertTrue(all(row['status'] == 'completed' for row in final['targets']))
        # An older completed card may be hidden while its sibling uses the
        # window. Hiding it must not release the sibling's cleanup lease.
        await manager.dismiss_completed_target(self.owner, task['id'], final['targets'][0]['id'])
        self.assertEqual(token, self.lease_token())
        with self.assertRaises(ConflictError):
            await manager.dismiss_completed_target(self.owner, task['id'], final['targets'][1]['id'])
        following = self.task(sources=('seed_three',))
        with self.assertRaises(ConflictError):
            await manager.start(self.owner, following['id'])
        self.assertEqual(token, self.lease_token())
        provider.result = {'closed': True}
        await manager.resume_window(self.owner, task['id'], 'window-a')
        await asyncio.wait_for(manager.wait(task['id']), 5)
        self.assertEqual({}, self.leases())
        await manager.start(self.owner, following['id'])
        await asyncio.wait_for(manager.wait(following['id']), 5)
        self.assertEqual(['seed_one', 'seed_two', 'seed_three'], collected)
        self.assertEqual(2, len(workers))
        self.assertEqual(['window-a'] * 3, provider.closed)
        self.assertEqual({}, self.leases())
        self.assertTrue(all(child.stopped for child in children))
        operator_page.close.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
