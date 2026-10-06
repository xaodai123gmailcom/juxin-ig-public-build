"""Seeded, offline continuity campaign against the real collector and SQLite.

Browser replies are controlled fixtures; no real social-network account is used.
900 parameter combinations supplement the 100 focused ownership regressions.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.service import CoreService

FAMILIES = ['batched', 'stream_duplicates', 'slow_child', 'cancelled_reply',
            'page_failure', 'write_failure', 'stop_resume', 'pause_resume',
            'competing_sources', 'source_resume']
KINDS = ['private', 'public', 'zero', 'counts', 'country', 'inactive']
ROUTES = {'private': 'private_review', 'public': 'public_primary_review',
          'zero': 'excluded_zero_posts', 'counts': 'excluded_account_count_ceiling',
          'country': 'excluded_non_us', 'inactive': 'excluded_public_activity_ceiling'}


async def until(condition, jobs, label):
    async with asyncio.timeout(20):
        while not condition():
            for job in jobs:
                if job.done():
                    job.result()
                    raise AssertionError('operation finished before ' + label)
            await asyncio.sleep(.001)


def make_control(owner, task_id):
    pause = asyncio.Event(); pause.set()
    return ExecutionControl(owner_user_id=owner, task_id=task_id, pause_event=pause,
        stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())


async def scenario(template, owner, number):
    family = FAMILIES[(number - 1) // 90]
    seed = (number - 1) % 90 + 1
    rng = random.Random(260930000 + number)
    children = (seed - 1) % 3 + 1
    mode = 'followers' if seed % 2 else 'following'
    count = 5 + seed % 17
    if family == 'batched': count = 40 + seed % 21
    batch_size = 1 + seed % 7
    names = [f'c{number}.account.{i}' for i in range(count)]
    kinds = {name: KINDS[(i + seed) % len(KINDS)] for i, name in enumerate(names)}
    kinds[names[0]] = 'public'  # All fault stages are reachable on this row.
    jitter = {name: rng.randrange(5) for name in names}
    fault_stage = ('counts', 'location', 'activity')[seed % 3]
    params = dict(id=f'C{number:04}', family=family, seed=seed, children=children,
        mode=mode, accounts=count, batch_size=batch_size, fault_stage=fault_stage,
        jitter=[jitter[name] for name in names])
    with tempfile.TemporaryDirectory(prefix=f'continuity-{number}-') as folder:
        path = Path(folder) / 'case.sqlite3'
        shutil.copy2(template, path)
        service = CoreService(Database(path))
        settings = {'parallel_screening_workers': children, 'gpt_enabled': False,
            'local_person_recognition': False, 'exclude_male_avatar': False,
            'location_enabled': True, 'exclude_public_zero_posts': True,
            'public_discard_followers_max': 100, 'public_discard_following_max': 100,
            'public_discard_posts_max': 100, 'public_discard_active_days_max': 30}
        task = service.create_task(owner, name=f'continuity {number}', modes=[mode],
            targets=[f'c{number}.source.0'], settings=settings)
        targets = [task['targets'][0]]
        tasks = [task]
        if family == 'competing_sources':
            other = service.create_task(owner, name=f'competing {number}', modes=[mode],
                targets=[f'c{number}.source.1'], settings=settings)
            tasks.append(other); targets.append(other['targets'][0])
        controls = [make_control(owner, t['id']) for t in tasks]
        reads, active, sources, jobs = [], {}, [], []
        completed_before_resume = set()
        injected = False
        block_initial = family in {'cancelled_reply', 'page_failure', 'write_failure', 'stop_resume', 'pause_resume'}
        gates = {name: asyncio.Event() for name in names}
        if not block_initial:
            for gate in gates.values(): gate.set()
        if family == 'slow_child': gates[names[0]].clear()
        phase = 0
        manager = ExecutionManager(service, SimpleNamespace())

        async def yield_turns(name):
            for _ in range(jitter[name]): await asyncio.sleep(0)

        def stats(index=0):
            return service.task_mode_candidate_stats(owner, tasks[index]['id'], targets[index]['id'], mode)

        def saved():
            return [row for t in tasks for row in service.list_results(owner, t['id'])]

        async def maybe_fault(name, stage):
            nonlocal injected
            if phase != 0 or injected or name != names[0]: return
            if family == 'cancelled_reply' and stage == fault_stage:
                injected = True
                reply = asyncio.get_running_loop().create_future(); reply.cancel()
                await reply
            if family == 'page_failure' and stage == fault_stage:
                injected = True
                raise PlaywrightWorker._page_recovery_exhausted(
                    WorkerExecutionError('injected unread page', reason='instagram_profile_not_ready'), name)

        class Child:
            def __init__(self, source, slot):
                self.source, self.slot, self.current = source, slot, None
                self.closed = False

            async def read_visible_profile(self, username, **kwargs):
                stage = 'activity' if kwargs.get('include_activity') else 'counts'
                if stage == 'counts':
                    if self.current is not None:
                        assert active.get(self.current) is self
                        active.pop(self.current)
                    assert username not in active, 'overlapping child ownership: ' + username
                    active[username] = self
                    self.current = username
                    assert not (phase and username in completed_before_resume), 'terminal account read on resume'
                else:
                    assert self.current == username and active.get(username) is self
                reads.append((phase, id(self), username, stage))
                await gates[username].wait()
                await yield_turns(username)
                await maybe_fault(username, stage)
                kind = kinds[username]
                return dict(username=username, instagram_user_id=str(10000000 + number * 100 + names.index(username)),
                    visibility='private' if kind == 'private' else 'public',
                    posts=0 if kind == 'zero' else 9,
                    followers=101 if kind == 'counts' else 20, following=30,
                    activity_days=60 if kind == 'inactive' else 2,
                    activity_status='identified', post_activity_days=60 if kind == 'inactive' else 2,
                    post_activity_status='identified')

            async def read_visible_account_location(self, username):
                assert self.current == username and active.get(username) is self
                reads.append((phase, id(self), username, 'location'))
                await yield_turns(username)
                await maybe_fault(username, 'location')
                return 'Canada' if kinds[username] == 'country' else 'United States'

            async def disconnect(self):
                assert not self.closed, 'child closed twice'
                if self.current:
                    for _ in range(2 + seed % 5): await asyncio.sleep(0)
                    assert active.get(self.current) is self
                    active.pop(self.current)
                self.closed = True

        class Source:
            supports_candidate_batch_sink = True
            supports_collection_progress_sink = True
            supports_parallel_screening_tab = True
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline

            def __init__(self, index, cursor=0):
                self.index, self.cursor = index, cursor
                self.finished = asyncio.Event()
                self.children = []
                self.source_calls = 0

            async def create_parallel_screening_worker(self):
                assert len(self.children) < children
                child = Child(self, len(self.children)); self.children.append(child)
                return child

            async def collect(self, _target, *, candidate_sink, progress_sink, hover_precheck=True, **kwargs):
                nonlocal injected
                assert hover_precheck is False, 'hover unexpectedly enabled'
                self.source_calls += 1
                if phase and family != 'source_resume':
                    raise AssertionError('completed source revisited on resume')
                while self.cursor < count:
                    end = min(count, self.cursor + batch_size)
                    batch = names[self.cursor:end]
                    if family in {'stream_duplicates', 'competing_sources'}:
                        batch = [value for name in batch for value in (name, '@'+name.upper(), 'https://www.instagram.com/'+name+'/?from=list')]
                    await candidate_sink(batch)
                    self.cursor = end
                    await progress_sink(dict(source_total=count + seed % 4, resume_tail=names[max(0,end-4):end],
                        rendered_count=end, progress_epoch=end))
                    if family == 'source_resume' and phase == 0 and not injected:
                        injected = True
                        raise WorkerExecutionError('injected list interruption', reason='browser_window_surface_unstable')
                    for _ in range(seed % 3): await asyncio.sleep(0)
                self.finished.set()
                return CollectionOutcome(mode, [], source_total=count + seed % 4)

            collect_followers = collect
            collect_following = collect

        def launch(index, *, cursor=0, checkpoint=None):
            source = Source(index, cursor); sources.append(source)
            job = asyncio.create_task(manager._execute_candidate_spooled_mode(
                controls[index], source, targets[index], mode, tasks[index]['settings'], checkpoint))
            jobs.append(job)
            return source, job

        original_record = service.record_result
        if family == 'write_failure':
            def write(*args, **kwargs):
                nonlocal injected
                if kwargs.get('username') == names[0] and not injected:
                    injected = True
                    raise OSError(28, 'injected result write failure')
                return original_record(*args, **kwargs)
            service.record_result = write
        started = time.monotonic()
        try:
            initial = [launch(i) for i in range(len(tasks))]
            if block_initial:
                await until(lambda: bool((service.get_checkpoint(owner, task['id'], targets[0]['id'], mode) or {})
                    .get('cursor', {}).get('candidate_spool_natural_end')), jobs, 'durable source end')
            if family == 'pause_resume':
                controls[0].pause_event.clear()
                for gate in gates.values(): gate.set()
                for _ in range(25): await asyncio.sleep(0)
                assert not saved(), 'result committed after pause boundary'
                controls[0].pause_event.set()
            elif family == 'stop_resume':
                allowed = seed % min(children + 1, count)
                for name in names[:allowed]: gates[name].set()
                if allowed: await until(lambda: stats()['recorded'] == allowed, jobs, 'partial committed prefix')
                controls[0].stop_event.set()
            elif family == 'slow_child':
                if children > 1:
                    await until(lambda: stats()['recorded'] == count - 1, jobs, 'healthy siblings')
                    assert not jobs[0].done()
                gates[names[0]].set()
            else:
                for gate in gates.values(): gate.set()

            outcome = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 25)
            recovery = family in {'cancelled_reply', 'page_failure', 'write_failure', 'stop_resume', 'source_resume'}
            if recovery:
                assert len(outcome) == 1 and isinstance(outcome[0], BaseException), 'fault did not surface'
                if family == 'stop_resume': assert isinstance(outcome[0], asyncio.CancelledError)
                elif family == 'write_failure': assert isinstance(outcome[0], OSError)
                else: assert isinstance(outcome[0], WorkerExecutionError), repr(outcome[0])
                if family != 'stop_resume': assert injected, 'fault never exercised'
                assert not active and all(child.closed for source in sources for child in source.children)
                per_name = Counter(name for p, _, name, stage in reads if p == 0 and stage == 'counts')
                assert max(per_name.values(), default=0) <= 1, 'failed row re-opened within same attempt'
                completed_before_resume = {row['username'] for row in saved()}
                service = CoreService(Database(path))
                checkpoint = service.get_checkpoint(owner, task['id'], targets[0]['id'], mode)
                if family != 'source_resume': assert checkpoint['cursor']['candidate_spool_natural_end']
                else: assert not checkpoint['cursor'].get('candidate_spool_natural_end')
                controls[0] = make_control(owner, task['id'])
                manager = ExecutionManager(service, SimpleNamespace())
                phase = 1
                for gate in gates.values(): gate.set()
                _, resumed = launch(0, cursor=initial[0][0].cursor if family == 'source_resume' else 0, checkpoint=checkpoint)
                result = await asyncio.wait_for(resumed, 25)
                assert result['pending'] == 0
            else:
                assert all(isinstance(value, dict) and value['pending'] == 0 for value in outcome), repr(outcome)
            rows = saved()
            assert Counter(row['username'] for row in rows) == Counter(names), 'lost or duplicate durable result'
            assert all(stats(i)['pending'] == 0 for i in range(len(tasks)))
            assert sum(stats(i)['recorded'] for i in range(len(tasks))) == count
            for row in rows:
                actual = row['screening']['routing_result']
                assert actual == ROUTES[kinds[row['username']]], (row['username'], actual, ROUTES[kinds[row['username']]])
                assert row['profile']['person_category'] == 'unknown'
            # Each completed visit respects counts -> location -> activity;
            # interrupted attempts are prefixes, with no repeated stage.
            visits = {}
            for p, child, name, stage in reads: visits.setdefault((p, child, name), []).append(stage)
            for (_, _, name), stages in visits.items():
                full = ['counts'] if kinds[name] in {'private','zero','counts'} else ['counts','location']
                if kinds[name] in {'public','inactive'}: full.append('activity')
                assert stages == full[:len(stages)], (name, stages, full)
            assert not active and all(child.closed for source in sources for child in source.children)
            assert all(len(source.children) == children for source in sources)
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()], 'orphan task'
            return {**params, 'status':'passed', 'seconds':round(time.monotonic()-started,4),
                'results':len(rows), 'profile_attempts':sum(stage=='counts' for _,_,_,stage in reads),
                'resumed':bool(phase), 'children_closed':sum(len(source.children) for source in sources)}
        finally:
            for control in controls: control.pause_event.set(); control.stop_event.set()
            for gate in gates.values(): gate.set()
            for job in jobs:
                if not job.done(): job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


async def campaign(output, numbers):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='continuity-template-') as folder:
        template = Path(folder) / 'template.sqlite3'
        database = Database(template); database.initialize()
        service = CoreService(database)
        owner = service.register_user('continuity-campaign', 'offline-campaign-only-password')['id']
        # Seal the committed baseline before per-case copies, including WAL data.
        with closing(sqlite3.connect(template)) as connection:
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        passed = 0
        with (output/'campaign.jsonl').open('w', encoding='utf-8') as stream:
            for number in numbers:
                try:
                    record = await scenario(template, owner, number)
                except BaseException as error:
                    record = {'id':f'C{number:04}', 'status':'failed', 'error':repr(error), 'traceback':traceback.format_exc()}
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n'); stream.flush()
                    print(json.dumps(record,ensure_ascii=False),flush=True)
                    return 1
                stream.write(json.dumps(record,ensure_ascii=False)+'\n'); stream.flush()
                passed += 1
                if passed % 30 == 0 or passed == len(numbers):
                    print(json.dumps({'passed':passed,'planned':len(numbers),'last':record['id'],'family':record['family']},ensure_ascii=False),flush=True)
        (output/'campaign-summary.json').write_text(json.dumps({'planned':len(numbers),'passed':passed,'failed':0,
            'families':FAMILIES,'real_sqlite':True,'real_execution_manager':True,'browser_simulated':True},indent=2)+'\n')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', help='comma-separated case numbers; default 1..900')
    args = parser.parse_args()
    numbers = [int(x) for x in args.cases.split(',')] if args.cases else list(range(1,901))
    if len(set(numbers)) != len(numbers) or any(n < 1 or n > 900 for n in numbers): parser.error('case numbers must be distinct integers from 1 through 900')
    raise SystemExit(asyncio.run(campaign(args.output.resolve(),numbers)))
