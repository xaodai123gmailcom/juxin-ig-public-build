from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.action_manager import ActionCampaignManager
from app.bitbrowser_v2 import BitBrowserClient, _restore_windows_process_to_front
from app.database import Database
from app.errors import UpstreamUnavailableError
from app.playwright_worker import ActionOutcome, PlaywrightWorker, WorkerExecutionError
from app.service import CoreService


PASSWORD = "Correct-Horse-Battery-Staple-42!"


class RecordingBitBrowser:
    def __init__(self) -> None:
        self.foregrounded: list[str] = []

    def close_profile(self, profile_id: str) -> dict[str, Any]:
        return {"profile_id": profile_id, "closed": True}

    def bring_profile_to_front(self, profile_id: str) -> dict[str, Any]:
        self.foregrounded.append(profile_id)
        return {"profile_id": profile_id, "restored": True, "foregrounded": True}


class ScriptedFollowWorker:
    scripts: dict[str, list[str]] = {}
    calls: list[tuple[str, str]] = []
    connects: dict[str, int] = {}

    def __init__(self, _bitbrowser: Any) -> None:
        self.profile_id = ""

    @classmethod
    def reset(cls, scripts: dict[str, list[str]]) -> None:
        cls.scripts = {key: list(value) for key, value in scripts.items()}
        cls.calls = []
        cls.connects = {}

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id
        type(self).connects[profile_id] = type(self).connects.get(profile_id, 0) + 1

    async def disconnect(self) -> None:
        return None

    async def execute_action(
        self, operation: str, target: str, *, message: str | None = None
    ) -> ActionOutcome:
        del message
        type(self).calls.append((self.profile_id, target))
        outcome = type(self).scripts[self.profile_id].pop(0)
        if outcome == "success":
            return ActionOutcome(operation, target, "confirmed", "following")
        if outcome == "already":
            return ActionOutcome(operation, target, "already_done", "following")
        if outcome == "network":
            raise WorkerExecutionError(
                "read Instagram page was interrupted; the task will wait and resume from its checkpoint",
                reason="worker_not_connected",
                pause_required=True,
                status_code=503,
            )
        if outcome == "unknown":
            raise WorkerExecutionError(
                "follow click outcome is unknown",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            )
        if outcome == "login":
            raise WorkerExecutionError(
                "Instagram login is required",
                reason="instagram_login_required",
                pause_required=True,
            )
        raise AssertionError(f"unknown scripted outcome: {outcome}")


class InitialConnectionFailureWorker(ScriptedFollowWorker):
    initial_failures: dict[str, int] = {}

    @classmethod
    def reset(cls, scripts: dict[str, list[str]]) -> None:
        super().reset(scripts)
        cls.initial_failures = {key: 1 for key in scripts}

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        await super().connect(profile_id, open_if_needed=open_if_needed)
        if type(self).initial_failures.get(profile_id, 0):
            type(self).initial_failures[profile_id] -= 1
            raise UpstreamUnavailableError(
                "BitBrowser connection unavailable",
                details={"reason": "worker_not_connected", "pause_required": True},
            )


class FastActionCampaignManager(ActionCampaignManager):
    waits: list[int] = []

    @staticmethod
    async def _wait_with_pause(control: Any, seconds: int) -> None:
        del control
        FastActionCampaignManager.waits.append(seconds)


class ActionFollowFailurePolicyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "follow-policy.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user("follow-policy-user", PASSWORD)
        self.bitbrowser = RecordingBitBrowser()
        self.managers: list[ActionCampaignManager] = []
        FastActionCampaignManager.waits = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.shutdown()
        self.temp_dir.cleanup()

    def manager(self, worker_factory: type[ScriptedFollowWorker]) -> FastActionCampaignManager:
        manager = FastActionCampaignManager(
            self.service,
            self.bitbrowser,  # type: ignore[arg-type]
            worker_factory=worker_factory,
        )
        self.managers.append(manager)
        return manager

    async def start(
        self,
        manager: ActionCampaignManager,
        *,
        profile_id: str,
        targets: list[str],
    ) -> dict[str, Any]:
        return await manager.start_campaign(
            self.user["id"],
            operation="follow",
            profile_id=profile_id,
            targets=targets,
            message=None,
            interval="1 秒",
            limit=len(targets),
        )

    async def test_network_failures_continue_then_third_failure_foregrounds_only_that_window(self) -> None:
        ScriptedFollowWorker.reset(
            {
                "failing-window": ["network", "network", "network", "success"],
                "healthy-window": ["success"],
            }
        )
        manager = self.manager(ScriptedFollowWorker)
        failing = await self.start(
            manager,
            profile_id="failing-window",
            targets=["first", "second", "third", "fourth"],
        )
        healthy = await self.start(
            manager,
            profile_id="healthy-window",
            targets=["healthy"],
        )
        await manager.wait(failing["id"])
        await manager.wait(healthy["id"])

        paused = self.service.get_action_campaign(self.user["id"], failing["id"])
        completed = self.service.get_action_campaign(self.user["id"], healthy["id"])
        self.assertEqual("paused", paused["status"])
        self.assertEqual(
            ["failed", "failed", "failed", "pending"],
            [target["status"] for target in paused["targets"]],
        )
        self.assertIn("连续 3 个关注目标失败", paused["last_error"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(["failing-window"], self.bitbrowser.foregrounded)
        self.assertEqual(3, ScriptedFollowWorker.connects["failing-window"])
        self.assertEqual([1, 1], FastActionCampaignManager.waits)

        await manager.resume(self.user["id"], failing["id"])
        await manager.wait(failing["id"])
        resumed = self.service.get_action_campaign(self.user["id"], failing["id"])
        self.assertEqual("completed", resumed["status"])
        self.assertEqual("confirmed", resumed["targets"][3]["status"])

    async def test_success_resets_window_failure_streak(self) -> None:
        ScriptedFollowWorker.reset(
            {"reset-window": ["network", "success", "network", "network", "success"]}
        )
        manager = self.manager(ScriptedFollowWorker)
        campaign = await self.start(
            manager,
            profile_id="reset-window",
            targets=["one", "two", "three", "four", "five"],
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(
            ["failed", "confirmed", "failed", "failed", "confirmed"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual([], self.bitbrowser.foregrounded)
        self.assertEqual([1, 1, 1, 1], FastActionCampaignManager.waits)

    async def test_already_done_also_resets_window_failure_streak(self) -> None:
        ScriptedFollowWorker.reset(
            {
                "already-window": [
                    "network",
                    "network",
                    "already",
                    "network",
                    "network",
                    "success",
                ]
            }
        )
        manager = self.manager(ScriptedFollowWorker)
        campaign = await self.start(
            manager,
            profile_id="already-window",
            targets=["one", "two", "three", "four", "five", "six"],
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(
            ["failed", "failed", "already_done", "failed", "failed", "confirmed"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual([], self.bitbrowser.foregrounded)

    async def test_unknown_post_click_outcome_pauses_immediately(self) -> None:
        ScriptedFollowWorker.reset({"unknown-window": ["unknown", "success"]})
        manager = self.manager(ScriptedFollowWorker)
        campaign = await self.start(
            manager,
            profile_id="unknown-window",
            targets=["uncertain", "remaining"],
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["unknown", "pending"], [target["status"] for target in stored["targets"]]
        )
        self.assertEqual(["unknown-window"], self.bitbrowser.foregrounded)
        self.assertEqual([], FastActionCampaignManager.waits)

    async def test_login_challenge_still_pauses_on_first_target(self) -> None:
        ScriptedFollowWorker.reset({"login-window": ["login", "success"]})
        manager = self.manager(ScriptedFollowWorker)
        campaign = await self.start(
            manager,
            profile_id="login-window",
            targets=["needs_login", "remaining_login_target"],
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["failed", "pending"], [target["status"] for target in stored["targets"]]
        )
        self.assertEqual(["login-window"], self.bitbrowser.foregrounded)
        self.assertEqual([], FastActionCampaignManager.waits)

    async def test_initial_connection_failure_is_one_failed_target_not_a_stopped_queue(self) -> None:
        InitialConnectionFailureWorker.reset({"connect-window": ["success"]})
        manager = self.manager(InitialConnectionFailureWorker)
        campaign = await self.start(
            manager,
            profile_id="connect-window",
            targets=["connection_failed", "next_target"],
        )
        await manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(
            ["failed", "confirmed"], [target["status"] for target in stored["targets"]]
        )
        self.assertEqual(2, InitialConnectionFailureWorker.connects["connect-window"])
        self.assertEqual([1], FastActionCampaignManager.waits)

    async def test_native_restore_without_foreground_uses_page_fallback(self) -> None:
        class RestoreOnlyBitBrowser:
            def bring_profile_to_front(self, profile_id: str) -> dict[str, Any]:
                return {
                    "profile_id": profile_id,
                    "restored": True,
                    "foregrounded": False,
                }

        class PageFallbackWorker:
            called = False

            async def bring_window_to_front(self) -> bool:
                self.called = True
                return True

        manager = FastActionCampaignManager(
            self.service,
            RestoreOnlyBitBrowser(),  # type: ignore[arg-type]
            worker_factory=ScriptedFollowWorker,
        )
        self.managers.append(manager)
        worker = PageFallbackWorker()
        await manager._bring_profile_to_front(worker, "fallback-window")
        self.assertTrue(worker.called)


class FollowClickBoundaryTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_after_click_becomes_unknown(self) -> None:
        class Button:
            clicked = False

            async def click(self) -> None:
                self.clicked = True

        class GuardFailureWorker(PlaywrightWorker):
            async def _guard(self) -> None:
                raise WorkerExecutionError(
                    "page disconnected during confirmation",
                    reason="worker_not_connected",
                    pause_required=True,
                    status_code=503,
                )

        worker = GuardFailureWorker(RecordingBitBrowser())  # type: ignore[arg-type]
        button = Button()
        with patch("app.playwright_worker.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(WorkerExecutionError) as raised:
                await worker._click_and_confirm_follow("target", button)
        self.assertTrue(button.clicked)
        self.assertEqual("instagram_action_outcome_unknown", raised.exception.code)


class BitBrowserForegroundHelpersTestCase(unittest.TestCase):
    def test_pid_payload_normalization_and_non_windows_fallback(self) -> None:
        self.assertEqual(
            {"window-a": 1234, "window-b": 5678},
            BitBrowserClient._extract_profile_pids(
                {
                    "success": True,
                    "data": {
                        "window-a": 1234,
                        "records": [{"browserId": "window-b", "processId": "5678"}],
                        "closed-window": 0,
                    },
                }
            ),
        )
        result = _restore_windows_process_to_front(1234)
        if result["supported"] is False:
            self.assertEqual("platform_not_windows", result["reason"])
            self.assertFalse(result["restored"])


if __name__ == "__main__":
    unittest.main()
