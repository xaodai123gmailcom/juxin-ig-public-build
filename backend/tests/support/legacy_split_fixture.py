"""Build pre-r55 database generations for migration/isolation regression tests.

These fixtures insert historical rows directly. They deliberately do not call or
patch the public admission API: r55 must reject a fresh duplicate while still
handling duplicate generations already present in an older user's database.
"""
from __future__ import annotations

import json
import uuid

from app.service import isoformat, normalize_instagram_username, validate_task_settings


def create_legacy_task(service, owner_user_id, *, name, modes, targets, settings,
                       window_ids=None, assignment_mode="sequential"):
    task_id, now = str(uuid.uuid4()), isoformat()
    with service.database.write() as connection:
        connection.execute(
            "INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,"
            "assignment_mode,created_at,updated_at) VALUES(?,?,?,'draft',?,?,?,?,?)",
            (task_id, owner_user_id, name, json.dumps(modes),
             json.dumps(validate_task_settings(settings)), assignment_mode, now, now),
        )
        for order, username in enumerate(targets, 1):
            normalized, display = normalize_instagram_username(username)
            connection.execute(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,"
                "queue_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), task_id, normalized, display, order, now, now),
            )
        for order, profile_id in enumerate(window_ids or [], 1):
            connection.execute(
                "INSERT INTO task_windows(task_id,profile_id,queue_order) VALUES(?,?,?)",
                (task_id, profile_id, order),
            )
    return service.get_task(owner_user_id, task_id)


def insert_legacy_waiting_generation(service, owner_user_id, username, windows):
    """An independently queued generation left by the old force-recollect UI."""
    candidate_id, now = str(uuid.uuid4()), isoformat()
    normalized, display = normalize_instagram_username(username)
    with service.database.write() as connection:
        connection.execute(
            "INSERT INTO split_candidates(id,owner_user_id,username_norm,username_display,"
            "candidate_kind,queue_state,queued_at,created_at,updated_at) "
            "VALUES(?,?,?,?,'manual','queued',?,?,?)",
            (candidate_id, owner_user_id, normalized, display, now, now, now),
        )
        for order, profile_id in enumerate(windows, 1):
            connection.execute(
                "INSERT INTO split_candidate_window_affinity(candidate_id,profile_id,queue_order) "
                "VALUES(?,?,?)", (candidate_id, profile_id, order),
            )
    return next(row for row in service.list_split_candidates(owner_user_id)
                if row['id'] == candidate_id)
