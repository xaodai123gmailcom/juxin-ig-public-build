"""Zero-post profile outcomes advance the real durable collection pipeline.

Browser extraction has its own DOM fixtures. These tests deliberately use the
production screen/record and spool functions with a real SQLite database so a
browser-read fix cannot conceal a later queue stall or broaden zero exclusions.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome
from app.service import CoreService


class _ProfileReader:
    def __init__(self, profiles, reads, *, entered=None, release=None):
        self.profiles = profiles
        self.reads = reads
        self.entered = entered
        self.release = release
        self.disconnect_calls = 0

    async def read_visible_profile(self, username, *, include_activity=False):
        self.reads.append((username, include_activity))
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return {"username": username, "followers": 14, "following": 18,
                "activity_days": None, "activity_status": "no_posts",
                **self.profiles[username]}

    async def disconnect(self):
        self.disconnect_calls += 1


class ZeroPostsPipelineR37Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "zero-posts.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "zero-posts-r37", "zero-posts-pipeline-regression-password"
        )["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, *, exclude=True, parallel=2):
        # Independent visibility/switch cases share this SQLite fixture but do
        # not intentionally recollect the same source account.
        self._fixture_source_serial = getattr(self, "_fixture_source_serial", 0) + 1
        task = self.service.create_task(
            self.owner, name="zero-post pipeline", modes=["followers"],
            targets=[f"source_account_{self._fixture_source_serial}"],
            settings={"exclude_public_zero_posts": exclude,
                      "parallel_screening_workers": parallel,
                      "local_person_recognition": False,
                      "location_enabled": False, "gpt_enabled": False},
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue(),
        )
        return task, task["targets"][0], control

    def table_count(self, table):
        with self.database.read() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def assert_excluded(self, task_id, usernames):
        results = self.service.list_results(self.owner, task_id)
        exclusions = [result for result in results
                      if result["screening"].get("routing_result") == "excluded_zero_posts"]
        self.assertEqual(set(usernames), {result["username"] for result in exclusions})
        for result in exclusions:
            self.assertEqual(0, result["profile"]["posts"])
            self.assertFalse(result["qualified"])
            self.assertFalse(result["screening"]["activity"]["checked"])
            self.assertFalse(result["screening"]["location"]["checked"])

    async def test_recovered_source_end_excludes_zero_and_advances_to_next_candidate(self):
        task, target, control = self.task()
        profiles = {"empty_first": {"visibility": "public", "posts": 0},
                    "private_next": {"visibility": "private", "posts": 7},
                    "empty_last": {"visibility": "public", "posts": 0}}
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target["id"], "followers", list(profiles))
        reads = []
        worker = _ProfileReader(profiles, reads)
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True,
                        "candidate_spool_natural_end": True}}), 10)
        self.assertEqual([(name, False) for name in profiles], reads)
        self.assertEqual((3, 0, True),
                         (result["recorded"], result["pending"], result["discovery_complete"]))
        self.assertEqual(1, self.table_count("workbench_candidates"))
        self.assertEqual(2, self.table_count("workbench_collection_exclusions"))
        self.assert_excluded(task["id"], ["empty_first", "empty_last"])
        # A restart resumes terminal spool rows without revisiting either zero page.
        await self.manager._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True,
                        "candidate_spool_natural_end": True}})
        self.assertEqual(3, len(reads))

    async def test_two_zero_screening_tabs_finish_after_source_has_already_ended(self):
        task, target, control = self.task()
        profiles = {"zero_one": {"visibility": "public", "posts": 0},
                    "zero_two": {"visibility": "public", "posts": 0},
                    "private_after": {"visibility": "private", "posts": 8}}
        release = asyncio.Event()
        entered = [asyncio.Event(), asyncio.Event()]
        reads = []
        children = [_ProfileReader(profiles, reads, entered=event, release=release)
                    for event in entered]
        case = self

        class Source:
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True

            def __init__(self):
                self.created = 0

            async def create_parallel_screening_worker(self):
                child = children[self.created]
                self.created += 1
                return child

            async def collect_followers(self, _username, **kwargs):
                await kwargs["candidate_sink"](list(profiles))
                await asyncio.gather(*(event.wait() for event in entered))
                return CollectionOutcome("followers", [], source_total=3)

            async def read_visible_profile(self, *_args, **_kwargs):
                case.fail("healthy child pages must finish their own candidates")

        parent = Source()
        operation = asyncio.create_task(self.manager._execute_candidate_spooled_mode(
            control, parent, target, "followers", task["settings"], None))
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(event.wait() for event in entered))
                while True:
                    checkpoint = self.service.get_checkpoint(
                        self.owner, task["id"], target["id"], "followers")
                    if self.manager._candidate_spool_complete(checkpoint, require_natural_end=True):
                        break
                    await asyncio.sleep(.01)
            self.assertFalse(operation.done(), "source end must not end pending screening")
            stats = self.service.task_mode_candidate_stats(
                self.owner, task["id"], target["id"], "followers")
            self.assertEqual(3, stats["pending"])
            self.assertEqual({"zero_one", "zero_two"}, {name for name, _ in reads})
            release.set()
            result = await asyncio.wait_for(operation, 10)
            self.assertEqual((3, 0), (result["recorded"], result["pending"]))
            self.assertEqual(2, parent.created)
            self.assertEqual([1, 1], [child.disconnect_calls for child in children])
            self.assertCountEqual([(name, False) for name in profiles], reads)
            self.assert_excluded(task["id"], ["zero_one", "zero_two"])
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def test_exclusion_bypasses_optional_reads_even_with_their_switches_enabled(self):
        task, target, control = self.task()
        profiles = {"confirmed_empty": {"visibility": "public", "posts": 0}}
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target["id"], "followers", list(profiles))
        claim = self.service.claim_workbench_identity(
            self.owner, username="confirmed_empty", source="followers",
            source_target=target["id"], allow_owned_resume=True)
        reads = []
        case = self

        class NoOptionalReads(_ProfileReader):
            supports_avatar_image_capture = True

            async def capture_visible_review_snapshot(self, *_args, **_kwargs):
                case.fail("excluded zero profiles must not wait for avatar or post previews")

            async def read_visible_location_country(self, *_args, **_kwargs):
                case.fail("excluded zero profiles must not open account details")

        settings = {**task["settings"], "local_person_recognition": True,
                    "location_enabled": True, "gpt_enabled": True, "active_days_max": 30}
        self.assertTrue(await self.manager._screen_and_record(
            control, NoOptionalReads(profiles, reads), target["id"],
            "confirmed_empty", "followers", settings, claim_id=claim["claim_id"]))
        self.assertEqual([("confirmed_empty", False)], reads)
        self.assertEqual(0, self.table_count("workbench_candidates"))
        self.assert_excluded(task["id"], ["confirmed_empty"])

    async def test_unknown_counts_and_disabled_switch_are_not_zero_exclusions(self):
        for username, visibility, posts, exclude in (
            ("unknown_count", "public", None, True),
            ("private_zero", "private", 0, False),
            ("switch_disabled", "public", 0, False),
        ):
            with self.subTest(username=username):
                task, target, control = self.task(exclude=exclude)
                self.service.append_task_mode_candidates(
                    self.owner, task["id"], target["id"], "followers", [username])
                reads = []
                result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
                    control, _ProfileReader({username: {"visibility": visibility, "posts": posts}}, reads),
                    target, "followers", task["settings"],
                    {"cursor": {"candidate_spool_complete": True,
                                "candidate_spool_natural_end": True}}), 10)
                self.assertEqual((1, 0), (result["recorded"], result["pending"]))
                results = self.service.list_results(self.owner, task["id"])
                self.assertEqual(1, len(results))
                self.assertNotEqual("excluded_zero_posts", results[0]["screening"].get("routing_result"))
        self.assertEqual(0, self.table_count("workbench_collection_exclusions"))
        self.assertEqual(3, self.table_count("workbench_candidates"))


if __name__ == "__main__":
    unittest.main()
