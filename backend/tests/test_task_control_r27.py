"""Target-bound window controls use real SQLite leases and controlled workers."""
from __future__ import annotations

import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.async_cleanup import finish_owned
from app.database import Database
from app.errors import ConflictError
from app.service import CoreService
from app.platform_scope import validate_platform
from test_runtime_r24 import CloseClient, WaitingManager


class TargetBoundWindowControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(Path(self.temp.name) / "control.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "target-control-r27", "test password sufficiently long"
        )["id"]
        self.browser = CloseClient()
        self.manager = WaitingManager(self.service, self.browser)
        self.addAsyncCleanup(self._shutdown_manager)
        self.task = self.service.create_task(
            self.owner, name="target-bound controls", modes=["followers"],
            targets=["first_target", "second_target", "next_target"],
            window_ids=["window-one", "window-two"],
            settings={"local_person_recognition": False, "live_queue_enabled": True},
        )
        self.first, self.second, self.next_target = self.task["targets"]
        await self.manager.start(self.owner, self.task["id"])
        self.control = self.manager._runs[self.task["id"]]
        async with asyncio.timeout(30):
            while set(self.control.started_profile_ids) != {"window-one", "window-two"}:
                await asyncio.sleep(0)
        self.bind("window-one", self.first)
        self.bind("window-two", self.second)

    async def _shutdown_manager(self):
        self.browser.release.set()
        await self.manager.shutdown()
        await asyncio.get_running_loop().shutdown_default_executor()

    def bind(self, profile, target):
        self.service.set_target_runtime_status(
            self.owner, self.task["id"], target["id"], "running", window_id=profile
        )
        self.manager._profile_state_locked(
            self.control, profile, state="working", target=target
        )

    def projection(self):
        return {
            "task": self.service.get_task(self.owner, self.task["id"]),
            "leases": self.lease_ownership(),
            "pause": {p: e.is_set() for p, e in self.control.profile_pause_events.items()},
            "stop": {p: e.is_set() for p, e in self.control.profile_stop_events.items()},
            "closed": list(self.browser.closed),
        }

    def lease_ownership(self):
        # Heartbeat timestamps may advance during an awaited stop; ownership must
        # stay byte-for-byte the same, including the exact lease generation.
        with self.database.read() as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT profile_id, owner_user_id, operation_type, entity_id, lease_token "
                "FROM browser_operation_leases ORDER BY profile_id"
            )]

    async def assert_both_reject_unchanged(self, *, owner=None, target=None):
        before = self.projection()
        for action in (self.manager.pause_window, self.manager.stop_window):
            with self.assertRaises(ConflictError):
                await action(owner or self.owner, self.task["id"], "window-one",
                             expected_target_id=target or self.first["id"])
            self.assertEqual(before, self.projection())

    async def test_matching_pause_affects_only_owned_window_and_retains_lease(self):
        leases = self.lease_ownership()
        result = await self.manager.pause_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual("paused", result["status"])
        self.assertFalse(self.control.profile_pause_events["window-one"].is_set())
        self.assertTrue(self.control.profile_pause_events["window-two"].is_set())
        self.assertTrue(self.control.pause_event.is_set())
        self.assertEqual(leases, self.lease_ownership())
        self.assertEqual([], self.browser.closed)

    async def test_matching_stop_does_not_stop_sibling_or_release_its_window(self):
        leases = self.lease_ownership()
        result = await self.manager.stop_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual("stopped", result["status"])
        self.assertTrue(self.control.profile_stop_events["window-one"].is_set())
        self.assertFalse(self.control.profile_stop_events["window-two"].is_set())
        self.assertFalse(self.control.profile_worker_tasks["window-two"].done())
        self.assertFalse(self.control.stop_event.is_set())
        self.assertEqual(leases, self.lease_ownership())
        self.assertEqual([], self.browser.closed)

    async def test_target_bound_stop_remains_available_after_window_pause(self):
        leases = self.lease_ownership()
        paused = await self.manager.pause_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual("paused", paused["status"])
        self.assertFalse(self.control.profile_pause_events["window-one"].is_set())
        self.assertEqual(self.first["id"], self.control.profile_states["window-one"]["current_target_id"])

        stopped = await self.manager.stop_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual("stopped", stopped["status"])
        self.assertTrue(self.control.profile_stop_events["window-one"].is_set())
        self.assertFalse(self.control.profile_stop_events["window-two"].is_set())
        self.assertFalse(self.control.profile_worker_tasks["window-two"].done())
        self.assertEqual(leases, self.lease_ownership())
        self.assertEqual([], self.browser.closed)

    async def test_target_bound_stop_remains_available_after_global_task_pause(self):
        leases = self.lease_ownership()
        paused = await self.manager.pause(self.owner, self.task["id"])
        self.assertEqual("paused", paused["status"])
        self.assertFalse(self.control.pause_event.is_set())

        stopped = await self.manager.stop_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual("stopped", stopped["status"])
        self.assertTrue(self.control.profile_stop_events["window-one"].is_set())
        self.assertFalse(self.control.profile_stop_events["window-two"].is_set())
        self.assertFalse(self.control.profile_worker_tasks["window-two"].done())
        self.assertFalse(self.control.pause_event.is_set())
        self.assertFalse(self.control.stop_event.is_set())
        self.assertEqual("paused", self.service.get_task(self.owner, self.task["id"])["status"])
        self.assertEqual(leases, self.lease_ownership())
        self.assertEqual([], self.browser.closed)

    async def test_window_continue_does_not_report_running_under_global_manual_pause(self):
        leases = self.lease_ownership()
        await self.manager.pause(self.owner, self.task["id"])
        await self.manager.pause_window(self.owner, self.task["id"], "window-one")
        result = await self.manager.resume_window(self.owner, self.task["id"], "window-one")
        self.assertEqual("paused", result["status"])
        self.assertTrue(result["waiting_for_task_resume"])
        state = self.control.profile_states["window-one"]
        self.assertEqual("paused", state["state"])
        self.assertIn("全部继续", state["message"])
        self.assertEqual(self.first["id"], state["current_target_id"])
        self.assertFalse(self.control.pause_event.is_set())
        self.assertTrue(self.control.profile_pause_events["window-one"].is_set())
        self.assertEqual(leases, self.lease_ownership())
        await self.manager.resume(self.owner, self.task["id"])
        self.assertTrue(self.control.pause_event.is_set())
        result = await self.manager.resume_window(self.owner, self.task["id"], "window-one")
        self.assertEqual("running", result["status"])
        self.assertFalse(result.get("waiting_for_task_resume", False))

    async def test_global_pause_while_local_continue_waits_is_reflected_in_reply(self):
        await self.control.network_state_lock.acquire()
        pending = asyncio.create_task(self.manager.resume_window(self.owner, self.task["id"], "window-one"))
        try:
            await asyncio.sleep(0)
            await self.manager.pause(self.owner, self.task["id"])
        finally:
            self.control.network_state_lock.release()
        result = await pending
        self.assertEqual("paused", result["status"])
        self.assertFalse(self.control.pause_event.is_set())
        self.assertEqual("paused", self.control.profile_states["window-one"]["state"])

    async def test_previous_target_card_cannot_control_next_target_in_same_task(self):
        self.service.set_target_runtime_status(
            self.owner, self.task["id"], self.first["id"], "completed", window_id="window-one"
        )
        self.bind("window-one", self.next_target)
        await self.assert_both_reject_unchanged()

    async def test_durable_binding_mismatch_rejects_even_if_runtime_card_matches(self):
        # The public status API now prevents moving a running target to a
        # different occupied window. Preserve that guard, then inject the
        # inconsistent durable row directly to simulate an old/stale database
        # while the in-memory card still points at window-one.
        with self.assertRaises(ConflictError) as reassignment:
            self.service.set_target_runtime_status(
                self.owner, self.task["id"], self.first["id"], "running",
                window_id="window-two",
            )
        self.assertEqual("target_owned_by_other_window", reassignment.exception.details["reason"])
        durable = self.service.get_task(self.owner, self.task["id"])["targets"][0]
        self.assertEqual("window-one", durable["current_window_id"])
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_targets SET current_window_id=? WHERE id=? AND task_id=?",
                ("window-two", self.first["id"], self.task["id"]),
            )
        await self.assert_both_reject_unchanged()

    async def test_replacement_owner_lease_rejects_old_live_control(self):
        old_token = self.control.leases["window-one"]
        self.service.release_browser_lease("window-one", old_token)
        replacement = self.service.acquire_browser_lease(
            self.owner, "window-one", operation_type="action", entity_id="new-greeting-task"
        )
        try:
            await self.assert_both_reject_unchanged()
        finally:
            self.service.release_browser_lease("window-one", replacement)

    async def test_inactive_target_card_does_not_edit_old_task_or_new_owner(self):
        await self.manager.stop(self.owner, self.task["id"], close_windows=False)
        replacement = self.service.acquire_browser_lease(
            self.owner, "window-one", operation_type="action", entity_id="new-active-task"
        )
        try:
            await self.assert_both_reject_unchanged()
        finally:
            self.service.release_browser_lease("window-one", replacement)

    async def test_wrong_owner_and_other_window_target_are_rejected(self):
        await self.assert_both_reject_unchanged(owner="another-owner")
        await self.assert_both_reject_unchanged(target=self.second["id"])

    async def test_pause_sets_gate_before_yielding_after_target_validation(self):
        observations = []
        validate = self.manager._validate_window_target_control

        def observe_after_validation(*args):
            validate(*args)
            asyncio.get_running_loop().call_soon(
                lambda: observations.append(self.control.profile_pause_events["window-one"].is_set())
            )

        self.manager._validate_window_target_control = observe_after_validation
        await self.control.network_state_lock.acquire()
        pending = asyncio.create_task(self.manager.pause_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        ))
        try:
            async with asyncio.timeout(30):
                while not observations:
                    await asyncio.sleep(0)
            self.assertEqual([False], observations)
        finally:
            self.control.network_state_lock.release()
            await pending

    async def test_stop_sets_gate_before_yielding_after_target_validation(self):
        observations = []
        validate = self.manager._validate_window_target_control

        def observe_after_validation(*args):
            validate(*args)
            asyncio.get_running_loop().call_soon(
                lambda: observations.append(self.control.profile_stop_events["window-one"].is_set())
            )

        self.manager._validate_window_target_control = observe_after_validation
        await self.manager.stop_window(
            self.owner, self.task["id"], "window-one", expected_target_id=self.first["id"]
        )
        self.assertEqual([True], observations)

    async def test_legacy_controls_without_expected_target_remain_compatible(self):
        await self.manager.pause_window(self.owner, self.task["id"], "window-one")
        self.assertFalse(self.control.profile_pause_events["window-one"].is_set())
        await self.manager.stop_window(self.owner, self.task["id"], "window-one")
        self.assertTrue(self.control.profile_stop_events["window-one"].is_set())


class ControlFixtureLifecycleTests(unittest.TestCase):
    def test_failed_setup_still_shuts_down_manager_and_removes_database(self):
        class FailedSetup(TargetBoundWindowControlTests):
            async def asyncSetUp(self):
                await super().asyncSetUp()
                raise RuntimeError("CONTROLLED_SETUP_FAILURE")

            async def test_probe(self):
                self.fail("body must not execute after failed setup")

        case = FailedSetup("test_probe")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(1, len(result.errors), result.errors)
        self.assertIn("CONTROLLED_SETUP_FAILURE", result.errors[0][1])
        self.assertEqual([], result.failures)
        self.assertTrue(case.manager._closing)
        self.assertEqual({}, case.manager._runs)
        self.assertFalse(Path(case.temp.name).exists())

    def test_slow_worker_start_preserves_real_window_ownership_checks(self):
        original = WaitingManager._run
        async def slow_start(manager, control):
            await asyncio.sleep(2.2)
            return await original(manager, control)

        case = TargetBoundWindowControlTests(
            "test_matching_pause_affects_only_owned_window_and_retains_lease")
        result = unittest.TestResult()
        with patch.object(WaitingManager, "_run", slow_start):
            case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual({}, case.manager._runs)
        self.assertFalse(Path(case.temp.name).exists())


class WorkbenchControlRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_command_handler_forwards_target_for_pause_and_stop(self):
        # Execute the real handler body without starting FastAPI/browser services.
        # Parsing/authentication are outside this routing regression's scope.
        source = Path(__file__).parents[1] / "app" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "workbench_command")
        handler.decorator_list = []
        handler.returns = None
        for argument in handler.args.args:
            argument.annotation = None
        manager = SimpleNamespace(pause_window=AsyncMock(return_value={"status": "paused"}),
                                  stop_window=AsyncMock(return_value={"status": "stopped"}))
        namespace = {
            "asyncio": asyncio, "finish_owned": finish_owned,
            "execution_manager": manager,
            "service": SimpleNamespace(advance_workbench_revision=Mock(), guard_platform_command=Mock()),
            "validate_platform": validate_platform,
            "WorkbenchTaskWindowControlPayload": object,
            "parse_workbench_payload": lambda _model, payload: SimpleNamespace(**payload),
            "workbench_response": lambda command, result: {"command": command, "result": result},
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                     str(source), "exec"), namespace)
        for action in ("pause", "stop"):
            for target in ("observed-target", None):
                body = SimpleNamespace(command="task_window_control", payload={
                    "task_id": "current-task", "profile_id": "current-window",
                    "action": action, "target_id": target,
                })
                await namespace["workbench_command"](body, ({"id": "owner"}, None))
                getattr(manager, action + "_window").assert_awaited_with(
                    "owner", "current-task", "current-window", expected_target_id=target
                )


if __name__ == "__main__":
    unittest.main()
