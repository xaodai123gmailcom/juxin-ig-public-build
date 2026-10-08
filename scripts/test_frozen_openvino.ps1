param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [ValidateRange(10, 600)][int]$TimeoutSeconds = 180,
    [switch]$RequireNonAsciiSource,
    [string]$ExpectedCacheRoot = "",
    [scriptblock]$DiagnosticObserver = $null
)

$ErrorActionPreference = "Stop"
$ResolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$LogDirectory = Split-Path -Parent $LogPath
if (-not [string]::IsNullOrWhiteSpace($LogDirectory)) {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
}

$Nonce = [Guid]::NewGuid().ToString("N")
$SuccessMarker = "IGAC_FROZEN_OPENVINO_OK:$Nonce`:openvino=2025.4.1"
$AsciiSuccessMarkerPrefix = "IGAC_FROZEN_OPENVINO_ASCII_OK:$Nonce`:openvino_dll="
$SourceSuccessMarkerPrefix = "IGAC_FROZEN_OPENVINO_SOURCE_B64_OK:$Nonce`:source_openvino_dll_utf8_b64="
$WorkingDirectory = Join-Path ([IO.Path]::GetTempPath()) "JuxinIGAC-FrozenSmoke-$Nonce"
$SystemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
$WindowsDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
New-Item -ItemType Directory -Path $WorkingDirectory -Force | Out-Null
$ResolvedExpectedCacheRoot = ""
if (-not [string]::IsNullOrWhiteSpace($ExpectedCacheRoot)) {
    New-Item -ItemType Directory -Path $ExpectedCacheRoot -Force | Out-Null
    $ResolvedExpectedCacheRoot = (Resolve-Path -LiteralPath $ExpectedCacheRoot).Path
    foreach ($Character in $ResolvedExpectedCacheRoot.ToCharArray()) {
        if ([int][char]$Character -gt 127) {
            throw "Expected OpenVINO cache root must be an ASCII path: $ResolvedExpectedCacheRoot"
        }
    }
    if ($ResolvedExpectedCacheRoot -notmatch '^[A-Za-z]:\\') {
        throw "Expected OpenVINO cache root must be an absolute drive path: $ResolvedExpectedCacheRoot"
    }
}

. "$PSScriptRoot\owned_process.ps1"
$EnvironmentChanges = @{
    IGAC_BUILD_VERIFY_FROZEN_PERSON_MODELS = $Nonce
    PYTHONUTF8 = "1"
    PYTHONIOENCODING = "utf-8:backslashreplace"
    PYTHONHOME = $null
    PYTHONPATH = $null
    OPENVINO_LIB_PATHS = $null
    PATH = "$SystemDirectory;$WindowsDirectory;$SystemDirectory\Wbem"
    IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE = $null
    IGAC_OPENVINO_CACHE_ROOT = $null
}
if ($RequireNonAsciiSource) { $EnvironmentChanges.IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE = "1" }
if (-not [string]::IsNullOrWhiteSpace($ResolvedExpectedCacheRoot)) { $EnvironmentChanges.IGAC_OPENVINO_CACHE_ROOT = $ResolvedExpectedCacheRoot }
$StandardOutput = ""
$StandardError = ""
$ExitCode = -1
$PrimaryFailure = $null
$CleanupFailures = @()
$StdoutPath = [IO.Path]::GetFullPath($LogPath) + ".stdout"
$StderrPath = [IO.Path]::GetFullPath($LogPath) + ".stderr"
try {
    $Receipt = Invoke-IgacOwnedProcess -DiagnosticObserver $DiagnosticObserver -Request @{
        executable = $ResolvedExecutable
        arguments = @()
        workingDirectory = $WorkingDirectory
        stdoutPath = $StdoutPath
        stderrPath = $StderrPath
        environment = $EnvironmentChanges
        timeoutSeconds = $TimeoutSeconds
        budgetLabel = "Frozen OpenVINO existing execution deadline"
        drainSeconds = 10
        terminationSeconds = 10
    }
    $ExitCode = [int]$Receipt.targetExitCode
} catch {
    $PrimaryFailure = $_
} finally {
    # Preserve partial output and the original failure. These bounded file reads
    # cannot wait forever for a descendant retaining a stdout/stderr pipe.
    try { if (Test-Path -LiteralPath $StdoutPath) { $StandardOutput = Read-IgacOwnedLog -Path $StdoutPath } } catch { $CleanupFailures += "stdout read: $($_.Exception.Message)" }
    try { if (Test-Path -LiteralPath $StderrPath) { $StandardError = Read-IgacOwnedLog -Path $StderrPath } } catch { $CleanupFailures += "stderr read: $($_.Exception.Message)" }
    try {
        $LogText = "STDOUT:`r`n$StandardOutput`r`nSTDERR:`r`n$StandardError"
        if ($null -ne $PrimaryFailure) { $LogText += "`r`nSUPERVISION FAILURE: $($PrimaryFailure.Exception.Message)" }
        if ($CleanupFailures.Count -gt 0) { $LogText += "`r`nOUTPUT ERRORS: " + ($CleanupFailures -join '; ') }
        [IO.File]::WriteAllText($LogPath, $LogText, [Text.Encoding]::UTF8)
    } catch { $CleanupFailures += "combined log write: $($_.Exception.Message)" }
    if (Test-Path -LiteralPath $WorkingDirectory) {
        try { Remove-Item -LiteralPath $WorkingDirectory -Recurse -Force -ErrorAction Stop } catch { $CleanupFailures += "working directory cleanup: $($_.Exception.Message)" }
    }
}
if ($null -ne $PrimaryFailure) {
    if ($CleanupFailures.Count -gt 0) { Write-Warning ("Frozen smoke cleanup also failed: " + ($CleanupFailures -join '; ')) }
    throw $PrimaryFailure
}
if ($CleanupFailures.Count -gt 0) { throw ("Frozen smoke output/cleanup failed: " + ($CleanupFailures -join '; ')) }

if (-not [string]::IsNullOrWhiteSpace($StandardOutput)) {
    Write-Host $StandardOutput
}
if (-not [string]::IsNullOrWhiteSpace($StandardError)) {
    Write-Host $StandardError -ForegroundColor Red
}
if ($ExitCode -ne 0 -or $StandardOutput -notmatch [regex]::Escape($SuccessMarker)) {
    throw "Frozen local person-recognition smoke failed (exit code: $ExitCode; model success marker missing); see $LogPath"
}

$AsciiMarkerPattern = "(?m)^" + [regex]::Escape($AsciiSuccessMarkerPrefix) + "(?<Path>[^`r`n]+)`r?$"
$AsciiMarkerMatch = [regex]::Match($StandardOutput, $AsciiMarkerPattern)
if (-not $AsciiMarkerMatch.Success) {
    throw "Frozen local person-recognition smoke failed (ASCII native bootstrap marker missing); see $LogPath"
}
$LoadedOpenVinoDll = $AsciiMarkerMatch.Groups["Path"].Value
$HasNonAsciiCharacter = $false
foreach ($Character in $LoadedOpenVinoDll.ToCharArray()) {
    if ([int][char]$Character -gt 127) {
        $HasNonAsciiCharacter = $true
        break
    }
}
if (
    $HasNonAsciiCharacter -or
    $LoadedOpenVinoDll -notmatch '^[A-Za-z]:\\' -or
    -not $LoadedOpenVinoDll.EndsWith("\openvino.dll", [StringComparison]::OrdinalIgnoreCase)
) {
    throw "Frozen local person-recognition smoke failed (loaded openvino.dll was not reported from an absolute ASCII path); see $LogPath"
}
if (-not [string]::IsNullOrWhiteSpace($ResolvedExpectedCacheRoot)) {
    $ExpectedCachePrefix = $ResolvedExpectedCacheRoot.TrimEnd("\") + "\"
    $LoadedOpenVinoDllFull = [IO.Path]::GetFullPath($LoadedOpenVinoDll)
    if (-not $LoadedOpenVinoDllFull.StartsWith($ExpectedCachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Frozen local person-recognition smoke reused a cache outside the fresh expected root; see $LogPath"
    }
}

if ($RequireNonAsciiSource) {
    $SourceMarkerPattern = "(?m)^" + [regex]::Escape($SourceSuccessMarkerPrefix) + "(?<Path>[^`r`n]+)`r?$"
    $SourceMarkerMatch = [regex]::Match($StandardOutput, $SourceMarkerPattern)
    if (-not $SourceMarkerMatch.Success) {
        throw "Frozen local person-recognition smoke failed (non-ASCII packaged source marker missing); see $LogPath"
    }
    try {
        $SourceOpenVinoDll = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($SourceMarkerMatch.Groups["Path"].Value)
        )
    } catch {
        throw "Frozen local person-recognition smoke failed (non-ASCII packaged source marker was not valid UTF-8 Base64); see $LogPath"
    }
    $SourceHasNonAsciiCharacter = $false
    foreach ($Character in $SourceOpenVinoDll.ToCharArray()) {
        if ([int][char]$Character -gt 127) {
            $SourceHasNonAsciiCharacter = $true
            break
        }
    }
    if (
        -not $SourceHasNonAsciiCharacter -or
        $SourceOpenVinoDll -notmatch '^[A-Za-z]:\\' -or
        -not $SourceOpenVinoDll.EndsWith("\openvino.dll", [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Frozen local person-recognition smoke failed (packaged openvino.dll source was not an absolute non-ASCII path); see $LogPath"
    }
}

Write-Host "Frozen local person-recognition smoke passed in a clean process; verified ASCII native DLL: $LoadedOpenVinoDll" -ForegroundColor Green
