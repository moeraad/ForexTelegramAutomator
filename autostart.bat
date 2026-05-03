@echo off
rem ============================================================
rem  CopyTrades — autostart wrapper for Windows login
rem
rem  Chains the right order: services first, MT5 second.
rem  Order matters because the EA polls the API on OnInit;
rem  starting MT5 before the API is up just produces noisy
rem  WebRequest errors until the API binds (it'll recover, but
rem  it's tidier this way).
rem
rem  HOW TO INSTALL (pick ONE of these):
rem
rem  A. Startup folder (simplest):
rem     1. Press Win+R, type:  shell:startup    Enter.
rem     2. Right-click in that folder -> New -> Shortcut.
rem     3. Browse to this file (autostart.bat) and finish.
rem     Done. Runs every time you log in.
rem
rem  B. Task Scheduler (more robust — restart on failure,
rem     run hidden, wait for network, etc.):
rem     1. Win+R, type:  taskschd.msc    Enter.
rem     2. Create Basic Task. Name it "CopyTrades autostart".
rem     3. Trigger: "When I log on".
rem     4. Action: Start a program. Browse to this file.
rem     5. Finish. Then right-click the task -> Properties:
rem        - General: check "Run with highest privileges" if
rem          your MT5 install requires admin.
rem        - Conditions: UNCHECK "Start the task only if the
rem          computer is on AC power" (laptops).
rem        - Settings: check "If the task fails, restart every:
rem          1 minute, attempt up to: 3 times".
rem
rem  EDIT THESE BEFORE FIRST USE
rem ============================================================

rem --- 1. Path to the MT5 terminal executable ---
rem Common installs:
rem   C:\Program Files\MetaTrader 5\terminal64.exe
rem   C:\Users\<you>\AppData\Roaming\MetaQuotes\Terminal\<id>\terminal64.exe
rem Find yours: right-click the MT5 shortcut -> Properties -> Target.
set "MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe"

rem --- 2. Seconds to wait between launching services and MT5 ---
rem Lets the API bind to 127.0.0.1:8765 before the EA starts polling.
set "API_WARMUP_SEC=8"

rem ============================================================
rem  Implementation — usually no need to edit below
rem ============================================================

cd /d "%~dp0"

rem Brief network warm-up; helpful when triggered very early in login.
timeout /t 3 /nobreak >nul

rem Spawn the three Python services (api, bot, listener) — each
rem opens its own console window. launch.bat is itself idempotent
rem about the venv check.
echo [autostart] Starting Python services via launch.bat...
call launch.bat

echo [autostart] Waiting %API_WARMUP_SEC%s for API to bind...
timeout /t %API_WARMUP_SEC% /nobreak >nul

if not exist "%MT5_PATH%" (
    echo [autostart] ERROR: MT5 not found at:
    echo            %MT5_PATH%
    echo [autostart] Edit MT5_PATH at the top of this file.
    pause
    exit /b 1
)

echo [autostart] Launching MT5: %MT5_PATH%
start "" "%MT5_PATH%"

echo [autostart] Done. Services + MT5 should be coming up.
rem Don't pause — autostart should exit cleanly. The 3 service
rem console windows + MT5 keep the system running.
exit /b 0
