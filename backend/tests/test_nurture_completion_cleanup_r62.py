"""Completed standalone nurture retires only its confirmed, exact lease generation.

All checks use temporary SQLite and controlled adapters; no browser/network runs.
"""
import asyncio
import json
import threading
import unittest
from unittest.mock import AsyncMock, patch

import test_studio as fixtures
from app.browser_cleanup import close_acknowledged, close_profile_and_wait
from app.database import Database
from app.errors import ConflictError
from app.service import CoreService
from app.studio import StudioManager


class CloseAcknowledgementTests(unittest.TestCase):
    def test_only_legacy_void_or_explicit_closed_acknowledges_retirement(self):
        for result in (None, True, {'closed': True}, {'provider_success': True, 'window_state': 'closed'}):
            with self.subTest(result=result):
                self.assertTrue(close_acknowledged(result))
        for result in (False, {}, {'requested': True}, {'provider_success': True},
                       {'window_state': 'unknown'}, {'window_state': 'open'},
                       {'closed': True, 'window_state': 'opening'},
                       {'closed': True, 'provider_success': False}):
            with self.subTest(result=result):
                self.assertFalse(close_acknowledged(result))


class NurtureCompletionCleanupTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown
    execute = fixtures.StudioTests.execute

    async def create(self, profile='w1', key='nurture-cleanup-fixture'):
        return (await self.m.command(self.owner, {'action': 'start', 'kind': 'nurture',
            'profile_ids': [profile], 'request_id': key, 'config': {'minutes': 1}}))['job_ids'][0]

    def lease(self, profile='w1'):
        with self.db.read() as c:
            row = c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone()
        return dict(row) if row else None

    async def seed_pending(self, profile='w1'):
        ident = await self.create(profile)
        token = self.s.acquire_browser_lease(self.owner, profile, operation_type='studio', entity_id=ident, ttl_seconds=600)
        result = {'window_hold': True, 'counts': {'browse': 4, 'like': 2}, 'confirmed_at': '2026-10-02T12:00:00Z',
                  'nurture_outcome': 'completed', 'window_cleanup': {'state': 'pending', 'lease_token': token}}
        self.m.update(ident, status='completed', cursor=4, result_json=json.dumps(result))
        return ident, token

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)

    async def test_helper_loss_never_claims_closed_or_closes_replacement(self):
        ident, token = await self.seed_pending()
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET lease_token='replacement' WHERE profile_id='w1'")
        self.assertFalse(await close_profile_and_wait(self.s, self.browser, 'w1', token))
        self.assertEqual([], self.browser.closed)
        self.assertEqual('replacement', self.lease()['lease_token'])

    async def test_cleanup_exception_retains_exact_lease_and_durable_retry_receipt(self):
        ident = await self.create()
        with patch('app.studio.PlaywrightWorker', fixtures.Worker), \
             patch('app.studio.StudioBrowser.nurture_step', AsyncMock(return_value={'browse': 4, 'like': 2})), \
             patch('app.studio.close_profile_and_wait', AsyncMock(side_effect=RuntimeError('injected cleanup failure'))):
            await self.execute(ident)
        held = self.lease()
        self.assertIsNotNone(held, 'failed completion cleanup must never release its owned lease')
        row = self.m.get(self.owner, ident)
        result = json.loads(row['result_json'])
        self.assertEqual('completed', row['status'])
        self.assertTrue(result['window_hold'])
        self.assertEqual(held['lease_token'], result['window_cleanup']['lease_token'])
        self.assertEqual('pending', result['window_cleanup']['state'])
        self.assertNotIn('窗口已关闭并释放', row['message'])
        before = dict(result)
        with patch('app.studio.PlaywrightWorker') as worker, \
             patch('app.studio.StudioBrowser.nurture_step', AsyncMock()) as effect:
            await self.m.control(self.owner, ident, 'retry_cleanup')
            await self.until(lambda: self.lease() is None)
            await self.m.shutdown()
            worker.assert_not_called()
            effect.assert_not_awaited()
        saved = json.loads(self.m.get(self.owner, ident)['result_json'])
        self.assertEqual(before['confirmed_at'], saved['confirmed_at'])
        self.assertEqual(before['counts'], saved['counts'])
        self.assertEqual('closed', saved['window_cleanup']['state'])

    async def test_restart_preserves_exact_cleanup_generation_and_retries_without_effects(self):
        ident, token = await self.seed_pending()
        self.db.live_browser_lease_tokens.clear()
        fresh_db = Database(self.db.path)
        fresh_db.initialize()
        fresh_service = CoreService(fresh_db, session_hours=1)
        fresh_service.recover_interrupted_operations()
        newer = StudioManager(fresh_service, self.browser)
        newer.recover()
        self.assertIsNotNone(self.lease(), 'startup must retain pending completion cleanup')
        self.assertEqual(token, self.lease()['lease_token'])
        with patch('app.studio.PlaywrightWorker') as worker, \
             patch('app.studio.StudioBrowser.nurture_step', AsyncMock()) as effect:
            newer._schedule_ready()
            await self.until(lambda: self.lease() is None)
            await newer.shutdown()
            worker.assert_not_called()
            effect.assert_not_awaited()
        saved = json.loads(newer.get(self.owner, ident)['result_json'])
        self.assertEqual({'browse': 4, 'like': 2}, saved['counts'])
        self.assertEqual('2026-10-02T12:00:00Z', saved['confirmed_at'])
        self.assertFalse(saved['window_hold'])
        self.assertEqual('closed', saved['window_cleanup']['state'])

    async def test_expired_cleanup_lease_cannot_be_scavenged_before_manager_recovery(self):
        ident, token = await self.seed_pending()
        self.db.live_browser_lease_tokens.clear()
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01',heartbeat_at='2000-01-01'")
        with self.assertRaises(ConflictError):
            self.s.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='competitor')
        self.assertEqual(token, self.lease()['lease_token'])

    async def test_missing_cleanup_lease_remains_fenced_and_does_not_close_a_new_window(self):
        ident, token = await self.seed_pending()
        with self.db.write() as c:
            c.execute('DELETE FROM browser_operation_leases')
        newer = StudioManager(self.s, self.browser)
        newer.recover()
        with self.assertRaises(ConflictError):
            self.s.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='competitor')
        newer._schedule_ready()
        await asyncio.sleep(.04)
        await newer.shutdown()
        self.assertEqual([], self.browser.closed)
        saved = json.loads(newer.get(self.owner, ident)['result_json'])
        self.assertEqual(token, saved['window_cleanup']['lease_token'])
        self.assertTrue(saved['window_hold'])
        self.assertNotIn('窗口已关闭并释放', newer.get(self.owner, ident)['message'])

    async def test_archived_missing_cleanup_fence_blocks_batch_admission_atomically(self):
        ident, token = await self.seed_pending()
        with self.db.write() as c:
            c.execute('DELETE FROM browser_operation_leases')
            c.execute("UPDATE studio_jobs SET deleted_at='2026-10-03' WHERE id=?", (ident,))
        with self.assertRaises(ConflictError):
            await self.m.command(self.owner, {'action': 'start', 'kind': 'nurture',
                'profile_ids': ['free', 'w1'], 'request_id': 'no-partial-batch', 'config': {'minutes': 1}})
        with self.db.read() as c:
            self.assertEqual(1, c.execute('SELECT count(*) FROM studio_jobs').fetchone()[0])
        with self.assertRaises(ConflictError):
            self.s.acquire_browser_lease(self.other, 'w1', operation_type='account', entity_id='competitor')

    async def test_restart_never_removes_replacement_generation_on_held_profile(self):
        ident, token = await self.seed_pending()
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET lease_token='replacement',entity_id='other-job'")
        fresh_db = Database(self.db.path)
        fresh_db.initialize()
        fresh_service = CoreService(fresh_db, session_hours=1)
        fresh_service.recover_interrupted_operations()
        newer = StudioManager(fresh_service, self.browser)
        newer.recover()
        self.assertIsNotNone(self.lease())
        self.assertEqual('replacement', self.lease()['lease_token'])
        newer._schedule_ready()
        await asyncio.sleep(.03)
        await newer.shutdown()
        self.assertEqual([], self.browser.closed)
        saved = json.loads(newer.get(self.owner, ident)['result_json'])
        self.assertEqual('lease_lost', saved['window_cleanup']['state'])
        self.assertEqual(token, saved['window_cleanup']['lease_token'])

    async def test_legacy_completed_hold_adopts_only_existing_owned_generation(self):
        ident, token = await self.seed_pending()
        with self.db.write() as c:
            c.execute("UPDATE studio_jobs SET result_json=json_remove(result_json,'$.window_cleanup') WHERE id=?", (ident,))
        self.s.recover_interrupted_operations()
        newer = StudioManager(self.s, self.browser)
        newer.recover()
        saved = json.loads(newer.get(self.owner, ident)['result_json'])
        self.assertEqual({'state': 'pending', 'lease_token': token}, saved['window_cleanup'])
        newer._schedule_ready()
        await self.until(lambda: self.lease() is None)
        await newer.shutdown()
        self.assertEqual(['w1'], self.browser.closed)

    async def test_uncertain_provider_result_retains_lease_then_confirmed_retry_releases_once(self):
        ident = await self.create()
        outcome = [RuntimeError('temporary close error')]
        calls = []
        def close(profile):
            calls.append(profile)
            if isinstance(outcome[0], Exception):raise outcome[0]
            return outcome[0]
        self.browser.close_profile = close
        effect = AsyncMock(return_value={'browse': 4, 'like': 2})
        with patch('app.studio.PlaywrightWorker', fixtures.Worker), \
             patch('app.studio.StudioBrowser.nurture_step', effect):
            task = asyncio.create_task(self.execute(ident))
            try:
                await self.until(lambda: bool(calls))
                token = self.lease()['lease_token']
                count = effect.await_count
                for response in ({'window_state': 'unknown'}, {'window_state': 'open'}, False):
                    before = len(calls)
                    outcome[0] = response
                    await self.until(lambda: len(calls) > before)
                    self.assertEqual(token, self.lease()['lease_token'])
                    self.assertFalse(task.done())
                    self.assertEqual(count, effect.await_count, 'retirement must not replay activity')
                    with self.assertRaises(ConflictError):
                        self.s.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='competitor')
            finally:
                outcome[0] = {'closed': True}
                await asyncio.wait_for(task, 5)
        self.assertIsNone(self.lease())
        saved = json.loads(self.m.get(self.owner, ident)['result_json'])
        self.assertEqual('closed', saved['window_cleanup']['state'])
        self.assertEqual({'browse': 4, 'like': 2}, saved['counts'])

    async def test_repeated_cancellation_waits_for_close_before_releasing_exact_token(self):
        ident = await self.create()
        entered, release = threading.Event(), threading.Event()
        def close(profile):
            entered.set()
            if not release.wait(5):raise RuntimeError('test close watchdog')
            return {'closed': True}
        self.browser.close_profile = close
        with patch('app.studio.PlaywrightWorker', fixtures.Worker), \
             patch('app.studio.StudioBrowser.nurture_step', AsyncMock(return_value={'browse': 1})):
            task = asyncio.create_task(self.execute(ident))
            try:
                await self.until(entered.is_set)
                token = self.lease()['lease_token']
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                    self.assertEqual(token, self.lease()['lease_token'])
                    self.assertTrue(json.loads(self.m.get(self.owner, ident)['result_json'])['window_hold'])
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        self.assertIsNone(self.lease())
        self.assertEqual('closed', json.loads(self.m.get(self.owner, ident)['result_json'])['window_cleanup']['state'])

    async def test_replaced_token_after_provider_ack_is_never_released_or_claimed_closed(self):
        ident, token = await self.seed_pending()
        def close(profile):
            with self.db.write() as c:
                c.execute("UPDATE browser_operation_leases SET lease_token='replacement' WHERE profile_id=?", (profile,))
            return {'closed': True}
        self.browser.close_profile = close
        await self.m._finish_nurture_cleanup(self.owner, ident, 'w1', token)
        self.assertEqual('replacement', self.lease()['lease_token'])
        saved = json.loads(self.m.get(self.owner, ident)['result_json'])
        self.assertEqual('lease_lost', saved['window_cleanup']['state'])
        self.assertTrue(saved['window_hold'])
        self.assertNotIn('窗口已关闭并释放', self.m.get(self.owner, ident)['message'])

    async def test_cleanup_requires_whole_owned_lease_and_preserves_replacement(self):
        ident, token = await self.seed_pending()
        for column, value in (('owner_user_id', self.other), ('operation_type', 'account'), ('entity_id', 'other-job')):
            with self.subTest(column=column):
                with self.db.write() as c:
                    c.execute('UPDATE browser_operation_leases SET '+column+'=?', (value,))
                await self.m._finish_nurture_cleanup(self.owner, ident, 'w1', token)
                self.assertEqual([], self.browser.closed)
                self.assertEqual(token, self.lease()['lease_token'])
                self.assertEqual(value, self.lease()[column])
                with self.db.write() as c:
                    c.execute("UPDATE browser_operation_leases SET owner_user_id=?,operation_type='studio',entity_id=?", (self.owner, ident))

    async def test_cleanup_scheduler_fences_same_profile_but_does_not_block_free_profile(self):
        ident, token = await self.seed_pending()
        free = await self.create('free', 'nurture-free-fixture')
        source = self.m.get(self.owner, free)
        with self.db.write() as c:
            c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ('next-round', self.owner, 'next-round', 'nurture', 'w1', source['config_json'], source['total_steps'], source['due_at'], source['created_at'], source['updated_at']))
        allow = [False]
        calls = []
        def close(profile):
            calls.append(profile)
            return {'closed': profile != 'w1' or allow[0]}
        self.browser.close_profile = close
        with patch('app.studio.PlaywrightWorker', fixtures.Worker), \
             patch('app.studio.StudioBrowser.nurture_step', AsyncMock(return_value={'browse': 1})):
            try:
                self.m._schedule_ready()
                await self.until(lambda: self.m.get(self.owner, free)['status'] == 'completed')
                self.assertEqual(token, self.lease()['lease_token'])
                self.assertEqual('waiting_window', self.m.get(self.owner, 'next-round')['status'])
                self.assertNotIn('next-round', self.m.active_ids())
                self.assertEqual('completed', self.m.get(self.owner, ident)['status'])
            finally:
                allow[0] = True
                await self.m.shutdown()
