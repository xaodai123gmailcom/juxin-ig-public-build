"""Pure SQLite migration/oracle tests, not an installed or native runtime pass."""
from contextlib import contextmanager, closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

fixture = load('archive_recovery_fixture', ROOT / 'scripts/recovery_upgrade_fixture_r64.py')
retirement = load('archive_production_migration', ROOT / 'backend/app/posting_retirement.py')

class DatabaseHarness:
    """Only SQLite/instance-lock protocol; no application/native imports."""
    def __init__(self, path):
        self.path = Path(path)
        self.browser_surface_lock = threading.RLock()
        self._instance_lock_file = None
    def acquire_instance_lock(self):
        assert self._instance_lock_file is None
        self._instance_lock_file = object()
    def release_instance_lock(self):
        self._instance_lock_file = None
    @contextmanager
    def write(self):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            yield c

class PostingArchiveOracleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        _, self.manifest = fixture.seed(self.root)
        self.database = DatabaseHarness(self.root / 'collector.sqlite3')
    def migrate(self):
        value = retirement.retire_legacy_posting(self.database)
        self.assertIsNone(self.database._instance_lock_file)
        return value
    def test_real_migration_archives_idle_keeps_unknown_owner_and_is_idempotent(self):
        result = self.migrate()
        self.assertEqual(4, result['retired_rows'])
        self.assertEqual(1, len(result['quarantined_jobs']))
        before = fixture.inspect(self.root, self.manifest, phase='startup')
        self.assertEqual(5, before['archive']['archived_rows'])
        self.assertEqual(0, self.migrate()['retired_rows'])
        self.assertEqual(before, fixture.inspect(self.root, self.manifest, phase='startup'))
    def test_changed_archived_caption_is_rejected(self):
        self.migrate()
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(archive)) as c, c:
            c.execute("UPDATE archived_rows SET row_json=json_set(row_json,'$.caption','lost') WHERE source_table='posting_jobs'")
        with self.assertRaises(RuntimeError): fixture.inspect(self.root, self.manifest, phase='startup')
    def test_missing_archive_schema_is_rejected(self):
        self.migrate()
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(archive)) as c, c:
            c.execute('DELETE FROM archived_schema')
        with self.assertRaisesRegex(RuntimeError, 'archive schema is missing'):
            fixture.inspect(self.root, self.manifest, phase='startup')
    def test_table_only_archive_even_with_rehashed_script_is_rejected(self):
        self.migrate()
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        import hashlib
        with closing(sqlite3.connect(self.database.path)) as source:
            table_sql = source.execute("SELECT sql FROM sqlite_master WHERE name='posting_assets'").fetchone()[0] + ';'
        with closing(sqlite3.connect(archive)) as c, c:
            c.execute("UPDATE archived_schema SET schema_sql=?,schema_sha256=? WHERE source_table='posting_assets'", (table_sql, hashlib.sha256(table_sql.encode()).hexdigest()))
        with self.assertRaisesRegex(RuntimeError, 'archive schema is missing'):
            fixture.inspect(self.root, self.manifest, phase='startup')
    def remove_schema_object_from_both(self, kind, name):
        self.migrate()
        original = self.manifest['archive_schema_before']['posting_assets']
        self.assertTrue(any(row['type'] == kind and row['name'] == name for row in original))
        with self.database.write() as c:
            c.execute('DROP ' + kind.upper() + ' ' + name)
        remaining = [row for row in original if row['name'] != name]
        ordered = sorted(remaining, key=lambda row: ({'table': 0, 'index': 1, 'trigger': 2}[row['type']], row['name']))
        script = '\n'.join(row['sql'].rstrip(';') + ';' for row in ordered)
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        import hashlib
        with closing(sqlite3.connect(archive)) as c, c:
            c.execute("UPDATE archived_schema SET schema_sql=?,schema_sha256=? WHERE source_table='posting_assets'", (script, hashlib.sha256(script.encode()).hexdigest()))
        with self.assertRaisesRegex(RuntimeError, 'independent prelaunch authority'):
            fixture.inspect(self.root, self.manifest, phase='startup')

    def test_coordinated_unique_index_loss_from_live_and_rehashed_archive_fails(self):
        self.remove_schema_object_from_both('index', 'posting_asset_hash')

    def test_coordinated_trigger_loss_from_live_and_rehashed_archive_fails(self):
        self.remove_schema_object_from_both('trigger', 'offline_r64_material_guard')

    def test_prelaunch_schema_authority_is_saved_before_candidate_runs(self):
        saved = json.loads((self.root / 'recovery-input.json').read_text())
        self.assertEqual(self.manifest['archive_schema_before'], saved['archive_schema_before'])
        self.migrate()
        self.assertEqual(self.manifest['archive_schema_before'], saved['archive_schema_before'])
        import copy
        missing = copy.deepcopy(self.manifest); missing['archive_schema_before'] = {}
        with self.assertRaisesRegex(RuntimeError, 'prelaunch schema authority is missing'):
            fixture.inspect(self.root, missing, phase='startup')

    def test_unapproved_archived_field_even_with_matching_hash_is_rejected(self):
        self.migrate()
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        import hashlib
        with closing(sqlite3.connect(archive)) as c, c:
            key, text = c.execute("SELECT row_key,row_json FROM archived_rows WHERE source_table='posting_jobs' ORDER BY row_key LIMIT 1").fetchone()
            row = json.loads(text); row['queue_revision'] = 0
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            c.execute("UPDATE archived_rows SET row_json=?,row_sha256=? WHERE source_table='posting_jobs' AND row_key=?", (payload, hashlib.sha256(payload.encode()).hexdigest(), key))
        with self.assertRaisesRegex(RuntimeError, 'Original posting row missing or changed'):
            fixture.inspect(self.root, self.manifest, phase='startup')

    def test_changed_material_or_active_owner_is_rejected(self):
        self.migrate()
        archive = Path(str(self.database.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(archive)) as c:
            path = Path(c.execute('SELECT archive_path FROM files').fetchone()[0])
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'material bytes changed'):
            fixture.inspect(self.root, self.manifest, phase='startup')
    def test_missing_material_aborts_all_retirement(self):
        before = fixture.read_state(self.root)
        (self.root / 'synthetic-material.bin').unlink()
        with self.assertRaises(retirement.PostingRetirementError): self.migrate()
        self.assertEqual(before, fixture.read_state(self.root))
    def test_lease_theft_and_row_loss_are_independently_rejected(self):
        self.migrate()
        with self.database.write() as c:
            c.execute("UPDATE browser_operation_leases SET owner_user_id=?", (fixture.OWNER,))
        with self.assertRaisesRegex(RuntimeError, 'ownership was removed or stolen'):
            fixture.inspect(self.root, self.manifest, phase='startup')

if __name__ == '__main__': unittest.main()
