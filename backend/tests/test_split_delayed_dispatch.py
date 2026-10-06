from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.errors import ConflictError, NotFoundError, ValidationError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.service import CoreService
from support.legacy_split_fixture import create_legacy_task


PASSWORD = "delayed dispatch test password"


class SplitDelayedDispatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "split.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user("split-dispatch", PASSWORD)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _task(self, *, windows: list[str] | None = None) -> dict[str, Any]:
        return self.service.create_task(
            self.user["id"],
            name="live split task",
            modes=["followers"],
            targets=["seed_target"],
            window_ids=windows or ["window-1"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 2}},
            },
        )

    def _queue_manual(self, usernames: list[str]) -> list[dict[str, Any]]:
        rows = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{"username": username} for username in usernames],
        )
        ids = [row["id"] for row in rows if row["username"] in usernames]
        return self.service.mark_split_candidates_queued(self.user["id"], ids)

    def test_enqueue_without_active_task_creates_no_task_target_and_survives_restart(self) -> None:
        before_tasks = self.service.list_tasks_with_details(self.user["id"])
        self._queue_manual(["wait_only"])
        after_tasks = self.service.list_tasks_with_details(self.user["id"])
        self.assertEqual(before_tasks, after_tasks)
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM task_targets").fetchone()[0])
        self.database.initialize()
        row = self.service.list_split_candidates(self.user["id"])[0]
        self.assertEqual("queued", row["queue_state"])
        self.assertIsNone(row["queued_target_id"])

    def test_split_only_task_requires_a_real_backlog(self) -> None:
        with self.assertRaisesRegex(Exception, "No split candidate"):
            self.service.create_task(
                self.user["id"], name="empty", modes=["followers"], targets=[],
                window_ids=["window-empty"],
                settings={"live_queue_enabled": True, "location_enabled": False},
            )
        self._queue_manual(["split_only"])
        task = self.service.create_task(
            self.user["id"], name="split only", modes=["followers"], targets=[],
            window_ids=["window-empty"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.assertEqual([], task["targets"])

    def test_twenty_windows_atomically_claim_five_unique_rows(self) -> None:
        windows = [f"window-{index}" for index in range(20)]
        task = self._task(windows=windows)
        seed = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], seed["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self._queue_manual([f"queued_{index}" for index in range(5)])

        with ThreadPoolExecutor(max_workers=20) as executor:
            claimed = list(
                executor.map(
                    lambda profile: self.service.claim_next_split_candidate(
                        self.user["id"], task["id"], profile
                    ),
                    windows,
                )
            )
        claimed_rows = [row for row in claimed if row is not None]
        self.assertEqual(5, len(claimed_rows))
        self.assertEqual(5, len({row["username"] for row in claimed_rows}))
        stored = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(5, sum(row["status"] == "running" for row in stored["targets"]))
        self.assertEqual(
            5,
            sum(
                row["queue_state"] == "claimed"
                for row in self.service.list_split_candidates(self.user["id"])
            ),
        )

    def test_paused_or_terminal_task_never_claims(self) -> None:
        task = self._task()
        self._queue_manual(["stay_waiting"])
        self.service.set_task_runtime_status(self.user["id"], task["id"], "paused")
        self.assertIsNone(
            self.service.claim_next_split_candidate(
                self.user["id"], task["id"], "window-1"
            )
        )
        candidate = self.service.list_split_candidates(self.user["id"])[0]
        self.assertEqual("queued", candidate["queue_state"])
        self.assertIsNone(candidate["queued_target_id"])

    def test_lifecycle_waiting_requires_no_runtime_target(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "lifecycle_waiting"}]
        )
        available = next(row for row in saved if row["username"] == "lifecycle_waiting")
        self.assertEqual("available", available["queue_state"])
        self.assertEqual("waiting", available["lifecycle_state"])
        self.assertIsNone(available["queued_target_id"])
        self.assertIsNone(available["claimed_at"])

        queued = self.service.mark_split_candidates_queued(
            self.user["id"], [available["id"]]
        )
        waiting = next(row for row in queued if row["id"] == available["id"])
        self.assertEqual("queued", waiting["queue_state"])
        self.assertEqual("waiting", waiting["lifecycle_state"])
        self.assertIsNone(waiting["queued_target_id"])
        duplicate = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "lifecycle_waiting"}],
            include_outcome=True,
        )
        self.assertEqual(0, duplicate["accepted_count"])
        self.assertEqual("waiting", duplicate["duplicates"][0]["disposition"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_targets "
                    "WHERE username_norm='lifecycle_waiting'"
                ).fetchone()[0],
            )

    def test_queued_upsert_promotes_existing_available_generation_atomically(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{"username": "account_page_queue", "queued": False}],
        )
        available = next(
            row for row in saved if row["username"] == "account_page_queue"
        )
        self.assertEqual("available", available["queue_state"])

        promoted = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{"username": "account_page_queue", "queued": True}],
            include_outcome=True,
        )

        self.assertEqual([available["id"]], promoted["accepted_ids"])
        self.assertEqual(1, promoted["accepted_count"])
        self.assertEqual([], promoted["duplicates"])
        current = next(
            row for row in promoted["candidates"] if row["id"] == available["id"]
        )
        self.assertEqual("queued", current["queue_state"])
        self.assertEqual("waiting", current["lifecycle_state"])

        duplicate = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{"username": "account_page_queue", "queued": True}],
            include_outcome=True,
        )
        self.assertEqual(0, duplicate["accepted_count"])
        self.assertEqual("waiting", duplicate["duplicates"][0]["disposition"])

    def test_lifecycle_claim_is_the_only_running_transition(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        queued = self._queue_manual(["lifecycle_running"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "lifecycle_running"
        )

        claimed_target = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        running = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("running", running["lifecycle_state"])
        self.assertEqual("claimed", running["queue_state"])
        self.assertEqual(claimed_target["id"], running["queued_target_id"])
        self.assertEqual("running", running["target_status"])
        self.assertEqual("running", running["task_status"])
        self.assertEqual("window-1", running["source_window_id"])
        self.assertIsNotNone(running["claimed_at"])
        self.assertIsNone(running["completed_at"])
        duplicate = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "lifecycle_running"}],
            include_outcome=True,
        )
        self.assertEqual(0, duplicate["accepted_count"])
        self.assertEqual("running", duplicate["duplicates"][0]["disposition"])

    def test_success_moves_running_generation_to_immutable_completed_history(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        queued = self._queue_manual(["lifecycle_completed"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "lifecycle_completed"
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        running = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        claimed_at = running["claimed_at"]

        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "completed",
            window_id="window-1",
        )
        completed = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("completed", completed["lifecycle_state"])
        self.assertEqual("completed", completed["target_status"])
        self.assertEqual(claimed_at, completed["claimed_at"])
        self.assertIsNotNone(completed["completed_at"])
        self.assertFalse(completed["requires_manual_action"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )

    def test_failed_claim_requires_manual_action_and_never_enters_completed(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self._queue_manual(["lifecycle_failure"])
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "recoverable",
            window_id="window-1",
        )
        rows = self.service.list_split_candidates(self.user["id"])
        failure = next(row for row in rows if row["username"] == "lifecycle_failure")
        self.assertEqual("failure", failure["kind"])
        self.assertTrue(failure["requires_manual_action"])
        self.assertIsNone(failure["lifecycle_state"])
        duplicate = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "lifecycle_failure"}],
            include_outcome=True,
        )
        self.assertEqual(0, duplicate["accepted_count"])
        self.assertEqual("failure", duplicate["duplicates"][0]["disposition"])
        self.assertFalse(
            any(
                row["username"] == "lifecycle_failure"
                and row["lifecycle_state"] == "completed"
                for row in rows
            )
        )
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history "
                    "WHERE username_norm='lifecycle_failure'"
                ).fetchone()[0],
            )

    def test_late_completion_cannot_reverse_failed_split_generation(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self._queue_manual(["late_completed_after_failure"])
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "recoverable",
            window_id="window-1",
        )
        before = [
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["username"] == "late_completed_after_failure"
        ]
        self.assertEqual(["failure"], [row["kind"] for row in before])
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], claimed["id"], "completed",
                window_id="window-1",
            )
        after = [
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["username"] == "late_completed_after_failure"
        ]
        self.assertEqual(["failure"], [row["kind"] for row in after])
        self.assertFalse(any(row["real_lifecycle_state"] == "completed" for row in after))
        self.assertEqual(
            "recoverable",
            next(
                row
                for row in self.service.get_task(self.user["id"], task["id"])["targets"]
                if row["id"] == claimed["id"]
            )["status"],
        )
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history "
                    "WHERE source_target_id=?",
                    (claimed["id"],),
                ).fetchone()[0],
            )

    def test_late_failure_cannot_reverse_completed_split_generation(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        queued = self._queue_manual(["late_failure_after_completed"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "late_failure_after_completed"
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "completed",
            window_id="window-1",
        )
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], claimed["id"], "failed",
                window_id="window-1",
            )
        rows = [
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["username"] == "late_failure_after_completed"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(candidate_id, rows[0]["id"])
        self.assertEqual("completed", rows[0]["real_lifecycle_state"])
        self.assertNotEqual("failure", rows[0]["kind"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_target_recovery_controls "
                    "WHERE target_id=? AND state='pending'",
                    (claimed["id"],),
                ).fetchone()[0],
            )
        with self.database.write() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE task_targets SET status='failed' WHERE id=?",
                    (claimed["id"],),
                )

    def test_running_and_completed_lifecycle_survive_database_restart(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        queued = self._queue_manual(["restart_lifecycle"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "restart_lifecycle"
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.database.initialize()
        running = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("running", running["lifecycle_state"])
        original_claimed_at = running["claimed_at"]

        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "completed",
            window_id="window-1",
        )
        self.database.initialize()
        completed = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("completed", completed["lifecycle_state"])
        self.assertEqual(original_claimed_at, completed["claimed_at"])

    def test_completed_username_readd_is_structured_duplicate_and_preserves_history(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        waiting = self._queue_manual(["same_completed_username"])
        candidate_id = next(
            row["id"] for row in waiting if row["username"] == "same_completed_username"
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "completed",
            window_id="window-1",
        )

        outcome = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "same_completed_username"}],
            include_outcome=True,
        )
        self.assertEqual(0, outcome["accepted_count"])
        self.assertEqual(1, outcome["duplicate_count"])
        self.assertEqual("completed", outcome["duplicates"][0]["disposition"])
        self.assertIn(candidate_id, outcome["duplicates"][0]["candidate_ids"])
        completed = [
            row
            for row in outcome["candidates"]
            if row["username"] == "same_completed_username"
        ]
        self.assertEqual(1, len(completed))
        self.assertEqual(candidate_id, completed[0]["id"])
        self.assertEqual("completed", completed[0]["real_lifecycle_state"])

    def test_unrelated_ordinary_same_username_target_cannot_claim_split_row(self) -> None:
        queued = self._queue_manual(["unrelated_same_username"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "unrelated_same_username"
        )
        ordinary = create_legacy_task(self.service,
            self.user["id"],
            name="ordinary same username",
            modes=["followers"],
            targets=["unrelated_same_username"],
            window_ids=["ordinary-window"],
            settings={
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        target = ordinary["targets"][0]
        self.service.set_task_runtime_status(
            self.user["id"], ordinary["id"], "running"
        )
        self.service.set_target_runtime_status(
            self.user["id"], ordinary["id"], target["id"], "running",
            window_id="ordinary-window",
        )
        still_waiting = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("waiting", still_waiting["lifecycle_state"])
        self.assertIsNone(still_waiting["queued_target_id"])

        self.service.set_target_runtime_status(
            self.user["id"], ordinary["id"], target["id"], "completed",
            window_id="ordinary-window",
        )
        after_completion = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("waiting", after_completion["lifecycle_state"])
        self.assertFalse(
            any(
                row["id"] == candidate_id and row["lifecycle_state"] == "completed"
                for row in self.service.list_split_candidates(self.user["id"])
            )
        )

    def test_manual_category_is_durable_cosmetic_and_idempotent(self) -> None:
        queued = self._queue_manual(["manual_category_waiting"])
        row = next(
            item for item in queued if item["username"] == "manual_category_waiting"
        )
        before_tasks = self.service.list_tasks_with_details(self.user["id"])
        moved = self.service.set_split_candidate_manual_category(
            self.user["id"], [row["id"]], "completed"
        )
        classified = next(item for item in moved if item["id"] == row["id"])
        self.assertEqual("completed", classified["lifecycle_state"])
        self.assertEqual("waiting", classified["real_lifecycle_state"])
        self.assertEqual("completed", classified["manual_category"])
        self.assertIsNotNone(classified["manual_category_at"])
        self.assertEqual("queued", classified["queue_state"])
        self.assertIsNone(classified["queued_target_id"])
        self.assertEqual(before_tasks, self.service.list_tasks_with_details(self.user["id"]))

        category_at = classified["manual_category_at"]
        replay = self.service.set_split_candidate_manual_category(
            self.user["id"], [row["id"]], "completed"
        )
        replayed = next(item for item in replay if item["id"] == row["id"])
        self.assertEqual(category_at, replayed["manual_category_at"])
        self.database.initialize()
        restarted = next(
            item
            for item in self.service.list_split_candidates(self.user["id"])
            if item["id"] == row["id"]
        )
        self.assertEqual("completed", restarted["lifecycle_state"])
        self.assertEqual("ignored", restarted["disposition"])
        self.assertTrue(restarted["ignored_unexecuted"])
        duplicate = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "manual_category_waiting"}],
            include_outcome=True,
        )
        self.assertEqual(0, duplicate["accepted_count"])
        self.assertEqual("ignored", duplicate["duplicates"][0]["disposition"])
        queue_outcome = self.service.mark_split_candidates_queued(
            self.user["id"], [row["id"]], include_outcome=True
        )
        self.assertEqual(0, queue_outcome["accepted_count"])
        self.assertEqual("ignored", queue_outcome["duplicates"][0]["disposition"])
        self.assertFalse(self.service.has_queued_split_candidates(self.user["id"]))
        with self.assertRaisesRegex(ValidationError, "No split candidate"):
            self.service.create_task(
                self.user["id"], name="ignored must not dispatch", modes=["followers"],
                targets=[], window_ids=["ignored-window"],
                settings={"live_queue_enabled": True, "location_enabled": False},
            )

        restored_rows = self.service.set_split_candidate_manual_category(
            self.user["id"], [row["id"]], "waiting"
        )
        restored = next(item for item in restored_rows if item["id"] == row["id"])
        self.assertEqual("waiting", restored["lifecycle_state"])
        self.assertEqual("waiting", restored["real_lifecycle_state"])
        self.assertFalse(restored["ignored_unexecuted"])
        self.assertTrue(self.service.has_queued_split_candidates(self.user["id"]))
        with self.database.read() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log "
                "WHERE entity_id=? AND event_type='split_candidate.category_set' "
                "ORDER BY seq",
                (row["id"],),
            ).fetchall()
        self.assertEqual(3, len(events))
        self.assertIn('"changed":false', events[1]["payload_json"])

    def test_real_claim_completion_and_failure_clear_manual_category(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")

        queued = self._queue_manual(["override_then_complete"])
        first_id = next(
            row["id"] for row in queued if row["username"] == "override_then_complete"
        )
        self.service.set_split_candidate_manual_category(
            self.user["id"], [first_id], "running"
        )
        first_target = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        running = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == first_id
        )
        self.assertEqual("running", running["lifecycle_state"])
        self.assertIsNone(running["manual_category"])

        self.service.set_split_candidate_manual_category(
            self.user["id"], [first_id], "waiting"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], first_target["id"], "completed",
            window_id="window-1",
        )
        completed = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == first_id
        )
        self.assertEqual("completed", completed["lifecycle_state"])
        self.assertEqual("completed", completed["real_lifecycle_state"])
        self.assertIsNone(completed["manual_category"])

        queued = self._queue_manual(["override_then_failure"])
        failure_live_id = next(
            row["id"] for row in queued if row["username"] == "override_then_failure"
        )
        failure_target = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        with self.assertRaises(ConflictError):
            self.service.set_split_candidate_manual_category(
                self.user["id"], [failure_live_id], "completed"
            )
        self.service.set_split_candidate_manual_category(
            self.user["id"], [failure_live_id], "waiting"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], failure_target["id"], "recoverable",
            window_id="window-1",
        )
        failure = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["username"] == "override_then_failure"
        )
        self.assertEqual("failure", failure["kind"])
        self.assertIsNone(failure["manual_category"])
        with self.database.read() as connection:
            live = connection.execute(
                "SELECT manual_category_override, manual_category_at "
                "FROM split_candidates WHERE id=?",
                (failure_live_id,),
            ).fetchone()
        self.assertIsNone(live["manual_category_override"])
        self.assertIsNone(live["manual_category_at"])

    def test_completed_manual_category_can_be_cleared_without_rewriting_history(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        queued = self._queue_manual(["completed_category_clear"])
        candidate_id = next(
            row["id"] for row in queued if row["username"] == "completed_category_clear"
        )
        target = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "completed",
            window_id="window-1",
        )
        original = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        moved = self.service.set_split_candidate_manual_category(
            self.user["id"], [candidate_id], "waiting"
        )
        classified = next(row for row in moved if row["id"] == candidate_id)
        self.assertEqual("waiting", classified["lifecycle_state"])
        self.assertEqual("completed", classified["real_lifecycle_state"])
        self.assertEqual(original["completed_at"], classified["completed_at"])
        queue_outcome = self.service.mark_split_candidates_queued(
            self.user["id"], [candidate_id], include_outcome=True
        )
        self.assertEqual(0, queue_outcome["accepted_count"])
        self.assertEqual("completed", queue_outcome["duplicates"][0]["disposition"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )

        cleared = self.service.set_split_candidate_manual_category(
            self.user["id"], [candidate_id], None
        )
        restored = next(row for row in cleared if row["id"] == candidate_id)
        self.assertEqual("completed", restored["lifecycle_state"])
        self.assertIsNone(restored["manual_category"])
        self.assertEqual(original["completed_at"], restored["completed_at"])

    def test_manual_category_rejects_failure_stale_and_cross_owner_generations_atomically(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable"
        )
        failure = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure"
        )
        with self.assertRaises(ConflictError):
            self.service.set_split_candidate_manual_category(
                self.user["id"], [failure["id"]], "completed"
            )

        queued = self._queue_manual(["atomic_category"])
        candidate = next(row for row in queued if row["username"] == "atomic_category")
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_manual_category(
                self.user["id"], [candidate["id"], "stale-generation-id"], "completed"
            )
        unchanged = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate["id"]
        )
        self.assertIsNone(unchanged["manual_category"])

        other = self.service.register_user("split-category-other", PASSWORD)
        other_rows = self.service.upsert_manual_split_candidates(
            other["id"], [{"username": "other_owner_split"}]
        )
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_manual_category(
                self.user["id"], [other_rows[0]["id"]], "running"
            )

    def test_same_username_manual_category_targets_exact_completed_generation(self) -> None:
        task = self._task()
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        first = self._queue_manual(["category_same_username"])
        first_id = next(
            row["id"] for row in first if row["username"] == "category_same_username"
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "completed",
            window_id="window-1",
        )
        second_id = "legacy-second-completed-generation"
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO split_candidate_history(
                    id, owner_user_id, username_norm, username_display,
                    source_target, source_task_id, source_target_id,
                    source_status, source_window_id, last_error, profile_json,
                    queued_task_id, queued_target_id, queued_at, claimed_at,
                    completed_at, manual_category_override,
                    manual_category_at, created_at, updated_at
                )
                SELECT ?, owner_user_id, username_norm, username_display,
                       source_target, source_task_id, source_target_id,
                       source_status, source_window_id, last_error, profile_json,
                       queued_task_id, queued_target_id, queued_at, claimed_at,
                       completed_at, NULL, NULL, created_at, updated_at
                FROM split_candidate_history WHERE id=?
                """,
                (second_id, first_id),
            )
        moved = self.service.set_split_candidate_manual_category(
            self.user["id"], [first_id], "running"
        )
        generations = {
            row["id"]: row
            for row in moved
            if row["username"] == "category_same_username"
        }
        self.assertEqual("running", generations[first_id]["lifecycle_state"])
        self.assertEqual("completed", generations[first_id]["real_lifecycle_state"])
        self.assertEqual("completed", generations[second_id]["lifecycle_state"])
        self.assertIsNone(generations[second_id]["manual_category"])

    def test_failure_requeue_is_idempotent_and_resumes_same_target_checkpoint(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.upsert_checkpoint(
            self.user["id"],
            task["id"],
            target["id"],
            mode="followers",
            stage="screening_accounts",
            cursor={"candidate_spool_complete": True},
            counters={"source_total": 100, "saved": 10},
            recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable",
            window_id="window-1",
        )
        failure = self.service.list_split_candidates(self.user["id"])[0]
        first = self.service.requeue_split_candidate(self.user["id"], failure["id"])
        replay = self.service.requeue_split_candidate(self.user["id"], failure["id"])
        self.assertEqual("manual", first["kind"])
        self.assertEqual("queued", first["queue_state"])
        self.assertEqual(first["id"], replay["id"])
        self.assertIsNone(first["queued_target_id"])

        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(target["id"], claimed["id"])
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual(100, checkpoint["counters"]["source_total"])
        self.assertEqual(10, checkpoint["counters"]["saved"])

    def test_failure_requeue_window_pool_is_returned_and_survives_target_retry(self) -> None:
        task = self._task(windows=["window-a", "window-b", "window-c"])
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable"
        )
        failure = self.service.list_split_candidates(self.user["id"])[0]

        queued = self.service.requeue_split_candidate(
            self.user["id"],
            failure["id"],
            allowed_window_ids=["window-b", "window-c"],
        )
        self.assertEqual(["window-b", "window-c"], queued["allowed_window_ids"])
        replay = self.service.requeue_split_candidate(self.user["id"], failure["id"])
        self.assertEqual(["window-b", "window-c"], replay["allowed_window_ids"])

        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self.assertIsNone(
            self.service.claim_next_split_candidate(
                self.user["id"], task["id"], "window-a"
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-b"
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(["window-b", "window-c"], claimed["allowed_window_ids"])

        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable",
            window_id="window-b",
        )
        retried = self.service.retry_task_target(
            self.user["id"], task["id"], target["id"]
        )
        self.assertEqual(["window-b", "window-c"], retried["allowed_window_ids"])
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], target["id"], "running",
                window_id="window-a",
            )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "running",
            window_id="window-c",
        )

    def test_failure_requeue_is_affine_to_source_task_and_preserves_checkpoint(self) -> None:
        source_task = self._task(windows=["source-window"])
        source_target = source_task["targets"][0]
        self.service.upsert_checkpoint(
            self.user["id"], source_task["id"], source_target["id"],
            mode="followers", stage="screening_accounts", cursor={},
            counters={"source_total": 100, "saved": 10}, recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], source_target["id"], "recoverable"
        )
        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])

        other_task = self.service.create_task(
            self.user["id"], name="other live task", modes=["followers"],
            targets=["other_seed"], window_ids=["other-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.service.set_target_runtime_status(
            self.user["id"], other_task["id"], other_task["targets"][0]["id"],
            "completed",
        )
        self.service.set_task_runtime_status(
            self.user["id"], other_task["id"], "running"
        )
        self.service.set_task_runtime_status(
            self.user["id"], source_task["id"], "running"
        )
        self.assertIsNone(
            self.service.claim_next_split_candidate(
                self.user["id"], other_task["id"], "other-window"
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], source_task["id"], "source-window"
        )
        self.assertEqual(source_target["id"], claimed["id"])
        self.assertEqual(
            10,
            self.service.get_checkpoint(
                self.user["id"], source_task["id"], source_target["id"], "followers"
            )["counters"]["saved"],
        )

    def test_returned_target_from_windowless_source_can_be_claimed_by_new_task(self) -> None:
        source_task = self._task(windows=["source-window"])
        source_target = source_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], source_target["id"],
            "recoverable", window_id="source-window",
        )
        self.service.delete_task_target(
            self.user["id"], source_task["id"], source_target["id"]
        )
        returned = self.service.requeue_removed_task_target(
            self.user["id"], source_target["id"],
            removed_profile_id="source-window",
        )
        self.service.remove_task_windows(
            self.user["id"], source_task["id"], ["source-window"]
        )

        receiver = self.service.create_task(
            self.user["id"], name="receiver task", modes=["followers"],
            targets=["receiver_seed"], window_ids=["receiver-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.service.set_target_runtime_status(
            self.user["id"], receiver["id"], receiver["targets"][0]["id"],
            "completed",
        )
        self.service.set_task_runtime_status(
            self.user["id"], receiver["id"], "running"
        )

        self.assertEqual("queued", returned["queue_state"])
        self.assertTrue(
            self.service.has_claimable_split_candidates(
                self.user["id"], receiver["id"]
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], receiver["id"], "receiver-window"
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(source_target["username"], claimed["username"])
        self.assertNotEqual(source_target["id"], claimed["id"])
        self.assertEqual("receiver-window", claimed["current_window_id"])
        self.assertEqual(
            "stopped",
            self.service.get_task(
                self.user["id"], source_task["id"]
            )["targets"][0]["status"],
        )

    def test_returned_target_from_dismissed_source_can_be_claimed_by_new_task(self) -> None:
        source_task = self._task(windows=["source-window"])
        source_target = source_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], source_target["id"],
            "recoverable", window_id="source-window",
        )
        self.service.delete_task_target(
            self.user["id"], source_task["id"], source_target["id"]
        )
        self.service.requeue_removed_task_target(
            self.user["id"], source_target["id"]
        )
        self.service.dismiss_task_from_list(self.user["id"], source_task["id"])

        receiver = self.service.create_task(
            self.user["id"], name="dismiss receiver", modes=["followers"],
            targets=["dismiss_receiver_seed"],
            window_ids=["dismiss-receiver-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.service.set_target_runtime_status(
            self.user["id"], receiver["id"], receiver["targets"][0]["id"],
            "completed",
        )
        self.service.set_task_runtime_status(
            self.user["id"], receiver["id"], "running"
        )

        self.assertTrue(
            self.service.has_claimable_split_candidates(
                self.user["id"], receiver["id"]
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], receiver["id"], "dismiss-receiver-window"
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(source_target["username"], claimed["username"])
        self.assertNotEqual(source_target["id"], claimed["id"])

    def test_returned_target_stays_pinned_while_visible_source_has_a_window(self) -> None:
        source_task = self._task(windows=["source-window"])
        source_target = source_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], source_target["id"],
            "recoverable", window_id="source-window",
        )
        self.service.delete_task_target(
            self.user["id"], source_task["id"], source_target["id"]
        )
        self.service.requeue_removed_task_target(
            self.user["id"], source_target["id"]
        )

        receiver = self.service.create_task(
            self.user["id"], name="pinned receiver", modes=["followers"],
            targets=["pinned_receiver_seed"],
            window_ids=["pinned-receiver-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.service.set_target_runtime_status(
            self.user["id"], receiver["id"], receiver["targets"][0]["id"],
            "completed",
        )
        self.service.set_task_runtime_status(
            self.user["id"], receiver["id"], "running"
        )
        self.service.set_task_runtime_status(
            self.user["id"], source_task["id"], "running"
        )

        self.assertFalse(
            self.service.has_claimable_split_candidates(
                self.user["id"], receiver["id"]
            )
        )
        self.assertIsNone(
            self.service.claim_next_split_candidate(
                self.user["id"], receiver["id"], "pinned-receiver-window"
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], source_task["id"], "source-window"
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(source_target["id"], claimed["id"])

    def test_delete_and_return_drops_removed_only_window_affinity_to_automatic(self) -> None:
        source_task = self._task(windows=["source-window"])
        source_seed = source_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], source_seed["id"], "completed"
        )
        self.service.set_task_runtime_status(
            self.user["id"], source_task["id"], "running"
        )
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"], [{
                "username": "only_removed_window",
                "queued": True,
                "allowed_window_ids": ["source-window"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, saved["accepted_count"])
        claimed_source = self.service.claim_next_split_candidate(
            self.user["id"], source_task["id"], "source-window"
        )
        self.assertIsNotNone(claimed_source)
        assert claimed_source is not None
        self.service.set_target_runtime_status(
            self.user["id"], source_task["id"], claimed_source["id"],
            "recoverable", window_id="source-window",
        )
        self.service.delete_task_target(
            self.user["id"], source_task["id"], claimed_source["id"]
        )
        returned = self.service.requeue_removed_task_target(
            self.user["id"], claimed_source["id"],
            removed_profile_id="source-window",
        )
        self.service.remove_task_windows(
            self.user["id"], source_task["id"], ["source-window"]
        )

        self.assertEqual([], returned["allowed_window_ids"])
        self.assertEqual("automatic", returned["window_assignment_mode"])
        receiver = self.service.create_task(
            self.user["id"], name="automatic receiver", modes=["followers"],
            targets=["automatic_receiver_seed"],
            window_ids=["automatic-receiver-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.service.set_target_runtime_status(
            self.user["id"], receiver["id"], receiver["targets"][0]["id"],
            "completed",
        )
        self.service.set_task_runtime_status(
            self.user["id"], receiver["id"], "running"
        )
        claimed_receiver = self.service.claim_next_split_candidate(
            self.user["id"], receiver["id"], "automatic-receiver-window"
        )
        self.assertIsNotNone(claimed_receiver)
        assert claimed_receiver is not None
        self.assertEqual("only_removed_window", claimed_receiver["username"])
        self.assertEqual([], claimed_receiver["allowed_window_ids"])

    def test_v025_linked_failure_retry_is_upgraded_without_deadlock(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable"
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_targets SET status='pending' WHERE id=?", (target["id"],)
            )
            connection.execute(
                """
                INSERT INTO split_candidates(
                    id, owner_user_id, username_norm, username_display,
                    candidate_kind, source_target, source_task_id,
                    source_target_id, source_status, profile_json, queue_state,
                    queued_task_id, queued_target_id, queued_at, created_at, updated_at
                ) VALUES(
                    'legacy-linked-failure', ?, ?, ?, 'failure', ?, ?, ?,
                    'recoverable', '{}', 'queued', ?, ?, datetime('now'),
                    datetime('now'), datetime('now')
                )
                """,
                (
                    self.user["id"], target["username"].lower(), target["username"],
                    target["username"], task["id"], target["id"], task["id"], target["id"],
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=9")
        self.database.initialize()
        candidate = self.service.list_split_candidates(self.user["id"])[0]
        self.assertEqual("manual", candidate["kind"])
        self.assertEqual(target["id"], candidate["queued_target_id"])
        upgraded_target = self.service.get_task(
            self.user["id"], task["id"]
        )["targets"][0]
        self.assertFalse(upgraded_target["manual_recovery_required"])

    def test_v025_false_same_username_claim_returns_to_waiting_not_history(self) -> None:
        task = self._task()
        ordinary = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], ordinary["id"], "completed",
            window_id="window-1",
        )
        fake_id = "legacy-false-claimed-same-name"
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO split_candidates(
                    id, owner_user_id, username_norm, username_display,
                    candidate_kind, source_target, source_task_id,
                    source_target_id, source_status, profile_json, queue_state,
                    queued_task_id, queued_target_id, queued_at, claimed_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'manual', NULL, NULL, NULL, NULL, '{}',
                         'claimed', ?, ?, datetime('now'), NULL,
                         datetime('now'), datetime('now'))
                """,
                (
                    fake_id,
                    self.user["id"],
                    ordinary["username"].lower(),
                    ordinary["username"],
                    task["id"],
                    ordinary["id"],
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=10")

        self.database.initialize()
        candidate = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == fake_id
        )
        self.assertEqual("waiting", candidate["real_lifecycle_state"])
        self.assertEqual("queued", candidate["queue_state"])
        self.assertIsNone(candidate["queued_task_id"])
        self.assertIsNone(candidate["queued_target_id"])
        self.assertIsNone(candidate["claimed_at"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history WHERE id=?",
                    (fake_id,),
                ).fetchone()[0],
            )

    def test_v025_exact_claimed_generation_migrates_to_completed_history(self) -> None:
        task = self._task()
        ordinary = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], ordinary["id"], "completed",
            window_id="window-1",
        )
        candidate_id = "legacy-exact-completed-generation"
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO split_candidates(
                    id, owner_user_id, username_norm, username_display,
                    candidate_kind, source_target, source_task_id,
                    source_target_id, source_status, profile_json, queue_state,
                    queued_task_id, queued_target_id, queued_at, claimed_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'manual', ?, ?, ?, 'claimed_generation',
                         '{}', 'claimed', ?, ?, datetime('now'), datetime('now'),
                         datetime('now'), datetime('now'))
                """,
                (
                    candidate_id,
                    self.user["id"],
                    ordinary["username"].lower(),
                    ordinary["username"],
                    ordinary["username"],
                    task["id"],
                    ordinary["id"],
                    task["id"],
                    ordinary["id"],
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=10")

        self.database.initialize()
        migrated = next(
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["id"] == candidate_id
        )
        self.assertEqual("completed", migrated["real_lifecycle_state"])
        self.assertEqual(ordinary["id"], migrated["source_target_id"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )

    def test_v025_exact_claimed_completed_with_pending_recovery_stays_failure(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "completed",
            window_id="window-1",
        )
        candidate_id = "legacy-exact-stale"
        failure_id = "legacy-failure"
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO split_candidates(
                    id, owner_user_id, username_norm, username_display,
                    candidate_kind, source_target, source_task_id,
                    source_target_id, source_status, profile_json, queue_state,
                    queued_task_id, queued_target_id, queued_at, claimed_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'manual', ?, ?, ?, 'claimed_generation',
                         '{}', 'claimed', ?, ?, datetime('now'), datetime('now'),
                         datetime('now'), datetime('now'))
                """,
                (
                    candidate_id,
                    self.user["id"],
                    target["username"].lower(),
                    target["username"],
                    target["username"],
                    task["id"],
                    target["id"],
                    task["id"],
                    target["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO task_target_recovery_controls(
                    target_id, owner_user_id, candidate_id, username_norm,
                    username_display, state, source_task_id, source_status,
                    source_window_id, last_error, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?, 'recoverable',
                         'window-1', 'legacy contradictory recovery', datetime('now'))
                """,
                (
                    target["id"],
                    self.user["id"],
                    failure_id,
                    target["username"].lower(),
                    target["username"],
                    task["id"],
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=10")

        self.database.initialize()
        visible = [
            row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["username"] == target["username"]
        ]
        self.assertEqual(1, len(visible))
        self.assertEqual(failure_id, visible[0]["id"])
        self.assertEqual("failure", visible[0]["kind"])
        self.assertTrue(visible[0]["requires_manual_action"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM split_candidate_history WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0],
            )
            live = connection.execute(
                "SELECT queue_state FROM split_candidates WHERE id=?",
                (candidate_id,),
            ).fetchone()
            self.assertIsNotNone(live)
            self.assertEqual("claimed", live["queue_state"])

    def test_delete_failure_is_idempotent_and_preserves_source_checkpoint(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], target["id"],
            mode="followers", stage="discovering_accounts", cursor={},
            counters={"saved": 3}, recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "failed"
        )
        failure = self.service.list_split_candidates(self.user["id"])[0]
        old_failure_id = failure["id"]
        self.assertTrue(
            self.service.delete_failed_split_candidate(
                self.user["id"], failure["id"]
            )["deleted"]
        )
        replay = self.service.delete_failed_split_candidate(
            self.user["id"], failure["id"]
        )
        self.assertTrue(replay["already_absent"])
        self.assertIsNotNone(
            self.service.get_checkpoint(
                self.user["id"], task["id"], target["id"], "followers"
            )
        )
        # Re-finalizing/restarting the same old task cannot resurrect a dismissed
        # inbox row, and the target stays behind the durable manual gate.
        self.service.set_task_runtime_status(
            self.user["id"], task["id"], "stopped", error="same old generation"
        )
        self.assertEqual([], self.service.list_split_candidates(self.user["id"]))
        gated = self.service.get_task(self.user["id"], task["id"])["targets"][0]
        self.assertTrue(gated["manual_recovery_required"])

        # An independently stored pre-r55 generation may still surface a new
        # failure. Fresh same-name API admission is separately forbidden.
        new_task = create_legacy_task(self.service,
            self.user["id"], name="new generation", modes=["followers"],
            targets=[target["username"]], window_ids=["new-window"], settings={},
        )
        new_target = new_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], new_task["id"], new_target["id"], "failed"
        )
        resurfaced = self.service.list_split_candidates(self.user["id"])
        self.assertEqual(1, len(resurfaced))
        self.assertEqual(new_target["id"], resurfaced[0]["source_target_id"])
        self.assertNotEqual(old_failure_id, resurfaced[0]["id"])
        # Delayed/replayed HTTP actions for the old generation cannot touch the
        # new failure, while the old source target remains durably dismissed.
        self.assertTrue(
            self.service.delete_failed_split_candidate(
                self.user["id"], old_failure_id
            )["already_absent"]
        )
        with self.assertRaises(NotFoundError):
            self.service.requeue_split_candidate(self.user["id"], old_failure_id)
        self.assertEqual(
            resurfaced[0]["id"],
            self.service.list_split_candidates(self.user["id"])[0]["id"],
        )
        self.assertTrue(
            self.service.get_task(self.user["id"], task["id"])["targets"][0][
                "manual_recovery_required"
            ]
        )

    def test_same_username_running_target_does_not_hide_failure_generation(self) -> None:
        failed_task = self._task(windows=["failed-window"])
        failed_target = failed_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], failed_task["id"], failed_target["id"], "failed"
        )
        failure_id = self.service.list_split_candidates(self.user["id"])[0]["id"]

        running_task = create_legacy_task(self.service,
            self.user["id"], name="same username running", modes=["followers"],
            targets=[failed_target["username"]], window_ids=["running-window"],
            settings={"location_enabled": False},
        )
        running_target = running_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], running_task["id"], running_target["id"], "running",
            window_id="running-window",
        )
        failures = self.service.list_split_candidates(self.user["id"])
        self.assertEqual([failure_id], [row["id"] for row in failures if row["kind"] == "failure"])

    def test_same_target_fails_again_with_new_action_generation(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable"
        )
        first_failure_id = self.service.list_split_candidates(self.user["id"])[0]["id"]
        self.service.requeue_split_candidate(self.user["id"], first_failure_id)
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "failed"
        )
        second = self.service.list_split_candidates(self.user["id"])
        self.assertEqual(1, len(second))
        self.assertNotEqual(first_failure_id, second[0]["id"])
        self.assertTrue(
            self.service.delete_failed_split_candidate(
                self.user["id"], first_failure_id
            )["already_absent"]
        )
        self.assertEqual(second[0]["id"], self.service.list_split_candidates(self.user["id"])[0]["id"])
        with self.assertRaises(NotFoundError):
            self.service.requeue_split_candidate(self.user["id"], first_failure_id)
        self.service.requeue_split_candidate(self.user["id"], second[0]["id"])
        reclaimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.assertEqual(target["id"], reclaimed["id"])

    def test_two_failed_generations_for_same_username_are_independently_visible(self) -> None:
        first_task = self._task(windows=["first-window"])
        username = first_task["targets"][0]["username"]
        second_task = create_legacy_task(self.service,
            self.user["id"], name="second generation", modes=["followers"],
            targets=[username], window_ids=["second-window"], settings={},
        )
        for task in (first_task, second_task):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], task["targets"][0]["id"], "failed"
            )
        failures = [
            row for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure"
        ]
        self.assertEqual(2, len(failures))
        self.assertEqual(2, len({row["id"] for row in failures}))
        self.service.delete_failed_split_candidate(self.user["id"], failures[0]["id"])
        remaining = [
            row for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure"
        ]
        self.assertEqual([failures[1]["id"]], [row["id"] for row in remaining])

    def test_requeued_same_username_generation_stays_visible_until_claimed(self) -> None:
        first_task = self._task(windows=["same-first-window"])
        username = first_task["targets"][0]["username"]
        second_task = create_legacy_task(self.service,
            self.user["id"], name="same pending sibling", modes=["followers"],
            targets=[username], window_ids=["same-second-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        for task in (first_task, second_task):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], task["targets"][0]["id"], "failed"
            )
        failures = {
            row["source_target_id"]: row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure"
        }
        first_target = first_task["targets"][0]
        second_target = second_task["targets"][0]
        self.service.requeue_split_candidate(
            self.user["id"], failures[first_target["id"]]["id"]
        )
        waiting = self.service.list_split_candidates(self.user["id"])
        manual = next(row for row in waiting if row["kind"] == "manual")
        remaining_failure = next(row for row in waiting if row["kind"] == "failure")
        self.assertEqual("queued", manual["queue_state"])
        self.assertEqual(first_target["id"], manual["source_target_id"])
        self.assertIsNone(manual["queued_target_id"])
        self.assertEqual(second_target["id"], remaining_failure["source_target_id"])

    def test_fresh_manual_claim_rebinds_generation_for_later_failure_recovery(self) -> None:
        task = self._task()
        seed = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], seed["id"], "completed"
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self._queue_manual(["fresh_split_generation"])
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.assertEqual("fresh_split_generation", claimed["username"])
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], claimed["id"],
            mode="followers", stage="screening_accounts", cursor={},
            counters={"source_total": 100, "saved": 7}, recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], claimed["id"], "recoverable"
        )
        failure = next(
            row for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure" and row["source_target_id"] == claimed["id"]
        )
        queued = self.service.requeue_split_candidate(
            self.user["id"], failure["id"]
        )
        self.assertEqual("queued", queued["queue_state"])
        reclaimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-1"
        )
        self.assertEqual(claimed["id"], reclaimed["id"])
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], claimed["id"], "followers"
        )
        self.assertEqual(7, checkpoint["counters"]["saved"])

    def test_dismissing_old_same_username_failure_releases_manual_marker(self) -> None:
        first_task = self._task(windows=["first-window"])
        first_target = first_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], first_task["id"], first_target["id"], "recoverable"
        )
        first_failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], first_failure["id"])
        self.service.set_task_runtime_status(
            self.user["id"], first_task["id"], "running"
        )
        self.service.claim_next_split_candidate(
            self.user["id"], first_task["id"], "first-window"
        )
        self.service.set_target_runtime_status(
            self.user["id"], first_task["id"], first_target["id"], "failed"
        )

        second_task = create_legacy_task(self.service,
            self.user["id"], name="same-name newer failure", modes=["followers"],
            targets=[first_target["username"]], window_ids=["second-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        second_target = second_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], second_task["id"], second_target["id"], "failed"
        )
        failures = {
            row["source_target_id"]: row
            for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure"
        }
        self.assertEqual({first_target["id"], second_target["id"]}, set(failures))
        self.service.delete_failed_split_candidate(
            self.user["id"], failures[first_target["id"]]["id"]
        )
        queued = self.service.requeue_split_candidate(
            self.user["id"], failures[second_target["id"]]["id"]
        )
        self.assertEqual("queued", queued["queue_state"])
        self.assertEqual(second_target["id"], queued["source_target_id"])

    def test_dismiss_after_restart_releases_gated_pending_manual_marker(self) -> None:
        first_task = self._task(windows=["restart-first-window"])
        first_target = first_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], first_task["id"], first_target["id"], "recoverable"
        )
        first_failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], first_failure["id"])
        # A second failure requires a real claim. A terminal parent callback
        # alone must preserve the acknowledged, still-unclaimed waiting row.
        self.service.set_task_runtime_status(self.user["id"], first_task["id"], "running")
        self.assertIsNotNone(self.service.claim_next_split_candidate(
            self.user["id"], first_task["id"], "restart-first-window"))
        self.service.set_target_runtime_status(
            self.user["id"], first_task["id"], first_target["id"], "recoverable",
            window_id="restart-first-window")
        self.service.set_task_runtime_status(
            self.user["id"], first_task["id"], "stopped"
        )
        self.service.control_task(self.user["id"], first_task["id"], "restart")
        new_failure = next(
            row for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure" and row["source_target_id"] == first_target["id"]
        )
        self.service.delete_failed_split_candidate(
            self.user["id"], new_failure["id"]
        )

        second_task = create_legacy_task(self.service,
            self.user["id"], name="post-dismiss same name", modes=["followers"],
            targets=[first_target["username"]], window_ids=["restart-second-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        second_target = second_task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], second_task["id"], second_target["id"], "failed"
        )
        second_failure = next(
            row for row in self.service.list_split_candidates(self.user["id"])
            if row["kind"] == "failure" and row["source_target_id"] == second_target["id"]
        )
        queued = self.service.requeue_split_candidate(
            self.user["id"], second_failure["id"]
        )
        self.assertEqual(second_target["id"], queued["source_target_id"])
        self.assertTrue(
            self.service.get_task(self.user["id"], first_task["id"])["targets"][0][
                "manual_recovery_required"
            ]
        )

    def test_live_network_wait_is_not_failure_but_core_interruption_is(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], target["id"],
            mode="followers", stage="waiting_network",
            cursor={"resume_stage": "discovering_accounts", "resume_cursor": {}},
            counters={
                "reason": "instagram_login_required",
                "wait_kind": "profile_intervention",
                "previous_counters": {"source_total": 100, "saved": 10},
            },
            recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "waiting_network",
            window_id="window-1",
        )
        self.assertEqual([], self.service.list_split_candidates(self.user["id"]))

        self.service.recover_interrupted_operations()
        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.assertEqual("failure", failure["kind"])
        self.assertEqual("available", failure["queue_state"])
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], target["id"], "followers"
        )
        self.assertEqual("profile_intervention", checkpoint["counters"]["wait_kind"])
        self.assertEqual("instagram_login_required", checkpoint["counters"]["reason"])

    def test_task_detail_mode_progress_is_source_scoped_and_durable(self) -> None:
        task = self._task()
        target = task["targets"][0]
        self.service.append_task_mode_candidates(
            self.user["id"], task["id"], target["id"], "followers",
            ["candidate_a", "candidate_b", "candidate_c"],
        )
        self.service.finish_task_mode_candidate(
            self.user["id"], task["id"], target["id"], "followers",
            "candidate_a", state="recorded",
        )
        self.service.finish_task_mode_candidate(
            self.user["id"], task["id"], target["id"], "followers",
            "candidate_b", state="deduped",
        )
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], target["id"],
            mode="followers", stage="screening_accounts", cursor={},
            counters={
                "source_total": 100,
                "discovered": 3,
                "processed": 2,
                "saved": 1,
            },
            recoverable=True,
        )
        progress = self.service.get_task(
            self.user["id"], task["id"]
        )["targets"][0]["mode_progress"]["followers"]
        self.assertEqual(
            {"source_total": 100, "discovered": 3, "processed": 2, "saved": 1,
             "skipped_global_duplicates": 1, "qualified_for_review": 0,
             "discarded": 0, "hover_discarded": 0},
            progress,
        )
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], target["id"],
            mode="followers", stage="waiting_network",
            cursor={"resume_stage": "screening_accounts", "resume_cursor": {}},
            counters={
                "reason": "network_unavailable",
                "wait_kind": "network",
                "previous_counters": {
                    "source_total": 100,
                    "discovered": 3,
                    "processed": 2,
                    "saved": 1,
                },
            },
            recoverable=True,
        )
        waiting_progress = self.service.get_task(
            self.user["id"], task["id"]
        )["targets"][0]["mode_progress"]["followers"]
        self.assertEqual(progress, waiting_progress)
        history_progress = self.service.list_history(
            self.user["id"], task_id=task["id"]
        )[0]["mode_progress"]["followers"]
        self.assertEqual(progress, history_progress)

    def test_split_window_pool_is_durable_editable_and_hard_at_claim(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "window_affinity",
                "queued": True,
                "allowed_window_ids": ["window-b", "window-c", "window-b"],
            }],
            include_outcome=True,
        )
        candidate_id = saved["accepted_ids"][0]
        candidate = saved["candidates"][0]
        self.assertEqual(["window-b", "window-c"], candidate["allowed_window_ids"])
        self.assertEqual("specified", candidate["window_assignment_mode"])

        changed = self.service.set_split_candidate_allowed_windows(
            self.user["id"], candidate_id, ["window-c", "window-b"]
        )
        self.assertEqual(["window-c", "window-b"], changed["allowed_window_ids"])
        self.database.initialize()
        restarted = self.service.list_split_candidates(self.user["id"])[0]
        self.assertEqual(["window-c", "window-b"], restarted["allowed_window_ids"])

        task = self._task(windows=["window-a", "window-b", "window-c"])
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self.assertIsNone(
            self.service.claim_next_split_candidate(
                self.user["id"], task["id"], "window-a"
            )
        )
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-b"
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual("window-b", claimed["current_window_id"])
        self.assertEqual(["window-c", "window-b"], claimed["allowed_window_ids"])
        with self.assertRaises(ConflictError):
            self.service.set_split_candidate_allowed_windows(
                self.user["id"], candidate_id, []
            )

    def test_matching_window_prefers_constrained_target_before_automatic_backlog(self) -> None:
        automatic = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "automatic_first", "queued": True}],
            include_outcome=True,
        )
        constrained = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "constrained_second",
                "queued": True,
                "allowed_window_ids": ["window-b"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, automatic["accepted_count"])
        self.assertEqual(1, constrained["accepted_count"])
        task = self._task(windows=["window-a", "window-b"])
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")

        first = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-b"
        )
        second = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-a"
        )
        self.assertEqual("constrained_second", first["username"] if first else None)
        self.assertEqual("automatic_first", second["username"] if second else None)

    def test_empty_split_task_requires_affinity_intersection_but_auto_still_works(self) -> None:
        specified = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "only_window_b",
                "queued": True,
                "allowed_window_ids": ["window-b"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, specified["accepted_count"])
        with self.assertRaisesRegex(ValidationError, "selected windows"):
            self.service.create_task(
                self.user["id"], name="unreachable", modes=["followers"],
                targets=[], window_ids=["window-a"],
                settings={"live_queue_enabled": True, "location_enabled": False},
            )

        automatic = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "automatic_reachable", "queued": True}],
            include_outcome=True,
        )
        self.assertEqual(1, automatic["accepted_count"])
        task = self.service.create_task(
            self.user["id"], name="reachable", modes=["followers"],
            targets=[], window_ids=["window-a"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        self.assertEqual([], task["targets"])

    def test_empty_window_pool_restores_automatic_and_cross_owner_cannot_edit(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "clear_affinity",
                "queued": True,
                "allowed_window_ids": ["window-only"],
            }],
            include_outcome=True,
        )
        candidate_id = saved["accepted_ids"][0]
        other = self.service.register_user("split-other", PASSWORD)
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_allowed_windows(
                other["id"], candidate_id, []
            )
        cleared = self.service.set_split_candidate_allowed_windows(
            self.user["id"], candidate_id, []
        )
        self.assertEqual([], cleared["allowed_window_ids"])
        self.assertEqual("automatic", cleared["window_assignment_mode"])
        task = self._task(windows=["window-any"])
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        claimed = self.service.claim_next_split_candidate(
            self.user["id"], task["id"], "window-any"
        )
        self.assertEqual("clear_affinity", claimed["username"] if claimed else None)

    def test_legacy_queued_upsert_omission_preserves_existing_window_pool(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "legacy_affinity_preserved",
                "allowed_window_ids": ["window-b", "window-c"],
            }],
            include_outcome=True,
        )
        self.assertEqual("available", saved["candidates"][0]["queue_state"])

        promoted = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{"username": "legacy_affinity_preserved", "queued": True}],
            include_outcome=True,
        )
        self.assertEqual(1, promoted["accepted_count"])
        self.assertEqual(
            ["window-b", "window-c"],
            promoted["candidates"][0]["allowed_window_ids"],
        )

        cleared_seed = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "explicit_auto_assignment",
                "allowed_window_ids": ["window-only"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, cleared_seed["accepted_count"])
        cleared = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "explicit_auto_assignment",
                "queued": True,
                "allowed_window_ids": [],
            }],
            include_outcome=True,
        )
        self.assertEqual([], cleared["candidates"][0]["allowed_window_ids"])


class _RuntimeBrowser:
    def close_profile(self, _profile_id):
        return {"closed": True}


class _ImmediateWorker:
    def __init__(self, _bitbrowser: object) -> None:
        self.profile_id = ""

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id

    async def disconnect(self) -> None:
        return None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        return CollectionOutcome("followers", [f"{target}_fan"][:limit], source_total=1)

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict[str, Any]:
        del include_activity
        return {
            "username": target,
            "visibility": "public",
            "followers": 1,
            "following": 1,
            "posts": 1,
        }

    async def read_visible_account_location(self, target: str) -> None:
        del target
        return None


class _MixedPriorityWorker(_ImmediateWorker):
    automatic_started: asyncio.Event | None = None
    release_automatic: asyncio.Event | None = None
    calls: list[tuple[str, str]] = []

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).calls.append((self.profile_id, target))
        if target.startswith("automatic_"):
            assert type(self).automatic_started is not None
            assert type(self).release_automatic is not None
            type(self).automatic_started.set()
            await type(self).release_automatic.wait()
        return await super().collect_followers(target, limit=limit)


class SplitDelayedDispatchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "runtime.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user("split-runtime", PASSWORD)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_db_claim_completion_never_calls_async_queue_task_done(self) -> None:
        initial_started, release_initial = asyncio.Event(), asyncio.Event()

        class DelayedArrivalWorker(_ImmediateWorker):
            async def collect_followers(self, target, *, limit):
                if target == "initial_source":
                    initial_started.set()
                    await release_initial.wait()
                return await super().collect_followers(target, limit=limit)

        class Browser:
            def close_profile(self, _profile_id):
                return {"closed": True}

        task = self.service.create_task(
            self.user["id"], name="split runtime", modes=["followers"],
            targets=["initial_source"], window_ids=["runtime-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, Browser(), worker_factory=DelayedArrivalWorker,
            lease_heartbeat_interval_seconds=60,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(initial_started.wait(), timeout=2)
        control = manager._runs[task["id"]]
        queue_completions = []
        original_task_done = control.target_queue.task_done

        def counted_task_done():
            queue_completions.append(True)
            original_task_done()

        control.target_queue.task_done = counted_task_done

        rows = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "delayed_source"}]
        )
        self.service.mark_split_candidates_queued(
            self.user["id"], [rows[0]["id"]]
        )
        manager.notify_split_queue(self.user["id"])
        # The source arrives while a real worker still owns its initial target;
        # an already empty window would have closed under the drain lifecycle.
        release_initial.set()
        for _ in range(200):
            stored = self.service.get_task(self.user["id"], task["id"])
            delayed = next(
                (row for row in stored["targets"] if row["username"] == "delayed_source"),
                None,
            )
            if delayed and delayed["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("Delayed split candidate was not completed")

        await asyncio.wait_for(manager.wait(task["id"]), timeout=2)
        self.assertEqual(1, len(queue_completions), "only the in-memory initial target completes the async queue")
        self.assertTrue(all(row["status"] == "completed" for row in stored["targets"]))
        self.assertEqual("completed", self.service.get_task(self.user["id"], task["id"])["status"])
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_all_manual_failure_targets_do_not_start_or_lease_windows(self) -> None:
        task = self.service.create_task(
            self.user["id"], name="gated runtime", modes=["followers"],
            targets=["failed_source"], window_ids=["gated-window"],
            settings={"live_queue_enabled": True, "location_enabled": False},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target["id"], "recoverable"
        )
        manager = ExecutionManager(
            self.service, _RuntimeBrowser(), worker_factory=_ImmediateWorker,
            lease_heartbeat_interval_seconds=60,
        )
        with self.assertRaises(ValidationError):
            await manager.start(self.user["id"], task["id"])
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM browser_operation_leases "
                    "WHERE profile_id='gated-window'"
                ).fetchone()[0],
            )

    async def test_split_only_task_starts_workers_then_materializes_target(self) -> None:
        rows = self.service.upsert_manual_split_candidates(
            self.user["id"], [{"username": "only_queued_source"}]
        )
        self.service.mark_split_candidates_queued(self.user["id"], [rows[0]["id"]])
        task = self.service.create_task(
            self.user["id"], name="split only runtime", modes=["followers"],
            targets=[], window_ids=["split-only-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, _RuntimeBrowser(), worker_factory=_ImmediateWorker,
            lease_heartbeat_interval_seconds=60,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(200):
            stored = self.service.get_task(self.user["id"], task["id"])
            if stored["targets"] and stored["targets"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("Split-only task did not claim its durable backlog")
        self.assertEqual("only_queued_source", stored["targets"][0]["username"])
        await manager.stop(self.user["id"], task["id"])

    async def test_nonmatching_worker_cannot_consume_matching_workers_wakeup(self) -> None:
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "window_b_only",
                "queued": True,
                "allowed_window_ids": ["window-b"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, saved["accepted_count"])
        task = self.service.create_task(
            self.user["id"], name="affinity runtime", modes=["followers"],
            targets=[], window_ids=["window-a", "window-b"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, _RuntimeBrowser(), worker_factory=_ImmediateWorker,
            lease_heartbeat_interval_seconds=60,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(200):
            stored = self.service.get_task(self.user["id"], task["id"])
            target = next(
                (item for item in stored["targets"] if item["username"] == "window_b_only"),
                None,
            )
            if target and target["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("The matching window slept after a sibling rejected the target")
        self.assertEqual("window-b", target["current_window_id"])
        await manager.stop(self.user["id"], task["id"])

    async def test_dynamic_matching_window_claims_already_waiting_target(self) -> None:
        initial_started, release_initial = asyncio.Event(), asyncio.Event()

        class ActiveSourceWorker(_ImmediateWorker):
            async def collect_followers(self, target, *, limit):
                if target == "initial_source":
                    initial_started.set()
                    await release_initial.wait()
                return await super().collect_followers(target, limit=limit)

        task = self.service.create_task(
            self.user["id"], name="dynamic affinity", modes=["followers"],
            targets=["initial_source"], window_ids=["window-a"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, _RuntimeBrowser(), worker_factory=ActiveSourceWorker,
            lease_heartbeat_interval_seconds=60,
        )
        await manager.start(self.user["id"], task["id"])
        # Keep a real source active. An empty window now closes automatically;
        # a missing close adapter must not be what keeps this task alive.
        await asyncio.wait_for(initial_started.wait(), timeout=2)

        self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "waits_for_added_window",
                "queued": True,
                "allowed_window_ids": ["window-b"],
            }],
            include_outcome=True,
        )
        manager.notify_split_queue(self.user["id"])
        await asyncio.sleep(0.05)
        self.assertFalse(any(
            target["username"] == "waits_for_added_window"
            for target in self.service.get_task(self.user["id"], task["id"])["targets"]
        ))

        await manager.add_windows(self.user["id"], task["id"], ["window-b"])
        for _ in range(200):
            stored = self.service.get_task(self.user["id"], task["id"])
            target = next(
                (item for item in stored["targets"] if item["username"] == "waits_for_added_window"),
                None,
            )
            if target and target["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("A dynamically added matching window did not inspect the durable queue")
        self.assertEqual("window-b", target["current_window_id"])
        release_initial.set()
        await manager.stop(self.user["id"], task["id"])

    async def test_durable_specified_target_precedes_shared_automatic_backlog(self) -> None:
        _MixedPriorityWorker.automatic_started = asyncio.Event()
        _MixedPriorityWorker.release_automatic = asyncio.Event()
        _MixedPriorityWorker.calls = []
        task = self.service.create_task(
            self.user["id"], name="mixed source priority", modes=["followers"],
            targets=["automatic_one", "automatic_two", "automatic_three"],
            window_ids=["window-a", "window-b"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "window_b_priority",
                "queued": True,
                "allowed_window_ids": ["window-b"],
            }],
            include_outcome=True,
        )
        manager = ExecutionManager(
            self.service, _RuntimeBrowser(), worker_factory=_MixedPriorityWorker,
            lease_heartbeat_interval_seconds=60,
        )
        try:
            await manager.start(self.user["id"], task["id"])
            assert _MixedPriorityWorker.automatic_started is not None
            await asyncio.wait_for(
                _MixedPriorityWorker.automatic_started.wait(), timeout=2
            )
            for _ in range(200):
                stored = self.service.get_task(self.user["id"], task["id"])
                specified = next(
                    (
                        item
                        for item in stored["targets"]
                        if item["username"] == "window_b_priority"
                    ),
                    None,
                )
                if specified and specified["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("The specified split target starved behind automatic work")
            self.assertEqual("window-b", specified["current_window_id"])
            self.assertEqual(
                ("window-b", "window_b_priority"),
                next(
                    call
                    for call in _MixedPriorityWorker.calls
                    if call[1] == "window_b_priority"
                ),
            )
        finally:
            assert _MixedPriorityWorker.release_automatic is not None
            _MixedPriorityWorker.release_automatic.set()
            await manager.stop(self.user["id"], task["id"])


if __name__ == "__main__":
    unittest.main()
