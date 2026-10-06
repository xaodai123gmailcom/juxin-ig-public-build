"""Global collection identity regressions, including stale worker writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.errors import ConflictError
from app.service import CoreService


class GlobalIngestionDedupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "collector.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.owner = self.service.register_user(
            "dedup-regression-owner", "correct horse battery staple"
        )["id"]
        self.task = self._task("source.one")
        self.target_id = self.task["targets"][0]["id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _task(self, source: str) -> dict:
        return self.service.create_task(
            self.owner, name=source, modes=["followers", "following"],
            targets=[source], settings={},
        )

    def _claim(self, username: str, *, target_id: str | None = None, resume: bool = False) -> dict:
        return self.service.claim_workbench_identity(
            self.owner, username=username, source="followers",
            source_target=target_id or self.target_id, allow_owned_resume=resume,
        )

    def _record(self, claim: dict, *, task: dict | None = None, marker: str = "original", qualified=None, mode: str = "followers", use_claim: bool = True) -> dict:
        task = task or self.task
        return self.service.record_result(
            self.owner, task["id"], task["targets"][0]["id"],
            username=claim["username"], instagram_user_id=None,
            source_mode=mode, visibility="private", profile={"marker": marker},
            screening={}, qualified=qualified,
            dedupe_claim_id=claim["claim_id"] if use_claim else None,
        )

    def _candidate(self, claim: dict) -> dict:
        return self.service.create_workbench_candidate(
            self.owner, claim_id=claim["claim_id"], username=claim["username"],
            visibility="private", profile={"marker": "original"},
            screening={}, review_cache={}, source_mode="followers",
            source_target=self.target_id,
        )

    def test_existing_direct_task_source_is_globally_seen(self) -> None:
        another = self._task("source.two")
        claim = self._claim("@SOURCE.ONE", target_id=another["targets"][0]["id"])
        self.assertTrue(claim["duplicate"])
        self.assertFalse(claim["should_collect_profile"])

    def test_late_added_task_source_is_globally_seen(self) -> None:
        self.service.add_targets(self.owner, self.task["id"], ["@Late.Source"])
        claim = self._claim("https://www.instagram.com/late.source/")
        self.assertTrue(claim["duplicate"])
        self.assertFalse(claim["should_collect_profile"])

    def test_foreign_target_cannot_reuse_completed_claim(self) -> None:
        claim = self._claim("already.collected")
        self._record(claim)
        candidate = self._candidate(claim)
        self.service.decide_workbench_candidate(
            self.owner, candidate_id=candidate["id"], decision="approved",
        )
        other = self._task("source.foreign")
        replay = self._record(claim, task=other, marker="stale foreign worker")
        self.assertTrue(replay["deduped"])
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT target_id, profile_json FROM task_results WHERE account_id=?",
                (claim["claim_id"],),
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(self.target_id, rows[0]["target_id"])
        self.assertEqual("original", json.loads(rows[0]["profile_json"])["marker"])

    def test_foreign_mode_cannot_reuse_unfinished_claim(self) -> None:
        claim = self._claim("unfinished.identity")
        with self.assertRaises(ConflictError):
            self._record(claim, mode="following")
        self.assertEqual([], self.service.list_results(self.owner, self.task["id"]))

    def test_reviewed_identity_stale_write_preserves_result_and_decision(self) -> None:
        for decision, use_claim in (("approved", True), ("rejected", True), ("approved", False), ("rejected", False)):
            with self.subTest(decision=decision, use_claim=use_claim):
                claim = self._claim(f"reviewed.{decision}.{use_claim}")
                self._record(claim)
                candidate = self._candidate(claim)
                self.service.decide_workbench_candidate(
                    self.owner, candidate_id=candidate["id"], decision=decision,
                )
                replay = self._record(claim, marker="late page response", qualified=True, use_claim=use_claim)
                self.assertTrue(replay["deduped"])
                with self.database.read() as connection:
                    result = connection.execute(
                        "SELECT profile_json, qualified FROM task_results WHERE account_id=?",
                        (claim["claim_id"],),
                    ).fetchone()
                    review = connection.execute(
                        "SELECT status FROM workbench_candidates WHERE account_id=?",
                        (claim["claim_id"],),
                    ).fetchone()
                self.assertEqual("original", json.loads(result["profile_json"])["marker"])
                self.assertIsNone(result["qualified"])
                self.assertEqual(decision, review["status"])
                self.assertTrue(self._claim(claim["username"], resume=True)["duplicate"])

    def test_result_commit_without_review_recovers_saved_evidence_without_reopen(self) -> None:
        claim = self._claim("crash.gap")
        original = self._record(claim, marker="saved before exit")
        self.assertTrue(self._claim("crash.gap", resume=True)["duplicate"])
        stats = self.service.append_task_mode_candidates(
            self.owner, self.task["id"], self.target_id, "followers", ["crash.gap"],
        )
        self.assertEqual(0, stats["pending"])
        self.assertEqual(1, stats["recorded"])
        with self.database.read() as connection:
            results = connection.execute(
                "SELECT profile_json FROM task_results WHERE account_id=?", (claim["claim_id"],),
            ).fetchall()
            reviews = connection.execute(
                "SELECT profile_json FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],),
            ).fetchall()
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(reviews))
        self.assertEqual(original["profile"], json.loads(results[0]["profile_json"]))
        self.assertEqual(results[0]["profile_json"], reviews[0]["profile_json"])

    def test_parallel_targets_share_one_atomic_global_claim(self) -> None:
        other = self._task("source.parallel")
        target_ids = [self.target_id, other["targets"][0]["id"]]
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(
                lambda target: self._claim("shared.person", target_id=target, resume=True),
                target_ids,
            ))
        self.assertEqual(1, sum(claim["claimed"] for claim in claims))
        self.assertEqual(1, sum(claim["duplicate"] for claim in claims))

    def test_reconcile_restores_review_from_committed_result_without_profile_reread(self) -> None:
        username = "result.without.review"
        self.service.append_task_mode_candidates(
            self.owner, self.task["id"], self.target_id, "followers", [username],
        )
        claim = self._claim(username)
        self._record(claim, marker="captured before crash")
        with self.database.read() as connection:
            before = dict(connection.execute(
                "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],),
            ).fetchone())
        for _ in range(2):
            stats = self.service.reconcile_task_mode_candidates(
                self.owner, self.task["id"], self.target_id, "followers",
            )
            self.assertEqual({"total": 1, "pending": 0, "recorded": 1, "deduped": 0}, stats)
        with self.database.read() as connection:
            after = dict(connection.execute(
                "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],),
            ).fetchone())
            reviews = connection.execute(
                "SELECT * FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],),
            ).fetchall()
        self.assertEqual(before, after)
        self.assertEqual(1, len(reviews))
        self.assertEqual("pending", reviews[0]["status"])
        self.assertEqual("private", reviews[0]["visibility"])
        self.assertEqual(self.target_id, reviews[0]["source_target"])
        self.assertEqual("followers", reviews[0]["source_mode"])
        self.assertEqual(before["profile_json"], reviews[0]["profile_json"])
        expected_screening = json.loads(before["screening_json"])
        expected_screening["review_tier"] = "primary"  # r38 retires the private secondary lane.
        self.assertEqual(expected_screening, json.loads(reviews[0]["screening_json"]))
        self.assertTrue(self._claim(username, resume=True)["duplicate"])

    def test_reconcile_never_restores_review_from_mismatched_legacy_claim(self) -> None:
        another = self._task("foreign.recovery.source")
        for column, mismatched_value in (
            ("source", "following"),
            ("source_target", another["targets"][0]["id"]),
        ):
            with self.subTest(column=column):
                username = f"legacy.wrong.{column}"
                self.service.append_task_mode_candidates(
                    self.owner, self.task["id"], self.target_id, "followers", [username],
                )
                claim = self._claim(username)
                self._record(claim)
                with self.database.write() as connection:
                    connection.execute(
                        f"UPDATE workbench_identity_claims SET {column}=? WHERE account_id=?",
                        (mismatched_value, claim["claim_id"]),
                    )
                self.service.reconcile_task_mode_candidates(
                    self.owner, self.task["id"], self.target_id, "followers",
                )
                with self.database.read() as connection:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM workbench_candidates WHERE account_id=?",
                        (claim["claim_id"],),
                    ).fetchone()[0])
                    self.assertEqual(1, connection.execute(
                        "SELECT COUNT(*) FROM task_results WHERE account_id=?",
                        (claim["claim_id"],),
                    ).fetchone()[0])

    def test_public_crash_recovery_restores_only_eligible_primary_review(self) -> None:
        primary = {
            "routing_result": "public_primary_review", "review_tier": "primary",
            "basic": {"passed": True}, "location": {"passed": True},
            "activity": {"enabled": True, "checked": True, "passed": True},
        }
        cases = (
            ("primary", {}, True),
            ("basic.failed", {"basic": {"passed": False}}, False),
            ("non.us", {"location": {"passed": False, "country": "Canada"}}, False),
            ("inactive", {"activity": {"enabled": True, "checked": True, "passed": False}}, False),
            ("secondary", {"review_tier": "secondary"}, False),
            ("unrouted", {"routing_result": None}, False),
        )
        for label, overrides, should_restore in cases:
            with self.subTest(case=label):
                username = f"public.gap.{label}"
                self.service.append_task_mode_candidates(
                    self.owner, self.task["id"], self.target_id, "followers", [username],
                )
                claim = self._claim(username)
                self.service.record_result(
                    self.owner, self.task["id"], self.target_id,
                    username=username, instagram_user_id=None, source_mode="followers",
                    visibility="public", profile={"posts": 2, "marker": label},
                    screening=primary | overrides, qualified=should_restore,
                    dedupe_claim_id=claim["claim_id"],
                )
                with self.database.read() as connection:
                    before = dict(connection.execute(
                        "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],),
                    ).fetchone())
                self.service.reconcile_task_mode_candidates(
                    self.owner, self.task["id"], self.target_id, "followers",
                )
                with self.database.read() as connection:
                    after = dict(connection.execute(
                        "SELECT * FROM task_results WHERE account_id=?", (claim["claim_id"],),
                    ).fetchone())
                    reviews = connection.execute(
                        "SELECT * FROM workbench_candidates WHERE account_id=?", (claim["claim_id"],),
                    ).fetchall()
                self.assertEqual(before, after)
                self.assertEqual(int(should_restore), len(reviews))
                if should_restore:
                    self.assertEqual("public", reviews[0]["visibility"])
                    self.assertEqual("pending", reviews[0]["status"])
                    self.assertEqual(before["profile_json"], reviews[0]["profile_json"])
                    self.assertEqual(before["screening_json"], reviews[0]["screening_json"])


if __name__ == "__main__":
    unittest.main()
