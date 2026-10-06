"""Synthetic completed-card evidence for the isolated installed-Core probe.

Standard-library only. Never open a user's database: the caller must name the
probe's temporary directory and the fresh HTTP-created smoke task explicitly.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


PREFIX = 'installed-completed-card-'
SOURCE = 'installed.card.source'
RESULT_USERNAME = 'installed.card.result'
WINDOW = 'fixture-only-never-opened'
EVENT = 'task_target.dismissed_from_collection_list'


def seed_completed_card_smoke(database_path, owner_id: str, task_id: str,
                              target_id: str, *, isolated_directory) -> dict:
    """Complete one fabricated IG target, retaining a positive header gap.

    Adds one saved profile and one immutable completion-history row. A completed
    explicit recheck is deliberately retained despite the 3-versus-1 header gap.
    All freshness/collision guards run before inserts; failures roll back.
    """
    path = Path(database_path).resolve(strict=True)
    isolated = Path(isolated_directory).resolve(strict=True)
    if (not path.is_file() or path.parent != isolated
            or path.name != 'collector.sqlite3' or not isolated.name.startswith('Juxin-CoreSmoke-')):
        raise ValueError('Completed-card smoke requires its isolated probe database')
    if any(not isinstance(value, str) or not value.strip() for value in (owner_id, task_id, target_id)):
        raise ValueError('Completed-card smoke requires explicit owner, task and target IDs')
    now = datetime.now(timezone.utc).isoformat()
    ids = {key: PREFIX + key for key in ('account', 'result', 'checkpoint', 'history')}
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        try:
            users = connection.execute('SELECT id,username_norm FROM app_users').fetchall()
            if len(users) != 1 or tuple(users[0]) != (owner_id, 'install-smoke'):
                raise ValueError('Completed-card smoke requires the isolated install-smoke owner')
            source = connection.execute('''SELECT task.settings_json,task.modes_json,task.status,
                    target.status AS target_status,target.username_norm,target.current_window_id
                FROM tasks task JOIN task_targets target ON target.task_id=task.id
                WHERE task.owner_user_id=? AND task.id=? AND target.id=?''',
                (owner_id, task_id, target_id)).fetchone()
            if (source is None or source['status'] != 'draft' or source['target_status'] != 'pending'
                    or source['current_window_id'] is not None or source['username_norm'] != SOURCE
                    or json.loads(source['settings_json']).get('platform') != 'instagram'
                    or json.loads(source['modes_json']) != ['followers']):
                raise ValueError('Completed-card smoke requires its fresh owned Instagram task/target')
            if connection.execute('SELECT COUNT(*) FROM task_targets WHERE task_id=?', (task_id,)).fetchone()[0] != 1:
                raise ValueError('Completed-card smoke refuses a task with other targets')
            windows = connection.execute('SELECT profile_id,status FROM task_windows WHERE task_id=?', (task_id,)).fetchall()
            if [tuple(row) for row in windows] != [(WINDOW, 'selected')]:
                raise ValueError('Completed-card smoke requires its unused fabricated window')
            if connection.execute('SELECT 1 FROM browser_operation_leases LIMIT 1').fetchone():
                raise ValueError('Completed-card smoke refuses a database with browser leases')
            for table, column in (('task_results', 'target_id'), ('task_checkpoints', 'target_id'),
                                  ('task_source_rechecks', 'target_id'), ('task_mode_candidates', 'target_id'),
                                  ('task_target_list_dismissals', 'target_id'),
                                  ('split_candidate_history', 'source_target_id')):
                if connection.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (target_id,)).fetchone():
                    raise ValueError('Completed-card smoke refuses existing target evidence')
            for table, key in (('instagram_accounts', 'account'), ('task_results', 'result'),
                               ('task_checkpoints', 'checkpoint'), ('split_candidates', 'history'),
                               ('split_candidate_history', 'history')):
                if connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (ids[key],)).fetchone():
                    raise ValueError('Completed-card smoke refuses existing fixture IDs')
            for table, column in (('instagram_accounts', 'current_username_norm'),
                                  ('instagram_username_aliases', 'username_norm')):
                if connection.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (RESULT_USERNAME,)).fetchone():
                    raise ValueError('Completed-card smoke refuses existing fixture usernames')
            connection.execute('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)',
                               (ids['account'], RESULT_USERNAME, RESULT_USERNAME, now, now))
            connection.execute('INSERT INTO instagram_username_aliases VALUES(?,?,?,?)',
                               (ids['account'], RESULT_USERNAME, now, now))
            connection.execute('INSERT INTO global_seen VALUES(?,?,?,?)',
                               (ids['account'], '["followers"]', now, now))
            connection.execute('INSERT INTO global_identity_owners VALUES(?,?,?,?)',
                               (ids['account'], owner_id, now, now))
            connection.execute('INSERT INTO workbench_identity_claims VALUES(?,?,?,?,?)',
                               (ids['account'], owner_id, 'followers', target_id, now))
            connection.execute('''INSERT INTO task_results
                (id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,
                 qualified,created_at,updated_at) VALUES(?,?,?,?,'["followers"]','public',?,'{}',1,?,?)''',
                (ids['result'], task_id, target_id, ids['account'],
                 json.dumps({'platform': 'instagram', 'username': RESULT_USERNAME,
                             'bio': 'Fabricated installed smoke profile; never navigated.'}), now, now))
            connection.execute('''INSERT INTO task_checkpoints
                (id,task_id,target_id,mode,stage,cursor_json,counters_json,recoverable,updated_at)
                VALUES(?,?,?,'followers','mode_completed',?,?,0,?)''',
                (ids['checkpoint'], task_id, target_id,
                 json.dumps({'candidate_spool_version': 1, 'candidate_spool_complete': True,
                             'candidate_spool_natural_end': True, 'rendered_count': 1}),
                 json.dumps({'source_total': 3, 'discovered': 1, 'processed': 1, 'saved': 1,
                             'skipped_global_duplicates': 0}), now))
            connection.execute('''INSERT INTO task_source_rechecks
                (target_id,mode,state,requested_at,completed_at) VALUES(?,'followers','completed',?,?)''',
                (target_id, now, now))
            # Let the service's real completion triggers write immutable history.
            connection.execute('''INSERT INTO split_candidates
                (id,owner_user_id,username_norm,username_display,candidate_kind,source_task_id,
                 source_target_id,queue_state,queued_task_id,queued_target_id,created_at,updated_at)
                VALUES(?,?,?,?,'manual',?,?,'claimed',?,?,?,?)''',
                (ids['history'], owner_id, SOURCE, SOURCE, task_id, target_id, task_id, target_id, now, now))
            connection.execute('''UPDATE task_targets SET status='completed',current_stage='completed_archived',
                preferred_window_id=?,last_success_at=?,updated_at=? WHERE id=?''', (WINDOW, now, now, target_id))
            connection.execute("UPDATE tasks SET status='completed',finished_at=?,updated_at=? WHERE id=?",
                               (now, now, task_id))
            if not connection.execute('SELECT 1 FROM split_candidate_history WHERE id=?', (ids['history'],)).fetchone():
                raise RuntimeError('Completed-card smoke completion history was not archived')
            connection.execute('UPDATE workbench_state_revision SET revision=revision+1 WHERE singleton_id=1')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {'owner_id': owner_id, 'task_id': task_id, 'target_id': target_id, **ids,
            'source_recheck': {'mode': 'followers', 'state': 'completed', 'completed_at': now, 'requested_at': now},
            'identities_added': 1, 'results_added': 1, 'public_added': 1, 'split_added': 1}


def completed_card_state(database_path, fixture: dict) -> dict:
    """Read exact fixture evidence plus ledger counts without loading scale blobs."""
    path = Path(database_path).resolve(strict=True)
    task, target, account = (fixture[key] for key in ('task_id', 'target_id', 'account'))
    selectors = {
        'tasks': ('id=?', (task,)), 'task_targets': ('task_id=?', (task,)),
        'task_windows': ('task_id=?', (task,)), 'task_checkpoints': ('target_id=?', (target,)),
        'task_source_rechecks': ('target_id=?', (target,)),
        'task_results': ('target_id=?', (target,)),
        'split_candidate_history': ('source_target_id=?', (target,)),
        'split_completed_targets': ('target_id=?', (target,)),
        'instagram_accounts': ('id=? OR current_username_norm=?', (account, SOURCE)),
        'instagram_username_aliases': ('account_id=? OR username_norm=?', (account, SOURCE)),
        'global_seen': ('account_id IN (SELECT id FROM instagram_accounts WHERE id=? OR current_username_norm=?)', (account, SOURCE)),
        'global_identity_owners': ('account_id=?', (account,)),
        'workbench_identity_claims': ('source_target=?', (target,)),
        'browser_operation_leases': ('1=1', ()),
    }
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
        connection.execute('BEGIN')
        rows = {table: connection.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY 1', params).fetchall()
                for table, (where, params) in selectors.items()}
        counts = {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in (*selectors, 'workbench_candidates', 'workbench_review_decisions', 'task_list_dismissals')}
        marker = connection.execute('SELECT * FROM task_target_list_dismissals WHERE target_id=?', (target,)).fetchall()
        events = connection.execute('SELECT * FROM event_log WHERE entity_id=? AND event_type=? ORDER BY seq',
                                    (target, EVENT)).fetchall()
    return {'rows': rows, 'counts': counts, 'marker': marker, 'events': events}


def assert_completed_card_dto(task: dict, fixture: dict, *, dismissed: bool) -> None:
    """Fail closed if the exact service DTO cannot distinguish a completed recheck."""
    targets = [row for row in task.get('targets', []) if row.get('id') == fixture['target_id']]
    if (task.get('id') != fixture['task_id'] or task.get('status') != 'completed'
            or task.get('settings', {}).get('platform') != 'instagram' or len(targets) != 1):
        raise RuntimeError('Installed completed-card task/target disappeared or changed scope')
    target = targets[0]
    if (target.get('status') != 'completed' or target.get('collection_list_dismissed') is not dismissed
            or target.get('source_recheck') != fixture['source_recheck']):
        raise RuntimeError('Installed completed-card dismissal/recheck DTO flags are invalid')
    progress = target.get('mode_progress', {}).get('followers', {})
    if any(progress.get(key) != value for key, value in
           {'source_total': 3, 'discovered': 1, 'processed': 1, 'saved': 1}.items()):
        raise RuntimeError('Installed completed-card positive header gap was lost')


def assert_completed_card_retained(before: dict, after: dict, fixture: dict) -> None:
    if before['rows'] != after['rows'] or before['counts'] != after['counts']:
        raise RuntimeError('Installed completed-card dismissal changed retained data')
    if (len(after['marker']) != 1 or after['marker'][0][:2] != (fixture['target_id'], fixture['owner_id'])
            or len(after['events']) != len(before['events']) + 1):
        raise RuntimeError('Installed completed-card dismissal marker/event was not written exactly once')
