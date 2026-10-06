"""Public activity discard is independent, durable, and shares the existing read."""
from __future__ import annotations

import ast
import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError as RequestValidationError

from app.database import Database
from app.errors import ValidationError
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import WorkerExecutionError
from app.schemas import DesktopTaskCreateRequest, TaskSettingsRequest
from app.service import CoreService, validate_task_settings


FIELD = "public_discard_active_days_max"
DISCARD = "excluded_public_activity_ceiling"


class ActivityReader:
    def __init__(self, profiles):
        self.profiles = profiles
        self.reads = []

    async def read_visible_profile(self, username, *, include_activity=False):
        self.reads.append((username, include_activity))
        profile = {"username": username, "followers": 14, "following": 18,
                   "posts": 7, "visibility": "public", "activity_status": "identified",
                   **self.profiles[username]}
        if not include_activity:
            for key in ("activity_days", "activity_status", "recent_post_datetime"):
                profile.pop(key, None)
        return profile

    async def read_visible_account_location(self, username):
        return "United States"


class PublicActivityDiscardR38Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "activity-discard.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "activity-discard-r38", "activity-discard-regression-password")["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.serial = 0

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, **settings):
        self.serial += 1
        task = self.service.create_task(
            self.owner, name="activity discard", modes=["followers"],
            targets=[f"source_{self.serial}"],
            settings={"local_person_recognition": False, "gpt_enabled": False,
                      **settings})
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
        return task, task["targets"][0], control

    async def screen(self, profile, *, reader_class=ActivityReader, **settings):
        task, target, control = self.task(**settings)
        username = f"candidate_{self.serial}"
        worker = reader_class({username: profile})
        claim = self.service.claim_workbench_identity(
            self.owner, username=username, source="followers", source_target=target["id"])
        self.assertTrue(await self.manager._screen_and_record(
            control, worker, target["id"], username, "followers", task["settings"],
            claim_id=claim["claim_id"]))
        return self.service.list_results(self.owner, task["id"])[0], worker

    def source_for_screening_child(self, child):
        # Exercise the production profile reader in its new child-page role.
        # The source is already durably complete and must not read any profile.
        return SimpleNamespace(supports_single_candidate_handoff=True,
            supports_parallel_screening_tab=True,
            create_parallel_screening_worker=AsyncMock(return_value=child),
            read_visible_profile=AsyncMock(side_effect=AssertionError('profile read on parent')))

    async def test_only_strictly_older_known_public_activity_is_discarded(self):
        for days, discarded in ((0, False), (29, False), (30, False), (31, True)):
            with self.subTest(days=days):
                result, worker = await self.screen(
                    {"activity_days": days, "activity_status": "timestamp_read"},
                    **{FIELD: 30})
                self.assertEqual(discarded, result["screening"].get("routing_result") == DISCARD)
                self.assertEqual(1, sum(active for _name, active in worker.reads))
                self.assertFalse(result["screening"]["activity"]["enabled"])
                self.assertEqual(not discarded, result["screening"]["activity_ceiling"]["passed"])
                if discarded:
                    self.assertFalse(result["qualified"])

    async def test_switch_off_and_zero_disable_discard_without_extra_reads(self):
        for settings in ({}, {FIELD: 0}, {FIELD: 30, "discard_count_limits_enabled": False}):
            with self.subTest(settings=settings):
                result, worker = await self.screen({"activity_days": 365}, **settings)
                self.assertNotEqual(DISCARD, result["screening"].get("routing_result"))
                self.assertFalse(result["screening"]["activity_ceiling"]["enabled"])
                # Existing activity evidence for review is still collected once.
                self.assertEqual([False, True], [active for _name, active in worker.reads])

    async def test_private_and_unknown_privacy_do_not_use_public_discard(self):
        for visibility in ("private", "unknown"):
            with self.subTest(visibility=visibility):
                result, worker = await self.screen(
                    {"visibility": visibility, "activity_days": 365}, **{FIELD: 30})
                self.assertNotEqual(DISCARD, result["screening"].get("routing_result"))
                self.assertFalse(result["screening"]["activity_ceiling"]["enabled"])
                if visibility == "private":
                    self.assertEqual("private_collected", result["screening"]["stage"])
                    self.assertEqual([False], [active for _name, active in worker.reads])

    async def test_unknown_activity_and_no_posts_never_prove_discard(self):
        for profile in (
            {"activity_days": None, "activity_status": "timestamp_unavailable"},
            {"posts": 0, "activity_days": None, "activity_status": "no_posts"},
            {"posts": 0, "activity_days": 365, "activity_status": "no_posts"},
            {"activity_days": 365, "activity_status": "no_posts"},
            {"activity_days": -1},
            {"activity_days": True},
        ):
            with self.subTest(profile=profile):
                result, _worker = await self.screen(profile, **{FIELD: 30})
                self.assertNotEqual(DISCARD, result["screening"].get("routing_result"))
                self.assertIsNone(result["screening"]["activity_ceiling"]["passed"])

    async def test_post_only_read_is_requested_once_and_story_cannot_override_post_age(self):
        class PostReader(ActivityReader):
            async def read_visible_profile(self, username, *, include_activity=False,
                                           include_post_activity=False):
                self.post_request = getattr(self, "post_request", []) + [include_post_activity]
                return await super().read_visible_profile(username, include_activity=include_activity)

        for configured, expected_requests, discarded in (
            ({FIELD: 30}, [False, True], True),
            ({FIELD: 0}, [False, False], False),
            ({FIELD: 30, "discard_count_limits_enabled": False}, [False, False], False),
        ):
            with self.subTest(configured=configured):
                result, worker = await self.screen(
                    {"activity_days": 0, "activity_status": "story_today",
                     "post_activity_days": 60, "post_activity_status": "identified"},
                    reader_class=PostReader, **configured)
                self.assertEqual(expected_requests, worker.post_request)
                self.assertEqual(discarded, result["screening"].get("routing_result") == DISCARD)

    async def test_legacy_story_requires_actual_post_timestamp_not_story_zero(self):
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        for extra, expected in (({}, False), ({"recent_post_datetime": old_timestamp}, True),
                                ({"recent_post_datetime": "bad timestamp"}, False)):
            with self.subTest(extra=extra):
                result, worker = await self.screen(
                    {"activity_days": 0, "activity_status": "story_today", **extra},
                    **{FIELD: 30})
                self.assertEqual(expected, result["screening"].get("routing_result") == DISCARD)
                self.assertEqual(1, sum(active for _name, active in worker.reads))

    async def test_unknown_or_missing_evidence_status_cannot_reuse_stale_days(self):
        for profile in (
            {"activity_days": 365, "activity_status": "post_grid_unavailable"},
            {"activity_days": 365, "activity_status": "timestamp_unavailable"},
            {"activity_days": 365, "activity_status": None},
            {"activity_days": 365, "activity_status": "not_checked"},
            {"activity_days": 0, "activity_status": "story_today",
             "post_activity_days": 365, "post_activity_status": "timestamp_unavailable"},
            {"activity_days": 365, "activity_status": "identified",
             "post_activity_days": 365, "post_activity_status": "post_grid_unavailable"},
        ):
            with self.subTest(profile=profile):
                result, _worker = await self.screen(profile, **{FIELD: 30})
                self.assertNotEqual(DISCARD, result["screening"].get("routing_result"))
                self.assertIsNone(result["screening"]["activity_ceiling"]["passed"])

    async def test_zero_post_switch_is_independent_and_precedes_activity(self):
        for visibility in ("public", "private"):
            with self.subTest(visibility=visibility):
                result, worker = await self.screen(
                    {"visibility": visibility, "posts": 0, "activity_days": 365},
                    exclude_public_zero_posts=True, **{FIELD: 30})
                self.assertEqual("excluded_zero_posts", result["screening"]["routing_result"])
                self.assertEqual([False], [active for _name, active in worker.reads])
                result, _worker = await self.screen(
                    {"visibility": visibility, "posts": 0, "activity_days": None,
                     "activity_status": "no_posts"},
                    exclude_public_zero_posts=False, **{FIELD: 30})
                self.assertEqual("primary", result["screening"]["review_tier"])

    async def test_retired_qualification_activity_never_filters_when_discard_is_disabled(self):
        for configured in ({FIELD: 0}, {FIELD: 5, "discard_count_limits_enabled": False}):
            with self.subTest(configured=configured):
                result, worker = await self.screen(
                    {"activity_days": 31}, active_days_max=30, **configured)
                self.assertNotEqual("excluded", result["screening"]["review_tier"])
                self.assertFalse(result["screening"]["activity"]["enabled"])
                self.assertFalse(result["screening"]["activity_ceiling"]["enabled"])
                self.assertEqual(1, sum(active for _name, active in worker.reads))

    async def test_discard_is_the_only_activity_limit_and_reads_once(self):
        for discard_max, qualify_max, expected in (
            (30, 100, DISCARD),
            (100, 30, None),
            (100, 100, None),
        ):
            with self.subTest(discard=discard_max, qualify=qualify_max):
                result, worker = await self.screen(
                    {"activity_days": 31}, **{FIELD: discard_max}, active_days_max=300,
                    mode_limits={"followers": {"active_days_max": qualify_max}})
                if expected:
                    self.assertEqual(expected, result["screening"].get("routing_result"))
                else:
                    self.assertNotEqual("excluded", result["screening"]["review_tier"])
                self.assertEqual(1, sum(active for _name, active in worker.reads))

    async def test_unknown_activity_is_retained_despite_retired_qualification_limit(self):
        result, _worker = await self.screen(
            {"activity_days": None, "activity_status": "timestamp_unavailable"},
            active_days_max=30, **{FIELD: 60})
        self.assertNotEqual("excluded", result["screening"]["review_tier"])
        self.assertIsNone(result["screening"]["activity_ceiling"]["passed"])

    async def test_retired_global_and_mode_count_bounds_never_supply_extra_gates(self):
        result, worker = await self.screen(
            {"followers": 500, "following": 20, "posts": 7, "activity_days": 2},
            followers_min=1000, following_max=1, posts_max=1,
            mode_limits={"followers": {"followers_max": 10, "following_min": 300,
                                       "posts_min": 200}},
            public_discard_followers_max=1000, **{FIELD: 30})
        self.assertNotEqual("excluded", result["screening"]["review_tier"])
        self.assertTrue(result["screening"]["basic"]["passed"])
        self.assertEqual([], result["screening"]["basic"]["reason_codes"])
        self.assertEqual([False, True], [active for _name, active in worker.reads])

    async def test_read_failure_and_cancellation_never_become_activity_discards(self):
        for failure in (
            WorkerExecutionError("worker closed", reason="worker_not_connected", pause_required=True),
            asyncio.CancelledError(),
        ):
            with self.subTest(failure=type(failure).__name__):
                task, target, control = self.task(**{FIELD: 30})
                username = f"failure_{self.serial}"
                claim = self.service.claim_workbench_identity(
                    self.owner, username=username, source="followers", source_target=target["id"])

                class BrokenReader(ActivityReader):
                    async def read_visible_profile(self, username, *, include_activity=False):
                        if include_activity:
                            raise failure
                        return await super().read_visible_profile(username, include_activity=False)

                with self.assertRaises(type(failure)):
                    await self.manager._screen_and_record(
                        control, BrokenReader({username: {}}), target["id"], username,
                        "followers", task["settings"], claim_id=claim["claim_id"])
                self.assertEqual([], self.service.list_results(self.owner, task["id"]))
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions").fetchone()[0])

    async def test_durable_discard_advances_and_restart_never_revisits(self):
        task, target, control = self.task(**{FIELD: 30})
        profiles = {"old_public": {"activity_days": 31},
                    "boundary_public": {"activity_days": 30},
                    "private_next": {"visibility": "private", "activity_days": 365}}
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target["id"], "followers", list(profiles))
        checkpoint = {"cursor": {"candidate_spool_complete": True,
                                 "candidate_spool_natural_end": True}}
        worker = ActivityReader(profiles)
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"], checkpoint), 10)
        self.assertEqual((3, 0), (result["recorded"], result["pending"]))
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions WHERE reason_code = ?",
                ("public_activity_ceiling_exceeded",)).fetchone()[0])
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM workbench_candidates").fetchone()[0])
        completed_reads = list(worker.reads)
        await ExecutionManager(CoreService(self.database), SimpleNamespace())._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"], checkpoint)
        self.assertEqual(completed_reads, worker.reads)

    async def test_production_post_reader_and_sqlite_pipeline_discard_old_post_then_continue(self):
        from test_public_post_activity_reader_r38 import PublicPostActivityReaderR38Tests
        from app.playwright_worker import VisibleProfile

        # Real production reader + executor, with a controlled DOM and timestamp
        # source; unrelated country/avatar reading is isolated from this contract.
        worker = PublicPostActivityReaderR38Tests().worker(story=True, post_age=60)
        worker.read_visible_account_location = AsyncMock(return_value="United States")
        worker.capture_visible_review_snapshot = AsyncMock(return_value={})
        worker._remember_profile("target", VisibleProfile("target", "public", 10, 20, 4))
        # This case verifies progression to a retained private account. The saved
        # default excludes zero posts for both public and private accounts.
        worker._remember_profile("private_next", VisibleProfile("private_next", "private", 12, 20, 1))
        task, target, control = self.task(**{FIELD: 30})
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target["id"], "followers", ["target", "private_next"])
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, self.source_for_screening_child(worker), target, "followers", task["settings"],
            {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}},
        ), 10)
        self.assertEqual((2, 0), (result["recorded"], result["pending"]))
        results = {row["username"]: row for row in self.service.list_results(self.owner, task["id"])}
        self.assertEqual(DISCARD, results["target"]["screening"]["routing_result"])
        self.assertEqual((0, 60), (results["target"]["profile"]["activity_days"],
                                  results["target"]["profile"]["post_activity_days"]))
        self.assertEqual("primary", results["private_next"]["screening"]["review_tier"])
        worker._read_original_post_datetime.assert_awaited_once()
        worker._replace_stuck_page_once.assert_not_awaited()

    async def test_production_private_zero_switch_controls_admission_and_durable_queue(self):
        from test_public_post_activity_reader_r38 import PublicPostActivityReaderR38Tests
        from app.playwright_worker import VisibleProfile

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                task, target, control = self.task(
                    exclude_public_zero_posts=enabled, **{FIELD: 30})
                zero, retained = f"private_zero_{self.serial}", f"private_kept_{self.serial}"
                worker = PublicPostActivityReaderR38Tests().worker(story=True, post_age=60)
                worker.read_visible_account_location = AsyncMock(return_value="United States")
                worker.capture_visible_review_snapshot = AsyncMock(return_value={})
                worker._remember_profile(zero, VisibleProfile(zero, "private", 12, 20, 0))
                worker._remember_profile(retained, VisibleProfile(retained, "private", 12, 20, 1))
                self.service.append_task_mode_candidates(
                    self.owner, task["id"], target["id"], "followers", [zero, retained])
                checkpoint = {"cursor": {"candidate_spool_complete": True,
                                         "candidate_spool_natural_end": True}}
                source = self.source_for_screening_child(worker)
                result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
                    control, source, target, "followers", task["settings"], checkpoint), 10)
                self.assertEqual((2, 0), (result["recorded"], result["pending"]))
                rows = {row["username"]: row for row in
                        self.service.list_results(self.owner, task["id"])}
                self.assertEqual("excluded" if enabled else "primary",
                                 rows[zero]["screening"]["review_tier"])
                self.assertEqual(enabled,
                    rows[zero]["screening"].get("routing_result") == "excluded_zero_posts")
                self.assertEqual("primary", rows[retained]["screening"]["review_tier"])
                with self.database.read() as connection:
                    admitted = connection.execute(
                        "SELECT a.current_username_norm FROM workbench_candidates c "
                        "JOIN instagram_accounts a ON a.id = c.account_id "
                        "WHERE a.current_username_norm IN (?, ?)", (zero, retained)).fetchall()
                self.assertCountEqual([retained] if enabled else [zero, retained],
                                      [row[0] for row in admitted])
                worker._read_original_post_datetime.assert_not_awaited()
                worker._replace_stuck_page_once.assert_not_awaited()
                worker._navigate_profile_with_privacy.assert_not_awaited()

                # Reopening the database must preserve terminal queue entries;
                # neither excluded nor admitted accounts may be visited again.
                self.database.initialize()
                restored = CoreService(self.database)
                worker.read_visible_profile = AsyncMock(
                    side_effect=AssertionError("A drained queue must not revisit a profile"))
                await asyncio.wait_for(ExecutionManager(restored, SimpleNamespace()).
                    _execute_candidate_spooled_mode(control, source, target, "followers",
                                                   task["settings"], checkpoint), 10)
                worker.read_visible_profile.assert_not_awaited()
                self.assertEqual(2, len(restored.list_results(self.owner, task["id"])))

    async def test_schema_validation_defaults_and_task_persistence_are_independent(self):
        self.assertEqual(0, validate_task_settings({})[FIELD])
        for model in (TaskSettingsRequest, DesktopTaskCreateRequest):
            base = {} if model is TaskSettingsRequest else {"window_ids": ["w"], "modes": ["followers"]}
            self.assertEqual(0, model(**base).model_dump()[FIELD])
            for invalid in (-1, None, True, 1.5, "30"):
                with self.subTest(model=model.__name__, invalid=invalid):
                    with self.assertRaises(RequestValidationError):
                        model(**base, **{FIELD: invalid})
                    with self.assertRaises(ValidationError):
                        validate_task_settings({FIELD: invalid})
        task, _target, _control = self.task(
            **{FIELD: 61}, active_days_max=90,
            mode_limits={"followers": {"active_days_max": 120}})
        restored = CoreService(self.database).get_task(self.owner, task["id"])["settings"]
        self.assertEqual((61, 90, 120), (restored[FIELD], restored["active_days_max"],
                         restored["mode_limits"]["followers"]["active_days_max"]))

    async def test_desktop_route_maps_activity_discard_separately_from_qualification(self):
        # Current route body, real validated request, real SQLite service; no HTTP
        # transport or unavailable FastAPI server installation is claimed here.
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        module = ast.parse(main_path.read_text(encoding="utf-8"))
        handler = copy.deepcopy(next(node for node in ast.walk(module)
                                     if isinstance(node, ast.FunctionDef)
                                     and node.name == "desktop_create_task"))
        handler.decorator_list = []
        handler.returns = None
        for argument in handler.args.args:
            argument.annotation = None
        route_module = ast.Module(body=[handler], type_ignores=[])
        namespace = {"service": self.service}
        exec(compile(ast.fix_missing_locations(route_module), str(main_path), "exec"), namespace)
        body = DesktopTaskCreateRequest(
            targets=["source_adapter"], window_ids=["window_adapter"], modes=["followers"],
            source_limits={"followers": {"activityDays": 90}}, **{FIELD: 60})
        response = namespace["desktop_create_task"](body, ({"id": self.owner}, "unused"))
        saved = self.service.get_task(self.owner, response["task_id"])["settings"]
        self.assertEqual(60, saved[FIELD])
        self.assertEqual(90, saved["mode_limits"]["followers"]["active_days_max"])


if __name__ == "__main__":
    unittest.main()
