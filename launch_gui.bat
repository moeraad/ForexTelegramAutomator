@echo off
setlocal
cd /d %~dp0

rem One-click launcher for the CopyTrades GUI (PySide6 / qfluentwidgets).
rem Uses pythonw.exe so no console window lingers behind the GUI. The GUI
rem enforces a single-instance lock, so double-clicking again while it's
rem already open just surfaces the existing window.

if not exist ".venv\Scripts\pythonw.exe" (
  echo [launch_gui] venv not found at .venv\Scripts\pythonw.exe
  echo [launch_gui] create it with: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e ".[dev]"
  pause
  exit /b 1
)

rem `start ""` launches the GUI detached so this window can close immediately.
rem To see startup logs in a console instead, run:  .venv\Scripts\python.exe -m src.gui
start "" ".venv\Scripts\pythonw.exe" -m src.gui

endlocal
