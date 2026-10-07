"""Real prior-index ownership must survive an R6.1 startup without data edits."""
import asyncio
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from app.config import Settings
from app.database import Database
from app.errors import ConflictError
from app.main import create_app
from app.posting_schema import initialize_posting_schema
from app.service import CoreService
from app.work_reports import work_report, _split_work_report_sql, _report_time


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('report_index_fixture', ROOT / 'scripts/report_index_upgrade_fixture.py')
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class ReportIndexUpgradeR61Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Juxin-ReportIndex-')
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'reports.db')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('r61-owner', 'fixture password only')['id']
        with self.db.write() as c:
            initialize_posting_schema(c)
        fixture.seed(self.db.path, self.owner, isolated_directory=self.temp.name)
        self.db.initialize()

    def report(self, summary=True):
        return work_report(self.db, self.owner, fixture.START, fixture.END, 'instagram', summary_only=summary)

    def check_reports(self):
        before = fixture.state(self.db.path)
        for _ in range(2):
            self.assertEqual(fixture.EXPECTED, self.report()['totals'])
            full = self.report(summary=False)
            self.assertEqual(fixture.EXPECTED, {key: full['totals'][key] for key in fixture.EXPECTED})
        self.assertEqual(before, fixture.state(self.db.path), 'Report reads must not alter data or leases')

    def check_upgrade(self, owner_table):
        legacy = fixture.install_old_index(self.db.path, isolated_directory=self.temp.name, owner_table=owner_table)
        before = fixture.state(self.db.path)
        for _ in range(2):
            self.db.initialize()
            self.check_reports()
            proof = fixture.verify(self.db.path, before, legacy)
            self.assertTrue(proof['window_leases_preserved'])
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, 'r61-held-window',
                    operation_type='collection', entity_id='competing-fixture')
        with self.db.read() as c:
            c.create_function('report_time', 1, _report_time, deterministic=True)
            params = {'owner': self.owner, 'start': fixture.START, 'end': fixture.END,
                      'exact_start': _report_time(fixture.START), 'exact_end': _report_time(fixture.END)}
            plan = '\n'.join(row[3] for row in c.execute('EXPLAIN QUERY PLAN ' + _split_work_report_sql('instagram') + 'SELECT COUNT(*) FROM first_completions WHERE rank=1', params))
        self.assertIn(fixture.TARGET_INDEX, plan)
        self.assertIn('idx_split_history_report_period', plan)
        self.assertIn('idx_split_history_report_identity', plan)

    def test_fresh_database_reports_all_four_positive_totals(self):
        self.check_reports()

    def test_full_lifecycle_preserves_unknown_owner_fence_for_both_index_layouts(self):
        async def restart_twice(owner_table):
            legacy = fixture.install_old_index(self.db.path, isolated_directory=self.temp.name,
                                              owner_table=owner_table)
            before = fixture.state(self.db.path)
            for _ in range(2):
                browser = Mock(spec=['native', 'list_all_windows', 'open_profile',
                                     'close_profile', 'closed_profile_guard'])
                browser.native = None
                for method in ('list_all_windows', 'open_profile', 'close_profile', 'closed_profile_guard'):
                    getattr(browser, method).side_effect = AssertionError('Unexpected browser operation')
                app = create_app(Settings(
                    startup_token='isolated-index-upgrade-fixture-token',
                    database_path=self.db.path, data_dir=Path(self.temp.name)),
                    database=Database(self.db.path), bitbrowser=browser)
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0)
                    self.check_reports()
                    self.assertTrue(fixture.verify(self.db.path, before, legacy)['window_leases_preserved'])
                    for operation in ('collection', 'studio'):
                        with self.assertRaises(ConflictError):
                            app.state.service.acquire_browser_lease(self.owner, 'r61-held-window',
                                operation_type=operation, entity_id='competing-fixture')
                    with self.db.read() as c:
                        self.assertIsNone(c.execute(
                            "SELECT id FROM studio_jobs WHERE id='r61-unknown-studio-owner'").fetchone())
                # Shutdown and scheduled recovery must not silently edit the fence.
                self.assertTrue(fixture.verify(self.db.path, before, legacy)['window_leases_preserved'])
                self.assertEqual([], browser.mock_calls)
        for owner_table in ('split_candidate_history', 'split_completed_targets'):
            with self.subTest(owner_table=owner_table):
                asyncio.run(restart_twice(owner_table))

    def test_fixture_verification_rejects_ownership_fence_token_edits_and_deletion(self):
        legacy = fixture.install_old_index(self.db.path, isolated_directory=self.temp.name)
        self.db.initialize()
        before = fixture.state(self.db.path)
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET lease_token='changed-fixture' WHERE profile_id='r61-held-window'")
        with self.assertRaisesRegex(AssertionError, 'browser_operation_leases'):
            fixture.verify(self.db.path, before, legacy)
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET lease_token='r61-fixture-lease' WHERE profile_id='r61-held-window'")
        fixture.verify(self.db.path, before, legacy)
        with self.db.write() as c:
            c.execute("DELETE FROM browser_operation_leases WHERE profile_id='r61-held-window'")
        with self.assertRaisesRegex(AssertionError, 'browser_operation_leases'):
            fixture.verify(self.db.path, before, legacy)

    def test_legacy_history_owned_index_upgrade_preserves_every_record_and_lock(self):
        self.check_upgrade('split_candidate_history')

    def test_already_r6_target_owned_index_upgrade_preserves_every_record_and_lock(self):
        self.check_upgrade('split_completed_targets')

    def test_fixture_verification_rejects_changed_ledger_not_zero_fallback(self):
        legacy = fixture.install_old_index(self.db.path, isolated_directory=self.temp.name)
        self.db.initialize()
        before = fixture.state(self.db.path)
        with self.db.write() as c:
            c.execute("UPDATE global_seen SET sources_json='[\"changed-fixture\"]' WHERE account_id='r61-old'")
        with self.assertRaisesRegex(AssertionError, 'global_seen'):
            fixture.verify(self.db.path, before, legacy)

    def test_index_rename_keeps_exact_query_plan_and_instruction_count(self):
        fixture.install_old_index(self.db.path, isolated_directory=self.temp.name,
                                  owner_table='split_completed_targets')
        self.db.initialize()
        with self.db.write() as c:
            c.executemany("INSERT INTO split_completed_targets(target_id,owner_user_id,username_norm,username_display,source_task_id,completed_at) VALUES(?,?,'fixture','fixture','fixture',?)",
                          [(f'perf-{i}', self.owner, fixture.NOW if i < 200 else fixture.OLD) for i in range(4000)])
        sql = _split_work_report_sql('instagram') + 'SELECT COUNT(*) FROM first_completions WHERE rank=1'
        params = {'owner': self.owner, 'start': fixture.START, 'end': fixture.END,
                  'exact_start': _report_time(fixture.START), 'exact_end': _report_time(fixture.END)}
        plans, steps = [], []
        for query in (sql.replace(fixture.TARGET_INDEX, fixture.OLD_INDEX), sql):
            with self.db.read() as c:
                c.create_function('report_time', 1, _report_time, deterministic=True)
                plans.append([row[3].replace(fixture.TARGET_INDEX, fixture.OLD_INDEX)
                              for row in c.execute('EXPLAIN QUERY PLAN ' + query, params)])
                count = [0]
                def tick():
                    count[0] += 1
                    return 0
                c.set_progress_handler(tick, 1)
                self.assertEqual(203, c.execute(query, params).fetchone()[0])
                steps.append(count[0])
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(steps[0], steps[1], 'Index rename must not add scans or VM work')


if __name__ == '__main__':
    unittest.main()
