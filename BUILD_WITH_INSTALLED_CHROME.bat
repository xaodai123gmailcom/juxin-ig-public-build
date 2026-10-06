@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo This edition requires installed Google Chrome on every target PC.
call "%~dp0build_installer_windows.bat" --installed-chrome
exit /b %ERRORLEVEL%
