"""A drained source releases its window only after screening and provider close."""
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
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.native_browser import BrowserHub
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from app.service import CoreService
from test_core import FakeCollectionWorker


class CloseProvider:
    def __init__(self):
        self.closed = []
        self.result = {"closed": True}
        self.error = None
        self.on_close = None

    def close_profile(self, profile_id):
        self.closed.append(profile_id)
        if self.on_close:
            self.on_close(profile_id)
        if self.error:
            raise self.error
        return self.result


class CollectionDrainR56Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "drain.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("drain-r56", "test password long enough")["id"]
        self.managers = []
        self.release_gates = []

    async def asyncTearDown(self):
        for gate in self.release_gates:
            gate.set()
        for manager in self.managers:
            await asyncio.wait_for(manager.shutdown(), 10)
        self.temp.cleanup()

    def task(self, sources=("source",), windows=("window-a",), **settings):
        return self.service.create_task(self.owner, name="drain", modes=["followers"],
            targets=list(sources), window_ids=list(windows), settings={
                "live_queue_enabled": True, "local_person_recognition": False,
                "location_enabled": False, **settings,
            })

    def manager(self, worker=FakeCollectionWorker, provider=None, manager_type=ExecutionManager):
        provider = provider or CloseProvider()
        manager = manager_type(self.service, provider, worker_factory=worker,
                              lease_heartbeat_interval_seconds=.03)
        self.managers.append(manager)
        return manager, provider

    def gate(self, threaded=False):
        gate = threading.Event() if threaded else asyncio.Event()
        self.release_gates.append(gate)
        return gate

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 10)

    def leases(self):
        return {row["profile_id"]: row for row in self.service.list_browser_lease_states(self.owner)}

    async def test_final_source_closes_after_durable_completion_and_disconnect(self):
        task = self.task()
        disconnected = asyncio.Event()

        class Worker(FakeCollectionWorker):
            async def disconnect(self):
                disconnected.set()

        manager, provider = self.manager(Worker)
        def inspect_close(profile_id):
            self.assertTrue(disconnected.is_set())
            current = self.service.get_task(self.owner, task["id"])
            self.assertEqual("completed", current["targets"][0]["status"])
            self.assertEqual("completed_archived", current["targets"][0]["current_stage"])
            self.assertEqual(0, self.service.task_mode_candidate_stats(
                self.owner, task["id"], task["targets"][0]["id"], "followers")["pending"])
            self.assertIn(profile_id, self.leases())
        provider.on_close = inspect_close
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({}, self.leases())
        current = self.service.get_task(self.owner, task["id"])
        self.assertEqual("completed", current["status"])
        self.assertEqual("window-a", current["targets"][0]["current_window_id"])

    async def test_locked_task_owned_pending_target_keeps_its_window_until_resumed(self):
        task = self.task()
        self.service.set_split_claim_locked(self.owner, True)
        manager, provider = self.manager()
        await manager.start(self.owner, task["id"])
        control = manager._runs[task["id"]]
        await self.until(lambda: control.profile_states.get("window-a", {}).get("reason") == "collection_dispatch_locked")
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())
        self.service.set_split_claim_locked(self.owner, False)
        manager.notify_split_queue(self.owner)
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], provider.closed)

    async def test_released_window_can_be_explicitly_rejoined_without_stealing_new_owner(self):
        entered, release = asyncio.Event(), self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, **kwargs):
                if target == "busy_source":
                    entered.set()
                    await release.wait()
                return await super().collect_followers(target, **kwargs)
        self.service.upsert_manual_split_candidates(self.owner,
            [{"username": "busy_source", "queued": True, "allowed_window_ids": ["window-b"]}])
        task = self.task((), ("window-a", "window-b"))
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(entered.wait(), 10)
        control = manager._runs[task["id"]]
        await self.until(lambda: "window-a" not in control.leases and "window-a" not in control.started_profile_ids)
        own_token = self.service.acquire_browser_lease(self.owner, "window-a", operation_type="account", entity_id="other")
        with self.assertRaises(ConflictError):
            await manager.add_windows(self.owner, task["id"], ["window-a"])
        self.assertEqual(["window-a"], provider.closed)
        self.service.release_browser_lease("window-a", own_token)
        self.service.upsert_manual_split_candidates(self.owner,
            [{"username": "new_source", "queued": True, "allowed_window_ids": ["window-a"]}])
        added = await manager.add_windows(self.owner, task["id"], ["window-a"])
        self.assertEqual(1, added["added"])
        await self.until(lambda: provider.closed == ["window-a", "window-a"] and "window-a" not in control.leases)
        self.assertIn("window-b", self.leases())
        self.assertNotIn("window-b", provider.closed)

    async def test_next_eligible_source_runs_before_single_close(self):
        task = self.task(("first_source", "next_source"))
        collected = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, **kwargs):
                collected.append(target)
                return await super().collect_followers(target, **kwargs)
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["first_source", "next_source"], collected)
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({}, self.leases())

    async def test_source_end_keeps_pending_consumers_and_child_cleanup_owned(self):
        source_done, profiles_entered, cleanup_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        profiles_release, cleanup_release = self.gate(), self.gate()
        screened = []
        class Child(FakeCollectionWorker):
            async def read_visible_profile(self, username, **kwargs):
                profiles_entered.set()
                await profiles_release.wait()
                screened.append(username)
                return await super().read_visible_profile(username, **kwargs)
            async def disconnect(self):
                cleanup_entered.set()
                await cleanup_release.wait()
        class Worker(FakeCollectionWorker):
            supports_parallel_screening_tab = True
            supports_candidate_batch_sink = True
            async def create_parallel_screening_worker(self):
                return Child(None)
            async def collect_followers(self, target, *, candidate_sink, **kwargs):
                await candidate_sink(["fan_one", "fan_two", "fan_three"])
                source_done.set()
                return CollectionOutcome("followers", [], source_total=3)
        task = self.task(parallel_screening_workers=2)
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(source_done.wait(), 10)
        await asyncio.wait_for(profiles_entered.wait(), 10)
        stats = self.service.task_mode_candidate_stats(self.owner, task["id"], task["targets"][0]["id"], "followers")
        self.assertEqual(3, stats["pending"])
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())
        profiles_release.set()
        await asyncio.wait_for(cleanup_entered.wait(), 10)
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())
        cleanup_release.set()
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual({"fan_one", "fan_two", "fan_three"}, set(screened))
        self.assertEqual(3, len(self.service.list_results(self.owner, task["id"])))
        self.assertEqual(["window-a"], provider.closed)

    async def test_idle_affinity_window_closes_without_touching_busy_sibling_or_other_task(self):
        busy, other_busy = asyncio.Event(), asyncio.Event()
        release, other_release = self.gate(), self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, **kwargs):
                if target == "busy_source":
                    busy.set()
                    await release.wait()
                else:
                    other_busy.set()
                    await other_release.wait()
                return await super().collect_followers(target, **kwargs)
        rows = self.service.upsert_manual_split_candidates(self.owner,
            [{"username": "busy_source", "queued": True, "allowed_window_ids": ["window-b"]}])
        task = self.task((), ("window-a", "window-b"))
        other = self.task(("other_source",), ("window-c",))
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, other["id"])
        await asyncio.wait_for(other_busy.wait(), 10)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(busy.wait(), 10)
        await self.until(lambda: "window-a" not in self.leases())
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({"window-b", "window-c"}, set(self.leases()))
        self.assertEqual("claimed", next(row for row in self.service.list_split_candidates(self.owner)
            if row["id"] == rows[0]["id"])["queue_state"])
        release.set()
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual({"window-c"}, set(self.leases()))
        self.assertNotIn("window-c", provider.closed)

    async def test_global_and_individual_locked_next_sources_do_not_keep_window_open(self):
        for global_lock in (True, False):
            with self.subTest(global_lock=global_lock):
                entered, release = asyncio.Event(), self.gate()
                class Worker(FakeCollectionWorker):
                    async def collect_followers(self, target, **kwargs):
                        entered.set()
                        await release.wait()
                        return await super().collect_followers(target, **kwargs)
                suffix = "global" if global_lock else "individual"
                task = self.task((f"first_{suffix}",), (f"window-{suffix}",))
                manager, provider = self.manager(Worker)
                await manager.start(self.owner, task["id"])
                await asyncio.wait_for(entered.wait(), 10)
                waiting = self.service.upsert_manual_split_candidates(self.owner,
                    [{"username": f"locked_{suffix}", "queued": True}])[0]
                if global_lock:
                    self.service.set_split_claim_locked(self.owner, True)
                else:
                    self.service.set_split_candidate_locked(self.owner, waiting["id"], True)
                manager.notify_split_queue(self.owner)
                release.set()
                await asyncio.wait_for(manager.wait(task["id"]), 10)
                self.assertEqual([f"window-{suffix}"], provider.closed)
                self.assertEqual({}, self.leases())
                remaining = next(row for row in self.service.list_split_candidates(self.owner)
                                 if row["id"] == waiting["id"])
                self.assertEqual("queued", remaining["queue_state"])
                self.service.set_split_claim_locked(self.owner, False)
                self.service.set_split_candidate_locked(self.owner, waiting["id"], True)

    async def test_pause_retains_current_target_even_after_producer_returns(self):
        entered, release = asyncio.Event(), self.gate()
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, **kwargs):
                entered.set()
                await release.wait()
                return await super().collect_followers(target, **kwargs)
        task = self.task()
        manager, provider = self.manager(Worker)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(entered.wait(), 10)
        await manager.pause_window(self.owner, task["id"], "window-a")
        release.set()
        await asyncio.sleep(.04)
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())
        await manager.resume_window(self.owner, task["id"], "window-a")
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], provider.closed)

    async def test_failed_and_incomplete_sources_are_never_auto_closed(self):
        class IncompleteManager(ExecutionManager):
            async def _execute_candidate_spooled_mode(self, *args, **kwargs):
                return {"total": 1, "recorded": 1, "deduped": 0, "pending": 0,
                        "source_total": 1, "skipped_posts": [], "discovery_complete": False}
        task = self.task()
        manager, provider = self.manager(manager_type=IncompleteManager)
        await manager.start(self.owner, task["id"])
        await self.until(lambda: self.service.get_task(self.owner, task["id"])["targets"][0]["status"] == "recoverable"
                         and not manager._runs[task["id"]].worker_tasks)
        current = self.service.get_task(self.owner, task["id"])
        self.assertEqual("recoverable", current["targets"][0]["status"])
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())

    async def test_incomplete_source_retried_in_same_worker_closes_when_it_finishes(self):
        class RetryManager(ExecutionManager):
            attempts = 0
            async def _execute_candidate_spooled_mode(self, *args, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise WorkerExecutionError("source unavailable", reason="source_unavailable", pause_required=False)
                return await super()._execute_candidate_spooled_mode(*args, **kwargs)
        task = self.task()
        manager, provider = self.manager(manager_type=RetryManager)
        await manager.start(self.owner, task["id"])
        await self.until(lambda: self.service.get_task(self.owner, task["id"])["targets"][0]["status"] == "recoverable")
        control = manager._runs[task["id"]]
        worker = control.profile_worker_tasks["window-a"]
        self.assertFalse(worker.done())
        self.assertEqual([], provider.closed)
        self.assertIn("window-a", self.leases())
        await manager.retry_target(self.owner, task["id"], task["targets"][0]["id"])
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(2, manager.attempts)
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({}, self.leases())

    async def test_late_source_during_close_is_preserved_and_never_reopens_closing_worker(self):
        entered, release = threading.Event(), self.gate(threaded=True)
        collected = []
        class Worker(FakeCollectionWorker):
            async def collect_followers(self, target, **kwargs):
                collected.append(target)
                return await super().collect_followers(target, **kwargs)
        task = self.task()
        manager, provider = self.manager(Worker)
        def blocked_close(_profile):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test close not released")
        provider.on_close = blocked_close
        await manager.start(self.owner, task["id"])
        self.assertTrue(await asyncio.to_thread(entered.wait, 10))
        added = await manager.add_targets(self.owner, task["id"], ["late_source"])
        self.assertEqual(1, added["added"])
        release.set()
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        current = self.service.get_task(self.owner, task["id"])
        self.assertEqual(["source"], collected)
        self.assertEqual("recoverable", current["status"])
        self.assertEqual("pending", next(target for target in current["targets"] if target["username"] == "late_source")["status"])
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({}, self.leases())
        # A source arriving during close has a durable explicit restart route.
        self.assertEqual(["window-a"], current["window_ids"])
        late = next(target for target in current["targets"] if target["username"] == "late_source")
        provider.on_close = None
        await manager.retry_target(self.owner, task["id"], late["id"])
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["source", "late_source"], collected)
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])
        self.assertEqual(["window-a", "window-a"], provider.closed)

    async def test_close_exception_and_negative_ack_retain_lease_and_retry_close_only(self):
        for failure, suffix in (
            (RuntimeError("provider unavailable"), "exception"),
            ({"provider_success": False, "window_state": "closing"}, "deferred"),
            ({"provider_success": True, "window_state": "closing"}, "unfinished"),
        ):
            with self.subTest(failure=failure):
                task = self.task((f"source_{suffix}",), (f"window-{suffix}",))
                collected = []
                class Worker(FakeCollectionWorker):
                    async def collect_followers(self, target, **kwargs):
                        collected.append(target)
                        return await super().collect_followers(target, **kwargs)
                manager, provider = self.manager(Worker)
                if isinstance(failure, Exception):
                    provider.error = failure
                else:
                    provider.result = failure
                await manager.start(self.owner, task["id"])
                control = manager._runs[task["id"]]
                await self.until(lambda: control.profile_states.get(f"window-{suffix}", {}).get("reason") == "browser_close_failed")
                await self.until(lambda: not control.worker_tasks)
                state = control.profile_states[f"window-{suffix}"]
                self.assertEqual("manual_required", state["state"])
                self.assertIsNone(state["current_target_id"])
                self.assertIn(f"window-{suffix}", self.leases())
                with self.assertRaises(ConflictError):
                    self.service.acquire_browser_lease(self.owner, f"window-{suffix}", operation_type="account", entity_id="unrelated")
                provider.error, provider.result = None, {"closed": True}
                result = await manager.resume_window(self.owner, task["id"], f"window-{suffix}")
                self.assertEqual("closed", result["status"])
                await asyncio.wait_for(manager.wait(task["id"]), 10)
                self.assertEqual([f"source_{suffix}"], collected)
                self.assertEqual([f"window-{suffix}"] * 2, provider.closed)
                self.assertEqual({}, self.leases())

    async def test_repeated_cancellation_waits_for_actual_provider_close(self):
        entered, release = threading.Event(), self.gate(threaded=True)
        task = self.task()
        manager, provider = self.manager()
        def blocked_close(_profile):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test close not released")
        provider.on_close = blocked_close
        await manager.start(self.owner, task["id"])
        control = manager._runs[task["id"]]
        self.assertTrue(await asyncio.to_thread(entered.wait, 10))
        worker = control.profile_worker_tasks["window-a"]
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(.04)
        self.assertFalse(worker.done())
        self.assertIn("window-a", self.leases())
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(self.owner, "window-a", operation_type="account", entity_id="unrelated")
        release.set()
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual(["window-a"], provider.closed)
        self.assertEqual({}, self.leases())

    async def test_auto_close_uses_original_provider_and_skips_foreign_token(self):
        native, legacy = CloseProvider(), CloseProvider()
        manager, _ = self.manager(provider=BrowserHub(native, legacy))
        # BrowserHub must keep native and legacy close paths isolated.
        for profile in ("native-owned", "legacy-owned"):
            token = self.service.acquire_browser_lease(self.owner, profile, operation_type="collection", entity_id="fixture")
            self.assertTrue(await manager._close_profiles({profile: token}))
            self.service.release_browser_lease(profile, token)
        token = self.service.acquire_browser_lease(self.owner, "foreign-window", operation_type="account", entity_id="other")
        self.assertFalse(await manager._close_profiles({"foreign-window": "stale-token"}))
        self.assertEqual(["native-owned"], native.closed)
        self.assertEqual(["legacy-owned"], legacy.closed)
        self.assertIn("foreign-window", self.leases())
        self.service.release_browser_lease("foreign-window", token)
