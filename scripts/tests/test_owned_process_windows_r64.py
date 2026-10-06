"""Native Windows acceptance gates for owned-process.py; never substituted by fakes.

Run after declared Python/Node/Electron prerequisites are available. These tests
spawn synthetic local children only. No installed application/account is used.
"""
from __future__ import annotations
import base64
import ctypes
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'scripts/owned_process.py'
spec = importlib.util.spec_from_file_location('r64_owned_windows', HELPER)
owned = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owned
spec.loader.exec_module(owned)


class OwnedProcessWaitTimeout(AssertionError):
    pass


class OwnedProcessWaitFailedInvalidHandle(AssertionError):
    pass


class OwnedProcessWaitFailedAccessDenied(AssertionError):
    pass


class OwnedProcessWaitFailedOther(AssertionError):
    pass


class OwnedProcessWaitFailedErrorUnavailable(AssertionError):
    pass


class OwnedProcessWaitUnexpected(AssertionError):
    pass


class OwnedProcessVerifiedEvidenceRejected(AssertionError):
    pass


class OwnedProcessVerifiedWaitTimeoutThenSignaled(AssertionError):
    pass


class OwnedProcessVerifiedAccountingZeroThenSignaled(AssertionError):
    pass


class OwnedProcessVerifiedSignalNotObservedInBudget(AssertionError):
    pass


def raise_held_process_api_failure():
    # Read the calling thread's last error before any further Windows call.
    try:
        last_error = ctypes.get_last_error()
    except Exception:
        raise OwnedProcessWaitFailedErrorUnavailable() from None
    if type(last_error) is not int:
        raise OwnedProcessWaitFailedErrorUnavailable()
    if last_error == 6:
        raise OwnedProcessWaitFailedInvalidHandle()
    if last_error == 5:
        raise OwnedProcessWaitFailedAccessDenied()
    raise OwnedProcessWaitFailedOther()


def assert_held_process_signaled(api, handle, milliseconds=0):
    """Preserve the zero-time exit assertion, exposing only fixed failure classes."""
    result = api.WaitForSingleObject(handle, milliseconds)
    if result == 0xFFFFFFFF:  # WAIT_FAILED: capture before any further Windows call.
        raise_held_process_api_failure()
    if result == 0:  # WAIT_OBJECT_0 is the sole successful return value.
        return
    if result == 258:  # WAIT_TIMEOUT
        raise OwnedProcessWaitTimeout()
    raise OwnedProcessWaitUnexpected()


def observe_held_process(api, handle, milliseconds=0):
    try:
        assert_held_process_signaled(api, handle, milliseconds)
    except (OwnedProcessWaitTimeout, OwnedProcessWaitFailedInvalidHandle,
            OwnedProcessWaitFailedAccessDenied, OwnedProcessWaitFailedOther,
            OwnedProcessWaitFailedErrorUnavailable, OwnedProcessWaitUnexpected) as error:
        return type(error)
    return None


def require_held_process_unsignaled(api, handle):
    observation = observe_held_process(api, handle)
    if observation is None:
        raise OwnedProcessVerifiedEvidenceRejected()
    if observation is not OwnedProcessWaitTimeout:
        raise observation()


class VerifiedCleanupContainment(owned.WindowsContainment):
    """Test-only observation of the unchanged private-job cleanup implementation."""
    def __init__(self, request, *, clock=time.monotonic):
        super().__init__()
        self.api.OpenProcess.argtypes = [owned.DWORD, owned.BOOL, owned.DWORD]
        self.api.OpenProcess.restype = owned.HANDLE
        self.request = request
        self.clock = clock
        self.held_child = self.verified_job = self.verified_root = None
        self.acquired_at = self.acknowledged_at = None
        self.termination_started_at = self.termination_completed_at = None
        self.termination_deadline = None
        self.accounting_zero_at = self.accounting_wait_at = None
        self.accounting_wait = None
        self.termination_calls = 0

    def acquire_child(self, pid, handles):
        if type(pid) is not int or pid <= 0 or not self.job or not self.process or self.closed:
            raise OwnedProcessVerifiedEvidenceRejected()
        # Retained Popen in the waiting parent prevents PID reuse during acquisition.
        handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            raise OwnedProcessVerifiedEvidenceRejected()
        handles.append(handle)
        member = owned.BOOL()
        if not self.api.IsProcessInJob(handle, self.job, ctypes.byref(member)):
            raise OwnedProcessVerifiedEvidenceRejected()
        if not member.value:
            raise OwnedProcessVerifiedEvidenceRejected()
        require_held_process_unsignaled(self.api, handle)
        require_held_process_unsignaled(self.api, self.process)
        if self.termination_calls:
            raise OwnedProcessVerifiedEvidenceRejected()
        self.held_child, self.verified_job, self.verified_root = handle, self.job, self.process
        self.acquired_at = self.clock()

    def acknowledge(self, acknowledgment):
        if (self.held_child is None or self.verified_job != self.job or
                self.verified_root != self.process or self.termination_calls or self.closed):
            raise OwnedProcessVerifiedEvidenceRejected()
        require_held_process_unsignaled(self.api, self.process)
        acknowledgment.write_text('ack', encoding='ascii')
        self.acknowledged_at = self.clock()

    def terminate(self):
        self.termination_calls += 1
        self.termination_started_at = self.clock()
        # Deliberately anchored BEFORE the same TerminateJobObject call.
        self.termination_deadline = self.termination_started_at + self.request.terminationSeconds
        super().terminate()
        self.termination_completed_at = self.clock()

    def active_processes(self):
        active = super().active_processes()
        if (self.termination_completed_at is not None and active == 0 and
                self.accounting_zero_at is None):
            self.accounting_zero_at = self.clock()
            if self.held_child is None:
                raise OwnedProcessVerifiedEvidenceRejected()
            self.accounting_wait = observe_held_process(self.api, self.held_child)
            self.accounting_wait_at = self.clock()
        return active


def assert_verified_cleanup_observation(native, receipt, returned_at, initial_wait, initial_wait_at):
    """A later signal categorizes the original assertion failure; it never repairs it."""
    times = (native.acquired_at, native.acknowledged_at, native.termination_started_at,
             native.termination_completed_at, native.accounting_zero_at,
             native.accounting_wait_at, returned_at, initial_wait_at)
    valid_time = lambda value: type(value) in (int, float) and math.isfinite(value)
    if (not all(valid_time(value) for value in times) or list(times) != sorted(times) or
            not valid_time(native.termination_deadline) or
            native.termination_deadline != native.termination_started_at + 2 or
            (native.request.timeoutSeconds, native.request.drainSeconds, native.request.terminationSeconds) != (10, .5, 2) or
            type(native.termination_calls) is not int or native.termination_calls != 1 or native.held_child is None or
            not native.verified_job or native.verified_job != native.job or
            not native.verified_root or native.verified_root != native.process or
            native.closed is not True or not owned.cleanup_receipt(receipt) or
            receipt['outcome'] != 'descendant-drain-timeout' or receipt['targetExitCode'] != 0):
        raise OwnedProcessVerifiedEvidenceRejected()
    for observation in (native.accounting_wait, initial_wait):
        if observation not in (None, OwnedProcessWaitTimeout):
            raise observation()
    if initial_wait is None:
        if native.accounting_wait is None:
            if native.accounting_wait_at <= native.termination_deadline:
                return
        elif initial_wait_at <= native.termination_deadline:
            raise OwnedProcessVerifiedAccountingZeroThenSignaled()
        raise OwnedProcessVerifiedSignalNotObservedInBudget()
    if native.accounting_wait is not OwnedProcessWaitTimeout:
        # A process handle cannot become unsignaled after being observed signaled.
        raise OwnedProcessVerifiedEvidenceRejected()
    now = native.clock()
    if not valid_time(now) or now < initial_wait_at:
        raise OwnedProcessVerifiedEvidenceRejected()
    milliseconds = max(0, math.floor((native.termination_deadline - now) * 1000))
    if milliseconds == 0:
        raise OwnedProcessVerifiedSignalNotObservedInBudget()
    late_wait = observe_held_process(native.api, native.held_child, milliseconds)
    observed_at = native.clock()
    if not valid_time(observed_at) or observed_at < now:
        raise OwnedProcessVerifiedEvidenceRejected()
    if late_wait not in (None, OwnedProcessWaitTimeout):
        raise late_wait()
    if late_wait is None and observed_at <= native.termination_deadline:
        raise OwnedProcessVerifiedWaitTimeoutThenSignaled()
    raise OwnedProcessVerifiedSignalNotObservedInBudget()


def supervise_verified_cleanup(request, native, cancelled):
    receipt = owned.supervise(request, containment=native, cancelled=cancelled)
    returned_at = native.clock()
    if native.held_child is None:
        raise OwnedProcessVerifiedEvidenceRejected()
    initial_wait = observe_held_process(native.api, native.held_child)
    initial_wait_at = native.clock()
    assert_verified_cleanup_observation(native, receipt, returned_at, initial_wait, initial_wait_at)


@unittest.skipUnless(os.name == 'nt', 'Actual Windows API acceptance requires Windows')
class NativeWindowsOwnership(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='R64 owned Unicode space 测试 ')
        self.folder = Path(self.temporary.name)
        self.api = owned.windows_api()
        self.api.OpenProcess.argtypes = [owned.DWORD, owned.BOOL, owned.DWORD]
        self.api.OpenProcess.restype = owned.HANDLE
        self.handles = []

    def tearDown(self):
        for handle in self.handles:
            self.api.CloseHandle(handle)
        self.temporary.cleanup()

    def request(self, arguments, **changes):
        values = dict(executable=sys.executable, arguments=arguments, workingDirectory=str(self.folder),
                      stdoutPath=str(self.folder/'stdout.log'), stderrPath=str(self.folder/'stderr.log'),
                      timeoutSeconds=10, drainSeconds=.5, terminationSeconds=2, budgetLabel='Native synthetic acceptance',
                      environment={'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
        values.update(changes)
        return owned.Request(**values)

    def held_process(self, pid):
        # The PID was just emitted by this exact owned test. Retain its handle
        # before cleanup so a later exit/PID reuse cannot falsify the assertion.
        handle = self.api.OpenProcess(0x00100000, False, pid)
        self.assertTrue(handle, 'Open exact owned fixture process')
        self.handles.append(handle)
        return handle

    def wait_json(self, file):
        until = time.monotonic()+5
        while time.monotonic()<until:
            try:
                return json.loads(file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                time.sleep(.02)
        self.fail('Owned fixture did not publish its PID within 5 seconds')

    def test_actual_interpreter_pid_prefix_unicode_output_and_nonzero_exit(self):
        command = 'import json,os,sys;print(json.dumps(dict(pid=os.getpid(),prefix=sys.prefix,executable=sys.executable,argv=sys.argv[1:]),ensure_ascii=False),flush=True);sys.stderr.write("stderr 中文\\n");sys.exit(23)'
        args = ['', '中文 space', 'quote"inside', 'trailing\\', '%PATH%', '& literal']
        receipt = owned.supervise(self.request(['-c',command,*args]))
        self.assertTrue(owned.terminal_receipt(receipt), receipt)
        self.assertEqual(receipt['targetExitCode'],23)
        proof=json.loads((self.folder/'stdout.log').read_text(encoding='utf-8'))
        self.assertEqual(proof['pid'],receipt['launchTargetPid'])
        self.assertEqual(proof['prefix'],sys.prefix)
        self.assertEqual(proof['argv'],args)
        self.assertIn('stderr 中文',(self.folder/'stderr.log').read_text(encoding='utf-8'))

    def test_real_high_bit_windows_exit_retains_both_representations(self):
        code='import ctypes;k=ctypes.WinDLL("kernel32");k.ExitProcess.argtypes=[ctypes.c_uint32];k.ExitProcess.restype=None;k.ExitProcess(0xC0000005)'
        receipt=owned.supervise(self.request(['-c',code]))
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        self.assertEqual(receipt['targetExitCodeUnsigned'],3221225477)
        self.assertEqual(receipt['targetExitCode'],-1073741819)
        self.assertTrue(receipt['confirmedTreeEmpty'])

    def test_explicit_project_venv_redirector_preserves_actual_interpreter_proof(self):
        venv_python=ROOT/'.venv/Scripts/python.exe'
        self.assertTrue(venv_python.is_file(),'Run this native gate after project venv setup')
        code='import json,os,sys;print(json.dumps(dict(pid=os.getpid(),prefix=sys.prefix,executable=sys.executable)),flush=True)'
        request=self.request(['-c',code],executable=str(venv_python))
        # Start the owner with the base interpreter so this test deliberately
        # exercises the real Windows venv redirector instead of rewriting it.
        base=getattr(sys,'_base_executable',sys.executable)
        environment={key:value for key,value in os.environ.items() if key.upper()!='__PYVENV_LAUNCHER__'}
        result=subprocess.run([base,'-I','-X','utf8',str(HELPER),'--request-base64',
            base64.b64encode(json.dumps(request.__dict__).encode()).decode()],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment,timeout=20,check=False)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        receipt=json.loads(result.stdout)
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        proof=json.loads((self.folder/'stdout.log').read_text(encoding='utf-8'))
        self.assertEqual(os.path.normcase(proof['prefix']),os.path.normcase(str(ROOT/'.venv')))
        self.assertEqual(os.path.normcase(proof['executable']),os.path.normcase(str(venv_python)))
        self.assertNotEqual(proof['pid'],receipt['launchTargetPid'],'Venv redirector PID must not be relabeled as interpreter proof PID')

    def test_parent_exit_and_inherited_output_descendant_cleanup(self):
        proof_file=self.folder/'child.json'
        script = 'import subprocess,sys,time,json;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);Path(sys.argv[1]).write_text(json.dumps(p.pid));time.sleep(.5)'
        results=[]
        worker=threading.Thread(target=lambda:results.append(owned.supervise(self.request(['-c',script,str(proof_file)]))))
        worker.start()
        handle=self.held_process(self.wait_json(proof_file))
        worker.join(15)
        self.assertFalse(worker.is_alive(),'Native supervision must be bounded')
        receipt=results[0]
        self.assertEqual(receipt['outcome'],'descendant-drain-timeout',receipt)
        self.assertTrue(receipt['confirmedTreeEmpty'],receipt)
        assert_held_process_signaled(self.api,handle)

    def test_verified_parent_exit_and_inherited_output_descendant_cleanup(self):
        proof_file = self.folder/'verified-child.json'
        acknowledgment = self.folder/'verified-ack'
        script = '\n'.join((
            'import subprocess,sys,time,json',
            'from pathlib import Path',
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])',
            'Path(sys.argv[1]).write_text(json.dumps(p.pid))',
            'ack=Path(sys.argv[2]); deadline=time.monotonic()+5',
            'while not ack.exists() and time.monotonic()<deadline: time.sleep(.01)',
            'if not ack.exists(): sys.exit(124)',
            'time.sleep(.5)'))
        request = self.request(['-c', script, str(proof_file), str(acknowledgment)])
        native = VerifiedCleanupContainment(request)
        cancelled = threading.Event()
        failures, handles, completed = [], [], []
        def run():
            try:
                supervise_verified_cleanup(request, native, cancelled)
                completed.append(True)
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            native.acquire_child(self.wait_json(proof_file), handles)
            native.acknowledge(acknowledgment)
        finally:
            if native.acknowledged_at is None:
                cancelled.set()
            worker.join(15)
            # Never invalidate a handle that a live worker might still sample.
            # The unchanged outer owned-process gate is the final watchdog.
            if not worker.is_alive():
                for handle in handles:
                    self.api.CloseHandle(handle)
            self.assertFalse(worker.is_alive(), 'Native supervision must be bounded')
        if failures:
            raise failures[0]
        self.assertEqual(completed, [True])

    def test_supervisor_death_closes_job_and_only_its_owned_processes(self):
        proof_file=self.folder/'tree.json'
        script='import subprocess,os,sys,time,json;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(60)'
        request=self.request(['-c',script,str(proof_file)],controlInput=True,receiptPath=str(self.folder/'receipt.json'))
        environment=dict(os.environ)
        command=owned.direct_child_command([sys.executable,'-I','-X','utf8',str(HELPER),'--request-base64',base64.b64encode(json.dumps(request.__dict__).encode()).decode()],environment)
        unrelated_environment=dict(os.environ)
        unrelated=subprocess.Popen(owned.direct_child_command([sys.executable,'-c','import time;time.sleep(60)'],unrelated_environment),env=unrelated_environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        supervisor=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=environment)
        try:
            handles=[self.held_process(pid) for pid in self.wait_json(proof_file)]
            supervisor.kill();supervisor.wait(timeout=5)
            for handle in handles:
                self.assertEqual(self.api.WaitForSingleObject(handle,5000),0,'Last job-owner death must terminate its descendants')
            self.assertIsNone(unrelated.poll(),'An unrelated same-name Python process must survive')
        finally:
            supervisor.stdin.close()
            if supervisor.poll() is None:supervisor.kill();supervisor.wait(timeout=5)
            unrelated.terminate();unrelated.wait(timeout=5)

    def test_real_control_eof_cancels_live_job(self):
        proof_file=self.folder/'started.json'
        script='import os,sys,time,json;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(os.getpid()));time.sleep(60)'
        request=self.request(['-c',script,str(proof_file)],controlInput=True,receiptPath=str(self.folder/'receipt.json'))
        env=dict(os.environ)
        command=owned.direct_child_command([sys.executable,'-I','-X','utf8',str(HELPER),'--request-base64',base64.b64encode(json.dumps(request.__dict__).encode()).decode()],env)
        supervisor=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
        try:
            handle=self.held_process(self.wait_json(proof_file))
            supervisor.stdin.close();supervisor.wait(timeout=8)
            receipt=json.loads((self.folder/'receipt.json').read_text())
            self.assertEqual(receipt['outcome'],'cancelled',receipt)
            self.assertTrue(receipt['confirmedTreeEmpty'],receipt)
            self.assertEqual(self.api.WaitForSingleObject(handle,0),0)
        finally:
            if supervisor.poll() is None:supervisor.kill();supervisor.wait(timeout=5)

    def test_cmd_and_installed_npm_typed_arguments(self):
        script=self.folder/'quoted fixture.cmd'
        script.write_text('@echo off\r\necho [%~1] [%~2]\r\nexit /b 23\r\n',encoding='ascii')
        receipt=owned.supervise(self.request(['path with spaces (3)',''],executable=str(script)))
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        self.assertEqual(receipt['targetExitCode'],23)
        self.assertIn('[path with spaces (3)] []',(self.folder/'stdout.log').read_text())
        npm=shutil.which('npm.cmd')
        self.assertTrue(npm,'Existing declared npm prerequisite is required for this native gate')
        receipt=owned.supervise(self.request(['--version'],executable=npm))
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        self.assertEqual(receipt['targetExitCode'],0)

    def test_real_electron_launch_under_job(self):
        electron=ROOT/'node_modules/electron/dist/electron.exe'
        self.assertTrue(electron.is_file(),'Run native ownership acceptance after existing Electron dependency setup')
        receipt=owned.supervise(self.request(['--version'],executable=str(electron)))
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        self.assertEqual(receipt['targetExitCode'],0)
        self.assertRegex((self.folder/'stdout.log').read_text(),r'v\d+\.\d+')

    def test_nested_job_runs_and_cleans_both_owned_scopes(self):
        inner=self.request(['-c','print("NESTED_OWNED_OK",flush=True)'],stdoutPath=str(self.folder/'inner.stdout'),stderrPath=str(self.folder/'inner.stderr'))
        code='import importlib.util,json,sys;spec=importlib.util.spec_from_file_location("owned_inner",sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);r=m.supervise(m.Request(**json.loads(sys.argv[2])));print(json.dumps(r),flush=True);sys.exit(0 if m.terminal_receipt(r) else 125)'
        receipt=owned.supervise(self.request(['-c',code,str(HELPER),json.dumps(inner.__dict__)]))
        self.assertTrue(owned.terminal_receipt(receipt),receipt)
        self.assertEqual(receipt['targetExitCode'],0,receipt)
        self.assertIn('NESTED_OWNED_OK',(self.folder/'inner.stdout').read_text())


if __name__=='__main__':unittest.main()
