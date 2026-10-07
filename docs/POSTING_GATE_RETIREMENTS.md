# Posting gate retirement map

## Scope and evidence boundary

This document maps the former checks to removed publishing behavior or retained collection/nurture/account safety. It records source wiring, not a successful Windows release. The source-only and mocked tests do not establish Windows native UI, frozen inference, NSIS installation, installed runtime acceptance or live-account behavior.

The full source/build/installed chain remains required. Source identity, isolated `-I` imports, Unicode source and copied-frozen checks, all four native models, collection scale and upgrades, lease ownership, hidden paused collection recovery, safe stop and separately confirmed cleanup are retained. The R10 bounded runner-local failure diagnostic is unchanged. Public artifacts remain exactly three size-bounded synthetic JSON summaries; EXE, logs, screenshots, accounts, raw archives and other runner-local evidence are not published. Binary publication stays on hold.

## Removed backend execution checks

Every removed original pattern is listed below. “Early” is `ci/public_ci_early.py`; full-build entries are `scripts/build_windows.ps1`. Replacements do not permit the removed publisher to execute.

| Original check | Former gate | Reason or replacement |
|---|---|---|
| `test_confirmed_posting_report_r6.py` | full build repair | Publishing-receipt count is no longer a report card; retained collection/follow/split/added totals are checked by work-report and conflicting-index upgrade gates |
| `test_crop_guard_r64.py` | early, full build late | Guard immediately before publishing crop input; no crop UI or publisher remains |
| `test_crop_icon_r64.py` | early, focused early precheck, full build late | Publisher crop-icon discovery only; the separate saturation browser precheck remains mandatory |
| `test_desktop_materials.py` | full build late | Posting material downloads, retry, isolation and paging only; account window/session isolation remains in account/native tests |
| `test_installed_posting_workflow_r6.py` | full build repair | Installed publishing queue self-test retired; replaced by exact frozen HTTP 404 probes and installed desktop route refusal plus legacy archive recovery |
| `test_internal_pexels_route_r6.py` | full build repair | Pexels configuration route removed; test_posting_removal_r65.py and frozen probes require its authenticated HTTP 404 response |
| `test_location_popup.py` | full build late | Publisher location-composer input only; collection location readers and readiness/pipeline r45 gates remain |
| `test_original_crop_readiness_r62.py` | early, full build late | Publisher original-ratio/crop readiness only |
| `test_pexels_configuration_r6.py` | full build repair | Pexels credentials/download configuration removed; negative API/config surface tests replace feature enablement |
| `test_posting_*_v2.py` | full build repair | Publishing manager/executor/schema behavior family retired; migration/no-replay/archive safety now covered by test_posting_retirement_r65.py and shared lease fences |
| `test_posting_account_stats.py` | full build late | Mixed account statistics retained as test_account_profile_stats.py; nurture/account profile counts remain required without published-receipt totals |
| `test_posting_api_integration_r6.py` | full build repair | Publishing API execution retired; test_posting_removal_r65.py requires removed routes, commands and cloud surfaces to remain unavailable |
| `test_posting_crop_transition_r62.py` | early, full build late | Publisher crop-to-edit transition only |
| `test_posting_dom.py` | early, full build late | Publishing composition/file chooser DOM fixtures retired; shared repeated manual-tab/cookie preservation, login/account-home behavior and browser fixtures are extracted into required account-home/lifecycle tests and support/account_browser_fixture.py; real-browser collection/nurture/identity gates remain |
| `test_posting_durable_preflight_r63.py` | early, full build repair | Reviewed publishing queue preflight retired; destructive-migration failure, archive integrity and active/unknown lease preservation now covered by retirement tests |
| `test_posting_editing_state_r62.py` | early, full build late | Publisher caption/editing state only |
| `test_posting_lease_integration_r6.py` | full build repair | Posting executor lease lifecycle retired; shared browser admission and nurture/collection lease checks retained, legacy unsafe rows covered by retired-window fences |
| `test_posting_missing_lease_surface_r6.py` | full build repair | Safety coverage retained under test_retired_window_fences_r65.py; legacy unknown/active publishing rows still block unsafe browser reuse |
| `test_posting_retry_r61.py` | early, full build repair | Publishing preparation/review retry only; no resume or re-send path remains; retirement tests verify no replay |
| `test_posting_startup_r62.py` | early, full build repair | Publisher startup only; shared native cold-start proof retained and narrowed to actual nurture startup |
| `test_posting_transition_r80.py` | early, full build transition | Publisher create/upload acknowledgement only; native collection and nurture browser transitions remain separately gated |
| `test_posting_viewport_labels.py` | early, full build late | Publisher viewport branch removed; exact candidate CDP target, current-target relabel and screening-slot/legacy-provider no-op guards are retained in required test_account_home_lifecycle_r65.py AccountTaskLabelTests, alongside desktop/native layout checks |
| `test_posting_viewport_lifecycle_r62.py` | early, full build late | Publisher viewport freeze/restore removed; label-before-navigation and absent candidate-session guards are retained in required test_account_home_lifecycle_r65.py AccountHomeLifecycleTests; collection/nurture ownership gates remain |
| `test_posting_withdraw*_r63.py` | early, full build repair | Queued publishing withdrawal API/state removed; verified recoverable retirement replaces the action, without releasing unknown/active leases |
| `test_posting_workflow.py` | full build late | Publishing selection/prepared-media/retry workflow only; test_studio.py remains required for nurture task controls and owner isolation |
| `test_publisher_entry.py` | full build late | Publisher create menu/file-upload entry only; shared own-account navigation remains tested by test_instagram_home.py and native cold startup; failed/cancelled navigation and delayed page creation cleanup are retained in test_account_home_lifecycle_r65.py |
| `test_publisher_submission.py` | full build late | Caption/publish fence/submission acknowledgement only; legacy uncertain submissions are quarantined rather than retried |
| `test_studio_auto_media.py` | full build late | Automatic posting media/AI preparation only |
| `test_studio_cleanup.py` | full build late | Published-material cleanup only; nurture cleanup/history/lease gates remain required |
| `test_studio_cleanup_ui.py` | full build late | React posting-material deletion/retry UI only; real React nurture cleanup is retained by test_nurture_delete_ui_r41.py and native recovery/cleanup fixtures |
| `test_studio_drafts.py` | full build late | Publishing draft preparation only; there is no remaining draft UI or publisher |
| `test_studio_history.py` | full build late | Publishing history/receipt deletion and uncertain publish-result protection only; recoverable archive/unknown-lease tests replace this, while nurture history deletion tests remain |

New mandatory early/full-build checks are `test_posting_retirement_r65.py`, `test_posting_removal_r65.py`, `test_retired_window_fences_r65.py`, and `test_account_home_lifecycle_r65.py`. The renamed `test_account_profile_stats.py` occupies the account-statistics late gate. Early coverage is 27 exact required groups, including these additions; the direct late sequence has 29 required entries. Existing failure aggregation, per-case deadlines and teardown watchdogs remain enforced.

## Mixed checks preserved

- `test_studio.py` remains in the full build. Its implementation is nurture-only; task controls, owner isolation, lease/window release and shared test fixtures remain. Removing the posting cases does not remove the Studio gate
- Explicit retained Studio safety methods include `test_control_owner_and_terminal_fence`, `test_other_module_lock_causes_wait_without_opening_or_closing`, `test_live_studio_lock_survives_occupancy_refresh_and_blocks_monitor`, `test_recovery_pauses_safe_progress_and_fences_inflight`, `test_pause_resume_keeps_cursor_and_does_not_replay_steps`, and `test_daily_counters_commit_atomically_and_ignore_replayed_step`
- Publishing-coupled effect tests are adapted to nurture as `test_successful_nurture_closes_and_releases_its_window`, `test_uncertain_nurture_never_restarts_automatically`, `test_cancel_during_nurture_preserves_lease_and_window_for_review`, and `test_shutdown_during_disconnect_still_closes_owned_window`. Owner-scoped templates/jobs/stats and `test_retired_queue_cannot_starve_nurture_scheduler_when_archive_is_pending` also remain required
- The exact `test_foreign_workflow_blocker_does_not_disclose_module_status_or_id` check remains in `test_nurture_cleanup_recovery_r63.py`, preserving cross-owner nondisclosure of workflow identity and state
- `test_combined_recovery_r64.py` and `test_installed_recovery_r64.py` remain early/full-build checks. Their current contracts cover feature removal, verified archive migration, unknown/active quarantine and collection/nurture recovery
- `test_nurture_delete_r41.py`, `test_nurture_delete_ui_r41.py`, nurture cleanup families, missing-lease/closed-profile checks, standalone checkpoint/runtime tests and installed nurture cleanup upgrade remain mandatory. The esbuild dependency contract now references `renderer/tests/build-nurture-r41-fixture.mjs`, rather than the deleted material-cleanup fixture
- `test_instagram_home.py`, `test_nurture_flow.py`, both Instagram identity r62 suites, and the thousand-person collection browser test remain required. Shared offline own-account browser fixtures are in `backend/tests/support/account_browser_fixture.py`. Browser requirements use `IGAC_REQUIRE_NURTURE_BROWSER` or `IGAC_REQUIRE_COLLECTION_BROWSER` and the selected `IGAC_TEST_CHROMIUM_EXECUTABLE`
- Shared label/lifecycle extractions are `AccountHomeLifecycleTests.test_label_new_target_before_navigation_then_activate`, `test_absent_new_session_never_labels_borrowed_source`, and `AccountTaskLabelTests.test_explicit_candidate_label_never_touches_source_session`, `test_normal_relabel_uses_current_exact_target`, `test_screening_slot_and_legacy_browser_behavior_are_unchanged` in `test_account_home_lifecycle_r65.py`
- Required real-browser `HomeTests` in `test_instagram_home.py` additionally cover `test_repeated_account_home_preserves_manual_tabs_cookies_and_draft`, `test_account_home_login_route_stops_before_nurture_effect`, and `test_feed_login_text_does_not_override_signed_in_sidebar_but_password_form_does`. Their Windows/browser execution is required; source registration or successful mock tests cannot stand in for it
- Collection post-activity reading and zero-post filters are retained. These inspect existing public profiles and are separate from creating posts. Their r37/r38/r39 checks are unchanged

## Native and renderer checks

| Original check/evidence | Current disposition |
|---|---|
| `desktop/tests/posting-viewport-native-r62.cjs`, its test and `posting-viewport-r62.test.mjs` | Removed publisher-only viewport proof; collection/nurture sizing and native surface checks retained |
| `desktop/tests/posting-r6.integration.cjs` | Removed posting workspace/material/publish UI integration |
| `desktop/tests/crop-icon-r64.cjs`, `scripts/crop_icon_fixture.py`, `ci/r64_crop_proof.py`, `ci/test-r64-crop-proof.py` | Retired crop feature and oracle; there is no replacement crop capability |
| `embedded_posting_probe.py` / `python-posting-adapter` stage | Retired actual publishing CDP adapter; the remaining embedded-browser integration still runs collection, nurture and task-owned startup |
| `desktop/tests/task-cold-start-r62.cjs`, `task-cold-start-r62.test.cjs`, `backend/tests/embedded_task_startup_probe_r62.py` | Kept. Posting startup branch removed; nurture is still the first opener, navigates an unopened owned profile, verifies zero-count own-account identity, runs through production Studio, then closes. `taskColdStart` remains mandatory in build and installed receipts |
| `desktop/tests/recovery-ui-native-r64.cjs`, its test and renderer fixture | Kept with schema 2, collection/nurture-only. Exact hidden task identity, repeated lookup/stop collapse, cancelled confirmation, live/stale refusal, stop preserving holds/history, separate cleanup and unlocked window availability remain. `start_intents` must be present and empty |
| Queued withdraw/review/fresh-reviewed-start recovery UI phases | Retired publishing UI only. Replaced by `nonpostingRoutesOnly`; installed/backend retirement checks independently cover archive and no-replay safety |
| `r62-posting-viewport-native.json/png`, `r64-crop-icon-native.json/png`, posting actual/stress/retry screenshots and stress geometry JSON | Removed from early retention, raw-proof preflight and installed validators. No nonposting screenshot/proof removed |
| Recovery `withdrawn-review` / `fresh-review` captures | Retired with their posting phases. Hidden-blocker and stopped-feedback captures retained; `cleaned-window` capture added |
| Posting renderer/retry/withdraw and desktop Pexels registrations | Removed by package/frontend integration; collection/nurture renderer/native registrations remain |

Native recovery still requires Windows, a visible 1440×1050 window, trusted input, source hashes including production App/workbench/Core client and fixture host, zero external activity, exact stop task/version, screenshot bytes/hashes, and the existing deadline. The three retained early native fixtures are nurture cleanup, standalone Reels nurture, and schema-2 collection recovery.

## Frozen and installed checks

- `--posting-workflow`, `probe_posting_workflow`, its installed self-test and `posting_workflow` receipt field are removed because they execute the retired publisher. The selected frozen Core instead must return authenticated HTTP 404 at exactly GET `/api/posting/snapshot`, POST `/api/posting/command`, and POST `/api/internal/integrations/pexels`. `posting_removed` is independently validated and must agree between installed reports; 401/403, surviving aliases and partial endpoint sets fail
- The actual installed desktop recovery probe remains mandatory, with its original 120-second inner and 180-second owned-process bounds. It independently verifies the guarded desktop IPC boundary, recovery and recoverable legacy migration. Its exact validator is invoked after upgrade/scale verification; this is not replaced by a source-only test
- `confirmed_posting` is removed from active reports; the four retained collection/follow/split/added totals remain checked by installed work-report, index-upgrade and UI summary proofs
- R63 old-database upgrade retains 18 historical cases, source/binary identity, separate frozen-process chronology, historical records, owner isolation and closed-profile admission. `prepared_posting` becomes a recovered idle archived case; its nurture hold can clear only through the existing closed-profile guard. Admissions/new jobs/recovered holds become 4, retained holds become 14, historical jobs remain 19, protected tables remain 15, login files remain 68 and untouched leases remain 3
- Upgrade startup replaces `posting.recover`/`posting.start_scheduler` with `retire_legacy_posting` immediately after database initialization (recorded as `posting.retire_legacy` in the ordered proof). Four queued nonposting blockers remain. Independent archive validation compares original rows and table/index/trigger topology captured in the prelaunch legacy-seed manifest, including coordinated-loss rejection; verified recoverable storage is required before idle data leaves active storage; uncertain/active rows and leases remain fenced byte-for-byte. Migration failures must not proceed to removal

## Source registration changes

`verify_build_source.mjs` no longer requires deleted publishing/crop sources, tests or fixtures. It now requires the retirement implementation, independent archive oracle, replacement tests, shared account browser fixture and this map. Legacy `posting_schema.py` remains required solely to seed/interpret the old schema for safe retirement; it does not register a publishing route or scheduler. The desktop source/bundle verifier separately rejects retired publishing implementations, renderer routes, Pexels integration and confirmed-posting output.

No source manifests, package registrations or workflow permissions were edited by the CI-gate worker. The integration owner must reseal the complete final source manifest after all removals/additions, then run the aggregate source/renderer/desktop/backend gates. A stale manifest or unmerged implementation must fail rather than be ignored.

## CI/build worker changed files

- `ci/README.md`
- `ci/public_build_contract.py`
- `ci/public_ci.py`
- `ci/public_ci_common.py`
- `ci/public_ci_early.py`
- `ci/public_ci_groups.py`
- `ci/public_ci_validate_installed.py`
- `ci/public_ci_verify_installed.ps1`
- `ci/r63-upgrade-contract.json`
- `ci/r63-upgrade-example.json`
- `ci/r63_upgrade_proof.py`
- `ci/r64_recovery_ui_proof.py`
- `ci/test-r63-upgrade-proof.py`
- `ci/test-r64-recovery-ui-proof.py`
- `ci/test_public_ci.py`
- `scripts/build_windows.ps1`
- `scripts/verify_build_source.mjs`
- `scripts/verify_desktop_build.mjs`
- `scripts/renderer_release_contract.mjs`
- `scripts/tests/browser-policy-r94.test.cjs`
- `scripts/tests/renderer_release_contract_r40.test.mjs`
- `scripts/tests/support/backend_gate_files_r94.json`
- `scripts/tests/test_release_packaging_r94.py`

Added: `docs/POSTING_GATE_RETIREMENTS.md`.

Deleted: `ci/r64_crop_proof.py`, `ci/test-r64-crop-proof.py`.

Other implementation, package, archive-fixture, runtime verifier and manifest changes are owned by their corresponding integration components and are not included in this file inventory.
