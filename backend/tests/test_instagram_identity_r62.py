"""Read-only identity resolver safety and readiness; no browser/network required."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch
from app.errors import ValidationError, ConflictError
from app.instagram_identity import OWN_LINK, OWN_METRICS, resolve_own_identity, wait_for_own_profile
from app.standalone_nurture import exact_count


class IdentityResolverR62Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.names=['', '', 'real_owner'];self.raw={};self.uid='123'
        self.cookies=AsyncMock(side_effect=lambda *a:[{'name':'ds_user_id','value':self.uid}])
        async def evaluate(script,*args):
            if script==OWN_LINK:
                return self.names.pop(0) if len(self.names)>1 else self.names[0]
            if script==OWN_METRICS:return self.raw
            raise AssertionError('unexpected DOM read')
        self.page=SimpleNamespace(context=SimpleNamespace(cookies=self.cookies),evaluate=AsyncMock(side_effect=evaluate))
        self.checkpoint=AsyncMock()

    async def wait_for_partial_profile(self):
        # Advance only this module's metrics clock. Real 1 ms sleeps racing a
        # 60 ms timeout can expire before the final poll on Windows/debug loops.
        # Keep the production 10 s identity / 3 s metrics budgets unchanged.
        self.clock=0.0
        async def advance(seconds):
            self.clock+=seconds
            await asyncio.sleep(0)
        resolver_asyncio=SimpleNamespace(**vars(asyncio))
        resolver_asyncio.get_running_loop=lambda:SimpleNamespace(time=lambda:self.clock)
        resolver_asyncio.sleep=advance
        # Neither the event loop nor asyncio.timeout uses the logical clock.
        # This real outer watchdog still fails a stuck fixture or broken loop.
        async with asyncio.timeout(5):
            with patch('app.instagram_identity.asyncio',resolver_asyncio):
                return await wait_for_own_profile(
                    self.page,'real_owner','123',self.checkpoint,
                    metrics_ready=lambda raw:False)

    async def test_delayed_identity_checks_cookie_and_checkpoint_each_time(self):
        identity=await resolve_own_identity(self.page,self.checkpoint,timeout=.1,poll=.001)
        self.assertEqual({'username':'real_owner','instagram_user_id':'123'},identity)
        self.assertEqual(3,self.checkpoint.await_count)
        self.assertGreaterEqual(self.cookies.await_count,4)

    async def test_observed_wrong_identity_is_never_waited_away(self):
        self.names=['wrong_owner','real_owner']
        with self.assertRaises(ValidationError) as caught:
            await resolve_own_identity(self.page,self.checkpoint,expected={'username':'real_owner'},timeout=.1,poll=.001)
        self.assertEqual('posting_account_mismatch',caught.exception.details['reason'])
        self.assertEqual(1,self.checkpoint.await_count)

    async def test_missing_identity_is_not_zero_or_expected_identity(self):
        self.names=['']
        with self.assertRaises(ValidationError) as caught:
            await resolve_own_identity(self.page,self.checkpoint,expected={'username':'real_owner'},timeout=.02,poll=.001)
        self.assertEqual('posting_identity_unverified',caught.exception.details['reason'])

    async def test_actor_swap_while_waiting_fails_before_return(self):
        async def checkpoint():self.uid='456'
        with self.assertRaises(ValidationError):
            await resolve_own_identity(self.page,checkpoint,timeout=.1,poll=.001)

    async def test_exact_zero_metrics_are_valid_own_profile(self):
        self.names=['real_owner'];self.raw={key:'0' for key in ('posts_count','followers_count','following_count')}
        ready=lambda raw:all(exact_count(raw.get(k)) is not None for k in self.raw)
        result=await wait_for_own_profile(self.page,'real_owner','123',self.checkpoint,timeout=.1,poll=.001,metrics_ready=ready)
        self.assertEqual([0,0,0],[exact_count(result[key]) for key in self.raw])
        self.assertEqual(1,self.checkpoint.await_count)

    async def test_unknown_metrics_preserved_as_partial_after_own_proof(self):
        self.names=['real_owner'];self.raw={'followers_count':'1.2K'}
        result=await self.wait_for_partial_profile()
        self.assertEqual({'followers_count':'1.2K'},result)
        self.assertIsNone(exact_count(result['followers_count']))
        self.assertNotIn('posts_count',result)
        self.assertNotIn('following_count',result)
        self.assertEqual(3.0,self.clock)
        self.assertEqual(13,self.checkpoint.await_count)
        self.assertEqual(26,self.cookies.await_count)
        self.assertEqual(
            [call(OWN_LINK),call(OWN_METRICS,'real_owner')]*13,
            self.page.evaluate.await_args_list)

    async def test_actor_swap_on_partial_metrics_final_poll_fails(self):
        self.names=['real_owner'];self.raw={'followers_count':'1.2K'}
        async def checkpoint():
            if self.clock>=3.0:self.uid='456'
        self.checkpoint.side_effect=checkpoint
        with self.assertRaises(ValidationError) as caught:
            await self.wait_for_partial_profile()
        self.assertEqual('posting_account_mismatch',caught.exception.details['reason'])
        self.assertEqual(13,self.checkpoint.await_count)

    async def test_actor_swap_during_final_partial_metrics_read_fails(self):
        self.names=['real_owner'];self.raw={'followers_count':'1.2K'}
        evaluate=self.page.evaluate.side_effect
        async def swap_after_read(script,*args):
            result=await evaluate(script,*args)
            if script==OWN_METRICS and self.clock>=3.0:self.uid='456'
            return result
        self.page.evaluate.side_effect=swap_after_read
        with self.assertRaises(ValidationError) as caught:
            await self.wait_for_partial_profile()
        self.assertEqual('posting_account_mismatch',caught.exception.details['reason'])
        self.assertEqual(26,self.cookies.await_count)

    async def test_wrong_identity_on_partial_metrics_final_poll_fails(self):
        self.names=['real_owner'];self.raw={'followers_count':'1.2K'}
        async def checkpoint():
            if self.clock>=3.0:self.names=['wrong_owner']
        self.checkpoint.side_effect=checkpoint
        with self.assertRaises(ValidationError) as caught:
            await self.wait_for_partial_profile()
        self.assertEqual('posting_account_mismatch',caught.exception.details['reason'])
        self.assertEqual(13,self.checkpoint.await_count)

    async def test_lease_loss_on_partial_metrics_final_poll_fails(self):
        self.names=['real_owner'];self.raw={'followers_count':'1.2K'}
        async def checkpoint():
            if self.clock>=3.0:raise ConflictError('fixture lease lost')
        self.checkpoint.side_effect=checkpoint
        with self.assertRaises(ConflictError):
            await self.wait_for_partial_profile()
        self.assertEqual(13,self.checkpoint.await_count)

    async def test_missing_own_header_cannot_be_promoted_by_sidebar(self):
        self.names=['real_owner'];self.raw=None
        with self.assertRaises(ValidationError):
            await wait_for_own_profile(self.page,'real_owner','123',self.checkpoint,timeout=.02,poll=.001)

    async def test_lease_loss_cannot_be_swallowed_as_readiness(self):
        self.checkpoint.side_effect=ConflictError('fixture lease lost')
        with self.assertRaises(ConflictError):
            await wait_for_own_profile(self.page,'real_owner','123',self.checkpoint,timeout=.1,poll=.001)
