"""Permanent source-admission counts and execution fences, separate from global dedup."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .errors import ConflictError


def was_split_executed(c, owner: str, username: str) -> bool:
    return bool(c.execute("""SELECT 1 FROM split_admission_totals
        WHERE owner_user_id=? AND username_norm=? AND has_executed=1
        UNION ALL SELECT 1 FROM split_completed_targets
        WHERE owner_user_id=? AND username_norm=?
        UNION ALL SELECT 1 FROM split_candidate_history
        WHERE owner_user_id=? AND username_norm=? AND source_status='completed'
        UNION ALL SELECT 1 FROM task_targets t JOIN tasks task ON task.id=t.task_id
        WHERE task.owner_user_id=? AND t.username_norm=? AND t.status!='pending'
        UNION ALL SELECT 1 FROM task_target_recovery_controls
        WHERE owner_user_id=? AND username_norm=? LIMIT 1""",
        (owner, username, owner, username, owner, username, owner, username, owner, username)).fetchone())


def guard_new_direct_source(c, owner: str, username: str, *, allow_completed: bool = False) -> None:
    if was_split_executed(c, owner, username) and not allow_completed:
        raise ConflictError(f'@{username} 已有分裂记录，不能再次加入分裂号',
                            details={'reason': 'split_already_executed', 'username': username})
    if c.execute("""SELECT 1 FROM split_candidates WHERE owner_user_id=? AND username_norm=?
        UNION ALL SELECT 1 FROM task_targets t JOIN tasks task ON task.id=t.task_id
        WHERE task.owner_user_id=? AND t.username_norm=? AND t.status!='completed'
          AND COALESCE(t.current_stage,'')!='deleted_archived' LIMIT 1""",
        (owner, username, owner, username)).fetchone():
        raise ConflictError(f'@{username} 已在等待或任务列表中，不能重复加入',
                            details={'reason': 'split_already_queued', 'username': username})


def record_split_admission(c, owner: str, username: str, now: str) -> None:
    """Call once inside the successful NEW admission transaction, before its insert.

    Never call from claims, available->queued promotion or recovery/retry paths.
    Existing lower-bound histories remain lower bounds after a new admission.
    """
    c.execute("""INSERT INTO split_admission_totals(
        owner_user_id,username_norm,successful_adds,history_complete,has_executed,updated_at)
        VALUES(?,?,1,1,0,?) ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET
        successful_adds=successful_adds+1,updated_at=excluded.updated_at""", (owner, username, now))


def backfill_split_admissions(c, owner: str | None = None) -> None:
    """Recover provable lower bounds; diagnostic logs may have already been pruned.

    Distinct completed generations cannot be retries of a successful generation.
    Saved/explicit operator-add events prove additions even if their rows were
    deleted. Taking MAX avoids counting one admission through multiple sources.
    Legacy counts remain explicitly incomplete, never claimed as exact totals.
    """
    where = ' WHERE owner_user_id=?' if owner else ''
    args = (owner,) if owner else ()
    seen = defaultdict(lambda: {'events': 0, 'completed': set(), 'present': False, 'executed': False})
    for row in c.execute('SELECT * FROM split_candidates' + where, args):
        seen[(row['owner_user_id'], row['username_norm'])]['present'] = True
    linked = set()
    for row in c.execute('SELECT * FROM split_candidate_history' + where, args):
        key = (row['owner_user_id'], row['username_norm'])
        value = seen[key]; value['present'] = True
        if row['source_status'] == 'completed':
            generation = row['source_target_id'] or 'history:' + row['id']
            value['completed'].add(generation); value['executed'] = True
            if row['source_target_id']: linked.add((row['owner_user_id'], row['source_target_id']))
    for row in c.execute('SELECT * FROM split_completed_targets' + where, args):
        value = seen[(row['owner_user_id'], row['username_norm'])]
        value['completed'].add(row['target_id']); value['executed'] = True; value['present'] = True
    task_where = ' WHERE task.owner_user_id=?' if owner else ''
    for row in c.execute('SELECT task.owner_user_id,t.* FROM task_targets t JOIN tasks task ON task.id=t.task_id' + task_where, args):
        value = seen[(row['owner_user_id'], row['username_norm'])]; value['present'] = True
        if row['status'] != 'pending': value['executed'] = True
        if row['status'] == 'completed' and (row['owner_user_id'], row['id']) not in linked:
            value['completed'].add(row['id'])
    for row in c.execute('SELECT * FROM task_target_recovery_controls' + where, args):
        value = seen[(row['owner_user_id'], row['username_norm'])]
        value['present'] = True; value['executed'] = True
    clause = " WHERE event_type IN ('split_candidate.saved','split_candidate.requeued')"
    if owner: clause += ' AND owner_user_id=?'
    for row in c.execute('SELECT owner_user_id,entity_id,event_type,payload_json FROM event_log' + clause, args):
        try: payload = json.loads(row['payload_json'])
        except (ValueError, TypeError): payload = {}
        if row['event_type'] == 'split_candidate.requeued' and payload.get('reason') != 'operator_override':
            continue  # failure/return actions are not new additions
        username = row['entity_id'].lstrip('@').lower()
        if not re.fullmatch(r'[a-z0-9._]{1,30}', username): continue
        seen[(row['owner_user_id'], username)]['events'] += 1
    for (owner_id, username), value in seen.items():
        lower = max(value['events'], len(value['completed']), int(value['present']))
        c.execute("""INSERT INTO split_admission_totals(
            owner_user_id,username_norm,successful_adds,history_complete,has_executed,updated_at)
            VALUES(?,?,?,0,?,datetime('now')) ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET
            successful_adds=CASE WHEN history_complete=0 THEN MAX(successful_adds,excluded.successful_adds) ELSE successful_adds END,
            has_executed=MAX(has_executed,excluded.has_executed)""",
            (owner_id, username, lower, int(value['executed'])))


def backfill_split_completions(c, owner: str | None = None) -> None:
    """Preserve the best retained completion evidence; never invent today's date."""
    clause = ' AND task.owner_user_id=?' if owner else ''
    c.execute("""INSERT OR IGNORE INTO split_completed_targets
        (target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at)
        SELECT t.id,task.owner_user_id,t.username_norm,t.username_display,t.task_id,
          COALESCE((SELECT h.source_window_id FROM split_candidate_history h
            WHERE h.owner_user_id=task.owner_user_id AND h.source_target_id=t.id AND h.source_status='completed'
            ORDER BY julianday(h.completed_at),h.id LIMIT 1),t.current_window_id,t.preferred_window_id),
          COALESCE((SELECT h.completed_at FROM split_candidate_history h
            WHERE h.owner_user_id=task.owner_user_id AND h.source_target_id=t.id AND h.source_status='completed'
            ORDER BY julianday(h.completed_at),h.id LIMIT 1),t.updated_at)
        FROM task_targets t JOIN tasks task ON task.id=t.task_id WHERE t.status='completed'""" + clause,
        (owner,) if owner else ())
    from .split_completion_details import backfill_completion_details
    backfill_completion_details(c, owner)
