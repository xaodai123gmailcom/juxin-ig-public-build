@echo off
setlocal
title Juxin IG Audience Collector NewGen - Install Runtime
cd /d "%~dp0"

echo.
echo ==================================================
echo   Installing Juxin IG Audience Collector NewGen runtime
echo ==================================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_windows.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAILED] Runtime installation did not finish.
  echo Please send a screenshot starting from the first error.
  if exist "%~dp0installer-output\python-environment-full.log" echo Also send installer-output\python-environment-full.log.
  if exist "%~dp0installer-output\pip-upgrade-full.log" echo Also send installer-output\pip-upgrade-full.log.
  if exist "%~dp0installer-output\python-dependencies-full.log" echo Also send installer-output\python-dependencies-full.log.
  if /I "%~1"=="--no-pause" exit /b %RC%
  pause
  exit /b %RC%
)

echo.
echo [DONE] Runtime installed. You can run open_app_windows.bat.
if /I "%~1"=="--no-pause" exit /b 0
pause
exit /b 0
