"""Source statistics are captured before collection leaves the exact profile."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.service import CoreService


class SourceProfileSnapshotR56Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self, *, header="", stats="", links=None):
        worker = PlaywrightWorker(None)
        links = links or {}

        def locator(selector):
            relation = "followers" if "/followers" in selector else "following"
            return SimpleNamespace(evaluate_all=AsyncMock(return_value=links.get(relation, [])))

        worker.page = SimpleNamespace(url="https://www.instagram.com/source/", locator=locator)
        worker._visible_profile_header = AsyncMock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=header)))
        worker._visible_profile_stats_text = AsyncMock(return_value=stats)
        worker._navigate_profile = AsyncMock(return_value="source")
        worker.read_visible_profile = AsyncMock(side_effect=AssertionError("must not screen the source"))
        worker._visible_relation_count = AsyncMock(return_value=0)
        return worker

    async def test_source_snapshot_uses_exact_relation_links_and_preserves_real_zero(self):
        worker = self.worker(header="0 posts 11 followers 12 following", links={
            "followers": [{"href": "/suggested/followers/", "text": "999"},
                          {"href": "/source/followers/?hl=en", "text": "31"}],
            "following": [{"href": "/suggested/following/", "text": "888"},
                          {"href": "/source/following/", "text": "0"}],
        })
        profile = await worker._source_profile_metrics("source")
        self.assertEqual({"username": "source", "followers": 31, "following": 0, "posts": 0}, profile)
        worker._navigate_profile.assert_not_awaited()
        worker.read_visible_profile.assert_not_awaited()

    async def test_unknowns_stay_null_and_posts_cache_is_exact_username_only(self):
        worker = self.worker()
        worker._profile_posts_count_cache["someone_else"] = 90
        self.assertEqual({"username": "source", "followers": None, "following": None, "posts": None},
                         await worker._source_profile_metrics("source"))
        worker._profile_posts_count_cache["source"] = 0
        self.assertEqual(0, (await worker._source_profile_metrics("source"))["posts"])

    async def test_wrong_profile_never_reads_or_uses_source_cache(self):
        worker = self.worker(header="22 posts 33 followers 44 following")
        worker.page.url = "https://www.instagram.com/someone_else/"
        worker._profile_posts_count_cache["source"] = 55
        profile = await worker._source_profile_metrics("source", relation="followers", source_total=66)
        self.assertEqual({}, profile)
        worker._visible_profile_header.assert_not_awaited()
        worker._visible_profile_stats_text.assert_not_awaited()

    async def test_navigation_during_metadata_read_discards_the_snapshot(self):
        worker = self.worker(header="22 posts 33 followers 44 following")

        async def moved(_username):
            worker.page.url = "https://www.instagram.com/someone_else/"
            return (100, 200)

        worker._visible_profile_relation_counts = moved
        profile = await worker._source_profile_metrics("source")
        self.assertEqual({}, profile)

    async def test_optional_failures_do_not_break_collection_or_add_navigation(self):
        worker = self.worker()
        failure = WorkerExecutionError("optional metadata failed", reason="instagram_profile_not_ready")
        worker._visible_profile_header.side_effect = failure
        worker._visible_profile_stats_text.side_effect = failure
        worker._visible_profile_relation_counts = AsyncMock(side_effect=failure)
        progress = AsyncMock()
        outcome = await worker.collect_followers("source", limit=None, progress_sink=progress)
        self.assertEqual(0, outcome.source_total)
        self.assertEqual({"username": "source", "followers": 0, "following": None, "posts": None},
                         progress.await_args_list[0].args[0]["source_profile"])
        worker._navigate_profile.assert_awaited_once_with("source")
        worker.read_visible_profile.assert_not_awaited()

    async def test_visible_stats_supply_unknown_metrics_without_replacing_known_relation_total(self):
        worker = self.worker(stats="0 posts 12 followers 13 following")
        profile = await worker._source_profile_metrics("source", relation="followers", source_total=0)
        self.assertEqual({"username": "source", "followers": 0, "following": 13, "posts": 0}, profile)

    async def test_no_progress_consumer_skips_optional_metadata(self):
        worker = self.worker()
        worker._source_profile_metrics = AsyncMock(side_effect=AssertionError("unrequested metadata"))
        outcome = await worker.collect_followers("source", limit=None)
        self.assertEqual(0, outcome.source_total)
        worker._source_profile_metrics.assert_not_awaited()
        worker._navigate_profile.assert_awaited_once_with("source")

    async def test_legacy_post_likers_capture_source_before_zero_post_completion(self):
        for with_progress in (False, True):
            with self.subTest(with_progress=with_progress):
                worker = self.worker(header="0 posts 21 followers 22 following")
                original_locator = worker.page.locator
                worker.page.locator = lambda selector: (
                    SimpleNamespace(inner_text=AsyncMock(return_value="0 posts 21 followers 22 following\nNo posts yet"))
                    if selector == "body" else original_locator(selector)
                )
                progress = AsyncMock() if with_progress else None
                worker._source_profile_metrics = AsyncMock(wraps=worker._source_profile_metrics)
                result = await worker.collect_post_likers(
                    "source", max_posts=1, per_post_limit=10, progress_sink=progress,
                )
                self.assertEqual([], result.usernames)
                worker._navigate_profile.assert_awaited_once_with("source")
                if progress is None:
                    worker._source_profile_metrics.assert_not_awaited()
                else:
                    snapshot = next(call.args[0]["source_profile"] for call in progress.await_args_list
                                    if "source_profile" in call.args[0])
                    self.assertEqual({"username": "source", "followers": 21, "following": 22, "posts": 0}, snapshot)

    async def test_stop_cancellation_is_not_swallowed_as_metadata_failure(self):
        worker = self.worker()
        worker._visible_profile_header.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await worker._source_profile_metrics("source")

    async def test_spooled_execution_persists_source_progress_before_mode_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "source.sqlite3")
            database.initialize()
            service = CoreService(database)
            owner = service.register_user("source-profile-test", "source profile test password")["id"]
            task = service.create_task(owner, name="source snapshot", modes=["followers"], targets=["source"],
                                       settings={"parallel_screening_workers": 1})
            target = task["targets"][0]
            paused = asyncio.Event()
            paused.set()
            control = ExecutionControl(owner_user_id=owner, task_id=task["id"], pause_event=paused,
                                       stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
            manager = ExecutionManager(service, SimpleNamespace())
            worker = self.worker(header="0 posts 0 followers 13 following")
            child = SimpleNamespace(disconnect=AsyncMock(), read_visible_profile=AsyncMock(
                side_effect=AssertionError('empty source must not screen any candidate')))
            worker.create_parallel_screening_worker = AsyncMock(return_value=child)
            result = await manager._execute_candidate_spooled_mode(
                control, worker, target, "followers", task["settings"], None,
            )
            self.assertTrue(result["discovery_complete"])
            self.assertEqual(0, result["pending"])
            with database.read() as connection:
                snapshot = json.loads(connection.execute(
                    "SELECT source_profile_json FROM task_targets WHERE id=?", (target["id"],),
                ).fetchone()[0])
            self.assertEqual({"username": "source", "followers": 0, "following": 13, "posts": 0}, snapshot)
            worker._navigate_profile.assert_awaited_once_with("source")
            worker.read_visible_profile.assert_not_awaited()
            child.read_visible_profile.assert_not_awaited()
            child.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
