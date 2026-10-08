"""Build/test-only Windows Job Object supervision, using the existing Python runtime.

The target is created suspended *inside* a private kill-on-close job via JOB_LIST.
No PID/name discovery, taskkill, breakaway, privilege or security-setting changes.
Actual Core/test proof PIDs are never replaced with this supervisor's identity.
Microsoft: https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from dataclasses import dataclass, field
import json
import math
import ntpath
import os
import platform
import struct
import sysconfig
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

# Fixed-width Windows ABI types, including when API-fake tests run on Unix.
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('nLength', DWORD), ('lpSecurityDescriptor', HANDLE), ('bInheritHandle', BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', HANDLE), ('lpDesktop', HANDLE), ('lpTitle', HANDLE),
                ('dwX', DWORD), ('dwY', DWORD), ('dwXSize', DWORD), ('dwYSize', DWORD),
                ('dwXCountChars', DWORD), ('dwYCountChars', DWORD), ('dwFillAttribute', DWORD),
                ('dwFlags', DWORD), ('wShowWindow', WORD), ('cbReserved2', WORD),
                ('lpReserved2', HANDLE), ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', ULONG_PTR), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                   'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', DWORD), ('TotalProcesses', DWORD),
                ('ActiveProcesses', DWORD), ('TotalTerminatedProcesses', DWORD)]


class SupervisionError(RuntimeError):
    pass


DIAGNOSTIC_LAUNCH_STAGES = frozenset({'not-started', 'job-setup', 'log-open', 'target-setup',
    'target-create', 'target-membership', 'target-resume', 'launched'})


def windows_api():
    if os.name != 'nt':
        raise SupervisionError('Owned native launcher requires Windows; no compatibility fallback is permitted')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    definitions = {
        'CreateJobObjectW': ([HANDLE, ctypes.c_wchar_p], HANDLE),
        'SetInformationJobObject': ([HANDLE, ctypes.c_int, HANDLE, DWORD], BOOL),
        'QueryInformationJobObject': ([HANDLE, ctypes.c_int, HANDLE, DWORD, HANDLE], BOOL),
        'IsProcessInJob': ([HANDLE, HANDLE, ctypes.POINTER(BOOL)], BOOL),
        'TerminateJobObject': ([HANDLE, ctypes.c_uint], BOOL),
        'TerminateProcess': ([HANDLE, ctypes.c_uint], BOOL),
        'CloseHandle': ([HANDLE], BOOL),
        'WaitForSingleObject': ([HANDLE, DWORD], DWORD),
        'ResumeThread': ([HANDLE], DWORD),
        'GetExitCodeProcess': ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
        'GetFileSizeEx': ([HANDLE, ctypes.POINTER(ctypes.c_int64)], BOOL),
        'CreateFileW': ([ctypes.c_wchar_p, DWORD, DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), DWORD, DWORD, HANDLE], HANDLE),
        'InitializeProcThreadAttributeList': ([HANDLE, DWORD, DWORD, ctypes.POINTER(SIZE_T)], BOOL),
        'UpdateProcThreadAttribute': ([HANDLE, DWORD, ULONG_PTR, HANDLE, SIZE_T, HANDLE, HANDLE], BOOL),
        'DeleteProcThreadAttributeList': ([HANDLE], None),
        'CreateProcessW': ([ctypes.c_wchar_p, ctypes.c_wchar_p, HANDLE, HANDLE, BOOL, DWORD, HANDLE,
                            ctypes.c_wchar_p, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)], BOOL),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def fully_qualified_windows_path(value):
    # Python 3.11/3.12 considered a single leading slash absolute; Windows
    # resolves it against the current drive. Never accept that ambiguous form.
    if not isinstance(value, str) or not value or '\0' in value:
        return False
    drive, tail = ntpath.splitdrive(value)
    return bool(drive and tail.startswith(('\\', '/')) and ntpath.isabs(value))


@dataclass
class Request:
    executable: str
    arguments: list[str]
    workingDirectory: str
    stdoutPath: str
    stderrPath: str
    timeoutSeconds: float
    budgetLabel: str
    environment: dict[str, str | None] = field(default_factory=dict)
    drainSeconds: float = 10
    terminationSeconds: float = 10
    maxLogBytes: int = 256 * 1024 * 1024
    controlInput: bool = False
    receiptPath: str | None = None
    requestId: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self):
        if not isinstance(self.requestId, str) or len(self.requestId) != 32 or any(c not in '0123456789abcdef' for c in self.requestId):
            raise ValueError('Owned request ID must be a fresh 128-bit hexadecimal identifier')
        for value in (self.timeoutSeconds, self.drainSeconds, self.terminationSeconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Every supervision budget must be finite and positive')
        if self.timeoutSeconds > 86400 or self.drainSeconds > 600 or self.terminationSeconds > 600:
            raise ValueError('Supervision budget exceeds the supported 24-hour execution or 10-minute cleanup cap')
        if not isinstance(self.budgetLabel, str) or not self.budgetLabel.strip():
            raise ValueError('An explicit execution budget label is required')
        if not isinstance(self.arguments, list) or any(not isinstance(a, str) or '\0' in a for a in self.arguments):
            raise ValueError('Native arguments must be a list of strings without NUL')
        for value in (self.executable, self.workingDirectory, self.stdoutPath, self.stderrPath):
            if not fully_qualified_windows_path(value):
                raise ValueError('Owned launch and log paths must be absolute Windows paths')
        if self.receiptPath is not None and not fully_qualified_windows_path(self.receiptPath):
            raise ValueError('Receipt path must be fully qualified and contain no NUL')
        if not isinstance(self.controlInput, bool):
            raise ValueError('Control input flag must be a boolean')
        normalized = lambda path: ntpath.normcase(ntpath.normpath(path))
        outputs = [self.stdoutPath, self.stderrPath] + ([self.receiptPath] if self.receiptPath is not None else [])
        if any(normalized(path) == normalized(self.executable) for path in outputs):
            raise ValueError('An owned output must not overwrite the requested executable')
        if self.receiptPath is not None and any(normalized(self.receiptPath) == normalized(path) for path in (self.stdoutPath, self.stderrPath)):
            raise ValueError('Receipt and target output paths must be distinct')
        if isinstance(self.maxLogBytes, bool) or not isinstance(self.maxLogBytes, int) or not 1024 <= self.maxLogBytes <= 1024**3:
            raise ValueError('Monitored per-stream log limit must be between 1 KiB and 1 GiB')
        if not isinstance(self.environment, dict):
            raise ValueError('Environment changes must be a mapping')
        for key, value in self.environment.items():
            if not isinstance(key, str) or not key or '=' in key or '\0' in key or (value is not None and (not isinstance(value, str) or '\0' in value)):
                raise ValueError('Invalid target environment entry')


def direct_child_command(command, environment, *, windows=None, executable=None, base_executable=None, frozen=None):
    """Same venv/base rule as verify_frozen_core_service; never rewrite a frozen EXE."""
    result = list(command)
    windows = os.name == 'nt' if windows is None else windows
    executable = sys.executable if executable is None else executable
    base_executable = getattr(sys, '_base_executable', sys.executable) if base_executable is None else base_executable
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    same = lambda a, b: ntpath.normcase(ntpath.abspath(a)) == ntpath.normcase(ntpath.abspath(b))
    if windows and result and base_executable and not frozen and same(result[0], executable) and not same(base_executable, executable):
        result[0] = base_executable
        environment['__PYVENV_LAUNCHER__'] = executable
    return result


def command_line(executable, arguments, *, system_directory=None):
    """CRT argv for EXEs; a deliberately narrow, non-expanding batch contract."""
    if ntpath.splitext(executable)[1].casefold() in {'.cmd', '.bat'}:
        values = [executable, *arguments]
        # cmd parses independently of the CRT. Do not silently interpret its data.
        if any(any(c in value for c in '"%!\r\n\0&|<>^') for value in values):
            raise ValueError('Batch argv contains unsupported shell syntax; use its executable entry point')
        system_directory = system_directory or ntpath.join(os.environ['SystemRoot'], 'System32')
        actual = ntpath.join(system_directory, 'cmd.exe')
        text = subprocess.list2cmdline([actual]) + ' /d /s /v:off /c "' + ' '.join('"' + value + '"' for value in values) + '"'
    else:
        actual, text = executable, subprocess.list2cmdline([executable, *arguments])
    if len(text) >= 32767:
        raise ValueError('Native command exceeds Windows command-line limit')
    return actual, text


class WindowsContainment:
    def __init__(self, api=None, *, makedirs=None):
        self.api = windows_api() if api is None else api
        self.makedirs = makedirs or (lambda path: Path(path).parent.mkdir(parents=True, exist_ok=True))
        self.job = self.process = self.thread = self.stdin = self.stdout = self.stderr = None
        self.pid = None
        self.launch_executable = None
        self.closed = False
        self.diagnostic_launch_stage = 'not-started'

    def check(self, success, operation):
        if not success:
            code = ctypes.get_last_error() if os.name == 'nt' else getattr(self.api, 'last_error', 0)
            # Never include argv/environment values in errors or receipts.
            raise SupervisionError(f'{operation} failed (Windows error {code})')

    def open_log(self, path):
        self.makedirs(path)
        security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, 1)
        handle = self.api.CreateFileW(path, 0x40000000 | 0x80, 1 | 2, ctypes.byref(security), 2, 0x80, None)
        self.check(handle not in (None, 0, ctypes.c_void_p(-1).value), 'Open owned output log')
        return handle

    def launch(self, request):
        if self.job is not None or self.closed:
            raise SupervisionError('Containment is single use')
        attributes = None
        initialized = False
        membership_verified = False
        self.diagnostic_launch_stage = 'job-setup'
        self.job = self.api.CreateJobObjectW(None, None)  # private, unnamed, non-inheritable
        self.check(self.job, 'Create private job')
        try:
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway flags
            self.check(self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), 'Set job kill-on-close')
            self.diagnostic_launch_stage = 'log-open'
            self.stdout = self.open_log(request.stdoutPath)
            self.stderr = self.stdout if ntpath.normcase(request.stdoutPath) == ntpath.normcase(request.stderrPath) else self.open_log(request.stderrPath)
            self.diagnostic_launch_stage = 'target-setup'
            security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, 1)
            self.stdin = self.api.CreateFileW('NUL', 0x80000000, 1 | 2, ctypes.byref(security), 3, 0x80, None)
            if self.stdin == ctypes.c_void_p(-1).value:
                self.stdin = None
            self.check(self.stdin, 'Open target null input')
            size = SIZE_T()
            self.api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            self.check(size.value, 'Size process attributes')
            attributes = ctypes.create_string_buffer(size.value)
            self.check(self.api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)), 'Initialize process attributes')
            initialized = True
            handle_values = list(dict.fromkeys((self.stdin, self.stdout, self.stderr)))
            handles = (HANDLE * len(handle_values))(*handle_values)
            self.check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None), 'Restrict inherited handles')
            jobs = (HANDLE * 1)(self.job)
            self.check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None), 'Set atomic private-job assignment')
            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = self.stdin, self.stdout, self.stderr
            startup.lpAttributeList = ctypes.cast(attributes, HANDLE)
            # Case-insensitive replacement is necessary for Windows PATH/Python vars.
            env = {key.upper(): (key, value) for key, value in os.environ.items()}
            for key, value in request.environment.items():
                if value is None:
                    env.pop(key.upper(), None)
                else:
                    env[key.upper()] = (key, value)
            target_environment = dict(env.values())
            command = direct_child_command([request.executable, *request.arguments], target_environment)
            self.launch_executable, text = command_line(command[0], command[1:])
            environment = ctypes.create_unicode_buffer('\0'.join(key + '=' + value for key, value in sorted(target_environment.items(), key=lambda item: item[0].upper())) + '\0')
            information = PROCESS_INFORMATION()
            self.diagnostic_launch_stage = 'target-create'
            self.check(self.api.CreateProcessW(self.launch_executable, ctypes.create_unicode_buffer(text), None, None, True,
                       0x4 | 0x400 | 0x80000 | 0x08000000, environment, request.workingDirectory,
                       ctypes.byref(startup), ctypes.byref(information)), 'Create suspended target inside private job')
            self.process, self.thread, self.pid = information.hProcess, information.hThread, information.dwProcessId
            member = BOOL()
            self.diagnostic_launch_stage = 'target-membership'
            self.check(self.api.IsProcessInJob(self.process, self.job, ctypes.byref(member)), 'Verify private-job membership')
            self.check(member.value, 'Target private-job membership; resume refused')
            membership_verified = True
            self.diagnostic_launch_stage = 'target-resume'
            self.check(self.api.ResumeThread(self.thread) != 0xFFFFFFFF, 'Resume owned target')
            self.diagnostic_launch_stage = 'launched'
        except BaseException as error:
            # A membership-check failure must never leave an uncontained
            # suspended target alive. This exact held handle is ours.
            if self.process and not membership_verified:
                try:
                    self.check(self.api.TerminateProcess(self.process, 1), 'Terminate unverified suspended target')
                    self.check(self.api.WaitForSingleObject(self.process, int(request.terminationSeconds * 1000)) == 0,
                               'Wait for unverified suspended target termination')
                except BaseException as cleanup_error:
                    error.add_note(str(cleanup_error))
            raise
        finally:
            # The values referenced by the list stay alive through deletion.
            if initialized:
                self.api.DeleteProcThreadAttributeList(attributes)

    def root_exited(self):
        status = self.api.WaitForSingleObject(self.process, 0)
        if status == 0:
            return True
        self.check(status == 258, 'Observe owned root handle')
        return False

    def exit_code(self):
        code = DWORD()
        self.check(self.api.GetExitCodeProcess(self.process, ctypes.byref(code)), 'Read owned root exit code')
        return code.value

    def active_processes(self):
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        self.check(self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None), 'Read private-job accounting')
        return info.ActiveProcesses

    def logs_exceeded(self, maximum):
        for handle in set((self.stdout, self.stderr)):
            size = ctypes.c_int64()
            self.check(self.api.GetFileSizeEx(handle, ctypes.byref(size)), 'Read owned log size')
            if size.value > maximum:
                return True
        return False

    def terminate(self):
        self.check(self.api.TerminateJobObject(self.job, 1), 'Terminate private job')

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        # Last owner closure kills the job even when explicit termination failed.
        # Close only handles returned to this owner; no PID or name discovery.
        for handle in dict.fromkeys((self.job, self.thread, self.process, self.stdin, self.stdout, self.stderr)):
            if handle:
                try:
                    self.check(self.api.CloseHandle(handle), 'Close owned handle')
                except Exception as error:
                    errors.append(str(error))
        if errors:
            raise SupervisionError('; '.join(errors))


def record_target_exit(receipt, raw_code):
    # Keep the exact Windows DWORD as evidence and the equivalent signed Int32
    # for PowerShell LASTEXITCODE/.NET compatibility. Never overflow a cast.
    receipt['targetExitCodeUnsigned'] = raw_code & 0xFFFFFFFF
    receipt['targetExitCode'] = ctypes.c_int32(receipt['targetExitCodeUnsigned']).value


def supervise(request, *, containment=None, cancelled=None, clock=time.monotonic, sleep=time.sleep):
    request.validate()
    cancelled = cancelled or threading.Event()
    native = containment if containment is not None else WindowsContainment()
    started = clock()
    receipt = {'schemaVersion': 1, 'requestId': request.requestId, 'supervisorPid': os.getpid(), 'launchTargetPid': None,
               'requestedExecutable': request.executable, 'launchExecutable': None, 'targetExitCode': None, 'targetExitCodeUnsigned': None,
               'outcome': 'supervision-error', 'budgetLabel': request.budgetLabel,
               'executionLimitSeconds': request.timeoutSeconds, 'elapsedSeconds': 0,
               'confirmedTreeEmpty': False, 'errors': []}
    launched = False
    root_exit_at = None
    try:
        if cancelled.is_set():
            receipt['outcome'] = 'cancelled-before-launch'
        else:
            native.launch(request)
            launched = True
            receipt.update(launchTargetPid=native.pid, launchExecutable=native.launch_executable)
            while True:
                if cancelled.is_set():
                    receipt['outcome'] = 'cancelled'; break
                if clock() - started >= request.timeoutSeconds:
                    receipt['outcome'] = 'execution-timeout'; break
                if native.logs_exceeded(request.maxLogBytes):
                    receipt['outcome'] = 'log-size-limit'; break
                exited, active = native.root_exited(), native.active_processes()
                if exited:
                    record_target_exit(receipt, native.exit_code())
                    if active == 0:
                        receipt['confirmedTreeEmpty'] = True
                        receipt['outcome'] = 'completed' if receipt['targetExitCode'] == 0 else 'target-exited-nonzero'
                        break
                    if receipt['targetExitCode'] != 0:
                        receipt['outcome'] = 'target-exited-nonzero'; break
                    if root_exit_at is None:
                        root_exit_at = clock()
                    if clock() - root_exit_at >= request.drainSeconds:
                        receipt['outcome'] = 'descendant-drain-timeout'; break
                sleep(.05)
    except BaseException as error:
        receipt['outcome'] = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'supervision-error'
        receipt['errors'].append('Supervision: ' + str(error) + ''.join('; ' + note for note in getattr(error, '__notes__', [])))
    finally:
        if launched and not receipt['confirmedTreeEmpty']:
            try:
                native.terminate()
                stop_by = clock() + request.terminationSeconds
                while True:
                    if native.active_processes() == 0:
                        receipt['confirmedTreeEmpty'] = True; break
                    if clock() >= stop_by:
                        receipt['errors'].append('Private job did not become empty within termination budget'); break
                    sleep(.05)
                if receipt['targetExitCode'] is None and native.root_exited():
                    record_target_exit(receipt, native.exit_code())
            except BaseException as error:
                receipt['errors'].append('Termination: ' + str(error))
        try:
            native.close()
        except BaseException as error:
            receipt['errors'].append('Handle cleanup: ' + str(error))
        receipt['elapsedSeconds'] = round(clock() - started, 6)
        # Observation only: legacy/test containments need not implement it, and
        # neither receipt validation nor terminal acceptance depends on it.
        receipt['diagnosticLaunchStage'] = 'unknown'
        try:
            stage = getattr(native, 'diagnostic_launch_stage', 'unknown')
            if type(stage) is str and stage in DIAGNOSTIC_LAUNCH_STAGES:
                receipt['diagnosticLaunchStage'] = stage
        except BaseException:
            pass  # Diagnostic observation must not replace the owned outcome.
    return receipt


OUTCOMES = {'completed', 'target-exited-nonzero', 'execution-timeout', 'cancelled',
            'cancelled-before-launch', 'descendant-drain-timeout', 'supervision-error', 'log-size-limit'}


RECEIPT_KEYS = {'schemaVersion', 'requestId', 'supervisorPid', 'launchTargetPid', 'requestedExecutable', 'launchExecutable',
                'targetExitCode', 'targetExitCodeUnsigned', 'outcome', 'budgetLabel', 'executionLimitSeconds', 'elapsedSeconds',
                'confirmedTreeEmpty', 'errors'}


def receipt_shape(receipt):
    integer = lambda value: isinstance(value, int) and not isinstance(value, bool)
    finite = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return (isinstance(receipt, dict) and RECEIPT_KEYS.issubset(receipt) and type(receipt.get('schemaVersion')) is int and receipt['schemaVersion'] == 1
            and isinstance(receipt.get('requestId'), str) and len(receipt['requestId']) == 32
            and all(c in '0123456789abcdef' for c in receipt['requestId'])
            and integer(receipt.get('supervisorPid')) and receipt['supervisorPid'] > 0
            and (receipt.get('launchTargetPid') is None or (integer(receipt['launchTargetPid']) and receipt['launchTargetPid'] > 0))
            and (receipt.get('targetExitCode') is None or (integer(receipt['targetExitCode']) and -(2**31) <= receipt['targetExitCode'] < 2**31))
            and (receipt['targetExitCodeUnsigned'] is None if receipt['targetExitCode'] is None else
                 integer(receipt['targetExitCodeUnsigned']) and receipt['targetExitCodeUnsigned'] == (receipt['targetExitCode'] & 0xFFFFFFFF))
            and receipt.get('outcome') in OUTCOMES and isinstance(receipt.get('confirmedTreeEmpty'), bool)
            and isinstance(receipt.get('errors'), list) and all(isinstance(error, str) for error in receipt['errors'])
            and isinstance(receipt.get('requestedExecutable'), str) and bool(receipt['requestedExecutable'])
            and (receipt.get('launchExecutable') is None or isinstance(receipt['launchExecutable'], str))
            and isinstance(receipt.get('budgetLabel'), str) and bool(receipt['budgetLabel'])
            and finite(receipt.get('executionLimitSeconds')) and receipt['executionLimitSeconds'] > 0
            and finite(receipt.get('elapsedSeconds')) and receipt['elapsedSeconds'] >= 0)


def cleanup_receipt(receipt):
    return (receipt_shape(receipt) and receipt['confirmedTreeEmpty'] and not receipt['errors']
            and receipt['launchTargetPid'] is not None and receipt['supervisorPid'] != receipt['launchTargetPid']
            and bool(receipt['launchExecutable']))


def terminal_receipt(receipt):
    return (cleanup_receipt(receipt) and receipt['targetExitCode'] is not None and
            ((receipt['outcome'] == 'completed' and receipt['targetExitCode'] == 0) or
             (receipt['outcome'] == 'target-exited-nonzero' and receipt['targetExitCode'] != 0)))


def enforce_deadline(completed, seconds, *, hard_exit=os._exit):
    # Independent of native calls/output IO. Exiting this sole, non-inheritable
    # job-handle owner is the final kill-on-close backstop, never a success proof.
    if not completed.wait(seconds):
        hard_exit(125)


def watch_control(cancelled, stream, completed=None, termination_seconds=10, *, hard_exit=os._exit):
    try:
        stream.readline()
    except Exception:
        pass
    finally:
        cancelled.set()  # EOF or one command: target receives NUL, not this pipe.
    if completed is not None:
        enforce_deadline(completed, termination_seconds + 5, hard_exit=hard_exit)


def write_receipt(path, receipt):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(receipt, stream, ensure_ascii=True, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_runtime():
    if not ((3, 11) <= sys.version_info[:2] < (3, 15) and platform.python_implementation() == 'CPython'
            and struct.calcsize('P') == 8 and platform.machine().casefold() in {'amd64', 'x86_64'}
            and not sysconfig.get_config_var('Py_GIL_DISABLED')):
        raise SupervisionError('Owned native launcher requires the existing standard x64 CPython 3.11-3.14 prerequisite')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-base64', required=True)
    arguments = parser.parse_args()
    request = Request(**json.loads(base64.b64decode(arguments.request_base64, validate=True).decode('utf-8')))
    request.validate()
    validate_runtime()
    cancelled, completed = threading.Event(), threading.Event()
    threading.Thread(target=enforce_deadline, args=(completed,
        request.timeoutSeconds + request.drainSeconds + request.terminationSeconds + 20), daemon=True).start()
    if request.controlInput:
        threading.Thread(target=watch_control, args=(cancelled, sys.stdin, completed, request.terminationSeconds), daemon=True).start()
    try:
        receipt = supervise(request, cancelled=cancelled)
        if request.receiptPath:
            write_receipt(request.receiptPath, receipt)
        else:
            print(json.dumps(receipt, ensure_ascii=True, sort_keys=True), flush=True)
        return 0 if terminal_receipt(receipt) else 125
    finally:
        completed.set()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Owned process launcher failed: ' + str(error), file=sys.stderr, flush=True)
        sys.exit(125)
