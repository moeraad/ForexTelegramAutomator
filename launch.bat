@echo off
setlocal
cd /d %~dp0

if not exist ".venv\Scripts\activate.bat" (
  echo [launch] venv not found at .venv\Scripts\activate.bat
  echo [launch] create it with: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e ".[dev]"
  exit /b 1
)

start "copytrades-api"      cmd /k ".venv\Scripts\activate.bat && python -m src.api"
start "copytrades-bot"      cmd /k ".venv\Scripts\activate.bat && python -m src.bot"
start "copytrades-listener" cmd /k ".venv\Scripts\activate.bat && python -m src.listener"

endlocal
