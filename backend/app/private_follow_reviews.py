"""Durable audit evidence for confirmed follow actions on known private accounts."""
from __future__ import annotations

import json

from .split_completion_details import count, object_json


def private_candidate_snapshot(connection, owner, username):
    row = connection.execute("""SELECT candidate.profile_json FROM workbench_candidates candidate
        JOIN instagram_username_aliases alias ON alias.account_id=candidate.account_id
        WHERE candidate.owner_user_id=? AND alias.username_norm=? AND candidate.visibility='private'
        ORDER BY candidate.created_at,candidate.id LIMIT 1""", (owner, username)).fetchone()
    if row is None:
        return None
    profile = object_json(row['profile_json'])
    # The alias joins the exact persisted account, including a later name change.
    # Keep only user-facing snapshot metadata; execution identity has its own field.
    result = {'username': username, 'visibility': 'private',
              **{field: count(profile.get(field)) for field in ('followers', 'following', 'posts')}}
    for field in ('display_name', 'full_name', 'avatar_url', 'bio'):
        if isinstance(profile.get(field), str):
            result[field] = profile[field]
    return result


def capture_private_follow_completions(connection, owner=None, attempt_id=None, username=None):
    """Capture once, or backfill only when retained evidence proves the action.

    already_done is a dispatch fence, not a newly executed follow. Private
    identity comes from the saved start snapshot or the owner's retained profile,
    never from the operation name or today's Instagram account.
    """
    where, params = [], []
    if owner is not None:
        where.append('success.owner_user_id=?'); params.append(owner)
    if attempt_id is not None:
        where.append('success.attempt_id=?'); params.append(attempt_id)
    if username is not None:
        where.append('success.username_norm=?'); params.append(username)
    rows = connection.execute("""SELECT success.*,attempt.details_json,campaign.profile_id
        FROM action_success_ledger success
        JOIN action_attempts attempt ON attempt.id=success.attempt_id
            AND attempt.campaign_id=success.campaign_id AND attempt.target_id=success.target_id
        JOIN action_campaigns campaign ON campaign.id=success.campaign_id
            AND campaign.owner_user_id=success.owner_user_id
        JOIN action_targets target ON target.id=success.target_id
            AND target.campaign_id=success.campaign_id AND target.username_norm=success.username_norm
        WHERE success.operation='follow' AND campaign.operation='follow' AND attempt.status='confirmed'
          AND NOT EXISTS(SELECT 1 FROM private_follow_completions saved
              WHERE saved.owner_user_id=success.owner_user_id AND saved.username_norm=success.username_norm)
        """ + (' AND ' + ' AND '.join(where) if where else ''), params).fetchall()
    for row in rows:
        details = object_json(row['details_json'])
        profile = details.get('private_follow_profile')
        if not isinstance(profile, dict) or profile.get('visibility') != 'private' or profile.get('username') != row['username_norm']:
            profile = private_candidate_snapshot(connection, row['owner_user_id'], row['username_norm'])
        if profile is None:
            continue
        executor = details.get('executor') if isinstance(details.get('executor'), dict) else {}
        executor = {key: value for key, value in executor.items()
                    if key in ('profile_id', 'window_name', 'username', 'instagram_user_id') and isinstance(value, str)}
        confirmation = details.get('confirmation') if isinstance(details.get('confirmation'), str) else ''
        connection.execute("""INSERT OR IGNORE INTO private_follow_completions(
            owner_user_id,username_norm,username_display,campaign_id,target_id,attempt_id,
            completed_at,source_window_id,profile_json,executor_json,confirmation)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (row['owner_user_id'], row['username_norm'],
            row['username_display'], row['campaign_id'], row['target_id'], row['attempt_id'],
            row['completed_at'], executor.get('profile_id') or row['profile_id'],
            json.dumps(profile, ensure_ascii=False), json.dumps(executor, ensure_ascii=False), confirmation))


def follow_state(confirmation):
    from .playwright_worker import _FOLLOW_CONFIRMED_MARKERS, _PRIVATE_RELATIONSHIP_STATE_MARKERS
    label = ' '.join(str(confirmation).casefold().split())
    if label in _PRIVATE_RELATIONSHIP_STATE_MARKERS:
        return 'requested'
    if label in _FOLLOW_CONFIRMED_MARKERS:
        return 'following'
    return 'unknown'
