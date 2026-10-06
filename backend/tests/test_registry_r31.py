"""Inventory reads snapshot loop-owned task registries before thread iteration."""
from __future__ import annotations

import asyncio
import threading
import unittest

from app.follow_monitor import FollowMonitorManager
from app.studio import StudioManager


class _ObservedTask(asyncio.Task):
    """A real running task with a controlled cross-thread observation boundary."""

    def __init__(self, coroutine, entered, release):
        super().__init__(coroutine)
        self.entered = entered
        self.release = release
        self.loop_thread = threading.get_ident()

    def done(self):
        if threading.get_ident() != self.loop_thread:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("registry mutation was not released")
        return super().done()


class RegistrySnapshotR31Tests(unittest.IsolatedAsyncioTestCase):
    async def check_registry(self, manager_type, registry_name, method_name):
        # Construction is unrelated to inventory reads; use the actual manager
        # methods against real asyncio tasks without launching browser workers.
        manager = manager_type.__new__(manager_type)
        entered, release = threading.Event(), threading.Event()
        pending_gate = asyncio.Event()
        observed = _ObservedTask(pending_gate.wait(), entered, release)
        original = asyncio.create_task(pending_gate.wait())
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        registry = {"observed": observed, "original": original, "finished": finished}
        setattr(manager, registry_name, registry)
        read = asyncio.create_task(asyncio.to_thread(getattr(manager, method_name)))
        late_tasks = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            # Change size and membership after the reader has begun checking a
            # task. A live dict iterator raises; a copied registry stays coherent.
            del registry["original"]
            for key in ("late_a", "late_b"):
                task = asyncio.create_task(pending_gate.wait())
                late_tasks.append(task)
                registry[key] = task
            release.set()
            self.assertEqual({"observed", "original"}, await asyncio.wait_for(read, 3))
            self.assertEqual({"observed", "late_a", "late_b"},
                             await asyncio.to_thread(getattr(manager, method_name)))
        finally:
            release.set()
            pending_gate.set()
            await asyncio.gather(read, observed, original, finished, *late_tasks,
                                 return_exceptions=True)

    async def test_monitor_inventory_survives_concurrent_registration_and_removal(self):
        await self.check_registry(FollowMonitorManager, "_tasks", "active_run_ids")

    async def test_studio_inventory_survives_concurrent_registration_and_removal(self):
        await self.check_registry(StudioManager, "tasks", "active_ids")


if __name__ == "__main__":
    unittest.main()
