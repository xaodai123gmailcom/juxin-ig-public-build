"""Independent, stdlib-only legacy seed and persistence oracle for R6.4 APIs.

No candidate database/schema code is imported. All identities, material and
session bytes are invented. An intentionally invalid render digest is a hard
production pre-browser safety fence after the explicit fresh reviewed start.
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

_spec = importlib.util.spec_from_file_location('r64_legacy_fixture', Path(__file__).with_name('nurture_cleanup_upgrade_fixture.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)
CONTRACT = 'installed-user-recovery-api-r64-v1'
OWNER, OTHER, AT = legacy.OWNER, legacy.OTHER, legacy.HISTORY_TIME
POST = 'offline-r64-queued-post'
TASK = '86feb8fc-f2a6-404f-9459-11dd3f884103'
TARGET = 'offline-r64-completed-target'
ASSET = 'offline-r64-preserved-material'
USERNAME = 'offline.verified'
CAPTION = 'Original reviewed caption\n保留原文案与素材 🌲'
PROFILES = {name: 'native:' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'offline-r64/' + name)) for name in ('posting', 'collection')}
JOBS = {name: 'offline-r64-held-' + name for name in PROFILES}
# Nonempty history, dedup and material registries are compared byte-for-byte.
PROTECTED = ('app_users', 'auth_sessions', 'native_browser_profiles', 'task_windows',
    'task_targets', 'task_checkpoints', 'task_results', 'task_list_dismissals',
    'task_target_list_dismissals', 'task_mode_candidates',
    'instagram_accounts', 'instagram_username_aliases', 'global_seen',
    'global_identity_owners', 'posting_assets', 'posting_receipts',
    'posting_retry_history', 'posting_account_snapshots')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_state(directory):
    with closing(sqlite3.connect(Path(directory) / 'collector.sqlite3')) as c:
        c.row_factory = sqlite3.Row
        require(c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Fixture database integrity failed')
        require(not c.execute('PRAGMA foreign_key_check').fetchall(), 'Fixture foreign keys changed')
        tables = PROTECTED + ('studio_jobs', 'tasks', 'posting_jobs', 'browser_operation_leases')
        state = {table: legacy.rows(c, table) for table in tables}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='posting_withdraw_history'").fetchone():
            state['posting_withdraw_history'] = legacy.rows(c, 'posting_withdraw_history')
        return state


def seed(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / 'collector.sqlite3'
    require(not database.exists(), 'R6.4 fixture refuses to overwrite any existing database')
    schema = Path(__file__).with_name('fixtures') / 'nurture_cleanup_upgrade_r62.sql'
    require(legacy.file_sha256(schema) == legacy.SCHEMA_SHA256, 'Legacy schema hash changed')
    tokens = {owner: secrets.token_hex(32) for owner in (OWNER, OTHER)}
    material = directory / 'synthetic-material.bin'
    material.write_bytes(b'OFFLINE-R64-NOT-A-PUBLISHABLE-IMAGE\x00preserve original bytes')
    with closing(sqlite3.connect(database)) as c:
        c.executescript(schema.read_text(encoding='utf-8'))
        c.execute('PRAGMA foreign_keys=ON')
        add = lambda table, **values: legacy.insert(c, table, **values)
        for owner in (OWNER, OTHER):
            add('app_users', id=owner, username_norm='offline-r64-' + owner[:8], username_display='Offline fixture', password_hash='not-an-authenticatable-password', created_at=AT)
            add('auth_sessions', id='offline-session-' + owner, user_id=owner, token_hash=hashlib.sha256(tokens[owner].encode()).hexdigest(), remember_login=1, auto_login=1, created_at=AT, expires_at=legacy.FUTURE_TIME)
        for serial, (name, profile) in enumerate(PROFILES.items(), 1):
            add('native_browser_profiles', id=profile, owner_user_id=OWNER, serial=serial, name='OFFLINE ' + name, created_at=AT, updated_at=AT)
            result = {'window_hold': True, 'counts': {'browse': 7, 'like': 3}, 'confirmed_at': AT,
                'nurture_finished_at': AT, 'nurture_outcome': 'completed',
                'window_cleanup': {'state': 'lease_lost', 'lease_token': 'historical-token-' + name, 'last_attempt_at': AT, 'attempts': 2}}
            add('studio_jobs', id=JOBS[name], owner_user_id=OWNER, request_key=JOBS[name], kind='nurture', profile_id=profile,
                status='completed', config_json='{}', result_json=json.dumps(result), cursor=7, total_steps=7,
                message='执行已完成；窗口占用凭证已失效，保留清理记录等待核验', due_at=AT, created_at=AT, updated_at=AT)
            folder = directory / 'browser-profiles' / OWNER / profile.removeprefix('native:')
            for relative in ('Local State', 'Default/Cookies', 'Default/Login Data', 'Default/Preferences'):
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(('OFFLINE-NO-CREDENTIALS\0' + name + '\0' + relative).encode())
        add('posting_jobs', id=POST, owner_user_id=OWNER, request_key=POST, theme='forest', caption=CAPTION,
            profile_id=PROFILES['posting'], expected_username=USERNAME, expected_actor_id='historical-actor', asset_id=ASSET,
            status='queued', message='等待窗口释放', created_at=AT, updated_at=AT)
        add('posting_assets', id=ASSET, provider_id='offline-material', job_id=POST,
            sha256=legacy.file_sha256(material), render_sha256='0' * 64, path=str(material), state='ready',
            source_url='https://example.invalid/offline', photographer='Synthetic', download_url='https://example.invalid/offline', created_at=AT)
        add('posting_account_snapshots', owner_user_id=OWNER, profile_id=PROFILES['posting'], username=USERNAME, posts_count=7, status='ok', checked_at=AT)
        add('posting_jobs', id='offline-r64-historical-post', owner_user_id=OWNER, request_key='offline-r64-historical-post', theme='historical', caption='Preserved successful history', status='completed', created_at=AT, updated_at=AT)
        add('posting_receipts', job_id='offline-r64-historical-post', owner_user_id=OWNER, profile_id=PROFILES['posting'], username=USERNAME, confirmed_at=AT, day_utc='2026-10-02', evidence_json='{"verification":"historical-synthetic"}')
        add('tasks', id=TASK, owner_user_id=OWNER, name='Hidden paused historical collection', status='paused', version=7,
            modes_json='["followers"]', settings_json='{}', created_at=AT, updated_at=AT)
        add('task_windows', task_id=TASK, profile_id=PROFILES['collection'], queue_order=0)
        add('task_targets', id=TARGET, task_id=TASK, username_norm='offline_source', username_display='offline_source', queue_order=0,
            status='completed', current_window_id=PROFILES['collection'], current_stage='completed_archived', last_success_at=AT, created_at=AT, updated_at=AT)
        add('task_checkpoints', id='offline-checkpoint', task_id=TASK, target_id=TARGET, mode='followers', stage='completed', cursor_json='{"position":17}', counters_json='{"saved":1}', updated_at=AT)
        add('task_list_dismissals', task_id=TASK, owner_user_id=OWNER, dismissed_at=AT)
        add('task_target_list_dismissals', task_id=TASK, target_id=TARGET, owner_user_id=OWNER, dismissed_at=AT)
        add('instagram_accounts', id='offline-account', instagram_user_id='900001', current_username_norm='offline_result', current_username_display='offline_result', first_seen_at=AT, last_seen_at=AT)
        add('instagram_username_aliases', account_id='offline-account', username_norm='offline_result', first_seen_at=AT, last_seen_at=AT)
        add('global_seen', account_id='offline-account', sources_json='["historical-collection"]', first_seen_at=AT, last_seen_at=AT)
        add('global_identity_owners', account_id='offline-account', owner_user_id=OWNER, first_seen_at=AT, last_seen_at=AT)
        add('task_results', id='offline-result', task_id=TASK, target_id=TARGET, account_id='offline-account', sources_json='["followers"]', visibility='public', profile_json='{"followers_count":4}', screening_json='{}', qualified=0, created_at=AT, updated_at=AT)
        c.commit()
    manifest = {'contract': CONTRACT, 'baseline_commit': legacy.BASELINE_COMMIT, 'legacy_schema_sha256': legacy.SCHEMA_SHA256,
        'database_sha256': legacy.file_sha256(database), 'nonce': secrets.token_hex(32), 'tokens': tokens,
        'seed_pid': os.getpid(), 'seed_completed_perf_ns': time.perf_counter_ns(),
        'chronology_clock': legacy.CHRONOLOGY_CLOCK, 'before': read_state(directory), 'login_files': legacy.files(directory),
        'material_sha256': legacy.file_sha256(material)}
    path = directory / 'recovery-input.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    return path, manifest


def inspect(directory, manifest, *, phase):
    """Reject unrequested changes even if every reported PASS flag is forged."""
    state = read_state(directory)
    before = manifest['before']
    require(phase in ('startup', 'withdrawn', 'cleaned', 'stopped', 'complete'), 'Unknown oracle phase')
    require(legacy.files(directory) == manifest['login_files'], 'Persisted synthetic login/profile files changed')
    require(legacy.file_sha256(Path(directory) / 'synthetic-material.bin') == manifest['material_sha256'], 'Original material bytes changed')
    for table in PROTECTED:
        require(state[table] == before[table], 'Protected material/history/dedup table changed: ' + table)
    jobs = {row['id']: row for row in state['posting_jobs']}
    require(set(jobs) == {row['id'] for row in before['posting_jobs']}, 'Posting job cardinality changed')
    for original in before['posting_jobs']:
        current = dict(jobs[original['id']])
        revision = current.pop('queue_revision', None)
        require(type(revision) is int, 'Queue revision is not a strict integer')
        if original['id'] != POST or phase == 'startup':
            require(revision == 0 and current == original, 'Startup changed existing posting fields or nonzero queue revision')
            continue
        require(revision == (2 if phase == 'complete' else 1), 'Withdrawal/start did not fence the expected generation')
        expected = dict(original)
        mutable = ('status', 'profile_id', 'expected_username', 'expected_actor_id', 'message', 'updated_at', 'failure_stage', 'failure_code')
        for field in mutable:
            current.pop(field); expected.pop(field)
        require(current == expected, 'Withdrawal/start changed protected posting or submission fields')
        post = jobs[POST]
        if phase == 'complete':
            require(post['status'] == 'failed' and post['failure_stage'] == 'pre_submit' and post['failure_code'] == 'material_unavailable', 'Synthetic render guard did not stop the real executor before browser/Share')
            require(post['profile_id'] == PROFILES['posting'] and post['expected_username'] == USERNAME and post['expected_actor_id'] == '', 'Fresh assignment is incorrect')
        else:
            require(post['status'] == 'ready' and all(post[key] == '' for key in ('profile_id', 'expected_username', 'expected_actor_id')), 'Withdrawal did not clear only the queue assignment')
    audit = state.get('posting_withdraw_history')
    if phase == 'startup':
        require(audit == [], 'Startup manufactured a withdrawal audit')
    else:
        require(isinstance(audit, list) and len(audit) == 1, 'Withdrawal audit is not singular')
        entry = audit[0]
        require(entry['job_id'] == POST and entry['owner_user_id'] == OWNER and entry['profile_id'] == PROFILES['posting'] and entry['expected_username'] == USERNAME and entry['expected_actor_id'] == 'historical-actor' and bool(entry['id']) and bool(entry['withdrawn_at']), 'Withdrawal audit identity changed')
    require(not state['browser_operation_leases'], 'Unexpected operation lease remains')
    require(len(state['studio_jobs']) == len(before['studio_jobs']), 'Historical nurture rows were removed or replayed')
    for original in before['studio_jobs']:
        current = next(row for row in state['studio_jobs'] if row['id'] == original['id'])
        name = next(name for name, ident in JOBS.items() if ident == original['id'])
        recovered = phase in ('cleaned', 'stopped', 'complete') and name == 'posting' or phase == 'complete' and name == 'collection'
        if not recovered:
            require(current == original, 'Held historical nurture changed before safe cleanup')
        else:
            require(legacy.history(current) == legacy.history(original), 'Historical counts/timestamps/identity changed')
            result = json.loads(current['result_json']); receipt = result.get('window_cleanup', {})
            old = json.loads(original['result_json'])['window_cleanup']
            require(result.get('window_hold') is False and receipt.get('state') == 'reconciled_closed' and receipt.get('reconciled_from_state') == 'lease_lost' and bool(receipt.get('reconciled_at')), 'Cleanup lacks a persisted affirmative receipt')
            require(all(receipt.get(key) == value for key, value in old.items() if key != 'state'), 'Historical cleanup identity/timing changed')
            require(receipt.get('reconciliation_evidence') == {'closed': True, 'profile_id': PROFILES[name], 'owner_user_id': OWNER, 'verification': 'desktop-absence-v1'}, 'Cleanup closure evidence is not exact')
    original, current = dict(before['tasks'][0]), dict(state['tasks'][0])
    require(len(state['tasks']) == 1, 'Hidden task cardinality changed')
    if phase in ('stopped', 'complete'):
        require(current['status'] == 'stopped' and current['version'] == original['version'] + 1 and current['updated_at'] != original['updated_at'] and current['finished_at'] == current['updated_at'] and current['last_error'] is None, 'Hidden task was not stopped by its versioned lifecycle')
        for key in ('status', 'version', 'updated_at', 'finished_at', 'last_error'):
            original.pop(key); current.pop(key)
    require(current == original, 'Hidden task changed outside its normal stop fields')
    return {'verified': True, 'protected_tables': len(PROTECTED), 'login_files': len(manifest['login_files']),
        'material_sha256': manifest['material_sha256'], 'protected_sha256': legacy.digest({table: state[table] for table in PROTECTED}),
        'withdrawal_audits': len(audit), 'historical_receipts': len(state['posting_receipts']),
        'posting_queue_revision': jobs[POST]['queue_revision'], 'no_submission': jobs[POST]['attempt_id'] == '' and jobs[POST]['submitted_at'] is None}
