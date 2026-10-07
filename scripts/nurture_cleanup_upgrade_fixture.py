"""Stdlib-only R6.2 fixture, persisted by the verifier BEFORE installed Core starts.

The checked-in schema is an export of Database.initialize() plus
initialize_posting_schema() from delivered commit 252590e, not candidate imports.
Only invented users, login-file bytes, accounts, and work records are used.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

BASELINE_COMMIT = '252590e257fbbc0e1974ad727e2086ab858bb2c6'
BASELINE_DATABASE_SHA256 = '4e24d55bb7a4cc0fee25045776bd42edda57b83147d7b6752c011a8812549377'
BASELINE_POSTING_SCHEMA_SHA256 = 'c56f627fc95d8f7e8539e164400418933394908d10f815c8c63ec0e7c4399a87'
SCHEMA_SHA256 = '105cc4955ae06ea2f59a94db1f6586c6826b8007dc2d5bba851abf7afac93775'
CONTRACT = 'nurture-orphan-hold-upgrade-v1'
CHRONOLOGY_CLOCK = 'perf_counter_ns-system-v1'
RECOVERED = ('closed_legacy', 'closed_lost', 'archived_closed', 'prepared_posting')
RETAINED = ('open', 'opening', 'closing', 'successor_lease', 'fresh_account_lease', 'queued_collection', 'queued_studio',
            'queued_monitor', 'queued_action', 'unknown_action',
            'inflight', 'unknown_profile', 'foreign_profile_owner', 'unknown_state')
CASES = RECOVERED + RETAINED
PROTECTED_TABLES = ('app_users', 'auth_sessions', 'native_browser_profiles', 'account_window_plans',
    'instagram_accounts', 'instagram_username_aliases', 'global_seen', 'global_identity_owners',
    'tasks', 'task_windows', 'task_targets', 'action_campaigns', 'follow_monitor_runs',
    'posting_jobs', 'browser_operation_leases')
OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
HISTORY_TIME = '2026-10-02T12:34:56+00:00'
FUTURE_TIME = '2099-01-01T00:00:00+00:00'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(connection, table):
    return [dict(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


def history(row):
    value = dict(row)
    value.pop('message')
    result = json.loads(value.pop('result_json'))
    result.pop('window_hold', None)
    result.pop('window_cleanup', None)
    value['result'] = result
    return value


def files(directory):
    root = Path(directory) / 'browser-profiles'
    return {str(path.relative_to(directory)).replace('\\', '/'): file_sha256(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


def insert(connection, table, **values):
    connection.execute('INSERT INTO ' + table + '(' + ','.join(values) + ') VALUES('
                       + ','.join('?' for _ in values) + ')', tuple(values.values()))


def seed(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / 'legacy-r62.sqlite3'
    manifest_path = directory / 'upgrade-input.json'
    if database.exists() or manifest_path.exists():
        raise RuntimeError('Upgrade fixture refuses to overwrite an existing database')
    schema = Path(__file__).with_name('fixtures') / 'nurture_cleanup_upgrade_r62.sql'
    if file_sha256(schema) != SCHEMA_SHA256:
        raise RuntimeError('Delivered R6.2 schema fixture hash does not match')
    with closing(sqlite3.connect(database)) as c:
        c.row_factory = sqlite3.Row
        c.executescript(schema.read_text(encoding='utf-8'))
        c.execute('PRAGMA foreign_keys=ON')
        for ident, label in ((OWNER, 'offline-upgrade-owner'), (OTHER, 'offline-upgrade-other')):
            insert(c, 'app_users', id=ident, username_norm=label, username_display=label,
                   password_hash='synthetic-not-an-authenticatable-password', created_at=HISTORY_TIME)
        insert(c, 'auth_sessions', id='synthetic-login-session', user_id=OWNER,
               token_hash='synthetic-not-a-login-token', remember_login=1, auto_login=1,
               created_at=HISTORY_TIME, expires_at=FUTURE_TIME)
        case_inputs = {}
        for index, name in enumerate(CASES, 1):
            profile = 'native:' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'offline-r62-upgrade/' + name))
            ident = 'historical-' + name
            result = {'window_hold': True, 'counts': {'browse': 7, 'like': 3, 'browse_seconds': 60},
                      'confirmed_at': HISTORY_TIME, 'nurture_finished_at': HISTORY_TIME,
                      'nurture_outcome': 'completed', 'nurture_actual_seconds': 60,
                      'account_snapshot': {'username': 'offline_' + name, 'instagram_user_id': str(800000 + index),
                                           'followers': 123, 'following': 45, 'posts': 6},
                      'nurture_decisions': {'/reel/synthetic': {'selected': True}},
                      'nurture_actions': {'like:synthetic': {'state': 'confirmed', 'action': 'like'}}}
            if name != 'closed_legacy':
                result['window_cleanup'] = {'state': 'lease_lost', 'lease_token': 'historical-token-' + name,
                                            'last_attempt_at': HISTORY_TIME, 'attempts': 2}
            if name == 'unknown_action':
                result['nurture_actions']['like:synthetic']['state'] = 'unknown'
            insert(c, 'studio_jobs', id=ident, owner_user_id=OWNER, request_key=ident, kind='nurture',
                   profile_id=profile, status='completed', config_json='{"historical_fixture":true}',
                   cursor=7, total_steps=7, inflight=int(name == 'inflight'),
                   result_json=json.dumps(result), message='Historical completed round', due_at=HISTORY_TIME,
                   created_at=HISTORY_TIME, updated_at=HISTORY_TIME,
                   deleted_at='2026-10-03T01:02:03+00:00' if name == 'archived_closed' else None)
            if name != 'unknown_profile':
                profile_owner = OTHER if name == 'foreign_profile_owner' else OWNER
                insert(c, 'native_browser_profiles', id=profile, owner_user_id=profile_owner, serial=index,
                       name='Synthetic ' + name, created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
                folder = directory / 'browser-profiles' / profile_owner / profile.removeprefix('native:')
                for relative in ('Local State', 'Default/Cookies', 'Default/Login Data', 'Default/Preferences'):
                    target = folder / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(('OFFLINE-NO-CREDENTIALS\0' + name + '\0' + relative).encode())
            case_inputs[name] = {'job_id': ident, 'profile_id': profile}
        p = lambda name: case_inputs[name]['profile_id']
        for name, profile in (('collection-blocker', p('queued_collection')),):
            insert(c, 'tasks', id=name, owner_user_id=OWNER, name=name, status='queued',
                   modes_json='["followers"]', settings_json='{}', created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
            insert(c, 'task_windows', task_id=name, profile_id=profile, queue_order=0)
            insert(c, 'task_targets', id='collection-target', task_id=name, username_norm='offline_source',
                   username_display='offline_source', queue_order=0, status='pending',
                   current_window_id=profile, created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
        insert(c, 'studio_jobs', id='studio-blocker', owner_user_id=OWNER, request_key='studio-blocker',
               kind='nurture', profile_id=p('queued_studio'), status='queued', config_json='{}',
               due_at=FUTURE_TIME, created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
        insert(c, 'follow_monitor_runs', id='monitor-blocker', owner_user_id=OWNER,
               profile_ids_json=json.dumps([p('queued_monitor')]), status='queued', started_at=HISTORY_TIME)
        insert(c, 'action_campaigns', id='action-blocker', owner_user_id=OWNER, operation='follow',
               execution_type='campaign', profile_id=p('queued_action'), interval_min_seconds=10,
               interval_max_seconds=20, limit_count=5, status='queued', created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
        insert(c, 'posting_jobs', id='posting-blocker', owner_user_id=OWNER, request_key='posting-blocker',
               theme='offline', caption='synthetic prepared draft', profile_id=p('prepared_posting'),
               status='prepared', created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
        for profile, owner, operation, entity, token in (
            (p('successor_lease'), OTHER, 'collection', 'successor-job', 'successor-generation'),
            ('unrelated-active-window', OTHER, 'posting', 'unrelated-job', 'unrelated-active-generation')):
            insert(c, 'browser_operation_leases', profile_id=profile, owner_user_id=owner,
                   operation_type=operation, entity_id=entity, lease_token=token,
                   acquired_at=HISTORY_TIME, heartbeat_at=HISTORY_TIME, expires_at='2000-01-01T00:00:00+00:00')
        insert(c, 'posting_jobs', id='unrelated-job', owner_user_id=OTHER, request_key='unrelated-job',
               theme='offline', caption='synthetic held posting', profile_id='unrelated-active-window',
               status='needs_review', lease_token='unrelated-active-generation',
               created_at=HISTORY_TIME, updated_at=HISTORY_TIME)
        # Permanent dedupe and recognizable account history, not only empty tables.
        for index in range(3):
            ident = 'synthetic-account-' + str(index)
            name = 'offline_collected_' + str(index)
            insert(c, 'instagram_accounts', id=ident, instagram_user_id=str(900000 + index),
                   current_username_norm=name, current_username_display=name,
                   first_seen_at=HISTORY_TIME, last_seen_at=HISTORY_TIME)
            insert(c, 'instagram_username_aliases', account_id=ident, username_norm=name,
                   first_seen_at=HISTORY_TIME, last_seen_at=HISTORY_TIME)
            insert(c, 'global_seen', account_id=ident, sources_json='["historical-collection"]',
                   first_seen_at=HISTORY_TIME, last_seen_at=HISTORY_TIME)
            insert(c, 'global_identity_owners', account_id=ident, owner_user_id=OWNER,
                   first_seen_at=HISTORY_TIME, last_seen_at=HISTORY_TIME)
        c.commit()
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Historical upgrade fixture is not an intact database')
        before_jobs = {row['id']: row for row in rows(c, 'studio_jobs')}
        protected = {table: rows(c, table) for table in PROTECTED_TABLES}
    spec = importlib.util.spec_from_file_location('upgrade_seed_archive_oracle', Path(__file__).with_name('retired_posting_archive_oracle.py'))
    oracle = importlib.util.module_from_spec(spec); spec.loader.exec_module(oracle)
    schema_before = oracle.capture_schema(database, ('posting_jobs',))
    manifest = {'contract': CONTRACT, 'baseline_commit': BASELINE_COMMIT,
        'baseline_database_sha256': BASELINE_DATABASE_SHA256,
        'baseline_posting_schema_sha256': BASELINE_POSTING_SCHEMA_SHA256, 'schema_sha256': SCHEMA_SHA256,
        'nonce': secrets.token_hex(32), 'seed_pid': os.getpid(), 'seed_completed_ns': time.time_ns(),
        # Python >=3.10 uses a system-wide performance counter on Windows.
        # Wall time is diagnostic only: it can repeat or move backwards.
        'chronology_clock': CHRONOLOGY_CLOCK, 'seed_completed_perf_ns': time.perf_counter_ns(),
        'database_sha256': file_sha256(database), 'database_name': database.name,
        'owner': OWNER, 'other': OTHER, 'cases': case_inputs,
        'runtime_account_lease': {'profile_id': p('fresh_account_lease'), 'owner_user_id': OWNER,
            'operation_type': 'account', 'entity_id': 'fresh-account-operation',
            'lease_token': 'fresh-account-successor-generation', 'acquired_at': HISTORY_TIME,
            'heartbeat_at': HISTORY_TIME, 'expires_at': FUTURE_TIME},
        'before_jobs': before_jobs, 'protected_tables': protected, 'login_files': files(directory),
        'archive_schema_before': schema_before}
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding='utf-8')
    return manifest_path, manifest


def inspect_after(directory, manifest, proof):
    """Independent post-exit oracle; no app imports and no trust in PASS booleans."""
    directory = Path(directory)
    if files(directory) != manifest['login_files']:
        raise RuntimeError('Installed upgrade changed or removed persisted login/profile files')
    with closing(sqlite3.connect(directory / manifest['database_name'])) as c:
        c.row_factory = sqlite3.Row
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Installed upgrade damaged the persisted database')
        for table, before in manifest['protected_tables'].items():
            actual = rows(c, table)
            if table == 'action_campaigns':
                if len(actual) != 1:
                    raise RuntimeError('Normal startup changed queued action cardinality')
                original, observed = dict(before[0]), dict(actual[0])
                transition = proof['startup_action_transition']
                if (observed['status'] != 'paused' or observed['version'] != original['version'] + 1
                        or observed['updated_at'] == original['updated_at']
                        or observed['last_error'] != 'Application restarted; task is paused and requires explicit resume'
                        or digest(observed) != transition['after_sha256']):
                    raise RuntimeError('Normal startup action transition was not the expected safe pause')
                for key in ('status', 'version', 'updated_at', 'last_error'):
                    original.pop(key); observed.pop(key)
                if original != observed:
                    raise RuntimeError('Normal startup changed unrelated action fields')
            elif table == 'browser_operation_leases':
                if actual != before + [manifest['runtime_account_lease']]:
                    raise RuntimeError('Installed upgrade removed or replaced a successor or unrelated active lease')
            elif table == 'posting_jobs':
                expected_active = [row for row in before if row['id'] == 'unrelated-job']
                if actual != expected_active:
                    raise RuntimeError('Installed upgrade changed quarantined posting ownership or retained an idle association')
                import importlib.util
                spec = importlib.util.spec_from_file_location('upgrade_archive_oracle', Path(__file__).with_name('retired_posting_archive_oracle.py'))
                oracle = importlib.util.module_from_spec(spec); spec.loader.exec_module(oracle)
                oracle.inspect_archive(directory / manifest['database_name'], {'posting_jobs': before}, expected_schema=manifest['archive_schema_before'], retained_rows={('posting_jobs', 'unrelated-job')})
            elif actual != before:
                raise RuntimeError('Installed upgrade changed protected table ' + table)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='posting_withdraw_history'").fetchone() and rows(c, 'posting_withdraw_history'):
            raise RuntimeError('Installed upgrade manufactured a posting withdrawal')
        all_jobs = {row['id']: row for row in rows(c, 'studio_jobs')}
        for ident, before in manifest['before_jobs'].items():
            after = all_jobs.get(ident)
            if after is None or history(after) != history(before):
                raise RuntimeError('Installed upgrade changed historical counts, timestamps or identity: ' + ident)
        for name in CASES:
            ident = manifest['cases'][name]['job_id']
            saved = json.loads(all_jobs[ident]['result_json'])
            case = proof['cases'][name]
            if case['history_sha256'] != digest(history(all_jobs[ident])):
                raise RuntimeError('Installed receipt disagrees with independent persisted history: ' + name)
            if name in RECOVERED:
                cleanup = saved.get('window_cleanup', {})
                evidence = cleanup.get('reconciliation_evidence', {})
                if (saved.get('window_hold') is not False or cleanup.get('state') != 'reconciled_closed'
                        or not cleanup.get('reconciled_at') or evidence.get('closed') is not True
                        or evidence.get('profile_id') != manifest['cases'][name]['profile_id']
                        or evidence.get('owner_user_id') != OWNER or evidence.get('verification') != 'desktop-absence-v1'):
                    raise RuntimeError('Installed upgrade failed to persist affirmative closure: ' + name)
                old = json.loads(manifest['before_jobs'][ident]['result_json']).get('window_cleanup', {})
                if any(cleanup.get(key) != value for key, value in old.items() if key != 'state'):
                    raise RuntimeError('Installed upgrade replaced historical cleanup token or timestamps: ' + name)
                if cleanup.get('reconciled_from_state') != 'lease_lost':
                    raise RuntimeError('Installed upgrade lost the prior cleanup state: ' + name)
            elif saved.get('window_hold') is not True or saved.get('window_cleanup', {}).get('state') == 'reconciled_closed':
                raise RuntimeError('Installed upgrade released a blocked historical hold: ' + name)
        new_ids = set(all_jobs) - set(manifest['before_jobs'])
        recorded = {case['new_job_id'] for case in proof['admission'].values()}
        if len(new_ids) != len(RECOVERED) or new_ids != recorded:
            raise RuntimeError('Installed upgrade created unexpected jobs or replayed historical work')
        for name in RECOVERED:
            new = all_jobs[proof['admission'][name]['new_job_id']]
            if (new['profile_id'] != manifest['cases'][name]['profile_id'] or new['owner_user_id'] != OWNER
                    or new['kind'] != 'nurture' or new['status'] != 'queued'
                    or new['cursor'] != 0 or new['inflight'] != 0 or new['result_json'] != '{}'):
                raise RuntimeError('Installed post-recovery admission is not a fresh queued task')
    return {'verified': True, 'historical_jobs': len(manifest['before_jobs']),
            'protected_tables': len(PROTECTED_TABLES), 'login_files': len(manifest['login_files']),
            'recovered_holds': len(RECOVERED), 'retained_holds': len(RETAINED),
            'new_jobs': len(RECOVERED), 'global_dedupe_identities': 3, 'untouched_leases': 3, 'expected_startup_action_pauses': 1}
