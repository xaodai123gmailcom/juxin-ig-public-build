"""Pure diagnostic/regression mocks; never run real children or Windows APIs."""
from __future__ import annotations
import ast
import importlib.util
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'scripts/tests/test_owned_process_windows_r64.py'
CASE = 'test_parent_exit_and_inherited_output_descendant_cleanup'
VERIFIED_CASE = 'test_verified_parent_exit_and_inherited_output_descendant_cleanup'
VERIFIED_CATEGORIES = {
    'OwnedProcessVerifiedEvidenceRejected',
    'OwnedProcessVerifiedSignalNotObservedInBudget',
}
CATEGORIES = {
    'OwnedProcessWaitTimeout': (258, None),
    'OwnedProcessWaitFailedInvalidHandle': (0xFFFFFFFF, 6),
    'OwnedProcessWaitFailedAccessDenied': (0xFFFFFFFF, 5),
    'OwnedProcessWaitFailedOther': (0xFFFFFFFF, 123456789),
    'OwnedProcessWaitFailedErrorUnavailable': (0xFFFFFFFF, None),
    'OwnedProcessWaitUnexpected': (987654321, None),
}


def diagnostic_scope(module='__main__', capture=None, *, api=None):
    # Compile the exact checked-in definitions without importing or running the
    # native test module, its production owner, or its platform-specific APIs.
    tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
    api = api or SimpleNamespace(OpenProcess=Mock(return_value=object()),
        IsProcessInJob=Mock(side_effect=lambda handle, job, member: setattr(member, 'value', 1) or 1),
        WaitForSingleObject=Mock(return_value=258))
    terminate, active = Mock(), Mock(return_value=0)
    class FakeContainment:
        def __init__(self):
            self.api, self.job, self.process, self.closed = api, object(), object(), False
        def terminate(self):
            terminate()
        def active_processes(self):
            return active()
    owner = SimpleNamespace(WindowsContainment=FakeContainment, BOOL=lambda: SimpleNamespace(value=0),
        DWORD=object, HANDLE=object, cleanup_receipt=Mock(return_value=True),
        supervise=Mock(), terminate=terminate, active=active)
    classes = set(CATEGORIES) | VERIFIED_CATEGORIES | {'CleanupDeadlineContainment', 'VerifiedCleanupContainment'}
    functions = {'raise_held_process_api_failure', 'assert_held_process_signaled',
        'observe_held_process', 'require_held_process_unsignaled',
        'assert_cleanup_deadline', 'assert_held_process_confirmation',
        'assert_verified_cleanup_observation', 'supervise_verified_cleanup'}
    nodes = [node for node in tree.body if
        isinstance(node, ast.ClassDef) and node.name in classes or
        isinstance(node, ast.FunctionDef) and node.name in functions]
    scope = {'__name__': module, 'ctypes': SimpleNamespace(get_last_error=capture, byref=lambda value: value),
             'owned': owner, 'math': math, 'time': SimpleNamespace(monotonic=Mock())}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(NATIVE), 'exec'), scope)
    return scope


def actual_cleanup_predicate():
    tree = ast.parse((ROOT/'scripts/owned_process.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if
        isinstance(node, ast.FunctionDef) and node.name in ('receipt_shape', 'cleanup_receipt') or
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in ('RECEIPT_KEYS', 'OUTCOMES') for target in node.targets)]
    definitions = {'math': math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<pure cleanup predicate>', 'exec'), definitions)
    return definitions['cleanup_receipt']


def complete_receipt():
    return dict(schemaVersion=1, requestId='a'*32, supervisorPid=1, launchTargetPid=2,
        requestedExecutable='private-executable', launchExecutable='private-executable',
        targetExitCode=0, targetExitCodeUnsigned=0, outcome='descendant-drain-timeout',
        budgetLabel='private-budget', executionLimitSeconds=10, elapsedSeconds=1,
        confirmedTreeEmpty=True, errors=[])


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
        self.assertEqual(common.DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES, frozenset(CATEGORIES) | VERIFIED_CATEGORIES)
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
            containment = SimpleNamespace(api=fixture.api, clock=Mock(return_value=1))
            worker = Mock()
            worker.is_alive.return_value = alive
            def thread(*, target):
                worker.start.side_effect = target
                return worker
            scope.update(threading=SimpleNamespace(Thread=thread),
                CleanupDeadlineContainment=Mock(return_value=containment),
                assert_cleanup_deadline=Mock(), assert_held_process_confirmation=Mock(), owned=SimpleNamespace(
                supervise=Mock(return_value={'outcome': outcome, 'confirmedTreeEmpty': empty})))
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(NATIVE), 'exec'), scope)
            with self.subTest(alive=alive, outcome=outcome, empty=empty):
                if alive or outcome != 'descendant-drain-timeout' or not empty:
                    with self.assertRaises(AssertionError):
                        scope[CASE](fixture)
                    wait.assert_not_called()
                    scope['assert_cleanup_deadline'].assert_not_called()
                    scope['assert_held_process_confirmation'].assert_not_called()
                else:
                    scope[CASE](fixture)
                    wait.assert_called_once_with(handle, 0)
                    scope['assert_held_process_confirmation'].assert_called_once_with(containment, handle, None, 1, 1)
                worker.join.assert_called_once_with(15)


class VerifiedCleanupDiagnostics(unittest.TestCase):
    def state(self, *, capture=None):
        scope = diagnostic_scope(capture=capture)
        request = SimpleNamespace(timeoutSeconds=10, drainSeconds=.5, terminationSeconds=2)
        native = scope['VerifiedCleanupContainment'](request, clock=Mock(side_effect=[4.75, 5.75]))
        native.held_child = native.verified_child = object()
        native.verified_job, native.verified_root = native.job, native.process
        native.termination_job, native.termination_root = native.job, native.process
        native.acquired_at, native.acknowledged_at = 1, 2
        native.termination_started_at, native.termination_completed_at = 4, 4.125
        native.termination_deadline = 6
        native.accounting_zero_at, native.accounting_wait_at = 4.25, 4.375
        native.accounting_wait = scope['OwnedProcessWaitTimeout']
        native.termination_calls, native.closed = 1, True
        receipt = {'outcome': 'descendant-drain-timeout', 'targetExitCode': 0}
        return scope, native, receipt

    def classify(self, scope, native, receipt, initial='OwnedProcessWaitTimeout'):
        return scope['assert_verified_cleanup_observation'](
            native, receipt, 4.5, None if initial is None else scope[initial], 4.625)

    def test_verified_initial_timeout_accepts_only_later_in_budget_signal(self):
        scope, native, receipt = self.state()
        native.api.WaitForSingleObject.return_value = 0
        self.classify(scope, native, receipt)
        native.api.WaitForSingleObject.assert_called_once_with(native.held_child, 1250)
        scope['owned'].cleanup_receipt.assert_called_once_with(receipt)

    def test_accounting_and_return_already_signaled_pass_without_later_wait(self):
        scope, native, receipt = self.state()
        native.accounting_wait = None
        self.classify(scope, native, receipt, initial=None)
        native.api.WaitForSingleObject.assert_not_called()
        native.clock.assert_not_called()

    def test_accounting_zero_timeout_then_in_budget_return_signal_passes(self):
        scope, native, receipt = self.state()
        self.classify(scope, native, receipt, initial=None)
        native.api.WaitForSingleObject.assert_not_called()
        native.clock.assert_not_called()

    def test_initial_signal_deadline_boundary_and_slow_termination_fail_closed(self):
        for signaled_at in (5.9999, 6, 6.0001):
            scope, native, receipt = self.state()
            with self.subTest(signaled_at=signaled_at):
                if signaled_at <= 6:
                    scope['assert_verified_cleanup_observation'](native, receipt, 4.5, None, signaled_at)
                else:
                    with self.assertRaises(scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
                        scope['assert_verified_cleanup_observation'](native, receipt, 4.5, None, signaled_at)
            native.api.WaitForSingleObject.assert_not_called()
        for accounting in (None, 'OwnedProcessWaitTimeout'):
            scope, native, receipt = self.state()
            native.termination_completed_at = 6.125
            native.accounting_zero_at, native.accounting_wait_at = 6.25, 6.375
            native.accounting_wait = None if accounting is None else scope[accounting]
            with self.subTest(accounting=accounting), self.assertRaises(scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
                scope['assert_verified_cleanup_observation'](native, receipt, 6.5, None, 6.625)
            native.api.WaitForSingleObject.assert_not_called()

    def test_in_budget_accounting_signal_survives_late_return_bookkeeping(self):
        scope, native, receipt = self.state()
        native.accounting_wait = None
        scope['assert_verified_cleanup_observation'](native, receipt, 7, None, 7.125)
        native.api.WaitForSingleObject.assert_not_called()

    def test_remaining_budget_timeout_never_passes(self):
        scope, native, receipt = self.state()
        with self.assertRaises(scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
            self.classify(scope, native, receipt)
        native.api.WaitForSingleObject.assert_called_once_with(native.held_child, 1250)

    def test_expired_and_submillisecond_remainder_do_not_add_wait_time(self):
        for now in (5.9995, 6, 8):
            scope, native, receipt = self.state()
            native.clock.side_effect = [now]
            with self.assertRaises(scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
                self.classify(scope, native, receipt)
            native.api.WaitForSingleObject.assert_not_called()

    def test_wait_rounding_and_late_signal_cannot_claim_within_deadline(self):
        for late_wait, observed_at in ((258, 5.9998), (0, 6.0001), (258, 6.1)):
            scope, native, receipt = self.state()
            native.api.WaitForSingleObject.return_value = late_wait
            native.clock.side_effect = [5.9985, observed_at]
            with self.assertRaises(scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
                self.classify(scope, native, receipt)
            native.api.WaitForSingleObject.assert_called_once_with(native.held_child, 1)

    def test_every_required_evidence_field_rejects_missing_or_inconsistent_values(self):
        mutations = {
            'held_child': (None, object()), 'verified_child': (None, object()),
            'verified_job': (None, 0, object()),
            'verified_root': (None, 0, object()), 'closed': (False, 1),
            'termination_job': (None, 0, object()), 'termination_root': (None, 0, object()),
            'termination_calls': (0, 2, True), 'termination_deadline': (None, float('nan'), 7),
            'acquired_at': (None, float('nan'), True, 3),
            'acknowledged_at': (None, 0, 5), 'termination_started_at': (None, 1),
            'termination_completed_at': (None, 3, 5), 'accounting_zero_at': (None, 3, 5),
            'accounting_wait_at': (None, 3, 5), 'accounting_wait': (None,),
        }
        for field, values in mutations.items():
            for value in values:
                scope, native, receipt = self.state()
                setattr(native, field, value)
                with self.subTest(field=field, value=value), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                    self.classify(scope, native, receipt)
                native.api.WaitForSingleObject.assert_not_called()

    def test_receipt_and_request_bounds_are_required_before_confirmation(self):
        for field, value in (('outcome', 'completed'), ('targetExitCode', 23), ('cleanup', False),
                ('timeoutSeconds', 11), ('drainSeconds', 1), ('terminationSeconds', 3)):
            scope, native, receipt = self.state()
            if field == 'cleanup':
                scope['owned'].cleanup_receipt.return_value = value
            elif field in receipt:
                receipt[field] = value
            else:
                setattr(native.request, field, value)
            with self.subTest(field=field), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                self.classify(scope, native, receipt)
            native.api.WaitForSingleObject.assert_not_called()

    def test_invalid_or_reversed_clocks_cannot_forge_timing_categories(self):
        for sequence in ([None], [True], [float('nan')], [4.5], [4.75, None],
                [4.75, float('inf')], [4.75, 4.5]):
            scope, native, receipt = self.state()
            native.clock.side_effect = sequence
            with self.subTest(sequence=sequence), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                self.classify(scope, native, receipt)

    def test_failed_waits_keep_the_fixed_api_category_at_every_observation(self):
        for name, (value, error) in CATEGORIES.items():
            if name == 'OwnedProcessWaitTimeout':
                continue
            for phase in ('accounting', 'initial', 'late'):
                scope, native, receipt = self.state(capture=Mock(return_value=error))
                initial = 'OwnedProcessWaitTimeout'
                if phase == 'accounting':
                    native.accounting_wait = scope[name]
                elif phase == 'initial':
                    initial = name
                else:
                    native.api.WaitForSingleObject.return_value = value
                with self.subTest(name=name, phase=phase), self.assertRaises(scope[name]):
                    self.classify(scope, native, receipt, initial)

    def test_acquisition_uses_exact_private_job_and_required_query_rights(self):
        scope = diagnostic_scope()
        native = scope['VerifiedCleanupContainment'](SimpleNamespace(), clock=Mock(return_value=1))
        handles = []
        native.acquire_child(123, handles)
        handle = native.api.OpenProcess.return_value
        self.assertEqual(handles, [handle])
        native.api.OpenProcess.assert_called_once_with(0x00101000, False, 123)
        self.assertIs(native.api.OpenProcess.restype, scope['owned'].HANDLE)
        self.assertEqual(native.api.OpenProcess.argtypes,
            [scope['owned'].DWORD, scope['owned'].BOOL, scope['owned'].DWORD])
        member_call = native.api.IsProcessInJob.call_args.args
        self.assertIs(member_call[0], handle)
        self.assertIs(member_call[1], native.job)
        self.assertEqual(native.api.WaitForSingleObject.call_args_list,
            [((handle, 0),), ((native.process, 0),)])
        self.assertIs(native.held_child, handle)
        self.assertIs(native.verified_child, handle)
        self.assertIs(native.verified_job, native.job)
        self.assertIs(native.verified_root, native.process)
        self.assertEqual(native.acquired_at, 1)

    def test_acquisition_rejects_missing_closed_job_invalid_pid_and_nonmember(self):
        for field, value in (('job', None), ('job', 0), ('process', None), ('closed', True),
                ('pid', True), ('pid', 0), ('pid', '123'), ('member', False), ('termination_calls', 1)):
            scope = diagnostic_scope()
            native = scope['VerifiedCleanupContainment'](SimpleNamespace())
            pid = value if field == 'pid' else 123
            if field == 'member':
                native.api.IsProcessInJob.side_effect = lambda handle, job, member: 1
            elif field != 'pid':
                setattr(native, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                native.acquire_child(pid, [])
            self.assertIsNone(native.held_child)
            self.assertIsNone(native.acquired_at)
            if field in ('job', 'process', 'closed', 'pid'):
                native.api.OpenProcess.assert_not_called()

    def test_acquisition_api_failures_reject_evidence_without_claiming_a_wait_failure(self):
        for operation in ('open', 'membership'):
            events = []
            capture = Mock(side_effect=lambda: events.append('capture') or 5)
            scope = diagnostic_scope(capture=capture)
            native = scope['VerifiedCleanupContainment'](SimpleNamespace())
            if operation == 'open':
                native.api.OpenProcess.side_effect = lambda *args: events.append('open') or 0
            else:
                native.api.IsProcessInJob.side_effect = lambda *args: events.append('membership') or 0
            handles = []
            with self.subTest(operation=operation), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                native.acquire_child(123, handles)
            self.assertEqual(events, [operation])
            capture.assert_not_called()
            self.assertEqual(len(handles), int(operation == 'membership'))
            native.api.WaitForSingleObject.assert_not_called()
            self.assertIsNone(native.held_child)

    def test_child_and_parent_must_be_unsignaled_before_acknowledgment(self):
        for observations, expected in (([0], 'OwnedProcessVerifiedEvidenceRejected'),
                ([258, 0], 'OwnedProcessVerifiedEvidenceRejected'),
                ([0xFFFFFFFF], 'OwnedProcessWaitFailedInvalidHandle'),
                ([258, 0xFFFFFFFF], 'OwnedProcessWaitFailedInvalidHandle')):
            scope = diagnostic_scope(capture=Mock(return_value=6))
            native = scope['VerifiedCleanupContainment'](SimpleNamespace())
            native.api.WaitForSingleObject.side_effect = observations
            with self.subTest(observations=observations), self.assertRaises(scope[expected]):
                native.acquire_child(123, [])
            self.assertIsNone(native.acquired_at)

    def test_actual_complete_cleanup_receipt_predicate_is_required(self):
        predicate, good = actual_cleanup_predicate(), complete_receipt()
        self.assertTrue(predicate(good))
        bad_receipts = [dict((key, value) for key, value in good.items() if key != removed) for removed in good]
        bad_receipts.extend(dict(good, **change) for change in (
            {'errors': ['private-cleanup-error']}, {'confirmedTreeEmpty': False},
            {'confirmedTreeEmpty': 1}, {'launchTargetPid': None}, {'launchTargetPid': 1},
            {'launchExecutable': ''}, {'targetExitCode': False}, {'targetExitCodeUnsigned': 1}))
        for bad in bad_receipts:
            scope, native, _ = self.state()
            scope['owned'].cleanup_receipt = predicate
            with self.subTest(fields=tuple(bad)), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                self.classify(scope, native, bad)
            native.api.WaitForSingleObject.assert_not_called()

    def test_acknowledgment_requires_still_live_exact_scope(self):
        for field, value in (('held_child', None), ('held_child', object()), ('verified_child', object()), ('verified_job', object()),
                ('verified_root', object()), ('termination_calls', 1), ('closed', True), ('root_wait', 0)):
            scope = diagnostic_scope()
            native = scope['VerifiedCleanupContainment'](SimpleNamespace(), clock=Mock(return_value=1))
            native.acquire_child(123, [])
            if field == 'root_wait':
                native.api.WaitForSingleObject.return_value = value
            else:
                setattr(native, field, value)
            acknowledgment = Mock()
            with self.subTest(field=field), self.assertRaises(scope['OwnedProcessVerifiedEvidenceRejected']):
                native.acknowledge(acknowledgment)
            acknowledgment.write_text.assert_not_called()
            self.assertIsNone(native.acknowledged_at)

    def test_termination_deadline_is_anchored_before_call_and_zero_is_sampled_once(self):
        scope = diagnostic_scope()
        native = scope['VerifiedCleanupContainment'](SimpleNamespace(terminationSeconds=2),
            clock=Mock(side_effect=[4, 4.125, 4.25, 4.375]))
        native.held_child = object()
        def terminate():
            self.assertEqual(native.termination_started_at, 4)
            self.assertEqual(native.termination_deadline, 6)
            self.assertIsNone(native.termination_completed_at)
        scope['owned'].terminate.side_effect = terminate
        self.assertEqual(native.active_processes(), 0)
        native.api.WaitForSingleObject.assert_not_called()
        native.terminate()
        scope['owned'].active.side_effect = [1, 0, 0]
        self.assertEqual([native.active_processes() for _ in range(3)], [1, 0, 0])
        native.api.WaitForSingleObject.assert_called_once_with(native.held_child, 0)
        self.assertIs(native.accounting_wait, scope['OwnedProcessWaitTimeout'])
        self.assertEqual((native.accounting_zero_at, native.accounting_wait_at), (4.25, 4.375))

    def test_termination_failure_does_not_create_post_termination_zero_evidence(self):
        scope = diagnostic_scope()
        native = scope['VerifiedCleanupContainment'](SimpleNamespace(terminationSeconds=2), clock=Mock(return_value=4))
        scope['owned'].terminate.side_effect = RuntimeError('private-termination-error')
        with self.assertRaises(RuntimeError):
            native.terminate()
        self.assertEqual(native.active_processes(), 0)
        self.assertIsNone(native.accounting_zero_at)
        self.assertIsNone(native.termination_completed_at)
        native.api.WaitForSingleObject.assert_not_called()

    def test_return_wait_is_captured_once_in_worker_before_diagnostic_wait(self):
        scope, native, receipt = self.state()
        native.clock.side_effect = [4.5, 4.625, 4.75, 5.75]
        native.api.WaitForSingleObject.side_effect = [258, 0]
        scope['owned'].supervise.return_value = receipt
        cancelled = object()
        scope['supervise_verified_cleanup'](native.request, native, cancelled)
        scope['owned'].supervise.assert_called_once_with(native.request, containment=native, cancelled=cancelled)
        self.assertEqual(native.api.WaitForSingleObject.call_args_list,
            [((native.held_child, 0),), ((native.held_child, 1250),)])

    def test_worker_bounds_join_before_close_and_propagate_original_failure(self):
        tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
        native_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnership')
        method = next(node for node in native_class.body if isinstance(node, ast.FunctionDef) and node.name == VERIFIED_CASE)
        for scenario in ('pass', 'failure', 'acquisition-failure', 'ack-failure', 'alive', 'no-result'):
            scope = diagnostic_scope()
            fixture = unittest.TestCase()
            fixture.folder, fixture.api = Path('/private-fixture'), SimpleNamespace(CloseHandle=Mock())
            fixture.request, fixture.wait_json = Mock(return_value=object()), Mock(return_value=123)
            native = Mock(acknowledged_at=None)
            handle, events = object(), []
            native.acquire_child.side_effect = lambda pid, handles: handles.append(handle)
            native.acknowledge.side_effect = lambda acknowledgment: setattr(native, 'acknowledged_at', 2)
            failure = scope['OwnedProcessVerifiedSignalNotObservedInBudget']()
            supervise = Mock(side_effect=failure if scenario == 'failure' else None)
            if scenario == 'acquisition-failure':
                native.acquire_child.side_effect = scope['OwnedProcessVerifiedEvidenceRejected']()
            if scenario == 'ack-failure':
                native.acknowledge.side_effect = AssertionError('private-ack-error')
            worker, cancelled = Mock(), Mock()
            worker.is_alive.return_value = scenario == 'alive'
            def thread(*, target, daemon):
                self.assertIs(daemon, True)
                def join(seconds):
                    events.append('join')
                    if scenario not in ('alive', 'no-result'):
                        target()
                worker.join.side_effect = join
                return worker
            fixture.api.CloseHandle.side_effect = lambda value: events.append('close')
            scope.update(threading=SimpleNamespace(Thread=thread, Event=Mock(return_value=cancelled)),
                VerifiedCleanupContainment=Mock(return_value=native), supervise_verified_cleanup=supervise)
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(NATIVE), 'exec'), scope)
            with self.subTest(scenario=scenario):
                if scenario == 'pass':
                    scope[VERIFIED_CASE](fixture)
                else:
                    with self.assertRaises(AssertionError) as caught:
                        scope[VERIFIED_CASE](fixture)
                    if scenario == 'failure':
                        self.assertIs(caught.exception, failure)
                worker.join.assert_called_once_with(15)
                if scenario in ('alive', 'acquisition-failure'):
                    fixture.api.CloseHandle.assert_not_called()
                else:
                    fixture.api.CloseHandle.assert_called_once_with(handle)
                    self.assertEqual(events, ['join', 'close'])
                self.assertEqual(cancelled.set.call_count, int(scenario in ('acquisition-failure', 'ack-failure')))
                args = fixture.request.call_args.args[0]
                self.assertEqual(args[0], '-c')
                self.assertIn('p=subprocess.Popen', args[1])
                self.assertIn('deadline=time.monotonic()+5', args[1])
                self.assertIn('if not ack.exists(): sys.exit(124)', args[1])
                self.assertTrue(args[1].endswith('time.sleep(.5)'))

    def test_parent_protocol_retains_popen_until_ack_and_exits_on_bounded_missing_ack(self):
        tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
        native_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnership')
        method = next(node for node in native_class.body if isinstance(node, ast.FunctionDef) and node.name == VERIFIED_CASE)
        assignment = next(node for node in method.body if isinstance(node, ast.Assign) and
            any(isinstance(target, ast.Name) and target.id == 'script' for target in node.targets))
        definition = {}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<fixture script definition>', 'exec'), definition)
        script_tree = ast.parse(definition['script'])
        # Execute the actual protocol with all process, path, time and exit calls
        # replaced. No module import, filesystem write, sleeper or native API runs.
        script_tree.body = [node for node in script_tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        for acknowledges in (True, False):
            now, sleeps = [0.0], []
            child = SimpleNamespace(pid=123)
            process = Mock(return_value=child)
            proof = Mock()
            ack = SimpleNamespace(exists=lambda: acknowledges and now[0] >= .03)
            def sleep(seconds):
                self.assertIs(protocol['p'], child)
                sleeps.append(seconds)
                now[0] += seconds
            protocol = {'subprocess': SimpleNamespace(Popen=process),
                'sys': SimpleNamespace(executable='fixture-python', argv=['fixture', 'proof', 'ack'],
                    exit=Mock(side_effect=SystemExit)),
                'time': SimpleNamespace(monotonic=lambda: now[0], sleep=sleep), 'json': json,
                'Path': lambda name: proof if name == 'proof' else ack}
            with self.subTest(acknowledges=acknowledges):
                if acknowledges:
                    exec(compile(script_tree, '<mock fixture protocol>', 'exec'), protocol)
                    self.assertEqual(sleeps[-1], .5)
                    self.assertLess(now[0], 1)
                else:
                    with self.assertRaises(SystemExit):
                        exec(compile(script_tree, '<mock fixture protocol>', 'exec'), protocol)
                    protocol['sys'].exit.assert_called_once_with(124)
                    self.assertNotIn(.5, sleeps)
                    self.assertGreaterEqual(now[0], 5)
                    self.assertLess(now[0], 5.02)
                self.assertIs(protocol['p'], child)
                process.assert_called_once_with(['fixture-python', '-c', 'import time;time.sleep(60)'])
                proof.write_text.assert_called_once_with('123')

    def test_regression_categories_format_as_failures_and_export_no_private_values(self):
        spec = importlib.util.spec_from_file_location('verified_diagnostic_common', ROOT / 'ci/public_ci_common.py')
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        self.assertEqual(len(common.DIAGNOSTIC_EXCEPTION_CATEGORIES), 17)
        manifest = {'scripts/tests/test_owned_process_windows_r64.py': 'a' * 64}
        for module in ('__main__', 'test_owned_process_windows_r64', 'scripts.tests.test_owned_process_windows_r64'):
            for name in VERIFIED_CATEGORIES:
                scope, native, receipt = self.state()
                scope[name].__module__ = module
                if name == 'OwnedProcessVerifiedEvidenceRejected':
                    native.verified_job = None
                def fixture(case):
                    self.classify(scope, native, receipt)
                case = type('MockedVerifiedCleanup', (unittest.TestCase,), {VERIFIED_CASE: fixture})
                stream = io.StringIO()
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(case(VERIFIED_CASE))
                self.assertEqual((len(result.failures), len(result.errors)), (1, 0))
                value = common.parse_diagnostic_tails([stream.getvalue()], manifest, ROOT)
                self.assertEqual(value['observed_exception_categories'], [name])
                self.assertEqual(value['failed_test_ids'], [VERIFIED_CASE])
                public = json.dumps(value)
                for private in ('private', '1250', '4.75', '5.75', str(ROOT)):
                    self.assertNotIn(private, public)


class OriginalCleanupRegression(unittest.TestCase):
    def fixture(self, *, clocks=(4, 4.125, 4.5, 4.625, 4.75, 5.75), waits=(258, 0),
                receipt_changes=None, native_changes=None, terminate=True, missing_receipt_field=None):
        scope = diagnostic_scope(capture=Mock(return_value=6))
        scope['time'].monotonic.side_effect = clocks
        scope['owned'].cleanup_receipt = actual_cleanup_predicate()
        request = SimpleNamespace(timeoutSeconds=10, drainSeconds=.5, terminationSeconds=2)
        receipt = complete_receipt()
        receipt.update(receipt_changes or {})
        if missing_receipt_field is not None:
            del receipt[missing_receipt_field]
        native_instances, events = [], []
        def supervise(actual_request, *, containment):
            self.assertIs(actual_request, request)
            self.assertIs(type(containment), scope['CleanupDeadlineContainment'])
            native_instances.append(containment)
            if terminate:
                containment.terminate()
            containment.closed = True
            for name, value in (native_changes or {}).items():
                setattr(containment, name, value)
            return receipt
        scope['owned'].supervise.side_effect = supervise
        def terminating():
            native = native_instances[0]
            self.assertEqual(native.termination_started_at, 4)
            self.assertEqual(native.termination_deadline, 6)
            self.assertIs(native.termination_job, native.job)
            self.assertIs(native.termination_root, native.process)
            self.assertIsNone(native.termination_completed_at)
            events.append('terminate')
        scope['owned'].terminate.side_effect = terminating
        worker = Mock()
        worker.is_alive.return_value = False
        worker.join.side_effect = lambda seconds: events.append('join')
        def thread(*, target):
            worker.start.side_effect = target
            return worker
        scope['threading'] = SimpleNamespace(Thread=thread)
        case, handle = unittest.TestCase(), object()
        case.folder, case.request = Path('/private-fixture'), Mock(return_value=request)
        case.wait_json, case.held_process = Mock(return_value=123), Mock(return_value=handle)
        tree = ast.parse(NATIVE.read_text(encoding='utf-8'))
        native_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeWindowsOwnership')
        method = next(node for node in native_class.body if isinstance(node, ast.FunctionDef) and node.name == CASE)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(NATIVE), 'exec'), scope)
        api = scope['CleanupDeadlineContainment'](request).api
        # Every wait must use the one held handle, after the unchanged worker join.
        responses = iter(waits)
        def wait(actual, milliseconds):
            self.assertIs(actual, handle)
            self.assertIn('join', events)
            return next(responses)
        api.WaitForSingleObject.side_effect = wait
        return SimpleNamespace(run=lambda: scope[CASE](case), scope=scope, api=api,
            worker=worker, case=case, handle=handle, instances=native_instances, events=events)

    def test_original_whole_method_uses_same_handle_and_remaining_cleanup_budget(self):
        fixture = self.fixture()
        fixture.run()
        fixture.worker.join.assert_called_once_with(15)
        fixture.case.held_process.assert_called_once_with(123)
        self.assertEqual(fixture.api.WaitForSingleObject.call_args_list,
            [((fixture.handle, 0),), ((fixture.handle, 1250),)])
        self.assertEqual(fixture.events, ['terminate', 'join'])

    def test_original_initial_or_final_signal_exactly_at_deadline_is_accepted(self):
        for clocks, waits in (((4, 4.125, 5.875, 6), (0,)),
                ((4, 4.125, 4.5, 4.625, 4.75, 6), (258, 0))):
            with self.subTest(clocks=clocks):
                fixture = self.fixture(clocks=clocks, waits=waits)
                fixture.run()
                self.assertEqual(fixture.api.WaitForSingleObject.call_count, len(waits))

    def test_original_slow_terminate_consumes_the_same_two_second_budget(self):
        fixture = self.fixture(clocks=(4, 5.5, 5.625, 5.75, 5.875, 6))
        fixture.run()
        self.assertEqual(fixture.api.WaitForSingleObject.call_args_list,
            [((fixture.handle, 0),), ((fixture.handle, 125),)])
        fixture = self.fixture(clocks=(4, 6.125, 6.25, 6.375), waits=(0,))
        with self.assertRaises(fixture.scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
            fixture.run()
        fixture.api.WaitForSingleObject.assert_called_once_with(fixture.handle, 0)

    def test_original_expired_join_late_signal_and_insufficient_residual_fail(self):
        for clocks, waits in (((4, 4.125, 6.125, 6.25), (0,)),
                ((4, 4.125, 6.125, 6.25, 6.375), (258,)),
                ((4, 4.125, 5.75, 5.875, 6), (258,)),
                ((4, 4.125, 5.75, 5.875, 5.9995), (258,)),
                ((4, 4.125, 4.5, 4.625, 4.75, 6.0001), (258, 0))):
            fixture = self.fixture(clocks=clocks, waits=waits)
            with self.subTest(clocks=clocks), self.assertRaises(fixture.scope['OwnedProcessVerifiedSignalNotObservedInBudget']):
                fixture.run()
            self.assertEqual(fixture.api.WaitForSingleObject.call_count, len(waits))

    def test_original_missing_signal_and_failed_or_unexpected_waits_never_pass(self):
        for waits, expected in (((258, 258), 'OwnedProcessVerifiedSignalNotObservedInBudget'),
                ((0xFFFFFFFF,), 'OwnedProcessWaitFailedInvalidHandle'),
                ((258, 0xFFFFFFFF), 'OwnedProcessWaitFailedInvalidHandle'),
                ((128,), 'OwnedProcessWaitUnexpected'), ((258, 128), 'OwnedProcessWaitUnexpected')):
            clocks = (4, 4.125, 4.5, 4.625) if len(waits) == 1 else (4, 4.125, 4.5, 4.625, 4.75, 5.75)
            fixture = self.fixture(clocks=clocks, waits=waits)
            with self.subTest(waits=waits), self.assertRaises(fixture.scope[expected]):
                fixture.run()
            self.assertEqual(fixture.api.WaitForSingleObject.call_count, len(waits))

    def test_original_requires_one_termination_and_the_same_private_scope(self):
        for changes in ({'termination_calls': 0}, {'termination_calls': 2}, {'termination_calls': True},
                {'termination_deadline': None}, {'termination_deadline': 7}, {'closed': False},
                {'termination_job': None}, {'termination_job': object()},
                {'termination_root': None}, {'termination_root': object()},
                {'request': SimpleNamespace(timeoutSeconds=10, drainSeconds=.5, terminationSeconds=3)}):
            fixture = self.fixture(native_changes=changes)
            with self.subTest(changes=changes), self.assertRaises(fixture.scope['OwnedProcessVerifiedEvidenceRejected']):
                fixture.run()
            fixture.api.WaitForSingleObject.assert_not_called()
        fixture = self.fixture(terminate=False, clocks=(4.5,))
        with self.assertRaises(fixture.scope['OwnedProcessVerifiedEvidenceRejected']):
            fixture.run()
        fixture.api.WaitForSingleObject.assert_not_called()
        fixture.scope['owned'].terminate.assert_not_called()

    def test_original_receipt_failures_block_all_held_handle_waits(self):
        for changes in ({'outcome': 'completed'}, {'confirmedTreeEmpty': False},
                {'targetExitCode': 23, 'targetExitCodeUnsigned': 23}, {'errors': ['private-error']},
                {'confirmedTreeEmpty': 1}, {'launchTargetPid': None}, {'launchExecutable': ''}):
            fixture = self.fixture(receipt_changes=changes)
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                fixture.run()
            fixture.api.WaitForSingleObject.assert_not_called()
            fixture.worker.join.assert_called_once_with(15)
        for field in complete_receipt():
            fixture = self.fixture(missing_receipt_field=field)
            with self.subTest(missing=field), self.assertRaises((AssertionError, KeyError)):
                fixture.run()
            fixture.api.WaitForSingleObject.assert_not_called()

    def test_original_backward_or_nonfinite_observation_clock_never_passes(self):
        for clocks in ((4, 3, 4.5), (4, 4.125, None), (4, 4.125, float('nan')),
                (4, 4.125, 4.5, 4.25), (4, 4.125, 4.5, float('inf')),
                (4, 4.125, 4.5, 4.625, 4.5), (4, 4.125, 4.5, 4.625, float('nan')),
                (4, 4.125, 4.5, 4.625, 4.75, 4.5), (4, 4.125, 4.5, 4.625, 4.75, None)):
            fixture = self.fixture(clocks=clocks)
            with self.subTest(clocks=clocks), self.assertRaises(fixture.scope['OwnedProcessVerifiedEvidenceRejected']):
                fixture.run()


if __name__ == '__main__':
    unittest.main()
