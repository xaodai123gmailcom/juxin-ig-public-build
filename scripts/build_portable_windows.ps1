param([string]$PythonExecutable = "python")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
. "$PSScriptRoot\build_mutex.ps1"
. "$PSScriptRoot\invoke_native_logged.ps1"
$IgacBuildMutex = Enter-IgacBuildMutex
try {

# Avoid Windows PowerShell 5.1's ANSI fallback for UTF-8 JSON without a BOM.
# The file contains Chinese product text, while Node parses the JSON as UTF-8
# consistently on every supported Windows locale.
$VersionOutput = & node -p "require('./package.json').version"
if ($LASTEXITCODE -ne 0) {
    throw "Portable build failed: application version could not be read"
}
$Version = ([string]$VersionOutput).Trim()
if ([string]::IsNullOrWhiteSpace($Version) -or $Version -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.-]+)?$') {
    throw "Portable build failed: package.json returned invalid version '$Version'"
}
$ElectronDist = Join-Path $Root "node_modules\electron\dist"
$ElectronExe = Join-Path $ElectronDist "electron.exe"
$CoreExe = Join-Path $Root "dist\collector_core\collector_core.exe"

if (-not (Test-Path -LiteralPath $ElectronExe -PathType Leaf)) {
    throw "Portable build failed: node_modules\electron\dist\electron.exe was not found"
}
if (-not (Test-Path -LiteralPath $CoreExe -PathType Leaf)) {
    throw "Portable build failed: dist\collector_core\collector_core.exe was not found"
}
if (-not (Test-Path -LiteralPath "renderer\dist\index.html" -PathType Leaf)) {
    throw "Portable build failed: renderer output was not found"
}
if (-not (Test-Path -LiteralPath "dist-electron\main.js" -PathType Leaf)) {
    throw "Portable build failed: desktop output was not found"
}

$PortableName = "Juxin-IGAC-NewGen-Portable-v$Version-x64"
$PortableRoot = Join-Path $Root "installer-output\$PortableName"
$PortableZip = Join-Path $Root "installer-output\$PortableName.zip"
$Resources = Join-Path $PortableRoot "resources"
$AppResources = Join-Path $Resources "app"
$BackendResources = Join-Path $Resources "backend\collector_core"
$ProductExe = Join-Path $PortableRoot "Juxin IG Audience Collector NewGen.exe"

if (Test-Path -LiteralPath $PortableRoot) {
    Remove-Item -LiteralPath $PortableRoot -Recurse -Force
}
if (Test-Path -LiteralPath $PortableZip) {
    Remove-Item -LiteralPath $PortableZip -Force
}

New-Item -ItemType Directory -Path $PortableRoot -Force | Out-Null
Get-ChildItem -LiteralPath $ElectronDist -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $PortableRoot -Recurse -Force
}
Move-Item -LiteralPath (Join-Path $PortableRoot "electron.exe") $ProductExe -Force

New-Item -ItemType Directory -Path $AppResources -Force | Out-Null
New-Item -ItemType Directory -Path $BackendResources -Force | Out-Null
Copy-Item -LiteralPath "dist-electron" $AppResources -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $AppResources "renderer") -Force | Out-Null
Copy-Item -LiteralPath "renderer\dist" (Join-Path $AppResources "renderer") -Recurse -Force
Copy-Item -LiteralPath "package.json" $AppResources -Force
& node (Join-Path $PSScriptRoot "stage_portable_resources.mjs") $Root $AppResources
if ($LASTEXITCODE -ne 0) {
    throw "Portable build failed: runtime dependencies, icon or translator resources could not be staged"
}
Get-ChildItem -LiteralPath (Join-Path $Root "dist\collector_core") -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $BackendResources -Recurse -Force
}
$BrowserRuntime = Join-Path $Root "build\browsers"
if (-not (Test-Path -LiteralPath $BrowserRuntime -PathType Container)) { throw "Standalone browser runtime is missing" }
Copy-Item -LiteralPath $BrowserRuntime (Join-Path $Resources "browsers") -Recurse -Force
Copy-Item -LiteralPath "README_CN.md" $PortableRoot -Force
& node (Join-Path $PSScriptRoot "verify_packaged_browser_policy.cjs") $Root $Resources
if ($LASTEXITCODE -ne 0) { throw "Portable build failed: browser dependency seal is missing or changed" }
$RuntimeRequirement = [IO.File]::ReadAllText((Join-Path $Resources "browsers\juxin-runtime-requirement.json"), [Text.Encoding]::UTF8) | ConvertFrom-Json
$BrowserPrerequisite = "A verified bundled browser is included."
if ($RuntimeRequirement.mode -eq "installed-chrome-required") {
    $BrowserPrerequisite = "Requires installed Google Chrome $($RuntimeRequirement.minimum_version) or newer on this computer."
}

$StartApp = @'
@echo off
setlocal
cd /d "%~dp0"
start "" "%~dp0Juxin IG Audience Collector NewGen.exe"
exit /b 0
'@
$StartApp | Set-Content -LiteralPath (Join-Path $PortableRoot "START_APP.bat") -Encoding ASCII

$ShortcutScript = @'
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $Root "Juxin IG Audience Collector NewGen.exe"
$Desktop = [Environment]::GetFolderPath("Desktop")
$BrandName = -join (0x805A,0x946B,0x56FD,0x9645 | ForEach-Object { [char]$_ })
$Link = Join-Path $Desktop "$BrandName.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($Link)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = $BrandName
$Shortcut.Save()
Write-Host "Desktop shortcut created: $Link" -ForegroundColor Green
'@
$ShortcutScript | Set-Content -LiteralPath (Join-Path $PortableRoot "CREATE_DESKTOP_SHORTCUT.ps1") -Encoding UTF8

$ShortcutBat = @'
@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0CREATE_DESKTOP_SHORTCUT.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
'@
$ShortcutBat | Set-Content -LiteralPath (Join-Path $PortableRoot "CREATE_DESKTOP_SHORTCUT.bat") -Encoding ASCII

$Readme = @"
Juxin IG Audience Collector NewGen portable APP v$Version

1. $BrowserPrerequisite
2. Double-click START_APP.bat to run the APP.
3. Optional: double-click CREATE_DESKTOP_SHORTCUT.bat once.

Keep this entire folder together. Do not move only the EXE.
The portable build has the same collection and local-data features as the installer build.
"@
$Readme | Set-Content -LiteralPath (Join-Path $PortableRoot "READ_ME_FIRST.txt") -Encoding UTF8

# Probe the copies that will actually ship, with temporary application data.
& $PythonExecutable -I -X utf8 (Join-Path $PSScriptRoot "verify_frozen_core_service.py") --executable (Join-Path $BackendResources "collector_core.exe") --log (Join-Path $Root "installer-output\portable-core-service.log")
if ($LASTEXITCODE -ne 0) { throw "Portable build failed: staged Core service could not start and stop cleanly" }

$ArchiveExitCode = Invoke-IgacNativeCommandWithLog `
    -FilePath (Get-Command $PythonExecutable -CommandType Application -TotalCount 1 -ErrorAction Stop).Source `
    -ArgumentList @("-I", "-X", "utf8", (Join-Path $PSScriptRoot "package_portable_archive.py"), "--source", $PortableRoot, "--destination", $PortableZip) `
    -LogPath (Join-Path $Root "installer-output\portable-archive-full.log")
if ($ArchiveExitCode -ne 0) { throw "Portable build failed: ZIP content verification failed; see installer-output\portable-archive-full.log" }
if (-not (Test-Path -LiteralPath $ProductExe -PathType Leaf) -or (Get-Item -LiteralPath $ProductExe).Length -le 0) {
    throw "Portable build failed: product EXE was not created"
}
if (-not (Test-Path -LiteralPath $PortableZip -PathType Leaf) -or (Get-Item -LiteralPath $PortableZip).Length -le 0) {
    throw "Portable build failed: portable ZIP was not created"
}

Write-Host "Portable APP folder: $PortableRoot" -ForegroundColor Green
Write-Host "Portable APP ZIP: $PortableZip" -ForegroundColor Green

} finally {
    Exit-IgacBuildMutex $IgacBuildMutex
}
