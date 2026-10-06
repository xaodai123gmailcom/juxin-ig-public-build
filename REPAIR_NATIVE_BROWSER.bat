@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title Juxin Native Browser Runtime Repair
set "JUXIN_DIAG_HOME=%~dp0"
set "JUXIN_DIAG_PROJECT=%~1"
echo Select the source folder used for your previous build.
echo Verify the original bundled browser, then repair it once if needed.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -STA -Command ^
  "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);" ^
  "Add-Type -AssemblyName System.Windows.Forms;" ^
  "function Get-ProjectProblem([string]$folder) {" ^
  "  if ([string]::IsNullOrWhiteSpace($folder)) { return 'No folder selected.' };" ^
  "  foreach ($name in @('package.json','START_HERE_NEWGEN.bat','.venv\Scripts\python.exe','backend\app\browser_runtime.py')) {" ^
  "    if (-not (Test-Path -LiteralPath (Join-Path $folder $name) -PathType Leaf)) { return ('Missing: '+$name) }" ^
  "  };" ^
  "  if (-not (Test-Path -LiteralPath (Join-Path $folder 'build\browsers') -PathType Container)) { return 'Missing: build\browsers' };" ^
  "  try { $meta=[IO.File]::ReadAllText((Join-Path $folder 'package.json'),[Text.Encoding]::UTF8) | ConvertFrom-Json; if ($meta.name -ne 'juxin-ig-audience-collector-newgen') { return 'This is not a Juxin project.' } } catch { return 'Cannot read package.json.' };" ^
  "  return $null" ^
  "};" ^
  "$project=$null; $homePath=[IO.Path]::GetFullPath($env:JUXIN_DIAG_HOME);" ^
  "$candidates=@($env:JUXIN_DIAG_PROJECT,$homePath,(Split-Path -Parent $homePath.TrimEnd('\')));" ^
  "foreach ($candidate in $candidates) { if (-not (Get-ProjectProblem $candidate)) { $project=$candidate; break } };" ^
  "while (-not $project) {" ^
  "  $dialog=New-Object System.Windows.Forms.FolderBrowserDialog;" ^
  "  $dialog.Description='选择上次构建失败的聚鑫国际项目文件夹，其中包含 START_HERE_NEWGEN.bat';" ^
  "  $dialog.ShowNewFolderButton=$false;" ^
  "  if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { $dialog.Dispose(); Write-Host '已取消，未执行修复。'; exit 2 };" ^
  "  $selected=$dialog.SelectedPath; $dialog.Dispose(); $problem=Get-ProjectProblem $selected;" ^
  "  if ($problem) { [System.Windows.Forms.MessageBox]::Show(('此文件夹缺少原构建环境，请选择之前构建失败的目录。'+[Environment]::NewLine+$problem),'重新选择项目目录') | Out-Null } else { $project=$selected }" ^
  "};" ^
  "$python=Join-Path $project '.venv\Scripts\python.exe'; $script=Join-Path $homePath 'scripts\repair_native_browser.py';" ^
  "if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw 'Diagnostic script is missing. Extract the complete repair ZIP.' };" ^
  "$env:PYTHONIOENCODING='utf-8'; Write-Host ('使用原项目：'+$project);" ^
  "$env:JUXIN_DIAGNOSTIC_PROJECT_DIR=$project;" ^
  ". (Join-Path $homePath 'scripts\build_mutex.ps1'); $mutex=Enter-IgacBuildMutex;" ^
  "try { & $python $script --project-dir-env; $result=$LASTEXITCODE } finally { Exit-IgacBuildMutex $mutex };" ^
  "$output=Join-Path $project 'installer-output'; if (Test-Path -LiteralPath $output) { Invoke-Item -LiteralPath $output };" ^
  "if ($null -eq $result) { exit 1 }; exit $result"
set "DIAG_RC=%ERRORLEVEL%"
pause
exit /b %DIAG_RC%
