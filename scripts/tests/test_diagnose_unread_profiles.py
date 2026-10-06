"""Focused read-only safeguards for the historical unread-profile diagnostic."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "unread_diagnostic", Path(__file__).resolve().parents[1] / "diagnose_unread_profiles.py"
)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class UnreadDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "collector.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE app_users(id TEXT,username_norm TEXT,username_display TEXT,password_hash TEXT);
                INSERT INTO app_users VALUES('owner','login','Login','SECRET_PASSWORD');
                CREATE TABLE tasks(id TEXT,owner_user_id TEXT,status TEXT);
                CREATE TABLE task_targets(id TEXT,task_id TEXT,status TEXT,current_stage TEXT);
                CREATE TABLE task_checkpoints(target_id TEXT,mode TEXT,stage TEXT);
                CREATE TABLE task_mode_candidates(target_id TEXT,mode TEXT,username_norm TEXT,state TEXT);
                CREATE TABLE instagram_accounts(id TEXT,current_username_norm TEXT);
                CREATE TABLE instagram_username_aliases(account_id TEXT,username_norm TEXT);
                CREATE TABLE task_results(id TEXT,task_id TEXT,target_id TEXT,account_id TEXT,
                    profile_json TEXT,screening_json TEXT,sources_json TEXT,created_at TEXT);
                CREATE TABLE global_seen(account_id TEXT);
                CREATE TABLE global_identity_owners(account_id TEXT,owner_user_id TEXT);
                CREATE TABLE workbench_identity_claims(account_id TEXT,claimed_by_user_id TEXT,source TEXT,source_target TEXT);
                CREATE TABLE workbench_candidates(id TEXT,account_id TEXT,owner_user_id TEXT,status TEXT,
                    review_stage INTEGER,review_transferred_at TEXT,profile_json TEXT);
                CREATE TABLE workbench_review_decisions(candidate_id TEXT);
                CREATE TABLE workbench_candidate_dismissals(candidate_id TEXT);
                CREATE TABLE workbench_collection_exclusions(account_id TEXT);
                CREATE TABLE task_result_duplicate_archive(account_id TEXT);
                CREATE TABLE browser_operation_leases(entity_id TEXT,operation_type TEXT);
            """)
        self.profile = json.dumps({
            "username": "SECRET_PROFILE", "page_read_status": "skipped_after_final_confirmation",
            "page_read_reason": "instagram_content_not_visible",
        })
        self.screening = json.dumps({"page_read": {
            "status": "partial", "reason": "instagram_content_not_visible",
        }, "message": "SECRET_SCREENING"})

    def add_unread(self, name: str, *, task_status="recoverable", target_status="recoverable", spool=True):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO tasks VALUES(?,?,?)", (name, "owner", task_status))
            connection.execute("INSERT INTO task_targets VALUES(?,?,?,?)", (name, name, target_status, None))
            connection.execute("INSERT INTO instagram_accounts VALUES(?,?)", (name, name))
            connection.execute("INSERT INTO instagram_username_aliases VALUES(?,?)", (name, name))
            connection.execute("INSERT INTO global_seen VALUES(?)", (name,))
            connection.execute("INSERT INTO global_identity_owners VALUES(?,?)", (name, "owner"))
            connection.execute("INSERT INTO workbench_identity_claims VALUES(?,?,?,?)",
                               (name, "owner", "followers", name))
            connection.execute("INSERT INTO task_results VALUES(?,?,?,?,?,?,?,?)",
                               (name, name, name, name, self.profile, self.screening, '["followers"]', "2026-09-01"))
            if spool:
                connection.execute("INSERT INTO task_mode_candidates VALUES(?,?,?,?)",
                                   (name, "followers", name, "recorded"))

    def test_unfinished_owned_claim_and_spool_is_only_eligible_class(self):
        self.add_unread("retry_me")
        self.add_unread("unreviewed_no_card")
        self.add_unread("reviewed")
        self.add_unread("excluded")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO workbench_candidates VALUES(?,?,?,?,?,?,?)",
                               ("c-pending", "retry_me", "owner", "pending", 1, None, self.profile))
            connection.execute("INSERT INTO workbench_candidates VALUES(?,?,?,?,?,?,?)",
                               ("c-review", "reviewed", "owner", "rejected", 1, None, self.profile))
            connection.execute("INSERT INTO workbench_review_decisions VALUES(?)", ("c-review",))
            connection.execute("INSERT INTO workbench_collection_exclusions VALUES(?)", ("excluded",))
        before = self.path.read_bytes()
        report = diagnostic.inspect_database(self.path)
        self.assertEqual("inspected", report["status"])
        self.assertEqual({"eligible_unfinished": 1, "review_pending_preserved": 1,
                          "manual_decision_preserved": 1, "other_terminal_history": 1}, report["counts"])
        self.assertEqual("unreviewed_no_card", next(r for r in report["rows"] if r["username"] == "unreviewed_no_card")["spool_username"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn("SECRET_", json.dumps(report))

    def test_completed_and_pruned_rows_remain_held(self):
        self.add_unread("completed", task_status="completed", target_status="completed")
        self.add_unread("pruned", spool=False)
        self.add_unread("checkpoint_done")
        self.add_unread("live", task_status="running", target_status="running")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO task_checkpoints VALUES('checkpoint_done','followers','mode_completed')")
        report = diagnostic.inspect_database(self.path)
        self.assertEqual(4, report["total"])
        self.assertEqual(3, report["counts"]["completed_or_pruned"])
        self.assertEqual(1, report["counts"]["active_execution_deferred"])
        self.assertNotIn("eligible_unfinished", report["counts"])

    def test_auto_discovery_multi_owner_and_mismatched_claim(self):
        self.add_unread("mismatch")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE workbench_identity_claims SET source_target='other' WHERE account_id='mismatch'")
            connection.execute("INSERT INTO app_users VALUES('other','other_login','Other Login','SECRET_HASH')")
        choose = diagnostic.inspect_database(self.path)
        self.assertEqual("owner_selection_required", choose["status"])
        self.assertEqual(2, len(choose["owners"]))
        report = diagnostic.inspect_database(self.path, "login")
        self.assertEqual({"claim_or_spool_mismatch": 1}, report["counts"])
        self.assertNotIn("SECRET_", json.dumps(report))
        env = {"APPDATA": str(self.path.parent / "Roaming"), "IGAC_DB_PATH": str(self.path)}
        # Discovery resolves paths, including Windows TEMP's 8.3 aliases.
        self.assertIn(self.path.resolve(), diagnostic.database_candidates(env))

    def test_committed_wal_decision_is_respected_without_writes(self):
        self.add_unread("wal_reviewed")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("INSERT INTO workbench_candidates VALUES(?,?,?,?,?,?,?)",
                               ("c-wal", "wal_reviewed", "owner", "approved", 1, None, self.profile))
            connection.execute("INSERT INTO workbench_review_decisions VALUES(?)", ("c-wal",))
            connection.commit()
            wal = Path(str(self.path) + "-wal")
            before = self.path.read_bytes(), wal.read_bytes()
            report = diagnostic.inspect_database(self.path)
            self.assertEqual({"manual_decision_preserved": 1}, report["counts"])
            self.assertEqual(before, (self.path.read_bytes(), wal.read_bytes()))
        finally:
            connection.close()

    def test_real_database_schema_is_supported(self):
        backend = Path(__file__).resolve().parents[2] / "backend"
        sys.path.insert(0, str(backend))
        try:
            from app.database import Database
            real_path = self.path.parent / "real-schema.sqlite3"
            Database(real_path).initialize()
            with closing(sqlite3.connect(real_path)) as connection:
                self.assertEqual([], diagnostic._has_schema(connection))
            self.assertEqual("owner_selection_required",
                             diagnostic.inspect_database(real_path)["status"])
        finally:
            sys.path.remove(str(backend))


if __name__ == "__main__":
    unittest.main()
