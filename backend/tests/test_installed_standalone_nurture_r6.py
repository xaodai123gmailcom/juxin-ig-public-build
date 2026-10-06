"""Installed executable proof must come from a separate, complete offline flow."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('installed_nurture_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class InstalledStandaloneNurtureTests(unittest.TestCase):
    def command(self, body):
        # The local protected runner's Python guard is deliberately restored
        # before any app import; release Windows tests have no local skip/guard.
        if os.environ.get('IGAC_LOCAL_TEST_ISOLATION') == '1':
            guard = ROOT.parent / 'local-test-isolation'
            self.assertTrue((guard / 'guard.py').is_file())
            body = f'import os,sys; os.environ["IGAC_LOCAL_TEST_ISOLATION"]="1"; sys.path.insert(0, {str(guard)!r}); import guard; guard.install(); ' + body
        return [sys.executable, '-c', body]

    def test_real_source_cli_runs_complete_production_flow_and_rejects_partial_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / 'user.sqlite3'
            sentinel.write_bytes(b'offline nurture must never open a user database')
            with patch.dict(os.environ, {'IGAC_DB_PATH': str(sentinel), 'IGAC_DATA_DIR': str(directory),
                    'IGAC_STARTUP_TOKEN': 'do-not-use', 'IGAC_PARENT_PID': 'poison-pid', 'IGAC_PORT': 'poison-port'}):
                try:
                    proof = probe.probe_standalone_nurture(self.command(
                        f'import sys; sys.path.insert(0, {str(ROOT / "backend")!r}); '
                        'from app.__main__ import main; main()'), directory / 'proof.log')
                except Exception:
                    # Preserve bounded stage evidence before TemporaryDirectory
                    # removes the child log, including a watchdog termination.
                    log=(directory/'proof.log')
                    tail=log.read_text(encoding='utf-8',errors='replace')[-32768:] if log.exists() else 'proof.log was not created'
                    print('SOURCE_STANDALONE_NURTURE_LOG_TAIL='+tail,flush=True)
                    raise
            for line in (directory/'proof.log').read_text(encoding='utf-8').splitlines():
                if line.startswith('STANDALONE_NURTURE_STAGE='):print(line,flush=True)
            self.assertEqual(b'offline nurture must never open a user database', sentinel.read_bytes())
        print('SOURCE_STANDALONE_NURTURE_PROOF=' + json.dumps(proof, sort_keys=True))
        self.assertTrue(proof['verified'])
        self.assertTrue(proof['production_nurture_engine'])
        self.assertEqual(7, len(proof['cases']))
        for name in proof['cases']:
            missing = copy.deepcopy(proof)
            del missing['cases'][name]
            with self.subTest(missing_case=name), self.assertRaises(RuntimeError):
                probe.validate_standalone_nurture_proof(missing)
            for field in proof['cases'][name]:
                missing = copy.deepcopy(proof)
                del missing['cases'][name][field]
                with self.subTest(missing_field=(name, field)), self.assertRaises(RuntimeError):
                    probe.validate_standalone_nurture_proof(missing)
        for field in proof:
            missing = copy.deepcopy(proof)
            del missing[field]
            with self.subTest(missing_root=field), self.assertRaises(RuntimeError):
                probe.validate_standalone_nurture_proof(missing)
        for name, field, value in (
            ('selection_and_fixed_policy', 'default_minutes', 3),
            ('selection_and_fixed_policy', 'blocked_batch_atomic', False),
            ('completed_history', 'posts', False),
            ('completed_history', 'actual_seconds', float('nan')),
            ('completed_history', 'actual_seconds', 300),
            ('completed_history', 'synthetic_like_clicks', 2),
            ('interrupted_pending_effect', 'restart_does_not_open_browser_or_replay', False)):
            forged = copy.deepcopy(proof)
            forged['cases'][name][field] = value
            with self.subTest(forged=(name, field, value)), self.assertRaises(RuntimeError):
                probe.validate_standalone_nurture_proof(forged)

    def test_selftest_fixture_supports_development_backend_package(self):
        # Electron development starts backend.main; frozen Core imports app.
        # Both package names must bind fake browser hooks to their own modules.
        import backend.app.standalone_nurture_selftest as development
        with tempfile.TemporaryDirectory() as temporary:
            fixture = development.Fixture(Path(temporary), 'development-package')
            with fixture.patches():
                self.assertEqual(fixture.worker, development.studio.PlaywrightWorker)

    def test_fixture_observer_reads_commits_without_changing_production_database(self):
        from app.standalone_nurture_selftest import Fixture
        async def run(directory):
            fixture=Fixture(directory,'observer')
            ident=(await fixture.start(['observer-window'],config={'minutes':1}))[0]
            database_read=fixture.database.read
            with fixture.patches():
                observer,_=fixture._evidence_reader
                self.assertEqual(1,observer.execute('PRAGMA query_only').fetchone()[0])
                self.assertIsNone(observer.isolation_level);self.assertFalse(observer.in_transaction)
                self.assertEqual(database_read,fixture.database.read)
                with self.assertRaises(sqlite3.OperationalError):observer.execute("DELETE FROM studio_jobs")
                # No cached rows or relaxed writes: each actual production commit
                # is immediately visible through the separately held observer.
                with patch.object(fixture.database,'_connect',wraps=fixture.database._connect) as connect:
                    for cursor in range(1,6):
                        fixture.manager.update(ident,cursor=cursor)
                        self.assertEqual(cursor,fixture.job('observer-window')['cursor'])
                        self.assertFalse(observer.in_transaction)
                    self.assertEqual(5,connect.call_count,'fixture reads must not reopen production connections')
                self.assertEqual(5,await asyncio.to_thread(lambda:fixture.job('observer-window')['cursor']))
            self.assertIsNone(fixture._evidence_reader)
            with self.assertRaises(sqlite3.ProgrammingError):observer.execute('SELECT 1')
            fixture.reopen()
            self.assertEqual(5,fixture.job('observer-window')['cursor'])
        with tempfile.TemporaryDirectory() as temporary:asyncio.run(run(Path(temporary)))

    def test_fixture_observer_closes_on_error(self):
        from app.standalone_nurture_selftest import Fixture
        with tempfile.TemporaryDirectory() as temporary:
            fixture=Fixture(Path(temporary),'observer-error')
            with self.assertRaisesRegex(RuntimeError,'synthetic'):
                with fixture.patches():
                    observer,_=fixture._evidence_reader
                    raise RuntimeError('synthetic')
            self.assertIsNone(fixture._evidence_reader)
            with self.assertRaises(sqlite3.ProgrammingError):observer.execute('SELECT 1')

    def test_stage_evidence_is_flushed_and_does_not_swallow_failure(self):
        from app.standalone_nurture_selftest import proof_stage
        async def passed(_):return {'verified':True}
        async def failed(_):raise RuntimeError('synthetic')
        with patch('builtins.print') as emitted:
            self.assertEqual({'verified':True},asyncio.run(proof_stage('pass',passed,Path('.'))))
            with self.assertRaisesRegex(RuntimeError,'synthetic'):asyncio.run(proof_stage('fail',failed,Path('.')))
        rows=[json.loads(call.args[0].split('=',1)[1]) for call in emitted.call_args_list]
        self.assertEqual(['started','passed','started','failed'],[row['state'] for row in rows])
        self.assertTrue(all(call.kwargs.get('flush') is True for call in emitted.call_args_list))
        self.assertEqual('RuntimeError',rows[-1]['error_type'])

    def test_failed_source_cli_preserves_bounded_log_tail_after_temporary_cleanup(self):
        paths=[];marker='STANDALONE_NURTURE_STAGE={"case":"verified_playback","state":"started"}'
        def fail(command,path,**kwargs):
            paths.append(path);path.write_text('x'*40000+'\n'+marker,encoding='utf-8')
            raise RuntimeError('synthetic source watchdog')
        with patch.object(probe,'probe_standalone_nurture',side_effect=fail),patch('builtins.print') as emitted:
            with self.assertRaisesRegex(RuntimeError,'synthetic source watchdog'):
                self.test_real_source_cli_runs_complete_production_flow_and_rejects_partial_proof()
        self.assertFalse(paths[0].exists())
        emitted.assert_called_once()
        self.assertIn(marker,emitted.call_args.args[0])
        self.assertLessEqual(len(emitted.call_args.args[0]),32768+len('SOURCE_STANDALONE_NURTURE_LOG_TAIL='))
        self.assertTrue(emitted.call_args.kwargs.get('flush'))

    def test_cli_report_exposes_separate_standalone_nurture_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            report = directory / 'installed-report.json'
            standalone = {'verified': True, 'policy': 'standalone-reels-8-20-70-v1'}
            collection = {'verified': True, 'single_gap_recheck': {'verified': True, 'gap_passes': 2},
                          'manual_parent_recheck': {'verified': True, 'durable_pending_admission': True},
                          'final_seed_completion': {'verified':True,'exact_lease_until_confirmed_close':True}}
            with patch.object(probe, 'probe_core', return_value={'verified': True}), \
                 patch.object(probe, 'probe_collection_completion', return_value=collection), \
                 patch.object(probe, 'probe_standalone_nurture', return_value=standalone) as nurture, \
                 patch.object(sys, 'argv', ['verify', '--executable', sys.executable,
                    '--log', str(directory / 'core.log'), '--collection-completion', '--standalone-nurture', '--report', str(report)]):
                self.assertEqual(0, probe.main())
            result = json.loads(report.read_text())
            self.assertEqual(standalone, result['standalone_nurture'])
            self.assertTrue(result['standalone_nurture']['verified'])
            self.assertEqual(collection['single_gap_recheck'], result['single_gap_recheck'])
            self.assertEqual(collection['manual_parent_recheck'], result['manual_parent_recheck'])
            self.assertEqual(collection['final_seed_completion'], result['final_seed_completion'])
            self.assertEqual([str(Path(sys.executable).resolve())], nurture.call_args.args[0])

    def test_missing_forged_duplicate_or_nonzero_proof_fails_closed(self):
        for body in ('print("no proof")', 'print("STANDALONE_NURTURE_SELFTEST=PASS {}")',
                     'raise SystemExit(5)',
                     'print("STANDALONE_NURTURE_SELFTEST=PASS {}\\nSTANDALONE_NURTURE_SELFTEST=PASS {}")'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    probe.probe_standalone_nurture(self.command(body), Path(temporary) / 'proof.log')

    def test_timeout_is_release_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                probe.probe_standalone_nurture(self.command('import time; time.sleep(30)'),
                    Path(temporary) / 'proof.log', timeout=.05)

    def test_invalid_deadline_rejected(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                probe.probe_standalone_nurture([], Path('unused.log'), timeout=timeout)


if __name__ == '__main__':
    unittest.main()
