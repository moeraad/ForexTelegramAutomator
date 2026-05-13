@echo off
REM Uninstall all six CopyTrades services. Stops them first, then removes registrations.
REM Does NOT delete logs or project files.

NET SESSION >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator.
    pause
    exit /b 1
)

echo This will REMOVE all CopyTrades service registrations.
echo Project files, venvs, and logs are NOT affected.
choice /C YN /M "Continue"
if errorlevel 2 (
    echo Aborted.
    pause
    exit /b 0
)

echo === Stopping ===
nssm stop CT-SMC-Listener
nssm stop CT-SMC-Bot
nssm stop CT-SMC-Api
nssm stop CT-AR-Listener
nssm stop CT-AR-Bot
nssm stop CT-AR-Api

echo === Removing ===
nssm remove CT-SMC-Listener confirm
nssm remove CT-SMC-Bot      confirm
nssm remove CT-SMC-Api      confirm
nssm remove CT-AR-Listener  confirm
nssm remove CT-AR-Bot       confirm
nssm remove CT-AR-Api       confirm

echo.
echo === Done ===
pause
