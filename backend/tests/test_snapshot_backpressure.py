from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.database import Database
from app.errors import UpstreamUnavailableError
from app.execution_manager import ExecutionControl
from app.main import create_app
from app.service import CoreService


PASSWORD = "correct horse battery staple"


class _FailIfInventoryStarts:
    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        self.calls += 1
        raise AssertionError("disconnected snapshot must not start inventory")


class _ControllableRequest:
    def __init__(self) -> None:
        self.disconnected = False
        self.disconnect_checks = 0

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return self.disconnected


class SnapshotBackpressureTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = Database(self.data_dir / "collector.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.owner = self.service.register_user("snapshot-pressure-owner", PASSWORD)
        self.login = self.service.login("snapshot-pressure-owner", PASSWORD)
        self.settings = Settings(
            startup_token="snapshot-pressure-startup-token-456",
            database_path=self.database.path,
            data_dir=self.data_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _snapshot_endpoint(app: Any) -> Any:
        for route in app.routes:
            if (
                getattr(route, "path", None) == "/api/workbench/snapshot"
                and "GET" in (getattr(route, "methods", None) or set())
            ):
                return route.endpoint
        raise AssertionError("workbench snapshot route is missing")

    @staticmethod
    async def _wait_for_thread_event(
        event: threading.Event, *, timeout: float = 3.0
    ) -> None:
        observed = await asyncio.wait_for(
            asyncio.to_thread(event.wait, timeout), timeout=timeout + 0.5
        )
        if not observed:
            raise AssertionError("timed out waiting for test worker event")

    async def test_known_status_avoids_one_database_read_per_diagnostic(self) -> None:
        app = create_app(
            self.settings,
            database=self.database,
            bitbrowser=_FailIfInventoryStarts(),  # type: ignore[arg-type]
        )
        manager = app.state.execution_manager
        calls = 0

        def unexpected_get_task(owner_user_id: str, task_id: str) -> dict[str, Any]:
            del owner_user_id, task_id
            nonlocal calls
            calls += 1
            raise AssertionError("known status must make get_task unnecessary")

        manager.service.get_task = unexpected_get_task
        diagnostic = await manager.runtime_diagnostics(
            self.owner["id"],
            "known-task",
            known_status="running",
        )

        self.assertEqual("running", diagnostic["status"])
        self.assertEqual(0, calls)

    async def test_disconnect_after_primary_rows_skips_remaining_history_reads(self) -> None:
        app = create_app(self.settings, database=self.database, bitbrowser=_FailIfInventoryStarts())
        service = app.state.service
        original = service.get_workbench_snapshot
        request = _ControllableRequest()
        calls = []
        def primary(*args, **kwargs):
            result = original(*args, **kwargs)
            request.disconnected = True
            return result
        def history(*args, **kwargs):
            calls.append(1)
            return []
        service.get_workbench_snapshot = primary
        service.list_tasks_with_details = history
        service.list_action_campaigns = history
        with self.assertRaises(UpstreamUnavailableError):
            await self._snapshot_endpoint(app)(request, (self.owner, self.login['token']), limit=10, history_limit=10)
        self.assertEqual([], calls, 'timed-out primary read still starts two unnecessary history scans')

    async def test_diagnostics_run_in_batches_and_disconnect_stops_later_work(
        self,
    ) -> None:
        bitbrowser = _FailIfInventoryStarts()
        app = create_app(
            self.settings,
            database=self.database,
            bitbrowser=bitbrowser,  # type: ignore[arg-type]
        )
        endpoint = self._snapshot_endpoint(app)
        manager = app.state.execution_manager
        service = app.state.service
        task_rows = [
            {
                "id": f"active-{index}",
                "status": "running",
                "targets": [],
                "windows": [],
                "modes": ["followers"],
            }
            for index in range(12)
        ]
        service.list_tasks_with_details = lambda *args, **kwargs: task_rows
        service.list_action_campaigns = lambda *args, **kwargs: []

        first_batch_started = asyncio.Event()
        release_first_batch = asyncio.Event()
        started_task_ids: list[str] = []
        observed_statuses: list[str | None] = []

        async def gated_diagnostic(
            owner_user_id: str,
            task_id: str,
            *,
            known_status: str | None = None,
        ) -> dict[str, Any]:
            del owner_user_id
            started_task_ids.append(task_id)
            observed_statuses.append(known_status)
            if len(started_task_ids) == 8:
                first_batch_started.set()
            await release_first_batch.wait()
            return {"task_id": task_id, "status": known_status}

        manager.runtime_diagnostics = gated_diagnostic
        request = _ControllableRequest()
        build = asyncio.create_task(
            endpoint(
                request,
                (self.owner, self.login["token"]),
                limit=100,
                history_limit=100,
            )
        )
        await asyncio.wait_for(first_batch_started.wait(), timeout=3.0)
        self.assertEqual(8, len(started_task_ids))
        request.disconnected = True
        release_first_batch.set()

        result = await asyncio.wait_for(
            asyncio.gather(build, return_exceptions=True), timeout=5.0
        )
        self.assertIsInstance(result[0], UpstreamUnavailableError)
        assert isinstance(result[0], UpstreamUnavailableError)
        self.assertEqual(
            "workbench_snapshot_client_disconnected",
            result[0].details.get("reason"),
        )
        self.assertEqual(
            8,
            len(started_task_ids),
            "a disconnected client must not start a second diagnostics batch",
        )
        self.assertEqual(["running"] * 8, observed_statuses)
        self.assertEqual(0, bitbrowser.calls)

    async def test_blocking_task_read_does_not_hold_network_state_lock(self) -> None:
        app = create_app(
            self.settings,
            database=self.database,
            bitbrowser=_FailIfInventoryStarts(),  # type: ignore[arg-type]
        )
        manager = app.state.execution_manager
        pause_event = asyncio.Event()
        pause_event.set()
        control = ExecutionControl(
            owner_user_id=self.owner["id"],
            task_id="live-task",
            pause_event=pause_event,
            stop_event=asyncio.Event(),
            leases={},
            target_queue=asyncio.Queue(),
        )
        manager._runs[control.task_id] = control
        get_task_entered = threading.Event()
        release_get_task = threading.Event()

        def blocking_get_task(owner_user_id: str, task_id: str) -> dict[str, Any]:
            del owner_user_id, task_id
            get_task_entered.set()
            if not release_get_task.wait(timeout=5.0):
                raise TimeoutError("test did not release get_task")
            return {"status": "running"}

        manager.service.get_task = blocking_get_task
        diagnostic_task = asyncio.create_task(
            manager.runtime_diagnostics(self.owner["id"], control.task_id)
        )
        probe_task: asyncio.Task[None] | None = None
        acquired_before_release = False
        try:
            await self._wait_for_thread_event(get_task_entered)
            lock_acquired = asyncio.Event()

            async def probe_network_lock() -> None:
                async with control.network_state_lock:
                    lock_acquired.set()

            probe_task = asyncio.create_task(probe_network_lock())
            try:
                await asyncio.wait_for(lock_acquired.wait(), timeout=0.5)
                acquired_before_release = True
            except TimeoutError:
                acquired_before_release = False
        finally:
            release_get_task.set()

        await asyncio.wait_for(diagnostic_task, timeout=3.0)
        if probe_task is not None:
            await asyncio.wait_for(probe_task, timeout=3.0)
        self.assertTrue(
            acquired_before_release,
            "a slow SQLite get_task read must happen outside network_state_lock",
        )


if __name__ == "__main__":
    unittest.main()
