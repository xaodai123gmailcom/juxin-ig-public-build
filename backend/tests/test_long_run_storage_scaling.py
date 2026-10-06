from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.service import CoreService


PASSWORD = "long run storage scaling password"


class _TracingDatabase(Database):
    """Capture only statements relevant to accidental hot-path table scans."""

    def __init__(self, path: Path) -> None:
        self.statements: list[str] = []
        super().__init__(path)

    def _connect(self):  # type: ignore[no-untyped-def]
        connection = super()._connect()

        def capture(statement: str) -> None:
            normalized = " ".join(statement.casefold().split())
            if (
                "task_mode_candidates" in normalized
                or "task_mode_candidate_counters" in normalized
                or "review_cache_json" in normalized
                or "workbench_cache_usage" in normalized
            ):
                self.statements.append(normalized)

        connection.set_trace_callback(capture)
        return connection


class LongRunStorageScalingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = _TracingDatabase(Path(self.temp_dir.name) / "long-run.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.user = self.service.register_user("long-run-storage", PASSWORD)
        self.task = self.service.create_task(
            self.user["id"],
            name="24 hour equivalent spool",
            modes=["followers"],
            targets=["large_source"],
            settings={"location_enabled": False},
        )
        self.target_id = self.task["targets"][0]["id"]
        self.database.statements.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _private_candidate(self, index: int, avatar_payload: str) -> dict[str, object]:
        username = f"private_cache_{index:04d}"
        claim = self.service.claim_workbench_identity(
            self.user["id"],
            username=username,
            source="followers",
            source_target=self.target_id,
        )
        self.assertFalse(claim["duplicate"])
        return self.service.create_workbench_candidate(
            self.user["id"],
            claim_id=claim["claim_id"],
            username=username,
            visibility="private",
            profile={"username": username, "visibility": "private"},
            screening={"review_tier": "secondary"},
            review_cache={"avatar_preview": avatar_payload},
            source_mode="followers",
            source_target=self.target_id,
        )

    def test_24_hour_equivalent_flushes_never_scan_the_accumulated_spool(self) -> None:
        # 288 flushes represents one durable batch every five minutes for a full
        # unattended day.  Ten unique rows per flush keeps this regression fast
        # while still growing the source spool to thousands of entries.
        for batch_index in range(288):
            batch = [
                f"longrun_{batch_index:03d}_{offset:02d}"
                for offset in range(10)
            ]
            result = self.service.append_task_mode_candidates(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
                batch,
            )

        self.assertEqual(2880, result["total"])
        self.assertEqual(2880, result["pending"])
        hot_statements = list(self.database.statements)

        # A startup/recovery reconciliation may scan the complete pending spool;
        # append must only reconcile its at-most-100 new usernames and read the
        # constant-size materialized counter.
        aggregate_scans = [
            statement
            for statement in hot_statements
            if "from task_mode_candidates" in statement
            and ("count(" in statement or "max(discovery_order" in statement)
        ]
        self.assertEqual([], aggregate_scans)
        reconciliation_updates = [
            statement
            for statement in hot_statements
            if "update task_mode_candidates as candidate" in statement
        ]
        self.assertTrue(reconciliation_updates)
        self.assertTrue(
            all("candidate.username_norm in (" in item for item in reconciliation_updates)
        )
        self.assertTrue(
            any("from task_mode_candidate_counters" in item for item in hot_statements)
        )

    def test_review_cache_budget_and_snapshot_use_materialized_usage(self) -> None:
        avatar = "data:image/jpeg;base64," + ("a" * 2048)
        candidates = [self._private_candidate(index, avatar) for index in range(32)]
        self.database.statements.clear()

        latest = self._private_candidate(99, avatar)
        candidate_statements = list(self.database.statements)
        cache_sum_scans = [
            statement
            for statement in candidate_statements
            if "sum(" in statement
            and "review_cache_json" in statement
            and "from workbench_candidates" in statement
        ]
        self.assertEqual([], cache_sum_scans)
        self.assertTrue(
            any("from workbench_cache_usage" in item for item in candidate_statements)
        )

        self.service.decide_workbench_candidate(
            self.user["id"],
            candidate_id=str(latest["id"]),
            decision="approved",
        )
        snapshot = self.service.get_workbench_snapshot(
            self.user["id"], limit=1, history_limit=1
        )
        expected_pending_entries = len(candidates)
        self.assertEqual(
            expected_pending_entries,
            snapshot["storage"]["pending_cache_entries"],
        )
        self.assertGreater(snapshot["storage"]["pending_preview_bytes"], 0)
        self.assertGreater(snapshot["storage"]["cache_bytes"], 0)
        self.assertGreater(snapshot["storage"]["disk_total_bytes"], 0)
        self.assertIn("low_space_warning", snapshot["storage"])

        cleared = self.service.clear_workbench_cache(self.user["id"])
        self.assertEqual(1, cleared["cleared_entries"])
        after = self.service.get_workbench_snapshot(
            self.user["id"], limit=1, history_limit=1
        )
        self.assertEqual(0, after["storage"]["cache_bytes"])
        self.assertEqual(
            expected_pending_entries,
            after["storage"]["pending_cache_entries"],
        )

    def test_startup_rebuild_repairs_materialized_counters(self) -> None:
        self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["repair_one", "repair_two"],
        )
        self._private_candidate(1, "data:image/jpeg;base64,cmVwYWly")
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE task_mode_candidate_counters
                SET total=999, pending=999, next_discovery_order=1000
                WHERE target_id=? AND mode='followers'
                """,
                (self.target_id,),
            )
            connection.execute(
                """
                UPDATE workbench_cache_usage
                SET pending_entries=999, pending_bytes=999
                WHERE owner_user_id=?
                """,
                (self.user["id"],),
            )

        self.database.initialize()
        self.assertEqual(
            {"total": 2, "pending": 2, "recorded": 0, "deduped": 0},
            self.service.task_mode_candidate_stats(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
            ),
        )
        snapshot = self.service.get_workbench_snapshot(
            self.user["id"], limit=1, history_limit=1
        )
        self.assertEqual(1, snapshot["storage"]["pending_cache_entries"])

    def test_migration_16_state_updates_and_cascade_keep_counter_exact(self) -> None:
        self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["state_one", "state_two", "state_three"],
        )
        self.service.finish_task_mode_candidate(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            "state_one",
            state="recorded",
        )
        self.service.finish_task_mode_candidate(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            "state_two",
            state="deduped",
        )
        self.assertEqual(
            {"total": 3, "pending": 1, "recorded": 1, "deduped": 1},
            self.service.task_mode_candidate_stats(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
            ),
        )
        with self.database.read() as connection:
            versions = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
        self.assertEqual(set(range(1, 42)), versions)

        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM task_targets WHERE id=?", (self.target_id,)
            )
        with self.database.read() as connection:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM task_mode_candidate_counters
                    WHERE target_id=? AND mode='followers'
                    """,
                    (self.target_id,),
                ).fetchone()
            )

    def test_thirty_day_cleanup_preserves_incomplete_resumable_spool(self) -> None:
        self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["resume_one", "resume_two", "resume_three"],
        )
        self.service.upsert_checkpoint(
            self.user["id"],
            self.task["id"],
            self.target_id,
            mode="followers",
            stage="discovering_accounts",
            cursor={
                "candidate_spool_version": 1,
                "candidate_spool_complete": False,
                "candidate_spool_natural_end": False,
                "resume_tail": ["resume_two", "resume_three"],
            },
            counters={"discovered": 3, "processed": 0, "saved": 0},
            recoverable=True,
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tasks SET status='stopped', updated_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", self.task["id"]),
            )

        self.database.initialize()

        self.assertEqual(
            {"total": 3, "pending": 3, "recorded": 0, "deduped": 0},
            self.service.task_mode_candidate_stats(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
            ),
        )

        # A stale completed checkpoint is not evidence that pending profiles
        # have been screened, even if the source list reached its physical tail.
        self.service.upsert_checkpoint(
            self.user["id"],
            self.task["id"],
            self.target_id,
            mode="followers",
            stage="mode_completed",
            cursor={
                "candidate_spool_version": 1,
                "candidate_spool_complete": True,
                "candidate_spool_natural_end": True,
            },
            counters={"discovered": 3, "processed": 3, "saved": 3},
            recoverable=True,
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tasks SET status='stopped', updated_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", self.task["id"]),
            )

        self.database.initialize()

        self.assertEqual(
            {"total": 3, "pending": 3, "recorded": 0, "deduped": 0},
            self.service.task_mode_candidate_stats(
                self.user["id"], self.task["id"], self.target_id, "followers",
            ),
        )
        for username in ("resume_one", "resume_two", "resume_three"):
            self.service.finish_task_mode_candidate(
                self.user["id"], self.task["id"], self.target_id, "followers",
                username, state="deduped",
            )
        self.database.initialize()

        self.assertEqual(
            {"total": 0, "pending": 0, "recorded": 0, "deduped": 0},
            self.service.task_mode_candidate_stats(
                self.user["id"],
                self.task["id"],
                self.target_id,
                "followers",
            ),
        )

if __name__ == "__main__":
    unittest.main()
