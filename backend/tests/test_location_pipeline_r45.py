"""Actual About-dialog outcomes preserve the collection business rules in SQLite."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import time
import unittest

from app.execution_manager import ExecutionControl
import test_zero_pipeline_r45 as fixtures


def location_profile_html(username, panel, *, posts=12):
    evidence = {"user": {"username": username, "is_private": False,
                          "edge_owner_to_timeline_media": {
                              "count": posts,
                              "edges": [] if posts == 0 else [
                                  {"node": {"taken_at_timestamp": int(time.time()) - 86400}}
                              ],
                          }}}
    return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
      <main><header><h2 onclick="openAbout()">{username}</h2>
      <div><span>{posts}帖子</span><span>108粉丝</span><span>679关注</span></div><button>关注</button></header>
      <section>{"这里空荡荡～" if posts == 0 else "已发布的帖子"}</section>
      <aside><h2>为你推荐</h2><div role="progressbar">加载推荐</div></aside></main>
      <script type="application/json">{json.dumps(evidence)}</script>
      <script>window.aboutClicks=0;
      window.openAbout=()=>{{window.aboutClicks++;let d=document.createElement('div');
      d.setAttribute('role','dialog');d.id='about';d.innerHTML={json.dumps(panel)};document.body.appendChild(d)}};
      document.addEventListener('keydown',event=>{{if(event.key==='Escape')document.querySelector('#about')?.remove()}});
      </script></body></html>'''


class _LocationWorker(fixtures._ProfileWorker):
    def __init__(self, page):
        super().__init__(page)
        self.location_reads = Counter()
        self.about_opens = {}
        self.location_request_min_interval_seconds = 0
        self.location_request_max_interval_seconds = 0

    async def read_visible_account_location(self, username):
        self.location_reads[username] += 1
        result = await super().read_visible_account_location(username)
        self.about_opens[username] = await self.page.evaluate("window.aboutClicks")
        return result


class _LocationSource(fixtures._Source):
    async def create_parallel_screening_worker(self):
        child = _LocationWorker(await self.context.new_page())
        self.children.append(child)
        return child


class LocationPipelineR45Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ZeroPipelineR45Tests.asyncSetUp
    asyncTearDown = fixtures.ZeroPipelineR45Tests.asyncTearDown
    execute = fixtures.ZeroPipelineR45Tests.execute
    table_usernames = fixtures.ZeroPipelineR45Tests.table_usernames

    def task(self):
        self.task_serial += 1
        task = self.service.create_task(
            self.owner, name="About outcome pipeline", modes=["followers"],
            targets=[f"location_source_{self.task_serial}"],
            settings={"parallel_screening_workers": 2, "exclude_public_zero_posts": True,
                      "location_enabled": True, "local_person_recognition": False,
                      "gpt_enabled": False, "public_discard_active_days_max": 0},
        )
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue(),
        )
        return task, task["targets"][0], control

    async def collect(self, profiles):
        self.documents = profiles
        state = self.task()
        source = _LocationSource(self.context, list(profiles))
        stats = await self.execute(state, source)
        self.assertEqual((len(profiles), 0), (stats["recorded"], stats["pending"]))
        self.assertIsNone(source.parent, "valid About outcomes must not defer a child or retry on the source page")
        self.assertEqual([1, 1], [child.disconnect_calls for child in source.children])
        for child in source.children:
            child._recover_stalled_profile_page.assert_not_awaited()
            self.assertEqual(0, child._location_request_coordinator.blocked_until)
        saved = {row["username"]: row for row in self.service.list_results(self.owner, state[0]["id"])}
        return source, saved

    async def test_named_country_keeps_existing_us_and_non_us_routes(self):
        _, saved = await self.collect({
            "visible_us": location_profile_html("visible_us", "<h2>About this account</h2><p>Account based in</p><p>United States</p>"),
            "visible_canada": location_profile_html("visible_canada", "<h2>About this account</h2><p>Account based in</p><p>Canada</p>"),
        })
        self.assertEqual("美国", saved["visible_us"]["profile"]["location_zh"])
        self.assertTrue(saved["visible_us"]["screening"]["location"]["passed"])
        self.assertEqual("public_primary_review", saved["visible_us"]["screening"]["routing_result"])
        self.assertEqual("加拿大", saved["visible_canada"]["profile"]["location_zh"])
        self.assertEqual("excluded_non_us", saved["visible_canada"]["screening"]["routing_result"])
        self.assertEqual({"visible_us"}, self.table_usernames("workbench_candidates"))
        self.assertEqual({"visible_canada"}, self.table_usernames("workbench_collection_exclusions"))

    async def test_undisclosed_and_absent_field_are_retained_without_interrupting_queue(self):
        panels = {
            "not_shared": "<h2>About this account</h2><p>Account based in</p><p>Not shared</p>",
            "not_public": "<h2>关于此账户</h2><p>账户所在地</p><p>不公开</p>",
            "no_country_field": "<h2>About this account</h2><p>Date joined</p><p>May 2014</p>",
        }
        source, saved = await self.collect({name: location_profile_html(name, panel) for name, panel in panels.items()})
        for name, row in saved.items():
            self.assertIsNone(row["profile"]["location_zh"])
            self.assertIsNone(row["qualified"])
            gate = row["screening"]["location"]
            self.assertIsNone(gate["country"])
            self.assertIsNone(gate["passed"])
            self.assertTrue(gate["retained_for_manual_review"])
            self.assertEqual("public_location_unavailable_retained", row["screening"]["review_reason"])
            self.assertEqual("public_primary_review", row["screening"]["routing_result"])
            self.assertTrue(self.service.check_global_dedupe(name)["seen"])
        self.assertEqual(set(panels), self.table_usernames("workbench_candidates"))
        self.assertEqual(set(), self.table_usernames("workbench_collection_exclusions"))
        opens = {name: count for child in source.children for name, count in child.about_opens.items()}
        self.assertEqual({name: 1 for name in panels}, opens)
        self.assertEqual({name: 1 for name in panels}, self.navigations)

    async def test_public_zero_discards_before_location_even_when_its_panel_would_fail(self):
        source, saved = await self.collect({
            "zero_before_location": location_profile_html("zero_before_location", "<h2>About this account</h2><p>Failed to load</p>", posts=0),
            "healthy_after_zero": location_profile_html("healthy_after_zero", "<h2>About this account</h2><p>Account based in</p><p>United States</p>"),
        })
        reads = sum((child.location_reads for child in source.children), Counter())
        self.assertEqual({"healthy_after_zero": 1}, reads)
        self.assertEqual("excluded_zero_posts", saved["zero_before_location"]["screening"]["routing_result"])
        self.assertFalse(saved["zero_before_location"]["screening"]["location"]["checked"])
        self.assertEqual("public_primary_review", saved["healthy_after_zero"]["screening"]["routing_result"])
        self.assertEqual({"healthy_after_zero"}, self.table_usernames("workbench_candidates"))
        self.assertEqual({"zero_before_location"}, self.table_usernames("workbench_collection_exclusions"))


if __name__ == "__main__":
    unittest.main()
