param([switch]$PortableOnly, [ValidateSet("bundled", "installed-chrome")][string]$BrowserMode = "bundled")

$ErrorActionPreference = "Stop"
# Python redirected output and PowerShell's native decoder must agree.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Bound every backend test, setup and cleanup through the existing process-level
# watchdog. Explicit narrower case budgets (including native final-source 90s)
# take precedence; no failed or hung case may become a passing release gate.
$env:IGAC_TEST_CASE_TIMEOUT_SECONDS = "180"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
. "$PSScriptRoot\build_mutex.ps1"
. "$PSScriptRoot\invoke_native_logged.ps1"
$IgacBuildMutex = Enter-IgacBuildMutex

try {

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code: $LASTEXITCODE)"
    }
}

# Read UTF-8 explicitly so current-version outputs can be invalidated before
# any dependency check fails.  The later Node read is an independent parse.
$PackageJsonPath = Join-Path $Root "package.json"
try {
    $PackageJsonText = [System.IO.File]::ReadAllText($PackageJsonPath, [System.Text.Encoding]::UTF8)
    $PackageMetadata = $PackageJsonText | ConvertFrom-Json
    $Version = ([string]$PackageMetadata.version).Trim()
} catch {
    throw "Read application version failed before build: $($_.Exception.Message)"
}
if ([string]::IsNullOrWhiteSpace($Version) -or $Version -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.-]+)?$') {
    throw "Read application version failed: package.json returned '$Version'"
}

$InstallerOutput = Join-Path $Root "installer-output"
New-Item -ItemType Directory -Path $InstallerOutput -Force | Out-Null
$PyInstallerLog = Join-Path $InstallerOutput "pyinstaller-full.log"
$FrozenSmokeLog = Join-Path $InstallerOutput "frozen-openvino-smoke.log"
$BuilderLog = Join-Path $InstallerOutput "electron-builder-full.log"
$EmbeddedRuntimeLog = Join-Path $InstallerOutput "embedded-runtime-full.log"
$EmbeddedBrowserLog = Join-Path $InstallerOutput "embedded-browser-full.log"
$EmbeddedResultPath = Join-Path $InstallerOutput "embedded-browser-check.json"
$EmbeddedFailurePath = Join-Path $InstallerOutput "embedded-browser-failure.json"
$BuildSourceLog = Join-Path $InstallerOutput "build-source-full.log"
$BuildSourceReport = Join-Path $InstallerOutput "build-source.json"
$SourceContractLog = Join-Path $InstallerOutput "source-contract-full.log"
$DesktopBuildLog = Join-Path $InstallerOutput "build-desktop-full.log"
$PythonEnvironmentLog = Join-Path $InstallerOutput "python-environment-full.log"
$PipUpgradeLog = Join-Path $InstallerOutput "pip-upgrade-full.log"
$PythonDependenciesLog = Join-Path $InstallerOutput "python-dependencies-full.log"
$PythonPackagingLog = Join-Path $InstallerOutput "python-packaging-full.log"
$BuildFailurePath = Join-Path $InstallerOutput "BUILD_FAILURE.txt"
$TimezoneDataLog = Join-Path $InstallerOutput "timezone-data-full.log"
$GreetingRegressionLog = Join-Path $InstallerOutput "greeting-regression-full.log"
$DedupRegressionLog = Join-Path $InstallerOutput "dedup-regression-full.log"
$DedupDiagnosticLog = Join-Path $InstallerOutput "dedup-diagnostic-full.log"
$StabilityRegressionLog = Join-Path $InstallerOutput "stability-regression-full.log"
$CorePreflightLog = Join-Path $InstallerOutput "core-preflight-full.log"
$RecoveryRegressionLog = Join-Path $InstallerOutput "recovery-regression-full.log"
$TaskControlRegressionLog = Join-Path $InstallerOutput "task-control-regression-full.log"
$BackendTestRunnerLog = Join-Path $InstallerOutput "backend-test-runner-full.log"
$CollectionPipelineRegressionLog = Join-Path $InstallerOutput "collection-pipeline-regression-full.log"
$CollectionLongRunRegressionLog = Join-Path $InstallerOutput "collection-longrun-regression-full.log"
$CollectionProgressR46Log = Join-Path $InstallerOutput "collection-progress-r46-full.log"
$CollectionProfileR45Log = Join-Path $InstallerOutput "collection-profile-r45-full.log"
$CollectionCompletionR44Log = Join-Path $InstallerOutput "collection-completion-r44-full.log"
$CollectionCompletionR43Log = Join-Path $InstallerOutput "collection-completion-r43-full.log"
$CollectionResourceRegressionLog = Join-Path $InstallerOutput "collection-resource-regression-full.log"
$WindowLifecycleRegressionLog = Join-Path $InstallerOutput "window-lifecycle-regression-full.log"
$WindowPerformanceRegressionLog = Join-Path $InstallerOutput "window-performance-regression-full.log"
$ScreenPersistenceRegressionLog = Join-Path $InstallerOutput "screen-persistence-regression-full.log"
$ChatConcurrencyRegressionLog = Join-Path $InstallerOutput "chat-concurrency-regression-full.log"
$SpecifiedWindowsRegressionLog = Join-Path $InstallerOutput "specified-windows-regression-full.log"
$ZeroPostRegressionLog = Join-Path $InstallerOutput "zero-post-regression-full.log"
$MonitorPartialRegressionLog = Join-Path $InstallerOutput "monitor-partial-regression-full.log"
$DiscardLimitsRegressionLog = Join-Path $InstallerOutput "discard-limits-regression-full.log"
$DiscardWorkflowRegressionLog = Join-Path $InstallerOutput "discard-workflow-r38-regression-full.log"
$PreopenDedupeRegressionLog = Join-Path $InstallerOutput "preopen-dedupe-r39-regression-full.log"
$HoverQueueR72Log = Join-Path $InstallerOutput "hover-queue-r72-full.log"
$WindowQueueR73Log = Join-Path $InstallerOutput "window-queue-r73-full.log"
$BuildResultMarker = Join-Path $InstallerOutput "LATEST_SUCCESS.txt"
$BuildResultTemporary = Join-Path $InstallerOutput "LATEST_SUCCESS.txt.tmp"
$InstallerPath = Join-Path $InstallerOutput "Juxin-IG-Audience-Collector-NewGen-Setup-$Version-x64.exe"
$InstallerHashPath = "$InstallerPath.sha256"
$InstallerHashTemporary = "$InstallerHashPath.tmp"
$PortableName = "Juxin-IGAC-NewGen-Portable-v$Version-x64"
$PortableRoot = Join-Path $InstallerOutput $PortableName
$PortablePath = Join-Path $InstallerOutput "$PortableName.zip"

# Only exact current-version generated artifacts are removed.  Older releases
# are left untouched, but can never satisfy this build's success check.
foreach ($GeneratedPath in @(
    $BuildFailurePath,
    $PythonPackagingLog,
    (Join-Path $InstallerOutput "build-entry-r94-full.log"),
    (Join-Path $InstallerOutput "build-recovery-r94-full.log"),
    $PyInstallerLog,
    $FrozenSmokeLog,
    (Join-Path $InstallerOutput "release-packaging-r94-full.log"),
    (Join-Path $InstallerOutput "frozen-core-service.log"),
    (Join-Path $InstallerOutput "portable-core-service.log"),
    (Join-Path $InstallerOutput "portable-archive-full.log"),
    $BuilderLog,
    $EmbeddedRuntimeLog,
    $EmbeddedBrowserLog,
    $EmbeddedResultPath,
    $EmbeddedFailurePath,
    (Join-Path $InstallerOutput "embedded-shell-failure.json"),
    (Join-Path $InstallerOutput "embedded-shell-failure.png"),
    (Join-Path $InstallerOutput "zero-switch-r84-full.log"),
    $BuildSourceLog,
    $BuildSourceReport,
    $SourceContractLog,
    $DesktopBuildLog,
    $PythonEnvironmentLog,
    $PipUpgradeLog,
    $PythonDependenciesLog,
    $TimezoneDataLog,
    $GreetingRegressionLog,
    $DedupRegressionLog,
    $DedupDiagnosticLog,
    $StabilityRegressionLog,
    $RecoveryRegressionLog,
    $TaskControlRegressionLog,
    $BackendTestRunnerLog,
    $CollectionPipelineRegressionLog,
    $CollectionLongRunRegressionLog,
    $CollectionResourceRegressionLog,
    $WindowLifecycleRegressionLog,
    $WindowPerformanceRegressionLog,
    $ScreenPersistenceRegressionLog,
    $ChatConcurrencyRegressionLog,
    $SpecifiedWindowsRegressionLog,
    $ZeroPostRegressionLog,
    $MonitorPartialRegressionLog,
    $DiscardLimitsRegressionLog,
    $DiscardWorkflowRegressionLog,
    $PreopenDedupeRegressionLog,
    $HoverQueueR72Log,
    $WindowQueueR73Log,
    $BuildResultMarker,
    $BuildResultTemporary,
    $InstallerPath,
    "$InstallerPath.blockmap",
    $InstallerHashPath,
    $InstallerHashTemporary,
    $PortableRoot,
    $PortablePath
)) {
    if (Test-Path -LiteralPath $GeneratedPath) {
        Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
    }
}
$ReleaseBuildStartedAt = [DateTime]::UtcNow
$BrowserRequirement = Join-Path $Root "build\browsers\juxin-runtime-requirement.json"
Remove-Item -LiteralPath $BrowserRequirement -ErrorAction SilentlyContinue
Remove-Item Env:IGAC_RUNTIME_REQUIREMENT -ErrorAction SilentlyContinue
Remove-Item Env:IGAC_TEST_CHROMIUM_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:IGAC_POSTING_TEST_BROWSER -ErrorAction SilentlyContinue
Write-Host "Browser build mode: $BrowserMode"
if ($BrowserMode -eq "installed-chrome") {
    Write-Host "This edition requires installed Google Chrome on every target computer." -ForegroundColor Yellow
}

Write-Host "Checking source revision before dependency installation..." -ForegroundColor Cyan
$BuildSourceExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command node -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("scripts\verify_build_source.mjs") `
    -LogPath $BuildSourceLog
if ($BuildSourceExitCode -ne 0) {
    throw "Source verification failed. Extract the complete 3.0 r94 package into a new directory. See installer-output\build-source-full.log."
}

Write-Host "Checking desktop source contracts before dependency installation..." -ForegroundColor Cyan
$SourceContractExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command node -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("scripts\verify_desktop_build.mjs", "--source-only") `
    -LogPath $SourceContractLog
if ($SourceContractExitCode -ne 0) {
    throw "Desktop source contract verification failed (exit code: $SourceContractExitCode). See installer-output\source-contract-full.log."
}

# Run the real native stdout/stderr and exit-code probe on this exact Windows
# PowerShell host before the lengthy dependency and OpenVINO build begins.
Write-Host "Verifying Windows native command logging..." -ForegroundColor Cyan
& "$PSScriptRoot\test_native_command_logging.ps1"

& "$PSScriptRoot\install_windows.ps1"

# Catch fixture path and native URL conversion regressions before long suites.
$RepairRendererExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command node -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("--test", "renderer/tests/source-recheck-r95.test.mjs", "renderer/tests/workbench-platform-r95.test.mjs", "renderer/tests/completed-card-delete-r96.test.mjs", "renderer/tests/automatic-recheck-r97.test.mjs") `
    -LogPath (Join-Path $InstallerOutput "repair-renderer-preflight.log")
if ($RepairRendererExitCode -ne 0) { throw "Verify platform and retained-recheck renderer fixtures failed." }

# Verify the exact native platform fixture before long Python/browser groups.
# This is additional fail-fast coverage; the complete embedded gate still runs.
$PlatformNativePreflightExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root "node_modules\.bin\electron.cmd") `
    -ArgumentList @("desktop\tests\workbench-platform.preflight.cjs") `
    -LogPath (Join-Path $InstallerOutput "platform-native-preflight-full.log")
if ($PlatformNativePreflightExitCode -ne 0) { throw "Native platform fixture preflight failed; full build stopped before expensive gates." }

# The cleanup fixture imports the real compiled host. Dependency installation
# does not create dist-electron; always clean and compile it before this gate.
# The later full desktop build and its complete verification remain mandatory.
$NativePreflightBuildExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command npm.cmd -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("run", "build:electron") `
    -LogPath (Join-Path $InstallerOutput "native-preflight-build-full.log")
if ($NativePreflightBuildExitCode -ne 0) {
    throw "Compile desktop host for native preflight failed (exit code: $NativePreflightBuildExitCode). See installer-output\native-preflight-build-full.log."
}

# R6.3: verify authoritative absence before expensive suites; this does not
# substitute for the separately installed legacy-database upgrade proof.
$env:JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE = "1"
foreach ($StaleCleanupProof in @("r63-nurture-cleanup-native.json", "r63-nurture-cleanup-native.failure.json", "r63-nurture-cleanup-native.png")) {
    Remove-Item -LiteralPath (Join-Path $InstallerOutput $StaleCleanupProof) -ErrorAction SilentlyContinue
}
$CleanupNativeExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root "node_modules\.bin\electron.cmd") `
    -ArgumentList @("desktop\tests\nurture-cleanup-native-r63.cjs") `
    -LogPath (Join-Path $InstallerOutput "r63-nurture-cleanup-native-full.log")
if ($CleanupNativeExitCode -ne 0) { throw "Native cleanup guard regression failed." }

# R6.4 additive native fixtures use the compiled production host/components.
# Both fail closed and have their own complete-process watchdogs.
$env:JUXIN_REQUIRE_RECOVERY_UI_NATIVE = "1"
$env:IGAC_CROP_FIXTURE_REPORT = Join-Path $InstallerOutput "r64-crop-icon-native.json"
$env:IGAC_CROP_FIXTURE_SCREENSHOT = Join-Path $InstallerOutput "r64-crop-icon-native.png"
$PreviousCropPython = $env:PYTHON
$env:PYTHON = Join-Path $Root ".venv\Scripts\python.exe"
try {
    foreach ($R64StaleProof in @("r64-crop-icon-native.json", "r64-crop-icon-native.png", "r64-recovery-ui-native-proof.json", "r64-recovery-ui-native-failure.json")) {
        Remove-Item -LiteralPath (Join-Path $InstallerOutput $R64StaleProof) -ErrorAction SilentlyContinue
    }
    foreach ($R64NativeFixture in @("desktop\tests\crop-icon-r64.cjs", "desktop\tests\recovery-ui-native-r64.cjs")) {
        $R64NativeExitCode = Invoke-IgacNativeCommandWithLog `
            -FilePath (Join-Path $Root "node_modules\.bin\electron.cmd") `
            -ArgumentList @($R64NativeFixture) `
            -LogPath (Join-Path $InstallerOutput (([IO.Path]::GetFileNameWithoutExtension($R64NativeFixture)) + "-full.log"))
        if ($R64NativeExitCode -ne 0) { throw "Required R6.4 native fixture failed: $R64NativeFixture" }
    }
} finally {
    if ($null -eq $PreviousCropPython) { Remove-Item Env:PYTHON -ErrorAction SilentlyContinue } else { $env:PYTHON = $PreviousCropPython }
}


# Pure IG deliberately retires the removed platform gates. All shared/native IG
# gates remain; rejection, safe legacy purge and saturation are mandatory.
foreach ($PureIgPattern in @("test_pure_ig*.py", "test_ig_only_runtime.py", "test_saturation_acceptance_r99.py")) {
    $PureIgExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
        -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", $PureIgPattern, "-v") `
        -LogPath (Join-Path $InstallerOutput ("pure-ig-" + $PureIgPattern.Replace("*", "all") + ".log"))
    if ($PureIgExitCode -ne 0) { throw "Pure IG safety or saturation regression failed: $PureIgPattern" }
}

# New repair coverage is mandatory, in addition to all retained historical gates.
$env:IGAC_REQUIRE_PARENT_REELS_BROWSER = "1"
$env:IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER = "1"
$env:IGAC_PARENT_REELS_FIXTURE_ARTIFACT_DIR = (Join-Path $InstallerOutput "parent-reels-fixtures")
foreach ($RepairPattern in @("test_installed_recovery_r64.py", "test_combined_recovery_r64.py", "test_hidden_collection_blocker_r63.py", "test_posting_withdraw*_r63.py", "test_posting_durable_preflight_r63.py", "test_installed_nurture_cleanup_upgrade_r63.py", "test_nurture_cleanup*.py", "test_nurture_closed_profile_guard.py", "test_nurture_missing_lease_surface.py", "test_final_seed_completion_r62.py", "test_screening_factory_recovery_r62.py", "test_standalone_reels_routes_r62.py", "test_standalone_watch_advance_r62.py", "test_nurture_completion_cleanup_r62.py", "test_parent_reels*.py", "test_collector_parent_handoff_r6.py", "test_live_parent_recheck_r6.py", "test_instagram_identity*r62.py", "test_posting_startup_r62.py", "test_posting_retry_r61.py", "test_automatic_gap_rechecks.py", "test_single_gap_recheck_r6.py", "test_standalone_nurture*.py", "test_installed_standalone_nurture_r6.py", "test_installed_posting_workflow_r6.py", "test_pexels_configuration_r6.py", "test_posting_lease_integration_r6.py", "test_posting_api_integration_r6.py", "test_posting_missing_lease_surface_r6.py", "test_recovery_responsiveness_r94.py", "test_internal_pexels_route_r6.py", "test_posting_*_v2.py", "test_confirmed_posting_report_r6.py", "test_installed_work_report_summary_r6.py", "test_work_report_performance_r6.py", "test_report_index_upgrade_r61.py", "test_frozen_service_r94.py", "test_*r97.py", "test_storage_r31.py", "test_completed_card_dismissal_r96.py", "test_installed_completed_card_fixture_r96.py", "test_explicit_source_recheck.py", "test_snapshot_scale_r95.py", "test_platform_scope.py", "test_platform_review_reports_r95.py", "test_instagram_500_report_r95.py", "test_relation_recommendation_tail_r95.py")) {
    $RepairExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
        -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", $RepairPattern, "-v") `
        -LogPath (Join-Path $InstallerOutput ("repair-" + $RepairPattern.Replace("*", "all").Replace("?", "one") + ".log"))
    if ($RepairExitCode -ne 0) {
        throw "Verify retained recheck, scale or platform isolation failed: $RepairPattern"
    }
}

# Each browser mode executes this gate exactly once. Installed Chrome can fail
# fast before long historical suites; bundled mode waits for its runtime below.
function Invoke-IgacInstagramThousandGate {
    $env:IGAC_REQUIRE_POSTING_BROWSER = "1"
    try {
        $InstagramThousandExitCode = Invoke-IgacNativeCommandWithLog `
            -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
            -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_instagram_thousand_browser_r95.py", "-v") `
            -LogPath (Join-Path $InstallerOutput "instagram-thousand-browser-full.log")
        if ($InstagramThousandExitCode -ne 0) { throw "Thousand-person Chrome regression failed." }
    } finally {
        Remove-Item Env:IGAC_REQUIRE_POSTING_BROWSER -ErrorAction SilentlyContinue
    }
    # The selected browser is now available in both supported build modes.
    $env:IGAC_REQUIRE_SATURATION_BROWSER = "1"
    try {
        $SaturationExitCode = Invoke-IgacNativeCommandWithLog `
            -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
            -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_saturation_acceptance_r99.py", "-v") `
            -LogPath (Join-Path $InstallerOutput "saturation-browser-full.log")
        if ($SaturationExitCode -ne 0) { throw "Required real-browser saturation fallback verification failed" }
    } finally {
        Remove-Item Env:IGAC_REQUIRE_SATURATION_BROWSER -ErrorAction SilentlyContinue
    }
}
if ($BrowserMode -eq "installed-chrome") {
    $EarlyChromeCandidatePath = Join-Path $InstallerOutput "early-chrome-candidate.json"
    & .venv\Scripts\python.exe -X utf8 scripts\browser_build_policy.py --candidate-output $EarlyChromeCandidatePath
    Assert-LastExitCode "Find installed Chrome for early collection regression"
    $EarlyChrome = [IO.File]::ReadAllText($EarlyChromeCandidatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $env:IGAC_TEST_CHROMIUM_EXECUTABLE = [string]$EarlyChrome.executable
    Invoke-IgacInstagramThousandGate
}

# Verify pinned packaging tools before the long regression suites. Recover once
# from stale pip sources using the same isolated installer as Core dependencies.
$PackagingDependenciesExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "scripts\install_python_dependencies.py", "--project-root", $Root, "--packaging") `
    -LogPath $PythonPackagingLog
if ($PackagingDependenciesExitCode -ne 0) {
    throw "Install Windows packaging component failed (exit code: $PackagingDependenciesExitCode). See installer-output\python-packaging-full.log."
}

# Includes a real offline npm install under production-only inherited settings.
$BuildEntryExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_build_entry_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "build-entry-r94-full.log")
if ($BuildEntryExitCode -ne 0) {
    throw "Verify build environment and process ownership failed (exit code: $BuildEntryExitCode). See installer-output\build-entry-r94-full.log."
}

# Archive fault injection and process ownership checks run before packaging.
$BuildRecoveryExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_build_recovery_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "build-recovery-r94-full.log")
if ($BuildRecoveryExitCode -ne 0) {
    throw "Verify build retry and dependency recovery failed (exit code: $BuildRecoveryExitCode). See installer-output\build-recovery-r94-full.log."
}

$BrowserDownloadExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_browser_download_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "browser-download-r94-full.log")
if ($BrowserDownloadExitCode -ne 0) {
    throw "Verify browser download resume and cache integrity failed. See installer-output\browser-download-r94-full.log."
}

$BrowserRepairExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_native_repair_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "browser-repair-r94-full.log")
if ($BrowserRepairExitCode -ne 0) {
    throw "Verify browser repair and rollback failed. See installer-output\browser-repair-r94-full.log."
}

$ReleasePackagingExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_release_packaging_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "release-packaging-r94-full.log")
if ($ReleasePackagingExitCode -ne 0) {
    throw "Verify release archive and Core process fault handling failed (exit code: $ReleasePackagingExitCode). See installer-output\release-packaging-r94-full.log."
}

# Offline regression: stale pip sources must recover without relaxing pins.
$DependencyRecoveryExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_python_dependencies_r83.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "python-dependency-recovery-r83-full.log")
if ($DependencyRecoveryExitCode -ne 0) {
    throw "Verify dependency source recovery failed (exit code: $DependencyRecoveryExitCode). See installer-output\python-dependency-recovery-r83-full.log."
}

# Optional pip updates may retain a verified local pip, never a broken one.
$PipUpgradeRecoveryExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_pip_upgrade_r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "pip-upgrade-recovery-r94-full.log")
if ($PipUpgradeRecoveryExitCode -ne 0) {
    throw "Verify local pip update recovery failed (exit code: $PipUpgradeRecoveryExitCode). See installer-output\pip-upgrade-recovery-r94-full.log."
}

# This subprocess probe deliberately ignores inherited Python paths. The same
# source-root runner is used by every backend test group below.
$BackendTestRunnerExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_backend_test_runner.py", "-v") `
    -LogPath $BackendTestRunnerLog
if ($BackendTestRunnerExitCode -ne 0) {
    throw "Verify backend tests in a clean Python process failed (exit code: $BackendTestRunnerExitCode). See installer-output\backend-test-runner-full.log."
}


# Catch stale schema assumptions before browser downloads and compilation.
$SchemaSmokeExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_backend_smoke_schema_r40.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "backend-smoke-schema-r40-full.log")
if ($SchemaSmokeExitCode -ne 0) {
    throw "Verify current SQLite release schema failed (exit code: $SchemaSmokeExitCode). See installer-output\backend-smoke-schema-r40-full.log."
}

# Fail on recovery, slow-disk watchdog and returned-queue regressions before
# native browser checks and desktop compilation. Each gate runs exactly once.
$RecoveryRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r25.py", "-v") `
    -LogPath $RecoveryRegressionLog
if ($RecoveryRegressionExitCode -ne 0) {
    throw "Verify automatic collection recovery and checkpoint persistence failed (exit code: $RecoveryRegressionExitCode). See installer-output\recovery-regression-full.log."
}

$CollectionWaitR53ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r53.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "collection-wait-r53-full.log")
if ($CollectionWaitR53ExitCode -ne 0) {
    throw "Verify durable collection progress watchdog failed (exit code: $CollectionWaitR53ExitCode). See installer-output\collection-wait-r53-full.log."
}

$WindowQueueR73ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_returned*.py", "-v") `
    -LogPath $WindowQueueR73Log
if ($WindowQueueR73ExitCode -ne 0) {
    throw "Verify stale window ownership and queue release failed (exit code: $WindowQueueR73ExitCode). See installer-output\window-queue-r73-full.log."
}

# All cumulative R94 ownership, persistence, recovery and build regressions.
# Includes batch handoff, progress, cancellation and R94 chat/hover checks once.
# Checkpoint R25 and pre-open R39 checks run in their earlier version groups.
$CollectionR94ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r94.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "collection-continuity-r94-full.log")
if ($CollectionR94ExitCode -ne 0) {
    throw "Verify collection continuity failed (exit code: $CollectionR94ExitCode). See installer-output\collection-continuity-r94-full.log."
}

# Detect a missing/corrupt test driver before the costly freeze/package steps.
# The loader calls project Python directly; no non-ASCII native output is
# round-tripped through PowerShell to become a JavaScript require() path.
$env:JUXIN_PYTHON = (Resolve-Path ".venv\Scripts\python.exe").Path
try {
    $EmbeddedRuntimeExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath (Get-Command node -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
        -ArgumentList @("--test", "desktop\tests\playwright-runtime.test.cjs") `
        -LogPath $EmbeddedRuntimeLog
    if ($EmbeddedRuntimeExitCode -ne 0) {
        throw "Verify embedded test dependencies and Unicode paths failed (exit code: $EmbeddedRuntimeExitCode). See installer-output\embedded-runtime-full.log."
    }
} finally {
    Remove-Item Env:JUXIN_PYTHON -ErrorAction SilentlyContinue
}

# install_windows.ps1 downloads each model atomically. Verify the complete set
# again immediately before packaging so a missing or modified file can never
# produce an installer whose local person recognition silently does not work.
& .venv\Scripts\python.exe scripts\download_person_models.py --check
Assert-LastExitCode "Verify local person-recognition models"

# Windows PowerShell 5.1 reads UTF-8 files without a BOM using the active
# system code page. package.json contains Chinese product text, so piping it
# through Get-Content/ConvertFrom-Json can corrupt the text and reject valid
# JSON. Node already parsed package.json during npm install; use it to obtain
# the version without depending on the Windows code page.
$VersionOutput = & node -p "require('./package.json').version"
Assert-LastExitCode "Read application version"
$NodeVersion = ([string]$VersionOutput).Trim()
if ($NodeVersion -ne $Version) {
    throw "Read application version failed: .NET returned '$Version' but Node returned '$NodeVersion'"
}


# Browser modes are explicit. The default still verifies the bundled engine.
# The compatibility entry requires a full installed-Chrome gate and seals that
# dependency into both installer and portable resources.
$BrowserRuntime = Join-Path $Root "build\browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRuntime
$env:IGAC_BROWSER_DIR = $BrowserRuntime
Remove-Item Env:IGAC_NATIVE_BROWSER_EXECUTABLE -ErrorAction SilentlyContinue
if ($BrowserMode -eq "installed-chrome") {
    New-Item -ItemType Directory -Path $BrowserRuntime -Force | Out-Null
    $ChromeCandidatePath = Join-Path $InstallerOutput "installed-chrome-candidate.json"
    Remove-Item -LiteralPath $ChromeCandidatePath -ErrorAction SilentlyContinue
    & .venv\Scripts\python.exe -X utf8 scripts\browser_build_policy.py --candidate-output $ChromeCandidatePath
    Assert-LastExitCode "Find the required installed Chrome"
    $ChromeCandidate = [IO.File]::ReadAllText($ChromeCandidatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $env:IGAC_TEST_CHROMIUM_EXECUTABLE = [string]$ChromeCandidate.executable
    $env:IGAC_POSTING_TEST_BROWSER = [string]$ChromeCandidate.executable
} else {
    & .venv\Scripts\python.exe -m playwright install chromium --no-shell
    if ($LASTEXITCODE -ne 0) {
        & .venv\Scripts\python.exe scripts\install_native_browser.py
        Assert-LastExitCode "Download exact pinned Chromium from official mirror"
    }
    & .venv\Scripts\python.exe scripts\prune_browser_runtime.py
    Assert-LastExitCode "Remove outdated generated browser runtimes"
}
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_native_launch.py" -v
Assert-LastExitCode "Verify native runtime selection and launch diagnostics"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_native_requirement.py" -v
Assert-LastExitCode "Verify installed Chrome dependency and rejected build results"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_native_diagnostic.py" -v
Assert-LastExitCode "Verify standalone diagnostic collector"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_browser_permissions.py" -v
Assert-LastExitCode "Verify scoped sandbox permissions and real network checks"
if ($BrowserMode -eq "bundled") {
    $BundledBrowserExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
        -ArgumentList @("-X", "utf8", "scripts\repair_native_browser.py") `
        -LogPath (Join-Path $InstallerOutput "native-bundled-full.log")
    if ($BundledBrowserExitCode -ne 0) {
        throw "The exact packaged browser failed verification and bounded recovery. See installer-output\native-bundled-full.log and native-browser-repair-*.zip."
    }
}
$NativeArguments = @("-X", "utf8", "scripts\verify_native_browser.py", "--no-headless", "--policy-output", $BrowserRequirement)
if ($BrowserMode -eq "installed-chrome") { $NativeArguments += "--require-installed-chrome" }
$NativeBrowserExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList $NativeArguments `
    -LogPath (Join-Path $InstallerOutput "native-browser-full.log")
if ($NativeBrowserExitCode -ne 0) {
    throw "Verify real standalone browser isolation and persistence failed (exit code: $NativeBrowserExitCode). See installer-output\native-browser-full.log and installer-output\native-browser-diagnostics."
}
$SealedBrowserRequirement = [IO.File]::ReadAllText($BrowserRequirement, [Text.Encoding]::UTF8) | ConvertFrom-Json
if ($BrowserMode -eq "installed-chrome" -and $SealedBrowserRequirement.mode -ne "installed-chrome-required") {
    throw "Installed-Chrome build did not seal its runtime dependency"
}

# Validate relation geometry and completion before desktop compilation.
$CollectionCompletionR44ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r44.py", "-v") `
    -LogPath $CollectionCompletionR44Log
if ($CollectionCompletionR44ExitCode -ne 0) {
    throw "Verify pending-candidate completion, source-end validation and cancellation cleanup failed (exit code: $CollectionCompletionR44ExitCode). See installer-output\collection-completion-r44-full.log."
}
# Exercise create/upload acknowledgement boundaries before the real CDP fixture.
$PostingTransitionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("scripts\run_backend_tests.py", "-p", "test_posting_transition_r80.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "posting-transition-r80-full.log")
if ($PostingTransitionExitCode -ne 0) {
    throw "Verify posting create/upload transitions failed (exit code: $PostingTransitionExitCode). See installer-output\posting-transition-r80-full.log."
}

# Retained unread pages and unacknowledged window closes keep their ownership.
$PipelineRetainedExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("scripts\run_backend_tests.py", "-p", "test_*r81.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "pipeline-retained-r81-full.log")
if ($PipelineRetainedExitCode -ne 0) {
    throw "Verify retained screening pages and finite-task window close failed (exit code: $PipelineRetainedExitCode). See installer-output\pipeline-retained-r81-full.log."
}

# Run the desktop and native integration gates before expensive backend freezing.
# Every release gate is retained; a UI failure must not require a model repack first.
Write-Host "Building the desktop application..." -ForegroundColor Cyan
$CorePreflightExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\verify_r18_core.py") `
    -LogPath $CorePreflightLog
if ($CorePreflightExitCode -ne 0) {
    throw "Verify stability, follow-list scrolling, task ownership and recovery regressions failed (exit code: $CorePreflightExitCode). See installer-output\core-preflight-full.log."
}
# Bundled mode runs the same mandatory gate after its runtime is installed.
if ($BrowserMode -ne "installed-chrome") { Invoke-IgacInstagramThousandGate }
$DesktopBuildExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command npm.cmd -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("run", "build") `
    -LogPath $DesktopBuildLog
if ($DesktopBuildExitCode -ne 0) {
    throw "Build desktop application failed (exit code: $DesktopBuildExitCode). See installer-output\build-desktop-full.log for the first error and complete output."
}
$env:JUXIN_EMBEDDED_RESULT = $EmbeddedResultPath
$env:JUXIN_EMBEDDED_FAILURE = $EmbeddedFailurePath
Remove-Item -LiteralPath $EmbeddedFailurePath -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $env:JUXIN_EMBEDDED_RESULT -ErrorAction SilentlyContinue
$env:JUXIN_PYTHON = (Resolve-Path ".venv\Scripts\python.exe").Path
try {
    $EmbeddedExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath (Join-Path $Root "node_modules\.bin\electron.cmd") `
        -ArgumentList @("desktop\tests\embedded-browser.integration.cjs") `
        -LogPath $EmbeddedBrowserLog
    if ($EmbeddedExitCode -ne 0) {
        $EmbeddedFailureCause = ""
        if (Test-Path -LiteralPath $EmbeddedFailurePath) {
            try {
                $FailureText = [System.IO.File]::ReadAllText($EmbeddedFailurePath, [System.Text.Encoding]::UTF8)
                $FailureDetails = $FailureText | ConvertFrom-Json
                $FailureStep = [string]$FailureDetails.checkpoint
                if ([string]::IsNullOrWhiteSpace($FailureStep)) { $FailureStep = [string]$FailureDetails.stage }
                $FailureReason = [string]$FailureDetails.error.message
                if ([string]::IsNullOrWhiteSpace($FailureReason)) { $FailureReason = [string]$FailureDetails.reason }
                $EmbeddedFailureCause = (" [$FailureStep] $FailureReason" -replace '[\r\n]+', ' ')
            } catch {
                $EmbeddedFailureCause = " (Failure detail file could not be read.)"
            }
        }
        throw "Verify real embedded account views and original Python task adapter failed (exit code: $EmbeddedExitCode).$EmbeddedFailureCause See installer-output\embedded-browser-full.log."
    }
    if (-not (Test-Path -LiteralPath $env:JUXIN_EMBEDDED_RESULT)) { throw "Embedded browser verification exited before completing all checks" }
    $EmbeddedProof = Get-Content -Raw -LiteralPath $env:JUXIN_EMBEDDED_RESULT | ConvertFrom-Json
    if (-not $EmbeddedProof.verified -or -not $EmbeddedProof.pythonWorker) { throw "Embedded browser and Python worker verification is incomplete" }
} finally {
    Remove-Item Env:JUXIN_EMBEDDED_RESULT -ErrorAction SilentlyContinue
    Remove-Item Env:JUXIN_EMBEDDED_FAILURE -ErrorAction SilentlyContinue
    Remove-Item Env:JUXIN_PYTHON -ErrorAction SilentlyContinue
}


# The complete async/endurance suite remains a release/CI gate because its
# sub-second scheduler checks are sensitive to a busy end-user PC.  The whole
# Direct greeting suite is still mandatory here: these deterministic tests guard
# the recipient, composer, send, confirmation and campaign paths fixed in v1.0.19.
& .venv\Scripts\python.exe -m compileall -q backend scripts\verify_backend_smoke.py
Assert-LastExitCode "Compile Python backend"
$GreetingRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_greeting*.py", "-v") `
    -LogPath $GreetingRegressionLog
if ($GreetingRegressionExitCode -ne 0) {
    throw "Run complete Direct greeting regression suite failed (exit code: $GreetingRegressionExitCode). See installer-output\greeting-regression-full.log for the failing test and full traceback."
}
$DedupRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_dedup*.py", "-v") `
    -LogPath $DedupRegressionLog
if ($DedupRegressionExitCode -ne 0) {
    throw "Verify permanent global deduplication and backup recovery failed (exit code: $DedupRegressionExitCode). See installer-output\dedup-regression-full.log."
}
$DedupDiagnosticExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\tests", "-p", "test_diagnose_dedupe.py", "-v") `
    -LogPath $DedupDiagnosticLog
if ($DedupDiagnosticExitCode -ne 0) {
    throw "Verify read-only deduplication diagnostics failed (exit code: $DedupDiagnosticExitCode). See installer-output\dedup-diagnostic-full.log."
}
$StabilityRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r24.py", "-v") `
    -LogPath $StabilityRegressionLog
if ($StabilityRegressionExitCode -ne 0) {
    throw "Verify runtime, browser, action and persistence stability failed (exit code: $StabilityRegressionExitCode). See installer-output\stability-regression-full.log."
}
$CollectionCompletionR43ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r43.py", "-v") `
    -LogPath $CollectionCompletionR43Log
if ($CollectionCompletionR43ExitCode -ne 0) {
    throw "Verify natural collection completion and independent delete/return controls failed (exit code: $CollectionCompletionR43ExitCode). See installer-output\collection-completion-r43-full.log."
}
$ReviewReportsR54ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r54.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "review-reports-r54-full.log")
if ($ReviewReportsR54ExitCode -ne 0) {
    throw "Verify review-layer isolation and seven-day split audit failed (exit code: $ReviewReportsR54ExitCode). See installer-output\review-reports-r54-full.log."
}
$ReviewWorkflowR55ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r55.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "review-workflow-r55-full.log")
if ($ReviewWorkflowR55ExitCode -ne 0) {
    throw "Verify permanent split admission rules and account exports failed (exit code: $ReviewWorkflowR55ExitCode). See installer-output\review-workflow-r55-full.log."
}
$ReportSplitR56ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r56.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "report-split-r56-full.log")
if ($ReportSplitR56ExitCode -ne 0) {
    throw "Verify split reports, completion snapshots and safe window drain failed (exit code: $ReportSplitR56ExitCode). See installer-output\report-split-r56-full.log."
}
$CollectionProfileR45ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r45.py", "-v") `
    -LogPath $CollectionProfileR45Log
if ($CollectionProfileR45ExitCode -ne 0) {
    throw "Verify profile readiness, zero-post continuity and location semantics failed (exit code: $CollectionProfileR45ExitCode). See installer-output\collection-profile-r45-full.log."
}
$CollectionProgressR46ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r46.py", "-v") `
    -LogPath $CollectionProgressR46Log
if ($CollectionProgressR46ExitCode -ne 0) {
    throw "Verify source-scoped durable dedupe progress failed (exit code: $CollectionProgressR46ExitCode). See installer-output\collection-progress-r46-full.log."
}
$TaskControlRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_task_control_r27.py", "-v") `
    -LogPath $TaskControlRegressionLog
if ($TaskControlRegressionExitCode -ne 0) {
    throw "Verify target-bound collection pause and stop failed (exit code: $TaskControlRegressionExitCode). See installer-output\task-control-regression-full.log."
}


$CollectionPipelineRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r29.py", "-v") `
    -LogPath $CollectionPipelineRegressionLog
if ($CollectionPipelineRegressionExitCode -ne 0) {
    throw "Verify collection pipeline completion, candidate isolation and incremental reads failed (exit code: $CollectionPipelineRegressionExitCode). See installer-output\collection-pipeline-regression-full.log."
}


$CollectionLongRunRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r30.py", "-v") `
    -LogPath $CollectionLongRunRegressionLog
if ($CollectionLongRunRegressionExitCode -ne 0) {
    throw "Verify final list reads, bounded reconciliation and cancellation ownership failed (exit code: $CollectionLongRunRegressionExitCode). See installer-output\collection-longrun-regression-full.log."
}

$CollectionResourceRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r31.py", "-v") `
    -LogPath $CollectionResourceRegressionLog
if ($CollectionResourceRegressionExitCode -ne 0) {
    throw "Verify collection resource lifecycle, bounded history and command responsiveness failed (exit code: $CollectionResourceRegressionExitCode). See installer-output\collection-resource-regression-full.log."
}

$WindowLifecycleRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_windows_r32.py", "-v") `
    -LogPath $WindowLifecycleRegressionLog
if ($WindowLifecycleRegressionExitCode -ne 0) {
    throw "Verify independent window lifecycle, cancellation and manual restore failed (exit code: $WindowLifecycleRegressionExitCode). See installer-output\window-lifecycle-regression-full.log."
}

$WindowPerformanceRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_window_performance_r33.py", "-v") `
    -LogPath $WindowPerformanceRegressionLog
if ($WindowPerformanceRegressionExitCode -ne 0) {
    throw "Verify bounded window snapshot reads, owner isolation and inventory index failed (exit code: $WindowPerformanceRegressionExitCode). See installer-output\window-performance-regression-full.log."
}

$ScreenPersistenceRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_screen_persistence_r33.py", "-v") `
    -LogPath $ScreenPersistenceRegressionLog
if ($ScreenPersistenceRegressionExitCode -ne 0) {
    throw "Verify responsive screening writes, cancellation ownership and recoverable persistence failed (exit code: $ScreenPersistenceRegressionExitCode). See installer-output\screen-persistence-regression-full.log."
}

$ChatConcurrencyRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_chat_concurrency_r34.py", "-v") `
    -LogPath $ChatConcurrencyRegressionLog
if ($ChatConcurrencyRegressionExitCode -ne 0) {
    throw "Verify chat reads, window lease isolation and bounded concurrency failed (exit code: $ChatConcurrencyRegressionExitCode). See installer-output\chat-concurrency-regression-full.log."
}

$SpecifiedWindowsRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_specified_windows_r34.py", "-v") `
    -LogPath $SpecifiedWindowsRegressionLog
if ($SpecifiedWindowsRegressionExitCode -ne 0) {
    throw "Verify specified-window affinity, recovery generation and duplicate requests failed (exit code: $SpecifiedWindowsRegressionExitCode). See installer-output\specified-windows-regression-full.log."
}

$ZeroPostRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_zero_posts_*r37.py", "-v") `
    -LogPath $ZeroPostRegressionLog
if ($ZeroPostRegressionExitCode -ne 0) {
    throw "Verify public zero-post and private profile reads, recovery and candidate progression failed (exit code: $ZeroPostRegressionExitCode). See installer-output\zero-post-regression-full.log."
}

$MonitorPartialRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_monitor_partial_r37.py", "-v") `
    -LogPath $MonitorPartialRegressionLog
if ($MonitorPartialRegressionExitCode -ne 0) {
    throw "Verify partial following observations are saved without false unfollows failed (exit code: $MonitorPartialRegressionExitCode). See installer-output\monitor-partial-regression-full.log."
}

$DiscardLimitsRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_count_ceiling_r37.py", "-v") `
    -LogPath $DiscardLimitsRegressionLog
if ($DiscardLimitsRegressionExitCode -ne 0) {
    throw "Verify independent public/private discard limits, settings persistence and candidate progression failed (exit code: $DiscardLimitsRegressionExitCode). See installer-output\discard-limits-regression-full.log."
}

$DiscardWorkflowRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r38.py", "-v") `
    -LogPath $DiscardWorkflowRegressionLog
if ($DiscardWorkflowRegressionExitCode -ne 0) {
    throw "Verify direct-discard-only settings, public post activity and single-stage private review failed (exit code: $DiscardWorkflowRegressionExitCode). See installer-output\discard-workflow-r38-regression-full.log."
}

$PreopenDedupeRegressionExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r39.py", "-v") `
    -LogPath $PreopenDedupeRegressionLog
if ($PreopenDedupeRegressionExitCode -ne 0) {
    throw "Verify durable discard deduplication and duplicate rejection before profile navigation failed (exit code: $PreopenDedupeRegressionExitCode). See installer-output\preopen-dedupe-r39-regression-full.log."
}

& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_follow_monitor.py" -v
Assert-LastExitCode "Verify recommendation filtering, monitor controls, window release and observation history"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio.py" -v
Assert-LastExitCode "Verify posting, nurture, task controls, owner isolation and window release"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_continuation.py" -v
Assert-LastExitCode "Verify complete reports, batch creation and restored global deduplication"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio_auto_media.py" -v
Assert-LastExitCode "Automatic multi-image and optional AI tests"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_posting_workflow.py" -v
Assert-LastExitCode "Verify posting retry, selection, prepared media and lease safety"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_publisher_submission.py" -v
Assert-LastExitCode "Verify publishing fence, caption check and uncertain result handling"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_publisher_entry.py" -v
Assert-LastExitCode "Verify fresh posting homepage, tab cleanup, Chinese create entry and local file upload"
if ($BrowserMode -eq "bundled") { Remove-Item Env:IGAC_POSTING_TEST_BROWSER -ErrorAction SilentlyContinue }
$env:IGAC_REQUIRE_POSTING_BROWSER = "1"
# Required real-browser final-source child-pool retirement gate.
$env:IGAC_REQUIRE_FINAL_SEED_BROWSER = "1"
$env:IGAC_FINAL_SEED_FIXTURE_ARTIFACT_DIR = (Join-Path $InstallerOutput "final-seed-fixtures")
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_final_seed_browser_r62.py" --case-timeout 90 -v
Assert-LastExitCode "Verify R6.2 final-source native pool cleanup"
# Required R6.4 crop-icon compatibility and immediate guard regressions.
foreach ($CropR64Pattern in @("test_crop_icon_r64.py", "test_crop_guard_r64.py")) {
    & .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p $CropR64Pattern -v
    Assert-LastExitCode "Verify R6.4 posting crop/icon regression $CropR64Pattern"
}
# R6.2 crop and measured-viewport regressions require the real selected browser.
foreach ($PostingR62Pattern in @("test_posting_crop_transition_r62.py", "test_posting_editing_state_r62.py", "test_original_crop_readiness_r62.py", "test_posting_viewport_labels.py", "test_posting_viewport_lifecycle_r62.py")) {
    & .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p $PostingR62Pattern -v
    Assert-LastExitCode "Verify R6.2 posting crop/viewport regression $PostingR62Pattern"
}
$CollectionR51ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r51.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "collection-manual-r51-full.log")
if ($CollectionR51ExitCode -ne 0) {
    throw "Verify relation DOM selection and confirmed manual control failed (exit code: $CollectionR51ExitCode). See installer-output\collection-manual-r51-full.log."
}
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_posting_dom.py" -v
Assert-LastExitCode "Verify real Chromium icon-only posting and file chooser fixtures"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio_cleanup_ui.py" -v
Assert-LastExitCode "Verify real React media deletion feedback and stale refresh protection"
$NurtureArchiveExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_nurture_delete*r41.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "nurture-archive-r41-full.log")
if ($NurtureArchiveExitCode -ne 0) {
    throw "Verify failed nurture removal, history and React controls failed (exit code: $NurtureArchiveExitCode). See installer-output\nurture-archive-r41-full.log."
}

& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_location_popup.py" -v
Assert-LastExitCode "Verify test_location_popup controlled browser regressions"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_instagram_home.py" -v
Assert-LastExitCode "Verify test_instagram_home controlled browser regressions"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_nurture_flow.py" -v
Assert-LastExitCode "Verify test_nurture_flow controlled browser regressions"
Remove-Item Env:IGAC_REQUIRE_POSTING_BROWSER -ErrorAction SilentlyContinue
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio_drafts.py" -v
Assert-LastExitCode "Verify test_studio_drafts state protection"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio_history.py" -v
Assert-LastExitCode "Verify test_studio_history state protection"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_posting_account_stats.py" -v
Assert-LastExitCode "Verify test_posting_account_stats state protection"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_studio_cleanup.py" -v
Assert-LastExitCode "Verify published media cleanup, active task protection and history retention"

& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_desktop_materials.py" -v
Assert-LastExitCode "Verify pre-start desktop downloads, retries, isolation and material paging"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_workspace.py" -v
Assert-LastExitCode "Verify account workspace, lock protection and ownership"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_surface.py" -v
Assert-LastExitCode "Verify embedded account window ownership and task locking"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_profile_preview.py" -v
Assert-LastExitCode "Verify target profile session ownership, task locks and load failures"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_embedded_browser.py" -v
Assert-LastExitCode "Verify embedded session generations, startup failures and task ownership"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_navigation.py" -v
Assert-LastExitCode "Verify account error-page recovery and operation lease release"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_collection_stability.py" -v
Assert-LastExitCode "Verify collection guard, scroll, pause and recovery stability"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_parallel_relation_pipeline.py" -v
Assert-LastExitCode "Verify candidate-page isolation, source continuation and durable resume"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_window_reuse_r93.py" -v
Assert-LastExitCode "Verify fixed child slots, bounded hover dispatch, discard totals and resume"
$HoverQueueR72ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_*r72.py", "-v") `
    -LogPath $HoverQueueR72Log
if ($HoverQueueR72ExitCode -ne 0) {
    throw "Verify unread-profile retry and split-queue ownership failed (exit code: $HoverQueueR72ExitCode). See installer-output\hover-queue-r72-full.log."
}
$ZeroSwitchR84ExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Join-Path $Root ".venv\Scripts\python.exe") `
    -ArgumentList @("-X", "utf8", "scripts\run_backend_tests.py", "-p", "test_zero_switch_r84.py", "-v") `
    -LogPath (Join-Path $InstallerOutput "zero-switch-r84-full.log")
if ($ZeroSwitchR84ExitCode -ne 0) {
    throw "Verify public/private zero-post switch and hover queue failed (exit code: $ZeroSwitchR84ExitCode). See installer-output\zero-switch-r84-full.log."
}
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_hover*_r6*.py" -v
Assert-LastExitCode "Verify Instagram hover card counts, row identity and saved exclusion bounds"
& .venv\Scripts\python.exe -X utf8 scripts\tests\test_diagnose_unread_profiles.py -v
Assert-LastExitCode "Verify read-only historical unread-profile diagnosis"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_candidate_spool.py" -v
Assert-LastExitCode "Verify persistent candidate spool ownership and retry"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_split_delayed_dispatch.py" -v
Assert-LastExitCode "Verify unique split claim and delayed queue dispatch"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_relation_longrun.py" -v
Assert-LastExitCode "Verify long-running relationship scroll and hover progress"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_parallel_screening_worker.py" -v
Assert-LastExitCode "Verify independent child-window screening and release"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_profile_stall_recovery.py" -v
Assert-LastExitCode "Verify one fresh page, retained old page and explicit retry"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_platforms.py" -v
Assert-LastExitCode "Verify platform login, Cookie import privacy and window task restrictions"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_native_cloud.py" -v
Assert-LastExitCode "Verify native windows, cloud backups and owner isolation"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_restore.py" -v
Assert-LastExitCode "Verify account controls and separate task windows"
& .venv\Scripts\python.exe -X utf8 scripts\run_backend_tests.py -p "test_account_updates.py" -v
Assert-LastExitCode "Verify unread messages, Cookie login and account ordering"

& .venv\Scripts\python.exe scripts\verify_backend_smoke.py
Assert-LastExitCode "Verify Python backend smoke checks"

$NativeManifest = Join-Path $Root "build\openvino-native-manifest.json"
& .venv\Scripts\python.exe scripts\verify_openvino_windows.py source --manifest $NativeManifest
Assert-LastExitCode "Verify installed OpenVINO native runtime and real CPU inference"

$OldFrozenDist = Join-Path $Root "dist\collector_core"
$OldFrozenBuild = Join-Path $Root "build\collector_core"
if (Test-Path -LiteralPath $OldFrozenDist) {
    Remove-Item -LiteralPath $OldFrozenDist -Recurse -Force
}
if (Test-Path -LiteralPath $OldFrozenBuild) {
    Remove-Item -LiteralPath $OldFrozenBuild -Recurse -Force
}

$PersonModelAssets = Join-Path $Root "backend\app\assets\person_classifier"
$SystemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
$MsvcRuntimeNames = @("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll")
$PyInstallerArguments = @(
    "--noconfirm",
    "--clean",
    "--noupx",
    "--onedir",
    "--name", "collector_core",
    "--paths", "backend",
    "--collect-all", "openvino",
    "--collect-all", "playwright",
    "--collect-all", "tzdata",
    "--hidden-import", "websockets.sync.client",
    "--runtime-hook", "scripts\pyi_rth_openvino.py",
    "--runtime-hook", "scripts\download_person_models.py",
    "--add-data", "$PersonModelAssets;app\assets\person_classifier",
    # The earliest runtime hook validates this exact build-generated manifest
    # before it stages or loads any OpenVINO native DLL.  Never let the frozen
    # process fall back to an unsealed directory scan.
    "--add-data", "$NativeManifest;.",
    "--distpath", "dist",
    "--workpath", "build"
)
foreach ($RuntimeName in $MsvcRuntimeNames) {
    $RuntimePath = Join-Path $SystemDirectory $RuntimeName
    if (-not (Test-Path -LiteralPath $RuntimePath -PathType Leaf)) {
        throw "Package Python local service failed: required Microsoft runtime file was not found: $RuntimePath"
    }
    $RuntimeSignature = Get-AuthenticodeSignature -FilePath $RuntimePath
    if ($RuntimeSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $RuntimeSignature.SignerCertificate -or
        $RuntimeSignature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
        throw "Package Python local service failed: Microsoft runtime signature is invalid: $RuntimePath"
    }
    $PyInstallerArguments += @("--add-binary", "$RuntimePath;.")
}
$PyInstallerArguments += "backend\app\__main__.py"
$PyInstallerExecutable = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath $PyInstallerExecutable `
    -ArgumentList $PyInstallerArguments `
    -LogPath $PyInstallerLog
if ($PyInstallerExitCode -ne 0) {
    throw "Package Python local service failed (exit code: $PyInstallerExitCode); see $PyInstallerLog"
}
$FrozenCore = Join-Path $Root "dist\collector_core\collector_core.exe"
if (-not (Test-Path -LiteralPath $FrozenCore -PathType Leaf)) {
    throw "Package Python local service failed: collector_core.exe was not created"
}
$FrozenNativeManifest = Join-Path (Split-Path -Parent $FrozenCore) "_internal\openvino-native-manifest.json"
if (-not (Test-Path -LiteralPath $FrozenNativeManifest -PathType Leaf)) {
    throw "Package Python local service failed: the sealed OpenVINO native manifest was not packaged"
}
$SourceNativeManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $NativeManifest).Hash
$FrozenNativeManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $FrozenNativeManifest).Hash
if ($SourceNativeManifestHash -ne $FrozenNativeManifestHash) {
    throw "Package Python local service failed: the packaged OpenVINO native manifest differs from the verified source manifest"
}
& .venv\Scripts\python.exe scripts\verify_openvino_windows.py frozen --manifest $NativeManifest --dist (Split-Path -Parent $FrozenCore)
Assert-LastExitCode "Verify frozen OpenVINO DLL layout"

# Execute the actual frozen service with a build-only runtime-hook flag. The
# hook verifies the packaged copies and compiles both models through the
# packaged OpenVINO Runtime, then exits before the HTTP service starts.
& "$PSScriptRoot\test_frozen_openvino.ps1" -Executable $FrozenCore -LogPath $FrozenSmokeLog -TimeoutSeconds 180

# Model-only runtime hooks exit before the actual HTTP application imports.
# Exercise the frozen application itself before either release packager runs.
& .venv\Scripts\python.exe -I -X utf8 scripts\verify_frozen_core_service.py --executable $FrozenCore --log (Join-Path $InstallerOutput "frozen-core-service.log")
Assert-LastExitCode "Verify frozen Core startup, authentication, SQLite schema and orderly shutdown"

# Recheck the selected Chrome after the long test run, before packaging.
# A disappearing/updating executable cannot leave a misleading success marker.
if ($BrowserMode -eq "installed-chrome") {
    & .venv\Scripts\python.exe -X utf8 scripts\browser_build_policy.py --candidate-output $ChromeCandidatePath
    Assert-LastExitCode "Recheck installed Chrome before packaging"
    $FinalChromeCandidate = [IO.File]::ReadAllText($ChromeCandidatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($FinalChromeCandidate.executable -ne $ChromeCandidate.executable -or $FinalChromeCandidate.version -ne $SealedBrowserRequirement.minimum_version) {
        throw "Chrome changed during verification. Rerun after its update finishes."
    }
}
$ElectronBuilder = Join-Path $Root "node_modules\.bin\electron-builder.cmd"
$InstallerSucceeded = $false
$TrustedOutputKind = ""
$TrustedOutputPath = ""

if ($PortableOnly) {
    "Installer build skipped because PortableOnly was selected." | Set-Content -Path $BuilderLog -Encoding UTF8
} elseif (Test-Path -LiteralPath $ElectronBuilder -PathType Leaf) {
    Write-Host "Building the Windows installer..." -ForegroundColor Cyan
    $BuilderExitCode = Invoke-IgacNativeCommandWithLog `
        -FilePath $ElectronBuilder `
        -ArgumentList @("--win", "nsis", "--x64", "--publish", "never") `
        -LogPath $BuilderLog
    if ($BuilderExitCode -eq 0) {
        $Installer = Get-Item -LiteralPath $InstallerPath -ErrorAction SilentlyContinue
        if (
            $Installer -and
            $Installer.Length -gt 0 -and
            $Installer.LastWriteTimeUtc -ge $ReleaseBuildStartedAt
        ) {
            $InstallerSucceeded = $true
            $TrustedOutputKind = "INSTALLER"
            $TrustedOutputPath = $Installer.FullName
            Write-Host "Installer created: $($Installer.FullName)" -ForegroundColor Green
        }
    }
} else {
    "electron-builder.cmd was not found after npm install." | Set-Content -Path $BuilderLog -Encoding UTF8
}

if ($PortableOnly -or -not $InstallerSucceeded) {
    # electron-builder may have left an empty or partial current-version file
    # even when it failed or when its exit code was misleading.  Never leave
    # that untrusted Setup beside a successful portable fallback.
    foreach ($UntrustedInstaller in @(
        $InstallerPath,
        "$InstallerPath.blockmap",
        $InstallerHashPath,
        $InstallerHashTemporary
    )) {
        if (Test-Path -LiteralPath $UntrustedInstaller) {
            Remove-Item -LiteralPath $UntrustedInstaller -Force
        }
    }
    if (-not $PortableOnly) {
        Write-Warning "The NSIS installer could not be created on this PC. The full error is saved to:"
        Write-Warning $BuilderLog
    }
    Write-Host "Creating a direct-run portable APP instead..." -ForegroundColor Yellow
    & "$PSScriptRoot\build_portable_windows.ps1" -PythonExecutable (Join-Path $Root ".venv\Scripts\python.exe")
    $Portable = Get-Item -LiteralPath $PortablePath -ErrorAction SilentlyContinue
    if (
        -not $Portable -or
        $Portable.Length -le 0 -or
        $Portable.LastWriteTimeUtc -lt $ReleaseBuildStartedAt
    ) {
        throw "Build portable Windows application failed: a new portable ZIP was not created by this build"
    }
    $TrustedOutputKind = "PORTABLE"
    $TrustedOutputPath = $Portable.FullName
}

if ([string]::IsNullOrWhiteSpace($TrustedOutputKind) -or [string]::IsNullOrWhiteSpace($TrustedOutputPath)) {
    throw "Build completed without selecting one trusted current-run output"
}
$TrustedOutputHash = ""
$TrustedOutputHashPath = ""
if ($TrustedOutputKind -eq "INSTALLER") {
    # Hash only the single exact current-version Setup.  Older Setup files may
    # remain in installer-output for operator reference and must never be
    # selected by a broad wildcard or recorded as this run's deliverable.
    $CurrentVersionSetupPattern = "Juxin-IG-Audience-Collector-NewGen-Setup-$Version-*.exe"
    $CurrentVersionSetups = @(
        Get-ChildItem -LiteralPath $InstallerOutput -File |
            Where-Object { $_.Name -like $CurrentVersionSetupPattern }
    )
    if (
        $CurrentVersionSetups.Count -ne 1 -or
        $CurrentVersionSetups[0].FullName -ne ([IO.Path]::GetFullPath($InstallerPath))
    ) {
        throw "Build completed without exactly one expected current-version Setup"
    }
    $TrustedOutputHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
    if ($TrustedOutputHash -notmatch '^[0-9a-f]{64}$') {
        throw "Build completed but the final Setup SHA-256 is invalid"
    }
    $TrustedOutputHashPath = $InstallerHashPath
    $TrustedHashRecord = "$TrustedOutputHash *$([IO.Path]::GetFileName($InstallerPath))"
    [IO.File]::WriteAllText(
        $InstallerHashTemporary,
        $TrustedHashRecord + [Environment]::NewLine,
        [Text.Encoding]::ASCII
    )
    Move-Item -LiteralPath $InstallerHashTemporary -Destination $InstallerHashPath -Force
    $RecordedHash = [IO.File]::ReadAllText($InstallerHashPath, [Text.Encoding]::ASCII).Trim()
    if ($RecordedHash -ne $TrustedHashRecord) {
        throw "Build completed but the final Setup SHA-256 record could not be verified"
    }
}
$BuildResultLines = @(
    "TYPE=$TrustedOutputKind",
    "VERSION=$Version",
    "BROWSER_MODE=$BrowserMode",
    "CHROME_MINIMUM=$($SealedBrowserRequirement.minimum_version)",
    "PATH=$TrustedOutputPath"
)
if ($TrustedOutputKind -eq "INSTALLER") {
    $BuildResultLines += @(
        "SHA256=$TrustedOutputHash",
        "SHA256_PATH=$TrustedOutputHashPath"
    )
}
$BuildResult = $BuildResultLines -join [Environment]::NewLine
[IO.File]::WriteAllText(
    $BuildResultTemporary,
    $BuildResult + [Environment]::NewLine,
    [Text.Encoding]::UTF8
)
Move-Item -LiteralPath $BuildResultTemporary -Destination $BuildResultMarker -Force
Write-Host "Trusted current-run output: $TrustedOutputPath" -ForegroundColor Green
Write-Host "The exact path is recorded in: $BuildResultMarker" -ForegroundColor Green
} catch {
    # Preserve the original failure, even if the disk cannot accept diagnostics.
    $BuildError = $_
    try {
        $FailureDirectory = Join-Path $Root "installer-output"
        [IO.Directory]::CreateDirectory($FailureDirectory) | Out-Null
        $FailurePath = Join-Path $FailureDirectory "BUILD_FAILURE.txt"
        $FailureDetails = @(
            "BUILD_FAILED_UTC=$([DateTime]::UtcNow.ToString('o'))",
            "PATCH=R94_BATCH_COLLECTION_FIX_5",
            $BuildError.Exception.ToString(),
            $BuildError.InvocationInfo.PositionMessage,
            $BuildError.ScriptStackTrace
        ) -join [Environment]::NewLine
        [IO.File]::WriteAllText($FailurePath, $FailureDetails, [Text.Encoding]::UTF8)
        Write-Host "Failure details: $FailurePath" -ForegroundColor Yellow
    } catch {
        Write-Warning "Could not save failure details: $($_.Exception.Message)"
    }
    throw $BuildError
} finally {
    Exit-IgacBuildMutex $IgacBuildMutex
}
