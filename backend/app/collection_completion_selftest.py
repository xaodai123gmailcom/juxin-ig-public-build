"""Offline installed verification of production completion and manual rechecks.

The CLI selects this module before constructing service settings. It owns a fresh
TemporaryDirectory and never reads IGAC_DATA_DIR/IGAC_DB_PATH, opens a browser,
loads credentials or contacts a website. Fabricated workers exercise the real
manager, storage and DTO projection; they do not claim live-site verification.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
from unittest.mock import AsyncMock, patch

from .database import Database
from .errors import ConflictError, ValidationError
from .execution_manager import ExecutionManager
from .playwright_worker import CollectionOutcome, WorkerExecutionError
from .service import CoreService


PROOF_PREFIX = 'COLLECTION_COMPLETION_SELFTEST=PASS '


def require(condition, message):
    if not condition:
        raise RuntimeError('Collection completion installed self-test: ' + message)


def rows(database, table):
    with database.read() as connection:
        return [tuple(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


def owned_lease_token(database, task_id):
    with database.read() as connection:
        leases = connection.execute(
            "SELECT lease_token FROM browser_operation_leases WHERE entity_id=?", (task_id,)).fetchall()
    require(len(leases) == 1, 'source did not own exactly one collection lease')
    return leases[0][0]


async def scenario(directory: Path, name: str, *, no_gap=False, abnormal=False, extra_failure=False):
    path = directory / (name + '.sqlite3')
    database = Database(path)
    database.initialize()
    service = CoreService(database)
    owner = service.register_user('offline-' + name, 'isolated fixture password only')['id']
    task = service.create_task(owner, name='Offline collection completion proof', modes=['followers'],
        targets=['offline_source'], window_ids=['offline-window'], settings={
            'live_queue_enabled': False, 'local_person_recognition': False, 'location_enabled': False})
    target_id = task['targets'][0]['id']
    unhealthy = abnormal or extra_failure
    if not unhealthy:
        # Use the production completion trigger to archive an actual claimed
        # source generation, rather than asserting that empty history survived.
        with database.write() as connection:
            connection.execute('''INSERT INTO split_candidates
                (id,owner_user_id,username_norm,username_display,candidate_kind,source_task_id,
                 source_target_id,queue_state,queued_task_id,queued_target_id,created_at,updated_at)
                VALUES(?,?,'offline_source','offline_source','manual',?,?,'claimed',?,?,?,?)''',
                ('offline-history', owner, task['id'], target_id, task['id'], target_id,
                 task['created_at'], task['created_at']))
    calls = []
    lease_tokens = []
    reads = []
    disconnected = []
    close_evidence = []
    retained = {}
    def current():
        return service.get_task(owner, task['id'])['targets'][0]

    class Provider:
        def close_profile(self, profile_id):
            require(profile_id == 'offline-window', 'unexpected browser profile')
            require(bool(disconnected), 'provider close preceded worker disconnect')
            require(not current()['collection_list_dismissed'], 'card hidden before provider cleanup')
            require(bool(service.list_browser_lease_states(owner)), 'lease released before provider cleanup')
            if not unhealthy:
                require(current()['status'] == 'completed', 'provider closed before durable target completion')
                require(service.task_mode_candidate_stats(owner, task['id'], target_id, 'followers')['pending'] == 0,
                        'provider closed with unfinished details')
                for table in ('task_results', 'task_mode_candidates', 'global_seen', 'workbench_identity_claims',
                              'split_completed_targets', 'split_candidate_history'):
                    retained[table] = rows(database, table)
            close_evidence.append(profile_id)
            return {'closed': True}

    class Worker:
        supports_candidate_batch_sink = True
        def __init__(self, provider):
            self.profile_id = ''
        async def connect(self, profile_id, **kwargs):
            require(profile_id == 'offline-window', 'unexpected worker profile')
            self.profile_id = profile_id
        async def disconnect(self):
            disconnected.append(self.profile_id)
        async def collect_followers(self, source, *, limit, candidate_sink, **kwargs):
            require(source == 'offline_source', 'unexpected source')
            calls.append(source)
            require(limit is None, 'relation scan was capped')
            lease_tokens.append(owned_lease_token(database, task['id']))
            if len(calls) == 2:
                cursor = ExecutionManager._checkpoint_resume_cursor(
                    service.get_checkpoint(owner, task['id'], target_id, 'followers'))
                require(cursor.get('automatic_gap_recheck_started') is True, 'extra pass started before durable budget write')
                require(not cursor.get('candidate_spool_natural_end'), 'first pass falsely published final extraction')
                require(not kwargs.get('initial_resume_tail'), 'extra pass inherited first-pass tail')
                require(kwargs.get('initial_candidate_count') == 1, 'extra pass lost durable discovery count')
            if abnormal or extra_failure and len(calls) == 2:
                raise WorkerExecutionError('Synthetic unavailable source', reason='source_unavailable', pause_required=True)
            await candidate_sink(['offline_one', 'offline_one'])
            return CollectionOutcome('followers', [], source_total=1 if no_gap else 3)
        async def read_visible_profile(self, username, **kwargs):
            if not kwargs.get("include_activity"):
                reads.append(username)
            return {'username': username, 'visibility': 'public', 'followers': 120,
                    'following': 80, 'posts': 9, 'activity_days': None}

    manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                               lease_heartbeat_interval_seconds=.03)
    if unhealthy:
        manager._recover_network_connection = AsyncMock(return_value=None)
    try:
        await manager.start(owner, task['id'])
        if unhealthy:
            async def await_recoverable():
                while current()['status'] != 'recoverable':
                    await asyncio.sleep(.01)
            await asyncio.wait_for(await_recoverable(), 10)
            require(not current()['collection_list_dismissed'], 'abnormal card hidden before manual stop')
            await manager.stop(owner, task['id'])
        try:
            await asyncio.wait_for(manager.wait(task['id']), 30)
        except TimeoutError as error:
            raise RuntimeError(f'{name} runtime timed out: {current()!r}; leases={service.list_browser_lease_states(owner)!r}') from error
        final = current()
        require(not service.list_browser_lease_states(owner), 'owned lease remains after cleanup')
        require(len(service.get_task(owner, task['id'])['targets']) == 1, 'recheck added another target')
        require(not rows(database, 'split_candidates'), 'recheck added a split queue entry')
        if unhealthy:
            require(final['status'] != 'completed', 'abnormal source marked completed')
            require(not final['collection_list_dismissed'], 'abnormal source card was hidden')
            require(len(calls) == (2 if extra_failure else 1), 'failed source was retried as a healthy gap')
            if extra_failure:
                cursor = ExecutionManager._checkpoint_resume_cursor(
                    service.get_checkpoint(owner, task['id'], target_id, 'followers'))
                require(cursor.get('automatic_gap_recheck_started') is True, 'failed extra pass lost consumed budget')
                require(not cursor.get('candidate_spool_natural_end'), 'failed extra pass marked extraction complete')
            return {'verified': True, 'source_calls': len(calls), 'abnormal_card_retained': True,
                    'failure_remains_incomplete': True}
        expected_calls = 1 if no_gap else 2
        require(len(calls) == expected_calls, f'{name}: expected {expected_calls} whole-list passes, got {len(calls)}')
        require(len(set(lease_tokens)) == 1, 'gap pass changed collection lease')
        final_cursor = ExecutionManager._checkpoint_resume_cursor(
            service.get_checkpoint(owner, task['id'], target_id, 'followers'))
        require(bool(final_cursor.get('automatic_gap_recheck_started')) == (not no_gap), 'final budget state was lost')
        require(final['status'] == 'completed', 'healthy source did not complete')
        require(final['collection_list_dismissed'], 'healthy completed card was not hidden')
        require(close_evidence == ['offline-window'], 'owned browser was not closed exactly once')
        require(all(count == 1 for count in Counter(reads).values()), f'duplicate profile entered detail screening: {reads!r}')
        expected_results = 1
        require(len(rows(database, 'task_results')) == expected_results, 'saved results were lost or inflated')
        for table, before in retained.items():
            require(rows(database, table) == before, 'auto hiding changed durable ' + table)
        coverage = final['mode_coverage']['followers']
        require(not coverage.get('automatic_recheck'), 'canceled whole-list campaign projected in DTO')
        require(coverage['unobserved_count'] == (0 if no_gap else 2), 'actual remaining gap was falsified')
        markers = rows(database, 'task_target_list_dismissals')
        require(len(markers) == 1, 'auto-hide marker missing or duplicated')
        restarted_database = Database(path)
        restarted_database.initialize()
        restarted_service = CoreService(restarted_database)
        restarted = restarted_service.get_task(owner, task['id'])['targets'][0]
        require(restarted['collection_list_dismissed'], 'auto-hide marker lost after reopening database')
        require(restarted['mode_coverage'] == final['mode_coverage'], 'coverage ledger lost after reopening database')
        require(rows(restarted_database, 'task_target_list_dismissals') == markers, 'restart rewrote marker')
        for table in ('split_completed_targets', 'split_candidate_history'):
            require(bool(retained[table]), 'completion produced no ' + table)
            require(rows(restarted_database, table) == retained[table], 'reopen changed ' + table)
        require(restarted_service.check_global_dedupe('offline_one', owner_user_id=owner)['seen'],
                'saved account missing from durable global deduplication')
        require(restarted_service.check_global_dedupe('offline_source', owner_user_id=owner)['seen'],
                'source missing from durable global deduplication')
        duplicate_seed = restarted_service.upsert_manual_split_candidates(owner,
            [{'username': 'offline_source', 'queued': True}], include_outcome=True)
        require(duplicate_seed['accepted_count'] == 0 and duplicate_seed['duplicate_count'] == 1,
                'completed source was silently requeued')
        require(not rows(restarted_database, 'split_candidates'), 'duplicate seed added a queue entry')
        second = restarted_service.create_task(owner, name='Cross-task recognition dedupe', modes=['followers'],
            targets=['offline_other_source'], window_ids=['offline-other-window'],
            settings={'live_queue_enabled': False, 'location_enabled': False})
        recognized = restarted_service.discover_task_mode_candidate(owner, second['id'],
            second['targets'][0]['id'], 'followers', 'offline_one')
        require(recognized['duplicate'] is True and recognized['pending'] == 0,
                'stored account re-entered a new task detail queue at recognition')
        require(not restarted_service.list_pending_task_mode_candidates(owner, second['id'],
            second['targets'][0]['id'], 'followers'), 'cross-task duplicate has pending detail work')
        require(rows(restarted_database, 'task_results') == retained['task_results'],
                'cross-task duplicate changed saved results')
        with closing(sqlite3.connect(path)) as connection:
            require(connection.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'database integrity failure')
        return {'verified': True, 'source_calls': len(calls), 'bounded_whole_list_passes': True,
                'same_lease': True, 'budget_persisted_before_extra': not no_gap,
                'extra_pass_restarts_at_top': not no_gap,
                'saved_results': expected_results, 'detail_reads': len(reads),
                'duplicates_never_requeued': True, 'cleanup_before_dismissal': True,
                'retained_data': True, 'database_reopen_persistence': True,
                'single_target_no_duplicate_queue': True, 'source_seed_dedupe': True,
                'cross_task_recognition_dedupe': True, 'completion_history_retained': True,
                'remaining_gap': coverage['unobserved_count']}
    finally:
        await asyncio.wait_for(manager.shutdown(), 10)


def fixture(directory, name, *, mode='followers'):
    path = directory / (name + '.sqlite3')
    database = Database(path)
    database.initialize()
    service = CoreService(database)
    owner = service.register_user('offline-' + name, 'isolated fixture password only')['id']
    task = service.create_task(owner, name='Offline single gap proof', modes=[mode],
        targets=['offline_source'], window_ids=['offline-window'], settings={
            'live_queue_enabled': False, 'local_person_recognition': False, 'location_enabled': False,
            'parallel_screening_workers': 1})
    return path, database, service, owner, task, task['targets'][0]['id']


def visible_profile(username):
    return {'username': username, 'visibility': 'public', 'followers': 120,
            'following': 80, 'posts': 9, 'activity_days': None}


async def pause_restart_scenario(directory):
    """Interrupt the second logical pass, reopen SQLite, and resume its own tail.

    A worker invocation resumed after shutdown is not another from-top pass. We
    report both counts explicitly, so a resumed call cannot masquerade as proof
    of only two physical invocations across an interrupted browser connection.
    """
    path, database, service, owner, task, target_id = fixture(directory, 'pause-restart', mode='following')
    calls = []
    completed_passes = []
    disconnected = []
    closed = []
    extra_checkpointed = asyncio.Event()
    pause_boundary = asyncio.Event()
    allow_checkpoint = asyncio.Event()
    checkpoint_passed = asyncio.Event()
    never_finish = asyncio.Event()
    phase = 0

    def current():
        return service.get_task(owner, task['id'])['targets'][0]

    class Provider:
        def close_profile(self, profile_id):
            require(len(disconnected) > len(closed), 'restart provider close preceded disconnect')
            require(not current()['collection_list_dismissed'], 'restart fixture hid target before cleanup')
            closed.append(profile_id)
            return {'closed': True}

    class Worker:
        supports_candidate_batch_sink = True
        supports_collection_progress_sink = True

        def __init__(self, provider):
            self.profile_id = ''

        async def connect(self, profile_id, **kwargs):
            self.profile_id = profile_id

        async def disconnect(self):
            disconnected.append(self.profile_id)

        async def collect_following(self, source, *, limit, candidate_sink, progress_sink,
                                    initial_candidate_count, initial_resume_tail=None, **kwargs):
            require(limit is None, 'restart relation scan was capped')
            tail = list(initial_resume_tail or [])
            calls.append({'tail': tail, 'lease': owned_lease_token(database, task['id']), 'phase': phase})
            cursor = ExecutionManager._checkpoint_resume_cursor(
                service.get_checkpoint(owner, task['id'], target_id, 'following'))
            if len(calls) == 1:
                await candidate_sink(['restart_one', 'restart_one'])
                await progress_sink({'source_total': 5, 'resume_tail': ['first_pass_tail'],
                                     'rendered_count': 100, 'progress_epoch': 9})
                completed_passes.append('initial')
                return CollectionOutcome('following', [], source_total=5)
            require(cursor.get('automatic_gap_recheck_started') is True, 'restart lost one-extra budget')
            require(not cursor.get('candidate_spool_natural_end'), 'interrupted extra was marked finished')
            if len(calls) == 2:
                require(tail == [], 'extra pass did not restart from top')
                require(initial_candidate_count == 1, 'extra pass ignored durable initial discoveries')
                require('rendered_count' not in cursor and 'progress_epoch' not in cursor,
                        'extra pass retained first-pass position counters')
                await candidate_sink(['restart_one', 'restart_two'])
                await progress_sink({'source_total': 5, 'resume_tail': ['second_pass_tail'],
                                     'rendered_count': 2, 'progress_epoch': 1})
                extra_checkpointed.set()
                await allow_checkpoint.wait()
                pause_boundary.set()
                await self.collection_checkpoint()
                checkpoint_passed.set()
                await never_finish.wait()
            require(len(calls) == 3 and phase == 1, 'a third from-top campaign was scheduled')
            require(tail == ['second_pass_tail'], 'restart failed to resume the interrupted extra tail')
            require(initial_candidate_count == 2, 'restart lost durable extra-pass discoveries')
            await candidate_sink(['restart_one', 'restart_two', 'restart_three'])
            completed_passes.append('extra')
            return CollectionOutcome('following', [], source_total=5)

        async def read_visible_profile(self, username, **kwargs):
            return visible_profile(username)

    manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                               lease_heartbeat_interval_seconds=.03)
    try:
        await manager.start(owner, task['id'])
        await asyncio.wait_for(extra_checkpointed.wait(), 15)
        await manager.pause(owner, task['id'])
        allow_checkpoint.set()
        await asyncio.wait_for(pause_boundary.wait(), 5)
        # Flush the event-loop ready queue. A real pause must block the installed
        # source's checkpoint without adding or replacing the owned lease.
        await asyncio.sleep(0)
        require(not checkpoint_passed.is_set(), 'paused extra pass kept collecting')
        require(service.get_task(owner, task['id'])['status'] == 'paused', 'pause was not durable')
        require(len(calls) == 2 and calls[0]['lease'] == calls[1]['lease'], 'extra pass reacquired the window')
        await manager.resume(owner, task['id'])
        await asyncio.wait_for(checkpoint_passed.wait(), 5)
        require(len(calls) == 2, 'Pause/Continue restarted the extra list')
        await manager.shutdown()
        require(not service.list_browser_lease_states(owner), 'shutdown leaked the original lease')
        require(current()['status'] != 'completed' and not current()['collection_list_dismissed'],
                'interrupted extra pass falsely completed or hid')
        saved_cursor = ExecutionManager._checkpoint_resume_cursor(
            service.get_checkpoint(owner, task['id'], target_id, 'following'))
        require(saved_cursor.get('automatic_gap_recheck_started') is True, 'shutdown erased extra budget')
        require(saved_cursor.get('resume_tail') == ['second_pass_tail'], 'shutdown erased extra position')
        # Construct an independent Database, CoreService and ExecutionManager,
        # just as an installed application reopening the same durable DB does.
        database = Database(path)
        database.initialize()
        service = CoreService(database)
        service.recover_interrupted_operations()
        phase = 1
        require(ExecutionManager._checkpoint_resume_cursor(
            service.get_checkpoint(owner, task['id'], target_id, 'following')) == saved_cursor,
            'database reopening changed the consumed extra-pass cursor')
        manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                                   lease_heartbeat_interval_seconds=.03)
        await manager.retry_target(owner, task['id'], target_id)
        await asyncio.wait_for(manager.wait(task['id']), 20)
        final = current()
        require(final['status'] == 'completed' and final['collection_list_dismissed'],
                'resumed extra did not complete normally after cleanup')
        require(len(calls) == 3 and sum(not call['tail'] for call in calls) == 2,
                'restart created another from-top pass')
        require(completed_passes == ['initial', 'extra'], 'not exactly two successful source passes')
        require(len(closed) == 2 and len(disconnected) == 2, 'restart cleanup count is not exact')
        require(not service.list_browser_lease_states(owner), 'restarted lease remains')
        require(service.task_mode_candidate_stats(owner, task['id'], target_id, 'following')['pending'] == 0,
                'restarted extra left pending detail work')
        require(len(rows(database, 'task_results')) == 3, 'restart lost or duplicated results')
        require(final['mode_coverage']['following']['unobserved_count'] == 2, 'restart falsified remaining gap')
        require(ExecutionManager._checkpoint_resume_cursor(service.get_checkpoint(
            owner, task['id'], target_id, 'following')).get('automatic_gap_recheck_started') is True,
            'final mode completion erased spent extra budget')
        return {'verified': True, 'source_invocations': len(calls), 'from_top_passes': 2,
                'completed_passes': 2, 'pause_resume_same_pass': True, 'database_reopen_persistence': True,
                'extra_pass_resumed_from_saved_tail': True, 'no_third_pass': True,
                'same_lease_before_shutdown': True, 'remaining_gap': 2,
                'completed_and_hidden': True, 'cleanup_after_restart': True}
    finally:
        await asyncio.wait_for(manager.shutdown(), 10)


async def parent_reels_scenario(directory):
    path, database, service, owner, task, target_id = fixture(directory, 'parent-reels')
    sibling = service.create_task(owner, name='Untouched sibling lease', modes=['followers'],
        targets=['offline_sibling'], window_ids=['offline-sibling-window'],
        settings={'live_queue_enabled': False})
    sibling_token = service.acquire_browser_lease(owner, 'offline-sibling-window',
        operation_type='collection', entity_id=sibling['id'])
    sibling_before = service.get_task(owner, sibling['id'])
    source_calls = []
    child_reads = []
    child_closed = []
    order = []
    child_started = asyncio.Event()
    allow_child = asyncio.Event()
    reels_started = asyncio.Event()
    reels_joined = asyncio.Event()
    source_final_return = False

    def current():
        return service.get_task(owner, task['id'])['targets'][0]

    class Provider:
        def close_profile(self, profile_id):
            require(profile_id == 'offline-window', 'cleanup addressed sibling browser')
            require(order[-1] == 'parent_disconnect', 'provider closed before parent disconnect')
            require(child_closed == [1], 'provider closed before child cleanup')
            require(reels_joined.is_set(), 'provider closed before Reels joined')
            require(not current()['collection_list_dismissed'], 'parallel card hidden before cleanup')
            require(current()['status'] == 'completed', 'parallel provider closed before completion')
            order.append('provider_close')
            return {'closed': True}

    class Child:
        async def read_visible_profile(self, username, **kwargs):
            if not kwargs.get('include_activity'):
                child_reads.append(username)
                child_started.set()
                await allow_child.wait()
            return visible_profile(username)

        async def disconnect(self):
            child_closed.append(1)
            order.append('child_disconnect')

    class Worker:
        supports_candidate_batch_sink = True
        supports_parallel_screening_tab = True
        collection_pipeline = 'r59-batch'

        def __init__(self, provider):
            self.profile_id = ''

        async def connect(self, profile_id, **kwargs):
            self.profile_id = profile_id

        async def disconnect(self):
            order.append('parent_disconnect')

        async def create_parallel_screening_worker(self):
            require(not source_calls, 'extra pass created another child pool')
            return Child()

        def parent_reels_adapter(self):
            return self

        async def collect_followers(self, source, *, candidate_sink, **kwargs):
            nonlocal source_final_return
            source_calls.append(owned_lease_token(database, task['id']))
            require(not reels_started.is_set(), 'parent left extraction before final pass')
            require(len(source_calls) <= 2, 'parallel source started a third pass')
            await candidate_sink(['parallel_one', 'parallel_one'])
            await asyncio.wait_for(child_started.wait(), 10)
            require(not child_closed, 'child pool closed between list passes')
            require(not current()['collection_list_dismissed'], 'target hidden before child screening')
            if len(source_calls) == 2:
                source_final_return = True
            return CollectionOutcome('followers', [], source_total=3)

    async def offline_reels(adapter, checkpoint, **kwargs):
        # Only the site adapter is synthetic. The production pipeline's eligibility
        # hook, parent activity owner, joins, child screening and DB writes run.
        require(source_final_return and len(source_calls) == 2, 'Reels started before extra extraction returned')
        require(len(set(source_calls)) == 1, 'parallel extra pass changed lease')
        saved = service.get_checkpoint(owner, task['id'], target_id, 'followers')
        require(saved['cursor'].get('candidate_spool_natural_end') is True, 'Reels preceded durable final extraction')
        require(saved['cursor'].get('automatic_gap_recheck_started') is True, 'Reels observed unspent extra budget')
        require(service.task_mode_candidate_stats(owner, task['id'], target_id, 'followers')['pending'] == 1,
                'Reels did not overlap unfinished child detail work')
        reels_started.set()
        allow_child.set()
        try:
            await asyncio.Event().wait()
        finally:
            reels_joined.set()

    manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                               lease_heartbeat_interval_seconds=.03)
    try:
        with patch('app.parent_reels.run_parent_reels', offline_reels):
            await manager.start(owner, task['id'])
            await asyncio.wait_for(reels_started.wait(), 15)
            await asyncio.wait_for(manager.wait(task['id']), 20)
        require(current()['status'] == 'completed' and current()['collection_list_dismissed'],
                'parallel extra pass did not complete and hide')
        require(child_reads == ['parallel_one'], 'duplicate went through parallel detail screening')
        require(len(rows(database, 'task_results')) == 1, 'parallel result was lost or duplicated')
        require(child_closed == [1] and reels_joined.is_set(), 'child or parent task remains')
        require(order[-1] == 'provider_close', 'provider cleanup did not finish')
        require(owned_lease_token(database, sibling['id']) == sibling_token,
                'parallel completion altered sibling lease ownership')
        require(service.get_task(owner, sibling['id']) == sibling_before,
                'parallel completion altered sibling task')
        remaining_leases = service.list_browser_lease_states(owner)
        require(len(remaining_leases) == 1 and remaining_leases[0]['profile_id'] == 'offline-sibling-window',
                'parallel completion leaked own lease or removed sibling lease')
        return {'verified': True, 'source_calls': 2, 'same_lease': True,
                'one_child_pool': True, 'sibling_lease_untouched': True, 'parent_reels_after_final_extraction': True,
                'children_finish_and_cleanup': True, 'reels_joined_before_cleanup': True,
                'duplicate_detail_reads': 0, 'remaining_gap': current()['mode_coverage']['followers']['unobserved_count']}
    finally:
        allow_child.set()
        await asyncio.wait_for(manager.shutdown(), 10)
        service.release_browser_lease('offline-sibling-window', sibling_token)


async def historical_completed_scenario(directory):
    path, database, service, owner, task, target_id = fixture(directory, 'historical')
    service.upsert_checkpoint(owner, task['id'], target_id, mode='followers', stage='mode_completed',
        cursor={'candidate_spool_version': 1, 'candidate_spool_complete': True,
                'candidate_spool_natural_end': True},
        counters={'source_total': 10, 'discovered': 1, 'processed': 1, 'saved': 1,
                  'pending_candidates': 0}, recoverable=False)
    service.set_target_runtime_status(owner, task['id'], target_id, 'completed', window_id='offline-window')
    service.set_task_runtime_status(owner, task['id'], 'completed')
    before = {table: rows(database, table) for table in ('task_checkpoints', 'task_targets', 'split_candidates')}
    database = Database(path)
    database.initialize()
    service = CoreService(database)
    service.recover_interrupted_operations()
    constructed_workers = []

    def forbidden_worker(provider):
        constructed_workers.append(provider)
        raise RuntimeError('historical completed gap was automatically scheduled')

    manager = ExecutionManager(service, object(), worker_factory=forbidden_worker)
    try:
        try:
            await manager.start(owner, task['id'])
        except (ConflictError, ValidationError):
            pass
        else:
            raise RuntimeError('historical completed task was admitted as runnable work')
        require(not constructed_workers, 'historical gap opened a browser')
        require(not service.list_browser_lease_states(owner), 'historical gap acquired a lease')
        require(all(rows(database, table) == content for table, content in before.items()),
                'reopening rescheduled or rewrote historical completed rows')
        return {'verified': True, 'source_calls': 0, 'no_historical_rescheduling': True,
                'completed_rows_unchanged': True, 'no_browser_or_lease': True}
    finally:
        await manager.shutdown()


async def pending_parent_recheck_scenario(directory, *, during_reels=False, restart=False):
    """Exercise durable admission, real pipeline joins and optional cold retry.

    Only browser workers/Reels and a deterministic final-return gate are fake.
    The manager, service, candidate screening, SQLite and retry entry point are
    production code. No sleeps are used as evidence that a producer has joined.
    """
    from . import execution_manager as execution_module

    name = 'pending-' + ('restart' if restart else 'reels' if during_reels else 'producer')
    path, database, service, owner, task, target_id = fixture(directory, name)
    source_calls, child_reads, committed_reads = [], [], []
    child_closed, parent_closed, provider_closed, reels_runs, reels_joined = [], [], [], [], []
    producer_at_return, allow_producer_return = asyncio.Event(), asyncio.Event()
    first_child_entered, allow_first_child = asyncio.Event(), asyncio.Event()
    allow_remaining_children = asyncio.Event()
    first_reels_entered, reels_cancelled, allow_reels_join = asyncio.Event(), asyncio.Event(), asyncio.Event()
    post_pass_reels = asyncio.Event()
    manual_entered, allow_manual_return = asyncio.Event(), asyncio.Event()
    source_producer = None
    initial_lease = None
    requested_at = None
    phase = 0
    children_created = 0

    def current():
        return service.get_task(owner, task['id'])['targets'][0]

    def checkpoint():
        return service.get_checkpoint(owner, task['id'], target_id, 'followers')

    def durable_request():
        with database.read() as connection:
            request = connection.execute('SELECT * FROM task_source_rechecks WHERE target_id=?',
                                         (target_id,)).fetchone()
        require(request is not None, 'pending request was not persisted')
        return dict(request)

    async def until(predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(poll(), 15)

    original_finish = execution_module.finish_owned

    async def gated_finish(awaitable):
        nonlocal source_producer
        operation = getattr(getattr(awaitable, 'cr_code', None), 'co_name', None)
        result = await original_finish(awaitable)
        if (not during_reels and phase == 0 and operation == 'persist_source_return'
                and result is False and not producer_at_return.is_set()):
            source_producer = asyncio.current_task()
            producer_at_return.set()
            await allow_producer_return.wait()
        return result

    class Provider:
        def close_profile(self, profile_id):
            require(profile_id == 'offline-window', 'pending cleanup addressed another profile')
            require(len(child_closed) == children_created, 'pending cleanup preceded child joins')
            require(len(parent_closed) == phase + 1, 'pending cleanup preceded parent disconnect')
            require(len(reels_joined) == len(reels_runs), 'pending cleanup preceded Reels join')
            provider_closed.append(profile_id)
            return {'closed': True}

    class Child:
        async def read_visible_profile(self, username, **kwargs):
            if not kwargs.get('include_activity'):
                # A stopped, unfinished pending_two may be retried; a committed
                # identity must never enter screening again.
                require(username not in committed_reads, 'completed duplicate reentered detail screening')
                child_reads.append(username)
                if username == 'pending_one':
                    first_child_entered.set()
                    await allow_first_child.wait()
                else:
                    await allow_remaining_children.wait()
                committed_reads.append(username)
            return visible_profile(username)

        async def disconnect(self):
            child_closed.append(1)

    class Worker:
        supports_candidate_batch_sink = True
        supports_collection_progress_sink = True
        supports_parallel_screening_tab = True
        collection_pipeline = 'r59-batch'

        def __init__(self, provider):
            self.profile_id = ''
            self.page = object()

        async def connect(self, profile_id, **kwargs):
            self.profile_id = profile_id

        async def disconnect(self):
            parent_closed.append(1)

        async def create_parallel_screening_worker(self):
            nonlocal children_created
            children_created += 1
            require(children_created == phase + 1, 'manual pass replaced the original child pool')
            return Child()

        def parent_reels_adapter(self):
            return self

        async def collect_followers(self, source, *, candidate_sink, progress_sink,
                                    initial_resume_tail=None, **kwargs):
            require(source == 'offline_source', 'pending pass used another source')
            token = owned_lease_token(database, task['id'])
            source_calls.append(token)
            require(len(source_calls) <= 3, 'manual pass chained an automatic extra pass')
            if len(source_calls) <= 2:
                await candidate_sink(['pending_one', 'pending_one', 'pending_two'])
                await progress_sink({'source_total': 5, 'resume_tail': ['pending_two']})
                await asyncio.wait_for(first_child_entered.wait(), 10)
            else:
                require(not initial_resume_tail, 'manual pass inherited the old source tail')
                if during_reels:
                    require(reels_joined == [1], 'manual source navigated before exact Reels join')
                request = durable_request()
                require(request['state'] == 'active', 'manual navigation preceded durable activation')
                require(request['requested_at'] == requested_at, 'manual pass replaced the admitted generation')
                require(request['request_lease_token'] == token, 'manual pass lacks its exact lease fence')
                require(token != initial_lease if restart else token == initial_lease,
                        'manual pass used the wrong collection lease')
                require(children_created == phase + 1, 'manual pass created another child pool')
                manual_entered.set()
                await candidate_sink(['pending_one', 'pending_two', 'pending_new', 'pending_new'])
                await allow_manual_return.wait()
            return CollectionOutcome('followers', [], source_total=5)

    async def offline_reels(adapter, saved_checkpoint, **kwargs):
        run = len(reels_runs) + 1
        reels_runs.append(run)
        require(checkpoint()['cursor'].get('candidate_spool_natural_end') is True,
                'Reels started before durable source completion')
        if len(source_calls) == 2:
            require(during_reels, 'parent browsed Reels while producer had not returned')
            first_reels_entered.set()
        else:
            require(len(source_calls) == 3, 'Reels resumed without exactly one manual source pass')
            require(service.task_mode_candidate_stats(owner, task['id'], target_id, 'followers')['pending'] > 0,
                    'post-pass Reels did not overlap unfinished children')
            post_pass_reels.set()
        try:
            await asyncio.Event().wait()
        finally:
            if during_reels and run == 1:
                reels_cancelled.set()
                await allow_reels_join.wait()
            reels_joined.append(run)

    manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                               lease_heartbeat_interval_seconds=.03)
    try:
        with patch.object(execution_module, 'finish_owned', gated_finish), \
                patch('app.parent_reels.run_parent_reels', offline_reels):
            await manager.start(owner, task['id'])
            await asyncio.wait_for((first_reels_entered if during_reels else producer_at_return).wait(), 15)
            initial_lease = owned_lease_token(database, task['id'])
            if not during_reels:
                require(source_producer is not None and not source_producer.done(),
                        'pending fixture did not hold the actual producer return')
            before = checkpoint()
            replies = await asyncio.gather(*(manager.recheck_source(
                owner, task['id'], target_id, 'followers') for _ in range(4)))
            require(all(reply.get('waiting_for_safe_point') is True and reply.get('parent_only') is True
                        and reply.get('waiting_for_task_resume') is False for reply in replies),
                    'live pending admission did not return its wait contract')
            generations = {reply['target']['source_recheck_requested_at'] for reply in replies}
            require(len(generations) == 1, 'repeated pending clicks created multiple generations')
            requested_at = generations.pop()
            request = durable_request()
            require(request['state'] == 'prepared' and request['requested_at'] == requested_at
                    and request['request_profile_id'] == 'offline-window'
                    and request['request_lease_token'] == initial_lease,
                    'pending request lost generation or exact source lease identity')
            require(checkpoint() == before, 'pending admission rewound source before its safe point')
            if during_reels:
                await asyncio.wait_for(reels_cancelled.wait(), 10)
                require(not reels_joined and not manual_entered.is_set(),
                        'manual pass did not wait for in-flight parent join')
            allow_first_child.set()
            await until(lambda: len(rows(database, 'task_results')) == 1)
            require(len(rows(database, 'task_results')) == 1, 'child did not commit while recheck pending')
            require(durable_request()['state'] == 'prepared', 'child progress activated pending source early')
            require(checkpoint()['cursor'].get('candidate_spool_natural_end') is True,
                    'pending child progress erased original natural-end cursor')
            require(len(source_calls) == 2 and not manual_entered.is_set(), 'pending source navigated early')
            require(not child_closed and not provider_closed, 'pending request stopped healthy children')
            require(owned_lease_token(database, task['id']) == initial_lease, 'pending request changed lease')

            if restart:
                await asyncio.wait_for(manager.stop(owner, task['id']), 15)
                await asyncio.wait_for(manager.wait(task['id']), 10)
                require(not service.list_browser_lease_states(owner), 'stop retained the previous collection lease')
                retained_request = durable_request()
                require(retained_request['state'] == 'prepared' and retained_request['requested_at'] == requested_at,
                        'stop discarded or activated the prepared generation')
                require(current()['status'] != 'completed' and not current()['collection_list_dismissed'],
                        'stop completed or hid the pending request')
                require(len(source_calls) == 2, 'stop executed the pending source pass')
                await manager.shutdown()
                database = Database(path)
                database.initialize()
                service = CoreService(database)
                service.recover_interrupted_operations()
                require(durable_request() == retained_request, 'database reopen replaced durable pending intent')
                require(not service.list_browser_lease_states(owner), 'reopen resurrected an old collection lease')
                require(len(rows(database, 'task_results')) == 1, 'reopen lost already committed child result')
                phase = 1
                manager = ExecutionManager(service, Provider(), worker_factory=Worker,
                                           lease_heartbeat_interval_seconds=.03)
                require(len(source_calls) == 2, 'reopen automatically navigated before explicit retry')
                await manager.retry_target(owner, task['id'], target_id)
            else:
                allow_reels_join.set()
                allow_producer_return.set()
            await asyncio.wait_for(manual_entered.wait(), 15)
            require(len(source_calls) == 3, 'one admitted generation did not run exactly one manual pass')
            allow_manual_return.set()
            await asyncio.wait_for(post_pass_reels.wait(), 15)
            require(checkpoint()['cursor'].get('automatic_gap_recheck_started') is True,
                    'manual pass lost its no-extra-loop budget')
            allow_remaining_children.set()
            await asyncio.wait_for(manager.wait(task['id']), 20)

        final = current()
        require(final['status'] == 'completed' and final['collection_list_dismissed'],
                'manual pending pass did not complete normal cleanup')
        require(durable_request()['state'] == 'completed' and durable_request()['requested_at'] == requested_at,
                'completion lost the original admitted request generation')
        require(len(source_calls) == 3, 'manual pass automatically scheduled another loop')
        require(Counter(committed_reads) == Counter(['pending_one', 'pending_two', 'pending_new']),
                'manual duplicate candidates were lost or screened again')
        require(child_reads.count('pending_one') == child_reads.count('pending_new') == 1,
                'manual duplicate candidates reentered a completed child detail read')
        require(len(rows(database, 'task_results')) == 3 and len(rows(database, 'task_mode_candidates')) == 3,
                'manual pass duplicated durable candidate or result rows')
        require(len(child_closed) == children_created == phase + 1 and len(provider_closed) == phase + 1,
                'manual pending cleanup leaked or replaced child ownership')
        require(reels_joined == reels_runs and len(reels_runs) == (2 if during_reels else 1),
                'post-pass Reels did not resume or join cleanly')
        require(not service.list_browser_lease_states(owner), 'manual pending pass leaked a collection lease')
        require(not rows(database, 'split_candidates'), 'manual source pass created duplicate split work')
        require(final['mode_coverage']['followers']['unobserved_count'] == 2, 'manual pass hid the remaining gap')
        before_completed = {table: rows(database, table) for table in
                            ('task_targets', 'task_checkpoints', 'task_source_rechecks', 'task_results')}
        try:
            await manager.recheck_source(owner, task['id'], target_id, 'followers')
        except (ConflictError, ValidationError):
            pass
        else:
            raise RuntimeError('completed manual target was resurrected by a late recheck')
        require(all(rows(database, table) == content for table, content in before_completed.items()),
                'late recheck changed completed durable rows')
        return {'verified': True, 'source_calls': 3, 'manual_passes': 1,
                'child_commits_while_pending': 1, 'saved_results': 3, 'remaining_gap': 2,
                'reels_runs': len(reels_runs), 'pending_request_durable': True,
                'duplicate_clicks_coalesced': True, 'cursor_unchanged_until_safe_point': True,
                'children_continue_while_pending': True, 'exact_lease_fenced': True,
                'post_pass_reels_resumed': True, 'no_automatic_extra_manual_loop': True,
                'duplicates_never_requeued': True, 'cleanup_joined': True,
                'completed_task_not_resurrected': True,
                **({'producer_not_returned_at_admission': True} if not during_reels else
                   {'parent_cancel_join_before_navigation': True, 'same_child_pool_and_lease': True}),
                **({'stop_preserves_prepared_request': True, 'database_reopen_persistence': True,
                    'explicit_retry_same_generation': True, 'new_lease_after_previous_released': True,
                    'no_automatic_restart': True} if restart else {})}
    finally:
        for event in (allow_producer_return, allow_first_child, allow_remaining_children,
                      allow_reels_join, allow_manual_return):
            event.set()
        await asyncio.wait_for(manager.shutdown(), 15)


async def run_selftest() -> dict:
    def network_forbidden(*args, **kwargs):
        raise RuntimeError('Offline self-test attempted network access')
    with tempfile.TemporaryDirectory(prefix='Juxin-CollectionCompletion-') as temporary:
        with patch.object(socket.socket, 'connect', network_forbidden), patch.object(socket, 'create_connection', network_forbidden):
            directory = Path(temporary)
            cases = {}
            for name, options in (
                ('normal_with_truthful_gap', {}),
                ('normal_without_gap', {'no_gap': True}), ('abnormal_source_retained', {'abnormal': True}),
                ('extra_pass_failure_retained', {'extra_failure': True}),
            ):
                cases[name] = await scenario(directory, name, **options)
            cases['pause_restart_extra_pass'] = await pause_restart_scenario(directory)
            cases['parent_reels_after_final_pass'] = await parent_reels_scenario(directory)
            cases['historical_completed_gap'] = await historical_completed_scenario(directory)
            cases['manual_pending_before_producer_return'] = await pending_parent_recheck_scenario(directory)
            cases['manual_pending_parent_join'] = await pending_parent_recheck_scenario(directory, during_reels=True)
            cases['manual_pending_stop_reopen_retry'] = await pending_parent_recheck_scenario(directory, restart=True)
            from .final_seed_completion_selftest import final_seed_scenario, factory_reconnect_scenario
            cases['final_seed_single_source'] = await final_seed_scenario(directory, source_count=1, child_count=1)
            cases['final_seed_two_sources'] = await final_seed_scenario(directory, source_count=2, child_count=3)
            cases['final_seed_multiple_sources'] = await final_seed_scenario(directory, source_count=3, child_count=3)
            for reason in ('worker_not_connected', 'browser_context_missing'):
                cases['final_seed_factory_' + reason] = await factory_reconnect_scenario(directory, failure_reason=reason)
    return {'verified': True, 'synthetic': True, 'live_accounts_tested': False,
            'network_disabled': True, 'user_data_touched': False,
            'production_manager_and_service': True, 'cases': cases,
            'final_seed_completion': {'verified': True, 'production_playwright_pool': True,
                'late_idle_children_retired': True, 'empty_queue_after_last_source': True,
                'authoritative_factory_reconnect': True,
                'exact_lease_until_confirmed_close': True, 'results_dedupe_history_preserved': True,
                'synthetic': True, 'live_accounts_tested': False},
            'single_gap_recheck': {'verified': True, 'no_gap_passes': 1, 'gap_passes': 2,
                'restart_from_top': True, 'durable_per_target_mode_budget': True,
                'pause_resume_and_restart': True, 'same_collection_lease': True, 'sibling_lease_untouched': True,
                'persistent_gap_completes_truthfully': True, 'failures_remain_incomplete': True,
                'no_historical_rescheduling': True, 'parent_reels_after_final_extraction': True,
                'children_finish_and_cleanup': True, 'synthetic': True, 'live_accounts_tested': False},
            'manual_parent_recheck': {'verified': True, 'durable_pending_admission': True,
                'producer_return_and_parent_join_fenced': True, 'children_continue_while_pending': True,
                'single_manual_pass': True, 'post_pass_reels_resumed': True,
                'stop_reopen_explicit_retry_same_generation': True, 'new_lease_only_after_old_released': True,
                'duplicates_never_requeued': True, 'completed_task_not_resurrected': True,
                'synthetic': True, 'live_accounts_tested': False}}


def main() -> None:
    proof = asyncio.run(run_selftest())
    print(PROOF_PREFIX + json.dumps(proof, sort_keys=True))
