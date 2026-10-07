"""Installed-Core upgrade proof over an externally persisted R6.2 database.

This module NEVER seeds or substitutes a database. The outer verifier creates
one from the delivered R6.2 schema before spawning this process, then independently
checks its mutations after exit. Production SQLite, service, manager, owner lookup,
and EmbeddedBrowser.closed_profile_guard are used; only desktop RPC is synthetic.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
from unittest.mock import patch

PROOF_PREFIX = 'NURTURE_CLEANUP_UPGRADE_SELFTEST=PASS '
CONTRACT = 'nurture-orphan-hold-upgrade-v1'
CHRONOLOGY_CLOCK = 'perf_counter_ns-system-v1'
BASELINE_COMMIT = '252590e257fbbc0e1974ad727e2086ab858bb2c6'
RECOVERED = ('closed_legacy', 'closed_lost', 'archived_closed', 'prepared_posting')
RETAINED = ('open', 'opening', 'closing', 'successor_lease', 'fresh_account_lease', 'queued_collection', 'queued_studio',
            'queued_monitor', 'queued_action', 'unknown_action',
            'inflight', 'unknown_profile', 'foreign_profile_owner', 'unknown_state')


def require(value, message):
    if not value:
        raise RuntimeError('Installed nurture upgrade: ' + message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def history(row):
    value = dict(row)
    value.pop('message')
    result = json.loads(value.pop('result_json'))
    result.pop('window_hold', None)
    result.pop('window_cleanup', None)
    value['result'] = result
    return value


def rows(database, table):
    with database.read() as c:
        return [dict(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


class OfflineLegacy:
    def start(self):
        pass
    def shutdown(self):
        pass
    def list_all_windows(self, **kwargs):
        return {'windows': [], 'stale': False}


class OfflineDesktop:
    """No native windows; RPC evidence exercises the real Core admission fences."""
    def __init__(self, inputs):
        self.names = {case['profile_id']: name for name, case in inputs.items()}
        self.calls = []

    def call(self, method, **body):
        from .errors import ConflictError
        self.calls.append((method, body))
        if method == 'inventory':
            return {'profiles': []}
        if method == 'hide':
            return {'hidden': True}
        require(method == 'confirm-closed', 'recovery attempted a browser effect: ' + method)
        name = self.names[body['profile']]
        if name == 'open':
            raise ConflictError('Synthetic desktop profile is still open')
        if name == 'unknown_state':
            return {'closed': None, 'verification': 'unknown'}
        return {'closed': True, 'profile_id': body['profile'], 'owner_user_id': body['owner'],
                'verification': 'desktop-absence-v1'}

    def confirmations(self, profile):
        return sum(method == 'confirm-closed' and body['profile'] == profile for method, body in self.calls)


async def run_selftest(manifest_path):
    # Validate pre-existence and input hash BEFORE candidate Database.initialize().
    opened_perf_ns = time.perf_counter_ns()
    opened_ns = time.time_ns()
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    require(manifest.get('contract') == CONTRACT and manifest.get('baseline_commit') == BASELINE_COMMIT,
            'missing delivered R6.2 fixture provenance')
    require(set(manifest.get('cases', {})) == set(RECOVERED + RETAINED), 'incomplete upgrade input cases')
    require(manifest.get('database_name') == 'legacy-r62.sqlite3', 'unexpected input database')
    require(type(manifest.get('seed_pid')) is int and manifest['seed_pid'] != os.getpid(),
            'database was not seeded by an earlier independent process')
    require(manifest.get('chronology_clock') == CHRONOLOGY_CLOCK
            and type(manifest.get('seed_completed_perf_ns')) is int
            and 0 < manifest['seed_completed_perf_ns'] < opened_perf_ns,
            'database was not persisted before this process opened it: ' + json.dumps({
                'chronology_clock': manifest.get('chronology_clock'),
                'seed_completed_perf_ns': manifest.get('seed_completed_perf_ns'), 'opened_perf_ns': opened_perf_ns,
                'seed_completed_ns': manifest.get('seed_completed_ns'), 'opened_ns': opened_ns,
                'seed_pid': manifest.get('seed_pid'), 'process_pid': os.getpid()}, sort_keys=True))
    directory = manifest_path.parent
    path = directory / manifest['database_name']
    require(path.is_file() and not path.is_symlink() and file_sha256(path) == manifest.get('database_sha256'),
            'persisted pre-start database bytes do not match the external seed')
    with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        before = {row['id']: dict(row) for row in c.execute('SELECT * FROM studio_jobs')}
        require(before == manifest['before_jobs'], 'persisted historical rows do not match the pre-start seed')
        require(c.execute('SELECT username_norm FROM app_users WHERE id=?', (manifest['owner'],)).fetchone()[0]
                == 'offline-upgrade-owner', 'not an isolated synthetic database')
    attempted = {'network': 0, 'activity': 0}
    def forbidden_network(*args, **kwargs):
        attempted['network'] += 1
        raise RuntimeError('Offline upgrade attempted network access')
    def forbidden_activity(*args, **kwargs):
        attempted['activity'] += 1
        raise RuntimeError('Historical upgrade replayed browser activity')
    with ExitStack() as guard:
        for name in ('connect', 'connect_ex', 'sendto'):
            guard.enter_context(patch.object(socket.socket, name, forbidden_network))
        for name in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex'):
            guard.enter_context(patch.object(socket, name, forbidden_network))
        guard.enter_context(patch(__package__ + '.studio.PlaywrightWorker', side_effect=forbidden_activity))
        guard.enter_context(patch(__package__ + '.native_browser._spawn_browser_process', side_effect=forbidden_activity))
        from .database import Database
        from .service import CoreService
        from .studio import StudioManager
        from .embedded_browser import EmbeddedBrowser
        from .native_browser import BrowserHub
        from .account_workspace import AccountWorkspace
        from .errors import ConflictError, NotFoundError, UpstreamUnavailableError
        from .main import create_app
        from .config import Settings
        from .follow_monitor import FollowMonitorManager
        startup_events = []
        def observe(cls, method, label):
            original = getattr(cls, method)
            def call(instance, *args, **kwargs):
                startup_events.append(label)
                return original(instance, *args, **kwargs)
            guard.enter_context(patch.object(cls, method, call))
        for cls, method, label in ((Database, 'initialize', 'database.initialize'),
            (CoreService, 'recover_interrupted_operations', 'service.recover_interrupted_operations'),
            (FollowMonitorManager, 'recover_interrupted', 'monitor.recover_interrupted'),
            (StudioManager, 'recover', 'studio.recover'),
            (AccountWorkspace, 'recover', 'accounts.recover'),
            (StudioManager, 'start_scheduler', 'studio.start_scheduler')):
            observe(cls, method, label)
        from . import main as main_module
        retire = main_module.retire_legacy_posting
        def observe_retirement(*args, **kwargs):
            startup_events.append('posting.retire_legacy')
            return retire(*args, **kwargs)
        guard.enter_context(patch.object(main_module, 'retire_legacy_posting', observe_retirement))
        db = Database(path)
        manager = None
        try:
            bridge = OfflineDesktop(manifest['cases'])
            native = EmbeddedBrowser(db, directory, bridge=bridge)
            provider = BrowserHub(native, OfflineLegacy())
            settings = Settings(startup_token=manifest['nonce'], database_path=path, data_dir=directory,
                                bitbrowser_url='http://127.0.0.1:1')
            app = create_app(settings, database=db, bitbrowser=provider)
            service = app.state.service
            manager = app.state.studio
            native.closing.add(manifest['cases']['closing']['profile_id'])
            native.connections['opening-ticket'] = {'profile': manifest['cases']['opening']['profile_id']}
            opened = []
            def offline_open(provider, profile, platform, cookies):
                lease = next((row for row in rows(db, 'browser_operation_leases') if row['profile_id'] == profile), None)
                require(lease and lease['owner_user_id'] == manifest['owner'] and lease['operation_type'] == 'account',
                        'manual-open admission did not acquire its production account lease')
                require(platform == 'instagram' and cookies is None, 'unexpected external account or login data')
                opened.append(profile)
                return {'opened': True, 'synthetic': True, 'profile_id': profile}
            accounts = app.state.accounts
            accounts.opener = offline_open
            async def start(name, label):
                return await manager.command(manifest['owner'], {'action': 'start', 'kind': 'nurture',
                    'request_id': 'upgrade-admission-' + label + '-' + name,
                    'profile_ids': [manifest['cases'][name]['profile_id']],
                    'config': {'minutes': 1, 'scheduled_at': '2099-01-01T00:00:00+00:00'}})
            # The same normal task and manual-open entrypoints must refuse the old DB.
            admission = {}
            for name in RECOVERED:
                try:
                    await start(name, 'before')
                except ConflictError:
                    pass
                else:
                    raise RuntimeError('Historical orphan hold admitted a new task before recovery')
                try:
                    accounts.control_profile(manifest['owner'], manifest['cases'][name]['profile_id'], 'open')
                except ConflictError:
                    pass
                else:
                    raise RuntimeError('Historical orphan hold admitted an open before recovery')
                admission[name] = {'blocked_task_before': True, 'blocked_open_before': True}
            # A different application owner cannot reconcile this user's history.
            first = manifest['cases']['closed_legacy']['job_id']
            try:
                await manager.control(manifest['other'], first, 'retry_cleanup')
            except NotFoundError:
                pass
            else:
                raise RuntimeError('Foreign owner recovered another owner historical hold')
            require(not bridge.calls, 'owner/admission checks consulted the desktop before authorizing')
            archived = manifest['cases']['archived_closed']['job_id']
            require(sum(row['id'] == archived for row in manager.snapshot(manifest['owner'])['jobs']) == 1,
                    'archived held record is missing or duplicated before startup')
            queued_before = {}
            for name in ('queued_collection', 'queued_studio', 'queued_monitor', 'queued_action'):
                try:
                    await manager.control(manifest['owner'], manifest['cases'][name]['job_id'], 'retry_cleanup')
                except ConflictError as error:
                    require(error.details.get('reason') == 'unfinished_workflow', 'queued blocker failed for another reason')
                    queued_before[name] = error.details.get('module')
                else:
                    raise RuntimeError('Queued/prepared workflow did not block before startup: ' + name)
            async with app.router.lifespan_context(app):
                startup_sequence = list(startup_events)
                fresh_lease = manifest['runtime_account_lease']
                require(not any(row['profile_id'] == fresh_lease['profile_id'] for row in rows(db, 'browser_operation_leases')),
                        'fresh account successor was already present before normal startup completed')
                # Model a new live generation arriving between startup recovery
                # and the background cleanup task's first event-loop turn.
                with db.write() as c:
                    c.execute('INSERT INTO browser_operation_leases(' + ','.join(fresh_lease) + ') VALUES(' +
                              ','.join('?' for _ in fresh_lease) + ')', tuple(fresh_lease.values()))
                db.live_browser_lease_tokens.add(fresh_lease['lease_token'])
                action_before = manifest['protected_tables']['action_campaigns'][0]
                action_after = rows(db, 'action_campaigns')[0]
                require(action_before['status'] == 'queued' and action_after['status'] == 'paused'
                        and action_after['version'] == action_before['version'] + 1,
                        'normal startup did not pause interrupted queued action')
                # Unknown desktop evidence is last and must remain pending at normal
                # production backoff. No shortened recovery interval or patched clock.
                async with asyncio.timeout(8):
                    while not bridge.confirmations(manifest['cases']['unknown_state']['profile_id']):
                        await asyncio.sleep(.01)
                require(manager.cleanup_recovery_task is not None and not manager.cleanup_recovery_task.done(),
                        'unknown desktop state was silently treated as closed or terminal')
                await manager.shutdown()
                cases = {}
                current = {row['id']: row for row in rows(db, 'studio_jobs')}
                for name in RECOVERED + RETAINED:
                    ident = manifest['cases'][name]['job_id']
                    profile = manifest['cases'][name]['profile_id']
                    row = current[ident]
                    result = json.loads(row['result_json'])
                    require(history(row) == history(before[ident]), 'history changed during recovery: ' + name)
                    should_recover = name in RECOVERED
                    require(result.get('window_hold') is (not should_recover), 'incorrect startup hold outcome: ' + name)
                    call_count = bridge.confirmations(profile)
                    expected_calls = 1 if name in RECOVERED + ('open', 'unknown_state') else 0
                    require(call_count == expected_calls, 'unexpected desktop verification count: ' + name)
                    cleanup = result.get('window_cleanup', {})
                    if should_recover:
                        require(cleanup.get('state') == 'reconciled_closed' and cleanup.get('reconciled_at'),
                                'closed recovery has no committed receipt')
                        expected_error = ''
                    else:
                        # Retry traverses the user-facing production control route;
                        # every blocker must survive explicit cleanup as well as startup.
                        snapshot = dict(row)
                        try:
                            await manager.control(manifest['owner'], ident, 'retry_cleanup')
                        except (ConflictError, NotFoundError, UpstreamUnavailableError) as error:
                            expected_error = error.code
                        else:
                            raise RuntimeError('Explicit cleanup bypassed retained condition: ' + name)
                        saved = next(item for item in rows(db, 'studio_jobs') if item['id'] == ident)
                        require(saved == snapshot, 'rejected cleanup mutated historical row: ' + name)
                    cases[name] = {'verified': True, 'job_id': ident, 'profile_id': profile,
                        'hold_before': True, 'hold_after': not should_recover,
                        'startup_guard_calls': call_count, 'recovery_state': cleanup.get('state'),
                        'history_sha256': digest(history(row)), 'explicit_retry_error': expected_error,
                        'historical_lease_token': cleanup.get('lease_token', '')}
                require(not manager.active_ids() and not opened and attempted == {'network': 0, 'activity': 0},
                        'historical startup opened a browser or replayed an interaction')
                visible = {row['id'] for row in manager.snapshot(manifest['owner'])['jobs']}
                require(manifest['cases']['archived_closed']['job_id'] in visible,
                        'archived recovered hold disappeared from bounded history')
                for name in RECOVERED:
                    profile = manifest['cases'][name]['profile_id']
                    response = accounts.control_profile(manifest['owner'], profile, 'open')
                    require(response.get('opened') is True, 'normal manual-open route remains blocked after recovery')
                    created = await start(name, 'after')
                    require(len(created['job_ids']) == 1, 'new task admission was not singular')
                    admission[name].update(open_after=True, new_task_after=True, new_job_id=created['job_ids'][0],
                                           account_lease_released=True)
                    require(not any(row['profile_id'] == profile for row in rows(db, 'browser_operation_leases')),
                            'manual-open admission leaked its account lease')
                # Repeated startup cannot reconcile completed receipts again.
                confirmed_before = list(bridge.calls)
                again = StudioManager(service, provider)
                again.recover()
                recovered_ids = {manifest['cases'][name]['job_id'] for name in RECOVERED}
                require(not recovered_ids.intersection(ident for _, ident, _ in again.cleanup_recovery_ids),
                        'second startup scheduled already recovered history')
                require(bridge.calls == confirmed_before, 'second startup replayed closed verification')
                for table, expected in manifest['protected_tables'].items():
                    actual = rows(db, table)
                    if table == 'action_campaigns':
                        require(len(actual) == 1, 'normal startup changed action row count')
                        observed = dict(actual[0]); historical = dict(expected[0])
                        for field in ('status', 'version', 'updated_at', 'last_error'):
                            observed.pop(field); historical.pop(field)
                        require(observed == historical and actual[0] == action_after,
                                'normal startup changed unrelated action fields or cleanup changed the paused action')
                    elif table == 'browser_operation_leases':
                        require(actual == expected + [fresh_lease], 'changed a persistent or fresh successor lease')
                    elif table == 'posting_jobs':
                        require(actual == [row for row in expected if row['id'] == 'unrelated-job'],
                                'changed quarantined posting ownership or retained idle association')
                    else:
                        require(actual == expected, 'changed dedupe, login, blocker or unrelated lock table: ' + table)
                with db.read() as check:
                    if check.execute("SELECT 1 FROM sqlite_master WHERE name='posting_withdraw_history'").fetchone():
                        require(not rows(db, 'posting_withdraw_history'), 'startup manufactured a posting withdrawal')
                require(attempted == {'network': 0, 'activity': 0}, 'forbidden external effect was attempted')
                runtime = {'pid': os.getpid(), 'frozen': bool(getattr(sys, 'frozen', False)),
                    'windows': os.name == 'nt', 'executable': str(Path(sys.executable).resolve()),
                    'executable_sha256': file_sha256(sys.executable),
                    'module_file': str(Path(__file__).resolve()),
                    'bundle_root': str(Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve())}
                proof = {'verified': True, 'contract': CONTRACT, 'baseline_commit': BASELINE_COMMIT,
                    'legacy_schema_sha256': manifest['schema_sha256'],
                    'baseline_database_code_sha256': manifest['baseline_database_sha256'],
                    'baseline_posting_code_sha256': manifest['baseline_posting_schema_sha256'],
                    'synthetic': True, 'network_disabled': True, 'live_accounts_tested': False,
                    'user_data_touched': False, 'production_manager_and_service': True,
                    'production_closed_profile_guard': True, 'synthetic_desktop_rpc': True,
                    'persisted_before_process': True, 'owner_isolation': True,
                    'archived_history_visible': True, 'archived_hold_visible_before_startup': True,
                    'normal_startup_and_shutdown': True, 'startup_sequence': startup_sequence,
                    'fresh_account_successor_after_startup': True,
                    'queued_blockers_before_startup': queued_before,
                    'startup_action_transition': {'before': 'queued', 'after': 'paused', 'version_before': action_before['version'],
                        'version_after': action_after['version'], 'after_sha256': digest(action_after)},
                    'second_startup_idempotent': True,
                    'no_historical_activity_replay': True, 'network_attempts': attempted['network'],
                    'activity_attempts': attempted['activity'], 'synthetic_open_admissions': len(opened),
                    'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                    'input_database_sha256': manifest['database_sha256'], 'nonce': manifest['nonce'],
                    'seed_pid': manifest['seed_pid'], 'seed_completed_ns': manifest['seed_completed_ns'],
                    'chronology_clock': CHRONOLOGY_CLOCK, 'seed_completed_perf_ns': manifest['seed_completed_perf_ns'],
                    'opened_perf_ns': opened_perf_ns,
                    'opened_ns': opened_ns, 'runtime': runtime, 'cases': cases, 'admission': admission}
            require(attempted == {'network': 0, 'activity': 0}, 'shutdown attempted an external effect')
            require(db._instance_lock_file is None and manager.scheduler.done() and not hasattr(app.state, 'posting'),
                    'normal lifespan shutdown did not release its process lock and stop schedulers')
            return proof
        finally:
            if manager is not None:
                await manager.shutdown()


def main(manifest_path):
    print(PROOF_PREFIX + json.dumps(asyncio.run(run_selftest(manifest_path)), sort_keys=True), flush=True)
