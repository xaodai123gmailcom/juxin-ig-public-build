from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.action_manager import ActionCampaignManager, ActionControl
from app.database import Database
from app.errors import ConflictError, UpstreamUnavailableError
from app.playwright_worker import ActionOutcome, WorkerExecutionError
from app.service import CoreService


class _ScriptedGreetingWorker:
    scripts: dict[str, list[str]] = {}
    connect_failures: set[str] = set()
    calls: list[tuple[str, str]] = []

    def __init__(self, _bitbrowser: object) -> None:
        self.profile_id = ""

    @classmethod
    def reset(
        cls,
        scripts: dict[str, list[str]],
        *,
        connect_failures: set[str] | None = None,
    ) -> None:
        cls.scripts = {key: list(values) for key, values in scripts.items()}
        cls.connect_failures = set(connect_failures or set())
        cls.calls = []

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id
        if profile_id in type(self).connect_failures:
            raise RuntimeError("window bootstrap failed")

    async def disconnect(self) -> None:
        return None

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del message
        type(self).calls.append((self.profile_id, target))
        outcome = type(self).scripts[self.profile_id].pop(0)
        if outcome == "missing":
            raise WorkerExecutionError(
                "exact inbox recipient did not appear",
                reason="instagram_direct_inbox_recipient_not_found",
                pause_required=False,
            )
        if outcome == "other_failure":
            raise WorkerExecutionError(
                "profile content did not render",
                reason="instagram_content_not_visible",
                pause_required=False,
            )
        if outcome == "confirmed":
            return ActionOutcome(operation, target, "confirmed", "message visible")
        raise AssertionError(f"unsupported scripted outcome: {outcome}")


class _NoDelayGreetingManager(ActionCampaignManager):
    async def _wait_for_next_action_target(
        self, control: object, campaign: dict[str, object]
    ) -> None:
        del control, campaign


class _BlockingGreetingWorker:
    started: asyncio.Event | None = None

    def __init__(self, _bitbrowser: object) -> None:
        self.profile_id = ""

    @classmethod
    def reset(cls) -> None:
        cls.started = asyncio.Event()

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id

    async def disconnect(self) -> None:
        return None

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del operation, target, message
        assert type(self).started is not None
        type(self).started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking greeting should have been cancelled")


class _BlockingCleanupGreetingWorker(_BlockingGreetingWorker):
    disconnect_started: asyncio.Event | None = None
    disconnect_release: asyncio.Event | None = None

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.disconnect_started = asyncio.Event()
        cls.disconnect_release = asyncio.Event()

    async def disconnect(self) -> None:
        assert type(self).disconnect_started is not None
        assert type(self).disconnect_release is not None
        type(self).disconnect_started.set()
        await type(self).disconnect_release.wait()


class _PreSendBlockingGreetingWorker(_BlockingGreetingWorker):
    sent: list[str] = []

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.sent = []

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del message
        assert type(self).started is not None
        type(self).started.set()
        await asyncio.Event().wait()
        type(self).sent.append(target)
        return ActionOutcome(operation, target, "confirmed", "sent")


class _CancellationToUnknownGreetingWorker(_BlockingGreetingWorker):
    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        del operation, target, message
        assert type(self).started is not None
        type(self).started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise WorkerExecutionError(
                "send may already have triggered",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            ) from exc
        raise AssertionError("blocking greeting should have been cancelled")


class GreetingQueueSafetyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = Database(Path(self.temporary.name) / "greeting-queue.sqlite3")
        database.initialize()
        self.service = CoreService(database)
        self.owner = self.service.register_user(
            "greeting-queue-owner",
            "Correct-Horse-Battery-Staple-42!",
        )
        self.manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temporary.cleanup()

    async def _start(
        self,
        profile_id: str,
        targets: list[str],
    ) -> dict[str, object]:
        campaign = await self.manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id=profile_id,
            targets=targets,
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=len(targets),
        )
        await self.manager.wait(str(campaign["id"]))
        return self.service.get_action_campaign(
            self.owner["id"], str(campaign["id"])
        )

    def _assert_action_runtime_released(self, campaign_id: str) -> None:
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM browser_operation_leases WHERE entity_id=?",
                    (campaign_id,),
                ).fetchone()[0],
            )

    async def test_three_consecutive_missing_recipients_pause_before_fourth(self) -> None:
        _ScriptedGreetingWorker.reset(
            {"missing-window": ["missing", "missing", "missing", "confirmed"]}
        )

        stored = await self._start(
            "missing-window",
            ["one.person", "two.person", "three.person", "must.wait"],
        )

        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["failed", "failed", "failed", "pending"],
            [target["status"] for target in stored["targets"]],  # type: ignore[index]
        )
        self.assertEqual(3, len(_ScriptedGreetingWorker.calls))
        self.assertIn("连续 3 个账号", str(stored["last_error"]))

    async def test_success_resets_missing_recipient_streak(self) -> None:
        _ScriptedGreetingWorker.reset(
            {
                "reset-window": [
                    "missing",
                    "missing",
                    "confirmed",
                    "missing",
                    "missing",
                    "confirmed",
                ]
            }
        )

        stored = await self._start(
            "reset-window",
            [f"person.{index}" for index in range(6)],
        )

        self.assertEqual("completed", stored["status"])
        self.assertEqual(6, len(_ScriptedGreetingWorker.calls))
        self.assertEqual(
            ["failed", "failed", "confirmed", "failed", "failed", "confirmed"],
            [target["status"] for target in stored["targets"]],  # type: ignore[index]
        )

    async def test_different_failure_resets_missing_recipient_streak(self) -> None:
        _ScriptedGreetingWorker.reset(
            {
                "mixed-window": [
                    "missing",
                    "missing",
                    "other_failure",
                    "missing",
                    "missing",
                    "confirmed",
                ]
            }
        )

        stored = await self._start(
            "mixed-window",
            [f"mixed.person.{index}" for index in range(6)],
        )

        self.assertEqual("completed", stored["status"])
        self.assertEqual(6, len(_ScriptedGreetingWorker.calls))
        self.assertEqual(
            ["failed", "failed", "failed", "failed", "failed", "confirmed"],
            [target["status"] for target in stored["targets"]],  # type: ignore[index]
        )

    async def test_missing_recipient_streak_is_isolated_per_window(self) -> None:
        _ScriptedGreetingWorker.reset(
            {
                "window-a": ["missing", "missing"],
                "window-b": ["missing", "confirmed"],
            }
        )

        first = await self._start(
            "window-a", ["window.a.one", "window.a.two"]
        )
        second = await self._start(
            "window-b", ["window.b.one", "window.b.two"]
        )

        self.assertEqual("completed", first["status"])
        self.assertEqual("completed", second["status"])
        self.assertEqual(
            ["failed", "confirmed"],
            [target["status"] for target in second["targets"]],  # type: ignore[index]
        )

    async def test_initial_connect_failure_pauses_only_that_window_and_keeps_pending(self) -> None:
        _ScriptedGreetingWorker.reset(
            {
                "bad-window": ["confirmed", "confirmed"],
                "good-window": ["confirmed"],
            },
            connect_failures={"bad-window"},
        )

        bad = await self._start("bad-window", ["first.pending", "second.pending"])
        good = await self._start("good-window", ["good.person"])

        self.assertEqual("paused", bad["status"])
        self.assertEqual(
            ["pending", "pending"],
            [target["status"] for target in bad["targets"]],  # type: ignore[index]
        )
        self.assertEqual([], bad["attempts"])
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (bad["id"],),
                ).fetchone()[0],
            )
        self.assertEqual("completed", good["status"])
        self.assertEqual("confirmed", good["targets"][0]["status"])  # type: ignore[index]
        self.assertEqual([("good-window", "good.person")], _ScriptedGreetingWorker.calls)

    async def test_campaign_task_cancellation_fences_running_greeting_as_unknown(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="cancelled-campaign-window",
            targets=["possibly.sent", "must.remain.pending"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=2,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None
        control.task.cancel()
        await asyncio.gather(control.task, return_exceptions=True)
        self.assertTrue(control.task.cancelled())

        stored = self.service.get_action_campaign(
            self.owner["id"], campaign["id"]
        )
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["unknown", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(["unknown"], [item["status"] for item in stored["attempts"]])
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0],
            )
        await manager.shutdown()

    async def test_same_tick_shutdown_rejects_unscheduled_manual_action(self) -> None:
        _ScriptedGreetingWorker.reset(
            {"late-manual-window": ["confirmed"]}
        )
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="late-manual-window",
                target="after.shutdown",
                message="Hello",
            )
        )

        # Deliberately do not yield after create_task: shutdown must close admission
        # before this coroutine receives its first instruction.
        await manager.shutdown()
        with self.assertRaisesRegex(ConflictError, "shutting down"):
            await action_task

        self.assertEqual([], _ScriptedGreetingWorker.calls)
        self.assertEqual([], self.service.list_action_campaigns(self.owner["id"]))
        self.assertEqual(set(), manager.active_campaign_ids())
        self.assertEqual({}, manager._manual_controls)

    async def test_same_tick_shutdown_rejects_unscheduled_campaign_start(self) -> None:
        _ScriptedGreetingWorker.reset(
            {"late-campaign-window": ["confirmed"]}
        )
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.start_campaign(
                self.owner["id"],
                operation="greet",
                profile_id="late-campaign-window",
                targets=["after.shutdown"],
                message="Hello",
                messages=["Hello"],
                interval="1 秒",
                limit=1,
            )
        )

        await manager.shutdown()
        with self.assertRaisesRegex(ConflictError, "shutting down"):
            await action_task

        self.assertEqual([], _ScriptedGreetingWorker.calls)
        self.assertEqual([], self.service.list_action_campaigns(self.owner["id"]))
        self.assertEqual(set(), manager.active_campaign_ids())
        self.assertEqual({}, manager._campaigns)

    async def test_manual_action_external_cancellation_fences_unknown(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="cancelled-manual-window",
                target="possibly.sent.manual",
                message="Hello",
            )
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        action_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action_task
        self.assertTrue(action_task.cancelled())

        stored = self.service.list_action_campaigns(self.owner["id"], limit=10)[0]
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (stored["id"],),
                ).fetchone()[0],
            )
        await manager.shutdown()

    async def test_manual_connect_domain_error_is_recoverable_without_residue(self) -> None:
        profile_id = "manual-connect-domain-error-window"
        _ScriptedGreetingWorker.reset({profile_id: ["confirmed"]})
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )

        with patch.object(
            _ScriptedGreetingWorker,
            "connect",
            side_effect=UpstreamUnavailableError("temporary connect failure"),
        ):
            with self.assertRaises(UpstreamUnavailableError):
                await manager.manual_action(
                    self.owner["id"],
                    operation="greet",
                    profile_id=profile_id,
                    target="send.after.reconnect",
                    message="Hello",
                )

        recoverable = self.service.list_action_campaigns(
            self.owner["id"], limit=10
        )[0]
        self.assertEqual("recoverable", recoverable["status"])
        self.assertEqual("pending", recoverable["targets"][0]["status"])
        self.assertEqual([], recoverable["attempts"])
        self._assert_action_runtime_released(recoverable["id"])
        self.assertNotIn(recoverable["id"], manager._manual_controls)

        resumed = await manager.resume(self.owner["id"], recoverable["id"])
        await manager.wait(recoverable["id"])
        resumed = self.service.get_action_campaign(
            self.owner["id"], recoverable["id"]
        )
        self.assertEqual("completed", resumed["status"])
        self.assertEqual("confirmed", resumed["targets"][0]["status"])
        self.assertEqual(
            [(profile_id, "send.after.reconnect")], _ScriptedGreetingWorker.calls
        )
        await manager.shutdown()

    async def test_manual_worker_factory_failure_releases_all_admission_state(self) -> None:
        def fail_worker_factory(_bitbrowser: object) -> object:
            raise RuntimeError("worker constructor failed")

        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=fail_worker_factory,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(RuntimeError, "worker constructor failed"):
            await manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="manual-factory-failure-window",
                target="must.not.start",
                message="Hello",
            )

        stored = self.service.list_action_campaigns(self.owner["id"], limit=10)[0]
        self.assertEqual("failed", stored["status"])
        self.assertEqual("failed", stored["targets"][0]["status"])
        self._assert_action_runtime_released(stored["id"])
        self.assertEqual(set(), manager.active_campaign_ids())
        self.assertEqual(set(), manager._manual_campaign_ids)
        self.assertEqual({}, manager._manual_controls)
        await manager.shutdown()

    async def test_manual_finish_write_failure_fences_unknown_and_resume_refuses(self) -> None:
        profile_id = "finish-write-failure-window"
        _ScriptedGreetingWorker.reset({profile_id: ["confirmed"]})
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )
        original_finish = self.service.finish_action_attempt
        finish_calls = 0

        def fail_first_finish(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise RuntimeError("finish write failed")
            return original_finish(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(
            self.service, "finish_action_attempt", side_effect=fail_first_finish
        ):
            with self.assertRaisesRegex(RuntimeError, "finish write failed"):
                await manager.manual_action(
                    self.owner["id"],
                    operation="greet",
                    profile_id=profile_id,
                    target="possibly.sent",
                    message="Hello",
                )

        stored = self.service.list_action_campaigns(self.owner["id"], limit=10)[0]
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        with self.service.database.read() as connection:
            self.assertEqual(
                (0, 0),
                (
                    connection.execute(
                        "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                        (stored["id"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                        (stored["id"],),
                    ).fetchone()[0],
                ),
            )

        with self.assertRaises(ConflictError):
            await manager.resume(self.owner["id"], stored["id"])
        after_resume = self.service.get_action_campaign(
            self.owner["id"], stored["id"]
        )
        self.assertEqual("paused", after_resume["status"])
        self.assertEqual("unknown", after_resume["targets"][0]["status"])
        self.assertEqual([(profile_id, "possibly.sent")], _ScriptedGreetingWorker.calls)
        self._assert_action_runtime_released(stored["id"])
        await manager.shutdown()

    async def test_shutdown_cancels_manual_pre_send_and_waits_for_cleanup(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="shutdown-manual-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))

        await manager.shutdown()

        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        self.assertNotIn(campaign_id, manager.active_campaign_ids())
        self.assertNotIn(campaign_id, manager._manual_campaign_ids)
        self.assertNotIn(campaign_id, manager._manual_controls)

    async def test_stop_cancels_manual_pre_send_and_never_sends(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="stop-manual-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))

        stopped = await manager.stop(self.owner["id"], campaign_id)
        await asyncio.gather(action_task, return_exceptions=True)

        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        self.assertEqual("stopped", stopped["status"])
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("stopped", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        self.assertNotIn(campaign_id, manager._manual_controls)
        await manager.shutdown()

    async def test_stop_read_failure_still_cancels_manual_pre_send(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="stop-read-failure-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))
        original_get = self.service.get_action_campaign
        read_calls = 0

        def fail_first_read(owner_user_id: str, requested_id: str) -> dict[str, object]:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise RuntimeError("campaign read failed")
            return original_get(owner_user_id, requested_id)

        with patch.object(
            self.service, "get_action_campaign", side_effect=fail_first_read
        ):
            with self.assertRaisesRegex(RuntimeError, "campaign read failed"):
                await manager.stop(self.owner["id"], campaign_id)
            await asyncio.gather(action_task, return_exceptions=True)

        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        await manager.shutdown()

    async def test_pause_read_failure_still_cancels_manual_pre_send(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="pause-read-failure-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))
        original_get = self.service.get_action_campaign
        read_calls = 0

        def fail_first_read(owner_user_id: str, requested_id: str) -> dict[str, object]:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise RuntimeError("campaign read failed")
            return original_get(owner_user_id, requested_id)

        with patch.object(
            self.service, "get_action_campaign", side_effect=fail_first_read
        ):
            with self.assertRaisesRegex(RuntimeError, "campaign read failed"):
                await manager.pause(self.owner["id"], campaign_id)
            await asyncio.gather(action_task, return_exceptions=True)

        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        await manager.shutdown()

    async def test_pause_active_cancels_manual_pre_send_and_never_sends(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="pause-manual-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))

        paused = await manager.pause_active(
            self.owner["id"], profile_id="pause-manual-window", operation="greet"
        )
        await asyncio.gather(action_task, return_exceptions=True)

        self.assertEqual("paused", paused["status"])
        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        await manager.shutdown()

    async def test_pause_active_read_failure_still_cancels_manual_pre_send(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="pause-active-read-failure-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))

        with patch.object(
            self.service,
            "find_active_campaign",
            side_effect=RuntimeError("active campaign read failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "active campaign read failed"):
                await manager.pause_active(
                    self.owner["id"],
                    profile_id="pause-active-read-failure-window",
                    operation="greet",
                )
            await asyncio.gather(action_task, return_exceptions=True)

        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        await manager.shutdown()

    async def test_target_cancel_cancels_manual_pre_send_and_never_sends(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="target-cancel-manual-window",
                target="must.not.send.manual",
                message="Hello",
            )
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))
        running = self.service.get_action_campaign(self.owner["id"], campaign_id)

        controlled = await manager.control_target(
            self.owner["id"],
            campaign_id,
            running["targets"][0]["id"],
            action="cancel",
        )
        await asyncio.gather(action_task, return_exceptions=True)

        self.assertTrue(controlled["deferred"])
        self.assertTrue(action_task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        await manager.shutdown()

    async def test_repeated_stop_repairs_stopped_running_attempt_and_claim(self) -> None:
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = self.service.create_action_campaign(
            self.owner["id"],
            operation="greet",
            execution_type="campaign",
            profile_id="stopped-residual-window",
            targets=["possibly.sent"],
            message="Hello",
            interval_min_seconds=0,
            interval_max_seconds=0,
            limit_count=1,
        )
        self.service.set_campaign_status(self.owner["id"], campaign["id"], "running")
        target = self.service.next_action_target(self.owner["id"], campaign["id"])
        assert target is not None
        self.service.start_action_attempt(
            self.owner["id"], campaign["id"], target["id"]
        )
        self.service.set_campaign_status(self.owner["id"], campaign["id"], "stopped")

        repaired = await manager.stop(self.owner["id"], campaign["id"])

        self.assertEqual("stopped", repaired["status"])
        self.assertEqual("unknown", repaired["targets"][0]["status"])
        self.assertEqual("unknown", repaired["attempts"][0]["status"])
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0],
            )
        await manager.shutdown()

    async def test_stopped_running_greeting_is_fenced_unknown_if_then_cancelled(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="stopped-then-cancelled-window",
            targets=["possibly.sent", "stopped.sibling"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=2,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        stopped = await manager.stop(self.owner["id"], campaign["id"])
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual("unknown", stopped["targets"][0]["status"])

        control = manager._campaigns[campaign["id"]]
        assert control.task is not None
        control.task.cancel()
        await asyncio.gather(control.task, return_exceptions=True)

        stored = self.service.get_action_campaign(
            self.owner["id"], campaign["id"]
        )
        self.assertTrue(control.task.cancelled())
        self.assertEqual("stopped", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self.assertEqual("failed", stored["targets"][1]["status"])
        with self.service.database.read() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM action_success_ledger WHERE campaign_id=?",
                    (campaign["id"],),
                ).fetchone()[0],
            )
        self._assert_action_runtime_released(campaign["id"])
        await manager.shutdown()

    async def test_shutdown_fences_once_then_preserves_task_cancellation(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="shutdown-window",
            targets=["possibly.sent", "shutdown.sibling"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=2,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        await manager.shutdown()

        stored = self.service.get_action_campaign(
            self.owner["id"], campaign["id"]
        )
        self.assertTrue(control.task.cancelled())
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["unknown", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(["unknown"], [item["status"] for item in stored["attempts"]])
        with self.service.database.read() as connection:
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
        self._assert_action_runtime_released(campaign["id"])
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())
        self.assertNotIn(campaign["id"], manager._campaigns)

    async def test_immediate_shutdown_releases_never_scheduled_campaign(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="never-scheduled-window",
            targets=["not.started"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=1,
        )
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        await manager.shutdown()

        self.assertTrue(control.task.cancelled())
        assert _BlockingGreetingWorker.started is not None
        self.assertFalse(_BlockingGreetingWorker.started.is_set())
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual("pending", stored["targets"][0]["status"])
        self.assertEqual([], stored["attempts"])
        self._assert_action_runtime_released(campaign["id"])
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())
        self.assertNotIn(campaign["id"], manager._campaigns)

    async def test_shutdown_fence_error_still_cancels_and_sweeps_every_control(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaigns = []
        for suffix in ("one", "two"):
            campaigns.append(
                await manager.start_campaign(
                    self.owner["id"],
                    operation="greet",
                    profile_id=f"shutdown-fence-error-{suffix}",
                    targets=[f"not.started.{suffix}"],
                    message="Hello",
                    messages=["Hello"],
                    interval="1 秒",
                    limit=1,
                )
            )
            if suffix == "one":
                # Ensure this control has a real in-flight dispatch claim while
                # the second remains unscheduled, independent of thread timing.
                assert _BlockingGreetingWorker.started is not None
                await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        controls = [manager._campaigns[item["id"]] for item in campaigns]

        with patch.object(
            self.service,
            "interrupt_action_campaign",
            side_effect=RuntimeError("database is already closing"),
        ):
            await manager.shutdown()

        self.assertTrue(all(control.task and control.task.cancelled() for control in controls))
        self.assertEqual(set(), manager.active_campaign_ids())
        self.assertEqual({}, manager._campaigns)
        for campaign in campaigns:
            self._assert_action_runtime_released(campaign["id"])
        first = self.service.get_action_campaign(self.owner["id"], campaigns[0]["id"])
        second = self.service.get_action_campaign(self.owner["id"], campaigns[1]["id"])
        self.assertEqual("paused", first["status"])
        self.assertEqual(["unknown"], [item["status"] for item in first["attempts"]])
        self.assertEqual("unknown", first["targets"][0]["status"])
        self.assertEqual("paused", second["status"])
        self.assertEqual([], second["attempts"])
        self.assertEqual("pending", second["targets"][0]["status"])

    async def test_shutdown_fallback_preserves_explicit_stop_and_unknown_result(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service, SimpleNamespace(close_profile=lambda _: {'closed': True}), worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"], operation="greet", profile_id="shutdown-fallback-stopped",
            targets=["possibly.sent", "not.sent"], message="Hello", messages=["Hello"],
            interval="1 秒", limit=2,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        self.service.set_campaign_status(self.owner["id"], campaign["id"], "stopped")
        with patch.object(self.service, "interrupt_action_campaign", side_effect=RuntimeError("campaign fence unavailable")):
            await manager.shutdown()
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("stopped", stored["status"])
        self.assertEqual(["unknown", "failed"], [item["status"] for item in stored["targets"]])
        self.assertEqual(["unknown"], [item["status"] for item in stored["attempts"]])
        self._assert_action_runtime_released(campaign["id"])

    async def test_shutdown_storage_failure_keeps_evidence_for_startup_recovery(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service, SimpleNamespace(close_profile=lambda _: {'closed': True}), worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"], operation="greet", profile_id="shutdown-unwritable",
            targets=["possibly.sent"], message="Hello", messages=["Hello"],
            interval="1 秒", limit=1,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        control = manager._campaigns[campaign["id"]]
        with patch.object(self.service, "interrupt_action_campaign", side_effect=RuntimeError("storage unavailable")), patch.object(self.service, "finish_action_attempt", side_effect=RuntimeError("storage unavailable")):
            await manager.shutdown()
        self.assertTrue(control.task and control.task.cancelled())
        self.assertEqual(set(), manager.active_campaign_ids())
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("running", stored["attempts"][0]["status"])
        with self.service.database.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?", (campaign["id"],)).fetchone()[0])
        self.service.recover_interrupted_operations()
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign["id"])

    async def test_stop_during_pre_send_wait_never_reaches_send_boundary(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="stop-before-send-window",
            targets=["must.not.send"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=1,
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        stopped = await manager.stop(self.owner["id"], campaign["id"])
        await asyncio.gather(control.task, return_exceptions=True)

        self.assertEqual("stopped", stopped["status"])
        self.assertTrue(control.task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("stopped", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign["id"])
        await manager.shutdown()

    async def test_stop_status_write_error_still_prevents_send(self) -> None:
        _PreSendBlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_PreSendBlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="stop-write-error-window",
            targets=["must.not.send"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=1,
        )
        assert _PreSendBlockingGreetingWorker.started is not None
        await asyncio.wait_for(
            _PreSendBlockingGreetingWorker.started.wait(), timeout=2
        )
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        with patch.object(
            self.service,
            "set_campaign_status",
            side_effect=RuntimeError("stop status write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop status write failed"):
                await manager.stop(self.owner["id"], campaign["id"])
        await asyncio.gather(control.task, return_exceptions=True)

        self.assertTrue(control.task.cancelled())
        self.assertEqual([], _PreSendBlockingGreetingWorker.sent)
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self._assert_action_runtime_released(campaign["id"])
        await manager.shutdown()

    async def test_stop_post_trigger_unknown_does_not_reopen_campaign(self) -> None:
        _CancellationToUnknownGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_CancellationToUnknownGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="stop-after-trigger-window",
            targets=["possibly.sent"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=1,
        )
        assert _CancellationToUnknownGreetingWorker.started is not None
        await asyncio.wait_for(
            _CancellationToUnknownGreetingWorker.started.wait(), timeout=2
        )
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        stopped = await manager.stop(self.owner["id"], campaign["id"])
        await asyncio.gather(control.task, return_exceptions=True)

        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("stopped", stored["status"])
        self.assertEqual(stopped["finished_at"], stored["finished_at"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign["id"])
        await manager.shutdown()

    async def test_heartbeat_fence_error_still_cancels_campaign_owner(self) -> None:
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        owner_task = asyncio.create_task(asyncio.Event().wait())
        control = ActionControl(
            owner_user_id=self.owner["id"],
            campaign_id="heartbeat-fence-error",
            profile_id="heartbeat-window",
            lease_token="heartbeat-lease",
            pause_event=asyncio.Event(),
            stop_event=asyncio.Event(),
            task=owner_task,
        )
        control.pause_event.clear()

        async def immediate_sleep(_seconds: float) -> None:
            return None

        async def fail_renew(_profile_id: str, _lease_token: str) -> None:
            raise RuntimeError("lease renewal failed")

        manager._renew_lease_with_retry = fail_renew  # type: ignore[method-assign]
        with (
            patch("app.action_manager.asyncio.sleep", new=immediate_sleep),
            patch.object(
                self.service,
                "interrupt_action_campaign",
                side_effect=RuntimeError("fence write failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fence write failed"):
                await manager._heartbeat(control)

        await asyncio.gather(owner_task, return_exceptions=True)
        self.assertTrue(owner_task.cancelled())
        self.assertTrue(control.stop_event.is_set())
        self.assertTrue(control.pause_event.is_set())
        await manager.shutdown()

    async def test_persistent_heartbeat_storage_failure_stops_browser_and_restart_fences_unknown(self) -> None:
        _BlockingGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="persistent-heartbeat-failure-window",
            targets=["possibly.sent"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=1,
        )
        assert _BlockingGreetingWorker.started is not None
        await asyncio.wait_for(_BlockingGreetingWorker.started.wait(), timeout=2)
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None

        async def immediate_sleep(_seconds: float) -> None:
            return None

        async def fail_renew(_profile_id: str, _lease_token: str) -> None:
            raise RuntimeError("lease renewal failed")

        manager._renew_lease_with_retry = fail_renew  # type: ignore[method-assign]
        with (
            patch("app.action_manager.asyncio.sleep", new=immediate_sleep),
            patch.object(
                self.service,
                "interrupt_action_campaign",
                side_effect=RuntimeError("storage remains unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "storage remains unavailable"):
                await manager._heartbeat(control)
            await asyncio.gather(control.task, return_exceptions=True)

        self.assertTrue(control.task.cancelled())
        self.assertTrue(control.stop_event.is_set())
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())
        with self.service.database.read() as connection:
            self.assertEqual(
                ("running", "running", "running", 1, 0),
                (
                    connection.execute(
                        "SELECT status FROM action_campaigns WHERE id=?",
                        (campaign["id"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT status FROM action_targets WHERE campaign_id=?",
                        (campaign["id"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT status FROM action_attempts WHERE campaign_id=?",
                        (campaign["id"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                        (campaign["id"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM browser_operation_leases WHERE entity_id=?",
                        (campaign["id"],),
                    ).fetchone()[0],
                ),
            )

        # Persistent storage failure is fail-closed in-process: the browser task and
        # lease are gone while the stale claim blocks retries. Normal startup recovery
        # atomically converts that residual action to UNKNOWN before releasing it.
        self.service.recover_interrupted_operations()
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign["id"])
        await manager.shutdown()

    async def test_manual_heartbeat_fence_error_still_cancels_parent(self) -> None:
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingGreetingWorker,
        )
        parent_task = asyncio.create_task(asyncio.Event().wait())
        stop_event = asyncio.Event()

        async def immediate_sleep(_seconds: float) -> None:
            return None

        async def fail_renew(_profile_id: str, _lease_token: str) -> None:
            raise RuntimeError("lease renewal failed")

        manager._renew_lease_with_retry = fail_renew  # type: ignore[method-assign]
        with (
            patch("app.action_manager.asyncio.sleep", new=immediate_sleep),
            patch.object(
                self.service,
                "interrupt_action_campaign",
                side_effect=RuntimeError("manual fence write failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "manual fence write failed"):
                await manager._manual_heartbeat(
                    self.owner["id"],
                    "manual-heartbeat-fence-error",
                    "manual-heartbeat-window",
                    "manual-heartbeat-lease",
                    stop_event,
                    parent_task,
                )

        await asyncio.gather(parent_task, return_exceptions=True)
        self.assertTrue(parent_task.cancelled())
        self.assertTrue(stop_event.is_set())
        await manager.shutdown()

    async def test_repeated_campaign_cancellation_waits_for_owned_cleanup(self) -> None:
        _BlockingCleanupGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingCleanupGreetingWorker,
        )
        campaign = await manager.start_campaign(
            self.owner["id"],
            operation="greet",
            profile_id="double-cancel-campaign-window",
            targets=["possibly.sent", "still.pending"],
            message="Hello",
            messages=["Hello"],
            interval="1 秒",
            limit=2,
        )
        assert _BlockingCleanupGreetingWorker.started is not None
        await asyncio.wait_for(
            _BlockingCleanupGreetingWorker.started.wait(), timeout=2
        )
        control = manager._campaigns[campaign["id"]]
        assert control.task is not None
        control.task.cancel()
        # Repeat immediately, before the coordinator has a chance to enter its
        # cleanup coroutine. Cleanup must still start and complete before the task
        # exposes its final CancelledError.
        control.task.cancel()
        assert _BlockingCleanupGreetingWorker.disconnect_started is not None
        await asyncio.wait_for(
            _BlockingCleanupGreetingWorker.disconnect_started.wait(), timeout=2
        )
        assert _BlockingCleanupGreetingWorker.disconnect_release is not None
        _BlockingCleanupGreetingWorker.disconnect_release.set()
        await asyncio.gather(control.task, return_exceptions=True)

        self.assertTrue(control.task.cancelled())
        stored = self.service.get_action_campaign(self.owner["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["unknown", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self._assert_action_runtime_released(campaign["id"])
        self.assertNotIn(campaign["id"], manager.active_campaign_ids())
        self.assertNotIn(campaign["id"], manager._campaigns)
        await manager.shutdown()

    async def test_repeated_manual_cancellation_waits_for_owned_cleanup(self) -> None:
        _BlockingCleanupGreetingWorker.reset()
        manager = _NoDelayGreetingManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_BlockingCleanupGreetingWorker,
        )
        action_task = asyncio.create_task(
            manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id="double-cancel-manual-window",
                target="possibly.sent.manual",
                message="Hello",
            )
        )
        assert _BlockingCleanupGreetingWorker.started is not None
        await asyncio.wait_for(
            _BlockingCleanupGreetingWorker.started.wait(), timeout=2
        )
        campaign_id = next(iter(manager._manual_campaign_ids))
        action_task.cancel()
        assert _BlockingCleanupGreetingWorker.disconnect_started is not None
        await asyncio.wait_for(
            _BlockingCleanupGreetingWorker.disconnect_started.wait(), timeout=2
        )
        action_task.cancel()
        assert _BlockingCleanupGreetingWorker.disconnect_release is not None
        _BlockingCleanupGreetingWorker.disconnect_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await action_task

        self.assertTrue(action_task.cancelled())
        stored = self.service.get_action_campaign(self.owner["id"], campaign_id)
        self.assertEqual("paused", stored["status"])
        self.assertEqual("unknown", stored["targets"][0]["status"])
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self._assert_action_runtime_released(campaign_id)
        self.assertNotIn(campaign_id, manager.active_campaign_ids())
        self.assertNotIn(campaign_id, manager._manual_campaign_ids)
        await manager.shutdown()

    async def test_manual_non_recipient_failure_resets_window_streak(self) -> None:
        profile_id = "manual-reset-window"
        key = (self.owner["id"], profile_id)
        self.manager._greet_recipient_failure_streaks[key] = 2
        _ScriptedGreetingWorker.reset({profile_id: ["other_failure"]})

        with self.assertRaises(WorkerExecutionError):
            await self.manager.manual_action(
                self.owner["id"],
                operation="greet",
                profile_id=profile_id,
                target="different.failure",
                message="Hello",
            )

        self.assertNotIn(key, self.manager._greet_recipient_failure_streaks)


if __name__ == "__main__":
    unittest.main()
