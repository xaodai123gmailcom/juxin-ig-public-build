from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The repository workflow invokes discovery from the project root. Keep the backend
# source directory importable without requiring an editable install first.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from support.legacy_bitbrowser_fixture import BitBrowserClient
from support.legacy_split_fixture import create_legacy_task
from app.action_manager import (
    ActionCampaignManager,
    choose_greeting_message,
    choose_interval_seconds,
    parse_interval,
)
from app.database import Database
from app.errors import AuthenticationError, BitBrowserAuthRequiredError, BitBrowserRateLimitedError, ConflictError, InvalidTransitionError, NotFoundError, UpstreamUnavailableError, ValidationError
from app.playwright_worker import (
    ActionOutcome,
    CollectionOutcome,
    PlaywrightWorker,
    VisibleProfile,
    WorkerExecutionError,
    build_instagram_post_likers_url,
    classify_guard_state,
    classify_profile_visibility,
    classify_visible_collection_surface,
    extract_embedded_profile_evidence,
    extract_embedded_profile_privacy,
    extract_about_account_location,
    external_profile_link_url,
    extract_instagram_profile_username,
    extract_public_empty_profile_metrics,
    extract_visible_metrics,
    has_visible_zero_likes,
    is_about_account_load_failure,
    is_real_estate_account_category,
    is_post_likers_trigger_candidate,
    is_exact_profile_relation_href,
    select_original_post_datetime,
    translate_location_to_zh,
)
from app.execution_manager import ExecutionManager, _normalized_known_location
from app.person_recognition import PersonRecognition
from app.openai_review import ReviewDecision
from app.schemas import DesktopTaskCreateRequest, TaskSettingsRequest
from app.security import hash_password, verify_password
from app.service import CoreService


PASSWORD = "correct horse battery staple"


class CoreServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "collector.sqlite3"
        self.database = Database(self.db_path)
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_user(self, username: str) -> dict:
        return self.service.register_user(username, PASSWORD)

    def create_task(self, user_id: str, target: str = "@source_account") -> dict:
        return self.service.create_task(
            user_id,
            name="采集任务",
            modes=["followers", "following", "post_likers"],
            targets=[target, target.upper()],
            settings={
                "followers_max": 3000,
                "following_max": 2000,
                "posts_max": 500,
                "active_days_max": 30,
                "like_posts_to_check": 5,
            },
        )

    def test_unknown_location_placeholders_are_not_treated_as_countries(self) -> None:
        for value in (
            None,
            "",
            "unknown",
            "not shown",
            "not provided",
            "未知",
            "未显示",
            "无法确认",
            "不可用",
            "未提供",
            "未公开",
            "未填写",
        ):
            with self.subTest(value=value):
                self.assertIsNone(_normalized_known_location(value))
        self.assertEqual("美国", _normalized_known_location(" 美国 "))

    def test_password_hash_and_session_revocation(self) -> None:
        encoded = hash_password(PASSWORD)
        self.assertNotIn(PASSWORD, encoded)
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        user = self.create_user("Alice")
        with self.assertRaises(ConflictError):
            self.create_user("alice")
        login = self.service.login("ALICE", PASSWORD, auto_login=True)
        self.assertEqual(user["id"], login["user"]["id"])
        self.assertTrue(login["remember_login"])
        self.assertEqual(user["id"], self.service.authenticate(login["token"])["id"])
        self.service.logout(login["token"])
        with self.assertRaises(AuthenticationError):
            self.service.authenticate(login["token"])

    def test_completed_source_requires_confirmation_and_keeps_original_history(self) -> None:
        user = self.create_user('completed-source-owner')
        original = self.service.create_task(user['id'],name='first',modes=['followers'],targets=['Source.Done'],settings={})
        self.service.set_target_runtime_status(user['id'],original['id'],original['targets'][0]['id'],'completed')
        before=self.service.get_task(user['id'],original['id'])
        with self.assertRaises(ConflictError):
            self.service.create_task(user['id'],name='unconfirmed',modes=['followers'],targets=['SOURCE.DONE'],settings={})
        repeated=self.service.create_task(user['id'],name='confirmed',modes=['followers'],targets=['SOURCE.DONE'],settings={},allow_completed_targets=True)
        self.assertNotEqual(original['targets'][0]['id'],repeated['targets'][0]['id'])
        self.assertEqual(before,self.service.get_task(user['id'],original['id']))
        with self.database.read() as c:
            self.assertEqual(2,c.execute('SELECT successful_adds FROM split_admission_totals WHERE owner_user_id=?',(user['id'],)).fetchone()[0])

    def test_incomplete_source_target_rejects_duplicate_and_keeps_original_task(self) -> None:
        user = self.create_user("incomplete-source-owner")
        original = self.service.create_task(
            user["id"], name="pending", modes=["followers"],
            targets=["retry_me"], settings={"followers_max": 10},
        )
        outcome = self.service.check_completed_targets(user["id"], ["retry_me"])
        self.assertEqual(0, outcome["completed_count"])
        for force in (False, True):
            with self.subTest(force=force), self.assertRaisesRegex(ConflictError, "不能重复加入"):
                self.service.create_task(
                    user["id"], name="duplicate", modes=["followers"],
                    targets=["retry_me"], settings={"followers_max": 10},
                    allow_completed_targets=force,
                )
        self.assertEqual(original["targets"][0]["id"], self.service.get_task(user["id"], original["id"])["targets"][0]["id"])
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM task_targets").fetchone()[0])

    def test_unlimited_relation_collection_ignores_stale_saved_limit(self) -> None:
        settings = {
            # A recovered legacy task can carry both the old false flag and a
            # finite saved limit. Neither may constrain current relation work.
            "unlimited_relation_collection": False,
            "mode_limits": {"followers": {"per_target_limit": 10}},
        }
        self.assertIsNone(
            ExecutionManager._candidate_spool_limit("followers", settings)
        )

    def test_new_and_recovered_relation_tasks_expose_no_collection_ceiling(self) -> None:
        user = self.create_user("unlimited-recovery-owner")
        task = self.service.create_task(
            user["id"],
            name="legacy finite relation task",
            modes=["followers", "following"],
            targets=["legacy.limit.source"],
            window_ids=["legacy-limit-window"],
            settings={
                "unlimited_relation_collection": False,
                "mode_limits": {
                    "followers": {"per_target_limit": 7},
                    "following": {"per_target_limit": 9},
                },
            },
        )

        # create_task and restart/recovery both serialize through _task_dict.
        # It must neutralize the finite values even when they remain in an old
        # on-disk settings_json row.
        for view in (task, self.service.get_task_execution_spec(user["id"], task["id"])):
            self.assertTrue(view["settings"]["unlimited_relation_collection"])
            self.assertNotIn(
                "per_target_limit",
                view["settings"]["mode_limits"]["followers"],
            )
            self.assertNotIn(
                "per_target_limit",
                view["settings"]["mode_limits"]["following"],
            )

        with self.database.read() as connection:
            stored = connection.execute(
                "SELECT settings_json FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()[0]
        self.assertIn('"per_target_limit":7', stored)
        self.assertIn('"unlimited_relation_collection":false', stored)

    def test_gpt_review_schema_has_no_marketing_classification(self) -> None:
        payload = ReviewDecision(
            review_status="reviewed",
            confidence="low",
            reason_codes=["visible_context_present"],
        ).model_dump()
        self.assertNotIn("marketing", payload)
        self.assertEqual("reviewed", payload["review_status"])

    def test_sqlite_wal_and_integrity(self) -> None:
        self.assertEqual("ok", self.database.integrity_check())
        with self.database.read() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def test_legacy_database_adds_action_source_and_counter_reset_schema(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """
            CREATE TABLE action_targets (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                username_norm TEXT NOT NULL,
                username_display TEXT NOT NULL,
                queue_order INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE action_campaigns (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                execution_type TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                message TEXT,
                interval_min_seconds INTEGER NOT NULL,
                interval_max_seconds INTEGER NOT NULL,
                limit_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO action_campaigns(
                id, owner_user_id, operation, execution_type, profile_id, message,
                interval_min_seconds, interval_max_seconds, limit_count, status,
                created_at, updated_at
            ) VALUES('legacy-greeting', 'legacy-owner', 'greet', 'campaign',
                     'legacy-window', 'Legacy hello', 8, 15, 1, 'completed',
                     '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()
        connection.close()
        legacy = Database(legacy_path)
        legacy.initialize()
        with legacy.read() as upgraded:
            columns = {row[1] for row in upgraded.execute("PRAGMA table_info(action_targets)")}
            self.assertIn("source_target", columns)
            self.assertIsNotNone(
                upgraded.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='action_counter_resets'"
                ).fetchone()
            )
            self.assertIsNotNone(
                upgraded.execute("SELECT version FROM schema_migrations WHERE version=2").fetchone()
            )
            self.assertIsNotNone(
                upgraded.execute("SELECT version FROM schema_migrations WHERE version=3").fetchone()
            )
            self.assertIsNotNone(
                upgraded.execute("SELECT version FROM schema_migrations WHERE version=4").fetchone()
            )
            self.assertIsNotNone(
                upgraded.execute("SELECT version FROM schema_migrations WHERE version=6").fetchone()
            )
            self.assertEqual(
                '["Legacy hello"]',
                upgraded.execute(
                    "SELECT messages_json FROM action_campaigns WHERE id='legacy-greeting'"
                ).fetchone()[0],
            )
            self.assertIsNotNone(
                upgraded.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_results_global_account'"
                ).fetchone()
            )

    def test_greeting_message_library_is_normalized_persisted_and_legacy_compatible(self) -> None:
        user = self.create_user("greeting-library")
        campaign = self.service.create_action_campaign(
            user["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="greeting-window",
            targets=["one.person", "two.person"],
            message="Hello there",
            messages=[" Hello there ", "你好，很高兴认识你！", "Hello there", "  "],
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=2,
        )
        self.assertEqual("Hello there", campaign["message"])
        self.assertEqual(["Hello there", "你好，很高兴认识你！"], campaign["messages"])

        legacy = self.service.create_action_campaign(
            user["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="legacy-window",
            targets=["legacy.person"],
            message="Legacy only",
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        self.assertEqual(["Legacy only"], legacy["messages"])

        with self.assertRaises(ValidationError):
            self.service.create_action_campaign(
                user["id"],
                operation="greet",
                execution_type="campaign",
                profile_id="empty-window",
                targets=["empty.person"],
                message=None,
                messages=["  "],
                interval_min_seconds=8,
                interval_max_seconds=15,
                limit_count=1,
            )
        with self.assertRaises(ValidationError):
            self.service.create_action_campaign(
                user["id"],
                operation="greet",
                execution_type="campaign",
                profile_id="long-window",
                targets=["long.person"],
                message=None,
                messages=["x" * 201],
                interval_min_seconds=8,
                interval_max_seconds=15,
                limit_count=1,
            )

    def test_database_instance_lock_blocks_a_second_core(self) -> None:
        second = Database(self.db_path)
        self.database.acquire_instance_lock()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire_instance_lock()
        finally:
            self.database.release_instance_lock()
        second.acquire_instance_lock()
        second.release_instance_lock()

    def test_task_lifecycle_targets_and_checkpoint(self) -> None:
        user = self.create_user("operator")
        task = self.create_task(user["id"])
        self.assertEqual("draft", task["status"])
        self.assertEqual(1, len(task["targets"]))
        self.assertEqual(3000, task["settings"]["followers_max"])
        self.assertEqual(30, task["settings"]["active_days_max"])

        started = self.service.control_task(user["id"], task["id"], "start", expected_version=1)
        self.assertEqual("running", started["status"])
        with self.assertRaises(InvalidTransitionError):
            self.service.control_task(user["id"], task["id"], "start")
        paused = self.service.control_task(user["id"], task["id"], "pause")
        resumed = self.service.control_task(user["id"], task["id"], "resume")
        self.assertEqual("paused", paused["status"])
        self.assertEqual("running", resumed["status"])

        target_id = task["targets"][0]["id"]
        checkpoint = self.service.upsert_checkpoint(
            user["id"],
            task["id"],
            target_id,
            mode="followers",
            stage="scrolling_visible_list",
            cursor={"last_username": "visible_user"},
            counters={"seen": 25},
        )
        self.assertEqual(25, checkpoint["counters"]["seen"])
        self.assertEqual(1, len(self.service.list_checkpoints(user["id"], task["id"])))

        stopped = self.service.control_task(user["id"], task["id"], "stop")
        restarted = self.service.control_task(user["id"], task["id"], "restart")
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual("queued", restarted["status"])
        self.assertEqual(1, restarted["restart_count"])
        self.assertEqual(1, len(self.service.list_checkpoints(user["id"], task["id"])))

    def test_gender_filters_are_not_exposed_or_accepted_for_new_tasks(self) -> None:
        for request_model in (TaskSettingsRequest, DesktopTaskCreateRequest):
            self.assertNotIn("gender_filter_male", request_model.model_fields)
            self.assertNotIn("gender_filter_female", request_model.model_fields)

        user = self.create_user("no-gender-settings")
        for removed_key in ("gender_filter_male", "gender_filter_female"):
            with self.assertRaises(ValidationError):
                self.service.create_task(
                    user["id"],
                    name="采集任务",
                    modes=["followers"],
                    targets=["source_account"],
                    window_ids=["window-no-gender"],
                    settings={removed_key: True},
                )

    def test_new_collection_defaults_are_off_zero_and_liker_mode_is_retired(self) -> None:
        generic = TaskSettingsRequest()
        desktop = DesktopTaskCreateRequest(
            targets=["source"], window_ids=["window"], modes=["followers"]
        )
        for field in (
            "followers_min",
            "followers_max",
            "following_min",
            "following_max",
            "posts_min",
            "posts_max",
            "active_days_max",
        ):
            self.assertEqual(0, getattr(generic, field))
        self.assertTrue(generic.location_enabled)
        self.assertFalse(generic.gpt_enabled)
        self.assertFalse(generic.local_person_recognition)
        self.assertFalse(generic.exclude_male_avatar)
        self.assertTrue(generic.exclude_public_zero_posts)
        self.assertEqual(1, generic.parallel_screening_workers)
        self.assertEqual(0, desktop.per_target_limit)
        self.assertTrue(desktop.read_location)
        self.assertFalse(desktop.auto_classify)
        self.assertFalse(desktop.local_person_recognition)
        self.assertFalse(desktop.exclude_male_avatar)
        self.assertTrue(desktop.exclude_public_zero_posts)
        self.assertEqual(1, desktop.parallel_screening_workers)

        for invalid_parallel_workers in (0, 4, 5, True):
            with self.assertRaises(Exception):
                TaskSettingsRequest(
                    parallel_screening_workers=invalid_parallel_workers,
                )

        topology_user = self.create_user("parallel-topology-bounds")
        for invalid_parallel_workers in (0, 4, 5, True):
            with self.assertRaises(ValidationError):
                self.service.create_task(
                    topology_user["id"],
                    name="invalid parallel topology",
                    modes=["followers"],
                    targets=["source_account"],
                    window_ids=["window"],
                    settings={"parallel_screening_workers": invalid_parallel_workers},
                )
            with self.assertRaises(Exception):
                DesktopTaskCreateRequest(
                    targets=["source"],
                    window_ids=["window"],
                    modes=["followers"],
                    parallel_screening_workers=invalid_parallel_workers,
                )

        selected_topology = self.service.create_task(
            topology_user["id"],
            name="selected 1-2 topology",
            modes=["followers"],
            targets=["topology_source"],
            window_ids=["window"],
            settings={"parallel_screening_workers": 2},
        )
        self.assertEqual(2, selected_topology["settings"]["parallel_screening_workers"])

        user = self.create_user("retired-liker-mode")
        mixed = self.service.create_task(
            user["id"],
            name="旧配置升级",
            modes=["followers", "post_likers"],
            targets=["source_account"],
            window_ids=["window-a"],
            settings={
                "like_posts_to_check": 5,
                "max_likers_per_post": 100,
                "mode_limits": {
                    "followers": {"per_target_limit": 7},
                    "post_likers": {
                        "per_target_limit": 999,
                        "like_posts_to_check": 5,
                    },
                },
            },
        )
        self.assertEqual(["followers"], mixed["modes"])
        self.assertNotIn("like_posts_to_check", mixed["settings"])
        self.assertNotIn("max_likers_per_post", mixed["settings"])
        self.assertNotIn("post_likers", mixed["settings"]["mode_limits"])
        with self.assertRaisesRegex(ValidationError, "帖子点赞采集已取消"):
            self.service.create_task(
                user["id"],
                name="已取消功能",
                modes=["post_likers"],
                targets=["source_account"],
                window_ids=["window-b"],
                settings={},
            )

    def test_zero_filter_defaults_do_not_conflict_with_one_sided_ranges(self) -> None:
        request = TaskSettingsRequest(followers_min=10)
        self.assertEqual(10, request.followers_min)
        self.assertEqual(0, request.followers_max)
        task = self.service.create_task(
            self.create_user("one-sided-filter")["id"],
            name="单侧条件",
            modes=["followers"],
            targets=["source_account"],
            window_ids=["window-a"],
            settings=request.model_dump(),
        )
        self.assertEqual(10, task["settings"]["followers_min"])
        self.assertEqual(0, task["settings"]["followers_max"])

    def test_desktop_api_ignores_relation_collection_limits_and_runs_unlimited(self) -> None:
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.main import create_app

        self.create_user("mode-limit-api")
        login = self.service.login("mode-limit-api", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=Path(self.temp_dir.name),
        )
        app = create_app(
            settings,
            database=self.database,
            bitbrowser=FakeBitBrowserClient(),
        )
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            created = client.post(
                "/api/tasks",
                headers=headers,
                json={
                    "targets": ["source_account"],
                    "window_ids": ["window-a"],
                    "modes": ["followers", "following"],
                    "source_limits": {
                        "followers": {
                            "perTargetLimit": 11,
                            "followers": 0,
                            "following": 101,
                            "posts": 0,
                            "activityDays": 0,
                        },
                        "following": {
                            "perTargetLimit": 23,
                            "followers": 202,
                            "following": 0,
                            "posts": 303,
                            "activityDays": 15,
                        },
                    },
                },
            )
            self.assertEqual(201, created.status_code, created.text)
            detail = client.get(
                f"/api/tasks/{created.json()['task_id']}", headers=headers
            ).json()
            self.assertEqual(
                {
                    "following_max": 101,
                },
                detail["settings"]["mode_limits"]["followers"],
            )
            self.assertEqual(
                {
                    "followers_max": 202,
                    "posts_max": 303,
                    "active_days_max": 15,
                },
                detail["settings"]["mode_limits"]["following"],
            )
            missing_limit = client.post(
                "/api/tasks",
                headers=headers,
                json={
                    "targets": ["source_two"],
                    "window_ids": ["window-a"],
                    "modes": ["followers"],
                    "source_limits": {"followers": {"perTargetLimit": 0}},
                },
            )
            self.assertEqual(201, missing_limit.status_code, missing_limit.text)
            unlimited_detail = client.get(
                f"/api/tasks/{missing_limit.json()['task_id']}", headers=headers
            ).json()
            self.assertTrue(unlimited_detail["settings"]["unlimited_relation_collection"])
            self.assertNotIn(
                "per_target_limit",
                unlimited_detail["settings"]["mode_limits"]["followers"],
            )
            retired = client.post(
                "/api/tasks",
                headers=headers,
                json={
                    "targets": ["source_three"],
                    "window_ids": ["window-a"],
                    "modes": ["post_likers"],
                    "source_limits": {"post_likers": {"perTargetLimit": 99}},
                },
            )
            self.assertEqual(422, retired.status_code)
            self.assertIn("帖子点赞采集已取消", retired.json()["detail"])

    def test_completed_targets_and_history_are_permanent_while_siblings_delete_independently(self) -> None:
        user = self.create_user("permanent-history-owner")
        task = self.service.create_task(
            user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["target_a", "target_b", "target_c"],
            window_ids=["window-history"],
            settings={},
        )
        target_a, target_b, target_c = task["targets"]
        self.service.set_target_runtime_status(
            user["id"], task["id"], target_a["id"], "completed", window_id="window-history"
        )
        self.service.delete_task_target(user["id"], task["id"], target_b["id"])
        remaining = self.service.get_task(user["id"], task["id"])["targets"]
        self.assertEqual(
            {target_a["id"], target_b["id"], target_c["id"]},
            {item["id"] for item in remaining},
        )
        archived = next(item for item in remaining if item["id"] == target_b["id"])
        self.assertEqual("stopped", archived["status"])
        self.assertTrue(archived["manual_recovery_required"])
        with self.assertRaises(ConflictError):
            self.service.delete_task_target(user["id"], task["id"], target_a["id"])
        with self.assertRaises(ConflictError):
            self.service.delete_history_target(user["id"], target_a["id"])
        with self.assertRaises(ConflictError):
            self.service.delete_task(user["id"], task["id"])

    def test_completed_task_can_be_removed_from_live_list_without_deleting_history(self) -> None:
        user = self.create_user("dismiss-completed-task-owner")
        task = self.service.create_task(
            user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["completed_source"],
            window_ids=["window-history"],
            settings={},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            user["id"], task["id"], target["id"], "completed", window_id="window-history"
        )
        self.service.dismiss_task_from_list(user["id"], task["id"])
        self.assertNotIn(task["id"], {item["id"] for item in self.service.list_tasks(user["id"])})
        self.assertNotIn(task["id"], {item["id"] for item in self.service.list_tasks_with_details(user["id"])})
        self.assertEqual(task["id"], self.service.get_task(user["id"], task["id"])["id"])

    def test_failed_split_candidate_stays_queued_until_worker_claims_target(self) -> None:
        user = self.create_user("failed-split-lifecycle")
        task = self.service.create_task(
            user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["recover_later"],
            window_ids=["window-recovery"],
            settings={"live_queue_enabled": True},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            user["id"], task["id"], target["id"], "recoverable",
            window_id="window-recovery",
        )
        candidate = self.service.list_split_candidates(user["id"])[0]
        self.assertEqual("failure", candidate["kind"])
        self.assertEqual("采集失败", candidate["group"])
        self.assertEqual("available", candidate["queue_state"])

        queued = self.service.requeue_split_candidate(user["id"], candidate["id"])
        self.assertEqual("queued", queued["queue_state"])
        # Re-queueing is not execution.  The split row must remain visible as
        # “added/waiting” until a worker performs the durable RUNNING claim.
        still_queued = self.service.list_split_candidates(user["id"])[0]
        self.assertEqual("queued", still_queued["queue_state"])
        self.assertIsNone(still_queued["queued_target_id"])

        self.service.set_task_runtime_status(user["id"], task["id"], "running")
        claimed_target = self.service.claim_next_split_candidate(
            user["id"], task["id"], "window-recovery"
        )
        self.assertEqual(target["id"], claimed_target["id"])
        claimed = self.service.list_split_candidates(user["id"])[0]
        self.assertEqual("claimed", claimed["queue_state"])
        self.assertEqual(target["id"], claimed["queued_target_id"])

    def test_unclaimed_split_candidate_can_be_deleted_but_claimed_target_cannot(self) -> None:
        user = self.create_user("split-wait-delete")
        self.service.upsert_manual_split_candidates(
            user["id"],
            [{"username": "waiting_delete", "source_target": "source", "queued": False, "profile": {}}],
        )
        candidate = self.service.list_split_candidates(user["id"])[0]
        self.service.mark_split_candidates_queued(user["id"], [candidate["id"]])
        deleted = self.service.delete_waiting_split_candidate(user["id"], candidate["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual([], self.service.list_split_candidates(user["id"]))

        self.service.upsert_manual_split_candidates(
            user["id"],
            [{"username": "already_claimed", "source_target": "source", "queued": False, "profile": {}}],
        )
        candidate = self.service.list_split_candidates(user["id"])[0]
        self.service.mark_split_candidates_queued(user["id"], [candidate["id"]])
        task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"], targets=[],
            window_ids=["window-claim"], settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(user["id"], task["id"], "running")
        self.service.claim_next_split_candidate(user["id"], task["id"], "window-claim")
        with self.assertRaises(ConflictError):
            self.service.delete_waiting_split_candidate(user["id"], candidate["id"])

    def test_failed_target_detaches_window_and_manual_requeue_is_unbound(self) -> None:
        user = self.create_user("failed-window-detach")
        task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"],
            targets=["detach_me"], window_ids=["failed-window"], settings={},
        )
        target = task["targets"][0]
        self.service.set_task_runtime_status(user["id"], task["id"], "running")
        self.service.set_target_runtime_status(
            user["id"], task["id"], target["id"], "running", window_id="failed-window"
        )
        self.service.set_target_runtime_status(
            user["id"], task["id"], target["id"], "recoverable", window_id="failed-window"
        )
        failed = self.service.get_task(user["id"], task["id"])["targets"][0]
        self.assertIsNone(failed["current_window_id"])
        self.assertEqual("failed-window", failed["preferred_window_id"])
        requeued = self.service.retry_task_target(user["id"], task["id"], target["id"])
        self.assertEqual("pending", requeued["status"])
        self.assertIsNone(requeued["current_window_id"])
        self.assertIsNone(requeued["preferred_window_id"])

    def test_deleted_and_stopped_unfinished_targets_are_durable_failed_split_candidates(self) -> None:
        user = self.create_user("failed-split-archive")
        task = self.service.create_task(
            user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["delete_me", "stopped_waiter"],
            window_ids=["window-archive"],
            settings={},
        )
        deleted, stopped = task["targets"]
        self.service.delete_task_target(user["id"], task["id"], deleted["id"])
        self.service.set_task_runtime_status(user["id"], task["id"], "stopped")

        by_username = {
            item["username"]: item for item in self.service.list_split_candidates(user["id"])
        }
        self.assertEqual("stopped", by_username["delete_me"]["source_status"])
        self.assertEqual("available", by_username["delete_me"]["queue_state"])
        self.assertEqual("stopped", by_username["stopped_waiter"]["source_status"])
        self.assertEqual("available", by_username["stopped_waiter"]["queue_state"])

        # Removing the now-empty unfinished history cannot remove the recovery
        # candidate because the archive has no FK back to the deleted task target.
        self.service.delete_history_target(user["id"], stopped["id"])
        self.assertEqual(
            {"delete_me", "stopped_waiter"},
            {item["username"] for item in self.service.list_split_candidates(user["id"])},
        )

    def test_manual_split_upsert_is_owner_scoped_and_cannot_downgrade_failure(self) -> None:
        owner = self.create_user("split-owner")
        other = self.create_user("split-other")
        task = self.service.create_task(
            owner["id"], name="采集任务", modes=["followers"],
            targets=["same_account"], window_ids=["split-window"], settings={},
        )
        target = task["targets"][0]
        self.service.set_target_runtime_status(
            owner["id"], task["id"], target["id"], "failed",
            window_id="split-window",
        )
        outcome = self.service.upsert_manual_split_candidates(
            owner["id"],
            [{"username": "@same_account", "source_target": "manual-source", "profile": {"followers": 12}}],
            include_outcome=True,
        )
        self.assertEqual(0, outcome["accepted_count"])
        self.assertEqual("failure", outcome["duplicates"][0]["disposition"])
        owner_candidate = self.service.list_split_candidates(owner["id"])[0]
        self.assertEqual("failure", owner_candidate["kind"])
        self.assertEqual("available", owner_candidate["queue_state"])
        self.assertEqual({}, owner_candidate["profile"])
        self.assertEqual([], self.service.list_split_candidates(other["id"]))

    def test_split_failure_and_delete_preserve_cross_task_claimed_or_queued_state(self) -> None:
        user = self.create_user("split-cross-target-priority")

        claimed_sibling_task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"],
            targets=["claimed_elsewhere"], window_ids=["window-claimed"], settings={},
        )
        claimed_sibling = claimed_sibling_task["targets"][0]
        self.service.set_target_runtime_status(
            user["id"], claimed_sibling_task["id"], claimed_sibling["id"], "running",
            window_id="window-claimed",
        )
        failed_task = create_legacy_task(self.service,
            user["id"], name="采集任务", modes=["followers"],
            targets=["claimed_elsewhere"], window_ids=["window-failed"], settings={},
        )
        failed_target = failed_task["targets"][0]
        self.service.set_target_runtime_status(
            user["id"], failed_task["id"], failed_target["id"], "failed",
            window_id="window-failed",
        )

        pending_sibling_task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"],
            targets=["queued_elsewhere"], window_ids=["window-queued"], settings={},
        )
        pending_sibling = pending_sibling_task["targets"][0]
        deleted_task = create_legacy_task(self.service,
            user["id"], name="采集任务", modes=["followers"],
            targets=["queued_elsewhere"], window_ids=["window-deleted"], settings={},
        )
        deleted_target = deleted_task["targets"][0]
        self.service.delete_task_target(user["id"], deleted_task["id"], deleted_target["id"])

        candidates = {
            item["username"]: item for item in self.service.list_split_candidates(user["id"])
        }
        claimed = candidates["claimed_elsewhere"]
        self.assertEqual("available", claimed["queue_state"])
        self.assertIsNone(claimed["queued_task_id"])
        self.assertIsNone(claimed["queued_target_id"])
        queued = candidates["queued_elsewhere"]
        self.assertEqual("available", queued["queue_state"])
        self.assertIsNone(queued["queued_task_id"])
        self.assertIsNone(queued["queued_target_id"])

    def test_split_task_terminal_trigger_uses_only_other_tasks_as_siblings(self) -> None:
        user = self.create_user("split-cross-terminal-priority")

        claimed_sibling_task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"],
            targets=["terminal_claimed"], window_ids=["terminal-claimed-window"], settings={},
        )
        claimed_sibling = claimed_sibling_task["targets"][0]
        self.service.set_target_runtime_status(
            user["id"], claimed_sibling_task["id"], claimed_sibling["id"], "completed",
            window_id="terminal-claimed-window",
        )
        claimed_terminal_task = create_legacy_task(self.service,
            user["id"], name="采集任务", modes=["followers"],
            targets=["terminal_claimed"], window_ids=["terminal-failed-window"], settings={},
        )
        self.service.set_task_runtime_status(user["id"], claimed_terminal_task["id"], "failed")

        pending_sibling_task = self.service.create_task(
            user["id"], name="采集任务", modes=["followers"],
            targets=["terminal_queued"], window_ids=["terminal-queued-window"], settings={},
        )
        pending_sibling = pending_sibling_task["targets"][0]
        queued_terminal_task = create_legacy_task(self.service,
            user["id"], name="采集任务", modes=["followers"],
            targets=["terminal_queued"], window_ids=["terminal-stopped-window"], settings={},
        )
        self.service.set_task_runtime_status(user["id"], queued_terminal_task["id"], "stopped")

        candidates = {
            item["username"]: item for item in self.service.list_split_candidates(user["id"])
        }
        claimed = candidates["terminal_claimed"]
        self.assertEqual("available", claimed["queue_state"])
        self.assertIsNone(claimed["queued_task_id"])
        self.assertIsNone(claimed["queued_target_id"])
        queued = candidates["terminal_queued"]
        self.assertEqual("available", queued["queue_state"])
        self.assertIsNone(queued["queued_task_id"])
        self.assertIsNone(queued["queued_target_id"])

    def test_split_trigger_v9_replaces_legacy_failure_definition(self) -> None:
        with self.database.write() as connection:
            connection.execute("DROP TRIGGER trg_split_candidate_target_failed")
            connection.execute(
                """
                CREATE TRIGGER trg_split_candidate_target_failed
                AFTER UPDATE OF status ON task_targets
                BEGIN SELECT 1; END
                """
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=9")

        self.database.initialize()
        with self.database.read() as connection:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_split_candidate_target_failed'"
            ).fetchone()[0]
            self.assertIn("task_target_recovery_controls", trigger_sql)
            self.assertNotIn("INSERT INTO split_candidates", trigger_sql)
            self.assertIsNotNone(
                connection.execute("SELECT version FROM schema_migrations WHERE version=9").fetchone()
            )

    def test_browser_lease_state_hides_other_login_entity_and_blocks_reuse(self) -> None:
        owner = self.create_user("lease-state-owner")
        other = self.create_user("lease-state-viewer")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["lease_target"],
            window_ids=["lease-window"],
            settings={},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        target_id = task["targets"][0]["id"]
        self.service.set_target_runtime_status(
            owner["id"], task["id"], target_id, "running", window_id="lease-window"
        )
        token = self.service.acquire_browser_lease(
            owner["id"], "lease-window", operation_type="collection", entity_id=task["id"]
        )
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(
                other["id"], "lease-window", operation_type="collection", entity_id="other-task"
            )
        owner_state = self.service.list_browser_lease_states(owner["id"])[0]
        other_state = self.service.list_browser_lease_states(other["id"])[0]
        self.assertEqual("collecting", owner_state["state"])
        self.assertEqual(task["id"], owner_state["entity_id"])
        self.assertIsNone(other_state["entity_id"])
        self.service.release_browser_lease("lease-window", token)
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))

    def test_profiles_reconciliation_removes_terminal_collection_lease(self) -> None:
        owner = self.create_user("terminal-lease-owner")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["terminal_source"],
            window_ids=["terminal-window"],
            settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        self.service.acquire_browser_lease(
            owner["id"],
            "terminal-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        self.service.set_target_runtime_status(
            owner["id"],
            task["id"],
            task["targets"][0]["id"],
            "completed",
            window_id="terminal-window",
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "completed")
        # Simulate a subsequent Core process; current-process cleanup retains ownership.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual(
            [],
            self.service.list_browser_lease_states(
                owner["id"], active_collection_entity_ids=set()
            ),
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "recoverable")
        self.service.acquire_browser_lease(
            owner["id"],
            "recoverable-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        self.service = CoreService(Database(self.db_path))
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))
        self.service.acquire_browser_lease(
            owner["id"],
            "orphan-window",
            operation_type="collection",
            entity_id="missing-task-id",
        )
        self.service = CoreService(Database(self.db_path))
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))

    def test_paused_task_lease_is_not_reported_as_collecting(self) -> None:
        owner = self.create_user("paused-lease-owner")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["paused_source"],
            window_ids=["paused-state-window"],
            settings={"live_queue_enabled": True},
        )
        target_id = task["targets"][0]["id"]
        self.service.set_task_runtime_status(owner["id"], task["id"], "paused")
        self.service.set_target_runtime_status(
            owner["id"],
            task["id"],
            target_id,
            "running",
            window_id="paused-state-window",
        )
        self.service.acquire_browser_lease(
            owner["id"],
            "paused-state-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        state = self.service.list_browser_lease_states(owner["id"])[0]
        self.assertEqual("paused", state["state"])

    def test_profiles_reconciliation_preserves_real_active_task_and_clears_old_inactive_one(self) -> None:
        owner = self.create_user("active-lease-owner")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["active_source"],
            window_ids=["active-window"],
            settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        self.service.acquire_browser_lease(
            owner["id"],
            "active-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        # A just-created lease receives a small startup grace even before the manager
        # registry becomes visible to a concurrent profiles refresh.
        self.assertEqual(
            1,
            len(
                self.service.list_browser_lease_states(
                    owner["id"], active_collection_entity_ids=set()
                )
            ),
        )
        old_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(
            timespec="milliseconds"
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE browser_operation_leases SET heartbeat_at=? WHERE profile_id=?",
                (old_heartbeat, "active-window"),
            )
        self.assertEqual(
            1,
            len(
                self.service.list_browser_lease_states(
                    owner["id"], active_collection_entity_ids={task["id"]}
                )
            ),
        )
        # New Core memory removes only residual leases, never live cleanup ownership.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual(
            [],
            self.service.list_browser_lease_states(
                owner["id"], active_collection_entity_ids=set()
            ),
        )

    def test_profiles_reconciliation_releases_managerless_lock_on_first_refresh(self) -> None:
        owner = self.create_user("immediate-stale-lock-owner")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["stale_source"],
            window_ids=["stale-window"],
            settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        self.service.acquire_browser_lease(
            owner["id"],
            "stale-window",
            operation_type="collection",
            entity_id=task["id"],
        )

        # The profiles endpoint supplies the live manager registry and uses zero
        # grace. A row not present there is a crash/residual lock, not active work.
        # New Core memory removes only residual leases, never live cleanup ownership.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual(
            [],
            self.service.list_browser_lease_states(
                owner["id"],
                active_collection_entity_ids=set(),
                active_action_entity_ids=set(),
                inactive_grace_seconds=0,
            ),
        )

    def test_profiles_reconciliation_keeps_terminal_lease_until_live_manager_cleanup(self) -> None:
        owner = self.create_user("live-cleanup-owner")
        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["cleanup_source"],
            window_ids=["cleanup-window"],
            settings={},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        token = self.service.acquire_browser_lease(
            owner["id"],
            "cleanup-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "stopped")

        states = self.service.list_browser_lease_states(
            owner["id"],
            active_collection_entity_ids={task["id"]},
            active_action_entity_ids=set(),
            inactive_grace_seconds=0,
        )
        self.assertEqual(["cleanup-window"], [item["profile_id"] for item in states])
        self.service.release_browser_lease("cleanup-window", token)

    def test_terminal_action_and_restart_residual_leases_are_reconciled(self) -> None:
        owner = self.create_user("restart-lease-owner")
        campaign = self.service.create_action_campaign(
            owner["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="terminal-action-window",
            targets=["action_target"],
            message=None,
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        self.service.acquire_browser_lease(
            owner["id"],
            "terminal-action-window",
            operation_type="action",
            entity_id=campaign["id"],
        )
        self.service.set_campaign_status(owner["id"], campaign["id"], "completed")
        old_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(
            timespec="milliseconds"
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE browser_operation_leases SET heartbeat_at=? WHERE profile_id=?",
                (old_heartbeat, "terminal-action-window"),
            )
        # New Core memory: no live owner for a residual on-disk lease.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))
        self.service.set_campaign_status(owner["id"], campaign["id"], "recoverable")
        self.service.acquire_browser_lease(
            owner["id"],
            "recoverable-action-window",
            operation_type="action",
            entity_id=campaign["id"],
        )
        # New Core memory: no live owner for a residual on-disk lease.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))

        task = self.service.create_task(
            owner["id"],
            name="采集任务",
            modes=["followers"],
            targets=["restart_source"],
            window_ids=["restart-window"],
            settings={},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        self.service.acquire_browser_lease(
            owner["id"],
            "restart-window",
            operation_type="collection",
            entity_id=task["id"],
        )
        self.service.recover_interrupted_operations()
        recovered = self.service.get_task(owner["id"], task["id"])
        self.assertEqual("paused", recovered["status"])
        self.assertEqual("pending", recovered["targets"][0]["status"])
        # New Core memory: no live owner for a residual on-disk lease.
        self.service = CoreService(Database(self.db_path))
        self.assertEqual([], self.service.list_browser_lease_states(owner["id"]))

    def test_startup_recovery_pauses_task_and_archives_only_actively_running_target(self) -> None:
        owner = self.create_user("startup-network-recovery")
        task = self.service.create_task(
            owner["id"],
            name="断网启动恢复",
            modes=["followers"],
            targets=["network_waiter", "active_reader", "unclaimed"],
            window_ids=["network-window", "active-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 10}}},
        )
        waiting_target, running_target, pending_target = task["targets"]
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        successful = self.service.upsert_checkpoint(
            owner["id"],
            task["id"],
            waiting_target["id"],
            mode="followers",
            stage="screening_accounts",
            cursor={"durable_results": 4},
            counters={"saved": 4},
        )
        self.service.set_target_runtime_status(
            owner["id"],
            task["id"],
            waiting_target["id"],
            "waiting_network",
            window_id="network-window",
        )
        self.service.upsert_checkpoint(
            owner["id"],
            task["id"],
            waiting_target["id"],
            mode="followers",
            stage="waiting_network",
            cursor={
                "resume_stage": "screening_accounts",
                "resume_cursor": {"durable_results": 4},
            },
            counters={
                "reason": "instagram_network_unavailable",
                "retry_count": 11,
                "next_retry_at": "2026-08-27T09:00:00+00:00",
                "previous_counters": {"saved": 4},
            },
        )
        self.service.set_target_runtime_status(
            owner["id"],
            task["id"],
            running_target["id"],
            "running",
            window_id="active-window",
        )
        # Reproduce the stale projection seen in v0.2.24: a successful reconnect
        # changed the target back to RUNNING but left its old network checkpoint.
        self.service.upsert_checkpoint(
            owner["id"],
            task["id"],
            running_target["id"],
            mode="followers",
            stage="waiting_network",
            cursor={"resume_stage": "mode_started", "resume_cursor": {}},
            counters={
                "reason": "instagram_network_unavailable",
                "retry_count": 7,
                "next_retry_at": "2026-08-27T09:05:00+00:00",
            },
        )

        self.service.recover_interrupted_operations()

        recovered = self.service.get_task(owner["id"], task["id"])
        by_username = {target["username"]: target for target in recovered["targets"]}
        self.assertEqual("paused", recovered["status"])
        waiting = by_username["network_waiter"]
        self.assertEqual("recoverable", waiting["status"])
        self.assertEqual("network-window", waiting["preferred_window_id"])
        self.assertIsNone(waiting["current_window_id"])
        self.assertEqual("interrupted_recoverable", waiting["current_stage"])
        self.assertEqual(successful["updated_at"], waiting["last_success_at"])
        self.assertEqual("recoverable", by_username["active_reader"]["status"])
        self.assertEqual(
            "interrupted_recoverable",
            by_username["active_reader"]["current_stage"],
        )
        self.assertEqual("pending", by_username["unclaimed"]["status"])
        self.assertIsNone(by_username["unclaimed"]["current_stage"])

        checkpoint = self.service.get_checkpoint(
            owner["id"], task["id"], waiting_target["id"], "followers"
        )
        self.assertEqual("interrupted_recoverable", checkpoint["stage"])
        self.assertEqual("screening_accounts", checkpoint["cursor"]["resume_stage"])
        self.assertEqual(
            {"durable_results": 4}, checkpoint["cursor"]["resume_cursor"]
        )
        # Restart preserves the remaining cooldown for an explicit resume.
        self.assertEqual(11, checkpoint["counters"]["retry_count"])
        self.assertEqual("2026-08-27T09:00:00+00:00", checkpoint["counters"]["next_retry_at"])
        self.assertEqual(
            {"network_waiter", "active_reader"},
            {
                candidate["username"]
                for candidate in self.service.list_split_candidates(owner["id"])
            },
        )
        history = self.service.list_history(owner["id"], task_id=task["id"])
        waiting_history = next(
            row for row in history if row["target_id"] == waiting_target["id"]
        )
        self.assertEqual("interrupted_recoverable", waiting_history["network_state"])
        self.assertEqual(0, waiting_history["retry_count"])
        self.assertIsNone(waiting_history["next_retry_at"])

    def test_history_task_filter_is_single_query_view_with_target_diagnostics(self) -> None:
        owner = self.create_user("scoped-history-owner")
        other = self.create_user("scoped-history-other")
        selected = self.service.create_task(
            owner["id"], name="selected", modes=["followers"],
            targets=["selected_source"], window_ids=["selected-window"], settings={},
        )
        self.service.create_task(
            owner["id"], name="unselected", modes=["followers"],
            targets=["unselected_source"], window_ids=["other-window"], settings={},
        )
        self.service.create_task(
            other["id"], name="private", modes=["followers"],
            targets=["private_source"], window_ids=["private-window"], settings={},
        )
        target = selected["targets"][0]
        checkpoint = self.service.upsert_checkpoint(
            owner["id"], selected["id"], target["id"],
            mode="followers", stage="screening_accounts",
            cursor={"durable_results": 1}, counters={"saved": 1},
        )
        self.service.record_result(
            owner["id"], selected["id"], target["id"],
            username="selected_fan", instagram_user_id=None,
            source_mode="followers", visibility="public",
            profile={"username": "selected_fan"}, screening={}, qualified=True,
        )

        original_connect = self.database._connect
        connection_count = 0

        def counted_connect():
            nonlocal connection_count
            connection_count += 1
            return original_connect()

        self.database._connect = counted_connect
        try:
            history = self.service.list_history(owner["id"], task_id=selected["id"])
        finally:
            self.database._connect = original_connect

        self.assertEqual(1, connection_count)
        self.assertEqual(1, len(history))
        self.assertEqual(selected["id"], history[0]["task_id"])
        self.assertEqual(1, history[0]["counts"]["collected"])
        self.assertEqual("screening_accounts", history[0]["current_stage"])
        self.assertGreaterEqual(history[0]["last_success_at"], checkpoint["updated_at"])
        self.assertIsNone(history[0]["last_error"])
        self.assertEqual([], self.service.list_history(other["id"], task_id=selected["id"]))

    def test_terminal_task_finalization_is_atomic_and_completed_requires_all_targets(self) -> None:
        owner = self.create_user("atomic-terminal-state")
        task = self.service.create_task(
            owner["id"], name="terminal", modes=["followers"],
            targets=["active_a", "active_b"],
            window_ids=["window-a", "window-b"], settings={},
        )
        self.service.set_task_runtime_status(owner["id"], task["id"], "running")
        self.service.set_target_runtime_status(
            owner["id"], task["id"], task["targets"][0]["id"],
            "running", window_id="window-a",
        )
        self.service.set_target_runtime_status(
            owner["id"], task["id"], task["targets"][1]["id"],
            "waiting_network", window_id="window-b",
        )
        finalized = self.service.finalize_task_runtime_status(
            owner["id"], task["id"], "failed", error="worker failed"
        )
        self.assertEqual("failed", finalized["status"])
        self.assertEqual(
            {"recoverable"}, {target["status"] for target in finalized["targets"]}
        )
        self.assertTrue(
            all(target["last_error"] == "worker failed" for target in finalized["targets"])
        )

        incomplete = self.service.create_task(
            owner["id"], name="cannot-complete", modes=["followers"],
            targets=["unfinished"], window_ids=["window-c"], settings={},
        )
        with self.assertRaises(ConflictError):
            self.service.set_task_runtime_status(
                owner["id"], incomplete["id"], "completed"
            )
        self.service.set_target_runtime_status(
            owner["id"], incomplete["id"], incomplete["targets"][0]["id"],
            "completed", window_id="window-c",
        )
        completed = self.service.set_task_runtime_status(
            owner["id"], incomplete["id"], "completed"
        )
        self.assertEqual("completed", completed["status"])
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(
                owner["id"], incomplete["id"], incomplete["targets"][0]["id"],
                "waiting_network", window_id="window-c",
            )

    def test_task_data_is_isolated_between_application_users(self) -> None:
        alice = self.create_user("alice-local")
        bob = self.create_user("bob-local")
        alice_task = self.create_task(alice["id"], "alice_source")
        bob_task = self.create_task(bob["id"], "bob_source")

        self.assertEqual([alice_task["id"]], [task["id"] for task in self.service.list_tasks(alice["id"])])
        self.assertEqual([bob_task["id"]], [task["id"] for task in self.service.list_tasks(bob["id"])])
        with self.assertRaises(NotFoundError):
            self.service.get_task(bob["id"], alice_task["id"])

    def test_results_are_owner_scoped_but_dedupe_is_global(self) -> None:
        alice = self.create_user("result-alice")
        bob = self.create_user("result-bob")
        alice_task = self.create_task(alice["id"], "source_one")
        bob_task = self.create_task(bob["id"], "source_two")

        first = self.service.record_result(
            alice["id"],
            alice_task["id"],
            alice_task["targets"][0]["id"],
            username="@shared.person",
            instagram_user_id="123456789",
            source_mode="followers",
            visibility="public",
            profile={"followers": 120, "following": 80, "posts": 9},
            screening={"stage": "basic", "reason_codes": []},
            qualified=True,
        )
        self.assertFalse(first["was_globally_seen"])
        self.assertFalse(first["deduped"])
        self.assertTrue(self.service.check_global_dedupe("SHARED.PERSON")["seen"])

        second = self.service.record_result(
            bob["id"],
            bob_task["id"],
            bob_task["targets"][0]["id"],
            username="shared.person",
            instagram_user_id="123456789",
            source_mode="following",
            visibility="public",
            profile={"followers": 120},
            screening={"stage": "basic"},
            qualified=None,
        )
        self.assertTrue(second["was_globally_seen"])
        self.assertTrue(second["deduped"])
        self.assertEqual(1, len(self.service.list_results(alice["id"], alice_task["id"])))
        self.assertEqual(0, len(self.service.list_results(bob["id"], bob_task["id"])))
        with self.assertRaises(NotFoundError):
            self.service.list_results(bob["id"], alice_task["id"])

    def test_upgrade_removes_legacy_cross_task_duplicates_and_enforces_unique_account(self) -> None:
        alice = self.create_user("legacy-dedupe-alice")
        bob = self.create_user("legacy-dedupe-bob")
        alice_task = self.create_task(alice["id"], "legacy_source_one")
        bob_task = self.create_task(bob["id"], "legacy_source_two")
        self.service.record_result(
            alice["id"],
            alice_task["id"],
            alice_task["targets"][0]["id"],
            username="legacy.duplicate",
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile={"followers": 10},
            screening={"stage": "basic"},
            qualified=True,
        )
        with self.database.write() as connection:
            connection.execute("DROP INDEX idx_results_global_account")
            canonical = connection.execute("SELECT * FROM task_results").fetchone()
            connection.execute(
                """
                INSERT INTO task_results(
                    id, task_id, target_id, account_id, sources_json, visibility,
                    profile_json, screening_json, qualified, created_at, updated_at
                ) VALUES('legacy-copy', ?, ?, ?, ?, ?, ?, ?, ?, '9999-01-01T00:00:00Z', '9999-01-01T00:00:00Z')
                """,
                (
                    bob_task["id"], bob_task["targets"][0]["id"], canonical["account_id"],
                    canonical["sources_json"], canonical["visibility"], canonical["profile_json"],
                    canonical["screening_json"], canonical["qualified"],
                ),
            )
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM task_results").fetchone()[0])

        self.database.initialize()
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM task_results").fetchone()[0])
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_results_global_account'"
                ).fetchone()
            )
        self.assertEqual(1, len(self.service.list_results(alice["id"], alice_task["id"])))
        self.assertEqual(0, len(self.service.list_results(bob["id"], bob_task["id"])))

    def test_result_serialization_recovers_legacy_private_fields(self) -> None:
        user = self.create_user("legacy-private-result")
        task = self.create_task(user["id"], "private_source")
        target_id = task["targets"][0]["id"]
        result = self.service.record_result(
            user["id"],
            task["id"],
            target_id,
            username="private.person",
            instagram_user_id=None,
            source_mode="followers",
            visibility="unknown",
            profile={"is_private": True, "followers": 12},
            screening={},
            qualified=None,
        )
        self.assertEqual("private", result["visibility"])

        # Older application versions could persist an account-type string directly
        # in the visibility column.  The desktop API must normalize it as well.
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_results SET visibility='PRIVATE_ACCOUNT', profile_json='{}' WHERE id=?",
                (result["id"],),
            )
        rows = self.service.list_all_results(user["id"])
        self.assertEqual("private", rows[0]["visibility"])

    def test_sensitive_worker_payload_is_rejected(self) -> None:
        user = self.create_user("safe-worker")
        task = self.create_task(user["id"])
        with self.assertRaises(ValidationError):
            self.service.record_result(
                user["id"],
                task["id"],
                task["targets"][0]["id"],
                username="visible_account",
                instagram_user_id=None,
                source_mode="followers",
                visibility="public",
                profile={"cookies": "must-not-be-stored"},
                screening={},
                qualified=None,
            )

    def test_interrupted_action_is_persisted_as_unknown(self) -> None:
        user = self.create_user("action-fencing")
        campaign = self.service.create_action_campaign(
            user["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="window-fenced",
            targets=["visible_target"],
            message=None,
            interval_min_seconds=30,
            interval_max_seconds=60,
            limit_count=1,
        )
        self.service.set_campaign_status(user["id"], campaign["id"], "running")
        target = self.service.next_action_target(user["id"], campaign["id"])
        self.assertIsNotNone(target)
        self.service.start_action_attempt(user["id"], campaign["id"], target["id"])

        interrupted = self.service.interrupt_action_campaign(
            user["id"],
            campaign["id"],
            error="lease lost during visible action",
        )
        self.assertEqual("paused", interrupted["status"])
        self.assertEqual("unknown", interrupted["targets"][0]["status"])
        self.assertEqual("unknown", interrupted["attempts"][0]["status"])

    def test_terminal_interruption_keeps_unknown_paused_and_preserves_unstarted_targets(self) -> None:
        user = self.create_user("terminal-action-fencing")
        campaign = self.service.create_action_campaign(
            user["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="window-terminal-fenced",
            targets=["possibly_clicked_target", "never_started_target"],
            message=None,
            interval_min_seconds=30,
            interval_max_seconds=60,
            limit_count=2,
        )
        self.service.set_campaign_status(user["id"], campaign["id"], "running")
        target = self.service.next_action_target(user["id"], campaign["id"])
        self.assertIsNotNone(target)
        self.service.start_action_attempt(user["id"], campaign["id"], target["id"])

        interrupted = self.service.interrupt_action_campaign(
            user["id"],
            campaign["id"],
            error="unexpected campaign task failure",
            final_status="failed",
        )
        self.assertEqual("paused", interrupted["status"])
        self.assertIsNone(interrupted["finished_at"])
        self.assertEqual(
            ["unknown", "pending"],
            [target["status"] for target in interrupted["targets"]],
        )
        self.assertEqual("unknown", interrupted["attempts"][0]["status"])
        with self.database.read() as connection:
            success_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(0, success_count)

    def test_prefenced_unknown_finish_is_idempotent_and_keeps_first_reason(self) -> None:
        user = self.create_user("prefenced-action-fencing")
        campaign = self.service.create_action_campaign(
            user["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="window-prefenced",
            targets=["possibly_sent_target"],
            message="Hello",
            interval_min_seconds=0,
            interval_max_seconds=0,
            limit_count=1,
        )
        self.service.set_campaign_status(user["id"], campaign["id"], "running")
        target = self.service.next_action_target(user["id"], campaign["id"])
        self.assertIsNotNone(target)
        attempt_id = self.service.start_action_attempt(
            user["id"], campaign["id"], target["id"]
        )

        self.service.interrupt_action_campaign(
            user["id"], campaign["id"], error="shutdown fence won the race"
        )
        self.service.finish_action_attempt(
            user["id"],
            campaign["id"],
            attempt_id,
            status="unknown",
            details={"message": "late worker unwind must not overwrite the fence"},
        )

        stored = self.service.get_action_campaign(user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self.assertEqual(
            "shutdown fence won the race",
            stored["attempts"][0]["details"]["message"],
        )
        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    """
                    SELECT COUNT(*) FROM event_log
                    WHERE entity_type='action_campaign'
                      AND entity_id=?
                      AND event_type='campaign.interrupted'
                    """,
                    (campaign["id"],),
                ).fetchone()[0],
            )

    def test_interrupt_converges_running_campaign_with_prefenced_unknown_row(self) -> None:
        user = self.create_user("partially-prefenced-action")
        campaign = self.service.create_action_campaign(
            user["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="window-partially-prefenced",
            targets=["possibly_sent_target"],
            message="Hello",
            interval_min_seconds=0,
            interval_max_seconds=0,
            limit_count=1,
        )
        self.service.set_campaign_status(user["id"], campaign["id"], "running")
        target = self.service.next_action_target(user["id"], campaign["id"])
        self.assertIsNotNone(target)
        attempt_id = self.service.start_action_attempt(
            user["id"], campaign["id"], target["id"]
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE action_attempts SET status='unknown' WHERE id=?",
                (attempt_id,),
            )
            connection.execute(
                "UPDATE action_targets SET status='unknown' WHERE id=?",
                (target["id"],),
            )
            connection.execute(
                "DELETE FROM action_dispatch_claims WHERE attempt_id=?",
                (attempt_id,),
            )

        interrupted = self.service.interrupt_action_campaign(
            user["id"],
            campaign["id"],
            error="finish the partially persisted cancellation fence",
        )

        self.assertEqual("paused", interrupted["status"])
        self.assertEqual("unknown", interrupted["targets"][0]["status"])
        self.assertEqual("unknown", interrupted["attempts"][0]["status"])
        self.assertIsNone(interrupted["finished_at"])

    def test_action_source_and_owner_scoped_counter_reset_are_persistent(self) -> None:
        owner = self.create_user("action-source-owner")
        other = self.create_user("action-source-other")
        campaign = self.service.create_action_campaign(
            owner["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="window-source-test",
            targets=["@target_one"],
            target_sources={"target_one": "@origin_account"},
            message=None,
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        self.assertEqual("@origin_account", campaign["targets"][0]["source_target"])
        reset = self.service.reset_action_counter(
            owner["id"], operation="follow", profile_id="window-source-test"
        )
        self.assertEqual("window-source-test", reset["profile_id"])
        self.assertEqual(
            [reset], self.service.list_action_counter_resets(owner["id"], operation="follow")
        )
        self.assertEqual(
            [], self.service.list_action_counter_resets(other["id"], operation="follow")
        )


class FakeBitBrowserClient(BitBrowserClient):
    def __init__(self) -> None:
        super().__init__(inventory_page_delay_seconds=0)
        self.calls: list[tuple[str, dict]] = []

    def close_profile(self, profile_id: str) -> dict:
        # This test-only provider completes its simulated close synchronously.
        # Keep the real adapter's validation/call bookkeeping, then acknowledge
        # completion explicitly; request acceptance alone is not a close proof.
        result = super().close_profile(profile_id)
        return {**result, "closed": True, "window_state": "closed"}

    def _post(self, path: str, payload: dict | None = None) -> dict:
        self.calls.append((path, payload or {}))
        if path == "/health":
            return {"success": True, "message": "ok"}
        if path == "/browser/list":
            return {
                "success": True,
                "data": {
                    "records": [
                        {"id": "window-a", "name": "IG-US-01", "groupName": "US"},
                        {"browserId": "window-b", "profileName": "IG-US-02"},
                    ],
                    "totalCount": 2,
                },
            }
        if path == "/browser/pids/all":
            return {"data": {"window-a": 1234, "window-b": 0}}
        if path == "/browser/ports":
            return {"success": True, "data": {"window-a": "64170"}}
        if path == "/browser/open":
            return {"success": True, "data": {"ws": "ws://127.0.0.1/devtools/browser/redacted"}}
        if path == "/browser/close":
            return {"success": True}
        raise AssertionError(f"Unexpected call: {path}")


class BitBrowserAdapterTestCase(unittest.TestCase):
    def test_defensive_profile_normalization_and_explicit_open(self) -> None:
        client = FakeBitBrowserClient()
        listing = client.list_windows(page=0, page_size=100, name="IG")
        self.assertEqual(2, listing["total"])
        self.assertEqual("IG-US-01", listing["windows"][0]["name"])
        self.assertTrue(listing["windows"][0]["is_open"])
        self.assertFalse(listing["windows"][1]["is_open"])

        opened = client.open_profile("window-a")
        self.assertTrue(opened["provider_success"])
        self.assertTrue(opened["endpoint_available"])
        open_call = next(payload for path, payload in client.calls if path == "/browser/open")
        self.assertEqual("https://www.instagram.com/", open_call["newPageUrl"])
        self.assertTrue(open_call["queue"])
        ports = client.profile_ports("window-a")
        self.assertTrue(ports["endpoint_available"])
        ports_call = next(payload for path, payload in client.calls if path == "/browser/ports")
        self.assertEqual({}, ports_call)
        self.assertEqual("http://127.0.0.1:64170", client.connection_endpoint("window-a")["http"])
        closed = client.close_profile("window-a")
        self.assertTrue(closed["provider_success"])
        self.assertIs(closed["closed"], True)
        self.assertEqual("closed", closed["window_state"])

    def test_single_open_port_is_not_misattributed_to_a_closed_profile(self) -> None:
        client = FakeBitBrowserClient()

        endpoint = client.connection_endpoint("window-b", open_if_needed=True)

        self.assertEqual(
            "ws://127.0.0.1/devtools/browser/redacted", endpoint["ws"]
        )
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in client.calls))
        open_calls = [
            payload for path, payload in client.calls if path == "/browser/open"
        ]
        self.assertEqual(["window-b"], [payload["id"] for payload in open_calls])

    def test_100_concurrent_worker_connections_share_one_ports_snapshot(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        class HundredOpenProfilesClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/ports":
                    # Keep the first provider read in flight long enough for every
                    # worker thread to queue behind the single-flight lock.
                    time.sleep(0.03)
                    return {
                        "success": True,
                        "data": {
                            f"open-window-{index}": 64_000 + index
                            for index in range(100)
                        },
                    }
                if path == "/browser/open":
                    raise AssertionError("An already-open profile must not be opened again")
                return super()._post(path, payload)

        client = HundredOpenProfilesClient()
        client.connection_ports_cache_seconds = 60
        profile_ids = [f"open-window-{index}" for index in range(100)]
        with ThreadPoolExecutor(max_workers=32) as executor:
            endpoints = list(
                executor.map(
                    lambda profile_id: client.connection_endpoint(
                        profile_id, open_if_needed=True
                    ),
                    profile_ids,
                )
            )

        self.assertEqual(
            [f"http://127.0.0.1:{64_000 + index}" for index in range(100)],
            [endpoint["http"] for endpoint in endpoints],
        )
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in client.calls))
        self.assertEqual(0, sum(path == "/browser/open" for path, _ in client.calls))

        # Reattaching the same workers during the short TTL is a pure cache read.
        for profile_id in profile_ids:
            self.assertIsNotNone(
                client.connection_endpoint(profile_id, open_if_needed=True)["http"]
            )
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in client.calls))
        self.assertEqual(0, sum(path == "/browser/open" for path, _ in client.calls))

    def test_100_process_clients_share_connection_ports_singleflight(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import threading
        import time

        api_key = f"connection-singleflight-{time.monotonic_ns()}"
        clients = [
            BitBrowserClient(
                api_key=api_key,
                connection_ports_cache_seconds=60,
            )
            for _ in range(100)
        ]
        profile_ids = [f"shared-window-{index}" for index in range(100)]
        calls: list[tuple[str, dict]] = []
        call_lock = threading.Lock()

        def provider_call(
            _client: BitBrowserClient,
            path: str,
            payload: dict | None = None,
        ) -> dict:
            with call_lock:
                calls.append((path, payload or {}))
            if path == "/browser/ports":
                time.sleep(0.03)
                return {
                    "success": True,
                    "data": {
                        profile_id: 62_000 + index
                        for index, profile_id in enumerate(profile_ids)
                    },
                }
            if path == "/browser/open":
                raise AssertionError("An existing profile must not be reopened")
            raise AssertionError(f"Unexpected call: {path}")

        with patch.object(BitBrowserClient, "_post", provider_call):
            with ThreadPoolExecutor(max_workers=32) as executor:
                endpoints = list(
                    executor.map(
                        lambda pair: pair[0].connection_endpoint(
                            pair[1], open_if_needed=True
                        ),
                        zip(clients, profile_ids),
                    )
                )

        self.assertTrue(all(endpoint["http"] for endpoint in endpoints))
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in calls))
        self.assertEqual(0, sum(path == "/browser/open" for path, _ in calls))

    def test_connection_endpoint_opens_each_truly_missing_profile_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        class MixedOpenProfilesClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                request = payload or {}
                self.calls.append((path, request))
                if path == "/browser/ports":
                    time.sleep(0.02)
                    return {
                        "success": True,
                        "data": {
                            f"mixed-window-{index}": 63_000 + index
                            for index in range(90)
                        },
                    }
                if path == "/browser/open":
                    profile_id = str(request["id"])
                    return {
                        "success": True,
                        "data": {
                            "id": profile_id,
                            "ws": f"ws://127.0.0.1/devtools/browser/{profile_id}",
                        },
                    }
                return super()._post(path, payload)

        client = MixedOpenProfilesClient()
        client.connection_ports_cache_seconds = 60
        profile_ids = [f"mixed-window-{index}" for index in range(100)]
        closed_profile_ids = [f"mixed-window-{index}" for index in range(90, 100)]
        # Repeating every missing profile in the same attach wave proves each open
        # command is single-flight, while the 90 existing windows are never reopened.
        requests = profile_ids + closed_profile_ids * 12
        with ThreadPoolExecutor(max_workers=32) as executor:
            endpoints = list(
                executor.map(
                    lambda profile_id: client.connection_endpoint(
                        profile_id, open_if_needed=True
                    ),
                    requests,
                )
            )

        self.assertTrue(all(endpoint["ws"] or endpoint["http"] for endpoint in endpoints))
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in client.calls))
        open_calls = [
            payload for path, payload in client.calls if path == "/browser/open"
        ]
        self.assertEqual(len(closed_profile_ids), len(open_calls))
        self.assertCountEqual(closed_profile_ids, [item["id"] for item in open_calls])

    def test_queued_open_without_endpoint_is_not_repeated_after_ports_refresh(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        class QueuedOpenClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/ports":
                    return {"success": True, "data": {}}
                if path == "/browser/open":
                    # BitBrowser may acknowledge a queued open before its CDP endpoint
                    # appears in /browser/ports or in the open response itself.
                    return {"success": True, "data": {}}
                return super()._post(path, payload)

        client = QueuedOpenClient()
        client.connection_ports_cache_seconds = 60
        client.connection_open_cooldown_seconds = 60

        with ThreadPoolExecutor(max_workers=24) as executor:
            first_wave = list(
                executor.map(
                    lambda _: client.connection_endpoint(
                        "queued-window", open_if_needed=True
                    ),
                    range(48),
                )
            )
        self.assertTrue(
            all(not endpoint["ws"] and not endpoint["http"] for endpoint in first_wave)
        )
        self.assertEqual(1, sum(path == "/browser/ports" for path, _ in client.calls))
        self.assertEqual(1, sum(path == "/browser/open" for path, _ in client.calls))

        # Force only the ports snapshot to expire. Even if a fresh snapshot still has
        # no endpoint, the queued open stays single-flight for the longer cooldown.
        with client._shared_state.connection_ports_lock:
            client._shared_state.connection_ports_cache = (
                time.monotonic() - client.connection_ports_cache_seconds - 1,
                {},
            )
        with ThreadPoolExecutor(max_workers=24) as executor:
            second_wave = list(
                executor.map(
                    lambda _: client.connection_endpoint(
                        "queued-window", open_if_needed=True
                    ),
                    range(48),
                )
            )
        self.assertTrue(
            all(not endpoint["ws"] and not endpoint["http"] for endpoint in second_wave)
        )
        self.assertEqual(2, sum(path == "/browser/ports" for path, _ in client.calls))
        self.assertEqual(1, sum(path == "/browser/open" for path, _ in client.calls))

    def test_connection_failure_clears_endpoints_and_is_negative_cached(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        class FlakyPortsClient(FakeBitBrowserClient):
            fail = False
            debug_port = 64_170

            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/ports":
                    if self.fail:
                        raise UpstreamUnavailableError("temporary ports failure")
                    return {
                        "success": True,
                        "data": {"flaky-window": type(self).debug_port},
                    }
                if path == "/browser/open":
                    raise AssertionError("A ports failure must not fan out into opens")
                return super()._post(path, payload)

        client = FlakyPortsClient()
        client.connection_ports_cache_seconds = 60
        client.connection_failure_cache_seconds = 60
        self.assertEqual(
            "http://127.0.0.1:64170",
            client.connection_endpoint("flaky-window", open_if_needed=True)["http"],
        )
        client._invalidate_connection_cache()
        client.fail = True
        with ThreadPoolExecutor(max_workers=24) as executor:
            futures = [
                executor.submit(
                    client.connection_endpoint, "flaky-window", open_if_needed=True
                )
                for _ in range(24)
            ]
            for future in futures:
                with self.assertRaises(UpstreamUnavailableError):
                    future.result(timeout=2)

        # Exactly one failed ports call represents the whole concurrent wave; stale
        # endpoint data was removed and no caller attempted /browser/open.
        self.assertEqual(2, sum(path == "/browser/ports" for path, _ in client.calls))
        self.assertEqual(0, sum(path == "/browser/open" for path, _ in client.calls))
        self.assertIsNone(client._shared_state.connection_ports_cache)
        self.assertEqual({}, client._shared_state.connection_endpoint_cache)

        client.fail = False
        type(client).debug_port = 64_171
        client.connection_failure_cache_seconds = 0
        self.assertEqual(
            "http://127.0.0.1:64171",
            client.connection_endpoint("flaky-window", open_if_needed=True)["http"],
        )

    def test_cached_connection_endpoint_never_bypasses_provider_circuits(self) -> None:
        cases = (
            ("auth", BitBrowserAuthRequiredError),
            ("rate", BitBrowserRateLimitedError),
        )
        for circuit, expected_error in cases:
            with self.subTest(circuit=circuit):
                client = FakeBitBrowserClient()
                client.connection_ports_cache_seconds = 60
                self.assertIsNotNone(
                    client.connection_endpoint("window-a", open_if_needed=True)["http"]
                )
                calls_before = len(client.calls)
                if circuit == "auth":
                    client._trip_auth_circuit(
                        path="/browser/list", provider_message="Login out!"
                    )
                else:
                    client._trip_rate_limit_circuit(
                        path="/browser/list", provider_message="requests too frequent"
                    )
                with self.assertRaises(expected_error):
                    client.connection_endpoint("window-a", open_if_needed=True)
                self.assertEqual(calls_before, len(client.calls))
                self.assertIsNone(client._shared_state.connection_ports_cache)
                self.assertEqual({}, client._shared_state.connection_endpoint_cache)

    def test_all_profiles_pages_until_short_page_when_total_is_absent(self) -> None:
        class PaginatedClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/list":
                    page = (payload or {}).get("page", 0)
                    start = page * 100
                    records = [
                        {"id": f"window-{index}", "name": f"IG-{index:03d}"}
                        for index in range(start, min(start + 100, 101))
                    ]
                    return {"success": True, "data": {"records": records}}
                if path == "/browser/pids/all":
                    return {"success": True, "data": {}}
                return super()._post(path, payload)

        client = PaginatedClient()
        listing = client.list_all_windows()
        self.assertEqual(101, listing["total"])
        self.assertEqual(101, len(listing["windows"]))
        pages = [payload["page"] for path, payload in client.calls if path == "/browser/list"]
        self.assertEqual([0, 1], pages)
        self.assertEqual(1, sum(path == "/browser/pids/all" for path, _ in client.calls))
        # The immediate second UI refresh reuses the complete successful snapshot.
        self.assertEqual(101, client.list_all_windows()["total"])
        self.assertEqual([0, 1], [payload["page"] for path, payload in client.calls if path == "/browser/list"])

    def test_680_profile_polling_uses_seven_pages_one_pid_read_and_sixty_second_floor(self) -> None:
        import time

        class ManyProfilesClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/list":
                    page = (payload or {}).get("page", 0)
                    start = page * 100
                    records = [
                        {"id": f"large-window-{index}", "name": f"IG-{index:04d}"}
                        for index in range(start, min(start + 100, 680))
                    ]
                    return {
                        "success": True,
                        "data": {"records": records, "totalNum": 680},
                    }
                if path == "/browser/pids/all":
                    return {"success": True, "data": {}}
                return super()._post(path, payload)

        client = ManyProfilesClient()
        first = client.list_all_windows()
        self.assertEqual(680, first["total"])
        self.assertEqual("fresh", first["connection"]["source"])
        self.assertEqual("connected", first["connection"]["state"])
        self.assertEqual(54345, first["connection"]["port"])
        self.assertEqual(0, first["connection"]["consecutive_failures"])
        self.assertGreaterEqual(first["connection"]["recommended_poll_interval_ms"], 60_000)
        self.assertEqual(7, sum(path == "/browser/list" for path, _ in client.calls))
        self.assertEqual(1, sum(path == "/browser/pids/all" for path, _ in client.calls))

        # A renderer poll two seconds later consumes no Local API request.
        _, cached_value = client._profiles_cache[""]
        client._profiles_cache[""] = (time.monotonic() - 2, cached_value)
        cached = client.list_all_windows()
        self.assertEqual(680, cached["total"])
        self.assertEqual("cache", cached["connection"]["source"])
        self.assertTrue(cached["connection"]["reachable"])
        self.assertGreaterEqual(cached["connection"]["cache_age_ms"], 1900)
        self.assertEqual(8, len(client.calls))

        # Once the hard minute floor expires, a fresh cycle is exactly 7+1 calls.
        _, cached_value = client._profiles_cache[""]
        client._profiles_cache[""] = (time.monotonic() - 61, cached_value)
        client._shared_state.next_inventory_refresh_not_before = 0
        self.assertEqual(680, client.list_all_windows()["total"])
        self.assertEqual(14, sum(path == "/browser/list" for path, _ in client.calls))
        self.assertEqual(2, sum(path == "/browser/pids/all" for path, _ in client.calls))

    def test_transient_read_failure_retries_but_open_is_never_replayed(self) -> None:
        class TransientClient(BitBrowserClient):
            def __init__(self) -> None:
                super().__init__(auto_detect=False, retry_backoff_seconds=0)
                self.attempts: list[str] = []

            def _post_once(
                self,
                base_url: str,
                path: str,
                payload: dict | None = None,
                *,
                timeout_seconds: float | None = None,
            ) -> dict:
                del base_url, payload, timeout_seconds
                self.attempts.append(path)
                if len([item for item in self.attempts if item == path]) == 1:
                    raise UpstreamUnavailableError("temporary local API timeout")
                return {"success": True, "data": {"records": []}}

        client = TransientClient()
        self.assertTrue(client._post("/browser/list", {})["success"])
        self.assertEqual(2, client.attempts.count("/browser/list"))
        with self.assertRaises(UpstreamUnavailableError):
            client._post("/browser/open", {"id": "window-a"})
        self.assertEqual(1, client.attempts.count("/browser/open"))

    def test_verified_url_auxiliary_failure_does_not_probe_every_port(self) -> None:
        class AuxiliaryFailureClient(BitBrowserClient):
            def __init__(self) -> None:
                super().__init__(retry_backoff_seconds=0)
                self.attempts = 0
                self.detects = 0

            def _post_once(
                self,
                base_url: str,
                path: str,
                payload: dict | None = None,
                *,
                timeout_seconds: float | None = None,
            ) -> dict:
                del base_url, path, payload, timeout_seconds
                self.attempts += 1
                raise UpstreamUnavailableError("temporary pids timeout")

            def _detect_base_url(self, *, exclude_base_url: str | None = None) -> str | None:
                del exclude_base_url
                self.detects += 1
                return None

        client = AuxiliaryFailureClient()
        with self.assertRaises(UpstreamUnavailableError):
            client._post("/browser/pids/all", {})
        self.assertEqual(2, client.attempts)
        self.assertEqual(0, client.detects)

    def test_health_uses_lightweight_probe_and_caches_success_and_failure(self) -> None:
        class HealthClient(FakeBitBrowserClient):
            fail = False

            def _post(self, path: str, payload: dict | None = None) -> dict:
                if self.fail:
                    raise UpstreamUnavailableError("BitBrowser disconnected")
                return super()._post(path, payload)

        client = HealthClient()
        self.assertTrue(client.health()["connected"])
        self.assertTrue(client.health()["connected"])
        self.assertEqual(1, sum(path == "/health" for path, _ in client.calls))
        client.fail = True
        client.health_cache_seconds = 0
        with self.assertRaises(UpstreamUnavailableError):
            client.health()
        attempts_after_failure = len(client.calls)
        with self.assertRaises(UpstreamUnavailableError):
            client.health()
        self.assertEqual(attempts_after_failure, len(client.calls))
        client.fail = False
        client.health_failure_cache_seconds = 0
        self.assertTrue(client.health()["connected"])

    def test_concurrent_profile_refreshes_share_snapshot_and_stale_fallback_is_bounded(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import time

        class SlowClient(FakeBitBrowserClient):
            uncached_reads = 0
            fail = False

            def _list_all_windows_uncached(self, *, name: str = "") -> dict:
                type(self).uncached_reads += 1
                time.sleep(0.05)
                if self.fail:
                    raise UpstreamUnavailableError("BitBrowser disconnected")
                return super()._list_all_windows_uncached(name=name)

        client = SlowClient()
        with ThreadPoolExecutor(max_workers=2) as executor:
            totals = list(executor.map(lambda _: client.list_all_windows()["total"], range(2)))
        self.assertEqual([2, 2], totals)
        self.assertEqual(1, SlowClient.uncached_reads)

        # A just-expired snapshot may bridge one brief refresh failure.
        client.profiles_cache_seconds = 0
        client.profiles_min_refresh_interval_seconds = 0
        client.profiles_failure_cache_seconds = 0
        client._shared_state.next_inventory_refresh_not_before = 0
        client.fail = True
        stale = client.list_all_windows()
        self.assertTrue(stale["stale"])
        self.assertEqual("stale", stale["connection"]["source"])
        self.assertEqual("degraded", stale["connection"]["state"])
        self.assertFalse(stale["connection"]["reachable"])
        self.assertEqual(1, stale["connection"]["consecutive_failures"])
        self.assertIsNotNone(stale["connection"]["last_success_at"])
        self.assertIsNotNone(stale["connection"]["last_failure_at"])
        # The fallback has a hard maximum age; disabling it demonstrates that a
        # sustained disconnect is surfaced instead of being hidden indefinitely.
        client.profiles_stale_if_error_seconds = 0
        with self.assertRaises(UpstreamUnavailableError):
            client.list_all_windows()

    def test_first_failure_rejects_a_snapshot_older_than_the_stale_age_limit(self) -> None:
        import time

        class AgingClient(FakeBitBrowserClient):
            fail = False

            def _list_all_windows_uncached(self, *, name: str = "") -> dict:
                if self.fail:
                    raise UpstreamUnavailableError("BitBrowser disconnected")
                return super()._list_all_windows_uncached(name=name)

        client = AgingClient()
        self.assertEqual(2, client.list_all_windows()["total"])
        cached_at, cached_listing = client._shared_state.profiles_cache[""]
        del cached_at
        client.profiles_cache_seconds = 0
        client.profiles_min_refresh_interval_seconds = 0
        client.profiles_failure_cache_seconds = 0
        client.profiles_stale_if_error_seconds = 10
        client._shared_state.next_inventory_refresh_not_before = 0
        client._shared_state.profiles_cache[""] = (
            time.monotonic() - 11,
            cached_listing,
        )
        client.fail = True

        # This is the outage's first failure, but the last successful inventory is
        # already beyond the ten-second cap and must not receive another ten seconds.
        with self.assertRaises(UpstreamUnavailableError):
            client.list_all_windows()

    def test_documented_total_num_and_nested_envelope_are_supported(self) -> None:
        class NestedClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/list":
                    return {
                        "status": "success",
                        "data": {
                            "success": True,
                            "data": {
                                "list": [{"id": "nested-window", "name": "IG-NESTED"}],
                                "totalNum": 37,
                            },
                        },
                    }
                if path == "/browser/pids/all":
                    return {"success": True, "data": {"nested-window": 1001}}
                return super()._post(path, payload)

        listing = NestedClient().list_windows()
        self.assertEqual(1, listing["total"])
        self.assertEqual("nested-window", listing["windows"][0]["id"])
        self.assertTrue(listing["windows"][0]["is_open"])

    def test_application_level_rejection_is_not_reported_as_connected(self) -> None:
        class RejectedClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/list":
                    return {"success": False, "msg": "Local API authentication failed"}
                return super()._post(path, payload)

        listing = RejectedClient().list_windows()
        self.assertEqual("auth_required", listing["connection"]["state"])
        self.assertTrue(listing["connection"]["auth_required"])
        self.assertEqual([], listing["windows"])

    def test_health_uses_the_documented_lightweight_probe(self) -> None:
        client = FakeBitBrowserClient()
        self.assertTrue(client.health()["connected"])
        self.assertEqual("/health", client.calls[0][0])

    def test_profiles_without_an_id_are_ignored(self) -> None:
        class MissingIdClient(FakeBitBrowserClient):
            def _post(self, path: str, payload: dict | None = None) -> dict:
                self.calls.append((path, payload or {}))
                if path == "/browser/list":
                    return {"success": True, "data": {"list": [{"name": "invalid"}, {"id": "valid", "name": "IG-VALID"}]}}
                if path == "/browser/pids/all":
                    return {"success": True, "data": {}}
                return super()._post(path, payload)

        listing = MissingIdClient().list_windows()
        self.assertEqual(["valid"], [item["id"] for item in listing["windows"]])

    def test_login_out_trips_circuit_without_retry_scan_or_snapshot_loss(self) -> None:
        class CircuitClient(BitBrowserClient):
            def __init__(self) -> None:
                super().__init__(
                    retry_backoff_seconds=0,
                    profiles_cache_seconds=0,
                    profiles_min_refresh_interval_seconds=0,
                    profiles_failure_cache_seconds=0,
                    auth_manual_probe_min_interval_seconds=0,
                    auth_full_refresh_delay_seconds=60,
                    inventory_page_delay_seconds=0,
                )
                self.mode = "ok"
                self.calls: list[str] = []
                self.detects = 0

            def _post_once(
                self,
                base_url,
                path,
                payload=None,
                *,
                timeout_seconds=None,
                recovery_probe=False,
            ):
                del base_url, payload, timeout_seconds, recovery_probe
                self.calls.append(path)
                if path == "/health":
                    return {"success": True}
                if path == "/browser/list":
                    if self.mode == "auth":
                        return {"success": False, "msg": "Login out!"}
                    return {"success": True, "data": {"records": [{"id": "window-a"}]}}
                if path == "/browser/pids/all":
                    return {"success": True, "data": {}}
                if path == "/browser/open":
                    return {"success": True, "data": {}}
                raise AssertionError(path)

            def _detect_base_url(self, *, exclude_base_url=None):
                del exclude_base_url
                self.detects += 1
                return None

        client = CircuitClient()
        self.assertEqual(1, client.list_all_windows()["total"])
        client._shared_state.next_inventory_refresh_not_before = 0
        client.mode = "auth"
        before = len(client.calls)
        blocked = client.list_all_windows()
        self.assertEqual(before + 1, len(client.calls))
        self.assertEqual(0, client.detects)
        self.assertEqual("auth_required", blocked["connection"]["state"])
        self.assertEqual(["window-a"], [item["id"] for item in blocked["windows"]])
        calls_after_trip = len(client.calls)
        with self.assertRaises(BitBrowserAuthRequiredError):
            client.open_profile("window-a")
        self.assertEqual(calls_after_trip, len(client.calls))
        self.assertEqual(1, len(client.list_all_windows()["windows"]))
        self.assertEqual(calls_after_trip, len(client.calls))

        client.mode = "ok"
        client._shared_state.auth_probe_not_before = 0
        recovered = client.confirm_login()
        self.assertEqual("recovering", recovered["connection"]["state"])
        self.assertEqual(["/health", "/browser/list"], client.calls[-2:])

    def test_frequency_rejection_has_zero_retry_and_blocks_operations(self) -> None:
        class LimitedClient(BitBrowserClient):
            def __init__(self) -> None:
                super().__init__(retry_backoff_seconds=0, inventory_page_delay_seconds=0)
                self.calls = 0
                self.detects = 0

            def _post_once(self, base_url, path, payload=None, *, timeout_seconds=None):
                del base_url, path, payload, timeout_seconds
                self.calls += 1
                return {"success": False, "msg": "browser/list requests too frequent"}

            def _detect_base_url(self, *, exclude_base_url=None):
                del exclude_base_url
                self.detects += 1
                return None

        client = LimitedClient()
        with self.assertRaises(BitBrowserRateLimitedError):
            client._post("/browser/list", {"page": 0, "pageSize": 100})
        self.assertEqual(1, client.calls)
        self.assertEqual(0, client.detects)
        with self.assertRaises(BitBrowserRateLimitedError):
            client.open_profile("window-a")
        self.assertEqual(1, client.calls)
        listing = client.list_all_windows()
        self.assertEqual("rate_limited", listing["connection"]["state"])

    def test_queued_requests_recheck_auth_and_rate_circuits_inside_provider_lock(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import threading
        import time

        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback

            def read(self, _limit):
                return self.body

        cases = (
            (b'{"success": false, "msg": "Login out!"}', BitBrowserAuthRequiredError),
            (
                b'{"success": false, "msg": "browser/list requests too frequent"}',
                BitBrowserRateLimitedError,
            ),
        )
        for body, expected_error in cases:
            with self.subTest(expected_error=expected_error.__name__):
                api_key = f"queued-circuit-{expected_error.__name__}-{time.monotonic_ns()}"
                clients = [
                    BitBrowserClient(api_key=api_key, auto_detect=False, read_retry_attempts=1)
                    for _ in range(2)
                ]
                provider_entered = threading.Event()
                release_provider = threading.Event()
                second_started = threading.Event()
                calls_lock = threading.Lock()
                provider_calls = 0

                def urlopen(_request, *, timeout):
                    nonlocal provider_calls
                    del timeout
                    with calls_lock:
                        provider_calls += 1
                    provider_entered.set()
                    self.assertTrue(release_provider.wait(2))
                    return Response(body)

                def queued_call():
                    second_started.set()
                    return clients[1]._post("/browser/open", {"id": "window-b"})

                with patch("support.legacy_bitbrowser_fixture.urllib.request.urlopen", side_effect=urlopen):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(
                            clients[0]._post,
                            "/browser/open",
                            {"id": "window-a"},
                        )
                        self.assertTrue(provider_entered.wait(2))
                        second = executor.submit(queued_call)
                        self.assertTrue(second_started.wait(2))
                        release_provider.set()
                        with self.assertRaises(expected_error):
                            first.result(timeout=2)
                        with self.assertRaises(expected_error):
                            second.result(timeout=2)
                self.assertEqual(1, provider_calls)

    def test_recovery_health_probe_switches_between_rate_and_auth_circuits(self) -> None:
        class RecoveryTransitionClient(BitBrowserClient):
            def __init__(self, health_error) -> None:
                super().__init__(
                    auth_manual_probe_min_interval_seconds=0,
                    auth_probe_interval_seconds=300,
                )
                self.health_error = health_error
                self.calls: list[tuple[str, bool]] = []

            def _post_once(
                self,
                base_url,
                path,
                payload=None,
                *,
                timeout_seconds=None,
                recovery_probe=False,
            ):
                del base_url, payload, timeout_seconds
                self.calls.append((path, recovery_probe))
                if path == "/health":
                    raise self.health_error
                raise AssertionError(path)

        rate_to_auth = RecoveryTransitionClient(
            BitBrowserAuthRequiredError(
                "login required",
                details={"path": "/health", "provider_message": "Login out!"},
            )
        )
        rate_to_auth._trip_rate_limit_circuit(path="/browser/list")
        rate_to_auth._shared_state.rate_limit_probe_not_before = 0
        auth_listing = rate_to_auth.confirm_login()
        self.assertEqual("auth_required", auth_listing["connection"]["state"])
        self.assertIsNotNone(rate_to_auth._shared_state.auth_required_since_epoch)
        self.assertIsNone(rate_to_auth._shared_state.rate_limited_since_epoch)
        self.assertEqual([("/health", True)], rate_to_auth.calls)

        auth_to_rate = RecoveryTransitionClient(
            BitBrowserRateLimitedError(
                "rate limited",
                details={"path": "/health", "provider_message": "requests too frequent"},
            )
        )
        auth_to_rate._trip_auth_circuit(path="/browser/list")
        auth_to_rate._shared_state.auth_probe_not_before = 0
        rate_listing = auth_to_rate.confirm_login()
        self.assertEqual("rate_limited", rate_listing["connection"]["state"])
        self.assertIsNone(auth_to_rate._shared_state.auth_required_since_epoch)
        self.assertIsNotNone(auth_to_rate._shared_state.rate_limited_since_epoch)
        self.assertEqual([("/health", True)], auth_to_rate.calls)

        ordinary_failure = RecoveryTransitionClient(
            UpstreamUnavailableError("temporary health timeout")
        )
        ordinary_failure._trip_auth_circuit(path="/browser/list")
        ordinary_failure._shared_state.auth_probe_not_before = 0
        blocked = ordinary_failure.confirm_login()
        self.assertEqual("auth_required", blocked["connection"]["state"])
        self.assertFalse(blocked["connection"]["reachable"])
        self.assertIsNotNone(ordinary_failure._shared_state.auth_required_since_epoch)
        self.assertEqual([("/health", True)], ordinary_failure.calls)

    def test_cached_circuit_does_not_retarget_port_detection_and_bypass_is_bounded(self) -> None:
        import time

        client = BitBrowserClient(api_key=f"detect-circuit-{time.monotonic_ns()}")
        original_base_url = client.base_url
        client._trip_auth_circuit(path="/browser/list", provider_message="Login out!")

        # The first non-excluded candidate is a different port. Its locked circuit
        # check is a local rejection, not evidence that the candidate returned an
        # auth response, so auto-detection must not adopt that uncontacted port.
        with self.assertRaises(BitBrowserAuthRequiredError) as caught:
            client._detect_base_url(exclude_base_url=original_base_url)
        self.assertTrue(caught.exception.details["provider_call_skipped"])
        self.assertEqual(original_base_url, client.base_url)

        with self.assertRaises(ValueError):
            client._post_once(
                original_base_url,
                "/browser/open",
                {"id": "window-a"},
                recovery_probe=True,
            )

    def test_process_clients_share_inventory_singleflight_and_failure_cache(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import threading
        import time

        api_key = f"singleflight-{time.monotonic_ns()}"
        clients = [
            BitBrowserClient(
                api_key=api_key,
                profiles_cache_seconds=60,
                profiles_min_refresh_interval_seconds=60,
                inventory_page_delay_seconds=0,
            )
            for _ in range(2)
        ]
        call_lock = threading.Lock()
        calls = 0

        def successful_read(_client, *, name=""):
            nonlocal calls
            del name
            with call_lock:
                calls += 1
            time.sleep(0.05)
            return {"total": 1, "windows": [{"id": "shared"}], "provider_success": True}

        with patch.object(BitBrowserClient, "_list_all_windows_uncached", successful_read):
            with ThreadPoolExecutor(max_workers=2) as executor:
                totals = list(executor.map(lambda item: item.list_all_windows()["total"], clients))
        self.assertEqual([1, 1], totals)
        self.assertEqual(1, calls)

    def test_process_clients_serialize_all_local_api_requests_without_deadlock(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import threading
        import time

        api_key = f"request-lock-{time.monotonic_ns()}"
        clients = [BitBrowserClient(api_key=api_key) for _ in range(2)]
        counter_lock = threading.Lock()
        active = 0
        maximum_active = 0

        class Response:
            def __enter__(self):
                nonlocal active, maximum_active
                with counter_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.03)
                return self

            def __exit__(self, exc_type, exc, traceback):
                nonlocal active
                del exc_type, exc, traceback
                with counter_lock:
                    active -= 1

            @staticmethod
            def read(_limit):
                return b'{"success": true}'

        with patch("support.legacy_bitbrowser_fixture.urllib.request.urlopen", return_value=Response()):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda client: client._post_once(
                            client.base_url,
                            "/health",
                            {},
                        ),
                        clients,
                    )
                )
        self.assertEqual([True, True], [item["success"] for item in results])
        self.assertEqual(1, maximum_active)
        self.assertEqual(0, active)

    def test_visible_dom_guard_classification(self) -> None:
        self.assertEqual(
            "instagram_login_required",
            classify_guard_state("https://www.instagram.com/accounts/login/", "Instagram"),
        )
        self.assertEqual(
            "instagram_challenge",
            classify_guard_state("https://www.instagram.com/challenge/123/", "Confirm it's you"),
        )
        self.assertEqual(
            "instagram_rate_limited",
            classify_guard_state("https://www.instagram.com/example/", "Please wait a few minutes"),
        )
        self.assertIsNone(classify_guard_state("https://www.instagram.com/example/", "12 posts"))

    def test_visible_profile_metrics_support_instagram_meta_text(self) -> None:
        self.assertEqual(
            (12_300, 892, 178),
            extract_visible_metrics("12.3K Followers, 892 Following, 178 Posts - Instagram profile"),
        )

    def test_visible_profile_metrics_support_common_instagram_ui_languages(self) -> None:
        samples = {
            "zh": "178 帖子 1.2万 粉丝 892 关注",
            "vi": "178 bài viết 12,3K người theo dõi 892 đang theo dõi",
            "th": "178 โพสต์ 12.3K ผู้ติดตาม 892 กำลังติดตาม",
            "es": "178 publicaciones 12,3 mil seguidores 892 siguiendo",
            "pt": "178 publicações 12,3 mil seguidores 892 seguindo",
            "id": "178 postingan 12,3 rb pengikut 892 mengikuti",
            "ja": "投稿 178 フォロワー 1.2万 フォロー中 892",
            "ko": "게시물 178 팔로워 1.2만 팔로잉 892",
        }
        for language, text in samples.items():
            with self.subTest(language=language):
                self.assertEqual((12_000 if language in {"zh", "ja", "ko"} else 12_300, 892, 178), extract_visible_metrics(text))

    def test_guard_classifies_localized_manual_and_unavailable_pages(self) -> None:
        self.assertEqual(
            "instagram_login_required",
            classify_guard_state("https://www.instagram.com/", "เข้าสู่ระบบ Instagram"),
        )
        self.assertEqual(
            "instagram_challenge",
            classify_guard_state("https://www.instagram.com/checkpoint/123/", "Instagram"),
        )
        self.assertEqual(
            "instagram_content_not_visible",
            classify_guard_state("https://www.instagram.com/missing/", "Esta página no está disponible."),
        )

    def test_visible_profile_privacy_uses_rendered_evidence_not_counts(self) -> None:
        for marker in (
            "This account is private",
            "此账户为私密账户",
            "บัญชีนี้เป็นส่วนตัว",
            "Esta cuenta es privada",
            "このアカウントは非公開です",
        ):
            self.assertEqual(
                "private",
                classify_profile_visibility(marker, has_visible_posts=False),
            )
        self.assertEqual(
            "unknown",
            classify_profile_visibility(
                "178 Posts 12.3K Followers 892 Following",
                has_visible_posts=False,
            ),
        )
        self.assertEqual(
            "public",
            classify_profile_visibility("Profile header", has_visible_posts=True),
        )
        self.assertEqual(
            "public",
            classify_profile_visibility("尚无帖子", has_visible_posts=False),
        )
        self.assertEqual(
            "public",
            classify_profile_visibility(
                "sample_empty01 0帖子 23粉丝 8关注 这里空荡荡~",
                has_visible_posts=False,
            ),
        )
        self.assertEqual(
            "private",
            classify_profile_visibility(
                "这是私密账户 0帖子 23粉丝 8关注 这里空荡荡~",
                has_visible_posts=False,
            ),
        )
        self.assertEqual(
            "unknown",
            classify_profile_visibility(
                "1帖子 23粉丝 8关注 这里空荡荡~",
                has_visible_posts=False,
            ),
        )

    def test_current_chinese_zero_post_surface_supplies_terminal_metrics(self) -> None:
        synthetic_profile_text = "sample_empty01\nSample Empty\n0帖子 23粉丝 8关注\n关注\n这里空荡荡~"
        self.assertEqual(
            (23, 8, 0),
            extract_public_empty_profile_metrics(synthetic_profile_text),
        )
        self.assertIsNone(
            extract_public_empty_profile_metrics("0帖子 23粉丝 8关注")
        )
        self.assertIsNone(
            extract_public_empty_profile_metrics(
                "1帖子 23粉丝 8关注 这里空荡荡~"
            )
        )
        self.assertIsNone(
            extract_public_empty_profile_metrics(
                "这是私密账户 0帖子 23粉丝 8关注 这里空荡荡~"
            )
        )

    def test_current_chinese_private_zero_post_copy_outranks_public_empty_shortcut(self) -> None:
        synthetic_profile_text = (
            "sample_private01_\nSample Private\n0帖子 246粉丝 864关注\n"
            "这是私密主页\n关注即可查看其照片和视频。\n这里空荡荡~"
        )

        self.assertEqual(
            "private",
            classify_profile_visibility(
                synthetic_profile_text,
                # A stale bootstrap may still say public and a recommendation tile
                # may match the broad post-link selector. Exact rendered private copy
                # must outrank both weaker public-looking signals.
                has_visible_posts=True,
                structured_is_private=False,
            ),
        )
        self.assertIsNone(extract_public_empty_profile_metrics(synthetic_profile_text))
        for private_copy in (
            "这是私密主页",
            "关注即可查看其照片和视频。",
            "這是私密主頁",
            "關注即可查看其相片和影片。",
        ):
            with self.subTest(private_copy=private_copy):
                self.assertEqual(
                    "private",
                    classify_profile_visibility(
                        f"0帖子 246粉丝 864关注\n{private_copy}",
                        has_visible_posts=True,
                        structured_is_private=False,
                    ),
                )

    def test_private_profile_relationship_prompts_and_lock_indicator_are_supported(self) -> None:
        self.assertEqual(
            "private",
            classify_profile_visibility(
                "Follow this account to see their photos and videos.",
                has_visible_posts=False,
            ),
        )
        self.assertEqual(
            "private",
            classify_profile_visibility(
                "Profile header and visible posts",
                has_visible_posts=True,
                has_private_indicator=True,
            ),
        )
        self.assertEqual(
            "private",
            classify_profile_visibility(
                "Profile posts are visible because this window already follows it",
                has_visible_posts=True,
                structured_is_private=True,
            ),
        )
        self.assertEqual(
            "public",
            classify_profile_visibility(
                "Profile header rendered before the post grid",
                has_visible_posts=False,
                structured_is_private=False,
            ),
        )
        self.assertEqual(
            "unknown",
            classify_profile_visibility(
                "Biography: this is my private account for family photos",
                has_visible_posts=False,
            ),
        )
        self.assertEqual(
            "private",
            classify_profile_visibility(
                "Stable profile header",
                has_visible_posts=False,
                has_stable_private_structure=True,
            ),
        )
        self.assertEqual(
            "public",
            classify_profile_visibility(
                "Stable profile header",
                has_visible_posts=False,
                structured_is_private=False,
                has_stable_private_structure=True,
            ),
        )

    def test_embedded_privacy_matches_the_exact_target_and_ignores_suggestions(self) -> None:
        script = """
            window.__profile = {
              "data": {"user": {"username": "Target.Name", "is_private": true}},
              "suggested": [{"username": "other_user", "is_private": false}]
            };
        """
        self.assertIs(
            True,
            extract_embedded_profile_privacy((script,), "target.name"),
        )
        self.assertIsNone(
            extract_embedded_profile_privacy(
                ({"user": {"username": "other_user", "is_private": True}},),
                "target.name",
            )
        )

    def test_embedded_privacy_supports_graphql_shape_and_stays_unknown_on_conflict(self) -> None:
        graphql_payload = {
            "data": {
                "xdt_api__v1__users__web_profile_info": {
                    "data": {"user": {"username": "public_target", "is_private": False}}
                }
            }
        }
        self.assertIs(
            False,
            extract_embedded_profile_privacy((graphql_payload,), "PUBLIC_TARGET"),
        )

    def test_embedded_profile_evidence_reads_exact_target_media_time_only(self) -> None:
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        target_time = int(datetime(2026, 8, 25, 8, 30, tzinfo=timezone.utc).timestamp())
        payload = {
            "data": {
                "user": {
                    "username": "zero_post_private",
                    "is_private": True,
                    "media_count": 19,
                    "edge_owner_to_timeline_media": {
                        "edges": [{"node": {"shortcode": "ABC", "taken_at_timestamp": target_time}}]
                    },
                    "comments": [{"created_at": int(now.timestamp())}],
                },
                "suggested": [{
                    "username": "other_user",
                    "is_private": False,
                    "media_count": 9_999,
                    "edge_owner_to_timeline_media": {
                        "edges": [{"node": {"shortcode": "NEW", "taken_at_timestamp": int(now.timestamp())}}]
                    },
                }],
            }
        }
        evidence = extract_embedded_profile_evidence(
            (payload,), "ZERO_POST_PRIVATE", now=now
        )
        self.assertIs(True, evidence.is_private)
        self.assertEqual(19, evidence.posts_count)
        self.assertEqual(
            (datetime(2026, 8, 25, 8, 30, tzinfo=timezone.utc).isoformat(),),
            evidence.post_datetimes,
        )
        self.assertIsNone(
            extract_embedded_profile_privacy(
                (
                    {"user": {"username": "conflicted", "is_private": True}},
                    {"user": {"username": "conflicted", "is_private": False}},
                ),
                "conflicted",
            )
        )

    def test_embedded_posts_count_is_exact_target_only_and_conflicts_fail_closed(self) -> None:
        nested = extract_embedded_profile_evidence(
            ({
                "data": {
                    "user": {
                        "username": "target_private",
                        "is_private": True,
                        "edge_owner_to_timeline_media": {"count": "0"},
                    },
                    "suggested": [{
                        "username": "suggested_target",
                        "media_count": 9999,
                    }],
                }
            },),
            "target_private",
        )
        self.assertEqual(0, nested.posts_count)

        conflict = extract_embedded_profile_evidence(
            (
                {"user": {"username": "changing_target", "media_count": 19}},
                {"user": {"username": "changing_target", "media_count": 20}},
            ),
            "changing_target",
        )
        self.assertIsNone(conflict.posts_count)

    def test_embedded_profile_metadata_is_exact_target_only(self) -> None:
        evidence = extract_embedded_profile_evidence(
            ({
                "data": {
                    "user": {
                        "username": "target_business",
                        "is_private": False,
                        "is_verified": True,
                        "category_name": "Real Estate Agent",
                        "bio_links": [{
                            "url": "https://l.instagram.com/?u=https%3A%2F%2Fexample.com%2Flisting"
                        }],
                    },
                    "suggested": [{
                        "username": "suggested_business",
                        "is_verified": False,
                        "category_name": "Personal blog",
                        "external_url": "https://suggested.example",
                    }],
                }
            },),
            "TARGET_BUSINESS",
        )
        self.assertIs(True, evidence.is_verified)
        self.assertIs(True, evidence.is_professional_account)
        self.assertEqual("Real Estate Agent", evidence.account_category)
        self.assertEqual("https://example.com/listing", evidence.external_bio_url)

        ordinary = extract_embedded_profile_evidence(
            ({
                "user": {
                    "username": "ordinary_target",
                    "is_verified": False,
                    "is_professional_account": False,
                    "biography": "Realtor helping families find a home/house",
                    "bio_links": [],
                }
            },),
            "ordinary_target",
        )
        self.assertIs(False, ordinary.is_verified)
        self.assertIs(False, ordinary.is_professional_account)
        self.assertIsNone(ordinary.account_category)
        self.assertIsNone(ordinary.external_bio_url)
        self.assertFalse(is_real_estate_account_category(ordinary.account_category))

    def test_embedded_threads_association_is_not_an_external_bio_link(self) -> None:
        threads_only = extract_embedded_profile_evidence(
            ({
                "user": {
                    "username": "sample_threads01",
                    "bio_links": [
                        {"url": "https://www.threads.net/@sample_threads01"},
                        {
                            "lynx_url": (
                                "https://l.instagram.com/?u=https%3A%2F%2F"
                                "www.threads.net%2F%40sample_threads01"
                            )
                        },
                        {"href": "https://www.instagram.com/sample_threads01/"},
                    ],
                }
            },),
            "sample_threads01",
        )
        self.assertIsNone(threads_only.external_bio_url)

        threads_and_website = extract_embedded_profile_evidence(
            ({
                "user": {
                    "username": "target_with_website",
                    "bio_links": [
                        {"url": "https://threads.net/@target_with_website"},
                        {
                            "url": (
                                "https://l.instagram.com/?u=https%3A%2F%2F"
                                "example.com%2Freal-profile-link"
                            )
                        },
                    ],
                }
            },),
            "target_with_website",
        )
        self.assertEqual(
            "https://example.com/real-profile-link",
            threads_and_website.external_bio_url,
        )

    def test_private_profile_stats_list_is_scoped_to_the_exact_username(self) -> None:
        class StatsNode:
            async def is_visible(self) -> bool:
                return True

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "19 posts\n128 followers\n251 following"

        class StatsLocator:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class StatsPage:
            url = "https://www.instagram.com/target_private/"

            def locator(self, selector: str) -> StatsLocator:
                self.selector = selector
                return StatsLocator([StatsNode()])

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = StatsPage()
        stats_text = asyncio.run(
            worker._visible_profile_stats_text("target_private")
        )

        self.assertIn('/target_private/followers/', worker.page.selector)
        self.assertIn('/target_private/following/', worker.page.selector)
        self.assertEqual((128, 251, 19), extract_visible_metrics(stats_text))

    def test_collection_surface_requires_parsed_rows_or_explicit_empty_evidence(self) -> None:
        self.assertEqual(
            "unrecognized",
            classify_visible_collection_surface("Followers", parsed_accounts=0),
        )
        self.assertEqual(
            "populated",
            classify_visible_collection_surface("Followers", parsed_accounts=3),
        )
        self.assertEqual(
            "empty",
            classify_visible_collection_surface("No followers yet", parsed_accounts=0),
        )
        self.assertEqual(
            "empty",
            classify_visible_collection_surface("0 likes", parsed_accounts=0),
        )
        self.assertFalse(has_visible_zero_likes("0 followers · 25 likes"))
        self.assertTrue(has_visible_zero_likes("0 likes"))
        self.assertTrue(has_visible_zero_likes("暂无点赞"))

    def test_post_liker_trigger_accepts_count_controls_but_not_the_like_action(self) -> None:
        self.assertTrue(
            is_post_likers_trigger_candidate(href="/p/ABC123/liked_by/")
        )
        self.assertTrue(is_post_likers_trigger_candidate(text="View all 1,248 likes"))
        self.assertTrue(
            is_post_likers_trigger_candidate(text="Liked by account_name and 42 others")
        )
        self.assertTrue(is_post_likers_trigger_candidate(text="查看全部 326 个赞"))
        self.assertFalse(is_post_likers_trigger_candidate(aria_label="Like"))
        self.assertFalse(is_post_likers_trigger_candidate(aria_label="取消点赞"))

    def test_liker_trigger_supports_other_count_experiments(self) -> None:
        self.assertTrue(is_post_likers_trigger_candidate(text="1,248 others"))
        self.assertTrue(is_post_likers_trigger_candidate(text="其他 326 人"))
        self.assertTrue(is_post_likers_trigger_candidate(text="另有 326 位用户"))
        self.assertTrue(is_post_likers_trigger_candidate(text="326 people like this"))
        self.assertTrue(is_post_likers_trigger_candidate(text="Liked by account_name"))

    def test_location_scans_known_countries_and_keeps_other_text_unknown(self) -> None:
        self.assertEqual("美国", translate_location_to_zh("Account based in United States"))
        self.assertEqual("菲律宾", translate_location_to_zh("来自 Philippines · Manila"))
        self.assertEqual("南非", translate_location_to_zh("South Africa"))
        self.assertEqual("老挝", translate_location_to_zh("Laos"))
        self.assertEqual(
            "老挝",
            extract_about_account_location("账户简介\n加入日期\n2015年6月\n账户所在地\n老挝"),
        )
        self.assertEqual(
            "不丹",
            extract_about_account_location("账户简介\n账户所在地\n不丹"),
        )
        self.assertIsNone(translate_location_to_zh("Account based in Somewhere"))
        self.assertEqual(
            "美国",
            extract_about_account_location("账户简介\n加入日期\n2014年5月\n账户所在地\n美国"),
        )
        self.assertEqual(
            "菲律宾",
            extract_about_account_location("About this account\nAccount based in: Philippines"),
        )
        self.assertEqual(
            "美国",
            extract_about_account_location(
                "เกี่ยวกับบัญชีนี้\nตำแหน่งที่ตั้งของบัญชี\nสหรัฐอเมริกา"
            ),
        )
        self.assertEqual(
            "Côte d’Ivoire",
            extract_about_account_location(
                "À propos de ce compte\nCompte basé à\nCôte d’Ivoire"
            ),
        )
        self.assertEqual(
            "美国",
            extract_about_account_location("账户简介\n加入日期 2026年3月\n账户所在地美国\n曾用账号 2"),
        )
        self.assertIsNone(
            extract_about_account_location(
                "About this account\nInstagram protects users in the United States\nJoined May 2014"
            )
        )

    def test_visible_profile_links_accept_relative_and_instagram_absolute_urls_only(self) -> None:
        self.assertEqual("valid.user", extract_instagram_profile_username("/Valid.User/?hl=en"))
        self.assertEqual(
            "another_user",
            extract_instagram_profile_username("https://www.instagram.com/Another_User/"),
        )
        self.assertIsNone(extract_instagram_profile_username("https://example.com/not_instagram/"))
        self.assertIsNone(extract_instagram_profile_username("/p/ABC123/"))
        self.assertEqual(
            "https://www.instagram.com/p/ABC123/liked_by/",
            build_instagram_post_likers_url("https://www.instagram.com/p/ABC123/?utm_source=test"),
        )
        self.assertEqual(
            "https://www.instagram.com/reel/XYZ/liked_by/",
            build_instagram_post_likers_url("/reel/XYZ/"),
        )
        self.assertIsNone(build_instagram_post_likers_url("https://example.com/p/ABC/"))

    def test_real_estate_categories_match_only_dedicated_exact_labels(self) -> None:
        for category in (
            "房地产",
            "房地產經紀人",
            "Real Estate Agent",
            " REALTOR® ",
            "Real Estate Broker",
            "Real Estate Company",
            "Real Estate Service",
            "Commercial Real Estate Agency",
        ):
            with self.subTest(category=category):
                self.assertTrue(is_real_estate_account_category(category))
        for category in (
            None,
            "Personal blog",
            "Digital creator",
            "Media Production Company",
            "Realtor helping families find a home",
            "Stock Broker",
        ):
            with self.subTest(category=category):
                self.assertFalse(is_real_estate_account_category(category))

    def test_external_profile_link_recognizes_only_off_instagram_http_links(self) -> None:
        self.assertEqual(
            "https://example.invalid",
            external_profile_link_url("https://example.invalid"),
        )
        self.assertEqual(
            "https://example.com/offer",
            external_profile_link_url(
                "https://l.instagram.com/?u=https%3A%2F%2Fexample.com%2Foffer"
            ),
        )
        self.assertIsNone(external_profile_link_url("/target_user/"))
        self.assertIsNone(
            external_profile_link_url("https://www.instagram.com/target_user/")
        )
        self.assertIsNone(
            external_profile_link_url(
                "https://l.instagram.com/?u=https%3A%2F%2Finstagram.com%2Ftarget_user%2F"
            )
        )
        self.assertEqual(
            "https://instagram.com.evil.example/offer",
            external_profile_link_url("https://instagram.com.evil.example/offer"),
        )
        for threads_association in (
            "https://threads.net/@sample_threads01",
            "https://www.threads.net/@sample_threads01",
            "https://www.threads.com/@sample_threads01",
            "//www.threads.net/@sample_threads01",
            "https://l.threads.net/?u=https%3A%2F%2Fthreads.net%2F%40sample_threads01",
            (
                "https://l.instagram.com/?u=https%3A%2F%2F"
                "www.threads.net%2F%40sample_threads01"
            ),
        ):
            with self.subTest(threads_association=threads_association):
                self.assertIsNone(external_profile_link_url(threads_association))
        self.assertEqual(
            "https://www.threads.net/@sample_threads01/post/C8-example",
            external_profile_link_url(
                "https://www.threads.net/@sample_threads01/post/C8-example"
            ),
        )
        self.assertEqual(
            "https://example.com/from-threads-shim",
            external_profile_link_url(
                "https://l.threads.net/?u=https%3A%2F%2F"
                "example.com%2Ffrom-threads-shim"
            ),
        )
        self.assertEqual(
            "https://threads.net.evil.example/offer",
            external_profile_link_url("https://threads.net.evil.example/offer"),
        )
        self.assertIsNone(external_profile_link_url("mailto:sales@example.com"))

    def test_activity_uses_original_post_time_and_rejects_future_or_invalid_values(self) -> None:
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        selected = select_original_post_datetime(
            (
                "2026-08-24T10:00:00Z",  # original post
                "2026-08-25T09:00:00Z",  # newer comment
                "not-a-date",
                "2026-08-26T10:00:00Z",  # invalid future timestamp
            ),
            now=now,
        )
        self.assertEqual(datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc), selected)
        self.assertIsNone(
            select_original_post_datetime(
                ("not-a-date", "2026-08-26T10:00:00Z"),
                now=now,
            )
        )


class PlaywrightCollectionEvidenceTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_relation_count_trigger_is_exactly_scoped_to_current_profile(self) -> None:
        class ExactLink:
            @property
            def first(self) -> "ExactLink":
                return self

            async def count(self) -> int:
                return 1

            def nth(self, index: int) -> "ExactLink":
                self.assert_index = index
                return self

            async def is_visible(self) -> bool:
                return True

            async def get_attribute(self, name: str) -> str | None:
                if name == "href":
                    return "/sample_relation01/following/?__variant=desktop"
                return None

        class Page:
            def __init__(self) -> None:
                self.selector = ""

            def locator(self, selector: str) -> ExactLink:
                self.selector = selector
                return ExactLink()

        worker = PlaywrightWorker(FakeBitBrowserClient())
        page = Page()
        worker.page = page
        trigger = await worker._find_relation_trigger("sample_relation01", "following")

        self.assertIsNotNone(trigger)
        self.assertEqual("a[href]", page.selector)

    def test_exact_relation_href_allows_parameters_but_rejects_other_profiles(self) -> None:
        self.assertTrue(is_exact_profile_relation_href(
            "/sample_relation01/following/?next=abc", "sample_relation01", "following"
        ))
        self.assertTrue(is_exact_profile_relation_href(
            "https://www.instagram.com/sample_relation01/following?x=1",
            "sample_relation01", "following",
        ))
        self.assertFalse(is_exact_profile_relation_href(
            "/another/following/", "sample_relation01", "following"
        ))
        self.assertFalse(is_exact_profile_relation_href(
            "https://evil.example/sample_relation01/following/",
            "sample_relation01", "following",
        ))

    async def test_plain_text_profile_stat_supplies_count_and_click_target(self) -> None:
        class EmptyLinks:
            async def count(self) -> int:
                return 0

        class TextTrigger:
            def filter(self, **kwargs: object) -> "TextTrigger":
                self.filter_args = kwargs
                return self

            async def count(self) -> int:
                return 1

            def nth(self, index: int) -> "TextTrigger":
                self.index = index
                return self

            async def is_visible(self) -> bool:
                return True

        class Header:
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "120帖子 80粉丝 160关注"

            def locator(self, selector: str) -> TextTrigger:
                self.selector = selector
                return TextTrigger()

        class Page:
            def locator(self, selector: str) -> EmptyLinks:
                self.selector = selector
                return EmptyLinks()

        class PlainTextWorker(PlaywrightWorker):
            async def _visible_profile_header(self) -> Header:
                return Header()

            async def _visible_profile_stats_text(self, username_norm: str) -> str:
                del username_norm
                return ""

        worker = PlainTextWorker(FakeBitBrowserClient())
        worker.page = Page()
        visible = await worker._visible_relation_count("sample_relation01", "following")
        trigger = await worker._find_relation_trigger("sample_relation01", "following")

        self.assertEqual(160, visible.count)
        self.assertIsNotNone(trigger)

    async def test_strict_following_snapshot_stops_before_recommendations(self) -> None:
        class ResetSurface:
            async def evaluate(self, script: str) -> dict[str, object]:
                self.script = script
                return {"valid": True, "top": 0}

        class StrictWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = type("Page", (), {})()
                self.received_limit = None
                self.received_minimum = None

            async def _navigate_profile(self, username: str) -> str:
                self.page.url = f"https://www.instagram.com/{username}/"
                return username

            async def _visible_relation_count(self, username_norm: str, relation: str) -> int:
                del username_norm, relation
                return 8

            async def _open_relation_surface(self, username_norm: str, relation: str) -> object:
                del username_norm, relation
                return ResetSurface()

            async def _read_visible_account_dialog(self, dialog: object, limit: int | None, **kwargs: object) -> list[str]:
                del dialog
                self.received_limit = limit
                self.received_minimum = kwargs.get("expected_minimum")
                return [f"account_{index}" for index in range(8)]

            async def _guard(self) -> None:
                return None

        worker = StrictWorker()
        outcome = await worker.collect_following(
            "owner_one", limit=None, strict_source_total=True
        )

        self.assertEqual(8, worker.received_limit)
        self.assertEqual(8, worker.received_minimum)
        self.assertEqual(8, len(outcome.usernames))
        self.assertEqual(8, outcome.source_total)

    async def test_visible_threads_badge_is_ignored_but_real_bio_link_is_kept(self) -> None:
        class LinkLocator:
            def __init__(self, hrefs: list[str]) -> None:
                self.hrefs = hrefs

            async def evaluate_all(self, expression: str) -> list[str]:
                self.expression = expression
                return list(self.hrefs)

        class Header:
            def __init__(self, hrefs: list[str]) -> None:
                self.links = LinkLocator(hrefs)

            def locator(self, selector: str) -> LinkLocator:
                self.selector = selector
                return self.links

        class ProfilePage:
            url = "https://www.instagram.com/sample_threads01/"

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = ProfilePage()
        threads_only = Header(
            [
                "https://www.threads.net/@sample_threads01",
                (
                    "https://l.instagram.com/?u=https%3A%2F%2F"
                    "threads.net%2F%40sample_threads01"
                ),
                "https://www.instagram.com/sample_threads01/",
            ]
        )
        self.assertIsNone(
            await worker._visible_external_bio_url(threads_only, "sample_threads01")
        )
        self.assertEqual("a[href]", threads_only.selector)
        self.assertIn("nodes.slice(0, 40)", threads_only.links.expression)

        real_website = Header(
            [
                "https://l.threads.net/?u=https%3A%2F%2Fthreads.net%2F%40target",
                (
                    "https://l.threads.net/?u=https%3A%2F%2F"
                    "example.com%2Freal-profile-link"
                ),
            ]
        )
        worker.page.url = "https://www.instagram.com/target/"
        self.assertEqual(
            "https://example.com/real-profile-link",
            await worker._visible_external_bio_url(real_website, "target"),
        )

    async def test_visible_external_link_does_not_read_a_different_profile_header(self) -> None:
        class DifferentProfilePage:
            url = "https://www.instagram.com/suggested_account/"

        class UnreadableHeader:
            def locator(self, selector: str) -> None:
                del selector
                raise AssertionError("a non-target profile header must not be inspected")

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = DifferentProfilePage()

        self.assertIsNone(
            await worker._visible_external_bio_url(
                UnreadableHeader(),
                "sample_threads01",
            )
        )

    async def test_complete_profile_metrics_ignore_unrelated_page_loader(self) -> None:
        class ReadyMetricsWorker(PlaywrightWorker):
            async def _profile_transport_failure(
                self, username_norm: str, *, body_text: str | None = None
            ) -> str:
                del username_norm, body_text
                return "instagram_profile_not_ready"

        worker = ReadyMetricsWorker(FakeBitBrowserClient())
        worker.page = type(
            "ReadyProfilePage",
            (),
            {"url": "https://www.instagram.com/s.ample_metric01/"},
        )()
        self.assertIsNone(
            await worker._profile_surface_is_transient(
                "s.ample_metric01",
                body_text="s.ample_metric01\n24 posts 1234 followers 567 following",
                metrics=(1234, 567, 24),
            )
        )
        self.assertEqual(
            "instagram_profile_not_ready",
            await worker._profile_surface_is_transient(
                "s.ample_metric01",
                body_text="s.ample_metric01\n1234 followers",
                metrics=(1234, None, None),
            ),
        )

    async def test_complete_profile_metrics_ignore_soft_post_area_failure_only(self) -> None:
        class SurfacePage:
            url = "https://www.instagram.com/s.ample_metric01/"
            notices: list[str] = []

            def locator(self, selector: str) -> "SurfacePage":
                return self

            async def evaluate(self, expression: str) -> list[str]:
                return self.notices

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = SurfacePage()
        self.assertIsNone(
            await worker._profile_surface_is_transient(
                "s.ample_metric01",
                body_text=(
                    "s.ample_metric01\n24 posts 1234 followers 567 following\n"
                    "Couldn't load posts · Reload page"
                ),
                metrics=(1234, 567, 24),
            )
        )
        # Real errors expose a system heading/alert; arbitrary profile prose is
        # intentionally not transport evidence since r18.
        worker.page.notices = ["This site can't be reached ERR_INTERNET_DISCONNECTED"]
        self.assertEqual(
            "instagram_network_unavailable",
            await worker._profile_surface_is_transient(
                "s.ample_metric01",
                body_text=(
                    "s.ample_metric01\n24 posts 1234 followers 567 following\n"
                    "This site can't be reached ERR_INTERNET_DISCONNECTED"
                ),
                metrics=(1234, 567, 24),
            ),
        )

        worker.page.url = "https://www.instagram.com/another_account/"
        worker.page.notices = []
        self.assertEqual(
            "instagram_content_not_visible",
            await worker._profile_surface_is_transient(
                "s.ample_metric01",
                body_text="s.ample_metric01\n24 posts 1234 followers 567 following",
                metrics=(1234, 567, 24),
            ),
        )

    async def test_current_chinese_zero_post_profile_finishes_in_one_read(self) -> None:
        synthetic_profile_text = (
            "sample_empty01\nSample Empty\n0帖子 23粉丝 8关注\n关注\n这里空荡荡~"
        )

        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

            async def get_attribute(self, name: str) -> None:
                del name
                return None

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return ""

            def locator(self, selector: str) -> "EmptyLocator":
                del selector
                return self

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs
                raise AssertionError("a terminal zero-post profile must not wait or refresh")

        class HeaderLocator(EmptyLocator):
            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "sample_empty01\nSample Empty"

            def locator(self, selector: str) -> EmptyLocator:
                del selector
                return EmptyLocator()

        class BodyLocator(EmptyLocator):
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return synthetic_profile_text

        class ZeroPostPage:
            url = "https://www.instagram.com/sample_empty01/"

            def locator(self, selector: str) -> EmptyLocator:
                if selector == "body":
                    return BodyLocator()
                return EmptyLocator()

        class ZeroPostWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = ZeroPostPage()
                self.reads = 0
                self.story_checks = 0

            async def _navigate_profile_with_privacy(
                self, target: str
            ) -> tuple[str, None]:
                self.reads += 1
                return target, None

            async def _visible_profile_header(self) -> HeaderLocator:
                return HeaderLocator()

            async def _has_visible_private_indicator(self) -> bool:
                return False

            async def _has_visible_loading_indicator(self) -> bool:
                return True

            async def _has_visible_target_story(self, username_norm: str) -> bool:
                del username_norm
                self.story_checks += 1
                return False

            async def _visible_profile_stats_text(self, username_norm: str) -> str:
                del username_norm
                raise AssertionError("body-level terminal metrics should be sufficient")

        worker = ZeroPostWorker()
        profile = await worker.read_visible_profile(
            "sample_empty01", include_activity=True
        )
        cached_profile = await worker.read_visible_profile(
            "sample_empty01", include_activity=True
        )

        self.assertEqual(1, worker.reads)
        self.assertEqual(1, worker.story_checks)
        self.assertEqual("public", profile.visibility)
        self.assertEqual((23, 8, 0), (profile.followers, profile.following, profile.posts))
        self.assertEqual("no_posts", profile.activity_status)
        self.assertIsNone(profile.account_category)
        self.assertIsNone(profile.external_bio_url)
        self.assertEqual("no_posts", cached_profile.activity_status)

    async def test_current_chinese_private_zero_post_profile_stays_private(self) -> None:
        synthetic_profile_text = (
            "sample_private01_\nSample Private\n0帖子 246粉丝 864关注\n"
            "这是私密主页\n关注即可查看其照片和视频。"
        )

        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

            async def get_attribute(self, name: str) -> None:
                del name
                return None

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return ""

            def locator(self, selector: str) -> "EmptyLocator":
                del selector
                return self

        class HeaderLocator(EmptyLocator):
            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "sample_private01_\nSample Private\n0帖子 246粉丝 864关注"

            def locator(self, selector: str) -> EmptyLocator:
                del selector
                return EmptyLocator()

        class BodyLocator(EmptyLocator):
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return synthetic_profile_text

        class PrivateZeroPostPage:
            url = "https://www.instagram.com/sample_private01_/"

            def locator(self, selector: str) -> EmptyLocator:
                if selector == "body":
                    return BodyLocator()
                return EmptyLocator()

        class PrivateZeroPostWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = PrivateZeroPostPage()
                self.reads = 0

            async def _navigate_profile_with_privacy(
                self, target: str
            ) -> tuple[str, None]:
                self.reads += 1
                return target, None

            async def _visible_profile_header(self) -> HeaderLocator:
                return HeaderLocator()

            async def _has_visible_private_indicator(self) -> bool:
                # This regression intentionally proves the rendered Chinese copy is
                # sufficient even when no private icon/aria selector is available.
                return False

            async def _has_visible_loading_indicator(self) -> bool:
                return True

            async def _visible_profile_stats_text(self, username_norm: str) -> str:
                del username_norm
                raise AssertionError("complete header counts must not need a fallback read")

        worker = PrivateZeroPostWorker()
        profile = await worker.read_visible_profile("sample_private01_")
        cached_profile = await worker.read_visible_profile("sample_private01_")

        self.assertEqual(1, worker.reads)
        self.assertEqual("private", profile.visibility)
        self.assertEqual((246, 864, 0), (profile.followers, profile.following, profile.posts))
        self.assertEqual("private", cached_profile.visibility)
        self.assertIsNone(profile.external_bio_url)
        self.assertEqual("not_checked", cached_profile.activity_status)

    async def test_inline_profile_evidence_batches_script_reads(self) -> None:
        class ScriptLocator:
            def __init__(self) -> None:
                self.evaluate_calls = 0

            async def evaluate_all(self, expression: str, username: str) -> list[str]:
                self.evaluate_calls += 1
                self.expression = expression
                self.username = username
                return [
                    '{"user":{"username":"target_private","is_private":true,"media_count":19}}'
                ]

            async def count(self) -> int:
                raise AssertionError("batched script read must not fall back to per-node reads")

        class ScriptPage:
            def __init__(self) -> None:
                self.scripts = ScriptLocator()

            def locator(self, selector: str) -> ScriptLocator:
                self.selector = selector
                return self.scripts

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = ScriptPage()

        evidence = await worker._read_inline_profile_evidence("target_private")

        self.assertEqual("script:not([src])", worker.page.selector)
        self.assertEqual(1, worker.page.scripts.evaluate_calls)
        self.assertEqual("target_private", worker.page.scripts.username)
        self.assertIn("nodes.slice(0, 150)", worker.page.scripts.expression)
        self.assertIs(True, evidence.is_private)
        self.assertEqual(19, evidence.posts_count)

    async def test_connect_does_not_block_event_loop_during_slow_endpoint_lookup(self) -> None:
        import threading
        import types
        from unittest.mock import patch

        endpoint_entered = threading.Event()
        release_endpoint = threading.Event()
        ticker_ran = threading.Event()
        ticker_state_at_return: list[bool] = []

        class SlowEndpointClient:
            def __init__(self) -> None:
                self.call: tuple[str, bool] | None = None

            def connection_endpoint(
                self,
                profile_id: str,
                *,
                open_if_needed: bool = True,
            ) -> dict:
                self.call = (profile_id, open_if_needed)
                endpoint_entered.set()
                if not release_endpoint.wait(1.0):
                    raise AssertionError("The event loop did not release the endpoint lookup")
                ticker_state_at_return.append(ticker_ran.is_set())
                return {}

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: None
        fake_package.async_api = fake_async_api

        client = SlowEndpointClient()
        worker = PlaywrightWorker(client)

        async def ticker() -> None:
            ready = await asyncio.to_thread(endpoint_entered.wait, 1.0)
            self.assertTrue(ready)
            await asyncio.sleep(0)
            ticker_ran.set()
            release_endpoint.set()

        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            ticker_task = asyncio.create_task(ticker())
            try:
                with self.assertRaises(UpstreamUnavailableError) as raised:
                    await asyncio.wait_for(
                        worker.connect("slow-window", open_if_needed=False),
                        timeout=2.0,
                    )
            finally:
                release_endpoint.set()
                await ticker_task

        self.assertEqual(("slow-window", False), client.call)
        self.assertEqual([True], ticker_state_at_return)
        self.assertEqual(
            "BitBrowser did not provide a CDP endpoint",
            str(raised.exception),
        )
        self.assertEqual("slow-window", raised.exception.details["profile_id"])

    async def test_connect_preserves_endpoint_lookup_exception(self) -> None:
        import types
        from unittest.mock import patch

        original = UpstreamUnavailableError(
            "BitBrowser endpoint lookup failed",
            details={"profile_id": "broken-window", "pause_required": True},
        )

        class FailingEndpointClient:
            @staticmethod
            def connection_endpoint(
                profile_id: str,
                *,
                open_if_needed: bool = True,
            ) -> dict:
                del profile_id, open_if_needed
                raise original

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: None
        fake_package.async_api = fake_async_api

        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            with self.assertRaises(UpstreamUnavailableError) as raised:
                await PlaywrightWorker(FailingEndpointClient()).connect("broken-window")

        self.assertIs(original, raised.exception)

    async def test_connect_validates_cdp_without_guarding_an_old_login_tab(self) -> None:
        import types
        from unittest.mock import patch

        class EndpointClient:
            @staticmethod
            def connection_endpoint(
                profile_id: str,
                *,
                open_if_needed: bool = True,
            ) -> dict:
                del profile_id, open_if_needed
                return {"ws": "ws://127.0.0.1/devtools/browser/test"}

        class OldLoginPage:
            url = "https://www.instagram.com/accounts/login/"

            @staticmethod
            def is_closed() -> bool:
                return False

            async def bring_to_front(self) -> None:
                raise AssertionError("background connection must not restore the window")

            def locator(self, selector: str) -> object:
                del selector
                raise AssertionError("connect() must not inspect an old tab's business DOM")

        old_page = OldLoginPage()

        class FakeContext:
            pages = [old_page]

            async def new_page(self) -> object:
                raise AssertionError("The reusable open page should be selected")

        class FakeBrowser:
            contexts = [FakeContext()]

        class FakeChromium:
            async def connect_over_cdp(self, endpoint: str, timeout: int) -> FakeBrowser:
                self.call = (endpoint, timeout)
                return FakeBrowser()

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        driver = FakePlaywright()

        class FakeStarter:
            async def start(self) -> FakePlaywright:
                return driver

        fake_package = types.ModuleType("playwright")
        fake_package.__path__ = []
        fake_async_api = types.ModuleType("playwright.async_api")
        fake_async_api.async_playwright = lambda: FakeStarter()
        fake_package.async_api = fake_async_api

        worker = PlaywrightWorker(EndpointClient())
        with patch.dict(
            sys.modules,
            {"playwright": fake_package, "playwright.async_api": fake_async_api},
        ):
            await worker.connect("login-window", open_if_needed=False)

        self.assertIs(old_page, worker.page)
        self.assertEqual("login-window", worker.profile_id)
        self.assertEqual(
            ("ws://127.0.0.1/devtools/browser/test", 30_000),
            driver.chromium.call,
        )
        await worker.disconnect()
        self.assertTrue(driver.stopped)

    async def test_minimized_window_remains_valid_without_foreground_or_viewport_checks(self) -> None:
        class MinimizedPage:
            async def bring_to_front(self) -> None:
                raise AssertionError("minimized collection must not steal foreground")

            async def evaluate(self, expression: str) -> object:
                del expression
                raise AssertionError("minimized viewport must not block CDP collection")

        class Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            async def send(self, method: str, params: dict) -> None:
                self.calls.append((method, params))

        worker = PlaywrightWorker(object())
        worker.page = MinimizedPage()
        worker._cdp_session = Session()

        await worker._ensure_window_surface_stable()

        self.assertEqual(
            [
                ("Page.setWebLifecycleState", {"state": "active"}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ],
            worker._cdp_session.calls,
        )

    def test_location_load_failure_surface_is_not_an_unknown_country(self) -> None:
        for text in (
            "Failed to load\n关闭",
            "Unable to load\nClose",
            "加载失败\n关闭",
            "無法載入\n關閉",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_about_account_load_failure(text))
        self.assertFalse(
            is_about_account_load_failure("账户简介\n账户所在地\n美国\n关闭")
        )

    async def test_visible_failed_to_load_dialog_raises_location_transport_error(self) -> None:
        class Locator:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class Trigger:
            async def click(self, timeout: int = 0) -> None:
                del timeout

        class FailureDialog:
            async def is_visible(self) -> bool:
                return True

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "Failed to load\n关闭"

        class Page:
            url = "https://www.instagram.com/location_failure/"

            def locator(self, selector: str) -> Locator:
                if selector == 'div[role="dialog"]':
                    return Locator([FailureDialog()])
                return Locator([])

        class FailureDialogWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = Page()
                self._profile_user_id_cache["location_failure"] = "123"

            async def _navigate_profile(self, username: str) -> str:
                return username

            async def _find_exact_profile_username_trigger(
                self, username_norm: str
            ) -> Trigger:
                del username_norm
                return Trigger()

            async def _dismiss_profile_information_surfaces(self) -> None:
                return None

        worker = FailureDialogWorker()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_location_once(
                "location_failure", force_reload=True
            )
        self.assertEqual("instagram_location_load_failed", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])

    async def test_location_load_failure_tries_fresh_page_without_reloading_then_succeeds(self) -> None:
        class LocationLoadRetryWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.calls: list[bool] = []
                self.location_request_min_interval_seconds = 0
                self.location_request_max_interval_seconds = 0
                self.location_load_failure_backoff_seconds = (0,)

            async def _recover_stalled_profile_page(self, target):
                self.recovered_target = target
                return True

            async def _read_visible_account_location_once(
                self, target: str, *, force_reload: bool = False
            ) -> str | None:
                del target
                self.calls.append(force_reload)
                if len(self.calls) == 1:
                    raise WorkerExecutionError(
                        "Instagram 未能加载账户所在地详情",
                        reason="instagram_location_load_failed",
                        pause_required=True,
                        status_code=503,
                    )
                return "美国"

        worker = LocationLoadRetryWorker()
        self.assertEqual(
            "美国", await worker.read_visible_account_location("retry_location")
        )
        self.assertEqual([False, False], worker.calls)
        self.assertEqual("retry_location", worker.recovered_target)

    async def test_location_load_failure_exhaustion_opens_shared_circuit(self) -> None:
        class LocationLoadFailureWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.calls = 0
                self.location_request_min_interval_seconds = 0
                self.location_request_max_interval_seconds = 0
                self.location_load_failure_backoff_seconds = (0, 0)
                self.location_failure_circuit_seconds = 60

            async def _recover_stalled_profile_page(self, target):
                self.recovered_target = target
                return True

            async def _read_visible_account_location_once(
                self, target: str, *, force_reload: bool = False
            ) -> str | None:
                del target, force_reload
                self.calls += 1
                raise WorkerExecutionError(
                    "Instagram 未能加载账户所在地详情",
                    reason="instagram_location_load_failed",
                    pause_required=True,
                    status_code=503,
                )

        worker = LocationLoadFailureWorker()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker.read_visible_account_location("blocked_location")
        self.assertEqual("instagram_page_recovery_exhausted", raised.exception.code)
        self.assertEqual("instagram_location_load_failed", raised.exception.details["original_reason"])
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertFalse(raised.exception.details["auto_retry"])
        self.assertEqual(2, worker.calls)

        with self.assertRaises(WorkerExecutionError) as circuit_open:
            await worker.read_visible_account_location("next_location")
        self.assertEqual(
            "instagram_location_temporarily_unavailable",
            circuit_open.exception.code,
        )
        self.assertGreater(circuit_open.exception.details["retry_after_seconds"], 0)
        self.assertLessEqual(circuit_open.exception.details["retry_after_seconds"], 60)
        self.assertEqual(2, worker.calls)

    async def test_parallel_location_flows_share_one_single_flight_channel(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum_active = 0
        starts: list[str] = []

        class SerializedLocationWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.location_request_min_interval_seconds = 0
                self.location_request_max_interval_seconds = 0

            async def _read_visible_account_location_once(
                self, target: str, *, force_reload: bool = False
            ) -> str | None:
                del force_reload
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                starts.append(target)
                try:
                    if target == "first_location":
                        entered.set()
                        await release.wait()
                    return "美国"
                finally:
                    active -= 1

        first = SerializedLocationWorker()
        second = SerializedLocationWorker()
        second._location_request_coordinator = first._location_request_coordinator
        first_task = asyncio.create_task(
            first.read_visible_account_location("first_location")
        )
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        second_task = asyncio.create_task(
            second.read_visible_account_location("second_location")
        )
        await asyncio.sleep(0.01)
        self.assertEqual(["first_location"], starts)
        self.assertEqual(1, maximum_active)
        release.set()
        self.assertEqual(
            ["美国", "美国"], await asyncio.gather(first_task, second_task)
        )
        self.assertEqual(1, maximum_active)

    async def test_location_clicks_exact_top_username_and_reads_only_location_row(self) -> None:
        class FakeLocator:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            @property
            def first(self) -> object:
                return self.nth(0)

            @property
            def last(self) -> object:
                return self.nth(max(0, len(self.nodes) - 1))

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

            def filter(self, *, has_text: object) -> "FakeLocator":
                return FakeLocator(
                    [node for node in self.nodes if has_text.search(getattr(node, "text", ""))]
                )

        class FakeNode:
            def __init__(
                self,
                text: str = "",
                *,
                href: str = "",
                visible: bool = True,
                click_hook: object | None = None,
            ) -> None:
                self.text = text
                self.href = href
                self.visible = visible
                self.click_hook = click_hook
                self.children: list[FakeNode] = []
                self.clicked = False

            async def is_visible(self) -> bool:
                return self.visible

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

            async def click(self, timeout: int = 0) -> None:
                del timeout
                self.clicked = True
                if self.click_hook:
                    self.click_hook()

            def locator(self, selector: str) -> FakeLocator:
                if "a[href=" in selector:
                    return FakeLocator(
                        [node for node in self.children if node.href and node.href in selector]
                    )
                if "button" in selector or "menuitem" in selector:
                    return FakeLocator(self.children)
                return FakeLocator([])

            def get_by_text(self, pattern: object, exact: bool = False) -> FakeLocator:
                del exact
                return FakeLocator(
                    [node for node in self.children if pattern.search(node.text)]
                )

        stale_dialog = FakeNode("旧的隐藏弹窗", visible=False)
        dialog = FakeNode(
            "账户简介\nInstagram 在 Canada 提供服务\n加入日期\n2020年1月\n账户所在地\n德国",
            visible=False,
        )
        about_menu = FakeNode("About this account", visible=False)
        about_entry = FakeNode(
            "About this account",
            click_hook=lambda: (
                setattr(about_menu, "visible", False),
                setattr(dialog, "visible", True),
            ),
        )
        about_menu.children = [about_entry]
        target = FakeNode(
            "sample_location01",
            href="/sample_location01/",
            click_hook=lambda: setattr(about_menu, "visible", True),
        )
        recommendation = FakeNode("sample_location01_fan", href="/sample_location01_fan/")
        header = FakeNode()
        header.children = [recommendation, target]

        class FakeKeyboard:
            def __init__(self) -> None:
                self.presses: list[str] = []

            async def press(self, key: str) -> None:
                self.presses.append(key)
                dialog.visible = False
                about_menu.visible = False

        class FakePage:
            url = "https://www.instagram.com/sample_location01/"
            keyboard = FakeKeyboard()

            def locator(self, selector: str) -> FakeLocator:
                if selector == "main header":
                    return FakeLocator([header])
                if selector == 'div[role="dialog"]':
                    return FakeLocator([stale_dialog, dialog])
                if selector == '[role="menu"]':
                    return FakeLocator([about_menu])
                return FakeLocator([])

            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

        class LocationDomWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = FakePage()
                self.navigations = 0

            async def _navigate_profile(self, username: str) -> str:
                self.navigations += 1
                self.page.url = f"https://www.instagram.com/{username}/"
                return username

        worker = LocationDomWorker()
        self.assertEqual("德国", await worker.read_visible_account_location("sample_location01"))
        self.assertTrue(target.clicked)
        self.assertFalse(recommendation.clicked)
        self.assertTrue(about_entry.clicked)
        self.assertFalse(dialog.visible)
        self.assertIn("Escape", worker.page.keyboard.presses)
        self.assertEqual(0, worker.navigations)

    async def test_location_reader_reenters_about_flow_before_returning_unknown(self) -> None:
        class RetryPage:
            def __init__(self) -> None:
                self.waits: list[int] = []

            async def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

        class RetryingLocationWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = RetryPage()
                self.reads = 0

            async def _read_visible_account_location_once(
                self, target: str, *, force_reload: bool = False
            ) -> str | None:
                self.asserted_target = target
                self.force_reload_values.append(force_reload)
                self.reads += 1
                return "美国" if self.reads == 2 else None

        worker = RetryingLocationWorker()
        worker.force_reload_values = []
        self.assertEqual("美国", await worker.read_visible_account_location("careful_reader"))
        self.assertEqual(2, worker.reads)
        self.assertEqual([350], worker.page.waits)
        self.assertEqual([False, False], worker.force_reload_values)

    def test_location_entry_is_strictly_the_exact_top_username(self) -> None:
        source = (BACKEND_ROOT / "app" / "playwright_worker.py").read_text(
            encoding="utf-8"
        )
        start = source.index("    async def _read_visible_account_location_once(")
        end = source.index("\n    async def _collect_relation(", start)
        method = source[start:end]
        self.assertIn("_find_exact_profile_username_trigger(username_norm)", method)
        self.assertIn("await username_trigger.click(timeout=10_000)", method)
        self.assertNotIn("_find_visible_profile_options_trigger", method)
        self.assertNotIn("tried_options_fallback", method)
        self.assertNotIn("_PROFILE_OPTIONS_LABELS", source)

    async def test_exact_visible_story_sets_today_and_skips_post_activity(self) -> None:
        class FakeLocator:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class FakeNode:
            def __init__(self, href: str = "") -> None:
                self.href = href
                self.children: list[FakeNode] = []

            async def is_visible(self) -> bool:
                return True

            def locator(self, selector: str) -> FakeLocator:
                if "/stories/" in selector:
                    if "/stories/_sample__story19/" in selector:
                        return FakeLocator([target_story])
                    if "/stories/recommended_user/" in selector:
                        return FakeLocator([recommendation_story])
                return FakeLocator([])

        target_story = FakeNode("/stories/_sample__story19/123456/")
        recommendation_story = FakeNode("/stories/recommended_user/987654/")
        header = FakeNode()
        header.children = [recommendation_story, target_story]

        class FakePage:
            url = "https://www.instagram.com/_sample__story19/"

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator([header]) if selector == "main header" else FakeLocator([])

        class StoryDomWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = FakePage()
                self.post_reads = 0

            async def _read_original_post_datetime(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                self.post_reads += 1
                raise AssertionError("A visible target Story must skip post inspection")

        worker = StoryDomWorker()
        worker.page.url = "https://www.instagram.com/not_the_target/"
        self.assertFalse(await worker._has_visible_target_story("not_the_target"))
        worker.page.url = "https://www.instagram.com/_sample__story19/"
        profile = await worker._read_profile_activity(
            "_sample__story19",
            VisibleProfile("_sample__story19", "public", 100, 50, 12),
        )
        self.assertEqual(0, profile.activity_days)
        self.assertEqual("story_today", profile.activity_status)
        self.assertEqual(0, worker.post_reads)

    async def test_activity_skips_cached_pinned_post_and_opens_newest_unpinned_post(self) -> None:
        class LatestVisiblePostWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.opened_posts: list[str] = []

                class Page:
                    url = "https://www.instagram.com/latest_only/"

                self.page = Page()

            async def _has_visible_target_story(self, username_norm: str) -> bool:
                del username_norm
                return False

            async def _visible_unpinned_post_urls(self, *, maximum: int) -> list[str]:
                self.assert_maximum = maximum
                return [
                    "https://www.instagram.com/p/NEWEST/",
                    "https://www.instagram.com/p/OLDER/",
                ]

            async def _read_original_post_datetime(
                self,
                post_url: str,
                *,
                now: datetime,
            ) -> datetime | None:
                self.opened_posts.append(post_url)
                return now - timedelta(days=2, hours=1)

        worker = LatestVisiblePostWorker()
        worker._profile_post_url_cache["latest_only"] = (
            "https://www.instagram.com/p/OLD_PINNED/",
            "https://www.instagram.com/p/OLDER/",
        )
        profile = await worker._read_profile_activity(
            "latest_only",
            VisibleProfile("latest_only", "public", 100, 50, 3),
        )

        self.assertEqual(["https://www.instagram.com/p/NEWEST/"], worker.opened_posts)
        self.assertEqual(1, worker.assert_maximum)
        self.assertEqual("identified", profile.activity_status)
        self.assertEqual(2, profile.activity_days)

    async def test_activity_shortcuts_are_explicit_and_do_not_open_hidden_posts(self) -> None:
        class CachedZeroPostWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.story_checks = 0

            async def _has_visible_target_story(self, username_norm: str) -> bool:
                del username_norm
                self.story_checks += 1
                return False

        worker = CachedZeroPostWorker()
        zero = await worker._read_profile_activity(
            "empty_public",
            VisibleProfile("empty_public", "public", 1, 1, 0),
        )
        self.assertEqual("no_posts", zero.activity_status)
        cached_zero = await worker.read_visible_profile(
            "empty_public", include_activity=True
        )
        self.assertEqual("no_posts", cached_zero.activity_status)
        self.assertEqual(1, worker.story_checks)
        private = await worker._read_profile_activity(
            "private_target",
            VisibleProfile("private_target", "private", 10, 20, 5),
        )
        self.assertEqual("private_not_visible", private.activity_status)

        now = datetime.now(timezone.utc)
        worker._profile_post_datetime_cache["active_public"] = (
            (now.replace(microsecond=0) - __import__("datetime").timedelta(hours=3)).isoformat(),
        )
        active = await worker._read_profile_activity(
            "active_public",
            VisibleProfile("active_public", "public", 10, 20, 5),
        )
        self.assertEqual("identified", active.activity_status)
        self.assertEqual(0, active.activity_days)

    async def test_profile_read_retries_transient_shell_and_never_caches_unknown_counts(self) -> None:
        class RetryPage:
            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

        class RetryWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = RetryPage()
                self.attempts = 0

            async def _recover_stalled_profile_page(self, target):
                self.recovered_target = target
                return True

            async def _read_visible_profile_once(
                self,
                target: str,
                *,
                include_activity: bool = False,
            ) -> VisibleProfile:
                del include_activity
                self.attempts += 1
                if self.attempts < 2:
                    # Simulates Instagram's empty client shell during a brief outage.
                    self._profile_base_cache[target] = VisibleProfile(
                        target, "unknown", None, None, None
                    )
                    raise WorkerExecutionError(
                        "profile shell incomplete",
                        reason="instagram_profile_not_ready",
                        pause_required=True,
                    )
                self.assert_cache_cleared = target not in self._profile_base_cache
                return VisibleProfile(target, "public", 120, 80, 9)

        worker = RetryWorker()
        profile = await worker.read_visible_profile("retry_target")
        self.assertEqual(2, worker.attempts)
        self.assertTrue(worker.assert_cache_cleared)
        self.assertEqual((120, 80, 9), (profile.followers, profile.following, profile.posts))

    def test_relation_collection_polling_allows_virtual_rows_to_render(self) -> None:
        worker = PlaywrightWorker(FakeBitBrowserClient())
        self.assertEqual(0.45, worker.collection_poll_interval_seconds)
        self.assertTrue(worker.handles_profile_read_retries)

    def test_fast_screening_wait_budgets_remain_bounded(self) -> None:
        source = (BACKEND_ROOT / "app" / "playwright_worker.py").read_text(
            encoding="utf-8"
        )
        for marker in (
            "_UNKNOWN_VISIBILITY_WAIT_MILLISECONDS = 1_400",
            "_LOCATION_FLOW_ATTEMPTS = 2",
            "_LOCATION_TRIGGER_POLL_ATTEMPTS = 8",
            "_LOCATION_TRIGGER_POLL_MILLISECONDS = 250",
            "_LOCATION_DIALOG_POLL_ATTEMPTS = 20",
            "_LOCATION_DIALOG_POLL_MILLISECONDS = 150",
            "_ACTIVITY_GRID_POLL_ATTEMPTS = 12",
            "_ACTIVITY_GRID_POLL_MILLISECONDS = 250",
        ):
            self.assertIn(marker, source)
        self.assertIn("_visible_unpinned_post_urls(maximum=1)", source)
        self.assertIn('"article time[datetime], main time[datetime]"', source)
        self.assertIn("batched_payloads = await scripts.evaluate_all(", source)
        self.assertGreaterEqual(
            source.count("if followers is None or following is None or posts is None:"),
            2,
        )

    def test_profile_classifies_visibility_before_reading_account_metrics(self) -> None:
        source = (BACKEND_ROOT / "app" / "playwright_worker.py").read_text(
            encoding="utf-8"
        )
        start = source.index("    async def _read_visible_profile_once(")
        end = source.index("\n    def get_cached_instagram_user_id", start)
        method = source[start:end]
        self.assertLess(
            method.index("classify_profile_visibility("),
            method.index("followers, following, posts = extract_visible_metrics(header_text)"),
        )
        self.assertLess(
            method.index("terminal_metrics = public_empty_metrics or private_metrics"),
            method.index("extract_visible_metrics(meta_description)"),
        )

    async def test_profile_read_exhaustion_pauses_instead_of_returning_unknown(self) -> None:
        class RetryPage:
            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

        class OfflineWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = RetryPage()
                self.attempts = 0

            async def _read_visible_profile_once(
                self,
                target: str,
                *,
                include_activity: bool = False,
            ) -> VisibleProfile:
                del target, include_activity
                self.attempts += 1
                raise WorkerExecutionError(
                    "network unavailable",
                    reason="instagram_network_unavailable",
                    pause_required=True,
                    status_code=503,
                )

        worker = OfflineWorker()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker.read_visible_profile("offline_target")
        self.assertEqual(1, worker.attempts)
        self.assertEqual("instagram_page_recovery_exhausted", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertNotIn("offline_target", worker._profile_base_cache)

    async def test_stalled_profile_closes_old_task_tab_only_after_replacement_progress(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []
                self.detached = False

            async def send(self, method: str, params: dict) -> None:
                self.calls.append((method, params))

            async def detach(self) -> None:
                self.detached = True

        class Locator:
            def __init__(self, *, text: str = "", visible: bool = False) -> None:
                self.text = text
                self.visible = visible

            @property
            def first(self) -> "Locator":
                return self

            async def wait_for(self, *, state: str, timeout: int) -> None:
                del state, timeout
                if not self.visible:
                    raise TimeoutError("surface missing")

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

            async def count(self) -> int:
                return 1 if self.visible else 0

            def nth(self, index: int) -> "Locator":
                del index
                return self

            async def is_visible(self) -> bool:
                return self.visible

        class ManualPage:
            url = "https://www.instagram.com/stale_target/"

            def __init__(self) -> None:
                self.closed = False

            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

            async def close(self) -> None:
                self.closed = True

        class RecoveryPage:
            def __init__(self) -> None:
                self.url = "about:blank"
                self.closed = False
                self.goto_calls: list[str] = []

            async def goto(self, url: str, **kwargs: object) -> None:
                del kwargs
                self.goto_calls.append(url)
                self.url = url

            def locator(self, selector: str) -> Locator:
                if selector == "body":
                    return Locator(
                        text="target_user\n12 posts 100 followers 20 following",
                        visible=True,
                    )
                if selector == "main:visible, header:visible":
                    return Locator(visible=True)
                return Locator(visible=False)

            async def close(self) -> None:
                self.closed = True

        manual_page = ManualPage()
        recovery_page = RecoveryPage()
        old_session = Session()
        recovery_session = Session()

        class Context:
            async def new_page(self) -> RecoveryPage:
                return recovery_page

            async def new_cdp_session(self, page: object) -> Session:
                self.session_page = page
                return recovery_session

        class StalledWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = manual_page
                self._context = Context()
                self._cdp_session = old_session
                self.read_attempts = 0

            async def _read_visible_profile_once(
                self,
                target: str,
                *,
                include_activity: bool = False,
                include_avatar_image: bool = False,
            ) -> VisibleProfile:
                del include_activity, include_avatar_image
                self.read_attempts += 1
                if self.page is manual_page:
                    raise WorkerExecutionError(
                        "stalled tab",
                        reason="instagram_profile_not_ready",
                        pause_required=True,
                        status_code=503,
                    )
                assert not manual_page.closed, "old tab must survive until replacement read succeeds"
                return VisibleProfile(target, "public", 100, 20, 12)

        worker = StalledWorker()
        profile = await worker.read_visible_profile("target_user")

        self.assertEqual("target_user", profile.username)
        self.assertEqual(2, worker.read_attempts)
        self.assertIs(recovery_page, worker.page)
        self.assertIs(recovery_page, worker._worker_owned_page)
        self.assertEqual(
            ["https://www.instagram.com/target_user/"],
            recovery_page.goto_calls,
        )
        self.assertTrue(manual_page.closed)
        self.assertFalse(recovery_page.closed)
        self.assertTrue(old_session.detached)
        self.assertFalse(recovery_session.detached)
        self.assertEqual(2, len(recovery_session.calls))
        await worker.disconnect()
        self.assertTrue(manual_page.closed)
        self.assertTrue(recovery_page.closed)
        self.assertTrue(recovery_session.detached)

    async def test_failed_stalled_tab_replacement_cleans_temporary_page_and_keeps_checkpoint_page(self) -> None:
        class Locator:
            @property
            def first(self) -> "Locator":
                return self

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs
                raise TimeoutError("still blank")

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return ""

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "Locator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

        class Page:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False

            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

            async def goto(self, url: str, **kwargs: object) -> None:
                del kwargs
                self.url = url

            def locator(self, selector: str) -> Locator:
                del selector
                return Locator()

            async def close(self) -> None:
                self.closed = True

        class Session:
            def __init__(self) -> None:
                self.detached = False

            async def send(self, method: str, params: dict) -> None:
                del method, params

            async def detach(self) -> None:
                self.detached = True

        original_page = Page("https://www.instagram.com/blank_target/")
        candidate_page = Page("about:blank")
        candidate_session = Session()

        class Context:
            async def new_page(self) -> Page:
                return candidate_page

            async def new_cdp_session(self, page: object) -> Session:
                del page
                return candidate_session

        class StalledWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = original_page
                self._context = Context()
                self.read_attempts = 0

            async def _read_visible_profile_once(
                self,
                target: str,
                *,
                include_activity: bool = False,
            ) -> VisibleProfile:
                del target, include_activity
                self.read_attempts += 1
                raise WorkerExecutionError(
                    "stalled tab",
                    reason="instagram_profile_not_ready",
                    pause_required=True,
                    status_code=503,
                )

        worker = StalledWorker()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker.read_visible_profile("blank_target")

        self.assertEqual("instagram_page_recovery_exhausted", raised.exception.code)
        self.assertEqual(1, worker.read_attempts)
        self.assertIs(original_page, worker.page)
        self.assertFalse(original_page.closed)
        self.assertTrue(candidate_page.closed)
        self.assertTrue(candidate_session.detached)

    async def test_recovery_tab_propagates_login_guard_after_cleaning_temporary_page(self) -> None:
        class Locator:
            def __init__(self, text: str = "", *, visible: bool = False) -> None:
                self.text = text
                self.visible = visible

            @property
            def first(self) -> "Locator":
                return self

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "Locator":
                del index
                return self

            async def is_visible(self) -> bool:
                return self.visible

        class Page:
            def __init__(self, url: str, body: str = "") -> None:
                self.url = url
                self.body = body
                self.closed = False

            async def goto(self, url: str, **kwargs: object) -> None:
                del url, kwargs
                self.url = "https://www.instagram.com/accounts/login/"
                self.body = "Log in to Instagram"

            def locator(self, selector: str) -> Locator:
                return Locator(
                    self.body if selector == "body" else "",
                    visible=selector == "main:visible, header:visible",
                )

            async def close(self) -> None:
                self.closed = True

        class Session:
            def __init__(self) -> None:
                self.detached = False

            async def send(self, method: str, params: dict) -> None:
                del method, params

            async def detach(self) -> None:
                self.detached = True

        original_page = Page("https://www.instagram.com/stale_target/")
        candidate_page = Page("about:blank")
        candidate_session = Session()

        class Context:
            async def new_page(self) -> Page:
                return candidate_page

            async def new_cdp_session(self, page: object) -> Session:
                del page
                return candidate_session

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = original_page
        worker._context = Context()

        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._recover_stalled_profile_page("target_user")

        self.assertEqual("instagram_login_required", raised.exception.code)
        self.assertIs(original_page, worker.page)
        self.assertFalse(original_page.closed)
        self.assertTrue(candidate_page.closed)
        self.assertTrue(candidate_session.detached)

    async def test_recovery_validation_accepts_private_and_zero_post_terminal_surfaces(self) -> None:
        class Locator:
            def __init__(self, text: str = "", *, visible: bool = False) -> None:
                self.text = text
                self.visible = visible

            @property
            def first(self) -> "Locator":
                return self

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs
                raise TimeoutError("semantic wrapper omitted")

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

            async def count(self) -> int:
                return 1 if self.visible else 0

            def nth(self, index: int) -> "Locator":
                del index
                return self

            async def is_visible(self) -> bool:
                return self.visible

        class Page:
            def __init__(self, username: str, body: str) -> None:
                self.url = f"https://www.instagram.com/{username}/"
                self.body = body

            def locator(self, selector: str) -> Locator:
                if selector == "body":
                    return Locator(self.body, visible=True)
                # Simulate an unrelated visible Instagram progress indicator.
                if "progressbar" in selector:
                    return Locator(visible=True)
                return Locator()

        worker = PlaywrightWorker(FakeBitBrowserClient())
        await worker._validate_recovery_profile_page(
            Page(
                "private_target",
                "private_target\n12 posts 100 followers 20 following\nThis account is private",
            ),
            "private_target",
        )
        await worker._validate_recovery_profile_page(
            Page(
                "zero_target",
                "zero_target\n0帖子 23粉丝 8关注\n这里空荡荡~",
            ),
            "zero_target",
        )

    async def test_profile_dom_mismatch_retries_finitely_without_becoming_network_wait(self) -> None:
        class RetryPage:
            async def wait_for_timeout(self, milliseconds: int) -> None:
                del milliseconds

        class UnknownDomWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = RetryPage()
                self.attempts = 0

            async def _read_visible_profile_once(
                self,
                target: str,
                *,
                include_activity: bool = False,
            ) -> VisibleProfile:
                del target, include_activity
                self.attempts += 1
                raise WorkerExecutionError(
                    "localized profile DOM is not recognized",
                    reason="instagram_profile_dom_unrecognized",
                    pause_required=True,
                )

        worker = UnknownDomWorker()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker.read_visible_profile("new_layout")

        self.assertEqual(1, worker.attempts)
        self.assertEqual("instagram_page_recovery_exhausted", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])

    async def test_relationship_list_waits_for_slow_first_rows(self) -> None:
        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

        class FakeLink:
            async def get_attribute(self, name: str) -> str | None:
                return "/slow_user/" if name == "href" else None

        class DelayedLinks:
            def __init__(self) -> None:
                self.reads = 0
                self.link = FakeLink()

            async def count(self) -> int:
                self.reads += 1
                return 1 if self.reads >= 5 else 0

            def nth(self, index: int) -> FakeLink:
                self.asserted_index = index
                return self.link

        class FakeDialog:
            def __init__(self) -> None:
                self.links = DelayedLinks()

            def locator(self, selector: str) -> DelayedLinks:
                del selector
                return self.links

            async def evaluate(self, script: str) -> None:
                del script

        class BodyLocator(EmptyLocator):
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "Followers"

        class FakePage:
            url = "https://www.instagram.com/source/followers/"

            def locator(self, selector: str) -> object:
                return BodyLocator() if selector == "body" else EmptyLocator()

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = FakePage()
        worker.collection_poll_interval_seconds = 0
        worker.collection_initial_idle_rounds = 6
        dialog = FakeDialog()

        self.assertEqual(
            ["slow_user"],
            await worker._read_visible_account_dialog(dialog, 1),
        )
        self.assertEqual(5, dialog.links.reads)

    async def test_unlimited_relationship_list_stops_only_at_confirmed_natural_end(self) -> None:
        class EmptyLoading:
            async def count(self) -> int:
                return 0

        class FakeLink:
            def __init__(self, username: str) -> None:
                self.username = username

            async def get_attribute(self, name: str) -> str | None:
                return f"/{self.username}/" if name == "href" else None

        class GrowingLinks:
            def __init__(self) -> None:
                self.reads = 0
                self.current: list[FakeLink] = []

            async def count(self) -> int:
                self.reads += 1
                names = ["first.account", "second.account"]
                if self.reads >= 2:
                    names.append("natural.tail")
                self.current = [FakeLink(name) for name in names]
                return len(self.current)

            def nth(self, index: int) -> FakeLink:
                return self.current[index]

        class FakeDialog:
            def __init__(self) -> None:
                self.links = GrowingLinks()

            def locator(self, selector: str) -> GrowingLinks | EmptyLoading:
                return self.links if "a[href" in selector else EmptyLoading()

            async def evaluate(self, script: str) -> dict[str, object] | None:
                if "relation-action:" in script:
                    return {"valid": True, "moved": False, "bottom": True,
                            "top": 0, "height": 200, "client": 200}
                return None

        class NaturalEndWorker(PlaywrightWorker):
            def __init__(self, *, allow_end: bool = True) -> None:
                super().__init__(FakeBitBrowserClient())
                self.allow_end = allow_end
                self.end_confirmations = 0
                # Fixture rows are already settled; loading delays have separate
                # logical-clock coverage in test_collection_stability.
                self.collection_loading_grace_seconds = .01

            async def _guard(self) -> None:
                return None

            async def _has_visible_loading_indicator(self) -> bool:
                return False

            async def _page_surface_failure(self) -> str | None:
                return None

            async def _confirm_relation_list_end(self, dialog: object) -> bool:
                self.end_confirmations += 1
                return self.allow_end and await super()._confirm_relation_list_end(dialog)

        worker = NaturalEndWorker()
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 1
        dialog = FakeDialog()
        durable_base = 3_000_000_000
        durable_seen: set[str] = set()

        async def sink(batch: list[str]) -> dict[str, int]:
            durable_seen.update(batch)
            return {"total": durable_base + len(durable_seen)}

        result = await worker._read_visible_account_dialog(
            dialog,
            None,
            candidate_sink=sink,
            initial_candidate_count=durable_base,
            candidate_total_limit=None,
        )

        self.assertEqual([], result)
        self.assertEqual(
            {"first.account", "second.account", "natural.tail"}, durable_seen
        )
        self.assertGreaterEqual(dialog.links.reads, 3)
        self.assertEqual(1, worker.end_confirmations)

        stalled = NaturalEndWorker(allow_end=False)
        stalled.collection_poll_interval_seconds = 0
        stalled.collection_settled_idle_rounds = 1
        with self.assertRaises(WorkerExecutionError) as incomplete:
            await stalled._read_visible_account_dialog(
                FakeDialog(),
                None,
                candidate_sink=sink,
                initial_candidate_count=durable_base + len(durable_seen),
                candidate_total_limit=None,
            )
        self.assertEqual(
            "instagram_followers_list_incomplete", incomplete.exception.code
        )

    async def test_relation_dispatch_passes_no_limit_for_new_and_legacy_settings(self) -> None:
        class RecordingWorker:
            received_limits: list[int | None] = []

            async def collect_followers(
                self, target: str, *, limit: int | None
            ) -> CollectionOutcome:
                del target
                type(self).received_limits.append(limit)
                return CollectionOutcome("followers", [])

            async def collect_following(
                self, target: str, *, limit: int | None
            ) -> CollectionOutcome:
                del target
                type(self).received_limits.append(limit)
                return CollectionOutcome("following", [])

        worker = RecordingWorker()
        for flag in (True, False):
            settings = {
                "unlimited_relation_collection": flag,
                "mode_limits": {
                    "followers": {"per_target_limit": 4},
                    "following": {"per_target_limit": 5},
                },
            }
            await ExecutionManager._collect_mode(
                worker, "source", "followers", settings
            )
            await ExecutionManager._collect_mode(
                worker, "source", "following", settings
            )
        self.assertEqual([None, None, None, None], RecordingWorker.received_limits)

    async def test_relationship_list_surfaces_cdp_disconnect_and_empty_shell(self) -> None:
        class ClosedLinks:
            async def count(self) -> int:
                raise RuntimeError("Target page, context or browser has been closed")

        class ClosedDialog:
            def locator(self, selector: str) -> ClosedLinks:
                del selector
                return ClosedLinks()

        closed_worker = PlaywrightWorker(FakeBitBrowserClient())
        with self.assertRaises(WorkerExecutionError) as closed:
            await closed_worker._read_visible_account_dialog(ClosedDialog(), 10)
        self.assertEqual("worker_not_connected", closed.exception.code)
        self.assertTrue(closed.exception.details["pause_required"])

        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

        class EmptyDialog:
            def locator(self, selector: str) -> EmptyLocator:
                del selector
                return EmptyLocator()

            async def evaluate(self, script: str) -> None:
                del script

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return ""

        class BodyLocator(EmptyLocator):
            def __init__(self, text: str) -> None:
                self.text = text

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

        class SurfacePage:
            url = "https://www.instagram.com/source/followers/"

            def __init__(self, text: str) -> None:
                self.text = text

            def locator(self, selector: str) -> object:
                return BodyLocator(self.text) if selector == "body" else EmptyLocator()

        empty_worker = PlaywrightWorker(FakeBitBrowserClient())
        empty_worker.page = SurfacePage("")
        empty_worker.collection_initial_idle_rounds = 1
        empty_worker.collection_poll_interval_seconds = 0
        # These immutable fixtures classify an already empty page. Loader grace
        # is covered separately; retaining its real 20 s clock here spins a zero
        # interval loop without adding a single new observation.
        empty_worker.collection_loading_grace_seconds = 0
        with self.assertRaises(WorkerExecutionError) as empty:
            await empty_worker._read_visible_account_dialog(EmptyDialog(), 10)
        self.assertEqual("instagram_profile_not_ready", empty.exception.code)
        self.assertTrue(empty.exception.details["pause_required"])

        complete_worker = PlaywrightWorker(FakeBitBrowserClient())
        complete_worker.page = SurfacePage("Followers list")
        complete_worker.collection_initial_idle_rounds = 1
        complete_worker.collection_poll_interval_seconds = 0
        complete_worker.collection_loading_grace_seconds = 0
        with self.assertRaises(WorkerExecutionError) as complete:
            await complete_worker._read_visible_account_dialog(EmptyDialog(), 10)
        self.assertEqual("instagram_followers_list_not_rendered", complete.exception.code)
        self.assertFalse(complete.exception.details["pause_required"])

    async def test_relationship_dialog_timeout_on_complete_page_is_not_network(self) -> None:
        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            @property
            def last(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

            async def is_visible(self) -> bool:
                return False

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs
                raise TimeoutError("Locator.wait_for: Timeout 12000ms exceeded")

        class BodyLocator(EmptyLocator):
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "Followers"

        class CompletePage:
            url = "https://www.instagram.com/source/followers/"

            async def goto(self, url: str, **kwargs: object) -> None:
                del kwargs
                self.url = url

            def locator(self, selector: str) -> object:
                if selector == "body":
                    return BodyLocator()
                return EmptyLocator()

        class CompleteRelationWorker(PlaywrightWorker):
            async def _find_relation_trigger(self, username_norm: str, relation: str) -> None:
                del username_norm, relation
                return None

            async def _wait_for_profile_surface(self) -> None:
                return None

            async def _guard(self) -> None:
                return None

        worker = CompleteRelationWorker(FakeBitBrowserClient())
        worker.page = CompletePage()
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._open_relation_surface("source", "followers")

        self.assertEqual("instagram_followers_list_not_rendered", raised.exception.code)
        self.assertFalse(raised.exception.details["pause_required"])

    async def test_disconnect_detaches_references_even_when_driver_cleanup_hangs(self) -> None:
        class HangingPlaywright:
            def __init__(self) -> None:
                self.started = False
                self.cancelled = False

            async def stop(self) -> None:
                self.started = True
                try:
                    await asyncio.Future()
                finally:
                    self.cancelled = True

        driver = HangingPlaywright()
        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.disconnect_timeout_seconds = 0.01
        worker._playwright = driver
        worker._browser = object()
        worker._context = object()
        worker.page = object()
        worker.profile_id = "stale-profile"
        worker._profile_base_cache["cached"] = VisibleProfile(
            "cached", "public", 1, 1, 1
        )

        await worker.disconnect()

        self.assertTrue(driver.started)
        self.assertTrue(driver.cancelled)
        self.assertIsNone(worker._playwright)
        self.assertIsNone(worker._browser)
        self.assertIsNone(worker._context)
        self.assertIsNone(worker.page)
        self.assertIsNone(worker.profile_id)
        self.assertFalse(worker._profile_base_cache)

    async def test_navigation_timeout_is_network_but_target_closed_is_cdp_failure(self) -> None:
        self.assertEqual(
            "instagram_network_unavailable",
            PlaywrightWorker._page_failure_reason(
                TimeoutError("Page.goto: Timeout 45000ms exceeded"),
                navigation_timeout=True,
            ),
        )
        self.assertEqual(
            "worker_not_connected",
            PlaywrightWorker._page_failure_reason(
                RuntimeError("Target page, context or browser has been closed"),
                navigation_timeout=True,
            ),
        )

    async def test_missing_metrics_and_chrome_error_are_transport_failures(self) -> None:
        class EmptyLocator:
            notices: list[str] = []

            async def evaluate(self, expression: str) -> list[str]:
                return self.notices

            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

        class SurfacePage:
            url = "https://www.instagram.com/target_user/"

            def locator(self, selector: str) -> EmptyLocator:
                del selector
                return EmptyLocator()

        worker = PlaywrightWorker(FakeBitBrowserClient())
        worker.page = SurfacePage()
        self.assertEqual(
            "instagram_profile_dom_unrecognized",
            await worker._profile_surface_is_transient(
                "target_user",
                body_text="target_user profile navigation shell",
                metrics=(None, None, None),
            ),
        )
        EmptyLocator.notices = ["This site can't be reached ERR_INTERNET_DISCONNECTED"]
        self.assertEqual(
            "instagram_network_unavailable",
            await worker._profile_transport_failure(
                "target_user",
                body_text="This site can't be reached ERR_INTERNET_DISCONNECTED",
            ),
        )
        EmptyLocator.notices = []
        self.assertIsNone(
            await worker._profile_surface_is_transient(
                "target_user",
                body_text="正常显示的主页",
                metrics=(120, 80, 9),
            )
        )

    async def test_zero_post_terminal_surface_ignores_unrelated_global_loader(self) -> None:
        class SurfacePage:
            url = "https://www.instagram.com/sample_empty01/"
            notices: list[str] = []

            def locator(self, selector: str) -> "SurfacePage":
                return self

            async def evaluate(self, expression: str) -> list[str]:
                return self.notices

        class BackgroundLoadingWorker(PlaywrightWorker):
            async def _has_visible_loading_indicator(self) -> bool:
                return True

        worker = BackgroundLoadingWorker(FakeBitBrowserClient())
        worker.page = SurfacePage()
        synthetic_profile_text = (
            "sample_empty01\nSample Empty\n0帖子 23粉丝 8关注\n关注\n这里空荡荡~"
        )

        self.assertIsNone(
            await worker._profile_surface_is_transient(
                "sample_empty01",
                body_text=synthetic_profile_text,
                metrics=(23, 8, 0),
                confirmed_public_empty=True,
            )
        )
        worker.page.notices = ["This site can't be reached ERR_INTERNET_DISCONNECTED"]
        self.assertEqual(
            "instagram_network_unavailable",
            await worker._profile_surface_is_transient(
                "sample_empty01",
                body_text=(
                    synthetic_profile_text
                    + "\nThis site can't be reached ERR_INTERNET_DISCONNECTED"
                ),
                metrics=(23, 8, 0),
                confirmed_public_empty=True,
            ),
        )
        worker.page.notices = []
        self.assertEqual(
            "instagram_profile_not_ready",
            await worker._profile_surface_is_transient(
                "sample_empty01",
                body_text=(
                    "这是私密账户\nsample_empty01\n0帖子 23粉丝 8关注\n"
                    "这里空荡荡~"
                ),
                metrics=(23, 8, 0),
                confirmed_public_empty=False,
            ),
        )

        private_synthetic_profile_text = (
            "sample_private01_\n0帖子 246粉丝 864关注\n"
            "这是私密主页\n关注即可查看其照片和视频。"
        )
        worker.page.url = "https://www.instagram.com/sample_private01_/"
        self.assertIsNone(
            await worker._profile_surface_is_transient(
                "sample_private01_",
                body_text=private_synthetic_profile_text,
                metrics=(246, 864, 0),
                confirmed_private=True,
            )
        )
        self.assertEqual(
            "instagram_profile_not_ready",
            await worker._profile_surface_is_transient(
                "sample_private01_",
                body_text=private_synthetic_profile_text,
                metrics=(246, None, 0),
                confirmed_private=True,
            ),
        )

    async def test_activity_and_location_propagate_offline_surface(self) -> None:
        class EmptyLocator:
            async def count(self) -> int:
                return 0

            def nth(self, index: int) -> "EmptyLocator":
                del index
                return self

        class BodyNode(EmptyLocator):
            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "This site can't be reached ERR_INTERNET_DISCONNECTED"

            async def evaluate(self, expression: str) -> list[str]:
                return ["This site can't be reached ERR_INTERNET_DISCONNECTED"]

        class OfflinePage:
            url = "https://www.instagram.com/offline_user/"

            class Keyboard:
                async def press(self, key: str) -> None:
                    del key

            keyboard = Keyboard()

            def locator(self, selector: str) -> object:
                return BodyNode() if selector == "body" else EmptyLocator()

        class OfflineSurfaceWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = OfflinePage()

            async def _navigate_profile(self, username: str) -> str:
                del username
                return "offline_user"

            async def _find_exact_profile_username_trigger(self, username_norm: str) -> None:
                del username_norm
                return None

            async def _has_visible_target_story(self, username_norm: str) -> bool:
                del username_norm
                return False

            async def _visible_post_urls(self, *, maximum: int) -> list[str]:
                del maximum
                return []

        worker = OfflineSurfaceWorker()
        with self.assertRaises(WorkerExecutionError) as location_error:
            await worker.read_visible_account_location("offline_user")
        self.assertEqual("instagram_page_recovery_exhausted", location_error.exception.code)

        with self.assertRaises(WorkerExecutionError) as activity_error:
            await worker._read_profile_activity(
                "offline_user",
                VisibleProfile("offline_user", "public", 100, 50, 9),
            )
        self.assertEqual("instagram_network_unavailable", activity_error.exception.code)

    async def test_privacy_response_listener_waits_for_delayed_profile_json(self) -> None:
        class FakeResponse:
            url = "https://www.instagram.com/graphql/query"
            headers = {"content-type": "application/json; charset=utf-8"}

            async def json(self) -> dict:
                return {"data": {"user": {"username": "delayed_target", "is_private": True}}}

        class FakePage:
            def __init__(self) -> None:
                self.handler = None

            def on(self, event: str, handler: object) -> None:
                self.handler = handler if event == "response" else None

            def remove_listener(self, event: str, handler: object) -> None:
                if event == "response" and self.handler is handler:
                    self.handler = None

        class DelayedPrivacyWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = FakePage()

            async def _navigate_profile(self, username: str) -> str:
                async def emit_after_shell() -> None:
                    await asyncio.sleep(0.01)
                    if self.page.handler:
                        self.page.handler(FakeResponse())

                asyncio.create_task(emit_after_shell())
                return username

            async def _read_inline_profile_privacy(self, username_norm: str) -> bool | None:
                del username_norm
                return None

        worker = DelayedPrivacyWorker()
        username, is_private = await worker._navigate_profile_with_privacy("delayed_target")
        self.assertEqual("delayed_target", username)
        self.assertIs(True, is_private)

    async def test_rendered_zero_relation_count_is_the_only_zero_shortcut(self) -> None:
        class ZeroRelationWorker(PlaywrightWorker):
            async def _navigate_profile(self, username: str) -> str:
                return username

            async def _visible_relation_count(self, username_norm: str, relation: str) -> int | None:
                del username_norm, relation
                return 0

            async def _open_relation_surface(self, username_norm: str, relation: str) -> object:
                del username_norm, relation
                raise AssertionError("A rendered zero count must not open an unneeded list surface")

        worker = ZeroRelationWorker(FakeBitBrowserClient())
        outcome = await worker.collect_followers("empty_source", limit=10)
        self.assertEqual([], outcome.usernames)

    async def test_post_liker_collection_skips_grid_wait_for_zero_post_profile(self) -> None:
        synthetic_profile_text = (
            "sample_empty01\nSample Empty\n0帖子 23粉丝 8关注\n关注\n这里空荡荡~"
        )

        class BodyLocator:
            def __init__(self, text: str) -> None:
                self.text = text

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return self.text

        class ForbiddenPostLocator:
            @property
            def first(self) -> "ForbiddenPostLocator":
                return self

            async def wait_for(self, **kwargs: object) -> None:
                del kwargs
                raise AssertionError("zero posts must bypass the post-grid wait")

        class ZeroPostPage:
            def __init__(
                self,
                *,
                url: str = "https://www.instagram.com/sample_empty01/",
                body_text: str = synthetic_profile_text,
            ) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> object:
                if selector == "body":
                    return BodyLocator(self.body_text)
                return ForbiddenPostLocator()

        class ZeroPostLikerWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = ZeroPostPage()

            async def _navigate_profile(self, username: str) -> str:
                return username

            async def _visible_post_urls(self, *, maximum: int) -> list[str]:
                del maximum
                raise AssertionError("zero posts must bypass post URL discovery")

        outcome = await ZeroPostLikerWorker().collect_post_likers(
            "sample_empty01",
            max_posts=1,
            per_post_limit=10,
        )

        self.assertEqual("post_likers", outcome.mode)
        self.assertEqual([], outcome.usernames)
        self.assertIsNone(outcome.candidate_count)

        wrong_target_worker = ZeroPostLikerWorker()
        wrong_target_worker.page.url = "https://www.instagram.com/another_user/"
        with self.assertRaisesRegex(AssertionError, "post URL discovery"):
            await wrong_target_worker.collect_post_likers(
                "sample_empty01",
                max_posts=1,
                per_post_limit=10,
            )

        private_worker = ZeroPostLikerWorker()
        private_worker.page.body_text = (
            "这是私密账户\nsample_empty01\n0帖子 23粉丝 8关注\n这里空荡荡~"
        )
        with self.assertRaisesRegex(AssertionError, "post URL discovery"):
            await private_worker.collect_post_likers(
                "sample_empty01",
                max_posts=1,
                per_post_limit=10,
            )

    async def test_post_liker_route_redirect_reacquires_and_clicks_the_visible_count(self) -> None:
        class FakePostSurface:
            @property
            def first(self) -> "FakePostSurface":
                return self

            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def inner_text(self, timeout: int = 0) -> str:
                del timeout
                return "42 likes"

        class FakePostPage:
            def __init__(self) -> None:
                self.url = "https://www.instagram.com/p/ABC123/"
                self.redirected = False
                self.surface = FakePostSurface()

            def locator(self, selector: str) -> FakePostSurface:
                del selector
                return self.surface

            async def goto(self, url: str, **kwargs: object) -> None:
                del url, kwargs
                # Simulate Instagram rewriting /liked_by/ back to the post page.
                self.redirected = True
                self.url = "https://www.instagram.com/p/ABC123/"

        class RedirectedLikerWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.page = FakePostPage()
                self.trigger = object()
                self.liker_surface = object()
                self.clicks = 0

            async def _find_post_likers_trigger(self) -> object | None:
                return self.trigger if self.page.redirected else None

            async def _click_post_likers_trigger(self, trigger: object) -> bool:
                self.assert_trigger = trigger
                self.clicks += 1
                return True

            async def _wait_for_post_likers_surface(
                self, *, attempts: int = 24, interval: float = 0.5
            ) -> tuple[object | None, bool]:
                del attempts, interval
                return (self.liker_surface, False) if self.clicks else (None, False)

            async def _wait_for_profile_surface(self) -> None:
                return None

            async def _guard(self) -> None:
                return None

        worker = RedirectedLikerWorker()
        surface, known_empty = await worker._open_post_likers_surface(
            "https://www.instagram.com/p/ABC123/"
        )
        self.assertIs(surface, worker.liker_surface)
        self.assertFalse(known_empty)
        self.assertEqual(1, worker.clicks)

    async def test_greeting_requires_an_empty_composer_and_new_transcript_bubble(self) -> None:
        class GreetingWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(FakeBitBrowserClient())
                self.transcript_count = 0

            async def _visible_transcript_message_count(self, message: str) -> int:
                del message
                return self.transcript_count

            async def _direct_send_control(self, composer: object | None = None) -> None:
                del composer
                return None

            async def _guard(self) -> None:
                return None

        class FakeComposer:
            def __init__(self, worker: GreetingWorker, *, actually_sends: bool) -> None:
                self.worker = worker
                self.actually_sends = actually_sends
                self.text = ""

            async def fill(self, value: str) -> None:
                self.text = value

            async def evaluate(self, expression: str) -> str:
                del expression
                return self.text

            async def press(self, key: str) -> None:
                self.asserted_key = key
                if self.actually_sends:
                    self.text = ""
                    self.worker.transcript_count += 1

        worker = GreetingWorker()
        sent = FakeComposer(worker, actually_sends=True)
        confirmation = await worker._send_and_confirm_greeting(
            sent, "你好，很高兴认识你！", poll_attempts=2, poll_interval=0
        )
        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)

        stalled_worker = GreetingWorker()
        stalled = FakeComposer(stalled_worker, actually_sends=False)
        with self.assertRaises(WorkerExecutionError) as raised:
            await stalled_worker._send_and_confirm_greeting(
                stalled, "你好，很高兴认识你！", poll_attempts=2, poll_interval=0
            )
        self.assertEqual("instagram_action_outcome_unknown", raised.exception.code)
        self.assertEqual("你好，很高兴认识你！", stalled.text)


class FakeCollectionWorker:
    connected_profiles: list[str] = []
    activity_reads: list[tuple[str, bool]] = []

    def __init__(self, _bitbrowser: BitBrowserClient) -> None:
        self.profile_id = ""

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id
        self.connected_profiles.append(profile_id)

    async def disconnect(self) -> None:
        return None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        await __import__("asyncio").sleep(0)
        return CollectionOutcome("followers", [f"{target.lstrip('@')}.fan"][:limit])

    async def collect_following(self, target: str, *, limit: int) -> CollectionOutcome:
        await __import__("asyncio").sleep(0)
        return CollectionOutcome("following", [f"{target.lstrip('@')}.friend"][:limit])

    async def collect_post_likers(self, target: str, *, max_posts: int, per_post_limit: int) -> CollectionOutcome:
        del max_posts, per_post_limit
        await __import__("asyncio").sleep(0)
        return CollectionOutcome("post_likers", [f"{target.lstrip('@')}.liker"])

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        self.activity_reads.append((target, include_activity))
        return {
            "username": target,
            "visibility": "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": 2 if include_activity else None,
        }

    async def read_visible_account_location(self, target: str) -> str | None:
        del target
        return "美国"


class BlockingQueueWorker(FakeCollectionWorker):
    started: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        if target == "keep_target":
            assert type(self).started is not None and type(self).release is not None
            type(self).started.set()
            await type(self).release.wait()
        return await super().collect_followers(target, limit=limit)


class DynamicTeardownRaceWorker(FakeCollectionWorker):
    """One initial worker fails while a later live-queue worker is still active."""

    initial_started: asyncio.Event | None = None
    dynamic_started: asyncio.Event | None = None
    allow_initial_failure: asyncio.Event | None = None
    dynamic_cancelled: asyncio.Event | None = None
    allow_dynamic_cleanup: asyncio.Event | None = None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        if self.profile_id == "teardown-initial-window":
            assert type(self).initial_started is not None
            assert type(self).allow_initial_failure is not None
            type(self).initial_started.set()
            await type(self).allow_initial_failure.wait()
            raise RuntimeError("initial worker fatal failure")
        if self.profile_id == "teardown-dynamic-window":
            assert type(self).dynamic_started is not None
            assert type(self).dynamic_cancelled is not None
            assert type(self).allow_dynamic_cleanup is not None
            type(self).dynamic_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                type(self).dynamic_cancelled.set()
                await type(self).allow_dynamic_cleanup.wait()
                raise
        return await super().collect_followers(target, limit=limit)


class LocationFirstWorker(FakeCollectionWorker):
    events: list[str] = []

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        type(self).events.append("activity" if include_activity else "profile")
        return {
            "username": target,
            "visibility": "public",
            "followers": 999,
            "following": 80,
            "posts": 9,
            "activity_days": None,
        }

    async def read_visible_account_location(self, target: str) -> str | None:
        del target
        type(self).events.append("location")
        return "美国"


class UnitedStatesGateWorker(FakeCollectionWorker):
    events: list[tuple[str, str]] = []

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        type(self).events.append((target, "activity" if include_activity else "profile"))
        return {
            "username": target,
            # Private profiles commonly expose no account-location row.  This
            # target proves that UNKNOWN location still reaches private review.
            "visibility": "private" if target.startswith("unknown") else "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": 2 if include_activity else None,
            "activity_status": "timestamp_found" if include_activity else "not_checked",
        }

    async def read_visible_account_location(self, target: str) -> str | None:
        type(self).events.append((target, "location"))
        if target.startswith("non_us"):
            return "加拿大"
        if target.startswith("us"):
            return "美国"
        return None


class RenamedNonUsGateWorker(FakeCollectionWorker):
    location_read = False

    async def read_visible_account_location(self, target: str) -> str | None:
        del target
        type(self).location_read = True
        return "加拿大"

    async def read_visible_profile(
        self, target: str, *, include_activity: bool = False
    ) -> dict:
        return {
            "username": target,
            "instagram_user_id": "9876543210987654321",
            "visibility": "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": None,
            "activity_status": "not_checked",
        }


class OverLimitCollectionWorker(FakeCollectionWorker):
    activity_reads: list[tuple[str, bool]] = []

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        type(self).activity_reads.append((target, include_activity))
        return {
            "username": target,
            "visibility": "public",
            "followers": 999,
            "following": 80,
            "posts": 9,
            "activity_days": 12 if include_activity else None,
            "activity_status": "timestamp_found" if include_activity else "not_checked",
        }


class UnknownCountActivityWorker(FakeCollectionWorker):
    activity_reads: list[tuple[str, bool]] = []

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        type(self).activity_reads.append((target, include_activity))
        activity_days = None
        activity_status = "not_checked"
        if include_activity:
            activity_days = 45 if "unknown_fail" in target else 5
            activity_status = "timestamp_found"
        return {
            "username": target,
            "visibility": "public",
            "followers": None,
            "following": 80,
            "posts": 9,
            "activity_days": activity_days,
            "activity_status": activity_status,
        }


class VerifiedCollectionWorker(FakeCollectionWorker):
    activity_reads: list[tuple[str, bool]] = []
    location_reads: list[str] = []

    async def read_visible_account_location(self, target: str) -> str:
        type(self).location_reads.append(target)
        return "美国"

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        type(self).activity_reads.append((target, include_activity))
        return {
            "username": target,
            "visibility": "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": None,
            "activity_status": "not_checked",
            "visible_description": "Creator · she/her",
            "is_verified": True,
        }


class MissingRelationWorker(FakeCollectionWorker):
    attempts: list[str] = []

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).attempts.append(target)
        if target == "blocked_source":
            raise WorkerExecutionError(
                "Instagram 已打开目标主页，但没有显示粉丝列表",
                reason="instagram_followers_list_not_rendered",
                pause_required=False,
            )
        return await super().collect_followers(target, limit=limit)


class PartialPostLikersWorker(FakeCollectionWorker):
    async def collect_post_likers(self, target: str, *, max_posts: int, per_post_limit: int) -> CollectionOutcome:
        del target, max_posts, per_post_limit
        return CollectionOutcome(
            "post_likers",
            ["saved.liker"],
            skipped_posts=["https://www.instagram.com/p/UNRENDERED/"],
        )


class PartialFailureCollectionWorker(FakeCollectionWorker):
    failures_remaining = 1
    profile_reads: list[str] = []

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        del target
        return CollectionOutcome("followers", ["saved.one", "saved.two", "rate.limited"][:limit])

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        del include_activity
        type(self).profile_reads.append(target)
        if target == "rate.limited" and type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise WorkerExecutionError(
                "Instagram asked this window to wait before continuing",
                reason="instagram_rate_limited",
                pause_required=True,
            )
        return {
            "username": target,
            "visibility": "public",
            "followers": 120,
            "following": 80,
            "posts": 9,
            "activity_days": None,
        }


class LocationFailureOnceCollectionWorker(FakeCollectionWorker):
    failures_remaining = 1
    location_reads = 0

    async def read_visible_account_location(self, target: str) -> str:
        del target
        type(self).location_reads += 1
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise WorkerExecutionError(
                "Instagram 所在地详情连续加载失败；已保留当前账号并自动重试",
                reason="instagram_location_temporarily_unavailable",
                pause_required=True,
                status_code=503,
                retry_after_seconds=0.2,
            )
        return "美国"


class PauseBetweenModesCollectionWorker(FakeCollectionWorker):
    profile_read_started: asyncio.Event | None = None
    profile_read_release: asyncio.Event | None = None
    follower_calls = 0
    following_calls = 0

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).follower_calls += 1
        return await super().collect_followers(target, limit=limit)

    async def collect_following(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).following_calls += 1
        return await super().collect_following(target, limit=limit)

    async def read_visible_profile(self, target: str, *, include_activity: bool = False) -> dict:
        if target.endswith(".fan"):
            assert type(self).profile_read_started is not None
            assert type(self).profile_read_release is not None
            type(self).profile_read_started.set()
            await type(self).profile_read_release.wait()
        return await super().read_visible_profile(target, include_activity=include_activity)


class MultiModeRecoverOnceWorker(FakeCollectionWorker):
    follower_calls = 0
    following_calls = 0
    following_failures_remaining = 1

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).follower_calls += 1
        return await super().collect_followers(target, limit=limit)

    async def collect_following(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).following_calls += 1
        if type(self).following_failures_remaining:
            type(self).following_failures_remaining -= 1
            raise WorkerExecutionError(
                "Instagram window was interrupted during the following mode",
                reason="instagram_challenge_required",
                pause_required=True,
            )
        return await super().collect_following(target, limit=limit)


class LiveQueueIncompleteOnceWorker(FakeCollectionWorker):
    attempts = 0

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise WorkerExecutionError(
                "Source content is currently not visible to this account",
                reason="instagram_content_not_visible",
                pause_required=False,
            )
        return await super().collect_followers(target, limit=limit)


class RelationshipListIncompleteOnceWorker(FakeCollectionWorker):
    attempts = 0
    connect_calls = 0
    disconnect_calls = 0
    stalled = None

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        type(self).connect_calls += 1
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def disconnect(self) -> None:
        type(self).disconnect_calls += 1

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).attempts += 1
        if type(self).attempts == 1:
            if type(self).stalled is not None:
                type(self).stalled.set()
            raise WorkerExecutionError(
                "Relationship list ended before the visible total",
                reason="instagram_followers_list_incomplete",
                pause_required=True,
                status_code=503,
            )
        return await super().collect_followers(target, limit=limit)


class TeardownGateExecutionManager(ExecutionManager):
    """Keep fatal worker teardown alive long enough to exercise the resume race."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.closing = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def _cancel_and_gather_worker_tasks(self, control) -> None:
        self.closing.set()
        await self.allow_close.wait()
        await super()._cancel_and_gather_worker_tasks(control)


class NetworkWaitObserverExecutionManager(ExecutionManager):
    """Expose an event when every expected window has published its retry waiter."""

    def __init__(self, *args, expected_waiters: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_waiters = expected_waiters
        self.waiters_ready = asyncio.Event()

    async def _set_network_waiting(self, control, profile_id, exc, **kwargs) -> int:
        generation = await super()._set_network_waiting(
            control, profile_id, exc, **kwargs
        )
        if len(control.network_waiters) >= self.expected_waiters:
            self.waiters_ready.set()
        return generation


class NetworkRecoveryCollectionWorker(FakeCollectionWorker):
    network_available: asyncio.Event | None = None
    first_connection_done = False
    collection_attempts = 0
    reconnect_attempts = 0

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if not type(self).first_connection_done:
            type(self).first_connection_done = True
            await super().connect(profile_id, open_if_needed=open_if_needed)
            return
        type(self).reconnect_attempts += 1
        assert type(self).network_available is not None
        if not type(self).network_available.is_set():
            raise UpstreamUnavailableError("Network is still unavailable")
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).collection_attempts += 1
        if type(self).collection_attempts == 1:
            raise UpstreamUnavailableError("Network is temporarily unavailable")
        return await super().collect_followers(target, limit=limit)


class InstagramSurfaceRetryWorker(FakeCollectionWorker):
    attempts = 0
    connect_calls = 0
    disconnect_calls = 0
    first_failure: asyncio.Event | None = None

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        type(self).connect_calls += 1
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def disconnect(self) -> None:
        type(self).disconnect_calls += 1

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).attempts += 1
        if type(self).attempts == 1:
            if type(self).first_failure is not None:
                type(self).first_failure.set()
            raise WorkerExecutionError(
                "Instagram profile is temporarily unavailable",
                reason="instagram_profile_temporarily_unavailable",
                pause_required=True,
                status_code=503,
            )
        return await super().collect_followers(target, limit=limit)


class AuthRequiredRecoveryWorker(FakeCollectionWorker):
    auth_available: asyncio.Event | None = None
    first_connection_done = False
    reconnect_attempted: asyncio.Event | None = None
    reconnect_attempts = 0
    collection_attempts = 0

    @staticmethod
    def _auth_error() -> BitBrowserAuthRequiredError:
        return BitBrowserAuthRequiredError(
            "BitBrowser 已退出登录，请重新登录后继续",
            details={
                "state": "auth_required",
                "reason": "bitbrowser_login_required",
            },
        )

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if not type(self).first_connection_done:
            type(self).first_connection_done = True
            await super().connect(profile_id, open_if_needed=open_if_needed)
            return
        type(self).reconnect_attempts += 1
        assert type(self).reconnect_attempted is not None
        type(self).reconnect_attempted.set()
        assert type(self).auth_available is not None
        if not type(self).auth_available.is_set():
            raise type(self)._auth_error()
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).collection_attempts += 1
        assert type(self).auth_available is not None
        if not type(self).auth_available.is_set():
            raise type(self)._auth_error()
        return await super().collect_followers(target, limit=limit)


class MultiWindowNetworkRecoveryWorker(FakeCollectionWorker):
    network_available: asyncio.Event | None = None
    initially_connected: set[str] = set()
    reconnect_profiles: list[str] = []
    collection_attempts: dict[str, int] = {}

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if profile_id not in type(self).initially_connected:
            type(self).initially_connected.add(profile_id)
            await super().connect(profile_id, open_if_needed=open_if_needed)
            return
        type(self).reconnect_profiles.append(profile_id)
        assert type(self).network_available is not None
        if not type(self).network_available.is_set():
            raise UpstreamUnavailableError("Network is still unavailable")
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        attempts = type(self).collection_attempts.get(target, 0) + 1
        type(self).collection_attempts[target] = attempts
        if attempts == 1:
            raise WorkerExecutionError(
                "Instagram transport unavailable",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        return await super().collect_followers(target, limit=limit)


class PartialWindowNetworkWorker(MultiWindowNetworkRecoveryWorker):
    online_release: asyncio.Event | None = None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        if target == "online_source":
            assert type(self).online_release is not None
            await type(self).online_release.wait()
            return await FakeCollectionWorker.collect_followers(self, target, limit=limit)
        return await super().collect_followers(target, limit=limit)


class LocationNetworkRecoveryWorker(NetworkRecoveryCollectionWorker):
    location_attempts = 0

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        return await FakeCollectionWorker.collect_followers(self, target, limit=limit)

    async def read_visible_account_location(self, target: str) -> str | None:
        del target
        type(self).location_attempts += 1
        if type(self).location_attempts == 1:
            raise WorkerExecutionError(
                "Location surface did not load because the network is unavailable",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        return "美国"


class CdpHealthyInstagramOfflineWorker(FakeCollectionWorker):
    instagram_available: asyncio.Event | None = None
    collection_attempts = 0

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        type(self).collection_attempts += 1
        assert type(self).instagram_available is not None
        if not type(self).instagram_available.is_set():
            raise WorkerExecutionError(
                "BitBrowser is connected but Instagram cannot be reached",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        return await super().collect_followers(target, limit=limit)


class NetworkThenUnavailableWorker(FakeCollectionWorker):
    """Recover transport once, then publish a conclusive unavailable surface."""

    attempts = 0
    reconnected = asyncio.Event()

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if self.profile_id:
            type(self).reconnected.set()
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        del target, limit
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise WorkerExecutionError(
                "Instagram transport unavailable",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        raise WorkerExecutionError(
            "Instagram did not render the followers list",
            reason="instagram_followers_list_not_rendered",
            pause_required=False,
        )


class ProfileInterventionIsolationWorker(FakeCollectionWorker):
    intervention_cleared: asyncio.Event | None = None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        if self.profile_id == "challenge-window":
            assert type(self).intervention_cleared is not None
            if not type(self).intervention_cleared.is_set():
                raise WorkerExecutionError(
                    "Instagram requires manual verification in this window",
                    reason="instagram_challenge",
                    pause_required=True,
                )
        return await super().collect_followers(target, limit=limit)


class AllTargetsBlockingWorker(FakeCollectionWorker):
    expected_profiles = 0
    started_profiles: set[str] = set()
    all_started: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        assert type(self).all_started is not None
        assert type(self).release is not None
        type(self).started_profiles.add(self.profile_id)
        if len(type(self).started_profiles) >= type(self).expected_profiles:
            type(self).all_started.set()
        await type(self).release.wait()
        return await super().collect_followers(target, limit=limit)


class BoundedReconnectWorker(FakeCollectionWorker):
    initially_connected: set[str] = set()
    attempts_by_profile: dict[str, int] = {}
    reconnect_started: asyncio.Event | None = None
    allow_reconnect: asyncio.Event | None = None
    active_reconnects = 0
    max_active_reconnects = 0
    reconnect_profiles: set[str] = set()

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if profile_id not in type(self).initially_connected:
            type(self).initially_connected.add(profile_id)
            await super().connect(profile_id, open_if_needed=open_if_needed)
            return
        assert type(self).reconnect_started is not None
        assert type(self).allow_reconnect is not None
        type(self).active_reconnects += 1
        type(self).max_active_reconnects = max(
            type(self).max_active_reconnects, type(self).active_reconnects
        )
        type(self).reconnect_profiles.add(profile_id)
        type(self).reconnect_started.set()
        try:
            await type(self).allow_reconnect.wait()
        finally:
            type(self).active_reconnects -= 1
        await super().connect(profile_id, open_if_needed=open_if_needed)

    async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
        attempts = type(self).attempts_by_profile.get(self.profile_id, 0) + 1
        type(self).attempts_by_profile[self.profile_id] = attempts
        if attempts == 1:
            raise WorkerExecutionError(
                "Instagram transport unavailable",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            )
        return await super().collect_followers(target, limit=limit)


class ExplodingReviewer:
    def review(self, _profile: dict) -> dict:
        raise AssertionError("GPT must not run after a basic-count rejection")


class UnavailableReviewer:
    def review(self, _profile: dict) -> dict:
        raise RuntimeError("temporary OpenAI failure")


class TrackingReviewer:
    reviewed: list[str] = []

    def review(self, profile: dict) -> dict:
        type(self).reviewed.append(str(profile.get("username")))
        return {"review_status": "reviewed", "confidence": "low", "reason_codes": []}


class FakeActionWorker:
    calls: list[tuple[str, str]] = []

    def __init__(self, _bitbrowser: BitBrowserClient) -> None:
        return None

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del profile_id, open_if_needed

    async def disconnect(self) -> None:
        return None

    async def execute_action(self, operation: str, target: str, *, message: str | None = None) -> ActionOutcome:
        del message
        self.calls.append((operation, target))
        return ActionOutcome(operation, target, "confirmed", "visible_state_changed")


class UnknownActionWorker(FakeActionWorker):
    attempts = 0

    async def execute_action(self, operation: str, target: str, *, message: str | None = None) -> ActionOutcome:
        del operation, target, message
        type(self).attempts += 1
        raise WorkerExecutionError(
            "Outcome unknown",
            reason="instagram_action_outcome_unknown",
            pause_required=True,
        )


class StructuralGreetingFailureWorker(FakeActionWorker):
    calls = 0

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del operation, target, message
        type(self).calls += 1
        raise WorkerExecutionError(
            "Conversation opened but the composer was not recognized",
            reason="instagram_direct_composer_not_ready",
            pause_required=False,
        )


class MissingRecipientThenSuccessWorker(FakeActionWorker):
    calls = 0

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del message
        type(self).calls += 1
        if type(self).calls == 1:
            raise WorkerExecutionError(
                "The exact first recipient was not present",
                reason="instagram_direct_inbox_recipient_not_found",
                pause_required=False,
            )
        return ActionOutcome(operation, target, "confirmed", "message visible")


class ExplodingActionConnectWorker(FakeActionWorker):
    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del profile_id, open_if_needed
        raise RuntimeError("unexpected browser bootstrap failure")


class BlockingManualActionWorker(FakeActionWorker):
    started: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del message
        assert type(self).started is not None
        assert type(self).release is not None
        type(self).started.set()
        await type(self).release.wait()
        return ActionOutcome(operation, target, "confirmed", "visible_state_changed")


class CapturingGreetingWorker(FakeActionWorker):
    calls: list[tuple[str, str, str | None]] = []

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        type(self).calls.append((operation, target, message))
        return ActionOutcome(operation, target, "confirmed", "message_visible_in_direct_thread")


class RuntimeManagerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "runtime.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user("runtime-user", PASSWORD)
        self.runtime_managers: list[ExecutionManager] = []
        self._original_execution_manager_init = ExecutionManager.__init__
        original_init = self._original_execution_manager_init

        def tracked_init(manager, *args, **kwargs) -> None:
            original_init(manager, *args, **kwargs)
            self.runtime_managers.append(manager)

        ExecutionManager.__init__ = tracked_init

    async def asyncTearDown(self) -> None:
        ExecutionManager.__init__ = self._original_execution_manager_init
        shutdown_error: BaseException | None = None
        try:
            for manager in self.runtime_managers:
                teardown_gate = getattr(manager, "allow_close", None)
                if isinstance(teardown_gate, asyncio.Event):
                    teardown_gate.set()
            if self.runtime_managers:
                try:
                    shutdown_results = await asyncio.wait_for(
                        asyncio.gather(
                            *(manager.shutdown() for manager in self.runtime_managers),
                            return_exceptions=True,
                        ),
                        timeout=5,
                    )
                    shutdown_error = next(
                        (
                            result
                            for result in shutdown_results
                            if isinstance(result, BaseException)
                        ),
                        None,
                    )
                except BaseException as exc:
                    shutdown_error = exc
        finally:
            try:
                current_task = asyncio.current_task()
                remaining_tasks = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not current_task and not task.done()
                ]
                for task in remaining_tasks:
                    task.cancel()
                if remaining_tasks:
                    await asyncio.gather(*remaining_tasks, return_exceptions=True)
                await asyncio.get_running_loop().shutdown_default_executor()
            finally:
                self.temp_dir.cleanup()
        if shutdown_error is not None:
            raise shutdown_error

    async def test_completed_live_targets_auto_archive_after_all_modes_and_preserve_window_history(self) -> None:
        class SharedAudienceWorker(FakeCollectionWorker):
            async def collect_followers(self, target, *, limit):
                return CollectionOutcome("followers", ["shared.one", "shared.two"], source_total=143)

        task = self.service.create_task(
            self.user["id"], name="完成自动归档", modes=["followers", "following"],
            targets=["first_source", "next_source"], window_ids=["reusable-window"],
            settings={"live_queue_enabled": True, "local_person_recognition": False,
                      "exclude_male_avatar": False},
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=SharedAudienceWorker)
        await manager.start(self.user["id"], task["id"])
        for _ in range(200):
            current = self.service.get_task(self.user["id"], task["id"])
            if all(item["status"] == "completed" for item in current["targets"]):
                break
            await asyncio.sleep(.01)
        else:
            self.fail("Both targets must finish on the same live window")
        await asyncio.wait_for(manager.wait(task["id"]), timeout=2)
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(["reusable-window"], current["window_ids"])
        self.assertEqual("completed", current["status"])
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))
        for target in current["targets"]:
            self.assertEqual("completed_archived", target["current_stage"])
            self.assertEqual("reusable-window", target["current_window_id"])
            for mode in ["followers", "following"]:
                checkpoint = self.service.get_checkpoint(self.user["id"], task["id"], target["id"], mode)
                self.assertEqual("mode_completed", checkpoint["stage"])
                self.assertEqual(0, checkpoint["counters"]["pending_candidates"])
        self.assertEqual(4, len(self.service.list_results(self.user["id"], task["id"])))
        second = current["targets"][1]
        stats = self.service.task_mode_candidate_stats(self.user["id"], task["id"], second["id"], "followers")
        self.assertEqual(2, stats["deduped"])
        self.assertEqual(143, second["mode_progress"]["followers"]["source_total"])
        self.assertEqual(2, second["mode_progress"]["followers"]["processed"])
        with self.database.read() as connection:
            history = connection.execute(
                "SELECT current_window_id FROM task_targets WHERE task_id=? AND status='completed'", (task["id"],)
            ).fetchall()
            self.assertEqual(2, len(history))
            self.assertTrue(all(row["current_window_id"] == "reusable-window" for row in history))

    async def test_completed_checkpoint_with_pending_candidate_is_drained_before_archive(self) -> None:
        class NoReopenWorker(FakeCollectionWorker):
            async def collect_followers(self, *args, **kwargs):
                raise AssertionError("Confirmed list must not be opened again")

        task = self.service.create_task(
            self.user["id"], name="完成断点还有待处理", modes=["followers"],
            targets=["pending_source"], window_ids=["pending-window"],
            settings={"local_person_recognition": False, "exclude_male_avatar": False},
        )
        target_id = task["targets"][0]["id"]
        self.service.append_task_mode_candidates(self.user["id"], task["id"], target_id, "followers", ["remaining.account"])
        self.service.upsert_checkpoint(
            self.user["id"], task["id"], target_id, mode="followers", stage="mode_completed",
            cursor={"candidate_spool_complete": True, "candidate_spool_natural_end": True},
            counters={"discovered": 1, "pending_candidates": 0},
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=NoReopenWorker)
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        self.assertEqual(1, len(self.service.list_results(self.user["id"], task["id"])))
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", current["status"])
        self.assertEqual("completed_archived", current["targets"][0]["current_stage"])

    async def test_missing_discovery_completion_does_not_archive_even_when_counts_match(self) -> None:
        class IncompleteManager(ExecutionManager):
            async def _execute_candidate_spooled_mode(self, *args, **kwargs):
                return {"total": 124, "recorded": 124, "deduped": 0, "pending": 0,
                        "source_total": 124, "skipped_posts": [], "discovery_complete": False}

        task = self.service.create_task(
            self.user["id"], name="数字不能代替完成证据", modes=["followers"],
            targets=["incomplete_source"], window_ids=["incomplete-window"],
            settings={"local_person_recognition": False, "exclude_male_avatar": False},
        )
        manager = IncompleteManager(self.service, FakeBitBrowserClient(), worker_factory=FakeCollectionWorker)
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertNotEqual("completed", current["status"])
        self.assertEqual("recoverable", current["targets"][0]["status"])
        self.assertNotEqual("completed_archived", current["targets"][0]["current_stage"])

    async def test_zero_optional_filters_are_disabled_but_location_stays_mandatory(self) -> None:
        class ZeroFilterUnitedStatesWorker(FakeCollectionWorker):
            activity_reads: list[tuple[str, bool]] = []

            async def read_visible_account_location(self, target: str) -> str:
                del target
                return "美国"

        ZeroFilterUnitedStatesWorker.activity_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="零值条件不启用",
            modes=["followers"],
            targets=["zero_filter_source"],
            window_ids=["zero-filter-window"],
            settings={
                "followers_min": 0,
                "followers_max": 0,
                "following_min": 0,
                "following_max": 0,
                "posts_min": 0,
                "posts_max": 0,
                "active_days_max": 0,
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=ZeroFilterUnitedStatesWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertTrue(result["qualified"])
        self.assertFalse(result["screening"]["activity"]["enabled"])
        self.assertTrue(result["screening"]["activity"]["checked"])
        self.assertEqual("collected_for_review", result["screening"]["activity"]["reason"])
        self.assertTrue(result["screening"]["location"]["enabled"])
        self.assertTrue(result["screening"]["location"]["checked"])
        self.assertEqual(
            [("zero_filter_source.fan", False), ("zero_filter_source.fan", True)],
            ZeroFilterUnitedStatesWorker.activity_reads,
        )

    async def test_delete_window_cleans_stale_persisted_execution_without_live_worker(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="旧历史任务",
            modes=["followers"],
            targets=["stale_source"],
            window_ids=["stale-window"],
            settings={},
        )
        target_id = task["targets"][0]["id"]
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target_id, "running",
            window_id="stale-window",
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient())

        result = await manager.delete_window(
            self.user["id"], task["id"], "stale-window", target_id=target_id
        )

        refreshed = self.service.get_task(self.user["id"], task["id"])
        self.assertTrue(result["stale_record_cleaned"])
        self.assertEqual("deleted", result["status"])
        self.assertEqual([], refreshed["window_ids"])
        self.assertEqual("stopped", refreshed["status"])
        self.assertEqual("stopped", refreshed["targets"][0]["status"])
        self.assertIsNone(refreshed["targets"][0]["current_window_id"])
        self.assertIsNone(refreshed["targets"][0]["preferred_window_id"])
        self.assertFalse(refreshed["targets"][0]["manual_recovery_required"])
        self.assertEqual("queued", result["requeued_candidate"]["queue_state"])
        queued = self.service.list_split_candidates(self.user["id"])
        self.assertTrue(any(
            item["username"] == "stale_source"
            and item["kind"] == "manual"
            and item["queue_state"] == "queued"
            for item in queued
        ))

    async def test_stop_window_is_idempotent_for_persisted_recoverable_profile_only(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="持久化窗口停止",
            modes=["followers"],
            targets=["recoverable_a", "recoverable_b"],
            window_ids=["persisted-window-a", "persisted-window-b"],
            settings={},
        )
        target_a, target_b = task["targets"]
        self.service.set_target_runtime_status(
            self.user["id"],
            task["id"],
            target_a["id"],
            "recoverable",
            window_id="persisted-window-a",
        )
        self.service.set_target_runtime_status(
            self.user["id"],
            task["id"],
            target_b["id"],
            "recoverable",
            window_id="persisted-window-b",
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient())

        stopped = await manager.stop_window(
            self.user["id"], task["id"], "persisted-window-a"
        )

        self.assertEqual("stopped", stopped["status"])
        self.assertEqual(1, stopped["affected_window_count"])
        self.assertFalse(stopped["already_stopped"])
        refreshed = self.service.get_task(self.user["id"], task["id"])
        refreshed_by_id = {item["id"]: item for item in refreshed["targets"]}
        self.assertEqual("stopped", refreshed_by_id[target_a["id"]]["status"])
        self.assertEqual("recoverable", refreshed_by_id[target_b["id"]]["status"])
        self.assertEqual(
            "persisted-window-b",
            refreshed_by_id[target_b["id"]]["preferred_window_id"],
        )

        replay = await manager.stop_window(
            self.user["id"], task["id"], "persisted-window-a"
        )
        self.assertTrue(replay["already_stopped"])
        self.assertEqual(1, replay["affected_window_count"])
        self.assertEqual(
            "recoverable",
            next(
                item
                for item in self.service.get_task(self.user["id"], task["id"])[
                    "targets"
                ]
                if item["id"] == target_b["id"]
            )["status"],
        )

        deleted = await manager.delete_window(
            self.user["id"],
            task["id"],
            "persisted-window-a",
            target_id=target_a["id"],
        )
        self.assertEqual("deleted", deleted["status"])
        self.assertEqual("queued", deleted["requeued_candidate"]["queue_state"])
        after_delete = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(["persisted-window-b"], after_delete["window_ids"])
        self.assertEqual(
            "recoverable",
            next(
                item
                for item in after_delete["targets"]
                if item["id"] == target_b["id"]
            )["status"],
        )

    async def test_delete_window_repairs_a_previously_dismissed_failure_and_is_idempotent(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="删除失败记录后仍可退回",
            modes=["followers"],
            targets=["dismissed_then_returned"],
            window_ids=["dismissed-window"],
            settings={},
        )
        target_id = task["targets"][0]["id"]
        self.service.set_target_runtime_status(
            self.user["id"],
            task["id"],
            target_id,
            "recoverable",
            window_id="dismissed-window",
        )
        failure = next(
            item
            for item in self.service.list_split_candidates(self.user["id"])
            if item["kind"] == "failure" and item["username"] == "dismissed_then_returned"
        )
        old_failure_id = failure["id"]
        self.service.delete_failed_split_candidate(self.user["id"], old_failure_id)

        # Model the half-finished state left by the old two-step delete flow: the
        # target was archived and detached, but its dismissed recovery gate made
        # requeue_removed_task_target raise before the task window was removed.
        self.service.delete_task_target(self.user["id"], task["id"], target_id)
        partially_deleted = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", partially_deleted["targets"][0]["status"])
        self.assertIsNone(partially_deleted["targets"][0]["current_window_id"])
        self.assertIsNone(partially_deleted["targets"][0]["preferred_window_id"])
        self.assertEqual(["dismissed-window"], partially_deleted["window_ids"])

        manager = ExecutionManager(self.service, FakeBitBrowserClient())
        result = await manager.delete_window(
            self.user["id"],
            task["id"],
            "dismissed-window",
            target_id=target_id,
        )

        self.assertEqual("deleted", result["status"])
        self.assertEqual("queued", result["requeued_candidate"]["queue_state"])
        self.assertEqual(
            "dismissed_then_returned", result["requeued_candidate"]["username"]
        )
        refreshed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual([], refreshed["window_ids"])
        waiting = [
            item
            for item in self.service.list_split_candidates(self.user["id"])
            if item["username"] == "dismissed_then_returned"
            and item["kind"] == "manual"
            and item["queue_state"] == "queued"
        ]
        self.assertEqual(1, len(waiting))

        # A delayed request for the dismissed generation cannot delete the newly
        # returned queue row, and replaying DELETE itself is also a clean success.
        stale_failure_delete = self.service.delete_failed_split_candidate(
            self.user["id"], old_failure_id
        )
        self.assertTrue(stale_failure_delete["already_absent"])
        replay = await manager.delete_window(
            self.user["id"],
            task["id"],
            "dismissed-window",
            target_id=target_id,
        )
        self.assertTrue(replay["already_deleted"])
        self.assertEqual(0, replay["affected_window_count"])
        self.assertEqual(
            1,
            len(
                [
                    item
                    for item in self.service.list_split_candidates(self.user["id"])
                    if item["username"] == "dismissed_then_returned"
                    and item["kind"] == "manual"
                    and item["queue_state"] == "queued"
                ]
            ),
        )

    async def test_stale_window_delete_cannot_remove_another_windows_target(self) -> None:
        task = self.service.create_task(
            self.user["id"], name="陈旧任务窗口隔离", modes=["followers"],
            targets=["stale_a", "stale_b"],
            window_ids=["stale-window-a", "stale-window-b"], settings={},
        )
        self.service.set_task_runtime_status(self.user["id"], task["id"], "running")
        for target, profile_id in zip(
            task["targets"], ["stale-window-a", "stale-window-b"], strict=True
        ):
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], target["id"], "running",
                window_id=profile_id,
            )
            self.service.set_target_runtime_status(
                self.user["id"], task["id"], target["id"], "recoverable",
                window_id=profile_id,
            )
        manager = ExecutionManager(self.service, FakeBitBrowserClient())

        with self.assertRaises(ConflictError):
            await manager.delete_window(
                self.user["id"], task["id"], "stale-window-a",
                target_id=task["targets"][1]["id"],
            )

        refreshed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(["stale-window-a", "stale-window-b"], refreshed["window_ids"])
        self.assertEqual("running", refreshed["status"])
        self.assertEqual(
            ["recoverable", "recoverable"],
            [target["status"] for target in refreshed["targets"]],
        )

    async def test_delete_completed_window_archives_card_without_requeue(self) -> None:
        task = self.service.create_task(
            self.user["id"], name="已完成任务", modes=["followers"],
            targets=["completed_source"], window_ids=["completed-window"], settings={},
        )
        target_id = task["targets"][0]["id"]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target_id, "completed",
            window_id="completed-window",
        )
        self.service.finalize_task_runtime_status(self.user["id"], task["id"], "completed")
        manager = ExecutionManager(self.service, FakeBitBrowserClient())

        result = await manager.delete_window(
            self.user["id"], task["id"], "completed-window", target_id=target_id
        )

        refreshed = self.service.get_task(self.user["id"], task["id"])
        target = refreshed["targets"][0]
        self.assertTrue(result["completed_archived"])
        self.assertIsNone(result["requeued_candidate"])
        self.assertEqual("completed", target["status"])
        self.assertEqual("completed_archived", target["current_stage"])
        self.assertIsNone(target["current_window_id"])
        self.assertIsNone(target["preferred_window_id"])
        self.assertFalse(any(
            item["username"] == "completed_source" and item["queue_state"] == "queued"
            for item in self.service.list_split_candidates(self.user["id"])
        ))

    async def test_stopped_live_window_delete_returns_target_with_window_pool(self) -> None:
        class UnavailableQueuedWorker(FakeCollectionWorker):
            async def collect_followers(self, target, *, limit):
                if target == "affine_delete_target":
                    raise WorkerExecutionError(
                        "Source is unavailable", reason="instagram_content_not_visible",
                        pause_required=False,
                    )
                return await super().collect_followers(target, limit=limit)

        task = self.service.create_task(
            self.user["id"], name="停止后删除并退回", modes=["followers"],
            targets=["completed_seed"], window_ids=["affinity-window-a"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        saved = self.service.upsert_manual_split_candidates(
            self.user["id"],
            [{
                "username": "affine_delete_target",
                "queued": True,
                "allowed_window_ids": ["affinity-window-a", "affinity-window-b"],
            }],
            include_outcome=True,
        )
        self.assertEqual(1, saved["accepted_count"])
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=UnavailableQueuedWorker
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            claimed = next((item for item in current["targets"]
                            if item["username"] == "affine_delete_target"), None)
            if claimed and claimed["status"] == "recoverable":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("The queued target did not retain its failed live window")
        self.assertIn("affinity-window-a", manager._runs[task["id"]].leases)
        failure = next(
            item
            for item in self.service.list_split_candidates(self.user["id"])
            if item["username"] == "affine_delete_target"
            and item["kind"] == "failure"
        )
        self.assertEqual(
            ["affinity-window-a", "affinity-window-b"],
            failure["allowed_window_ids"],
        )
        self.assertEqual("specified", failure["window_assignment_mode"])

        await manager.stop_window(
            self.user["id"], task["id"], "affinity-window-a"
        )
        result = await manager.delete_window(
            self.user["id"], task["id"], "affinity-window-a",
            target_id=claimed["id"],
        )

        self.assertEqual(claimed["id"], result["target_id"])
        self.assertEqual(
            ["affinity-window-b"],
            result["requeued_candidate"]["allowed_window_ids"],
        )
        refreshed_target = next(
            item
            for item in self.service.get_task(self.user["id"], task["id"])["targets"]
            if item["id"] == claimed["id"]
        )
        self.assertEqual("stopped", refreshed_target["status"])
        self.assertIsNone(refreshed_target["current_window_id"])
        self.assertIsNone(refreshed_target["preferred_window_id"])
        waiting = next(
            item
            for item in self.service.list_split_candidates(self.user["id"])
            if item["username"] == "affine_delete_target"
        )
        self.assertEqual("queued", waiting["queue_state"])
        self.assertEqual(
            ["affinity-window-b"],
            waiting["allowed_window_ids"],
        )

    async def test_live_window_delete_return_reclaims_same_target_id_on_sibling(self) -> None:
        class DeleteReturnWorker(FakeCollectionWorker):
            started_on_a: asyncio.Event | None = None
            release_a: asyncio.Event | None = None
            started_on_b: asyncio.Event | None = None
            release_b: asyncio.Event | None = None
            collection_calls: list[tuple[str, str]] = []

            async def collect_followers(
                self, target: str, *, limit: int
            ) -> CollectionOutcome:
                type(self).collection_calls.append((self.profile_id, target))
                if target == "sibling_busy_source":
                    assert type(self).started_on_b is not None
                    assert type(self).release_b is not None
                    type(self).started_on_b.set()
                    await type(self).release_b.wait()
                if self.profile_id == "delete-return-window-a":
                    assert type(self).started_on_a is not None
                    assert type(self).release_a is not None
                    type(self).started_on_a.set()
                    await type(self).release_a.wait()
                    # Model an in-flight browser operation unwinding after the
                    # window-local stop signal.  The runtime must first converge
                    # this target to recoverable before delete-and-return can
                    # publish the durable split generation.
                    raise WorkerExecutionError(
                        "window stopped during collection",
                        reason="instagram_network_unavailable",
                        pause_required=True,
                        status_code=503,
                    )
                return await super().collect_followers(target, limit=limit)

        DeleteReturnWorker.started_on_a = asyncio.Event()
        DeleteReturnWorker.release_a = asyncio.Event()
        DeleteReturnWorker.started_on_b = asyncio.Event()
        DeleteReturnWorker.release_b = asyncio.Event()
        DeleteReturnWorker.collection_calls = []
        task = self.service.create_task(
            self.user["id"],
            name="删除窗口后同目标异窗口重领",
            modes=["followers"],
            targets=["delete_return_source", "sibling_busy_source"],
            window_ids=[
                "delete-return-window-a",
                "delete-return-window-b",
            ],
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
            assignment_mode="manual",
        )
        target_id = task["targets"][0]["id"]
        self.service.set_manual_assignments(
            self.user["id"],
            task["id"],
            {"delete-return-window-a": "delete_return_source",
             "delete-return-window-b": "sibling_busy_source"},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=DeleteReturnWorker,
        )
        await manager.start(self.user["id"], task["id"])
        assert DeleteReturnWorker.started_on_a is not None
        await asyncio.wait_for(DeleteReturnWorker.started_on_a.wait(), timeout=2)
        running = self.service.get_task(self.user["id"], task["id"])["targets"][0]
        self.assertEqual(target_id, running["id"])
        self.assertEqual("running", running["status"])
        self.assertEqual("delete-return-window-a", running["current_window_id"])

        assert DeleteReturnWorker.started_on_b is not None
        await asyncio.wait_for(DeleteReturnWorker.started_on_b.wait(), timeout=2)

        delete_request = asyncio.create_task(
            manager.delete_window(
                self.user["id"],
                task["id"],
                "delete-return-window-a",
                target_id=target_id,
            )
        )
        for _ in range(100):
            local_stop = manager._runs[task["id"]].profile_stop_events.get(
                "delete-return-window-a"
            )
            if local_stop is not None and local_stop.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            delete_request.cancel()
            await asyncio.gather(delete_request, return_exceptions=True)
            self.fail("Window A did not receive its local stop signal")
        assert DeleteReturnWorker.release_a is not None
        DeleteReturnWorker.release_a.set()
        deleted = await asyncio.wait_for(delete_request, timeout=2)
        self.assertEqual(target_id, deleted["target_id"])
        self.assertEqual(1, deleted["remaining_window_count"])
        self.assertEqual(target_id, deleted["requeued_candidate"]["source_target_id"])
        assert DeleteReturnWorker.release_b is not None
        DeleteReturnWorker.release_b.set()

        for _ in range(200):
            current = self.service.get_task(self.user["id"], task["id"])
            reclaimed = next(
                item for item in current["targets"] if item["id"] == target_id
            )
            if reclaimed["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            active_control = manager._runs.get(task["id"])
            self.fail(
                "The returned target was not reclaimed by window B: "
                f"target={reclaimed!r}, "
                f"candidates={self.service.list_split_candidates(self.user['id'])!r}, "
                f"profile_states={getattr(active_control, 'profile_states', None)!r}, "
                f"calls={DeleteReturnWorker.collection_calls!r}"
            )
        self.assertEqual(target_id, reclaimed["id"])
        self.assertEqual("delete-return-window-b", reclaimed["current_window_id"])
        self.assertIn(
            ("delete-return-window-b", "delete_return_source"),
            DeleteReturnWorker.collection_calls,
        )
        await manager.close_task(self.user["id"], task["id"])

    async def test_live_window_delete_rejects_a_target_owned_by_another_window(self) -> None:
        AllTargetsBlockingWorker.expected_profiles = 2
        AllTargetsBlockingWorker.started_profiles = set()
        AllTargetsBlockingWorker.all_started = asyncio.Event()
        AllTargetsBlockingWorker.release = asyncio.Event()
        profiles = ["stale-window-a", "stale-window-b"]
        task = self.service.create_task(
            self.user["id"], name="拒绝陈旧窗口目标", modes=["followers"],
            targets=["stale_target", "sibling_target"], window_ids=profiles,
            settings={
                "live_queue_enabled": True,
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=AllTargetsBlockingWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(AllTargetsBlockingWorker.all_started.wait(), timeout=2)
        current = self.service.get_task(self.user["id"], task["id"])
        target = current["targets"][0]
        self.assertEqual("running", target["status"])
        owning_window = str(target["current_window_id"])
        wrong_window = next(profile for profile in profiles if profile != owning_window)

        with self.assertRaises(ConflictError):
            await manager.delete_window(
                self.user["id"], task["id"], wrong_window,
                target_id=target["id"],
            )
        self.assertEqual(
            profiles,
            self.service.get_task(self.user["id"], task["id"])["window_ids"],
        )
        control = manager._runs[task["id"]]
        self.assertFalse(control.profile_stop_events[wrong_window].is_set())
        await manager.stop(self.user["id"], task["id"])

    async def test_resume_window_rebuilds_missing_execution_from_checkpoint(self) -> None:
        from unittest.mock import AsyncMock

        task = self.service.create_task(
            self.user["id"], name="重启后可恢复", modes=["followers"],
            targets=["recoverable_source"], window_ids=["recoverable-window"], settings={},
        )
        target_id = task["targets"][0]["id"]
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target_id, "recoverable",
            window_id="recoverable-window"
        )
        self.service.finalize_task_runtime_status(
            self.user["id"], task["id"], "recoverable", error="应用在采集中断"
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient())
        manager.start = AsyncMock(return_value={})

        result = await manager.resume_window(
            self.user["id"], task["id"], "recoverable-window"
        )

        self.assertTrue(result["restored_from_checkpoint"])
        manager.start.assert_awaited_once_with(self.user["id"], task["id"], profile_ids=["recoverable-window"])
        refreshed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("pending", refreshed["targets"][0]["status"])
        self.assertFalse(refreshed["targets"][0]["manual_recovery_required"])

    async def test_collection_active_entity_is_atomic_during_lease_startup(self) -> None:
        import threading

        task = self.service.create_task(
            self.user["id"],
            name="采集租约启动原子性",
            modes=["followers"],
            targets=["atomic_collection_source"],
            window_ids=["atomic-collection-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=FakeCollectionWorker,
        )
        original_acquire = self.service.acquire_browser_lease
        lease_acquired = threading.Event()
        allow_publish = threading.Event()
        observed: dict[str, Any] = {}

        def blocked_acquire(*args, **kwargs):
            token = original_acquire(*args, **kwargs)
            lease_acquired.set()
            if not allow_publish.wait(2):
                raise RuntimeError("startup observer did not release lease publication")
            return token

        def reconcile_during_startup() -> None:
            if not lease_acquired.wait(2):
                observed["error"] = "lease was not acquired"
                allow_publish.set()
                return
            active_ids = manager.active_task_ids()
            observed["active_ids"] = active_ids
            observed["leases"] = self.service.list_browser_lease_states(
                self.user["id"],
                active_collection_entity_ids=active_ids,
                active_action_entity_ids=set(),
                inactive_grace_seconds=0,
            )
            allow_publish.set()

        self.service.acquire_browser_lease = blocked_acquire
        observer = threading.Thread(target=reconcile_during_startup)
        observer.start()
        try:
            await manager.start(self.user["id"], task["id"])
        finally:
            allow_publish.set()
            self.service.acquire_browser_lease = original_acquire
            observer.join(timeout=2)
        self.assertNotIn("error", observed)
        self.assertIn(task["id"], observed["active_ids"])
        self.assertEqual(
            ["atomic-collection-window"],
            [item["profile_id"] for item in observed["leases"]],
        )
        await manager.wait(task["id"])
        self.assertNotIn(task["id"], manager.active_task_ids())

    async def test_action_active_entity_is_atomic_during_lease_startup(self) -> None:
        import threading

        campaign = self.service.create_action_campaign(
            self.user["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="atomic-action-window",
            targets=["atomic_action_target"],
            message=None,
            interval_min_seconds=1,
            interval_max_seconds=1,
            limit_count=1,
        )
        manager = ActionCampaignManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=FakeActionWorker,
        )
        original_acquire = self.service.acquire_browser_lease
        lease_acquired = threading.Event()
        allow_publish = threading.Event()
        observed: dict[str, Any] = {}

        def blocked_acquire(*args, **kwargs):
            token = original_acquire(*args, **kwargs)
            lease_acquired.set()
            if not allow_publish.wait(2):
                raise RuntimeError("action observer did not release lease publication")
            return token

        def reconcile_during_startup() -> None:
            if not lease_acquired.wait(2):
                observed["error"] = "lease was not acquired"
                allow_publish.set()
                return
            active_ids = manager.active_campaign_ids()
            observed["active_ids"] = active_ids
            observed["leases"] = self.service.list_browser_lease_states(
                self.user["id"],
                active_collection_entity_ids=set(),
                active_action_entity_ids=active_ids,
                inactive_grace_seconds=0,
            )
            allow_publish.set()

        self.service.acquire_browser_lease = blocked_acquire
        observer = threading.Thread(target=reconcile_during_startup)
        observer.start()
        try:
            await manager._start_persisted(self.user["id"], campaign["id"])
        finally:
            allow_publish.set()
            self.service.acquire_browser_lease = original_acquire
            observer.join(timeout=2)
        self.assertNotIn("error", observed)
        self.assertIn(campaign["id"], observed["active_ids"])
        self.assertEqual(
            ["atomic-action-window"],
            [item["profile_id"] for item in observed["leases"]],
        )
        await manager.wait(campaign["id"])
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())

    async def test_multi_window_execution_persists_screened_results_and_checkpoints(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        FakeCollectionWorker.activity_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers", "following"],
            targets=["source_one", "source_two"],
            window_ids=["window-1", "window-2"],
            settings={
                "mode_limits": {
                    "followers": {
                        "followers_max": 500,
                        "following_max": 500,
                        "posts_max": 100,
                        "active_days_max": 30,
                        "per_target_limit": 10,
                    },
                    "following": {
                        "followers_max": 500,
                        "following_max": 500,
                        "posts_max": 100,
                        "active_days_max": 30,
                        "per_target_limit": 10,
                    },
                }
            },
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=FakeCollectionWorker)
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertTrue(all(target["status"] == "completed" for target in completed["targets"]))
        self.assertEqual(4, len(self.service.list_results(self.user["id"], task["id"])))
        self.assertEqual(4, len(self.service.list_checkpoints(self.user["id"], task["id"])))
        history = self.service.list_history(self.user["id"])
        self.assertEqual(2, len(history))
        self.assertEqual(2, len({row["target_id"] for row in history}))
        self.assertTrue(all(row["task_id"] == task["id"] for row in history))
        self.assertTrue(all(row["checkpoint"] for row in history))
        self.assertEqual({"window-1", "window-2"}, set(FakeCollectionWorker.connected_profiles))
        self.assertTrue(any(include_activity for _, include_activity in FakeCollectionWorker.activity_reads))
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM browser_operation_leases").fetchone()[0])

    async def test_pause_resume_keeps_original_task_and_completed_mode_checkpoint(self) -> None:
        PauseBetweenModesCollectionWorker.profile_read_started = asyncio.Event()
        PauseBetweenModesCollectionWorker.profile_read_release = asyncio.Event()
        PauseBetweenModesCollectionWorker.profile_read_release.set()
        PauseBetweenModesCollectionWorker.follower_calls = 0
        PauseBetweenModesCollectionWorker.following_calls = 0
        followers_finished = asyncio.Event()
        publish_mode_completion = asyncio.Event()

        class PauseAtCompletedModeBoundary(ExecutionManager):
            async def _execute_candidate_spooled_mode(self, control, worker, target, mode, settings, checkpoint, **_kwargs):
                progress = await super()._execute_candidate_spooled_mode(
                    control, worker, target, mode, settings, checkpoint,
                )
                if mode == "followers":
                    # Pause after real candidate processing commits, before the
                    # outer coordinator publishes mode_completed. Pausing at the
                    # first profile read would correctly leave this mode unfinished.
                    followers_finished.set()
                    await publish_mode_completion.wait()
                return progress

        task = self.service.create_task(
            self.user["id"],
            name="暂停后接续采集",
            modes=["followers", "following"],
            targets=["pause_resume_source"],
            window_ids=["pause-resume-window"],
            settings={
                "mode_limits": {
                    "followers": {"per_target_limit": 1},
                    "following": {"per_target_limit": 1},
                }
            },
        )
        manager = PauseAtCompletedModeBoundary(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PauseBetweenModesCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(
            followers_finished.wait(), timeout=5
        )
        paused = await manager.pause(self.user["id"], task["id"])
        self.assertEqual("paused", paused["status"])
        publish_mode_completion.set()

        for _ in range(100):
            checkpoint = self.service.get_checkpoint(
                self.user["id"], task["id"], task["targets"][0]["id"], "followers"
            )
            if checkpoint and checkpoint["stage"] == "mode_completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("mode_completed", checkpoint["stage"])
        self.assertEqual(0, PauseBetweenModesCollectionWorker.following_calls)

        resumed = await manager.resume(self.user["id"], task["id"])
        self.assertEqual(task["id"], resumed["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, PauseBetweenModesCollectionWorker.follower_calls)
        self.assertEqual(1, PauseBetweenModesCollectionWorker.following_calls)
        self.assertEqual(1, len(self.service.list_tasks(self.user["id"])))

    async def test_resume_waits_for_fatal_teardown_then_restarts_original_task(self) -> None:
        MultiModeRecoverOnceWorker.follower_calls = 0
        MultiModeRecoverOnceWorker.following_calls = 0
        MultiModeRecoverOnceWorker.following_failures_remaining = 1
        task = self.service.create_task(
            self.user["id"],
            name="异常退出竞态续采",
            modes=["followers", "following"],
            targets=["teardown_resume_source"],
            window_ids=["teardown-resume-window"],
            settings={
                "mode_limits": {
                    "followers": {"per_target_limit": 1},
                    "following": {"per_target_limit": 1},
                }
            },
        )
        manager = TeardownGateExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MultiModeRecoverOnceWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.closing.wait(), timeout=5)
        self.assertEqual(
            "recoverable", self.service.get_task(self.user["id"], task["id"])["status"]
        )

        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])
        resume_call = asyncio.create_task(manager.resume(self.user["id"], task["id"]))
        await asyncio.sleep(0)
        self.assertFalse(resume_call.done())
        manager.allow_close.set()
        resumed = await asyncio.wait_for(resume_call, timeout=5)
        self.assertEqual(task["id"], resumed["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        # Followers finished before the interruption and must not run twice.
        self.assertEqual(1, MultiModeRecoverOnceWorker.follower_calls)
        self.assertEqual(2, MultiModeRecoverOnceWorker.following_calls)
        self.assertEqual(1, len(self.service.list_tasks(self.user["id"])))

    async def test_retry_target_waits_for_worker_unwind_and_starts_a_real_replacement(self) -> None:
        MultiModeRecoverOnceWorker.follower_calls = 0
        MultiModeRecoverOnceWorker.following_calls = 0
        MultiModeRecoverOnceWorker.following_failures_remaining = 1
        task = self.service.create_task(
            self.user["id"],
            name="失败目标清理中重新采集",
            modes=["followers", "following"],
            targets=["retry_unwind"],
            window_ids=["retry-during-teardown-window"],
            settings={
                "mode_limits": {
                    "followers": {"per_target_limit": 1},
                    "following": {"per_target_limit": 1},
                }
            },
        )
        manager = TeardownGateExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MultiModeRecoverOnceWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.closing.wait(), timeout=5)
        target_id = task["targets"][0]["id"]
        self.assertEqual(
            "recoverable",
            self.service.get_task(self.user["id"], task["id"])["targets"][0][
                "status"
            ],
        )

        retry_call = asyncio.create_task(
            manager.retry_target(self.user["id"], task["id"], target_id)
        )
        await asyncio.sleep(0)
        self.assertFalse(retry_call.done())
        manager.allow_close.set()
        retried = await asyncio.wait_for(retry_call, timeout=5)
        self.assertEqual(task["id"], retried["task"]["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["targets"][0]["status"])
        self.assertEqual(1, MultiModeRecoverOnceWorker.follower_calls)
        self.assertEqual(2, MultiModeRecoverOnceWorker.following_calls)

    async def test_live_queue_resume_requeues_recoverable_target(self) -> None:
        LiveQueueIncompleteOnceWorker.attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="运行队列内目标续采",
            modes=["followers"],
            targets=["live_recoverable_source"],
            window_ids=["live-recoverable-window"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=LiveQueueIncompleteOnceWorker,
            # A source-specific unavailable outcome enters the operator's inbox.
            # Transient list-loading failures now continue automatically and do
            # not exercise this explicit requeue path.
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            if current["targets"][0]["status"] == "recoverable":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("running", current["status"])
        self.assertEqual("recoverable", current["targets"][0]["status"])
        interrupted_checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], task["targets"][0]["id"], "followers"
        )
        self.assertEqual("mode_unavailable", interrupted_checkpoint["stage"])

        self.assertEqual(1, LiveQueueIncompleteOnceWorker.attempts)
        original_target_id = task["targets"][0]["id"]
        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])
        await manager.resume(self.user["id"], task["id"])
        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            if current["targets"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("completed", current["targets"][0]["status"])
        self.assertEqual(original_target_id, current["targets"][0]["id"])
        self.assertEqual(2, LiveQueueIncompleteOnceWorker.attempts)
        self.assertEqual(1, len(self.service.list_results(self.user["id"], task["id"])))
        await manager.stop(self.user["id"], task["id"], close_windows=False)

    async def test_stopped_target_resumes_original_task_from_preserved_checkpoint(self) -> None:
        PauseBetweenModesCollectionWorker.profile_read_started = asyncio.Event()
        PauseBetweenModesCollectionWorker.profile_read_release = asyncio.Event()
        PauseBetweenModesCollectionWorker.follower_calls = 0
        PauseBetweenModesCollectionWorker.following_calls = 0
        task = self.service.create_task(
            self.user["id"],
            name="停止后原任务续采",
            modes=["followers"],
            targets=["stopped_resume_source"],
            window_ids=["stopped-resume-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        target_id = task["targets"][0]["id"]
        checkpoint = self.service.upsert_checkpoint(
            self.user["id"],
            task["id"],
            target_id,
            mode="followers",
            stage="mode_started",
            cursor={"resume_from": "visible_list"},
            counters={"saved": 0},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PauseBetweenModesCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(
            PauseBetweenModesCollectionWorker.profile_read_started.wait(), timeout=5
        )
        stopped = await manager.stop(
            self.user["id"], task["id"], close_windows=False
        )
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual(
            "recoverable",
            self.service.get_task(self.user["id"], task["id"])["targets"][0]["status"],
        )
        self.assertEqual(
            checkpoint["id"],
            self.service.get_checkpoint(
                self.user["id"], task["id"], target_id, "followers"
            )["id"],
        )

        PauseBetweenModesCollectionWorker.profile_read_release.set()
        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])
        resumed = await manager.resume(self.user["id"], task["id"])
        self.assertEqual(task["id"], resumed["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["targets"][0]["status"])
        self.assertEqual(
            checkpoint["id"],
            self.service.get_checkpoint(
                self.user["id"], task["id"], target_id, "followers"
            )["id"],
        )
        self.assertEqual(1, len(self.service.list_tasks(self.user["id"])))

    async def test_failed_task_resumes_original_task_without_clearing_checkpoint(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="失败后原任务续采",
            modes=["followers"],
            targets=["failed_resume_source"],
            window_ids=["failed-resume-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        target_id = task["targets"][0]["id"]
        checkpoint = self.service.upsert_checkpoint(
            self.user["id"],
            task["id"],
            target_id,
            mode="followers",
            stage="mode_started",
            cursor={"resume_from": "visible_list"},
            counters={"saved": 0},
        )
        self.service.set_target_runtime_status(
            self.user["id"], task["id"], target_id, "recoverable"
        )
        self.service.set_task_runtime_status(
            self.user["id"], task["id"], "failed", error="unexpected worker exit"
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=FakeCollectionWorker
        )

        failure = self.service.list_split_candidates(self.user["id"])[0]
        self.service.requeue_split_candidate(self.user["id"], failure["id"])
        resumed = await manager.resume(self.user["id"], task["id"])
        self.assertEqual(task["id"], resumed["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["targets"][0]["status"])
        self.assertEqual(
            checkpoint["id"],
            self.service.get_checkpoint(
                self.user["id"], task["id"], target_id, "followers"
            )["id"],
        )
        self.assertEqual(1, len(self.service.list_tasks(self.user["id"])))

    async def test_network_outage_waits_indefinitely_and_resumes_same_target(self) -> None:
        NetworkRecoveryCollectionWorker.network_available = asyncio.Event()
        NetworkRecoveryCollectionWorker.first_connection_done = False
        NetworkRecoveryCollectionWorker.collection_attempts = 0
        NetworkRecoveryCollectionWorker.reconnect_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="断网恢复采集",
            modes=["followers"],
            targets=["network_source"],
            window_ids=["network-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=NetworkRecoveryCollectionWorker,
            network_retry_delays=(60,),
        )
        await manager.start(self.user["id"], task["id"])

        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            if current["status"] == "waiting_network":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("waiting_network", current["status"])
        self.assertEqual("waiting_network", current["targets"][0]["status"])
        self.assertEqual([], self.service.list_results(self.user["id"], task["id"]))
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], current["targets"][0]["id"], "followers"
        )
        self.assertEqual("waiting_network", checkpoint["stage"])

        diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertTrue(diagnostics["active"])
        self.assertTrue(diagnostics["waiting_for_network"])
        self.assertEqual(1, diagnostics["network_waiting_window_count"])
        self.assertEqual("network_source", diagnostics["network_waiters"][0]["target"])
        self.assertIsNotNone(diagnostics["network_waiters"][0]["next_retry_at"])
        profile_state = diagnostics["profile_states"][0]
        self.assertEqual("followers", profile_state["current_mode"])
        self.assertEqual("waiting_network", profile_state["current_stage"])

        # A user-triggered probe does not lose the target if the connection is still
        # down. The next probe can be triggered immediately after connectivity returns.
        await manager.retry_network_now(self.user["id"], task["id"])
        for _ in range(100):
            if NetworkRecoveryCollectionWorker.reconnect_attempts >= 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual("waiting_network", self.service.get_task(self.user["id"], task["id"])["status"])

        NetworkRecoveryCollectionWorker.network_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["targets"][0]["status"])
        self.assertEqual(2, NetworkRecoveryCollectionWorker.collection_attempts)
        self.assertEqual(1, len(self.service.list_results(self.user["id"], task["id"])))
        final_checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], completed["targets"][0]["id"], "followers"
        )
        self.assertEqual("mode_completed", final_checkpoint["stage"])

    async def test_instagram_page_retry_preserves_healthy_browser_connection(self) -> None:
        InstagramSurfaceRetryWorker.attempts = 0
        InstagramSurfaceRetryWorker.connect_calls = 0
        InstagramSurfaceRetryWorker.disconnect_calls = 0
        InstagramSurfaceRetryWorker.first_failure = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="Instagram 页面原地恢复",
            modes=["followers"],
            targets=["surface_retry_source"],
            window_ids=["surface-retry-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=InstagramSurfaceRetryWorker,
            network_retry_delays=(0.05,),
            network_retry_stagger_seconds=0,
        )
        await manager.start(self.user["id"], task["id"])
        assert InstagramSurfaceRetryWorker.first_failure is not None
        await asyncio.wait_for(
            InstagramSurfaceRetryWorker.first_failure.wait(), timeout=10
        )
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(2, InstagramSurfaceRetryWorker.attempts)
        self.assertEqual(1, InstagramSurfaceRetryWorker.connect_calls)
        # One disconnect is the normal final task teardown. There was no reconnect
        # between the first page failure and the successful second collection read.
        self.assertEqual(1, InstagramSurfaceRetryWorker.disconnect_calls)

        for reason in (
            "instagram_profile_not_ready",
            "instagram_profile_wrong_target",
            "instagram_profile_temporarily_unavailable",
            "instagram_location_temporarily_unavailable",
        ):
            with self.subTest(reason=reason):
                error = WorkerExecutionError(
                    "Instagram page is not ready",
                    reason=reason,
                    pause_required=True,
                    status_code=503,
                )
                self.assertTrue(
                    ExecutionManager._is_instagram_surface_retry_error(error)
                )

    async def test_manual_pause_freezes_network_probes_until_resume(self) -> None:
        NetworkRecoveryCollectionWorker.network_available = asyncio.Event()
        NetworkRecoveryCollectionWorker.first_connection_done = False
        NetworkRecoveryCollectionWorker.collection_attempts = 0
        NetworkRecoveryCollectionWorker.reconnect_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="暂停断网恢复",
            modes=["followers"],
            targets=["paused_network_source"],
            window_ids=["paused-network-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=NetworkRecoveryCollectionWorker,
            network_retry_delays=(60,),
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            if self.service.get_task(self.user["id"], task["id"])["status"] == "waiting_network":
                break
            await asyncio.sleep(0.01)
        await manager.pause(self.user["id"], task["id"])
        NetworkRecoveryCollectionWorker.network_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.sleep(0.05)
        self.assertEqual(0, NetworkRecoveryCollectionWorker.reconnect_attempts)
        self.assertEqual("paused", self.service.get_task(self.user["id"], task["id"])["status"])

        await manager.resume(self.user["id"], task["id"])
        # retry-now fired while paused is intentionally retained by the per-window
        # event, so resume probes immediately rather than waiting the full backoff.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        self.assertEqual("completed", self.service.get_task(self.user["id"], task["id"])["status"])

    async def test_resume_wakes_ordinary_network_backoff_without_retry_now(self) -> None:
        NetworkRecoveryCollectionWorker.network_available = asyncio.Event()
        NetworkRecoveryCollectionWorker.first_connection_done = False
        NetworkRecoveryCollectionWorker.collection_attempts = 0
        NetworkRecoveryCollectionWorker.reconnect_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="普通网络等待直接继续",
            modes=["followers"],
            targets=["resume_network_source"],
            window_ids=["resume-network-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=NetworkRecoveryCollectionWorker,
            network_retry_delays=(60,),
            expected_waiters=1,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)

        NetworkRecoveryCollectionWorker.network_available.set()
        await manager.resume(self.user["id"], task["id"])
        # Resume has already been observed above; this guard only detects a stuck
        # coordinator and must tolerate a busy Windows debug event loop.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)

        self.assertEqual(1, NetworkRecoveryCollectionWorker.reconnect_attempts)
        self.assertEqual(
            "completed", self.service.get_task(self.user["id"], task["id"])["status"]
        )

    async def test_conclusive_mode_unavailable_clears_recovery_generation(self) -> None:
        NetworkThenUnavailableWorker.attempts = 0
        NetworkThenUnavailableWorker.reconnected = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="网络恢复后列表不可用",
            modes=["followers"],
            targets=["unavailable_after_network"],
            window_ids=["generation-window"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=NetworkThenUnavailableWorker,
            network_retry_delays=(60,),
            expected_waiters=1,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)
        first = await manager.runtime_diagnostics(self.user["id"], task["id"])
        generation = first["network_waiters"][0]["generation"]

        await manager.retry_network_now(self.user["id"], task["id"])
        for _ in range(100):
            stored = self.service.get_task(self.user["id"], task["id"])
            if stored["targets"][0]["status"] == "recoverable":
                break
            await asyncio.sleep(0.01)

        diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertEqual("running", diagnostics["status"])
        self.assertEqual(0, diagnostics["network_waiting_window_count"])
        self.assertEqual(1, diagnostics["active_window_count"])
        self.assertEqual([], diagnostics["network_waiters"])
        self.assertEqual("degraded", diagnostics["profile_states"][0]["state"])
        self.assertNotEqual(
            generation, diagnostics["profile_states"][0].get("generation")
        )
        control = manager._runs[task["id"]]
        self.assertNotIn("generation-window", control.retry_network_events)
        self.assertNotIn("generation-window", control.network_waiters)
        await manager.stop(self.user["id"], task["id"], close_windows=False)

    async def test_instagram_challenge_isolates_only_its_profile(self) -> None:
        healthy_busy = asyncio.Event()
        healthy_release = asyncio.Event()
        class BusyHealthyWorker(ProfileInterventionIsolationWorker):
            async def collect_followers(self, target, *, limit):
                if target == "healthy_busy_source":
                    healthy_busy.set()
                    await healthy_release.wait()
                return await super().collect_followers(target, limit=limit)

        ProfileInterventionIsolationWorker.intervention_cleared = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="单窗口 Instagram 验证隔离",
            modes=["followers"],
            targets=["challenged_source", "healthy_source", "healthy_busy_source"],
            window_ids=["challenge-window", "healthy-window"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=BusyHealthyWorker,
            network_retry_delays=(0,),
            expected_waiters=1,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)
        await asyncio.wait_for(healthy_busy.wait(), timeout=2)
        for _ in range(100):
            stored = self.service.get_task(self.user["id"], task["id"])
            if any(item["status"] == "completed" for item in stored["targets"]):
                break
            await asyncio.sleep(0.01)

        diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertEqual("running", diagnostics["status"])
        self.assertEqual(1, diagnostics["network_waiting_window_count"])
        self.assertEqual(1, diagnostics["active_window_count"])
        waiter = diagnostics["network_waiters"][0]
        self.assertEqual("challenge-window", waiter["profile_id"])
        self.assertEqual("manual_intervention", waiter["state"])
        self.assertEqual("instagram_challenge", waiter["reason"])
        self.assertIsNone(waiter["next_retry_at"])
        by_profile = {item["profile_id"]: item for item in diagnostics["profile_states"]}
        self.assertEqual("manual_intervention", by_profile["challenge-window"]["state"])
        self.assertEqual("followers", by_profile["challenge-window"]["current_mode"])
        self.assertEqual("manual_required", by_profile["challenge-window"]["current_stage"])
        self.assertIn(by_profile["healthy-window"]["state"], {"idle", "working"})
        self.assertEqual(1, sum(item["status"] == "completed" for item in stored["targets"]))

        assert ProfileInterventionIsolationWorker.intervention_cleared is not None
        healthy_release.set()
        ProfileInterventionIsolationWorker.intervention_cleared.set()
        await manager.resume(self.user["id"], task["id"])
        for _ in range(100):
            stored = self.service.get_task(self.user["id"], task["id"])
            if all(item["status"] == "completed" for item in stored["targets"]):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(all(item["status"] == "completed" for item in stored["targets"]))
        recovered = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertEqual(0, recovered["network_waiting_window_count"])
        await manager.stop(self.user["id"], task["id"], close_windows=False)

    def test_incomplete_relationship_lists_are_retryable_per_window(self) -> None:
        for reason in (
            "instagram_followers_list_incomplete",
            "instagram_following_list_incomplete",
        ):
            with self.subTest(reason=reason):
                error = WorkerExecutionError(
                    "Relationship list ended before the requested amount was read",
                    reason=reason,
                    pause_required=True,
                    status_code=503,
                )
                self.assertFalse(ExecutionManager._is_network_error(error))
                self.assertTrue(
                    ExecutionManager._is_relationship_list_incomplete_error(error)
                )

    async def test_incomplete_relationship_list_retries_without_network_reconnect(self) -> None:
        RelationshipListIncompleteOnceWorker.attempts = 0
        RelationshipListIncompleteOnceWorker.connect_calls = 0
        RelationshipListIncompleteOnceWorker.disconnect_calls = 0
        RelationshipListIncompleteOnceWorker.stalled = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="列表加载重试",
            modes=["followers"],
            targets=["list_retry_source"],
            window_ids=["list-retry-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=RelationshipListIncompleteOnceWorker,
            network_retry_delays=(0.2,),
        )
        last_success_before = task["targets"][0].get("last_success_at")
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(
            RelationshipListIncompleteOnceWorker.stalled.wait(), timeout=2
        )
        for _ in range(100):
            stored = self.service.get_task(self.user["id"], task["id"])
            checkpoint = self.service.get_checkpoint(
                self.user["id"], task["id"], stored["targets"][0]["id"], "followers"
            )
            if checkpoint and checkpoint["stage"] == "collecting_list":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("running", stored["status"])
        self.assertEqual("running", stored["targets"][0]["status"])
        self.assertEqual("collecting_list", checkpoint["stage"])
        self.assertEqual(last_success_before, stored["targets"][0]["last_success_at"])
        diagnostics = await manager.runtime_diagnostics(
            self.user["id"], task["id"]
        )
        self.assertEqual([], diagnostics["network_waiters"])
        self.assertEqual(1, RelationshipListIncompleteOnceWorker.connect_calls)
        self.assertEqual(0, RelationshipListIncompleteOnceWorker.disconnect_calls)

        for _ in range(200):
            stored = self.service.get_task(self.user["id"], task["id"])
            if stored["targets"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("completed", stored["targets"][0]["status"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=2)
        self.assertEqual(2, RelationshipListIncompleteOnceWorker.attempts)
        self.assertEqual(1, RelationshipListIncompleteOnceWorker.connect_calls)
        # The retry reused the connection; the sole disconnect is final drain.
        self.assertEqual(1, RelationshipListIncompleteOnceWorker.disconnect_calls)
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_incomplete_list_backoff_obeys_pause_and_stop(self) -> None:
        RelationshipListIncompleteOnceWorker.attempts = 0
        RelationshipListIncompleteOnceWorker.connect_calls = 0
        RelationshipListIncompleteOnceWorker.disconnect_calls = 0
        RelationshipListIncompleteOnceWorker.stalled = asyncio.Event()
        task = self.service.create_task(
            self.user["id"], name="列表暂停退避", modes=["followers"],
            targets=["list_pause_source"], window_ids=["list-pause-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(),
            worker_factory=RelationshipListIncompleteOnceWorker,
            network_retry_delays=(0.25,),
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(
            RelationshipListIncompleteOnceWorker.stalled.wait(), timeout=2
        )
        await manager.pause(self.user["id"], task["id"])
        await asyncio.sleep(0.35)
        self.assertEqual(1, RelationshipListIncompleteOnceWorker.attempts)
        self.assertEqual("paused", self.service.get_task(self.user["id"], task["id"])["status"])
        await manager.resume(self.user["id"], task["id"])
        for _ in range(100):
            stored = self.service.get_task(self.user["id"], task["id"])
            if stored["targets"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("completed", stored["targets"][0]["status"])
        await manager.stop(self.user["id"], task["id"], close_windows=False)

        RelationshipListIncompleteOnceWorker.attempts = 0
        RelationshipListIncompleteOnceWorker.stalled = asyncio.Event()
        stop_task = self.service.create_task(
            self.user["id"], name="列表停止退避", modes=["followers"],
            targets=["list_stop_source"], window_ids=["list-stop-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        stop_manager = ExecutionManager(
            self.service, FakeBitBrowserClient(),
            worker_factory=RelationshipListIncompleteOnceWorker,
            network_retry_delays=(60,),
        )
        await stop_manager.start(self.user["id"], stop_task["id"])
        await asyncio.wait_for(
            RelationshipListIncompleteOnceWorker.stalled.wait(), timeout=2
        )
        await asyncio.wait_for(
            stop_manager.stop(
                self.user["id"], stop_task["id"], close_windows=False
            ),
            timeout=1,
        )
        self.assertEqual(1, RelationshipListIncompleteOnceWorker.attempts)

    async def test_reconnect_gate_bounds_attempts_without_limiting_window_count(self) -> None:
        window_count = 7
        BoundedReconnectWorker.initially_connected = set()
        BoundedReconnectWorker.attempts_by_profile = {}
        BoundedReconnectWorker.reconnect_started = asyncio.Event()
        BoundedReconnectWorker.allow_reconnect = asyncio.Event()
        BoundedReconnectWorker.active_reconnects = 0
        BoundedReconnectWorker.max_active_reconnects = 0
        BoundedReconnectWorker.reconnect_profiles = set()
        windows = [f"bounded-window-{index}" for index in range(window_count)]
        task = self.service.create_task(
            self.user["id"],
            name="无限窗口有界重连",
            modes=["followers"],
            targets=[f"bounded_source_{index}" for index in range(window_count)],
            window_ids=windows,
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=BoundedReconnectWorker,
            network_retry_delays=(60,),
            network_retry_concurrency=2,
            network_retry_stagger_seconds=0.01,
            expected_waiters=window_count,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)
        self.assertEqual(window_count, len(manager._runs[task["id"]].started_profile_ids))

        await manager.retry_network_now(self.user["id"], task["id"])
        assert BoundedReconnectWorker.reconnect_started is not None
        await asyncio.wait_for(
            BoundedReconnectWorker.reconnect_started.wait(), timeout=10
        )
        for _ in range(100):
            if BoundedReconnectWorker.max_active_reconnects >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(2, BoundedReconnectWorker.max_active_reconnects)
        assert BoundedReconnectWorker.allow_reconnect is not None
        BoundedReconnectWorker.allow_reconnect.set()
        # Seven durable completions include serialized SQLite writes.  This is
        # a deadlock guard, not a three-second throughput requirement.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)

        self.assertEqual(window_count, len(BoundedReconnectWorker.reconnect_profiles))
        self.assertLessEqual(BoundedReconnectWorker.max_active_reconnects, 2)
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertTrue(all(item["status"] == "completed" for item in completed["targets"]))

    async def test_auth_required_waits_for_explicit_continue_and_keeps_checkpoint(self) -> None:
        AuthRequiredRecoveryWorker.auth_available = asyncio.Event()
        AuthRequiredRecoveryWorker.first_connection_done = False
        AuthRequiredRecoveryWorker.reconnect_attempted = asyncio.Event()
        AuthRequiredRecoveryWorker.reconnect_attempts = 0
        AuthRequiredRecoveryWorker.collection_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="BitBrowser 登录失效后续采",
            modes=["followers"],
            targets=["auth_checkpoint_source"],
            window_ids=["auth-required-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=AuthRequiredRecoveryWorker,
            # Zero delay makes this test fail immediately if auth_required is ever
            # misclassified as an automatically retried network outage.
            network_retry_delays=(0,),
            expected_waiters=1,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)

        diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertEqual("waiting_network", diagnostics["status"])
        self.assertEqual(1, diagnostics["network_waiting_window_count"])
        waiter = diagnostics["network_waiters"][0]
        self.assertEqual("auth_required", waiter["state"])
        self.assertEqual("auth_required", waiter["reason"])
        self.assertIsNone(waiter["next_retry_at"])
        self.assertIsNone(waiter["retry_delay_seconds"])
        self.assertIn(
            "重新登录",
            self.service.get_task(self.user["id"], task["id"])["last_error"],
        )
        self.assertEqual(
            ["auth-required-window"],
            [
                item["profile_id"]
                for item in self.service.list_browser_lease_states(self.user["id"])
            ],
        )
        waiting_checkpoint = self.service.get_checkpoint(
            self.user["id"],
            task["id"],
            task["targets"][0]["id"],
            "followers",
        )
        self.assertEqual("waiting_network", waiting_checkpoint["stage"])
        self.assertEqual("mode_started", waiting_checkpoint["cursor"]["resume_stage"])

        assert AuthRequiredRecoveryWorker.reconnect_attempted is not None
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                AuthRequiredRecoveryWorker.reconnect_attempted.wait(), timeout=0.05
            )
        self.assertEqual(0, AuthRequiredRecoveryWorker.reconnect_attempts)

        assert AuthRequiredRecoveryWorker.auth_available is not None
        AuthRequiredRecoveryWorker.auth_available.set()
        await manager.resume(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["targets"][0]["status"])
        final_checkpoint = self.service.get_checkpoint(
            self.user["id"],
            task["id"],
            task["targets"][0]["id"],
            "followers",
        )
        self.assertEqual(waiting_checkpoint["id"], final_checkpoint["id"])
        self.assertEqual("mode_completed", final_checkpoint["stage"])
        self.assertEqual(1, AuthRequiredRecoveryWorker.reconnect_attempts)
        self.assertEqual(2, AuthRequiredRecoveryWorker.collection_attempts)

    async def test_retry_network_now_wakes_every_waiting_window(self) -> None:
        MultiWindowNetworkRecoveryWorker.network_available = asyncio.Event()
        MultiWindowNetworkRecoveryWorker.initially_connected = set()
        MultiWindowNetworkRecoveryWorker.reconnect_profiles = []
        MultiWindowNetworkRecoveryWorker.collection_attempts = {}
        task = self.service.create_task(
            self.user["id"],
            name="多窗口断网恢复",
            modes=["followers"],
            targets=["offline_a", "offline_b"],
            window_ids=["network-a", "network-b"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MultiWindowNetworkRecoveryWorker,
            network_retry_delays=(60,),
            expected_waiters=2,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=10)
        diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertEqual(2, diagnostics["network_waiting_window_count"])
        self.assertEqual("waiting_network", diagnostics["status"])
        self.assertEqual(
            {"waiting_network"},
            {waiter["state"] for waiter in diagnostics["network_waiters"]},
        )

        MultiWindowNetworkRecoveryWorker.network_available.set()
        # Both the route and manager method run on the execution event loop so event
        # wakeups and the returned diagnostic snapshot are thread-safe.
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        self.assertEqual(
            {"network-a", "network-b"},
            set(MultiWindowNetworkRecoveryWorker.reconnect_profiles),
        )
        self.assertEqual("completed", self.service.get_task(self.user["id"], task["id"])["status"])

    async def test_runtime_diagnostics_stays_coherent_during_network_recovery(self) -> None:
        MultiWindowNetworkRecoveryWorker.network_available = asyncio.Event()
        MultiWindowNetworkRecoveryWorker.initially_connected = set()
        MultiWindowNetworkRecoveryWorker.reconnect_profiles = []
        MultiWindowNetworkRecoveryWorker.collection_attempts = {}
        task = self.service.create_task(
            self.user["id"],
            name="网络恢复诊断并发快照",
            modes=["followers"],
            targets=["diagnostic_offline_a", "diagnostic_offline_b"],
            window_ids=["diagnostic-window-a", "diagnostic-window-b"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = NetworkWaitObserverExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MultiWindowNetworkRecoveryWorker,
            network_retry_delays=(60,),
            expected_waiters=2,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(manager.waiters_ready.wait(), timeout=5)

        # Capture the gated two-waiter state before allowing recovery. A single
        # sleep(0) below cannot guarantee a poller's threaded task read finished.
        snapshots: list[dict[str, Any]] = [
            await manager.runtime_diagnostics(self.user["id"], task["id"])
        ]

        async def poll_runtime() -> None:
            for _ in range(40):
                snapshot = await manager.runtime_diagnostics(
                    self.user["id"], task["id"]
                )
                self.assertEqual(
                    snapshot["network_waiting_window_count"],
                    len(snapshot["network_waiters"]),
                )
                self.assertEqual(
                    snapshot["waiting_for_network"],
                    bool(snapshot["network_waiters"]),
                )
                profile_ids = [
                    waiter["profile_id"] for waiter in snapshot["network_waiters"]
                ]
                self.assertEqual(len(profile_ids), len(set(profile_ids)))
                self.assertGreaterEqual(snapshot["active_window_count"], 0)
                snapshots.append(snapshot)
                await asyncio.sleep(0)

        pollers = [asyncio.create_task(poll_runtime()) for _ in range(4)]
        await asyncio.sleep(0)
        MultiWindowNetworkRecoveryWorker.network_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)
        await asyncio.gather(*pollers)

        self.assertTrue(
            any(snapshot["network_waiting_window_count"] == 2 for snapshot in snapshots)
        )
        final = await manager.runtime_diagnostics(self.user["id"], task["id"])
        self.assertFalse(final["active"])
        self.assertEqual(0, final["network_waiting_window_count"])

    async def test_one_offline_window_does_not_block_an_online_window(self) -> None:
        PartialWindowNetworkWorker.network_available = asyncio.Event()
        PartialWindowNetworkWorker.online_release = asyncio.Event()
        PartialWindowNetworkWorker.initially_connected = set()
        PartialWindowNetworkWorker.reconnect_profiles = []
        PartialWindowNetworkWorker.collection_attempts = {}
        task = self.service.create_task(
            self.user["id"],
            name="部分窗口断网",
            modes=["followers"],
            targets=["offline_source", "online_source"],
            window_ids=["partial-a", "partial-b"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PartialWindowNetworkWorker,
            network_retry_delays=(60,),
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
            if diagnostics["network_waiting_window_count"] == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, diagnostics["network_waiting_window_count"])
        self.assertEqual(1, diagnostics["active_window_count"])
        self.assertEqual("running", diagnostics["status"])

        PartialWindowNetworkWorker.network_available.set()
        PartialWindowNetworkWorker.online_release.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        # Windows Server CI can need more than two seconds to drain both
        # coordinators after 600+ tests have exercised the same event loop.
        # Keep the completion assertion strict while allowing normal scheduler
        # jitter on slower hosted runners.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=5)
        self.assertEqual("completed", self.service.get_task(self.user["id"], task["id"])["status"])

    async def test_location_network_failure_waits_instead_of_saving_unknown(self) -> None:
        LocationNetworkRecoveryWorker.network_available = asyncio.Event()
        LocationNetworkRecoveryWorker.first_connection_done = False
        LocationNetworkRecoveryWorker.reconnect_attempts = 0
        LocationNetworkRecoveryWorker.location_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="所在地断网恢复",
            modes=["followers"],
            targets=["location_network_source"],
            window_ids=["location-network-window"],
            settings={
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=LocationNetworkRecoveryWorker,
            network_retry_delays=(60,),
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            if self.service.get_task(self.user["id"], task["id"])["status"] == "waiting_network":
                break
            await asyncio.sleep(0.01)
        self.assertEqual([], self.service.list_results(self.user["id"], task["id"]))

        LocationNetworkRecoveryWorker.network_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual("美国", result["profile"]["location_zh"])
        self.assertTrue(result["screening"]["location"]["checked"])
        self.assertEqual(2, LocationNetworkRecoveryWorker.location_attempts)

    async def test_healthy_cdp_does_not_reset_long_instagram_outage_backoff(self) -> None:
        CdpHealthyInstagramOfflineWorker.instagram_available = asyncio.Event()
        CdpHealthyInstagramOfflineWorker.collection_attempts = 0
        task = self.service.create_task(
            self.user["id"],
            name="CDP在线但Instagram断网",
            modes=["followers"],
            targets=["wan_offline_source"],
            window_ids=["cdp-online-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=CdpHealthyInstagramOfflineWorker,
            network_retry_delays=(0.01, 0.02, 60),
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(200):
            diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
            waiters = diagnostics["network_waiters"]
            if waiters and waiters[0]["retry_count"] >= 2:
                break
            await asyncio.sleep(0.005)
        self.assertGreaterEqual(waiters[0]["retry_count"], 2)
        self.assertEqual(60, waiters[0]["retry_delay_seconds"])
        self.assertGreaterEqual(CdpHealthyInstagramOfflineWorker.collection_attempts, 3)
        self.assertEqual([], self.service.list_results(self.user["id"], task["id"]))
        waiting_target = self.service.get_task(self.user["id"], task["id"])["targets"][0]
        waiting_checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], waiting_target["id"], "followers"
        )
        self.assertEqual("mode_started", waiting_checkpoint["cursor"]["resume_stage"])
        self.assertNotIn("resume_stage", waiting_checkpoint["cursor"]["resume_cursor"])

        CdpHealthyInstagramOfflineWorker.instagram_available.set()
        await manager.retry_network_now(self.user["id"], task["id"])
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        self.assertEqual("completed", self.service.get_task(self.user["id"], task["id"])["status"])
        self.assertEqual(1, len(self.service.list_results(self.user["id"], task["id"])))

    async def test_live_queue_accepts_new_targets_while_busy_and_drained_windows_close(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target"],
            window_ids=["live-window-a", "live-window-b", "live-window-c"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        bitbrowser = FakeBitBrowserClient()
        manager = ExecutionManager(
            self.service, bitbrowser, worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(BlockingQueueWorker.started.wait(), timeout=2)
        for _ in range(100):
            control = manager._runs[task["id"]]
            if len(FakeCollectionWorker.connected_profiles) == 3 and len(control.leases) == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, len(control.leases))
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("running", current["status"])
        self.assertEqual(
            {"live-window-a", "live-window-b", "live-window-c"},
            set(FakeCollectionWorker.connected_profiles),
        )

        added = await manager.add_targets(
            self.user["id"], task["id"], ["second_live_source"]
        )
        self.assertEqual(1, added["added"])
        BlockingQueueWorker.release.set()
        await asyncio.wait_for(manager.wait(task["id"]), timeout=2)
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", current["targets"][0]["status"])
        self.assertEqual("completed", current["targets"][1]["status"])
        self.assertEqual("completed", current["status"])
        closed_profiles = {
            payload.get("id") or payload.get("browserId")
            for path, payload in bitbrowser.calls
            if path == "/browser/close"
        }
        self.assertEqual(
            {"live-window-a", "live-window-b", "live-window-c"}, closed_profiles
        )
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_single_window_pause_resume_stop_delete_never_controls_siblings(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        AllTargetsBlockingWorker.expected_profiles = 3
        AllTargetsBlockingWorker.started_profiles = set()
        AllTargetsBlockingWorker.all_started = asyncio.Event()
        AllTargetsBlockingWorker.release = asyncio.Event()
        profiles = ["isolated-window-a", "isolated-window-b", "isolated-window-c"]
        task = self.service.create_task(
            self.user["id"],
            name="单窗口控制隔离",
            modes=["followers"],
            targets=["isolated_source_a", "isolated_source_b", "isolated_source_c"],
            window_ids=profiles,
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=AllTargetsBlockingWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(AllTargetsBlockingWorker.all_started.wait(), timeout=2)
        control = manager._runs.get(task["id"])
        assert control is not None

        paused = await manager.pause_window(
            self.user["id"], task["id"], "isolated-window-a"
        )
        self.assertEqual(1, paused["affected_window_count"])
        self.assertFalse(control.profile_pause_events["isolated-window-a"].is_set())
        self.assertTrue(control.profile_pause_events["isolated-window-b"].is_set())
        self.assertTrue(control.profile_pause_events["isolated-window-c"].is_set())
        self.assertTrue(control.pause_event.is_set())
        self.assertEqual(
            "running", self.service.get_task(self.user["id"], task["id"])["status"]
        )

        resumed = await manager.resume_window(
            self.user["id"], task["id"], "isolated-window-a"
        )
        self.assertEqual("running", resumed["status"])
        stopped = await manager.stop_window(
            self.user["id"], task["id"], "isolated-window-a"
        )
        self.assertEqual(1, stopped["affected_window_count"])
        self.assertFalse(control.stop_event.is_set())
        self.assertIn("isolated-window-b", control.started_profile_ids)
        self.assertIn("isolated-window-c", control.started_profile_ids)

        deleted = await manager.delete_window(
            self.user["id"], task["id"], "isolated-window-a"
        )
        self.assertEqual(1, deleted["affected_window_count"])
        self.assertEqual(2, deleted["remaining_window_count"])
        self.assertEqual(
            ["isolated-window-b", "isolated-window-c"],
            self.service.get_task(self.user["id"], task["id"])["window_ids"],
        )
        self.assertEqual(
            {"isolated-window-b", "isolated-window-c"}, set(control.leases)
        )
        replayed = await manager.delete_window(
            self.user["id"], task["id"], "isolated-window-a"
        )
        self.assertTrue(replayed["already_deleted"])
        self.assertEqual(0, replayed["affected_window_count"])
        self.assertEqual(2, replayed["remaining_window_count"])
        self.assertEqual(
            "running", self.service.get_task(self.user["id"], task["id"])["status"]
        )
        self.assertEqual(
            {"isolated-window-b", "isolated-window-c"}, set(control.leases)
        )
        self.assertEqual(
            {"isolated-window-b", "isolated-window-c"},
            {
                lease["profile_id"]
                for lease in self.service.list_browser_lease_states(self.user["id"])
            },
        )
        await manager.stop(self.user["id"], task["id"])

    async def test_candidate_profile_timeout_stays_pending_and_becomes_recoverable(self) -> None:
        class ReadyClassifier:
            def warmup(self) -> None:
                return None

            def classify(self, payload: bytes) -> str:
                del payload
                return "female"

        class TimedOutCandidateWorker(FakeCollectionWorker):
            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                del target, include_activity
                raise WorkerExecutionError(
                    "profile stayed unavailable after bounded confirmation",
                    reason="instagram_profile_temporarily_unavailable",
                    pause_required=True,
                    status_code=503,
                )

            async def read_visible_profile_recovery_evidence(
                self, target: str, *, include_avatar_image: bool = False
            ) -> dict:
                del target
                return {
                    "visibility": "private",
                    "followers": 771,
                    "following": 1419,
                    "posts": 19,
                    "avatar_url": "https://scontent.cdninstagram.com/recovered.jpg",
                    "avatar_image_bytes": b"recovered-private-avatar"
                    if include_avatar_image
                    else None,
                    "avatar_capture_source": "exact_profile_og_image",
                    "recovery_evidence": True,
                }

        task = self.service.create_task(
            self.user["id"],
            name="候选主页超时继续",
            modes=["followers"],
            targets=["timeout_source"],
            window_ids=["timeout-window"],
            settings={
                "live_queue_enabled": True,
                "location_enabled": True,
                "local_person_recognition": True,
                "mode_limits": {
                    "followers": {
                        "followers_max": 3000,
                        "active_days_max": 90,
                        "per_target_limit": 1,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=TimedOutCandidateWorker,
            person_classifier_factory=ReadyClassifier,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(600):
            results = self.service.list_results(self.user["id"], task["id"])
            current_target = self.service.get_task(
                self.user["id"], task["id"]
            )["targets"][0]
            if current_target["status"] in {"waiting_network", "recoverable"}:
                break
            await asyncio.sleep(0.01)
        self.assertEqual([], results)
        self.assertIn(current_target["status"], {"waiting_network", "recoverable"})
        pending_stats = self.service.task_mode_candidate_stats(
            self.user["id"], task["id"], current_target["id"], "followers"
        )
        self.assertEqual(1, pending_stats["pending"])
        await manager.stop(self.user["id"], task["id"])

    async def test_weak_avatar_requires_two_consistent_profile_images_to_change_label(self) -> None:
        class EvidenceClassifier:
            def __init__(self) -> None:
                self.index = 0

            def warmup(self) -> None:
                return None

            def classify(self, payload: bytes) -> PersonRecognition:
                del payload
                values = [
                    PersonRecognition(category="male", confidence=0.60, face_count=1),
                    PersonRecognition(category="female", confidence=0.91, face_count=1),
                    PersonRecognition(category="female", confidence=0.88, face_count=1),
                ]
                result = values[min(self.index, len(values) - 1)]
                self.index += 1
                return result

        class EvidenceWorker:
            async def read_visible_person_evidence_images(
                self, target: str, *, limit: int = 3
            ) -> list[bytes]:
                del target, limit
                return [b"post-one", b"post-two"]

        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            person_classifier_factory=EvidenceClassifier,
        )
        profile: dict[str, object] = {"avatar_capture_source": "current_src"}
        screening: dict[str, object] = {}
        await manager._apply_person_recognition(
            profile,
            screening,
            {"local_person_recognition": True},
            avatar_image_bytes=b"weak-avatar",
            worker=EvidenceWorker(),
            username="evidence_user",
        )
        recognition = screening["person_recognition"]
        self.assertIsInstance(recognition, dict)
        assert isinstance(recognition, dict)
        self.assertEqual("female", profile["person_category"])
        self.assertEqual("local_openvino_multi_image", recognition["source"])
        self.assertEqual("profile_grid_consensus", recognition["reason"])
        self.assertEqual(2, recognition["supplemental_consensus_count"])

    async def test_avatar_only_recognition_never_reads_public_post_images(self) -> None:
        class WeakAvatarClassifier:
            def __init__(self) -> None:
                self.images: list[bytes] = []

            def warmup(self) -> None:
                return None

            def classify(self, payload: bytes) -> PersonRecognition:
                self.images.append(payload)
                return PersonRecognition(
                    category="male", confidence=0.60, face_count=1
                )

        class EvidenceWorker:
            def __init__(self) -> None:
                self.requests = 0

            async def read_visible_person_evidence_images(
                self, target: str, *, limit: int = 3
            ) -> list[bytes]:
                del target, limit
                self.requests += 1
                return [b"post-one", b"post-two"]

        classifier = WeakAvatarClassifier()
        worker = EvidenceWorker()
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            person_classifier_factory=lambda: classifier,
        )
        profile: dict[str, object] = {"avatar_capture_source": "current_src"}
        screening: dict[str, object] = {}

        await manager._apply_person_recognition(
            profile,
            screening,
            {"local_person_recognition": True},
            avatar_image_bytes=b"avatar-only",
            worker=worker,
            username="avatar_only_user",
            allow_supplemental_post_images=False,
        )

        self.assertEqual([b"avatar-only"], classifier.images)
        self.assertEqual(0, worker.requests)
        self.assertEqual("male", profile["person_category"])
        recognition = screening["person_recognition"]
        self.assertIsInstance(recognition, dict)
        assert isinstance(recognition, dict)
        self.assertNotIn("supplemental_images_checked", recognition)

    async def test_avatar_only_review_snapshot_discards_legacy_post_previews(self) -> None:
        class SnapshotWorker:
            async def capture_visible_review_snapshot(
                self, target: str, *, include_post_previews: bool
            ) -> dict:
                self.target = target
                self.include_post_previews = include_post_previews
                return {
                    "avatar_image_bytes": b"avatar-bytes",
                    "avatar_url": "https://example.invalid/avatar.jpg",
                    "recent_posts": [{"post_url": "https://example.invalid/post"}],
                    "review_cache": {
                        "avatar_preview": "data:image/jpeg;base64,YXZhdGFy",
                        "recent_posts": [
                            {"preview_data_url": "data:image/jpeg;base64,cG9zdA=="}
                        ],
                    },
                }

        worker = SnapshotWorker()
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
        )
        profile: dict[str, object] = {
            "recent_posts": [{"post_url": "https://example.invalid/old-post"}]
        }
        review_cache: dict[str, object] = {
            "recent_posts": [{"preview_data_url": "old-preview"}]
        }

        payload = await manager._capture_final_review_evidence(
            worker,
            "avatar_only_user",
            profile,
            review_cache,
            {},
            include_post_previews=False,
        )

        self.assertEqual(b"avatar-bytes", payload)
        self.assertEqual("avatar_only_user", worker.target)
        self.assertFalse(worker.include_post_previews)
        self.assertNotIn("recent_posts", profile)
        self.assertEqual(
            {"avatar_preview": "data:image/jpeg;base64,YXZhdGFy"},
            review_cache,
        )

    async def test_live_queue_can_append_window_and_it_claims_a_waiting_target(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target", "dynamic_target"],
            window_ids=["dynamic-window-a"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(BlockingQueueWorker.started.wait(), timeout=2)

        added = await manager.add_windows(
            self.user["id"], task["id"], ["dynamic-window-b"]
        )
        self.assertEqual(["dynamic-window-b"], added["window_ids"])
        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            dynamic = next(item for item in current["targets"] if item["username"] == "dynamic_target")
            if dynamic["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("completed", dynamic["status"])
        self.assertEqual("dynamic-window-b", dynamic["current_window_id"])
        self.assertEqual(
            ["dynamic-window-a", "dynamic-window-b"],
            self.service.get_task(self.user["id"], task["id"])["window_ids"],
        )

        BlockingQueueWorker.release.set()
        await manager.close_task(self.user["id"], task["id"])

    async def test_user_stop_converges_every_claimed_target_and_remains_terminal(self) -> None:
        AllTargetsBlockingWorker.expected_profiles = 2
        AllTargetsBlockingWorker.started_profiles = set()
        AllTargetsBlockingWorker.all_started = asyncio.Event()
        AllTargetsBlockingWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="停止时收敛所有目标",
            modes=["followers"],
            targets=["blocking_source_a", "blocking_source_b"],
            window_ids=["blocking-window-a", "blocking-window-b"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=AllTargetsBlockingWorker,
        )
        await manager.start(self.user["id"], task["id"])
        assert AllTargetsBlockingWorker.all_started is not None
        await asyncio.wait_for(AllTargetsBlockingWorker.all_started.wait(), timeout=2)

        stopped = await manager.stop(
            self.user["id"], task["id"], close_windows=False
        )
        self.assertEqual("stopped", stopped["status"])
        stored = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", stored["status"])
        self.assertEqual(
            {"recoverable"}, {target["status"] for target in stored["targets"]}
        )
        self.assertFalse(
            any(
                target["status"] in {"running", "waiting_network"}
                for target in stored["targets"]
            )
        )

    async def test_initial_window_failure_does_not_cancel_dynamic_healthy_worker(self) -> None:
        DynamicTeardownRaceWorker.initial_started = asyncio.Event()
        DynamicTeardownRaceWorker.dynamic_started = asyncio.Event()
        DynamicTeardownRaceWorker.allow_initial_failure = asyncio.Event()
        DynamicTeardownRaceWorker.dynamic_cancelled = asyncio.Event()
        DynamicTeardownRaceWorker.allow_dynamic_cleanup = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="动态窗口终审竞态",
            modes=["followers"],
            targets=["fatal_source", "busy_source"],
            window_ids=["teardown-initial-window"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        bitbrowser = FakeBitBrowserClient()
        manager = ExecutionManager(
            self.service,
            bitbrowser,
            worker_factory=DynamicTeardownRaceWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(
            DynamicTeardownRaceWorker.initial_started.wait(), timeout=2
        )
        await manager.add_windows(
            self.user["id"], task["id"], ["teardown-dynamic-window"]
        )
        await asyncio.wait_for(
            DynamicTeardownRaceWorker.dynamic_started.wait(), timeout=2
        )

        try:
            DynamicTeardownRaceWorker.allow_initial_failure.set()
            for _ in range(200):
                current = self.service.get_task(self.user["id"], task["id"])
                failed_target = next(
                    item
                    for item in current["targets"]
                    if item["username"] == "fatal_source"
                )
                if failed_target["status"] == "recoverable":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("The failed window did not preserve its target")

            # A profile-local RuntimeError must not tear down the dynamically added
            # healthy generation. The coordinator and both lease fences remain live
            # until the operator explicitly stops or restarts that failed window.
            self.assertFalse(DynamicTeardownRaceWorker.dynamic_cancelled.is_set())
            self.assertFalse(manager._runs[task["id"]].coordinator.done())
            active_ids = manager.active_task_ids()
            lease_profiles = {
                item["profile_id"]
                for item in self.service.list_browser_lease_states(
                    self.user["id"],
                    active_collection_entity_ids=active_ids,
                    active_action_entity_ids=set(),
                    inactive_grace_seconds=0,
                )
            }
            self.assertEqual(
                {"teardown-initial-window", "teardown-dynamic-window"},
                lease_profiles,
            )
            self.assertIn(task["id"], active_ids)
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(
                    self.user["id"],
                    "teardown-dynamic-window",
                    operation_type="collection",
                    entity_id="duplicate-during-teardown",
                )
            self.assertFalse(
                any(path == "/browser/close" for path, _ in bitbrowser.calls)
            )
        finally:
            DynamicTeardownRaceWorker.allow_dynamic_cleanup.set()
            await manager.stop(self.user["id"], task["id"], close_windows=False)

        self.assertTrue(DynamicTeardownRaceWorker.dynamic_cancelled.is_set())
        self.assertNotIn(task["id"], manager.active_task_ids())
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))
        closed_profiles = {
            payload.get("id") or payload.get("browserId")
            for path, payload in bitbrowser.calls
            if path == "/browser/close"
        }
        self.assertEqual(set(), closed_profiles)
        fatal_task = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", fatal_task["status"])
        self.assertFalse(
            any(
                target["status"] in {"running", "waiting_network"}
                for target in fatal_task["targets"]
            )
        )

    async def test_teardown_renews_leases_until_all_slow_profile_closes_finish(self) -> None:
        for completion_mode in ("natural", "stop"):
            with self.subTest(completion_mode=completion_mode):
                await self._assert_slow_close_lease_renewal(completion_mode)

    async def _assert_slow_close_lease_renewal(self, completion_mode: str) -> None:
        import threading
        from unittest.mock import patch

        profiles = ("slow-close-a", "slow-close-b")
        loop = asyncio.get_running_loop()
        workers_started: set[str] = set()
        all_workers_started = asyncio.Event()
        allow_collection = asyncio.Event()

        class TeardownWorker(FakeCollectionWorker):
            async def collect_followers(self, target: str, *, limit: int) -> CollectionOutcome:
                workers_started.add(self.profile_id)
                if workers_started == set(profiles):
                    all_workers_started.set()
                await allow_collection.wait()
                return await super().collect_followers(target, limit=limit)

        class SlowCloseBitBrowser(FakeBitBrowserClient):
            def __init__(self) -> None:
                super().__init__()
                self.close_started = {profile: asyncio.Event() for profile in profiles}
                self.close_finished = {profile: asyncio.Event() for profile in profiles}
                self.allow_close = {profile: threading.Event() for profile in profiles}

            def close_profile(self, profile_id: str) -> dict[str, Any]:
                loop.call_soon_threadsafe(self.close_started[profile_id].set)
                # The test releases every gate in finally. A slow runner must not
                # turn an unfinished close into a spurious provider timeout.
                self.allow_close[profile_id].wait()
                result = super().close_profile(profile_id)
                loop.call_soon_threadsafe(self.close_finished[profile_id].set)
                return result

        class RenewalObserver(ExecutionManager):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.observe_renewals = False
                self.renewals = asyncio.Queue()
                self.renewal_gates: list[asyncio.Event] = []

            async def _renew_all_leases(self, control) -> None:
                finished = None
                if self.observe_renewals:
                    allowed, finished = asyncio.Event(), asyncio.Event()
                    self.renewal_gates.append(allowed)
                    self.renewals.put_nowait((allowed, finished))
                    await allowed.wait()
                # Observe the real heartbeat and real SQLite renewal; the fixture
                # never renews a lease on behalf of the manager.
                await super()._renew_all_leases(control)
                if finished is not None:
                    finished.set()

        task = self.service.create_task(
            self.user["id"],
            name="慢关闭租约心跳",
            modes=["followers"],
            targets=[f"{completion_mode}_first_target", f"{completion_mode}_second_target"],
            window_ids=list(profiles),
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        bitbrowser = SlowCloseBitBrowser()
        manager = RenewalObserver(
            self.service,
            bitbrowser,
            worker_factory=TeardownWorker,
            lease_heartbeat_interval_seconds=0.01,
        )
        completion_call = None
        try:
            await manager.start(self.user["id"], task["id"])
            await asyncio.wait_for(all_workers_started.wait(), timeout=5)
            control = manager._runs[task["id"]]
            original_leases = dict(control.leases)
            # Exercise both ownership paths: natural window draining and explicit
            # Stop finalization. Natural closes can precede the task finalizer.
            if completion_mode == "natural":
                allow_collection.set()
                completion_call = asyncio.create_task(manager.wait(task["id"]))
            else:
                completion_call = asyncio.create_task(manager.stop(self.user["id"], task["id"]))
            await asyncio.wait_for(asyncio.gather(*(
                event.wait() for event in bitbrowser.close_started.values()
            )), timeout=5)
            if completion_mode == "stop":
                self.assertTrue(control.tearing_down)
            manager.observe_renewals = True

            def lease_rows():
                with self.database.read() as connection:
                    return connection.execute(
                        "SELECT profile_id, lease_token, heartbeat_at, expires_at "
                        "FROM browser_operation_leases WHERE entity_id=? ORDER BY profile_id",
                        (task["id"],),
                    ).fetchall()

            lease_time = [datetime.now(timezone.utc)]
            with patch("app.service.utc_now", side_effect=lambda: lease_time[0]):
                close_stages = (None, profiles[0]) if completion_mode == "stop" else (None, None)
                for completed_profile in close_stages:
                    if completed_profile is not None:
                        bitbrowser.allow_close[completed_profile].set()
                        await asyncio.wait_for(
                            bitbrowser.close_finished[completed_profile].wait(), timeout=5
                        )
                    # Settle any earlier pass, then advance beyond every previous
                    # TTL while this next real heartbeat is held at its boundary.
                    # This proves renewal across two lease periods, including after
                    # one Stop close returns, without a 40/80 ms wall-clock race.
                    allowed, finished = await asyncio.wait_for(manager.renewals.get(), timeout=5)
                    before = lease_rows()
                    self.assertEqual(original_leases, {
                        row["profile_id"]: row["lease_token"] for row in before
                    })
                    lease_time[0] = max(
                        datetime.fromisoformat(row["expires_at"]) for row in before
                    ) + timedelta(seconds=1)
                    allowed.set()
                    await asyncio.wait_for(finished.wait(), timeout=5)
                    rows = lease_rows()
                    self.assertEqual(original_leases, {
                        row["profile_id"]: row["lease_token"] for row in rows
                    })
                    self.assertTrue(all(
                        datetime.fromisoformat(row["expires_at"]) > lease_time[0]
                        and datetime.fromisoformat(row["heartbeat_at"]) == lease_time[0]
                        for row in rows
                    ))
                    self.assertFalse(completion_call.done())
                    self.assertIn(task["id"], manager.active_task_ids())
                    self.assertFalse(bitbrowser.close_finished[profiles[1]].is_set())
                    for profile_id in profiles:
                        with self.assertRaises(ConflictError):
                            self.service.acquire_browser_lease(
                                self.user["id"], profile_id,
                                operation_type="collection",
                                entity_id="duplicate-while-old-close-is-running",
                            )
        finally:
            manager.observe_renewals = False
            for gate in manager.renewal_gates:
                gate.set()
            for gate in bitbrowser.allow_close.values():
                gate.set()
            allow_collection.set()
            if completion_call is not None:
                await asyncio.wait_for(completion_call, timeout=5)

        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))
        self.assertNotIn(task["id"], manager.active_task_ids())
        expected_status = "completed" if completion_mode == "natural" else "stopped"
        self.assertEqual(expected_status, self.service.get_task(self.user["id"], task["id"])["status"])
        self.assertEqual(set(profiles), {
            payload.get("id") or payload.get("browserId")
            for path, payload in bitbrowser.calls if path == "/browser/close"
        })

    async def test_live_queue_immediate_window_append_does_not_start_duplicate_workers(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target"],
            window_ids=["immediate-window-a"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await manager.add_windows(
            self.user["id"], task["id"], ["immediate-window-b"]
        )
        for _ in range(100):
            if set(FakeCollectionWorker.connected_profiles) == {
                "immediate-window-a",
                "immediate-window-b",
            }:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            ["immediate-window-a", "immediate-window-b"],
            sorted(FakeCollectionWorker.connected_profiles),
        )
        self.assertEqual(
            "running", self.service.get_task(self.user["id"], task["id"])["status"]
        )
        await manager.close_task(self.user["id"], task["id"])

    async def test_live_queue_appended_empty_window_closes_without_stopping_busy_sibling(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target"],
            window_ids=["waiting-window-a"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        bitbrowser = FakeBitBrowserClient()
        manager = ExecutionManager(
            self.service, bitbrowser, worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(BlockingQueueWorker.started.wait(), timeout=2)

        await manager.add_windows(
            self.user["id"], task["id"], ["waiting-window-b"]
        )
        for _ in range(100):
            if ("waiting-window-b" in FakeCollectionWorker.connected_profiles
                    and "waiting-window-b" not in manager._runs[task["id"]].leases):
                break
            await asyncio.sleep(0.01)
        states = {
            item["profile_id"]: item["state"]
            for item in self.service.list_browser_lease_states(self.user["id"])
        }
        self.assertNotIn("waiting-window-b", states)
        self.assertEqual({"waiting-window-a"}, set(states))
        self.assertEqual("running", self.service.get_task(self.user["id"], task["id"])["status"])

        BlockingQueueWorker.release.set()
        await asyncio.wait_for(manager.wait(task["id"]), timeout=2)
        closed_profiles = {
            payload.get("id") or payload.get("browserId")
            for path, payload in bitbrowser.calls
            if path == "/browser/close"
        }
        self.assertEqual({"waiting-window-a", "waiting-window-b"}, closed_profiles)
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_live_queue_append_windows_rejects_duplicate_and_rolls_back_occupied_batch(self) -> None:
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target"],
            window_ids=["append-existing"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        with self.assertRaises(ConflictError):
            await manager.add_windows(self.user["id"], task["id"], ["append-existing"])

        occupied_campaign = self.service.create_action_campaign(
            self.user["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="append-occupied",
            targets=["occupied_target"],
            message=None,
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        occupied_token = self.service.acquire_browser_lease(
            self.user["id"],
            "append-occupied",
            operation_type="action",
            entity_id=occupied_campaign["id"],
        )
        with self.assertRaises(ConflictError):
            await manager.add_windows(
                self.user["id"],
                task["id"],
                ["append-free-first", "append-occupied"],
            )
        stored = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(["append-existing"], stored["window_ids"])
        states = {
            item["profile_id"]: item for item in self.service.list_browser_lease_states(self.user["id"])
        }
        self.assertNotIn("append-free-first", states)
        self.assertIn("append-occupied", states)
        self.service.release_browser_lease("append-occupied", occupied_token)
        await manager.close_task(self.user["id"], task["id"])

    async def test_live_queue_window_added_while_paused_does_not_claim_until_resume(self) -> None:
        FakeCollectionWorker.connected_profiles = []
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target"],
            window_ids=["paused-window-a"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(BlockingQueueWorker.started.wait(), timeout=2)
        await manager.pause(self.user["id"], task["id"])
        added_target = await manager.add_targets(
            self.user["id"], task["id"], ["pause_waiting"]
        )
        await manager.add_windows(
            self.user["id"], task["id"], ["paused-window-b"]
        )
        await asyncio.sleep(0.05)
        pending_id = added_target["targets"][0]["id"]
        paused_task = self.service.get_task(self.user["id"], task["id"])
        pending = next(item for item in paused_task["targets"] if item["id"] == pending_id)
        self.assertEqual("pending", pending["status"])
        states = {
            item["profile_id"]: item["state"]
            for item in self.service.list_browser_lease_states(self.user["id"])
        }
        self.assertEqual("paused", states["paused-window-b"])

        await manager.resume(self.user["id"], task["id"])
        BlockingQueueWorker.release.set()
        for _ in range(100):
            resumed = self.service.get_task(self.user["id"], task["id"])
            pending = next(item for item in resumed["targets"] if item["id"] == pending_id)
            if pending["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual("completed", pending["status"])
        await manager.close_task(self.user["id"], task["id"])

    async def test_live_queue_single_target_delete_does_not_touch_siblings_or_run_deleted_target(self) -> None:
        BlockingQueueWorker.started = asyncio.Event()
        BlockingQueueWorker.release = asyncio.Event()
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["keep_target", "delete_target", "later_target"],
            window_ids=["delete-window"],
            settings={
                "live_queue_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=BlockingQueueWorker
        )
        await manager.start(self.user["id"], task["id"])
        await asyncio.wait_for(BlockingQueueWorker.started.wait(), timeout=2)
        delete_id = next(item["id"] for item in task["targets"] if item["username"] == "delete_target")
        await manager.delete_target(self.user["id"], task["id"], delete_id)
        BlockingQueueWorker.release.set()
        for _ in range(100):
            current = self.service.get_task(self.user["id"], task["id"])
            statuses = {item["username"]: item["status"] for item in current["targets"]}
            if statuses.get("keep_target") == "completed" and statuses.get("later_target") == "completed":
                break
            await asyncio.sleep(0.01)
        current = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual(
            {"keep_target", "delete_target", "later_target"},
            {item["username"] for item in current["targets"]},
        )
        deleted = next(
            item for item in current["targets"] if item["username"] == "delete_target"
        )
        self.assertEqual("stopped", deleted["status"])
        self.assertTrue(deleted["manual_recovery_required"])
        self.assertEqual("running", current["status"])
        await manager.close_task(self.user["id"], task["id"])

    async def test_public_profile_risk_filters_short_circuit_before_expensive_gates(self) -> None:
        class ProfileRiskGateWorker(FakeCollectionWorker):
            events: list[tuple[str, str]] = []

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append(
                    (target, "activity" if include_activity else "profile")
                )
                profile = {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 9,
                    "activity_days": 2 if include_activity else None,
                    "activity_status": (
                        "timestamp_found" if include_activity else "not_checked"
                    ),
                    "is_verified": target.startswith("verified_source"),
                    "is_professional_account": False,
                    "visible_description": "Realtor helping families find a home/house",
                }
                if target.startswith("estate_source"):
                    profile["account_category"] = "房地产经纪人"
                    profile["is_professional_account"] = True
                elif target.startswith("realtor_source"):
                    profile["account_category"] = "Real Estate Agent"
                    profile["is_professional_account"] = True
                elif target.startswith("media_source"):
                    profile["account_category"] = "Media Production Company"
                    profile["is_professional_account"] = True
                elif target.startswith("external_source"):
                    profile["external_bio_url"] = "https://example.com/offer"
                    profile["has_external_bio_link"] = True
                elif target.startswith("threads_source"):
                    # Restored/legacy payloads may contain the raw Threads badge and
                    # a stale truthy flag.  ExecutionManager must normalize the URL
                    # again instead of trusting the serialized boolean alone.
                    profile["external_bio_url"] = (
                        "https://www.threads.net/@threads_source.fan"
                    )
                    profile["has_external_bio_link"] = True
                return profile

            async def read_visible_account_location(self, target: str) -> str:
                type(self).events.append((target, "location"))
                return "美国"

            async def capture_visible_review_snapshot(
                self, target: str, *, include_post_previews: bool
            ) -> dict:
                type(self).events.append(
                    (target, "snapshot", include_post_previews)
                )
                return {}

        ProfileRiskGateWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="公开主页风险前置排除",
            modes=["followers"],
            targets=[
                "estate_source",
                "realtor_source",
                "media_source",
                "external_source",
                "verified_source",
                "threads_source",
                "ordinary_source",
            ],
            window_ids=["profile-risk-window"],
            settings={
                "exclude_verified": True,
                "location_enabled": True,
                "mode_limits": {
                    "followers": {
                        "followers_min": 1,
                        "active_days_max": 30,
                        "per_target_limit": 1,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=ProfileRiskGateWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = {
            result["username"]: result
            for result in self.service.list_results(self.user["id"], task["id"])
        }
        expected_reasons = {
            "estate_source.fan": "professional_account_excluded",
            "realtor_source.fan": "professional_account_excluded",
            "media_source.fan": "professional_account_excluded",
            "external_source.fan": "external_bio_link_excluded",
            "verified_source.fan": "verified_account_excluded",
        }
        for username, reason in expected_reasons.items():
            result = results[username]
            self.assertFalse(result["qualified"])
            self.assertEqual("excluded", result["screening"]["review_tier"])
            self.assertEqual(reason, result["screening"]["review_reason"])
            self.assertNotIn((username, "location"), ProfileRiskGateWorker.events)
            self.assertNotIn((username, "activity"), ProfileRiskGateWorker.events)
            self.assertFalse(
                any(
                    event[0] == username and event[1] == "snapshot"
                    for event in ProfileRiskGateWorker.events
                )
            )

        for retained_username in ("threads_source.fan", "ordinary_source.fan"):
            with self.subTest(retained_username=retained_username):
                retained = results[retained_username]
                self.assertTrue(retained["qualified"])
                self.assertEqual(
                    "public_primary_review",
                    retained["screening"]["routing_result"],
                )
                self.assertIn(
                    (retained_username, "location"), ProfileRiskGateWorker.events
                )
                self.assertIn(
                    (retained_username, "activity"), ProfileRiskGateWorker.events
                )
                self.assertIn(
                    (retained_username, "snapshot", False),
                    ProfileRiskGateWorker.events,
                )
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(
            {"threads_source.fan", "ordinary_source.fan"},
            {item["username"] for item in snapshot["pending_public_accounts"]},
        )
        self.assertEqual(
            set(expected_reasons.values()),
            {
                item["reason_code"]
                for item in snapshot["collection_exclusion_history"]
            },
        )

    async def test_public_count_discard_excludes_before_location_or_activity(self) -> None:
        LocationFirstWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["location_source"],
            window_ids=["location-window"],
            settings={
                "location_enabled": True,
                "public_discard_followers_max": 500,
                "mode_limits": {"followers": {"followers_max": 500, "per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=LocationFirstWorker
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual(["profile"], LocationFirstWorker.events)
        self.assertNotIn("location_zh", result["profile"])
        self.assertFalse(result["qualified"])
        self.assertFalse(result["screening"]["location"]["checked"])
        self.assertFalse(result["screening"]["activity"]["checked"])
        self.assertEqual("excluded", result["screening"]["review_tier"])
        self.assertEqual("excluded_account_count_ceiling", result["screening"]["routing_result"])
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual(
            "account_count_ceiling_exceeded",
            snapshot["collection_exclusion_history"][0]["reason_code"],
        )

    async def test_location_defaults_on_and_non_us_short_circuits_screening(self) -> None:
        self.assertTrue(TaskSettingsRequest().location_enabled)
        self.assertTrue(
            DesktopTaskCreateRequest(
                targets=["source"], window_ids=["window"], modes=["followers"]
            ).read_location
        )
        UnitedStatesGateWorker.events = []
        TrackingReviewer.reviewed = []
        task = self.service.create_task(
            self.user["id"],
            name="所在地优先筛选",
            modes=["followers"],
            targets=["non_us_source", "us_source", "unknown_source"],
            window_ids=["location-gate-window"],
            settings={
                "location_enabled": True,
                "gpt_enabled": True,
                "mode_limits": {
                    "followers": {"active_days_max": 30, "per_target_limit": 1}
                },
            },
        )
        self.assertTrue(task["settings"]["location_enabled"])
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=UnitedStatesGateWorker,
            reviewer_factory=TrackingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = {
            item["username"]: item
            for item in self.service.list_results(self.user["id"], task["id"])
        }
        non_us = results["non_us_source.fan"]
        united_states = results["us_source.fan"]
        unknown = results["unknown_source.fan"]

        self.assertFalse(non_us["qualified"])
        self.assertEqual("加拿大", non_us["profile"]["location_zh"])
        self.assertFalse(non_us["screening"]["location"]["passed"])
        self.assertEqual("加拿大", non_us["screening"]["location"]["country"])
        self.assertEqual("excluded_non_us", non_us["screening"]["routing_result"])
        self.assertEqual("non_us_location", non_us["screening"]["location"]["reason"])
        self.assertFalse(non_us["screening"]["activity"]["checked"])
        self.assertFalse(non_us["screening"]["gpt"]["checked"])
        self.assertNotIn(
            ("non_us_source.fan", "activity"), UnitedStatesGateWorker.events
        )
        self.assertNotIn("non_us_source.fan", TrackingReviewer.reviewed)

        self.assertTrue(united_states["qualified"])
        self.assertEqual("美国", united_states["profile"]["location_zh"])
        self.assertTrue(united_states["screening"]["location"]["passed"])
        self.assertEqual("美国", united_states["screening"]["location"]["country"])
        self.assertEqual("public_primary_review", united_states["screening"]["routing_result"])
        self.assertIn(("us_source.fan", "activity"), UnitedStatesGateWorker.events)
        self.assertIn("us_source.fan", TrackingReviewer.reviewed)

        self.assertTrue(unknown["qualified"])
        self.assertEqual("private", unknown["visibility"])
        self.assertNotIn("location_zh", unknown["profile"])
        self.assertIsNone(unknown["screening"]["location"]["passed"])
        self.assertEqual("not_visible_on_private_account", unknown["screening"]["location"]["reason"])
        self.assertNotIn(("unknown_source.fan", "activity"), UnitedStatesGateWorker.events)
        self.assertNotIn("unknown_source.fan", TrackingReviewer.reviewed)
        pending = self.service.get_workbench_snapshot(self.user["id"])
        self.assertIn(
            "unknown_source.fan",
            [item["username"] for item in pending["pending_private_accounts"]],
        )

    async def test_non_us_renamed_duplicate_is_collapsed_before_exclusion_history(self) -> None:
        original = self.service.claim_workbench_identity(
            self.user["id"],
            username="canonical.non.us",
            source="followers",
            source_target="seed",
        )
        self.service.confirm_workbench_identity(
            self.user["id"],
            claim_id=original["claim_id"],
            username="canonical.non.us",
            instagram_user_id="9876543210987654321",
        )
        RenamedNonUsGateWorker.location_read = False
        task = self.service.create_task(
            self.user["id"],
            name="改名重复所在地门禁",
            modes=["followers"],
            targets=["renamed_non_us_source"],
            window_ids=["renamed-location-window"],
            settings={
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=RenamedNonUsGateWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        self.assertEqual([], self.service.list_results(self.user["id"], task["id"]),)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["collection_exclusion_history"])
        expected_seen = {"canonical.non.us", "renamed_non_us_source"}
        self.assertEqual(len(expected_seen), snapshot["dedupe"]["total"])
        with self.database.read() as connection:
            self.assertEqual(expected_seen, {
                row[0] for row in connection.execute(
                    "SELECT a.current_username_norm FROM instagram_accounts a "
                    "JOIN global_seen g ON g.account_id=a.id"
                )
            })
            aliases = connection.execute(
                """
                SELECT username_norm FROM instagram_username_aliases
                WHERE account_id=? ORDER BY username_norm
                """,
                (original["account_id"],),
            ).fetchall()
        self.assertEqual(
            ["canonical.non.us", "renamed_non_us_source.fan"],
            [row["username_norm"] for row in aliases],
        )

    async def test_public_unknown_location_is_retained_and_activity_continues(self) -> None:
        class PublicUnknownLocationWorker(FakeCollectionWorker):
            location_reads = 0
            activity_reads = 0

            async def read_visible_account_location(self, target: str) -> None:
                del target
                type(self).location_reads += 1
                return None

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                if include_activity:
                    type(self).activity_reads += 1
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 9,
                    "activity_days": 2 if include_activity else None,
                    "activity_status": (
                        "timestamp_found" if include_activity else "not_checked"
                    ),
                }

        task = self.service.create_task(
            self.user["id"],
            name="公开未显示地区保留人工审核",
            modes=["followers"],
            targets=["unknownloc"],
            window_ids=["public-unknown-location-window"],
            settings={
                "location_enabled": True,
                "mode_limits": {
                    "followers": {
                        "per_target_limit": 1,
                        "followers_min": 1,
                        "active_days_max": 30,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PublicUnknownLocationWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        self.assertEqual(1, PublicUnknownLocationWorker.location_reads)
        self.assertEqual(1, PublicUnknownLocationWorker.activity_reads)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(
            ["unknownloc.fan"],
            [item["username"] for item in snapshot["pending_public_accounts"]],
        )
        self.assertEqual([], snapshot["collection_exclusion_history"])
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertIsNone(result["qualified"])
        self.assertIsNone(result["screening"]["location"]["passed"])
        self.assertTrue(
            result["screening"]["location"]["retained_for_manual_review"]
        )
        self.assertEqual("country_not_visible", result["screening"]["location"]["reason"])
        self.assertEqual("primary", result["screening"]["review_tier"])
        self.assertEqual(
            "public_location_unavailable_retained",
            result["screening"]["review_reason"],
        )
        self.assertEqual("public_primary_review", result["screening"]["routing_result"])
        self.assertTrue(result["screening"]["activity"]["checked"])
        self.assertIsNone(result["screening"]["activity"]["passed"])
        self.assertFalse(result["screening"]["activity"]["enabled"])

    async def test_public_count_discard_failure_skips_later_gates_and_review(self) -> None:
        OverLimitCollectionWorker.activity_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["source_basic_filter"],
            window_ids=["window-filter"],
            settings={
                "gpt_enabled": True,
                "public_discard_followers_max": 500,
                "mode_limits": {
                    "followers": {
                        "followers_max": 500,
                        "active_days_max": 30,
                        "per_target_limit": 10,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=OverLimitCollectionWorker,
            reviewer_factory=ExplodingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = self.service.list_results(self.user["id"], task["id"])
        self.assertEqual(1, len(results))
        # The configured follower-count rejection ends public screening immediately.
        self.assertFalse(results[0]["qualified"])
        self.assertFalse(results[0]["screening"]["basic"]["passed"])
        self.assertIn(
            "account_count_ceiling_exceeded", results[0]["screening"]["basic"]["reason_codes"]
        )
        self.assertEqual(
            [False],
            [include_activity for _, include_activity in OverLimitCollectionWorker.activity_reads],
        )
        self.assertFalse(results[0]["screening"]["activity"]["checked"])
        self.assertEqual("excluded", results[0]["screening"]["review_tier"])
        self.assertFalse(results[0]["screening"]["gpt"]["checked"])
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual(1, snapshot["counts"]["collection_excluded"])
        self.assertEqual(
            "公开账号粉丝 999 > 500，已按丢弃上限在采集阶段排除",
            snapshot["collection_exclusion_history"][0]["reason"],
        )

    async def test_public_unknown_counts_are_retained_and_retired_limits_do_not_exclude(self) -> None:
        UnknownCountActivityWorker.activity_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["unknown_pass", "unknown_fail"],
            window_ids=["window-unknown-activity"],
            settings={
                "gpt_enabled": True,
                "public_discard_followers_max": 500,
                "mode_limits": {
                    "followers": {
                        "followers_max": 500,
                        "active_days_max": 30,
                        "per_target_limit": 1,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=UnknownCountActivityWorker,
            reviewer_factory=ExplodingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = {
            result["username"]: result
            for result in self.service.list_results(self.user["id"], task["id"])
        }
        first_unknown = results["unknown_pass.fan"]
        second_unknown = results["unknown_fail.fan"]

        for result in (first_unknown, second_unknown):
            self.assertTrue(result["screening"]["basic"]["passed"])
            self.assertTrue(result["screening"]["location"]["checked"])
            self.assertTrue(result["screening"]["activity"]["checked"])
            self.assertTrue(result["qualified"])
            self.assertTrue(result["screening"]["gpt"]["checked"])
            self.assertEqual("primary", result["screening"]["review_tier"])
        self.assertEqual(2, sum(active for _, active in UnknownCountActivityWorker.activity_reads))
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(2, len(snapshot["pending_public_accounts"]))
        self.assertEqual(0, snapshot["counts"]["collection_excluded"])
        self.assertEqual([], snapshot["collection_exclusion_history"])

    async def test_public_following_and_posts_discard_only_known_excess(self) -> None:
        class FollowingPostsGateWorker(FakeCollectionWorker):
            events: list[tuple[str, str]] = []

            async def read_visible_account_location(self, target: str) -> str:
                type(self).events.append((target, "location"))
                return "美国"

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append(
                    (target, "activity" if include_activity else "profile")
                )
                following: int | None = 80
                posts: int | None = 9
                if "following_fail" in target:
                    following = 999
                elif "following_unknown" in target or "disabled_unknown" in target:
                    following = None
                if "posts_fail" in target:
                    posts = 999
                elif "posts_unknown" in target or "disabled_unknown" in target:
                    posts = None
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": following,
                    "posts": posts,
                    "activity_days": 2 if include_activity else None,
                    "activity_status": (
                        "timestamp_found" if include_activity else "not_checked"
                    ),
                }

        FollowingPostsGateWorker.events = []
        strict_task = self.service.create_task(
            self.user["id"],
            name="公开关注帖子严格门禁",
            modes=["followers"],
            targets=[
                "following_fail",
                "following_unknown",
                "posts_fail",
                "posts_unknown",
            ],
            window_ids=["following-posts-gate-window"],
            settings={
                "public_discard_following_max": 100,
                "public_discard_posts_max": 100,
                "mode_limits": {
                    "followers": {
                        "following_max": 100,
                        "posts_max": 100,
                        "per_target_limit": 1,
                    }
                }
            },
        )
        strict_manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=FollowingPostsGateWorker,
        )
        await strict_manager.start(self.user["id"], strict_task["id"])
        await strict_manager.wait(strict_task["id"])

        strict_results = self.service.list_results(self.user["id"], strict_task["id"])
        self.assertEqual(4, len(strict_results))
        for result in strict_results:
            if "_fail" in result["username"]:
                self.assertFalse(result["qualified"])
                self.assertEqual("account_count_ceiling_exceeded", result["screening"]["review_reason"])
                self.assertFalse(result["screening"]["location"]["checked"])
                self.assertFalse(result["screening"]["activity"]["checked"])
                self.assertNotIn((result["username"], "activity"), FollowingPostsGateWorker.events)
            else:
                self.assertTrue(result["qualified"])
                self.assertEqual("primary", result["screening"]["review_tier"])
                self.assertTrue(result["screening"]["activity"]["checked"])

        disabled_task = self.service.create_task(
            self.user["id"],
            name="未启用关注帖子门禁",
            modes=["followers"],
            targets=["disabled_unknown"],
            window_ids=["following-posts-disabled-window"],
            settings={
                "mode_limits": {
                    "followers": {
                        "following_max": 0,
                        "posts_max": 0,
                        "per_target_limit": 1,
                    }
                }
            },
        )
        disabled_manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=FollowingPostsGateWorker,
        )
        await disabled_manager.start(self.user["id"], disabled_task["id"])
        await disabled_manager.wait(disabled_task["id"])
        disabled_result = self.service.list_results(
            self.user["id"], disabled_task["id"]
        )[0]
        self.assertTrue(disabled_result["qualified"])
        self.assertTrue(disabled_result["screening"]["basic"]["passed"])
        self.assertEqual([], disabled_result["screening"]["basic"]["reason_codes"])
        self.assertEqual(
            "public_primary_review",
            disabled_result["screening"]["routing_result"],
        )

    async def test_public_post_activity_discard_excludes_known_excess_and_retains_unknown(self) -> None:
        class PublicActivityGateWorker(FakeCollectionWorker):
            events: list[tuple[str, str]] = []

            async def read_visible_account_location(self, target: str) -> str:
                type(self).events.append((target, "location"))
                return "美国"

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append(
                    (target, "activity" if include_activity else "profile")
                )
                activity_days = None
                activity_status = "not_checked"
                if include_activity and "activity_fail" in target:
                    activity_days = 45
                    activity_status = "timestamp_found"
                elif include_activity:
                    activity_status = "timestamp_unavailable"
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 9,
                    "activity_days": activity_days,
                    "activity_status": activity_status,
                }

        PublicActivityGateWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="公开活跃度采集排除",
            modes=["followers"],
            targets=["activity_fail", "activity_unknown"],
            window_ids=["activity-gate-window"],
            settings={
                "public_discard_active_days_max": 30,
                "mode_limits": {
                    "followers": {
                        "followers_min": 1,
                        "active_days_max": 30,
                        "per_target_limit": 1,
                    }
                }
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PublicActivityGateWorker,
            reviewer_factory=ExplodingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = {
            result["username"]: result
            for result in self.service.list_results(self.user["id"], task["id"])
        }
        failed = results["activity_fail.fan"]
        unknown = results["activity_unknown.fan"]
        self.assertEqual("public_activity_ceiling_exceeded", failed["screening"]["review_reason"])
        self.assertFalse(failed["qualified"])
        self.assertEqual("excluded", failed["screening"]["review_tier"])
        self.assertIsNone(unknown["screening"]["activity_ceiling"]["passed"])
        self.assertEqual("primary", unknown["screening"]["review_tier"])
        for result in (failed, unknown):
            self.assertTrue(result["screening"]["basic"]["passed"])
            self.assertTrue(result["screening"]["location"]["passed"])
            self.assertTrue(result["screening"]["activity"]["checked"])
            self.assertFalse(result["screening"]["activity"]["enabled"])
            self.assertFalse(result["screening"]["gpt"]["checked"])
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(["activity_unknown.fan"], [item["username"] for item in snapshot["pending_public_accounts"]])
        self.assertEqual(["public_activity_ceiling_exceeded"], [item["reason_code"] for item in snapshot["collection_exclusion_history"]])
        self.assertEqual(
            "公开账号最近发帖距今 45 天 > 丢弃上限 30 天，已按直接丢弃规则在采集阶段排除",
            snapshot["collection_exclusion_history"][0]["reason"],
        )

    async def test_public_zero_posts_without_activity_enters_manual_review(self) -> None:
        class PublicZeroPostWorker(FakeCollectionWorker):
            events: list[str] = []

            async def read_visible_account_location(self, target: str) -> str:
                del target
                type(self).events.append("location")
                return "美国"

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append("activity" if include_activity else "profile")
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 0,
                    "activity_days": None,
                    "activity_status": "no_posts" if include_activity else "not_checked",
                }

        PublicZeroPostWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="公开零帖保留人工审核",
            modes=["followers"],
            targets=["zero_post_source"],
            window_ids=["zero-post-window"],
            settings={
                # Retention is the explicit opt-out path; r39 defaults to discard.
                "exclude_public_zero_posts": False,
                "gpt_enabled": True,
                "mode_limits": {
                    "followers": {
                        "followers_min": 1,
                        "active_days_max": 30,
                        "per_target_limit": 1,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PublicZeroPostWorker,
            reviewer_factory=ExplodingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertIsNone(result["qualified"])
        self.assertEqual("no_posts", result["profile"]["activity_status"])
        self.assertTrue(result["screening"]["activity"]["checked"])
        self.assertIsNone(result["screening"]["activity"]["passed"])
        self.assertEqual(
            "activity_no_posts", result["screening"]["activity"]["reason"]
        )
        self.assertTrue(
            result["screening"]["activity"]["retained_for_manual_review"]
        )
        self.assertEqual("primary", result["screening"]["review_tier"])
        self.assertEqual(
            "public_zero_posts_activity_requires_review",
            result["screening"]["review_reason"],
        )
        self.assertEqual(
            "public_primary_review", result["screening"]["routing_result"]
        )
        self.assertFalse(result["screening"]["gpt"]["checked"])
        self.assertEqual(["profile", "location", "activity"], PublicZeroPostWorker.events)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(
            ["zero_post_source.fan"],
            [item["username"] for item in snapshot["pending_public_accounts"]],
        )
        self.assertEqual([], snapshot["collection_exclusion_history"])

    async def test_public_zero_posts_switch_excludes_before_location_and_activity(self) -> None:
        class PublicZeroPostSkipWorker(FakeCollectionWorker):
            events: list[str] = []

            async def read_visible_account_location(self, target: str) -> str:
                del target
                type(self).events.append("location")
                raise AssertionError("zero-post shortcut must run before location")

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append("activity" if include_activity else "profile")
                if include_activity:
                    raise AssertionError("zero-post shortcut must run before activity")
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 0,
                    "activity_days": None,
                    "activity_status": "not_checked",
                }

        PublicZeroPostSkipWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="公开零帖采集前规避",
            modes=["followers"],
            targets=["zero_post_skip_source"],
            window_ids=["zero-post-skip-window"],
            settings={
                "exclude_public_zero_posts": True,
                "gpt_enabled": True,
                "mode_limits": {
                    "followers": {
                        "followers_min": 1,
                        "active_days_max": 30,
                        "per_target_limit": 1,
                    }
                },
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PublicZeroPostSkipWorker,
            reviewer_factory=ExplodingReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertFalse(result["qualified"])
        self.assertEqual(
            "public_zero_posts_excluded", result["screening"]["review_reason"]
        )
        self.assertEqual("excluded", result["screening"]["review_tier"])
        self.assertEqual(
            "skipped_public_zero_posts_excluded",
            result["screening"]["location"]["reason"],
        )
        self.assertEqual(
            "skipped_public_zero_posts_excluded",
            result["screening"]["activity"]["reason"],
        )
        self.assertEqual(["profile"], PublicZeroPostSkipWorker.events)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual(
            ["public_zero_posts_excluded"],
            [
                item["reason_code"]
                for item in snapshot["collection_exclusion_history"]
            ],
        )

    async def test_private_zero_posts_are_excluded_when_zero_switch_is_enabled(self) -> None:
        class PrivateZeroPostSkipWorker(FakeCollectionWorker):
            events: list[str] = []

            async def read_visible_account_location(self, target: str) -> str:
                del target
                type(self).events.append("location")
                raise AssertionError("private zero-post shortcut must precede location")

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append("activity" if include_activity else "profile")
                if include_activity:
                    raise AssertionError("private zero-post shortcut must precede activity")
                return {
                    "username": target,
                    "visibility": "private",
                    "followers": 120,
                    "following": 80,
                    "posts": 0,
                    "activity_days": None,
                    "activity_status": "not_checked",
                }

            async def capture_visible_review_snapshot(self, *args: object, **kwargs: object) -> dict:
                del args, kwargs
                type(self).events.append("review_evidence")
                return {"avatar_url": "https://example.com/avatar.png"}

        PrivateZeroPostSkipWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="私密零帖采集前规避",
            modes=["followers"],
            targets=["priv0src"],
            window_ids=["private-zero-post-skip-window"],
            settings={
                "exclude_public_zero_posts": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PrivateZeroPostSkipWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual("private", result["visibility"])
        self.assertFalse(result["qualified"])
        self.assertEqual("private_zero_posts_excluded", result["screening"]["review_reason"])
        self.assertEqual("excluded", result["screening"]["review_tier"])
        self.assertEqual(["profile"], PrivateZeroPostSkipWorker.events)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(["private_zero_posts_excluded"], [row["reason_code"] for row in snapshot["collection_exclusion_history"]])
        self.assertEqual([], snapshot["pending_private_accounts"])

    async def test_retired_gender_switch_cannot_warm_classify_or_exclude_public_and_private(self) -> None:
        class RetainedWorker(FakeCollectionWorker):
            async def read_visible_profile(self, target, *, include_activity=False):
                return {"username": target, "visibility": "private" if "private" in target else "public",
                    "followers": 120, "following": 80, "posts": 9, "activity_days": 2,
                    "activity_status": "identified"}
        def forbidden_classifier():
            raise AssertionError("Retired gender model must never initialize")
        task = self.service.create_task(self.user["id"], name="已取消男女判断",
            modes=["followers"], targets=["public_legacy", "private_legacy"],
            window_ids=["retired-gender-window"], settings={"local_person_recognition": True,
                "exclude_male_avatar": True, "mode_limits": {"followers": {"per_target_limit": 1}}})
        self.assertFalse(task["settings"]["local_person_recognition"])
        self.assertFalse(task["settings"]["exclude_male_avatar"])
        manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=RetainedWorker,
                                   person_classifier_factory=forbidden_classifier)
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        results = self.service.list_results(self.user["id"], task["id"])
        self.assertEqual(2, len(results))
        self.assertEqual({"public", "private"}, {row["visibility"] for row in results})
        for row in results:
            self.assertEqual("primary", row["screening"]["review_tier"])
            self.assertNotIn("male_avatar_filter", row["screening"])
            self.assertFalse(row["screening"].get("person_recognition", {}).get("checked"))
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["collection_exclusion_history"])

    async def test_homepage_screening_precedes_activity_and_keeps_avatar_evidence(self) -> None:
        events = []

        class OrderedWorker(FakeCollectionWorker):
            supports_avatar_image_capture = True

            async def read_visible_profile(self, target, *, include_activity=False):
                events.append("activity" if include_activity else "profile")
                return {"username": target, "visibility": "public", "followers": 10,
                        "following": 20, "posts": 3, "activity_days": 1 if include_activity else None,
                        "activity_status": "identified" if include_activity else "not_checked",
                        "person_category": "unknown", "avatar_capture_source": "not_captured"}

            async def read_visible_account_location(self, target):
                events.append("location")
                return "美国"

            async def capture_visible_review_snapshot(self, target, *, include_post_previews):
                events.append("avatar")
                self.assertion = not include_post_previews
                return {"avatar_image_bytes": b"avatar", "avatar_capture_source": "exact_profile_avatar"}

        class Classifier:
            def warmup(self):
                pass

            def classify(self, payload):
                events.append("recognition")
                return PersonRecognition(category="female", confidence=.95, face_count=1)

        class Reviewer:
            def review(self, payload):
                events.append("gpt")
                return {"review_status": "reviewed", "confidence": "high", "reason_codes": []}

        task = self.service.create_task(
            self.user["id"], name="主页筛选先于活跃度", modes=["followers"],
            targets=["ordering_source"], window_ids=["ordering_window"],
            settings={"exclude_male_avatar": True, "location_enabled": True,
                      "gpt_enabled": True, "active_days_max": 7,
                      "mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(self.service, FakeBitBrowserClient(), worker_factory=OrderedWorker,
                                   person_classifier_factory=Classifier, reviewer_factory=Reviewer)
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])
        self.assertEqual(["profile", "location", "gpt", "activity", "avatar"], events)
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertTrue(result["qualified"])
        self.assertEqual("unknown", result["profile"]["person_category"])
        self.assertEqual("exact_profile_avatar", result["profile"]["avatar_capture_source"])
        self.assertEqual(1, result["profile"]["activity_days"])

    async def test_private_legacy_basic_failure_and_unknown_enter_single_manual_review(self) -> None:
        class PrivateBasicGateWorker(FakeCollectionWorker):
            location_read = False

            async def read_visible_account_location(self, target: str) -> str:
                del target
                type(self).location_read = True
                raise AssertionError("private account must not read location")

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                if include_activity:
                    raise AssertionError("private account must not read activity")
                return {
                    "username": target,
                    "visibility": "private",
                    "followers": None if "private_unknown" in target else 999,
                    "following": 80,
                    "posts": 9,
                    "activity_days": None,
                    "account_category": "Real Estate Agent",
                    "external_bio_url": "https://example.com/private-offer",
                    "has_external_bio_link": True,
                    "is_verified": True,
                }

        PrivateBasicGateWorker.location_read = False
        task = self.service.create_task(
            self.user["id"],
            name="私密旧合格条件不再分出二审",
            modes=["followers"],
            targets=["private_failed", "private_unknown"],
            window_ids=["private-secondary-window"],
            settings={
                "exclude_verified": True,
                "mode_limits": {
                    "followers": {
                        "followers_max": 500,
                        "per_target_limit": 1,
                    }
                }
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PrivateBasicGateWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual([], snapshot["collection_exclusion_history"])
        self.assertEqual(2, len(snapshot["pending_private_accounts"]))
        self.assertFalse(PrivateBasicGateWorker.location_read)
        by_name = {
            item["username"]: item for item in snapshot["pending_private_accounts"]
        }
        self.assertTrue(
            by_name["private_failed.fan"]["screening"]["basic"]["passed"]
        )
        self.assertTrue(
            by_name["private_unknown.fan"]["screening"]["basic"]["passed"]
        )
        for item in by_name.values():
            self.assertEqual("primary", item["screening"]["review_tier"])
            self.assertEqual(
                "private_account_collected",
                item["screening"]["review_reason"],
            )

    async def test_chinese_private_zero_post_profile_never_enters_public_location_gate(self) -> None:
        class ChinesePrivateZeroPostWorker(FakeCollectionWorker):
            connected_profiles: list[str] = []
            location_read = False
            activity_read = False

            async def read_visible_account_location(self, target: str) -> str:
                del target
                type(self).location_read = True
                raise AssertionError("private zero-post account must not read location")

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                if include_activity:
                    type(self).activity_read = True
                    raise AssertionError("private zero-post account must not read activity")
                visible_text = (
                    f"{target}\n0帖子 246粉丝 864关注\n"
                    "这是私密主页\n关注即可查看其照片和视频。"
                )
                return {
                    "username": target,
                    "visibility": classify_profile_visibility(
                        visible_text,
                        has_visible_posts=False,
                        structured_is_private=False,
                    ),
                    "followers": 246,
                    "following": 864,
                    "posts": 0,
                    "activity_days": None,
                    "activity_status": "not_checked",
                }

        ChinesePrivateZeroPostWorker.connected_profiles = []
        ChinesePrivateZeroPostWorker.location_read = False
        ChinesePrivateZeroPostWorker.activity_read = False
        task = self.service.create_task(
            self.user["id"],
            name="中文私密0帖路由",
            modes=["followers"],
            targets=["private_zero_source"],
            window_ids=["private-zero-window"],
            settings={
                "location_enabled": True,
                "exclude_public_zero_posts": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=ChinesePrivateZeroPostWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual("private", result["visibility"])
        self.assertFalse(ChinesePrivateZeroPostWorker.location_read)
        self.assertFalse(ChinesePrivateZeroPostWorker.activity_read)
        self.assertIsNone(result["screening"]["location"]["passed"])
        self.assertEqual(
            "not_visible_on_private_account",
            result["screening"]["location"]["reason"],
        )
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["collection_exclusion_history"])
        self.assertEqual(
            ["private_zero_source.fan"],
            [item["username"] for item in snapshot["pending_private_accounts"]],
        )

    async def test_enabled_verified_filter_allows_unknown_and_confirmed_false(self) -> None:
        class VerificationStateWorker(FakeCollectionWorker):
            events: list[tuple[str, str]] = []

            async def read_visible_account_location(self, target: str) -> str:
                type(self).events.append((target, "location"))
                return "美国"

            async def read_visible_profile(
                self, target: str, *, include_activity: bool = False
            ) -> dict:
                type(self).events.append(
                    (target, "activity" if include_activity else "profile")
                )
                return {
                    "username": target,
                    "visibility": "public",
                    "followers": 120,
                    "following": 80,
                    "posts": 9,
                    "activity_days": 2 if include_activity else None,
                    "activity_status": (
                        "timestamp_found" if include_activity else "not_checked"
                    ),
                    "is_verified": None if "verify_unknown" in target else False,
                }

        VerificationStateWorker.events = []
        task = self.service.create_task(
            self.user["id"],
            name="认证状态严格筛选",
            modes=["followers"],
            targets=["verify_unknown", "verify_false"],
            window_ids=["verification-state-window"],
            settings={
                "exclude_verified": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=VerificationStateWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        results = {
            result["username"]: result
            for result in self.service.list_results(self.user["id"], task["id"])
        }
        unknown = results["verify_unknown.fan"]
        confirmed_false = results["verify_false.fan"]
        for result in (unknown, confirmed_false):
            self.assertTrue(result["qualified"])
            self.assertEqual(
                "public_primary_review",
                result["screening"]["routing_result"],
            )
            self.assertNotIn("verified", result["screening"])
        for username in ("verify_unknown.fan", "verify_false.fan"):
            self.assertIn((username, "location"), VerificationStateWorker.events)
            self.assertIn((username, "activity"), VerificationStateWorker.events)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(
            ["verify_unknown.fan", "verify_false.fan"],
            [item["username"] for item in snapshot["pending_public_accounts"]],
        )
        self.assertEqual([], snapshot["collection_exclusion_history"])

    async def test_public_verified_filter_excludes_only_when_enabled(self) -> None:
        VerifiedCollectionWorker.activity_reads = []
        VerifiedCollectionWorker.location_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["blue_source"],
            window_ids=["window-verified"],
            settings={
                "exclude_verified": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=VerifiedCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertFalse(result["qualified"])
        self.assertNotIn("gender", result["screening"])
        self.assertNotIn("gender_filter", result["screening"])
        self.assertFalse(result["screening"]["verified"]["passed"])
        self.assertEqual("verified_account_excluded", result["screening"]["review_reason"])
        self.assertEqual(
            [("blue_source.fan", False)],
            VerifiedCollectionWorker.activity_reads,
        )
        self.assertEqual([], VerifiedCollectionWorker.location_reads)
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual(
            "verified_account_excluded",
            snapshot["collection_exclusion_history"][0]["reason_code"],
        )

        allowed_task = self.service.create_task(
            self.user["id"],
            name="认证账号筛选关闭",
            modes=["followers"],
            targets=["blue_allowed"],
            window_ids=["window-verified-allowed"],
            settings={
                "exclude_verified": False,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        allowed_manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=VerifiedCollectionWorker,
        )
        await allowed_manager.start(self.user["id"], allowed_task["id"])
        await allowed_manager.wait(allowed_task["id"])
        allowed_result = self.service.list_results(
            self.user["id"], allowed_task["id"]
        )[0]
        self.assertTrue(allowed_result["qualified"])
        self.assertEqual("public_primary_review", allowed_result["screening"]["routing_result"])
        self.assertNotIn("verified", allowed_result["screening"])

    async def test_missing_relation_list_waits_without_false_completion_and_stop_preserves_results(self) -> None:
        MissingRelationWorker.attempts = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["visible_source", "blocked_source", "next_source"],
            window_ids=["window-relation"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MissingRelationWorker,
            relationship_no_progress_retry_limit=0,
            surface_no_progress_retry_limit=0,
            network_retry_delays=(60,),
        )
        await manager.start(self.user["id"], task["id"])
        async with asyncio.timeout(5):
            while True:
                diagnostics = await manager.runtime_diagnostics(self.user["id"], task["id"])
                if diagnostics["network_waiting_window_count"] == 1:
                    break
                await asyncio.sleep(.01)

        stored = self.service.get_task(self.user["id"], task["id"])
        targets = {target["username"]: target for target in stored["targets"]}
        self.assertEqual("waiting_network", stored["status"])
        self.assertEqual("waiting_network", targets["blocked_source"]["status"])
        self.assertEqual("window-relation", targets["blocked_source"]["current_window_id"])
        self.assertEqual("completed", targets["visible_source"]["status"])
        self.assertEqual("pending", targets["next_source"]["status"])
        self.assertEqual(["visible_source", "blocked_source"], MissingRelationWorker.attempts)
        leases = self.service.list_browser_lease_states(self.user["id"])
        self.assertEqual(["window-relation"], [lease["profile_id"] for lease in leases])
        self.assertEqual(task["id"], leases[0]["entity_id"])
        saved_results = self.service.list_results(self.user["id"], task["id"])
        self.assertEqual(1, len(saved_results))
        checkpoints = self.service.list_checkpoints(self.user["id"], task["id"])
        unavailable = next(item for item in checkpoints if item["target_id"] == targets["blocked_source"]["id"])
        self.assertEqual("waiting_network", unavailable["stage"])
        self.assertTrue(unavailable["recoverable"])
        self.assertEqual("instagram_relationship_list_no_progress", unavailable["counters"]["reason"])
        self.assertEqual("instagram_followers_list_not_rendered", unavailable["counters"]["original_reason"])
        self.assertIsNotNone(unavailable["counters"]["next_retry_at"])
        # The new policy keeps a temporarily unavailable source under its current
        # owner. A user stop must interrupt its long cooldown, preserve previous
        # results, and prevent the queued next source from being picked up.
        await asyncio.wait_for(manager.stop(self.user["id"], task["id"]), timeout=5)
        stopped = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", stopped["status"])
        stopped_targets = {target["username"]: target for target in stopped["targets"]}
        self.assertEqual("recoverable", stopped_targets["blocked_source"]["status"])
        self.assertNotEqual("completed", stopped_targets["next_source"]["status"])
        self.assertEqual(["visible_source", "blocked_source"], MissingRelationWorker.attempts)
        self.assertEqual(saved_results, self.service.list_results(self.user["id"], task["id"]))
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_legacy_post_liker_task_remains_recoverable_after_new_creation_is_disabled(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["post_source"],
            window_ids=["window-likers"],
            settings={"mode_limits": {"followers": {"per_target_limit": 10}}},
        )
        # Simulate a real v0.2.25 row. New task creation filters this mode, while
        # persisted execution/history/checkpoint contracts remain readable until
        # the old task reaches a terminal state.
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET modes_json='["post_likers"]',
                    settings_json='{"mode_limits":{"post_likers":{"per_target_limit":10}}}'
                WHERE id=?
                """,
                (task["id"],),
            )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PartialPostLikersWorker,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        stored = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("recoverable", stored["status"])
        self.assertEqual("recoverable", stored["targets"][0]["status"])
        self.assertEqual(
            ["saved.liker"],
            [item["username"] for item in self.service.list_results(self.user["id"], task["id"])],
        )
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], task["targets"][0]["id"], "post_likers"
        )
        self.assertEqual("mode_unavailable", checkpoint["stage"])
        self.assertEqual("instagram_post_likers_partial", checkpoint["counters"]["reason"])

    async def test_partial_results_are_kept_and_skipped_after_recovery(self) -> None:
        PartialFailureCollectionWorker.failures_remaining = 1
        PartialFailureCollectionWorker.profile_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["partial_source"],
            window_ids=["window-partial"],
            settings={"mode_limits": {"followers": {"per_target_limit": 3}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PartialFailureCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            diagnostics = await manager.runtime_diagnostics(
                self.user["id"], task["id"]
            )
            if diagnostics["network_waiting_window_count"] == 1:
                break
            await asyncio.sleep(0.01)

        stored = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("waiting_network", stored["status"])
        self.assertIn("等待限流解除", stored["last_error"])
        self.assertEqual("manual_intervention", diagnostics["network_waiters"][0]["state"])
        self.assertIn(
            "wait before continuing", diagnostics["network_waiters"][0]["message"]
        )
        partial_results = self.service.list_all_results(self.user["id"])
        self.assertEqual(
            {"saved.one", "saved.two"},
            {item["username"] for item in partial_results},
        )
        checkpoint = self.service.get_checkpoint(
            self.user["id"], task["id"], task["targets"][0]["id"], "followers"
        )
        self.assertEqual("waiting_network", checkpoint["stage"])
        self.assertEqual("screening_accounts", checkpoint["cursor"]["resume_stage"])
        self.assertEqual(2, checkpoint["counters"]["previous_counters"]["saved"])
        history = self.service.list_history(self.user["id"])
        self.assertEqual(2, history[0]["counts"]["collected"])
        self.assertEqual("waiting_network", history[0]["task_status"])

        await manager.resume(self.user["id"], task["id"])
        await manager.wait(task["id"])
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(3, len(self.service.list_all_results(self.user["id"])))
        self.assertEqual(2, PartialFailureCollectionWorker.profile_reads.count("saved.one"))
        self.assertEqual(2, PartialFailureCollectionWorker.profile_reads.count("saved.two"))
        self.assertEqual(3, PartialFailureCollectionWorker.profile_reads.count("rate.limited"))

    async def test_location_load_failure_auto_retries_without_persisting_unknown_or_deduping(self) -> None:
        LocationFailureOnceCollectionWorker.failures_remaining = 1
        LocationFailureOnceCollectionWorker.location_reads = 0
        task = self.service.create_task(
            self.user["id"],
            name="所在地加载失败保留断点",
            modes=["followers"],
            targets=["location_failure_source"],
            window_ids=["location-failure-window"],
            settings={
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=LocationFailureOnceCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            diagnostics = await manager.runtime_diagnostics(
                self.user["id"], task["id"]
            )
            if diagnostics["network_waiting_window_count"] == 1:
                break
            await asyncio.sleep(0.01)

        waiting = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("waiting_network", waiting["status"])
        self.assertEqual([], self.service.list_results(self.user["id"], task["id"]))
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        expected_seen = {"location_failure_source", "location_failure_source.fan"}
        self.assertEqual(len(expected_seen), snapshot["dedupe"]["total"])
        with self.database.read() as connection:
            self.assertEqual(expected_seen, {
                row[0] for row in connection.execute(
                    "SELECT a.current_username_norm FROM instagram_accounts a "
                    "JOIN global_seen g ON g.account_id=a.id"
                )
            })
        self.assertEqual(
            "instagram_location_temporarily_unavailable",
            diagnostics["network_waiters"][0]["reason"],
        )
        self.assertEqual(
            "waiting_network", diagnostics["network_waiters"][0]["state"]
        )
        self.assertIsNotNone(diagnostics["network_waiters"][0]["next_retry_at"])
        self.assertFalse(
            manager._is_profile_intervention_error(
                WorkerExecutionError(
                    "所在地冷却",
                    reason="instagram_location_temporarily_unavailable",
                    pause_required=True,
                )
            )
        )

        # No user click is required. The real worker supplies its circuit cooldown
        # as retry_after_seconds; this deterministic fixture uses 0.2 seconds.
        await asyncio.wait_for(manager.wait(task["id"]), timeout=10)
        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual("美国", result["profile"]["location_zh"])
        self.assertTrue(result["screening"]["location"]["passed"])
        self.assertEqual(2, LocationFailureOnceCollectionWorker.location_reads)

    async def test_retry_target_preserves_checkpoint_and_partial_results(self) -> None:
        PartialFailureCollectionWorker.failures_remaining = 1
        PartialFailureCollectionWorker.profile_reads = []
        task = self.service.create_task(
            self.user["id"],
            name="失败目标检查点续采",
            modes=["followers"],
            targets=["partial_retry_source"],
            window_ids=["partial-retry-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 3}}},
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=PartialFailureCollectionWorker,
        )
        await manager.start(self.user["id"], task["id"])
        for _ in range(100):
            diagnostics = await manager.runtime_diagnostics(
                self.user["id"], task["id"]
            )
            if diagnostics["network_waiting_window_count"] == 1:
                break
            await asyncio.sleep(0.01)
        await manager.stop(self.user["id"], task["id"], close_windows=False)
        interrupted = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("stopped", interrupted["status"])
        self.assertEqual("recoverable", interrupted["targets"][0]["status"])
        target_id = interrupted["targets"][0]["id"]
        checkpoint_before = self.service.get_checkpoint(
            self.user["id"], task["id"], target_id, "followers"
        )
        result_ids_before = {
            item["id"] for item in self.service.list_results(self.user["id"], task["id"])
        }
        self.assertEqual(2, len(result_ids_before))

        retried = await manager.retry_target(self.user["id"], task["id"], target_id)
        self.assertEqual(task["id"], retried["task"]["id"])
        # The endpoint returns after scheduling; the durable checkpoint/results must
        # already be intact before the next worker has a chance to finish.
        checkpoint_after_schedule = self.service.get_checkpoint(
            self.user["id"], task["id"], target_id, "followers"
        )
        self.assertEqual(checkpoint_before["id"], checkpoint_after_schedule["id"])
        self.assertTrue(
            result_ids_before.issubset(
                {
                    item["id"]
                    for item in self.service.list_results(self.user["id"], task["id"])
                }
            )
        )
        await manager.wait(task["id"])

        completed = self.service.get_task(self.user["id"], task["id"])
        self.assertEqual("completed", completed["status"])
        all_results = self.service.list_results(self.user["id"], task["id"])
        self.assertEqual(3, len(all_results))
        self.assertTrue(result_ids_before.issubset({item["id"] for item in all_results}))
        self.assertEqual(2, PartialFailureCollectionWorker.profile_reads.count("saved.one"))
        self.assertEqual(2, PartialFailureCollectionWorker.profile_reads.count("saved.two"))
        self.assertEqual(3, PartialFailureCollectionWorker.profile_reads.count("rate.limited"))

    async def test_manual_assignments_run_on_the_selected_initial_windows(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["manual_one", "manual_two"],
            window_ids=["manual-window-a", "manual-window-b"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
            assignment_mode="manual",
        )
        self.service.set_manual_assignments(
            self.user["id"],
            task["id"],
            {
                "manual-window-a": "manual_two",
                "manual-window-b": "manual_one",
            },
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=FakeCollectionWorker
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        completed = self.service.get_task(self.user["id"], task["id"])
        assigned = {target["username"]: target["current_window_id"] for target in completed["targets"]}
        self.assertEqual("manual-window-b", assigned["manual_one"])
        self.assertEqual("manual-window-a", assigned["manual_two"])

    async def test_gpt_failure_is_recorded_without_losing_deterministic_result(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["gpt_source"],
            window_ids=["gpt-window"],
            settings={
                "gpt_enabled": True,
                "mode_limits": {"followers": {"followers_max": 500, "per_target_limit": 1}},
            },
        )
        manager = ExecutionManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=FakeCollectionWorker,
            reviewer_factory=UnavailableReviewer,
        )
        await manager.start(self.user["id"], task["id"])
        await manager.wait(task["id"])

        completed = self.service.get_task(self.user["id"], task["id"])
        result = self.service.list_results(self.user["id"], task["id"])[0]
        self.assertEqual("completed", completed["status"])
        self.assertTrue(result["qualified"])
        self.assertEqual("gpt_review_failed", result["screening"]["gpt"]["error"])
        self.assertEqual(
            "insufficient_visible_data",
            result["screening"]["gpt"]["result"]["review_status"],
        )
        self.assertNotIn("marketing", result["screening"]["gpt"]["result"])
        self.assertNotIn("gender", result["screening"]["gpt"]["result"])

    async def test_abnormal_collection_can_be_kept_paused_without_a_live_worker(self) -> None:
        task = self.service.create_task(
            self.user["id"],
            name="采集任务",
            modes=["followers"],
            targets=["recoverable_source"],
            window_ids=["recoverable-window"],
            settings={"mode_limits": {"followers": {"per_target_limit": 1}}},
        )
        target_id = task["targets"][0]["id"]
        self.service.upsert_checkpoint(
            self.user["id"],
            task["id"],
            target_id,
            mode="followers",
            stage="scrolling_visible_list",
            cursor={"last_username": "saved-user"},
            counters={"seen": 12},
        )
        self.service.set_task_runtime_status(
            self.user["id"], task["id"], "recoverable", error="browser disconnected"
        )
        manager = ExecutionManager(
            self.service, FakeBitBrowserClient(), worker_factory=FakeCollectionWorker
        )
        paused = await manager.pause(self.user["id"], task["id"])
        self.assertEqual("paused", paused["status"])
        self.assertEqual(1, len(self.service.list_checkpoints(self.user["id"], task["id"])))

    async def test_greeting_direct_structure_failure_pauses_before_burning_queue(self) -> None:
        StructuralGreetingFailureWorker.calls = 0
        manager = ActionCampaignManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=StructuralGreetingFailureWorker,
        )
        campaign = await manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="greeting-structure-window",
            targets=["first.person", "second.person", "third.person"],
            message="Hello",
            interval="1 秒",
            limit=3,
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["failed", "pending", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(1, StructuralGreetingFailureWorker.calls)
        self.assertIn("Direct 页面结构异常", stored["last_error"])
        self.assertEqual(
            "instagram_direct_composer_not_ready",
            stored["attempts"][0]["details"]["reason"],
        )
        with self.database.read() as connection:
            success_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(0, success_count)

    async def test_missing_greeting_recipient_waits_then_continues_next_target(self) -> None:
        class WaitTrackingManager(ActionCampaignManager):
            wait_calls = 0

            async def _wait_for_next_action_target(
                self, control: object, campaign: dict
            ) -> None:
                del control, campaign
                type(self).wait_calls += 1

        MissingRecipientThenSuccessWorker.calls = 0
        WaitTrackingManager.wait_calls = 0
        manager = WaitTrackingManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=MissingRecipientThenSuccessWorker,
        )
        campaign = await manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="greeting-missing-recipient-window",
            targets=["missing.person", "available.person"],
            message="Hello",
            interval="1 秒",
            limit=2,
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(
            ["failed", "confirmed"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(2, MissingRecipientThenSuccessWorker.calls)
        self.assertEqual(1, WaitTrackingManager.wait_calls)

    async def test_persistent_action_campaign_and_unknown_no_retry(self) -> None:
        self.assertEqual((45, 90), parse_interval("45–90 秒"))
        self.assertEqual((8, 15), parse_interval("8-15 秒"))
        self.assertTrue(all(8 <= choose_interval_seconds(8, 15) <= 15 for _ in range(50)))
        action_manager = ActionCampaignManager(
            self.service, FakeBitBrowserClient(), worker_factory=FakeActionWorker
        )
        campaign = await action_manager.start_campaign(
            self.user["id"],
            operation="follow",
            profile_id="action-window",
            targets=["person_one"],
            message=None,
            interval="45–90 秒",
            limit=1,
        )
        await action_manager.wait(campaign["id"])
        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual("confirmed", stored["targets"][0]["status"])
        self.assertEqual("confirmed", stored["attempts"][0]["status"])

        UnknownActionWorker.attempts = 0
        unknown_manager = ActionCampaignManager(
            self.service, FakeBitBrowserClient(), worker_factory=UnknownActionWorker
        )
        unknown = await unknown_manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="unknown-window",
            targets=["person_two"],
            message="Hello",
            interval="45–90 秒",
            limit=1,
        )
        await unknown_manager.wait(unknown["id"])
        stored_unknown = self.service.get_action_campaign(self.user["id"], unknown["id"])
        self.assertEqual("paused", stored_unknown["status"])
        self.assertEqual("unknown", stored_unknown["targets"][0]["status"])
        self.assertEqual("Hello", stored_unknown["attempts"][0]["message"])
        self.assertEqual(
            "Hello", stored_unknown["attempts"][0]["details"]["greeting_message"]
        )
        with self.database.read() as connection:
            unknown_success_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (unknown["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(0, unknown_success_count)
        self.assertEqual(1, UnknownActionWorker.attempts)
        with self.assertRaises(ConflictError):
            await unknown_manager.resume(self.user["id"], unknown["id"])
        self.assertEqual(1, UnknownActionWorker.attempts)

        mixed = self.service.create_action_campaign(
            self.user["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="mixed-window",
            targets=["uncertain_person", "remaining_person"],
            message=None,
            interval_min_seconds=1,
            interval_max_seconds=1,
            limit_count=2,
        )
        self.service.set_campaign_status(self.user["id"], mixed["id"], "running")
        first_target = self.service.next_action_target(self.user["id"], mixed["id"])
        self.assertIsNotNone(first_target)
        first_attempt = self.service.start_action_attempt(
            self.user["id"], mixed["id"], first_target["id"]
        )
        self.service.finish_action_attempt(
            self.user["id"],
            mixed["id"],
            first_attempt,
            status="unknown",
            details={"message": "outcome must be checked manually"},
        )
        self.service.set_campaign_status(self.user["id"], mixed["id"], "paused")
        safe_resume_manager = ActionCampaignManager(
            self.service, FakeBitBrowserClient(), worker_factory=FakeActionWorker
        )
        await safe_resume_manager.resume(self.user["id"], mixed["id"])
        await safe_resume_manager.wait(mixed["id"])
        mixed_stored = self.service.get_action_campaign(self.user["id"], mixed["id"])
        self.assertEqual("unknown", mixed_stored["targets"][0]["status"])
        self.assertEqual("confirmed", mixed_stored["targets"][1]["status"])
        self.assertEqual("paused", mixed_stored["status"])

    async def test_unexpected_action_campaign_failure_is_terminal_and_lists_pending_targets(self) -> None:
        manager = ActionCampaignManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=ExplodingActionConnectWorker,
        )
        campaign = await manager.start_campaign(
            self.user["id"],
            operation="follow",
            profile_id="unexpected-action-window",
            targets=["pending_person_one", "pending_person_two"],
            message=None,
            interval="1 秒",
            limit=2,
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("failed", stored["status"])
        self.assertIsNotNone(stored["finished_at"])
        self.assertEqual({"failed"}, {target["status"] for target in stored["targets"]})
        self.assertTrue(
            all("unexpected browser bootstrap failure" in target["last_error"] for target in stored["targets"])
        )
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())

    async def test_workbench_manual_action_is_published_before_browser_completion(self) -> None:
        BlockingManualActionWorker.started = asyncio.Event()
        BlockingManualActionWorker.release = asyncio.Event()
        manager = ActionCampaignManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=BlockingManualActionWorker,
        )
        campaign = await manager.submit_manual_action(
            self.user["id"],
            operation="follow",
            profile_id="background-manual-window",
            target="manual_background_target",
            source_target="source_account",
            message=None,
        )

        self.assertEqual("manual", campaign["execution_type"])
        self.assertEqual("running", campaign["status"])
        self.assertEqual("pending", campaign["targets"][0]["status"])
        assert BlockingManualActionWorker.started is not None
        await asyncio.wait_for(BlockingManualActionWorker.started.wait(), timeout=2)
        in_flight = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("running", in_flight["status"])
        self.assertEqual("running", in_flight["targets"][0]["status"])

        assert BlockingManualActionWorker.release is not None
        BlockingManualActionWorker.release.set()
        await manager.wait(campaign["id"])
        completed = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("confirmed", completed["targets"][0]["status"])
        with self.database.read() as connection:
            success_count = int(connection.execute(
                "SELECT COUNT(*) FROM action_success_ledger WHERE owner_user_id=?",
                (self.user["id"],),
            ).fetchone()[0])
        self.assertEqual(1, success_count)
        self.assertEqual([], self.service.list_browser_lease_states(self.user["id"]))

    async def test_greeting_campaign_chooses_and_audits_one_stable_message_per_target(self) -> None:
        CapturingGreetingWorker.calls = []
        action_manager = ActionCampaignManager(
            self.service,
            FakeBitBrowserClient(),
            worker_factory=CapturingGreetingWorker,
        )
        library = ["Hello!", "Nice to meet you.", "你好，很高兴认识你！"]
        campaign = await action_manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="greeting-library-window",
            targets=["alpha.person", "beta.person", "gamma.person"],
            message=library[0],
            messages=library,
            interval="1 秒",
            limit=3,
        )
        await action_manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual(library, stored["messages"])
        actual_by_target = {
            target: message for operation, target, message in CapturingGreetingWorker.calls
            if operation == "greet"
        }
        self.assertEqual(3, len(actual_by_target))
        target_by_id = {target["id"]: target["username"] for target in stored["targets"]}
        for attempt in stored["attempts"]:
            target = target_by_id[attempt["target_id"]]
            expected = choose_greeting_message(campaign["id"], target, library)
            self.assertEqual(expected, actual_by_target[target])
            self.assertEqual(expected, attempt["message"])
            self.assertEqual(expected, attempt["details"]["greeting_message"])
            self.assertEqual("confirmed", attempt["status"])

        # Stable for resume/re-render, while the campaign UUID provides a fresh
        # shuffle for another campaign. A broad target sample also guards against
        # accidentally always selecting the first library item.
        sample = {
            choose_greeting_message("fixed-campaign", f"person-{index}", library)
            for index in range(100)
        }
        self.assertGreater(len(sample), 1)


FASTAPI_TESTS_AVAILABLE = bool(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"))


@unittest.skipUnless(FASTAPI_TESTS_AVAILABLE, "FastAPI/httpx development dependencies are not installed")
class FastAPIIntegrationTestCase(unittest.TestCase):
    def test_startup_header_and_authenticated_task_route(self) -> None:
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.main import create_app

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            settings = Settings(
                startup_token="test-startup-token-that-is-long-enough-123",
                database_path=path / "api.sqlite3",
                data_dir=path,
            )
            app = create_app(settings, bitbrowser=FakeBitBrowserClient())
            app.state.execution_manager.worker_factory = FakeCollectionWorker
            startup_headers = {"X-Startup-Token": settings.startup_token}
            with TestClient(app) as client:
                self.assertEqual(401, client.get("/health").status_code)
                self.assertEqual(200, client.get("/health", headers=startup_headers).status_code)
                register = client.post(
                    "/v1/auth/register",
                    headers=startup_headers,
                    json={"username": "api-user", "password": PASSWORD},
                )
                self.assertEqual(201, register.status_code)
                login = client.post(
                    "/v1/auth/login",
                    headers=startup_headers,
                    json={"username": "api-user", "password": PASSWORD},
                )
                self.assertEqual(200, login.status_code)
                session_headers = startup_headers | {"Authorization": f"Bearer {login.json()['token']}"}
                created = client.post(
                    "/v1/tasks",
                    headers=session_headers,
                    json={
                        "name": "采集任务",
                        "modes": ["followers"],
                        "targets": ["source_account"],
                        "window_ids": ["window-a"],
                        "settings": {"followers_max": 3000},
                    },
                )
                self.assertEqual(201, created.status_code, created.text)
                task_id = created.json()["id"]
                desktop_tasks = client.get("/api/tasks", headers=session_headers)
                self.assertEqual(200, desktop_tasks.status_code, desktop_tasks.text)
                self.assertEqual(1, desktop_tasks.json()["total"])
                desktop_task = desktop_tasks.json()["tasks"][0]
                self.assertEqual(task_id, desktop_task["id"])
                self.assertEqual(["window-a"], desktop_task["window_ids"])
                self.assertEqual("source_account", desktop_task["targets"][0]["username"])
                desktop_detail = client.get(f"/api/tasks/{task_id}", headers=session_headers)
                self.assertEqual(200, desktop_detail.status_code, desktop_detail.text)
                self.assertEqual(task_id, desktop_detail.json()["id"])
                self.assertEqual("pending", desktop_detail.json()["targets"][0]["status"])
                started = client.post(
                    f"/v1/tasks/{task_id}/start",
                    headers=session_headers,
                    json={"expected_version": 1},
                )
                self.assertEqual(200, started.status_code, started.text)
                self.assertEqual("running", started.json()["status"])


if __name__ == "__main__":
    unittest.main()
