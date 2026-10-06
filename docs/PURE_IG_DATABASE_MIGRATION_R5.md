# Pure-IG R5 database retirement

## Scope and guarantees

Version 39 applies logical row deletion in the local SQLite database during safe startup, after schema setup and before worker recovery, dispatch and the earlier hover-repair backup step. There is no FB-retirement backup, export, shadow copy or content archive. Existing user backups and remote exports are not modified.

An OS instance lock prevents a second Core from migrating an active database. An in-process live-worker token blocks initialization. An unexpired or unreadable-expiry affected browser lease blocks retirement rather than being stolen. This code does not navigate, close, delete or rewrite a browser session directory.

Ownership comes from explicit platform settings, reserved `fb:` usernames and typed provenance. Historical FB stable IDs are `fbid:<digits>`; an equal IG numeric ID or equal display name never establishes FB ownership. Non-FB aliases, conflicting IG stable IDs, unlabelled/shared records, malformed JSON and uncertain provenance are preserved. Shared browser profiles and their on-disk sessions remain. Exclusively FB-labelled profile IDs receive an owner-scoped ID-only retirement fence so inventory cannot silently present their old sessions as newly configured IG accounts; explicit IG configuration may supersede that fence. No profile name, content, token or payload is copied into it.

All deletion, temporary guard suspension, exact counter repair and the migration marker share one transaction. Failure or process death rolls back the entire retirement, including immutable guard definitions. A restore invokes the same classifier in a savepoint inside its outer transaction, even when version 39 already exists; release of that savepoint does not commit the restore. Foreign keys remain enabled. A valid input remains valid; a pre-existing unrelated/ambiguous FK violation is preserved rather than being “fixed” through unauthorized deletion, and no new violation is accepted.

Unrelated existing IG false-hover repair keeps its original backup behavior, after positively classified FB rows are removed. Deliberately preserved ambiguous/shared records can remain in such a backup. The purge does not create a new backup of its own.

## What permanent deletion means

These rows disappear from the live application and cannot be undone by its normal UI. This is **not a forensic erasure guarantee**: SQLite free pages/WAL history, filesystem snapshots, shared browser sessions and previously created external backups can retain bytes or copies. The migration neither wipes disks nor deletes those external copies. Ambiguous/shared data is intentionally not destroyed.

## Complete table and relationship inventory

The following inventory was generated from an initialized synthetic database using `PRAGMA foreign_key_list`. The optional old hover-repair archive is included. No live user database was accessed. Foreign keys referencing application users do not imply that a user account is FB-owned.

| Table | Retirement treatment | Foreign keys (delete action) |
|---|---|---|
| account_creation_batches | Delete receipts only if every plan ID belongs to removed FB plans; preserve mixed/invalid receipts | owner_user_id → app_users.id (NO ACTION) |
| account_window_events | Delete only classified FB plan events | owner_user_id → app_users.id (NO ACTION) |
| account_window_open_state | Cascade only from removed FB plans | owner_user_id → app_users.id (NO ACTION); plan_id → account_window_plans.id (CASCADE) |
| account_window_order | Remove only deleted FB plan IDs from otherwise unchanged ordering | owner_user_id → app_users.id (NO ACTION) |
| account_window_plans | Delete only explicit environment_json.platform=facebook | owner_user_id → app_users.id (NO ACTION) |
| action_attempts | Cascade only from positively classified action targets | target_id → action_targets.id (CASCADE); campaign_id → action_campaigns.id (CASCADE) |
| action_campaigns | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (CASCADE) |
| action_counter_resets | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (CASCADE) |
| action_dispatch_claims | Delete only fb: recipients; restore immutable guards in same transaction | owner_user_id → app_users.id (CASCADE) |
| action_success_ledger | Delete only fb: recipients; restore immutable guards in same transaction | owner_user_id → app_users.id (CASCADE) |
| action_targets | Delete only fb: recipient rows; preserve shared campaign | campaign_id → action_campaigns.id (CASCADE) |
| app_users | Preserve: shared/IG-only state has no positive FB row ownership | None; inspect typed/detached provenance explicitly |
| auth_sessions | Preserve: shared/IG-only state has no positive FB row ownership | user_id → app_users.id (CASCADE) |
| browser_operation_leases | Block on live/invalid-expiry affected lease; delete only expired, positively FB-owned entity leases | owner_user_id → app_users.id (CASCADE) |
| cloud_workspace_links | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| collection_dispatch_locks | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (CASCADE) |
| event_log | Delete typed FB entity events and explicit FB platform/account/target payload provenance; never free-text match | owner_user_id → app_users.id (CASCADE) |
| event_log_usage | Rebuild exact derived count/byte projections after deletion; retain covering indexes | owner_user_id → app_users.id (CASCADE) |
| follow_monitor_accounts | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | owner_user_id → app_users.id (CASCADE) |
| follow_monitor_daily_counts | Preserve: shared/IG-only state has no positive FB row ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_dm_accounts | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | owner_user_id → app_users.id (CASCADE) |
| follow_monitor_latest_dm | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_latest_follow | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_latest_unfollow | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_members | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_rounds | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | owner_user_id → app_users.id (CASCADE) |
| follow_monitor_runs | Preserve: shared/IG-only state has no positive FB row ownership | None; inspect typed/detached provenance explicitly |
| follow_monitor_seen | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | owner_user_id → app_users.id (CASCADE) |
| global_identity_owners | Delete only positively classified FB account children | owner_user_id → app_users.id (RESTRICT); account_id → instagram_accounts.id (RESTRICT) |
| global_seen | Delete only positively classified FB account children | account_id → instagram_accounts.id (CASCADE) |
| global_seen_platform_stats | Rebuild exact derived count/byte projections after deletion; retain covering indexes | None; inspect typed/detached provenance explicitly |
| global_seen_stats | Rebuild exact derived count/byte projections after deletion; retain covering indexes | None; inspect typed/detached provenance explicitly |
| hover_r64_repair_archive | Delete only already-classified FB account repair records; create no purge archive | None; inspect typed/detached provenance explicitly |
| instagram_accounts | Delete fb: current identities only with no contradictory non-FB alias or IG stable id; fbid: stable IDs are FB | None; inspect typed/detached provenance explicitly |
| instagram_username_aliases | Delete FB account children and aliases in the reserved fb: namespace | account_id → instagram_accounts.id (CASCADE) |
| native_browser_profiles | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| posting_account_snapshots | Remove only explicit fb: username/identity rows; profile IDs and names never establish ownership | owner_user_id → app_users.id (NO ACTION) |
| private_follow_completions | Delete only fb: recipients; restore immutable guards in same transaction | owner_user_id → app_users.id (CASCADE) |
| report_review_decisions | Delete FB target/history-prefixed review keys or FB private-follow attempt keys | owner_user_id → app_users.id (CASCADE) |
| retired_account_profiles | Add only owner/profile IDs for exclusively FB-tagged profiles; no content, name, credential or session copy | owner_user_id → app_users.id (CASCADE) |
| schema_migrations | Commit version 39 only with successful transaction; ordinary restart skips full purge | None; inspect typed/detached provenance explicitly |
| split_admission_totals | Delete only reserved fb: identity totals; preserve permanent IG admissions | owner_user_id → app_users.id (CASCADE) |
| split_candidate_history | Delete fb: names or FB source provenance without contradictory queued IG/shared generation | owner_user_id → app_users.id (CASCADE) |
| split_candidate_window_affinity | Cascade only from removed FB split candidates | candidate_id → split_candidates.id (CASCADE) |
| split_candidates | Delete fb: names or FB source provenance without contradictory queued IG/shared generation | owner_user_id → app_users.id (CASCADE) |
| split_completed_targets | Delete explicit fb: names or classified FB task/target provenance, including detached rows | owner_user_id → app_users.id (CASCADE) |
| studio_assets | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| studio_daily_actions | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| studio_jobs | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| studio_templates | Preserve: shared/IG-only state has no positive FB row ownership | owner_user_id → app_users.id (NO ACTION) |
| task_automatic_completions | Cascade only from positively classified task/target parents | task_id → tasks.id (CASCADE); target_id → task_targets.id (CASCADE) |
| task_checkpoints | Cascade only from positively classified task/target parents | target_id → task_targets.id (CASCADE); task_id → tasks.id (CASCADE) |
| task_list_dismissals | Cascade only from positively classified task/target parents | owner_user_id → app_users.id (CASCADE); task_id → tasks.id (CASCADE) |
| task_mode_candidate_counters | Rebuild exact derived count/byte projections after deletion; retain covering indexes | target_id → task_targets.id (CASCADE) |
| task_mode_candidates | Cascade from FB targets; remove explicit fb: candidates even in a mixed spool | target_id → task_targets.id (CASCADE) |
| task_parent_reel_decisions | Cascade only from positively classified task/target parents | target_id → task_targets.id (CASCADE); task_id → tasks.id (CASCADE); owner_user_id → app_users.id (CASCADE) |
| task_result_duplicate_archive | Delete classified FB account/task/target rows including detached duplicate archives | account_id → instagram_accounts.id (RESTRICT); owner_user_id → app_users.id (RESTRICT) |
| task_results | Delete classified FB account/task/target rows including detached duplicate archives | account_id → instagram_accounts.id (RESTRICT); target_id → task_targets.id (CASCADE); task_id → tasks.id (CASCADE) |
| task_source_rechecks | Cascade only from positively classified task/target parents | target_id → task_targets.id (CASCADE) |
| task_target_list_dismissals | Cascade only from positively classified task/target parents | task_id → tasks.id (CASCADE); owner_user_id → app_users.id (CASCADE); target_id → task_targets.id (CASCADE) |
| task_target_recovery_controls | Delete explicit fb: names or classified FB task/target provenance, including detached rows | None; inspect typed/detached provenance explicitly |
| task_targets | Delete children of FB tasks or reserved fb: target identities | task_id → tasks.id (CASCADE) |
| task_windows | Cascade only from positively classified task/target parents | task_id → tasks.id (CASCADE) |
| tasks | Delete only explicit settings_json.platform=facebook | owner_user_id → app_users.id (CASCADE) |
| workbench_actionable_candidates | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_aggregate_members | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_aggregate_state | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_cache_usage | Rebuild exact derived count/byte projections after deletion; retain covering indexes | owner_user_id → app_users.id (CASCADE) |
| workbench_candidate_dismissals | Delete only FB-candidate children; restore immutable guards in same transaction | owner_user_id → app_users.id (RESTRICT); candidate_id → workbench_candidates.id (RESTRICT) |
| workbench_candidates | Delete only positively classified FB account children | account_id → instagram_accounts.id (RESTRICT); owner_user_id → app_users.id (RESTRICT) |
| workbench_collection_exclusions | Delete only positively classified FB account children | owner_user_id → app_users.id (RESTRICT); account_id → instagram_accounts.id (RESTRICT) |
| workbench_identity_claims | Delete only positively classified FB account children | claimed_by_user_id → app_users.id (RESTRICT); account_id → instagram_accounts.id (RESTRICT) |
| workbench_progress_contributions | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_progress_totals | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_review_decisions | Delete only FB-candidate children; restore immutable guards in same transaction | owner_user_id → app_users.id (RESTRICT); candidate_id → workbench_candidates.id (RESTRICT) |
| workbench_snapshot_counts | Version40/41 rebuildable ID/count-only projections; SQLite triggers maintain exact purge deltas; restore can rebuild atomically; no profile/history payload copied | None; inspect typed/detached provenance explicitly |
| workbench_state_revision | Bump projection revision after atomic retirement | None; inspect typed/detached provenance explicitly |

## Verification

Dedicated tests cover mixed IG/FB task, target, alias, identity, result, cache, exclusion, decision, admission, report, action and plan rows; equal display names; IG numeric-ID versus FB fbid collisions; ambiguous shared identity preservation; detached/orphan provenance; exact surviving IG-row hashes; cold and repeated startup; restore savepoint/rollback; partial process termination; injected late failure; restored immutability; foreign keys; accurate counters; live lease/process fencing; backup ordering and external-backup preservation.

Run: `PYTHONPATH=backend:backend/tests python -m unittest test_pure_ig_data_migration_r5 test_startup_scaling_r98`

The database tests use synthetic local fixtures. They do not operate on a user's live database, browser or external accounts. Windows runtime and installer validation remain separate release gates.
