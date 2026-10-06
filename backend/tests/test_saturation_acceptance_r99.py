"""Offline cap-saturation acceptance: optional signals vs durable required work.

The Chromium cases intercept all requests. Cache pressure and superseded passive
responses must still allow exact inline/visible evidence, or remain unknown.
"""
import asyncio
import gc
import json
import os
import time
import tracemalloc
import unittest
from unittest.mock import AsyncMock, patch

from app.playwright_worker import PlaywrightWorker, VisibleProfile, WorkerExecutionError, _PROFILE_REUSABLE_IDENTITIES
from test_profile_capture_performance_r98 import Response, Page
from test_zero_profile_reader_r45 import launch_fixture_browser, profile_html
import test_single_handoff_r94 as _handoff


def fill_metadata(worker, username):
    values = (True, '12345', False, True, 'Creator', 'https://example.invalid/',
              37, ('2026-10-01T00:00:00+00:00',), ('https://www.instagram.com/p/synthetic/',),
              VisibleProfile(username, 'private', 11, 22, 37))
    for name,value in zip(worker._PROFILE_CACHE_NAMES,values):
        getattr(worker,name)[username]=value
    worker._bound_profile_caches(username)


class MetadataSaturationAcceptance(unittest.TestCase):
    def test_all_ten_caches_stay_bounded_through_twenty_thousand_successes_and_cleanup(self):
        worker=PlaywrightWorker(None)
        tracemalloc.start()
        try:
            for i in range(_PROFILE_REUSABLE_IDENTITIES):fill_metadata(worker,f'person{i}')
            gc.collect();baseline=tracemalloc.get_traced_memory()[0]
            for i in range(_PROFILE_REUSABLE_IDENTITIES,20_000):
                name=f'person{i}';fill_metadata(worker,name)
                worker._remember_profile(name,VisibleProfile(name,'private',11,22,37,avatar_image_bytes=b'x'*8192))
            gc.collect();current,peak=tracemalloc.get_traced_memory()
            for name in worker._PROFILE_CACHE_NAMES:
                self.assertEqual(_PROFILE_REUSABLE_IDENTITIES,len(getattr(worker,name)),name)
                self.assertNotIn('person0',getattr(worker,name),name)
                self.assertIn('person19999',getattr(worker,name),name)
            self.assertLess(current-baseline,2_000_000,(baseline,current,peak))
            self.assertTrue(all(p.avatar_image_bytes is None for p in worker._profile_base_cache.values()))
            worker.invalidate_after_manual_control()
            self.assertTrue(all(not getattr(worker,name) for name in worker._PROFILE_CACHE_NAMES))
        finally:tracemalloc.stop()


class ResponseHandlerTap:
    """Inject synthetic replies through callbacks registered on real Page.on.

    Playwright's public async Page has no emit method. Native registration and
    removal still occur, so this fixture preserves the real event lifecycle.
    """
    def __init__(self,page):
        self.native_on=page.on
        self.native_remove=page.remove_listener
        self.handlers=[]

    def on(self,event,handler):
        result=self.native_on(event,handler)
        if event=='response':self.handlers.append(handler)
        return result

    def remove_listener(self,event,handler):
        result=self.native_remove(event,handler)
        if event=='response' and handler in self.handlers:self.handlers.remove(handler)
        return result

    def dispatch(self,response):
        for handler in tuple(self.handlers):handler(response)


class ResponseHandlerTapContract(unittest.TestCase):
    def test_page_without_emit_uses_real_registration_and_removal(self):
        class PageWithoutEmit:
            def __init__(self):self.listeners=[]
            def on(self,event,handler):self.listeners.append((event,handler))
            def remove_listener(self,event,handler):self.listeners.remove((event,handler))
        page=PageWithoutEmit();self.assertFalse(hasattr(page,'emit'))
        tap=ResponseHandlerTap(page);seen=[];callback=seen.append
        tap.on('response',callback)
        self.assertEqual([('response',callback)],page.listeners)
        payload=object();tap.dispatch(payload)
        self.assertEqual([payload],seen)
        tap.remove_listener('response',callback)
        tap.dispatch(object())
        self.assertEqual([payload],seen)
        self.assertEqual([],page.listeners)
        self.assertEqual([],tap.handlers)


@unittest.skipUnless(os.environ.get("IGAC_REQUIRE_SATURATION_BROWSER") == "1", "Opt-in Chromium saturation checks; set IGAC_REQUIRE_SATURATION_BROWSER=1")
class BrowserFallbackSaturationAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.context=await launch_fixture_browser(self)

    async def saturated(self,html):
        await self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
        worker=PlaywrightWorker(None);worker.page=await self.context.new_page();worker._context=self.context
        response_tap=ResponseHandlerTap(worker.page)
        for attribute,handler in [('on',response_tap.on),('remove_listener',response_tap.remove_listener)]:
            patcher=patch.object(worker.page,attribute,handler)
            patcher.start();self.addCleanup(patcher.stop)
        # All metadata caches start full; this target was evicted in each one.
        fill_metadata(worker,'target')
        for i in range(_PROFILE_REUSABLE_IDENTITIES):fill_metadata(worker,f'old{i}')
        self.assertTrue(all('target' not in getattr(worker,n) for n in worker._PROFILE_CACHE_NAMES))
        gate=asyncio.Event();self.addAsyncCleanup(self.drain,worker,gate)
        state={'active':0,'peak':0}
        blockers=[Response(username='other',release=gate,state=state) for _ in range(4)]
        discarded=Response(username='target',private=False)
        tail=[Response(username='other') for _ in range(40)]
        navigate=worker._navigate_profile
        async def navigate_and_burst(target):
            result=await navigate(target)
            for response in [*blockers,discarded,*tail]:response_tap.dispatch(response)
            return result
        worker._navigate_profile=navigate_and_burst
        return worker,gate,state,blockers,discarded,tail

    async def drain(self,worker,gate):
        gate.set()
        await asyncio.gather(*tuple(worker._late_lifecycle_tasks),return_exceptions=True)
        self.assertFalse(worker._profile_capture_tasks)
        self.assertFalse(worker._late_lifecycle_tasks)

    def assert_pressure(self,worker,state,blockers,discarded,tail):
        self.assertEqual(4,state['peak'])
        self.assertEqual(4,len(worker._profile_capture_tasks))
        self.assertEqual(4,sum(r.reads for r in blockers))
        self.assertEqual(0,discarded.reads,'superseded optional descriptor must really have been evicted')
        self.assertEqual(0,sum(r.reads for r in tail),'blocked readers still own all four slots')
        self.assertTrue(all(len(getattr(worker,n))<=_PROFILE_REUSABLE_IDENTITIES for n in worker._PROFILE_CACHE_NAMES))

    async def test_full_caches_and_capture_queue_still_use_exact_inline_evidence(self):
        html=profile_html('target',private=True,posts=37).replace('<span>37帖子</span>','')
        html += '<script type="application/json">'+json.dumps({'data':{'user':{'username':'target','is_private':True,'id':'98765','media_count':37}}})+'</script>'
        worker,gate,state,blockers,discarded,tail=await self.saturated(html)
        with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',.01):
            result=await worker._read_visible_profile_once('target')
        self.assertEqual(('private',37,'98765'),(result.visibility,result.posts,result.instagram_user_id))
        self.assert_pressure(worker,state,blockers,discarded,tail)
        gate.set();await asyncio.gather(*tuple(worker._late_lifecycle_tasks),return_exceptions=True)
        self.assertIs(True,worker._profile_privacy_cache['target'],'late body cannot replace exact inline evidence')

    async def test_full_caches_and_capture_queue_still_use_exact_visible_counts(self):
        worker,gate,state,blockers,discarded,tail=await self.saturated(profile_html('target',private=True,posts=9))
        with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',.01):
            result=await worker._read_visible_profile_once('target')
        self.assertEqual(('private',9,240,680),(result.visibility,result.posts,result.followers,result.following))
        self.assert_pressure(worker,state,blockers,discarded,tail)

    async def test_no_passive_inline_or_visible_evidence_never_invents_zero_or_success(self):
        worker,gate,state,blockers,discarded,tail=await self.saturated(profile_html('target',unknown=True,loader=False))
        with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',.01),patch('app.playwright_worker._PROFILE_DATA_SETTLE_SECONDS',0):
            try:result=await worker._read_visible_profile_once('target')
            except WorkerExecutionError as error:
                self.assertIn(error.code,{'instagram_profile_not_ready','instagram_profile_dom_unrecognized'})
                self.assertNotIn('target',worker._profile_base_cache)
            else:
                self.assertIsNone(result.posts)
                self.assertEqual('unknown',result.visibility)
        self.assertNotEqual(0,worker._profile_posts_count_cache.get('target'))
        self.assert_pressure(worker,state,blockers,discarded,tail)


class TransportFallbackSaturationAcceptance(unittest.IsolatedAsyncioTestCase):
    """Real parser/profile assembly with deterministic read-only DOM transport.

    This isolates unavailable Chromium from capture/parser logic; it does not
    replace the opt-in real-browser DOM checks above.
    """
    async def fixture(self, *, inline=False, visible=False):
        worker=PlaywrightWorker(None)
        header_text='target 108 followers 679 following'+(' 9 posts' if visible else '')
        body_text=header_text+(' This account is private' if visible else '')
        payload=json.dumps({'data':{'user':{'username':'target','is_private':True,'id':'98765','media_count':37}}}) if inline else ''
        class Locator:
            def __init__(self,text='',exists=True):self.text=text;self.exists=exists;self.first=self
            async def count(self):return int(self.exists)
            async def is_visible(self):return self.exists
            async def inner_text(self,**kwargs):return self.text
            async def evaluate_all(self,*args):return [payload] if payload else []
            async def wait_for(self,**kwargs):raise TimeoutError('synthetic missing locator')
            def locator(self,*args):return Locator(exists=False)
        class Transport(Page):
            def locator(self,selector):return Locator(body_text if selector=='body' else '')
        worker.page=Transport()
        for i in range(_PROFILE_REUSABLE_IDENTITIES):fill_metadata(worker,f'old{i}')
        worker._visible_profile_header=AsyncMock(return_value=Locator(header_text))
        worker._visible_external_bio_url=AsyncMock(return_value=None)
        worker._profile_metadata_snapshot=AsyncMock(return_value=('',None))
        worker._visible_profile_stats_text=AsyncMock(return_value='')
        worker._visible_profile_relation_counts=AsyncMock(return_value=(None,None))
        worker._first_visible=AsyncMock(return_value=None)
        worker._has_visible_private_indicator=AsyncMock(return_value=visible)
        worker._has_stable_private_profile_structure=AsyncMock(return_value=False)
        # This fixture supplies a stable target shell. Real DOM shell readiness
        # and rendered selector behavior are exercised by Chromium-only cases.
        worker._profile_surface_is_transient=AsyncMock(return_value=None)
        gate=asyncio.Event();state={'active':0,'peak':0}
        blockers=[Response(username='other',release=gate,state=state) for _ in range(4)]
        discarded=Response(username='target',private=False)
        responses=[*blockers,discarded,*[Response(username='other') for _ in range(40)]]
        async def navigate(target):
            for r in responses:worker.page.emit(r)
            return target
        worker._navigate_profile=navigate
        async def cleanup():
            gate.set();await asyncio.gather(*tuple(worker._late_lifecycle_tasks),return_exceptions=True)
            self.assertFalse(worker._profile_capture_tasks)
            self.assertFalse(worker._late_lifecycle_tasks)
        self.addAsyncCleanup(cleanup)
        return worker,state,responses,discarded

    async def outcome(self,**kwargs):
        worker,state,responses,discarded=await self.fixture(**kwargs)
        with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',.01):
            result=await worker._read_visible_profile_once('target')
        self.assertEqual(4,state['peak'])
        self.assertEqual(4,len(worker._profile_capture_tasks))
        self.assertEqual(4,sum(r.reads for r in responses))
        self.assertEqual(0,discarded.reads)
        self.assertFalse(worker.page.listeners)
        self.assertTrue(all(len(getattr(worker,n))<=_PROFILE_REUSABLE_IDENTITIES for n in worker._PROFILE_CACHE_NAMES))
        return result

    async def test_exact_inline_parser_supplies_evicted_passive_signal(self):
        result=await self.outcome(inline=True)
        self.assertEqual(('private',37,'98765'),(result.visibility,result.posts,result.instagram_user_id))

    async def test_exact_visible_text_supplies_evicted_passive_signal(self):
        result=await self.outcome(visible=True)
        self.assertEqual(('private',9,108,679),(result.visibility,result.posts,result.followers,result.following))

    async def test_unavailable_fallback_stays_unknown_with_no_false_zero(self):
        result=await self.outcome()
        self.assertEqual('unknown',result.visibility)
        self.assertIsNone(result.posts)
        self.assertIsNone(result.instagram_user_id)


class DurableQueueSaturationAcceptance(unittest.IsolatedAsyncioTestCase):
    # Reuse only fixture methods, not the fixture test class's test suite.
    asyncSetUp= _handoff.SingleHandoffR94Tests.asyncSetUp
    asyncTearDown= _handoff.SingleHandoffR94Tests.asyncTearDown
    _task_and_control= _handoff.SingleHandoffR94Tests._task_and_control
    pipeline= _handoff.SingleHandoffR94Tests.pipeline
    until= _handoff.SingleHandoffR94Tests.until
    stats= _handoff.SingleHandoffR94Tests.stats
    finish= _handoff.SingleHandoffR94Tests.finish
    cleanup= _handoff.SingleHandoffR94Tests.cleanup

    async def durable_boundary(self, predicate, job, state):
        # This 640-account fixture performs hundreds of real disk commits.
        # Reuse the already-tested bounded durable-progress watchdog, rather
        # than the ten-second small-fixture wall clock. No count or ownership
        # assertion is weakened; stalled progress and total duration still fail.
        manager, control = state.completion_context
        async def boundary():
            while not predicate():
                if job.done():
                    job.result()
                    self.fail('pipeline finished before expected durable boundary')
                await asyncio.sleep(.005)
        started = time.monotonic()
        await _handoff.wait_for_collection_operation(
            boundary, manager, self.service, control.owner_user_id, control.task_id,
            idle_timeout=30, overall_timeout=120,
        )
        print('SATURATION_DURABLE_BOUNDARY_SECONDS=' + str(round(time.monotonic()-started,3)), flush=True)

    async def test_production_batch_beyond_caches_remains_durable_and_resumes_after_stop(self):
        names=[f'required_work_{i}' for i in range(640)]
        task,target,control,state,source,job=self.pipeline(3,names=names,batch=True)
        # The production transport submits at most100 names per callback. Keep
        # that contract while the durable total crosses the reusable metadata count bound.
        async def collect_bounded(_target, *, candidate_sink, **kwargs):
            from app.playwright_worker import CollectionOutcome
            state.source_calls+=1
            state.source_running=True
            try:
                for offset in range(0,len(names),100):
                    batch=names[offset:offset+100]
                    await candidate_sink(batch)
                    state.delivered.extend(batch)
                return CollectionOutcome('followers',[],source_total=len(names))
            finally:state.source_running=False
        source.collect_followers=collect_bounded
        try:
            await self.durable_boundary(lambda:len(state.delivered)==len(names),job,state)
            await self.durable_boundary(lambda:bool((self.service.get_checkpoint(self.user['id'],task['id'],target['id'],'followers') or {}).get('cursor',{}).get('candidate_spool_natural_end')),job,state)
            self.assertEqual(640,self.stats(task,target)['pending'])
            self.assertEqual(3,len(state.children))
            self.assertFalse(job.done(),'natural list end does not mean required profile work is complete')
            control.stop_event.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(job,15)
            self.assertEqual(640,self.stats(task,target)['pending'])
            from app.database import Database
            from app.service import CoreService
            self.service=CoreService(Database(self.service.database.path))
            checkpoint=self.service.get_checkpoint(self.user['id'],task['id'],target['id'],'followers')
            for username in names:
                self.assertTrue(self.service.check_global_dedupe(username)['seen'])
            control.stop_event.clear()
            _,_,_,resumed,_,retry=self.pipeline(3,names=names,batch=True,fixture=(task,target,control),checkpoint=checkpoint)
            try:
                result=await self.finish(resumed,retry)
                self.assertEqual(0,resumed.source_calls)
                self.assertEqual((640,640,0),(result['total'],result['recorded'],result['pending']))
                self.assertEqual(set(names),{name for _,name in resumed.screened})
                self.assertEqual(640,len(resumed.screened))
            finally:await self.cleanup(resumed,retry)
        finally:await self.cleanup(state,job)

    async def test_legacy_handoff_capacity_blocks_then_resumes_without_dropping(self):
        task,target,control,state,source,job=self.pipeline(3,names=[f'legacy_work_{i}' for i in range(20)],batch=False)
        try:
            await self.until(lambda:self.stats(task,target)['total']==4,job)
            await asyncio.sleep(.1)
            self.assertEqual((4,4),(self.stats(task,target)['total'],self.stats(task,target)['pending']))
            self.assertFalse(job.done())
            state.gates[state.candidates[0]].set()
            await self.until(lambda:self.stats(task,target)['total']==5,job)
            self.assertEqual(4,self.stats(task,target)['pending'])
            result=await self.finish(state,job)
            self.assertEqual((20,20,0),(result['total'],result['recorded'],result['pending']))
            self.assertEqual(20,len({name for _,name in state.screened}))
        finally:await self.cleanup(state,job)

if __name__=='__main__':unittest.main()
