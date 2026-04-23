@echo off
setlocal EnableDelayedExpansion
cd /d %~dp0

if not exist ".venv\Scripts\activate.bat" (
  echo [launch] venv not found at .venv\Scripts\activate.bat
  echo [launch] create it with: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e ".[dev]"
  pause
  exit /b 1
)

rem Guard: abort if port 8765 is already bound. Otherwise launch.bat spawns a
rem second API window that silently fails with WinError 10048 and the user
rem stares at a stale red-error window from a previous run. pause keeps the
rem message readable when launch.bat is double-clicked from Explorer.
set PORT_PID=
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765"') do set PORT_PID=%%P
if defined PORT_PID (
  echo [launch] port 8765 is already held by PID !PORT_PID!.
  echo [launch] stop the stale API first:
  echo [launch]   taskkill /F /PID !PORT_PID!
  echo [launch] or close the existing copytrades-api window, then re-run launch.bat.
  pause
  exit /b 1
)

start "copytrades-api"      cmd /k ".venv\Scripts\activate.bat && python -m src.api"
start "copytrades-bot"      cmd /k ".venv\Scripts\activate.bat && python -m src.bot"
start "copytrades-listener" cmd /k ".venv\Scripts\activate.bat && python -m src.listener"

endlocal
