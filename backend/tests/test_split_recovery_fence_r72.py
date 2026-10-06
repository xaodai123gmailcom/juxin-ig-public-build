"""A returned failure and its old task target cannot execute on two windows."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.service import CoreService


class SplitRecoveryFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "fence.sqlite")
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user(
            "split-fence", "correct horse battery staple"
        )["id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _returned(self, *, live: bool, allowed: list[str] | None = None):
        task = self.service.create_task(
            self.owner,
            name="recover same target",
            modes=["followers"],
            targets=["same_source"],
            window_ids=["window-a", "window-b"],
            settings={"live_queue_enabled": live, "location_enabled": False},
        )
        target = task["targets"][0]
        self.service.upsert_checkpoint(
            self.owner, task["id"], target["id"], mode="followers",
            stage="screening_accounts", cursor={"offset": 7},
            counters={"source_total": 8, "saved": 2}, recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable",
            window_id="window-a",
        )
        failure = next(row for row in self.service.list_split_candidates(self.owner)
                       if row["kind"] == "failure")
        waiting = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=allowed
        )
        return task, target, waiting

    def test_live_returned_failure_rejects_old_retry_and_direct_dispatch(self) -> None:
        task, target, waiting = self._returned(live=True)
        self.assertTrue(self.service.get_task(self.owner, task["id"])["targets"][0]["manual_recovery_required"])
        listed = next(item for item in self.service.list_tasks_with_details(self.owner)
                      if item["id"] == task["id"])
        self.assertTrue(listed["targets"][0]["manual_recovery_required"])
        with self.assertRaises(ConflictError) as error:
            self.service.retry_task_target(self.owner, task["id"], target["id"])
        self.assertEqual("split_candidate_waiting", error.exception.details["reason"])
        with self.assertRaises(ConflictError) as error:
            self.service.set_target_runtime_status(
                self.owner, task["id"], target["id"], "running", window_id="window-a"
            )
        self.assertEqual("split_candidate_waiting", error.exception.details["reason"])
        self.assertEqual("recoverable", self.service.get_task(self.owner, task["id"])["targets"][0]["status"])
        current = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual("queued", current["queue_state"])
        self.assertIsNone(current["queued_target_id"])

    def test_two_windows_one_durable_claim_and_loser_cannot_rewrite_owner(self) -> None:
        task, target, waiting = self._returned(live=True)
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service.claim_next_split_candidate,
                                   self.owner, task["id"], window)
                       for window in ("window-a", "window-b")]
            claims = [future.result() for future in futures]
        self.assertEqual(1, sum(claim is not None for claim in claims))
        claimed = next(claim for claim in claims if claim is not None)
        winner = claimed["current_window_id"]
        loser = "window-b" if winner == "window-a" else "window-a"
        self.assertEqual(target["id"], claimed["id"])
        for status in ("running", "waiting_network", "recoverable", "completed"):
            with self.subTest(status=status), self.assertRaises(ConflictError) as error:
                self.service.set_target_runtime_status(
                    self.owner, task["id"], target["id"], status, window_id=loser
                )
            self.assertEqual("target_owned_by_other_window", error.exception.details["reason"])
        actual = self.service.get_task(self.owner, task["id"])["targets"][0]
        self.assertEqual("running", actual["status"])
        self.assertEqual(winner, actual["current_window_id"])
        self.assertEqual(2, self.service.get_checkpoint(
            self.owner, task["id"], target["id"], "followers"
        )["counters"]["saved"])
        current = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual("claimed", current["queue_state"])
        self.assertEqual(target["id"], current["queued_target_id"])

    def test_nonlive_continue_atomically_transfers_waiting_row_to_old_target(self) -> None:
        task, target, waiting = self._returned(live=False, allowed=["window-b"])
        self.assertFalse(self.service.get_task(self.owner, task["id"])["targets"][0]["manual_recovery_required"])
        resumed = self.service.retry_task_target(self.owner, task["id"], target["id"])
        self.assertEqual(["window-b"], resumed["allowed_window_ids"])
        current = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual("claimed", current["queue_state"])
        self.assertEqual(target["id"], current["queued_target_id"])
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(
                self.owner, task["id"], target["id"], "running", window_id="window-a"
            )
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "running", window_id="window-b"
        )
        self.assertEqual(2, self.service.get_checkpoint(
            self.owner, task["id"], target["id"], "followers"
        )["counters"]["saved"])

    def test_nonlive_locked_waiting_target_cannot_bypass_claim_lock(self) -> None:
        task, target, waiting = self._returned(live=False)
        self.service.set_split_candidate_locked(self.owner, waiting["id"], True)
        with self.assertRaises(ConflictError) as error:
            self.service.retry_task_target(self.owner, task["id"], target["id"])
        self.assertIn(error.exception.details["reason"], {"split_candidate_waiting", "collection_dispatch_locked"})
        self.assertEqual("queued", self.service.list_split_candidates(
            self.owner, candidate_ids=[waiting["id"]]
        )[0]["queue_state"])

    def test_old_target_cannot_retry_after_other_task_claimed_dismissed_source(self) -> None:
        source, old_target, waiting = self._returned(live=True)
        self.service.set_task_runtime_status(self.owner, source["id"], "failed")
        self.service.dismiss_task_from_list(self.owner, source["id"])
        receiver = self.service.create_task(
            self.owner, name="new receiver", modes=["followers"],
            targets=["other_source"], window_ids=["window-c"],
            settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(self.owner, receiver["id"], "running")
        claimed = self.service.claim_next_split_candidate(
            self.owner, receiver["id"], "window-c"
        )
        self.assertIsNotNone(claimed)
        self.assertNotEqual(old_target["id"], claimed["id"])
        with self.assertRaises(ConflictError) as error:
            self.service.retry_task_target(self.owner, source["id"], old_target["id"])
        self.assertEqual("split_target_already_owned", error.exception.details["reason"])
        current = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual(claimed["id"], current["queued_target_id"])


class SplitRecoveryResumeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_resume_uses_waiting_claim_without_direct_target_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "resume.sqlite")
            db.initialize()
            service = CoreService(db)
            owner = service.register_user("split-resume", "correct horse battery staple")["id"]
            task = service.create_task(
                owner, name="resume", modes=["followers"], targets=["recover_me"],
                window_ids=["window-a"], settings={"live_queue_enabled": True},
            )
            target = task["targets"][0]
            service.set_target_runtime_status(owner, task["id"], target["id"], "recoverable",
                                              window_id="window-a")
            failure = next(row for row in service.list_split_candidates(owner) if row["kind"] == "failure")
            waiting = service.requeue_split_candidate(owner, failure["id"])
            manager = ExecutionManager(service, object())
            with patch.object(manager, "start", AsyncMock(return_value=task)) as start:
                result = await manager.resume_window(owner, task["id"], "window-a")
            self.assertEqual("running", result["status"])
            start.assert_awaited_once_with(owner, task["id"], profile_ids=["window-a"])
            self.assertEqual("recoverable", service.get_task(owner, task["id"])["targets"][0]["status"])
            current = service.list_split_candidates(owner, candidate_ids=[waiting["id"]])[0]
            self.assertEqual("queued", current["queue_state"])
            self.assertIsNone(current["queued_target_id"])


if __name__ == "__main__":
    unittest.main()
