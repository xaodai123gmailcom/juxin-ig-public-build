"""Seeded full scheduler/SQLite lifecycle with a controlled browser adapter."""
import asyncio
import os
from pathlib import Path
import random
import tempfile
import unittest

from app.database import Database
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.service import CoreService
from test_core import FakeCollectionWorker
from test_collection_drain_r56 import CloseProvider


class CollectionContinuityR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_seeded_stream_pause_drain_restart_and_global_dedupe(self):
        seed = int(os.environ.get("R94_AUDIT_SEED", "1"))
        rng = random.Random(seed)
        names = [f"round{seed}_candidate{i}" for i in range(12 + seed % 13)]
        stream = names + rng.choices(names, k=len(names))
        rng.shuffle(stream)
        batch_size = 1 + seed % 7
        zero_posts = set(names[::4])
        first_batch, allow_rest = asyncio.Event(), asyncio.Event()
        reads, disconnected = [], []
        child_attempts = []

        class Reader(FakeCollectionWorker):
            async def read_visible_profile(self, username, **kwargs):
                reads.append((username, bool(kwargs.get("include_activity") or kwargs.get("include_post_activity"))))
                await asyncio.sleep((sum(map(ord, username)) + seed) % 4 * .001)
                profile = await super().read_visible_profile(username, **kwargs)
                profile["posts"] = 0 if username in zero_posts else 9
                return profile

            async def disconnect(self):
                await asyncio.sleep(seed % 3 * .001)
                disconnected.append(self)

        class Worker(Reader):
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True

            async def create_parallel_screening_worker(self):
                child_attempts.append(True)
                if seed % 4 == 0:
                    raise RuntimeError("simulated child creation failure")
                return Reader(None)

            async def collect_followers(self, target, *, candidate_sink, progress_sink, **kwargs):
                for offset in range(0, len(stream), batch_size):
                    batch = stream[offset:offset + batch_size]
                    await candidate_sink(batch)
                    await progress_sink({"source_total": len(names), "resume_tail": batch,
                                         "rendered_count": offset + len(batch)})
                    if offset == 0 and target == "first_source":
                        first_batch.set()
                        await allow_rest.wait()
                    await asyncio.sleep((offset + seed) % 3 * .001)
                return CollectionOutcome("followers", [], source_total=len(names))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuity.sqlite3"
            database = Database(path)
            database.initialize()
            service = CoreService(database)
            owner = service.register_user("continuity", "controlled test password")["id"]
            provider = CloseProvider()
            manager = ExecutionManager(service, provider, worker_factory=Worker,
                                       lease_heartbeat_interval_seconds=.03)

            def create_task(core, source):
                return core.create_task(owner, name="continuity", modes=["followers"],
                    targets=[source], window_ids=["window-a"], settings={
                        "local_person_recognition": False, "location_enabled": False,
                        "parallel_screening_workers": 1 + seed % 3,
                        "exclude_public_zero_posts": True,
                    })

            task = create_task(service, "first_source")
            try:
                await manager.start(owner, task["id"])
                await asyncio.wait_for(first_batch.wait(), 10)
                await manager.pause(owner, task["id"])
                self.assertEqual("paused", service.get_task(owner, task["id"])["status"])
                self.assertEqual([], provider.closed)
                self.assertEqual(1, len(service.list_browser_lease_states(owner)))
                allow_rest.set()
                await manager.resume(owner, task["id"])
                await asyncio.wait_for(manager.wait(task["id"]), 15)
                current = service.get_task(owner, task["id"])
                self.assertEqual("completed", current["status"])
                results = service.list_results(owner, task["id"])
                self.assertEqual(set(names), {row["username"] for row in results})
                self.assertEqual(len(names), len(results))
                initial_reads = [username for username, activity in reads if not activity]
                self.assertEqual(set(names), set(initial_reads))
                self.assertEqual(len(names), len(initial_reads))
                for row in results:
                    if row["username"] in zero_posts:
                        self.assertEqual("excluded_zero_posts", row["screening"]["routing_result"])
                stats = service.task_mode_candidate_stats(owner, task["id"], task["targets"][0]["id"], "followers")
                self.assertEqual(0, stats["pending"])
                self.assertEqual(["window-a"], provider.closed)
                self.assertEqual([], service.list_browser_lease_states(owner))
                self.assertTrue(disconnected)
                self.assertTrue(child_attempts)
            finally:
                allow_rest.set()
                await asyncio.wait_for(manager.shutdown(), 10)

            # Reopen the actual on-disk database, then rediscover the same rows
            # under another source. Terminal identities must never reopen pages.
            database = Database(path)
            database.initialize()
            recovered = CoreService(database)
            second = create_task(recovered, "second_source")
            provider = CloseProvider()
            manager = ExecutionManager(recovered, provider, worker_factory=Worker)
            reads.clear()
            try:
                await manager.start(owner, second["id"])
                await asyncio.wait_for(manager.wait(second["id"]), 15)
                self.assertEqual("completed", recovered.get_task(owner, second["id"])["status"])
                self.assertEqual([], reads)
                self.assertEqual([], recovered.list_results(owner, second["id"]))
                stats = recovered.task_mode_candidate_stats(owner, second["id"], second["targets"][0]["id"], "followers")
                self.assertEqual((len(names), 0), (stats["deduped"], stats["pending"]))
                self.assertEqual(["window-a"], provider.closed)
                self.assertEqual([], recovered.list_browser_lease_states(owner))
            finally:
                await asyncio.wait_for(manager.shutdown(), 10)


if __name__ == "__main__":
    unittest.main()
