"""Keep resource ownership until an asynchronous cleanup really settles."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable


async def finish_owned(operation: Awaitable[Any]) -> Any:
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    # Retrieve the result even when the caller was cancelled. In particular a
    # to_thread close is not finished just because its caller timed out.
    try:
        result = task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError()
    return result
