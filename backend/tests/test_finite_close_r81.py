"""Finite collection tasks retain ownership until automatic close is acknowledged."""
import asyncio
import unittest

import test_collection_drain_r56 as fixtures
from app.errors import ConflictError


class FiniteCloseR81Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = fixtures.CollectionDrainR56Tests.asyncTearDown
    manager = fixtures.CollectionDrainR56Tests.manager
    until = fixtures.CollectionDrainR56Tests.until
    leases = fixtures.CollectionDrainR56Tests.leases

    def task(self, sources=("source",), windows=("window-a",), **settings):
        return fixtures.CollectionDrainR56Tests.task(
            self, sources, windows, live_queue_enabled=False, **settings)

    async def test_finite_completion_closes_after_disconnect_and_durable_completion(self):
        await fixtures.CollectionDrainR56Tests.test_final_source_closes_after_durable_completion_and_disconnect(self)

    async def test_finite_negative_close_ack_retains_lease_until_close_only_retry(self):
        for result, suffix in (
            ({"closed": False}, "negative"),
            ({"provider_success": True, "window_state": "closing"}, "deferred"),
            (RuntimeError("provider unavailable"), "exception"),
        ):
            with self.subTest(suffix=suffix):
                profile = f"finite-{suffix}"
                task = self.task((f"source_{suffix}",), (profile,))
                collected = []

                class Worker(fixtures.FakeCollectionWorker):
                    async def collect_followers(self, username, **kwargs):
                        collected.append(username)
                        return await super().collect_followers(username, **kwargs)

                manager, provider = self.manager(Worker)
                if isinstance(result, Exception):
                    provider.error = result
                else:
                    provider.result = result
                await manager.start(self.owner, task["id"])
                control = manager._runs[task["id"]]
                await self.until(lambda: task["id"] not in manager._runs or (
                    control.profile_states.get(profile, {}).get("reason") == "browser_close_failed"
                    and not control.worker_tasks))
                self.assertIn(profile, self.leases())
                self.assertIn(task["id"], manager.active_task_ids())
                self.assertEqual("manual_required", control.profile_states[profile]["state"])
                with self.assertRaises(ConflictError):
                    self.service.acquire_browser_lease(self.owner, profile,
                        operation_type="account", entity_id="unrelated")
                provider.error, provider.result = None, {"closed": True}
                retried = await manager.resume_window(self.owner, task["id"], profile)
                self.assertEqual("closed", retried["status"])
                await asyncio.wait_for(manager.wait(task["id"]), 5)
                self.assertEqual([f"source_{suffix}"], collected)
                self.assertEqual([profile, profile], provider.closed)
                self.assertEqual({}, self.leases())
                self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])


if __name__ == "__main__":
    unittest.main()
