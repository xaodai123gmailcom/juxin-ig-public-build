"""Installed CLI verifier gates; source runtime is offline synthetic evidence."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('installed_auto_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class InstalledCollectionCompletionTests(unittest.TestCase):
    def command(self, body):
        return [sys.executable, '-c', body]

    def test_real_production_runtime_in_fresh_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / 'user.sqlite3'
            sentinel.write_bytes(b'not a database: must never open this')
            with patch.dict(os.environ, {'IGAC_DB_PATH': str(sentinel), 'IGAC_DATA_DIR': str(directory),
                                        'IGAC_STARTUP_TOKEN': 'do-not-use', 'IGAC_PARENT_PID': 'poison-pid', 'IGAC_PORT': 'poison-port'}):
                proof = probe.probe_collection_completion(self.command(
                    f'import sys; sys.path.insert(0, {str(ROOT / "backend")!r}); '
                    'from app.__main__ import main; main()'), directory / 'proof.log')
            self.assertEqual(b'not a database: must never open this', sentinel.read_bytes())
            self.assertTrue(proof['network_disabled'])
            self.assertFalse(proof['user_data_touched'])
            self.assertEqual(2, proof['cases']['normal_with_truthful_gap']['source_calls'])
            self.assertEqual(2, proof['cases']['normal_with_truthful_gap']['remaining_gap'])
            self.assertTrue(proof['single_gap_recheck']['verified'])
            self.assertEqual(3, proof['cases']['pause_restart_extra_pass']['source_invocations'])
            self.assertEqual(2, proof['cases']['pause_restart_extra_pass']['from_top_passes'])
            self.assertEqual(2, proof['cases']['pause_restart_extra_pass']['completed_passes'])
            self.assertTrue(proof['manual_parent_recheck']['verified'])
            self.assertTrue(proof['final_seed_completion']['verified'])
            for name in ('final_seed_single_source', 'final_seed_two_sources', 'final_seed_multiple_sources',
                         'final_seed_factory_worker_not_connected', 'final_seed_factory_browser_context_missing'):
                for field, value in proof['cases'][name].items():
                    missing = copy.deepcopy(proof)
                    del missing['cases'][name][field]
                    with self.subTest(missing_final_seed=(name, field)), self.assertRaises(RuntimeError):
                        probe.validate_collection_completion_proof(missing)
                    forged = copy.deepcopy(proof)
                    forged['cases'][name][field] = False if type(value) is bool else True
                    with self.subTest(forged_final_seed=(name, field)), self.assertRaises(RuntimeError):
                        probe.validate_collection_completion_proof(forged)
            for reason in ('worker_not_connected', 'browser_context_missing'):
                case = proof['cases']['final_seed_factory_' + reason]
                self.assertEqual(reason, case['injected_reason'])
                self.assertEqual(1, case['factory_failures'])
                self.assertEqual(1, case['reconnects'])
                self.assertEqual(2, case['saved_results'])
                self.assertTrue(case['parent_health_probe_true'])
                self.assertTrue(case['no_remaining_seed_lease_or_waiter'])
            for field, value in proof['final_seed_completion'].items():
                missing = copy.deepcopy(proof)
                del missing['final_seed_completion'][field]
                with self.subTest(missing_final_summary=field), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(missing)
                forged = copy.deepcopy(proof)
                forged['final_seed_completion'][field] = not value
                with self.subTest(forged_final_summary=field), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(forged)

            for name in ('manual_pending_before_producer_return', 'manual_pending_parent_join',
                         'manual_pending_stop_reopen_retry'):
                with self.subTest(pending_runtime=name):
                    case = proof['cases'][name]
                    self.assertEqual(3, case['source_calls'])
                    self.assertEqual(1, case['manual_passes'])
                    self.assertEqual(1, case['child_commits_while_pending'])
                    self.assertEqual(3, case['saved_results'])
                    self.assertTrue(case['cursor_unchanged_until_safe_point'])
                    self.assertTrue(case['post_pass_reels_resumed'])
                    self.assertTrue(case['duplicates_never_requeued'])
                    # Every new proof field is mandatory, including numeric
                    # counts: booleans must never pass as Python integer 1.
                    for field, value in case.items():
                        missing = copy.deepcopy(proof)
                        del missing['cases'][name][field]
                        with self.subTest(missing_pending=(name, field)), self.assertRaises(RuntimeError):
                            probe.validate_collection_completion_proof(missing)
                        forged = copy.deepcopy(proof)
                        forged['cases'][name][field] = False if type(value) is bool else True
                        with self.subTest(forged_pending=(name, field)), self.assertRaises(RuntimeError):
                            probe.validate_collection_completion_proof(forged)
            self.assertEqual(2, proof['cases']['manual_pending_parent_join']['reels_runs'])
            restart = proof['cases']['manual_pending_stop_reopen_retry']
            self.assertTrue(restart['stop_preserves_prepared_request'])
            self.assertTrue(restart['database_reopen_persistence'])
            self.assertTrue(restart['explicit_retry_same_generation'])
            self.assertTrue(restart['new_lease_after_previous_released'])
            # A stale R5 executable or merely a positive summary is insufficient.
            # Every required R6 runtime case and each evidence flag is gated.
            for name in proof['cases']:
                missing = copy.deepcopy(proof)
                del missing['cases'][name]
                with self.subTest(missing_case=name), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(missing)
            for field in proof['single_gap_recheck']:
                missing = copy.deepcopy(proof)
                del missing['single_gap_recheck'][field]
                with self.subTest(missing_summary=field), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(missing)
            for field in proof['manual_parent_recheck']:
                missing = copy.deepcopy(proof)
                del missing['manual_parent_recheck'][field]
                with self.subTest(missing_manual_summary=field), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(missing)
            stale = copy.deepcopy(proof)
            del stale['manual_parent_recheck']
            for name in tuple(stale['cases']):
                if name.startswith('manual_pending_'):
                    del stale['cases'][name]
            with self.assertRaises(RuntimeError):
                probe.validate_collection_completion_proof(stale)
            for field in ('cases', 'single_gap_recheck', 'manual_parent_recheck', 'final_seed_completion'):
                malformed = copy.deepcopy(proof)
                malformed[field] = []
                with self.subTest(malformed=field), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(malformed)
            for name in proof['cases']:
                malformed = copy.deepcopy(proof)
                malformed['cases'][name] = None
                with self.subTest(malformed_case=name), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(malformed)
            for name, field, incorrect in (
                    ('normal_with_truthful_gap', 'source_calls', 1),
                    ('normal_with_truthful_gap', 'remaining_gap', 0),
                    ('normal_without_gap', 'source_calls', 2),
                    ('pause_restart_extra_pass', 'from_top_passes', 3),
                    ('pause_restart_extra_pass', 'extra_pass_resumed_from_saved_tail', False),
                    ('parent_reels_after_final_pass', 'parent_reels_after_final_extraction', False),
                    ('parent_reels_after_final_pass', 'children_finish_and_cleanup', False),
                    ('extra_pass_failure_retained', 'failure_remains_incomplete', False),
                    ('historical_completed_gap', 'source_calls', 1)):
                forged = copy.deepcopy(proof)
                forged['cases'][name][field] = incorrect
                with self.subTest(forged=(name, field, incorrect)), self.assertRaises(RuntimeError):
                    probe.validate_collection_completion_proof(forged)

    def test_cli_report_exposes_existing_and_manual_parent_recheck_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            report = directory / 'report.json'
            evidence = {'verified': True, 'single_gap_recheck': {'verified': True, 'gap_passes': 2},
                        'manual_parent_recheck': {'verified': True, 'durable_pending_admission': True},
                        'final_seed_completion': {'verified': True, 'production_playwright_pool': True}}
            with patch.object(probe, 'probe_core', return_value={'verified': True}), \
                 patch.object(probe, 'probe_collection_completion', return_value=evidence), \
                 patch.object(sys, 'argv', ['verify', '--executable', sys.executable,
                    '--log', str(directory / 'core.log'), '--collection-completion', '--report', str(report)]):
                self.assertEqual(0, probe.main())
            result = json.loads(report.read_text())
            self.assertEqual(evidence['final_seed_completion'], result['final_seed_completion'])
            self.assertTrue(result['single_gap_recheck']['verified'])
            self.assertEqual(result['collection_completion']['single_gap_recheck'], result['single_gap_recheck'])
            self.assertTrue(result['manual_parent_recheck']['verified'])
            self.assertEqual(result['collection_completion']['manual_parent_recheck'], result['manual_parent_recheck'])

    def test_missing_or_forged_proof_fails_closed(self):
        for body in ('print("no proof")', 'print("COLLECTION_COMPLETION_SELFTEST=PASS {}")',
                     'raise SystemExit(5)',
                     'print("COLLECTION_COMPLETION_SELFTEST=PASS {}\\nCOLLECTION_COMPLETION_SELFTEST=PASS {}")'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    probe.probe_collection_completion(self.command(body), Path(temporary) / 'proof.log')

    def test_timeout_is_release_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                probe.probe_collection_completion(self.command('import time; time.sleep(30)'),
                    Path(temporary) / 'proof.log', timeout=.05)

    def test_invalid_deadline_rejected(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                probe.probe_collection_completion([], Path('unused.log'), timeout=timeout)


if __name__ == '__main__':
    unittest.main()
