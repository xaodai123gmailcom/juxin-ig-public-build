@echo off
setlocal
title Juxin IG Audience Collector NewGen
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_source_app.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [FAILED] The APP did not start.
  echo Please send a screenshot starting from the first error.
  pause
  exit /b %RC%
)
exit /b 0
