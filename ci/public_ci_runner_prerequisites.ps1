# Read-only prerequisites. Missing or untrusted system components stop the job.
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw 'Hosted Windows x64 is required'
}
function Assert-OrdinaryPath([string]$Path) {
    $Item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $Item) {
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Linked prerequisite path is not accepted' }
        if ($Item -is [IO.FileInfo]) { $Item = $Item.Directory } else { $Item = $Item.Parent }
    }
}
$SystemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
$MinimumRuntimeVersion = [Version]'14.44.35112.0'
foreach ($RuntimeName in @('MSVCP140.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll')) {
    $RuntimePath = Join-Path $SystemDirectory $RuntimeName
    if (-not (Test-Path -LiteralPath $RuntimePath -PathType Leaf)) { throw 'Required existing Microsoft runtime is missing' }
    Assert-OrdinaryPath $RuntimePath
    $Signature = Get-AuthenticodeSignature -FilePath $RuntimePath
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $Signature.SignerCertificate -or $Signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw 'Existing Microsoft runtime signature is invalid'
    }
    $VersionInfo = (Get-Item -LiteralPath $RuntimePath).VersionInfo
    $RuntimeVersion = [Version]("$($VersionInfo.FileMajorPart).$($VersionInfo.FileMinorPart).$($VersionInfo.FileBuildPart).$($VersionInfo.FilePrivatePart)")
    if ($RuntimeVersion -lt $MinimumRuntimeVersion) { throw 'Existing Microsoft runtime is too old' }
}
$Chrome = $null
foreach ($Variable in @('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')) {
    $Base = [Environment]::GetEnvironmentVariable($Variable)
    if ($Base) {
        $Candidate = Join-Path $Base 'Google\Chrome\Application\chrome.exe'
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) { $Chrome = $Candidate; break }
    }
}
if (-not $Chrome) { throw 'Existing Google Chrome is required on the hosted runner' }
Assert-OrdinaryPath $Chrome
$Signature = Get-AuthenticodeSignature -FilePath $Chrome
if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $null -eq $Signature.SignerCertificate -or $Signature.SignerCertificate.Subject -notmatch 'Google (LLC|Inc)') {
    throw 'Existing Google Chrome signature is invalid'
}
if ((Get-Item -LiteralPath $Chrome).VersionInfo.FileMajorPart -le 0) { throw 'Chrome version is unreadable' }
Write-Host 'PUBLIC_RUNNER_PREREQUISITES=PASS'
