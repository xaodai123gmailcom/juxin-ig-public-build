"""Immutable source metrics and per-generation completion progress snapshots."""
from __future__ import annotations

import json

from .collection_coverage import coverage_for_targets, describe_coverage


FIELDS = ('followers', 'following', 'posts', 'processed_count', 'new_count', 'duplicate_count')


def count(value):
    return value if type(value) is int and value >= 0 else None


def object_json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def source_snapshot(profile, username):
    if not isinstance(profile, dict):
        return {}
    # The history row supplies identity for old snapshots without a username;
    # an explicit conflicting profile identity cannot prove source metrics.
    if profile.get('username') and str(profile['username']).strip().lstrip('@').casefold() != username.casefold():
        return {}
    return profile


def report_details(raw, profile=None, username=''):
    """Missing legacy evidence is unknown, including private-account counts."""
    details = object_json(raw)
    if details.get('version') in (1, 2):
        result = {field: count(details.get(field)) for field in FIELDS}
        result['mode_coverage'] = {}
        coverage = details.get('mode_coverage')
        if details.get('version') == 2 and isinstance(coverage, dict):
            for mode in ('followers', 'following'):
                value = coverage.get(mode)
                if isinstance(value, dict):
                    result['mode_coverage'][mode] = describe_coverage(value,
                        finished=value.get('discovery_finished') is True and value.get('end_reason') == 'visible_list_end')
        return result
    profile = source_snapshot(profile, username)
    return {**{field: count(profile.get(field)) if field in ('followers', 'following', 'posts') else None
               for field in FIELDS}, 'mode_coverage': {}}


def completion_details(connection, owner, target_id):
    """Use the same per-mode projection as r46, never task-wide result totals."""
    from .service import CoreService

    target = connection.execute("""SELECT target.source_profile_json,target.username_norm,task.modes_json
        FROM task_targets target JOIN tasks task ON task.id=target.task_id
        WHERE target.id=? AND task.owner_user_id=?""", (target_id, owner)).fetchone()
    source = source_snapshot(object_json(target['source_profile_json']), target['username_norm']) if target else {}
    if not source:
        history = connection.execute("""SELECT profile_json,username_norm FROM split_candidate_history
            WHERE owner_user_id=? AND source_target_id=? AND source_status='completed'
              AND username_norm=(SELECT username_norm FROM split_completed_targets
                  WHERE owner_user_id=? AND target_id=?)
            ORDER BY julianday(completed_at),id LIMIT 1""", (owner, target_id, owner, target_id)).fetchone()
        source = source_snapshot(object_json(history['profile_json']), history['username_norm']) if history else {}
    details = {'version': 2, **{field: count(source.get(field)) for field in ('followers', 'following', 'posts')}}
    progress = CoreService._mode_progress_for_targets(connection, [target_id]).get(target_id, {}) if target else {}
    try:
        modes = json.loads(target['modes_json']) if target else []
    except (TypeError, ValueError):
        modes = []
    modes = list(dict.fromkeys(mode for mode in modes if isinstance(mode, str))) if isinstance(modes, list) else []
    coverage = coverage_for_targets(connection, {target_id: progress}).get(target_id, {}) if target else {}
    details['mode_coverage'] = {mode: coverage[mode] for mode in modes
                                if mode in coverage and mode in ('followers', 'following')}
    for field in ('followers', 'following'):
        if details[field] is None:
            details[field] = count(progress.get(field, {}).get('source_total'))
    for field, metric in (('processed_count', 'processed'), ('new_count', 'saved'),
                          ('duplicate_count', 'skipped_global_duplicates')):
        values = [count(progress.get(mode, {}).get(metric)) for mode in modes]
        details[field] = sum(values) if values and all(value is not None for value in values) else None
    return details


def save_completion_details(connection, owner, target_id):
    details = completion_details(connection, owner, target_id)
    connection.execute("""UPDATE split_completed_targets SET completion_details_json=?
        WHERE target_id=? AND owner_user_id=? AND completion_details_json='{}'""",
        (json.dumps(details, separators=(',', ':')), target_id, owner))


def backfill_completion_details(connection, owner=None):
    """Freeze only retained per-target evidence on upgrade or legacy restore."""
    clause = ' AND owner_user_id=?' if owner else ''
    rows = connection.execute("SELECT target_id,owner_user_id FROM split_completed_targets "
        "WHERE completion_details_json='{}'" + clause, (owner,) if owner else ()).fetchall()
    for row in rows:
        save_completion_details(connection, row['owner_user_id'], row['target_id'])
