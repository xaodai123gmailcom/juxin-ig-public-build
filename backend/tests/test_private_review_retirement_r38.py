"""Private secondary retirement preserves pending review and durable history."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.service import CoreService


class PrivateReviewRetirementR38Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "review-retirement.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "private-review-r38", "private-review-regression-password")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, username, *, screening=None, visibility="private"):
        claim = self.service.claim_workbench_identity(
            self.owner, username=username, source="followers", source_target="source")
        return self.service.create_workbench_candidate(
            self.owner, claim_id=claim["claim_id"], username=username,
            visibility=visibility,
            profile={"username": username, "visibility": visibility,
                     "followers": None, "following": 22, "posts": 0},
            screening=screening or {"review_tier": "primary"},
            review_cache={"avatar_preview": "data:image/jpeg;base64,cHJpdmF0ZQ=="},
            source_mode="followers", source_target="source")

    def rows(self):
        with self.database.read() as connection:
            return {row["id"]: dict(row) for row in connection.execute(
                "SELECT * FROM workbench_candidates ORDER BY id")}

    def test_older_worker_markers_enter_one_pending_queue_without_mutating_input(self):
        for index, markers in enumerate((
            {"review_tier": "secondary"},
            {"routing_result": "private_secondary_review"},
            {"review_tier": " SECONDARY ", "routing_result": " PRIVATE_SECONDARY_REVIEW "},
        )):
            with self.subTest(markers=markers):
                screening = {"basic": {"passed": False, "reason_codes": ["followers_above_max"]},
                             "review_reason": "private_account_requires_secondary_review", **markers}
                original = copy.deepcopy(screening)
                candidate = self.candidate(f"older.private.{index}", screening=screening)
                self.assertEqual(original, screening)
                self.assertEqual("pending", candidate["status"])
                self.assertEqual("primary", candidate["screening"]["review_tier"])
                self.assertEqual("private_review", candidate["screening"]["routing_result"])
                self.assertEqual(original["basic"], candidate["screening"]["basic"])
                for key in ("review_tier", "review_reason", "routing_result"):
                    self.assertEqual(original.get(key), candidate["screening"]["legacy_private_review"][key])
        snapshot = self.service.get_workbench_snapshot(self.owner, limit=1)
        self.assertEqual(3, snapshot["counts"]["pending_private"])
        self.assertEqual(3, snapshot["counts"]["pending_private_primary"])
        self.assertEqual(0, snapshot["counts"]["pending_private_secondary"])
        self.assertEqual(1, len(snapshot["pending_private_accounts"]))
        self.assertTrue(snapshot["has_more"]["pending_private_accounts"])
        self.assertEqual([], snapshot["approved_private_accounts"])
        self.assertEqual([], snapshot["collection_exclusion_history"])

    def test_upgrade_merges_only_pending_private_rows_and_is_idempotent(self):
        tier = self.candidate("legacy.tier")
        route = self.candidate("legacy.route")
        primary = self.candidate("current.primary")
        approved = self.candidate("history.approved")
        rejected = self.candidate("history.rejected")
        public = self.candidate("current.public", visibility="public")
        malformed = self.candidate("legacy.malformed")
        for item, decision in ((approved, "approved"), (rejected, "rejected")):
            self.service.decide_workbench_candidate(
                self.owner, candidate_id=item["id"], decision=decision)
        # Settle the pre-existing startup cleanup of terminal preview caches; the
        # retirement migration itself must preserve every durable historical row.
        self.database.initialize()
        legacy_screenings = {
            tier["id"]: {"review_tier": " SECONDARY ", "basic": {"passed": False}},
            route["id"]: {"routing_result": "private_secondary_review", "basic": {"passed": None}},
            approved["id"]: {"review_tier": "secondary", "review_reason": "historical_decision"},
            rejected["id"]: {"review_tier": "secondary", "review_reason": "historical_decision"},
        }
        with self.database.write() as connection:
            for candidate_id, screening in legacy_screenings.items():
                connection.execute("UPDATE workbench_candidates SET screening_json=? WHERE id=?",
                                   (json.dumps(screening), candidate_id))
            connection.execute("UPDATE workbench_candidates SET screening_json='broken legacy json' WHERE id=?",
                               (malformed["id"],))
            connection.execute("DELETE FROM schema_migrations WHERE version=31")
            revision = connection.execute(
                "SELECT revision FROM workbench_state_revision WHERE singleton_id=1").fetchone()[0]
            preserved_tables = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("global_seen", "workbench_identity_claims", "workbench_review_decisions",
                              "action_campaigns", "workbench_collection_exclusions")
            }
        before = self.rows()
        self.database.initialize()
        after = self.rows()
        self.assertEqual(set(before), set(after))
        for candidate_id in (tier["id"], route["id"]):
            old = dict(before[candidate_id])
            new = dict(after[candidate_id])
            old.pop("screening_json")
            screening = json.loads(new.pop("screening_json"))
            self.assertEqual(old, new)  # IDs, cache, timestamps, status and all profile data.
            self.assertEqual("primary", screening["review_tier"])
            self.assertEqual("private_review", screening["routing_result"])
            self.assertEqual(legacy_screenings[candidate_id]["basic"], screening["basic"])
            self.assertEqual(legacy_screenings[candidate_id].get("review_tier"),
                             screening["legacy_private_review"]["review_tier"])
        for item in (primary, approved, rejected, public, malformed):
            self.assertEqual(before[item["id"]], after[item["id"]])
        with self.database.read() as connection:
            self.assertEqual(revision + 1, connection.execute(
                "SELECT revision FROM workbench_state_revision WHERE singleton_id=1").fetchone()[0])
            for table, expected in preserved_tables.items():
                self.assertEqual(expected, [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")])
        self.database.initialize()
        self.assertEqual(after, self.rows())
        with self.database.read() as connection:
            self.assertEqual(revision + 1, connection.execute(
                "SELECT revision FROM workbench_state_revision WHERE singleton_id=1").fetchone()[0])
        snapshot = self.service.get_workbench_snapshot(self.owner, limit=1)
        self.assertEqual(4, snapshot["counts"]["pending_private"])
        self.assertEqual(4, snapshot["counts"]["pending_private_primary"])
        self.assertEqual(0, snapshot["counts"]["pending_private_secondary"])

    def test_merged_private_candidate_still_needs_explicit_manual_decision(self):
        candidate = self.candidate("manual.only", screening={"review_tier": "secondary"})
        snapshot = self.service.get_workbench_snapshot(self.owner)
        self.assertEqual([candidate["id"]], [row["id"] for row in snapshot["pending_private_accounts"]])
        self.assertEqual([], snapshot["approved_private_accounts"])
        result = self.service.decide_workbench_candidate(
            self.owner, candidate_id=candidate["id"], decision="approved")
        self.assertEqual("approved_private", result["destination"])
        duplicate = self.service.claim_workbench_identity(
            self.owner, username="manual.only", source="followers", source_target="new.source")
        self.assertTrue(duplicate["duplicate"])
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM action_campaigns").fetchone()[0])

    def test_post_upgrade_crash_gap_restores_legacy_result_to_primary_without_rewriting_history(self):
        task = self.service.create_task(
            self.owner, name="legacy review gap", modes=["followers"], targets=["source"],
            settings={"local_person_recognition": False})
        target_id = task["targets"][0]["id"]
        for index, marker in enumerate((
            {"review_tier": "secondary"},
            {"routing_result": "private_secondary_review"},
        )):
            with self.subTest(marker=marker):
                username = f"private.crash.gap.{index}"
                self.service.append_task_mode_candidates(
                    self.owner, task["id"], target_id, "followers", [username])
                claim = self.service.claim_workbench_identity(
                    self.owner, username=username, source="followers", source_target=target_id)
                self.service.record_result(
                    self.owner, task["id"], target_id, username=username,
                    instagram_user_id=None, source_mode="followers", visibility="private",
                    profile={"username": username, "posts": 0, "followers": None},
                    screening={"basic": {"passed": False}, **marker}, qualified=False,
                    dedupe_claim_id=claim["claim_id"])
                with self.database.read() as connection:
                    before = dict(connection.execute(
                        "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],)).fetchone())
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],)).fetchone()[0])
                # A v31 migration already completed before this result/review gap.
                # Restart cannot rely on running that migration a second time.
                self.database.initialize()
                self.service.reconcile_task_mode_candidates(
                    self.owner, task["id"], target_id, "followers")
                with self.database.read() as connection:
                    after = dict(connection.execute(
                        "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],)).fetchone())
                    reviews = [dict(row) for row in connection.execute(
                        "SELECT * FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],))]
                self.assertEqual(before, after)
                self.assertEqual(1, len(reviews))
                self.assertEqual("pending", reviews[0]["status"])
                self.assertEqual(before["profile_json"], reviews[0]["profile_json"])
                screening = json.loads(reviews[0]["screening_json"])
                self.assertEqual("primary", screening["review_tier"])
                self.assertEqual("private_review", screening["routing_result"])
                for key, value in marker.items():
                    self.assertEqual(value, screening["legacy_private_review"][key])
                self.service.reconcile_task_mode_candidates(
                    self.owner, task["id"], target_id, "followers")
                with self.database.read() as connection:
                    self.assertEqual(reviews, [dict(row) for row in connection.execute(
                        "SELECT * FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],))])
                    self.assertEqual("recorded", connection.execute(
                        "SELECT state FROM task_mode_candidates WHERE target_id=? AND username_norm=?",
                        (target_id, username)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
