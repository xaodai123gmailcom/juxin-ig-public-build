"""Fault injection for progress during rerenders and cancellation-hostile CDP."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.collection_surface import RELATION_ROWS_SCRIPT
from test_relation_completion_r43 import Worker, Surface, frame


class CollectionFaultsR94(unittest.IsolatedAsyncioTestCase):
    async def hover_transport(self, stage, *, stop=False, cooperative=False):
        worker = PlaywrightWorker(None)
        worker.cdp_command_timeout_seconds = .01
        worker._ensure_window_surface_stable = AsyncMock()
        worker._collection_checkpoint = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        expected = {'username': 'target', 'posts': 1, 'followers': 2,
                    'following': 3, 'evidence': 'relationship_hover_card'}
        sends = []

        async def send(method, params):
            sends.append(params)
            if stage == 'clear' or params['x'] != 1:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        if cooperative:
                            raise
            return {}

        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/target/'),
            hover=AsyncMock(side_effect=RuntimeError('hover input unavailable')
                            if stage == 'fallback' else None),
            bounding_box=AsyncMock(return_value=dict(x=10, y=10, width=20, height=20)))
        dialog = SimpleNamespace(locator=lambda _: SimpleNamespace(
            count=AsyncMock(return_value=1), nth=lambda _: row))
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected),
            mouse=SimpleNamespace(move=AsyncMock()))
        # The baseline card must be absent before attempting the new hover.
        worker.page.evaluate.side_effect = [None, expected] if stage == 'clear' else None
        if stage == 'fallback':
            worker.page.evaluate.return_value = None
        worker._cdp_session = SimpleNamespace(send=send)
        task = asyncio.create_task(worker._read_relation_hover_preview(dialog, 'target'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if stop:
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=.8)
            self.assertIn(task, done, 'a stalled mouse command must have a real deadline')
            if stop:
                with self.assertRaises(asyncio.CancelledError):
                    await task
            elif cooperative:
                self.assertEqual(expected, await task)
                worker.page.mouse.move.assert_awaited_once_with(1, 1)
                self.assertFalse(worker._page_stage_abandoned)
                return
            else:
                with self.assertRaises(WorkerExecutionError) as error:
                    await task
                self.assertEqual('worker_not_connected', error.exception.code)
            self.assertTrue(worker._page_stage_abandoned)
            self.assertEqual(1 if stage == 'clear' else 2, len(sends))
            worker.page.mouse.move.assert_not_awaited()
            if stage == 'clear':
                row.hover.assert_not_awaited()
            with self.assertRaises(WorkerExecutionError):
                await worker._await_page_stage(asyncio.sleep(0), timeout=.01)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(30):
                if not worker._late_lifecycle_tasks:
                    break
                await asyncio.sleep(.001)
            self.assertFalse(worker._late_lifecycle_tasks)

    async def test_hostile_clear_mouse_cannot_overlap_next_hover(self):
        await self.hover_transport('clear')

    async def test_hostile_fallback_mouse_cannot_retry_same_transport(self):
        await self.hover_transport('fallback')

    async def test_stop_during_mouse_input_invalidates_transport(self):
        for stage in ('clear', 'fallback'):
            with self.subTest(stage=stage):
                await self.hover_transport(stage, stop=True)

    async def test_cooperative_mouse_timeout_preserves_pointer_fallback(self):
        await self.hover_transport('clear', cooperative=True)

    async def test_confirmed_hover_survives_next_row_disconnect_or_stop(self):
        for failure in (WorkerExecutionError('disconnected', reason='worker_not_connected'),
                        asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                worker = Worker()
                surface = Surface(worker, ['confirmed', 'broken'])
                worker._read_relation_hover_preview = AsyncMock(side_effect=[
                    {'username': 'confirmed', 'posts': 1, 'followers': 2,
                     'following': 3, 'evidence': 'relationship_hover_card'}, failure])
                saved = {}
                async def sink(names, evidence):
                    saved.update(evidence)
                    return len(saved)
                with self.assertRaises(type(failure)):
                    await worker._read_visible_account_dialog(surface, None,
                        candidate_sink=sink, hover_precheck=True)
                self.assertEqual({'confirmed'}, set(saved))
                self.assertEqual('confirmed', saved['confirmed']['username'])

    async def test_slow_duplicate_database_check_does_not_end_loading_grace(self):
        worker = PlaywrightWorker(None)
        worker.collection_loading_grace_seconds = .05
        worker.collection_poll_interval_seconds = .005
        worker.collection_settled_idle_rounds = 1
        worker._guard = AsyncMock()
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        worker._confirm_relation_list_end = AsyncMock(return_value=True)
        worker._relation_list_end_is_current = AsyncMock(return_value=True)
        saved = {'first'}
        reads = 0
        async def evaluate(script):
            nonlocal reads
            if script == RELATION_ROWS_SCRIPT:
                reads += 1
                return frame('first') if reads < 4 else frame('first', 'late')
            return dict(valid=True, bottom=True, moved=False, top=600, height=1000, client=400)
        async def duplicate(name):
            if name == 'first':
                await asyncio.sleep(.07)
                return True
            return False
        async def sink(names, evidence):
            saved.update(names)
            return len(saved)
        worker.hover_duplicate_check = duplicate
        worker._read_relation_hover_preview = AsyncMock(return_value={
            'username': 'late', 'posts': 1, 'followers': 2, 'following': 3,
            'evidence': 'relationship_hover_card'})
        await asyncio.wait_for(worker._read_visible_account_dialog(
            SimpleNamespace(evaluate=evaluate), None, candidate_sink=sink,
            hover_precheck=True, initial_candidate_count=1), 2)
        self.assertEqual({'first', 'late'}, saved)
        self.assertEqual(['late'], [call.args[1] for call in
            worker._read_relation_hover_preview.await_args_list])

    async def test_repeated_productive_hover_repaints_finish_and_checkpoint_every_frame(self):
        for mode in ('followers', 'following'):
            with self.subTest(mode=mode):
                worker=Worker(); surface=Surface(worker, ['account0'])
                saved=set(); progress=[]
                async def hover(_surface, name):
                    number=int(name.removeprefix('account'))
                    if number < 7:surface.names=[f'account{number+1}']
                    return {'username':name,'posts':1,'followers':10,'following':10,'evidence':'relationship_hover_card'}
                async def sink(names, _evidence):
                    saved.update(names);return {'total':len(saved)}
                async def checkpoint(value):progress.append(value)
                worker._read_relation_hover_preview=hover
                await asyncio.wait_for(worker._read_visible_account_dialog(surface,None,
                    surface_kind=mode,candidate_sink=sink,hover_precheck=True,
                    scan_progress_sink=checkpoint),5)
                self.assertEqual({f'account{i}' for i in range(8)},saved)
                # Pending-identity durability updates are separate from a
                # productive-frame cursor. Both must survive every repaint.
                frames=[x for x in progress if 'resume_tail' in x]
                pending=[x for x in progress if 'pending_relation_usernames' in x]
                self.assertEqual([[f'account{i}'] for i in range(8)],[x['resume_tail'] for x in frames])
                self.assertEqual(list(range(1,9)),[x['progress_epoch'] for x in frames])
                self.assertEqual([1]*8,[x['rendered_count'] for x in frames])
                self.assertEqual(
                    [state for i in range(8) for state in ([f'account{i}'],[])],
                    [x['pending_relation_usernames'] for x in pending])
                self.assertTrue(all(set(x)=={'pending_relation_usernames'} for x in pending))
                self.assertEqual(len(progress),len(frames)+len(pending))
                self.assertGreaterEqual(surface.measures,2)

    async def test_unproductive_hover_repaints_still_have_a_finite_retry_budget(self):
        worker=Worker();surface=Surface(worker,['first']);saved={'first','second'};calls=[]
        async def hover(_surface,name):
            calls.append(name);surface.names=['second' if name=='first' else 'first']
            return {'username':name,'posts':1,'followers':10,'following':10,'evidence':'relationship_hover_card'}
        async def sink(names,_evidence):saved.update(names);return {'total':len(saved)}
        worker._read_relation_hover_preview=hover
        with self.assertRaises(WorkerExecutionError) as error:
            await asyncio.wait_for(worker._read_visible_account_dialog(surface,None,
                surface_kind='followers',candidate_sink=sink,hover_precheck=True,
                initial_candidate_count=2),3)
        self.assertEqual('instagram_followers_list_incomplete',error.exception.code)
        self.assertLessEqual(len(calls),5);self.assertEqual({'first','second'},saved)

    async def hostile(self, operation, *, stop=False, cooperative=False):
        worker=Worker();worker.collection_dom_operation_timeout_seconds=.01
        release=asyncio.Event();entered=asyncio.Event()
        async def short_deadline(awaitable, *, timeout):
            return await PlaywrightWorker._await_page_stage(worker, awaitable, timeout=.01)
        worker._await_page_stage=short_deadline
        async def wedged(*args,**kwargs):
            entered.set()
            while not release.is_set():
                try:await release.wait()
                except asyncio.CancelledError:
                    if cooperative:raise
            return {'valid':True,'bottom':True,'moved':True,'top':600,'height':1000,'client':400}
        worker._has_visible_relation_loading_indicator=AsyncMock(return_value=False)
        surface=SimpleNamespace(evaluate=wedged)
        saved=set()
        if operation=='scroll':
            worker._read_visible_account_hrefs=AsyncMock(return_value=['/saved/'])
            async def sink(names):saved.update(names);return len(saved)
            read=worker._read_visible_account_dialog(surface,None,surface_kind='followers',candidate_sink=sink)
        elif operation=='confirm':read=worker._confirm_relation_list_end(surface)
        elif operation=='recheck':
            worker._relation_end_measurement={'valid':True,'bottom':True,'top':600,'height':1000,'client':400}
            read=worker._relation_list_end_is_current(surface)
        elif operation=='loader':
            surface=SimpleNamespace(locator=lambda _:SimpleNamespace(count=wedged))
            read=PlaywrightWorker._has_visible_relation_loading_indicator(worker,surface)
        else:
            page=SimpleNamespace(url='https://www.instagram.com/source/',
                locator=lambda _:SimpleNamespace(inner_text=wedged,evaluate=wedged))
            worker.page=page
            if operation=='guard':read=PlaywrightWorker._guard(worker)
            elif operation=='body':read=worker._page_body_text()
            elif operation=='guard_evidence':
                read=worker._classify_page_guard(page,page.url,'Source account')
            else:read=worker._visible_transport_failure(page,'Source account')
        task=asyncio.create_task(read)
        try:
            await asyncio.wait_for(entered.wait(),1)
            if stop:task.cancel()
            done,_=await asyncio.wait({task},timeout=.8)
            self.assertIn(task,done,operation+' must not wait forever for CDP cancellation')
            if stop:
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertTrue(worker._page_stage_abandoned)
                return
            if cooperative:
                if operation=='scroll':
                    with self.assertRaises(WorkerExecutionError) as error:await task
                    self.assertEqual('instagram_followers_list_incomplete',error.exception.code)
                    self.assertEqual({'saved'},saved)
                else:self.assertEqual(operation=='loader',await task)
                self.assertFalse(worker._page_stage_abandoned)
                return
            with self.assertRaises(WorkerExecutionError) as error:await task
            self.assertEqual('worker_not_connected',error.exception.code)
            self.assertTrue(worker._page_stage_abandoned)
            if operation=='scroll':self.assertEqual({'saved'},saved)
            with self.assertRaises(WorkerExecutionError):
                await worker._await_page_stage(asyncio.sleep(0),timeout=.01)
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            for _ in range(20):
                if not worker._late_lifecycle_tasks:break
                await asyncio.sleep(.001)

    async def test_hostile_scroll_cannot_hang_collection(self):await self.hostile('scroll')
    async def test_hostile_bottom_confirmation_cannot_hang_collection(self):await self.hostile('confirm')
    async def test_hostile_final_bottom_recheck_cannot_hang_collection(self):await self.hostile('recheck')
    async def test_hostile_loader_probe_cannot_hang_collection(self):await self.hostile('loader')
    async def test_hostile_page_guard_cannot_hang_collection(self):await self.hostile('guard')
    async def test_hostile_page_body_cannot_hang_collection(self):await self.hostile('body')
    async def test_hostile_guard_evidence_cannot_hang_collection(self):await self.hostile('guard_evidence')
    async def test_hostile_network_evidence_cannot_hang_collection(self):await self.hostile('network_evidence')

    async def test_stop_during_hostile_dom_does_not_wait_for_the_transport(self):
        for operation in ('scroll','confirm','recheck','loader','guard','body','guard_evidence','network_evidence'):
            with self.subTest(operation=operation):await self.hostile(operation,stop=True)

    async def test_cooperative_timeouts_do_not_confirm_completion_or_poison_worker(self):
        for operation in ('scroll','confirm','recheck','loader'):
            with self.subTest(operation=operation):await self.hostile(operation,cooperative=True)

    async def test_closed_page_is_never_treated_as_healthy_missing_evidence(self):
        for operation in ('guard','body','guard_evidence','network_evidence'):
            with self.subTest(operation=operation):
                worker=Worker()
                closed=AsyncMock(side_effect=RuntimeError('Target page, context or browser has been closed'))
                page=SimpleNamespace(url='https://www.instagram.com/source/',
                    locator=lambda _:SimpleNamespace(inner_text=closed,evaluate=closed))
                worker.page=page
                if operation=='guard':read=PlaywrightWorker._guard(worker)
                elif operation=='body':read=worker._page_body_text()
                elif operation=='guard_evidence':
                    read=worker._classify_page_guard(page,page.url,'Source account')
                else:read=worker._visible_transport_failure(page,'Source account')
                with self.assertRaises(WorkerExecutionError) as error:await read
                self.assertEqual('worker_not_connected',error.exception.code)

if __name__=='__main__':unittest.main()
