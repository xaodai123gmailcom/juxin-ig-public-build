"""Task-page dedupe counts come from durable source-scoped progress, not totals."""
from pathlib import Path
import tempfile
import unittest

from app.database import Database
from app.service import CoreService


class ProgressDedupeR46Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "progress.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("progress-r46", "long enough test password")["id"]
        self.task = self.service.create_task(
            self.owner, name="Dedupe display", modes=["followers", "following"],
            targets=["source.one", "source.two"], settings={},
        )
        self.task_id = self.task["id"]
        self.target = self.task["targets"][0]["id"]
        self.other = self.task["targets"][1]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def append(self, usernames, *, mode="followers", target=None):
        return self.service.append_task_mode_candidates(
            self.owner, self.task_id, target or self.target, mode, usernames,
        )

    def finish(self, username, state, *, mode="followers", target=None):
        return self.service.finish_task_mode_candidate(
            self.owner, self.task_id, target or self.target, mode, username, state=state,
        )

    def checkpoint(self, counters, *, mode="followers", stage="screening_accounts"):
        self.service.upsert_checkpoint(
            self.owner, self.task_id, self.target, mode=mode, stage=stage,
            cursor={"candidate_spool_complete": True, "candidate_spool_natural_end": True},
            counters=counters,
        )

    def progress(self, *, target=None, mode="followers"):
        task = self.service.get_task(self.owner, self.task_id)
        return next(row for row in task["targets"] if row["id"] == (target or self.target))["mode_progress"][mode]

    def reopen(self):
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)

    def collect_for_review(self, username, *, mode="followers", target=None,
                           visibility="private", create_candidate=True,
                           candidate_mode=None, candidate_target=None,
                           qualified=True):
        target = target or self.target
        self.append([username], mode=mode, target=target)
        claim = self.service.claim_workbench_identity(
            self.owner, username=username, source=mode, source_target=target)
        profile = {"username": username, "visibility": visibility, "posts": 5}
        screening = {"review_tier": "primary", "routing_result": (
            "private_review" if visibility == "private" else "public_primary_review")}
        self.service.record_result(
            self.owner, self.task_id, target, username=username,
            instagram_user_id=None, source_mode=mode, visibility=visibility,
            profile=profile, screening=screening, qualified=qualified,
            dedupe_claim_id=claim["claim_id"],
        )
        candidate = None
        if create_candidate:
            candidate = self.service.create_workbench_candidate(
                self.owner, claim_id=claim["claim_id"], username=username,
                visibility=visibility, profile=profile, screening=screening,
                review_cache={}, source_mode=candidate_mode if candidate_mode is not None else mode,
                source_target=candidate_target if candidate_target is not None else target,
            )
        self.service.reconcile_task_mode_candidates(
            self.owner, self.task_id, target, mode)
        return candidate

    def test_legacy_foreign_relation_metadata_does_not_project_into_ig_progress(self):
        self.append(["one.profile"])
        self.checkpoint({"previous_counters": {"facebook_selected_relation": "friends", "source_total": 7}})
        self.assertNotIn("facebook_selected_relation", self.progress())
        self.assertEqual(7, self.progress()["source_total"])
        self.checkpoint({"facebook_selected_relation": "following", "source_total": 7,
                         "previous_counters": {"facebook_selected_relation": "friends"}})
        self.assertNotIn("facebook_selected_relation", self.progress())
        self.reopen()
        self.assertNotIn("facebook_selected_relation", self.progress())
        self.assertEqual(7, self.progress()["source_total"])
        for invalid in ("invalid", [], {}, None):
            self.checkpoint({"facebook_selected_relation": invalid})
            self.assertNotIn("facebook_selected_relation", self.progress())

    def test_qualified_count_is_actual_review_admission_not_saved_or_approved(self):
        self.assertEqual(0, self.progress()["qualified_for_review"])
        public = self.collect_for_review(
            "public.unknown", visibility="public", qualified=None)
        private = self.collect_for_review(
            "private.ok", mode="following", visibility="private")
        self.append(["direct.exclude"])
        excluded_claim = self.service.claim_workbench_identity(
            self.owner, username="direct.exclude", source="followers", source_target=self.target)
        self.service.record_workbench_exclusion(
            self.owner, claim_id=excluded_claim["claim_id"], username="direct.exclude",
            reason_code="account_count_ceiling", reason="超过上限",
            location_country=None,
            profile={"username": "direct.exclude", "visibility": "private"},
        )
        self.service.record_result(
            self.owner, self.task_id, self.target, username="direct.exclude",
            instagram_user_id=None, source_mode="followers", visibility="private",
            profile={"username": "direct.exclude", "visibility": "private"},
            screening={"routing_result": "excluded_account_count_ceiling"},
            qualified=False, dedupe_claim_id=excluded_claim["claim_id"],
        )
        self.service.reconcile_task_mode_candidates(
            self.owner, self.task_id, self.target, "followers")
        self.assertEqual(2, self.progress()["saved"])
        self.assertEqual(1, self.progress()["qualified_for_review"])
        self.assertEqual(1, self.progress(mode="following")["qualified_for_review"])
        self.assertEqual(0, self.progress(target=self.other)["qualified_for_review"])
        self.service.decide_workbench_candidate(
            self.owner, candidate_id=public["id"], decision="approved")
        self.service.decide_workbench_candidate(
            self.owner, candidate_id=private["id"], decision="rejected")
        for _ in range(2):
            self.assertEqual(1, self.progress()["qualified_for_review"])
            self.assertEqual(1, self.progress(mode="following")["qualified_for_review"])
            live = self.service.get_task_live_status(
                self.owner, self.task_id, current_target_ids=[self.target])
            row = next(item for item in live["targets"] if item["id"] == self.target)
            self.assertEqual(1, row["mode_progress"]["followers"]["qualified_for_review"])
            history = next(item for item in self.service.list_history(self.owner, task_id=self.task_id)
                           if item["target_id"] == self.target)
            self.assertEqual(1, history["mode_progress"]["following"]["qualified_for_review"])
            self.reopen()

    def test_missing_or_wrong_source_cannot_inflate_actual_review_admissions(self):
        # A saved result is not evidence that its account entered review.
        self.append(["result.first"])
        self.service.record_result(
            self.owner, self.task_id, self.target, username="result.first",
            instagram_user_id=None, source_mode="followers", visibility="private",
            profile={"username": "result.first", "visibility": "private"},
            screening={"routing_result": "private_review"}, qualified=True,
        )
        self.service.reconcile_task_mode_candidates(
            self.owner, self.task_id, self.target, "followers")
        self.assertEqual(0, self.progress()["qualified_for_review"])
        legacy = self.collect_for_review(
            "missing.source", mode="following", candidate_mode="", candidate_target="")
        # Old candidates omitted optional source metadata. Their original
        # bound claim and result still prove that they entered this mode.
        self.assertEqual(1, self.progress(mode="following")["qualified_for_review"])
        with self.database.write() as connection:
            connection.execute(
                "UPDATE workbench_identity_claims SET source_target=NULL WHERE account_id=?",
                (legacy["account_id"],),
            )
        self.assertEqual(0, self.progress(mode="following")["qualified_for_review"])
        with self.database.write() as connection:
            connection.execute(
                "UPDATE workbench_identity_claims SET source_target=? WHERE account_id=?",
                (self.target, legacy["account_id"]),
            )
        self.assertEqual(1, self.progress(mode="following")["qualified_for_review"])
        self.reopen()
        self.assertEqual(1, self.progress(mode="following")["qualified_for_review"])
        # An unrelated queue row cannot count for another source mode or target.
        self.assertEqual(0, self.progress(target=self.other)["qualified_for_review"])
        self.assertEqual(0, self.progress()["qualified_for_review"])
        # Valid review evidence remains visible even alongside an old, unattributed
        # saved result. A completely unprovable compacted old checkpoint is unknown.
        self.collect_for_review("proper.source")
        self.assertEqual(1, self.progress()["qualified_for_review"])
        self.service.upsert_checkpoint(
            self.owner, self.task_id, self.other, mode="following", stage="mode_completed",
            cursor={}, counters={"saved": 5, "processed": 5})
        self.assertIsNone(self.progress(target=self.other, mode="following")["qualified_for_review"])

    def test_review_admission_requires_matching_owner_claim_target_and_mode(self):
        candidate = self.collect_for_review("verified.origin")
        account_id = candidate["account_id"]
        self.assertEqual(1, self.progress()["qualified_for_review"])
        # A second JSON source alone does not change the immutable first claim.
        with self.database.write() as c:
            c.execute("UPDATE task_results SET sources_json=? WHERE account_id=?",
                      ('["followers","following"]', account_id))
            c.execute("UPDATE workbench_candidates SET source_mode='following' WHERE id=?",
                      (candidate["id"],))
        self.assertEqual(0, self.progress(mode="following")["qualified_for_review"])
        with self.database.write() as c:
            c.execute("UPDATE workbench_candidates SET source_mode='followers',source_target=? WHERE id=?",
                      (self.other, candidate["id"]))
        self.assertEqual(0, self.progress()["qualified_for_review"])
        self.assertEqual(0, self.progress(target=self.other)["qualified_for_review"])

        another_owner = self.service.register_user("other-progress-owner", "long enough test password")["id"]
        with self.database.write() as c:
            c.execute("UPDATE workbench_candidates SET source_target=?,owner_user_id=? WHERE id=?",
                      (self.target, another_owner, candidate["id"]))
        self.assertEqual(0, self.progress()["qualified_for_review"])

        # An invalid or non-array historical JSON source must not break task
        # status reads, and cannot prove that the account came from followers.
        with self.database.write() as c:
            c.execute("UPDATE workbench_candidates SET owner_user_id=? WHERE id=?",
                      (self.owner, candidate["id"]))
        for broken_source in ("{broken", '{"mode":"followers"}'):
            with self.subTest(sources_json=broken_source):
                with self.database.write() as c:
                    c.execute("UPDATE task_results SET sources_json=? WHERE account_id=?",
                              (broken_source, account_id))
                self.assertEqual(0, self.progress()["qualified_for_review"])
        with self.database.write() as c:
            c.execute("UPDATE task_results SET sources_json=? WHERE account_id=?",
                      ('["followers"]', account_id))
        self.assertEqual(1, self.progress()["qualified_for_review"])

    def test_compacted_old_direct_exclusion_is_known_zero(self):
        self.append(["old.direct.exclusion"])
        claim = self.service.claim_workbench_identity(
            self.owner, username="old.direct.exclusion", source="followers",
            source_target=self.target)
        profile = {"username": "old.direct.exclusion", "visibility": "private"}
        self.service.record_workbench_exclusion(
            self.owner, claim_id=claim["claim_id"], username="old.direct.exclusion",
            reason_code="count_ceiling", reason="超出上限", location_country=None,
            profile=profile)
        self.service.record_result(
            self.owner, self.task_id, self.target, username="old.direct.exclusion",
            instagram_user_id=None, source_mode="followers", visibility="private",
            profile=profile, screening={"routing_result": "excluded_account_count_ceiling"},
            qualified=False, dedupe_claim_id=claim["claim_id"])
        self.service.reconcile_task_mode_candidates(
            self.owner, self.task_id, self.target, "followers")
        self.checkpoint({"saved": 1, "processed": 1}, stage="mode_completed")
        for target in (self.target, self.other):
            self.service.set_target_runtime_status(self.owner, self.task_id, target, "completed")
        self.service.finalize_task_runtime_status(self.owner, self.task_id, "completed")
        with self.database.write() as connection:
            connection.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00Z' WHERE id=?",
                               (self.task_id,))
        self.reopen()
        self.assertEqual(0, self.service.task_mode_candidate_stats(
            self.owner, self.task_id, self.target, "followers")["total"])
        self.assertEqual(0, self.progress()["qualified_for_review"])

    def test_not_started_modes_have_explicit_zero_dedupe(self):
        for target in (self.target, self.other):
            for mode in ("followers", "following"):
                self.assertEqual(0, self.progress(target=target, mode=mode)["skipped_global_duplicates"])

    def test_source_modes_live_history_and_restart_use_same_exact_counts(self):
        self.append(["shared", "new.one", "pending.one", "SHARED", "new.one"])
        self.finish("shared", "deduped")
        self.finish("shared", "recorded")  # Repeated/racing terminal writes are immutable.
        self.finish("new.one", "recorded")
        self.append(["shared", "new.one"])
        self.append(["shared", "new.two"], mode="following")
        self.finish("shared", "deduped", mode="following")
        self.finish("new.two", "deduped", mode="following")
        self.append(["shared"], target=self.other)
        self.finish("shared", "recorded", target=self.other)
        expected = {
            "source_total": None, "discovered": 3, "processed": 2,
            "saved": 1, "skipped_global_duplicates": 1,
            "discarded": 0, "hover_discarded": 0,
            "qualified_for_review": 0,  # Synthetic recorded row never entered review.
        }
        for _ in range(2):
            self.assertEqual(expected, self.progress())
            self.assertEqual(2, self.progress(mode="following")["skipped_global_duplicates"])
            self.assertEqual(0, self.progress(target=self.other)["skipped_global_duplicates"])
            live = self.service.get_task_live_status(self.owner, self.task_id, current_target_ids=[self.target])
            target = next(row for row in live["targets"] if row["id"] == self.target)
            self.assertEqual(expected, target["mode_progress"]["followers"])
            history = next(row for row in self.service.list_history(self.owner, task_id=self.task_id)
                           if row["target_id"] == self.target)
            self.assertEqual(expected, history["mode_progress"]["followers"])
            self.reopen()

    def test_real_global_duplicate_is_counted_at_append_before_profile_read(self):
        self.append(["known.person"])
        claim = self.service.claim_workbench_identity(
            self.owner, username="known.person", source="followers", source_target=self.target,
        )
        self.service.record_result(
            self.owner, self.task_id, self.target, username="known.person", instagram_user_id=None,
            source_mode="followers", visibility="public", profile={"posts": 0},
            screening={"routing_result": "excluded_zero_posts"}, qualified=False,
            dedupe_claim_id=claim["claim_id"],
        )
        self.service.reconcile_task_mode_candidates(self.owner, self.task_id, self.target, "followers")
        for _ in range(2):
            stats = self.append(["known.person"], mode="following")
            self.assertEqual({"total": 1, "pending": 0, "recorded": 0, "deduped": 1},
                             {key: stats[key] for key in ("total", "pending", "recorded", "deduped")})
        self.assertEqual(1, self.progress()["saved"])  # Saved terminal exclusions also count.
        self.assertEqual(0, self.progress()["skipped_global_duplicates"])
        self.assertEqual(1, self.progress(mode="following")["skipped_global_duplicates"])
        self.assertEqual(0, self.progress(mode="following")["saved"])

    def test_live_counters_win_over_inflated_recovery_checkpoint(self):
        self.append(["known", "pending"])
        self.finish("known", "deduped")
        self.checkpoint({"source_total": 241, "discovered": 100, "processed": 90,
                         "saved": 40, "skipped_global_duplicates": 50,
                         "previous_counters": {"processed": 900, "saved": 700,
                                               "skipped_global_duplicates": 200}})
        self.assertEqual({"source_total": 241, "discovered": 2, "processed": 1,
                          "saved": 0, "skipped_global_duplicates": 1,
                          "qualified_for_review": 0, "discarded": 0, "hover_discarded": 0}, self.progress())

    def test_terminal_spool_compaction_retains_checkpoint_dedupe(self):
        self.append(["saved", "seen.one", "seen.two"])
        self.finish("saved", "recorded")
        self.finish("seen.one", "deduped")
        self.finish("seen.two", "deduped")
        expected = {"source_total": 241, "discovered": 3, "processed": 3,
                    "saved": 1, "skipped_global_duplicates": 2,
                    "qualified_for_review": None, "discarded": 0, "hover_discarded": 0}
        self.checkpoint(expected, stage="mode_completed")
        for target in (self.target, self.other):
            self.service.set_target_runtime_status(self.owner, self.task_id, target, "completed")
        self.service.finalize_task_runtime_status(self.owner, self.task_id, "completed")
        with self.database.write() as connection:
            connection.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00Z' WHERE id=?", (self.task_id,))
        self.reopen()
        self.assertEqual(0, self.service.task_mode_candidate_stats(
            self.owner, self.task_id, self.target, "followers")["total"])
        self.assertEqual(expected, self.progress())

    def test_nested_recovery_uses_one_counter_snapshot_not_independent_maxima(self):
        self.checkpoint({"reason": "network_unavailable", "previous_counters": {
            "source_total": 40, "discovered": 20, "processed": 10,
            "saved": 2, "skipped_global_duplicates": 8,
            "previous_counters": {"source_total": 100, "discovered": 99, "processed": 9,
                                  "saved": 9, "skipped_global_duplicates": 0},
        }})
        self.assertEqual({"source_total": 40, "discovered": 20, "processed": 10,
                          "saved": 2, "skipped_global_duplicates": 8,
                          "qualified_for_review": None, "discarded": 0, "hover_discarded": 0}, self.progress())

    def test_explicit_zero_snapshot_does_not_reuse_old_counts(self):
        self.checkpoint({"source_total": 0, "discovered": 0, "processed": 0,
                         "saved": 0, "skipped_global_duplicates": 0,
                         "previous_counters": {"source_total": 99, "discovered": 99,
                                               "processed": 99, "saved": 1,
                                               "skipped_global_duplicates": 98}})
        self.assertEqual({"source_total": 0, "discovered": 0, "processed": 0,
                          "saved": 0, "skipped_global_duplicates": 0,
                          "qualified_for_review": 0, "discarded": 0, "hover_discarded": 0}, self.progress())

    def test_old_checkpoint_same_snapshot_pair_can_recover_missing_counter(self):
        snapshots = [
            {"processed": 8, "saved": 3},
            {"saved": 3, "skipped_global_duplicates": 5},
            {"processed": 8, "skipped_global_duplicates": 5},
        ]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.checkpoint({"previous_counters": snapshot})
                progress = self.progress()
                self.assertEqual((8, 3, 5), tuple(progress[key] for key in
                                 ("processed", "saved", "skipped_global_duplicates")))

    def test_incomplete_old_checkpoint_does_not_guess_dedupe_from_source_total(self):
        self.checkpoint({"source_total": 241, "processed": 47})
        progress = self.progress()
        self.assertEqual(47, progress["processed"])
        self.assertIsNone(progress["saved"])
        self.assertIsNone(progress["skipped_global_duplicates"])

    def test_incomplete_current_layer_does_not_borrow_older_unrelated_counter(self):
        self.checkpoint({"processed": 47, "previous_counters": {
            "processed": 40, "saved": 30, "skipped_global_duplicates": 10}})
        progress = self.progress()
        self.assertEqual(47, progress["processed"])
        self.assertIsNone(progress["saved"])
        self.assertIsNone(progress["skipped_global_duplicates"])

    def test_conflicting_or_invalid_legacy_values_do_not_publish_false_dedupe(self):
        for snapshot in (
            {"processed": 3, "saved": 2, "skipped_global_duplicates": 4},
            {"processed": 3, "saved": 4},
            {"processed": 3, "skipped_global_duplicates": 4},
            {"processed": True, "saved": "2", "skipped_global_duplicates": -1},
        ):
            with self.subTest(snapshot=snapshot):
                self.checkpoint(snapshot)
                self.assertIsNone(self.progress()["skipped_global_duplicates"])


if __name__ == "__main__":
    unittest.main()
