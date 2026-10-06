"""Screening writes stay durable without blocking unrelated asyncio work."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionControl, ExecutionManager
from app.service import CoreService
from support.concurrency_probe import WAIT_SECONDS


class ScreenPersistenceR33Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'screen.sqlite')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('screen-r33', 'correct horse battery staple')['id']
        self.task = self.service.create_task(self.owner, name='screening writes', modes=['followers'], targets=['source_one'], settings={'local_person_recognition': False, 'location_enabled': False})
        self.target = self.task['targets'][0]['id']
        pause = asyncio.Event()
        pause.set()
        self.control = ExecutionControl(owner_user_id=self.owner, task_id=self.task['id'], pause_event=pause, stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
        self.manager = ExecutionManager(self.service, SimpleNamespace())

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def _claim(self, username):
        self.service.append_task_mode_candidates(self.owner, self.task['id'], self.target, 'followers', [username])
        return self.service.claim_workbench_identity(self.owner, username=username, source='followers', source_target=self.target, allow_owned_resume=True)['claim_id']

    def _screen(self, username, *, claim=None, visibility='private', stable_id=None, exclude=False):
        async def read(name, **kwargs):
            result = {'username': name, 'visibility': visibility, 'followers': 100, 'following': 50, 'posts': 0 if exclude else 10, 'activity_days': 1, 'activity_status': 'available'}
            if stable_id is not None:
                result['instagram_user_id'] = stable_id
            return result
        settings = {'location_enabled': False, 'local_person_recognition': False, 'exclude_public_zero_posts': exclude}
        return self.manager._screen_and_record(self.control, SimpleNamespace(read_visible_profile=read), self.target, username, 'followers', settings, claim_id=claim)

    def _counts(self):
        with self.db.read() as connection:
            return {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('task_results', 'workbench_candidates', 'workbench_collection_exclusions')}

    async def test_all_screening_write_routes_leave_event_loop_responsive_under_real_sqlite_contention(self):
        for kind in ('private', 'public', 'stable_id', 'exclusion'):
            with self.subTest(kind=kind):
                username = 'screen_' + kind
                claim = self._claim(username)
                locked, heartbeat = threading.Event(), threading.Event()
                observations = []
                loop_thread = threading.get_ident()
                original_write = self.db.write
                @contextmanager
                def checked_write():
                    # A synchronous write on the event loop is the regression;
                    # detect it directly, without a subsecond speed assumption.
                    self.assertNotEqual(loop_thread, threading.get_ident(),
                                        'screening SQLite write ran on the event loop')
                    with original_write() as connection:
                        yield connection
                def writer():
                    with self.db.write():
                        locked.set()
                        # Timeout releases a regressed implementation's deadlock;
                        # the assertion checks ordering, not machine throughput.
                        observations.append(heartbeat.wait(WAIT_SECONDS))
                holder = threading.Thread(target=writer)
                holder.start()
                screen = None
                try:
                    self.assertTrue(await asyncio.to_thread(locked.wait, WAIT_SECONDS), 'SQLite contention did not start')
                    with patch.object(self.db, 'write', side_effect=checked_write):
                        screen = asyncio.create_task(self._screen(username, claim=claim, visibility='public' if kind in {'public', 'exclusion'} else 'private', stable_id='998877' if kind == 'stable_id' else None, exclude=kind == 'exclusion'))
                        await asyncio.sleep(.01)
                        heartbeat.set()
                        await asyncio.wait_for(screen, WAIT_SECONDS)
                    self.assertEqual([True], observations, f'{kind} blocked other collection heartbeats')
                finally:
                    heartbeat.set()
                    await asyncio.to_thread(holder.join, WAIT_SECONDS)
                    if screen is not None:
                        await asyncio.gather(screen, return_exceptions=True)
                    self.assertFalse(holder.is_alive())
        self.assertEqual({'task_results': 4, 'workbench_candidates': 3, 'workbench_collection_exclusions': 1}, self._counts())

    async def test_repeated_cancel_joins_result_and_review_before_cleanup_can_release_lease(self):
        username = 'cancel_during_review'
        claim = self._claim(username)
        token = self.service.acquire_browser_lease(self.owner, 'window-r33', operation_type='collection', entity_id=self.task['id'])
        self.control.leases['window-r33'] = token
        entered, release = threading.Event(), threading.Event()
        original = self.service.create_workbench_candidate
        def blocked_review(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture review stalled')
            return original(*args, **kwargs)
        with patch.object(self.service, 'create_workbench_candidate', side_effect=blocked_review):
            screen = asyncio.create_task(self._screen(username, claim=claim))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertEqual(1, self._counts()['task_results'])
                self.control.pause_event.clear()
                self.control.stop_event.set()
                screen.cancel()
                await asyncio.sleep(0)
                screen.cancel()
                await asyncio.sleep(0)
                self.assertFalse(screen.done(), 'worker cleanup ran before the owned review write finished')
                with self.assertRaises(ConflictError):
                    self.service.acquire_browser_lease(self.owner, 'window-r33', operation_type='studio', entity_id='must-wait')
            finally:
                release.set()
                await asyncio.gather(screen, return_exceptions=True)
                self.service.release_browser_lease('window-r33', token)
        self.assertTrue(screen.cancelled())
        self.assertEqual({'task_results': 1, 'workbench_candidates': 1, 'workbench_collection_exclusions': 0}, self._counts())
        stats = self.service.reconcile_task_mode_candidates(self.owner, self.task['id'], self.target, 'followers')
        self.assertEqual((0, 1), (stats['pending'], stats['recorded']))

    async def test_result_write_failure_stays_pending_and_owned_claim_can_resume(self):
        username = 'failed_result'
        claim = self._claim(username)
        with patch.object(self.service, 'record_result', side_effect=sqlite3.OperationalError('injected disk failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'injected disk failure'):
                await self._screen(username, claim=claim)
        self.assertEqual({'task_results': 0, 'workbench_candidates': 0, 'workbench_collection_exclusions': 0}, self._counts())
        stats = self.service.task_mode_candidate_stats(self.owner, self.task['id'], self.target, 'followers')
        self.assertEqual(1, stats['pending'])
        resumed = self.service.claim_workbench_identity(self.owner, username=username, source='followers', source_target=self.target, allow_owned_resume=True)
        self.assertTrue(resumed['resumed'])
        self.assertTrue(await self._screen(username, claim=claim))
        self.assertEqual(1, self._counts()['task_results'])

    async def test_review_write_failure_preserves_repairable_result_gap(self):
        username = 'failed_review'
        claim = self._claim(username)
        with patch.object(self.service, 'create_workbench_candidate', side_effect=sqlite3.OperationalError('injected review failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'injected review failure'):
                await self._screen(username, claim=claim)
        self.assertEqual({'task_results': 1, 'workbench_candidates': 0, 'workbench_collection_exclusions': 0}, self._counts())
        self.db.initialize()
        stats = self.service.reconcile_task_mode_candidates(self.owner, self.task['id'], self.target, 'followers')
        self.assertEqual((0, 1), (stats['pending'], stats['recorded']))
        self.assertEqual({'task_results': 1, 'workbench_candidates': 1, 'workbench_collection_exclusions': 0}, self._counts())

    async def test_pause_or_stop_during_final_reads_prevents_new_terminal_writes(self):
        for action in ('pause', 'stop'):
            for stage in ('identity', 'location', 'activity', 'preview'):
                with self.subTest(action=action, stage=stage):
                    username = f'{action}_{stage}'
                    claim = self._claim(username)
                    before = self._counts()
                    paused = asyncio.Event()
                    class ObservedPause(asyncio.Event):
                        async def wait(inner):
                            if not inner.is_set():
                                paused.set()
                            return await super().wait()
                    self.control.pause_event = ObservedPause()
                    self.control.pause_event.set()
                    self.control.stop_event.clear()
                    reached = []
                    def interrupt():
                        reached.append(stage)
                        if action == 'pause':
                            self.control.pause_event.clear()
                        else:
                            self.control.stop_event.set()
                    async def read(name, **kwargs):
                        if stage == 'activity' and kwargs.get('include_activity'):
                            interrupt()
                        return {'username': name, 'visibility': 'public', 'followers': 100,
                                'following': 50, 'posts': 0 if stage == 'identity' else 10,
                                'activity_days': 100, 'activity_status': 'identified',
                                **({'instagram_user_id': '1001' if action == 'pause' else '1002'}
                                   if stage == 'identity' else {})}
                    async def location(name):
                        interrupt()
                        return 'Canada'
                    async def preview(*args, **kwargs):
                        if stage == 'preview':
                            interrupt()
                    settings = {'location_enabled': stage == 'location',
                                'exclude_public_zero_posts': stage == 'identity',
                                'public_discard_active_days_max': 30 if stage == 'activity' else 0}
                    durable = self.manager._await_durable_thread_call
                    async def after_durable(function, *args, **kwargs):
                        result = await durable(function, *args, **kwargs)
                        if stage == 'identity' and function == self.service.confirm_workbench_identity:
                            interrupt()
                        return result
                    with patch.object(self.manager, '_capture_final_review_evidence', side_effect=preview), \
                         patch.object(self.manager, '_await_durable_thread_call', side_effect=after_durable):
                        job = asyncio.create_task(self.manager._screen_and_record(
                            self.control, SimpleNamespace(read_visible_profile=read,
                                read_visible_account_location=location), self.target,
                            username, 'followers', settings, claim_id=claim))
                        waiter = asyncio.create_task(paused.wait())
                        try:
                            if action == 'pause':
                                await asyncio.wait_for(asyncio.wait(
                                    {job, waiter}, return_when=asyncio.FIRST_COMPLETED), WAIT_SECONDS)
                                self.assertEqual([stage], reached)
                                self.assertFalse(job.done(), 'terminal write passed an accepted pause')
                                self.assertEqual(before, self._counts())
                                self.control.pause_event.set()
                                self.assertTrue(await asyncio.wait_for(job, WAIT_SECONDS))
                                after = self._counts()
                                self.assertEqual(before['task_results'] + 1, after['task_results'])
                                table = 'workbench_candidates' if stage == 'preview' else 'workbench_collection_exclusions'
                                self.assertEqual(before[table] + 1, after[table])
                            else:
                                with self.assertRaises(asyncio.CancelledError):
                                    await asyncio.wait_for(job, WAIT_SECONDS)
                                self.assertEqual([stage], reached)
                                self.assertEqual(before, self._counts())
                        finally:
                            self.control.pause_event.set()
                            job.cancel(); waiter.cancel()
                            await asyncio.gather(job, waiter, return_exceptions=True)

    async def test_cancelled_exclusion_joins_result_write_without_creating_review(self):
        username = 'cancel_exclusion'
        claim = self._claim(username)
        entered, release = threading.Event(), threading.Event()
        original = self.service.record_result
        def blocked_result(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture result stalled')
            return original(*args, **kwargs)
        with patch.object(self.service, 'record_result', side_effect=blocked_result):
            screen = asyncio.create_task(self._screen(username, claim=claim, visibility='public', exclude=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertEqual(1, self._counts()['workbench_collection_exclusions'])
                screen.cancel()
                await asyncio.sleep(0)
                self.assertFalse(screen.done())
            finally:
                release.set()
                await asyncio.gather(screen, return_exceptions=True)
        self.assertTrue(screen.cancelled())
        self.assertEqual({'task_results': 1, 'workbench_candidates': 0, 'workbench_collection_exclusions': 1}, self._counts())


if __name__ == '__main__':
    unittest.main()
