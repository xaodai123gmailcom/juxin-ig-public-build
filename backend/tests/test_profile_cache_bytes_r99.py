"""Reusable-byte budgets never remove business results or active worker ownership."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.playwright_worker import (
    PlaywrightWorker, VisibleProfile, _PROFILE_REUSABLE_CACHE_BYTES,
    _PROFILE_REUSABLE_ENTRY_BYTES,
)
from test_profile_capture_performance_r98 import Page, Response


class ProfileCacheBytesR99Tests(unittest.IsolatedAsyncioTestCase):
    async def test_large_result_is_returnable_but_not_reused_or_truncated(self):
        worker=PlaywrightWorker(None)
        description='x'*(_PROFILE_REUSABLE_ENTRY_BYTES*2)
        result=VisibleProfile('large','private',1,2,3,visible_description=description,
            avatar_image_bytes=b'needed-by-current-recognition')
        worker._remember_profile('large',result)
        self.assertNotIn('large',worker._profile_base_cache)
        self.assertEqual(description,result.visible_description)
        self.assertEqual(b'needed-by-current-recognition',result.avatar_image_bytes)
        worker._navigate_profile_with_privacy=AsyncMock(return_value=('large',True))
        worker._read_current_profile_snapshot=AsyncMock(return_value=result)
        self.assertIs(result,await worker._read_visible_profile_once('large'))
        worker._navigate_profile_with_privacy.assert_awaited_once_with('large')

    async def test_byte_pressure_evicts_old_optional_metadata_before_count_cap(self):
        worker=PlaywrightWorker(None)
        page,context,browser,endpoint=object(),object(),object(),'retained-endpoint'
        worker.page=page;worker._context=context;worker._browser=browser;worker._connected_endpoint=endpoint
        for i in range(256):
            name=f'person{i}'
            worker._remember_profile(name,VisibleProfile(name,'private',1,2,3,
                visible_description=str(i)+('x'*40_000)))
            self.assertLessEqual(worker._profile_cache_estimated_bytes(),_PROFILE_REUSABLE_CACHE_BYTES)
        self.assertLess(len(worker._profile_base_cache),100)
        self.assertNotIn('person0',worker._profile_base_cache)
        self.assertIn('person255',worker._profile_base_cache)
        self.assertIs(page,worker.page);self.assertIs(context,worker._context)
        self.assertIs(browser,worker._browser);self.assertEqual(endpoint,worker._connected_endpoint)

    async def test_current_oversized_signal_is_consumed_before_next_identity_eviction(self):
        worker=PlaywrightWorker(None)
        required_url='https://example.invalid/'+('x'*100_000)
        worker._profile_external_bio_url_cache['current']=required_url
        worker._bound_profile_caches('current')
        self.assertEqual(required_url,worker._profile_external_bio_url_cache['current'])
        actual=VisibleProfile('current','private',1,2,3,external_bio_url=required_url)
        worker._remember_profile('current',actual)
        self.assertEqual(required_url,actual.external_bio_url)
        self.assertNotIn('current',worker._profile_base_cache)
        worker._bound_profile_caches('next')
        self.assertNotIn('current',worker._profile_external_bio_url_cache)
        self.assertEqual(required_url,actual.external_bio_url)

    async def test_caller_review_mutation_cannot_grow_an_already_cached_copy(self):
        worker=PlaywrightWorker(None)
        profile=VisibleProfile('small','private',1,2,3,
            recent_posts=[{'url':'https://example.invalid/post'}],review_cache={'preview':{'small':'ok'}})
        worker._remember_profile('small',profile)
        before=worker._profile_cache_estimated_bytes()
        profile.review_cache['preview']['pixels']='x'*2_000_000
        profile.recent_posts.append({'url':'x'*2_000_000})
        cached=worker._profile_base_cache['small']
        self.assertNotIn('pixels',cached.review_cache['preview'])
        self.assertEqual(1,len(cached.recent_posts))
        self.assertEqual(before,worker._profile_cache_estimated_bytes())
        self.assertEqual(2_000_000,len(profile.review_cache['preview']['pixels']))
        reused=await worker._read_visible_profile_once('small')
        reused.review_cache['preview']['pixels']='y'*2_000_000
        self.assertNotIn('pixels',cached.review_cache['preview'])
        self.assertEqual(before,worker._profile_cache_estimated_bytes())

    async def test_parent_and_three_children_have_independent_limits_and_owned_reads(self):
        await self.worker_cap_case(4)

    async def test_eight_workers_share_optional_slots_and_all_fallbacks_still_succeed(self):
        await self.worker_cap_case(8)

    async def worker_cap_case(self,count):
        workers=[PlaywrightWorker(None) for _ in range(count)]
        release=asyncio.Event();states=[];responses=[]
        for index,worker in enumerate(workers):
            worker.page=Page();worker._connected_endpoint=f'endpoint-{index}'
            state={'active':0,'peak':0};states.append(state)
            emitted=[Response(username='other',release=release,state=state) for _ in range(4)]
            emitted.extend(Response(username='other') for _ in range(100));responses.append(emitted)
            async def navigate(target,worker=worker,emitted=emitted):
                for response in emitted:worker.page.emit(response)
                return target
            worker._navigate_profile=navigate
            from app.playwright_worker import EmbeddedProfileEvidence
            worker._read_inline_profile_evidence=AsyncMock(return_value=EmbeddedProfileEvidence(is_private=True,posts_count=37))
        try:
            with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',0):
                results=await asyncio.gather(*(w._navigate_profile_with_privacy(f'person{i}') for i,w in enumerate(workers)))
            self.assertEqual([(f'person{i}',True) for i in range(count)],results)
            self.assertEqual([4 if i<4 else 0 for i in range(count)],[s['peak'] for s in states])
            self.assertEqual(min(count*4,16),sum(len(w._profile_capture_tasks) for w in workers))
            for i,worker in enumerate(workers):
                for j in range(100):worker._remember_profile(f'p{j}',VisibleProfile(f'p{j}','private',1,2,3,visible_description='x'*40_000))
                self.assertLessEqual(worker._profile_cache_estimated_bytes(),_PROFILE_REUSABLE_CACHE_BYTES)
                self.assertEqual(4 if i<4 else 0,len(worker._profile_capture_tasks))
                self.assertEqual(f'endpoint-{i}',worker._connected_endpoint)
        finally:
            release.set()
            await asyncio.gather(*(t for w in workers for t in tuple(w._late_lifecycle_tasks)),return_exceptions=True)
        self.assertTrue(all(not w._profile_capture_tasks and not w._late_lifecycle_tasks for w in workers))
        before=sum(r.reads for r in responses[-1])
        with patch('app.playwright_worker._PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS',0):
            self.assertEqual(('recovered',True),await workers[-1]._navigate_profile_with_privacy('recovered'))
        self.assertGreater(sum(r.reads for r in responses[-1]),before,'released shared slots must admit later optional work')
        self.assertEqual(f'endpoint-{count-1}',workers[-1]._connected_endpoint)

if __name__=='__main__':unittest.main()
