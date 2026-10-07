"""Isolated R6.1 upgrade fixture; never opens an existing user's data directory."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


OLD_INDEX = 'idx_split_completed_target_report'
TARGET_INDEX = 'idx_split_completed_targets_report_period'
OLD_HISTORY_SQL = (f'CREATE INDEX {OLD_INDEX} ON split_candidate_history'
                   "(owner_user_id,source_target_id) WHERE source_status='completed'")
OLD_TARGET_SQL = (f'CREATE INDEX {OLD_INDEX} ON split_completed_targets'
                  '(owner_user_id,julianday(completed_at) DESC,target_id)')
START, END = '2026-10-03T00:00:00+08:00', '2026-10-04T00:00:00+08:00'
NOW, OLD = '2026-10-03T06:00:00+00:00', '2026-09-01T06:00:00+00:00'
EXPECTED = {'collection': 3, 'follow': 1, 'split': 3, 'added': 7}
TABLES = (
    'instagram_accounts', 'instagram_username_aliases', 'global_seen',
    'global_seen_stats', 'global_seen_platform_stats', 'global_identity_owners',
    'tasks', 'task_targets', 'task_windows', 'task_results', 'task_result_duplicate_archive', 'workbench_candidates',
    'workbench_collection_exclusions', 'workbench_identity_claims',
    'split_completed_targets', 'split_candidate_history', 'split_admission_totals',
    'action_success_ledger', 'follow_monitor_seen', 'follow_monitor_rounds',
    'posting_receipts', 'posting_assets', 'posting_jobs', 'browser_operation_leases',
)


def _isolated(database, isolated_directory):
    database, directory = Path(database).resolve(), Path(isolated_directory).resolve()
    if (not directory.name.startswith(('Juxin-CoreSmoke-', 'Juxin-ReportIndex-'))
            or database.parent != directory or not database.is_file()):
        raise ValueError('Report index fixture requires its existing isolated temporary database')
    return database


def state(database):
    """Stable streaming hashes, including complete row payloads and dedup facts."""
    result = {}
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as c:
        for table in TABLES:
            columns = c.execute(f'PRAGMA table_info({table})').fetchall()
            if not columns:
                continue
            keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = ','.join('"' + key + '"' for key in keys) or 'rowid'
            digest, count = hashlib.sha256(), 0
            for row in c.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode())
                digest.update(b'\n'); count += 1
            result[table] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def install_old_index(database, *, isolated_directory, owner_table='split_candidate_history'):
    database = _isolated(database, isolated_directory)
    if owner_table not in {'split_candidate_history', 'split_completed_targets'}:
        raise ValueError('Unsupported historical index owner')
    with closing(sqlite3.connect(database)) as c:
        c.execute(f'DROP INDEX IF EXISTS {TARGET_INDEX}')
        c.execute(f'DROP INDEX IF EXISTS {OLD_INDEX}')
        c.execute(OLD_HISTORY_SQL if owner_table == 'split_candidate_history' else OLD_TARGET_SQL)
        c.commit()
        row = c.execute('SELECT tbl_name,sql FROM sqlite_master WHERE name=?', (OLD_INDEX,)).fetchone()
        if row[0] != owner_table:
            raise AssertionError('Legacy index was not physically installed on the requested table')
    return {'table': row[0], 'sql': row[1]}


def seed(database, owner, *, isolated_directory):
    """Add positive four-card evidence for a new, otherwise empty fixture owner."""
    database = _isolated(database, isolated_directory)
    with closing(sqlite3.connect(database)) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES('r61-task',?,'index upgrade fixture','draft','[\"followers\"]','{}',?,?)", (owner, OLD, OLD))
        c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES('r61-target','r61-task','r61.source','r61.source',1,'pending',?,?)", (OLD, OLD))
        for name, at in [('candidate', NOW), ('task', NOW), ('exclusion', NOW), ('old', OLD)]:
            ident = 'r61-' + name
            c.execute('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)', (ident, ident, ident, at, at))
            c.execute('INSERT INTO global_seen VALUES(?,?,?,?)', (ident, '["fixture"]', at, at))
            c.execute('INSERT INTO global_identity_owners VALUES(?,?,?,?)', (ident, owner, at, at))
            if name in {'candidate', 'old'}:
                c.execute("INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,profile_json,created_at,updated_at) VALUES(?,?,?,'private','{}',?,?)", (ident, owner, ident, at, at))
            elif name == 'task':
                c.execute("INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) VALUES(?,'r61-task','r61-target',?,'[]','public','{}','{}',?,?)", (ident, ident, at, at))
            else:
                c.execute("INSERT INTO workbench_collection_exclusions(id,account_id,owner_user_id,username_display,reason_code,reason,profile_snapshot_json,excluded_at) VALUES(?,?,?,?,'not_us','fixture','{}',?)", (ident, ident, owner, ident, at))
        # A later copy of an old identity must not become today's collection.
        c.execute("INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) VALUES('r61-old-copy','r61-task','r61-target','r61-old','[]','private','{}','{}',?,?)", (NOW, NOW))
        # Match the normal persistence path's derived success timestamp so a
        # startup backfill is not mistaken for a hotfix business-record change.
        c.execute("UPDATE task_targets SET last_success_at=? WHERE id='r61-target'", (NOW,))
        for target, at in [('r61-split', NOW), ('r61-fact-only', NOW), ('r61-old-split', OLD)]:
            c.execute("INSERT INTO split_completed_targets(target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at) VALUES(?,?,'r61.source','r61.source','r61-task','r61-window',?)", (target, owner, at))
        for ident, target in [('r61-split-copy', 'r61-split'), ('r61-history-only', None), ('r61-late-copy', 'r61-old-split')]:
            c.execute("INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_target_id,source_status,source_window_id,completed_at,created_at,updated_at) VALUES(?,?,'r61.source','r61.source',?,'completed','r61-window',?,?,?)", (ident, owner, target, NOW, NOW, NOW))
        c.execute("INSERT INTO action_campaigns(id,owner_user_id,operation,execution_type,profile_id,interval_min_seconds,interval_max_seconds,limit_count,status,created_at,updated_at) VALUES('r61-campaign',?,'follow','campaign','r61-window',0,0,1,'completed',?,?)", (owner, NOW, NOW))
        c.execute("INSERT INTO action_targets(id,campaign_id,username_norm,username_display,queue_order,status,updated_at) VALUES('r61-action','r61-campaign','r61.follow','r61.follow',1,'confirmed',?)", (NOW,))
        c.execute("INSERT INTO action_attempts VALUES('r61-attempt','r61-campaign','r61-action',1,'confirmed',?,?,'{}')", (NOW, NOW))
        c.execute("INSERT INTO action_success_ledger VALUES(?,'follow','r61.follow','r61.follow','r61-campaign','r61-action','r61-attempt',?)", (owner, NOW))
        c.execute("INSERT INTO follow_monitor_rounds(owner_user_id,batch_id,profile_id,owner_username,actual_count,first_read_count,added_count,repeat_count,unfollow_count,checked_at) VALUES(?,'r61-round','r61-window','fixture',20,20,7,0,0,?)", (owner, NOW))
        # Window leases remain part of the index upgrade's independent oracle.
        # Retired posting records are covered by the legacy archive fixture.
        c.execute("INSERT INTO browser_operation_leases VALUES('r61-held-window',?,'collection','r61-task','r61-fixture-lease',?,?,?)", (owner, NOW, NOW, '2099-01-01T00:00:00+00:00'))
        c.commit()
    return {'start': START, 'end': END, 'totals': dict(EXPECTED)}


def verify(database, before, legacy):
    after = state(database)
    if after != before:
        changed = [table for table in set(before) | set(after) if before.get(table) != after.get(table)]
        raise AssertionError('Report index upgrade changed retained data: ' + ', '.join(sorted(changed)))
    with closing(sqlite3.connect(database)) as c:
        old = c.execute('SELECT tbl_name,sql FROM sqlite_master WHERE name=?', (OLD_INDEX,)).fetchone()
        new = c.execute('SELECT tbl_name FROM sqlite_master WHERE name=?', (TARGET_INDEX,)).fetchone()
        if old != (legacy['table'], legacy['sql']) or new != ('split_completed_targets',):
            raise AssertionError('Report indexes do not preserve the legacy index and add the target index')
        if c.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise AssertionError('Report upgrade fixture integrity check failed')
    return {'verified': True, 'retained_rows': before, 'legacy_index_preserved': True,
            'target_period_index_created': True, 'inventory_dedup_hashes_preserved': True,
            'window_leases_preserved': True, 'synthetic': True, 'user_data_touched': False}
