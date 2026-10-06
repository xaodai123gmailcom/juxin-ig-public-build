"""Offline synthetic data for repeatable snapshot load tests; never use a real DB.

The caller must create an isolated, initialized database and pass its fixture
owner id. This module has only standard-library dependencies, so packaged Core
smoke tests can populate the same fixture without importing the app process.
"""
from __future__ import annotations

import json
import sqlite3
import time

NOW = '2026-10-01T00:00:00+00:00'


def seed_snapshot_scale(database_path, owner_id: str, task_id: str | None = None,
                        target_id: str | None = None, *, result_count: int = 441_552,
                        identity_count: int = 602_831, split_count: int = 1_272,
                        profile_bytes: int = 1_000) -> dict:
    """Seed an isolated DB; optionally reuse the caller's synthetic task/target."""
    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        return _seed_snapshot_scale(connection, owner_id, task_id, target_id,
            result_count=result_count, dedupe_count=identity_count,
            split_count=split_count, profile_bytes=profile_bytes)
    finally:
        connection.close()


def _seed_snapshot_scale(connection: sqlite3.Connection, owner_id: str,
                         task_id: str | None, target_id: str | None, *,
                        result_count: int = 441_552, dedupe_count: int = 602_831,
                        split_count: int = 1_272, profile_bytes: int = 1_000) -> dict:
    """Populate an EMPTY synthetic ledger and return exact expected counts.

    Does not erase, update, or reuse existing collection records. Refuses a
    populated ledger or a caller's open transaction. Keep the returned IDs for
    synthetic writer probes; all names and profiles are fabricated.
    """
    if connection.in_transaction:
        raise ValueError('Seed requires its own transaction')
    if (result_count < 1 or dedupe_count < result_count or split_count < 0
            or profile_bytes < 0):
        raise ValueError('Invalid synthetic fixture sizes')
    if not connection.execute('SELECT 1 FROM app_users WHERE id=?', (owner_id,)).fetchone():
        raise ValueError('Fixture owner does not exist')
    for table in ('task_results', 'global_seen', 'workbench_candidates',
                  'workbench_collection_exclusions', 'split_candidate_history'):
        if connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
            raise ValueError(f'Refusing nonempty {table}; use an isolated synthetic DB')
    if bool(task_id) != bool(target_id):
        raise ValueError('Supply both synthetic task and target IDs, or neither')
    if task_id and not connection.execute('''SELECT 1 FROM task_targets target
            JOIN tasks task ON task.id=target.task_id
            WHERE target.id=? AND task.id=? AND task.owner_user_id=?''',
            (target_id, task_id, owner_id)).fetchone():
        raise ValueError('Synthetic task/target is not owned by the fixture owner')
    task, target = task_id or 'snapshot-scale-task', target_id or 'snapshot-scale-target'
    public = min(result_count, round(result_count * 261_204 / 441_552))
    private = min(result_count-public, round(result_count * 172_319 / 441_552))
    classified = public + private
    profile = json.dumps({'followers_count': 1200, 'following_count': 600,
                          'posts_count': 40, 'bio': 'x' * profile_bytes,
                          'page_read_status': 'hover_preview'})
    def account(i):
        return f'scale-a{i:07}'
    def username(i):
        return f'scale_u{i:07}'
    started = time.perf_counter()
    connection.execute('BEGIN IMMEDIATE')
    try:
        if not task_id:
            connection.execute('''INSERT INTO tasks
                (id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at)
                VALUES(?,?,?,'running','["followers"]','{}',?,?)''',
                (task, owner_id, 'Synthetic snapshot load', NOW, NOW))
            connection.execute('''INSERT INTO task_targets
                (id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at)
                VALUES(?,?,?, ?,0,'running',?,?)''',
                (target, task, 'synthetic_source', 'synthetic_source', NOW, NOW))
        connection.executemany('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)',
            ((account(i), username(i), username(i), NOW, NOW) for i in range(dedupe_count)))
        connection.executemany('INSERT INTO instagram_username_aliases VALUES(?,?,?,?)',
            ((account(i), username(i), NOW, NOW) for i in range(dedupe_count)))
        connection.executemany('INSERT INTO global_seen VALUES(?,?,?,?)',
            ((account(i), '["followers"]', NOW, NOW) for i in range(dedupe_count)))
        connection.executemany('INSERT INTO workbench_identity_claims VALUES(?,?,\'followers\',?,?)',
            ((account(i), owner_id, target, NOW) for i in range(result_count)))
        connection.executemany('''INSERT INTO task_results
            (id,task_id,target_id,account_id,sources_json,visibility,profile_json,
             screening_json,qualified,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            ((f'scale-r{i:07}', task, target, account(i), '["followers"]',
              'public' if i < public else 'private' if i < classified else 'unknown',
              profile, '{}', 1 if i < classified and i % 2 == 0 else 0, NOW, NOW)
             for i in range(result_count)))
        connection.executemany('''INSERT INTO workbench_candidates
            (id,owner_user_id,account_id,visibility,status,profile_json,source_mode,
             source_target,created_at,updated_at,reviewed_at)
            VALUES(?,?,?,?,?,?,'followers',?,?,?,?)''',
            ((f'scale-c{i:07}', owner_id, account(i), 'public' if i < public else 'private',
              'approved' if i % 3 == 0 else 'pending', profile, target, NOW, NOW,
              NOW if i % 3 == 0 else None) for i in range(classified) if i % 2 == 0))
        connection.executemany('''INSERT INTO workbench_collection_exclusions
            (id,account_id,owner_user_id,username_display,reason_code,reason,
             location_country,profile_snapshot_json,excluded_at) VALUES(?,?,?,?,?,?,NULL,?,?)''',
            ((f'scale-e{i:07}', account(i), owner_id, username(i), 'synthetic',
              'Synthetic fixture', profile, NOW) for i in range(result_count)
             if i % 2 == 1 or i >= classified))
        connection.executemany('''INSERT INTO split_candidate_history
            (id,owner_user_id,username_norm,username_display,completed_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)''',
            ((f'scale-s{i:07}', owner_id, f'synthetic_split_{i}', f'synthetic_split_{i}',
              NOW, NOW, NOW) for i in range(split_count)))
        connection.execute('''INSERT INTO task_checkpoints
            (id,task_id,target_id,mode,stage,cursor_json,counters_json,updated_at)
            VALUES(?,?,?,'followers','screening_accounts','{}',?,?)''',
            ('snapshot-scale-checkpoint', task, target,
             json.dumps(dict(source_total=dedupe_count, discovered=dedupe_count,
                             processed=result_count, saved=result_count,
                             skipped_global_duplicates=0)), NOW))
        connection.execute('UPDATE workbench_state_revision SET revision=revision+1 WHERE singleton_id=1')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return dict(owner_id=owner_id, task_id=task, target_id=target,
                total_collected=result_count, total_public=public, total_private=private,
                total_split=split_count, global_dedupe_count=dedupe_count,
                qualified_for_review=(classified + 1)//2,
                discarded=result_count-(classified+1)//2,
                seed_seconds=round(time.perf_counter()-started, 3))
