"""Pure mocked diagnostics; never execute the native fixture or Windows APIs."""
from __future__ import annotations
import ast
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'scripts/tests/test_owned_process_windows_r64.py'
CASE = 'test_parent_exit_and_inherited_output_descendant_cleanup'
CATEGORIES = {
    'OwnedProcessWaitTimeout': (258, None),
    'OwnedProcessWaitFailedInvalidHandle': (0xFFFFFFFF, 6),
    'OwnedProcessWaitFailedAccessDenied': (0xFFFFFFFF, 5),
    'OwnedProcessWaitFailedOther': (0xFFFFFFFF, 123456789),
    'OwnedProcessWaitFailedErrorUnavailable': (0xFFFFFFFF, None),
    'OwnedProcessWaitUnexpected': (987654321, None),
}


def diagnostic_scope(module='__main__', capture=None):
    # Compile the exact checked-in definitions without importing or running the
    # native test module, its production owner, or its platform-specific APIs.
    tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if
        isinstance(node, ast.ClassDef) and node.name in CATEGORIES or
        isinstance(node, ast.FunctionDef) and node.name == 'assert_held_process_signaled']
    scope = {'__name__': module, 'ctypes': SimpleNamespace(get_last_error=capture)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(NATIVE), 'exec'), scope)
    return scope


class WaitDiagnostics(unittest.TestCase):
    def test_only_signaled_returns_success_without_reading_last_error(self):
        capture = Mock(side_effect=RuntimeError('private-capture-error'))
        scope = diagnostic_scope(capture=capture)
        handle = object()
        api = SimpleNamespace(WaitForSingleObject=Mock(return_value=0))
        self.assertIsNone(scope['assert_held_process_signaled'](api, handle))
        api.WaitForSingleObject.assert_called_once_with(handle, 0)
        capture.assert_not_called()

    def test_every_fixed_category_remains_an_assertion_failure_without_values(self):
        for name, (result, error) in CATEGORIES.items():
            with self.subTest(category=name):
                capture = Mock(return_value=error)
                scope = diagnostic_scope(capture=capture)
                handle = object()
                api = SimpleNamespace(WaitForSingleObject=Mock(return_value=result))
                self.assertTrue(issubclass(scope[name], AssertionError))
                with self.assertRaises(scope[name]) as caught:
                    scope['assert_held_process_signaled'](api, handle)
                self.assertEqual(caught.exception.args, ())
                self.assertEqual(str(caught.exception), '')
                api.WaitForSingleObject.assert_called_once_with(handle, 0)
                self.assertEqual(capture.call_count, int(result == 0xFFFFFFFF))

    def test_unexpected_nonzero_or_malformed_wait_values_cannot_pass(self):
        for value in (-1, 1, 128, 259, 987654321, None, True, 'private-wait-value'):
            with self.subTest(value=value):
                capture = Mock(side_effect=RuntimeError('must not capture'))
                scope = diagnostic_scope(capture=capture)
                api = SimpleNamespace(WaitForSingleObject=Mock(return_value=value))
                with self.assertRaises(scope['OwnedProcessWaitUnexpected']):
                    scope['assert_held_process_signaled'](api, object())
                capture.assert_not_called()
                self.assertEqual(api.WaitForSingleObject.call_count, 1)

    def test_failed_wait_captures_thread_last_error_immediately_once(self):
        events = []
        handle = object()
        def wait(actual_handle, timeout):
            self.assertIs(actual_handle, handle)
            self.assertEqual(timeout, 0)
            events.append('wait')
            return 0xFFFFFFFF
        def capture():
            self.assertEqual(events, ['wait'])
            events.append('capture')
            return 6
        scope = diagnostic_scope(capture=capture)
        with self.assertRaises(scope['OwnedProcessWaitFailedInvalidHandle']):
            scope['assert_held_process_signaled'](SimpleNamespace(WaitForSingleObject=wait), handle)
        self.assertEqual(events, ['wait', 'capture'])

    def test_missing_or_raising_last_error_capture_cannot_hide_failure(self):
        for capture in (None, Mock(side_effect=RuntimeError('private-capture-error'))):
            scope = diagnostic_scope(capture=capture)
            if capture is None:
                del scope['ctypes'].get_last_error
            with self.assertRaises(scope['OwnedProcessWaitFailedErrorUnavailable']) as caught:
                scope['assert_held_process_signaled'](
                    SimpleNamespace(WaitForSingleObject=Mock(return_value=0xFFFFFFFF)), object())
            self.assertEqual(caught.exception.args, ())
            self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_last_error_capture_is_unavailable_and_other_numbers_still_fail(self):
        for value in (None, True, 'private-error-value', 0, -1, 123456789):
            scope = diagnostic_scope(capture=Mock(return_value=value))
            name = 'OwnedProcessWaitFailedOther' if type(value) is int else 'OwnedProcessWaitFailedErrorUnavailable'
            with self.assertRaises(scope[name]):
                scope['assert_held_process_signaled'](
                    SimpleNamespace(WaitForSingleObject=Mock(return_value=0xFFFFFFFF)), object())

    def test_actual_unittest_formatting_exports_only_fixed_categories(self):
        spec = importlib.util.spec_from_file_location('wait_diagnostic_common', ROOT / 'ci/public_ci_common.py')
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        self.assertEqual(common.DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES, frozenset(CATEGORIES))
        manifest = {'scripts/tests/test_owned_process_windows_r64.py': 'a' * 64}
        for module in ('__main__', 'test_owned_process_windows_r64', 'scripts.tests.test_owned_process_windows_r64'):
            for name, (result, error) in CATEGORIES.items():
                with self.subTest(module=module, category=name):
                    capture = Mock(return_value=error)
                    if name == 'OwnedProcessWaitFailedErrorUnavailable':
                        capture.side_effect = RuntimeError('private-capture-error')
                    scope = diagnostic_scope(module, capture)
                    api = SimpleNamespace(WaitForSingleObject=Mock(return_value=result))
                    def fixture(self):
                        scope['assert_held_process_signaled'](api, 'private-held-handle')
                    case = type('MockedNativeWait', (unittest.TestCase,), {CASE: fixture})
                    stream = io.StringIO()
                    outcome = unittest.TextTestRunner(stream=stream, verbosity=2).run(case(CASE))
                    self.assertFalse(outcome.wasSuccessful())
                    self.assertEqual((len(outcome.failures), len(outcome.errors)), (1, 0))
                    tail = stream.getvalue()
                    rendered = name if module == '__main__' else module + '.' + name
                    self.assertIn('\n' + rendered + '\n', tail)
                    self.assertNotIn('private-capture-error', tail)
                    value = common.parse_diagnostic_tails([tail], manifest, ROOT)
                    self.assertEqual(value['observed_exception_categories'], [name])
                    self.assertEqual(value['failed_test_ids'], [CASE])
                    public = json.dumps(value)
                    for private in ('private', '123456789', '987654321', '4294967295', str(ROOT)):
                        self.assertNotIn(private, public)

    def test_receipt_assertions_and_original_join_bound_precede_signal_assertion(self):
        tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
        native = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnership')
        method = next(node for node in native.body if isinstance(node, ast.FunctionDef) and node.name == CASE)
        for alive, outcome, empty in ((False, 'descendant-drain-timeout', True),
                (True, 'descendant-drain-timeout', True), (False, 'completed', True),
                (False, 'descendant-drain-timeout', False)):
            scope = diagnostic_scope(capture=Mock())
            wait = Mock(return_value=0)
            fixture = unittest.TestCase()
            fixture.folder = Path('/private-fixture')
            fixture.api = SimpleNamespace(WaitForSingleObject=wait)
            fixture.request = Mock(return_value=object())
            fixture.wait_json = Mock(return_value=123456789)
            handle = object()
            fixture.held_process = Mock(return_value=handle)
            worker = Mock()
            worker.is_alive.return_value = alive
            def thread(*, target):
                worker.start.side_effect = target
                return worker
            scope.update(threading=SimpleNamespace(Thread=thread), owned=SimpleNamespace(
                supervise=Mock(return_value={'outcome': outcome, 'confirmedTreeEmpty': empty})))
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(NATIVE), 'exec'), scope)
            with self.subTest(alive=alive, outcome=outcome, empty=empty):
                if alive or outcome != 'descendant-drain-timeout' or not empty:
                    with self.assertRaises(AssertionError):
                        scope[CASE](fixture)
                    wait.assert_not_called()
                else:
                    scope[CASE](fixture)
                    wait.assert_called_once_with(handle, 0)
                worker.join.assert_called_once_with(15)


if __name__ == '__main__':
    unittest.main()
