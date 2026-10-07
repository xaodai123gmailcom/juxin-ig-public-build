"""R93 regressions: stable tabs, real rejection totals and target resume."""
import asyncio
import json
import unittest

from app.playwright_worker import WorkerExecutionError
from app.account_profile_stats import save_account_snapshot
from test_parallel_screening_worker import _Page, _connected_parent
import test_hover_precheck_r61 as hover_cases
import test_task_control_r27 as control_cases
import test_account_profile_stats as stats_cases
import test_parallel_relation_pipeline as pipeline_cases
from test_parallel_relation_pipeline import (
    _PipelineManager, _NoopBitBrowser, _RelationParent, _PipelineState, _ScreeningChild,
)
from app.playwright_worker import CollectionOutcome, PlaywrightWorker
from types import SimpleNamespace
from unittest.mock import AsyncMock


class FixedSlots(unittest.IsolatedAsyncioTestCase):
    async def test_three_slots_reuse_replace_closed_only_and_close_on_parent_exit(self):
        pages=[]
        class Context:
            async def new_page(self):
                page=_Page(str(len(pages)+1));pages.append(page);return page
        parent=_connected_parent(Context(),_Page('source'))
        children=[await parent.create_parallel_screening_worker() for _ in range(3)]
        self.assertEqual([1,2,3],[c._task_page_slot for c in children])
        with self.assertRaises(WorkerExecutionError):await parent.create_parallel_screening_worker()
        parent.release_parallel_screening_worker(children[1])
        self.assertIs(children[1],await parent.create_parallel_screening_worker())
        self.assertEqual([0,0,0],[p.close_calls for p in pages])
        # A manually closed child releases its slot even while it was reserved.
        await children[1].disconnect()
        replacement=await parent.create_parallel_screening_worker()
        self.assertEqual(2,replacement._task_page_slot)
        self.assertIs(parent._screening_worker_pool[1],children[0])
        self.assertIs(parent._screening_worker_pool[3],children[2])
        await parent.disconnect()
        self.assertEqual([1,1,1,1],[p.close_calls for p in pages])


class HoverTotals(hover_cases.HoverPrecheckTests):
    async def test_hover_exclusion_counter_is_durable_and_idempotent(self):
        names=['large.account']
        previews={names[0]:{'username':names[0],'followers':9000,'following':20,'posts':1,'visibility':'unknown','evidence':'relationship_hover_card'}}
        for _ in range(2):
            await self.manager._record_hover_exclusions(self.control,self.target,'followers',self.task['settings'],names,previews)
            self.service.append_task_mode_candidates(self.owner,self.task['id'],self.target,'followers',names)
        row=self.service.get_task(self.owner,self.task['id'])['targets'][0]['mode_progress']['followers']
        self.assertEqual(1,row['discarded'])
        self.assertEqual(1,row['hover_discarded'])
        self.assertEqual(1,len(self.service.list_results(self.owner,self.task['id'])))


class WindowResume(control_cases.TargetBoundWindowControlTests):
    async def test_continue_rearms_failed_target_while_own_window_loop_is_still_idle(self):
        ident=self.first['id']
        self.service.set_target_runtime_status(self.owner,self.task['id'],ident,'recoverable',window_id='window-one')
        self.control.enqueued_target_ids.discard(ident)
        self.control.profile_states['window-one'].update(state='idle',current_target_id=None)
        self.assertTrue(self.service.get_task(self.owner,self.task['id'])['targets'][0]['manual_recovery_required'])
        leases=self.lease_ownership()
        result=await self.manager.resume_window(self.owner,self.task['id'],'window-one',expected_target_id=ident)
        self.assertEqual('running',result['status'])
        target=self.service.get_task(self.owner,self.task['id'])['targets'][0]
        self.assertEqual('pending',target['status'])
        self.assertFalse(target['manual_recovery_required'])
        self.assertIn(ident,self.control.enqueued_target_ids)
        self.assertEqual(leases,self.lease_ownership())
        self.assertFalse(self.control.profile_stop_events['window-two'].is_set())


class NurtureTotals(stats_cases.AccountStatsTests):
    def test_metrics_and_archived_nurture_runs_are_owner_scoped(self):
        save_account_snapshot(self.db,self.owner,'w1',{'username':'me','posts_count':0,'followers_count':132,'following_count':24})
        async def run():
            for i in range(3):
                ident=(await self.start('nurture',['w1'],{'minutes':1},'r93-count-'+str(i)))['job_ids'][0]
                self.m.update(ident,status='failed',cursor=1 if i<2 else 0,result_json=json.dumps({'nurture_started_at':'2026-09-27T10:00:00Z'} if i<2 else {}))
                if i==0:await self.m.command(self.owner,{'action':'delete_failed_nurture','job_id':ident})
        asyncio.run(run())
        row=self.m.snapshot(self.owner)['window_stats'][0]
        self.assertEqual((0,132,24),(row['posts_count'],row['followers_count'],row['following_count']))
        self.assertEqual(2,row['nurture_count'])
        self.assertEqual('2026-09-27T10:00:00Z',row['last_nurture_at'])
        self.assertEqual([],self.m.snapshot(self.other)['window_stats'])


class BoundedPipeline(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline_cases.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline_cases.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline_cases.ParallelRelationPipelineTests._task_and_control

    def setup_pipeline(self, *, fail=False):
        task, target, control = self._task_and_control(2)
        state = _PipelineState('bounded')
        state.gates = {name: asyncio.Event() for name in state.candidates}
        state.started = {name: asyncio.Event() for name in state.candidates}
        state.admitted = []
        state.third_capacity_wait = asyncio.Event()
        state.fail = asyncio.Event()
        class Child(_ScreeningChild):
            async def screen(self, username):
                state.started[username].set()
                if fail:
                    await state.fail.wait()
                    raise RuntimeError('both child pages unavailable')
                await state.gates[username].wait()
                state.screened.append(('child', username))
        class Source(_RelationParent):
            async def create_parallel_screening_worker(self):
                child = Child(state)
                self.children.append(child)
                return child
            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                for index, name in enumerate(state.candidates):
                    if index == 2:
                        state.third_capacity_wait.set()
                    await self.hover_capacity_checkpoint()
                    await candidate_sink([name])
                    state.admitted.append(name)
                return CollectionOutcome('followers', [], source_total=3)
        parent = Source(state)
        parent.children = []
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        running = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, 'followers', task['settings'], None))
        return task, target, control, state, parent, running

    async def test_two_full_slots_wait_for_committed_result_before_third_admission(self):
        task, target, control, state, parent, running = self.setup_pipeline()
        try:
            await asyncio.wait_for(state.third_capacity_wait.wait(), 5)
            for name in state.candidates[:2]:
                await asyncio.wait_for(state.started[name].wait(), 5)
            self.assertEqual(state.candidates[:2], state.admitted)
            self.assertFalse(state.started[state.candidates[2]].is_set())
            state.gates[state.candidates[0]].set()
            await asyncio.wait_for(state.started[state.candidates[2]].wait(), 5)
            for gate in state.gates.values():
                gate.set()
            result = await asyncio.wait_for(running, 5)
            self.assertEqual((3, 0), (result['total'], result['pending']))
            self.assertEqual(set(state.candidates), {name for _, name in state.screened})
            self.assertTrue(all(kind == 'child' for kind, _ in state.screened))
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_all_failed_children_wake_capacity_wait_and_keep_durable_candidates(self):
        task, target, control, state, parent, running = self.setup_pipeline(fail=True)
        try:
            await asyncio.wait_for(state.third_capacity_wait.wait(), 5)
            state.fail.set()
            with self.assertRaisesRegex(RuntimeError, 'both child pages unavailable'):
                await asyncio.wait_for(running, 5)
            stats = self.service.task_mode_candidate_stats(
                self.user['id'], task['id'], target['id'], 'followers')
            self.assertEqual((2, 2), (stats['total'], stats['pending']))
            self.assertEqual(state.candidates[:2], state.admitted)
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_stop_interrupts_full_pool_without_accepting_another_candidate(self):
        task, target, control, state, parent, running = self.setup_pipeline()
        try:
            await asyncio.wait_for(state.third_capacity_wait.wait(), 5)
            control.stop_event.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
            self.assertEqual(state.candidates[:2], state.admitted)
            self.assertEqual([1, 1], [child.disconnect_calls for child in parent.children])
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)


class ImmediateHoverAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_skips_hover_and_first_confirmed_card_reaches_sink_before_next_hover(self):
        worker = PlaywrightWorker(object())
        worker._read_visible_account_hrefs = AsyncMock(return_value=['/duplicate/', '/first/', '/second/'])
        worker._collection_checkpoint = AsyncMock()
        events = []
        async def duplicate(name):
            return name == 'duplicate'
        async def capacity():
            events.append('capacity')
        async def hover(_dialog, name):
            events.append('hover:' + name)
            return {'username': name, 'posts': 9, 'followers': 34, 'following': 360,
                    'visibility': 'unknown', 'evidence': 'relationship_hover_card'} if name == 'first' else None
        async def sink(names, evidence):
            events.append('saved:' + ','.join(names))
            return len(events)
        worker.hover_duplicate_check = duplicate
        worker.hover_capacity_checkpoint = capacity
        worker._read_relation_hover_preview = hover
        with self.assertRaises(WorkerExecutionError):
            await worker._read_visible_account_dialog(SimpleNamespace(), None,
                surface_kind='followers', candidate_sink=sink, hover_precheck=True)
        self.assertEqual(['saved:duplicate', 'capacity', 'hover:first', 'saved:first',
                          'capacity', 'hover:second'], events)
