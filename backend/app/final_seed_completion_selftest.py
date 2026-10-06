"""Offline final-seed proof using production scheduling, storage and page pools.

Only native page/session responses and source/profile observations are fabricated.
No browser, account, credentials, network or user data are accessed.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from .database import Database
from .execution_manager import ExecutionManager
from .playwright_worker import CollectionOutcome, PlaywrightWorker
from .service import CoreService


def require(value, message):
    if not value:
        raise RuntimeError('Final-seed completion proof: ' + message)


def rows(database, table):
    with database.read() as connection:
        return [tuple(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


class _LeaseEvidenceReader:
    """Reuse only the fixture's lease observer, never production DB connections."""
    def __init__(self, path):
        self.path = path
        self._connection = None
        self._lock = threading.RLock()
        self._closed = False

    @contextmanager
    def read(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Fixture lease observer is closed')
            if self._connection is None:
                connection = sqlite3.connect(self.path, timeout=30.0,
                    isolation_level=None, check_same_thread=False)
                try:
                    connection.row_factory = sqlite3.Row
                    connection.execute('PRAGMA query_only=ON')
                except BaseException:
                    connection.close()
                    raise
                self._connection = connection
            # Autocommit sees every real commit; no rows/tokens are cached.
            yield self._connection

    def close(self):
        with self._lock:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None


async def final_seed_scenario(directory: Path, *, source_count: int, child_count: int, completion_timeout: float = 30.0) -> dict:
    """A late child metadata command can settle only when its owned page closes."""
    name = f'final-seed-{source_count}-{child_count}'
    database = Database(directory / (name + '.sqlite3'))
    database.initialize()
    service = CoreService(database)
    owner = service.register_user(name, 'offline fixture password only')['id']
    with database.write() as connection:
        connection.execute('INSERT INTO native_browser_profiles VALUES(?,?,?,?,?,?,?,?)',
            ('native:offline-final', owner, 1, 'Offline final', '', '', '2026-01-01', '2026-01-01'))
    sources = [f'final_source_{index}' for index in range(source_count)]
    service.upsert_manual_split_candidates(owner, [{'username': source, 'queued': True} for source in sources])
    task = service.create_task(owner, name='Offline final seed ownership proof', modes=['followers'],
        targets=[], window_ids=['native:offline-final'], settings={
            'live_queue_enabled': True, 'parallel_screening_workers': child_count,
            'local_person_recognition': False, 'location_enabled': False})
    source_calls, detail_reads, pages, parents, lease_tokens, close_calls = [], [], [], [], [], []
    late_started, late_finished = [], []
    retained = {}
    abort_cleanup = False
    max_native_pages = 0
    evidence = _LeaseEvidenceReader(database.path)

    def current_task():
        return service.get_task(owner, task['id'])

    def token():
        with evidence.read() as connection:
            found = connection.execute('SELECT lease_token FROM browser_operation_leases WHERE entity_id=?',
                                       (task['id'],)).fetchall()
        require(len(found) == 1, 'window lost its single original collection lease')
        return found[0][0]

    class Page:
        def __init__(self, label, *, source=False):
            self.label, self.source = label, source
            self.closed = asyncio.Event()
            self.url = 'about:blank'
            self.close_calls = 0
        def is_closed(self):
            return self.closed.is_set()
        async def evaluate(self, expression, *args):
            require(expression == '() => 1', 'unexpected synthetic browser evaluation')
            return 1
        async def close(self):
            require(not self.source, 'operator source page must not be closed by child cleanup')
            self.close_calls += 1
            require(self.close_calls == 1, 'native child close repeated')
            self.closed.set()
            await asyncio.sleep(0)

    class Session:
        def __init__(self, page):
            self.page = page
        async def send(self, method, params=None):
            if method == 'Target.getTargetInfo':
                if not self.page.source:
                    late_started.append(self.page.label)
                    # Model a CDP metadata call that ignores cancellation but is
                    # naturally rejected/settled after the same target is retired.
                    while not self.page.closed.is_set():
                        try:
                            await self.page.closed.wait()
                        except asyncio.CancelledError:
                            pass
                    late_finished.append(self.page.label)
                return {'targetInfo': {'targetId': self.page.label}}
            require(method in {'Page.setWebLifecycleState', 'Emulation.setFocusEmulationEnabled'},
                    'unexpected synthetic CDP command')
            return {}
        async def detach(self):
            pass

    class Context:
        async def new_page(self):
            nonlocal max_native_pages
            page = Page('child-' + str(len(pages)))
            pages.append(page)
            count = sum(not item.is_closed() for item in pages)
            max_native_pages = max(max_native_pages, count)
            require(count <= child_count, 'source handoff exceeded the selected physical child pool')
            return page
        async def new_cdp_session(self, page):
            return Session(page)
        async def cookies(self, *_):
            return []

    class Provider:
        native = SimpleNamespace(bridge=SimpleNamespace(call=lambda *args, **kwargs: None))
        def close_profile(self, profile_id):
            if abort_cleanup:
                return {'closed': True}
            require(profile_id == 'native:offline-final', 'wrong window closed')
            lease_tokens.append(token())
            targets = current_task()['targets']
            require(len(targets) == source_count and all(item['status'] == 'completed' for item in targets),
                    'provider close preceded final durable source completion')
            require(not rows(database, 'split_candidates'), 'provider close preceded final seed queue drain')
            for target in targets:
                stats = service.task_mode_candidate_stats(owner, task['id'], target['id'], 'followers')
                require(stats['pending'] == 0 and stats['recorded'] + stats['deduped'] == stats['total'],
                        'provider close preceded durable candidate drain')
            require(all(not item['collection_list_dismissed'] for item in targets),
                    'completed card hidden before provider acknowledgement')
            require(all(page.is_closed() for page in pages), 'provider closed with owned child pages live')
            require(sorted(late_started) == sorted(late_finished), 'provider closed before late child commands joined')
            require(all(not parent._screening_worker_pool and not parent._late_lifecycle_tasks for parent in parents),
                    'provider close preceded production pool/late task cleanup')
            for table in ('task_results', 'task_mode_candidates', 'global_seen', 'workbench_identity_claims',
                          'split_completed_targets', 'split_candidate_history'):
                retained[table] = rows(database, table)
            close_calls.append(profile_id)
            return {'closed': True}

    class Parent(PlaywrightWorker):
        parent_reels_adapter = None
        def __init__(self, provider):
            super().__init__(provider)
            self.cdp_command_timeout_seconds = .005
            self.disconnect_timeout_seconds = .2
            self.source_page = Page('operator-source', source=True)
            parents.append(self)
        async def connect(self, profile_id, **kwargs):
            require(self.page is None, 'unexpected reconnect rather than idle-child retirement')
            self.profile_id = profile_id
            self.page = self.source_page
            self._context = Context()
            self._browser = SimpleNamespace(is_connected=lambda: True)
            self._cdp_session = Session(self.page)
        async def create_parallel_screening_worker(self):
            child = await super().create_parallel_screening_worker()
            require(child._late_lifecycle_tasks, 'fixture failed to create a real late metadata task')
            lease_tokens.append(token())
            async def read_profile(username, **kwargs):
                detail_reads.append(username)
                return {'username': username, 'visibility': 'private', 'followers': 80,
                        'following': 30, 'posts': 5, 'activity_days': None}
            child.read_visible_profile = read_profile
            child.capture_visible_review_snapshot = None
            return child
        async def collect_followers(self, source, *, candidate_sink, **kwargs):
            source_calls.append(source)
            lease_tokens.append(token())
            name = source + '_candidate'
            await candidate_sink([name, name])
            return CollectionOutcome('followers', [], source_total=1)

    provider = Provider()
    manager = ExecutionManager(service, provider, worker_factory=Parent,
        network_retry_delays=(.01,), recovery_cooldown_seconds=.01,
        network_retry_stagger_seconds=0, lease_heartbeat_interval_seconds=.05)
    try:
        await manager.start(owner, task['id'])
        try:
            await asyncio.wait_for(asyncio.shield(manager.wait(task['id'])), completion_timeout)
        except TimeoutError as error:
            with database.read() as connection:
                queued = connection.execute(
                    "SELECT COUNT(*) FROM split_candidates WHERE owner_user_id=? AND queue_state='queued'",
                    (owner,),
                ).fetchone()[0]
            raise RuntimeError(f'{name} did not drain final source: '
                               f'calls={source_calls!r}; targets={[(row["username"],row["status"]) for row in current_task()["targets"]]!r}; '
                               f'live_children={sum(not page.is_closed() for page in pages)}; waiting_split_count={queued}') from error
        final = current_task()
        require(final['status'] == 'completed', 'task did not complete')
        require(len(final['targets']) == source_count, 'source ownership duplicated')
        require(all(item['status'] == 'completed' and item['collection_list_dismissed'] for item in final['targets']),
                'confirmed clean completion did not hide each source card')
        require(Counter(source_calls) == Counter(sources), 'source repeated or skipped')
        require(Counter(detail_reads) == Counter(source + '_candidate' for source in sources),
                'duplicate detail reads or missing saved candidate')
        require(len(rows(database, 'task_results')) == source_count, 'saved results missing or duplicated')
        require(len(rows(database, 'task_mode_candidates')) == source_count, 'durable spool identities changed')
        require(not rows(database, 'split_candidates'), 'final claimed seed remains queued')
        require(not service.list_browser_lease_states(owner), 'lease remained after confirmed cleanup')
        require(len(set(lease_tokens)) == 1, 'one window switched lease generations between sources')
        require(close_calls == ['native:offline-final'], 'window was not closed exactly once')
        require(all(rows(database, table) == before for table, before in retained.items()),
                'completion/card cleanup mutated retained business data')
        require(all(page.close_calls == 1 for page in pages), 'owned pages were leaked or double-closed')
        # Reopening the database must retain results, identity dedupe and complete
        # history; this proves persistence, without starting historical work again.
        evidence.close()
        reopened = Database(database.path)
        reopened.initialize()
        require(all(rows(reopened, table) == before for table, before in retained.items()),
                'database reopen changed retained results/dedupe/history')
        reopened_service = CoreService(reopened)
        for source in sources:
            require(reopened_service.check_global_dedupe(source, owner_user_id=owner)['seen'],
                    'completed source lost durable dedupe identity')
            require(reopened_service.check_global_dedupe(source + '_candidate', owner_user_id=owner)['seen'],
                    'saved candidate lost durable dedupe identity')
        require(len(retained['split_completed_targets']) == source_count
                and len(retained['split_candidate_history']) == source_count,
                'completed split inventory/history missing')
        return {'verified': True, 'source_count': source_count, 'source_calls': len(source_calls),
                'saved_results': source_count, 'selected_children': child_count,
                'provider_closes': 1, 'lease_generations': 1,
                'production_playwright_pool': True, 'late_metadata_requires_owned_close': True,
                'selected_pool_bound': max_native_pages <= child_count,
                'all_sources_complete': True, 'durable_candidates_drained': True,
                'duplicate_details_prevented': True, 'children_joined_before_provider_close': True,
                'cards_hidden_after_close_ack': True, 'results_dedupe_history_preserved': True,
                'database_reopen_persistence': True, 'no_remaining_seed_or_lease': True}
    finally:
        abort_cleanup = True
        try:
            await asyncio.wait_for(manager.shutdown(), 15)
        finally:
            evidence.close()


async def factory_reconnect_scenario(directory: Path, *, failure_reason: str,
                                     completion_timeout: float = 30.0) -> dict:
    """A responsive parent tab cannot override an authoritative factory failure."""
    from .playwright_worker import WorkerExecutionError
    require(failure_reason in {'worker_not_connected', 'browser_context_missing'}, 'invalid reconnect fixture cause')
    database = Database(directory / ('factory-' + failure_reason + '.sqlite3'))
    database.initialize()
    service = CoreService(database)
    owner = service.register_user('factory-' + failure_reason, 'offline fixture password only')['id']
    sources = ['reconnect_source_a', 'reconnect_source_b']
    service.upsert_manual_split_candidates(owner, [{'username': source, 'queued': True} for source in sources])
    profile_id = 'offline-factory-window'
    task = service.create_task(owner, name='Offline authoritative factory reconnect proof', modes=['followers'],
        targets=[], window_ids=[profile_id], settings={
            'live_queue_enabled': True, 'parallel_screening_workers': 1,
            'local_person_recognition': False, 'location_enabled': False})
    parents, source_calls, detail_reads, lease_tokens, provider_closes = [], [], [], [], []
    child_closed, retained, observed_causes = [], {}, []
    abort_cleanup = False
    evidence = _LeaseEvidenceReader(database.path)

    def current():
        return service.get_task(owner, task['id'])

    def token():
        with evidence.read() as connection:
            found = connection.execute('SELECT lease_token FROM browser_operation_leases WHERE entity_id=?',
                                       (task['id'],)).fetchall()
        require(len(found) == 1, 'factory reconnect lost the owned lease')
        return found[0][0]

    class Provider:
        def close_profile(self, profile):
            if abort_cleanup:
                return {'closed': True}
            require(profile == profile_id, 'factory reconnect closed wrong profile')
            lease_tokens.append(token())
            targets = current()['targets']
            require(len(targets) == 2 and all(target['status'] == 'completed' for target in targets),
                    'factory failure was force-completed instead of reading final source')
            require(all(not target['collection_list_dismissed'] for target in targets),
                    'factory reconnect hid cards before close acknowledgement')
            require(len(child_closed) == 2 and parents[0].disconnects == 2 and not parents[0].children,
                    'factory reconnect did not join child/parent cleanup')
            for table in ('task_results', 'task_mode_candidates', 'global_seen', 'workbench_identity_claims',
                          'split_completed_targets', 'split_candidate_history'):
                retained[table] = rows(database, table)
            provider_closes.append(profile)
            return {'closed': True}

    class Child:
        async def read_visible_profile(self, username, **kwargs):
            detail_reads.append(username)
            return {'username': username, 'visibility': 'private', 'followers': 60,
                    'following': 20, 'posts': 4, 'activity_days': None}
        async def disconnect(self):
            require(self not in child_closed, 'factory reconnect repeated a child close')
            child_closed.append(self)

    class Parent:
        supports_candidate_batch_sink = True
        supports_parallel_screening_tab = True
        supports_single_candidate_handoff = True
        def __init__(self, provider):
            self.connects = self.disconnects = self.factory_failures = self.probes = 0
            self.children = []
            self.page = object()
            parents.append(self)
        async def connect(self, profile, **kwargs):
            require(profile == profile_id and not self.children, 'reconnect raced old child cleanup')
            self.connects += 1
            self.profile_id = profile
            lease_tokens.append(token())
        async def disconnect(self):
            self.disconnects += 1
            if self.factory_failures and self.connects == 1 and not abort_cleanup:
                target = next(row for row in current()['targets'] if row['status'] != 'completed')
                saved = service.get_checkpoint(owner, task['id'], target['id'], 'followers')
                observed_causes.append(saved['counters'].get('reason'))
                require(observed_causes[-1] == failure_reason, 'authoritative factory error was masked')
            await asyncio.gather(*(child.disconnect() for child in self.children))
            self.children.clear()
        async def connection_healthy(self):
            self.probes += 1
            return True
        async def create_parallel_screening_worker(self):
            if source_calls and self.connects == 1:
                self.factory_failures += 1
                require(await self.connection_healthy(), 'fixture parent probe was not healthy')
                targets = current()['targets']
                require(sum(row['status'] == 'completed' for row in targets) == 1,
                        'factory fault did not occur after preceding source completed')
                require(len(rows(database, 'task_results')) == 1, 'first source result missing before factory failure')
                with database.read() as connection:
                    waiting = connection.execute("SELECT COUNT(*) FROM split_candidates WHERE owner_user_id=? AND queue_state='queued'",
                                                 (owner,)).fetchone()[0]
                require(waiting == 0, 'factory fault must occur after final seed claim emptied queue')
                raise WorkerExecutionError('Synthetic factory context requires reconnect',
                                           reason=failure_reason, pause_required=True)
            child = Child()
            self.children.append(child)
            return child
        def release_parallel_screening_worker(self, child):
            return child in self.children
        def prepare_page_retry(self, target):
            pass
        def request_page_replacement(self, *args):
            raise RuntimeError('Factory context failure must reconnect, not rewind a healthy source tab')
        async def collect_followers(self, source, *, candidate_sink, **kwargs):
            source_calls.append(source)
            lease_tokens.append(token())
            await candidate_sink([source + '_candidate', source + '_candidate'])
            return CollectionOutcome('followers', [], source_total=1)

    manager = ExecutionManager(service, Provider(), worker_factory=Parent,
        network_retry_delays=(.01,), recovery_cooldown_seconds=.01,
        network_retry_stagger_seconds=0, lease_heartbeat_interval_seconds=.05)
    try:
        await manager.start(owner, task['id'])
        control = manager._runs[task['id']]
        try:
            await asyncio.wait_for(asyncio.shield(manager.wait(task['id'])), completion_timeout)
        except TimeoutError as error:
            parent = parents[0]
            raise RuntimeError(f'Factory {failure_reason} did not recover final seed: '
                f'connects={parent.connects}; factory_failures={parent.factory_failures}; '
                f'source_calls={source_calls!r}; queued_seed_count=0') from error
        parent, final = parents[0], current()
        require(parent.connects == 2 and parent.disconnects == 2 and parent.factory_failures == 1,
                'factory recovery did not perform exactly one reconnect')
        require(parent.probes >= 1 and observed_causes == [failure_reason],
                'factory cause/healthy parent probe evidence missing')
        require(Counter(source_calls) == Counter(sources), 'factory recovery skipped or repeated a source')
        require(Counter(detail_reads) == Counter(source + '_candidate' for source in sources),
                'factory recovery skipped or repeated candidate detail reads')
        require(final['status'] == 'completed' and all(row['status'] == 'completed' and row['collection_list_dismissed']
                for row in final['targets']), 'factory recovery did not complete and hide both sources')
        require(provider_closes == [profile_id] and len(set(lease_tokens)) == 1,
                'factory recovery changed lease or closed window multiple times')
        require(not control.leases and not control.network_waiters and not control.worker_tasks,
                'factory recovery left leases, network waiters or child workers registered')
        require(not service.list_browser_lease_states(owner) and not rows(database, 'split_candidates'),
                'factory recovery left durable lease or seed queue')
        require(len(rows(database, 'task_results')) == 2 and len(rows(database, 'task_mode_candidates')) == 2,
                'factory recovery lost saved results or candidate inventory')
        require(all(rows(database, table) == before for table, before in retained.items()),
                'factory completion cleanup mutated retained inventory')
        evidence.close()
        reopened = Database(database.path)
        reopened.initialize()
        require(all(rows(reopened, table) == before for table, before in retained.items()),
                'factory recovery inventory did not persist after reopen')
        reopened_service = CoreService(reopened)
        for source in sources:
            require(reopened_service.check_global_dedupe(source, owner_user_id=owner)['seen']
                    and reopened_service.check_global_dedupe(source + '_candidate', owner_user_id=owner)['seen'],
                    'factory recovery lost source/candidate dedupe')
        require(len(retained['split_completed_targets']) == len(retained['split_candidate_history']) == 2,
                'factory recovery lost completed source history')
        return {'verified': True, 'injected_reason': failure_reason, 'source_count': 2,
                'source_calls': 2, 'saved_results': 2, 'factory_failures': 1,
                'connections': 2, 'reconnects': 1, 'provider_closes': 1, 'lease_generations': 1,
                'failure_after_first_source_completed': True, 'waiting_queue_empty_at_failure': True,
                'parent_health_probe_true': True, 'authoritative_cause_preserved': True,
                'final_source_read_after_reconnect': True, 'both_sources_completed': True,
                'children_joined_before_close': True, 'cards_hidden_after_close_ack': True,
                'no_remaining_seed_lease_or_waiter': True, 'results_dedupe_history_preserved': True,
                'database_reopen_persistence': True}
    finally:
        abort_cleanup = True
        try:
            await asyncio.wait_for(manager.shutdown(), 15)
        finally:
            evidence.close()
