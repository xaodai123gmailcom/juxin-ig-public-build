@echo off
setlocal
title Juxin R94 Fix20 - Quick Build Check
cd /d "%~dp0"
echo Checking Fix20 source and release process cleanup. No dependency installation.
node "%~dp0scripts\verify_build_source.mjs"
if errorlevel 1 goto failed
set "IGAC_FIX_CHECK_PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "IGAC_FIX_CHECK_PY=%~dp0.venv\Scripts\python.exe"
"%IGAC_FIX_CHECK_PY%" -I -X utf8 "%~dp0scripts\tests\test_release_packaging_r94.py" -v
if errorlevel 1 goto failed
echo.
echo BUILD_FIX20_CHECK=PASS
echo The focused self-check passed. You can now run START_HERE_NEWGEN.bat.
pause
exit /b 0
:failed
echo.
echo BUILD_FIX20_CHECK=FAIL
echo This command has not started the installer build. Keep the first error above.
pause
exit /b 1
