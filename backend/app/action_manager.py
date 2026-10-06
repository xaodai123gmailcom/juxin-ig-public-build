from __future__ import annotations

import asyncio
import hashlib
import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .bitbrowser_api import BitBrowserClient
from .browser_cleanup import disconnect_worker, close_profile_and_wait
from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .playwright_worker import PlaywrightWorker, WorkerExecutionError
from .service import CoreService


WorkerFactory = Callable[[BitBrowserClient], Any]
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(?:[-–—至]\s*(\d+)\s*)?(?:秒|s|sec|seconds)?\s*$", re.IGNORECASE)
# The desktop UI defaults to an 8-15 second human-paced range, while still
# allowing the operator to choose a different positive interval.  Zero-second
# intervals remain reserved for explicit one-off manual actions.
MIN_CAMPAIGN_INTERVAL_SECONDS = 1
_INTERVAL_RANDOM = random.SystemRandom()

# A follow can fail before Instagram receives a click (for example while opening
# or reading the target profile).  Those failures are safe to skip.  These states,
# however, require the operator to repair the signed-in Instagram window before
# any more targets are attempted.
_FOLLOW_MANUAL_PAUSE_REASONS = frozenset(
    {
        "auth_required",
        "authentication_failed",
        "instagram_login_required",
        "instagram_challenge",
        "instagram_challenge_required",
        "instagram_rate_limited",
        "instagram_action_blocked",
    }
)
_FOLLOW_RECONNECT_REASONS = frozenset(
    {
        "worker_not_connected",
        "worker_reconnect_failed",
        "instagram_network_unavailable",
        "browser_context_missing",
        "cdp_connection_generation_changed",
        "cdp_endpoint_not_ready",
        "cdp_profile_mapping_changed",
        "cdp_profile_verification_failed",
        "action_window_closing",
        "action_window_verification_failed",
        "connection_attempt_invalid",
        "connection_attempt_not_verified",
        "connection_manager_not_accepting",
        "connection_manager_stopping",
        "connection_manager_stopped",
        "connection_manager_timeout",
        "open_cancelled",
        "upstream_unavailable",
    }
)
_FOLLOW_FAILURE_PAUSE_THRESHOLD = 3
_GREET_RECIPIENT_FAILURE_PAUSE_THRESHOLD = 3

# These failures all happen before the greeting send boundary, but they usually
# mean that Instagram's Direct surface no longer matches the selectors used by
# this build.  Advancing through the rest of the queue would therefore turn one
# layout problem into hundreds of known failures in a few seconds.  Fail the
# current target, pause this one window's campaign, and leave every later target
# pending for an explicit operator resume after the window has been inspected.
_GREET_STRUCTURAL_PAUSE_REASONS = frozenset(
    {
        "instagram_direct_inbox_not_rendered",
        "instagram_direct_inbox_search_not_ready",
        "instagram_direct_chat_not_rendered",
        "instagram_direct_recipient_mismatch",
        "instagram_direct_composer_not_ready",
    }
)


def parse_interval(value: str) -> tuple[int, int]:
    match = _INTERVAL_RE.fullmatch(value)
    if not match:
        raise ValidationError("Action interval must look like '8–15 秒'")
    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)
    if maximum < minimum:
        raise ValidationError("Action interval maximum cannot be lower than minimum")
    if minimum < MIN_CAMPAIGN_INTERVAL_SECONDS:
        raise ValidationError(
            f"Automatic action interval must be at least {MIN_CAMPAIGN_INTERVAL_SECONDS} seconds"
        )
    if maximum > 24 * 60 * 60:
        raise ValidationError("Action interval is too large")
    return minimum, maximum


def choose_interval_seconds(minimum: int, maximum: int) -> int:
    """Choose the next delay inside the operator-selected inclusive range."""
    if minimum < MIN_CAMPAIGN_INTERVAL_SECONDS or maximum < minimum:
        raise ValidationError("Invalid automatic action interval")
    return _INTERVAL_RANDOM.randint(minimum, maximum)


def choose_greeting_message(
    campaign_id: str,
    target: str,
    messages: list[str] | None,
    *,
    legacy_message: str | None = None,
) -> str:
    """Choose one campaign greeting with stable per-target randomness.

    A campaign resume must not silently change the text selected for a target.
    Including the random UUID campaign id in the digest makes a new campaign
    reshuffle the library, while retries/resumes of the same campaign remain
    deterministic and auditable.
    """
    choices = [item.strip() for item in (messages or []) if isinstance(item, str) and item.strip()]
    if not choices and legacy_message and legacy_message.strip():
        choices = [legacy_message.strip()]
    if not choices:
        raise ValidationError("At least one greeting message is required")
    target_key = target.strip().lstrip("@").casefold()
    digest = hashlib.sha256(f"{campaign_id}\0{target_key}".encode("utf-8")).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def _greeting_attempt_details(operation: str, selected_message: str | None) -> dict[str, str]:
    if operation == "greet" and selected_message:
        return {"greeting_message": selected_message}
    return {}


@dataclass(slots=True)
class ActionControl:
    owner_user_id: str
    campaign_id: str
    profile_id: str
    lease_token: str
    pause_event: asyncio.Event
    stop_event: asyncio.Event
    operation: str = ""
    task: asyncio.Task[None] | None = None


class ActionCampaignManager:
    def __init__(
        self,
        service: CoreService,
        bitbrowser: BitBrowserClient,
        *,
        worker_factory: WorkerFactory = PlaywrightWorker,
    ) -> None:
        self.service = service
        self.bitbrowser = bitbrowser
        self.worker_factory = worker_factory
        self._campaigns: dict[str, ActionControl] = {}
        self._starting_campaign_ids: set[str] = set()
        self._manual_campaign_ids: set[str] = set()
        self._manual_controls: dict[str, ActionControl] = {}
        # Synchronous profile reconciliation runs in a FastAPI worker thread. Keep
        # one atomic lease-lifetime set instead of reading three structures across a
        # publication handoff where an entity could momentarily disappear.
        self._activity_lock = threading.Lock()
        self._active_lease_campaign_ids: set[str] = set()
        # A BitBrowser window can execute only one leased campaign at a time, so
        # this event-loop-owned map provides a true per-window consecutive streak
        # even when a short campaign ends and another starts on the same window.
        self._follow_failure_streaks: dict[tuple[str, str], int] = {}
        self._greet_recipient_failure_streaks: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()
        # Once shutdown begins this manager instance must never admit new browser
        # work. In particular, a coroutine created immediately before shutdown may
        # not receive its first scheduling turn until after shutdown's control
        # snapshot has already been taken.
        self._closing = False

    def _ensure_accepting_actions(self) -> None:
        if self._closing:
            raise ConflictError("Action manager is shutting down")

    async def start_campaign(
        self,
        owner_user_id: str,
        *,
        operation: str,
        profile_id: str,
        targets: list[str],
        target_sources: dict[str, str] | None = None,
        message: str | None,
        messages: list[str] | None = None,
        interval: str,
        limit: int,
    ) -> dict[str, Any]:
        self._ensure_accepting_actions()
        interval_min, interval_max = parse_interval(interval)
        campaign = self.service.create_action_campaign(
            owner_user_id,
            operation=operation,
            execution_type="campaign",
            profile_id=profile_id,
            targets=targets,
            target_sources=target_sources,
            message=message,
            interval_min_seconds=interval_min,
            interval_max_seconds=interval_max,
            limit_count=limit,
            messages=messages,
        )
        try:
            await self._start_persisted(owner_user_id, campaign["id"])
        except Exception as exc:
            self.service.fail_unstarted_action_campaign(
                owner_user_id, campaign["id"], error=f"Task did not start: {exc}"
            )
            raise
        return self.service.get_action_campaign(owner_user_id, campaign["id"])

    async def manual_action(
        self,
        owner_user_id: str,
        *,
        operation: str,
        profile_id: str,
        target: str,
        source_target: str | None = None,
        message: str | None,
    ) -> dict[str, Any]:
        self._ensure_accepting_actions()
        campaign = self.service.create_action_campaign(
            owner_user_id,
            operation=operation,
            execution_type="manual",
            profile_id=profile_id,
            targets=[target],
            target_sources={target: source_target} if source_target else None,
            message=message,
            interval_min_seconds=0,
            interval_max_seconds=0,
            limit_count=1,
        )
        parent_task = asyncio.current_task()
        if parent_task is None:
            raise RuntimeError("Manual action must run inside an asyncio task")
        try:
            # Worker construction belongs to admission, before any durable browser
            # lease is acquired. A constructor failure must fail the unstarted
            # campaign without publishing an entity that shutdown cannot see.
            worker = self.worker_factory(self.bitbrowser)
        except Exception as exc:
            self.service.fail_unstarted_action_campaign(
                owner_user_id,
                campaign["id"],
                error=f"Manual action worker did not initialize: {exc}",
            )
            raise
        with self._activity_lock:
            self._manual_campaign_ids.add(campaign["id"])
            self._active_lease_campaign_ids.add(campaign["id"])
        lease_token: str | None = None
        try:
            lease_token = await self.service.acquire_browser_lease_async(
                owner_user_id,
                profile_id,
                operation_type="action",
                entity_id=campaign["id"],
            )
            self._ensure_accepting_actions()
        except BaseException as exc:
            try:
                if lease_token is not None:
                    self.service.release_browser_lease(profile_id, lease_token)
            finally:
                with self._activity_lock:
                    self._manual_campaign_ids.discard(campaign["id"])
                    self._active_lease_campaign_ids.discard(campaign["id"])
            self.service.fail_unstarted_action_campaign(
                owner_user_id, campaign["id"], error=f"Manual action did not start: {exc}"
            )
            raise
        assert lease_token is not None
        stop_event = asyncio.Event()
        manual_control = ActionControl(
            owner_user_id=owner_user_id,
            campaign_id=campaign["id"],
            profile_id=profile_id,
            lease_token=lease_token,
            pause_event=asyncio.Event(),
            stop_event=stop_event,
            operation=operation,
            task=parent_task,
        )
        manual_control.pause_event.set()
        with self._activity_lock:
            self._manual_controls[campaign["id"]] = manual_control
        heartbeat: asyncio.Task[None] | None = None
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            self._ensure_accepting_actions()
            heartbeat = asyncio.create_task(
                self._manual_heartbeat(
                    owner_user_id,
                    campaign["id"],
                    profile_id,
                    lease_token,
                    stop_event,
                    parent_task,
                )
            )
            self.service.set_campaign_status(owner_user_id, campaign["id"], "running")
            await worker.connect(profile_id, open_if_needed=True)
            target_row = self.service.next_action_target(owner_user_id, campaign["id"])
            if target_row is None:
                raise ConflictError("Manual action has no pending target")
            selected_message = (
                choose_greeting_message(
                    campaign["id"],
                    target_row["username"],
                    campaign.get("messages"),
                    legacy_message=campaign.get("message"),
                )
                if operation == "greet"
                else None
            )
            attempt_details = _greeting_attempt_details(operation, selected_message)
            from .work_reports import capture_executor
            attempt_details["executor"] = await capture_executor(worker, self.service.database, owner_user_id, profile_id)
            attempt_id = self.service.start_action_attempt(
                owner_user_id,
                campaign["id"],
                target_row["id"],
                details=attempt_details,
            )
            try:
                outcome = await worker.execute_action(
                    operation, target_row["username"], message=selected_message
                )
            except WorkerExecutionError as exc:
                attempt_status = "unknown" if exc.code == "instagram_action_outcome_unknown" else "failed"
                if (
                    operation == "greet"
                    and exc.code != "instagram_direct_inbox_recipient_not_found"
                ):
                    self._reset_greet_recipient_failure_streak(
                        owner_user_id, profile_id
                    )
                self.service.finish_action_attempt(
                    owner_user_id,
                    campaign["id"],
                    attempt_id,
                    status=attempt_status,
                    details={**attempt_details, "reason": exc.code, "message": exc.message},
                )
                self._set_campaign_status_preserving_stop(
                    owner_user_id,
                    campaign["id"],
                    "paused" if attempt_status == "unknown" else "failed",
                    error=exc.message,
                )
                raise
            except Exception as exc:
                if operation == "greet":
                    self._reset_greet_recipient_failure_streak(
                        owner_user_id, profile_id
                    )
                self.service.finish_action_attempt(
                    owner_user_id,
                    campaign["id"],
                    attempt_id,
                    status="unknown",
                    details={
                        **attempt_details,
                        "reason": "unexpected_action_error",
                        "message": str(exc),
                    },
                )
                self._set_campaign_status_preserving_stop(
                    owner_user_id,
                    campaign["id"],
                    "paused",
                    error="Action outcome is unknown; automatic retry disabled",
                )
                raise WorkerExecutionError(
                    "Action outcome is unknown; it will not be retried automatically",
                    reason="instagram_action_outcome_unknown",
                    pause_required=True,
                ) from exc
            self.service.finish_action_attempt(
                owner_user_id,
                campaign["id"],
                attempt_id,
                status=outcome.status,
                details={**attempt_details, "confirmation": outcome.visible_confirmation},
            )
            if operation == "greet":
                self._reset_greet_recipient_failure_streak(owner_user_id, profile_id)
            self._set_campaign_status_preserving_stop(
                owner_user_id, campaign["id"], "completed"
            )
            return {
                "campaign_id": campaign["id"],
                "operation": outcome.operation,
                "target": outcome.target,
                "status": outcome.status,
                "confirmation": outcome.visible_confirmation,
            }
        except asyncio.CancelledError as exc:
            cleanup_cancellation = exc
            if operation == "greet":
                self._reset_greet_recipient_failure_streak(
                    owner_user_id, profile_id
                )
            self._fence_cancelled_campaign(
                owner_user_id,
                campaign["id"],
                error=str(
                    getattr(
                        exc,
                        "action_outcome_message",
                        "Manual action task was cancelled; verify any UNKNOWN "
                        "outcome before retrying",
                    )
                ),
            )
            raise
        except DomainError as exc:
            if operation == "greet":
                self._reset_greet_recipient_failure_streak(
                    owner_user_id, profile_id
                )
            # A connect/validation DomainError is safely recoverable because the
            # target is still pending. A DomainError raised while committing a
            # post-trigger result can instead leave a running attempt, which must
            # follow the same conservative UNKNOWN boundary as any other write
            # failure.
            current = self.service.get_action_campaign(owner_user_id, campaign["id"])
            has_running = any(
                item["status"] == "running"
                for item in (*current["targets"], *current["attempts"])
            )
            if has_running:
                self.service.interrupt_action_campaign(
                    owner_user_id,
                    campaign["id"],
                    error=(
                        "Manual action result could not be persisted after its "
                        f"external boundary: {type(exc).__name__}: {exc}"
                    ),
                )
            elif current["status"] in {"queued", "running"}:
                self._set_campaign_status_preserving_stop(
                    owner_user_id,
                    campaign["id"],
                    "recoverable",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        except Exception as exc:
            if operation == "greet":
                self._reset_greet_recipient_failure_streak(
                    owner_user_id, profile_id
                )
            current = self.service.get_action_campaign(owner_user_id, campaign["id"])
            has_running = any(
                item["status"] == "running"
                for item in (*current["targets"], *current["attempts"])
            )
            if has_running:
                # Once an attempt is running, an exception may be a failed result
                # commit after the external click/send. It is unsafe to call this
                # merely recoverable: fence UNKNOWN and clear its dispatch claim so
                # resume cannot misread the queue as naturally completed.
                self.service.interrupt_action_campaign(
                    owner_user_id,
                    campaign["id"],
                    error=(
                        "Manual action result could not be persisted after its "
                        f"external boundary: {type(exc).__name__}: {exc}"
                    ),
                )
            elif current["status"] in {"queued", "running"}:
                self._set_campaign_status_preserving_stop(
                    owner_user_id,
                    campaign["id"],
                    "recoverable",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        finally:
            stop_event.set()

            async def cleanup_manual_action() -> None:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                try:
                    await disconnect_worker(worker)
                except Exception:
                    pass
                finally:
                    try:
                        current = self.service.get_action_campaign(owner_user_id, campaign['id'])
                        if current['status'] == 'completed':
                            await close_profile_and_wait(self.service, self.bitbrowser, profile_id, lease_token)
                    except Exception:
                        pass
                    finally:
                        self._release_manual_control(manual_control)

            await self._shield_cleanup(
                cleanup_manual_action(), cancellation=cleanup_cancellation
            )

    async def submit_manual_action(
        self,
        owner_user_id: str,
        *,
        operation: str,
        profile_id: str,
        target: str,
        source_target: str | None = None,
        message: str | None,
    ) -> dict[str, Any]:
        """Persist and publish a one-target manual action without awaiting it.

        The desktop bridge has a finite request deadline, whereas browser
        navigation and visible confirmation can legitimately take longer.  The
        workbench therefore receives a durable campaign id as soon as its lease
        and background coordinator are published.  Completion, failure and an
        indeterminate external outcome continue through the ordinary campaign
        snapshot/control/UNKNOWN-resolution protocol.

        The legacy synchronous :meth:`manual_action` remains available for its
        existing endpoint contract.
        """

        self._ensure_accepting_actions()
        campaign = self.service.create_action_campaign(
            owner_user_id,
            operation=operation,
            execution_type="manual",
            profile_id=profile_id,
            targets=[target],
            target_sources={target: source_target} if source_target else None,
            message=message,
            interval_min_seconds=0,
            interval_max_seconds=0,
            limit_count=1,
        )
        try:
            await self._start_persisted(owner_user_id, campaign["id"])
        except Exception as exc:
            self.service.fail_unstarted_action_campaign(
                owner_user_id,
                campaign["id"],
                error=f"Manual action did not start: {exc}",
            )
            raise
        return self.service.get_action_campaign(owner_user_id, campaign["id"])

    async def pause_active(self, owner_user_id: str, *, profile_id: str, operation: str) -> dict[str, Any]:
        # Arm the process-local safety boundary before touching storage. A transient
        # read failure must not leave a legacy manual action free to cross its send
        # boundary after the pause request has already arrived.
        local_controls = [
            control
            for control in (*self._campaigns.values(), *self._manual_controls.values())
            if control.owner_user_id == owner_user_id
            and control.profile_id == profile_id
            and control.operation == operation
        ]
        manual_controls = [
            control
            for control in local_controls
            if self._manual_controls.get(control.campaign_id) is control
        ]
        for control in local_controls:
            control.pause_event.clear()
        for control in manual_controls:
            control.stop_event.set()
            if control.task is not None and not control.task.done():
                control.task.cancel()
        try:
            campaign = self.service.find_active_campaign(
                owner_user_id, profile_id=profile_id, operation=operation
            )
            if campaign is None:
                raise NotFoundError("No active action campaign found")
            if campaign["status"] == "paused":
                return campaign
            return await self.pause(owner_user_id, campaign["id"])
        finally:
            # Keep cancellation idempotent if the durable read/write path failed.
            for control in manual_controls:
                if control.task is not None and not control.task.done():
                    control.task.cancel()

    async def pause(self, owner_user_id: str, campaign_id: str) -> dict[str, Any]:
        campaign_control = self._campaigns.get(campaign_id)
        manual_control = self._manual_controls.get(campaign_id)
        control = campaign_control or manual_control
        if control is not None and control.owner_user_id != owner_user_id:
            control = None
            manual_control = None
        if control is not None:
            control.pause_event.clear()
            if manual_control is control:
                # Legacy manual actions do not have a resumable worker pause gate.
                # Cancel conservatively so a pre-send action cannot cross its side-
                # effect boundary after the UI says it is paused.
                control.stop_event.set()
                if control.task is not None and not control.task.done():
                    control.task.cancel()
        try:
            campaign = self.service.get_action_campaign(owner_user_id, campaign_id)
            if campaign["status"] not in {"queued", "running", "recoverable"}:
                raise ConflictError("Only an active action campaign can be paused")
            return self.service.set_campaign_status(
                owner_user_id, campaign_id, "paused"
            )
        finally:
            if (
                manual_control is control
                and control is not None
                and control.task is not None
                and not control.task.done()
            ):
                control.task.cancel()

    async def stop(self, owner_user_id: str, campaign_id: str) -> dict[str, Any]:
        control = self._campaigns.get(campaign_id) or self._manual_controls.get(
            campaign_id
        )
        if control is not None and control.owner_user_id != owner_user_id:
            control = None
        if control is not None:
            # Stopping must prevent an action that is still navigating/searching or
            # typing from reaching its irreversible click/Enter boundary. Cancelling
            # the coordinator does that at its next browser await; if the boundary
            # was already crossed, the worker marks cancellation as UNKNOWN.
            control.stop_event.set()
            control.pause_event.set()
            if control.task is not None and not control.task.done():
                control.task.cancel()
        try:
            campaign = self.service.get_action_campaign(owner_user_id, campaign_id)
            if campaign["status"] in {"completed", "failed", "stopped"}:
                # A prior stop/failure may have committed just before its in-flight
                # attempt was fenced. Retrying Stop is an idempotent in-process
                # repair path for that residual running row/dispatch claim.
                return self.service.interrupt_action_campaign(
                    owner_user_id,
                    campaign_id,
                    error=(
                        "Action was stopped; any unfinished external outcome "
                        "requires manual confirmation"
                    ),
                )
            self.service.set_campaign_status(
                owner_user_id, campaign_id, "stopped"
            )
            return self.service.interrupt_action_campaign(
                owner_user_id,
                campaign_id,
                error=(
                    "Action was stopped; any in-flight external outcome "
                    "requires manual confirmation"
                ),
            )
        finally:
            # A persistence failure cannot grant permission to continue toward an
            # external side effect. Cancellation is the final local stop fence.
            if (
                control
                and control.owner_user_id == owner_user_id
                and control.task is not None
                and not control.task.done()
            ):
                control.task.cancel()

    async def resume(self, owner_user_id: str, campaign_id: str) -> dict[str, Any]:
        self._ensure_accepting_actions()
        campaign = self.service.get_action_campaign(owner_user_id, campaign_id)
        if campaign["status"] not in {"paused", "recoverable", "queued"}:
            raise ConflictError("Only a paused or recoverable action campaign can resume")
        if campaign["operation"] == "follow":
            # An explicit resume means the operator has inspected/repaired the
            # foregrounded window.  Start a fresh three-failure observation run.
            self._reset_follow_failure_streak(owner_user_id, campaign["profile_id"])
        manual_control = self._manual_controls.get(campaign_id)
        if (
            manual_control is not None
            and manual_control.owner_user_id == owner_user_id
            and manual_control.task is not None
            and not manual_control.task.done()
        ):
            raise ConflictError(
                "Manual action is still stopping; wait for cleanup before continuing"
            )
        control = self._campaigns.get(campaign_id)
        if control and control.owner_user_id == owner_user_id and control.task and not control.task.done():
            control.pause_event.set()
            return self.service.set_campaign_status(owner_user_id, campaign_id, "running")
        await self._start_persisted(owner_user_id, campaign_id)
        return self.service.get_action_campaign(owner_user_id, campaign_id)

    async def control_target(
        self,
        owner_user_id: str,
        campaign_id: str,
        target_id: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Control one target and restart only when a resumed target needs it."""

        manual_control = self._manual_controls.get(campaign_id)
        if (
            action == "resume"
            and manual_control is not None
            and manual_control.owner_user_id == owner_user_id
            and manual_control.task is not None
            and not manual_control.task.done()
        ):
            raise ConflictError(
                "Manual action is still stopping; wait for cleanup before continuing"
            )
        if action == "resume":
            self._ensure_accepting_actions()
        if (
            action in {"pause", "cancel"}
            and manual_control is not None
            and manual_control.owner_user_id == owner_user_id
        ):
            manual_control.stop_event.set()
            manual_control.pause_event.clear()
            try:
                result = self.service.control_action_target(
                    owner_user_id,
                    campaign_id,
                    target_id,
                    action=action,
                )
            finally:
                if (
                    manual_control.task is not None
                    and not manual_control.task.done()
                ):
                    manual_control.task.cancel()
        else:
            result = self.service.control_action_target(
                owner_user_id,
                campaign_id,
                target_id,
                action=action,
            )
        if action != "resume" or result["status"] != "pending":
            return result
        control = self._campaigns.get(campaign_id)
        if (
            control is not None
            and control.owner_user_id == owner_user_id
            and control.task is not None
            and not control.task.done()
        ):
            control.pause_event.set()
            campaign = self.service.get_action_campaign(owner_user_id, campaign_id)
            if campaign["status"] == "paused":
                self.service.set_campaign_status(owner_user_id, campaign_id, "running")
            return result
        await self._start_persisted(owner_user_id, campaign_id)
        return result

    async def wait(self, campaign_id: str) -> None:
        control = self._campaigns.get(campaign_id) or self._manual_controls.get(
            campaign_id
        )
        if control and control.task:
            await asyncio.shield(control.task)

    def active_campaign_ids(self) -> set[str]:
        """Return the atomic set of entities that may still own action leases."""
        with self._activity_lock:
            return self._active_lease_campaign_ids.copy()

    async def shutdown(self) -> None:
        # Set the admission fence synchronously, before the first await and before
        # taking any registry snapshot. Coroutines that have been created but have
        # not run yet will observe this flag on their first instruction.
        self._closing = True
        campaign_controls = list(self._campaigns.values())
        manual_controls = list(self._manual_controls.values())
        controls = [*campaign_controls, *manual_controls]
        for control in controls:
            control.stop_event.set()
            control.pause_event.set()
            try:
                self.service.interrupt_action_campaign(
                    control.owner_user_id,
                    control.campaign_id,
                    error="Application shutdown interrupted the action; verify UNKNOWN outcomes manually",
                )
            except Exception:
                # The campaign-wide fence can fail independently of per-attempt
                # persistence. Keep possible sends UNKNOWN while retiring their
                # dispatch claims; merely releasing the browser leaves them stuck.
                # If storage itself is unavailable, startup recovery must finish
                # this work from the retained running rows, never retry the send.
                try:
                    self._fence_shutdown_attempts(control)
                except Exception:
                    pass
            finally:
                if control.task:
                    control.task.cancel()
        await asyncio.gather(
            *(control.task for control in controls if control.task),
            return_exceptions=True,
        )
        # A task cancelled before its coroutine receives its first scheduling turn
        # never enters ``_run`` and therefore cannot execute that coroutine's
        # ``finally``. Sweep every captured control after gather so its lease and
        # registry publication cannot survive shutdown.
        for control in campaign_controls:
            try:
                self._release_action_control(control)
            except Exception:
                pass
        for control in manual_controls:
            try:
                self._release_manual_control(control)
            except Exception:
                pass

    def _fence_shutdown_attempts(self, control: ActionControl) -> None:
        """Best-effort fallback when the campaign-wide shutdown fence fails."""

        current = self.service.get_action_campaign(
            control.owner_user_id, control.campaign_id
        )
        error = "Application shutdown interrupted the action; verify UNKNOWN outcomes manually"
        for attempt in current.get("attempts", ()):
            if attempt["status"] not in {"running", "unknown"}:
                continue
            try:
                self.service.finish_action_attempt(
                    control.owner_user_id,
                    control.campaign_id,
                    attempt["id"],
                    status="unknown",
                    details={"reason": "execution_interrupted", "message": error},
                )
            except ConflictError:
                # A concurrent final result is authoritative; never overwrite it.
                continue
        current = self.service.get_action_campaign(
            control.owner_user_id, control.campaign_id
        )
        if current["status"] not in {"paused", "stopped"} and (
            current["status"] in {"queued", "running", "recoverable"}
            or any(target["status"] == "unknown" for target in current["targets"])
        ):
            self._set_campaign_status_preserving_stop(
                control.owner_user_id, control.campaign_id, "paused", error=error
            )

    async def _start_persisted(self, owner_user_id: str, campaign_id: str) -> None:
        async with self._lock:
            self._ensure_accepting_actions()
            campaign = self.service.get_action_campaign(owner_user_id, campaign_id)
            active = self._campaigns.get(campaign_id)
            if active and active.task and not active.task.done():
                raise ConflictError("Action campaign is already running")
            has_running = any(
                item["status"] == "running"
                for item in (*campaign["targets"], *campaign["attempts"])
            )
            if has_running:
                # No live coordinator owns this row, so a prior post-trigger
                # persistence failure or process interruption left an indeterminate
                # side effect. Fence it UNKNOWN before any resume can acquire a new
                # lease, and never reinterpret it as an empty/completed queue.
                repaired = self.service.interrupt_action_campaign(
                    owner_user_id,
                    campaign_id,
                    error=(
                        "Unowned running action was fenced during resume; verify the "
                        "UNKNOWN outcome before retrying"
                    ),
                )
                operation_label = (
                    "打招呼" if repaired["operation"] == "greet" else "关注"
                )
                raise ConflictError(
                    f"{operation_label}存在执行中断后的未知结果；请先人工确认"
                )
            has_unknown = any(target["status"] == "unknown" for target in campaign["targets"])
            has_pending = any(target["status"] == "pending" for target in campaign["targets"])
            if has_unknown and not has_pending:
                operation_label = "打招呼" if campaign["operation"] == "greet" else "关注"
                raise ConflictError(
                    f"{operation_label}结果存在待人工确认项，且没有剩余目标可继续；"
                    "请保持暂停并人工核对"
                )
            lease_token: str | None = None
            with self._activity_lock:
                self._starting_campaign_ids.add(campaign_id)
                self._active_lease_campaign_ids.add(campaign_id)
            published = False
            try:
                lease_token = await self.service.acquire_browser_lease_async(
                    owner_user_id,
                    campaign["profile_id"],
                    operation_type="action",
                    entity_id=campaign_id,
                )
                self._ensure_accepting_actions()
                pause_event = asyncio.Event()
                pause_event.set()
                control = ActionControl(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    profile_id=campaign["profile_id"],
                    lease_token=lease_token,
                    pause_event=pause_event,
                    stop_event=asyncio.Event(),
                    operation=campaign["operation"],
                )
                self.service.set_campaign_status(owner_user_id, campaign_id, "running")
                control.task = asyncio.create_task(
                    self._run(control), name=f"action-campaign:{campaign_id}"
                )
                control.task.add_done_callback(
                    lambda _task, finished_control=control: (
                        self._release_finished_action_control(finished_control)
                    )
                )
                with self._activity_lock:
                    self._campaigns[campaign_id] = control
                    self._starting_campaign_ids.discard(campaign_id)
                    published = True
            except BaseException:
                if lease_token is not None:
                    self.service.release_browser_lease(campaign["profile_id"], lease_token)
                with self._activity_lock:
                    self._active_lease_campaign_ids.discard(campaign_id)
                raise
            finally:
                if not published:
                    with self._activity_lock:
                        self._starting_campaign_ids.discard(campaign_id)

    async def _run(self, control: ActionControl) -> None:
        worker: Any | None = None
        heartbeat: asyncio.Task[None] | None = None
        pending_connection_error: DomainError | None = None
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            # Initialization belongs to the persisted campaign's lifecycle too.
            # If it fails, retire pending targets and the running status before
            # releasing the lease; the done callback alone cannot repair storage.
            worker = self.worker_factory(self.bitbrowser)
            heartbeat = asyncio.create_task(
                self._heartbeat(control),
                name=f"action-heartbeat:{control.campaign_id}",
            )
            try:
                await worker.connect(control.profile_id, open_if_needed=True)
            except Exception as exc:
                initial = self.service.get_action_campaign(
                    control.owner_user_id, control.campaign_id
                )
                if initial["operation"] == "greet":
                    self._reset_greet_recipient_failure_streak(
                        control.owner_user_id, control.profile_id
                    )
                    await self._bring_profile_to_front(worker, control.profile_id)
                    control.pause_event.clear()
                    detail = getattr(exc, "message", None) or str(exc)
                    self.service.set_campaign_status(
                        control.owner_user_id,
                        control.campaign_id,
                        "paused",
                        error=(
                            "打招呼窗口连接失败，尚未开始任何账号；"
                            f"已保留全部待执行账号：{detail}"
                        ),
                    )
                    return
                if not isinstance(exc, DomainError):
                    raise
                # No Instagram action can have fired before connect succeeds.  Count
                # the failure against the next follow target instead of failing the
                # whole queue at its first item.
                pending_connection_error = exc
            while not control.stop_event.is_set():
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    return
                campaign = self.service.get_action_campaign(control.owner_user_id, control.campaign_id)
                target = self.service.next_action_target(control.owner_user_id, control.campaign_id)
                if target is None:
                    latest = self.service.get_action_campaign(
                        control.owner_user_id, control.campaign_id
                    )
                    if any(
                        item["status"] == "running"
                        for item in (*latest["targets"], *latest["attempts"])
                    ):
                        # ``next_action_target`` returning no work while a row remains
                        # running is never successful natural completion. It is a
                        # stale/indeterminate dispatch and must be fenced UNKNOWN.
                        self.service.interrupt_action_campaign(
                            control.owner_user_id,
                            control.campaign_id,
                            error=(
                                "Action dispatch lost its completion record; verify "
                                "the UNKNOWN outcome before retrying"
                            ),
                        )
                    elif any(item["status"] == "unknown" for item in latest["targets"]):
                        self.service.set_campaign_status(
                            control.owner_user_id,
                            control.campaign_id,
                            "paused",
                            error=(
                                f"剩余目标已完成，但存在"
                                f"{'打招呼' if latest['operation'] == 'greet' else '关注'}"
                                "结果待人工确认；未知项未自动重试"
                            ),
                        )
                    elif any(
                        item["status"] in {"paused", "pending"}
                        for item in latest["targets"]
                    ):
                        self.service.set_campaign_status(
                            control.owner_user_id,
                            control.campaign_id,
                            "paused",
                            error="剩余目标已单独暂停或正在其他窗口执行",
                        )
                    else:
                        self.service.set_campaign_status(
                            control.owner_user_id, control.campaign_id, "completed"
                        )
                    return
                selected_message = (
                    choose_greeting_message(
                        campaign["id"],
                        target["username"],
                        campaign.get("messages"),
                        legacy_message=campaign.get("message"),
                    )
                    if campaign["operation"] == "greet"
                    else None
                )
                attempt_details = _greeting_attempt_details(
                    campaign["operation"], selected_message
                )
                from .work_reports import capture_executor
                attempt_details["executor"] = await capture_executor(worker, self.service.database, control.owner_user_id, control.profile_id)
                try:
                    attempt_id = self.service.start_action_attempt(
                        control.owner_user_id,
                        control.campaign_id,
                        target["id"],
                        details=attempt_details,
                    )
                except ConflictError as exc:
                    status = exc.details.get("status")
                    if status in {"paused", "already_done", "dispatching"}:
                        if status == "dispatching":
                            await asyncio.sleep(1)
                        continue
                    raise
                try:
                    if pending_connection_error is not None:
                        connection_error = pending_connection_error
                        pending_connection_error = None
                        raise connection_error
                    outcome = await worker.execute_action(
                        campaign["operation"], target["username"], message=selected_message
                    )
                except WorkerExecutionError as exc:
                    status = "unknown" if exc.code == "instagram_action_outcome_unknown" else "failed"
                    self.service.finish_action_attempt(
                        control.owner_user_id,
                        control.campaign_id,
                        attempt_id,
                        status=status,
                        details={**attempt_details, "reason": exc.code, "message": exc.message},
                    )
                    if campaign["operation"] == "follow":
                        should_continue = await self._handle_follow_failure(
                            control,
                            worker,
                            reason=exc.code,
                            message=exc.message,
                            outcome_unknown=status == "unknown",
                        )
                        if should_continue:
                            pending_connection_error = await self._reconnect_follow_worker(
                                worker, control.profile_id
                            ) if (
                                exc.code in _FOLLOW_RECONNECT_REASONS
                                or bool(exc.details.get("pause_required", True))
                            ) else None
                            await self._wait_for_next_action_target(control, campaign)
                            continue
                        return
                    if exc.code == "instagram_direct_inbox_recipient_not_found":
                        streak = self._greet_recipient_failure_streak(
                            control.owner_user_id,
                            control.profile_id,
                        )
                        if streak >= _GREET_RECIPIENT_FAILURE_PAUSE_THRESHOLD:
                            await self._bring_profile_to_front(worker, control.profile_id)
                            control.pause_event.clear()
                            self._set_campaign_status_preserving_stop(
                                control.owner_user_id,
                                control.campaign_id,
                                "paused",
                                error=(
                                    f"同一窗口连续 {streak} 个账号未出现精确搜索结果，"
                                    "可能是 Direct 页面结构已变化；已暂停并保留剩余账号。"
                                ),
                            )
                            return
                        await self._wait_for_next_action_target(control, campaign)
                        continue
                    # The fuse is deliberately consecutive: any different
                    # greeting result proves that this window did not just
                    # produce another recipient-search miss.
                    self._reset_greet_recipient_failure_streak(
                        control.owner_user_id, control.profile_id
                    )
                    if exc.code in _GREET_STRUCTURAL_PAUSE_REASONS:
                        await self._bring_profile_to_front(worker, control.profile_id)
                        control.pause_event.clear()
                        self._set_campaign_status_preserving_stop(
                            control.owner_user_id,
                            control.campaign_id,
                            "paused",
                            error=(
                                f"Direct 页面结构异常，已暂停该窗口以保护剩余账号："
                                f"{exc.message}"
                            ),
                        )
                        return
                    if status == "unknown" or exc.details.get("pause_required", True):
                        control.pause_event.clear()
                        self._set_campaign_status_preserving_stop(
                            control.owner_user_id, control.campaign_id, "paused", error=exc.message
                        )
                        return
                    await self._wait_for_next_action_target(control, campaign)
                    continue
                except DomainError as exc:
                    # For follows, a DomainError raised after connect but before the
                    # worker's click boundary is a confirmed no-action failure.  The
                    # worker converts every post-click uncertainty into
                    # instagram_action_outcome_unknown, so this branch is safe to
                    # record as failed and advance.
                    if campaign["operation"] != "follow":
                        self._reset_greet_recipient_failure_streak(
                            control.owner_user_id, control.profile_id
                        )
                        raise
                    reason = str(exc.details.get("reason") or exc.code)
                    self.service.finish_action_attempt(
                        control.owner_user_id,
                        control.campaign_id,
                        attempt_id,
                        status="failed",
                        details={
                            **attempt_details,
                            "reason": reason,
                            "message": exc.message,
                        },
                    )
                    should_continue = await self._handle_follow_failure(
                        control,
                        worker,
                        reason=reason,
                        message=exc.message,
                        outcome_unknown=False,
                    )
                    if not should_continue:
                        return
                    pending_connection_error = await self._reconnect_follow_worker(
                        worker, control.profile_id
                    )
                    await self._wait_for_next_action_target(control, campaign)
                    continue
                except Exception as exc:
                    if campaign["operation"] == "greet":
                        self._reset_greet_recipient_failure_streak(
                            control.owner_user_id, control.profile_id
                        )
                    self.service.finish_action_attempt(
                        control.owner_user_id,
                        control.campaign_id,
                        attempt_id,
                        status="unknown",
                        details={
                            **attempt_details,
                            "reason": "unexpected_action_error",
                            "message": str(exc),
                        },
                    )
                    if campaign["operation"] == "follow":
                        await self._bring_profile_to_front(worker, control.profile_id)
                        control.pause_event.clear()
                    self._set_campaign_status_preserving_stop(
                        control.owner_user_id,
                        control.campaign_id,
                        "paused",
                        error="Action outcome is unknown; automatic retry disabled",
                    )
                    return
                self.service.finish_action_attempt(
                    control.owner_user_id,
                    control.campaign_id,
                    attempt_id,
                    status=outcome.status,
                    details={**attempt_details, "confirmation": outcome.visible_confirmation},
                )
                if campaign["operation"] == "follow":
                    self._reset_follow_failure_streak(
                        control.owner_user_id, control.profile_id
                    )
                elif campaign["operation"] == "greet":
                    self._reset_greet_recipient_failure_streak(
                        control.owner_user_id, control.profile_id
                    )
                if self.service.next_action_target(control.owner_user_id, control.campaign_id) is not None:
                    await self._wait_with_pause(
                        control,
                        choose_interval_seconds(
                            campaign["interval_min_seconds"],
                            campaign["interval_max_seconds"],
                        ),
                    )
        except asyncio.CancelledError as exc:
            cleanup_cancellation = exc
            try:
                current = self.service.get_action_campaign(
                    control.owner_user_id, control.campaign_id
                )
                if current["operation"] == "greet":
                    self._reset_greet_recipient_failure_streak(
                        control.owner_user_id, control.profile_id
                    )
                self._fence_cancelled_campaign(
                    control.owner_user_id,
                    control.campaign_id,
                    error=str(
                        getattr(
                            exc,
                            "action_outcome_message",
                            "Action coordinator was cancelled; verify any UNKNOWN "
                            "outcome before retrying",
                        )
                    ),
                )
            except DomainError:
                pass
            raise
        except Exception as exc:
            current = self.service.get_action_campaign(control.owner_user_id, control.campaign_id)
            if current["status"] == "running":
                self.service.interrupt_action_campaign(
                    control.owner_user_id,
                    control.campaign_id,
                    error=f"{type(exc).__name__}: {exc}",
                    final_status="failed",
                )
        finally:
            control.stop_event.set()

            async def cleanup_campaign() -> None:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                try:
                    if worker is not None:
                        await disconnect_worker(worker)
                except Exception:
                    pass
                finally:
                    try:
                        current = self.service.get_action_campaign(control.owner_user_id, control.campaign_id)
                        if current['status'] == 'completed':
                            await close_profile_and_wait(self.service, self.bitbrowser, control.profile_id, control.lease_token)
                    except Exception:
                        # A close failure must not turn a completed external action
                        # into a duplicate retry or mask the saved result.
                        pass
                    finally:
                        self._release_action_control(control)

            await self._shield_cleanup(
                cleanup_campaign(), cancellation=cleanup_cancellation
            )

    def _follow_failure_streak(self, owner_user_id: str, profile_id: str) -> int:
        key = (owner_user_id, profile_id)
        streak = self._follow_failure_streaks.get(key, 0) + 1
        self._follow_failure_streaks[key] = streak
        return streak

    def _reset_follow_failure_streak(self, owner_user_id: str, profile_id: str) -> None:
        self._follow_failure_streaks.pop((owner_user_id, profile_id), None)

    def _greet_recipient_failure_streak(
        self, owner_user_id: str, profile_id: str
    ) -> int:
        key = (owner_user_id, profile_id)
        streak = self._greet_recipient_failure_streaks.get(key, 0) + 1
        self._greet_recipient_failure_streaks[key] = streak
        return streak

    def _reset_greet_recipient_failure_streak(
        self, owner_user_id: str, profile_id: str
    ) -> None:
        self._greet_recipient_failure_streaks.pop((owner_user_id, profile_id), None)

    def _set_campaign_status_preserving_stop(
        self,
        owner_user_id: str,
        campaign_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Publish worker progress without undoing an explicit user stop."""

        current = self.service.get_action_campaign(owner_user_id, campaign_id)
        if current["status"] == "stopped":
            return current
        return self.service.set_campaign_status(
            owner_user_id, campaign_id, status, error=error
        )

    def _release_action_control(self, control: ActionControl) -> None:
        """Idempotently release one control without removing a newer replacement."""

        try:
            self.service.release_browser_lease(
                control.profile_id, control.lease_token
            )
        finally:
            with self._activity_lock:
                current = self._campaigns.get(control.campaign_id)
                if current is control:
                    self._campaigns.pop(control.campaign_id, None)
                    self._active_lease_campaign_ids.discard(control.campaign_id)
                elif current is None:
                    self._active_lease_campaign_ids.discard(control.campaign_id)

    def _release_finished_action_control(self, control: ActionControl) -> None:
        """Best-effort done callback for tasks cancelled before ``_run`` starts."""

        try:
            self._release_action_control(control)
        except Exception:
            # Shutdown performs the same idempotent sweep. If storage itself is
            # unavailable, lease reconciliation will see the entity as inactive.
            pass

    def _release_manual_control(self, control: ActionControl) -> None:
        """Idempotently release one synchronous manual-action registration."""

        try:
            self.service.release_browser_lease(
                control.profile_id, control.lease_token
            )
        finally:
            with self._activity_lock:
                current = self._manual_controls.get(control.campaign_id)
                if current is control:
                    self._manual_controls.pop(control.campaign_id, None)
                    self._manual_campaign_ids.discard(control.campaign_id)
                    self._active_lease_campaign_ids.discard(control.campaign_id)
                elif current is None:
                    self._manual_campaign_ids.discard(control.campaign_id)
                    self._active_lease_campaign_ids.discard(control.campaign_id)

    def _fence_cancelled_campaign(
        self,
        owner_user_id: str,
        campaign_id: str,
        *,
        error: str,
    ) -> dict[str, Any]:
        """Persist at most one cancellation fence without weakening UNKNOWN.

        Heartbeat/shutdown paths fence before cancelling their task.  A second
        interrupt would only overwrite the useful first error and create duplicate
        audit events.  Conversely, a user may have marked a campaign ``stopped``
        while one attempt was still confirming; that in-flight row still requires
        an UNKNOWN fence before the task can exit.
        """

        current = self.service.get_action_campaign(owner_user_id, campaign_id)
        has_running = any(
            item.get("status") == "running"
            for item in (*current.get("targets", ()), *current.get("attempts", ()))
        )
        has_unknown = any(
            item.get("status") == "unknown"
            for item in (*current.get("targets", ()), *current.get("attempts", ()))
        )
        if not has_running:
            if has_unknown or current.get("status") not in {
                "queued",
                "running",
                "recoverable",
            }:
                return current
        return self.service.interrupt_action_campaign(
            owner_user_id,
            campaign_id,
            error=error,
        )

    @staticmethod
    async def _shield_cleanup(
        cleanup: Any,
        *,
        cancellation: asyncio.CancelledError | None = None,
    ) -> None:
        """Finish lease/registry cleanup even if cancellation is repeated.

        ``asyncio.shield`` protects the child cleanup task but still raises into
        its caller. Keep waiting for the child after every outer cancellation,
        then propagate cancellation only after all owned resources are released.
        """

        owner_task = asyncio.current_task()
        # The first CancelledError is already being propagated through the outer
        # ``finally``. Drain its outstanding cancellation requests before our first
        # await so an immediately repeated ``cancel()`` cannot prevent the child
        # cleanup task from starting. Re-raising the saved exception below preserves
        # the caller-visible cancelled task state.
        if cancellation is not None and owner_task is not None:
            while owner_task.cancelling():
                owner_task.uncancel()

        cleanup_task = asyncio.create_task(cleanup)
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                if owner_task is not None:
                    while owner_task.cancelling():
                        owner_task.uncancel()
                continue
            except BaseException:
                break

        cleanup_error: BaseException | None = None
        try:
            cleanup_task.result()
        except BaseException as exc:
            cleanup_error = exc
        if cancellation is not None:
            if cleanup_error is not None and cleanup_error is not cancellation:
                cancellation.add_note(
                    f"cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise cancellation
        if cleanup_error is not None:
            raise cleanup_error

    async def _handle_follow_failure(
        self,
        control: ActionControl,
        worker: Any,
        *,
        reason: str,
        message: str,
        outcome_unknown: bool,
    ) -> bool:
        """Return True only when the next follow target may be claimed safely."""

        if outcome_unknown or reason == "instagram_action_outcome_unknown":
            await self._bring_profile_to_front(worker, control.profile_id)
            control.pause_event.clear()
            self._set_campaign_status_preserving_stop(
                control.owner_user_id,
                control.campaign_id,
                "paused",
                error=message,
            )
            return False
        if reason in _FOLLOW_MANUAL_PAUSE_REASONS:
            await self._bring_profile_to_front(worker, control.profile_id)
            control.pause_event.clear()
            self._set_campaign_status_preserving_stop(
                control.owner_user_id,
                control.campaign_id,
                "paused",
                error=message,
            )
            return False

        streak = self._follow_failure_streak(
            control.owner_user_id, control.profile_id
        )
        if streak < _FOLLOW_FAILURE_PAUSE_THRESHOLD:
            return True

        await self._bring_profile_to_front(worker, control.profile_id)
        control.pause_event.clear()
        self._set_campaign_status_preserving_stop(
            control.owner_user_id,
            control.campaign_id,
            "paused",
            error=(
                f"同一窗口连续 {streak} 个关注目标失败，已将窗口置前并暂停；"
                f"请人工检查后继续。最后错误：{message}"
            ),
        )
        return False

    async def _reconnect_follow_worker(
        self, worker: Any, profile_id: str
    ) -> DomainError | None:
        """Reconnect after a definite pre-click failure without stopping the queue."""

        try:
            await disconnect_worker(worker)
        except Exception:
            pass
        try:
            await worker.connect(profile_id, open_if_needed=True)
        except DomainError as exc:
            return exc
        except Exception as exc:
            return WorkerExecutionError(
                f"BitBrowser window reconnect failed: {type(exc).__name__}: {exc}",
                reason="worker_reconnect_failed",
                pause_required=True,
                status_code=503,
            )
        return None

    async def _wait_for_next_action_target(
        self, control: ActionControl, campaign: dict[str, Any]
    ) -> None:
        """Apply the configured human-paced interval after an ordinary failure."""

        if self.service.next_action_target(
            control.owner_user_id, control.campaign_id
        ) is None:
            return
        await self._wait_with_pause(
            control,
            choose_interval_seconds(
                campaign["interval_min_seconds"],
                campaign["interval_max_seconds"],
            ),
        )

    async def _bring_profile_to_front(self, worker: Any, profile_id: str) -> None:
        """Best-effort handoff of one failed window to the operator."""

        brought_forward = False
        bring_profile = getattr(self.bitbrowser, "bring_profile_to_front", None)
        if callable(bring_profile):
            try:
                result = await asyncio.to_thread(bring_profile, profile_id)
                if isinstance(result, dict):
                    # A restored window can still remain behind other apps when
                    # Windows rejects SetForegroundWindow. In that case use the
                    # Playwright page activation fallback as well.
                    brought_forward = bool(result.get("foregrounded"))
                else:
                    brought_forward = bool(result)
            except Exception:
                pass
        if brought_forward:
            return
        bring_page = getattr(worker, "bring_window_to_front", None)
        if callable(bring_page):
            try:
                await bring_page()
            except Exception:
                pass

    @staticmethod
    async def _wait_with_pause(control: ActionControl, seconds: int) -> None:
        remaining = float(seconds)
        while remaining > 0 and not control.stop_event.is_set():
            await control.pause_event.wait()
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _heartbeat(self, control: ActionControl) -> None:
        try:
            while not control.stop_event.is_set():
                await asyncio.sleep(20)
                await self._renew_lease_with_retry(control.profile_id, control.lease_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"Browser lease was lost; action outcome may be unknown: {type(exc).__name__}: {exc}"
            try:
                self.service.interrupt_action_campaign(
                    control.owner_user_id,
                    control.campaign_id,
                    error=error,
                )
            finally:
                # Losing the lease means this coordinator no longer owns the
                # browser even when durable fencing itself encounters a storage
                # error. Never let it continue toward another click/send boundary.
                control.stop_event.set()
                control.pause_event.set()
                if control.task and control.task is not asyncio.current_task():
                    control.task.cancel()

    async def _manual_heartbeat(
        self,
        owner_user_id: str,
        campaign_id: str,
        profile_id: str,
        lease_token: str,
        stop_event: asyncio.Event,
        parent_task: asyncio.Task[Any],
    ) -> None:
        try:
            while not stop_event.is_set():
                await asyncio.sleep(20)
                await self._renew_lease_with_retry(profile_id, lease_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self.service.interrupt_action_campaign(
                    owner_user_id,
                    campaign_id,
                    error=f"Browser lease was lost; manual action outcome may be unknown: {type(exc).__name__}: {exc}",
                )
            finally:
                stop_event.set()
                parent_task.cancel()

    async def _renew_lease_with_retry(self, profile_id: str, lease_token: str) -> None:
        """Do not interrupt a live action window for one transient SQLite failure."""

        last_error: Exception | None = None
        for attempt in range(4):
            try:
                await asyncio.to_thread(self.service.renew_browser_lease, profile_id, lease_token)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(0.75 * (attempt + 1))
        assert last_error is not None
        raise last_error
