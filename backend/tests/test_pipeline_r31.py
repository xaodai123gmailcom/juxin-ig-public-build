"""Long-running candidate queue progress and monotonic live checkpoints."""
from __future__ import annotations

import asyncio
import contextvars
import json
import sys
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import test_parallel_relation_pipeline as pipeline
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.errors import ConflictError


async def wait_for_candidate_progress(awaitable, progress, *, stalled_after=15):
    """Bound inactivity, not the disk-dependent duration of 155 durable claims.

    Only an increasing count of distinct screened candidates renews the budget.
    The test separately bounds query materialization, attempts and transactions;
    repeating a callback or timer cannot keep a stuck pipeline alive.
    """
    operation = asyncio.create_task(awaitable)
    loop = asyncio.get_running_loop()
    observed = progress()
    deadline = loop.time() + stalled_after
    try:
        while not operation.done():
            await asyncio.wait({operation}, timeout=min(.05, stalled_after / 4))
            current = progress()
            if current > observed:
                observed = current
                deadline = loop.time() + stalled_after
            if not operation.done() and loop.time() >= deadline:
                raise TimeoutError('candidate pipeline made no durable candidate progress')
        return await operation
    finally:
        if not operation.done():operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


class PipelineR31Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    def _cleanup_runtime(self, *, targets=None, live_queue=False, block_second=False):
        entered = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()

        class Browser:
            def close_profile(self, _profile_id):
                # The synthetic provider completes its close synchronously.
                return {"closed": True}

        class Worker:
            async def connect(self, *_args, **_kwargs):
                pass

            async def disconnect(self):
                entered.set()
                await release.wait()

        class Manager(pipeline._PipelineManager):
            async def _execute_candidate_spooled_mode(self, _control, _worker, target, *_args, **_kwargs):
                if block_second and target["username"] == "second_source":
                    second_started.set()
                    await second_release.wait()
                return {"skipped_posts": [], "total": 0, "source_total": 0,
                        "recorded": 0, "deduped": 0, "pending": 0, "discovery_complete": True}

        task = self.service.create_task(self.user["id"], name="real cleanup runtime", modes=["followers"],
            targets=targets or ["source_one"], window_ids=["window-one"],
            settings={"local_person_recognition": False, "live_queue_enabled": live_queue})
        manager = Manager(self.service, Browser(), worker_factory=lambda _: Worker())
        return manager, task, entered, release, second_started, second_release

    async def test_real_cleanup_target_stays_in_bounded_live_projection_until_disconnect_finishes(self):
        manager, task, entered, release, _, _ = self._cleanup_runtime()
        target = task["targets"][0]
        control = None
        try:
            await manager.start(self.user["id"], task["id"])
            control = manager._runs[task["id"]]
            await asyncio.wait_for(entered.wait(), 3)
            with self.service.database.write() as connection:
                connection.executemany(
                    """INSERT INTO task_targets(id,task_id,username_norm,username_display,
                       queue_order,status,current_window_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,'completed','other-window',?,?)""",
                    [(str(uuid.uuid4()), task["id"], f"new_history_{i}", f"new_history_{i}", i + 2,
                      "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00") for i in range(201)],
                )
            runtime = await manager.runtime_diagnostics(self.user["id"], task["id"])
            profile = runtime["profile_states"][0]
            self.assertEqual(target["id"], profile["current_target_id"])
            self.assertEqual("disconnecting", profile["current_stage"])
            current_ids = [state["current_target_id"] for state in runtime["profile_states"] if state.get("current_target_id")]
            live = self.service.get_task_live_status(self.user["id"], task["id"], current_target_ids=current_ids)
            self.assertIn(target["id"], {item["id"] for item in live["targets"]})
            self.assertEqual(1, len(self.service.list_browser_lease_states(self.user["id"])))
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.user["id"], "window-one",
                    operation_type="monitor", entity_id="must_wait_for_cleanup")
        finally:
            release.set()
            if control is not None:
                await asyncio.gather(control.coordinator, return_exceptions=True)
            await manager.shutdown()
        self.assertIsNone(control.profile_states["window-one"]["current_target_id"])
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_idle_live_queue_does_not_restore_an_old_target_during_later_stop(self):
        manager, task, entered, release, _, _ = self._cleanup_runtime(
            live_queue=True, targets=["source_one", "pending_source"])
        original_execute = manager._execute_candidate_spooled_mode

        async def finish_then_lock(*args, **kwargs):
            result = await original_execute(*args, **kwargs)
            # A completed, empty window now closes. Retain the next owned target
            # behind a dispatch lock to exercise a genuine idle worker's stop.
            self.service.set_split_claim_locked(self.user["id"], True)
            return result

        manager._execute_candidate_spooled_mode = finish_then_lock
        stopping = None
        try:
            await manager.start(self.user["id"], task["id"])
            control = manager._runs[task["id"]]
            async with asyncio.timeout(3):
                while self.service.get_task(self.user["id"], task["id"])["targets"][0]["status"] != "completed":
                    await asyncio.sleep(0)
            # A background commit becomes visible before its event-loop callback.
            # Wait for the actual dispatch-locked idle boundary this test exercises.
            async with asyncio.timeout(3):
                while control.profile_states["window-one"].get("reason") != "collection_dispatch_locked":
                    await asyncio.sleep(.005)
            self.assertEqual("pending", self.service.get_task(
                self.user["id"], task["id"])["targets"][1]["status"])
            self.assertEqual("idle", control.profile_states["window-one"]["state"])
            self.assertIsNone(control.profile_states["window-one"]["current_target_id"])
            self.assertFalse(entered.is_set())
            stopping = asyncio.create_task(manager.stop(self.user["id"], task["id"], close_windows=False))
            await asyncio.wait_for(entered.wait(), 3)
            self.assertIsNone(control.profile_states["window-one"]["current_target_id"])
        finally:
            release.set()
            if stopping:
                await asyncio.gather(stopping, return_exceptions=True)
            await manager.shutdown()
            self.service.set_split_claim_locked(self.user["id"], False)

    async def test_next_target_binding_is_not_replaced_by_previous_cleanup_metadata(self):
        manager, task, entered, release, second_started, second_release = self._cleanup_runtime(
            targets=["first_source", "second_source"], block_second=True)
        try:
            await manager.start(self.user["id"], task["id"])
            await asyncio.wait_for(second_started.wait(), 3)
            control = manager._runs[task["id"]]
            self.assertEqual(task["targets"][1]["id"], control.profile_states["window-one"]["current_target_id"])
            self.assertFalse(entered.is_set())
            with self.assertRaises(ConflictError):
                await manager.pause_window(self.user["id"], task["id"], "window-one",
                    expected_target_id=task["targets"][0]["id"])
            self.assertTrue(control.profile_pause_events["window-one"].is_set())
        finally:
            second_release.set()
            release.set()
            await manager.shutdown()

    async def test_failed_prefix_does_not_get_rescanned_for_every_later_candidate(self):
        task, target, control = self._task_and_control(1)
        names = [f"bad_{index:03}" for index in range(150)] + [f"healthy_{index}" for index in range(5)]
        for offset in range(0, len(names), 100):
            self.service.append_task_mode_candidates(
                self.user["id"], task["id"], target["id"], "followers", names[offset:offset + 100])
        original = self.service.list_pending_task_mode_candidates
        original_write = self.service.database.write
        original_checkpoint = self.service.upsert_checkpoint
        rows_returned = []
        attempted = set()
        transactions = 0
        checkpoint_writes = 0

        @contextmanager
        def counted_write():
            nonlocal transactions
            with original_write() as connection:
                transactions += 1
                self.assertLessEqual(transactions, len(names) * 2 + 10,
                                     'unchanged failed rows must not amplify durable writes')
                self.assertEqual(2, connection.execute('PRAGMA synchronous').fetchone()[0])
                yield connection

        def checkpoint(*args, **kwargs):
            nonlocal checkpoint_writes
            checkpoint_writes += 1
            return original_checkpoint(*args, **kwargs)

        def pending(*args, **kwargs):
            result = original(*args, **kwargs)
            rows_returned.append(len(result))
            self.assertLessEqual(len(rows_returned), len(names) + 2)
            self.assertLessEqual(sum(rows_returned), len(names) * 2)
            return result

        self.service.list_pending_task_mode_candidates = pending
        self.service.database.write = counted_write
        self.service.upsert_checkpoint = checkpoint
        state = pipeline._PipelineState()
        case = self

        class Parent(pipeline._RelationParent):
            async def screen(self, username):
                case.assertNotIn(username, attempted, 'a failed row must not be retried in the same drain')
                attempted.add(username)  # Its production identity claim has committed.
                if username.startswith("bad_"):
                    raise PlaywrightWorker._page_recovery_exhausted(
                        WorkerExecutionError("still loading", reason="instagram_profile_not_ready"), username)
                await super().screen(username)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        try:
            with self.assertRaises(WorkerExecutionError):
                await wait_for_candidate_progress(manager._execute_candidate_spooled_mode(
                    control, Parent(state), target, "followers", task["settings"],
                    {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}),
                    lambda: len(attempted))
        finally:
            self.service.list_pending_task_mode_candidates = original
            self.service.database.write = original_write
            self.service.upsert_checkpoint = original_checkpoint
        stats = self.service.task_mode_candidate_stats(self.user["id"], task["id"], target["id"], "followers")
        self.assertEqual(150, stats["pending"])
        self.assertEqual(5, stats["recorded"])
        print(json.dumps({"case": "failed_prefix", "candidates": len(names),
            "candidate_queries": len(rows_returned), "rows_materialized": sum(rows_returned),
            "write_transactions": transactions, "checkpoint_writes": checkpoint_writes,
            "pending": stats["pending"], "recorded": stats["recorded"]}))
        self.assertEqual(set(names), attempted)
        self.assertEqual(2, checkpoint_writes, 'first failure and final error boundary must both be saved')
        self.assertLessEqual(sum(rows_returned), len(names) * 2,
                             f"candidate queries={len(rows_returned)}, rows materialized={sum(rows_returned)}")

    async def test_candidate_progress_watchdog_rejects_real_stall_and_joins_cleanup(self):
        cleaned = asyncio.Event()
        async def stalled():
            try:await asyncio.Event().wait()
            finally:cleaned.set()
        with self.assertRaisesRegex(TimeoutError, 'no durable candidate progress'):
            await wait_for_candidate_progress(stalled(), lambda: 1, stalled_after=.05)
        self.assertTrue(cleaned.is_set())

    async def test_candidate_progress_watchdog_allows_bounded_continuing_work(self):
        completed = 0
        async def advancing():
            nonlocal completed
            for _ in range(6):
                await asyncio.sleep(.03)
                completed += 1
            return completed
        self.assertEqual(6, await wait_for_candidate_progress(advancing(), lambda: completed, stalled_after=.1))

    async def test_deferred_error_checkpoints_keep_changed_stats_and_final_boundary(self):
        task, target, control = self._task_and_control(1)
        names = ['bad_a', 'bad_b', 'healthy_a', 'bad_c', 'bad_d', 'healthy_b']
        self.service.append_task_mode_candidates(self.user['id'], task['id'], target['id'], 'followers', names)
        saved = []
        original = self.service.upsert_checkpoint
        def checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            saved.append(dict(kwargs['counters']))
            return result
        self.service.upsert_checkpoint = checkpoint
        state = pipeline._PipelineState()
        attempted = set()
        class Parent(pipeline._RelationParent):
            async def screen(self, username):
                attempted.add(username)
                if username.startswith('bad_'):
                    raise PlaywrightWorker._page_recovery_exhausted(
                        WorkerExecutionError('still loading', reason='instagram_profile_not_ready'), username)
                await super().screen(username)
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        try:
            with self.assertRaises(WorkerExecutionError):
                await wait_for_candidate_progress(manager._execute_candidate_spooled_mode(
                    control, Parent(state), target, 'followers', task['settings'],
                    {'cursor': {'candidate_spool_complete': True, 'candidate_spool_natural_end': True}}),
                    lambda: len(attempted))
        finally:
            self.service.upsert_checkpoint = original
        self.assertEqual([0, 1, 2], [row['saved'] for row in saved])
        self.assertEqual([6, 5, 4], [row['pending_candidates'] for row in saved])
        latest = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
        self.assertTrue(latest['cursor']['candidate_spool_natural_end'])
        self.assertEqual(2, latest['counters']['saved'])

    async def test_late_source_snapshot_cannot_overwrite_newer_screened_count(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        state.allow_screen.set()
        from_source = contextvars.ContextVar("source_checkpoint_test", default=False)
        old_source_read = asyncio.Event()
        screening_saved = asyncio.Event()
        screening_processed = asyncio.Event()
        source_sink_returned = asyncio.Event()
        release_read = threading.Event()
        original_stats = self.service.task_mode_candidate_stats
        original_save = self.service.upsert_checkpoint
        original_finish = self.service.finish_task_mode_candidate
        loop = asyncio.get_running_loop()
        blocked = False
        written_processed = []

        def delayed_stats(*args, **kwargs):
            nonlocal blocked
            result = original_stats(*args, **kwargs)
            if from_source.get() and result["total"] == 12 and not blocked:
                blocked = True
                loop.call_soon_threadsafe(old_source_read.set)
                if not release_read.wait(5):
                    raise RuntimeError("test did not release old source snapshot")
            return result

        def observe_save(*args, **kwargs):
            result = original_save(*args, **kwargs)
            processed = kwargs.get("counters", {}).get("processed", 0)
            written_processed.append(processed)
            if processed >= 10:
                loop.call_soon_threadsafe(screening_saved.set)
            return result

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            if args[4] == "profile_9":
                loop.call_soon_threadsafe(screening_processed.set)
            return result

        self.service.task_mode_candidate_stats = delayed_stats
        self.service.upsert_checkpoint = observe_save
        self.service.finish_task_mode_candidate = finish

        class Parent(pipeline._RelationParent):
            async def collect_followers(self, *_args, **kwargs):
                token = from_source.set(True)
                try:
                    await kwargs["candidate_sink"]([f"profile_{index}" for index in range(12)])
                    source_sink_returned.set()
                    await asyncio.Event().wait()
                    return CollectionOutcome("followers", [], source_total=12)
                finally:
                    from_source.reset(token)

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=pipeline._ScreeningChild(state)), target,
            "followers", task["settings"], None))
        try:
            await asyncio.wait_for(old_source_read.wait(), 3)
            await asyncio.wait_for(screening_processed.wait(), 3)
            release_read.set()
            await asyncio.wait_for(source_sink_returned.wait(), 3)
            await asyncio.wait_for(screening_saved.wait(), 3)
            after = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
            print(json.dumps({"case": "checkpoint_order", "processed_writes": written_processed,
                "current_processed": after["counters"]["processed"]}))
            self.assertEqual(sorted(written_processed), written_processed,
                             f"checkpoint processed sequence: {written_processed}")
            self.assertGreaterEqual(after["counters"]["processed"], 10)
        finally:
            release_read.set()
            control.stop_event.set()
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            self.service.task_mode_candidate_stats = original_stats
            self.service.upsert_checkpoint = original_save
            self.service.finish_task_mode_candidate = original_finish

    async def test_released_earlier_sibling_reservation_is_revisited_on_next_drain(self):
        task, target, control = self._task_and_control(1)
        self.service.append_task_mode_candidates(self.user["id"], task["id"], target["id"],
            "followers", ["leased_early", "healthy_later"])
        reservations = {"leased_early"}
        reservation_lock = asyncio.Lock()
        state = pipeline._PipelineState()

        class Parent(pipeline._RelationParent):
            async def screen(self, username):
                await super().screen(username)
                if username == "healthy_later":
                    async with reservation_lock:
                        reservations.discard("leased_early")

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        parent = Parent(state)
        arguments = dict(discovery_complete=True,
            candidate_reservations=reservations, candidate_reservation_lock=reservation_lock)
        first = await manager._drain_candidate_spool(
            control, parent, target["id"], "followers", task["settings"], **arguments)
        self.assertEqual(1, first["pending"])
        second = await manager._drain_candidate_spool(
            control, parent, target["id"], "followers", task["settings"], **arguments)
        self.assertEqual(0, second["pending"])
        self.assertEqual([("parent", "healthy_later"), ("parent", "leased_early")], state.screened)


if __name__ == "__main__":
    unittest.main()
