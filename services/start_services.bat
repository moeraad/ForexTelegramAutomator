@echo off
REM Start all six CopyTrades services.
REM Bot/Listener depend on Api so they wait for Api to come up.

NET SESSION >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator.
    pause
    exit /b 1
)

echo === Starting SMC stack ===
nssm start CT-SMC-Api
nssm start CT-SMC-Bot
nssm start CT-SMC-Listener

echo === Starting Arabic stack ===
nssm start CT-AR-Api
nssm start CT-AR-Bot
nssm start CT-AR-Listener

echo.
echo === Status ===
sc query CT-SMC-Api      | findstr STATE
sc query CT-SMC-Bot      | findstr STATE
sc query CT-SMC-Listener | findstr STATE
sc query CT-AR-Api       | findstr STATE
sc query CT-AR-Bot       | findstr STATE
sc query CT-AR-Listener  | findstr STATE
pause
