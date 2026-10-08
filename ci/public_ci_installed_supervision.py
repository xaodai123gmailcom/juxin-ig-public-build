"""Failure-only allowlist for exact, runner-local installed ownership observations.

Raw receipts and bindings never leave the runner. No directory discovery, latest
file selection, message parsing, retries, or acceptance decisions happen here.
"""
from __future__ import annotations
import json
import math
import ntpath
from pathlib import Path
import re

from public_ci_common import regular, write_json, load_source_module

PHASES = ('nsis', 'native-smoke', 'recovery')
LOGS = {'nsis': 'installed-nsis.log', 'native-smoke': 'installed-openvino-smoke.log.stdout',
        'recovery': 'installed-recovery-r64-stdout-full.log'}
BUDGETS = {'nsis': (180, 'Native command orchestration cap (narrower child deadlines unchanged)'),
           'native-smoke': (180, 'Frozen OpenVINO existing execution deadline'),
           'recovery': (540, 'Native command orchestration cap (narrower child deadlines unchanged)')}
ADAPTER_STAGES = {'supervisor-start', 'supervisor-wait', 'progress-log-open', 'progress-log-read',
                  'final-log-read', 'receipt-read', 'receipt-validation', 'returned'}
LAUNCH_STAGES = {'unknown', 'not-started', 'job-setup', 'log-open', 'target-setup', 'target-create',
                 'target-membership', 'target-resume', 'launched'}
VERIFIER_PHASES = {'started', 'source-binding', 'import-setup', 'seed', 'launch', 'debugger',
                   'renderer', 'preload', 'readiness', 'api', 'shutdown', 'validation', 'complete'}


def identifier(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None


def prepare(state, directory):
    """Best effort; observation setup must not change the actual gate result."""
    try:
        if not identifier(state.get('nonce')):
            return []
        status, start = read_bounded(Path(directory) / 'installed-start.json', 16384)
        if status != 'read' or not stage_start(start, state):
            return []
        invocation = start['supervision_invocation']
        destination = Path(directory) / 'installed-supervision' / invocation
        destination.mkdir(parents=True, exist_ok=False)
        regular(destination, directory=True)
        write_json(Path(directory) / 'installed-supervision-context.json',
                   {'schema': 1, 'runNonce': state['nonce'], 'invocationId': invocation})
        return ['-DiagnosticDirectory', str(destination), '-DiagnosticRunNonce', state['nonce'],
                '-DiagnosticInvocationId', invocation]
    except Exception:
        return []


def stage_start(value, state):
    # run_stage creates this once with exclusive creation before the actual
    # installed action. A second invocation must have a new stage-start token;
    # a same-run old context cannot become the expected current invocation.
    return (type(value) is dict and set(value) == {'nonce', 'started_ns', 'supervision_invocation'} and
            value['nonce'] == state.get('nonce') and identifier(value['supervision_invocation']) and
            type(value['started_ns']) is int and value['started_ns'] > 0)


def receipt_path_matches(actual, expected_log):
    # Windows APIs may preserve equivalent slash/case spellings. Constrain the
    # normalized entire path, not just a basename, to this exact phase's log.
    return (type(actual) is str and re.fullmatch(
        re.escape(ntpath.normcase(ntpath.normpath(str(expected_log)))) + r'\.owned-[0-9a-f]{32}\.json',
        ntpath.normcase(ntpath.normpath(actual))) is not None)


def read_bounded(path, maximum=65536):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate observation key')
            result[key] = value
        return result
    try:
        with regular(path).open('rb') as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            return 'malformed', None
        return 'read', json.loads(data.decode('utf-8-sig'), object_pairs_hook=pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except FileNotFoundError:
        return 'missing', None
    except (ValueError, UnicodeError):
        return 'malformed', None
    except Exception:
        return 'unreadable', None


def envelope(value, context, phase, event):
    return (type(value) is dict and set(value) ==
            {'schema', 'runNonce', 'invocationId', 'phase', 'event', 'observation'} and
            type(value['schema']) is int and value['schema'] == 1 and
            value['runNonce'] == context['runNonce'] and value['invocationId'] == context['invocationId'] and
            value['phase'] == phase and value['event'] == event and type(value['observation']) is dict)


def empty_phase(phase):
    return {'phase': phase, 'evidence_state': 'not-observed', 'entered': False,
            'request_bound': False, 'adapter_state': 'not-observed', 'adapter_stage': None,
            'supervisor_started': None, 'supervisor_exit_code': None,
            'outer_deadline_exceeded': None, 'cleanup_wait_timed_out': None,
            'receipt_shape_valid': False, 'receipt_matches_request': False,
            'terminal_receipt': False, 'cleanup_confirmed': False, 'confirmed_tree_empty': None,
            'target_launched': None, 'outcome': None, 'target_exit_code': None, 'error_count': None,
            'launch_stage': None, 'log_state': 'not-observed', 'success_marker': 'not-observed',
            'verifier_phase': None}


def exit_code(value):
    return value is None or (type(value) is int and -(2**31) <= value < 2**31)


def marker_observation(path, phase, result):
    # Observe only exact fixed syntax in a bounded tail. Absence means not
    # observed in this tail, not proof that the verifier never reached a phase.
    try:
        with regular(path).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 32768))
            lines = stream.read(32768).decode('utf-8', errors='replace').splitlines()
        result['log_state'] = 'readable-tail'
        if phase == 'recovery':
            result['success_marker'] = ('observed' if any(line.startswith('INSTALLED_RECOVERY_R64=PASS ')
                                         for line in lines) else 'not-observed-in-tail')
            for line in lines:
                prefix = 'INSTALLED_RECOVERY_PHASE='
                if line.startswith(prefix) and line[len(prefix):] in VERIFIER_PHASES:
                    result['verifier_phase'] = line[len(prefix):]
        else:
            result['success_marker'] = 'not-applicable'
    except FileNotFoundError:
        result['log_state'] = 'missing'
    except Exception:
        result['log_state'] = 'unreadable'


def phase_observation(root, directory, context, phase, runner):
    result = empty_phase(phase)
    state, entered = read_bounded(directory / (phase + '-entered.json'), 16384)
    if state != 'read':
        result['evidence_state'] = 'entry-' + state
        return result
    if not envelope(entered, context, phase, 'entered') or entered['observation'] != {}:
        result['evidence_state'] = 'entry-invalid'
        return result
    result['entered'] = True
    state, binding = read_bounded(directory / (phase + '-bound.json'), 16384)
    if state != 'read':
        result['evidence_state'] = 'binding-' + state
        return result
    if not envelope(binding, context, phase, 'bound'):
        result['evidence_state'] = 'binding-invalid'
        return result
    bound = binding['observation']
    expected_log = root / 'installer-output' / LOGS[phase]
    timeout, label = BUDGETS[phase]
    if (set(bound) != {'requestId', 'receiptPath', 'executable', 'timeoutSeconds', 'budgetLabel'} or
        not identifier(bound['requestId']) or type(bound['receiptPath']) is not str or
        type(bound['executable']) is not str or not bound['executable'] or
        type(bound['timeoutSeconds']) not in (int, float) or not math.isfinite(bound['timeoutSeconds']) or
        bound['timeoutSeconds'] != timeout or bound['budgetLabel'] != label or
        not receipt_path_matches(bound['receiptPath'], expected_log)):
        result['evidence_state'] = 'binding-invalid'
        return result
    result['request_bound'] = True
    state, settled = read_bounded(directory / (phase + '-settled.json'), 16384)
    result['adapter_state'] = state
    if state == 'read':
        info = settled.get('observation', {}) if type(settled) is dict else {}
        if (envelope(settled, context, phase, 'settled') and set(info) ==
            {'requestId', 'adapterStage', 'supervisorStarted', 'supervisorExitCode',
             'outerDeadlineExceeded', 'cleanupWaitTimedOut'} and info['requestId'] == bound['requestId'] and
            type(info['adapterStage']) is str and info['adapterStage'] in ADAPTER_STAGES and
            all(type(info[key]) is bool for key in ('supervisorStarted', 'outerDeadlineExceeded', 'cleanupWaitTimedOut')) and
            exit_code(info['supervisorExitCode']) and
            (info['supervisorStarted'] or info['supervisorExitCode'] is None)):
            result.update(adapter_stage=info['adapterStage'], supervisor_started=info['supervisorStarted'],
                          supervisor_exit_code=info['supervisorExitCode'], outer_deadline_exceeded=info['outerDeadlineExceeded'],
                          cleanup_wait_timed_out=info['cleanupWaitTimedOut'])
        else:
            result['adapter_state'] = 'invalid'
    state, receipt = read_bounded(Path(bound['receiptPath']))
    if state != 'read':
        result['evidence_state'] = 'receipt-' + state
        return result
    try:
        valid_shape = (type(receipt) is dict and set(receipt) == runner.RECEIPT_KEYS | {'diagnosticLaunchStage'} and
            runner.receipt_shape(receipt) and type(receipt['diagnosticLaunchStage']) is str and
            receipt['diagnosticLaunchStage'] in LAUNCH_STAGES and len(receipt['errors']) <= 65536)
    except Exception:
        valid_shape = False
    if not valid_shape:
        result['evidence_state'] = 'receipt-malformed'
        return result
    result['receipt_shape_valid'] = True
    if (receipt['requestId'] != bound['requestId'] or
        receipt['requestedExecutable'].casefold() != bound['executable'].casefold() or
        receipt['budgetLabel'] != bound['budgetLabel'] or receipt['executionLimitSeconds'] != bound['timeoutSeconds'] or
        receipt['supervisorPid'] == receipt['launchTargetPid']):
        result['evidence_state'] = 'receipt-mismatch'
        return result
    result.update(evidence_state='receipt-matched', receipt_matches_request=True,
                  terminal_receipt=runner.terminal_receipt(receipt), cleanup_confirmed=runner.cleanup_receipt(receipt),
                  confirmed_tree_empty=receipt['confirmedTreeEmpty'], target_launched=receipt['launchTargetPid'] is not None,
                  outcome=receipt['outcome'], target_exit_code=receipt['targetExitCode'], error_count=len(receipt['errors']),
                  launch_stage=receipt['diagnosticLaunchStage'])
    # A readable file alone does not establish launch. Do not attribute stale
    # marker text to a request that never launched its target.
    if result['target_launched']:
        marker_observation(expected_log, phase, result)
    return result


def collect(state, directory, root):
    """Construct only typed allowlisted observations; never copy raw fields."""
    result = {'state': 'context-missing', 'phases': []}
    try:
        status, start = read_bounded(Path(directory) / 'installed-start.json', 16384)
        if status != 'read' or not stage_start(start, state):
            return {'state': 'stage-binding-invalid', 'phases': []}
        status, context = read_bounded(Path(directory) / 'installed-supervision-context.json', 16384)
        if status != 'read':
            result['state'] = 'context-' + status
            return result
        if (type(context) is not dict or set(context) != {'schema', 'runNonce', 'invocationId'} or
            type(context['schema']) is not int or context['schema'] != 1 or
            not identifier(context['runNonce']) or not identifier(context['invocationId'])):
            result['state'] = 'context-invalid'
            return result
        if context['runNonce'] != state['nonce'] or context['invocationId'] != start['supervision_invocation']:
            result['state'] = 'context-mismatch'
            return result
        destination = Path(directory) / 'installed-supervision' / context['invocationId']
        runner = load_source_module('owned_process')
        rows = [phase_observation(Path(root), destination, context, phase, runner) for phase in PHASES]
        result.update(state='observed', phases=rows)
        if len(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2).encode()) > 8192:
            raise ValueError('Installed observation exceeded bound')
    except Exception:
        result = {'state': 'capture-error', 'phases': []}
    return result
