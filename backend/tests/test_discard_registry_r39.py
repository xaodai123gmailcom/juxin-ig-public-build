"""Terminal discard backups and pre-profile global dedupe survive failures/restarts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.service import CoreService


class DiscardRegistryR39Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("discard.registry.r39", "discard registry regression password")["id"]
        self.task = self.service.create_task(
            self.owner, name="registry", modes=["followers", "following"],
            targets=["source.registry"], settings={"local_person_recognition": True})
        self.target = self.task["targets"][0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, username, **kwargs):
        return self.service.claim_workbench_identity(
            self.owner, username=username, source="followers", source_target=self.target, **kwargs)

    def exclude(self, claim, **kwargs):
        return self.service.record_workbench_exclusion(
            self.owner, claim_id=claim["claim_id"], username=claim["username"],
            reason_code="account_count_ceiling_exceeded", reason="posts 4001 > 4000",
            location_country=None, profile={"visibility": "private", "posts": 4001, **kwargs})

    def record(self, username, *, excluded=False, visibility="private"):
        claim = self.claim(username)
        self.service.record_result(
            self.owner, self.task["id"], self.target, username=username,
            instagram_user_id=None, source_mode="followers", visibility=visibility,
            profile={"username": username, "posts": 4001 if excluded else 3},
            screening={"routing_result": "excluded_account_count_ceiling"} if excluded else {},
            qualified=False if excluded else None, dedupe_claim_id=claim["claim_id"])
        return claim

    def remove_lookup_rows(self, claim, *, remove_claim=False):
        # Emulate an imported/legacy DB with intact business history but missing indices.
        with self.database.write() as c:
            for table in ("global_seen", "instagram_username_aliases", "global_identity_owners"):
                c.execute(f"DELETE FROM {table} WHERE account_id=?", (claim["claim_id"],))
            if remove_claim:
                c.execute("DELETE FROM workbench_identity_claims WHERE account_id=?", (claim["claim_id"],))

    def test_exclusion_and_registry_repair_commit_or_rollback_together(self):
        claim = self.claim("discard.atomic")
        self.remove_lookup_rows(claim)
        with self.database.write() as c:
            c.execute("""CREATE TRIGGER reject_test_exclusion BEFORE INSERT ON workbench_collection_exclusions
                         BEGIN SELECT RAISE(ABORT, 'injected disk write failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected disk"):
            self.exclude(claim)
        with self.database.read() as c:
            for table in ("global_seen", "instagram_username_aliases", "global_identity_owners", "workbench_collection_exclusions"):
                self.assertEqual(0, c.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
        with self.database.write() as c:
            c.execute("DROP TRIGGER reject_test_exclusion")
        result = self.exclude(claim)
        self.assertEqual("excluded", result["outcome"])
        with self.database.read() as c:
            for table in ("global_seen", "instagram_username_aliases", "global_identity_owners", "workbench_collection_exclusions"):
                self.assertEqual(1, c.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
            sources = json.loads(c.execute("SELECT sources_json FROM global_seen WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
            self.assertIn("collection_excluded", sources)

    def test_repeated_discard_preserves_first_snapshot_and_restart_dedupes_aliases(self):
        claim = self.claim("Discard.Persist")
        original = self.exclude(claim, marker="first snapshot")
        repeated = self.exclude(claim, marker="late snapshot", posts=9999)
        self.assertEqual(original["id"], repeated["id"])
        restarted = CoreService(Database(self.path))
        for username in ("discard.persist", "@DISCARD.PERSIST", "https://www.instagram.com/Discard.Persist/"):
            repeated = restarted.claim_workbench_identity(
                self.owner, username=username, source="following", source_target="other-window-task")
            self.assertTrue(repeated["duplicate"])
            self.assertFalse(repeated["should_collect_profile"])
        with self.database.read() as c:
            rows = c.execute("SELECT profile_snapshot_json FROM workbench_collection_exclusions WHERE account_id=?", (claim["claim_id"],)).fetchall()
            self.assertEqual(1, len(rows))
            self.assertEqual("first snapshot", json.loads(rows[0][0])["marker"])

    def test_history_is_authoritative_after_lookup_rows_and_claim_are_lost(self):
        claim = self.claim("history.retained")
        self.exclude(claim)
        self.remove_lookup_rows(claim, remove_claim=True)
        self.assertTrue(self.service.check_global_dedupe("@HISTORY.RETAINED")["seen"])
        repeated = self.claim("https://instagram.com/history.retained/", allow_owned_resume=True)
        self.assertTrue(repeated["duplicate"])
        self.assertFalse(repeated["should_collect_profile"])
        with self.database.read() as c:
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM global_seen WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM workbench_collection_exclusions WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM workbench_identity_claims WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])

    def test_independent_instances_racing_normalized_names_reserve_only_once(self):
        names = ("Concurrent.Person", "@concurrent.person", "https://instagram.com/CONCURRENT.PERSON/")
        def attempt(name):
            service = CoreService(Database(self.path))
            return service.claim_workbench_identity(self.owner, username=name, source="followers", source_target=name)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(attempt, names))
        self.assertEqual(1, sum(result["claimed"] for result in results))
        self.assertEqual(2, sum(result["duplicate"] for result in results))
        winner = next(result for result in results if result["claimed"])
        self.exclude(winner)
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertTrue(all(result["duplicate"] for result in pool.map(attempt, names)))

    def test_stable_id_alias_survives_discard_without_new_account_or_profile(self):
        claim = self.claim("old.handle", instagram_user_id="90012345")
        self.exclude(claim)
        renamed = self.service.claim_workbench_identity(
            self.owner, username="new.handle", instagram_user_id="90012345", source="following")
        self.assertTrue(renamed["duplicate"])
        self.assertEqual(claim["account_id"], renamed["account_id"])
        restarted = CoreService(Database(self.path))
        result = restarted.claim_workbench_identity(self.owner, username="@NEW.HANDLE", source="followers")
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["should_collect_profile"])

    def test_legacy_excluded_result_cannot_reopen_or_be_restored_to_private_review(self):
        for visibility in ("private", "public"):
            claim = self.record(f"legacy.excluded.{visibility}", excluded=True, visibility=visibility)
            stats = self.service.append_task_mode_candidates(
                self.owner, self.task["id"], self.target, "followers", [claim["username"]])
            self.assertEqual(0, stats["pending"])
            self.assertTrue(self.claim(claim["username"], allow_owned_resume=True)["duplicate"])
            with self.database.read() as c:
                self.assertEqual(0, c.execute("SELECT COUNT(*) FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
                self.assertEqual(1, c.execute("SELECT COUNT(*) FROM task_results WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])

    def test_unfinished_owned_claim_can_resume_until_result_exists(self):
        claim = self.claim("unfinished.pending")
        resumed = self.claim("UNFINISHED.PENDING", allow_owned_resume=True)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(claim["claim_id"], resumed["claim_id"])
        stats = self.service.append_task_mode_candidates(
            self.owner, self.task["id"], self.target, "followers", [claim["username"]])
        self.assertEqual(1, stats["pending"])
        with self.database.read() as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM task_results WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])

    def test_rank_zero_history_is_batch_deduped_without_per_account_recognition_queries(self):
        names = [f"legacy.batch.{index}" for index in range(40)]
        for name in names:
            self.record(name)
        other = self.service.create_task(
            self.owner, name="second window", modes=["following"], targets=["source.other"],
            settings={"local_person_recognition": True})
        statements = []
        original_connect = self.database._connect
        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection
        self.database._connect = traced_connect
        try:
            stats = self.service.append_task_mode_candidates(
                self.owner, other["id"], other["targets"][0]["id"], "following", names)
        finally:
            self.database._connect = original_connect
        self.assertEqual(0, stats["pending"])
        self.assertEqual(len(names), stats["deduped"])
        # INSERT/executemany and trigger invocations are expected per-name. The
        # removed rank-0 path issued SELECTs per identity; reconciliation is bounded.
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertLess(len(selects), 20, selects)
        for name in names:
            self.assertFalse(self.service.check_global_dedupe(name)["person_recognition_needed"])


if __name__ == "__main__":
    unittest.main()
