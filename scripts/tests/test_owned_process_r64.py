"""Executable Windows-API simulations, not native Windows acceptance."""
from __future__ import annotations
import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'owned_process.py'
spec = importlib.util.spec_from_file_location('r64_owned_process', PATH)
owned = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owned
spec.loader.exec_module(owned)


def request(**changes):
    values = dict(executable=r'C:\实验室\python.exe', arguments=['fixture.py'], workingDirectory=r'C:\实验室',
                  stdoutPath=r'C:\logs\stdout.log', stderrPath=r'C:\logs\stderr.log',
                  timeoutSeconds=2, drainSeconds=.2, terminationSeconds=.3, budgetLabel='test execution deadline')
    values.update(changes)
    return owned.Request(**values)


def put(pointer, cls, value):
    ctypes.cast(pointer, ctypes.POINTER(cls))[0] = value


class Clock:
    value = 0
    def now(self): return self.value
    def sleep(self, amount): self.value += amount


class FakeApi:
    """Model only handles this owner creates. A separate unrelated process survives."""
    def __init__(self, failure=None):
        self.failure = failure
        self.last_error = 5
        self.next = 100
        self.handles = {}
        self.closed = []
        self.events = []
        self.attributes = {}
        self.job_members = 0
        self.root_exited = False
        self.code = 0
        self.unrelated = {'pid': 900001, 'alive': True}
        self.log_size = 0
        self.attributes_deleted = 0

    def handle(self, kind):
        self.next += 1
        self.handles[self.next] = kind
        return self.next

    def CreateJobObjectW(self, security, name):
        self.events.append('create-job')
        assert security is None and name is None
        return 0 if self.failure == 'create-job' else self.handle('job')

    def SetInformationJobObject(self, job, kind, info, size):
        self.events.append('kill-on-close')
        assert kind == 9
        limits = ctypes.cast(info, ctypes.POINTER(owned.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)).contents
        assert limits.BasicLimitInformation.LimitFlags == 0x2000
        return self.failure != 'limits'

    def CreateFileW(self, path, access, share, security, creation, flags, template):
        assert ctypes.cast(security, ctypes.POINTER(owned.SECURITY_ATTRIBUTES)).contents.bInheritHandle == 1
        if path != 'NUL': assert access == 0x40000080  # write plus explicit FILE_READ_ATTRIBUTES for size checks
        if self.failure == 'output' and path != 'NUL':
            return ctypes.c_void_p(-1).value
        if self.failure == 'null-input' and path == 'NUL':
            return ctypes.c_void_p(-1).value
        return self.handle('input' if path == 'NUL' else 'output')

    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        assert count == 2
        if attributes is None:
            if self.failure != 'attribute-size': put(size, owned.SIZE_T, owned.SIZE_T(128))
            return 0
        self.events.append('initialize-attributes')
        return self.failure != 'attribute-init'

    def UpdateProcThreadAttribute(self, attributes, flags, kind, values, size, previous, returned):
        self.attributes[kind] = list(ctypes.cast(values, ctypes.POINTER(owned.HANDLE * (size // ctypes.sizeof(owned.HANDLE)))).contents)
        self.events.append('handle-list' if kind == 0x20002 else 'job-list')
        return self.failure != ('inheritance' if kind == 0x20002 else 'job-list')

    def DeleteProcThreadAttributeList(self, attributes):
        self.attributes_deleted += 1
        self.events.append('delete-attributes')

    def CreateProcessW(self, executable, command, process_security, thread_security, inherit, flags, env, cwd, startup, info):
        self.events.append('create-suspended-in-job')
        assert process_security is None and thread_security is None
        assert inherit is True and flags == 0x8080404
        inherited = self.attributes[0x20002]
        jobs = self.attributes[0x2000D]
        assert len(jobs) == 1 and self.handles[jobs[0]] == 'job'
        assert jobs[0] not in inherited
        assert set(self.handles[h] for h in inherited) <= {'input', 'output'}
        start = ctypes.cast(startup, ctypes.POINTER(owned.STARTUPINFOEXW)).contents
        assert start.StartupInfo.cb == ctypes.sizeof(owned.STARTUPINFOEXW)
        assert start.StartupInfo.dwFlags == 0x100
        assert {start.StartupInfo.hStdInput, start.StartupInfo.hStdOutput, start.StartupInfo.hStdError} == set(inherited)
        self.command = command.value
        self.executable = executable
        self.environment = env[:]
        if self.failure == 'create-process': return 0
        self.job_members = 1
        put(info, owned.PROCESS_INFORMATION, owned.PROCESS_INFORMATION(self.handle('process'), self.handle('thread'), 4567, 4568))
        return 1

    def IsProcessInJob(self, process, job, result):
        self.events.append('verify-membership')
        put(result, owned.BOOL, owned.BOOL(self.failure != 'membership-false'))
        return self.failure != 'membership-api'

    def ResumeThread(self, thread):
        self.events.append('resume')
        return 0xFFFFFFFF if self.failure == 'resume' else 1

    def WaitForSingleObject(self, process, timeout):
        assert self.handles[process] == 'process'
        assert timeout >= 0
        return 0 if self.root_exited else 258

    def GetExitCodeProcess(self, process, code):
        put(code, owned.DWORD, owned.DWORD(self.code))
        return 1

    def QueryInformationJobObject(self, job, kind, info, size, returned):
        assert self.handles[job] == 'job' and kind == 1
        if self.failure == 'query': return 0
        value = owned.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        value.ActiveProcesses = self.job_members
        put(info, owned.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, value)
        return 1

    def GetFileSizeEx(self, handle, size):
        if self.failure == 'log-read': return 0
        put(size, ctypes.c_int64, ctypes.c_int64(self.log_size))
        return 1

    def TerminateJobObject(self, job, code):
        self.events.append('terminate-job')
        if self.failure == 'terminate': return 0
        self.code = 1
        self.root_exited = True
        if self.failure != 'termination-stalled': self.job_members = 0
        return 1

    def TerminateProcess(self, process, code):
        assert self.handles[process] == 'process'
        self.events.append('terminate-held-suspended-process')
        self.root_exited = True
        self.job_members = 0
        return 1

    def CloseHandle(self, handle):
        assert handle in self.handles and handle not in self.closed
        self.closed.append(handle)
        if self.handles[handle] == 'job':
            self.events.append('close-last-job-owner')
            # KILL_ON_JOB_CLOSE kills even after explicit termination API failed.
            self.job_members = 0
            self.root_exited = True
        return self.failure != 'close'


class ApiOwnershipTests(unittest.TestCase):
    def test_win64_abi_structure_sizes_and_offsets(self):
        self.assertEqual(ctypes.sizeof(owned.DWORD), 4)
        self.assertEqual(ctypes.sizeof(owned.BOOL), 4)
        if ctypes.sizeof(owned.HANDLE) != 8: self.skipTest('x64 ABI layout test')
        for cls, size in ((owned.SECURITY_ATTRIBUTES, 24), (owned.STARTUPINFOW, 104), (owned.STARTUPINFOEXW, 112),
                          (owned.PROCESS_INFORMATION, 24), (owned.JOBOBJECT_BASIC_LIMIT_INFORMATION, 64),
                          (owned.JOBOBJECT_EXTENDED_LIMIT_INFORMATION, 144), (owned.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, 48)):
            self.assertEqual(ctypes.sizeof(cls), size, cls.__name__)
        self.assertEqual(owned.STARTUPINFOW.hStdInput.offset, 80)
        self.assertEqual(owned.JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags.offset, 16)

    def test_atomic_job_then_suspended_create_membership_resume_no_handle_escape(self):
        api = FakeApi(); native = owned.WindowsContainment(api, makedirs=lambda _: None)
        native.launch(request())
        self.assertEqual(native.pid, 4567)
        self.assertEqual(api.events, ['create-job','kill-on-close','initialize-attributes','handle-list','job-list',
                                     'create-suspended-in-job','verify-membership','resume','delete-attributes'])
        native.close(); native.close()
        self.assertEqual(set(api.closed), set(api.handles))
        self.assertTrue(api.unrelated['alive'])
        self.assertEqual(api.job_members, 0)

    def test_every_launch_failure_closes_only_owned_handles_and_never_runs_unsupervised(self):
        for failure in ('create-job','limits','output','null-input','attribute-size','attribute-init','inheritance',
                        'job-list','create-process','membership-api','membership-false','resume'):
            with self.subTest(failure=failure):
                api = FakeApi(failure); native = owned.WindowsContainment(api, makedirs=lambda _: None)
                clock = Clock()
                receipt = owned.supervise(request(), containment=native, clock=clock.now, sleep=clock.sleep)
                self.assertEqual(receipt['outcome'], 'supervision-error')
                self.assertFalse(owned.terminal_receipt(receipt))
                self.assertEqual(set(api.closed), set(api.handles))
                self.assertTrue(api.unrelated['alive'])
                self.assertEqual(api.job_members, 0)
                if failure not in ('resume',): self.assertNotIn('resume', api.events)
                if failure in ('membership-api','membership-false'):
                    self.assertIn('terminate-held-suspended-process', api.events)
                self.assertEqual(api.attributes_deleted, int('initialize-attributes' in api.events and failure != 'attribute-init'))

    def test_combined_stdout_stderr_handle_is_inherited_and_closed_once(self):
        api = FakeApi(); native = owned.WindowsContainment(api, makedirs=lambda _: None)
        native.launch(request(stderrPath=r'C:\logs\stdout.log'))
        self.assertEqual(len(api.attributes[0x20002]), 2)
        native.close()
        self.assertEqual(len(api.closed), len(set(api.closed)))

    def test_environment_is_case_insensitive_and_never_appears_in_receipt(self):
        api = FakeApi(); native = owned.WindowsContainment(api, makedirs=lambda _: None)
        clock = Clock()
        with patch.dict(os.environ, {'Path':'old-path','SECRET_SENTINEL':'private-value'}, clear=True):
            result = owned.supervise(request(environment={'PATH':'new-path', 'SECRET_SENTINEL':None}), containment=native, clock=clock.now, sleep=clock.sleep)
        self.assertIn('PATH=new-path\0', api.environment)
        self.assertNotIn('old-path', api.environment)
        self.assertNotIn('private-value', api.environment)
        self.assertNotIn('new-path', json.dumps(result))
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))


class LifecycleTests(unittest.TestCase):
    def run_case(self, failure=None, **changes):
        api = FakeApi(failure)
        native = owned.WindowsContainment(api, makedirs=lambda _: None)
        clock = Clock()
        original = native.launch
        def launch(req):
            original(req)
            for key, value in changes.items(): setattr(api, key, value)
        native.launch = launch
        result = owned.supervise(request(), containment=native, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(set(api.closed), set(api.handles))
        self.assertTrue(api.unrelated['alive'])
        return result, api, clock

    def test_output_eof_or_silence_is_not_process_completion(self):
        result, api, clock = self.run_case()
        self.assertEqual(result['outcome'], 'execution-timeout')
        self.assertTrue(result['confirmedTreeEmpty'])
        self.assertLess(clock.value, 2.1)
        self.assertIn('terminate-job', api.events)

    def test_clean_success_preserves_real_target_pid(self):
        result, api, _ = self.run_case(root_exited=True, job_members=0)
        self.assertTrue(owned.terminal_receipt(result))
        self.assertEqual(result['targetExitCode'], 0)
        self.assertEqual(result['launchTargetPid'], 4567)
        self.assertEqual(result['supervisorPid'], os.getpid())
        self.assertNotIn('terminate-job', api.events)

    def test_high_bit_native_exit_preserves_unsigned_dword_and_signed_powershell_code(self):
        result, _, _ = self.run_case(root_exited=True, job_members=0, code=0xC0000005)
        self.assertEqual(result['targetExitCodeUnsigned'], 3221225477)
        self.assertEqual(result['targetExitCode'], -1073741819)
        self.assertTrue(owned.terminal_receipt(result))
        self.assertTrue(result['confirmedTreeEmpty'])

    def test_parent_exit_does_not_forget_owned_live_descendants(self):
        result, api, clock = self.run_case(root_exited=True, job_members=2)
        self.assertEqual(result['outcome'], 'descendant-drain-timeout')
        self.assertTrue(result['confirmedTreeEmpty'])
        self.assertFalse(owned.terminal_receipt(result))
        self.assertGreaterEqual(clock.value, .2)
        self.assertLess(clock.value, .3)
        self.assertIn('terminate-job', api.events)

    def test_nonzero_root_terminates_descendants_without_claiming_success(self):
        result, api, clock = self.run_case(root_exited=True, job_members=2, code=23)
        self.assertEqual(result['outcome'], 'target-exited-nonzero')
        # The initial exit code must survive terminating remaining descendants.
        self.assertEqual(result['targetExitCode'], 23)
        self.assertTrue(owned.terminal_receipt(result))
        self.assertEqual(clock.value, 0)

    def test_termination_api_error_closes_last_job_owner_but_never_claims_confirmation(self):
        result, api, _ = self.run_case('terminate')
        self.assertEqual(result['outcome'], 'execution-timeout')
        self.assertFalse(result['confirmedTreeEmpty'])
        self.assertFalse(owned.terminal_receipt(result))
        self.assertTrue(any('Termination:' in e for e in result['errors']))
        self.assertIn('close-last-job-owner', api.events)
        self.assertEqual(api.job_members, 0)

    def test_stalled_termination_is_bounded_and_unconfirmed(self):
        result, api, clock = self.run_case('termination-stalled')
        self.assertFalse(result['confirmedTreeEmpty'])
        self.assertGreaterEqual(clock.value, 2.3)
        self.assertLess(clock.value, 2.4)
        self.assertEqual(api.job_members, 0)

    def test_query_log_and_handle_errors_never_become_a_pass(self):
        for failure in ('query','log-read','close'):
            with self.subTest(failure=failure):
                result, api, _ = self.run_case(failure)
                self.assertFalse(owned.terminal_receipt(result))
                self.assertTrue(result['errors'])
                self.assertEqual(api.job_members, 0)

    def test_log_size_limit_fails_and_keeps_owned_output_prefix(self):
        result, _, _ = self.run_case(log_size=256*1024*1024+1)
        self.assertEqual(result['outcome'], 'log-size-limit')
        self.assertTrue(result['confirmedTreeEmpty'])
        self.assertFalse(owned.terminal_receipt(result))

    def test_control_eof_and_command_cancel_without_waiting_on_target_output(self):
        for text in ('', 'stop\n'):
            event = threading.Event()
            owned.watch_control(event, io.StringIO(text))
            self.assertTrue(event.is_set())
        api = FakeApi(); native = owned.WindowsContainment(api, makedirs=lambda _: None)
        result = owned.supervise(request(), containment=native, cancelled=event)
        self.assertEqual(result['outcome'], 'cancelled-before-launch')
        self.assertEqual(api.events, [])


class ArgumentsAndIdentityTests(unittest.TestCase):
    def test_exe_argv_preserves_unicode_empty_quotes_backslashes_and_metacharacters(self):
        values = ['', '中文 space', 'trailing\\', 'a"b', '%PATH%', '& not a command', 'line\nfeed']
        actual, text = owned.command_line(r'C:\路径 with spaces\electron.exe', values)
        self.assertEqual(actual, r'C:\路径 with spaces\electron.exe')
        self.assertEqual(text, __import__('subprocess').list2cmdline([actual, *values]))
        self.assertNotIn(' /c ', text)

    def test_cmd_and_npm_use_normalized_safe_quoted_argv(self):
        actual, text = owned.command_line(r'C:\Program Files\nodejs\npm.cmd', ['run','build','--','路径 (3)',''], system_directory=r'C:\Windows\System32')
        self.assertEqual(actual, r'C:\Windows\System32\cmd.exe')
        self.assertEqual(text, 'C:\\Windows\\System32\\cmd.exe /d /s /v:off /c ""C:\\Program Files\\nodejs\\npm.cmd" "run" "build" "--" "路径 (3)" """')
        for special in ('x&calc','x|calc','%PATH%','!PATH!','x"y','x\ny','x^y','x>y'):
            with self.subTest(special=special), self.assertRaisesRegex(ValueError, 'unsupported shell syntax'):
                owned.command_line(r'C:\npm.cmd', [special], system_directory=r'C:\Windows\System32')

    def test_venv_redirector_rule_keeps_prefix_and_direct_interpreter_identity(self):
        environment = {}
        command = owned.direct_child_command([r'C:\project\.venv\Scripts\python.exe','fixture.py'], environment, windows=True,
            executable=r'C:\project\.venv\Scripts\python.exe', base_executable=r'C:\Python313\python.exe', frozen=False)
        self.assertEqual(command, [r'C:\Python313\python.exe','fixture.py'])
        self.assertEqual(environment, {'__PYVENV_LAUNCHER__':r'C:\project\.venv\Scripts\python.exe'})
        for target, frozen in ((r'C:\Core\collector_core.exe',False),(r'C:\project\.venv\Scripts\python.exe',True)):
            environment = {}
            result = owned.direct_child_command([target], environment, windows=True, executable=r'C:\project\.venv\Scripts\python.exe', base_executable=r'C:\Python313\python.exe', frozen=frozen)
            self.assertEqual(result, [target]); self.assertEqual(environment,{})

    def test_invalid_budget_and_argv_fail_before_launch(self):
        for changes in ({'timeoutSeconds':float('inf')}, {'timeoutSeconds':0}, {'timeoutSeconds':1e100}, {'terminationSeconds':1e100}, {'drainSeconds':601}, {'arguments':'bad'}, {'arguments':['bad\0arg']},
                        {'environment':{'BAD=NAME':'value'}}, {'environment':{'NAME':12}}, {'budgetLabel':''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): request(**changes).validate()

    def test_rooted_and_drive_relative_paths_receipt_collisions_and_flags_are_rejected(self):
        invalid = [r'\rooted\python.exe', '/rooted/python.exe', 'C:relative.exe', 'relative.exe', '', None, 42, 'C:\\nul\0bad']
        for key in ('executable', 'workingDirectory', 'stdoutPath', 'stderrPath', 'receiptPath'):
            for value in invalid:
                if key == 'receiptPath' and value is None:
                    continue
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    request(**{key:value}).validate()
        for changes in ({'controlInput':1}, {'controlInput':'true'}, {'controlInput':None},
                        {'receiptPath':r'C:\logs\stdout.log'}, {'receiptPath':r'c:\logs\sub\..\STDERR.log'},
                        {'stdoutPath':r'C:\实验室\python.exe'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                request(**changes).validate()
        request(receiptPath=r'C:\logs\receipt.json', controlInput=True).validate()

    def test_independent_hard_deadline_and_control_eof_backstops_do_not_claim_success(self):
        class Completion:
            def __init__(self, completed): self.completed=completed; self.waits=[]
            def wait(self, seconds): self.waits.append(seconds); return self.completed
        for done in (False, True):
            state=Completion(done); exits=[]
            owned.enforce_deadline(state, 7, hard_exit=exits.append)
            self.assertEqual(state.waits,[7]); self.assertEqual(exits,[] if done else [125])
            cancelled=threading.Event();state=Completion(done);exits=[]
            owned.watch_control(cancelled,io.StringIO(''),state,3,hard_exit=exits.append)
            self.assertTrue(cancelled.is_set());self.assertEqual(state.waits,[8]);self.assertEqual(exits,[] if done else [125])

    def test_runtime_refuses_unsupported_version_abi_implementation_and_free_threading(self):
        defaults = dict(version=(3,13,0), width=8, implementation='CPython', machine='AMD64', free_threaded=0)
        cases = [{'version':(3,10,0)}, {'version':(3,15,0)}, {'width':4}, {'implementation':'PyPy'}, {'machine':'ARM64'}, {'free_threaded':1}]
        for changes in cases + [{}]:
            settings = {**defaults, **changes}
            with self.subTest(changes=changes), patch.object(owned.sys,'version_info',settings['version']), \
                 patch.object(owned.struct,'calcsize',return_value=settings['width']), \
                 patch.object(owned.platform,'python_implementation',return_value=settings['implementation']), \
                 patch.object(owned.platform,'machine',return_value=settings['machine']), \
                 patch.object(owned.sysconfig,'get_config_var',return_value=settings['free_threaded']):
                if changes:
                    with self.assertRaisesRegex(owned.SupervisionError,'standard x64 CPython 3.11-3.14'):
                        owned.validate_runtime()
                else:
                    owned.validate_runtime()

    def test_missing_mistyped_and_unknown_receipt_fields_never_confirm_cleanup(self):
        api=FakeApi();native=owned.WindowsContainment(api,makedirs=lambda _:None);clock=Clock()
        good=owned.supervise(request(),containment=native,clock=clock.now,sleep=clock.sleep)
        self.assertTrue(owned.cleanup_receipt(good))
        for key in owned.RECEIPT_KEYS:
            bad=dict(good);bad.pop(key)
            with self.subTest(missing=key):
                self.assertFalse(owned.cleanup_receipt(bad));self.assertFalse(owned.terminal_receipt(bad))
        for changes in ({'schemaVersion':True},{'confirmedTreeEmpty':'true'},{'errors':None},{'outcome':'invented'},{'launchTargetPid':True}):
            with self.subTest(changes=changes):
                self.assertFalse(owned.cleanup_receipt({**good,**changes}))


if __name__ == '__main__': unittest.main()
