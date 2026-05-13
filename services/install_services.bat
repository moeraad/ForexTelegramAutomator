@echo off
REM Install CopyTrades Windows Services via NSSM
REM Must run from elevated cmd (Administrator).

NET SESSION >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator.
    pause
    exit /b 1
)

where nssm >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: nssm.exe not found in PATH. Install NSSM first:
    echo   1. Download https://nssm.cc/release/nssm-2.24.zip
    echo   2. Extract nssm.exe to C:\Windows\System32\
    pause
    exit /b 1
)

set SMC=C:\Users\Administrator\Documents\Copy Trades
set SMCPY="%SMC%\.venv\Scripts\python.exe"
set AR=C:\Users\Administrator\Documents\projects\copytrades
set ARPY="%AR%\.venv\Scripts\python.exe"

if not exist %SMCPY% (
    echo ERROR: SMC venv not found at %SMCPY%
    pause
    exit /b 1
)
if not exist %ARPY% (
    echo ERROR: Arabic venv not found at %ARPY%
    pause
    exit /b 1
)

echo === Installing SMC stack ===

nssm install CT-SMC-Api %SMCPY% -m src.api
nssm set CT-SMC-Api AppDirectory "%SMC%"
nssm set CT-SMC-Api AppStdout "%SMC%\logs\nssm-api.out.log"
nssm set CT-SMC-Api AppStderr "%SMC%\logs\nssm-api.err.log"
nssm set CT-SMC-Api AppRotateFiles 1
nssm set CT-SMC-Api AppRotateBytes 10485760
nssm set CT-SMC-Api Start SERVICE_AUTO_START
nssm set CT-SMC-Api AppExit Default Restart
nssm set CT-SMC-Api AppRestartDelay 5000
nssm set CT-SMC-Api Description "CopyTrades SMC - FastAPI bridge (port 8766)"

nssm install CT-SMC-Bot %SMCPY% -m src.bot
nssm set CT-SMC-Bot AppDirectory "%SMC%"
nssm set CT-SMC-Bot AppStdout "%SMC%\logs\nssm-bot.out.log"
nssm set CT-SMC-Bot AppStderr "%SMC%\logs\nssm-bot.err.log"
nssm set CT-SMC-Bot AppRotateFiles 1
nssm set CT-SMC-Bot AppRotateBytes 10485760
nssm set CT-SMC-Bot Start SERVICE_AUTO_START
nssm set CT-SMC-Bot AppExit Default Restart
nssm set CT-SMC-Bot AppRestartDelay 5000
nssm set CT-SMC-Bot DependOnService CT-SMC-Api
nssm set CT-SMC-Bot Description "CopyTrades SMC - Telegram bot + promoter"

nssm install CT-SMC-Listener %SMCPY% -m src.listener
nssm set CT-SMC-Listener AppDirectory "%SMC%"
nssm set CT-SMC-Listener AppStdout "%SMC%\logs\nssm-listener.out.log"
nssm set CT-SMC-Listener AppStderr "%SMC%\logs\nssm-listener.err.log"
nssm set CT-SMC-Listener AppRotateFiles 1
nssm set CT-SMC-Listener AppRotateBytes 10485760
nssm set CT-SMC-Listener Start SERVICE_AUTO_START
nssm set CT-SMC-Listener AppExit Default Restart
nssm set CT-SMC-Listener AppRestartDelay 5000
nssm set CT-SMC-Listener DependOnService CT-SMC-Api
nssm set CT-SMC-Listener Description "CopyTrades SMC - Telethon channel listener"

echo === Installing Arabic stack ===

nssm install CT-AR-Api %ARPY% -m src.api
nssm set CT-AR-Api AppDirectory "%AR%"
nssm set CT-AR-Api AppStdout "%AR%\logs\nssm-api.out.log"
nssm set CT-AR-Api AppStderr "%AR%\logs\nssm-api.err.log"
nssm set CT-AR-Api AppRotateFiles 1
nssm set CT-AR-Api AppRotateBytes 10485760
nssm set CT-AR-Api Start SERVICE_AUTO_START
nssm set CT-AR-Api AppExit Default Restart
nssm set CT-AR-Api AppRestartDelay 5000
nssm set CT-AR-Api Description "CopyTrades Arabic - FastAPI bridge (port 8765)"

nssm install CT-AR-Bot %ARPY% -m src.bot
nssm set CT-AR-Bot AppDirectory "%AR%"
nssm set CT-AR-Bot AppStdout "%AR%\logs\nssm-bot.out.log"
nssm set CT-AR-Bot AppStderr "%AR%\logs\nssm-bot.err.log"
nssm set CT-AR-Bot AppRotateFiles 1
nssm set CT-AR-Bot AppRotateBytes 10485760
nssm set CT-AR-Bot Start SERVICE_AUTO_START
nssm set CT-AR-Bot AppExit Default Restart
nssm set CT-AR-Bot AppRestartDelay 5000
nssm set CT-AR-Bot DependOnService CT-AR-Api
nssm set CT-AR-Bot Description "CopyTrades Arabic - Telegram bot + promoter"

nssm install CT-AR-Listener %ARPY% -m src.listener
nssm set CT-AR-Listener AppDirectory "%AR%"
nssm set CT-AR-Listener AppStdout "%AR%\logs\nssm-listener.out.log"
nssm set CT-AR-Listener AppStderr "%AR%\logs\nssm-listener.err.log"
nssm set CT-AR-Listener AppRotateFiles 1
nssm set CT-AR-Listener AppRotateBytes 10485760
nssm set CT-AR-Listener Start SERVICE_AUTO_START
nssm set CT-AR-Listener AppExit Default Restart
nssm set CT-AR-Listener AppRestartDelay 5000
nssm set CT-AR-Listener DependOnService CT-AR-Api
nssm set CT-AR-Listener Description "CopyTrades Arabic - Telethon channel listener"

echo.
echo === Install complete. Services are registered but NOT started. ===
echo Stop any manual python -m src.api/bot/listener cmd windows BEFORE running start_services.bat
echo (port and bot-token conflicts will crash the services otherwise).
pause
