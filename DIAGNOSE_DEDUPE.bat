@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title Juxin Deduplication Diagnosis
set "JUXIN_DEDUPE_HOME=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "JUXIN_DEDUPE_PYTHON=%~dp0.venv\Scripts\python.exe"
echo This checks saved identity records. It does not repair or upload data.
if not exist "%JUXIN_DEDUPE_HOME%scripts\diagnose_dedupe.py" (
  echo [FAILED] scripts\diagnose_dedupe.py is missing. Extract the complete source package.
  pause
  exit /b 2
)
if not exist "%JUXIN_DEDUPE_PYTHON%" (
  echo Select the source folder used for your previous build. No file merging is needed.
  for /f "usebackq delims=" %%P in (`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -STA -Command "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); Add-Type -AssemblyName System.Windows.Forms; $dialog=New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description='选择之前构建过的聚鑫国际源码文件夹，其中应包含 .venv'; $dialog.ShowNewFolderButton=$false; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::WriteLine((Join-Path $dialog.SelectedPath '.venv\Scripts\python.exe')) }; $dialog.Dispose()"`) do set "JUXIN_DEDUPE_PYTHON=%%P"
)
if not exist "%JUXIN_DEDUPE_PYTHON%" (
  echo [FAILED] Existing build Python not found, or folder selection was cancelled.
  echo No new installation or complete build was started.
  pause
  exit /b 2
)
"%JUXIN_DEDUPE_PYTHON%" "%JUXIN_DEDUPE_HOME%scripts\diagnose_dedupe.py" %*
set "JUXIN_DEDUPE_RC=%ERRORLEVEL%"
if "%JUXIN_DEDUPE_RC%"=="0" (
  echo [OK] Open installer-output\dedup-diagnostic.json to inspect the report.
) else (
  echo [NOTICE] No readable Juxin database was found, or the report could not be saved.
  echo For a custom location, run: DIAGNOSE_DEDUPE.bat --database "FULL_PATH_TO_COLLECTOR.sqlite3"
)
pause
exit /b %JUXIN_DEDUPE_RC%
