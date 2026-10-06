-- Exact initialized schema exported from delivered R6.2 commit 252590e257fbbc0e1974ad727e2086ab858bb2c6
CREATE TABLE account_creation_batches(
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),request_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(owner_user_id,request_key));

CREATE TABLE account_window_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        plan_id TEXT NOT NULL, name TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE account_window_open_state (
        plan_id TEXT PRIMARY KEY REFERENCES account_window_plans(id) ON DELETE CASCADE,
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        opened INTEGER NOT NULL CHECK(opened IN (0,1)), updated_at TEXT NOT NULL);

CREATE TABLE account_window_order (owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id), ids_json TEXT NOT NULL);

CREATE TABLE account_window_plans (
        id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        serial INTEGER NOT NULL, name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '', environment_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(owner_user_id,serial));

CREATE TABLE action_attempts (
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

CREATE TABLE action_campaigns (
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

CREATE TABLE action_counter_resets (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('follow', 'greet')),
    profile_id TEXT NOT NULL,
    reset_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, operation, profile_id)
);

CREATE TABLE action_dispatch_claims (
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

CREATE TABLE action_success_ledger (
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

CREATE TABLE action_targets (
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

CREATE TABLE app_users (
    id TEXT PRIMARY KEY,
    username_norm TEXT NOT NULL UNIQUE,
    username_display TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    remember_login INTEGER NOT NULL DEFAULT 0 CHECK (remember_login IN (0, 1)),
    auto_login INTEGER NOT NULL DEFAULT 0 CHECK (auto_login IN (0, 1)),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE browser_operation_leases (
    profile_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE cloud_workspace_links(
        owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id), cloud_user_id TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, digest TEXT NOT NULL DEFAULT '', last_sync_at TEXT);

CREATE TABLE collection_dispatch_locks (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE event_log_usage (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    entries INTEGER NOT NULL DEFAULT 0 CHECK(entries >= 0),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes >= 0)
);

CREATE TABLE follow_monitor_accounts (
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
    checked_at TEXT, homepage_count INTEGER, previous_homepage_count INTEGER, last_repeat_count INTEGER NOT NULL DEFAULT 0, total_repeat_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_user_id, profile_id)
);

CREATE TABLE follow_monitor_daily_counts (
    owner_user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    added_count INTEGER NOT NULL DEFAULT 0, repeat_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_user_id, day)
);

CREATE TABLE follow_monitor_dm_accounts (
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

CREATE TABLE follow_monitor_latest_dm (
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

CREATE TABLE follow_monitor_latest_follow (
    owner_user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    username TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id, username)
);

CREATE TABLE follow_monitor_latest_unfollow (
    owner_user_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_username TEXT NOT NULL,
    username TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, batch_id, profile_id, username)
);

CREATE TABLE follow_monitor_members (
    owner_user_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    username TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id, generation, username)
);

CREATE TABLE follow_monitor_rounds (
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

CREATE TABLE follow_monitor_runs (
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
, check_kind TEXT NOT NULL DEFAULT 'legacy', repeat_count INTEGER NOT NULL DEFAULT 0);

CREATE TABLE follow_monitor_seen (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    instagram_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id, instagram_user_id, username)
);

CREATE TABLE global_identity_owners (
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, account_id)
);

CREATE TABLE global_seen (
    account_id TEXT PRIMARY KEY REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    sources_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE global_seen_platform_stats (
    platform TEXT PRIMARY KEY CHECK(platform IN ('instagram','facebook')),
    total_count INTEGER NOT NULL CHECK(total_count>=0)
);

CREATE TABLE global_seen_stats (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    total_count INTEGER NOT NULL CHECK(total_count>=0)
);

CREATE TABLE hover_r64_repair_archive(
                            account_id TEXT PRIMARY KEY, username_display TEXT NOT NULL,
                            exclusion_id TEXT NOT NULL, repaired_at TEXT NOT NULL
                        );

CREATE TABLE instagram_accounts (
    id TEXT PRIMARY KEY,
    instagram_user_id TEXT UNIQUE,
    current_username_norm TEXT NOT NULL UNIQUE,
    current_username_display TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE instagram_username_aliases (
    account_id TEXT NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL UNIQUE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id, username_norm)
);

CREATE TABLE native_browser_profiles (
        id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        serial INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
        proxy_server TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE posting_account_snapshots(
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),profile_id TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT '',posts_count INTEGER,status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',checked_at TEXT NOT NULL, followers_count INTEGER, following_count INTEGER,
        PRIMARY KEY(owner_user_id,profile_id));

CREATE TABLE posting_api_backoff(id INTEGER PRIMARY KEY CHECK(id=1),until_epoch REAL NOT NULL DEFAULT 0);

CREATE TABLE posting_api_cache(cache_key TEXT PRIMARY KEY,response_json TEXT NOT NULL,expires_at REAL NOT NULL);

CREATE TABLE posting_api_usage(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0);

CREATE TABLE posting_assets(id TEXT PRIMARY KEY,provider TEXT NOT NULL DEFAULT 'pexels',
        provider_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,sha256 TEXT,render_sha256 TEXT,path TEXT NOT NULL DEFAULT '',
        recovery_path TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'reserved',source_url TEXT NOT NULL,
        photographer TEXT NOT NULL,photographer_url TEXT NOT NULL DEFAULT '',download_url TEXT NOT NULL,
        preview TEXT NOT NULL DEFAULT '',source_license TEXT NOT NULL DEFAULT '',license_url TEXT NOT NULL DEFAULT '',credit TEXT NOT NULL DEFAULT '',verified_at TEXT NOT NULL DEFAULT '',provider_sha1 TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(provider,provider_id));

CREATE TABLE posting_jobs(id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,
        request_key TEXT NOT NULL,provider_name TEXT NOT NULL DEFAULT 'pexels',theme TEXT NOT NULL,caption TEXT NOT NULL,profile_id TEXT NOT NULL DEFAULT '',
        expected_username TEXT NOT NULL DEFAULT '',expected_actor_id TEXT NOT NULL DEFAULT '',asset_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'preparing',
        lease_token TEXT NOT NULL DEFAULT '',attempt_id TEXT NOT NULL DEFAULT '',submitted_at TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,hidden_at TEXT,message TEXT NOT NULL DEFAULT '', failure_stage TEXT NOT NULL DEFAULT '', failure_code TEXT NOT NULL DEFAULT '',
        UNIQUE(owner_user_id,request_key));

CREATE TABLE posting_receipts(job_id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,username TEXT NOT NULL,post_url TEXT NOT NULL DEFAULT '',confirmed_at TEXT NOT NULL,
        day_utc TEXT NOT NULL,evidence_json TEXT NOT NULL);

CREATE TABLE posting_retry_history(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,attempt_id TEXT NOT NULL DEFAULT '',submitted_at TEXT,
        failure_stage TEXT NOT NULL,failure_code TEXT NOT NULL,retried_at TEXT NOT NULL);

CREATE TABLE private_follow_completions (
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

CREATE TABLE report_review_decisions (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    review_kind TEXT NOT NULL CHECK(review_kind IN ('split', 'private_follow')),
    record_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('passed', 'failed')),
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, review_kind, record_id)
);

CREATE TABLE retired_account_profiles (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, profile_id)
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE split_admission_totals (
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    successful_adds INTEGER NOT NULL DEFAULT 0 CHECK(successful_adds>=0),
    history_complete INTEGER NOT NULL DEFAULT 0 CHECK(history_complete IN (0,1)),
    has_executed INTEGER NOT NULL DEFAULT 0 CHECK(has_executed IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, username_norm)
);

CREATE TABLE split_candidate_history (
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

CREATE TABLE split_candidate_window_affinity (
    candidate_id TEXT NOT NULL REFERENCES split_candidates(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    PRIMARY KEY(candidate_id, profile_id),
    UNIQUE(candidate_id, queue_order)
);

CREATE TABLE split_candidates (
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

CREATE TABLE split_completed_targets (
    target_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    username_norm TEXT NOT NULL,
    username_display TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_window_id TEXT,
    completed_at TEXT NOT NULL,
    completion_details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE studio_assets(
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
            source TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, media_type TEXT NOT NULL,
            preview TEXT NOT NULL DEFAULT '', attribution TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL);

CREATE TABLE studio_daily_actions(
            owner_user_id TEXT NOT NULL REFERENCES app_users(id), profile_id TEXT NOT NULL,
            day TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_user_id,profile_id,day,action));

CREATE TABLE studio_jobs(
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
            request_key TEXT NOT NULL, kind TEXT NOT NULL, profile_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued', config_json TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
            total_steps INTEGER NOT NULL DEFAULT 0, inflight INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT, source_draft_id TEXT NOT NULL DEFAULT '', draft_target_profile_id TEXT NOT NULL DEFAULT '',
            UNIQUE(owner_user_id,request_key));

CREATE TABLE studio_templates(
            owner_user_id TEXT NOT NULL REFERENCES app_users(id), kind TEXT NOT NULL,
            config_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_user_id,kind));

CREATE TABLE task_automatic_completions (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'cleanup_pending' CHECK(state IN ('cleanup_pending','dismissed')),
    updated_at TEXT NOT NULL
);

CREATE TABLE task_checkpoints (
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

CREATE TABLE task_list_dismissals (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL
);

CREATE TABLE task_mode_candidate_counters (
    target_id TEXT NOT NULL REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('followers', 'following', 'post_likers')),
    total INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    recorded INTEGER NOT NULL DEFAULT 0,
    deduped INTEGER NOT NULL DEFAULT 0,
    next_discovery_order INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(target_id, mode)
);

CREATE TABLE task_mode_candidates (
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

CREATE TABLE task_parent_reel_decisions (
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

CREATE TABLE task_result_duplicate_archive (
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

CREATE TABLE task_results (
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

CREATE TABLE task_source_rechecks (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('followers', 'following')),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'active', 'resuming', 'completed')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    request_profile_id TEXT,
    request_lease_token TEXT
);

CREATE TABLE task_target_list_dismissals (
    target_id TEXT PRIMARY KEY REFERENCES task_targets(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE task_target_recovery_controls (
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

CREATE TABLE task_targets (
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

CREATE TABLE task_windows (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    queue_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'selected',
    PRIMARY KEY(task_id, profile_id),
    UNIQUE(task_id, queue_order)
);

CREATE TABLE tasks (
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

CREATE TABLE workbench_actionable_candidates(
        candidate_id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,account_id TEXT NOT NULL,
        visibility TEXT NOT NULL,reviewed_at TEXT);

CREATE TABLE workbench_aggregate_members(
        kind TEXT NOT NULL,record_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,bucket TEXT NOT NULL,
        PRIMARY KEY(kind,record_id));

CREATE TABLE workbench_aggregate_state(
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), rebuilding INTEGER NOT NULL DEFAULT 0);

CREATE TABLE workbench_cache_usage (
    owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
    pending_entries INTEGER NOT NULL DEFAULT 0,
    pending_bytes INTEGER NOT NULL DEFAULT 0,
    terminal_entries INTEGER NOT NULL DEFAULT 0,
    terminal_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE workbench_candidate_dismissals (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES workbench_candidates(id) ON DELETE RESTRICT,
    owner_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    dismissed_at TEXT NOT NULL
);

CREATE TABLE workbench_candidates (
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

CREATE TABLE workbench_collection_exclusions (
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

CREATE TABLE workbench_identity_claims (
    account_id TEXT PRIMARY KEY REFERENCES instagram_accounts(id) ON DELETE RESTRICT,
    claimed_by_user_id TEXT NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
    source TEXT NOT NULL,
    source_target TEXT,
    claimed_at TEXT NOT NULL
);

CREATE TABLE workbench_progress_contributions (
            account_id TEXT NOT NULL, kind TEXT NOT NULL,
            target_id TEXT NOT NULL, mode TEXT NOT NULL,
            qualified INTEGER NOT NULL, recorded_total INTEGER NOT NULL,
            excluded_total INTEGER NOT NULL, discarded INTEGER NOT NULL,
            hover_discarded INTEGER NOT NULL,
            PRIMARY KEY(account_id, kind, target_id, mode)
        ) WITHOUT ROWID;

CREATE TABLE workbench_progress_totals (
            target_id TEXT NOT NULL, mode TEXT NOT NULL,
            qualified INTEGER NOT NULL DEFAULT 0,
            recorded_total INTEGER NOT NULL DEFAULT 0,
            excluded_total INTEGER NOT NULL DEFAULT 0,
            discarded INTEGER NOT NULL DEFAULT 0,
            hover_discarded INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(target_id, mode)
        ) WITHOUT ROWID;

CREATE TABLE workbench_review_decisions (
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

CREATE TABLE workbench_snapshot_counts(
        owner_user_id TEXT NOT NULL,metric TEXT NOT NULL,total INTEGER NOT NULL CHECK(total>=0),
        PRIMARY KEY(owner_user_id,metric));

CREATE TABLE workbench_state_revision (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at TEXT NOT NULL,
    last_cleanup_at TEXT
);

CREATE VIEW workbench_actionable_refresh AS SELECT CAST(NULL AS TEXT) AS candidate_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_candidate AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_claim AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_dismissal AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_exclusion AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_result AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_split AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_aggregate_refresh_success AS SELECT CAST(NULL AS TEXT) AS record_id WHERE 0;

CREATE VIEW workbench_progress_refresh AS
            SELECT CAST(NULL AS TEXT) AS account_id WHERE 0;

CREATE INDEX account_events_owner ON account_window_events(owner_user_id,seq DESC);

CREATE INDEX account_window_profile_archive ON account_window_plans(profile_id,archived);

CREATE UNIQUE INDEX account_window_unique_binding ON account_window_plans(profile_id) WHERE profile_id<>'' AND archived=0;

CREATE INDEX idx_accounts_platform_scope
ON instagram_accounts(id,current_username_norm);

CREATE INDEX idx_action_attempts_campaign ON action_attempts(campaign_id, started_at DESC);

CREATE INDEX idx_action_success_campaign_owner
ON action_success_ledger(campaign_id, owner_user_id, operation, completed_at);

CREATE INDEX idx_action_success_owner_time
ON action_success_ledger(owner_user_id, completed_at DESC, username_norm);

CREATE INDEX idx_action_targets_queue ON action_targets(campaign_id, queue_order);

CREATE INDEX idx_actions_report_period
ON action_success_ledger(owner_user_id,julianday(completed_at),operation,campaign_id,attempt_id);

CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id);

CREATE INDEX idx_automatic_completion_lease
ON task_automatic_completions(profile_id,lease_token,state);

CREATE INDEX idx_browser_leases_entity ON browser_operation_leases(entity_id);

CREATE INDEX idx_campaigns_owner_profile
ON action_campaigns(owner_user_id, profile_id, id);

CREATE INDEX idx_campaigns_owner_updated ON action_campaigns(owner_user_id, updated_at DESC);

CREATE INDEX idx_campaigns_profile_status ON action_campaigns(profile_id, status);

CREATE INDEX idx_candidates_platform_counts
ON workbench_candidates(owner_user_id,status,visibility,account_id,id);

CREATE INDEX idx_candidates_platform_created
ON workbench_candidates(owner_user_id,status,visibility,created_at,id,account_id);

CREATE INDEX idx_candidates_platform_review_stage ON workbench_candidates(owner_user_id,status,visibility,review_stage,created_at,id,account_id);

CREATE INDEX idx_candidates_platform_reviewed
ON workbench_candidates(owner_user_id,status,visibility,reviewed_at DESC,id DESC,account_id);

CREATE INDEX idx_candidates_report_identity
ON workbench_candidates(account_id,owner_user_id,julianday(created_at),created_at);

CREATE INDEX idx_candidates_report_period
ON workbench_candidates(owner_user_id,julianday(created_at),account_id,id,created_at);

CREATE INDEX idx_candidates_report_reviewed
ON workbench_candidates(owner_user_id,status,julianday(reviewed_at),account_id);

CREATE INDEX idx_checkpoints_task ON task_checkpoints(task_id, updated_at DESC);

CREATE INDEX idx_events_owner_seq ON event_log(owner_user_id, seq);

CREATE INDEX idx_exclusions_aggregate_rebuild ON workbench_collection_exclusions(owner_user_id,username_display,id);

CREATE INDEX idx_exclusions_legacy_hover
ON workbench_collection_exclusions(reason_code)
WHERE reason_code='hover_preview_unavailable';

CREATE INDEX idx_exclusions_platform_counts
ON workbench_collection_exclusions(owner_user_id,username_display);

CREATE INDEX idx_exclusions_platform_page
ON workbench_collection_exclusions(owner_user_id,excluded_at DESC,id DESC,username_display);

CREATE INDEX idx_exclusions_report_identity
ON workbench_collection_exclusions(account_id,owner_user_id,julianday(excluded_at),excluded_at);

CREATE INDEX idx_exclusions_report_period
ON workbench_collection_exclusions(owner_user_id,julianday(excluded_at),account_id,id,excluded_at);

CREATE INDEX idx_follow_monitor_members_generation
ON follow_monitor_members(owner_user_id, profile_id, generation);

CREATE INDEX idx_follow_monitor_runs_owner
ON follow_monitor_runs(owner_user_id, started_at DESC);

CREATE INDEX idx_follow_rounds_report_period
ON follow_monitor_rounds(owner_user_id,julianday(checked_at),added_count);

CREATE INDEX idx_global_identity_owners_account
ON global_identity_owners(account_id);

CREATE INDEX idx_private_follow_completion_report
ON private_follow_completions(owner_user_id,julianday(completed_at) DESC,attempt_id);

CREATE INDEX idx_result_duplicate_archive_account
ON task_result_duplicate_archive(account_id);

CREATE INDEX idx_result_duplicate_archive_owner
ON task_result_duplicate_archive(owner_user_id, archived_at DESC);

CREATE INDEX idx_results_aggregate_rebuild ON task_results(task_id,account_id,visibility,id);

CREATE UNIQUE INDEX idx_results_global_account ON task_results(account_id);

CREATE INDEX idx_results_progress_provenance
ON task_results(target_id, account_id, task_id, sources_json);

CREATE INDEX idx_results_report_identity
ON task_results(account_id,julianday(created_at),task_id,created_at);

CREATE INDEX idx_results_report_period
ON task_results(julianday(created_at),account_id,task_id,id,created_at);

CREATE INDEX idx_results_task_updated ON task_results(task_id, updated_at DESC);

CREATE INDEX idx_results_task_visibility
ON task_results(task_id, visibility);

CREATE INDEX idx_split_candidates_dispatch ON split_candidates(owner_user_id, queue_state, queued_at, created_at);

CREATE INDEX idx_split_candidates_owner_state
ON split_candidates(owner_user_id, queue_state, updated_at DESC);

CREATE INDEX idx_split_completed_report ON split_candidate_history(owner_user_id,julianday(completed_at) DESC,id) WHERE source_status='completed';

CREATE INDEX idx_split_completed_target_report ON split_candidate_history(owner_user_id,source_target_id) WHERE source_status='completed';

CREATE INDEX idx_split_completed_target_username
ON split_completed_targets(owner_user_id,username_norm);

CREATE INDEX idx_split_completed_targets_report_period
ON split_completed_targets(owner_user_id,julianday(completed_at) DESC,target_id);

CREATE INDEX idx_split_history_aggregate_rebuild ON split_candidate_history(owner_user_id,username_norm,id);

CREATE INDEX idx_split_history_owner_completed
ON split_candidate_history(owner_user_id, completed_at DESC, id);

CREATE INDEX idx_split_history_owner_username
ON split_candidate_history(owner_user_id, username_norm);

CREATE INDEX idx_split_history_report_identity
ON split_candidate_history(owner_user_id,source_target_id,source_status,julianday(completed_at));

CREATE INDEX idx_split_history_report_period
ON split_candidate_history(owner_user_id,source_status,julianday(completed_at),source_target_id,id);

CREATE INDEX idx_split_window_affinity_profile
ON split_candidate_window_affinity(profile_id, candidate_id);

CREATE INDEX idx_success_aggregate_key ON action_success_ledger(json_array(owner_user_id,operation,username_norm));

CREATE INDEX idx_target_completed_owner_report ON task_targets(task_id,julianday(updated_at) DESC,id) WHERE status='completed';

CREATE INDEX idx_target_completed_report ON task_targets(julianday(updated_at) DESC,id) WHERE status='completed';

CREATE UNIQUE INDEX idx_target_recovery_candidate_id ON task_target_recovery_controls(candidate_id);

CREATE INDEX idx_target_recovery_owner_state
ON task_target_recovery_controls(owner_user_id, state, updated_at DESC);

CREATE INDEX idx_target_recovery_owner_username
ON task_target_recovery_controls(owner_user_id,username_norm);

CREATE INDEX idx_targets_task_live_occupied
ON task_targets(task_id, id)
WHERE current_window_id IS NOT NULL AND status NOT IN ('completed', 'failed', 'stopped');

CREATE INDEX idx_targets_task_occupied
ON task_targets(task_id, id) WHERE current_window_id IS NOT NULL;

CREATE INDEX idx_targets_task_queue ON task_targets(task_id, queue_order);

CREATE INDEX idx_targets_task_snapshot_priority
ON task_targets(task_id,
    CASE WHEN status IN ('running', 'waiting_network') THEN 0
         WHEN status IN ('failed', 'recoverable') THEN 1
         WHEN status IN ('pending', 'paused') THEN 2 ELSE 3 END,
    queue_order, id)
WHERE username_norm NOT GLOB 'fb:*';

CREATE INDEX idx_targets_task_updated
ON task_targets(task_id, updated_at DESC, id DESC);

CREATE INDEX idx_task_list_dismissals_owner
ON task_list_dismissals(owner_user_id, dismissed_at DESC);

CREATE INDEX idx_task_mode_candidates_pending
ON task_mode_candidates(target_id, mode, state, discovery_order);

CREATE INDEX idx_task_mode_candidates_state_username
ON task_mode_candidates(target_id, mode, state, username_norm);

CREATE INDEX idx_task_mode_candidates_unfinished
ON task_mode_candidates(target_id) WHERE state='pending';

CREATE INDEX idx_task_target_list_dismissals_owner
ON task_target_list_dismissals(owner_user_id, dismissed_at DESC);

CREATE INDEX idx_task_target_list_dismissals_recent ON task_target_list_dismissals(owner_user_id,task_id,dismissed_at DESC,target_id DESC);

CREATE INDEX idx_task_targets_username_task_status
ON task_targets(username_norm,task_id,status);

CREATE INDEX idx_task_windows_task_queue ON task_windows(task_id, queue_order);

CREATE INDEX idx_tasks_owner_snapshot_priority
ON tasks(owner_user_id,
    CASE WHEN status IN ('queued', 'running', 'waiting_network', 'paused', 'recoverable')
         THEN 0 ELSE 1 END,
    updated_at DESC, id DESC);

CREATE INDEX idx_tasks_owner_updated ON tasks(owner_user_id, updated_at DESC);

CREATE INDEX idx_workbench_actionable_page
        ON workbench_actionable_candidates(owner_user_id,visibility,reviewed_at DESC,candidate_id DESC);

CREATE INDEX idx_workbench_cache_rebuild
ON workbench_candidates(owner_user_id, status,
    (review_cache_json <> '{}'), length(CAST(review_cache_json AS BLOB)));

CREATE INDEX idx_workbench_candidates_legacy_progress
ON workbench_candidates(account_id, owner_user_id, source_target, source_mode)
WHERE source_target IS NULL OR source_mode IS NULL;

CREATE INDEX idx_workbench_candidates_owner_queue
ON workbench_candidates(owner_user_id, status, visibility, created_at, id);

CREATE INDEX idx_workbench_candidates_owner_reviewed
ON workbench_candidates(owner_user_id, status, visibility, reviewed_at DESC, id DESC);

CREATE INDEX idx_workbench_candidates_progress
ON workbench_candidates(source_target, source_mode, account_id, owner_user_id);

CREATE INDEX idx_workbench_candidates_source_mode_account
ON workbench_candidates(source_target, source_mode, account_id);

CREATE INDEX idx_workbench_claims_owner_time
ON workbench_identity_claims(claimed_by_user_id, claimed_at DESC);

CREATE INDEX idx_workbench_claims_progress
ON workbench_identity_claims(source_target, source, account_id, claimed_by_user_id);

CREATE INDEX idx_workbench_claims_source_mode_account
ON workbench_identity_claims(source_target, source, account_id);

CREATE INDEX idx_workbench_decisions_owner_decision_time
ON workbench_review_decisions(owner_user_id, decision, decided_at DESC, id DESC);

CREATE INDEX idx_workbench_decisions_owner_time
ON workbench_review_decisions(owner_user_id, decided_at DESC, id);

CREATE INDEX idx_workbench_dismissals_owner_time
ON workbench_candidate_dismissals(owner_user_id, dismissed_at DESC, id);

CREATE INDEX idx_workbench_exclusions_owner_page
ON workbench_collection_exclusions(owner_user_id, excluded_at DESC, id DESC);

CREATE INDEX idx_workbench_exclusions_owner_time
ON workbench_collection_exclusions(owner_user_id, excluded_at DESC, id);

CREATE INDEX idx_workbench_exclusions_progress
ON workbench_collection_exclusions(account_id, owner_user_id,
    CASE WHEN json_valid(profile_snapshot_json)
         THEN json_extract(profile_snapshot_json,'$.page_read_status')='hover_preview'
         ELSE 0 END);

CREATE INDEX idx_workbench_review_stage ON workbench_candidates(owner_user_id,status,visibility,review_stage,created_at,id);

CREATE UNIQUE INDEX posting_asset_hash ON posting_assets(sha256) WHERE sha256 IS NOT NULL;

CREATE UNIQUE INDEX posting_asset_render_hash ON posting_assets(render_sha256) WHERE render_sha256 IS NOT NULL;

CREATE INDEX posting_jobs_holds ON posting_jobs(profile_id,owner_user_id,id) WHERE lease_token<>'';

CREATE INDEX posting_jobs_owner ON posting_jobs(owner_user_id,hidden_at,created_at);

CREATE INDEX posting_jobs_page ON posting_jobs(owner_user_id,hidden_at,created_at,id);

CREATE INDEX posting_jobs_queue ON posting_jobs(status,created_at);

CREATE INDEX posting_jobs_window ON posting_jobs(profile_id,status);

CREATE INDEX posting_receipts_date ON posting_receipts(confirmed_at,owner_user_id,profile_id);

CREATE INDEX posting_receipts_owner_date ON posting_receipts(owner_user_id,confirmed_at);

CREATE INDEX posting_receipts_report_period ON posting_receipts(owner_user_id,julianday(confirmed_at));

CREATE INDEX studio_assets_owner ON studio_assets(owner_user_id,created_at);

CREATE INDEX studio_jobs_due ON studio_jobs(status,due_at);

CREATE INDEX studio_jobs_owner ON studio_jobs(owner_user_id,created_at);

CREATE UNIQUE INDEX studio_one_draft_assignment ON studio_jobs(owner_user_id,source_draft_id) WHERE source_draft_id<>'';

CREATE TRIGGER trg_action_success_no_delete
BEFORE DELETE ON action_success_ledger
BEGIN
    SELECT RAISE(ABORT, 'action success ledger is immutable');
END;

CREATE TRIGGER trg_action_success_no_update
BEFORE UPDATE ON action_success_ledger
BEGIN
    SELECT RAISE(ABORT, 'action success ledger is immutable');
END;

CREATE TRIGGER trg_candidate_closed_target_insert_fence
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

CREATE TRIGGER trg_candidate_closed_target_reopen_fence
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

CREATE TRIGGER trg_candidate_target_completion_fence
BEFORE UPDATE OF status ON task_targets
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_mode_candidates WHERE target_id=NEW.id AND state='pending'
)
BEGIN
    SELECT RAISE(ABORT, 'target has pending candidates');
END;

CREATE TRIGGER trg_candidate_task_completion_fence
BEFORE UPDATE OF status ON tasks
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_targets target JOIN task_mode_candidates candidate
      ON candidate.target_id=target.id
    WHERE target.task_id=NEW.id AND candidate.state='pending'
)
BEGIN
    SELECT RAISE(ABORT, 'task has pending candidates');
END;

CREATE TRIGGER trg_event_log_usage_delete
AFTER DELETE ON event_log
BEGIN
    UPDATE event_log_usage SET entries=entries-1,
        bytes=bytes-(length(CAST(OLD.payload_json AS BLOB)) + length(OLD.entity_type)
            + length(OLD.entity_id) + length(OLD.event_type) + length(OLD.created_at))
    WHERE owner_user_id=OLD.owner_user_id;
END;

CREATE TRIGGER trg_event_log_usage_insert
AFTER INSERT ON event_log
BEGIN
    INSERT INTO event_log_usage(owner_user_id, entries, bytes)
    VALUES(NEW.owner_user_id, 1,
        length(CAST(NEW.payload_json AS BLOB)) + length(NEW.entity_type)
        + length(NEW.entity_id) + length(NEW.event_type) + length(NEW.created_at))
    ON CONFLICT(owner_user_id) DO UPDATE SET
        entries=entries+1, bytes=bytes+excluded.bytes;
END;

CREATE TRIGGER trg_event_log_usage_update
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

CREATE TRIGGER trg_global_seen_platform_account_delete
BEFORE DELETE ON instagram_accounts
WHEN EXISTS(SELECT 1 FROM global_seen WHERE account_id=OLD.id)
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=CASE WHEN OLD.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
END;

CREATE TRIGGER trg_global_seen_platform_delete
BEFORE DELETE ON global_seen
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=(SELECT CASE WHEN current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END
                    FROM instagram_accounts WHERE id=OLD.account_id);
END;

CREATE TRIGGER trg_global_seen_platform_insert
AFTER INSERT ON global_seen
BEGIN
    UPDATE global_seen_platform_stats SET total_count=total_count+1
    WHERE platform=(SELECT CASE WHEN current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END
                    FROM instagram_accounts WHERE id=NEW.account_id);
END;

CREATE TRIGGER trg_global_seen_platform_rename
AFTER UPDATE OF current_username_norm ON instagram_accounts
WHEN (OLD.current_username_norm GLOB 'fb:*') != (NEW.current_username_norm GLOB 'fb:*')
 AND EXISTS(SELECT 1 FROM global_seen WHERE account_id=NEW.id)
BEGIN
    UPDATE global_seen_platform_stats SET total_count=MAX(0,total_count-1)
    WHERE platform=CASE WHEN OLD.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
    UPDATE global_seen_platform_stats SET total_count=total_count+1
    WHERE platform=CASE WHEN NEW.current_username_norm GLOB 'fb:*' THEN 'facebook' ELSE 'instagram' END;
END;

CREATE TRIGGER trg_global_seen_stats_delete
AFTER DELETE ON global_seen
BEGIN
    UPDATE global_seen_stats
    SET total_count=CASE WHEN total_count>0 THEN total_count-1 ELSE 0 END
    WHERE singleton_id=1;
END;

CREATE TRIGGER trg_global_seen_stats_insert
AFTER INSERT ON global_seen
BEGIN
    UPDATE global_seen_stats SET total_count=total_count+1 WHERE singleton_id=1;
END;

CREATE TRIGGER trg_pending_source_recheck_blocks_completion
BEFORE UPDATE OF status ON task_targets
WHEN NEW.status='completed' AND EXISTS(
    SELECT 1 FROM task_source_rechecks WHERE target_id=NEW.id AND state='prepared'
)
BEGIN
    SELECT RAISE(ABORT, 'pending source recheck must settle before completion');
END;

CREATE TRIGGER trg_private_follow_completion_no_delete
BEFORE DELETE ON private_follow_completions
WHEN EXISTS(SELECT 1 FROM app_users WHERE id=OLD.owner_user_id)
BEGIN
    SELECT RAISE(ABORT, 'private follow completion is immutable');
END;

CREATE TRIGGER trg_private_follow_completion_no_update
BEFORE UPDATE ON private_follow_completions
BEGIN
    SELECT RAISE(ABORT, 'private follow completion is immutable');
END;

CREATE TRIGGER trg_progress_contribution_delete
        AFTER DELETE ON workbench_progress_contributions BEGIN
          UPDATE workbench_progress_totals SET
            qualified=qualified-OLD.qualified, recorded_total=recorded_total-OLD.recorded_total, excluded_total=excluded_total-OLD.excluded_total, discarded=discarded-OLD.discarded, hover_discarded=hover_discarded-OLD.hover_discarded
          WHERE target_id=OLD.target_id AND mode=OLD.mode;
          DELETE FROM workbench_progress_totals WHERE target_id=OLD.target_id
            AND mode=OLD.mode AND qualified=0 AND recorded_total=0
            AND excluded_total=0 AND discarded=0 AND hover_discarded=0;
        END;

CREATE TRIGGER trg_progress_contribution_insert
        AFTER INSERT ON workbench_progress_contributions BEGIN
          INSERT INTO workbench_progress_totals(target_id, mode, qualified, recorded_total, excluded_total, discarded, hover_discarded)
          VALUES(NEW.target_id, NEW.mode, NEW.qualified, NEW.recorded_total, NEW.excluded_total, NEW.discarded, NEW.hover_discarded)
          ON CONFLICT(target_id, mode) DO UPDATE SET
            qualified=qualified+excluded.qualified, recorded_total=recorded_total+excluded.recorded_total, excluded_total=excluded_total+excluded.excluded_total, discarded=discarded+excluded.discarded, hover_discarded=hover_discarded+excluded.hover_discarded;
        END;

CREATE TRIGGER trg_progress_refresh_identity
        INSTEAD OF INSERT ON workbench_progress_refresh BEGIN
          
        DELETE FROM workbench_progress_contributions WHERE account_id IN (SELECT NEW.account_id);
        INSERT INTO workbench_progress_contributions
            (account_id, kind, target_id, mode, qualified, recorded_total, excluded_total, discarded, hover_discarded)
        
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
        WHERE review.source_target IS NOT NULL AND review.source_mode IN ('followers', 'following', 'post_likers')
          AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(recorded.sources_json) THEN CASE WHEN json_type(recorded.sources_json)='array' THEN recorded.sources_json ELSE '[]' END ELSE '[]' END) source
                     WHERE source.value=review.source_mode)  AND review.account_id IN (SELECT NEW.account_id)
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
        WHERE claim.source_target IS NOT NULL AND claim.source IN ('followers', 'following', 'post_likers')
          AND (review.source_target IS NULL OR review.source_mode IS NULL)
          AND (review.source_target IS NULL OR review.source_target=claim.source_target)
          AND (review.source_mode IS NULL OR review.source_mode=claim.source)
          AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(recorded.sources_json) THEN CASE WHEN json_type(recorded.sources_json)='array' THEN recorded.sources_json ELSE '[]' END ELSE '[]' END) source
                     WHERE source.value=claim.source)  AND review.account_id IN (SELECT NEW.account_id)
        UNION ALL
        SELECT recorded.account_id, 'recorded', recorded.target_id, source.value,
               0, COUNT(*), COUNT(excluded.id), 0, 0
        FROM task_results recorded
        JOIN tasks task ON task.id=recorded.task_id
        JOIN json_each(CASE WHEN json_valid(recorded.sources_json) THEN CASE WHEN json_type(recorded.sources_json)='array' THEN recorded.sources_json ELSE '[]' END ELSE '[]' END) source
        LEFT JOIN workbench_collection_exclusions excluded
          ON excluded.account_id=recorded.account_id
          AND excluded.owner_user_id=task.owner_user_id
        WHERE source.type IN ('text', 'array', 'object')  AND recorded.account_id IN (SELECT NEW.account_id)
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
        WHERE claim.source_target IS NOT NULL  AND claim.account_id IN (SELECT NEW.account_id)
    ;
    
        END;

CREATE TRIGGER trg_progress_task_owner_update
        AFTER UPDATE OF owner_user_id ON tasks
        WHEN OLD.owner_user_id IS NOT NEW.owner_user_id BEGIN
          INSERT INTO workbench_progress_refresh(account_id)
          SELECT account_id FROM task_results WHERE task_id=NEW.id;
        END;

CREATE TRIGGER trg_progress_task_results_delete
                AFTER DELETE ON task_results  BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id;
                END;

CREATE TRIGGER trg_progress_task_results_insert
                AFTER INSERT ON task_results  BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_task_results_update
                AFTER UPDATE OF account_id,task_id,target_id,sources_json ON task_results WHEN (OLD.account_id IS NOT NEW.account_id OR OLD.task_id IS NOT NEW.task_id OR OLD.target_id IS NOT NEW.target_id OR OLD.sources_json IS NOT NEW.sources_json) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id UNION SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_candidates_delete
                AFTER DELETE ON workbench_candidates WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT OLD.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT OLD.account_id)
                              AND qualified<>0) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_candidates_insert
                AFTER INSERT ON workbench_candidates WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT NEW.account_id)
                              AND qualified<>0) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_candidates_update
                AFTER UPDATE OF account_id,owner_user_id,source_target,source_mode ON workbench_candidates WHEN (OLD.account_id IS NOT NEW.account_id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.source_target IS NOT NEW.source_target OR OLD.source_mode IS NOT NEW.source_mode) AND (EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT OLD.account_id UNION SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT OLD.account_id UNION SELECT NEW.account_id)
                              AND qualified<>0)) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id UNION SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_collection_exclusions_delete
                AFTER DELETE ON workbench_collection_exclusions  BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_collection_exclusions_insert
                AFTER INSERT ON workbench_collection_exclusions  BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_collection_exclusions_update
                AFTER UPDATE OF account_id,owner_user_id,profile_snapshot_json ON workbench_collection_exclusions WHEN (OLD.account_id IS NOT NEW.account_id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.profile_snapshot_json IS NOT NEW.profile_snapshot_json) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id UNION SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_identity_claims_delete
                AFTER DELETE ON workbench_identity_claims WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT OLD.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_collection_exclusions WHERE account_id IN (SELECT OLD.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT OLD.account_id)
                              AND (qualified<>0 OR discarded<>0)) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_identity_claims_insert
                AFTER INSERT ON workbench_identity_claims WHEN EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_collection_exclusions WHERE account_id IN (SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT NEW.account_id)
                              AND (qualified<>0 OR discarded<>0)) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_progress_workbench_identity_claims_update
                AFTER UPDATE OF account_id,claimed_by_user_id,source_target,source ON workbench_identity_claims WHEN (OLD.account_id IS NOT NEW.account_id OR OLD.claimed_by_user_id IS NOT NEW.claimed_by_user_id OR OLD.source_target IS NOT NEW.source_target OR OLD.source IS NOT NEW.source) AND (EXISTS(SELECT 1 FROM task_results WHERE account_id IN (SELECT OLD.account_id UNION SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_collection_exclusions WHERE account_id IN (SELECT OLD.account_id UNION SELECT NEW.account_id))
                    OR EXISTS(SELECT 1 FROM workbench_progress_contributions WHERE account_id IN (SELECT OLD.account_id UNION SELECT NEW.account_id)
                              AND (qualified<>0 OR discarded<>0))) BEGIN
                  INSERT INTO workbench_progress_refresh(account_id) SELECT OLD.account_id UNION SELECT NEW.account_id;
                END;

CREATE TRIGGER trg_relation_obligation_target_completion_fence
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

CREATE TRIGGER trg_relation_obligation_task_completion_fence
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

CREATE TRIGGER trg_split_admission_executed_insert
AFTER INSERT ON task_targets WHEN NEW.status!='pending'
BEGIN
    INSERT INTO split_admission_totals(owner_user_id,username_norm,has_executed,updated_at)
    SELECT owner_user_id,NEW.username_norm,1,NEW.updated_at FROM tasks WHERE id=NEW.task_id
    ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET has_executed=1,updated_at=excluded.updated_at;
END;

CREATE TRIGGER trg_split_admission_executed_update
AFTER UPDATE OF status ON task_targets WHEN NEW.status!='pending'
BEGIN
    INSERT INTO split_admission_totals(owner_user_id,username_norm,has_executed,updated_at)
    SELECT owner_user_id,NEW.username_norm,1,NEW.updated_at FROM tasks WHERE id=NEW.task_id
    ON CONFLICT(owner_user_id,username_norm) DO UPDATE SET has_executed=1,updated_at=excluded.updated_at;
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

CREATE TRIGGER trg_split_completed_target_insert
AFTER INSERT ON task_targets WHEN NEW.status='completed'
BEGIN
    INSERT OR IGNORE INTO split_completed_targets
        (target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at)
    SELECT NEW.id,owner_user_id,NEW.username_norm,NEW.username_display,NEW.task_id,
        COALESCE(NEW.current_window_id,NEW.preferred_window_id),NEW.updated_at
    FROM tasks WHERE id=NEW.task_id;
END;

CREATE TRIGGER trg_split_completed_target_update
AFTER UPDATE OF status ON task_targets WHEN NEW.status='completed'
BEGIN
    INSERT OR IGNORE INTO split_completed_targets
        (target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at)
    SELECT NEW.id,owner_user_id,NEW.username_norm,NEW.username_display,NEW.task_id,
        COALESCE(NEW.current_window_id,NEW.preferred_window_id,OLD.current_window_id,OLD.preferred_window_id),NEW.updated_at
    FROM tasks WHERE id=NEW.task_id;
END;

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

CREATE TRIGGER trg_task_mode_candidate_counter_delete
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

CREATE TRIGGER trg_task_mode_candidate_counter_insert
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

CREATE TRIGGER trg_task_mode_candidate_counter_state
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

CREATE TRIGGER trg_workbench_aggregate_account_delete BEFORE DELETE ON instagram_accounts  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='claim' AND record_id IN (SELECT r.account_id FROM workbench_identity_claims r WHERE r.account_id IN (OLD.id));
DELETE FROM workbench_aggregate_members WHERE kind='result' AND record_id IN (SELECT r.id FROM task_results r WHERE r.account_id IN (OLD.id));
DELETE FROM workbench_aggregate_members WHERE kind='candidate' AND record_id IN (SELECT r.id FROM workbench_candidates r WHERE r.account_id IN (OLD.id));
DELETE FROM workbench_aggregate_members WHERE kind='dismissal' AND record_id IN (SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (SELECT id FROM workbench_candidates WHERE account_id IN (OLD.id)));
DELETE FROM workbench_actionable_candidates WHERE candidate_id IN (SELECT id FROM workbench_candidates WHERE account_id IN (OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_account_insert AFTER INSERT ON instagram_accounts WHEN EXISTS(SELECT 1 FROM workbench_identity_claims WHERE account_id=NEW.id)
                OR EXISTS(SELECT 1 FROM task_results WHERE account_id=NEW.id)
                OR EXISTS(SELECT 1 FROM workbench_candidates WHERE account_id=NEW.id) BEGIN
INSERT INTO workbench_aggregate_refresh_claim(record_id) SELECT r.account_id FROM workbench_identity_claims r WHERE r.account_id IN (NEW.id);
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT r.id FROM task_results r WHERE r.account_id IN (NEW.id);
INSERT INTO workbench_aggregate_refresh_candidate(record_id) SELECT r.id FROM workbench_candidates r WHERE r.account_id IN (NEW.id);
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (SELECT id FROM workbench_candidates WHERE account_id IN (NEW.id));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT id FROM workbench_candidates WHERE account_id IN (NEW.id);

END;

CREATE TRIGGER trg_workbench_aggregate_account_update AFTER UPDATE OF id,current_username_norm ON instagram_accounts WHEN OLD.id IS NOT NEW.id OR (OLD.current_username_norm GLOB 'fb:*')<>(NEW.current_username_norm GLOB 'fb:*') BEGIN
INSERT INTO workbench_aggregate_refresh_claim(record_id) SELECT r.account_id FROM workbench_identity_claims r WHERE r.account_id IN (OLD.id,NEW.id);
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT r.id FROM task_results r WHERE r.account_id IN (OLD.id,NEW.id);
INSERT INTO workbench_aggregate_refresh_candidate(record_id) SELECT r.id FROM workbench_candidates r WHERE r.account_id IN (OLD.id,NEW.id);
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (SELECT id FROM workbench_candidates WHERE account_id IN (OLD.id,NEW.id));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT id FROM workbench_candidates WHERE account_id IN (OLD.id,NEW.id);

END;

CREATE TRIGGER trg_workbench_aggregate_actionable_delete AFTER DELETE ON workbench_actionable_candidates WHEN (SELECT rebuilding FROM workbench_aggregate_state WHERE singleton_id=1)=0 BEGIN

        UPDATE workbench_snapshot_counts SET total=total-1
        WHERE owner_user_id=OLD.owner_user_id AND metric='approved:'||OLD.visibility;
END;

CREATE TRIGGER trg_workbench_aggregate_actionable_insert AFTER INSERT ON workbench_actionable_candidates WHEN (SELECT rebuilding FROM workbench_aggregate_state WHERE singleton_id=1)=0 BEGIN

        INSERT INTO workbench_snapshot_counts(owner_user_id,metric,total)
        VALUES(NEW.owner_user_id,'approved:'||NEW.visibility,1)
        ON CONFLICT(owner_user_id,metric) DO UPDATE SET total=total+1;
END;

CREATE TRIGGER trg_workbench_aggregate_alias_delete AFTER DELETE ON instagram_username_aliases  BEGIN
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT id FROM workbench_candidates WHERE account_id IN (OLD.account_id);

END;

CREATE TRIGGER trg_workbench_aggregate_alias_insert AFTER INSERT ON instagram_username_aliases  BEGIN
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT id FROM workbench_candidates WHERE account_id IN (NEW.account_id);

END;

CREATE TRIGGER trg_workbench_aggregate_alias_update AFTER UPDATE OF account_id,username_norm ON instagram_username_aliases  BEGIN
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT id FROM workbench_candidates WHERE account_id IN (OLD.account_id,NEW.account_id);

END;

CREATE TRIGGER trg_workbench_aggregate_candidate_delete AFTER DELETE ON workbench_candidates  BEGIN
INSERT INTO workbench_aggregate_refresh_candidate(record_id) SELECT value FROM json_each(json_array(OLD.id));
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (OLD.id);
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_candidate_insert AFTER INSERT ON workbench_candidates  BEGIN
INSERT INTO workbench_aggregate_refresh_candidate(record_id) SELECT value FROM json_each(json_array(NEW.id));
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (NEW.id);
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_candidate_order AFTER UPDATE OF reviewed_at ON workbench_candidates WHEN OLD.reviewed_at IS NOT NEW.reviewed_at BEGIN
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_candidate_update AFTER UPDATE OF id,owner_user_id,account_id,status,visibility ON workbench_candidates WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.account_id IS NOT NEW.account_id OR OLD.status IS NOT NEW.status OR OLD.visibility IS NOT NEW.visibility BEGIN
INSERT INTO workbench_aggregate_refresh_candidate(record_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT r.id FROM workbench_candidate_dismissals r WHERE r.candidate_id IN (OLD.id,NEW.id);
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_claim_delete AFTER DELETE ON workbench_identity_claims  BEGIN
INSERT INTO workbench_aggregate_refresh_claim(record_id) SELECT value FROM json_each(json_array(OLD.account_id));

END;

CREATE TRIGGER trg_workbench_aggregate_claim_insert AFTER INSERT ON workbench_identity_claims  BEGIN
INSERT INTO workbench_aggregate_refresh_claim(record_id) SELECT value FROM json_each(json_array(NEW.account_id));

END;

CREATE TRIGGER trg_workbench_aggregate_claim_update AFTER UPDATE OF account_id ON workbench_identity_claims WHEN OLD.account_id IS NOT NEW.account_id BEGIN
INSERT INTO workbench_aggregate_refresh_claim(record_id) SELECT value FROM json_each(json_array(OLD.account_id,NEW.account_id));

END;

CREATE TRIGGER trg_workbench_aggregate_dismissal_delete AFTER DELETE ON workbench_candidate_dismissals  BEGIN
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT value FROM json_each(json_array(OLD.id));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(OLD.candidate_id));

END;

CREATE TRIGGER trg_workbench_aggregate_dismissal_insert AFTER INSERT ON workbench_candidate_dismissals  BEGIN
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT value FROM json_each(json_array(NEW.id));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(NEW.candidate_id));

END;

CREATE TRIGGER trg_workbench_aggregate_dismissal_update AFTER UPDATE OF id,owner_user_id,candidate_id ON workbench_candidate_dismissals WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.candidate_id IS NOT NEW.candidate_id BEGIN
INSERT INTO workbench_aggregate_refresh_dismissal(record_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT value FROM json_each(json_array(OLD.candidate_id,NEW.candidate_id));

END;

CREATE TRIGGER trg_workbench_aggregate_exclusion_delete AFTER DELETE ON workbench_collection_exclusions  BEGIN
INSERT INTO workbench_aggregate_refresh_exclusion(record_id) SELECT value FROM json_each(json_array(OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_exclusion_insert AFTER INSERT ON workbench_collection_exclusions  BEGIN
INSERT INTO workbench_aggregate_refresh_exclusion(record_id) SELECT value FROM json_each(json_array(NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_exclusion_update AFTER UPDATE OF id,owner_user_id,username_display ON workbench_collection_exclusions WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.username_display IS NOT NEW.username_display BEGIN
INSERT INTO workbench_aggregate_refresh_exclusion(record_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_member_delete AFTER DELETE ON workbench_aggregate_members WHEN (SELECT rebuilding FROM workbench_aggregate_state WHERE singleton_id=1)=0 BEGIN

        UPDATE workbench_snapshot_counts SET total=total-1
        WHERE owner_user_id=OLD.owner_user_id AND metric IN (SELECT 'claimed' AS metric WHERE OLD.kind='claim' AND 1 UNION ALL SELECT 'total_collected' AS metric WHERE OLD.kind='result' AND 1 UNION ALL SELECT 'total_public' AS metric WHERE OLD.kind='result' AND OLD.bucket='public' UNION ALL SELECT 'total_private' AS metric WHERE OLD.kind='result' AND OLD.bucket='private' UNION ALL SELECT 'candidate:'||OLD.bucket AS metric WHERE OLD.kind='candidate' AND 1 UNION ALL SELECT 'total_split' AS metric WHERE OLD.kind='split' AND 1 UNION ALL SELECT 'collection_excluded' AS metric WHERE OLD.kind='exclusion' AND 1 UNION ALL SELECT 'approved_dismissed' AS metric WHERE OLD.kind='dismissal' AND 1 UNION ALL SELECT 'success:'||OLD.bucket AS metric WHERE OLD.kind='success' AND 1);
END;

CREATE TRIGGER trg_workbench_aggregate_member_insert AFTER INSERT ON workbench_aggregate_members WHEN (SELECT rebuilding FROM workbench_aggregate_state WHERE singleton_id=1)=0 BEGIN

        INSERT INTO workbench_snapshot_counts(owner_user_id,metric,total)
        SELECT NEW.owner_user_id,metric,1 FROM (SELECT 'claimed' AS metric WHERE NEW.kind='claim' AND 1 UNION ALL SELECT 'total_collected' AS metric WHERE NEW.kind='result' AND 1 UNION ALL SELECT 'total_public' AS metric WHERE NEW.kind='result' AND NEW.bucket='public' UNION ALL SELECT 'total_private' AS metric WHERE NEW.kind='result' AND NEW.bucket='private' UNION ALL SELECT 'candidate:'||NEW.bucket AS metric WHERE NEW.kind='candidate' AND 1 UNION ALL SELECT 'total_split' AS metric WHERE NEW.kind='split' AND 1 UNION ALL SELECT 'collection_excluded' AS metric WHERE NEW.kind='exclusion' AND 1 UNION ALL SELECT 'approved_dismissed' AS metric WHERE NEW.kind='dismissal' AND 1 UNION ALL SELECT 'success:'||NEW.bucket AS metric WHERE NEW.kind='success' AND 1) WHERE 1
        ON CONFLICT(owner_user_id,metric) DO UPDATE SET total=total+1;
END;

CREATE TRIGGER trg_workbench_aggregate_refresh_actionable INSTEAD OF INSERT ON workbench_actionable_refresh  BEGIN
DELETE FROM workbench_actionable_candidates WHERE candidate_id IN (NEW.candidate_id);
INSERT INTO workbench_actionable_candidates(candidate_id,owner_user_id,account_id,visibility,reviewed_at) SELECT candidate.id,candidate.owner_user_id,candidate.account_id,
        candidate.visibility,candidate.reviewed_at
        FROM workbench_candidates candidate
        JOIN instagram_accounts account ON account.id=candidate.account_id
        WHERE candidate.status='approved' AND account.current_username_norm NOT GLOB 'fb:*' AND (candidate.id IN (NEW.candidate_id))
          AND NOT EXISTS(SELECT 1 FROM workbench_candidate_dismissals dismissal
                         WHERE dismissal.candidate_id=candidate.id)
          AND NOT EXISTS(SELECT 1 FROM instagram_username_aliases alias
              WHERE alias.account_id=candidate.account_id AND EXISTS(
                  SELECT 1 FROM action_success_ledger success
                  WHERE success.owner_user_id=candidate.owner_user_id
                    AND success.operation=CASE candidate.visibility WHEN 'public' THEN 'greet' ELSE 'follow' END
                    AND success.username_norm=alias.username_norm
                    AND success.username_norm NOT GLOB 'fb:*'));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_candidate INSTEAD OF INSERT ON workbench_aggregate_refresh_candidate  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='candidate' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'candidate',r.id,r.owner_user_id,r.status||':'||r.visibility FROM workbench_candidates r JOIN instagram_accounts account ON account.id=r.account_id WHERE (account.current_username_norm NOT GLOB 'fb:*') AND (r.id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_claim INSTEAD OF INSERT ON workbench_aggregate_refresh_claim  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='claim' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'claim',r.account_id,'','' FROM workbench_identity_claims r JOIN instagram_accounts account ON account.id=r.account_id WHERE (account.current_username_norm NOT GLOB 'fb:*') AND (r.account_id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_dismissal INSTEAD OF INSERT ON workbench_aggregate_refresh_dismissal  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='dismissal' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'dismissal',r.id,r.owner_user_id,'' FROM workbench_candidate_dismissals r JOIN workbench_candidates candidate ON candidate.id=r.candidate_id JOIN instagram_accounts account ON account.id=candidate.account_id WHERE (account.current_username_norm NOT GLOB 'fb:*') AND (r.id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_exclusion INSTEAD OF INSERT ON workbench_aggregate_refresh_exclusion  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='exclusion' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'exclusion',r.id,r.owner_user_id,'' FROM workbench_collection_exclusions r  WHERE (r.username_display NOT GLOB 'fb:*') AND (r.id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_result INSTEAD OF INSERT ON workbench_aggregate_refresh_result  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='result' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'result',r.id,task.owner_user_id,r.visibility FROM task_results r JOIN tasks task ON task.id=r.task_id JOIN instagram_accounts account ON account.id=r.account_id WHERE (account.current_username_norm NOT GLOB 'fb:*' AND (CASE WHEN json_valid(task.settings_json) THEN json_type(task.settings_json)='object'
    AND (json_type(task.settings_json,'$.platform') IS NULL OR
         (json_type(task.settings_json,'$.platform')='text' AND json_extract(task.settings_json,'$.platform')='instagram'))
    ELSE 0 END)) AND (r.id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_split INSTEAD OF INSERT ON workbench_aggregate_refresh_split  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='split' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'split',r.id,r.owner_user_id,'' FROM split_candidate_history r  WHERE (r.username_norm NOT GLOB 'fb:*') AND (r.id IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_refresh_success INSTEAD OF INSERT ON workbench_aggregate_refresh_success  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='success' AND record_id IN (NEW.record_id);
INSERT INTO workbench_aggregate_members(kind,record_id,owner_user_id,bucket) SELECT 'success',json_array(r.owner_user_id,r.operation,r.username_norm),r.owner_user_id,r.operation FROM action_success_ledger r  WHERE (r.username_norm NOT GLOB 'fb:*') AND (json_array(r.owner_user_id,r.operation,r.username_norm) IN (NEW.record_id));

END;

CREATE TRIGGER trg_workbench_aggregate_result_delete AFTER DELETE ON task_results  BEGIN
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT value FROM json_each(json_array(OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_result_insert AFTER INSERT ON task_results  BEGIN
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT value FROM json_each(json_array(NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_result_update AFTER UPDATE OF id,task_id,account_id,visibility ON task_results WHEN OLD.id IS NOT NEW.id OR OLD.task_id IS NOT NEW.task_id OR OLD.account_id IS NOT NEW.account_id OR OLD.visibility IS NOT NEW.visibility BEGIN
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_split_delete AFTER DELETE ON split_candidate_history  BEGIN
INSERT INTO workbench_aggregate_refresh_split(record_id) SELECT value FROM json_each(json_array(OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_split_insert AFTER INSERT ON split_candidate_history  BEGIN
INSERT INTO workbench_aggregate_refresh_split(record_id) SELECT value FROM json_each(json_array(NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_split_update AFTER UPDATE OF id,owner_user_id,username_norm ON split_candidate_history WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.username_norm IS NOT NEW.username_norm BEGIN
INSERT INTO workbench_aggregate_refresh_split(record_id) SELECT value FROM json_each(json_array(OLD.id,NEW.id));

END;

CREATE TRIGGER trg_workbench_aggregate_success_delete AFTER DELETE ON action_success_ledger  BEGIN
INSERT INTO workbench_aggregate_refresh_success(record_id) SELECT value FROM json_each(json_array(json_array(OLD.owner_user_id,OLD.operation,OLD.username_norm)));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT candidate.id FROM instagram_username_aliases alias JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id WHERE alias.username_norm=OLD.username_norm AND candidate.owner_user_id=OLD.owner_user_id;

END;

CREATE TRIGGER trg_workbench_aggregate_success_insert AFTER INSERT ON action_success_ledger  BEGIN
INSERT INTO workbench_aggregate_refresh_success(record_id) SELECT value FROM json_each(json_array(json_array(NEW.owner_user_id,NEW.operation,NEW.username_norm)));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT candidate.id FROM instagram_username_aliases alias JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id WHERE alias.username_norm=NEW.username_norm AND candidate.owner_user_id=NEW.owner_user_id;

END;

CREATE TRIGGER trg_workbench_aggregate_success_update AFTER UPDATE OF owner_user_id,operation,username_norm ON action_success_ledger WHEN OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.operation IS NOT NEW.operation OR OLD.username_norm IS NOT NEW.username_norm BEGIN
INSERT INTO workbench_aggregate_refresh_success(record_id) SELECT value FROM json_each(json_array(json_array(OLD.owner_user_id,OLD.operation,OLD.username_norm),json_array(NEW.owner_user_id,NEW.operation,NEW.username_norm)));
INSERT INTO workbench_actionable_refresh(candidate_id) SELECT candidate.id FROM instagram_username_aliases alias JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id WHERE alias.username_norm=OLD.username_norm AND candidate.owner_user_id=OLD.owner_user_id UNION SELECT candidate.id FROM instagram_username_aliases alias JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id WHERE alias.username_norm=NEW.username_norm AND candidate.owner_user_id=NEW.owner_user_id;

END;

CREATE TRIGGER trg_workbench_aggregate_task_delete BEFORE DELETE ON tasks  BEGIN
DELETE FROM workbench_aggregate_members WHERE kind='result' AND record_id IN (SELECT r.id FROM task_results r WHERE r.task_id IN (OLD.id));

END;

CREATE TRIGGER trg_workbench_aggregate_task_insert AFTER INSERT ON tasks WHEN EXISTS(SELECT 1 FROM task_results WHERE task_id=NEW.id) BEGIN
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT r.id FROM task_results r WHERE r.task_id IN (NEW.id);

END;

CREATE TRIGGER trg_workbench_aggregate_task_update AFTER UPDATE OF id,owner_user_id,settings_json ON tasks WHEN OLD.id IS NOT NEW.id OR OLD.owner_user_id IS NOT NEW.owner_user_id OR (CASE WHEN json_valid(OLD.settings_json) THEN json_type(OLD.settings_json)='object'
    AND (json_type(OLD.settings_json,'$.platform') IS NULL OR
         (json_type(OLD.settings_json,'$.platform')='text' AND json_extract(OLD.settings_json,'$.platform')='instagram'))
    ELSE 0 END) IS NOT (CASE WHEN json_valid(NEW.settings_json) THEN json_type(NEW.settings_json)='object'
    AND (json_type(NEW.settings_json,'$.platform') IS NULL OR
         (json_type(NEW.settings_json,'$.platform')='text' AND json_extract(NEW.settings_json,'$.platform')='instagram'))
    ELSE 0 END) BEGIN
INSERT INTO workbench_aggregate_refresh_result(record_id) SELECT r.id FROM task_results r WHERE r.task_id IN (OLD.id,NEW.id);

END;

CREATE TRIGGER trg_workbench_cache_usage_delete
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

CREATE TRIGGER trg_workbench_cache_usage_insert
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

CREATE TRIGGER trg_workbench_cache_usage_update
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

CREATE TRIGGER trg_workbench_decision_no_delete
BEFORE DELETE ON workbench_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'workbench review decisions are immutable');
END;

CREATE TRIGGER trg_workbench_decision_no_update
BEFORE UPDATE ON workbench_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'workbench review decisions are immutable');
END;

CREATE TRIGGER trg_workbench_dismissal_no_delete
BEFORE DELETE ON workbench_candidate_dismissals
BEGIN
    SELECT RAISE(ABORT, 'workbench candidate dismissals are immutable');
END;

CREATE TRIGGER trg_workbench_dismissal_no_update
BEFORE UPDATE ON workbench_candidate_dismissals
BEGIN
    SELECT RAISE(ABORT, 'workbench candidate dismissals are immutable');
END;

CREATE TRIGGER trg_workbench_exclusion_no_delete
                            BEFORE DELETE ON workbench_collection_exclusions
                            BEGIN SELECT RAISE(ABORT, 'workbench collection exclusions are immutable'); END;

CREATE TRIGGER trg_workbench_exclusion_no_update
BEFORE UPDATE ON workbench_collection_exclusions
BEGIN
    SELECT RAISE(ABORT, 'workbench collection exclusions are immutable');
END;
INSERT INTO schema_migrations VALUES(1,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(2,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(3,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(4,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(5,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(6,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(7,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(8,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(9,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(10,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(11,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(12,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(13,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(14,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(15,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(16,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(17,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(18,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(19,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(20,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(21,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(22,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(23,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(24,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(25,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(26,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(27,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(28,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(29,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(30,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(31,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(32,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(33,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(34,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(35,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(36,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(37,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(38,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(39,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(40,'2026-10-03T00:00:00+00:00');
INSERT INTO schema_migrations VALUES(41,'2026-10-03T00:00:00+00:00');
