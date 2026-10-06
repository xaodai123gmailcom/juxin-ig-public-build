"""Persistent collection intake locks, atomic claims, and uninterrupted workers."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import Database
from app.errors import ConflictError, NotFoundError, ValidationError
from app.execution_manager import ExecutionManager
from app.service import CoreService
from test_core import FakeBitBrowserClient, FakeCollectionWorker
from support.legacy_split_fixture import create_legacy_task


class SplitLockServiceR43Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "locks.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("split-lock-r43", "long enough test password")["id"]
        self.other = self.service.register_user("split-lock-other", "long enough test password")["id"]
        self.task = self.service.create_task(self.owner, name="intake lock", modes=["followers"], targets=["seed"],
            window_ids=["window-a", "window-b"], settings={"live_queue_enabled": True})
        self.service.set_task_runtime_status(self.owner, self.task["id"], "running")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, windows=None):
        rows = self.service.upsert_manual_split_candidates(self.owner, [{"username": name, "queued": True, "allowed_window_ids": windows or []}])
        return next(row for row in rows if row["username"] == name)

    def claim(self, window="window-a"):
        return self.service.claim_next_split_candidate(self.owner, self.task["id"], window)

    def test_individual_lock_skips_first_and_honors_designated_windows(self):
        first = self.add("locked_first", ["window-a"])
        second = self.add("open_second", ["window-b"])
        self.assertTrue(self.service.set_split_candidate_locked(self.owner, first["id"], True)["locked"])
        self.assertIsNone(self.claim())
        self.assertEqual("open_second", self.claim("window-b")["username"])
        self.service.set_split_candidate_locked(self.owner, first["id"], False)
        self.assertEqual("locked_first", self.claim()["username"])
        with self.assertRaises(ConflictError):
            self.service.set_split_candidate_locked(self.owner, second["id"], True)

    def test_owner_gate_covers_new_and_returned_rows_without_changing_per_row_lock(self):
        item = self.add("returned_source")
        target = self.claim()
        self.service.set_split_claim_locked(self.owner, True)
        self.service.set_target_runtime_status(self.owner, self.task["id"], target["id"], "stopped", window_id="window-a")
        self.service.delete_task_target(self.owner, self.task["id"], target["id"])
        returned = self.service.requeue_removed_task_target(self.owner, target["id"])
        new = self.add("new_while_locked")
        self.service.set_split_candidate_locked(self.owner, new["id"], True)
        self.assertIsNone(self.claim())
        self.assertFalse(self.service.has_claimable_split_candidates(self.owner, self.task["id"]))
        self.service.set_split_claim_locked(self.owner, False)
        self.assertEqual(target["id"], self.claim()["id"])
        self.assertIsNone(self.claim())
        rows = self.service.list_split_candidates(self.owner)
        self.assertTrue(next(row for row in rows if row["id"] == new["id"])["locked"])

    def test_current_target_and_network_recovery_continue_but_next_claim_is_blocked(self):
        first = self.task["targets"][0]
        self.service.set_target_runtime_status(self.owner, self.task["id"], first["id"], "running", window_id="window-a")
        self.service.set_split_claim_locked(self.owner, True)
        self.service.set_target_runtime_status(self.owner, self.task["id"], first["id"], "waiting_network", window_id="window-a")
        self.service.set_target_runtime_status(self.owner, self.task["id"], first["id"], "running", window_id="window-a")
        self.service.set_target_runtime_status(self.owner, self.task["id"], first["id"], "completed", window_id="window-a")
        second = self.service.add_targets(self.owner, self.task["id"], ["new_direct"])["targets"][-1]
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(self.owner, self.task["id"], second["id"], "running", window_id="window-a")

    def test_locked_username_cannot_bypass_via_create_add_or_direct_claim(self):
        row = self.add("locked_direct")
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        with self.assertRaises(ConflictError):
            self.service.create_task(self.owner, name="manual bypass", modes=["followers"],
                targets=["locked_direct"], window_ids=["window-c"], settings={}, assignment_mode="manual")
        with self.assertRaises(ConflictError):
            self.service.add_targets(self.owner, self.task["id"], ["locked_direct"])
        unchanged = next(item for item in self.service.list_split_candidates(self.owner) if item["id"] == row["id"])
        self.assertTrue(unchanged["locked"])
        self.assertIsNone(unchanged["queued_task_id"])
        self.assertIsNone(unchanged["queued_target_id"])
        self.assertEqual(1, len(self.service.get_task(self.owner, self.task["id"])["targets"]))
        # An inherited old-version task row can already reference the same name;
        # the final claim and explicit retry still enforce the lock atomically.
        with self.database.write() as connection:
            connection.execute("UPDATE task_targets SET username_norm='locked_direct',username_display='locked_direct' WHERE id=?", (self.task["targets"][0]["id"],))
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(self.owner, self.task["id"], self.task["targets"][0]["id"], "running", window_id="window-a")
        with self.assertRaises(ConflictError):
            self.service.retry_task_target(self.owner, self.task["id"], self.task["targets"][0]["id"])
        self.service.set_split_candidate_locked(self.owner, row["id"], False)
        self.assertEqual("locked_direct", self.claim()["username"])

    def test_owner_isolation_and_missing_rows_do_not_report_lock_success(self):
        row = self.add("isolated")
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_locked(self.other, row["id"], True)
        with self.assertRaises(NotFoundError):
            self.service.set_split_candidate_locked(self.owner, "absent", False)
        self.service.set_split_claim_locked(self.other, True)
        self.assertFalse(self.service.get_split_claim_locked(self.owner))
        self.assertEqual("isolated", self.claim()["username"])

    def test_strict_boolean_service_arguments_do_not_silently_unlock(self):
        row = self.add("bool_guard")
        self.service.set_split_claim_locked(self.owner, True)
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        for value in (None, "false", 0, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.service.set_split_claim_locked(self.owner, value)
                with self.assertRaises(ValidationError):
                    self.service.set_split_candidate_locked(self.owner, row["id"], value)
        self.assertTrue(self.service.get_split_claim_locked(self.owner))

    def test_restart_persists_locks_and_affinity_edits_do_not_unlock(self):
        row = self.add("persisted", ["window-a"])
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        self.service.set_split_claim_locked(self.owner, True)
        edited = self.service.set_split_candidate_allowed_windows(self.owner, row["id"], ["window-b"])
        self.assertTrue(edited["locked"])
        self.database.initialize()
        self.database.initialize()
        self.service = CoreService(self.database)
        self.assertTrue(self.service.get_split_claim_locked(self.owner))
        refreshed = next(item for item in self.service.list_split_candidates(self.owner) if item["id"] == row["id"])
        self.assertTrue(refreshed["locked"])
        self.assertEqual(["window-b"], refreshed["allowed_window_ids"])
        self.assertIsNone(self.claim("window-b"))

    def test_existing_database_adds_lock_column_without_losing_waiting_rows(self):
        row = self.add("legacy_waiting")
        with self.database.write() as connection:
            connection.execute("ALTER TABLE split_candidates DROP COLUMN dispatch_locked")
            connection.execute("DROP TABLE collection_dispatch_locks")
        self.database.initialize()
        restored = next(item for item in self.service.list_split_candidates(self.owner) if item["id"] == row["id"])
        self.assertFalse(restored["locked"])
        self.assertFalse(self.service.get_split_claim_locked(self.owner))
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        self.database.initialize()
        self.assertTrue(next(item for item in self.service.list_split_candidates(self.owner) if item["id"] == row["id"])["locked"])

    def test_lock_and_claim_race_has_one_atomic_winner(self):
        for iteration in range(8):
            row = self.add(f"race_{iteration}")
            barrier = threading.Barrier(2)
            def lock():
                barrier.wait()
                try:
                    return self.service.set_split_candidate_locked(self.owner, row["id"], True)
                except ConflictError:
                    return None
            def claim():
                barrier.wait()
                return self.claim()
            with ThreadPoolExecutor(max_workers=2) as executor:
                lock_future = executor.submit(lock)
                claim_future = executor.submit(claim)
                locked, claimed = lock_future.result(), claim_future.result()
            if locked is None:
                self.assertIsNotNone(claimed)
                self.assertEqual(row["username"], claimed["username"])
            else:
                self.assertTrue(locked["locked"])
                self.assertIsNone(claimed)
                self.service.delete_waiting_split_candidate(self.owner, row["id"])

    def test_locked_waiting_target_can_be_deleted_without_touching_global_gate(self):
        row = self.add("delete_locked")
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        self.service.set_split_claim_locked(self.owner, True)
        self.service.delete_waiting_split_candidate(self.owner, row["id"])
        self.assertFalse(any(item["id"] == row["id"] for item in self.service.list_split_candidates(self.owner)))
        self.assertTrue(self.service.get_split_claim_locked(self.owner))


class SplitLockRuntimeR43Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "runtime.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("lock-runtime", "long enough test password")["id"]
        self.manager = None
        self.release = asyncio.Event()

    async def asyncTearDown(self):
        self.release.set()
        if self.manager:
            await self.manager.shutdown()
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.005)

    def make_task(self, targets, *, assignment="sequential"):
        return self.service.create_task(self.owner, name="runtime locked intake", modes=["followers"], targets=targets,
            window_ids=["window-a"], assignment_mode=assignment,
            settings={"local_person_recognition": False, "exclude_male_avatar": False,
                      "location_enabled": False, "mode_limits": {"followers": {"per_target_limit": 1}}})

    async def test_nonlive_current_finishes_then_owner_lock_waits_and_unlock_resumes(self):
        started = asyncio.Event()
        release = self.release
        calls = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, *, limit):
                calls.append(target)
                if target == "first":
                    started.set()
                    await release.wait()
                return await super().collect_followers(target, limit=limit)
        task = self.make_task(["first", "second"])
        self.manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=Worker)
        await self.manager.start(self.owner, task["id"])
        await asyncio.wait_for(started.wait(), 3)
        self.service.set_split_claim_locked(self.owner, True)
        self.manager.notify_split_queue(self.owner)
        release.set()
        await self.wait_until(lambda: self.service.get_task(self.owner, task["id"])["targets"][0]["status"] == "completed")
        await asyncio.sleep(.03)
        self.assertEqual(["first"], calls)
        self.assertEqual("pending", self.service.get_task(self.owner, task["id"])["targets"][1]["status"])
        self.assertIn("window-a", self.manager._runs[task["id"]].leases)
        self.service.set_split_claim_locked(self.owner, False)
        self.manager.notify_split_queue(self.owner)
        await asyncio.wait_for(self.manager.wait(task["id"]), 3)
        self.assertEqual(["first", "second"], calls)
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])

    async def test_locked_row_remains_unlockable_after_preassignment_is_rejected(self):
        row = self.service.upsert_manual_split_candidates(self.owner, [{"username": "locked_first", "queued": True}])[0]
        self.service.set_split_candidate_locked(self.owner, row["id"], True)
        with self.assertRaises(ConflictError):
            self.make_task(["locked_first", "open_second"], assignment="manual")
        unlocked = self.service.set_split_candidate_locked(self.owner, row["id"], False)
        self.assertFalse(unlocked["locked"])
        self.assertIsNone(unlocked["queued_target_id"])
        # Unlock does not authorize a duplicate new source admission. Historical
        # direct-target/waiting overlaps still have to obey locks and assignments.
        with self.assertRaises(ConflictError):
            self.make_task(["locked_first", "open_second"], assignment="manual")
        task = create_legacy_task(
            self.service, self.owner, name="legacy runtime locked intake", modes=["followers"],
            targets=["locked_first", "open_second"], window_ids=["window-a"], assignment_mode="manual",
            settings={"local_person_recognition": False, "exclude_male_avatar": False,
                      "location_enabled": False, "mode_limits": {"followers": {"per_target_limit": 1}}})
        self.service.set_manual_assignments(self.owner, task["id"], {"window-a": "locked_first"})
        calls = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, *, limit):
                calls.append(target)
                return await super().collect_followers(target, limit=limit)
        self.manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=Worker)
        await self.manager.start(self.owner, task["id"])
        await asyncio.wait_for(self.manager.wait(task["id"]), 3)
        self.assertEqual(["locked_first", "open_second"], calls)

    def test_unrelated_window_does_not_wait_for_locked_b_only_target(self):
        queue = asyncio.Queue()
        queue.put_nowait({"id": "b", "username": "locked_b", "allowed_window_ids": ["window-b"]})
        waiting = set()
        with self.assertRaises(asyncio.QueueEmpty):
            ExecutionManager._dequeue_compatible_target(queue, "window-a", {"locked_b"}, waiting)
        self.assertEqual(set(), waiting)
        self.assertEqual(1, queue.qsize())
        with self.assertRaises(asyncio.QueueEmpty):
            ExecutionManager._dequeue_compatible_target(queue, "window-b", {"locked_b"}, waiting)
        self.assertEqual({"locked_b"}, waiting)


if __name__ == "__main__":
    unittest.main()
