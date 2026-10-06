"""Storage telemetry races and full-database failures must be distinguishable."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import CoreService


class SnapshotStorageR94Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / 'core.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('storage-probe', 'storage-probe-password')['id']

    def snapshot(self):
        return self.service.get_workbench_snapshot(self.owner, limit=10, history_limit=10, maintain=False)

    def test_wal_removed_between_exists_and_stat_does_not_fail_snapshot(self):
        original = Path.stat
        probes = []
        def stat(path, *args, **kwargs):
            if path.name == 'core.sqlite3-wal':
                probes.append(1)
                if len(probes) > 1:
                    raise FileNotFoundError('WAL checkpoint completed')
                from types import SimpleNamespace
                return SimpleNamespace(st_size=16384)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            result = self.snapshot()
        self.assertEqual(0, result['counts']['total_collected'])
        self.assertLessEqual(len(probes), 1, 'check-then-stat races with WAL recycling')

    def test_file_probe_permission_error_does_not_disable_business_snapshot(self):
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path.name == 'core.sqlite3-wal':
                raise PermissionError('temporary file inspection failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            result = self.snapshot()
        self.assertEqual(0, result['counts']['total_collected'])
        self.assertNotIn('wal_bytes', result['storage'])
        self.assertFalse(result['storage']['file_probe_available'])

    def test_disk_probe_failure_is_unknown_instead_of_zero_free_space(self):
        with patch('app.service.shutil.disk_usage', side_effect=OSError('disk query unavailable')):
            storage = self.snapshot()['storage']
        self.assertNotIn('disk_free_bytes', storage)
        self.assertFalse(storage['disk_probe_available'])
        self.assertIsNone(storage['low_space_warning'])

    def test_full_disk_measurement_preserves_zero_as_a_real_observation(self):
        with patch('app.service.shutil.disk_usage', return_value=(100 * 1024**3, 100 * 1024**3, 0)):
            storage = self.snapshot()['storage']
        self.assertEqual(0, storage['disk_free_bytes'])
        self.assertTrue(storage['disk_probe_available'])
        self.assertTrue(storage['low_space_warning'])

    def app(self):
        from test_snapshot_availability_r94 import Inventory
        settings = Settings(startup_token='snapshot-storage-startup-token',
                            database_path=self.db.path, data_dir=self.db.path.parent)
        app = create_app(settings, database=self.db, bitbrowser=Inventory())
        login = self.service.login('storage-probe', 'storage-probe-password')
        headers = {'X-Startup-Token': settings.startup_token, 'Authorization': 'Bearer ' + login['token']}
        return app, headers

    async def test_storage_diagnostic_does_not_need_the_failed_full_snapshot(self):
        app, headers = self.app()
        with patch.object(app.state.service, 'get_workbench_snapshot', side_effect=AssertionError('must not build full snapshot')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/api/workbench/storage', headers=headers)
                rejected = await client.get('/api/workbench/storage', headers={'X-Startup-Token': headers['X-Startup-Token']})
        self.assertEqual(200, response.status_code)
        self.assertGreater(response.json()['disk_total_bytes'], 0)
        self.assertEqual(401, rejected.status_code)

    async def test_real_sqlite_page_limit_error_is_reported_as_storage_full(self):
        app, headers = self.app()
        def full(*args, **kwargs):
            with self.db.write() as c:
                c.execute('CREATE TABLE fullness_probe(payload BLOB)')
                pages = c.execute('PRAGMA page_count').fetchone()[0]
                c.execute(f'PRAGMA max_page_count={pages + 1}')
                c.execute('INSERT INTO fullness_probe VALUES(zeroblob(2097152))')
        with patch.object(app.state.service, 'get_workbench_snapshot', side_effect=full):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
                response = await client.get('/api/workbench/snapshot', headers=headers)
        self.assertEqual(507, response.status_code, response.text)
        self.assertEqual('storage_full', response.json()['code'])
        self.assertIn('空间', response.json()['detail'])
        self.assertEqual(0, self.snapshot()['counts']['total_collected'])

    async def test_contention_readonly_and_unknown_errors_are_not_mislabelled_full(self):
        app, headers = self.app()
        for code, expected in ((sqlite3.SQLITE_BUSY, 'database_busy'),
                               (sqlite3.SQLITE_READONLY, 'database_readonly'),
                               (sqlite3.SQLITE_ERROR, 'database_error')):
            with self.subTest(code=code):
                error = sqlite3.OperationalError('controlled SQLite fault')
                error.sqlite_errorcode = code
                with patch.object(app.state.service, 'get_workbench_snapshot', side_effect=error):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
                        response = await client.get('/api/workbench/snapshot', headers=headers)
                self.assertEqual(expected, response.json()['code'])
                self.assertNotEqual(507, response.status_code)


class SnapshotTaskPayloadR94Tests(unittest.TestCase):
    def test_progress_counts_do_not_reparse_retained_profile_payloads(self):
        # Real SQLite expression/covering indexes must serve each refresh even
        # after a restart; matching small-fixture counts alone cannot prove this.
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / 'progress.sqlite3'); db.initialize()
            service = CoreService(db)
            owner = service.register_user('counter-probe', 'counter-probe-password')['id']
            task = service.create_task(owner, name='Large exclusions', modes=['followers'],
                targets=['source_one'], settings={})
            target = task['targets'][0]['id']
            now = '2026-10-01T00:00:00+00:00'
            count = 1200
            large = json.dumps({'page_read_status': 'profile', 'bio': 'x' * 16384})
            hover = json.dumps({'page_read_status': 'hover_preview', 'bio': 'x' * 16384})
            with db.write() as c:
                c.executemany('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)',
                    ((f'a{i}', f'user_{i}', f'user_{i}', now, now) for i in range(count)))
                c.executemany('INSERT INTO workbench_identity_claims VALUES(?,?,?,?,?)',
                    ((f'a{i}', owner, 'followers', target, now) for i in range(count)))
                c.executemany('''INSERT INTO workbench_collection_exclusions
                    (id,account_id,owner_user_id,username_display,reason_code,reason,
                     profile_snapshot_json,excluded_at) VALUES(?,?,?,?,?,?,?,?)''',
                    ((f'e{i}', f'a{i}', owner, f'user_{i}', 'test', 'test',
                      hover if i % 10 == 0 else large, now) for i in range(count)))
            service.append_task_mode_candidates(owner, task['id'], target, 'followers', ['user_0'])
            # Exercise an old installation acquiring the new index, not just a
            # clean database whose indexes were empty at creation.
            with db.write() as c:
                c.execute('DROP INDEX idx_workbench_exclusions_progress')
            db.initialize()
            parsed = []
            with db.read() as c, sqlite3.connect(':memory:') as evaluator:
                def tracked_extract(value, path):
                    if len(value or '') > 8192:
                        parsed.append(len(value))
                    return evaluator.execute('SELECT json_extract(?,?)', (value, path)).fetchone()[0]
                c.create_function('json_extract', 2, tracked_extract, deterministic=True)
                for _ in range(3):
                    progress = service._mode_progress_for_targets(c, [target])[target]['followers']
                    self.assertEqual(count, progress['discarded'])
                    self.assertEqual(count // 10, progress['hover_discarded'])
                    self.assertEqual(0, progress['qualified_for_review'])
            self.assertEqual([], parsed, 'live progress reparsed retained exclusion profiles')

    def test_task_page_evaluates_payload_only_for_returned_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / 'task.sqlite3'); db.initialize()
            service = CoreService(db)
            owner = service.register_user('payload-probe', 'payload-probe-password')['id']
            task = service.create_task(owner, name='Large history', modes=['followers'],
                targets=[f'source_{i}' for i in range(300)], window_ids=['w1'], settings={})
            payload = json.dumps({'bio': 'x' * 32768})
            with db.write() as c:
                c.execute("UPDATE task_targets SET status=CASE WHEN queue_order=299 THEN 'running' ELSE 'completed' END,source_profile_json=?", (payload,))
                columns = [r[1] for r in c.execute('PRAGMA table_info(task_targets)')]
            original = db._connect
            evaluated = []
            def connect():
                c = original()
                def probe(value):
                    evaluated.append(len(value)); return value
                c.create_function('task_payload_probe', 1, probe)
                projection = ','.join('task_payload_probe(source_profile_json) AS source_profile_json'
                                      if name == 'source_profile_json' else name for name in columns)
                c.execute('CREATE TEMP VIEW task_targets AS SELECT ' + projection + ' FROM main.task_targets')
                return c
            expected = service.list_tasks_with_details(owner, limit=1, detail_limit=3)
            with patch.object(db, '_connect', side_effect=connect):
                actual = service.list_tasks_with_details(owner, limit=1, detail_limit=3)
            self.assertEqual(expected, actual)
            self.assertEqual(3, len(evaluated), 'bounded snapshot still materializes all 300 historical payloads')
            self.assertTrue(actual[0]['targets_truncated'])
            self.assertIn('running', {t['status'] for t in actual[0]['targets']})
            self.assertEqual(task['id'], actual[0]['id'])


if __name__ == '__main__':
    unittest.main()
