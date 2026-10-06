"""Exercise returned-window close acknowledgements and generation fencing."""
import asyncio
import threading
import unittest

import test_returned_window_release_r73 as fixtures
from app.errors import ConflictError


class ReturnedCloseR90Tests(unittest.IsolatedAsyncioTestCase):
    start = fixtures.ReturnedWindowRuntimeTests.start
    until = fixtures.ReturnedWindowRuntimeTests.until

    async def asyncSetUp(self):
        await fixtures.ReturnedWindowRuntimeTests.asyncSetUp(self)
        self.gates = []

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.set()
        await fixtures.ReturnedWindowRuntimeTests.asyncTearDown(self)

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    async def return_to_b(self, *, completed_probe=False, window_ids=None):
        task, failure = await self.start(window_ids=window_ids)
        control = self.manager._runs[task["id"]]
        await self.until(lambda: not control.worker_tasks)
        token = control.leases["window-a"]
        if completed_probe:
            # Reproduce a completed earlier probe whose done callback has not
            # removed its index yet, before a newly committed return arrives.
            previous = asyncio.create_task(self.manager._release_returned_window_if_idle(
                control, "window-a", token))
            await previous
            self.manager._returned_window_closures[(task["id"], "window-a", token)] = previous
            self.previous_probe = previous
        returned = self.service.requeue_split_candidate(
            self.owner, failure["id"], allowed_window_ids=["window-b"])
        self.manager.notify_split_queue(self.owner, returned_candidate=returned)
        closure = self.manager._returned_window_closures[(task["id"], "window-a", token)]
        return task, returned, control, token, closure

    async def test_completed_probe_cannot_swallow_a_new_return_notification(self):
        task, _, control, _, closure = await self.return_to_b(completed_probe=True)
        self.assertIsNot(self.previous_probe, closure)
        await asyncio.wait_for(asyncio.shield(closure), 10)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertNotIn("window-a", control.leases)
        self.assertEqual(["window-a"], self.browser.closed)

    def assert_occupied(self, control, token):
        self.assertEqual(token, control.leases.get("window-a"))
        rows = self.service.list_browser_lease_states(self.owner)
        self.assertIn("window-a", {row["profile_id"] for row in rows})
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(
                self.owner, "window-a", operation_type="account", entity_id="other")

    async def test_recorded_close_is_not_an_ack_and_duplicate_notifications_close_once(self):
        entered, release = self.gate(), self.gate()

        class Browser(fixtures._Browser):
            def close_profile(self, profile):
                result = super().close_profile(profile)
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("ack barrier was not released")
                return result

        self.browser = Browser()
        task, returned, control, token, closure = await self.return_to_b()
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        self.assertEqual(["window-a"], self.browser.closed)
        self.assertFalse(closure.done())
        self.assert_occupied(control, token)
        # Real caller threads may deliver the same durable notification again.
        await asyncio.gather(*(asyncio.to_thread(
            self.manager.notify_split_queue, self.owner, returned_candidate=returned)
            for _ in range(8)))
        self.assertEqual(["window-a"], self.browser.closed)
        self.assert_occupied(control, token)
        with self.assertRaises(ConflictError):
            await self.manager.resume_window(self.owner, task["id"], "window-a")
        release.set()
        await asyncio.wait_for(asyncio.shield(closure), 10)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertNotIn("window-a", control.leases)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        waiting = self.service.list_split_candidates(self.owner, candidate_ids=[returned["id"]])[0]
        self.assertEqual("queued", waiting["queue_state"])
        self.assertEqual(["window-b"], waiting["allowed_window_ids"])

    async def test_negative_ack_preserves_occupancy_and_retry_does_not_collect_again(self):
        await self.check_failed_close({"closed": False})

    async def test_deferred_ack_preserves_occupancy_and_retry_does_not_collect_again(self):
        await self.check_failed_close({"window_state": "closing"})

    async def test_provider_exception_preserves_occupancy_and_retry_does_not_collect_again(self):
        await self.check_failed_close(RuntimeError("offline"))

    async def check_failed_close(self, failure):
        class Browser(fixtures._Browser):
            result = failure

            def close_profile(self, profile):
                super().close_profile(profile)
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        self.browser = Browser()
        task, returned, control, token, closure = await self.return_to_b()
        await asyncio.wait_for(asyncio.shield(closure), 10)
        self.assert_occupied(control, token)
        self.assertEqual("browser_close_failed", control.profile_states["window-a"]["reason"])
        attempts = self.manager.attempts
        self.browser.result = {"closed": True}
        result = await self.manager.resume_window(self.owner, task["id"], "window-a")
        self.assertEqual("closed", result["status"])
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(attempts, self.manager.attempts)
        self.assertEqual(["window-a", "window-a"], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        waiting = self.service.list_split_candidates(self.owner, candidate_ids=[returned["id"]])[0]
        self.assertEqual("queued", waiting["queue_state"])

    async def test_cancelled_observer_does_not_abandon_owned_close(self):
        entered, release = self.gate(), self.gate()

        class Browser(fixtures._Browser):
            def close_profile(self, profile):
                result = super().close_profile(profile)
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("close barrier was not released")
                return result

        self.browser = Browser()
        task, _, control, token, closure = await self.return_to_b()
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))

        async def observe():
            await asyncio.shield(closure)

        observer = asyncio.create_task(observe())
        await asyncio.sleep(0)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertFalse(closure.done())
        self.assert_occupied(control, token)
        release.set()
        await asyncio.wait_for(asyncio.shield(closure), 10)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        self.assertEqual(["window-a"], self.browser.closed)

    async def test_b_can_finish_returned_source_after_a_closes(self):
        task, _, _, _, closure = await self.return_to_b(window_ids=["window-a", "window-b"])
        await asyncio.wait_for(asyncio.shield(closure), 10)
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        await self.manager.start(self.owner, task["id"], profile_ids=["window-b"])
        await asyncio.wait_for(self.manager.wait(task["id"]), 10)
        self.assertEqual(2, self.manager.attempts)
        self.assertEqual(["window-a", "window-b"], self.browser.closed)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["status"])


if __name__ == "__main__":
    unittest.main()
