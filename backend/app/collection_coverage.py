"""Reconcile visible-list evidence without inventing missing account identities."""
from __future__ import annotations

import json


def known_count(value):
    return value if type(value) is int and value >= 0 else None


def describe_coverage(progress, *, finished=False):
    total = known_count(progress.get('source_total'))
    discovered = known_count(progress.get('discovered'))
    processed = known_count(progress.get('processed'))
    mismatch = (discovered is not None and processed is not None and processed > discovered) or (
        total is not None and any(value is not None and value > total for value in (discovered, processed)))
    remaining = max(0, total - processed) if total is not None and processed is not None else None
    unseen = max(0, total - discovered) if total is not None and discovered is not None else None
    pending = discovered - processed if discovered is not None and processed is not None and processed <= discovered else None
    if mismatch:
        status = 'count_mismatch'
    elif not finished:
        status = 'in_progress'
    elif pending is not None and pending > 0:
        status = 'pending'
    elif remaining is not None and remaining > 0:
        status = 'gap'
    elif pending == 0 and remaining == 0:
        status = 'reconciled'
    else:
        status = 'unverified'
    result = dict(source_total=total, discovered=discovered, processed=processed,
                  remaining_count=remaining, unobserved_count=unseen, pending_count=pending,
                  discovery_finished=finished, status=status,
                  end_reason='visible_list_end' if finished else None)
    return result


def coverage_for_targets(connection, progress_by_target):
    """Batch alongside the existing counter projection; never scan account rows."""
    result = {target: {mode: describe_coverage(progress) for mode, progress in modes.items()}
              for target, modes in progress_by_target.items()}
    ids = list(result)
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        rows = connection.execute(f'SELECT target_id,mode,cursor_json FROM task_checkpoints WHERE target_id IN ({marks})', batch)
        for row in rows:
            try:
                cursor = json.loads(row['cursor_json'])
            except (TypeError, ValueError):
                continue
            # Recovery wraps the original cursor. The first explicit marker is
            # authoritative, including False; old generations cannot override it.
            for _ in range(6):
                if not isinstance(cursor, dict):
                    break
                if 'candidate_spool_complete' in cursor:
                    finished = (cursor.get('candidate_spool_complete') is True
                                and cursor.get('candidate_spool_natural_end') is True
                                and not cursor.get('pending_relation_usernames')
                                and row['mode'] in ('followers', 'following'))
                    progress = progress_by_target.get(row['target_id'], {}).get(row['mode'], {})
                    result[row['target_id']][row['mode']] = describe_coverage(progress, finished=finished)
                    break
                cursor = cursor.get('resume_cursor')
    return result
