@echo off
REM Quick status check for all six CopyTrades services.

echo === SMC stack ===
sc query CT-SMC-Api      | findstr "SERVICE_NAME STATE"
sc query CT-SMC-Bot      | findstr "SERVICE_NAME STATE"
sc query CT-SMC-Listener | findstr "SERVICE_NAME STATE"
echo.
echo === Arabic stack ===
sc query CT-AR-Api       | findstr "SERVICE_NAME STATE"
sc query CT-AR-Bot       | findstr "SERVICE_NAME STATE"
sc query CT-AR-Listener  | findstr "SERVICE_NAME STATE"
pause
