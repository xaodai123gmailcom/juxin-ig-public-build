@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title Juxin Legacy Unread Profile Diagnosis
set "JUXIN_UNREAD_ROOT=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "JUXIN_UNREAD_PYTHON=%JUXIN_UNREAD_ROOT%.venv\Scripts\python.exe"
if not exist "%JUXIN_UNREAD_ROOT%scripts\diagnose_unread_profiles.py" (
  echo [FAILED] Missing scripts\diagnose_unread_profiles.py. Extract the complete source package.
  pause
  exit /b 2
)
if not exist "%JUXIN_UNREAD_PYTHON%" (
  echo [FAILED] Build Python is missing. First run START_HERE_NEWGEN.bat in this source folder.
  pause
  exit /b 2
)
echo Checking old unread profile results without changing any database records.
"%JUXIN_UNREAD_PYTHON%" "%JUXIN_UNREAD_ROOT%scripts\diagnose_unread_profiles.py" %*
set "JUXIN_UNREAD_RC=%ERRORLEVEL%"
echo.
echo No database records were changed. For multiple logins, rerun with --owner LOGIN.
pause
exit /b %JUXIN_UNREAD_RC%
