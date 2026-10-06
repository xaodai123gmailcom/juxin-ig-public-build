"""Unread profile pages stay in the durable candidate queue until inspected."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import WorkerExecutionError
from app.service import CoreService


class _ProfileWorker:
    handles_profile_read_retries = True

    def __init__(self) -> None:
        self.unavailable = True
        self.reads: list[str] = []

    async def read_visible_profile(self, username: str, *, include_activity: bool = False):
        self.reads.append(username)
        if username == "first.unread" and self.unavailable:
            raise WorkerExecutionError(
                "Instagram content is not visible",
                reason="instagram_content_not_visible",
                pause_required=False,
            )
        return {
            "username": username,
            "visibility": "private",
            "posts": 11,
            "followers": 103,
            "following": 616,
        }


class UnreadProfileQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp.name) / "unread.sqlite3")
        database.initialize()
        self.service = CoreService(database)
        self.owner = self.service.register_user("unread-queue", "long-enough-queue-password")["id"]
        self.task = self.service.create_task(
            self.owner,
            name="Unread candidate queue",
            modes=["followers"],
            targets=["source.account"],
            settings={"local_person_recognition": False, "location_enabled": False},
        )
        self.target = self.task["targets"][0]["id"]
        self.service.append_task_mode_candidates(
            self.owner, self.task["id"], self.target, "followers",
            ["first.unread", "second.readable"],
        )
        pause = asyncio.Event()
        pause.set()
        self.control = ExecutionControl(
            owner_user_id=self.owner, task_id=self.task["id"],
            pause_event=pause, stop_event=asyncio.Event(), leases={},
            target_queue=asyncio.Queue(),
        )
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.worker = _ProfileWorker()

    async def asyncTearDown(self) -> None:
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def _drain(self, failures: dict) -> dict:
        return await self.manager._drain_candidate_spool(
            self.control, self.worker, self.target, "followers",
            self.task["settings"], discovery_complete=True,
            checkpoint_writer=lambda _stats: None,
            candidate_failures=failures, defer_candidate_failures=True,
        )

    async def test_unread_account_waits_while_next_account_finishes_and_retries(self) -> None:
        failures: dict = {}
        stats = await self._drain(failures)
        self.assertEqual((1, 1), (stats["pending"], stats["recorded"]))
        self.assertIn("first.unread", failures)
        self.assertEqual("instagram_content_not_visible", failures["first.unread"].code)
        self.assertEqual(["first.unread"], [row["username"] for row in
            self.service.list_pending_task_mode_candidates(
                self.owner, self.task["id"], self.target, "followers")])
        self.assertEqual(["second.readable"], [row["username"] for row in
            self.service.list_results(self.owner, self.task["id"])])

        # The username-first identity belongs to this pending source row. A
        # restart may reuse that claim but must not fabricate a completed result.
        self.worker.unavailable = False
        stats = await self._drain({})
        self.assertEqual((0, 2, 0),
            (stats["pending"], stats["recorded"], stats["deduped"]))
        self.assertEqual(["first.unread", "second.readable"], sorted(
            row["username"] for row in self.service.list_results(
                self.owner, self.task["id"])))
        self.assertEqual(1, self.worker.reads.count("second.readable"))


if __name__ == "__main__":
    unittest.main()
