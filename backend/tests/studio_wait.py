"""Bounded, progress-aware observation for disk-backed Studio scheduler tests.

Only test observation changes here. The real scheduler retains task admission,
window ownership, durable completion and cleanup responsibilities.
"""
from __future__ import annotations

import asyncio
import json
import math

from app.async_cleanup import finish_owned


class StudioCompletionTimeout(AssertionError):
    pass


class _StudioProgressBudget:
    def __init__(self, now, idle_timeout, overall_timeout):
        self.started = self.last_progress = now
        self.idle_timeout, self.overall_timeout = idle_timeout, overall_timeout
        self.cursors = {}
        self.milestones = set()

    def observe(self, snapshot, now):
        changed = False
        active = set(snapshot['active_ids'])
        leased = {item['entity_id'] for item in snapshot['leases']}
        for job in snapshot['jobs']:
            ident = job['id']
            if job['cursor'] > self.cursors.get(ident, 0):
                self.cursors[ident] = job['cursor']
                changed = True
            stages = []
            if ident in active or job['cursor'] > 0 or job['status'] == 'completed':
                stages.append('started')
            if job['status'] == 'completed':
                stages.append('completed')
                if ident not in active and ident not in leased:
                    stages.append('released')
            for stage in stages:
                key = (ident, stage)
                if key not in self.milestones:
                    self.milestones.add(key)
                    changed = True
        if changed:
            self.last_progress = now

    def expired(self, now):
        if now - self.started >= self.overall_timeout:
            return 'overall deadline'
        if now - self.last_progress >= self.idle_timeout:
            return 'no durable progress'
        return None

    def remaining(self, now):
        return max(0.0, min(self.started + self.overall_timeout - now,
                            self.last_progress + self.idle_timeout - now))


def _read_snapshot(manager, owner, ids):
    placeholders = ','.join('?' for _ in ids)
    # One read connection per sample, rather than one per job every 5 ms.
    with manager.db.read() as c:
        jobs = [dict(row) for row in c.execute(
            f'SELECT id,profile_id,status,cursor,total_steps,inflight,message FROM studio_jobs '
            f'WHERE owner_user_id=? AND deleted_at IS NULL AND id IN ({placeholders}) ORDER BY rowid',
            (owner, *ids))]
        leases = [dict(row) for row in c.execute(
            f'SELECT profile_id,entity_id FROM browser_operation_leases '
            f"WHERE owner_user_id=? AND operation_type='studio' AND entity_id IN ({placeholders})",
            (owner, *ids))]
    return {'jobs': jobs, 'leases': leases}


def _diagnostic(snapshot, budget, now):
    return json.dumps({
        'elapsed_seconds': round(now - budget.started, 3),
        'idle_seconds': round(now - budget.last_progress, 3),
        'job_count': len(snapshot['jobs']),
        'jobs': snapshot['jobs'][:16],
        'active_ids': snapshot['active_ids'][:16],
        'leases': snapshot['leases'][:16],
    }, ensure_ascii=False, sort_keys=True)


async def wait_for_studio_completion(manager, owner, ids, *, idle_timeout=120.0,
                                     overall_timeout=900.0, poll_interval=0.05):
    """Wait for all requested jobs, active cleanup and their leases to finish.

    Cursor high-water marks and one-time lifecycle milestones renew the idle
    budget. Heartbeats, timestamps, retry messages and status oscillation do not.
    Failed jobs fail immediately; a stuck job still fails at a bounded deadline.
    This helper never cancels scheduler jobs or releases browser ownership.
    """
    for name, value in (('idle_timeout', idle_timeout), ('overall_timeout', overall_timeout),
                        ('poll_interval', poll_interval)):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    ids = tuple(dict.fromkeys(ids))
    if not ids:
        raise ValueError('at least one job is required')
    wanted = set(ids)
    loop = asyncio.get_running_loop()
    budget = _StudioProgressBudget(loop.time(), idle_timeout, overall_timeout)
    snapshot = {'jobs': [], 'leases': [], 'active_ids': []}
    sample = None

    def timeout(reason):
        return StudioCompletionTimeout('Studio completion ' + reason + ': ' +
                                       _diagnostic(snapshot, budget, loop.time()))

    async def observe():
        return await finish_owned(asyncio.to_thread(_read_snapshot, manager, owner, ids))

    try:
        while True:
            reason = budget.expired(loop.time())
            if reason:
                raise timeout(reason)
            manager._schedule_ready()
            sample = asyncio.create_task(observe(), name='studio-test-durable-observer')
            await asyncio.wait({sample}, timeout=budget.remaining(loop.time()))
            if not sample.done():
                raise timeout('durable observation deadline')
            snapshot = await sample
            sample = None
            snapshot['active_ids'] = sorted(manager.active_ids() & wanted)
            if {job['id'] for job in snapshot['jobs']} != wanted:
                raise AssertionError('Studio job missing from completion observation: ' +
                                     _diagnostic(snapshot, budget, loop.time()))
            failed = any(job['status'] in {'failed', 'cancelled', 'needs_review'}
                         for job in snapshot['jobs'])
            if failed:
                raise AssertionError('Studio job did not complete successfully: ' +
                                     _diagnostic(snapshot, budget, loop.time()))
            reason = budget.expired(loop.time())
            if reason:
                raise timeout(reason)
            budget.observe(snapshot, loop.time())
            if (all(job['status'] == 'completed' for job in snapshot['jobs'])
                    and not snapshot['active_ids'] and not snapshot['leases']):
                return snapshot
            await asyncio.sleep(min(poll_interval, budget.remaining(loop.time())))
    finally:
        if sample is not None:
            sample.cancel()
            await finish_owned(asyncio.gather(sample, return_exceptions=True))
