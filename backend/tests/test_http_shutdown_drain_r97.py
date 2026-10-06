"""HTTP transport failure must not prevent or truncate owned lifespan cleanup."""
from __future__ import annotations

import asyncio
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from uvicorn.lifespan.on import LifespanOn
from app.__main__ import HTTP_DRAIN_TIMEOUT_SECONDS, create_server
from app.async_cleanup import finish_owned


class HttpShutdownDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_connection_reaches_lifespan_without_timing_out_owned_cleanup(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        durable_finished = asyncio.Event()
        ownership = {'held': True}

        async def app(scope, receive, send):
            self.assertEqual(scope['type'], 'lifespan')
            self.assertEqual((await receive())['type'], 'lifespan.startup')
            await send({'type': 'lifespan.startup.complete'})
            self.assertEqual((await receive())['type'], 'lifespan.shutdown')
            entered.set()
            # Model the real app's cancellation-safe durable/lease cleanup.
            async def cleanup():
                await release.wait()
                ownership['held'] = False
                durable_finished.set()
            await finish_owned(cleanup())
            await send({'type': 'lifespan.shutdown.complete'})

        server = create_server(app, host='127.0.0.1', port=0)
        self.assertEqual(server.config.timeout_graceful_shutdown, 5.0)
        self.assertEqual(HTTP_DRAIN_TIMEOUT_SECONDS, 5.0)
        # Exercise real Uvicorn behavior with a shorter test-only HTTP budget.
        server.config.timeout_graceful_shutdown = .02
        server.config.load()
        server.lifespan = LifespanOn(server.config)
        server.servers = []
        await server.lifespan.startup()
        connection = Mock()
        server.server_state.connections.add(connection)
        shutdown = asyncio.create_task(server.shutdown())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            connection.shutdown.assert_called_once_with()
            await asyncio.sleep(.06)
            self.assertFalse(shutdown.done())
            self.assertTrue(ownership['held'])
            self.assertFalse(durable_finished.is_set())
            self.assertFalse(server.force_exit)
            release.set()
            await asyncio.wait_for(shutdown, 2)
            self.assertTrue(durable_finished.is_set())
            self.assertFalse(ownership['held'])
            self.assertTrue(server.lifespan.shutdown_event.is_set())
        finally:
            release.set()
            await asyncio.wait_for(shutdown, 2)

    async def test_http_task_cancellation_does_not_release_durable_work_early(self):
        release = asyncio.Event()
        started = asyncio.Event()
        completed = asyncio.Event()

        async def owned_operation():
            started.set()
            await release.wait()
            completed.set()

        request_task = asyncio.create_task(finish_owned(owned_operation()))
        await started.wait()

        class Lifespan:
            async def shutdown(self):
                # Production managers drain their owned operations on lifespan.
                with self_test.assertRaises(asyncio.CancelledError):
                    await request_task

        self_test = self
        server = create_server(None, host='127.0.0.1', port=0)
        server.config.timeout_graceful_shutdown = .02
        server.servers = []
        server.lifespan = Lifespan()
        server.server_state.tasks.add(request_task)
        shutdown = asyncio.create_task(server.shutdown())
        try:
            async def wait_for_cancellation():
                while not request_task.cancelling():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_for_cancellation(), 2)
            self.assertGreater(request_task.cancelling(), 0)
            self.assertFalse(request_task.done())
            self.assertFalse(shutdown.done())
            self.assertFalse(completed.is_set())
            release.set()
            await asyncio.wait_for(shutdown, 2)
            self.assertTrue(completed.is_set())
        finally:
            release.set()
            await asyncio.wait_for(shutdown, 2)


class ProbeResponseLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'scripts/verify_frozen_core_service.py'
        spec = importlib.util.spec_from_file_location('http_drain_probe', path)
        cls.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.probe)

    def test_expected_http_error_is_drained_closed_and_reraised(self):
        for status in (401, 409):
            with self.subTest(status=status):
                body = io.BytesIO(b'{"detail":"expected refusal"}')
                error = HTTPError('http://127.0.0.1/', status, 'refused', {}, body)
                error.read = Mock(wraps=error.read)
                opener = Mock()
                opener.open.side_effect = error
                with self.assertRaises(HTTPError) as caught:
                    self.probe.read_json_response(opener, object(), timeout=1)
                self.assertIs(caught.exception, error)
                error.read.assert_called_once_with()
                self.assertTrue(body.closed)

    def test_read_failure_still_closes_error_response_and_fails_probe(self):
        body = io.BytesIO(b'incomplete')
        error = HTTPError('http://127.0.0.1/', 409, 'refused', {}, body)
        error.read = Mock(side_effect=TimeoutError('read deadline'))
        opener = Mock()
        opener.open.side_effect = error
        with self.assertRaises(TimeoutError):
            self.probe.read_json_response(opener, object(), timeout=1)
        self.assertTrue(body.closed)


class RealApplicationOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_lifespan_drains_workbench_async_and_sync_handlers_before_unlock(self):
        import tempfile
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.config import Settings
        from app.main import create_app
        from app.schemas import RegisterRequest, WorkbenchCommandRequest
        from fastapi import HTTPException

        for kind in ('workbench', 'async_account', 'sync_register'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                settings = Settings(startup_token='shutdown-test-token-at-least-32-characters',
                                    database_path=Path(temp) / 'core.sqlite3', data_dir=Path(temp))
                app = create_app(settings, bitbrowser=SimpleNamespace())
                server = create_server(app, host='127.0.0.1', port=0)
                server.config.timeout_graceful_shutdown = .02
                server.config.load()
                server.lifespan = LifespanOn(server.config)
                server.servers = []
                await server.lifespan.startup()
                self.assertFalse(server.lifespan.startup_failed)
                entered = threading.Event()
                release = threading.Event()
                committed = threading.Event()
                db = app.state.database

                def write_after_release(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test release deadline')
                    with db.write() as connection:
                        connection.execute('CREATE TABLE shutdown_proof (value TEXT)')
                        connection.execute("INSERT INTO shutdown_proof VALUES ('committed')")
                    committed.set()
                    return {}

                if kind == 'workbench':
                    target, attribute = app.state.service, 'claim_workbench_identity'
                    path = '/api/workbench/commands'
                    kwargs = {'body': WorkbenchCommandRequest(command='dedupe_claim', payload={'username':'proof'}),
                              'session': ({'id':'proof-owner'}, 'token')}
                elif kind == 'async_account':
                    target, attribute = app.state.accounts, 'command'
                    path = '/api/accounts/command'
                    kwargs = {'payload': {}, 'session': ({'id':'proof-owner'}, 'token')}
                else:
                    target, attribute = app.state.service, 'register_user'
                    path = '/v1/auth/register'
                    kwargs = {'body': RegisterRequest(username='proof', password='long-proof-password')}
                endpoint = next(route.endpoint for route in app.routes if route.path == path)
                with patch.object(target, attribute, side_effect=write_after_release), patch.object(
                        app.state.execution_manager, 'shutdown', wraps=app.state.execution_manager.shutdown) as stop:
                    request = asyncio.create_task(endpoint(**kwargs))
                    shutdown = None
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        server.server_state.tasks.add(request)
                        shutdown = asyncio.create_task(server.shutdown())
                        async def wait_for_drain():
                            while not app.state.owned_requests.draining:
                                await asyncio.sleep(.01)
                        await asyncio.wait_for(wait_for_drain(), 2)
                        self.assertGreater(request.cancelling(), 0)
                        self.assertFalse(shutdown.done())
                        self.assertFalse(committed.is_set())
                        self.assertIsNotNone(db._instance_lock_file)
                        stop.assert_not_called()
                        with self.assertRaises(HTTPException) as refused:
                            await endpoint(**kwargs)
                        self.assertEqual(refused.exception.status_code, 503)
                        release.set()
                        await asyncio.wait_for(shutdown, 3)
                        with self.assertRaises(asyncio.CancelledError):
                            await request
                        self.assertTrue(committed.is_set())
                        stop.assert_awaited_once()
                        self.assertIsNone(db._instance_lock_file)
                        with db.read() as connection:
                            self.assertEqual('committed', connection.execute('SELECT value FROM shutdown_proof').fetchone()[0])
                    finally:
                        release.set()
                        if shutdown is not None:
                            await asyncio.wait_for(shutdown, 3)
                        await asyncio.gather(request, return_exceptions=True)


    async def test_repeated_lifespan_cancellation_finishes_cleanup_and_reopens_admission(self):
        import tempfile
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.config import Settings
        from app.main import create_app

        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Settings(startup_token='shutdown-test-token-at-least-32-characters',
                database_path=Path(temp) / 'core.sqlite3', data_dir=Path(temp)), bitbrowser=SimpleNamespace())
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            entered = threading.Event()
            release = threading.Event()
            def blocked(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test release deadline')
                return {}
            endpoint = next(route.endpoint for route in app.routes if route.path == '/api/accounts/command')
            with patch.object(app.state.accounts, 'command', side_effect=blocked), patch.object(
                    app.state.execution_manager, 'shutdown', wraps=app.state.execution_manager.shutdown) as stop:
                request = asyncio.create_task(endpoint(payload={}, session=({'id':'proof'}, 'token')))
                closing = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
                    while not app.state.owned_requests.draining:
                        await asyncio.sleep(.01)
                    for _ in range(2):
                        closing.cancel()
                        request.cancel()
                        await asyncio.sleep(.01)
                    self.assertFalse(closing.done())
                    self.assertIsNotNone(app.state.database._instance_lock_file)
                    stop.assert_not_called()
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(closing, 3)
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                    stop.assert_awaited_once()
                    self.assertIsNone(app.state.database._instance_lock_file)
                finally:
                    release.set()
                    await asyncio.gather(request, *([closing] if closing is not None else []), return_exceptions=True)
            async with app.router.lifespan_context(app):
                self.assertFalse(app.state.owned_requests.draining)
                self.assertEqual({}, await app.state.owned_requests.run(lambda: {}))


if __name__ == '__main__':
    unittest.main()
