"""Real Chromium profile reads joined to the durable SQLite collection pipeline.

Only the relationship-list producer is controlled here. Profile navigation, DOM
parsing, readiness, exclusion, candidate claims and result writes are production
code. Every browser request is fulfilled locally; no Instagram account is used.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome, PlaywrightWorker
from app.service import CoreService
from test_zero_profile_reader_r45 import launch_fixture_browser, profile_html


class _ProfileWorker(PlaywrightWorker):
    """Keep fixture page cleanup local to its one shared test browser context."""

    def __init__(self, page):
        super().__init__(None)
        self.page = page
        self.profile_id = None
        self.disconnect_calls = 0
        self.profile_reads = Counter()
        self.first_samples = set()
        self._recover_stalled_profile_page = AsyncMock(return_value=False)
        original = self._profile_surface_is_transient

        async def finish_render(username, **kwargs):
            # The DOM and counters settle just after the first captured sample.
            # This fixes the event ordering without a speed-sensitive wall clock.
            if username not in self.first_samples:
                self.first_samples.add(username)
                await self.page.evaluate("window.renderProfile?.()")
            return await original(username, **kwargs)

        self._profile_surface_is_transient = finish_render

    async def read_visible_profile(self, username, **kwargs):
        if not kwargs.get("include_activity") and not kwargs.get("include_post_activity"):
            self.profile_reads[username] += 1
        return await super().read_visible_profile(username, **kwargs)

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.page is not None:
            await self.page.close()
            self.page = None


class _Source:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    supports_parallel_screening_tab = True

    def __init__(self, context, candidates):
        self.context = context
        self.candidates = candidates
        self.children = []
        self.source_reads = 0
        self.before_collect = None
        self.parent = None

    async def create_parallel_screening_worker(self):
        worker = _ProfileWorker(await self.context.new_page())
        self.children.append(worker)
        return worker

    async def collect_followers(self, _target, *, candidate_sink, **_kwargs):
        self.source_reads += 1
        if self.before_collect is not None:
            self.before_collect(self.source_reads, _kwargs)
        await candidate_sink(self.candidates)
        # The intentional header gap grants exactly one R6 supplemental pass;
        # repeated identities must never duplicate profile reads or queue results.
        return CollectionOutcome("followers", [], source_total=len(self.candidates) + 3)

    async def read_visible_profile(self, username, **kwargs):
        if self.parent is None:
            self.parent = _ProfileWorker(await self.context.new_page())
        return await self.parent.read_visible_profile(username, **kwargs)


class ZeroPipelineR45Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = await launch_fixture_browser(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zero-reader-pipeline.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user("zero-pipeline-r45", "zero-reader-pipeline-password")["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.documents = {}
        self.navigations = Counter()
        self.task_serial = 0

        async def route(request):
            username = urlparse(request.request.url).path.strip("/")
            if request.request.is_navigation_request():
                self.navigations[username] += 1
            html = self.documents.get(username, "<!doctype html><body>fixture resource</body>")
            await request.fulfill(content_type="text/html; charset=utf-8", body=html)

        await self.context.route("**/*", route)

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()

    def task(self, *, parallel=2):
        self.task_serial += 1
        task = self.service.create_task(
            self.owner, name="real DOM zero-post pipeline", modes=["followers"],
            targets=[f"fixture_source_{self.task_serial}"],
            settings={"parallel_screening_workers": parallel,
                      "exclude_public_zero_posts": True,
                      "local_person_recognition": False, "location_enabled": False,
                      "gpt_enabled": False, "public_discard_active_days_max": 0},
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue(),
        )
        return task, task["targets"][0], control

    async def execute(self, state, source, checkpoint=None):
        task, target, control = state
        return await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, source, target, "followers", task["settings"], checkpoint,
        ), 20)

    def table_usernames(self, table):
        # Business tables reference canonical account IDs, not display text.
        with self.database.read() as connection:
            return {row[0] for row in connection.execute(
                f"SELECT a.current_username_norm FROM {table} x JOIN instagram_accounts a ON a.id=x.account_id"
            )}

    async def parallel_case(self, parallel):
        self.documents = {
            "sample_reader01": profile_html("sample_reader01", delayed=True),
            "private_zero": profile_html("private_zero", private=True, delayed=True),
            "next_private": profile_html("next_private", private=True, posts=7, delayed=True),
        }
        state = self.task(parallel=parallel)
        source = _Source(self.context, list(self.documents))
        pass_budget_observed = []

        def before_collect(pass_number, inputs):
            if pass_number == 1:
                return
            self.assertEqual(2, pass_number, "only one supplemental source pass is allowed")
            checkpoint = self.service.get_checkpoint(
                self.owner, state[0]["id"], state[1]["id"], "followers",
            )
            cursor = self.manager._checkpoint_resume_cursor(checkpoint)
            self.assertTrue(cursor["automatic_gap_recheck_started"], "budget must commit before reopening")
            self.assertFalse(cursor["candidate_spool_complete"])
            self.assertFalse(cursor["candidate_spool_natural_end"])
            self.assertFalse(cursor.get("resume_tail"))
            self.assertEqual(3, inputs["initial_candidate_count"])
            self.assertFalse(inputs.get("initial_resume_tail"))
            self.assertEqual(parallel, len(source.children), "extra pass must reuse the same child pool")
            pass_budget_observed.append(pass_number)

        source.before_collect = before_collect
        stats = await self.execute(state, source)
        self.assertEqual((3, 0, True), (stats["recorded"], stats["pending"], stats["discovery_complete"]))
        self.assertEqual(2, source.source_reads)
        self.assertEqual([2], pass_budget_observed)
        self.assertEqual((3, 6), (stats["total"], stats["source_total"]))
        self.assertTrue(stats["automatic_gap_recheck_started"])
        checkpoint = self.service.get_checkpoint(self.owner, state[0]["id"], state[1]["id"], "followers")
        self.assertTrue(checkpoint["cursor"]["automatic_gap_recheck_started"])
        self.assertTrue(self.manager._candidate_spool_complete(checkpoint, require_natural_end=True))
        self.assertEqual(parallel, len(source.children))
        self.assertIsNone(source.parent, "healthy parallel pages must finish the queue themselves")
        self.assertEqual([1] * parallel, [child.disconnect_calls for child in source.children])
        for child in source.children:
            child._recover_stalled_profile_page.assert_not_awaited()
        self.assertEqual({"sample_reader01": 1, "private_zero": 1, "next_private": 1}, self.navigations)
        saved = {row["username"]: row for row in self.service.list_results(self.owner, state[0]["id"])}
        self.assertEqual("excluded_zero_posts", saved["sample_reader01"]["screening"]["routing_result"])
        self.assertEqual("excluded_zero_posts", saved["private_zero"]["screening"]["routing_result"])
        self.assertEqual({"sample_reader01", "private_zero"}, self.table_usernames("workbench_collection_exclusions"))
        self.assertEqual({"next_private"}, self.table_usernames("workbench_candidates"))
        self.assertTrue(self.service.check_global_dedupe("sample_reader01")["seen"])

        # Reload durable state, then encounter the discarded identity in another
        # source. Its profile must not navigate even once more after restart.
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        before = self.navigations.copy()
        repeat_source = _Source(self.context, ["SAMPLE_READER01"])
        repeat = await self.execute(self.task(parallel=parallel), repeat_source)
        self.assertEqual((0, 1, 0), (repeat["recorded"], repeat["deduped"], repeat["pending"]))
        self.assertEqual(2, repeat_source.source_reads)
        self.assertEqual((1, 4), (repeat["total"], repeat["source_total"]))
        self.assertTrue(repeat["automatic_gap_recheck_started"])
        self.assertEqual(before, self.navigations)

    async def test_one_child_delayed_public_zero_discards_and_continues(self):
        await self.parallel_case(1)

    async def test_two_children_delayed_public_zero_discards_and_continues(self):
        await self.parallel_case(2)

    async def test_completed_source_retries_old_pending_zero_without_reopening_source(self):
        self.documents = {
            "sample_reader01": profile_html("sample_reader01", delayed=True),
            "private_zero": profile_html("private_zero", private=True, delayed=True),
        }
        state = self.task()
        task, target, _ = state
        self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", list(self.documents))
        self.service.claim_workbench_identity(self.owner, username="sample_reader01", source="followers", source_target=target["id"], allow_owned_resume=True)
        self.service.upsert_checkpoint(
            self.owner, task["id"], target["id"], mode="followers", stage="screening_accounts",
            cursor={"candidate_spool_complete": True, "candidate_spool_natural_end": True},
            counters={"source_total": 509, "discovered": 2, "pending_candidates": 2, "processed": 0},
            recoverable=True,
        )
        checkpoint = self.service.get_checkpoint(self.owner, task["id"], target["id"], "followers")
        source = _Source(self.context, [])
        stats = await self.execute(state, source, checkpoint)
        self.assertEqual((2, 0, 509), (stats["recorded"], stats["pending"], stats["source_total"]))
        self.assertEqual(0, source.source_reads)
        self.assertEqual([], source.children)
        source.parent._recover_stalled_profile_page.assert_not_awaited()
        self.assertEqual({"sample_reader01": 1, "private_zero": 1}, self.navigations)
        self.assertEqual({"sample_reader01", "private_zero"}, self.table_usernames("workbench_collection_exclusions"))
        self.assertEqual(set(), self.table_usernames("workbench_candidates"))

    async def test_missing_post_count_remains_unknown_instead_of_zero_discard(self):
        timestamp = int(time.time()) - 86400
        for username, private in (("unknown_public", False), ("unknown_private", True)):
            evidence = {"user": {"username": username, "is_private": private,
                                  "edge_owner_to_timeline_media": {"edges": [{"node": {"taken_at_timestamp": timestamp}}]}}}
            self.documents[username] = (
                f'<!doctype html><html><head><meta charset="utf-8"></head><body><main>'
                f'<header><h2>{username}</h2><div>240粉丝680关注</div></header>'
                f'<p>{"这是私密账户" if private else "个人主页"}</p></main>'
                f'<script type="application/json">{json.dumps(evidence)}</script></body></html>'
            )
        state = self.task()
        source = _Source(self.context, list(self.documents))
        stats = await self.execute(state, source)
        self.assertEqual((2, 0), (stats["recorded"], stats["pending"]))
        self.assertEqual(set(), self.table_usernames("workbench_collection_exclusions"))
        self.assertEqual(set(self.documents), self.table_usernames("workbench_candidates"))
        for result in self.service.list_results(self.owner, state[0]["id"]):
            self.assertIsNone(result["profile"]["posts"])
            self.assertNotEqual("excluded_zero_posts", result["screening"].get("routing_result"))


if __name__ == "__main__":
    unittest.main()
