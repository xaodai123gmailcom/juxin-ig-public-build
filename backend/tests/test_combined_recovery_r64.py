"""Legacy idle retirement and hidden collection recovery on the same window.

The posting executor/routes are gone. Retiring an idle posting association must
never stand in for a versioned collection stop or authoritative window closure.
"""
import json
from pathlib import Path
import unittest
from app.posting_retirement import retire_legacy_posting
import test_hidden_collection_blocker_r63 as hidden

ROOT = Path(__file__).resolve().parents[2]

class CombinedRecoveryR64Tests(unittest.TestCase):
    setUp = hidden.HiddenCollectionBlockerTests.setUp
    task = hidden.HiddenCollectionBlockerTests.task
    command = hidden.HiddenCollectionBlockerTests.command
    locate = hidden.HiddenCollectionBlockerTests.locate
    stop = hidden.HiddenCollectionBlockerTests.stop
    retained = hidden.HiddenCollectionBlockerTests.retained

    def seed_legacy_post(self):
        # Exact independent legacy table, not a removed publishing initializer.
        source = (ROOT / 'scripts/fixtures/nurture_cleanup_upgrade_r62.sql').read_text()
        statement = next(part.strip() for part in source.split(';') if part.strip().startswith('CREATE TABLE posting_jobs('))
        with self.db.write() as c:
            c.execute(statement)
            c.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,profile_id,status,created_at,updated_at) VALUES('legacy-idle',?,'legacy-idle','offline',?,'w1','queued','historical','historical')", (self.owner, 'Exact preserved caption\n保留 🌲'))
            before = dict(c.execute("SELECT * FROM posting_jobs WHERE id='legacy-idle'").fetchone())
        return before

    def assert_archived(self, original):
        import sqlite3
        from contextlib import closing
        archive = Path(str(self.db.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(archive)) as c:
            saved = json.loads(c.execute("SELECT row_json FROM archived_rows WHERE source_table='posting_jobs'").fetchone()[0])
        self.assertEqual(original, saved)
        with self.db.read() as c:
            self.assertEqual(0, c.execute('SELECT count(*) FROM posting_jobs').fetchone()[0])

    def test_retire_then_stop_preserves_collection_and_requires_separate_cleanup(self):
        original = self.seed_legacy_post(); before = self.retained()
        result = retire_legacy_posting(self.db)
        self.assertEqual(1, result['retired_rows']); self.assert_archived(original)
        self.assertEqual(before, self.retained())
        self.assertEqual(409, self.command('control', operation='retry_cleanup').status_code)
        blocker = self.locate()
        self.assertEqual(409, self.command('stop_cleanup_collection', task_id=self.ident, version=blocker['version']-1).status_code)
        self.assertEqual(200, self.stop(blocker).status_code)
        self.assertEqual(before, self.retained())
        clean = self.command('control', operation='retry_cleanup')
        self.assertEqual(200, clean.status_code, clean.text)
        self.assertTrue(clean.json()['cleanup_reconciled'])
        self.assert_archived(original)
        self.assertEqual(404, self.client.post('/api/posting/command', headers=self.headers['owner'], json={'action':'start','job_ids':['legacy-idle']}).status_code)

    def test_stop_then_retire_never_replays_or_restores_hidden_history(self):
        original = self.seed_legacy_post(); before = self.retained()
        self.assertEqual(200, self.stop().status_code)
        self.assertEqual(before, self.retained())
        self.assertEqual(1, retire_legacy_posting(self.db)['retired_rows'])
        self.assert_archived(original)
        self.assertEqual(before, self.retained())
        self.assertEqual(0, retire_legacy_posting(self.db)['retired_rows'])
        self.assertIsNone(self.locate())
        self.assertEqual(200, self.command('control', operation='retry_cleanup').status_code)

    def test_successor_owner_blocks_stop_and_survives_retirement(self):
        original = self.seed_legacy_post(); before = self.retained()
        with self.db.write() as c:
            c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?,?,?,?)',
                ('w1',self.owners['other'],'account','successor','different-generation','2099','2099','2099'))
        with self.db.read() as c: lease = tuple(c.execute('SELECT * FROM browser_operation_leases').fetchone())
        retire_legacy_posting(self.db)
        self.assert_archived(original)
        self.assertFalse(self.locate()['can_stop'])
        self.assertEqual(409, self.stop().status_code)
        self.assertEqual(409, self.command('control', operation='retry_cleanup').status_code)
        self.assertEqual(before, self.retained())
        with self.db.read() as c: self.assertEqual(lease, tuple(c.execute('SELECT * FROM browser_operation_leases').fetchone()))

class InstalledScenarioAsgiContractTests(unittest.TestCase):
    def test_exact_installed_scenario_uses_real_routes_and_independent_oracle(self):
        """ASGI + mocked closed browser only; never an installed release claim."""
        import importlib.util
        import tempfile
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.database import Database
        from app.main import create_app
        spec = importlib.util.spec_from_file_location('installed_scenario_contract', ROOT / 'scripts/verify_installed_recovery_r64.py')
        probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
        with tempfile.TemporaryDirectory(prefix='r64-exact-api-mock-') as directory:
            root = Path(directory); _, manifest = probe.fixture.seed(root)
            settings = Settings(startup_token=manifest['nonce'], database_path=root / 'collector.sqlite3', data_dir=root)
            app = create_app(settings, database=Database(settings.database_path), bitbrowser=hidden.Browser())
            with TestClient(app) as client:
                headers = {'X-Startup-Token': settings.startup_token}
                def api(path, body=None):
                    response = client.get(path, headers=headers) if body is None else client.post(path, headers=headers, json=body)
                    value = response.json()
                    if path == '/api/session/resume' and response.is_success:
                        headers['Authorization'] = 'Bearer ' + value['session_token']
                    return {'ok': True, 'body': value} if response.is_success else {'ok': False, 'status': response.status_code, 'error': value}
                result = probe.exercise(api, root, manifest)
                self.assertEqual(dict.fromkeys(probe.FLAGS, True), result['checks'])
                self.assertEqual(17, len(result['api_calls']))
                self.assertEqual(5, result['persisted_state']['archive']['archived_rows'])
            probe.fixture.inspect(root, manifest, phase='complete')

if __name__ == '__main__': unittest.main()
