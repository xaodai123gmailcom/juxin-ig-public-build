"""Pre-indexed bounded task-detail selection oracle for equivalence tests."""
import sqlite3
from typing import Any
from app.service import _task_dict
from app.platform_scope import task_platform_sql

def legacy_list_tasks_with_details(
    self,
    owner_user_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    platform: str | None = None,
    detail_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return a task page with targets/windows using bounded bulk queries.

    The desktop polls this view frequently.  The previous route fetched the task
    list and then opened two more SQLite connections per task via ``get_task``,
    which became increasingly expensive as history accumulated.
    """

    platform_scope = task_platform_sql(platform, 'task.settings_json')
    with self.database.read() as connection:
        bounded_offset = max(0, int(offset))
        if limit is None and bounded_offset == 0:
            task_rows = connection.execute(
                f"""
                SELECT task.* FROM tasks task WHERE task.owner_user_id=? {platform_scope}
                  AND NOT EXISTS(
                    SELECT 1 FROM task_list_dismissals dismissal
                    WHERE dismissal.task_id=task.id
                      AND dismissal.owner_user_id=task.owner_user_id
                  )
                ORDER BY updated_at DESC, id DESC
                """,
                (owner_user_id,),
            ).fetchall()
        elif limit is None:
            task_rows = connection.execute(
                f"""
                SELECT task.* FROM tasks task WHERE task.owner_user_id=? {platform_scope}
                  AND NOT EXISTS(
                    SELECT 1 FROM task_list_dismissals dismissal
                    WHERE dismissal.task_id=task.id
                      AND dismissal.owner_user_id=task.owner_user_id
                  )
                ORDER BY updated_at DESC, id DESC
                LIMIT -1 OFFSET ?
                """,
                (owner_user_id, bounded_offset),
            ).fetchall()
        else:
            bounded_limit = max(1, min(int(limit), 5001))
            task_rows = connection.execute(
                f"""
                SELECT task.* FROM tasks task
                WHERE task.owner_user_id=? {platform_scope}
                  AND NOT EXISTS(
                    SELECT 1 FROM task_list_dismissals dismissal
                    WHERE dismissal.task_id=task.id
                      AND dismissal.owner_user_id=task.owner_user_id
                  )
                ORDER BY CASE
                    WHEN status IN ('queued', 'running', 'waiting_network',
                                    'paused', 'recoverable') THEN 0 ELSE 1 END,
                         updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (owner_user_id, bounded_limit, bounded_offset),
            ).fetchall()
        if not task_rows:
            return []
        task_ids = [row["id"] for row in task_rows]
        placeholders = ",".join("?" for _ in task_ids)
        detail_filter = ""
        detail_parameters: list[Any] = [*task_ids]
        if detail_limit is not None:
            detail_filter = "WHERE detail_rank<=?"
            detail_budget = max(1, min(int(detail_limit), 50001))
            # Share one global detail budget fairly across the outer page.
            # Workbench callers keep detail_budget >= len(task_ids), so this
            # yields at least one row per task without multiplying the budget.
            detail_parameters.append(max(1, detail_budget // len(task_ids)))
        # Rank identity/sort fields first. Only the bounded selected rows
        # may load profile JSON or evaluate recovery payloads; lifetime
        # history must not enter the window sorter's temporary storage.
        target_rows = connection.execute(
            f"""
            WITH ranked_targets AS (
                SELECT target.id, target.task_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY target.task_id
                           ORDER BY CASE
                               WHEN target.status IN ('running', 'waiting_network')
                               THEN 0
                               WHEN target.status IN ('failed', 'recoverable') THEN 1
                               WHEN target.status IN ('pending', 'paused') THEN 2
                               ELSE 3 END,
                               target.queue_order, target.id
                       ) AS detail_rank
                FROM task_targets target
                WHERE target.task_id IN ({placeholders}) AND target.username_norm NOT GLOB 'fb:*'
            )
            SELECT target.*,
                   dismissal.target_id IS NOT NULL AS collection_list_dismissed,
                   (SELECT 'automatic' FROM task_automatic_completions automatic WHERE automatic.target_id=target.id) AS completion_policy,
                   recheck.mode AS source_recheck_mode,
                   recheck.state AS source_recheck_state,
                   recheck.completed_at AS source_recheck_completed_at,
                       (
                       EXISTS(
                         SELECT 1 FROM task_target_recovery_controls recovery
                         WHERE recovery.owner_user_id=task.owner_user_id
                           AND recovery.target_id=target.id
                           AND recovery.state IN ('pending', 'dismissed')
                       )
                       OR EXISTS(
                         SELECT 1 FROM split_candidates failure
                         WHERE failure.owner_user_id=task.owner_user_id
                           AND failure.source_target_id=target.id
                           AND NOT (
                             (
                               failure.queue_state IN ('queued', 'claimed')
                               AND failure.queued_target_id IS NOT NULL
                               AND failure.queued_target_id=target.id
                             )
                             OR (
                               failure.candidate_kind='manual'
                               AND failure.queue_state='queued'
                               AND failure.queued_target_id IS NULL
                               AND COALESCE(
                                 json_extract(task.settings_json, '$.live_queue_enabled'), 0
                               )=0
                             )
                           )
                       )) AS manual_recovery_required,
                   task.updated_at AS parent_updated_at,
                   ranked_targets.detail_rank
            FROM ranked_targets
            JOIN task_targets target ON target.id=ranked_targets.id
            JOIN tasks task ON task.id=target.task_id
            LEFT JOIN task_target_list_dismissals dismissal
              ON dismissal.target_id=target.id AND dismissal.owner_user_id=task.owner_user_id
            LEFT JOIN task_source_rechecks recheck ON recheck.target_id=target.id
            {detail_filter}
            ORDER BY parent_updated_at DESC, target.queue_order, target.id
            """,
            tuple(detail_parameters),
        ).fetchall()
        target_totals: dict[str, int] = {}
        window_totals: dict[str, int] = {}
        if detail_limit is not None:
            target_totals = {
                row["task_id"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT task_id, COUNT(*) AS count
                    FROM task_targets WHERE task_id IN ({placeholders}) AND username_norm NOT GLOB 'fb:*'
                    GROUP BY task_id
                    """,
                    tuple(task_ids),
                ).fetchall()
            }
            window_totals = {
                row["task_id"]: int(row["count"])
                for row in connection.execute(
                    f"""
                    SELECT task_id, COUNT(*) AS count
                    FROM task_windows WHERE task_id IN ({placeholders})
                    GROUP BY task_id
                    """,
                    tuple(task_ids),
                ).fetchall()
            }
        mode_progress = self._mode_progress_for_targets(
            connection, (row["id"] for row in target_rows)
        )
        from app.collection_coverage import coverage_for_targets
        mode_coverage = coverage_for_targets(connection, mode_progress)
        window_rows = connection.execute(
            f"""
            WITH ranked_windows AS (
                SELECT window.*,
                       task.updated_at AS parent_updated_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY window.task_id
                           ORDER BY CASE
                               WHEN window.status IN ('running', 'waiting_network')
                               THEN 0
                               WHEN window.status='paused' THEN 1
                               WHEN window.status='selected' THEN 2
                               ELSE 3 END,
                               window.queue_order, window.profile_id
                       ) AS detail_rank
                FROM task_windows window
                JOIN tasks task ON task.id=window.task_id
                WHERE task.id IN ({placeholders})
            )
            SELECT * FROM ranked_windows
            {detail_filter}
            ORDER BY parent_updated_at DESC, queue_order, profile_id
            """,
            tuple(detail_parameters),
        ).fetchall()
    targets_by_task: dict[str, list[sqlite3.Row]] = {}
    for row in target_rows:
        targets_by_task.setdefault(row["task_id"], []).append(row)
    windows_by_task: dict[str, list[sqlite3.Row]] = {}
    for row in window_rows:
        windows_by_task.setdefault(row["task_id"], []).append(row)
    result: list[dict[str, Any]] = []
    for row in task_rows:
        task = _task_dict(
            row,
            targets_by_task.get(row["id"], ()),
            windows_by_task.get(row["id"], ()),
            mode_progress,
            mode_coverage,
        )
        task["targets_truncated"] = (
            target_totals.get(row["id"], 0)
            > len(targets_by_task.get(row["id"], ()))
        )
        task["windows_truncated"] = (
            window_totals.get(row["id"], 0)
            > len(windows_by_task.get(row["id"], ()))
        )
        result.append(task)
    return result

