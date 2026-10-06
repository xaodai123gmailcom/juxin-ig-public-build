"""End-to-end durable spool dedupe: a duplicate must not call the profile reader."""
from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import WorkerExecutionError
from app.service import CoreService


class Reader:
    def __init__(self, profiles=None, forbidden=False):
        self.profiles = profiles or {}
        self.reads = []
        self.forbidden = forbidden

    async def read_visible_profile(self, username, **kwargs):
        self.reads.append(username)
        if self.forbidden:
            raise AssertionError(f"already recorded identity opened: {username}")
        return {"username": username, "visibility": "private", "followers": 10,
                "following": 20, "posts": 4, **self.profiles.get(username, {})}

    async def read_visible_account_location(self, username):
        return "United States"


class PreopenDedupeR39Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "dedupe.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user("preopen-r39", "preopen-regression-password")["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.serial = 0

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, **settings):
        self.serial += 1
        task = self.service.create_task(self.owner, name="preopen dedupe", modes=["followers"],
            targets=[f"source_{self.serial}"], settings={"gpt_enabled": False, "local_person_recognition": False, **settings})
        pause = asyncio.Event(); pause.set()
        control = ExecutionControl(owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
        return task, task["targets"][0], control

    async def drain(self, state, worker, names=None):
        task, target, control = state
        if names is not None:
            self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", names)
        return await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}), 5)

    async def test_discarded_and_reviewed_accounts_skip_before_profile_after_restart(self):
        profiles = {
            "large_public": {"visibility": "public", "followers": 4001},
            "large_private": {"posts": 4001},
            "old_posts": {"visibility": "public", "post_activity_days": 60, "post_activity_status": "identified"},
            "public_zero": {"visibility": "public", "posts": 0},
            "awaiting_review": {},
        }
        first = self.task(public_discard_active_days_max=30, exclude_public_zero_posts=True)
        original = Reader(profiles)
        stats = await self.drain(first, original, list(profiles))
        self.assertEqual((5, 0), (stats["recorded"], stats["pending"]))
        for name in profiles:
            self.assertTrue(self.service.check_global_dedupe(name)["seen"])
        with self.db.read() as c:
            before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ["global_seen", "task_results", "workbench_candidates", "workbench_collection_exclusions"]}
        self.db = Database(self.path); self.db.initialize()
        self.service = CoreService(self.db); self.manager = ExecutionManager(self.service, SimpleNamespace())
        second = self.task(local_person_recognition=True, exclude_male_avatar=True)
        # Each new source target is itself reserved globally, independently of the candidate set.
        before["global_seen"] += 1
        forbidden = Reader(forbidden=True)
        stats = await self.drain(second, forbidden, ["@" + name.upper() for name in profiles])
        self.assertEqual([], forbidden.reads)
        self.assertEqual((0, 5, 0), (stats["recorded"], stats["deduped"], stats["pending"]))
        with self.db.read() as c:
            after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        self.assertEqual(before, after)

    async def test_public_and_private_zero_posts_discard_without_activity(self):
        state = self.task(exclude_public_zero_posts=True, public_discard_active_days_max=30)
        class NoActivity(Reader):
            async def read_visible_profile(self, username, **kwargs):
                if kwargs.get("include_activity") or kwargs.get("include_post_activity"):
                    raise AssertionError("zero-post outcomes must not wait for post activity")
                return await super().read_visible_profile(username, **kwargs)
        worker = NoActivity({"zero_public": {"visibility": "public", "posts": 0},
                             "zero_private": {"visibility": "private", "posts": 0}})
        stats = await self.drain(state, worker, ["zero_public", "zero_private"])
        self.assertEqual(["zero_public", "zero_private"], worker.reads)
        self.assertEqual((2, 0), (stats["recorded"], stats["pending"]))
        saved = {row["username"]: row for row in self.service.list_results(self.owner, state[0]["id"])}
        self.assertEqual("excluded_zero_posts", saved["zero_public"]["screening"]["routing_result"])
        self.assertEqual("excluded_zero_posts", saved["zero_private"]["screening"]["routing_result"])
        self.assertTrue(self.service.check_global_dedupe("zero_public")["seen"])
        forbidden = Reader(forbidden=True)
        repeat = await self.drain(self.task(), forbidden, ["zero_public"])
        self.assertEqual([], forbidden.reads)
        self.assertEqual(1, repeat["deduped"])

    async def test_legacy_unrecognized_result_is_terminal_even_on_same_target(self):
        task, target, control = state = self.task(local_person_recognition=True)
        self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", ["old_unknown"])
        claim = self.service.claim_workbench_identity(self.owner, username="old_unknown", source="followers", source_target=target["id"])
        self.service.record_result(self.owner, task["id"], target["id"], username="old_unknown",
            instagram_user_id=None, source_mode="followers", visibility="unknown",
            profile={"person_category": "unknown", "marker": "saved-original"}, screening={}, qualified=None,
            dedupe_claim_id=claim["claim_id"])
        forbidden = Reader(forbidden=True)
        stats = await self.drain(state, forbidden)
        self.assertEqual([], forbidden.reads)
        self.assertEqual((1, 0), (stats["recorded"], stats["pending"]))
        self.assertEqual("saved-original", self.service.list_results(self.owner, task["id"])[0]["profile"]["marker"])
        self.assertFalse(self.service.check_global_dedupe("old_unknown", owner_user_id=self.owner)["person_recognition_needed"])

    async def test_registered_rename_alias_skips_without_opening_renamed_homepage(self):
        first = self.task()
        await self.drain(first, Reader({"old_name": {"instagram_user_id": "991234"}}), ["old_name"])
        alias = self.service.claim_workbench_identity(self.owner, username="new_name", instagram_user_id="991234")
        self.assertTrue(alias["duplicate"])
        forbidden = Reader(forbidden=True)
        stats = await self.drain(self.task(), forbidden, ["NEW_NAME", "@old_name"])
        self.assertEqual([], forbidden.reads)
        self.assertEqual((2, 0), (stats["deduped"], stats["pending"]))

    async def test_duplicate_discovered_before_other_target_finishes_still_skips_before_open(self):
        a, b = self.task(), self.task()
        for task, target, _ in [a, b]:
            self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", ["shared_person"])
        await self.drain(a, Reader())
        # Deliberately exercise the final atomic claim check, not only append-time dedupe.
        forbidden = Reader(forbidden=True)
        task, target, control = b
        stats = await self.manager._drain_candidate_spool(control, forbidden, target["id"], "followers", task["settings"],
            discovery_complete=True, reconcile_on_entry=False)
        self.assertEqual([], forbidden.reads)
        self.assertEqual((1, 0), (stats["deduped"], stats["pending"]))

    async def test_concurrent_windows_open_new_identity_only_once(self):
        a, b = self.task(), self.task()
        for task, target, _ in [a, b]:
            self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", ["race_person"])
        wa, wb = Reader(), Reader()
        stats = await asyncio.gather(self.drain(a, wa), self.drain(b, wb))
        self.assertEqual(1, len(wa.reads) + len(wb.reads))
        self.assertEqual(1, sum(s["recorded"] for s in stats))
        self.assertEqual(1, sum(s["deduped"] for s in stats))
        self.assertTrue(all(s["pending"] == 0 for s in stats))

    async def test_interrupted_unread_claim_stays_resumable_and_is_not_treated_as_collected(self):
        state = self.task()
        class Offline(Reader):
            async def read_visible_profile(self, username, **kwargs):
                raise WorkerExecutionError("offline", reason="worker_not_connected", pause_required=True)
        with self.assertRaises(WorkerExecutionError):
            await self.drain(state, Offline(), ["unread_person"])
        task, target, _ = state
        self.assertEqual(1, self.service.task_mode_candidate_stats(self.owner, task["id"], target["id"], "followers")["pending"])
        self.assertEqual([], self.service.list_results(self.owner, task["id"]))
        self.db = Database(self.path); self.db.initialize(); self.service = CoreService(self.db)
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        worker = Reader()
        stats = await self.drain(state, worker)
        self.assertEqual(["unread_person"], worker.reads)
        self.assertEqual((1, 0), (stats["recorded"], stats["pending"]))

    async def test_conflicting_profile_identity_stays_pending_at_counts_and_activity_then_resumes(self):
        for index, (stage, other_known) in enumerate([
            ('counts', False), ('counts', True), ('activity', False), ('activity', True)
        ]):
            with self.subTest(stage=stage, other_known=other_known):
                task, target, control = state = self.task()
                name = f'identity.{index}'
                expected_id, wrong_id = f'61000{index}', f'62000{index}'
                self.service.claim_workbench_identity(self.owner, username=name,
                    instagram_user_id=expected_id, source='followers', source_target=target['id'])
                if other_known:
                    self.service.claim_workbench_identity(self.owner, username=f'other.identity.{index}',
                        instagram_user_id=wrong_id, source='other')
                class ConflictingReader(Reader):
                    async def read_visible_profile(self, username, **kwargs):
                        profile = await super().read_visible_profile(username, **kwargs)
                        profile.update(visibility='public', activity_status='identified', activity_days=1,
                            instagram_user_id=wrong_id if stage == 'counts' or kwargs.get('include_activity') else expected_id)
                        return profile
                with self.assertRaises(WorkerExecutionError) as caught:
                    await self.drain(state, ConflictingReader(), [name])
                self.assertEqual('instagram_profile_wrong_target', caught.exception.details['original_reason'])
                stats = self.service.task_mode_candidate_stats(self.owner, task['id'], target['id'], 'followers')
                self.assertEqual((1, 0, 0), (stats['pending'], stats['recorded'], stats['deduped']))
                self.assertEqual([], self.service.list_results(self.owner, task['id']))
                self.service = CoreService(Database(self.path))
                self.manager = ExecutionManager(self.service, SimpleNamespace())
                recovered = Reader({name: {'instagram_user_id': expected_id}})
                stats = await self.drain(state, recovered)
                self.assertEqual((0, 1, 0), (stats['pending'], stats['recorded'], stats['deduped']))
                self.assertEqual([name], recovered.reads)

    async def test_invalid_profile_ids_never_bind_or_replace_a_confirmed_numeric_identity(self):
        serial = 0
        for stage in ('counts', 'activity', 'exclusion'):
            for invalid in (True, False, 0, '0', 'unknown', '１２３', None):
                with self.subTest(stage=stage, invalid=invalid):
                    serial += 1
                    state = self.task()
                    name, expected_id = f'invalid.id.{serial}', f'7300{serial}'
                    class InvalidIdReader(Reader):
                        async def read_visible_profile(self, username, **kwargs):
                            profile = await super().read_visible_profile(username, **kwargs)
                            profile.update(visibility='private' if stage == 'counts' else 'public',
                                posts=0 if stage == 'exclusion' else 4,
                                activity_status='identified', activity_days=1,
                                instagram_user_id=invalid if stage != 'activity' or kwargs.get('include_activity') else expected_id)
                            return profile
                    stats = await self.drain(state, InvalidIdReader(), [name])
                    self.assertEqual((1, 0), (stats['recorded'], stats['pending']))
                    row = self.service.list_results(self.owner, state[0]['id'])[0]
                    if stage != 'activity':
                        self.assertIsNone(row['instagram_user_id'])
                        self.assertNotIn('instagram_user_id', row['profile'])
                    else:
                        self.assertEqual(expected_id, row['instagram_user_id'])
                        self.assertEqual(expected_id, row['profile']['instagram_user_id'])
