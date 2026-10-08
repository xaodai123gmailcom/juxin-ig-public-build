"""Allowlist contracts plus real Windows PowerShell adapter execution.

The portable fixtures establish diagnostic behavior only. Actual installed
acceptance and original native ownership gates remain separate requirements.
"""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import public_ci_installed_supervision as observation
import public_ci as ci
import public_ci_common as common


class Snapshot:
    def __init__(self, directory, phase='recovery'):
        self.root = Path(directory)
        self.state = {'nonce': 'a' * 32}
        (self.root / 'installed-start.json').write_text(json.dumps(
            {'nonce':self.state['nonce'], 'started_ns':1, 'supervision_invocation':uuid.uuid4().hex}))
        self.arguments = observation.prepare(self.state, self.root)
        self.directory = Path(self.arguments[1])
        self.context = json.loads((self.root / 'installed-supervision-context.json').read_text())
        self.phase = phase
        self.log = self.root / 'installer-output' / observation.LOGS[phase]
        self.log.parent.mkdir(exist_ok=True)
        self.receipt_path = Path(str(self.log) + '.owned-' + 'c' * 32 + '.json')
        timeout, label = observation.BUDGETS[phase]
        self.bound = dict(requestId='b' * 32, receiptPath=str(self.receipt_path),
                          executable=str(self.root / 'private-user-python.exe'), timeoutSeconds=timeout, budgetLabel=label)
        self.settled = dict(requestId=self.bound['requestId'], adapterStage='receipt-validation',
                            supervisorStarted=True, supervisorExitCode=125,
                            outerDeadlineExceeded=False, cleanupWaitTimedOut=False)
        self.receipt = dict(schemaVersion=1, requestId=self.bound['requestId'], supervisorPid=101,
                            launchTargetPid=102, requestedExecutable=self.bound['executable'],
                            launchExecutable=self.bound['executable'], targetExitCode=0, targetExitCodeUnsigned=0,
                            outcome='descendant-drain-timeout', budgetLabel=label, executionLimitSeconds=timeout,
                            elapsedSeconds=2.5, confirmedTreeEmpty=True, errors=[], diagnosticLaunchStage='launched')
        self.event('entered', {})
        self.event('bound', self.bound)
        self.event('settled', self.settled)
        self.write_receipt()
        self.log.write_text('INSTALLED_RECOVERY_PHASE=shutdown\nINSTALLED_RECOVERY_R64=PASS private-json-payload\n')

    def event(self, name, value, **changes):
        payload = dict(self.context, phase=self.phase, event=name, observation=value)
        payload.update(changes)
        (self.directory / (self.phase + '-' + name + '.json')).write_text(json.dumps(payload))

    def write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def result(self):
        summary = observation.collect(self.state, self.root, self.root)
        self.assert_private(summary)
        return next(row for row in summary['phases'] if row['phase'] == self.phase)

    def assert_private(self, value):
        text = json.dumps(value)
        for secret in (str(self.root), 'private-user-python', 'private-json-payload', 'credential-token',
                       self.bound['requestId'], self.context['invocationId']):
            if secret in text:
                raise AssertionError('Private evidence escaped')


class InstalledSupervisionContracts(unittest.TestCase):
    def test_failure_has_exact_bound_receipt_and_success_marker_with_descendant_drain(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            row = fixture.result()
            self.assertEqual(row['evidence_state'], 'receipt-matched')
            self.assertEqual(row['outcome'], 'descendant-drain-timeout')
            self.assertEqual(row['target_exit_code'], 0)
            self.assertEqual(row['supervisor_exit_code'], 125)
            self.assertTrue(row['receipt_matches_request'])
            self.assertTrue(row['cleanup_confirmed'])
            self.assertFalse(row['terminal_receipt'])
            self.assertEqual(row['success_marker'], 'observed')
            self.assertEqual(row['verifier_phase'], 'shutdown')

    def test_regular_child_failure_remains_terminal_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            fixture.receipt.update(outcome='target-exited-nonzero', targetExitCode=1, targetExitCodeUnsigned=1)
            fixture.settled.update(supervisorExitCode=0, adapterStage='returned')
            fixture.event('settled', fixture.settled); fixture.write_receipt()
            row = fixture.result()
            self.assertTrue(row['terminal_receipt']); self.assertTrue(row['cleanup_confirmed'])
            self.assertEqual(row['target_exit_code'], 1); self.assertEqual(row['supervisor_exit_code'], 0)

    def test_supervisor_failure_does_not_reclassify_terminal_child_success(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            fixture.receipt['outcome'] = 'completed'; fixture.write_receipt()
            row = fixture.result()
            self.assertTrue(row['terminal_receipt']); self.assertEqual(row['supervisor_exit_code'], 125)
            self.assertEqual(row['outcome'], 'completed')

    def test_success_marker_with_handle_cleanup_failure_is_not_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            fixture.receipt.update(outcome='completed', errors=['private credential-token cleanup failure'])
            fixture.write_receipt(); row = fixture.result()
            self.assertEqual(row['success_marker'], 'observed'); self.assertEqual(row['error_count'], 1)
            self.assertFalse(row['terminal_receipt']); self.assertFalse(row['cleanup_confirmed'])
            self.assertTrue(row['confirmed_tree_empty'])

    def test_prelaunch_log_open_and_create_are_fixed_observations_without_log_attribution(self):
        for stage in ('log-open', 'target-create', 'target-membership', 'target-resume'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp)
                fixture.receipt.update(outcome='supervision-error', launchTargetPid=None, launchExecutable=None,
                    targetExitCode=None, targetExitCodeUnsigned=None, confirmedTreeEmpty=False,
                    errors=['credential-token Windows error message'], diagnosticLaunchStage=stage)
                fixture.write_receipt(); row = fixture.result()
                self.assertEqual(row['launch_stage'], stage); self.assertFalse(row['target_launched'])
                self.assertEqual(row['log_state'], 'not-observed'); self.assertEqual(row['error_count'], 1)
                self.assertIsNone(row['target_exit_code'])

    def test_every_supervisor_outcome_is_allowed_only_from_matched_shape(self):
        runner = common.load_source_module('owned_process')
        for outcome in runner.OUTCOMES:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp); fixture.receipt['outcome'] = outcome; fixture.write_receipt()
                self.assertEqual(fixture.result()['outcome'], outcome)

    def test_mismatch_suppresses_outcome_exit_cleanup_and_markers(self):
        for changes in ({'requestId':'d'*32}, {'requestedExecutable':'unrelated.exe'},
                        {'executionLimitSeconds':999}, {'budgetLabel':'unrelated'}, {'supervisorPid':102}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp); fixture.receipt.update(changes); fixture.write_receipt()
                row = fixture.result()
                self.assertEqual(row['evidence_state'], 'receipt-mismatch')
                self.assertTrue(row['receipt_shape_valid']); self.assertFalse(row['receipt_matches_request'])
                self.assertIsNone(row['outcome']); self.assertIsNone(row['target_exit_code'])
                self.assertIsNone(row['error_count']); self.assertFalse(row['cleanup_confirmed'])
                self.assertEqual(row['success_marker'], 'not-observed')

    def test_missing_malformed_oversize_and_unexpected_receipts_are_distinct(self):
        for payload, expected in ((None, 'receipt-missing'), ('{broken', 'receipt-malformed'),
                (' ' * 65537, 'receipt-malformed'), ('[]', 'receipt-malformed'),
                ('{"schemaVersion":1,"schemaVersion":1}', 'receipt-malformed')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp)
                if payload is None: fixture.receipt_path.unlink()
                else: fixture.receipt_path.write_text(payload)
                self.assertEqual(fixture.result()['evidence_state'], expected)
        for key, value in (('private', 'credential-token'), ('schemaVersion', True), ('targetExitCode', True),
                           ('errors', ['x', 123]), ('confirmedTreeEmpty', 1),
                           ('diagnosticLaunchStage', 'credential-token'), ('outcome', [])):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp); fixture.receipt[key] = value; fixture.write_receipt()
                self.assertEqual(fixture.result()['evidence_state'], 'receipt-malformed')

    def test_stale_nonce_or_invocation_cannot_bind_current_phase(self):
        for event in ('entered', 'bound', 'settled'):
            for field in ('runNonce', 'invocationId'):
                with self.subTest(event=event, field=field), tempfile.TemporaryDirectory() as temp:
                    fixture = Snapshot(temp)
                    value = {} if event == 'entered' else getattr(fixture, event)
                    fixture.event(event, value, **{field:'d'*32})
                    row = fixture.result()
                    if event == 'settled':
                        self.assertEqual(row['adapter_state'], 'invalid'); self.assertIsNone(row['supervisor_exit_code'])
                    else:
                        self.assertFalse(row['request_bound']); self.assertIsNone(row['outcome'])
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            self.assertEqual(observation.collect({'nonce':'d'*32}, fixture.root, fixture.root),
                             {'state':'stage-binding-invalid', 'phases':[]})

    def test_same_nonce_old_context_is_not_reused_after_current_setup_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            # Simulate a distinct current installed invocation in the same run.
            start = json.loads((fixture.root/'installed-start.json').read_text())
            start.update(supervision_invocation=uuid.uuid4().hex, started_ns=2)
            (fixture.root/'installed-start.json').write_text(json.dumps(start))
            self.assertEqual(observation.prepare(fixture.state, fixture.root), [])
            self.assertEqual(observation.collect(fixture.state, fixture.root, fixture.root),
                             {'state':'context-mismatch', 'phases':[]})

    def test_windows_equivalent_receipt_path_spelling_is_exactly_constrained(self):
        expected = r'C:\Runner\source\installer-output\installed-nsis.log'
        self.assertTrue(observation.receipt_path_matches(
            'c:/runner/source/installer-output/INSTALLED-NSIS.LOG.owned-' + 'a'*32 + '.json', expected))
        for prefix in (r'C:\Other', r'C:\Runner\source\elsewhere'):
            self.assertFalse(observation.receipt_path_matches(prefix + r'\installed-nsis.log.owned-' + 'a'*32 + '.json', expected))

    def test_no_glob_or_latest_receipt_and_no_out_of_phase_path_read(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            unrelated = fixture.root / 'credential-token.json'; unrelated.write_text(json.dumps(fixture.receipt))
            fixture.receipt_path.unlink()
            self.assertEqual(fixture.result()['evidence_state'], 'receipt-missing')
            fixture.bound['receiptPath'] = str(unrelated); fixture.event('bound', fixture.bound)
            with patch.object(observation, 'read_bounded', wraps=observation.read_bounded) as read:
                self.assertEqual(fixture.result()['evidence_state'], 'binding-invalid')
                self.assertNotIn(unrelated, [call.args[0] for call in read.call_args_list])

    def test_settled_is_strict_and_separate_from_receipt_result(self):
        for key, value in (('supervisorExitCode', 2**31), ('supervisorExitCode', True), ('supervisorStarted', 1),
                           ('outerDeadlineExceeded', 1), ('cleanupWaitTimedOut', 1), ('requestId','d'*32),
                           ('adapterStage','credential-token'), ('extra','credential-token')):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp); fixture.settled[key] = value; fixture.event('settled', fixture.settled)
                row = fixture.result()
                self.assertEqual(row['adapter_state'], 'invalid'); self.assertIsNone(row['supervisor_exit_code'])
                self.assertEqual(row['evidence_state'], 'receipt-matched')
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            fixture.settled.update(adapterStage='progress-log-read', outerDeadlineExceeded=True, cleanupWaitTimedOut=True,
                                   supervisorExitCode=None)
            fixture.event('settled', fixture.settled); row = fixture.result()
            self.assertTrue(row['outer_deadline_exceeded']); self.assertTrue(row['cleanup_wait_timed_out'])
            self.assertEqual(row['adapter_stage'], 'progress-log-read')

    def test_missing_settled_still_reads_exact_late_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            (fixture.directory / 'recovery-settled.json').unlink()
            row = fixture.result()
            self.assertEqual(row['adapter_state'], 'missing'); self.assertIsNone(row['supervisor_exit_code'])
            self.assertEqual(row['evidence_state'], 'receipt-matched')

    def test_marker_tail_is_bounded_exact_and_does_not_export_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            fixture.log.write_text('INSTALLED_RECOVERY_PHASE=api\n' + 'x'*40000 +
                '\nlookalike INSTALLED_RECOVERY_R64=PASS credential-token\nINSTALLED_RECOVERY_PHASE=credential-token\n')
            row = fixture.result()
            self.assertEqual(row['success_marker'], 'not-observed-in-tail'); self.assertIsNone(row['verifier_phase'])
            fixture.log.unlink(); self.assertEqual(fixture.result()['log_state'], 'missing')

    def test_phase_slots_cover_all_three_calls_and_fit_existing_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Snapshot(temp)
            summary = observation.collect(fixture.state, fixture.root, fixture.root)
            self.assertEqual([row['phase'] for row in summary['phases']], list(observation.PHASES))
            self.assertLess(len(json.dumps(summary, indent=2).encode()), 8192)
            self.assertEqual(summary['phases'][0]['evidence_state'], 'entry-missing')
            self.assertEqual(summary['phases'][1]['evidence_state'], 'entry-missing')

    def test_diagnostic_setup_failure_never_blocks_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(observation, 'write_json', side_effect=OSError('credential-token')):
                self.assertEqual(observation.prepare({'nonce':'a'*32}, temp), [])
        failure = RuntimeError('original gate failure')
        with patch.object(ci, 'verify_run_state', return_value={'nonce':'a'*32}), \
             patch.object(ci, 'state_root', return_value=Path('/synthetic')), \
             patch.object(ci, 'read_json', return_value={'status':'passed'}), patch.object(ci, 'validate_source_build'), \
             patch.object(ci, 'powershell', return_value=['powershell']), \
             patch.object(ci, 'prepare_installed_observation', return_value=['-DiagnosticDirectory','fixed-private-path']) as prepare, \
             patch.object(ci, 'run_owned', side_effect=failure) as run, patch.object(ci, 'save_failure_diagnostic'):
            with self.assertRaises(RuntimeError) as caught: ci.installed()
            self.assertIs(caught.exception, failure)
            run.assert_called_once_with('actual-installed-product-acceptance',
                ['powershell','-DiagnosticDirectory','fixed-private-path'], 3600)
            prepare.assert_called_once()

    def test_export_keeps_three_jsons_and_limits_new_section_to_installed_failure(self):
        for status in ('passed', 'failed', 'interrupted', 'not-run'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                fixture = Snapshot(temp)
                state = dict(fixture.state, run={'fixture':True}, started_ns=1)
                observed = observation.collect(fixture.state, fixture.root, fixture.root)
                with patch.object(ci,'state_root',return_value=fixture.root), \
                     patch.object(ci,'verify_run_state',return_value=state), \
                     patch.object(ci,'phase_status',side_effect=lambda _, name: status if name=='installed' else 'failed'), \
                     patch.object(ci,'source_identity',return_value={}), patch.object(ci,'public_identity',return_value={}), \
                     patch.object(ci,'failure_diagnostics',return_value={}), \
                     patch.object(ci,'installed_supervision',return_value=observed) as capture, \
                     patch.object(ci.runpy,'run_path'), patch.object(ci,'installed_inventory',return_value={}), \
                     patch.object(ci,'installed_hashes',return_value={}), patch.object(ci,'read_json',return_value={'hashes':{}}):
                    ci.export()
                files = list((fixture.root/'public').iterdir())
                self.assertEqual({path.name for path in files},set(common.PUBLIC_FILES))
                self.assertTrue(all(path.stat().st_size <= 65536 for path in files))
                summary = json.loads((fixture.root/'public/run-summary.json').read_text())
                fixture.assert_private(summary)
                self.assertEqual('installed_supervision' in summary, status in ('failed','interrupted'))
                self.assertEqual(capture.call_count, int(status in ('failed','interrupted')))
                self.assertFalse(summary['all_required_stages_passed'])
                for name in common.PUBLIC_FILES[1:]:
                    self.assertNotIn('installed_supervision',json.loads((fixture.root/'public'/name).read_text()))

    def test_failure_only_export_and_early_required_runtime_test_wiring(self):
        source = (HERE/'public_ci.py').read_text()
        tree = ast.parse(source)
        export = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'export')
        conditional = next(node.value for node in export.body if isinstance(node, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == 'installed_diagnostics' for t in node.targets))
        self.assertIsInstance(conditional, ast.IfExp)
        self.assertEqual(ast.unparse(conditional.test), "statuses['installed'] in ('failed', 'interrupted')")
        self.assertIn("'test_public_ci_installed_supervision.py'", source)
        with patch.object(ci,'run_owned') as run, patch.object(ci,'powershell',return_value=['powershell']):
            ci.contracts()
        run.assert_any_call('contract-test-public-ci-installed-supervision-py',
            [sys.executable,'-I','-X','utf8',str(HERE/'test_public_ci_installed_supervision.py'),'-v'],180)
        installed = (HERE/'public_ci_verify_installed.ps1').read_text()
        for phase, variable in (('nsis','NsisObserver'),('native-smoke','NativeObserver'),('recovery','RecoveryObserver')):
            self.assertIn('-Phase '+phase, installed); self.assertIn('-DiagnosticObserver $'+variable, installed)
        for retained in ('-TimeoutSeconds 180', '-TimeoutSeconds 540', "'--timeout', '420'",
                         "if ($RecoveryExitCode -ne 0) { throw", '^INSTALLED_RECOVERY_R64=PASS '):
            self.assertIn(retained, installed)


FAKE_SUPERVISOR = r'''
import base64,json,pathlib,sys,time
r=json.loads(base64.b64decode(sys.argv[sys.argv.index('--request-base64')+1]))
mode=r['arguments'][0] if r['arguments'] else 'smoke'
p=pathlib.Path(r['stdoutPath']);p.parent.mkdir(parents=True,exist_ok=True)
if mode != 'late': p.write_text('INSTALLED_RECOVERY_PHASE=shutdown\nINSTALLED_RECOVERY_R64=PASS private-json-payload\n')
if mode=='smoke':
    nonce=r['environment']['IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS']
    p.write_text('IGAC_FROZEN_OPENVINO_OK:'+nonce+':openvino=2025.4.1\n'+
                 'IGAC_FROZEN_OPENVINO_ASCII_OK:'+nonce+':openvino_dll=C:\\diagnostic-fixture\\openvino.dll\n')
    pathlib.Path(r['stderrPath']).write_text('')
v=dict(schemaVersion=1,requestId=r['requestId'],supervisorPid=101,launchTargetPid=102,
       requestedExecutable=r['executable'],launchExecutable=r['executable'],targetExitCode=0,targetExitCodeUnsigned=0,
       outcome='completed',budgetLabel=r['budgetLabel'],executionLimitSeconds=r['timeoutSeconds'],elapsedSeconds=.1,
       confirmedTreeEmpty=True,errors=[],diagnosticLaunchStage='launched')
code=0
if mode=='child-one':v.update(targetExitCode=1,targetExitCodeUnsigned=1,outcome='target-exited-nonzero')
if mode=='mismatch':v['requestId']='d'*32
if mode=='timeout':v['outcome']='execution-timeout';code=125
if mode=='drain':v['outcome']='descendant-drain-timeout';code=125
if mode=='cleanup':v['errors']=['private credential-token cleanup error'];code=125
if mode=='supervisor-one':code=1
if mode=='log-open':v.update(launchTargetPid=None,launchExecutable=None,targetExitCode=None,targetExitCodeUnsigned=None,
    outcome='supervision-error',confirmedTreeEmpty=False,errors=['private-error'],diagnosticLaunchStage='log-open');code=125
if mode=='missing':sys.exit(125)
if mode=='malformed':pathlib.Path(r['receiptPath']).write_text('{broken');sys.exit(125)
if mode=='late':
    # A real sharing violation triggers progress-log-open while the supervisor
    # is alive. Its receipt arrives only after the unchanged EOF cleanup wait.
    import ctypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,
                             ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    api.CreateFileW.restype=ctypes.c_void_p
    api.CloseHandle.argtypes=[ctypes.c_void_p];api.CloseHandle.restype=ctypes.c_int
    handle=api.CreateFileW(str(p),0x40000000,0,None,2,0x80,None)
    assert handle not in (None,0,ctypes.c_void_p(-1).value)
    try:sys.stdin.read()
    finally:api.CloseHandle(handle)
    time.sleep(.1)
pathlib.Path(r['receiptPath']).write_text(json.dumps(v))
sys.exit(code)
'''

POWERSHELL_PROBE = r'''
param([string]$Python, [string]$CasesFile, [string]$NativeWrapper, [string]$FrozenSmoke, [string]$Observer)
$ErrorActionPreference = 'Stop'
. $NativeWrapper
. $Observer
$Cases = [IO.File]::ReadAllText($CasesFile) | ConvertFrom-Json
$Results = @()
foreach ($Case in $Cases) {
    $Observe = New-PublicInstalledObserver -Directory $Case.directory -RunNonce $Case.nonce -InvocationId $Case.invocation -Phase $Case.phase
    if ($null -eq $Observe) { throw 'Observer could not be initialized' }
    $Failed = $false
    $TargetExit = $null
    try {
        if ($Case.mode -eq 'smoke') {
            & $FrozenSmoke -Executable $Python -LogPath $Case.log -TimeoutSeconds 180 -DiagnosticObserver $Observe
            $TargetExit = 0
        } else {
            $TargetExit = Invoke-IgacNativeCommandWithLog -FilePath $Python -ArgumentList @($Case.mode) `
                -LogPath $Case.log -TimeoutSeconds 540 -DiagnosticObserver $Observe
            if ($global:LASTEXITCODE -ne $TargetExit -or $TargetExit -isnot [int]) { throw 'Native return was contaminated' }
        }
    } catch { $Failed = $true }
    $Results += @{ mode=$Case.mode; failed=[bool]$Failed; targetExit=$TargetExit }
}
[IO.File]::WriteAllText(($CasesFile + '.results'), ($Results | ConvertTo-Json -Depth 4))
'''


@unittest.skipUnless(os.name == 'nt', 'Real PowerShell runtime contracts execute on the required Windows runner')
class WindowsPowerShellRuntime(unittest.TestCase):
    def test_real_powershell_hosts_observe_exact_failures_and_late_receipts(self):
        hosts = [str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'), shutil.which('pwsh.exe')]
        self.assertTrue(all(hosts), 'Both existing PowerShell hosts are required')
        modes = ('child-one','mismatch','timeout','drain','cleanup','supervisor-one','log-open','missing','malformed','late','smoke')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            adapter = base/'owned_process.ps1'; shutil.copyfile(ROOT/'scripts/owned_process.ps1',adapter)
            (base/'owned_process.py').write_text(FAKE_SUPERVISOR)
            wrapper = base/'invoke_native_logged.ps1'; shutil.copyfile(ROOT/'scripts/invoke_native_logged.ps1',wrapper)
            smoke = base/'test_frozen_openvino.ps1'; shutil.copyfile(ROOT/'scripts/test_frozen_openvino.ps1',smoke)
            probe = base/'probe.ps1'; probe.write_text(POWERSHELL_PROBE)
            for index, host in enumerate(hosts):
                cases = []
                for mode in modes:
                    root = base/str(index)/mode; root.mkdir(parents=True)
                    state = {'nonce':'a'*32}
                    (root/'installed-start.json').write_text(json.dumps(
                        {'nonce':state['nonce'],'started_ns':1,'supervision_invocation':uuid.uuid4().hex}))
                    args = observation.prepare(state, root)
                    (root/'installer-output').mkdir()
                    phase = 'native-smoke' if mode=='smoke' else 'recovery'
                    log = 'installed-openvino-smoke.log' if mode=='smoke' else observation.LOGS['recovery']
                    cases.append(dict(mode=mode, phase=phase, root=str(root), log=str(root/'installer-output'/log),
                                      directory=args[1],nonce=args[3],invocation=args[5]))
                cases_file=base/('cases-'+str(index)+'.json');cases_file.write_text(json.dumps(cases))
                completed=subprocess.run([host,'-NoLogo','-NoProfile','-NonInteractive','-File',str(probe),
                    '-Python',sys.executable,'-CasesFile',str(cases_file),'-NativeWrapper',str(wrapper),
                    '-FrozenSmoke',str(smoke),'-Observer',str(HERE/'public_ci_installed_observer.ps1')],capture_output=True,timeout=80)
                self.assertEqual(completed.returncode,0, 'Runtime observer probe failed; raw output remains local')
                results=json.loads(Path(str(cases_file)+'.results').read_text(encoding='utf-8-sig'))
                self.assertEqual(len(results),len(modes))
                for case, result in zip(cases,results):
                    mode=case['mode']; self.assertEqual(result['mode'],mode)
                    self.assertEqual(result['failed'],mode not in ('child-one','smoke'))
                    rows=observation.collect({'nonce':'a'*32},case['root'],case['root'])['phases']
                    row=rows[1 if mode=='smoke' else 2]
                    self.assertTrue(row['request_bound']); self.assertEqual(row['adapter_state'],'read')
                    self.assertTrue(row['supervisor_started'])
                    if mode=='smoke':
                        self.assertEqual(result['targetExit'],0);self.assertTrue(row['terminal_receipt'])
                        self.assertEqual(row['phase'],'native-smoke')
                    elif mode=='child-one':
                        self.assertEqual(result['targetExit'],1);self.assertTrue(row['terminal_receipt'])
                    elif mode=='mismatch':self.assertEqual(row['evidence_state'],'receipt-mismatch');self.assertIsNone(row['outcome'])
                    elif mode=='missing':self.assertEqual(row['evidence_state'],'receipt-missing')
                    elif mode=='malformed':self.assertEqual(row['evidence_state'],'receipt-malformed')
                    elif mode=='late':
                        self.assertEqual(row['evidence_state'],'receipt-matched');self.assertEqual(row['adapter_stage'],'progress-log-open')
                        self.assertEqual(row['supervisor_exit_code'],0)
                    else:
                        self.assertEqual(row['evidence_state'],'receipt-matched')
                        self.assertFalse(row['terminal_receipt'] and row['supervisor_exit_code']==0)

    def test_real_owner_and_throwing_observer_preserve_child_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); probe=root/'native.ps1'
            probe.write_text(r'''
param([string]$Python,[string]$Adapter,[string]$Root)
$ErrorActionPreference='Stop'
. $Adapter
$Receipt=Invoke-IgacOwnedProcess -SupervisorPython $Python -DiagnosticObserver { throw 'diagnostics cannot replace target' } -Request @{
    executable=$Python; arguments=@('-I','-X','utf8','-c','import sys; print("actual-owned-child"); sys.exit(1)')
    workingDirectory=$Root; stdoutPath=(Join-Path $Root 'native.stdout'); stderrPath=(Join-Path $Root 'native.stderr')
    timeoutSeconds=10; budgetLabel='Real diagnostic isolation fixture'; drainSeconds=1; terminationSeconds=2
}
if ($Receipt.targetExitCode -ne 1 -or -not (Test-IgacOwnedTerminalReceipt $Receipt)) { throw 'Original result changed' }
''')
            for host in (str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),shutil.which('pwsh.exe')):
                self.assertTrue(host)
                completed=subprocess.run([host,'-NoLogo','-NoProfile','-NonInteractive','-File',str(probe),
                    '-Python',sys.executable,'-Adapter',str(ROOT/'scripts/owned_process.ps1'),'-Root',str(root)],
                    capture_output=True,timeout=30)
                self.assertEqual(completed.returncode,0,'Real owner isolation probe failed; raw output remains local')


if __name__ == '__main__':
    unittest.main()
