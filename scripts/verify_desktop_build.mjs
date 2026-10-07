import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { verifyRendererReleaseCopy } from "./renderer_release_contract.mjs";
import { verifyRendererProductionRuntime } from "./renderer_release_contract.mjs";

/** Fail-closed release gate for the independent NewGen desktop product. */
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const at = (relativePath) => resolve(root, relativePath);
const args = process.argv.slice(2);
assert(args.length === 0 || (args.length === 1 && args[0] === "--source-only"),
  "usage: node scripts/verify_desktop_build.mjs [--source-only]");
const sourceOnly = args.length === 1;

function fail(message) {
  throw new Error(`NewGen desktop verification failed: ${message}`);
}

function assert(condition, message) {
  if (!condition) fail(message);
}

function requireFile(relativePath) {
  const absolutePath = at(relativePath);
  if (!existsSync(absolutePath) || !statSync(absolutePath).isFile()) {
    fail(`required file is missing: ${relativePath}`);
  }
  return absolutePath;
}

function read(relativePath) {
  return readFileSync(requireFile(relativePath), "utf8");
}

function requireAll(source, values, label) {
  for (const value of values) {
    assert(source.includes(value), `${label} is missing contract marker: ${value}`);
  }
}

function forbidAll(source, values, label) {
  for (const value of values) {
    assert(!source.includes(value), `${label} contains retired or unsafe marker: ${value}`);
  }
}

function requireInOrder(source, values, label) {
  let cursor = 0;
  for (const value of values) {
    const index = source.indexOf(value, cursor);
    assert(index !== -1, `${label} is missing or reorders contract marker: ${value}`);
    cursor = index + value.length;
  }
}

function filesBelow(relativeDirectory, predicate = () => true) {
  const directory = at(relativeDirectory);
  if (!existsSync(directory)) return [];
  const output = [];
  const visit = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile() && predicate(path)) output.push(path);
    }
  };
  visit(directory);
  return output;
}

requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/collection-removal-r43.test.mjs"], "separate collection delete and return UI gate");
requireFile("renderer/tests/collection-removal-r43.test.mjs");
requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/collection-claim-lock-r43.test.mjs"], "persistent collection claim lock UI gate");
for (const path of ["renderer/tests/collection-claim-lock-r43.test.mjs", "backend/tests/test_split_lock_r43.py", "backend/tests/test_split_lock_api_r43.py", "backend/tests/test_split_lock_runtime_r43.py"]) requireFile(path);
for (const path of ["desktop/tests/review-reset-r52.test.cjs", "renderer/tests/review-account-reset-r52.test.mjs"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/review-reset-r52.test.cjs"], "isolated review reset regression gate");
requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/review-account-reset-r52.test.mjs"], "review reset control regression gate");
for (const path of ["backend/tests/completion_wait.py", "backend/tests/test_completion_wait_r53.py"]) requireFile(path);
requireAll(read("scripts/build_windows.ps1"), ["test_*r53.py", "collection-wait-r53-full.log", "$CollectionWaitR53ExitCode -ne 0"], "durable progress watchdog regression gate");
for (const path of ["backend/app/review_layers.py", "renderer/src/review-queue-state.ts", "renderer/src/split-review-workspace.tsx", "renderer/src/collection-fixed-footer.tsx"]) requireFile(path);
requireAll(read("scripts/build_windows.ps1"), ["test_*r55.py", "review-workflow-r55-full.log", "$ReviewWorkflowR55ExitCode -ne 0"], "r74 retained r55 split admission and account export regression gate");
requireAll(read("backend/tests/test_split_count_r55.py"), ["test_old_cloud_completion_prefers_immutable_history_over_changed_updated_at", "self.live('cloud.history.date',window_id='original-window')", "'target_owned_by_other_window'", "self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed',window_id='original-window')"], "r74 old-cloud completion test keeps original window ownership");
requireFile("scripts/install_python_dependencies.py");
requireFile("scripts/tests/test_python_dependencies_r83.py");
requireFile("scripts/stage_portable_resources.mjs");
requireFile("scripts/package_portable_archive.py");
requireFile("scripts/verify_frozen_core_service.py");
requireFile("scripts/tests/test_release_packaging_r94.py");
requireFile("backend/tests/test_frozen_service_r94.py");
requireInOrder(read("scripts/build_windows.ps1"), [
  'test_release_packaging_r94.py', '$PyInstallerExitCode -ne 0',
  'verify_frozen_core_service.py', 'Assert-LastExitCode "Verify frozen Core', '$InstallerSucceeded = $false'
], "verified archive and real frozen Core release gates");

requireFile("scripts/tests/portable_resources_r94.test.mjs");
requireAll(JSON.parse(read("package.json")).scripts["test:source"], ["scripts/tests/portable_resources_r94.test.mjs"], "portable runtime resource regression gate");
requireInOrder(read("scripts/build_portable_windows.ps1"), [
  'stage_portable_resources.mjs', 'if ($LASTEXITCODE -ne 0)', 'verify_frozen_core_service.py', 'package_portable_archive.py', '$ArchiveExitCode -ne 0'
], "portable runtime dependencies and resources before archive creation");
requireAll(read("scripts/install_windows.ps1"), ['scripts\\install_python_dependencies.py', '$PythonDependenciesExitCode -ne 0'], "r83 dependency recovery install path");
requireAll(read("scripts/install_python_dependencies.py"), ["https://pypi.org/simple", "PIP_CONFIG_FILE", "os.devnull", "--no-cache-dir", "--only-binary=Pillow", "('configured', 'official')", "--verify-only", "'-pip-check'", "PILLOW_CODEC_CHECK=PASS"], "r83 bounded official source fallback and actual dependency verification");
requireAll(read("backend/requirements.txt"), ["Pillow==12.3.0"], "r83 retained Pillow release pin");
requireAll(read("backend/pyproject.toml"), ["Pillow==12.3.0"], "r83 matching development Pillow release pin");
requireAll(read("scripts/build_windows.ps1"), ["test_python_dependencies_r83.py", "$DependencyRecoveryExitCode -ne 0"], "r83 dependency recovery regression gate");
requireAll(read("scripts/verify_runtime_ready.py"), ['"scripts/install_python_dependencies.py"'], "r83 dependency installer readiness fingerprint");
requireAll(read("backend/tests/studio_wait.py"), ["class _StudioProgressBudget", "idle_timeout=120.0", "overall_timeout=900.0", "not snapshot['active_ids'] and not snapshot['leases']", "no durable progress"], "r82 bounded Studio completion and ownership observation");
requireAll(read("backend/tests/test_nurture_scheduler.py"), ["await wait_for_studio_completion(self.m,self.owner,ids)", "self.assertEqual(4,maximum)", "self.assertEqual(sum(r['total_steps'] for r in rows),len(calls))", "self.assertEqual(8,len(self.browser.closed))"], "r82 nurture scheduler keeps real overlap and close assertions");
requireAll(read("backend/tests/test_nurture_wait_r82.py"), ["test_two_rounds_complete_with_close_slower_than_old_test_budget", "test_completed_status_cannot_pass_while_close_and_lease_are_owned", "test_stuck_job_still_fails_without_progress", "test_continuous_progress_cannot_extend_absolute_deadline"], "r82 nurture completion watchdog regression");
requireAll(read("scripts/verify_r18_core.py"), ["StudioWaitBudgetR82Tests,StudioWaitRuntimeR82Tests", "i.RelationDOMR20Tests"], "r82 mandatory watchdog and retained real Chromium regression gate");
requireAll(read("scripts/build_windows.ps1"), ["$CorePreflightExitCode = Invoke-IgacNativeCommandWithLog", "core-preflight-full.log", "$CorePreflightExitCode -ne 0"], "r82 complete preflight output and nonzero exit failure gate");
requireAll(read("backend/tests/test_pipeline_retained_r81.py"), ["test_one_slot_retries_its_retained_page_without_opening_another", "test_retained_page_counts_toward_two_slot_limit", "test_retained_pages_count_toward_three_slot_limit", "test_repeated_source_and_child_failures_keep_the_original_page_and_pending_row", "test_all_slots_retained_and_unread_still_process_healthy_new_rows"], "r81 retained screening-page queue regression");
requireAll(read("backend/tests/test_finite_close_r81.py"), ["test_finite_completion_closes_after_disconnect_and_durable_completion", "test_finite_negative_close_ack_retains_lease_until_close_only_retry"], "r81 finite collection close ownership regression");
requireAll(read("scripts/build_windows.ps1"), ["test_*r81.py", "pipeline-retained-r81-full.log", "$PipelineRetainedExitCode -ne 0"], "r81 retained screening-page and finite close recovery release gate");
requireAll(read("desktop/tests/embedded-browser.integration.cjs"), ["if(code!==0)throw pythonProbeFailure(script,code,output)"], "r80 Python probe retains its exception and fails closed");
requireAll(read("backend/tests/test_completion_persistence_r44.py"), ["test_stale_numeric_checkpoint_cannot_inflate_live_progress", "'skipped_global_duplicates': 0, 'qualified_for_review': 0"], "r79 old checkpoint cannot inflate the new review admission count");
requireAll(read("desktop/tests/chat-translation.integration.cjs"), ["await read();host.window().focus();wc.focus();", "native Enter must reach the focused chat input", "attempt<20"], "r77 native chat focus and bounded event acknowledgement");
requireAll(read("backend/app/execution_manager.py"), ["instagram_hover_card_unavailable", "_is_relationship_list_incomplete_error", "request_page_replacement"], "r76 automatic same-window hover recovery");
requireAll(read("backend/app/playwright_worker.py"), ["hover_frame_repaints", "latest_hrefs = await read_frame()", "pending_end_frame = latest_hrefs"], "r76 recheck virtual list after hover before scrolling");
requireAll(read("backend/tests/test_recovery_runtime_r25.py"), ["test_transient_hover_failure_reopens_same_list_without_manual_retry"], "r76 source hover recovery regression");
requireAll(read("backend/tests/test_hover_card_r64.py"), ["test_hover_repaint_is_read_before_next_scroll"], "r76 virtual row repaint regression");
requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/collection-task-rows.test.mjs"], "r75 qualified collection progress regression gate");
requireAll(read("scripts/build_windows.ps1"), ["test_standalone_nurture*.py", "IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER"], "R6 standalone nurture mandatory runtime and browser gates");
requireAll(read("desktop/tests/embedded-browser.integration.cjs"), ["nurture-reels-r6.integration.cjs"], "R6 standalone nurture native UI gate");
requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/standalone-nurture-r6.test.mjs"], "R6 standalone nurture renderer checks");
requireAll(read("backend/app/execution_manager.py"), ["automatic_gap_recheck_started", "persist_source_return", "pass_candidate_count"], "R6 persistent single gap pass");
requireAll(read("scripts/build_windows.ps1"), ["test_single_gap_recheck_r6.py", "test_installed_work_report_summary_r6.py", "test_work_report_performance_r6.py"], "R6 mandatory backend verification");
requireAll(read("scripts/verify_frozen_core_service.py"), ["validate_collection_completion_proof", "single_gap_recheck", "work_report_summary"], "R6 exact installed runtime proof");
requireAll(read("desktop/tests/embedded-browser.integration.cjs"), ["work-report-summary-r6.integration.cjs"], "R6 mandatory merged report UI gate");
requireAll(read("renderer/src/collection-task-rows.ts"), ['metric("source", `${modeLabel}总数`', 'metric("discovered", "已识别"', 'metric("unobserved", "未识别"', 'metric("deduped", "去重"', 'metric("pending-work", "队列"', 'metric("discarded", "丢弃"', 'metric("qualified", "合格"', 'Math.max(0, total - discovered)', 'discovered - processed'], "R6.1 seven truthful source counters and separate unseen gap");
requireAll(read("renderer/src/collection-task-rows.ts"), ["qualified_for_review", 'metric("qualified", "合格", qualified ?? "—"'], "r75 confirmed manual-review admission display");
requireAll(read("renderer/tests/collection-task-rows.test.mjs"), ["qualified count comes only from confirmed manual-review admissions", "合格 —"], "r75 unknown and confirmed qualification count regression");
requireAll(read("scripts/build_windows.ps1"), ["test_*r46.py", "collection-progress-r46-full.log", "$CollectionProgressR46ExitCode -ne 0"], "r75 backend admission tests remain a mandatory Windows gate");
requireAll(read("backend/tests/test_progress_dedupe_r46.py"), ["test_qualified_count_is_actual_review_admission_not_saved_or_approved", "test_missing_or_wrong_source_cannot_inflate_actual_review_admissions", "qualified_for_review"], "r75 backend review admission and unknown-history tests");
requireAll(read("backend/app/service.py"), ["qualified_for_review", "workbench_progress_totals"], "r75 exact progress projection lookup");
requireAll(read("backend/app/workbench_progress_aggregates.py"), ["recorded.target_id=review.source_target", "recorded.account_id=review.account_id", "source.value=review.source_mode"], "r75 per-target and per-mode durable review attribution");
requireAll(read("renderer/src/main.tsx"), ["./workbench-polish-r55.css"], "r55 global visual layer");
requireAll(read("scripts/build_windows.ps1"), ["test_*r56.py", "report-split-r56-full.log", "$ReportSplitR56ExitCode -ne 0"], "r56 period split report regression gate");
for (const path of ["backend/tests/test_recovery_wait_r25.py", "backend/tests/test_returned_close_r90.py", "backend/tests/test_returned_queue_r90.py", "backend/tests/test_returned_window_release_r73.py", "DIAGNOSE_UNREAD_PROFILES.bat", "scripts/diagnose_unread_profiles.py", "scripts/tests/test_diagnose_unread_profiles.py", "backend/tests/test_unread_profile_queue_r72.py", "backend/tests/test_split_recovery_fence_r72.py"]) requireFile(path);
requireAll(read("scripts/build_windows.ps1"), ["test_returned*.py", "window-queue-r73-full.log", "$WindowQueueR73ExitCode -ne 0", "test_*r25.py", "test_candidate_spool.py", "test_split_delayed_dispatch.py", "test_relation_longrun.py", "test_parallel_screening_worker.py"], "r90 returned-window release, dedupe and queue regression gate");
requireInOrder(read("scripts/build_windows.ps1"), [
  '$SchemaSmokeExitCode =', '$RecoveryRegressionExitCode =',
  '$CollectionWaitR53ExitCode =', '$WindowQueueR73ExitCode =',
  '$NativeBrowserExitCode =', '$DesktopBuildExitCode =',
], 'r91 early recovery, watchdog and queue gates');
requireAll(read("scripts/build_windows.ps1"), ["test_*r72.py", "hover-queue-r72-full.log", "$HoverQueueR72ExitCode -ne 0", "test_hover*_r6*.py", "test_diagnose_unread_profiles.py"], "r72 hover and queue regression gate");
requireAll(read("scripts/build_windows.ps1"), ["test_*r54.py", "review-reports-r54-full.log", "$ReviewReportsR54ExitCode -ne 0"], "review-layer and split audit regression gate");
requireAll(read("desktop/src/core-request-policy.ts"), ['"/api/workbench/review/query"', '"/api/reports/split-review"'], "desktop review and report API bridge");
for (const path of ["desktop/src/account-task-control.ts", "desktop/tests/account-task-control-r51.test.mjs", "backend/tests/test_relation_dom_r51.py", "backend/tests/test_relation_surface_r51.py", "backend/tests/test_relation_diagnostics_r51.py", "backend/tests/test_manual_surface_r51.py", "backend/tests/test_manual_collection_r51.py", "backend/tests/test_manual_api_r51.py"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/account-task-control-r51.test.mjs"], "confirmed task interference regression gate");
requireAll(read("scripts/build_windows.ps1"), ["test_*r51.py", "collection-manual-r51-full.log"], "real relation DOM and cooperative manual control regression gate");
for (const path of ["desktop/tests/navigation-fixture.cjs", "desktop/tests/navigation-fixture-r49.test.cjs"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/navigation-fixture-r49.test.cjs"], "exact fixture document identity and bounded navigation gate");
requireAll(read("desktop/tests/task-watch.integration.cjs"), ["waitFixtureDocument", "loadFixtureDocument", "source-before-display", "manual-initial-document"], "native fixture document readiness checkpoints");
for (const path of ["desktop/tests/task-watch-close-r48.test.cjs", "desktop/tests/view-cleanup-r48.test.cjs", "desktop/tests/native-destroy-r48.test.cjs"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/task-watch-close-r48.test.cjs", "desktop/tests/view-cleanup-r48.test.cjs", "desktop/tests/native-destroy-r48.test.cjs"], "native contents destruction regression gate");
requireAll(read("desktop/tests/task-watch.integration.cjs"), ["closeFixtureContents(contents", "!p.pages.has(page.targetId)"], "observed contents destruction and page registry removal");
for (const path of ["desktop/tests/task-watch-fixture.cjs", "desktop/tests/integration-failure.cjs", "desktop/tests/task-watch-r47.test.cjs", "desktop/tests/integration-failure-r47.test.cjs"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/task-watch-r47.test.cjs", "desktop/tests/integration-failure-r47.test.cjs"], "native task-watch timing and first-failure regression gate");
requireAll(read("desktop/tests/task-watch.integration.cjs"), ["waitTaskWatchCondition", "waitRendererFixture", "integrationCheckpoint"], "task-watch observed-state verification");
requireAll(read("desktop/tests/task-watch.integration.cjs"), ["['采集页','1-1','1-2','1-3'].slice(0,workers+1)", "role:'screening',slot:i"], "r93 fixed-slot task-watch fixture");
requireAll(read("renderer/tests/fixtures/account-recovery.tsx"), ["label:'1-'+(i+1)", "screening_slots:3"], "r93 fixed-slot renderer fixture");
requireAll(read("desktop/tests/embedded-browser.integration.cjs"), ["关闭1-2", "等待 1-2 子页就绪"], "r93 fixed-slot native UI fixture");
requireAll(read("desktop/tests/embedded-browser.integration.cjs"), ["JUXIN_EMBEDDED_FAILURE", "integrationFailureSummary", "writeIntegrationFailure"], "native integration failure evidence");
requireAll(read("scripts/build_windows.ps1"), ["embedded-browser-failure.json", "$EmbeddedFailureCause", "$EmbeddedExitCode -ne 0"], "native integration first-failure build output");
requireAll(read("scripts/build_windows.ps1"), ["test_*r46.py", "collection-progress-r46-full.log"], "durable per-source dedupe progress regression gate");
for (const path of ["backend/tests/test_progress_dedupe_r46.py", "desktop/tests/task-display-r46.test.mjs"]) requireFile(path);
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/task-display-r46.test.mjs"], "task display lifecycle regression gate");
requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/browser-surface-r33.test.mjs", "renderer/tests/collection-task-rows.test.mjs"], "task surface race and progress UI regression gate");
requireAll(read("scripts/build_windows.ps1"), ["test_*r45.py", "collection-profile-r45-full.log"], "profile readiness and location semantics regression gate");
for (const path of ["backend/tests/test_zero_profile_reader_r45.py", "backend/tests/test_zero_pipeline_r45.py", "backend/tests/test_private_indicator_r45.py", "backend/tests/test_location_readiness_r45.py", "backend/tests/test_location_pipeline_r45.py"]) requireFile(path);
requireAll(read("scripts/build_windows.ps1"), ["test_*r44.py", "collection-completion-r44-full.log"], "durable completion and cancellation regression gate");
for (const path of ["backend/tests/test_completion_persistence_r44.py", "backend/tests/test_pipeline_completion_r44.py", "backend/tests/test_source_completion_r44.py"]) requireFile(path);
requireAll(read("scripts/build_windows.ps1"), ["test_*r43.py", "collection-completion-r43-full.log"], "natural completion and delete/return regression gate");
for (const path of ["backend/tests/test_relation_completion_r43.py", "backend/tests/test_completion_runtime_r43.py", "backend/tests/test_collection_remove_r43.py"]) requireFile(path);

requireAll(read("scripts/build_windows.ps1"), ["test_specified_windows_r34.py", "specified-windows-regression-full.log"], "specified-window recovery generation gate");

requireAll(JSON.parse(read("package.json")).scripts["test:renderer"], ["renderer/tests/window-assignment-r34.test.mjs"], "specified-window UI regression gate");


requireAll(read("backend/requirements.txt"), ["tzdata==2026.4"], "r57 Windows timezone dependency");
requireAll(read("backend/pyproject.toml"), ['"tzdata==2026.4"'], "r57 development timezone dependency");
requireFile("scripts/verify_timezone_data.py");
requireFile("scripts/tests/test_timezone_data_r57.py");
requireInOrder(read("scripts/install_windows.ps1"), [
  '$PythonDependenciesExitCode -ne 0', 'scripts\\verify_timezone_data.py',
  '$TimezoneDataExitCode -ne 0', '[4/8] Checking Microsoft Visual C++ runtime'
], "r57 early build timezone preflight");
requireAll(read("scripts/verify_timezone_data.py"), ['zoneinfo.reset_tzpath(())', 'zoneinfo.ZoneInfo.clear_cache()', 'TIMEZONE_DATA_CHECK=FAIL'], "r57 package-only timezone verification");
requireAll(read("ci/public_ci.py"), ["test_timezone_data_r57.py", "scripts/tests", "unittest", "discover"], "r57 Windows timezone CI regression");

const required = [
  "desktop/tests/renderer-fixture.cjs",
  "desktop/tests/renderer-fixture-r42.test.cjs",
  "desktop/tests/floating-cleanup-r42.test.cjs",

  "renderer/src/workbench-polish-r41.css",
  "renderer/tests/collection-settings-r41.test.mjs",
  "backend/tests/test_nurture_delete_r41.py",
  "backend/tests/test_nurture_delete_ui_r41.py",
  "renderer/tests/build-nurture-r41-fixture.mjs",
  "renderer/tests/fixtures/studio-nurture-r41.tsx",

  "package.json",
  "package-lock.json",
  "backend/pyproject.toml",
  "backend/app/__init__.py",
  "README.md",
  "README_CN.md",
  "docs/PUBLIC_BUILD.md",
  "docs/CI_SOURCE_IDENTITY.md",
  "BITBROWSER_V2_RELEASE.md",
  "NEWGEN_RELEASE_STATUS.md",
  "RELEASE_VERIFICATION.md",
  "docs/ACCEPTANCE_CHECKLIST.md",
  "renderer/vite.config.ts",
  "renderer/src/App.tsx",
  "renderer/src/auth-gate.tsx",
  "renderer/src/auth-session-rollback.ts",
  "renderer/src/greeting-messages.ts",
  "renderer/src/formal-workbench.tsx",
  "renderer/src/collection-task-rows.ts",
  "renderer/tests/collection-task-rows.test.mjs",
  "renderer/src/workbench-command-state.ts",
  "renderer/tests/workbench-command-state.test.mjs",
  "backend/tests/test_task_control_r27.py",
  "scripts/run_backend_tests.py",
  "scripts/tests/test_backend_test_runner.py",
  "renderer/src/formal-workbench.css",
  "renderer/src/core-client.ts",
  "renderer/src/desktop.d.ts",
  "renderer/src/global.css",
  "desktop/src/main.ts",
  "desktop/src/atomic-secure-store.ts",
  "desktop/src/preload.cts",
  "desktop/src/core-request-policy.ts",
  "desktop/src/navigation-policy.ts",
  "backend/app/main.py",
  "backend/app/schemas.py",
  "backend/app/service.py",
  "backend/app/action_manager.py",
  "backend/app/execution_manager.py",
  "backend/app/playwright_worker.py",
  "backend/app/database.py",
  "backend/app/bitbrowser_api.py",
  "backend/app/bitbrowser_v2.py",
  "backend/tests/test_greeting_inbox_flow.py",
  "backend/tests/test_greeting_first_result.py",
  "backend/tests/test_greeting_send_flow.py",
  "backend/tests/test_greeting_queue_safety.py",
  "backend/tests/test_core.py",
  "renderer/tests/auth-session-rollback.test.mjs",
  "renderer/tests/core-client.test.mjs",
  "scripts/build_windows.ps1",
  "scripts/build_portable_windows.ps1",
  "scripts/verify_backend_smoke.py",
  "build_installer_windows.bat",
  "START_HERE_NEWGEN.bat",
  ".github/workflows/public-windows-verify.yml",
];
required.forEach(requireFile);

for (const legacyPath of [
  "renderer/src/workbench.tsx",
  "renderer/src/workspace-pages.tsx",
  "renderer/src/runtime-mode.ts",
  "renderer/src/bitbrowser-connection.ts",
  "renderer/src/bitbrowser-profile-request-gate.js",
  "renderer/.env.simulator.example",
  "backend/app/bitbrowser.py",
  "START_HERE_一键生成安装包.bat",
]) {
  assert(!existsSync(at(legacyPath)), `retired source is still packaged: ${legacyPath}`);
}
for (const legacyBytecode of filesBelow("backend/app/__pycache__", (path) =>
  /(?:^|[\\/])bitbrowser\.[^.]+\.pyc$/i.test(path)
)) {
  fail(`retired BitBrowser bytecode is still packaged: ${legacyBytecode.slice(root.length + 1)}`);
}

const packageJson = JSON.parse(read("package.json"));
assert(packageJson.name === "juxin-ig-audience-collector-newgen", "package name is not NewGen");
assert(/^\d+\.\d+\.\d+$/.test(String(packageJson.version)), `invalid application version: ${String(packageJson.version)}`);
const releaseVersion = String(packageJson.version);
const packageLock = JSON.parse(read("package-lock.json"));
assert(packageLock.version === releaseVersion, "package-lock top-level version differs from package.json");
assert(packageLock.packages?.[""]?.version === releaseVersion, "package-lock root package version differs from package.json");
const pyprojectVersion = read("backend/pyproject.toml").match(/^version\s*=\s*"([^"]+)"/m)?.[1];
const coreVersion = read("backend/app/__init__.py").match(/^__version__\s*=\s*"([^"]+)"/m)?.[1];
assert(pyprojectVersion === releaseVersion, "Python package version differs from package.json");
assert(coreVersion === releaseVersion, "Core runtime version differs from package.json");
for (const [path, label] of [
  ["README.md", "English README"],
  ["README_CN.md", "Chinese README"],
  ["BITBROWSER_V2_RELEASE.md", "BitBrowser release notes"],
  ["NEWGEN_RELEASE_STATUS.md", "NewGen release status"],
  ["RELEASE_VERIFICATION.md", "release verification"],
  ["docs/ACCEPTANCE_CHECKLIST.md", "acceptance checklist"],
]) {
  assert(read(path).includes(`v${releaseVersion}`), `${label} does not identify v${releaseVersion}`);
}
const releaseStatusSource = read("NEWGEN_RELEASE_STATUS.md");
assert(
  releaseStatusSource.startsWith(`# 聚鑫国际 v${releaseVersion} 发布状态`),
  "NewGen release status heading does not identify the current application version",
);
requireAll(
  releaseStatusSource,
  [`- 版本：${releaseVersion}`, `NewGen-Setup-${releaseVersion}-x64.exe`],
  "current NewGen release identity",
);
const expectedSetupChecksum = `Juxin-IG-Audience-Collector-NewGen-Setup-${releaseVersion}-x64.exe.sha256`;
requireAll(
  releaseStatusSource + read("RELEASE_VERIFICATION.md"),
  [expectedSetupChecksum, "LATEST_SUCCESS.txt", "SHA-256"],
  "final Setup checksum documentation",
);
assert(packageJson.main === "dist-electron/main.js", "Electron entry point is incorrect");
assert(packageJson.build?.appId === "com.juxin.igaudiencecollector.newgen", "NewGen application id is incorrect");
assert(packageJson.build?.productName === "聚鑫国际", "NewGen product name is incorrect");
assert(
  packageJson.build?.win?.artifactName === "Juxin-IG-Audience-Collector-NewGen-Setup-${version}-${arch}.${ext}",
  "Windows installer name must identify NewGen",
);
assert(packageJson.build?.nsis?.oneClick === true, "NSIS installer must remain one-click");
assert(packageJson.build?.nsis?.allowToChangeInstallationDirectory === false, "one-click install must not prompt for a directory");
assert(String(packageJson.scripts?.build || "").split("&&").at(-1)?.trim()
  === "node scripts/verify_desktop_build.mjs",
  "release build must end with the full desktop verifier without source-only or ignored failures");
assert(
  String(packageJson.scripts?.["dist:win"] || "").includes("--publish never"),
  "Windows packaging must disable electron-builder implicit CI publishing",
);

const appSource = read("renderer/src/App.tsx");
const authSource = read("renderer/src/auth-gate.tsx");
const authRollbackSource = read("renderer/src/auth-session-rollback.ts");
const greetingMessagesSource = read("renderer/src/greeting-messages.ts");
const workbenchSource = read("renderer/src/formal-workbench.tsx");
const workbenchCssSource = read("renderer/src/formal-workbench.css");
const coreClientSource = read("renderer/src/core-client.ts");
const viteSource = read("renderer/vite.config.ts");
const desktopMainSource = read("desktop/src/main.ts");
const atomicSecureStoreSource = read("desktop/src/atomic-secure-store.ts");
const desktopPreloadSource = read("desktop/src/preload.cts");
const requestPolicySource = read("desktop/src/core-request-policy.ts");
const navigationPolicySource = read("desktop/src/navigation-policy.ts");
const backendMainSource = read("backend/app/main.py");
const backendSchemasSource = read("backend/app/schemas.py");
const backendServiceSource = read("backend/app/service.py");
const actionManagerSource = read("backend/app/action_manager.py");
const executionManagerSource = read("backend/app/execution_manager.py");
const playwrightWorkerSource = read("backend/app/playwright_worker.py");
const directGreetingTestSource = read("backend/tests/test_greeting_first_result.py");
const directGreetingInboxTestSource = read("backend/tests/test_greeting_inbox_flow.py");
const directGreetingSendTestSource = read("backend/tests/test_greeting_send_flow.py");
const directGreetingQueueSafetyTestSource = read("backend/tests/test_greeting_queue_safety.py");
const backendCoreTestSource = read("backend/tests/test_core.py");
const backendWorkbenchTestSource = read("backend/tests/test_workbench_newgen.py");
const rendererWorkbenchTestSource = read("renderer/tests/auth-session-rollback.test.mjs");
const rendererCoreClientTestSource = read("renderer/tests/core-client.test.mjs");
const databaseSource = read("backend/app/database.py");
const bitBrowserApiSource = read("backend/app/bitbrowser_api.py");
const bitBrowserV2Source = read("backend/app/bitbrowser_v2.py");
const windowsBuilderSource = read("scripts/build_windows.ps1");
// Fresh release extraction has no dist-electron output. The early native
// cleanup gate must compile its real host and propagate compiler failure;
// the later full desktop build cannot satisfy this earlier prerequisite.
requireInOrder(windowsBuilderSource, [
  '& "$PSScriptRoot\\install_windows.ps1"',
  '$NativePreflightBuildExitCode = Invoke-IgacNativeCommandWithLog',
  '-ArgumentList @("run", "build:electron")',
  'if ($NativePreflightBuildExitCode -ne 0) {',
  'throw "Compile desktop host for native preflight failed',
  '$CleanupNativeExitCode = Invoke-IgacNativeCommandWithLog',
  '$DesktopBuildExitCode = Invoke-IgacNativeCommandWithLog',
], 'R6.3 native cleanup compilation prerequisite');
const portableBuilderSource = read("scripts/build_portable_windows.ps1");
const builderBatchSource = read("build_installer_windows.bat");
const startHereSource = read("START_HERE_NEWGEN.bat");
const workflowSource = read(".github/workflows/public-windows-verify.yml");
const backendSmokeSource = read("scripts/verify_backend_smoke.py");

// Retired publishing implementations cannot silently return to either product.
for (const path of [
  'backend/app/posting_workflow.py', 'backend/app/posting_executor.py',
  'backend/app/instagram_publisher.py', 'backend/app/instagram_crop.py',
  'backend/app/instagram_crop_dom.py', 'backend/app/posting_pexels.py',
  'renderer/src/posting-workspace.tsx', 'renderer/src/posting-workspace.css',
  'desktop/src/pexels-integration.ts',
]) assert(!existsSync(at(path)), 'retired publishing implementation remains: ' + path);
forbidAll(appSource + '\n' + coreClientSource, [
  'PostingWorkspace', 'posting-workspace', '/api/posting/', '/api/internal/integrations/pexels',
], 'retained renderer routes and Core client');

// Exactly six formal routes, protected by the local authentication gate.
requireAll(appSource, [
  '"collection"', '"review"', '"public"', '"private"', '"history"', '"settings"',
  '"/review"', '"/public"', '"/private"', '"/history"', '"/settings"',
  "<AuthGate>", "<FormalWorkbench mode={mode}",
], "formal route adapter");
forbidAll(appSource, ["WorkspaceApp", "<Workbench ", "workspace-pages"], "formal route adapter");

// Authentication is fail-closed and only persists a session token through
// Electron's secure storage bridge.
requireAll(authSource, [
  '"/api/session/register"', '"/api/session/login"', '"/api/session/resume"',
  'secureGet("session-token")', 'secureSet("session-token"', 'secureDelete("session-token")',
  "validateAuthResponse", 'state === "blocked"',
  "rollbackAuthenticatedSession",
], "formal authentication gate");
requireAll(authRollbackSource, ["client.logout()", 'secureDelete("session-token")'], "session rollback");
forbidAll(authSource, ["localStorage", "sessionStorage", "@demo", "fixture", "mockUser"], "formal authentication gate");

// UI state and mutations must use one typed local Core client. The production
// UI must not contain fixtures or a parallel BitBrowser connector.
requireAll(coreClientSource, [
  "CollectorCoreUnavailableError", "CollectorCoreProtocolError", "requireCollectorCoreBridge",
  "/api/workbench/snapshot?limit=", "/api/workbench/commands", "startWorkbenchSnapshotPolling",
  '"dedupe_claim"', '"review_decision"', '"bitbrowser_refresh"',
  '"task_create"', '"task_control"', '"action_campaign_start"', '"action_campaign_control"',
  '"action_target_control"', '"action_failure_dismiss"', '"action_unknown_resolve"', '"approved_candidate_dismiss"',
  'storage_cache_clear:', 'clearStorageCache() { return this.command("storage_cache_clear", {}); }',
  "business_records_retained: true", "pending_review_previews: true",
  "latestAppliedSnapshotRevision", "lastCommandSnapshotSeq", "#snapshotRequests",
  "staleSnapshotMaxAttempts = 4", "readFreshSnapshot", "readDeliverableSnapshot", "requiredRevision",
  "snapshot.revision >= requiredRevision", "Core returned stale snapshot revision",
  "Core snapshot became stale before renderer delivery", "deliverSnapshot", "no Promise boundary may sit between them",
], "formal Core client");
forbidAll(coreClientSource, ["window.localStorage", "globalThis.fetch", "fetch(`", "@simulator", "mockSnapshot", "fixture"], "formal Core client");
requireAll(greetingMessagesSource, [
  "MAX_GREETING_MESSAGE_CHARACTERS = 200", "parseGreetingMessages",
  "Array.from(value).length", 'code: "empty" | "too_long"',
  "请至少输入一条非空打招呼话术", "超过 ${MAX_GREETING_MESSAGE_CHARACTERS} 字符上限",
  "resolveSuccessfulGreetingMessage", "details.greeting_message",
  "attemptRecord.message", "campaignMessage",
], "greeting message UI validator");
requireAll(workbenchSource, [
  'from "./core-client"', "startWorkbenchSnapshotPolling", "getCollectorCoreClient",
  'from "./greeting-messages"', "parseGreetingMessages(messages)",
  "refreshBitBrowserWindows", "createCollectionTask", "controlCollectionTask",
  "controlActionTarget", "dismissActionFailure", "resolveUnknownAction", "dismissApprovedCandidate",
  "setSelectedOrder", "selectedWindowOrder", "formal-capacity-banner",
  "exclude_verified",
  'type ReviewView = "public" | "private"',
  "批量不合格", "批量合格", "reviewMutationRef", "candidateIdsRef",
  "确认未执行，退回待执行", "确认已执行，记为成功",
  "一键删除并退回", "结果未知项不会被处理", "ordinaryFailures", "failure-dismiss-all-",
  "公开页面 · 自动打招呼", "私密页面 · 自动点关注", "全局去重",
  "SNAPSHOT_PAGE_LIMIT = 2_000", 'STORAGE_CACHE_CLEAR_BUSY_KEY = "storage-cache-clear"',
  "truncatedSnapshotScopes", "storageCleanupNotice", "pending_preview_bytes", "last_maintenance_at",
  'aria-label="刷新" title="刷新"', "refreshInFlightRef.current", "core.loading || core.refreshing", "仅清理预览缓存",
  "完整业务记录和 Core 总数仍会保留", "SQLite 业务记录不设应用额度",
  "仅受本机可用磁盘空间限制", "清理预览不会减少 Core 总数", "待审核预览",
  "busyKeysRef.current.has(key)", "busyKeysRef.current.add(key)", "busyKeysRef.current.delete(key)",
  "setAllDiscardLimitsUnlimited", "全部不限", "公开帖子活跃度上限（天）",
  "if (isPublic && !greetingMessages.ok) return;", "message: lines[0], messages: lines",
  "resolveSuccessfulGreetingMessage", "实际话术", "formal-history-message",
  '["split_candidates", "分裂号队列"]',
  "snapshot?.counts.pending_public", "snapshot?.counts.pending_private", "publicReviewCount + privateReviewCount",
  '<StableReviewWorkspace snapshot={snapshot} run={reviewRun} disabled={reviewDisabled} />',
  '<RecoveryCollectionControls live={core.liveStatus} controls={collectionControls} />',
  'snapshot.counts[isPublic ? "approved_public" : "approved_private"]',
  "retainedHistory.approved", "SQLite + WAL 占用（字节）", "storage.wal_bytes",
  "本次快照更新失败，正在自动重试", "总览数据暂未更新；独立读取正常的审核名单仍可操作，已有任务可暂停或停止",
  "立即重试",
  'firstNumber(itemProfile, ["posts", "posts_count", "media_count"])',
  '<span>粉丝</span><span>关注</span><span>帖子</span><span>采集时间</span><span>头像与账号</span><span className="quick-review-select-head">选择</span><span>是否合格</span>',
  '<span>采集时间</span><span>所在地</span><span>帖子数</span><span>粉丝数</span><span>关注数量</span><span>活跃度</span><span className="quick-review-avatar-head">头像</span><span>账号</span><span className="quick-review-select-head">多选</span><span>功能按键</span>',
  'itemLocation === "—" ? "未知" : itemLocation', "0 帖（待人工）",
  "collectionExclusionReason", "粉丝数过高", "关注数过高", "帖子数过高", "活跃度超过限制",
  "fallbackByCode[raw] || fallbackByCode[record.reason_code]", "raw === record.reason_code",
  "action-runtime-column", "action-runtime-section", "collection-middle-column", "collection-execution-column",
  "action-bottom-list", "sourcesById.get(String(candidate.source_target_id || \"\"))",
  'collectionModeProgressText(displayMode, asRecord(progress[mode]))',
  'unavailableText="原任务进度记录不可用"',
], "formal workbench");
// The visible discard card is now the sole numeric/post-age policy. Legacy
// qualification values must never be silently submitted by new task controls.
requireAll(JSON.parse(read("package.json")).scripts["test:desktop"], ["desktop/tests/renderer-fixture-r42.test.cjs", "desktop/tests/floating-cleanup-r42.test.cjs"], "bounded renderer fixture regression gate");
requireAll(read("desktop/tests/floating-fixture.cjs"), ["rendererFixtureRead", "floating parent layout", "rendererTimeoutMs = 5000"], "bounded floating parent layout probe");
for (const path of ["desktop/tests/floating-fixture.cjs", "desktop/tests/embedded-browser.integration.cjs", "desktop/tests/core-route-stability.integration.cjs"]) {
  forbidAll(read(path), ["requestAnimationFrame("], "frame-independent embedded fixture " + path);
}
const collectionWorkspaceStart = workbenchSource.indexOf("function CollectionWorkspace(");
const collectionWorkspaceEnd = workbenchSource.indexOf("function CollectionTaskList(", collectionWorkspaceStart);
assert(collectionWorkspaceStart >= 0 && collectionWorkspaceEnd > collectionWorkspaceStart,
  "collection settings contract cannot locate the real workspace");
const collectionWorkspaceSource = workbenchSource.slice(collectionWorkspaceStart, collectionWorkspaceEnd);
requireAll(collectionWorkspaceSource, [
  "discardCountLimits: readDiscardCountLimits(settings)",
  "const filtersValid = (!discardLimitsError)", "function setAllDiscardLimitsUnlimited()",
  'DISCARD_LIMIT_KEYS.map(key => [key, "0"])', "source_limits: {}",
  '...(discardCountLimitsPayload(discardLimits))', 'className="collection-settings-panel"', 'id="collection-discard-title"',
  'visibility === "public" ? <>',
  "公开帖子活跃度上限（天）", "discardLimits.limits.public_discard_active_days_max",
  "public_discard_active_days_max: event.target.value", 'min="0" step="1"',
  "disabled={!discardLimits.enabled}", "启用直接丢弃",
], "single visible direct-discard settings and public post activity");
// Pure IG removes the foreign runtime and switch, rather than merely hiding it.
requireAll(collectionWorkspaceSource, [
  'useWorkbenchPlatform()', 'platform: taskPlatform', 'targets: []', 'source_limits: {}',
  'auto_classify: autoClassify', 'gpt_review: openAiReview',
  'createTaskRef.current', 'item.id === id && !item.locked',
], "pure IG collection contract");
requireAll(workbenchSource, ['href={collectionProfileUrl(item)}',
  'const canReturnToWaiting = Boolean(target) && !SUCCESS_STATES.has(state)',
], "pure IG review and interaction contract");
requireAll(read("renderer/src/workbench-platform.tsx"), [
  'const INSTAGRAM = { platform: "instagram" as const }',
  'getCollectorCoreClient(undefined, "instagram")',
], "pure IG fixed platform client");
forbidAll(collectionWorkspaceSource, ['facebook_relation_strategy', 'FacebookProfileFacts', 'togglePlatform'], "removed platform UI");
requireAll(packageJson.scripts["test:renderer"], ["renderer/tests/collection-platform.test.mjs",
  "renderer/tests/workbench-platform-r95.test.mjs"], "pure IG renderer regression gate");

forbidAll(collectionWorkspaceSource, [
  "followersMax", "followingMax", "postsMax", "activityDays", "filterValues",
  "followersMin", "followingMin", "postsMin", "setAllCollectionLimitsUnlimited",
], "retired qualification limit controls");
const discardLimitsSource = read("renderer/src/collection-discard-limits.ts");
const discardSettingKeys = [
  "private_discard_followers_max", "private_discard_following_max", "private_discard_posts_max",
  "public_discard_followers_max", "public_discard_following_max", "public_discard_posts_max",
  "public_discard_active_days_max",
];
requireAll(discardLimitsSource, [
  ...discardSettingKeys, 'key === "public_discard_active_days_max" ? 0 : 4000',
  "discard_count_limits_enabled", "Number.isSafeInteger", "discardCountLimitsError",
], "typed discard setting defaults and validation");
for (const [source, label] of [[coreClientSource, "Core client"], [backendSchemasSource, "Core schema"],
  [backendMainSource, "Core command routing"], [backendServiceSource, "Core settings persistence"]]) {
  requireAll(source, discardSettingKeys, `${label} direct-discard fields`);
}
const reviewWorkspaceStart = workbenchSource.indexOf("function ReviewWorkspace(");
const reviewWorkspaceEnd = workbenchSource.indexOf("function ActionWorkspace(", reviewWorkspaceStart);
assert(reviewWorkspaceStart >= 0 && reviewWorkspaceEnd > reviewWorkspaceStart,
  "private review contract cannot locate the real workspace");
const reviewWorkspaceSource = workbenchSource.slice(reviewWorkspaceStart, reviewWorkspaceEnd);
requireAll(reviewWorkspaceSource, [
  'type ReviewView = "public" | "private"',
  "client.reviewQueue(query)", "core.moveReviewStage", "candidate_ids: candidateIds",
  "const candidates = page?.items ?? [];",
  "新采集结果", "等待筛选", "私密审核",
], "manual review layers with independently paginated retained queues");
requireAll(reviewWorkspaceSource, ["counts.public.stage1 + counts.public.stage2", "counts.private.stage1 + counts.private.stage2"], "complete pending counts across both review layers");
requireAll(packageJson.scripts["test:renderer"], ["renderer/tests/review-report-client-r54.test.mjs", "renderer/tests/review-stages-r54.test.mjs", "renderer/tests/split-review-r54.test.mjs", "renderer/tests/collection-fixed-footer-r54.test.mjs"], "review layers, split audit and fixed footer UI regression gates");
forbidAll(reviewWorkspaceSource, ["private-secondary", "privateSecondary", "私密二审"],
  "retired private second-review UI");
requireAll(windowsBuilderSource, ["test_*r38.py", "discard-workflow-r38-regression-full.log",
  "$DiscardWorkflowRegressionExitCode -ne 0"], "direct-discard workflow regression gate");
requireAll(windowsBuilderSource, ["test_*r39.py", "preopen-dedupe-r39-regression-full.log",
  "$PreopenDedupeRegressionExitCode -ne 0"], "durable discard and pre-navigation dedupe regression gate");
requireAll(packageJson.scripts["test:renderer"], ["renderer/tests/public-activity-discard-r38.test.mjs"],
  "public post activity UI regression gate");
for (const path of ["START_HERE_NEWGEN.bat", "BUILD_PORTABLE.bat", "build_installer_windows.bat"]) {
  requireAll(read(path), ["discard-workflow-r38-regression-full.log"], "r38 failure log collection");
  requireAll(read(path), ["preopen-dedupe-r39-regression-full.log"], "r39 failure log collection");
}
requireAll(workbenchSource, [
  'collectionControls.feedback("collection-create").pending',
  "snapshotErrorRef.current = reason", "snapshotErrorRef.current = null",
  "if (!backgroundRefresh && snapshotErrorRef.current)",
  "performCollectionSafetyControls", "runWithSnapshotRefresh",
], "responsive collection controls with stale-snapshot protection");
requireAll(backendMainSource, ["expected_target_id=payload.target_id"], "target-bound pause and stop routing");
requireAll(executionManagerSource, ["def _validate_window_target_control(", "requested_target_id"], "target-bound collection controls");
requireAll(windowsBuilderSource, ["test_task_control_r27.py", "task-control-regression-full.log"], "target-bound control regression gate");
requireAll(windowsBuilderSource, ["scripts\\run_backend_tests.py", "test_backend_test_runner.py", "backend-test-runner-full.log"], "isolated backend test entry");
forbidAll(windowsBuilderSource, [
  "-m unittest discover -s backend\\tests",
  '\"-m\", \"unittest\", \"discover\", \"-s\", \"backend\\tests\"',
], "backend test gates must initialize their own import roots");
assert(
  workbenchSource.split('"storage-cache-clear"').length - 1 === 1,
  "storage cleanup busy key must have one shared literal definition",
);
const settingsWorkspaceStart = workbenchSource.indexOf("function SettingsWorkspace");
const formalWorkbenchStart = workbenchSource.indexOf("function LiveFormalWorkbench");
assert(settingsWorkspaceStart !== -1 && formalWorkbenchStart > settingsWorkspaceStart, "storage cleanup UI sections are missing");
const settingsWorkspaceSource = workbenchSource.slice(settingsWorkspaceStart, formalWorkbenchStart);
const shellSource = workbenchSource.slice(formalWorkbenchStart);
requireAll(settingsWorkspaceSource, [
  "disabled(STORAGE_CACHE_CLEAR_BUSY_KEY)",
  "STORAGE_CACHE_CLEAR_BUSY_KEY,",
  "(client) => client.clearStorageCache()",
  "storageCleanupNotice",
], "settings safe cleanup action");
// The R6 shell hides status decoration and capacity shortcuts only. Keep the
// authoritative reader and settings cleanup/version diagnostics intact.
forbidAll(shellSource, [
  "部分列表显示最近", "前往设置", "仅清理预览缓存", "clearStorageCache",
  "界面 r94 / Core", 'className={`formal-live', "刷新页面",
], "simplified R6 shell");
requireAll(shellSource, [
  '<Rail mode={mode} refresh={core.refresh} refreshing={core.loading || core.refreshing} />',
  "snapshot.dedupe.total",
], "R6 wolf safe refresh and dedupe");
requireAll(settingsWorkspaceSource, ["Core 修订版本", "snapshot.source_revision"], "settings Core revision diagnostics");
forbidAll(workbenchSource, [
  "localStorage", "sessionStorage", "Math.random", "@demo", "@simulator",
  "Jenny Liu", "Mia Wong", "Grace Wu", "Kelly Huang", "模拟数据效果图", "公开二审",
  "Core 快照已达到当前容量上限", "缓存和临时文件已清理", "手动清理缓存",
  "每类最多显示最近", "storage.temporary_bytes", "<span>临时文件</span>",
  "单目标采集硬上限", "perTargetLimit:",
], "formal workbench");
const privateRowStart = workbenchSource.indexOf('{queue === "private" ? <>');
const privateRowEnd = workbenchSource.indexOf('</> : <>', privateRowStart);
assert(privateRowStart !== -1 && privateRowEnd !== -1, "private review row branch is missing");
requireInOrder(workbenchSource.slice(privateRowStart, privateRowEnd), [
  'firstNumber(itemProfile, ["followers", "followers_count", "follower_count"])',
  'firstNumber(itemProfile, ["following", "following_count"])',
  'firstNumber(itemProfile, ["posts", "posts_count", "media_count"])',
  "{formatTime(item.created_at)}",
  'className="quick-review-account formal-profile-identity-link"',
], "private review row layout");
requireAll(workbenchCssSource, [
  ".private-review-list .quick-review-table-head, .private-review-list .quick-review-row { grid-template-columns: repeat(3, minmax(90px, .6fr)) minmax(150px, .85fr) minmax(310px, 1.7fr) 52px minmax(210px, 1fr); }",
  ".private-review-list .formal-avatar.small, .public-review-list .formal-avatar.small { width: 87px; height: 87px;",
  ".quick-review-panel { margin-top: 0; container-name: quick-review; container-type: inline-size; }",
  ".quick-review-actions { width: 100%; min-width: 0; flex-wrap: wrap; }",
  "@container quick-review (max-width: 1280px)",
  "@container quick-review (max-width: 900px)",
  ".action-runtime-column { display: grid; grid-template-rows: auto minmax(0, 1fr);",
  ".action-bottom-list .formal-scroll.short { min-height: 260px; max-height: 440px; }",
  ".collection-workbench-grid { height: clamp(610px, calc(100vh - 250px), 760px); min-height: 0;",
  ".collection-column-scroll { flex: 1 1 0; min-height: 0; max-height: none; overflow-y: auto;",
  ".collection-middle-column > .formal-panel:first-child > .formal-panel-body:last-child, .collection-execution-column > .formal-panel > .formal-panel-body:last-child { flex: 1 1 0; min-height: 0; max-height: none; overflow-y: auto; }",
], "private review grid layout");
const collectionTaskRowStart = workbenchSource.indexOf("function CollectionTaskRow");
const collectionTaskRowEnd = workbenchSource.indexOf("type WorkspaceProps =", collectionTaskRowStart);
assert(collectionTaskRowStart !== -1 && collectionTaskRowEnd !== -1, "collection task row source is missing");
const collectionTaskRowSource = workbenchSource.slice(collectionTaskRowStart, collectionTaskRowEnd);
assert(
  collectionTaskRowSource.split('windowNames.get(profileId) || "未知窗口"').length - 1 === 1,
  "collection progress repeats the BitBrowser window name",
);
forbidAll(collectionTaskRowSource, ["<span><Monitor size={13} />"], "collection progress summary");
requireAll(viteSource, ['base: "./"'], "production Vite configuration");
forbidAll(String(packageJson.scripts?.build || "") + windowsBuilderSource + workflowSource, [
  "VITE_ENABLE_SIMULATOR=1", "NEXT_PUBLIC_ENABLE_DEMO_DATA=true",
], "release build commands");

// v75 provides real desktop cache management; the old fictitious temporary-byte
// field remains forbidden, while real managed temporary files are now displayed.
requireAll(read("desktop/src/storage-management.ts"), ["clearCache()", "p.busy", "clearManagedFiles", "20*MB", "writePrivateFileAtomically"], "safe desktop storage management");
forbidAll(read("desktop/src/storage-management.ts"), [".clearStorageData("], "cache cleanup must retain login databases");

// Electron is only a hardened loopback bridge. Python Core owns BitBrowser V2.
requireAll(desktopMainSource, [
  "contextIsolation: true", "nodeIntegration: false", "sandbox: true", "collector_core.exe",
  'http://127.0.0.1:${corePort}', "isAllowedCoreRequest", "AbortController", "30_000", '"session-token"',
  'const rendererSecureSettingKeys = new Set(["session-token"])',
  "setWindowOpenHandler", 'on("will-navigate"', "shell.openExternal", "classifyRendererNavigation",
  "normalizedInstagramProfilePreviewUrl", "normalizedInstagramPreviewNavigationUrl",
  "reviewPage.openFromRenderer", "activeSessionOwner", "app.userAgentFallback=accountUserAgent(app.userAgentFallback)", "activeSessionToken", "mainWindow",
  "coreFetchRedirectMode", "assertDirectCoreResponse", "writePrivateFileAtomically",
], "Electron main process");
forbidAll(desktopMainSource, ['persist:instagram-profile-preview', 'instagramPreviewWindow'], "retired guest target preview");
requireAll(read("desktop/src/embedded-browser.ts"), ["profilePreviewUrl", "loadProfilePreview", "body.owner !== p.owner", "body.generation !== p.generation", "session: p.session", "nodeIntegration: false", "sandbox: true", "webSecurity: true", "allowRunningInsecureContent: false", "webviewTag: false", "setPermissionCheckHandler", "setPermissionRequestHandler"], "owned embedded target preview");
requireAll(read("renderer/src/profile-preview.tsx"), ["ProfilePreviewHost", "profile_preview", "AccountBrowserSurface", "locked(plan)", "重新打开目标", "登录 / 检查"], "legacy owned preview controls");
requireAll(read("desktop/src/review-page.ts"), ["ReviewPage", "profilePreviewUrl", "this.openTarget(win,username)", "reviewPreviewBounds", "Promise.race", "this.open(win)", "previous===popup", "登录 / 检查"], "independent review browser");
forbidAll(read("desktop/src/review-page.ts"), ["accountCommand", "host.ensure", "profile_preview"], "independent review must not borrow task windows");
assert(!read("renderer/src/formal-workbench.tsx").includes("<ProfilePreviewHost"), "review still mounts the slow account-selector overlay");
requireAll(atomicSecureStoreSource, ["renameSync", "chmodSync", "temporaryPath", "unlinkSync"], "atomic secure storage");
requireAll(desktopPreloadSource, ["contextBridge.exposeInMainWorld", '"collectorCore"'], "Electron preload");
forbidAll(desktopMainSource, ["/browser/list", "/browser/open", "/browser/close", "localhost:"], "Electron BitBrowser ownership");
requireAll(requestPolicySource, ["/api/", "workbench", "allowedMethods", '"GET"', '"POST"'], "Core request allowlist");
requireAll(requestPolicySource + desktopMainSource, [
  "isAllowedCoreRequest", 'method === "GET"', 'method === "POST"',
  "shouldClearSessionTokenAfterRequest", "clearSessionAfterRequest",
  '"/api/session/register"', '"/api/session/login"', '"/api/session/resume"',
  '"/api/session/logout"', '"/api/workbench/commands"',
], "exact Core route and method allowlist");
forbidAll(requestPolicySource, [
  '"DELETE"', '"PATCH"', '"PUT"', "/api/tasks", "/api/history",
  "/api/results", "/api/split-candidates", "/api/bitbrowser",
], "exact Core route and method allowlist");
requireAll(navigationPolicySource, [
  '"local"', '"external-https"', '"deny"', 'target.protocol !== "https:"',
  "normalizedExternalHttpsUrl",
  "normalizedInstagramProfilePreviewUrl", "normalizedInstagramPreviewNavigationUrl",
  "centeredChildWindowBounds", 'new Set(["instagram.com", "www.instagram.com"])',
  "/^[A-Za-z0-9._]{1,30}$/",
  "target.host === trusted.host",
  'trusted.hostname === "127.0.0.1"',
], "Electron renderer navigation policy");

// One durable workbench model, immutable decisions/exclusions, and every UI
// command must terminate in the Core dispatcher rather than a placeholder.
requireAll(backendMainSource, ['"/api/workbench/snapshot"', '"/api/workbench/commands"', "get_workbench_snapshot"], "NewGen Core routes");
requireAll(backendSchemasSource + backendMainSource, [
  "WorkbenchCommandRequest", '"dedupe_claim"', '"review_decision"',
  '"action_failure_dismiss"', '"action_unknown_resolve"', '"action_target_control"', '"approved_candidate_dismiss"',
  '"storage_cache_clear"',
], "NewGen command schema and dispatcher");
requireAll(backendSchemasSource, [
  "location_enabled: Literal[True] = True", "read_location: Literal[True] = True",
  "per_target_limit: int = Field(default=0, ge=0)",
  "Zero is the UI's persisted \"not configured\" sentinel",
], "mandatory location and unlimited collection schema");
requireAll(workbenchSource + coreClientSource + backendSchemasSource + backendMainSource + backendServiceSource + executionManagerSource, [
  "推荐二档", "([1, 2, 3] as ParallelScreeningWorkers[])", "parallel_screening_workers",
  "ge=1, le=3, strict=True", "min(configured_children, 3)",
  "retained_child_count = len({id(child) for child in held.values()})",
  "for _ in range(max(0, parallel_child_count - retained_child_count))",
], "selectable 1-1 through 1-3 relation topology counts retained pages");
forbidAll(backendSchemasSource, [
  "MAX_COLLECTION_PER_TARGET", "le=100_000", "le=100000",
], "unlimited relationship collection schema");
requireAll(backendMainSource, [
  'elif command == "action_failure_dismiss"',
  'elif command == "action_unknown_resolve"',
  'elif command == "action_target_control"',
  'elif command == "approved_candidate_dismiss"',
  'elif command == "storage_cache_clear"',
  "storage_cache_clear payload must be empty", 'await asyncio.to_thread(service.clear_workbench_cache, user["id"])',
  '"exclude_verified": body.exclude_verified',
  "submit_manual_action", "action_success_history",
  "limit=limit + 1", "detail_limit=limit + 1",
  "limit=history_limit + 1", "detail_limit=history_limit + 1",
  "row_limit=limit + 1", '"split_candidates": split_candidates_have_more',
], "NewGen command branches and bounded snapshot");
requireAll(backendMainSource, [
  "return await finish_owned(execute_owned_command())",
], "owned workbench command completion");
requireAll(backendServiceSource + databaseSource, [
  "workbench_identity_claims", "workbench_candidates", "workbench_review_decisions",
  "workbench_collection_exclusions", "workbench_state_revision", "action_success_ledger",
  "global_seen_stats",
], "NewGen durable data model");
requireAll(backendServiceSource + databaseSource, [
  "collapse_empty_alias_claim", "global_seen_stats", "TECHNICAL_SPOOL_RETENTION_DAYS",
  "action_success_history",
], "dedupe, retention, and ledger-backed history");
requireAll(backendServiceSource + backendMainSource + databaseSource, [
  "_disposable_review_cache_json", "_shed_inline_review_previews",
  "idx_split_history_owner_username", "row_limit: int | None = None",
  "candidate_ids: Iterable[str] | None = None", "_list_split_candidates_by_ids",
  "incoming_usernames", "generation_count",
], "unbounded business storage with bounded disposable previews and snapshot reads");
requireAll(executionManagerSource + playwrightWorkerSource, [
  "get_cached_instagram_user_id", "confirm_workbench_identity",
], "stable-id duplicate gate before exclusion history");
requireAll(executionManagerSource + playwrightWorkerSource + backendCoreTestSource, [
  "handles_profile_read_retries", "profile_attempts", "collection_poll_interval_seconds = 0.45",
  "capture_preview_image: bool = False", "avatar_capture_deferred",
  "_capture_final_review_evidence", "test_relation_collection_polling_allows_virtual_rows_to_render",
  "test_fast_screening_wait_budgets_remain_bounded", "_LOCATION_FLOW_ATTEMPTS = 2",
  "_ACTIVITY_GRID_POLL_ATTEMPTS = 12", "_UNKNOWN_VISIBILITY_WAIT_MILLISECONDS = 1_400",
], "balanced collection throughput policy");
requireAll(executionManagerSource + playwrightWorkerSource + backendCoreTestSource, [
  "capture_visible_review_snapshot", "review_snapshot_captured",
  "test_profile_classifies_visibility_before_reading_account_metrics",
  'screening["routing_result"] = "private_review"', 'settings = {**settings, "local_person_recognition": False, "exclude_male_avatar": False}',
  "public_location_unavailable_retained", "public_zero_posts_activity_requires_review",
  "test_public_unknown_location_is_retained_and_activity_continues",
  "test_public_zero_posts_without_activity_enters_manual_review",
], "visibility-first screening and final review snapshot policy");
requireAll(executionManagerSource + backendServiceSource + backendCoreTestSource + backendWorkbenchTestSource, [
  "activity_no_posts", "zero_post_activity_review",
  "test_only_confirmed_zero_post_activity_unknown_can_enter_public_review",
  "account_count_ceiling_exceeded", "public_activity_ceiling_exceeded",
  "excessive_counts", "known_discard_days",
], "manual-review exceptions and detailed exclusion reasons");
requireAll(executionManagerSource + backendCoreTestSource, [
  "public_zero_posts_excluded", "excluded_zero_posts",
  "test_private_zero_posts_are_excluded_when_zero_switch_is_enabled",
], "zero-post switch excludes public and private profiles");
requireAll(workbenchSource, ["local_person_recognition: false", "exclude_male_avatar: false"],
  "retired gender inference switches are disabled");
forbidAll(workbenchSource, ["排除男性≥55%", "checked={localPersonRecognition}", "checked={excludeMaleAvatar}"],
  "retired gender filtering has no collection control");
forbidAll(executionManagerSource, [
  "include_post_previews=True",
], "public review avatar-only capture policy");
requireAll(executionManagerSource, [
  'if mode in {"followers", "following"}:',
  "_candidate_spool_limit", "return None", "candidate_spool_natural_end",
  "require_natural_end=True",
], "unlimited relationship collection execution");
forbidAll(executionManagerSource, [
  "2_147_483_647",
], "unlimited relationship collection must not use a finite sentinel");
requireAll(actionManagerSource, [
  "async def submit_manual_action(", '"failed"', 'status="unknown"',
  "_FOLLOW_FAILURE_PAUSE_THRESHOLD = 3", "_reset_follow_failure_streak",
  "_GREET_RECIPIENT_FAILURE_PAUSE_THRESHOLD = 3", "_greet_recipient_failure_streak",
  "instagram_direct_inbox_recipient_not_found", "同一窗口连续 {streak} 个账号未出现精确搜索结果",
  "打招呼窗口连接失败，尚未开始任何账号", "已保留全部待执行账号",
  "_wait_for_next_action_target", "_bring_profile_to_front",
  "_GREET_STRUCTURAL_PAUSE_REASONS", "if exc.code in _GREET_STRUCTURAL_PAUSE_REASONS",
  "Direct 页面结构异常", "choose_greeting_message", "_greeting_attempt_details",
  "cleanup_cancellation: asyncio.CancelledError | None = None",
  "_set_campaign_status_preserving_stop", "_release_finished_action_control",
  "_fence_cancelled_campaign", "_shield_cleanup", "owner_task.uncancel()",
  "self._manual_controls: dict[str, ActionControl] = {}",
  'self._manual_controls[campaign["id"]] = manual_control',
  "_release_manual_control", 'action in {"pause", "cancel"}',
  'operation: str = ""', "self._closing = False",
  "def _ensure_accepting_actions(", "self._closing = True",
  "Manual action task was cancelled; verify any UNKNOWN ",
  "Action coordinator was cancelled; verify any UNKNOWN ",
  "Application shutdown interrupted the action; verify UNKNOWN outcomes manually",
], "durable asynchronous manual action and failure fencing");
const actionPauseActiveStart = actionManagerSource.indexOf("    async def pause_active(");
const actionPauseStart = actionManagerSource.indexOf("    async def pause(", actionPauseActiveStart);
const actionStopStart = actionManagerSource.indexOf("    async def stop(");
const actionResumeStart = actionManagerSource.indexOf("    async def resume(", actionStopStart);
assert(
  actionPauseActiveStart !== -1 && actionPauseStart > actionPauseActiveStart && actionStopStart > actionPauseStart,
  "action pause source boundaries are missing",
);
const actionPauseActiveSource = actionManagerSource.slice(actionPauseActiveStart, actionPauseStart);
const actionPauseSource = actionManagerSource.slice(actionPauseStart, actionStopStart);
assert(
  actionPauseActiveSource.indexOf("self._manual_controls.values()") < actionPauseActiveSource.indexOf("self.service.find_active_campaign("),
  "pause-active must arm local manual controls before its first durable read",
);
assert(
  actionPauseSource.indexOf("self._manual_controls.get(") < actionPauseSource.indexOf("self.service.get_action_campaign("),
  "pause must arm its local manual control before its first durable read",
);
assert(actionStopStart !== -1 && actionResumeStart > actionStopStart, "action stop source boundary is missing");
const actionStopSource = actionManagerSource.slice(actionStopStart, actionResumeStart);
requireAll(actionStopSource, [
  "self._manual_controls.get(",
  'campaign["status"] in {"completed", "failed", "stopped"}',
  "return self.service.interrupt_action_campaign(",
  "Action was stopped; any unfinished external outcome ",
  'owner_user_id, campaign_id, "stopped"',
  "Action was stopped; any in-flight external outcome ",
  "finally:", "control.task.cancel()",
], "stop terminal interrupt and local cancellation fence");
assert(
  actionStopSource.indexOf("self._manual_controls.get(") < actionStopSource.indexOf("self.service.get_action_campaign("),
  "stop must arm its local control before its first durable read",
);
requireAll(playwrightWorkerSource, [
  "instagram_action_outcome_unknown", "async def bring_window_to_front(",
  "normalize_direct_message_text", "direct_recipient_text_matches", "direct_inbox_result_text_matches",
  'line.strip().casefold() == f"@{target}"', "return lines[1].strip().lstrip(\"@\").casefold() == target",
  "_direct_inbox_result_candidates", 'main input[placeholder*="search" i]',
  "_first_direct_inbox_recipient_row", "_direct_inbox_result_click_target",
  "_open_direct_thread_from_inbox_search", "_direct_thread_matches_recipient",
  "header_center_limit", "belongs_to_header_band", "return len(clusters) == 1",
  "_visible_outside_dialog", "_visible_direct_composer", "_current_direct_composer",
  "_stable_direct_composer_with_text", "_write_greeting_to_composer",
  "keyboard.insert_text(message)", "_visible_transcript_message_count", "_direct_send_control(active)",
  "username_norm=username_norm", "triggered = False", "current_count > before_count",
  'reason="instagram_direct_recipient_mismatch"',
  'reason="instagram_direct_composer_not_ready"',
  'reason="instagram_action_outcome_unknown"',
  "first_result_seen", "click_target.click",
  "_profile_posts_count_cache", "posts_count_matches",
  "_visible_profile_stats_text",
  "except asyncio.CancelledError as exc:",
  'setattr(exc, "instagram_action_outcome_unknown", True)',
  '"action_outcome_message"',
  "消息发送动作被中断，可能已经执行；结果待人工确认且不会自动重试",
], "browser action and fail-closed Direct greeting flow");
forbidAll(playwrightWorkerSource, [
  "/direct/new/", "chat_labels", 'await composer.fill("")',
  "navigator.clipboard", "document.execCommand('paste')", 'keyboard.press("Control+V")',
  'keyboard.press("Meta+V")', "self.page.locator(self.selectors.message_composer).last",
], "Direct inbox greeting flow");
requireAll(directGreetingTestSource, [
  "test_synthetic_bare_username_line_matches_without_href",
  "test_display_name_collision_does_not_match_a_different_handle",
  "test_missing_search_geometry_never_selects_a_main_control",
  "test_first_mismatch_never_clicks_a_later_exact_result",
  "test_exact_first_result_clicks_outer_row_not_profile_link",
  "test_visible_composer_is_verified_even_when_url_does_not_change",
  "test_greet_runs_first_result_through_exact_fill_enter_and_bubble",
], "Direct first-result regression suite");
requireAll(directGreetingInboxTestSource, [
  "test_recipient_match_requires_profile_href_or_explicit_handle",
  "test_greeting_uses_only_the_inbox_search_flow",
  "test_plain_div_button_header_rejects_bare_display_name_collision",
  "test_plain_div_button_header_accepts_explicit_at_handle",
  "test_left_column_body_text_and_display_name_collision_are_rejected",
  "test_thread_verifier_rejects_left_result_and_accepts_right_header",
  "test_recipient_mismatch_never_reaches_send",
], "Direct recipient regression suite");
requireAll(directGreetingSendTestSource, [
  "test_visible_composer_is_selected_even_when_it_is_not_last",
  "test_selector_contract_covers_textarea_plaintext_and_lexical_editors",
  "test_real_page_never_falls_back_to_an_unsafe_old_composer",
  "test_no_role_contenteditable_is_selected_by_real_selector",
  "test_slate_is_selected_while_readonly_disabled_and_dialog_nodes_are_excluded",
  "test_send_control_is_enabled_visible_and_near_the_right_composer",
  "test_real_send_control_selection_clicks_only_safe_adjacent_button",
  "test_only_transport_artifacts_are_normalized",
  "test_textarea_plaintext_and_lexical_fill_exactly_then_enter",
  "test_existing_draft_is_replaced_by_the_verified_greeting",
  "test_visually_empty_editor_artifacts_are_not_mistaken_for_a_draft",
  "test_placeholder_descendant_is_removed_before_draft_detection",
  "test_fill_noop_uses_insert_text_without_clipboard",
  "test_fill_followed_by_unknown_editor_read_never_inserts_or_sends",
  "test_fill_noop_with_disappearing_editor_never_uses_keyboard_fallback",
  "test_partial_or_truncated_fill_never_uses_fallback_or_sends",
  "test_collapsed_multiline_or_double_space_text_is_not_sent",
  "test_recipient_switch_after_fill_is_caught_before_send",
  "test_switch_to_other_thread_with_same_text_draft_preserves_it",
  "test_visible_send_button_path_requires_new_right_bubble_and_empty_input",
  "test_preexisting_identical_bubble_does_not_confirm_without_a_new_one",
  "test_empty_without_bubble_and_bubble_without_empty_are_unknown",
  "test_composer_dom_replacement_after_enter_still_confirms",
  "test_post_trigger_exception_is_unknown_and_pauses",
  "test_post_trigger_cancellation_is_marked_unknown_and_propagated",
  "test_pre_trigger_transcript_read_failure_does_not_send",
  "test_post_trigger_worker_error_is_always_unknown",
  "test_unknown_send_pauses_campaign_and_never_enters_success_history",
  "test_definite_pre_send_failure_pauses_then_resume_continues_next_target",
  "test_two_greeting_windows_are_isolated_and_history_keeps_exact_message",
], "Direct write, send, confirmation, and campaign regression suite");
requireAll(directGreetingQueueSafetyTestSource, [
  "test_three_consecutive_missing_recipients_pause_before_fourth",
  "test_success_resets_missing_recipient_streak",
  "test_different_failure_resets_missing_recipient_streak",
  "test_missing_recipient_streak_is_isolated_per_window",
  "test_initial_connect_failure_pauses_only_that_window_and_keeps_pending",
  "test_campaign_task_cancellation_fences_running_greeting_as_unknown",
  "test_same_tick_shutdown_rejects_unscheduled_manual_action",
  "test_same_tick_shutdown_rejects_unscheduled_campaign_start",
  "test_manual_action_external_cancellation_fences_unknown",
  "test_manual_connect_domain_error_is_recoverable_without_residue",
  "test_manual_worker_factory_failure_releases_all_admission_state",
  "test_manual_finish_write_failure_fences_unknown_and_resume_refuses",
  "test_shutdown_cancels_manual_pre_send_and_waits_for_cleanup",
  "test_stop_cancels_manual_pre_send_and_never_sends",
  "test_stop_read_failure_still_cancels_manual_pre_send",
  "test_pause_read_failure_still_cancels_manual_pre_send",
  "test_pause_active_cancels_manual_pre_send_and_never_sends",
  "test_pause_active_read_failure_still_cancels_manual_pre_send",
  "test_target_cancel_cancels_manual_pre_send_and_never_sends",
  "test_repeated_stop_repairs_stopped_running_attempt_and_claim",
  "test_stopped_running_greeting_is_fenced_unknown_if_then_cancelled",
  "test_shutdown_fences_once_then_preserves_task_cancellation",
  "test_immediate_shutdown_releases_never_scheduled_campaign",
  "test_shutdown_fence_error_still_cancels_and_sweeps_every_control",
  "test_stop_during_pre_send_wait_never_reaches_send_boundary",
  "test_stop_status_write_error_still_prevents_send",
  "test_stop_post_trigger_unknown_does_not_reopen_campaign",
  "test_heartbeat_fence_error_still_cancels_campaign_owner",
  "test_persistent_heartbeat_storage_failure_stops_browser_and_restart_fences_unknown",
  "test_manual_heartbeat_fence_error_still_cancels_parent",
  "test_repeated_campaign_cancellation_waits_for_owned_cleanup",
  "test_repeated_manual_cancellation_waits_for_owned_cleanup",
  "test_manual_non_recipient_failure_resets_window_streak",
], "Direct per-window queue safety regression suite");
const greetingRegressionCount = [
  directGreetingTestSource,
  directGreetingInboxTestSource,
  directGreetingSendTestSource,
  directGreetingQueueSafetyTestSource,
].reduce(
  (count, source) => count + (source.match(/^\s*(?:async\s+)?def\s+test_/gm) || []).length,
  0,
);
assert(greetingRegressionCount === 79, `expected 79 Direct greeting regressions, found ${greetingRegressionCount}`);
requireAll(backendCoreTestSource, [
  "test_unlimited_relation_collection_ignores_stale_saved_limit",
  "test_new_and_recovered_relation_tasks_expose_no_collection_ceiling",
  "test_unlimited_relationship_list_stops_only_at_confirmed_natural_end",
  "test_relation_dispatch_passes_no_limit_for_new_and_legacy_settings",
  "test_desktop_api_ignores_relation_collection_limits_and_runs_unlimited",
  "test_greeting_message_library_is_normalized_persisted_and_legacy_compatible",
  "test_greeting_direct_structure_failure_pauses_before_burning_queue",
  "test_persistent_action_campaign_and_unknown_no_retry",
  "test_greeting_campaign_chooses_and_audits_one_stable_message_per_target",
  "test_prefenced_unknown_finish_is_idempotent_and_keeps_first_reason",
  "test_interrupt_converges_running_campaign_with_prefenced_unknown_row",
], "Core collection and greeting regression suite");
requireAll(backendWorkbenchTestSource, [
  "test_preview_size_boundaries_degrade_cache_without_losing_business_records",
  "test_snapshot_bounds_completed_split_history_and_keeps_live_rows",
  "test_split_mutations_do_not_scan_or_return_unrelated_history",
  "test_private_review_tier_counts_are_complete_beyond_snapshot_row_limit",
  'snapshot["counts"]["pending_private_primary"]',
  'snapshot["counts"]["pending_private_secondary"]',
], "storage and split-history scale regressions");
requireAll(backendServiceSource, [
  '"pending_private_primary": pending_private_primary',
  '"pending_private_secondary": pending_private_secondary',
], "authoritative private review tier totals");
requireAll(backendServiceSource, [
  "def interrupt_action_campaign(", "has_existing_unknown",
  "effective_final_status", 'campaign["status"] == "stopped"',
  "released_dispatch_claims", '"campaign.interrupted"',
], "durable cancellation convergence and explicit-stop preservation");
requireAll(rendererWorkbenchTestSource, [
  "greeting message parser rejects an empty library",
  "greeting message parser uses Core-compatible Unicode length",
  "automatic and manual greeting UI share fail-closed validation",
  "unified discard limits can all be disabled without hidden qualification controls",
  "SQLite 业务记录不设应用额度",
  "history summary uses complete Core totals and labels the bounded visible rows",
  "review and action headline counts use complete Core totals instead of bounded arrays",
  "successful greeting history resolves the exact attempt message with safe legacy fallbacks",
  "greeting history renders actual messages with validated selection payloads",
  "snapshot failure UI is fail-closed while accurately describing automatic recovery",
  "public review is an avatar-first pending list without post photos",
  "collection and action workspaces use clean aligned columns with internal scrolling",
  "collection exclusion history translates precise rejection reasons",
], "renderer workbench regression suite");
requireAll(rendererCoreClientTestSource, [
  "a Core that remains behind the applied revision must fail after bounded retries",
  "a legitimately late response must be discarded and replaced by a fresh snapshot",
  "a pre-command response arriving after snapshot_seq must be retried behind the command fence",
  "a command-time refresh must not reuse a snapshot accepted before its revision fence",
  "a pre-command revision must never reach onSnapshot after the command completes",
  "a command completing in the final Promise gap must fence the already accepted snapshot",
  "delivery-time fencing must perform one bounded fresh Core read",
  "polling must automatically recover when the first post-command response is stale",
], "renderer stale snapshot regression suite");
requireAll(backendServiceSource, [
  "def dismiss_action_failure(", "def control_action_target(",
  "def resolve_unknown_action(", "def dismiss_approved_candidate(", "INSERT INTO action_dispatch_claims(",
  "INSERT OR IGNORE INTO action_success_ledger(",
  '"greet_successes": greet_successes', '"follow_successes": follow_successes',
  '"source": "queued_upsert"', "normalized_messages", "seen_messages",
  "Greeting messages must be text", "Greeting message may not exceed 200 characters",
  "At least one greeting message is required", "messages_json",
  '"message": greeting_message',
], "NewGen action state machine");
requireAll(backendServiceSource, [
  "def clear_workbench_cache(", "status!='pending'", '"temporary_bytes": 0',
  '"pending_preview_bytes": pending_preview_bytes', '"business_records_retained": True',
  '"last_maintenance_at": revision_row["last_cleanup_at"]',
  '"global_dedupe": True', '"review_history": True',
  '"collection_exclusions": True', '"action_success_history": True',
  '"live_task_checkpoints": True', '"pending_review_previews": True',
], "safe storage cleanup retention contract");
const manualCleanupStart = backendServiceSource.indexOf("def clear_workbench_cache(");
const snapshotStart = backendServiceSource.indexOf("def get_workbench_snapshot(", manualCleanupStart);
assert(manualCleanupStart !== -1 && snapshotStart > manualCleanupStart, "manual cleanup source boundary is missing");
forbidAll(
  backendServiceSource.slice(manualCleanupStart, snapshotStart),
  ["SET last_cleanup_at"],
  "owner-scoped manual cleanup maintenance watermark",
);
requireAll(backendMainSource + backendServiceSource, [
  "count_action_campaigns_by_operation", '"greet_campaigns"', '"follow_campaigns"',
  '"greet_action_success_history"', '"follow_action_success_history"',
], "operation-specific capacity flags");
requireAll(databaseSource, [
  "workbench review decisions are immutable", "workbench collection exclusions are immutable",
  "BEFORE DELETE ON workbench_review_decisions", "BEFORE DELETE ON workbench_collection_exclusions",
], "immutable NewGen history");
requireAll(bitBrowserApiSource, [".bitbrowser_v2", "BitBrowserClient"], "BitBrowser V2 entry point");
requireAll(bitBrowserV2Source, [
  '"/browser/pids/all"', "def bring_profile_to_front(", "SetForegroundWindow", "ShowWindowAsync",
], "BitBrowser native foreground handoff");
forbidAll(bitBrowserApiSource + backendMainSource, ["from .bitbrowser import", "import app.bitbrowser"], "formal BitBrowser import chain");
requireAll(backendSmokeSource, [
  '"account_window_plans"', '"account_window_events"', '"event_log_usage"', '"studio_jobs"', '"studio_assets"', '"studio_templates"', '"studio_daily_actions"', '"action_success_ledger"',
  '"action_dispatch_claims"', '"workbench_candidate_dismissals"',
  '"split_candidate_window_affinity"', '"allowed_window_ids_json"',
], "backend packaging smoke gate");
requireAll(backendSmokeSource, [
  "set(range(1, 42))", "_verify_sqlite_schema(connection)",
  '"trg_event_log_usage_insert"', '"trg_event_log_usage_update"', '"trg_event_log_usage_delete"',
  "_verify_event_log_projection(connection)",
  '"split_admission_totals"', '"successful_adds"', '"history_complete"', '"has_executed"',
  '"split_completed_targets"', '"source_window_id"', '"completed_at"',
  '"review_stage"', '"review_transferred_at"',
  '"trg_split_admission_executed_insert"', '"trg_split_admission_executed_update"',
  '"trg_split_completed_target_insert"', '"trg_split_completed_target_update"',
  "_verify_split_registry(connection)",
], "current backend schema, event projection and split registry smoke gate");
requireAll(windowsBuilderSource, [
  "test_backend_smoke_schema_r40.py", "backend-smoke-schema-r40-full.log", "if ($SchemaSmokeExitCode -ne 0)",
], "current SQLite release schema regression gate");

// Both distributions must carry the standalone browser, and the Windows build
// must exercise a real browser before publishing installation artifacts.
const runtimeResource = packageJson.build.extraResources.find((entry) => entry.to === "browsers");
if (!runtimeResource || runtimeResource.from !== "build/browsers") throw new Error("Missing bundled Chromium resource");
requireAll(windowsBuilderSource, ["playwright install chromium", "scripts\\verify_native_browser.py", '-p "test_native_cloud.py" -v'], "standalone browser and cloud gates");
requireAll(windowsBuilderSource, ['-p "test_native_launch.py" -v', '"--no-headless"', '"native-browser-full.log"'], "production native launch and retained failure diagnostics");
requireAll(windowsBuilderSource, ['"scripts\\repair_native_browser.py"', '$BundledBrowserExitCode -ne 0', '"native-bundled-full.log"', 'test_native_repair_r94.py'], "mandatory packaged browser verification");
requireAll(read('scripts/repair_native_browser.py'), [
  "'--require-bundled'", "'--no-headless'", "result.get('status') != 'passed'",
  "result.get('require_bundled') is not True", "len(versions) != 2",
  "result.get('network_verified') is not True", "preparer(root)",
], "repair must retain real pinned-browser verification");
requireAll(read('scripts/verify_native_browser.py'), [
  'NETWORK_CHECK_TIMEOUT_SECONDS = 15.0', 'VERIFICATION_TIMEOUT_SECONDS = 180.0',
  'timeout=NETWORK_CHECK_TIMEOUT_SECONDS', 'timeout=VERIFICATION_TIMEOUT_SECONDS',
  'asyncio.run(verify_with_timeout(',
], 'native verifier must bound unresponsive page and CDP requests');
requireAll(workflowSource, ['ci/public_ci.py early', 'ci/public_ci.py build', 'ci/public_ci.py installed'], "CI native, full-build and installed acceptance stages");
requireAll(read('ci/public_ci.py'), ["powershell('scripts/build_windows.ps1')", "['-BrowserMode', 'installed-chrome']"], 'CI invokes the full installed-Chrome release entry');
requireAll(windowsBuilderSource, ['"--require-installed-chrome"', '$NativeBrowserExitCode -ne 0', 'scripts\\verify_openvino_windows.py', 'test_frozen_openvino.ps1'], 'CI retains real browser, source and frozen native inference gates');
requireAll(backendSmokeSource, ['"native_browser_profiles"', '"cloud_workspace_links"'], "native/cloud migrations");

// Every Windows release path carries the independent NewGen identity.
const expectedSetup = "Juxin-IG-Audience-Collector-NewGen-Setup-$Version-x64.exe";
requireAll(windowsBuilderSource, [
  expectedSetup, "Juxin-IGAC-NewGen-Portable-v$Version-x64", "LATEST_SUCCESS.txt",
  "pyinstaller-full.log", "frozen-openvino-smoke.log", "electron-builder-full.log",
  '-p "test_account_workspace.py" -v',
  '-p "test_studio.py" -v',
  '$GreetingRegressionExitCode = Invoke-IgacNativeCommandWithLog',
  '"scripts\\run_backend_tests.py", "-p", "test_greeting*.py", "-v"',
  '-LogPath $GreetingRegressionLog',
  'if ($GreetingRegressionExitCode -ne 0)',
  'Run complete Direct greeting regression suite failed',
  'greeting-regression-full.log',
  'test_dedup*.py', 'dedup-regression-full.log',
  'if ($DedupRegressionExitCode -ne 0)',
  '$DedupDiagnosticExitCode = Invoke-IgacNativeCommandWithLog',
  'test_diagnose_dedupe.py', 'dedup-diagnostic-full.log',
  '-LogPath $DedupDiagnosticLog',
  'if ($DedupDiagnosticExitCode -ne 0)',
  '$StabilityRegressionExitCode = Invoke-IgacNativeCommandWithLog',
  'test_*r24.py', 'stability-regression-full.log',
  '-LogPath $StabilityRegressionLog',
  'if ($StabilityRegressionExitCode -ne 0)',
  '$RecoveryRegressionExitCode = Invoke-IgacNativeCommandWithLog',
  'test_*r25.py', 'recovery-regression-full.log',
  '-LogPath $RecoveryRegressionLog',
  'if ($RecoveryRegressionExitCode -ne 0)',
  '$CurrentVersionSetupPattern = "Juxin-IG-Audience-Collector-NewGen-Setup-$Version-*.exe"',
  "$CurrentVersionSetups[0].FullName -ne ([IO.Path]::GetFullPath($InstallerPath))",
  "Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath",
  '"SHA256=$TrustedOutputHash"', '"SHA256_PATH=$TrustedOutputHashPath"',
  "CurrentVersionSetups.Count -ne 1", '$InstallerHashPath = "$InstallerPath.sha256"',
  '$TrustedHashRecord = "$TrustedOutputHash *$([IO.Path]::GetFileName($InstallerPath))"',
  "Move-Item -LiteralPath $InstallerHashTemporary -Destination $InstallerHashPath -Force",
  "$RecordedHash -ne $TrustedHashRecord",
], "Windows one-click builder");
requireAll(portableBuilderSource, ["Juxin-IGAC-NewGen-Portable-v$Version-x64", "Juxin IG Audience Collector NewGen.exe"], "Windows portable builder");
requireAll(builderBatchSource + startHereSource, ["NewGen", "build_installer_windows.bat"], "Windows one-click entry");
requireAll(startHereSource, [
  'set "EXPECTED_SETUP=Juxin-IG-Audience-Collector-NewGen-Setup-%APP_VERSION%-x64.exe"',
  'set "EXPECTED_HASH_PATH=%EXPECTED_SETUP_PATH%.sha256"',
  'if /i not "%RECORDED_SETUP_PATH%"=="%EXPECTED_SETUP_PATH%"',
  'if /i not "%RECORDED_HASH_PATH%"=="%EXPECTED_HASH_PATH%"',
  "Get-FileHash -Algorithm SHA256 -LiteralPath $env:VERIFY_SETUP_PATH",
  "The Setup SHA-256 does not match LATEST_SUCCESS.txt and its sidecar",
], "Windows one-click checksum verification");
const publicCiEntry = read('ci/public_ci.py');
const publicCiControls = read('ci/public_ci_common.py');
const publicInstalledGate = read('ci/public_ci_verify_installed.ps1');
const publicInstalledOracle = read('ci/public_ci_validate_installed.py');
requireAll(workflowSource, [
  'contents: read', 'runs-on: windows-2022', 'persist-credentials: false',
  'ref: ${{ github.sha }}', 'github.repository_id == \'1406784621\'',
  'github.repository_owner_id == \'337452708\'', 'github.event.repository.private == false',
  'ci/public_ci.py initialize', 'ci/public_ci.py contracts',
  'ci/public_ci.py early', 'ci/public_ci.py build', 'ci/public_ci.py installed', 'ci/public_ci.py export',
  "if: always() && steps.export_proof.outcome == 'success'", 'if-no-files-found: error',
], 'public Windows source, full build and installed workflow');
requireInOrder(workflowSource, [
  'ci/public_ci.py initialize', 'ci/public_ci.py contracts', 'ci/public_ci.py early',
  'Check out same exact commit into a fresh build tree', 'ci/public_ci.py build',
  'ci/public_ci.py installed', 'ci/public_ci.py export', 'Export only the three reviewed JSON summaries',
], 'fresh same-commit early, full build, installed acceptance and bounded export order');
for (const action of workflowSource.matchAll(/uses:\s+([^\s#]+)/g)) {
  assert(/^actions\/(checkout|setup-python|setup-node|upload-artifact)@[a-f0-9]{40}$/.test(action[1]), 'public CI actions must be official immutable commit pins');
}
requireAll(publicCiEntry, [
  "powershell('scripts/build_windows.ps1')", "['-BrowserMode', 'installed-chrome']",
  "'test_cloud_configuration_public.py'", "'test_python_environment.py'", "'test_timezone_data_r57.py'",
  "powershell('ci/public_ci_verify_installed.ps1')", 'validate_source_build(state)',
  "require(not (ROOT / 'installer-output').exists()", "ci/public_ci_validate_installed.py",
  'installed_app_asar_sha256', 'installed_core_sha256', 'installed_desktop_sha256',
], 'full original gates, exact binary identity and separate installed acceptance');
requireAll(publicCiControls, [
  'verify_ci_source_binding(ROOT)', "'public-sanitized-source'", 'source_provenance',
  "path.write_bytes(b'0')", "read_bytes() == b'0'", 'SHGetFolderPathW',
  'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe',
  "list(output.glob('Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-*.exe')) == [installer]",
  "marker.get('TYPE') == 'INSTALLER'", "marker.get('SHA256') == digest(installer)",
  "marker.get('SHA256_PATH', '')", "digest(installer) + ' *' + installer.name",
  'sidecar.stat().st_mtime_ns >= started', 'installer.stat().st_mtime_ns >= started',
  'runner.supervise(request)', "receipt['targetExitCode'] == 0", 'runner.terminal_receipt(receipt)',
  "PIP_INDEX_URL='https://pypi.org/simple'", "NPM_CONFIG_REGISTRY='https://registry.npmjs.org/'",
], 'public exact source, fresh single NSIS hash and checksum verification');
requireAll(publicInstalledGate, [
  '--pure-ig', '--snapshot-scale', '--collection-completion', '--standalone-nurture',
  '--nurture-cleanup-upgrade', '441552', '602831', 'posting_removed', '$removed.http_status -ne 404',
  'verify_installed_recovery_r64.py', '-TimeoutSeconds 180', "'--timeout', '420'",
  '-LogPath $recoveryStdout -TimeoutSeconds 540',
], 'actual installed source-independent Core, migration, scale and recovery gates');
requireAll(publicInstalledOracle, [
  'r63_upgrade_proof.py', 'r63_native_proof.py', 'r64_recovery_ui_proof.py',
  'verify_installed_recovery_r64.py', 'bind_native(bound,identity)',
  "same_json(installed.get('recovery_api'),recovery_api)", 'core_upgrade_manifest_sha256',
], 'all independent legacy and current installed proof validators');
for (const filename of ['run-summary.json', 'source-build-proof.json', 'installed-acceptance-proof.json']) {
  requireAll(workflowSource, ['${{ runner.temp }}/juxin-public-ci-${{ github.run_id }}-${{ github.run_attempt }}/public/' + filename], 'explicit bounded synthetic artifact allowlist');
  requireAll(publicCiEntry, [filename], 'freshly constructed bounded public summary');
}
forbidAll(workflowSource, [
  'installer-output/', 'delivery/', '*.exe', '*.zip', '*.log', '*.png', 'contents: write',
  'continue-on-error', 'secrets.', 'github.token', 'ExecutionPolicy', 'pull_request_target',
], 'public verification exports bounded JSON only and cannot weaken required gates');
forbidAll(windowsBuilderSource + portableBuilderSource + builderBatchSource + startHereSource + workflowSource, [
  "worktree-v0.2.51", "Juxin-IG-Audience-Collector-Setup-",
], "NewGen release packaging");

// Unresolved release records also fail source preflight. A complete desktop
// build additionally requires every compiled-artifact check below.
const unresolvedReleasePlaceholders = [
  ...new Set(releaseStatusSource.match(/\bFINAL_[A-Z0-9_]+\b/g) ?? []),
];
assert(
  unresolvedReleasePlaceholders.length === 0,
  `NewGen release status contains unresolved FINAL_* verification placeholders: ${unresolvedReleasePlaceholders.join(", ")}`,
);

// Source checks run before any compiled artifact is opened or imported.
for (const file of ['desktop/vendor/immersive-translate/host.html',
 'desktop/vendor/immersive-translate/NOTICE.txt',
 'desktop/src/immersive-source.ts', 'desktop/tests/support/immersive-synthetic-source.cjs',
 'desktop/tests/immersive-runtime.integration.cjs', 'desktop/tests/immersive-host.test.mjs']) requireFile(file);
assert(!existsSync(at('desktop/vendor/immersive-translate/immersive-translate.user.js')), 'optional proprietary translator payload must not ship in public source');
for (const file of ['desktop/vendor/immersive-translate/host.html', 'desktop/vendor/immersive-translate/NOTICE.txt']) {
  assert(packageJson.build.files.includes(file), 'first-party translator resource missing from installer: ' + file);
}
assert(!packageJson.build.files.includes('desktop/vendor/immersive-translate/**/*'), 'installer must not bundle an optional proprietary translator payload');
requireAll(read('desktop/tests/embedded-browser.integration.cjs'), ["watchdog.begin('immersive-runtime'", "require('./immersive-runtime.integration.cjs')"], 'optional translation synthetic native runtime release gate');
requireAll(read('desktop/tests/immersive-runtime.integration.cjs'), ['missing.status(', 'missing.settings(', 'missing.translate(', 'createdWindows,0', 'syntheticSource', 'vendor execution NOT RUN'], 'optional translation absence and honest synthetic native coverage');

requireAll(read('desktop/tests/immersive-runtime.integration.cjs'), ['panel.visible,true', 'panel.controls>=2', 'firstId', '.popup-container'], 'visible settings and warm renderer release checks');
requireAll(read('desktop/tests/immersive-runtime.integration.cjs'), ['translator.acquireWorker', 'coldStarts,2', 'translator.diagnostics()'], 'cold worker recovery and actionable startup diagnostics');
requireAll(read('desktop/tests/immersive-runtime.integration.cjs'), ["GM.setValue('localConfig'", "GM.setValue('fullLocalUserConfig'", 'managedHostProbes', 'translator.cacheIdentity(id),identity'], 'real settings IPC and background probe stability release checks');
requireAll(read('desktop/src/main.ts'), ['immersiveOnly:true'], 'removed builtin engine stays disabled in production');
forbidAll(read('renderer/src/chat-translation.tsx'), ['aria-label="同步翻译"', 'aria-label="翻译设置"'], 'retired translation controls');
requireAll(read('desktop/tests/embedded-browser.integration.cjs'), ["watchdog.begin('whatsapp-layout'", "watchdog.begin('instagram-chat'"], 'WhatsApp drag and Instagram native-chat release checks');
requireAll(read('desktop/tests/instagram-layout.integration.cjs'), ['actual.dividers,0', 'actual.contacts,340', 'actual.selection', 'contextMessage'], 'Instagram has no divider and retains chat selection/copy');
requireAll(read('desktop/tests/whatsapp-layout.integration.cjs'), ['sendInputEvent', 'await drag(-160)', 'await drag(130)'], 'WhatsApp real native pointer drag remains required');
requireAll(read('desktop/tests/whatsapp-layout.integration.cjs'), ['old native border no longer remains', 'contacts and conversation remain adjacent'], 'native boundary and full-column continuity checks');
for(const source of ['desktop/src/whatsapp-page.ts','desktop/src/embedded-browser.ts'])requireAll(read(source), ['code:whatsappColumnLayoutScript()'], 'same divider and native boundary adapter in '+source);
requireAll(packageJson.scripts['test:desktop'], ['desktop/tests/whatsapp-boundary.test.mjs'], 'native boundary decoration regression checks');
requireAll(packageJson.scripts['test:desktop'], ['desktop/tests/whatsapp-columns.test.mjs'], 'archive and welcome structural-column checks');
requireAll(read('desktop/tests/whatsapp-layout.integration.cjs'), ['archive welcome keeps the saved ratio without side or main', 'archive welcome divider remains draggable', 'returning from archive to normal list preserves the archive width', 'late native boundary is cleared after navigation'], 'archive welcome and late paint release checks');

// r32 lifecycle regressions are part of the real release, not an optional
// source-only path. Keep the Windows runner and complete source package aligned.
requireAll(packageJson.scripts['test:desktop'], [
  'desktop/tests/whatsapp-runtime-r32.test.mjs', 'desktop/tests/whatsapp-messaging-r32.test.mjs',
  'desktop/tests/whatsapp-quote-r32.test.mjs', 'desktop/tests/window-host-r32.test.mjs',
  'desktop/tests/cdp-lifecycle-r32.test.mjs',
], 'WhatsApp and window lifecycle regression gates');
requireAll(packageJson.scripts['test:renderer'], [
  'renderer/tests/window-workspace-r32.test.mjs', 'renderer/tests/chat-translation-r32.test.mjs',
], 'window and translation renderer regression gates');
requireAll(windowsBuilderSource, ['test_windows_r32.py', 'window-lifecycle-regression-full.log'], 'window backend lifecycle release gate');

requireAll(packageJson.scripts['test:desktop'], [
  'desktop/tests/window-performance-r33.test.mjs', 'desktop/tests/surface-switch-r33.test.mjs',
], 'window sizing, focus and authorization regression gates');
requireAll(packageJson.scripts['test:renderer'], [
  'renderer/tests/window-performance-r33.test.mjs', 'renderer/tests/browser-surface-r33.test.mjs',
], 'window indexing and hidden surface regression gates');
requireAll(windowsBuilderSource, ['test_window_performance_r33.py', 'window-performance-regression-full.log'], 'bounded window snapshot release gate');
requireAll(windowsBuilderSource, ['test_screen_persistence_r33.py', 'screen-persistence-regression-full.log'], 'screen persistence responsiveness and cancellation release gate');

// r34 chat regressions remain mandatory in the actual desktop and backend build.
requireAll(packageJson.scripts['test:desktop'], [
  'desktop/tests/chat-reminders-r34.test.mjs', 'desktop/tests/chat-translation-r34.test.mjs',
  'desktop/tests/whatsapp-navigation-r34.test.mjs', 'desktop/tests/floating-chat-session-r34.test.mjs',
], 'chat reminders, translation and navigation lifecycle regression gates');
requireAll(windowsBuilderSource, [
  '"test_chat_concurrency_r34.py"', '"test_*r94.py"', 'chat-concurrency-regression-full.log',
  '-LogPath $ChatConcurrencyRegressionLog', 'if ($ChatConcurrencyRegressionExitCode -ne 0)',
], 'chat concurrency and task lease isolation release gate');
requireFile('backend/tests/test_chat_concurrency_r34.py');
requireFile('backend/tests/test_chat_concurrency_r94.py');
requireFile('backend/tests/support/concurrency_probe.py');

// A leftover python.exe alone cannot make a partial installation ready.
requireFile('scripts/ensure_python_environment.py');
requireFile('scripts/tests/test_python_environment.py');
requireFile('scripts/upgrade_build_pip.py');
requireFile('scripts/tests/test_pip_upgrade_r94.py');
requireAll(read('scripts/install_windows.ps1'), ['scripts\\upgrade_build_pip.py'], 'verified local pip update recovery');
requireAll(read('scripts/build_windows.ps1'), [
  'test_pip_upgrade_r94.py', '$PipUpgradeRecoveryExitCode -ne 0',
  '"test_*r94.py"', '$CollectionR94ExitCode -ne 0',
], 'r94 build and collection continuity regression gates');
requireInOrder(read('scripts/install_windows.ps1'), [
  'scripts\\ensure_python_environment.py',
  'if ($EnvironmentExitCode -ne 0)',
  '[2/8] Updating pip',
  'if ($PipUpgradeExitCode -ne 0)',
  '[3/8] Installing Python dependencies',
  'if ($PythonDependenciesExitCode -ne 0)',
  'verify_runtime_ready.py write',
], 'verify local pip before dependency installation and runtime readiness');
requireAll(read('scripts/install_windows.ps1'), [
  'python-environment-full.log', 'pip-upgrade-full.log', 'python-dependencies-full.log',
  "getattr(sys, '_base_executable', sys.executable)",
], 'isolated environment repair diagnostics');
requireAll(publicCiEntry, ['test_python_environment.py', 'scripts/tests', 'unittest', 'discover'], 'Windows environment regression before full release build');

// The default release command always verifies real compiled output. Source-only
// preflight does not create artifacts and must never report a desktop build PASS.
verifyRendererReleaseCopy(workbenchSource + "\n" + greetingMessagesSource, "renderer source release copy");

async function verifyCompiledArtifacts() {
  for (const file of [
    "renderer/dist/index.html",
    "dist-electron/main.js",
    "dist-electron/preload.cjs",
    "dist-electron/core-supervisor.js",
    "dist-electron/core-request-policy.js",
    "dist-electron/navigation-policy.js",
    "dist-electron/immersive-translator.js",
    "dist-electron/immersive-source.js",
    "dist-electron/immersive-bootstrap.js",
    "dist-electron/immersive-policy.js",
    "dist-electron/immersive-domains.js",
    "dist-electron/immersive-preload.cjs",
    "dist-electron/immersive-pool.js",
    "dist-electron/immersive-startup.js",
    "dist-electron/whatsapp-boundary.js",
    "dist-electron/whatsapp-columns.js"
  ]) requireFile(file);

  // tsc preserves files whose source was removed. Check the complete inventory,
  // not only a small set of entry points, before packaging an incremental build.
  const expectedDesktop = new Set(filesBelow('desktop/src', path =>
    /\.(?:ts|cts|mts)$/.test(path) && !/\.d\.(?:ts|cts|mts)$/.test(path)).map(path => {
      const name = relative(at('desktop/src'), path)
        .replace(/\.cts$/, '.cjs').replace(/\.mts$/, '.mjs').replace(/\.ts$/, '.js');
      return join(at('dist-electron'), name);
    }));
  const actualDesktop = new Set(filesBelow('dist-electron'));
  for (const path of actualDesktop) {
    assert(expectedDesktop.has(path), `unexpected desktop artifact: ${relative(root, path)}`);
  }
  for (const path of expectedDesktop) {
    assert(actualDesktop.has(path), `compiled desktop source is missing: ${relative(root, path)}`);
  }

  const productionBundles = filesBelow("renderer/dist", (path) => /\.(?:js|mjs|cjs)$/.test(path));
  assert(
    productionBundles.length === 1,
    `renderer production output must contain exactly one current JavaScript bundle; found ${productionBundles.length}`,
  );
  const productionBundleText = productionBundles.map((path) => readFileSync(path, "utf8")).join("\n");
  verifyRendererProductionRuntime(productionBundleText);
  forbidAll(productionBundleText, [
    "@simulator_flow", "@demo.public", "@demo.private", "Jenny Liu", "Mia Wong",
    "Grace Wu", "Kelly Huang", "ig-screening-simulator", "workspace-pages",
    "Core 快照已达到当前容量上限", "缓存和临时文件已清理", "手动清理缓存",
    "每类最多显示最近", "storage.temporary_bytes",
  ], "production renderer bundle");
  verifyRendererReleaseCopy(productionBundleText, "production capacity and safe cleanup copy");
  requireAll(productionBundleText, ["清空所有缓存", "自动清理", "谷歌翻译"], "desktop tools UI");
  const productionStyles = filesBelow("renderer/dist", (path) => path.endsWith(".css"));
  assert(productionStyles.length > 0, "renderer production stylesheet is missing");
  const productionStyleText = productionStyles.map((path) => readFileSync(path, "utf8")).join("\n");
  assert(
    /\.private-review-list \.quick-review-table-head,\.private-review-list \.quick-review-row\{grid-template-columns:repeat\(3,minmax\(90px,\.6fr\)\) minmax\(150px,\.85fr\) minmax\(310px,1\.7fr\) 52px minmax\(210px,1fr\)/.test(productionStyleText),
    "production renderer uses a stale private-review column order",
  );

  const navigationPolicy = await import(`${pathToFileURL(at("dist-electron/navigation-policy.js")).href}?verify=${Date.now()}`);
  assert(
    navigationPolicy.normalizedInstagramProfilePreviewUrl("https://www.instagram.com/example.user_7/")
      === "https://www.instagram.com/example.user_7/",
    "Electron no longer routes a strict Instagram profile into its isolated preview",
  );
  for (const rejectedUrl of [
    "http://www.instagram.com/example/",
    "https://user:secret@www.instagram.com/example/",
    "https://www.instagram.com:444/example/",
    "https://instagram.com.evil.example/example/",
    "https://www.instagram.com/example/followers/",
    "https://www.instagram.com/example/?next=secret",
  ]) {
    assert(
      navigationPolicy.normalizedInstagramProfilePreviewUrl(rejectedUrl) === null,
      `Electron accepted an unsafe Instagram preview URL: ${rejectedUrl}`,
    );
  }

  const policy = await import(`${pathToFileURL(at("dist-electron/core-request-policy.js")).href}?verify=${Date.now()}`);
  assert(policy.isAllowedCorePath("/api/workbench/snapshot?limit=500&history_limit=500"), "Electron blocks the bounded snapshot query");
  assert(policy.isAllowedCorePath("/api/workbench/commands"), "Electron blocks command dispatch");
  for (const rejectedPath of [
    "/v1/workbench/snapshot",
    "/api/workbench/snapshot?limit=500&history_limit=500&admin=true",
    "/api/workbench/snapshot?limit=-1&history_limit=500",
    "/api/workbench/../session/login",
    "https://example.com/api/workbench/snapshot",
  ]) {
    assert(!policy.isAllowedCorePath(rejectedPath), `Electron accepted unsafe Core path: ${rejectedPath}`);
  }
  let traceRejected = false;
  try { policy.normalizeCoreMethod("TRACE"); } catch { traceRejected = true; }
  assert(traceRejected, "Electron accepted TRACE");

  const { OptionalImmersiveSource } = await import(pathToFileURL(at('dist-electron/immersive-source.js')).href);
  const optionalSource = new OptionalImmersiveSource();
  assert(optionalSource.status().available === false, 'unconfigured optional translator must be unavailable');
  let missingSourceRejected = false;
  try { optionalSource.load(); } catch { missingSourceRejected = true; }
  assert(missingSourceRejected, 'unconfigured optional translator must reject loading without a bundled payload');
}

// r84 keeps foreground verification and both zero-post switch states mandatory.
for (const file of ['desktop/tests/visible-fixture.cjs', 'desktop/tests/visible-fixture-r84.test.cjs', 'backend/tests/test_zero_switch_r84.py']) requireFile(file);
requireAll(read('package.json'), ['desktop/tests/visible-fixture-r84.test.cjs'], 'r84 foreground fixture regression gate');
requireAll(read('scripts/build_windows.ps1'), ['test_zero_switch_r84.py', 'ZeroSwitchR84ExitCode -ne 0'], 'r84 zero-post switch regression gate');
requireAll(read('desktop/tests/embedded-browser.integration.cjs'), ["visibleShell('review-preview-ui entry')", 'loginSurfaceStart', 'captureShellFailure'], 'r84 real preview visibility and failure evidence');

// Slow-machine allowances retain failure checks and owned-process cleanup.
for (const path of ["scripts/backend_test_process.py"]) requireFile(path);
requireAll(read("scripts/backend_test_process.py"), ["CASE_TIMEOUT_SECONDS = 180.0", "PROCESS_TIMEOUT_SECONDS = 1800.0", "process.kill()", "process.wait()", "Captured output"], "r92 bounded subprocess diagnostics");
requireAll(read("scripts/run_backend_tests.py"), ["--case-timeout", "class TimedTestResult", "dump_traceback_later", "exit=True"], "r92 per-case watchdog");
requireAll(read("scripts/tests/test_backend_test_runner.py"), ["test_blocked_child_emits_stack_and_failure_instead_of_losing_output", "test_parent_deadline_joins_its_child_and_preserves_output", "test_budget_restarts_for_each_case_and_cleanup_not_log_output"], "r92 failure and timeout regressions");

console.log("NewGen desktop source verification: PASS");
if (sourceOnly) {
  console.log("DESKTOP_ARTIFACTS_CHECK=NOT_RUN (source-only preflight)");
} else {
  try {
    await verifyCompiledArtifacts();
    console.log("NewGen desktop build verification: PASS");
    console.log(`Product: 聚鑫国际 v${packageJson.version}`);
    console.log(`Installer: Juxin-IG-Audience-Collector-NewGen-Setup-${packageJson.version}-x64.exe`);
  } catch (error) {
    // Keep the concrete cause at the end of the log instead of burying it above
    // a long Node/PowerShell stack. The build must still stop on every failure.
    console.error(`DESKTOP_ARTIFACTS_CHECK=FAILED: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
