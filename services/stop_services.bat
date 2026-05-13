@echo off
REM Stop all six CopyTrades services.
REM Reverse dependency order: Bot/Listener first, then Api.

NET SESSION >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator.
    pause
    exit /b 1
)

echo === Stopping SMC stack ===
nssm stop CT-SMC-Listener
nssm stop CT-SMC-Bot
nssm stop CT-SMC-Api

echo === Stopping Arabic stack ===
nssm stop CT-AR-Listener
nssm stop CT-AR-Bot
nssm stop CT-AR-Api

echo.
echo === Status ===
sc query CT-SMC-Api      | findstr STATE
sc query CT-SMC-Bot      | findstr STATE
sc query CT-SMC-Listener | findstr STATE
sc query CT-AR-Api       | findstr STATE
sc query CT-AR-Bot       | findstr STATE
sc query CT-AR-Listener  | findstr STATE
pause
