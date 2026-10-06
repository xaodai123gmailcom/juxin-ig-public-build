"""Offline same-window final-source completion and production child-pool reuse."""
import asyncio
from contextlib import closing
import inspect
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.final_seed_completion_selftest import final_seed_scenario, factory_reconnect_scenario


class FinalSeedCompletionR62Tests(unittest.IsolatedAsyncioTestCase):
    # Use the same bounded 30s deadline as the installed/Core selftest. These
    # real FULL-sync SQLite scenarios prove ownership and persistence, not an
    # 8s disk-speed target; their completion and data assertions stay intact.
    async def test_one_source_drains_and_confirms_native_close(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = await final_seed_scenario(Path(directory), source_count=1, child_count=1)
        self.assertEqual(1, proof['source_calls'])
        self.assertTrue(proof['results_dedupe_history_preserved'])

    async def test_two_sources_recover_when_last_seed_has_already_emptied_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = await final_seed_scenario(Path(directory), source_count=2, child_count=3)
        self.assertEqual(2, proof['source_calls'])
        self.assertTrue(proof['no_remaining_seed_or_lease'])

    async def test_three_sources_retire_idle_late_children_without_losing_last_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = await final_seed_scenario(Path(directory), source_count=3, child_count=3)
        self.assertEqual(3, proof['source_calls'])
        self.assertEqual(3, proof['saved_results'])
        self.assertEqual(1, proof['provider_closes'])
        self.assertEqual(1, proof['lease_generations'])
        self.assertTrue(proof['production_playwright_pool'])
        self.assertTrue(proof['selected_pool_bound'])

class FinalSeedFactoryReconnectR62Tests(unittest.IsolatedAsyncioTestCase):
    async def test_authoritative_factory_errors_reconnect_despite_healthy_parent(self):
        for reason in ('worker_not_connected', 'browser_context_missing'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                proof = await factory_reconnect_scenario(Path(directory), failure_reason=reason)
                self.assertEqual(reason, proof['injected_reason'])
                self.assertEqual(1, proof['reconnects'])
                self.assertEqual(2, proof['saved_results'])
                self.assertTrue(proof['waiting_queue_empty_at_failure'])
                self.assertTrue(proof['no_remaining_seed_lease_or_waiter'])


class FinalSeedPoolOwnershipR62Tests(unittest.IsolatedAsyncioTestCase):
    def parent(self):
        from types import SimpleNamespace
        from test_parallel_screening_worker import _Page, _connected_parent
        pages = []
        async def new_page():
            page = _Page('child-' + str(len(pages)))
            pages.append(page)
            self.assertLessEqual(sum(item.close_calls == 0 for item in pages), 3)
            return page
        source = _Page('operator-source')
        return _connected_parent(SimpleNamespace(new_page=new_page), source), source, pages

    async def test_active_child_with_late_operation_is_never_retired(self):
        parent, source, pages = self.parent()
        child = await parent.create_parallel_screening_worker()
        late = asyncio.get_running_loop().create_future()
        child._track_late_lifecycle_task(late)
        try:
            other = await parent.create_parallel_screening_worker()
            self.assertIsNot(other, child)
            self.assertEqual(0, pages[0].close_calls)
            self.assertIn(child._task_page_slot, parent._screening_slots_in_use)
            self.assertIs(parent._screening_worker_pool[child._task_page_slot], child)
            self.assertEqual(0, source.close_calls)
        finally:
            late.set_result(None)
            await asyncio.sleep(0)
            await parent.disconnect()
        self.assertTrue(all(page.close_calls == 1 for page in pages))
        self.assertEqual(0, source.close_calls)

    async def test_concurrent_factories_cannot_reacquire_retiring_idle_child(self):
        parent, source, pages = self.parent()
        retiring = await parent.create_parallel_screening_worker()
        parent.release_parallel_screening_worker(retiring)
        late = asyncio.get_running_loop().create_future()
        retiring._track_late_lifecycle_task(late)
        # Deterministic ready-queue order: A reserves retirement, the late-task
        # callback drains its set, B runs before disconnect gets its first turn.
        first = asyncio.create_task(parent.create_parallel_screening_worker())
        late.set_result(None)
        second = asyncio.create_task(parent.create_parallel_screening_worker())
        try:
            children = await asyncio.wait_for(asyncio.gather(first, second), 2)
            self.assertTrue(all(child is not retiring for child in children))
            self.assertIsNot(children[0], children[1])
            self.assertEqual(1, pages[0].close_calls)
            self.assertEqual(0, source.close_calls)
            self.assertLessEqual(sum(page.close_calls == 0 for page in pages), 3)
        finally:
            await asyncio.gather(first, second, return_exceptions=True)
            await parent.disconnect()
        self.assertTrue(all(page.close_calls == 1 for page in pages))
        self.assertEqual(0, source.close_calls)


class FinalSeedEvidenceReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_is_read_only_autocommit_and_sees_full_sync_commits(self):
        import sqlite3
        from unittest.mock import patch
        from app.database import Database
        from app.final_seed_completion_selftest import _LeaseEvidenceReader
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'observer.sqlite3')
            database.initialize()
            with database.write() as connection:
                connection.execute('CREATE TABLE fixture_observation(value INTEGER)')
                connection.execute('INSERT INTO fixture_observation VALUES(0)')
            original_read, original_write = database.read, database.write
            observer = _LeaseEvidenceReader(database.path)
            try:
                with observer.read() as connection:
                    self.assertEqual(1, connection.execute('PRAGMA query_only').fetchone()[0])
                    self.assertIsNone(connection.isolation_level)
                    self.assertFalse(connection.in_transaction)
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute('UPDATE fixture_observation SET value=99')
                self.assertEqual(original_read, database.read)
                self.assertEqual(original_write, database.write)
                with patch.object(database, '_connect', wraps=database._connect) as opened:
                    for value in range(1, 6):
                        with database.write() as production:
                            self.assertEqual(2, production.execute('PRAGMA synchronous').fetchone()[0])
                            production.execute('UPDATE fixture_observation SET value=?', (value,))
                        with observer.read() as connection:
                            self.assertEqual(value, connection.execute('SELECT value FROM fixture_observation').fetchone()[0])
                            self.assertFalse(connection.in_transaction)
                    self.assertEqual(5, opened.call_count)
                def worker_read():
                    with observer.read() as connection:
                        return connection.execute('SELECT value FROM fixture_observation').fetchone()[0]
                self.assertEqual(5, await asyncio.to_thread(worker_read))
            finally:
                observer.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            reopened = Database(database.path)
            reopened.initialize()
            with reopened.read() as connection:
                self.assertEqual(5, connection.execute('SELECT value FROM fixture_observation').fetchone()[0])

    async def test_observer_closes_after_error_and_cannot_reopen_late(self):
        import sqlite3
        from app.database import Database
        from app.final_seed_completion_selftest import _LeaseEvidenceReader
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'error.sqlite3')
            database.initialize()
            observer = _LeaseEvidenceReader(database.path)
            with self.assertRaisesRegex(RuntimeError, 'injected observer failure'):
                try:
                    with observer.read() as connection:
                        raise RuntimeError('injected observer failure')
                finally:
                    observer.close()
            observer.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            with self.assertRaisesRegex(RuntimeError, 'observer is closed'):
                with observer.read(): pass


class FinalSeedDeadlineContractR62Tests(unittest.IsolatedAsyncioTestCase):
    def test_unit_and_installed_selftests_share_the_bounded_default(self):
        for scenario in (final_seed_scenario, factory_reconnect_scenario):
            with self.subTest(scenario=scenario.__name__):
                self.assertEqual(
                    30.0, inspect.signature(scenario).parameters['completion_timeout'].default)

    async def test_real_idle_child_retirement_stall_is_still_a_failure(self):
        from app.execution_manager import ExecutionManager
        from app.playwright_worker import PlaywrightWorker

        entered = asyncio.Event()
        release = asyncio.Event()
        managers, shutdowns, blocked_children = [], [], []
        original_start = ExecutionManager.start
        original_shutdown = ExecutionManager.shutdown
        original_disconnect = PlaywrightWorker.disconnect

        async def stalled_disconnect(worker):
            if getattr(worker, '_screening_pool_parent', None) is not None:
                blocked_children.append(worker)
                entered.set()
                await release.wait()
            return await original_disconnect(worker)

        async def start_after_stall(manager, *args, **kwargs):
            managers.append(manager)
            result = await original_start(manager, *args, **kwargs)
            # Only the test waits for its controlled stall before starting the
            # short deadline. Slow disk/setup cannot make this negative case
            # expire before it reaches real idle-child retirement.
            await asyncio.wait_for(entered.wait(), 30)
            return result

        async def release_during_shutdown(manager):
            shutdowns.append(manager)
            release.set()
            await original_shutdown(manager)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(PlaywrightWorker, 'disconnect', stalled_disconnect), \
                 patch.object(ExecutionManager, 'start', start_after_stall), \
                 patch.object(ExecutionManager, 'shutdown', release_during_shutdown):
                with self.assertRaisesRegex(RuntimeError, 'did not drain final source') as failure:
                    await final_seed_scenario(
                        Path(directory), source_count=2, child_count=1, completion_timeout=.05)
            self.assertIsInstance(failure.exception.__cause__, TimeoutError)
            self.assertTrue(entered.is_set())
            self.assertEqual(managers, shutdowns)
            self.assertFalse(managers[0].active_task_ids())
            self.assertTrue(blocked_children)
            self.assertTrue(all(child._worker_owned_page is None
                                and not child._late_lifecycle_tasks
                                for child in blocked_children))
            with closing(sqlite3.connect(Path(directory) / 'final-seed-2-1.sqlite3')) as connection:
                self.assertEqual(0, connection.execute(
                    'SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])
                self.assertEqual(1, connection.execute(
                    'SELECT COUNT(*) FROM task_results').fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM task_targets WHERE status='completed'").fetchone()[0])
                self.assertNotEqual('completed', connection.execute(
                    'SELECT status FROM tasks').fetchone()[0])


    async def test_retirement_observer_closes_before_temporary_directory_cleanup(self):
        for assertion_failure in (False, True):
            with self.subTest(assertion_failure=assertion_failure):
                observers, cleanup_states = [], []
                original_connect = sqlite3.connect
                original_directory = tempfile.TemporaryDirectory

                class TrackingConnection(sqlite3.Connection):
                    closed = False

                    def close(connection):
                        super().close()
                        connection.closed = True

                    def execute(connection, statement, *args, **kwargs):
                        if assertion_failure and statement == 'SELECT COUNT(*) FROM browser_operation_leases':
                            raise AssertionError('injected observer assertion')
                        return super().execute(statement, *args, **kwargs)

                def connect_observer(database, *args, **kwargs):
                    # Production connections supply durability/thread options.
                    # Track only this test's final read-only assertion observer.
                    if not args and not kwargs and Path(database).name == 'final-seed-2-1.sqlite3':
                        connection = original_connect(database, factory=TrackingConnection)
                        observers.append(connection)
                        return connection
                    return original_connect(database, *args, **kwargs)

                class TrackingDirectory(original_directory):
                    def __exit__(directory, *args):
                        cleanup_states.append([connection.closed for connection in observers])
                        # Preserve the pre-cleanup evidence, then release a leaked
                        # test handle so this regression also fails cleanly on Windows.
                        for connection in observers:
                            if not connection.closed:
                                connection.close()
                        return super().__exit__(*args)

                with patch.object(sqlite3, 'connect', connect_observer), \
                     patch.object(tempfile, 'TemporaryDirectory', TrackingDirectory):
                    if assertion_failure:
                        with self.assertRaisesRegex(AssertionError, 'injected observer assertion'):
                            await self.test_real_idle_child_retirement_stall_is_still_a_failure()
                    else:
                        await self.test_real_idle_child_retirement_stall_is_still_a_failure()
                self.assertEqual(1, len(observers))
                self.assertEqual([[True]], cleanup_states, 'SQLite observer was open before directory cleanup')
                with self.assertRaises(sqlite3.ProgrammingError):
                    observers[0].execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
