"""Exact incremental source-progress totals; original business rows stay authoritative.

A snapshot must not recount an ever-growing target's lifetime results. Small,
rebuildable contributions retain only identity/target/mode and integer evidence.
Each source mutation refreshes the changed identity through unique-key joins in
its existing transaction. No timers, TTLs, worker jobs, or retained JSON copies.
"""
from __future__ import annotations

import sqlite3

_VERSION = 41
_COLUMNS = 'qualified, recorded_total, excluded_total, discarded, hover_discarded'


def _sources(expression: str) -> str:
    return (f"CASE WHEN json_valid({expression}) THEN CASE "
            f"WHEN json_type({expression})='array' THEN {expression} "
            "ELSE '[]' END ELSE '[]' END")


def _contributions(account_ids: str | None = None) -> str:
    """The original four evidence queries, restricted to affected identities."""
    review_scope = f' AND review.account_id IN ({account_ids})' if account_ids else ''
    result_scope = f' AND recorded.account_id IN ({account_ids})' if account_ids else ''
    claim_scope = f' AND claim.account_id IN ({account_ids})' if account_ids else ''
    modes = "('followers', 'following', 'post_likers')"
    source_json = _sources('recorded.sources_json')
    return f"""
        SELECT review.account_id, 'qualified', review.source_target, review.source_mode,
               1, 0, 0, 0, 0
        FROM workbench_candidates review
        JOIN task_results recorded ON recorded.account_id=review.account_id
          AND recorded.target_id=review.source_target
        JOIN tasks task ON task.id=recorded.task_id
          AND task.owner_user_id=review.owner_user_id
        JOIN workbench_identity_claims claim ON claim.account_id=review.account_id
          AND claim.claimed_by_user_id=review.owner_user_id
          AND claim.source IN (review.source_mode, 'collection')
          AND (claim.source_target=review.source_target OR claim.source_target IS NULL)
        WHERE review.source_target IS NOT NULL AND review.source_mode IN {modes}
          AND EXISTS(SELECT 1 FROM json_each({source_json}) source
                     WHERE source.value=review.source_mode) {review_scope}
        UNION ALL
        SELECT review.account_id, 'qualified', claim.source_target, claim.source,
               1, 0, 0, 0, 0
        FROM workbench_candidates review
        JOIN workbench_identity_claims claim ON claim.account_id=review.account_id
          AND claim.claimed_by_user_id=review.owner_user_id
        JOIN task_results recorded ON recorded.account_id=claim.account_id
          AND recorded.target_id=claim.source_target
        JOIN tasks task ON task.id=recorded.task_id
          AND task.owner_user_id=claim.claimed_by_user_id
        WHERE claim.source_target IS NOT NULL AND claim.source IN {modes}
          AND (review.source_target IS NULL OR review.source_mode IS NULL)
          AND (review.source_target IS NULL OR review.source_target=claim.source_target)
          AND (review.source_mode IS NULL OR review.source_mode=claim.source)
          AND EXISTS(SELECT 1 FROM json_each({source_json}) source
                     WHERE source.value=claim.source) {review_scope}
        UNION ALL
        SELECT recorded.account_id, 'recorded', recorded.target_id, source.value,
               0, COUNT(*), COUNT(excluded.id), 0, 0
        FROM task_results recorded
        JOIN tasks task ON task.id=recorded.task_id
        JOIN json_each({source_json}) source
        LEFT JOIN workbench_collection_exclusions excluded
          ON excluded.account_id=recorded.account_id
          AND excluded.owner_user_id=task.owner_user_id
        WHERE source.type IN ('text', 'array', 'object') {result_scope}
        GROUP BY recorded.account_id, recorded.target_id, source.value
        UNION ALL
        SELECT claim.account_id, 'discarded', claim.source_target, claim.source,
               0, 0, 0, 1,
               CASE WHEN json_valid(excluded.profile_snapshot_json)
                    THEN COALESCE(json_extract(excluded.profile_snapshot_json,
                                               '$.page_read_status')='hover_preview', 0)
                    ELSE 0 END
        FROM workbench_identity_claims claim
        JOIN workbench_collection_exclusions excluded ON excluded.account_id=claim.account_id
          AND excluded.owner_user_id=claim.claimed_by_user_id
        WHERE claim.source_target IS NOT NULL {claim_scope}
    """


def _refresh(account_ids: str) -> str:
    return f"""
        DELETE FROM workbench_progress_contributions WHERE account_id IN ({account_ids});
        INSERT INTO workbench_progress_contributions
            (account_id, kind, target_id, mode, {_COLUMNS})
        {_contributions(account_ids)};
    """


def _trigger_definitions() -> dict[str, str]:
    statements = []
    statements.append(f"""CREATE TRIGGER IF NOT EXISTS trg_progress_contribution_insert
        AFTER INSERT ON workbench_progress_contributions BEGIN
          INSERT INTO workbench_progress_totals(target_id, mode, {_COLUMNS})
          VALUES(NEW.target_id, NEW.mode, {', '.join('NEW.'+c for c in _COLUMNS.split(', '))})
          ON CONFLICT(target_id, mode) DO UPDATE SET
            {', '.join(c+'='+c+'+excluded.'+c for c in _COLUMNS.split(', '))};
        END""")
    statements.append(f"""CREATE TRIGGER IF NOT EXISTS trg_progress_contribution_delete
        AFTER DELETE ON workbench_progress_contributions BEGIN
          UPDATE workbench_progress_totals SET
            {', '.join(c+'='+c+'-OLD.'+c for c in _COLUMNS.split(', '))}
          WHERE target_id=OLD.target_id AND mode=OLD.mode;
          DELETE FROM workbench_progress_totals WHERE target_id=OLD.target_id
            AND mode=OLD.mode AND qualified=0 AND recorded_total=0
            AND excluded_total=0 AND discarded=0 AND hover_discarded=0;
        END""")
    statements.append(f"""CREATE TRIGGER IF NOT EXISTS trg_progress_refresh_identity
        INSTEAD OF INSERT ON workbench_progress_refresh BEGIN
          {_refresh('SELECT NEW.account_id')}
        END""")
    for table, columns in (
        ('workbench_candidates', 'account_id,owner_user_id,source_target,source_mode'),
        ('workbench_identity_claims', 'account_id,claimed_by_user_id,source_target,source'),
        ('task_results', 'account_id,task_id,target_id,sources_json'),
        ('workbench_collection_exclusions', 'account_id,owner_user_id,profile_snapshot_json'),
    ):
        for event, ids in (('INSERT', 'SELECT NEW.account_id'),
                           ('DELETE', 'SELECT OLD.account_id'),
                           (f'UPDATE OF {columns}', 'SELECT OLD.account_id UNION SELECT NEW.account_id')):
            suffix = event.split()[0].lower()
            # Recognition reserves a claim before there is any result or
            # exclusion. That empty identity has no progress contribution;
            # avoid evaluating the full proof until evidence actually exists.
            relevant = ''
            if table == 'workbench_identity_claims':
                relevant = f"""WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN ({ids}))
                    OR EXISTS(SELECT 1 FROM workbench_collection_exclusions WHERE account_id IN ({ids}))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN ({ids})
                              AND (qualified<>0 OR discarded<>0))"""
            elif table == 'workbench_candidates':
                relevant = f"""WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN ({ids}))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN ({ids})
                              AND qualified<>0)"""
            if suffix == 'update':
                changed = ' OR '.join(f'OLD.{column} IS NOT NEW.{column}'
                                      for column in columns.split(','))
                relevant = (f'WHEN ({changed})' +
                            (f' AND ({relevant.removeprefix("WHEN ")})' if relevant else ''))
            statements.append(f"""CREATE TRIGGER IF NOT EXISTS trg_progress_{table}_{suffix}
                AFTER {event} ON {table} {relevant} BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) {ids};
                END""")
    statements.append(f"""CREATE TRIGGER IF NOT EXISTS trg_progress_task_owner_update
        AFTER UPDATE OF owner_user_id ON tasks
        WHEN OLD.owner_user_id IS NOT NEW.owner_user_id BEGIN
          INSERT INTO workbench_progress_refresh(account_id)
          SELECT account_id FROM task_results WHERE task_id=NEW.id;
        END""")
    return {statement.split()[5]: statement for statement in statements}


def initialize_workbench_progress_aggregates(connection: sqlite3.Connection) -> None:
    """Install/backfill atomically after legacy repairs, before workers start.

    Existing triggers also maintain these projections when an older Core writes
    the same database. A missing projection or migration marker causes a rebuild;
    ordinary restarts do not scan permanent history again.
    """
    expected_tables = {'workbench_progress_contributions', 'workbench_progress_totals'}
    definitions = _trigger_definitions()
    expected_triggers = set(definitions)
    installed = {row[0]: row[1] for row in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger','view')")}
    changed = any(installed.get(name, '') != statement.replace(
        'CREATE TRIGGER IF NOT EXISTS ', 'CREATE TRIGGER ')
        for name, statement in definitions.items())
    had_projection = (not changed and
        (expected_tables | expected_triggers | {'workbench_progress_refresh'}) <= set(installed))
    connection.execute('SAVEPOINT workbench_progress_aggregates')
    try:
        # An insert-only, empty view dispatches the shared refresh SQL once.
        # Source triggers remain tiny, avoiding reparsing the same complex proof
        # a dozen times whenever a collector opens a new SQLite connection.
        connection.execute("""CREATE VIEW IF NOT EXISTS workbench_progress_refresh AS
            SELECT CAST(NULL AS TEXT) AS account_id WHERE 0""")
        connection.execute(f"""CREATE TABLE IF NOT EXISTS workbench_progress_contributions (
            account_id TEXT NOT NULL, kind TEXT NOT NULL,
            target_id TEXT NOT NULL, mode TEXT NOT NULL,
            qualified INTEGER NOT NULL, recorded_total INTEGER NOT NULL,
            excluded_total INTEGER NOT NULL, discarded INTEGER NOT NULL,
            hover_discarded INTEGER NOT NULL,
            PRIMARY KEY(account_id, kind, target_id, mode)
        ) WITHOUT ROWID""")
        connection.execute("""CREATE TABLE IF NOT EXISTS workbench_progress_totals (
            target_id TEXT NOT NULL, mode TEXT NOT NULL,
            qualified INTEGER NOT NULL DEFAULT 0,
            recorded_total INTEGER NOT NULL DEFAULT 0,
            excluded_total INTEGER NOT NULL DEFAULT 0,
            discarded INTEGER NOT NULL DEFAULT 0,
            hover_discarded INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(target_id, mode)
        ) WITHOUT ROWID""")
        migration = connection.execute(
            'SELECT 1 FROM schema_migrations WHERE version=?', (_VERSION,)).fetchone()
        if not had_projection or migration is None:
            # Drop projection triggers only, so a repaired/missing projection can
            # be rebuilt without double-applying deltas from its former contents.
            for name in expected_triggers:
                connection.execute(f'DROP TRIGGER IF EXISTS {name}')
            connection.execute('DELETE FROM workbench_progress_contributions')
            connection.execute('DELETE FROM workbench_progress_totals')
            connection.execute(f"""INSERT INTO workbench_progress_contributions
                (account_id, kind, target_id, mode, {_COLUMNS}) {_contributions()}""")
            connection.execute(f"""INSERT INTO workbench_progress_totals
                (target_id, mode, {_COLUMNS})
                SELECT target_id, mode, {', '.join('SUM('+c+')' for c in _COLUMNS.split(', '))}
                FROM workbench_progress_contributions GROUP BY target_id, mode""")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))",
                (_VERSION,))
        for statement in definitions.values():
            connection.execute(statement)
    except BaseException:
        connection.execute('ROLLBACK TO workbench_progress_aggregates')
        connection.execute('RELEASE workbench_progress_aggregates')
        raise
    connection.execute('RELEASE workbench_progress_aggregates')


def rebuild_workbench_progress_aggregates(connection: sqlite3.Connection) -> None:
    """Reconcile after a bulk restore, inside that restore's transaction."""
    connection.execute('SAVEPOINT rebuild_workbench_progress')
    try:
        connection.execute('DELETE FROM schema_migrations WHERE version=?', (_VERSION,))
        initialize_workbench_progress_aggregates(connection)
    except BaseException:
        connection.execute('ROLLBACK TO rebuild_workbench_progress')
        connection.execute('RELEASE rebuild_workbench_progress')
        raise
    connection.execute('RELEASE rebuild_workbench_progress')
