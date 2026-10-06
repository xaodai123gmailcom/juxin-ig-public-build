"""Read-only and old-schema guarantees for the standalone support diagnostic."""
from __future__ import annotations

from contextlib import closing, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("dedup_diagnostic", Path(__file__).resolve().parents[1] / "diagnose_dedupe.py")
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class DedupDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "collector.sqlite3"
        # Keep connections alive until teardown so Linux/GC cannot conceal the
        # open-file cleanup failure that Windows reports as WinError 32.
        self.connections = []
        self.sqlite_connect = sqlite3.connect
        self.addCleanup(self.close_connections)
        connect_patch = patch("sqlite3.connect", side_effect=self.tracked_connect)
        connect_patch.start()
        self.addCleanup(connect_patch.stop)

    def tracked_connect(self, *args, **kwargs):
        connection = self.sqlite_connect(*args, **kwargs)
        self.connections.append(connection)
        return connection

    def close_connections(self):
        for connection in self.connections:
            connection.close()

    def tearDown(self):
        open_connections = 0
        for connection in self.connections:
            try:
                connection.execute("SELECT 1").close()
            except sqlite3.ProgrammingError:
                pass  # A closed sqlite3 connection rejects further operations.
            else:
                open_connections += 1
        self.assertEqual(0, open_connections,
                         "SQLite connections must close before temporary directory cleanup")

    def fixture(self):
        # sqlite3's context manager ends a transaction; closing() owns the file.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE instagram_accounts(id TEXT, instagram_user_id TEXT, current_username_norm TEXT, current_username_display TEXT, first_seen_at TEXT, last_seen_at TEXT);
                INSERT INTO instagram_accounts VALUES('identity-one','12345','sample_dedupe01','sample_dedupe01','2026-09-01','2026-09-15');
                CREATE TABLE instagram_username_aliases(account_id TEXT, username_norm TEXT, first_seen_at TEXT, last_seen_at TEXT);
                INSERT INTO instagram_username_aliases VALUES('identity-one','sample_dedupe01','2026-09-01','2026-09-15');
                INSERT INTO instagram_username_aliases VALUES('identity-one','previous_name','2026-08-01','2026-09-01');
                CREATE TABLE global_seen(account_id TEXT, first_seen_at TEXT, last_seen_at TEXT, sources_json TEXT);
                CREATE TABLE global_seen_stats(singleton_id INTEGER, total_count INTEGER);
                INSERT INTO global_seen_stats VALUES(1,999);
                CREATE TABLE task_results(id TEXT, account_id TEXT, created_at TEXT, profile_json TEXT, screening_json TEXT);
                INSERT INTO task_results VALUES('result-old','identity-one','2026-09-01','SECRET_PROFILE','SECRET_SCREENING');
                INSERT INTO task_results VALUES('result-new','identity-one','2026-09-15','SECRET_PROFILE','SECRET_SCREENING');
                CREATE TABLE workbench_candidates(id TEXT, account_id TEXT, status TEXT, visibility TEXT, created_at TEXT, reviewed_at TEXT, review_cache_json TEXT);
                INSERT INTO workbench_candidates VALUES('candidate-one','identity-one','rejected','private','2026-09-01','2026-09-02','SECRET_IMAGE');
                CREATE TABLE workbench_review_decisions(id TEXT, candidate_id TEXT, decision TEXT, decided_at TEXT, profile_snapshot_json TEXT);
                INSERT INTO workbench_review_decisions VALUES('decision-one','candidate-one','rejected','2026-09-02','SECRET_SNAPSHOT');
                CREATE TABLE task_result_duplicate_archive(original_result_id TEXT, account_id TEXT, created_at TEXT, archived_at TEXT, profile_json TEXT);
                INSERT INTO task_result_duplicate_archive VALUES('archived-result','identity-one','2026-08-01','2026-09-15','SECRET_ARCHIVED_PROFILE');
                CREATE TABLE global_identity_owners(account_id TEXT, owner_user_id TEXT, first_seen_at TEXT, last_seen_at TEXT);
                INSERT INTO global_identity_owners VALUES('identity-one','SECRET_OWNER','2026-08-01','2026-09-15');
                CREATE TABLE app_users(id TEXT,password_hash TEXT);
                INSERT INTO app_users VALUES('owner','SECRET_PASSWORD');
                CREATE TABLE app_settings(key TEXT,value TEXT);
                INSERT INTO app_settings VALUES('token','SECRET_TOKEN');
            """)

    def test_identity_timeline_and_gaps_without_sensitive_payload_or_writes(self):
        self.fixture()
        before = self.path.read_bytes()
        report = diagnostic.inspect_database(self.path, ["sample_dedupe01", "missing_account"])
        self.assertEqual("inspected", report["status"])
        self.assertEqual(1, report["metrics"]["business_identities_missing_ledger"])
        self.assertEqual(1, report["metrics"]["duplicate_result_account_ids"])
        self.assertEqual(0, report["metrics"]["global_ledger_count"])
        self.assertEqual(999, report["metrics"]["displayed_ledger_count"])
        row = report["usernames"][0]
        self.assertEqual("12345", row["accounts"]["rows"][0]["instagram_user_id"])
        self.assertEqual(2, row["aliases"]["count"])
        self.assertEqual("rejected", row["workbench_candidates"]["rows"][0]["status"])
        self.assertEqual("2026-09-02", row["workbench_review_decisions"]["rows"][0]["decided_at"])
        self.assertEqual("archived-result", row["task_result_duplicate_archive"]["rows"][0]["original_result_id"])
        self.assertEqual("2026-08-01", row["global_identity_owners"]["rows"][0]["first_seen_at"])
        self.assertFalse(report["usernames"][1]["found"])
        self.assertNotIn("SECRET_", json.dumps(report))
        self.assertNotIn("password_hash", json.dumps(report))
        self.assertEqual(before, self.path.read_bytes())

    def test_minimal_old_schema_and_absent_file_remain_read_only(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE instagram_accounts(id TEXT, current_username_norm TEXT)")
            connection.execute("INSERT INTO instagram_accounts VALUES('old','SAMPLE_DEDUPE01')")
        before = self.path.read_bytes()
        result = diagnostic.inspect_database(self.path, ["sample_dedupe01"])
        self.assertTrue(result["usernames"][0]["found"])
        self.assertIsNone(result["metrics"]["global_ledger_count"])
        self.assertFalse(result["usernames"][0]["workbench_candidates"]["available"])
        self.assertEqual(before, self.path.read_bytes())
        missing = self.root / "missing.sqlite3"
        self.assertEqual("not_found", diagnostic.inspect_database(missing, ["x"])["status"])
        self.assertFalse(missing.exists())

    def test_committed_wal_records_are_included(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE instagram_accounts(id TEXT, current_username_norm TEXT)")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("INSERT INTO instagram_accounts VALUES('wal-identity','sample_dedupe01')")
            connection.commit()
            before = self.path.read_bytes()
            wal = Path(str(self.path) + "-wal")
            before_wal = wal.read_bytes()
            result = diagnostic.inspect_database(self.path, ["sample_dedupe01"])
            self.assertTrue(result["usernames"][0]["found"])
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(before_wal, wal.read_bytes())
        finally:
            connection.close()

    def test_read_failure_closes_database_without_exporting_error_content(self):
        self.fixture()
        before = self.path.read_bytes()
        with patch.object(diagnostic.Inspector, "metrics",
                          side_effect=sqlite3.OperationalError("SECRET_SQL_DETAILS")):
            report = diagnostic.inspect_database(self.path, ["sample_dedupe01"])
        self.assertEqual("read_failed", report["status"])
        self.assertEqual("OperationalError", report["error_type"])
        self.assertNotIn("SECRET_SQL_DETAILS", json.dumps(report))
        self.assertEqual(before, self.path.read_bytes())

    def test_exact_path_discovery_and_username_normalization(self):
        env = {"APPDATA": str(self.root / "Roaming"), "LOCALAPPDATA": str(self.root / "Local"), "IGAC_DB_PATH": str(self.path)}
        paths = diagnostic.database_candidates([str(self.path)], env)
        self.assertEqual(5, len(paths))
        # Discovery resolves paths, including Windows TEMP's 8.3 aliases.
        self.assertEqual(self.path.resolve(), paths[0])
        self.assertIn((self.root / "Roaming" / "聚鑫国际" / "data" / "collector.sqlite3").resolve(), paths)
        self.assertFalse((self.root / "Roaming").exists())
        self.assertEqual("sample_dedupe01", diagnostic.normalize_username(" https://www.instagram.com/SAMPLE_DEDUPE01/?x=1 "))
        with self.assertRaises(ValueError):
            diagnostic.normalize_username("https://example.com/user")

    def test_report_output_cannot_replace_database(self):
        # A SQLite database may have a JSON suffix; exact path protection is
        # required in addition to the report extension check.
        self.path = self.root / "existing.json"
        self.fixture()
        before = self.path.read_bytes()
        errors = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(errors), self.assertRaises(SystemExit):
            diagnostic.main(["--database", str(self.path), "--output", str(self.path)])
        self.assertIn("Output must be a JSON report file", errors.getvalue())
        self.assertEqual(before, self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
