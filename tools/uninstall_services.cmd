@echo off
REM CopyTrades - non-interactive service uninstaller.
REM
REM Called by the Inno Setup uninstaller via [UninstallRun] with
REM `shellexec; Verb: runas` so it runs elevated. Stops + deletes every
REM CT-* service registered with the Windows SCM via sc.exe so there is
REM no dependency on the bundled nssm.exe path (which moves whenever
REM PyInstaller layout shifts).
REM
REM Exits 0 on success or when no CT-* services exist. Exits 5 when
REM not elevated so the uninstaller surfaces the real reason instead of
REM silently leaving services behind.

setlocal EnableExtensions

net session >nul 2>&1
if errorlevel 1 (
    echo [CopyTrades] uninstall_services.cmd must run elevated. 1>&2
    exit /b 5
)

REM Enumerate every service whose SERVICE_NAME starts with "CT-".
REM `sc query state= all` lists stopped + running. `tokens=2` skips the
REM "SERVICE_NAME:" label and grabs the name itself.
REM
REM `sc stop` returns asynchronously; sleep 2s before delete so the
REM service has transitioned out of RUNNING, otherwise the delete is
REM merely queued (marked-for-deletion) and the service survives until
REM reboot, defeating the point of an "uninstall".
for /f "tokens=2" %%S in ('sc.exe query state^= all ^| findstr /b /c:"SERVICE_NAME: CT-"') do call :remove_one "%%S"

endlocal
exit /b 0

:remove_one
echo [CopyTrades] stopping %~1
sc.exe stop %1 >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1
echo [CopyTrades] deleting %~1
sc.exe delete %1 >nul 2>&1
exit /b 0
