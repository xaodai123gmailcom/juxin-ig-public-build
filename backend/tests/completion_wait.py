"""Bounded completion observation for disk-backed collection integration tests.

The manager remains the sole authority for completion.  This watchdog only gives
a steadily advancing real SQLite queue time to drain on slower build machines.
It never changes production retry, completion, or window-ownership behaviour.
"""
from __future__ import annotations

import asyncio
import json
import math

from app.async_cleanup import finish_owned


class CollectionCompletionTimeout(AssertionError):
    pass


class _ProgressBudget:
    def __init__(self, now: float, idle_timeout: float, overall_timeout: float):
        self.started = self.last_progress = now
        self.idle_timeout = idle_timeout
        self.overall_timeout = overall_timeout
        self.high_water: dict[tuple[str, str], int] = {}
        self.completed: set[str] = set()

    def observe(self, snapshot: dict, now: float) -> None:
        changed = False
        for target in snapshot.get("targets", []):
            key = target["id"]
            stats = target["candidates"]
            values = {
                "discovered": stats["total"],
                "terminal": stats["recorded"] + stats["deduped"],
            }
            for kind, value in values.items():
                if value > self.high_water.get((key, kind), 0):
                    self.high_water[key, kind] = value
                    changed = True
            if target["status"] == "completed" and key not in self.completed:
                self.completed.add(key)
                changed = True
        if changed:
            self.last_progress = now

    def expired(self, now: float) -> str | None:
        if now - self.started >= self.overall_timeout:
            return "overall deadline"
        if now - self.last_progress >= self.idle_timeout:
            return "no durable progress"
        return None

    def remaining(self, now: float) -> float:
        return max(0.0, min(
            self.started + self.overall_timeout - now,
            self.last_progress + self.idle_timeout - now,
        ))


def _read_snapshot(service, owner: str, task_id: str, mode: str) -> dict:
    task = service.get_task(owner, task_id)
    return {
        "task_id": task_id,
        "status": task["status"],
        "mode": mode,
        "targets": [{
            "id": target["id"],
            "username": target["username"],
            "status": target["status"],
            "stage": target.get("current_stage"),
            "last_error": target.get("last_error"),
            "candidates": service.task_mode_candidate_stats(
                owner, task_id, target["id"], mode,
            ),
        } for target in task["targets"]],
    }


async def wait_for_collection_completion(
    manager, service, owner: str, task_id: str, *, mode: str = "followers",
    idle_timeout: float = 120.0, overall_timeout: float = 900.0,
    poll_interval: float = 0.25,
):
    """Observe manager completion without cancelling its owned coordinator."""
    return await wait_for_collection_operation(
        lambda: manager.wait(task_id), manager, service, owner, task_id,
        mode=mode, idle_timeout=idle_timeout, overall_timeout=overall_timeout,
        poll_interval=poll_interval,
    )


async def wait_for_collection_operation(
    operation_factory, manager, service, owner: str, task_id: str, *, mode: str = "followers",
    idle_timeout: float = 120.0, overall_timeout: float = 900.0,
    poll_interval: float = 0.25,
):
    """Await one real operation, with both a stalled-work and absolute deadline.

    Only increases in durable discovered/terminal counts or a newly completed
    target renew the idle budget. Heartbeats, retry counters, timestamps, status
    churn, and falling/rising previously observed counts cannot renew it.
    Pending counts never imply completion, even when the visible list has ended.

    The factory is called once, after budget validation. The operation must
    actually return or raise; a completed status or empty queue is insufficient.
    On timeout/cancellation we cancel and join this operation and the observers.
    A manager.wait operation shields its coordinator; a direct pipeline operation
    instead finishes its own cancellation cleanup before returning. Database
    observations run in read-only threads; cancellation joins those threads
    before the fixture can remove its database.
    """
    for name, value in (("idle_timeout", idle_timeout),
                        ("overall_timeout", overall_timeout),
                        ("poll_interval", poll_interval)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    loop = asyncio.get_running_loop()
    budget = _ProgressBudget(loop.time(), idle_timeout, overall_timeout)
    completion = asyncio.create_task(operation_factory(), name="collection-test-completion")
    sample: asyncio.Task | None = None
    snapshot: dict = {"task_id": task_id, "targets": [], "observation": "not yet read"}

    async def observe() -> dict:
        return await finish_owned(asyncio.to_thread(
            _read_snapshot, service, owner, task_id, mode,
        ))

    async def failure(reason: str) -> CollectionCompletionTimeout:
        details = {**snapshot, "elapsed_seconds": round(loop.time() - budget.started, 3),
                   "idle_seconds": round(loop.time() - budget.last_progress, 3)}
        try:
            runtime = await asyncio.wait_for(
                # Supplying the already-observed status avoids spawning another
                # database thread inside this bounded diagnostic operation.
                manager.runtime_diagnostics(owner, task_id,
                                            known_status=str(snapshot.get("status") or "unknown")),
                timeout=1.0,
            )
            details["recovery"] = {
                key: runtime.get(key) for key in ("network_waiters", "profile_states")
            }
        except Exception as exc:
            details["recovery_diagnostic_error"] = f"{type(exc).__name__}: {exc}"
        return CollectionCompletionTimeout(
            f"Collection completion {reason}: " + json.dumps(details, ensure_ascii=False, sort_keys=True)
        )

    try:
        while True:
            if completion.done():
                return await completion
            reason = budget.expired(loop.time())
            if reason:
                raise await failure(reason)
            sample = asyncio.create_task(
                observe(),
                name="collection-test-durable-observer",
            )
            await asyncio.wait(
                {completion, sample}, timeout=budget.remaining(loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if completion.done():
                return await completion
            reason = budget.expired(loop.time())
            if reason:
                raise await failure(reason)
            if not sample.done():
                # Do not turn a timed-out observation into an unbounded await.
                raise await failure("durable observation deadline")
            snapshot = await sample
            sample = None
            budget.observe(snapshot, loop.time())
            await asyncio.wait(
                {completion}, timeout=min(poll_interval, budget.remaining(loop.time())),
            )
    finally:
        observers = [completion] + ([sample] if sample is not None else [])
        for observer in observers:
            if not observer.done():
                observer.cancel()
        await finish_owned(asyncio.gather(*observers, return_exceptions=True))
