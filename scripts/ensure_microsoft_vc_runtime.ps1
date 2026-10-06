param([switch]$ForceRepair)

$ErrorActionPreference = "Stop"

if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw "Microsoft Visual C++ runtime validation requires 64-bit Windows PowerShell."
}

# OpenVINO's official Windows wheel links against these Visual C++ 2015-2022
# x64 runtime files.  A clean Windows installation may not have them yet.
$RequiredRuntimeFiles = @(
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll"
)
$SystemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
$MinimumRuntimeVersion = [Version]"14.44.35112.0"

function Get-RuntimeIssues {
    $Issues = @()
    foreach ($RuntimeName in $RequiredRuntimeFiles) {
        $RuntimePath = Join-Path $SystemDirectory $RuntimeName
        if (-not (Test-Path $RuntimePath -PathType Leaf)) {
            $Issues += "$RuntimeName is missing"
            continue
        }
        $Signature = Get-AuthenticodeSignature -FilePath $RuntimePath
        if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
            $null -eq $Signature.SignerCertificate -or
            $Signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
            $Issues += "$RuntimeName does not have a valid Microsoft Authenticode signature"
            continue
        }
        $VersionInfo = (Get-Item $RuntimePath).VersionInfo
        try {
            $RuntimeVersion = [Version](
                "$($VersionInfo.FileMajorPart).$($VersionInfo.FileMinorPart)." +
                "$($VersionInfo.FileBuildPart).$($VersionInfo.FilePrivatePart)"
            )
        } catch {
            $Issues += "$RuntimeName has an unreadable file version"
            continue
        }
        if ($RuntimeVersion -lt $MinimumRuntimeVersion) {
            $Issues += "$RuntimeName version $RuntimeVersion is older than $MinimumRuntimeVersion"
        }
    }
    return @($Issues)
}

$RuntimeIssues = @(Get-RuntimeIssues)
if ($RuntimeIssues.Count -eq 0 -and -not $ForceRepair) {
    Write-Host "Microsoft Visual C++ x64 runtime is available." -ForegroundColor Green
    return
}

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "Microsoft Visual C++ runtime is missing and LOCALAPPDATA is unavailable. Install https://aka.ms/vc14/vc_redist.x64.exe and rerun."
}

if ($RuntimeIssues.Count -gt 0) {
    Write-Host "Microsoft Visual C++ runtime requires installation or repair:" -ForegroundColor Yellow
    $RuntimeIssues | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}
Write-Host "Microsoft Visual C++ 2015-2022 x64 Runtime is required by local OpenVINO recognition." -ForegroundColor Yellow
Write-Host "Downloading the Microsoft-signed installer; Windows will request permission once." -ForegroundColor Yellow

$CacheDirectory = Join-Path $env:LOCALAPPDATA "JuxinIGAC\BuildCache"
$InstallerPath = Join-Path $CacheDirectory "vc_redist.x64.exe"
$TemporaryPath = Join-Path $CacheDirectory "vc_redist.x64.download.exe"
New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://aka.ms/vc14/vc_redist.x64.exe" -OutFile $TemporaryPath
    $Signature = Get-AuthenticodeSignature -FilePath $TemporaryPath
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $Signature.SignerCertificate -or
        $Signature.SignerCertificate.Subject -notmatch "Microsoft Corporation") {
        throw "downloaded Visual C++ runtime does not have a valid Microsoft Authenticode signature"
    }
    Move-Item -Path $TemporaryPath -Destination $InstallerPath -Force
} finally {
    if (Test-Path $TemporaryPath -PathType Leaf) {
        Remove-Item $TemporaryPath -Force
    }
}

$Process = Start-Process -FilePath $InstallerPath -ArgumentList "/install", "/passive", "/norestart" -Verb RunAs -Wait -PassThru
if ($Process.ExitCode -notin @(0, 1638, 3010)) {
    throw "Microsoft Visual C++ x64 runtime installation failed (exit code: $($Process.ExitCode)). Install $InstallerPath manually and rerun."
}

$RuntimeIssues = @(Get-RuntimeIssues)
if ($RuntimeIssues.Count -ne 0) {
    throw "Microsoft Visual C++ x64 runtime is still incomplete: $($RuntimeIssues -join '; '). Restart Windows if requested, then rerun."
}

Write-Host "Microsoft Visual C++ x64 runtime is ready." -ForegroundColor Green
