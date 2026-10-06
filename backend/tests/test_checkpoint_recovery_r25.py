"""Durable collection crash boundaries, using reopened SQLite and no browser."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.database import Database
from app.service import CoreService


class CheckpointRecoveryR25Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "checkpoint.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "checkpoint-r25", "test password sufficiently long"
        )["id"]
        self.task = self.service.create_task(
            self.owner, name="Durable recovery", modes=["followers"],
            targets=["recovery.source"], window_ids=["window-one"],
            settings={"local_person_recognition": True},
        )
        self.task_id = self.task["id"]
        self.target_id = self.task["targets"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *usernames):
        return self.service.append_task_mode_candidates(
            self.owner, self.task_id, self.target_id, "followers", usernames,
        )

    def claim(self, username):
        return self.service.claim_workbench_identity(
            self.owner, username=username, source="followers",
            source_target=self.target_id, allow_owned_resume=True,
        )

    def record(self, claim):
        return self.service.record_result(
            self.owner, self.task_id, self.target_id,
            username=claim["username"], instagram_user_id=None,
            source_mode="followers", visibility="private",
            profile={"is_private": True, "marker": "saved-before-interruption"},
            screening={}, qualified=None, dedupe_claim_id=claim["claim_id"],
        )

    def reconcile(self):
        return self.service.reconcile_task_mode_candidates(
            self.owner, self.task_id, self.target_id, "followers",
        )

    def reopen_after_interruption(self):
        # All connections in the old service are scoped and closed. Construct a
        # new database/service to exercise disk data, startup projections and
        # runtime recovery rather than reusing in-memory state.
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.service.recover_interrupted_operations()

    def rows(self, table):
        with self.database.read() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_unread_claim_stays_pending_across_restart_and_can_resume_only_original_target(self):
        self.append("unread.person", "next.person")
        first = self.claim("unread.person")
        self.service.set_task_runtime_status(self.owner, self.task_id, "running")
        self.service.set_target_runtime_status(
            self.owner, self.task_id, self.target_id, "running", window_id="window-one",
        )
        self.service.upsert_checkpoint(
            self.owner, self.task_id, self.target_id, mode="followers",
            stage="screening_accounts", cursor={"candidate_spool_complete": True,
                "candidate_spool_natural_end": True}, counters={"discovered": 2, "processed": 0},
        )
        self.reopen_after_interruption()
        self.assertEqual({"total": 2, "pending": 2, "recorded": 0, "deduped": 0}, self.reconcile())
        resumed = self.claim("unread.person")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(first["claim_id"], resumed["claim_id"])
        self.assertEqual([], self.rows("task_results"))
        self.assertEqual([], self.rows("workbench_candidates"))
        foreign = self.service.claim_workbench_identity(
            self.owner, username="unread.person", source="followers",
            source_target="different-target", allow_owned_resume=True,
        )
        self.assertTrue(foreign["duplicate"])
        self.assertFalse(foreign["should_collect_profile"])
        checkpoint = self.service.get_checkpoint(self.owner, self.task_id, self.target_id, "followers")
        self.assertTrue(checkpoint["cursor"]["candidate_spool_natural_end"])
        self.assertEqual(0, checkpoint["counters"]["processed"])

    def test_committed_result_gap_recovers_once_without_counting_repeated_discovery(self):
        self.append("committed.person", "unread.person")
        claim = self.claim("committed.person")
        self.record(claim)
        before = self.rows("task_results")
        self.reopen_after_interruption()
        expected = {"total": 2, "pending": 1, "recorded": 1, "deduped": 0}
        self.assertEqual(expected, self.reconcile())
        review = self.rows("workbench_candidates")
        self.assertEqual(1, len(review))
        self.assertEqual("saved-before-interruption", json.loads(review[0]["profile_json"])["marker"])
        self.service.decide_workbench_candidate(self.owner, candidate_id=review[0]["id"], decision="rejected")
        for _ in range(2):
            self.reopen_after_interruption()
            self.append("@COMMITTED.PERSON", "unread.person", "committed.person")
            self.assertEqual(expected, self.reconcile())
            self.assertTrue(self.claim("committed.person")["duplicate"])
        self.assertEqual(before, self.rows("task_results"))
        after_review = self.rows("workbench_candidates")
        self.assertEqual(1, len(after_review))
        self.assertEqual(review[0]["id"], after_review[0]["id"])
        self.assertEqual("rejected", after_review[0]["status"])

    def test_completed_exclusion_gap_does_not_reopen_or_inflate_collected_count(self):
        self.append("public.zero.posts", "unread.person")
        claim = self.claim("public.zero.posts")
        self.service.record_workbench_exclusion(
            self.owner, claim_id=claim["claim_id"], username=claim["username"],
            reason_code="public_zero_posts", reason="Confirmed public profile has zero posts",
            location_country=None, profile={"is_private": False, "posts": 0},
        )
        exclusion = self.rows("workbench_collection_exclusions")
        for _ in range(2):
            self.reopen_after_interruption()
            self.assertEqual({"total": 2, "pending": 1, "recorded": 0, "deduped": 1}, self.reconcile())
            self.assertTrue(self.claim("public.zero.posts")["duplicate"])
        self.assertEqual(exclusion, self.rows("workbench_collection_exclusions"))
        self.assertEqual([], self.rows("task_results"))
        self.assertEqual([], self.rows("workbench_candidates"))

    def test_historical_terminal_exclusion_skips_owned_hover_without_a_result(self):
        for username, reason_code in (
            ("old.zero", "public_zero_posts"),
            ("old.review", "legacy_public_secondary_review"),
        ):
            with self.subTest(reason_code=reason_code):
                claim = self.claim(username)
                self.service.record_workbench_exclusion(
                    self.owner, claim_id=claim["claim_id"], username=username,
                    reason_code=reason_code, reason="Historical terminal exclusion",
                    location_country=None,
                    profile={"visibility": "public", "posts": 0},
                )
                self.reopen_after_interruption()
                self.assertTrue(self.service.should_skip_relationship_hover(
                    self.owner, username, source="followers", source_target=self.target_id
                ))
                repeated = self.claim(username)
                self.assertTrue(repeated["duplicate"])
                self.assertFalse(repeated["should_collect_profile"])
        self.assertEqual([], self.rows("task_results"))
        self.assertEqual(2, len(self.rows("workbench_collection_exclusions")))

    def test_renamed_identity_confirmed_before_interruption_keeps_one_result_and_alias(self):
        self.append("original.person")
        original = self.claim("original.person")
        self.service.confirm_workbench_identity(
            self.owner, claim_id=original["claim_id"], username="original.person", instagram_user_id="551199",
        )
        self.record(original)
        self.reconcile()
        self.append("renamed.person")
        renamed = self.claim("renamed.person")
        confirmation = self.service.confirm_workbench_identity(
            self.owner, claim_id=renamed["claim_id"], username="renamed.person", instagram_user_id="551199",
        )
        self.assertTrue(confirmation["duplicate"])
        self.assertTrue(confirmation["placeholder_removed"])
        self.reopen_after_interruption()
        self.assertEqual({"total": 2, "pending": 0, "recorded": 1, "deduped": 1}, self.reconcile())
        # Two discovered aliases are two processed list rows, but one collection.
        progress = self.service.get_task(self.owner, self.task_id)["targets"][0]["mode_progress"]["followers"]
        self.assertEqual(1, progress["saved"])
        self.assertEqual(2, progress["processed"])
        self.assertEqual(1, len(self.rows("task_results")))
        self.assertEqual(1, len(self.rows("workbench_candidates")))
        self.assertTrue(self.claim("original.person")["duplicate"])
        self.assertTrue(self.claim("renamed.person")["duplicate"])
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM instagram_accounts WHERE instagram_user_id='551199'").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_two_pending_aliases_recover_as_one_saved_identity_with_either_recognition_setting(self):
        for recognition_enabled in (False, True):
            with self.subTest(recognition_enabled=recognition_enabled):
                suffix = str(int(recognition_enabled))
                task = self.service.create_task(
                    self.owner, name="Alias commit gap", modes=["followers"],
                    targets=[f"alias.source.{suffix}"],
                    settings={"local_person_recognition": recognition_enabled},
                )
                self.task_id = task["id"]
                self.target_id = task["targets"][0]["id"]
                original_username = f"alias.original.{suffix}"
                renamed_username = f"alias.renamed.{suffix}"
                self.append(original_username, renamed_username)
                original = self.claim(original_username)
                self.service.confirm_workbench_identity(
                    self.owner, claim_id=original["claim_id"], username=original_username,
                    instagram_user_id=f"99110{suffix}",
                )
                self.record(original)
                renamed = self.claim(renamed_username)
                self.service.confirm_workbench_identity(
                    self.owner, claim_id=renamed["claim_id"], username=renamed_username,
                    instagram_user_id=f"99110{suffix}",
                )
                self.reopen_after_interruption()
                for _ in range(2):
                    self.assertEqual({"total": 2, "pending": 0, "recorded": 1, "deduped": 1}, self.reconcile())
                progress = self.service.get_task(self.owner, self.task_id)["targets"][0]["mode_progress"]["followers"]
                self.assertEqual(1, progress["saved"])
                self.assertEqual(2, progress["processed"])
                self.assertEqual(1, len(self.service.list_results(self.owner, self.task_id)))

    def test_stopped_target_retries_original_checkpoint_and_preserves_terminal_counter(self):
        self.append("finished.person", "remaining.person")
        self.record(self.claim("finished.person"))
        expected = {"total": 2, "pending": 1, "recorded": 1, "deduped": 0}
        self.assertEqual(expected, self.reconcile())
        cursor = {"candidate_spool_complete": True, "candidate_spool_natural_end": True}
        self.service.upsert_checkpoint(
            self.owner, self.task_id, self.target_id, mode="followers",
            stage="screening_accounts", cursor=cursor, counters={"discovered": 2, "processed": 1},
        )
        self.service.set_task_runtime_status(self.owner, self.task_id, "stopped")
        self.service.set_target_runtime_status(self.owner, self.task_id, self.target_id, "stopped")
        self.reopen_after_interruption()
        target = self.service.retry_task_target(self.owner, self.task_id, self.target_id)
        self.assertEqual(self.target_id, target["id"])
        self.assertEqual("pending", target["status"])
        self.append("finished.person", "remaining.person")
        self.assertEqual(expected, self.reconcile())
        checkpoint = self.service.get_checkpoint(self.owner, self.task_id, self.target_id, "followers")
        self.assertEqual(cursor, checkpoint["cursor"])
        self.assertEqual(1, checkpoint["counters"]["processed"])
        self.assertEqual(1, len(self.rows("task_results")))

    def test_restart_retains_recovery_deadline_and_original_failure_classification(self):
        cursor = {"candidate_spool_complete": True, "candidate_spool_natural_end": True}
        for kind, reason, original_reason in (
            ("network", "network_unavailable", "network_unavailable"),
            ("surface", "instagram_page_recovery_exhausted", "instagram_profile_not_ready"),
            ("manual_required", "instagram_login_required", "instagram_login_required"),
        ):
            with self.subTest(wait_kind=kind):
                self.service.upsert_checkpoint(
                    self.owner, self.task_id, self.target_id, mode="followers",
                    stage="waiting_network",
                    cursor={"resume_stage": "screening_accounts", "resume_cursor": cursor},
                    counters={"reason": reason, "message": "Original waiting state",
                        "original_reason": original_reason, "wait_kind": kind,
                        "next_retry_at": "2099-01-02T03:04:05+00:00", "retry_delay_seconds": 300,
                        "retry_count": 4, "surface_retry_count": 3, "list_retry_count": 2,
                        "no_progress_retry_kind": "surface", "recovery_target": "unread.person",
                        "candidate_username": "unread.person", "source_discovery_complete": True,
                        "previous_counters": {"discovered": 2, "processed": 1}},
                )
                for _ in range(2):
                    self.reopen_after_interruption()
                    saved = self.service.get_checkpoint(self.owner, self.task_id, self.target_id, "followers")
                    self.assertEqual("interrupted_recoverable", saved["stage"])
                    self.assertEqual({"resume_stage": "screening_accounts", "resume_cursor": cursor}, saved["cursor"])
                    retained = saved["counters"]
                    self.assertEqual(reason, retained["reason"])
                    self.assertEqual(kind, retained["wait_kind"])
                    self.assertEqual(original_reason, retained.get("original_reason"))
                    self.assertEqual("2099-01-02T03:04:05+00:00", retained.get("next_retry_at"))
                    self.assertEqual(300, retained.get("retry_delay_seconds"))
                    self.assertEqual(4, retained.get("retry_count"))
                    self.assertEqual(3, retained.get("surface_retry_count"))
                    self.assertEqual(2, retained.get("list_retry_count"))
                    self.assertEqual("surface", retained.get("no_progress_retry_kind"))
                    self.assertEqual("unread.person", retained.get("recovery_target"))
                    self.assertEqual("unread.person", retained.get("candidate_username"))
                    self.assertTrue(retained.get("source_discovery_complete"))
                    self.assertEqual({"discovered": 2, "processed": 1}, retained["previous_counters"])


if __name__ == "__main__":
    unittest.main()
