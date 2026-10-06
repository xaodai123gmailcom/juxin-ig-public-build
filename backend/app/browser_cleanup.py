"""Join asynchronous browser retirement before releasing its lease generation."""
from __future__ import annotations

import asyncio
from typing import Any

from .async_cleanup import finish_owned


def close_acknowledged(result: Any) -> bool:
    # Legacy void adapters confirm by returning normally. Structured adapters
    # must positively report closure, not merely acceptance of a close request.
    if result is None or result is True:
        return True
    if not isinstance(result, dict):
        return False
    if result.get('provider_success') is False or result.get('closed') is False:
        return False
    if 'window_state' in result and result['window_state'] != 'closed':
        return False
    return result.get('closed') is True or result.get('window_state') == 'closed'


async def disconnect_worker(worker: Any) -> None:
    join = getattr(worker, 'wait_for_cleanup', None)
    try:
        await worker.disconnect()
    except asyncio.CancelledError:
        if not callable(join) or asyncio.current_task().cancelling():
            raise
    finally:
        if callable(join):
            await finish_owned(join())


async def close_profile_and_wait(service: Any, provider: Any, profile: str, token: str) -> bool:
    def owned() -> bool:
        with service.database.read() as connection:
            row = connection.execute(
                'SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',
                (profile,),
            ).fetchone()
        return row is not None and row['lease_token'] == token

    def close_once() -> bool | None:
        if not owned():
            return None  # Lost authority is not confirmation of closure.
        # The live lease prevents admission. Holding the global surface lock over
        # provider I/O would also stop unrelated windows from being admitted.
        acknowledged = close_acknowledged(provider.close_profile(profile))
        return acknowledged if owned() else None

    async def retry() -> bool:
        delay = .25
        while True:
            try:
                result = await asyncio.to_thread(close_once)
                if result is None:
                    return False
                if result:
                    return True
            except Exception:
                pass  # Storage/provider failure is not confirmation of closure.
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 2)

    return await finish_owned(retry())
