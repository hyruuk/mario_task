@echo off
REM mario_task -- Windows installer.
REM
REM Double-click this file in File Explorer to install everything.
REM This just launches install.ps1 with the right execution policy;
REM all real work lives in the PowerShell script.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Starting mario_task installer...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install.ps1"
set "RC=%ERRORLEVEL%"

echo.
if %RC% NEQ 0 (
    echo Installer exited with error code %RC%.
    echo See above for the failing step and recovery hint.
) else (
    echo Installer finished successfully.
)

pause
exit /b %RC%
