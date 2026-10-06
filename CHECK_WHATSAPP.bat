@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title WhatsApp Same-Engine Check
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\check_whatsapp.ps1"
set "JUXIN_CHECK_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %JUXIN_CHECK_EXIT%
