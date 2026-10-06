"""An explicit return releases only the former window's owned generation."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.service import CoreService
from test_core import FakeCollectionWorker


class ReturnedWindowReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "returned.sqlite")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("returned-r73", "correct horse battery staple")["id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def failed(self, *, live=True):
        task = self.service.create_task(
            self.owner, name="return", modes=["followers"], targets=["source.one"],
            window_ids=["window-a", "window-b"], settings={"live_queue_enabled": live},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable", window_id="window-a"
        )
        failure = next(row for row in self.service.list_split_candidates(self.owner)
                       if row["kind"] == "failure")
        return task, target, failure

    def test_return_transfers_old_owner_and_retains_b_window_affinity_across_restart(self) -> None:
        task, target, failure = self.failed()
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertFalse(self.service.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        waiting = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        restored = CoreService(Database(self.database.path))
        self.assertTrue(restored.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        self.assertFalse(restored.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertFalse(restored.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a"
        ))
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.assertFalse(restored.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(restored.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-b"
        ))
        claimed = self.service.claim_next_split_candidate(self.owner, task["id"], "window-b")
        self.assertEqual(target["id"], claimed["id"])
        self.assertEqual("window-b", claimed["current_window_id"])
        self.assertFalse(restored.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(restored.has_unfinished_window_targets(
            self.owner, task["id"], "window-b"
        ))
        row = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual("claimed", row["queue_state"])

    def test_same_window_may_claim_the_returned_candidate(self) -> None:
        task, _, failure = self.failed()
        self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-a"]
        )
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.assertFalse(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a"
        ))
        self.assertFalse(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-b"
        ))

    def test_paused_or_locked_same_window_must_keep_its_future_queue_reader(self) -> None:
        task, _, failure = self.failed()
        waiting = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-a"]
        )
        self.service.set_task_runtime_status(self.owner, task["id"], "paused")
        self.assertFalse(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a", include_temporarily_blocked=True
        ))
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.service.set_split_candidate_locked(self.owner, waiting["id"], True)
        self.assertFalse(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_claimable_split_candidate_for_window(
            self.owner, task["id"], "window-a", include_temporarily_blocked=True
        ))

    def test_nonlive_retry_preserves_old_owner_until_durable_rebind(self) -> None:
        task, target, failure = self.failed(live=False)
        self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        self.assertFalse(self.service.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.service.retry_task_target(self.owner, task["id"], target["id"])
        self.assertFalse(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-b"
        ))

    def test_ambiguous_legacy_window_binding_keeps_lease_for_manual_resolution(self) -> None:
        task, target, failure = self.failed()
        self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_targets SET current_window_id='window-a' WHERE id=?",
                (target["id"],),
            )
        self.assertFalse(self.service.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))

    def test_second_failure_names_real_b_window_not_preferred_a(self) -> None:
        task, target, failure = self.failed()
        self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.service.claim_next_split_candidate(self.owner, task["id"], "window-b")
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable", window_id="window-b"
        )
        second = next(row for row in self.service.list_split_candidates(self.owner)
                      if row["kind"] == "failure")
        self.assertEqual("window-b", second["source_window_id"])
        self.assertFalse(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-b"
        ))
        self.service.requeue_split_candidate(
            self.owner, second["id"], allowed_window_ids=["window-a"]
        )
        self.assertFalse(self.service.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_returned_window_target(
            self.owner, task["id"], "window-b"
        ))


class _Browser:
    def __init__(self):
        self.closed = []

    def close_profile(self, profile_id):
        self.closed.append(profile_id)
        return {"closed": True}


class _FailOnce(ExecutionManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = 0

    async def _execute_candidate_spooled_mode(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            return {"total": 1, "recorded": 1, "deduped": 0, "pending": 0,
                    "source_total": 1, "skipped_posts": [], "discovery_complete": False}
        return await super()._execute_candidate_spooled_mode(*args, **kwargs)


class ReturnedWindowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        db = Database(Path(self.temp.name) / "runtime.sqlite")
        db.initialize()
        self.service = CoreService(db)
        self.owner = self.service.register_user("returned-runtime", "correct horse battery staple")["id"]
        self.browser = _Browser()
        self.release_disconnect = asyncio.Event()
        self.disconnect_entered = asyncio.Event()
        self.manager = None

    async def asyncTearDown(self) -> None:
        self.release_disconnect.set()
        if self.manager is not None:
            await asyncio.wait_for(self.manager.shutdown(), 10)
        self.temp.cleanup()

    async def until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(poll(), 10)

    async def start(self, *, wait_disconnect=False, window_ids=None):
        audit = self

        class Worker(FakeCollectionWorker):
            async def disconnect(self):
                if wait_disconnect:
                    audit.disconnect_entered.set()
                    await audit.release_disconnect.wait()

        self.manager = _FailOnce(self.service, self.browser, worker_factory=Worker,
                                 lease_heartbeat_interval_seconds=.03)
        task = self.service.create_task(
            self.owner, name="runtime", modes=["followers"], targets=["source.one"],
            window_ids=window_ids or ["window-a"], settings={
                "live_queue_enabled": True, "local_person_recognition": False,
                "location_enabled": False,
            },
        )
        await self.manager.start(self.owner, task["id"], profile_ids=["window-a"])
        await self.until(lambda: self.service.get_task(self.owner, task["id"])
                         ["targets"][0]["status"] == "recoverable")
        failure = next(row for row in self.service.list_split_candidates(self.owner)
                       if row["kind"] == "failure")
        return task, failure

    async def test_b_only_return_closes_stopped_a_from_owning_loop(self):
        task, failure = await self.start()
        await self.until(lambda: not self.manager._runs[task["id"]].worker_tasks)
        self.assertEqual([], self.browser.closed)
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        self.manager.notify_split_queue(self.owner, returned_candidate=returned)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        self.assertEqual("recoverable", self.service.get_task(self.owner, task["id"])["status"])

    async def test_a_only_return_respawns_worker_and_finishes_once(self):
        task, failure = await self.start()
        await self.until(lambda: not self.manager._runs[task["id"]].worker_tasks)
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-a"]
        )
        self.manager.notify_split_queue(self.owner, returned_candidate=returned)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(2, self.manager.attempts)
        self.assertEqual(["window-a"], self.browser.closed)
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])

    async def test_thread_notification_during_disconnect_rechecks_after_worker_exit(self):
        task, failure = await self.start(wait_disconnect=True)
        await asyncio.wait_for(self.disconnect_entered.wait(), 10)
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        await asyncio.to_thread(
            self.manager.notify_split_queue, self.owner, returned_candidate=returned
        )
        self.assertEqual([], self.browser.closed)
        self.release_disconnect.set()
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_paused_a_only_restarts_its_stopped_worker_on_resume(self):
        task, failure = await self.start()
        await self.until(lambda: not self.manager._runs[task["id"]].worker_tasks)
        await self.manager.pause(self.owner, task["id"])
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-a"]
        )
        self.manager.notify_split_queue(self.owner, returned_candidate=returned)
        await asyncio.sleep(.05)
        self.assertEqual([], self.browser.closed)
        self.assertIn("window-a", self.manager._runs[task["id"]].leases)
        await self.manager.resume(self.owner, task["id"])
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(2, self.manager.attempts)
        self.assertEqual(["window-a"], self.browser.closed)

    async def test_handoff_snapshot_closes_a_even_if_b_fails_before_notification(self):
        task = self.service.create_task(
            self.owner, name="fast-b", modes=["followers"], targets=["source.two"],
            window_ids=["window-a", "window-b"], settings={"live_queue_enabled": True},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable", window_id="window-a"
        )
        failure = next(row for row in self.service.list_split_candidates(self.owner)
                       if row["kind"] == "failure")
        first_return = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.service.claim_next_split_candidate(self.owner, task["id"], "window-b")
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable", window_id="window-b"
        )
        self.assertFalse(self.service.has_returned_window_target(
            self.owner, task["id"], "window-a"
        ))
        self.assertFalse(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-a"
        ))
        self.assertTrue(self.service.has_unfinished_window_targets(
            self.owner, task["id"], "window-b"
        ))
        self.manager = ExecutionManager(self.service, self.browser)
        token = self.service.acquire_browser_lease(
            self.owner, "window-a", operation_type="collection", entity_id=task["id"]
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={"window-a": token},
            target_queue=asyncio.Queue(),
        )
        control.profile_states["window-a"] = {"state": "recoverable"}
        control.coordinator = asyncio.create_task(control.stop_event.wait())
        self.manager._runs[task["id"]] = control
        closure = None
        try:
            self.manager.notify_split_queue(self.owner, returned_candidate=first_return)
            # The provider records its call from a thread BEFORE its response
            # and the event-loop continuation release the durable lease. Join
            # that whole owned operation, not its first externally visible step.
            closure = self.manager._returned_window_closures[(task["id"], "window-a", token)]
            await asyncio.wait_for(asyncio.shield(closure), 10)
            self.assertEqual(["window-a"], self.browser.closed)
            self.assertNotIn("window-a", control.leases)
            self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        finally:
            if closure is not None and not closure.done():
                await asyncio.wait_for(asyncio.shield(closure), 10)
            control.stop_event.set()
            await control.coordinator
            self.manager._runs.pop(task["id"], None)
            self.manager = None

    async def test_stop_waits_for_started_close_before_releasing_owned_lease(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingBrowser(_Browser):
            def close_profile(self, profile_id):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test close remained blocked")
                return super().close_profile(profile_id)

        blocked = BlockingBrowser()
        self.browser = blocked
        task, failure = await self.start()
        await self.until(lambda: not self.manager._runs[task["id"]].worker_tasks)
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"]
        )
        self.manager.notify_split_queue(self.owner, returned_candidate=returned)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            stopping = asyncio.create_task(self.manager.stop(
                self.owner, task["id"], close_windows=False
            ))
            await asyncio.sleep(.05)
            self.assertFalse(stopping.done())
            self.assertIn("window-a", {row["profile_id"] for row in
                                   self.service.list_browser_lease_states(self.owner)})
            release.set()
            await asyncio.wait_for(stopping, 10)
            self.assertEqual(["window-a"], blocked.closed)
            self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        finally:
            release.set()

    async def check_heartbeat_connection_drain(self, *, shutdown=False, repeated_cancels=1, hold_seconds=.02):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        original = self.service.renew_browser_lease

        def blocked_renewal(*args, **kwargs):
            result = original(*args, **kwargs)
            # Hold a real SQLite connection without blocking all writer tests.
            # Windows refuses TemporaryDirectory.cleanup while this is open.
            with self.service.database.read() as connection:
                connection.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('test renewal release timed out')
            closed.set()
            return result

        self.service.renew_browser_lease = blocked_renewal
        stopping = None
        try:
            task, _ = await self.start()
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            heartbeat = next(item for item in asyncio.all_tasks()
                             if item.get_name() == f"lease-heartbeat:{task['id']}")
            operation = (self.manager.shutdown() if shutdown else
                         self.manager.stop(self.owner, task['id'], close_windows=False))
            stopping = asyncio.create_task(operation)
            await self.until(lambda: heartbeat.cancelling() or stopping.done())
            # Repeated cancellation must not detach the same owned I/O.
            for _ in range(repeated_cancels):
                heartbeat.cancel()
                await asyncio.sleep(0)
            await asyncio.sleep(hold_seconds)
            self.assertFalse(stopping.done(), 'cleanup returned while heartbeat SQLite connection remained open')
            self.assertFalse(closed.is_set())
            self.assertIn(task['id'], self.manager._runs)
            release.set()
            await asyncio.wait_for(asyncio.shield(stopping), 10)
            self.assertTrue(closed.is_set())
            self.assertNotIn(task['id'], self.manager._runs)
            self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        finally:
            release.set()
            if stopping is not None:
                await asyncio.wait_for(asyncio.shield(stopping), 10)
            await asyncio.to_thread(closed.wait, 5)

    async def test_stop_joins_heartbeat_sqlite_io_before_retiring_database(self):
        await self.check_heartbeat_connection_drain()

    async def test_shutdown_joins_heartbeat_sqlite_io_even_after_repeated_cancel(self):
        await self.check_heartbeat_connection_drain(shutdown=True)


if __name__ == "__main__":
    unittest.main()
