@echo off
REM Reset copytrades.db to a fresh, empty state.
REM
REM Usage:
REM   reset_db.bat          -- interactive (asks for "reset" confirmation)
REM   reset_db.bat --yes    -- skip confirmation prompt
REM
REM Stop the API / bot / listener BEFORE running this -- the script will
REM refuse if another process holds the DB lock.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [reset_db] .venv not found. Run setup.bat first.
    exit /b 2
)

".venv\Scripts\python.exe" -m scripts.reset_db %*
set RC=%ERRORLEVEL%

if %RC% neq 0 (
    echo.
    echo [reset_db] exited with code %RC%
)

REM Only pause when launched by double-click (no caller terminal).
REM cmdcmdline includes "/c" when the user ran us from a terminal;
REM otherwise it's the bare path and we should pause so the window
REM doesn't vanish before the user reads the output.
echo %cmdcmdline% | findstr /C:"/c" >nul
if errorlevel 1 pause

endlocal & exit /b %RC%
