"""Callback connection reuse preserves per-identity durability and ownership."""
import asyncio
from contextlib import contextmanager
import threading
import sqlite3
import unittest
from unittest.mock import patch

from app.database import Database
from app.discovery_write_session import DiscoveryWriteSession
import test_discovery_checkpoint_r5 as checkpoint_fixture


class _TrackedConnection:
    def __init__(self, connection, events):
        self.connection = connection
        self.events = events
        self.commits = 0
        self.closes = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        self.connection.commit()
        self.commits += 1
        self.events.append('commit')

    def close(self):
        self.closes += 1
        self.connection.close()
        self.events.append('close')


class DiscoveryConnectionR5Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = checkpoint_fixture.DiscoveryCheckpointR5Tests.asyncSetUp
    asyncTearDown = checkpoint_fixture.DiscoveryCheckpointR5Tests.asyncTearDown
    _task_and_control = checkpoint_fixture.DiscoveryCheckpointR5Tests._task_and_control
    launch = checkpoint_fixture.DiscoveryCheckpointR5Tests.launch
    checkpoint = checkpoint_fixture.DiscoveryCheckpointR5Tests.checkpoint
    stats = checkpoint_fixture.DiscoveryCheckpointR5Tests.stats

    @contextmanager
    def track(self):
        self.connections = []
        self.sessions = []
        self.events = []
        original_connect = self.service.database._connect_discovery
        original_factory = self.service._new_discovery_write_session

        def connect():
            connection = _TrackedConnection(original_connect(), self.events)
            self.connections.append(connection)
            self.events.append('open')
            return connection

        def factory():
            session = original_factory()
            self.sessions.append(session)
            return session

        with patch.object(self.service.database, '_connect_discovery', side_effect=connect), \
             patch.object(self.service, '_new_discovery_write_session', side_effect=factory):
            yield

    def assert_closed(self):
        self.assertTrue(all(session._closed and session._connection is None for session in self.sessions))
        self.assertTrue(all(connection.closes == 1 for connection in self.connections))

    async def test_reuse_is_callback_scoped_and_commits_every_identity(self):
        task, target, _, source = await self.launch()
        with self.track():
            for batch in range(3):
                result = await source.sink([f'session_{batch}_{i}' for i in range(100)])
                self.assertEqual((batch + 1) * 100, result['total'])
                self.assertEqual(batch + 1, len(self.connections))
                self.assertEqual(100, self.connections[-1].commits)
                self.assert_closed()
        self.assertEqual(300, self.stats(task, target)['total'])

    async def test_paused_callback_holds_no_transaction_or_writer_lock(self):
        first = await self.launch()
        second = await self.launch()
        loop = asyncio.get_running_loop()
        paused_prefix = asyncio.Event()
        original_discover = self.service.discover_task_mode_candidate
        original_save = self.service.upsert_checkpoint

        def discover(*args, **kwargs):
            result = original_discover(*args, **kwargs)
            if args[-1] == 'paused_first':
                loop.call_soon_threadsafe(first[2].pause_event.clear)
            return result

        def save(*args, **kwargs):
            result = original_save(*args, **kwargs)
            if args[1] == first[0]['id'] and kwargs['counters'].get('discovered') == 1:
                loop.call_soon_threadsafe(paused_prefix.set)
            return result

        with self.track(), patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover), \
             patch.object(self.service, 'upsert_checkpoint', side_effect=save):
            call = asyncio.create_task(first[3].sink(['paused_first', 'paused_second']))
            try:
                await asyncio.wait_for(paused_prefix.wait(), 10)
                self.assertFalse(call.done())
                self.assertEqual(1, len(self.connections))
                self.assertFalse(self.connections[0].in_transaction)
                # A different callback/window still commits while the first is
                # paused with its connection open. No global pool is involved.
                result = await asyncio.wait_for(second[3].sink(['other_window']), 10)
                self.assertEqual(1, result['total'])
                self.assertEqual(2, len(self.connections))
                self.assertIsNot(self.sessions[0], self.sessions[1])
                self.assertIsNot(self.connections[0], self.connections[1])
                self.assertEqual([0, 1], [c.closes for c in self.connections])
                self.assertEqual(1, self.stats(first[0], first[1])['total'])
                first[2].pause_event.set()
                await asyncio.wait_for(call, 10)
                self.assert_closed()
            finally:
                first[2].pause_event.set()
                if not call.done():
                    call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    async def test_cancel_before_first_admission_closes_unopened_session(self):
        _, _, control, source = await self.launch()
        created = asyncio.Event()
        original_factory = self.service._new_discovery_write_session
        sessions = []

        def factory():
            session = original_factory()
            sessions.append(session)
            control.pause_event.clear()
            created.set()
            return session

        with patch.object(self.service, '_new_discovery_write_session', side_effect=factory), \
             patch.object(self.service.database, '_connect_discovery', side_effect=AssertionError('unexpected open')):
            call = asyncio.create_task(source.sink(['never_admitted']))
            try:
                await asyncio.wait_for(created.wait(), 10)
                call.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(call, 10)
                self.assertEqual(1, len(sessions))
                self.assertTrue(sessions[0]._closed)
                self.assertIsNone(sessions[0]._connection)
                self.assertFalse(self.service.check_global_dedupe('never_admitted')['seen'])
            finally:
                control.pause_event.set()
                await asyncio.gather(call, return_exceptions=True)

    async def test_cancel_during_first_open_drains_commit_then_repeated_cancel_drains_close(self):
        task, target, _, source = await self.launch()
        loop = asyncio.get_running_loop()
        opening, closing = asyncio.Event(), asyncio.Event()
        release_open, release_close = threading.Event(), threading.Event()
        original_connect = self.service.database._connect_discovery
        original_close = DiscoveryWriteSession.close
        connection = None
        sessions = []
        events = []

        def connect():
            nonlocal connection
            loop.call_soon_threadsafe(opening.set)
            if not release_open.wait(10):
                raise TimeoutError('test did not release first open')
            connection = _TrackedConnection(original_connect(), events)
            events.append('open')
            return connection

        def close(session):
            sessions.append(session)
            loop.call_soon_threadsafe(closing.set)
            if not release_close.wait(10):
                raise TimeoutError('test did not release close')
            original_close(session)

        with patch.object(self.service.database, '_connect_discovery', side_effect=connect), \
             patch.object(DiscoveryWriteSession, 'close', close):
            call = asyncio.create_task(source.sink(['cancel_committed', 'cancel_suffix']))
            try:
                await asyncio.wait_for(opening.wait(), 10)
                call.cancel()
                await asyncio.sleep(0)
                call.cancel()
                self.assertFalse(call.done())
                self.assertIsNone(connection)
                release_open.set()
                await asyncio.wait_for(closing.wait(), 10)
                self.assertEqual(['open', 'commit'], events)
                self.assertEqual(1, self.checkpoint(task, target)['counters']['discovered'])
                call.cancel()
                await asyncio.sleep(0)
                call.cancel()
                self.assertFalse(call.done())
                self.assertEqual(0, connection.closes)
                release_close.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(call, 10)
                self.assertEqual(['open', 'commit', 'close'], events)
                self.assertEqual(1, len(sessions))
                self.assertTrue(sessions[0]._closed)
                self.assertFalse(self.service.check_global_dedupe('cancel_suffix')['seen'])
            finally:
                release_open.set()
                release_close.set()
                await asyncio.gather(call, return_exceptions=True)

    async def test_flush_error_still_closes_committed_connection(self):
        task, target, _, source = await self.launch()
        with self.track(), patch.object(self.service, 'upsert_checkpoint', side_effect=RuntimeError('flush failed')):
            with self.assertRaisesRegex(RuntimeError, 'flush failed'):
                await source.sink(['flush_committed'])
            self.assert_closed()
        self.assertEqual(1, self.stats(task, target)['total'])

    async def test_cancel_during_first_transaction_commits_before_close(self):
        task, target, _, source = await self.launch()
        loop = asyncio.get_running_loop()
        writing = asyncio.Event()
        release = threading.Event()
        original_append = self.service.append_task_mode_candidates

        def append(*args, **kwargs):
            result = original_append(*args, **kwargs)
            loop.call_soon_threadsafe(writing.set)
            if not release.wait(10):
                raise TimeoutError('test did not release first transaction')
            return result

        with self.track(), patch.object(self.service, 'append_task_mode_candidates', side_effect=append):
            call = asyncio.create_task(source.sink(['transaction_first', 'transaction_never']))
            try:
                await asyncio.wait_for(writing.wait(), 10)
                self.assertTrue(self.connections[0].in_transaction)
                self.assertFalse(self.service.check_global_dedupe('transaction_first')['seen'])
                call.cancel()
                await asyncio.sleep(0)
                call.cancel()
                self.assertFalse(call.done())
                self.assertEqual(['open'], self.events)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(call, 10)
                self.assertEqual(['open', 'commit', 'close'], self.events)
                self.assertEqual(1, self.checkpoint(task, target)['counters']['discovered'])
                self.assertTrue(self.service.check_global_dedupe('transaction_first')['seen'])
                self.assertFalse(self.service.check_global_dedupe('transaction_never')['seen'])
                self.assert_closed()
            finally:
                release.set()
                await asyncio.gather(call, return_exceptions=True)

    async def test_failed_identity_rolls_back_and_session_can_close_once(self):
        task, target, _, _ = await self.launch()
        with self.track():
            session = self.service._new_discovery_write_session()
            with patch.object(self.service, 'append_task_mode_candidates', side_effect=RuntimeError('append failed')):
                with self.assertRaisesRegex(RuntimeError, 'append failed'):
                    await asyncio.to_thread(self.service.discover_task_mode_candidate,
                        self.user['id'], task['id'], target['id'], 'followers', 'rollback_identity', _session=session)
            self.assertFalse(self.connections[0].in_transaction)
            self.assertFalse(self.service.check_global_dedupe('rollback_identity')['seen'])
            await asyncio.to_thread(session.close)
            await asyncio.to_thread(session.close)
            self.assert_closed()

    async def test_old_service_adapter_uses_original_call_signature(self):
        _, _, _, source = await self.launch()
        original = self.service.discover_task_mode_candidate

        def old_discover(owner, task, target, mode, username):
            return original(owner, task, target, mode, username)

        with patch.object(self.service, '_new_discovery_write_session', None), \
             patch.object(self.service, 'discover_task_mode_candidate', side_effect=old_discover), \
             patch.object(self.service.database, '_connect_discovery', side_effect=AssertionError('legacy opened session')):
            self.assertEqual(2, (await source.sink(['legacy_a', 'legacy_b']))['total'])

    async def test_session_rejects_foreign_database_and_closed_reuse(self):
        with self.track():
            session = self.service._new_discovery_write_session()
            with self.assertRaisesRegex(ValueError, 'different database'):
                with session.write(Database(self.service.database.path)):
                    self.fail('foreign database accepted')
            session.close()
            with self.assertRaisesRegex(RuntimeError, 'already closed'):
                with session.write(self.service.database):
                    self.fail('closed session accepted')
            self.assertEqual([], self.connections)
            self.assert_closed()

    async def test_initialization_failure_closes_connection_and_keeps_session_unopened(self):
        session = self.service._new_discovery_write_session()

        class BrokenConnection:
            row_factory = None
            closes = 0

            def execute(self, statement):
                raise sqlite3.OperationalError('pragma failed')

            def close(self):
                self.closes += 1

        broken = BrokenConnection()
        with patch('app.database.sqlite3.connect', return_value=broken):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'pragma failed'):
                with session.write(self.service.database):
                    self.fail('broken connection entered transaction')
        self.assertEqual(1, broken.closes)
        self.assertIsNone(session._connection)
        session.close()
        session.close()
        self.assertEqual(1, broken.closes)
        self.assertTrue(session._closed)

    async def test_private_connection_matches_durability_and_can_change_threads(self):
        ordinary = self.service.database._connect()
        discovery = self.service.database._connect_discovery()
        try:
            for pragma in ('foreign_keys', 'busy_timeout', 'synchronous', 'wal_autocheckpoint'):
                self.assertEqual(ordinary.execute('PRAGMA ' + pragma).fetchone()[0],
                    discovery.execute('PRAGMA ' + pragma).fetchone()[0])
            self.assertEqual(1, await asyncio.to_thread(lambda: discovery.execute('SELECT 1').fetchone()[0]))
            # Cross-thread opt-in is confined to the new private connector.
            with self.assertRaises(sqlite3.ProgrammingError):
                await asyncio.to_thread(lambda: ordinary.execute('SELECT 1'))
        finally:
            ordinary.close()
            await asyncio.to_thread(discovery.close)

    async def test_cancelled_producer_commits_before_cleanup_and_closes_before_return(self):
        from test_single_handoff_r94 import SingleHandoffR94Tests
        from test_parallel_relation_pipeline import _ScreeningChild

        loop = asyncio.get_running_loop()
        committed = asyncio.Event()
        release = threading.Event()
        original_discover = self.service.discover_task_mode_candidate
        original_disconnect = _ScreeningChild.disconnect

        def discover(*args, **kwargs):
            result = original_discover(*args, **kwargs)
            loop.call_soon_threadsafe(committed.set)
            if not release.wait(10):
                raise TimeoutError('test did not release committed producer')
            return result

        async def disconnect(child):
            self.events.append('child_cleanup')
            await original_disconnect(child)

        with self.track(), patch.object(self.service, 'discover_task_mode_candidate', side_effect=discover), \
             patch.object(_ScreeningChild, 'disconnect', disconnect):
            task, target, control, state, source, job = SingleHandoffR94Tests.pipeline(
                self, 1, names=['owned_first', 'owned_never'], batch=True)
            try:
                await asyncio.wait_for(committed.wait(), 10)
                job.cancel()
                await asyncio.sleep(0)
                job.cancel()
                self.assertFalse(job.done())
                self.assertNotIn('close', self.events)
                self.assertNotIn('child_cleanup', self.events)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(job, 10)
                self.assert_closed()
                # Independent child cleanup can run concurrently with producer
                # cleanup. The identity has committed, and mode ownership cannot
                # return to its outer lease cleanup until the session is closed.
                self.assertLess(self.events.index('commit'), self.events.index('child_cleanup'))
                self.assertEqual(1, self.stats(task, target)['total'])
                self.assertFalse(self.service.check_global_dedupe('owned_never')['seen'])
            finally:
                release.set()
                await SingleHandoffR94Tests.cleanup(self, state, job)
