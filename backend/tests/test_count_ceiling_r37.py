"""Configurable discard limits stay separate from qualification and task progress."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError as RequestValidationError

from app.database import Database
from app.errors import ValidationError
from app.execution_manager import ExecutionControl, ExecutionManager
from app.schemas import DesktopTaskCreateRequest, TaskSettingsRequest
from app.service import CoreService, validate_task_settings


class _Reader:
    def __init__(self, profiles):
        self.profiles = profiles
        self.reads = []

    async def read_visible_profile(self, username, *, include_activity=False):
        self.reads.append(username)
        return {"username": username, "followers": 14, "following": 18,
                "posts": 7, "visibility": "private", **self.profiles[username]}


class CountCeilingR37Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "discard.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "count-discard-r37", "count-discard-regression-password")["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.serial = 0

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, **settings):
        self.serial += 1
        task = self.service.create_task(
            self.owner, name="discard limits", modes=["followers"],
            targets=[f"source_{self.serial}"],
            settings={"local_person_recognition": False,
                      "gpt_enabled": False, **settings})
        pause = asyncio.Event()
        pause.set()
        control = ExecutionControl(
            owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
        return task, task["targets"][0], control

    async def screen(self, profile, **settings):
        task, target, control = self.task(**settings)
        username = f"candidate_{self.serial}"
        claim = self.service.claim_workbench_identity(
            self.owner, username=username, source="followers", source_target=target["id"])
        self.assertTrue(await self.manager._screen_and_record(
            control, _Reader({username: profile}), target["id"], username,
            "followers", task["settings"], claim_id=claim["claim_id"]))
        return self.service.list_results(self.owner, task["id"])[0]

    def assert_discarded(self, result, field_name, actual, maximum):
        screening = result["screening"]
        self.assertFalse(result["qualified"])
        self.assertEqual("excluded_account_count_ceiling", screening["routing_result"])
        self.assertEqual({"actual": actual, "maximum": maximum},
                         screening["count_ceiling"]["exceeded"][field_name])
        for gate in ("location", "activity", "gpt"):
            self.assertFalse(screening[gate]["checked"])

    async def test_all_three_counts_strict_boundary_for_public_and_private(self):
        for visibility in ("public", "private"):
            for field_name in ("followers", "following", "posts"):
                for count in (4000, 4001, None):
                    with self.subTest(visibility=visibility, field=field_name, count=count):
                        result = await self.screen({"visibility": visibility, field_name: count})
                        if count == 4001:
                            self.assert_discarded(result, field_name, count, 4000)
                        else:
                            self.assertNotEqual("excluded_account_count_ceiling",
                                                result["screening"].get("routing_result"))

    async def test_distinct_visibility_limits_and_disabled_field(self):
        for visibility, count, discard in (
            ("private", 25, False), ("private", 26, True),
            ("public", 25, False), ("public", 76, True),
            ("unknown", 9999, False),
        ):
            with self.subTest(visibility=visibility, count=count):
                result = await self.screen(
                    {"visibility": visibility, "followers": count},
                    private_discard_followers_max=25, public_discard_followers_max=75)
                self.assertEqual(discard, result["screening"].get("routing_result") ==
                                 "excluded_account_count_ceiling")
        for visibility in ("private", "public"):
            for field_name in ("followers", "following", "posts"):
                result = await self.screen(
                    {"visibility": visibility, field_name: 9999},
                    **{f"{visibility}_discard_{field_name}_max": 0})
                self.assertNotEqual("excluded_account_count_ceiling",
                                    result["screening"].get("routing_result"))

    async def test_discard_limits_and_existing_qualification_rules_are_independent(self):
        # Retired qualification maxima never reappear when direct discard is off.
        public = await self.screen({"visibility": "public", "followers": 5000},
                                   discard_count_limits_enabled=False, followers_max=100)
        self.assertNotEqual("excluded", public["screening"]["review_tier"])
        # Private candidates use the one retained review route.
        private = await self.screen({"visibility": "private", "followers": 5000},
                                    discard_count_limits_enabled=False, followers_max=100)
        self.assertEqual("primary", private["screening"]["review_tier"])
        for visibility in ("private", "public"):
            # Raising qualification max cannot replace the separate discard value.
            result = await self.screen(
                {"visibility": visibility, "followers": 4001}, followers_max=9000,
                mode_limits={"followers": {"followers_max": 10000}})
            self.assert_discarded(result, "followers", 4001, 4000)
            # A zero-post switch neither enables nor disables the count ceiling.
            for zero_switch in (True, False):
                result = await self.screen(
                    {"visibility": visibility, "followers": 4001, "posts": 0},
                    exclude_public_zero_posts=zero_switch)
                self.assert_discarded(result, "followers", 4001, 4000)

    async def test_exclusion_precedes_optional_avatar_location_activity_and_gpt(self):
        for visibility in ("public", "private"):
            task, target, control = self.task(
                local_person_recognition=True, gpt_enabled=True, active_days_max=30)
            username = f"large_{self.serial}"
            claim = self.service.claim_workbench_identity(
                self.owner, username=username, source="followers", source_target=target["id"])
            case = self

            class NoOptionalReads(_Reader):
                supports_avatar_image_capture = True

                async def capture_visible_review_snapshot(self, *_args, **_kwargs):
                    case.fail("discarded candidate must not capture avatar/review data")

                async def read_visible_location_country(self, *_args, **_kwargs):
                    case.fail("discarded candidate must not read location")

                async def read_visible_profile_activity(self, *_args, **_kwargs):
                    case.fail("discarded candidate must not read activity")

            async def forbidden_recognition(*_args, **_kwargs):
                case.fail("discarded candidate must not invoke recognition")

            original_recognition = self.manager._apply_person_recognition
            self.manager._apply_person_recognition = forbidden_recognition
            try:
                await self.manager._screen_and_record(
                    control, NoOptionalReads({username: {"visibility": visibility, "posts": 4001}}),
                    target["id"], username, "followers", task["settings"], claim_id=claim["claim_id"])
            finally:
                self.manager._apply_person_recognition = original_recognition
            self.assert_discarded(self.service.list_results(self.owner, task["id"])[0], "posts", 4001, 4000)

    async def test_durable_exclusions_advance_and_restart_does_not_revisit(self):
        task, target, control = self.task()
        profiles = {"large_public": {"visibility": "public", "followers": 4001},
                    "large_private": {"visibility": "private", "following": 4001},
                    "eligible_next": {"visibility": "private", "posts": 4000}}
        self.service.append_task_mode_candidates(
            self.owner, task["id"], target["id"], "followers", list(profiles))
        worker = _Reader(profiles)
        checkpoint = {"cursor": {"candidate_spool_complete": True,
                                 "candidate_spool_natural_end": True}}
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"], checkpoint), 10)
        self.assertEqual((3, 0), (result["recorded"], result["pending"]))
        self.assertEqual(list(profiles), worker.reads)
        with self.database.read() as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions WHERE reason_code = ?",
                ("account_count_ceiling_exceeded",)).fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM workbench_candidates").fetchone()[0])
        restored = ExecutionManager(CoreService(self.database), SimpleNamespace())
        await restored._execute_candidate_spooled_mode(
            control, worker, target, "followers", task["settings"], checkpoint)
        self.assertEqual(list(profiles), worker.reads)

    async def test_settings_persist_without_mixing_qualification_limits(self):
        custom = {"discard_count_limits_enabled": False,
                  "private_discard_followers_max": 21, "private_discard_following_max": 22,
                  "private_discard_posts_max": 0, "public_discard_followers_max": 51,
                  "public_discard_following_max": 52, "public_discard_posts_max": 53,
                  "followers_max": 80, "mode_limits": {"followers": {"posts_max": 100}}}
        task, _target, _control = self.task(**custom)
        restored = CoreService(self.database).get_task(self.owner, task["id"])
        for key, value in custom.items():
            self.assertEqual(value, restored["settings"][key])
        request = DesktopTaskCreateRequest(
            targets=["source"], window_ids=["window"], modes=["followers"],
            **{key: value for key, value in custom.items() if "discard_" in key})
        for key, value in custom.items():
            if "discard_" in key:
                self.assertEqual(value, request.model_dump()[key])

    async def test_settings_validate_explicit_zero_without_coercing_invalid_values(self):
        for model in (TaskSettingsRequest, DesktopTaskCreateRequest):
            base = {} if model is TaskSettingsRequest else {"window_ids": ["w"], "modes": ["followers"]}
            for field_name in ("private_discard_followers_max", "private_discard_following_max",
                               "private_discard_posts_max", "public_discard_followers_max",
                               "public_discard_following_max", "public_discard_posts_max"):
                self.assertEqual(4000, model(**base).model_dump()[field_name])
                self.assertEqual(0, model(**base, **{field_name: 0}).model_dump()[field_name])
                for invalid in (-1, None, True, 1.5, "4000"):
                    with self.subTest(model=model.__name__, field=field_name, invalid=invalid):
                        with self.assertRaises(RequestValidationError):
                            model(**base, **{field_name: invalid})
                        with self.assertRaises(ValidationError):
                            validate_task_settings({field_name: invalid})
            with self.assertRaises(RequestValidationError):
                model(**base, discard_count_limits_enabled="false")
        with self.assertRaises(ValidationError):
            validate_task_settings({"discard_count_limits_enabled": 0})

    async def test_desktop_adapter_preserves_seven_fields_and_separate_source_limits(self):
        import ast
        import copy

        # Execute the current production route body with a real validated request
        # and real CoreService. This isolates mapping from optional HTTP/server
        # packages; HTTP transport and background browser scheduling are not used.
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
        configured = {"discard_count_limits_enabled": False,
                      "private_discard_followers_max": 91, "private_discard_following_max": 92,
                      "private_discard_posts_max": 0, "public_discard_followers_max": 191,
                      "public_discard_following_max": 192, "public_discard_posts_max": 193}
        body = DesktopTaskCreateRequest(
            targets=["source_adapter"], window_ids=["window_adapter"], modes=["followers"],
            source_limits={"followers": {"followersMax": 88, "postsMax": 77}}, **configured)
        response = namespace["desktop_create_task"](body, ({"id": self.owner}, "unused"))
        saved = self.service.get_task(self.owner, response["task_id"])["settings"]
        for key, value in configured.items():
            self.assertEqual(value, saved[key])
        self.assertEqual({"followers_max": 88, "posts_max": 77}, saved["mode_limits"]["followers"])


if __name__ == "__main__":
    unittest.main()
