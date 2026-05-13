@echo off
REM Build a channel-profile draft by sampling Telegram history + classifying
REM each message via OpenAI. Output: channels\<name>_draft.json
REM
REM Usage:
REM   build_channel_profile.bat                            -- default --name SMC
REM   build_channel_profile.bat --name SMC                 -- explicit
REM   build_channel_profile.bat --name SMC --limit 1000    -- broader sample
REM   build_channel_profile.bat --name SMC --since 2026-04-01
REM   build_channel_profile.bat --name SMC --model gpt-5
REM
REM Requires ajmclen.env at project root with TG_API_ID, TG_API_HASH,
REM TG_PHONE, OPENAI_API_KEY. First run prompts for the SMS code and
REM creates ajmclen.session.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [build_channel_profile] .venv not found. Run setup.bat first.
    exit /b 2
)

if not exist "ajmclen.env" (
    echo [build_channel_profile] Missing ajmclen.env at project root.
    echo Create it with TG_API_ID, TG_API_HASH, TG_PHONE, OPENAI_API_KEY.
    exit /b 2
)

if "%~1"=="" (
    ".venv\Scripts\python.exe" "scripts\build_channel_profile.py" --name SMC
) else (
    ".venv\Scripts\python.exe" "scripts\build_channel_profile.py" %*
)
set RC=%ERRORLEVEL%

if %RC% neq 0 (
    echo.
    echo [build_channel_profile] exited with code %RC%
)

REM Pause when launched by double-click so you can read the output. The
REM standard detection: cmdcmdline contains this bat's own filename when
REM Windows invoked us via `cmd /c "name.bat"` (i.e. double-click). It
REM does NOT contain the filename when the user typed the name into an
REM already-open cmd prompt.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause

endlocal & exit /b %RC%
