"""Bounded live task projections use real SQLite history and ownership."""
from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from app.database import Database
from app.errors import DomainError, NotFoundError
from app.service import CoreService


OLD = "2026-01-01T00:00:00.000+00:00"
RECENT = "2026-09-16T00:00:00.000+00:00"


class LiveTaskSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "live.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) "
                "VALUES(?,?,?,?,?)",
                [(owner, owner, owner, "unused-test-hash", OLD) for owner in ("owner", "other")],
            )
            connection.executemany(
                "INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) "
                "VALUES(?,?,?,'running','[\"followers\"]','{}',?,?)",
                [(task, owner, task, OLD, OLD) for task, owner in (("task", "owner"), ("foreign", "other"))],
            )

    def tearDown(self):
        self.temp.cleanup()

    def seed_targets(self, count, *, task="task", prefix="history", offset=0,
                     status="completed", stamp=RECENT, occupied=False):
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                "current_window_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ((f"{prefix}{index:05d}", task, f"{prefix}{index:05d}", f"{prefix}{index:05d}",
                  offset + index + 1, status, f"window{index:05d}" if occupied else None, OLD, stamp)
                 for index in range(count)),
            )

    def test_twenty_thousand_history_rows_stay_out_of_live_payload(self):
        self.seed_targets(20_000)
        self.seed_targets(1, prefix="active", offset=20_000, status="running", stamp=OLD, occupied=True)
        projected_ids = []
        original_progress = self.service._mode_progress_for_targets

        def selected_progress(connection, target_ids):
            ids = list(target_ids)
            projected_ids.extend(ids)
            return original_progress(connection, ids)

        with patch.object(self.service, "_mode_progress_for_targets",
                          side_effect=selected_progress) as progress:
            live = self.service.get_task_live_status("owner", "task")
        self.assertTrue(live["targets_partial"])
        self.assertNotIn("targets_truncated", live)
        self.assertEqual(201, len(live["targets"]))
        self.assertIn("active00000", {row["id"] for row in live["targets"]})
        self.assertLess(len(json.dumps(live).encode()), 150_000)
        # Progress receives only the selected rows, not the lifetime target list.
        progress.assert_called_once()
        self.assertEqual({row["id"] for row in live["targets"]}, set(projected_ids))
        self.assertEqual(201, len(projected_ids))
        full = self.service.get_task("owner", "task")
        self.assertEqual(20_001, len(full["targets"]))
        self.assertNotIn("targets_partial", full)
        self.assertEqual("active00000", full["targets"][-1]["id"])

    def test_every_occupied_window_survives_recent_page_limit(self):
        self.seed_targets(300, prefix="active", status="paused", stamp=OLD, occupied=True)
        self.seed_targets(300, offset=300)
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO task_windows(task_id,profile_id,queue_order) VALUES('task',?,?)",
                ((f"window{index:05d}", index+1) for index in range(305)),
            )
        live = self.service.get_task_live_status("owner", "task")
        returned = {row["id"] for row in live["targets"]}
        self.assertEqual(500, len(returned))
        self.assertTrue({f"active{index:05d}" for index in range(300)} <= returned)
        self.assertEqual([f"window{index:05d}" for index in range(305)], live["window_ids"])

    def test_recent_completion_and_explicit_window_unbinding_are_returned(self):
        self.seed_targets(500, stamp=OLD)
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_targets SET status='completed',current_window_id=NULL,updated_at=? "
                "WHERE id='history00000'", (RECENT,),
            )
        live = self.service.get_task_live_status("owner", "task")
        by_id = {row["id"]: row for row in live["targets"]}
        self.assertEqual(200, len(by_id))
        self.assertEqual("completed", by_id["history00000"]["status"])
        self.assertIsNone(by_id["history00000"]["current_window_id"])
        # Tied timestamps have a deterministic id tie-breaker.
        self.assertIn("history00499", by_id)
        self.assertNotIn("history00001", by_id)

    def test_owner_is_checked_before_projecting_any_foreign_targets(self):
        self.seed_targets(1, task="foreign", prefix="foreign", occupied=True)
        with self.assertRaises(NotFoundError):
            self.service.get_task_live_status("owner", "foreign")
        own = self.service.get_task_live_status("owner", "task")
        self.assertEqual([], own["targets"])
        other = self.service.get_task_live_status("other", "foreign")
        self.assertEqual(["foreign00000"], [row["id"] for row in other["targets"]])

    def test_task_and_target_remain_in_one_read_snapshot_during_completion(self):
        self.seed_targets(1, prefix="active", status="running", stamp=OLD, occupied=True)
        original_owned = self.service._owned_task

        def complete_after_task_read(connection, owner, task_id):
            row = original_owned(connection, owner, task_id)
            # A separate WAL writer commits between the task and target reads.
            with self.database.write() as writer:
                writer.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id='task'", (RECENT,))
                writer.execute("UPDATE task_targets SET status='completed',current_window_id=NULL,updated_at=? WHERE task_id='task'", (RECENT,))
            return row

        with patch.object(self.service, "_owned_task", side_effect=complete_after_task_read):
            before = self.service.get_task_live_status("owner", "task")
        self.assertEqual("running", before["status"])
        self.assertEqual("running", before["targets"][0]["status"])
        self.assertEqual("window00000", before["targets"][0]["current_window_id"])
        after = self.service.get_task_live_status("owner", "task")
        self.assertEqual("completed", after["status"])
        self.assertEqual("completed", after["targets"][0]["status"])
        self.assertIsNone(after["targets"][0]["current_window_id"])

    def test_live_projection_preserves_full_target_and_progress_semantics(self):
        self.seed_targets(2, status="running", occupied=True)
        target = "history00000"
        self.service.upsert_checkpoint(
            "owner", "task", target, mode="followers", stage="screening_accounts",
            cursor={"candidate_username": "example"},
            counters={"source_total": 500, "discovered": 100, "processed": 90, "saved": 7},
        )
        full = self.service.get_task("owner", "task")
        live = self.service.get_task_live_status("owner", "task")
        self.assertEqual(full, {key: value for key, value in live.items() if key != "targets_partial"})
        self.assertEqual(7, live["targets"][0]["mode_progress"]["followers"]["saved"])


class LiveStatusEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_endpoint_uses_partial_projection_and_skips_foreign_task(self):
        # Execute the actual handler without starting the optional FastAPI/browser
        # stack. Request authentication is deliberately outside this unit's scope.
        source = Path(__file__).parents[1] / "app" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "workbench_live_status")
        handler.decorator_list = []
        handler.returns = None
        for argument in handler.args.args:
            argument.annotation = None
        task = {"id": "task", "status": "running", "targets_partial": True, "targets": []}

        def read(owner, task_id, *, current_target_ids=()):
            self.assertEqual("owner", owner)
            if task_id == "foreign":
                raise NotFoundError("foreign task")
            return task

        service = SimpleNamespace(
            get_task_live_status=Mock(side_effect=read),
            get_task_status=Mock(side_effect=lambda owner, task_id: read(owner, task_id)["status"]),
            get_task=Mock(side_effect=AssertionError("live heartbeat must not load full history")),
            get_workbench_revision=Mock(return_value=23),
        )
        manager = SimpleNamespace(active_task_ids=lambda: ("task", "foreign"),
                                  runtime_diagnostics=AsyncMock(return_value={"status": "running"}))
        namespace = {"service": service, "execution_manager": manager, "asyncio": asyncio,
                     "datetime": datetime, "timezone": timezone, "DomainError": DomainError, "Any": Any}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                     str(source), "exec"), namespace)
        result = await namespace["workbench_live_status"](({"id": "owner"}, None))
        self.assertEqual(23, result["revision"])
        self.assertEqual([task], [row["task"] for row in result["tasks"]])
        self.assertEqual(2, service.get_task_status.call_count)
        self.assertEqual(1, service.get_task_live_status.call_count)
        service.get_task_live_status.assert_called_once_with("owner", "task", current_target_ids=())
        service.get_task.assert_not_called()
        manager.runtime_diagnostics.assert_awaited_once_with("owner", "task", known_status="running")


if __name__ == "__main__":
    unittest.main()
