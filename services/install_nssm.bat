@echo off
REM One-time NSSM installer. Downloads, extracts, and places nssm.exe in System32.
REM Must run from elevated cmd (Administrator).

NET SESSION >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: This script must be run as Administrator.
    pause
    exit /b 1
)

where nssm >NUL 2>&1
IF %ERRORLEVEL% EQU 0 (
    echo NSSM already installed:
    nssm version
    pause
    exit /b 0
)

if not exist C:\Tools mkdir C:\Tools
echo Downloading NSSM 2.24...
curl -L -o C:\Tools\nssm.zip https://nssm.cc/release/nssm-2.24.zip
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Download failed.
    pause
    exit /b 1
)

echo Extracting...
if exist C:\Tools\nssm rmdir /s /q C:\Tools\nssm
mkdir C:\Tools\nssm
tar -xf C:\Tools\nssm.zip -C C:\Tools\nssm

echo Copying nssm.exe to C:\Windows\System32\...
copy /Y C:\Tools\nssm\nssm-2.24\win64\nssm.exe C:\Windows\System32\nssm.exe
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Copy failed.
    pause
    exit /b 1
)

echo.
echo === NSSM installed ===
nssm version
echo.
echo Next: run install_services.bat
pause
