"""The missing zero-post option means the same thing at every task boundary."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.schemas import DesktopTaskCreateRequest, TaskSettingsRequest
from app.service import CoreService, validate_task_settings


FIELD = "exclude_public_zero_posts"


class Reader:
    def __init__(self, profiles):
        self.profiles = profiles
        self.reads = []

    async def read_visible_profile(self, username, *, include_activity=False):
        self.reads.append((username, include_activity))
        return {"username": username, "visibility": "public", "followers": 10,
                "following": 20, "posts": 0, "activity_days": None,
                "activity_status": "no_posts", **self.profiles[username]}

    async def read_visible_account_location(self, username):
        return "United States"


class ZeroPublicDefaultR39Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "zero-default.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user("zero-default-r39", "zero-default-regression-password")["id"]
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.serial = 0

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, settings=None):
        self.serial += 1
        return self.service.create_task(self.owner, name="zero defaults", modes=["followers"],
            targets=[f"source_{self.serial}"], settings=settings or {})

    def control(self, task):
        pause = asyncio.Event()
        pause.set()
        return ExecutionControl(owner_user_id=self.owner, task_id=task["id"], pause_event=pause,
            stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())

    async def screen(self, task, username, profile, settings=None):
        target = task["targets"][0]
        worker = Reader({username: profile})
        claim = self.service.claim_workbench_identity(self.owner, username=username,
            source="followers", source_target=target["id"])
        self.assertTrue(await self.manager._screen_and_record(self.control(task), worker,
            target["id"], username, "followers", task["settings"] if settings is None else settings,
            claim_id=claim["claim_id"]))
        result = next(row for row in self.service.list_results(self.owner, task["id"])
                      if row["username"] == username)
        return result, worker

    def assert_discarded_and_seen(self, username, result, worker):
        self.assertEqual("excluded_zero_posts", result["screening"]["routing_result"])
        self.assertFalse(result["qualified"])
        self.assertEqual([(username, False)], worker.reads)
        self.assertTrue(self.service.check_global_dedupe(username)["seen"])
        with self.db.read() as connection:
            row = connection.execute("SELECT reason_code FROM workbench_collection_exclusions "
                                     "WHERE username_display=?", (username,)).fetchone()
        self.assertEqual("public_zero_posts_excluded", row["reason_code"])

    async def test_generic_request_default_persists_and_discards_confirmed_public_zero(self):
        self.assertTrue(TaskSettingsRequest().exclude_public_zero_posts)
        self.assertTrue(validate_task_settings({})[FIELD])
        task = self.task(TaskSettingsRequest().model_dump())
        self.assertTrue(self.service.get_task(self.owner, task["id"])["settings"][FIELD])
        result, worker = await self.screen(task, "generic_zero", {})
        self.assert_discarded_and_seen("generic_zero", result, worker)

    async def test_desktop_route_preserves_default_and_explicit_false(self):
        # Run the production route body with validated requests and SQLite. This
        # exercises request mapping, without claiming an HTTP server integration.
        main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
        module = ast.parse(main_path.read_text(encoding="utf-8"))
        handler = copy.deepcopy(next(node for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef) and node.name == "desktop_create_task"))
        handler.decorator_list = []
        handler.returns = None
        for argument in handler.args.args:
            argument.annotation = None
        namespace = {"service": self.service}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                     str(main_path), "exec"), namespace)
        for explicit in (None, False):
            options = {} if explicit is None else {FIELD: explicit}
            name = "desktop_default" if explicit is None else "desktop_disabled"
            body = DesktopTaskCreateRequest(targets=[f"source_{name}"], modes=["followers"],
                window_ids=[f"window_{name}"], local_person_recognition=False, exclude_male_avatar=False, **options)
            response = namespace["desktop_create_task"](body, ({"id": self.owner}, "unused"))
            task = self.service.get_task(self.owner, response["task_id"])
            self.assertEqual(explicit is None, task["settings"][FIELD])
            result, worker = await self.screen(task, name, {})
            if explicit is None:
                self.assert_discarded_and_seen(name, result, worker)
            else:
                self.assertNotEqual("excluded_zero_posts", result["screening"].get("routing_result"))

    async def test_restored_old_task_missing_field_defaults_without_rewriting_explicit_false(self):
        for explicit in (None, False):
            task = self.task()
            original = dict(task["settings"])
            if explicit is None:
                original.pop(FIELD)
            else:
                original[FIELD] = False
            with self.db.write() as connection:
                connection.execute("UPDATE tasks SET settings_json=? WHERE id=?",
                                   (json.dumps(original), task["id"]))
            restored = CoreService(self.db).get_task(self.owner, task["id"])
            self.assertEqual(explicit is None, restored["settings"][FIELD])
            name = "legacy_missing" if explicit is None else "legacy_disabled"
            result, worker = await self.screen(restored, name, {})
            if explicit is None:
                self.assert_discarded_and_seen(name, result, worker)
            else:
                self.assertNotEqual("excluded_zero_posts", result["screening"].get("routing_result"))
            with self.db.read() as connection:
                raw = json.loads(connection.execute("SELECT settings_json FROM tasks WHERE id=?",
                                                    (task["id"],)).fetchone()[0])
            self.assertEqual(original, raw, "reading old settings must not rewrite the saved task")

    async def test_executor_missing_field_still_discards_without_optional_reads(self):
        task = self.task()
        settings = dict(task["settings"])
        settings.pop(FIELD)
        settings.update(public_discard_active_days_max=30, local_person_recognition=True,
                        gpt_enabled=True)
        result, worker = await self.screen(task, "raw_missing_zero", {}, settings)
        self.assert_discarded_and_seen("raw_missing_zero", result, worker)

    async def test_explicit_false_generic_request_and_service_retain_public_zero(self):
        request = TaskSettingsRequest(exclude_public_zero_posts=False)
        self.assertFalse(validate_task_settings(request.model_dump())[FIELD])
        task = self.task(request.model_dump())
        result, _worker = await self.screen(task, "opted_out", {})
        self.assertNotEqual("excluded_zero_posts", result["screening"].get("routing_result"))
        with self.db.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM workbench_candidates candidate "
                "JOIN instagram_accounts account ON account.id=candidate.account_id "
                "WHERE account.current_username_norm='opted_out'").fetchone()[0])

    async def test_unknown_count_and_unknown_visibility_are_retained(self):
        task = self.task()
        for name, profile in (
            ("private_unknown", {"visibility": "private", "posts": None}),
            ("unknown_count", {"posts": None}),
            ("unknown_visibility", {"visibility": "unknown"}),
            ("invalid_boolean_count", {"posts": False}),
        ):
            with self.subTest(name=name):
                result, _worker = await self.screen(task, name, profile)
                self.assertNotEqual("excluded_zero_posts", result["screening"].get("routing_result"))
        with self.db.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM workbench_collection_exclusions").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM workbench_candidates candidate "
                "JOIN instagram_accounts account ON account.id=candidate.account_id "
                "WHERE account.current_username_norm='private_unknown'").fetchone()[0])

    async def test_default_zero_discard_advances_and_dedupes_before_open_after_restart(self):
        task = self.task()
        target = task["targets"][0]
        names = ["empty_public", "private_next"]
        self.service.append_task_mode_candidates(self.owner, task["id"], target["id"], "followers", names)
        reader = Reader({"empty_public": {}, "private_next": {"visibility": "private"}})
        checkpoint = {"cursor": {"candidate_spool_complete": True, "candidate_spool_natural_end": True}}
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            self.control(task), reader, target, "followers", task["settings"], checkpoint), 5)
        self.assertEqual((2, 0), (result["recorded"], result["pending"]))
        self.assertEqual([(name, False) for name in names], reader.reads)
        self.db = Database(self.path)
        self.db.initialize()
        self.service = CoreService(self.db)
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        second = self.task()
        second_target = second["targets"][0]
        self.service.append_task_mode_candidates(self.owner, second["id"], second_target["id"], "followers", names)
        class MustNotOpen(Reader):
            async def read_visible_profile(self, username, **kwargs):
                raise AssertionError(f"previously recorded profile was reopened: {username}")
        result = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
            self.control(second), MustNotOpen({}), second_target, "followers", second["settings"], checkpoint), 5)
        self.assertEqual((0, 2, 0), (result["recorded"], result["deduped"], result["pending"]))


if __name__ == "__main__":
    unittest.main()
