"""Strict installed upgrade evidence, independent persistence checks, and fail-first faults."""
from __future__ import annotations

import copy
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('installed_upgrade_probe', ROOT / 'scripts/verify_frozen_core_service.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
fixture = probe._nurture_upgrade_fixture()
PREFIX = 'NURTURE_CLEANUP_UPGRADE_SELFTEST=PASS '


def command(extra=''):
    body = f'import sys; sys.path.insert(0, {str(ROOT / "backend")!r}); '
    if os.environ.get('IGAC_LOCAL_TEST_ISOLATION') == '1':
        guard = ROOT.parent / 'local-test-isolation'
        if not (guard / 'guard.py').is_file():
            raise RuntimeError('Local isolation guard is missing')
        body = f'import os,sys; os.environ["IGAC_LOCAL_TEST_ISOLATION"]="1"; sys.path.insert(0, {str(guard)!r}); import guard; guard.install(); ' + body
    return [sys.executable, '-c', body + extra + '; from app.__main__ import main; main()' if extra else body + 'from app.__main__ import main; main()']


class InstalledNurtureCleanupUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='nurture-upgrade-tests-')
        cls.root = Path(cls.directory.name)
        cls.manifest_path, cls.manifest = fixture.seed(cls.root)
        cls.manifest_hash = fixture.file_sha256(cls.manifest_path)
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(('IGAC_', 'COLLECTOR_CORE_', 'PYTHON', 'PLAYWRIGHT_'))
               and key.upper() != '__PYVENV_LAUNCHER__'}
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        cmd = probe.direct_child_command(command(), env)
        cls.launch_ns = time.time_ns()
        cls.launch_perf_ns = time.perf_counter_ns()
        process = subprocess.Popen([*cmd, '--verify-nurture-cleanup-upgrade', str(cls.manifest_path)],
            cwd=cls.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        try:
            output, _ = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate(timeout=10)
            raise
        cls.pid = process.pid
        if process.returncode:
            raise RuntimeError('Real upgrade fixture failed:\n' + output[-32768:])
        lines = [line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
        if len(lines) != 1:
            raise RuntimeError('Real upgrade fixture omitted its unique proof')
        cls.proof = json.loads(lines[0])
        cls.arguments = dict(manifest=cls.manifest, manifest_sha256=cls.manifest_hash,
            expected_executable=Path(sys.executable).resolve(), process_pid=cls.pid,
            launch_ns=cls.launch_ns, launch_perf_ns=cls.launch_perf_ns, require_installed=False)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_source_process_uses_preexisting_old_database_and_real_lifespan(self):
        result = probe.validate_nurture_cleanup_upgrade_proof(self.proof, **self.arguments)
        state = fixture.inspect_after(self.root, self.manifest, result)
        self.assertEqual({'verified': True, 'historical_jobs': 19, 'protected_tables': 15,
            'login_files': 68, 'recovered_holds': 3, 'retained_holds': 15, 'new_jobs': 3,
            'global_dedupe_identities': 3, 'untouched_leases': 3, 'expected_startup_action_pauses': 1}, state)
        self.assertEqual(18, len(result['cases']))
        self.assertNotEqual(result['seed_pid'], result['runtime']['pid'])
        self.assertEqual(0, result['network_attempts'])
        self.assertEqual(0, result['activity_attempts'])
        self.assertEqual(self.manifest_hash, fixture.file_sha256(self.manifest_path))
        print('SOURCE_NURTURE_CLEANUP_UPGRADE_PROOF=' + json.dumps({**result, 'persisted_state': state}, sort_keys=True), flush=True)

    def test_every_root_case_and_admission_field_is_required(self):
        paths = [(key,) for key in self.proof]
        for section in ('runtime', 'cases', 'admission', 'startup_action_transition', 'queued_blockers_before_startup'):
            paths.extend((section, key) for key in self.proof[section])
        for section in ('cases', 'admission'):
            for name, value in self.proof[section].items():
                paths.extend((section, name, field) for field in value)
        for path in paths:
            damaged = copy.deepcopy(self.proof)
            current = damaged
            for part in path[:-1]:
                current = current[part]
            del current[path[-1]]
            with self.subTest(missing=path), self.assertRaises(RuntimeError):
                probe.validate_nurture_cleanup_upgrade_proof(damaged, **self.arguments)

    def test_false_forged_stale_and_wrong_generation_receipts_are_rejected(self):
        changes = [
            (('nonce',), '0' * 64), (('manifest_sha256',), '0' * 64),
            (('input_database_sha256',), '0' * 64), (('seed_pid',), self.pid),
            (('opened_ns',), True),
            (('opened_perf_ns',), self.manifest['seed_completed_perf_ns'] - 1),
            (('chronology_clock',), 'time_ns'),
            (('production_closed_profile_guard',), False), (('normal_startup_and_shutdown',), False),
            (('runtime', 'pid'), self.pid + 1), (('runtime', 'executable_sha256'), '0' * 64),
            (('runtime', 'executable'), str(ROOT / 'wrong.exe')),
            (('cases', 'closed_legacy', 'hold_after'), True),
            (('cases', 'open', 'hold_after'), False),
            (('cases', 'opening', 'startup_guard_calls'), 1),
            (('cases', 'closing', 'startup_guard_calls'), True),
            (('cases', 'unknown_state', 'explicit_retry_error'), ''),
            (('cases', 'successor_lease', 'historical_lease_token'), 'replacement'),
            (('cases', 'fresh_account_lease', 'hold_after'), False),
            (('cases', 'unknown_profile', 'explicit_retry_error'), 'conflict'),
            (('cases', 'foreign_profile_owner', 'startup_guard_calls'), 1),
            (('cases', 'closed_lost', 'history_sha256'), '0' * 64),
            (('admission', 'closed_legacy', 'blocked_open_before'), False),
            (('startup_action_transition', 'version_after'), True),
            (('startup_action_transition', 'after'), 'completed'),
            (('startup_sequence',), ['studio.recover', 'studio.start_scheduler']),
            (('network_attempts',), True), (('activity_attempts',), 1),
        ]
        for path, value in changes:
            damaged = copy.deepcopy(self.proof)
            current = damaged
            for part in path[:-1]:
                current = current[part]
            current[path[-1]] = value
            with self.subTest(forged=path), self.assertRaises(RuntimeError):
                probe.validate_nurture_cleanup_upgrade_proof(damaged, **self.arguments)

    def test_installed_mode_requires_windows_frozen_and_bundle_origin(self):
        args = dict(self.arguments, require_installed=True)
        with self.assertRaisesRegex(RuntimeError, 'frozen Windows'):
            probe.validate_nurture_cleanup_upgrade_proof(self.proof, **args)
        # A synthetic installed-shaped receipt is used only to exercise rejection
        # branches. The actual release caller has no source-mode CLI escape flag.
        installed = copy.deepcopy(self.proof)
        installed['runtime'].update(frozen=True, windows=True)
        probe.validate_nurture_cleanup_upgrade_proof(installed, **args)
        for key, value in (('frozen', False), ('windows', False),
                           ('bundle_root', str(ROOT / 'unrelated-bundle')),
                           ('module_file', str(ROOT / 'checkout/app/other.py'))):
            damaged = copy.deepcopy(installed)
            damaged['runtime'][key] = value
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                probe.validate_nurture_cleanup_upgrade_proof(damaged, **args)

    def test_independent_persisted_oracle_rejects_data_loss_even_with_success_receipt(self):
        mutations = {
            'history_counts': "UPDATE studio_jobs SET result_json=json_set(result_json,'$.counts.like',999) WHERE id='historical-closed_legacy'",
            'cleanup_timestamp': "UPDATE studio_jobs SET result_json=json_set(result_json,'$.window_cleanup.last_attempt_at','2099-01-01') WHERE id='historical-closed_lost'",
            'history_timestamp': "UPDATE studio_jobs SET updated_at='2099-01-01' WHERE id='historical-closed_legacy'",
            'unsafe_clear': "UPDATE studio_jobs SET result_json=json_set(result_json,'$.window_hold',json('false')) WHERE id='historical-open'",
            'dedupe': "UPDATE global_seen SET sources_json='[]'",
            'login': "UPDATE auth_sessions SET remember_login=0",
            'replacement_lease': "UPDATE browser_operation_leases SET lease_token='wrong' WHERE lease_token='successor-generation'",
            'fresh_lease_loss': "DELETE FROM browser_operation_leases WHERE lease_token='fresh-account-successor-generation'",
            'action_mutation': "UPDATE action_campaigns SET limit_count=999",
            'posting_content': "UPDATE posting_jobs SET caption='changed by upgrade'",
            'posting_generation': "UPDATE posting_jobs SET queue_revision=99",
            'posting_withdrawal': "INSERT INTO posting_withdraw_history VALUES('forged','forged','forged','forged','forged','forged','now')",
            'extra_job': "DELETE FROM studio_jobs WHERE request_key LIKE 'upgrade-admission-after-closed_legacy%'",
        }
        for name, sql in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / 'copy'
                shutil.copytree(self.root, target)
                with closing(sqlite3.connect(target / self.manifest['database_name'])) as c, c:
                    c.execute(sql)
                # Linux can unlink an open SQLite file; require actual closure
                # so every corruption case also catches Windows handle leaks.
                with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                    c.execute('SELECT 1')
                with self.assertRaises(RuntimeError):
                    fixture.inspect_after(target, self.manifest, self.proof)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'copy'
            shutil.copytree(self.root, target)
            first = next(iter(self.manifest['login_files']))
            (target / first).write_bytes(b'damaged login data')
            with self.assertRaisesRegex(RuntimeError, 'login/profile'):
                fixture.inspect_after(target, self.manifest, self.proof)

    def test_removed_orphan_reconciliation_fails_the_real_process(self):
        # Reproduce the old behavior: closed historical rows keep their hold.
        extra = ('import app.nurture_cleanup_recovery as recovery; original=recovery.reconcile_closed_nurture; '
            'recovery.reconcile_closed_nurture=lambda manager, owner, ident: None '
            'if ident in {"historical-closed_legacy","historical-closed_lost","historical-archived_closed"} '
            'else original(manager, owner, ident)')
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'failure.log'
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                probe.probe_nurture_cleanup_upgrade(command(extra), log, require_installed=False)
            self.assertIn('incorrect startup hold outcome: closed_legacy', log.read_text(encoding='utf-8'))
            self.assertNotIn(PREFIX, log.read_text(encoding='utf-8'))

    def test_bypassing_native_owner_and_state_guard_fails_the_real_process(self):
        extra = ('from contextlib import contextmanager; from app.embedded_browser import EmbeddedBrowser; '
            'EmbeddedBrowser.closed_profile_guard=contextmanager(lambda self, profile, owner: '
            'iter([{"closed":True,"profile_id":profile,"owner_user_id":owner,"verification":"desktop-absence-v1"}]))')
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'failure.log'
            with self.assertRaises(RuntimeError):
                probe.probe_nurture_cleanup_upgrade(command(extra), log, require_installed=False)
            self.assertNotIn(PREFIX, log.read_text(encoding='utf-8'))

    def test_probe_isolates_poisoned_user_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user = root / 'user.sqlite3'
            user.write_bytes(b'do not touch this user database')
            with patch.dict(os.environ, {'IGAC_DB_PATH': str(user), 'IGAC_DATA_DIR': str(root),
                    'IGAC_PARENT_PID': 'poison', 'IGAC_PORT': 'poison', 'IGAC_STARTUP_TOKEN': 'poison',
                    'IGAC_EMBEDDED_BROWSER_URL': 'https://should-not-connect.invalid'}):
                result = probe.probe_nurture_cleanup_upgrade(command(), root / 'proof.log', require_installed=False)
            self.assertEqual(b'do not touch this user database', user.read_bytes())
            self.assertTrue(result['persisted_state']['verified'])

    def test_equal_or_backward_wall_clock_keeps_strict_process_chronology(self):
        # The real child must still pass when UTC repeats or moves backwards
        # between seed completion and Popen; no sleep or tolerance is added.
        fixed_wall_ns = 1_700_000_000_000_000_000
        for delta in (0, -10_000_000):
            with self.subTest(wall_delta_ns=delta), tempfile.TemporaryDirectory() as temporary, \
                 patch.object(time, 'time_ns', side_effect=[fixed_wall_ns, fixed_wall_ns + delta]), \
                 patch.object(probe, 'validate_nurture_cleanup_upgrade_proof',
                              wraps=probe.validate_nurture_cleanup_upgrade_proof) as validate:
                result = probe.probe_nurture_cleanup_upgrade(command(), Path(temporary) / 'proof.log',
                                                            require_installed=False)
            self.assertEqual(fixed_wall_ns, result['seed_completed_ns'])
            self.assertEqual(fixed_wall_ns + delta, validate.call_args.kwargs['launch_ns'])
            self.assertEqual('perf_counter_ns-system-v1', result['chronology_clock'])
            self.assertLess(result['seed_completed_perf_ns'], validate.call_args.kwargs['launch_perf_ns'])
            self.assertLess(validate.call_args.kwargs['launch_perf_ns'], result['opened_perf_ns'])
            self.assertTrue(result['persisted_state']['verified'])

    def test_chronology_stays_strict_and_reports_exact_timestamps_and_pids(self):
        # Equality/reversal in the authoritative performance counter remains a
        # hard failure, including equality at the child-open boundary.
        cases = ((self.manifest['seed_completed_perf_ns'], self.proof['opened_perf_ns']),
                 (self.manifest['seed_completed_perf_ns'] - 1, self.proof['opened_perf_ns']),
                 (self.launch_perf_ns, self.launch_perf_ns),
                 (self.launch_perf_ns, self.launch_perf_ns - 1))
        for launch, opened in cases:
            changed = copy.deepcopy(self.proof)
            changed['opened_perf_ns'] = opened
            args = dict(self.arguments, launch_perf_ns=launch)
            with self.subTest(launch=launch, opened=opened), self.assertRaises(RuntimeError) as caught:
                probe.validate_nurture_cleanup_upgrade_proof(changed, **args)
            detail = json.loads(str(caught.exception).split(': ', 1)[1])
            self.assertEqual(self.manifest['seed_completed_perf_ns'], detail['seed_completed_perf_ns'])
            self.assertEqual(launch, detail['launch_perf_ns'])
            self.assertEqual(opened, detail['opened_perf_ns'])
            self.assertEqual(self.manifest['seed_pid'], detail['seed_pid'])
            self.assertEqual(self.pid, detail['process_pid'])
            self.assertEqual(self.proof['runtime']['pid'], detail['runtime']['pid'])
            self.assertTrue(detail['performance_clock']['monotonic'])

    def test_missing_duplicate_malformed_nonzero_and_timeout_fail_closed(self):
        bodies = ('print("no proof")', 'print("' + PREFIX + '{}")', 'raise SystemExit(7)',
                  'print("' + PREFIX + '{}\\n' + PREFIX + '{}")')
        for body in bodies:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    probe.probe_nurture_cleanup_upgrade([sys.executable, '-c', body], Path(temporary) / 'proof.log',
                                                        require_installed=False)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                probe.probe_nurture_cleanup_upgrade([sys.executable, '-c', 'import time; time.sleep(30)'],
                    Path(temporary) / 'proof.log', timeout=.05, require_installed=False)
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                probe.probe_nurture_cleanup_upgrade([], Path('unused.log'), timeout=timeout)

    def test_windows_venv_redirector_keeps_reported_executable_and_owns_base_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / 'venv-python.exe'
            launcher.write_bytes(b'synthetic redirector identity only')
            base = str(Path(sys.executable).resolve())
            process = Mock(pid=43210)
            process.wait.return_value = 0
            launched = {}
            def spawn(args, **kwargs):
                launched.update(args=args, environment=kwargs['env'])
                kwargs['stdout'].write((PREFIX + json.dumps(self.proof) + '\n').encode())
                kwargs['stdout'].flush()
                return process
            with patch.object(probe, 'IS_WINDOWS', True), \
                 patch.object(sys, 'executable', str(launcher)), \
                 patch.object(sys, '_base_executable', base), \
                 patch.object(probe.subprocess, 'Popen', side_effect=spawn), \
                 patch.object(probe, '_nurture_upgrade_fixture', return_value=fixture), \
                 patch.object(probe, 'validate_nurture_cleanup_upgrade_proof', return_value={}) as validate, \
                 patch.object(fixture, 'inspect_after', return_value={'verified': True}):
                probe.probe_nurture_cleanup_upgrade([str(launcher), '-c', 'unused'], root / 'proof.log',
                                                    require_installed=False)
            self.assertEqual(base, launched['args'][0])
            self.assertEqual(str(launcher), launched['environment']['__PYVENV_LAUNCHER__'])
            self.assertEqual(launcher.resolve(), validate.call_args.kwargs['expected_executable'])
            self.assertEqual(process.pid, validate.call_args.kwargs['process_pid'])

    def test_cli_adds_strict_upgrade_gate_without_replacing_prior_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / 'report.json'
            collection = {'single_gap_recheck': {}, 'manual_parent_recheck': {}, 'final_seed_completion': {}}
            with patch.object(probe, 'probe_core', return_value={'verified': True}), \
                 patch.object(probe, 'probe_collection_completion', return_value=collection) as c, \
                 patch.object(probe, 'probe_standalone_nurture', return_value={'existing_cases': 7}) as n, \
                 patch.object(probe, 'probe_posting_workflow', return_value={'existing_cases': 5}) as p, \
                 patch.object(probe, 'probe_nurture_cleanup_upgrade', return_value={'new_cases': 18}) as u, \
                 patch.object(sys, 'argv', ['verify', '--executable', sys.executable, '--log', str(root / 'core.log'),
                    '--collection-completion', '--standalone-nurture', '--posting-workflow',
                    '--nurture-cleanup-upgrade', '--report', str(report)]):
                self.assertEqual(0, probe.main())
            result = json.loads(report.read_text())
            self.assertEqual({'new_cases': 18}, result['nurture_cleanup_upgrade'])
            self.assertEqual(7, result['standalone_nurture']['existing_cases'])
            self.assertEqual(5, result['posting_workflow']['existing_cases'])
            for call in (c, n, p, u):
                call.assert_called_once()
            self.assertNotIn('require_installed', u.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
