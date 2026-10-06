from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator


EVENT_LOG_MAX_PER_OWNER = 50_000
EVENT_LOG_RETENTION_DAYS = 90
EVENT_LOG_MAINTENANCE_INTERVAL_SECONDS = 60 * 60
TECHNICAL_SPOOL_RETENTION_DAYS = 30


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_review_decisions (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    review_kind TEXT NOT NULL CHECK(review_kind IN ('split', 'private_follow')),
    record_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('passed', 'failed')),
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, review_kind, record_id)
);

CREATE TABLE IF NOT EXISTS app_users (
    id TEXT PRIMARY KEY,
    username_norm TEXT NOT NULL UNIQUE,
    username_display TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    remember_login INTEGER NOT NULL DEFAULT 0 CHECK (remember_login IN (0, 1)),
    auto_login INTEGER NOT NULL DEFAULT 0 CHECK (auto_login IN (0, 1)),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    modes_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    assignment_mode TEXT NOT NULL DEFAULT 'sequential',
    version INTEGER NOT NULL DEFAULT 1,
    restart_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_updated ON tasks(owner_user_id, updated_at DESC);
-- A capped workbench page must seek its existing live-first order rather than
-- sort the owner's entire retained task history before applying LIMIT.
CREATE INDEX IF NOT EXISTS idx_tasks_owner_snapshot_priority
ON tasks(owner_user_id,
    CASE WHEN status IN ('queued', 'running', 'waiting_network', 'paused', 'recoverable')
         THEN 0 ELSE 1 END,
    updated_at DESC, id DESC);

-- User-facing removal from the live task list.  The underlying task, results,
-- review evidence and global identity ledger remain intact for history/dedupe.
CREATE TABLE IF NOT EXISTS task_list_dismissals (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_list_dismissals_owner
ON task_list_dismissals(owner_user_id, dismissed_at DESC);

CREATE TABLE IF NOT EXISTS task_targets (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    preferred_window_id TEXT,
    current_window_id TEXT,
    allowed_window_ids_json TEXT NOT NULL DEFAULT '[]',
    current_stage TEXT,
    last_success_at TEXT,
    last_error TEXT,
    source_profile_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, username_norm),
    UNIQUE(task_id, queue_order)
);
CREATE INDEX IF NOT EXISTS idx_targets_task_queue ON task_targets(task_id, queue_order);
-- The same per-task priority as the workbench detail page. Only the requested
-- IDs plus one truncation sentinel are visited; original target rows stay intact.
CREATE INDEX IF NOT EXISTS idx_targets_task_snapshot_priority
ON task_targets(task_id,
    CASE WHEN status IN ('running', 'waiting_network') THEN 0
         WHEN status IN ('failed', 'recoverable') THEN 1
         WHEN status IN ('pending', 'paused') THEN 2 ELSE 3 END,
    queue_order, id)
WHERE username_norm NOT GLOB 'fb:*';
-- Heartbeats select a small recent-change page independently from all occupied
-- window targets. Neither branch should sort or scan lifetime task history.
CREATE INDEX IF NOT EXISTS idx_targets_task_updated
ON task_targets(task_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_targets_task_occupied
ON task_targets(task_id, id) WHERE current_window_id IS NOT NULL;
-- Archived targets retain their historical window id. Runtime supplies any
-- terminal target still being cleaned up; this index covers newly claimed/live
-- bindings without scanning every completed source from a long-running task.
CREATE INDEX IF NOT EXISTS idx_targets_task_live_occupied
ON task_targets(task_id, id)
WHERE current_window_id IS NOT NULL AND status NOT IN ('completed', 'failed', 'stopped');

-- Display-only dismissal of a single completed collection card. This never
-- changes execution state, saved results, completion history or window leases.
CREATE TABLE IF NOT EXISTS task_target_list_dismissals (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_target_list_dismissals_owner
ON task_target_list_dismissals(owner_user_id, dismissed_at DESC);

CREATE TABLE IF NOT EXISTS task_windows (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'selected',
    PRIMARY KEY(task_id, profile_id),
    UNIQUE(task_id, queue_order)
);
CREATE INDEX IF NOT EXISTS idx_task_windows_task_queue ON task_windows(task_id, queue_order);

-- Following/DM monitor. Detailed results are replaced by each completed run;
-- only baselines, daily aggregate counts and compact run logs persist.
CREATE TABLE IF NOT EXISTS follow_monitor_accounts (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    instagram_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    previous_generation INTEGER,
    previous_following_count INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    total_added_count INTEGER NOT NULL DEFAULT 0,
    last_added_count INTEGER NOT NULL DEFAULT 0,
    total_unfollow_count INTEGER NOT NULL DEFAULT 0,
    last_unfollow_count INTEGER NOT NULL DEFAULT 0,
    total_dm_count INTEGER NOT NULL DEFAULT 0,
    last_dm_count INTEGER NOT NULL DEFAULT 0,
    baseline_verified INTEGER NOT NULL DEFAULT 1,
    last_status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    checked_at TEXT,
    PRIMARY KEY(owner_user_id, profile_id)
);
CREATE TABLE IF NOT EXISTS follow_monitor_members (
    owner_user_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    username TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id, generation, username)
);
CREATE INDEX IF NOT EXISTS idx_follow_monitor_members_generation
ON follow_monitor_members(owner_user_id, profile_id, generation);
CREATE TABLE IF NOT EXISTS follow_monitor_latest_follow (
    owner_user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    username TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id, username)
);
CREATE TABLE IF NOT EXISTS follow_monitor_latest_unfollow (
    owner_user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    username TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id, username)
);
CREATE TABLE IF NOT EXISTS follow_monitor_latest_dm (
    owner_user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    sender TEXT,
    preview TEXT NOT NULL,
    message_at TEXT,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id, thread_id)
);
CREATE TABLE IF NOT EXISTS follow_monitor_daily_counts (
    owner_user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    added_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_user_id, day)
);
CREATE TABLE IF NOT EXISTS follow_monitor_runs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    profile_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    unfollow_count INTEGER NOT NULL DEFAULT 0,
    dm_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_follow_monitor_runs_owner
ON follow_monitor_runs(owner_user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS follow_monitor_seen (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    instagram_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id, instagram_user_id, username)
);
CREATE TABLE IF NOT EXISTS follow_monitor_rounds (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    homepage_count INTEGER,
    previous_homepage_count INTEGER,
    actual_count INTEGER NOT NULL,
    previous_actual_count INTEGER,
    first_read_count INTEGER NOT NULL,
    second_read_count INTEGER,
    second_homepage_count INTEGER,
    added_count INTEGER NOT NULL,
    repeat_count INTEGER NOT NULL,
    unfollow_count INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id)
);
CREATE TABLE IF NOT EXISTS follow_monitor_dm_accounts (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    instagram_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    total_dm_count INTEGER NOT NULL DEFAULT 0,
    last_dm_count INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    last_status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    checked_at TEXT,
    PRIMARY KEY(owner_user_id, profile_id)
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    stage TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    counters_json TEXT NOT NULL,
    recoverable INTEGER NOT NULL DEFAULT 1 CHECK (recoverable IN (0, 1)),
    updated_at TEXT NOT NULL,
    UNIQUE(target_id, mode)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON task_checkpoints(task_id, updated_at DESC);

-- Durable spool between a visible Instagram relationship list and profile
-- screening.  Collection workers append small batches while the source dialog is
-- still open; a network outage therefore cannot discard every username already
-- observed.  Processing state is deliberately terminal-only: a crash before a
-- terminal update leaves the row PENDING and safe to retry, while a result that was
-- committed first is reconciled from the global dedupe tables on resume.
CREATE TABLE IF NOT EXISTS task_mode_candidates (
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('followers', 'following', 'post_likers')),
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    discovery_order INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'recorded', 'deduped')),
    discovered_at TEXT NOT NULL,
    processed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(target_id, mode, username_norm),
    UNIQUE(target_id, mode, discovery_order)
);
CREATE INDEX IF NOT EXISTS idx_task_mode_candidates_pending
ON task_mode_candidates(target_id, mode, state, discovery_order);
-- Append reconciliation is bounded by username, not discovery order. Without
-- this index SQLite can scan every pending row before applying a small IN list,
-- turning repeated batches into quadratic work as the source queue grows.
CREATE INDEX IF NOT EXISTS idx_task_mode_candidates_state_username
ON task_mode_candidates(target_id, mode, state, username_norm);
CREATE INDEX IF NOT EXISTS idx_task_mode_candidates_unfinished
ON task_mode_candidates(target_id) WHERE state='pending';

-- Explicit source-recheck generations preserve the original target and all data.
-- Prepared requests are durable; only an owned safe point activates a rewind.
CREATE TABLE IF NOT EXISTS task_source_rechecks (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('followers', 'following')),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'active', 'resuming', 'completed')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    request_profile_id TEXT,
    request_lease_token TEXT
);

CREATE TRIGGER IF NOT EXISTS trg_pending_source_recheck_blocks_completion
BEFORE UPDATE OF status ON task_targets
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_source_rechecks WHERE target_id=NEW.id AND state='prepared'
)
BEGIN
    SELECT RAISE(ABORT, 'pending source recheck must settle before completion');
END;

CREATE TABLE IF NOT EXISTS task_parent_reel_decisions (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    reel_key TEXT NOT NULL,
    like_selected INTEGER NOT NULL CHECK(like_selected IN (0,1)),
    profile_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id,target_id,reel_key)
);

CREATE TABLE IF NOT EXISTS task_automatic_completions (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'cleanup_pending' CHECK(state IN ('cleanup_pending','dismissed')),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automatic_completion_lease
ON task_automatic_completions(profile_id,lease_token,state);

-- Completion and late producer writes share the same SQLite transaction fence.
-- Neither a stale checkpoint nor a caller bypassing CoreService can publish a
-- completed task while discovered profiles still need processing.
CREATE TRIGGER IF NOT EXISTS trg_candidate_target_completion_fence
BEFORE UPDATE OF status ON task_targets
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_mode_candidates WHERE target_id=NEW.id AND state='pending'
)
BEGIN
    SELECT RAISE(ABORT, 'target has pending candidates');
END;
-- Source identities seen but not yet hover-confirmed are deliberately outside
-- the candidate spool. They still prevent false natural completion, including
-- wrapped recovery cursors and direct SQL callers.
CREATE TRIGGER IF NOT EXISTS trg_relation_obligation_target_completion_fence
BEFORE UPDATE OF status ON task_targets
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_checkpoints checkpoint, json_tree(checkpoint.cursor_json) obligation
    WHERE checkpoint.task_id=NEW.task_id AND checkpoint.target_id=NEW.id AND checkpoint.mode IN ('followers','following')
      AND obligation.key='pending_relation_usernames' AND obligation.type='array'
      AND json_array_length(obligation.value)>0
)
BEGIN
    SELECT RAISE(ABORT, 'target has unconfirmed list identities');
END;
CREATE TRIGGER IF NOT EXISTS trg_relation_obligation_task_completion_fence
BEFORE UPDATE OF status ON tasks
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_checkpoints checkpoint, json_tree(checkpoint.cursor_json) obligation
    WHERE checkpoint.task_id=NEW.id AND checkpoint.mode IN ('followers','following')
      AND obligation.key='pending_relation_usernames' AND obligation.type='array'
      AND json_array_length(obligation.value)>0
)
BEGIN
    SELECT RAISE(ABORT, 'task has unconfirmed list identities');
END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_task_completion_fence
BEFORE UPDATE OF status ON tasks
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_targets target JOIN task_mode_candidates candidate
      ON candidate.target_id=target.id
    WHERE target.task_id=NEW.id AND candidate.state='pending'
)
BEGIN
    SELECT RAISE(ABORT, 'task has pending candidates');
END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_closed_target_insert_fence
BEFORE INSERT ON task_mode_candidates
WHEN NEW.state='pending'
 AND EXISTS(
    SELECT 1 FROM task_targets target JOIN tasks task ON task.id=target.task_id
    WHERE target.id=NEW.target_id AND (
        target.status='completed' OR task.status='completed'
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'closed target cannot receive pending candidates');
END;
CREATE TRIGGER IF NOT EXISTS trg_candidate_closed_target_reopen_fence
BEFORE UPDATE OF state ON task_mode_candidates
WHEN NEW.state='pending' AND OLD.state!='pending' AND EXISTS(
    SELECT 1 FROM task_targets target JOIN tasks task ON task.id=target.task_id
    WHERE target.id=NEW.target_id AND (
        target.status='completed' OR task.status='completed'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'closed target cannot reopen candidates');
END;

-- Constant-time progress for long relationship lists.  The source spool can
-- contain hundreds of thousands of rows, so the hot 100-name append path must
-- not aggregate the whole target after every flush.  Triggers keep this small
-- projection exact; initialize() rebuilds it once after an upgrade or an
-- unclean shutdown before workers are allowed to start.
CREATE TABLE IF NOT EXISTS task_mode_candidate_counters (
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('followers', 'following', 'post_likers')),
    total INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    recorded INTEGER NOT NULL DEFAULT 0,
    deduped INTEGER NOT NULL DEFAULT 0,
    next_discovery_order INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(target_id, mode)
);

CREATE TRIGGER IF NOT EXISTS trg_task_mode_candidate_counter_insert
AFTER INSERT ON task_mode_candidates
BEGIN
    INSERT INTO task_mode_candidate_counters(
        target_id, mode, total, pending, recorded, deduped,
        next_discovery_order
    ) VALUES(
        NEW.target_id,
        NEW.mode,
        1,
        CASE WHEN NEW.state='pending' THEN 1 ELSE 0 END,
        CASE WHEN NEW.state='recorded' THEN 1 ELSE 0 END,
        CASE WHEN NEW.state='deduped' THEN 1 ELSE 0 END,
        NEW.discovery_order + 1
    )
    ON CONFLICT(target_id, mode) DO UPDATE SET
        total=total + 1,
        pending=pending + CASE WHEN NEW.state='pending' THEN 1 ELSE 0 END,
        recorded=recorded + CASE WHEN NEW.state='recorded' THEN 1 ELSE 0 END,
        deduped=deduped + CASE WHEN NEW.state='deduped' THEN 1 ELSE 0 END,
        next_discovery_order=MAX(
            next_discovery_order,
            NEW.discovery_order + 1
        );
END;

CREATE TRIGGER IF NOT EXISTS trg_task_mode_candidate_counter_state
AFTER UPDATE OF state ON task_mode_candidates
WHEN OLD.state <> NEW.state
BEGIN
    UPDATE task_mode_candidate_counters
    SET pending=pending
            - CASE WHEN OLD.state='pending' THEN 1 ELSE 0 END
            + CASE WHEN NEW.state='pending' THEN 1 ELSE 0 END,
        recorded=recorded
            - CASE WHEN OLD.state='recorded' THEN 1 ELSE 0 END
            + CASE WHEN NEW.state='recorded' THEN 1 ELSE 0 END,
        deduped=deduped
            - CASE WHEN OLD.state='deduped' THEN 1 ELSE 0 END
            + CASE WHEN NEW.state='deduped' THEN 1 ELSE 0 END
    WHERE target_id=NEW.target_id AND mode=NEW.mode;
END;

CREATE TRIGGER IF NOT EXISTS trg_task_mode_candidate_counter_delete
AFTER DELETE ON task_mode_candidates
BEGIN
    UPDATE task_mode_candidate_counters
    SET total=total - 1,
        pending=pending - CASE WHEN OLD.state='pending' THEN 1 ELSE 0 END,
        recorded=recorded - CASE WHEN OLD.state='recorded' THEN 1 ELSE 0 END,
        deduped=deduped - CASE WHEN OLD.state='deduped' THEN 1 ELSE 0 END
    WHERE target_id=OLD.target_id AND mode=OLD.mode;
    DELETE FROM task_mode_candidate_counters
    WHERE target_id=OLD.target_id AND mode=OLD.mode AND total <= 0;
END;

CREATE TABLE IF NOT EXISTS instagram_accounts (
    id TEXT PRIMARY KEY,
    instagram_user_id TEXT UNIQUE,
    current_username_norm TEXT NOT NULL UNIQUE,
    current_username_display TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Scope membership must not fetch the identity payload row per candidate.
CREATE INDEX IF NOT EXISTS idx_accounts_platform_scope
ON instagram_accounts(id,current_username_norm);

CREATE TABLE IF NOT EXISTS instagram_username_aliases (
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id, username_norm)
);

CREATE TABLE IF NOT EXISTS global_seen (
    account_id TEXT PRIMARY KEY REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    sources_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Backup provenance is permanent even when a waiting source/action row is
-- removed. It does not confer a workbench claim or make dedupe owner-scoped.
CREATE TABLE IF NOT EXISTS global_identity_owners (
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_global_identity_owners_account
ON global_identity_owners(account_id);

-- COUNT(*) over a permanent global dedupe ledger becomes progressively more
-- expensive.  Keep the dashboard total exact and constant-time with two tiny
-- triggers; global_seen remains the authoritative row-level ledger.
CREATE TABLE IF NOT EXISTS global_seen_stats (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    total_count INTEGER NOT NULL CHECK(total_count>=0)
);
INSERT OR IGNORE INTO global_seen_stats(singleton_id, total_count)
SELECT 1, (SELECT COUNT(*) FROM global_seen)
WHERE NOT EXISTS(SELECT 1 FROM global_seen_stats WHERE singleton_id=1);
-- Platform dashboards retain the same constant-time count contract as the
-- all-platform ledger. Legacy non-prefixed identities are Instagram.
CREATE TABLE IF NOT EXISTS global_seen_platform_stats (
    platform TEXT PRIMARY KEY CHECK(platform IN ('instagram','facebook')),
    total_count INTEGER NOT NULL CHECK(total_count>=0)
);
INSERT INTO global_seen_platform_stats(platform,total_count)
SELECT 'instagram', (SELECT COUNT(*) FROM global_seen seen JOIN instagram_accounts account ON account.id=seen.account_id
                     WHERE account.current_username_norm NOT GLOB 'fb:*')
WHERE NOT EXISTS(SELECT 1 FROM global_seen_platform_stats WHERE platform='instagram');
INSERT INTO global_seen_platform_stats(platform,total_count)
SELECT 'facebook', (SELECT COUNT(*) FROM global_seen seen JOIN instagram_accounts account ON account.id=seen.account_id
                    WHERE account.current_username_norm GLOB 'fb:*')
WHERE NOT EXISTS(SELECT 1 FROM global_seen_platform_stats WHERE platform='facebook');
CREATE TRIGGER IF NOT EXISTS trg_global_seen_platform_insert
AFTER INSERT ON global_seen
BEGIN
    UPDATE global_seen_platform_stats SET total_count=total_count+1
    WHERE platform=(SELECT CASE WHEN current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END
                    FROM instagram_accounts WHERE id=NEW.account_id);
END;
CREATE TRIGGER IF NOT EXISTS trg_global_seen_platform_delete
BEFORE DELETE ON global_seen
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=(SELECT CASE WHEN current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END
                    FROM instagram_accounts WHERE id=OLD.account_id);
END;
-- Cascading global_seen deletion runs after the parent identity is removed,
-- so retain its namespace here while OLD is still available. The child delete
-- trigger then finds no parent and cannot decrement the same count twice.
CREATE TRIGGER IF NOT EXISTS trg_global_seen_platform_account_delete
BEFORE DELETE ON instagram_accounts
WHEN EXISTS(SELECT 1 FROM global_seen WHERE account_id=OLD.id)
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=CASE WHEN OLD.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
END;
CREATE TRIGGER IF NOT EXISTS trg_global_seen_platform_rename
AFTER UPDATE OF current_username_norm ON instagram_accounts
WHEN (OLD.current_username_norm GLOB 'fb:*') != (NEW.current_username_norm GLOB 'fb:*')
 AND EXISTS(SELECT 1 FROM global_seen WHERE account_id=NEW.id)
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=CASE WHEN OLD.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
    UPDATE global_seen_platform_stats SET total_count=total_count+1
    WHERE platform=CASE WHEN NEW.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
END;
CREATE TRIGGER IF NOT EXISTS trg_global_seen_stats_insert
AFTER INSERT ON global_seen
BEGIN
    UPDATE global_seen_stats SET total_count=total_count+1 WHERE singleton_id=1;
END;
CREATE TRIGGER IF NOT EXISTS trg_global_seen_stats_delete
AFTER DELETE ON global_seen
BEGIN
    UPDATE global_seen_stats
    SET total_count=CASE WHEN total_count>0 THEN total_count-1 ELSE 0 END
    WHERE singleton_id=1;
END;

CREATE TABLE IF NOT EXISTS task_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    sources_json TEXT NOT NULL,
    visibility TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    screening_json TEXT NOT NULL,
    qualified INTEGER CHECK (qualified IN (0, 1) OR qualified IS NULL),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(target_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_results_task_updated ON task_results(task_id, updated_at DESC);
-- Dashboard classification counts must not read every stored profile JSON.
CREATE INDEX IF NOT EXISTS idx_results_task_visibility
ON task_results(task_id, visibility);

-- Cover progress joins with provenance, leaving retained profile payloads cold.
CREATE INDEX IF NOT EXISTS idx_results_progress_provenance
ON task_results(target_id, account_id, task_id, sources_json);

-- Legacy builds could store several task results for one global identity.
-- Preserve every displaced row in full before enforcing the canonical index.
-- Task/target IDs remain historical provenance even after their live rows vanish.
CREATE TABLE IF NOT EXISTS task_result_duplicate_archive (
    original_result_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    sources_json TEXT NOT NULL,
    visibility TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    screening_json TEXT NOT NULL,
    qualified INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_duplicate_archive_owner
ON task_result_duplicate_archive(owner_user_id, archived_at DESC);
-- Recognition/resume probes retained business history by stable identity.
-- The owner/archive-time index cannot serve this lookup, so a missing terminal
-- row otherwise scans the lifetime archive while holding the claim write lock.
CREATE INDEX IF NOT EXISTS idx_result_duplicate_archive_account
ON task_result_duplicate_archive(account_id);

CREATE TABLE IF NOT EXISTS browser_operation_leases (
    profile_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_leases_entity ON browser_operation_leases(entity_id);

-- Minimal retirement fence for generic browser sessions whose sole labelled
-- account plan was removed. Contains no profile content, credentials or names;
-- prevents inventory from silently treating a retired session as fresh IG.
CREATE TABLE IF NOT EXISTS retired_account_profiles (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id)
);

CREATE TABLE IF NOT EXISTS action_campaigns (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('follow', 'greet')),
    execution_type TEXT NOT NULL CHECK(execution_type IN ('manual', 'campaign')),
    profile_id TEXT NOT NULL,
    message TEXT,
    messages_json TEXT NOT NULL DEFAULT '[]',
    interval_min_seconds INTEGER NOT NULL,
    interval_max_seconds INTEGER NOT NULL,
    limit_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaigns_owner_updated ON action_campaigns(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_campaigns_profile_status ON action_campaigns(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_campaigns_owner_profile
ON action_campaigns(owner_user_id, profile_id, id);

CREATE TABLE IF NOT EXISTS action_targets (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES action_campaigns(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    source_target TEXT,
    queue_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    control_after_attempt TEXT
        CHECK(control_after_attempt IS NULL OR control_after_attempt IN ('pause', 'cancel')),
    last_error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, username_norm),
    UNIQUE(campaign_id, queue_order)
);
CREATE INDEX IF NOT EXISTS idx_action_targets_queue ON action_targets(campaign_id, queue_order);

CREATE TABLE IF NOT EXISTS action_attempts (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES action_campaigns(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES action_targets(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    details_json TEXT NOT NULL,
    UNIQUE(target_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_action_attempts_campaign ON action_attempts(campaign_id, started_at DESC);

-- A success is a permanent business ledger, not a UI cache.  It is consulted
-- before every later dispatch so snapshot/history pagination can never permit a
-- duplicate greet/follow.
CREATE TABLE IF NOT EXISTS action_success_ledger (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('follow', 'greet')),
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, operation, username_norm)
);
CREATE INDEX IF NOT EXISTS idx_action_success_owner_time
ON action_success_ledger(owner_user_id, completed_at DESC, username_norm);
CREATE INDEX IF NOT EXISTS idx_action_success_campaign_owner
ON action_success_ledger(campaign_id, owner_user_id, operation, completed_at);

-- Detached successful private follows survive campaign/attempt removal. These
-- are historical collection-profile snapshots, never current account lookups.
CREATE TABLE IF NOT EXISTS private_follow_completions (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    source_window_id TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    executor_json TEXT NOT NULL DEFAULT '{}',
    confirmation TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(owner_user_id,username_norm)
);
CREATE INDEX IF NOT EXISTS idx_private_follow_completion_report
ON private_follow_completions(owner_user_id,julianday(completed_at) DESC,attempt_id);
CREATE TRIGGER IF NOT EXISTS trg_private_follow_completion_no_update
BEFORE UPDATE ON private_follow_completions
BEGIN
    SELECT RAISE(ABORT, 'private follow completion is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_private_follow_completion_no_delete
BEFORE DELETE ON private_follow_completions
WHEN EXISTS(SELECT 1 FROM app_users WHERE id=OLD.owner_user_id)
BEGIN
    SELECT RAISE(ABORT, 'private follow completion is immutable');
END;

-- Transient unique dispatch fence.  A crashed process releases these during
-- recovery only after converting the running attempt to UNKNOWN.
CREATE TABLE IF NOT EXISTS action_dispatch_claims (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('follow', 'greet')),
    username_norm TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, operation, username_norm),
    UNIQUE(attempt_id)
);

CREATE TRIGGER IF NOT EXISTS trg_action_success_no_update
BEFORE UPDATE ON action_success_ledger
BEGIN
    SELECT RAISE(ABORT, 'action success ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_action_success_no_delete
BEFORE DELETE ON action_success_ledger
BEGIN
    SELECT RAISE(ABORT, 'action success ledger is immutable');
END;

CREATE TABLE IF NOT EXISTS action_counter_resets (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('follow', 'greet')),
    profile_id TEXT NOT NULL,
    reset_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, operation, profile_id)
);

-- Per-target manual recovery gate.  This is deliberately not keyed by Instagram
-- username: several task generations may fail for the same account, and dismissing
-- an old generation must not be forgotten when a newer generation enters the inbox.
-- target_id has no cascading FK so a deleted task/history row can still retain its
-- durable manual disposition.
CREATE TABLE IF NOT EXISTS task_target_recovery_controls (
    target_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'requeued', 'dismissed')),
    source_task_id TEXT,
    source_status TEXT,
    source_window_id TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target_recovery_owner_state
ON task_target_recovery_controls(owner_user_id, state, updated_at DESC);

-- Durable hand-off between collection history and the split-account workspace.
-- A row is intentionally keyed by application owner + Instagram username: the
-- same failed source must not be added to the collection queue more than once.
CREATE TABLE IF NOT EXISTS split_candidates (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    candidate_kind TEXT NOT NULL CHECK(candidate_kind IN ('manual', 'failure')),
    source_target TEXT,
    source_task_id TEXT,
    source_target_id TEXT,
    source_status TEXT,
    source_window_id TEXT,
    last_error TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    queue_state TEXT NOT NULL DEFAULT 'available'
        CHECK(queue_state IN ('available', 'queued', 'claimed')),
    queued_task_id TEXT,
    queued_target_id TEXT,
    queued_at TEXT,
    claimed_at TEXT,
    dispatch_locked INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_locked IN (0, 1)),
    manual_category_override TEXT
        CHECK(manual_category_override IN ('waiting', 'running', 'completed')),
    manual_category_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_user_id, username_norm)
);
CREATE INDEX IF NOT EXISTS idx_split_candidates_owner_state
ON split_candidates(owner_user_id, queue_state, updated_at DESC);

-- First completion evidence; presentation updates cannot change the finish date.
CREATE TABLE IF NOT EXISTS split_completed_targets (
    target_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_window_id TEXT,
    completed_at TEXT NOT NULL,
    completion_details_json TEXT NOT NULL DEFAULT '{}'
);
-- The singular legacy name belongs to split_candidate_history in older
-- databases. SQLite index names are database-wide: IF NOT EXISTS must not
-- silently skip this target-period index during an in-place upgrade.
-- Leave either historical index definition untouched; this is additive only.
CREATE INDEX IF NOT EXISTS idx_split_completed_targets_report_period
ON split_completed_targets(owner_user_id,julianday(completed_at) DESC,target_id);
CREATE INDEX IF NOT EXISTS idx_split_completed_target_username
ON split_completed_targets(owner_user_id,username_norm);
CREATE INDEX IF NOT EXISTS idx_target_recovery_owner_username
ON task_target_recovery_controls(owner_user_id,username_norm);
CREATE INDEX IF NOT EXISTS idx_task_targets_username_task_status
ON task_targets(username_norm,task_id,status);
DROP TRIGGER IF EXISTS trg_split_completed_target_insert;
CREATE TRIGGER trg_split_completed_target_insert
AFTER INSERT ON task_targets WHEN NEW.status='completed'
BEGIN
    INSERT OR IGNORE INTO split_completed_targets
        (target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at)
    SELECT NEW.id,owner_user_id,NEW.username_norm,NEW.username_display,NEW.task_id,
        COALESCE(NEW.current_window_id,NEW.preferred_window_id),NEW.updated_at
    FROM tasks WHERE id=NEW.task_id;
END;
DROP TRIGGER IF EXISTS trg_split_completed_target_update;
CREATE TRIGGER trg_split_completed_target_update
AFTER UPDATE OF status ON task_targets WHEN NEW.status='completed'
BEGIN
    INSERT OR IGNORE INTO split_completed_targets
        (target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at)
    SELECT NEW.id,owner_user_id,NEW.username_norm,NEW.username_display,NEW.task_id,
        COALESCE(NEW.current_window_id,NEW.preferred_window_id,OLD.current_window_id,OLD.preferred_window_id),NEW.updated_at
    FROM tasks WHERE id=NEW.task_id;
END;

-- Permanent owner-scoped source admissions. Never cleaned with the 7-day report.
CREATE TABLE IF NOT EXISTS split_admission_totals (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    successful_adds INTEGER NOT NULL DEFAULT 0 CHECK(successful_adds>=0),
    history_complete INTEGER NOT NULL DEFAULT 0 CHECK(history_complete IN (0,1)),
    has_executed INTEGER NOT NULL DEFAULT 0 CHECK(has_executed IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, username_norm)
);
-- The execution marker is independent of UI history and survives its removal.
CREATE TRIGGER IF NOT EXISTS trg_split_admission_executed_insert
AFTER INSERT ON task_targets WHEN NEW.status!='pending'
BEGIN
    INSERT INTO split_admission_totals(owner_user_id,username_norm,has_executed,updated_at)
    SELECT owner_user_id,NEW.username_norm,1,NEW.updated_at FROM tasks WHERE id=NEW.task_id
    ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET has_executed=1,updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS trg_split_admission_executed_update
AFTER UPDATE OF status ON task_targets WHEN NEW.status!='pending'
BEGIN
    INSERT INTO split_admission_totals(owner_user_id,username_norm,has_executed,updated_at)
    SELECT owner_user_id,NEW.username_norm,1,NEW.updated_at FROM tasks WHERE id=NEW.task_id
    ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET has_executed=1,updated_at=excluded.updated_at;
END;

-- Owner-level collection intake gate. It never changes the current window lease
-- or interrupts a target that already belongs to a running worker.
CREATE TABLE IF NOT EXISTS collection_dispatch_locks (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    updated_at TEXT NOT NULL
);

-- Optional hard window affinity for a delayed split target.  No rows means
-- automatic dispatch to any window owned by the accepting collection task.
-- One or more rows form an eligible-window pool; the normal atomic claim still
-- lets exactly one of those windows own the target generation.
CREATE TABLE IF NOT EXISTS split_candidate_window_affinity (
    candidate_id TEXT NOT NULL REFERENCES split_candidates(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    PRIMARY KEY(candidate_id, profile_id),
    UNIQUE(candidate_id, queue_order)
);
CREATE INDEX IF NOT EXISTS idx_split_window_affinity_profile
ON split_candidate_window_affinity(profile_id, candidate_id);

-- Completed split generations are immutable history.  They live outside the
-- username-unique delayed queue so the same Instagram account can be selected
-- again later without overwriting an earlier successful generation.
CREATE TABLE IF NOT EXISTS split_candidate_history (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    source_target TEXT,
    source_task_id TEXT,
    source_target_id TEXT,
    source_status TEXT,
    source_window_id TEXT,
    last_error TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    queued_task_id TEXT,
    queued_target_id TEXT,
    queued_at TEXT,
    claimed_at TEXT,
    completed_at TEXT NOT NULL,
    manual_category_override TEXT
        CHECK(manual_category_override IN ('waiting', 'running', 'completed')),
    manual_category_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_split_history_owner_completed
ON split_candidate_history(owner_user_id, completed_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_split_history_owner_username
ON split_candidate_history(owner_user_id, username_norm);

-- New-generation screening workbench.  ``instagram_accounts`` + ``global_seen``
-- remain the one authoritative, application-wide dedupe store.  These tables add
-- only durable workflow state around an identity that has already been claimed.
CREATE TABLE IF NOT EXISTS workbench_identity_claims (
    account_id TEXT PRIMARY KEY REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    claimed_by_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    source TEXT NOT NULL,
    source_target TEXT,
    claimed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_claims_owner_time
ON workbench_identity_claims(claimed_by_user_id, claimed_at DESC);
CREATE INDEX IF NOT EXISTS idx_workbench_claims_source_mode_account
ON workbench_identity_claims(source_target, source, account_id);

CREATE INDEX IF NOT EXISTS idx_workbench_claims_progress
ON workbench_identity_claims(source_target, source, account_id, claimed_by_user_id);

CREATE TABLE IF NOT EXISTS workbench_candidates (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    account_id TEXT NOT NULL UNIQUE REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    visibility TEXT NOT NULL CHECK(visibility IN ('public', 'private')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected')),
    profile_json TEXT NOT NULL DEFAULT '{}',
    screening_json TEXT NOT NULL DEFAULT '{}',
    review_cache_json TEXT NOT NULL DEFAULT '{}',
    source_mode TEXT,
    source_target TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_stage INTEGER NOT NULL DEFAULT 1 CHECK(review_stage IN (1, 2)),
    review_transferred_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workbench_candidates_owner_queue
ON workbench_candidates(owner_user_id, status, visibility, created_at, id);
CREATE INDEX IF NOT EXISTS idx_workbench_candidates_owner_reviewed
ON workbench_candidates(owner_user_id, status, visibility, reviewed_at DESC, id DESC);
-- Scope predicates can inspect account_id from the ordered/count index before
-- a bounded page loads profile/review payloads. Legacy unscoped indexes stay.
CREATE INDEX IF NOT EXISTS idx_candidates_platform_counts
ON workbench_candidates(owner_user_id,status,visibility,account_id,id);
CREATE INDEX IF NOT EXISTS idx_candidates_platform_created
ON workbench_candidates(owner_user_id,status,visibility,created_at,id,account_id);
CREATE INDEX IF NOT EXISTS idx_candidates_platform_reviewed
ON workbench_candidates(owner_user_id,status,visibility,reviewed_at DESC,id DESC,account_id);
CREATE INDEX IF NOT EXISTS idx_workbench_candidates_source_mode_account
ON workbench_candidates(source_target, source_mode, account_id);

CREATE INDEX IF NOT EXISTS idx_workbench_candidates_progress
ON workbench_candidates(source_target, source_mode, account_id, owner_user_id);

-- Old source-less candidates are sparse; keep them out of the normal claim scan.
CREATE INDEX IF NOT EXISTS idx_workbench_candidates_legacy_progress
ON workbench_candidates(account_id, owner_user_id, source_target, source_mode)
WHERE source_target IS NULL OR source_mode IS NULL;

-- Reconcile cache counters on every startup without rereading all profile and
-- preview payloads. Store only their byte length and nonempty flag, not JSON.
-- SQLite maintains these values atomically even when an older Core writes.
CREATE INDEX IF NOT EXISTS idx_workbench_cache_rebuild
ON workbench_candidates(owner_user_id, status,
    (review_cache_json <> '{}'), length(CAST(review_cache_json AS BLOB)));

-- Review previews are optional, but enforcing their disk budget used to SUM
-- every pending JSON blob for each newly discovered account.  Keep exact
-- owner-level byte/entry totals instead so a large unattended review backlog
-- does not progressively slow collection.
CREATE TABLE IF NOT EXISTS workbench_cache_usage (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    pending_entries INTEGER NOT NULL DEFAULT 0,
    pending_bytes INTEGER NOT NULL DEFAULT 0,
    terminal_entries INTEGER NOT NULL DEFAULT 0,
    terminal_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS trg_workbench_cache_usage_insert
AFTER INSERT ON workbench_candidates
WHEN NEW.review_cache_json <> '{}'
BEGIN
    INSERT INTO workbench_cache_usage(
        owner_user_id, pending_entries, pending_bytes,
        terminal_entries, terminal_bytes
    ) VALUES(
        NEW.owner_user_id,
        CASE WHEN NEW.status='pending' THEN 1 ELSE 0 END,
        CASE WHEN NEW.status='pending'
            THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
            ELSE 0 END,
        CASE WHEN NEW.status<>'pending' THEN 1 ELSE 0 END,
        CASE WHEN NEW.status<>'pending'
            THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
            ELSE 0 END
    )
    ON CONFLICT(owner_user_id) DO UPDATE SET
        pending_entries=pending_entries
            + CASE WHEN NEW.status='pending' THEN 1 ELSE 0 END,
        pending_bytes=pending_bytes
            + CASE WHEN NEW.status='pending'
                THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END,
        terminal_entries=terminal_entries
            + CASE WHEN NEW.status<>'pending' THEN 1 ELSE 0 END,
        terminal_bytes=terminal_bytes
            + CASE WHEN NEW.status<>'pending'
                THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END;
END;

CREATE TRIGGER IF NOT EXISTS trg_workbench_cache_usage_update
AFTER UPDATE OF owner_user_id, status, review_cache_json ON workbench_candidates
WHEN OLD.owner_user_id <> NEW.owner_user_id
  OR OLD.status <> NEW.status
  OR OLD.review_cache_json <> NEW.review_cache_json
BEGIN
    INSERT OR IGNORE INTO workbench_cache_usage(owner_user_id)
    VALUES(OLD.owner_user_id);
    UPDATE workbench_cache_usage
    SET pending_entries=pending_entries
            - CASE WHEN OLD.status='pending' AND OLD.review_cache_json<>'{}'
                THEN 1 ELSE 0 END,
        pending_bytes=pending_bytes
            - CASE WHEN OLD.status='pending'
                THEN MAX(length(CAST(OLD.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END,
        terminal_entries=terminal_entries
            - CASE WHEN OLD.status<>'pending' AND OLD.review_cache_json<>'{}'
                THEN 1 ELSE 0 END,
        terminal_bytes=terminal_bytes
            - CASE WHEN OLD.status<>'pending'
                THEN MAX(length(CAST(OLD.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END
    WHERE owner_user_id=OLD.owner_user_id;

    INSERT OR IGNORE INTO workbench_cache_usage(owner_user_id)
    VALUES(NEW.owner_user_id);
    UPDATE workbench_cache_usage
    SET pending_entries=pending_entries
            + CASE WHEN NEW.status='pending' AND NEW.review_cache_json<>'{}'
                THEN 1 ELSE 0 END,
        pending_bytes=pending_bytes
            + CASE WHEN NEW.status='pending'
                THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END,
        terminal_entries=terminal_entries
            + CASE WHEN NEW.status<>'pending' AND NEW.review_cache_json<>'{}'
                THEN 1 ELSE 0 END,
        terminal_bytes=terminal_bytes
            + CASE WHEN NEW.status<>'pending'
                THEN MAX(length(CAST(NEW.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END
    WHERE owner_user_id=NEW.owner_user_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_workbench_cache_usage_delete
AFTER DELETE ON workbench_candidates
WHEN OLD.review_cache_json <> '{}'
BEGIN
    UPDATE workbench_cache_usage
    SET pending_entries=pending_entries
            - CASE WHEN OLD.status='pending' THEN 1 ELSE 0 END,
        pending_bytes=pending_bytes
            - CASE WHEN OLD.status='pending'
                THEN MAX(length(CAST(OLD.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END,
        terminal_entries=terminal_entries
            - CASE WHEN OLD.status<>'pending' THEN 1 ELSE 0 END,
        terminal_bytes=terminal_bytes
            - CASE WHEN OLD.status<>'pending'
                THEN MAX(length(CAST(OLD.review_cache_json AS BLOB)) - 2, 0)
                ELSE 0 END
    WHERE owner_user_id=OLD.owner_user_id;
END;

-- One immutable decision per candidate.  A rejected row keeps the same lean
-- profile snapshot as an approved row, but never contains the transient post
-- review cache.  This makes rejection history useful without retaining screenshots.
CREATE TABLE IF NOT EXISTS workbench_review_decisions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES workbench_candidates(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    visibility TEXT NOT NULL CHECK(visibility IN ('public', 'private')),
    profile_snapshot_json TEXT NOT NULL,
    screening_snapshot_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_decisions_owner_time
ON workbench_review_decisions(owner_user_id, decided_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_workbench_decisions_owner_decision_time
ON workbench_review_decisions(owner_user_id, decision, decided_at DESC, id DESC);

-- Removing an approved row from the actionable queue is a projection change,
-- never a deletion of the immutable review/global-dedupe ledger.
CREATE TABLE IF NOT EXISTS workbench_candidate_dismissals (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES workbench_candidates(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    dismissed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workbench_dismissals_owner_time
ON workbench_candidate_dismissals(owner_user_id, dismissed_at DESC, id);

-- Collection-stage terminal exclusions (notably non-US location) never enter an
-- audit queue.  They are immutable history and the referenced account remains in
-- global_seen forever, so a later discovery is skipped before opening the profile.
CREATE TABLE IF NOT EXISTS workbench_collection_exclusions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    username_display TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    location_country TEXT,
    profile_snapshot_json TEXT NOT NULL DEFAULT '{}',
    excluded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exclusions_platform_counts
ON workbench_collection_exclusions(owner_user_id,username_display);
-- Older cores can write false-hover records even after the repair migration.
-- Keep checking on every launch, but do not scan immutable exclusion payloads.
CREATE INDEX IF NOT EXISTS idx_exclusions_legacy_hover
ON workbench_collection_exclusions(reason_code)
WHERE reason_code='hover_preview_unavailable';
CREATE INDEX IF NOT EXISTS idx_exclusions_platform_page
ON workbench_collection_exclusions(owner_user_id,excluded_at DESC,id DESC,username_display);
CREATE INDEX IF NOT EXISTS idx_workbench_exclusions_owner_time
ON workbench_collection_exclusions(owner_user_id, excluded_at DESC, id);

-- Snapshot history uses descending ids to break equal-timestamp ties. The older
-- mixed-direction index forces SQLite to sort and load whole timestamp batches.
CREATE INDEX IF NOT EXISTS idx_workbench_exclusions_owner_page
ON workbench_collection_exclusions(owner_user_id, excluded_at DESC, id DESC);

-- An expression index computes the disposition once on insert, preserving the
-- authoritative immutable evidence without reparsing large profiles per poll.
CREATE INDEX IF NOT EXISTS idx_workbench_exclusions_progress
ON workbench_collection_exclusions(account_id, owner_user_id,
    CASE WHEN json_valid(profile_snapshot_json)
         THEN json_extract(profile_snapshot_json,'$.page_read_status')='hover_preview'
         ELSE 0 END);

-- R6 work reports seek a selected period before deduplicating identities.
-- Only index keys, never large profile/preview JSON, participate in the sort.
CREATE INDEX IF NOT EXISTS idx_results_report_period
ON task_results(julianday(created_at),account_id,task_id,id,created_at);
CREATE INDEX IF NOT EXISTS idx_results_report_identity
ON task_results(account_id,julianday(created_at),task_id,created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_report_period
ON workbench_candidates(owner_user_id,julianday(created_at),account_id,id,created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_report_identity
ON workbench_candidates(account_id,owner_user_id,julianday(created_at),created_at);
CREATE INDEX IF NOT EXISTS idx_exclusions_report_identity
ON workbench_collection_exclusions(account_id,owner_user_id,julianday(excluded_at),excluded_at);
CREATE INDEX IF NOT EXISTS idx_candidates_report_reviewed
ON workbench_candidates(owner_user_id,status,julianday(reviewed_at),account_id);
CREATE INDEX IF NOT EXISTS idx_exclusions_report_period
ON workbench_collection_exclusions(owner_user_id,julianday(excluded_at),account_id,id,excluded_at);
CREATE INDEX IF NOT EXISTS idx_split_history_report_period
ON split_candidate_history(owner_user_id,source_status,julianday(completed_at),source_target_id,id);
CREATE INDEX IF NOT EXISTS idx_split_history_report_identity
ON split_candidate_history(owner_user_id,source_target_id,source_status,julianday(completed_at));
CREATE INDEX IF NOT EXISTS idx_actions_report_period
ON action_success_ledger(owner_user_id,julianday(completed_at),operation,campaign_id,attempt_id);
CREATE INDEX IF NOT EXISTS idx_follow_rounds_report_period
ON follow_monitor_rounds(owner_user_id,julianday(checked_at),added_count);

-- A global revision lets the renderer replace its former localStorage merge logic
-- with one coherent SQLite snapshot.  Every successful workbench mutation bumps it.
CREATE TABLE IF NOT EXISTS workbench_state_revision (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    last_cleanup_at TEXT
);
INSERT OR IGNORE INTO workbench_state_revision(singleton_id, revision, updated_at)
VALUES(1, 0, datetime('now'));

CREATE TRIGGER IF NOT EXISTS trg_workbench_decision_no_update
BEFORE UPDATE ON workbench_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'workbench review decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_workbench_decision_no_delete
BEFORE DELETE ON workbench_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'workbench review decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_workbench_dismissal_no_update
BEFORE UPDATE ON workbench_candidate_dismissals
BEGIN
    SELECT RAISE(ABORT, 'workbench candidate dismissals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_workbench_dismissal_no_delete
BEFORE DELETE ON workbench_candidate_dismissals
BEGIN
    SELECT RAISE(ABORT, 'workbench candidate dismissals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_workbench_exclusion_no_update
BEFORE UPDATE ON workbench_collection_exclusions
BEGIN
    SELECT RAISE(ABORT, 'workbench collection exclusions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_workbench_exclusion_no_delete
BEFORE DELETE ON workbench_collection_exclusions
BEGIN
    SELECT RAISE(ABORT, 'workbench collection exclusions are immutable');
END;

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_owner_seq ON event_log(owner_user_id, seq);

-- The snapshot previously read every retained diagnostic payload to SUM its
-- bytes on every refresh. Maintain the exact same byte accounting at write time.
CREATE TABLE IF NOT EXISTS event_log_usage (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    entries INTEGER NOT NULL DEFAULT 0 CHECK(entries >= 0),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes >= 0)
);
CREATE TRIGGER IF NOT EXISTS trg_event_log_usage_insert
AFTER INSERT ON event_log
BEGIN
    INSERT INTO event_log_usage(owner_user_id, entries, bytes)
    VALUES(NEW.owner_user_id, 1,
        length(CAST(NEW.payload_json AS BLOB)) + length(NEW.entity_type)
        + length(NEW.entity_id) + length(NEW.event_type) + length(NEW.created_at))
    ON CONFLICT(owner_user_id) DO UPDATE SET
        entries=entries+1, bytes=bytes+excluded.bytes;
END;
CREATE TRIGGER IF NOT EXISTS trg_event_log_usage_delete
AFTER DELETE ON event_log
BEGIN
    UPDATE event_log_usage SET entries=entries-1,
        bytes=bytes-(length(CAST(OLD.payload_json AS BLOB)) + length(OLD.entity_type)
            + length(OLD.entity_id) + length(OLD.event_type) + length(OLD.created_at))
    WHERE owner_user_id=OLD.owner_user_id;
END;
CREATE TRIGGER IF NOT EXISTS trg_event_log_usage_update
AFTER UPDATE OF owner_user_id, payload_json, entity_type, entity_id, event_type, created_at
ON event_log
BEGIN
    UPDATE event_log_usage SET entries=entries-1,
        bytes=bytes-(length(CAST(OLD.payload_json AS BLOB)) + length(OLD.entity_type)
            + length(OLD.entity_id) + length(OLD.event_type) + length(OLD.created_at))
    WHERE owner_user_id=OLD.owner_user_id;
    INSERT INTO event_log_usage(owner_user_id, entries, bytes)
    VALUES(NEW.owner_user_id, 1,
        length(CAST(NEW.payload_json AS BLOB)) + length(NEW.entity_type)
        + length(NEW.entity_id) + length(NEW.event_type) + length(NEW.created_at))
    ON CONFLICT(owner_user_id) DO UPDATE SET
        entries=entries+1, bytes=bytes+excluded.bytes;
END;
"""


# Version 9 rebuilds these triggers even for databases which already contain older
# definitions.  CREATE TRIGGER IF NOT EXISTS is insufficient for an
# installed application because SQLite would otherwise retain the old, unconditional
# failure -> available transition forever.
# v9 separates the delayed manual queue from the per-target recovery inbox.
# ``split_candidates`` remains username-keyed because it is only a work queue;
# failures are target-generation-keyed in ``task_target_recovery_controls`` so a
# healthy sibling with the same username can never conceal a failed generation.
SPLIT_CANDIDATE_TRIGGERS_V9 = """
DROP TRIGGER IF EXISTS trg_split_candidate_target_inserted;
DROP TRIGGER IF EXISTS trg_split_candidate_target_claimed;
DROP TRIGGER IF EXISTS trg_split_candidate_target_requeued;
DROP TRIGGER IF EXISTS trg_split_candidate_target_failed;
DROP TRIGGER IF EXISTS trg_split_candidate_target_deleted;
DROP TRIGGER IF EXISTS trg_split_candidate_task_terminal;

-- These three triggers are deliberately limited to the manual delayed queue.
-- Recovery controls are changed only by an explicit recovery action or a new
-- failure; ordinary target state changes must not silently acknowledge a failure.
CREATE TRIGGER trg_split_candidate_target_inserted
AFTER INSERT ON task_targets
BEGIN
    UPDATE split_candidates
    SET queue_state='queued', queued_task_id=NEW.task_id,
        queued_target_id=NEW.id, queued_at=COALESCE(queued_at, NEW.created_at),
        updated_at=NEW.updated_at
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND username_norm=NEW.username_norm
      AND candidate_kind='manual'
      AND (source_target_id IS NULL OR source_target_id=NEW.id)
      AND queue_state!='claimed';
END;

CREATE TRIGGER trg_split_candidate_target_claimed
AFTER UPDATE OF status ON task_targets
WHEN NEW.status IN ('running', 'waiting_network', 'completed')
BEGIN
    UPDATE split_candidates
    SET queue_state='claimed', queued_task_id=NEW.task_id,
        queued_target_id=NEW.id, queued_at=COALESCE(queued_at, NEW.updated_at),
        updated_at=NEW.updated_at
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND username_norm=NEW.username_norm
      AND candidate_kind='manual'
      AND (source_target_id IS NULL OR source_target_id=NEW.id);
END;

CREATE TRIGGER trg_split_candidate_target_requeued
AFTER UPDATE OF status ON task_targets
WHEN NEW.status='pending'
BEGIN
    UPDATE split_candidates
    SET queue_state='queued', queued_task_id=NEW.task_id,
        queued_target_id=NEW.id, queued_at=COALESCE(queued_at, NEW.updated_at),
        updated_at=NEW.updated_at
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND username_norm=NEW.username_norm
      AND candidate_kind='manual'
      AND (source_target_id IS NULL OR source_target_id=NEW.id)
      AND queue_state!='claimed';
END;

-- candidate_id identifies one visible failure generation. Repeated failure
-- notifications while PENDING preserve it (idempotent UI actions); a target that
-- was explicitly REQUEUED and fails again receives a fresh identifier. A
-- DISMISSED generation remains dismissed even if a later task-terminal update is
-- replayed for the same target.
CREATE TRIGGER trg_split_candidate_target_failed
AFTER UPDATE OF status ON task_targets
WHEN NEW.status IN ('failed', 'recoverable', 'stopped')
 AND NOT (
    OLD.status IN ('pending', 'failed', 'recoverable', 'stopped')
    AND OLD.current_window_id IS NULL
    AND EXISTS(
      SELECT 1 FROM task_target_recovery_controls recovery
      JOIN split_candidates waiting
        ON waiting.owner_user_id=recovery.owner_user_id
       AND waiting.source_target_id=recovery.target_id
       AND waiting.source_task_id=recovery.source_task_id
      WHERE recovery.target_id=NEW.id
        AND recovery.source_task_id=NEW.task_id
        AND recovery.state='requeued'
        AND waiting.candidate_kind='manual'
        AND waiting.source_status='requeued_by_user'
        AND waiting.queue_state='queued'
        AND waiting.queued_task_id IS NULL
        AND waiting.queued_target_id IS NULL
    )
 )
BEGIN
    INSERT INTO task_target_recovery_controls(
        target_id, owner_user_id, candidate_id, username_norm, username_display,
        state, source_task_id, source_status, source_window_id, last_error,
        updated_at
    )
    SELECT NEW.id, task.owner_user_id, lower(hex(randomblob(16))),
           NEW.username_norm, NEW.username_display, 'pending', NEW.task_id,
           NEW.status, COALESCE(NEW.current_window_id, NEW.preferred_window_id),
           COALESCE(NEW.last_error, task.last_error), NEW.updated_at
    FROM tasks task
    WHERE task.id=NEW.task_id
      AND NOT EXISTS(
        SELECT 1 FROM split_candidate_history history
        WHERE history.owner_user_id=task.owner_user_id
          AND history.source_target_id=NEW.id
      )
    ON CONFLICT(target_id) DO UPDATE SET
        owner_user_id=excluded.owner_user_id,
        candidate_id=CASE
          WHEN task_target_recovery_controls.state='requeued'
          THEN excluded.candidate_id
          ELSE COALESCE(task_target_recovery_controls.candidate_id,
                        excluded.candidate_id)
        END,
        username_norm=excluded.username_norm,
        username_display=excluded.username_display,
        state=CASE
          WHEN task_target_recovery_controls.state='dismissed'
          THEN 'dismissed' ELSE 'pending'
        END,
        source_task_id=excluded.source_task_id,
        source_status=excluded.source_status,
        source_window_id=excluded.source_window_id,
        last_error=excluded.last_error,
        updated_at=excluded.updated_at;
END;

CREATE TRIGGER trg_split_candidate_target_deleted
BEFORE DELETE ON task_targets
WHEN OLD.status!='completed'
 AND NOT EXISTS(
   SELECT 1 FROM split_candidate_history history
   WHERE history.source_target_id=OLD.id
 )
BEGIN
    INSERT INTO task_target_recovery_controls(
        target_id, owner_user_id, candidate_id, username_norm, username_display,
        state, source_task_id, source_status, source_window_id, last_error,
        updated_at
    )
    SELECT OLD.id, task.owner_user_id, lower(hex(randomblob(16))),
           OLD.username_norm, OLD.username_display, 'pending', OLD.task_id,
           'deleted', COALESCE(OLD.current_window_id, OLD.preferred_window_id),
           COALESCE(OLD.last_error, task.last_error), datetime('now')
    FROM tasks task
    WHERE task.id=OLD.task_id
    ON CONFLICT(target_id) DO UPDATE SET
        owner_user_id=excluded.owner_user_id,
        candidate_id=CASE
          WHEN task_target_recovery_controls.state='requeued'
          THEN excluded.candidate_id
          ELSE COALESCE(task_target_recovery_controls.candidate_id,
                        excluded.candidate_id)
        END,
        username_norm=excluded.username_norm,
        username_display=excluded.username_display,
        state=CASE
          WHEN task_target_recovery_controls.state='dismissed'
          THEN 'dismissed' ELSE 'pending'
        END,
        source_task_id=excluded.source_task_id,
        source_status=excluded.source_status,
        source_window_id=excluded.source_window_id,
        last_error=excluded.last_error,
        updated_at=excluded.updated_at;
END;

CREATE TRIGGER trg_split_candidate_task_terminal
AFTER UPDATE OF status ON tasks
WHEN NEW.status IN ('failed', 'recoverable', 'stopped')
BEGIN
    INSERT INTO task_target_recovery_controls(
        target_id, owner_user_id, candidate_id, username_norm, username_display,
        state, source_task_id, source_status, source_window_id, last_error,
        updated_at
    )
    SELECT target.id, NEW.owner_user_id, lower(hex(randomblob(16))),
           target.username_norm, target.username_display, 'pending', NEW.id,
           NEW.status,
           COALESCE(target.current_window_id, target.preferred_window_id),
           COALESCE(target.last_error, NEW.last_error), NEW.updated_at
    FROM task_targets target
    WHERE target.task_id=NEW.id AND target.status!='completed'
      -- Both automatic release of a returned window and explicit removal can
      -- finalize the old parent after the return transaction. Neither is a new
      -- failure of this unclaimed waiting generation. Preferred window/stage
      -- are historical metadata, not proof that it was claimed again.
      AND NOT (
        target.status IN ('pending', 'failed', 'recoverable', 'stopped')
        AND target.current_window_id IS NULL
        AND EXISTS(
          SELECT 1 FROM task_target_recovery_controls recovery
          JOIN split_candidates waiting
            ON waiting.owner_user_id=recovery.owner_user_id
           AND waiting.source_target_id=recovery.target_id
           AND waiting.source_task_id=recovery.source_task_id
          WHERE recovery.target_id=target.id
            AND recovery.owner_user_id=NEW.owner_user_id
            AND recovery.state='requeued'
            AND waiting.candidate_kind='manual'
            AND waiting.source_status='requeued_by_user'
            AND waiting.queue_state='queued'
            AND waiting.queued_task_id IS NULL
            AND waiting.queued_target_id IS NULL
        )
      )
      AND NOT EXISTS(
        SELECT 1 FROM split_candidate_history history
        WHERE history.owner_user_id=NEW.owner_user_id
          AND history.source_target_id=target.id
      )
    ON CONFLICT(target_id) DO UPDATE SET
        owner_user_id=excluded.owner_user_id,
        candidate_id=CASE
          WHEN task_target_recovery_controls.state='requeued'
          THEN excluded.candidate_id
          ELSE COALESCE(task_target_recovery_controls.candidate_id,
                        excluded.candidate_id)
        END,
        username_norm=excluded.username_norm,
        username_display=excluded.username_display,
        state=CASE
          WHEN task_target_recovery_controls.state='dismissed'
          THEN 'dismissed' ELSE 'pending'
        END,
        source_task_id=excluded.source_task_id,
        source_status=excluded.source_status,
        -- A parent replay must retain the actual last failing window (B),
        -- rather than replacing it with the target's historical preference (A).
        source_window_id=CASE
          WHEN task_target_recovery_controls.state='pending'
          THEN COALESCE(task_target_recovery_controls.source_window_id,
                        excluded.source_window_id)
          ELSE excluded.source_window_id
        END,
        last_error=excluded.last_error,
        updated_at=excluded.updated_at;
END;
"""


# Version 10 adds an explicit, durable split lifecycle.  A selected candidate
# stays in the username-unique delayed queue while WAITING/RUNNING.  Successful
# completion atomically snapshots that exact generation into immutable history
# and removes it from the live queue, freeing the username for a later generation.
SPLIT_CANDIDATE_LIFECYCLE_TRIGGERS_V10 = """
DROP TRIGGER IF EXISTS trg_split_candidate_target_inserted;
DROP TRIGGER IF EXISTS trg_split_candidate_target_claimed;
DROP TRIGGER IF EXISTS trg_split_candidate_target_requeued;
DROP TRIGGER IF EXISTS trg_split_candidate_target_completed;
DROP TRIGGER IF EXISTS trg_split_candidate_target_failed_lifecycle;
DROP TRIGGER IF EXISTS trg_split_target_generation_fence;

-- Status callbacks may arrive after a worker has already converged the same
-- generation.  Reject failure<->completion reversals at SQLite's boundary so a
-- stale coroutine, bulk recovery write, or future caller cannot produce both an
-- immutable completion and a pending manual failure for one target generation.
CREATE TRIGGER trg_split_target_generation_fence
BEFORE UPDATE OF status ON task_targets
WHEN (
       OLD.status='completed' AND NEW.status!='completed'
       AND NOT (NEW.status='pending' AND NEW.current_stage='source_recheck_requested'
         AND EXISTS(SELECT 1 FROM task_source_rechecks recheck
                    JOIN task_checkpoints checkpoint ON checkpoint.target_id=recheck.target_id
                         AND checkpoint.mode=recheck.mode
                    WHERE recheck.target_id=NEW.id AND recheck.state='prepared'
                      AND checkpoint.stage='source_recheck_requested'
                      AND json_extract(checkpoint.cursor_json, '$.candidate_spool_complete')=0))
     )
  OR (
       OLD.status IN ('failed', 'recoverable', 'stopped')
       AND NEW.status='completed'
     )
  OR (
       NEW.status='completed'
       AND EXISTS(
         SELECT 1 FROM task_target_recovery_controls recovery
         WHERE recovery.target_id=NEW.id AND recovery.state='pending'
       )
     )
  OR (
       NEW.status IN ('failed', 'recoverable', 'stopped')
       AND EXISTS(
         SELECT 1 FROM split_candidate_history history
         WHERE history.source_target_id=NEW.id
       )
       AND NOT EXISTS(SELECT 1 FROM task_source_rechecks recheck
                      WHERE recheck.target_id=NEW.id AND recheck.state IN ('prepared','active','resuming'))
     )
BEGIN
    SELECT RAISE(ABORT, 'stale split target lifecycle transition');
END;

CREATE TRIGGER trg_split_candidate_target_claimed
AFTER UPDATE OF status ON task_targets
WHEN NEW.status IN ('running', 'waiting_network')
BEGIN
    UPDATE split_candidates
    SET queue_state='claimed', queued_task_id=NEW.task_id,
        queued_target_id=NEW.id, queued_at=COALESCE(queued_at, NEW.updated_at),
        claimed_at=COALESCE(claimed_at, NEW.updated_at),
        source_window_id=COALESCE(NEW.current_window_id, NEW.preferred_window_id,
                                  source_window_id),
        updated_at=NEW.updated_at
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND candidate_kind='manual'
      AND queue_state='claimed'
      AND queued_task_id=NEW.task_id
      AND queued_target_id=NEW.id;
END;

CREATE TRIGGER trg_split_candidate_target_completed
AFTER UPDATE OF status ON task_targets
WHEN NEW.status='completed'
BEGIN
    INSERT INTO split_candidate_history(
        id, owner_user_id, username_norm, username_display, source_target,
        source_task_id, source_target_id, source_status, source_window_id,
        last_error, profile_json, queued_task_id, queued_target_id, queued_at,
        claimed_at, completed_at, manual_category_override,
        manual_category_at, created_at, updated_at
    )
    SELECT candidate.id, candidate.owner_user_id, candidate.username_norm,
           candidate.username_display, candidate.source_target, NEW.task_id,
           NEW.id, 'completed',
           COALESCE(NEW.current_window_id, NEW.preferred_window_id,
                    candidate.source_window_id),
           NULL, candidate.profile_json, NEW.task_id, NEW.id,
           COALESCE(candidate.queued_at, candidate.created_at),
           COALESCE(candidate.claimed_at, NEW.updated_at), NEW.updated_at,
           NULL, NULL, candidate.created_at, NEW.updated_at
    FROM split_candidates candidate
    WHERE candidate.owner_user_id=(
              SELECT owner_user_id FROM tasks WHERE id=NEW.task_id
          )
      AND candidate.candidate_kind='manual'
      AND candidate.queue_state='claimed'
      AND candidate.queued_target_id=NEW.id
      AND NOT EXISTS(
        SELECT 1 FROM task_target_recovery_controls recovery
        WHERE recovery.target_id=NEW.id AND recovery.state='pending'
      )
    ON CONFLICT(id) DO UPDATE SET
        source_task_id=excluded.source_task_id,
        source_target_id=excluded.source_target_id,
        source_status='completed',
        source_window_id=COALESCE(split_candidate_history.source_window_id,
                                  excluded.source_window_id),
        last_error=NULL,
        queued_task_id=excluded.queued_task_id,
        queued_target_id=excluded.queued_target_id,
        queued_at=COALESCE(split_candidate_history.queued_at,
                           excluded.queued_at),
        claimed_at=COALESCE(split_candidate_history.claimed_at,
                            excluded.claimed_at),
        completed_at=COALESCE(split_candidate_history.completed_at,
                              excluded.completed_at),
        updated_at=split_candidate_history.updated_at;

    DELETE FROM split_candidates
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND candidate_kind='manual'
      AND queue_state='claimed'
      AND queued_target_id=NEW.id
      AND NOT EXISTS(
        SELECT 1 FROM task_target_recovery_controls recovery
        WHERE recovery.target_id=NEW.id AND recovery.state='pending'
      );
END;

CREATE TRIGGER trg_split_candidate_target_failed_lifecycle
AFTER UPDATE OF status ON task_targets
WHEN NEW.status IN ('failed', 'recoverable', 'stopped')
BEGIN
    UPDATE split_candidates
    SET manual_category_override=NULL, manual_category_at=NULL,
        updated_at=NEW.updated_at
    WHERE owner_user_id=(SELECT owner_user_id FROM tasks WHERE id=NEW.task_id)
      AND candidate_kind='manual'
      AND queued_task_id=NEW.task_id
      AND queued_target_id=NEW.id;
END;
"""


def _prune_diagnostic_event_log(connection: sqlite3.Connection) -> None:
    """Bound diagnostic rows only; authoritative business ledgers are separate."""

    event_owners = connection.execute(
        "SELECT DISTINCT owner_user_id FROM event_log"
    ).fetchall()
    for event_owner in event_owners:
        owner_id = event_owner["owner_user_id"]
        connection.execute(
            """
            DELETE FROM event_log
            WHERE owner_user_id=?
              AND julianday(created_at) < julianday('now', ?)
            """,
            (owner_id, f"-{EVENT_LOG_RETENTION_DAYS} days"),
        )
        connection.execute(
            """
            DELETE FROM event_log
            WHERE owner_user_id=?
              AND seq < COALESCE(
                  (
                      SELECT MIN(seq) FROM (
                          SELECT seq FROM event_log
                          WHERE owner_user_id=?
                          ORDER BY seq DESC
                          LIMIT ?
                      )
                  ),
                  0
              )
            """,
            (owner_id, owner_id, EVENT_LOG_MAX_PER_OWNER),
        )


def _prune_terminal_candidate_spool(connection: sqlite3.Connection) -> None:
    """Drop old *completed-mode* discovery rows; preserve resumable work.

    A failed/stopped task can still contain pending candidates and an incomplete
    relation cursor.  Task status alone is therefore not proof that its technical
    spool is disposable.  Require the per-mode completion checkpoint as well, and
    for unbounded relation modes require the explicit physical-tail marker.
    """

    connection.execute(
        """
        DELETE FROM task_mode_candidates
        WHERE state IN ('recorded', 'deduped')
          AND NOT EXISTS (
            SELECT 1 FROM task_mode_candidates unfinished
            WHERE unfinished.target_id=task_mode_candidates.target_id
              AND unfinished.mode=task_mode_candidates.mode
              AND unfinished.state='pending'
          )
          AND EXISTS (
            SELECT 1
            FROM task_targets target
            JOIN tasks task ON task.id=target.task_id
            JOIN task_checkpoints checkpoint
              ON checkpoint.task_id=task.id
             AND checkpoint.target_id=target.id
             AND checkpoint.mode=task_mode_candidates.mode
            WHERE target.id=task_mode_candidates.target_id
              AND checkpoint.stage='mode_completed'
              AND COALESCE(
                    json_extract(
                        checkpoint.cursor_json,
                        '$.candidate_spool_complete'
                    ),
                    0
                  )=1
              AND (
                    task_mode_candidates.mode NOT IN ('followers', 'following')
                    OR COALESCE(
                        json_extract(
                            checkpoint.cursor_json,
                            '$.candidate_spool_natural_end'
                        ),
                        0
                    )=1
                  )
              AND task.status IN ('completed', 'failed', 'stopped')
              AND julianday(task.updated_at) <= julianday('now', ?)
        )
        """,
        (f"-{TECHNICAL_SPOOL_RETENTION_DAYS} days",),
    )


# The pure-IG upgrade is the sole exception to permanent FB business ledgers.
# It removes logical rows, not forensic traces, browser sessions or user backups.
PURE_IG_MIGRATION_VERSION = 39


def _purge_legacy_facebook_data(connection: sqlite3.Connection, *, live_lease_tokens=()) -> bool:
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version=?",
                          (PURE_IG_MIGRATION_VERSION,)).fetchone():
        return False
    return purge_legacy_facebook_data(connection, live_lease_tokens=live_lease_tokens)


def purge_legacy_facebook_data(connection: sqlite3.Connection, *, live_lease_tokens=()) -> bool:
    """Atomically retire positively identified Facebook data at upgrade or restore.

    Ownership is an explicit task platform or the reserved fb: identity namespace,
    never a display name, numeric id, URL found in free text or window name. An
    identity with a non-FB alias is contradictory/shared evidence and is retained.
    The same applies to shared queue rows whose queued generation belongs to IG.
    Unknown/orphan provenance is retained unless its own identity proves FB.

    The caller is in startup's OS instance lock, before worker recovery/dispatch.
    A still-live affected lease blocks this upgrade rather than stealing a window.
    All deletes, temporarily suspended immutable guards, derived repairs and the
    marker share ONE transaction. No backup or archive is made by this migration.
    """
    if live_lease_tokens:
        raise RuntimeError("Close active collection windows before the pure-IG database upgrade")
    # Restore calls this inside its existing all-or-nothing transaction. A
    # savepoint must never commit the caller's writes or swallow its rollback.
    nested = connection.in_transaction
    connection.execute("SAVEPOINT pure_ig_retirement" if nested else "BEGIN IMMEDIATE")
    try:
        original_fk_errors = {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")}
        sets = {
            'tasks': """SELECT id FROM tasks WHERE CASE WHEN json_valid(settings_json)
                THEN lower(trim(CAST(json_extract(settings_json,'$.platform') AS TEXT)))='facebook'
                ELSE 0 END""",
            'leases': """SELECT lease.profile_id FROM browser_operation_leases lease
                JOIN tasks task ON task.id=lease.entity_id AND task.owner_user_id=lease.owner_user_id
                WHERE lease.operation_type='collection' AND task.id IN (SELECT id FROM pure_ig_tasks)""",
            'targets': """SELECT id FROM task_targets WHERE username_norm GLOB 'fb:*'
                OR task_id IN (SELECT id FROM pure_ig_tasks)""",
            'accounts': """SELECT account.id FROM instagram_accounts account
                WHERE account.current_username_norm GLOB 'fb:*'
                  AND NOT EXISTS (SELECT 1 FROM instagram_username_aliases alias
                      WHERE alias.account_id=account.id AND alias.username_norm NOT GLOB 'fb:*')
                  AND (account.instagram_user_id IS NULL OR account.instagram_user_id GLOB 'fbid:*' OR account.instagram_user_id GLOB 'fb:*')""",
            'completed_targets': """SELECT target_id AS id FROM split_completed_targets
                WHERE username_norm GLOB 'fb:*' OR target_id IN (SELECT id FROM pure_ig_targets)
                  OR source_task_id IN (SELECT id FROM pure_ig_tasks)""",
            'split_history': """SELECT id FROM split_candidate_history WHERE username_norm GLOB 'fb:*' OR (
                (source_task_id IN (SELECT id FROM pure_ig_tasks) OR source_target_id IN (SELECT id FROM pure_ig_targets))
                AND (queued_task_id IS NULL OR queued_task_id IN (SELECT id FROM pure_ig_tasks))
                AND (queued_target_id IS NULL OR queued_target_id IN (SELECT id FROM pure_ig_targets)))""",
            'candidates': """SELECT id FROM workbench_candidates
                WHERE account_id IN (SELECT id FROM pure_ig_accounts)""",
            'action_targets': "SELECT id FROM action_targets WHERE username_norm GLOB 'fb:*'",
            'plans': """SELECT id FROM account_window_plans WHERE CASE WHEN json_valid(environment_json)
                THEN lower(trim(CAST(json_extract(environment_json,'$.platform') AS TEXT)))='facebook'
                ELSE 0 END""",

            'split_candidates': """SELECT id FROM split_candidates
                WHERE username_norm GLOB 'fb:*' OR (
                    (source_task_id IN (SELECT id FROM pure_ig_tasks)
                     OR source_target_id IN (SELECT id FROM pure_ig_targets))
                    AND (queued_task_id IS NULL OR queued_task_id IN (SELECT id FROM pure_ig_tasks))
                    AND (queued_target_id IS NULL OR queued_target_id IN (SELECT id FROM pure_ig_targets)))""",
        }
        for name, select in sets.items():
            connection.execute(f"CREATE TEMP TABLE pure_ig_{name}(id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO pure_ig_{name} {select}")
        # Generic browser directories cannot be classified by their display
        # names and are never removed. Retain only this content-free fence when
        # every known plan for the profile was explicitly FB and no IG subsystem
        # has ever recorded use. Archived or malformed plans count as shared.
        connection.execute("""INSERT OR IGNORE INTO retired_account_profiles(owner_user_id,profile_id)
            SELECT DISTINCT plan.owner_user_id,plan.profile_id FROM account_window_plans plan
            WHERE plan.id IN (SELECT id FROM pure_ig_plans) AND plan.profile_id<>''
              AND NOT EXISTS (SELECT 1 FROM account_window_plans other
                  WHERE other.profile_id=plan.profile_id AND other.id NOT IN (SELECT id FROM pure_ig_plans))
              AND NOT EXISTS (SELECT 1 FROM task_windows window JOIN tasks task ON task.id=window.task_id
                  WHERE window.profile_id=plan.profile_id AND task.id NOT IN (SELECT id FROM pure_ig_tasks))
              AND NOT EXISTS (SELECT 1 FROM task_targets target
                  WHERE target.id NOT IN (SELECT id FROM pure_ig_targets)
                    AND (target.current_window_id=plan.profile_id OR target.preferred_window_id=plan.profile_id))
              AND NOT EXISTS (SELECT 1 FROM action_campaigns WHERE profile_id=plan.profile_id)
              AND NOT EXISTS (SELECT 1 FROM follow_monitor_accounts WHERE profile_id=plan.profile_id)
              AND NOT EXISTS (SELECT 1 FROM follow_monitor_dm_accounts WHERE profile_id=plan.profile_id)
              AND NOT EXISTS (SELECT 1 FROM studio_jobs WHERE profile_id=plan.profile_id)
              AND NOT EXISTS (SELECT 1 FROM studio_daily_actions WHERE profile_id=plan.profile_id)
              AND NOT EXISTS (SELECT 1 FROM posting_account_snapshots WHERE profile_id=plan.profile_id)""")
        affected_lease = """profile_id IN (SELECT id FROM pure_ig_leases)
            OR (operation_type='action' AND EXISTS (
                SELECT 1 FROM action_targets target JOIN action_campaigns campaign ON campaign.id=target.campaign_id
                WHERE target.id IN (SELECT id FROM pure_ig_action_targets)
                  AND campaign.id=browser_operation_leases.entity_id
                  AND campaign.owner_user_id=browser_operation_leases.owner_user_id))
            OR EXISTS (SELECT 1 FROM task_targets target
                WHERE target.id IN (SELECT id FROM pure_ig_targets)
                  AND target.current_window_id=browser_operation_leases.profile_id)
            OR EXISTS (SELECT 1 FROM account_window_plans plan
                WHERE plan.id IN (SELECT id FROM pure_ig_plans)
                  AND plan.profile_id=browser_operation_leases.profile_id)"""
        if connection.execute(f"""SELECT 1 FROM browser_operation_leases
            WHERE ({affected_lease}) AND
              (julianday(expires_at) IS NULL OR julianday(expires_at)>julianday('now')) LIMIT 1""").fetchone():
            raise RuntimeError("A Facebook collection window is still occupied; stop it before the pure-IG upgrade")

        # Capture exact definitions; restoring them in the same transaction keeps
        # every surviving IG ledger immutable even on failure or process death.
        guard_names = (
            'trg_workbench_decision_no_delete', 'trg_workbench_dismissal_no_delete',
            'trg_workbench_exclusion_no_delete', 'trg_action_success_no_delete',
            'trg_private_follow_completion_no_delete', 'trg_split_candidate_target_deleted',
        )
        guards = list(connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (" +
            ','.join('?' for _ in guard_names) + ')', guard_names))
        for name, _ in guards:
            connection.execute(f'DROP TRIGGER "{name}"')

        connection.execute("""DELETE FROM report_review_decisions WHERE
            (review_kind='split' AND record_id IN (
                SELECT 'target:'||id FROM pure_ig_completed_targets UNION SELECT 'history:'||id FROM pure_ig_split_history
                UNION SELECT id FROM pure_ig_targets))
            OR (review_kind='private_follow' AND record_id IN (
                SELECT attempt_id FROM private_follow_completions WHERE username_norm GLOB 'fb:*'))""")
        for table in ('workbench_review_decisions', 'workbench_candidate_dismissals'):
            connection.execute(f"DELETE FROM {table} WHERE candidate_id IN (SELECT id FROM pure_ig_candidates)")
        for table in ('task_results', 'task_result_duplicate_archive'):
            connection.execute(f"""DELETE FROM {table} WHERE account_id IN (SELECT id FROM pure_ig_accounts)
                OR task_id IN (SELECT id FROM pure_ig_tasks) OR target_id IN (SELECT id FROM pure_ig_targets)""")
        for table in ('workbench_candidates', 'workbench_collection_exclusions',
                      'workbench_identity_claims', 'global_identity_owners', 'global_seen'):
            connection.execute(f"DELETE FROM {table} WHERE account_id IN (SELECT id FROM pure_ig_accounts)")
        connection.execute("""DELETE FROM instagram_username_aliases WHERE
            account_id IN (SELECT id FROM pure_ig_accounts) OR username_norm GLOB 'fb:*'""")
        # Earlier corrective migrations left a detached, compact repair record.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hover_r64_repair_archive'").fetchone():
            connection.execute("DELETE FROM hover_r64_repair_archive WHERE account_id IN (SELECT id FROM pure_ig_accounts)")
        connection.execute("DELETE FROM instagram_accounts WHERE id IN (SELECT id FROM pure_ig_accounts)")

        # Detached permanent records do not cascade with their former live task.
        for table in ('task_target_recovery_controls', 'split_completed_targets'):
            connection.execute(f"""DELETE FROM {table} WHERE username_norm GLOB 'fb:*'
                OR target_id IN (SELECT id FROM pure_ig_targets)
                OR source_task_id IN (SELECT id FROM pure_ig_tasks)""")
        connection.execute("DELETE FROM split_candidates WHERE id IN (SELECT id FROM pure_ig_split_candidates)")
        connection.execute("DELETE FROM split_candidate_history WHERE id IN (SELECT id FROM pure_ig_split_history)")
        connection.execute("DELETE FROM split_admission_totals WHERE username_norm GLOB 'fb:*'")
        for table in ('action_success_ledger', 'private_follow_completions', 'action_dispatch_claims'):
            connection.execute(f"DELETE FROM {table} WHERE username_norm GLOB 'fb:*'")
        connection.execute("DELETE FROM action_targets WHERE id IN (SELECT id FROM pure_ig_action_targets)")
        # Mixed/shared campaigns remain; removing an FB target does not authorize
        # deleting the IG campaign, its window, scheduling or action counters.
        connection.execute("DELETE FROM task_mode_candidates WHERE username_norm GLOB 'fb:*'")
        connection.execute("DELETE FROM task_targets WHERE id IN (SELECT id FROM pure_ig_targets)")
        connection.execute("DELETE FROM tasks WHERE id IN (SELECT id FROM pure_ig_tasks)")
        # Only positively owned stale leases may be removed. A generic/shared
        # profile binding is sufficient to BLOCK a live lease, never to delete it.
        connection.execute("DELETE FROM browser_operation_leases WHERE profile_id IN (SELECT id FROM pure_ig_leases)")
        connection.execute("DELETE FROM account_window_events WHERE plan_id IN (SELECT id FROM pure_ig_plans)")
        connection.execute("""UPDATE account_window_order SET ids_json=(
            SELECT json_group_array(value) FROM json_each(account_window_order.ids_json)
            WHERE value NOT IN (SELECT id FROM pure_ig_plans))
            WHERE CASE WHEN json_valid(ids_json) THEN json_type(ids_json)='array'
                AND NOT EXISTS (SELECT 1 FROM json_each(ids_json) WHERE type<>'text')
                AND EXISTS (SELECT 1 FROM json_each(ids_json) WHERE value IN (SELECT id FROM pure_ig_plans))
                ELSE 0 END""")
        # Batch receipts only contain plan ids. A mixed receipt is retained;
        # changing its fingerprint/ids would corrupt retry-idempotency for IG.
        connection.execute("""DELETE FROM account_creation_batches WHERE
            CASE WHEN json_valid(result_json) THEN
              json_type(result_json,'$.ids')='array' AND
              json_array_length(result_json,'$.ids')>0 AND NOT EXISTS (
                SELECT 1 FROM json_each(result_json,'$.ids') WHERE type<>'text' OR value NOT IN (SELECT id FROM pure_ig_plans))
            ELSE 0 END""")
        connection.execute("DELETE FROM account_window_plans WHERE id IN (SELECT id FROM pure_ig_plans)")
        connection.execute("""DELETE FROM event_log WHERE
            (entity_type='task' AND entity_id IN (SELECT id FROM pure_ig_tasks)) OR
            (entity_type='task_target' AND entity_id IN (SELECT id FROM pure_ig_targets)) OR
            (entity_type='split_candidate' AND (entity_id IN (SELECT id FROM pure_ig_split_candidates)
                 OR entity_id GLOB 'fb:*')) OR
            (entity_type='workbench_identity' AND entity_id IN (SELECT id FROM pure_ig_accounts)) OR
            (entity_type='workbench_candidate' AND entity_id IN (SELECT id FROM pure_ig_candidates)) OR
            (entity_type='action_target' AND entity_id IN (SELECT id FROM pure_ig_action_targets)) OR
            CASE WHEN json_valid(payload_json)
                 THEN json_extract(payload_json,'$.platform')='facebook'
                   OR json_extract(payload_json,'$.account_id') IN (SELECT id FROM pure_ig_accounts)
                   OR (entity_type IN ('task','task_target') AND
                       json_extract(payload_json,'$.target_id') IN (SELECT id FROM pure_ig_targets))
                   OR (entity_type IN ('action_campaign','action_target') AND
                       json_extract(payload_json,'$.target_id') IN (SELECT id FROM pure_ig_action_targets))
                 ELSE 0 END""")

        # Namespaced orphan entries can be classified without touching unrelated
        # accounts/windows. These IG-only features never infer FB from profile_id.
        for table, columns in (
            ('follow_monitor_accounts', ('username', 'instagram_user_id')),
            ('follow_monitor_dm_accounts', ('username', 'instagram_user_id')),
            ('follow_monitor_members', ('username',)),
            ('follow_monitor_seen', ('username', 'instagram_user_id')),
            ('follow_monitor_latest_follow', ('username', 'owner_username')),
            ('follow_monitor_latest_unfollow', ('username', 'owner_username')),
            ('follow_monitor_latest_dm', ('owner_username',)),
            ('follow_monitor_rounds', ('owner_username',)),
            ('posting_account_snapshots', ('username',)),
        ):
            connection.execute(f"DELETE FROM {table} WHERE " + ' OR '.join(f"{column} GLOB 'fb:*'" for column in columns))

        for _, statement in guards:
            connection.execute(statement)
        # Exact projections after deletes. The cache rebuild deliberately uses
        # the covering expression index, never retained profile payload scans.
        connection.execute("UPDATE global_seen_stats SET total_count=(SELECT COUNT(*) FROM global_seen) WHERE singleton_id=1")
        connection.execute("""UPDATE global_seen_platform_stats SET total_count=(
            SELECT COUNT(*) FROM global_seen seen JOIN instagram_accounts account ON account.id=seen.account_id
            WHERE (account.current_username_norm GLOB 'fb:*')=(platform='facebook'))""")
        connection.execute("DELETE FROM workbench_cache_usage")
        connection.execute("""INSERT INTO workbench_cache_usage
            SELECT owner_user_id,
              SUM(CASE WHEN status='pending' AND review_cache_json<>'{}' THEN 1 ELSE 0 END),
              SUM(CASE WHEN status='pending' THEN MAX(length(CAST(review_cache_json AS BLOB))-2,0) ELSE 0 END),
              SUM(CASE WHEN status<>'pending' AND review_cache_json<>'{}' THEN 1 ELSE 0 END),
              SUM(CASE WHEN status<>'pending' THEN MAX(length(CAST(review_cache_json AS BLOB))-2,0) ELSE 0 END)
            FROM workbench_candidates INDEXED BY idx_workbench_cache_rebuild GROUP BY owner_user_id""")
        connection.execute("DELETE FROM task_mode_candidate_counters")
        connection.execute("""INSERT INTO task_mode_candidate_counters
            SELECT target_id,mode,COUNT(*),SUM(state='pending'),SUM(state='recorded'),SUM(state='deduped'),
                   MAX(discovery_order)+1 FROM task_mode_candidates GROUP BY target_id,mode""")
        connection.execute("DELETE FROM event_log_usage")
        connection.execute("""INSERT INTO event_log_usage SELECT owner_user_id,COUNT(*),
            SUM(length(CAST(payload_json AS BLOB))+length(entity_type)+length(entity_id)+length(event_type)+length(created_at))
            FROM event_log GROUP BY owner_user_id""")
        connection.execute("UPDATE workbench_state_revision SET revision=revision+1,updated_at=datetime('now') WHERE singleton_id=1")
        if {tuple(row) for row in connection.execute("PRAGMA foreign_key_check")} - original_fk_errors:
            raise RuntimeError("Pure-IG migration would introduce a foreign-key violation")
        for name in reversed(sets):
            connection.execute(f"DROP TABLE pure_ig_{name}")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,datetime('now'))",
                           (PURE_IG_MIGRATION_VERSION,))
        connection.execute("RELEASE SAVEPOINT pure_ig_retirement") if nested else connection.commit()
        return True
    except BaseException:
        if nested:
            connection.execute("ROLLBACK TO SAVEPOINT pure_ig_retirement")
            connection.execute("RELEASE SAVEPOINT pure_ig_retirement")
        else:
            connection.rollback()
        raise


class Database:
    """Small SQLite wrapper. Domain writes are serialized through one process lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = threading.RLock()
        # Presentation and acquisition share this fence, without holding a SQLite
        # transaction while waiting for the desktop process. Tokens are process
        # ownership, not TTL guesses; only finished cleanup releases them.
        self.browser_surface_lock = threading.RLock()
        self.live_browser_lease_tokens: set[str] = set()
        self._instance_lock_file = None

    def acquire_instance_lock(self) -> None:
        """Hold an OS lock so a second local core cannot recover/clear live work."""
        if self._instance_lock_file is not None:
            raise RuntimeError("Database instance lock is already held by this process")
        lock_path = Path(f"{self.path}.instance.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RuntimeError("Another IG Audience Collector core is already using this database") from exc
        self._instance_lock_file = handle

    def release_instance_lock(self) -> None:
        handle = self._instance_lock_file
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._instance_lock_file = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            # Keep WAL growth bounded even during long-running desktop sessions.
            # PASSIVE automatic checkpoints never block active readers/writers.
            connection.execute("PRAGMA wal_autocheckpoint=1000")
            return connection
        except BaseException as error:
            # read()/write() cannot close a connection that _connect never
            # returned. Keep the initialization error if close itself fails.
            try:
                connection.close()
            except BaseException as close_error:
                error.add_note(f"SQLite initialization cleanup also failed: {close_error}")
            raise

    def _connect_discovery(self) -> sqlite3.Connection:
        """Open only for an owned DiscoveryWriteSession, never ordinary callers.

        Match _connect's durability settings. A callback's successive drained
        thread calls may use different executor threads; the session's ownership
        guard and this Database's writer lock serialize all connection access.
        """
        connection = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA wal_autocheckpoint=1000")
            return connection
        except BaseException:
            connection.close()
            raise

    def initialize(self) -> None:
        # Direct startup callers (including import/repair tools) receive the same
        # exclusive-process protection as FastAPI lifespan. Reinitializing an
        # active instance must never rewrite a live worker's database underneath it.
        with self._write_lock, self.browser_surface_lock:
            if self.live_browser_lease_tokens:
                raise RuntimeError("Cannot initialize the database while browser work is active")
            temporary_instance_lock = self._instance_lock_file is None
            if temporary_instance_lock:
                self.acquire_instance_lock()
            try:
                self._initialize_locked()
            finally:
                if temporary_instance_lock:
                    self.release_instance_lock()

    def _initialize_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(SCHEMA)
                # Pending live rechecks bind one exact source request to its owner.
                # Existing completed generations remain unchanged; no backfill scans.
                recheck_columns = {row[1] for row in connection.execute("PRAGMA table_info(task_source_rechecks)")}
                for column in ("request_profile_id", "request_lease_token"):
                    if column not in recheck_columns:
                        connection.execute(f"ALTER TABLE task_source_rechecks ADD COLUMN {column} TEXT")
                # Dismissed-card heartbeat selection must seek a single task's
                # latest markers, not join/sort its lifetime target history or
                # scan another task's markers. Backfill only the additive field;
                # the original dismissal identity and timestamp stay unchanged.
                dismissal_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(task_target_list_dismissals)")
                }
                if "task_id" not in dismissal_columns:
                    connection.execute(
                        "ALTER TABLE task_target_list_dismissals "
                        "ADD COLUMN task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE"
                    )
                connection.execute(
                    "UPDATE task_target_list_dismissals SET task_id=("
                    "SELECT task_id FROM task_targets WHERE id=target_id) WHERE task_id IS NULL"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_target_list_dismissals_recent "
                    "ON task_target_list_dismissals(owner_user_id,task_id,dismissed_at DESC,target_id DESC)"
                )
                # Additive completion snapshots preserve the exact source and
                # progress after live cards/checkpoints are archived or removed.
                for table, column in (
                    ('task_targets', 'source_profile_json'),
                    ('split_completed_targets', 'completion_details_json'),
                ):
                    columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                    if column not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
                # Manual review batches are independent of the retired automatic
                # private-secondary tier. Existing and incoming rows begin in 1.
                review_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(workbench_candidates)")
                }
                if "review_stage" not in review_columns:
                    connection.execute(
                        "ALTER TABLE workbench_candidates ADD COLUMN review_stage "
                        "INTEGER NOT NULL DEFAULT 1 CHECK(review_stage IN (1, 2))"
                    )
                if "review_transferred_at" not in review_columns:
                    connection.execute(
                        "ALTER TABLE workbench_candidates ADD COLUMN review_transferred_at TEXT"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_workbench_review_stage ON "
                    "workbench_candidates(owner_user_id,status,visibility,review_stage,created_at,id)"
                )
                # Legacy databases gain review_stage above before either review
                # index can reference it, including the platform covering index.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_candidates_platform_review_stage ON "
                    "workbench_candidates(owner_user_id,status,visibility,review_stage,created_at,id,account_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_target_completed_report ON "
                    "task_targets(julianday(updated_at) DESC,id) WHERE status='completed'"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_split_completed_report ON "
                    "split_candidate_history(owner_user_id,julianday(completed_at) DESC,id) "
                    "WHERE source_status='completed'"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_target_completed_owner_report ON "
                    "task_targets(task_id,julianday(updated_at) DESC,id) WHERE status='completed'"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_split_completed_target_report ON "
                    "split_candidate_history(owner_user_id,source_target_id) WHERE source_status='completed'"
                )
                follow_monitor_account_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(follow_monitor_accounts)"
                    ).fetchall()
                }
                follow_monitor_account_additions = {
                    "previous_following_count": "INTEGER NOT NULL DEFAULT 0",
                    "following_count": "INTEGER NOT NULL DEFAULT 0",
                    "total_added_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_added_count": "INTEGER NOT NULL DEFAULT 0",
                    "total_unfollow_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_unfollow_count": "INTEGER NOT NULL DEFAULT 0",
                    "total_dm_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_dm_count": "INTEGER NOT NULL DEFAULT 0",
                    "baseline_verified": "INTEGER NOT NULL DEFAULT 0",
                }
                for column_name, column_type in follow_monitor_account_additions.items():
                    if column_name not in follow_monitor_account_columns:
                        connection.execute(
                            "ALTER TABLE follow_monitor_accounts "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
                follow_monitor_run_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(follow_monitor_runs)"
                    ).fetchall()
                }
                if "unfollow_count" not in follow_monitor_run_columns:
                    connection.execute(
                        "ALTER TABLE follow_monitor_runs "
                        "ADD COLUMN unfollow_count INTEGER NOT NULL DEFAULT 0"
                    )
                # Rebuild the two small materialized projections once at startup.
                # This is both the upgrade backfill and crash-safety repair: hot
                # collection writes stay O(batch size), while an interrupted
                # process can never leave a stale counter in service.
                connection.execute("DELETE FROM task_mode_candidate_counters")
                connection.execute(
                    """
                    INSERT INTO task_mode_candidate_counters(
                        target_id, mode, total, pending, recorded, deduped,
                        next_discovery_order
                    )
                    SELECT target_id,
                           mode,
                           COUNT(*),
                           SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN state='recorded' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN state='deduped' THEN 1 ELSE 0 END),
                           COALESCE(MAX(discovery_order), 0) + 1
                    FROM task_mode_candidates
                    GROUP BY target_id, mode
                    """
                )
                connection.execute("DELETE FROM workbench_cache_usage")
                connection.execute(
                    """
                    INSERT INTO workbench_cache_usage(
                        owner_user_id, pending_entries, pending_bytes,
                        terminal_entries, terminal_bytes
                    )
                    SELECT owner_user_id,
                           SUM(CASE
                               WHEN status='pending' AND review_cache_json<>'{}'
                               THEN 1 ELSE 0 END),
                           SUM(CASE WHEN status='pending' THEN
                               MAX(length(CAST(review_cache_json AS BLOB)) - 2, 0)
                               ELSE 0 END),
                           SUM(CASE
                               WHEN status<>'pending' AND review_cache_json<>'{}'
                               THEN 1 ELSE 0 END),
                           SUM(CASE WHEN status<>'pending' THEN
                               MAX(length(CAST(review_cache_json AS BLOB)) - 2, 0)
                               ELSE 0 END)
                    FROM workbench_candidates INDEXED BY idx_workbench_cache_rebuild
                    GROUP BY owner_user_id
                    """
                )
                # Authentication sessions are transient credentials, not business
                # history.  Remove expired rows and old revoked rows at startup so
                # a long-lived installation does not accumulate one row per login.
                connection.execute(
                    """
                    DELETE FROM auth_sessions
                    WHERE julianday(expires_at) <= julianday('now')
                       OR (
                            revoked_at IS NOT NULL
                            AND julianday(revoked_at) <= julianday('now', '-30 days')
                       )
                    """
                )
                # ``CREATE TABLE IF NOT EXISTS`` does not add columns to an
                # installed database.  Add the v9 recovery-inbox metadata before
                # creating triggers which write it.  The legacy candidate_id
                # column is retained and made unique after it has been repaired.
                recovery_control_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(task_target_recovery_controls)"
                    ).fetchall()
                }
                recovery_control_additions = {
                    "candidate_id": "TEXT",
                    "username_norm": "TEXT",
                    "username_display": "TEXT",
                    "source_task_id": "TEXT",
                    "source_status": "TEXT",
                    "source_window_id": "TEXT",
                    "last_error": "TEXT",
                }
                for column_name, column_type in recovery_control_additions.items():
                    if column_name not in recovery_control_columns:
                        connection.execute(
                            "ALTER TABLE task_target_recovery_controls "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
                split_candidate_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(split_candidates)"
                    ).fetchall()
                }
                if "dispatch_locked" not in split_candidate_columns:
                    connection.execute(
                        "ALTER TABLE split_candidates ADD COLUMN dispatch_locked "
                        "INTEGER NOT NULL DEFAULT 0 CHECK(dispatch_locked IN (0, 1))"
                    )
                if "claimed_at" not in split_candidate_columns:
                    connection.execute(
                        "ALTER TABLE split_candidates ADD COLUMN claimed_at TEXT"
                    )
                if "manual_category_override" not in split_candidate_columns:
                    connection.execute(
                        "ALTER TABLE split_candidates "
                        "ADD COLUMN manual_category_override TEXT"
                    )
                if "manual_category_at" not in split_candidate_columns:
                    connection.execute(
                        "ALTER TABLE split_candidates ADD COLUMN manual_category_at TEXT"
                    )
                split_history_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(split_candidate_history)"
                    ).fetchall()
                }
                if "manual_category_override" not in split_history_columns:
                    connection.execute(
                        "ALTER TABLE split_candidate_history "
                        "ADD COLUMN manual_category_override TEXT"
                    )
                if "manual_category_at" not in split_history_columns:
                    connection.execute(
                        "ALTER TABLE split_candidate_history "
                        "ADD COLUMN manual_category_at TEXT"
                    )
                task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
                if "last_error" not in task_columns:
                    connection.execute("ALTER TABLE tasks ADD COLUMN last_error TEXT")
                task_target_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(task_targets)"
                    ).fetchall()
                }
                if "current_stage" not in task_target_columns:
                    connection.execute(
                        "ALTER TABLE task_targets ADD COLUMN current_stage TEXT"
                    )
                if "last_success_at" not in task_target_columns:
                    connection.execute(
                        "ALTER TABLE task_targets ADD COLUMN last_success_at TEXT"
                    )
                if "last_error" not in task_target_columns:
                    connection.execute(
                        "ALTER TABLE task_targets ADD COLUMN last_error TEXT"
                    )
                if "allowed_window_ids_json" not in task_target_columns:
                    connection.execute(
                        "ALTER TABLE task_targets ADD COLUMN "
                        "allowed_window_ids_json TEXT NOT NULL DEFAULT '[]'"
                    )
                # Backfill the durable target-level view for databases created by
                # v0.2.24 and earlier.  The latest checkpoint describes the current
                # stage, while a network/error checkpoint must never masquerade as a
                # successful save time.
                connection.execute(
                    """
                    UPDATE task_targets
                    SET current_stage=COALESCE(
                        current_stage,
                        (
                            SELECT checkpoint.stage
                            FROM task_checkpoints checkpoint
                            WHERE checkpoint.target_id=task_targets.id
                            ORDER BY checkpoint.updated_at DESC, checkpoint.rowid DESC
                            LIMIT 1
                        )
                    )
                    WHERE current_stage IS NULL
                    """
                )
                connection.execute(
                    """
                    UPDATE task_targets
                    SET last_success_at=COALESCE(
                        last_success_at,
                        (
                            SELECT MAX(result.updated_at)
                            FROM task_results result
                            WHERE result.target_id=task_targets.id
                        ),
                        (
                            SELECT MAX(checkpoint.updated_at)
                            FROM task_checkpoints checkpoint
                            WHERE checkpoint.target_id=task_targets.id
                              AND checkpoint.stage NOT IN (
                                  'waiting_network',
                                  'interrupted_recoverable',
                                  'mode_unavailable'
                              )
                        )
                    )
                    WHERE last_success_at IS NULL
                    """
                )
                action_target_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(action_targets)").fetchall()
                }
                if "source_target" not in action_target_columns:
                    connection.execute("ALTER TABLE action_targets ADD COLUMN source_target TEXT")
                if "control_after_attempt" not in action_target_columns:
                    connection.execute(
                        "ALTER TABLE action_targets ADD COLUMN control_after_attempt TEXT"
                    )
                action_campaign_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(action_campaigns)").fetchall()
                }
                if "messages_json" not in action_campaign_columns:
                    connection.execute(
                        "ALTER TABLE action_campaigns ADD COLUMN messages_json TEXT NOT NULL DEFAULT '[]'"
                    )
                # Preserve greeting text created by older releases as a one-item
                # message library.  JSON quoting is delegated to SQLite so legacy
                # text containing quotes or line breaks remains valid JSON.
                connection.execute(
                    """
                    UPDATE action_campaigns
                    SET messages_json=json_array(message)
                    WHERE operation='greet'
                      AND message IS NOT NULL
                      AND trim(message)<>''
                      AND (messages_json IS NULL OR messages_json='[]')
                    """
                )
                workbench_revision_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(workbench_state_revision)"
                    ).fetchall()
                }
                if "last_cleanup_at" not in workbench_revision_columns:
                    connection.execute(
                        "ALTER TABLE workbench_state_revision ADD COLUMN last_cleanup_at TEXT"
                    )
                # Terminal review caches are disposable.  This repair is
                # idempotent and protects an upgraded/crash-recovered database
                # even if an older build committed the decision before cleanup.
                # Use the projection rebuilt above, not another payload scan.
                terminal_cache_rows = connection.execute(
                    """
                    SELECT COALESCE(SUM(terminal_entries), 0)
                    FROM workbench_cache_usage
                    """
                ).fetchone()[0]
                if terminal_cache_rows:
                    connection.execute(
                        """
                        UPDATE workbench_candidates
                        SET review_cache_json='{}'
                        WHERE status!='pending' AND review_cache_json!='{}'
                        """
                    )
                    connection.execute(
                        """
                        UPDATE workbench_state_revision
                        SET last_cleanup_at=datetime('now')
                        WHERE singleton_id=1
                        """
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, datetime('now'))"
                )
                # Global Instagram username dedupe is intentionally shared by every
                # local application login. Older builds only prevented duplicates
                # inside one source target. Archive later copies in full before
                # keeping the earliest canonical result in the active table.
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_results_global_account'"
                ).fetchone() is None:
                    duplicate_filter = """EXISTS (
                        SELECT 1 FROM task_results AS earlier
                        WHERE earlier.account_id=candidate.account_id
                          AND (
                              earlier.created_at < candidate.created_at
                              OR (
                                  earlier.created_at = candidate.created_at
                                  AND earlier.rowid < candidate.rowid
                              )
                        )
                    )"""
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(
                            f"""INSERT OR IGNORE INTO task_result_duplicate_archive(
                                original_result_id,owner_user_id,task_id,target_id,account_id,
                                sources_json,visibility,profile_json,screening_json,qualified,
                                created_at,updated_at,archived_at
                            ) SELECT candidate.id,task.owner_user_id,candidate.task_id,
                                     candidate.target_id,candidate.account_id,candidate.sources_json,
                                     candidate.visibility,candidate.profile_json,candidate.screening_json,
                                     candidate.qualified,candidate.created_at,candidate.updated_at,datetime('now')
                              FROM task_results candidate JOIN tasks task ON task.id=candidate.task_id
                              WHERE {duplicate_filter}"""
                        )
                        connection.execute(
                            f"DELETE FROM task_results AS candidate WHERE {duplicate_filter}"
                        )
                        connection.execute(
                            "CREATE UNIQUE INDEX idx_results_global_account ON task_results(account_id)"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(7, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(8, datetime('now'))"
                )
                # v9 introduces delayed split dispatch.  Preserve valid legacy
                # queued->pending associations (their original worker will claim
                # them), but detach broken pointers so the candidate cannot remain
                # permanently undispatchable after an older task was deleted.
                connection.execute(
                    """
                    UPDATE split_candidates
                    SET queued_task_id=NULL, queued_target_id=NULL
                    WHERE queue_state='queued'
                      AND queued_target_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM task_targets target
                        WHERE target.id=split_candidates.queued_target_id
                          AND target.task_id=split_candidates.queued_task_id
                      )
                    """
                )
                # v0.2.25 stored the recovery inbox in split_candidates.  Migrate
                # every legacy failure before deleting those username-keyed rows.
                # A valid queued -> pending association represents an explicit
                # retry and must remain REQUEUED; a prior user dismissal has higher
                # precedence than any status replay.
                connection.execute(
                    """
                    INSERT INTO task_target_recovery_controls(
                        target_id, owner_user_id, candidate_id,
                        username_norm, username_display, state,
                        source_task_id, source_status, source_window_id,
                        last_error, updated_at
                    )
                    SELECT
                        COALESCE(candidate.source_target_id,
                                 'legacy-split:' || candidate.id),
                        candidate.owner_user_id,
                        CASE
                          WHEN EXISTS(
                            SELECT 1
                            FROM task_target_recovery_controls other
                            WHERE other.candidate_id=candidate.id
                              AND other.target_id!=COALESCE(
                                  candidate.source_target_id,
                                  'legacy-split:' || candidate.id
                              )
                          ) THEN lower(hex(randomblob(16)))
                          ELSE candidate.id
                        END,
                        candidate.username_norm,
                        candidate.username_display,
                        CASE
                          WHEN candidate.source_status='dismissed_by_user'
                          THEN 'dismissed'
                          WHEN candidate.queue_state='queued'
                           AND candidate.queued_target_id IS NOT NULL
                           AND EXISTS(
                             SELECT 1 FROM task_targets linked
                             WHERE linked.id=candidate.queued_target_id
                               AND linked.task_id=candidate.queued_task_id
                               AND linked.status='pending'
                           )
                          THEN 'requeued'
                          ELSE 'pending'
                        END,
                        candidate.source_task_id,
                        candidate.source_status,
                        candidate.source_window_id,
                        candidate.last_error,
                        candidate.updated_at
                    FROM split_candidates candidate
                    WHERE candidate.candidate_kind='failure'
                    ON CONFLICT(target_id) DO UPDATE SET
                        candidate_id=COALESCE(
                            task_target_recovery_controls.candidate_id,
                            excluded.candidate_id
                        ),
                        username_norm=COALESCE(
                            NULLIF(task_target_recovery_controls.username_norm, ''),
                            excluded.username_norm
                        ),
                        username_display=COALESCE(
                            NULLIF(task_target_recovery_controls.username_display, ''),
                            excluded.username_display
                        ),
                        state=CASE
                          WHEN task_target_recovery_controls.state='dismissed'
                            OR excluded.state='dismissed'
                          THEN 'dismissed'
                          WHEN task_target_recovery_controls.state='requeued'
                            OR excluded.state='requeued'
                          THEN 'requeued'
                          ELSE 'pending'
                        END,
                        source_task_id=COALESCE(
                            task_target_recovery_controls.source_task_id,
                            excluded.source_task_id
                        ),
                        source_status=COALESCE(
                            task_target_recovery_controls.source_status,
                            excluded.source_status
                        ),
                        source_window_id=COALESCE(
                            task_target_recovery_controls.source_window_id,
                            excluded.source_window_id
                        ),
                        last_error=COALESCE(
                            task_target_recovery_controls.last_error,
                            excluded.last_error
                        ),
                        updated_at=CASE
                          WHEN task_target_recovery_controls.updated_at
                               >= excluded.updated_at
                          THEN task_target_recovery_controls.updated_at
                          ELSE excluded.updated_at
                        END
                    """
                )
                # Also recover unfinished target generations which predate the
                # split inbox or whose legacy username row was overwritten by a
                # same-username sibling.  Per-target keys make every failure
                # independently visible.
                connection.execute(
                    """
                    INSERT INTO task_target_recovery_controls(
                        target_id, owner_user_id, candidate_id,
                        username_norm, username_display, state,
                        source_task_id, source_status, source_window_id,
                        last_error, updated_at
                    )
                    SELECT target.id, task.owner_user_id,
                           lower(hex(randomblob(16))),
                           target.username_norm, target.username_display,
                           'pending', task.id,
                           CASE
                             WHEN target.status IN ('failed', 'recoverable', 'stopped')
                             THEN target.status
                             ELSE task.status
                           END,
                           COALESCE(target.current_window_id,
                                    target.preferred_window_id),
                           COALESCE(target.last_error, task.last_error),
                           CASE
                             WHEN target.updated_at >= task.updated_at
                             THEN target.updated_at ELSE task.updated_at
                           END
                    FROM task_targets target
                    JOIN tasks task ON task.id=target.task_id
                    WHERE target.status IN ('failed', 'recoverable', 'stopped')
                       OR (
                         task.status IN ('failed', 'recoverable', 'stopped')
                         AND target.status!='completed'
                       )
                    ON CONFLICT(target_id) DO UPDATE SET
                        candidate_id=COALESCE(
                            task_target_recovery_controls.candidate_id,
                            excluded.candidate_id
                        ),
                        username_norm=COALESCE(
                            NULLIF(task_target_recovery_controls.username_norm, ''),
                            excluded.username_norm
                        ),
                        username_display=COALESCE(
                            NULLIF(task_target_recovery_controls.username_display, ''),
                            excluded.username_display
                        ),
                        state=task_target_recovery_controls.state,
                        source_task_id=COALESCE(
                            task_target_recovery_controls.source_task_id,
                            excluded.source_task_id
                        ),
                        source_status=COALESCE(
                            task_target_recovery_controls.source_status,
                            excluded.source_status
                        ),
                        source_window_id=COALESCE(
                            task_target_recovery_controls.source_window_id,
                            excluded.source_window_id
                        ),
                        last_error=COALESCE(
                            task_target_recovery_controls.last_error,
                            excluded.last_error
                        ),
                        updated_at=CASE
                          WHEN task_target_recovery_controls.updated_at
                               >= excluded.updated_at
                          THEN task_target_recovery_controls.updated_at
                          ELSE excluded.updated_at
                        END
                    """
                )
                # Repair rows created by the provisional recovery schema: multiple
                # same-username target generations could share one split candidate
                # id.  Candidate ids are API identities and therefore unique.
                connection.execute(
                    """
                    UPDATE task_target_recovery_controls
                    SET candidate_id=lower(hex(randomblob(16)))
                    WHERE candidate_id IS NULL OR trim(candidate_id)=''
                       OR rowid IN (
                         SELECT duplicate.rowid
                         FROM task_target_recovery_controls duplicate
                         JOIN task_target_recovery_controls first
                           ON first.candidate_id=duplicate.candidate_id
                          AND first.rowid < duplicate.rowid
                       )
                    """
                )
                # All historical control rows should now have a source target or a
                # legacy split snapshot.  Keep a deterministic last-resort label so
                # an unexpectedly old partial database still upgrades atomically.
                connection.execute(
                    """
                    UPDATE task_target_recovery_controls
                    SET username_norm=COALESCE(
                            NULLIF(username_norm, ''),
                            'unknown-' || substr(target_id, 1, 12)
                        ),
                        username_display=COALESCE(
                            NULLIF(username_display, ''),
                            'unknown-' || substr(target_id, 1, 12)
                        )
                    WHERE username_norm IS NULL OR trim(username_norm)=''
                       OR username_display IS NULL OR trim(username_display)=''
                    """
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_target_recovery_candidate_id "
                    "ON task_target_recovery_controls(candidate_id)"
                )
                # Preserve the exact v0.2.25 shape which had already created a
                # real pending target for an explicit retry.  Its control is now
                # REQUEUED, while the username row continues as an ordinary manual
                # queue association until that original target is claimed.
                connection.execute(
                    """
                    UPDATE split_candidates
                    SET candidate_kind='manual',
                        source_status='requeued_by_legacy'
                    WHERE candidate_kind='failure'
                      AND queue_state='queued'
                      AND queued_target_id IS NOT NULL
                      AND EXISTS(
                        SELECT 1 FROM task_targets linked
                        WHERE linked.id=split_candidates.queued_target_id
                          AND linked.task_id=split_candidates.queued_task_id
                          AND linked.id=split_candidates.source_target_id
                          AND linked.status='pending'
                      )
                    """
                )
                # Recovery controls are now authoritative.  Remove only legacy
                # FAILURE rows; manual delayed-queue rows remain intact.
                connection.execute(
                    "DELETE FROM split_candidates WHERE candidate_kind='failure'"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_split_candidates_dispatch "
                    "ON split_candidates(owner_user_id, queue_state, queued_at, created_at)"
                )
                connection.executescript(SPLIT_CANDIDATE_TRIGGERS_V9)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(9, datetime('now'))"
                )
                # v10 makes lifecycle transitions explicit.  A CLAIMED row is a
                # running generation; infer its first claim time for upgraded
                # databases, then move already-successful rows into immutable
                # history before installing the completion trigger.
                # v0.2.25's username-wide triggers could attach an unrelated
                # ordinary same-name target without writing the source-generation
                # pair.  Such rows were never genuinely claimed: return them to the
                # delayed queue instead of fabricating completed history.
                connection.execute(
                    """
                    UPDATE split_candidates
                    SET queue_state='queued', queued_task_id=NULL,
                        queued_target_id=NULL, claimed_at=NULL
                    WHERE candidate_kind='manual' AND queue_state='claimed'
                      AND NOT (
                        source_task_id IS NOT NULL
                        AND source_target_id IS NOT NULL
                        AND source_task_id=queued_task_id
                        AND source_target_id=queued_target_id
                        AND EXISTS(
                          SELECT 1 FROM task_targets exact_target
                          WHERE exact_target.id=split_candidates.queued_target_id
                            AND exact_target.task_id=split_candidates.queued_task_id
                        )
                      )
                    """
                )
                connection.execute(
                    """
                    UPDATE split_candidates
                    SET claimed_at=COALESCE(claimed_at, updated_at, queued_at, created_at)
                    WHERE candidate_kind='manual' AND queue_state='claimed'
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO split_candidate_history(
                        id, owner_user_id, username_norm, username_display,
                        source_target, source_task_id, source_target_id,
                        source_status, source_window_id, last_error, profile_json,
                        queued_task_id, queued_target_id, queued_at, claimed_at,
                        completed_at, manual_category_override,
                        manual_category_at, created_at, updated_at
                    )
                    SELECT candidate.id, candidate.owner_user_id,
                           candidate.username_norm, candidate.username_display,
                           candidate.source_target, target.task_id, target.id,
                           'completed',
                           COALESCE(target.current_window_id,
                                    target.preferred_window_id,
                                    candidate.source_window_id),
                           NULL, candidate.profile_json, target.task_id, target.id,
                           COALESCE(candidate.queued_at, candidate.created_at),
                           COALESCE(candidate.claimed_at, target.updated_at),
                           target.updated_at, NULL, NULL,
                           candidate.created_at, target.updated_at
                    FROM split_candidates candidate
                    JOIN task_targets target
                      ON target.id=candidate.queued_target_id
                     AND target.task_id=candidate.queued_task_id
                    WHERE candidate.candidate_kind='manual'
                      AND candidate.queue_state='claimed'
                      AND candidate.source_task_id=candidate.queued_task_id
                      AND candidate.source_target_id=candidate.queued_target_id
                      AND target.status='completed'
                      AND NOT EXISTS(
                        SELECT 1 FROM task_target_recovery_controls recovery
                        WHERE recovery.target_id=target.id
                          AND recovery.state='pending'
                      )
                    """
                )
                connection.execute(
                    """
                    DELETE FROM split_candidates
                    WHERE candidate_kind='manual' AND queue_state='claimed'
                      AND source_task_id=queued_task_id
                      AND source_target_id=queued_target_id
                      AND EXISTS(
                        SELECT 1 FROM task_targets target
                        WHERE target.id=split_candidates.queued_target_id
                          AND target.task_id=split_candidates.queued_task_id
                          AND target.status='completed'
                          AND NOT EXISTS(
                            SELECT 1 FROM task_target_recovery_controls recovery
                            WHERE recovery.target_id=target.id
                              AND recovery.state='pending'
                          )
                      )
                    """
                )
                connection.executescript(SPLIT_CANDIDATE_LIFECYCLE_TRIGGERS_V10)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(10, datetime('now'))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(11, datetime('now'))"
                )
                # Backfill the permanent action-success ledger from every older
                # confirmed attempt.  The earliest success wins the unique
                # owner/operation/account identity; later attempts remain in the
                # immutable campaign history.
                migration_12_applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=12"
                ).fetchone()
                if migration_12_applied is None:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO action_success_ledger(
                            owner_user_id, operation, username_norm, username_display,
                            campaign_id, target_id, attempt_id, completed_at
                        )
                        SELECT campaign.owner_user_id, campaign.operation,
                               target.username_norm, target.username_display,
                               campaign.id, target.id, attempt.id,
                               COALESCE(attempt.finished_at, attempt.started_at)
                        FROM action_attempts attempt
                        JOIN action_targets target ON target.id=attempt.target_id
                        JOIN action_campaigns campaign ON campaign.id=attempt.campaign_id
                        WHERE attempt.status IN ('confirmed', 'already_done')
                        ORDER BY COALESCE(attempt.finished_at, attempt.started_at), attempt.id
                        """
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(12, datetime('now'))"
                )
                # v13 materializes only the global dedupe total.  Reconcile once
                # during upgrade before the INSERT/DELETE triggers maintain it.
                # This avoids a full global_seen scan on every claim/snapshot as
                # the permanent ledger grows into the millions.
                migration_13_applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=13"
                ).fetchone()
                if migration_13_applied is None:
                    connection.execute(
                        """
                        UPDATE global_seen_stats
                        SET total_count=(SELECT COUNT(*) FROM global_seen)
                        WHERE singleton_id=1
                        """
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(13, datetime('now'))"
                )
                # v14 retires public secondary review. Preserve every legacy pending
                # row as immutable collection-exclusion history, but remove its
                # transient review cache and queue row so the upgraded snapshot cannot
                # keep showing a public account whose deterministic conditions failed
                # or could not be confirmed. Private rows are handled later by v31.
                migration_14_applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=14"
                ).fetchone()
                if migration_14_applied is None:
                    legacy_public_secondary_filter = """
                        candidate.status='pending'
                        AND candidate.visibility='public'
                        AND json_valid(candidate.screening_json)
                        AND (
                            lower(trim(COALESCE(CAST(
                                json_extract(candidate.screening_json, '$.review_tier')
                                AS TEXT), '')))='secondary'
                            OR lower(trim(COALESCE(CAST(
                                json_extract(candidate.screening_json, '$.routing_result')
                                AS TEXT), '')))='public_secondary_review'
                        )
                    """
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        legacy_count = int(
                            connection.execute(
                                f"""
                                SELECT COUNT(*)
                                FROM workbench_candidates candidate
                                WHERE {legacy_public_secondary_filter}
                                """
                            ).fetchone()[0]
                        )
                        connection.execute(
                            f"""
                            INSERT OR IGNORE INTO workbench_collection_exclusions(
                                id, account_id, owner_user_id, username_display,
                                reason_code, reason, location_country,
                                profile_snapshot_json, excluded_at
                            )
                            SELECT
                                candidate.id || ':public-secondary-retired',
                                candidate.account_id,
                                candidate.owner_user_id,
                                account.current_username_display,
                                'legacy_public_secondary_review',
                                '公开二审已停用；原账号存在未达标或无法确认的采集条件',
                                CASE
                                  WHEN json_valid(candidate.profile_json)
                                  THEN CAST(json_extract(
                                      candidate.profile_json, '$.location_zh'
                                  ) AS TEXT)
                                  ELSE NULL
                                END,
                                candidate.profile_json,
                                datetime('now')
                            FROM workbench_candidates candidate
                            JOIN instagram_accounts account
                              ON account.id=candidate.account_id
                            WHERE {legacy_public_secondary_filter}
                            """
                        )
                        connection.execute(
                            f"""
                            UPDATE workbench_candidates AS candidate
                            SET review_cache_json='{{}}'
                            WHERE {legacy_public_secondary_filter}
                            """
                        )
                        connection.execute(
                            f"""
                            DELETE FROM workbench_candidates AS candidate
                            WHERE {legacy_public_secondary_filter}
                              AND EXISTS(
                                  SELECT 1
                                  FROM workbench_collection_exclusions exclusion
                                  WHERE exclusion.account_id=candidate.account_id
                              )
                            """
                        )
                        if legacy_count:
                            connection.execute(
                                """
                                UPDATE workbench_state_revision
                                SET revision=revision+1,
                                    updated_at=datetime('now'),
                                    last_cleanup_at=datetime('now')
                                WHERE singleton_id=1
                                """
                            )
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(14, datetime('now'))"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                # v15 adds durable candidate-level window affinity.  SCHEMA creates
                # the table for both new and upgraded databases; this marker keeps
                # the installed schema history explicit and auditable.
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    "VALUES(15, datetime('now'))"
                )
                # v16 adds exact materialized counters for the durable source spool
                # and review-preview cache.  Their startup rebuild above is
                # deliberately idempotent for upgraded v1.0.34 installations.
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    "VALUES(16, datetime('now'))"
                )
                # Preserve observed baselines and all counters on upgrade. Older
                # v17/v18 releases reset these records; that behavior is retired.
                for version in (17, 18):
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))",
                        (version,),
                    )
                monitor_columns = {
                    "follow_monitor_accounts": {
                        "homepage_count": "INTEGER",
                        "previous_homepage_count": "INTEGER",
                        "last_repeat_count": "INTEGER NOT NULL DEFAULT 0",
                        "total_repeat_count": "INTEGER NOT NULL DEFAULT 0",
                    },
                    "follow_monitor_runs": {
                        "check_kind": "TEXT NOT NULL DEFAULT 'legacy'",
                        "repeat_count": "INTEGER NOT NULL DEFAULT 0",
                    },
                    "follow_monitor_daily_counts": {
                        "repeat_count": "INTEGER NOT NULL DEFAULT 0",
                    },
                }
                for table, additions in monitor_columns.items():
                    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    for name, declaration in additions.items():
                        if name not in existing:
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=19").fetchone() is None:
                    # Seed the persistent seen ledger from all retained generations
                    # and surviving result rows, including the initial baseline.
                    connection.execute("""INSERT OR IGNORE INTO follow_monitor_seen
                        SELECT m.owner_user_id, m.profile_id, a.instagram_user_id, lower(m.username),
                               COALESCE(a.checked_at, datetime('now')), COALESCE(a.checked_at, datetime('now'))
                        FROM follow_monitor_members m JOIN follow_monitor_accounts a
                          ON a.owner_user_id=m.owner_user_id AND a.profile_id=m.profile_id""")
                    connection.execute("""INSERT OR IGNORE INTO follow_monitor_seen
                        SELECT f.owner_user_id, f.profile_id, a.instagram_user_id, lower(f.username),
                               f.discovered_at, f.discovered_at
                        FROM follow_monitor_latest_follow f JOIN follow_monitor_accounts a
                          ON a.owner_user_id=f.owner_user_id AND a.profile_id=f.profile_id
                         AND lower(a.username)=lower(f.owner_username)""")
                    connection.execute("""INSERT OR IGNORE INTO follow_monitor_dm_accounts
                        (owner_user_id, profile_id, instagram_user_id, username,
                         total_dm_count, last_dm_count, last_status, last_error, checked_at)
                        SELECT owner_user_id, profile_id, instagram_user_id, username,
                               total_dm_count, last_dm_count, last_status, last_error, checked_at
                        FROM follow_monitor_accounts""")
                    # Legacy count fields did not reliably distinguish homepage
                    # and actual counts. Keep them; unknown homepage stays NULL.
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(19, datetime('now'))"
                    )
                from .studio_schema import initialize_studio_schema
                initialize_studio_schema(connection)
                from .account_workspace import initialize_account_schema
                initialize_account_schema(connection)
                from .native_browser import initialize_native_schema
                initialize_native_schema(connection)
                from .cloud_workspace import initialize_cloud_schema
                initialize_cloud_schema(connection)
                from .account_batch import initialize_batch_schema
                initialize_batch_schema(connection)
                # Retire FB before older business backfills or hover repair can
                # duplicate its historical payloads into another projection/backup.
                _purge_legacy_facebook_data(
                    connection, live_lease_tokens=self.live_browser_lease_tokens)
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=30").fetchone() is None:
                    # Repair all durable business sources and retain their owner
                    # provenance without changing any historical decision/record.
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        from .identity_registry import repair_global_registry
                        repair_global_registry(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(30, datetime('now'))"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=31").fetchone() is None:
                    # Retire the private secondary lane without approving, deleting,
                    # or excluding anyone. Keep the same pending row, profile, cache,
                    # timestamps and identity; preserve the old routing as evidence.
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        migrated = connection.execute(
                            """
                            UPDATE workbench_candidates
                            SET screening_json=json_set(
                                screening_json,
                                '$.legacy_private_review',
                                COALESCE(
                                    json_extract(screening_json, '$.legacy_private_review'),
                                    json_object(
                                        'review_tier', json_extract(screening_json, '$.review_tier'),
                                        'review_reason', json_extract(screening_json, '$.review_reason'),
                                        'routing_result', json_extract(screening_json, '$.routing_result')
                                    )
                                ),
                                '$.review_tier', 'primary',
                                '$.review_reason', 'private_secondary_retired_pending_review',
                                '$.routing_result', 'private_review'
                            )
                            WHERE status='pending' AND visibility='private'
                              AND json_valid(screening_json)
                              AND (
                                lower(trim(COALESCE(CAST(json_extract(
                                    screening_json, '$.review_tier') AS TEXT), '')))='secondary'
                                OR lower(trim(COALESCE(CAST(json_extract(
                                    screening_json, '$.routing_result') AS TEXT), '')))='private_secondary_review'
                              )
                            """
                        ).rowcount
                        if migrated:
                            connection.execute(
                                """
                                UPDATE workbench_state_revision
                                SET revision=revision+1, updated_at=datetime('now')
                                WHERE singleton_id=1
                                """
                            )
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(31, datetime('now'))"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=32").fetchone() is None:
                    # Atomic upgrade backfill: old installs already have retained
                    # events, while newly installed triggers only cover new writes.
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute("DELETE FROM event_log_usage")
                        connection.execute(
                            """
                            INSERT INTO event_log_usage(owner_user_id, entries, bytes)
                            SELECT owner_user_id, COUNT(*), SUM(
                                length(CAST(payload_json AS BLOB)) + length(entity_type)
                                + length(entity_id) + length(event_type) + length(created_at)
                            ) FROM event_log GROUP BY owner_user_id
                            """
                        )
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(32, datetime('now'))"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=34").fetchone() is None:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        from .split_admissions import backfill_split_completions
                        backfill_split_completions(connection)
                        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(34,datetime('now'))")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=33").fetchone() is None:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        from .split_admissions import backfill_split_admissions
                        backfill_split_admissions(connection)
                        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(33,datetime('now'))")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=35").fetchone() is None:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        from .split_completion_details import backfill_completion_details
                        backfill_completion_details(connection)
                        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(35,datetime('now'))")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=36").fetchone() is None:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        from .private_follow_reviews import capture_private_follow_completions
                        capture_private_follow_completions(connection)
                        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(36,datetime('now'))")
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                hover_migration_needed = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=37"
                ).fetchone() is None
                legacy_hover_rows_present = connection.execute(
                    "SELECT 1 FROM workbench_collection_exclusions "
                    "WHERE reason_code='hover_preview_unavailable' LIMIT 1"
                ).fetchone() is not None
                if hover_migration_needed or legacy_hover_rows_present:
                    # A prior version may have recorded false exclusions AFTER
                    # r64 first ran, for example when an older Core was started
                    # again against the same database. Recheck on every startup.
                    bad_hover = """
                        SELECT exclusion.account_id
                        FROM workbench_collection_exclusions exclusion
                        WHERE exclusion.reason_code='hover_preview_unavailable'
                          AND json_valid(exclusion.profile_snapshot_json)
                          AND json_extract(exclusion.profile_snapshot_json,
                                           '$.page_read_reason')='hover_card_missing_or_incomplete'
                          AND NOT EXISTS (SELECT 1 FROM workbench_candidates candidate
                                          WHERE candidate.account_id=exclusion.account_id)
                          AND NOT EXISTS (SELECT 1 FROM task_result_duplicate_archive archived
                                          WHERE archived.account_id=exclusion.account_id)
                          AND NOT EXISTS (
                              SELECT 1 FROM task_results result
                              WHERE result.account_id=exclusion.account_id
                                AND (NOT json_valid(result.screening_json)
                                  OR NOT json_valid(result.profile_json)
                                  OR COALESCE(json_extract(result.screening_json,
                                             '$.review_reason'),'')!='hover_preview_unavailable'
                                  OR COALESCE(json_extract(result.profile_json,
                                             '$.page_read_status'),'')!='hover_preview_unavailable')
                          )
                    """
                    if connection.execute("SELECT 1 FROM (" + bad_hover + ") LIMIT 1").fetchone():
                        prefix = f"{self.path}.pre-hover-r{'64' if hover_migration_needed else '65'}"
                        backup_path = Path(prefix + '.bak')
                        for sequence in range(2, 1000):
                            if not backup_path.exists():
                                break
                            backup_path = Path(f"{prefix}-{sequence}.bak")
                        if backup_path.exists():
                            raise RuntimeError("No free hover repair backup filename")
                        temporary_backup = Path(f"{backup_path}.{uuid.uuid4().hex}.tmp")
                        try:
                            with closing(sqlite3.connect(temporary_backup)) as backup:
                                connection.backup(backup)
                            os.replace(temporary_backup, backup_path)
                        finally:
                            temporary_backup.unlink(missing_ok=True)
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute("CREATE TEMP TABLE hover_r64_repair(account_id TEXT PRIMARY KEY)")
                        connection.execute("INSERT INTO hover_r64_repair " + bad_hover)
                        connection.execute("""CREATE TABLE IF NOT EXISTS hover_r64_repair_archive(
                            account_id TEXT PRIMARY KEY, username_display TEXT NOT NULL,
                            exclusion_id TEXT NOT NULL, repaired_at TEXT NOT NULL
                        )""")
                        connection.execute("""
                            INSERT OR IGNORE INTO hover_r64_repair_archive
                            SELECT exclusion.account_id,exclusion.username_display,
                                   exclusion.id,datetime('now')
                            FROM workbench_collection_exclusions exclusion
                            JOIN hover_r64_repair repair ON repair.account_id=exclusion.account_id
                        """)
                        # The exclusion ledger is normally immutable. This
                        # one-time corrective migration retracts the exact
                        # false record, then restores its delete guard in the
                        # same transaction before any other core work begins.
                        connection.execute("DROP TRIGGER trg_workbench_exclusion_no_delete")
                        for table in (
                            'task_results', 'workbench_collection_exclusions',
                            'workbench_identity_claims', 'global_identity_owners',
                            'global_seen',
                        ):
                            connection.execute(
                                f"DELETE FROM {table} WHERE account_id IN "
                                "(SELECT account_id FROM hover_r64_repair)"
                            )
                        connection.execute("""CREATE TRIGGER trg_workbench_exclusion_no_delete
                            BEFORE DELETE ON workbench_collection_exclusions
                            BEGIN SELECT RAISE(ABORT, 'workbench collection exclusions are immutable'); END""")
                        connection.execute("""UPDATE workbench_state_revision
                            SET revision=revision+1,updated_at=datetime('now')
                            WHERE singleton_id=1 AND EXISTS(SELECT 1 FROM hover_r64_repair)""")
                        connection.execute("DROP TABLE hover_r64_repair")
                        connection.execute(
                            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(37,datetime('now'))"
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                # event_log is diagnostic/audit telemetry only; authoritative
                # decisions, exclusions, action successes and global dedupe live
                # in their own immutable tables above.  Bound diagnostics per
                # owner by both age and count without touching business ledgers.
                _prune_diagnostic_event_log(connection)
                _prune_terminal_candidate_spool(connection)
                connection.execute(
                    """
                    UPDATE workbench_state_revision
                    SET last_cleanup_at=datetime('now')
                    WHERE singleton_id=1
                    """
                )
                from .workbench_aggregates import initialize_workbench_aggregates
                initialize_workbench_aggregates(connection)
                from .workbench_progress_aggregates import initialize_workbench_progress_aggregates
                initialize_workbench_progress_aggregates(connection)
            finally:
                # sqlite3.Connection's context manager commits or rolls back but does not
                # close the handle.  An explicit close is required on Windows so temporary
                # test databases and upgraded app data can be removed immediately.
                connection.close()

    def maintain_transient_data(
        self,
        *,
        minimum_interval_seconds: int = EVENT_LOG_MAINTENANCE_INTERVAL_SECONDS,
    ) -> bool:
        """Run low-frequency transient cleanup while the application is alive."""

        minimum_interval_seconds = max(60, int(minimum_interval_seconds))
        with self.read() as connection:
            due = bool(
                connection.execute(
                    """
                    SELECT last_cleanup_at IS NULL
                        OR julianday(last_cleanup_at) <=
                           julianday('now', '-' || ? || ' seconds')
                    FROM workbench_state_revision WHERE singleton_id=1
                    """,
                    (minimum_interval_seconds,),
                ).fetchone()[0]
            )
        if not due:
            return False
        with self.try_write() as connection:
            if connection is None:
                return False
            due = bool(
                connection.execute(
                    """
                    SELECT last_cleanup_at IS NULL
                        OR julianday(last_cleanup_at) <=
                           julianday('now', '-' || ? || ' seconds')
                    FROM workbench_state_revision WHERE singleton_id=1
                    """,
                    (minimum_interval_seconds,),
                ).fetchone()[0]
            )
            if not due:
                return False
            _prune_diagnostic_event_log(connection)
            connection.execute(
                """
                DELETE FROM auth_sessions
                WHERE julianday(expires_at) <= julianday('now')
                   OR (
                       revoked_at IS NOT NULL
                       AND julianday(revoked_at) <= julianday('now', '-30 days')
                   )
                """
            )
            connection.execute(
                """
                UPDATE workbench_candidates
                SET review_cache_json='{}'
                WHERE status!='pending' AND review_cache_json!='{}'
                """
            )
            _prune_terminal_candidate_spool(connection)
            connection.execute(
                """
                UPDATE workbench_state_revision
                SET last_cleanup_at=datetime('now')
                WHERE singleton_id=1
                """
            )
        return True

    @contextmanager
    def try_write(self) -> Iterator[sqlite3.Connection | None]:
        """Optional cleanup must never queue a WAL reader behind a writer.

        Skipped cleanup keeps its old watermark so the next idle pass retries.
        Both the in-process write fence and SQLite's cross-process writer lock
        are probed without waiting; real schema/storage errors still propagate.
        """
        if not self._write_lock.acquire(blocking=False):
            yield None
            return
        connection = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA busy_timeout=0")
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", None)
                if isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    yield None
                    return
                raise
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._write_lock.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def integrity_check(self) -> str:
        with self.read() as connection:
            return str(connection.execute("PRAGMA quick_check").fetchone()[0])

    def liveness_check(self) -> str:
        """Return a constant-time database readiness result.

        ``PRAGMA quick_check`` is intentionally kept in :meth:`integrity_check`
        for startup diagnostics and explicit verification.  The desktop Core
        supervisor calls the health endpoint every few seconds, so that path must
        not scan a growing production database or mistake slow disk I/O for a
        crashed Core process.
        """

        with self.read() as connection:
            row = connection.execute("SELECT 1").fetchone()
        return "ok" if row is not None and int(row[0]) == 1 else "unavailable"
