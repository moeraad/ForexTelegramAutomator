@echo off
REM Compile ea\CopyTrades.mq5 via MetaEditor's command-line flag and surface
REM the compile log to this console. Saves a round-trip through the
REM MetaEditor GUI -- F4 from MT5, F7 -- whenever the .mq5 changes.
REM
REM Override the MetaEditor path with:
REM   set METAEDITOR=C:\full\path\to\metaeditor64.exe
REM
REM Exit code 0 = compile succeeded with 0 errors.

setlocal

set "EA_DIR=%~dp0"
set "EA_FILE=%EA_DIR%CopyTrades.mq5"
set "LOG_FILE=%EA_DIR%CopyTrades.compile.log"

if not exist "%EA_FILE%" (
    echo [compile_ea] ERROR: source not found: %EA_FILE%
    exit /b 2
)

REM Find metaeditor64.exe. Honour an explicit METAEDITOR env var first;
REM otherwise probe the usual install locations. The MT5 data directory
REM under %APPDATA%\MetaQuotes does NOT contain metaeditor -- that lives
REM next to terminal64.exe in the install folder.
if defined METAEDITOR (
    set "ME=%METAEDITOR%"
) else (
    call :find_me
)

if not defined ME (
    echo [compile_ea] ERROR: metaeditor64.exe not found.
    echo [compile_ea] Set METAEDITOR to the full path, e.g.:
    echo [compile_ea]   set METAEDITOR=C:\Program Files\MetaTrader 5\metaeditor64.exe
    exit /b 3
)

echo [compile_ea] MetaEditor: %ME%
echo [compile_ea] Source:     %EA_FILE%
echo [compile_ea] Log:        %LOG_FILE%
echo.

"%ME%" /compile:"%EA_FILE%" /log:"%LOG_FILE%"
REM MetaEditor's exit code counts errors + warnings, not just errors. A
REM clean compile with one deprecation warning returns 1. Parse the log's
REM "Result: N errors" line so warnings don't make the script look failed.

echo.
echo --- MetaEditor log -----------------------------------------------------
if exist "%LOG_FILE%" (
    type "%LOG_FILE%"
) else (
    echo [compile_ea] no log produced
    exit /b 4
)
echo ------------------------------------------------------------------------

REM MetaEditor's log is UTF-16 LE -- findstr only handles ASCII, so use
REM PowerShell to extract the error count. Looks for the "Result: N errors"
REM summary line and returns just N.
set "ERR_COUNT="
for /f "usebackq tokens=*" %%n in (`powershell -NoProfile -Command "(Select-String -Path '%LOG_FILE%' -Pattern 'Result:\s*(\d+)\s*errors').Matches.Groups[1].Value"`) do set "ERR_COUNT=%%n"

if not defined ERR_COUNT (
    echo [compile_ea] could not parse error count from log
    exit /b 5
)

if "%ERR_COUNT%"=="0" (
    echo [compile_ea] OK -- 0 errors
    exit /b 0
) else (
    echo [compile_ea] FAILED -- %ERR_COUNT% error/s
    exit /b 1
)


:find_me
for %%P in (
    "C:\Program Files\Fusion Markets MetaTrader 5\MetaEditor64.exe"
    "C:\Program Files\MetaTrader 5\metaeditor64.exe"
    "C:\Program Files\MetaTrader 5 EXNESS\metaeditor64.exe"
    "C:\Program Files\MetaTrader 5 IC Markets Global\metaeditor64.exe"
    "C:\Program Files (x86)\MetaTrader 5\metaeditor64.exe"
) do if exist %%P set "ME=%%~P"
exit /b 0
