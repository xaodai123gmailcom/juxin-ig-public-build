@echo off
setlocal
title Juxin IG Audience Collector NewGen - Build Portable APP
cd /d "%~dp0"

echo.
echo ==================================================
echo   Juxin IG Audience Collector NewGen - Portable Builder
echo   Source revision: stability-r94
echo   This mode skips electron-builder and NSIS.
echo ==================================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1" -PortableOnly
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAILED] Portable APP was not created.
  if exist "%~dp0installer-output\dedup-regression-full.log" echo Also send installer-output\dedup-regression-full.log.
  if exist "%~dp0installer-output\dedup-diagnostic-full.log" echo Also send installer-output\dedup-diagnostic-full.log.
  if exist "%~dp0installer-output\stability-regression-full.log" echo Also send installer-output\stability-regression-full.log.
  if exist "%~dp0installer-output\core-preflight-full.log" echo Also send installer-output\core-preflight-full.log.
  if exist "%~dp0installer-output\backend-test-runner-full.log" echo Also send installer-output\backend-test-runner-full.log.
  if exist "%~dp0installer-output\task-control-regression-full.log" echo Also send installer-output\task-control-regression-full.log.
  if exist "%~dp0installer-output\collection-pipeline-regression-full.log" echo Also send installer-output\collection-pipeline-regression-full.log.
  if exist "%~dp0installer-output\collection-manual-r51-full.log" echo Also send installer-output\collection-manual-r51-full.log.
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
  if exist "%~dp0installer-output\embedded-runtime-full.log" echo Also send installer-output\embedded-runtime-full.log.
  if exist "%~dp0installer-output\embedded-browser-full.log" echo Also send installer-output\embedded-browser-full.log.
  if exist "%~dp0installer-output\embedded-browser-failure.json" echo Also send installer-output\embedded-browser-failure.json.
  if exist "%~dp0installer-output\embedded-shell-failure.json" echo Also send installer-output\embedded-shell-failure.json.
  if exist "%~dp0installer-output\embedded-shell-failure.png" echo Also send installer-output\embedded-shell-failure.png.
  if exist "%~dp0installer-output\embedded-posting-failure.json" echo Also send installer-output\embedded-posting-failure.json.
  if exist "%~dp0installer-output\embedded-posting-failure.png" echo Also send installer-output\embedded-posting-failure.png.
  if exist "%~dp0installer-output\posting-transition-r80-full.log" echo Also send installer-output\posting-transition-r80-full.log.
  if exist "%~dp0installer-output\pipeline-retained-r81-full.log" echo Also send installer-output\pipeline-retained-r81-full.log.
  if exist "%~dp0installer-output\embedded-account-surface.json" echo Also send installer-output\embedded-account-surface.json.
  if exist "%~dp0installer-output\embedded-account-surface.png" echo Also send installer-output\embedded-account-surface.png.
  pause
  exit /b %RC%
)

echo.
echo [DONE] Use only the current-run Portable output listed below.
if exist "%~dp0installer-output\LATEST_SUCCESS.txt" type "%~dp0installer-output\LATEST_SUCCESS.txt"
start "" "%~dp0installer-output"
pause
exit /b 0
