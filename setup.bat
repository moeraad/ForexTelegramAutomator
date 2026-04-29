@echo off
setlocal EnableDelayedExpansion

rem ============================================================
rem  CopyTrades — One-shot Windows setup
rem  Idempotent: safe to re-run.
rem ============================================================

cd /d "%~dp0"
echo.
echo [CopyTrades] Setup starting in %CD%
echo.

rem ---- 1. Check Python -------------------------------------------------
echo [1/5] Checking Python...
where python >nul 2>&1
if errorlevel 1 (
    echo   ERROR: python not found in PATH.
    echo   Install Python 3.11+ from https://www.python.org/downloads/
    echo   and re-run this script.
    goto :fail
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   Found Python !PYVER!

for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if !PYMAJOR! LSS 3 goto :badpy
if !PYMAJOR! EQU 3 if !PYMINOR! LSS 11 goto :badpy

rem ---- 2. Create venv --------------------------------------------------
echo.
echo [2/5] Creating virtual environment (.venv)...
if exist ".venv\Scripts\python.exe" (
    echo   .venv already exists, reusing.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo   ERROR: venv creation failed.
        goto :fail
    )
    echo   .venv created.
)

rem ---- 3. Install dependencies ----------------------------------------
echo.
echo [3/5] Installing dependencies (this can take a couple of minutes)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -e ".[dev]"
if errorlevel 1 (
    echo   ERROR: pip install failed. See output above.
    goto :fail
)
echo   Dependencies installed.

rem ---- 4. Scaffold .env ------------------------------------------------
echo.
echo [4/5] Configuring .env...
if exist ".env" (
    echo   .env already exists, leaving it alone.
) else (
    if not exist ".env.example" (
        echo   ERROR: .env.example missing. Cannot scaffold .env.
        goto :fail
    )
    copy /Y ".env.example" ".env" >nul
    echo   Created .env from template. EDIT IT before launching.
)

rem ---- 5. Logs dir + smoke test ---------------------------------------
echo.
echo [5/5] Preparing logs directory and smoke-testing the install...
if not exist "logs" mkdir logs
python -m pytest -q --no-header -k "not test_replay" 2>nul
if errorlevel 1 (
    echo   WARNING: hermetic test suite did not pass cleanly.
    echo   Setup completed but you should investigate before running live.
) else (
    echo   Tests pass.
)

rem ---- Done ------------------------------------------------------------
echo.
echo ============================================================
echo  Setup complete.
echo.
echo  Next steps:
echo    1. Edit .env and fill in your Telegram + AI credentials.
echo       See INSTALL.md for where to get each value.
echo    2. Find your Telegram channel ID:
echo         .venv\Scripts\python.exe scripts\find_channel_id.py
echo    3. Compile the EA:
echo         Open ea\CopyTrades.mq5 in MetaEditor, press F7.
echo         In MT5: Tools - Options - Expert Advisors - allow
echo         WebRequest for http://127.0.0.1:8765
echo    4. Start the services:
echo         launch.bat
echo ============================================================
echo.
goto :end

:badpy
echo   ERROR: Python !PYVER! is too old. Need 3.11 or newer.
goto :fail

:fail
echo.
echo [CopyTrades] Setup FAILED. Fix the error above and re-run.
exit /b 1

:end
endlocal
exit /b 0
