# Exercise the already installed PowerShell 7 host; no system setup or settings.
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Existing PowerShell 7 is required' }
$PSNativeCommandUseErrorActionPreference = $true
& (Join-Path $PSScriptRoot '..\scripts\test_native_command_logging.ps1')
if (-not $PSNativeCommandUseErrorActionPreference) {
    throw 'Native logging probe did not restore the PowerShell 7 native exit preference'
}
Write-Host 'PUBLIC_POWERSHELL7_NATIVE_LOGGING=PASS'
