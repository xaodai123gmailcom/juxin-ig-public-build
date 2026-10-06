"""Drain admitted domain handlers before releasing application resources.

HTTP parsing stays cancelable. Once FastAPI invokes an endpoint, its sync or
async work belongs to the application even if the transport disconnects.
"""
from __future__ import annotations

import asyncio
import functools
import inspect

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from .async_cleanup import finish_owned


class OwnedRequests:
    def __init__(self):
        self.draining = False
        self.tasks: set[asyncio.Task] = set()

    def start(self):
        if any(not task.done() for task in self.tasks):
            raise RuntimeError('Cannot reopen endpoint admission with active owners')
        self.tasks.clear()
        self.draining = False

    async def run(self, endpoint, *args, **kwargs):
        # No await between admission and registration: lifespan cannot miss a
        # handler admitted immediately before its shutdown barrier.
        if self.draining:
            raise HTTPException(status_code=503, detail='Core is shutting down')

        async def invoke():
            if inspect.iscoroutinefunction(endpoint):
                return await endpoint(*args, **kwargs)
            return await run_in_threadpool(endpoint, *args, **kwargs)

        task = asyncio.create_task(invoke())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return await finish_owned(task)

    async def drain(self):
        self.draining = True
        if self.tasks:
            # Handler exceptions belong to their HTTP caller; drain must still
            # wait for every other owner rather than abandoning those tasks.
            await finish_owned(asyncio.gather(*self.tasks, return_exceptions=True))

    def route_class(self):
        owner = self

        class OwnedRoute(APIRoute):
            def __init__(self, path, endpoint, **kwargs):
                @functools.wraps(endpoint)
                async def owned_endpoint(*args, **kw):
                    return await owner.run(endpoint, *args, **kw)

                # Resolve forward annotations against the original endpoint's
                # globals, including on supported older FastAPI versions.
                owned_endpoint.__signature__ = inspect.signature(endpoint, eval_str=True)
                super().__init__(path, owned_endpoint, **kwargs)

        return OwnedRoute
