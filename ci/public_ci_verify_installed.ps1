$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\scripts\invoke_native_logged.ps1"
$name = 'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe'
$installer = (Resolve-Path -LiteralPath (Join-Path 'installer-output' $name)).Path
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\juxin-ig-audience-collector-newgen'
if (Test-Path -LiteralPath $installRoot) { throw 'Install verification requires a fresh destination' }
$InstallExitCode = Invoke-IgacNativeCommandWithLog -FilePath $installer -ArgumentList @('/S') -LogPath 'installer-output\installed-nsis.log' -TimeoutSeconds 180
if ($InstallExitCode -ne 0) { throw 'Final NSIS installation failed' }
$core = Join-Path $installRoot 'resources\backend\collector_core'
$exe = Join-Path $core 'collector_core.exe'
if (!(Test-Path -LiteralPath $exe)) { throw 'Installed Core executable is missing' }
$policyPath = Join-Path $installRoot 'resources\browsers\juxin-runtime-requirement.json'
$policy = [IO.File]::ReadAllText($policyPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
if ($policy.mode -ne 'installed-chrome-required') { throw 'Installed browser requirement is missing or incorrect' }
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python scripts\verify_openvino_windows.py frozen --manifest build\openvino-native-manifest.json --dist $core
if ($LASTEXITCODE -ne 0) { throw 'Installed native model files failed verification' }
& .\scripts\test_frozen_openvino.ps1 -Executable $exe -LogPath installer-output\installed-openvino-smoke.log -TimeoutSeconds 180
& $python -I -X utf8 scripts\verify_frozen_core_service.py --executable $exe --log installer-output\installed-core-service-smoke.log --pure-ig --snapshot-scale --collection-completion --standalone-nurture --nurture-cleanup-upgrade --report installer-output\installed-scale-verification.json
if ($LASTEXITCODE -ne 0) { throw 'Installed Core service failed verification' }
$scale = [IO.File]::ReadAllText((Join-Path $PWD 'installer-output\installed-scale-verification.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
if (!$scale.nurture_cleanup_upgrade.verified) { throw 'Installed pre-existing nurture cleanup database upgrade proof is missing' }
if (!$scale.snapshot_scale.verified -or $scale.snapshot_scale.collected -ne 441552 -or $scale.snapshot_scale.identities -ne 602831) { throw 'Installed high-volume snapshot check is missing or incorrect' }
if (!$scale.snapshot_scale.legacy_platform_counter_upgrade.verified -or !$scale.snapshot_scale.legacy_platform_counter_upgrade.records_preserved) { throw 'Installed legacy platform counter upgrade check is missing' }
if ($null -eq $scale.snapshot_scale.platform_snapshot_seconds.instagram) { throw 'Installed platform-scoped snapshots were not verified' }
if (!$scale.snapshot_scale.work_report_summary.verified) { throw 'Installed work-report summary equality proof is missing' }
$removed = $scale.posting_removed
if (!$removed.verified -or $removed.http_status -ne 404 -or $removed.endpoints.Count -ne 3) { throw 'Installed removed-route rejection proof is missing' }
$removedPaths = @('/api/posting/snapshot', '/api/posting/command', '/api/internal/integrations/pexels')
$removedMethods = @('GET', 'POST', 'POST')
for ($i = 0; $i -lt 3; $i++) {
    if ($removed.endpoints[$i].path -cne $removedPaths[$i] -or $removed.endpoints[$i].method -cne $removedMethods[$i]) { throw 'Installed removed-route rejection target changed' }
}
if ($null -ne $scale.posting_workflow) { throw 'Installed report exposes retired workflow' }
if (!$scale.standalone_nurture.verified) { throw 'Installed standalone nurture verification is missing' }
if (!$scale.standalone_nurture.cases.verified_playback.verified -or !$scale.standalone_nurture.cases.completed_cleanup_fence.verified -or !$scale.standalone_nurture.cases.completed_history.canonical_singular_and_plural_routes -or !$scale.standalone_nurture.cases.legacy_plan_fence.legacy_wall_time_not_reinterpreted) { throw 'Installed R6.3 nurture playback and confirmed cleanup proof is missing' }
if (!$scale.collection_completion.single_gap_recheck.verified) { throw 'Installed one-pass gap recheck verification is missing' }
if (!$scale.collection_completion.manual_parent_recheck.verified) { throw 'Installed durable pending manual parent recheck verification is missing' }
if (!$scale.collection_completion.final_seed_completion.verified -or !$scale.collection_completion.final_seed_completion.authoritative_factory_reconnect) { throw 'Installed final-source reconnect and confirmed cleanup proof is missing' }
if (!$scale.collection_completion.verified) { throw 'Installed collection completion runtime verification is missing' }
if (!$scale.snapshot_scale.compact_wire.verified -or !$scale.snapshot_scale.compact_wire.canonical_rows_equal) { throw 'Installed compact snapshot equivalence is missing' }
if (!$scale.snapshot_scale.normal_restart.verified -or !$scale.snapshot_scale.normal_restart.retained_data -or $scale.snapshot_scale.performance_indexes_verified.Count -ne 3) { throw 'Installed normal restart or performance indexes check is missing' }
if (!$scale.snapshot_scale.pure_ig_upgrade.verified -or !$scale.snapshot_scale.pure_ig_upgrade.ig_identity_hash_preserved) { throw 'Installed pure IG deletion/IG preservation proof is missing' }
$dismissal = $scale.snapshot_scale.completed_card_dismissal
if (!$dismissal.verified -or !$dismissal.persistence_after_restart -or !$dismissal.retained_data -or !$dismissal.platform_isolation) { throw 'Installed completed-card deletion and restart check is missing' }
$upgrade = $scale.report_index_upgrade
if (!$upgrade.verified -or !$upgrade.legacy_index_preserved -or !$upgrade.target_period_index_created -or !$upgrade.inventory_dedup_hashes_preserved -or !$upgrade.window_leases_preserved -or !$upgrade.repeated_startup_idempotent -or $upgrade.restart_count -ne 2 -or !$upgrade.synthetic -or $upgrade.user_data_touched) { throw 'Installed R6.1 conflicting-index upgrade proof is missing or invalid' }
if ($upgrade.four_card_totals.collection -ne 3 -or $upgrade.four_card_totals.follow -ne 1 -or $upgrade.four_card_totals.split -ne 3 -or $upgrade.four_card_totals.added -ne 7) { throw 'Installed R6.1 four-card totals differ from the fixture' }
$native = [IO.File]::ReadAllText((Join-Path $PWD 'installer-output\embedded-browser-check.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
if (!$native.verified -or !$native.pythonWorker -or !$native.taskColdStart) { throw 'Native task-owned unopened-window startup proof is missing' }
# Actual installed desktop and its owned frozen Core must execute the R6.4
# recovery routes over pre-existing synthetic data. This is not a source selftest.
$package = [IO.File]::ReadAllText((Join-Path $PWD 'package.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
$installedAppExe = Join-Path $installRoot ($package.build.productName + '.exe')
if (!(Test-Path -LiteralPath $installedAppExe)) { throw 'Installed desktop executable is missing' }
$recoveryStdout = Join-Path $PWD 'installer-output\installed-recovery-r64-stdout-full.log'
$recoveryStderr = Join-Path $PWD 'installer-output\installed-recovery-r64-stderr-full.log'
$recoveryArguments = @('-I', '-X', 'utf8', 'scripts\verify_installed_recovery_r64.py', '--executable', $installedAppExe, '--core-executable', $exe, '--core-report', 'installer-output\installed-scale-verification.json', '--log', 'installer-output\installed-recovery-r64.log', '--report', 'installer-output\installed-recovery-r64.json', '--timeout', '120')
$RecoveryExitCode = Invoke-IgacNativeCommandWithLog -FilePath $python -ArgumentList $recoveryArguments -LogPath $recoveryStdout -TimeoutSeconds 180
if ($RecoveryExitCode -ne 0) { throw 'Actual installed desktop R6.4 API recovery proof failed' }
if (!(Select-String -LiteralPath $recoveryStdout -Pattern '^INSTALLED_RECOVERY_R64=PASS ' -Quiet)) { throw 'Actual installed recovery success marker is missing' }
$recovery = [IO.File]::ReadAllText((Join-Path $PWD 'installer-output\installed-recovery-r64.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
if (!$recovery.verified) { throw 'Actual installed R6.4 recovery receipt is missing' }
$record = @{
    verified = $true
    recovery_api = $recovery
    nurture_cleanup_upgrade = $scale.nurture_cleanup_upgrade
    report_index_upgrade = $upgrade
    installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    browser_mode = $policy.mode
    chrome_minimum = $policy.minimum_version
    installed_root = $installRoot
    native_task_cold_start_verified = $true
    core_service_verified = $true
    snapshot_scale_verified = $true
    snapshot_scale = $scale.snapshot_scale
    collection_completion = $scale.collection_completion
    standalone_nurture = $scale.standalone_nurture
    posting_removed = $removed
    pure_instagram_verified = $scale.pure_instagram
    removed_platform_inputs_rejected = $scale.removed_platform_inputs_rejected
    native_models_verified = $true
}
[IO.File]::WriteAllText((Join-Path $PWD 'installer-output\installed-verification.json'), ($record | ConvertTo-Json -Depth 10), [Text.Encoding]::UTF8)



