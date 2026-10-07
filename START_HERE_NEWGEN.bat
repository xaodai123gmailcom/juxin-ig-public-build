@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist "%~dp0package.json" (
  echo [FAILED] package.json is missing.
  echo Extract and keep the complete NewGen source folder together.
  pause
  exit /b 2
)

set "PACKAGE_JSON=%~dp0package.json"
set "APP_VERSION="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$package = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:PACKAGE_JSON ^| ConvertFrom-Json; [Console]::Write($package.version)"`) do set "APP_VERSION=%%V"
if not defined APP_VERSION (
  echo [FAILED] Could not read the application version from package.json.
  pause
  exit /b 2
)
set "EXPECTED_SETUP=Juxin-IG-Audience-Collector-NewGen-Setup-%APP_VERSION%-x64.exe"
set "EXPECTED_SETUP_PATH=%~dp0installer-output\%EXPECTED_SETUP%"
set "EXPECTED_HASH_PATH=%EXPECTED_SETUP_PATH%.sha256"
title Juxin IG Audience Collector NewGen v%APP_VERSION%

echo.
echo ============================================================
echo   Juxin IG Audience Collector NewGen v%APP_VERSION%
echo   Windows one-click installer build
echo   Source revision: stability-r94
echo ============================================================
echo.

findstr /c:"juxin-ig-audience-collector-newgen" "%~dp0package.json" >nul
if errorlevel 1 (
  echo [FAILED] This is not the independent NewGen v%APP_VERSION% source package.
  echo Do not continue with an old v0.2.51 directory.
  pause
  exit /b 3
)

call "%~dp0build_installer_windows.bat"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAILED] NewGen installer build returned exit code %RC%.
  echo Do not use an older EXE left in installer-output.
  echo Keep the first red error and these logs when present:
  if exist "%~dp0installer-output\dedup-regression-full.log" echo Also send installer-output\dedup-regression-full.log.
  if exist "%~dp0installer-output\dedup-diagnostic-full.log" echo Also send installer-output\dedup-diagnostic-full.log.
  if exist "%~dp0installer-output\stability-regression-full.log" echo Also send installer-output\stability-regression-full.log.
  if exist "%~dp0installer-output\core-preflight-full.log" echo Also send installer-output\core-preflight-full.log.
  if exist "%~dp0installer-output\backend-test-runner-full.log" echo Also send installer-output\backend-test-runner-full.log.
  if exist "%~dp0installer-output\task-control-regression-full.log" echo Also send installer-output\task-control-regression-full.log.
  if exist "%~dp0installer-output\collection-pipeline-regression-full.log" echo Also send installer-output\collection-pipeline-regression-full.log.
  if exist "%~dp0installer-output\collection-wait-r53-full.log" echo Also send installer-output\collection-wait-r53-full.log.
  if exist "%~dp0installer-output\review-reports-r54-full.log" echo Also send installer-output\review-reports-r54-full.log.
  if exist "%~dp0installer-output\review-workflow-r55-full.log" echo Also send installer-output\review-workflow-r55-full.log.
  if exist "%~dp0installer-output\report-split-r56-full.log" echo Also send installer-output\report-split-r56-full.log.
  if exist "%~dp0installer-output\collection-completion-r43-full.log" echo Also send installer-output\collection-completion-r43-full.log.
  if exist "%~dp0installer-output\collection-completion-r44-full.log" echo Also send installer-output\collection-completion-r44-full.log.
  if exist "%~dp0installer-output\collection-profile-r45-full.log" echo Also send installer-output\collection-profile-r45-full.log.
  if exist "%~dp0installer-output\collection-progress-r46-full.log" echo Also send installer-output\collection-progress-r46-full.log.
  if exist "%~dp0installer-output\collection-longrun-regression-full.log" echo Also send installer-output\collection-longrun-regression-full.log.
  if exist "%~dp0installer-output\collection-resource-regression-full.log" echo Also send installer-output\collection-resource-regression-full.log.
  if exist "%~dp0installer-output\window-lifecycle-regression-full.log" echo Also send installer-output\window-lifecycle-regression-full.log.
  if exist "%~dp0installer-output\window-performance-regression-full.log" echo Also send installer-output\window-performance-regression-full.log.
  if exist "%~dp0installer-output\screen-persistence-regression-full.log" echo Also send installer-output\screen-persistence-regression-full.log.
  if exist "%~dp0installer-output\chat-concurrency-regression-full.log" echo Also send installer-output\chat-concurrency-regression-full.log.
  if exist "%~dp0installer-output\specified-windows-regression-full.log" echo Also send installer-output\specified-windows-regression-full.log.
  if exist "%~dp0installer-output\discard-limits-regression-full.log" echo Also send installer-output\discard-limits-regression-full.log.
  if exist "%~dp0installer-output\discard-workflow-r38-regression-full.log" echo Also send installer-output\discard-workflow-r38-regression-full.log.
  if exist "%~dp0installer-output\preopen-dedupe-r39-regression-full.log" echo Also send installer-output\preopen-dedupe-r39-regression-full.log.
  if exist "%~dp0installer-output\monitor-partial-regression-full.log" echo Also send installer-output\monitor-partial-regression-full.log.
  if exist "%~dp0installer-output\zero-post-regression-full.log" echo Also send installer-output\zero-post-regression-full.log.
  if exist "%~dp0installer-output\recovery-regression-full.log" echo Also send installer-output\recovery-regression-full.log.
  if exist "%~dp0installer-output\greeting-regression-full.log" echo Also send installer-output\greeting-regression-full.log.
  if exist "%~dp0installer-output\release-packaging-r94-full.log" echo Also send installer-output\release-packaging-r94-full.log.
  if exist "%~dp0installer-output\source-contract-full.log" echo Also send installer-output\source-contract-full.log.
  if exist "%~dp0installer-output\build-desktop-full.log" echo Also send installer-output\build-desktop-full.log.
  if exist "%~dp0installer-output\backend-smoke-schema-r40-full.log" echo Also send installer-output\backend-smoke-schema-r40-full.log.
  if exist "%~dp0installer-output\build-source-full.log" echo Also send installer-output\build-source-full.log.
  if exist "%~dp0installer-output\build-source.json" echo Also send installer-output\build-source.json.
  if exist "%~dp0installer-output\python-environment-full.log" echo Also send installer-output\python-environment-full.log.
  if exist "%~dp0installer-output\pip-upgrade-full.log" echo Also send installer-output\pip-upgrade-full.log.
  if exist "%~dp0installer-output\timezone-data-full.log" echo Also send installer-output\timezone-data-full.log.
  if exist "%~dp0installer-output\python-dependencies-full.log" echo Also send installer-output\python-dependencies-full.log.
  if exist "%~dp0installer-output\embedded-runtime-full.log" echo Also send installer-output\embedded-runtime-full.log.
  if exist "%~dp0installer-output\embedded-browser-full.log" echo Also send installer-output\embedded-browser-full.log.
  if exist "%~dp0installer-output\embedded-browser-failure.json" echo Also send installer-output\embedded-browser-failure.json.
  if exist "%~dp0installer-output\embedded-shell-failure.json" echo Also send installer-output\embedded-shell-failure.json.
  if exist "%~dp0installer-output\embedded-shell-failure.png" echo Also send installer-output\embedded-shell-failure.png.
  if exist "%~dp0installer-output\pipeline-retained-r81-full.log" echo Also send installer-output\pipeline-retained-r81-full.log.
  if exist "%~dp0installer-output\embedded-account-surface.json" echo Also send installer-output\embedded-account-surface.json.
  if exist "%~dp0installer-output\embedded-account-surface.png" echo Also send installer-output\embedded-account-surface.png.
  if exist "%~dp0installer-output\pyinstaller-full.log" echo   installer-output\pyinstaller-full.log
  if exist "%~dp0installer-output\frozen-openvino-smoke.log" echo   installer-output\frozen-openvino-smoke.log
  if exist "%~dp0installer-output\electron-builder-full.log" echo   installer-output\electron-builder-full.log
  if exist "%~dp0installer-output\BUILD_FAILURE.txt" echo First send this file: installer-output\BUILD_FAILURE.txt
  exit /b %RC%
)

if not exist "%~dp0installer-output\LATEST_SUCCESS.txt" (
  echo.
  echo [FAILED] Build returned without installer-output\LATEST_SUCCESS.txt.
  echo No output from this run is trusted.
  pause
  exit /b 4
)

findstr /c:"TYPE=INSTALLER" "%~dp0installer-output\LATEST_SUCCESS.txt" >nul
if errorlevel 1 (
  echo.
  echo [FAILED] The current build did not produce a trusted Setup installer.
  echo No output from this run is trusted.
  type "%~dp0installer-output\LATEST_SUCCESS.txt"
  pause
  exit /b 5
)

findstr /c:"VERSION=%APP_VERSION%" "%~dp0installer-output\LATEST_SUCCESS.txt" >nul
if errorlevel 1 (
  echo.
  echo [FAILED] The success marker version does not match v%APP_VERSION%.
  type "%~dp0installer-output\LATEST_SUCCESS.txt"
  pause
  exit /b 5
)

set "RECORDED_SETUP_PATH="
set "RECORDED_SHA256="
set "RECORDED_HASH_PATH="
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0installer-output\LATEST_SUCCESS.txt") do (
  if /i "%%A"=="PATH" set "RECORDED_SETUP_PATH=%%B"
  if /i "%%A"=="SHA256" set "RECORDED_SHA256=%%B"
  if /i "%%A"=="SHA256_PATH" set "RECORDED_HASH_PATH=%%B"
)

if /i not "%RECORDED_SETUP_PATH%"=="%EXPECTED_SETUP_PATH%" (
  echo.
  echo [FAILED] The success marker does not point exactly to %EXPECTED_SETUP%.
  type "%~dp0installer-output\LATEST_SUCCESS.txt"
  pause
  exit /b 5
)
if /i not "%RECORDED_HASH_PATH%"=="%EXPECTED_HASH_PATH%" (
  echo.
  echo [FAILED] The success marker does not point to the exact Setup SHA-256 file.
  type "%~dp0installer-output\LATEST_SUCCESS.txt"
  pause
  exit /b 6
)
if not exist "%EXPECTED_SETUP_PATH%" (
  echo.
  echo [FAILED] The exact current-version Setup no longer exists.
  pause
  exit /b 6
)
if not exist "%EXPECTED_HASH_PATH%" (
  echo.
  echo [FAILED] The exact current-version Setup SHA-256 file is missing.
  pause
  exit /b 6
)
set "VERIFY_SETUP_PATH=%EXPECTED_SETUP_PATH%"
set "VERIFY_HASH_PATH=%EXPECTED_HASH_PATH%"
set "VERIFY_EXPECTED_HASH=%RECORDED_SHA256%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$actual=(Get-FileHash -Algorithm SHA256 -LiteralPath $env:VERIFY_SETUP_PATH).Hash.ToLowerInvariant(); $record=[IO.File]::ReadAllText($env:VERIFY_HASH_PATH,[Text.Encoding]::ASCII).Trim(); $expected=$actual+' *'+[IO.Path]::GetFileName($env:VERIFY_SETUP_PATH); if ($actual -ne $env:VERIFY_EXPECTED_HASH -or $record -ne $expected) { exit 1 }"
if errorlevel 1 (
  echo.
  echo [FAILED] The Setup SHA-256 does not match LATEST_SUCCESS.txt and its sidecar.
  pause
  exit /b 7
)

echo.
echo [OK] NewGen v%APP_VERSION% build completed.
echo Use only the files listed in installer-output\LATEST_SUCCESS.txt.
exit /b 0
