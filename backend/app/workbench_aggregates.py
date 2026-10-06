"""Exact, incremental workbench counters and actionable IDs (no payload copies).

Source rows remain authoritative. Small per-row memberships remember the former
owner/bucket so cascading deletes and parent owner/platform changes can subtract
exactly once even after their parent disappears. SQLite triggers update these
projections in the source transaction, including writes from older Core builds.
No TTL, page-cap approximation, lifetime scan on polling or owner-wide recount on
a normal mutation is used. Upgrade and explicit restore rebuild atomically.
"""
from __future__ import annotations

import sqlite3

VERSION = 40
_VIEWS = tuple('workbench_aggregate_refresh_' + kind for kind in ('claim','result','candidate','split','exclusion','dismissal','success')) + ('workbench_actionable_refresh',)
_TABLES = ('workbench_aggregate_state', 'workbench_aggregate_members',
           'workbench_snapshot_counts', 'workbench_actionable_candidates')
_IG = "account.current_username_norm NOT GLOB 'fb:*'"
_TASK_IG = """CASE WHEN json_valid(task.settings_json) THEN json_type(task.settings_json)='object'
    AND (json_type(task.settings_json,'$.platform') IS NULL OR
         (json_type(task.settings_json,'$.platform')='text' AND json_extract(task.settings_json,'$.platform')='instagram'))
    ELSE 0 END"""

# kind: (table, stable row key, owner, bucket, joins, qualification, relevant columns)
_SOURCES = {
    'claim': ('workbench_identity_claims', 'r.account_id', "''", "''",
              'JOIN instagram_accounts account ON account.id=r.account_id', _IG,
              'account_id'),
    'result': ('task_results', 'r.id', 'task.owner_user_id', 'r.visibility',
               'JOIN tasks task ON task.id=r.task_id JOIN instagram_accounts account ON account.id=r.account_id',
               f'{_IG} AND ({_TASK_IG})', 'id,task_id,account_id,visibility'),
    'candidate': ('workbench_candidates', 'r.id', 'r.owner_user_id', "r.status||':'||r.visibility",
                  'JOIN instagram_accounts account ON account.id=r.account_id', _IG,
                  'id,owner_user_id,account_id,status,visibility'),
    'split': ('split_candidate_history', 'r.id', 'r.owner_user_id', "''", '',
              "r.username_norm NOT GLOB 'fb:*'", 'id,owner_user_id,username_norm'),
    'exclusion': ('workbench_collection_exclusions', 'r.id', 'r.owner_user_id', "''", '',
                  "r.username_display NOT GLOB 'fb:*'", 'id,owner_user_id,username_display'),
    'dismissal': ('workbench_candidate_dismissals', 'r.id', 'r.owner_user_id', "''",
                  'JOIN workbench_candidates candidate ON candidate.id=r.candidate_id '
                  'JOIN instagram_accounts account ON account.id=candidate.account_id', _IG,
                  'id,owner_user_id,candidate_id'),
    'success': ('action_success_ledger', 'json_array(r.owner_user_id,r.operation,r.username_norm)',
                'r.owner_user_id', 'r.operation', '', "r.username_norm NOT GLOB 'fb:*'",
                'owner_user_id,operation,username_norm'),
}


def _select(kind: str, predicate='1') -> str:
    table, key, owner, bucket, joins, scope, _ = _SOURCES[kind]
    return (f"SELECT '{kind}',{key},{owner},{bucket} FROM {table} r {joins} "
            f'WHERE ({scope}) AND ({predicate})')


def _key(kind: str, prefix: str) -> str:
    return _SOURCES[kind][1].replace('r.', prefix + '.')


def _refresh_direct(kind: str, keys: str) -> str:
    """Replace only named memberships; old contribution survives missing parent."""
    return (f"DELETE FROM workbench_aggregate_members WHERE kind='{kind}' AND record_id IN ({keys});\n"
            'INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) ' +
            _select(kind, f'{_SOURCES[kind][1]} IN ({keys})') + ';\n')


def _keys(kind: str, predicate: str) -> str:
    return f'SELECT {_SOURCES[kind][1]} FROM {_SOURCES[kind][0]} r WHERE {predicate}'


def _metrics(prefix: str) -> str:
    """Fixed small metric set per membership; generated SQL never parses payload."""
    metrics = [
        ('claim', "'claimed'", '1'),
        ('result', "'total_collected'", '1'),
        ('result', "'total_public'", f"{prefix}.bucket='public'"),
        ('result', "'total_private'", f"{prefix}.bucket='private'"),
        ('candidate', f"'candidate:'||{prefix}.bucket", '1'),
        ('split', "'total_split'", '1'),
        ('exclusion', "'collection_excluded'", '1'),
        ('dismissal', "'approved_dismissed'", '1'),
        ('success', f"'success:'||{prefix}.bucket", '1'),
    ]
    return ' UNION ALL '.join(f'SELECT {metric} AS metric WHERE {prefix}.kind=\'{kind}\' AND {condition}'
                              for kind, metric, condition in metrics)


def _approved_select(predicate='1') -> str:
    return f"""SELECT candidate.id,candidate.owner_user_id,candidate.account_id,
        candidate.visibility,candidate.reviewed_at
        FROM workbench_candidates candidate
        JOIN instagram_accounts account ON account.id=candidate.account_id
        WHERE candidate.status='approved' AND {_IG} AND ({predicate})
          AND NOT EXISTS(SELECT 1 FROM workbench_candidate_dismissals dismissal
                         WHERE dismissal.candidate_id=candidate.id)
          AND NOT EXISTS(SELECT 1 FROM instagram_username_aliases alias
              WHERE alias.account_id=candidate.account_id AND EXISTS(
                  SELECT 1 FROM action_success_ledger success
                  WHERE success.owner_user_id=candidate.owner_user_id
                    AND success.operation=CASE candidate.visibility WHEN 'public' THEN 'greet' ELSE 'follow' END
                    AND success.username_norm=alias.username_norm
                    AND success.username_norm NOT GLOB 'fb:*'))"""


def _approved_refresh_direct(keys: str) -> str:
    return (f'DELETE FROM workbench_actionable_candidates WHERE candidate_id IN ({keys});\n'
            'INSERT INTO workbench_actionable_candidates(candidate_id,owner_user_id,account_id,visibility,reviewed_at) ' +
            _approved_select(f'candidate.id IN ({keys})') + ';\n')


def _refresh(kind: str, keys: str) -> str:
    # Separate no-row views avoid compiling seven unrelated trigger programs
    # whenever one changed row needs a single kind-specific refresh.
    values = keys if keys.lstrip().startswith('SELECT') else f'SELECT value FROM json_each(json_array({keys}))'
    return f'INSERT INTO workbench_aggregate_refresh_{kind}(record_id) ' + values + ';\n'


def _approved_refresh(keys: str) -> str:
    values = keys if keys.lstrip().startswith('SELECT') else f'SELECT value FROM json_each(json_array({keys}))'
    return 'INSERT INTO workbench_actionable_refresh(candidate_id) ' + values + ';\n'


def _trigger(name: str, timing: str, table: str, body: str, when='') -> tuple[str, str]:
    full_name = 'trg_workbench_aggregate_' + name
    return full_name, f'CREATE TRIGGER IF NOT EXISTS {full_name} {timing} ON {table} {when} BEGIN\n{body}\nEND'


def _trigger_definitions() -> dict[str, str]:
    triggers = {}
    def add(*args, **kwargs):
        name, sql = _trigger(*args, **kwargs)
        triggers[name] = sql
    for kind in _SOURCES:
        add('refresh_' + kind, 'INSTEAD OF INSERT', 'workbench_aggregate_refresh_' + kind,
            _refresh_direct(kind, 'NEW.record_id'))
    add('refresh_actionable', 'INSTEAD OF INSERT', 'workbench_actionable_refresh',
        _approved_refresh_direct('NEW.candidate_id'))
    active = 'WHEN (SELECT rebuilding FROM workbench_aggregate_state WHERE singleton_id=1)=0'
    add('member_insert', 'AFTER INSERT', 'workbench_aggregate_members', f"""
        INSERT INTO workbench_snapshot_counts(owner_user_id,metric,total)
        SELECT NEW.owner_user_id,metric,1 FROM ({_metrics('NEW')}) WHERE 1
        ON CONFLICT(owner_user_id,metric) DO UPDATE SET total=total+1;""", when=active)
    add('member_delete', 'AFTER DELETE', 'workbench_aggregate_members', f"""
        UPDATE workbench_snapshot_counts SET total=total-1
        WHERE owner_user_id=OLD.owner_user_id AND metric IN ({_metrics('OLD')});""", when=active)
    add('actionable_insert', 'AFTER INSERT', 'workbench_actionable_candidates', """
        INSERT INTO workbench_snapshot_counts(owner_user_id,metric,total)
        VALUES(NEW.owner_user_id,'approved:'||NEW.visibility,1)
        ON CONFLICT(owner_user_id,metric) DO UPDATE SET total=total+1;""", when=active)
    add('actionable_delete', 'AFTER DELETE', 'workbench_actionable_candidates', """
        UPDATE workbench_snapshot_counts SET total=total-1
        WHERE owner_user_id=OLD.owner_user_id AND metric='approved:'||OLD.visibility;""", when=active)

    # Every relevant source mutation refreshes its former AND current row key.
    # UPDATE OF omits profile/cache payloads and unrelated timestamps.
    for kind, (table, _, _, _, _, _, columns) in _SOURCES.items():
        for event in ('insert', 'delete', 'update'):
            prefixes = ('OLD', 'NEW') if event == 'update' else (('OLD',) if event == 'delete' else ('NEW',))
            key_list = ','.join(_key(kind, prefix) for prefix in prefixes)
            body = _refresh(kind, key_list)
            if kind == 'candidate':
                candidate_ids = ','.join(prefix + '.id' for prefix in prefixes)
                body += _refresh('dismissal', _keys('dismissal', f'r.candidate_id IN ({candidate_ids})'))
                body += _approved_refresh(candidate_ids)
            elif kind == 'dismissal':
                body += _approved_refresh(','.join(prefix + '.candidate_id' for prefix in prefixes))
            elif kind == 'success':
                # Separate username seeks keep owner cardinality out of the
                # update plan; an OR over owners/aliases can drive a large owner scan.
                keys = ' UNION '.join(
                    'SELECT candidate.id FROM instagram_username_aliases alias '
                    'JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id '
                    f'WHERE alias.username_norm={prefix}.username_norm AND candidate.owner_user_id={prefix}.owner_user_id'
                    for prefix in prefixes)
                body += _approved_refresh(keys)
            timing = 'AFTER ' + ('UPDATE OF ' + columns if event == 'update' else event.upper())
            changed = ('WHEN ' + ' OR '.join(f'OLD.{column} IS NOT NEW.{column}' for column in columns.split(','))) if event=='update' else ''
            add(kind + '_' + event, timing, table, body, when=changed)

    # Reviewed order is a separate dependency from candidate status/count bucket.
    add('candidate_order', 'AFTER UPDATE OF reviewed_at', 'workbench_candidates', _approved_refresh('NEW.id'), when='WHEN OLD.reviewed_at IS NOT NEW.reviewed_at')
    for event in ('insert', 'delete', 'update'):
        prefixes = ('OLD', 'NEW') if event == 'update' else (('OLD',) if event == 'delete' else ('NEW',))
        ids = ','.join(prefix + '.account_id' for prefix in prefixes)
        add('alias_' + event, 'AFTER ' + ('UPDATE OF account_id,username_norm' if event == 'update' else event.upper()),
            'instagram_username_aliases', _approved_refresh(f'SELECT id FROM workbench_candidates WHERE account_id IN ({ids})'))

    # Canonical identity namespace determines scope, never copied profile JSON.
    # Parent removal BEFORE child cascades retires stored memberships exactly once.
    for event in ('insert', 'update', 'delete'):
        ids = 'OLD.id,NEW.id' if event == 'update' else ('OLD.id' if event == 'delete' else 'NEW.id')
        body = ''
        for kind in ('claim', 'result', 'candidate'):
            keys = _keys(kind, f'r.account_id IN ({ids})')
            if event == 'delete':
                body += f"DELETE FROM workbench_aggregate_members WHERE kind='{kind}' AND record_id IN ({keys});\n"
            else:
                body += _refresh(kind, keys)
        dismissal_keys = _keys('dismissal', f'r.candidate_id IN (SELECT id FROM workbench_candidates WHERE account_id IN ({ids}))')
        candidate_keys = f'SELECT id FROM workbench_candidates WHERE account_id IN ({ids})'
        if event == 'delete':
            body += f"DELETE FROM workbench_aggregate_members WHERE kind='dismissal' AND record_id IN ({dismissal_keys});\n"
            body += f'DELETE FROM workbench_actionable_candidates WHERE candidate_id IN ({candidate_keys});\n'
        else:
            body += _refresh('dismissal', dismissal_keys) + _approved_refresh(candidate_keys)
        timing = 'BEFORE DELETE' if event == 'delete' else ('AFTER UPDATE OF id,current_username_norm' if event == 'update' else 'AFTER INSERT')
        when = ''
        if event=='update':
            when = "WHEN OLD.id IS NOT NEW.id OR (OLD.current_username_norm GLOB 'fb:*')<>(NEW.current_username_norm GLOB 'fb:*')"
        elif event=='insert':
            when = """WHEN EXISTS(SELECT 1 FROM workbench_identity_claims WHERE account_id=NEW.id)
                OR EXISTS(SELECT 1 FROM task_results WHERE account_id=NEW.id)
                OR EXISTS(SELECT 1 FROM workbench_candidates WHERE account_id=NEW.id)"""
        add('account_' + event, timing, 'instagram_accounts', body, when=when)
    for event in ('insert', 'update', 'delete'):
        ids = 'OLD.id,NEW.id' if event == 'update' else ('OLD.id' if event == 'delete' else 'NEW.id')
        keys = _keys('result', f'r.task_id IN ({ids})')
        body = (f"DELETE FROM workbench_aggregate_members WHERE kind='result' AND record_id IN ({keys});\n"
                if event == 'delete' else _refresh('result', keys))
        timing = 'BEFORE DELETE' if event == 'delete' else ('AFTER UPDATE OF id,owner_user_id,settings_json' if event == 'update' else 'AFTER INSERT')
        when = ''
        if event=='insert':
            when = 'WHEN EXISTS(SELECT 1 FROM task_results WHERE task_id=NEW.id)'
        elif event=='update':
            # Ordinary live-queue/settings changes must not revisit lifetime
            # results when neither owner nor platform membership changed.
            old_scope = _TASK_IG.replace('task.', 'OLD.')
            new_scope = _TASK_IG.replace('task.', 'NEW.')
            when = f'WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR ({old_scope}) IS NOT ({new_scope})'
        add('task_' + event, timing, 'tasks', body, when=when)
    return triggers


_SCHEMA = (
    *(f"CREATE VIEW IF NOT EXISTS workbench_aggregate_refresh_{kind} AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0" for kind in _SOURCES),
    "CREATE VIEW IF NOT EXISTS workbench_actionable_refresh AS SELECT CAST(NULL AS TEXT) AS candidate_id WHERE 0",
    '''CREATE TABLE IF NOT EXISTS workbench_aggregate_state(
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), rebuilding INTEGER NOT NULL DEFAULT 0)''',
    'INSERT OR IGNORE INTO workbench_aggregate_state VALUES(1,0)',
    '''CREATE TABLE IF NOT EXISTS workbench_snapshot_counts(
        owner_user_id TEXT NOT NULL,metric TEXT NOT NULL,total INTEGER NOT NULL CHECK(total>=0),
        PRIMARY KEY(owner_user_id,metric))''',
    '''CREATE TABLE IF NOT EXISTS workbench_aggregate_members(
        kind TEXT NOT NULL,record_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,bucket TEXT NOT NULL,
        PRIMARY KEY(kind,record_id))''',
    '''CREATE TABLE IF NOT EXISTS workbench_actionable_candidates(
        candidate_id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,account_id TEXT NOT NULL,
        visibility TEXT NOT NULL,reviewed_at TEXT)''',
    '''CREATE INDEX IF NOT EXISTS idx_workbench_actionable_page
        ON workbench_actionable_candidates(owner_user_id,visibility,reviewed_at DESC,candidate_id DESC)''',
    # Small additive covering indexes keep first upgrade backfills off retained
    # profile/screening/cache blobs. Existing startup indexes remain unchanged.
    '''CREATE INDEX IF NOT EXISTS idx_success_aggregate_key ON action_success_ledger(json_array(owner_user_id,operation,username_norm))''',
    '''CREATE INDEX IF NOT EXISTS idx_results_aggregate_rebuild ON task_results(task_id,account_id,visibility,id)''',
    '''CREATE INDEX IF NOT EXISTS idx_split_history_aggregate_rebuild ON split_candidate_history(owner_user_id,username_norm,id)''',
    '''CREATE INDEX IF NOT EXISTS idx_exclusions_aggregate_rebuild ON workbench_collection_exclusions(owner_user_id,username_display,id)''',
)


def _atomic(connection):
    nested = connection.in_transaction
    connection.execute('SAVEPOINT workbench_aggregate_rebuild' if nested else 'BEGIN IMMEDIATE')
    return nested


def _finish(connection, nested, *, error=False):
    if nested:
        if error:
            connection.execute('ROLLBACK TO SAVEPOINT workbench_aggregate_rebuild')
        connection.execute('RELEASE SAVEPOINT workbench_aggregate_rebuild')
    elif error:
        connection.rollback()
    else:
        connection.commit()


def _rebuild(connection):
    connection.execute('UPDATE workbench_aggregate_state SET rebuilding=1 WHERE singleton_id=1')
    connection.execute('DELETE FROM workbench_aggregate_members')
    connection.execute('DELETE FROM workbench_actionable_candidates')
    connection.execute('DELETE FROM workbench_snapshot_counts')
    for kind in _SOURCES:
        connection.execute('INSERT INTO workbench_aggregate_members ' + _select(kind))
    connection.execute('INSERT INTO workbench_actionable_candidates ' + _approved_select())
    # Group only the four narrow membership fields; no retained JSON is copied.
    # SQLite does not support lateral FROM subqueries; each fixed contribution
    # is an ordinary covering scan over the compact membership table instead.
    contributions = (
        ("kind='claim'", "'claimed'"), ("kind='result'", "'total_collected'"),
        ("kind='result' AND bucket='public'", "'total_public'"),
        ("kind='result' AND bucket='private'", "'total_private'"),
        ("kind='candidate'", "'candidate:'||bucket"), ("kind='split'", "'total_split'"),
        ("kind='exclusion'", "'collection_excluded'"), ("kind='dismissal'", "'approved_dismissed'"),
        ("kind='success'", "'success:'||bucket"),
    )
    for where, metric in contributions:
        connection.execute(f'''INSERT INTO workbench_snapshot_counts
            SELECT owner_user_id,{metric},COUNT(*) FROM workbench_aggregate_members WHERE {where}
            GROUP BY owner_user_id,{metric}''')
    connection.execute('''INSERT INTO workbench_snapshot_counts
        SELECT owner_user_id,'approved:'||visibility,COUNT(*) FROM workbench_actionable_candidates
        GROUP BY owner_user_id,visibility''')
    connection.execute('UPDATE workbench_aggregate_state SET rebuilding=0 WHERE singleton_id=1')


def initialize_workbench_aggregates(connection: sqlite3.Connection) -> None:
    """Install persistent triggers and one-time version-40 backfill atomically.

    A missing table/trigger also triggers a repair, covering older/offline tools
    which removed projection triggers. Crash rollback cannot commit half a count.
    """
    definitions = _trigger_definitions()
    installed = {row[0]:row[1] for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','view')")}
    existing = set(installed)
    changed = any(installed.get(name,'') != sql.replace('CREATE TRIGGER IF NOT EXISTS ', 'CREATE TRIGGER ')
                  for name,sql in definitions.items())
    rebuild = (not set(_TABLES + _VIEWS).issubset(existing) or changed
               or connection.execute('SELECT 1 FROM schema_migrations WHERE version=?', (VERSION,)).fetchone() is None)
    nested = _atomic(connection)
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        if changed:
            for name in definitions:
                connection.execute(f'DROP TRIGGER IF EXISTS {name}')
        for statement in definitions.values():
            connection.execute(statement)
        if rebuild:
            _rebuild(connection)
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,datetime('now'))", (VERSION,))
        _finish(connection, nested)
    except BaseException:
        _finish(connection, nested, error=True)
        raise


def rebuild_workbench_aggregates(connection: sqlite3.Connection) -> None:
    """Reconcile restore/import inside its outer transaction without committing it."""
    nested = _atomic(connection)
    try:
        _rebuild(connection)
        _finish(connection, nested)
    except BaseException:
        _finish(connection, nested, error=True)
        raise


def snapshot_totals(connection: sqlite3.Connection, owner_user_id: str) -> dict[str, int]:
    totals = {row[0]: int(row[1]) for row in connection.execute(
        'SELECT metric,total FROM workbench_snapshot_counts WHERE owner_user_id=?', (owner_user_id,))}
    claimed = connection.execute("SELECT total FROM workbench_snapshot_counts WHERE owner_user_id='' AND metric='claimed'").fetchone()
    totals['claimed'] = int(claimed[0]) if claimed else 0
    return totals
