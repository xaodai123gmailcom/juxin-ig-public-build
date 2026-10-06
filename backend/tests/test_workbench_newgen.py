from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app import __version__
from app.action_manager import ActionCampaignManager
from app.database import Database
from app.errors import ConflictError, ValidationError
from app.main import create_app
from app.service import CoreService
from app.schemas import DesktopTaskCreateRequest, TaskSettingsRequest


PASSWORD = "correct horse battery staple"


class EmptyBitBrowser:
    """Deterministic empty provider: an empty inventory is not demo data."""

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        return {
            "windows": [],
            "total": 0,
            "provider_success": True,
            "stale": False,
            "connection": {
                "state": "connected",
                "connected": True,
                "source": "test-empty-provider",
            },
        }


class OneWindowBitBrowser(EmptyBitBrowser):
    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        return {
            "windows": [
                {
                    "id": "window-counts",
                    "name": "计数窗口",
                    "group": "测试",
                    "serial_number": 1,
                    "provider_order": 1,
                    "is_open": True,
                    "window_state": "ready",
                    "generation": 1,
                    "ready": True,
                    "opening": False,
                }
            ],
            "total": 1,
            "provider_success": True,
            "stale": False,
            "connection": {
                "state": "connected",
                "connected": True,
                "source": "test-one-window-provider",
            },
        }


class NewGenerationWorkbenchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "collector.sqlite3"
        self.database = Database(self.db_path)
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.owner = self.service.register_user("workbench-owner", PASSWORD)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def claim(
        self,
        username: str,
        *,
        owner_id: str | None = None,
        source: str = "followers",
        source_target: str = "source-account",
    ) -> dict[str, Any]:
        return self.service.claim_workbench_identity(
            owner_id or self.owner["id"],
            username=username,
            source=source,
            source_target=source_target,
        )

    def candidate(
        self,
        username: str,
        visibility: str,
        *,
        profile: dict[str, Any] | None = None,
        screening: dict[str, Any] | None = None,
        review_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        claim = self.claim(username)
        self.assertTrue(claim["claimed"])
        return self.service.create_workbench_candidate(
            self.owner["id"],
            claim_id=claim["claim_id"],
            username=username,
            visibility=visibility,
            profile=profile or {"display_name": username},
            screening=screening or {"location_country": "United States"},
            review_cache=review_cache or {},
            source_mode="followers",
            source_target="source-account",
        )

    def test_location_gate_cannot_be_disabled_and_positive_collection_limit_is_unbounded(self) -> None:
        # Strict HTTP models reject an explicit bypass.
        with self.assertRaises(Exception):
            TaskSettingsRequest(location_enabled=False)
        with self.assertRaises(Exception):
            DesktopTaskCreateRequest(
                targets=["source"],
                window_ids=["window"],
                modes=["followers"],
                read_location=False,
            )
        desktop_request = DesktopTaskCreateRequest(
            targets=["source"],
            window_ids=["window"],
            modes=["followers"],
            per_target_limit=9_876_543_210_123_456_789,
        )
        self.assertEqual(
            9_876_543_210_123_456_789,
            desktop_request.per_target_limit,
        )

        # Recovery and internal callers may still carry an old false value; the
        # service upgrades it to the mandatory first gate instead of persisting
        # or executing a bypass.
        task = self.service.create_task(
            self.owner["id"],
            name="legacy location setting",
            modes=["followers"],
            targets=["source"],
            window_ids=["window"],
            settings={
                "location_enabled": False,
                "mode_limits": {"followers": {"per_target_limit": 987_654_321}},
            },
        )
        self.assertTrue(task["settings"]["location_enabled"])
        self.assertTrue(task["settings"]["unlimited_relation_collection"])
        self.assertNotIn(
            "per_target_limit",
            task["settings"]["mode_limits"]["followers"],
        )

        # A paused task persisted by an older release may keep its positive limit
        # on disk, but the execution view must neutralize it during recovery.
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET settings_json=?
                WHERE id=?
                """,
                (
                    json.dumps({
                        "location_enabled": False,
                        "mode_limits": {
                            "followers": {"per_target_limit": 1_234_567_890}
                        },
                    }),
                    task["id"],
                ),
            )
        recovered = self.service.get_task(self.owner["id"], task["id"])
        self.assertTrue(recovered["settings"]["location_enabled"])
        self.assertTrue(recovered["settings"]["unlimited_relation_collection"])
        self.assertNotIn(
            "per_target_limit",
            recovered["settings"]["mode_limits"]["followers"],
        )

    def test_atomic_global_claim_has_exactly_one_winner_and_no_duplicate_history(self) -> None:
        second_owner = self.service.register_user("second-owner", PASSWORD)
        barrier = threading.Barrier(2)

        def race(owner_id: str) -> dict[str, Any]:
            barrier.wait(timeout=5)
            return self.claim("Same.Person", owner_id=owner_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race, [self.owner["id"], second_owner["id"]]))

        claimed = [result for result in results if result["outcome"] == "claimed"]
        duplicates = [result for result in results if result["outcome"] == "duplicate"]
        self.assertEqual(1, len(claimed))
        self.assertEqual(1, len(duplicates))
        self.assertTrue(claimed[0]["should_read_location"])
        self.assertTrue(claimed[0]["should_collect_profile"])
        self.assertFalse(duplicates[0]["should_read_location"])
        self.assertFalse(duplicates[0]["should_collect_profile"])
        self.assertFalse(duplicates[0]["should_create_history"])
        self.assertEqual(claimed[0]["account_id"], duplicates[0]["account_id"])

        with self.database.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0])
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_identity_claims"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM workbench_candidates").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_collection_exclusions"
                ).fetchone()[0],
            )

    def test_parallel_public_private_claims_share_one_global_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        def collect(value):
            handle,visibility=value
            claim=self.service.claim_workbench_identity(self.owner['id'],username=handle,source='followers',source_target=visibility)
            if claim['claimed']:
                self.service.create_workbench_candidate(self.owner['id'],claim_id=claim['claim_id'],username=handle,visibility=visibility,profile={'username':handle},screening={'location_country':'United States'},review_cache={})
            return claim
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(collect,[('@SAME.Handle','public'),('same.handle','private'),('https://www.instagram.com/same.handle/','public'),('Same.Handle','private')]))
        self.assertEqual(1,sum(x['claimed'] for x in results))
        with self.database.read() as c:
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM workbench_candidates').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM global_seen').fetchone()[0])

    def test_repeat_observation_backfills_id_before_rename_across_visibility(self):
        first=self.claim("first.handle")
        self.service.create_workbench_candidate(self.owner["id"],claim_id=first["claim_id"],username="first.handle",visibility="private",profile={"username":"first.handle"},screening={},review_cache={})
        repeat=self.service.claim_workbench_identity(self.owner["id"],username="FIRST.HANDLE",instagram_user_id="987654321",source="following")
        self.assertTrue(repeat["duplicate"])
        renamed=self.service.claim_workbench_identity(self.owner["id"],username="renamed.handle",instagram_user_id="987654321",source="followers")
        self.assertTrue(renamed["duplicate"])
        self.assertEqual(first["account_id"],renamed["account_id"])
        with self.database.read() as c:
            self.assertEqual(1,c.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0])
            self.assertEqual(1,c.execute("SELECT COUNT(*) FROM workbench_candidates").fetchone()[0])

    def test_stable_instagram_id_backfill_blocks_a_renamed_duplicate_without_merging(self) -> None:
        original = self.claim("original.name")
        confirmed = self.service.confirm_workbench_identity(
            self.owner["id"],
            claim_id=original["claim_id"],
            username="original.name",
            instagram_user_id="111111111111",
        )
        self.assertTrue(confirmed["confirmed"])
        self.assertTrue(confirmed["stable_id_bound"])

        renamed = self.service.claim_workbench_identity(
            self.owner["id"],
            username="renamed.name",
            instagram_user_id="111111111111",
            source="followers",
            source_target="another-source",
        )
        self.assertEqual("duplicate", renamed["outcome"])
        self.assertEqual(original["account_id"], renamed["account_id"])
        self.assertFalse(renamed["should_collect_profile"])
        with self.database.read() as connection:
            account = connection.execute(
                "SELECT * FROM instagram_accounts WHERE id=?",
                (original["account_id"],),
            ).fetchone()
            self.assertEqual("111111111111", account["instagram_user_id"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM global_seen"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM instagram_accounts"
            ).fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM instagram_username_aliases"
            ).fetchone()[0])

        first = self.claim("first.conflict")
        second = self.claim("second.conflict")
        self.service.confirm_workbench_identity(
            self.owner["id"], claim_id=first["claim_id"],
            username="first.conflict", instagram_user_id="222222222222",
        )
        self.service.confirm_workbench_identity(
            self.owner["id"], claim_id=second["claim_id"],
            username="second.conflict", instagram_user_id="333333333333",
        )
        conflict = self.service.confirm_workbench_identity(
            self.owner["id"], claim_id=first["claim_id"],
            username="first.conflict", instagram_user_id="333333333333",
        )
        self.assertTrue(conflict["duplicate"])
        self.assertFalse(conflict["should_continue"])
        self.assertEqual(second["account_id"], conflict["account_id"])
        with self.database.read() as connection:
            self.assertEqual(
                "222222222222",
                connection.execute(
                    "SELECT instagram_user_id FROM instagram_accounts WHERE id=?",
                    (first["account_id"],),
                ).fetchone()[0],
            )

    def test_late_stable_id_collapses_empty_username_claim_without_count_inflation(self) -> None:
        original = self.claim("original.late")
        self.service.confirm_workbench_identity(
            self.owner["id"],
            claim_id=original["claim_id"],
            username="original.late",
            instagram_user_id="444444444444",
        )

        # Relationship lists expose only a username at the atomic first gate.  The
        # stable id becomes visible later from the already-open profile header.
        renamed = self.claim("renamed.late")
        self.assertEqual(2, renamed["global_dedupe_count"])
        conflict = self.service.confirm_workbench_identity(
            self.owner["id"],
            claim_id=renamed["claim_id"],
            username="renamed.late",
            instagram_user_id="444444444444",
        )
        self.assertTrue(conflict["duplicate"])
        self.assertTrue(conflict["placeholder_removed"])
        self.assertEqual(original["account_id"], conflict["account_id"])

        with self.database.read() as connection:
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0]
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM instagram_accounts"
                ).fetchone()[0],
            )
            aliases = connection.execute(
                """
                SELECT username_norm FROM instagram_username_aliases
                WHERE account_id=? ORDER BY username_norm
                """,
                (original["account_id"],),
            ).fetchall()
            self.assertEqual(
                ["original.late", "renamed.late"],
                [row["username_norm"] for row in aliases],
            )

    def test_public_and_private_review_queues_route_to_separate_approved_pages(self) -> None:
        avatar_preview = "data:image/jpeg;base64,YXZhdGFy"
        post_preview = "data:image/jpeg;base64,cG9zdA=="
        public_candidate = self.candidate(
            "public.person",
            "public",
            review_cache={
                "avatar_preview": avatar_preview,
                "recent_posts": [{"post_url": "https://www.instagram.com/p/one/", "preview_data_url": post_preview}],
            },
        )
        private_candidate = self.candidate(
            "private.person",
            "private",
            profile={"followers": 128, "following": 251, "posts": 19},
            review_cache={"avatar_preview": avatar_preview, "recent_posts": [{"preview_data_url": post_preview}]},
        )

        pending = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([public_candidate["id"]], [row["id"] for row in pending["pending_public_accounts"]])
        self.assertEqual([private_candidate["id"]], [row["id"] for row in pending["pending_private_accounts"]])
        self.assertEqual(
            {"avatar_preview": avatar_preview},
            pending["pending_public_accounts"][0]["review_cache"],
        )
        self.assertNotIn(
            "recent_posts", pending["pending_public_accounts"][0]["profile"]
        )
        self.assertEqual({"avatar_preview": avatar_preview}, pending["pending_private_accounts"][0]["review_cache"])
        self.assertEqual(19, pending["pending_private_accounts"][0]["profile"]["posts"])
        self.assertEqual(1, pending["counts"]["pending_private"])
        self.assertEqual(1, pending["counts"]["pending_private_primary"])
        self.assertEqual(0, pending["counts"]["pending_private_secondary"])

        public_result = self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=public_candidate["id"], decision="approved"
        )
        private_result = self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=private_candidate["id"], decision="approved"
        )
        self.assertEqual("approved_public", public_result["destination"])
        self.assertEqual("approved_private", private_result["destination"])

        approved = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([], approved["pending_public_accounts"])
        self.assertEqual([], approved["pending_private_accounts"])
        self.assertEqual([public_candidate["id"]], [row["id"] for row in approved["approved_public_accounts"]])
        self.assertEqual([private_candidate["id"]], [row["id"] for row in approved["approved_private_accounts"]])
        self.assertEqual(1, approved["counts"]["approved_public"])
        self.assertEqual(1, approved["counts"]["approved_private"])
        self.assertEqual({"avatar_preview": avatar_preview}, approved["approved_public_accounts"][0]["review_cache"])
        self.assertEqual({"avatar_preview": avatar_preview}, approved["approved_private_accounts"][0]["review_cache"])
        self.assertEqual(19, approved["approved_private_accounts"][0]["profile"]["posts"])

    def test_private_review_tier_counts_are_complete_beyond_snapshot_row_limit(self) -> None:
        self.candidate(
            "private.primary.explicit",
            "private",
            screening={"review_tier": "primary"},
        )
        self.candidate(
            "private.secondary",
            "private",
            screening={"review_tier": "secondary"},
        )
        self.candidate("private.primary.legacy", "private", screening={})

        snapshot = self.service.get_workbench_snapshot(
            self.owner["id"], limit=1
        )

        self.assertEqual(1, len(snapshot["pending_private_accounts"]))
        self.assertTrue(snapshot["has_more"]["pending_private_accounts"])
        self.assertEqual(3, snapshot["counts"]["pending_private"])
        self.assertEqual(3, snapshot["counts"]["pending_private_primary"])
        self.assertEqual(0, snapshot["counts"]["pending_private_secondary"])
        self.assertEqual(3, snapshot["counts"]["pending_review"])

    def test_public_secondary_candidate_api_is_retired_for_both_legacy_markers(self) -> None:
        for index, screening in enumerate(
            (
                {"review_tier": "secondary"},
                {"routing_result": "public_secondary_review"},
                {"basic": {"passed": False, "reason_codes": ["posts_above_max"]}},
                {"basic": {"passed": None, "reason_codes": ["following_unknown"]}},
                {"activity": {"checked": True, "passed": False}},
                {
                    "verified": {
                        "enabled": True,
                        "checked": False,
                        "passed": None,
                    }
                },
            ),
            start=1,
        ):
            username = f"retired.public.secondary.{index}"
            claim = self.claim(username)
            with self.assertRaisesRegex(ValidationError, "collection-excluded"):
                self.service.create_workbench_candidate(
                    self.owner["id"],
                    claim_id=claim["claim_id"],
                    username=username,
                    visibility="public",
                    profile={"display_name": username},
                    screening=screening,
                    review_cache={"avatar_preview": "data:image/jpeg;base64,b2xk"},
                    source_mode="followers",
                    source_target="source-account",
                )
        unknown_username = "public.location.unknown.review"
        unknown_claim = self.claim(unknown_username)
        unknown = self.service.create_workbench_candidate(
            self.owner["id"],
            claim_id=unknown_claim["claim_id"],
            username=unknown_username,
            visibility="public",
            profile={"display_name": unknown_username},
            screening={"location": {"enabled": True, "passed": None, "reason": "country_not_visible"}},
            review_cache={},
            source_mode="followers",
            source_target="source-account",
        )
        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([unknown["id"]], [row["id"] for row in snapshot["pending_public_accounts"]])
        self.assertEqual(1, snapshot["counts"]["pending_public"])

    def test_public_candidate_does_not_require_a_disabled_activity_filter(self) -> None:
        claim = self.claim("public.activity.disabled")
        candidate = self.service.create_workbench_candidate(
            self.owner["id"],
            claim_id=claim["claim_id"],
            username="public.activity.disabled",
            visibility="public",
            profile={"display_name": "Activity disabled"},
            screening={
                "review_tier": "primary",
                "routing_result": "public_primary_review",
                "basic": {"passed": True, "reason_codes": []},
                "location": {"enabled": True, "checked": True, "passed": True},
                "activity": {
                    "enabled": False,
                    "checked": False,
                    "passed": None,
                    "reason": "page_read_incomplete",
                },
            },
            review_cache={},
            source_mode="followers",
            source_target="source-account",
        )
        self.assertEqual("public.activity.disabled", candidate["username"])
        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(
            ["public.activity.disabled"],
            [item["username"] for item in snapshot["pending_public_accounts"]],
        )

    def test_only_confirmed_zero_post_activity_unknown_can_enter_public_review(self) -> None:
        base_screening = {
            "review_tier": "primary",
            "routing_result": "public_primary_review",
            "basic": {"passed": True, "reason_codes": []},
            "location": {"enabled": True, "checked": True, "passed": True},
            "activity": {
                "enabled": True,
                "checked": True,
                "passed": None,
                "reason": "activity_no_posts",
                "retained_for_manual_review": True,
            },
        }
        username = "public.zero.posts.review"
        claim = self.claim(username)
        candidate = self.service.create_workbench_candidate(
            self.owner["id"],
            claim_id=claim["claim_id"],
            username=username,
            visibility="public",
            profile={"display_name": "Zero posts", "posts": 0},
            screening=base_screening,
            review_cache={},
            source_mode="followers",
            source_target="source-account",
        )
        self.assertEqual(username, candidate["username"])

        invalid_cases = (
            ("public.zero.posts.spoofed", {"posts": 1}, base_screening),
            (
                "public.zero.posts.unchecked",
                {"posts": 0},
                {
                    **base_screening,
                    "activity": {**base_screening["activity"], "checked": False},
                },
            ),
            (
                "public.zero.posts.failed",
                {"posts": 0},
                {
                    **base_screening,
                    "activity": {**base_screening["activity"], "passed": False},
                },
            ),
        )
        for invalid_username, profile, screening in invalid_cases:
            with self.subTest(username=invalid_username):
                invalid_claim = self.claim(invalid_username)
                with self.assertRaisesRegex(ValidationError, "collection-excluded"):
                    self.service.create_workbench_candidate(
                        self.owner["id"],
                        claim_id=invalid_claim["claim_id"],
                        username=invalid_username,
                        visibility="public",
                        profile=profile,
                        screening=screening,
                        review_cache={},
                        source_mode="followers",
                        source_target="source-account",
                    )

    def test_v14_migrates_only_legacy_pending_public_secondary_to_exclusion(self) -> None:
        public_cache = "data:image/jpeg;base64,cHVibGljLXNlY29uZGFyeQ=="
        public_candidate = self.candidate(
            "legacy.public.secondary",
            "public",
            profile={
                "display_name": "Legacy Public",
                "location_zh": "美国",
                "followers": 999,
            },
            screening={"review_tier": "primary"},
            review_cache={"avatar_preview": public_cache},
        )
        private_candidate = self.candidate(
            "legacy.private.secondary",
            "private",
            profile={"display_name": "Legacy Private", "followers": None},
            screening={
                "review_tier": "secondary",
                "routing_result": "private_secondary_review",
            },
            review_cache={
                "avatar_preview": "data:image/jpeg;base64,cHJpdmF0ZQ=="
            },
        )
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE workbench_candidates
                SET screening_json=?
                WHERE id=?
                """,
                (
                    json.dumps(
                        {
                            "review_tier": "secondary",
                            "routing_result": "public_secondary_review",
                            "review_reason": "collection_conditions_not_passed",
                        }
                    ),
                    public_candidate["id"],
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=14")
            revision_before = int(
                connection.execute(
                    "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                ).fetchone()[0]
            )
            global_seen_before = int(
                connection.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0]
            )
            claims_before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_identity_claims"
                ).fetchone()[0]
            )

        self.database.initialize()
        migrated = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([], migrated["pending_public_accounts"])
        self.assertEqual(
            [private_candidate["id"]],
            [item["id"] for item in migrated["pending_private_accounts"]],
        )
        exclusion = next(
            item
            for item in migrated["collection_exclusion_history"]
            if item["username"] == "legacy.public.secondary"
        )
        self.assertEqual("legacy_public_secondary_review", exclusion["reason_code"])
        self.assertEqual("Legacy Public", exclusion["profile"]["display_name"])
        self.assertEqual("美国", exclusion["location_country"])
        self.assertEqual(0, migrated["counts"]["pending_public"])
        self.assertEqual(1, migrated["counts"]["pending_private"])
        self.assertEqual(1, migrated["counts"]["pending_private_primary"])
        self.assertEqual(0, migrated["counts"]["pending_private_secondary"])
        self.assertEqual(1, migrated["counts"]["collection_excluded"])

        with self.database.read() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM workbench_candidates WHERE id=?",
                    (public_candidate["id"],),
                ).fetchone()
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_candidates WHERE instr(review_cache_json, ?) > 0",
                    (public_cache,),
                ).fetchone()[0],
            )
            self.assertEqual(
                global_seen_before,
                connection.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0],
            )
            self.assertEqual(
                claims_before,
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_identity_claims"
                ).fetchone()[0],
            )
            revision_after = int(
                connection.execute(
                    "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                ).fetchone()[0]
            )
            self.assertEqual(revision_before + 1, revision_after)

        self.database.initialize()
        with self.database.read() as connection:
            self.assertEqual(
                revision_after,
                connection.execute(
                    "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_collection_exclusions"
                ).fetchone()[0],
            )

    def test_inline_images_are_allowed_only_in_bounded_review_cache(self) -> None:
        inline_image = "data:image/jpeg;base64,Ym91bmRlZA=="
        candidate = self.candidate(
            "cache.image.person",
            "public",
            review_cache={"avatar_preview": inline_image},
        )
        self.assertEqual(inline_image, candidate["review_cache"]["avatar_preview"])

        claim = self.claim("profile.inline.binary")
        with self.assertRaises(ValidationError):
            self.service.create_workbench_candidate(
                self.owner["id"],
                claim_id=claim["claim_id"],
                username="profile.inline.binary",
                visibility="public",
                profile={"avatar": inline_image},
                screening={},
                review_cache={},
                source_mode="followers",
                source_target="source-account",
            )

    def test_preview_size_boundaries_degrade_cache_without_losing_business_records(self) -> None:
        max_cache_bytes = 256 * 1024
        task = self.service.create_task(
            self.owner["id"],
            name="preview-boundary-live-task",
            modes=["followers"],
            targets=["preview.boundary.source"],
            settings={"location_enabled": True},
            window_ids=["preview-boundary-window"],
        )

        inline_prefix = "data:image/jpeg;base64,"
        empty_payload_bytes = len(
            json.dumps(
                {"avatar_preview": inline_prefix},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        exact_cache = {
            "avatar_preview": inline_prefix
            + ("A" * (max_cache_bytes - empty_payload_bytes))
        }
        self.assertEqual(
            max_cache_bytes,
            len(
                json.dumps(
                    exact_cache,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ),
        )
        exact = self.candidate(
            "preview.boundary.exact",
            "public",
            review_cache=exact_cache,
        )
        self.assertEqual(exact_cache, exact["review_cache"])

        post_url = "https://www.instagram.com/p/oversized-preview/"
        oversized_inline = self.candidate(
            "preview.boundary.inline",
            "public",
            review_cache={
                "recent_posts": [
                    {
                        "post_url": post_url,
                        "preview_data_url": inline_prefix + ("B" * max_cache_bytes),
                    }
                ]
            },
        )
        self.assertEqual(
            {},
            oversized_inline["review_cache"],
        )

        oversized_list_inline = self.candidate(
            "preview.boundary.list_inline",
            "public",
            review_cache={
                "screenshots": [
                    inline_prefix + ("E" * max_cache_bytes),
                    "https://example.invalid/retained-preview.jpg",
                ]
            },
        )
        self.assertEqual(
            {},
            oversized_list_inline["review_cache"],
        )

        oversized_metadata = self.candidate(
            "preview.boundary.metadata",
            "public",
            review_cache={"screenshots": ["C" * (max_cache_bytes + 1)]},
        )
        self.assertEqual({}, oversized_metadata["review_cache"])

        oversized_private = self.candidate(
            "preview.boundary.private",
            "private",
            review_cache={
                "avatar_preview": inline_prefix + ("D" * max_cache_bytes),
                "recent_posts": [{"preview_data_url": inline_prefix + "ignored"}],
            },
        )
        self.assertEqual({}, oversized_private["review_cache"])

        # The aggregate allowance is also a cache-degradation trigger, never a
        # business-record quota.  Even non-inline preview metadata is discarded
        # if necessary and the pending candidate is still inserted.
        with patch("app.service.WORKBENCH_PENDING_CACHE_BUDGET_BYTES", 1):
            aggregate = self.candidate(
                "preview.boundary.aggregate",
                "public",
                review_cache={"thumbnail_url": "https://example.invalid/preview.jpg"},
            )
        self.assertEqual({}, aggregate["review_cache"])

        candidate_ids = {
            exact["id"],
            oversized_inline["id"],
            oversized_list_inline["id"],
            oversized_metadata["id"],
            oversized_private["id"],
            aggregate["id"],
        }
        with self.database.read() as connection:
            pending_ids = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM workbench_candidates WHERE status='pending'"
                ).fetchall()
            }
            self.assertTrue(candidate_ids.issubset(pending_ids))
            expected_seen = {
                "preview.boundary.source", "preview.boundary.exact",
                "preview.boundary.inline", "preview.boundary.list_inline",
                "preview.boundary.metadata", "preview.boundary.private",
                "preview.boundary.aggregate",
            }
            self.assertEqual(expected_seen, {
                row[0] for row in connection.execute(
                    "SELECT a.current_username_norm FROM instagram_accounts a "
                    "JOIN global_seen g ON g.account_id=a.id"
                )
            })
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id=?", (task["id"],)
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM task_targets WHERE task_id=?", (task["id"],)
                ).fetchone()[0],
            )

    def test_rejection_is_idempotent_and_uses_an_immutable_snapshot(self) -> None:
        profile = {"display_name": "Rejected Person", "followers": 321}
        screening = {"location_country": "United States", "activity": "recent"}
        candidate = self.candidate(
            "rejected.person",
            "public",
            profile=profile,
            screening=screening,
            review_cache={"screenshots": ["temporary-cache-key"]},
        )
        normalized_profile = candidate["profile"]
        normalized_screening = candidate["screening"]

        first = self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="rejected"
        )
        retry = self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="rejected"
        )
        self.assertEqual("manual_rejections", first["destination"])
        self.assertEqual(first["reviewed_at"], retry["reviewed_at"])
        self.assertEqual(normalized_profile, first["candidate"]["profile"])
        self.assertEqual(normalized_profile, retry["candidate"]["profile"])
        self.assertEqual(normalized_screening, retry["candidate"]["screening"])
        self.assertEqual({}, retry["candidate"]["review_cache"])

        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([], snapshot["pending_public_accounts"])
        self.assertEqual(1, snapshot["counts"]["rejected"])
        self.assertEqual(
            normalized_profile, snapshot["manual_rejection_history"][0]["profile"]
        )
        self.assertEqual(
            normalized_screening, snapshot["manual_rejection_history"][0]["screening"]
        )

        with self.database.read() as connection:
            candidate_row = connection.execute(
                "SELECT profile_json, screening_json, review_cache_json FROM workbench_candidates WHERE id=?",
                (candidate["id"],),
            ).fetchone()
            self.assertEqual("{}", candidate_row["profile_json"])
            self.assertEqual("{}", candidate_row["screening_json"])
            self.assertEqual("{}", candidate_row["review_cache_json"])
            decision_id = connection.execute(
                "SELECT id FROM workbench_review_decisions WHERE candidate_id=?",
                (candidate["id"],),
            ).fetchone()["id"]

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE workbench_review_decisions SET decision='approved' WHERE id=?",
                    (decision_id,),
                )
        with self.assertRaises(ConflictError):
            self.service.decide_workbench_candidate(
                self.owner["id"], candidate_id=candidate["id"], decision="approved"
            )

    def test_non_us_first_gate_creates_only_immutable_collection_exclusion(self) -> None:
        claim = self.claim("outside.us")
        exclusion = self.service.record_workbench_exclusion(
            self.owner["id"],
            claim_id=claim["claim_id"],
            username="outside.us",
            reason_code="non_us",
            reason="所在地不是美国",
            location_country="Canada",
            profile={"location_country": "Canada"},
        )
        self.assertEqual("excluded_non_us", exclusion["outcome"])
        self.assertFalse(exclusion["continue_collection"])

        duplicate = self.claim("OUTSIDE.US", source="following", source_target="another-source")
        self.assertEqual("duplicate", duplicate["outcome"])
        self.assertFalse(duplicate["should_read_location"])
        self.assertFalse(duplicate["should_collect_profile"])
        self.assertFalse(duplicate["should_create_history"])

        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(0, snapshot["counts"]["pending_review"])
        self.assertEqual(1, snapshot["counts"]["collection_excluded"])
        self.assertEqual("non_us_location", snapshot["collection_exclusion_history"][0]["reason_code"])
        self.assertEqual("Canada", snapshot["collection_exclusion_history"][0]["location_country"])
        self.assertEqual(1, snapshot["dedupe"]["total"])

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.write() as connection:
                connection.execute(
                    "DELETE FROM workbench_collection_exclusions WHERE id=?",
                    (exclusion["id"],),
                )

    def test_empty_api_snapshot_has_formal_shape_real_storage_and_no_demo_rows(self) -> None:
        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(
            settings,
            database=self.database,
            bitbrowser=EmptyBitBrowser(),  # type: ignore[arg-type]
        )
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.get("/api/workbench/snapshot", headers=headers)
            self.assertEqual(200, response.status_code, response.text)
            snapshot = response.json()
            required = {
                "version",
                "revision",
                "snapshot_seq",
                "generated_at",
                "counts",
                "dedupe",
                "storage",
                "pending",
                "approved",
                "history",
                "windows",
                "sources",
                "tasks",
                "campaigns",
                "connection",
                "truncated",
            }
            self.assertTrue(required.issubset(snapshot), sorted(required - set(snapshot)))
            self.assertEqual(__version__, snapshot["version"])
            self.assertEqual(snapshot["snapshot_seq"], snapshot["revision"])
            self.assertEqual({"public": [], "private": []}, snapshot["pending"])
            self.assertEqual({"public": [], "private": []}, snapshot["approved"])
            self.assertEqual(
                {
                    "manual_rejections": [],
                    "collection_exclusions": [],
                    "approvals": [],
                    "actions": [],
                    "tasks": [],
                },
                snapshot["history"],
            )
            self.assertEqual([], snapshot["windows"])
            self.assertEqual([], snapshot["sources"])
            self.assertEqual([], snapshot["tasks"])
            self.assertEqual([], snapshot["campaigns"])
            self.assertEqual(0, snapshot["dedupe"]["total"])
            self.assertEqual("sqlite", snapshot["storage"]["backend"])
            self.assertTrue(snapshot["storage"]["durable"])
            self.assertEqual(self.db_path.stat().st_size, snapshot["storage"]["database_bytes"])
            self.assertGreater(snapshot["storage"]["database_bytes"], 0)
            self.assertFalse(snapshot["storage"]["inline_binary_allowed"])
            self.assertNotIn("demo", json.dumps(snapshot, ensure_ascii=False).casefold())

            command = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "dedupe_claim",
                    "payload": {
                        "username": "api.claim",
                        "source": "followers",
                        "source_target": "api-source",
                    },
                },
            )
            self.assertEqual(200, command.status_code, command.text)
            command_payload = command.json()
            self.assertEqual({"command", "result", "snapshot_seq"}, set(command_payload))
            self.assertEqual("dedupe_claim", command_payload["command"])
            self.assertTrue(command_payload["result"]["claimed"])
            self.assertEqual(
                command_payload["result"]["snapshot_seq"],
                command_payload["snapshot_seq"],
            )

    def test_failure_dismiss_returns_approved_account_without_removing_global_dedupe(self) -> None:
        candidate = self.candidate("action.target", "private")
        approved = self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="approved"
        )
        self.assertEqual("approved_private", approved["destination"])

        campaign = self.service.create_action_campaign(
            self.owner["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="window-01",
            targets=["action.target"],
            target_sources={"action.target": "source-account"},
            message=None,
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        target_id = campaign["targets"][0]["id"]
        failed = self.service.set_campaign_status(
            self.owner["id"], campaign["id"], "failed", error="browser closed"
        )
        self.assertEqual("failed", failed["targets"][0]["status"])

        before = self.service.get_workbench_dedupe_stats(self.owner["id"])["total"]
        dismissed = self.service.dismiss_action_failure(
            self.owner["id"], campaign["id"], target_id
        )
        after = self.service.get_workbench_dedupe_stats(self.owner["id"])["total"]
        self.assertEqual(before, after)
        self.assertEqual(1, after)
        self.assertEqual("dismissed", dismissed["status"])
        self.assertTrue(dismissed["returned_to_approved"])
        self.assertTrue(dismissed["global_dedupe_retained"])
        self.assertEqual(candidate["id"], dismissed["approved_candidate_id"])
        self.assertEqual(
            "dismissed",
            self.service.get_action_campaign(self.owner["id"], campaign["id"])["targets"][0]["status"],
        )

        duplicate = self.claim("ACTION.TARGET", source="followers", source_target="new-source")
        self.assertEqual("duplicate", duplicate["outcome"])
        self.assertFalse(duplicate["should_create_history"])
        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([candidate["id"]], [row["id"] for row in snapshot["approved_private_accounts"]])

    def test_per_target_control_and_permanent_success_dedupe(self) -> None:
        candidate = self.candidate("greet.once", "public")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="approved"
        )
        first = self.service.create_action_campaign(
            self.owner["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="window-01",
            targets=["greet.once", "sibling.target"],
            target_sources={},
            message="Hello",
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=2,
        )
        greet_target, sibling = first["targets"]
        paused = self.service.control_action_target(
            self.owner["id"], first["id"], sibling["id"], action="pause"
        )
        self.assertEqual("paused", paused["status"])
        self.assertEqual(greet_target["id"], self.service.next_action_target(
            self.owner["id"], first["id"]
        )["id"])

        attempt_id = self.service.start_action_attempt(
            self.owner["id"], first["id"], greet_target["id"]
        )
        deferred = self.service.control_action_target(
            self.owner["id"], first["id"], greet_target["id"], action="cancel"
        )
        self.assertTrue(deferred["deferred"])
        self.assertEqual("cancel", deferred["deferred_action"])
        self.service.finish_action_attempt(
            self.owner["id"],
            first["id"],
            attempt_id,
            status="confirmed",
            details={"confirmation": "message visible"},
        )
        stored = self.service.get_action_campaign(self.owner["id"], first["id"])
        self.assertEqual("confirmed", stored["targets"][0]["status"])
        self.assertIsNone(stored["targets"][0]["deferred_action"])
        self.assertEqual(
            "cancel", stored["attempts"][0]["details"]["deferred_control"]
        )

        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([], snapshot["approved_public_accounts"])
        self.assertEqual(1, snapshot["dedupe"]["action_successes"])
        self.assertEqual(1, snapshot["dedupe"]["greet_successes"])
        self.assertEqual(0, snapshot["dedupe"]["follow_successes"])
        with self.assertRaises(ConflictError):
            self.service.create_action_campaign(
                self.owner["id"],
                operation="greet",
                execution_type="campaign",
                profile_id="window-02",
                targets=["GREET.ONCE"],
                target_sources={},
                message="Hello again",
                interval_min_seconds=8,
                interval_max_seconds=15,
                limit_count=1,
            )

        resumed = self.service.control_action_target(
            self.owner["id"], first["id"], sibling["id"], action="resume"
        )
        self.assertEqual("pending", resumed["status"])
        cancelled = self.service.control_action_target(
            self.owner["id"], first["id"], sibling["id"], action="cancel"
        )
        self.assertEqual("failed", cancelled["status"])

    def test_success_history_is_read_directly_from_ledger_when_campaign_details_are_truncated(self) -> None:
        campaign = self.service.create_action_campaign(
            self.owner["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="window-ledger-history",
            targets=["ledger.success", "failed.one", "failed.two", "failed.three"],
            target_sources={},
            message="Hello",
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=4,
        )
        successful_target = campaign["targets"][0]
        attempt_id = self.service.start_action_attempt(
            self.owner["id"], campaign["id"], successful_target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"],
            campaign["id"],
            attempt_id,
            status="confirmed",
            details={
                "confirmation": "message visible",
                "greeting_message": "Hello",
            },
        )
        self.service.set_campaign_status(
            self.owner["id"], campaign["id"], "failed", error="window closed"
        )

        # Operational detail pagination deliberately returns a failed target
        # first, proving the confirmed target is absent from this campaign view.
        truncated = self.service.list_action_campaigns(
            self.owner["id"], limit=1, detail_limit=1
        )[0]
        self.assertTrue(truncated["targets_truncated"])
        self.assertNotIn(
            "confirmed", {target["status"] for target in truncated["targets"]}
        )

        snapshot = self.service.get_workbench_snapshot(
            self.owner["id"], limit=1, history_limit=1
        )
        self.assertEqual(1, len(snapshot["action_success_history"]))
        ledger_row = snapshot["action_success_history"][0]
        self.assertTrue(ledger_row["ledger_backed"])
        self.assertEqual("ledger.success", ledger_row["targets"][0]["username"])
        self.assertEqual("window-ledger-history", ledger_row["profile_id"])
        self.assertEqual("Hello", ledger_row["message"])
        self.assertEqual(["Hello"], ledger_row["messages"])
        self.assertEqual("Hello", ledger_row["attempts"][0]["message"])
        self.assertEqual(
            "Hello",
            ledger_row["attempts"][0]["details"]["greeting_message"],
        )

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.get(
                "/api/workbench/snapshot?limit=1&history_limit=1",
                headers=headers,
            )
        self.assertEqual(200, response.status_code, response.text)
        visible = [
            action
            for action in response.json()["history"]["actions"]
            if action.get("ledger_backed") is True
        ]
        self.assertEqual(1, len(visible))
        self.assertEqual("ledger.success", visible[0]["targets"][0]["username"])
        self.assertEqual("Hello", visible[0]["attempts"][0]["message"])

    def test_concurrent_dispatch_fence_prevents_two_windows_acting_on_one_account(self) -> None:
        first = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-a", targets=["same.action"], target_sources={},
            message=None, interval_min_seconds=8, interval_max_seconds=15,
            limit_count=1,
        )
        second = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-b", targets=["same.action"], target_sources={},
            message=None, interval_min_seconds=8, interval_max_seconds=15,
            limit_count=1,
        )
        first_target = first["targets"][0]
        second_target = second["targets"][0]
        attempt = self.service.start_action_attempt(
            self.owner["id"], first["id"], first_target["id"]
        )
        with self.assertRaises(ConflictError):
            self.service.start_action_attempt(
                self.owner["id"], second["id"], second_target["id"]
            )
        self.service.finish_action_attempt(
            self.owner["id"], first["id"], attempt,
            status="confirmed", details={"confirmation": "following"},
        )
        self.assertIsNone(self.service.next_action_target(self.owner["id"], second["id"]))
        self.assertEqual(
            "already_done",
            self.service.get_action_campaign(self.owner["id"], second["id"])["targets"][0]["status"],
        )
        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(0, snapshot["dedupe"]["greet_successes"])
        self.assertEqual(1, snapshot["dedupe"]["follow_successes"])

    def test_snapshot_window_counts_use_permanent_ledger_and_reset_boundary(self) -> None:
        campaign = self.service.create_action_campaign(
            self.owner["id"], operation="greet", execution_type="campaign",
            profile_id="window-counts", targets=["window.count.target"],
            target_sources={}, message="Hello", interval_min_seconds=8,
            interval_max_seconds=15, limit_count=1,
        )
        target = campaign["targets"][0]
        attempt = self.service.start_action_attempt(
            self.owner["id"], campaign["id"], target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"], campaign["id"], attempt,
            status="confirmed", details={"confirmation": "message visible"},
        )
        before_reset = self.service.get_window_action_counts(
            self.owner["id"], ["window-counts"]
        )[0]
        self.assertEqual(1, before_reset["greet_successes"])
        self.assertEqual(1, before_reset["greet_current"])
        self.assertEqual(0, before_reset["follow_successes"])
        self.service.reset_action_counter(
            self.owner["id"], operation="greet", profile_id="window-counts"
        )
        after_reset = self.service.get_window_action_counts(
            self.owner["id"], ["window-counts"]
        )[0]
        self.assertEqual(1, after_reset["greet_successes"])
        self.assertEqual(0, after_reset["greet_current"])
        self.assertIsNotNone(after_reset["greet_reset_at"])

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(
            settings, database=self.database, bitbrowser=OneWindowBitBrowser()
        )
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.get("/api/workbench/snapshot", headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(
            after_reset, payload["dedupe"]["window_action_counts"][0]
        )
        self.assertEqual(after_reset, payload["windows"][0]["action_counts"])

    def test_unknown_action_requires_explicit_resolution_before_return_or_success(self) -> None:
        not_done_candidate = self.candidate("unknown.not.done", "private")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=not_done_candidate["id"], decision="approved"
        )
        not_done_campaign = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-unknown-a", targets=["unknown.not.done"],
            target_sources={}, message=None, interval_min_seconds=8,
            interval_max_seconds=15, limit_count=1,
        )
        not_done_target = not_done_campaign["targets"][0]
        not_done_attempt = self.service.start_action_attempt(
            self.owner["id"], not_done_campaign["id"], not_done_target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"], not_done_campaign["id"], not_done_attempt,
            status="unknown", details={"message": "connection lost after click"},
        )
        with self.assertRaises(ConflictError) as blocked:
            self.service.dismiss_action_failure(
                self.owner["id"], not_done_campaign["id"], not_done_target["id"]
            )
        self.assertTrue(blocked.exception.details["resolution_required"])
        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            resolved_response = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "action_unknown_resolve",
                    "payload": {
                        "campaign_id": not_done_campaign["id"],
                        "target_id": not_done_target["id"],
                        "outcome": "not_completed",
                    },
                },
            )
        self.assertEqual(200, resolved_response.status_code, resolved_response.text)
        self.assertEqual(
            "action_unknown_resolve", resolved_response.json()["command"]
        )
        not_done = resolved_response.json()["result"]
        self.assertEqual("dismissed", not_done["status"])
        self.assertTrue(not_done["returned_to_approved"])
        self.assertFalse(not_done["success_ledger_recorded"])
        self.assertEqual(
            [not_done_candidate["id"]],
            [row["id"] for row in self.service.get_workbench_snapshot(
                self.owner["id"]
            )["approved_private_accounts"]],
        )

        completed_candidate = self.candidate("unknown.completed", "public")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=completed_candidate["id"], decision="approved"
        )
        completed_campaign = self.service.create_action_campaign(
            self.owner["id"], operation="greet", execution_type="campaign",
            profile_id="window-unknown-b", targets=["unknown.completed"],
            target_sources={}, message="Hello", interval_min_seconds=8,
            interval_max_seconds=15, limit_count=1,
        )
        completed_target = completed_campaign["targets"][0]
        completed_attempt = self.service.start_action_attempt(
            self.owner["id"], completed_campaign["id"], completed_target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"], completed_campaign["id"], completed_attempt,
            status="unknown", details={"message": "confirmation unavailable"},
        )
        completed = self.service.resolve_unknown_action(
            self.owner["id"], completed_campaign["id"], completed_target["id"],
            outcome="completed",
        )
        self.assertEqual("confirmed", completed["status"])
        self.assertFalse(completed["returned_to_approved"])
        self.assertTrue(completed["success_ledger_recorded"])
        completed_snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertNotIn(
            completed_candidate["id"],
            [row["id"] for row in completed_snapshot["approved_public_accounts"]],
        )
        self.assertEqual(1, completed_snapshot["dedupe"]["greet_successes"])
        with self.assertRaises(ConflictError):
            self.service.create_action_campaign(
                self.owner["id"], operation="greet", execution_type="campaign",
                profile_id="window-unknown-c", targets=["UNKNOWN.COMPLETED"],
                target_sources={}, message="Again", interval_min_seconds=8,
                interval_max_seconds=15, limit_count=1,
            )

    def test_campaign_eligibility_queries_only_request_usernames_in_bounded_batches(self) -> None:
        target_names = [f"bulk{index:04d}" for index in range(405)]
        successful_names = {target_names[0], target_names[400]}
        dispatching_names = {target_names[1], target_names[401]}
        with self.database.write() as connection:
            connection.executemany(
                """
                INSERT INTO action_success_ledger(
                    owner_user_id, operation, username_norm, username_display,
                    campaign_id, target_id, attempt_id, completed_at
                ) VALUES(?, 'follow', ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')
                """,
                [
                    (
                        self.owner["id"], username, username,
                        f"campaign-{username}", f"target-{username}",
                        f"attempt-{username}",
                    )
                    for username in successful_names
                ],
            )
            connection.executemany(
                """
                INSERT INTO action_dispatch_claims(
                    owner_user_id, operation, username_norm, campaign_id,
                    target_id, attempt_id, claimed_at
                ) VALUES(?, 'follow', ?, ?, ?, ?, '2026-01-01T00:00:00Z')
                """,
                [
                    (
                        self.owner["id"], username, f"campaign-{username}",
                        f"target-{username}", f"dispatch-{username}",
                    )
                    for username in dispatching_names
                ],
            )

        statements: list[str] = []
        original_connect = self.database._connect

        def traced_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        self.database._connect = traced_connect  # type: ignore[method-assign]
        try:
            campaign = self.service.create_action_campaign(
                self.owner["id"], operation="follow", execution_type="campaign",
                profile_id="window-bulk", targets=target_names, target_sources={},
                message=None, interval_min_seconds=8, interval_max_seconds=15,
                limit_count=len(target_names),
            )
        finally:
            self.database._connect = original_connect  # type: ignore[method-assign]

        returned_names = {target["username"] for target in campaign["targets"]}
        self.assertEqual(401, len(returned_names))
        self.assertTrue((successful_names | dispatching_names).isdisjoint(returned_names))
        eligibility_queries = [
            statement
            for statement in statements
            if "FROM action_success_ledger" in statement
            and "UNION ALL" in statement
        ]
        self.assertEqual(2, len(eligibility_queries))
        self.assertTrue(
            all(query.count("username_norm IN (") == 2 for query in eligibility_queries)
        )

    def test_bulk_detail_budget_is_fair_global_and_prefers_operational_rows(self) -> None:
        task_ids: list[str] = []
        interesting_task_statuses = ["running", "waiting_network"]
        for index, status in enumerate(interesting_task_statuses):
            task = self.service.create_task(
                self.owner["id"], name=f"task-{index}", modes=["followers"],
                targets=[f"source{index}{target}" for target in range(4)],
                settings={"location_enabled": True},
                window_ids=[f"window-{index}-a", f"window-{index}-b"],
            )
            task_ids.append(task["id"])
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE tasks SET status='running' WHERE id=?", (task["id"],)
                )
                connection.execute(
                    """
                    UPDATE task_targets SET status=?
                    WHERE id=(
                        SELECT id FROM task_targets WHERE task_id=?
                        ORDER BY queue_order DESC LIMIT 1
                    )
                    """,
                    (status, task["id"]),
                )
        tasks = self.service.list_tasks_with_details(
            self.owner["id"], limit=2, detail_limit=2
        )
        self.assertEqual(2, sum(len(task["targets"]) for task in tasks))
        self.assertEqual(
            set(interesting_task_statuses),
            {task["targets"][0]["status"] for task in tasks},
        )
        self.assertTrue(all(task["targets_truncated"] for task in tasks))

        campaign_statuses = ["running", "unknown"]
        for index, target_status in enumerate(campaign_statuses):
            campaign = self.service.create_action_campaign(
                self.owner["id"], operation="follow", execution_type="campaign",
                profile_id=f"campaign-window-{index}",
                targets=[f"action{index}{target}" for target in range(4)],
                target_sources={}, message=None, interval_min_seconds=8,
                interval_max_seconds=15, limit_count=4,
            )
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE action_campaigns SET status='running' WHERE id=?",
                    (campaign["id"],),
                )
                connection.execute(
                    """
                    UPDATE action_targets SET status=?
                    WHERE id=(
                        SELECT id FROM action_targets WHERE campaign_id=?
                        ORDER BY queue_order DESC LIMIT 1
                    )
                    """,
                    (target_status, campaign["id"]),
                )
        campaigns = self.service.list_action_campaigns(
            self.owner["id"], limit=2, detail_limit=2
        )
        self.assertEqual(2, sum(len(campaign["targets"]) for campaign in campaigns))
        self.assertEqual(
            set(campaign_statuses),
            {campaign["targets"][0]["status"] for campaign in campaigns},
        )
        self.assertTrue(all(campaign["targets_truncated"] for campaign in campaigns))

    def test_legacy_list_routes_are_bounded_and_history_delete_uses_direct_lookup(self) -> None:
        tasks = [
            self.service.create_task(
                self.owner["id"], name=f"paged-task-{index}", modes=["followers"],
                targets=[f"source{index}a", f"source{index}b"],
                settings={"location_enabled": True}, window_ids=[f"window-{index}"],
            )
            for index in range(3)
        ]
        first_target_id = tasks[0]["targets"][0]["id"]
        for index in range(3):
            self.service.record_result(
                self.owner["id"], tasks[0]["id"], first_target_id,
                username=f"result{index}", instagram_user_id=f"ig-{index}",
                source_mode="followers", visibility="public",
                profile={"display_name": f"Result {index}"}, screening={},
                qualified=True,
            )
        campaigns = [
            self.service.create_action_campaign(
                self.owner["id"], operation="follow", execution_type="campaign",
                profile_id=f"paged-window-{index}", targets=[f"pagedaction{index}"],
                target_sources={}, message=None, interval_min_seconds=8,
                interval_max_seconds=15, limit_count=1,
            )
            for index in range(3)
        ]
        with self.database.write() as connection:
            connection.executemany(
                "UPDATE action_campaigns SET status='stopped' WHERE id=?",
                [(campaign["id"],) for campaign in campaigns],
            )
            connection.executemany(
                "UPDATE action_targets SET status='stopped' WHERE campaign_id=?",
                [(campaign["id"],) for campaign in campaigns],
            )

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            for path, collection_key, expected_total in (
                ("/api/tasks", "tasks", 3),
                ("/api/actions/campaigns", "campaigns", 3),
                ("/api/results", "results", 3),
                ("/api/history", "history", 6),
            ):
                response = client.get(
                    f"{path}?limit=1&offset=1&detail_limit=1", headers=headers
                )
                self.assertEqual(200, response.status_code, response.text)
                payload = response.json()
                self.assertEqual(expected_total, payload["total"])
                self.assertEqual(1, len(payload[collection_key]))
                self.assertEqual(1, payload["limit"])
                self.assertEqual(1, payload["offset"])
                self.assertTrue(payload["has_more"])

            delete_target_id = tasks[2]["targets"][0]["id"]
            original_list_history = self.service.list_history

            def forbidden_history_scan(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
                raise AssertionError("DELETE history must not scan the history list")

            self.service.list_history = forbidden_history_scan  # type: ignore[method-assign]
            try:
                deleted = client.delete(
                    f"/api/history/{delete_target_id}", headers=headers
                )
            finally:
                self.service.list_history = original_list_history  # type: ignore[method-assign]
            self.assertEqual(204, deleted.status_code, deleted.text)

    def test_task_window_details_and_snapshot_are_bounded_and_marked_truncated(self) -> None:
        task = self.service.create_task(
            self.owner["id"],
            name="many windows",
            modes=["followers"],
            targets=["source.one", "source.two", "source.three"],
            settings={"location_enabled": True},
            window_ids=[f"window-{index:02d}" for index in range(25)],
        )
        bounded = self.service.list_tasks_with_details(
            self.owner["id"], limit=1, detail_limit=1
        )
        self.assertEqual(task["id"], bounded[0]["id"])
        self.assertEqual(1, len(bounded[0]["targets"]))
        self.assertEqual(1, len(bounded[0]["window_ids"]))
        self.assertTrue(bounded[0]["targets_truncated"])
        self.assertTrue(bounded[0]["windows_truncated"])

        for index in range(2):
            self.service.create_action_campaign(
                self.owner["id"],
                operation="follow",
                execution_type="campaign",
                profile_id=f"follow-window-{index}",
                targets=[f"follow.target.{index}"],
                target_sources={},
                message=None,
                interval_min_seconds=8,
                interval_max_seconds=15,
                limit_count=1,
            )

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.get(
                "/api/workbench/snapshot?limit=1&history_limit=1", headers=headers
            )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload["has_more"]["tasks"])
        self.assertTrue(payload["has_more"]["sources"])
        self.assertTrue(payload["has_more"]["follow_campaigns"])
        self.assertFalse(payload["has_more"]["greet_campaigns"])
        self.assertFalse(payload["has_more"]["greet_action_success_history"])
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(len(payload["tasks"]), 1)

    def test_snapshot_bounds_completed_split_history_and_keeps_live_rows(self) -> None:
        live_rows = self.service.upsert_manual_split_candidates(
            self.owner["id"],
            [
                {"username": "split.waiting.one", "queued": True},
                {"username": "split.waiting.two", "queued": True},
            ],
        )
        live_ids = {row["id"] for row in live_rows}
        ignored_rows = self.service.upsert_manual_split_candidates(
            self.owner["id"],
            [{"username": "split.ignored", "queued": False}],
        )
        ignored_id = next(
            row["id"]
            for row in ignored_rows
            if row["username"] == "split.ignored"
        )
        self.service.set_split_candidate_manual_category(
            self.owner["id"], [ignored_id], "completed"
        )

        failed_task = self.service.create_task(
            self.owner["id"],
            name="split failure remains actionable",
            modes=["followers"],
            targets=["split.failed.source"],
            settings={"location_enabled": True},
            window_ids=["split-failure-window"],
        )
        failed_target = failed_task["targets"][0]
        self.service.set_target_runtime_status(
            self.owner["id"],
            failed_task["id"],
            failed_target["id"],
            "recoverable",
            window_id="split-failure-window",
        )

        completed_ids = [
            "completed-split-oldest",
            "completed-split-middle",
            "completed-split-newest",
        ]
        with self.database.write() as connection:
            connection.executemany(
                """
                INSERT INTO split_candidate_history(
                    id, owner_user_id, username_norm, username_display,
                    profile_json, completed_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, '{}', ?, ?, ?)
                """,
                [
                    (
                        candidate_id,
                        self.owner["id"],
                        f"history.{index}",
                        f"history.{index}",
                        f"2026-01-0{index + 1}T00:00:00Z",
                        f"2026-01-0{index + 1}T00:00:00Z",
                        f"2026-01-0{index + 1}T00:00:00Z",
                    )
                    for index, candidate_id in enumerate(completed_ids)
                ],
            )

        statements: list[str] = []
        original_connect = self.database._connect

        def traced_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        self.database._connect = traced_connect  # type: ignore[method-assign]
        try:
            sentinel_page = self.service.list_split_candidates(
                self.owner["id"], row_limit=2, completed_limit=2
            )
        finally:
            self.database._connect = original_connect  # type: ignore[method-assign]

        self.assertTrue(live_ids.issubset({row["id"] for row in sentinel_page}))
        self.assertEqual(
            1,
            sum(row["kind"] == "failure" for row in sentinel_page),
        )
        self.assertEqual(
            2,
            sum(
                row.get("real_lifecycle_state") == "completed"
                for row in sentinel_page
            ),
        )
        normalized_statements = [" ".join(statement.split()) for statement in statements]
        completed_queries = [
            statement
            for statement in normalized_statements
            if "FROM split_candidate_history history" in statement
            and "ORDER BY history.completed_at" in statement
        ]
        self.assertEqual(1, len(completed_queries))
        self.assertTrue(completed_queries[0].endswith("LIMIT 2"))
        manual_queries = [
            statement
            for statement in normalized_statements
            if "FROM split_candidates candidate" in statement
            and "candidate.updated_at DESC" in statement
        ]
        failure_queries = [
            statement
            for statement in normalized_statements
            if "FROM task_target_recovery_controls recovery" in statement
            and "ORDER BY recovery.updated_at" in statement
        ]
        self.assertEqual(1, len(manual_queries))
        self.assertEqual(1, len(failure_queries))
        self.assertTrue(manual_queries[0].endswith("LIMIT 2"))
        self.assertTrue(failure_queries[0].endswith("LIMIT 2"))

        # The optional bound is snapshot-only.  Existing service/API callers keep
        # the legacy full-list result unless they explicitly request a page.
        default_rows = self.service.list_split_candidates(self.owner["id"])
        self.assertEqual(
            set(completed_ids),
            {
                row["id"]
                for row in default_rows
                if row.get("real_lifecycle_state") == "completed"
            },
        )

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            tiny_response = client.get(
                "/api/workbench/snapshot?limit=1&history_limit=1",
                headers=headers,
            )
            priority_response = client.get(
                "/api/workbench/snapshot?limit=3&history_limit=1",
                headers=headers,
            )
            bounded_response = client.get(
                "/api/workbench/snapshot?limit=50&history_limit=1",
                headers=headers,
            )
            complete_response = client.get(
                "/api/workbench/snapshot?limit=50&history_limit=3",
                headers=headers,
            )
        self.assertEqual(200, tiny_response.status_code, tiny_response.text)
        tiny_payload = tiny_response.json()
        self.assertEqual(1, len(tiny_payload["split_candidates"]))
        self.assertEqual("failure", tiny_payload["split_candidates"][0]["kind"])
        self.assertTrue(tiny_payload["has_more"]["split_candidates"])
        self.assertTrue(tiny_payload["truncated"])
        self.assertEqual(200, priority_response.status_code, priority_response.text)
        priority_payload = priority_response.json()
        self.assertEqual(3, len(priority_payload["split_candidates"]))
        self.assertEqual("failure", priority_payload["split_candidates"][0]["kind"])
        self.assertEqual(
            live_ids,
            {row["id"] for row in priority_payload["split_candidates"][1:]},
        )
        self.assertNotIn(
            ignored_id,
            {row["id"] for row in priority_payload["split_candidates"]},
        )
        self.assertTrue(priority_payload["has_more"]["split_candidates"])

        self.assertEqual(200, bounded_response.status_code, bounded_response.text)
        bounded_payload = bounded_response.json()
        bounded_split_rows = bounded_payload["split_candidates"]
        self.assertTrue(live_ids.issubset({row["id"] for row in bounded_split_rows}))
        self.assertEqual(
            1,
            sum(row["kind"] == "failure" for row in bounded_split_rows),
        )
        bounded_completed = [
            row
            for row in bounded_split_rows
            if row.get("real_lifecycle_state") == "completed"
        ]
        self.assertEqual(["completed-split-newest"], [row["id"] for row in bounded_completed])
        self.assertEqual(3, bounded_payload["counts"]["total_split"])
        self.assertTrue(bounded_payload["has_more"]["split_candidates"])
        self.assertTrue(bounded_payload["truncated"])

        self.assertEqual(200, complete_response.status_code, complete_response.text)
        complete_payload = complete_response.json()
        self.assertEqual(
            completed_ids[::-1],
            [
                row["id"]
                for row in complete_payload["split_candidates"]
                if row.get("real_lifecycle_state") == "completed"
            ],
        )
        self.assertEqual(3, complete_payload["counts"]["total_split"])
        self.assertFalse(complete_payload["has_more"]["split_candidates"])

    def test_split_mutations_do_not_scan_or_return_unrelated_history(self) -> None:
        with self.database.write() as connection:
            connection.executemany(
                """
                INSERT INTO split_candidate_history(
                    id, owner_user_id, username_norm, username_display,
                    profile_json, completed_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, '{}', ?, ?, ?)
                """,
                [
                    (
                        f"unrelated-completed-{index}",
                        self.owner["id"],
                        "repeated.completed",
                        "repeated.completed",
                        f"2026-02-{index + 1:02d}T00:00:00Z",
                        f"2026-02-{index + 1:02d}T00:00:00Z",
                        f"2026-02-{index + 1:02d}T00:00:00Z",
                    )
                    for index in range(20)
                ],
            )

        statements: list[str] = []
        original_connect = self.database._connect

        def traced_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        self.database._connect = traced_connect  # type: ignore[method-assign]
        try:
            created = self.service.upsert_manual_split_candidates(
                self.owner["id"],
                [{"username": "affected.only", "queued": False}],
                include_outcome=True,
            )
        finally:
            self.database._connect = original_connect  # type: ignore[method-assign]

        self.assertEqual(1, created["accepted_count"])
        self.assertEqual(1, created["affected_count"])
        self.assertEqual(1, len(created["candidates"]))
        affected_id = created["accepted_ids"][0]
        self.assertEqual(affected_id, created["candidates"][0]["id"])
        normalized_statements = [" ".join(statement.split()) for statement in statements]
        duplicate_history_queries = [
            statement
            for statement in normalized_statements
            if "WITH ranked_history AS" in statement
            and "FROM split_candidate_history" in statement
        ]
        self.assertEqual(1, len(duplicate_history_queries))
        self.assertIn("ROW_NUMBER() OVER", duplicate_history_queries[0])
        self.assertIn("username_norm IN ('affected.only')", duplicate_history_queries[0])

        repeated = self.service.upsert_manual_split_candidates(
            self.owner["id"],
            [{"username": "repeated.completed", "queued": True}],
            include_outcome=True,
        )
        self.assertEqual(0, repeated["accepted_count"])
        self.assertEqual(1, repeated["duplicate_count"])
        self.assertEqual(20, repeated["duplicates"][0]["generation_count"])
        self.assertEqual(
            ["unrelated-completed-19"],
            repeated["duplicates"][0]["candidate_ids"],
        )
        self.assertEqual(
            ["unrelated-completed-19"],
            [row["id"] for row in repeated["candidates"]],
        )
        with self.database.read() as connection:
            history_indexes = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA index_list(split_candidate_history)"
                ).fetchall()
            }
        self.assertIn("idx_split_history_owner_username", history_indexes)

        queued = self.service.mark_split_candidates_queued(
            self.owner["id"], [affected_id], include_outcome=True
        )
        self.assertEqual(1, queued["affected_count"])
        self.assertEqual([affected_id], [row["id"] for row in queued["candidates"]])
        categorized = self.service.set_split_candidate_manual_category(
            self.owner["id"],
            [affected_id],
            "completed",
            affected_only=True,
        )
        self.assertEqual([affected_id], [row["id"] for row in categorized])

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "split_waiting_add",
                    "payload": {"targets": ["command.affected.only"]},
                },
            )
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()["result"]
        self.assertEqual(1, result["accepted_count"])
        self.assertEqual(1, result["affected_count"])
        self.assertEqual(
            ["command.affected.only"],
            [row["username"] for row in result["candidates"]],
        )

    def test_split_window_assignment_commands_are_persistent_and_generation_safe(self) -> None:
        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-affinity",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            added = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "split_waiting_add",
                    "payload": {
                        "targets": ["assigned.command"],
                        "allowed_window_ids": ["window-b", "window-c"],
                    },
                },
            )
            self.assertEqual(200, added.status_code, added.text)
            added_result = added.json()["result"]
            candidate_id = added_result["accepted_ids"][0]
            self.assertEqual(
                ["window-b", "window-c"],
                added_result["candidates"][0]["allowed_window_ids"],
            )

            legacy_upsert = client.post(
                "/api/split-candidates",
                headers=headers,
                json={
                    "candidates": [
                        {"username": "assigned.command", "queued": True}
                    ]
                },
            )
            self.assertEqual(200, legacy_upsert.status_code, legacy_upsert.text)
            self.assertEqual(
                ["window-b", "window-c"],
                legacy_upsert.json()["candidates"][0]["allowed_window_ids"],
            )

            missing_command_pool = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "split_waiting_assign_windows",
                    "payload": {"candidate_id": candidate_id},
                },
            )
            self.assertEqual(
                422, missing_command_pool.status_code, missing_command_pool.text
            )

            changed = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "split_waiting_assign_windows",
                    "payload": {
                        "candidate_id": candidate_id,
                        "allowed_window_ids": ["window-c"],
                    },
                },
            )
            self.assertEqual(200, changed.status_code, changed.text)
            self.assertEqual(
                ["window-c"], changed.json()["result"]["allowed_window_ids"]
            )
            self.assertGreater(
                changed.json()["snapshot_seq"], added.json()["snapshot_seq"]
            )

            cleared = client.patch(
                f"/api/split-candidates/{candidate_id}/windows",
                headers=headers,
                json={"allowed_window_ids": []},
            )
            self.assertEqual(200, cleared.status_code, cleared.text)
            self.assertEqual(
                "automatic",
                cleared.json()["candidate"]["window_assignment_mode"],
            )

            missing_patch_pool = client.patch(
                f"/api/split-candidates/{candidate_id}/windows",
                headers=headers,
                json={},
            )
            self.assertEqual(
                422, missing_patch_pool.status_code, missing_patch_pool.text
            )

    def test_failure_requeue_rest_atomically_accepts_a_window_pool(self) -> None:
        self.service.upsert_manual_split_candidates(
            self.owner["id"],
            [{
                "username": "rest.failure.preserve",
                "queued": True,
                "allowed_window_ids": ["window-a", "window-b"],
            }],
        )
        task = self.service.create_task(
            self.owner["id"], name="REST failure affinity", modes=["followers"],
            targets=[], window_ids=["window-a", "window-b", "window-c"],
            settings={"live_queue_enabled": True},
        )
        self.service.set_task_runtime_status(self.owner["id"], task["id"], "running")

        def create_failure(
            username: str, allowed_window_ids: list[str], profile_id: str
        ) -> dict[str, object]:
            self.service.upsert_manual_split_candidates(
                self.owner["id"],
                [{
                    "username": username,
                    "queued": True,
                    "allowed_window_ids": allowed_window_ids,
                }],
            )
            target = self.service.claim_next_split_candidate(
                self.owner["id"], task["id"], profile_id
            )
            self.assertIsNotNone(target)
            assert target is not None
            self.assertEqual(username, target["username"])
            self.service.set_target_runtime_status(
                self.owner["id"], task["id"], target["id"], "recoverable",
                window_id=profile_id,
            )
            return next(
                item
                for item in self.service.list_split_candidates(self.owner["id"])
                if item["kind"] == "failure" and item["username"] == username
            )

        preserve_failure = create_failure(
            "rest.failure.preserve", ["window-a", "window-b"], "window-a"
        )
        replace_failure = create_failure(
            "rest.failure.replace", ["window-a"], "window-a"
        )
        clear_failure = create_failure(
            "rest.failure.clear", ["window-b"], "window-b"
        )

        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-rest-failure-affinity",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            preserved = client.post(
                f"/api/split-candidates/{preserve_failure['id']}/requeue",
                headers=headers,
            )
            replaced = client.post(
                f"/api/split-candidates/{replace_failure['id']}/requeue",
                headers=headers,
                json={"allowed_window_ids": ["window-c"]},
            )
            cleared = client.post(
                f"/api/split-candidates/{clear_failure['id']}/requeue",
                headers=headers,
                json={"allowed_window_ids": []},
            )
        self.assertEqual(200, preserved.status_code, preserved.text)
        self.assertEqual(
            ["window-a", "window-b"],
            preserved.json()["candidate"]["allowed_window_ids"],
        )
        self.assertEqual(200, replaced.status_code, replaced.text)
        self.assertEqual(
            ["window-c"], replaced.json()["candidate"]["allowed_window_ids"]
        )
        self.assertEqual(200, cleared.status_code, cleared.text)
        self.assertEqual(
            [], cleared.json()["candidate"]["allowed_window_ids"]
        )
        stored = {
            item["username"]: item
            for item in self.service.list_split_candidates(self.owner["id"])
        }
        self.assertEqual(
            ["window-a", "window-b"],
            stored["rest.failure.preserve"]["allowed_window_ids"],
        )
        self.assertEqual(
            ["window-c"], stored["rest.failure.replace"]["allowed_window_ids"]
        )
        self.assertEqual(
            "automatic", stored["rest.failure.clear"]["window_assignment_mode"]
        )

    def test_approved_candidate_dismiss_is_soft_and_fails_unstarted_action(self) -> None:
        candidate = self.candidate("remove.approved", "private")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="approved"
        )
        campaign = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-a", targets=["remove.approved"], target_sources={},
            message=None, interval_min_seconds=8, interval_max_seconds=15,
            limit_count=1,
        )
        dismissed = self.service.dismiss_approved_candidate(
            self.owner["id"], candidate["id"]
        )
        self.assertTrue(dismissed["global_dedupe_retained"])
        self.assertEqual("failed", dismissed["affected_targets"][0]["to"])
        snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual([], snapshot["approved_private_accounts"])
        self.assertEqual(1, snapshot["counts"]["approved_dismissed"])
        self.assertEqual(candidate["id"], snapshot["approval_history"][0]["id"])
        self.assertIsNotNone(snapshot["approval_history"][0]["dismissed_at"])
        self.assertEqual(
            "failed",
            self.service.get_action_campaign(self.owner["id"], campaign["id"])["targets"][0]["status"],
        )
        self.assertEqual("duplicate", self.claim("REMOVE.APPROVED")["outcome"])

    def test_new_camel_case_filters_reach_task_settings_and_stale_limit_is_ignored(self) -> None:
        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "task_create",
                    "payload": {
                        "targets": ["source.account"],
                        "window_ids": ["window-a"],
                        "modes": ["followers"],
                        "source_limits": {
                            "followers": {
                                "followersMin": 1000,
                                "followersMax": 10000,
                                "followingMax": 1500,
                                "postsMin": 30,
                                "postsMax": 5000,
                                "activityDays": 30,
                                "perTargetLimit": 25,
                            }
                        },
                        "exclude_verified": True,
                    },
                },
            )
            self.assertEqual(200, response.status_code, response.text)
            task = self.service.get_task(
                self.owner["id"], response.json()["result"]["task_id"]
            )
            self.assertEqual(
                {
                    "followers_min": 1000,
                    "followers_max": 10000,
                    "following_max": 1500,
                    "posts_min": 30,
                    "posts_max": 5000,
                    "active_days_max": 30,
                },
                task["settings"]["mode_limits"]["followers"],
            )
            self.assertTrue(task["settings"]["exclude_verified"])
            self.assertTrue(task["settings"]["exclude_public_zero_posts"])

    def test_manual_cache_cleanup_preserves_pending_previews_and_permanent_ledgers(self) -> None:
        pending_cache = {
            "avatar_preview": "data:image/jpeg;base64,cGVuZGluZw==",
            "recent_posts": [
                {
                    "post_url": "https://www.instagram.com/p/pending-preview/",
                    "thumbnail_url": "https://example.invalid/pending.jpg",
                }
            ],
        }
        pending = self.candidate(
            "cleanup.pending",
            "public",
            review_cache=pending_cache,
        )
        approved = self.candidate(
            "cleanup.approved",
            "private",
            review_cache={
                "avatar_preview": "data:image/jpeg;base64,YXBwcm92ZWQ=",
                "recent_posts": ["approved-preview"],
            },
        )
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=approved["id"], decision="approved"
        )
        rejected = self.candidate(
            "cleanup.rejected",
            "public",
            review_cache={"screenshots": ["rejected-preview"]},
        )
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=rejected["id"], decision="rejected"
        )

        # A rejected decision already drops its live preview.  Re-inject one
        # terminal cache row to model an upgraded database and prove the manual
        # cleanup is driven by terminal state, not by a particular decision path.
        with self.database.write() as connection:
            connection.execute(
                "UPDATE workbench_candidates SET review_cache_json=? WHERE id=?",
                (
                    json.dumps(
                        {"screenshots": ["legacy-terminal-preview"]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    rejected["id"],
                ),
            )

        exclusion_claim = self.claim("cleanup.excluded")
        self.service.record_workbench_exclusion(
            self.owner["id"],
            claim_id=exclusion_claim["claim_id"],
            username="cleanup.excluded",
            reason_code="non_us_location",
            reason="所在地非美国",
            location_country="Canada",
            profile={"location_zh": "Canada"},
        )

        campaign = self.service.create_action_campaign(
            self.owner["id"],
            operation="follow",
            execution_type="campaign",
            profile_id="cleanup-action-window",
            targets=["cleanup.success"],
            target_sources={},
            message=None,
            interval_min_seconds=8,
            interval_max_seconds=15,
            limit_count=1,
        )
        action_target = campaign["targets"][0]
        attempt_id = self.service.start_action_attempt(
            self.owner["id"], campaign["id"], action_target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"],
            campaign["id"],
            attempt_id,
            status="confirmed",
            details={"confirmation": "following"},
        )

        task = self.service.create_task(
            self.owner["id"],
            name="cleanup-live-task",
            modes=["followers"],
            targets=["cleanup.source"],
            settings={"location_enabled": True},
            window_ids=["cleanup-collection-window"],
        )
        task_target = task["targets"][0]
        self.service.control_task(self.owner["id"], task["id"], "start")
        self.service.set_target_runtime_status(
            self.owner["id"],
            task["id"],
            task_target["id"],
            "running",
            window_id="cleanup-collection-window",
        )
        self.service.upsert_checkpoint(
            self.owner["id"],
            task["id"],
            task_target["id"],
            mode="followers",
            stage="collecting_list",
            cursor={"end_cursor": "durable-cursor"},
            counters={"collected": 17},
            recoverable=True,
        )

        protected_tables = (
            "global_seen",
            "global_seen_stats",
            "workbench_review_decisions",
            "workbench_collection_exclusions",
            "action_success_ledger",
            "tasks",
            "task_targets",
            "task_checkpoints",
        )

        def protected_rows() -> dict[str, list[tuple[Any, ...]]]:
            with self.database.read() as connection:
                return {
                    table: [
                        tuple(row)
                        for row in connection.execute(
                            f"SELECT * FROM {table} ORDER BY 1"
                        ).fetchall()
                    ]
                    for table in protected_tables
                }

        rows_before = protected_rows()
        snapshot_before = self.service.get_workbench_snapshot(self.owner["id"])
        revision_before = snapshot_before["snapshot_seq"]
        self.assertGreater(snapshot_before["storage"]["cache_bytes"], 0)
        self.assertGreater(snapshot_before["storage"]["pending_preview_bytes"], 0)
        pending_preview_bytes = snapshot_before["storage"]["pending_preview_bytes"]
        maintenance_before = snapshot_before["storage"]["last_maintenance_at"]

        result = self.service.clear_workbench_cache(self.owner["id"])

        self.assertEqual(2, result["cleared_entries"])
        self.assertGreater(result["cleared_bytes"], 0)
        self.assertTrue(result["business_records_retained"])
        self.assertEqual(
            {
                "global_dedupe": True,
                "review_history": True,
                "collection_exclusions": True,
                "action_success_history": True,
                "live_task_checkpoints": True,
                "pending_review_previews": True,
            },
            result["retained"],
        )
        self.assertEqual(revision_before + 1, result["snapshot_seq"])
        self.assertEqual(rows_before, protected_rows())

        with self.database.read() as connection:
            caches = {
                row["id"]: row["review_cache_json"]
                for row in connection.execute(
                    """
                    SELECT id, review_cache_json
                    FROM workbench_candidates
                    WHERE id IN (?, ?, ?)
                    """,
                    (pending["id"], approved["id"], rejected["id"]),
                ).fetchall()
            }
        self.assertEqual(
            {"avatar_preview": pending_cache["avatar_preview"]},
            json.loads(caches[pending["id"]]),
        )
        self.assertEqual("{}", caches[approved["id"]])
        self.assertEqual("{}", caches[rejected["id"]])

        snapshot_after = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(0, snapshot_after["storage"]["cache_bytes"])
        self.assertEqual(
            pending_preview_bytes,
            snapshot_after["storage"]["pending_preview_bytes"],
        )
        self.assertGreater(snapshot_after["storage"]["pending_preview_bytes"], 0)
        self.assertEqual(snapshot_before["counts"], snapshot_after["counts"])
        self.assertEqual(snapshot_before["dedupe"], snapshot_after["dedupe"])
        self.assertEqual(
            maintenance_before,
            snapshot_after["storage"]["last_maintenance_at"],
        )

        repeated = self.service.clear_workbench_cache(self.owner["id"])
        self.assertEqual(0, repeated["cleared_entries"])
        self.assertEqual(0, repeated["cleared_bytes"])
        self.assertTrue(repeated["business_records_retained"])
        self.assertEqual(rows_before, protected_rows())
        repeated_snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(0, repeated_snapshot["storage"]["cache_bytes"])
        self.assertEqual(
            pending_preview_bytes,
            repeated_snapshot["storage"]["pending_preview_bytes"],
        )

        # Even a no-op owner-scoped click must not postpone the global hourly
        # maintenance pass shared by diagnostic logs, sessions and old spool.
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE workbench_state_revision
                SET last_cleanup_at=datetime('now', '-2 hours')
                WHERE singleton_id=1
                """
            )
        no_op = self.service.clear_workbench_cache(self.owner["id"])
        self.assertEqual(0, no_op["cleared_entries"])
        with self.database.read() as connection:
            watermark = connection.execute(
                """
                SELECT last_cleanup_at FROM workbench_state_revision
                WHERE singleton_id=1
                """
            ).fetchone()[0]
        self.assertNotEqual(no_op["last_cleanup_at"], watermark)
        self.assertTrue(
            self.database.maintain_transient_data(minimum_interval_seconds=60)
        )

    def test_manual_cache_cleanup_is_owner_scoped(self) -> None:
        owner_candidate = self.candidate(
            "cleanup.owner.one",
            "public",
            review_cache={"avatar_preview": "data:image/jpeg;base64,b3duZXI="},
        )
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=owner_candidate["id"], decision="approved"
        )

        second_owner = self.service.register_user("cleanup-second-owner", PASSWORD)
        second_claim = self.claim("cleanup.owner.two", owner_id=second_owner["id"])
        second_candidate = self.service.create_workbench_candidate(
            second_owner["id"],
            claim_id=second_claim["claim_id"],
            username="cleanup.owner.two",
            visibility="public",
            profile={"display_name": "Second owner"},
            screening={"location_country": "United States"},
            review_cache={"avatar_preview": "data:image/jpeg;base64,c2Vjb25k"},
            source_mode="followers",
            source_target="source-account",
        )
        self.service.decide_workbench_candidate(
            second_owner["id"],
            candidate_id=second_candidate["id"],
            decision="approved",
        )

        result = self.service.clear_workbench_cache(self.owner["id"])
        self.assertEqual(1, result["cleared_entries"])
        with self.database.read() as connection:
            rows = {
                row["id"]: row["review_cache_json"]
                for row in connection.execute(
                    """
                    SELECT id, review_cache_json FROM workbench_candidates
                    WHERE id IN (?, ?)
                    """,
                    (owner_candidate["id"], second_candidate["id"]),
                ).fetchall()
            }
        self.assertEqual("{}", rows[owner_candidate["id"]])
        self.assertEqual(
            {"avatar_preview": "data:image/jpeg;base64,c2Vjb25k"},
            json.loads(rows[second_candidate["id"]]),
        )
        self.assertEqual(
            0,
            self.service.get_workbench_snapshot(self.owner["id"])["storage"]["cache_bytes"],
        )
        self.assertGreater(
            self.service.get_workbench_snapshot(second_owner["id"])["storage"]["cache_bytes"],
            0,
        )

    def test_storage_cache_clear_command_is_safe_and_does_not_change_capacity_state(self) -> None:
        from fastapi.testclient import TestClient

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            # Arrange after lifespan startup so automatic maintenance cannot
            # consume the terminal preview intended for this command test.
            self.candidate(
                "cleanup.api.pending.one",
                "public",
                review_cache={"avatar_preview": "data:image/jpeg;base64,b25l"},
            )
            self.candidate(
                "cleanup.api.pending.two",
                "public",
                review_cache={"avatar_preview": "data:image/jpeg;base64,dHdv"},
            )
            terminal = self.candidate(
                "cleanup.api.terminal",
                "private",
                review_cache={"avatar_preview": "data:image/jpeg;base64,dGVybWluYWw="},
            )
            self.service.decide_workbench_candidate(
                self.owner["id"], candidate_id=terminal["id"], decision="approved"
            )

            before_response = client.get(
                "/api/workbench/snapshot?limit=1&history_limit=1",
                headers=headers,
            )
            self.assertEqual(200, before_response.status_code, before_response.text)
            before = before_response.json()
            self.assertTrue(before["truncated"])
            self.assertTrue(before["has_more"]["pending_public_accounts"])
            self.assertGreater(before["storage"]["cache_bytes"], 0)
            self.assertGreater(before["storage"]["pending_preview_bytes"], 0)

            rejected = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "storage_cache_clear",
                    "payload": {"include_pending": True},
                },
            )
            self.assertEqual(422, rejected.status_code, rejected.text)
            self.assertEqual("validation_error", rejected.json()["code"])
            with self.database.read() as connection:
                self.assertNotEqual(
                    "{}",
                    connection.execute(
                        "SELECT review_cache_json FROM workbench_candidates WHERE id=?",
                        (terminal["id"],),
                    ).fetchone()[0],
                )

            cleared = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={"type": "storage_cache_clear", "payload": {}},
            )
            self.assertEqual(200, cleared.status_code, cleared.text)
            envelope = cleared.json()
            self.assertEqual("storage_cache_clear", envelope["command"])
            self.assertEqual(envelope["snapshot_seq"], envelope["result"]["snapshot_seq"])
            self.assertEqual(1, envelope["result"]["cleared_entries"])
            self.assertGreater(envelope["result"]["cleared_bytes"], 0)
            self.assertTrue(envelope["result"]["business_records_retained"])
            self.assertEqual(
                {
                    "global_dedupe": True,
                    "review_history": True,
                    "collection_exclusions": True,
                    "action_success_history": True,
                    "live_task_checkpoints": True,
                    "pending_review_previews": True,
                },
                envelope["result"]["retained"],
            )

            after_response = client.get(
                "/api/workbench/snapshot?limit=1&history_limit=1",
                headers=headers,
            )
            self.assertEqual(200, after_response.status_code, after_response.text)
            after = after_response.json()

        self.assertEqual(before["truncated"], after["truncated"])
        self.assertEqual(before["has_more"], after["has_more"])
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before["dedupe"], after["dedupe"])
        self.assertEqual(0, after["storage"]["cache_bytes"])
        self.assertEqual(
            before["storage"]["pending_preview_bytes"],
            after["storage"]["pending_preview_bytes"],
        )
        self.assertEqual(
            before["storage"]["last_maintenance_at"],
            after["storage"]["last_maintenance_at"],
        )

    def test_startup_bounds_diagnostic_events_without_touching_business_ledgers(self) -> None:
        rejected = self.candidate("retained.reject", "public")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=rejected["id"], decision="rejected"
        )
        exclusion_claim = self.claim("retained.exclusion")
        self.service.record_workbench_exclusion(
            self.owner["id"],
            claim_id=exclusion_claim["claim_id"],
            username="retained.exclusion",
            reason_code="non_us_location",
            reason="所在地非美国",
            location_country="Canada",
            profile={"location_zh": "Canada"},
        )
        campaign = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-retention", targets=["retained.success"],
            target_sources={}, message=None, interval_min_seconds=8,
            interval_max_seconds=15, limit_count=1,
        )
        target = campaign["targets"][0]
        attempt = self.service.start_action_attempt(
            self.owner["id"], campaign["id"], target["id"]
        )
        self.service.finish_action_attempt(
            self.owner["id"], campaign["id"], attempt,
            status="confirmed", details={"confirmation": "following"},
        )

        with self.database.write() as connection:
            connection.execute("DELETE FROM event_log")
            connection.executemany(
                """
                INSERT INTO event_log(
                    owner_user_id, entity_type, entity_id, event_type,
                    payload_json, created_at
                ) VALUES(?, 'diagnostic', ?, 'sample', '{}', datetime('now'))
                """,
                (
                    (self.owner["id"], f"event-{index}")
                    for index in range(50005)
                ),
            )
            connection.execute(
                """
                INSERT INTO event_log(
                    owner_user_id, entity_type, entity_id, event_type,
                    payload_json, created_at
                ) VALUES(?, 'diagnostic', 'very-old', 'sample', '{}', '2000-01-01T00:00:00Z')
                """,
                (self.owner["id"],),
            )

        self.database.initialize()
        with self.database.read() as connection:
            self.assertEqual(
                50000,
                connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE owner_user_id=?",
                    (self.owner["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_review_decisions"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM action_success_ledger"
            ).fetchone()[0])
            self.assertEqual({"retained.reject", "retained.exclusion", "retained.success"}, {
                row[0] for row in connection.execute(
                    "SELECT a.current_username_norm FROM instagram_accounts a "
                    "JOIN global_seen g ON g.account_id=a.id"
                )
            })

        # Cleanup is not startup-only.  A frequent workbench snapshot performs
        # an hourly-gated maintenance pass once the diagnostic log is due.
        with self.database.write() as connection:
            connection.executemany(
                """
                INSERT INTO event_log(
                    owner_user_id, entity_type, entity_id, event_type,
                    payload_json, created_at
                ) VALUES(?, 'diagnostic', ?, 'runtime', '{}', datetime('now'))
                """,
                (
                    (self.owner["id"], f"runtime-{index}")
                    for index in range(5)
                ),
            )
            connection.execute(
                """
                UPDATE workbench_state_revision
                SET last_cleanup_at=datetime('now', '-2 hours')
                WHERE singleton_id=1
                """
            )
        runtime_snapshot = self.service.get_workbench_snapshot(self.owner["id"])
        self.assertEqual(50000, runtime_snapshot["storage"]["event_log_entries"])
        self.assertIsNotNone(runtime_snapshot["storage"]["last_cleanup_at"])
        with self.database.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_review_decisions"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM action_success_ledger"
            ).fetchone()[0])
            self.assertEqual({"retained.reject", "retained.exclusion", "retained.success"}, {
                row[0] for row in connection.execute(
                    "SELECT a.current_username_norm FROM instagram_accounts a "
                    "JOIN global_seen g ON g.account_id=a.id"
                )
            })

    def test_startup_prunes_only_old_terminal_candidate_spool(self) -> None:
        task = self.service.create_task(
            self.owner["id"],
            name="旧技术缓存",
            modes=["followers"],
            targets=["cache.source"],
            window_ids=["cache-window"],
            settings={
                "location_enabled": True,
                "mode_limits": {"followers": {"per_target_limit": 3}},
            },
        )
        target = task["targets"][0]
        self.service.append_task_mode_candidates(
            self.owner["id"],
            task["id"],
            target["id"],
            "followers",
            ["cache.one", "cache.two", "cache.three"],
            max_total=3,
        )
        for username in ("cache.one", "cache.two", "cache.three"):
            self.service.finish_task_mode_candidate(
                self.owner["id"],
                task["id"],
                target["id"],
                "followers",
                username,
                state="deduped",
            )
        self.service.upsert_checkpoint(
            self.owner["id"],
            task["id"],
            target["id"],
            mode="followers",
            stage="mode_completed",
            cursor={
                "candidate_spool_version": 1,
                "candidate_spool_complete": True,
                "candidate_spool_natural_end": True,
            },
            counters={"discovered": 3, "processed": 3, "saved": 0},
            recoverable=True,
        )
        self.service.set_target_runtime_status(
            self.owner["id"], task["id"], target["id"], "completed"
        )
        self.service.finalize_task_runtime_status(
            self.owner["id"], task["id"], "completed"
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at='2000-01-01T00:00:00Z' WHERE id=?",
                (task["id"],),
            )
        self.database.initialize()

        with self.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_mode_candidates WHERE target_id=?",
                    (target["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                "completed",
                connection.execute(
                    "SELECT status FROM tasks WHERE id=?", (task["id"],)
                ).fetchone()[0],
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM global_seen").fetchone()[0],
                connection.execute(
                    "SELECT total_count FROM global_seen_stats WHERE singleton_id=1"
                ).fetchone()[0],
            )

    def test_new_queue_control_commands_are_reachable_through_unified_dispatcher(self) -> None:
        from fastapi.testclient import TestClient

        candidate = self.candidate("api.remove", "private")
        self.service.decide_workbench_candidate(
            self.owner["id"], candidate_id=candidate["id"], decision="approved"
        )
        campaign = self.service.create_action_campaign(
            self.owner["id"], operation="follow", execution_type="campaign",
            profile_id="window-api", targets=["api.remove"], target_sources={},
            message=None, interval_min_seconds=8, interval_max_seconds=15,
            limit_count=1,
        )
        target_id = campaign["targets"][0]["id"]
        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        app = create_app(settings, database=self.database, bitbrowser=EmptyBitBrowser())
        headers = {
            "X-Startup-Token": settings.startup_token,
            "Authorization": f"Bearer {login['token']}",
        }
        with TestClient(app) as client:
            paused = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "action_target_control",
                    "payload": {
                        "campaign_id": campaign["id"],
                        "target_id": target_id,
                        "action": "pause",
                    },
                },
            )
            self.assertEqual(200, paused.status_code, paused.text)
            self.assertEqual(
                {"command", "result", "snapshot_seq"}, set(paused.json())
            )
            self.assertEqual("paused", paused.json()["result"]["status"])

            dismissed = client.post(
                "/api/workbench/commands",
                headers=headers,
                json={
                    "type": "approved_candidate_dismiss",
                    "payload": {"candidate_id": candidate["id"]},
                },
            )
            self.assertEqual(200, dismissed.status_code, dismissed.text)
            self.assertEqual("dismissed", dismissed.json()["result"]["status"])
            self.assertTrue(dismissed.json()["result"]["global_dedupe_retained"])

    def test_workbench_manual_action_returns_durable_background_campaign_ack(self) -> None:
        from fastapi.testclient import TestClient

        submitted: dict[str, Any] = {}

        async def fake_submit(
            _manager: ActionCampaignManager,
            owner_user_id: str,
            **payload: Any,
        ) -> dict[str, Any]:
            submitted.update({"owner_user_id": owner_user_id, **payload})
            return {"id": "manual-background-campaign", "status": "running"}

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        with patch.object(ActionCampaignManager, "submit_manual_action", fake_submit):
            app = create_app(
                settings,
                database=self.database,
                bitbrowser=EmptyBitBrowser(),
            )
            headers = {
                "X-Startup-Token": settings.startup_token,
                "Authorization": f"Bearer {login['token']}",
            }
            with TestClient(app) as client:
                response = client.post(
                    "/api/workbench/commands",
                    headers=headers,
                    json={
                        "type": "action_manual",
                        "payload": {
                            "operation": "follow",
                            "profile_id": "manual-window",
                            "target": "manual.target",
                            "source_target": "source.account",
                        },
                    },
                )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("action_manual", body["command"])
        self.assertEqual(
            {"campaign_id": "manual-background-campaign", "status": "running"},
            body["result"],
        )
        self.assertEqual(self.owner["id"], submitted["owner_user_id"])
        self.assertEqual("manual.target", submitted["target"])
        self.assertEqual("source.account", submitted["source_target"])

    def test_legacy_manual_action_endpoint_keeps_synchronous_contract(self) -> None:
        from fastapi.testclient import TestClient

        async def fake_legacy_manual(
            _manager: ActionCampaignManager,
            owner_user_id: str,
            **payload: Any,
        ) -> dict[str, Any]:
            self.assertEqual(self.owner["id"], owner_user_id)
            self.assertEqual("legacy.target", payload["target"])
            return {
                "campaign_id": "legacy-manual-campaign",
                "operation": "follow",
                "target": "legacy.target",
                "status": "confirmed",
                "confirmation": "visible_state_changed",
            }

        async def background_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy endpoint must retain synchronous manual_action")

        login = self.service.login("workbench-owner", PASSWORD)
        settings = Settings(
            startup_token="test-startup-token-that-is-long-enough-456",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        with (
            patch.object(ActionCampaignManager, "manual_action", fake_legacy_manual),
            patch.object(ActionCampaignManager, "submit_manual_action", background_must_not_run),
        ):
            app = create_app(
                settings,
                database=self.database,
                bitbrowser=EmptyBitBrowser(),
            )
            headers = {
                "X-Startup-Token": settings.startup_token,
                "Authorization": f"Bearer {login['token']}",
            }
            with TestClient(app) as client:
                response = client.post(
                    "/api/actions/manual",
                    headers=headers,
                    json={
                        "operation": "follow",
                        "profile_id": "legacy-window",
                        "target": "legacy.target",
                    },
                )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("confirmed", response.json()["status"])
        self.assertEqual("legacy-manual-campaign", response.json()["campaign_id"])


if __name__ == "__main__":
    unittest.main()
