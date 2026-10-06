"""Real SQLite/manager coverage for separate collection delete and return actions."""
from __future__ import annotations

import asyncio
import ast
import tempfile
import threading
import unittest
from pathlib import Path

from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.schemas import WorkbenchTaskWindowControlPayload
from app.service import CoreService


class ClosingBrowser:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.closed = []

    def close_profile(self, profile_id):
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Test must release browser cleanup")
        self.closed.append(profile_id)


class ControlledManager(ExecutionManager):
    async def _window_loop(self, control, profile_id, *args):
        # Only the external browser work is replaced. Real stop/join/lease,
        # SQLite transactions, coordinator and sibling ownership remain active.
        await control.stop_event.wait()
        target_id = control.profile_states.get(profile_id, {}).get("current_target_id")
        if target_id:
            self.service.set_target_runtime_status(
                control.owner_user_id, control.task_id, target_id,
                "stopped", window_id=profile_id,
            )
        if profile_id == "window-one" and hasattr(self, "disconnect_release"):
            self.disconnect_entered.set()
            await self.disconnect_release.wait()


class CollectionRemoveR43Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "removal.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("collection-remove-r43", "long test password")['id']
        self.browser = ClosingBrowser()
        self.manager = ControlledManager(self.service, self.browser)
        self.task = self.service.create_task(
            self.owner, name="separate delete and return", modes=["followers"],
            targets=["remove_target", "sibling_target", "next_target"],
            window_ids=["window-one", "window-two"],
            settings={"live_queue_enabled": True, "local_person_recognition": False},
        )
        self.first, self.second, self.next_target = self.task["targets"]

    async def asyncTearDown(self):
        self.browser.release.set()
        if hasattr(self.manager, "disconnect_release"):
            self.manager.disconnect_release.set()
        await self.manager.shutdown()
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def start_bound(self):
        await self.manager.start(self.owner, self.task["id"])
        control = self.manager._runs[self.task["id"]]
        async with asyncio.timeout(2):
            while set(control.started_profile_ids) != {"window-one", "window-two"}:
                await asyncio.sleep(0)
        for profile, target in (("window-one", self.first), ("window-two", self.second)):
            self.service.set_target_runtime_status(
                self.owner, self.task["id"], target["id"], "running", window_id=profile,
            )
            self.manager._profile_state_locked(control, profile, state="working", target=target)
        return control

    async def remove(self, *, requeue=False):
        return await self.manager.delete_window(
            self.owner, self.task["id"], "window-one",
            target_id=self.first["id"], requeue=requeue,
        )

    def target(self):
        return next(item for item in self.service.get_task(self.owner, self.task["id"])["targets"]
                    if item["id"] == self.first["id"])

    def queue(self):
        return [item for item in self.service.list_split_candidates(self.owner)
                if item["username"] == "remove_target"]

    def seed_history(self):
        self.service.record_result(
            self.owner, self.task["id"], self.first["id"], username="saved_account",
            instagram_user_id="43001", source_mode="followers", visibility="private",
            profile={"followers": 4}, screening={}, qualified=True,
        )
        self.service.upsert_checkpoint(
            self.owner, self.task["id"], self.first["id"], mode="followers",
            stage="screening_accounts", cursor={"offset": 17},
            counters={"saved": 1, "processed": 17}, recoverable=True,
        )
        with self.database.read() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in ("task_results", "global_seen", "task_checkpoints")}

    def assert_history(self, before):
        with self.database.read() as connection:
            for table, rows in before.items():
                self.assertEqual(rows, [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")], table)

    async def test_running_direct_delete_preserves_history_sibling_and_never_requeues(self):
        history = self.seed_history()
        control = await self.start_bound()
        sibling_token = control.leases["window-two"]
        result = await self.remove()
        self.assertEqual("delete_only", result["removal_mode"])
        self.assertIsNone(result["requeued_candidate"])
        self.assertEqual("deleted_archived", self.target()["current_stage"])
        self.assertIsNone(self.target()["current_window_id"])
        self.assertTrue(self.target()["manual_recovery_required"])
        self.assertEqual([], self.queue())
        self.assertEqual(sibling_token, control.leases["window-two"])
        self.assertFalse(control.profile_stop_events["window-two"].is_set())
        self.assertFalse(control.profile_worker_tasks["window-two"].done())
        self.assertEqual(["window-one"], self.browser.closed)
        self.assert_history(history)
        # Replayed terminal notifications cannot regenerate a visible failure.
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "stopped")
        self.assertEqual([], self.queue())

    async def test_stopped_direct_delete_is_idempotent_even_for_delayed_return_click(self):
        history = self.seed_history()
        self.service.set_target_runtime_status(
            self.owner, self.task["id"], self.first["id"], "stopped", window_id="window-one"
        )
        first = await self.remove()
        self.assertTrue(first["stale_record_cleaned"])
        for requeue in (False, True):
            replay = await self.remove(requeue=requeue)
            self.assertTrue(replay["already_deleted"])
            self.assertIsNone(replay["requeued_candidate"])
            self.assertEqual([], self.queue())
        self.assert_history(history)

    async def test_return_removes_card_but_creates_one_waiting_entry_with_saved_checkpoint(self):
        history = self.seed_history()
        await self.start_bound()
        first = await self.remove(requeue=True)
        self.assertEqual("requeue", first["removal_mode"])
        self.assertEqual("queued", first["requeued_candidate"]["queue_state"])
        self.assertIsNone(self.target()["current_window_id"])
        self.assertNotIn("window-one", self.service.get_task(self.owner, self.task["id"])["window_ids"])
        await self.remove(requeue=True)
        waiting = self.queue()
        self.assertEqual(1, len(waiting))
        self.assertEqual("manual", waiting[0]["kind"])
        self.assertEqual("queued", waiting[0]["queue_state"])
        self.assert_history(history)

    async def test_cleanup_keeps_lock_and_rejects_resume_and_opposite_disposition(self):
        control = await self.start_bound()
        self.browser.release.clear()
        deleting = asyncio.create_task(self.remove())
        duplicate = None
        try:
            async with asyncio.timeout(2):
                while not self.browser.entered.is_set():
                    await asyncio.sleep(.002)
            duplicate = asyncio.create_task(self.remove())
            with self.assertRaises(ConflictError):
                await self.remove(requeue=True)
            with self.assertRaises(ConflictError):
                await self.manager.resume_window(self.owner, self.task["id"], "window-one")
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, "window-one", operation_type="action", entity_id="competitor")
            deleting.cancel()
            await asyncio.sleep(0)
            self.assertFalse(deleting.done())
            self.assertIn("window-one", control.leases)
        finally:
            self.browser.release.set()
            results = await asyncio.gather(deleting, *([duplicate] if duplicate else []), return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual("deleted", results[1]["status"])
        self.assertEqual(["window-one"], self.browser.closed)
        self.assertEqual({}, self.manager._window_removals)
        self.assertEqual([], self.queue())

    async def test_old_target_click_cannot_stop_or_delete_next_target(self):
        control = await self.start_bound()
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "completed", window_id="window-one")
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.next_target["id"], "running", window_id="window-one")
        self.manager._profile_state_locked(control, "window-one", state="working", target=self.next_target)
        for requeue in (False, True):
            with self.assertRaises(ConflictError):
                await self.remove(requeue=requeue)
        self.assertFalse(control.profile_stop_events["window-one"].is_set())
        self.assertEqual([], self.browser.closed)

    async def test_removed_target_cannot_resume_after_restart_or_old_explicit_retry(self):
        await self.start_bound()
        await self.remove()
        with self.assertRaises(ConflictError):
            self.service.retry_task_target(self.owner, self.task["id"], self.first["id"])
        with self.assertRaises(ConflictError):
            self.service.requeue_removed_task_target(self.owner, self.first["id"])
        with self.assertRaises(ConflictError):
            self.service.delete_task_target(self.owner, self.task["id"], self.first["id"])
        await self.manager.shutdown()
        self.database.initialize()
        self.service = CoreService(self.database)
        self.manager = ControlledManager(self.service, self.browser)
        self.service.retry_task_target(self.owner, self.task["id"], self.second["id"])
        await self.manager.start(self.owner, self.task["id"])
        control = self.manager._runs[self.task["id"]]
        self.assertNotIn(self.first["id"], control.enqueued_target_ids)
        await self.manager._enqueue_resumable_targets(control)
        self.assertNotIn(self.first["id"], control.enqueued_target_ids)
        self.assertEqual([], self.queue())

    async def test_direct_delete_clears_exact_manual_claim_marker(self):
        # A task created from delayed dispatch has a durable queue marker as well
        # as a target; removing the target must not leave that marker visible.
        with self.database.write() as connection:
            connection.execute("UPDATE task_targets SET status='completed' WHERE id=?", (self.first["id"],))
        self.service.upsert_manual_split_candidates(self.owner, [{"username": "queued_remove", "queued": True}])
        self.service.set_task_runtime_status(self.owner, self.task["id"], "running")
        claimed = self.service.claim_next_split_candidate(self.owner, self.task["id"], "window-one")
        self.assertIsNotNone(claimed)
        self.service.set_target_runtime_status(self.owner, self.task["id"], claimed["id"], "stopped", window_id="window-one")
        await self.manager.delete_window(self.owner, self.task["id"], "window-one", target_id=claimed["id"], requeue=False)
        self.assertFalse(any(item["username"] == "queued_remove" for item in self.service.list_split_candidates(self.owner)))
        with self.database.read() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM split_candidates WHERE queued_target_id=?", (claimed["id"],)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM task_targets WHERE id=?", (claimed["id"],)).fetchone())

    async def test_completed_target_keeps_completion_history_without_any_requeue(self):
        history = self.seed_history()
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "completed", window_id="window-one")
        result = await self.remove()
        self.assertTrue(result["completed_archived"])
        self.assertEqual("completed", self.target()["status"])
        self.assertEqual("completed_archived", self.target()["current_stage"])
        self.assertEqual([], self.queue())
        self.assert_history(history)

    async def test_pending_disconnect_blocks_retry_and_failure_requeue_from_other_thread(self):
        for requeue in (False, True):
            with self.subTest(requeue=requeue):
                # Each disposition gets its own target generation.
                if requeue:
                    await self.manager.shutdown()
                    self.manager = ControlledManager(self.service, self.browser)
                    self.task = self.service.create_task(
                        self.owner, name="return fence", modes=["followers"],
                        targets=["remove_again", "sibling_again"], window_ids=["window-one", "window-two"],
                        settings={"live_queue_enabled": True, "local_person_recognition": False},
                    )
                    self.first, self.second = self.task["targets"]
                control = await self.start_bound()
                self.manager.disconnect_entered = asyncio.Event()
                self.manager.disconnect_release = asyncio.Event()
                deleting = asyncio.create_task(self.remove(requeue=requeue))
                try:
                    await asyncio.wait_for(self.manager.disconnect_entered.wait(), 2)
                    failure = next(item for item in self.service.list_split_candidates(self.owner)
                                   if item.get("source_target_id") == self.first["id"] and item["kind"] == "failure")
                    with self.assertRaises(ConflictError):
                        await self.manager.retry_target(self.owner, self.task["id"], self.first["id"])
                    with self.assertRaises(ConflictError):
                        await asyncio.to_thread(self.service.requeue_split_candidate, self.owner, failure["id"])
                    self.assertFalse(control.profile_stop_events["window-two"].is_set())
                    self.assertIn("window-one", control.leases)
                finally:
                    self.manager.disconnect_release.set()
                    await deleting
                self.assertEqual({}, self.service._target_removal_tokens)

    async def test_stale_idle_delete_cannot_remove_new_target_but_idle_can_be_removed(self):
        control = await self.start_bound()
        with self.assertRaises(ConflictError):
            await self.manager.delete_window(self.owner, self.task["id"], "window-one", requeue=False)
        self.assertFalse(control.profile_stop_events["window-one"].is_set())
        self.assertEqual({}, self.service._target_removal_tokens)
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "completed")
        self.manager._profile_state_locked(control, "window-one", state="idle")
        result = await self.manager.delete_window(self.owner, self.task["id"], "window-one", requeue=False)
        self.assertEqual("deleted", result["status"])
        self.assertEqual("completed", self.target()["status"])

    async def test_legacy_history_and_task_archival_preserve_direct_delete_tombstone(self):
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "stopped", window_id="window-one")
        await self.remove()
        self.service.delete_history_target(self.owner, self.first["id"])
        self.assertEqual("deleted_archived", self.target()["current_stage"])
        self.service.delete_task(self.owner, self.task["id"])
        self.assertEqual("deleted_archived", self.target()["current_stage"])
        with self.assertRaises(ConflictError):
            self.service.requeue_removed_task_target(self.owner, self.first["id"])
        self.assertEqual([], self.queue())

    async def test_explicit_new_waiting_generation_does_not_reuse_removed_target_id(self):
        await self.start_bound()
        await self.remove()
        self.service.upsert_manual_split_candidates(self.owner, [{"username": "remove_target", "queued": True}])
        self.assertIsNone(self.service.claim_next_split_candidate(self.owner, self.task["id"], "window-two"))
        self.assertEqual("deleted_archived", self.target()["current_stage"])

    async def test_resume_waiting_for_queue_lock_cannot_reopen_removed_lease(self):
        control = await self.start_bound()
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "completed")
        self.manager._profile_state_locked(control, "window-one", state="idle")
        await self.manager.stop_window(self.owner, self.task["id"], "window-one")
        await control.queue_claim_lock.acquire()
        resuming = asyncio.create_task(self.manager.resume_window(self.owner, self.task["id"], "window-one"))
        try:
            await asyncio.sleep(0)
            self.assertFalse(resuming.done())
            await self.remove()
            self.assertNotIn("window-one", control.leases)
        finally:
            control.queue_claim_lock.release()
        with self.assertRaises(ConflictError):
            await resuming
        self.assertNotIn("window-one", control.profile_worker_tasks)
        self.assertEqual(["window-one"], self.browser.closed)

    async def test_return_claim_waits_for_cleanup_then_wakes_sibling_without_error(self):
        control = await self.start_bound()
        self.browser.release.clear()
        deleting = asyncio.create_task(self.remove(requeue=True))
        try:
            async with asyncio.timeout(2):
                while not self.browser.entered.is_set():
                    await asyncio.sleep(.002)
            self.assertEqual("queued", self.queue()[0]["queue_state"])
            self.assertIsNone(self.service.claim_next_split_candidate(self.owner, self.task["id"], "window-two"))
            # Simulate a waiting sibling consuming the earlier queue wakeup.
            control.work_available.clear()
            control.profile_work_events["window-two"].clear()
        finally:
            self.browser.release.set()
            await deleting
        self.assertTrue(control.profile_work_events["window-two"].is_set())
        claimed = self.service.claim_next_split_candidate(self.owner, self.task["id"], "window-two")
        self.assertEqual(self.first["id"], claimed["id"])
        self.assertEqual("window-two", claimed["current_window_id"])

    async def test_late_parent_terminal_callback_preserves_returned_waiting_generation(self):
        self.service.set_target_runtime_status(self.owner, self.task["id"], self.first["id"], "stopped", window_id="window-one")
        result = await self.remove(requeue=True)
        waiting_id = result["requeued_candidate"]["id"]
        with self.database.read() as connection:
            original_generation = connection.execute(
                "SELECT candidate_id FROM task_target_recovery_controls WHERE target_id=?", (self.first["id"],)
            ).fetchone()[0]
        for status in ("recoverable", "stopped", "failed"):
            self.service.finalize_task_runtime_status(self.owner, self.task["id"], status)
            waiting = self.queue()
            self.assertEqual(1, len(waiting))
            self.assertEqual((waiting_id, "manual", "queued"), (waiting[0]["id"], waiting[0]["kind"], waiting[0]["queue_state"]))
            with self.database.read() as connection:
                row = connection.execute("SELECT candidate_id,state FROM task_target_recovery_controls WHERE target_id=?", (self.first["id"],)).fetchone()
            self.assertEqual((original_generation, "requeued"), tuple(row))
        # initialize installs the corrected triggers into an existing database on
        # every startup, and repeated initialization leaves the queue untouched.
        self.database.initialize()
        self.database.initialize()
        self.service.finalize_task_runtime_status(self.owner, self.task["id"], "recoverable")
        self.assertEqual("queued", self.queue()[0]["queue_state"])
        self.service.set_task_runtime_status(self.owner, self.task["id"], "running")
        claimed = self.service.claim_next_split_candidate(self.owner, self.task["id"], "window-two")
        self.assertEqual(self.first["id"], claimed["id"])
        # A real failure after a new claim must create a fresh failure generation.
        self.service.finalize_task_runtime_status(self.owner, self.task["id"], "failed")
        failure = self.queue()[0]
        self.assertEqual("failure", failure["kind"])
        self.assertNotEqual(original_generation, failure["id"])

    def test_actual_api_routes_keep_delete_dispositions_distinct(self):
        module = ast.parse((Path(__file__).parents[1] / "app" / "main.py").read_text())
        rest = next(node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef) and node.name == "desktop_delete_task_window")
        rest_call = next(node for node in ast.walk(rest) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete_window")
        self.assertEqual("requeue", ast.unparse(next(k.value for k in rest_call.keywords if k.arg == "requeue")))
        default = dict(zip([arg.arg for arg in rest.args.args[-len(rest.args.defaults):]], rest.args.defaults))["requeue"]
        self.assertEqual("Query(default=True)", ast.unparse(default))
        command = next(node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef) and node.name == "workbench_command")
        call = next(node for node in ast.walk(command) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete_window")
        expr = ast.Expression(next(k.value for k in call.keywords if k.arg == "requeue"))
        from types import SimpleNamespace
        for action, expected in (("delete", True), ("delete_only", False)):
            self.assertIs(expected, eval(compile(expr, "actual_workbench_route", "eval"), {"payload": SimpleNamespace(action=action)}))
        self.assertEqual("payload.target_id", ast.unparse(next(k.value for k in call.keywords if k.arg == "target_id")))

    def test_both_actions_have_distinct_validated_api_values(self):
        for action in ("delete", "delete_only"):
            payload = WorkbenchTaskWindowControlPayload(
                task_id=self.task["id"], profile_id="window-one", target_id=self.first["id"], action=action
            )
            self.assertEqual(action, payload.action)


if __name__ == "__main__":
    unittest.main()
