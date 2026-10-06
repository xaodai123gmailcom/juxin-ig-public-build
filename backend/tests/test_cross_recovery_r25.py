"""Independent review of recovery after source discovery, using real SQLite."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

import test_parallel_relation_pipeline as pipeline
from completion_wait import wait_for_collection_operation
from app.execution_manager import ExecutionManager
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class CrossRecoveryR25Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = pipeline.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = pipeline.ParallelRelationPipelineTests._task_and_control

    def test_post_liker_transient_is_automatic_but_nested_guard_stays_manual(self):
        transient = PlaywrightWorker._page_recovery_exhausted(
            WorkerExecutionError("liker list loading", reason="instagram_post_likers_list_not_rendered"), "source")
        self.assertTrue(ExecutionManager._is_instagram_surface_retry_error(transient))
        self.assertFalse(ExecutionManager._is_profile_intervention_error(transient))
        for reason in ("instagram_login_required", "instagram_challenge", "instagram_rate_limited", "instagram_action_blocked", "unknown"):
            guard = PlaywrightWorker._page_recovery_exhausted(
                PlaywrightWorker._page_recovery_exhausted(WorkerExecutionError("guard", reason=reason), "source"))
            guard.details["auto_retry"] = True
            self.assertTrue(ExecutionManager._is_profile_intervention_error(guard))
            self.assertFalse(ExecutionManager._is_instagram_surface_retry_error(guard))

    async def test_repeated_retained_candidate_failures_never_reopen_completed_source(self):
        task, target, control = self._task_and_control(1)
        control.started_profile_ids.add("window")
        state = pipeline._PipelineState()

        class Child(pipeline._ScreeningChild):
            attempts = 0
            replacements = 0

            def request_page_replacement(self, target, reason):
                self.replacements += 1

            async def screen(self, username):
                self.state.screen_started.set()
                if username == "pipeline_a":
                    self.attempts += 1
                    if self.attempts <= 3:
                        raise PlaywrightWorker._page_recovery_exhausted(
                            WorkerExecutionError("still loading", reason="instagram_profile_not_ready"), username)
                await super().screen(username)

        class Parent(pipeline._RelationParent):
            collections = 0

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.replacements = []
                self.connection_healthy = AsyncMock(return_value=True)

            def prepare_page_retry(self, target):
                pass

            def request_page_replacement(self, target, reason):
                self.replacements.append(target)

            async def collect_followers(self, *args, **kwargs):
                self.collections += 1
                return await super().collect_followers(*args, **kwargs)

        child = Child(state)
        parent = Parent(state, child=child)
        manager = pipeline._PipelineManager(self.service, pipeline._NoopBitBrowser(),
            network_retry_delays=(0,), network_retry_stagger_seconds=0)
        manager.recovery_cooldown_seconds = 0
        async def phase(operation_factory):
            return await wait_for_collection_operation(
                operation_factory, manager, self.service, self.user["id"], task["id"])

        checkpoint = None
        for attempt in range(1, 4):
            with self.assertRaises(WorkerExecutionError) as caught:
                await phase(lambda: manager._execute_candidate_spooled_mode(
                    control, parent, target, "followers", task["settings"], checkpoint))
            error = caught.exception
            self.assertTrue(error.details.get("source_discovery_complete"), f"lost source state on failed screening round {attempt}")
            self.assertEqual("pipeline_a", error.details.get("candidate_username"))
            self.assertEqual("pipeline_a", error.details.get("recovery_target"))
            self.assertEqual(0, child.disconnect_calls)
            self.assertEqual(1, parent.collections)
            pending = self.service.list_pending_task_mode_candidates(
                self.user["id"], task["id"], target["id"], "followers")
            self.assertEqual(["pipeline_a"], [row["username"] for row in pending])
            generation = await phase(lambda: manager._recover_network_connection(
                control, parent, "window", error, target=target, mode="followers"))
            self.assertIsNotNone(generation)
            self.assertEqual([], parent.replacements, "completed source must never receive a new-page request")
            checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], "followers")
        result = await phase(lambda: manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], checkpoint))
        self.assertTrue(result["discovery_complete"])
        self.assertEqual(3, result["recorded"])
        self.assertEqual(0, result["pending"])
        self.assertEqual(1, parent.collections)
        self.assertEqual(3, child.replacements)
        self.assertEqual(1, child.disconnect_calls)
        self.assertEqual({}, parent._deferred_screening_workers)
        self.assertEqual(["pipeline_a", "pipeline_b", "pipeline_c"], sorted(name for _, name in state.screened))


if __name__ == "__main__":
    unittest.main()
