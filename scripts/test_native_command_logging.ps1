$ErrorActionPreference = "Stop"
$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$ScriptDirectory\invoke_native_logged.ps1"

$UnicodeSegment = -join @([char]0x539F, [char]0x751F, [char]0x547D, [char]0x4EE4)
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("JuxinIGAC $UnicodeSegment native log test " + [Guid]::NewGuid().ToString("N"))
$SuccessCommand = Join-Path $TestRoot "success.cmd"
$FailureCommand = Join-Path $TestRoot "failure.cmd"
$SuccessLog = Join-Path $TestRoot "success.log"
$FailureLog = Join-Path $TestRoot "failure.log"
$OriginalErrorActionPreference = $ErrorActionPreference
$HasNativeErrorPreference = Test-Path Variable:\PSNativeCommandUseErrorActionPreference
$OriginalNativeErrorPreference = $null
if ($HasNativeErrorPreference) {
    $OriginalNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
}

function Assert-IgacLoggingPreferencesRestored([string]$Phase) {
    if ($ErrorActionPreference -ne $OriginalErrorActionPreference) {
        throw "Native logging probe did not restore ErrorActionPreference after $Phase"
    }
    if ($HasNativeErrorPreference -and
        $PSNativeCommandUseErrorActionPreference -ne $OriginalNativeErrorPreference) {
        throw "Native logging probe did not restore PSNativeCommandUseErrorActionPreference after $Phase"
    }
}

try {
    New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
    [IO.File]::WriteAllText(
        $SuccessCommand,
        "@echo off`r`necho IGAC_STDOUT_OK`r`necho IGAC_STDERR_INFO 1>&2`r`nexit /b 0`r`n",
        [Text.Encoding]::ASCII
    )
    [IO.File]::WriteAllText(
        $FailureCommand,
        "@echo off`r`necho IGAC_STDERR_FAILURE 1>&2`r`nexit /b 23`r`n",
        [Text.Encoding]::ASCII
    )

    $global:LASTEXITCODE = 91
    $SuccessExitCode = Invoke-IgacNativeCommandWithLog -FilePath $SuccessCommand -LogPath $SuccessLog
    if ($SuccessExitCode -isnot [int] -or
        $SuccessExitCode -ne 0 -or
        $global:LASTEXITCODE -ne 0) {
        throw "Native logging success probe returned an invalid exit code: $SuccessExitCode"
    }
    Assert-IgacLoggingPreferencesRestored "the success path"
    $SuccessText = [IO.File]::ReadAllText($SuccessLog)
    if ($SuccessText -notmatch "IGAC_STDOUT_OK" -or $SuccessText -notmatch "IGAC_STDERR_INFO") {
        throw "Native logging success probe did not preserve stdout and informational stderr"
    }

    $global:LASTEXITCODE = 91
    $FailureExitCode = Invoke-IgacNativeCommandWithLog -FilePath $FailureCommand -LogPath $FailureLog
    if ($FailureExitCode -isnot [int] -or
        $FailureExitCode -ne 23 -or
        $global:LASTEXITCODE -ne 23) {
        throw "Native logging failure probe did not preserve exit code 23: $FailureExitCode"
    }
    Assert-IgacLoggingPreferencesRestored "the nonzero-exit path"
    if ([IO.File]::ReadAllText($FailureLog) -notmatch "IGAC_STDERR_FAILURE") {
        throw "Native logging failure probe did not preserve stderr"
    }

    $LogWriteFailureWasCaught = $false
    try {
        [void](Invoke-IgacNativeCommandWithLog -FilePath $SuccessCommand -LogPath $TestRoot)
    } catch {
        $LogWriteFailureWasCaught = $true
    }
    if (-not $LogWriteFailureWasCaught) {
        throw "Native logging probe accepted an unwritable log target"
    }
    Assert-IgacLoggingPreferencesRestored "the log-write failure path"

    Write-Host "Windows PowerShell native stdout/stderr logging probe passed." -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $TestRoot) {
        Remove-Item -LiteralPath $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
