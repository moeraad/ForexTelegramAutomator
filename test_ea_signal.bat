@echo off
setlocal EnableDelayedExpansion
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo [test_ea_signal] venv not found at .venv\Scripts\python.exe
  pause
  exit /b 1
)

set /p PRICE=Current XAUUSD price (e.g. 4734):
if "%PRICE%"=="" (
  echo [test_ea_signal] no price entered; aborting.
  pause
  exit /b 1
)

set SIDE=BUY
set /p SIDE=Side BUY/SELL [default BUY]:
if "%SIDE%"=="" set SIDE=BUY

set OFFSETS=10,20,35
set /p OFFSETS=TP offsets tp1,tp2,tp3 [default 10,20,35]:
if "%OFFSETS%"=="" set OFFSETS=10,20,35

echo.
echo Injecting %SIDE% at %PRICE% with TP offsets %OFFSETS% ...
echo.

.venv\Scripts\python.exe scripts\test_ea_signal.py --price %PRICE% --side %SIDE% --tp-offsets %OFFSETS%

echo.
pause
endlocal
