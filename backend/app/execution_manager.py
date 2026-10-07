from __future__ import annotations

import asyncio
import inspect
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .async_cleanup import finish_owned
from .browser_cleanup import close_acknowledged
from .bitbrowser_api import BitBrowserClient
from .errors import (
    BitBrowserAuthRequiredError,
    ConflictError,
    DomainError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from .openai_review import OpenAIProfileReviewer
from .person_recognition import (
    LocalPersonClassifier,
    PersonClassifier,
    PersonRecognition,
    normalize_recognition,
)
from .playwright_worker import (
    PlaywrightWorker,
    WorkerExecutionError,
    external_profile_link_url,
)
from .service import CoreService
from .social_platform import collection_platform


WorkerFactory = Callable[[BitBrowserClient], Any]
ReviewerFactory = Callable[[], Any]
PersonClassifierFactory = Callable[[], PersonClassifier]
_PERSON_INFERENCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="person-recognition",
)
MALE_AVATAR_EXCLUSION_THRESHOLD = 0.55


class _ManualControlYield(BaseException):
    """Cooperative safe-point exit, deliberately outside page-error catch blocks."""


class _CandidateChangeSignal:
    """Broadcast a queue change without consumers clearing each other's wakeup.

    Subscribe before reading durable state. Each publication permanently sets
    that generation's event, including for wait coroutines scheduled later.
    This is confined to the pipeline's event loop; SQLite remains the queue.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def subscribe(self) -> asyncio.Event:
        return self._event

    def set(self) -> None:
        previous = self._event
        self._event = asyncio.Event()
        previous.set()


def _normalized_known_location(value: Any) -> str | None:
    """Return a visible country value, keeping absent/unknown values unknown."""

    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.casefold() in {
        "unknown",
        "not visible",
        "not_visible",
        "not shown",
        "not provided",
        "not available",
        "unavailable",
        "未知",
        "未显示",
        "未识别",
        "无法确认",
        "不可用",
        "未提供",
        "未公开",
        "未填写",
    }:
        return None
    return normalized


def _is_confirmed_zero_count(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) == 0
    )


def _visible_instagram_user_id(profile: dict[str, Any]) -> str | None:
    """Accept only an explicit stable id exposed by the exact visible profile."""

    if profile.get("platform", "instagram") != "instagram":
        return None
    raw = profile.get("instagram_user_id")
    if isinstance(raw, bool) or raw is None:
        return None
    normalized = str(raw).strip()
    # Instagram ids are numeric.  Refusing generic ``id``/``pk`` strings avoids
    # binding a media id or a recommendation object to the target identity.
    if (not normalized.isascii() or not normalized.isdigit()
            or not normalized.strip("0") or len(normalized) > 100):
        return None
    return normalized


def _is_united_states_location(value: str) -> bool:
    compact = "".join(value.split()).casefold()
    return compact == "美国" or compact.startswith("美国·") or compact in {
        "unitedstates",
        "unitedstatesofamerica",
        "usa",
        "u.s.a.",
        "us",
        "u.s.",
    }


@dataclass(slots=True)
class ExecutionControl:
    owner_user_id: str
    task_id: str
    pause_event: asyncio.Event
    stop_event: asyncio.Event
    leases: dict[str, str]
    target_queue: asyncio.Queue[dict[str, Any]]
    enqueued_target_ids: set[str] = field(default_factory=set)
    deleted_target_ids: set[str] = field(default_factory=set)
    queue_claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    coordinator: asyncio.Task[None] | None = None
    # ``worker_tasks`` contains only live generations.  Completed generations are
    # consumed by a done callback and removed immediately so a task which restarts a
    # window for days cannot retain thousands of Task objects and their tracebacks.
    worker_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    worker_results: asyncio.Queue[
        tuple[str, BaseException | None, bool]
    ] = field(default_factory=asyncio.Queue)
    workers_changed: asyncio.Event = field(default_factory=asyncio.Event)
    started_profile_ids: set[str] = field(default_factory=set)
    fatal_status: str | None = None
    fatal_error: str | None = None
    tearing_down: bool = False
    close_profiles_on_exit: bool = False
    profiles_closed: bool = False
    closing_profile_ids: set[str] = field(default_factory=set)
    profile_close_tasks: dict[tuple[str, str], asyncio.Task[bool]] = field(default_factory=dict)
    network_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    network_state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_network_events: dict[str, asyncio.Event] = field(default_factory=dict)
    network_wait_generations: dict[str, int] = field(default_factory=dict)
    profile_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Exact completed source still attached to a worker's cleanup/close. The
    # public profile state clears current_target_id before provider close ends.
    profile_completed_target_ids: dict[str, str] = field(default_factory=dict)
    cleaned_profile_ids: set[str] = field(default_factory=set)
    network_retry_gate: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(3)
    )
    next_network_probe_at: float = 0.0
    last_progress_at: str | None = None
    last_network_success_at: str | None = None
    person_model_state: str = "not_required"
    person_model_error: str | None = None
    terminal_override: str | None = None
    manually_paused: bool = False
    # Shared wakeup for in-memory targets, durable split arrivals and intake locks.
    # Windows with protected unfinished work sleep here instead of polling SQLite.
    # Fully drained windows close and release their leases.
    work_available: asyncio.Event = field(default_factory=asyncio.Event)
    # A wake arriving during an off-loop read invalidates its empty-queue result.
    work_generation: int = 0
    # Each worker gets its own wake event.  A window that cannot claim a target
    # because of hard affinity may clear only its own signal; it must never put
    # the matching sibling window to sleep by clearing the shared notification.
    profile_work_events: dict[str, asyncio.Event] = field(default_factory=dict)
    # Per-window controls are intentionally separate from the task-wide events.
    # Row actions must never mutate ``pause_event``/``stop_event`` because those
    # are reserved for the explicit "all windows" controls.
    profile_pause_events: dict[str, asyncio.Event] = field(default_factory=dict)
    profile_stop_events: dict[str, asyncio.Event] = field(default_factory=dict)
    profile_worker_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    manual_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    manual_events: dict[str, asyncio.Event] = field(default_factory=dict)
    manual_checkpoints: dict[str, Callable[[], Any]] = field(default_factory=dict)
    manual_mode_active: set[str] = field(default_factory=set)
    source_recheck_profiles: set[str] = field(default_factory=set)
    source_recheck_target_id: str | None = None
    source_recheck_cleanup_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    profile_workers: dict[str, Any] = field(default_factory=dict)
    # Source-only admission owned by the currently running parallel pipeline.
    # Entries disappear before mode/target completion, never recreate a worker.
    live_source_rechecks: dict[str, dict[str, Any]] = field(default_factory=dict)


class _CombinedPauseEvent:
    """Event-like gate that opens only when task and owning window are resumed."""

    def __init__(self, global_event: asyncio.Event, local_event: asyncio.Event,
                 manual_event: asyncio.Event | None = None, manual_checkpoint=None) -> None:
        self._global = global_event
        self._local = local_event
        self._manual_event = manual_event
        self._manual_checkpoint = manual_checkpoint

    def is_set(self) -> bool:
        return self._global.is_set() and self._local.is_set()

    def set(self) -> None:
        self._local.set()

    def clear(self) -> None:
        self._local.clear()

    async def wait(self) -> bool:
        while True:
            if self._manual_event is not None and self._manual_event.is_set():
                await self._manual_checkpoint()
            if self.is_set():
                return True
            if self._manual_event is None:
                await self._global.wait()
                await self._local.wait()
                continue
            gate = self._global if not self._global.is_set() else self._local
            waits = [asyncio.create_task(gate.wait()),
                     asyncio.create_task(self._manual_event.wait())]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)


class _CombinedStopEvent:
    """Event-like signal raised by either the whole task or one window."""

    def __init__(self, global_event: asyncio.Event, local_event: asyncio.Event) -> None:
        self._global = global_event
        self._local = local_event

    def is_set(self) -> bool:
        return self._global.is_set() or self._local.is_set()

    def set(self) -> None:
        self._local.set()

    def clear(self) -> None:
        self._local.clear()

    async def wait(self) -> bool:
        if self.is_set():
            return True
        global_wait = asyncio.create_task(self._global.wait())
        local_wait = asyncio.create_task(self._local.wait())
        waits = {global_wait, local_wait}
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            return bool(done)
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)


class _ProfileWorkEvent:
    """Event view whose clear is local and whose set wakes every sibling."""

    def __init__(
        self,
        shared: ExecutionControl,
        local_event: asyncio.Event,
    ) -> None:
        self._shared = shared
        self._local = local_event

    def is_set(self) -> bool:
        return self._local.is_set()

    def set(self) -> None:
        self._shared.work_generation += 1
        self._shared.work_available.set()
        for event in tuple(self._shared.profile_work_events.values()):
            event.set()

    def clear(self) -> None:
        self._local.clear()
        if not any(
            event.is_set()
            for event in tuple(self._shared.profile_work_events.values())
        ):
            self._shared.work_available.clear()

    async def wait(self) -> bool:
        return await self._local.wait()


class _WindowControlView:
    """Delegate shared state while replacing only pause/stop with window gates."""

    __slots__ = (
        "_shared",
        "pause_event",
        "stop_event",
        "work_available",
        "profile_id",
    )

    def __init__(
        self,
        shared: ExecutionControl,
        profile_id: str,
        local_pause: asyncio.Event,
        local_stop: asyncio.Event,
    ) -> None:
        object.__setattr__(self, "_shared", shared)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(
            self, "pause_event", _CombinedPauseEvent(shared.pause_event, local_pause,
                shared.manual_events.get(profile_id), shared.manual_checkpoints.get(profile_id))
        )
        object.__setattr__(
            self, "stop_event", _CombinedStopEvent(shared.stop_event, local_stop)
        )
        local_work = shared.profile_work_events[profile_id]
        object.__setattr__(
            self, "work_available", _ProfileWorkEvent(shared, local_work)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._shared, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._shared, name, value)


class ExecutionManager:
    """Runs one real PlaywrightWorker per selected BitBrowser profile.

    Targets are claimed from one asyncio queue, so each target belongs to exactly one
    window at a time. Completed mode checkpoints are skipped when a recoverable task is
    restarted. The database remains the durable source of truth.
    """

    def __init__(
        self,
        service: CoreService,
        bitbrowser: BitBrowserClient,
        *,
        worker_factory: WorkerFactory = PlaywrightWorker,
        reviewer_factory: ReviewerFactory = OpenAIProfileReviewer,
        person_classifier_factory: PersonClassifierFactory = LocalPersonClassifier.from_env,
        network_retry_delays: tuple[float, ...] = (5, 15, 30, 60),
        lease_heartbeat_interval_seconds: float = 20,
        network_retry_concurrency: int = 3,
        network_retry_stagger_seconds: float = 0.25,
        person_recognition_timeout_seconds: float = 12.0,
        relationship_no_progress_retry_limit: int = 5,
        surface_no_progress_retry_limit: int = 3,
        recovery_cooldown_seconds: float = 15.0,
    ) -> None:
        self.service = service
        self.bitbrowser = bitbrowser
        self.worker_factory = worker_factory
        self.reviewer_factory = reviewer_factory
        self.person_classifier_factory = person_classifier_factory
        self._person_classifier: PersonClassifier | None = None
        self._person_classifier_lock = threading.Lock()
        self._person_inference_slots = asyncio.Semaphore(2)
        self._person_inference_circuit_open = False
        self._person_inference_circuit_event = asyncio.Event()
        self._person_detached_inferences: set[asyncio.Future[PersonRecognition]] = set()
        if not network_retry_delays or any(delay < 0 for delay in network_retry_delays):
            raise ValueError("network_retry_delays must contain non-negative delays")
        if lease_heartbeat_interval_seconds <= 0:
            raise ValueError("lease_heartbeat_interval_seconds must be positive")
        if network_retry_concurrency < 1:
            raise ValueError("network_retry_concurrency must be positive")
        if network_retry_stagger_seconds < 0:
            raise ValueError("network_retry_stagger_seconds must be non-negative")
        if person_recognition_timeout_seconds <= 0:
            raise ValueError("person_recognition_timeout_seconds must be positive")
        if relationship_no_progress_retry_limit < 0:
            raise ValueError(
                "relationship_no_progress_retry_limit must be non-negative"
            )
        if surface_no_progress_retry_limit < 0:
            raise ValueError("surface_no_progress_retry_limit must be non-negative")
        if (
            isinstance(recovery_cooldown_seconds, bool)
            or not isinstance(recovery_cooldown_seconds, (int, float))
            or not math.isfinite(recovery_cooldown_seconds)
            or recovery_cooldown_seconds <= 0
        ):
            raise ValueError("recovery_cooldown_seconds must be finite and positive")
        self.network_retry_delays = network_retry_delays
        self.lease_heartbeat_interval_seconds = lease_heartbeat_interval_seconds
        self.network_retry_concurrency = network_retry_concurrency
        self.network_retry_stagger_seconds = network_retry_stagger_seconds
        self.person_recognition_timeout_seconds = person_recognition_timeout_seconds
        # These are *no-progress* budgets, not total retry counters. Any newly
        # persisted candidate resets both counters. This lets a very large healthy
        # list run for hours. Once the short budget is exhausted, a bounded-rate
        # cooldown keeps this target and its window owned until it really finishes
        # or the user stops it. A timer never yields a locked window to another task.
        self.relationship_no_progress_retry_limit = (
            relationship_no_progress_retry_limit
        )
        self.surface_no_progress_retry_limit = surface_no_progress_retry_limit
        self.recovery_cooldown_seconds = float(recovery_cooldown_seconds)
        self._runs: dict[str, ExecutionControl] = {}
        # One window removal owns its stop/close sequence. Concurrent row actions
        # cannot change disposition or resume the worker midway through cleanup.
        self._window_removals: dict[
            tuple[str, str, str], tuple[str | None, bool, asyncio.Task[dict[str, Any]]]
        ] = {}
        # A profiles refresh runs in a FastAPI worker thread and can overlap the
        # few synchronous statements between lease acquisition and `_runs` setup.
        # Publishing this startup fence lets reconciliation remove genuinely stale
        # rows immediately without ever deleting a lease that is being started.
        self._starting_task_ids: set[str] = set()
        # This is the single cross-thread source for lease reconciliation. An entity
        # enters before its first lease acquisition and leaves only after every lease
        # is released, so there is no `_runs` -> `_starting` handoff gap to observe.
        self._activity_lock = threading.Lock()
        self._active_lease_task_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._closing = False
        # Serialize user-initiated retries per task. This is separate from the
        # start/add-window lock because a retry may need to await an old coordinator's
        # teardown before calling start(), which itself acquires ``_lock``. Per-task
        # locks ensure a slow 680-window cleanup does not block retries in other tasks.
        self._target_retry_locks: dict[str, asyncio.Lock] = {}
        self._returned_window_closures: dict[
            tuple[str, str, str], asyncio.Task[None]
        ] = {}
        self._returned_window_handoffs: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _wake_work_available(control: ExecutionControl | _WindowControlView) -> None:
        """Wake every window without exposing one worker's local clear to siblings."""

        shared = control._shared if isinstance(control, _WindowControlView) else control
        shared.work_generation += 1
        shared.work_available.set()
        for event in tuple(shared.profile_work_events.values()):
            event.set()

    @staticmethod
    def _dequeue_compatible_target(
        queue: asyncio.Queue[dict[str, Any]], profile_id: str,
        locked_usernames: set[str] | None = None,
        locked_waiters: set[str] | None = None,
    ) -> dict[str, Any]:
        """Pop one compatible target while preserving every skipped queue row.

        Explicitly constrained targets win over automatic rows for a matching
        window, preventing a large automatic backlog from starving a target that
        only a small window pool can process.  Other windows may skip that row and
        continue with the earliest compatible/automatic target.
        """

        queued: list[dict[str, Any]] = []
        while True:
            try:
                queued.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not queued:
            raise asyncio.QueueEmpty

        selected_index: int | None = None
        for index, target in enumerate(queued):
            username = str(target.get("username_norm") or target.get("username") or "").casefold()
            allowed = target.get("allowed_window_ids")
            if username in (locked_usernames or set()):
                if locked_waiters is not None and (not isinstance(allowed, list) or not allowed or profile_id in allowed):
                    locked_waiters.add(username)
                continue
            if isinstance(allowed, list) and allowed and profile_id in allowed:
                selected_index = index
                break
        if selected_index is None:
            for index, target in enumerate(queued):
                if str(target.get("username_norm") or target.get("username") or "").casefold() in (locked_usernames or set()):
                    continue
                allowed = target.get("allowed_window_ids")
                if not isinstance(allowed, list) or not allowed:
                    selected_index = index
                    break

        selected = queued[selected_index] if selected_index is not None else None
        for index, target in enumerate(queued):
            if index == selected_index:
                continue
            queue.put_nowait(target)
            # Balance the original queued item; the replacement put keeps one
            # unfinished unit for this target.
            queue.task_done()
        if selected is None:
            raise asyncio.QueueEmpty
        return selected

    async def start(self, owner_user_id: str, task_id: str, *, profile_ids: list[str] | None = None) -> dict[str, Any]:
        async with self._lock:
            if self._closing:raise ConflictError("Collection manager is shutting down")
            active = self._runs.get(task_id)
            if active and active.coordinator and not active.coordinator.done():
                raise ConflictError("Task is already executing")
            spec = self.service.get_task_execution_spec(owner_user_id, task_id)
            collection_platform(spec.get("settings"))
            if spec["status"] not in {
                "draft",
                "queued",
                "recoverable",
                "failed",
                "stopped",
                "paused",
                "waiting_network",
            }:
                raise ConflictError("Task cannot be started from its current state", details={"status": spec["status"]})
            runnable_targets = [
                target
                for target in spec["targets"]
                if target["status"] != "completed"
                and not target.get("manual_recovery_required")
            ]
            has_delayed_work = bool(
                spec.get("settings", {}).get("live_queue_enabled")
                and self.service.has_claimable_split_candidates(
                    owner_user_id, task_id
                )
            )
            if not runnable_targets and not has_delayed_work:
                raise ValidationError(
                    "Task has no runnable target; recover or delete its failed targets first"
                )
            if not spec["window_ids"]:
                raise ValidationError("Task has no BitBrowser windows")
            if not spec["modes"]:
                raise ValidationError("Task has no collection modes")
            selected_profiles = list(dict.fromkeys(profile_ids if profile_ids is not None else spec["window_ids"]))
            if not selected_profiles or not set(selected_profiles).issubset(spec["window_ids"]):
                raise ValidationError("Requested windows do not belong to this task")

            leases: dict[str, str] = {}
            with self._activity_lock:
                self._starting_task_ids.add(task_id)
                self._active_lease_task_ids.add(task_id)
            published = False
            try:
                for profile_id in selected_profiles:
                    leases[profile_id] = await self.service.acquire_browser_lease_async(
                        owner_user_id,
                        profile_id,
                        operation_type="collection",
                        entity_id=task_id,
                    )
                if self._closing:raise ConflictError("Collection manager is shutting down")
                pause_event = asyncio.Event()
                pause_event.set()
                control = ExecutionControl(
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    pause_event=pause_event,
                    stop_event=asyncio.Event(),
                    leases=leases,
                    target_queue=asyncio.Queue(),
                    network_retry_gate=asyncio.Semaphore(
                        self.network_retry_concurrency
                    ),
                )
                self.service.set_task_runtime_status(owner_user_id, task_id, "running")
                control.coordinator = asyncio.create_task(self._run(control), name=f"collection:{task_id}")
                with self._activity_lock:
                    self._runs[task_id] = control
                    self._starting_task_ids.discard(task_id)
                    published = True
                return self.service.get_task(owner_user_id, task_id)
            except BaseException:
                for profile_id, token in leases.items():
                    self.service.release_browser_lease(profile_id, token)
                with self._activity_lock:
                    self._active_lease_task_ids.discard(task_id)
                raise
            finally:
                if not published:
                    with self._activity_lock:
                        self._starting_task_ids.discard(task_id)

    async def pause(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        control = self._runs.get(task_id)
        if not control or not control.coordinator or control.coordinator.done():
            task = self.service.get_task(owner_user_id, task_id)
            if task["status"] in {"recoverable", "failed"}:
                # An abnormal worker exit has no live asyncio control left. Persisting
                # PAUSED lets the recovery dialog close without discarding checkpoints.
                return self.service.set_task_runtime_status(owner_user_id, task_id, "paused")
            if task["status"] == "paused":
                return task
            raise NotFoundError("Active task execution not found")
        if control.owner_user_id != owner_user_id:
            raise NotFoundError("Task execution not found")
        control.manually_paused = True
        control.pause_event.clear()
        self._wake_work_available(control)
        return self.service.set_task_runtime_status(owner_user_id, task_id, "paused")

    async def resume(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        control = self._runs.get(task_id)
        if control and control.source_recheck_profiles:
            raise ConflictError("来源复查正在安全清理窗口，请稍后继续")
        if not control or not control.coordinator or control.coordinator.done():
            # Recover a persisted paused/recoverable task by rebuilding its workers.
            return await self.start(owner_user_id, task_id)
        if control.owner_user_id != owner_user_id:
            raise NotFoundError("Task execution not found")

        # A fatal worker marks the durable task/target recoverable before the
        # coordinator has necessarily finished closing profiles and releasing leases.
        # Treat that short teardown window as a dead execution.  Reusing it used to
        # make "Continue" flash RUNNING and then immediately fall back to RECOVERABLE
        # when the old coordinator's final status write won the race.
        if control.fatal_status or control.stop_event.is_set():
            await asyncio.shield(control.coordinator)
            return await self.start(owner_user_id, task_id)

        # A live-queue worker can remain healthy and wait for new targets after one
        # target became recoverable/incomplete.  Resume those target rows inside the
        # same task and queue instead of merely toggling the task pause flag.
        for profile_id in tuple(control.manual_requests):
            self._revoke_manual_control(control, profile_id)
            control.profile_pause_events[profile_id].set()
        await self._enqueue_resumable_targets(control)
        control.manually_paused = False
        control.pause_event.set()
        self._wake_work_available(control)
        if control.network_waiters:
            # Resume is an explicit request to continue now. Wake both manually gated
            # authentication/challenge waits and ordinary capped-backoff waits; leaving
            # the latter asleep for up to five minutes made a healthy connection appear
            # impossible to resume.
            self._wake_network_waiters(control)
        active_waiter_ids = set(control.network_waiters) & set(
            control.started_profile_ids
        )
        status = (
            "waiting_network"
            if control.started_profile_ids
            and (
                active_waiter_ids == set(control.started_profile_ids)
                or self._has_global_auth_waiter(control.network_waiters)
            )
            else "running"
        )
        result = self.service.set_task_runtime_status(owner_user_id, task_id, status)
        self._notify_split_queue_control(control)
        return result

    def _owned_profile_control(
        self, owner_user_id: str, task_id: str, profile_id: str
    ) -> ExecutionControl:
        control = self._owned_control(owner_user_id, task_id)
        if profile_id not in control.leases:
            raise NotFoundError("Window execution not found")
        return control

    async def pause_for_page_close(self,owner_user_id,profile_id,lease_token):
        control=next((c for c in self._runs.values() if c.owner_user_id==owner_user_id and c.leases.get(profile_id)==lease_token),None)
        if control is None:raise ConflictError('任务已结束或更换，请重新查看')
        return await self.pause_window(owner_user_id,control.task_id,profile_id)

    def _manual_owned_control(self, owner_user_id, profile_id, lease_token):
        control = next((item for item in self._runs.values()
            if item.owner_user_id == owner_user_id
            and item.leases.get(profile_id) == lease_token
            and item.coordinator and not item.coordinator.done()
            and not item.tearing_down and not item.stop_event.is_set()), None)
        if control is None:
            raise ConflictError('任务已结束或更换，请重新查看')
        worker = control.profile_worker_tasks.get(profile_id)
        if worker is None or worker.done():
            raise ConflictError('当前窗口没有可停稳的采集执行')
        with self.service.database.read() as connection:
            row = connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',
                                     (profile_id,)).fetchone()
        if not row or row['lease_token'] != lease_token:
            raise ConflictError('窗口占用已变化，请重新查看')
        return control

    def manual_control_active(self, owner_user_id, profile_id, lease_token):
        try:
            control = self._manual_owned_control(owner_user_id, profile_id, lease_token)
        except DomainError:
            return False
        request = control.manual_requests.get(profile_id)
        return bool(request and request.get('state') == 'active'
                    and not request.get('resume_requested')
                    and request.get('lease_token') == lease_token
                    and not control.profile_pause_events[profile_id].is_set()
                    and profile_id not in control.manual_mode_active)

    async def begin_manual_control(self, owner_user_id, profile_id, lease_token):
        control = self._manual_owned_control(owner_user_id, profile_id, lease_token)
        request = control.manual_requests.get(profile_id)
        if request is None:
            request = {'state': 'preparing', 'lease_token': lease_token,
                       'settled': asyncio.Event(), 'resume': asyncio.Event(), 'children': {}}
            control.manual_requests[profile_id] = request
            control.manual_events[profile_id].set()
            # These signals only wake safe-point checks; no automation operation
            # passes a manual checkpoint while the request remains registered.
            self._wake_work_available(control)
            retry = control.retry_network_events.get(profile_id)
            if retry is not None:
                retry.set()
        try:
            await asyncio.wait_for(request['settled'].wait(),
                timeout=getattr(self, 'manual_control_wait_seconds', 8.0))
        except asyncio.TimeoutError as exc:
            raise ConflictError('正在等待该窗口当前页面操作安全结束，请稍后再次确认；画面仍保持只读',
                                details={'reason': 'manual_control_preparing'}) from exc
        if not self.manual_control_active(owner_user_id, profile_id, lease_token):
            raise ConflictError(request.get('message') or '尚未确认所有页面操作已停止，画面保持只读',
                                details={'reason': 'manual_control_not_quiescent'})
        return {'task_id': control.task_id, 'profile_id': profile_id, 'status': 'manual_control'}

    def _revoke_manual_control(self, control, profile_id):
        request = control.manual_requests.get(profile_id)
        if request is None:
            return
        revoke = getattr(self, 'manual_control_revoke', None)
        if callable(revoke):
            # Hide the interactive surface synchronously before any resumed task
            # can emit another browser command. A failed revoke leaves it paused.
            revoke(control.owner_user_id, profile_id)
        request['resume_requested'] = True
        if request['state'] in {'active', 'unsafe'}:
            request['state'] = 'resuming'
            control.manual_events[profile_id].clear()
        request['resume'].set()

    async def end_manual_control(self, owner_user_id, profile_id, lease_token):
        control = self._manual_owned_control(owner_user_id, profile_id, lease_token)
        return await self.resume_window(owner_user_id, control.task_id, profile_id)

    async def _manual_pause_checkpoint(self, control, profile_id):
        request = control.manual_requests.get(profile_id)
        if request is None or request.get('state') == 'resuming':
            return
        if profile_id in control.manual_mode_active:
            raise _ManualControlYield()
        worker = control.profile_workers.get(profile_id)
        owned = [worker, *request['children'].values()]
        owned.extend(getattr(worker, '_deferred_screening_workers', {}).values())
        owned.extend(getattr(worker, '_screening_worker_pool', {}).values())
        owned = list({id(item): item for item in owned if item is not None}.values())
        safe = worker is not None and all(
            not getattr(item, '_page_stage_abandoned', False)
            # A done task can still have a queued callback which starts resource
            # cleanup. Require the ownership set to drain, not merely Task.done().
            and not getattr(item, '_late_lifecycle_tasks', ())
            and not getattr(item, '_screening_slots_opening', ())
            and not getattr(item, '_retired_pages', ())
            and not getattr(item, '_page_factories', ())
            and (getattr(item, '_disconnect_task', None) is None
                 or item._disconnect_task.done())
            for item in owned if item is not None)
        request['state'] = ('resuming' if request.get('resume_requested')
                            else 'active' if safe else 'unsafe')
        if not safe:
            request['message'] = '页面仍有未结束的浏览器操作，暂不能手动操作；请继续只读查看'
        if not request.get('resume_requested'):
            control.profile_pause_events[profile_id].clear()
            state = control.profile_states.get(profile_id, {})
            self._profile_state_locked(control, profile_id, state='paused',
                reason='manual_control_active' if safe else 'manual_control_not_quiescent',
                message='该窗口已安全暂停，可手动处理；在采集页点击继续恢复任务' if safe else request['message'],
                current_mode=state.get('current_mode'), current_stage=state.get('current_stage'))
        request['settled'].set()
        resume_wait = asyncio.create_task(request['resume'].wait())
        stop_wait = asyncio.create_task(control.stop_event.wait())
        local_stop = control.profile_stop_events.get(profile_id)
        waits = [resume_wait, stop_wait]
        if local_stop is not None:
            waits.append(asyncio.create_task(local_stop.wait()))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                if not wait.done():
                    wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
            # Resume discards each old screening locator/page after input has been
            # revoked; the same durable candidate is reread by the next attempt.
            async def close_children():
                invalidation_errors = []
                for item in owned:
                    invalidate = getattr(item, 'invalidate_after_manual_control', None)
                    if callable(invalidate):
                        try:
                            invalidate()
                        except Exception as exc:
                            invalidation_errors.append(exc)
                async def close_one(child):
                    pending = child.disconnect()
                    if inspect.isawaitable(pending):
                        await pending
                results = await asyncio.gather(
                    *(close_one(child) for child in tuple(request['children'].values())),
                    return_exceptions=True)
                request['children'].clear()
                errors = [*invalidation_errors, *(value for value in results if isinstance(value, BaseException))]
                if errors:
                    raise errors[0]
            try:
                await finish_owned(close_children())
            finally:
                if control.manual_requests.get(profile_id) is request:
                    control.manual_requests.pop(profile_id, None)
                    control.manual_events[profile_id].clear()

    @staticmethod
    async def _manual_worker_checkpoint(control):
        profile_id = getattr(control, 'profile_id', None)
        if profile_id and profile_id in control.manual_requests:
            request = control.manual_requests[profile_id]
            if request.get('state') != 'resuming':
                raise _ManualControlYield()

    def _validate_window_target_control(
        self, owner_user_id: str, task_id: str, profile_id: str,
        expected_target_id: str,
    ) -> None:
        """Reject a stale target card before changing any window control event.

        This check and the caller's pause/stop event change contain no await.
        The coordinator therefore cannot switch targets between validation and
        accepting the control. Check the durable lease generation too: an old
        in-memory worker must never authorize control of a replacement owner.
        """
        control = self._runs.get(task_id)
        worker = control.profile_worker_tasks.get(profile_id) if control else None
        valid = bool(
            control and control.owner_user_id == owner_user_id
            and control.coordinator and not control.coordinator.done()
            and not control.tearing_down
            and profile_id in control.leases
            and worker and not worker.done()
            and control.profile_states.get(profile_id, {}).get("current_target_id")
            == expected_target_id
        )
        if valid:
            assert control is not None
            with self.service.database.read() as connection:
                valid = connection.execute(
                    """
                    SELECT 1 FROM task_targets target
                    JOIN tasks task ON task.id=target.task_id
                    JOIN browser_operation_leases lease
                      ON lease.profile_id=target.current_window_id
                    WHERE target.id=? AND target.task_id=?
                      AND task.owner_user_id=? AND target.current_window_id=?
                      AND target.status IN ('running', 'waiting_network', 'recoverable')
                      AND lease.owner_user_id=? AND lease.operation_type='collection'
                      AND lease.entity_id=? AND lease.lease_token=?
                    """,
                    (expected_target_id, task_id, owner_user_id, profile_id,
                     owner_user_id, task_id, control.leases[profile_id]),
                ).fetchone() is not None
        if not valid:
            raise ConflictError(
                "该窗口的任务目标已变化，请等待状态刷新后再操作",
                details={"task_id": task_id, "profile_id": profile_id,
                         "requested_target_id": expected_target_id},
            )

    async def pause_window(
        self, owner_user_id: str, task_id: str, profile_id: str,
        *, expected_target_id: str | None = None,
    ) -> dict[str, Any]:
        """Pause exactly one BitBrowser worker without changing task-wide state."""

        if expected_target_id:
            self._validate_window_target_control(
                owner_user_id, task_id, profile_id, expected_target_id
            )
        control = self._owned_profile_control(owner_user_id, task_id, profile_id)
        local_pause = control.profile_pause_events.get(profile_id)
        worker_task = control.profile_worker_tasks.get(profile_id)
        if local_pause is None or worker_task is None or worker_task.done():
            raise NotFoundError("Active window execution not found")
        local_pause.clear()
        async with control.network_state_lock:
            state = control.profile_states.get(profile_id, {})
            self._profile_state_locked(
                control,
                profile_id,
                state="paused",
                reason="manual_window_pause",
                message="该窗口已单独暂停；其他窗口继续采集",
                current_mode=state.get("current_mode"),
                current_stage=state.get("current_stage"),
            )
            control.profile_states[profile_id].update(
                {
                    "current_target_id": state.get("current_target_id"),
                    "current_target": state.get("current_target"),
                    "current_mode": state.get("current_mode"),
                    "current_stage": state.get("current_stage"),
                }
            )
        return {
            "task_id": task_id,
            "profile_id": profile_id,
            "status": "paused",
            "affected_window_count": 1,
        }

    async def resume_window(
        self, owner_user_id: str, task_id: str, profile_id: str,
        *, expected_target_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume or recreate exactly one window worker from durable checkpoints."""

        if (owner_user_id, task_id, profile_id) in self._window_removals:
            raise ConflictError("该窗口正在移除，清理完成前不能继续任务")
        control = self._runs.get(task_id)
        if control and profile_id in control.source_recheck_profiles:
            raise ConflictError("来源复查正在安全清理该窗口，请稍后继续")
        if expected_target_id:
            task = self.service.get_task(owner_user_id, task_id)
            target = next((t for t in task['targets'] if t['id'] == expected_target_id), None)
            bound = str((target or {}).get('current_window_id') or (target or {}).get('preferred_window_id') or '')
            current = (control.profile_states.get(profile_id, {}) if control else {})
            if (target is None or profile_id not in task.get('window_ids', [])
                    or (bound and bound != profile_id)
                    or target.get('status') == 'completed'
                    or target.get('current_stage') == 'deleted_archived'
                    or (current.get('current_target_id') not in {None, '', expected_target_id}
                        and current.get('state') not in {'idle', 'closed', 'stopped'})):
                raise ConflictError('该窗口的任务目标已变化，请刷新后再继续')
        if (
            not control
            or control.owner_user_id != owner_user_id
            or not control.coordinator
            or control.coordinator.done()
            or profile_id not in control.leases
        ):
            # After an app/Core restart the durable task and window assignment still
            # exist, but their asyncio execution object does not. Re-arm the target
            # bound to this window and rebuild workers from its saved checkpoints.
            task = self.service.get_task(owner_user_id, task_id)
            if profile_id not in task.get("window_ids", []):
                raise NotFoundError("Window execution not found")
            target = next(
                (
                    item for item in task.get("targets", [])
                    if (item['id'] == expected_target_id if expected_target_id else str(item.get("current_window_id") or item.get("preferred_window_id") or "") == profile_id)
                    and item.get("status") in {"recoverable", "failed", "stopped", "paused"}
                ),
                None,
            )
            if target is not None and target.get("manual_recovery_required"):
                try:
                    self.service.retry_task_target(
                        owner_user_id, task_id, str(target["id"])
                    )
                except ConflictError as exc:
                    if exc.details.get("reason") != "split_candidate_waiting":
                        raise
                    # Return-to-waiting has already transferred ownership to
                    # the durable queue. Rebuild the old window to claim that
                    # same target id; do not enqueue the old target separately.
                    if not task.get("settings", {}).get("live_queue_enabled"):
                        raise
            await self.start(owner_user_id, task_id, profile_ids=[profile_id])
            return {
                "task_id": task_id,
                "profile_id": profile_id,
                "status": "running",
                "affected_window_count": 1,
                "restored_from_checkpoint": True,
            }
        assert control is not None
        # A live idle worker can outlive a failed target. Continue must re-arm
        # that target's durable failure gate, not merely wake the idle worker.
        async with control.queue_claim_lock:
            if self._runs.get(task_id) is not control or control.tearing_down:
                raise ConflictError('该任务正在清理，请清理完成后继续')
            current = control.profile_states.get(profile_id, {})
            if (expected_target_id and current.get('current_target_id') not in {None, '', expected_target_id}
                    and current.get('state') not in {'idle', 'closed', 'stopped'}):
                raise ConflictError('该窗口的任务目标已变化，请刷新后再继续')
            with self.service.database.read() as connection:
                lease = connection.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?', (profile_id,)).fetchone()
            if not lease or lease['lease_token'] != control.leases.get(profile_id):
                raise ConflictError('窗口占用已变化，请刷新后再继续')
            spec = self.service.get_task_execution_spec(owner_user_id, task_id)
            for target in spec['targets']:
                matches = target['id'] == expected_target_id if expected_target_id else str(target.get('current_window_id') or target.get('preferred_window_id') or '') == profile_id
                if not matches or target['id'] in control.enqueued_target_ids or target['status'] not in {'recoverable', 'failed', 'stopped'}:
                    continue
                try:
                    target = self.service.retry_task_target(owner_user_id, task_id, target['id'])
                except ConflictError as exc:
                    if exc.details.get('reason') == 'split_candidate_waiting' and spec['settings'].get('live_queue_enabled'):
                        continue  # The durable waiting queue already owns it.
                    raise
                # This explicit Continue belongs to the clicked window only.
                target['allowed_window_ids'] = [profile_id]
                control.enqueued_target_ids.add(target['id'])
                control.target_queue.put_nowait(target)
        if control.profile_states.get(profile_id, {}).get("reason") == "browser_close_failed":
            # The source and its screening work already finished. Continue here
            # retries the owned close; it must not reopen a page or claim new work.
            closed = await finish_owned(self._close_drained_window(control, profile_id))
            control.workers_changed.set()
            return {
                "task_id": task_id,
                "profile_id": profile_id,
                "status": "closed" if closed else "manual_required",
                "affected_window_count": 1,
            }
        self._revoke_manual_control(control, profile_id)
        resume_token = control.leases.get(profile_id)
        worker_task = control.profile_worker_tasks.get(profile_id)
        local_pause = control.profile_pause_events.setdefault(profile_id, asyncio.Event())
        local_stop = control.profile_stop_events.get(profile_id)
        if worker_task is not None and not worker_task.done() and local_stop is not None and local_stop.is_set():
            # Resume is allowed only after the old generation releases its driver.
            await asyncio.shield(asyncio.gather(worker_task, return_exceptions=True))
            if self._runs.get(task_id) is not control or control.stop_event.is_set():
                raise ConflictError("旧任务已结束，请重新启动所选窗口")
            worker_task = control.profile_worker_tasks.get(profile_id)
        if (owner_user_id, task_id, profile_id) in self._window_removals:
            raise ConflictError("该窗口正在移除，清理完成前不能继续任务")
        if worker_task is None or worker_task.done():
            spec = self.service.get_task_execution_spec(owner_user_id, task_id)
            await self._enqueue_resumable_targets(control)
            # Enqueue waits for the queue lock. A delete may finish during that
            # wait, so require the original lease generation before reopening.
            if (
                (owner_user_id, task_id, profile_id) in self._window_removals
                or self._runs.get(task_id) is not control
                or control.leases.get(profile_id) != resume_token
                or profile_id in control.closing_profile_ids
            ):
                raise ConflictError("该窗口执行已变化，不能继续旧任务")
            self._spawn_window_loop(
                control, profile_id, [], spec["modes"], spec["settings"]
            )
        else:
            local_pause.set()
        retry_event = control.retry_network_events.get(profile_id)
        if retry_event is not None:
            retry_event.set()
        async with control.network_state_lock:
            # Local Continue must preserve a deliberate task-wide pause. Check
            # after acquiring the lock because Pause can arrive while we wait.
            # Re-arm this window for All Continue without claiming it is running.
            waiting_for_task_resume = control.manually_paused and not control.pause_event.is_set()
            state = dict(control.profile_states.get(profile_id, {}))
            self._profile_state_locked(
                control,
                profile_id,
                state="paused" if waiting_for_task_resume else "working",
                reason="global_task_pause" if waiting_for_task_resume else None,
                message="该窗口已准备继续；任务仍处于全部暂停，请点击“全部继续”" if waiting_for_task_resume else None,
            )
            control.profile_states[profile_id].update(
                {
                    "current_target_id": state.get("current_target_id"),
                    "current_target": state.get("current_target"),
                    "current_mode": state.get("current_mode"),
                    "current_stage": state.get("current_stage"),
                }
            )
        self._wake_work_available(control)
        return {
            "task_id": task_id,
            "profile_id": profile_id,
            "status": "paused" if waiting_for_task_resume else "running",
            "waiting_for_task_resume": waiting_for_task_resume,
            "affected_window_count": 1,
        }

    async def stop_window(
        self,
        owner_user_id: str,
        task_id: str,
        profile_id: str,
        *, expected_target_id: str | None = None,
    ) -> dict[str, Any]:
        """Stop one worker while retaining its window assignment for restart."""

        if expected_target_id:
            self._validate_window_target_control(
                owner_user_id, task_id, profile_id, expected_target_id
            )
        control = self._runs.get(task_id)
        has_live_profile = bool(
            control
            and control.owner_user_id == owner_user_id
            and control.coordinator
            and not control.coordinator.done()
            and profile_id in control.leases
        )
        if not has_live_profile:
            # A window-local worker can finish/fail before an older renderer sends
            # its Stop command.  The durable row is still a valid execution card;
            # make Stop an idempotent projection update instead of requiring a
            # live asyncio object which can no longer exist.  Never finalize the
            # task here because sibling windows may still be collecting.
            task = self.service.get_task(owner_user_id, task_id)
            if profile_id not in task.get("window_ids", []):
                return {
                    "task_id": task_id,
                    "profile_id": profile_id,
                    "status": "stopped",
                    "affected_window_count": 0,
                    "already_stopped": True,
                }
            bound_targets = [
                target
                for target in task.get("targets", [])
                if str(
                    target.get("current_window_id")
                    or target.get("preferred_window_id")
                    or ""
                )
                == profile_id
            ]
            changed = 0
            for target in bound_targets:
                if target.get("status") in {
                    "pending",
                    "running",
                    "waiting_network",
                    "recoverable",
                    "failed",
                }:
                    self.service.set_target_runtime_status(
                        owner_user_id,
                        task_id,
                        str(target["id"]),
                        "stopped",
                        window_id=profile_id,
                    )
                    changed += 1
            return {
                "task_id": task_id,
                "profile_id": profile_id,
                "status": "stopped",
                "affected_window_count": 1,
                "already_stopped": changed == 0,
            }
        assert control is not None
        retried_completed_cleanup = False
        if profile_id in control.source_recheck_profiles and profile_id in control.profile_workers:
            if self._closing or control.tearing_down or control.stop_event.is_set():
                raise ConflictError("任务正在清理，不能重复启动页面清理")
            retained = control.profile_workers[profile_id]
            old_task = control.profile_worker_tasks.get(profile_id)
            if old_task is None or old_task.done():
                async def retry_source_cleanup():
                    await retained.disconnect()
                    wait_for_cleanup = getattr(retained, "wait_for_cleanup", None)
                    if callable(wait_for_cleanup):
                        await wait_for_cleanup()
                    if control.profile_workers.get(profile_id) is retained:
                        control.profile_workers.pop(profile_id, None)
                    control.source_recheck_profiles.discard(profile_id)
                    control.cleaned_profile_ids.add(profile_id)
                    control.workers_changed.set()
                cleanup_task = control.source_recheck_cleanup_tasks.get(profile_id)
                if cleanup_task is None or cleanup_task.done():
                    cleanup_task = asyncio.create_task(retry_source_cleanup())
                    control.source_recheck_cleanup_tasks[profile_id] = cleanup_task
                try:
                    await finish_owned(cleanup_task)
                    retried_completed_cleanup = True
                except Exception as exc:
                    self._profile_state_locked(control, profile_id, state="manual_required",
                        reason="source_recheck_cleanup_failed", message="页面清理未成功，窗口仍由原任务占用；请再次停止重试清理")
                    raise ConflictError("页面清理未成功，已保留原窗口占用和检查点，请再次停止重试清理") from exc
                finally:
                    if cleanup_task.done() and control.source_recheck_cleanup_tasks.get(profile_id) is cleanup_task:
                        control.source_recheck_cleanup_tasks.pop(profile_id, None)
        self._revoke_manual_control(control, profile_id)
        local_stop = control.profile_stop_events.get(profile_id)
        local_pause = control.profile_pause_events.get(profile_id)
        if local_stop is not None:
            local_stop.set()
        if local_pause is not None:
            local_pause.set()
        retry_event = control.retry_network_events.get(profile_id)
        if retry_event is not None:
            retry_event.set()
        self._wake_work_available(control)
        worker_task = control.profile_worker_tasks.get(profile_id)
        if worker_task is not None and not worker_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker_task), timeout=5.0)
            except asyncio.CancelledError:
                # The worker may already have been cancelled by a profile-local
                # teardown path. Consume that terminal state without cancelling the
                # caller or any sibling worker.
                await asyncio.gather(worker_task, return_exceptions=True)
            except asyncio.TimeoutError:
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
        if retried_completed_cleanup and not await self._await_durable_thread_call(
            self.service.has_unfinished_window_targets, owner_user_id, task_id, profile_id,
        ):
            closed = await finish_owned(self._close_drained_window(control, profile_id))
            return {"task_id": task_id, "profile_id": profile_id,
                    "status": "closed" if closed else "manual_required", "affected_window_count": 1}
        async with control.network_state_lock:
            self._profile_state_locked(
                control,
                profile_id,
                state="stopped",
                reason="manual_window_stop",
                message="该窗口已单独停止；其他窗口继续采集",
            )
        return {
            "task_id": task_id,
            "profile_id": profile_id,
            "status": "stopped",
            "affected_window_count": 1,
        }

    async def restart_window(
        self, owner_user_id: str, task_id: str, profile_id: str,
        *, expected_target_id: str | None = None,
    ) -> dict[str, Any]:
        control = self._runs.get(task_id)
        if (
            not control
            or control.owner_user_id != owner_user_id
            or not control.coordinator
            or control.coordinator.done()
            or profile_id not in control.leases
        ):
            return await self.resume_window(owner_user_id, task_id, profile_id, expected_target_id=expected_target_id)
        if expected_target_id:
            task = self.service.get_task(owner_user_id, task_id)
            target = next((item for item in task['targets'] if item['id'] == expected_target_id), None)
            bound = str((target or {}).get('current_window_id') or (target or {}).get('preferred_window_id') or '')
            if (target is None or (bound and bound != profile_id)
                    or target.get('status') == 'completed' or target.get('current_stage') == 'deleted_archived'):
                raise ConflictError('该窗口的任务目标已变化，请刷新后再继续')
            current = control.profile_states.get(profile_id, {}).get('current_target_id')
            if current and current != expected_target_id:
                raise ConflictError('该窗口的任务目标已变化，请刷新后再继续')
        await self.stop_window(owner_user_id, task_id, profile_id)
        return await self.resume_window(owner_user_id, task_id, profile_id, expected_target_id=expected_target_id)

    async def delete_window(
        self,
        owner_user_id: str,
        task_id: str,
        profile_id: str,
        *,
        target_id: str | None = None,
        requeue: bool = True,
    ) -> dict[str, Any]:
        """Remove one execution unit; returning it to waiting is explicit disposition.

        Keep the historical default for older clients. A repeated in-flight request
        joins the same cleanup; conflicting delete/return requests never race each
        other. Cancellation still waits for the owned worker and browser to close.
        """
        key = (owner_user_id, task_id, profile_id)
        pending = self._window_removals.get(key)
        if pending is not None:
            if pending[:2] != (target_id, requeue):
                raise ConflictError("该窗口正在移除，请等待当前操作完成")
            result = await finish_owned(pending[2])
            return {**result, "removal_mode": "requeue" if requeue else "delete_only"}
        control = self._runs.get(task_id)
        guarded_target_id = target_id or str(
            (control.profile_states.get(profile_id, {}) if control else {}).get("current_target_id") or ""
        ) or None
        removal_token = (
            self.service.begin_target_removal(owner_user_id, task_id, guarded_target_id)
            if guarded_target_id else None
        )
        operation = asyncio.create_task(self._delete_window_owned(
            owner_user_id, task_id, profile_id, target_id=target_id, requeue=requeue,
            removal_token=removal_token,
        ))
        self._window_removals[key] = (target_id, requeue, operation)
        try:
            result = await finish_owned(operation)
            return {**result, "removal_mode": "requeue" if requeue else "delete_only"}
        finally:
            if self._window_removals.get(key, (None, None, None))[2] is operation:
                self._window_removals.pop(key, None)
            if guarded_target_id and removal_token is not None:
                self.service.end_target_removal(owner_user_id, guarded_target_id, removal_token)
                if requeue and operation.done() and not operation.cancelled() and operation.exception() is None:
                    # A sibling may have seen the durable waiting row while its
                    # source was still fenced. Wake it again after releasing that
                    # fence, including when the HTTP caller cancelled mid-close.
                    self.notify_split_queue(owner_user_id)

    async def _delete_window_owned(
        self,
        owner_user_id: str,
        task_id: str,
        profile_id: str,
        *,
        target_id: str | None,
        requeue: bool,
        removal_token: object | None,
    ) -> dict[str, Any]:
        """Remove one execution unit, release only its lease, preserve all others."""

        def target_window_id(target: dict[str, Any] | None) -> str:
            return str(
                (target or {}).get("current_window_id")
                or (target or {}).get("preferred_window_id")
                or ""
            )

        def deletable_after_partial_cleanup(target: dict[str, Any] | None) -> bool:
            """Recognize a prior DELETE which archived before returning its queue row."""

            return bool(
                target
                and not target_window_id(target)
                and target.get("status")
                in {"completed", "failed", "recoverable", "stopped"}
            )

        control = self._runs.get(task_id)
        if not requeue and not target_id:
            current_id = (control.profile_states.get(profile_id, {}) if control else {}).get("current_target_id")
            durable = self.service.get_task(owner_user_id, task_id)
            if current_id or any(
                target_window_id(item) == profile_id
                and item.get("status") not in {"completed"}
                for item in durable.get("targets", [])
            ):
                raise ConflictError("该窗口已领取目标，请刷新后使用目标卡上的删除按钮")
        live_task_control = bool(
            control
            and control.owner_user_id == owner_user_id
            and control.coordinator
            and not control.coordinator.done()
        )
        if live_task_control and profile_id not in control.leases:
            # A duplicated or delayed DELETE can arrive after the first request has
            # already removed this profile from the live control.  It must not fall
            # through to the stale-task cleanup below: that path intentionally stops
            # the whole persisted task and releases every lease when no live
            # coordinator exists.
            assert control is not None
            task = self.service.get_task(owner_user_id, task_id)
            if profile_id in task.get("window_ids", []):
                raise ConflictError(
                    "Window assignment exists but its live execution is unavailable",
                    details={"profile_id": profile_id, "task_id": task_id},
                )
            if target_id:
                surviving_target = next(
                    (
                        item
                        for item in task.get("targets", [])
                        if item["id"] == target_id
                    ),
                    None,
                )
                if surviving_target is not None:
                    surviving_window_id = target_window_id(surviving_target)
                    if surviving_window_id and surviving_window_id != profile_id:
                        raise ConflictError(
                            "The requested target no longer belongs to this window",
                            details={
                                "profile_id": profile_id,
                                "target_id": target_id,
                            },
                        )
                    if deletable_after_partial_cleanup(surviving_target):
                        return {
                            "task_id": task_id,
                            "profile_id": profile_id,
                            "target_id": target_id,
                            "status": "deleted",
                            "affected_window_count": 0,
                            "remaining_window_count": len(control.leases),
                            "already_deleted": True,
                            "requeued_candidate": None,
                            "completed_archived": bool(
                                surviving_target.get("status") == "completed"
                            ),
                        }
                    raise ConflictError(
                        "The requested target still exists after its window was removed",
                        details={
                            "profile_id": profile_id,
                            "target_id": target_id,
                        },
                    )
            return {
                "task_id": task_id,
                "profile_id": profile_id,
                "target_id": target_id,
                "status": "deleted",
                "affected_window_count": 0,
                "remaining_window_count": len(control.leases),
                "already_deleted": True,
                "requeued_candidate": None,
                "completed_archived": False,
            }
        live_control = bool(live_task_control and control and profile_id in control.leases)
        if not live_control:
            # Old releases can leave terminal task/window rows after their in-memory
            # execution has already disappeared. Treat deletion as idempotent cleanup
            # instead of reporting "execution not found" or "already running".
            task = self.service.get_task(owner_user_id, task_id)
            window_assigned = profile_id in task.get("window_ids", [])
            target = None
            if target_id:
                target = next(
                    (
                        item
                        for item in task.get("targets", [])
                        if item["id"] == target_id
                    ),
                    None,
                )
                if target is None:
                    raise NotFoundError("Task target not found")
                assigned_target_window_id = target_window_id(target)
                if assigned_target_window_id and assigned_target_window_id != profile_id:
                    raise ConflictError(
                        "The requested target no longer belongs to this window",
                        details={"profile_id": profile_id, "target_id": target_id},
                    )
                if not assigned_target_window_id:
                    if not deletable_after_partial_cleanup(target):
                        raise ConflictError(
                            "The requested target no longer belongs to this window",
                            details={"profile_id": profile_id, "target_id": target_id},
                        )
                    if not window_assigned:
                        # A delayed duplicate arrived after the first request had
                        # already removed the window and returned (or archived) the
                        # target.  It is an idempotent success, not a reason to make
                        # the operator repeat the cleanup.
                        return {
                            "task_id": task_id,
                            "profile_id": profile_id,
                            "target_id": target_id,
                            "status": "deleted",
                            "affected_window_count": 0,
                            "remaining_window_count": len(task.get("window_ids", [])),
                            "already_deleted": True,
                            "requeued_candidate": None,
                            "completed_archived": bool(
                                target.get("status") == "completed"
                            ),
                        }
            if task["status"] in {"running", "waiting_network", "paused", "queued"}:
                self.service.finalize_task_runtime_status(
                    owner_user_id,
                    task_id,
                    "stopped",
                    error="stale execution record removed",
                )
            if target_id:
                assert target is not None
                if target["status"] == "completed":
                    self.service.archive_completed_task_target_from_list(
                        owner_user_id, task_id, target_id
                    )
                    requeued_candidate = None
                    completed_archived = True
                else:
                    completed_archived = False
                    self.service.delete_task_target(
                        owner_user_id, task_id, target_id, dismiss=not requeue
                    )
                    requeued_candidate = (
                        self.service.requeue_removed_task_target(
                            owner_user_id, target_id, removed_profile_id=profile_id,
                            _removal_token=removal_token,
                        ) if requeue else None
                    )
            else:
                requeued_candidate = None
                completed_archived = False
            self.service.remove_task_windows(owner_user_id, task_id, [profile_id])
            self.service.release_browser_leases_for_entity(
                owner_user_id, task_id, operation_type="collection"
            )
            if requeued_candidate is not None:
                # A stale source task may have no live coordinator left.  Wake every
                # other live queue owned by this user after the durable return and
                # window removal have committed, so an eligible worker can claim it
                # without requiring the operator to paste the username again.
                self.notify_split_queue(owner_user_id)
            return {
                "task_id": task_id,
                "profile_id": profile_id,
                "target_id": target_id,
                "status": "deleted",
                "affected_window_count": 1,
                "remaining_window_count": len(
                    self.service.get_task(owner_user_id, task_id).get("window_ids", [])
                ),
                "stale_record_cleaned": True,
                "requeued_candidate": requeued_candidate,
                "completed_archived": completed_archived,
            }
        assert control is not None
        state = dict(control.profile_states.get(profile_id, {}))
        pre_stop_target_id = str(state.get("current_target_id") or "") or None
        requested_target_id = str(target_id or "").strip() or None
        if (
            pre_stop_target_id
            and requested_target_id
            and pre_stop_target_id != requested_target_id
        ):
            raise ConflictError(
                "The requested target no longer belongs to this window",
                details={
                    "profile_id": profile_id,
                    "requested_target_id": requested_target_id,
                    "current_target_id": pre_stop_target_id,
                },
            )
        resolved_target_id = pre_stop_target_id or requested_target_id
        if resolved_target_id:
            before_stop = self.service.get_task(owner_user_id, task_id)
            before_target = next(
                (
                    item
                    for item in before_stop.get("targets", [])
                    if item["id"] == resolved_target_id
                ),
                None,
            )
            before_window_id = target_window_id(before_target)
            if before_target is None or (
                before_window_id != profile_id
                and not deletable_after_partial_cleanup(before_target)
            ):
                raise ConflictError(
                    "The requested target no longer belongs to this window",
                    details={
                        "profile_id": profile_id,
                        "target_id": resolved_target_id,
                    },
                )
        await self.stop_window(owner_user_id, task_id, profile_id)
        if resolved_target_id:
            current = self.service.get_task(owner_user_id, task_id)
            target = next(
                (
                    item
                    for item in current.get("targets", [])
                    if item["id"] == resolved_target_id
                ),
                None,
            )
            current_target_window_id = target_window_id(target)
            if target is None or (
                current_target_window_id != profile_id
                and not deletable_after_partial_cleanup(target)
            ):
                raise ConflictError(
                    "The target changed while its window was stopping",
                    details={
                        "profile_id": profile_id,
                        "target_id": resolved_target_id,
                    },
                )
            if target["status"] == "completed":
                self.service.archive_completed_task_target_from_list(
                    owner_user_id, task_id, resolved_target_id
                )
                requeued_candidate = None
                completed_archived = True
            else:
                self.service.delete_task_target(
                    owner_user_id, task_id, resolved_target_id, dismiss=not requeue
                )
                requeued_candidate = (
                    self.service.requeue_removed_task_target(
                        owner_user_id, resolved_target_id, removed_profile_id=profile_id,
                        _removal_token=removal_token,
                    ) if requeue else None
                )
                completed_archived = False
            control.deleted_target_ids.add(resolved_target_id)
            control.enqueued_target_ids.discard(resolved_target_id)
            self._wake_work_available(control)
        else:
            requeued_candidate = None
            completed_archived = False
        self.service.remove_task_windows(owner_user_id, task_id, [profile_id])
        if requeued_candidate is not None:
            # Notify after removing the source window.  This ordering matters when
            # it was the task's last window: the returned candidate can become
            # eligible for another live queue only after that durable removal.
            self.notify_split_queue(owner_user_id)
        token = control.leases.get(profile_id)
        control.closing_profile_ids.add(profile_id)

        async def finish_delete() -> None:
            # Keep both the registry entry and the persisted token until the close
            # thread settles. Concurrent Stop/Shutdown joins the same close task.
            if token is not None:
                await self._close_profiles_until_confirmed({profile_id: token}, control=control)
                self.service.release_browser_lease(profile_id, token)
                control.profile_close_tasks.pop((profile_id, token), None)
            # Duplicate deletes can finish after this profile has been added again.
            # Only the removed token may dispose its worker gates and UI state.
            if control.leases.get(profile_id) == token:
                control.leases.pop(profile_id, None)
                control.profile_pause_events.pop(profile_id, None)
                control.profile_stop_events.pop(profile_id, None)
                control.profile_work_events.pop(profile_id, None)
                control.profile_worker_tasks.pop(profile_id, None)
                control.profile_states.pop(profile_id, None)
                control.network_waiters.pop(profile_id, None)
                control.retry_network_events.pop(profile_id, None)
                control.closing_profile_ids.discard(profile_id)
            if not control.leases:
                control.fatal_status = "recoverable"
                control.fatal_error = "所有执行窗口均已删除，任务检查点已保留"
                control.stop_event.set()
                control.pause_event.set()
                control.workers_changed.set()

        await finish_owned(finish_delete())
        return {
            "task_id": task_id,
            "profile_id": profile_id,
            "target_id": resolved_target_id,
            "status": "deleted",
            "affected_window_count": 1,
            "remaining_window_count": len(control.leases),
            "requeued_candidate": requeued_candidate,
            "completed_archived": completed_archived,
        }

    async def _enqueue_resumable_targets(self, control: ExecutionControl) -> int:
        """Requeue persisted unfinished targets without losing progress.

        ``enqueued_target_ids`` stays set while a target is queued or actively owned
        by a window, so the lock also closes the small race between the worker marking
        a target recoverable and removing its in-memory ownership marker.
        """
        added = 0
        async with control.queue_claim_lock:
            spec = self.service.get_task_execution_spec(
                control.owner_user_id, control.task_id
            )
            for target in spec["targets"]:
                if control.source_recheck_target_id and target["id"] != control.source_recheck_target_id:
                    continue
                if target.get("manual_recovery_required"):
                    continue
                if target["status"] not in {"pending", "recoverable", "failed", "stopped"}:
                    continue
                if target["id"] in control.enqueued_target_ids:
                    continue
                if target["status"] != "pending":
                    target = self.service.retry_task_target(
                        control.owner_user_id, control.task_id, target["id"]
                    )
                control.enqueued_target_ids.add(target["id"])
                control.target_queue.put_nowait(target)
                added += 1
        return added

    async def runtime_diagnostics(
        self,
        owner_user_id: str,
        task_id: str,
        *,
        known_status: str | None = None,
    ) -> dict[str, Any]:
        """Return one coherent event-loop-owned network/progress snapshot.

        FastAPI previously executed this as a synchronous worker-thread route while
        collection coroutines mutated ``network_waiters``. Copying ``dict.values()``
        across those threads could raise ``RuntimeError: dictionary changed size``.
        The endpoint now copies state synchronously on the owning event loop.
        Durable task reads happen before this copy: slow SQLite must never block
        network recovery, pause or stop state transitions.  Workbench callers may
        provide the status they already loaded to avoid a second full task query.
        """
        control = self._runs.get(task_id)
        status = known_status
        if status is None:
            task = await asyncio.to_thread(
                self.service.get_task, owner_user_id, task_id
            )
            status = str(task["status"])
        if not control or control.owner_user_id != owner_user_id:
            return {
                "task_id": task_id,
                "active": False,
                "status": status,
                "waiting_for_network": status == "waiting_network",
                "network_waiters": [],
                "profile_states": [],
                "network_waiting_window_count": 0,
                "active_window_count": 0,
                "last_progress_at": None,
                "last_network_success_at": None,
                "person_model_state": "inactive",
                "person_model_error": None,
            }
        # This projection contains no await: all runtime dictionaries belong to
        # this event loop, so one synchronous copy is coherent even while another
        # coroutine awaits a durable recovery write under network_state_lock.
        # Waiting for that write would make a read-only UI refresh time out too.
        # Remove impossible leftovers before publishing diagnostics. A waiter is a
        # live coroutine owned by a started profile, not durable history; retaining
        # one after its worker exited made Retry-now signal an Event nobody awaited.
        waiters_by_profile = self._active_waiters_locked(control)
        waiting_profiles = {
            profile_id for profile_id, waiter in waiters_by_profile.items()
            if not waiter.get("recovery_in_progress")
        }
        # Public waiters and their count describe actual sleepers. Retained
        # in-progress recovery budgets remain internal; their live stage is
        # published separately in profile_states below.
        waiters = [
            dict(waiter) for key, waiter in waiters_by_profile.items()
            if key in waiting_profiles
        ]
        active_profile_ids = set(control.started_profile_ids)
        global_auth_wait = self._has_global_auth_waiter(waiters_by_profile)
        ready_profile_ids = (
            set()
            if global_auth_wait
            else active_profile_ids - waiting_profiles
        )
        profile_states: list[dict[str, Any]] = []
        for profile_id in control.leases:
            state = dict(
                control.profile_states.get(
                    profile_id,
                    {
                        "profile_id": profile_id,
                        "state": (
                            "starting"
                            if profile_id in active_profile_ids
                            else "stopped"
                        ),
                        "current_target_id": None,
                        "current_target": None,
                        "current_mode": None,
                        "current_stage": None,
                        "last_progress_at": None,
                        "last_success_at": None,
                        "reason": None,
                        "message": None,
                    },
                )
            )
            waiter = waiters_by_profile.get(profile_id)
            if waiter:
                attempting = bool(waiter.get("recovery_in_progress"))
                progressed = attempting and bool(waiter.get("progress_confirmed"))
                waiter_state = waiter.get("state") or "waiting_network"
                state.update(
                    {
                        "state": "working" if attempting else waiter_state,
                        "current_target_id": waiter.get("target_id"),
                        "current_target": waiter.get("target"),
                        "current_mode": waiter.get("mode"),
                        "current_stage": (
                            state.get("current_stage") if progressed else "recovering_page" if attempting else
                            "manual_required"
                            if waiter_state in {"auth_required", "manual_intervention"}
                            else "waiting_network"
                        ),
                        "reason": None if progressed else waiter.get("reason"),
                        "message": None if progressed else "正在按保存的检查点恢复并验证进度" if attempting else waiter.get("message"),
                        "retry_count": waiter.get("retry_count", 0),
                        "next_retry_at": None if attempting else waiter.get("next_retry_at"),
                        "recovery_in_progress": attempting,
                        "progress_confirmed": progressed,
                        "retry_delay_seconds": waiter.get("retry_delay_seconds"),
                        "recovery_kind": waiter.get("recovery_kind"),
                        "original_reason": waiter.get("original_reason"),
                        "candidate_username": waiter.get("candidate_username"),
                        "source_discovery_complete": waiter.get("source_discovery_complete", False),
                        "generation": waiter.get("generation"),
                    }
                )
                for key in ("surface_diagnostics", "row_diagnostics", "scroll_diagnostics", "hover_diagnostics"):
                    if clean := PlaywrightWorker._safe_relation_diagnostics(waiter.get(key)):
                        state[key] = clean
            elif global_auth_wait and profile_id in active_profile_ids:
                state.update(
                    {
                        "state": "blocked_by_auth",
                        "current_stage": "manual_required",
                        "reason": BitBrowserAuthRequiredError.code,
                        "message": "BitBrowser 全局登录失效，等待重新登录",
                    }
                )
            manual = control.manual_requests.get(profile_id)
            if manual and manual.get('state') in {'active', 'unsafe'}:
                state.update(state='paused', reason='manual_control_active'
                    if manual['state'] == 'active' else 'manual_control_not_quiescent',
                    message='该窗口已安全暂停，可手动处理；在采集页点击继续恢复任务'
                    if manual['state'] == 'active' else manual.get('message'))
            source_session = control.live_source_rechecks.get(profile_id)
            if source_session and source_session.get("target_id") == state.get("current_target_id"):
                state["parent_activity"] = source_session.get("parent_activity")
                state["source_recheck_mode"] = source_session.get("mode")
            profile_states.append(state)
        return {
            "task_id": task_id,
            "active": bool(
                control.coordinator and not control.coordinator.done()
            ),
            "status": status,
            "waiting_for_network": bool(waiting_profiles),
            "network_waiting_window_count": len(waiting_profiles),
            "active_window_count": len(ready_profile_ids),
            "network_waiters": waiters,
            "profile_states": profile_states,
            "last_progress_at": control.last_progress_at,
            "last_network_success_at": control.last_network_success_at,
            "person_model_state": control.person_model_state,
            "person_model_error": control.person_model_error,
            "person_inference_circuit_open": self._person_inference_circuit_open,
            "person_detached_inference_count": len(
                self._person_detached_inferences
            ),
        }

    async def retry_waiting_windows(self, owner_user_id: str) -> int:
        """Connection recheck wakes existing recovery, without resuming pauses."""
        count = 0
        for control in tuple(self._runs.values()):
            if control.owner_user_id != owner_user_id or control.manually_paused:
                continue
            async with control.network_state_lock:
                for profile_id, waiter in self._active_waiters_locked(control).items():
                    pause = control.profile_pause_events.get(profile_id)
                    if (pause is not None and not pause.is_set()) or waiter.get('recovery_kind') in {'global_auth', 'profile_intervention'}:
                        continue
                    retry = control.retry_network_events.get(profile_id)
                    if retry is not None:
                        retry.set(); count += 1
        return count

    async def retry_network_now(
        self, owner_user_id: str, task_id: str
    ) -> dict[str, Any]:
        """Wake every current backoff sleeper on the execution's owning loop."""
        control = self._owned_control(owner_user_id, task_id)
        for profile_id in tuple(control.manual_requests):
            self._revoke_manual_control(control, profile_id)
            control.profile_pause_events[profile_id].set()
        async with control.network_state_lock:
            waiters = self._active_waiters_locked(control)
            if (
                self._has_global_auth_waiter(waiters)
                and not control.manually_paused
            ):
                control.pause_event.set()
            self._wake_network_waiters(control)
        return await self.runtime_diagnostics(owner_user_id, task_id)

    @staticmethod
    def _wake_network_waiters(control: ExecutionControl) -> None:
        """Broadcast one retry request from the execution's owning event loop."""
        active_profile_ids = set(control.started_profile_ids)
        for profile_id, retry_event in tuple(control.retry_network_events.items()):
            if profile_id in active_profile_ids and profile_id in control.network_waiters:
                retry_event.set()

    @staticmethod
    def _active_waiters_locked(
        control: ExecutionControl,
    ) -> dict[str, dict[str, Any]]:
        """Return only waiters that still have a live owning worker.

        This method must run synchronously on the owning event loop. It also removes stale
        wake Events so a successful API response can never mean “an orphan Event was
        set” instead of “a recovery coroutine was woken”.
        """

        active_profile_ids = set(control.started_profile_ids)
        for profile_id in tuple(control.network_waiters):
            if profile_id not in active_profile_ids:
                control.network_waiters.pop(profile_id, None)
                control.retry_network_events.pop(profile_id, None)
        return {
            profile_id: waiter
            for profile_id, waiter in control.network_waiters.items()
            if profile_id in active_profile_ids
        }

    @staticmethod
    def _network_wait_error(waiters: dict[str, dict[str, Any]]) -> str:
        if any(waiter.get("reason") == "instagram_page_recovery_exhausted" for waiter in waiters.values()):
            if any(waiter.get("state") == "manual_intervention" for waiter in waiters.values()):
                return "部分页面需要手动检查，账号和检查点已保留；其他窗口继续采集"
            return "部分页面暂未恢复，已保留检查点，冷却后自动续采；其他窗口继续采集"
        if any(
            waiter.get("reason") == BitBrowserAuthRequiredError.code
            for waiter in waiters.values()
        ):
            return "BitBrowser 已退出登录；任务已保留检查点，请重新登录后点击从原检查点继续"
        if any(
            ExecutionManager._is_profile_intervention_error_code(
                str(waiter.get("reason") or "")
            )
            for waiter in waiters.values()
        ):
            return "部分账号窗口需要登录、验证或等待限流解除；其他正常窗口会继续采集"
        return "网络或 Instagram 暂不可达；任务已保留检查点并等待自动恢复"

    @staticmethod
    def _has_global_auth_waiter(waiters: dict[str, dict[str, Any]]) -> bool:
        return any(
            waiter.get("reason") == BitBrowserAuthRequiredError.code
            for waiter in waiters.values()
        )

    async def _reconcile_network_task_status_locked(
        self, control: ExecutionControl, *,
        waiters: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Derive the durable task state from active profile IDs, never raw counts."""

        if waiters is None:
            waiters = self._active_waiters_locked(control)
        active_profile_ids = set(control.started_profile_ids)
        if (
            control.stop_event.is_set()
            or control.terminal_override
            or not active_profile_ids
        ):
            return
        all_waiting = (
            self._has_global_auth_waiter(waiters)
            or {key for key, waiter in waiters.items() if not waiter.get("recovery_in_progress")} == active_profile_ids
        )
        desired = "waiting_network" if all_waiting else "running"
        error = self._network_wait_error(waiters) if all_waiting else None
        await self._await_durable_thread_call(
            self.service.reconcile_task_network_status,
            control.owner_user_id, control.task_id, desired, error=error,
        )

    def _record_profile_progress(
        self, control: ExecutionControl | _WindowControlView,
        profile_id: str | None = None,
        *, current_stage: str | None = None,
    ) -> None:
        """Record useful work for its window; sibling traffic is not progress."""
        now = self._utc_iso()
        control.last_progress_at = now
        profile_id = profile_id or getattr(control, "profile_id", None)
        if profile_id is not None:
            state = control.profile_states.get(profile_id)
            if state is not None:
                state["last_progress_at"] = now
                if current_stage is not None:
                    state["current_stage"] = current_stage
            waiter = control.network_waiters.get(profile_id)
            if waiter and waiter.get("recovery_in_progress"):
                waiter["progress_confirmed"] = True

    @staticmethod
    def _profile_state_locked(
        control: ExecutionControl,
        profile_id: str,
        *,
        state: str,
        target: dict[str, Any] | None = None,
        reason: str | None = None,
        message: str | None = None,
        last_success_at: str | None = None,
        current_mode: str | None = None,
        current_stage: str | None = None,
    ) -> None:
        existing = control.profile_states.get(profile_id, {})
        progress_at = existing.get("last_progress_at")
        if state in {"starting", "idle"} and target is None:
            progress_at = None
        elif target and target.get("id") != existing.get("current_target_id"):
            # A fresh target gets its own starting baseline. Network heartbeat or
            # probe success on the same target must never reset its stall clock.
            progress_at = ExecutionManager._utc_iso()
        control.profile_states[profile_id] = {
            "profile_id": profile_id,
            "state": state,
            "current_target_id": target.get("id") if target else None,
            "current_target": target.get("username") if target else None,
            "current_mode": (
                current_mode
                if current_mode is not None
                else existing.get("current_mode") if target else None
            ),
            "current_stage": (
                current_stage
                if current_stage is not None
                else existing.get("current_stage") if target else None
            ),
            "last_progress_at": progress_at,
            "last_success_at": last_success_at
            if last_success_at is not None
            else existing.get("last_success_at"),
            "reason": reason,
            "message": message,
        }

    async def stop(
        self,
        owner_user_id: str,
        task_id: str,
        *,
        close_windows: bool = True,
    ) -> dict[str, Any]:
        inactive_task: dict[str, Any] | None = None
        closing_leases: dict[str, str] = {}
        # Serialize the stale-control decision with start(). Historical window
        # assignments grant no right to close a window another operation now owns.
        async with self._lock:
            control = self._runs.get(task_id)
            if not control or not control.coordinator or control.coordinator.done():
                inactive_task = self.service.get_task(owner_user_id, task_id)
                if inactive_task["status"] not in {"completed", "failed", "stopped"}:
                    inactive_task = self.service.finalize_task_runtime_status(
                        owner_user_id, task_id, "stopped"
                    )
                self.service.release_browser_leases_for_entity(
                    owner_user_id, task_id, operation_type="collection"
                )
                try:
                    if close_windows:
                        for profile_id in dict.fromkeys(inactive_task.get("window_ids", [])):
                            try:
                                closing_leases[profile_id] = await self.service.acquire_browser_lease_async(
                                    owner_user_id, profile_id, operation_type="account",
                                    entity_id=f"collection-close:{task_id}",
                                )
                            except ConflictError:
                                # A currently occupied window belongs exclusively to
                                # its new task; closing this historical task skips it.
                                continue
                except BaseException:
                    for profile_id, token in closing_leases.items():
                        self.service.release_browser_lease(profile_id, token)
                    raise
        if inactive_task is not None:
            async def close_inactive() -> None:
                try:
                    await self._close_profiles_until_confirmed(closing_leases)
                finally:
                    for profile_id, token in closing_leases.items():
                        self.service.release_browser_lease(profile_id, token)
            # A cancelled HTTP request must not abandon a still-running Local API
            # close thread or release the temporary lock before it finishes.
            await finish_owned(close_inactive())
            return inactive_task
        assert control is not None
        if control.owner_user_id != owner_user_id:
            raise NotFoundError("Task execution not found")
        # A user-requested terminal state wins over any recoverable/fatal marker that
        # an already-unwinding worker may publish a few milliseconds later.
        for profile_id in tuple(control.manual_requests):
            self._revoke_manual_control(control, profile_id)
        control.terminal_override = "stopped"
        control.close_profiles_on_exit = close_windows
        # Fence add_windows before taking a worker snapshot. Otherwise a dynamic
        # worker can be appended after stop() gathered the old list and keep using a
        # profile whose lease is about to be released by the coordinator.
        control.tearing_down = True
        control.stop_event.set()
        control.pause_event.set()
        self._wake_work_available(control)
        for worker_task in tuple(control.worker_tasks):
            if worker_task is not control.coordinator and not worker_task.done() and not worker_task.cancelling():
                worker_task.cancel()
        self._mark_running_targets(owner_user_id, task_id, "recoverable")
        result = self.service.finalize_task_runtime_status(
            owner_user_id, task_id, "stopped"
        )
        if control.coordinator and not control.coordinator.done():
            # The worker tasks are already cancelled above. Let the coordinator
            # finish its `finally` block instead of cancelling it mid-cleanup,
            # otherwise a window may close while its lease remains locked.
            await asyncio.gather(control.coordinator, return_exceptions=True)
        # `stop()` can race with the coordinator finishing its `finally` block.
        # Closing through one guarded path makes the user-facing one-click close
        # deterministic without issuing duplicate close requests.
        if close_windows:
            await self._close_control_profiles(control)
        # Idempotent safety net for a coordinator that was already externally
        # cancelled: a stopped task must never leave a browser lease behind.
        for profile_id, token in control.leases.items():
            self.service.release_browser_lease(
                profile_id, token, completed_cleanup=profile_id in control.cleaned_profile_ids,
            )
        for key in tuple(self._returned_window_handoffs):
            if key[0] == control.task_id:
                self._returned_window_handoffs.pop(key, None)
        with self._activity_lock:
            # Cleanup may have released the old generation before this waiter
            # resumes. Never unregister a new generation started in that interval.
            if self._runs.get(task_id) is control:
                self._active_lease_task_ids.discard(task_id)
                self._runs.pop(task_id, None)
        return result

    async def stop_nurture_collection_blocker(self, studio, owner_user_id: str,
                                              job_id: str, task_id: str, version: int):
        from .nurture_collection_blocker import stop_inactive_collection_blocker
        async with self._lock:
            # Surface locks may be held during desktop I/O. Keep the API loop
            # responsive, but retain the manager lock until the owned thread ends.
            return await finish_owned(asyncio.to_thread(stop_inactive_collection_blocker,
                studio, self, owner_user_id, job_id, task_id, version))

    async def close_task(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        """One-click close: stop the current queue, close its profiles, and release leases."""
        return await self.stop(owner_user_id, task_id, close_windows=True)

    async def restart(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        await self.stop(owner_user_id, task_id)
        current = self.service.get_task(owner_user_id, task_id)
        if current["status"] in {"completed", "failed", "stopped", "recoverable"}:
            self.service.control_task(owner_user_id, task_id, "restart")
        return await self.start(owner_user_id, task_id)

    async def wait(self, task_id: str) -> None:
        control = self._runs.get(task_id)
        if control and control.coordinator:
            await asyncio.shield(control.coordinator)

    def active_task_ids(self) -> set[str]:
        """Return the atomic set of entities that may still own collection leases."""
        with self._activity_lock:
            return self._active_lease_task_ids.copy()

    async def add_targets(
        self, owner_user_id: str, task_id: str, targets: list[str]
    ) -> dict[str, Any]:
        """Persist targets and immediately wake idle windows of a live collection."""
        result = self.service.add_targets(owner_user_id, task_id, targets)
        control = self._runs.get(task_id)
        if (
            control
            and control.owner_user_id == owner_user_id
            and control.coordinator
            and not control.coordinator.done()
        ):
            for target in result.get("targets", []):
                if target["id"] not in control.enqueued_target_ids:
                    control.enqueued_target_ids.add(target["id"])
                    control.target_queue.put_nowait(target)
                    self._wake_work_available(control)
        return result

    async def add_windows(
        self, owner_user_id: str, task_id: str, window_ids: list[str]
    ) -> dict[str, Any]:
        """Atomically fence and append workers to an active live collection queue."""
        async with self._lock:
            if self._closing:raise ConflictError("Collection manager is shutting down")
            control = self._owned_control(owner_user_id, task_id)
            if control.tearing_down:
                raise ConflictError("Task execution is shutting down")
            spec = self.service.get_task_execution_spec(owner_user_id, task_id)
            if not spec.get("settings", {}).get("live_queue_enabled"):
                raise ConflictError("Windows can only be added to a live queue task")
            if spec["status"] not in {"running", "paused", "waiting_network"}:
                raise ConflictError(
                    "Task is not accepting additional windows",
                    details={"status": spec["status"]},
                )

            normalized: list[str] = []
            for raw_profile_id in window_ids:
                profile_id = str(raw_profile_id).strip()
                if not profile_id or len(profile_id) > 128 or any(
                    ord(character) < 32 for character in profile_id
                ):
                    raise ValidationError("Invalid BitBrowser profile id")
                if profile_id in normalized or profile_id in control.leases:
                    raise ConflictError(
                        "BitBrowser window is already assigned to this task",
                        details={"profile_id": profile_id, "entity_id": task_id},
                    )
                normalized.append(profile_id)
            if not normalized:
                raise ValidationError("At least one BitBrowser window is required")

            acquired: dict[str, str] = {}
            newly_assigned = [profile_id for profile_id in normalized if profile_id not in spec["window_ids"]]
            persisted = False
            created_workers: list[asyncio.Task[None]] = []
            try:
                # Acquire every lease before changing task_windows. A conflict on any
                # requested profile rolls back all leases acquired earlier in this call.
                for profile_id in normalized:
                    acquired[profile_id] = await self.service.acquire_browser_lease_async(
                        owner_user_id,
                        profile_id,
                        operation_type="collection",
                        entity_id=task_id,
                    )
                if self._closing:raise ConflictError("Collection manager is shutting down")
                if control.tearing_down or self._runs.get(task_id) is not control:
                    raise ConflictError("Task execution is shutting down")
                if newly_assigned:
                    self.service.add_task_windows(owner_user_id, task_id, newly_assigned)
                    persisted = True
                # Drained profiles keep their durable assignment for recovery,
                # but only a new, explicitly acquired lease re-admits a worker.
                result = {
                    "added": len(normalized), "window_ids": normalized,
                    "task": self.service.get_task(owner_user_id, task_id),
                }
                control.leases.update(acquired)
                for profile_id in normalized:
                    created_workers.append(
                        self._spawn_window_loop(
                            control,
                            profile_id,
                            [],
                            spec["modes"],
                            spec["settings"],
                        )
                    )
            except BaseException:
                for worker_task in created_workers:
                    worker_task.cancel()
                if created_workers:
                    await asyncio.gather(*created_workers, return_exceptions=True)
                for profile_id in normalized:
                    control.started_profile_ids.discard(profile_id)
                    control.leases.pop(profile_id, None)
                if persisted:
                    self.service.remove_task_windows(owner_user_id, task_id, newly_assigned)
                for profile_id, token in acquired.items():
                    self.service.release_browser_lease(profile_id, token)
                raise
            return result

    def notify_split_queue(
        self, owner_user_id: str, *, returned_candidate: dict[str, Any] | None = None
    ) -> int:
        """Wake idle live-queue workers after a durable split enqueue.

        The queue row is committed before this method is called.  An Event is enough
        because the worker performs the actual database claim in one transaction;
        no candidate payload crosses threads and duplicate notifications are free.
        FastAPI may invoke this from a worker thread, so only the coordinator's
        owning loop may inspect/mutate the execution control and its asyncio events.
        """

        notified = 0
        for control in tuple(self._runs.values()):
            if (
                control.owner_user_id == owner_user_id
                and control.coordinator
                and not control.coordinator.done()
                and not control.stop_event.is_set()
            ):
                owning_loop = control.coordinator.get_loop()
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None
                if current_loop is owning_loop:
                    self._notify_split_queue_control(control, returned_candidate)
                else:
                    try:
                        owning_loop.call_soon_threadsafe(
                            self._notify_split_queue_control, control, returned_candidate
                        )
                    except RuntimeError:
                        continue
                notified += 1
        return notified

    def _notify_split_queue_control(
        self, control: ExecutionControl, returned_candidate: dict[str, Any] | None = None
    ) -> None:
        if (
            self._closing or self._runs.get(control.task_id) is not control
            or not control.coordinator or control.coordinator.done()
            or control.tearing_down or control.stop_event.is_set()
        ):
            return
        if returned_candidate and returned_candidate.get("source_task_id") == control.task_id:
            returned_window = str(returned_candidate.get("source_window_id") or "")
            returned_target = str(returned_candidate.get("source_target_id") or "")
            token = control.leases.get(returned_window)
            if token and returned_target:
                # Snapshot the committed handoff before another window claims the
                # row and overwrites the one per-target recovery-control state.
                self._returned_window_handoffs[(control.task_id, returned_window, token)] = returned_target
        self._wake_work_available(control)
        # Do not run a SQLite read for every window on each batch notification.
        # A failed worker has no event loop to receive this wakeup; only those
        # stopped, still leased generations need the durable handoff check.
        for profile_id, token in tuple(control.leases.items()):
            worker = control.profile_worker_tasks.get(profile_id)
            if (profile_id in control.source_recheck_profiles or profile_id in control.started_profile_ids
                    or (worker is not None and not worker.done())):
                continue
            if control.profile_states.get(profile_id, {}).get("state") not in {
                "recoverable", "stopped", "degraded"
            }:
                continue
            key = (control.task_id, profile_id, token)
            existing_closure = self._returned_window_closures.get(key)
            if existing_closure is not None and not existing_closure.done():
                continue
            # A completed idle probe can still be indexed until its done callback
            # runs. A new durable return must not lose its only notification in
            # that interval. The old callback is identity-fenced below.
            closure = asyncio.create_task(
                self._release_returned_window_if_idle(control, profile_id, token)
            )
            self._returned_window_closures[key] = closure

            def finished(task: asyncio.Task[None], *, release_key=key, run=control,
                         window=profile_id, release_token=token) -> None:
                current = self._returned_window_closures.get(release_key) is task
                if current:
                    self._returned_window_closures.pop(release_key, None)
                if task.cancelled():
                    return
                try:
                    task.result()
                except Exception as exc:
                    if current and run.leases.get(window) == release_token:
                        self._profile_state_locked(
                            run, window, state="manual_required",
                            reason="browser_close_failed",
                            message=f"退回等待后关闭窗口失败：{type(exc).__name__}: {exc}",
                        )

            closure.add_done_callback(finished)

    async def _release_returned_window_if_idle(
        self, control: ExecutionControl, profile_id: str, token: str
    ) -> None:
        async with control.queue_claim_lock:
            if (
                self._closing or self._runs.get(control.task_id) is not control
                or control.tearing_down or control.stop_event.is_set()
                or control.leases.get(profile_id) != token
                or profile_id in control.closing_profile_ids
                or profile_id in control.source_recheck_profiles
                or (control.owner_user_id, control.task_id, profile_id) in self._window_removals
            ):
                return
            worker = control.profile_worker_tasks.get(profile_id)
            if profile_id in control.started_profile_ids or (worker is not None and not worker.done()):
                return  # Its own loop can drain after the current target completes.
            state = control.profile_states.get(profile_id, {})
            if state.get("reason") == "browser_close_failed":
                return  # Preserve the explicit retry-close control.
            handoff_key = (control.task_id, profile_id, token)
            handoff_target = self._returned_window_handoffs.get(handoff_key)
            returned = self.service.has_returned_window_target(
                control.owner_user_id, control.task_id, profile_id
            )
            if not returned and handoff_target:
                current_task = self.service.get_task(control.owner_user_id, control.task_id)
                returned = any(
                    row["id"] == handoff_target
                    and row["status"] in {"pending", "running", "waiting_network",
                                          "recoverable", "failed", "stopped", "completed"}
                    and row.get("current_window_id") != profile_id
                    for row in current_task["targets"]
                )
            if not returned or self.service.has_unfinished_window_targets(
                control.owner_user_id, control.task_id, profile_id
            ):
                return
            if self.service.has_claimable_split_candidate_for_window(
                control.owner_user_id, control.task_id, profile_id,
                include_temporarily_blocked=True,
            ):
                # Return-to-waiting can allow the same window. A stopped worker
                # must be replaced or the now eligible candidate has no reader.
                spec = self.service.get_task_execution_spec(
                    control.owner_user_id, control.task_id
                )
                if self.service.has_claimable_split_candidate_for_window(
                    control.owner_user_id, control.task_id, profile_id
                ) and spec["status"] == "running":
                    self._returned_window_handoffs.pop(handoff_key, None)
                    self._spawn_window_loop(
                        control, profile_id, [], spec["modes"], spec["settings"]
                    )
                return
            # Fence Resume/Add before yielding to the provider's close thread.
            control.closing_profile_ids.add(profile_id)
        await self._close_drained_window(control, profile_id)
        self._returned_window_handoffs.pop(handoff_key, None)

    async def dismiss_completed_target(
        self, owner_user_id: str, task_id: str, target_id: str,
    ) -> dict[str, Any]:
        # Serialize with recheck/retry admission. Whichever wins must be visible
        # before the loser can acquire a lease or rewind a completed generation.
        lock = self._target_retry_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            async with self._lock:
                spec = self.service.get_task_execution_spec(owner_user_id, task_id)
                target = next((row for row in spec["targets"] if row["id"] == target_id), None)
                if target is None:
                    raise NotFoundError("Task target not found")
                control = self._runs.get(task_id)
                if control and control.owner_user_id == owner_user_id:
                    for profile_id in set(control.profile_states) | set(control.profile_completed_target_ids):
                        state = control.profile_states.get(profile_id, {})
                        bound = (state.get("current_target_id") == target_id
                                 or control.profile_completed_target_ids.get(profile_id) == target_id)
                        worker = control.profile_worker_tasks.get(profile_id)
                        if bound and (profile_id in control.leases
                                      or profile_id in control.profile_workers
                                      or (worker is not None and not worker.done())):
                            raise ConflictError("该目标的窗口仍在清理，请等待清理完成后删除任务卡",
                                                details={"reason": "collection_card_cleanup_pending",
                                                         "target_id": target_id, "profile_id": profile_id})
                return await self._await_durable_thread_call(
                    self.service.dismiss_completed_task_target, owner_user_id, task_id, target_id,
                )

    async def recheck_source(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
    ) -> dict[str, Any]:
        """One explicit, lease-fenced recheck; ordinary resume never rewinds."""
        lock = self._target_retry_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            # Cancellation must not abandon an owned paused-worker cleanup.
            return await finish_owned(self._recheck_source_owned(owner_user_id, task_id, target_id, mode))

    async def _recheck_source_owned(self, owner_user_id, task_id, target_id, mode):
        async with self._lock:
            if self._closing:
                raise ConflictError("Collection manager is shutting down")
            spec = self.service.get_task_execution_spec(owner_user_id, task_id)
            target = next((row for row in spec["targets"] if row["id"] == target_id), None)
            if target is None:
                raise NotFoundError("Task target not found")
            if target.get("collection_list_dismissed"):
                raise ConflictError("该采集任务卡已删除，不能再次复查",
                                    details={"reason": "collection_card_dismissed", "target_id": target_id})
            if spec.get("settings", {}).get("platform", "instagram") != "instagram" or mode not in {"followers", "following"}:
                raise ValidationError("该复查操作仅适用于 Instagram 粉丝或关注来源")
            control = self._runs.get(task_id)
            if (control and control.coordinator and not control.coordinator.done()
                    and control.owner_user_id == owner_user_id and not control.tearing_down
                    and not control.stop_event.is_set() and not control.fatal_status):
                for profile_id, session in tuple(control.live_source_rechecks.items()):
                    if session["target_id"] == target_id and session["mode"] == mode:
                        result = await session["request"]()
                        paused = not control.pause_event.is_set() or not control.profile_pause_events[profile_id].is_set()
                        return {"task": self.service.get_task(owner_user_id, task_id), "target": result,
                                "profile_id": profile_id, "mode": mode,
                                "status": "paused" if paused else "running", "waiting_for_task_resume": paused,
                                "parent_only": True, "waiting_for_safe_point": bool(result.get("waiting_for_safe_point"))}
            coverage = target.get("mode_coverage", {}).get(mode, {})
            if not coverage.get("discovery_finished") or not (coverage.get("unobserved_count") or 0) > 0:
                raise ConflictError("该来源没有已确认的未发现差额，或复查已开始；无需重复复查")
            control = self._runs.get(task_id)
            live = bool(control and control.coordinator and not control.coordinator.done())
            if live and (control.owner_user_id != owner_user_id or control.tearing_down
                         or control.stop_event.is_set() or control.fatal_status):
                raise ConflictError("任务正在清理，请等清理结束后再复查")
            paused = bool(spec["status"] == "paused" or (live and control.manually_paused))
            allowed = target.get("allowed_window_ids") or spec["window_ids"]
            profiles = [value for value in dict.fromkeys([
                target.get("current_window_id"), target.get("preferred_window_id"), *spec["window_ids"]
            ]) if value and value in spec["window_ids"] and value in allowed]
            active_profile = None
            if live:
                for profile_id in control.leases:
                    state = control.profile_states.get(profile_id, {})
                    worker_task = control.profile_worker_tasks.get(profile_id)
                    if (state.get("current_target_id") == target_id
                            and worker_task is not None and not worker_task.done()):
                        local_pause = control.profile_pause_events.get(profile_id)
                        if not paused and (local_pause is None or local_pause.is_set()):
                            raise ConflictError("该来源正在切换或清理，尚未到安全复查点，请稍后再试")
                        active_profile = profile_id
                        profiles = [profile_id]
                        break
                if target_id in control.enqueued_target_ids and active_profile is None:
                    raise ConflictError("该目标已在执行队列中，请等目标安全暂停后再复查")
            elif target["status"] in {"running", "waiting_network"}:
                raise ConflictError("请先停止并等待旧任务清理完成后再复查")
            selected = None
            token = None
            acquired = False
            registered = False
            cleanup_failed = False
            if not live:
                with self._activity_lock:
                    self._starting_task_ids.add(task_id)
                    self._active_lease_task_ids.add(task_id)
                registered = True
            try:
                for profile_id in profiles:
                    if (owner_user_id, task_id, profile_id) in self._window_removals:
                        continue
                    if live and profile_id in control.leases:
                        worker_task = control.profile_worker_tasks.get(profile_id)
                        state = control.profile_states.get(profile_id, {})
                        settled_idle = (state.get("state") == "idle" and not state.get("current_target_id")
                                        and not control.queue_claim_lock.locked()
                                        and profile_id not in control.manual_mode_active)
                        if (profile_id in control.closing_profile_ids or profile_id in control.source_recheck_profiles
                                or state.get("reason") == "browser_close_failed"
                                or ((worker_task is None or worker_task.done()) and profile_id in control.profile_workers)
                                or (worker_task is not None and not worker_task.done()
                                    and profile_id != active_profile and not settled_idle)):
                            continue
                        selected, token = profile_id, control.leases[profile_id]
                        break
                    try:
                        token = await self.service.acquire_browser_lease_async(
                            owner_user_id, profile_id, operation_type="collection", entity_id=task_id)
                    except (ConflictError, ValidationError):
                        continue
                    selected, acquired = profile_id, True
                    break
                if selected is None:
                    raise ConflictError("没有可用的原任务窗口；请先释放一个该目标允许使用的 Instagram 窗口，再复查",
                                        details={"reason": "source_recheck_no_idle_window"})
                if live:
                    assert control is not None
                    control.source_recheck_profiles.add(selected)
                    worker_task = control.profile_worker_tasks.get(selected)
                    if worker_task is not None and not worker_task.done():
                        local_stop = control.profile_stop_events.get(selected)
                        if local_stop is not None:
                            local_stop.set()
                        worker_task.cancel()
                        await asyncio.gather(worker_task, return_exceptions=True)
                        # The old generation removes its driver only after all
                        # cleanup succeeds. A failed cleanup keeps the lease and
                        # admission fence, leaving the original checkpoint intact.
                        if selected in control.profile_workers:
                            cleanup_failed = True
                            self._profile_state_locked(control, selected, state="manual_required",
                                reason="source_recheck_cleanup_failed", message="页面清理未成功，窗口仍由原任务占用；请停止该窗口重试清理")
                            raise ConflictError("窗口清理尚未成功，已保留占用和原检查点；请停止该窗口后重试")
                    if (self._runs.get(task_id) is not control or control.tearing_down
                            or control.stop_event.is_set()):
                        raise ConflictError("任务已停止或正在清理，未重置来源")
                    async with control.queue_claim_lock:
                        target = self.service.recheck_task_source(
                            owner_user_id, task_id, target_id, mode,
                            profile_id=selected, lease_token=token)
                        target["allowed_window_ids"] = [selected]
                        control.leases[selected] = token
                        control.enqueued_target_ids.add(target_id)
                        control.source_recheck_profiles.discard(selected)
                        self._spawn_window_loop(control, selected, [target], spec["modes"],
                                                {**spec["settings"], "live_queue_enabled": False})
                        self._wake_work_available(control)
                        acquired = False
                else:
                    target = self.service.recheck_task_source(
                        owner_user_id, task_id, target_id, mode,
                        profile_id=selected, lease_token=token)
                    pause_event = asyncio.Event()
                    if not paused:
                        pause_event.set()
                    control = ExecutionControl(
                        owner_user_id=owner_user_id, task_id=task_id,
                        pause_event=pause_event, stop_event=asyncio.Event(), leases={selected: token},
                        target_queue=asyncio.Queue(), manually_paused=paused, source_recheck_target_id=target_id,
                        network_retry_gate=asyncio.Semaphore(self.network_retry_concurrency),
                    )
                    self.service.set_task_runtime_status(owner_user_id, task_id, "paused" if paused else "running")
                    control.coordinator = asyncio.create_task(self._run(control), name=f"collection:{task_id}")
                    with self._activity_lock:
                        self._runs[task_id] = control
                        self._starting_task_ids.discard(task_id)
                    registered = False
                    acquired = False
                return {"task": self.service.get_task(owner_user_id, task_id), "target": target,
                        "profile_id": selected, "mode": mode,
                        "status": "paused" if paused else "running", "waiting_for_task_resume": paused}
            finally:
                if live and selected and not cleanup_failed:
                    control.source_recheck_profiles.discard(selected)
                    control.workers_changed.set()
                if acquired and selected and token:
                    self.service.release_browser_lease(selected, token)
                if registered:
                    with self._activity_lock:
                        self._starting_task_ids.discard(task_id)
                        self._active_lease_task_ids.discard(task_id)

    async def retry_target(
        self, owner_user_id: str, task_id: str, target_id: str
    ) -> dict[str, Any]:
        retry_lock = self._target_retry_locks.setdefault(task_id, asyncio.Lock())
        async with retry_lock:
            while True:
                control = self._runs.get(task_id)
                live = bool(
                    control
                    and control.owner_user_id == owner_user_id
                    and control.coordinator
                    and not control.coordinator.done()
                )
                if live and control is not None:
                    coordinator = control.coordinator
                    shutting_down = bool(
                        control.fatal_status
                        or control.stop_event.is_set()
                        or control.tearing_down
                    )
                    if shutting_down:
                        # The old worker can still write RECOVERABLE while unwinding.
                        # Wait before changing the durable row or publishing a new run.
                        assert coordinator is not None
                        await asyncio.shield(coordinator)
                        continue

                    # Target completion removes enqueued_target_ids under this same
                    # lock. Therefore a retry cannot observe the old ownership marker,
                    # skip enqueueing, and then have the worker discard it afterwards.
                    async with control.queue_claim_lock:
                        if (
                            control.fatal_status
                            or control.stop_event.is_set()
                            or control.tearing_down
                        ):
                            coordinator = control.coordinator
                        else:
                            target = self.service.retry_task_target(
                                owner_user_id, task_id, target_id
                            )
                            if target["id"] not in control.enqueued_target_ids:
                                control.enqueued_target_ids.add(target["id"])
                                control.target_queue.put_nowait(target)
                            self._wake_work_available(control)
                            return {
                                "target": target,
                                "task": self.service.get_task(owner_user_id, task_id),
                            }
                    if coordinator is not None:
                        await asyncio.shield(coordinator)
                    continue

                # No live coordinator owns this task. Requeue only after any previous
                # teardown has completed, then create a real replacement coordinator.
                target = self.service.retry_task_target(
                    owner_user_id, task_id, target_id
                )
                try:
                    await self.start(owner_user_id, task_id)
                except ConflictError:
                    # A concurrent Resume/Start can win the manager lock after the
                    # durable retry. Hand the target to that healthy run; if it is
                    # already tearing down, loop and retry after its final write.
                    racing = self._runs.get(task_id)
                    if not (
                        racing
                        and racing.owner_user_id == owner_user_id
                        and racing.coordinator
                        and not racing.coordinator.done()
                    ):
                        raise
                    async with racing.queue_claim_lock:
                        if (
                            racing.fatal_status
                            or racing.stop_event.is_set()
                            or racing.tearing_down
                        ):
                            coordinator = racing.coordinator
                        else:
                            if target["id"] not in racing.enqueued_target_ids:
                                racing.enqueued_target_ids.add(target["id"])
                                racing.target_queue.put_nowait(target)
                            self._wake_work_available(racing)
                            return {
                                "target": target,
                                "task": self.service.get_task(owner_user_id, task_id),
                            }
                    assert coordinator is not None
                    await asyncio.shield(coordinator)
                    continue
                return {
                    "target": target,
                    "task": self.service.get_task(owner_user_id, task_id),
                }

    async def delete_target(
        self, owner_user_id: str, task_id: str, target_id: str
    ) -> None:
        """Delete one target atomically against an in-memory worker queue claim."""
        control = self._runs.get(task_id)
        if control and control.owner_user_id == owner_user_id and control.coordinator and not control.coordinator.done():
            async with control.queue_claim_lock:
                self.service.delete_task_target(owner_user_id, task_id, target_id)
                control.deleted_target_ids.add(target_id)
                control.enqueued_target_ids.discard(target_id)
            return
        self.service.delete_task_target(owner_user_id, task_id, target_id)

    async def shutdown(self) -> None:
        self._closing = True
        async with self._lock:
            controls = list(self._runs.values())
        for control in controls:
            for profile_id in tuple(control.manual_requests):
                self._revoke_manual_control(control, profile_id)
            control.fatal_status = "recoverable"
            control.fatal_error = "Application shutdown interrupted collection"
            control.close_profiles_on_exit = True
            control.tearing_down = True
            control.stop_event.set()
            control.pause_event.set()
            self._wake_work_available(control)
            for worker_task in tuple(control.worker_tasks):
                if not worker_task.cancelling():
                    worker_task.cancel()
        await asyncio.gather(
            *(control.coordinator for control in controls if control.coordinator),
            return_exceptions=True,
        )

    def _owned_control(self, owner_user_id: str, task_id: str) -> ExecutionControl:
        control = self._runs.get(task_id)
        if not control or control.owner_user_id != owner_user_id or not control.coordinator or control.coordinator.done():
            raise NotFoundError("Active task execution not found")
        return control

    async def _run(self, control: ExecutionControl) -> None:
        # Start lease protection before model warm-up or any other potentially slow
        # preparation. The heartbeat renews once immediately, so a 90-second model
        # preparation can never consume the whole 90-second lease generation.
        heartbeat: asyncio.Task[None] | None = asyncio.create_task(
            self._heartbeat(control), name=f"lease-heartbeat:{control.task_id}"
        )
        auto_close_on_exit = False
        try:
            spec = self.service.get_task_execution_spec(control.owner_user_id, control.task_id)
            if control.source_recheck_target_id:
                spec = {**spec, "targets": [target for target in spec["targets"]
                                           if target["id"] == control.source_recheck_target_id],
                        "settings": {**spec["settings"], "live_queue_enabled": False}}
            # A naturally completed task closes any remaining assigned windows,
            # including live-queue windows that were not already drained.
            auto_close_on_exit = True
            # Gender classification is retired. Collection never warms its model,
            # even when a legacy execution spec still carries enabled switches.
            if control.stop_event.is_set():
                # A first heartbeat can expose a stale/fenced lease while model
                # warm-up is still running. Publish the durable failure before the
                # common cleanup path releases ownership.
                self.service.finalize_task_runtime_status(
                    control.owner_user_id,
                    control.task_id,
                    control.fatal_status or "recoverable",
                    error=control.fatal_error or "Browser lease was lost",
                )
                return
            queue = control.target_queue
            preferred_by_window: dict[str, list[dict[str, Any]]] = {
                profile_id: [] for profile_id in control.leases
            }
            for target in spec["targets"]:
                if target["status"] == "completed":
                    continue
                if target.get("manual_recovery_required"):
                    continue
                if target["id"] in control.enqueued_target_ids:
                    continue
                if (
                    not spec.get("settings", {}).get("live_queue_enabled")
                    and target["status"] in {"recoverable", "failed", "stopped"}
                ):
                    # A legacy finite task resumes its old target directly. If
                    # that failure was returned to waiting, retry_task_target
                    # atomically attaches its queue row to this same target
                    # before any browser window receives the payload.
                    target = self.service.retry_task_target(
                        control.owner_user_id, control.task_id, target["id"]
                    )
                preferred_window = target.get("preferred_window_id")
                if (
                    spec.get("assignment_mode") == "manual"
                    and preferred_window in preferred_by_window
                ):
                    control.enqueued_target_ids.add(target["id"])
                    preferred_by_window[preferred_window].append(target)
                else:
                    control.enqueued_target_ids.add(target["id"])
                    queue.put_nowait(target)
            self._wake_work_available(control)
            # An add-windows request can arrive immediately after start(), before this
            # coordinator gets its first event-loop turn. Reuse workers already spawned
            # by that request instead of creating a second loop for the same profile.
            for profile_id in list(control.leases):
                if profile_id in control.started_profile_ids:
                    continue
                self._spawn_window_loop(
                    control,
                    profile_id,
                    preferred_by_window[profile_id],
                    spec["modes"],
                    spec["settings"],
                )
            await self._supervise_worker_tasks(
                control,
                keep_alive=bool(spec.get("settings", {}).get("live_queue_enabled")),
            )
            if control.stop_event.is_set() or control.fatal_status:
                self._mark_running_targets(
                    control.owner_user_id, control.task_id, "recoverable"
                )
            if control.terminal_override:
                return
            if control.stop_event.is_set() and not control.fatal_status:
                return
            if control.fatal_status:
                self.service.finalize_task_runtime_status(
                    control.owner_user_id,
                    control.task_id,
                    control.fatal_status,
                    error=control.fatal_error,
                )
            else:
                try:
                    self.service.finalize_task_runtime_status(
                        control.owner_user_id, control.task_id, "completed"
                    )
                except ConflictError as exc:
                    # A worker can exit without an exception while a durable target is
                    # still unfinished (for example, an externally deleted queue claim
                    # racing teardown). Never publish the contradictory COMPLETED task;
                    # retain every unfinished row as one resumable generation instead.
                    control.fatal_status = "recoverable"
                    control.fatal_error = (
                        "任务仍有未完成目标，已保留检查点等待继续采集："
                        f"{exc.message}"
                    )
                    self.service.finalize_task_runtime_status(
                        control.owner_user_id,
                        control.task_id,
                        "recoverable",
                        error=control.fatal_error,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not control.stop_event.is_set() and not control.terminal_override:
                control.fatal_status = "failed"
                control.fatal_error = f"{type(exc).__name__}: {exc}"
                self.service.finalize_task_runtime_status(
                    control.owner_user_id,
                    control.task_id,
                    "failed",
                    error=control.fatal_error,
                )
        finally:
            await finish_owned(self._finalize_run(control, heartbeat, auto_close_on_exit))

    async def _finalize_run(
        self, control: ExecutionControl, heartbeat: asyncio.Task[None] | None,
        auto_close_on_exit: bool,
    ) -> None:
        """Keep the cleanup task and all window leases alive through cancellation."""
        # Close the worker-admission gate before yielding. add_windows performs
        # its append synchronously under the manager lock, so every worker that
        # won the race before this assignment is now visible in worker_tasks and
        # no later append can pass the teardown fence.
        control.tearing_down = True
        if control.stop_event.is_set() or control.fatal_status:
            self._mark_running_targets(
                control.owner_user_id, control.task_id, "recoverable"
            )
        # initial_workers is only the coordinator's startup snapshot. Live queue
        # windows may have been appended after that gather began. Drain the full
        # evolving set before closing a profile, releasing a lease, or removing
        # _runs; otherwise another task can acquire a still-active window. Keep
        # renewing the leases throughout worker disconnect and Local API close:
        # hundreds of serialized close requests can legitimately outlive the
        # 90-second lease TTL.
        await self._cancel_and_gather_worker_tasks(control)
        # Explicit cleanup retries can outlive their original worker generation.
        # Admission is fenced by tearing_down above; retain the same lease until
        # each already-admitted driver cleanup really settles.
        source_cleanups = tuple(control.source_recheck_cleanup_tasks.values())
        if source_cleanups:
            await asyncio.shield(asyncio.gather(*source_cleanups, return_exceptions=True))
        # A previously failed recheck cleanup has no live worker task left to
        # gather. Task-wide Stop/Shutdown must retry that retained driver too,
        # retaining heartbeats and leases throughout any cleanup failure.
        for profile_id in tuple(control.source_recheck_profiles):
            retained = control.profile_workers.get(profile_id)
            if retained is None:
                control.source_recheck_profiles.discard(profile_id)
                continue
            while True:
                try:
                    await retained.disconnect()
                    wait_for_cleanup = getattr(retained, "wait_for_cleanup", None)
                    if callable(wait_for_cleanup):
                        await wait_for_cleanup()
                    break
                except Exception:
                    self._profile_state_locked(control, profile_id, state="manual_required",
                        reason="source_recheck_cleanup_failed", message="页面清理仍未成功，原任务继续保留窗口占用")
                    await asyncio.sleep(.5)
            if control.profile_workers.get(profile_id) is retained:
                control.profile_workers.pop(profile_id, None)
            control.source_recheck_profiles.discard(profile_id)
            control.cleaned_profile_ids.add(profile_id)
        while True:
            # A returned failed window can be closing in a separate, token-fenced
            # task after its worker exited. Never release its lease while the
            # provider's close thread may still be addressing that profile.
            pending_returns = tuple(
                task for (task_id, _, _), task in self._returned_window_closures.items()
                if task_id == control.task_id and not task.done()
            )
            if not pending_returns:
                break
            await asyncio.shield(asyncio.gather(*pending_returns, return_exceptions=True))
        pending_closes = tuple(control.profile_close_tasks.values())
        if pending_closes:
            await asyncio.shield(asyncio.gather(*pending_closes, return_exceptions=True))
        # A collection error must never close the user's BitBrowser window.
        # Keep the visible page available for inspection/recovery; only an
        # explicit close/stop request, or a clean non-live task completion, may
        # invoke BitBrowser's /browser/close endpoint.
        clean_auto_close = False
        if auto_close_on_exit and control.fatal_status is None and not control.stop_event.is_set():
            try:
                clean_auto_close = self.service.get_task(control.owner_user_id, control.task_id)['status'] == 'completed'
            except Exception:
                # Database failures must not skip lease and heartbeat cleanup.
                pass
        if control.close_profiles_on_exit or clean_auto_close:
            await self._close_control_profiles(control)
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        for profile_id, token in control.leases.items():
            self.service.release_browser_lease(
                profile_id, token, completed_cleanup=profile_id in control.cleaned_profile_ids,
            )
        for key in tuple(self._returned_window_handoffs):
            if key[0] == control.task_id:
                self._returned_window_handoffs.pop(key, None)
        with self._activity_lock:
            self._active_lease_task_ids.discard(control.task_id)
            self._runs.pop(control.task_id, None)

    def _spawn_window_loop(
        self,
        control: ExecutionControl,
        profile_id: str,
        preferred_targets: list[dict[str, Any]],
        modes: list[str],
        settings: dict[str, Any],
    ) -> asyncio.Task[None]:
        if (control.tearing_down or profile_id in control.closing_profile_ids
                or profile_id in control.source_recheck_profiles):
            raise ConflictError(
                "Task execution is shutting down",
                details={"profile_id": profile_id, "entity_id": control.task_id},
            )
        if profile_id in control.started_profile_ids:
            raise ConflictError(
                "BitBrowser window worker is already running",
                details={"profile_id": profile_id, "entity_id": control.task_id},
            )
        control.cleaned_profile_ids.discard(profile_id)
        control.started_profile_ids.add(profile_id)
        local_pause = control.profile_pause_events.get(profile_id)
        if local_pause is None:
            local_pause = asyncio.Event()
            control.profile_pause_events[profile_id] = local_pause
        local_pause.set()
        local_stop = asyncio.Event()
        control.profile_stop_events[profile_id] = local_stop
        local_work = control.profile_work_events.get(profile_id)
        if local_work is None:
            local_work = asyncio.Event()
            control.profile_work_events[profile_id] = local_work
        # A dynamically added window must inspect the already-durable split queue
        # immediately; it cannot depend on a second enqueue notification.
        local_work.set()
        control.manual_events[profile_id] = asyncio.Event()
        async def manual_checkpoint():
            await self._manual_pause_checkpoint(control, profile_id)
        control.manual_checkpoints[profile_id] = manual_checkpoint
        window_control = _WindowControlView(
            control, profile_id, local_pause, local_stop
        )
        self._profile_state_locked(
            control,
            profile_id,
            state="starting",
        )
        worker_task = asyncio.create_task(
            self._window_loop(
                window_control,
                profile_id,
                preferred_targets,
                control.target_queue,
                modes,
                settings,
            ),
            name=f"collection:{control.task_id}:{profile_id}",
        )
        control.worker_tasks.append(worker_task)
        control.profile_worker_tasks[profile_id] = worker_task
        control.workers_changed.set()

        def worker_finished(completed: asyncio.Task[None]) -> None:
            cancelled = completed.cancelled()
            error: BaseException | None = None
            if not cancelled:
                # Retrieving the exception here prevents an unobserved Task warning.
                # The coordinator receives the same object through worker_results and
                # applies the profile-local failure policy.
                try:
                    error = completed.exception()
                except asyncio.CancelledError:
                    cancelled = True
            if control.profile_worker_tasks.get(profile_id) is completed:
                control.profile_worker_tasks.pop(profile_id, None)
                control.started_profile_ids.discard(profile_id)
                if profile_id not in control.leases:
                    # A drained generation released only after disconnect/close.
                    # Keep its admission fence until this exact worker is done.
                    control.closing_profile_ids.discard(profile_id)
                    control.profile_pause_events.pop(profile_id, None)
                    control.profile_stop_events.pop(profile_id, None)
                    control.profile_work_events.pop(profile_id, None)
            try:
                control.worker_tasks.remove(completed)
            except ValueError:
                pass
            control.worker_results.put_nowait((profile_id, error, cancelled))
            control.workers_changed.set()
            # A return-to-waiting notification may arrive while disconnect is
            # still in flight. Recheck after this exact worker has exited so its
            # failed-but-retained lease cannot remain permanently occupied.
            self._notify_split_queue_control(control)

        worker_task.add_done_callback(worker_finished)
        return worker_task

    async def _supervise_worker_tasks(
        self, control: ExecutionControl, *, keep_alive: bool = False
    ) -> None:
        """Supervise the evolving worker set until its last live generation exits.

        ``add_windows`` and ``resume_window`` can append generations after startup.
        Waiting on the startup snapshot used to finalize the task and cancel those
        later workers as soon as the original generation ended. A change event plus
        an active-only list gives the coordinator one durable ownership boundary.
        """

        while True:
            control.workers_changed.clear()
            while True:
                try:
                    profile_id, error, cancelled = (
                        control.worker_results.get_nowait()
                    )
                except asyncio.QueueEmpty:
                    break
                try:
                    if (
                        error is not None
                        and not cancelled
                        and not control.stop_event.is_set()
                        and control.fatal_status is None
                        and profile_id not in control.source_recheck_profiles
                    ):
                        # An exception escaping _window_loop is still scoped to its
                        # BitBrowser profile. Its claimed target is converged by the
                        # loop's finally block; healthy sibling generations continue.
                        async with control.network_state_lock:
                            self._profile_state_locked(
                                control,
                                profile_id,
                                state="recoverable",
                                reason="window_execution_failed",
                                message=f"{type(error).__name__}: {error}",
                            )
                finally:
                    control.worker_results.task_done()

            if control.stop_event.is_set() or control.fatal_status is not None:
                return
            if not control.worker_tasks:
                # Let callbacks for a generation spawned in the same loop turn
                # publish before making the terminal decision.
                await asyncio.sleep(0)
                if not control.worker_tasks:
                    pending_close = any(
                        control.profile_states.get(profile_id, {}).get("reason")
                        == "browser_close_failed"
                        for profile_id in control.leases
                    )
                    if not control.leases or (not keep_alive and not pending_close and not control.source_recheck_profiles):
                        return
                    # Finite batches also retain ownership when a provider has
                    # not acknowledged automatic close. Resume retries only close;
                    # it must not recollect or admit another task to that window.
                    # A live queue with all windows locally stopped remains owned by
                    # its coordinator, allowing Resume/Add/Delete to act on the same
                    # lease generation. It ends only on an explicit task-wide stop or
                    # wakes when a replacement generation is spawned.
                    change_wait = asyncio.create_task(
                        control.workers_changed.wait()
                    )
                    stop_wait = asyncio.create_task(control.stop_event.wait())
                    done, pending = await asyncio.wait(
                        {change_wait, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for pending_task in pending:
                        pending_task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if stop_wait in done and control.stop_event.is_set():
                        return
                    continue
                continue
            await control.workers_changed.wait()

    @staticmethod
    async def _cancel_and_gather_worker_tasks(control: ExecutionControl) -> None:
        """Cancel and consume every worker, including workers appended mid-run."""
        current = asyncio.current_task()
        consumed: set[asyncio.Task[None]] = set()
        while True:
            snapshot = tuple(
                worker_task
                for worker_task in control.worker_tasks
                if worker_task is not current
                and worker_task is not control.coordinator
                and worker_task not in consumed
            )
            if not snapshot:
                return
            for worker_task in snapshot:
                if not worker_task.done() and not worker_task.cancelling():
                    worker_task.cancel()
            await asyncio.gather(*snapshot, return_exceptions=True)
            consumed.update(snapshot)
            # Give a callback already queued before the teardown fence one turn to
            # publish its task. The loop then drains that final generation as well.
            await asyncio.sleep(0)

    async def _close_profile_once(
        self, control: ExecutionControl, profile_id: str, token: str,
    ) -> bool:
        key = (profile_id, token)
        closing = control.profile_close_tasks.get(key)
        if closing is None:
            control.closing_profile_ids.add(profile_id)
            closing = asyncio.create_task(self._close_profiles({profile_id: token}))
            control.profile_close_tasks[key] = closing
        return await finish_owned(closing)

    async def _close_drained_window(
        self, control: ExecutionControl, profile_id: str,
    ) -> bool:
        """Close/release only this fully drained worker's lease generation."""
        token = control.leases.get(profile_id)
        if token is None:
            return False
        closed = await self._close_profile_once(control, profile_id, token)
        if not closed:
            # A requested/deferred close is not a completed close. Keep heartbeat
            # and admission fencing, and expose a retryable window-only card.
            control.profile_close_tasks.pop((profile_id, token), None)
            self._profile_state_locked(
                control, profile_id, state="manual_required",
                reason="browser_close_failed",
                message="当前采集已结束，但窗口自动关闭失败；占用已保留，请检查浏览器服务后重试关闭",
            )
            return False
        if control.leases.get(profile_id) != token:
            return False
        # Preserve durable window assignments for an explicit retry of a pending
        # source added while close was in flight. History does not grant occupancy;
        # only this exact token does. A later Start/Add must acquire a new lease.
        async def release_and_publish() -> bool:
            await asyncio.to_thread(
                self.service.release_browser_lease, profile_id, token,
                completed_cleanup=profile_id in control.cleaned_profile_ids,
            )
            # Only retire the generation whose durable release just settled.
            if control.leases.get(profile_id) != token:
                return False
            control.leases.pop(profile_id, None)
            control.profile_completed_target_ids.pop(profile_id, None)
            self._profile_state_locked(
                control, profile_id, state="closed", reason="collection_queue_drained",
            )
            # Retry has no live worker callback left to remove the admission fence.
            worker_task = control.profile_worker_tasks.get(profile_id)
            if worker_task is None or worker_task.done():
                control.closing_profile_ids.discard(profile_id)
            control.workers_changed.set()
            return True

        return await finish_owned(release_and_publish())

    async def _close_control_profiles(self, control: ExecutionControl) -> None:
        if control.profiles_closed:
            return
        snapshot = tuple(control.leases.items())
        control.closing_profile_ids.update(profile_id for profile_id, _ in snapshot)
        await self._close_profiles_until_confirmed(dict(snapshot), control=control)
        control.profiles_closed = True

    async def _close_profiles_until_confirmed(
        self, leases: dict[str, str], *, control: ExecutionControl | None = None,
    ) -> None:
        """A negative/deferred provider acknowledgement cannot release a lease."""
        pending = dict(leases)
        delay = .25
        while pending:
            items = tuple(pending.items())
            results = await asyncio.gather(*(
                self._close_profile_once(control, profile_id, token) if control is not None
                else self._close_profiles({profile_id: token})
                for profile_id, token in items
            ))
            for (profile_id, token), closed in zip(items, results):
                if closed:
                    pending.pop(profile_id, None)
                    continue
                try:
                    if not await asyncio.to_thread(self._owns_profile_lease, profile_id, token):
                        # This generation no longer has authority over the window.
                        pending.pop(profile_id, None)
                        continue
                    if control is None:
                        await asyncio.to_thread(self.service.renew_browser_lease, profile_id, token)
                except Exception:
                    pass  # Uncertain storage state is not proof of release.
                if control is not None:
                    closing = control.profile_close_tasks.get((profile_id, token))
                    if (closing is not None and closing.done() and not closing.cancelled()
                            and closing.exception() is None and closing.result() is False):
                        control.profile_close_tasks.pop((profile_id, token), None)
                    self._profile_state_locked(
                        control, profile_id, state='stopped', reason='browser_close_failed',
                        message='窗口关闭尚未确认，占用已保留，正在自动重试关闭',
                    )
            if pending:
                await asyncio.sleep(delay)
                delay = min(2.0, delay * 2)

    def _owns_profile_lease(self, profile_id: str, lease_token: str) -> bool:
        with self.service.database.read() as connection:
            row = connection.execute(
                "SELECT lease_token FROM browser_operation_leases WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        return row is not None and row["lease_token"] == lease_token

    def _close_owned_profile(self, profile_id: str, lease_token: str) -> bool:
        if self._owns_profile_lease(profile_id, lease_token):
            result = self.bitbrowser.close_profile(profile_id)
            # BrowserHub routes native/embedded/legacy by profile id. Legacy can
            # acknowledge a deferred close without actually ending its browser.
            # None remains compatible with older provider adapters/test doubles.
            return close_acknowledged(result)
        return False

    async def _close_profiles(self, leases: dict[str, str]) -> bool:
        """Close only token-matching generations; retain ownership through I/O."""
        async def close_one(profile_id: str, token: str) -> bool:
            try:
                return await asyncio.to_thread(self._close_owned_profile, profile_id, token)
            except Exception:
                return False

        results = await asyncio.gather(
            *(close_one(profile_id, token) for profile_id, token in tuple(leases.items())),
            return_exceptions=True,
        )
        return all(result is True for result in results)

    async def _renew_all_leases(self, control: ExecutionControl) -> None:
        """Renew one snapshot; retry transient storage contention per profile."""

        # add_windows may publish another lease during this pass. Snapshot the
        # dictionary so existing generations are all renewed consistently and the
        # new, freshly-created generation joins on the next pass.
        for profile_id, token in tuple(control.leases.items()):
            last_error: Exception | None = None
            for attempt in range(4):
                if control.leases.get(profile_id) != token:
                    # An intentionally deleted window may still be in this heartbeat
                    # snapshot. Its absence must not stop healthy sibling windows.
                    last_error = None
                    break
                try:
                    # Cancelling the heartbeat does not cancel its SQLite thread.
                    # Join the actual I/O before _finalize_run can retire this
                    # generation or a caller can close/remove its database.
                    await finish_owned(asyncio.to_thread(
                        self.service.renew_browser_lease, profile_id, token
                    ))
                    last_error = None
                    break
                except Exception as exc:
                    if control.leases.get(profile_id) != token:
                        last_error = None
                        break
                    last_error = exc
                    if attempt < 3:
                        await asyncio.sleep(0.75 * (attempt + 1))
            if last_error is not None:
                raise last_error

    async def _heartbeat(self, control: ExecutionControl) -> None:
        try:
            # The coordinator owns this task and explicitly cancels it only after all
            # workers have disconnected and every requested profile close has
            # returned. ``stop_event`` is a worker signal, not a lease-release signal.
            while True:
                # Renew first. Sleeping before the first renewal made long local-model
                # warm-up consume most of a fresh lease before protection began.
                await self._renew_all_leases(control)
                await asyncio.sleep(self.lease_heartbeat_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Losing a lease is a fencing failure: stop every window immediately so
            # another owner can never operate the same BitBrowser profile concurrently.
            control.fatal_status = "recoverable"
            control.fatal_error = f"Browser lease was lost: {type(exc).__name__}: {exc}"
            control.stop_event.set()
            control.pause_event.set()
            for worker_task in list(control.worker_tasks):
                worker_task.cancel()

    @staticmethod
    def _utc_iso(value: datetime | None = None) -> str:
        return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    @staticmethod
    def _is_auth_required_error(exc: BaseException) -> bool:
        return isinstance(exc, BitBrowserAuthRequiredError) or getattr(
            exc, "code", None
        ) == BitBrowserAuthRequiredError.code

    @staticmethod
    def _is_profile_intervention_error_code(code: str) -> bool:
        """Return errors scoped to one Instagram login/proxy browser profile.

        These conditions must pause only their owning profile. Treating one Instagram
        challenge as a process-wide fatal error would stop and close every healthy
        collection window. Location-detail cooldowns are automatically retryable
        page conditions and therefore do not belong to this manual-only set.
        """

        return code in {
            "instagram_login_required",
            "instagram_challenge",
            "instagram_rate_limited",
            "instagram_action_blocked",
            "instagram_page_recovery_exhausted",
        }

    @classmethod
    def _is_profile_intervention_error(cls, exc: BaseException) -> bool:
        if getattr(exc, "code", "") == "instagram_page_recovery_exhausted":
            return not cls._is_instagram_surface_retry_error(exc)
        return cls._is_profile_intervention_error_code(
            str(getattr(exc, "code", "") or "")
        )

    @staticmethod
    def _is_candidate_page_failure(exc: BaseException) -> bool:
        # Only a confirmed page-read failure is local to one screening candidate.
        # Auth, challenge, rate-limit, and transport failures retain their
        # profile-level recovery path rather than becoming candidate exclusions.
        if not isinstance(exc, WorkerExecutionError):
            return False
        if exc.code == "instagram_content_not_visible":
            # A profile may become visible on retry. Keep this candidate pending
            # while sibling accounts continue, rather than recording an unread
            # account as globally seen with no review row.
            return True
        return exc.code == "instagram_page_recovery_exhausted" and exc.details.get("original_reason") in {
            "instagram_profile_not_ready", "instagram_profile_wrong_target",
            "instagram_profile_dom_unrecognized", "instagram_profile_temporarily_unavailable",
            "instagram_location_load_failed", "instagram_location_temporarily_unavailable",
        }

    @classmethod
    def _is_parent_profile_failure(cls, exc: BaseException) -> bool:
        reasons = {"cdp_profile_verification_failed", "worker_not_connected", "browser_context_missing"}
        details = getattr(exc, "details", {})
        return (cls._is_profile_intervention_error(exc) or cls._is_auth_required_error(exc)
                or getattr(exc, "code", "") in reasons or details.get("reason") in reasons
                or details.get("original_reason") in reasons)

    @classmethod
    def _is_isolated_screening_page_failure(cls, exc: BaseException) -> bool:
        """Only known child-local page failures leave parent navigation healthy."""
        if cls._is_candidate_page_failure(exc):
            return True
        if not isinstance(exc, WorkerExecutionError):
            return False
        reason = exc.details.get("original_reason") if exc.code == "instagram_page_recovery_exhausted" else exc.code
        return reason in {
            "browser_window_surface_unstable", "instagram_profile_not_ready",
            "instagram_profile_wrong_target", "instagram_profile_dom_unrecognized",
            "instagram_profile_temporarily_unavailable", "instagram_location_load_failed",
            "instagram_location_temporarily_unavailable",
        }

    @staticmethod
    def _is_relationship_list_incomplete_error(exc: BaseException) -> bool:
        """Return a loaded Instagram list that stalled before its visible total.

        This is a page/list-progress condition, not evidence that BitBrowser's
        localhost API, CDP socket, proxy, or Internet connection disappeared.
        """

        return isinstance(exc, WorkerExecutionError) and exc.code in {
            "instagram_followers_list_incomplete",
            "instagram_following_list_incomplete",
            "instagram_followers_list_not_rendered",
            "instagram_following_list_not_rendered",
            # The hovered row may have been recycled while the virtual list
            # repainted. The verified prefix is durable, but discovery is not
            # finished: restart this source page and replay the unverified row.
            "instagram_hover_card_unavailable",
        }

    @staticmethod
    def _is_instagram_surface_retry_error(exc: BaseException) -> bool:
        """Return page-readiness failures that must not tear down healthy CDP.

        BitBrowser and its CDP socket can remain fully healthy while Instagram shows
        a loading shell, briefly redirects to the wrong profile, or returns its own
        temporary-unavailable surface. Reconnecting the browser for those states
        creates a disconnect loop and loses useful page/session state.
        """

        if isinstance(exc, WorkerExecutionError) and exc.code == "instagram_page_recovery_exhausted":
            # The worker's budget bounds one attempt, not the whole task lifetime.
            # Only known page/read transport causes may retry automatically. An
            # arbitrary auto_retry flag cannot override login/challenge/rate guards.
            return exc.details.get("original_reason") in {
                "instagram_network_unavailable",
                "worker_not_connected",
                "browser_context_missing",
                "browser_operations_pending",
                "screening_slots_busy",
                "parallel_screening_page_create_timeout",
                "browser_window_surface_unstable",
                "instagram_profile_not_ready",
                "instagram_profile_wrong_target",
                "instagram_profile_dom_unrecognized",
                "instagram_profile_temporarily_unavailable",
                "instagram_location_load_failed",
                "instagram_location_temporarily_unavailable",
                "instagram_followers_list_incomplete",
                "instagram_following_list_incomplete",
                "instagram_followers_list_not_rendered",
                "instagram_following_list_not_rendered",
                "instagram_hover_card_unavailable",
                "instagram_post_likers_list_not_rendered",
                "instagram_post_grid_not_rendered",
            }
        return isinstance(exc, WorkerExecutionError) and exc.code in {
            "instagram_profile_not_ready",
            "instagram_profile_wrong_target",
            "instagram_profile_temporarily_unavailable",
            "instagram_location_temporarily_unavailable",
            "instagram_relationship_list_no_progress",
            "instagram_surface_no_progress",
            "instagram_hover_card_unavailable",
        }

    @classmethod
    def _is_network_error(cls, exc: BaseException) -> bool:
        """Separate transport/CDP failures from Instagram account evidence.

        Login challenges, rate limits and action blocks deliberately do not appear
        here: those states require user review and must not retry forever as if Wi-Fi
        were unavailable.
        """
        if cls._is_auth_required_error(exc) or cls._is_instagram_surface_retry_error(exc):
            return True
        if isinstance(exc, UpstreamUnavailableError):
            return True
        if isinstance(exc, WorkerExecutionError):
            return exc.code in {
                "instagram_network_unavailable",
                "instagram_profile_not_ready",
                "instagram_profile_wrong_target",
                "instagram_profile_temporarily_unavailable",
                "instagram_location_temporarily_unavailable",
                "worker_not_connected",
                "browser_context_missing",
                "browser_operations_pending",
            }
        if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
            return True
        message = f"{type(exc).__name__}: {exc}".casefold()
        return any(
            marker in message
            for marker in (
                "err_internet_disconnected",
                "err_network_changed",
                "err_connection_reset",
                "err_connection_closed",
                "err_name_not_resolved",
                "net::err_",
                "connection closed",
                "connection refused",
                "connection reset",
                "target page, context or browser has been closed",
                "browser has been closed",
                "websocket is not open",
                "socket hang up",
            )
        )

    async def _wait_retry_delay(
        self,
        control: ExecutionControl,
        profile_id: str,
        generation: int,
        delay: float | None,
    ) -> bool:
        """Wait for backoff, Retry now, or Stop; ``None`` disables timed retry."""
        if control.stop_event.is_set():
            return False
        current = control.network_waiters.get(profile_id)
        if not current or int(current.get("generation", -1)) != generation:
            return False
        retry_event = control.retry_network_events.get(profile_id)
        if retry_event is None:
            return False
        retry_wait = asyncio.create_task(retry_event.wait())
        stop_wait = asyncio.create_task(control.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {retry_wait, stop_wait}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done and stop_wait.result():
                return False
            if retry_wait in done and retry_wait.result():
                retry_event.clear()
            return not control.stop_event.is_set()
        finally:
            for waiter in (retry_wait, stop_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(retry_wait, stop_wait, return_exceptions=True)

    async def _set_network_waiting(
        self, control: ExecutionControl, profile_id: str, exc: BaseException, *,
        retry_count: int, retry_delay: float | None,
        target: dict[str, Any] | None, mode: str | None,
    ) -> int:
        return await finish_owned(self._set_network_waiting_owned(
            control, profile_id, exc, retry_count=retry_count,
            retry_delay=retry_delay, target=target, mode=mode,
        ))

    async def _set_network_waiting_owned(
        self,
        control: ExecutionControl,
        profile_id: str,
        exc: BaseException,
        *,
        retry_count: int,
        retry_delay: float | None,
        target: dict[str, Any] | None,
        mode: str | None,
    ) -> int:
        now = datetime.now(timezone.utc)
        auth_required = self._is_auth_required_error(exc)
        intervention_required = self._is_profile_intervention_error(exc)
        recovery_kind = (
            "global_auth" if auth_required else "profile_intervention"
            if intervention_required else "instagram_page"
            if self._is_instagram_surface_retry_error(exc) else "network"
        )
        target_id = target.get("id") if target else None
        async with control.network_state_lock:
            existing = control.network_waiters.get(profile_id, {})
            same_episode = bool(existing) and (
                existing.get("target_id") == target_id
                and existing.get("mode") == mode
            )
            if same_episode:
                generation = int(existing.get("generation", 1))
            else:
                generation = control.network_wait_generations.get(profile_id, 0) + 1
                control.network_wait_generations[profile_id] = generation
                control.retry_network_events[profile_id] = asyncio.Event()
                restored_count = getattr(exc, "details", {}).get("_restored_retry_count")
                retry_count = restored_count if isinstance(restored_count, int) and not isinstance(restored_count, bool) and restored_count >= 0 else 0
            # Publish the wake Event before the diagnostic. Once the renderer can see a
            # waiter, Retry-now can wake that exact generation even in the tiny gap
            # before its timed wait starts.
            control.retry_network_events.setdefault(profile_id, asyncio.Event())
            diagnostic = {
                "profile_id": profile_id,
                "target_id": target_id,
                "target": target.get("username") if target else None,
                "mode": mode,
                "generation": generation,
                "state": (
                    BitBrowserAuthRequiredError.code
                    if auth_required
                    else "manual_intervention"
                    if intervention_required
                    else "waiting_network"
                ),
                "reason": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
                "recovery_kind": recovery_kind,
                "original_reason": getattr(exc, "details", {}).get("original_reason"),
                "recovery_scope": getattr(exc, "details", {}).get("recovery_scope"),
                "candidate_username": getattr(exc, "details", {}).get("candidate_username"),
                "source_discovery_complete": getattr(exc, "details", {}).get("source_discovery_complete", False),
                "waiting_since": (
                    existing.get("waiting_since")
                    if same_episode
                    else self._utc_iso(now)
                ),
                "retry_count": retry_count,
                "next_retry_at": (
                    (now + timedelta(seconds=retry_delay)).isoformat()
                    if retry_delay is not None
                    else None
                ),
                "retry_delay_seconds": retry_delay,
            }
            for key in ("surface_diagnostics", "row_diagnostics", "scroll_diagnostics", "hover_diagnostics"):
                if clean := PlaywrightWorker._safe_relation_diagnostics(getattr(exc, "details", {}).get(key)):
                    diagnostic[key] = clean
            if recovery_kind == "instagram_page" and retry_delay is not None:
                candidate = diagnostic.get("candidate_username")
                prefix = (
                    f"列表采集已完成；候选 @{candidate} 的资料暂未读完；"
                    if diagnostic["source_discovery_complete"] and candidate
                    else "Instagram 页面暂未恢复；"
                )
                diagnostic["message"] = prefix + f"已保留进度，{retry_delay:g} 秒后自动继续"
            if auth_required and not control.manually_paused:
                # BitBrowser Local API authentication is process-wide, unlike an
                # Instagram challenge tied to one browser profile. Freeze all workers
                # at their next checkpoint until the user logs back in and continues.
                # A window loop receives a profile-local control view. Authentication
                # is the deliberate exception that must close the shared task gate;
                # clearing the view's local gate would leave this one worker blocked
                # forever after task-level Continue reopened only the global event.
                global_pause = (
                    control._shared.pause_event
                    if isinstance(control, _WindowControlView)
                    else control.pause_event
                )
                global_pause.clear()
            def persist_target_wait() -> None:
                if target:
                    self.service.set_target_runtime_status(
                        control.owner_user_id,
                        control.task_id,
                        target["id"],
                        "waiting_network",
                        window_id=profile_id,
                    )
                    if mode:
                        previous = self.service.get_checkpoint(
                            control.owner_user_id, control.task_id, target["id"], mode
                        )
                        previous_cursor = previous.get("cursor", {}) if previous else {}
                        previous_counters = previous.get("counters", {}) if previous else {}
                        if previous and previous.get("stage") == "waiting_network":
                            # Keep one stable resume snapshot across an hours-long outage;
                            # never recursively nest waiting_network checkpoints on each probe.
                            resume_stage = previous_cursor.get("resume_stage", "mode_started")
                            resume_cursor = previous_cursor.get("resume_cursor", {})
                            durable_counters = previous_counters.get("previous_counters", {})
                        else:
                            resume_stage = previous.get("stage") if previous else "mode_started"
                            resume_cursor = previous_cursor
                            durable_counters = previous_counters
                        self.service.upsert_checkpoint(
                            control.owner_user_id,
                            control.task_id,
                            target["id"],
                            mode=mode,
                            stage="waiting_network",
                            cursor={
                                "resume_stage": resume_stage,
                                "resume_cursor": resume_cursor,
                            },
                            counters={
                                "reason": diagnostic["reason"],
                                "message": diagnostic["message"],
                                "wait_kind": recovery_kind,
                                "original_reason": diagnostic["original_reason"],
                                "recovery_scope": diagnostic["recovery_scope"],
                                "retry_delay_seconds": retry_delay,
                                "recovery_target": getattr(exc, "details", {}).get("recovery_target"),
                                "candidate_username": diagnostic["candidate_username"],
                                "source_discovery_complete": diagnostic["source_discovery_complete"],
                                **{key: diagnostic[key] for key in (
                                    "surface_diagnostics", "row_diagnostics", "scroll_diagnostics", "hover_diagnostics"
                                ) if key in diagnostic},
                                **{
                                    key: getattr(exc, "details", {})[key]
                                    for key in ("list_retry_count", "surface_retry_count", "no_progress_retry_kind")
                                    if key in getattr(exc, "details", {})
                                },
                                "retry_count": diagnostic["retry_count"],
                                "next_retry_at": diagnostic["next_retry_at"],
                                "previous_counters": durable_counters,
                            },
                            recoverable=True,
                        )
            await self._await_durable_thread_call(persist_target_wait)
            pending_waiters = {**self._active_waiters_locked(control), profile_id: diagnostic}
            await self._reconcile_network_task_status_locked(control, waiters=pending_waiters)
            # No await between this publication and the completed transition.
            # Visible Retry now always refers to an already committed checkpoint.
            control.network_waiters[profile_id] = diagnostic
            self._profile_state_locked(
                control,
                profile_id,
                state=diagnostic["state"],
                target=target,
                reason=str(diagnostic["reason"]),
                message=str(diagnostic["message"]),
                current_mode=mode,
                current_stage=(
                    "manual_required"
                    if diagnostic["state"] in {"auth_required", "manual_intervention"}
                    else "waiting_network"
                ),
            )
        return generation

    async def _clear_network_waiting(
        self, control: ExecutionControl, profile_id: str,
        target: dict[str, Any] | None, *, generation: int | None = None,
        confirmed: bool = True, mark_target_running: bool = True,
        profile_state: str = "working", reason: str | None = None,
        message: str | None = None,
    ) -> bool:
        return await finish_owned(self._clear_network_waiting_owned(
            control, profile_id, target, generation=generation, confirmed=confirmed,
            mark_target_running=mark_target_running, profile_state=profile_state,
            reason=reason, message=message,
        ))

    async def _clear_network_waiting_owned(
        self,
        control: ExecutionControl,
        profile_id: str,
        target: dict[str, Any] | None,
        *,
        generation: int | None = None,
        confirmed: bool = True,
        mark_target_running: bool = True,
        profile_state: str = "working",
        reason: str | None = None,
        message: str | None = None,
    ) -> bool:
        async with control.network_state_lock:
            current_waiter = control.network_waiters.get(profile_id)
            if current_waiter is None:
                return False
            if generation is not None and int(
                current_waiter.get("generation", -1)
            ) != generation:
                return False
            if (
                target
                and mark_target_running
                and not control.stop_event.is_set()
            ):
                await self._await_durable_thread_call(
                    self.service.set_target_runtime_status,
                    control.owner_user_id,
                    control.task_id,
                    target["id"],
                    "running",
                    window_id=profile_id,
                )
            remaining_waiters = {key: value for key, value in self._active_waiters_locked(control).items()
                                 if key != profile_id}
            await self._reconcile_network_task_status_locked(control, waiters=remaining_waiters)
            control.network_waiters.pop(profile_id, None)
            control.retry_network_events.pop(profile_id, None)
            succeeded_at = self._utc_iso() if confirmed else None
            if succeeded_at:
                control.last_network_success_at = succeeded_at
            self._profile_state_locked(
                control,
                profile_id,
                state=profile_state,
                target=target,
                reason=reason,
                message=message,
                last_success_at=succeeded_at,
                current_mode=(current_waiter.get("mode") if target else None),
                current_stage=("mode_completed" if confirmed and target else None),
            )
        self._wake_work_available(control)
        self._notify_split_queue_control(control)
        return True

    async def _wait_for_network_probe_slot(
        self, control: ExecutionControl
    ) -> bool:
        """Space retry starts while a task-level semaphore bounds active reconnects."""

        loop = asyncio.get_running_loop()
        async with control.network_state_lock:
            now = loop.time()
            scheduled = max(now, control.next_network_probe_at)
            control.next_network_probe_at = (
                scheduled + self.network_retry_stagger_seconds
            )
        delay = max(0.0, scheduled - loop.time())
        if delay:
            await asyncio.sleep(delay)
        # The caller owns a shared probe slot here. It checks pause/manual state
        # synchronously afterwards and leaves that slot before waiting for input.
        return not control.stop_event.is_set()

    async def _recover_network_connection(
        self,
        control: ExecutionControl,
        worker: Any,
        profile_id: str,
        initial_error: BaseException,
        *,
        target: dict[str, Any] | None,
        mode: str | None,
        initial_retry_delay: float | None = None,
    ) -> int | None:
        """Wait with capped network backoff, then reconnect the same window.

        A signed-out BitBrowser session is not a transient transport outage. It waits
        without a timeout so the client circuit breaker is not repeatedly probed; only
        an explicit Continue/Retry-now signal attempts authentication again.
        """
        error: BaseException = initial_error
        existing = control.network_waiters.get(profile_id)
        restored_count = getattr(error, "details", {}).get("_restored_retry_count")
        retry_count = (
            (0 if existing.get("progress_confirmed") else int(existing.get("retry_count", -1)) + 1)
            if existing else restored_count
            if isinstance(restored_count, int) and not isinstance(restored_count, bool) and restored_count >= 0 else 0
        )
        generation: int | None = None
        while not control.stop_event.is_set():
            auth_required = self._is_auth_required_error(error)
            intervention_required = self._is_profile_intervention_error(error)
            page_retry_only = self._is_instagram_surface_retry_error(error)
            replacement_wait = getattr(error, "code", "") == "instagram_page_recovery_exhausted"
            requested_retry_delay = getattr(error, "details", {}).get(
                "retry_after_seconds"
            )
            if (
                isinstance(requested_retry_delay, bool)
                or not isinstance(requested_retry_delay, (int, float))
                or not math.isfinite(requested_retry_delay)
                or requested_retry_delay < 0
            ):
                requested_retry_delay = None
            delay = None
            if not (auth_required or intervention_required):
                delay = self.network_retry_delays[min(retry_count, len(self.network_retry_delays) - 1)]
                if (getattr(error, "code", "") == "browser_operations_pending"
                        or getattr(error, "details", {}).get("original_reason") == "browser_operations_pending"):
                    # Internal cleanup owns the same lease and needs no manual
                    # Continue. Recheck briefly without starting a new generation
                    # before the worker's pending-operation fence permits it.
                    delay = min(delay, 1.0)
                if page_retry_only and requested_retry_delay is not None:
                    delay = max(delay, float(requested_retry_delay))
                if replacement_wait:
                    # A local profile page failure is not a five-minute network
                    # outage. Bound retries while retaining the exact lease and
                    # source cursor; site Retry-After remains authoritative.
                    delay = min(delay, 60.0)
                    if requested_retry_delay is not None:
                        delay = max(delay, float(requested_retry_delay))
                elif getattr(error, "code", "") in {
                    "instagram_relationship_list_no_progress", "instagram_surface_no_progress"
                }:
                    delay = max(delay, self.recovery_cooldown_seconds)
                if initial_retry_delay is not None:
                    # Explicit restart/Continue honors the unexpired durable
                    # deadline once; failures after this probe get normal backoff.
                    delay = initial_retry_delay
            initial_retry_delay = None
            generation = await self._set_network_waiting(
                control,
                profile_id,
                error,
                retry_count=retry_count,
                retry_delay=delay,
                target=target,
                mode=mode,
            )
            # A manual pause freezes probing for minutes or hours without losing the
            # waiter/checkpoint. Resume or Retry now wakes normal recovery.
            await control.pause_event.wait()
            if not await self._wait_retry_delay(
                control, profile_id, generation, delay
            ):
                return None
            await control.pause_event.wait()
            if control.stop_event.is_set():
                return None
            try:
                async with control.network_retry_gate:
                    if not await self._wait_for_network_probe_slot(control):
                        return None
                    if not control.pause_event.is_set():
                        raise _ManualControlYield()
                    await self._manual_worker_checkpoint(control)
                    local_healthy = False
                    health_probe = getattr(worker, "connection_healthy", None)
                    # WAN/page timeouts are not proof that the local CDP channel died.
                    # Reuse it only after a bounded live probe. Authentication and
                    # challenge recovery retain their explicit manual gate above.
                    page_failure = page_retry_only or replacement_wait or (
                        isinstance(error, WorkerExecutionError)
                        and error.code == "instagram_network_unavailable"
                    )
                    if callable(health_probe) and page_failure and not auth_required and (not intervention_required or replacement_wait):
                        try:
                            local_healthy = await health_probe()
                            await self._manual_worker_checkpoint(control)
                        except Exception:
                            pass
                    await self._manual_worker_checkpoint(control)
                    transport_closed = getattr(error, "details", {}).get("original_reason") in {
                        "worker_not_connected", "browser_context_missing", "browser_operations_pending"
                    }
                    if (((page_retry_only or replacement_wait) and not callable(health_probe) and not transport_closed)
                            or local_healthy):
                        prepare_retry = getattr(worker, "prepare_page_retry", None)
                        if (replacement_wait or getattr(error, "code", "") in {
                            "instagram_relationship_list_no_progress", "instagram_surface_no_progress"
                        }) and callable(prepare_retry):
                            prepare_retry(getattr(error, "details", {}).get("recovery_target"))
                            # This generation passed its cooldown or manual gate: grant
                            # each retained candidate exactly one new-page attempt.
                            for username, child in getattr(worker, "_deferred_screening_workers", {}).items():
                                request = getattr(child, "request_page_replacement", None)
                                if callable(request):
                                    request(username, "instagram_profile_not_ready")
                                else:
                                    child.prepare_page_retry(username)
                        replace_page = getattr(worker, "request_page_replacement", None)
                        if (page_retry_only or replacement_wait) and target and callable(replace_page) and not getattr(error, "details", {}).get("source_discovery_complete") and getattr(error, "details", {}).get("recovery_scope") != "screening_child":
                            replace_page(target["username"], getattr(error, "details", {}).get("original_reason") or "instagram_profile_not_ready")
                        # The caller consumes the fresh-page request while it still owns
                        # this exact account and source checkpoint. Keep healthy CDP and
                        # the Instagram login session instead of reopening the account.
                        return generation
                    try:
                        try:
                            await worker.disconnect()
                        except Exception:
                            pass
                        await worker.connect(profile_id, open_if_needed=True)
                        await self._manual_worker_checkpoint(control)
                        replace_page = getattr(worker, "request_page_replacement", None)
                        if (page_retry_only or replacement_wait) and target and callable(replace_page) and not getattr(error, "details", {}).get("source_discovery_complete") and getattr(error, "details", {}).get("recovery_scope") != "screening_child":
                            replace_page(target["username"], getattr(error, "details", {}).get("original_reason") or "instagram_profile_not_ready")
                    except Exception as exc:
                        if not (
                            self._is_network_error(exc)
                            or self._is_profile_intervention_error(exc)
                        ):
                            raise
                        error = exc
                        # Auth/challenge/rate-limit waits remain manually gated. Other
                        # transport failures retain capped automatic backoff progression.
                        if not (
                            self._is_auth_required_error(exc)
                            or self._is_profile_intervention_error(exc)
                        ):
                            retry_count += 1
                        continue
            except _ManualControlYield:
                # Never hold a shared recovery slot while the operator
                # owns this paused page. Other windows keep recovering.
                await self._manual_pause_checkpoint(control, profile_id)
                initial_retry_delay = 0.0
                continue
            # A healthy local CDP attachment does not prove Instagram connectivity:
            # BitBrowser commonly stays reachable while the WAN is down. For a live
            # target, keep the waiter/retry counter until the actual Instagram mode
            # succeeds. Initial target-less BitBrowser connection may clear here.
            if target is None:
                await self._clear_network_waiting(
                    control,
                    profile_id,
                    target,
                    generation=generation,
                    profile_state="idle",
                )
            return generation
        return None

    async def _recover_no_progress(
        self, control: ExecutionControl, worker: Any, profile_id: str,
        target: dict[str, Any], mode: str, cause: WorkerExecutionError,
        *, retry_kind: str, retry_count: int,
    ) -> int | None:
        """Slow down an exhausted short retry cycle without abandoning its target."""
        list_retry = retry_kind == "relationship_list"
        error = WorkerExecutionError(
            (f"{self._mode_label(mode)}列表" if list_retry else "Instagram 页面")
            + f"连续 {retry_count} 次未推进；已保留进度，冷却后自动继续原目标",
            reason="instagram_relationship_list_no_progress" if list_retry else "instagram_surface_no_progress",
            pause_required=True,
        )
        error.details.update(cause.details)
        error.details.update(
            original_reason=cause.details.get("original_reason") or cause.code,
            recovery_target=cause.details.get("recovery_target") or target["username"],
            no_progress_retry_kind=retry_kind,
            **{"list_retry_count" if list_retry else "surface_retry_count": retry_count},
        )
        return await self._recover_network_connection(
            control, worker, profile_id, error, target=target, mode=mode,
        )

    async def _restore_recovery_wait(
        self, control: ExecutionControl, worker: Any, profile_id: str,
        target: dict[str, Any], mode: str, checkpoint: dict[str, Any] | None,
    ) -> int | None:
        """Honor a durable automatic wait on explicit restart, without auto-starting tasks."""
        if not checkpoint or checkpoint.get("stage") not in {"waiting_network", "interrupted_recoverable"}:
            return None
        counters = checkpoint.get("counters") or {}
        if counters.get("wait_kind") not in {"network", "instagram_page"}:
            return None
        reason = counters.get("reason")
        error = WorkerExecutionError(str(counters.get("message") or "按保存的检查点继续恢复"), reason=reason)
        error.details.update({
            key: counters[key]
            for key in ("original_reason", "recovery_target", "candidate_username", "source_discovery_complete", "recovery_scope",
                        "list_retry_count", "surface_retry_count", "no_progress_retry_kind")
            if key in counters
        })
        for key in ("surface_diagnostics", "row_diagnostics", "scroll_diagnostics", "hover_diagnostics"):
            if clean := PlaywrightWorker._safe_relation_diagnostics(counters.get(key)):
                error.details[key] = clean
        error.details["_restored_retry_count"] = counters.get("retry_count", 0)
        if counters.get("wait_kind") == "network" and reason in {
            "TimeoutError", "ConnectionError", "ConnectionResetError",
            "ConnectionRefusedError", "ConnectionAbortedError", "BrokenPipeError",
            "upstream_unavailable",
        }:
            # Rehydration loses the original exception class. Preserve its known
            # transport identity instead of accidentally ignoring its saved wait.
            error.code = "instagram_network_unavailable"
            error.details["original_reason"] = counters.get("original_reason") or reason
        # Old or damaged metadata cannot turn account restrictions or unknown
        # application errors into a timer-triggered retry.
        if not self._is_network_error(error) or self._is_profile_intervention_error(error):
            return None
        delay: float | None = None
        raw_deadline = counters.get("next_retry_at")
        if isinstance(raw_deadline, str):
            try:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
                if deadline.tzinfo is not None:
                    delay = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        return await self._recover_network_connection(
            control, worker, profile_id, error, target=target, mode=mode,
            initial_retry_delay=delay,
        )

    @staticmethod
    async def _wait_for_collection_work(control: ExecutionControl) -> None:
        work_wait = asyncio.create_task(control.work_available.wait())
        stop_wait = asyncio.create_task(control.stop_event.wait())
        try:
            await asyncio.wait({work_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (work_wait, stop_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(work_wait, stop_wait, return_exceptions=True)

    async def _window_loop(
        self,
        control: ExecutionControl,
        profile_id: str,
        preferred_targets: list[dict[str, Any]],
        queue: asyncio.Queue[dict[str, Any]],
        modes: list[str],
        settings: dict[str, Any],
    ) -> None:
        collection_platform(settings)
        worker = self.worker_factory(self.bitbrowser)
        control.profile_workers[profile_id] = worker
        async def worker_manual_checkpoint():
            await self._manual_worker_checkpoint(control)
        try:
            worker.manual_control_checkpoint = worker_manual_checkpoint
        except (AttributeError, TypeError):
            pass
        current_target: dict[str, Any] | None = None
        last_processed_target: dict[str, Any] | None = None
        current_from_shared_queue = False
        active_network_generation: int | None = None
        drained_window = False

        async def claim_live_target() -> None:
            # A cancelled await cannot discard a claim that SQLite committed.
            # Publish its ownership inside the protected operation, so the normal
            # finally block reconciles this exact target before releasing a window.
            async def claim_and_publish() -> None:
                nonlocal current_target, preclaimed_split, current_from_shared_queue
                current_target = await asyncio.to_thread(
                    self.service.claim_next_split_candidate,
                    control.owner_user_id, control.task_id, profile_id,
                )
                preclaimed_split = current_target is not None
                current_from_shared_queue = False
                if current_target is not None:
                    control.enqueued_target_ids.add(current_target["id"])
            await finish_owned(claim_and_publish())

        try:
            while not control.stop_event.is_set():
                await self._manual_pause_checkpoint(control, profile_id)
                try:
                    await worker.connect(profile_id, open_if_needed=True)
                    await self._manual_pause_checkpoint(control, profile_id)
                    break
                except Exception as exc:
                    if not (
                        self._is_network_error(exc)
                        or self._is_profile_intervention_error(exc)
                    ):
                        raise
                    recovered_generation = await self._recover_network_connection(
                        control,
                        worker,
                        profile_id,
                        exc,
                        target=None,
                        mode=None,
                    )
                    if recovered_generation is None:
                        return
                    break
            async with control.network_state_lock:
                self._profile_state_locked(control, profile_id, state="idle")
                await self._reconcile_network_task_status_locked(control)
            while not control.stop_event.is_set():
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    return
                dispatch_generation = control.work_generation
                dispatch_state = await self._await_durable_thread_call(self.service.collection_dispatch_state, control.owner_user_id)
                if dispatch_state["locked"]:
                    if (settings.get("live_queue_enabled")
                        and not await self._await_durable_thread_call(
                            self.service.has_unfinished_window_targets,
                            control.owner_user_id, control.task_id, profile_id)):
                        if control.work_generation != dispatch_generation:
                            continue
                        drained_window = True
                        return
                    if not settings.get("live_queue_enabled") and not preferred_targets and queue.empty():
                        unfinished = await self._await_durable_thread_call(
                            self.service.has_unfinished_window_targets,
                            control.owner_user_id, control.task_id, profile_id)
                        if control.work_generation != dispatch_generation:
                            continue
                        drained_window = not unfinished
                        return
                    # Clear before rereading the persistent gate, so an unlock
                    # notification cannot be consumed while the gate is opening.
                    control.work_available.clear()
                    dispatch_state = await self._await_durable_thread_call(self.service.collection_dispatch_state, control.owner_user_id)
                    if dispatch_state["locked"]:
                        if control.work_generation != dispatch_generation:
                            continue
                        last_processed_target = None
                        self._profile_state_locked(control, profile_id, state="idle",
                            reason="collection_dispatch_locked", message="领取已锁定，等待解锁后继续")
                        await self._wait_for_collection_work(control)
                        continue
                    control.work_available.set()
                locked_usernames = dispatch_state["usernames"]
                from_shared_queue = False
                preclaimed_split = False
                locked_waiters: set[str] = set()
                preferred_index = next((index for index, item in enumerate(preferred_targets)
                    if str(item.get("username_norm") or item.get("username") or "").casefold() not in locked_usernames), None)
                if preferred_index is not None:
                    current_target = preferred_targets.pop(preferred_index)
                else:
                    current_target = None
                    async with control.queue_claim_lock:
                        if (
                            settings.get("live_queue_enabled")
                            and control.work_available.is_set()
                        ):
                            # A durable split notification is checked before the
                            # ordinary in-memory queue. This lets a B-only split
                            # target reach B promptly even while many automatic
                            # initial/retry targets are already queued.
                            await claim_live_target()
                            preclaimed_split = current_target is not None
                            if current_target is None:
                                # Clear and recheck while holding the same claim
                                # lock. A durable enqueue racing the clear is either
                                # found by this second read or leaves the event set.
                                control.work_available.clear()
                                await claim_live_target()
                                preclaimed_split = current_target is not None
                        if current_target is None:
                            try:
                                current_target = self._dequeue_compatible_target(
                                    queue, profile_id, locked_usernames, locked_waiters
                                )
                                from_shared_queue = True
                            except asyncio.QueueEmpty:
                                pass
                    if current_target is None:
                        if not settings.get("live_queue_enabled") and not preferred_targets and not locked_waiters:
                            unfinished = await self._await_durable_thread_call(
                                self.service.has_unfinished_window_targets,
                                control.owner_user_id, control.task_id, profile_id)
                            if control.work_generation != dispatch_generation:
                                continue
                            drained_window = not unfinished
                            return
                        # Locked initial/preferred targets are still unfinished.
                        # Keep ownership and wait, including non-live tasks.
                        last_processed_target = None
                        control.work_available.clear()
                        if await self._await_durable_thread_call(self.service.collection_dispatch_state, control.owner_user_id) != dispatch_state:
                            continue
                        if settings.get("live_queue_enabled"):
                            async with control.queue_claim_lock:
                                await claim_live_target()
                                preclaimed_split = current_target is not None
                        if current_target is None:
                            if (settings.get("live_queue_enabled")
                                and not await self._await_durable_thread_call(
                                    self.service.has_unfinished_window_targets,
                                    control.owner_user_id, control.task_id, profile_id)):
                                if control.work_generation != dispatch_generation:
                                    continue
                                # The same atomic claim path has checked global /
                                # per-source locks and this window's hard affinity.
                                # No browser action or candidate consumer is active
                                # here: each prior mode returned only after draining
                                # and durably recording every candidate.
                                drained_window = True
                                return
                            if control.work_generation != dispatch_generation:
                                continue
                            self._profile_state_locked(control, profile_id, state="idle",
                                reason="collection_dispatch_locked" if locked_usernames else None,
                                message="部分分裂号已锁定，等待可领取目标" if locked_usernames else None)
                            await self._wait_for_collection_work(control)
                            continue
                current_from_shared_queue = from_shared_queue
                # A target may arrive while this window is paused. Never claim it until
                # the user resumes the task; leave other selected windows waiting too.
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    return
                async with control.queue_claim_lock:
                    if (
                        current_target["id"] in control.deleted_target_ids
                        and not preclaimed_split
                    ):
                        # The tombstone belongs to an old in-memory queue payload.
                        # A durable split requeue deliberately reuses the same target
                        # id to preserve its checkpoint; once claim_next_split_candidate
                        # has atomically claimed that new generation, it must run.
                        # Keep the marker until the stale shared payload consumes it.
                        if from_shared_queue:
                            queue.task_done()
                        control.deleted_target_ids.discard(current_target["id"])
                        control.enqueued_target_ids.discard(current_target["id"])
                        current_target = None
                        continue
                    if not preclaimed_split:
                        try:
                            await self._await_durable_thread_call(
                                self.service.set_target_runtime_status,
                                control.owner_user_id, control.task_id,
                                current_target["id"], "running", window_id=profile_id,
                            )
                        except ConflictError as exc:
                            if exc.details.get("reason") in {
                                "target_owned_by_other_window", "split_candidate_waiting",
                            }:
                                # A delayed row won the durable SQLite claim
                                # after a stale in-memory payload was selected.
                                # Ignore this payload; its owner window continues.
                                if from_shared_queue:
                                    queue.task_done()
                                if exc.details.get("reason") == "split_candidate_waiting":
                                    control.enqueued_target_ids.discard(current_target["id"])
                                current_target = None
                                current_from_shared_queue = False
                                continue
                            if exc.details.get("reason") != "collection_dispatch_locked":
                                raise
                            # The lock won the SQLite transaction after selection
                            # (possibly while pause_event was awaited). Put the
                            # unclaimed target back without marking a failure.
                            if from_shared_queue:
                                queue.put_nowait(current_target)
                                queue.task_done()
                            else:
                                preferred_targets.insert(0, current_target)
                            current_target = None
                            current_from_shared_queue = False
                            continue
                    last_processed_target = None
                    control.profile_completed_target_ids.pop(profile_id, None)
                    self._profile_state_locked(
                        control,
                        profile_id,
                        state="working",
                        target=current_target,
                        current_stage="opening_profile",
                    )
                target_failed = False
                target_incomplete = False
                recheck_mode = await self._await_durable_thread_call(
                    self.service.active_source_recheck_mode,
                    control.owner_user_id, control.task_id, current_target["id"],
                )
                target_modes = [recheck_mode] if recheck_mode else modes
                mode_index = 0
                list_loading_retry_count = 0
                surface_retry_count = 0
                retry_progress_marker: tuple[int, int] | None = None
                while mode_index < len(target_modes):
                    mode = target_modes[mode_index]
                    await control.pause_event.wait()
                    if control.stop_event.is_set():
                        return
                    checkpoint = await self._await_durable_thread_call(
                        self.service.get_checkpoint,
                        control.owner_user_id, control.task_id, current_target["id"], mode
                    )
                    if retry_progress_marker is None:
                        list_loading_retry_count = self._checkpoint_retry_count(
                            checkpoint, "list_retry_count"
                        )
                        surface_retry_count = self._checkpoint_retry_count(
                            checkpoint,
                            "surface_retry_count",
                            instagram_page_fallback=True,
                        )
                        retry_progress_marker = self._checkpoint_progress_marker(
                            checkpoint
                        )
                        active_network_generation = await self._restore_recovery_wait(
                            control, worker, profile_id, current_target, mode, checkpoint,
                        )
                        if control.stop_event.is_set():
                            return
                    if (
                        checkpoint
                        and checkpoint["stage"] == "mode_completed"
                        and (
                            mode not in {"followers", "following"}
                            or self._candidate_spool_complete(
                                checkpoint, require_natural_end=True
                            )
                        )
                    ):
                        completed_stats = await self._await_durable_thread_call(
                            self.service.reconcile_task_mode_candidates,
                            control.owner_user_id, control.task_id,
                            current_target["id"], mode,
                        )
                        if completed_stats["pending"] == 0:
                            list_loading_retry_count = 0
                            surface_retry_count = 0
                            retry_progress_marker = None
                            mode_index += 1
                            continue
                    async with control.network_state_lock:
                        waiter = control.network_waiters.get(profile_id)
                        attempting_recovery = bool(waiter and waiter.get("state") == "waiting_network")
                        if attempting_recovery:
                            waiter["recovery_in_progress"] = True
                            waiter["progress_confirmed"] = False
                            # Preserve the internal failure budget until real mode
                            # completion. Merely entering a probe is not success.
                            await self._await_durable_thread_call(
                                self.service.set_target_runtime_status,
                                control.owner_user_id, control.task_id, current_target["id"],
                                "running", window_id=profile_id,
                            )
                        self._profile_state_locked(
                            control,
                            profile_id,
                            state="working",
                            target=current_target,
                            current_mode=mode,
                            current_stage="recovering_page" if attempting_recovery else "collecting_list",
                        )
                        await self._reconcile_network_task_status_locked(control)
                    try:
                        progress = await self._execute_candidate_spooled_mode(
                            control,
                            worker,
                            current_target,
                            mode,
                            settings,
                            checkpoint,
                            parent_reels_eligible=(mode_index == len(target_modes) - 1 and not target_incomplete),
                        )
                        self._record_profile_progress(control, profile_id)
                        skipped_posts = progress["skipped_posts"]
                        if mode == "post_likers" and skipped_posts:
                            await self._await_durable_thread_call(
                                self.service.upsert_checkpoint,
                                control.owner_user_id,
                                control.task_id,
                                current_target["id"],
                                mode=mode,
                                stage="mode_unavailable",
                                cursor={
                                    "candidate_spool_version": 1,
                                    "candidate_spool_complete": False,
                                    "skipped_posts": skipped_posts,
                                },
                                counters={
                                    "reason": "instagram_post_likers_partial",
                                    "message": "部分帖子未能打开点赞用户列表",
                                    "visible_accounts": progress["total"],
                                    "source_total": progress["source_total"],
                                    "discovered": progress["total"],
                                    "processed": progress["recorded"] + progress["deduped"],
                                    "saved": progress["recorded"],
                                    "skipped_existing": 0,
                                    "skipped_global_duplicates": progress["deduped"],
                                    "pending_candidates": progress["pending"],
                                },
                                recoverable=True,
                            )
                            target_incomplete = True
                        else:
                            if (
                                progress.get("discovery_complete") is not True
                                or progress.get("pending") != 0
                                or progress["recorded"] + progress["deduped"] != progress["total"]
                            ):
                                raise WorkerExecutionError(
                                    "列表发现或账号处理尚未结束，已保留进度等待继续",
                                    reason="collection_mode_incomplete", pause_required=True,
                                )
                            await self._save_candidate_progress_checkpoint(
                                control,
                                current_target["id"],
                                mode,
                                progress,
                                stage="mode_completed",
                                discovery_complete=True,
                                source_total=progress["source_total"],
                                resume_tail=progress.get("resume_tail"),
                                rendered_count=progress.get("rendered_count"),
                                progress_epoch=progress.get("progress_epoch"),
                                pending_relation_usernames=progress.get("pending_relation_usernames"),
                                automatic_gap_recheck_started=progress.get("automatic_gap_recheck_started", False),
                            )
                    except _ManualControlYield:
                        await self._manual_pause_checkpoint(control, profile_id)
                        # Re-read the same mode's durable cursor. The old source
                        # and screening coroutines have all exited; no saved
                        # locator resumes on a page the operator may have changed.
                        continue
                    except WorkerExecutionError as exc:
                        await control.pause_event.wait()
                        if self._is_relationship_list_incomplete_error(exc):
                            if profile_id in control.network_waiters:
                                await self._clear_network_waiting(
                                    control, profile_id, current_target,
                                    generation=active_network_generation, confirmed=False,
                                    mark_target_running=True, profile_state="degraded",
                                    reason=exc.code, message=exc.message,
                                )
                                active_network_generation = None
                            # Instagram sometimes leaves the relationship dialog
                            # open but stops appending rows. Re-open/re-scan the same
                            # read-only mode after a capped backoff without tearing
                            # down CDP or declaring a network outage. Candidate spool
                            # writes already committed by candidate_sink remain the
                            # resume checkpoint.
                            latest = await self._await_durable_thread_call(
                                self.service.get_checkpoint,
                                control.owner_user_id,
                                control.task_id,
                                current_target["id"],
                                mode,
                            ) or checkpoint
                            previous_cursor = (
                                dict(latest.get("cursor") or {}) if latest else {}
                            )
                            previous_counters = (
                                dict(latest.get("counters") or {}) if latest else {}
                            )
                            if latest and latest.get("stage") == "collecting_list":
                                nested_cursor = previous_cursor.get("resume_cursor")
                                nested_counters = previous_counters.get("previous_counters")
                                if isinstance(nested_cursor, dict):
                                    previous_cursor = nested_cursor
                                if isinstance(nested_counters, dict):
                                    previous_counters = nested_counters
                            current_stats = await asyncio.to_thread(
                                self.service.task_mode_candidate_stats,
                                control.owner_user_id,
                                control.task_id,
                                current_target["id"],
                                mode,
                            )
                            current_marker = (
                                int(current_stats["total"]),
                                int(current_stats["recorded"])
                                + int(current_stats["deduped"]),
                            )
                            if (
                                retry_progress_marker is None
                                or current_marker[0] > retry_progress_marker[0]
                                or current_marker[1] > retry_progress_marker[1]
                            ):
                                # A huge list may legitimately stall several times.
                                # Only consecutive stalls without any durable new row
                                # consume its retry budget.
                                list_loading_retry_count = 0
                                surface_retry_count = 0
                            retry_progress_marker = (
                                max(
                                    current_marker[0],
                                    (retry_progress_marker or (0, 0))[0],
                                ),
                                max(
                                    current_marker[1],
                                    (retry_progress_marker or (0, 0))[1],
                                ),
                            )
                            list_loading_retry_count += 1
                            if (
                                list_loading_retry_count
                                > self.relationship_no_progress_retry_limit
                            ):
                                recovered_generation = await self._recover_no_progress(
                                    control, worker, profile_id, current_target, mode,
                                    exc, retry_kind="relationship_list",
                                    retry_count=list_loading_retry_count,
                                )
                                if recovered_generation is None:
                                    return
                                active_network_generation = recovered_generation
                                continue
                            delay = self.network_retry_delays[
                                min(
                                    list_loading_retry_count - 1,
                                    len(self.network_retry_delays) - 1,
                                )
                            ]
                            await self._await_durable_thread_call(
                                self.service.upsert_checkpoint,
                                control.owner_user_id,
                                control.task_id,
                                current_target["id"],
                                mode=mode,
                                stage="collecting_list",
                                cursor={
                                    "resume_stage": (
                                        latest.get("stage") if latest else "discovering_accounts"
                                    ),
                                    "resume_cursor": previous_cursor,
                                },
                                counters={
                                    "reason": exc.code,
                                    "message": f"列表暂未推进，{delay:g} 秒后新建采集页继续（重试 {list_loading_retry_count}）",
                                    "list_retry_count": list_loading_retry_count,
                                    "retry_delay_seconds": delay,
                                    "previous_counters": previous_counters,
                                },
                                recoverable=True,
                            )
                            async with control.network_state_lock:
                                self._profile_state_locked(
                                    control,
                                    profile_id,
                                    state="degraded",
                                    target=current_target,
                                    current_mode=mode,
                                    current_stage="collecting_list",
                                    reason=exc.code,
                                    message=f"列表暂未推进，{delay:g} 秒后新建采集页继续（重试 {list_loading_retry_count}）",
                                )
                            await control.pause_event.wait()
                            retry_signals = [asyncio.create_task(control.stop_event.wait())]
                            manual_event = control.manual_events.get(profile_id)
                            if manual_event is not None:
                                retry_signals.append(asyncio.create_task(manual_event.wait()))
                            try:
                                await asyncio.wait(retry_signals, timeout=delay,
                                                   return_when=asyncio.FIRST_COMPLETED)
                            finally:
                                for signal in retry_signals:
                                    if not signal.done():
                                        signal.cancel()
                                await asyncio.gather(*retry_signals, return_exceptions=True)
                            if control.stop_event.is_set():
                                return
                            await control.pause_event.wait()
                            if control.stop_event.is_set():
                                return
                            replace_page = getattr(worker, "request_page_replacement", None)
                            if callable(replace_page):
                                replace_page(current_target["username"], exc.code)
                            continue
                        if self._is_instagram_surface_retry_error(exc):
                            current_stats = await asyncio.to_thread(
                                self.service.task_mode_candidate_stats,
                                control.owner_user_id,
                                control.task_id,
                                current_target["id"],
                                mode,
                            )
                            current_marker = (
                                int(current_stats["total"]),
                                int(current_stats["recorded"])
                                + int(current_stats["deduped"]),
                            )
                            if (
                                retry_progress_marker is None
                                or current_marker[0] > retry_progress_marker[0]
                                or current_marker[1] > retry_progress_marker[1]
                            ):
                                list_loading_retry_count = 0
                                surface_retry_count = 0
                            retry_progress_marker = (
                                max(
                                    current_marker[0],
                                    (retry_progress_marker or (0, 0))[0],
                                ),
                                max(
                                    current_marker[1],
                                    (retry_progress_marker or (0, 0))[1],
                                ),
                            )
                            surface_retry_count += 1
                            if (
                                surface_retry_count
                                > self.surface_no_progress_retry_limit
                            ):
                                recovered_generation = await self._recover_no_progress(
                                    control, worker, profile_id, current_target, mode,
                                    exc, retry_kind="instagram_surface",
                                    retry_count=surface_retry_count,
                                )
                                if recovered_generation is None:
                                    return
                                active_network_generation = recovered_generation
                                continue
                        if self._is_network_error(
                            exc
                        ) or self._is_profile_intervention_error(exc):
                            recovered_generation = await self._recover_network_connection(
                                control,
                                worker,
                                profile_id,
                                exc,
                                target=current_target,
                                mode=mode,
                            )
                            if recovered_generation is not None:
                                active_network_generation = recovered_generation
                                # Retry the same read-only collection mode. Durable
                                # results and global dedupe skip every account already
                                # saved before the outage.
                                continue
                            target_failed = True
                            break
                        if not exc.details.get("pause_required", True):
                            # This is a conclusive target/UI outcome, not an active
                            # network wait. End the matching recovery generation before
                            # marking the target recoverable so Retry-now cannot retain a
                            # stale Event for an idle worker.
                            if profile_id in control.network_waiters:
                                await self._clear_network_waiting(
                                    control,
                                    profile_id,
                                    None,
                                    generation=active_network_generation,
                                    confirmed=False,
                                    mark_target_running=False,
                                    profile_state="degraded",
                                    reason=exc.code,
                                    message=exc.message,
                                )
                                active_network_generation = None
                            # Unavailable/private sources can be retried later.
                            # Retain the supplemental-pass budget and last durable
                            # source position rather than granting a fresh pass.
                            await self._save_profile_local_failure_checkpoint(
                                control, current_target, mode, checkpoint,
                                reason=exc.code, message=exc.message,
                            )
                            target_incomplete = True
                            list_loading_retry_count = 0
                            surface_retry_count = 0
                            retry_progress_marker = None
                            mode_index += 1
                            continue
                        await self._save_profile_local_failure_checkpoint(
                            control,
                            current_target,
                            mode,
                            checkpoint,
                            reason=exc.code or "window_mode_failed",
                            message=exc.message,
                        )
                        async with control.network_state_lock:
                            self._profile_state_locked(
                                control,
                                profile_id,
                                state="recoverable",
                                target=current_target,
                                current_mode=mode,
                                current_stage="mode_unavailable",
                                reason=exc.code or "window_mode_failed",
                                message=exc.message,
                            )
                        target_failed = True
                        break
                    except (UpstreamUnavailableError, DomainError) as exc:
                        await control.pause_event.wait()
                        if self._is_network_error(
                            exc
                        ) or self._is_profile_intervention_error(exc):
                            recovered_generation = await self._recover_network_connection(
                                control,
                                worker,
                                profile_id,
                                exc,
                                target=current_target,
                                mode=mode,
                            )
                            if recovered_generation is not None:
                                active_network_generation = recovered_generation
                                continue
                            target_failed = True
                            break
                        await self._save_profile_local_failure_checkpoint(
                            control,
                            current_target,
                            mode,
                            checkpoint,
                            reason=(
                                str(getattr(exc, "code", "") or "window_domain_failed")
                            ),
                            message=str(exc),
                        )
                        async with control.network_state_lock:
                            self._profile_state_locked(
                                control,
                                profile_id,
                                state="recoverable",
                                target=current_target,
                                current_mode=mode,
                                current_stage="mode_unavailable",
                                reason=(
                                    str(
                                        getattr(exc, "code", "")
                                        or "window_domain_failed"
                                    )
                                ),
                                message=str(exc),
                            )
                        target_failed = True
                        break
                    except Exception as exc:
                        await control.pause_event.wait()
                        if self._is_network_error(
                            exc
                        ) or self._is_profile_intervention_error(exc):
                            recovered_generation = await self._recover_network_connection(
                                control,
                                worker,
                                profile_id,
                                exc,
                                target=current_target,
                                mode=mode,
                            )
                            if recovered_generation is not None:
                                active_network_generation = recovered_generation
                                continue
                            target_failed = True
                            break
                        failure_message = f"{type(exc).__name__}: {exc}"
                        await self._save_profile_local_failure_checkpoint(
                            control,
                            current_target,
                            mode,
                            checkpoint,
                            reason="window_unclassified_failure",
                            message=failure_message,
                        )
                        async with control.network_state_lock:
                            self._profile_state_locked(
                                control,
                                profile_id,
                                state="recoverable",
                                target=current_target,
                                current_mode=mode,
                                current_stage="mode_unavailable",
                                reason="window_unclassified_failure",
                                message=failure_message,
                            )
                        target_failed = True
                        break
                    if profile_id in control.network_waiters:
                        # Clear WAITING_NETWORK only after the real Instagram
                        # collection/screening mode completed, not merely after the
                        # localhost CDP socket reattached.
                        await self._clear_network_waiting(
                            control,
                            profile_id,
                            current_target,
                            generation=active_network_generation,
                        )
                        active_network_generation = None
                    else:
                        succeeded_at = self._utc_iso()
                        async with control.network_state_lock:
                            control.last_network_success_at = succeeded_at
                            self._profile_state_locked(
                                control,
                                profile_id,
                                state="working",
                                target=current_target,
                                last_success_at=succeeded_at,
                                current_mode=mode,
                                current_stage="mode_completed",
                            )
                    list_loading_retry_count = 0
                    surface_retry_count = 0
                    retry_progress_marker = None
                    mode_index += 1
                if control.stop_event.is_set():
                    return
                completed_status = "recoverable" if target_failed or target_incomplete else "completed"
                if recheck_mode and completed_status == "completed":
                    completed_status = await self._await_durable_thread_call(
                        self.service.source_recheck_completion_status,
                        control.owner_user_id, control.task_id, current_target["id"], recheck_mode,
                    )
                async with control.queue_claim_lock:
                    await self._await_durable_thread_call(
                        self.service.set_target_runtime_status,
                        control.owner_user_id,
                        control.task_id,
                        current_target["id"],
                        completed_status,
                        window_id=profile_id,
                        source_recheck_mode=recheck_mode if not (target_failed or target_incomplete) else None,
                        automatic_completion_token=control.leases.get(profile_id) if completed_status == "completed" else None,
                    )
                    if from_shared_queue:
                        queue.task_done()
                    control.enqueued_target_ids.discard(current_target["id"])
                if completed_status == "completed":
                    control.profile_completed_target_ids[profile_id] = current_target["id"]
                last_processed_target = current_target
                self._record_profile_progress(control, profile_id)
                previous_profile_state = control.profile_states.get(profile_id, {})
                self._profile_state_locked(
                    control,
                    profile_id,
                    state=(
                        "recoverable"
                        if target_failed
                        else "degraded" if target_incomplete else "idle"
                    ),
                    reason=(
                        previous_profile_state.get("reason")
                        if target_failed
                        else "target_incomplete" if target_incomplete else None
                    ),
                    message=(
                        previous_profile_state.get("message")
                        if target_failed
                        else None
                    ),
                )
                current_target = None
                current_from_shared_queue = False
                if recheck_mode:
                    drained_window = completed_status == "completed" and not await self._await_durable_thread_call(
                        self.service.has_unfinished_window_targets,
                        control.owner_user_id, control.task_id, profile_id,
                    )
                    return
                if target_failed:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A single browser/profile failure is an execution-unit failure, not a
            # task-wide fatal condition.  Stop only this window and retain its current
            # target as recoverable; the other workers keep draining the shared queue.
            if isinstance(control, _WindowControlView):
                async with control.network_state_lock:
                    self._profile_state_locked(
                        control,
                        profile_id,
                        state="recoverable",
                        target=current_target,
                        reason="window_execution_failed",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                control.stop_event.set()
            else:
                control.fatal_status = "recoverable"
                control.fatal_error = f"{type(exc).__name__}: {exc}"
                control.stop_event.set()
            self._wake_work_available(control)
        finally:
            async def finalize_window() -> None:
                self._revoke_manual_control(control, profile_id)
                # Own the state/queue locks as well as disconnect: a second Stop
                # must not abandon cleanup before it reaches the browser close.
                prior_state = control.profile_states.get(profile_id, {})
                preserve_recoverable = prior_state.get("state") == "recoverable"
                try:
                    # Every exit path, including a direct return after stop_event, converges the
                    # currently claimed target. Previously those direct returns left durable
                    # RUNNING rows after the coordinator became RECOVERABLE/STOPPED.
                    if current_target is not None:
                        async with control.queue_claim_lock:
                            try:
                                current = await self._await_durable_thread_call(
                                    self.service.get_task,
                                    control.owner_user_id, control.task_id
                                )
                                stored = next(
                                    (
                                        item
                                        for item in current.get("targets", [])
                                        if item["id"] == current_target["id"]
                                    ),
                                    None,
                                )
                                if stored and stored["status"] in {
                                    "running",
                                    "waiting_network",
                                }:
                                    await self._await_durable_thread_call(
                                        self.service.set_target_runtime_status,
                                        control.owner_user_id,
                                        control.task_id,
                                        current_target["id"],
                                        "recoverable",
                                        window_id=profile_id,
                                    )
                            except DomainError:
                                pass
                            if current_from_shared_queue:
                                queue.task_done()
                            control.enqueued_target_ids.discard(current_target["id"])
                    if profile_id in control.network_waiters:
                        await self._clear_network_waiting(
                            control,
                            profile_id,
                            None,
                            generation=active_network_generation,
                            confirmed=False,
                            mark_target_running=False,
                            profile_state="stopped",
                            reason="worker_exited",
                        )
                    async with control.network_state_lock:
                        prior_state = control.profile_states.get(profile_id, {})
                        preserve_recoverable = prior_state.get("state") == "recoverable"
                        cleanup_target = current_target or last_processed_target
                        self._profile_state_locked(
                            control,
                            profile_id,
                            state="recoverable" if preserve_recoverable else "stopped",
                            target=cleanup_target,
                            current_stage="disconnecting" if cleanup_target else None,
                            reason=(
                                prior_state.get("reason")
                                if preserve_recoverable
                                else "worker_exited"
                            ),
                            message=(
                                prior_state.get("message") if preserve_recoverable else None
                            ),
                        )
                        await self._reconcile_network_task_status_locked(control)
                finally:
                    # Keep the generation registered until disconnect settles,
                    # even if target/status reconciliation itself failed.
                    cleanup_succeeded = False
                    cleanup_failed = False
                    try:
                        await worker.disconnect()
                        cleanup_succeeded = True
                    except asyncio.CancelledError:
                        if not callable(getattr(worker, 'wait_for_cleanup', None)):
                            raise
                    except Exception:
                        cleanup_failed = True
                        control.source_recheck_profiles.add(profile_id)
                        self._profile_state_locked(control, profile_id, state="manual_required",
                            reason="source_recheck_cleanup_failed",
                            message="页面清理未成功，窗口仍由原任务占用；请停止该窗口重试清理")
                        raise
                    finally:
                        wait_for_cleanup = getattr(worker, 'wait_for_cleanup', None)
                        if callable(wait_for_cleanup):
                            try:
                                await finish_owned(wait_for_cleanup())
                                cleanup_succeeded = not cleanup_failed
                            except BaseException:
                                control.source_recheck_profiles.add(profile_id)
                                self._profile_state_locked(control, profile_id, state="manual_required",
                                    reason="source_recheck_cleanup_failed",
                                    message="页面清理未成功，窗口占用已保留，请停止该窗口重试清理")
                                raise
                        if cleanup_succeeded:
                            control.cleaned_profile_ids.add(profile_id)
                        async with control.network_state_lock:
                            self._profile_state_locked(
                                control, profile_id,
                                state="manual_required" if not cleanup_succeeded else "recoverable" if preserve_recoverable else "stopped",
                                reason="source_recheck_cleanup_failed" if not cleanup_succeeded else prior_state.get("reason") if preserve_recoverable else "worker_exited",
                                message="页面清理未成功，窗口占用已保留，请停止该窗口重试清理" if not cleanup_succeeded else prior_state.get("message") if preserve_recoverable else None,
                            )
                    if drained_window:
                        await self._close_drained_window(control, profile_id)
                # Publish settled cleanup before finish_owned re-raises any
                # cancellation received while disconnect was still running.
                if control.profile_workers.get(profile_id) is worker:
                    control.profile_workers.pop(profile_id, None)

            await finish_owned(finalize_window())

    @staticmethod
    def _candidate_spool_complete(
        checkpoint: dict[str, Any] | None,
        *,
        require_natural_end: bool = False,
    ) -> bool:
        if not checkpoint:
            return False
        cursor = checkpoint.get("cursor") or {}
        # Both a live WAITING_NETWORK checkpoint and startup recovery's
        # INTERRUPTED_RECOVERABLE checkpoint wrap the same durable cursor.  Unwrap by
        # shape rather than stage name so an application restart cannot make a fully
        # discovered source appear incomplete and reopen its relationship list.
        for _ in range(4):
            resume_cursor = cursor.get("resume_cursor")
            if not isinstance(resume_cursor, dict):
                break
            cursor = resume_cursor
        if not cursor.get("candidate_spool_complete") or cursor.get("pending_relation_usernames"):
            return False
        return not require_natural_end or bool(
            cursor.get("candidate_spool_natural_end")
        )

    @staticmethod
    def _checkpoint_source_total(checkpoint: dict[str, Any] | None) -> int | None:
        """Recover the last visible relationship total through wait wrappers."""

        if not checkpoint:
            return None
        counters: Any = checkpoint.get("counters") or {}
        for _ in range(4):
            if not isinstance(counters, dict):
                return None
            value = counters.get("source_total")
            if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
                return value
            previous = counters.get("previous_counters")
            if not isinstance(previous, dict):
                return None
            counters = previous
        return None

    @staticmethod
    def _checkpoint_resume_cursor(
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return the innermost durable cursor through retry/wait wrappers."""

        cursor: Any = (checkpoint or {}).get("cursor") or {}
        for _ in range(6):
            if not isinstance(cursor, dict):
                return {}
            nested = cursor.get("resume_cursor")
            if not isinstance(nested, dict):
                return dict(cursor)
            cursor = nested
        return dict(cursor) if isinstance(cursor, dict) else {}

    @staticmethod
    def _normalize_pending_relation_usernames(raw: Any) -> list[str]:
        """Unconfirmed visible identities are obligations, never candidate counts."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise WorkerExecutionError("待确认列表身份检查点无效，已保留来源等待恢复",
                                       reason="instagram_relation_checkpoint_invalid", pause_required=True)
        names: list[str] = []
        seen: set[str] = set()
        for value in raw:
            if not isinstance(value, str):
                continue
            value = value.strip().lstrip("@").casefold()
            if not re.fullmatch(r"[a-z0-9._]{1,30}", value) or value in seen:
                continue
            seen.add(value)
            names.append(value)
            if len(names) > 1000:
                raise WorkerExecutionError("待确认列表身份过多，已停止来源以避免丢失检查点",
                                           reason="instagram_relation_checkpoint_overflow", pause_required=True)
        return names

    @classmethod
    def _checkpoint_resume_tail(
        cls, checkpoint: dict[str, Any] | None
    ) -> list[str]:
        cursor = cls._checkpoint_resume_cursor(checkpoint)
        raw = cursor.get("resume_tail")
        if not isinstance(raw, list):
            return []
        return list(
            dict.fromkeys(
                normalized
                for value in raw[-20:]
                if isinstance(value, str)
                and (normalized := value.strip().lstrip("@").casefold())
            )
        )[-12:]

    @staticmethod
    def _checkpoint_retry_count(
        checkpoint: dict[str, Any] | None,
        key: str,
        *,
        instagram_page_fallback: bool = False,
    ) -> int:
        counters: Any = (checkpoint or {}).get("counters") or {}
        for _ in range(6):
            if not isinstance(counters, dict):
                return 0
            value = counters.get(key)
            if (
                not isinstance(value, bool)
                and isinstance(value, int)
                and value >= 0
            ):
                return value
            if (
                instagram_page_fallback
                and counters.get("wait_kind") == "instagram_page"
            ):
                fallback = counters.get("retry_count")
                if (
                    not isinstance(fallback, bool)
                    and isinstance(fallback, int)
                    and fallback >= 0
                ):
                    # Network-wait retry_count is zero based; this helper returns
                    # the number of surface failures already consumed.
                    return fallback + 1
            counters = counters.get("previous_counters")
        return 0

    @staticmethod
    def _checkpoint_progress_marker(
        checkpoint: dict[str, Any] | None,
    ) -> tuple[int, int]:
        """Recover durable discovered/processed counters through wrappers."""

        counters: Any = (checkpoint or {}).get("counters") or {}
        for _ in range(6):
            if not isinstance(counters, dict):
                return (0, 0)
            discovered = counters.get("discovered", counters.get("visible_accounts"))
            processed = counters.get("processed")
            if (
                not isinstance(discovered, bool)
                and isinstance(discovered, int)
                and discovered >= 0
            ):
                return (
                    discovered,
                    processed
                    if not isinstance(processed, bool)
                    and isinstance(processed, int)
                    and processed >= 0
                    else 0,
                )
            counters = counters.get("previous_counters")
        return (0, 0)

    async def _save_profile_local_failure_checkpoint(
        self,
        control: ExecutionControl,
        target: dict[str, Any],
        mode: str,
        checkpoint: dict[str, Any] | None,
        *,
        reason: str,
        message: str,
    ) -> None:
        """Preserve a target checkpoint before isolating one failed window."""

        latest = self.service.get_checkpoint(
            control.owner_user_id,
            control.task_id,
            target["id"],
            mode,
        ) or checkpoint
        await self._await_durable_thread_call(
            self.service.upsert_checkpoint,
            control.owner_user_id,
            control.task_id,
            target["id"],
            mode=mode,
            stage="mode_unavailable",
            cursor={
                "resume_stage": (
                    (latest or {}).get("stage") or "discovering_accounts"
                ),
                "resume_cursor": self._checkpoint_resume_cursor(latest),
            },
            counters={
                "reason": reason,
                "message": message,
                "previous_counters": (latest or {}).get("counters") or {},
            },
            recoverable=True,
        )

    async def _save_candidate_progress_checkpoint(
        self,
        control: ExecutionControl,
        target_id: str,
        mode: str,
        stats: dict[str, int],
        *,
        discovery_complete: bool,
        stage: str = "screening_accounts",
        skipped_posts: list[str] | None = None,
        source_total: int | None = None,
        resume_tail: list[str] | None = None,
        rendered_count: int | None = None,
        progress_epoch: int | None = None,
        pending_relation_usernames: list[str] | None = None,
        automatic_gap_recheck_started: bool = False,
        source_recheck_requested: bool = False,
    ) -> None:
        cursor: dict[str, Any] = {
            "candidate_spool_version": 1,
            "candidate_spool_complete": discovery_complete,
            # A relation source is complete only after the worker confirms the
            # physical list tail. This marker distinguishes current checkpoints
            # from old releases that called a finite-limit stop "completed".
            "candidate_spool_natural_end": bool(
                discovery_complete and mode in {"followers", "following"}
            ),
        }
        if source_recheck_requested:
            cursor["source_recheck_requested"] = True
        if automatic_gap_recheck_started:
            # A per-target/mode budget, written before reopening the source. Keep
            # it on every subsequent producer, consumer and completion checkpoint.
            cursor["automatic_gap_recheck_started"] = True
        if pending_relation_usernames is not None:
            cursor["pending_relation_usernames"] = self._normalize_pending_relation_usernames(pending_relation_usernames)
        if skipped_posts:
            cursor["skipped_posts"] = list(skipped_posts)
        if resume_tail:
            cursor["resume_tail"] = list(
                dict.fromkeys(
                    normalized
                    for value in resume_tail[-20:]
                    if isinstance(value, str)
                    and (normalized := value.strip().lstrip("@").casefold())
                )
            )[-12:]
        if (
            not isinstance(rendered_count, bool)
            and isinstance(rendered_count, int)
            and rendered_count >= 0
        ):
            cursor["rendered_count"] = rendered_count
        if (
            not isinstance(progress_epoch, bool)
            and isinstance(progress_epoch, int)
            and progress_epoch >= 0
        ):
            cursor["progress_epoch"] = progress_epoch
        counters: dict[str, Any] = {
            "visible_accounts": stats["total"],
            "discovered": stats["total"],
            "processed": stats["recorded"] + stats["deduped"],
            "saved": stats["recorded"],
            "skipped_existing": 0,
            "skipped_global_duplicates": stats["deduped"],
            "pending_candidates": stats["pending"],
        }
        if source_total is not None:
            counters["source_total"] = max(0, int(source_total))
        await self._await_durable_thread_call(
            self.service.upsert_checkpoint,
            control.owner_user_id,
            control.task_id,
            target_id,
            mode=mode,
            stage=stage,
            cursor=cursor,
            counters=counters,
            recoverable=True,
        )

    @staticmethod
    async def _await_durable_thread_call(
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Keep SQLite calls owned until their thread finishes and closes handles.

        Cancelling ``asyncio.to_thread`` only cancels the awaiting coroutine; the
        worker thread continues.  In the parallel relation pipeline that could release
        ``checkpoint_lock`` and let the Stop cleanup write a newer cursor before an
        older background upsert finally committed over it. Reads also own open
        connections: joining a cancelled coroutine is not enough before pipeline
        cleanup or database removal. Drain the started call, remember every
        cancellation request, then re-raise cancellation after it settles. SQLite's
        busy timeout bounds lock contention, not the total thread lifetime; never
        abandon an open connection merely because a cleanup deadline elapsed.
        """

        operation = asyncio.create_task(
            asyncio.to_thread(function, *args, **kwargs)
        )
        cancellation: asyncio.CancelledError | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            except Exception:
                # Retrieve the settled failure below so an already requested
                # cancellation is preserved after a late storage error as well.
                break

        try:
            result = operation.result()
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
        if cancellation is not None:
            raise cancellation
        return result

    async def _drain_candidate_spool(
        self,
        control: ExecutionControl,
        worker: Any,
        target_id: str,
        mode: str,
        settings: dict[str, Any],
        *,
        discovery_complete: bool,
        source_total: int | None = None,
        resume_tail: list[str] | None = None,
        rendered_count: int | None = None,
        progress_epoch: int | None = None,
        checkpoint_writer: Callable[[dict[str, int]], Any] | None = None,
        candidate_reservation_lock: asyncio.Lock | None = None,
        candidate_reservations: set[str] | None = None,
        candidate_available: asyncio.Event | _CandidateChangeSignal | None = None,
        capacity_changed: asyncio.Event | None = None,
        candidate_failures: dict[str, BaseException] | None = None,
        on_screening_failure: Callable[[Exception], None] | None = None,
        defer_candidate_failures: bool = False,
        reserve_all_failures: bool = False,
        reconcile_on_entry: bool = True,
    ) -> dict[str, int]:
        """Screen bounded pages; terminal rows are never reopened on recovery."""

        if defer_candidate_failures:
            # Failed reads remain durable pending rows. A single attempt visits
            # each at most once while healthy rows behind them can still finish.
            candidate_reservation_lock = candidate_reservation_lock or asyncio.Lock()
            if candidate_reservations is None:
                candidate_reservations = set(candidate_failures or ())

        async def close_terminal_retained() -> None:
            held = getattr(worker, "_deferred_screening_workers", {})

            async def close_and_forget(username: str, child: Any) -> None:
                release = getattr(worker, 'release_parallel_screening_worker', None)
                pending_close = None if callable(release) and release(child) else child.disconnect()
                if inspect.isawaitable(pending_close):
                    await pending_close
                if held.get(username) is child:
                    held.pop(username, None)

            retained = list(held.items())
            for offset in range(0, len(retained), 100):
                batch = retained[offset:offset + 100]
                terminal = await self._await_durable_thread_call(
                    self.service.terminal_task_mode_candidate_usernames,
                    control.owner_user_id, control.task_id, target_id, mode,
                    [username for username, _ in batch],
                )
                for username, child in batch:
                    if username not in terminal or held.get(username) is not child:
                        continue
                    # A different result/identity reconciliation can finish a row
                    # without revisiting its retained bad page. Live-queue workers
                    # survive target completion, so join this close here instead of
                    # accumulating old pages until the entire worker disconnects.
                    await finish_owned(close_and_forget(username, child))

        async def save_progress(current: dict[str, int]) -> None:
            if checkpoint_writer is not None:
                pending_write = checkpoint_writer(current)
                if inspect.isawaitable(pending_write):
                    await pending_write
                return
            await self._save_candidate_progress_checkpoint(
                control,
                target_id,
                mode,
                current,
                discovery_complete=discovery_complete,
                source_total=source_total,
                resume_tail=resume_tail,
                rendered_count=rendered_count,
                progress_epoch=progress_epoch,
            )

        async def finish_drain(current: dict[str, int]) -> dict[str, int]:
            if defer_candidate_failures and candidate_failures:
                # Individual read failures repair only their stable-ID alias group.
                # Retain one full convergence at the deferred-pass boundary, before
                # publishing pending/error state or allowing the mode to complete.
                current = await self._await_durable_thread_call(
                    self.service.reconcile_task_mode_candidates,
                    control.owner_user_id, control.task_id, target_id, mode,
                )
            await close_terminal_retained()
            return current

        stats = await self._await_durable_thread_call(
            self.service.reconcile_task_mode_candidates if reconcile_on_entry
            else self.service.task_mode_candidate_stats,
            control.owner_user_id,
            control.task_id,
            target_id,
            mode,
        )
        await close_terminal_retained()
        processed_since_checkpoint = 0
        deferred_checkpoint_stats: dict[str, int] | None = None
        after_discovery_order = None
        while stats["pending"]:
            await control.pause_event.wait()
            if control.stop_event.is_set():
                raise asyncio.CancelledError
            reservation_key: str | None = None
            if (
                candidate_reservation_lock is not None
                and candidate_reservations is not None
            ):
                # SQLite remains the durable queue.  This tiny in-process lease only
                # prevents screening tabs owned by this exact window/mode
                # from opening the same still-pending row concurrently.  A crash
                # loses the lease but not the row, so recovery safely retries it.
                async with candidate_reservation_lock:
                    candidate = None
                    while candidate is None:
                        pending_page = await self._await_durable_thread_call(
                            self.service.list_pending_task_mode_candidates,
                            control.owner_user_id,
                            control.task_id,
                            target_id,
                            mode,
                            limit=1,
                            after_discovery_order=after_discovery_order,
                        )
                        candidate = next(
                            (
                                item
                                for item in pending_page
                                if str(
                                    item.get("username_norm")
                                    or item.get("username")
                                    or ""
                                ).casefold()
                                not in candidate_reservations
                            ),
                            None,
                        )
                        if candidate is not None or not pending_page:
                            break
                        after_discovery_order = pending_page[-1]["discovery_order"]
                    if candidate is not None:
                        # Advance through this pass instead of rereading every
                        # deferred prefix for each next row. A new drain starts at
                        # the beginning, so a sibling's released earlier lease is
                        # still retried after this pass; no durable row is skipped.
                        after_discovery_order = candidate["discovery_order"]
                        reservation_key = str(
                            candidate.get("username_norm")
                            or candidate.get("username")
                        ).casefold()
                        candidate_reservations.add(reservation_key)
                        if capacity_changed is not None:
                            capacity_changed.set()
                        candidates = [candidate]
                if candidate is None:
                    # Every remaining row belongs to a sibling or this attempt's
                    # deferred set. A terminal write may also have settled a held page.
                    return await finish_drain(stats)
            else:
                candidates = await self._await_durable_thread_call(
                    self.service.list_pending_task_mode_candidates,
                    control.owner_user_id,
                    control.task_id,
                    target_id,
                    mode,
                    limit=100,
                )
            if not candidates:
                # A concurrent global-result write may have reconciled the final page.
                stats = await self._await_durable_thread_call(
                    self.service.reconcile_task_mode_candidates,
                    control.owner_user_id,
                    control.task_id,
                    target_id,
                    mode,
                )
                break
            try:
                for candidate in candidates:
                    await control.pause_event.wait()
                    if control.stop_event.is_set():
                        raise asyncio.CancelledError
                    username = candidate["username"]
                    claim = await self._await_durable_thread_call(
                        self.service.claim_workbench_identity,
                        control.owner_user_id,
                        username=username,
                        source=mode,
                        source_target=target_id,
                        allow_owned_resume=True,
                    )
                    if claim["duplicate"]:
                        terminal_state = "deduped"
                    else:
                        held = getattr(worker, "_deferred_screening_workers", {})
                        retained = held.get(username.casefold())
                        try:
                            result_saved = await self._screen_and_record(
                                control, retained or worker, target_id, username, mode,
                                settings, claim_id=claim["claim_id"],
                            )
                        except asyncio.CancelledError as exc:
                            current = asyncio.current_task()
                            if control.stop_event.is_set() or (current is not None and current.cancelling()):
                                raise
                            # A cancelled browser reply is not a user/owner Task
                            # cancellation. Keep its candidate reserved through
                            # real page cleanup, let siblings finish other rows,
                            # and report a recoverable page failure for this row.
                            raise PlaywrightWorker._page_recovery_exhausted(
                                WorkerExecutionError(
                                    '资料页读取意外中断，已保留账号等待恢复',
                                    reason='browser_window_surface_unstable',
                                    pause_required=True,
                                ), username,
                            ) from exc
                        if retained is not None:
                            async def close_retried_child() -> None:
                                release = getattr(worker, 'release_parallel_screening_worker', None)
                                pending_close = None if callable(release) and release(retained) else retained.disconnect()
                                if inspect.isawaitable(pending_close):
                                    await pending_close
                                if held.get(username.casefold()) is retained:
                                    held.pop(username.casefold(), None)

                            # Keep the failed page registered through its successful
                            # retry and close, including repeated Stop requests.
                            await finish_owned(close_retried_child())
                        terminal_state = "recorded" if result_saved else "deduped"
                    await self._await_durable_thread_call(
                        self.service.finish_task_mode_candidate,
                        control.owner_user_id,
                        control.task_id,
                        target_id,
                        mode,
                        username,
                        state=terminal_state,
                    )
                    self._record_profile_progress(control, current_stage="screening_accounts")
                    processed_since_checkpoint += 1
                    if processed_since_checkpoint >= 10:
                        stats = await self._await_durable_thread_call(
                            self.service.task_mode_candidate_stats,
                            control.owner_user_id,
                            control.task_id,
                            target_id,
                            mode,
                        )
                        await save_progress(stats)
                        processed_since_checkpoint = 0
            except Exception as exc:
                if on_screening_failure is not None:
                    # Publish lost capacity before durable failure checkpoints can
                    # wait behind a parent activation's checkpoint lock.
                    on_screening_failure(exc)
                if discovery_complete and isinstance(exc, WorkerExecutionError):
                    # Every later retry still belongs to this pending candidate.
                    # The first parallel failure already marked natural source end;
                    # a second/third child failure must not reopen the source list.
                    exc.details["source_discovery_complete"] = True
                    exc.details.setdefault("candidate_username", username.casefold())
                    exc.details.setdefault("recovery_target", username.casefold())
                if candidate_failures is not None and reservation_key and (
                    reserve_all_failures or self._is_candidate_page_failure(exc)
                ):
                    # Keep this row reserved/pending for explicit handling while
                    # siblings process other rows. An unexpected child exception
                    # must not release its username before native cleanup settles
                    # or send every sibling through the same broken account.
                    if isinstance(exc, WorkerExecutionError):
                        exc.details["recovery_target"] = reservation_key
                        exc.details["candidate_username"] = reservation_key
                    candidate_failures[reservation_key] = exc
                stats = await self._await_durable_thread_call(
                    self.service.reconcile_task_mode_candidates,
                    control.owner_user_id,
                    control.task_id,
                    target_id,
                    mode,
                    usernames=[username],
                )
                if defer_candidate_failures and reservation_key in (candidate_failures or {}):
                    # Failed reads stay pending durably; another identical
                    # failed-row checkpoint contains no new progress. Slow FULL
                    # commits must not multiply with a long unreadable prefix.
                    # Keep the first observation and every complete stats change;
                    # source/cursor updates have their own producer writer, and
                    # the caller still saves the final error boundary explicitly.
                    if deferred_checkpoint_stats != stats:
                        await save_progress(stats)
                        deferred_checkpoint_stats = dict(stats)
                    continue
                await save_progress(stats)
                raise
            finally:
                if (
                    reservation_key is not None
                    and candidate_reservation_lock is not None
                    and candidate_reservations is not None
                ):
                    async with candidate_reservation_lock:
                        if candidate_failures is None or reservation_key not in candidate_failures:
                            candidate_reservations.discard(reservation_key)
                    if candidate_available is not None:
                        candidate_available.set()
                    if capacity_changed is not None:
                        capacity_changed.set()
            stats = await self._await_durable_thread_call(
                self.service.task_mode_candidate_stats,
                control.owner_user_id,
                control.task_id,
                target_id,
                mode,
            )
        return await finish_drain(stats)

    async def _execute_candidate_spooled_mode(
        self, control, worker, target, mode, settings, checkpoint, *, parent_reels_eligible=False,
    ):
        profile_id = getattr(control, 'profile_id', None)
        if profile_id:
            await self._manual_pause_checkpoint(control, profile_id)
            control.manual_mode_active.add(profile_id)
        try:
            return await self._execute_candidate_spooled_mode_body(
                control, worker, target, mode, settings, checkpoint,
                parent_reels_eligible=parent_reels_eligible)
        finally:
            if profile_id:
                control.manual_mode_active.discard(profile_id)

    async def _execute_candidate_spooled_mode_body(
        self,
        control: ExecutionControl,
        worker: Any,
        target: dict[str, Any],
        mode: str,
        settings: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        *, parent_reels_eligible: bool = False,
    ) -> dict[str, Any]:
        """Discover durably and screen relations using a bounded child-tab pool.

        The SQLite spool remains the only candidate queue. A broadcast signal
        announces durable changes, while the child consumers repeatedly claim one
        pending row through a shared in-process reservation.  No username batches
        accumulate in memory, and a process exit simply leaves the SQLite row
        pending for the next recovery pass.
        """

        collection_platform(settings)
        target_id = target["id"]
        profile_id = getattr(control, "profile_id", None)
        lease_token = control.leases.get(profile_id)
        restored_request = None
        if profile_id and lease_token and mode in {"followers", "following"}:
            restored_request = await self._await_durable_thread_call(
                self.service.restore_pending_source_recheck,
                control.owner_user_id, control.task_id, target_id, mode,
                profile_id=profile_id, lease_token=lease_token,
            )
        batch_discovery = getattr(worker, "collection_pipeline", None) == "r59-batch"
        direct_handoff = mode in {"followers", "following"} and bool(
            getattr(worker, "supports_single_candidate_handoff", False)
        )
        reservation_lock = asyncio.Lock()
        reservations: set[str] = set()
        candidate_limit = self._candidate_spool_limit(mode, settings)
        discovery_complete = self._candidate_spool_complete(
            checkpoint,
            require_natural_end=mode in {"followers", "following"},
        )
        source_total = self._checkpoint_source_total(checkpoint)
        resume_tail = self._checkpoint_resume_tail(checkpoint)
        resume_cursor = self._checkpoint_resume_cursor(checkpoint)
        automatic_gap_recheck_started = (
            resume_cursor.get("automatic_gap_recheck_started") is True
            # An explicit source recheck is already the supplemental pass. It
            # must not chain another automatic pass, including after a restart.
            or resume_cursor.get("source_recheck_requested") is True
        )
        pending_relation_usernames = self._normalize_pending_relation_usernames(
            resume_cursor.get("pending_relation_usernames")
        ) if mode in {"followers", "following"} and settings.get("platform", "instagram") == "instagram" else []
        if pending_relation_usernames:
            confirmed_names = await self._await_durable_thread_call(
                self.service.confirmed_task_mode_candidate_usernames,
                control.owner_user_id, control.task_id, target_id, mode, pending_relation_usernames,
            )
            pending_relation_usernames = [name for name in pending_relation_usernames if name not in confirmed_names]
            if not pending_relation_usernames:
                discovery_complete = self._candidate_spool_complete(
                    {"cursor": {**resume_cursor, "pending_relation_usernames": []}},
                    require_natural_end=mode in {"followers", "following"},
                )
        rendered_count = resume_cursor.get("rendered_count")
        if isinstance(rendered_count, bool) or not isinstance(rendered_count, int):
            rendered_count = None
        progress_epoch = resume_cursor.get("progress_epoch")
        if isinstance(progress_epoch, bool) or not isinstance(progress_epoch, int):
            progress_epoch = None
        stats = await self._await_durable_thread_call(
            self.service.reconcile_task_mode_candidates,
            control.owner_user_id,
            control.task_id,
            target_id,
            mode,
        )
        skipped_posts: list[str] = []
        checkpoint_lock = asyncio.Lock()
        last_checkpoint_total = stats["total"]
        candidate_available = _CandidateChangeSignal()
        capacity_changed = asyncio.Event()
        active_screeners: set[int] = set()
        capacity_failures: list[Exception] = []
        pending_child_failures: dict[int, Exception] = {}
        live_recheck: dict[str, Any] | None = ({"target": restored_request,
            "requested_at": restored_request["source_recheck_requested_at"],
            "pending": restored_request["waiting_for_safe_point"]}
            if restored_request else None)
        live_session: dict[str, Any] | None = None

        def parent_navigation_failure() -> BaseException | None:
            return next((exc for exc in [*pending_child_failures.values(), *capacity_failures,
                                        *candidate_failures.values()]
                         if not self._is_isolated_screening_page_failure(exc)), None)

        def screening_capacity_failure() -> BaseException | None:
            # mark_child_failure publishes before durable failure bookkeeping
            # yields. Source wakeups must observe that same cause immediately,
            # rather than inventing a generic failure until consume's except
            # later appends capacity_failures. Unknown/account-wide failures
            # must not inherit a sibling's isolated-page automatic retry.
            blocking = parent_navigation_failure()
            if blocking is not None:
                return blocking
            return next(iter([*pending_child_failures.values(), *capacity_failures]), None)

        async def wait_for_screening_capacity() -> None:
            # Pending includes both reserved and not-yet-reserved durable rows.
            # A child frees capacity only after its result is committed. Restored
            # backlogs drain before the source admits any new hover-approved row.
            while True:
                await self._manual_worker_checkpoint(control)
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    raise asyncio.CancelledError
                capacity_changed.clear()
                current = await self._await_durable_thread_call(self.service.task_mode_candidate_stats,
                    control.owner_user_id, control.task_id, target_id, mode)
                if not active_screeners:
                    failure = screening_capacity_failure()
                    if failure is not None:
                        raise failure
                    raise WorkerExecutionError('筛选子页面暂不可用，已保留待采账号',
                        reason='instagram_page_recovery_exhausted', pause_required=True)
                # Retained failed pages are already part of parallel_children
                # in this attempt; count stable page identities exactly once.
                capacity = len({id(child) for child in [*parallel_children, *held.values()]})
                if current['pending'] < capacity:
                    return
                try:
                    await asyncio.wait_for(capacity_changed.wait(), timeout=.5)
                except asyncio.TimeoutError:
                    pass  # Observe Pause/Stop even if all pages are still busy.

        async def check_source_controls() -> None:
            await self._manual_worker_checkpoint(control)
            await control.pause_event.wait()
            if control.stop_event.is_set():
                raise asyncio.CancelledError
            if live_recheck is not None and not live_recheck.get("pending"):
                blocking_failure = parent_navigation_failure()
                if blocking_failure is not None:
                    raise blocking_failure
            if not active_screeners:
                failure = screening_capacity_failure()
                if failure is not None:
                    if isinstance(failure, WorkerExecutionError):
                        failure.details['recovery_scope'] = 'screening_child'
                    raise failure
                failure = PlaywrightWorker._page_recovery_exhausted(WorkerExecutionError(
                    '筛选子页尚未就绪，已保留当前名单位置',
                    reason='browser_window_surface_unstable'), target["username"])
                failure.details['recovery_scope'] = 'screening_child'
                raise failure

        async def wait_for_empty_handoff() -> None:
            while True:
                await check_source_controls()
                capacity_changed.clear()
                async with reservation_lock:
                    pending = await self._await_durable_thread_call(
                        self.service.list_pending_task_mode_candidates,
                        control.owner_user_id, control.task_id, target_id, mode,
                        limit=len(reservations) + 1,
                    )
                    waiting = any(row['username_norm'] not in reservations for row in pending)
                # The queue read yields to Pause/manual intervention. Recheck
                # outside the reservation lock before admitting another identity.
                await self._manual_worker_checkpoint(control)
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    raise asyncio.CancelledError
                if not waiting:
                    return
                try:
                    await asyncio.wait_for(capacity_changed.wait(), timeout=.5)
                except asyncio.TimeoutError:
                    pass

        async def write_progress_unlocked(
            current: dict[str, int],
            *,
            complete: bool | None = None,
            stage: str = "discovering_accounts",
        ) -> None:
            await self._save_candidate_progress_checkpoint(
                control,
                target_id,
                mode,
                current,
                discovery_complete=discovery_complete if complete is None else complete,
                stage=stage,
                skipped_posts=skipped_posts,
                source_total=source_total,
                resume_tail=resume_tail,
                rendered_count=rendered_count,
                progress_epoch=progress_epoch,
                pending_relation_usernames=pending_relation_usernames,
                automatic_gap_recheck_started=automatic_gap_recheck_started,
                source_recheck_requested=live_recheck is not None and not live_recheck.get("pending"),
            )

        async def write_progress(
            current: dict[str, int],
            *,
            complete: bool | None = None,
            stage: str = "discovering_accounts",
        ) -> None:
            # Producer progress and the screening tab can checkpoint at the same
            # time.  Serialize them and read the shared resume fields only after
            # taking the lock, so an older screening counter write can never erase
            # a newer relation tail/epoch.
            async with checkpoint_lock:
                # The caller may have waited behind another writer after taking
                # its snapshot. Read the maintained counters under the same lock
                # as the checkpoint write so an older producer snapshot cannot
                # roll processed/saved counts backwards after screening progresses.
                current = await self._await_durable_thread_call(
                    self.service.task_mode_candidate_stats,
                    control.owner_user_id, control.task_id, target_id, mode,
                )
                await write_progress_unlocked(
                    current,
                    complete=complete,
                    stage=stage,
                )

        async def progress_sink(payload: dict[str, Any]) -> None:
            nonlocal source_total, last_checkpoint_total
            nonlocal resume_tail, rendered_count, progress_epoch, pending_relation_usernames
            await control.pause_event.wait()
            if control.stop_event.is_set():
                raise asyncio.CancelledError
            if isinstance(payload.get("source_profile"), dict):
                await self._await_durable_thread_call(
                    self.service.capture_target_source_profile,
                    control.owner_user_id, control.task_id, target_id, payload["source_profile"],
                )
            async with checkpoint_lock:
                if ("pending_relation_usernames" in payload and mode in {"followers", "following"}
                        and settings.get("platform", "instagram") == "instagram"):
                    if not isinstance(payload["pending_relation_usernames"], list):
                        raise WorkerExecutionError("待确认列表身份必须是完整列表，已保留原检查点",
                                                   reason="instagram_relation_checkpoint_invalid", pause_required=True)
                    pending_relation_usernames = self._normalize_pending_relation_usernames(payload["pending_relation_usernames"])
                observed_total = payload.get("source_total")
                if (
                    not isinstance(observed_total, bool)
                    and isinstance(observed_total, int)
                    and observed_total >= 0
                ):
                    source_total = observed_total
                observed_tail = payload.get("resume_tail")
                if isinstance(observed_tail, list):
                    resume_tail = list(
                        dict.fromkeys(
                            normalized
                            for value in observed_tail[-20:]
                            if isinstance(value, str)
                            and (
                                normalized := value.strip().lstrip("@").casefold()
                            )
                        )
                    )[-12:]
                observed_rendered = payload.get("rendered_count")
                if (
                    not isinstance(observed_rendered, bool)
                    and isinstance(observed_rendered, int)
                    and observed_rendered >= 0
                ):
                    rendered_count = observed_rendered
                observed_epoch = payload.get("progress_epoch")
                if (
                    not isinstance(observed_epoch, bool)
                    and isinstance(observed_epoch, int)
                    and observed_epoch >= 0
                ):
                    progress_epoch = observed_epoch
                current = await self._await_durable_thread_call(
                    self.service.task_mode_candidate_stats,
                    control.owner_user_id,
                    control.task_id,
                    target_id,
                    mode,
                )
                last_checkpoint_total = max(last_checkpoint_total, current["total"])
                await write_progress_unlocked(
                    current,
                    stage="discovering_accounts",
                )

        async def legacy_candidate_sink(batch: list[str], previews: dict[str, dict] | None = None) -> dict[str, Any]:
            nonlocal last_checkpoint_total
            # The source dialog is still open here. A confirmed hover-card count
            # can be excluded before any screening child opens the account page.
            # Persist it first; append reconciles the same username to a terminal
            # spool row even if the process is interrupted between these writes.
            if previews is not None:
                await self._record_hover_exclusions(
                    control, target_id, mode, settings, batch, previews
                )
            # Persist the worker's bounded batch before observing Pause/Stop.  Once
            # SQLite commits it, one event wake is enough regardless of how many
            # batches arrive before a child consumer runs.
            result = await self._await_durable_thread_call(
                self.service.append_task_mode_candidates,
                control.owner_user_id,
                control.task_id,
                target_id,
                mode,
                batch,
                max_total=candidate_limit,
            )
            candidate_available.set()
            if result["total"] > last_checkpoint_total:
                self._record_profile_progress(control, current_stage="collecting_list")
            if result["total"] != last_checkpoint_total:
                await write_progress(result, stage="discovering_accounts")
                last_checkpoint_total = max(last_checkpoint_total, result["total"])
            await control.pause_event.wait()
            if control.stop_event.is_set():
                raise asyncio.CancelledError
            return result

        async def candidate_sink(batch: list[str], previews: dict[str, dict] | None = None) -> dict[str, Any]:
            nonlocal last_checkpoint_total
            if not direct_handoff:
                return await legacy_candidate_sink(batch, previews)
            if previews is not None:
                raise WorkerExecutionError('直接采集不应提交悬浮卡结果', reason='collection_handoff_protocol_error')
            result = await self._await_durable_thread_call(self.service.task_mode_candidate_stats,
                control.owner_user_id, control.task_id, target_id, mode)
            checkpoint_pending = False

            async def flush_discovery_progress() -> None:
                nonlocal checkpoint_pending, last_checkpoint_total
                if not checkpoint_pending:
                    return
                async with checkpoint_lock:
                    # A cancelled durable call may have committed without
                    # returning its counters. Read committed state under the same
                    # lock as the checkpoint, including any child progress.
                    current = await self._await_durable_thread_call(
                        self.service.task_mode_candidate_stats,
                        control.owner_user_id, control.task_id, target_id, mode,
                    )
                    if current['total'] > last_checkpoint_total:
                        await write_progress_unlocked(current, stage='discovering_accounts')
                        last_checkpoint_total = max(last_checkpoint_total, current['total'])
                checkpoint_pending = False

            # The factory does no I/O: ownership exists before the first
            # cancellable thread submission can open a connection. Old service
            # adapters retain their original per-call path.
            session_factory = getattr(self.service, '_new_discovery_write_session', None)
            write_session = session_factory() if batch_discovery and callable(session_factory) else None
            try:
                for username in batch:
                    if batch_discovery:
                        # Keep every recognition-time reservation durable before
                        # checking the next identity. Only display checkpoints
                        # are coalesced; Pause still stops between durable rows.
                        if not control.pause_event.is_set():
                            await finish_owned(flush_discovery_progress())
                        await check_source_controls()
                        checkpoint_pending = True
                    else:
                        await wait_for_empty_handoff()
                    result = await self._await_durable_thread_call(
                        self.service.discover_task_mode_candidate,
                        control.owner_user_id, control.task_id, target_id, mode, username,
                        **({'_session': write_session} if write_session is not None else {}),
                    )
                    candidate_available.set()
                    if batch_discovery:
                        checkpoint_pending = result['total'] > last_checkpoint_total
                    if result['total'] > last_checkpoint_total:
                        self._record_profile_progress(control, current_stage='collecting_list')
                        if not batch_discovery:
                            await write_progress(result, stage='discovering_accounts')
                            last_checkpoint_total = max(last_checkpoint_total, result['total'])
                    if not batch_discovery:
                        # Legacy adapters retain their single waiting handoff;
                        # production batch discovery does not wait for a child.
                        await wait_for_empty_handoff()
            finally:
                try:
                    if batch_discovery:
                        # Flush once on return, error or Stop, even if repeated
                        # cancellation arrives while a database thread is committing.
                        await finish_owned(flush_discovery_progress())
                finally:
                    # Every admitted write is drained above. Close even if the
                    # checkpoint fails or receives another cancellation, before
                    # this callback can release its parent/lease ownership.
                    if write_session is not None:
                        await finish_owned(asyncio.to_thread(write_session.close))
            return result

        async def collect_source() -> Any:
            nonlocal source_total, skipped_posts, discovery_complete
            nonlocal automatic_gap_recheck_started, resume_tail, rendered_count, progress_epoch
            async def checkpoint():
                await self._manual_worker_checkpoint(control)
                await control.pause_event.wait()
                if control.stop_event.is_set():
                    raise asyncio.CancelledError
                if parallel_children and (direct_handoff or live_recheck is not None and not live_recheck.get("pending")):
                    await check_source_controls()
            previous = getattr(worker, "collection_checkpoint", None)
            previous_duplicate_check = getattr(worker, "hover_duplicate_check", None)
            previous_capacity_check = getattr(worker, "hover_capacity_checkpoint", None)
            previous_pending_confirmed = getattr(worker, "relation_pending_confirmed_usernames", None)
            worker.collection_checkpoint = checkpoint
            async def duplicate_check(username: str) -> bool:
                result = await self._await_durable_thread_call(
                    self.service.should_skip_relationship_hover,
                    control.owner_user_id, username,
                    source=mode, source_target=target_id,
                )
                return bool(result)
            async def confirmed_pending(names: list[str]) -> list[str]:
                validated = self._normalize_pending_relation_usernames(names)
                confirmed = await self._await_durable_thread_call(
                    self.service.confirmed_task_mode_candidate_usernames,
                    control.owner_user_id, control.task_id, target_id, mode, validated,
                )
                return [name for name in validated if name in confirmed]
            if mode in {'followers', 'following'}:
                worker.relation_pending_confirmed_usernames = confirmed_pending
                worker.hover_duplicate_check = duplicate_check
                worker.hover_capacity_checkpoint = wait_for_screening_capacity if parallel_children else None
            pass_candidate_count = (await self._await_durable_thread_call(
                self.service.task_mode_candidate_stats,
                control.owner_user_id, control.task_id, target_id, mode))["total"]
            try:
                while True:
                    # Initial collection and its optional one supplemental pass
                    # share this producer, worker, child pool and window lease.
                    await checkpoint()
                    outcome = await self._collect_mode(
                        worker, target["username"], mode, settings,
                        candidate_sink=candidate_sink, initial_candidate_count=pass_candidate_count,
                        progress_sink=progress_sink, initial_resume_tail=resume_tail,
                        initial_pending_relation_usernames=pending_relation_usernames,
                    )
                    # Compatibility workers can still return one list. Commit it
                    # through the same deduplicating sink before measuring the gap.
                    usernames = list(getattr(outcome, "usernames", []) or [])
                    for offset in range(0, len(usernames), 100):
                        await candidate_sink(usernames[offset : offset + 100])
                    skipped_posts = list(getattr(outcome, "skipped_posts", []) or [])
                    observed_source_total = getattr(outcome, "source_total", None)

                    async def persist_source_return() -> bool:
                        nonlocal source_total, discovery_complete, pass_candidate_count
                        nonlocal automatic_gap_recheck_started, resume_tail, rendered_count, progress_epoch
                        # A normal relation return already proves physical bottom
                        # and exhausted loading. Never infer this from counts or
                        # progress events; exceptions cannot reach this boundary.
                        async with checkpoint_lock:
                            if (
                                not isinstance(observed_source_total, bool)
                                and isinstance(observed_source_total, int)
                                and observed_source_total >= 0
                            ):
                                source_total = observed_source_total
                            current = await self._await_durable_thread_call(
                                self.service.task_mode_candidate_stats,
                                control.owner_user_id, control.task_id, target_id, mode,
                            )
                            pass_candidate_count = current["total"]
                            source_finished = not pending_relation_usernames and not (
                                mode == "post_likers" and skipped_posts
                            )
                            recheck = bool(
                                source_finished and mode in {"followers", "following"}
                                and not automatic_gap_recheck_started and (live_recheck is None or live_recheck.get("pending"))
                                and source_total is not None and source_total > current["total"]
                            )
                            discovery_complete = source_finished and not recheck
                            if recheck:
                                # Consume before navigation, not after it. A crash,
                                # Pause or Stop now resumes this same pass, never a
                                # third gap pass. Clear only location hints; keep all
                                # durable identities, results and screening claims.
                                automatic_gap_recheck_started = True
                                resume_tail = []
                                rendered_count = None
                                progress_epoch = None
                            await write_progress_unlocked(
                                current, complete=discovery_complete,
                                stage="discovering_accounts" if recheck else "screening_accounts",
                            )
                            return recheck

                    # Once the source has returned, join the gap decision and its
                    # durable write even on cancellation. Completion or the spent
                    # pass budget must survive the Stop/checkpoint-lock boundary.
                    recheck = await finish_owned(persist_source_return())
                    if pending_relation_usernames:
                        raise WorkerExecutionError("仍有已观察但未确认的列表身份，已保留来源等待恢复",
                                                   reason="instagram_relation_unconfirmed_rows", pause_required=True)
                    if not recheck:
                        return outcome
            finally:
                worker.collection_checkpoint = previous
                if mode in {'followers', 'following'}:
                    worker.hover_duplicate_check = previous_duplicate_check
                    worker.hover_capacity_checkpoint = previous_capacity_check
                    worker.relation_pending_confirmed_usernames = previous_pending_confirmed

        failed_child_ids: set[int] = set()

        async def close_child(child: Any) -> None:
            if child is None or child is worker:
                return
            release = getattr(worker, 'release_parallel_screening_worker', None)
            if id(child) not in failed_child_ids and callable(release) and release(child):
                return
            profile_id = getattr(control, 'profile_id', None)
            request = control.manual_requests.get(profile_id) if profile_id else None
            if request and request.get('state') != 'resuming' and not control.stop_event.is_set():
                request['children'][id(child)] = child
                return
            disconnect = getattr(child, "disconnect", None)
            if not callable(disconnect):
                return
            try:
                pending_close = disconnect()
                if inspect.isawaitable(pending_close):
                    await pending_close
                if id(child) in failed_child_ids:
                    join_cleanup = getattr(child, 'wait_for_cleanup', None)
                    if callable(join_cleanup):
                        await join_cleanup()
            except Exception:
                # Child pages are accelerators. Their bounded worker cleanup has
                # already detached ownership; a close error must not mask the
                # durable producer/checkpoint result.
                return

        async def create_child_with_stop(factory: Callable[[], Any]) -> Any:
            async def invoke() -> Any:
                pending_child = factory()
                if inspect.isawaitable(pending_child):
                    return await pending_child
                return pending_child

            creation = asyncio.create_task(invoke())
            stop_wait = asyncio.create_task(control.stop_event.wait())

            async def abandon_creation() -> None:
                if not creation.done():
                    creation.cancel()
                created = await asyncio.gather(creation, return_exceptions=True)
                if created and not isinstance(created[0], BaseException):
                    await close_child(created[0])
                if not stop_wait.done():
                    stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)

            try:
                done, _ = await asyncio.wait(
                    {creation, stop_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_wait in done and control.stop_event.is_set():
                    raise asyncio.CancelledError
                child = await creation
                if not stop_wait.done():
                    stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)
                return child
            except BaseException:
                # stop()/shutdown() also cancel the owning worker task directly.
                # Join creation and close a late child before releasing its window.
                await finish_owned(abandon_creation())
                raise

        parallel_children: list[Any] = []
        child_creation_failures: list[Exception] = []
        closed_child_ids: set[int] = set()
        candidate_failures: dict[str, BaseException] = {}
        source_list_error: WorkerExecutionError | None = None

        async def can_drain_source_error(error: WorkerExecutionError) -> bool:
            # An unconfirmed source list says nothing about already-open profile
            # pages. Finish their durable queue before retrying discovery instead
            # of cancelling healthy reads. Account-wide guards and child failures
            # still interrupt the entire window immediately.
            if error.details.get("recovery_scope") == "screening_child":
                return False
            list_error = self._is_relationship_list_incomplete_error(error) or (
                error.code == "instagram_page_recovery_exhausted" and error.details.get("original_reason") in {
                "instagram_followers_list_incomplete", "instagram_following_list_incomplete",
                "instagram_followers_list_not_rendered", "instagram_following_list_not_rendered",
            })
            if not list_error:
                return False
            current = await self._await_durable_thread_call(self.service.task_mode_candidate_stats,
                control.owner_user_id, control.task_id, target_id, mode)
            # With no queue to drain, preserve the existing error path. Writing
            # an empty screening checkpoint would invent a last-success time for
            # a list that failed before discovering or processing any account.
            return current['pending'] > 0

        held = getattr(worker, "_deferred_screening_workers", None)
        if held is None:
            held = worker._deferred_screening_workers = {}

        async def close_child_once(child: Any) -> None:
            child_id = id(child)
            if child_id in closed_child_ids or any(value is child for value in held.values()):
                return
            closed_child_ids.add(child_id)
            # The child adapter need not implement its own cancellation barrier.
            # Keep this owner alive until an already-started close really settles.
            await finish_owned(close_child(child))

        create_parallel_child = getattr(
            worker, "create_parallel_screening_worker", None
        )
        parallel_supported = bool(
            (restored_request is not None or not discovery_complete or direct_handoff and stats["pending"] > 0)
            and mode in {"followers", "following"}
            and getattr(worker, "supports_parallel_screening_tab", False)
            and callable(create_parallel_child)
        )
        if parallel_supported:
            try:
                configured_children = settings.get("parallel_screening_workers", 1)
                parallel_child_count = (
                    min(configured_children, 3)
                    if isinstance(configured_children, int)
                    and not isinstance(configured_children, bool)
                    and configured_children >= 1
                    else 1
                )
                # A failed source can resume while its unread child pages are
                # still retained. They continue to occupy their selected slots;
                # never add a fresh full set on top of those owned pages.
                retained_child_count = len({id(child) for child in held.values()})
                # Use only the remaining isolated screening slots. Durable claims and
                # the shared reservation set keep each username single-owner even
                # when multiple screening pages are active at the same time.
                for _ in range(max(0, parallel_child_count - retained_child_count)):
                    await self._manual_worker_checkpoint(control)
                    try:
                        child = await create_child_with_stop(create_parallel_child)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Account/transport ownership errors are authoritative,
                        # even if another child was created successfully. Do not
                        # disguise a login guard or disconnected context as an
                        # ordinary page-readiness retry on a healthy parent.
                        if self._is_parent_profile_failure(exc):
                            raise
                        child_creation_failures.append(exc)
                        continue
                    if child is None or child is worker:
                        if child is not None and child is not worker:
                            await close_child(child)
                        continue
                    async def child_manual_checkpoint():
                        await self._manual_worker_checkpoint(control)
                    try:
                        child.manual_control_checkpoint = child_manual_checkpoint
                    except (AttributeError, TypeError):
                        pass
                    parallel_children.append(child)
            except BaseException:
                await finish_owned(asyncio.gather(
                    *(close_child_once(child) for child in parallel_children),
                    return_exceptions=True,
                ))
                raise

        if direct_handoff and not parallel_children and (not discovery_complete or stats['pending']):
            causes = child_creation_failures or [WorkerExecutionError(
                '筛选子页未能创建，父页已保留位置等待恢复',
                reason='browser_window_surface_unstable')]
            failures = [PlaywrightWorker._page_recovery_exhausted(cause, target["username"])
                        for cause in causes]
            # A known transient failure in one slot cannot grant automatic retry
            # to an unclassified failure observed while opening another slot.
            error = next((failure for failure in failures
                          if not self._is_instagram_surface_retry_error(failure)), failures[0])
            error.details['recovery_scope'] = 'screening_child'
            raise error

        if not discovery_complete and not parallel_children:
            # The source has not moved yet, so failure to create every child is a
            # lossless optimization miss. Preserve the established one-tab order:
            # drain recovered rows, collect, then drain newly discovered rows.
            stats = await self._drain_candidate_spool(
                control,
                worker,
                target_id,
                mode,
                settings,
                discovery_complete=False,
                source_total=source_total,
                resume_tail=resume_tail,
                rendered_count=rendered_count,
                progress_epoch=progress_epoch,
                checkpoint_writer=lambda current: write_progress(
                    current, stage="screening_accounts"
                ),
                candidate_failures=candidate_failures,
                defer_candidate_failures=True,
                reconcile_on_entry=False,
            )
            try:
                await collect_source()
            except WorkerExecutionError as exc:
                if not await can_drain_source_error(exc):
                    raise
                # No screening child was created. Still drain the verified
                # prefix just appended by the source before retrying this list.
                source_list_error = exc
            else:
                discovery_complete = not (mode == "post_likers" and skipped_posts)
            stats = await self._await_durable_thread_call(
                self.service.reconcile_task_mode_candidates,
                control.owner_user_id,
                control.task_id,
                target_id,
                mode,
            )
            await write_progress(
                stats,
                complete=discovery_complete,
                stage="screening_accounts",
            )

        elif parallel_children and (restored_request is not None or not discovery_complete or direct_handoff):
            active_screeners.update(id(child) for child in parallel_children)
            # Retained pages belong to their exact pending usernames. New
            # consumers must not open those usernames on another page, overwrite
            # their cleanup owner, or race their read. The parent retries them
            # through the retained mapping after the source/children have joined.
            retained_reservations = set(held)
            reservations.update(retained_reservations)
            producer_done = asyncio.Event()
            producer_task: asyncio.Task[Any] | None = None
            parent_reels_task: asyncio.Task[Any] | None = None
            consumer_tasks: list[asyncio.Task[Any]] = []
            stop_wait: asyncio.Task[bool] | None = None
            intervention: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()
            manual_yielded = False
            recheck_available = asyncio.Event()
            if live_recheck is not None and live_recheck.get("pending"):
                recheck_available.set()
            profile_id = getattr(control, "profile_id", None)
            lease_token = control.leases.get(profile_id)
            parent_page = getattr(worker, "page", None)

            async def request_live_recheck():
                nonlocal live_recheck, discovery_complete, resume_tail, rendered_count, progress_epoch
                async with checkpoint_lock:
                    if (not live_session or not live_session["accepting"]
                            or self._runs.get(control.task_id) is not getattr(control, "_shared", control)
                            or control.stop_event.is_set() or control.tearing_down
                            or control.leases.get(profile_id) != lease_token
                            or getattr(worker, "page", None) is not parent_page
                            or getattr(worker, "_page_stage_abandoned", False)
                            or control.profile_workers.get(profile_id) is not worker):
                        raise ConflictError("原父页已结束或正在清理，未重置来源")
                    if live_recheck is not None:
                        return live_recheck["target"]
                    if intervention.done():
                        raise ConflictError("窗口需要处理登录或安全提示，请处理后继续复查")
                    if source_list_error is not None:
                        raise ConflictError("来源名单读取尚未恢复，请先继续当前来源")
                    if parent_navigation_failure() is not None:
                        raise ConflictError("筛选页面出现未确认故障，已保留当前任务，请先恢复窗口")
                    if (producer_task is None or not active_screeners
                            or not any(not child.done() for child in consumer_tasks)):
                        raise ConflictError("筛选子页均已结束或正在恢复，请先继续当前任务")
                    # Serialize admission with child exit and source checkpoints.
                    # Persist intent only; the old producer may still be returning.
                    was_done = producer_done.is_set()
                    producer_done.clear()
                    try:
                        result = await self._await_durable_thread_call(
                            self.service.recheck_task_source,
                            control.owner_user_id, control.task_id, target_id, mode,
                            profile_id=profile_id, lease_token=lease_token, live_parent=True, queue_only=True)
                    except BaseException:
                        if was_done:
                            producer_done.set()
                        candidate_available.set()
                        raise
                    live_recheck = {"target": result, "requested_at": result["source_recheck_requested_at"], "pending": True}
                    live_session["parent_activity"] = "waiting_safe_point"
                    recheck_available.set()
                    candidate_available.set()
                    return result

            async def producer() -> Any:
                nonlocal manual_yielded
                try:
                    try:
                        result = None if discovery_complete else await collect_source()
                    except Exception:
                        # A manually closed page must not trigger recovery or
                        # sibling teardown while its account is paused.
                        await control.pause_event.wait()
                        raise
                    await control.pause_event.wait()
                    return result
                except _ManualControlYield:
                    manual_yielded = True
                    return None
                finally:
                    producer_done.set()
                    candidate_available.set()

            async def consume(child: Any) -> Exception | None:
                nonlocal manual_yielded
                def mark_child_failure(exc: Exception) -> None:
                    pending_child_failures[id(child)] = exc
                    if direct_handoff and self._is_isolated_screening_page_failure(exc):
                        # Publish retirement with lost capacity, before durable
                        # failure writes yield. The source may abort and cancel
                        # this consumer before its outer except block executes.
                        failed_child_ids.add(id(child))
                    if self._is_parent_profile_failure(exc) and not intervention.done():
                        intervention.set_result(exc)
                    active_screeners.discard(id(child))
                    capacity_changed.set()
                try:
                    while True:
                        await control.pause_event.wait()
                        if control.stop_event.is_set():
                            raise asyncio.CancelledError
                        await self._drain_candidate_spool(
                            control,
                            child,
                            target_id,
                            mode,
                            settings,
                            discovery_complete=False,
                            checkpoint_writer=lambda current: write_progress(
                                current, stage="screening_accounts"
                            ),
                            candidate_reservation_lock=reservation_lock,
                            candidate_reservations=reservations,
                            candidate_available=candidate_available,
                            capacity_changed=capacity_changed,
                            candidate_failures=candidate_failures,
                            on_screening_failure=mark_child_failure,
                            reserve_all_failures=batch_discovery,
                            # The mode entry and bounded append transactions already
                            # reconcile these rows. Waking a drained consumer needs
                            # only the maintained counters, not another full write.
                            reconcile_on_entry=False,
                        )

                        # Subscribe before re-reading SQLite and the local leases.
                        # A sibling cannot erase this notification, even if it
                        # finishes before the waiter receives its first time slice.
                        available_signal = candidate_available.subscribe()
                        current = await self._await_durable_thread_call(
                            self.service.task_mode_candidate_stats,
                            control.owner_user_id,
                            control.task_id,
                            target_id,
                            mode,
                        )
                        async with reservation_lock:
                            reserved_count = len(reservations)
                        if current["pending"] > reserved_count:
                            continue
                        async with checkpoint_lock:
                            if (producer_done.is_set() and not recheck_available.is_set()
                                    and current["pending"] <= len(retained_reservations | candidate_failures.keys())):
                                return None
                        available_wait = asyncio.create_task(
                            available_signal.wait()
                        )
                        local_stop_wait = asyncio.create_task(
                            control.stop_event.wait()
                        )
                        profile_id = getattr(control, 'profile_id', None)
                        manual_event = control.manual_events.get(profile_id)
                        manual_wait = asyncio.create_task(manual_event.wait()) if manual_event else None
                        signals = {available_wait, local_stop_wait}
                        if manual_wait is not None:
                            signals.add(manual_wait)
                        async def settle_waiters() -> None:
                            for waiter in signals:
                                if not waiter.done():
                                    waiter.cancel()
                            await asyncio.gather(*signals, return_exceptions=True)

                        try:
                            done, _ = await asyncio.wait(
                                    signals,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            # A source/page error cancels this consumer while it is
                            # idle. Drain both signals even when that await is
                            # cancelled, so retries cannot accumulate orphan waits.
                            await finish_owned(settle_waiters())
                        if (
                            local_stop_wait in done
                            and control.stop_event.is_set()
                        ):
                            raise asyncio.CancelledError
                except _ManualControlYield:
                    manual_yielded = True
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await control.pause_event.wait()
                    capacity_failures.append(exc)
                    isolated_page_failure = self._is_isolated_screening_page_failure(exc)
                    if direct_handoff and isolated_page_failure:
                        # A reusable slot must not retain an exhausted retry budget
                        # or a broken candidate page. Its real cleanup still owns
                        # the slot until the worker confirms native retirement.
                        failed_child_ids.add(id(child))
                    failed_username = getattr(exc, "details", {}).get("candidate_username")
                    if not direct_handoff and failed_username in candidate_failures and self._is_candidate_page_failure(exc):
                        # Retain the failed old page under this same account lease.
                        # The source keeps collecting; the parent/sibling cannot
                        # race this candidate again within the same attempt.
                        held[failed_username] = child
                    elif self._is_parent_profile_failure(exc) and not intervention.done():
                        # Account identity/session failures fence all parent work.
                        intervention.set_result(exc)
                    # Direct handoff retries pending rows only in isolated children.
                    # Account intervention pages remain owned for manual recovery.
                    return exc
                finally:
                    active_screeners.discard(id(child))
                    capacity_changed.set()
                    await close_child_once(child)

            try:
                producer_task = asyncio.create_task(producer())
                consumer_tasks = [
                    asyncio.create_task(consume(child))
                    for child in parallel_children
                ]
                if profile_id and lease_token and mode in {"followers", "following"}:
                    live_session = {"target_id": target_id, "mode": mode, "accepting": True,
                                    "request": request_live_recheck,
                                    "parent_activity": ("waiting_safe_point" if live_recheck.get("pending") else "rechecking") if live_recheck else "collecting"}
                    control.live_source_rechecks[profile_id] = live_session
                stop_wait = asyncio.create_task(control.stop_event.wait())
                done, _ = await asyncio.wait(
                    {producer_task, stop_wait, intervention},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_wait in done and control.stop_event.is_set():
                    raise asyncio.CancelledError
                if intervention in done:
                    raise intervention.result()
                try:
                    await producer_task
                except WorkerExecutionError as exc:
                    if not await can_drain_source_error(exc):
                        raise
                    # The source stopped without verified completion. Previously
                    # non-hover list stalls cancelled every child, including pages
                    # already screening durable rows. Let those pages finish first;
                    # the incomplete source cursor remains available for retry.
                    source_list_error = exc
                    # A queued request cannot turn an unconfirmed source failure
                    # into a safe navigation. Drain healthy children, preserving
                    # the durable request for the owned recovery entry instead.
                    if live_recheck is not None and live_recheck.get("pending"):
                        recheck_available.clear()
                        candidate_available.set()

                if (live_recheck is not None and not live_recheck.get("pending")
                        and source_list_error is None and not manual_yielded):
                    async with checkpoint_lock:
                        await self._await_durable_thread_call(
                            self.service.finish_live_source_recheck,
                            control.owner_user_id, control.task_id, target_id, mode,
                            profile_id=profile_id, lease_token=lease_token,
                            requested_at=live_recheck["requested_at"],
                        )
                        live_recheck = None
                        candidate_available.set()
                if live_session and not recheck_available.is_set():
                    live_session["parent_activity"] = "waiting_recovery" if source_list_error else "idle"
                consumer_group = asyncio.gather(
                    *consumer_tasks, return_exceptions=True
                )
                recheck_wait = asyncio.create_task(recheck_available.wait())
                try:
                    while True:
                        # Only an idle parent with a verified final source return
                        # may browse Reels; never race a queued manual source pass.
                        if (not recheck_available.is_set() and parent_reels_task is None
                                and parent_reels_eligible and settings.get("platform", "instagram") == "instagram"
                                and mode == "followers" and discovery_complete
                                and not pending_relation_usernames and source_list_error is None
                                and not manual_yielded and active_screeners
                                and parent_navigation_failure() is None
                                and any(not task.done() for task in consumer_tasks)
                                and callable(getattr(worker, "parent_reels_adapter", None))):
                            if live_session:
                                live_session["parent_activity"] = "browsing_reels"
                            parent_reels_task = asyncio.create_task(self._browse_parent_reels(
                                control, worker, consumer_tasks, intervention,
                                lambda: not active_screeners or parent_navigation_failure() is not None, target_id))
                            def parent_activity_finished(_):
                                if live_session and live_session.get("parent_activity") == "browsing_reels":
                                    live_session["parent_activity"] = "idle"
                            parent_reels_task.add_done_callback(parent_activity_finished)
                        done, _ = await asyncio.wait(
                            {consumer_group, stop_wait, intervention, recheck_wait},
                            return_when=asyncio.FIRST_COMPLETED)
                        if stop_wait in done and control.stop_event.is_set():
                            raise asyncio.CancelledError
                        if intervention in done:
                            raise intervention.result()
                        if recheck_available.is_set():
                            # Cancellation is shielded within _browse_parent_reels;
                            # join its exact page operations before source navigation.
                            if parent_reels_task is not None:
                                parent_reels_task.cancel()
                                await finish_owned(asyncio.gather(parent_reels_task, return_exceptions=True))
                                parent_reels_task = None
                            if intervention.done():
                                raise intervention.result()
                            if (not active_screeners
                                    or parent_navigation_failure() is not None):
                                failure = screening_capacity_failure()
                                if failure is not None:
                                    raise failure
                                raise WorkerExecutionError("筛选子页尚未就绪，已保留父页复查请求",
                                                           reason="browser_operations_pending", pause_required=True)
                            if (control.stop_event.is_set() or control.tearing_down
                                    or self._runs.get(control.task_id) is not getattr(control, "_shared", control)
                                    or control.profile_workers.get(profile_id) is not worker
                                    or control.leases.get(profile_id) != lease_token
                                    or getattr(worker, "page", None) is not parent_page
                                    or getattr(worker, "_page_stage_abandoned", False)):
                                raise WorkerExecutionError("父页复查等待安全恢复，已保留请求和候选", reason="browser_operations_pending", pause_required=True)
                            # Global/local pause remains authoritative. Joining
                            # Reels alone is not permission to read or rewind yet.
                            await check_source_controls()
                            async with checkpoint_lock:
                                result = await self._await_durable_thread_call(
                                    self.service.recheck_task_source,
                                    control.owner_user_id, control.task_id, target_id, mode,
                                    profile_id=profile_id, lease_token=lease_token, live_parent=True,
                                    pending_requested_at=live_recheck["requested_at"],
                                )
                                discovery_complete = False
                                resume_tail = []
                                rendered_count = None
                                progress_epoch = None
                                live_recheck.update(target=result, pending=False)
                                live_session["parent_activity"] = "rechecking"
                                producer_done.clear()
                            # Durable activation yields to Stop, account guards,
                            # child retirement and ownership changes. Revalidate
                            # capacity before scheduling any new parent read.
                            await check_source_controls()
                            if intervention.done():
                                raise intervention.result()
                            if (control.stop_event.is_set() or control.tearing_down
                                    or self._runs.get(control.task_id) is not getattr(control, "_shared", control)
                                    or control.profile_workers.get(profile_id) is not worker
                                    or control.leases.get(profile_id) != lease_token
                                    or getattr(worker, "page", None) is not parent_page
                                    or getattr(worker, "_page_stage_abandoned", False)):
                                raise WorkerExecutionError("父页复查等待安全恢复，已保留请求和候选", reason="browser_operations_pending", pause_required=True)
                            # The source restarts inside this original producer and
                            # child pool. No lease, worker, task or global gate moves.
                            producer_task = asyncio.create_task(producer())
                            done, _ = await asyncio.wait({producer_task, stop_wait, intervention},
                                                       return_when=asyncio.FIRST_COMPLETED)
                            if stop_wait in done and control.stop_event.is_set():
                                raise asyncio.CancelledError
                            if intervention in done:
                                raise intervention.result()
                            try:
                                await producer_task
                            except WorkerExecutionError as exc:
                                if not await can_drain_source_error(exc):
                                    raise
                                # A failed source pass does not invalidate healthy
                                # child pages. Drain their durable backlog first,
                                # preserving the active request for later resume.
                                source_list_error = exc
                            if manual_yielded:
                                raise _ManualControlYield()
                            async with checkpoint_lock:
                                if source_list_error is None:
                                    await self._await_durable_thread_call(
                                        self.service.finish_live_source_recheck,
                                        control.owner_user_id, control.task_id, target_id, mode,
                                        profile_id=profile_id, lease_token=lease_token,
                                        requested_at=live_recheck["requested_at"])
                                    live_recheck = None
                                    live_session["parent_activity"] = "idle"
                                recheck_available.clear()
                                candidate_available.set()
                            recheck_wait = asyncio.create_task(recheck_available.wait())
                            continue
                        if consumer_group in done:
                            # Admission may be committing while the last child
                            # fails or yields. Serialize completion with that write;
                            # a request that won must remain incomplete/recoverable.
                            async with checkpoint_lock:
                                if recheck_available.is_set():
                                    continue
                                if live_session:
                                    live_session["accepting"] = False
                            await consumer_group
                            break
                finally:
                    recheck_wait.cancel()
                    await asyncio.gather(recheck_wait, return_exceptions=True)
                if intervention.done():
                    raise intervention.result()
                if manual_yielded:
                    raise _ManualControlYield()
            except BaseException:
                if live_session:
                    live_session["accepting"] = False
                async def settle_pipeline() -> None:
                    if parent_reels_task is not None:
                        parent_reels_task.cancel()
                        await asyncio.gather(parent_reels_task, return_exceptions=True)
                    if producer_task is not None and not producer_task.done():
                        producer_task.cancel()
                    for consumer_task in consumer_tasks:
                        if not consumer_task.done():
                            consumer_task.cancel()
                    await asyncio.gather(
                        *([producer_task] if producer_task is not None else []),
                        *consumer_tasks,
                        return_exceptions=True,
                    )
                    # Joining writers and preserving the final cursor is one owned
                    # cleanup. A second cancellation must not skip the save after
                    # an older consumer write finally releases checkpoint_lock.
                    try:
                        current = await self._await_durable_thread_call(
                            self.service.reconcile_task_mode_candidates,
                            control.owner_user_id, control.task_id, target_id, mode,
                        )
                        await write_progress(
                            current,
                            complete=discovery_complete,
                            stage="screening_accounts" if discovery_complete else "discovering_accounts",
                        )
                    except Exception:
                        pass

                await finish_owned(settle_pipeline())
                raise
            finally:
                if live_session:
                    live_session["accepting"] = False
                    if control.live_source_rechecks.get(profile_id) is live_session:
                        control.live_source_rechecks.pop(profile_id, None)
                # Join optional parent work before fallback screening, mode change,
                # page cleanup or releasing the original collection lease.
                if parent_reels_task is not None:
                    parent_reels_task.cancel()
                    await finish_owned(asyncio.gather(parent_reels_task, return_exceptions=True))
                if stop_wait is not None and not stop_wait.done():
                    stop_wait.cancel()
                if stop_wait is not None:
                    await asyncio.gather(stop_wait, return_exceptions=True)
                await asyncio.gather(
                    *(close_child_once(child) for child in parallel_children),
                    return_exceptions=True,
                )

            if source_list_error is None:
                discovery_complete = True
            stats = await self._await_durable_thread_call(
                self.service.reconcile_task_mode_candidates,
                control.owner_user_id,
                control.task_id,
                target_id,
                mode,
            )
            await write_progress(
                stats,
                complete=discovery_complete,
                stage="screening_accounts",
            )

        # The source has naturally finished (or its recovered cursor already says
        # so), and every child has joined. One common parent drain handles both
        # healthy leftovers and accelerator failures without a second full pass.
        if direct_handoff:
            stats = await self._await_durable_thread_call(self.service.task_mode_candidate_stats,
                control.owner_user_id, control.task_id, target_id, mode)
            if stats['pending'] and not candidate_failures:
                error = screening_capacity_failure() or PlaywrightWorker._page_recovery_exhausted(
                    WorkerExecutionError('子页尚有未完成账号', reason='browser_window_surface_unstable'), target["username"])
                if isinstance(error, WorkerExecutionError) and discovery_complete:
                    error.details['source_discovery_complete'] = True
                raise error
        else:
            stats = await self._drain_candidate_spool(
                control,
                worker,
                target_id,
                mode,
                settings,
                discovery_complete=discovery_complete,
                source_total=source_total,
                resume_tail=resume_tail,
                rendered_count=rendered_count,
                progress_epoch=progress_epoch,
                checkpoint_writer=lambda current: write_progress(
                    current,
                    complete=discovery_complete,
                    stage="screening_accounts",
                ),
                candidate_failures=candidate_failures,
                defer_candidate_failures=True,
                reconcile_on_entry=False,
            )
        if candidate_failures and stats["pending"]:
            await write_progress(stats, complete=discovery_complete, stage="screening_accounts")
            pending_page = await self._await_durable_thread_call(
                self.service.list_pending_task_mode_candidates,
                control.owner_user_id, control.task_id, target_id, mode, limit=100,
            )
            error = next(
                (candidate_failures[item["username_norm"]] for item in pending_page
                 if item["username_norm"] in candidate_failures),
                next(iter(candidate_failures.values())),
            )
            if isinstance(error, WorkerExecutionError):
                if discovery_complete:
                    error.details["source_discovery_complete"] = True
                username = error.details.get("candidate_username", "")
                error.message = f"其余可读取账号已继续处理；资料页 @{username} 仍未恢复，已保留 {stats['pending']} 个候选等待重试"
                error.args = (error.message,)
            raise error
        if source_list_error is not None:
            raise source_list_error
        return {
            **stats,
            "source_total": source_total,
            "discovery_complete": discovery_complete,
            "skipped_posts": skipped_posts,
            "resume_tail": resume_tail,
            "rendered_count": rendered_count,
            "progress_epoch": progress_epoch,
            "pending_relation_usernames": pending_relation_usernames,
            "automatic_gap_recheck_started": automatic_gap_recheck_started,
        }

    async def _browse_parent_reels(self, control, worker, children, intervention, has_failures, target_id):
        """Bound optional parent activity to independent child ownership."""
        from .parent_reels import run_parent_reels

        stopping = asyncio.Event()
        profile_id = getattr(control, "profile_id", None)
        leases = getattr(control, "leases", {})
        lease_token = leases.get(profile_id)
        parent_page = getattr(worker, "page", None)

        def owns_generation():
            return (getattr(worker, "page", None) is parent_page
                    and getattr(control, "leases", {}).get(profile_id) == lease_token)

        async def checkpoint():
            if (stopping.is_set() or not control.pause_event.is_set() or control.stop_event.is_set()
                    or intervention.done() or has_failures()
                    or not owns_generation()
                    or not any(not child.done() for child in children)):
                raise asyncio.CancelledError
            await self._manual_worker_checkpoint(control)

        class OwnedAdapter:
            """Join a native operation before handing the parent page back.

            Cancelling a guard mid-read marks the real worker's page stage as
            abandoned. Routine child completion must instead stop at the next
            operation boundary, or the next source inherits a poisoned driver.
            Existing adapter timeouts still bound the browser work itself.
            """
            def __init__(self, adapter):
                self.adapter = adapter

            def __getattr__(self, name):
                method = getattr(self.adapter, name)
                if name not in {"open", "current", "like", "advance", "stop"}:
                    return method

                async def owned(*args, **kwargs):
                    # stop is document-fenced cleanup and must run even after
                    # eligibility ends; every new browsing action is rejected.
                    if name != "stop":
                        await checkpoint()
                    elif not owns_generation():
                        return None
                    return await finish_owned(method(*args, **kwargs))
                return owned

        decisions = getattr(worker, "_parent_reels_decisions", None)
        if decisions is None:
            decisions = worker._parent_reels_decisions = set()
        async def claim(key, selected):
            return await self._await_durable_thread_call(
                self.service.claim_parent_reel_decision, control.owner_user_id,
                control.task_id, target_id, getattr(control, "profile_id", None), key, selected,
                lease_token=control.leases.get(getattr(control, "profile_id", None)))

        activity = asyncio.create_task(run_parent_reels(
            OwnedAdapter(worker.parent_reels_adapter()), checkpoint, decisions=decisions, claim=claim))

        async def monitor():
            while True:
                await checkpoint()
                await asyncio.sleep(.05)

        watcher = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({activity, watcher}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (_ManualControlYield, asyncio.CancelledError):
            # Pause/manual ownership ends browsing; collection keeps its own
            # established pause/resume behavior and does not restart a like.
            pass
        except Exception as exc:
            # Account guards and ownership loss remain authoritative. Optional
            # media/control failures cannot turn a successful collection into loss.
            if (self._is_profile_intervention_error(exc)
                    or getattr(exc, "code", "") in {"worker_not_connected", "cdp_profile_verification_failed"}):
                if not intervention.done():
                    intervention.set_result(exc)
        finally:
            # Set before cancelling: an in-flight owned operation can finish,
            # but its next checkpoint cannot send another navigation or click.
            stopping.set()
            async def settle():
                activity.cancel()
                watcher.cancel()
                await asyncio.gather(activity, watcher, return_exceptions=True)
            await finish_owned(settle())

    @staticmethod
    def _candidate_spool_limit(mode: str, settings: dict[str, Any]) -> int | None:
        limits = settings.get("mode_limits", {}).get(mode, {})
        # Relationship sources stop only after the rendered list reaches its
        # confirmed physical end.  ``None`` is carried through the worker and
        # service sink, so this remains genuinely unbounded for both new jobs and
        # legacy recovered jobs instead of relying on a large integer sentinel.
        if mode in {"followers", "following"}:
            return None
        per_target_limit = limits.get("per_target_limit") or 500
        if mode != "post_likers":
            return int(per_target_limit)
        max_posts = (
            limits.get("like_posts_to_check")
            or settings.get("like_posts_to_check")
            or 1
        )
        per_post = (
            limits.get("max_likers_per_post")
            or settings.get("max_likers_per_post")
            or per_target_limit
        )
        return int(max_posts) * int(per_post)

    @staticmethod
    async def _collect_mode(
        worker: Any,
        target: str,
        mode: str,
        settings: dict[str, Any],
        *,
        candidate_sink: Callable[[list[str]], Any] | None = None,
        initial_candidate_count: int = 0,
        progress_sink: Callable[[dict[str, Any]], Any] | None = None,
        initial_resume_tail: list[str] | None = None,
        initial_pending_relation_usernames: list[str] | None = None,
    ) -> Any:
        collection_platform(settings)
        limits = settings.get("mode_limits", {}).get(mode, {})
        per_target_limit = (
            None
            if mode in {"followers", "following"}
            else limits.get("per_target_limit") or 500
        )
        sink_kwargs: dict[str, Any] = {}
        if candidate_sink is not None and getattr(
            worker, "supports_candidate_batch_sink", False
        ):
            sink_kwargs = {
                "candidate_sink": candidate_sink,
                "initial_candidate_count": initial_candidate_count,
            }
        if progress_sink is not None and getattr(
            worker, "supports_collection_progress_sink", False
        ):
            sink_kwargs["progress_sink"] = progress_sink
        if mode in {"followers", "following"} and initial_resume_tail:
            try:
                parameters = inspect.signature(
                    worker.collect_followers
                    if mode == "followers"
                    else worker.collect_following
                ).parameters.values()
                accepts_resume_tail = any(
                    parameter.name == "initial_resume_tail"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_resume_tail = False
            if accepts_resume_tail:
                sink_kwargs["initial_resume_tail"] = list(initial_resume_tail)
        if mode in {"followers", "following"} and initial_pending_relation_usernames:
            method = worker.collect_followers if mode == "followers" else worker.collect_following
            try:
                parameters = inspect.signature(method).parameters.values()
                accepts_pending = any(parameter.name == "initial_pending_relation_usernames"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            except (TypeError, ValueError):
                accepts_pending = False
            if accepts_pending:
                sink_kwargs["initial_pending_relation_usernames"] = list(initial_pending_relation_usernames)
            else:
                raise WorkerExecutionError("当前采集适配器不能恢复待确认列表身份",
                                           reason="instagram_relation_checkpoint_unsupported", pause_required=True)
        if mode in {"followers", "following"} and candidate_sink is not None:
            method = worker.collect_followers if mode == "followers" else worker.collect_following
            try:
                parameters = inspect.signature(method).parameters.values()
                supports_hover = any(parameter.name == "hover_precheck" for parameter in parameters)
            except (TypeError, ValueError):
                supports_hover = False
            if supports_hover:
                sink_kwargs['hover_precheck'] = not getattr(worker, 'supports_single_candidate_handoff', False)
            elif (settings.get('platform', 'instagram') == 'instagram' and isinstance(worker, PlaywrightWorker)
                  and not getattr(worker, 'supports_single_candidate_handoff', False)):
                # Real Instagram list collection must never silently downgrade
                # to opening every profile when an adapter loses hover support.
                # The owning target stays recoverable with its cursor intact.
                raise WorkerExecutionError(
                    '当前浏览器适配器不支持粉丝/关注悬浮卡预审，已保留进度等待恢复',
                    reason='instagram_hover_precheck_unavailable',
                    pause_required=True,
                )
        if mode == "followers":
            return await worker.collect_followers(
                target, limit=per_target_limit, **sink_kwargs
            )
        if mode == "following":
            return await worker.collect_following(
                target, limit=per_target_limit, **sink_kwargs
            )
        if mode == "post_likers":
            max_posts = limits.get("like_posts_to_check") or settings.get("like_posts_to_check") or 1
            per_post = limits.get("max_likers_per_post") or settings.get("max_likers_per_post") or per_target_limit
            return await worker.collect_post_likers(
                target,
                max_posts=max_posts,
                per_post_limit=per_post,
                **sink_kwargs,
            )
        raise ValidationError(f"Unsupported collection mode: {mode}")

    @staticmethod
    def _mode_label(mode: str) -> str:
        return {
            "followers": "粉丝",
            "following": "关注",
            "post_likers": "帖子点赞用户",
        }.get(mode, mode)

    async def _record_hover_exclusions(
        self, control: ExecutionControl, target_id: str, mode: str,
        settings: dict[str, Any], batch: list[str], previews: dict[str, dict],
    ) -> None:
        from .profile_hover_preview import hover_count_exclusion
        exclusions: list[tuple[str, dict[str, Any], tuple[str, dict]]] = []
        for username in batch:
            preview = previews.get(username)
            if isinstance(preview, dict) and preview.get('duplicate') is True:
                continue
            bounded = preview.get('bounded_counts', {}) if isinstance(preview, dict) else {}
            bounded_valid = isinstance(bounded, dict) and all(
                field in ('posts', 'followers', 'following')
                and isinstance(evidence, dict)
                and type(evidence.get('minimum')) is int
                and evidence['minimum'] >= 0
                and evidence['minimum'] == preview.get(field)
                and isinstance(evidence.get('display'), str)
                and 0 < len(evidence['display']) <= 32
                for field, evidence in bounded.items()
            )
            verified = (
                isinstance(preview, dict)
                and preview.get('username') == username
                and preview.get('evidence') == 'relationship_hover_card'
                and bounded_valid
                and all(type(preview.get(field)) is int and preview[field] >= 0
                        for field in ('posts', 'followers', 'following'))
            )
            exclusion = hover_count_exclusion(preview, settings) if verified else None
            if not verified:
                raise WorkerExecutionError(
                    f'@{username} 的悬浮卡证据不完整；未写入排除记录',
                    reason='instagram_hover_card_unavailable',
                    pause_required=True, status_code=503,
                )
            if exclusion is None:
                continue
            exclusions.append((username, preview, exclusion))
        # Validate the entire confirmed prefix before writing any terminal
        # exclusions. A later incomplete card cannot leave a recorded exclusion
        # with zero corresponding candidates in the source spool.
        for username, preview, exclusion in exclusions:
            await self._manual_worker_checkpoint(control)
            await control.pause_event.wait()
            if control.stop_event.is_set():
                raise asyncio.CancelledError()
            reason_code, exceeded = exclusion
            claim = await self._await_durable_thread_call(
                self.service.claim_workbench_identity,
                control.owner_user_id,
                username=username, source=mode, source_target=target_id,
                allow_owned_resume=True,
            )
            if claim['duplicate']:
                continue
            profile = {key: preview[key] for key in ('followers', 'following', 'posts')}
            bounded = preview.get('bounded_counts') or {}
            if bounded:
                # A rounded hover number is a proven floor for an exclusion,
                # never an exact profile statistic for reports or review rows.
                profile['bounded_counts'] = {
                    field: {'display': value['display'], 'minimum': value['minimum']}
                    for field, value in bounded.items()
                }
                for field in bounded:
                    profile[field] = None
            persisted_exceeded = {
                field: (
                    {'minimum': bounded[field]['minimum'],
                     'display': bounded[field]['display'],
                     'maximum': values['maximum']}
                    if field in bounded else dict(values)
                )
                for field, values in exceeded.items()
            }
            profile.update(username=username, visibility=preview.get('visibility', 'unknown'),
                           page_read_status='hover_preview',
                           page_read_reason=(
                               'bounded_hover_card_counts' if bounded
                               else 'exact_hover_card_counts'))
            screening = {
                'stage': 'hover_count_rejected', 'review_tier': 'excluded',
                'review_reason': reason_code, 'routing_result': 'excluded_from_hover_card',
                'count_ceiling': {'checked': True, 'passed': False,
                                  'visibility': profile['visibility'],
                                  'exceeded': persisted_exceeded},
                'basic': {'passed': False, 'reason_codes': [reason_code]},
            }
            await self._await_durable_thread_call(
                self._record_collection_exclusion, control,
                target_id=target_id, username=username, instagram_user_id=None,
                mode=mode, profile=profile, screening=screening,
                claim_id=claim['claim_id'], reason_code=reason_code,
                reason=(
                    '悬浮资料卡确认帖子数为 0；已按排除0帖开关跳过主页检查'
                    if reason_code.endswith('_zero_posts_excluded')
                    else '悬浮资料卡显示数量下界超出直接丢弃规则；跳过主页检查'
                    if bounded else '悬浮资料卡已确认超出直接丢弃规则；跳过主页检查'
                ),
            )

    def _record_collection_exclusion(
        self,
        control: ExecutionControl,
        *,
        target_id: str,
        username: str,
        instagram_user_id: str | None,
        mode: str,
        profile: dict[str, Any],
        screening: dict[str, Any],
        claim_id: str | None,
        reason_code: str,
        reason: str,
        location_country: str | None = None,
    ) -> bool:
        """Persist a terminal collection-stage exclusion without creating review work."""

        if claim_id is not None:
            self.service.record_workbench_exclusion(
                control.owner_user_id,
                claim_id=claim_id,
                username=username,
                reason_code=reason_code,
                reason=reason,
                location_country=location_country,
                profile=profile,
            )
        result = self.service.record_result(
            control.owner_user_id,
            control.task_id,
            target_id,
            username=username,
            instagram_user_id=instagram_user_id,
            source_mode=mode,
            visibility=profile.get("visibility") or "unknown",
            profile=profile,
            screening=screening,
            qualified=False,
            **({"dedupe_claim_id": claim_id} if claim_id is not None else {}),
        )
        return not bool(result.get("deduped"))

    @staticmethod
    def _male_avatar_filter_excludes(
        settings: dict[str, Any],
        screening: dict[str, Any],
    ) -> bool:
        """Apply the fixed local-avatar decision without guessing UNKNOWN rows."""

        enabled = bool(settings.get("exclude_male_avatar"))
        recognition = screening.get("person_recognition")
        recognition = recognition if isinstance(recognition, dict) else {}
        category = str(recognition.get("category") or "unknown").casefold()
        raw_confidence = recognition.get("confidence")
        confidence = (
            float(raw_confidence)
            if not isinstance(raw_confidence, bool)
            and isinstance(raw_confidence, (int, float))
            and math.isfinite(float(raw_confidence))
            else None
        )
        checked = recognition.get("checked") is True
        evidence_confirmed = (
            recognition.get("male_exclusion_confirmed") is True
            and recognition.get("source") == "local_openvino"
        )
        excluded = bool(
            enabled
            and checked
            and category == "male"
            and evidence_confirmed
            and confidence is not None
            and MALE_AVATAR_EXCLUSION_THRESHOLD <= confidence <= 1.0
        )
        gate: dict[str, Any] = {
            "enabled": enabled,
            "checked": checked if enabled else False,
            "threshold": MALE_AVATAR_EXCLUSION_THRESHOLD,
            "comparison": "greater_than_or_equal",
            "category": category,
            "evidence_confirmed": evidence_confirmed,
            "passed": False if excluded else (True if checked and category != "unknown" else None),
            "reason": (
                "male_probability_at_least_55_percent"
                if excluded
                else "male_evidence_requires_review"
                if checked and category == "male" and not evidence_confirmed
                else "male_probability_below_55_percent"
                if checked and category == "male" and confidence is not None
                else "non_male_category"
                if checked and category in {"female", "couple"}
                else "recognition_unknown"
                if enabled
                else "disabled"
            ),
        }
        if confidence is not None:
            gate["confidence"] = round(min(1.0, max(0.0, confidence)), 6)
        screening["male_avatar_filter"] = gate
        return excluded

    def _record_collected_profile(
        self, control: ExecutionControl, *, target_id: str, username: str,
        instagram_user_id: str | None, mode: str, visibility: str,
        profile: dict[str, Any], screening: dict[str, Any],
        qualified: bool | None, claim_id: str | None,
        review_cache: dict[str, Any],
    ) -> bool:
        """Complete the existing result/review sequence in one owned thread call.

        Service methods retain their durable transactions and restart repair gap.
        Cancellation must join both writes before worker cleanup can release its
        task/window lease; a blocked SQLite writer must not block the event loop.
        """
        result = self.service.record_result(
            control.owner_user_id, control.task_id, target_id,
            username=username, instagram_user_id=instagram_user_id,
            source_mode=mode, visibility=visibility, profile=profile,
            screening=screening, qualified=qualified,
            **({"dedupe_claim_id": claim_id} if claim_id is not None else {}),
        )
        if claim_id is not None and not result.get("deduped") and visibility in {"public", "private"}:
            self.service.create_workbench_candidate(
                control.owner_user_id, claim_id=claim_id, username=username,
                visibility=visibility, profile=profile, screening=screening,
                review_cache=review_cache, source_mode=mode, source_target=target_id,
            )
        return not bool(result.get("deduped"))

    @staticmethod
    def _require_consistent_profile_identity(confirmation: dict[str, Any], username: str) -> None:
        if confirmation.get("reason") == "claim_has_different_stable_id":
            # Conflicting evidence is not a confirmed duplicate. A stale/wrong
            # profile read must leave the original candidate recoverable rather
            # than making its durable queue row terminal forever.
            cause = WorkerExecutionError(
                "主页账号身份与已保留账号不一致，已保留候选等待重新确认",
                reason="instagram_profile_wrong_target", pause_required=True,
            )
            raise PlaywrightWorker._page_recovery_exhausted(cause, username) from cause

    async def _screen_and_record(
        self,
        control: ExecutionControl,
        worker: Any,
        target_id: str,
        username: str,
        mode: str,
        settings: dict[str, Any],
        *,
        claim_id: str | None = None,
    ) -> bool:
        # Defence for callers restoring raw pre-upgrade task settings.
        collection_platform(settings)
        settings = {**settings, "local_person_recognition": False, "exclude_male_avatar": False}
        profile_read_reason: str | None = None
        async def stage_checkpoint():
            if control.stop_event.is_set():raise asyncio.CancelledError()
            await self._manual_worker_checkpoint(control)
            await control.pause_event.wait()
            if control.stop_event.is_set():raise asyncio.CancelledError()
        await stage_checkpoint()
        try:
            profile_read_kwargs: dict[str, Any] = {"include_activity": False}
            try:
                profile_read_parameters = inspect.signature(
                    worker.read_visible_profile
                ).parameters.values()
                accepts_avatar_image = any(
                    parameter.name == "include_avatar_image"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in profile_read_parameters
                )
            except (TypeError, ValueError):
                accepts_avatar_image = False
            # Person recognition is deliberately deferred until the inexpensive
            # count/privacy gates have retained the account. Excluded public rows
            # therefore never pay for an avatar download or local model inference.
            can_defer_avatar_capture = bool(
                settings.get("local_person_recognition")
                and getattr(worker, "supports_avatar_image_capture", False)
                and (
                    callable(getattr(worker, "capture_visible_review_snapshot", None))
                    or callable(
                        getattr(worker, "read_visible_profile_recovery_evidence", None)
                    )
                )
            )
            if (
                settings.get("local_person_recognition")
                and getattr(worker, "supports_avatar_image_capture", False)
                and accepts_avatar_image
                and not can_defer_avatar_capture
            ):
                # Compatibility workers without the deferred exact-profile reader
                # retain the original one-call contract.
                profile_read_kwargs["include_avatar_image"] = True
            transient_profile_errors = {
                "instagram_profile_not_ready",
                "instagram_profile_wrong_target",
                "instagram_profile_temporarily_unavailable",
                "instagram_profile_dom_unrecognized",
            }
            visible_profile = None
            profile_attempts = (
                1 if getattr(worker, "handles_profile_read_retries", False) else 3
            )
            for profile_attempt in range(profile_attempts):
                await stage_checkpoint()
                try:
                    visible_profile = await worker.read_visible_profile(
                        username,
                        **profile_read_kwargs,
                    )
                    break
                except WorkerExecutionError as exc:
                    if exc.code not in transient_profile_errors:
                        raise
                    if profile_attempt >= profile_attempts - 1:
                        # Never convert an unread profile into a completed/deduped
                        # candidate. The pending spool row remains resumable and the
                        # window stops on this exact account instead of jumping on.
                        raise WorkerExecutionError(
                            "目标主页连续三次未能完整读取，已保留当前账号等待恢复",
                            reason=exc.code,
                            pause_required=True,
                        ) from exc
                    await asyncio.sleep(1.2 * (profile_attempt + 1))
            if visible_profile is None:
                raise WorkerExecutionError(
                    "目标主页未返回可采集资料",
                    reason="instagram_profile_not_ready",
                    pause_required=True,
                )
            avatar_image_bytes = getattr(
                visible_profile,
                "avatar_image_bytes",
                None,
            )
            review_cache = getattr(visible_profile, "review_cache", {})
            profile = (
                visible_profile.as_dict()
                if hasattr(visible_profile, "as_dict")
                else dict(visible_profile)
            )
            if avatar_image_bytes is None:
                avatar_image_bytes = profile.pop("avatar_image_bytes", None)
            else:
                # A custom worker may expose both an attribute and a mapping key.
                # Never allow the ephemeral image to reach record_result/profile_json.
                profile.pop("avatar_image_bytes", None)
        except WorkerExecutionError as exc:
            if exc.code == "instagram_content_not_visible":
                # The final same-page check still cannot prove this profile's
                # privacy, counts or review eligibility. A partial terminal result
                # would make global dedupe skip it forever without a review row.
                # The queue defers only this pending account and retries after the
                # page is available; other candidates continue in the meantime.
                raise WorkerExecutionError(
                    "目标主页暂不可见，已保留该账号等待重试",
                    reason=exc.code,
                    pause_required=True,
                ) from exc
            raise

        collection_platform(profile)
        await stage_checkpoint()
        from .work_reports import capture_executor
        executor_profile_id = getattr(worker, "profile_id", None)
        if isinstance(executor_profile_id, str) and executor_profile_id:
            profile["executor"] = await capture_executor(worker, self.service.database, control.owner_user_id, executor_profile_id)

        profile["person_category"] = "unknown"  # No new gender judgments.
        instagram_user_id = _visible_instagram_user_id(profile)
        if instagram_user_id is None:
            # The review/exclusion writers also read this field. Do not leave an
            # invalid raw value behind after rejecting it at the first ID gate.
            profile.pop("instagram_user_id", None)
        else:
            profile["instagram_user_id"] = instagram_user_id
        if claim_id is not None and instagram_user_id is not None:
            identity_confirmation = await self._await_durable_thread_call(
                self.service.confirm_workbench_identity,
                control.owner_user_id,
                claim_id=claim_id,
                username=username,
                instagram_user_id=instagram_user_id,
            )
            await stage_checkpoint()
            self._require_consistent_profile_identity(identity_confirmation, username)
            if identity_confirmation["duplicate"]:
                # The stable id proves that the username-first claim refers to a
                # person already known under another alias.  Do not create a second
                # exclusion/review/result row and do not read any more profile data.
                return False

        # Legacy qualification bounds remain readable on saved tasks, but are no
        # longer execution gates. Only the explicit discard rules below set limits.
        screening: dict[str, Any] = {
            "stage": "basic",
            "basic": {"passed": None, "reason_codes": []},
            "activity": {
                "enabled": False,
                "checked": False,
                "passed": None,
            },
            "location": {"enabled": bool(settings.get("location_enabled")), "checked": False},
            "gpt": {"enabled": bool(settings.get("gpt_enabled")), "checked": False},
            "page_read": {
                "checked": True,
                "status": "partial" if profile_read_reason else "success",
                "reason": profile_read_reason,
            },
        }
        visibility = str(profile.get("visibility") or "unknown").lower()
        activity_discard_max = settings.get("public_discard_active_days_max", 0)
        activity_discard_enabled = bool(
            settings.get("discard_count_limits_enabled", True)
            and visibility == "public"
            and not isinstance(activity_discard_max, bool)
            and isinstance(activity_discard_max, int)
            and activity_discard_max > 0
        )
        screening["activity_ceiling"] = {
            "enabled": activity_discard_enabled,
            "checked": False,
            "passed": None,
            "maximum": activity_discard_max,
        }

        # The saved public/private discard limits precede their separate review
        # routes. Unknown privacy/counts never supply evidence for this exclusion;
        # zero disables one field, and equality always remains eligible.
        excessive_counts: dict[str, dict[str, int | float]] = {}
        if settings.get("discard_count_limits_enabled", True) and visibility in {"public", "private"}:
            for field_name in ("followers", "following", "posts"):
                threshold = settings.get(f"{visibility}_discard_{field_name}_max", 4000)
                value = profile.get(field_name)
                if (
                    not isinstance(threshold, bool)
                    and isinstance(threshold, int)
                    and threshold > 0
                    and not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and (isinstance(value, int) or math.isfinite(value))
                    and value > threshold
                ):
                    excessive_counts[field_name] = {"actual": value, "maximum": threshold}
        if excessive_counts:
            ceiling_reason = "account_count_ceiling_exceeded"
            skipped_ceiling_reason = f"skipped_{ceiling_reason}"
            screening["stage"] = "account_count_ceiling_rejected"
            screening["review_tier"] = "excluded"
            screening["review_reason"] = ceiling_reason
            screening["routing_result"] = "excluded_account_count_ceiling"
            screening["basic"].update({
                "passed": False,
                "reason_codes": [ceiling_reason],
            })
            screening["count_ceiling"] = {
                "checked": True,
                "passed": False,
                "enabled": True,
                "visibility": visibility,
                "comparison": "greater_than",
                "exceeded": excessive_counts,
            }
            for gate in ("location", "activity", "gpt"):
                screening[gate].update({
                    "checked": False,
                    "passed": None,
                    "reason": skipped_ceiling_reason,
                })
            count_labels = {"followers": "粉丝", "following": "关注", "posts": "帖子"}
            details = "、".join(
                f"{count_labels[field_name]} {values['actual']:,} > {values['maximum']:,}"
                for field_name, values in excessive_counts.items()
            )
            return await self._await_durable_thread_call(
                self._record_collection_exclusion,
                control,
                target_id=target_id,
                username=username,
                instagram_user_id=instagram_user_id,
                mode=mode,
                profile=profile,
                screening=screening,
                claim_id=claim_id,
                reason_code=ceiling_reason,
                reason=f"{'私密' if visibility == 'private' else '公开'}账号{details}，已按丢弃上限在采集阶段排除",
            )

        qualified: bool | None = True
        screening["basic"].update({"passed": True, "reason_codes": []})
        if profile_read_reason:
            screening["basic"]["reason_codes"].append("page_read_incomplete")

        # The saved zero-post switch applies to both public and private profiles.
        # An incomplete page/unknown count still cannot prove a zero-post profile.
        if (
            settings.get("exclude_public_zero_posts", True) is True
            and visibility in {"public", "private"}
            and _is_confirmed_zero_count(profile.get("posts"))
        ):
            zero_post_reason = f"{visibility}_zero_posts_excluded"
            skipped_zero_post_reason = f"skipped_{zero_post_reason}"
            screening["stage"] = f"{visibility}_zero_posts_rejected"
            screening["review_tier"] = "excluded"
            screening["review_reason"] = zero_post_reason
            screening["routing_result"] = "excluded_zero_posts"
            screening["zero_posts"] = {
                "enabled": True,
                "checked": True,
                "passed": False,
                "reason": zero_post_reason,
            }
            screening["location"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": skipped_zero_post_reason,
                }
            )
            screening["activity"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": skipped_zero_post_reason,
                }
            )
            account_type = "公开" if visibility == "public" else "私密"
            return await self._await_durable_thread_call(
                self._record_collection_exclusion,
                control,
                target_id=target_id,
                username=username,
                instagram_user_id=instagram_user_id,
                mode=mode,
                profile=profile,
                screening=screening,
                claim_id=claim_id,
                reason_code=zero_post_reason,
                reason=f"{account_type}账号帖子数确认是 0，已按任务设置在采集阶段排除",
            )

        # Private accounts retained by the discard gates share one manual-review
        # queue. Historical count qualification must never recreate a second lane.
        if visibility == "private":
            screening["stage"] = "private_collected"
            screening["review_tier"] = "primary"
            screening["review_reason"] = "private_account_collected"
            screening["routing_result"] = "private_review"
            screening["location"].update(
                {"checked": False, "passed": None, "reason": "not_visible_on_private_account"}
            )
            screening["activity"].update(
                {"checked": False, "passed": None, "reason": "not_visible_on_private_account"}
            )
            await stage_checkpoint()
            deferred_avatar = await self._capture_final_review_evidence(
                worker,
                username,
                profile,
                review_cache,
                settings,
                include_post_previews=False,
            )
            if deferred_avatar is not None:
                avatar_image_bytes = deferred_avatar
            await stage_checkpoint()
            return await self._await_durable_thread_call(
                self._record_collected_profile,
                control, target_id=target_id, username=username,
                instagram_user_id=instagram_user_id, mode=mode,
                visibility="private", profile=profile, screening=screening,
                qualified=qualified, claim_id=claim_id, review_cache=review_cache,
            )

        verified_state = profile.get("is_verified")
        # Verification is tri-state because Instagram does not always expose an
        # authoritative value in the visible page data.  Fail open for UNKNOWN:
        # only a positively identified badge may trigger the opt-in exclusion.
        if (
            qualified is True
            and settings.get("exclude_verified")
            and verified_state is True
        ):
            screening["stage"] = "verified_rejected"
            screening["review_tier"] = "excluded"
            screening["review_reason"] = "verified_account_excluded"
            screening["routing_result"] = "excluded_public_conditions"
            screening["verified"] = {
                "enabled": True,
                "checked": True,
                "passed": False,
                "reason": "verified_account_excluded",
            }
            screening["location"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_verified_account_excluded",
                }
            )
            screening["activity"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_verified_account_excluded",
                }
            )
            return await self._await_durable_thread_call(
                self._record_collection_exclusion,
                control,
                target_id=target_id,
                username=username,
                instagram_user_id=instagram_user_id,
                mode=mode,
                profile=profile,
                screening=screening,
                claim_id=claim_id,
                reason_code="verified_account_excluded",
                reason="公开认证账号已按任务设置在采集阶段排除",
            )

        account_category = profile.get("account_category")
        is_professional_account = bool(
            account_category or profile.get("is_professional_account") is True
        )
        if is_professional_account:
            screening["stage"] = "professional_account_rejected"
            screening["review_tier"] = "excluded"
            screening["review_reason"] = "professional_account_excluded"
            screening["routing_result"] = "excluded_public_conditions"
            screening["professional_account"] = {
                "checked": True,
                "passed": False,
                "category": str(account_category) if account_category else None,
                "reason": "professional_account_excluded",
            }
            screening["location"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_professional_account_excluded",
                }
            )
            screening["activity"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_professional_account_excluded",
                }
            )
            return await self._await_durable_thread_call(
                self._record_collection_exclusion,
                control,
                target_id=target_id,
                username=username,
                instagram_user_id=instagram_user_id,
                mode=mode,
                profile=profile,
                screening=screening,
                claim_id=claim_id,
                reason_code="professional_account_excluded",
                reason=(
                    f"公开专业账号已在采集阶段排除 · {account_category}"
                    if account_category
                    else "公开专业账号已在采集阶段排除"
                ),
            )

        raw_external_bio_url = profile.get("external_bio_url")
        external_bio_url = external_profile_link_url(raw_external_bio_url)
        # Re-normalize at the decision boundary as well as in the browser reader.
        # This protects resumed/legacy worker payloads that may have labelled the
        # built-in Threads identity badge as an external biography link.
        if raw_external_bio_url is not None:
            profile["external_bio_url"] = external_bio_url
            profile["has_external_bio_link"] = external_bio_url is not None
        has_external_bio_link = bool(
            external_bio_url
            or (
                raw_external_bio_url is None
                and profile.get("has_external_bio_link") is True
            )
        )
        if has_external_bio_link:
            screening["stage"] = "external_link_rejected"
            screening["review_tier"] = "excluded"
            screening["review_reason"] = "external_bio_link_excluded"
            screening["routing_result"] = "excluded_public_conditions"
            screening["external_bio_link"] = {
                "checked": True,
                "passed": False,
                "url": str(external_bio_url) if external_bio_url else None,
                "reason": "external_bio_link_excluded",
            }
            screening["location"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_external_bio_link_excluded",
                }
            )
            screening["activity"].update(
                {
                    "checked": False,
                    "passed": None,
                    "reason": "skipped_external_bio_link_excluded",
                }
            )
            return await self._await_durable_thread_call(
                self._record_collection_exclusion,
                control,
                target_id=target_id,
                username=username,
                instagram_user_id=instagram_user_id,
                mode=mode,
                profile=profile,
                screening=screening,
                claim_id=claim_id,
                reason_code="external_bio_link_excluded",
                reason="公开账号简介含可点击站外链接，已在采集阶段排除",
            )

        screening["review_tier"] = "primary"

        location_value: str | None = None
        location_error: str | None = None
        if settings.get("location_enabled"):
            screening["stage"] = "location"
            try:
                location_reader = getattr(worker, "read_visible_account_location", None)
                if callable(location_reader):
                    await stage_checkpoint()
                    location_value = _normalized_known_location(
                        await location_reader(username)
                    )
                else:
                    location_error = "location_reader_unavailable"
            except WorkerExecutionError as exc:
                if exc.details.get("pause_required", True) or self._is_network_error(exc):
                    # Transport/login failures pause and retry; they never become a
                    # fabricated unknown location or a false exclusion.
                    raise
                location_error = exc.code
            await stage_checkpoint()
            profile["location_zh"] = location_value
            screening["location"] = {
                "enabled": True,
                "checked": location_error is None,
                "found": location_value is not None,
                # Keep the normalized country in both durable screening evidence and
                # the profile snapshot. This makes the review display resilient to a
                # legacy/custom profile serializer omitting optional profile fields.
                "country": location_value,
                "passed": (
                    _is_united_states_location(location_value)
                    if location_value is not None
                    else None
                ),
                "reason": (
                    location_error
                    if location_error is not None
                    else (None if location_value is not None else "country_not_visible")
                ),
            }
            if location_value is None:
                # Many otherwise valid public profiles do not expose a location.
                # Preserve that absence for manual review instead of fabricating a
                # non-US result or discarding the account. Explicit non-US evidence
                # remains a terminal exclusion below.
                screening["location"].update(
                    {
                        "passed": None,
                        "reason": location_error or "country_not_visible",
                        "retained_for_manual_review": True,
                    }
                )
                if qualified is True:
                    qualified = None
            if location_value is not None and not _is_united_states_location(location_value):
                screening["stage"] = "location_rejected"
                screening["review_tier"] = "excluded"
                screening["review_reason"] = "public_location_not_us"
                screening["routing_result"] = "excluded_non_us"
                screening["location"]["reason"] = "non_us_location"
                screening["activity"].update(
                    {"checked": False, "passed": None, "reason": "skipped_non_us_location"}
                )
                return await self._await_durable_thread_call(
                    self._record_collection_exclusion,
                    control,
                    target_id=target_id,
                    username=username,
                    instagram_user_id=instagram_user_id,
                    mode=mode,
                    profile=profile,
                    screening=screening,
                    claim_id=claim_id,
                    reason_code="non_us_location",
                    reason=f"所在地不符合：{location_value}（要求美国）",
                    location_country=location_value,
                )
        # GPT annotates visible profile text before activity; it does not override
        # any deterministic gate or infer demographic attributes.
        if qualified is True and not _is_confirmed_zero_count(profile.get("posts")) and settings.get("gpt_enabled"):
            await stage_checkpoint()
            reviewer = self.reviewer_factory()
            try:
                review = await asyncio.wait_for(asyncio.to_thread(
                    reviewer.review,
                    {
                        "username": username,
                        "display_name": profile.get("display_name"),
                        "bio": profile.get("visible_description"),
                    },
                ), timeout=25.0)
            except Exception as exc:
                screening["gpt"] = {
                    "enabled": True,
                    "checked": True,
                    "result": {"review_status": "insufficient_visible_data"},
                    "error": "gpt_review_failed",
                    "error_type": type(exc).__name__,
                }
            else:
                screening["gpt"] = {"enabled": True, "checked": True, "result": review}

        screening["stage"] = "activity"

        if profile_read_reason is None:
            # Read activity once. Post-only evidence is requested only when the
            # public post-inactivity discard is enabled; Stories are not posts.
            await stage_checkpoint()
            activity_read_kwargs = {"include_activity": True}
            if activity_discard_enabled:
                try:
                    activity_parameters = inspect.signature(worker.read_visible_profile).parameters.values()
                except (TypeError, ValueError):
                    activity_parameters = ()
                if any(
                    parameter.name == "include_post_activity"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in activity_parameters
                ):
                    activity_read_kwargs["include_post_activity"] = True
            active_profile = await worker.read_visible_profile(username, **activity_read_kwargs)
            await stage_checkpoint()
            active_values = (
                active_profile.as_dict()
                if hasattr(active_profile, "as_dict")
                else dict(active_profile)
            )
            for key in ("activity_days", "activity_status", "recent_post_datetime",
                        "post_activity_days", "post_activity_status", "post_activity_reason"):
                if key in active_values:
                    profile[key] = active_values[key]
            # A partial activity response cannot erase a valid ID read from this
            # profile's header or smuggle invalid raw IDs into durable writers.
            activity_instagram_user_id = _visible_instagram_user_id(active_values) or instagram_user_id
            if activity_instagram_user_id is not None:
                profile["instagram_user_id"] = activity_instagram_user_id
            if activity_instagram_user_id != instagram_user_id:
                instagram_user_id = activity_instagram_user_id
                if claim_id is not None and instagram_user_id is not None:
                    identity_confirmation = await self._await_durable_thread_call(
                        self.service.confirm_workbench_identity,
                        control.owner_user_id,
                        claim_id=claim_id,
                        username=username,
                        instagram_user_id=instagram_user_id,
                    )
                    await stage_checkpoint()
                    self._require_consistent_profile_identity(identity_confirmation, username)
                    if identity_confirmation["duplicate"]:
                        return False
            screening["activity"]["checked"] = True
            activity_days = profile.get("activity_days")
            no_posts = (
                _is_confirmed_zero_count(profile.get("posts"))
                or profile.get("activity_status") == "no_posts"
            )
            screening["activity"].update({
                "passed": None,
                "reason": "activity_no_posts" if no_posts else "collected_for_review",
                **({"days": activity_days} if activity_days is not None else {}),
                **({"retained_for_manual_review": True} if no_posts else {}),
            })
            if no_posts:
                qualified = None
        elif _is_confirmed_zero_count(profile.get("posts")):
            profile["activity_status"] = "no_posts"
            profile["activity_days"] = None
            screening["activity"].update({
                "checked": True,
                "passed": None,
                "reason": "activity_no_posts",
                "retained_for_manual_review": True,
            })
            qualified = None
        else:
            profile["activity_status"] = "page_read_incomplete"
            screening["activity"].update({
                "checked": False,
                "passed": None,
                "reason": profile_read_reason,
            })

        # Reuse the one existing activity read for this independent discard rule.
        # Private profiles returned earlier. Unknown dates and no-post profiles
        # never prove excessive inactivity, even if a stale worker supplied days.
        # Retired qualification limits do not supply an additional activity gate.
        if activity_discard_enabled:
            discard_status = profile.get("post_activity_status", profile.get("activity_status"))
            discard_days = profile.get("post_activity_days", profile.get("activity_days"))
            discard_evidence_known = discard_status in {
                "identified", "timestamp_found", "timestamp_read",
            }
            if "post_activity_days" not in profile and discard_status == "story_today":
                # Older adapters can return today's Story along with an actual
                # post timestamp. Only the timestamp can prove post inactivity.
                discard_days = None
                timestamp = profile.get("recent_post_datetime")
                if isinstance(timestamp, str):
                    try:
                        posted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if posted_at.tzinfo is None:
                            posted_at = posted_at.replace(tzinfo=timezone.utc)
                        discard_days = max(0, int((datetime.now(timezone.utc) - posted_at).total_seconds() // 86400))
                        discard_evidence_known = True
                    except (ValueError, OverflowError):
                        pass
            known_discard_days = (
                profile_read_reason is None
                and discard_evidence_known
                and not _is_confirmed_zero_count(profile.get("posts"))
                and discard_status != "no_posts"
                and not isinstance(discard_days, bool)
                and isinstance(discard_days, (int, float))
                and (isinstance(discard_days, int) or math.isfinite(discard_days))
                and discard_days >= 0
            )
            screening["activity_ceiling"].update({
                "checked": bool(known_discard_days),
                "passed": discard_days <= activity_discard_max if known_discard_days else None,
                "reason": "activity_days_known" if known_discard_days else "activity_not_confirmed",
                **({"days": discard_days} if known_discard_days else {}),
            })
            if known_discard_days and discard_days > activity_discard_max:
                discard_reason = "public_activity_ceiling_exceeded"
                screening["stage"] = "public_activity_ceiling_rejected"
                screening["review_tier"] = "excluded"
                screening["review_reason"] = discard_reason
                screening["routing_result"] = "excluded_public_activity_ceiling"
                screening["activity_ceiling"].update({
                    "reason": discard_reason,
                    "comparison": "greater_than",
                })
                return await self._await_durable_thread_call(
                    self._record_collection_exclusion,
                    control,
                    target_id=target_id,
                    username=username,
                    instagram_user_id=instagram_user_id,
                    mode=mode,
                    profile=profile,
                    screening=screening,
                    claim_id=claim_id,
                    reason_code=discard_reason,
                    reason=(
                        f"公开账号最近发帖距今 {discard_days} 天 > 丢弃上限 "
                        f"{activity_discard_max} 天，已按直接丢弃规则在采集阶段排除"
                    ),
                    location_country=location_value,
                )

        zero_post_activity_review = (
            _is_confirmed_zero_count(profile.get("posts"))
            and screening["activity"].get("checked") is True
            and screening["activity"].get("passed") is None
            and screening["activity"].get("reason") == "activity_no_posts"
        )
        # Every enabled deterministic condition has passed at this point. New public
        # accounts can enter only primary review; the retired public-secondary lane is
        # preserved solely for startup migration of legacy rows.
        screening["review_tier"] = "primary"
        location_gate = screening.get("location") or {}
        if (
            isinstance(location_gate, dict)
            and location_gate.get("enabled") is True
            and location_gate.get("passed") is None
        ):
            screening["review_reason"] = "public_location_unavailable_retained"
        elif zero_post_activity_review:
            screening["review_reason"] = "public_zero_posts_activity_requires_review"
        else:
            screening["review_reason"] = "us_and_collection_conditions_passed"
        screening["routing_result"] = "public_primary_review"

        await stage_checkpoint()
        await self._capture_final_review_evidence(
            worker, username, profile, review_cache, settings, include_post_previews=False,
        )
        await stage_checkpoint()
        return await self._await_durable_thread_call(
            self._record_collected_profile,
            control, target_id=target_id, username=username,
            instagram_user_id=instagram_user_id, mode=mode,
            visibility=profile.get("visibility") or "unknown",
            profile=profile, screening=screening, qualified=qualified,
            claim_id=claim_id, review_cache=review_cache,
        )

    async def _apply_person_recognition(
        self,
        profile: dict[str, Any],
        screening: dict[str, Any],
        settings: dict[str, Any],
        *,
        avatar_image_bytes: bytes | None = None,
        control: ExecutionControl | None = None,
        worker: Any | None = None,
        username: str | None = None,
        allow_supplemental_post_images: bool = True,
    ) -> None:
        """Attach a local avatar category without ever blocking result persistence."""

        # Privacy auto-classification is a separate feature. Only this explicit
        # opt-in may start local avatar inference.
        enabled = bool(settings.get("local_person_recognition"))
        if not enabled:
            profile["person_category"] = "unknown"
            screening["person_recognition"] = PersonRecognition(
                category="unknown",
                checked=False,
                source="local",
                reason="disabled",
            ).as_screening(enabled=False)
            return

        if not avatar_image_bytes:
            recognition = PersonRecognition(
                category="unknown",
                checked=False,
                source="local_openvino",
                reason=str(
                    profile.get("avatar_capture_reason") or "avatar_unavailable"
                ),
                image_width=self._optional_nonnegative_int(
                    profile.get("avatar_capture_width")
                ),
                image_height=self._optional_nonnegative_int(
                    profile.get("avatar_capture_height")
                ),
            )
            profile["person_category"] = recognition.category
            screening["person_recognition"] = recognition.as_screening(enabled=True)
            return

        await self._person_recognition_gate(control)
        if not await self._acquire_person_inference_slot(control):
            recognition = PersonRecognition(
                category="unknown",
                checked=False,
                source="local_openvino",
                reason="timeout_circuit_open",
            )
            profile["person_category"] = recognition.category
            screening["person_recognition"] = recognition.as_screening(enabled=True)
            return
        loop = asyncio.get_running_loop()
        inference_started_at = time.perf_counter()
        try:
            inference_future = loop.run_in_executor(
                _PERSON_INFERENCE_EXECUTOR,
                self._classify_person_avatar,
                bytes(avatar_image_bytes or b""),
            )
        except Exception as exc:
            # Submission can fail during an unusual interpreter/executor teardown.
            # It is still a per-avatar local failure and must not leak a semaphore
            # permit or enter the browser/network retry path.
            self._person_inference_slots.release()
            recognition = PersonRecognition(
                category="unknown",
                checked=False,
                source="local",
                reason="classifier_failed",
            )
            payload = recognition.as_screening(enabled=True)
            payload["error_type"] = type(exc).__name__
            await self._person_recognition_gate(control)
            profile["person_category"] = recognition.category
            screening["person_recognition"] = payload
            return
        except BaseException:
            self._person_inference_slots.release()
            raise
        release_slot_when_done = False

        try:
            recognition = await asyncio.wait_for(
                asyncio.shield(inference_future),
                timeout=self.person_recognition_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Keep the async slot until the already-running native call finishes.
            # Waiting accounts therefore do not enter the two-thread executor queue
            # and their own 12-second inference budget never includes queue time.
            self._detach_person_inference(inference_future)
            release_slot_when_done = True
            recognition = PersonRecognition(
                category="unknown",
                checked=False,
                source="local_openvino",
                reason="timeout",
            )
            payload = recognition.as_screening(enabled=True)
        except asyncio.CancelledError:
            self._detach_person_inference(inference_future)
            release_slot_when_done = True
            raise
        except Exception as exc:
            recognition = PersonRecognition(
                category="unknown",
                checked=False,
                source="local",
                reason="classifier_failed",
            )
            payload = recognition.as_screening(enabled=True)
            payload["error_type"] = type(exc).__name__
        else:
            payload = recognition.as_screening(enabled=True)
        finally:
            if not release_slot_when_done:
                self._person_inference_slots.release()

        # A legacy single-image model is weakest on circular crops, side poses and
        # background/full-body avatars.  Only ambiguous results enter this bounded
        # local fallback.  One post never changes the label: at least two consistent
        # clear-face results are required, so an occasional friend/couple photo cannot
        # turn a single-person account into "couple".
        confidence = recognition.confidence or 0.0
        weak_avatar = (
            recognition.category == "unknown"
            or recognition.source == "local_openvino_body_fallback"
            or (recognition.category == "male" and confidence < 0.82)
            or (recognition.category == "female" and confidence < 0.65)
        )
        evidence_reader = getattr(worker, "read_visible_person_evidence_images", None)
        if (
            allow_supplemental_post_images
            and weak_avatar
            and callable(evidence_reader)
            and username
            and recognition.reason not in {"timeout", "timeout_circuit_open"}
        ):
            try:
                evidence_images = await asyncio.wait_for(
                    evidence_reader(username, limit=3), timeout=6.0
                )
            except Exception:
                evidence_images = []
            evidence_results: list[PersonRecognition] = []
            for evidence_image in list(evidence_images)[:3]:
                supplemental = await self._classify_supplemental_person_image(
                    control, evidence_image
                )
                if (
                    supplemental is not None
                    and supplemental.checked
                    and supplemental.category in {"male", "female", "couple"}
                    and (supplemental.face_count or 0) > 0
                ):
                    evidence_results.append(supplemental)
            if len(evidence_results) >= 2:
                counts = {
                    category: sum(
                        item.category == category for item in evidence_results
                    )
                    for category in ("male", "female", "couple")
                }
                winner = max(counts, key=counts.get)  # type: ignore[arg-type]
                required = 2 if len(evidence_results) <= 3 else math.ceil(
                    len(evidence_results) * 0.7
                )
                if counts[winner] >= required:
                    winning = [
                        item for item in evidence_results if item.category == winner
                    ]
                    recognition = PersonRecognition(
                        category=winner,  # type: ignore[arg-type]
                        checked=True,
                        source="local_openvino_multi_image",
                        confidence=sum(item.confidence or 0.0 for item in winning)
                        / len(winning),
                        reason="profile_grid_consensus",
                        face_count=sum(item.face_count or 0 for item in winning),
                    )
                    payload = recognition.as_screening(enabled=True)
                    payload["supplemental_images_checked"] = len(evidence_images)
                    payload["supplemental_valid_results"] = len(evidence_results)
                    payload["supplemental_consensus_count"] = counts[winner]
        # Native inference cannot be forcibly stopped in-flight. Recheck
        # task control after it returns so Stop never permits a late database write,
        # while Pause holds the finished result until the same task resumes.
        await self._person_recognition_gate(control)
        payload["inference_ms"] = max(
            0,
            int(round((time.perf_counter() - inference_started_at) * 1000)),
        )
        capture_source = profile.get("avatar_capture_source")
        if capture_source:
            payload["capture_source"] = str(capture_source)
        profile["person_category"] = recognition.category
        screening["person_recognition"] = payload

    async def _capture_final_review_evidence(
        self,
        worker: Any,
        username: str,
        profile: dict[str, Any],
        review_cache: dict[str, Any],
        settings: dict[str, Any],
        *,
        include_post_previews: bool,
    ) -> bytes | None:
        """Capture bounded review evidence after all applicable gates retain a row."""

        if not include_post_previews:
            profile.pop("recent_posts", None)
            review_cache.pop("recent_posts", None)

        snapshot_reader = getattr(worker, "capture_visible_review_snapshot", None)
        if callable(snapshot_reader):
            try:
                captured = await asyncio.wait_for(
                    snapshot_reader(
                        username,
                        include_post_previews=include_post_previews,
                    ),
                    timeout=12.0,
                )
            except Exception:
                captured = {}
            if isinstance(captured, dict):
                payload = captured.pop("avatar_image_bytes", None)
                captured_cache = captured.pop("review_cache", None)
                if isinstance(captured_cache, dict):
                    if not include_post_previews:
                        captured_cache.pop("recent_posts", None)
                    review_cache.update(captured_cache)
                if not include_post_previews:
                    captured.pop("recent_posts", None)
                for key in (
                    "avatar_url",
                    "avatar_capture_reason",
                    "avatar_capture_source",
                    "avatar_capture_width",
                    "avatar_capture_height",
                    "recent_posts",
                    "review_snapshot_captured",
                ):
                    if captured.get(key) is not None:
                        profile[key] = captured[key]
                return bytes(payload) if isinstance(payload, (bytes, bytearray)) else None

        # Compatibility fallback supplies an avatar only when recognition needs it.
        if not settings.get("local_person_recognition"):
            return None
        if not getattr(worker, "supports_avatar_image_capture", False):
            return None
        recovery_reader = getattr(worker, "read_visible_profile_recovery_evidence", None)
        if not callable(recovery_reader):
            return None
        try:
            recovered = await asyncio.wait_for(
                recovery_reader(username, include_avatar_image=True),
                timeout=6.0,
            )
        except Exception:
            profile["avatar_capture_reason"] = "deferred_avatar_capture_failed"
            return None
        if not isinstance(recovered, dict):
            return None
        payload = recovered.pop("avatar_image_bytes", None)
        for key in (
            "avatar_url",
            "avatar_capture_reason",
            "avatar_capture_source",
            "avatar_capture_width",
            "avatar_capture_height",
        ):
            if recovered.get(key) is not None:
                profile[key] = recovered[key]
        return bytes(payload) if isinstance(payload, (bytes, bytearray)) else None

    async def _classify_supplemental_person_image(
        self,
        control: ExecutionControl | None,
        payload: bytes,
    ) -> PersonRecognition | None:
        if not payload:
            return None
        await self._person_recognition_gate(control)
        if not await self._acquire_person_inference_slot(control):
            return None
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            _PERSON_INFERENCE_EXECUTOR,
            self._classify_person_avatar,
            bytes(payload),
        )
        detached = False
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=4.0)
        except asyncio.TimeoutError:
            self._detach_person_inference(future)
            detached = True
            return None
        except asyncio.CancelledError:
            self._detach_person_inference(future)
            detached = True
            raise
        except Exception:
            return None
        finally:
            if not detached:
                self._person_inference_slots.release()

    def _detach_person_inference(
        self,
        inference_future: asyncio.Future[PersonRecognition],
    ) -> None:
        """Track an unkillable native call and open a temporary circuit safely."""

        if inference_future in self._person_detached_inferences:
            return
        self._person_detached_inferences.add(inference_future)
        self._person_inference_circuit_open = True
        self._person_inference_circuit_event.set()

        def completed(future: asyncio.Future[PersonRecognition]) -> None:
            try:
                future.exception()
            except (asyncio.CancelledError, Exception):
                pass
            self._person_inference_slots.release()
            self._person_detached_inferences.discard(future)
            if not self._person_detached_inferences:
                self._person_inference_circuit_open = False
                self._person_inference_circuit_event.clear()

        inference_future.add_done_callback(completed)

    async def _acquire_person_inference_slot(
        self,
        control: ExecutionControl | None,
    ) -> bool:
        while True:
            await self._person_recognition_gate(control)
            if self._person_inference_circuit_open:
                recovered = await self._wait_for_person_inference_recovery(control)
                if not recovered:
                    return False
                continue
            acquire_task = asyncio.create_task(self._person_inference_slots.acquire())
            stop_task: asyncio.Task[bool] | None = None
            circuit_task = asyncio.create_task(
                self._person_inference_circuit_event.wait()
            )
            acquired = False
            circuit_opened = False
            try:
                waiters: set[asyncio.Task[Any]] = {acquire_task, circuit_task}
                if control is not None:
                    stop_task = asyncio.create_task(control.stop_event.wait())
                    waiters.add(stop_task)
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if acquire_task.done() and not acquire_task.cancelled():
                    await acquire_task
                    acquired = True
                if control is not None and control.stop_event.is_set():
                    raise asyncio.CancelledError
                circuit_opened = self._person_inference_circuit_open
                if circuit_opened and not acquire_task.done():
                    acquire_task.cancel()
                    await asyncio.gather(acquire_task, return_exceptions=True)
                elif not circuit_opened and not acquired:
                    await acquire_task
                    acquired = True
            except BaseException:
                if (
                    not acquired
                    and acquire_task.done()
                    and not acquire_task.cancelled()
                    and acquire_task.exception() is None
                ):
                    acquired = bool(acquire_task.result())
                if not acquire_task.done():
                    acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
                if acquired:
                    self._person_inference_slots.release()
                raise
            finally:
                signal_tasks: list[asyncio.Task[Any]] = [circuit_task]
                if stop_task is not None:
                    signal_tasks.append(stop_task)
                for signal_task in signal_tasks:
                    signal_task.cancel()
                await asyncio.gather(*signal_tasks, return_exceptions=True)
            if circuit_opened or self._person_inference_circuit_open:
                if acquired:
                    self._person_inference_slots.release()
                recovered = await self._wait_for_person_inference_recovery(control)
                if not recovered:
                    return False
                continue
            if control is None or control.pause_event.is_set():
                return True
            self._person_inference_slots.release()

    async def _wait_for_person_inference_recovery(
        self,
        control: ExecutionControl | None,
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Wait briefly for a timed-out native call instead of poisoning a batch."""

        if timeout_seconds is None:
            timeout_seconds = min(
                3.0,
                max(0.1, self.person_recognition_timeout_seconds * 0.25),
            )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self._person_inference_circuit_open:
            await self._person_recognition_gate(control)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.1, remaining))
        return True

    @staticmethod
    async def _person_recognition_gate(
        control: ExecutionControl | None,
    ) -> None:
        if control is None:
            return
        await control.pause_event.wait()
        if control.stop_event.is_set():
            raise asyncio.CancelledError

    def _classify_person_avatar(self, avatar_image_bytes: bytes) -> PersonRecognition:
        # Model initialization is lazy and single-flight so a disabled task incurs no
        # model cost and many collection windows share one local inference session.
        with self._person_classifier_lock:
            if self._person_classifier is None:
                self._person_classifier = self.person_classifier_factory()
            classifier = self._person_classifier
        return normalize_recognition(classifier.classify(avatar_image_bytes))

    def _warm_person_classifier(self) -> None:
        """Create and compile the shared classifier before any account is claimed."""

        with self._person_classifier_lock:
            if self._person_classifier is None:
                self._person_classifier = self.person_classifier_factory()
            classifier = self._person_classifier
        warmup = getattr(classifier, "warmup", None)
        if callable(warmup):
            warmup()

    @staticmethod
    def _optional_nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(0, value)

    def _mark_running_targets(self, owner_user_id: str, task_id: str, status: str) -> None:
        spec = self.service.get_task(owner_user_id, task_id)
        for target in spec["targets"]:
            if target["status"] in {"running", "waiting_network"}:
                self.service.set_target_runtime_status(
                    owner_user_id,
                    task_id,
                    target["id"],
                    status,
                    window_id=target["current_window_id"],
                )
