"""A delayed heartbeat must not borrow a later command's revision."""
from __future__ import annotations

import ast
import asyncio
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from app.errors import DomainError


class LiveObservationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_during_slow_task_read_cannot_relabel_old_status_as_fresh(self):
        source = Path(__file__).parents[1] / "app/main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "workbench_live_status")
        handler.decorator_list = []
        handler.returns = None
        for argument in handler.args.args:
            argument.annotation = None
        row_read = threading.Event()
        release = threading.Event()
        revision = [10]
        observed = []

        def read_task(owner, task_id, *, current_target_ids=()):
            self.assertEqual((owner, task_id), ("owner", "task"))
            captured = {"id": "task", "status": "running", "version": 1,
                        "targets_partial": True, "targets": []}
            row_read.set()
            if not release.wait(3):
                raise TimeoutError("test did not release task read")
            return captured

        def read_revision():
            observed.append(revision[0])
            return revision[0]

        service = SimpleNamespace(get_task_live_status=read_task, get_workbench_revision=read_revision,
                                  get_task_status=lambda owner, task_id: "running")
        manager = SimpleNamespace(active_task_ids=lambda: ("task",),
                                  runtime_diagnostics=AsyncMock(return_value={"profile_states": []}))
        namespace = {"service": service, "execution_manager": manager, "asyncio": asyncio,
                     "datetime": datetime, "timezone": timezone, "DomainError": DomainError, "Any": Any}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                     str(source), "exec"), namespace)
        # Execute the real handler body; HTTP authentication and transport are
        # outside this controlled ordering test, as in the r29 endpoint test.
        request = asyncio.create_task(namespace["workbench_live_status"](({"id": "owner"}, None)))
        try:
            self.assertTrue(await asyncio.to_thread(row_read.wait, 3))
            revision[0] = 11  # a pause/stop commits after this heartbeat read began
        finally:
            release.set()
        reply = await asyncio.wait_for(request, 3)
        self.assertEqual("running", reply["tasks"][0]["task"]["status"])
        self.assertEqual(10, reply["revision"], "old rows must not claim the later command revision")
        self.assertEqual([10], observed)


if __name__ == "__main__":
    unittest.main()
