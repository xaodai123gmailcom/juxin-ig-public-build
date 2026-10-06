"""A slow workbench command must not stall collection, or outlive its owner."""
from __future__ import annotations

import ast
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timezone

from app.async_cleanup import finish_owned
from app.database import Database
from app.errors import DomainError, ValidationError
from app.service import CoreService
from app.platform_scope import validate_platform


def real_command_handler(service, manager=None, **extra):
    # These doubles isolate routing/I/O; real service cases retain the real guard.
    if isinstance(service, SimpleNamespace) and not hasattr(service, 'guard_platform_command'):
        service.guard_platform_command = Mock()
    source = Path(__file__).parents[1] / "app/main.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "workbench_command")
    response = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "workbench_response")
    handler.decorator_list = []
    handler.returns = None
    for arg in handler.args.args:
        arg.annotation = None
    namespace = {"asyncio": asyncio, "finish_owned": finish_owned,
                 "validate_platform": validate_platform,
                 "service": service, "execution_manager": manager,
                 "Any": Any, "ValidationError": ValidationError,
                 "parse_workbench_payload": lambda _, p: SimpleNamespace(**p)}
    for n in ast.walk(handler):
        if isinstance(n, ast.Name) and (n.id.endswith("Payload") or n.id.endswith("Request")):
            namespace[n.id] = object
    namespace.update(extra)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[response, handler], type_ignores=[])),
                 str(source), "exec"), namespace)
    return namespace["workbench_command"]


def waiting_add_body():
    return SimpleNamespace(command="split_waiting_add", payload={
        "targets": ["fresh_candidate"], "allowed_window_ids": [],
        "allow_completed_targets": False,
    })


class WorkbenchCommandIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_projection_includes_terminal_target_still_owned_by_runtime(self):
        source = Path(__file__).parents[1] / "app/main.py"
        handler = next(n for n in ast.walk(ast.parse(source.read_text()))
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "workbench_live_status")
        handler.decorator_list = []
        handler.returns = None
        for arg in handler.args.args:
            arg.annotation = None
        service = SimpleNamespace(get_workbench_revision=lambda: 8,
                                  get_task_status=Mock(return_value="running"),
                                  get_task_live_status=Mock(return_value={"id": "task", "targets": []}))
        runtime = {"profile_states": [
            {"current_target_id": "terminal-but-owned", "state": "stopping"},
            {"current_target_id": "paused-target", "state": "paused"},
            {"current_target_id": "paused-target", "state": "paused"},
            {"current_target_id": None, "state": "starting"},
        ]}
        manager = SimpleNamespace(active_task_ids=lambda: ("task",),
                                  runtime_diagnostics=AsyncMock(return_value=runtime))
        namespace = {"asyncio": asyncio, "datetime": datetime, "timezone": timezone,
                     "service": service, "execution_manager": manager, "Any": Any,
                     "DomainError": DomainError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                     str(source), "exec"), namespace)
        result = await namespace["workbench_live_status"](({"id": "owner"}, None))
        service.get_task_status.assert_called_once_with("owner", "task")
        manager.runtime_diagnostics.assert_awaited_once_with("owner", "task", known_status="running")
        service.get_task_live_status.assert_called_once_with(
            "owner", "task", current_target_ids=("terminal-but-owned", "paused-target"))
        self.assertIs(runtime, result["tasks"][0]["runtime"])

    async def test_real_sqlite_writer_contention_does_not_block_other_coroutines(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "commands.sqlite3")
            database.initialize()
            service = CoreService(database)
            owner = service.register_user("command-r31", "a sufficiently long password")["id"]
            manager = SimpleNamespace(notify_split_queue=Mock())
            handler = real_command_handler(service, manager)
            locked, heartbeat = threading.Event(), threading.Event()
            observations = []

            def competing_writer():
                with database.write():
                    locked.set()
                    # A timeout only releases the old implementation's deadlock;
                    # the assertion checks ordering, not machine throughput.
                    observations.append(heartbeat.wait(0.5))

            holder = threading.Thread(target=competing_writer)
            holder.start()
            try:
                self.assertTrue(await asyncio.to_thread(locked.wait, 3))
                command = asyncio.create_task(handler(waiting_add_body(), ({"id": owner}, None)))
                await asyncio.sleep(0.01)
                heartbeat.set()
                response = await asyncio.wait_for(command, 3)
                self.assertEqual("split_waiting_add", response["command"])
                manager.notify_split_queue.assert_called_once_with(owner)
                self.assertEqual([True], observations,
                                 "database contention stopped unrelated collection heartbeats")
            finally:
                heartbeat.set()
                await asyncio.to_thread(holder.join, 3)
                await asyncio.get_running_loop().shutdown_default_executor()

    async def test_slow_browser_inventory_does_not_block_collection_heartbeat(self):
        heartbeat = threading.Event()
        observations = []

        def inventory():
            observations.append(heartbeat.wait(0.5))
            return ["window"]

        service = SimpleNamespace(get_workbench_revision=lambda: 7)
        handler = real_command_handler(service, bitbrowser=SimpleNamespace(list_all_windows=inventory),
                                       desktop_bitbrowser_profiles_payload=lambda owner, rows: {"profiles": rows})
        command = asyncio.create_task(handler(SimpleNamespace(command="bitbrowser_refresh", payload={}),
                                              ({"id": "owner"}, None)))
        try:
            await asyncio.sleep(0.01)
            heartbeat.set()
            response = await asyncio.wait_for(command, 3)
            self.assertEqual(["window"], response["result"]["windows"])
            self.assertEqual([True], observations, "browser inventory froze collection coroutines")
        finally:
            heartbeat.set()
            await asyncio.gather(command, return_exceptions=True)

    async def test_repeated_request_cancel_waits_for_commit_notification_and_revision(self):
        entered, release = threading.Event(), threading.Event()
        mutations, revisions, notifications = [], [], []

        def mutation(*args, **kwargs):
            entered.set()
            if not release.wait(0.5):
                raise TimeoutError("command mutation was not released")
            mutations.append("committed")
            return {"added": 1}

        service = SimpleNamespace(upsert_manual_split_candidates=mutation,
                                  advance_workbench_revision=lambda: revisions.append(1),
                                  get_workbench_revision=lambda: len(revisions))
        handler = real_command_handler(service, SimpleNamespace(notify_split_queue=notifications.append))
        command = asyncio.create_task(handler(waiting_add_body(), ({"id": "owner"}, None)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            command.cancel()
            await asyncio.sleep(0)
            command.cancel()
            await asyncio.sleep(0)
            self.assertFalse(command.done(), "cancel detached a running mutation from its command")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(command, 3)
            self.assertEqual(["committed"], mutations)
            self.assertEqual(["owner"], notifications)
            self.assertEqual([1], revisions)
        finally:
            release.set()
            await asyncio.gather(command, return_exceptions=True)

    async def test_failed_mutation_does_not_publish_success_or_queue_notification(self):
        failure = ValidationError("bad candidate")
        service = SimpleNamespace(upsert_manual_split_candidates=Mock(side_effect=failure),
                                  advance_workbench_revision=Mock(), get_workbench_revision=Mock())
        manager = SimpleNamespace(notify_split_queue=Mock())
        handler = real_command_handler(service, manager)
        with self.assertRaises(ValidationError) as caught:
            await handler(waiting_add_body(), ({"id": "owner"}, None))
        self.assertIs(failure, caught.exception)
        manager.notify_split_queue.assert_not_called()
        service.advance_workbench_revision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
