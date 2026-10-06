"""Opening/list-metadata stages must not wedge before the bounded scan begins."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class CollectionPreflightR94Tests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, phase, *, stop=False):
        worker=PlaywrightWorker(None)
        release=asyncio.Event(); entered=asyncio.Event()
        async def wedged(*args, **kwargs):
            entered.set()
            while not release.is_set():
                try:await release.wait()
                except asyncio.CancelledError:pass
            return None
        async def deadline(operation, *, timeout):
            return await PlaywrightWorker._await_page_stage(worker,operation,timeout=.01)
        worker._await_page_stage=deadline
        worker._guard=AsyncMock()
        worker._is_current_profile=lambda _:True
        worker.page=SimpleNamespace(url='https://www.instagram.com/source/',goto=wedged)
        if phase=='visible_control':
            read=worker._first_visible(SimpleNamespace(count=wedged))
        elif phase=='relation_count':
            worker._visible_profile_header=wedged
            read=worker._visible_relation_count('source','followers')
        elif phase=='source_metadata':
            worker._visible_profile_header=wedged
            read=worker._source_profile_metrics('source',relation='followers',source_total=10)
        elif phase=='surface_probe':
            worker.page.locator=lambda _:SimpleNamespace(evaluate_all=wedged)
            read=worker._select_relation_surface('source','followers')
        elif phase=='trigger_probe':
            worker.page.locator=lambda _:SimpleNamespace(evaluate_all=wedged)
            read=worker._find_relation_trigger('source','followers')
        else:
            worker._select_relation_surface=AsyncMock(return_value=None)
            worker._find_relation_trigger=AsyncMock(return_value=SimpleNamespace(click=wedged) if phase=='trigger_click' else None)
            read=worker._open_relation_surface('source','followers')
        task=asyncio.create_task(read)
        try:
            await asyncio.wait_for(entered.wait(),1)
            if stop:task.cancel()
            done,_=await asyncio.wait({task},timeout=.8)
            self.assertIn(task,done,phase+' must terminate without waiting for hostile cancellation')
            if stop:
                with self.assertRaises(asyncio.CancelledError):await task
            else:
                with self.assertRaises(WorkerExecutionError) as error:await task
                self.assertEqual('worker_not_connected',error.exception.code)
            self.assertTrue(worker._page_stage_abandoned)
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            for _ in range(20):
                if not worker._late_lifecycle_tasks:break
                await asyncio.sleep(.001)

    async def test_all_preflight_stages_have_a_real_deadline(self):
        for phase in ('visible_control','relation_count','source_metadata','surface_probe','trigger_probe','trigger_click','route_navigation'):
            with self.subTest(phase=phase):await self.probe(phase)

    async def test_stop_can_interrupt_every_preflight_stage(self):
        for phase in ('visible_control','relation_count','source_metadata','surface_probe','trigger_probe','trigger_click','route_navigation'):
            with self.subTest(phase=phase):await self.probe(phase,stop=True)
