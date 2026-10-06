"""Exercise the release startup probe against a real separate Core process."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CoreReleaseStartupR94Tests(unittest.TestCase):
    def test_installed_probe_checks_scaled_counts_heartbeat_and_pure_instagram_together(self):
        spec = importlib.util.spec_from_file_location('release_core_scale_probe', ROOT / 'scripts/verify_frozen_core_service.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        paths = [str(ROOT / 'backend'), *[str(Path(p).resolve()) for p in sys.path if p]]
        code = f'import sys; sys.path[:0]={json.dumps(paths)}; from app.__main__ import main; main()'
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'core-scale.log'
            try:
                result = probe.probe_core([sys.executable, '-c', code], log, timeout=45,
                    pure_ig_smoke=True, snapshot_scale_smoke=True,
                    snapshot_result_count=240, snapshot_identity_count=320)
            except Exception as error:
                self.fail(f'{error}\n{log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""}')
            self.assertTrue(result['pure_instagram'])
            self.assertTrue(result['snapshot_scale']['verified'])
            report = result['work_report_summary']
            self.assertEqual(result['snapshot_scale']['work_report_summary'], report)
            self.assertTrue(report['verified'])
            self.assertTrue(report['summary_legacy_totals_equal'])
            self.assertTrue(report['fixture_totals_verified'])
            self.assertTrue(report['repeated_summary_equal'])
            self.assertEqual({'collection': 240, 'follow': 0, 'split': 0, 'added': 0, 'confirmed_posting': 0}, report['totals'])
            self.assertLess(report['summary_bytes'], report['legacy_bytes'])
            for field in ('summary_first_seconds', 'summary_repeat_seconds', 'legacy_full_seconds'):
                self.assertGreaterEqual(report[field], 0)
            self.assertEqual(240, result['snapshot_scale']['collected'])
            self.assertEqual(320, result['snapshot_scale']['identities'])
            self.assertTrue(result['snapshot_scale']['pure_ig_upgrade']['ig_identity_hash_preserved'])
            self.assertTrue(result['snapshot_scale']['pure_ig_upgrade']['classified_rows_removed'])
            self.assertTrue(result['snapshot_scale']['normal_restart']['verified'])
            self.assertTrue(result['snapshot_scale']['normal_restart']['retained_data'])
            upgrade = result['report_index_upgrade']
            self.assertEqual(upgrade, result['snapshot_scale']['report_index_upgrade'])
            for key in ('verified', 'legacy_index_preserved', 'target_period_index_created',
                        'inventory_dedup_hashes_preserved', 'window_leases_preserved',
                        'repeated_startup_idempotent'):
                self.assertTrue(upgrade[key])
            self.assertEqual(2, upgrade['restart_count'])
            self.assertEqual({'collection': 3, 'follow': 1, 'split': 3, 'added': 7, 'confirmed_posting': 1}, upgrade['five_card_totals'])
            self.assertGreater(upgrade['retained_rows']['browser_operation_leases']['rows'], 0)
            self.assertGreaterEqual(upgrade['retained_rows']['global_seen']['rows'], 324)
            self.assertEqual(2, len(upgrade['report_timings']))
            self.assertEqual(3, len(result['snapshot_scale']['performance_indexes_verified']))
            self.assertTrue(result['snapshot_scale']['compact_wire']['canonical_rows_equal'])
            self.assertLess(result['snapshot_scale']['compact_wire']['compact_bytes'],
                            result['snapshot_scale']['compact_wire']['legacy_bytes'])
            self.assertEqual(2, len(result['snapshot_scale']['snapshot_seconds']))
            self.assertEqual(3, len(result['snapshot_scale']['heartbeat_seconds']))
            self.assertEqual({'instagram'}, set(result['snapshot_scale']['platform_snapshot_seconds']))
            self.assertTrue(result['snapshot_scale']['legacy_platform_counter_upgrade']['verified'])
            self.assertTrue(result['snapshot_scale']['legacy_platform_counter_upgrade']['records_preserved'])
            self.assertTrue(result['snapshot_scale']['legacy_platform_counter_upgrade']['legacy_review_columns_restored'])

    def test_real_service_initializes_schema_authenticates_and_exits(self):
        spec = importlib.util.spec_from_file_location('release_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        # Explicit paths let the source-only test use the test interpreter's
        # dependencies. The release CLI always executes the frozen EXE directly.
        paths = [str(ROOT / 'backend'), *[str(Path(p).resolve()) for p in sys.path if p]]
        code = f'import sys; sys.path[:0]={json.dumps(paths)}; from app.__main__ import main; main()'
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'core.log'
            try:
                result = probe.probe_core([sys.executable, '-c', code], log, timeout=45)
            except Exception as error:
                self.fail(f'{error}\n{log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""}')
            self.assertEqual({'verified': True, 'source_revision': 'stability-r94',
                              'authentication': True, 'database': 'ok', 'orderly_shutdown': True}, result)


if __name__ == '__main__':
    unittest.main()
