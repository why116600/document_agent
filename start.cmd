@echo off
rem =====================================================================
rem Document Agent Web service launcher for Windows (convenience wrapper)
rem
rem Calls start.ps1 with -ExecutionPolicy Bypass so it works even when the
rem default PowerShell execution policy is Restricted. Double-click this
rem file to start the service, or pass an action:
rem
rem   start.cmd start
rem   start.cmd stop
rem   start.cmd restart
rem   start.cmd status
rem
rem For the full option list run:
rem   powershell -ExecutionPolicy Bypass -File start.ps1 help
rem =====================================================================
setlocal
chcp 65001 >nul 2>&1
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

rem Double-clicked with no arguments: keep the window open to show the result.
if "%~1"=="" pause

exit /b %EXIT_CODE%
