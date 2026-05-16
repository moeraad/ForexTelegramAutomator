@echo off
setlocal EnableExtensions

REM ---------------------------------------------------------------------------
REM fix-services.bat
REM
REM Self-elevating launcher for fix-services.ps1. Patches NSSM
REM AppParameters on every CT-*-(Api|Bot|Listener) service so --db-path
REM is quoted -- without this NSSM splits the path on a space (e.g.
REM "Forex Engineer") and services boot pointing at a non-existent DB.
REM ---------------------------------------------------------------------------

REM Self-elevate to admin if we're not already.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "PS1=%~dp0fix-services.ps1"
if not exist "%PS1%" (
    echo Expected fix-services.ps1 next to this .bat:
    echo   %PS1%
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
pause
endlocal
