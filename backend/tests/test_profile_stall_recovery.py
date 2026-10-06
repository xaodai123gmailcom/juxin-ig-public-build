from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.playwright_worker import (
    PlaywrightWorker, VisibleProfile, WorkerExecutionError, CollectionOutcome, is_instagram_navigation_shell,
)
from app.execution_manager import ExecutionManager


class ProfileStallRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_retry_creates_fresh_same_context_page_and_preserves_119_results(self):
        class Page:
            closed = False
            url = "about:blank"

            async def goto(self, url, **kwargs):
                self.url = url

            async def close(self):
                self.closed = True

        class Context:
            def __init__(self):
                self.created = []

            async def new_page(self):
                page = Page()
                self.created.append(page)
                return page

        checks = []
        old, context = Page(), Context()

        class Worker(PlaywrightWorker):
            async def _new_active_page_session(self, page):
                return None

            async def _validate_recovery_profile_page(self, page, username, **kwargs):
                checks.append((page.url, username))
                if len(checks) == 1:
                    raise WorkerExecutionError("temporary blank", reason="instagram_profile_not_ready")

            async def _collect_relation_once(self, target, **kwargs):
                self_test.assertEqual(119, kwargs["initial_candidate_count"])
                self_test.assertEqual(["saved_118", "saved_119"], kwargs["initial_resume_tail"])
                self_test.assertFalse(old.closed, "old page remains until the replacement makes progress")
                await kwargs["progress_sink"]({"discovered_count": 120, "resume_tail": ["saved_119", "new_120"]})
                self_test.assertTrue(old.closed)
                return CollectionOutcome("followers", [], source_total=962)

        self_test = self
        worker = Worker(None)
        worker.page, worker._context = old, context
        worker.request_page_replacement("source", "instagram_followers_list_incomplete")
        self.assertEqual([], context.created, "request only queues work; the task coroutine opens the page")
        with self.assertRaises(WorkerExecutionError) as error:
            await worker.collect_followers("source", limit=None, initial_candidate_count=119, initial_resume_tail=["saved_118", "saved_119"])
        self.assertFalse(error.exception.details["auto_retry"])
        self.assertIs(worker.page, old)
        self.assertFalse(old.closed)
        self.assertTrue(context.created[0].closed)
        worker.request_page_replacement("source", "instagram_followers_list_incomplete")
        await worker.collect_followers("source", limit=None, initial_candidate_count=119, initial_resume_tail=["saved_118", "saved_119"])
        self.assertIs(worker._context, context)
        self.assertIs(worker.page, context.created[1])
        self.assertEqual(2, len(context.created))
        self.assertEqual([("https://www.instagram.com/source/", "source")] * 2, checks)
        self.assertEqual({}, worker._fresh_page_retry_targets)

    async def test_fresh_page_login_guard_is_propagated_without_attempting_collection(self):
        class Worker(PlaywrightWorker):
            async def _recover_stalled_profile_page(self, target):
                raise WorkerExecutionError("login needed", reason="instagram_login_required")

            async def _collect_relation_once(self, target, **kwargs):
                self_test.fail("login page must never be collected")

        self_test = self
        worker = Worker(None)
        worker.request_page_replacement("source", "instagram_followers_list_incomplete")
        with self.assertRaises(WorkerExecutionError) as error:
            await worker.collect_followers("source", limit=None)
        self.assertEqual("instagram_login_required", error.exception.code)

    def test_abandoned_page_operations_cannot_schedule_replacements_before_reconnect(self):
        worker = PlaywrightWorker(None)
        worker._page_stage_abandoned = True
        self.assertFalse(worker.request_page_replacement("source"))
        self.assertEqual({}, worker._fresh_page_retry_targets)

    async def test_replacement_budget_is_shared_across_profile_and_location(self):
        class Worker(PlaywrightWorker):
            openings = 0

            async def _recover_stalled_profile_page(self, username):
                self.openings += 1
                return True

            async def _read_visible_account_location_attempts(self, target):
                raise WorkerExecutionError("stalled", reason="browser_window_surface_unstable", pause_required=True)

        worker = Worker(None)
        await worker._replace_stuck_page_once("target")
        with self.assertRaises(WorkerExecutionError) as error:
            await worker.read_visible_account_location("target")
        self.assertEqual(1, worker.openings)
        self.assertEqual("instagram_page_recovery_exhausted", error.exception.code)
        # One worker attempt stays bounded; the owning manager may admit the
        # next attempt after its cooldown without requiring an operator click.
        self.assertFalse(ExecutionManager._is_profile_intervention_error(error.exception))
        self.assertTrue(ExecutionManager._is_instagram_surface_retry_error(error.exception))
        self.assertEqual("browser_window_surface_unstable", error.exception.details["original_reason"])
        self.assertIsNone(error.exception.details.get("retry_after_seconds"))
        worker.prepare_page_retry("target")
        await worker._replace_stuck_page_once("target")
        self.assertEqual(2, worker.openings)

    async def test_failed_new_page_restores_old_and_never_closes_it(self):
        class Page:
            closed = False

            async def close(self):
                self.closed = True

        worker = PlaywrightWorker(None)
        old, new = Page(), Page()
        worker.page = new
        worker._worker_owned_page = new
        worker._pending_recovery_old = (old, None, None)
        await worker._finish_page_recovery(progressed=False)
        self.assertIs(old, worker.page)
        self.assertFalse(old.closed)
        self.assertTrue(new.closed)

    async def test_relation_stall_tries_one_new_page_then_requires_intervention(self):
        class Worker(PlaywrightWorker):
            calls = []
            openings = 0

            async def _collect_relation_once(self, target, **kwargs):
                self.calls.append((kwargs["initial_candidate_count"], list(kwargs["initial_resume_tail"])))
                if len(self.calls) == 1:
                    await kwargs["progress_sink"]({"discovered_count": 12, "resume_tail": ["saved_a", "saved_b"]})
                raise WorkerExecutionError("stalled", reason="instagram_followers_list_incomplete", pause_required=True)

            async def _recover_stalled_profile_page(self, username):
                self.openings += 1
                return True

        worker = Worker(None)
        with self.assertRaises(WorkerExecutionError) as error:
            await worker.collect_followers("source", limit=None, initial_candidate_count=10, initial_resume_tail=["old_tail"])
        self.assertEqual([(10, ["old_tail"]), (12, ["saved_a", "saved_b"])], worker.calls)
        self.assertEqual(1, worker.openings)
        self.assertEqual("instagram_page_recovery_exhausted", error.exception.code)
        self.assertFalse(error.exception.details["auto_retry"])

    async def test_confirmed_white_shell_opens_replacement_without_old_page_retries(self):
        class Worker(PlaywrightWorker):
            reads = 0
            replacements = 0

            async def _has_visible_loading_indicator(self):
                return False

            async def _read_visible_profile_once(self, target, **kwargs):
                self.reads += 1
                if self.reads == 1:
                    reason = await self._profile_transport_failure(target, body_text="消息")
                    raise WorkerExecutionError("white shell", reason=reason, pause_required=True)
                return VisibleProfile(username=target, visibility="public", followers=1, following=2, posts=3)

            async def _recover_stalled_profile_page(self, username):
                self.replacements += 1
                return True

        worker = Worker(None)
        worker.page = type("Page", (), {"url": "https://www.instagram.com/target/"})()
        result = await worker.read_visible_profile("target")
        self.assertEqual("target", result.username)
        self.assertEqual(2, worker.reads)
        self.assertEqual(1, worker.replacements)

    async def test_validated_replacement_is_not_navigated_a_second_time(self):
        class Worker(PlaywrightWorker):
            async def _guard(self):
                pass

            async def _ensure_window_surface_stable(self):
                pass

        class Page:
            url = "https://www.instagram.com/target/"

            async def goto(self, *args, **kwargs):
                raise AssertionError("healthy replacement must not be reloaded")

        worker = Worker(None)
        worker.page = Page()
        worker._validated_recovery_profile = (worker.page, "target")
        self.assertEqual("target", await worker._navigate_profile("target"))
        self.assertIsNone(worker._validated_recovery_profile)

    async def test_message_only_shell_is_retryable_not_profile_data(self):
        class Worker(PlaywrightWorker):
            async def _has_visible_loading_indicator(self):
                return False

        worker = Worker(None)
        worker.page = type("Page", (), {"url": "https://www.instagram.com/sample_stall01/"})()
        for text in ("消息", "Instagram\nMessages\n3", "首頁\n訊息"):
            self.assertTrue(is_instagram_navigation_shell(text))
            self.assertEqual("instagram_profile_not_ready", await worker._profile_surface_is_transient(
                "sample_stall01", body_text=text,
            ))
        self.assertFalse(is_instagram_navigation_shell("sample_stall01\n0 posts 20 followers 30 following"))

    async def test_hung_profile_read_recovers_once_and_retries_same_account(self):
        class Worker(PlaywrightWorker):
            calls = 0
            recoveries = []

            async def _read_visible_profile_once(self, target, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.Event().wait()
                return VisibleProfile(username=target, visibility="public", followers=20, following=30, posts=4)

            async def _recover_stalled_profile_page(self, username):
                self.recoveries.append(username)
                return True

        worker = Worker(None)
        worker.profile_read_timeout_seconds = 0.01
        result = await asyncio.wait_for(worker.read_visible_profile("target"), 1)
        self.assertEqual("target", result.username)
        self.assertEqual(2, worker.calls)
        self.assertEqual(["target"], worker.recoveries)

    async def test_cancellation_hostile_read_quarantines_worker_without_new_tab(self):
        release = asyncio.Event()

        class Worker(PlaywrightWorker):
            calls = 0

            async def _read_visible_profile_once(self, target, **kwargs):
                self.calls += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()

            async def _recover_stalled_profile_page(self, username):
                raise AssertionError("must not overlap late read with a new tab")

        worker = Worker(None)
        worker.profile_read_timeout_seconds = 0.01
        try:
            with self.assertRaises(WorkerExecutionError) as error:
                await asyncio.wait_for(worker.read_visible_profile("target"), 1)
            self.assertEqual("worker_not_connected", error.exception.code)
            with self.assertRaises(WorkerExecutionError):
                await worker.read_visible_profile("next")
            self.assertEqual(1, worker.calls)
        finally:
            release.set()
            await asyncio.gather(*worker._late_lifecycle_tasks, return_exceptions=True)

    async def test_location_stall_releases_shared_window_lock(self):
        class Worker(PlaywrightWorker):
            async def _read_visible_account_location_once(self, target, **kwargs):
                await asyncio.Event().wait()

        worker = Worker(None)
        worker.location_read_timeout_seconds = 0.01
        worker.location_request_min_interval_seconds = 0
        worker.location_request_max_interval_seconds = 0
        with self.assertRaises(WorkerExecutionError):
            await asyncio.wait_for(worker.read_visible_account_location("target"), 1)
        self.assertFalse(worker._location_request_coordinator.lock.locked())

    async def test_failed_recovery_never_returns_empty_completed_profile(self):
        class Worker(PlaywrightWorker):
            async def _read_visible_profile_once(self, target, **kwargs):
                await asyncio.Event().wait()

            async def _recover_stalled_profile_page(self, username):
                return False

        worker = Worker(None)
        worker.profile_read_timeout_seconds = 0.01
        with self.assertRaises(WorkerExecutionError) as error:
            await worker.read_visible_profile("target")
        self.assertEqual("instagram_page_recovery_exhausted", error.exception.code)
        self.assertTrue(error.exception.details["pause_required"])

    async def test_fifty_five_percent_boundary_and_unknowns(self):
        for category, confidence, checked, expected in (
            ("male", .549999, True, False), ("male", .55, True, True),
            ("male", .550001, True, True), ("male", .99, False, False),
            ("male", None, True, False), ("male", float("nan"), True, False),
            ("male", 1.5, True, False), ("female", .99, True, False),
            ("couple", .99, True, False), ("unknown", .99, True, False),
        ):
            with self.subTest(category=category, confidence=confidence, checked=checked):
                result = ExecutionManager._male_avatar_filter_excludes(
                    {"exclude_male_avatar": True},
                    {"person_recognition": {"category": category, "confidence": confidence, "checked": checked, "source": "local_openvino", "male_exclusion_confirmed": True}},
                )
                self.assertEqual(expected, result)
