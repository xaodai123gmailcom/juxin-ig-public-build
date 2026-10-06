import {readFileSync, realpathSync, mkdirSync, writeFileSync, existsSync, lstatSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve, relative, isAbsolute, dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
const requireLocal=createRequire(import.meta.url);

export const sourceRevision = 'stability-r94';
export const requiredFiles = [
  'scripts/source_binding_io.py', 'scripts/tests/test_source_binding_io.py', 'scripts/local_source_binding.cjs', 'scripts/ci_source_binding.py', 'backend/app/studio.py', 'backend/tests/test_standalone_nurture_checkpoint_r64.py',
  'README.md', 'README_CN.md', 'docs/PUBLIC_BUILD.md', 'docs/CI_SOURCE_IDENTITY.md', 'RELEASE_VERIFICATION.md', 'docs/ACCEPTANCE_CHECKLIST.md',
  'scripts/recovery_upgrade_fixture_r64.py', 'scripts/verify_installed_recovery_r64.py', 'backend/tests/test_installed_recovery_r64.py', 'desktop/tests/recovery-ui-native-r64.cjs', 'desktop/tests/recovery-ui-native-r64.test.cjs', 'renderer/tests/fixtures/recovery-ui-r64.tsx',
  'backend/app/browser_admission.py', 'backend/app/execution_manager.py', 'backend/app/instagram_crop.py', 'backend/app/main.py', 'backend/app/nurture_collection_blocker.py', 'backend/app/posting_schema.py', 'backend/app/posting_workflow.py', 'backend/app/posting_workflow_selftest.py', 'backend/tests/test_combined_recovery_r64.py', 'backend/tests/test_hidden_collection_blocker_r63.py', 'backend/tests/test_posting_durable_preflight_r63.py', 'backend/tests/test_posting_withdraw_api_r63.py', 'backend/tests/test_posting_withdraw_r63.py', 'backend/tests/test_posting_workflow_v2.py', 'renderer/src/nurture-collection-blocker.tsx', 'renderer/src/posting-workspace.tsx', 'renderer/src/standalone-nurture-workspace.tsx', 'renderer/tests/fixtures/hidden-collection-blocker.tsx', 'renderer/tests/fixtures/posting-r6.tsx', 'renderer/tests/hidden-collection-blocker-r63.test.mjs', 'renderer/tests/posting-r6.test.mjs', 'renderer/tests/posting-withdraw-r63.test.mjs', 'scripts/verify_hidden_collection_blocker_ui.py',
  'backend/app/instagram_crop_dom.py', 'backend/tests/test_crop_icon_r64.py', 'backend/tests/test_crop_guard_r64.py', 'scripts/crop_icon_fixture.py', 'desktop/tests/crop-icon-r64.cjs',
  'backend/app/nurture_cleanup_recovery.py', 'backend/tests/test_nurture_cleanup_recovery_r63.py',
  'backend/tests/test_nurture_closed_profile_guard.py', 'backend/tests/test_nurture_cleanup_occupancy.py',
  'backend/tests/test_nurture_missing_lease_surface.py', 'backend/tests/test_nurture_cleanup_manager_registry.py',
  'backend/app/nurture_cleanup_upgrade_selftest.py', 'backend/tests/test_installed_nurture_cleanup_upgrade_r63.py', 'scripts/nurture_cleanup_upgrade_fixture.py', 'scripts/fixtures/nurture_cleanup_upgrade_r62.sql', 'desktop/tests/nurture-cleanup-native-r63.cjs', 'desktop/tests/nurture-cleanup-native-r63.test.cjs', 'desktop/tests/nurture-closed-proof.test.mjs', 'renderer/tests/fixtures/standalone-nurture-recovery-r62.tsx',
  'backend/app/final_seed_completion_selftest.py', 'backend/tests/test_final_seed_completion_r62.py', 'backend/tests/test_screening_factory_recovery_r62.py', 'backend/tests/test_final_seed_browser_r62.py',
  'backend/tests/test_standalone_reels_routes_r62.py', 'backend/tests/test_standalone_watch_advance_r62.py', 'backend/tests/test_nurture_completion_cleanup_r62.py', 'backend/tests/fixtures/standalone_reels_routes_r62.cjs',
  'desktop/tests/posting-viewport-native-r62.cjs', 'desktop/tests/posting-viewport-native-r62.test.cjs',
  'desktop/tests/posting-viewport-r62.test.mjs', 'backend/tests/test_posting_crop_transition_r62.py', 'backend/tests/test_posting_editing_state_r62.py', 'backend/tests/test_original_crop_readiness_r62.py', 'backend/tests/test_posting_viewport_labels.py', 'backend/tests/test_posting_viewport_lifecycle_r62.py',
  'backend/app/instagram_identity.py', 'backend/tests/embedded_task_startup_probe_r62.py',
  'desktop/tests/task-cold-start-r62.cjs', 'desktop/tests/task-cold-start-r62.test.cjs',
  'backend/tests/test_instagram_identity_browser_r62.py', 'backend/tests/test_instagram_identity_r62.py',
  'backend/tests/test_posting_retry_r61.py', 'backend/tests/test_posting_startup_r62.py',
  'renderer/tests/posting-retry-r61.test.mjs',
  'scripts/report_index_upgrade_fixture.py', 'backend/tests/test_report_index_upgrade_r61.py',
  'IG_RELEASE.json', 'backend/app/discovery_write_session.py', 'backend/app/workbench_aggregates.py',
  'backend/app/workbench_progress_aggregates.py', 'scripts/pure_ig_upgrade_fixture.py',
  'backend/tests/test_saturation_acceptance_r99.py', 'backend/tests/test_pure_ig_data_migration_r5.py',
  'backend/tests/test_completed_card_dismissal_r96.py',
  'backend/tests/test_installed_completed_card_fixture_r96.py',
  'scripts/completed_card_smoke_fixture.py',
  'renderer/tests/completed-card-delete-r96.test.mjs',
  'renderer/tests/fixtures/completed-card-delete.tsx',
  'desktop/tests/completed-card-delete.integration.cjs',

  'scripts/simulate_instagram_500.py',
  'scripts/ig500_geometry.cjs',
  'scripts/verify_instagram_500_report.py',
  'backend/tests/test_instagram_500_report_r95.py',
  'backend/tests/test_relation_recommendation_tail_r95.py',

  'backend/tests/test_instagram_thousand_browser_r95.py',

  'backend/app/platform_scope.py',
  'backend/tests/test_platform_scope.py',
  'backend/tests/test_platform_review_reports_r95.py',

  'renderer/src/workbench-platform.tsx',
  'renderer/tests/workbench-platform-r95.test.mjs',
  'renderer/tests/fixtures/workbench-platform.tsx',
  'desktop/tests/workbench-platform.integration.cjs', 'backend/tests/test_collector_missing_r94.py', 'backend/tests/test_explicit_source_recheck.py',
  'backend/tests/test_snapshot_scale_r95.py', 'scripts/snapshot_scale_fixture.py', 'scripts/benchmark_snapshot_scale.py',
  'renderer/tests/source-recheck-r95.test.mjs', 'renderer/tests/fixtures/source-recheck.tsx', 'desktop/tests/source-recheck.integration.cjs',
  "renderer/tests/core-route-contract-r94.test.mjs", "desktop/tests/core-route-stability.integration.cjs", "renderer/tests/fixtures/core-route-stability.tsx",
  "BUILD_WITH_INSTALLED_CHROME.bat", "backend/app/browser_requirement.py", "backend/tests/test_native_requirement.py", "scripts/browser_build_policy.py", "scripts/verify_packaged_browser_policy.cjs", "scripts/tests/browser-policy-r94.test.cjs",'scripts/diagnose_native_startup.py', 'scripts/verify_native_browser.py', 'backend/tests/test_native_launch.py', 'backend/tests/test_native_diagnostic.py', 'scripts/browser_download.py', 'scripts/tests/test_browser_download_r94.py', 'backend/app/browser_bundle.py', 'backend/tests/test_recovery_responsiveness_r94.py', 'backend/tests/test_single_handoff_r94.py', 'backend/tests/test_hover_performance_r94.py', 'backend/tests/support/hover_performance.cjs', 'backend/tests/test_shared_retirement_r94.py', 'backend/tests/test_retirement_concurrency_r94.py', 'backend/tests/support/concurrency_probe.py', 'backend/tests/support/relation_scroll_selection.cjs', 'scripts/windows-packaging-requirements.txt', 'scripts/tests/test_build_entry_r94.py', '.gitattributes', 'scripts/package_portable_archive.py', 'scripts/verify_frozen_core_service.py', 'scripts/tests/test_release_packaging_r94.py', 'backend/tests/test_frozen_service_r94.py', 'renderer/src/snapshot-sharing.ts', 'renderer/tests/snapshot-sharing-r94.test.mjs', 'backend/tests/test_collection_faults_r94.py', 'renderer/tests/approved-delete-r94.test.mjs', 'backend/app/collection_coverage.py', 'backend/tests/test_collection_coverage_r94.py', 'renderer/src/workbench-r93.css', 'backend/tests/test_window_reuse_r93.py', 'scripts/backend_test_process.py', 'backend/tests/test_recovery_wait_r25.py', 'backend/tests/test_returned_close_r90.py', 'backend/tests/test_returned_queue_r90.py', 'desktop/tests/visible-fixture.cjs', 'desktop/tests/visible-fixture-r84.test.cjs', 'backend/tests/test_zero_switch_r84.py', 'scripts/install_python_dependencies.py', 'scripts/tests/test_python_dependencies_r83.py', 'backend/tests/studio_wait.py', 'backend/tests/test_nurture_wait_r82.py', 'backend/tests/test_pipeline_retained_r81.py', 'backend/tests/test_finite_close_r81.py', 'backend/tests/test_posting_transition_r80.py', 'backend/app/service.py', 'renderer/src/collection-task-rows.ts', 'renderer/tests/collection-task-rows.test.mjs', 'backend/tests/test_returned_window_release_r73.py', 'DIAGNOSE_UNREAD_PROFILES.bat', 'scripts/diagnose_unread_profiles.py', 'scripts/tests/test_diagnose_unread_profiles.py', 'backend/tests/test_unread_profile_queue_r72.py', 'backend/tests/test_split_recovery_fence_r72.py', 'desktop/assets/war-wolf.ico', 'desktop/assets/war-wolf.png', 'desktop/assets/war-wolf.svg', 'scripts/generate_app_icon.py', 'backend/tests/test_hover_card_r64.py', 'backend/app/profile_hover_preview.py', 'backend/tests/test_hover_precheck_r61.py', 'renderer/src/report-review-decision.tsx', 'backend/tests/test_report_review_decisions_r59.py', 'docs/UI_REQUIREMENTS.md', 'renderer/src/workbench-density.css', 'scripts/verify_timezone_data.py', 'scripts/tests/test_timezone_data_r57.py', 'backend/tests/test_private_follow_review_r56.py', 'backend/app/private_follow_reviews.py', 'renderer/src/private-follow-review-report.ts', 'renderer/src/private-follow-review-workspace.tsx', 'renderer/tests/private-follow-review-r56.test.mjs', 'backend/app/split_completion_details.py', 'backend/tests/test_collection_drain_r56.py', 'backend/tests/test_unfinished_window_targets_r56.py', 'backend/tests/test_split_completion_details_r56.py', 'backend/tests/test_source_profile_snapshot_r56.py', 'renderer/tests/activity-reports-r56.test.mjs', 'backend/tests/test_work_report_split_r56.py', 'backend/app/split_admissions.py', 'backend/tests/support/legacy_split_fixture.py', 'backend/app/account_exports.py', 'backend/tests/test_split_count_r55.py', 'backend/tests/test_account_exports_r55.py', 'renderer/src/account-export.ts', 'renderer/tests/account-export-r55.test.mjs', 'renderer/src/workbench-polish-r55.css', 'backend/tests/test_split_review_report_r54.py', 'renderer/src/review-queue-state.ts', 'renderer/src/review-stages-r54.css', 'renderer/src/split-review-report.ts', 'renderer/src/split-review-workspace.tsx', 'renderer/src/collection-fixed-footer.tsx', 'renderer/src/collection-fixed-footer-layout.ts', 'renderer/src/collection-fixed-footer.css', 'renderer/tests/review-stages-r54.test.mjs', 'renderer/tests/split-review-r54.test.mjs', 'renderer/tests/collection-fixed-footer-r54.test.mjs', 'backend/tests/test_review_layers_r54.py', 'backend/app/review_layers.py', 'renderer/tests/review-report-client-r54.test.mjs', 'backend/tests/completion_wait.py', 'backend/tests/test_completion_wait_r53.py', 'desktop/tests/review-reset-r52.test.cjs', 'renderer/tests/review-account-reset-r52.test.mjs', 'desktop/src/account-task-control.ts', 'desktop/tests/account-task-control-r51.test.mjs', 'backend/tests/test_relation_dom_r51.py', 'backend/tests/test_relation_surface_r51.py', 'backend/tests/test_relation_diagnostics_r51.py', 'backend/tests/test_manual_surface_r51.py', 'backend/tests/test_manual_collection_r51.py', 'backend/tests/test_manual_api_r51.py', 'desktop/tests/navigation-fixture.cjs', 'desktop/tests/navigation-fixture-r49.test.cjs', 'desktop/tests/task-watch-close-r48.test.cjs', 'desktop/tests/view-cleanup-r48.test.cjs', 'desktop/tests/native-destroy-r48.test.cjs', 'desktop/tests/task-watch-r47.test.cjs', 'desktop/tests/task-watch-fixture.cjs', 'desktop/tests/integration-failure.cjs', 'desktop/tests/integration-failure-r47.test.cjs', 'backend/tests/test_progress_dedupe_r46.py', 'desktop/tests/task-display-r46.test.mjs', 'backend/tests/test_zero_profile_reader_r45.py', 'backend/tests/test_zero_pipeline_r45.py', 'backend/tests/test_private_indicator_r45.py', 'backend/tests/test_location_readiness_r45.py', 'backend/tests/test_location_pipeline_r45.py', 'backend/tests/test_completion_persistence_r44.py', 'backend/tests/test_pipeline_completion_r44.py', 'backend/tests/test_source_completion_r44.py', 'backend/tests/test_split_lock_runtime_r43.py', 'renderer/tests/collection-claim-lock-r43.test.mjs', 'backend/tests/test_split_lock_api_r43.py', 'backend/tests/test_split_lock_r43.py', 'renderer/tests/collection-removal-r43.test.mjs', 'backend/tests/test_relation_completion_r43.py', 'backend/tests/test_completion_runtime_r43.py', 'backend/tests/test_collection_remove_r43.py', 'desktop/tests/renderer-fixture.cjs', 'desktop/tests/renderer-fixture-r42.test.cjs', 'desktop/tests/floating-cleanup-r42.test.cjs', 'renderer/src/workbench-polish-r41.css', 'renderer/tests/collection-settings-r41.test.mjs', 'backend/tests/test_nurture_delete_r41.py', 'backend/tests/test_nurture_delete_ui_r41.py', 'renderer/tests/build-nurture-r41-fixture.mjs', 'renderer/tests/fixtures/studio-nurture-r41.tsx', 'scripts/renderer_release_contract.mjs', 'scripts/tests/renderer_release_contract_r40.test.mjs', 'scripts/tests/test_backend_smoke_schema_r40.py', 'backend/tests/test_snapshot_scale_r39.py', 'backend/tests/test_snapshot_campaign_scale_r39.py', 'desktop/tests/snapshot-load-r39.test.mjs', 'renderer/tests/snapshot-load-r39.test.mjs', 'backend/tests/test_zero_public_default_r39.py', 'backend/tests/test_preopen_dedupe_r39.py', 'backend/tests/test_discard_registry_r39.py', 'backend/tests/test_public_activity_discard_r38.py', 'backend/tests/test_private_review_retirement_r38.py', 'backend/tests/test_public_post_activity_reader_r38.py', 'renderer/tests/public-activity-discard-r38.test.mjs', 'backend/tests/test_count_ceiling_r37.py', 'renderer/src/collection-discard-limits.ts', 'renderer/tests/discard-limits-r37.test.mjs', 'backend/tests/test_zero_posts_source_r37.py', 'backend/tests/test_monitor_partial_r37.py', 'desktop/tests/relation-recommendations-r37.test.mjs', 'backend/tests/test_zero_posts_reads_r37.py', 'backend/tests/test_zero_posts_pipeline_r37.py', 'desktop/tests/profile-private-icons-r37.test.mjs', 'renderer/src/collection-window-assignment.ts', 'renderer/tests/window-assignment-r34.test.mjs', 'backend/tests/test_specified_windows_r34.py', 'backend/tests/test_chat_concurrency_r34.py', 'desktop/tests/chat-reminders-r34.test.mjs', 'desktop/tests/chat-translation-r34.test.mjs', 'desktop/tests/whatsapp-navigation-r34.test.mjs', 'desktop/tests/floating-chat-session-r34.test.mjs', 'backend/tests/test_screen_persistence_r33.py', 'backend/tests/test_window_performance_r33.py', 'desktop/tests/window-performance-r33.test.mjs', 'desktop/tests/surface-switch-r33.test.mjs', 'renderer/tests/window-performance-r33.test.mjs', 'renderer/tests/browser-surface-r33.test.mjs', 'desktop/tests/whatsapp-quote-r32.test.mjs', 'backend/tests/test_windows_r32.py', 'desktop/tests/whatsapp-runtime-r32.test.mjs', 'desktop/tests/whatsapp-messaging-r32.test.mjs', 'desktop/tests/window-host-r32.test.mjs', 'desktop/tests/cdp-lifecycle-r32.test.mjs', 'renderer/tests/window-workspace-r32.test.mjs', 'renderer/tests/chat-translation-r32.test.mjs', 'backend/tests/test_pipeline_r31.py', 'backend/tests/test_browser_r31.py', 'backend/tests/test_storage_r31.py', 'backend/tests/test_alias_storage_r31.py', 'backend/tests/test_command_io_r31.py', 'backend/tests/test_registry_r31.py', 'renderer/tests/workbench-polling-r31.test.mjs', 'backend/tests/test_pipeline_r30.py', 'backend/tests/test_browser_reads_r30.py', 'backend/tests/test_spool_performance_r30.py', 'backend/tests/test_live_order_r30.py', 'renderer/src/workbench-render-state.ts', 'renderer/tests/workbench-render-state.test.mjs', 'renderer/tests/workbench-live-order.test.mjs', 'backend/tests/test_pipeline_r29.py', 'backend/tests/test_live_snapshot_r29.py', 'backend/tests/test_browser_reads_r29.py', 'renderer/src/workbench-format.ts', 'renderer/tests/workbench-format.test.mjs', 'scripts/run_backend_tests.py', 'scripts/tests/test_backend_test_runner.py', 'renderer/src/workbench-command-state.ts', 'renderer/tests/workbench-command-state.test.mjs', 'backend/tests/test_task_control_r27.py', 'scripts/verify_desktop_build.mjs', 'scripts/tests/verify_desktop_build.test.mjs', 'backend/tests/test_recovery_runtime_r25.py', 'backend/tests/test_browser_recovery_r25.py', 'backend/tests/test_checkpoint_recovery_r25.py', 'backend/tests/test_cross_recovery_r25.py', 'backend/app/__init__.py', 'backend/tests/test_runtime_r24.py', 'backend/tests/test_browser_stability_r24.py', 'backend/tests/test_persistence_r24.py', 'backend/tests/test_action_monitor_r24.py', 'renderer/src/workbench-live-status.ts', 'renderer/tests/workbench-live-status.test.mjs', 'desktop/tests/embedded-shutdown.test.mjs', 'backend/app/identity_registry.py', 'backend/tests/test_dedup_r22.py', 'backend/tests/test_dedup_persistence_r22.py', 'scripts/diagnose_dedupe.py', 'DIAGNOSE_DEDUPE.bat', 'scripts/tests/test_diagnose_dedupe.py', 'backend/tests/test_greeting_queue_safety.py', 'backend/app/async_cleanup.py', 'desktop/src/core-log.ts', 'desktop/tests/core-log.test.mjs', 'scripts/verify_r18_core.py', 'backend/tests/test_stability_r18.py', 'BUILD_REVISION.txt', 'package.json', 'scripts/build_windows.ps1',
  'scripts/install_windows.ps1', 'scripts/ensure_python_environment.py', 'scripts/tests/test_python_environment.py',
  'scripts/tests/test_build_recovery_r94.py', 'scripts/tests/test_native_repair_r94.py', 'scripts/repair_native_browser.py', 'backend/app/browser_permissions.py', 'scripts/browser_sandbox_permissions.py', 'backend/tests/test_browser_permissions.py', 'REPAIR_NATIVE_BROWSER.bat', 'scripts/prune_browser_runtime.py', 'scripts/install_native_browser.py',
  'scripts/verify_build_source.mjs', 'desktop/tests/floating-pages.integration.cjs',
  'desktop/tests/review-recovery-fixture.cjs', 'desktop/src/chat-translation.ts',
  'desktop/src/chat-translation-page.ts', 'desktop/src/message-activity-page.ts',
  'desktop/src/notification-registration.ts', 'desktop/src/whatsapp-unread-page.ts',
  'backend/app/work_reports.py', 'desktop/src/immersive-translator.ts',
  'desktop/src/immersive-bootstrap.ts', 'desktop/src/immersive-policy.ts',
  'desktop/src/immersive-domains.ts', 'desktop/src/immersive-preload.cts',
  'desktop/src/immersive-source.ts', 'desktop/tests/support/immersive-synthetic-source.cjs',
  'desktop/vendor/immersive-translate/host.html', 'desktop/vendor/immersive-translate/NOTICE.txt',
  'desktop/src/immersive-pool.ts', 'desktop/tests/immersive-host.test.mjs',
  'desktop/tests/immersive-runtime.integration.cjs', 'desktop/tests/immersive-policy.test.mjs',
  'desktop/src/account-viewport.ts', 'desktop/src/web-page-menu.ts',
  'desktop/tests/account-interaction.test.mjs', 'desktop/tests/instagram-layout.integration.cjs',
  'desktop/src/immersive-startup.ts', 'desktop/tests/immersive-controller.test.mjs',
  'desktop/tests/whatsapp-layout.test.mjs', 'desktop/src/whatsapp-boundary.ts',
  'desktop/tests/whatsapp-boundary.test.mjs', 'desktop/src/whatsapp-columns.ts',
  'desktop/tests/whatsapp-columns.test.mjs', 'backend/tests/test_new_page_first.py',
  'RECOVERY_POLICY.md', 'backend/app/collection_surface.py', 'backend/tests/test_collection_stability.py',
  'backend/tests/test_nurture_scheduler.py',
  'renderer/src/nurture-schedule.ts', 'renderer/tests/nurture-schedule.test.mjs'];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const revisionOf = root => readFileSync(join(root, 'BUILD_REVISION.txt'), 'utf8').match(/^Source revision:\s*(\S+)\s*$/m)?.[1];

/** Detect incomplete extraction and mixed source revisions before installing dependencies. */
export function verifyBuildSource(directory) {
  const root = realpathSync(directory), revision = revisionOf(root);
  try {
    lstatSync(join(root, 'desktop/vendor/immersive-translate/immersive-translate.user.js'));
    throw new Error('Optional proprietary translator payload must not ship in public source');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  for (const retired of ['backend/app/facebook_worker.py', 'backend/app/facebook_dom.py',
      'renderer/src/workbench-platform-state.ts', 'scripts/facebook_smoke_fixture.py',
      'scripts/repeat_facebook_browser_r95.py', 'FB_INTEGRATION_RELEASE.json']) {
    if (existsSync(join(root, retired))) throw new Error('Removed platform source must not ship: ' + retired);
  }
  if (revision !== sourceRevision) throw new Error('Source revision mismatch: expected ' + sourceRevision + ', found ' + (revision || 'missing'));
  const metadata = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  if (metadata.name !== 'juxin-ig-audience-collector-newgen' || metadata.version !== '3.0.4') throw new Error('Unexpected project or product version');
  const bytes = readFileSync(join(root, 'SOURCE_SHA256.json'));
  const manifest = JSON.parse(bytes.toString('utf8'));
  if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object') throw new Error('Invalid source manifest');
  for (const name of requiredFiles) if (!Object.hasOwn(manifest, name)) throw new Error('Required source file missing from manifest: ' + name);
  const failures = [];
  for (const [name, expected] of Object.entries(manifest)) {
    if (isAbsolute(name) || name.includes('\\') || name.split('/').some(part => !part || part === '..' || part === '.') || !/^[a-f0-9]{64}$/.test(expected)) throw new Error('Invalid source manifest entry');
    try {
      const file = realpathSync(join(root, name)), location = relative(root, file);
      if (isAbsolute(location) || location === '..' || location.startsWith('..' + (process.platform === 'win32' ? '\\' : '/'))) throw new Error('outside source directory');
      if (sha256(readFileSync(file)) !== expected) failures.push(name + ': checksum mismatch');
    } catch { failures.push(name + ': missing or inaccessible file'); }
  }
  if (failures.length) throw new Error('Source files are incomplete or mixed: ' + failures.slice(0, 12).join('; '));
  const markerPresent=['LOCAL_SOURCE_PROVENANCE.json','CI_SOURCE_PROVENANCE.json'].some(name=>{try{lstatSync(join(root,name));return true}catch(error){if(error.code==='ENOENT')return false;throw error}});
  const ciPresent=Boolean(process.env.GITHUB_SHA)||String(process.env.GITHUB_ACTIONS||'').trim().toLowerCase()==='true';
  const local=markerPresent||ciPresent?requireLocal('./local_source_binding.cjs').collectSourceIdentity(resolve(directory)):{};
  return {verified: true, revision, version: metadata.version, checkedFiles: Object.keys(manifest).length, manifestSha256: sha256(bytes),...local};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  let report;
  try {
    report = verifyBuildSource(root);
    console.log('BUILD_SOURCE_REVISION=' + report.revision);
    console.log('BUILD_SOURCE_CHECK=PASS (' + report.checkedFiles + ' files)');
  } catch (error) {
    report = {verified: false, expectedRevision: sourceRevision, error: error.message};
    console.error('BUILD_SOURCE_CHECK=FAILED: ' + error.message);
    console.error('Extract the complete 3.0 r94 ZIP into a NEW directory and run START_HERE_NEWGEN.bat.');
    process.exitCode = 1;
  }
  try {
    mkdirSync(join(root, 'installer-output'), {recursive: true});
    writeFileSync(join(root, 'installer-output', 'build-source.json'), JSON.stringify(report, null, 2) + '\n');
  } catch (error) {
    console.error('Could not save source verification report: ' + error.code);
    process.exitCode = 1;
  }
}
