"""Natural relationship-list completion with real SQLite and task ownership.

These tests control browser observations, not execution-manager completion logic.
The visible header total is deliberately higher than the durable candidate count.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import Database
from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from app.service import CoreService
from completion_wait import wait_for_collection_completion


class _Browser:
    def close_profile(self, _profile_id):
        return {"closed": True}


class _ControlledWorker:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True

    def __init__(self, _browser):
        self.profile_id = None
        self.connect_calls = 0
        self.source_calls = Counter()
        self.source_inputs = []
        self.profile_reads = Counter()
        self.plans = {}
        self.incomplete_once = set()
        self.on_source = None
        self.replacements = []
        self.block_username = None
        self.profile_entered = asyncio.Event()
        self.allow_profile = asyncio.Event()

    async def connect(self, profile_id, **_kwargs):
        self.profile_id = profile_id
        self.connect_calls += 1

    async def disconnect(self):
        pass

    async def connection_healthy(self):
        return True

    def prepare_page_retry(self, _target):
        pass

    def request_page_replacement(self, target, reason):
        self.replacements.append((target, reason))

    async def collect_followers(
        self, target, *, limit, candidate_sink, initial_candidate_count,
        progress_sink, initial_resume_tail=None, **_kwargs,
    ):
        self.source_calls[target] += 1
        self.source_inputs.append((target, initial_candidate_count, list(initial_resume_tail or [])))
        if self.on_source is not None:
            self.on_source(target)
        if target not in self.plans:
            raise AssertionError("A naturally completed source was reopened")
        usernames, source_total = self.plans[target]
        for offset in range(0, len(usernames), 100):
            await candidate_sink(usernames[offset:offset + 100])
        await progress_sink({"source_total": source_total, "resume_tail": usernames[-2:]})
        if target in self.incomplete_once and self.source_calls[target] == 1:
            raise WorkerExecutionError(
                "Controlled interrupted list before confirmed natural end",
                reason="instagram_followers_list_incomplete",
            )
        # A successful worker return means a confirmed natural source end. The
        # worker's physical-tail detector is tested separately; this suite tests
        # the manager, spool, recovery cleanup, and next-target transition.
        return CollectionOutcome("followers", [], source_total=source_total)

    async def read_visible_profile(self, username, **kwargs):
        # The normal public-profile pipeline separately reads activity from the
        # same page. Count initial profile reads to detect duplicate navigation.
        if not kwargs.get("include_activity"):
            self.profile_reads[username] += 1
        if username == self.block_username and not kwargs.get("include_activity"):
            self.profile_entered.set()
            await self.allow_profile.wait()
        return {
            "username": username, "visibility": "public",
            "followers": 10, "following": 10, "posts": 2,
        }

    async def read_visible_account_location(self, _username):
        return "美国"


class CompletionRuntimeR43Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "completion.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "completion-r43", "completion runtime test password"
        )["id"]
        self.managers = []
        self.workers = []
        self.worker_created = asyncio.Event()

    async def asyncTearDown(self):
        for worker in self.workers:
            worker.allow_profile.set()
        for manager in self.managers:
            await manager.shutdown()
        self.temp.cleanup()

    def make_task(self, targets):
        return self.service.create_task(
            self.owner, name="short natural list", modes=["followers"],
            targets=targets, window_ids=["same-window"],
            settings={"local_person_recognition": False, "location_enabled": False},
        )

    def make_manager(self, configure=None):
        def factory(browser):
            worker = _ControlledWorker(browser)
            if configure is not None:
                configure(worker)
            self.workers.append(worker)
            self.worker_created.set()
            return worker

        manager = ExecutionManager(
            self.service, _Browser(), worker_factory=factory,
            network_retry_delays=(.001,), network_retry_stagger_seconds=0,
            relationship_no_progress_retry_limit=0,
            recovery_cooldown_seconds=.001,
        )
        self.managers.append(manager)
        return manager

    def checkpoint(self, task, target_id, *, stage, total, processed):
        self.service.upsert_checkpoint(
            self.owner, task["id"], target_id, mode="followers", stage=stage,
            cursor={
                "candidate_spool_version": 1,
                "candidate_spool_complete": True,
                "candidate_spool_natural_end": True,
            },
            counters={
                "source_total": 167, "discovered": total,
                "processed": processed, "pending_candidates": total - processed,
            },
            recoverable=True,
        )

    async def test_164_of_167_recovers_completes_and_same_window_runs_next_without_duplicates(self):
        task = self.make_task(["short_source", "next_source"])
        fans = [f"short_fan_{index:03d}" for index in range(164)]
        next_observations = []
        source_leases = []

        def configure(worker):
            worker.plans = {
                "short_source": (fans, 167),
                "next_source": ([fans[0], "next_unique_fan"], 2),
            }
            worker.incomplete_once.add("short_source")

            def observe(target):
                with self.database.read() as connection:
                    lease = connection.execute(
                        'SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',
                        ('same-window',),
                    ).fetchone()
                self.assertIsNotNone(lease)
                source_leases.append(lease['lease_token'])
                if target == "short_source" and worker.source_calls[target] == 3:
                    saved = self.service.get_checkpoint(self.owner, task['id'],
                        task['targets'][0]['id'], 'followers')
                    cursor = ExecutionManager._checkpoint_resume_cursor(saved)
                    self.assertTrue(cursor['automatic_gap_recheck_started'])
                    self.assertFalse(cursor['candidate_spool_complete'])
                    self.assertFalse(cursor.get('resume_tail'))
                if target == "next_source":
                    control = manager._runs[task["id"]]
                    next_observations.append({
                        "window": worker.profile_id,
                        "waiters": dict(control.network_waiters),
                        "retry_events": dict(control.retry_network_events),
                    })
            worker.on_source = observe

        manager = self.make_manager(configure)
        await manager.start(self.owner, task["id"])
        await wait_for_collection_completion(manager, self.service, self.owner, task["id"])
        stored = self.service.get_task(self.owner, task["id"])
        self.assertTrue(all(item["status"] == "completed" for item in stored["targets"]))
        short_target = next(item for item in stored["targets"] if item["username"] == "short_source")
        checkpoint = self.service.get_checkpoint(
            self.owner, task["id"], short_target["id"], "followers"
        )
        self.assertEqual("mode_completed", checkpoint["stage"])
        self.assertEqual(167, checkpoint["counters"]["source_total"])
        self.assertEqual(164, checkpoint["counters"]["processed"])
        self.assertEqual(0, checkpoint["counters"]["pending_candidates"])
        self.assertTrue(checkpoint["cursor"]["candidate_spool_natural_end"])
        self.assertEqual(1, len(self.workers))
        worker = self.workers[0]
        self.assertEqual(1, worker.connect_calls)
        # One interrupted source, its resumed initial pass, then exactly one
        # fresh supplemental pass. The distinct no-gap next source scans once.
        self.assertEqual({"short_source": 3, "next_source": 1}, worker.source_calls)
        self.assertEqual([
            ('short_source', 0, []), ('short_source', 164, fans[-2:]),
            ('short_source', 164, []), ('next_source', 0, []),
        ], worker.source_inputs)
        self.assertEqual(1, len(set(source_leases)))
        self.assertTrue(checkpoint['cursor']['automatic_gap_recheck_started'])
        next_target = next(item for item in stored['targets'] if item['username'] == 'next_source')
        next_checkpoint = self.service.get_checkpoint(self.owner, task['id'], next_target['id'], 'followers')
        self.assertFalse(next_checkpoint['cursor'].get('automatic_gap_recheck_started'))
        self.assertEqual({name: 1 for name in fans + ["next_unique_fan"]}, worker.profile_reads)
        self.assertEqual([{"window": "same-window", "waiters": {}, "retry_events": {}}], next_observations)
        self.assertEqual(165, len(self.service.list_results(self.owner, task["id"])))
        diagnostic = await manager.runtime_diagnostics(self.owner, task["id"])
        self.assertEqual([], diagnostic["network_waiters"])
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_natural_end_and_even_stale_mode_completed_do_not_skip_pending_candidate(self):
        task = self.make_task(["short_source"])
        target_id = task["targets"][0]["id"]
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target_id, "followers", ["pending_fan"]
        )
        # A stale completed projection cannot outweigh the pending SQLite row.
        self.checkpoint(task, target_id, stage="mode_completed", total=1, processed=1)

        def configure(worker):
            worker.block_username = "pending_fan"

        manager = self.make_manager(configure)
        await manager.start(self.owner, task["id"])
        await asyncio.wait_for(self.worker_created.wait(), 10)
        worker = self.workers[0]
        try:
            await asyncio.wait_for(worker.profile_entered.wait(), 10)
            stored = self.service.get_task(self.owner, task["id"])
            self.assertEqual("running", stored["targets"][0]["status"])
            stats = self.service.task_mode_candidate_stats(
                self.owner, task["id"], target_id, "followers"
            )
            self.assertEqual(1, stats["pending"])
            self.assertEqual({}, worker.source_calls)
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(
                    self.owner, "same-window", operation_type="action", entity_id="must-not-steal"
                )
        finally:
            worker.allow_profile.set()
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        self.assertEqual("completed", self.service.get_task(self.owner, task["id"])["targets"][0]["status"])
        self.assertEqual({"pending_fan": 1}, worker.profile_reads)
        self.assertEqual({}, worker.source_calls)
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))

    async def test_restart_of_164_terminal_candidates_preserves_short_complete_source(self):
        task = self.make_task(["short_source"])
        target_id = task["targets"][0]["id"]
        fans = [f"restored_fan_{index:03d}" for index in range(164)]
        for offset in range(0, len(fans), 100):
            self.service.append_task_mode_candidates(
                self.owner, task["id"], target_id, "followers", fans[offset:offset + 100]
            )
        for username in fans:
            self.service.record_result(
                self.owner, task["id"], target_id, username=username,
                instagram_user_id=None, source_mode="followers", visibility="public",
                profile={"username": username, "posts": 2}, screening={}, qualified=True,
            )
        self.service.reconcile_task_mode_candidates(self.owner, task["id"], target_id, "followers")
        # Simulate an exit after the source and candidates committed, before the
        # owning target's terminal update. Reconstruct the service as startup does.
        self.checkpoint(task, target_id, stage="screening_accounts", total=164, processed=164)
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        self.service.set_target_runtime_status(
            self.owner, task["id"], target_id, "running", window_id="same-window"
        )
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.service.recover_interrupted_operations()
        manager = self.make_manager()
        await manager.retry_target(self.owner, task["id"], target_id)
        await asyncio.wait_for(manager.wait(task["id"]), 10)
        stored = self.service.get_task(self.owner, task["id"])
        self.assertEqual("completed", stored["targets"][0]["status"])
        self.assertEqual({}, self.workers[0].source_calls)
        self.assertEqual({}, self.workers[0].profile_reads)
        self.assertEqual(164, len(self.service.list_results(self.owner, task["id"])))
        checkpoint = self.service.get_checkpoint(self.owner, task["id"], target_id, "followers")
        self.assertEqual("mode_completed", checkpoint["stage"])
        self.assertEqual(167, checkpoint["counters"]["source_total"])
        self.assertEqual(0, checkpoint["counters"]["pending_candidates"])
        self.assertEqual([], self.service.list_browser_lease_states(self.owner))


if __name__ == "__main__":
    unittest.main()
