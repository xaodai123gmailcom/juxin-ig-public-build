"""Shutdown contracts, plus --real-driver's mandatory pinned-driver CDP regression.

The default suite is stdlib-only for the pre-dependency Windows contracts stage.
The early stage explicitly runs --real-driver immediately after dependencies;
it cannot silently skip a missing Playwright/websockets installation. The local
CDP server is a protocol fixture, never installed-product passing evidence.
"""
import ast
import asyncio
from contextlib import contextmanager
import importlib.metadata
import io
import json
import math
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/verify_installed_recovery_r64.py'


def load_verifier():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'diagnostic_phase', 'installed_phase', 'require_owned_process', 'shutdown_remaining',
             'request_installed_shutdown', 'finish_installed_shutdown', 'installed_probe_directory'}
    nodes = [node for node in tree.body if
        isinstance(node, ast.FunctionDef) and node.name in names or
        isinstance(node, ast.ClassDef) and (node.name.startswith('InstalledRecovery') or node.name == 'OwnedRecoveryTools') or
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('INSTALLED_') for t in node.targets)]
    def require(condition, message):
        if not condition: raise RuntimeError(message)
    scope = dict(contextmanager=contextmanager, require=require, threading=threading,
                 time=time, math=math, sys=sys, socket=socket, subprocess=subprocess, json=json, re=re)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return SimpleNamespace(**scope)


class ShutdownContracts(unittest.TestCase):
    def setUp(self):
        self.verifier = load_verifier()
        self.output = patch('sys.stdout', io.StringIO()); self.output.start()
        self.addCleanup(self.output.stop)

    def request(self, endpoint=None, status=200, encoded=None):
        v = self.verifier
        raw = Mock()
        owned = Mock(interrupted=False)
        @contextmanager
        def owned_socket(*args): yield raw
        owned.socket = Mock(side_effect=owned_socket)
        endpoint = endpoint if endpoint is not None else 'ws://127.0.0.1:31002/devtools/browser/fixture'
        response = Mock(status=status)
        response.read.return_value = encoded if encoded is not None else json.dumps({'webSocketDebuggerUrl': endpoint}).encode()
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        connection = Mock(); connection.getresponse.return_value = response
        websocket = Mock(); connection_context = Mock()
        connection_context.__enter__ = Mock(return_value=websocket); connection_context.__exit__ = Mock(return_value=False)
        connect = Mock(return_value=connection_context)
        with patch('http.client.HTTPConnection', return_value=connection), patch.dict(sys.modules,
                {'websockets': SimpleNamespace(), 'websockets.sync': SimpleNamespace(),
                 'websockets.sync.client': SimpleNamespace(connect=connect)}):
            v.request_installed_shutdown(Mock(poll=Mock(return_value=None)), 31002, time.monotonic() + 5, owned)
        return owned, raw, connection, websocket, connect

    def test_same_root_browser_close_is_sent_once_without_waiting_for_ack(self):
        owned, raw, http, websocket, connect = self.request()
        websocket.send.assert_called_once_with('{"id":1,"method":"Browser.close"}')
        websocket.recv.assert_not_called()
        self.assertEqual(owned.socket.call_count, 2)
        self.assertEqual(connect.call_args.args, ('ws://127.0.0.1:31002/devtools/browser/fixture',))
        options = connect.call_args.kwargs
        self.assertIs(options['sock'], raw); self.assertIsNone(options['proxy'])
        self.assertLessEqual(options['open_timeout'], 2); self.assertLessEqual(options['close_timeout'], 1)
        http.request.assert_called_once_with('GET', '/json/version', headers={'Connection': 'close'})
        http.close.assert_called_once_with()

    def test_foreign_or_ambiguous_websocket_bindings_fail_before_send(self):
        for endpoint in ('ws://localhost:31002/devtools/browser/x', 'ws://127.0.0.1:31003/devtools/browser/x',
                'wss://127.0.0.1:31002/devtools/browser/x', 'ws://user@127.0.0.1:31002/devtools/browser/x',
                'ws://127.0.0.1:31002/devtools/browser/x?token=a', 'ws://127.0.0.1:31002/devtools/browser/x#x',
                'ws://127.0.0.1:31002/devtools/page/x', 'ws://127.0.0.1:31002/devtools/browser/x/../y',
                'ws://127.0.0.1:31002/devtools/browser/%78', 'ws://example.test:31002/devtools/browser/x',
                'ws://[::1]:31002/devtools/browser/x', 4):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(RuntimeError, 'binding changed'):
                self.request(endpoint)

    def test_http_redirect_error_malformed_and_unbounded_payload_fail(self):
        for status in (301, 302, 307, 308, 500):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'endpoint refused'):
                self.request(status=status)
        for encoded in (b'null', b'[]', b'{"not-url":true}', b'not-json', b'x' * 65537):
            with self.subTest(encoded=encoded[:20]), self.assertRaises((RuntimeError, ValueError)):
                self.request(encoded=encoded)

    def test_socket_registration_rejects_expired_latch_and_invalid_port(self):
        owned = self.verifier.OwnedRecoveryTools(); owned.abort()
        raw = Mock()
        with patch.object(socket, 'socket', return_value=raw):
            with self.assertRaisesRegex(RuntimeError, 'no longer eligible'):
                with owned.socket(31002, time.monotonic() + 1): self.fail('expired socket was usable')
        raw.close.assert_called_once(); raw.connect.assert_not_called()
        self.assertIsNone(owned.raw_socket)
        for port in (True, 0, 65536, -1, '31002'):
            with self.subTest(port=port), patch.object(socket, 'socket') as factory:
                with self.assertRaisesRegex(RuntimeError, 'port is invalid'):
                    with owned.socket(port, time.monotonic() + 1): pass
                factory.assert_not_called()

    def test_deadline_interrupts_registered_http_and_websocket_opens(self):
        for blocked in ('http', 'websocket'):
            with self.subTest(blocked=blocked):
                v = self.verifier; owned = v.OwnedRecoveryTools(); interrupted = threading.Event()
                raw = Mock(); raw.shutdown.side_effect = lambda *args: interrupted.set()
                response = Mock(status=200)
                response.read.return_value = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:31002/devtools/browser/fixture"}'
                response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
                http = Mock(); http.getresponse.return_value = response
                def pending(*args, **kwargs):
                    self.assertIs(owned.raw_socket, raw)
                    self.assertTrue(interrupted.wait(2), 'watchdog never interrupted registered socket')
                    raise OSError('owned socket interrupted')
                connect = Mock(side_effect=pending if blocked == 'websocket' else AssertionError('unexpected websocket'))
                if blocked == 'http': http.request.side_effect = pending
                with patch.object(socket, 'socket', return_value=raw), \
                        patch('http.client.HTTPConnection', return_value=http), patch.dict(sys.modules,
                        {'websockets': SimpleNamespace(), 'websockets.sync': SimpleNamespace(),
                         'websockets.sync.client': SimpleNamespace(connect=connect)}):
                    with self.assertRaises(v.InstalledRecoveryShutdownTimeout):
                        with v.installed_phase(Mock(), time.monotonic() + 3, .05,
                                v.InstalledRecoveryShutdownTimeout, cleanup=owned.abort) as deadline:
                            v.request_installed_shutdown(Mock(poll=Mock(return_value=None)), 31002, deadline, owned)
                self.assertTrue(owned.interrupted)
                raw.shutdown.assert_called_once_with(socket.SHUT_RDWR)
                self.assertIsNone(owned.raw_socket)

    def test_watchdog_interrupts_real_blocked_raw_send_and_timeout_still_fails(self):
        # socketpair is AF_UNIX on Unix and loopback TCP on Windows. Neither a
        # send-buffer hint nor a fixed payload proves backpressure on both.
        for send_buffer, receive_buffer in ((4096, 4096), (262144, 65536)):
            with self.subTest(send_buffer=send_buffer, receive_buffer=receive_buffer):
                v = self.verifier; owned = v.OwnedRecoveryTools()
                sender, unread = socket.socketpair()
                worker = None; done = threading.Event(); progress_lock = threading.Lock()
                progress = [0, 0]; errors = []; observed_at_abort = []
                chunk = b'x' * 65536
                def counters():
                    with progress_lock: return tuple(progress)
                def send_until_interrupted():
                    try:
                        while True:
                            with progress_lock: progress[0] += 1
                            sender.sendall(chunk)
                            with progress_lock: progress[1] += 1
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        done.set()
                def pending_send(wait):
                    before = counters()
                    writable = select.select([], [sender], [], wait)[1]
                    after = counters()
                    return (before == after and after[0] > after[1] and
                            not writable and not done.is_set())
                def abort_pending_send():
                    # Observe the actual socket immediately before interruption;
                    # an entered event alone could just be a delayed worker.
                    try: observed_at_abort.append(pending_send(0))
                    finally: owned.abort()
                try:
                    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer)
                    unread.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
                    owned.raw_socket = sender
                    setup_deadline = time.monotonic() + 3
                    sender.setblocking(False)
                    while True:
                        self.assertLess(time.monotonic(), setup_deadline, 'socket never reached backpressure')
                        try:
                            self.assertGreater(sender.send(chunk), 0)
                        except BlockingIOError:
                            break
                    sender.setblocking(True)
                    worker = threading.Thread(target=send_until_interrupted, daemon=True)
                    worker.start()
                    # Require no writable capacity and the same uncompleted
                    # real send across an observation interval. If a TCP ACK
                    # frees capacity, the worker keeps filling; no byte-count
                    # or receive-window assumption is needed.
                    while not pending_send(.02):
                        self.assertFalse(done.is_set(), 'sender failed before the watchdog')
                        self.assertLess(time.monotonic(), setup_deadline, 'no pending send was established')
                    self.assertEqual(errors, [])
                    started = time.monotonic()
                    with self.assertRaises(v.InstalledRecoveryShutdownTimeout):
                        with v.installed_phase(Mock(), started + 3, .15,
                                v.InstalledRecoveryShutdownTimeout, cleanup=abort_pending_send):
                            self.assertTrue(done.wait(1), 'socket interruption did not release the sender')
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), 'interrupted sender did not drain')
                    self.assertTrue(done.is_set())
                    self.assertEqual(observed_at_abort, [True])
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], OSError)
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertTrue(owned.interrupted)
                finally:
                    # Even an assertion failure must not strand the real I/O
                    # worker. Closing its unread peer is cleanup, never proof.
                    try: owned.abort()
                    finally:
                        unread.close(); sender.close()
                        if worker is not None:
                            worker.join(1)
                            self.assertFalse(worker.is_alive(), 'socket fixture cleanup did not drain')

    def test_windows_abort_uses_only_retained_handle_and_release_is_idempotent(self):
        owned = self.verifier.OwnedRecoveryTools()
        owned.driver = Mock(); owned.driver_handle = 123
        kernel = Mock(); kernel.WaitForSingleObject.return_value = 258
        kernel.TerminateProcess.return_value = True; kernel.CloseHandle.return_value = True
        owned.kernel = kernel
        raw = Mock(); owned.raw_socket = raw
        owned.abort()
        raw.shutdown.assert_called_once_with(socket.SHUT_RDWR); raw.close.assert_called_once()
        kernel.TerminateProcess.assert_called_once_with(123, 1)
        owned.driver.kill.assert_not_called(); owned.driver.poll.assert_not_called()
        owned.release(); owned.release()
        kernel.CloseHandle.assert_called_once_with(123)
        self.assertTrue(owned.interrupted)

    def test_driver_handle_cannot_be_released_while_any_watchdog_is_alive(self):
        owned = self.verifier.OwnedRecoveryTools()
        owned.driver_handle = 123; owned.kernel = Mock()
        timer = Mock(); timer.is_alive.return_value = True; owned.watchdogs.append(timer)
        with self.assertRaisesRegex(RuntimeError, 'cleanup is still running'): owned.release()
        owned.kernel.CloseHandle.assert_not_called()
        timer.is_alive.return_value = False
        owned.release(); owned.kernel.CloseHandle.assert_called_once_with(123)

    def test_driver_inspection_failure_cannot_become_success(self):
        owned = self.verifier.OwnedRecoveryTools()
        owned.driver_handle = 123; owned.kernel = Mock()
        owned.kernel.WaitForSingleObject.return_value = 0xffffffff
        with self.assertRaisesRegex(RuntimeError, 'inspect retained'):
            owned.abort()
        self.assertTrue(owned.interrupted); owned.kernel.TerminateProcess.assert_not_called()

    def finish(self, *, returncode=0, core_code=0, interrupted=False, runtime=None, wait_error=None, stop_error=None):
        v = self.verifier
        process = Mock(pid=10, returncode=returncode)
        process.wait.side_effect = wait_error
        runtime = runtime if runtime is not None else {'pid': 11, 'parent_pid': 10}
        playwright = Mock(); playwright.stop.side_effect = stop_error
        with patch.object(subprocess, 'run', return_value=Mock(returncode=core_code)) as query:
            v.finish_installed_shutdown(process, runtime, playwright, time.monotonic() + 3,
                                        SimpleNamespace(interrupted=interrupted))
        return process, playwright, query

    def test_normal_desktop_zero_core_gone_and_ordinary_driver_stop_all_required(self):
        process, playwright, query = self.finish()
        process.wait.assert_called_once(); playwright.stop.assert_called_once()
        self.assertIn('Get-Process -Id 11 ', query.call_args.args[0][-1])
        for kwargs, message in (({'returncode': 1}, 'not clean'), ({'core_code': 9}, 'Core alive'),
                ({'interrupted': True}, 'cannot prove normal'),
                ({'runtime': {'pid': 11, 'parent_pid': 20}}, 'binding changed'),
                ({'runtime': {'pid': True, 'parent_pid': 10}}, 'binding changed'),
                ({'wait_error': subprocess.TimeoutExpired('fixture', 3)}, 'did not shut down'),
                ({'stop_error': RuntimeError('driver stop failed')}, 'driver stop failed')):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(RuntimeError, message): self.finish(**kwargs)

    def test_deadline_exhaustion_never_grants_extra_time(self):
        for deadline in (time.monotonic() - 1, float('nan'), float('-inf')):
            with self.subTest(deadline=deadline), self.assertRaises(self.verifier.InstalledRecoveryShutdownTimeout):
                self.verifier.shutdown_remaining(deadline, 60)
        self.assertEqual(self.verifier.INSTALLED_PHASE_SECONDS['shutdown'], 60)
        self.assertEqual(self.verifier.INSTALLED_TIMEOUT_SECONDS, 420)

    def test_public_phase_allowlist_matches_without_new_proof_fields(self):
        tree = ast.parse((ROOT / 'ci/public_ci_installed_supervision.py').read_text())
        value = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'VERIFIER_PHASES' for t in n.targets))
        self.assertEqual(ast.literal_eval(value), self.verifier.INSTALLED_DIAGNOSTIC_PHASES)

    def test_real_driver_gate_runs_immediately_after_dependencies_before_build_work(self):
        tree = ast.parse((ROOT / 'ci/public_ci.py').read_text())
        early = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'early')
        calls = [n.value for n in early.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        labels = [n.args[0].value for n in calls if isinstance(n.func, ast.Name) and n.func.id == 'run_owned' and
                  n.args and isinstance(n.args[0], ast.Constant)]
        self.assertEqual(labels[labels.index('early-python-dependencies') + 1], 'early-installed-recovery-real-driver')
        call = next(n for n in calls if n.args and isinstance(n.args[0], ast.Constant) and
                    n.args[0].value == 'early-installed-recovery-real-driver')
        self.assertIn('--real-driver', ast.unparse(call))
        self.assertIn('test_installed_recovery_shutdown_r64.py', ast.unparse(call))


class LocalCdpFixture:
    def __init__(self, mode):
        self.mode = mode
        self.ready = threading.Event(); self.socket_closed = threading.Event(); self.close_received = threading.Event()
        self.clients = set(); self.errors = []; self.upgrades = []
        self.loop = None; self.stop_future = None; self.port = None
        def worker():
            try: asyncio.run(self.run())
            except BaseException as error: self.errors.append(error); self.ready.set()
        self.thread = threading.Thread(target=worker, daemon=True); self.thread.start()
        if not self.ready.wait(5) or self.errors: raise RuntimeError('Local CDP fixture startup failed')

    async def reply(self, ws, request, result):
        response = {'id': request['id'], 'result': result}
        if 'sessionId' in request: response['sessionId'] = request['sessionId']
        await ws.send(json.dumps(response))

    async def handle(self, ws):
        from websockets.exceptions import ConnectionClosed
        self.clients.add(ws)
        try:
            async for data in ws:
                request = json.loads(data); method = request['method']
                if method == 'Browser.close':
                    self.close_received.set()
                    if self.mode == 'ack': await self.reply(ws, request, {})
                    await asyncio.sleep(.05)
                    await asyncio.gather(*(client.close() for client in list(self.clients)), return_exceptions=True)
                    self.socket_closed.set()
                    break
                result = {}
                if method == 'Browser.getVersion':
                    result = {'protocolVersion': '1.3', 'product': 'Chrome/140.0.0.0',
                              'revision': 'fixture', 'userAgent': 'Chrome/140.0.0.0', 'jsVersion': '14'}
                elif method == 'Target.attachToBrowserTarget': result = {'sessionId': 'fixture-session'}
                elif method == 'Target.getTargetInfo':
                    result = {'targetInfo': {'targetId': 'fixture', 'type': 'browser',
                                             'title': '', 'url': '', 'attached': True}}
                await self.reply(ws, request, result)
        except ConnectionClosed:
            pass  # Expected only when this fixture's driver is deliberately killed/stopped.
        finally:
            self.clients.discard(ws)

    async def http(self, connection, request):
        from http import HTTPStatus
        if request.path == '/json/version':
            return connection.respond(HTTPStatus.OK, json.dumps({'webSocketDebuggerUrl': self.endpoint}))
        self.upgrades.append(request.path)
        if self.mode == 'redirect':
            response = connection.respond(HTTPStatus.FOUND, 'fixture redirect must be refused')
            response.headers['Location'] = self.endpoint + '/redirected'
            return response

    async def run(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop(); self.stop_future = self.loop.create_future()
        async with serve(self.handle, '127.0.0.1', 0, process_request=self.http) as server:
            self.port = server.sockets[0].getsockname()[1]; self.ready.set()
            await self.stop_future

    @property
    def endpoint(self): return f'ws://127.0.0.1:{self.port}/devtools/browser/fixture'

    def close(self):
        if self.loop is not None and self.stop_future is not None:
            self.loop.call_soon_threadsafe(self.stop_future.set_result, None)
        self.thread.join(5)
        if self.thread.is_alive() or self.errors: raise RuntimeError('Local CDP fixture cleanup failed')


class RealDriverRegression(unittest.TestCase):
    def setUp(self):
        self.assertEqual(importlib.metadata.version('playwright'), '1.62.0')
        from playwright.sync_api import sync_playwright
        from websockets.sync.client import connect
        self.start = sync_playwright
        self.verifier = load_verifier()

    def trial(self, mode):
        fixture = LocalCdpFixture('ack' if mode == 'ack' else 'noack')
        owned = self.verifier.OwnedRecoveryTools()
        timer = None; fired = threading.Event(); disconnected = threading.Event(); observed_before_rescue = []
        try:
            with self.start() as playwright:
                owned.bind_driver(playwright)
                if sys.platform == 'win32':
                    self.assertIsNotNone(owned.driver_handle)
                    self.assertEqual(owned.kernel.GetProcessId(owned.driver_handle), owned.driver.pid)
                browser = playwright.chromium.connect_over_cdp(fixture.endpoint, timeout=2000)
                browser.on('disconnected', lambda _: disconnected.set())
                def rescue():
                    observed_before_rescue.append((fixture.socket_closed.is_set(), disconnected.is_set()))
                    fired.set(); owned.abort()
                timer = threading.Timer(.7, rescue); timer.daemon = True
                started = time.monotonic()
                if mode == 'raw':
                    # This is the exact changed verifier transport, including
                    # HTTP ownership validation and both tracked sockets.
                    process = Mock(pid=10, poll=Mock(return_value=None), returncode=0)
                    with self.verifier.installed_phase(process, started + 3, 2,
                            self.verifier.InstalledRecoveryShutdownTimeout, cleanup=owned.abort) as deadline:
                        self.verifier.request_installed_shutdown(process, fixture.port, deadline, owned)
                        self.assertTrue(fixture.socket_closed.wait(1))
                        with patch.object(subprocess, 'run', return_value=Mock(returncode=0)):
                            self.verifier.finish_installed_shutdown(process, {'pid': 11, 'parent_pid': 10},
                                                                   playwright, deadline, owned)
                    self.assertFalse(owned.interrupted)
                else:
                    session = browser.new_browser_cdp_session(); timer.start()
                    if mode == 'noack':
                        with self.assertRaises(Exception): session.send('Browser.close')
                    else:
                        session.send('Browser.close')
                    timer.cancel(); timer.join(3)
                    self.assertFalse(timer.is_alive())
                    self.assertEqual(fired.is_set(), mode == 'noack')
                    self.assertTrue(fixture.socket_closed.wait(1))
                    if mode == 'noack':
                        self.assertEqual(observed_before_rescue, [(True, True)])
                        self.assertTrue(disconnected.is_set())
                        self.assertGreaterEqual(time.monotonic() - started, .65)
                    playwright.stop()
                self.assertTrue(fixture.close_received.is_set())
                self.assertLess(time.monotonic() - started, 2)
        finally:
            if timer is not None:
                timer.cancel()
                if timer.ident is not None: timer.join(3)
            owned.abort(); owned.release(); fixture.close()

    def test_old_ack_fixture_passes(self): self.trial('ack')
    def test_old_no_ack_fixture_hangs_despite_disconnect_until_exact_driver_cleanup(self): self.trial('noack')
    def test_new_no_ack_fixture_uses_real_driver_and_completes_without_forced_cleanup(self): self.trial('raw')

    def test_actual_websocket_upgrade_redirect_is_rejected_without_following_it(self):
        from websockets.exceptions import InvalidStatus
        fixture = LocalCdpFixture('redirect'); owned = self.verifier.OwnedRecoveryTools()
        try:
            with self.assertRaises((InvalidStatus, ValueError)) as rejected:
                with self.verifier.installed_phase(Mock(), time.monotonic() + 3, 2,
                        self.verifier.InstalledRecoveryShutdownTimeout, cleanup=owned.abort) as deadline:
                    self.verifier.request_installed_shutdown(Mock(poll=Mock(return_value=None)),
                                                            fixture.port, deadline, owned)
            refusal = rejected.exception
            if type(refusal) is ValueError:
                # websockets 17.1 wraps the actual 302 when the retained socket
                # forbids following it. Reject every unrelated ValueError.
                self.assertEqual(str(refusal), 'cannot follow redirect to ' +
                    fixture.endpoint + '/redirected with a preexisting socket')
                refusal = refusal.__cause__
            self.assertIs(type(refusal), InvalidStatus)
            self.assertEqual(refusal.response.status_code, 302)
            self.assertEqual(refusal.response.headers['Location'], fixture.endpoint + '/redirected')
            self.assertEqual(fixture.upgrades, ['/devtools/browser/fixture'])
            self.assertFalse(fixture.close_received.is_set())
        finally: owned.abort(); owned.release(); fixture.close()

    def test_wrong_version_and_driver_binding_fail_closed_without_killing_foreign_process(self):
        with self.start() as playwright:
            owned = self.verifier.OwnedRecoveryTools()
            with patch('importlib.metadata.version', return_value='1.63.0'):
                with self.assertRaisesRegex(RuntimeError, 'Unreviewed Playwright driver version'):
                    owned.bind_driver(playwright)
            self.assertIsNone(owned.driver)
            driver = playwright._impl_obj._connection._transport._proc._transport.get_extra_info('subprocess')
            with patch.object(driver, 'args', ('foreign-node', 'foreign-cli', 'run-driver')):
                with self.assertRaisesRegex(RuntimeError, 'driver identity changed'):
                    owned.bind_driver(playwright)
            self.assertIsNone(owned.driver); self.assertIsNone(driver.poll())
            # Service the initialized connection before stopping its real driver.
            fixture = LocalCdpFixture('noack')
            try: playwright.chromium.connect_over_cdp(fixture.endpoint, timeout=2000)
            finally: playwright.stop(); fixture.close()

    def test_late_valid_driver_registration_after_timeout_is_immediately_terminated(self):
        owned = self.verifier.OwnedRecoveryTools(); owned.abort()
        try:
            with self.start() as playwright:
                with self.assertRaisesRegex(RuntimeError, 'no longer eligible'):
                    owned.bind_driver(playwright)
                self.assertTrue(owned.interrupted)
                self.assertIsNotNone(owned.driver)
        finally: owned.abort(); owned.release()

    def test_driver_stop_inside_shutdown_phase_is_interruptible_and_disqualifies_success(self):
        fixture = LocalCdpFixture('noack'); owned = self.verifier.OwnedRecoveryTools()
        try:
            with self.start() as playwright:
                owned.bind_driver(playwright)
                if sys.platform == 'win32': self.assertIsNotNone(owned.driver_handle)
                playwright.chromium.connect_over_cdp(fixture.endpoint, timeout=2000)
                original_stop = playwright.stop
                def stalled_stop():
                    while not owned.interrupted: time.sleep(.005)
                    original_stop()
                playwright.stop = stalled_stop
                process = Mock(pid=10, returncode=0)
                with self.assertRaises(self.verifier.InstalledRecoveryShutdownTimeout):
                    with self.verifier.installed_phase(process, time.monotonic() + 3, .15,
                            self.verifier.InstalledRecoveryShutdownTimeout, cleanup=owned.abort) as deadline:
                        with patch.object(subprocess, 'run', return_value=Mock(returncode=0)):
                            self.verifier.finish_installed_shutdown(process, {'pid': 11, 'parent_pid': 10},
                                                                   playwright, deadline, owned)
                self.assertTrue(owned.interrupted)
        finally: owned.abort(); owned.release(); fixture.close()


if __name__ == '__main__':
    real = '--real-driver' in sys.argv
    if real: sys.argv.remove('--real-driver')
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RealDriverRegression if real else ShutdownContracts)
    outcome = unittest.TextTestRunner(verbosity=2 if '-v' in sys.argv else 1).run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
