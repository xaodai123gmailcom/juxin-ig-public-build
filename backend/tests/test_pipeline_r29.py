"""Source completion, candidate isolation and cancelled child ownership on SQLite."""
from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import test_parallel_relation_pipeline as pipeline
from completion_wait import wait_for_collection_operation
from app.errors import ValidationError
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


def page_failure(username):
    return PlaywrightWorker._page_recovery_exhausted(
        WorkerExecutionError("profile still loading", reason="instagram_profile_not_ready"),
        username,
    )


class PipelineR29Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    def _checkpoint(self, task, target):
        return self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")

    def _stats(self, task, target):
        return self.service.task_mode_candidate_stats(self.user["id"], task["id"], target["id"], "followers")

    async def test_one_bad_child_does_not_block_healthy_candidates_or_lose_claim(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()

        class Child(pipeline._ScreeningChild):
            failures = True
            attempts = 0

            async def screen(self, username):
                self.state.screen_started.set()
                if username == "pipeline_a":
                    self.attempts += 1
                    if self.failures:
                        raise page_failure(username)
                await super().screen(username)

        class Parent(pipeline._RelationParent):
            collections = 0

            async def collect_followers(self, *args, **kwargs):
                self.collections += 1
                return await super().collect_followers(*args, **kwargs)

        child = Child(state)
        parent = Parent(state, child=child)
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        checkpoint = None
        for attempt in range(1, 4):
            if attempt == 2:
                self.service.append_task_mode_candidates(
                    self.user["id"], task["id"], target["id"], "followers", ["new_healthy"])
            with self.assertRaises(WorkerExecutionError) as caught:
                await wait_for_collection_operation(
                    lambda: manager._execute_candidate_spooled_mode(
                        control, parent, target,
                        "followers", task["settings"], checkpoint,
                    ),
                    manager, self.service, control.owner_user_id, control.task_id,
                )
            self.assertTrue(caught.exception.details["source_discovery_complete"])
            self.assertEqual(1, self._stats(task, target)["pending"])
            self.assertEqual(attempt, child.attempts, "one failed read per recovery attempt")
            self.assertEqual(0, child.disconnect_calls)
            self.assertIn("pipeline_a", parent._deferred_screening_workers)
            checkpoint = self._checkpoint(task, target)
            self.assertNotEqual("mode_completed", checkpoint["stage"])
            resumed_claim = self.service.claim_workbench_identity(
                self.user["id"], username="pipeline_a", source="followers",
                source_target=target["id"], allow_owned_resume=True)
            self.assertFalse(resumed_claim["duplicate"])
        self.assertEqual(["new_healthy", "pipeline_b", "pipeline_c"],
                         sorted(username for _, username in state.screened))
        child.failures = False
        result = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], checkpoint,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )
        self.assertEqual(0, result["pending"])
        self.assertEqual(4, result["recorded"])
        self.assertEqual(1, parent.collections)
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)

    async def test_more_than_one_page_of_bad_candidates_does_not_hide_healthy_tail(self):
        task, target, control = self._task_and_control(1)
        names = [f"bad_{index:03}" for index in range(105)] + ["healthy_tail"]
        for offset in range(0, len(names), 100):
            self.service.append_task_mode_candidates(
                self.user["id"], task["id"], target["id"], "followers", names[offset:offset + 100])
        state = pipeline._PipelineState()
        attempts = []

        class Parent(pipeline._RelationParent):
            async def screen(self, username):
                attempts.append(username)
                if username.startswith("bad_"):
                    raise page_failure(username)
                await super().screen(username)

        parent = Parent(state)
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        checkpoint = {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}
        with self.assertRaises(WorkerExecutionError):
            await wait_for_collection_operation(
                lambda: manager._execute_candidate_spooled_mode(
                    control, parent, target,
                    "followers", task["settings"], checkpoint,
                ),
                manager, self.service, control.owner_user_id, control.task_id,
            )
        self.assertEqual(names, attempts, "each failure must be deferred, not retried in a tight loop")
        self.assertEqual([("parent", "healthy_tail")], state.screened)
        self.assertEqual(105, self._stats(task, target)["pending"])
        self.assertEqual(1, self._stats(task, target)["recorded"])

    async def test_auth_limit_transport_and_unknown_errors_still_stop_owning_pipeline(self):
        for reason in ("instagram_login_required", "instagram_challenge", "instagram_rate_limited",
                       "instagram_action_blocked", "browser_disconnected", "unknown"):
            with self.subTest(reason=reason):
                task, target, control = self._task_and_control(1)
                guarded_name = f"guard_{reason}"[:30]
                healthy_name = f"tail_{reason}"[:30]
                self.service.append_task_mode_candidates(
                    self.user["id"], task["id"], target["id"], "followers", [guarded_name, healthy_name])
                state = pipeline._PipelineState()
                attempts = []

                class Parent(pipeline._RelationParent):
                    async def screen(self, username):
                        attempts.append(username)
                        raise WorkerExecutionError("guard", reason=reason)

                manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
                with self.assertRaises(WorkerExecutionError) as caught:
                    await manager._execute_candidate_spooled_mode(control, Parent(state), target,
                        "followers", task["settings"],
                        {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}})
                self.assertEqual(reason, caught.exception.code)
                self.assertEqual([guarded_name], attempts)
                self.assertEqual(2, self._stats(task, target)["pending"])

    async def test_natural_source_end_is_durable_before_screening_finishes_and_after_stop(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        child = pipeline._ScreeningChild(state, block=True)
        parent = pipeline._RelationParent(state, child=child)
        saved_complete = asyncio.Event()

        class Manager(pipeline._PipelineManager):
            async def _save_candidate_progress_checkpoint(self, *args, **kwargs):
                await super()._save_candidate_progress_checkpoint(*args, **kwargs)
                if kwargs.get("discovery_complete"):
                    saved_complete.set()

        manager = Manager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(saved_complete.wait(), 3)
            self.assertFalse(execution.done())
            self.assertTrue(manager._candidate_spool_complete(self._checkpoint(task, target), require_natural_end=True))
        finally:
            control.stop_event.set()
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(manager._candidate_spool_complete(self._checkpoint(task, target), require_natural_end=True))
        self.assertEqual(3, self._stats(task, target)["pending"])

    async def test_repeated_cancel_during_source_commit_waits_and_preserves_natural_end(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        parent = pipeline._RelationParent(state, child=pipeline._ScreeningChild(state, block=True))
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        write_started = asyncio.Event()
        release_write = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.service.upsert_checkpoint
        blocked = False

        def delayed(*args, **kwargs):
            nonlocal blocked
            if kwargs.get("cursor", {}).get("candidate_spool_complete") and not blocked:
                blocked = True
                loop.call_soon_threadsafe(write_started.set)
                if not release_write.wait(5):
                    raise RuntimeError("test did not release checkpoint writer")
            return original(*args, **kwargs)

        self.service.upsert_checkpoint = delayed
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(write_started.wait(), 3)
            control.stop_event.set()
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done(), "the owner cannot exit before its SQLite write")
        finally:
            release_write.set()
            await asyncio.gather(execution, return_exceptions=True)
            self.service.upsert_checkpoint = original
        self.assertTrue(manager._candidate_spool_complete(self._checkpoint(task, target), require_natural_end=True))

    async def test_screening_checkpoints_cannot_erase_a_completed_source(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        state.candidates = [f"profile_{index}" for index in range(12)]
        written_flags = []

        class Manager(pipeline._PipelineManager):
            async def _save_candidate_progress_checkpoint(self, *args, **kwargs):
                await super()._save_candidate_progress_checkpoint(*args, **kwargs)
                written_flags.append(kwargs["discovery_complete"])

        manager = Manager(self.service, pipeline._NoopBitBrowser())
        parent = pipeline._RelationParent(state, child=pipeline._ScreeningChild(state))
        result = await wait_for_collection_operation(
            lambda: manager._execute_candidate_spooled_mode(
                control, parent, target,
                "followers", task["settings"], None,
            ),
            manager, self.service, control.owner_user_id, control.task_id,
        )
        self.assertEqual(0, result["pending"])
        first_complete = written_flags.index(True)
        self.assertTrue(all(written_flags[first_complete:]))
        self.assertGreaterEqual(len(written_flags[first_complete:]), 2)

    async def test_cancelled_child_creation_joins_late_child_before_releasing_owner(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()
        child = pipeline._ScreeningChild(state)
        creation_started = asyncio.Event()
        creation_cancelled = asyncio.Event()
        release_creation = asyncio.Event()

        class Parent(pipeline._RelationParent):
            async def create_parallel_screening_worker(self):
                creation_started.set()
                try:
                    await release_creation.wait()
                except asyncio.CancelledError:
                    creation_cancelled.set()
                    await release_creation.wait()
                return child

        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Parent(state, child=child), target, "followers", task["settings"], None))
        try:
            await asyncio.wait_for(creation_started.wait(), 3)
            control.stop_event.set()
            execution.cancel()
            await asyncio.wait_for(creation_cancelled.wait(), 3)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done(), "the late page is still owned until cleanup")
        finally:
            release_creation.set()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertEqual(1, child.disconnect_calls)
        self.assertFalse(state.source_started.is_set())

    async def test_external_result_reconciliation_closes_only_this_targets_terminal_held_page(self):
        task, target, control = self._task_and_control(1)
        state = pipeline._PipelineState()

        class Child(pipeline._ScreeningChild):
            attempts = 0

            async def screen(self, username):
                self.state.screen_started.set()
                if username == "pipeline_a":
                    self.attempts += 1
                    raise page_failure(username)
                await super().screen(username)

        child = Child(state)
        parent = pipeline._RelationParent(state, child=child)
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        with self.assertRaises(WorkerExecutionError):
            await manager._execute_candidate_spooled_mode(
                control, parent, target, "followers", task["settings"], None)
        self.assertEqual(1, self._stats(task, target)["pending"])
        self.assertEqual(0, child.disconnect_calls, "a pending page stays owned")
        claim = self.service.claim_workbench_identity(
            self.user["id"], username="pipeline_a", source="followers",
            source_target=target["id"], allow_owned_resume=True)
        # Simulate the durable result/identity repair arriving after the page failed,
        # without the original caller getting to finish_task_mode_candidate().
        self.service.record_result(
            self.user["id"], task["id"], target["id"], username="pipeline_a",
            instagram_user_id="9001234", source_mode="followers", visibility="private",
            profile={"username": "pipeline_a", "visibility": "private"},
            screening={"person_recognition": {"checked": True, "category": "female",
                "confidence": 0.99, "source": "local_openvino"}},
            qualified=True, dedupe_claim_id=claim["claim_id"],
        )
        other_task, other_target, _ = self._task_and_control(1)
        self.service.append_task_mode_candidates(self.user["id"], other_task["id"],
            other_target["id"], "followers", ["other_target_terminal", "other_target_pending"])
        self.service.finish_task_mode_candidate(self.user["id"], other_task["id"],
            other_target["id"], "followers", "other_target_terminal", state="deduped")
        foreign_terminal = pipeline._ScreeningChild(state)
        foreign_pending = pipeline._ScreeningChild(state)
        parent._deferred_screening_workers.update(
            other_target_terminal=foreign_terminal, other_target_pending=foreign_pending)
        result = await manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], self._checkpoint(task, target))
        self.assertEqual(0, result["pending"])
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual(1, child.attempts, "a terminal row must never reopen the failed page")
        self.assertNotIn("pipeline_a", parent._deferred_screening_workers)
        self.assertEqual(0, foreign_terminal.disconnect_calls)
        self.assertEqual(0, foreign_pending.disconnect_calls)
        self.assertEqual({"other_target_terminal", "other_target_pending"}, set(parent._deferred_screening_workers))

    async def test_cancelled_terminal_page_cleanup_waits_before_forgetting_ownership(self):
        task, target, control = self._task_and_control(1)
        self.service.append_task_mode_candidates(
            self.user["id"], task["id"], target["id"], "followers", ["already_terminal"])
        self.service.finish_task_mode_candidate(self.user["id"], task["id"], target["id"],
            "followers", "already_terminal", state="deduped")
        state = pipeline._PipelineState()
        close_started = asyncio.Event()
        close_allowed = asyncio.Event()

        class Child(pipeline._ScreeningChild):
            async def disconnect(self):
                close_started.set()
                await close_allowed.wait()
                await super().disconnect()

        child = Child(state)
        parent = pipeline._RelationParent(state)
        parent._deferred_screening_workers = {"already_terminal": child}
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        execution = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}))
        try:
            await asyncio.wait_for(close_started.wait(), 3)
            control.stop_event.set()
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
            self.assertIn("already_terminal", parent._deferred_screening_workers)
        finally:
            close_allowed.set()
            await asyncio.gather(execution, return_exceptions=True)
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)

    async def test_pending_pagination_preserves_legacy_zero_order_and_validates_cursor(self):
        task, target, control = self._task_and_control(1)
        self.service.append_task_mode_candidates(
            self.user["id"], task["id"], target["id"], "followers", ["legacy", "next"])
        with self.service.database.write() as connection:
            connection.execute("UPDATE task_mode_candidates SET discovery_order=0 WHERE target_id=? AND username_norm='legacy'", (target["id"],))
        args = (self.user["id"], task["id"], target["id"], "followers")
        self.assertEqual(["legacy", "next"], [item["username"] for item in self.service.list_pending_task_mode_candidates(*args)])
        self.assertEqual(["next"], [item["username"] for item in self.service.list_pending_task_mode_candidates(*args, after_discovery_order=0)])
        for invalid in (True, -1, 1.5, "1"):
            with self.assertRaises(ValidationError):
                self.service.list_pending_task_mode_candidates(*args, after_discovery_order=invalid)
        state = pipeline._PipelineState()
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser())
        result = await manager._execute_candidate_spooled_mode(
            control, pipeline._RelationParent(state), target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}},
        )
        self.assertEqual(0, result["pending"])
        self.assertEqual(["legacy", "next"], [username for _, username in state.screened])


if __name__ == "__main__":
    unittest.main()
