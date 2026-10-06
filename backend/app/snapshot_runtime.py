"""Bound optional snapshot work without cancelling threads that still own I/O."""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Callable

from pydantic import TypeAdapter
from starlette.responses import JSONResponse

from .async_cleanup import finish_owned
from .errors import DomainError, UpstreamUnavailableError

logger = logging.getLogger(__name__)
_snapshot_adapter = TypeAdapter(dict[str, Any])


class SnapshotJSONResponse(JSONResponse):
    """Retain the typed route's JSON semantics while encoding off its event loop."""

    def render(self, content: Any) -> bytes:
        return _snapshot_adapter.dump_json(_snapshot_adapter.validate_python(content))


# Legacy service aliases repeat exactly the same rows already exposed through
# the canonical nested workbench response. Keep the default API backward
# compatible; modern desktop clients opt in to transmitting each bucket once.
WORKBENCH_ROW_ALIASES = frozenset({
    'pending_public_accounts', 'pending_private_accounts',
    'approved_public_accounts', 'approved_private_accounts',
    'approval_history', 'manual_rejection_history', 'collection_exclusion_history',
})


def compact_workbench_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove wire-only duplicate aliases without changing rows or durable state.

    This is deliberately not a cache: each response still reads current business
    data and occupancy. Count/has_more keys, action successes, and all canonical
    buckets remain intact. The shallow projection does not copy payload rows.
    """
    return {key: value for key, value in snapshot.items()
            if key not in WORKBENCH_ROW_ALIASES}


async def observe_snapshot_stage(label: str, operation, *, slow_seconds: float = 5.0):
    """Identify a slow stage without logging profiles, credentials or payloads.

    The warning fires while a database/provider/encoder worker is still held,
    so a later transport timeout does not erase the only useful diagnostic.
    This observer never cancels or retries the work it observes.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    timer = loop.call_later(slow_seconds, logger.warning,
        'Workbench snapshot stage still running: %s (over %.1fs)', label, slow_seconds)
    try:
        return await operation
    finally:
        timer.cancel()
        elapsed = loop.time() - started
        if elapsed >= slow_seconds:
            logger.warning('Workbench snapshot stage settled: %s (%.3fs)', label, elapsed)


class SnapshotInventoryReader:
    """At most one provider inventory call; leases are deliberately never cached.

    A slow desktop bridge must not consume the workbench's entire HTTP deadline.
    Cached metadata is marked stale and cannot claim that a window is ready.
    The caller attaches fresh durable occupancy after every read, including a
    fallback. Never cache the owner-filtered or lease-enriched response here.
    """

    def __init__(self, load: Callable[[], dict[str, Any]], *, timeout_seconds: float = 2.0):
        self.load = load
        self.timeout_seconds = timeout_seconds
        self._pending: asyncio.Task | None = None
        self._cached: dict[str, Any] | None = None
        self._closed = False

    async def _load(self) -> dict[str, Any]:
        payload = await asyncio.to_thread(self.load)
        if (not isinstance(payload, dict) or not isinstance(payload.get('windows'), list)
                or not isinstance(payload.get('connection', {}), dict)):
            raise UpstreamUnavailableError('窗口服务返回了无效列表')
        for row in payload['windows']:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not row['id'] or not isinstance(row.get('name'), str)
                    or not isinstance(row.get('is_open'), bool)):
                raise UpstreamUnavailableError('窗口服务返回了无效窗口状态')
        self._cached = copy.deepcopy(payload)
        return payload

    def _fallback(self, message: str, *, code: str = 'upstream_unavailable') -> dict[str, Any]:
        payload = copy.deepcopy(self._cached) if self._cached is not None else {'windows': []}
        payload.update(stale=True, provider_success=False)
        for window in payload['windows']:
            window.update(ready=False, opening=False, window_state='unknown')
        payload['connection'] = {
            **payload.get('connection', {}),
            'connected': False, 'state': 'unavailable', 'inventory_stale': True,
            'message': message, 'detail': message, 'error_code': code,
        }
        return payload

    async def read(self) -> dict[str, Any]:
        if self._closed:
            return self._fallback('窗口服务正在关闭')
        if self._pending is not None and self._pending.done():
            if self._pending.cancelled() or self._pending.exception() is not None:
                # A late failure has no fresh value to deliver. Permit one new
                # bounded attempt on the next poll, as with a timely failure.
                self._pending = None
        # Consume a read that completed after the previous bounded wait before
        # starting another. Otherwise a slow healthy provider stays stale forever.
        if self._pending is None:
            self._pending = asyncio.create_task(self._load(), name='workbench-window-inventory')
            # A provider can fail after a timeout and before another request.
            # Always retrieve that exception, even if the UI never polls again.
            self._pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        pending = self._pending
        done, _ = await asyncio.wait({pending}, timeout=self.timeout_seconds)
        if pending not in done:
            return self._fallback('窗口状态更新较慢，正在后台重试；其他列表仍可使用')
        try:
            payload = copy.deepcopy(pending.result())
            if payload.get('stale'):
                return self._fallback('窗口服务正在恢复，当前窗口状态为上次读取结果')
            return payload
        except DomainError as error:
            return self._fallback(error.message, code=error.code)
        except Exception:
            logger.exception('Workbench window inventory failed')
            return self._fallback('窗口状态暂时无法更新，正在自动重试')
        finally:
            if self._pending is pending:
                self._pending = None

    async def close(self) -> None:
        self._closed = True
        if self._pending is not None:
            # Do not abandon a desktop RPC during shutdown or release its owner
            # while the worker thread is still running.
            await finish_owned(asyncio.gather(self._pending, return_exceptions=True))


async def run_transient_maintenance(database: Any, stop: asyncio.Event,
                                    *, interval_seconds: float = 60.0) -> None:
    """One serial background pass, with the database's existing hourly gate.

    Failure is recorded and retried; it does not make a read-only snapshot fail.
    Shutdown signals stop and joins the actual pass before closing the database.
    """
    while not stop.is_set():
        try:
            await finish_owned(asyncio.to_thread(database.maintain_transient_data))
        except Exception:
            logger.exception('Transient data maintenance failed; will retry')
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
